from __future__ import annotations

import unittest

from hafleet_arc.capabilities import build_capability_model


class CapabilityModelTests(unittest.TestCase):
    def test_normalizes_nested_requirements_and_scenarios(self) -> None:
        model = build_capability_model(
            {
                "id": "REQ-2",
                "name": "Search",
                "dependencies": ["REQ-1"],
                "acceptance_criteria": ["empty query is rejected; show 'Please enter a query.'"],
                "scenarios": [{"id": "SC-1", "name": "valid search", "description": "See ![screen](./reference/search.png)", "steps": [{"keyword": "GIVEN", "content": "the form is visible"}, {"keyword": "THEN", "content": "results are shown"}]}],
                "children": [{"id": "REQ-2.1", "description": "Persist recent searches", "scenarios": []}],
            }
        )
        self.assertEqual([item["id"] for item in model["requirements"]], ["REQ-2", "REQ-2.1"])
        self.assertEqual(model["requirements"][0]["dependencies"], ["REQ-1"])
        self.assertEqual(model["requirements"][0]["scenarios"][0]["id"], "SC-1")
        scenario = model["requirements"][0]["scenarios"][0]
        self.assertEqual(scenario["steps"][0]["action"], "the form is visible")
        self.assertEqual(scenario["steps"][1]["expected"], "results are shown")
        self.assertEqual(scenario["references"], ["./reference/search.png"])
        self.assertEqual(scenario["transition"]["preconditions"], ["the form is visible"])
        self.assertEqual(scenario["transition"]["observable_results"], ["results are shown"])
        self.assertIn("empty query", model["requirements"][0]["acceptance"][0])
        self.assertIn("Please enter a query.", model["requirements"][0]["observable_strings"])
        contract = model["requirements"][0]["observable_contract"]
        self.assertIn("success", contract)
        self.assertIn("navigation_and_refresh", contract)
        self.assertIn("ui_api_parity", contract)

    def test_model_is_requirement_only_and_has_generic_quality_rules(self) -> None:
        model = build_capability_model({"id": "ROOT", "children": [{"id": "A", "title": "A"}]})
        self.assertEqual(model["source"], "arc_requirement_tree")
        self.assertTrue(any("hidden" in rule for rule in model["coverage_rules"]))
        self.assertNotIn("tests", model)
        web = model["web_contract"]
        self.assertTrue(any("canonical spelling" in rule for rule in web["routing"]))
        self.assertTrue(any("API boundary" in rule for rule in web["forms"]))
        self.assertTrue(any("retry states" in rule for rule in web["state"]))

    def test_preserves_root_seed_contracts_and_dependency_impact(self) -> None:
        model = build_capability_model({
            "id": "ROOT",
            "data": [{"category": "Accounts", "items": ["The app contains a seeded user."]}],
            "children": [
                {"id": "AUTH", "name": "Authentication"},
                {"id": "PROFILE", "dependencies": ["AUTH"]},
                {"id": "ORDERS", "dependencies": ["AUTH"]},
            ],
        })
        self.assertEqual(model["seed_contracts"][0]["category"], "Accounts")
        auth = next(item for item in model["requirements"] if item["id"] == "AUTH")
        self.assertEqual(auth["dependent_count"], 2)
        self.assertTrue(auth["critical_prerequisite"])


if __name__ == "__main__":
    unittest.main()
