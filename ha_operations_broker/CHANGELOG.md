# Changelog

## [0.2.0] - 2026-07-31

### Added

- Add a Home Assistant admin-only Ingress page for exact proposal review and Passkey confirmation.
- Add private SQLite persistence for hashed HA operator identities, Passkey verification material, immutable authorization requests, counters, and `passkey_verified` receipts.
- Add bearer-authenticated authorization-request creation and status APIs with action/hash idempotency.
- Add exact HTTPS RP/origin validation, one-time in-memory WebAuthn challenges, user verification, replay checks, and signature-counter rollback protection.
- Pin Yubico `fido2 2.2.1` to a verified wheel SHA-256 and use Debian's packaged cryptographic backend.

### Security

- Keep `hassio_role: default`; do not add Home Assistant Core, manager, backup, admin, host, privileged, Docker, file-map, or host-port access.
- Require both an authenticated HA admin Ingress session and a private enrollment token for initial Passkey registration.
- Never persist raw HA/Weixin identities, enrollment tokens, private keys, biometrics, or WebAuthn challenge state.
- Keep hashed HA operator and credential identifiers out of the Ingress context; expose them only through the authenticated internal receipt API.
- Passkey success still returns `execution_allowed: false`; no execution endpoint exists.

## [0.1.0] - 2026-07-31

### Added

- Add an independent experimental Home Assistant app for P5 read-only operation preflights.
- Validate immutable P4 proposal hashes, code-owned risk, backup requirements, owner hashes, approval state, and TTL.
- Observe only Supervisor, Core, or exact add-on information through a fixed GET allowlist.
- Add an internal bearer-authenticated, size-bounded JSON API and minimal health endpoint.

### Security

- Use only `hassio_api: true` with `hassio_role: default`; do not request Home Assistant Core, manager, backup, admin, host, privileged, Docker, file map, Ingress, or host-port access.
- Always return `execution_allowed: false`; approval assurance is explicitly `structural_only` until an independently verifiable production authorization root exists.
- Never return Supervisor options, logs, credentials, raw request parameters, or response bodies.
