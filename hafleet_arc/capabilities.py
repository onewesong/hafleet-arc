"""Requirement-to-capability normalization for agent prompts.

The normalizer deliberately consumes only the ARC requirement tree.  It does not
inspect benchmark/grading tests, making the resulting model useful for arbitrary
domains while giving the implementer a compact, traceable execution contract.
"""

from __future__ import annotations

import re
from typing import Any, Iterable


_TEXT_KEYS = ("description", "text", "statement", "details", "expected", "acceptance")
_CRITERIA_KEYS = ("acceptance_criteria", "acceptanceCriteria", "criteria", "acceptance")


def _text(value: Any, limit: int = 600) -> str:
    if isinstance(value, str):
        return " ".join(value.split())[:limit]
    if isinstance(value, (int, float, bool)):
        return str(value)
    return ""


def _strings(value: Any, limit: int = 8) -> list[str]:
    if isinstance(value, str):
        return [_text(value)] if _text(value) else []
    if isinstance(value, (list, tuple)):
        result: list[str] = []
        for item in value:
            if isinstance(item, dict):
                item_text = next((_text(item.get(key)) for key in _TEXT_KEYS if _text(item.get(key))), "")
            else:
                item_text = _text(item)
            if item_text:
                result.append(item_text)
            if len(result) >= limit:
                break
        return result
    return []


def _references(*values: Any, limit: int = 8) -> list[str]:
    """Extract author-supplied visual/reference paths without reading the files."""
    found: list[str] = []
    pattern = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
    for value in values:
        text = value if isinstance(value, str) else ""
        for path in pattern.findall(text):
            path = path.strip()
            if path and path not in found:
                found.append(path)
            if len(found) >= limit:
                return found
    return found


def _scenario(scenario: dict[str, Any], fallback_id: str, index: int) -> dict[str, Any]:
    scenario_id = _text(scenario.get("id") or scenario.get("scenario_id")) or f"{fallback_id}-S{index:03d}"
    name = _text(scenario.get("name") or scenario.get("title")) or scenario_id
    steps = scenario.get("steps") or scenario.get("actions") or []
    normalized_steps: list[dict[str, str]] = []
    if isinstance(steps, list):
        for step in steps[:12]:
            if isinstance(step, dict):
                keyword = _text(step.get("keyword") or step.get("type"))
                content = _text(step.get("content") or step.get("text"))
                action = _text(step.get("action") or step.get("when") or step.get("do"))
                expected = _text(step.get("expected") or step.get("then") or step.get("result"))
                # ARC requirement YAML commonly represents Gherkin steps as
                # {keyword, content}; retain them instead of silently dropping
                # the actual behavior statement.
                if not action and content and keyword.upper() not in {"THEN", "AND_THEN"}:
                    action = content
                if not expected and content and keyword.upper() in {"THEN", "AND_THEN"}:
                    expected = content
                normalized_steps.append({"keyword": keyword, "action": action, "expected": expected, "content": content})
            elif _text(step):
                normalized_steps.append({"action": _text(step), "expected": ""})
    expected = _strings(scenario.get("expected") or scenario.get("outcomes") or scenario.get("acceptance"), 6)
    return {
        "id": scenario_id,
        "name": name,
        "steps": normalized_steps,
        "expected": expected,
        "references": _references(scenario.get("description"), scenario.get("text")),
    }


