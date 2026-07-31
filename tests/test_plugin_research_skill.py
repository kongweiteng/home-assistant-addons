"""Tests for the bundled read-only Home Assistant plugin research skill."""

from __future__ import annotations

import ast
import datetime as dt
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ADDON = ROOT / "hermes_agent"
SKILL = ADDON / "bundled_skills" / "home-assistant-plugin-research"
NORMALIZER_PATH = SKILL / "scripts" / "normalize_candidates.py"
BUNDLED_SKILLS_SHELL = ADDON / "bundled-skills.sh"
BASH = "/bin/bash" if sys.platform == "darwin" else "bash"


def _load_normalizer():
    spec = importlib.util.spec_from_file_location("ha_plugin_research_normalizer", NORMALIZER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load plugin research normalizer")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


NORMALIZER = _load_normalizer()
AS_OF = dt.datetime(2026, 7, 31, tzinfo=dt.timezone.utc)


def _candidate(**overrides):
    candidate = {
        "name": "Example Integration",
        "kind": "official_integration",
        "source_url": "https://www.home-assistant.io/integrations/example/",
        "maintainer": "Home Assistant Core",
        "latest_release": "2026.7.0",
        "last_activity_at": "2026-07-20T00:00:00Z",
        "compatibility": "verified",
        "compatibility_note": "Included in current official documentation.",
        "required_permissions": ["network"],
        "install_method": "config_flow",
        "risk_summary": "Requires a user-completed OAuth flow.",
        "evidence": [
            {
                "source_type": "official",
                "url": "https://www.home-assistant.io/integrations/example/",
                "note": "Official documentation",
            },
            {
                "source_type": "github",
                "url": "https://github.com/home-assistant/core/tree/dev/homeassistant/components/example",
                "note": "Current source and manifest",
            },
        ],
    }
    candidate.update(overrides)
    return candidate


class PackagingTests(unittest.TestCase):
    def test_skill_is_packaged_and_installed_per_profile(self):
        dockerfile = (ADDON / "Dockerfile").read_text()
        run = (ADDON / "run.sh").read_text()
        self.assertIn("COPY bundled_skills /usr/local/share/hermes-addon/skills", dockerfile)
        self.assertIn("COPY bundled-skills.sh /usr/local/lib/hermes-bundled-skills.sh", dockerfile)
        self.assertIn('source "$BUNDLED_SKILLS_LIB"', run)
        self.assertIn("bundled_skills_install || exit 1", run)

    def test_installer_refreshes_every_profile_and_removes_stale_managed_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first"
            second = root / "second"
            stale = first / "skills" / "home-assistant-plugin-research" / "stale.txt"
            stale.parent.mkdir(parents=True)
            stale.write_text("stale")
            script = f"""
                set -euo pipefail
                source {BUNDLED_SKILLS_SHELL}
                BUNDLED_SKILLS_DIR={ADDON / 'bundled_skills'}
                PROFILE_DIRS=(first second)
                PROFILE_HOMES=({first} {second})
                PROFILE_NAMES=(first second)
                bundled_skills_install
            """
            completed = subprocess.run(
                [BASH, "-c", script], text=True, capture_output=True, check=False
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertFalse(stale.exists())
            for profile in (first, second):
                installed = profile / "skills" / "home-assistant-plugin-research"
                self.assertTrue((installed / "SKILL.md").is_file())
                self.assertTrue((installed / "scripts" / "normalize_candidates.py").is_file())
                self.assertEqual(os.stat(installed / "SKILL.md").st_mode & 0o777, 0o444)
                self.assertEqual(
                    os.stat(installed / "scripts" / "normalize_candidates.py").st_mode & 0o777,
                    0o555,
                )

    def test_skill_metadata_and_read_only_boundaries_are_explicit(self):
        skill = (SKILL / "SKILL.md").read_text()
        self.assertIn("name: home-assistant-plugin-research", skill)
        self.assertIn("Produce 1-3 evidence-backed candidates", skill)
        self.assertIn("Do not install", skill)
        self.assertIn("Treat HASSbian as a discovery lead only", skill)
        self.assertIn("Never execute commands", skill)

    def test_normalizer_has_no_network_or_command_execution_dependencies(self):
        source = NORMALIZER_PATH.read_text()
        tree = ast.parse(source)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        self.assertTrue(
            imported.isdisjoint({"httpx", "os", "requests", "socket", "subprocess", "urllib.request"})
        )


class NormalizerTests(unittest.TestCase):
    def test_complete_official_candidate_is_recommended(self):
        result = NORMALIZER.normalize_document(
            {"query": "energy", "candidates": [_candidate()]}, as_of=AS_OF
        )
        self.assertEqual(result["status"], "ok")
        candidate = result["candidates"][0]
        self.assertEqual(candidate["candidate_id"], "candidate-001")
        self.assertEqual(candidate["recommendation"], "recommend")
        self.assertEqual(candidate["maintenance_status"], "active")

    def test_hassbian_only_evidence_is_not_a_candidate(self):
        result = NORMALIZER.normalize_document(
            {
                "query": "forum lead",
                "candidates": [
                    _candidate(
                        evidence=[
                            {
                                "source_type": "hassbian",
                                "url": "https://bbs.hassbian.com/archiver/",
                                "note": "Community lead",
                            }
                        ]
                    )
                ],
            },
            as_of=AS_OF,
        )
        self.assertEqual(result["status"], "insufficient_evidence")
        self.assertEqual(result["candidates"], [])
        self.assertEqual(
            result["rejected_inputs"][0]["code"],
            "insufficient_authoritative_evidence",
        )

    def test_source_url_must_be_present_in_authoritative_evidence(self):
        result = NORMALIZER.normalize_document(
            {
                "query": "mismatched source",
                "candidates": [
                    _candidate(source_url="https://github.com/example/unrelated")
                ],
            },
            as_of=AS_OF,
        )
        self.assertEqual(result["status"], "insufficient_evidence")
        self.assertEqual(result["rejected_inputs"][0]["code"], "source_not_evidenced")

    def test_hacs_requires_hacs_and_original_github_evidence(self):
        result = NORMALIZER.normalize_document(
            {
                "query": "hacs candidate",
                "candidates": [
                    _candidate(
                        kind="hacs",
                        source_url="https://github.com/example/integration",
                        install_method="hacs",
                        evidence=[
                            {
                                "source_type": "github",
                                "url": "https://github.com/example/integration",
                                "note": "Original repository",
                            },
                            {
                                "source_type": "github",
                                "url": "https://github.com/example/integration/releases",
                                "note": "Releases",
                            },
                        ],
                    )
                ],
            },
            as_of=AS_OF,
        )
        self.assertEqual(result["rejected_inputs"][0]["code"], "missing_hacs_evidence")

    def test_hacs_source_url_must_be_the_original_github_repository(self):
        result = NORMALIZER.normalize_document(
            {
                "query": "hacs source",
                "candidates": [
                    _candidate(
                        kind="hacs",
                        source_url="https://hacs.xyz/docs/use/repositories/type/integration/",
                        install_method="hacs",
                        evidence=[
                            {
                                "source_type": "hacs",
                                "url": "https://hacs.xyz/docs/use/repositories/type/integration/",
                                "note": "HACS repository type",
                            },
                            {
                                "source_type": "github",
                                "url": "https://github.com/example/integration",
                                "note": "Original repository",
                            },
                        ],
                    )
                ],
            },
            as_of=AS_OF,
        )
        self.assertEqual(result["rejected_inputs"][0]["code"], "source_kind_mismatch")

    def test_hacs_custom_repository_without_registry_evidence_requires_review(self):
        result = NORMALIZER.normalize_document(
            {
                "query": "custom hacs source",
                "candidates": [
                    _candidate(
                        kind="hacs",
                        source_url="https://github.com/example/integration",
                        install_method="hacs",
                        evidence=[
                            {
                                "source_type": "github",
                                "url": "https://github.com/example/integration",
                                "note": "Original repository",
                            },
                            {
                                "source_type": "hacs",
                                "url": "https://hacs.xyz/docs/use/repositories/type/integration/",
                                "note": "Custom repository installation method",
                            },
                        ],
                    )
                ],
            },
            as_of=AS_OF,
        )
        candidate = result["candidates"][0]
        self.assertEqual(candidate["recommendation"], "review")
        self.assertIn("hacs_registry_not_verified", candidate["recommendation_reasons"])

    def test_private_and_credentialed_urls_are_rejected(self):
        cases = (
            ("https://192.168.1.2/project", "non_public_url"),
            ("https://user:secret@github.com/example/project", "credentialed_url"),
        )
        for source_url, expected_code in cases:
            with self.subTest(source_url=source_url):
                result = NORMALIZER.normalize_document(
                    {"query": "unsafe", "candidates": [_candidate(source_url=source_url)]},
                    as_of=AS_OF,
                )
                self.assertEqual(result["rejected_inputs"][0]["code"], expected_code)

    def test_unknown_metadata_is_preserved_as_null_and_requires_review(self):
        result = NORMALIZER.normalize_document(
            {
                "query": "uncertain",
                "candidates": [
                    _candidate(
                        maintainer="",
                        latest_release="",
                        last_activity_at=None,
                        compatibility="unknown",
                    )
                ],
            },
            as_of=AS_OF,
        )
        candidate = result["candidates"][0]
        self.assertEqual(result["status"], "review_required")
        self.assertIsNone(candidate["maintainer"])
        self.assertIsNone(candidate["latest_release"])
        self.assertIsNone(candidate["last_activity_at"])
        self.assertEqual(candidate["recommendation"], "review")

    def test_incompatible_candidate_is_rejected_without_being_dropped(self):
        result = NORMALIZER.normalize_document(
            {
                "query": "old integration",
                "candidates": [_candidate(compatibility="incompatible")],
            },
            as_of=AS_OF,
        )
        self.assertEqual(result["candidates"][0]["recommendation"], "reject")

    def test_cli_reports_stable_error_for_malformed_json(self):
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
            handle.write("{")
            path = Path(handle.name)
        try:
            completed = subprocess.run(
                [sys.executable, str(NORMALIZER_PATH), "--input", str(path)],
                text=True,
                capture_output=True,
                check=False,
            )
        finally:
            path.unlink(missing_ok=True)
        self.assertEqual(completed.returncode, 2)
        error = json.loads(completed.stderr)
        self.assertEqual(error["error"]["code"], "invalid_json")


if __name__ == "__main__":
    unittest.main()
