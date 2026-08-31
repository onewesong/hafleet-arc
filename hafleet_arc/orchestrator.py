from __future__ import annotations

import hashlib
import json
import os
import shutil
import textwrap
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Protocol

from .checkpoint import CheckpointStore
from .capabilities import build_capability_model
from .feedback import (
    blocking_findings,
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
from .test_runner import persist_test_result, run_project_tests
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
            # Keep the initial implementation context warm, but isolate corrective
            # turns from stale model conclusions. The complete requirement subtree,
            # plan, and structured feedback are supplied again, while workspace files
            # and MessageBus history remain durable across the fresh conversation.
            if phase in {"review", "final-review", "repair", "completion", "self-check"}:
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
checks and return JSON with changed_files, covered requirement IDs, checks, and any
remaining risks; an empty changed_files result is acceptable only when you can cite
evidence for every leaf being implemented.
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
not search for or infer hidden/evaluator tests. Keep changes inside this module and
return JSON with changed_files, covered_requirements, checks, and remaining_risks.
Do not merely describe gaps: fix concrete gaps before returning.
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
            "Planning, implementation, and test authoring were performed by the "
            "Implementer role in a single turn. The requirement context used for "
            "that turn follows:\n\n"
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
        previous_hash = ""
        previous_fingerprint: tuple[tuple[str, str], ...] | None = None
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
        for round_number in range(first_round, max_rounds + 1):
            self._check_pause(module, "final-review" if final_review else "review")
            if self._tester_enabled(module, workspace_dir):
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
                if not test_passes(test_result):
                    current_hash = test_hash(test_result)
                    current_fingerprint = _content_fingerprint(workspace_dir or self.output_dir)
                    if current_hash == previous_hash and current_fingerprint == previous_fingerprint:
                        self.checkpoint.update_pipeline(
                            module.node_id if module else "ROOT",
                            node="blocked",
                            loop_status="blocked",
                            test_status="failed",
                            last_test_hash=current_hash,
                        )
                        self._defer_quality(module, "test", "test loop made no progress")
                        return test_result
                    if round_number >= max_rounds:
                        self.checkpoint.update_pipeline(module.node_id if module else "ROOT", node="blocked", loop_status="blocked", test_status="failed", last_test_hash=current_hash)
                        self._defer_quality(module, "test", f"test loop exceeded {max_rounds} rounds")
                        return test_result
                    repair_prompt = base_prompt + f"""

Test failures require repair before review. This is repair round {round_number}; test
feedback is authoritative:
{json.dumps(test_result, ensure_ascii=False, indent=2)}

You are the implementation agent. Modify only implementation files belonging to the
current module, resolve the failing tests, and leave test files intact unless a test
itself is demonstrably incorrect. You must make a concrete file change when a failure
is actionable and report changed_files; do not return only an explanation.
"""
                    self.checkpoint.update_pipeline(module.node_id if module else "ROOT", node="repair", loop_status="changes_requested", round_number=round_number, review_findings=test_result.get("findings", []))
                    repair_result = self._run_agent(repair_role, repair_prompt.strip(), module=module, phase="repair", round_number=round_number, workspace_dir=workspace_dir, parent_id=parent_id)
                    self._message("agent.message", repair_role, module=module, phase="repair", round_number=round_number, payload={"response": str(getattr(repair_result, "final_response", "") or "")[:20000], "test_feedback": test_result}, parent_id=parent_id)
                    previous_hash = current_hash
                    previous_fingerprint = current_fingerprint
                    parent_id = self.bus.replay(module_id=module.node_id if module else "")[-1]["id"] if self.bus.replay(module_id=module.node_id if module else "") else parent_id
                    continue
            self.checkpoint.update_pipeline(
                module.node_id if module else "ROOT",
                node="review_loop",
                round_number=round_number,
                loop_status="reviewing",
            )
            review_workspace = workspace_dir or self.output_dir
            before_files = _file_snapshot(review_workspace)
            before = _content_fingerprint(review_workspace)
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
                "verdict": "pass", "summary": "Reviewer completed without structured findings.", "findings": [], "checks": [], "raw": ""
            }
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
                self.checkpoint.update_pipeline(module.node_id if module else "ROOT", node="checkpoint", loop_status="approved", review_findings=feedback.get("findings", []), last_feedback_message_id=feedback_message["id"], last_feedback_hash=current_hash)
                self._message("pipeline.state", "orchestrator", module=module, phase="review", round_number=round_number, payload={"status": "approved"}, parent_id=verdict["id"])
                return feedback
            fingerprint = after
            if current_hash == previous_hash and fingerprint == previous_fingerprint:
                self.checkpoint.update_pipeline(module.node_id if module else "ROOT", loop_status="blocked", last_feedback_hash=current_hash, review_findings=feedback.get("findings", []))
                self._defer_quality(module, "review", "review loop made no progress")
                return feedback
            if round_number >= max_rounds:
                self.checkpoint.update_pipeline(module.node_id if module else "ROOT", loop_status="blocked", last_feedback_hash=current_hash, review_findings=feedback.get("findings", []))
                self._defer_quality(module, "review", f"review loop exceeded {max_rounds} rounds")
                return feedback
            if module:
                self.checkpoint.update_pipeline(
                    module.node_id,
                    node="repair",
                    round_number=round_number,
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
            Coordinator plan path: {plan_path}
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
            """
        ).strip()

    def run(self, modules: list[RequirementModule]) -> None:
        run_started = time.monotonic()
        state = self.checkpoint.read()
        completed_ids = list(state["completed"])
        completed_set = set(completed_ids)
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

            if resume_loop and resume_feedback:
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
                if planner_enabled:
                    self._check_pause(module, "design")
                    self.checkpoint.mark_module_started(module.node_id, "design")
                    self.runtime.events.mark_design_started(module.node_id, "HAFleet planner started")
                    log(f"[hafleet]   planner started -> {plan_path}", flush=True)
                    try:
                        self._run_agent(
                            self.pipeline.role_for("planner", "planner"),
                            base_prompt + f"\n\nWrite the implementation plan to exactly: {plan_path}",
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
                    self.runtime.events.mark_design_done(module.node_id, "HAFleet plan completed")
                    log(f"[hafleet]   planner finished ({plan_path.stat().st_size} bytes)", flush=True)

                self._check_pause(module, "implement")
                self.checkpoint.mark_module_started(module.node_id, "implement")
                if not planner_enabled:
                    self.runtime.events.mark_design_started(
                        module.node_id, "HAFleet implementer planning started"
                    )
                self.runtime.events.mark_implementation_started(
                    module.node_id, "HAFleet implementer started"
                )
                log(
                    f"[hafleet]   implementer started{' (planning + implementation)' if not planner_enabled else ''}: {module.node_id}",
                    flush=True,
                )
                try:
                    implementer_prompt = base_prompt + (
                        f"\n\nYou own planning and implementation, as well as test authoring, for this module. First write a concrete implementation plan to exactly: {plan_path}. Then implement the complete requirement subtree, create or update executable tests derived directly from its requirement IDs and scenarios, and run those tests plus focused build checks in the same turn. For web behavior use Playwright when appropriate. Do not delegate planning or testing to another role."
                        if not planner_enabled
                        else "\n\nRead the coordinator plan, then implement the complete subtree, create or update requirement-derived executable tests, and run those tests plus focused build checks now."
                    )
                    self._run_agent(
                        self.pipeline.role_for("implementer", "implementer"),
                        implementer_prompt,
                        module=module,
                        phase="implement",
                    )
                    if not planner_enabled:
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
                    self._review_loop(module, base_prompt)
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

                    Preserve all already-implemented behavior and module boundaries. Make real
                    source changes (not static text), update or add requirement-derived tests,
                    and run the available build/focused checks. Do not tailor selectors or
                    behavior to undocumented tests. Return a concise structured summary of
                    changed_files, requirement_ids, checks, and unresolved risks.
                    """
                ).strip()
                self._run_agent(
                    self.pipeline.role_for("implementer", "implementer"),
                    integration_prompt,
                    phase="integration",
                )
            if final_review_enabled:
                log("[hafleet] Final integration review started", flush=True)
                self._review_loop(
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
                        files.
                        """
                    ).strip(),
                    final_review=True,
                )
            log(
                f"[hafleet] Final review {'enabled' if final_review_enabled else 'disabled'}; "
                "running delivery postflight",
                flush=True,
            )
            self._run_postflight(module_ids)
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
        if planner_enabled:
            self._run_agent(
                self.pipeline.role_for("planner", "planner"),
                base_prompt + f"\n\nWrite the implementation plan to exactly: {plan_path}",
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
        self._run_agent(
            self.pipeline.role_for("implementer", "implementer"),
            base_prompt + (
                f"\n\nYou own planning and implementation, as well as test authoring, for this module. First write a concrete implementation plan to exactly: {plan_path}. Then implement the complete requirement subtree, create or update executable tests derived directly from its requirement IDs and scenarios, and run those tests plus focused build checks in the same turn. For web behavior use Playwright when appropriate. Do not delegate planning or testing to another role."
                if not planner_enabled
                else "\n\nRead the coordinator plan, then implement the complete subtree, create or update requirement-derived executable tests, and run those tests plus focused build checks now."
            ),
            module=module,
            phase="implement",
            workspace_dir=workspace,
        )
        if not planner_enabled:
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
        self._review_loop(module, base_prompt, workspace_dir=workspace)
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
                        "phase": "design" if self._planner_enabled() else "implement",
                    },
                )
                self.runtime.events.mark_design_started(
                    module.node_id,
                    "HAFleet parallel planner started"
                    if self._planner_enabled()
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

    def _run_postflight(self, module_ids: str) -> None:
        enabled = os.environ.get("HAFLEET_POSTFLIGHT", "1").strip().lower() not in {
            "0",
            "false",
            "no",
        }
        if not enabled or self.task_type != "web":
            reason = "disabled by HAFLEET_POSTFLIGHT" if not enabled else f"task type is {self.task_type}"
            log(f"[hafleet] Web postflight skipped ({reason})", flush=True)
            return
        try:
            repair_attempts = max(int(os.environ.get("HAFLEET_POSTFLIGHT_REPAIRS", "2")), 0)
        except ValueError:
            repair_attempts = 2

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
            try:
                rehearse_web_app(self.output_dir, self.smoke_port)
                log(
                    f"[hafleet] Web postflight passed on smoke port {self.smoke_port}",
                    flush=True,
                )
                self._message(
                    "operation.completed",
                    "orchestrator",
                    phase="postflight",
                    payload={"operation": "postflight", "attempt": attempt + 1},
                    parent_id=operation["id"],
                )
                return
            except PostflightError as exc:
                self._message(
                    "operation.failed",
                    "orchestrator",
                    phase="postflight",
                    payload={"operation": "postflight", "attempt": attempt + 1, "error": str(exc)},
                    parent_id=operation["id"],
                )
                if attempt >= repair_attempts:
                    raise
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
                        """
                    ).strip(),
                )
