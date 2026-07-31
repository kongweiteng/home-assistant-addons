# HA Operations Broker documentation

## Status

Version `0.1.0` is a source-only P5 canary. Do not install it as a production
operations executor. It has no execution endpoint and always returns
`execution_allowed: false`.

## Configuration

- `broker_api_token`: random value of at least 32 characters, stored only in
  Home Assistant private app options.
- `trusted_owner_identity_hashes`: lowercase SHA-256 hashes of exact
  `weixin:user_id` identities. Do not store raw Weixin IDs here.
- `max_request_bytes`: bounded JSON body size, default 32768.
- `supervisor_timeout_seconds`: read-only Supervisor information timeout.

The app is manual and experimental. It does not map port `8098` to the host.

## API

`GET /healthz` returns only the service version and confirms execution is
disabled.

`POST /v1/preflight` requires `Authorization: Bearer <broker_api_token>` and an
`application/json` P4 envelope:

```json
{
  "version": 1,
  "proposal": {
    "version": 1,
    "action_id": "OPS-20260731-A1B2C3D4E5F6",
    "action_type": "restart_addon",
    "target": "example_addon",
    "parameter_summary": {},
    "risk_level": "L3",
    "requires_backup": true,
    "expected_change": "Restart the exact add-on after prechecks.",
    "validation_plan": ["Read current state", "Verify health after execution"],
    "rollback_plan": ["Stop before execution", "Keep current version"],
    "created_at": "2026-07-31T12:00:00+00:00",
    "expires_at": "2026-07-31T12:10:00+00:00",
    "state": "awaiting_approval",
    "parameter_summary_hash": "sha256:<64 lowercase hex>",
    "proposal_hash": "sha256:<64 lowercase hex>"
  },
  "approval": {
    "version": 1,
    "action_id": "OPS-20260731-A1B2C3D4E5F6",
    "proposal_hash": "sha256:<same proposal hash>",
    "state": "approved",
    "approved_by_hash": "<configured 64 lowercase hex owner hash>",
    "approved_at": "2026-07-31T12:01:00+00:00",
    "expires_at": "2026-07-31T12:10:00+00:00"
  }
}
```

The response contains a redacted observation or a blocking issue. It never
returns Supervisor options, logs, tokens, request parameters, or a production
execution authorization.

## Current limitation

Hermes/Weixin events do not currently carry an independently verifiable
cross-process signature. A matching owner hash is therefore structural evidence
only. A future production broker must add an authorization root that the Hermes
process cannot forge before any manager or backup role and before any write API.

## Rollback

Because `0.1.0` is not a production executor, source rollback means keeping the
app uninstalled or stopped. If a future canary installation is authorized,
stop/uninstall it and remove its private token configuration.
