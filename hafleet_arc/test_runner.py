from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .postflight import cleanup_workspace_port


class TestExecutionError(RuntimeError):
    pass


def _artifact_limit() -> int:
    try:
        return max(int(os.environ.get("HAFLEET_TEST_ARTIFACT_LIMIT", "60000")), 1000)
    except ValueError:
        return 60000


def _run(command: list[str], cwd: Path, env: dict[str, str], timeout: int) -> tuple[int, str]:
    process: subprocess.Popen[str] | None = None
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            start_new_session=True,
        )
        stdout, _ = process.communicate(timeout=timeout)
        output = (stdout or "").strip()
        return process.returncode, output[-_artifact_limit():]
    except subprocess.TimeoutExpired as exc:
        if process is not None:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except OSError:
                process.kill()
            stdout, _ = process.communicate()
        else:
            stdout = str(exc.stdout or "")
        return 124, f"timed out after {timeout}s\n{str(stdout or '')[-_artifact_limit():]}"
    except OSError as exc:
        return 127, str(exc)


def _ready(port: int) -> bool:
    try:
        with urlopen(f"http://127.0.0.1:{port}/", timeout=2) as response:
            return response.status < 500
    except HTTPError as exc:
        return exc.code < 500
    except (URLError, TimeoutError, OSError):
        return False


def _reset_test_state(port: int) -> tuple[str, str]:
    """Reset an isolated app when it advertises the generic test contract.

    A 404 is intentionally treated as legacy compatibility (the endpoint is
    optional for older outputs); other failures are surfaced to the caller.
    """
    request = Request(f"http://127.0.0.1:{port}/api/test/reset", method="POST")
    try:
        with urlopen(request, timeout=5) as response:
            if response.status >= 400:
                return "failed", f"POST /api/test/reset returned HTTP {response.status}"
            return "passed", response.read(16_384).decode("utf-8", errors="replace")
    except HTTPError as exc:
        if exc.code == 404:
            return "skipped", "POST /api/test/reset is not implemented (legacy project)"
        return "failed", f"POST /api/test/reset returned HTTP {exc.code}"
    except (URLError, TimeoutError, OSError) as exc:
        return "failed", f"POST /api/test/reset failed: {exc}"


def _stop(process: subprocess.Popen[str] | None) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        process.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except OSError:
            pass


def _browser_error(output: str) -> bool:
    text = str(output or "").lower()
    return any(marker in text for marker in (
        "executable doesn't exist",
        "browser_type.launch",
        "browsers.path",
        "playwright install",
        "could not find chromium",
        "browser unavailable",
    ))


def _safe_cwd(output_dir: Path, value: object) -> Path | None:
    try:
        candidate = (output_dir / str(value or ".")).resolve()
        candidate.relative_to(output_dir.resolve())
    except (OSError, ValueError):
        return None
    return candidate if candidate.is_dir() else None


