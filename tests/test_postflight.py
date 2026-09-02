from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hafleet_arc.postflight import (
    PostflightError,
    rehearse_web_app,
    validate_web_structure,
)


class PostflightTests(unittest.TestCase):
    def setUp(self) -> None:
        self.template = Path(__file__).resolve().parents[1] / "template" / "web"

    def test_reports_missing_delivery_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            errors = validate_web_structure(Path(temporary))

        self.assertIn("frontend/ is missing at the project root", errors)
        self.assertIn("backend/ is missing at the project root", errors)

    def test_bundled_template_satisfies_structure_contract(self) -> None:
        self.assertEqual(validate_web_structure(self.template), [])

    def test_modular_backend_config_can_receive_process_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "project"
            shutil.copytree(self.template, output)
            server = output / "backend" / "server.js"
            server.write_text(
                'import { config } from "./src/config.js";\n'
                'const settings = config(process.env);\n'
                'server.listen(settings.port);\n',
                encoding="utf-8",
            )
            config = output / "backend" / "src" / "config.js"
            config.parent.mkdir(parents=True, exist_ok=True)
            config.write_text(
                'export const config = (env) => ({ port: Number(env.PORT || 3000) });\n',
                encoding="utf-8",
            )

            self.assertEqual(validate_web_structure(output), [])

    def test_bundled_template_passes_grader_startup_rehearsal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "project"
            shutil.copytree(self.template, output)
            with mock.patch.dict(
                "os.environ",
                {"HAFLEET_NPM_TIMEOUT": "60", "HAFLEET_READY_TIMEOUT": "10"},
                clear=False,
            ):
                rehearse_web_app(output, 39117)

    def test_rehearsal_rejects_backend_that_ignores_port(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "project"
            shutil.copytree(self.template, output)
            server = output / "backend" / "server.js"
            server.write_text(
                server.read_text(encoding="utf-8").replace(
                    'process.env.PORT || "3000"', '"3000"'
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(PostflightError, "must read the PORT"):
                rehearse_web_app(output, 39118)


if __name__ == "__main__":
    unittest.main()
