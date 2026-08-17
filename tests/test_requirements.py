from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from hafleet_arc.requirements import load_requirement_tree, plan_modules


class RequirementPlanningTests(unittest.TestCase):
    def test_loads_root_and_orders_direct_dependencies(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "requirements.yaml").write_text(
                """
id: ROOT
children:
  - id: REQ-2
    name: Second
    dependencies: [REQ-1]
  - id: REQ-1
    name: First
""".lstrip(),
                encoding="utf-8",
            )
            modules = plan_modules(load_requirement_tree(root))

        self.assertEqual([module.node_id for module in modules], ["REQ-1", "REQ-2"])
        self.assertEqual([module.index for module in modules], [1, 2])

    def test_unknown_descendant_dependency_preserves_source_order(self) -> None:
        tree = {
            "id": "ROOT",
            "children": [
                {"id": "REQ-1", "dependencies": ["REQ-9-1"]},
                {"id": "REQ-2"},
            ],
        }
        self.assertEqual([item.node_id for item in plan_modules(tree)], ["REQ-1", "REQ-2"])

    def test_cycle_falls_back_to_source_order(self) -> None:
        tree = {
            "id": "ROOT",
            "children": [
                {"id": "REQ-1", "dependencies": ["REQ-2"]},
                {"id": "REQ-2", "dependencies": ["REQ-1"]},
            ],
        }
        self.assertEqual([item.node_id for item in plan_modules(tree)], ["REQ-1", "REQ-2"])

    def test_rejects_non_root_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "requirements.yaml").write_text("id: REQ-1\nchildren: []\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "ROOT mapping"):
                load_requirement_tree(root)


if __name__ == "__main__":
    unittest.main()
