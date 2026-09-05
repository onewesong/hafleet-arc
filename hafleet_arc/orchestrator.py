from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import textwrap
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Protocol

from .checkpoint import CheckpointStore
from .capabilities import build_capability_model
from .contracts import contract_gaps, ensure_contract_file, scenario_contracts
from .feedback import (
    blocking_findings,
    contract_review_passes,
    parse_review,
    parse_test_result,
    review_hash,
    review_passes,
    test_hash,
    test_passes,
    test_blocking_findings,
)
from .message_bus import MessageBus
from .models import RequirementModule
from .pipeline import Pipeline, load_pipeline
from .postflight import PostflightError, rehearse_web_app, validate_web_structure
from .test_runner import has_project_tests, persist_test_result, run_project_tests
from .log import log
from .worktree import WorktreeConflict, WorktreeManager


def _content_fingerprint(root: Path) -> tuple[tuple[str, str], ...]:
    entries: list[tuple[str, str]] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if relative.parts and relative.parts[0] in {".arc", ".git", "node_modules"}:
            continue
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue
        entries.append((relative.as_posix(), digest))
    return tuple(sorted(entries))


def _file_snapshot(root: Path) -> dict[str, bytes]:
    snapshot: dict[str, bytes] = {}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if relative.parts and relative.parts[0] in {".arc", ".git", "node_modules"}:
            continue
        try:
            snapshot[relative.as_posix()] = path.read_bytes()
        except OSError:
            continue
    return snapshot


def _restore_file_snapshot(root: Path, snapshot: dict[str, bytes]) -> None:
    current = _file_snapshot(root)
    for relative in set(current) - set(snapshot):
        try:
            (root / relative).unlink()
        except OSError:
            pass
    for relative, content in snapshot.items():
        target = root / relative
        if current.get(relative) != content:
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                target.write_bytes(content)
            except OSError:
                pass


class FleetDriver(Protocol):
    def run(self, role: str, prompt: str, workspace_dir: Path | None = None) -> object: ...


class RuntimeEvents(Protocol):
    def mark_design_started(self, node_id: str, message: str | None = None) -> None: ...
    def mark_design_done(self, node_id: str, message: str | None = None) -> None: ...
    def mark_design_failed(self, node_id: str, message: str | None = None) -> None: ...
    def mark_implementation_started(self, node_id: str, message: str | None = None) -> None: ...
    def mark_implementation_done(self, node_id: str, message: str | None = None) -> None: ...
    def mark_implementation_failed(self, node_id: str, message: str | None = None) -> None: ...


class RuntimeGit(Protocol):
    def commit(self, message: str, role: str = "reviewer") -> bool: ...


class RuntimeLike(Protocol):
    events: RuntimeEvents
    git: RuntimeGit


class PauseRequested(RuntimeError):
    pass


def _quality_round_budget(configured: int) -> int:
    """Resolve a bounded quality-loop budget for unattended executions.

    The pipeline remains the source of truth for its minimum budget.  Unattended
    runs get a small adaptive extension (two rounds by default), which avoids
    abandoning a useful repair on the hard boundary while remaining finite. A
    run-local operator can set an absolute budget through
    ``HAFLEET_QUALITY_MAX_ROUNDS`` or tune the extension with
    ``HAFLEET_QUALITY_EXTRA_ROUNDS``. Invalid values are ignored.
    """

    fallback = max(int(configured or 1), 1)
    raw = os.environ.get("HAFLEET_QUALITY_MAX_ROUNDS", "").strip()
    if not raw:
        try:
            extra = int(os.environ.get("HAFLEET_QUALITY_EXTRA_ROUNDS", "2"))
        except ValueError:
            extra = 2
        return fallback + max(extra, 0)
    try:
        override = int(raw)
    except ValueError:
        return fallback
    return max(override, 1)


def _verification_repair_budget(review_rounds: int) -> int:
    """Return a separate finite budget for deterministic test repair turns."""

    raw = os.environ.get("HAFLEET_VERIFICATION_MAX_REPAIRS", "").strip()
    if not raw:
        return max(int(review_rounds or 1), 1)
    try:
        return max(int(raw), 1)
    except ValueError:
        return max(int(review_rounds or 1), 1)


def _quality_stall_limit() -> int:
    """Allow one fresh recovery attempt before declaring repeated no progress."""

    try:
        return max(int(os.environ.get("HAFLEET_QUALITY_STALL_LIMIT", "2")), 1)
    except ValueError:
        return 2


def _contract_round_budget(configured: int) -> int:
    """Resolve the independent pre-implementation contract-review budget."""

    fallback = max(int(configured or 1), 1)
    raw = os.environ.get("HAFLEET_CONTRACT_MAX_ROUNDS", "").strip()
    if not raw:
        return fallback
    try:
        return max(int(raw), 1)
    except ValueError:
        return fallback


def _implementation_continuation_budget() -> int:
    """Return the finite number of semantic incomplete-result continuations."""

    try:
        return max(int(os.environ.get("HAFLEET_IMPLEMENTATION_CONTINUATIONS", "2")), 0)
    except ValueError:
        return 2


def _implementation_slice_threshold() -> int:
    """Return the module leaf threshold for requirement-tree implementation slices."""

    try:
        return max(int(os.environ.get("HAFLEET_IMPLEMENTATION_SLICE_LEAVES", "12")), 0)
    except ValueError:
        return 12


_IMPLEMENTATION_INCOMPLETE_PATTERNS = (
    re.compile(r"\b(?:i\s+)?(?:can(?:not|['’]t)|could\s+not|was\s+not\s+able\s+to)\s+(?:fully\s+)?complete\b", re.I),
    re.compile(r"\b(?:implementation|requested\s+work|task)\s+(?:is|remains)\s+(?:incomplete|unfinished)\b", re.I),
    re.compile(r"\bstill\s+(?:needs?|need)\s+to\s+be\s+implemented\b", re.I),
    re.compile(r"\bfuture\s+work\s+is\s+(?:still\s+)?needed\b", re.I),
    re.compile(r"\bremaining\s+work\s+(?:is|includes|requires|needs)\b", re.I),
)


def _implementation_response_incomplete(response: str) -> bool:
    """Detect an explicit refusal/deferral, without treating zero edits as failure."""

    text = str(response or "").strip()
    if not text:
        return False
    json_candidates = [text]
    json_candidates.extend(
        match.group(1).strip()
        for match in re.finditer(r"```(?:json)?\s*([\s\S]*?)```", text, re.I)
    )
    for candidate in json_candidates:
        try:
            payload = json.loads(candidate)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        status = str(payload.get("status") or payload.get("verdict") or "").strip().lower().replace("-", "_")
        if status in {"incomplete", "partial", "unfinished", "blocked", "implementation_incomplete"}:
            return True
        if payload.get("completed") is False:
            return True
        remaining = payload.get("remaining_work")
        if isinstance(remaining, (list, dict)) and bool(remaining):
            return True
        if isinstance(remaining, str) and remaining.strip().lower() not in {"", "none", "nothing", "n/a", "no"}:
            return True
    lowered = " ".join(text.lower().split())
    if any(
        phrase in lowered
        for phrase in (
            "no remaining work",
            "no future work is needed",
            "nothing remains to be implemented",
            "fully implemented and verified",
        )
    ):
        return False
    return any(pattern.search(text) for pattern in _IMPLEMENTATION_INCOMPLETE_PATTERNS)


def copy_template_contents(template_dir: Path, output_dir: Path) -> None:
    """Copy optional starter assets without overwriting resumed work."""

    if not template_dir.is_dir():
        return
    output_dir.mkdir(parents=True, exist_ok=True)
    for source in sorted(template_dir.iterdir()):
        if source.name == "template.yaml":
            continue
        destination = output_dir / source.name
        if destination.exists():
            continue
        if source.is_dir():
            shutil.copytree(source, destination)
        else:
            shutil.copy2(source, destination)


