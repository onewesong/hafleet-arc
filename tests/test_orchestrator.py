from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from hafleet_arc.checkpoint import CheckpointStore
from hafleet_arc.models import RequirementModule
from hafleet_arc.orchestrator import FleetOrchestrator


class FakeDriver:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def run(self, role: str, prompt: str) -> None:
        self.calls.append((role, prompt))


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
            orchestrator.run([self._module(1, "REQ-1"), self._module(2, "REQ-2")])

            self.assertEqual([role for role, _ in driver.calls], ["planner", "implementer", "reviewer", "reviewer"])


if __name__ == "__main__":
    unittest.main()
