"""Strict Renovation Hub business-tool manifest validation and bootstrap support."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Iterable

from .tool_catalog import BOOTSTRAP_HUB_DEFINITIONS, ToolDefinition, tool_definition_from_manifest


MANIFEST_VERSION = 1
MANIFEST_SERVICE = "renovation_hub"
MANIFEST_SCOPE = "business"
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_TOOL_COUNT = 256
MAX_SCHEMA_BYTES = 64 * 1024
MAX_DESCRIPTION_CHARS = 2000
MAX_DISPLAY_NAME_CHARS = 80
MAX_INTENT_EXAMPLES = 8
TOOL_NAME_RE = re.compile(r"^(?:ledger|renovation)_[a-z0-9_]{1,79}$")
DIGEST_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
TRANSPORTS = frozenset({"json", "gateway_attachment", "gateway_media_stream"})
RISK_TYPES = frozenset({"read", "write"})
ANNOTATION_KEYS = frozenset(
    {"readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"}
)
TOOL_REQUIRED_KEYS = frozenset(
    {
        "name",
        "display_name",
        "description",
        "risk_type",
        "transport",
        "exposure",
        "requires_job_context",
        "idempotent_write",
        "inputSchema",
        "annotations",
    }
)
TOOL_OPTIONAL_KEYS: frozenset[str] = frozenset()


class HubManifestError(ValueError):
    """A deterministic, safe-to-expose manifest validation failure."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def manifest_digest(document: dict[str, Any]) -> str:
    payload = {key: value for key, value in document.items() if key != "catalog_digest"}
    return f"sha256:{hashlib.sha256(canonical_json(payload).encode('utf-8')).hexdigest()}"


@dataclass(frozen=True)
class ValidatedHubManifest:
    document: dict[str, Any]
    definitions: tuple[ToolDefinition, ...]
    revision: int
    digest: str

    @property
    def names(self) -> frozenset[str]:
        return frozenset(definition.name for definition in self.definitions)


def _bounded_string(value: Any, *, field: str, maximum: int, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value) or len(value) > maximum:
        raise HubManifestError("manifest_tool_invalid", f"Hub manifest {field} 无效")
    if value.strip() != value or any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise HubManifestError("manifest_tool_invalid", f"Hub manifest {field} 包含无效字符")
    return value


def _validate_annotations(
    value: Any,
    risk_type: str,
    *,
    idempotent_write: bool,
) -> dict[str, bool]:
    if not isinstance(value, dict) or set(value) != ANNOTATION_KEYS:
        raise HubManifestError("manifest_annotations_invalid", "Hub manifest annotations 字段无效")
    if any(not isinstance(value[key], bool) for key in ANNOTATION_KEYS):
        raise HubManifestError("manifest_annotations_invalid", "Hub manifest annotations 类型无效")
    expected_read_only = risk_type == "read"
    expected_idempotent = expected_read_only or idempotent_write
    if (
        value["readOnlyHint"] is not expected_read_only
        or (expected_read_only and value["destructiveHint"] is not False)
        or value["idempotentHint"] is not expected_idempotent
        or value["openWorldHint"] is not False
    ):
        raise HubManifestError("manifest_annotations_invalid", "Hub manifest annotations 与风险类型不一致")
    return {key: value[key] for key in sorted(ANNOTATION_KEYS)}


def _validate_schema(value: Any, *, require_closed: bool) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("type") != "object":
        raise HubManifestError("manifest_schema_invalid", "Hub manifest inputSchema 必须是 object Schema")
    try:
        encoded = canonical_json(value).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise HubManifestError("manifest_schema_invalid", "Hub manifest inputSchema 不是有效 JSON") from exc
    if len(encoded) > MAX_SCHEMA_BYTES:
        raise HubManifestError("manifest_schema_invalid", "Hub manifest inputSchema 过大")
    properties = value.get("properties", {})
    required = value.get("required", [])
    if not isinstance(properties, dict) or not isinstance(required, list):
        raise HubManifestError("manifest_schema_invalid", "Hub manifest inputSchema 字段无效")
    if any(not isinstance(name, str) or name not in properties for name in required):
        raise HubManifestError("manifest_schema_invalid", "Hub manifest inputSchema required 无效")
    if require_closed and value.get("additionalProperties") is not False:
        raise HubManifestError("manifest_schema_invalid", "Hub manifest inputSchema 必须拒绝未知字段")
    return json.loads(encoded)


