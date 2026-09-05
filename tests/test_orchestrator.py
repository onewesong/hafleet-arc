from __future__ import annotations

import json
import tempfile
import unittest
import subprocess
from types import SimpleNamespace
from pathlib import Path
from unittest import mock

from hafleet_arc.checkpoint import CheckpointStore
from hafleet_arc.contracts import ensure_contract_file
from hafleet_arc.feedback import review_passes
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

    def test_reviewer_feedback_loops_back_to_implementer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            class LoopDriver:
                def __init__(self) -> None:
                    self.calls: list[str] = []

                def run(self, role: str, prompt: str, workspace_dir: Path | None = None):
                    self.calls.append(role)
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
            messages = orchestrator.bus.replay()
            self.assertTrue(any(item["kind"] == "review.feedback" for item in messages))
            self.assertTrue(any(item["kind"] == "pipeline.state" and item["payload"].get("status") == "approved" for item in messages))

    def test_module_review_requires_explicit_contract_obligation_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            class ObligationDriver:
                def __init__(self) -> None:
                    self.calls: list[str] = []

                def run(self, role: str, prompt: str, workspace_dir: Path | None = None):
                    self.calls.append(role)
                    if role == "reviewer" and self.calls.count("reviewer") > 1:
                        return SimpleNamespace(
                            final_response='{"verdict":"pass","findings":[],"checks":[],"resolved_finding_ids":["C-1"]}'
                        )
                    if role == "implementer":
                        (workspace_dir or root).joinpath("fixed.txt").write_text("fixed\n", encoding="utf-8")
                    return SimpleNamespace(final_response='{"verdict":"pass","findings":[],"checks":[]}')

            driver = ObligationDriver()
            checkpoint = CheckpointStore(root / ".arc" / "checkpoint.json")
            checkpoint.set_contract_obligations(
                "REQ-1",
                [{"id": "C-1", "severity": "major", "title": "Required behavior"}],
            )
            orchestrator = FleetOrchestrator(
                driver=driver,
                runtime=FakeRuntime(),
                checkpoint=checkpoint,
                requirements_dir=root / "requirements",
                output_dir=root,
                task_type="cli",
            )
            result = orchestrator._review_loop(self._module(1, "REQ-1"), "Review REQ-1")
            self.assertTrue(review_passes(result))
            self.assertEqual(driver.calls, ["reviewer", "implementer", "reviewer"])
            self.assertEqual(checkpoint.contract_obligations("REQ-1"), [])

    def test_final_checkpoint_is_withheld_while_contract_obligations_remain(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checkpoint = CheckpointStore(root / ".arc" / "checkpoint.json")
            checkpoint.mark_architecture_completed()
            checkpoint.mark_module_completed("REQ-1", 1)
            checkpoint.set_contract_obligations(
                "REQ-1",
                [{"id": "C-1", "severity": "major", "title": "Still open"}],
            )
            runtime = FakeRuntime()
            orchestrator = FleetOrchestrator(
                driver=FakeDriver(),
                runtime=runtime,
                checkpoint=checkpoint,
                requirements_dir=root / "requirements",
                output_dir=root,
                task_type="web",
            )
            with mock.patch.dict(
                "os.environ",
                {"HAFLEET_FINAL_REVIEW": "0"},
                clear=False,
            ), mock.patch.object(orchestrator, "_run_postflight", return_value=True):
                orchestrator.run([self._module(1, "REQ-1")])
            state = checkpoint.read()
            self.assertFalse(state["final_review_completed"])
            self.assertTrue(state["quality_deferred"])
            self.assertEqual([item["id"] for item in checkpoint.contract_obligations("REQ-1")], ["C-1"])
            self.assertNotIn("ROOT: final HAFleet integration review", runtime.git.messages)

    def test_project_owned_test_failure_loops_to_implementer_before_review(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            class Driver:
                def __init__(self) -> None:
                    self.calls: list[str] = []

                def run(self, role: str, prompt: str, workspace_dir: Path | None = None):
                    self.calls.append(role)
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
            self.assertTrue(any(item["kind"] == "test.failed" for item in orchestrator.bus.replay()))

    def test_project_verification_repairs_do_not_consume_reviewer_rounds(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            class Driver:
                def __init__(self) -> None:
                    self.calls: list[tuple[str, str]] = []
                    self.repairs = 0

                def run(self, role: str, prompt: str, workspace_dir: Path | None = None):
                    self.calls.append((role, prompt))
                    if role == "implementer":
                        self.repairs += 1
                        (workspace_dir or root).joinpath("repair.txt").write_text(
                            f"repair {self.repairs}\n", encoding="utf-8"
                        )
                    return SimpleNamespace(final_response='{"verdict":"pass","summary":"ok","findings":[],"checks":[]}')

            failed_one = {"verdict": "changes_requested", "summary": "first", "findings": [{"id": "T-1", "severity": "major", "title": "failed"}], "checks": []}
            failed_two = {"verdict": "changes_requested", "summary": "second", "findings": [{"id": "T-2", "severity": "major", "title": "failed"}], "checks": []}
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
                side_effect=[failed_one, failed_two, passed],
            ):
                result = orchestrator._review_loop(self._module(1, "REQ-1"), "Review REQ-1")

            self.assertEqual(result["verdict"], "pass")
            self.assertEqual([role for role, _ in driver.calls], ["implementer", "implementer", "reviewer"])
            reviewer_prompt = next(prompt for role, prompt in driver.calls if role == "reviewer")
            self.assertIn("Review loop round 1/2", reviewer_prompt)
            state = orchestrator.checkpoint.read()
            self.assertEqual(state["current_review_round"], 1)
            self.assertEqual(state["current_verification_attempt"], 3)

    def test_postflight_repairs_failed_final_registered_verification(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            driver = FakeDriver()
            checkpoint = CheckpointStore(root / ".arc" / "checkpoint.json")
            checkpoint.mark_module_deferred("REQ-1", 1)
            orchestrator = FleetOrchestrator(
                driver=driver,
                runtime=FakeRuntime(),
                checkpoint=checkpoint,
                requirements_dir=root / "requirements",
                output_dir=root,
                task_type="web",
            )
            failed = {"verdict": "changes_requested", "summary": "failed", "findings": [{"id": "T-ROOT", "severity": "major", "title": "failed"}], "checks": []}
            passed = {"verdict": "pass", "summary": "passed", "findings": [], "checks": []}
            with mock.patch.dict(
                "os.environ",
                {"HAFLEET_POSTFLIGHT": "1", "HAFLEET_POSTFLIGHT_REPAIRS": "1"},
                clear=False,
            ), mock.patch("hafleet_arc.orchestrator.rehearse_web_app"), mock.patch(
                "hafleet_arc.orchestrator.has_project_tests", return_value=True
            ), mock.patch.object(
                orchestrator,
                "_run_registered_project_tests",
                side_effect=[failed, passed],
            ):
                approved = orchestrator._run_postflight("REQ-1")

            self.assertTrue(approved)
            self.assertEqual([role for role, _ in driver.calls], ["implementer"])
            self.assertIn("earliest failing registered project test", driver.calls[0][1])
            state = checkpoint.read()
            # Postflight proves runtime/project-test health only. Deferred
            # modules are promoted later, after final Reviewer approval and
            # explicit contract-obligation resolution.
            self.assertEqual(state["deferred_modules"], ["REQ-1"])
            self.assertEqual(state["completed"], [])

    def test_postflight_withholds_approval_when_final_verification_stays_failed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            orchestrator = FleetOrchestrator(
                driver=FakeDriver(),
                runtime=FakeRuntime(),
                checkpoint=CheckpointStore(root / ".arc" / "checkpoint.json"),
                requirements_dir=root / "requirements",
                output_dir=root,
                task_type="web",
            )
            failed = {"verdict": "changes_requested", "summary": "failed", "findings": [{"id": "T-ROOT", "severity": "major", "title": "failed"}], "checks": []}
            with mock.patch.dict(
                "os.environ",
                {"HAFLEET_POSTFLIGHT": "1", "HAFLEET_POSTFLIGHT_REPAIRS": "1"},
                clear=False,
            ), mock.patch("hafleet_arc.orchestrator.rehearse_web_app"), mock.patch(
                "hafleet_arc.orchestrator.has_project_tests", return_value=True
            ), mock.patch.object(
                orchestrator,
                "_run_registered_project_tests",
                side_effect=[failed, failed],
            ):
                approved = orchestrator._run_postflight("REQ-1")

            self.assertFalse(approved)
            state = orchestrator.checkpoint.read()
            self.assertTrue(state["quality_deferred"])
            self.assertFalse(state["final_review_completed"])

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

    def test_contract_review_approves_plan_before_same_implementer_writes_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            module = RequirementModule(
                1,
                1,
                "REQ-1",
                "Demo",
                {
                    "id": "REQ-1",
                    "children": [
                        {
                            "id": "REQ-1.1",
                            "scenarios": [
                                {
                                    "name": "Show page",
                                    "steps": [
                                        {"keyword": "GIVEN", "content": "The app is available."},
                                        {"keyword": "WHEN", "content": "Open the page."},
                                        {"keyword": "THEN", "content": "The page is visible."},
                                    ],
                                }
                            ],
                        }
                    ],
                },
            )

            class ContractDriver:
                def __init__(self) -> None:
                    self.calls: list[tuple[str, str]] = []
                    self.contract_saw_source = False

                def run(self, role: str, prompt: str, workspace_dir: Path | None = None):
                    self.calls.append((role, prompt))
                    if role == "architect":
                        path = Path(prompt.split("Architecture document path: ", 1)[1].splitlines()[0])
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text("# Architecture\n", encoding="utf-8")
                    elif role == "implementer" and "Planning-only turn" in prompt:
                        plan = root / ".arc" / "hafleet" / "plans" / "REQ-1.md"
                        contract = root / ".arc" / "hafleet" / "contracts" / "REQ-1.json"
                        plan.parent.mkdir(parents=True, exist_ok=True)
                        plan.write_text("# Plan\n", encoding="utf-8")
                        payload = json.loads(contract.read_text(encoding="utf-8"))
                        for row in payload["scenarios"]:
                            row["planned_files"] = ["src/app.js"]
                            row["observable_checks"] = ["The public page is visible."]
                            row["canonical_url"] = "/"
                            row["durable_state"] = "not_applicable"
                            row["test_id"] = "T-REQ-1.1-S001"
                            row["assertions"] = ["Assert the page heading is visible."]
                        contract.write_text(json.dumps(payload), encoding="utf-8")
                    elif role == "reviewer" and "Pre-implementation contract review" in prompt:
                        self.contract_saw_source = (root / "src" / "app.js").exists()
                    elif role == "implementer" and "planning-only phase" in prompt.lower():
                        app = root / "src" / "app.js"
                        app.parent.mkdir(parents=True, exist_ok=True)
                        app.write_text("export const ready = true;\n", encoding="utf-8")
                    return SimpleNamespace(final_response='{"verdict":"pass","summary":"ok","findings":[],"checks":[]}')

            driver = ContractDriver()
            orchestrator = FleetOrchestrator(
                driver=driver,
                runtime=FakeRuntime(),
                checkpoint=CheckpointStore(root / ".arc" / "checkpoint.json"),
                requirements_dir=root / "requirements",
                output_dir=root,
                task_type="cli",
            )
            with mock.patch.dict(
                "os.environ",
                {
                    "HAFLEET_SELF_CHECK": "0",
                    "HAFLEET_COMPLETION_PASS": "0",
                    "HAFLEET_PROJECT_TESTS": "0",
                    "HAFLEET_FINAL_REVIEW": "0",
                    "HAFLEET_POSTFLIGHT": "0",
                },
                clear=False,
            ):
                orchestrator.run([module])

            roles = [role for role, _ in driver.calls]
            self.assertEqual(roles, ["architect", "implementer", "reviewer", "implementer", "reviewer"])
            self.assertFalse(driver.contract_saw_source)
            self.assertTrue((root / "src" / "app.js").is_file())
            state = orchestrator.checkpoint.read()
            self.assertEqual(state["contract_review_status"], "approved")
            self.assertTrue(any(item["kind"] == "contract.verdict" for item in orchestrator.bus.replay()))

    def test_contract_review_routes_incomplete_contract_back_to_implementer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            module = RequirementModule(
                1,
                1,
                "REQ-1",
                "Demo",
                {"id": "REQ-1", "scenarios": [{"name": "Show", "steps": [{"keyword": "THEN", "content": "Visible"}]}]},
            )
            plan = root / ".arc" / "hafleet" / "plans" / "REQ-1.md"
            contract = root / ".arc" / "hafleet" / "contracts" / "REQ-1.json"
            plan.parent.mkdir(parents=True, exist_ok=True)
            plan.write_text("# Plan\n", encoding="utf-8")

            class RepairDriver:
                def __init__(self) -> None:
                    self.calls: list[str] = []

                def run(self, role: str, prompt: str, workspace_dir: Path | None = None):
                    self.calls.append(role)
                    if role == "implementer":
                        payload = json.loads(contract.read_text(encoding="utf-8"))
                        for row in payload["scenarios"]:
                            row["planned_files"] = ["app.py"]
                            row["observable_checks"] = ["Visible"]
                            row["canonical_url"] = "/"
                            row["durable_state"] = "not_applicable"
                            row["test_id"] = "T-1"
                            row["assertions"] = ["Assert Visible"]
                        contract.write_text(json.dumps(payload), encoding="utf-8")
                    return SimpleNamespace(final_response='{"verdict":"pass","summary":"ok","findings":[],"checks":[]}')

            driver = RepairDriver()
            orchestrator = FleetOrchestrator(
                driver=driver,
                runtime=FakeRuntime(),
                checkpoint=CheckpointStore(root / ".arc" / "checkpoint.json"),
                requirements_dir=root / "requirements",
                output_dir=root,
                task_type="cli",
            )
            ensure_contract_file(contract, module.node_id, module.subtree)
            result = orchestrator._contract_review_loop(module, "Review", plan, contract)
            self.assertEqual(result["verdict"], "pass")
            self.assertEqual(driver.calls, ["reviewer", "implementer", "reviewer"])

    def test_contract_review_reconciles_last_feedback_and_carries_findings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            module = RequirementModule(
                1,
                1,
                "REQ-1",
                "Demo",
                {"id": "REQ-1", "scenarios": [{"name": "Show", "steps": [{"keyword": "THEN", "content": "Visible"}]}]},
            )
            plan = root / ".arc" / "hafleet" / "plans" / "REQ-1.md"
            contract = root / ".arc" / "hafleet" / "contracts" / "REQ-1.json"
            plan.parent.mkdir(parents=True, exist_ok=True)
            plan.write_text("# Plan\n", encoding="utf-8")
            payload = ensure_contract_file(contract, module.node_id, module.subtree)
            row = payload["scenarios"][0]
            row.update(
                {
                    "planned_files": ["app.py"],
                    "observable_checks": ["Visible"],
                    "canonical_url": "/",
                    "durable_state": "not_applicable",
                    "test_id": "T-1",
                    "assertions": ["Assert Visible"],
                }
            )
            contract.write_text(json.dumps(payload), encoding="utf-8")

            class StuckContractDriver:
                def __init__(self) -> None:
                    self.calls: list[str] = []

                def run(self, role: str, prompt: str, workspace_dir: Path | None = None):
                    self.calls.append(role)
                    if role == "reviewer":
                        return SimpleNamespace(final_response='{"verdict":"changes_requested","findings":[{"id":"C-1","severity":"major","title":"Missing branch"}],"checks":[]}')
                    return SimpleNamespace(final_response='{"summary":"reconciled"}')

            driver = StuckContractDriver()
            orchestrator = FleetOrchestrator(
                driver=driver,
                runtime=FakeRuntime(),
                checkpoint=CheckpointStore(root / ".arc" / "checkpoint.json"),
                requirements_dir=root / "requirements",
                output_dir=root,
                task_type="cli",
            )
            result = orchestrator._contract_review_loop(module, "Review", plan, contract)
            self.assertEqual(driver.calls, ["reviewer", "implementer", "reviewer", "implementer"])
            self.assertEqual(result["contract_status"], "deferred")
            self.assertEqual([item["id"] for item in result["carried_findings"]], ["C-1"])
            state = orchestrator.checkpoint.read()
            self.assertEqual(state["contract_review_status"], "deferred")
            self.assertEqual([item["id"] for item in state["carried_contract_findings"]], ["C-1"])

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

    def test_failed_final_verification_withholds_completion_without_crashing_run(self) -> None:
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
            with mock.patch.dict(
                "os.environ",
                {"HAFLEET_FINAL_REVIEW": "0"},
                clear=False,
            ), mock.patch.object(orchestrator, "_run_postflight", return_value=False):
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
