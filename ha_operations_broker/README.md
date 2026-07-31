# HA Operations Broker

HA Operations Broker is an experimental P6-A canary for the Hermes-driven Home
Assistant operations design. It runs as a separate Home Assistant app and
provides deterministic read-only Supervisor preflights plus a Broker-owned
Passkey authorization root.

It does not install, configure, start, stop, restart, update, uninstall, back
up, clean, or otherwise change Home Assistant. Every response contains
`execution_allowed: false`.

## Security boundary

- `hassio_api: true` with `hassio_role: default` only.
- Home Assistant Ingress on container port `8098`, restricted to HA admins;
  no host port, Core API, manager/backup/admin role, host network, privileged
  mode, Docker socket, or file-system maps.
- Fixed Supervisor GET allowlist: supervisor info, Core info, and exact add-on
  info.
- Authenticated internal bearer API with bounded JSON requests.
- Proposal hash, risk, backup requirement, structural owner hash, approval
  state, and TTL are revalidated before an authorization request exists.
- Initial Passkey enrollment requires both an authenticated HA admin Ingress
  session and a private enrollment token. Raw HA user IDs are hashed before
  persistence.
- WebAuthn verification uses exact HTTPS origins, user verification, one-time
  in-memory challenges, and the pinned Yubico `fido2` library.
- Passkey verification produces `passkey_verified` receipts but never enables
  execution.
- The Ingress context exposes only a minimal receipt summary; hashed operator
  and credential identifiers remain restricted to the internal bearer API.

See [DOCS.md](DOCS.md) for configuration, enrollment, API, persistence, and
rollback details.
