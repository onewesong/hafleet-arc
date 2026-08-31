"""Requirement-to-capability normalization for agent prompts.

The normalizer deliberately consumes only the ARC requirement tree.  It does not
inspect benchmark/grading tests, making the resulting model useful for arbitrary
domains while giving the implementer a compact, traceable execution contract.
"""

from __future__ import annotations

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


def _scenario(scenario: dict[str, Any], fallback_id: str, index: int) -> dict[str, Any]:
    scenario_id = _text(scenario.get("id") or scenario.get("scenario_id")) or f"{fallback_id}-S{index:03d}"
    name = _text(scenario.get("name") or scenario.get("title")) or scenario_id
    steps = scenario.get("steps") or scenario.get("actions") or []
    normalized_steps: list[dict[str, str]] = []
    if isinstance(steps, list):
        for step in steps[:12]:
            if isinstance(step, dict):
                normalized_steps.append(
                    {
                        "action": _text(step.get("action") or step.get("when") or step.get("do")),
                        "expected": _text(step.get("expected") or step.get("then") or step.get("result")),
                    }
                )
            elif _text(step):
                normalized_steps.append({"action": _text(step), "expected": ""})
    expected = _strings(scenario.get("expected") or scenario.get("outcomes") or scenario.get("acceptance"), 6)
    return {"id": scenario_id, "name": name, "steps": normalized_steps, "expected": expected}


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
        requirements.append({"id": req_id, "title": title, "description": description, "acceptance": criteria[:12], "scenarios": scenarios[:12]})
        children = node.get("children") or node.get("requirements") or []
        if isinstance(children, list):
            for child in children:
                if isinstance(child, dict):
                    visit(child, req_id)

    visit(tree)
    return {
        "source": "arc_requirement_tree",
        "requirements": requirements,
        "coverage_rules": [
            "Trace every leaf requirement and scenario to an implementation behavior and executable test.",
            "For each user-visible flow cover success, invalid input, empty state, failure response, and refresh/persistence where applicable.",
            "For each API cover input validation, authorization, success response, error status, and persistence where applicable.",
            "Do not invent hidden acceptance-test details; derive behavior only from the supplied requirements and observable contracts.",
        ],
    }


__all__ = ["build_capability_model"]
