from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

from hafleet_arc.dashboard import DashboardServer
from hafleet_arc.dashboard.server import DashboardCollector
from hafleet_arc.dashboard.server import _git_snapshot


class DashboardTests(unittest.TestCase):
    def test_git_snapshot_exposes_file_level_worktree_and_commit_diffs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            def git(*args: str) -> None:
                subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
            git("init", "-q")
            git("config", "user.email", "test@example.com")
            git("config", "user.name", "Dashboard Test")
            (root / "tracked.txt").write_text("before\n", encoding="utf-8")
            git("add", "tracked.txt")
            git("commit", "-qm", "initial")
            (root / "tracked.txt").write_text("after\n", encoding="utf-8")
            (root / "new.txt").write_text("new line\n", encoding="utf-8")
            snapshot = _git_snapshot(str(root))
            paths = {(item["source"], item["path"]): item for item in snapshot["file_changes"]}
            self.assertIn(("working_tree", "tracked.txt"), paths)
            self.assertIn(("working_tree", "new.txt"), paths)
            self.assertIn("after", paths[("working_tree", "tracked.txt")]["diff"])
            self.assertIn("new line", paths[("working_tree", "new.txt")]["diff"])
            (root / "committed.txt").write_text("committed\n", encoding="utf-8")
            git("add", "committed.txt")
            git("commit", "-qm", "second")
            committed = _git_snapshot(str(root))["file_changes"]
            self.assertTrue(any(item["source"] == "latest_commit" and item["path"] == "committed.txt" for item in committed))

    def test_collector_reads_events_checkpoint_and_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            arc = root / ".arc"
            sessions = arc / "hafleet" / "codex-home" / "sessions" / "2026"
            sessions.mkdir(parents=True)
            (arc / "checkpoint.json").write_text(
                json.dumps({"completed": ["REQ-1"], "current_phase": "implement"}), encoding="utf-8"
            )
            (arc / "runner-events.jsonl").write_text(
                json.dumps({"type": "runner_state", "state": "running"})
                + "\n"
                + json.dumps(
                    {
                        "type": "requirement_state",
                        "node_id": "REQ-1",
                        "phase": "implement",
                        "status": "running",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            session = sessions / "rollout-session-1.jsonl"
            session.write_text(
                json.dumps(
                    {
                        "timestamp": "2026-01-01T00:00:00Z",
                        "type": "session_meta",
                        "payload": {
                            "session_id": "session-1",
                            "cwd": str(root),
                            "base_instructions": {"text": "You are the implementation agent."},
                        },
                    }
                )
                + "\n"
                + json.dumps(
                    {
                        "timestamp": "2026-01-01T00:00:00.250Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "developer",
                            "content": [{"text": "Module: 1/2 - REQ-1 - Demo"}],
                        },
                    }
                )
                + "\n"
                + json.dumps(
                    {
                        "timestamp": "2026-01-01T00:00:00.500Z",
                        "type": "event_msg",
                        "payload": {"type": "agent_message", "message": "Inspecting files."},
                    }
                )
                + "\n"
                + json.dumps(
                    {
                        "timestamp": "2026-01-01T00:00:00.501Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"text": "Inspecting files."}],
                        },
                    }
                )
                + "\n"
                + json.dumps(
                    {
                        "timestamp": "2026-01-01T00:00:00.750Z",
                        "type": "response_item",
                        "payload": {"type": "custom_tool_call", "name": "exec", "input": "ls"},
                    }
                )
                + "\n"
                + json.dumps(
                    {
                        "timestamp": "2026-01-01T00:00:01Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "input_text", "text": "Implemented the module."}],
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            collector = DashboardCollector(root)
            state = collector.state()
            self.assertEqual(state["runner"]["state"], "running")
            self.assertEqual(state["modules"][0]["node_id"], "REQ-1")
            self.assertEqual(state["characters"][2]["module_id"], "REQ-1")
            self.assertEqual(state["sessions"][0]["role"], "implementer")
            detail = collector.session("session-1")
            self.assertIsNotNone(detail)
            self.assertTrue(any("Implemented" in message["content"] for message in detail["messages"]))
            self.assertTrue(any(message["role"] == "tool" for message in detail["messages"]))
            self.assertTrue(any(message["content"] == "Inspecting files." for message in detail["messages"]))
            self.assertEqual(sum(message["content"] == "Inspecting files." for message in detail["messages"]), 1)
            self.assertEqual(detail["module_id"], "REQ-1")
            self.assertIn("diff", detail)
            self.assertIn("commit_diff", detail)

    def test_server_exposes_state_and_session_detail(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            server = DashboardServer(root, port=0).start()
            try:
                port = server.httpd.server_address[1]
                with urlopen(f"http://127.0.0.1:{port}/api/state") as response:
                    state = json.loads(response.read())
                self.assertEqual(state["output_dir"], str(root.resolve()))
                with urlopen(f"http://127.0.0.1:{port}/") as response:
                    body = response.read()
                    self.assertTrue(
                        b"HAFleet Factory Floor" in body or b"HAFleet ARC Dashboard" in body
                    )
            finally:
                server.stop()

    def test_api_only_server_exposes_api_but_not_ui(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            server = DashboardServer(root, port=0, api_only=True).start()
            try:
                port = server.httpd.server_address[1]
                with urlopen(f"http://127.0.0.1:{port}/api/state") as response:
                    self.assertEqual(response.status, 200)
                with self.assertRaises(HTTPError) as error:
                    urlopen(f"http://127.0.0.1:{port}/")
                self.assertEqual(error.exception.code, 404)
            finally:
                server.stop()

    def test_dashboard_frontend_manifest_and_vite_config(self) -> None:
        frontend = Path(__file__).parents[1] / "hafleet_arc" / "dashboard" / "frontend"
        package = json.loads((frontend / "package.json").read_text(encoding="utf-8"))
        scripts = package["scripts"]
        self.assertEqual(scripts["dev"], "vite")
        self.assertEqual(scripts["build"], "vite build")
        self.assertEqual(scripts["preview"], "vite preview")
        vite_config = (frontend / "vite.config.js").read_text(encoding="utf-8")
        self.assertIn("127.0.0.1:3200", vite_config)
        self.assertIn('process.env.DASHBOARD_API_URL', vite_config)
        self.assertIn('process.env.VITE_PORT', vite_config)
        app = (frontend / ".." / "static" / "app.js").resolve().read_text(encoding="utf-8")
        styles = (frontend / ".." / "static" / "styles.css").resolve().read_text(encoding="utf-8")
        self.assertIn("file_changes", app)
        self.assertIn("file-change-trigger", app)
        self.assertIn("file-change-diff", styles)
        self.assertIn('diff-line ${kind}', app)
        self.assertIn(".diff-line.removed", styles)
        self.assertIn("conversation-message-", app)

    def test_role_task_cards_keep_completed_module_history(self) -> None:
        modules = [
            {"node_id": "REQ-1", "phase": "implement", "status": "completed"},
            {"node_id": "REQ-2", "phase": "implement", "status": "completed"},
        ]
        events = [
            {"type": "requirement_state", "node_id": "REQ-1", "phase": "design", "status": "completed"},
            {"type": "requirement_state", "node_id": "REQ-1", "phase": "implement", "status": "completed"},
            {"type": "requirement_state", "node_id": "REQ-2", "phase": "design", "status": "completed"},
            {"type": "requirement_state", "node_id": "REQ-2", "phase": "implement", "status": "completed"},
        ]
        tasks = DashboardCollector._module_tasks(events, modules, {})
        self.assertEqual([item["node_id"] for item in tasks["planner"]], ["REQ-1", "REQ-2"])
        self.assertEqual([item["node_id"] for item in tasks["reviewer"]], ["REQ-1", "REQ-2"])

    def test_architect_stays_done_after_pipeline_moves_to_feature_module(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            arc = root / ".arc"
            arc.mkdir()
            (arc / "checkpoint.json").write_text(
                json.dumps(
                    {
                        "architecture_completed": True,
                        "current_node_id": "REQ-1",
                        "current_phase": "implement",
                    }
                ),
                encoding="utf-8",
            )
            (arc / "runner-events.jsonl").write_text(
                json.dumps({"type": "runner_state", "state": "running"})
                + "\n"
                + json.dumps(
                    {
                        "type": "requirement_state",
                        "node_id": "REQ-1",
                        "phase": "implement",
                        "status": "running",
                        "message": "HAFleet implementer started",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            characters = DashboardCollector(root).state()["characters"]
            by_id = {item["id"]: item for item in characters}
            self.assertEqual(by_id["architect"]["status"], "success")
            self.assertEqual(by_id["architect"]["message"], "Architecture scaffold completed")
            self.assertEqual(by_id["planner"]["status"], "success")
            self.assertEqual(by_id["implementer"]["status"], "working")


if __name__ == "__main__":
    unittest.main()
