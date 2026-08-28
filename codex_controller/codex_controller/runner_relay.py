"""Controller adapters for the transport Relay and pinned Runner installer manifest."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import re
import shlex
import socket
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

from .store import StoreError


RUNNER_VERSION = "0.3.14"
CODEX_VERSION = "0.146.0"
PYTHON_VERSION = "3.11.13"
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
RUNNER_ID_RE = re.compile(r"^RN-[A-Z2-7]{20,32}$")
PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
PLATFORM_KEYS = frozenset(
    {"linux-amd64", "linux-aarch64", "macos-amd64", "macos-aarch64"}
)
INTERNAL_RELAY_HOST = "local-codex-runner-relay"
INTERNAL_RELAY_PORT = 8098


class RelayPublishError(RuntimeError):
    """Classify a Relay publish failure without guessing whether delivery occurred."""

    def __init__(self, code: str, *, definitely_undelivered: bool) -> None:
        super().__init__(code)
        self.code = code
        self.definitely_undelivered = definitely_undelivered


def validate_internal_relay_url(value: str) -> str:
    if not isinstance(value, str) or value.strip() != value:
        raise ValueError("Runner Relay 内部 URL 无效")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "http"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or parsed.hostname != INTERNAL_RELAY_HOST
        or parsed.port != INTERNAL_RELAY_PORT
    ):
        raise ValueError("Runner Relay 内部 URL 必须是精确 Add-on HTTP 地址")
    return value.rstrip("/")


def validate_relay_auth_config(
    base_url: str,
    publisher_token: str,
    controller_api_token: str,
) -> str:
    """Validate the two least-privilege Relay identities as one closed configuration."""

    values = (base_url, publisher_token, controller_api_token)
    if not any(values):
        return ""
    if not all(values):
        raise ValueError("Runner Relay 内部 URL、发布 Token 与 Controller 回调 Token 必须同时配置")
    normalized_url = validate_internal_relay_url(base_url)
    for label, token in (
        ("发布", publisher_token),
        ("Controller 回调", controller_api_token),
    ):
        if not isinstance(token, str) or token.strip() != token or len(token) < 32:
            raise ValueError(f"Runner Relay {label} Token 无效")
    if hmac.compare_digest(publisher_token, controller_api_token):
        raise ValueError("Runner Relay 发布 Token 与 Controller 回调 Token 必须不同")
    return normalized_url


def validate_public_url(
    value: str,
    *,
    scheme: str,
    resolver: Callable[..., list[tuple]] = socket.getaddrinfo,
) -> str:
    if not isinstance(value, str) or value.strip() != value or "\\" in value:
        raise ValueError("Runner 公开 URL 无效")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Runner 公开 URL 结构无效") from exc
    if (
        parsed.scheme != scheme
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"Runner 公开 URL 必须使用 {scheme}")
    hostname = parsed.hostname.rstrip(".").lower()
    if hostname in {"localhost", "supervisor", "homeassistant", "hassio"} or hostname.endswith(
        (".local", ".internal", ".home.arpa", ".localhost")
    ):
        raise ValueError("Runner 公开 URL 指向内部主机")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        try:
            ascii_host = hostname.encode("idna").decode("ascii")
            records = resolver(ascii_host, port or (443 if scheme in {"https", "wss"} else 80), type=socket.SOCK_STREAM)
        except (UnicodeError, OSError) as exc:
            raise ValueError("Runner 公开 URL DNS 解析失败") from exc
        addresses: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
        for record in records:
            try:
                addresses.add(ipaddress.ip_address(record[4][0]))
            except (IndexError, TypeError, ValueError) as exc:
                raise ValueError("Runner 公开 URL DNS 响应无效") from exc
        if not addresses or any(not item.is_global for item in addresses):
            raise ValueError("Runner 公开 URL 解析到非公网地址")
    else:
        if not address.is_global:
            raise ValueError("Runner 公开 URL IP 不是公网地址")
    return value


class RelayPublisher:
    """Transport-neutral publisher backed by Relay's authenticated internal HTTP API."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout_seconds: int = 10,
        opener: Callable[..., Any] = urlopen,
    ) -> None:
        self.base_url = validate_internal_relay_url(base_url)
        if not isinstance(token, str) or len(token) < 32:
            raise ValueError("Runner Relay API token 无效")
        if not isinstance(timeout_seconds, int) or isinstance(timeout_seconds, bool) or not 1 <= timeout_seconds <= 60:
            raise ValueError("Runner Relay timeout 无效")
        self._token = token
        self._timeout_seconds = timeout_seconds
        self._opener = opener

    def publish_request(self, runner_id: str, document: dict[str, Any]) -> None:
        self._publish(runner_id, "request", document)

    def publish_control(self, runner_id: str, document: dict[str, Any]) -> None:
        self._publish(runner_id, "control", document)

    def publish_desktop_command(self, runner_id: str, document: dict[str, Any]) -> None:
        self._publish(runner_id, "desktop_command", document)

    def _publish(self, runner_id: str, kind: str, document: dict[str, Any]) -> None:
        if not RUNNER_ID_RE.fullmatch(runner_id) or kind not in {
            "request",
            "control",
            "desktop_command",
        }:
            raise RuntimeError("Runner Relay publish target 无效")
        body = json.dumps(
            {"document": document}, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        maximum = 512 * 1024 if kind == "desktop_command" else 64 * 1024
        if len(body) > maximum:
            raise RuntimeError("Runner Relay publish document 过大")
        request = Request(
            f"{self.base_url}/internal/v1/runners/{runner_id}/{kind}",
            data=body,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "codex-controller/0.5.20",
            },
            method="POST",
        )
        try:
            with self._opener(request, timeout=self._timeout_seconds) as response:
                raw = response.read(64 * 1024 + 1)
                status = int(getattr(response, "status", 0))
        except HTTPError as exc:
            try:
                error_body = exc.read(1025)
            except OSError:
                error_body = b""
            if (
                exc.code == 503
                and len(error_body) <= 1024
                and error_body.decode("utf-8", errors="replace").strip() == "runner_offline"
            ):
                raise RelayPublishError(
                    "runner_offline", definitely_undelivered=True
                ) from exc
            raise RelayPublishError(
                "relay_publish_indeterminate", definitely_undelivered=False
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise RelayPublishError(
                "relay_publish_indeterminate", definitely_undelivered=False
            ) from exc
        if (
            status == 503
            and len(raw) <= 1024
            and raw.decode("utf-8", errors="replace").strip() == "runner_offline"
        ):
            raise RelayPublishError("runner_offline", definitely_undelivered=True)
        if status != 202 or len(raw) > 64 * 1024:
            raise RelayPublishError(
                "relay_publish_indeterminate", definitely_undelivered=False
            )


class RunnerInstallerCatalog:
    """Loads one digest-pinned manifest and renders a one-time install link."""

    def __init__(
        self,
        manifest_url: str,
        manifest_sha256: str,
        relay_url: str,
        *,
        timeout_seconds: int = 10,
        cache_seconds: int = 300,
        pinned_manifest_body: bytes | None = None,
        opener: Callable[..., Any] = urlopen,
        resolver: Callable[..., list[tuple]] = socket.getaddrinfo,
    ) -> None:
        self.manifest_url = validate_public_url(manifest_url, scheme="https", resolver=resolver)
        if not SHA256_RE.fullmatch(manifest_sha256):
            raise ValueError("Runner installer manifest SHA-256 无效")
        self.manifest_sha256 = manifest_sha256
        self.relay_url = validate_public_url(relay_url, scheme="wss", resolver=resolver)
        if not isinstance(timeout_seconds, int) or isinstance(timeout_seconds, bool) or not 1 <= timeout_seconds <= 60:
            raise ValueError("Runner installer timeout 无效")
        if not isinstance(cache_seconds, int) or isinstance(cache_seconds, bool) or cache_seconds < 0:
            raise ValueError("Runner installer cache 无效")
        self.timeout_seconds = timeout_seconds
        self.cache_seconds = cache_seconds
        if pinned_manifest_body is not None and not isinstance(pinned_manifest_body, bytes):
            raise ValueError("Runner installer 内置 manifest 无效")
        self._pinned_manifest_body = pinned_manifest_body
        self._opener = opener
        self._resolver = resolver
        self._cached: dict[str, Any] | None = None
        self._cached_at = 0.0
        self.last_error: str | None = None

    def status(self) -> dict[str, Any]:
        try:
            manifest = self.manifest()
        except StoreError as exc:
            return {"ready": False, "error_code": exc.code, "runner_version": RUNNER_VERSION}
        return {
            "ready": True,
            "error_code": None,
            "runner_version": manifest["runner_version"],
            "codex_version": manifest["codex_version"],
            "python_version": manifest["python_version"],
        }

    def manifest(self) -> dict[str, Any]:
        now = time.monotonic()
        if self._cached is not None and now - self._cached_at < self.cache_seconds:
            return self._cached
        if self._pinned_manifest_body is not None:
            manifest = self._manifest_from_raw(self._pinned_manifest_body)
            self._cached = manifest
            self._cached_at = now
            self.last_error = None
            return manifest
        request = Request(
            self.manifest_url,
            headers={"Accept": "application/json", "User-Agent": "codex-controller/0.5.20"},
            method="GET",
        )
        try:
            with self._opener(request, timeout=self.timeout_seconds) as response:
                raw = response.read(64 * 1024 + 1)
                status = int(getattr(response, "status", 0))
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            self.last_error = "installer_manifest_unavailable"
            raise StoreError(
                "installer_manifest_unavailable", "Runner 安装制品 manifest 当前不可用", status=503
            ) from exc
        if status != 200 or len(raw) > 64 * 1024:
            self.last_error = "installer_manifest_invalid"
            raise StoreError("installer_manifest_invalid", "Runner 安装制品 manifest 响应无效", status=503)
        manifest = self._manifest_from_raw(raw)
        self._cached = manifest
        self._cached_at = now
        self.last_error = None
        return manifest

    def _manifest_from_raw(self, raw: bytes) -> dict[str, Any]:
        if len(raw) > 64 * 1024:
            self.last_error = "installer_manifest_invalid"
            raise StoreError("installer_manifest_invalid", "Runner 安装制品 manifest 响应无效", status=503)
        if hashlib.sha256(raw).hexdigest() != self.manifest_sha256:
            self.last_error = "installer_manifest_digest_mismatch"
            raise StoreError(
                "installer_manifest_digest_mismatch", "Runner 安装制品 manifest 摘要不匹配", status=503
            )
        try:
            document = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self.last_error = "installer_manifest_invalid"
            raise StoreError("installer_manifest_invalid", "Runner 安装制品 manifest JSON 无效", status=503) from exc
        manifest = self._validate_manifest(document)
        return manifest

    def command(
        self,
        *,
        runner_id: str,
        enrollment_token: str,
        os_name: str,
        arch: str,
        projects: list[str],
        manifest: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        manifest = self.manifest() if manifest is None else manifest
        platform_key = f"{os_name}-{arch}"
        if platform_key not in PLATFORM_KEYS:
            raise StoreError("runner_platform_unsupported", "Runner 安装平台不受支持", status=409)
        asset = manifest["assets"][platform_key]
        installer = manifest["installer"]
        if not RUNNER_ID_RE.fullmatch(runner_id):
            raise StoreError("runner_payload_invalid", "Runner ID 无效")
        if not isinstance(enrollment_token, str) or len(enrollment_token) < 32:
            raise StoreError("runner_payload_invalid", "Runner enrollment 无效")
        if not projects or any(not PROJECT_RE.fullmatch(value) for value in projects):
            raise StoreError("runner_payload_invalid", "Runner 项目白名单无效")
        parsed_relay = urlsplit(self.relay_url)
        link = urlunsplit(("https", parsed_relay.netloc, f"/install/{enrollment_token}", "", ""))
        quoted = shlex.quote
        execute = 'sh "$installer_tmp"'
        if os_name == "linux":
            execute = (
                "if [ \"$(id -u)\" -eq 0 ]; then sh \"$installer_tmp\"; "
                "else sudo sh \"$installer_tmp\"; fi"
            )
        command = (
            "installer_tmp=$(mktemp \"${TMPDIR:-/tmp}/codex-runner-install.XXXXXX\"); "
            + "trap 'rm -f \"$installer_tmp\"' EXIT; "
            + "curl -fsSL --proto '=https' --tlsv1.2 "
            + quoted(link)
            + ' -o "$installer_tmp"; chmod 700 "$installer_tmp"; '
            + execute
        )
        return {
            "link": link,
            "command": command,
            "runner_version": manifest["runner_version"],
            "codex_version": manifest["codex_version"],
            "python_version": manifest["python_version"],
            "platform": os_name,
            "arch": arch,
            "self_contained": manifest["self_contained"],
        }

    def bootstrap(
        self,
        *,
        runner_id: str,
        enrollment_token: str,
        os_name: str,
        arch: str,
        projects: list[str],
        labels: list[str],
        policy_revision: int,
    ) -> dict[str, Any]:
        manifest = self.manifest()
        platform_key = f"{os_name}-{arch}"
        if platform_key not in PLATFORM_KEYS or platform_key not in manifest["assets"]:
            raise StoreError("runner_platform_unsupported", "Runner 安装平台不受支持", status=409)
        if not RUNNER_ID_RE.fullmatch(runner_id) or not isinstance(enrollment_token, str):
            raise StoreError("runner_payload_invalid", "Runner 安装材料无效")
        if not projects or any(not PROJECT_RE.fullmatch(value) for value in projects):
            raise StoreError("runner_payload_invalid", "Runner 项目白名单无效")
        if len(labels) > 32 or any(not LABEL_RE.fullmatch(value) for value in labels):
            raise StoreError("runner_payload_invalid", "Runner 标签无效")
        if not isinstance(policy_revision, int) or isinstance(policy_revision, bool) or policy_revision < 1:
            raise StoreError("runner_payload_invalid", "Runner policy revision 无效")
        asset = manifest["assets"][platform_key]
        installer = manifest["installer"]
        return {
            "runner_id": runner_id,
            "enrollment_token": enrollment_token,
            "relay_url": self.relay_url,
            "os": os_name,
            "arch": arch,
            "projects": projects,
            "labels": labels,
            "policy_revision": policy_revision,
            "asset_url": asset["url"],
            "asset_sha256": asset["sha256"],
            "asset_size": asset["size"],
            "installer_url": installer["url"],
            "installer_sha256": installer["sha256"],
            "installer_size": installer["size"],
            "runner_version": manifest["runner_version"],
            "codex_version": manifest["codex_version"],
            "python_version": manifest["python_version"],
            "self_contained": manifest["self_contained"],
        }

    def _validate_manifest(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict) or set(value) != {
            "version",
            "runner_version",
            "codex_version",
            "python_version",
            "self_contained",
            "installer",
            "assets",
        }:
            raise StoreError("installer_manifest_invalid", "Runner 安装制品 manifest 字段无效", status=503)
        if (
            value.get("version") != 2
            or value.get("runner_version") != RUNNER_VERSION
            or value.get("codex_version") != CODEX_VERSION
            or value.get("python_version") != PYTHON_VERSION
            or value.get("self_contained") is not True
        ):
            raise StoreError("installer_manifest_version_mismatch", "Runner 安装制品版本不匹配", status=503)
        installer = self._validate_asset(value.get("installer"))
        assets = value.get("assets")
        if not isinstance(assets, dict) or set(assets) != PLATFORM_KEYS:
            raise StoreError("installer_manifest_invalid", "Runner 安装制品平台不完整", status=503)
        normalized_assets = {key: self._validate_asset(assets[key]) for key in sorted(assets)}
        return {**value, "installer": installer, "assets": normalized_assets}

    def _validate_asset(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict) or set(value) != {"url", "sha256", "size"}:
            raise StoreError("installer_manifest_invalid", "Runner 安装制品条目无效", status=503)
        digest = value.get("sha256")
        if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
            raise StoreError("installer_manifest_invalid", "Runner 安装制品摘要无效", status=503)
        size = value.get("size")
        if type(size) is not int or not 1 <= size <= 1024 * 1024 * 1024:
            raise StoreError("installer_manifest_invalid", "Runner 安装制品大小无效", status=503)
        try:
            url = validate_public_url(str(value.get("url")), scheme="https", resolver=self._resolver)
        except ValueError as exc:
            raise StoreError("installer_manifest_invalid", "Runner 安装制品 URL 无效", status=503) from exc
        return {"url": url, "sha256": digest, "size": size}


__all__ = [
    "CODEX_VERSION",
    "PYTHON_VERSION",
    "RUNNER_VERSION",
    "RelayPublishError",
    "RelayPublisher",
    "RunnerInstallerCatalog",
    "validate_relay_auth_config",
    "validate_internal_relay_url",
    "validate_public_url",
]
