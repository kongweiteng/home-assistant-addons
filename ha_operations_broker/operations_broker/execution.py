"""Fail-closed execution of the single allowlisted restart_addon operation."""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Any, Callable

from .authorization import AuthorizationError, AuthorizationStore, utc_now
from .contract import (
    ACTION_ID_RE,
    ContractError,
    validate_execution_request,
    validate_recovery_resolution,
)
from .supervisor import SupervisorClient, SupervisorError


class ExecutionManager:
    """Consumes one Passkey receipt and executes at most one exact add-on restart."""

    def __init__(
        self,
        *,
        store: AuthorizationStore,
        supervisor: SupervisorClient,
        execution_enabled: bool,
        enabled_actions: frozenset[str],
        restart_addon_allowlist: frozenset[str],
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if enabled_actions - {"restart_addon"}:
            raise AuthorizationError(
                "enabled_actions_invalid", "Only restart_addon can be enabled"
            )
        self.store = store
        self.supervisor = supervisor
        self.execution_enabled = bool(execution_enabled)
        self.enabled_actions = enabled_actions
        self.restart_addon_allowlist = restart_addon_allowlist
        self.clock = clock
        self._lock = threading.Lock()
        self.recovered_executions = self.store.recover_incomplete_executions(
            recovered_at=self.clock()
        )

    def execute(self, request: Any) -> dict[str, Any]:
        try:
            validated = validate_execution_request(request)
        except ContractError as exc:
            raise AuthorizationError(exc.code, str(exc)) from exc
        if not self.execution_enabled:
            raise AuthorizationError("execution_disabled", "Operation execution is disabled")
        if "restart_addon" not in self.enabled_actions:
            raise AuthorizationError("action_disabled", "restart_addon is not enabled")
        if not self._lock.acquire(blocking=False):
            raise AuthorizationError("execution_busy", "Another operation is executing")
        try:
            proposal = self.store.get_native_proposal(
                validated["action_id"], now=self.clock()
            )
            if proposal["state"] == "expired":
                raise AuthorizationError("proposal_expired", "Operation proposal expired")
            if (
                proposal["action_type"] != "restart_addon"
                or proposal["proposal_hash"] != validated["proposal_hash"]
                or proposal["idempotency_key"] != validated["idempotency_key"]
            ):
                raise AuthorizationError(
                    "proposal_mismatch", "Execution request does not match the proposal"
                )
            if proposal["target"] not in self.restart_addon_allowlist:
                raise AuthorizationError(
                    "target_not_allowlisted", "Add-on target is not allowlisted"
                )
            claimed, replayed = self.store.claim_execution(
                receipt_id=validated["receipt_id"],
                action_id=validated["action_id"],
                proposal_hash=validated["proposal_hash"],
                idempotency_key=validated["idempotency_key"],
                claimed_at=self.clock(),
            )
            if replayed:
                return {**claimed, "replayed": True}
            target = claimed["target"]
            if claimed["action_type"] != "restart_addon":
                return self._fail_before_write(
                    claimed["action_id"], "unsupported_action"
                )
            try:
                before = self.supervisor.addon_info(target)
            except SupervisorError as exc:
                return self._fail_before_write(claimed["action_id"], exc.code)
            if (
                before.get("slug") != target
                or before.get("state") != "started"
                or before.get("installed") is False
            ):
                return self._fail_before_write(
                    claimed["action_id"], "preflight_state_invalid", preflight=before
                )
            executing = self.store.update_execution(
                action_id=claimed["action_id"],
                expected_states=frozenset({"authorized"}),
                state="executing",
                updated_at=self.clock(),
                preflight=before,
            )
            try:
                self.supervisor.restart_addon(target)
            except SupervisorError as exc:
                return self.store.update_execution(
                    action_id=executing["action_id"],
                    expected_states=frozenset({"executing"}),
                    state="recovery_required",
                    updated_at=self.clock(),
                    error_code=exc.code,
                    finished=True,
                )
            verifying = self.store.update_execution(
                action_id=executing["action_id"],
                expected_states=frozenset({"executing"}),
                state="verifying",
                updated_at=self.clock(),
            )
            try:
                after = self.supervisor.addon_info(target)
            except SupervisorError as exc:
                return self.store.update_execution(
                    action_id=verifying["action_id"],
                    expected_states=frozenset({"verifying"}),
                    state="recovery_required",
                    updated_at=self.clock(),
                    error_code=exc.code,
                    finished=True,
                )
            verification_ok = (
                after.get("slug") == target
                and after.get("state") == "started"
                and after.get("version") == before.get("version")
            )
            return self.store.update_execution(
                action_id=verifying["action_id"],
                expected_states=frozenset({"verifying"}),
                state="succeeded" if verification_ok else "recovery_required",
                updated_at=self.clock(),
                postflight=after,
                error_code=None if verification_ok else "postflight_mismatch",
                finished=True,
            )
        finally:
            self._lock.release()

    def status(self, action_id: str) -> dict[str, Any]:
        return self.store.get_execution(action_id)

    def resolve_recovery(self, action_id: str, request: Any) -> dict[str, Any]:
        if not isinstance(action_id, str) or not ACTION_ID_RE.fullmatch(action_id):
            raise AuthorizationError("invalid_action_id", "Action ID is invalid")
        try:
            validated = validate_recovery_resolution(request)
        except ContractError as exc:
            raise AuthorizationError(exc.code, str(exc)) from exc
        return self.store.resolve_recovery(
            action_id=action_id,
            resolution=validated["resolution"],
            evidence_hash=validated["evidence_hash"],
            resolved_at=self.clock(),
        )

    def _fail_before_write(
        self,
        action_id: str,
        error_code: str,
        *,
        preflight: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.store.update_execution(
            action_id=action_id,
            expected_states=frozenset({"authorized"}),
            state="failed",
            updated_at=self.clock(),
            preflight=preflight,
            error_code=error_code,
            finished=True,
        )
