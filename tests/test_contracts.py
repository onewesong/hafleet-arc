from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from hafleet_arc.contracts import contract_gaps, ensure_contract_file, scenario_contracts


class ScenarioContractTests(unittest.TestCase):
    def _subtree(self) -> dict[str, object]:
        return {
            "id": "REQ-1",
            "children": [
                {
                    "id": "REQ-1.1",
                    "scenarios": [
                        {
                            "name": "Valid flow",
                            "steps": [
                                {"keyword": "GIVEN", "content": "A user is signed in."},
                                {"keyword": "WHEN", "content": "Submit the form."},
                                {"keyword": "THEN", "content": "The saved record is visible."},
                            ],
                        },
                        {
                            "name": "Rejected flow",
                            "steps": [
                                {"keyword": "GIVEN", "content": "The invalid data is entered."},
                                {"keyword": "WHEN", "content": "Submit the form."},
                                {"keyword": "THEN", "content": "A validation error is visible."},
                            ],
                        },
                    ],
                }
            ],
        }

    def test_builds_one_stable_contract_row_per_public_scenario(self) -> None:
        rows = scenario_contracts(self._subtree())
        self.assertEqual([row["scenario_id"] for row in rows], ["REQ-1.1-S001", "REQ-1.1-S002"])
        self.assertEqual(rows[0]["given"], ["A user is signed in."])
        self.assertEqual(rows[0]["when"], ["Submit the form."])
        self.assertEqual(rows[0]["then"], ["The saved record is visible."])

    def test_contract_validator_requires_planned_files_checks_and_assertions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "REQ-1.json"
            payload = ensure_contract_file(path, "REQ-1", self._subtree())
            self.assertEqual(len(contract_gaps(payload, self._subtree())), 12)
            for row in payload["scenarios"]:
                row["planned_files"] = ["src/app.js"]
                row["observable_checks"] = ["The public result is visible."]
                row["canonical_url"] = "/records"
                row["durable_state"] = "The record remains visible after refresh."
                row["test_id"] = "T-" + row["scenario_id"]
                row["assertions"] = ["Assert the visible result."]
            path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertEqual(contract_gaps(ensure_contract_file(path, "REQ-1", self._subtree()), self._subtree()), [])

    def test_contract_validator_rejects_semantic_placeholders_and_duplicate_test_ids(self) -> None:
        payload = {"scenarios": scenario_contracts(self._subtree())}
        for row in payload["scenarios"]:
            row["planned_files"] = ["src/TODO.js"]
            row["observable_checks"] = ["The result is visible."]
            row["canonical_url"] = "/records/YYYY-MM-DD|/records/..."
            row["durable_state"] = "not_applicable"
            row["test_id"] = "T-DUPLICATE"
            row["assertions"] = ["Assert the result."]
        payload["scenarios"][0]["then"] = ["A different outcome."]
        gaps = contract_gaps(payload, self._subtree())
        fields = [gap["field"] for gap in gaps]
        self.assertIn("then", fields)
        self.assertIn("canonical_url", fields)
        self.assertIn("planned_files", fields)
        self.assertEqual(fields.count("test_id"), 2)


if __name__ == "__main__":
    unittest.main()
