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
            self.assertEqual(started["contract_review_status"], "")
            self.assertEqual(started["carried_contract_findings"], [])

            store.update_pipeline(
                "REQ-1",
                node="contract_review",
                contract_review_status="approved",
                contract_review_round=2,
                last_contract_feedback_message_id="msg-2",
            )
            reviewed = store.read()
            self.assertEqual(reviewed["contract_review_status"], "approved")
            self.assertEqual(reviewed["contract_review_round"], 2)

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

    def test_contract_obligations_are_persisted_per_module(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = CheckpointStore(Path(temporary) / "checkpoint.json")
            store.set_contract_obligations("REQ-1", [{"id": "C-1", "severity": "major"}])
            store.set_contract_obligations("REQ-2", [{"id": "C-2", "severity": "major"}])
            self.assertEqual([item["id"] for item in store.contract_obligations("REQ-1")], ["C-1"])
            self.assertEqual([item["id"] for item in store.contract_obligations("REQ-2")], ["C-2"])
            store.set_contract_obligations("REQ-1", [])
            self.assertEqual(store.contract_obligations("REQ-1"), [])
            self.assertEqual([item["id"] for item in store.contract_obligations("REQ-2")], ["C-2"])

    def test_contract_obligations_require_explicit_id_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = CheckpointStore(Path(temporary) / "checkpoint.json")
            store.set_contract_obligations(
                "REQ-1",
                [{"id": "C-1"}, {"id": "C-2"}],
            )
            remaining = store.resolve_contract_obligations(["C-1"], module_id="REQ-1")
            self.assertEqual([item["id"] for item in remaining["REQ-1"]], ["C-2"])
            self.assertEqual([item["id"] for item in store.contract_obligations("REQ-1")], ["C-2"])

    def test_architecture_completion_is_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = CheckpointStore(Path(temporary) / "checkpoint.json")
            store.mark_architecture_completed()
            self.assertTrue(store.read()["architecture_completed"])

    def test_deferred_module_is_not_completed_and_can_later_be_approved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = CheckpointStore(Path(temporary) / "checkpoint.json")
            store.mark_module_completed("REQ-1", 1)
            deferred = store.mark_module_deferred("REQ-1", 1)
            self.assertEqual(deferred["completed"], [])
            self.assertEqual(deferred["deferred_modules"], ["REQ-1"])
            self.assertEqual(deferred["current_node_id"], "REQ-1")

            approved = store.mark_module_completed("REQ-1", 1)
            self.assertEqual(approved["completed"], ["REQ-1"])
            self.assertEqual(approved["deferred_modules"], [])

    def test_whole_project_gate_resolves_all_deferred_modules(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = CheckpointStore(Path(temporary) / "checkpoint.json")
            store.mark_module_deferred("REQ-1", 1)
            store.mark_module_deferred("REQ-2", 2)
            resolved = store.resolve_all_deferred_modules()
            self.assertEqual(resolved["completed"], ["REQ-1", "REQ-2"])
            self.assertEqual(resolved["deferred_modules"], [])
            self.assertFalse(resolved["quality_deferred"])


if __name__ == "__main__":
    unittest.main()
