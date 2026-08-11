from __future__ import annotations

import hashlib
import json
import socket
import unittest

from codex_controller.runner_relay import RunnerInstallerCatalog
from codex_controller.store import StoreError


def public_resolver(_host: str, port: int, **_kwargs: object) -> list[tuple]:
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port))]


def private_resolver(_host: str, port: int, **_kwargs: object) -> list[tuple]:
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.10", port))]


def manifest_document() -> dict:
    return {
        "version": 1,
        "runner_version": "0.2.0",
        "codex_version": "0.146.0",
        "python_version": "3.11.13",
        "installer": {
            "url": "https://downloads.example.com/codex-runner/install.sh",
            "sha256": "1" * 64,
        },
        "assets": {
            "linux-amd64": {
                "url": "https://downloads.example.com/codex-runner/linux-amd64.tar.gz",
                "sha256": "2" * 64,
            },
            "linux-aarch64": {
                "url": "https://downloads.example.com/codex-runner/linux-aarch64.tar.gz",
                "sha256": "3" * 64,
            },
            "macos-amd64": {
                "url": "https://downloads.example.com/codex-runner/macos-amd64.tar.gz",
                "sha256": "4" * 64,
            },
            "macos-aarch64": {
                "url": "https://downloads.example.com/codex-runner/macos-aarch64.tar.gz",
                "sha256": "5" * 64,
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
        self.assertIn("CODEX_RUNNER_ENROLLMENT_TOKEN", linux["command"])
        self.assertIn("sha256sum -c -", linux["command"])
        self.assertIn("sudo --preserve-env=CODEX_RUNNER_ENROLLMENT_TOKEN", linux["command"])
        self.assertIn("--relay-url wss://runner.example.com/v1/connect", linux["command"])
        self.assertIn("--asset-sha256 " + "2" * 64, linux["command"])
        self.assertEqual(linux["runner_version"], "0.2.0")

        macos = catalog.command(
            runner_id="RN-" + "C" * 20,
            enrollment_token="ENROLL-" + "D" * 32,
            os_name="macos",
            arch="aarch64",
            projects=["renovation-hub"],
            manifest=manifest,
        )
        self.assertIn("shasum -a 256 -c -", macos["command"])
        self.assertNotIn("sudo --preserve-env", macos["command"])
        self.assertIn("--asset-sha256 " + "5" * 64, macos["command"])

    def test_manifest_digest_mismatch_and_version_drift_fail_closed(self) -> None:
        body = manifest_bytes()
        mismatch = self.catalog(body, digest="f" * 64)
        self.assertEqual(
            mismatch.status(),
            {
                "ready": False,
                "error_code": "installer_manifest_digest_mismatch",
                "runner_version": "0.2.0",
            },
        )

        drifted = manifest_document()
        drifted["runner_version"] = "0.2.1"
        drifted_body = manifest_bytes(drifted)
        with self.assertRaises(StoreError) as context:
            self.catalog(drifted_body).manifest()
        self.assertEqual(context.exception.code, "installer_manifest_version_mismatch")

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
