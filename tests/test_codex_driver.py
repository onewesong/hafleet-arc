from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from hafleet_arc.codex_driver import CodexFleet, TurnTimeoutError


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
        self.start_kwargs: list[dict[str, object]] = []

    def thread_start(self, **kwargs):
        self.starts += 1
        self.start_kwargs.append(kwargs)
        return FakeThread(self.outcomes.pop(0))


class CodexFleetRetryTests(unittest.TestCase):
    def test_role_model_overrides_global_model(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fleet = CodexFleet(Path(temporary))
            codex = FakeCodex([SimpleNamespace(error=None, final_response="architect")])
            fleet._codex = codex
            with mock.patch.dict(
                "os.environ",
                {
                    "MODEL": "global-model",
                    "HAFLEET_ARCHITECT_MODEL": "architect-model",
                },
                clear=False,
            ):
                fleet.run("architect", "design")

            self.assertEqual(codex.start_kwargs[0]["model"], "architect-model")

    def test_role_model_falls_back_to_global_model(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fleet = CodexFleet(Path(temporary))
            codex = FakeCodex([SimpleNamespace(error=None, final_response="planner")])
            fleet._codex = codex
            with mock.patch.dict(
                "os.environ",
                {"MODEL": "global-model", "HAFLEET_PLANNER_MODEL": ""},
                clear=False,
            ):
                fleet.run("planner", "plan")

            self.assertEqual(codex.start_kwargs[0]["model"], "global-model")

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

    def test_retries_model_capacity_response_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fleet = CodexFleet(Path(temporary))
            codex = FakeCodex(
                [
                    RuntimeError("Selected model is at capacity. Please try a different model."),
                    SimpleNamespace(error=None, final_response="done"),
                ]
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
                result = fleet.run("reviewer", "review")

            self.assertEqual(result.final_response, "done")
            self.assertEqual(codex.starts, 2)

    def test_default_retry_budget_survives_repeated_capacity_errors(self) -> None:
        """The unattended default allows several provider capacity windows."""

        with tempfile.TemporaryDirectory() as temporary:
            fleet = CodexFleet(Path(temporary))
            codex = FakeCodex(
                [
                    RuntimeError("Selected model is at capacity. Please try a different model.")
                    for _ in range(5)
                ]
                + [SimpleNamespace(error=None, final_response="done")]
            )
            fleet._codex = codex
            with mock.patch.dict(
                "os.environ",
                {"HAFLEET_RETRY_DELAYS": "0"},
                clear=False,
            ):
                # Ensure an ambient setting cannot mask the documented default.
                os.environ.pop("HAFLEET_MAX_ATTEMPTS", None)
                result = fleet.run("implementer", "implement")

            self.assertEqual(result.final_response, "done")
            self.assertEqual(codex.starts, 6)

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

    def test_timeout_with_workspace_progress_uses_continuation_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fleet = CodexFleet(root)
            prompts: list[str] = []

            def run_once(role, prompt, timeout_s, workspace_dir=None):
                prompts.append(prompt)
                if len(prompts) == 1:
                    (root / "partial.js").write_text("partial progress\n", encoding="utf-8")
                    raise TurnTimeoutError("implementer turn timed out after 2s")
                return SimpleNamespace(error=None, final_response="done")

            with mock.patch.object(fleet, "_run_once", side_effect=run_once), mock.patch.dict(
                "os.environ",
                {"HAFLEET_MAX_ATTEMPTS": "2", "HAFLEET_RETRY_DELAYS": "0", "HAFLEET_TURN_TIMEOUT": "2"},
                clear=False,
            ):
                result = fleet.run("implementer", "very large original requirement prompt")

            self.assertEqual(result.final_response, "done")
            self.assertEqual(prompts[0], "very large original requirement prompt")
            self.assertIn("Continue the interrupted implementer task", prompts[1])
            self.assertNotIn("very large original requirement prompt", prompts[1])

    def test_reviewer_uses_read_only_sandbox(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fleet = CodexFleet(Path(temporary))
            codex = FakeCodex([SimpleNamespace(error=None, final_response="review")])
            fleet._codex = codex
            fleet.run("reviewer", "review")
            self.assertEqual(str(codex.start_kwargs[0]["sandbox"]), "Sandbox.read_only")

    def test_role_prompt_is_loaded_from_pipeline_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / ".arc" / "hafleet" / "pipeline.yaml"
            config.parent.mkdir(parents=True)
            config.write_text(
                "version: 1\n"
                "roles:\n"
                "  planner: |\n"
                "    Custom planner instructions from YAML.\n"
                "nodes:\n"
                "  - {id: planner, type: agent, role: planner}\n"
                "  - {id: review_loop, type: loop, review: reviewer, repair: implementer, max_rounds: 1}\n",
                encoding="utf-8",
            )
            fleet = CodexFleet(root)
            codex = FakeCodex([SimpleNamespace(error=None, final_response="planner")])
            fleet._codex = codex
            fleet.run("planner", "plan")
            self.assertIn("Custom planner instructions from YAML.", codex.start_kwargs[0]["developer_instructions"])


if __name__ == "__main__":
    unittest.main()
