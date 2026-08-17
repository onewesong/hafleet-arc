from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .models import RequirementModule


def load_requirement_tree(requirements_dir: Path) -> dict[str, Any]:
    requirements_path = requirements_dir / "requirements.yaml"
    if not requirements_path.is_file():
        raise FileNotFoundError(f"requirements.yaml not found: {requirements_path}")

    payload = yaml.safe_load(requirements_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or str(payload.get("id") or "").strip() != "ROOT":
        raise ValueError("requirements.yaml must contain a ROOT mapping")
    children = payload.get("children")
    if not isinstance(children, list) or not any(isinstance(item, dict) for item in children):
        raise ValueError("ROOT must contain at least one child module")
    return payload


def _dependencies(node: dict[str, Any]) -> tuple[str, ...]:
    value = node.get("dependencies")
    if not isinstance(value, list):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


def plan_modules(requirement_tree: dict[str, Any]) -> list[RequirementModule]:
    """Build a stable, dependency-aware plan for direct ROOT children.

    ARC requirement trees occasionally express dependencies on descendants rather
    than on another direct ROOT child. Those references do not constrain this
    top-level plan. Cycles also fall back to source order instead of deadlocking a
    benchmark run.
    """

    children = [item for item in requirement_tree.get("children", []) if isinstance(item, dict)]
    nodes: list[tuple[str, str, dict[str, Any], tuple[str, ...]]] = []
    for source_index, subtree in enumerate(children, start=1):
        node_id = str(subtree.get("id") or subtree.get("req_id") or "").strip()
        if not node_id:
            raise ValueError(f"ROOT child {source_index} has no id")
        nodes.append(
            (
                node_id,
                str(subtree.get("name") or node_id).strip(),
                subtree,
                _dependencies(subtree),
            )
        )

    direct_ids = {node_id for node_id, _, _, _ in nodes}
    pending = list(nodes)
    ordered: list[tuple[str, str, dict[str, Any], tuple[str, ...]]] = []
    completed: set[str] = set()
    while pending:
        ready_index = next(
            (
                index
                for index, (node_id, _name, _tree, dependencies) in enumerate(pending)
                if all(dep not in direct_ids or dep in completed for dep in dependencies if dep != node_id)
            ),
            None,
        )
        if ready_index is None:
            ordered.extend(pending)
            break
        current = pending.pop(ready_index)
        ordered.append(current)
        completed.add(current[0])

    total = len(ordered)
    return [
        RequirementModule(
            index=index,
            total=total,
            node_id=node_id,
            name=name,
            subtree=subtree,
            dependencies=dependencies,
        )
        for index, (node_id, name, subtree, dependencies) in enumerate(ordered, start=1)
    ]

