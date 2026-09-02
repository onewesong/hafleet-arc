from __future__ import annotations

import tempfile
import unittest
import subprocess
import shutil
from types import SimpleNamespace
from pathlib import Path
from unittest import mock

from hafleet_arc.checkpoint import CheckpointStore
from hafleet_arc.models import RequirementModule
from hafleet_arc.orchestrator import FleetOrchestrator, PauseRequested
from hafleet_arc.postflight import PostflightError


class FakeDriver:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def run(self, role: str, prompt: str, workspace_dir: Path | None = None) -> None:
        self.calls.append((role, prompt))
        if role == "architect":
            marker = "Architecture document path: "
            architecture_path = Path(
                prompt.split(marker, 1)[1].splitlines()[0].strip()
            )
            architecture_path.parent.mkdir(parents=True, exist_ok=True)
            architecture_path.write_text("# Architecture\n", encoding="utf-8")
            if "ARC-Bench task type: web" in prompt:
                project_root = architecture_path.parents[2]
                frontend = project_root / "frontend"
                backend = project_root / "backend"
                (frontend / "src").mkdir(parents=True, exist_ok=True)
                (backend / "data").mkdir(parents=True, exist_ok=True)
                (frontend / "package.json").write_text(
                    '{"scripts":{"build":"node -e \\\"\\\""}}\n', encoding="utf-8"
                )
                (backend / "package.json").write_text(
                    '{"scripts":{"start":"node server.js"}}\n', encoding="utf-8"
                )
                (backend / "server.js").write_text(
                    'const port = process.env.PORT;\n', encoding="utf-8"
                )
        if role == "planner" and "exactly:" in prompt:
            plan_path = Path(prompt.rsplit("exactly:", 1)[1].strip().splitlines()[0])
            plan_path.parent.mkdir(parents=True, exist_ok=True)
            plan_path.write_text("# Plan\n", encoding="utf-8")
        if workspace_dir is not None and role in {"implementer", "reviewer"}:
            module_id = Path(
                prompt.split("Coordinator plan path:", 1)[1].splitlines()[0].strip()
            ).stem
            (workspace_dir / f"{module_id}.txt").write_text("implemented\n", encoding="utf-8")


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

    def test_agent_conversations_follow_module_and_decision_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            class ThreadAwareDriver:
                def __init__(self) -> None:
                    self.resets: list[tuple[str, Path | None]] = []
                    self.calls: list[tuple[str, str]] = []

                def reset_thread(self, role: str, workspace_dir: Path | None = None) -> None:
                    self.resets.append((role, workspace_dir))

                def run(self, role: str, prompt: str, workspace_dir: Path | None = None):
                    self.calls.append((role, prompt))
                    return SimpleNamespace(final_response="done")

            driver = ThreadAwareDriver()
            orchestrator = FleetOrchestrator(
                driver=driver,
                runtime=FakeRuntime(),
                checkpoint=CheckpointStore(root / ".arc" / "checkpoint.json"),
                requirements_dir=root / "requirements",
                output_dir=root,
                task_type="cli",
            )
            first = self._module(1, "REQ-1")
            second = self._module(2, "REQ-2")

            orchestrator._run_agent("implementer", "implement one", module=first, phase="implement")
            orchestrator._run_agent("implementer", "self check", module=first, phase="self-check")
            orchestrator._run_agent("implementer", "complete", module=first, phase="completion")
            orchestrator._run_agent("reviewer", "review", module=first, phase="review")
            orchestrator._run_agent("implementer", "repair", module=first, phase="repair")
            orchestrator._run_agent("reviewer", "review again", module=first, phase="review")
            orchestrator._run_agent("implementer", "implement two", module=second, phase="implement")
            orchestrator._run_agent("implementer", "integrate", phase="integration")

            self.assertEqual(
                [role for role, _ in driver.resets],
                ["implementer", "reviewer", "implementer", "reviewer", "implementer", "implementer"],
            )
            self.assertEqual(len(driver.calls), 8)

    def test_reviewer_feedback_loops_back_to_implementer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            class LoopDriver:
                def __init__(self) -> None:
                    self.calls: list[str] = []
                    self.prompts: list[tuple[str, str]] = []

                def run(self, role: str, prompt: str, workspace_dir: Path | None = None):
                    self.calls.append(role)
                    self.prompts.append((role, prompt))
                    if role == "reviewer" and self.calls.count("reviewer") == 1:
                        return SimpleNamespace(final_response='{"verdict":"changes_requested","summary":"fix","findings":[{"id":"F-1","severity":"major","title":"missing"}],"checks":[]}')
                    if role == "implementer":
                        (workspace_dir or root / "workspace").mkdir(parents=True, exist_ok=True)
                        (workspace_dir or root / "workspace").joinpath("fixed.txt").write_text("fixed\n", encoding="utf-8")
                    return SimpleNamespace(final_response='{"verdict":"pass","summary":"ok","findings":[],"checks":[]}')

            driver = LoopDriver()
            orchestrator = FleetOrchestrator(
                driver=driver,
                runtime=FakeRuntime(),
                checkpoint=CheckpointStore(root / ".arc" / "checkpoint.json"),
                requirements_dir=root / "requirements",
                output_dir=root,
                task_type="cli",
            )
            result = orchestrator._review_loop(self._module(1, "REQ-1"), "Review REQ-1")
            self.assertEqual(result["verdict"], "pass")
            self.assertEqual(driver.calls, ["reviewer", "implementer", "reviewer"])
            review_prompts = [prompt for role, prompt in driver.prompts if role == "reviewer"]
            self.assertIn("first Reviewer pass", review_prompts[0])
            self.assertIn("incremental verification pass", review_prompts[1])
            self.assertIn('"id": "F-1"', review_prompts[1])
            self.assertIn("A workspace/fixed.txt", review_prompts[1])
            messages = orchestrator.bus.replay()
            self.assertTrue(any(item["kind"] == "review.feedback" for item in messages))
            feedback = [item for item in messages if item["kind"] == "review.feedback"]
            self.assertEqual([item["payload"]["review_scope"] for item in feedback], ["full", "incremental"])
            self.assertTrue(any(item["kind"] == "pipeline.state" and item["payload"].get("status") == "approved" for item in messages))

    def test_project_owned_test_failure_loops_to_implementer_before_review(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            class Driver:
                def __init__(self) -> None:
                    self.calls: list[str] = []
                    self.prompts: list[tuple[str, str]] = []

                def run(self, role: str, prompt: str, workspace_dir: Path | None = None):
                    self.calls.append(role)
                    self.prompts.append((role, prompt))
                    return SimpleNamespace(final_response='{"verdict":"pass","summary":"ok","findings":[],"checks":[]}')

            driver = Driver()
            orchestrator = FleetOrchestrator(
                driver=driver,
                runtime=FakeRuntime(),
                checkpoint=CheckpointStore(root / ".arc" / "checkpoint.json"),
                requirements_dir=root / "requirements",
                output_dir=root,
                task_type="web",
            )
            failed = {"verdict": "changes_requested", "summary": "failed", "findings": [{"id": "T-1", "severity": "major", "title": "failed"}], "checks": []}
            passed = {"verdict": "pass", "summary": "passed", "findings": [], "checks": []}
            with mock.patch("hafleet_arc.orchestrator.has_project_tests", return_value=True), mock.patch(
                "hafleet_arc.orchestrator.run_project_tests", side_effect=[failed, passed]
            ):
                result = orchestrator._review_loop(self._module(1, "REQ-1"), "Review REQ-1")
            self.assertEqual(result["verdict"], "pass")
            self.assertEqual(driver.calls, ["implementer", "reviewer"])
            review_prompt = next(prompt for role, prompt in driver.prompts if role == "reviewer")
            self.assertIn("first Reviewer pass", review_prompt)
            feedback = [item for item in orchestrator.bus.replay() if item["kind"] == "review.feedback"]
            self.assertEqual(feedback[0]["payload"]["review_scope"], "full")
            self.assertTrue(any(item["kind"] == "test.failed" for item in orchestrator.bus.replay()))

    def test_project_verification_repairs_do_not_consume_reviewer_rounds(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            class Driver:
                def __init__(self) -> None:
                    self.calls: list[str] = []
                    self.prompts: list[tuple[str, str]] = []
                    self.repairs = 0
                    self.reviews = 0

                def run(self, role: str, prompt: str, workspace_dir: Path | None = None):
                    self.calls.append(role)
                    self.prompts.append((role, prompt))
                    if role == "implementer":
                        self.repairs += 1
                        (workspace_dir or root).joinpath("repair.txt").write_text(
                            f"repair {self.repairs}\n", encoding="utf-8"
                        )
                        return SimpleNamespace(final_response='{"changed_files":["repair.txt"]}')
                    self.reviews += 1
                    if self.reviews == 1:
                        return SimpleNamespace(final_response='{"verdict":"changes_requested","summary":"fix review finding","findings":[{"id":"F-1","severity":"major","title":"missing"}],"checks":[]}')
                    return SimpleNamespace(final_response='{"verdict":"pass","summary":"ok","findings":[],"checks":[]}')

            failed_one = {"verdict": "changes_requested", "summary": "first failure", "findings": [{"id": "T-1", "severity": "major", "title": "failed"}], "checks": []}
            failed_two = {"verdict": "changes_requested", "summary": "second failure", "findings": [{"id": "T-2", "severity": "major", "title": "failed again"}], "checks": []}
            passed = {"verdict": "pass", "summary": "passed", "findings": [], "checks": []}
            driver = Driver()
            orchestrator = FleetOrchestrator(
                driver=driver,
                runtime=FakeRuntime(),
                checkpoint=CheckpointStore(root / ".arc" / "checkpoint.json"),
                requirements_dir=root / "requirements",
                output_dir=root,
                task_type="web",
            )
            with mock.patch.dict(
                "os.environ",
                {"HAFLEET_QUALITY_MAX_ROUNDS": "2", "HAFLEET_VERIFICATION_MAX_REPAIRS": "3"},
                clear=False,
            ), mock.patch("hafleet_arc.orchestrator.has_project_tests", return_value=True), mock.patch(
                "hafleet_arc.orchestrator.run_project_tests",
                side_effect=[failed_one, failed_two, passed, passed],
            ):
                result = orchestrator._review_loop(self._module(1, "REQ-1"), "Review REQ-1")

            self.assertEqual(result["verdict"], "pass")
            self.assertEqual(driver.calls, ["implementer", "implementer", "reviewer", "implementer", "reviewer"])
            review_prompts = [prompt for role, prompt in driver.prompts if role == "reviewer"]
            self.assertIn("Review loop round 1/2", review_prompts[0])
            self.assertIn("Review loop round 2/2", review_prompts[1])
            verification_prompts = [prompt for role, prompt in driver.prompts if role == "implementer" and "Deterministic execution" in prompt]
            self.assertEqual(len(verification_prompts), 2)
            self.assertTrue(all("before Reviewer round\n1/2" in prompt for prompt in verification_prompts))
            state = orchestrator.checkpoint.read()
            self.assertEqual(state["current_review_round"], 2)
            self.assertEqual(state["current_verification_attempt"], 3)

    def test_incremental_review_skips_reviewer_when_repair_changes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            class Driver:
                def __init__(self) -> None:
                    self.calls: list[str] = []

                def run(self, role: str, prompt: str, workspace_dir: Path | None = None):
                    self.calls.append(role)
                    if role == "reviewer":
                        return SimpleNamespace(final_response='{"verdict":"changes_requested","summary":"fix","findings":[{"id":"F-1","severity":"major","title":"missing"}],"checks":[]}')
                    return SimpleNamespace(final_response='{"changed_files":[],"resolved_findings":[],"checks":[]}')

            driver = Driver()
            orchestrator = FleetOrchestrator(
                driver=driver,
                runtime=FakeRuntime(),
                checkpoint=CheckpointStore(root / ".arc" / "checkpoint.json"),
                requirements_dir=root / "requirements",
                output_dir=root,
                task_type="cli",
            )

            result = orchestrator._review_loop(self._module(1, "REQ-1"), "Review REQ-1")

            self.assertEqual(result["verdict"], "changes_requested")
            self.assertEqual(driver.calls, ["reviewer", "implementer"])
            self.assertIn(
                "no project file changes",
                orchestrator.checkpoint.read()["quality_exhaustion_reason"],
            )

    def test_reviewer_receives_current_orchestrator_test_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            class Driver:
                def __init__(self) -> None:
                    self.prompts: list[tuple[str, str]] = []

                def run(self, role: str, prompt: str, workspace_dir: Path | None = None):
                    self.prompts.append((role, prompt))
                    return SimpleNamespace(final_response='{"verdict":"pass","summary":"ok","findings":[],"checks":[]}')

            driver = Driver()
            orchestrator = FleetOrchestrator(
                driver=driver,
                runtime=FakeRuntime(),
                checkpoint=CheckpointStore(root / ".arc" / "checkpoint.json"),
                requirements_dir=root / "requirements",
                output_dir=root,
                task_type="web",
            )
            passed = {
                "verdict": "pass",
                "summary": "current deterministic suite passed",
                "findings": [],
                "checks": [{"name": "focused suite", "status": "passed", "output": "ok"}],
            }
            with mock.patch("hafleet_arc.orchestrator.has_project_tests", return_value=True), mock.patch(
                "hafleet_arc.orchestrator.run_project_tests", return_value=passed
            ):
                orchestrator._review_loop(self._module(1, "REQ-1"), "Review REQ-1")

            review_prompt = next(prompt for role, prompt in driver.prompts if role == "reviewer")
            self.assertIn("current deterministic suite passed", review_prompt)
            self.assertIn("authoritative for current pass/fail status", review_prompt)
            self.assertIn("historical audit artifacts", review_prompt)

    def test_registered_project_tests_restore_product_files_but_keep_arc_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            product_file = root / "backend" / "data" / "db.json"
            product_file.parent.mkdir(parents=True)
            product_file.write_text('{"seed":"original"}\n', encoding="utf-8")
            source_file = root / "frontend" / "src" / "app.js"
            source_file.parent.mkdir(parents=True)
            source_file.write_text("export const app = true;\n", encoding="utf-8")

            orchestrator = FleetOrchestrator(
                driver=FakeDriver(),
                runtime=FakeRuntime(),
                checkpoint=CheckpointStore(root / ".arc" / "checkpoint.json"),
                requirements_dir=root / "requirements",
                output_dir=root,
                task_type="web",
            )

            def mutate_workspace(*args, **kwargs):
                product_file.write_text('{"seed":"test-mutated"}\n', encoding="utf-8")
                source_file.unlink()
                generated = root / "frontend" / "dist" / "index.html"
                generated.parent.mkdir(parents=True)
                generated.write_text("generated\n", encoding="utf-8")
                artifact = root / ".arc" / "hafleet" / "test-results" / "REQ-1.json"
                artifact.parent.mkdir(parents=True, exist_ok=True)
                artifact.write_text('{"verdict":"pass"}\n', encoding="utf-8")
                return {"verdict": "pass", "summary": "passed", "findings": [], "checks": []}

            with mock.patch("hafleet_arc.orchestrator.run_project_tests", side_effect=mutate_workspace):
                result = orchestrator._run_registered_project_tests(
                    self._module(1, "REQ-1"), workspace_dir=root, round_number=1
                )

            self.assertEqual(result["verdict"], "pass")
            self.assertEqual(product_file.read_text(encoding="utf-8"), '{"seed":"original"}\n')
            self.assertEqual(source_file.read_text(encoding="utf-8"), "export const app = true;\n")
            self.assertFalse((root / "frontend" / "dist" / "index.html").exists())
            self.assertTrue((root / ".arc" / "hafleet" / "test-results" / "REQ-1.json").exists())

    def test_requirement_modules_get_implementer_self_check(self) -> None:
        """Nested requirement modules receive a warm-context completeness pass."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            class SelfCheckDriver:
                def __init__(self) -> None:
                    self.calls: list[tuple[str, str]] = []

                def run(self, role: str, prompt: str, workspace_dir: Path | None = None):
                    self.calls.append((role, prompt))
                    if role == "architect":
                        marker = "Architecture document path: "
                        path = Path(prompt.split(marker, 1)[1].splitlines()[0].strip())
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text("# architecture\n", encoding="utf-8")
                    return SimpleNamespace(final_response='{"verdict":"pass","summary":"ok","findings":[],"checks":[]}')

            driver = SelfCheckDriver()
            orchestrator = FleetOrchestrator(
                driver=driver,
                runtime=FakeRuntime(),
                checkpoint=CheckpointStore(root / ".arc" / "checkpoint.json"),
                requirements_dir=root / "requirements",
                output_dir=root,
                task_type="cli",
            )
            with mock.patch.dict("os.environ", {"HAFLEET_POSTFLIGHT": "0"}, clear=False):
                orchestrator.run([
                    RequirementModule(
                        1,
                        1,
                        "REQ-1",
                        "Demo",
                        {"id": "REQ-1", "children": [{"id": "REQ-1.1", "scenarios": []}]},
                    )
                ])
            implementer_prompts = [prompt for role, prompt in driver.calls if role == "implementer"]
            self.assertEqual(len(implementer_prompts), 2)
            self.assertIn("Implementation self-check", implementer_prompts[1])

    def test_quality_exhaustion_is_deferred_for_unattended_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            class StuckDriver:
                def run(self, role: str, prompt: str, workspace_dir: Path | None = None):
                    if role == "reviewer":
                        return SimpleNamespace(
                            final_response='{"verdict":"changes_requested","summary":"still broken","findings":[{"id":"F-1","severity":"major","title":"still broken"}],"checks":[]}'
                        )
                    if role == "implementer":
                        (workspace_dir or root).joinpath("attempt.txt").write_text("attempt\n", encoding="utf-8")
                    return SimpleNamespace(final_response='{"verdict":"changes_requested","summary":"repair attempted","findings":[],"checks":[]}')

            orchestrator = FleetOrchestrator(
                driver=StuckDriver(),
                runtime=FakeRuntime(),
                checkpoint=CheckpointStore(root / ".arc" / "checkpoint.json"),
                requirements_dir=root / "requirements",
                output_dir=root,
                task_type="cli",
            )
            result = orchestrator._review_loop(self._module(1, "REQ-1"), "Review REQ-1")
            self.assertEqual(result["verdict"], "changes_requested")
            state = orchestrator.checkpoint.read()
            self.assertFalse(state["paused"])
            self.assertTrue(state["quality_deferred"])
            self.assertEqual(state["loop_status"], "deferred")
            self.assertTrue(any(item["payload"].get("status") == "quality_deferred" for item in orchestrator.bus.replay() if item["kind"] == "pipeline.state"))

    def test_quality_exhaustion_can_use_strict_pause_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            class StuckDriver:
                def run(self, role: str, prompt: str, workspace_dir: Path | None = None):
                    if role == "reviewer":
                        return SimpleNamespace(
                            final_response='{"verdict":"changes_requested","summary":"still broken","findings":[{"id":"F-1","severity":"major","title":"still broken"}],"checks":[]}'
                        )
                    return SimpleNamespace(final_response="repair")

            orchestrator = FleetOrchestrator(
                driver=StuckDriver(),
                runtime=FakeRuntime(),
                checkpoint=CheckpointStore(root / ".arc" / "checkpoint.json"),
                requirements_dir=root / "requirements",
                output_dir=root,
                task_type="cli",
            )
            with mock.patch.dict("os.environ", {"HAFLEET_QUALITY_ON_EXHAUSTION": "pause"}, clear=False):
                with self.assertRaises(PauseRequested):
                    orchestrator._review_loop(self._module(1, "REQ-1"), "Review REQ-1")
            self.assertTrue(orchestrator.checkpoint.read()["paused"])

    def test_runs_combined_implementer_and_checkpoints_each_module(self) -> None:
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
            self.assertEqual(
                roles,
                [
                    "architect",
                    "implementer",
                    "reviewer",
                    "implementer",
                    "reviewer",
                    "reviewer",
                ],
            )
            self.assertEqual(checkpoint.read()["completed"], ["REQ-1", "REQ-2"])
            self.assertTrue(checkpoint.read()["architecture_completed"])
            self.assertEqual(len(runtime.git.messages), 4)
            implementer_prompt = next(prompt for role, prompt in driver.calls if role == "implementer")
            self.assertIn("architecture.md", implementer_prompt)
            self.assertIn("planning and implementation", implementer_prompt)
            self.assertIn("frontend/src/app.js", implementer_prompt)

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

            self.assertEqual(
                [role for role, _ in driver.calls],
                ["architect", "implementer", "reviewer", "reviewer"],
            )

    def test_force_final_review_reruns_integration_for_completed_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checkpoint = CheckpointStore(root / ".arc" / "checkpoint.json")
            checkpoint.mark_architecture_completed()
            checkpoint.mark_module_completed("REQ-1", 1)
            checkpoint.mark_final_review_completed()
            architecture = root / ".arc" / "hafleet" / "architecture.md"
            architecture.parent.mkdir(parents=True, exist_ok=True)
            architecture.write_text("# Existing architecture\n", encoding="utf-8")
            module = RequirementModule(
                index=1,
                total=1,
                node_id="REQ-1",
                name="REQ-1",
                subtree={
                    "id": "REQ-1",
                    "children": [{"id": f"REQ-1.{index}"} for index in range(1, 5)],
                },
            )
            driver = FakeDriver()
            orchestrator = FleetOrchestrator(
                driver=driver,
                runtime=FakeRuntime(),
                checkpoint=checkpoint,
                requirements_dir=root / "requirements",
                output_dir=root,
                task_type="web",
            )

            with mock.patch.dict(
                "os.environ",
                {
                    "HAFLEET_FORCE_FINAL_REVIEW": "1",
                    "HAFLEET_POSTFLIGHT": "0",
                },
                clear=False,
            ):
                orchestrator.run([module])

            roles = [role for role, _ in driver.calls]
            self.assertEqual(roles, ["implementer", "reviewer"])
            integration_prompt = driver.calls[0][1]
            self.assertIn("gateway smoke suite", integration_prompt)
            self.assertIn("same account concurrently", integration_prompt)
            self.assertIn("collection projection atomicity", integration_prompt)
            review_prompt = driver.calls[1][1]
            self.assertIn("two isolated clients", review_prompt)
            self.assertIn("collection projection as a release gate", review_prompt)
            self.assertTrue(checkpoint.read()["final_review_completed"])

    def test_resume_reopens_legacy_completed_module_with_deferred_quality(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checkpoint = CheckpointStore(root / ".arc" / "checkpoint.json")
            checkpoint.mark_architecture_completed()
            architecture = root / ".arc" / "hafleet" / "architecture.md"
            architecture.parent.mkdir(parents=True, exist_ok=True)
            architecture.write_text("# Existing architecture\n", encoding="utf-8")
            checkpoint.mark_module_completed("REQ-1", 1)
            checkpoint.update_pipeline(
                "REQ-1",
                node="checkpoint",
                loop_status="deferred",
                quality_deferred=True,
                quality_exhaustion_reason="project verification exceeded budget",
            )
            driver = FakeDriver()
            orchestrator = FleetOrchestrator(
                driver=driver,
                runtime=FakeRuntime(),
                checkpoint=checkpoint,
                requirements_dir=root / "requirements",
                output_dir=root,
                task_type="cli",
            )

            with mock.patch.dict("os.environ", {"HAFLEET_POSTFLIGHT": "0"}, clear=False):
                orchestrator.run([self._module(1, "REQ-1")])

            roles = [role for role, _ in driver.calls]
            self.assertNotIn("implementer", roles)
            self.assertIn("reviewer", roles)
            self.assertEqual(checkpoint.read()["completed"], ["REQ-1"])
            self.assertEqual(checkpoint.read()["deferred_modules"], [])

    def test_resume_recovers_deferred_module_from_message_log_after_flag_was_cleared(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checkpoint = CheckpointStore(root / ".arc" / "checkpoint.json")
            checkpoint.mark_architecture_completed()
            architecture = root / ".arc" / "hafleet" / "architecture.md"
            architecture.parent.mkdir(parents=True, exist_ok=True)
            architecture.write_text("# Existing architecture\n", encoding="utf-8")
            checkpoint.mark_module_completed("REQ-1", 1)
            driver = FakeDriver()
            orchestrator = FleetOrchestrator(
                driver=driver,
                runtime=FakeRuntime(),
                checkpoint=checkpoint,
                requirements_dir=root / "requirements",
                output_dir=root,
                task_type="cli",
            )
            orchestrator._message(
                "checkpoint.created",
                "orchestrator",
                module=self._module(1, "REQ-1"),
                phase="checkpoint",
                payload={"committed": True, "quality_deferred": True},
            )
            # Starting a later module models the old behavior that erased the one
            # global quality_deferred flag before the process was interrupted.
            checkpoint.mark_module_started("REQ-2", "implement")
            self.assertFalse(checkpoint.read()["quality_deferred"])

            with mock.patch.dict("os.environ", {"HAFLEET_POSTFLIGHT": "0"}, clear=False):
                orchestrator.run([self._module(1, "REQ-1")])

            self.assertNotIn("implementer", [role for role, _ in driver.calls])
            self.assertIn("reviewer", [role for role, _ in driver.calls])
            self.assertEqual(checkpoint.read()["completed"], ["REQ-1"])
            self.assertEqual(checkpoint.read()["deferred_modules"], [])

    def test_completed_architecture_is_skipped_on_resume(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checkpoint = CheckpointStore(root / ".arc" / "checkpoint.json")
            checkpoint.mark_architecture_completed()
            architecture = root / ".arc" / "hafleet" / "architecture.md"
            architecture.parent.mkdir(parents=True, exist_ok=True)
            architecture.write_text("# Existing architecture\n", encoding="utf-8")
            driver = FakeDriver()
            orchestrator = FleetOrchestrator(
                driver=driver,
                runtime=FakeRuntime(),
                checkpoint=checkpoint,
                requirements_dir=root / "requirements",
                output_dir=root,
                task_type="cli",
            )
            with mock.patch.dict("os.environ", {"HAFLEET_POSTFLIGHT": "0"}, clear=False):
                orchestrator.run([self._module(1, "REQ-1"), self._module(2, "REQ-2")])

            self.assertNotIn("architect", [role for role, _ in driver.calls])

    def test_architect_failure_does_not_start_feature_modules(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            driver = FakeDriver()
            driver.run = lambda role, prompt: driver.calls.append((role, prompt)) if role == "architect" else (_ for _ in ()).throw(AssertionError("feature module started"))
            orchestrator = FleetOrchestrator(
                driver=driver,
                runtime=FakeRuntime(),
                checkpoint=CheckpointStore(root / ".arc" / "checkpoint.json"),
                requirements_dir=root / "requirements",
                output_dir=root,
                task_type="cli",
            )
            with self.assertRaisesRegex(RuntimeError, "architecture document"):
                orchestrator.run([self._module(1, "REQ-1")])

            self.assertFalse(orchestrator.checkpoint.read()["architecture_completed"])

    def test_resume_recovers_completed_architecture_before_restarting_agent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            template = Path(__file__).resolve().parents[1] / "template" / "web"
            shutil.copytree(template, root, dirs_exist_ok=True)
            driver = FakeDriver()
            checkpoint = CheckpointStore(root / ".arc" / "checkpoint.json")
            orchestrator = FleetOrchestrator(
                driver=driver,
                runtime=FakeRuntime(),
                checkpoint=checkpoint,
                requirements_dir=root / "requirements",
                output_dir=root,
                task_type="web",
            )
            orchestrator.requirement_tree = {"id": "ROOT", "children": []}
            orchestrator.architecture_path.parent.mkdir(parents=True, exist_ok=True)
            orchestrator.architecture_path.write_text("# Completed architecture\n", encoding="utf-8")
            orchestrator._message(
                "turn.completed",
                "architect",
                phase="architecture",
                payload={"response": "completed"},
            )

            orchestrator._run_architecture()

            self.assertTrue(checkpoint.read()["architecture_completed"])
            self.assertNotIn("architect", [role for role, _ in driver.calls])
            self.assertEqual(len(orchestrator.runtime.git.messages), 1)

    def test_architecture_pause_records_root_phase(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pause_path = root / ".arc" / "pause-request"
            pause_path.parent.mkdir(parents=True, exist_ok=True)
            pause_path.write_text("pause\n", encoding="utf-8")
            orchestrator = FleetOrchestrator(
                driver=FakeDriver(),
                runtime=FakeRuntime(),
                checkpoint=CheckpointStore(root / ".arc" / "checkpoint.json"),
                requirements_dir=root / "requirements",
                output_dir=root,
                task_type="cli",
            )
            with mock.patch.dict(
                "os.environ",
                {"ARCBENCH_PAUSE_REQUEST_PATH": str(pause_path)},
                clear=False,
            ):
                with self.assertRaises(PauseRequested):
                    orchestrator.run([self._module(1, "REQ-1")])

            state = orchestrator.checkpoint.read()
            self.assertTrue(state["paused"])
            self.assertEqual(state["current_node_id"], "ROOT")
            self.assertEqual(state["current_phase"], "architecture")

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

    def test_parallel_modules_use_worktrees_and_merge_back(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
            (root / ".gitignore").write_text(".arc/\n", encoding="utf-8")
            (root / "seed.txt").write_text("seed\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "seed"], cwd=root, check=True)

            driver = FakeDriver()
            checkpoint = CheckpointStore(root / ".arc" / "checkpoint.json")
            orchestrator = FleetOrchestrator(
                driver=driver,
                runtime=FakeRuntime(),
                checkpoint=checkpoint,
                requirements_dir=root / "requirements",
                output_dir=root,
                task_type="cli",
                parallel=True,
                max_workers=2,
            )
            with mock.patch.dict("os.environ", {"HAFLEET_POSTFLIGHT": "0"}, clear=False):
                orchestrator.run([self._module(1, "REQ-1"), self._module(2, "REQ-2")])

            self.assertTrue((root / "REQ-1.txt").is_file())
            self.assertTrue((root / "REQ-2.txt").is_file())
            self.assertTrue((root / ".arc" / "hafleet" / "plans" / "REQ-1.md").is_file())
            self.assertTrue((root / ".arc" / "hafleet" / "plans" / "REQ-2.md").is_file())
            self.assertFalse((root / ".arc" / "hafleet" / "worktrees" / "REQ-1").exists())
            self.assertTrue(checkpoint.read()["parallel_mode"])
            self.assertEqual(checkpoint.read()["completed"], ["REQ-1", "REQ-2"])


if __name__ == "__main__":
    unittest.main()
