"""Runtime entrypoint for the authorization-gated HA Operations Broker."""

from __future__ import annotations

import os

from .api import create_server
from .authorization import AuthorizationManager, AuthorizationStore
from .contract import parse_owner_hashes, utc_now
from .execution import ExecutionManager
from .passkeys import Fido2PasskeyBackend, validate_webauthn_configuration
from .service import preflight
from .supervisor import SupervisorClient


def main() -> None:
    api_token = os.environ["BROKER_API_TOKEN"]
    owner_hashes = parse_owner_hashes(os.environ["BROKER_OWNER_HASHES"])
    max_request_bytes = int(os.environ.get("BROKER_MAX_REQUEST_BYTES", "32768"))
    timeout_seconds = int(os.environ.get("BROKER_SUPERVISOR_TIMEOUT_SECONDS", "5"))
    rp_id = os.environ["BROKER_WEBAUTHN_RP_ID"]
    configured_origins = tuple(
        item.strip()
        for item in os.environ["BROKER_WEBAUTHN_ALLOWED_ORIGINS"].split(",")
        if item.strip()
    )
    _, allowed_origins = validate_webauthn_configuration(rp_id, configured_origins)
    enrollment_token = os.environ.pop("BROKER_PASSKEY_ENROLLMENT_TOKEN", "")
    challenge_ttl_seconds = int(
        os.environ.get("BROKER_PASSKEY_CHALLENGE_TTL_SECONDS", "180")
    )
    max_passkeys = int(os.environ.get("BROKER_MAX_PASSKEYS", "8"))
    max_pending_flows = int(os.environ.get("BROKER_MAX_PENDING_FLOWS", "100"))
    execution_enabled = os.environ.get("BROKER_EXECUTION_ENABLED", "false") == "true"
    enabled_actions = frozenset(
        item.strip()
        for item in os.environ.get("BROKER_ENABLED_ACTIONS", "").split(",")
        if item.strip()
    )
    restart_addon_allowlist = frozenset(
        item.strip()
        for item in os.environ.get("BROKER_RESTART_ADDON_ALLOWLIST", "").split(",")
        if item.strip()
    )
    proposal_ttl_seconds = int(os.environ.get("BROKER_PROPOSAL_TTL_SECONDS", "600"))
    store = AuthorizationStore(
        os.environ.get(
            "BROKER_AUTHORIZATION_DATABASE",
            "/data/authorization/passkeys.sqlite3",
        )
    )
    passkeys = Fido2PasskeyBackend(
        rp_id=rp_id,
        allowed_origins=tuple(allowed_origins),
    )
    authorization = AuthorizationManager(
        store=store,
        passkeys=passkeys,
        trusted_owner_hashes=owner_hashes,
        enrollment_token=enrollment_token,
        challenge_ttl_seconds=challenge_ttl_seconds,
        max_passkeys=max_passkeys,
        max_pending_flows=max_pending_flows,
        restart_addon_allowlist=restart_addon_allowlist,
        proposal_ttl_seconds=proposal_ttl_seconds,
        clock=utc_now,
    )
    supervisor = SupervisorClient(
        os.environ["BROKER_SUPERVISOR_TOKEN"],
        base_url=os.environ.get("BROKER_SUPERVISOR_BASE_URL", "http://supervisor"),
        timeout_seconds=timeout_seconds,
    )
    execution = ExecutionManager(
        store=store,
        supervisor=supervisor,
        execution_enabled=execution_enabled,
        enabled_actions=enabled_actions,
        restart_addon_allowlist=restart_addon_allowlist,
        clock=utc_now,
    )

    def handler(payload):
        return preflight(
            payload,
            trusted_owner_hashes=owner_hashes,
            supervisor=supervisor,
            clock=utc_now,
        )

    server = create_server(
        "0.0.0.0",
        8098,
        api_token=api_token,
        max_request_bytes=max_request_bytes,
        preflight_handler=handler,
        authorization_manager=authorization,
        execution_handler=execution.execute,
        execution_status_handler=execution.status,
        recovery_resolution_handler=execution.resolve_recovery,
        execution_enabled=execution_enabled,
        enabled_actions=enabled_actions,
        allowed_ingress_origins=allowed_origins,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
