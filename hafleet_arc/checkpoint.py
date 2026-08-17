from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


class CheckpointStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    @classmethod
    def from_env(cls, output_dir: Path) -> CheckpointStore:
        configured = os.environ.get("ARCBENCH_CHECKPOINT_PATH", "").strip()
        return cls(Path(configured).resolve() if configured else output_dir / ".arc" / "checkpoint.json")

    def read(self) -> dict[str, Any]:
        default = {
            "version": 1,
            "last_completed_index": 0,
            "completed": [],
            "paused": False,
            "final_review_completed": False,
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
        payload.setdefault("paused", False)
        payload.setdefault("final_review_completed", False)
        return payload

    def write(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.path)

    def mark_module_started(self, module_id: str, phase: str) -> dict[str, Any]:
        payload = self.read()
        payload.update({"paused": False, "current_node_id": module_id, "current_phase": phase})
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
