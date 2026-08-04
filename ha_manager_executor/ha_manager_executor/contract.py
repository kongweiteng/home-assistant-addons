"""Closed request and evidence contract for restart shadow observations."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any


ACTION_ID_RE = re.compile(r"^OPS-[0-9]{8}-[A-F0-9]{12}$")
ADDON_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_]{0,63}$")
SHA256_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
REQUEST_FIELDS = frozenset(
    {
        "version",
        "action_id",
        "proposal_hash",
        "action_type",
        "target",
        "adapter_version",
        "adapter_schema_version",
        "baseline_etag",
    }
)
EVIDENCE_FIELDS = (
    "slug",
    "state",
    "version",
    "version_latest",
    "update_available",
    "available",
    "installed",
    "protected",
    "rating",
    "hassio_role",
    "hassio_api",
    "homeassistant_api",
    "host_network",
    "full_access",
)


class ContractError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_document(value: Any) -> str:
    digest = hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def addon_baseline_etag(value: Any) -> str:
    normalized = normalize_addon_info(value)
    selected = {
        field: normalized.get(field)
        for field in ("slug", "state", "version", "installed")
    }
    if (
        not isinstance(selected["slug"], str)
        or not selected["slug"]
        or not isinstance(selected["state"], str)
        or not selected["state"]
        or not isinstance(selected["version"], str)
        or not selected["version"]
        or not isinstance(selected["installed"], bool)
    ):
        raise ContractError("baseline_invalid", "Add-on baseline fields are incomplete")
    return sha256_document(selected)


def validate_shadow_request(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError("invalid_request", "Shadow request must be an object")
    if frozenset(value) != REQUEST_FIELDS:
        raise ContractError("invalid_fields", "Shadow request fields are not exact")
    if value.get("version") != 1:
        raise ContractError("invalid_version", "Shadow request version is invalid")
    action_id = value.get("action_id")
    if not isinstance(action_id, str) or not ACTION_ID_RE.fullmatch(action_id):
        raise ContractError("invalid_action_id", "Action ID is invalid")
    proposal_hash = value.get("proposal_hash")
    baseline_etag = value.get("baseline_etag")
    if not isinstance(proposal_hash, str) or not SHA256_RE.fullmatch(proposal_hash):
        raise ContractError("invalid_proposal_hash", "Proposal hash is invalid")
    if not isinstance(baseline_etag, str) or not SHA256_RE.fullmatch(baseline_etag):
        raise ContractError("invalid_baseline_etag", "Baseline etag is invalid")
    if value.get("action_type") != "restart_addon":
        raise ContractError("unsupported_action", "Only restart_addon shadow is supported")
    target = value.get("target")
    if not isinstance(target, str) or not ADDON_SLUG_RE.fullmatch(target):
        raise ContractError("invalid_target", "Target must be an exact Add-on slug")
    if value.get("adapter_version") != "manager-restart-v1":
        raise ContractError("adapter_mismatch", "Adapter version is invalid")
    if value.get("adapter_schema_version") != 1:
        raise ContractError("adapter_schema_mismatch", "Adapter schema version is invalid")
    return dict(value)


def normalize_addon_info(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError("invalid_supervisor_data", "Add-on information is invalid")
    return {field: value.get(field) for field in EVIDENCE_FIELDS if field in value}
