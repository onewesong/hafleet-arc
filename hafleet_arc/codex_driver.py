from __future__ import annotations

import json
import os
import queue
import shutil
import threading
import time
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from .log import log
from .pipeline import Pipeline, load_pipeline

TRANSIENT_ERROR_MARKERS = (
    # Provider/model capacity errors are retryable even when the SDK does not
    # expose an HTTP status code (for example: "Selected model is at capacity").
    "at capacity",
    "capacity exceeded",
    "capacity_exceeded",
    "model_capacity",
    "model overloaded",
    "model_overloaded",
    "overloaded",
    "try a different model",
    "try_a_different_model",
    "401",
    "403",
    "429",
    "rate_limit_exceeded",
    "500",
    "502",
    "503",
    "504",
    "520",
    "521",
    "522",
    "524",
    "529",
    "authentication failed",
    "connection",
    "closed stdout",
    "empty turn",
    "failed to send",
    "rate limit",
    "retry limit",
    "server busy",
    "server_busy",
    "server_error",
    "internal server error",
    "internal_server_error",
    "server overloaded",
    "process closed",
    "stream disconnected",
    "streaming request",
    "temporarily unavailable",
    "temporarily_unavailable",
    "timed out",
    "timeout",
    "transport closed",
    "unauthorized",
)

