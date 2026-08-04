"""Deterministic validation for P4 proposal and approval envelopes."""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urlsplit


ACTION_RISKS = {
    "check_ha_config": "L1",
    "install_addon": "L3",
    "configure_addon": "L3",
    "start_addon": "L3",
    "stop_addon": "L3",
    "restart_addon": "L3",
    "update_addon": "L3",
    "repair_addon": "L3",
    "uninstall_addon": "L3",
    "install_hacs": "L3",
    "update_hacs": "L3",
    "repair_hacs": "L3",
    "remove_hacs": "L3",
    "start_config_flow": "L3",
    "enable_integration": "L3",
    "disable_integration": "L3",
    "reload_integration": "L3",
    "remove_integration": "L3",
    "restart_core": "L3",
    "create_backup": "L3",
    "purge_recorder": "L3",
    "delete_expired_backup": "L3",
    "cleanup_allowlisted_cache": "L3",
}

DEFAULT_POLICY_EPOCH = 1
DEFAULT_POLICY_HASH = "sha256:c408de9e17185d22a780c6e5bca8cb3ad7cb092f46cdfe9a35fe1ecd6a3719b8"
DEFAULT_ADAPTER_VERSION = "manager-restart-v1"
DEFAULT_ADAPTER_SCHEMA_VERSION = 1

ACTION_ID_RE = re.compile(r"^OPS-[0-9]{8}-[A-F0-9]{12}$")
TARGET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")
PARAMETER_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,63}$")
IDENTITY_HASH_RE = re.compile(r"^[a-f0-9]{64}$")
SHA256_RE = re.compile(r"^sha256:([a-f0-9]{64})$")
NATIVE_INTENT_FIELDS = frozenset(
    {
        "version",
        "action_type",
        "target",
        "idempotency_key",
    }
)
NATIVE_AUTHORIZATION_REQUEST_FIELDS = frozenset({"version", "action_id"})
EXECUTION_REQUEST_FIELDS = frozenset(
    {"version", "receipt_id", "action_id", "proposal_hash", "idempotency_key"}
)
RECOVERY_RESOLUTION_FIELDS = frozenset(
    {"version", "resolution", "evidence_hash"}
)
RECOVERY_RESOLUTIONS = frozenset({"confirmed_healthy", "compensated"})
BACKUP_EVIDENCE_FIELDS = frozenset(
    {
        "version",
        "scope",
        "logical_id",
        "completed",
        "created_at",
        "size",
        "sha256",
        "off_device_sha256",
        "readable",
        "baseline",
        "expires_at",
    }
)
BACKUP_EVIDENCE_SCOPES = frozenset({"full", "addon", "dashboard", "recorder"})
BACKUP_EVIDENCE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}$")
RECEIPT_ID_RE = re.compile(r"^RCPT-[A-F0-9]{32}$")

SENSITIVE_KEY_PARTS = (
    "accesskey",
    "apikey",
    "authorization",
    "cookie",
    "credential",
    "password",
    "privatekey",
    "refreshtoken",
    "secret",
    "token",
)
SENSITIVE_VALUE_RE = re.compile(
    r"(-----BEGIN [A-Z ]*PRIVATE KEY-----|\bBearer\s+\S+|\bsk-[A-Za-z0-9_-]{16,}|\bAKIA[A-Z0-9]{16}\b)",
    re.IGNORECASE,
)

PROPOSAL_HASH_FIELDS = (
    "version",
    "action_id",
    "action_type",
    "target",
    "parameter_summary",
    "risk_level",
    "requires_backup",
    "expected_change",
    "validation_plan",
    "rollback_plan",
    "created_at",
    "expires_at",
    "state",
)
PROPOSAL_ALLOWED_FIELDS = frozenset(
    (*PROPOSAL_HASH_FIELDS, "parameter_summary_hash", "proposal_hash")
)
NATIVE_PROPOSAL_HASH_FIELDS = (
    "version",
    "action_id",
    "action_type",
    "target",
    "parameter_summary",
    "risk_level",
    "requires_backup",
    "expected_change",
    "validation_plan",
    "rollback_plan",
    "policy_epoch",
    "policy_hash",
    "allowlist_hash",
    "adapter_version",
    "adapter_schema_version",
    "baseline_etag",
    "backup_evidence_id",
    "created_at",
    "expires_at",
    "state",
)
APPROVAL_REQUIRED_FIELDS = frozenset(
    {
        "version",
        "action_id",
        "proposal_hash",
        "state",
        "approved_by_hash",
        "approved_at",
        "expires_at",
    }
)


