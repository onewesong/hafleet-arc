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

    def test_derives_gateway_contracts_from_repeated_public_preconditions(self) -> None:
        model = build_capability_model({
            "id": "ROOT",
            "children": [
                {
                    "id": "PROFILE",
                    "scenarios": [
                        {"id": "P-1", "steps": [{"keyword": "GIVEN", "content": "The user is logged in."}]},
                        {"id": "P-2", "steps": [{"keyword": "GIVEN", "content": "the  user is LOGGED in"}]},
                    ],
                },
                {
                    "id": "ORDERS",
                    "scenarios": [
                        {"id": "O-1", "steps": [{"keyword": "GIVEN", "content": "The user is logged in"}]},
                    ],
                },
            ],
        })
        self.assertEqual(len(model["gateway_contracts"]), 1)
        gateway = model["gateway_contracts"][0]
        self.assertEqual(gateway["consumer_count"], 3)
        self.assertEqual(gateway["requirement_ids"], ["PROFILE", "ORDERS"])
        self.assertTrue(gateway["release_gate"])
        self.assertTrue(any("canonical URL" in rule for rule in gateway["verification_contract"]))
        self.assertTrue(any("two isolated clients" in rule for rule in gateway["verification_contract"]))

    def test_web_contract_requires_independent_concurrent_sessions(self) -> None:
        model = build_capability_model({"id": "ROOT"})
        state_contract = "\n".join(model["web_contract"]["state"])
        coverage = "\n".join(model["coverage_rules"])
        self.assertIn("must not invalidate another active client session", state_contract)
        self.assertIn("two isolated clients", state_contract)
        self.assertIn("later login silently invalidates an earlier", coverage)

    def test_web_contract_requires_atomic_collection_projections(self) -> None:
        model = build_capability_model({"id": "ROOT"})
        collections = "\n".join(model["web_contract"]["collections"])
        forms = "\n".join(model["web_contract"]["forms"])
        coverage = "\n".join(model["coverage_rules"])
        self.assertIn("stale rows must be hidden", collections)
        self.assertIn("every visible data item", collections)
        self.assertIn("semantic list item/article/card", collections)
        self.assertIn("protected row", collections)
        self.assertIn("deduplicate legacy persisted owner rows", collections)
        self.assertIn("enumerable choices", forms)
        self.assertIn("accessible region name", forms)
        self.assertIn("one atomic projection", coverage)
        self.assertIn("persisted owner copies", coverage)

    def test_derives_state_interference_gate_from_public_mutations(self) -> None:
        model = build_capability_model({
            "id": "ROOT",
            "children": [
                {
                    "id": "ACCOUNT",
                    "name": "Change password",
                    "scenarios": [{
                        "id": "ACCOUNT-S1",
                        "name": "Save a new password",
                        "steps": [
                            {"keyword": "WHEN", "content": "the user changes the password"},
                            {"keyword": "THEN", "content": "the account is updated"},
                        ],
                    }],
                },
                {
                    "id": "ITEMS",
                    "name": "Delete a saved item",
                    "scenarios": [{"id": "ITEMS-S1", "name": "Remove one item"}],
                },
            ],
        })
        contract = model["state_interference_contract"]
        self.assertTrue(contract["release_gate"])
        self.assertIn("ACCOUNT", contract["mutable_requirement_ids"])
        self.assertIn("ACCOUNT", contract["credential_requirement_ids"])
        self.assertIn("ITEMS", contract["destructive_requirement_ids"])
        self.assertIn("ACCOUNT-S1", contract["scenario_ids"])
        rules = "\n".join(contract["verification_contract"])
        self.assertIn("two isolated clients", rules)
        self.assertIn("Run destructive scenarios", rules)
        self.assertIn("atomic persistence", rules)


if __name__ == "__main__":
    unittest.main()
