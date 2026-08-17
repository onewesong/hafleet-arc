from __future__ import annotations

import json
import os
import queue
import threading
import time
from pathlib import Path
from types import TracebackType
from typing import Any, Self

ROLE_INSTRUCTIONS = {
    "planner": """
You are the planning agent in a finite HAFleet run. Analyze the supplied requirement
subtree and the current repository. Write a concise, concrete implementation plan to
the exact plan path supplied by the coordinator. Do not modify any other file. Cover
data model, routes/UI, persistence, validation, scenarios, and verification. Resolve
uncertainty by inspecting the existing project. Do not start a long-running server.
""",
    "implementer": """
You are the implementation agent in a finite HAFleet run. Read the coordinator's plan
and implement the entire supplied requirement subtree in the current repository.
Preserve working behavior from earlier modules. Build real persisted behavior rather
than static mock screens. Run focused checks while working. Do not merely explain what
to do, do not stop at scaffolding, and do not start a long-running server.
""",
    "reviewer": """
You are the reviewer and repair agent in a finite HAFleet run. Inspect the implemented
requirement subtree against its scenarios, run practical tests or build checks, and fix
all defects you find. Check cross-module regressions, persistence, permissions,
validation, and visible UI behavior. Finish with a runnable project. Do not only write
a review report and do not start a long-running server.
""",
}

TRANSIENT_ERROR_MARKERS = (
    "401",
    "403",
    "429",
    "500",
    "502",
    "503",
    "504",
    "authentication failed",
    "connection",
    "empty turn",
    "failed to send",
    "rate limit",
    "retry limit",
    "server busy",
    "server overloaded",
    "stream disconnected",
    "streaming request",
    "temporarily unavailable",
    "timed out",
    "timeout",
    "transport closed",
    "unauthorized",
)


class TurnTimeoutError(RuntimeError):
    pass


def _workspace_fingerprint(root: Path) -> tuple[tuple[str, int, int], ...]:
    """Cheaply detect whether a turn produced project files."""

    entries: list[tuple[str, int, int]] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if relative.parts and relative.parts[0] in {".arc", ".git", "node_modules"}:
            continue
        if "node_modules" in relative.parts:
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        entries.append((relative.as_posix(), stat.st_size, stat.st_mtime_ns))
    return tuple(sorted(entries))


def _positive_int_env(name: str, default: int) -> int:
    try:
        return max(int(os.environ.get(name, str(default))), 1)
    except ValueError:
        return default