# Capacity incidents are often short-lived and may outlast a single provider
# retry window. Keep the retry budget generous enough for unattended ARC-Bench
# runs while allowing operators to override it through environment variables.
DEFAULT_MAX_ATTEMPTS = 6
DEFAULT_RETRY_DELAYS = "30,60,120,180,300"
DEFAULT_RETRY_DELAY_VALUES = (30.0, 60.0, 120.0, 180.0, 300.0)


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
    """Persistent role threads sharing one ARC-Bench output workspace."""

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
        self._threads: dict[tuple[str, str], Any] = {}
        self._thread_lock = threading.Lock()
        self.pipeline: Pipeline = load_pipeline(output_dir)

    @staticmethod
    def _model_for_role(role: str) -> str | None:
        """Resolve a role-specific model, falling back to the global MODEL."""

        role_model = os.environ.get(f"HAFLEET_{role.upper()}_MODEL", "").strip()
        return role_model or os.environ.get("MODEL", "").strip() or None

    @staticmethod
    def _read_only_role(role: str) -> bool:
        normalized = str(role or "").strip().lower().replace("-", "_")
        return (
            normalized in {"reviewer", "review", "qa"}
            or normalized.endswith("_reviewer")
            or "review" in normalized
            or "audit" in normalized
            or normalized.endswith("_qa")
        )

    @staticmethod
    def _seed_codex_auth(codex_home: Path) -> None:
        """Seed an isolated Codex home from the operator's existing login.

        ARC-Bench runs keep sessions and SQLite state inside the output directory,
        but an empty isolated home also loses the operator's ChatGPT login.  Prefer
        an explicit API key when one is supplied; otherwise copy only auth.json and
        leave the user's global Codex configuration untouched.
        """

        target = codex_home / "auth.json"
        if target.exists() or os.environ.get("OPENAI_API_KEY", "").strip():
            return
        inherited_home = os.environ.get("CODEX_HOME", "").strip()
        candidates = [Path(inherited_home)] if inherited_home else []
        default_home = Path.home() / ".codex"
        if default_home not in candidates:
            candidates.append(default_home)
        for home in candidates:
            source = home.expanduser() / "auth.json"
            try:
                if not source.is_file() or source.resolve() == target.resolve():
                    continue
                shutil.copy2(source, target)
                target.chmod(0o600)
                return
            except OSError as exc:
                log(f"[hafleet] warning: could not seed isolated Codex authentication: {exc}", flush=True)
                return

    @staticmethod
    def _seed_codex_config(codex_home: Path) -> None:
        """Preserve the operator's configured model provider in an isolated home."""

        target = codex_home / "config.toml"
        if target.exists():
            return
        inherited_home = os.environ.get("CODEX_HOME", "").strip()
        candidates = [Path(inherited_home)] if inherited_home else []
        default_home = Path.home() / ".codex"
        if default_home not in candidates:
            candidates.append(default_home)
        for home in candidates:
            source = home.expanduser() / "config.toml"
            try:
                if not source.is_file() or source.resolve() == target.resolve():
                    continue
                shutil.copy2(source, target)
                target.chmod(0o600)
                return
            except OSError as exc:
                log(f"[hafleet] warning: could not seed isolated Codex configuration: {exc}", flush=True)
                return

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
        self._seed_codex_auth(codex_home)
        self._seed_codex_config(codex_home)
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

    def _thread(self, role: str, workspace_dir: Path | None = None) -> Any:
        cwd = (workspace_dir or self.output_dir).resolve()
        key = (role, str(cwd))
        if key in self._threads:
            return self._threads[key]
        instructions = self.pipeline.prompt_for(role) or self.pipeline.default_prompt
        if not instructions:
            raise RuntimeError(f"pipeline has no prompt for role {role!r} and no default_prompt")
        from openai_codex import ApprovalMode, Sandbox

        skill_note = (
            f"\nARC-Bench skills are available under {self.skills_dir}. Read their SKILL.md files when useful."
            if self.skills_dir
            else ""
        )
        model = self._model_for_role(role)
        if self._read_only_role(role):
            instructions += "\nThis role is read-only: do not modify project files or Git state; report findings only."
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
- Use canonical history-style URL paths for user-visible views. A direct request or
  browser refresh at a client route must return the app shell and render that route;
  reserve /api/* for JSON and return structured non-2xx errors there. Hash links can
  be compatibility aliases only. Every form control needs a label/accessible name,
  required and described-by semantics, deterministic validation, and a named submit
  button with duplicate-submit protection. Keep loading/empty/error/retry states
  visible and ensure successful mutations survive a reload through the public API.
- Before finishing a web turn, smoke-check each requirement-derived route directly
  (HTTP GET), then exercise one valid and one invalid form action. Record failures in
  the structured result instead of silently substituting static placeholder content.
- Treat high-fan-out prerequisite flows as gates. In particular, assert the canonical
  URL after account creation/sign-in and after primary search/navigation actions; a
  success message or changed header alone is not evidence that dependent flows work.
- Use ARC_TEST_DATE/current runtime time for app fixtures and executable tests. Do not
  hard-code the date on which the project was generated.
- Register project-owned verification commands in .arc/hafleet/verification.json using
  JSON entries with module_id, cwd, and an argv-array command. Never reference evaluator
  tests or directories outside the generated workspace.
"""
        thread = self._codex.thread_start(
            cwd=str(cwd),
            sandbox=Sandbox.read_only if self._read_only_role(role) else Sandbox.full_access,
            approval_mode=ApprovalMode.deny_all,
            model=model,
            developer_instructions=instructions.strip() + delivery_note + skill_note,
        )
        self._threads[key] = thread
        return thread

    def reset_thread(self, role: str, workspace_dir: Path | None = None) -> None:
        """Drop a role/workspace conversation before a corrective turn.

        Initial implementation turns benefit from a persistent conversation, but a
        long review/repair loop can accumulate stale or contradictory conclusions.
        The orchestrator may request a fresh context for those bounded corrective
        passes; the files and durable MessageBus context remain the source of truth.
        """
        key = (role, str((workspace_dir or self.output_dir).resolve()))
        with self._thread_lock:
            self._threads.pop(key, None)

    def _run_once(
        self,
        role: str,
        prompt: str,
        timeout_s: int,
        workspace_dir: Path | None = None,
    ) -> Any:
        results: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)
        active: dict[str, Any] = {}

        def target() -> None:
            try:
                with self._thread_lock:
                    thread = self._thread(role, workspace_dir)
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
                    log(
                        f"[hafleet] Failed to interrupt timed-out {role} turn: {interrupt_error}",
                        flush=True,
                    )
            raise TurnTimeoutError(f"{role} turn timed out after {timeout_s}s") from exc
        if not succeeded:
            raise value
        return value

    @staticmethod
    def _transient(error: BaseException) -> bool:
        # SDK versions expose provider failures either as exception text or as
        # structured attributes. Include both forms so capacity/rate-limit
        # responses are retried consistently across adapters.
        details = [f"{type(error).__name__}: {error}"]
        for attribute in ("code", "type", "status", "status_code", "error_code", "message"):
            value = getattr(error, attribute, None)
            if value is not None:
                details.append(str(value))
        message = " ".join(details).lower()
        return isinstance(error, TurnTimeoutError) or any(
            marker in message for marker in TRANSIENT_ERROR_MARKERS
        )

    @staticmethod
    def _retry_delays() -> list[float]:
        configured = os.environ.get("HAFLEET_RETRY_DELAYS", DEFAULT_RETRY_DELAYS)
        delays: list[float] = []
        for value in configured.split(","):
            try:
                delays.append(max(float(value.strip()), 0.0))
            except ValueError:
                continue
        return delays or list(DEFAULT_RETRY_DELAY_VALUES)

    @staticmethod
    def _progress_retry_prompt(role: str) -> str:
        return f"""
Continue the interrupted {role} task from the current workspace. The previous turn
timed out after making persistent file changes, so do not restart planning or replace
working code. Inspect the current git diff, the existing .arc/hafleet plan and
architecture, and the registered verification manifest. Finish only the incomplete
requirement-derived behavior and tests, run the focused registered checks, repair any
failures, and return the required structured completion summary. Preserve all valid
work already present and do not access external or hidden evaluator tests.
""".strip()

    def run(self, role: str, prompt: str, workspace_dir: Path | None = None) -> Any:
        attempts = _positive_int_env("HAFLEET_MAX_ATTEMPTS", DEFAULT_MAX_ATTEMPTS)
        timeout_s = _positive_int_env("HAFLEET_TURN_TIMEOUT", 1200)
        delays = self._retry_delays()
        workspace = (workspace_dir or self.output_dir).resolve()
        attempt_prompt = prompt
        for attempt in range(1, attempts + 1):
            before = _workspace_fingerprint(workspace)
            log(
                f"[hafleet] {role} turn started in {workspace} "
                f"(attempt {attempt}/{attempts}, timeout={timeout_s}s)",
                flush=True,
            )
            try:
                result = self._run_once(role, attempt_prompt, timeout_s, workspace)
                error = getattr(result, "error", None)
                if error is not None:
                    raise RuntimeError(f"{role} agent failed: {error}")
                final_response = getattr(result, "final_response", None)
                if not str(final_response or "").strip() and before == _workspace_fingerprint(workspace):
                    raise RuntimeError(f"{role} empty turn: no response and no project file changes")
                changed = len(_workspace_fingerprint(workspace)) - len(before)
                log(f"[hafleet] {role} turn finished; file-count delta={changed:+d}", flush=True)
                return result
            except Exception as exc:
                log(f"[hafleet] {role} turn failed: {exc}", flush=True)
                if attempt >= attempts or not self._transient(exc):
                    raise
                made_progress = before != _workspace_fingerprint(workspace)
                if isinstance(exc, TurnTimeoutError) and made_progress and not self._read_only_role(role):
                    attempt_prompt = self._progress_retry_prompt(role)
                    log(
                        f"[hafleet] {role} timeout preserved workspace progress; "
                        "next attempt will continue from the existing diff",
                        flush=True,
                    )
                self._threads.pop((role, str(workspace)), None)
                delay = delays[min(attempt - 1, len(delays) - 1)]
                log(
                    f"[hafleet] Transient {role} failure; retrying "
                    f"{attempt + 1}/{attempts} after {delay:g}s: {exc}",
                    flush=True,
                )
                if delay:
                    time.sleep(delay)
        raise AssertionError("unreachable")
