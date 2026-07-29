from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
ADDON = ROOT / "drawio"


class DrawIoAddonTests(unittest.TestCase):
    def test_required_files_exist(self) -> None:
        required = (
            "config.yaml",
            "build.yaml",
            "Dockerfile",
            "README.md",
            "DOCS.md",
            "CHANGELOG.md",
        )
        for relative_path in required:
            self.assertTrue((ADDON / relative_path).is_file(), relative_path)

    def test_ingress_only_metadata_and_architectures(self) -> None:
        config = (ADDON / "config.yaml").read_text()

        self.assertRegex(config, r'(?m)^version: "30\.3\.14"$')
        self.assertRegex(config, r"(?m)^slug: drawio$")
        self.assertRegex(config, r"(?m)^ingress: true$")
        self.assertRegex(config, r"(?m)^ingress_port: 8080$")
        self.assertIn("  - amd64", config)
        self.assertIn("  - aarch64", config)
        self.assertNotRegex(config, r"(?m)^ports:")

    def test_upstream_images_are_digest_pinned(self) -> None:
        build = (ADDON / "build.yaml").read_text()
        dockerfile = (ADDON / "Dockerfile").read_text()

        manifests = re.findall(r"jgraph/drawio@sha256:([0-9a-f]{64})", build)
        self.assertEqual(2, len(manifests))
        self.assertEqual(2, len(set(manifests)))
        self.assertIn("FROM ${BUILD_FROM}", dockerfile)

    def test_storage_boundary_is_documented(self) -> None:
        docs = "\n".join(
            [
                (ADDON / "README.md").read_text(),
                (ADDON / "DOCS.md").read_text(),
            ]
        ).lower()

        self.assertIn("browser local storage", docs)
        self.assertIn("home assistant backups do not automatically include", docs)
        self.assertIn(".drawio", docs)


if __name__ == "__main__":
    unittest.main()
