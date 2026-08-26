from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any


class CheckpointStore:
    _write_lock = threading.RLock()

    def __init__(self, path: Path) -> None:
        self.path = path

    @classmethod
    def from_env(cls, output_dir: Path) -> CheckpointStore:
        configured = os.environ.get("ARCBENCH_CHECKPOINT_PATH", "").strip()
        return cls(Path(configured).resolve() if configured else output_dir / ".arc" / "checkpoint.json")

    def read(self) -> dict[str, Any]:
        default = {
            "version": 1,
            "architecture_completed": False,
            "parallel_mode": False,
            "max_workers": 2,
            "active_worktrees": {},
            "failed_modules": [],
            "conflicted_modules": [],
            "last_completed_index": 0,
            "completed": [],
            "paused": False,
            "final_review_completed": False,
            "current_pipeline_node": None,
            "current_round": 0,
            "loop_status": "",
            "last_feedback_message_id": "",
            "last_feedback_hash": "",
            "review_findings": [],
            "reviewer_write_violation": False,
            "message_cursor": 0,
        }
        if not self.path.is_file():
            return default
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return default
        if not isinstance(payload, dict):
            return default
        completed = payload.get("completed")
        payload["completed"] = [str(item) for item in completed] if isinstance(completed, list) else []
        payload["last_completed_index"] = max(int(payload.get("last_completed_index", 0) or 0), 0)
        payload.setdefault("version", 1)
        payload.setdefault("architecture_completed", False)
        payload.setdefault("parallel_mode", False)
        payload.setdefault("max_workers", 2)
        payload.setdefault("active_worktrees", {})
        payload.setdefault("failed_modules", [])
        payload.setdefault("conflicted_modules", [])
        payload.setdefault("paused", False)
        payload.setdefault("final_review_completed", False)
        payload.setdefault("current_pipeline_node", None)
        payload.setdefault("current_round", 0)
        payload.setdefault("loop_status", "")
        payload.setdefault("last_feedback_message_id", "")
        payload.setdefault("last_feedback_hash", "")
        payload.setdefault("review_findings", [])
        payload.setdefault("reviewer_write_violation", False)
        payload.setdefault("message_cursor", 0)
        return payload

    def write(self, payload: dict[str, Any]) -> None:
        with self._write_lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temporary.replace(self.path)

    def mark_module_started(self, module_id: str, phase: str) -> dict[str, Any]:
        payload = self.read()
        payload.update({"paused": False, "current_node_id": module_id, "current_phase": phase, "current_pipeline_node": phase, "current_round": 0, "loop_status": ""})
        self.write(payload)
        return payload

    def update_pipeline(self, module_id: str, **updates: Any) -> dict[str, Any]:
        # Keep read/modify/write under the same lock. Parallel module workers
        # can update different loop fields at nearly the same time; locking
        # only the final replace would allow one worker to overwrite the
        # other's freshly-written state.
        with self._write_lock:
            payload = self.read()
            normalized = dict(updates)
            if module_id:
                payload["current_node_id"] = module_id
            if "node" in normalized:
                payload["current_pipeline_node"] = normalized.pop("node")
            if "round_number" in normalized:
                payload["current_round"] = int(normalized.pop("round_number") or 0)
            payload.update(normalized)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temporary.replace(self.path)
            return payload

    def mark_architecture_completed(self) -> dict[str, Any]:
        payload = self.read()
        payload.update(
            {
                "architecture_completed": True,
                "paused": False,
                "current_node_id": None,
                "current_phase": None,
            }
        )
        self.write(payload)
        return payload

    def configure_parallel(self, enabled: bool, max_workers: int) -> dict[str, Any]:
        payload = self.read()
        payload.update({"parallel_mode": bool(enabled), "max_workers": int(max_workers)})
        self.write(payload)
        return payload

    def set_active_worktree(self, module_id: str, metadata: dict[str, Any]) -> dict[str, Any]:
        payload = self.read()
        active = dict(payload.get("active_worktrees") or {})
        active[module_id] = dict(metadata)
        payload["active_worktrees"] = active
        self.write(payload)
        return payload

    def update_active_worktree(self, module_id: str, **updates: Any) -> dict[str, Any]:
        payload = self.read()
        active = dict(payload.get("active_worktrees") or {})
        item = dict(active.get(module_id) or {})
        item.update(updates)
        active[module_id] = item
        payload["active_worktrees"] = active
        self.write(payload)
        return payload

    def clear_active_worktree(self, module_id: str) -> dict[str, Any]:
        payload = self.read()
        active = dict(payload.get("active_worktrees") or {})
        active.pop(module_id, None)
        payload["active_worktrees"] = active
        self.write(payload)
        return payload

    def mark_parallel_failure(self, module_id: str) -> dict[str, Any]:
        payload = self.read()
        values = list(payload.get("failed_modules") or [])
        if module_id not in values:
            values.append(module_id)
        payload["failed_modules"] = values
        self.write(payload)
        return payload

    def mark_parallel_conflict(self, module_id: str) -> dict[str, Any]:
        payload = self.read()
        values = list(payload.get("conflicted_modules") or [])
        if module_id not in values:
            values.append(module_id)
        payload["conflicted_modules"] = values
        self.write(payload)
        return payload

    def mark_module_completed(self, module_id: str, index: int) -> dict[str, Any]:
        payload = self.read()
        completed = list(payload["completed"])
        if module_id not in completed:
            completed.append(module_id)
        payload.update(
            {
                "completed": completed,
                "last_completed_index": max(int(payload["last_completed_index"]), index),
                "paused": False,
                "current_node_id": None,
                "current_phase": None,
            }
        )
        self.write(payload)
        return payload

    def mark_paused(self, module_id: str | None, phase: str | None) -> None:
        payload = self.read()
        payload.update({"paused": True, "current_node_id": module_id, "current_phase": phase})
        self.write(payload)

    def mark_final_review_completed(self) -> None:
        payload = self.read()
        payload.update(
            {
                "final_review_completed": True,
                "paused": False,
                "current_node_id": None,
                "current_phase": None,
            }
        )
        self.write(payload)
