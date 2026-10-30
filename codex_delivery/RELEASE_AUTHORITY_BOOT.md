# Fixed release authority boot composition

Status: local implementation in progress; no production qualification is granted.

## Frozen configuration contract

The image-owned boot layer reads only
`/etc/codex-delivery/release-authority/registration.json` before UID drop. Every
ancestor is opened descriptor-by-descriptor with `O_NOFOLLOW`, must be root-owned
and not group/world writable. The final directory is root0700 and the regular,
single-link registration is root0600, at most32768 bytes. Missing registration or
exact `{version:1,enabled:false}` returns no plan. Malformed/incomplete enabled
registration fails closed with a stable code. No option/environment/model input
selects these paths, code, URLs or policies.

Enabled registration has exactly `version:1`, `enabled:true`, `policy_sha256`,
`manager_instance_ref`, `role_tokens` and `target_observation_token`. `role_tokens`
has exactly broker/manager/backup strings. The four printable ASCII tokens are
32–4096 bytes, pairwise distinct and distinct from every active forbidden
credential supplied by trusted boot. Fingerprints pinned in the image also
forbid reuse of other credentials, including fnOS OAuth credentials that this
boot code must not discover/load. Provisioning must pin all applicable fnOS and
other reserved fingerprints; a parser cannot establish completeness of that
external inventory. Never record token values in examples or ordinary logs.

The fixed image file `/opt/codex-delivery/release-authority-policy.json` is
root-owned, single-link, not group/world writable, at most32768 bytes; its entire
ancestor chain has the same ownership/no-symlink boundary. Its exact fields are
`version:1`, `manager_origin`, `manager_instance_ref`, `roles`, and
`forbidden_token_sha256`. Registration `policy_sha256` is the SHA256 of the exact
policy file bytes. `manager_origin` is one canonical HTTP(S) origin (explicit
port optional), without userinfo/path/query/fragment; a fixed DNS alias is
supported. The instance reference must match registration. Each role contains
exact nonempty, unique arrays `owner_refs`, `project_refs`, `target_refs`,
`release_refs`, at most128 references each. Exactly three roles are required.
`forbidden_token_sha256` is a unique array of SHA256 fingerprints, at most128.
Policy/registration is data only; it cannot select factories, modules or commands.

## Read client and deadlines

The only remote request is one GET to
`/internal/v2/release-target-observations/{target_ref}/{broker|manager|backup}`
at the image-pinned Manager origin, using the independent target observation
bearer. Logical references and role allowlists are checked before DNS. The
Manager returns exactly existing broker/manager/backup view fields, with no
additional envelope. The declared Manager instance is an independently
provisioned origin binding, not an extra identity claim synthesized from target
instance fields. Actual target facts still pass the canonical authority checks.

An absolute budget of at most5 seconds includes one DNS resolution, one address
connect, optional verified TLS, headers and body. There is no proxy, redirect,
HTTP retry or DNS retry. Client work has two slots; timeout returns unavailable,
but a worker retains its slot until actually exiting. A watchdog closes the
exact created socket at the original deadline, including slow response headers.
Late DNS results cannot create sockets or send requests. A response requires
status200, one Content-Type exactly `application/json` or the existing Manager
`application/json; charset=utf-8`, one decimal Content-Length1–32768, no
transfer/content encoding and no duplicate headers/JSON keys. Status and response
headers have a separate32768byte cumulative read budget. Invalid framing,
nonfinite JSON, incomplete/oversized/late results fail closed without echoing
headers, tokens, URLs, response bodies or underlying exceptions.

## Lifecycle and acceptance

`ReleaseAuthorityBootPlan.load(forbidden_credentials=...)` only reads/parses
fixed files. `--check-config` does not resolve names, connect, construct clients,
create state or drop UID. Root-private tokens remain only in trusted RAM after
drop. Once DeliveryService exists, `plan.create(service.store, release_reader=...,
source_qualifier=...)` installs a canonical authority against the exact same
WorkStore before listeners start. Trusted optional source/release callables are
explicit Python boot arguments. Missing callables stay absent and return503;
no options or expected facts become source/build qualification. The existing
canonical authority two-slot limit is preserved. Same-library task scope does
not require repository/source qualification.

This composition neither starts workers nor creates releases, deploys services,
changes tasks, reads Docker or performs NAS operations. Health does not claim a
complete publish path. Local fixture results and live HA deployment acceptance
remain separate. Rollback removes/disables this registration and restarts the
service through its already-authorized deployment procedure; disabled authority
routes retain their existing unavailable response.


## Closed image staging

`runtime-build/stage_execution.py` accepts the optional explicit administrator
`--release-authority-policy /absolute/nonsecret-policy.json`. The same pure,
closed policy parser runs before creating the destination context. Only exact
validated policy bytes are copied to the fixed
`runtime-assets/release-authority-policy.json`, mode0444; changed bytes or a
symlink input fail without a success receipt. Its exact SHA256 is included in
both the source manifest and `release_authority_policy_sha256` staging receipt.
There is no registration, token, secret or session staging argument; credential
fields/unknown policy fields are rejected. Never supply a private registration
as policy input. Public runtime and SQL/CLI staging keeps its existing contracts.

The generated `.dockerignore` enumerates exact staged runtime paths, rather than
allowing every file below runtime-assets; late-added registrations/keys cannot
enter the image. The root Dockerfile's fixed allowlist also permits this one
optional policy filename. With no policy input the new context has no authority
policy and its receipt records null. The source-root default build remains
possible with neither execution nor authority policy. An enabled authority
registration cannot run without the matching policy. Staging/building never
installs a private registration, starts a remote service or qualifies source or
release facts; those factories remain explicitly absent from main.


## Local validation on 2026-10-29

The final related suite passed61 tests, including actual Manager HTTP role-view
interoperability, same-WorkStore task scope without repository qualification,
DNS timeout slot retention, slow-header socket abort, malformed/oversized/header
budget rejection, boot ordering and active credential separation. Opt-in Docker
checks read actual root0700/0600 files, ran check-config without state, rejected
symlink ancestry, dropped to UID65534 and constructed the authority using only
preloaded RAM. These Docker checks use synthetic credentials and no network.

Both source-default and synthetic-policy staged amd64 Dockerfile builds passed.
Actual image checks confirmed policy hash/manifest, SQL and CLI preservation,
exclusion of three late-added credential-shaped fixture files, default-off
behavior, enabled-without-policy rejection and a real DeliveryService using the
same WorkStore. Health still claims no execution or complete publish path.
The synthetic staged runtimes/CLI were copied only and are never executed or
qualified. The initial incorrect Docker directory allowlist was caught by this
real COPY check; the final context uses per-directory re-exclusion and exact
file exceptions. Its failed earlier image tag was removed. No container remains.

`git diff --check`, scoped Python AST and the root design-governance check also
passed. No real credential, Broker, remote service, account, deployment, Git
submission/push, HA qualification or source/release factory was changed here.
