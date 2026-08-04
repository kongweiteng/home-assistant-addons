"""Authenticated, fail-closed restart shadow service."""

from __future__ import annotations

from typing import Any

from .contract import ContractError, addon_baseline_etag, validate_shadow_request
from .supervisor import SupervisorClient, SupervisorError


class ShadowError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class ShadowManager:
    def __init__(
        self,
        *,
        supervisor: SupervisorClient,
        restart_addon_allowlist: frozenset[str],
    ) -> None:
        self.supervisor = supervisor
        self.restart_addon_allowlist = restart_addon_allowlist

    def restart_addon(self, request: Any) -> dict[str, Any]:
        try:
            validated = validate_shadow_request(request)
        except ContractError as exc:
            raise ShadowError(exc.code, str(exc)) from exc
        target = validated["target"]
        if target not in self.restart_addon_allowlist:
            raise ShadowError("target_not_allowlisted", "Add-on target is not allowlisted")
        try:
            observation = self.supervisor.addon_info(target)
        except SupervisorError as exc:
            raise ShadowError(exc.code, str(exc)) from exc
        if observation.get("slug") != target:
            raise ShadowError("target_mismatch", "Supervisor returned another Add-on target")
        if observation.get("installed") is False or observation.get("state") != "started":
            raise ShadowError("preflight_state_invalid", "Add-on is not in the restart canary state")
        try:
            actual_etag = addon_baseline_etag(observation)
        except ContractError as exc:
            raise ShadowError(exc.code, str(exc)) from exc
        if actual_etag != validated["baseline_etag"]:
            raise ShadowError("baseline_drift", "Add-on baseline changed before shadow validation")
        return {
            "version": 1,
            "mode": "shadow",
            "action_id": validated["action_id"],
            "proposal_hash": validated["proposal_hash"],
            "action_type": "restart_addon",
            "target": target,
            "adapter_version": "manager-restart-v1",
            "adapter_schema_version": 1,
            "baseline_etag": actual_etag,
            "execution_allowed": False,
            "observation": observation,
        }
