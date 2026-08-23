from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from hafleet_arc.checkpoint import CheckpointStore


class CheckpointStoreTests(unittest.TestCase):
    def test_tracks_started_completed_and_paused_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = CheckpointStore(Path(temporary) / ".arc" / "checkpoint.json")
            store.mark_module_started("REQ-1", "design")
            started = store.read()
            self.assertEqual(started["current_node_id"], "REQ-1")
            self.assertEqual(started["current_phase"], "design")

            store.mark_module_completed("REQ-1", 1)
            completed = store.read()
            self.assertEqual(completed["completed"], ["REQ-1"])
            self.assertEqual(completed["last_completed_index"], 1)

            store.mark_paused("REQ-2", "review")
            paused = store.read()
            self.assertTrue(paused["paused"])
            self.assertEqual(paused["current_node_id"], "REQ-2")

    def test_corrupt_checkpoint_uses_default(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "checkpoint.json"
            path.write_text("not-json", encoding="utf-8")
            state = CheckpointStore(path).read()
            self.assertEqual(state["completed"], [])
            self.assertFalse(state["architecture_completed"])

    def test_architecture_completion_is_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = CheckpointStore(Path(temporary) / "checkpoint.json")
            store.mark_architecture_completed()
            self.assertTrue(store.read()["architecture_completed"])


if __name__ == "__main__":
    unittest.main()
