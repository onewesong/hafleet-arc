from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from hafleet_arc.test_runner import discover_project_test_commands, has_project_tests


class ProjectTestDiscoveryTests(unittest.TestCase):
    def test_prefers_workspace_verification_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".arc" / "hafleet").mkdir(parents=True)
            (root / "backend").mkdir()
            (root / ".arc" / "hafleet" / "verification.json").write_text(
                json.dumps({
                    "commands": [
                        {"module_id": "REQ-1", "cwd": "backend", "command": ["node", "test-auth.mjs"]},
                        {"module_id": "REQ-2", "cwd": "backend", "command": ["node", "test-search.mjs"]},
                    ]
                }),
                encoding="utf-8",
            )
            commands = discover_project_test_commands(root, "REQ-1")
            self.assertEqual(commands[0][0], ["node", "test-auth.mjs"])
            self.assertEqual(commands[0][1], root / "backend")
            self.assertTrue(has_project_tests(root, "REQ-1"))
            self.assertEqual(len(discover_project_test_commands(root, "ROOT")), 2)

    def test_falls_back_to_conventional_backend_tests(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            backend = root / "backend"
            backend.mkdir()
            (backend / "test-auth.mjs").write_text("", encoding="utf-8")
            commands = discover_project_test_commands(root)
            self.assertEqual(commands[0][0], ["node", "test-auth.mjs"])

    def test_rejects_manifest_cwd_outside_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".arc" / "hafleet").mkdir(parents=True)
            (root / ".arc" / "hafleet" / "verification.json").write_text(
                json.dumps({"commands": [{"module_id": "ROOT", "cwd": "..", "command": ["node", "hidden-test.mjs"]}]}),
                encoding="utf-8",
            )
            self.assertEqual(discover_project_test_commands(root), [])


if __name__ == "__main__":
    unittest.main()