class CodexFleet:
    """Three persistent role threads sharing one ARC-Bench output workspace."""

    def __init__(
        self,
        output_dir: Path,
        skills_dir: Path | None = None,
        *,
        task_type: str = "web",
        grading_port: int = 3000,
        smoke_port: int = 3100,
    ) -> None:
        self.output_dir = output_dir
        self.skills_dir = skills_dir
        self.task_type = task_type
        self.grading_port = grading_port
        self.smoke_port = smoke_port
        self._codex: Any = None
        self._threads: dict[str, Any] = {}

    def __enter__(self) -> Self:
        try:
            from openai_codex import Codex, CodexConfig
        except ImportError as exc:
            raise RuntimeError("Install requirements.txt before running HAFleet ARC.") from exc

        env = os.environ.copy()
        # ARC-Bench containers may expose a read-only /root. Codex persists its
        # SQLite state under CODEX_HOME, so keep all ephemeral agent state in the
        # writable output workspace instead of inheriting ~/.codex.
        codex_home = self.output_dir / ".arc" / "hafleet" / "codex-home"
        codex_home.mkdir(parents=True, exist_ok=True)
        env["CODEX_HOME"] = str(codex_home)
        env["ARCBENCH_WEB_PORT"] = str(self.grading_port)
        env["HAFLEET_SMOKE_PORT"] = str(self.smoke_port)
        # Commands launched by Codex inherit this safe port. The generated
        # backend must still read PORT so the grader can inject its own value.
        env["PORT"] = str(self.smoke_port)
        overrides: list[str] = []
        base_url = os.environ.get("OPENAI_BASE_URL", "").strip()
        if base_url:
            env["OPENAI_BASE_URL"] = base_url
            overrides.append(f"openai_base_url={json.dumps(base_url)}")
        self._codex = Codex(config=CodexConfig(env=env, config_overrides=tuple(overrides)))
        self._codex.__enter__()
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if api_key:
            self._codex.login_api_key(api_key)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._codex is not None:
            self._codex.__exit__(exc_type, exc, traceback)

    def _thread(self, role: str) -> Any:
        if role in self._threads:
            return self._threads[role]
        if role not in ROLE_INSTRUCTIONS:
            raise ValueError(f"unknown fleet role: {role}")
        from openai_codex import ApprovalMode, Sandbox

        skill_note = (
            f"\nARC-Bench skills are available under {self.skills_dir}. Read their SKILL.md files when useful."
            if self.skills_dir
            else ""
        )
        model = os.environ.get("MODEL", "").strip() or None
        delivery_note = ""
        if self.task_type == "web":
            delivery_note = f"""

ARC-Bench web delivery contract:
- Keep frontend/ and backend/ at the project root.
- frontend/package.json must provide a working `npm run build`.
- backend/package.json must provide `npm run start`; the server must read process.env.PORT,
  serve the built frontend, and persist mutable data without native Node.js dependencies.
- During generation, use only smoke port {self.smoke_port}. Never bind grading port
  {self.grading_port}, and stop every server process you start.
- Match visible requirement text exactly. Use visible labels, real text buttons, inline
  validation messages, and avoid rendering the same test-targeted value more than once.
"""
        thread = self._codex.thread_start(
            cwd=str(self.output_dir),
            sandbox=Sandbox.full_access,
            approval_mode=ApprovalMode.deny_all,
            model=model,
            developer_instructions=ROLE_INSTRUCTIONS[role].strip() + delivery_note + skill_note,
        )
        self._threads[role] = thread
        return thread

    def _run_once(self, role: str, prompt: str, timeout_s: int) -> Any:
        results: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)
        active: dict[str, Any] = {}

        def target() -> None:
            try:
                thread = self._thread(role)
                if hasattr(thread, "turn"):
                    handle = thread.turn(prompt)
                    active["handle"] = handle
                    result = handle.run()
                else:  # test doubles and older SDKs
                    result = thread.run(prompt)
                results.put((True, result))
            except Exception as exc:  # noqa: BLE001 - cross-thread propagation
                results.put((False, exc))

        worker = threading.Thread(target=target, name=f"hafleet-{role}-turn", daemon=True)
        worker.start()
        try:
            succeeded, value = results.get(timeout=timeout_s)
        except queue.Empty as exc:
            handle = active.get("handle")
            if handle is not None:
                try:
                    handle.interrupt()
                except Exception as interrupt_error:  # noqa: BLE001 - best-effort SDK interrupt
                    print(
                        f"[hafleet] Failed to interrupt timed-out {role} turn: {interrupt_error}",
                        flush=True,
                    )
            raise TurnTimeoutError(f"{role} turn timed out after {timeout_s}s") from exc
        if not succeeded:
            raise value
        return value

    @staticmethod
    def _transient(error: BaseException) -> bool:
        message = f"{type(error).__name__}: {error}".lower()
        return isinstance(error, TurnTimeoutError) or any(
            marker in message for marker in TRANSIENT_ERROR_MARKERS
        )

    @staticmethod
    def _retry_delays() -> list[float]:
        configured = os.environ.get("HAFLEET_RETRY_DELAYS", "30,60")
        delays: list[float] = []
        for value in configured.split(","):
            try:
                delays.append(max(float(value.strip()), 0.0))
            except ValueError:
                continue
        return delays or [30.0, 60.0]

    def run(self, role: str, prompt: str) -> Any:
        attempts = _positive_int_env("HAFLEET_MAX_ATTEMPTS", 3)
        timeout_s = _positive_int_env("HAFLEET_TURN_TIMEOUT", 1200)
        delays = self._retry_delays()
        for attempt in range(1, attempts + 1):
            before = _workspace_fingerprint(self.output_dir)
            try:
                result = self._run_once(role, prompt, timeout_s)
                error = getattr(result, "error", None)
                if error is not None:
                    raise RuntimeError(f"{role} agent failed: {error}")
                final_response = getattr(result, "final_response", None)
                if not str(final_response or "").strip() and before == _workspace_fingerprint(
                    self.output_dir
                ):
                    raise RuntimeError(f"{role} empty turn: no response and no project file changes")
                return result
            except Exception as exc:
                if attempt >= attempts or not self._transient(exc):
                    raise
                self._threads.pop(role, None)
                delay = delays[min(attempt - 1, len(delays) - 1)]
                print(
                    f"[hafleet] Transient {role} failure; retrying "
                    f"{attempt + 1}/{attempts} after {delay:g}s: {exc}",
                    flush=True,
                )
                if delay:
                    time.sleep(delay)
        raise AssertionError("unreachable")
