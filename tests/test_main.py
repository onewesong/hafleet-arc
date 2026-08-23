from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import ClassVar, Self
from unittest import mock

import main as entrypoint


class FakeEvents:
    def __init__(self) -> None:
        self.states: list[str] = []

    def __getattr__(self, name: str):
        def record(*_args, **_kwargs) -> None:
            self.states.append(name)

        return record


class FakeTraceability:
    def __init__(self) -> None:
        self.tree = None

    def init_store(self) -> None:
        pass

    def store_requirement_tree(self, tree) -> None:
        self.tree = tree


class FakeGit:
    def __init__(self) -> None:
        self.commits: list[str] = []

    def ensure_repo(self) -> None:
        pass

    def commit(self, message: str) -> bool:
        self.commits.append(message)
        return True


class FakeRuntime:
    def __init__(self) -> None:
        self.events = FakeEvents()
        self.traceability = FakeTraceability()
        self.git = FakeGit()


class FakeFleet:
    instances: ClassVar[list[FakeFleet]] = []

    def __init__(self, *_args, **_kwargs) -> None:
        self.calls: list[str] = []
        self.__class__.instances.append(self)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args) -> None:
        pass

    def run(self, role: str, prompt: str) -> None:
        self.calls.append(role)
        if role == "architect":
            marker = "Architecture document path: "
            architecture_path = Path(prompt.split(marker, 1)[1].splitlines()[0].strip())
            architecture_path.parent.mkdir(parents=True, exist_ok=True)
            architecture_path.write_text("# Architecture\n", encoding="utf-8")
        if role == "planner" and "exactly:" in prompt:
            plan_path = Path(prompt.rsplit("exactly:", 1)[1].strip().splitlines()[0])
            plan_path.parent.mkdir(parents=True, exist_ok=True)
            plan_path.write_text("# Plan\n", encoding="utf-8")


class EntrypointSmokeTests(unittest.TestCase):
    def test_arcbench_command_contract_runs_to_completion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            requirements = root / "requirements"
            output = root / "output"
            requirements.mkdir()
            (requirements / "requirements.yaml").write_text(
                "id: ROOT\nchildren:\n  - id: REQ-1\n    name: Demo\n",
                encoding="utf-8",
            )
            runtime = FakeRuntime()
            with (
                mock.patch.object(entrypoint, "_runtime", return_value=runtime),
                mock.patch.object(entrypoint, "CodexFleet", FakeFleet),
                mock.patch.dict(
                    "os.environ",
                    {"HAFLEET_FINAL_REVIEW": "0", "HAFLEET_POSTFLIGHT": "0"},
                    clear=False,
                ),
            ):
                result = entrypoint.main(
                    [str(requirements), "--output-dir", str(output), "--type", "web"]
                )

            self.assertEqual(result, 0)
            self.assertEqual(
                FakeFleet.instances[-1].calls,
                ["architect", "planner", "implementer", "reviewer"],
            )
            self.assertEqual(runtime.traceability.tree["id"], "ROOT")
            self.assertIn("mark_run_completed", runtime.events.states)
            self.assertTrue((output / ".arc" / "checkpoint.json").is_file())
            self.assertTrue((output / "frontend" / "package.json").is_file())
            self.assertTrue((output / "backend" / "package.json").is_file())


if __name__ == "__main__":
    unittest.main()
