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
        scripts = package.get("scripts") if isinstance(package, dict) else {}
        command = ["npm", "run", "test:e2e"] if isinstance(scripts, dict) and scripts.get("test:e2e") else ["npm", "test"]
        if command[-1] == "test:e2e":
            try:
                retries = max(int(os.environ.get("HAFLEET_TEST_RETRIES", "1")), 0)
            except ValueError:
                retries = 1
            command.extend(["--", "--retries", str(retries)])
        install_code, install_output = _run(["npm", "install", "--no-audit", "--no-fund"], cwd, env, timeout)
        checks = [{"name": "npm install", "status": "passed" if install_code == 0 else "failed", "output": install_output}]
        if install_code != 0:
            return _write_result(result_dir, round_number, {"verdict": "changes_requested", "summary": "Test dependency installation failed", "findings": [{"id": f"T-{module_id}-ENV", "severity": "blocker", "title": "Test dependencies unavailable", "description": install_output}], "checks": checks, "tests": [], "artifacts": {}})
        playwright_enabled = "playwright" in json.dumps(package).lower()
        install_browser = os.environ.get("HAFLEET_PLAYWRIGHT_INSTALL", "1").strip().lower() not in {"0", "false", "no"}
        if playwright_enabled and install_browser:
            browser_code, browser_output = _run(["npx", "playwright", "install", "chromium"], cwd, env, timeout)
            checks.append({"name": "playwright install chromium", "status": "passed" if browser_code == 0 else "failed", "output": browser_output})
            if browser_code != 0:
                return _write_result(result_dir, round_number, {"verdict": "changes_requested", "summary": "Chromium installation failed", "findings": [{"id": f"T-{module_id}-BROWSER", "severity": "blocker", "title": "Browser unavailable", "description": browser_output}], "checks": checks, "tests": [], "artifacts": {}})
        elif playwright_enabled and not install_browser:
            # Explicitly disabling installation must never silently downgrade a
            # missing browser into a normal test failure.  A no-install version
            # probe is cheap and works with both npm and pnpm projects.
            probe_code, probe_output = _run(["npx", "--no-install", "playwright", "--version"], cwd, env, min(timeout, 30))
            checks.append({"name": "playwright browser availability", "status": "passed" if probe_code == 0 else "failed", "output": probe_output})
            if probe_code != 0:
                return _write_result(result_dir, round_number, {"verdict": "changes_requested", "summary": "Playwright browser is unavailable", "findings": [{"id": f"T-{module_id}-BROWSER", "severity": "blocker", "title": "Browser unavailable", "description": probe_output or "HAFLEET_PLAYWRIGHT_INSTALL=0 and Playwright is not available."}], "checks": checks, "tests": [], "artifacts": {}})
    else:
        cwd = output_dir
        command = ["npm", "test"] if (cwd / "package.json").is_file() else ["python3", "-m", "unittest", "discover"]

    process: subprocess.Popen[str] | None = None
    backend = output_dir / "backend"
    try:
        if task_type == "web" and (backend / "package.json").is_file():
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
        code, output = _run(command, cwd, env, timeout)
        checks.append({"name": " ".join(command), "status": "passed" if code == 0 else "failed", "output": output})
        failure_severity = "blocker" if code != 0 and _browser_error(output) else "major"
        payload: dict[str, Any] = {"verdict": "pass" if code == 0 else "changes_requested", "summary": "Tests passed" if code == 0 else "Tests failed", "findings": [] if code == 0 else [{"id": f"T-{module_id}-RUN", "severity": failure_severity, "title": "Browser unavailable" if failure_severity == "blocker" else "Required tests failed", "description": output}], "checks": checks, "tests": [], "artifacts": {"report": str(round_dir), "playwright_report": str(round_dir / "playwright-report"), "screenshots": str(round_dir / "screenshots"), "traces": str(round_dir / "traces")}}
        return _write_result(result_dir, round_number, payload)
    finally:
        _stop(process)


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
