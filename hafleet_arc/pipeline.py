from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from collections.abc import Callable, Mapping

import yaml


SUPPORTED_OPERATIONS = {"validate", "postflight", "checkpoint", "commit", "merge", "parallel_map"}
DEFAULT_PIPELINE_PATH = Path(__file__).with_name("pipeline.yaml")


@dataclass(frozen=True)
class PipelineNode:
    id: str
    type: str
    role: str = ""
    operation: str = ""
    review: str = ""
    repair: str = ""
    until: str = ""
    max_rounds: int = 3
    options: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Pipeline:
    version: int
    nodes: tuple[PipelineNode, ...]
    roles: dict[str, str] = field(default_factory=dict)
    default_prompt: str = ""

    def node(self, node_id: str) -> PipelineNode | None:
        return next((node for node in self.nodes if node.id == node_id), None)

    def loop(self) -> PipelineNode:
        for node in self.nodes:
            if node.type == "loop":
                return node
        raise ValueError("pipeline has no loop node")

    def role_for(self, node_id: str, fallback: str) -> str:
        node = self.node(node_id) or next(
            (candidate for candidate in self.nodes if candidate.type == "agent" and candidate.role == fallback),
            None,
        )
        return node.role if node and node.role else fallback

    def operation(self, name: str) -> PipelineNode | None:
        return next((node for node in self.nodes if node.type == "operation" and node.operation == name), None)

    def prompt_for(self, role: str) -> str:
        return self.roles.get(str(role or "").strip().lower(), "")


class PipelineExecutor:
    """Small callback-driven executor for declarative pipeline nodes.

    The ARC orchestrator supplies its own agent/operation implementations, while
    this class keeps node ordering and context propagation reusable for future
    roles and embedding environments. A callback receives the node and the
    mutable context and may return a value; that value is stored under the node
    ID for downstream references.
    """

    def __init__(
        self,
        pipeline: Pipeline,
        *,
        agent: Callable[[PipelineNode, dict[str, Any]], Any] | None = None,
        operation: Callable[[PipelineNode, dict[str, Any]], Any] | None = None,
        loop: Callable[[PipelineNode, dict[str, Any]], Any] | None = None,
    ) -> None:
        self.pipeline = pipeline
        self.agent = agent
        self.operation_runner = operation
        self.loop_runner = loop

    @staticmethod
    def _resolve(value: Any, context: Mapping[str, Any]) -> Any:
        if not isinstance(value, str) or not value.startswith("$"):
            return value
        current: Any = context
        for part in value[1:].split("."):
            if isinstance(current, Mapping):
                current = current.get(part)
            else:
                return None
        return current

    def run(self, context: Mapping[str, Any] | None = None) -> dict[str, Any]:
        values: dict[str, Any] = dict(context or {})
        for node in self.pipeline.nodes:
            node_context = dict(values)
            inputs = node.options.get("inputs", node.options.get("input", {}))
            if isinstance(inputs, Mapping):
                node_context["inputs"] = {key: self._resolve(value, values) for key, value in inputs.items()}
            if node.type == "agent":
                if self.agent is None:
                    raise RuntimeError("pipeline agent callback is not configured")
                result = self.agent(node, node_context)
            elif node.type == "operation":
                if self.operation_runner is None:
                    raise RuntimeError("pipeline operation callback is not configured")
                result = self.operation_runner(node, node_context)
            else:
                if self.loop_runner is None:
                    raise RuntimeError("pipeline loop callback is not configured")
                result = self.loop_runner(node, node_context)
            values[node.id] = result
        return values


