from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from hafleet_arc.worktree import WorktreeConflict, WorktreeManager, _role_identity


class WorktreeManagerTests(unittest.TestCase):
    def test_role_identity_uses_prefix_and_domain_rules(self) -> None:
        self.assertEqual(_role_identity("architect"), ("HAFleet-Architect", "architect@hafleet.local"))
        self.assertEqual(_role_identity("final review"), ("HAFleet-Final-Review", "final-review@hafleet.local"))
        self.assertEqual(_role_identity(""), ("HAFleet-Reviewer", "reviewer@hafleet.local"))

    def test_create_commit_cherry_pick_and_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
            (root / ".gitignore").write_text(".arc/\n", encoding="utf-8")
            (root / "seed.txt").write_text("seed\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "seed"], cwd=root, check=True)

            manager = WorktreeManager(root)
            base = manager.current_head()
            worktree, branch = manager.create_or_reuse("REQ-1", base)
            (worktree / "feature.txt").write_text("feature\n", encoding="utf-8")
            manager.ensure_commit(worktree, "REQ-1: feature")
            author = subprocess.run(
                ["git", "log", "-1", "--format=%an <%ae>"], cwd=worktree,
                check=True, capture_output=True, text=True,
            ).stdout.strip()
            self.assertEqual(author, "HAFleet-Reviewer <reviewer@hafleet.local>")
            manager.cherry_pick(manager.commits_since(worktree, base))
            manager.remove_successful(worktree, branch)

            self.assertTrue((root / "feature.txt").is_file())
            self.assertFalse(worktree.exists())

    def test_cherry_pick_conflict_aborts_and_keeps_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
            (root / ".gitignore").write_text(".arc/\n", encoding="utf-8")
            (root / "shared.txt").write_text("base\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "seed"], cwd=root, check=True)

            manager = WorktreeManager(root)
            base = manager.current_head()
            worktree, _branch = manager.create_or_reuse("REQ-1", base)
            (worktree / "shared.txt").write_text("worktree\n", encoding="utf-8")
            manager.ensure_commit(worktree, "REQ-1: change shared file")
            (root / "shared.txt").write_text("main\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "main change"], cwd=root, check=True)

            with self.assertRaises(WorktreeConflict):
                manager.cherry_pick(manager.commits_since(worktree, base))

            self.assertTrue(worktree.exists())
            self.assertEqual((root / "shared.txt").read_text(encoding="utf-8"), "main\n")


if __name__ == "__main__":
    unittest.main()
