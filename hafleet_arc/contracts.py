from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


_GENERIC_CONTRACT_TEXT = re.compile(
    r"^(?:(?:assert|verify|check)(?:\s+that)?\s+)?(?:the\s+)?"
    r"(?:(?:public|expected|visible|correct)\s+)?"
    r"(?:result|behavior|outcome|state|content)"
    r"(?:\s+(?:is\s+(?:visible|correct)|works|matches\s+(?:the\s+)?requirements?|persists))?[.!]?$",
    re.IGNORECASE,
)


def _generic_contract_value(value: object) -> bool:
    return bool(_GENERIC_CONTRACT_TEXT.fullmatch(" ".join(str(value or "").strip().split())))


def _text_steps(scenario: dict[str, Any], keyword: str) -> list[str]:
    values: list[str] = []
    for step in scenario.get("steps") or []:
        if not isinstance(step, dict):
            continue
        if str(step.get("keyword") or "").strip().upper() != keyword:
            continue
        content = str(step.get("content") or "").strip()
        if content:
            values.append(content)
    return values


def scenario_contracts(requirement_subtree: dict[str, Any]) -> list[dict[str, Any]]:
    """Build a deterministic contract row for every supplied public scenario."""

    rows: list[dict[str, Any]] = []

    def visit(node: object) -> None:
        if not isinstance(node, dict):
            return
        children = node.get("children") or node.get("requirements") or []
        if isinstance(children, list) and children:
            for child in children:
                visit(child)
            return
        requirement_id = str(
            node.get("id") or node.get("req_id") or node.get("requirement_id") or ""
        ).strip()
        scenarios = node.get("scenarios") or []
        if not isinstance(scenarios, list):
            return
        for index, raw_scenario in enumerate(scenarios, 1):
            scenario = raw_scenario if isinstance(raw_scenario, dict) else {}
            rows.append(
                {
                    "scenario_id": f"{requirement_id}-S{index:03d}" if requirement_id else f"S{len(rows) + 1:03d}",
                    "requirement_id": requirement_id,
                    "name": str(scenario.get("name") or f"Scenario {index}").strip(),
                    "given": _text_steps(scenario, "GIVEN"),
                    "when": _text_steps(scenario, "WHEN"),
                    "then": _text_steps(scenario, "THEN"),
                    "planned_files": [],
                    "observable_checks": [],
                    "canonical_url": "",
                    "durable_state": "",
                    "test_id": "",
                    "assertions": [],
                }
            )

    visit(requirement_subtree)
    return rows


