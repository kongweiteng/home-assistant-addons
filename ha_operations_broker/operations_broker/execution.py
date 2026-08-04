"""Fail-closed execution of the single allowlisted restart_addon operation."""

from __future__ import annotations

import threading
import secrets
from datetime import datetime
from typing import Any, Callable

from .authorization import AuthorizationError, AuthorizationStore, utc_now
from .contract import (
    ACTION_ID_RE,
    ContractError,
    DEFAULT_ADAPTER_SCHEMA_VERSION,
    DEFAULT_ADAPTER_VERSION,
    DEFAULT_POLICY_EPOCH,
    DEFAULT_POLICY_HASH,
    addon_baseline_etag,
    allowlist_fingerprint,
    parse_timestamp,
    validate_execution_request,
    validate_recovery_resolution,
)
from .manager_executor import ManagerExecutorClient, ManagerExecutorError
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
        policy_epoch: int = DEFAULT_POLICY_EPOCH,
        policy_hash: str = DEFAULT_POLICY_HASH,
        adapter_version: str = DEFAULT_ADAPTER_VERSION,
        adapter_schema_version: int = DEFAULT_ADAPTER_SCHEMA_VERSION,
        manager_shadow: ManagerExecutorClient | None = None,
        instance_id: str | None = None,
        lease_ttl_seconds: int = 30,
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
        self.policy_epoch = policy_epoch
        self.policy_hash = policy_hash
        self.allowlist_hash = allowlist_fingerprint(restart_addon_allowlist)
        self.adapter_version = adapter_version
        self.adapter_schema_version = adapter_schema_version
        self.manager_shadow = manager_shadow
        self.instance_id = instance_id or f"BROKER-{secrets.token_hex(16).upper()}"
        if not 5 <= lease_ttl_seconds <= 300:
            raise AuthorizationError("lease_ttl_invalid", "Lease TTL is invalid")
        self.lease_ttl_seconds = lease_ttl_seconds
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
            if proposal["version"] != 2:
                raise AuthorizationError(
                    "proposal_version_changed", "Executable proposal version is unsupported"
                )
            runtime_binding = {
                "policy_epoch": self.policy_epoch,
                "policy_hash": self.policy_hash,
                "allowlist_hash": self.allowlist_hash,
                "adapter_version": self.adapter_version,
                "adapter_schema_version": self.adapter_schema_version,
            }
            drift_codes = {
                "policy_epoch": "policy_changed",
                "policy_hash": "policy_changed",
                "allowlist_hash": "allowlist_changed",
                "adapter_version": "adapter_changed",
                "adapter_schema_version": "adapter_changed",
            }
            for field, expected in runtime_binding.items():
                if proposal[field] != expected:
                    raise AuthorizationError(
                        drift_codes[field], "Operation binding changed before execution"
                    )
            if proposal["target"] not in self.restart_addon_allowlist:
                raise AuthorizationError(
                    "target_not_allowlisted", "Add-on target is not allowlisted"
                )
            try:
                existing = self.store.get_execution(validated["action_id"])
            except AuthorizationError as exc:
                if exc.code != "execution_not_found":
                    raise
            else:
                if (
                    existing["receipt_id"] != validated["receipt_id"]
                    or existing["proposal_hash"] != validated["proposal_hash"]
                    or existing["idempotency_key"] != validated["idempotency_key"]
                ):
                    raise AuthorizationError(
                        "execution_conflict", "Action already has another execution claim"
                )
                return {**existing, "replayed": True}
            self.store.assert_execution_interlock_clear()
            self.store.assert_backup_evidence_valid(
                logical_id=proposal["backup_evidence_id"],
                scopes=("addon", "full"),
                baseline=proposal["baseline_etag"],
                now=self.clock(),
                valid_until=parse_timestamp(
                    proposal["expires_at"], field="proposal.expires_at"
                ),
            )
            target = proposal["target"]
            try:
                before = self.supervisor.addon_info(target)
            except SupervisorError as exc:
                raise AuthorizationError(exc.code, str(exc)) from exc
            if (
                before.get("slug") != target
                or before.get("state") != "started"
                or before.get("installed") is False
            ):
                raise AuthorizationError(
                    "preflight_state_invalid", "Add-on preflight state is invalid"
                )
            try:
                current_baseline = addon_baseline_etag(before)
            except ContractError as exc:
                raise AuthorizationError(exc.code, str(exc)) from exc
            if current_baseline != proposal["baseline_etag"]:
                raise AuthorizationError(
                    "baseline_changed", "Add-on baseline changed before execution"
                )
            if self.manager_shadow is not None:
                try:
                    shadow = self.manager_shadow.shadow_restart(proposal)
                except ManagerExecutorError as exc:
                    raise AuthorizationError(exc.code, str(exc)) from exc
                try:
                    shadow_baseline = addon_baseline_etag(shadow["observation"])
                except ContractError as exc:
                    raise AuthorizationError(exc.code, str(exc)) from exc
                if shadow_baseline != current_baseline:
                    raise AuthorizationError(
                        "manager_shadow_mismatch",
                        "Manager Executor observation differs from the Broker baseline",
                    )
            claimed, replayed = self.store.claim_execution(
                receipt_id=validated["receipt_id"],
                action_id=validated["action_id"],
                proposal_hash=validated["proposal_hash"],
                idempotency_key=validated["idempotency_key"],
                policy_epoch=self.policy_epoch,
                policy_hash=self.policy_hash,
                allowlist_hash=self.allowlist_hash,
                adapter_version=self.adapter_version,
                adapter_schema_version=self.adapter_schema_version,
                baseline_etag=current_baseline,
                backup_evidence_id=proposal["backup_evidence_id"],
                instance_id=self.instance_id,
                lease_ttl_seconds=self.lease_ttl_seconds,
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
                revalidated = self.supervisor.addon_info(target)
            except SupervisorError as exc:
                return self._fail_before_write(claimed["action_id"], exc.code)
            try:
                revalidated_baseline = addon_baseline_etag(revalidated)
            except ContractError as exc:
                return self._fail_before_write(claimed["action_id"], exc.code)
            if (
                revalidated.get("slug") != target
                or revalidated.get("state") != "started"
                or revalidated.get("installed") is False
                or revalidated_baseline != claimed["baseline_etag"]
            ):
                return self._fail_before_write(
                    claimed["action_id"],
                    "preflight_revalidation_changed",
                    preflight=revalidated,
                )
            executing = self.store.update_execution(
                action_id=claimed["action_id"],
                expected_states=frozenset({"authorized"}),
                state="executing",
                updated_at=self.clock(),
                preflight=revalidated,
            )
            self.store.heartbeat_execution_leases(
                action_id=executing["action_id"],
                instance_id=self.instance_id,
                heartbeat_at=self.clock(),
                lease_ttl_seconds=self.lease_ttl_seconds,
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
            self.store.heartbeat_execution_leases(
                action_id=verifying["action_id"],
                instance_id=self.instance_id,
                heartbeat_at=self.clock(),
                lease_ttl_seconds=self.lease_ttl_seconds,
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
