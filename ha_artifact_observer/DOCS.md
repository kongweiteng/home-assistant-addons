# Configuration and acceptance

## Build compatibility

Use BuildKit with the intended Docker platform (`linux/amd64` or `linux/arm64`).
The Dockerfile contains two independently digest-pinned Debian base stages and
selects `base-${TARGETARCH}`. Home Assistant's `aarch64` metadata corresponds to
Docker's automatic `arm64` architecture. Unsupported architecture names have no
base stage and fail the build. No caller-supplied base image or runtime module
can replace these stages, and Supervisor's `BUILD_FROM` argument is unused.

There is deliberately no `build.yaml`. Supervisor `2026.10.5` rejected the
`0.0.4` digest-form `build_from` entries, fell back to its default Alpine image,
and failed the Debian `apt-get` step. The real installation therefore ended in
failure with no installed version or running observer. Earlier successful
local builds did not establish compatibility with Supervisor's build path.
Version `0.0.5` moves digest pins into the Dockerfile; record new dual-platform
image checks and the real Supervisor install/start/observation results before
claiming this correction is deployed.

## Instance authorization

The published metadata has `docker_api:false`, `hassio_api:false`, manual boot,
no host network or privileged permission, and no host-port mapping. Enabling
the software flags does not attach a socket. Before any instance is enabled,
approve the exact host, target set, independent caller identities, socket
attachment, internal network visibility and rollback. Do not grant the socket
to Controller, Delivery, Manager, Backup or untrusted task programs.

Docker's `:ro` socket mount does not restrict API methods. Fixed GET behavior is
an application constraint; process compromise can still affect the whole host.
Keep auto-update disabled and change this trusted boundary deliberately.

## Fixed administrator options

Main reads only `/data/options.json`, at most 128 KiB, as a regular file; symlink,
FIFO, directory, duplicate JSON keys, unknown keys and non-finite numbers are
rejected. Bad configuration stops startup with a stable error code. Options,
bearers, raw engine responses and exception text are never logged.

| Field | Default and accepted values |
| --- | --- |
| `observation_enabled` | `false`; strict Boolean |
| `docker_socket_enabled` | `false`; separate strict Boolean |
| `docker_api_version` | `v1.44`; fixed v1/two-digit minor, no discovery or downgrade |
| `timeout_seconds` | `3`; finite 0.05–30-second Docker total budget |
| `max_response_bytes` | `1048576`; integer 4096–4194304 |
| `bindings` | `[]`; at most 64 closed target registrations |
| `principals` | `[]`; at most 16 independent caller registrations |

Each binding has exactly `target_ref`, `instance_ref`, `slug`, `container_name`.
Only the verified exact `app_{slug}` or legacy `addon_{slug}` name is accepted;
the observer never tries alternative names. One observer serves one instance.
Duplicate target, instance/container or instance/slug registrations are rejected.

Each principal has exactly `principal_ref`, `token`, `target_refs`. Tokens must
be independent of Supervisor and other service credentials, mutually distinct,
and injected through the approved machine-secret path. Each target list must
be nonempty, unique and entirely registered. Do not paste live credentials into
repository examples, command arguments, logs or chat.

Both flags and valid nonempty bindings/principals are required for observation.
Disabled mode never constructs or accesses the Docker inspector. Enabled mode
uses only AF_UNIX `/var/run/docker.sock`; options cannot select a path or URL,
and `DOCKER_HOST`/proxy environment variables are ignored. A missing or unusable
socket fails the observation, without substituting expected image data.

## HTTP interface

Main binds internal `0.0.0.0:8099`; the metadata maps no host port.

- `GET /healthz` returns `status`, protocol `version` and `observation_enabled`.
  The enablement flag does not prove the socket works or targets passed acceptance.
- `GET /v1/artifacts/{target_ref}` requires one independent Authorization bearer
  and an exact target belonging to that principal. Disabled returns 404; bad
  identity 401; unauthorized target 403; unknown, unstable or unavailable facts 503.
- Query strings, encoded paths, extra path components, arbitrary Docker paths,
  GET bodies, duplicate Content-Length, any Transfer-Encoding/Content-Encoding
  (including empty values), and mutating methods are rejected.

GET accepts absent Content-Length or one exact `0`. Request-line/header reads
have a five-second absolute deadline, including clients continuously sending
bytes. At most four connection handlers run. This prevents slow-header clients
from retaining all slots indefinitely; it is not a network access-control layer.

Successful artifact responses contain exactly:

```text
version, target_ref, instance_ref, slug, container_id, running,
docker_image_id:{kind:docker_image_id,digest:sha256:64hex},
oci_repo_digests:[{kind:oci_repo_digest,repository,digest:sha256:64hex}],
observed_at
```

`observed_at` uses `Asia/Shanghai` with `+08:00`. The container ID is complete,
`running` is true, and RepoDigests may be empty for a local build. Docker
ImageID is kept separate from OCI repository digest. Engine store semantics can
associate its ImageID with config or target descriptors; the observer preserves
the engine's actual typed identity and does not infer an OCI digest from it.
No Config, Env, Mounts, labels, logs, original JSON or private exception appears
in responses.

Docker reads are exactly container inspect → actual container ImageID's image
inspect → same container inspect. Container ID, ImageID and StartedAt must be
stable, the container must be running and not restarting/dead, and the image
inspect ID must match. Status/header/body reads share the configured Docker
budget. At most two observations run. Redirects, malformed/duplicate JSON,
oversize responses, truncation and identity drift fail closed.

The exact `POST /internal/v1/work-preview/qualification` remains 404 in the
standard image. A trusted Python composition may inject the optional bounded
hook: it accepts one Authorization/Content-Length/application-json frame,
4096-byte input and a two-second absolute budget, and returns only bounded
bytes plus a detached MAC. No published startup path installs such a hook or
imports preview code. Tests of an injected synthetic hook do not claim preview
deployment or identity qualification.

## Verify, restart and revoke

Before first enablement, verify instance and engine API version, exact live
container name, independent credentials, permissions, no unintended host
port/public exposure, and no socket on other executors. Compare the returned
container/ImageID/RepoDigests with an independent trusted engine read. Check
wrong bearer, foreign target, invalid frame and unavailable target failures.
Restart this observer and recheck its exact options and facts; restart is not
permission to restart an observed service. It persists no observation database.

Before changing an existing installation, preserve its reviewed image and
options through the platform's approved backup path. This service holds no user
workload data, but its options contain secrets and require private backup
protection. Do not call a local image build an HA backup or rollback result.

Rollback: revoke observer caller credentials and block new calls, disable both
flags, stop the observer, remove its socket/API permission, and confirm that
other execution components still have no socket or host-port exposure. Start
the default disabled configuration only if needed and verify artifact/preview
routes are 404. Restore a previous installation only with its original reviewed
image/options and explicit target authority.

Local validation uses real temporary Unix/TCP fixtures and synthetic secrets.
Opt-in container checks exercise actual init/run.sh/main without network,
host-port/socket mounts or production credentials. Multi-architecture builds,
HA installation, target facts, restart and revocation results must be recorded
by the deployment owner before claiming instance acceptance. This component
does not build, publish, deploy, backup or restore target workloads.
