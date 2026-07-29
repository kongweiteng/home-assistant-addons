from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
ADDON = ROOT / "ddns_go"


class DdnsGoAddonTests(unittest.TestCase):
    def test_required_files_exist(self) -> None:
        required = (
            "config.yaml",
            "build.yaml",
            "Dockerfile",
            "README.md",
            "DOCS.md",
            "CHANGELOG.md",
            "rootfs/etc/services.d/ddns-go/run",
        )
        for relative_path in required:
            self.assertTrue((ADDON / relative_path).is_file(), relative_path)

    def test_version_and_checksums_are_pinned(self) -> None:
        config = (ADDON / "config.yaml").read_text()
        build = (ADDON / "build.yaml").read_text()

        addon_version = re.search(
            r'^version: "([0-9]+(?:\.[0-9]+){2,3})"$', config, re.M
        )
        upstream_version = re.search(
            r'^\s*DDNS_GO_VERSION: "([0-9]+\.[0-9]+\.[0-9]+)"$', build, re.M
        )
        self.assertIsNotNone(addon_version)
        self.assertIsNotNone(upstream_version)
        self.assertTrue(addon_version.group(1).startswith(upstream_version.group(1)))

        checksums = re.findall(r'DDNS_GO_SHA256_[A-Z0-9]+: "([0-9a-f]{64})"', build)
        self.assertEqual(2, len(checksums))

    def test_service_uses_private_addon_config(self) -> None:
        config = (ADDON / "config.yaml").read_text()
        dockerfile = (ADDON / "Dockerfile").read_text()
        service = (ADDON / "rootfs/etc/services.d/ddns-go/run").read_text()

        self.assertIn("addon_config:rw", config)
        self.assertIn("chmod 0755 /etc/services.d/ddns-go/run", dockerfile)
        self.assertIn('"/config/ddns-go.yaml"', service)
        self.assertNotIn("AccessKey", service)

    def test_bashio_runtime_dependency_is_not_removed(self) -> None:
        dockerfile = (ADDON / "Dockerfile").read_text()

        self.assertRegex(dockerfile, r"apk add --no-cache[^\n]*\bcurl\b")
        self.assertNotIn("apk del curl", dockerfile)


if __name__ == "__main__":
    unittest.main()
