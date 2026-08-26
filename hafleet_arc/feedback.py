from __future__ import annotations

import hashlib
import json
import re
from typing import Any


SEVERITIES = {"blocker", "major", "minor", "info"}


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
    checks = [item for item in (parsed.get("checks") or []) if isinstance(item, dict)]
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
        if str(item.get("status", "")).strip().lower() not in passed_statuses
    ]
    return review.get("verdict") == "pass" and not blocking_findings(review) and not failed_checks


def review_hash(review: dict[str, Any]) -> str:
    payload = {
        "verdict": review.get("verdict"),
        "findings": review.get("findings", []),
        "checks": review.get("checks", []),
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
