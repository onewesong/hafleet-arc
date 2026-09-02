from __future__ import annotations

import json
import os
import posixpath
import signal
import subprocess
import tempfile
import threading
import time
import re
from pathlib import Path
from typing import Self
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

from .log import log


class PostflightError(RuntimeError):
    """The generated project does not satisfy the ARC-Bench delivery contract."""


def _read_package(path: Path) -> dict[str, object] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def validate_web_structure(output_dir: Path) -> list[str]:
    """Return actionable violations of the ARC-Bench web template contract."""

    errors: list[str] = []
    frontend = output_dir / "frontend"
    backend = output_dir / "backend"
    if not frontend.is_dir():
        errors.append("frontend/ is missing at the project root")
    if not backend.is_dir():
        errors.append("backend/ is missing at the project root")
    if errors:
        return errors

    frontend_package = _read_package(frontend / "package.json")
    if frontend_package is None:
        errors.append("frontend/package.json is missing or invalid JSON")
    elif not isinstance(frontend_package.get("scripts"), dict) or not str(
        frontend_package["scripts"].get("build", "")  # type: ignore[index]
    ).strip():
        errors.append("frontend/package.json must define scripts.build")

    backend_package = _read_package(backend / "package.json")
    if backend_package is None:
        errors.append("backend/package.json is missing or invalid JSON")
    elif not isinstance(backend_package.get("scripts"), dict) or not str(
        backend_package["scripts"].get("start", "")  # type: ignore[index]
    ).strip():
        errors.append("backend/package.json must define scripts.start")

    backend_sources = [
        path
        for suffix in ("*.js", "*.mjs", "*.cjs", "*.ts")
        for path in backend.rglob(suffix)
        if "node_modules" not in path.parts
    ]
    # Accept both direct reads and a modular configuration boundary such as
    # `createConfig(process.env)` plus `env.PORT` in another backend module.
    # The later startup rehearsal remains the authoritative proof that the
    # injected port is actually honored, so this structural gate should not
    # reject a thin server entrypoint merely because configuration is delegated.
    direct_port = re.compile(
        r"\bprocess\s*\.\s*env\s*(?:\??\.\s*PORT|\[\s*['\"]PORT['\"]\s*\])"
    )
    indirect_port = re.compile(
        r"\b(?:env|environment)\s*(?:\??\.\s*PORT|\[\s*['\"]PORT['\"]\s*\])"
    )
    destructured_port = re.compile(
        r"\{[^}]*\bPORT\b[^}]*\}\s*=\s*(?:process\s*\.\s*)?env\b"
    )
    reads_process_env = False
    reads_indirect_port = False
    reads_port = False
    for source in backend_sources:
        try:
            text = source.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        reads_process_env = reads_process_env or bool(re.search(r"\bprocess\s*\.\s*env\b", text))
        reads_indirect_port = reads_indirect_port or bool(
            indirect_port.search(text) or destructured_port.search(text)
        )
        if direct_port.search(text):
            reads_port = True
            break
    reads_port = reads_port or (reads_process_env and reads_indirect_port)
    if not reads_port:
        errors.append("backend must read the PORT environment variable")
    return errors


def _run_command(command: list[str], cwd: Path, timeout_s: int) -> tuple[int, str]:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        output = "\n".join(filter(None, [str(exc.stdout or ""), str(exc.stderr or "")]))
        return 124, f"timed out after {timeout_s}s\n{output[-2000:]}"
    except OSError as exc:
        return 127, str(exc)
    output = "\n".join(filter(None, [result.stdout, result.stderr])).strip()
    return result.returncode, output[-3000:]