class FleetOrchestrator:
    def __init__(
        self,
        *,
        driver: FleetDriver,
        runtime: RuntimeLike,
        checkpoint: CheckpointStore,
        requirements_dir: Path,
        output_dir: Path,
        task_type: str,
        smoke_port: int = 3100,
        requirement_tree: dict[str, Any] | None = None,
        parallel: bool = False,
        max_workers: int = 2,
        bus: MessageBus | None = None,
        pipeline: Pipeline | None = None,
    ) -> None:
        self.driver = driver
        self.runtime = runtime
        self.checkpoint = checkpoint
        self.requirements_dir = requirements_dir
        self.output_dir = output_dir
        self.task_type = task_type
        self.smoke_port = smoke_port
        self.requirement_tree = requirement_tree
        self.parallel = bool(parallel)
        self.max_workers = max(int(max_workers), 1)
        self.plan_dir = output_dir / ".arc" / "hafleet" / "plans"
        self.contract_dir = output_dir / ".arc" / "hafleet" / "contracts"
        self.architecture_path = output_dir / ".arc" / "hafleet" / "architecture.md"
        configured_pause = os.environ.get("ARCBENCH_PAUSE_REQUEST_PATH", "").strip()
        self.pause_request_path = Path(configured_pause) if configured_pause else output_dir / ".arc" / "pause-request"
        self.bus = bus or MessageBus(output_dir / ".arc" / "hafleet" / "messages.jsonl")
        self.pipeline = pipeline or load_pipeline(output_dir)

    def _message(
        self,
        kind: str,
        sender: str,
        *,
        recipient: str = "orchestrator",
        module: RequirementModule | None = None,
        phase: str = "",
        round_number: int = 0,
        payload: dict[str, Any] | None = None,
        correlation_id: str = "",
        parent_id: str = "",
    ) -> dict[str, Any]:
        message = self.bus.publish(
            kind,
            sender=sender,
            recipient=recipient,
            module_id=module.node_id if module else "",
            phase=phase,
            round_number=round_number,
            payload=payload,
            correlation_id=correlation_id,
            parent_id=parent_id,
        )
        if module:
            self.checkpoint.update_pipeline(module.node_id, message_cursor=message.get("sequence", 0))
        return message

    def _run_agent(
        self,
        role: str,
        prompt: str,
        *,
        module: RequirementModule | None = None,
        phase: str = "",
        round_number: int = 0,
        workspace_dir: Path | None = None,
        parent_id: str = "",
    ) -> Any:
        request = self._message(
            "turn.request",
            "orchestrator",
            recipient=role,
            module=module,
            phase=phase,
            round_number=round_number,
            payload={"prompt": prompt[:20000]},
            parent_id=parent_id,
        )
        self._message(
            "turn.started",
            role,
            module=module,
            phase=phase,
            round_number=round_number,
            correlation_id=request["id"],
            payload={"workspace": str(workspace_dir or self.output_dir)},
            parent_id=request["id"],
        )
        try:
            # Start execution and corrective phases from authoritative durable
            # artifacts instead of carrying compressed conclusions across modules or
            # from a long planning/contract conversation. The complete requirement,
            # plan, contract, and feedback are present in each prompt; workspace files
            # and MessageBus history remain durable across fresh conversations.
            if phase in {
                "design",
                "contract-review",
                "contract-repair",
                "contract-reconciliation",
                "implement",
                "implement-slice",
                "implementation-continuation",
                "self-check",
                "completion",
                "test",
                "review",
                "final-review",
                "repair",
                "recovery",
                "integration",
            }:
                reset_thread = getattr(self.driver, "reset_thread", None)
                if callable(reset_thread):
                    reset_thread(role, workspace_dir=workspace_dir)
            result = self.driver.run(role, prompt, workspace_dir=workspace_dir)
        except TypeError as error:
            if "workspace_dir" not in str(error):
                raise
            result = self.driver.run(role, prompt)
        response = str(getattr(result, "final_response", "") or "")
        self._message(
            "turn.completed",
            role,
            module=module,
            phase=phase,
            round_number=round_number,
            correlation_id=request["id"],
            payload={"response": response[:20000]},
            parent_id=request["id"],
        )
        return result

    def _run_implementation_with_continuations(
        self,
        module: RequirementModule,
        prompt: str,
        *,
        phase: str = "implement",
        workspace_dir: Path | None = None,
    ) -> Any:
        """Continue explicit incomplete implementation handoffs before quality gates."""

        role = self.pipeline.role_for("implementer", "implementer")
        result = self._run_agent(
            role,
            prompt,
            module=module,
            phase=phase,
            workspace_dir=workspace_dir,
        )
        response = str(getattr(result, "final_response", "") or "")
        budget = _implementation_continuation_budget()
        attempt = 0
        while _implementation_response_incomplete(response) and attempt < budget:
            attempt += 1
            workspace = workspace_dir or self.output_dir
            fingerprint_before = _content_fingerprint(workspace)
            self.checkpoint.update_pipeline(
                module.node_id,
                node="implementation_continuation",
                phase=phase,
                loop_status="implementation_incomplete",
                implementation_continuation_attempt=attempt,
                implementation_incomplete_response=response[:20000],
            )
            self._message(
                "pipeline.state",
                "orchestrator",
                module=module,
                phase=phase,
                round_number=attempt,
                payload={
                    "status": "implementation_incomplete",
                    "attempt": attempt,
                    "max_continuations": budget,
                    "response": response[:20000],
                },
            )
            log(
                f"[hafleet]   implementation incomplete; continuing {module.node_id} "
                f"({attempt}/{budget})",
                flush=True,
            )
            continuation_prompt = prompt + f"""

Implementation continuation {attempt}/{budget}. Your preceding turn explicitly
reported that the requested implementation was not complete:

```text
{response[:8000]}
```

Continue the same module in the current workspace now. Re-read the complete supplied
requirement subtree, architecture, reviewed plan, scenario contract, and current files.
Implement the remaining product behavior and executable requirement-derived tests;
do not merely summarize, defer, apologize, or list future work. Run focused checks and
return concrete changed_files and verification evidence. If the workspace already
contains partial edits from the preceding turn, preserve and finish them.
"""
            result = self._run_agent(
                role,
                textwrap.dedent(continuation_prompt).strip(),
                module=module,
                phase="implementation-continuation",
                round_number=attempt,
                workspace_dir=workspace_dir,
            )
            response = str(getattr(result, "final_response", "") or "")
            fingerprint_after = _content_fingerprint(workspace)
            self._message(
                "pipeline.state",
                "orchestrator",
                module=module,
                phase=phase,
                round_number=attempt,
                payload={
                    "status": "implementation_continuation_completed",
                    "attempt": attempt,
                    "workspace_changed": fingerprint_after != fingerprint_before,
                    "still_incomplete": _implementation_response_incomplete(response),
                },
            )

        still_incomplete = _implementation_response_incomplete(response)
        self.checkpoint.update_pipeline(
            module.node_id,
            node=phase,
            phase=phase,
            loop_status="implementation_incomplete_exhausted" if still_incomplete else "implemented",
            implementation_continuation_attempt=attempt,
            implementation_incomplete_response=response[:20000] if still_incomplete else "",
        )
        if still_incomplete:
            self._message(
                "pipeline.state",
                "orchestrator",
                module=module,
                phase=phase,
                round_number=attempt,
                payload={
                    "status": "implementation_incomplete_exhausted",
                    "attempt": attempt,
                    "max_continuations": budget,
                },
            )
            log(
                f"[hafleet]   implementation continuation budget exhausted for "
                f"{module.node_id}; entering self-check fallback",
                flush=True,
            )
        return result

    def _implementation_slices(self, module: RequirementModule) -> list[dict[str, Any]]:
        """Split a large module by its author-defined first-level requirement domains."""

        threshold = _implementation_slice_threshold()
        if threshold <= 0 or self._module_leaf_count(module) < threshold:
            return []
        subtree = module.subtree if isinstance(module.subtree, dict) else {}
        children = subtree.get("children") or subtree.get("requirements") or []
        if not isinstance(children, list):
            return []
        slices = [dict(child) for child in children if isinstance(child, dict)]
        return slices if len(slices) >= 2 else []

    @staticmethod
    def _focus_prompt_on_slice(
        prompt: str,
        module: RequirementModule,
        slice_subtree: dict[str, Any],
    ) -> tuple[str, bool]:
        """Replace verbose module JSON blocks with the authoritative focused subtree."""

        focused = prompt
        replaced = False
        full_requirement = json.dumps(module.subtree, ensure_ascii=False, indent=2)
        focused_requirement = json.dumps(slice_subtree, ensure_ascii=False, indent=2)
        if full_requirement in focused:
            focused = focused.replace(full_requirement, focused_requirement, 1)
            replaced = True
        full_capability = json.dumps(
            build_capability_model(module.subtree), ensure_ascii=False, indent=2
        )
        focused_capability = json.dumps(
            build_capability_model(slice_subtree), ensure_ascii=False, indent=2
        )
        if full_capability in focused:
            focused = focused.replace(full_capability, focused_capability, 1)
            replaced = True
        return focused, replaced

    def _run_module_implementation(
        self,
        module: RequirementModule,
        prompt: str,
        *,
        workspace_dir: Path | None = None,
    ) -> Any:
        """Implement a large module in bounded requirement-tree slices when useful."""

        slices = self._implementation_slices(module)
        if not slices:
            return self._run_implementation_with_continuations(
                module,
                prompt,
                workspace_dir=workspace_dir,
            )

        state = self.checkpoint.read()
        slice_map = state.get("completed_implementation_slices_by_module") or {}
        completed = {
            str(item)
            for item in (
                slice_map.get(module.node_id, state.get("completed_implementation_slices", []))
                if isinstance(slice_map, dict)
                else state.get("completed_implementation_slices", [])
            )
            if str(item)
        }
        result: Any = None
        total = len(slices)
        log(
            f"[hafleet]   large module split into {total} requirement-domain implementation slices: "
            f"{module.node_id}",
            flush=True,
        )
        for index, slice_subtree in enumerate(slices, 1):
            slice_id = str(
                slice_subtree.get("id")
                or slice_subtree.get("req_id")
                or slice_subtree.get("requirement_id")
                or f"slice-{index}"
            )
            if slice_id in completed:
                log(f"[hafleet]   skipping completed implementation slice {slice_id}", flush=True)
                continue
            self.checkpoint.record_implementation_slice(
                module.node_id,
                slice_id,
                index,
                total,
                completed=False,
            )
            self._message(
                "pipeline.state",
                "orchestrator",
                module=module,
                phase="implement",
                round_number=index,
                payload={
                    "status": "implementation_slice_started",
                    "slice_id": slice_id,
                    "slice_index": index,
                    "slice_total": total,
                },
            )
            focused_prompt, replaced = self._focus_prompt_on_slice(
                prompt,
                module,
                slice_subtree,
            )
            fallback_subtree = (
                ""
                if replaced
                else f"""

Focused public requirement subtree:
```json
{json.dumps(slice_subtree, ensure_ascii=False, indent=2)}
```
"""
            )
            slice_prompt = focused_prompt + f"""

The coordinator is staging this large module by its author-defined requirement tree
so one turn is not consumed by unrelated workflows. This is implementation slice
{index}/{total}: {slice_id}. Implement every requirement and scenario in the focused
subtree below now, including its real domain state, API/UI behavior, persistence,
validation, navigation, and executable public-boundary tests. Read the full module plan
and scenario contract for shared decisions, preserve all behavior already implemented
by earlier slices, and build reusable foundations needed by later slices. Do not limit
your work to planning or placeholders, and do not implement later slices merely by
guessing their details. Return a structured completion summary for this slice.
{fallback_subtree}
"""
            result = self._run_implementation_with_continuations(
                module,
                textwrap.dedent(slice_prompt).strip(),
                phase="implement-slice",
                workspace_dir=workspace_dir,
            )
            completed.add(slice_id)
            self.checkpoint.record_implementation_slice(
                module.node_id,
                slice_id,
                index,
                total,
                completed=True,
            )
            self._message(
                "pipeline.state",
                "orchestrator",
                module=module,
                phase="implement",
                round_number=index,
                payload={
                    "status": "implementation_slice_completed",
                    "slice_id": slice_id,
                    "slice_index": index,
                    "slice_total": total,
                },
            )
        self.checkpoint.update_pipeline(
            module.node_id,
            node="implement",
            phase="implement",
            loop_status="implemented",
            implementation_slice_index=total,
            implementation_slice_total=total,
            implementation_slice_id="",
            completed_implementation_slices=sorted(completed),
        )
        return result

    def _deferred_quality_feedback(self) -> dict[str, list[dict[str, Any]]]:
        """Recover each deferred module's latest structured quality findings."""

        state = self.checkpoint.read()
        deferred = {str(item) for item in state.get("deferred_modules", []) if str(item)}
        if not deferred:
            return {}
        latest: dict[str, tuple[int, list[dict[str, Any]]]] = {}
        for message in self.bus.replay():
            module_id = str(message.get("module_id") or "")
            if module_id not in deferred:
                continue
            if message.get("kind") not in {"review.feedback", "test.feedback", "test.failed"}:
                continue
            payload = message.get("payload")
            findings = payload.get("findings", []) if isinstance(payload, dict) else []
            normalized = [dict(item) for item in findings if isinstance(item, dict)]
            if not normalized:
                continue
            sequence = int(message.get("sequence", 0) or 0)
            if module_id not in latest or sequence >= latest[module_id][0]:
                latest[module_id] = (sequence, normalized)

        # Legacy/incomplete logs may only have the current checkpoint fields.
        current = str(state.get("current_node_id") or "")
        checkpoint_findings = state.get("review_findings", [])
        if current in deferred and current not in latest and isinstance(checkpoint_findings, list):
            normalized = [dict(item) for item in checkpoint_findings if isinstance(item, dict)]
            if normalized:
                latest[current] = (0, normalized)
        return {module_id: findings for module_id, (_, findings) in sorted(latest.items())}

    @staticmethod
    def _tester_path_allowed(relative: str) -> bool:
        path = relative.replace("\\", "/")
        name = Path(path).name
        return (
            path.startswith("tests/")
            or path.startswith("frontend/tests/")
            # Playwright/common test runners emit diagnostics beside test code.
            or path.startswith("test-results/")
            or path.startswith("frontend/test-results/")
            or path.startswith("playwright-report/")
            or path.startswith("frontend/playwright-report/")
            or path.startswith("screenshots/")
            or path.startswith("frontend/screenshots/")
            or path.startswith("traces/")
            or path.startswith("frontend/traces/")
            or name.startswith("playwright.config")
            or path in {"package.json", "package-lock.json", "frontend/package.json", "frontend/package-lock.json", "frontend/pnpm-lock.yaml"}
        )

    def _tester_enabled(self, module: RequirementModule | None, workspace_dir: Path | None) -> bool:
        if os.environ.get("HAFLEET_TESTER", "1").strip().lower() in {"0", "false", "no"}:
            return False
        configured_tester = bool(self.pipeline.role_for("tester", ""))
        configured_tester = configured_tester or any(
            node.type == "loop" and bool(node.test)
            for node in self.pipeline.nodes
        )
        final_test_node = self.pipeline.node("final_test")
        configured_tester = configured_tester or bool(final_test_node and final_test_node.role)
        if not configured_tester:
            return False
        # Keep legacy empty test doubles and old output directories compatible;
        # real requirement subtrees contain children/scenarios or an existing test command.
        if module is not None:
            subtree = module.subtree if isinstance(module.subtree, dict) else {}
            meaningful = any(key in subtree for key in ("children", "requirements", "scenarios", "description", "acceptance"))
            if not meaningful:
                return False
        if module is None:
            root = workspace_dir or self.output_dir
            frontend = root / "frontend"
            return (frontend / "tests").exists() or (root / "tests").exists()
        return True

    def _planner_enabled(self) -> bool:
        """Return whether the configured pipeline still has a standalone planner.

        The built-in pipeline deliberately folds planning into the implementer turn.
        A run-local YAML may still declare a planner node, so retaining this check
        keeps older/custom pipelines resumable without forcing the extra turn on new
        runs.
        """

        return any(
            node.type == "agent" and node.role.strip().lower() == "planner"
            for node in self.pipeline.nodes
        )

    def _contract_review_enabled(self, module: RequirementModule | None) -> bool:
        """Enable the YAML-declared planning gate only for real scenario modules."""

        setting = os.environ.get("HAFLEET_CONTRACT_REVIEW", "1").strip().lower()
        if setting in {"0", "false", "no", "off"} or module is None:
            return False
        node = self.pipeline.node("contract_review")
        if node is None or node.type != "loop":
            return False
        subtree = module.subtree if isinstance(module.subtree, dict) else {}
        return bool(scenario_contracts(subtree))

    @staticmethod
    def _read_contract(path: Path) -> dict[str, Any]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _run_plan_only_agent(
        self,
        module: RequirementModule,
        base_prompt: str,
        plan_path: Path,
        contract_path: Path,
        *,
        workspace_dir: Path | None = None,
        phase: str = "design",
        round_number: int = 0,
        parent_id: str = "",
        feedback: dict[str, Any] | None = None,
    ) -> Any:
        """Let Implementer plan without allowing premature product-source edits."""

        workspace = workspace_dir or self.output_dir
        ensure_contract_file(contract_path, module.node_id, module.subtree)
        node = self.pipeline.node("contract_review")
        repair_instructions = str((node.options if node else {}).get("repair_prompt") or "").strip()
        if feedback:
            task = f"""
{repair_instructions}

Structured contract-review feedback:
```json
{json.dumps(feedback, ensure_ascii=False, indent=2)}
```
"""
        else:
            task = f"""
Planning-only turn. Do not implement or edit product source files yet. Write the
complete implementation plan to exactly {plan_path}. Fill the pre-created scenario
contract at exactly {contract_path}; keep one row for every supplied scenario ID.
For every row, populate planned_files, observable_checks, canonical_url (or the
literal string "not_applicable"), durable_state (or "not_applicable"), one stable
test_id, and concrete assertions. Preserve the original GIVEN/WHEN/THEN text. Inspect
author-provided reference assets when they define observable UI states. Split success,
validation, cancellation, empty, authorization, navigation, and persistence branches
into explicit checks rather than hiding them in a broad claim. Planning files under
.arc/hafleet are the only files you may modify in this turn.
"""
        before_files = _file_snapshot(workspace)
        result = self._run_agent(
            self.pipeline.role_for("implementation_plan", "implementer"),
            textwrap.dedent(base_prompt + "\n\n" + task).strip(),
            module=module,
            phase=phase,
            round_number=round_number,
            workspace_dir=workspace_dir,
            parent_id=parent_id,
        )
        after_files = _file_snapshot(workspace)
        changed_source = sorted(
            path for path in set(before_files) | set(after_files)
            if before_files.get(path) != after_files.get(path)
        )
        if changed_source:
            _restore_file_snapshot(workspace, before_files)
            self._message(
                "pipeline.state",
                "orchestrator",
                module=module,
                phase=phase,
                round_number=round_number,
                payload={"status": "planning_write_reverted", "files": changed_source},
                parent_id=parent_id,
            )
        self._ensure_plan_artifact(plan_path, module)
        ensure_contract_file(contract_path, module.node_id, module.subtree)
        self.checkpoint.update_pipeline(
            module.node_id,
            node="implementation_plan" if phase == "design" else "contract_repair",
            phase="design",
            contract_review_status="planned" if phase == "design" else "repairing",
            contract_review_round=round_number,
        )
        self._message(
            "agent.message",
            self.pipeline.role_for("implementation_plan", "implementer"),
            module=module,
            phase=phase,
            round_number=round_number,
            payload={
                "response": str(getattr(result, "final_response", "") or "")[:20000],
                "plan_path": str(plan_path),
                "contract_path": str(contract_path),
                "source_changes_reverted": changed_source,
            },
            parent_id=parent_id,
        )
        return result

    def _contract_review_loop(
        self,
        module: RequirementModule,
        base_prompt: str,
        plan_path: Path,
        contract_path: Path,
        *,
        workspace_dir: Path | None = None,
    ) -> dict[str, Any]:
        """Review and repair the public scenario contract before implementation."""

        node = self.pipeline.node("contract_review")
        if node is None or node.type != "loop":
            return {"verdict": "pass", "summary": "Contract review is not configured.", "findings": [], "checks": []}
        reviewer_role = node.review or "reviewer"
        repair_role = node.repair or "implementer"
        max_rounds = _contract_round_budget(node.max_rounds)
        parent_id = ""
        feedback: dict[str, Any] = {}
        for round_number in range(1, max_rounds + 1):
            self._check_pause(module, "contract-review")
            self.checkpoint.update_pipeline(
                module.node_id,
                node="contract_review",
                phase="design",
                contract_review_status="reviewing",
                contract_review_round=round_number,
            )
            contract = self._read_contract(contract_path)
            machine_gaps = contract_gaps(contract, module.subtree)
            prompt = base_prompt + f"""

Pre-implementation contract review round {round_number}/{max_rounds}. You are read-only.
Product implementation has not started for this module. Read the original public
requirement subtree and reference assets, then audit:
- implementation plan: {plan_path}
- scenario contract: {contract_path}

{str(node.options.get("prompt") or "").strip()}

The deterministic contract validator currently reports:
```json
{json.dumps(machine_gaps, ensure_ascii=False, indent=2)}
```

Return ONLY a JSON review object followed by a short summary. Use verdict=pass only
when every original scenario has a complete, non-invented and independently verifiable
contract row and there are no blocker/major findings. Do not execute tests, start a
server, modify files, or review implementation that does not exist yet.
"""
            review_workspace = workspace_dir or self.output_dir
            before_files = _file_snapshot(review_workspace)
            result = self._run_agent(
                reviewer_role,
                textwrap.dedent(prompt).strip(),
                module=module,
                phase="contract-review",
                round_number=round_number,
                workspace_dir=workspace_dir,
                parent_id=parent_id,
            )
            after_files = _file_snapshot(review_workspace)
            if before_files != after_files:
                _restore_file_snapshot(review_workspace, before_files)
                self.checkpoint.update_pipeline(
                    module.node_id,
                    reviewer_write_violation=True,
                    contract_review_status="blocked",
                )
                self._pause_pipeline(module, "contract-review", "reviewer modified project files during contract review")
            response = str(getattr(result, "final_response", "") or "")
            feedback = parse_review(response) if response.strip() else {
                "verdict": "pass",
                "summary": "Reviewer completed without structured findings.",
                "findings": [],
                "checks": [],
                "raw": "",
            }
            if machine_gaps:
                feedback["verdict"] = "changes_requested"
                findings = list(feedback.get("findings") or [])
                existing = {str(item.get("id") or "") for item in findings if isinstance(item, dict)}
                for gap in machine_gaps:
                    finding_id = "CONTRACT-" + hashlib.sha256(
                        f"{gap.get('scenario_id')}:{gap.get('field')}".encode("utf-8")
                    ).hexdigest()[:10].upper()
                    if finding_id in existing:
                        continue
                    findings.append(
                        {
                            "id": finding_id,
                            "severity": "major",
                            "title": f"Incomplete scenario contract: {gap.get('scenario_id')}",
                            "description": gap.get("message", "Scenario contract is incomplete."),
                            "files": [str(contract_path)],
                            "expected": f"Populate {gap.get('field')} from the supplied public scenario.",
                            "verification": "Re-run the pre-implementation contract review.",
                        }
                    )
                feedback["findings"] = findings
                feedback["summary"] = f"Contract validator found {len(machine_gaps)} incomplete scenario fields."
            feedback_message = self._message(
                "contract.feedback",
                reviewer_role,
                module=module,
                phase="contract-review",
                round_number=round_number,
                payload=feedback,
                parent_id=parent_id,
            )
            passed = contract_review_passes(feedback, machine_gaps)
            verdict = self._message(
                "contract.verdict",
                reviewer_role,
                module=module,
                phase="contract-review",
                round_number=round_number,
                payload={"verdict": feedback.get("verdict"), "passed": passed, "blocking_findings": blocking_findings(feedback)},
                parent_id=feedback_message["id"],
            )
            self.checkpoint.update_pipeline(
                module.node_id,
                contract_review_status="approved" if passed else "changes_requested",
                contract_review_round=round_number,
                last_contract_feedback_message_id=feedback_message["id"],
                last_contract_feedback_hash=review_hash(feedback),
                contract_findings=feedback.get("findings", []),
                carried_contract_findings=[] if passed else blocking_findings(feedback),
            )
            self.checkpoint.set_contract_obligations(
                module.node_id,
                [] if passed else blocking_findings(feedback),
            )
            if passed:
                self._message(
                    "pipeline.state",
                    "orchestrator",
                    module=module,
                    phase="contract-review",
                    round_number=round_number,
                    payload={"status": "contract_approved", "contract_path": str(contract_path)},
                    parent_id=verdict["id"],
                )
                return feedback
            if round_number < max_rounds:
                self.checkpoint.update_pipeline(
                    module.node_id,
                    node="contract_repair",
                    phase="design",
                    contract_review_status="repairing",
                )
                self._run_plan_only_agent(
                    module,
                    base_prompt,
                    plan_path,
                    contract_path,
                    workspace_dir=workspace_dir,
                    phase="contract-repair",
                    round_number=round_number,
                    parent_id=verdict["id"],
                    feedback=feedback,
                )
                parent_id = verdict["id"]
        # The last review still contains actionable information. Give the same
        # Implementer session one final reconciliation turn before unattended
        # implementation starts; otherwise the final Reviewer response would be
        # logged but never acted upon.
        if blocking_findings(feedback):
            self.checkpoint.update_pipeline(
                module.node_id,
                node="contract_reconciliation",
                phase="design",
                contract_review_status="reconciling",
            )
            self._run_plan_only_agent(
                module,
                base_prompt,
                plan_path,
                contract_path,
                workspace_dir=workspace_dir,
                phase="contract-reconciliation",
                round_number=max_rounds,
                parent_id=parent_id,
                feedback=feedback,
            )
        final_contract = self._read_contract(contract_path)
        final_machine_gaps = contract_gaps(final_contract, module.subtree)
        carried = blocking_findings(feedback)
        if final_machine_gaps:
            for gap in final_machine_gaps:
                carried.append(
                    {
                        "id": "CONTRACT-" + hashlib.sha256(
                            f"{gap.get('scenario_id')}:{gap.get('field')}".encode("utf-8")
                        ).hexdigest()[:10].upper(),
                        "severity": "major",
                        "title": f"Incomplete scenario contract: {gap.get('scenario_id')}",
                        "description": gap.get("message", "Scenario contract is incomplete."),
                        "files": [str(contract_path)],
                    }
                )
        # Preserve a de-duplicated set of obligations for implementation and
        # the later source/test review.
        carried_by_id = {
            str(item.get("id") or review_hash({"findings": [item]})): item
            for item in carried
            if isinstance(item, dict)
        }
        carried = list(carried_by_id.values())
        feedback["contract_status"] = "deferred"
        feedback["carried_findings"] = carried
        reason = f"contract review exceeded {max_rounds} rounds"
        self.checkpoint.update_pipeline(
            module.node_id,
            node="contract_review",
            contract_review_status="deferred",
            contract_findings=feedback.get("findings", []),
            carried_contract_findings=carried,
        )
        self.checkpoint.set_contract_obligations(module.node_id, carried)
        self._message(
            "pipeline.state",
            "orchestrator",
            module=module,
            phase="contract-review",
            payload={"status": "contract_deferred", "reason": reason},
        )
        if os.environ.get("HAFLEET_QUALITY_ON_EXHAUSTION", "defer").strip().lower() in {"pause", "stop", "fail"}:
            self._pause_pipeline(module, "contract-review", reason)
        log(f"[hafleet] contract review deferred ({module.node_id}): {reason}; implementation will use the latest feedback", flush=True)
        return feedback

    def _self_check_enabled(self, module: RequirementModule | None) -> bool:
        """Whether to run the short implementation self-check before review.

        The check is deliberately derived only from the supplied requirement tree. It
        gives an implementer a second, focused pass to catch omissions while the
        module context is still warm, without reading evaluator tests or introducing
        a product-specific validator. Empty synthetic modules used by legacy callers
        keep the historical single-turn behaviour.
        """
        if module is None:
            return False
        setting = os.environ.get("HAFLEET_SELF_CHECK", "auto").strip().lower()
        if setting in {"0", "false", "no"}:
            return False
        subtree = module.subtree if isinstance(module.subtree, dict) else {}
        # A nested requirement tree (or explicit scenarios/acceptance) is enough
        # evidence that a real module benefits from the extra pass.  Keep tiny
        # synthetic ``{id, name}`` modules used by legacy integrations on the old
        # single-turn path; operators can still force the check with
        # ``HAFLEET_SELF_CHECK=1``.
        meaningful = any(
            key in subtree
            for key in ("children", "requirements", "scenarios", "acceptance", "acceptance_criteria")
        )
        return setting in {"1", "true", "yes", "on"} or meaningful

    @staticmethod
    def _module_leaf_count(module: RequirementModule | None) -> int:
        """Count concrete requirement leaves without inspecting evaluator data."""
        if module is None or not isinstance(module.subtree, dict):
            return 0
        count = 0

        def visit(node: object) -> None:
            nonlocal count
            if not isinstance(node, dict):
                return
            children = node.get("children") or node.get("requirements") or []
            if isinstance(children, list) and children:
                for child in children:
                    visit(child)
            else:
                count += 1

        visit(module.subtree)
        return count

    def _completion_pass_enabled(self, module: RequirementModule | None) -> bool:
        """Enable a second implementation pass for genuinely large modules.

        Large ARC modules frequently contain several independent workflows. A single
        context window can produce a convincing shell while omitting entire leaves.
        This bounded pass is still the same Implementer role and remains driven only
        by the supplied requirement subtree. Small synthetic fixtures retain the old
        call sequence; operators can force/disable it explicitly.
        """
        setting = os.environ.get("HAFLEET_COMPLETION_PASS", "auto").strip().lower()
        if setting in {"0", "false", "no"}:
            return False
        if setting in {"1", "true", "yes", "on"}:
            return module is not None
        return self._module_leaf_count(module) >= 8

    def _run_implementer_completion_pass(
        self,
        module: RequirementModule,
        base_prompt: str,
        *,
        workspace_dir: Path | None = None,
        plan_path: Path | None = None,
    ) -> Any:
        """Ask Implementer to finish omitted leaves after the initial turn/self-check."""
        self.checkpoint.update_pipeline(
            module.node_id,
            node="completion",
            phase="implement",
            loop_status="completing",
        )
        prompt = base_prompt + f"""

Implementation completion pass. The module contains {self._module_leaf_count(module)}
concrete requirement leaves. Re-read the complete subtree, the architecture, and the
current files after the previous implementation/self-check. Enumerate every leaf and
identify what is actually implemented versus still a static placeholder, missing
route/API, missing state transition, or untested behavior. Now implement the missing
high-impact behavior in the current module, including real persistence and cross-view
handoffs where the requirements imply them. Do not merely write an audit or claim that
future work is needed: make concrete source and executable-test changes in this turn.
Prioritize domain entities/services and API contracts, then shared client state and
canonical routes, then view details. Preserve existing working behavior and module
boundaries. Do not search for or infer hidden/evaluator tests. Run focused tests/build
checks, register the exact argv commands in .arc/hafleet/verification.json, and return
JSON with changed_files, covered requirement IDs, checks, and any
remaining risks; an empty changed_files result is acceptable only when you can cite
evidence for every leaf being implemented.

Before declaring completion, exercise a public-boundary prerequisite matrix derived
from the subtree: click every named menu/dropdown entry and assert its destination;
submit ordinary valid typed values without requiring an undocumented autocomplete
selection; open every canonical route in a fresh browser context; and create/reset the
stateful fixtures needed by each workflow rather than borrowing records from an earlier
test. Run mutation suites twice from clean storage and fix order dependence. A passing
downstream test is not meaningful when its login, search, navigation, or seed-data
prerequisite was bypassed by directly injecting internal state.
"""
        result = self._run_agent(
            self.pipeline.role_for("implementer", "implementer"),
            textwrap.dedent(prompt).strip(),
            module=module,
            phase="completion",
            round_number=0,
            workspace_dir=workspace_dir,
        )
        self._message(
            "agent.message",
            self.pipeline.role_for("implementer", "implementer"),
            module=module,
            phase="completion",
            payload={"response": str(getattr(result, "final_response", "") or "")[:20000]},
        )
        return result

    def _run_implementer_self_check(
        self,
        module: RequirementModule,
        base_prompt: str,
        *,
        workspace_dir: Path | None = None,
        plan_path: Path | None = None,
    ) -> Any:
        """Run a bounded, requirement-derived implementation completeness pass.

        This is intentionally an Implementer turn rather than a new role. It is a
        short feedback-free audit that asks the same agent to inspect its own output,
        exercise public contracts on the smoke port, and repair concrete omissions.
        The final Reviewer remains read-only and authoritative for approval.
        """
        round_number = 0
        self.checkpoint.update_pipeline(
            module.node_id,
            node="self_check",
            phase="implement",
            loop_status="self_checking",
        )
        prompt = base_prompt + f"""

Implementation self-check (before read-only review). Re-read the complete requirement
subtree and your plan at {plan_path or 'the module plan'}. Inspect the files you just
changed and perform a concise completeness pass. Build a requirement traceability
matrix in your response (requirement/scenario -> implementation path -> observable
check), then repair concrete omissions you can verify from the supplied requirements.
Treat test quality as part of completeness: if existing tests only scan source strings,
assert unconditional markup, or otherwise do not exercise behavior, replace them with
real HTTP/API or browser-DOM checks and run them. Do not report a gap without editing
the affected file when the supplied requirements make the correction actionable.
For web tasks, use only a short-lived smoke server on port {self.smoke_port} and check
the public URL/API contracts: direct navigation and refresh, semantic labels and
accessible names, success/validation/conflict/not-found/error/empty states, and
durable state after reload where applicable. Run focused build/unit checks, but do
not search for or infer hidden/evaluator tests. Assert the canonical URL after every
successful navigation/form transition, especially prerequisite auth and primary-list
flows; success text alone is insufficient evidence. Use runtime/configured dates, not
the generation date. Register every executed argv command in
.arc/hafleet/verification.json. Keep changes inside this module and
return JSON with changed_files, covered_requirements, checks, and remaining_risks.
Do not merely describe gaps: fix concrete gaps before returning.

For each scenario whose WHEN step names a link, menu item, dropdown entry, form input,
or button, exercise that exact rendered entry action rather than navigating directly
or setting internal state. Verify that ordinary valid text input is accepted when the
requirement does not mandate choosing a suggestion. For authenticated or stateful
flows, provision the precondition from a fresh isolated store through an app-owned
public setup path, then repeat the scenario after reload and in a second clean context.
Run the module's state-mutating tests twice to expose shared-fixture consumption and
test-order dependencies before handing off to Reviewer.
"""
        result = self._run_agent(
            self.pipeline.role_for("implementer", "implementer"),
            textwrap.dedent(prompt).strip(),
            module=module,
            phase="self-check",
            round_number=round_number,
            workspace_dir=workspace_dir,
        )
        self._message(
            "agent.message",
            self.pipeline.role_for("implementer", "implementer"),
            module=module,
            phase="self-check",
            payload={"response": str(getattr(result, "final_response", "") or "")[:20000]},
        )
        return result

    @staticmethod
    def _ensure_plan_artifact(plan_path: Path, module: RequirementModule) -> None:
        """Ensure a durable plan exists when implementer-owned planning is used.

        The implementer is instructed to write the concrete plan. If an adapter
        returns without creating it, retain a small requirement-derived artifact
        rather than spending another model turn solely to recreate a missing file.
        """

        if plan_path.is_file() and plan_path.read_text(encoding="utf-8", errors="ignore").strip():
            return
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        requirement = json.dumps(module.subtree, ensure_ascii=False, indent=2)
        plan_path.write_text(
            "# Implementation plan\n\n"
            "The Implementer role did not return the required plan artifact. This "
            "requirement-derived fallback preserves the authoritative module context "
            "for contract review and a later planning repair:\n\n"
            "```json\n" + requirement + "\n```\n",
            encoding="utf-8",
        )

    def _run_tester(
        self,
        module: RequirementModule | None,
        base_prompt: str,
        *,
        tester_role: str | None = None,
        workspace_dir: Path | None = None,
        round_number: int = 1,
        mode: str = "module",
        parent_id: str = "",
    ) -> dict[str, Any]:
        tester_role = tester_role or self.pipeline.role_for("tester", "tester")
        module_id = module.node_id if module else "ROOT"
        workspace = workspace_dir or self.output_dir
        self.checkpoint.update_pipeline(module_id, node="tester", phase="test", round_number=round_number, loop_status="testing", test_status="testing")
        request = self._message(
            "test.request",
            "orchestrator",
            recipient=tester_role,
            module=module,
            phase="test",
            round_number=round_number,
            payload={"mode": mode, "workspace": str(workspace)},
            parent_id=parent_id,
        )
        self._message(
            "test.started",
            tester_role,
            module=module,
            phase="test",
            round_number=round_number,
            payload={"mode": mode, "workspace": str(workspace)},
            correlation_id=request["id"],
            parent_id=request["id"],
        )
        before_files = _file_snapshot(workspace)
        prompt = base_prompt + f"""

Test round {round_number}. You are the test agent in {mode} mode. Generate or update
tests for the supplied ARC requirements, then execute them against smoke port {self.smoke_port}.
Only modify tests, Playwright configuration, and test dependency manifests. Never modify
implementation source files. Return the required structured Tester JSON response.
"""
        result = self._run_agent(tester_role, prompt.strip(), module=module, phase="test", round_number=round_number, workspace_dir=workspace_dir, parent_id=request["id"])
        after_files = _file_snapshot(workspace)
        changed_paths = {
            path for path in set(before_files) | set(after_files)
            if before_files.get(path) != after_files.get(path)
        }
        # Browser tests may exercise persistence and leave runtime records in
        # backend/data (for example a registered test user). Restore those
        # side-effects before evaluating Tester file authorization.
        runtime_data = {
            path for path in changed_paths
            if path.startswith("backend/data/") and Path(path).suffix.lower() in {".json", ".db", ".sqlite"}
        }
        for relative in runtime_data:
            target = workspace / relative
            if relative in before_files:
                try:
                    target.write_bytes(before_files[relative])
                except OSError:
                    pass
            else:
                try:
                    target.unlink()
                except OSError:
                    pass
        unauthorized = {
            path for path in changed_paths
            if path not in runtime_data and not self._tester_path_allowed(path)
        }
        if unauthorized:
            _restore_file_snapshot(workspace, before_files)
            self.checkpoint.update_pipeline(module_id, tester_write_violation=True, test_status="blocked", loop_status="blocked")
            self.checkpoint.mark_paused(module_id, "test")
            self._message("pipeline.state", "orchestrator", module=module, phase="test", round_number=round_number, payload={"status": "tester_write_violation", "files": sorted(unauthorized)}, parent_id=request["id"])
            raise PauseRequested("tester modified implementation files")
        response = str(getattr(result, "final_response", "") or "")
        parsed = parse_test_result(response, module_id=module_id, round_number=round_number) if response.strip() else {
            "verdict": "pass", "summary": "Tester completed without structured findings.",
            "module_id": module_id, "round": round_number, "tests": [], "findings": [], "checks": [], "artifacts": {}, "raw": "",
        }
        # If the agent created a runnable e2e command, execute it centrally so the
        # result is reproducible and server cleanup is guaranteed.
        package = workspace / "frontend" / "package.json"
        if self.task_type == "web" and package.is_file():
            try:
                package_data = json.loads(package.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                package_data = {}
            scripts = package_data.get("scripts") if isinstance(package_data, dict) else {}
            if isinstance(scripts, dict) and scripts.get("test:e2e"):
                executed = run_project_tests(workspace, task_type=self.task_type, module_id=module_id, round_number=round_number, smoke_port=self.smoke_port)
                parsed.setdefault("checks", [])
                parsed["checks"].extend(executed.get("checks", []))
                parsed.setdefault("artifacts", {}).update(executed.get("artifacts", {}))
                if not executed.get("verdict") == "pass":
                    parsed["verdict"] = "changes_requested"
                    parsed.setdefault("findings", []).extend(executed.get("findings", []))
        parsed["round"] = round_number
        parsed["module_id"] = module_id
        # Persist each generated case/artifact as a first-class bus event so the
        # dashboard can show test creation separately from execution results.
        for case in parsed.get("tests", []):
            if isinstance(case, dict):
                self._message(
                    "test.case.generated",
                    tester_role,
                    module=module,
                    phase="test",
                    round_number=round_number,
                    payload={"test": case},
                    parent_id=request["id"],
                )
        artifacts = parsed.get("artifacts") if isinstance(parsed.get("artifacts"), dict) else {}
        for artifact_name, artifact_path in artifacts.items():
            if artifact_path:
                self._message(
                    "test.artifact.created",
                    tester_role,
                    module=module,
                    phase="test",
                    round_number=round_number,
                    payload={"name": artifact_name, "path": artifact_path},
                    parent_id=request["id"],
                )
        persist_test_result(workspace, module_id, round_number, parsed)
        result_hash = test_hash(parsed)
        kind = "test.completed" if test_passes(parsed) else "test.failed"
        completed = self._message(kind, tester_role, module=module, phase="test", round_number=round_number, payload=parsed, parent_id=request["id"])
        if not test_passes(parsed):
            self._message("test.feedback", tester_role, recipient="orchestrator", module=module, phase="test", round_number=round_number, payload={"result": parsed, "finding_ids": [item.get("id") for item in test_blocking_findings(parsed)]}, parent_id=completed["id"])
        self.checkpoint.update_pipeline(module_id, test_status="passed" if test_passes(parsed) else "failed", last_test_message_id=completed["id"], last_test_hash=result_hash, test_results=parsed.get("tests", []))
        return parsed

    def _run_registered_project_tests(
        self,
        module: RequirementModule | None,
        *,
        workspace_dir: Path | None,
        round_number: int,
        parent_id: str = "",
    ) -> dict[str, Any]:
        """Execute Implementer-authored tests without introducing a Tester role."""

        workspace = workspace_dir or self.output_dir
        module_id = module.node_id if module else "ROOT"
        started = self._message(
            "test.started",
            "orchestrator",
            module=module,
            phase="verify",
            round_number=round_number,
            payload={"mode": "integration" if module is None else "module", "source": "project_verification"},
            parent_id=parent_id,
        )
        # Verification is observational. Tests may exercise real persistence, but
        # their fixture mutations and generated build files must not become product
        # changes or perturb no-progress detection. Preserve the exact project state
        # while retaining .arc test reports and dependency caches.
        before_files = _file_snapshot(workspace)
        try:
            result = run_project_tests(
                workspace,
                task_type=self.task_type,
                module_id=module_id,
                round_number=round_number,
                smoke_port=self.smoke_port,
            )
        finally:
            _restore_file_snapshot(workspace, before_files)
        kind = "test.completed" if test_passes(result) else "test.failed"
        completed = self._message(
            kind,
            "orchestrator",
            module=module,
            phase="verify",
            round_number=round_number,
            payload=result,
            parent_id=started["id"],
        )
        self.checkpoint.update_pipeline(
            module_id,
            test_status="passed" if test_passes(result) else "failed",
            last_test_message_id=completed["id"],
            last_test_hash=test_hash(result),
        )
        return result

    def _review_loop(
        self,
        module: RequirementModule | None,
        base_prompt: str,
        *,
        workspace_dir: Path | None = None,
        final_review: bool = False,
        resume_round: int = 0,
        resume_feedback: dict[str, Any] | None = None,
        resume_message_id: str = "",
    ) -> dict[str, Any]:
        # Select the loop declared for this stage.  Older custom pipelines may
        # only define one loop, so retain the first-loop fallback for backwards
        # compatibility while allowing a distinct final integration loop.
        loop = self.pipeline.loop("final_review" if final_review else "quality_loop")
        reviewer_role = loop.review or "reviewer"
        repair_role = loop.repair or "implementer"
        max_rounds = _quality_round_budget(loop.max_rounds)
        max_verification_repairs = _verification_repair_budget(max_rounds)
        stall_limit = _quality_stall_limit()
        previous_hash = ""
        previous_fingerprint: tuple[tuple[str, str], ...] | None = None
        previous_test_hash = ""
        previous_test_fingerprint: tuple[tuple[str, str], ...] | None = None
        verification_repairs = 0
        verification_stalls = 0
        review_stalls = 0
        feedback: dict[str, Any] = {}
        parent_id = ""
        first_round = 1
        if resume_feedback and resume_round > 0:
            # A process may stop after a reviewer has requested changes but
            # before the repair turn starts. Resume from that durable message
            # instead of rerunning planner/implementation or losing feedback.
            if resume_round >= max_rounds:
                if module:
                    self.checkpoint.update_pipeline(module.node_id, loop_status="blocked")
                self._defer_quality(module, "review", f"review loop exceeded {max_rounds} rounds")
                return resume_feedback
            repair_round = resume_round
            repair_prompt = base_prompt + f"""

Repair loop round {repair_round}. This is a resumed pipeline node. Reviewer feedback is authoritative:
{json.dumps(resume_feedback, ensure_ascii=False, indent=2)}

You are the implementation agent. Modify only the current module/workspace, resolve
all blocker and major findings, add or update executable tests that cover the repaired
requirements, run those tests and focused build checks, and return a structured summary
of changed_files, resolved_findings, remaining_findings, and checks. You must make the
required edits in this turn; an explanation without a changed file is not a repair.
If a finding says a test is a source-string scan, tautological, static, or otherwise
not observable, replace it with a real black-box test that starts/uses the public
application boundary (HTTP/API or browser DOM interactions) and asserts the resulting
behavior. Do not "fix" such a finding by weakening the reviewer or merely adding more
source text assertions. Re-run the relevant test and include its command and result.
"""
            if module:
                self.checkpoint.update_pipeline(module.node_id, node="repair", loop_status="repairing", round_number=repair_round)
            repair_result = self._run_agent(
                repair_role,
                repair_prompt.strip(),
                module=module,
                phase="repair",
                round_number=repair_round,
                workspace_dir=workspace_dir,
                parent_id=resume_message_id,
            )
            self._message(
                "agent.message",
                repair_role,
                module=module,
                phase="repair",
                round_number=repair_round,
                payload={"response": str(getattr(repair_result, "final_response", "") or "")[:20000], "feedback": resume_feedback, "resumed": True},
                parent_id=resume_message_id,
            )
            previous_hash = review_hash(resume_feedback)
            previous_fingerprint = _content_fingerprint(workspace_dir or self.output_dir)
            parent_id = resume_message_id
            first_round = repair_round + 1
        round_number = first_round
        while round_number <= max_rounds:
            self._check_pause(module, "final-review" if final_review else "review")
            latest_test_result: dict[str, Any] | None = None
            tester_enabled = self._tester_enabled(module, workspace_dir)
            project_test_setting = os.environ.get("HAFLEET_PROJECT_TESTS", "1").strip().lower()
            project_tests_enabled = (
                not tester_enabled
                and project_test_setting not in {"0", "false", "no"}
                and has_project_tests(workspace_dir or self.output_dir, module.node_id if module else "ROOT")
            )
            if project_tests_enabled:
                verification_attempt = verification_repairs + 1
                self.checkpoint.update_pipeline(
                    module.node_id if module else "ROOT",
                    current_verification_attempt=verification_attempt,
                    review_round=round_number,
                )
                test_result = self._run_registered_project_tests(
                    module,
                    workspace_dir=workspace_dir,
                    round_number=round_number,
                    parent_id=parent_id,
                )
                latest_test_result = test_result
                if not test_passes(test_result):
                    current_hash = test_hash(test_result)
                    current_fingerprint = _content_fingerprint(workspace_dir or self.output_dir)
                    stalled = (
                        current_hash == previous_test_hash
                        and current_fingerprint == previous_test_fingerprint
                    )
                    verification_stalls = verification_stalls + 1 if stalled else 0
                    if verification_stalls >= stall_limit:
                        self._defer_quality(
                            module,
                            "test",
                            f"project verification made no progress for {verification_stalls} consecutive attempts",
                        )
                        return test_result
                    if verification_repairs >= max_verification_repairs:
                        self._defer_quality(
                            module,
                            "test",
                            f"project verification exceeded {max_verification_repairs} repair attempts",
                        )
                        return test_result
                    repair_prompt = base_prompt + f"""

Deterministic execution of the project-owned verification commands failed in verification
repair attempt {verification_attempt}/{max_verification_repairs}, before Reviewer round
{round_number}/{max_rounds}. These commands and diagnostics come only from tests generated
inside the workspace from the supplied requirements; they are not evaluator tests:
{json.dumps(test_result, ensure_ascii=False, indent=2)}

Repair the implementation or its requirement-derived tests as appropriate. Preserve
strong assertions: do not delete, skip, weaken, or replace a failing behavioral test
with source inspection. Re-run the registered commands, update
.arc/hafleet/verification.json, and report concrete changed_files and check results.
Prioritize the earliest failed prerequisite because dependent workflows may be
invalid until it passes.
{"The preceding repair made no observable progress. Start from a fresh diagnosis: reproduce the earliest failure, inspect the real rendered/API state and its prerequisite flow, and make a substantive implementation or test-fixture correction instead of repeating the prior patch." if stalled else ""}
"""
                    self.checkpoint.update_pipeline(module.node_id if module else "ROOT", node="repair", loop_status="changes_requested", review_round=round_number, current_verification_attempt=verification_attempt, review_findings=test_result.get("findings", []))
                    repair_result = self._run_agent(repair_role, repair_prompt.strip(), module=module, phase="repair", round_number=round_number, workspace_dir=workspace_dir, parent_id=parent_id)
                    self._message("agent.message", repair_role, module=module, phase="repair", round_number=round_number, payload={"response": str(getattr(repair_result, "final_response", "") or "")[:20000], "test_feedback": test_result}, parent_id=parent_id)
                    previous_test_hash = current_hash
                    previous_test_fingerprint = current_fingerprint
                    verification_repairs += 1
                    continue
            if tester_enabled:
                verification_attempt = verification_repairs + 1
                tester_node = self.pipeline.node("final_test") if final_review else self.pipeline.node("tester")
                tester_role = loop.test or (tester_node.role if tester_node else "") or self.pipeline.role_for("tester", "tester")
                test_result = self._run_tester(
                    module,
                    base_prompt,
                    tester_role=tester_role,
                    workspace_dir=workspace_dir,
                    round_number=round_number,
                    mode=(tester_node.mode if tester_node and tester_node.mode else ("integration" if final_review else "module")),
                    parent_id=parent_id,
                )
                latest_test_result = test_result
                if not test_passes(test_result):
                    current_hash = test_hash(test_result)
                    current_fingerprint = _content_fingerprint(workspace_dir or self.output_dir)
                    stalled = (
                        current_hash == previous_test_hash
                        and current_fingerprint == previous_test_fingerprint
                    )
                    verification_stalls = verification_stalls + 1 if stalled else 0
                    if verification_stalls >= stall_limit:
                        self.checkpoint.update_pipeline(
                            module.node_id if module else "ROOT",
                            node="blocked",
                            loop_status="blocked",
                            test_status="failed",
                            last_test_hash=current_hash,
                        )
                        self._defer_quality(module, "test", f"test loop made no progress for {verification_stalls} consecutive attempts")
                        return test_result
                    if verification_repairs >= max_verification_repairs:
                        self.checkpoint.update_pipeline(module.node_id if module else "ROOT", node="blocked", loop_status="blocked", test_status="failed", last_test_hash=current_hash)
                        self._defer_quality(module, "test", f"test loop exceeded {max_verification_repairs} repair attempts")
                        return test_result
                    repair_prompt = base_prompt + f"""

Test failures require repair before review. This is verification repair attempt
{verification_attempt}/{max_verification_repairs}, before Reviewer round
{round_number}/{max_rounds}; test feedback is authoritative:
{json.dumps(test_result, ensure_ascii=False, indent=2)}

You are the implementation agent. Modify only implementation files belonging to the
current module, resolve the failing tests, and leave test files intact unless a test
itself is demonstrably incorrect. You must make a concrete file change when a failure
is actionable and report changed_files; do not return only an explanation.
{"The preceding repair made no observable progress. Reproduce the earliest failing public behavior from a clean state and make a substantive correction instead of repeating the previous patch." if stalled else ""}
"""
                    self.checkpoint.update_pipeline(module.node_id if module else "ROOT", node="repair", loop_status="changes_requested", review_round=round_number, current_verification_attempt=verification_attempt, review_findings=test_result.get("findings", []))
                    repair_result = self._run_agent(repair_role, repair_prompt.strip(), module=module, phase="repair", round_number=round_number, workspace_dir=workspace_dir, parent_id=parent_id)
                    self._message("agent.message", repair_role, module=module, phase="repair", round_number=round_number, payload={"response": str(getattr(repair_result, "final_response", "") or "")[:20000], "test_feedback": test_result}, parent_id=parent_id)
                    previous_test_hash = current_hash
                    previous_test_fingerprint = current_fingerprint
                    verification_repairs += 1
                    parent_id = self.bus.replay(module_id=module.node_id if module else "")[-1]["id"] if self.bus.replay(module_id=module.node_id if module else "") else parent_id
                    continue
            self.checkpoint.update_pipeline(
                module.node_id if module else "ROOT",
                node="review_loop",
                review_round=round_number,
                loop_status="reviewing",
            )
            review_workspace = workspace_dir or self.output_dir
            before_files = _file_snapshot(review_workspace)
            before = _content_fingerprint(review_workspace)
            scenario_contract_note = ""
            contract_obligations = (
                {module.node_id: self.checkpoint.contract_obligations(module.node_id)}
                if module is not None
                else self.checkpoint.all_contract_obligations()
            )
            contract_obligations = {
                key: value for key, value in contract_obligations.items() if value
            }
            if module is not None:
                scenario_contract = review_workspace / ".arc" / "hafleet" / "contracts" / f"{module.node_id}.json"
                scenario_contract_note = f"""
The approved pre-implementation scenario contract is at {scenario_contract}.
Compare it with the final implementation and executable tests. Every stable test_id
must correspond to a real behavioral test with the promised assertions; flag plan-to-
implementation drift and contract rows that were deleted, merged, or satisfied only by
self-authored assumptions.
"""
            obligation_note = ""
            if contract_obligations:
                obligation_note = f"""
These pre-implementation blocker/major obligations remain open. Audit each ID against
the current source and executable black-box tests. Include `resolved_finding_ids` in
your JSON only for IDs whose required behavior and regression evidence now exist.
Repeat every unresolved obligation as a blocker/major finding; a general verdict or
passing self-authored test suite does not resolve it:
```json
{json.dumps(contract_obligations, ensure_ascii=False, indent=2)}
```
"""
            review_prompt = base_prompt + f"""

Review loop round {round_number}/{max_rounds}. You are read-only. Audit the original
requirements, current implementation, executable test cases, and the Implementer's
reported test results together. Do not execute test commands, start servers, install
dependencies, or modify files. Verify every requirement scenario is implemented and
meaningfully covered, inspect tests for weak assertions, false positives, tautologies,
and source-string-only checks, and verify that the reported results are consistent with
the test files. Return ONLY a JSON review object followed by a short summary.
Use verdict=pass only when all blocker/major findings are resolved and required checks pass.
Do not edit project files or Git state.

{scenario_contract_note}
{obligation_note}

The Orchestrator executed this structured test result in the current round immediately
before review. It is authoritative for current pass/fail status:
```json
{json.dumps(latest_test_result or {"verdict": "not_run", "summary": "No project test command was available in this round."}, ensure_ascii=False, indent=2)}
```
Older files under .arc/hafleet/test-results are historical audit artifacts. You may use
them to understand prior failures, but must not report an older round as the latest
result or contradict the current result solely because a stale higher-numbered file
exists from an earlier resumed attempt.
"""
            result = self._run_agent(
                reviewer_role,
                review_prompt.strip(),
                module=module,
                phase="final-review" if final_review else "review",
                round_number=round_number,
                workspace_dir=workspace_dir,
                parent_id=parent_id,
            )
            after = _content_fingerprint(review_workspace)
            if before != after:
                _restore_file_snapshot(review_workspace, before_files)
                self.checkpoint.update_pipeline(module.node_id if module else "ROOT", reviewer_write_violation=True, loop_status="blocked")
                self._message(
                    "pipeline.state",
                    "orchestrator",
                    module=module,
                    phase="review",
                    round_number=round_number,
                    payload={"status": "reviewer_write_violation"},
                )
                self._pause_pipeline(module, "review", "reviewer modified project files")
            response = str(getattr(result, "final_response", "") or "")
            # Legacy test doubles and older adapters have no final_response; an
            # empty response is treated as a passing review for compatibility.
            feedback = parse_review(response) if response.strip() else {
                "verdict": "pass", "summary": "Reviewer completed without structured findings.", "findings": [], "checks": [], "resolved_finding_ids": [], "raw": ""
            }
            if contract_obligations:
                blocking_ids = {
                    str(item.get("id") or "").strip()
                    for item in blocking_findings(feedback)
                    if str(item.get("id") or "").strip()
                }
                resolved_ids = {
                    str(item).strip() for item in feedback.get("resolved_finding_ids", [])
                    if str(item).strip()
                } - blocking_ids
                if resolved_ids:
                    self.checkpoint.resolve_contract_obligations(
                        resolved_ids,
                        module_id=module.node_id if module else None,
                    )
                remaining_map = (
                    {module.node_id: self.checkpoint.contract_obligations(module.node_id)}
                    if module is not None
                    else self.checkpoint.all_contract_obligations()
                )
                remaining = [item for values in remaining_map.values() for item in values]
                existing_ids = {
                    str(item.get("id") or "").strip()
                    for item in feedback.get("findings", [])
                    if isinstance(item, dict)
                }
                for obligation in remaining:
                    obligation_id = str(obligation.get("id") or "").strip()
                    if obligation_id in existing_ids:
                        continue
                    carried = dict(obligation)
                    carried["severity"] = "major"
                    carried["status"] = "open"
                    carried["title"] = f"Unverified contract obligation: {carried.get('title') or obligation_id}"
                    feedback.setdefault("findings", []).append(carried)
                if remaining:
                    feedback["verdict"] = "changes_requested"
                    feedback["summary"] = (
                        f"{len(remaining)} contract obligation(s) still lack explicit resolution evidence."
                    )
            current_hash = review_hash(feedback)
            feedback_message = self._message(
                "review.feedback",
                reviewer_role,
                recipient="orchestrator",
                module=module,
                phase="review",
                round_number=round_number,
                payload=feedback,
                parent_id=parent_id,
            )
            verdict = self._message(
                "review.verdict",
                reviewer_role,
                recipient="orchestrator",
                module=module,
                phase="review",
                round_number=round_number,
                payload={"verdict": feedback["verdict"], "blocking_findings": blocking_findings(feedback), "passed": review_passes(feedback)},
                parent_id=parent_id,
            )
            if review_passes(feedback):
                self.checkpoint.update_pipeline(
                    module.node_id if module else "ROOT",
                    node="checkpoint",
                    loop_status="approved",
                    review_findings=feedback.get("findings", []),
                    carried_contract_findings=[] if module else self.checkpoint.read().get("carried_contract_findings", []),
                    last_feedback_message_id=feedback_message["id"],
                    last_feedback_hash=current_hash,
                )
                self._message("pipeline.state", "orchestrator", module=module, phase="review", round_number=round_number, payload={"status": "approved"}, parent_id=verdict["id"])
                return feedback
            fingerprint = after
            stalled = current_hash == previous_hash and fingerprint == previous_fingerprint
            review_stalls = review_stalls + 1 if stalled else 0
            if review_stalls >= stall_limit:
                self.checkpoint.update_pipeline(module.node_id if module else "ROOT", loop_status="blocked", last_feedback_hash=current_hash, review_findings=feedback.get("findings", []))
                self._defer_quality(module, "review", f"review loop made no progress for {review_stalls} consecutive attempts")
                return feedback
            if round_number >= max_rounds:
                self.checkpoint.update_pipeline(module.node_id if module else "ROOT", loop_status="blocked", last_feedback_hash=current_hash, review_findings=feedback.get("findings", []))
                self._defer_quality(module, "review", f"review loop exceeded {max_rounds} rounds")
                return feedback
            if module:
                self.checkpoint.update_pipeline(
                    module.node_id,
                    node="repair",
                    review_round=round_number,
                    loop_status="changes_requested",
                    review_findings=feedback.get("findings", []),
                    last_feedback_message_id=feedback_message["id"],
                    last_feedback_hash=current_hash,
                )
            repair_prompt = base_prompt + f"""

Repair loop round {round_number}. Reviewer feedback is authoritative:
{json.dumps(feedback, ensure_ascii=False, indent=2)}

You are the implementation agent. Modify only the current module/workspace, resolve
all blocker and major findings, add or update executable regression tests derived from
the original requirements, run those tests and focused build checks, and return a
structured summary of changed_files, resolved_findings, remaining_findings, and checks.
You must make the required edits in this turn; an explanation without a changed file
is not a repair. If any finding identifies source-string-only, tautological, or static
tests, rewrite those tests to exercise the public application over HTTP/API or through
real browser DOM interactions, with behavior assertions that can fail for a broken
implementation. Never satisfy a test-quality finding by adding more source scans or
weakening the assertion. Re-run the focused tests and report the command/result.
{"The preceding repair did not change either the finding set or project fingerprint. Reproduce each unresolved finding from the public requirement, inspect the affected implementation and tests from first principles, and make a substantive correction rather than repeating the previous explanation." if stalled else ""}
"""
            repair_result = self._run_agent(
                repair_role,
                repair_prompt.strip(),
                module=module,
                phase="repair",
                round_number=round_number,
                workspace_dir=workspace_dir,
                parent_id=verdict["id"],
            )
            self._message(
                "agent.message",
                repair_role,
                module=module,
                phase="repair",
                round_number=round_number,
                payload={"response": str(getattr(repair_result, "final_response", "") or "")[:20000], "feedback": feedback},
                parent_id=verdict["id"],
            )
            previous_hash, previous_fingerprint, parent_id = current_hash, fingerprint, verdict["id"]
            round_number += 1
        self._defer_quality(module, "review", "review loop terminated without approval")
        return feedback

    def _commit(self, message: str, role: str) -> bool:
        """Commit with a role identity while keeping older RuntimeGit adapters working."""
        try:
            return self.runtime.git.commit(message, role=role)
        except TypeError as error:
            if "role" not in str(error):
                raise
            return self.runtime.git.commit(message)

    def _commit_operation(
        self,
        message: str,
        role: str,
        *,
        module: RequirementModule | None = None,
        phase: str = "checkpoint",
    ) -> bool:
        operation = self._message(
            "operation.started",
            "orchestrator",
            module=module,
            phase=phase,
            payload={"operation": "commit", "message": message, "role": role},
        )
        try:
            committed = self._commit(message, role)
        except Exception as error:  # noqa: BLE001 - preserve checkpoint failure semantics
            self._message(
                "operation.failed",
                "orchestrator",
                module=module,
                phase=phase,
                payload={"operation": "commit", "message": message, "error": str(error)},
                parent_id=operation["id"],
            )
            raise
        self._message(
            "operation.completed",
            "orchestrator",
            module=module,
            phase=phase,
            payload={"operation": "commit", "message": message, "committed": committed, "role": role},
            parent_id=operation["id"],
        )
        return committed

    def _check_pause(self, module: RequirementModule | None, phase: str | None) -> None:
        if not self.pause_request_path.exists():
            return
        self.checkpoint.mark_paused(module.node_id if module else None, phase)
        raise PauseRequested("ARC-Bench pause requested")

    def _pause_pipeline(self, module: RequirementModule | None, phase: str, reason: str) -> None:
        """Persist a durable paused state before unwinding a bounded loop."""
        self.checkpoint.mark_paused(module.node_id if module else "ROOT", phase)
        raise PauseRequested(reason)

    def _defer_quality(self, module: RequirementModule | None, phase: str, reason: str) -> None:
        """Record bounded quality exhaustion without stopping unattended runs.

        ARC-Bench executions are normally unattended. A review that cannot converge
        within its configured budget should remain visible and auditable, but should
        not terminate the whole delivery. Strict operators can restore the historical
        pause behavior with ``HAFLEET_QUALITY_ON_EXHAUSTION=pause``.
        """

        policy = os.environ.get("HAFLEET_QUALITY_ON_EXHAUSTION", "defer").strip().lower()
        if policy in {"pause", "stop", "fail"}:
            self._pause_pipeline(module, phase, reason)
        module_id = module.node_id if module else "ROOT"
        self.checkpoint.update_pipeline(
            module_id,
            node="checkpoint",
            loop_status="deferred",
            quality_deferred=True,
            quality_exhaustion_reason=reason,
        )
        self._message(
            "pipeline.state",
            "orchestrator",
            module=module,
            phase=phase,
            payload={"status": "quality_deferred", "reason": reason},
        )
        log(f"[hafleet] quality deferred ({module_id}): {reason}; continuing unattended", flush=True)

    def _architecture_prompt(self, requirement_tree: dict[str, object]) -> str:
        return textwrap.dedent(
            f"""
            ARC-Bench task type: {self.task_type}
            Requirement source directory: {self.requirements_dir}
            Output workspace: {self.output_dir}
            Architecture document path: {self.architecture_path}

            Complete ROOT requirement tree:
            ```json
            {json.dumps(requirement_tree, ensure_ascii=False, indent=2)}
            ```

            Write the architecture document exactly to {self.architecture_path}, then
            create or refactor the project skeleton in the workspace. Preserve any
            existing working behavior and do not overwrite user data unnecessarily.
            The document and source tree must define clear frontend/backend module
            boundaries. For web tasks, keep frontend/ and backend/ at the project root,
            provide npm run build and npm run start, read process.env.PORT, and use only
            smoke port {self.smoke_port} for any short verification. Do not implement
            the full requirement tree yet. Do not put browser-loaded frontend modules
            under a top-level frontend/src/api path because the backend reserves
            /api/* for JSON endpoints; use frontend/src/client or frontend/src/services.
            Define a testable state/data contract for each domain entity, including
            validation, error and empty states, authorization boundaries, persistence,
            and refresh semantics. For browser tasks, include a route table with stable
            non-hash paths, direct-navigation/deep-link fallback behavior, and a
            semantic form/API contract (labels, accessible names, keyboard behavior,
            status codes, JSON error envelope). Keep these contracts domain-neutral so
            later modules and external evaluators can exercise behavior through public
            UI/API boundaries without relying on implementation-specific selectors.
            """
        ).strip()

    def _base_prompt(
        self,
        module: RequirementModule,
        completed_ids: list[str],
        plan_path: Path,
        workspace_dir: Path | None = None,
        branch: str | None = None,
    ) -> str:
        completed = ", ".join(completed_ids) if completed_ids else "none"
        scenario_contract_path = plan_path.parent.parent / "contracts" / f"{module.node_id}.json"
        # A compact index of the whole requirement tree prevents scoped module
        # turns from inventing incompatible routes/entities while keeping the
        # prompt bounded (the full subtree remains the authoritative detail).
        global_index: list[str] = []
        def collect_index(node: object) -> None:
            if len(global_index) >= 120 or not isinstance(node, dict):
                return
            node_id = str(node.get("id") or node.get("req_id") or node.get("requirement_id") or "").strip()
            title = str(node.get("name") or node.get("title") or "").strip()
            if node_id:
                global_index.append(f"{node_id}: {title or node_id}")
            for child in (node.get("children") or node.get("requirements") or []):
                collect_index(child)
        collect_index(self.requirement_tree or {})
        global_index_text = "\n".join(global_index) or "(unavailable)"
        root_contracts = build_capability_model(self.requirement_tree or {}).get("seed_contracts", [])
        root_contracts_text = json.dumps(root_contracts, ensure_ascii=False, indent=2)
        workspace_note = ""
        if workspace_dir is not None:
            workspace_note = textwrap.dedent(
                f"""
                Module worktree: {workspace_dir}
                Module branch: {branch or 'unknown'}
                This is an isolated parallel worktree. Modify only this worktree; do not
                modify the main output workspace or depend on other unmerged modules.
                """
            )
        return textwrap.dedent(
            f"""
            ARC-Bench task type: {self.task_type}
            Module: {module.index}/{module.total} - {module.node_id} - {module.name}
            Requirement source directory: {self.requirements_dir}
            Previously completed ROOT modules: {completed}
            Whole-project requirement index (IDs/titles only; do not implement future
            modules by guessing details):
            {global_index_text}
            Author-provided ROOT data/seed contracts (these are part of the public
            requirements, not evaluator fixtures; preserve every prerequisite record
            needed by this module and use the configured runtime date/environment):
            ```json
            {root_contracts_text}
            ```
            Coordinator plan path: {plan_path}
            Scenario contract path: {scenario_contract_path}
            Global architecture document: {self.architecture_path}

            Before changing files, read {self.architecture_path}. Follow its module
            boundaries. Put new behavior in the appropriate modules instead of
            appending business logic to frontend/src/app.js or backend/server.js.
            Preserve existing APIs, persisted data, and visible behavior. If a small
            architecture extension is necessary, update the architecture document.
            {workspace_note}

            Complete requirement subtree:
            ```json
            {json.dumps(module.subtree, ensure_ascii=False, indent=2)}
            ```

            Coordinator capability model (derived only from the requirement tree; do
            not look for or infer hidden benchmark tests):
            ```json
            {json.dumps(build_capability_model(module.subtree), ensure_ascii=False, indent=2)}
            ```

            Treat the capability model as a traceability checklist, not as permission
            to broaden scope. Before finishing, ensure every listed requirement and
            scenario has an observable implementation path and a meaningful test.
            Prefer real state transitions and persisted data over hard-coded fixtures.
            For each flow consider success, validation/error, empty/loading states,
            authorization, navigation, and refresh persistence when applicable. Do not
            access, search for, or mention external/hidden acceptance-test source files;
            the supplied requirement tree is the sole product specification.

            Maintain the generated verification manifest at
            .arc/hafleet/verification.json. It must contain a JSON object with a
            `commands` array. Each command entry has `module_id`, `cwd`, and `command`
            (an argv string array), plus `server_mode`: use `managed` when the command
            expects HAFleet to provide the smoke server, `self` when the test starts
            and stops its own server, and `none` for build/static checks. Commands
            without this field use conservative inference. Each command must execute requirement-derived tests through
            the public application boundary. Register every focused test command you
            actually ran. Never put evaluator paths or hidden tests in this manifest.
            """
        ).strip()

    def run(self, modules: list[RequirementModule]) -> None:
        run_started = time.monotonic()
        state = self.checkpoint.read()
        completed_ids = list(state["completed"])
        completed_set = set(completed_ids)
        deferred_modules = set(str(item) for item in state.get("deferred_modules", []))
        current_module = str(state.get("current_node_id") or "")
        if state.get("quality_deferred") and current_module:
            deferred_modules.add(current_module)

        # Legacy checkpoints had one global quality flag. Starting the next module
        # cleared it, so reconstruct each module's last durable checkpoint verdict
        # from the append-only message log. Later successful checkpoints override an
        # earlier deferred one.
        message_quality: dict[str, bool] = {}
        for message in self.bus.replay():
            if message.get("kind") != "checkpoint.created":
                continue
            module_id = str(message.get("module_id") or "")
            payload = message.get("payload")
            if module_id and isinstance(payload, dict) and "quality_deferred" in payload:
                message_quality[module_id] = bool(payload.get("quality_deferred"))
        deferred_modules.update(module_id for module_id, deferred in message_quality.items() if deferred)
        deferred_modules.difference_update(module_id for module_id, deferred in message_quality.items() if not deferred)

        module_indices = {module.node_id: module.index for module in modules}
        for deferred_module in sorted(deferred_modules & completed_set):
            self.checkpoint.mark_module_deferred(
                deferred_module,
                module_indices.get(deferred_module, int(state.get("last_completed_index", 0) or 0)),
            )
            completed_ids = [item for item in completed_ids if item != deferred_module]
            completed_set.discard(deferred_module)
            log(
                f"[hafleet] Reopening deferred quality module {deferred_module} on resume",
                flush=True,
            )
        state = self.checkpoint.read()
        self.plan_dir.mkdir(parents=True, exist_ok=True)
        log(
            f"[hafleet] orchestrator ready: {len(modules)} module(s), "
            f"already completed={len(completed_ids)}",
            flush=True,
        )

        if self.requirement_tree is None:
            self.requirement_tree = {
                "id": "ROOT",
                "children": [module.subtree for module in modules],
            }
        active_worktrees = state.get("active_worktrees") or {}
        if active_worktrees and not self.parallel:
            raise RuntimeError(
                "checkpoint contains active parallel worktrees; resume with --parallel "
                "to inspect or complete them"
            )
        self.checkpoint.configure_parallel(self.parallel, self.max_workers)
        if not bool(state.get("architecture_completed")):
            self._run_architecture()
            state = self.checkpoint.read()
        else:
            log(
                f"[hafleet] Skipping completed architecture: {self.architecture_path}",
                flush=True,
            )

        if self.parallel:
            self._run_parallel(modules, completed_ids, completed_set)

        for module in ([] if self.parallel else modules):
            if module.node_id in completed_set:
                log(f"[hafleet] Skipping completed module {module.node_id}", flush=True)
                continue
            module_started = time.monotonic()
            log(
                f"[hafleet] Module {module.index}/{module.total} started: "
                f"{module.node_id} - {module.name}",
                flush=True,
            )
            plan_path = self.plan_dir / f"{module.node_id}.md"
            base_prompt = self._base_prompt(module, completed_ids, plan_path)

            # If the checkpoint records a review feedback boundary, continue
            # with the implementer repair turn. This makes a restart after a
            # crash/pause deterministic and avoids repeating planner work.
            checkpoint_state = self.checkpoint.read()
            resume_loop = (
                checkpoint_state.get("current_node_id") == module.node_id
                and checkpoint_state.get("current_pipeline_node") in {"review_loop", "repair", "review"}
                and checkpoint_state.get("loop_status") in {"changes_requested", "repairing", "reviewing"}
            )
            resume_feedback: dict[str, Any] | None = None
            resume_message_id = str(checkpoint_state.get("last_feedback_message_id") or "")
            resume_round = int(checkpoint_state.get("current_round", 0) or 0)
            if resume_loop:
                messages = self.bus.replay(module_id=module.node_id)
                if resume_message_id:
                    match = next((item for item in messages if item.get("id") == resume_message_id and item.get("kind") == "review.feedback"), None)
                else:
                    match = next((item for item in reversed(messages) if item.get("kind") == "review.feedback"), None)
                if match and isinstance(match.get("payload"), dict):
                    resume_feedback = dict(match["payload"])
                else:
                    resume_loop = False

            retry_deferred = module.node_id in deferred_modules
            if retry_deferred:
                # The implementation already exists and downstream work may depend
                # on it. Resume at deterministic verification/review instead of
                # spending another full agent turn re-implementing the requirement.
                self._check_pause(module, "review")
                self.checkpoint.mark_module_started(module.node_id, "review")
                log(f"[hafleet]   retrying deferred quality: {module.node_id}", flush=True)
                carried = self.checkpoint.contract_obligations(module.node_id)
                retry_prompt = base_prompt
                if carried:
                    retry_prompt += f"""

These unresolved pre-implementation contract obligations remain authoritative on
resume. Verify each finding ID against source and executable tests before approval:
```json
{json.dumps(carried, ensure_ascii=False, indent=2)}
```
"""
                self._review_loop(module, retry_prompt)
            elif resume_loop and resume_feedback:
                self._check_pause(module, "repair")
                log(f"[hafleet]   resuming repair round {resume_round}: {module.node_id}", flush=True)
                self._review_loop(
                    module,
                    base_prompt,
                    resume_round=resume_round,
                    resume_feedback=resume_feedback,
                    resume_message_id=resume_message_id,
                )
            else:
                planner_enabled = self._planner_enabled()
                contract_enabled = self._contract_review_enabled(module)
                contract_path = self.contract_dir / f"{module.node_id}.json"
                contract_already_approved = (
                    checkpoint_state.get("current_node_id") == module.node_id
                    and checkpoint_state.get("contract_review_status") == "approved"
                    and plan_path.is_file()
                    and contract_path.is_file()
                )
                contract_resume_ready = (
                    checkpoint_state.get("current_node_id") == module.node_id
                    and checkpoint_state.get("contract_review_status")
                    in {"planned", "reviewing", "changes_requested", "repairing", "deferred"}
                    and plan_path.is_file()
                    and contract_path.is_file()
                )
                contract_feedback: dict[str, Any] = {}
                if planner_enabled and not contract_already_approved:
                    self._check_pause(module, "design")
                    self.checkpoint.mark_module_started(module.node_id, "design")
                    self.runtime.events.mark_design_started(module.node_id, "HAFleet planner started")
                    log(f"[hafleet]   planner started -> {plan_path}", flush=True)
                    try:
                        if contract_enabled:
                            ensure_contract_file(contract_path, module.node_id, module.subtree)
                        self._run_agent(
                            self.pipeline.role_for("planner", "planner"),
                            base_prompt
                            + f"\n\nWrite the implementation plan to exactly: {plan_path}"
                            + (
                                f" and fill the scenario contract at exactly: {contract_path}. "
                                "Do not edit product source files during this planning turn."
                                if contract_enabled
                                else ""
                            ),
                            module=module,
                            phase="design",
                        )
                        if not plan_path.is_file() or not plan_path.read_text(
                            encoding="utf-8", errors="ignore"
                        ).strip():
                            self._run_agent(
                                self.pipeline.role_for("planner", "planner"),
                                base_prompt
                                + f"\n\nThe required plan file was not created. Write a concrete plan now to exactly: {plan_path}",
                                module=module,
                                phase="design",
                            )
                        if not plan_path.is_file() or not plan_path.read_text(
                            encoding="utf-8", errors="ignore"
                        ).strip():
                            raise RuntimeError(f"planner did not create required plan: {plan_path}")
                    except Exception:
                        self.runtime.events.mark_design_failed(module.node_id, "HAFleet planner failed")
                        raise
                    log(f"[hafleet]   planner finished ({plan_path.stat().st_size} bytes)", flush=True)
                elif contract_enabled and not contract_already_approved and not contract_resume_ready:
                    self._check_pause(module, "design")
                    self.checkpoint.mark_module_started(module.node_id, "design")
                    self.runtime.events.mark_design_started(
                        module.node_id, "HAFleet implementer contract planning started"
                    )
                    log(
                        f"[hafleet]   implementer planning started -> {plan_path}; contract={contract_path}",
                        flush=True,
                    )
                    self._run_plan_only_agent(module, base_prompt, plan_path, contract_path)
                elif contract_enabled and contract_resume_ready:
                    log(
                        f"[hafleet]   resuming contract review from existing plan: {module.node_id}",
                        flush=True,
                    )

                if contract_enabled and not contract_already_approved:
                    log(f"[hafleet]   contract review started: {module.node_id}", flush=True)
                    contract_feedback = self._contract_review_loop(
                        module,
                        base_prompt,
                        plan_path,
                        contract_path,
                    )
                    self.runtime.events.mark_design_done(
                        module.node_id, "HAFleet implementation contract reviewed"
                    )
                elif planner_enabled:
                    self.runtime.events.mark_design_done(module.node_id, "HAFleet plan completed")

                self._check_pause(module, "implement")
                self.checkpoint.mark_module_started(module.node_id, "implement")
                if not planner_enabled and not contract_enabled:
                    self.runtime.events.mark_design_started(
                        module.node_id, "HAFleet implementer planning started"
                    )
                self.runtime.events.mark_implementation_started(
                    module.node_id, "HAFleet implementer started"
                )
                log(
                    f"[hafleet]   implementer started"
                    f"{' (reviewed contract + implementation)' if contract_enabled else ' (planning + implementation)' if not planner_enabled else ''}: "
                    f"{module.node_id}",
                    flush=True,
                )
                try:
                    if contract_enabled:
                        implementer_prompt = base_prompt + f"""

The planning-only phase and pre-implementation contract review are complete. Read
the plan at {plan_path} and the scenario contract at {contract_path}. Now implement
the complete subtree literally from those artifacts, create executable tests using
each scenario's stable test_id and concrete assertions, and run the focused tests and
build checks. Keep the scenario contract synchronized if an implementation detail must
change, but do not remove or merge original scenario rows. The same Implementer role
owns this plan and implementation, so preserve the decisions already made.
If the contract gate was deferred, every item in carried_findings is a mandatory
implementation obligation. Resolve it in source and executable tests, and report
evidence keyed by the original finding ID; do not merely acknowledge the feedback.

Latest contract-review result (approved unless the finite unattended gate was deferred):
```json
{json.dumps(contract_feedback or {"verdict": "pass", "summary": "Previously approved contract restored from checkpoint."}, ensure_ascii=False, indent=2)}
```
"""
                    else:
                        implementer_prompt = base_prompt + (
                            f"\n\nYou own planning and implementation, as well as test authoring, for this module. First write a concrete implementation plan to exactly: {plan_path}. Then implement the complete requirement subtree, create or update executable tests derived directly from its requirement IDs and scenarios, and run those tests plus focused build checks in the same turn. For web behavior use Playwright when appropriate. Do not delegate planning or testing to another role."
                            if not planner_enabled
                            else "\n\nRead the coordinator plan, then implement the complete subtree, create or update requirement-derived executable tests, and run those tests plus focused build checks now."
                        )
                    self._run_module_implementation(
                        module,
                        implementer_prompt,
                    )
                    if not planner_enabled and not contract_enabled:
                        self._ensure_plan_artifact(plan_path, module)
                        self.runtime.events.mark_design_done(
                            module.node_id, "HAFleet implementer plan completed"
                        )
                    if self._self_check_enabled(module):
                        log(f"[hafleet]   implementer self-check: {module.node_id}", flush=True)
                        self._run_implementer_self_check(module, base_prompt, plan_path=plan_path)
                    if self._completion_pass_enabled(module):
                        log(f"[hafleet]   implementer completion pass: {module.node_id}", flush=True)
                        self._run_implementer_completion_pass(module, base_prompt, plan_path=plan_path)
                    self._check_pause(module, "review")
                    self.checkpoint.mark_module_started(module.node_id, "review")
                    log(f"[hafleet]   reviewer started: {module.node_id}", flush=True)
                    carried = self.checkpoint.contract_obligations(module.node_id)
                    review_base_prompt = base_prompt
                    if carried:
                        review_base_prompt += f"""

The pre-implementation contract gate carried these unresolved obligations into
implementation. Verify each finding ID against the final source and executable tests.
Do not approve the module while any blocker/major obligation remains unresolved:
```json
{json.dumps(carried, ensure_ascii=False, indent=2)}
```
"""
                    self._review_loop(module, review_base_prompt)
                except PauseRequested:
                    raise
                except Exception:
                    self.runtime.events.mark_implementation_failed(module.node_id, "HAFleet worker failed")
                    raise

            self.runtime.events.mark_implementation_done(module.node_id, "Implementation reviewed and repaired")
            checkpoint_message = f"{module.node_id}: implement and review {module.name}"
            quality_deferred = bool(self.checkpoint.read().get("quality_deferred"))
            committed = self._commit_operation(checkpoint_message, "reviewer", module=module)
            self._message(
                "checkpoint.created",
                "orchestrator",
                module=module,
                phase="checkpoint",
                payload={"message": checkpoint_message, "committed": committed, "quality_deferred": quality_deferred},
            )
            log(
                f"[hafleet]   checkpoint {'created' if committed else 'skipped'}: {checkpoint_message}",
                flush=True,
            )
            if quality_deferred:
                log(
                    f"[hafleet]   warning: {module.node_id} checkpoint carries deferred quality review",
                    flush=True,
                )
            if quality_deferred:
                self.checkpoint.mark_module_deferred(module.node_id, module.index)
            else:
                self.checkpoint.mark_module_completed(module.node_id, module.index)
            self.checkpoint.update_pipeline(module.node_id, message_cursor=self.bus.last_sequence)
            completed_ids.append(module.node_id)
            completed_set.add(module.node_id)
            log(
                f"[hafleet] Completed {module.index}/{module.total}: {module.node_id} "
                f"({time.monotonic() - module_started:.1f}s)",
                flush=True,
            )

        final_review_enabled = os.environ.get("HAFLEET_FINAL_REVIEW", "1").strip().lower() not in {
            "0",
            "false",
            "no",
        }
        if modules and not bool(self.checkpoint.read().get("final_review_completed")):
            self._check_pause(None, "final-review")
            module_ids = ", ".join(item.node_id for item in modules)
            # Give the implementation agent one explicit, whole-project integration
            # pass before the read-only final audit.  Module turns are intentionally
            # scoped, but cross-module navigation/authentication/persistence bugs are
            # only observable once all modules have landed.  This pass is driven by
            # the supplied requirements (never evaluator tests) and is opt-out for
            # legacy/expensive runs.
            # Enable by default for substantive requirement trees. Tiny adapter
            # fixtures (and legacy smoke runs) skip the extra turn automatically;
            # HAFLEET_FINAL_INTEGRATION=0 remains an explicit opt-out.
            leaf_count = 0
            def count_leaves(node: object) -> None:
                nonlocal leaf_count
                if not isinstance(node, dict):
                    return
                children = node.get("children") or node.get("requirements") or []
                if isinstance(children, list) and children:
                    for child in children:
                        count_leaves(child)
                else:
                    leaf_count += 1
            count_leaves(self.requirement_tree or {})
            integration_enabled = os.environ.get("HAFLEET_FINAL_INTEGRATION", "1").strip().lower() not in {
                "0", "false", "no",
            } and final_review_enabled and leaf_count >= 4
            outstanding_contracts = self.checkpoint.all_contract_obligations()
            deferred_feedback = self._deferred_quality_feedback()
            if integration_enabled and modules:
                log("[hafleet] Final integration implementation pass started", flush=True)
                integration_prompt = textwrap.dedent(
                    f"""
                    Perform a whole-project integration pass for ARC-Bench task type {self.task_type}.
                    All feature modules are now implemented: {module_ids}.
                    Read the complete original requirement tree from {self.requirements_dir},
                    the architecture document {self.architecture_path}, and the current
                    workspace. Do not access, search for, or infer hidden/evaluator tests.

                    Audit and improve the running product across module boundaries. Exercise
                    public behavior with focused smoke checks where useful, but do not start a
                    long-lived server. Prioritize concrete requirement-derived gaps in:
                    - navigation and direct URL/deep-link refresh between every user flow;
                    - authentication/session/logout transitions and authorization guards;
                    - API/UI state parity, validation/conflict/error/empty states;
                    - deterministic app-owned seed/bootstrap records described by requirements;
                    - durable persistence across refresh and process restart;
                    - end-to-end handoffs such as search -> booking -> order -> payment and
                    profile/passenger updates, when those concepts exist in the requirements.

                    The following unresolved pre-implementation obligations are mandatory.
                    Resolve them in source and executable black-box tests, preserving their IDs
                    in your structured report. A passing existing test suite is not evidence by
                    itself; reproduce the required public behavior and add a regression assertion:
                    ```json
                    {json.dumps(outstanding_contracts, ensure_ascii=False, indent=2)}
                    ```

                    The following module-level quality findings exhausted their bounded
                    review loops. They are not approved or optional. Reproduce and resolve
                    each finding in the final integrated source and black-box regression
                    tests, retaining its module and finding ID in your report:
                    ```json
                    {json.dumps(deferred_feedback, ensure_ascii=False, indent=2)}
                    ```

                    Independently exercise high-fan-out prerequisites before downstream flows:
                    - activate every menu/dropdown entry through the rendered UI and assert its
                      history URL, direct-load behavior, refresh behavior, and protected redirect;
                    - accept ordinary valid text input whenever the requirement says users type a
                      value; do not make suggestion selection an undocumented prerequisite;
                    - provision each stateful test's user/record/order data from a clean isolated
                      store and prove the same suite passes twice and in a different order;
                    - verify downstream workflows from their public prerequisite setup rather than
                      relying on state left by another test.

                    Preserve all already-implemented behavior and module boundaries. Make real
                    source changes (not static text), update or add requirement-derived tests,
                    and run the available build/focused checks. Do not tailor selectors or
                    behavior to undocumented tests. Return a concise structured summary of
                    changed_files, requirement_ids, checks, and unresolved risks.
                    """
                ).strip()
                integration_module = RequirementModule(
                    index=len(modules),
                    total=len(modules),
                    node_id="ROOT",
                    name="Final Integration",
                    subtree=self.requirement_tree or {"id": "ROOT"},
                )
                self._run_implementation_with_continuations(
                    integration_module,
                    integration_prompt,
                    phase="integration",
                )
            final_review_passed = True
            if final_review_enabled:
                log("[hafleet] Final integration review started", flush=True)
                final_feedback = self._review_loop(
                    None,
                    textwrap.dedent(
                        f"""
                        Perform the final integration review for ARC-Bench task type {self.task_type}.
                        Completed ROOT modules: {module_ids}.
                        Read the original requirements from {self.requirements_dir} and read
                        {self.architecture_path}. Audit the requirements, implementation, and all
                        executable tests together while preserving module boundaries. Report missing
                        coverage, weak or misleading tests, regressions, and integration gaps. Do not
                        execute test commands, start servers, install dependencies, or modify project
                        files. Explicitly resolve carried contract finding IDs only when current source
                        and black-box regression tests prove the promised behavior. Treat unresolved
                        module obligations, broken UI entry points, undocumented input prerequisites,
                        shared mutable test fixtures, and order-dependent tests as major findings.
                        Re-check every deferred module finding below and list its finding ID in
                        resolved_finding_ids only when source and executable tests prove it fixed:
                        ```json
                        {json.dumps(deferred_feedback, ensure_ascii=False, indent=2)}
                        ```
                        """
                    ).strip(),
                    final_review=True,
                )
                final_review_passed = review_passes(final_feedback)
            log(
                f"[hafleet] Final review {'enabled' if final_review_enabled else 'disabled'}; "
                "running delivery postflight",
                flush=True,
            )
            delivery_approved = self._run_postflight(module_ids)
            if not delivery_approved:
                log(
                    "[hafleet] Final checkpoint withheld: project-owned verification remains failing; "
                    "the output is preserved for unattended evaluation and a later resume",
                    flush=True,
                )
                return
            remaining_contracts = self.checkpoint.all_contract_obligations()
            if not final_review_passed or remaining_contracts:
                self.checkpoint.update_pipeline(
                    "ROOT",
                    node="final_quality_gate",
                    loop_status="deferred",
                    quality_deferred=True,
                    quality_exhaustion_reason=(
                        "unresolved contract obligations"
                        if remaining_contracts
                        else "final reviewer did not approve"
                    ),
                )
                self._message(
                    "pipeline.state",
                    "orchestrator",
                    phase="final-review",
                    payload={
                        "status": "final_quality_deferred",
                        "reason": "unresolved contract obligations" if remaining_contracts else "final review not approved",
                        "contract_obligations": remaining_contracts,
                    },
                )
                log(
                    "[hafleet] Final checkpoint withheld: final Reviewer approval and explicit "
                    "contract-obligation resolution are required; output remains available for evaluation/resume",
                    flush=True,
                )
                return
            self.checkpoint.resolve_all_deferred_modules()
            final_checkpoint = "ROOT: final HAFleet integration review"
            committed = self._commit_operation(final_checkpoint, "postflight", phase="checkpoint")
            self._message(
                "checkpoint.created",
                "postflight",
                phase="checkpoint",
                payload={"message": final_checkpoint, "committed": committed},
            )
            log(
                f"[hafleet] Final checkpoint {'created' if committed else 'skipped'}: {final_checkpoint}",
                flush=True,
            )
            self.checkpoint.mark_final_review_completed()
            log(
                f"[hafleet] Final integration completed ({time.monotonic() - run_started:.1f}s)",
                flush=True,
            )

    def _run_parallel_module(
        self,
        module: RequirementModule,
        workspace: Path,
        branch: str,
        completed_ids: list[str],
    ) -> Path:
        plan_path = workspace / ".arc" / "hafleet" / "plans" / f"{module.node_id}.md"
        contract_path = workspace / ".arc" / "hafleet" / "contracts" / f"{module.node_id}.json"
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        base_prompt = self._base_prompt(module, completed_ids, plan_path, workspace, branch)
        checkpoint_state = self.checkpoint.read()
        resume_loop = (
            checkpoint_state.get("current_node_id") == module.node_id
            and checkpoint_state.get("current_pipeline_node") in {"review_loop", "repair", "review"}
            and checkpoint_state.get("loop_status") in {"changes_requested", "repairing", "reviewing"}
        )
        if resume_loop:
            feedback_id = str(checkpoint_state.get("last_feedback_message_id") or "")
            messages = self.bus.replay(module_id=module.node_id)
            match = next((item for item in messages if item.get("id") == feedback_id and item.get("kind") == "review.feedback"), None)
            if match and isinstance(match.get("payload"), dict):
                self._review_loop(
                    module,
                    base_prompt,
                    workspace_dir=workspace,
                    resume_round=int(checkpoint_state.get("current_round", 0) or 0),
                    resume_feedback=dict(match["payload"]),
                    resume_message_id=feedback_id,
                )
                return plan_path
        planner_enabled = self._planner_enabled()
        contract_enabled = self._contract_review_enabled(module)
        contract_feedback: dict[str, Any] = {}
        if planner_enabled:
            if contract_enabled:
                ensure_contract_file(contract_path, module.node_id, module.subtree)
            self._run_agent(
                self.pipeline.role_for("planner", "planner"),
                base_prompt
                + f"\n\nWrite the implementation plan to exactly: {plan_path}"
                + (
                    f" and fill the scenario contract at exactly: {contract_path}. "
                    "Do not edit product source files during this planning turn."
                    if contract_enabled
                    else ""
                ),
                module=module,
                phase="design",
                workspace_dir=workspace,
            )
            if not plan_path.is_file() or not plan_path.read_text(encoding="utf-8", errors="ignore").strip():
                self._run_agent(
                    self.pipeline.role_for("planner", "planner"),
                    base_prompt
                    + f"\n\nThe required plan file was not created. Write a concrete plan now to exactly: {plan_path}",
                    module=module,
                    phase="design",
                    workspace_dir=workspace,
                )
            if not plan_path.is_file() or not plan_path.read_text(encoding="utf-8", errors="ignore").strip():
                raise RuntimeError(f"planner did not create required plan: {plan_path}")
        elif contract_enabled:
            self._run_plan_only_agent(
                module,
                base_prompt,
                plan_path,
                contract_path,
                workspace_dir=workspace,
            )
        if contract_enabled:
            contract_feedback = self._contract_review_loop(
                module,
                base_prompt,
                plan_path,
                contract_path,
                workspace_dir=workspace,
            )
        if contract_enabled:
            implementer_prompt = base_prompt + f"""

The planning-only phase and pre-implementation contract review are complete. Read
the plan at {plan_path} and scenario contract at {contract_path}, then implement the
complete subtree literally. Create and run executable tests using every scenario's
stable test_id and assertions. Keep all original scenario rows.
If carried_findings are present below, resolve every item in implementation and tests
and report evidence keyed by its original finding ID.

Latest contract-review result:
```json
{json.dumps(contract_feedback, ensure_ascii=False, indent=2)}
```
"""
        else:
            implementer_prompt = base_prompt + (
                f"\n\nYou own planning and implementation, as well as test authoring, for this module. First write a concrete implementation plan to exactly: {plan_path}. Then implement the complete requirement subtree, create or update executable tests derived directly from its requirement IDs and scenarios, and run those tests plus focused build checks in the same turn. For web behavior use Playwright when appropriate. Do not delegate planning or testing to another role."
                if not planner_enabled
                else "\n\nRead the coordinator plan, then implement the complete subtree, create or update requirement-derived executable tests, and run those tests plus focused build checks now."
            )
        self._run_module_implementation(
            module,
            textwrap.dedent(implementer_prompt).strip(),
            workspace_dir=workspace,
        )
        if not planner_enabled and not contract_enabled:
            self._ensure_plan_artifact(plan_path, module)
        if self._self_check_enabled(module):
            self._run_implementer_self_check(
                module,
                base_prompt,
                workspace_dir=workspace,
                plan_path=plan_path,
            )
        if self._completion_pass_enabled(module):
            self._run_implementer_completion_pass(
                module,
                base_prompt,
                workspace_dir=workspace,
                plan_path=plan_path,
            )
        carried = self.checkpoint.contract_obligations(module.node_id)
        review_base_prompt = base_prompt
        if carried:
            review_base_prompt += f"""

The pre-implementation contract gate carried these unresolved obligations into
implementation. Verify each finding ID against the final source and executable tests.
Do not approve while any blocker/major obligation remains unresolved:
```json
{json.dumps(carried, ensure_ascii=False, indent=2)}
```
"""
        self._review_loop(module, review_base_prompt, workspace_dir=workspace)
        return plan_path

    def _run_parallel(
        self,
        modules: list[RequirementModule],
        completed_ids: list[str],
        completed_set: set[str],
    ) -> None:
        manager = WorktreeManager(self.output_dir)
        pending = [module for module in modules if module.node_id not in completed_set]
        direct_ids = {module.node_id for module in modules}
        log(
            f"[hafleet] parallel mode enabled: max_workers={self.max_workers}, "
            f"pending={len(pending)}",
            flush=True,
        )

        while pending:
            ready = [
                module
                for module in pending
                if all(
                    dependency not in direct_ids or dependency in completed_set
                    for dependency in module.dependencies
                    if dependency != module.node_id
                )
            ]
            if not ready:
                self.checkpoint.mark_paused("ROOT", "parallel")
                raise PauseRequested("parallel dependency graph has no ready module")
            batch = ready[: self.max_workers]
            base_commit = manager.current_head()
            workspaces: dict[str, tuple[Path, str, str]] = {}
            for module in batch:
                existing = (self.checkpoint.read().get("active_worktrees") or {}).get(module.node_id)
                existing_path = Path(existing["path"]) if existing and existing.get("path") else None
                if existing_path is not None and not existing_path.exists():
                    raise RuntimeError(
                        f"recorded worktree is missing for {module.node_id}: {existing_path}"
                    )
                module_base = str(existing.get("base_commit") or base_commit) if existing else base_commit
                workspace, branch = manager.create_or_reuse(module.node_id, module_base, existing_path)
                workspace_architecture = workspace / ".arc" / "hafleet" / "architecture.md"
                workspace_architecture.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(self.architecture_path, workspace_architecture)
                workspaces[module.node_id] = (workspace, branch, module_base)
                self.checkpoint.set_active_worktree(
                    module.node_id,
                    {
                        "path": str(workspace),
                        "branch": branch,
                        "base_commit": module_base,
                        "phase": "design"
                        if self._planner_enabled() or self._contract_review_enabled(module)
                        else "implement",
                    },
                )
                self.runtime.events.mark_design_started(
                    module.node_id,
                    "HAFleet parallel planner started"
                    if self._planner_enabled()
                    else "HAFleet parallel implementer contract planning started"
                    if self._contract_review_enabled(module)
                    else "HAFleet parallel implementer planning started",
                )
                self.runtime.events.mark_implementation_started(
                    module.node_id, "HAFleet parallel implementer started"
                )
                log(
                    f"[hafleet] dispatching {module.node_id} to {workspace} ({branch})",
                    flush=True,
                )
            failures: dict[str, BaseException] = {}
            module_by_id = {module.node_id: module for module in batch}
            with ThreadPoolExecutor(max_workers=len(batch), thread_name_prefix="hafleet-module") as pool:
                future_map = {
                    pool.submit(
                        self._run_parallel_module,
                        module_by_id[module_id],
                        workspace,
                        branch,
                        list(completed_ids),
                    ): module_by_id[module_id]
                    for module_id, (workspace, branch, _base) in workspaces.items()
                }
                for future in as_completed(future_map):
                    module = future_map[future]
                    try:
                        future.result()
                        self.checkpoint.update_active_worktree(module.node_id, phase="merge")
                    except BaseException as exc:  # noqa: BLE001 - preserve worker failure for checkpointing
                        failures[module.node_id] = exc

            blocked = False
            for module in modules:
                if module not in batch:
                    continue
                workspace, branch, module_base = workspaces[module.node_id]
                if module.node_id in failures:
                    blocked = True
                    self.runtime.events.mark_design_failed(module.node_id, "HAFleet parallel worker failed")
                    self.runtime.events.mark_implementation_failed(
                        module.node_id, "HAFleet parallel worker failed"
                    )
                    self.checkpoint.update_active_worktree(module.node_id, phase="failed")
                    self.checkpoint.mark_parallel_failure(module.node_id)
                    log(f"[hafleet] parallel module failed {module.node_id}: {failures[module.node_id]}", flush=True)
                    continue
                try:
                    checkpoint_message = f"{module.node_id}: implement and review {module.name}"
                    operation = self._message(
                        "operation.started",
                        "orchestrator",
                        module=module,
                        phase="checkpoint",
                        payload={"operation": "commit", "message": checkpoint_message, "role": "reviewer", "parallel": True},
                    )
                    try:
                        commit = manager.ensure_commit(workspace, checkpoint_message, role="reviewer")
                    except Exception as error:  # noqa: BLE001 - retain worktree for diagnosis
                        self._message(
                            "operation.failed",
                            "orchestrator",
                            module=module,
                            phase="checkpoint",
                            payload={"operation": "commit", "message": checkpoint_message, "error": str(error), "parallel": True},
                            parent_id=operation["id"],
                        )
                        raise
                    self._message(
                        "operation.completed",
                        "orchestrator",
                        module=module,
                        phase="checkpoint",
                        payload={"operation": "commit", "message": checkpoint_message, "committed": bool(commit), "parallel": True},
                        parent_id=operation["id"],
                    )
                    self._message(
                        "checkpoint.created",
                        "orchestrator",
                        module=module,
                        phase="checkpoint",
                        payload={"message": checkpoint_message, "commit": commit, "parallel": True},
                    )
                    commits = manager.commits_since(workspace, module_base)
                    if not commits:
                        raise RuntimeError(f"parallel module produced no commit: {commit}")
                    source_plan = workspace / ".arc" / "hafleet" / "plans" / f"{module.node_id}.md"
                    destination_plan = self.plan_dir / f"{module.node_id}.md"
                    destination_plan.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source_plan, destination_plan)
                    source_contract = workspace / ".arc" / "hafleet" / "contracts" / f"{module.node_id}.json"
                    if source_contract.is_file():
                        destination_contract = self.contract_dir / f"{module.node_id}.json"
                        destination_contract.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(source_contract, destination_contract)
                    manager.cherry_pick(commits)
                    self.runtime.events.mark_design_done(module.node_id, "HAFleet plan completed")
                    self.runtime.events.mark_implementation_done(
                        module.node_id, "Implementation reviewed and repaired"
                    )
                    try:
                        manager.remove_successful(workspace, branch)
                    except Exception as cleanup_error:  # noqa: BLE001 - merged code is still valid
                        log(
                            f"[hafleet] merged {module.node_id} but could not clean worktree: {cleanup_error}",
                            flush=True,
                        )
                    self.checkpoint.clear_active_worktree(module.node_id)
                    self.checkpoint.mark_module_completed(module.node_id, module.index)
                    completed_set.add(module.node_id)
                    completed_ids.append(module.node_id)
                    log(f"[hafleet] parallel module merged: {module.node_id}", flush=True)
                except WorktreeConflict as exc:
                    blocked = True
                    self.runtime.events.mark_implementation_failed(
                        module.node_id, "HAFleet parallel cherry-pick conflict"
                    )
                    self.checkpoint.update_active_worktree(module.node_id, phase="conflict")
                    self.checkpoint.mark_parallel_conflict(module.node_id)
                    log(f"[hafleet] cherry-pick conflict for {module.node_id}: {exc}", flush=True)
                except BaseException as exc:  # noqa: BLE001 - retain worktree for diagnosis
                    blocked = True
                    self.runtime.events.mark_implementation_failed(
                        module.node_id, "HAFleet parallel merge failed"
                    )
                    self.checkpoint.update_active_worktree(module.node_id, phase="merge-failed")
                    self.checkpoint.mark_parallel_failure(module.node_id)
                    log(f"[hafleet] parallel merge failed for {module.node_id}: {exc}", flush=True)

            pending = [module for module in pending if module.node_id not in completed_set]
            if blocked:
                self.checkpoint.mark_paused("ROOT", "parallel")
                raise PauseRequested("parallel module failure or merge conflict")
            if self.pause_request_path.exists() and pending:
                self.checkpoint.mark_paused("ROOT", "parallel")
                raise PauseRequested("ARC-Bench pause requested")

    def _run_architecture(self) -> None:
        """Run the one-time global architecture and scaffold phase."""

        if self.pause_request_path.exists():
            self.checkpoint.mark_paused("ROOT", "architecture")
            raise PauseRequested("ARC-Bench pause requested")

        self.checkpoint.mark_module_started("ROOT", "architecture")
        log(
            f"[hafleet] architecture started -> {self.architecture_path}",
            flush=True,
        )
        requirement_tree = self.requirement_tree
        if requirement_tree is None:
            raise RuntimeError("architecture requirement tree is unavailable")
        try:
            self._run_agent(self.pipeline.role_for("architect", "architect"), self._architecture_prompt(requirement_tree), phase="architecture")
            if self.pause_request_path.exists():
                self.checkpoint.mark_paused("ROOT", "architecture")
                raise PauseRequested("ARC-Bench pause requested")
            if not self.architecture_path.is_file() or not self.architecture_path.read_text(
                encoding="utf-8", errors="ignore"
            ).strip():
                raise RuntimeError(
                    f"architect did not create required architecture document: {self.architecture_path}"
                )
            if self.task_type == "web":
                violations = validate_web_structure(self.output_dir)
                if violations:
                    raise RuntimeError(
                        "architect scaffold failed web delivery contract:\n- "
                        + "\n- ".join(violations)
                    )
            checkpoint_message = "ROOT: architecture scaffold"
            committed = self._commit_operation(checkpoint_message, "architect", phase="architecture")
            self.checkpoint.mark_architecture_completed()
            log(
                f"[hafleet] architecture document created ({self.architecture_path.stat().st_size} bytes)",
                flush=True,
            )
            log(
                f"[hafleet] architecture scaffold checkpoint "
                f"{'created' if committed else 'skipped'}: {checkpoint_message}",
                flush=True,
            )
        except PauseRequested:
            raise
        except Exception:
            log("[hafleet] architecture failed; feature modules will not start", flush=True)
            raise

    def _run_postflight(self, module_ids: str) -> bool:
        enabled = os.environ.get("HAFLEET_POSTFLIGHT", "1").strip().lower() not in {
            "0",
            "false",
            "no",
        }
        if not enabled or self.task_type != "web":
            reason = "disabled by HAFLEET_POSTFLIGHT" if not enabled else f"task type is {self.task_type}"
            log(f"[hafleet] Web postflight skipped ({reason})", flush=True)
            return True
        try:
            repair_attempts = max(int(os.environ.get("HAFLEET_POSTFLIGHT_REPAIRS", "2")), 0)
        except ValueError:
            repair_attempts = 2
        final_verification = os.environ.get("HAFLEET_FINAL_VERIFICATION", "1").strip().lower() not in {
            "0",
            "false",
            "no",
        }

        for attempt in range(repair_attempts + 1):
            self._check_pause(None, "postflight")
            operation = self._message(
                "operation.started",
                "orchestrator",
                phase="postflight",
                payload={"operation": "postflight", "attempt": attempt + 1},
            )
            log(
                f"[hafleet] Web postflight attempt {attempt + 1}/{repair_attempts + 1} "
                f"on smoke port {self.smoke_port}",
                flush=True,
            )
            verification_failed = False
            try:
                rehearse_web_app(self.output_dir, self.smoke_port)
                verification_result: dict[str, Any] | None = None
                if final_verification and has_project_tests(self.output_dir, "ROOT"):
                    log("[hafleet] Running final registered project verification gate", flush=True)
                    verification_result = self._run_registered_project_tests(
                        None,
                        workspace_dir=self.output_dir,
                        round_number=100 + attempt + 1,
                        parent_id=operation["id"],
                    )
                    if not test_passes(verification_result):
                        verification_failed = True
                        summary = json.dumps(verification_result, ensure_ascii=False, indent=2)
                        raise PostflightError(
                            "Final registered project verification failed:\n"
                            + summary[-20000:]
                        )
                log(
                    f"[hafleet] Web postflight passed on smoke port {self.smoke_port}",
                    flush=True,
                )
                self.checkpoint.update_pipeline(
                    "ROOT",
                    node="checkpoint",
                    loop_status="approved",
                    test_status="passed" if verification_result is not None else "",
                    quality_deferred=False,
                    quality_exhaustion_reason="",
                )
                self._message(
                    "operation.completed",
                    "orchestrator",
                    phase="postflight",
                    payload={"operation": "postflight", "attempt": attempt + 1},
                    parent_id=operation["id"],
                )
                return True
            except PostflightError as exc:
                self._message(
                    "operation.failed",
                    "orchestrator",
                    phase="postflight",
                    payload={"operation": "postflight", "attempt": attempt + 1, "error": str(exc)},
                    parent_id=operation["id"],
                )
                if attempt >= repair_attempts:
                    if not verification_failed:
                        raise
                    self._defer_quality(
                        None,
                        "postflight",
                        f"final registered project verification exceeded {repair_attempts} repair attempts",
                    )
                    return False
                log(
                    f"[hafleet] Web postflight failed; repair {attempt + 1}/{repair_attempts}: {exc}",
                    flush=True,
                )
                self._run_agent(
                    self.pipeline.role_for("implementer", "implementer"),
                    textwrap.dedent(
                        f"""
                        The ARC-Bench web delivery postflight failed after modules {module_ids}.
                        Repair the project now as the implementation agent, then verify it on smoke port {self.smoke_port}.
                        The grader requires frontend/package.json with `npm run build` and
                        backend/package.json with `npm run start`; the backend must read PORT and
                        serve the built frontend. Do not bind the grading port. Stop all servers.

                        Exact postflight error:
                        {exc}

                        If the delivery structure and health contract already pass, focus on the
                        earliest failing registered project test. Reproduce it from isolated state,
                        repair the underlying public behavior and its cross-module prerequisites,
                        keep strong requirement-derived assertions, and rerun the failing command
                        before the full registered verification set. Do not delete, skip, or weaken
                        a failing behavioral test merely to pass this gate.
                        """
                    ).strip(),
                    phase="recovery",
                )
        return False
