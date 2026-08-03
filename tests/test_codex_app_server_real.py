from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from codex_controller.app_server import AppServerClient


class RealAppServerSmokeTests(unittest.TestCase):
    def test_official_app_server_starts_with_empty_unauthenticated_home(self) -> None:
        binary_value = os.environ.get("CODEX_REAL_BINARY", "")
        if not binary_value:
            self.skipTest("CODEX_REAL_BINARY 未配置")
        binary = Path(binary_value).resolve(strict=True)
        version = subprocess.run(
            [str(binary), "--version"],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        ).stdout.strip()
        self.assertEqual(version, "codex-cli 0.146.0")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            client = AppServerClient(
                [str(binary), "app-server", "--listen", "stdio://"],
                codex_home=root / "codex-home",
                workspace=root / "workspace",
                request_timeout=20,
            )
            try:
                client.start()
                status = client.status()
                self.assertTrue(status["running"])
                self.assertTrue(status["initialized"])
                self.assertFalse(status["account"]["ready"])
                self.assertIsNone(status["account"]["auth_mode"])
            finally:
                client.stop()


if __name__ == "__main__":
    unittest.main()
