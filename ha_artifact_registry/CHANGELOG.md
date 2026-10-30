# Changes

## 0.0.1 — local candidate, not deployed

- Select fixed amd64/arm64 Debian stages with Docker TARGETARCH; remove Supervisor-incompatible digest-form build.yaml and ignore its BUILD_FROM fallback.
- Restrict build context and image COPY to the six reviewed runtime modules; add opt-in isolated dual-architecture packaging checks, source-hash comparison, disabled startup and missing-policy refusal.
- Local 2026-10-31 builds and packaging checks passed on both architectures using cached APT layers; APT versions remain unlocked, and no HA build or network permission acceptance is implied.
- Fixed signed canary publication grants, one-attempt immutable repository/version slots and restart observation.
- Complete OCI graph/raw/diff verification; Distribution manifest/blob GET and HEAD; independent authenticated HTTP full-graph readback.
- Separate read and publication bearers, bounded requests, no Docker/HA APIs, no discovery or overwrite endpoints.
- Host networking remains disabled pending separate deployment approval and real acceptance.