def _verification_commands(output_dir: Path, module_id: str) -> list[tuple[list[str], Path, str]]:
    """Load project-owned commands without consulting any evaluator directory."""

    path = output_dir / ".arc" / "hafleet" / "verification.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    raw = payload.get("commands") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        return []
    commands: list[tuple[list[str], Path, str]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        owner = str(item.get("module_id") or "ROOT")
        if module_id != "ROOT" and owner not in {module_id, "ROOT", "*"}:
            continue
        argv = item.get("command")
        cwd = _safe_cwd(output_dir, item.get("cwd"))
        if not isinstance(argv, list) or not argv or cwd is None:
            continue
        normalized = [str(part) for part in argv if str(part)]
        if not normalized:
            continue
        # The manifest belongs to the generated project. Refuse path traversal or
        # obvious benchmark/evaluator references even if an agent writes a bad entry.
        joined = " ".join(normalized).lower()
        if "arc-bench/webapp" in joined or "benchmark/tests" in joined or "evaluator/tests" in joined:
            continue
        commands.append((normalized, cwd, f"verification command {index + 1}"))
    return commands


def _package_test_commands(output_dir: Path) -> list[tuple[list[str], Path, str]]:
    commands: list[tuple[list[str], Path, str]] = []
    for cwd in (output_dir, output_dir / "frontend", output_dir / "backend"):
        package_path = cwd / "package.json"
        try:
            package = json.loads(package_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        scripts = package.get("scripts") if isinstance(package, dict) else None
        if not isinstance(scripts, dict):
            continue
        script = "test:e2e" if str(scripts.get("test:e2e") or "").strip() else "test"
        value = str(scripts.get(script) or "").strip()
        if not value or "no test specified" in value.lower():
            continue
        commands.append((["npm", "run", script], cwd, f"{cwd.name or 'root'} npm run {script}"))
    return commands


def _fallback_test_commands(output_dir: Path) -> list[tuple[list[str], Path, str]]:
    """Run conventional project tests when an older output has no manifest/scripts."""

    commands: list[tuple[list[str], Path, str]] = []
    backend = output_dir / "backend"
    if backend.is_dir():
        for path in sorted(backend.glob("test-*.mjs")) + sorted(backend.glob("test-*.js")):
            commands.append((["node", path.name], backend, f"node {path.relative_to(output_dir)}"))
    if not commands and (output_dir / "tests").is_dir():
        commands.append((["python3", "-m", "unittest", "discover", "-s", "tests"], output_dir, "python unittest discover"))
    return commands


def discover_project_test_commands(output_dir: Path, module_id: str = "ROOT") -> list[tuple[list[str], Path, str]]:
    """Return deterministic, workspace-confined verification commands."""

    return (
        _verification_commands(output_dir, module_id)
        or _package_test_commands(output_dir)
        or _fallback_test_commands(output_dir)
    )


def has_project_tests(output_dir: Path, module_id: str = "ROOT") -> bool:
    return bool(discover_project_test_commands(output_dir, module_id))


def _project_uses_playwright(output_dir: Path, commands: list[tuple[list[str], Path, str]]) -> bool:
    for package_path in (output_dir / "package.json", output_dir / "frontend" / "package.json", output_dir / "backend" / "package.json"):
        try:
            if "playwright" in package_path.read_text(encoding="utf-8", errors="ignore").lower():
                return True
        except OSError:
            pass
    for command, cwd, _label in commands:
        joined = " ".join(command).lower()
        if "playwright" in joined:
            return True
        for argument in command[1:]:
            path = (cwd / argument).resolve()
            try:
                path.relative_to(output_dir.resolve())
                if path.is_file() and "playwright" in path.read_text(encoding="utf-8", errors="ignore")[:20_000].lower():
                    return True
            except (OSError, ValueError):
                continue
    return False


def run_project_tests(
    output_dir: Path,
    *,
    task_type: str = "web",
    module_id: str = "ROOT",
    round_number: int = 1,
    smoke_port: int = 3100,
) -> dict[str, Any]:
    """Execute the generated project's test command and persist an audit result.

    The Tester agent is responsible for authoring tests and returning the semantic
    case list. This helper provides deterministic command execution and server
    lifecycle handling for the orchestrator.
    """
    result_dir = output_dir / ".arc" / "hafleet" / "test-results" / module_id
    result_dir.mkdir(parents=True, exist_ok=True)
    round_dir = result_dir / f"round-{round_number}"
    for artifact_dir in (round_dir, round_dir / "playwright-report", round_dir / "screenshots", round_dir / "traces"):
        artifact_dir.mkdir(parents=True, exist_ok=True)
    timeout = max(int(os.environ.get("HAFLEET_TEST_TIMEOUT", "600")), 1)
    env = os.environ.copy()
    env["PLAYWRIGHT_BASE_URL"] = f"http://127.0.0.1:{smoke_port}"
    env["PLAYWRIGHT_BROWSERS_PATH"] = str(output_dir / ".arc" / "hafleet" / "playwright-browsers")
    env["HAFLEET_TEST_MODULE"] = module_id
    env["HAFLEET_TEST_ROUND"] = str(round_number)
    # The runner owns this isolated smoke process, so enable the generic reset
    # contract for the duration of the test only. Production launches inherit
    # their caller's environment and keep reset disabled by default.
    env["ARC_TEST_MODE"] = "1"

    if task_type == "web":
        cwd = output_dir / "frontend"
        package_path = cwd / "package.json"
        if not cwd.is_dir() or not package_path.is_file():
            return _write_result(result_dir, round_number, {
                "verdict": "changes_requested", "summary": "frontend test project is missing",
                "findings": [{"id": f"T-{module_id}-ENV", "severity": "blocker", "title": "Test project missing", "description": "frontend/package.json is missing."}],
                "checks": [], "tests": [], "artifacts": {},
            })
        try:
            package = json.loads(package_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            package = {}
        commands = discover_project_test_commands(output_dir, module_id)
        if not commands:
            return _write_result(result_dir, round_number, {
                "verdict": "changes_requested",
                "summary": "No executable project verification command was registered",
                "findings": [{"id": f"T-{module_id}-MISSING", "severity": "major", "title": "Executable tests missing", "description": "Add project-owned tests and register their commands in .arc/hafleet/verification.json or package.json."}],
                "checks": [], "tests": [], "artifacts": {},
            })
        install_code, install_output = _run(["npm", "install", "--no-audit", "--no-fund"], cwd, env, timeout)
        checks = [{"name": "npm install", "status": "passed" if install_code == 0 else "failed", "output": install_output}]
        if install_code != 0:
            return _write_result(result_dir, round_number, {"verdict": "changes_requested", "summary": "Test dependency installation failed", "findings": [{"id": f"T-{module_id}-ENV", "severity": "blocker", "title": "Test dependencies unavailable", "description": install_output}], "checks": checks, "tests": [], "artifacts": {}})
        backend_dir = output_dir / "backend"
        if (backend_dir / "package.json").is_file():
            backend_install_code, backend_install_output = _run(["npm", "install", "--no-audit", "--no-fund"], backend_dir, env, timeout)
            checks.append({"name": "backend npm install", "status": "passed" if backend_install_code == 0 else "failed", "output": backend_install_output})
            if backend_install_code != 0:
                return _write_result(result_dir, round_number, {"verdict": "changes_requested", "summary": "Backend test dependency installation failed", "findings": [{"id": f"T-{module_id}-ENV", "severity": "blocker", "title": "Backend test dependencies unavailable", "description": backend_install_output}], "checks": checks, "tests": [], "artifacts": {}})
        playwright_enabled = _project_uses_playwright(output_dir, commands)
        playwright_cwd = next((command_cwd for _command, command_cwd, _label in commands if (command_cwd / "node_modules").exists()), cwd)
        install_browser = os.environ.get("HAFLEET_PLAYWRIGHT_INSTALL", "1").strip().lower() not in {"0", "false", "no"}
        if playwright_enabled and install_browser:
            browser_code, browser_output = _run(["npx", "playwright", "install", "chromium"], playwright_cwd, env, timeout)
            checks.append({"name": "playwright install chromium", "status": "passed" if browser_code == 0 else "failed", "output": browser_output})
            if browser_code != 0:
                return _write_result(result_dir, round_number, {"verdict": "changes_requested", "summary": "Chromium installation failed", "findings": [{"id": f"T-{module_id}-BROWSER", "severity": "blocker", "title": "Browser unavailable", "description": browser_output}], "checks": checks, "tests": [], "artifacts": {}})
        elif playwright_enabled and not install_browser:
            # Explicitly disabling installation must never silently downgrade a
            # missing browser into a normal test failure.  A no-install version
            # probe is cheap and works with both npm and pnpm projects.
            probe_code, probe_output = _run(["npx", "--no-install", "playwright", "--version"], playwright_cwd, env, min(timeout, 30))
            checks.append({"name": "playwright browser availability", "status": "passed" if probe_code == 0 else "failed", "output": probe_output})
            if probe_code != 0:
                return _write_result(result_dir, round_number, {"verdict": "changes_requested", "summary": "Playwright browser is unavailable", "findings": [{"id": f"T-{module_id}-BROWSER", "severity": "blocker", "title": "Browser unavailable", "description": probe_output or "HAFLEET_PLAYWRIGHT_INSTALL=0 and Playwright is not available."}], "checks": checks, "tests": [], "artifacts": {}})
    else:
        commands = discover_project_test_commands(output_dir, module_id)
        if not commands:
            commands = [(["python3", "-m", "unittest", "discover"], output_dir, "python unittest discover")]

    process: subprocess.Popen[str] | None = None
    backend = output_dir / "backend"
    try:
        if task_type == "web" and (backend / "package.json").is_file():
            cleanup_workspace_port(smoke_port, output_dir)
            process = subprocess.Popen(["npm", "start"], cwd=backend, env={**env, "PORT": str(smoke_port), "HOST": "127.0.0.1"}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True, start_new_session=True)
            deadline = time.monotonic() + max(int(os.environ.get("HAFLEET_READY_TIMEOUT", "45")), 1)
            while time.monotonic() < deadline and not _ready(smoke_port):
                if process.poll() is not None:
                    break
                time.sleep(0.5)
            if not _ready(smoke_port):
                output = ""
                if process.poll() is not None:
                    output = "backend process exited before smoke endpoint became ready"
                return _write_result(result_dir, round_number, {"verdict": "changes_requested", "summary": "Test server did not become ready", "findings": [{"id": f"T-{module_id}-SERVER", "severity": "blocker", "title": "Test server unavailable", "description": output}], "checks": checks, "tests": [], "artifacts": {}})
            reset_status, reset_output = _reset_test_state(smoke_port)
            checks.append({"name": "POST /api/test/reset", "status": reset_status, "output": reset_output})
            if reset_status == "failed":
                return _write_result(result_dir, round_number, {
                    "verdict": "changes_requested",
                    "summary": "Test state reset failed",
                    "findings": [{"id": f"T-{module_id}-RESET", "severity": "blocker", "title": "Test state reset failed", "description": reset_output}],
                    "checks": checks, "tests": [], "artifacts": {},
                })
        failures: list[str] = []
        browser_failure = False
        for command, command_cwd, label in commands:
            if task_type == "web":
                reset_status, reset_output = _reset_test_state(smoke_port)
                checks.append({"name": f"{label}: POST /api/test/reset", "status": reset_status, "output": reset_output})
                if reset_status == "failed":
                    failures.append(f"{label}: {reset_output}")
                    continue
            code, output = _run(command, command_cwd, env, timeout)
            checks.append({"name": label, "status": "passed" if code == 0 else "failed", "output": output})
            if code != 0:
                failures.append(f"{label} failed with exit code {code}:\n{output}")
                browser_failure = browser_failure or _browser_error(output)
        combined = "\n\n".join(failures)[-_artifact_limit():]
        failure_severity = "blocker" if browser_failure else "major"
        payload: dict[str, Any] = {"verdict": "changes_requested" if failures else "pass", "summary": "Tests failed" if failures else "Tests passed", "findings": [] if not failures else [{"id": f"T-{module_id}-RUN", "severity": failure_severity, "title": "Browser unavailable" if failure_severity == "blocker" else "Required tests failed", "description": combined}], "checks": checks, "tests": [], "artifacts": {"report": str(round_dir), "playwright_report": str(round_dir / "playwright-report"), "screenshots": str(round_dir / "screenshots"), "traces": str(round_dir / "traces")}}
        return _write_result(result_dir, round_number, payload)
    finally:
        _stop(process)
        if task_type == "web":
            cleanup_workspace_port(smoke_port, output_dir)


def _write_result(result_dir: Path, round_number: int, payload: dict[str, Any]) -> dict[str, Any]:
    path = result_dir / f"round-{round_number}.json"
    # Keep persisted audit output bounded while retaining the full structured
    # shape.  The raw command output is already truncated by _run().
    if isinstance(payload.get("checks"), list):
        for check in payload["checks"]:
            if isinstance(check, dict) and isinstance(check.get("output"), str):
                check["output"] = check["output"][-_artifact_limit():]
    try:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass
    payload["result_path"] = str(path)
    return payload


def persist_test_result(output_dir: Path, module_id: str, round_number: int, payload: dict[str, Any]) -> dict[str, Any]:
    """Persist an agent-reported result even when no central command ran.

    Web projects with ``test:e2e`` are written by :func:`run_project_tests`, but
    CLI/android projects (and test doubles) still need the same durable audit
    artifact for checkpoint recovery and Dashboard replay.
    """
    result_dir = output_dir / ".arc" / "hafleet" / "test-results" / str(module_id or "ROOT")
    result_dir.mkdir(parents=True, exist_ok=True)
    return _write_result(result_dir, round_number, payload)
