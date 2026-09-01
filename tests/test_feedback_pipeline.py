from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from hafleet_arc.feedback import blocking_findings, parse_review, review_hash, review_passes, test_hash, test_passes
from hafleet_arc.pipeline import DEFAULT_PIPELINE_PATH, load_pipeline


class FeedbackPipelineTests(unittest.TestCase):
    def test_structured_review_and_blocking_findings(self) -> None:
        review = parse_review('```json\n{"verdict":"changes_requested","summary":"fix it","findings":[{"severity":"major","title":"bad"},{"severity":"minor","title":"nit"}],"checks":[{"name":"build","status":"passed"}]}\n```')
        self.assertEqual(len(blocking_findings(review)), 1)
        self.assertFalse(review_passes(review))
        self.assertTrue(review_hash(review))

    def test_pass_requires_no_blocking_findings_and_no_failed_checks(self) -> None:
        self.assertTrue(review_passes(parse_review('{"verdict":"pass","findings":[],"checks":[{"status":"passed"}]}')))
        self.assertFalse(review_passes(parse_review('{"verdict":"pass","findings":[],"checks":[{"status":"failed"}]}')))

    def test_non_blocking_findings_do_not_block_review(self) -> None:
        review = parse_review(
            '{"verdict":"changes_requested","findings":[{"severity":"minor","title":"polish"}],"checks":[{"status":"passed"}]}'
        )
        self.assertTrue(review_passes(review))

    def test_passed_check_with_minor_gaps_does_not_block_review(self) -> None:
        review = parse_review(
            '{"verdict":"pass","findings":[{"severity":"minor","title":"coverage follow-up"}],'
            '"checks":[{"name":"assertion quality","status":"passed_with_minor_gaps"}]}'
        )
        self.assertTrue(review_passes(review))

    def test_optional_skipped_check_does_not_fail_test_result(self) -> None:
        self.assertTrue(test_passes({
            "verdict": "pass",
            "findings": [],
            "tests": [],
            "checks": [{"name": "optional compatibility probe", "status": "skipped", "output": "not implemented"}],
        }))

    def test_test_hash_ignores_volatile_command_output(self) -> None:
        left = {"verdict": "changes_requested", "tests": [], "findings": [], "checks": [{"name": "browser", "status": "failed", "output": "duration 1.2s"}]}
        right = {"verdict": "changes_requested", "tests": [], "findings": [], "checks": [{"name": "browser", "status": "failed", "output": "duration 9.8s"}]}
        self.assertEqual(test_hash(left), test_hash(right))

    def test_default_and_custom_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertTrue(DEFAULT_PIPELINE_PATH.is_file())
            default = load_pipeline(root)
            self.assertEqual(default.loop().max_rounds, 3)
            self.assertEqual(default.loop().options["review_strategy"], "full_then_incremental")
            config = root / ".arc" / "hafleet" / "pipeline.yaml"
            config.parent.mkdir(parents=True)
            config.write_text(
                "version: 1\n"
                "nodes:\n"
                "  - {id: planner, type: agent, role: planner}\n"
                "  - {id: implementer, type: agent, role: implementer}\n"
                "  - {id: loop, type: loop, review: reviewer, repair: implementer, max_rounds: 5}\n",
                encoding="utf-8",
            )
            configured = load_pipeline(root)
            self.assertEqual(configured.loop().max_rounds, 5)
            config.write_text("version: 1\nroles:\n  reviewer: Custom review prompt\n", encoding="utf-8")
            role_only = load_pipeline(root)
            self.assertEqual(role_only.loop().max_rounds, 3)
            self.assertEqual(role_only.prompt_for("reviewer"), "Custom review prompt")

            config.write_text(
                "version: 1\n"
                "nodes:\n"
                "  - {id: loop, type: loop, review: reviewer, repair: implementer, review_strategy: unknown}\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "unsupported review strategy"):
                load_pipeline(root)

    def test_default_pipeline_folds_planning_into_implementer(self) -> None:
        pipeline = load_pipeline(Path("/tmp/nonexistent-hafleet-output"))
        self.assertIsNone(pipeline.node("planner"))
        self.assertIsNone(pipeline.node("tester"))
        self.assertIsNone(pipeline.node("final_test"))
        self.assertIn("planning and implementation", pipeline.prompt_for("implementer"))
        self.assertIn("test authoring", pipeline.prompt_for("implementer"))
        self.assertIn("protected projection", pipeline.prompt_for("implementer"))
        self.assertIn("native select/radio controls", pipeline.prompt_for("implementer"))
        self.assertIn("test cases", pipeline.prompt_for("reviewer"))
        self.assertIn("Do not execute test commands", pipeline.prompt_for("reviewer"))
        self.assertIn("non-deletable current-user/account-holder row", pipeline.prompt_for("reviewer"))


if __name__ == "__main__":
    unittest.main()
