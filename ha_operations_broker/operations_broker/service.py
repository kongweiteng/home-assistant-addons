"""Read-only action preflight routing with no execution path."""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Any, Callable

from .contract import ContractError, validate_envelope
from .supervisor import SupervisorClient, SupervisorError


ADDON_ACTIONS = frozenset(
    {
        "install_addon",
        "configure_addon",
        "start_addon",
        "stop_addon",
        "restart_addon",
        "update_addon",
        "repair_addon",
        "uninstall_addon",
    }
)
CORE_ACTIONS = frozenset({"check_ha_config", "restart_core"})
BACKUP_ACTIONS = frozenset({"create_backup", "delete_expired_backup"})
UNSUPPORTED_ACTIONS = frozenset(
    {
        "install_hacs",
        "update_hacs",
        "repair_hacs",
        "remove_hacs",
        "start_config_flow",
        "enable_integration",
        "disable_integration",
        "reload_integration",
        "remove_integration",
        "purge_recorder",
        "cleanup_allowlisted_cache",
    }
)


def iso_now(clock: Callable[[], datetime]) -> str:
    return clock().astimezone(timezone.utc).replace(microsecond=0).isoformat()


def preflight(
    envelope: Any,
    *,
    trusted_owner_hashes: frozenset[str],
    supervisor: SupervisorClient,
    clock: Callable[[], datetime],
) -> dict[str, Any]:
    request_id = f"PREFLIGHT-{secrets.token_hex(8).upper()}"
    sampled_at = iso_now(clock)
    try:
        validated = validate_envelope(
            envelope, trusted_owner_hashes=trusted_owner_hashes, clock=clock
        )
        action_type = validated["action_type"]
        target = validated["target"]
        if action_type in ADDON_ACTIONS:
            observation = {
                "kind": "addon_info",
                "data": supervisor.addon_info(target),
            }
            decision = "preflight_observed"
            issues: list[dict[str, str]] = []
            future_role = "manager"
        elif action_type in CORE_ACTIONS:
            if target != "home-assistant-core":
                raise ContractError(
                    "target_mismatch", "Core actions must target home-assistant-core"
                )
            observation = {"kind": "core_info", "data": supervisor.core_info()}
            decision = "preflight_observed"
            issues = []
            future_role = "homeassistant"
        elif action_type in BACKUP_ACTIONS:
            observation = None
            decision = "blocked"
            issues = [
                {
                    "code": "permission_not_granted",
                    "message": "Backup APIs are outside the P5 default-role canary.",
                }
            ]
            future_role = "backup"
        elif action_type in UNSUPPORTED_ACTIONS:
            observation = None
            decision = "blocked"
            issues = [
                {
                    "code": "unsupported_canary",
                    "message": "This action has no P5 read-only Supervisor preflight.",
                }
            ]
            future_role = "separate_design_required"
        else:
            raise ContractError("unsupported_action", "Action is not routed by the canary")
        return {
            "version": 1,
            "request_id": request_id,
            "sampled_at": sampled_at,
            "action_id": validated["action_id"],
            "proposal_hash": validated["proposal_hash"],
            "decision": decision,
            "authorization_assurance": "structural_only",
            "execution_allowed": False,
            "observation": observation,
            "issues": issues,
            "future_required_role": future_role,
        }
    except ContractError as exc:
        return _blocked(request_id, sampled_at, exc.code, str(exc))
    except SupervisorError as exc:
        return _blocked(request_id, sampled_at, exc.code, str(exc))
    except Exception:
        return _blocked(
            request_id,
            sampled_at,
            "internal_error",
            "Preflight failed without executing any operation.",
        )


def _blocked(request_id: str, sampled_at: str, code: str, message: str) -> dict[str, Any]:
    return {
        "version": 1,
        "request_id": request_id,
        "sampled_at": sampled_at,
        "decision": "blocked",
        "authorization_assurance": "none",
        "execution_allowed": False,
        "observation": None,
        "issues": [{"code": code, "message": message}],
        "future_required_role": None,
    }