class ContractError(ValueError):
    """Raised when an untrusted preflight envelope is rejected."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def allowlist_fingerprint(values: frozenset[str]) -> str:
    return "sha256:" + sha256_text(canonical_json(sorted(values)))


def addon_baseline_etag(observation: Any) -> str:
    if not isinstance(observation, dict):
        raise ContractError("baseline_invalid", "Add-on baseline observation is invalid")
    selected = {
        field: observation.get(field)
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
    return "sha256:" + sha256_text(canonical_json(selected))


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_timestamp(value: Any, *, field: str) -> datetime:
    if not isinstance(value, str) or len(value) > 64:
        raise ContractError("invalid_timestamp", f"{field} must be a timestamp string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ContractError("invalid_timestamp", f"{field} is invalid") from exc
    if parsed.tzinfo is None:
        raise ContractError("invalid_timestamp", f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def parse_owner_hashes(raw: str) -> frozenset[str]:
    owners = frozenset(item.strip().lower() for item in raw.split(",") if item.strip())
    if any(not IDENTITY_HASH_RE.fullmatch(item) for item in owners):
        raise ContractError("invalid_owner_config", "Trusted owner hash configuration is invalid")
    return owners


def validate_backup_evidence(value: Any, *, now: datetime) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != BACKUP_EVIDENCE_FIELDS:
        raise ContractError("backup_evidence_fields_invalid", "Backup evidence fields are invalid")
    if value.get("version") != 1:
        raise ContractError("backup_evidence_version_invalid", "Backup evidence version is invalid")
    scope = value.get("scope")
    if scope not in BACKUP_EVIDENCE_SCOPES:
        raise ContractError("backup_evidence_scope_invalid", "Backup evidence scope is invalid")
    logical_id = value.get("logical_id")
    if (
        not isinstance(logical_id, str)
        or not BACKUP_EVIDENCE_ID_RE.fullmatch(logical_id)
        or SENSITIVE_VALUE_RE.search(logical_id)
    ):
        raise ContractError("backup_evidence_id_invalid", "Backup evidence logical ID is invalid")
    if value.get("completed") is not True:
        raise ContractError("backup_evidence_incomplete", "Backup evidence is not completed")
    if value.get("readable") is not True:
        raise ContractError("backup_evidence_unreadable", "Backup evidence is not readable")
    size = value.get("size")
    if (
        not isinstance(size, int)
        or isinstance(size, bool)
        or not 1 <= size <= 9_223_372_036_854_775_807
    ):
        raise ContractError("backup_evidence_size_invalid", "Backup evidence size is invalid")
    for field in ("sha256", "off_device_sha256", "baseline"):
        if not isinstance(value.get(field), str) or not SHA256_RE.fullmatch(value[field]):
            raise ContractError("backup_evidence_hash_invalid", f"Backup evidence {field} is invalid")
    created_at = parse_timestamp(value.get("created_at"), field="backup_evidence.created_at")
    expires_at = parse_timestamp(value.get("expires_at"), field="backup_evidence.expires_at")
    current = now.astimezone(timezone.utc)
    if created_at > current:
        raise ContractError("backup_evidence_created_in_future", "Backup evidence creation time is in the future")
    if expires_at <= current or expires_at <= created_at:
        raise ContractError("backup_evidence_expired", "Backup evidence is expired")
    return {
        "version": 1,
        "scope": scope,
        "logical_id": logical_id,
        "completed": True,
        "created_at": created_at.replace(microsecond=0).isoformat(),
        "size": size,
        "sha256": value["sha256"],
        "off_device_sha256": value["off_device_sha256"],
        "readable": True,
        "baseline": value["baseline"],
        "expires_at": expires_at.replace(microsecond=0).isoformat(),
    }


def _safe_text(value: Any, *, field: str, maximum: int = 300) -> str:
    if not isinstance(value, str):
        raise ContractError("invalid_field", f"{field} must be a string")
    text = value.strip()
    if not text or len(text) > maximum:
        raise ContractError("invalid_field", f"{field} must be 1-{maximum} characters")
    if any(ord(character) < 32 and character not in "\t" for character in text):
        raise ContractError("invalid_field", f"{field} contains control characters")
    if SENSITIVE_VALUE_RE.search(text):
        raise ContractError("sensitive_value", f"{field} contains a secret-like value")
    if "://" in text:
        parsed = urlsplit(text)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ContractError("invalid_url", f"{field} contains an unsupported URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ContractError("sensitive_url", f"{field} contains a credentialed or variable URL")
    return text


def _safe_text_list(value: Any, *, field: str) -> list[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= 10:
        raise ContractError("invalid_field", f"{field} must contain 1-10 items")
    return [_safe_text(item, field=field) for item in value]


def _sanitize_parameter_value(value: Any, *, field: str, depth: int, count: list[int]) -> Any:
    count[0] += 1
    if count[0] > 64 or depth > 4:
        raise ContractError("parameters_too_large", "parameter_summary is too large")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, float) and not math.isfinite(value):
            raise ContractError("invalid_number", f"{field} must be finite")
        return value
    if isinstance(value, str):
        return _safe_text(value, field=field, maximum=256)
    if isinstance(value, list):
        if len(value) > 16:
            raise ContractError("parameters_too_large", f"{field} has too many items")
        return [
            _sanitize_parameter_value(item, field=f"{field}[]", depth=depth + 1, count=count)
            for item in value
        ]
    if isinstance(value, dict):
        if len(value) > 32:
            raise ContractError("parameters_too_large", f"{field} has too many keys")
        result: dict[str, Any] = {}
        for key in sorted(value):
            if not isinstance(key, str) or not PARAMETER_KEY_RE.fullmatch(key):
                raise ContractError("invalid_parameter_key", f"Invalid parameter key in {field}")
            normalized = re.sub(r"[^a-z0-9]", "", key.lower())
            if any(part in normalized for part in SENSITIVE_KEY_PARTS):
                raise ContractError("sensitive_parameter", f"Sensitive parameter key rejected: {key}")
            result[key] = _sanitize_parameter_value(
                value[key], field=f"{field}.{key}", depth=depth + 1, count=count
            )
        return result
    raise ContractError("invalid_parameter_type", f"Unsupported value in {field}")


def sanitize_parameter_summary(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError("invalid_parameters", "parameter_summary must be an object")
    return _sanitize_parameter_value(value, field="parameter_summary", depth=0, count=[0])


def _proposal_hash(proposal: dict[str, Any]) -> tuple[str, str]:
    hash_document = {field: proposal[field] for field in PROPOSAL_HASH_FIELDS}
    parameter_hash = sha256_text(canonical_json(hash_document["parameter_summary"]))
    proposal_hash = sha256_text(canonical_json(hash_document))
    return parameter_hash, proposal_hash


def validate_native_intent(
    intent: Any,
    *,
    restart_addon_allowlist: frozenset[str],
) -> dict[str, Any]:
    """Validate the model-facing minimal intent without accepting policy fields."""
    if not isinstance(intent, dict) or set(intent) != NATIVE_INTENT_FIELDS:
        raise ContractError("invalid_intent", "Operation intent fields are invalid")
    if intent["version"] != 1:
        raise ContractError("unsupported_version", "Operation intent version is unsupported")
    if intent["action_type"] != "restart_addon":
        raise ContractError("unsupported_action", "Only restart_addon is implemented")
    target = intent["target"]
    if not isinstance(target, str) or not re.fullmatch(r"^[a-z0-9][a-z0-9_]{0,63}$", target):
        raise ContractError("invalid_addon_slug", "Add-on target must be an exact slug")
    if target not in restart_addon_allowlist:
        raise ContractError("target_not_allowlisted", "Add-on target is not allowlisted")
    idempotency_key = intent["idempotency_key"]
    if not isinstance(idempotency_key, str) or not SHA256_RE.fullmatch(idempotency_key):
        raise ContractError(
            "invalid_idempotency_key", "idempotency_key must be a SHA-256 identifier"
        )
    return {
        "action_type": "restart_addon",
        "target": target,
        "idempotency_key": idempotency_key,
    }


def validate_native_authorization_request(value: Any) -> str:
    if not isinstance(value, dict) or set(value) != NATIVE_AUTHORIZATION_REQUEST_FIELDS:
        raise ContractError(
            "invalid_authorization_request", "Authorization request fields are invalid"
        )
    if value["version"] != 1:
        raise ContractError("unsupported_version", "Authorization request version is unsupported")
    action_id = value["action_id"]
    if not isinstance(action_id, str) or not ACTION_ID_RE.fullmatch(action_id):
        raise ContractError("invalid_action_id", "Action ID is invalid")
    return action_id


def validate_execution_request(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != EXECUTION_REQUEST_FIELDS:
        raise ContractError("invalid_execution_request", "Execution request fields are invalid")
    if value["version"] != 1:
        raise ContractError("unsupported_version", "Execution request version is unsupported")
    action_id = value["action_id"]
    receipt_id = value["receipt_id"]
    proposal_hash = value["proposal_hash"]
    idempotency_key = value["idempotency_key"]
    if not isinstance(action_id, str) or not ACTION_ID_RE.fullmatch(action_id):
        raise ContractError("invalid_action_id", "Action ID is invalid")
    if not isinstance(receipt_id, str) or not RECEIPT_ID_RE.fullmatch(receipt_id):
        raise ContractError("invalid_receipt_id", "Receipt ID is invalid")
    if not isinstance(proposal_hash, str) or not SHA256_RE.fullmatch(proposal_hash):
        raise ContractError("invalid_hash", "Proposal hash is invalid")
    if not isinstance(idempotency_key, str) or not SHA256_RE.fullmatch(idempotency_key):
        raise ContractError(
            "invalid_idempotency_key", "idempotency_key must be a SHA-256 identifier"
        )
    return {
        "action_id": action_id,
        "receipt_id": receipt_id,
        "proposal_hash": proposal_hash,
        "idempotency_key": idempotency_key,
    }


def validate_recovery_resolution(value: Any) -> dict[str, str]:
    """Validate the internal, metadata-only recovery conclusion contract."""
    if not isinstance(value, dict) or set(value) != RECOVERY_RESOLUTION_FIELDS:
        raise ContractError(
            "invalid_recovery_resolution", "Recovery resolution fields are invalid"
        )
    if value["version"] != 1:
        raise ContractError(
            "unsupported_version", "Recovery resolution version is unsupported"
        )
    resolution = value["resolution"]
    if not isinstance(resolution, str) or resolution not in RECOVERY_RESOLUTIONS:
        raise ContractError(
            "invalid_recovery_resolution", "Recovery resolution is unsupported"
        )
    evidence_hash = value["evidence_hash"]
    if not isinstance(evidence_hash, str) or not SHA256_RE.fullmatch(evidence_hash):
        raise ContractError(
            "invalid_evidence_hash", "evidence_hash must be a SHA-256 identifier"
        )
    return {
        "resolution": resolution,
        "evidence_hash": evidence_hash,
    }


def validate_envelope(
    envelope: Any,
    *,
    trusted_owner_hashes: frozenset[str],
    clock: Callable[[], datetime] = utc_now,
) -> dict[str, Any]:
    if not isinstance(envelope, dict) or set(envelope) != {"version", "proposal", "approval"}:
        raise ContractError("invalid_envelope", "Envelope fields are invalid")
    if envelope["version"] != 1:
        raise ContractError("unsupported_version", "Envelope version is unsupported")
    proposal = envelope["proposal"]
    approval = envelope["approval"]
    if not isinstance(proposal, dict) or set(proposal) != PROPOSAL_ALLOWED_FIELDS:
        raise ContractError("invalid_proposal", "Proposal fields are invalid")
    if not isinstance(approval, dict) or set(approval) != APPROVAL_REQUIRED_FIELDS:
        raise ContractError("invalid_approval", "Approval receipt fields are invalid")

    if proposal["version"] != 1 or approval["version"] != 1:
        raise ContractError("unsupported_version", "Proposal or approval version is unsupported")
    action_id = proposal["action_id"]
    if not isinstance(action_id, str) or not ACTION_ID_RE.fullmatch(action_id):
        raise ContractError("invalid_action_id", "Action ID is invalid")
    if approval["action_id"] != action_id:
        raise ContractError("approval_mismatch", "Approval action ID does not match")

    action_type = proposal["action_type"]
    if action_type not in ACTION_RISKS:
        raise ContractError("unsupported_action", "Action type is not allowlisted")
    if proposal["risk_level"] != ACTION_RISKS[action_type]:
        raise ContractError("risk_mismatch", "Proposal risk level does not match policy")
    if not isinstance(proposal["requires_backup"], bool):
        raise ContractError("invalid_field", "requires_backup must be boolean")
    if proposal["risk_level"] == "L3" and not proposal["requires_backup"]:
        raise ContractError("backup_required", "L3 proposals must require a backup")
    target = proposal["target"]
    if not isinstance(target, str) or not TARGET_RE.fullmatch(target):
        raise ContractError("invalid_target", "Target is not a bounded logical identifier")
    if ".." in target or target.startswith(("/", "\\")):
        raise ContractError("invalid_target", "Target paths are not accepted")

    proposal["parameter_summary"] = sanitize_parameter_summary(proposal["parameter_summary"])
    proposal["expected_change"] = _safe_text(
        proposal["expected_change"], field="expected_change"
    )
    proposal["validation_plan"] = _safe_text_list(
        proposal["validation_plan"], field="validation_plan"
    )
    proposal["rollback_plan"] = _safe_text_list(
        proposal["rollback_plan"], field="rollback_plan"
    )
    if proposal["state"] != "awaiting_approval":
        raise ContractError("invalid_proposal_state", "Immutable proposal state is invalid")

    created_at = parse_timestamp(proposal["created_at"], field="proposal.created_at")
    expires_at = parse_timestamp(proposal["expires_at"], field="proposal.expires_at")
    approved_at = parse_timestamp(approval["approved_at"], field="approval.approved_at")
    approval_expires_at = parse_timestamp(approval["expires_at"], field="approval.expires_at")
    if not created_at < expires_at or approval_expires_at != expires_at:
        raise ContractError("expiry_mismatch", "Proposal and approval expiry are inconsistent")
    if not created_at <= approved_at < expires_at:
        raise ContractError("approval_time_invalid", "Approval time is outside proposal lifetime")
    now = clock().astimezone(timezone.utc)
    if now >= expires_at:
        raise ContractError("approval_expired", "Approval receipt has expired")

    parameter_hash, proposal_hash = _proposal_hash(proposal)
    expected_parameter_hash = f"sha256:{parameter_hash}"
    expected_proposal_hash = f"sha256:{proposal_hash}"
    if proposal["parameter_summary_hash"] != expected_parameter_hash:
        raise ContractError("parameter_hash_mismatch", "Parameter summary hash is invalid")
    if proposal["proposal_hash"] != expected_proposal_hash:
        raise ContractError("proposal_hash_mismatch", "Proposal hash is invalid")
    if approval["proposal_hash"] != expected_proposal_hash:
        raise ContractError("approval_mismatch", "Approval proposal hash does not match")
    if approval["state"] != "approved":
        raise ContractError("not_approved", "Approval receipt is not approved")
    owner_hash = approval["approved_by_hash"]
    if not isinstance(owner_hash, str) or not IDENTITY_HASH_RE.fullmatch(owner_hash):
        raise ContractError("invalid_owner", "Approval owner hash is invalid")
    if owner_hash not in trusted_owner_hashes:
        raise ContractError("owner_not_trusted", "Approval owner is not trusted by the broker")
    if not isinstance(approval["proposal_hash"], str) or not SHA256_RE.fullmatch(
        approval["proposal_hash"]
    ):
        raise ContractError("invalid_hash", "Approval proposal hash format is invalid")

    return {
        "action_id": action_id,
        "action_type": action_type,
        "target": target,
        "proposal_hash": expected_proposal_hash,
        "risk_level": proposal["risk_level"],
        "requires_backup": proposal["requires_backup"],
        "parameter_summary": proposal["parameter_summary"],
        "expected_change": proposal["expected_change"],
        "validation_plan": proposal["validation_plan"],
        "rollback_plan": proposal["rollback_plan"],
        "approved_by_hash": owner_hash,
        "approved_at": approval["approved_at"],
        "expires_at": proposal["expires_at"],
    }
