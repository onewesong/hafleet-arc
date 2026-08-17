from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hafleet_arc.checkpoint import CheckpointStore
from hafleet_arc.models import RequirementModule
from hafleet_arc.orchestrator import FleetOrchestrator
from hafleet_arc.postflight import PostflightError


class FakeDriver:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def run(self, role: str, prompt: str) -> None:
        self.calls.append((role, prompt))
        if role == "planner" and "exactly:" in prompt:
            plan_path = Path(prompt.rsplit("exactly:", 1)[1].strip().splitlines()[0])
            plan_path.parent.mkdir(parents=True, exist_ok=True)
            plan_path.write_text("# Plan\n", encoding="utf-8")


class FakeEvents:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __getattr__(self, name: str):
        def record(node_id: str, message: str | None = None) -> None:
            self.calls.append((name, node_id))

        return record


class FakeGit:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def commit(self, message: str) -> bool:
        self.messages.append(message)
        return True


class FakeRuntime:
    def __init__(self) -> None:
        self.events = FakeEvents()
        self.git = FakeGit()


class OrchestratorTests(unittest.TestCase):
    def _module(self, index: int, node_id: str) -> RequirementModule:
        return RequirementModule(index, 2, node_id, node_id, {"id": node_id})

    def test_runs_three_roles_and_checkpoints_each_module(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            driver = FakeDriver()
            runtime = FakeRuntime()
            checkpoint = CheckpointStore(root / ".arc" / "checkpoint.json")
            orchestrator = FleetOrchestrator(
                driver=driver,
                runtime=runtime,
                checkpoint=checkpoint,
                requirements_dir=root / "requirements",
                output_dir=root,
                task_type="web",
            )
            with mock.patch.dict("os.environ", {"HAFLEET_POSTFLIGHT": "0"}, clear=False):
                orchestrator.run([self._module(1, "REQ-1"), self._module(2, "REQ-2")])

            roles = [role for role, _ in driver.calls]
            self.assertEqual(roles, ["planner", "implementer", "reviewer", "planner", "implementer", "reviewer", "reviewer"])
            self.assertEqual(checkpoint.read()["completed"], ["REQ-1", "REQ-2"])
            self.assertEqual(len(runtime.git.messages), 3)

    def test_resume_skips_completed_modules(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checkpoint = CheckpointStore(root / ".arc" / "checkpoint.json")
            checkpoint.mark_module_completed("REQ-1", 1)
            driver = FakeDriver()
            orchestrator = FleetOrchestrator(
                driver=driver,
                runtime=FakeRuntime(),
                checkpoint=checkpoint,
                requirements_dir=root / "requirements",
                output_dir=root,
                task_type="web",
            )
            with mock.patch.dict("os.environ", {"HAFLEET_POSTFLIGHT": "0"}, clear=False):
                orchestrator.run([self._module(1, "REQ-1"), self._module(2, "REQ-2")])

            self.assertEqual([role for role, _ in driver.calls], ["planner", "implementer", "reviewer", "reviewer"])

    def test_postflight_failure_prevents_final_completion_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checkpoint = CheckpointStore(root / ".arc" / "checkpoint.json")
            runtime = FakeRuntime()
            orchestrator = FleetOrchestrator(
                driver=FakeDriver(),
                runtime=runtime,
                checkpoint=checkpoint,
                requirements_dir=root / "requirements",
                output_dir=root,
                task_type="web",
            )
            with (
                mock.patch.dict(
                    "os.environ",
                    {
                        "HAFLEET_FINAL_REVIEW": "0",
                        "HAFLEET_POSTFLIGHT": "1",
                        "HAFLEET_POSTFLIGHT_REPAIRS": "0",
                    },
                    clear=False,
                ),
                mock.patch(
                    "hafleet_arc.orchestrator.rehearse_web_app",
                    side_effect=PostflightError("broken build"),
                ),
                self.assertRaisesRegex(PostflightError, "broken build"),
            ):
                orchestrator.run([self._module(1, "REQ-1")])

            self.assertEqual(checkpoint.read()["completed"], ["REQ-1"])
            self.assertFalse(checkpoint.read()["final_review_completed"])
            self.assertNotIn("ROOT: final HAFleet integration review", runtime.git.messages)


if __name__ == "__main__":
    unittest.main()
