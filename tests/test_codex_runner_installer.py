from __future__ import annotations

import hashlib
import json
from pathlib import Path
import socket
import unittest

import codex_controller
from codex_controller.runner_relay import RunnerInstallerCatalog
from codex_controller.store import StoreError


def public_resolver(_host: str, port: int, **_kwargs: object) -> list[tuple]:
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port))]


def private_resolver(_host: str, port: int, **_kwargs: object) -> list[tuple]:
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.10", port))]


def manifest_document() -> dict:
    return {
        "version": 2,
        "runner_version": "0.3.23",
        "codex_version": "0.146.0",
        "python_version": "3.11.13",
        "self_contained": True,
        "installer": {
            "url": "https://downloads.example.com/codex-runner/install.sh",
            "sha256": "1" * 64,
            "size": 1234,
        },
        "assets": {
            "linux-amd64": {
                "url": "https://downloads.example.com/codex-runner/linux-amd64.tar.gz",
                "sha256": "2" * 64,
                "size": 2001,
            },
            "linux-aarch64": {
                "url": "https://downloads.example.com/codex-runner/linux-aarch64.tar.gz",
                "sha256": "3" * 64,
                "size": 2002,
            },
            "macos-amd64": {
                "url": "https://downloads.example.com/codex-runner/macos-amd64.tar.gz",
                "sha256": "4" * 64,
                "size": 2003,
            },
            "macos-aarch64": {
                "url": "https://downloads.example.com/codex-runner/macos-aarch64.tar.gz",
                "sha256": "5" * 64,
                "size": 2004,
            },
        },
    }


def manifest_bytes(document: dict | None = None) -> bytes:
    return json.dumps(
        manifest_document() if document is None else document,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


class Response:
    def __init__(self, body: bytes, *, status: int = 200) -> None:
        self.body = body
        self.status = status

    def __enter__(self) -> "Response":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, limit: int) -> bytes:
        return self.body[:limit]


class Opener:
    def __init__(self, body: bytes, *, status: int = 200) -> None:
        self.body = body
        self.status = status
        self.calls: list[tuple[str, int]] = []

    def __call__(self, request: object, *, timeout: int) -> Response:
        self.calls.append((request.full_url, timeout))
        return Response(self.body, status=self.status)


class ForbiddenOpener:
    def __call__(self, _request: object, *, timeout: int) -> Response:
        raise AssertionError(f"runtime network access is forbidden (timeout={timeout})")