def _listener_pids(port: int) -> list[int]:
    try:
        result = subprocess.run(
            ["lsof", "-ti", f"TCP:{port}", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    pids: list[int] = []
    for value in result.stdout.split():
        try:
            pids.append(int(value))
        except ValueError:
            continue
    return pids


def _owned_by_workspace(pid: int, output_dir: Path) -> bool:
    try:
        process_cwd = Path(os.readlink(f"/proc/{pid}/cwd")).resolve()
        process_cwd.relative_to(output_dir.resolve())
        return True
    except (OSError, ValueError):
        return False


def cleanup_workspace_port(port: int, output_dir: Path) -> list[int]:
    """Stop only listeners launched from this workspace, never foreign tenants."""

    owned = [pid for pid in _listener_pids(port) if _owned_by_workspace(pid, output_dir)]
    for pid in owned:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    if owned:
        time.sleep(0.5)
    for pid in owned:
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError):
            continue
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    return owned


class WorkspacePortGuard:
    """Prevent generated processes from occupying the grading port."""

    def __init__(self, output_dir: Path, grading_port: int, smoke_port: int) -> None:
        self.output_dir = output_dir
        self.grading_port = grading_port
        self.smoke_port = smoke_port
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> Self:
        if self.grading_port == self.smoke_port:
            raise ValueError("HAFleet smoke port must differ from the ARC-Bench grading port")

        def watch() -> None:
            while not self._stop.wait(5):
                killed = cleanup_workspace_port(self.grading_port, self.output_dir)
                if killed:
                    log(
                        f"[hafleet] Stopped workspace listener(s) on grading port "
                        f"{self.grading_port}: {killed}",
                        flush=True,
                    )

        self._thread = threading.Thread(target=watch, name="hafleet-port-guard", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1)
        cleanup_workspace_port(self.grading_port, self.output_dir)
        cleanup_workspace_port(self.smoke_port, self.output_dir)


def _http_ready(port: int) -> bool:
    try:
        with urlopen(f"http://127.0.0.1:{port}/", timeout=2) as response:
            return response.status < 500
    except HTTPError as exc:
        return exc.code < 500
    except (URLError, TimeoutError, OSError):
        return False


def _check_health_contract(port: int) -> list[str]:
    """Validate the small, framework-independent readiness API contract."""
    try:
        with urlopen(f"http://127.0.0.1:{port}/api/health", timeout=3) as response:
            content_type = response.headers.get("content-type", "")
            raw = response.read(16_384).decode("utf-8", errors="replace")
            payload = json.loads(raw)
    except (HTTPError, URLError, TimeoutError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return [f"/api/health is not a valid JSON readiness response: {exc}"]
    errors: list[str] = []
    if response.status >= 400:
        errors.append(f"/api/health returned HTTP {response.status}")
    if "json" not in content_type.lower():
        errors.append("/api/health must return an application/json content type")
    if not isinstance(payload, dict) or str(payload.get("status", "")).lower() not in {"ok", "ready", "healthy"}:
        errors.append("/api/health JSON must contain status=ok, ready, or healthy")
    return errors


_IMPORT_RE = re.compile(r"(?:from|import)\s*[\(]?\s*[\"']([^\"']+)")


def _check_frontend_module_graph(port: int) -> list[str]:
    """Verify browser-loaded ES modules are actually served by the backend."""

    pending = ["/app.js"]
    seen: set[str] = set()
    errors: list[str] = []
    while pending:
        path = pending.pop()
        if path in seen or not path.startswith("/"):
            continue
        seen.add(path)
        try:
            with urlopen(f"http://127.0.0.1:{port}{path}", timeout=3) as response:
                if response.status >= 400:
                    errors.append(f"frontend module {path} returned HTTP {response.status}")
                    continue
                body = response.read().decode("utf-8", errors="replace")
        except HTTPError as exc:
            errors.append(f"frontend module {path} returned HTTP {exc.code}")
            continue
        except (URLError, TimeoutError, OSError) as exc:
            errors.append(f"frontend module {path} could not be fetched: {exc}")
            continue
        parent = path.rsplit("/", 1)[0]
        for imported in _IMPORT_RE.findall(body):
            if imported.startswith("."):
                resolved = posixpath.normpath(posixpath.join(parent, imported))
                if not resolved.startswith("/"):
                    resolved = "/" + resolved
                pending.append(resolved)
    return errors


def rehearse_web_app(output_dir: Path, smoke_port: int) -> None:
    """Run the same build/start sequence as the grader on a safe smoke port."""

    violations = validate_web_structure(output_dir)
    if violations:
        raise PostflightError("Web delivery contract failed:\n- " + "\n- ".join(violations))

    npm_timeout = max(int(os.environ.get("HAFLEET_NPM_TIMEOUT", "600")), 1)
    ready_timeout = max(int(os.environ.get("HAFLEET_READY_TIMEOUT", "45")), 1)
    frontend = output_dir / "frontend"
    backend = output_dir / "backend"
    commands = (
        (["npm", "install", "--no-audit", "--no-fund"], frontend, "frontend npm install"),
        (["npm", "run", "build"], frontend, "frontend npm run build"),
        (["npm", "install", "--no-audit", "--no-fund"], backend, "backend npm install"),
    )
    for command, cwd, label in commands:
        returncode, output = _run_command(command, cwd, npm_timeout)
        if returncode != 0:
            raise PostflightError(f"{label} failed with exit code {returncode}:\n{output}")

    cleanup_workspace_port(smoke_port, output_dir)
    environment = os.environ.copy()
    environment.update({"PORT": str(smoke_port), "HOST": "127.0.0.1"})
    process: subprocess.Popen[str] | None = None
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as log_file:
        try:
            process = subprocess.Popen(
                ["npm", "start"],
                cwd=backend,
                env=environment,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
            deadline = time.monotonic() + ready_timeout
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    log_file.seek(0)
                    output = log_file.read()[-3000:]
                    raise PostflightError(
                        f"backend npm start exited early with code {process.returncode}:\n{output}"
                    )
                if _http_ready(smoke_port):
                    health_errors = _check_health_contract(smoke_port)
                    if health_errors:
                        raise PostflightError(
                            "runtime health contract failed:\n- " + "\n- ".join(health_errors)
                        )
                    module_errors = _check_frontend_module_graph(smoke_port)
                    if module_errors:
                        raise PostflightError(
                            "frontend module graph failed:\n- " + "\n- ".join(module_errors)
                        )
                    return
                time.sleep(1)
            log_file.seek(0)
            output = log_file.read()[-3000:]
            raise PostflightError(
                f"backend did not become ready on smoke port {smoke_port} "
                f"within {ready_timeout}s:\n{output}"
            )
        except OSError as exc:
            raise PostflightError(f"backend npm start could not launch: {exc}") from exc
        finally:
            if process is not None and process.poll() is None:
                try:
                    os.killpg(os.getpgid(process.pid), signal.SIGTERM)
                    process.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    try:
                        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                    except OSError:
                        pass
            cleanup_workspace_port(smoke_port, output_dir)
