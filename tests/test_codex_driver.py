from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from hafleet_arc.codex_driver import CodexFleet


class FakeThread:
    def __init__(self, outcome) -> None:
        self.outcome = outcome

    def run(self, _prompt: str):
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


class FakeCodex:
    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = list(outcomes)
        self.starts = 0

    def thread_start(self, **_kwargs):
        self.starts += 1
        return FakeThread(self.outcomes.pop(0))


class CodexFleetRetryTests(unittest.TestCase):
    def test_workspace_dir_uses_distinct_codex_threads(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "one"
            second = root / "two"
            first.mkdir()
            second.mkdir()
            fleet = CodexFleet(root)
            codex = FakeCodex(
                [
                    SimpleNamespace(error=None, final_response="one"),
                    SimpleNamespace(error=None, final_response="two"),
                ]
            )
            fleet._codex = codex
            fleet.run("implementer", "implement", workspace_dir=first)
            fleet.run("implementer", "implement", workspace_dir=second)
            self.assertEqual(codex.starts, 2)

    def test_retries_transient_failure_with_fresh_role_thread(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fleet = CodexFleet(Path(temporary))
            codex = FakeCodex(
                [RuntimeError("503 server overloaded"), SimpleNamespace(error=None, final_response="done")]
            )
            fleet._codex = codex
            with mock.patch.dict(
                "os.environ",
                {
                    "HAFLEET_MAX_ATTEMPTS": "2",
                    "HAFLEET_RETRY_DELAYS": "0",
                    "HAFLEET_TURN_TIMEOUT": "2",
                },
                clear=False,
            ):
                result = fleet.run("implementer", "implement")

            self.assertEqual(result.final_response, "done")
            self.assertEqual(codex.starts, 2)

    def test_does_not_retry_non_transient_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fleet = CodexFleet(Path(temporary))
            codex = FakeCodex([RuntimeError("invalid request")])
            fleet._codex = codex
            with mock.patch.dict(
                "os.environ",
                {"HAFLEET_MAX_ATTEMPTS": "3", "HAFLEET_RETRY_DELAYS": "0"},
                clear=False,
            ), self.assertRaisesRegex(RuntimeError, "invalid request"):
                fleet.run("planner", "plan")

            self.assertEqual(codex.starts, 1)

    def test_retries_empty_turn(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fleet = CodexFleet(Path(temporary))
            codex = FakeCodex(
                [SimpleNamespace(error=None, final_response=None), SimpleNamespace(error=None, final_response="ok")]
            )
            fleet._codex = codex
            with mock.patch.dict(
                "os.environ",
                {
                    "HAFLEET_MAX_ATTEMPTS": "2",
                    "HAFLEET_RETRY_DELAYS": "0",
                    "HAFLEET_TURN_TIMEOUT": "2",
                },
                clear=False,
            ):
                fleet.run("reviewer", "review")

            self.assertEqual(codex.starts, 2)


if __name__ == "__main__":
    unittest.main()
