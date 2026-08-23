from __future__ import annotations

import json
import os
import shutil
import textwrap
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Protocol

from .checkpoint import CheckpointStore
from .models import RequirementModule
from .postflight import PostflightError, rehearse_web_app, validate_web_structure
from .log import log
from .worktree import WorktreeConflict, WorktreeManager


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
    def commit(self, message: str) -> bool: ...


class RuntimeLike(Protocol):
    events: RuntimeEvents
    git: RuntimeGit


class PauseRequested(RuntimeError):
    pass


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

    def _check_pause(self, module: RequirementModule | None, phase: str | None) -> None:
        if not self.pause_request_path.exists():
            return
        self.checkpoint.mark_paused(module.node_id if module else None, phase)
        raise PauseRequested("ARC-Bench pause requested")

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
            the full requirement tree yet.
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

            self._check_pause(module, "design")
            self.checkpoint.mark_module_started(module.node_id, "design")
            self.runtime.events.mark_design_started(module.node_id, "HAFleet planner started")
            log(f"[hafleet]   planner started -> {plan_path}", flush=True)
            try:
                self.driver.run(
                    "planner",
                    base_prompt + f"\n\nWrite the implementation plan to exactly: {plan_path}",
                )
                if not plan_path.is_file() or not plan_path.read_text(
                    encoding="utf-8", errors="ignore"
                ).strip():
                    self.driver.run(
                        "planner",
                        base_prompt
                        + f"\n\nThe required plan file was not created. Write a concrete plan now to exactly: {plan_path}",
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
            self.runtime.events.mark_implementation_started(module.node_id, "HAFleet implementer started")
            log(f"[hafleet]   implementer started: {module.node_id}", flush=True)
            try:
                self.driver.run(
                    "implementer",
                    base_prompt + "\n\nRead the coordinator plan, then implement and verify the complete subtree now.",
                )
                self._check_pause(module, "review")
                self.checkpoint.mark_module_started(module.node_id, "review")
                log(f"[hafleet]   reviewer started: {module.node_id}", flush=True)
                self.driver.run(
                    "reviewer",
                    base_prompt + "\n\nReview the current implementation, run checks, and directly repair every issue found.",
                )
            except PauseRequested:
                raise
            except Exception:
                self.runtime.events.mark_implementation_failed(module.node_id, "HAFleet worker failed")
                raise

            self.runtime.events.mark_implementation_done(module.node_id, "Implementation reviewed and repaired")
            checkpoint_message = f"{module.node_id}: implement and review {module.name}"
            committed = self.runtime.git.commit(checkpoint_message)
            log(
                f"[hafleet]   checkpoint {'created' if committed else 'skipped'}: {checkpoint_message}",
                flush=True,
            )
            self.checkpoint.mark_module_completed(module.node_id, module.index)
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
            if final_review_enabled:
                log("[hafleet] Final integration review started", flush=True)
                self.driver.run(
                    "reviewer",
                    textwrap.dedent(
                        f"""
                        Perform the final integration review for ARC-Bench task type {self.task_type}.
                        Completed ROOT modules: {module_ids}.
                        Read {self.architecture_path} and preserve its module boundaries.
                        Inspect the whole project, run the build and practical tests, fix regressions and
                        integration gaps, and leave the application runnable. For web smoke tests use only
                        port {self.smoke_port}, stop every server afterward, and never bind the grading port.
                        """
                    ).strip(),
                )
            log(
                f"[hafleet] Final review {'enabled' if final_review_enabled else 'disabled'}; "
                "running delivery postflight",
                flush=True,
            )
            self._run_postflight(module_ids)
            final_checkpoint = "ROOT: final HAFleet integration review"
            committed = self.runtime.git.commit(final_checkpoint)
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
        self.driver.run(
            "planner",
            base_prompt + f"\n\nWrite the implementation plan to exactly: {plan_path}",
            workspace_dir=workspace,
        )
        if not plan_path.is_file() or not plan_path.read_text(encoding="utf-8", errors="ignore").strip():
            self.driver.run(
                "planner",
                base_prompt
                + f"\n\nThe required plan file was not created. Write a concrete plan now to exactly: {plan_path}",
                workspace_dir=workspace,
            )
        if not plan_path.is_file() or not plan_path.read_text(encoding="utf-8", errors="ignore").strip():
            raise RuntimeError(f"planner did not create required plan: {plan_path}")
        self.driver.run(
            "implementer",
            base_prompt + "\n\nRead the coordinator plan, then implement and verify the complete subtree now.",
            workspace_dir=workspace,
        )
        self.driver.run(
            "reviewer",
            base_prompt + "\n\nReview the current implementation, run checks, and directly repair every issue found.",
            workspace_dir=workspace,
        )
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
                        "phase": "planner",
                    },
                )
                self.runtime.events.mark_design_started(module.node_id, "HAFleet parallel planner started")
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
                    commit = manager.ensure_commit(
                        workspace,
                        f"{module.node_id}: implement and review {module.name}",
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
            self.driver.run("architect", self._architecture_prompt(requirement_tree))
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
            committed = self.runtime.git.commit(checkpoint_message)
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
                return
            except PostflightError as exc:
                if attempt >= repair_attempts:
                    raise
                log(
                    f"[hafleet] Web postflight failed; repair {attempt + 1}/{repair_attempts}: {exc}",
                    flush=True,
                )
                self.driver.run(
                    "reviewer",
                    textwrap.dedent(
                        f"""
                        The ARC-Bench web delivery postflight failed after modules {module_ids}.
                        Repair the project now, then verify it on smoke port {self.smoke_port}.
                        The grader requires frontend/package.json with `npm run build` and
                        backend/package.json with `npm run start`; the backend must read PORT and
                        serve the built frontend. Do not bind the grading port. Stop all servers.

                        Exact postflight error:
                        {exc}
                        """
                    ).strip(),
                )
