from __future__ import annotations

import json
import os
from pathlib import Path
from types import TracebackType
from typing import Any, Self

ROLE_INSTRUCTIONS = {
    "planner": """
You are the planning agent in a finite HAFleet run. Analyze the supplied requirement
subtree and the current repository. Write a concise, concrete implementation plan to
the exact plan path supplied by the coordinator. Do not modify any other file. Cover
data model, routes/UI, persistence, validation, scenarios, and verification. Resolve
uncertainty by inspecting the existing project. Do not start a long-running server.
""",
    "implementer": """
You are the implementation agent in a finite HAFleet run. Read the coordinator's plan
and implement the entire supplied requirement subtree in the current repository.
Preserve working behavior from earlier modules. Build real persisted behavior rather
than static mock screens. Run focused checks while working. Do not merely explain what
to do, do not stop at scaffolding, and do not start a long-running server.
""",
    "reviewer": """
You are the reviewer and repair agent in a finite HAFleet run. Inspect the implemented
requirement subtree against its scenarios, run practical tests or build checks, and fix
all defects you find. Check cross-module regressions, persistence, permissions,
validation, and visible UI behavior. Finish with a runnable project. Do not only write
a review report and do not start a long-running server.
""",
}


class CodexFleet:
    """Three persistent role threads sharing one ARC-Bench output workspace."""

    def __init__(self, output_dir: Path, skills_dir: Path | None = None) -> None:
        self.output_dir = output_dir
        self.skills_dir = skills_dir
        self._codex: Any = None
        self._threads: dict[str, Any] = {}

    def __enter__(self) -> Self:
        try:
            from openai_codex import Codex, CodexConfig
        except ImportError as exc:
            raise RuntimeError("Install requirements.txt before running HAFleet ARC.") from exc

        env = os.environ.copy()
        # ARC-Bench containers may expose a read-only /root. Codex persists its
        # SQLite state under CODEX_HOME, so keep all ephemeral agent state in the
        # writable output workspace instead of inheriting ~/.codex.
        codex_home = self.output_dir / ".arc" / "hafleet" / "codex-home"
        codex_home.mkdir(parents=True, exist_ok=True)
        env["CODEX_HOME"] = str(codex_home)
        overrides: list[str] = []
        base_url = os.environ.get("OPENAI_BASE_URL", "").strip()
        if base_url:
            env["OPENAI_BASE_URL"] = base_url
            overrides.append(f"openai_base_url={json.dumps(base_url)}")
        self._codex = Codex(config=CodexConfig(env=env, config_overrides=tuple(overrides)))
        self._codex.__enter__()
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if api_key:
            self._codex.login_api_key(api_key)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._codex is not None:
            self._codex.__exit__(exc_type, exc, traceback)

    def _thread(self, role: str) -> Any:
        if role in self._threads:
            return self._threads[role]
        if role not in ROLE_INSTRUCTIONS:
            raise ValueError(f"unknown fleet role: {role}")
        from openai_codex import ApprovalMode, Sandbox

        skill_note = (
            f"\nARC-Bench skills are available under {self.skills_dir}. Read their SKILL.md files when useful."
            if self.skills_dir
            else ""
        )
        model = os.environ.get("MODEL", "").strip() or None
        thread = self._codex.thread_start(
            cwd=str(self.output_dir),
            sandbox=Sandbox.full_access,
            approval_mode=ApprovalMode.deny_all,
            model=model,
            developer_instructions=ROLE_INSTRUCTIONS[role].strip() + skill_note,
        )
        self._threads[role] = thread
        return thread

    def run(self, role: str, prompt: str) -> None:
        result = self._thread(role).run(prompt)
        error = getattr(result, "error", None)
        if error is not None:
            raise RuntimeError(f"{role} agent failed: {error}")
