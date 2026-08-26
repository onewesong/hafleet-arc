from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from queue import Empty, Queue
from typing import Any, Iterator


class MessageBus:
    """Append-only, run-local message bus with in-process subscriptions.

    The JSONL file is the durable source for replay and the subscription queues
    are deliberately ephemeral.  This keeps the runner single-process while
    allowing the dashboard to consume the same ordered message stream.
    """

    _registry_lock = threading.Lock()
    _path_locks: dict[str, threading.RLock] = {}

    def __init__(self, path: Path, run_id: str | None = None) -> None:
        self.path = Path(path)
        self.run_id = run_id or self._read_run_id() or f"run-{uuid.uuid4().hex[:12]}"
        key = str(self.path.resolve())
        with self._registry_lock:
            self._lock = self._path_locks.setdefault(key, threading.RLock())
        self._subscribers: list[Queue[dict[str, Any]]] = []
        self._published_ids: dict[str, dict[str, Any]] = self._read_published_ids()
        self._sequence = self._read_last_sequence()

    def _read_published_ids(self) -> dict[str, dict[str, Any]]:
        items: dict[str, dict[str, Any]] = {}
        try:
            with self.path.open(encoding="utf-8", errors="replace") as stream:
                for line in stream:
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(item, dict) and item.get("id"):
                        items[str(item["id"])] = item
        except OSError:
            return items
        return items

    def _read_run_id(self) -> str:
        try:
            with self.path.open(encoding="utf-8", errors="replace") as stream:
                for line in stream:
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if item.get("run_id"):
                        return str(item["run_id"])
        except OSError:
            return ""
        return ""

    def _read_last_sequence(self) -> int:
        sequence = 0
        try:
            with self.path.open(encoding="utf-8", errors="replace") as stream:
                for line in stream:
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    try:
                        sequence = max(sequence, int(item.get("sequence", 0) or 0))
                    except (TypeError, ValueError):
                        continue
        except OSError:
            return 0
        return sequence

    def _next_sequence(self) -> int:
        self._sequence += 1
        return self._sequence

    def publish(
        self,
        kind: str,
        *,
        sender: str,
        recipient: str = "orchestrator",
        payload: dict[str, Any] | None = None,
        conversation_id: str = "",
        correlation_id: str = "",
        parent_id: str = "",
        module_id: str = "",
        phase: str = "",
        round_number: int = 0,
        message_id: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            # Multiple MessageBus objects may observe the same output directory
            # (for example an integrated dashboard and the runner). Refresh the
            # durable cursor while holding the shared path lock so sequences
            # remain monotonic across those objects.
            self._sequence = max(self._sequence, self._read_last_sequence())
            if message_id and message_id not in self._published_ids:
                self._published_ids.update(self._read_published_ids())
            if message_id and message_id in self._published_ids:
                # Publishing the same envelope again is an idempotent retry.
                # This is important when a turn is resumed after a process
                # interruption: the durable log must not gain a duplicate.
                return dict(self._published_ids[message_id])
            item = {
                "sequence": self._next_sequence(),
                "id": message_id or f"msg-{uuid.uuid4().hex}",
                "run_id": self.run_id,
                "conversation_id": conversation_id or (f"module-{module_id}" if module_id else "run"),
                "correlation_id": correlation_id,
                "parent_id": parent_id,
                "from": sender,
                "to": recipient,
                "kind": kind,
                "module_id": module_id,
                "phase": phase,
                "round": int(round_number or 0),
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "payload": payload or {},
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(item, ensure_ascii=False) + "\n")
                stream.flush()
            self._published_ids[item["id"]] = item
            for subscriber in list(self._subscribers):
                subscriber.put_nowait(item)
            return item

    def replay(self, after_sequence: int = 0, *, module_id: str = "") -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        with self._lock:
            messages = self._read_messages(after_sequence, module_id=module_id)
            if messages:
                self._sequence = max(self._sequence, max(int(item.get("sequence", 0) or 0) for item in messages))
        return messages

    def _read_messages(self, after_sequence: int = 0, *, module_id: str = "") -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        try:
            with self.path.open(encoding="utf-8", errors="replace") as stream:
                for line in stream:
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(item, dict):
                        continue
                    try:
                        item_sequence = int(item.get("sequence", 0) or 0)
                    except (TypeError, ValueError):
                        continue
                    if item_sequence <= int(after_sequence):
                        continue
                    if module_id and item.get("module_id") != module_id:
                        continue
                    messages.append(item)
        except OSError:
            return []
        return messages

    def subscribe(self, after_sequence: int = 0, *, module_id: str = "") -> Iterator[dict[str, Any]]:
        # Register before yielding the replay while holding the same lock used
        # by publish(). This closes the replay/subscribe gap: a message written
        # by another thread or process cannot land between those two actions.
        with self._lock:
            replayed = self._read_messages(after_sequence, module_id=module_id)
            queue: Queue[dict[str, Any]] = Queue()
            self._subscribers.append(queue)
            cursor = int(after_sequence or 0)
            if replayed:
                cursor = max(cursor, max(int(item.get("sequence", 0) or 0) for item in replayed))
        for item in replayed:
            yield item
        last_heartbeat = time.monotonic()
        try:
            while True:
                try:
                    item = queue.get(timeout=0.5)
                except Empty:
                    # The dashboard may run in a separate process from the
                    # orchestrator, so also poll the append-only file. The
                    # in-process queue keeps the common case immediate.
                    external = self.replay(cursor, module_id=module_id)
                    if external:
                        for item in external:
                            cursor = max(cursor, int(item.get("sequence", 0) or 0))
                            yield item
                        continue
                    if time.monotonic() - last_heartbeat >= 15:
                        last_heartbeat = time.monotonic()
                        with self._lock:
                            sequence = self._sequence
                        yield {"kind": "heartbeat", "sequence": sequence, "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
                    continue
                if item.get("kind") == "heartbeat":
                    continue
                sequence = int(item.get("sequence", 0) or 0)
                if sequence <= cursor:
                    continue
                if module_id and item.get("module_id") != module_id:
                    continue
                cursor = sequence
                last_heartbeat = time.monotonic()
                yield item
        finally:
            with self._lock:
                if queue in self._subscribers:
                    self._subscribers.remove(queue)

    @property
    def last_sequence(self) -> int:
        return self._sequence
