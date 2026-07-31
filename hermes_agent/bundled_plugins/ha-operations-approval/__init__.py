"""Hermes plugin entry point for model-free HA operation approvals."""

from __future__ import annotations

import hmac
import json
import os
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from .ledger import (
    ACTION_RISKS,
    ApprovalLedger,
    ProposalError,
    configured_owner_hashes,
    identity_hash,
)


@dataclass(frozen=True)
class ApprovalActor:
    platform: str
    chat_type: str
    identity_hash: str
    authorized: bool


_CURRENT_ACTOR: ContextVar[ApprovalActor | None] = ContextVar(
    "ha_operations_approval_actor", default=None
)


def _enabled() -> bool:
    return os.getenv("HA_OPERATIONS_APPROVAL_ENABLED", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _platform_name(value: Any) -> str:
    return str(getattr(value, "value", value) or "").strip().lower()


def _capture_gateway_actor(event: Any = None, **_kwargs: Any) -> None:
    _CURRENT_ACTOR.set(None)
    if event is None or getattr(event, "internal", False):
        return None
    source = getattr(event, "source", None)
    platform = _platform_name(getattr(source, "platform", ""))
    chat_type = str(getattr(source, "chat_type", "") or "").strip().lower()
    user_id = str(getattr(source, "user_id", "") or "").strip()
    if not platform or not user_id:
        return None
    actor_hash = identity_hash(platform, user_id)
    owners = configured_owner_hashes()
    authorized = (
        platform == "weixin"
        and chat_type == "dm"
        and any(hmac.compare_digest(actor_hash, owner) for owner in owners)
    )
    _CURRENT_ACTOR.set(
        ApprovalActor(
            platform=platform,
            chat_type=chat_type,
            identity_hash=actor_hash,
            authorized=authorized,
        )
    )
    return None


def _owner_actor() -> ApprovalActor:
    actor = _CURRENT_ACTOR.get()
    if actor is None or not actor.authorized:
        raise ProposalError(
            "not_authorized",
            "Approval commands require the configured owner in a Weixin private chat",
        )
    return actor


def _ledger() -> ApprovalLedger:
    return ApprovalLedger.from_env()


def _tool_result(callable_result: Any) -> str:
    try:
        return json.dumps({"result": callable_result()}, ensure_ascii=False, sort_keys=True)
    except ProposalError as exc:
        return json.dumps(
            {"error": {"code": exc.code, "message": str(exc)}},
            ensure_ascii=False,
            sort_keys=True,
        )
    except Exception:
        return json.dumps(
            {"error": {"code": "internal_error", "message": "Approval ledger operation failed"}},
            sort_keys=True,
        )


def _create_proposal(args: dict[str, Any], **_kwargs: Any) -> str:
    return _tool_result(lambda: _ledger().create(args))


def _get_proposal_status(args: dict[str, Any], **_kwargs: Any) -> str:
    return _tool_result(lambda: _ledger().get(str(args.get("action_id", "")).strip()))


def _command_error(exc: ProposalError) -> str:
    if exc.code == "not_authorized":
        return "Approval command unavailable or not authorized."
    return f"Operation approval rejected ({exc.code})."


def _approve_command(raw_args: str) -> str:
    try:
        actor = _owner_actor()
        parts = raw_args.strip().split()
        if len(parts) != 1:
            raise ProposalError("invalid_command", "Usage: /ha-approve <action_id>")
        result = _ledger().approve(parts[0].upper(), actor.identity_hash)
        if result["state"] == "awaiting_confirmation":
            challenge = result.get("confirmation_challenge")
            if challenge:
                return (
                    f"L3 confirmation required for {result['action_id']} "
                    f"({result['proposal_hash']}). Send /ha-confirm "
                    f"{result['action_id']} {challenge} before {result['expires_at']}."
                )
            return f"L3 confirmation is still required for {result['action_id']}."
        return f"Operation {result['action_id']} state: {result['state']} ({result['proposal_hash']})."
    except ProposalError as exc:
        return _command_error(exc)


def _confirm_command(raw_args: str) -> str:
    try:
        actor = _owner_actor()
        parts = raw_args.strip().split()
        if len(parts) != 2:
            raise ProposalError(
                "invalid_command", "Usage: /ha-confirm <action_id> <challenge>"
            )
        result = _ledger().confirm(
            parts[0].upper(), actor.identity_hash, parts[1].upper()
        )
        return f"Operation {result['action_id']} state: {result['state']} ({result['proposal_hash']})."
    except ProposalError as exc:
        return _command_error(exc)


def _cancel_command(raw_args: str) -> str:
    try:
        actor = _owner_actor()
        parts = raw_args.strip().split()
        if len(parts) != 1:
            raise ProposalError("invalid_command", "Usage: /ha-cancel <action_id>")
        result = _ledger().cancel(parts[0].upper(), actor.identity_hash)
        return f"Operation {result['action_id']} state: {result['state']}."
    except ProposalError as exc:
        return _command_error(exc)


CREATE_PROPOSAL_SCHEMA = {
    "name": "ha_create_operation_proposal",
    "description": (
        "Create an immutable, non-executing Home Assistant operation proposal. "
        "This never changes Home Assistant and never constitutes approval."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action_type": {"type": "string", "enum": sorted(ACTION_RISKS)},
            "target": {"type": "string", "description": "Bounded logical target identifier."},
            "parameter_summary": {
                "type": "string",
                "description": "JSON object containing only non-secret review parameters.",
            },
            "requires_backup": {"type": "boolean"},
            "expected_change": {"type": "string"},
            "validation_plan": {
                "type": "string",
                "description": "JSON array with 1-10 deterministic validation steps.",
            },
            "rollback_plan": {
                "type": "string",
                "description": "JSON array with 1-10 rollback steps.",
            },
        },
        "required": [
            "action_type",
            "target",
            "parameter_summary",
            "requires_backup",
            "expected_change",
            "validation_plan",
            "rollback_plan",
        ],
    },
}

GET_STATUS_SCHEMA = {
    "name": "ha_get_operation_proposal_status",
    "description": "Read the safe status of one operation proposal without executing it.",
    "parameters": {
        "type": "object",
        "properties": {"action_id": {"type": "string"}},
        "required": ["action_id"],
    },
}


def register(ctx: Any) -> None:
    if not _enabled():
        return
    if not configured_owner_hashes():
        raise RuntimeError("HA operations approval is enabled without an owner identity hash")
    ctx.register_hook("pre_gateway_dispatch", _capture_gateway_actor)
    ctx.register_command(
        "ha-approve",
        _approve_command,
        description="Approve one immutable HA operation proposal.",
        args_hint="<action_id>",
    )
    ctx.register_command(
        "ha-confirm",
        _confirm_command,
        description="Complete the second confirmation for one L3 proposal.",
        args_hint="<action_id> <challenge>",
    )
    ctx.register_command(
        "ha-cancel",
        _cancel_command,
        description="Cancel one pending HA operation proposal.",
        args_hint="<action_id>",
    )
    ctx.register_tool(
        name="ha_create_operation_proposal",
        toolset="homeassistant",
        schema=CREATE_PROPOSAL_SCHEMA,
        handler=_create_proposal,
        description="Create an immutable non-executing HA operation proposal.",
        emoji="🧾",
    )
    ctx.register_tool(
        name="ha_get_operation_proposal_status",
        toolset="homeassistant",
        schema=GET_STATUS_SCHEMA,
        handler=_get_proposal_status,
        description="Read a safe HA operation proposal status.",
        emoji="🧾",
    )
