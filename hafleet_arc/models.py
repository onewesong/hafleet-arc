from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RequirementModule:
    """One direct child of the ARC-Bench ROOT requirement."""

    index: int
    total: int
    node_id: str
    name: str
    subtree: dict[str, Any]
    dependencies: tuple[str, ...] = ()

