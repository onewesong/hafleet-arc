from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from hafleet_arc.message_bus import MessageBus


class MessageBusTests(unittest.TestCase):
    def test_publish_replay_and_monotonic_sequence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "messages.jsonl"
            bus = MessageBus(path, run_id="run-test")
            first = bus.publish("turn.request", sender="orchestrator", recipient="planner", module_id="REQ-1")
            second = bus.publish("agent.message", sender="planner", recipient="orchestrator", module_id="REQ-1", parent_id=first["id"], payload={"response": "done"})
            self.assertLess(first["sequence"], second["sequence"])
            replay = bus.replay(after_sequence=first["sequence"])
            self.assertEqual([item["id"] for item in replay], [second["id"]])
            restarted = MessageBus(path, run_id="run-test")
            self.assertEqual(restarted.publish("pipeline.state", sender="orchestrator")["sequence"], second["sequence"] + 1)

    def test_subscribe_replays_and_filters_modules(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            bus = MessageBus(Path(temporary) / "messages.jsonl")
            bus.publish("agent.message", sender="planner", module_id="REQ-1")
            bus.publish("agent.message", sender="planner", module_id="REQ-2")
            messages = list(bus.replay(module_id="REQ-2"))
            self.assertEqual(len(messages), 1)
            self.assertEqual(messages[0]["module_id"], "REQ-2")


if __name__ == "__main__":
    unittest.main()
