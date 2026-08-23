from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path


class WorktreeConflict(RuntimeError):
    """A cherry-pick conflict that requires a later/manual repair."""


def _safe_id(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-")
    if not normalized:
        raise ValueError("module id cannot produce a safe worktree name")
    return normalized


class WorktreeManager:
    """Create, commit, merge, and safely clean module worktrees."""

    def __init__(self, repository: Path) -> None:
        self.repository = repository.resolve()
        self.root = self.repository / ".arc" / "hafleet" / "worktrees"

    def _run(
        self,
        args: list[str],
        cwd: Path | None = None,
        *,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.setdefault("GIT_AUTHOR_NAME", os.environ.get("ARC_GIT_USER_NAME", "ARC Bench Agent"))
        env.setdefault("GIT_AUTHOR_EMAIL", os.environ.get("ARC_GIT_USER_EMAIL", "arcbench@example.com"))
        env.setdefault("GIT_COMMITTER_NAME", env["GIT_AUTHOR_NAME"])
        env.setdefault("GIT_COMMITTER_EMAIL", env["GIT_AUTHOR_EMAIL"])
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd or self.repository),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if check and result.returncode != 0:
            detail = (result.stderr or result.stdout).strip() or "git command failed"
            raise RuntimeError(detail)
        return result

    def current_head(self) -> str:
        return self._run(["rev-parse", "HEAD"]).stdout.strip()

    def path_for(self, module_id: str) -> Path:
        return self.root / _safe_id(module_id)

    def branch_for(self, module_id: str) -> str:
        return f"hafleet/{_safe_id(module_id)}"

    def create_or_reuse(
        self,
        module_id: str,
        base_commit: str,
        existing_path: Path | None = None,
    ) -> tuple[Path, str]:
        path = (existing_path or self.path_for(module_id)).resolve()
        branch = self.branch_for(module_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            check = self._run(["rev-parse", "--is-inside-work-tree"], path, check=False)
            if check.returncode != 0:
                raise RuntimeError(f"recorded worktree exists but is not a git worktree: {path}")
            actual_branch = self._run(["branch", "--show-current"], path).stdout.strip()
            return path, actual_branch or branch

        branch_exists = self._run(
            ["show-ref", "--verify", f"refs/heads/{branch}"], check=False
        ).returncode == 0
        if branch_exists:
            self._run(["worktree", "add", str(path), branch])
        else:
            self._run(["worktree", "add", "-b", branch, str(path), base_commit])
        return path, branch

    def ensure_commit(self, path: Path, message: str) -> str:
        self._run(["add", "-A"], path)
        status = self._run(["status", "--porcelain"], path).stdout.strip()
        if status:
            self._run(["commit", "-m", message], path)
        return self._run(["rev-parse", "HEAD"], path).stdout.strip()

    def commits_since(self, path: Path, base_commit: str) -> list[str]:
        result = self._run(["rev-list", "--reverse", f"{base_commit}..HEAD"], path)
        return [item for item in result.stdout.splitlines() if item.strip()]

    def cherry_pick(self, commits: list[str]) -> None:
        for commit in commits:
            result = self._run(["cherry-pick", commit], check=False)
            if result.returncode != 0:
                self._run(["cherry-pick", "--abort"], check=False)
                detail = (result.stderr or result.stdout).strip() or "cherry-pick conflict"
                raise WorktreeConflict(detail)

    def remove_successful(self, path: Path, branch: str) -> None:
        resolved = path.resolve()
        resolved.relative_to(self.root.resolve())
        self._run(["worktree", "remove", "--force", str(resolved)])
        self._run(["branch", "-D", branch], check=False)
        self._run(["worktree", "prune"], check=False)
