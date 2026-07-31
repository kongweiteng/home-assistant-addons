# Changelog

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
