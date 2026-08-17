from __future__ import annotations

import json
import os
import shutil
import textwrap
from pathlib import Path
from typing import Protocol

from .checkpoint import CheckpointStore
from .models import RequirementModule
from .postflight import PostflightError, rehearse_web_app


class FleetDriver(Protocol):
    def run(self, role: str, prompt: str) -> object: ...


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
    ) -> None:
        self.driver = driver
        self.runtime = runtime
        self.checkpoint = checkpoint
        self.requirements_dir = requirements_dir
        self.output_dir = output_dir
        self.task_type = task_type
        self.smoke_port = smoke_port
        self.plan_dir = output_dir / ".arc" / "hafleet" / "plans"
        configured_pause = os.environ.get("ARCBENCH_PAUSE_REQUEST_PATH", "").strip()
        self.pause_request_path = Path(configured_pause) if configured_pause else output_dir / ".arc" / "pause-request"

    def _check_pause(self, module: RequirementModule | None, phase: str | None) -> None:
        if not self.pause_request_path.exists():
            return
        self.checkpoint.mark_paused(module.node_id if module else None, phase)
        raise PauseRequested("ARC-Bench pause requested")

    def _base_prompt(self, module: RequirementModule, completed_ids: list[str], plan_path: Path) -> str:
        completed = ", ".join(completed_ids) if completed_ids else "none"
        return textwrap.dedent(
            f"""
            ARC-Bench task type: {self.task_type}
            Module: {module.index}/{module.total} - {module.node_id} - {module.name}
            Requirement source directory: {self.requirements_dir}
            Previously completed ROOT modules: {completed}
            Coordinator plan path: {plan_path}

            Complete requirement subtree:
            ```json
            {json.dumps(module.subtree, ensure_ascii=False, indent=2)}
            ```
            """
        ).strip()

    def run(self, modules: list[RequirementModule]) -> None:
        state = self.checkpoint.read()
        completed_ids = list(state["completed"])
        completed_set = set(completed_ids)
        self.plan_dir.mkdir(parents=True, exist_ok=True)

        for module in modules:
            if module.node_id in completed_set:
                print(f"[hafleet] Skipping completed module {module.node_id}", flush=True)
                continue
            plan_path = self.plan_dir / f"{module.node_id}.md"
            base_prompt = self._base_prompt(module, completed_ids, plan_path)

            self._check_pause(module, "design")
            self.checkpoint.mark_module_started(module.node_id, "design")
            self.runtime.events.mark_design_started(module.node_id, "HAFleet planner started")
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

            self._check_pause(module, "implement")
            self.checkpoint.mark_module_started(module.node_id, "implement")
            self.runtime.events.mark_implementation_started(module.node_id, "HAFleet implementer started")
            try:
                self.driver.run(
                    "implementer",
                    base_prompt + "\n\nRead the coordinator plan, then implement and verify the complete subtree now.",
                )
                self._check_pause(module, "review")
                self.checkpoint.mark_module_started(module.node_id, "review")
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
            self.runtime.git.commit(f"{module.node_id}: implement and review {module.name}")
            self.checkpoint.mark_module_completed(module.node_id, module.index)
            completed_ids.append(module.node_id)
            completed_set.add(module.node_id)
            print(f"[hafleet] Completed {module.index}/{module.total}: {module.node_id}", flush=True)

        final_review_enabled = os.environ.get("HAFLEET_FINAL_REVIEW", "1").strip().lower() not in {
            "0",
            "false",
            "no",
        }
        if modules and not bool(self.checkpoint.read().get("final_review_completed")):
            self._check_pause(None, "final-review")
            module_ids = ", ".join(item.node_id for item in modules)
            if final_review_enabled:
                self.driver.run(
                    "reviewer",
                    textwrap.dedent(
                        f"""
                        Perform the final integration review for ARC-Bench task type {self.task_type}.
                        Completed ROOT modules: {module_ids}.
                        Inspect the whole project, run the build and practical tests, fix regressions and
                        integration gaps, and leave the application runnable. For web smoke tests use only
                        port {self.smoke_port}, stop every server afterward, and never bind the grading port.
                        """
                    ).strip(),
                )
            self._run_postflight(module_ids)
            self.runtime.git.commit("ROOT: final HAFleet integration review")
            self.checkpoint.mark_final_review_completed()

    def _run_postflight(self, module_ids: str) -> None:
        enabled = os.environ.get("HAFLEET_POSTFLIGHT", "1").strip().lower() not in {
            "0",
            "false",
            "no",
        }
        if not enabled or self.task_type != "web":
            return
        try:
            repair_attempts = max(int(os.environ.get("HAFLEET_POSTFLIGHT_REPAIRS", "2")), 0)
        except ValueError:
            repair_attempts = 2

        for attempt in range(repair_attempts + 1):
            self._check_pause(None, "postflight")
            try:
                rehearse_web_app(self.output_dir, self.smoke_port)
                print(
                    f"[hafleet] Web postflight passed on smoke port {self.smoke_port}",
                    flush=True,
                )
                return
            except PostflightError as exc:
                if attempt >= repair_attempts:
                    raise
                print(
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
