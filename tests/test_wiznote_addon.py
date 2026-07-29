import os
import pathlib
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
ADDON = ROOT / "wiznote_server"


class WizNoteAddonTests(unittest.TestCase):
    def test_required_metadata_and_cold_backup_are_declared(self):
        config = (ADDON / "config.yaml").read_text(encoding="utf-8")

        self.assertIn("slug: wiznote_server", config)
        self.assertIn("backup: cold", config)
        self.assertIn("path: /wiz/storage", config)
        self.assertIn("80/tcp: 8088", config)
        self.assertIn("9269/udp: 9269", config)
        self.assertIn("stage: experimental", config)
        self.assertIn("timeout: 60", config)
        self.assertIn("  - amd64", config)
        self.assertNotIn("  - aarch64", config)

    def test_amd64_upstream_image_is_pinned_in_dockerfile(self):
        dockerfile = (ADDON / "Dockerfile").read_text(encoding="utf-8")

        self.assertIn("FROM wiznote/wizserver@sha256:", dockerfile)
        self.assertNotIn("wiznote/wizserver:latest", dockerfile)
        self.assertNotIn("ARG BUILD_FROM", dockerfile)
        self.assertFalse((ADDON / "build.yaml").exists())

    def test_wrapper_passes_first_run_options_to_upstream(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            storage = tmp_path / "storage"
            storage.mkdir()
            options = storage / "options.json"
            options.write_text(
                '{"admin_password":"test-secret","timezone":"UTC"}',
                encoding="utf-8",
            )
            observed = tmp_path / "observed.txt"
            upstream = tmp_path / "upstream.sh"
            upstream.write_text(
                '#!/bin/bash\nprintf "%s\\n%s\\n" "$ADMIN_PASSWORD" "$TZ" > "$OBSERVED_FILE"\n',
                encoding="utf-8",
            )
            upstream.chmod(0o755)

            env = os.environ.copy()
            env.update(
                {
                    "OBSERVED_FILE": str(observed),
                    "WIZNOTE_STORAGE_DIR": str(storage),
                    "WIZNOTE_OPTIONS_FILE": str(options),
                    "WIZNOTE_ENTRYPOINT": str(upstream),
                    "WIZNOTE_LOCALTIME_PATH": str(tmp_path / "localtime"),
                    "WIZNOTE_SKIP_SERVICE_SHUTDOWN": "1",
                }
            )
            result = subprocess.run(
                ["bash", str(ADDON / "run.sh")],
                check=False,
                capture_output=True,
                text=True,
                env=env,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(observed.read_text(encoding="utf-8"), "test-secret\nUTC\n")
            self.assertNotIn("test-secret", result.stdout)

    def test_documentation_contains_storage_and_migration_warnings(self):
        readme = (ADDON / "README.md").read_text(encoding="utf-8")

        self.assertIn("Set `internal` to `false`", readme)
        self.assertIn("Do not rsync `/wiz/storage` while the app is running", readme)
        self.assertIn("not to switch an existing installation", readme)


if __name__ == "__main__":
    unittest.main()
