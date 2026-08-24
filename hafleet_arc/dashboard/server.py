from __future__ import annotations

import json
import re
import subprocess
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse


ROLE_PATTERNS = {
    "architect": re.compile(r"architecture agent", re.IGNORECASE),
    "planner": re.compile(r"planning agent", re.IGNORECASE),
    "implementer": re.compile(r"implementation agent", re.IGNORECASE),
    "reviewer": re.compile(r"reviewer and repair agent|reviewer", re.IGNORECASE),
}
ROLE_LABELS = {
    "architect": "Architect",
    "planner": "Planner",
    "implementer": "Implementer",
    "reviewer": "Reviewer",
    "postflight": "Postflight",
}
ROLE_PHASES = {
    "architect": "architecture",
    "planner": "design",
    "implementer": "implement",
    "reviewer": "review",
    "postflight": "postflight",
}


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _text_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(_text_content(item) for item in value)
    if isinstance(value, dict):
        if "text" in value:
            return _text_content(value["text"])
        if "content" in value:
            return _text_content(value["content"])
        if "message" in value:
            return _text_content(value["message"])
        if "output" in value:
            return _text_content(value["output"])
        if "input" in value:
            return _text_content(value["input"])
    return ""


def _classify_role(text: str) -> str:
    for role, pattern in ROLE_PATTERNS.items():
        if pattern.search(text):
            return role
    return "unknown"


def _module_id_from_text(text: str) -> str:
    match = re.search(r"Module:\s*[^\n]*?\s-\s([A-Za-z0-9._-]+)", text, re.IGNORECASE)
    if match:
        return match.group(1)
    match = re.search(r"(?:worktrees|worktree)[/\\]([A-Za-z0-9._-]+)", text, re.IGNORECASE)
    return match.group(1) if match else ""


