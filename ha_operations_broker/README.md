# HA Operations Broker

HA Operations Broker is an experimental P5 canary for the Hermes-driven Home
Assistant operations design. It runs as a separate Home Assistant app and
performs only deterministic, read-only Supervisor information preflights.

It does not install, configure, start, stop, restart, update, uninstall, back
up, clean, or otherwise change Home Assistant. Every response contains
`execution_allowed: false`.

## Security boundary

- `hassio_api: true` with `hassio_role: default` only.
- No Home Assistant Core API permission, manager/backup/admin role, host network,
  privileged mode, Docker socket, file-system maps, Ingress, or host port.
- Fixed Supervisor GET allowlist: supervisor info, Core info, and exact add-on
  info.
- Authenticated internal preflight API with bounded JSON requests.
- Proposal hash, risk, backup requirement, owner hash, approval state, and TTL
  are revalidated.
- Approval assurance remains `structural_only`; the canary is not a production
  authorization root.

See [DOCS.md](DOCS.md) for the envelope and response contracts.
