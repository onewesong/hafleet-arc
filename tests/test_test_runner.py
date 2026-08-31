from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from hafleet_arc.test_runner import discover_project_test_commands, has_project_tests, run_project_tests


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

    def test_infers_self_hosted_backend_contract_test(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            backend = root / "backend"
            backend.mkdir()
            test_path = backend / "contracts.test.js"
            test_path.write_text(
                'import { spawn } from "node:child_process";\nspawn("node", ["server.js"], { env: { PORT: "3100" } });\n',
                encoding="utf-8",
            )
            (root / ".arc" / "hafleet").mkdir(parents=True)
            (root / ".arc" / "hafleet" / "verification.json").write_text(
                json.dumps({"commands": [{"module_id": "REQ-1", "cwd": "backend", "command": ["node", "contracts.test.js"]}]}),
                encoding="utf-8",
            )
            self.assertEqual(discover_project_test_commands(root, "REQ-1")[0][3], "self")

    def test_infers_self_hosted_test_referenced_by_package_script(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            frontend = root / "frontend"
            (frontend / "test").mkdir(parents=True)
            (frontend / "package.json").write_text(
                json.dumps({"scripts": {"test:home": "node --test test/home.test.js"}}),
                encoding="utf-8",
            )
            (frontend / "test" / "home.test.js").write_text(
                'import { spawn } from "node:child_process";\nspawn("node", ["../backend/server.js"], { env: { PORT: "3100" } });\n',
                encoding="utf-8",
            )
            (root / ".arc" / "hafleet").mkdir(parents=True)
            (root / ".arc" / "hafleet" / "verification.json").write_text(
                json.dumps({"commands": [{"module_id": "REQ-1", "cwd": "frontend", "command": ["npm", "run", "test:home"]}]}),
                encoding="utf-8",
            )
            self.assertEqual(discover_project_test_commands(root, "REQ-1")[0][3], "self")

    def test_self_hosted_command_owns_smoke_port(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            frontend = root / "frontend"
            backend = root / "backend"
            frontend.mkdir()
            backend.mkdir()
            (frontend / "package.json").write_text('{"private":true}\n', encoding="utf-8")
            (backend / "package.json").write_text(
                '{"private":true,"type":"module","scripts":{"start":"node server.js"}}\n', encoding="utf-8"
            )
            (backend / "server.js").write_text(
                'import http from "node:http"; const server=http.createServer((q,r)=>r.end("ok")); server.listen(Number(process.env.PORT));\n',
                encoding="utf-8",
            )
            (backend / "self-test.js").write_text(
                'import http from "node:http"; const server=http.createServer((q,r)=>r.end("ok")); server.on("error",()=>process.exit(1)); server.listen(31991,"127.0.0.1",()=>server.close(()=>process.exit(0)));\n',
                encoding="utf-8",
            )
            manifest = root / ".arc" / "hafleet" / "verification.json"
            manifest.parent.mkdir(parents=True)
            manifest.write_text(
                json.dumps({"commands": [{"module_id": "REQ-1", "cwd": "backend", "command": ["node", "self-test.js"], "server_mode": "self"}]}),
                encoding="utf-8",
            )

            result = run_project_tests(root, task_type="web", module_id="REQ-1", smoke_port=31991)

            self.assertEqual(result["verdict"], "pass")
            command_check = next(item for item in result["checks"] if item["name"] == "verification command 1")
            self.assertEqual(command_check["server_mode"], "self")

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