def _parse(payload: dict[str, Any]) -> Pipeline:
    version = int(payload.get("version", 1) or 1)
    raw_nodes = payload.get("nodes")
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise ValueError("pipeline nodes must be a non-empty list")
    nodes: list[PipelineNode] = []
    seen: set[str] = set()
    for raw in raw_nodes:
        if not isinstance(raw, dict):
            raise ValueError("pipeline node must be an object")
        node_id = str(raw.get("id") or "").strip()
        node_type = str(raw.get("type") or "").strip().lower()
        if not node_id or node_id in seen:
            raise ValueError(f"invalid or duplicate pipeline node: {node_id!r}")
        if node_type not in {"agent", "operation", "loop"}:
            raise ValueError(f"unsupported pipeline node type: {node_type}")
        if node_type == "agent" and not str(raw.get("role") or "").strip():
            raise ValueError(f"agent node requires a role: {node_id!r}")
        if node_type == "operation":
            operation = str(raw.get("operation") or "").strip().lower()
            if operation not in SUPPORTED_OPERATIONS:
                raise ValueError(f"unsupported pipeline operation: {operation!r}")
        if node_type == "loop" and str(raw.get("until") or "no_major_findings").strip() not in {"no_major_findings", "pass", "approved"}:
            raise ValueError(f"unsupported loop termination condition: {raw.get('until')!r}")
        seen.add(node_id)
        max_rounds = max(int(raw.get("max_rounds", 3) or 3), 1)
        nodes.append(
            PipelineNode(
                id=node_id,
                type=node_type,
                role=str(raw.get("role") or ""),
                operation=str(raw.get("operation") or ""),
                review=str(raw.get("review") or ""),
                repair=str(raw.get("repair") or ""),
                until=str(raw.get("until") or ""),
                max_rounds=max_rounds,
                options={key: value for key, value in raw.items() if key not in {"id", "type", "role", "operation", "review", "repair", "until", "max_rounds"}},
            )
        )
    loops = [node for node in nodes if node.type == "loop"]
    if not loops:
        raise ValueError("pipeline must define at least one loop node")
    for loop in loops:
        if not loop.review or not loop.repair:
            raise ValueError("loop node requires review and repair roles")
    raw_roles = payload.get("roles") or {}
    if not isinstance(raw_roles, Mapping):
        raise ValueError("pipeline roles must be a YAML object")
    roles: dict[str, str] = {}
    for raw_role, raw_prompt in raw_roles.items():
        role = str(raw_role or "").strip().lower()
        if not role:
            continue
        if isinstance(raw_prompt, Mapping):
            raw_prompt = raw_prompt.get("prompt", raw_prompt.get("instructions", ""))
        if not isinstance(raw_prompt, str) or not raw_prompt.strip():
            raise ValueError(f"role prompt must be a non-empty string: {role!r}")
        roles[role] = raw_prompt.strip()
    default_prompt = payload.get("default_prompt", "")
    if not isinstance(default_prompt, str):
        raise ValueError("pipeline default_prompt must be a string")
    return Pipeline(version=version, nodes=tuple(nodes), roles=roles, default_prompt=default_prompt.strip())


def load_pipeline(output_dir: Path) -> Pipeline:
    configured_path = output_dir / ".arc" / "hafleet" / "pipeline.yaml"
    path = configured_path if configured_path.is_file() else DEFAULT_PIPELINE_PATH
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as error:
        raise ValueError(f"invalid pipeline configuration: {path}: {error}") from error
    if not isinstance(payload, dict):
        raise ValueError("pipeline configuration must be a YAML object")
    # A run-local file may customize only nodes or only selected role prompts.
    # Merge it with the package defaults so adding a new role does not require
    # copying every built-in prompt into each output directory.
    if path != DEFAULT_PIPELINE_PATH:
        try:
            defaults = yaml.safe_load(DEFAULT_PIPELINE_PATH.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as error:
            raise ValueError(f"invalid built-in pipeline configuration: {DEFAULT_PIPELINE_PATH}: {error}") from error
        if isinstance(defaults, dict):
            merged_roles = dict(defaults.get("roles") or {})
            custom_roles = payload.get("roles") or {}
            if not isinstance(custom_roles, Mapping):
                raise ValueError("pipeline roles must be a YAML object")
            merged_roles.update(custom_roles)
            payload = dict(payload)
            payload["roles"] = merged_roles
            if not payload.get("default_prompt"):
                payload["default_prompt"] = defaults.get("default_prompt", "")
            if not payload.get("nodes"):
                payload["nodes"] = defaults.get("nodes", [])
            if not payload.get("version"):
                payload["version"] = defaults.get("version", 1)
    return _parse(payload)
