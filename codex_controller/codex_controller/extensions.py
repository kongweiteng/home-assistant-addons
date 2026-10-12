"""Administrator-registered HTTP JSON extensions, isolated from model credentials."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import http.client
import json
from pathlib import Path
import re
import stat
from typing import Any, Callable
from urllib.parse import urlsplit

from .extension_schema import SchemaError, bounded_json, validate_schema, validate_value
from .skills import SkillError, private_text
from .tool_catalog import ToolDefinition

ID_RE = re.compile(r"^[a-z][a-z0-9]{0,23}$")
LOCAL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
HOST_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
DIGEST_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
SENSITIVE_KEYS = re.compile(r"(?:password|secret|token|authorization|cookie|private.?key|bearer)", re.I)


class ExtensionError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ExtensionTool:
    definition: ToolDefinition
    local_name: str
    base_url: str
    extension_id: str
    output_schema: dict[str, Any]
    manifest_digest: str


def base_url(value: Any) -> str:
    if not isinstance(value, str):
        raise ExtensionError("extension_config_invalid")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ExtensionError("extension_config_invalid") from exc
    # Add-on/container DNS names only. No arbitrary web targets, paths or redirects.
    if (parsed.scheme != "http" or not parsed.hostname or not HOST_RE.fullmatch(parsed.hostname)
            or parsed.username or parsed.password or parsed.path not in {"", "/"}
            or parsed.query or parsed.fragment or port is None or not 1 <= port <= 65535):
        raise ExtensionError("extension_config_invalid")
    if parsed.hostname in {"supervisor", "homeassistant", "hassio", "localhost"}:
        raise ExtensionError("extension_config_invalid")
    return f"http://{parsed.hostname}:{port}"


def request_extension(url: str, token: str, payload: dict[str, Any]) -> dict[str, Any]:
    parsed = urlsplit(url)
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=15)
    try:
        connection.request("POST", "/internal/v1/tools/call", body=bounded_json(payload),
                           headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        response = connection.getresponse()
        if response.status != 200:  # Deliberately do not follow 3xx or expose error bodies.
            raise ExtensionError("extension_upstream_rejected")
        data = response.read(131073)
        if len(data) > 131072:
            raise ExtensionError("extension_response_invalid")
        document = json.loads(data)
        if not isinstance(document, dict):
            raise ExtensionError("extension_response_invalid")
        return document
    except ExtensionError:
        raise
    except (OSError, http.client.HTTPException, ValueError, SchemaError) as exc:
        raise ExtensionError("extension_upstream_unavailable") from exc
    finally:
        connection.close()


def safe_result(value: Any, token: str, depth: int = 0) -> Any:
    if depth > 8:
        raise ExtensionError("extension_response_invalid")
    if isinstance(value, dict):
        if any(not isinstance(key, str) or SENSITIVE_KEYS.search(key) for key in value):
            raise ExtensionError("extension_response_unsafe")
        return {key: safe_result(child, token, depth + 1) for key, child in value.items()}
    if isinstance(value, list):
        return [safe_result(child, token, depth + 1) for child in value]
    if isinstance(value, str) and (token in value or re.search(r"(?i)bearer\s+\S+|-----BEGIN .*PRIVATE KEY-----", value)):
        raise ExtensionError("extension_response_unsafe")
    return value


class ExtensionRegistry:
    def __init__(self, root: Path, secrets_root: Path,
                 request: Callable[[str, str, dict[str, Any]], dict[str, Any]] | None = None):
        self.root = root
        self.secrets_root = secrets_root
        self.request = request or request_extension
        self.tools: dict[str, ExtensionTool] = {}
        self.errors: list[dict[str, str]] = []

    def refresh(self) -> None:
        tools: dict[str, ExtensionTool] = {}
        errors: list[dict[str, str]] = []
        if not (self.root / "registry.json").exists():
            self.tools, self.errors = {}, []
            return
        try:
            registry = json.loads(private_text(self.root, "registry.json", 65536))
            if (not isinstance(registry, dict) or set(registry) != {"version", "extensions"}
                    or type(registry["version"]) is not int or registry["version"] != 1
                    or not isinstance(registry["extensions"], list) or len(registry["extensions"]) > 32):
                raise ExtensionError("extension_registry_invalid")
            seen: set[str] = set()
            for item in registry["extensions"]:
                if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not ID_RE.fullmatch(item["id"]) or item["id"] in seen:
                    raise ExtensionError("extension_registry_invalid")
                seen.add(item["id"])
            for item in registry["extensions"]:
                extension_id = item["id"]
                try:
                    if (set(item) != {"id", "enabled", "base_url", "manifest_sha256", "allow_write"}
                            or type(item["enabled"]) is not bool or type(item["allow_write"]) is not bool):
                        raise ExtensionError("extension_config_invalid")
                    if not item["enabled"]:
                        continue
                    url = base_url(item["base_url"])
                    digest = item["manifest_sha256"]
                    if not isinstance(digest, str) or not DIGEST_RE.fullmatch(digest):
                        raise ExtensionError("extension_digest_invalid")
                    raw = private_text(self.root, f"{extension_id}/manifest.json", 524288)
                    if "sha256:" + hashlib.sha256(raw.encode()).hexdigest() != digest:
                        raise ExtensionError("extension_digest_mismatch")
                    manifest = json.loads(raw)
                    if (not isinstance(manifest, dict) or set(manifest) != {"version", "tools"}
                            or type(manifest["version"]) is not int or manifest["version"] != 1
                            or not isinstance(manifest["tools"], list) or not 1 <= len(manifest["tools"]) <= 64):
                        raise ExtensionError("extension_manifest_invalid")
                    entries: dict[str, ExtensionTool] = {}
                    for tool in manifest["tools"]:
                        entry = self._tool(extension_id, url, digest, tool, item["allow_write"])
                        if entry.definition.name in entries:
                            raise ExtensionError("extension_manifest_invalid")
                        entries[entry.definition.name] = entry
                    # Credentials must be configured before the catalog is published.
                    self._token(extension_id)
                    if len(tools) + len(entries) > 256:
                        raise ExtensionError("extension_catalog_too_large")
                    tools.update(entries)
                except (ExtensionError, SkillError, SchemaError, ValueError, TypeError, RecursionError) as exc:
                    code = exc.code if isinstance(exc, ExtensionError) else "extension_manifest_invalid"
                    errors.append({"id": extension_id, "error_code": code})
        except (ExtensionError, SkillError, ValueError, TypeError, RecursionError):
            tools = {}
            errors = [{"error_code": "extension_registry_invalid"}]
        self.tools, self.errors = tools, errors

    @staticmethod
    def _tool(extension_id: str, url: str, digest: str, value: Any, allow_write: bool) -> ExtensionTool:
        if not isinstance(value, dict) or set(value) != {"name", "display_name", "description", "risk_type", "inputSchema", "outputSchema"}:
            raise ExtensionError("extension_manifest_invalid")
        name = value["name"]
        if not isinstance(name, str) or not LOCAL_NAME_RE.fullmatch(name):
            raise ExtensionError("extension_manifest_invalid")
        if any(not isinstance(value[key], str) or not 1 <= len(value[key]) <= limit
               for key, limit in (("display_name", 80), ("description", 2000))):
            raise ExtensionError("extension_manifest_invalid")
        risk = value["risk_type"]
        if risk not in {"read_only", "write"} or (risk == "write" and not allow_write):
            raise ExtensionError("extension_write_not_allowed")
        for key in ("inputSchema", "outputSchema"):
            validate_schema(value[key])
            if value[key]["type"] != "object":
                raise ExtensionError("extension_manifest_invalid")
        definition = ToolDefinition(
            name=f"ext_{extension_id}_{name}", display_name=value["display_name"],
            service=f"extension:{extension_id}", risk_type=risk, description=value["description"],
            intent_examples=(), input_schema=value["inputSchema"], requires_job_context=True,
            idempotent_write=risk == "write", transport="extension_json",
        )
        return ExtensionTool(definition, name, url, extension_id, value["outputSchema"], digest)

    def _token(self, extension_id: str) -> str:
        try:
            path = self.secrets_root / f"{extension_id}.json"
            if stat.S_IMODE(self.secrets_root.stat().st_mode) != 0o700 or stat.S_IMODE(path.stat().st_mode) != 0o600:
                raise ExtensionError("extension_credentials_unavailable")
            value = json.loads(private_text(self.secrets_root, path.name, 8192))
            token = value.get("bearer") if isinstance(value, dict) and set(value) == {"bearer"} else None
            if not isinstance(token, str) or not 32 <= len(token) <= 4096 or any(ord(c) < 33 or ord(c) > 126 for c in token):
                raise ExtensionError("extension_credentials_unavailable")
            return token
        except (OSError, SkillError, ValueError) as exc:
            raise ExtensionError("extension_credentials_unavailable") from exc

    def definitions(self) -> tuple[ToolDefinition, ...]:
        return tuple(entry.definition for entry in self.tools.values())

    def fingerprint(self) -> dict[str, str]:
        return {name: hashlib.sha256(bounded_json({"manifest": entry.manifest_digest, "base_url": entry.base_url})).hexdigest()
                for name, entry in self.tools.items()}

    def call(self, name: str, arguments: dict[str, Any], context: dict[str, str]) -> dict[str, Any]:
        entry = self.tools.get(name)
        if entry is None:
            raise ExtensionError("extension_unavailable")
        try:
            bounded_json(arguments)
            validate_value(arguments, entry.definition.input_schema)
        except SchemaError as exc:
            raise ExtensionError("extension_arguments_invalid") from exc
        token = self._token(entry.extension_id)
        result = self.request(entry.base_url, token, {"version": 1, "name": entry.local_name, "arguments": arguments, "context": context})
        try:
            bounded_json(result)
            validate_value(result, entry.output_schema)
        except (SchemaError, RecursionError) as exc:
            raise ExtensionError("extension_response_invalid") from exc
        return safe_result(result, token)