def _validate_tool(value: Any, *, require_closed_schema: bool) -> dict[str, Any]:
    if not isinstance(value, dict) or not TOOL_REQUIRED_KEYS.issubset(value) or set(value) - TOOL_REQUIRED_KEYS - TOOL_OPTIONAL_KEYS:
        raise HubManifestError("manifest_tool_invalid", "Hub manifest 工具字段集合无效")
    name = value.get("name")
    if not isinstance(name, str) or not TOOL_NAME_RE.fullmatch(name):
        raise HubManifestError("manifest_tool_name_invalid", "Hub manifest 工具名不在业务命名空间")
    display_name = _bounded_string(value.get("display_name"), field="display_name", maximum=MAX_DISPLAY_NAME_CHARS)
    description = _bounded_string(value.get("description"), field="description", maximum=MAX_DESCRIPTION_CHARS)
    risk_type = value.get("risk_type")
    transport = value.get("transport")
    requires_job_context = value.get("requires_job_context")
    idempotent_write = value.get("idempotent_write")
    if risk_type not in RISK_TYPES:
        raise HubManifestError("manifest_risk_invalid", "Hub manifest risk_type 无效")
    if transport not in TRANSPORTS:
        raise HubManifestError("manifest_transport_invalid", "Hub manifest transport 无效")
    if not isinstance(requires_job_context, bool) or not isinstance(idempotent_write, bool):
        raise HubManifestError("manifest_tool_invalid", "Hub manifest 写入属性类型无效")
    if value.get("exposure") != "mcp":
        raise HubManifestError("manifest_exposure_invalid", "Hub manifest exposure 无效")
    read_only = risk_type == "read"
    if read_only and (transport != "json" or requires_job_context or idempotent_write):
        raise HubManifestError("manifest_risk_invalid", "Hub manifest 只读工具声明了写入能力")
    if not read_only and requires_job_context is not idempotent_write:
        raise HubManifestError("manifest_risk_invalid", "Hub manifest 写工具的上下文和幂等声明不一致")
    if transport != "json" and (read_only or not requires_job_context or not idempotent_write):
        raise HubManifestError("manifest_transport_invalid", "Hub manifest 受控附件传输必须是上下文内幂等写入")
    if transport == "gateway_media_stream" and name != "renovation_media_ingest":
        raise HubManifestError("manifest_transport_invalid", "Hub manifest 媒体流传输工具不受支持")
    return {
        "name": name,
        "display_name": display_name,
        "description": description,
        "risk_type": risk_type,
        "transport": transport,
        "exposure": "mcp",
        "requires_job_context": requires_job_context,
        "idempotent_write": idempotent_write,
        "inputSchema": _validate_schema(
            value.get("inputSchema"),
            require_closed=require_closed_schema,
        ),
        "annotations": _validate_annotations(
            value.get("annotations"),
            risk_type,
            idempotent_write=idempotent_write,
        ),
    }


def validate_hub_manifest(
    value: Any,
    *,
    require_closed_schema: bool = True,
) -> ValidatedHubManifest:
    if not isinstance(value, dict):
        raise HubManifestError("manifest_invalid", "Hub manifest 不是对象")
    if set(value) != {"version", "service", "scope", "catalog_revision", "catalog_digest", "tools"}:
        raise HubManifestError("manifest_invalid", "Hub manifest 顶层字段无效")
    try:
        encoded = canonical_json(value).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise HubManifestError("manifest_invalid", "Hub manifest 不是有效 JSON") from exc
    if len(encoded) > MAX_MANIFEST_BYTES:
        raise HubManifestError("manifest_too_large", "Hub manifest 超过大小上限")
    if value.get("version") != MANIFEST_VERSION or value.get("service") != MANIFEST_SERVICE or value.get("scope") != MANIFEST_SCOPE:
        raise HubManifestError("manifest_identity_invalid", "Hub manifest 身份无效")
    revision = value.get("catalog_revision")
    digest = value.get("catalog_digest")
    tools = value.get("tools")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise HubManifestError("manifest_revision_invalid", "Hub manifest revision 无效")
    if not isinstance(digest, str) or not DIGEST_RE.fullmatch(digest):
        raise HubManifestError("manifest_digest_invalid", "Hub manifest digest 格式无效")
    if not isinstance(tools, list) or not tools or len(tools) > MAX_TOOL_COUNT:
        raise HubManifestError("manifest_tools_invalid", "Hub manifest 工具数量无效")
    normalized_tools = [
        _validate_tool(tool, require_closed_schema=require_closed_schema)
        for tool in tools
    ]
    names = [tool["name"] for tool in normalized_tools]
    if len(names) != len(set(names)):
        raise HubManifestError("manifest_duplicate_tool", "Hub manifest 包含重复工具")
    normalized = {
        "version": MANIFEST_VERSION,
        "service": MANIFEST_SERVICE,
        "scope": MANIFEST_SCOPE,
        "catalog_revision": revision,
        "catalog_digest": digest,
        "tools": sorted(normalized_tools, key=lambda tool: tool["name"]),
    }
    if manifest_digest(normalized) != digest:
        raise HubManifestError("manifest_digest_mismatch", "Hub manifest digest 不一致")
    definitions = tuple(tool_definition_from_manifest(tool) for tool in normalized["tools"])
    return ValidatedHubManifest(normalized, definitions, revision, digest)


def _manifest_tool(definition: ToolDefinition) -> dict[str, Any]:
    annotations = definition.mcp_document().get("annotations") or {
        "readOnlyHint": definition.read_only,
        "destructiveHint": False,
        "idempotentHint": definition.read_only or definition.idempotent_write,
        "openWorldHint": False,
    }
    return {
        "name": definition.name,
        "display_name": definition.display_name,
        "description": definition.description,
        "risk_type": "read" if definition.read_only else "write",
        "transport": definition.transport,
        "exposure": "mcp",
        "requires_job_context": definition.requires_job_context,
        "idempotent_write": definition.idempotent_write,
        "inputSchema": definition.input_schema,
        "annotations": annotations,
    }


def build_bootstrap_manifest(
    definitions: Iterable[ToolDefinition] = BOOTSTRAP_HUB_DEFINITIONS,
) -> ValidatedHubManifest:
    definition_tuple = tuple(definitions)
    document: dict[str, Any] = {
        "version": MANIFEST_VERSION,
        "service": MANIFEST_SERVICE,
        "scope": MANIFEST_SCOPE,
        "catalog_revision": 1,
        "catalog_digest": "",
        "tools": sorted(
            (_manifest_tool(definition) for definition in definition_tuple),
            key=lambda tool: tool["name"],
        ),
    }
    document["catalog_digest"] = manifest_digest(document)
    validated = validate_hub_manifest(document, require_closed_schema=False)
    return ValidatedHubManifest(
        validated.document,
        definition_tuple,
        validated.revision,
        validated.digest,
    )


BOOTSTRAP_MANIFEST = build_bootstrap_manifest()
