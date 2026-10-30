# Fixed OCI to Registry bridge

The bridge implements the [Broker project delegation contract](../ha_operations_broker/BUILD_DELEGATION.md).
It reuses the exact RepositoryManager Store, same ObjectArchive and same replica.
The confirmed existing software.build.claim is the sole pipeline build claim.

`RegistryBuildBridge.execute(request, files, deadline=...)` reserves its original
request at Broker, executes the fixed OCI builder, seals actual artifact bytes and
metadata to ObjectArchive, restores and compares them, asks Broker to issue one
exact grant, registers it, verifies an independent grant GET, publishes once and
returns actual Registry-backed BuildObservation.

`observe(request, deadline=...)` and `BuildEvidenceProjection.input/artifact` never
call build, save, reserve, issue, register or publish. They re-read original durable
facts, source/OCI artifacts and the off-machine archive. Temporary private restore
and Git object-reading directories are necessary for validation; these do not create
new persistent build claims or artifacts. The archive's internal save budget cannot
be forcibly interrupted by this wrapper; checks before/after save retain the original
deadline and prohibit any later action after expiry. Total artifact + metadata stays
within the existing 256 MiB ObjectArchive bound.

A durable local phase record is written exclusively before each side effect. A crash
between archive completion and receipt persistence is unavailable, never an excuse to
save again. Lost Broker/register/publish replies are resolved with GET of the original
grant; absent or uncertain evidence stops. Qualification never repairs these records.
The Registry writer and reader are separate credentials; signing key lives only in
Broker. Real startup still must install exact fixed policy, original Passkey delegation,
credentials and role allowlists. These modules do not qualify runtime health or deploy.

## Fixed boot composition candidate

`registry_build_composition.py` supplies a data-only boot plan and concrete lazy
build/evidence Ports. Production `main` source now connects these Ports to the
original RepositoryExecutionFactory and the existing bounded HTTP listener,
before either scheduler starts. This is default-disabled source wiring, not live
activation. No Add-on option enables it, and this module starts no listener,
creates no approved delegation, migrates no database, loads no signing private
key, and changes no remote service or network permission.
The existing Registry host-network approval, actual Broker v8 startup/Passkey
activation, full repository gates and HA acceptance remain separate prerequisites.

`RegistryBuildBootPlan.load(forbidden_credentials=...)` reads only:

- `/etc/codex-delivery/registry-build/registration.json`: root-owned `0700`
  parent / `0600` regular file, exact disabled registration or four distinct
  issuer/writer/reader/evidence credentials plus the exact policy digest.
- `/opt/codex-delivery/registry-build-policy.json`: root-owned image policy,
  pinning service UID, owner/project/full repository-policy set, private numeric
  Broker and Registry origins, original delegation reference, Registry public
  key, complete canary recipe/base identities and exact Registry binding.
- `/opt/codex-delivery/registry-build/base.oci.tar`: fixed root-owned image asset,
  bounded and verified as the complete pinned OCI base graph before use.

All ancestors are opened with `openat`/no-follow and checked for root ownership
and writable modes. The opened regular file must have one link, a bounded size
and unchanged identity before/after reading. Missing or exact disabled registration
loads neither policy nor base. Parsing and Port creation perform no network calls.
`parse(...)` exists for trusted boot/tests; it does not establish file provenance.
All four credentials reject pairwise reuse, active reserved credentials and image
policy fingerprints. None is accepted from tasks or dynamic executable paths.

Trusted boot creates `ports = plan.create()` before constructing the repository
factory, passes `ports.execute` / `ports.observe` as its build Ports, then calls
`ports.attach(manager, archive, state_root)` with the factory's original Manager
and ObjectArchive. Attachment is allowed once, even after failure. It requires
`/data/state`, the original Manager Store's exact parent, the image-pinned UID,
private non-symlink state/build directories and the Store's identical Replica and
failure domains. The concrete builder, projection, publication client and independent
Registry reader all retain that same Store/Archive; attachment creates no new Store,
Archive, state tree, task, claim or thread. A failed or absent attachment leaves
execution, observation and `ports.evidence_routes` unavailable.

The evidence routes now use the existing bounded trusted Delivery HTTP server;
they expose only GET `/internal/v1/build-evidence/<64-hex-request>/input` and
`artifact`. They authenticate the independent
evidence credential and reject any owner/project/target/repository-policy mismatch
before off-machine reads. They retain the original bridge's read-only/unknown-result
semantics. Missing Ports return unavailable; invalid paths, non-GET methods,
duplicate authorization and nonempty/ambiguous GET framing are rejected before
projection reads. Actual image staging, registrations, deployment and qualification
are still required.

`software_registration.py` is a lightweight selector: missing or exactly disabled
root-provenance registrations do not import optional Registry libraries. The
default metadata-only package can pass configuration checks without the sibling
Registry package. An active registration requires the actual staged single-source
library, complete policy/base and paired repository plan; a missing dependency
fails closed. A missing file is checked before descriptor traversal, while every
existing file still undergoes root/private/no-follow checks.

`ProductionLifecycle` attaches original source/validation and Registry Ports
exactly once before starting Service/SoftwareWorker. During shutdown an unsettled
software worker or original service retains QualifiedExecution resources and
locks, returns failure, and is not silently retried in finally. The combined local
boot/HTTP/Repository/Registry and existing service regression group passed **97
tests in 40.968 seconds**. It overlaps earlier groups and includes fixture
qualification/targets; it is not VM, Passkey, recovery or HA platform acceptance.

Local tests use generated TEST keys, a synthetic non-executable base and actual
loopback Broker/Registry HTTP, Git objects, SQLite and AES archive readback. They
exercise the concrete boot-created clients and Ports through the pipeline, including
read-only observation without build/save/reserve/issue/register/publish. They do not
prove production Passkey, runtime qualification, HA deployment or runtime health.
