#!/usr/bin/env python3
"""Normalize read-only Home Assistant plugin research evidence.

The program performs no network requests and executes no external commands. It
only validates a bounded JSON document supplied by the research skill.
"""

from __future__ import annotations

import argparse
import datetime as dt
import ipaddress
import json
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import SplitResult, urlsplit, urlunsplit


VERSION = 1
MAX_CANDIDATES = 3
MAX_EVIDENCE = 12
MAX_INPUT_BYTES = 256 * 1024
MAX_PERMISSIONS = 32
UNKNOWN = "unknown"

KINDS = {
    "official": "official_integration",
    "official_integration": "official_integration",
    "integration": "official_integration",
    "addon": "addon",
    "add-on": "addon",
    "hacs": "hacs",
    "manual": "manual_custom_component",
    "manual_custom_component": "manual_custom_component",
    "custom_component": "manual_custom_component",
}
INSTALL_METHODS = {
    "official_integration": "config_flow",
    "addon": "supervisor",
    "hacs": "hacs",
    "manual_custom_component": "manual",
}
COMPATIBILITY = frozenset({"verified", "likely", "unknown", "incompatible"})
SOURCE_TYPES = frozenset({"official", "hacs", "github", "hassbian"})
AUTHORITATIVE_TYPES = frozenset({"official", "hacs", "github"})
CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class ValidationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _text(value: Any, field: str, *, required: bool = True, limit: int = 500) -> str:
    if value is None:
        if required:
            raise ValidationError("missing_field", f"{field} is required")
        return ""
    if not isinstance(value, str):
        raise ValidationError("invalid_field", f"{field} must be a string")
    cleaned = CONTROL_CHARS.sub(" ", value).strip()
    if required and not cleaned:
        raise ValidationError("missing_field", f"{field} is required")
    if len(cleaned) > limit:
        raise ValidationError("field_too_long", f"{field} exceeds {limit} characters")
    return cleaned


def _public_https_url(value: Any, field: str) -> str:
    raw = _text(value, field, limit=2048)
    parsed = urlsplit(raw)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise ValidationError("invalid_url", f"{field} must be an absolute HTTPS URL")
    if parsed.username or parsed.password:
        raise ValidationError("credentialed_url", f"{field} must not contain credentials")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValidationError("invalid_url", f"{field} has an invalid port") from exc
    if port not in (None, 443):
        raise ValidationError("non_public_url", f"{field} must not use a custom port")

    host = parsed.hostname.rstrip(".").lower()
    blocked_suffixes = (".local", ".localhost", ".lan", ".internal", ".invalid", ".test")
    if host == "localhost" or host.endswith(blocked_suffixes):
        raise ValidationError("non_public_url", f"{field} must reference a public host")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address and not address.is_global:
        raise ValidationError("non_public_url", f"{field} must not reference a private address")

    netloc = host
    normalized = SplitResult("https", netloc, parsed.path or "/", parsed.query, parsed.fragment)
    return urlunsplit(normalized)


