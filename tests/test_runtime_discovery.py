from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import main as entrypoint


class RuntimeDiscoveryTests(unittest.TestCase):
    def test_local_sdk_path_is_added_before_import(self) -> None:
        expected = (
            Path(entrypoint.__file__).resolve().parent
            / "arcbench-agent-runtime"
            / "src"
        )
        if not expected.is_dir():
            self.skipTest("local ARC-Bench runtime checkout is unavailable")

        original_path = list(sys.path)
        sys.modules.pop("arcbench_agent_runtime", None)
        try:
            sys.path[:] = [item for item in sys.path if Path(item or ".").resolve() != expected]
            with tempfile.TemporaryDirectory() as temporary:
                runtime = entrypoint._runtime(Path(temporary))
            self.assertEqual(sys.path[0], str(expected))
            self.assertEqual(runtime.paths.project_dir, Path(temporary).resolve())
        finally:
            sys.path[:] = original_path


if __name__ == "__main__":
    unittest.main()