class RunnerInstallerCatalogTests(unittest.TestCase):
    def catalog(self, body: bytes, *, digest: str | None = None) -> RunnerInstallerCatalog:
        return RunnerInstallerCatalog(
            "https://downloads.example.com/codex-runner/manifest.json",
            digest or hashlib.sha256(body).hexdigest(),
            "wss://runner.example.com/v1/connect",
            opener=Opener(body),
            resolver=public_resolver,
        )

    def test_valid_pinned_manifest_builds_single_line_linux_and_macos_commands(self) -> None:
        body = manifest_bytes()
        opener = Opener(body)
        catalog = RunnerInstallerCatalog(
            "https://downloads.example.com/codex-runner/manifest.json",
            hashlib.sha256(body).hexdigest(),
            "wss://runner.example.com/v1/connect",
            opener=opener,
            resolver=public_resolver,
        )
        manifest = catalog.manifest()
        self.assertEqual(catalog.status()["ready"], True)
        self.assertEqual(len(opener.calls), 1)

        linux = catalog.command(
            runner_id="RN-" + "A" * 20,
            enrollment_token="ENROLL-" + "B" * 32,
            os_name="linux",
            arch="amd64",
            projects=["renovation-hub"],
            manifest=manifest,
        )
        self.assertNotIn("\n", linux["command"])
        self.assertEqual(linux["link"], "https://runner.example.com/install/" + "ENROLL-" + "B" * 32)
        self.assertIn(linux["link"], linux["command"])
        self.assertIn("sudo sh", linux["command"])
        self.assertNotIn("CODEX_RUNNER_ENROLLMENT_TOKEN", linux["command"])
        self.assertNotIn("--asset-sha256", linux["command"])
        self.assertEqual(linux["runner_version"], "0.3.23")
        self.assertTrue(linux["self_contained"])

        macos = catalog.command(
            runner_id="RN-" + "C" * 20,
            enrollment_token="ENROLL-" + "D" * 32,
            os_name="macos",
            arch="aarch64",
            projects=["renovation-hub"],
            manifest=manifest,
        )
        self.assertEqual(macos["link"], "https://runner.example.com/install/" + "ENROLL-" + "D" * 32)
        self.assertNotIn("sudo sh", macos["command"])

        bootstrap = catalog.bootstrap(
            runner_id="RN-" + "C" * 20,
            enrollment_token="ENROLL-" + "D" * 32,
            os_name="macos",
            arch="aarch64",
            projects=["renovation-hub"],
            labels=["local", "macos"],
            policy_revision=2,
        )
        self.assertEqual(bootstrap["asset_sha256"], "5" * 64)
        self.assertEqual(bootstrap["asset_size"], 2004)
        self.assertEqual(bootstrap["installer_sha256"], "1" * 64)
        self.assertEqual(bootstrap["labels"], ["local", "macos"])
        self.assertEqual(bootstrap["policy_revision"], 2)

    def test_manifest_digest_mismatch_and_version_drift_fail_closed(self) -> None:
        body = manifest_bytes()
        mismatch = self.catalog(body, digest="f" * 64)
        self.assertEqual(
            mismatch.status(),
            {
                "ready": False,
                "error_code": "installer_manifest_digest_mismatch",
                "runner_version": "0.3.23",
            },
        )

        drifted = manifest_document()
        drifted["runner_version"] = "0.2.1"
        drifted_body = manifest_bytes(drifted)
        with self.assertRaises(StoreError) as context:
            self.catalog(drifted_body).manifest()
        self.assertEqual(context.exception.code, "installer_manifest_version_mismatch")

    def test_pinned_manifest_body_avoids_runtime_network_dependency(self) -> None:
        body = manifest_bytes()
        catalog = RunnerInstallerCatalog(
            "https://downloads.example.com/codex-runner/manifest.json",
            hashlib.sha256(body).hexdigest(),
            "wss://runner.example.com/v1/connect",
            pinned_manifest_body=body,
            opener=ForbiddenOpener(),
            resolver=public_resolver,
        )

        status = catalog.status()

        self.assertEqual(status["ready"], True)
        self.assertEqual(status["runner_version"], "0.3.23")

    def test_pinned_manifest_body_digest_mismatch_fails_closed(self) -> None:
        catalog = RunnerInstallerCatalog(
            "https://downloads.example.com/codex-runner/manifest.json",
            "f" * 64,
            "wss://runner.example.com/v1/connect",
            pinned_manifest_body=manifest_bytes(),
            opener=ForbiddenOpener(),
            resolver=public_resolver,
        )

        self.assertEqual(
            catalog.status(),
            {
                "ready": False,
                "error_code": "installer_manifest_digest_mismatch",
                "runner_version": "0.3.23",
            },
        )

    def test_packaged_runner_0321_manifest_matches_frozen_candidate_digest(self) -> None:
        body = Path(codex_controller.__file__).with_name("runner_manifest_v0321.json").read_bytes()
        self.assertEqual(
            hashlib.sha256(body).hexdigest(),
            "1f25f83f669d4ad403741d9d6283831750ccbf262a5bc8cbc40d0197943bf488",
        )
        catalog = RunnerInstallerCatalog(
            "https://github.com/example/project/releases/download/codex-runner-v0.3.21/manifest.json",
            hashlib.sha256(body).hexdigest(),
            "wss://runner.example.com/v1/connect",
            pinned_manifest_body=body,
            opener=ForbiddenOpener(),
            resolver=public_resolver,
        )

        status = catalog.status()

        self.assertEqual(
            status,
            {
                "ready": False,
                "error_code": "installer_manifest_version_mismatch",
                "runner_version": "0.3.23",
            },
        )

    def test_packaged_runner_0322_manifest_matches_frozen_candidate_digest(self) -> None:
        package_root = Path(codex_controller.__file__).parent
        body = (package_root / "runner_manifest_v0322.json").read_bytes()
        self.assertEqual(
            hashlib.sha256(body).hexdigest(),
            "2ac0ee443f918ac32031b4b6129b44556303cf4c0866bb4c9705bc3e9a649489",
        )
        catalog = RunnerInstallerCatalog(
            "https://github.com/example/project/releases/download/codex-runner-v0.3.22/manifest.json",
            hashlib.sha256(body).hexdigest(),
            "wss://runner.example.com/v1/connect",
            pinned_manifest_body=body,
            opener=ForbiddenOpener(),
            resolver=public_resolver,
        )

        status = catalog.status()

        self.assertEqual(status["ready"], False)
        self.assertEqual(status["error_code"], "installer_manifest_version_mismatch")
        self.assertEqual(status["runner_version"], "0.3.23")

    def test_packaged_runner_0323_manifest_matches_frozen_candidate_digest(self) -> None:
        package_root = Path(codex_controller.__file__).parent
        body = (package_root / "runner_manifest_v0323.json").read_bytes()
        self.assertEqual(
            hashlib.sha256(body).hexdigest(),
            "50e407811dd3eda48b96e35adfe3312e50c5178fec9e69ae3d1deea360d79fa6",
        )
        self.assertIn(
            'with_name("runner_manifest_v0323.json")',
            (package_root / "main.py").read_text(encoding="utf-8"),
        )
        catalog = RunnerInstallerCatalog(
            "https://github.com/example/project/releases/download/codex-runner-v0.3.23/manifest.json",
            hashlib.sha256(body).hexdigest(),
            "wss://runner.example.com/v1/connect",
            pinned_manifest_body=body,
            opener=ForbiddenOpener(),
            resolver=public_resolver,
        )

        status = catalog.status()

        self.assertEqual(status["ready"], True)
        self.assertEqual(status["runner_version"], "0.3.23")
        self.assertEqual(status["codex_version"], "0.146.0")
        self.assertEqual(status["python_version"], "3.11.13")

    def test_manifest_requires_all_four_assets_and_public_https_urls(self) -> None:
        incomplete = manifest_document()
        incomplete["assets"].pop("macos-aarch64")
        body = manifest_bytes(incomplete)
        with self.assertRaises(StoreError) as context:
            self.catalog(body).manifest()
        self.assertEqual(context.exception.code, "installer_manifest_invalid")

        with self.assertRaises(ValueError):
            RunnerInstallerCatalog(
                "https://downloads.example.com/manifest.json",
                "a" * 64,
                "wss://runner.example.com/connect",
                opener=Opener(manifest_bytes()),
                resolver=private_resolver,
            )


if __name__ == "__main__":
    unittest.main()