def _host_matches(source_type: str, url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    if source_type == "official":
        return host == "home-assistant.io" or host.endswith(".home-assistant.io")
    if source_type == "hacs":
        return host == "hacs.xyz" or host.endswith(".hacs.xyz")
    if source_type == "github":
        return host == "github.com" or host.endswith(".github.com")
    if source_type == "hassbian":
        return host == "hassbian.com" or host.endswith(".hassbian.com")
    return False


def _validate_source_contract(
    kind: str, source_url: str, evidence: list[dict[str, str]]
) -> None:
    source_types = {item["source_type"] for item in evidence}
    source_evidence = [item for item in evidence if item["url"] == source_url]
    if not source_evidence:
        raise ValidationError(
            "source_not_evidenced", "source_url must also appear in the evidence list"
        )

    allowed_source_types = {
        "official_integration": {"official", "github"},
        "addon": {"official", "github"},
        "hacs": {"github"},
        "manual_custom_component": {"github"},
    }[kind]
    if source_evidence[0]["source_type"] not in allowed_source_types:
        raise ValidationError(
            "source_kind_mismatch", f"{kind} does not have a valid original source"
        )
    if kind == "official_integration" and "official" not in source_types:
        raise ValidationError(
            "missing_official_evidence",
            "official integrations require Home Assistant official evidence",
        )
    if kind == "hacs" and not {"hacs", "github"}.issubset(source_types):
        raise ValidationError(
            "missing_hacs_evidence",
            "HACS candidates require both HACS and original GitHub evidence",
        )


def _timestamp(value: Any, field: str) -> tuple[str | None, dt.datetime | None]:
    if value in (None, "", UNKNOWN):
        return None, None
    raw = _text(value, field, limit=80)
    candidate = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        parsed = dt.datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ValidationError("invalid_timestamp", f"{field} must be ISO 8601") from exc
    if parsed.tzinfo is None:
        raise ValidationError("invalid_timestamp", f"{field} must include a timezone")
    utc = parsed.astimezone(dt.timezone.utc).replace(microsecond=0)
    return utc.isoformat().replace("+00:00", "Z"), utc


def _as_of(value: str | None) -> dt.datetime:
    if not value:
        return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    normalized, parsed = _timestamp(value, "as_of")
    if normalized is None or parsed is None:
        raise ValidationError("invalid_timestamp", "as_of must not be empty")
    return parsed


def _maintenance_status(last_activity: dt.datetime | None, as_of: dt.datetime) -> str:
    if last_activity is None:
        return UNKNOWN
    age = max(0, (as_of - last_activity).days)
    if age <= 365:
        return "active"
    if age <= 730:
        return "aging"
    return "stale"


def _permissions(value: Any) -> list[str]:
    if value is None:
        return [UNKNOWN]
    if not isinstance(value, list):
        raise ValidationError("invalid_permissions", "required_permissions must be a list")
    if len(value) > MAX_PERMISSIONS:
        raise ValidationError(
            "too_many_permissions", f"at most {MAX_PERMISSIONS} permissions are allowed"
        )
    result: list[str] = []
    for index, item in enumerate(value):
        permission = _text(item, f"required_permissions[{index}]", limit=120)
        if permission not in result:
            result.append(permission)
    return result or []


def _evidence(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list) or not value:
        raise ValidationError("missing_evidence", "at least one evidence item is required")
    if len(value) > MAX_EVIDENCE:
        raise ValidationError(
            "too_many_evidence_items", f"at most {MAX_EVIDENCE} evidence items are allowed"
        )
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValidationError("invalid_evidence", f"evidence[{index}] must be an object")
        source_type = _text(item.get("source_type"), f"evidence[{index}].source_type", limit=30).lower()
        if source_type not in SOURCE_TYPES:
            raise ValidationError("invalid_source_type", f"unsupported evidence source_type: {source_type}")
        url = _public_https_url(item.get("url"), f"evidence[{index}].url")
        if not _host_matches(source_type, url):
            raise ValidationError(
                "source_host_mismatch",
                f"evidence[{index}] host does not match source_type {source_type}",
            )
        note = _text(item.get("note"), f"evidence[{index}].note", required=False, limit=300)
        key = (source_type, url)
        if key in seen:
            continue
        seen.add(key)
        result.append({"source_type": source_type, "url": url, "note": note})
    if not any(item["source_type"] in AUTHORITATIVE_TYPES for item in result):
        raise ValidationError(
            "insufficient_authoritative_evidence",
            "HASSbian or community-only evidence cannot support a candidate",
        )
    return result


def _recommendation(
    *,
    kind: str,
    compatibility: str,
    maintenance_status: str,
    maintainer: str | None,
    latest_release: str | None,
    last_activity_at: str | None,
    evidence: list[dict[str, str]],
) -> tuple[str, list[str]]:
    reasons: list[str] = []
    if compatibility == "incompatible":
        return "reject", ["compatibility_incompatible"]
    if compatibility != "verified":
        reasons.append(f"compatibility_{compatibility}")
    if maintenance_status == "stale":
        reasons.append("maintenance_stale")
    elif maintenance_status == UNKNOWN:
        reasons.append("maintenance_unknown")
    if not maintainer:
        reasons.append("maintainer_unknown")
    if not latest_release:
        reasons.append("latest_release_unknown")
    if not last_activity_at:
        reasons.append("last_activity_unknown")
    if len([item for item in evidence if item["source_type"] in AUTHORITATIVE_TYPES]) < 2:
        reasons.append("single_authoritative_evidence")
    if kind == "hacs" and not any(
        (urlsplit(item["url"]).hostname or "").lower() == "data-v2.hacs.xyz"
        for item in evidence
        if item["source_type"] == "hacs"
    ):
        reasons.append("hacs_registry_not_verified")
    if kind == "manual_custom_component":
        reasons.append("manual_installation")
    return ("review", reasons) if reasons else ("recommend", ["evidence_complete"])


def normalize_candidate(raw: Any, index: int, as_of: dt.datetime) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValidationError("invalid_candidate", "candidate must be an object")
    name = _text(raw.get("name"), "name", limit=160)
    raw_kind = _text(raw.get("kind"), "kind", limit=60).lower().replace(" ", "_")
    kind = KINDS.get(raw_kind)
    if kind is None:
        raise ValidationError("invalid_kind", f"unsupported candidate kind: {raw_kind}")
    source_url = _public_https_url(raw.get("source_url"), "source_url")
    evidence = _evidence(raw.get("evidence"))
    _validate_source_contract(kind, source_url, evidence)

    compatibility = _text(
        raw.get("compatibility", UNKNOWN), "compatibility", limit=30
    ).lower()
    if compatibility not in COMPATIBILITY:
        raise ValidationError("invalid_compatibility", f"unsupported compatibility: {compatibility}")

    requested_method = _text(
        raw.get("install_method", INSTALL_METHODS[kind]), "install_method", limit=40
    ).lower()
    expected_method = INSTALL_METHODS[kind]
    if requested_method != expected_method:
        raise ValidationError(
            "invalid_install_method",
            f"{kind} must use install_method {expected_method}",
        )

    maintainer = _text(raw.get("maintainer"), "maintainer", required=False, limit=160) or None
    latest_release = _text(
        raw.get("latest_release"), "latest_release", required=False, limit=120
    ) or None
    last_activity_at, last_activity = _timestamp(raw.get("last_activity_at"), "last_activity_at")
    maintenance_status = _maintenance_status(last_activity, as_of)
    compatibility_note = _text(
        raw.get("compatibility_note"),
        "compatibility_note",
        required=False,
        limit=500,
    ) or "No explicit compatibility evidence was recorded."
    risk_summary = _text(
        raw.get("risk_summary"), "risk_summary", required=False, limit=500
    ) or "Risk evidence is incomplete and requires manual review."

    recommendation, reasons = _recommendation(
        kind=kind,
        compatibility=compatibility,
        maintenance_status=maintenance_status,
        maintainer=maintainer,
        latest_release=latest_release,
        last_activity_at=last_activity_at,
        evidence=evidence,
    )
    return {
        "candidate_id": f"candidate-{index:03d}",
        "kind": kind,
        "name": name,
        "source_url": source_url,
        "maintainer": maintainer,
        "latest_release": latest_release,
        "last_activity_at": last_activity_at,
        "maintenance_status": maintenance_status,
        "compatibility": compatibility,
        "compatibility_note": compatibility_note,
        "required_permissions": _permissions(raw.get("required_permissions")),
        "install_method": expected_method,
        "risk_summary": risk_summary,
        "recommendation": recommendation,
        "recommendation_reasons": reasons,
        "evidence": evidence,
    }


def normalize_document(raw: Any, *, as_of: dt.datetime) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValidationError("invalid_document", "input must be a JSON object")
    query = _text(raw.get("query"), "query", limit=300)
    candidates = raw.get("candidates")
    if not isinstance(candidates, list):
        raise ValidationError("invalid_candidates", "candidates must be a list")
    if len(candidates) > MAX_CANDIDATES:
        raise ValidationError("too_many_candidates", "at most three candidates are allowed")

    normalized: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for source_index, candidate in enumerate(candidates, start=1):
        try:
            normalized.append(normalize_candidate(candidate, len(normalized) + 1, as_of))
        except ValidationError as exc:
            rejected.append({"input_index": source_index, "code": exc.code, "message": exc.message})

    if not normalized:
        status = "insufficient_evidence"
    elif any(item["recommendation"] == "recommend" for item in normalized):
        status = "ok"
    else:
        status = "review_required"
    return {
        "version": VERSION,
        "query": query,
        "as_of": as_of.isoformat().replace("+00:00", "Z"),
        "status": status,
        "candidates": normalized,
        "rejected_inputs": rejected,
        "disclaimer": "Research only. This output does not authorize or perform installation.",
    }


def _read_input(path: str | None) -> Any:
    try:
        content = (
            Path(path).read_bytes()
            if path
            else sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        )
    except OSError as exc:
        raise ValidationError("input_read_failed", str(exc)) from exc
    if len(content) > MAX_INPUT_BYTES:
        raise ValidationError(
            "input_too_large", f"input exceeds {MAX_INPUT_BYTES} bytes"
        )
    try:
        decoded = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationError("invalid_encoding", "input must be UTF-8") from exc
    try:
        return json.loads(decoded)
    except json.JSONDecodeError as exc:
        raise ValidationError("invalid_json", f"invalid JSON at line {exc.lineno} column {exc.colno}") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", help="UTF-8 JSON file; omit to read stdin")
    parser.add_argument("--as-of", help="ISO 8601 timestamp used for maintenance age")
    parser.add_argument("--pretty", action="store_true", help="pretty-print JSON output")
    args = parser.parse_args(argv)
    try:
        result = normalize_document(_read_input(args.input), as_of=_as_of(args.as_of))
    except ValidationError as exc:
        error = {"version": VERSION, "status": "error", "error": {"code": exc.code, "message": exc.message}}
        print(json.dumps(error, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2 if args.pretty else None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