def _git_snapshot(workspace: str) -> dict[str, Any]:
    if not workspace:
        return {"branch": "", "path": "", "files_changed": [], "diff_stat": "", "diff": "", "commit_diff": ""}
    path = Path(workspace).resolve()
    if not path.is_dir():
        return {"branch": "", "path": workspace, "files_changed": [], "diff_stat": "", "diff": "", "commit_diff": ""}

    def run(args: list[str]) -> str:
        try:
            result = subprocess.run(
                ["git", "-C", str(path), *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=3,
                check=False,
            )
            return result.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            return ""

    status = run(["status", "--short"])
    # Keep the dashboard read-only and bounded even when an agent generated a
    # very large patch.  The recent commit diff covers changes that were
    # already checkpointed and are therefore no longer in the working tree.
    diff = run(["diff", "--no-ext-diff", "--unified=3"])
    commit_diff = run(["diff", "HEAD~1", "HEAD", "--no-ext-diff", "--unified=3"])
    return {
        "branch": run(["branch", "--show-current"]),
        "path": str(path),
        "files_changed": status.splitlines()[:200],
        "diff_stat": run(["diff", "--stat"]),
        "diff": diff[:120000],
        "commit_diff": commit_diff[:120000],
    }


class DashboardCollector:
    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir.resolve()
        self.arc_dir = self.output_dir / ".arc"
        self.events_path = self.arc_dir / "runner-events.jsonl"
        self.checkpoint_path = self.arc_dir / "checkpoint.json"
        self.sessions_dir = self.arc_dir / "hafleet" / "codex-home" / "sessions"

    def _events(self) -> list[dict[str, Any]]:
        if not self.events_path.is_file():
            return []
        events: list[dict[str, Any]] = []
        try:
            with self.events_path.open(encoding="utf-8", errors="replace") as stream:
                for line in stream:
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(item, dict):
                        events.append(item)
        except OSError:
            return events
        return events

    def _session_files(self) -> list[Path]:
        if not self.sessions_dir.is_dir():
            return []
        return sorted(self.sessions_dir.rglob("*.jsonl"), key=lambda item: item.stat().st_mtime_ns)

    def _parse_session(self, path: Path, detail: bool = False) -> dict[str, Any]:
        session_id = path.stem
        role = "unknown"
        cwd = ""
        model = ""
        started_at = ""
        module_id = ""
        updated_at = ""
        messages: list[dict[str, Any]] = []
        try:
            with path.open(encoding="utf-8", errors="replace") as stream:
                for line in stream:
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    timestamp = str(item.get("timestamp", ""))
                    if timestamp:
                        updated_at = timestamp
                    payload = item.get("payload") if isinstance(item, dict) else {}
                    if item.get("type") == "session_meta" and isinstance(payload, dict):
                        session_id = str(payload.get("session_id") or payload.get("id") or session_id)
                        cwd = str(payload.get("cwd") or "")
                        model = str(payload.get("model_provider") or "")
                        started_at = timestamp
                        role = _classify_role(_text_content(payload.get("base_instructions")))
                        module_id = _module_id_from_text(_text_content(payload.get("base_instructions")))
                    if isinstance(payload, dict) and item.get("type") == "response_item" and payload.get("type") == "message":
                        message_text = _text_content(payload.get("content"))
                        if payload.get("role") == "developer":
                            detected_role = _classify_role(message_text)
                            if detected_role != "unknown":
                                role = detected_role
                            module_id = module_id or _module_id_from_text(message_text)
                    if isinstance(payload, dict) and item.get("type") == "event_msg" and payload.get("type") == "user_message":
                        module_id = module_id or _module_id_from_text(_text_content(payload.get("message")))
                    if not detail or not isinstance(payload, dict):
                        continue
                    if item.get("type") == "response_item" and payload.get("type") == "message":
                        content = _text_content(payload.get("content"))
                        module_id = module_id or _module_id_from_text(content)
                        if content.strip():
                            messages.append(
                                {
                                    "timestamp": timestamp,
                                    "role": str(payload.get("role") or "unknown"),
                                    "content": content,
                                }
                            )
                    elif item.get("type") == "event_msg" and payload.get("type") in {
                        "user_message",
                        "agent_message",
                    }:
                        content = _text_content(payload.get("message"))
                        module_id = module_id or _module_id_from_text(content)
                        if content.strip():
                            messages.append(
                                {
                                    "timestamp": timestamp,
                                    "role": "user" if payload.get("type") == "user_message" else "assistant",
                                    "kind": payload.get("type"),
                                    "content": content,
                                }
                            )
                    elif item.get("type") == "response_item" and payload.get("type") in {
                        "custom_tool_call",
                        "custom_tool_call_output",
                        "function_call",
                        "function_call_output",
                    }:
                        tool_name = str(payload.get("name") or payload.get("type"))
                        content = _text_content(payload.get("input") or payload.get("output"))
                        if content.strip():
                            messages.append(
                                {
                                    "timestamp": timestamp,
                                    "role": "tool",
                                    "kind": payload.get("type"),
                                    "name": tool_name,
                                    "content": content,
                                }
                            )
                    elif item.get("type") == "event_msg" and payload.get("type") in {
                        "task_started",
                        "task_complete",
                        "context_compacted",
                    }:
                        messages.append(
                            {
                                "timestamp": timestamp,
                                "role": "system",
                                "kind": payload.get("type"),
                                "content": json.dumps(payload, ensure_ascii=False),
                            }
                        )
        except OSError:
            pass
        result: dict[str, Any] = {
            "id": session_id,
            "file": path.name,
            "role": role,
            "cwd": cwd,
            "model": model,
            "module_id": module_id,
            "started_at": started_at,
            "updated_at": updated_at,
            "workspace": cwd,
            "size": path.stat().st_size if path.exists() else 0,
        }
        if detail:
            result["messages"] = messages[-3000:]
            snapshot = _git_snapshot(cwd)
            result.update(snapshot)
            result["errors"] = [
                message
                for message in messages
                if message.get("role") == "system"
                or re.search(r"\b(error|failed|failure|conflict|timeout)\b", str(message.get("content", "")), re.IGNORECASE)
            ][-20:]
        return result

    def _module_states(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        latest: dict[str, dict[str, Any]] = {}
        for event in events:
            if event.get("type") != "requirement_state":
                continue
            node_id = str(event.get("node_id") or "")
            if node_id:
                latest[node_id] = {
                    "node_id": node_id,
                    "phase": event.get("phase"),
                    "status": event.get("status"),
                    "timestamp": event.get("timestamp"),
                "message": event.get("message"),
                "character": next((role for role, phase in ROLE_PHASES.items() if phase == event.get("phase")), ""),
            }
        return list(latest.values())

    @staticmethod
    def _status_for_character(
        role: str,
        runner_state: str,
        module: dict[str, Any] | None,
        checkpoint: dict[str, Any],
    ) -> str:
        # Architecture is a one-time global phase and does not have a
        # requirement_state event.  Its completion is therefore represented
        # by the checkpoint rather than by the currently active module.
        if role == "architect" and checkpoint.get("architecture_completed"):
            return "success"
        if module:
            status = str(module.get("status") or "")
            if status == "running":
                return "working"
            if status in {"failed", "error"}:
                return "failed"
            if status in {"paused", "waiting"}:
                return "paused"
            if status == "completed":
                return "success"
        if runner_state in {"failed", "error"}:
            return "failed"
        if runner_state == "paused":
            return "paused"
        if runner_state == "completed":
            return "success"
        return "idle"

    @classmethod
    def _pipeline_status(
        cls,
        role: str,
        runner_state: str,
        module: dict[str, Any] | None,
        checkpoint: dict[str, Any],
    ) -> str:
        """Map the current pipeline phase to every role's visible status.

        A planner has no current ``design`` event once implementation starts,
        but it should remain visibly complete.  The same applies to the
        implementer once review starts.  This keeps the room representing the
        whole pipeline instead of only the latest requirement event.
        """
        direct = cls._status_for_character(role, runner_state, module, checkpoint)
        if direct in {"failed", "paused"}:
            return direct
        if role == "architect":
            return direct

        phase = str(checkpoint.get("current_phase") or (module or {}).get("phase") or "")
        phases = {"design": 1, "implement": 2, "review": 3, "postflight": 4, "completed": 5}
        current_rank = phases.get(phase, 0)
        role_rank = {"planner": 1, "implementer": 2, "reviewer": 3, "postflight": 4}.get(role, 0)
        if runner_state == "completed" or current_rank > role_rank:
            return "success"
        if current_rank == role_rank:
            return "working" if direct in {"working", "idle"} else direct
        return "idle"

    def _characters(
        self,
        sessions: list[dict[str, Any]],
        modules: list[dict[str, Any]],
        runner: dict[str, Any],
        checkpoint: dict[str, Any],
    ) -> list[dict[str, Any]]:
        by_phase = {str(item.get("phase")): item for item in modules}
        tasks_by_phase: dict[str, list[dict[str, Any]]] = {}
        for module in modules:
            tasks_by_phase.setdefault(str(module.get("phase") or ""), []).append(module)
        by_role: dict[str, dict[str, Any]] = {}
        for item in sessions:
            role = str(item.get("role") or "")
            if role in ROLE_LABELS:
                by_role[role] = item
        active = checkpoint.get("active_worktrees") or {}
        characters: list[dict[str, Any]] = []
        for role, label in ROLE_LABELS.items():
            session = by_role.get(role, {})
            phase = ROLE_PHASES[role]
            module = by_phase.get(phase)
            module_id = str(session.get("module_id") or (module or {}).get("node_id") or "")
            worktree = dict(active.get(module_id) or {})
            workspace = str(session.get("workspace") or worktree.get("path") or "")
            snapshot = _git_snapshot(workspace) if workspace else {"branch": "", "path": "", "files_changed": [], "diff_stat": ""}
            if worktree.get("branch"):
                snapshot["branch"] = worktree["branch"]
            if worktree.get("path"):
                snapshot["path"] = worktree["path"]
            status = self._pipeline_status(role, str(runner.get("state") or ""), module, checkpoint)
            message = (module or {}).get("message") or ""
            if not message and role == "architect" and checkpoint.get("architecture_completed"):
                message = "Architecture scaffold completed"
            if not message and status == "success":
                message = f"{label} completed"
            message = message or "Waiting for work"
            characters.append(
                {
                    "id": role,
                    "label": label,
                    "status": status,
                    "phase": phase,
                    "module_id": module_id,
                    "session_id": session.get("id", ""),
                    "workspace": workspace,
                    "message": message,
                    "started_at": session.get("started_at", ""),
                    "updated_at": session.get("updated_at") or (module or {}).get("timestamp", ""),
                    "files_changed": snapshot.get("files_changed", []),
                    "worktree": {"branch": snapshot.get("branch", ""), "path": snapshot.get("path", "")},
                    "diff_stat": snapshot.get("diff_stat", ""),
                    "diff": snapshot.get("diff", ""),
                    "commit_diff": snapshot.get("commit_diff", ""),
                    "errors": session.get("errors", []),
                    "tasks": tasks_by_phase.get(phase, []),
                }
            )
        return characters

    def state(self) -> dict[str, Any]:
        events = self._events()
        runner = next((item for item in reversed(events) if item.get("type") == "runner_state"), {})
        checkpoint = _read_json(self.checkpoint_path, {})
        modules = self._module_states(events)
        sessions = [self._parse_session(path) for path in self._session_files()]
        return {
            "output_dir": str(self.output_dir),
            "runner": {
                "state": runner.get("state", "unknown"),
                "timestamp": runner.get("timestamp"),
                "message": runner.get("message"),
            },
            "checkpoint": checkpoint,
            "modules": modules,
            "events": events[-200:],
            "sessions": sessions,
            "characters": self._characters(sessions, modules, runner, checkpoint),
            "generated_at": datetime.now().astimezone().isoformat(),
        }

    def sessions(self) -> list[dict[str, Any]]:
        return [self._parse_session(path) for path in self._session_files()]

    def session(self, session_id: str) -> dict[str, Any] | None:
        for path in self._session_files():
            item = self._parse_session(path)
            if item["id"] == session_id or path.stem == session_id:
                return self._parse_session(path, detail=True)
        return None


class _DashboardHandler(BaseHTTPRequestHandler):
    collector: DashboardCollector
    static_dir = Path(__file__).with_name("static")

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/api/state":
            self._send(200, json.dumps(self.collector.state(), ensure_ascii=False).encode(), "application/json; charset=utf-8")
            return
        if parsed.path == "/api/sessions":
            self._send(200, json.dumps(self.collector.sessions(), ensure_ascii=False).encode(), "application/json; charset=utf-8")
            return
        if parsed.path.startswith("/api/sessions/"):
            session_id = unquote(parsed.path.removeprefix("/api/sessions/"))
            session = self.collector.session(session_id)
            if session is None:
                self._send(404, b'{"error":"session not found"}', "application/json; charset=utf-8")
            else:
                self._send(200, json.dumps(session, ensure_ascii=False).encode(), "application/json; charset=utf-8")
            return
        relative = parsed.path.removeprefix("/") or "index.html"
        target = (self.static_dir / relative).resolve()
        try:
            target.relative_to(self.static_dir.resolve())
        except ValueError:
            self._send(404, b"Not found", "text/plain; charset=utf-8")
            return
        if not target.is_file():
            self._send(404, b"Not found", "text/plain; charset=utf-8")
            return
        content_type = "text/plain; charset=utf-8"
        if target.suffix == ".html":
            content_type = "text/html; charset=utf-8"
        elif target.suffix == ".js":
            content_type = "text/javascript; charset=utf-8"
        elif target.suffix == ".css":
            content_type = "text/css; charset=utf-8"
        elif target.suffix == ".svg":
            content_type = "image/svg+xml"
        self._send(200, target.read_bytes(), content_type)

    def log_message(self, _format: str, *_args: Any) -> None:
        return


class DashboardServer:
    """Optional local dashboard that observes one HAFleet output directory."""

    def __init__(self, output_dir: Path, host: str = "127.0.0.1", port: int = 3200) -> None:
        self.collector = DashboardCollector(output_dir)
        self.host = host
        self.port = int(port)
        handler = type("DashboardHandler", (_DashboardHandler,), {"collector": self.collector})
        self.httpd = ThreadingHTTPServer((self.host, self.port), handler)
        self.thread: threading.Thread | None = None

    def start(self) -> "DashboardServer":
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="hafleet-dashboard", daemon=True)
        self.thread.start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        if self.thread is not None:
            self.thread.join(timeout=2)
