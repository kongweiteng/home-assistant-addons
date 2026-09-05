"""Render a short-lived, digest-pinned Runner bootstrap shell."""

from __future__ import annotations

import re
import shlex
from typing import Any
from urllib.parse import urlsplit


RUNNER_ID_RE = re.compile(r"^RN-[A-Z2-7]{20,32}$")
TICKET_RE = re.compile(r"^[A-Za-z0-9_-]{32,512}$")
PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
SUPPORTED_RUNNER_VERSIONS = frozenset(
    {
        "0.3.6",
        "0.3.11",
        "0.3.12",
        "0.3.13",
        "0.3.14",
        "0.3.15",
        "0.3.16",
        "0.3.17",
        "0.3.18",
        "0.3.19",
        "0.3.20",
        "0.3.21",
        "0.3.22",
        "0.3.23",
        "0.3.24",
    }
)
EXPECTED_FIELDS = {
    "runner_id",
    "enrollment_token",
    "relay_url",
    "os",
    "arch",
    "projects",
    "labels",
    "policy_revision",
    "asset_url",
    "asset_sha256",
    "asset_size",
    "installer_url",
    "installer_sha256",
    "installer_size",
    "runner_version",
    "codex_version",
    "python_version",
    "self_contained",
}


class InstallRenderError(ValueError):
    pass


def render_install_script(value: dict[str, Any]) -> str:
    if not isinstance(value, dict) or set(value) != EXPECTED_FIELDS:
        raise InstallRenderError("bootstrap fields are invalid")
    runner_id = _text(value["runner_id"], RUNNER_ID_RE, "runner_id")
    token = _text(value["enrollment_token"], TICKET_RE, "enrollment token")
    os_name = value["os"]
    arch = value["arch"]
    if (os_name, arch) not in {
        ("linux", "amd64"),
        ("linux", "aarch64"),
        ("macos", "amd64"),
        ("macos", "aarch64"),
    }:
        raise InstallRenderError("bootstrap platform is invalid")
    projects = value["projects"]
    if (
        not isinstance(projects, list)
        or not projects
        or len(projects) > 32
        or any(not isinstance(item, str) or not PROJECT_RE.fullmatch(item) for item in projects)
    ):
        raise InstallRenderError("bootstrap projects are invalid")
    labels = value["labels"]
    if (
        not isinstance(labels, list)
        or len(labels) > 32
        or any(not isinstance(item, str) or not LABEL_RE.fullmatch(item) for item in labels)
    ):
        raise InstallRenderError("bootstrap labels are invalid")
    policy_revision = value["policy_revision"]
    if not isinstance(policy_revision, int) or isinstance(policy_revision, bool) or policy_revision < 1:
        raise InstallRenderError("bootstrap policy revision is invalid")
    relay_url = _url(value["relay_url"], "wss")
    installer_url = _url(value["installer_url"], "https")
    asset_url = _url(value["asset_url"], "https")
    installer_sha256 = _text(value["installer_sha256"], SHA256_RE, "installer digest")
    asset_sha256 = _text(value["asset_sha256"], SHA256_RE, "asset digest")
    installer_size = _size(value["installer_size"], "installer size")
    asset_size = _size(value["asset_size"], "asset size")
    if (
        value["runner_version"] not in SUPPORTED_RUNNER_VERSIONS
        or value["codex_version"] != "0.146.0"
        or value["python_version"] != "3.11.13"
        or value["self_contained"] is not True
    ):
        raise InstallRenderError("bootstrap version identity is invalid")

    quoted = shlex.quote
    arguments = [
        "--relay-url",
        relay_url,
        "--runner-id",
        runner_id,
        "--platform",
        os_name,
        "--arch",
        arch,
        "--asset-url",
        asset_url,
        "--asset-sha256",
        asset_sha256,
        "--asset-size",
        str(asset_size),
        "--projects",
        ",".join(projects),
        "--labels",
        ",".join(labels),
        "--policy-revision",
        str(policy_revision),
    ]
    return "\n".join(
        [
            "#!/bin/sh",
            "set -eu",
            "umask 077",
            f"export CODEX_RUNNER_ENROLLMENT_TOKEN={quoted(token)}",
            'installer_tmp=$(mktemp "${TMPDIR:-/tmp}/codex-runner-installer.XXXXXX")',
            "cleanup() {",
            '  rm -f "$installer_tmp"',
            "  unset CODEX_RUNNER_ENROLLMENT_TOKEN",
            "}",
            "trap cleanup EXIT HUP INT TERM",
            "for command in curl wc tr cut; do",
            '  command -v "$command" >/dev/null 2>&1 || { printf \'%s\\n\' "$command is required" >&2; exit 78; }',
            "done",
            f"curl -fsSL --proto '=https' --tlsv1.2 {quoted(installer_url)} -o \"$installer_tmp\"",
            'actual_size=$(wc -c < "$installer_tmp" | tr -d \' \')',
            f'[ "$actual_size" = {quoted(str(installer_size))} ] || {{ printf \'%s\\n\' "Installer size mismatch" >&2; exit 78; }}',
            "if command -v sha256sum >/dev/null 2>&1; then",
            '  actual_sha256=$(sha256sum "$installer_tmp" | cut -d\' \' -f1)',
            "elif command -v shasum >/dev/null 2>&1; then",
            '  actual_sha256=$(shasum -a 256 "$installer_tmp" | cut -d\' \' -f1)',
            "else",
            "  printf '%s\\n' 'SHA-256 command is required' >&2",
            "  exit 78",
            "fi",
            f'[ "$actual_sha256" = {quoted(installer_sha256)} ] || {{ printf \'%s\\n\' "Installer SHA-256 mismatch" >&2; exit 78; }}',
            'chmod 700 "$installer_tmp"',
            'sh "$installer_tmp" ' + " ".join(quoted(item) for item in arguments),
            "",
        ]
    )


def _text(value: Any, pattern: re.Pattern[str], label: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise InstallRenderError(f"{label} is invalid")
    return value


def _size(value: Any, label: str) -> int:
    if type(value) is not int or not 1 <= value <= 1024 * 1024 * 1024:
        raise InstallRenderError(f"{label} is invalid")
    return value


def _url(value: Any, scheme: str) -> str:
    if not isinstance(value, str) or value.strip() != value or "\\" in value:
        raise InstallRenderError("bootstrap URL is invalid")
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise InstallRenderError("bootstrap URL is invalid") from exc
    if (
        parsed.scheme != scheme
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise InstallRenderError("bootstrap URL is invalid")
    return value


__all__ = [
    "InstallRenderError",
    "SUPPORTED_RUNNER_VERSIONS",
    "TICKET_RE",
    "render_install_script",
]