def ensure_contract_file(path: Path, module_id: str, requirement_subtree: dict[str, Any]) -> dict[str, Any]:
    """Create a scenario-complete planning contract without overwriting agent work."""

    if path.is_file():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = None
        if isinstance(payload, dict) and isinstance(payload.get("scenarios"), list):
            return payload
    payload = {
        "version": 1,
        "module_id": module_id,
        "status": "draft",
        "scenarios": scenario_contracts(requirement_subtree),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload


def contract_gaps(payload: dict[str, Any], requirement_subtree: dict[str, Any]) -> list[dict[str, str]]:
    """Return concrete planning gaps that must be resolved before implementation."""

    expected_rows = {row["scenario_id"]: row for row in scenario_contracts(requirement_subtree)}
    expected = set(expected_rows)
    raw_rows = payload.get("scenarios")
    rows = raw_rows if isinstance(raw_rows, list) else []
    actual_ids = [
        str(row.get("scenario_id") or "").strip()
        for row in rows
        if isinstance(row, dict) and str(row.get("scenario_id") or "").strip()
    ]
    actual = set(actual_ids)
    gaps: list[dict[str, str]] = []
    for scenario_id in sorted(expected - actual):
        gaps.append({"scenario_id": scenario_id, "field": "scenario", "message": "Scenario is missing from the contract."})
    for scenario_id in sorted(actual - expected):
        gaps.append({"scenario_id": scenario_id, "field": "scenario", "message": "Scenario is not present in the supplied requirement subtree."})
    for scenario_id in sorted({item for item in actual_ids if actual_ids.count(item) > 1}):
        gaps.append({"scenario_id": scenario_id, "field": "scenario", "message": "Scenario appears more than once in the contract."})
    test_ids: list[tuple[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        scenario_id = str(row.get("scenario_id") or "unknown").strip()
        for field, message in (
            ("planned_files", "No implementation file or component is planned."),
            ("observable_checks", "No public observable check is defined."),
            ("test_id", "No executable test ID is assigned."),
            ("assertions", "No concrete test assertion is defined."),
        ):
            value = row.get(field)
            missing = not value if field == "test_id" else not isinstance(value, list) or not value
            if missing:
                gaps.append({"scenario_id": scenario_id, "field": field, "message": message})
        expected_row = expected_rows.get(scenario_id)
        if expected_row:
            for field in ("given", "when", "then"):
                if row.get(field) != expected_row.get(field):
                    gaps.append(
                        {
                            "scenario_id": scenario_id,
                            "field": field,
                            "message": f"Original {field.upper()} steps were changed or omitted.",
                        }
                    )
        test_id = str(row.get("test_id") or "").strip()
        if test_id:
            test_ids.append((scenario_id, test_id))
        canonical_url = str(row.get("canonical_url") or "").strip()
        if not canonical_url:
            gaps.append({"scenario_id": scenario_id, "field": "canonical_url", "message": "No canonical URL or not_applicable marker is defined."})
        elif canonical_url.lower() != "not_applicable":
            if not canonical_url.startswith("/"):
                gaps.append({"scenario_id": scenario_id, "field": "canonical_url", "message": "Canonical URL must be an application path or not_applicable."})
            if any(marker in canonical_url.lower() for marker in ("...", "yyyy-mm-dd", "tbd", "todo")) or "|" in canonical_url:
                gaps.append({"scenario_id": scenario_id, "field": "canonical_url", "message": "Canonical URL contains an unresolved placeholder or multiple alternatives."})
        durable_state = str(row.get("durable_state") or "").strip()
        if not durable_state:
            gaps.append({"scenario_id": scenario_id, "field": "durable_state", "message": "No durable-state outcome or not_applicable marker is defined."})
        for field in ("planned_files", "observable_checks", "assertions"):
            values = row.get(field)
            if not isinstance(values, list):
                continue
            for value in values:
                text = str(value or "").strip()
                if not text or re.search(r"(?:\.\.\.|\bTBD\b|\bTODO\b)", text, re.IGNORECASE):
                    gaps.append({"scenario_id": scenario_id, "field": field, "message": f"{field} contains an empty or unresolved placeholder value."})
                    break
                if field in {"observable_checks", "assertions"} and _generic_contract_value(text):
                    gaps.append(
                        {
                            "scenario_id": scenario_id,
                            "field": field,
                            "message": f"{field} contains a generic assertion without a concrete public value or transition.",
                        }
                    )
                    break
    # Bulk-generated contracts can evade per-value placeholder checks by repeating
    # one plausible sentence for many unrelated scenarios. Three or more identical
    # assertion sets are an actionable planning gap; scenario-specific checks should
    # name the actual control, value, route, API outcome, or durable transition.
    for field in ("observable_checks", "assertions"):
        signatures: dict[tuple[str, ...], list[str]] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            values = row.get(field)
            if not isinstance(values, list) or not values:
                continue
            signature = tuple(" ".join(str(value or "").lower().split()) for value in values)
            signatures.setdefault(signature, []).append(str(row.get("scenario_id") or "unknown"))
        for scenario_ids in signatures.values():
            if len(scenario_ids) < 3:
                continue
            for scenario_id in scenario_ids:
                gaps.append(
                    {
                        "scenario_id": scenario_id,
                        "field": field,
                        "message": f"{field} repeats the same template across {len(scenario_ids)} scenarios instead of defining scenario-specific evidence.",
                    }
                )
    duplicate_test_ids = sorted({test_id for _, test_id in test_ids if sum(1 for _, value in test_ids if value == test_id) > 1})
    for test_id in duplicate_test_ids:
        for scenario_id, value in test_ids:
            if value == test_id:
                gaps.append({"scenario_id": scenario_id, "field": "test_id", "message": f"Test ID is not unique: {test_id}."})
    return gaps
