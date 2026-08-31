from __future__ import annotations

import unittest

from hafleet_arc.capabilities import build_capability_model


class CapabilityModelTests(unittest.TestCase):
    def test_normalizes_nested_requirements_and_scenarios(self) -> None:
        model = build_capability_model(
            {
                "id": "REQ-2",
                "name": "Search",
                "acceptance_criteria": ["empty query is rejected"],
                "scenarios": [{"id": "SC-1", "name": "valid search", "description": "See ![screen](./reference/search.png)", "steps": [{"keyword": "GIVEN", "content": "the form is visible"}, {"keyword": "THEN", "content": "results are shown"}]}],
                "children": [{"id": "REQ-2.1", "description": "Persist recent searches", "scenarios": []}],
            }
        )
        self.assertEqual([item["id"] for item in model["requirements"]], ["REQ-2", "REQ-2.1"])
        self.assertEqual(model["requirements"][0]["scenarios"][0]["id"], "SC-1")
        scenario = model["requirements"][0]["scenarios"][0]
        self.assertEqual(scenario["steps"][0]["action"], "the form is visible")
        self.assertEqual(scenario["steps"][1]["expected"], "results are shown")
        self.assertEqual(scenario["references"], ["./reference/search.png"])
        self.assertIn("empty query", model["requirements"][0]["acceptance"][0])
        contract = model["requirements"][0]["observable_contract"]
        self.assertIn("success", contract)
        self.assertIn("navigation_and_refresh", contract)

    def test_model_is_requirement_only_and_has_generic_quality_rules(self) -> None:
        model = build_capability_model({"id": "ROOT", "children": [{"id": "A", "title": "A"}]})
        self.assertEqual(model["source"], "arc_requirement_tree")
        self.assertTrue(any("hidden" in rule for rule in model["coverage_rules"]))
        self.assertNotIn("tests", model)


if __name__ == "__main__":
    unittest.main()
