from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from urllib.request import urlopen

from hafleet_arc.dashboard import DashboardServer
from hafleet_arc.dashboard.server import DashboardCollector


class DashboardTests(unittest.TestCase):
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
            self.assertEqual(detail["module_id"], "REQ-1")

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
                    self.assertIn(b"HAFleet Factory Floor", response.read())
            finally:
                server.stop()

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
