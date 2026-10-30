# HA Immutable Artifact Registry Candidate

This uninstalled candidate provides a complete fixed OCI Distribution read API and a separately authenticated publication API. It uses no Docker socket or Supervisor token. Its public metadata disables host networking; enabling the proposed HA loopback deployment requires separate approval and actual isolated pull/upgrade/rollback acceptance.

Read [DESIGN.md](DESIGN.md) for scope, source evidence, permissions and rollback; see [DOCS.md](DOCS.md) for private startup configuration and API contracts. Offline tests prove graph/persistence/authentication/HTTP behavior only. They do not qualify a real Supervisor, Docker engine, encrypted off-device receipt or service runtime.
