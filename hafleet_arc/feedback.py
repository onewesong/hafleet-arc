from __future__ import annotations

import hashlib
import json
import re
from typing import Any


SEVERITIES = {"blocker", "major", "minor", "info"}
TEST_STATUSES = {"passed", "pass", "success", "ok", "failed", "fail", "error", "timeout", "skipped", "pending"}


def _json_candidate(text: str) -> dict[str, Any] | None:
    value = str(text or "").strip()
    if not value:
        return None
    candidates = [value]
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", value, re.DOTALL | re.IGNORECASE)
    if match:
        candidates.insert(0, match.group(1))
    start, end = value.find("{"), value.rfind("}")
    if start >= 0 and end > start:
        candidates.append(value[start:end + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def parse_review(text: str | None) -> dict[str, Any]:
    parsed = _json_candidate(str(text or "")) or {}
    findings: list[dict[str, Any]] = []
    for index, raw in enumerate(parsed.get("findings") or []):
        if not isinstance(raw, dict):
            continue
        severity = str(raw.get("severity") or "major").strip().lower()
        if severity not in SEVERITIES:
            severity = "major"
        finding = dict(raw)
        finding.setdefault("id", f"F-{index + 1:03d}")
        finding["severity"] = severity
        finding.setdefault("title", "Review finding")
        finding.setdefault("description", "")
        findings.append(finding)
    verdict = str(parsed.get("verdict") or "changes_requested").strip().lower()
    if verdict not in {"pass", "changes_requested"}:
        verdict = "changes_requested"
    checks: list[dict[str, Any]] = []
    for raw in parsed.get("checks") or []:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        # Reviewer prompts historically used both ``status`` and ``result``.
        # Normalize at the protocol boundary so a successful static contract
        # check is not accidentally treated as a failed implementation check.
        if not str(item.get("status") or "").strip() and "result" in item:
            item["status"] = item.get("result")
        checks.append(item)
    return {
        "verdict": verdict,
        "summary": str(parsed.get("summary") or ("Review passed" if verdict == "pass" else "Changes requested")),
        "findings": findings,
        "checks": checks,
        "raw": str(text or ""),
    }


def blocking_findings(review: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in review.get("findings", []) if item.get("severity") in {"blocker", "major"}]


def review_passes(review: dict[str, Any]) -> bool:
    checks = review.get("checks") or []
    passed_statuses = {"passed", "pass", "success", "ok"}
    failed_checks = [
        item for item in checks
        if (
            str(item.get("status", "")).strip().lower() not in passed_statuses
            and not str(item.get("status", "")).strip().lower().startswith("passed_")
        )
    ]
    # Only blocker/major findings are gate conditions. Reviewers sometimes retain
    # ``changes_requested`` while reporting only minor/info observations; treating
    # that wording as a hard failure wastes unattended repair rounds and contradicts
    # the pipeline contract. Once all required checks pass and no blocking finding
    # remains, the review is safe to advance while preserving the non-blocking notes
    # in the audit log and Dashboard.
    verdict = str(review.get("verdict") or "").strip().lower()
    return verdict in {"pass", "changes_requested"} and not blocking_findings(review) and not failed_checks


def contract_review_passes(review: dict[str, Any], machine_gaps: list[dict[str, Any]] | None = None) -> bool:
    """Return whether a pre-implementation contract is ready for implementation.

    Contract review is a static gate: unlike the later implementation review it
    must receive an explicit ``pass`` verdict, but it does not require executable
    test output. Any deterministic validator gap remains blocking regardless of
    model wording.
    """

    explicitly_failed_checks = []
    for item in review.get("checks") or []:
        status = str(item.get("status") or item.get("result") or "").strip().lower()
        if status in {"failed", "fail", "error", "timeout", "blocked"} or status.startswith(("failed_", "error_", "timeout_")):
            explicitly_failed_checks.append(item)
    return (
        not (machine_gaps or [])
        and str(review.get("verdict") or "").strip().lower() == "pass"
        and not blocking_findings(review)
        and not explicitly_failed_checks
    )


def review_hash(review: dict[str, Any]) -> str:
    payload = {
        "verdict": review.get("verdict"),
        "findings": review.get("findings", []),
        "checks": review.get("checks", []),
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def parse_test_result(text: str | None, *, module_id: str = "", round_number: int = 0) -> dict[str, Any]:
    """Parse and normalize a Tester JSON response."""
    raw_text = str(text or "")
    parsed = _json_candidate(raw_text)
    malformed = bool(raw_text.strip()) and parsed is None
    parsed = parsed or {}
    tests: list[dict[str, Any]] = []
    for index, raw in enumerate(parsed.get("tests") or []):
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        item.setdefault("id", f"T-{module_id or 'ROOT'}-{index + 1:03d}")
        item["status"] = str(item.get("status") or "pending").strip().lower()
        if item["status"] not in TEST_STATUSES:
            item["status"] = "error"
        item.setdefault("requirement_ids", [module_id] if module_id else [])
        if not isinstance(item.get("requirement_ids"), list):
            item["requirement_ids"] = [str(item["requirement_ids"])] if item.get("requirement_ids") else ([module_id] if module_id else [])
        tests.append(item)
    findings: list[dict[str, Any]] = []
    for index, raw in enumerate(parsed.get("findings") or []):
        if not isinstance(raw, dict):
            continue
        finding = dict(raw)
        finding.setdefault("id", f"T-{module_id or 'ROOT'}-F{index + 1:03d}")
        severity = str(finding.get("severity") or "major").strip().lower()
        finding["severity"] = severity if severity in SEVERITIES else "major"
        finding.setdefault("title", "Test finding")
        finding.setdefault("description", "")
        findings.append(finding)
    # A tester response must be machine-readable.  Treat malformed output as a
    # blocking infrastructure/protocol failure instead of silently approving it.
    if malformed:
        findings.append({
            "id": f"T-{module_id or 'ROOT'}-PROTOCOL",
            "severity": "blocker",
            "title": "Tester returned invalid JSON",
            "description": "The tester response did not contain a valid structured JSON result.",
        })
    existing_finding_ids = {str(item.get("test_id")) for item in findings if item.get("test_id")}
    for test in tests:
        status = str(test.get("status") or "pending").lower()
        test_id = str(test.get("id") or "")
        finding_base = test_id or f"T-{module_id or 'ROOT'}-{len(findings) + 1:03d}"
        if status in {"failed", "fail", "error", "timeout"} and test_id not in existing_finding_ids:
            findings.append({
                "id": f"{finding_base}-FAIL",
                "severity": "major",
                "title": f"Required test {test_id or 'case'} did not pass",
                "description": str(test.get("error") or test.get("actual") or "The test failed."),
                "test_id": test_id,
                "requirement_ids": test.get("requirement_ids", []),
            })
        if status == "skipped" and not str(test.get("reason") or test.get("skip_reason") or "").strip():
            findings.append({
                "id": f"{finding_base}-SKIP",
                "severity": "major",
                "title": f"Skipped test {test_id or 'case'} has no reason",
                "description": "Skipped tests must include a reproducible reason.",
                "test_id": test_id,
            })
    for check in [item for item in (parsed.get("checks") or []) if isinstance(item, dict)]:
        status = str(check.get("status") or "").lower()
        name = str(check.get("name") or "").lower()
        if status not in {"passed", "pass", "success", "ok"} and any(marker in name for marker in ("browser", "chromium", "playwright install", "npm install", "test server")):
            findings.append({
                "id": f"T-{module_id or 'ROOT'}-ENV-{len(findings) + 1:03d}",
                "severity": "blocker",
                "title": "Test environment unavailable",
                "description": str(check.get("output") or check.get("error") or check.get("name") or "Test environment check failed."),
            })
    verdict = str(parsed.get("verdict") or "changes_requested").strip().lower()
    if verdict not in {"pass", "changes_requested"}:
        verdict = "changes_requested"
    try:
        parsed_round = int(parsed.get("round") or round_number or 0)
    except (TypeError, ValueError):
        parsed_round = int(round_number or 0)
    result = dict(parsed)
    result.update({
        "verdict": verdict,
        "summary": str(parsed.get("summary") or ("Tests passed" if verdict == "pass" else "Tests failed")),
        "module_id": str(parsed.get("module_id") or module_id),
        "round": parsed_round,
        "tests": tests,
        "findings": findings,
        "checks": [item for item in (parsed.get("checks") or []) if isinstance(item, dict)],
        "artifacts": parsed.get("artifacts") if isinstance(parsed.get("artifacts"), dict) else {},
        "raw": raw_text,
    })
    return result


def test_blocking_findings(result: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in result.get("findings", []) if item.get("severity") in {"blocker", "major"}]


def test_passes(result: dict[str, Any]) -> bool:
    tests = result.get("tests") or []
    checks = result.get("checks") or []
    failed_tests = [item for item in tests if str(item.get("status", "")).lower() not in {"passed", "pass", "success", "ok"}]
    # Optional compatibility probes (for example an absent legacy reset endpoint)
    # may be explicitly skipped with an explanation. They are audit evidence, not a
    # failed required check. Individual skipped test cases remain governed by
    # parse_test_result(), which turns an unexplained skip into a major finding.
    failed_checks = [item for item in checks if str(item.get("status", "")).lower() not in {"passed", "pass", "success", "ok", "skipped", "skip"}]
    return result.get("verdict") == "pass" and not test_blocking_findings(result) and not failed_tests and not failed_checks


def test_hash(result: dict[str, Any]) -> str:
    # Command output commonly contains durations, temporary paths, download progress,
    # and other nondeterministic text. No-progress detection must compare semantic
    # outcomes rather than those volatile diagnostics.
    payload = {
        "verdict": result.get("verdict"),
        "tests": [
            {
                "id": item.get("id"),
                "status": item.get("status"),
                "requirement_ids": item.get("requirement_ids", []),
            }
            for item in (result.get("tests") or [])
            if isinstance(item, dict)
        ],
        "findings": [
            {
                "id": item.get("id"),
                "severity": item.get("severity"),
                "title": item.get("title"),
            }
            for item in (result.get("findings") or [])
            if isinstance(item, dict)
        ],
        "checks": [
            {"name": item.get("name"), "status": item.get("status")}
            for item in (result.get("checks") or [])
            if isinstance(item, dict)
        ],
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
