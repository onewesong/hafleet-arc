from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from hafleet_arc.feedback import blocking_findings, parse_review, review_hash, review_passes
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

    def test_default_and_custom_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertTrue(DEFAULT_PIPELINE_PATH.is_file())
            default = load_pipeline(root)
            self.assertEqual(default.loop().max_rounds, 3)
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


if __name__ == "__main__":
    unittest.main()