def build_capability_model(tree: dict[str, Any], *, max_requirements: int = 80) -> dict[str, Any]:
    """Return a bounded, deterministic capability matrix from any requirement tree."""

    requirements: list[dict[str, Any]] = []

    def visit(node: dict[str, Any], parent_id: str = "") -> None:
        if len(requirements) >= max_requirements:
            return
        req_id = _text(node.get("id") or node.get("req_id") or node.get("requirement_id"))
        if not req_id:
            req_id = parent_id or "REQUIREMENT"
        title = _text(node.get("name") or node.get("title")) or req_id
        description = next((_text(node.get(key)) for key in _TEXT_KEYS if _text(node.get(key))), "")
        criteria: list[str] = []
        for key in _CRITERIA_KEYS:
            criteria.extend(_strings(node.get(key), 8))
        scenarios_raw = node.get("scenarios") or node.get("use_cases") or []
        scenarios = [_scenario(item, req_id, i) for i, item in enumerate(scenarios_raw, 1) if isinstance(item, dict)] if isinstance(scenarios_raw, list) else []
        # Keep this contract domain-neutral.  It gives an agent an observable
        # boundary checklist without revealing (or depending on) any evaluator
        # implementation details.
        requirements.append(
            {
                "id": req_id,
                "title": title,
                "description": description,
                "references": _references(node.get("description"), node.get("text"), node.get("acceptance")),
                "acceptance": criteria[:12],
                "scenarios": scenarios[:12],
                "observable_contract": {
                    "actor_and_preconditions": "Identify the actor, required permissions, and initial state.",
                    "success": "Describe the visible response and durable state after a valid action.",
                    "invalid_and_failure": "Define validation, conflict, unauthorized/not-found, and server failure behavior when applicable.",
                    "navigation_and_refresh": "Define a stable public entry point and behavior after direct navigation or refresh for web flows.",
                    "persistence": "Verify state survives the supported storage boundary or process restart when the requirement implies persistence.",
                    "ui_api_parity": "The public UI and API must expose the same state transition, result data, and structured errors.",
                },
            }
        )
        children = node.get("children") or node.get("requirements") or []
        if isinstance(children, list):
            for child in children:
                if isinstance(child, dict):
                    visit(child, req_id)

    visit(tree)
    return {
        "source": "arc_requirement_tree",
        "requirements": requirements,
        # This is a domain-neutral browser/API contract.  It gives an agent a
        # stable interoperability target without revealing evaluator details or
        # prescribing product-specific selectors.
        "web_contract": {
            "routing": [
                "Every requirement-mentioned page has a stable canonical URL path.",
                "Opening a deep URL directly and refreshing it renders the same page; the server falls back to the app shell for client routes.",
                "Navigation uses real links/buttons with accessible names; hash-only navigation is not the sole way to reach a page.",
                "Each route has one canonical spelling and normalizes trailing slashes, query defaults, and legacy aliases without losing state.",
                "Authenticated routes have an explicit unauthenticated redirect or visible access-denied state.",
            ],
            "forms": [
                "Every input, select, radio group, checkbox, and date control has a visible label or equivalent accessible name.",
                "Required fields expose required semantics and deterministic validation messages without losing entered values.",
                "Validation is enforced at the API boundary as well as the UI; errors identify the field or business conflict and use a non-2xx status for rejected mutations.",
                "Submit controls expose a stable accessible name and prevent duplicate submissions while pending.",
            ],
            "api": [
                "JSON endpoints use consistent success and error envelopes with appropriate HTTP status codes.",
                "Mutations validate input, persist state, and return the resulting resource or a structured error.",
                "Loading, empty, error, and retry states are observable in the UI for asynchronous data.",
            ],
            "state": [
                "Successful auth and mutations update navigation and visible state immediately.",
                "Refresh/reload reconstructs state from the public API or durable storage rather than in-memory-only fixtures.",
                "Every async view has explicit loading, empty, recoverable error, and retry states; stale data is not presented as a successful response.",
                "Logout clears client credentials and protects authenticated views after reload.",
            ],
        },
        "coverage_rules": [
            "Trace every leaf requirement and scenario to an implementation behavior and executable test.",
            "For each user-visible flow cover success, invalid input, empty state, failure response, and refresh/persistence where applicable.",
            "For each API cover input validation, authorization, success response, error status, and persistence where applicable.",
            "When requirements mention seeded/example records, verify a fresh process bootstraps deterministic app-owned data without evaluator setup.",
            "When author-provided visual references exist, verify the corresponding observable layout/content states without relying on hidden selectors.",
            "Do not invent hidden acceptance-test details; derive behavior only from the supplied requirements and observable contracts.",
        ],
    }


__all__ = ["build_capability_model"]
