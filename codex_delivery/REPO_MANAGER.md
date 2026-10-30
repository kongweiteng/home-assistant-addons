# Bare repository manager candidate

Explicit new-source packages, authenticated code-Work handoff, actual
candidate/import/Publisher composition and remaining build prerequisites are
documented in [SOFTWARE_EXECUTION.md](SOFTWARE_EXECUTION.md).

`codex_delivery.repo_manager` is a trusted service component. It uses the existing
DeliveryStore owner, project, task, command, event and durability records. It does
not create a second task database. No production Git credentials or network
enrollment are supplied, and these local tests are not production acceptance.

## Installation and trust

DeliveryStore schema v4 installs and verifies the repository evidence inside its
new-database or explicit migration transaction. Opening v1/v2/v3 requires
`upgrade_schema=True` and preserves a private, fsynced pre-upgrade backup.
`install_repository_schema(connection)` is an internal migration primitive that
requires an active transaction; RepoManager never installs from its constructor.
Missing or changed v4 evidence definitions fail closed. Snapshots and frozen
restores include these tables in the existing backup/restore contract.

Construct the manager with a dedicated private persistent root, a tuple of
administrator-owned `RepositoryPolicy` entries, a fixed `GitRuntime`, and both
mandatory gates. Remote identities resolve through the existing repository
catalog to exact HTTPS URLs, baseline refs and named publication targets. URLs
cannot contain userinfo, query, fragment, redirects or a transport override.
Models must never set policies, runtime executables, paths, refs or gate callbacks.

`authorization_gate(context)` must check current ownership, delegation, expiry,
revocation and action scope, returning `AuthorizationReceipt` bound to the exact
canonical context digest. It is invoked again after claim replication and before
push. The initial grant and context digest are retained in the claim event; a
changed grant cannot reuse that claim. `validation_gate(context)` must return `ValidationReceipt` binding the exact
bundle digest, baseline/candidate OIDs, context, qualified secret-scan evidence and
qualified test evidence. The manager verifies these bindings; it does not invent
tests or implement the scanner. Both `scan_passed` and `tests_passed` are required
boolean `true` decisions; a digest of a failed report cannot grant publication.
Changed evidence prevents publication. Gates must
sanitize their own logging and fail closed. A callback that always approves is
only a test fixture.

Git has a clean environment, system/global config disabled, fixed user identity,
no prompting/helper, no hooks, URL redirects, filters, textconv, submodule/LFS
fetching, automatic GC or pruning. Its total time and combined stdout/stderr
budget are shared across each logical Git operation. Diagnostics are not exposed.
TERM/KILL request cleanup; they do not prove that a kernel operation stopped.
Provision a filesystem quota/resource boundary before qualifying remote fetch;
a command timeout is not a downloaded-pack disk quota. Private local storage and
the trusted Git installation must remain responsive.

## Interfaces

All methods take a trusted owner and idempotency key. Task methods bind
`task_ref`, `expected_revision` and `repo_ref` to the frozen task repository set.

- `sync`: fetch the registered baseline into a new retained ref and verify its
  current remote OID. Preserve the previous manifest when the remote races.
- `export_bundle`: freeze a whole-repository source bundle for the task revision.
  Path-limited access is rejected. This does not include dirty/untracked files.
- `stage_bundle`: the trusted upload adapter stores bounded input bytes under a
  verified digest and returns an opaque artifact ref. Do not expose this as a
  model-selected filesystem operation.
- `import_candidate`: use that artifact ref, expected digest and exact candidate
  OID. Verify in a separate bare quarantine, including bundle refs, fsck, baseline
  ancestry, mandatory gates, and recursive-content checks. The manager rejects
  gitlinks, `.gitmodules`, LFS pointers, filter attributes and conservatively all
  symlinks. Preserve quarantine evidence and retained candidate refs.
- `prepare_publish`: bind an approved candidate to a named administrator target
  and the observed remote baseline. Only deliver/deploy tasks can publish.
- `publish`: only the invocation that truly first claims a prepared operation and
  receives replicated acceptance can issue one ordinary, non-force push.
- `reconcile_publish`: only query the exact remote ref. Candidate OID confirms;
  unchanged baseline/absence remains unknown; another OID produces conflict.
- `publication_status`: read stored evidence without invoking Git or the network.
  State and event durability are reported separately.

## Unknown results and recovery

Claims and results share the original SQLite and append-only events. A pending
replica prevents push. Sending claims are publicly unknown after restart. A
duplicate command, lost response, process interruption or timeout cannot call
push a second time, even when `ls-remote` still shows the original baseline.
Unresolved publications block another sending claim for that repository. A new
publication requires a separate reviewed authorization/baseline/evidence check.

Bare mirrors are single-writer and exec children inherit the lock. The manager
never checks out a candidate or runs project code on its host. It does not
reset/stash/clean user work, delete refs, merge/rebase, or compensate a remote
publication. Runner workspace execution and stopping remain the Runner contract.
No multirepository atomicity is implied: retain per-repository publication facts.

## Sealed source inputs for general work

`RepositorySources` is the trusted bridge into the existing Work coordinator.
For the complete frozen task repository set it first calls the real manager's
`sync` and `export_bundle` with stable owner/task/revision/repository keys. Both
events must have replicated acceptance. Retries reuse the original keys and
baseline instead of fetching a later remote tip into the same task revision.
Administrator/Runner workspace bindings and a stopped-workspace gate remain
mandatory. The full Git/dirty/ignored capture then uses the same encrypted
ObjectArchive and canonical `repository.archive` event described below.

`repository_inputs.sealed_repository_inputs` separately verifies that confirmed
event and restores its exact encrypted bytes into private temporary trusted
storage. It performs the same manifest, every-file hash, Git fsck/state and mirror
identity checks as offline restore; this internal staging does not clear recovery
mode or expose the recovery tree. Public `RepositoryArchive.restore` still
requires a frozen recovery-only Store.

The input view uses only the sealed index and sealed working-tree contents.
Raw `.git`, config, objects, ignored and untracked files never enter it. An
index-tracked file now ignored by the sealed ignore rules, or absent in the
sealed working tree, is listed as excluded and is not substituted from the
index/live checkout. Global excludes are explicitly disabled. Known credential
paths are rejected; that filename guard is supplemented by a mandatory
independent `input_gate(context, files) -> SourceExposureReceipt`. The receipt
must bind the exact task/repository/archive, manifest and actual scan digest.
Missing/denied/mismatched scanning closes admission. A production gate must
actually qualify its scanner and inspect content; fixture receipts are not
production credential-scan evidence.

Each reviewed view becomes `repositories/<repo-id>.tar` plus a canonical JSON
manifest in the guest's readonly input tree. The bounded USTAR has only reviewed
regular source files and owner read/execute modes, never host paths or Git
configuration. Packaging keeps larger repository file counts within the existing
bounded number of Work input objects; total archive/runtime byte budgets still
apply. Resolved user inputs cannot occupy this reserved namespace. Public-browser
tasks cannot acquire repository source inputs implicitly.

WorkSourceArchive seals these packages together with the exact program, original
resolved input bytes, canonical task and complete repository event/receipt set.
Before claim, the coordinator reads back the confirmed encrypted source archive
and passes those bytes to the executor. A restarted queued task reuses its
original confirmed archive without re-resolving inputs or reading any live
checkout. Changed/missing/corrupt evidence never falls back to current files or
authorizes another original capture. Existing Work mode remains unable to write
or publish through RepoManager; execution inputs confer no Git/deployment rights.

## Full Git and workspace archive

`RepositoryArchive` uses the **same ObjectArchive and replica instance** as the
DeliveryStore. The existing `repository.archive` command/event binds its receipt
to owner, task, input revision, project, repository and an opaque workspace ref;
there is no additional journal or task database. A trusted administrator/Runner
provides an exact `RepositoryWorkspace` binding, including registered remote
identity and a private canonical directory. Model arguments cannot choose that
directory. Both the authorization gate and a mandatory stopped-workspace gate
are checked before capture and again before metadata acceptance.

Capture includes the complete standalone workspace `.git` directory (all raw
objects, refs, HEAD, index, reflogs, private config and unpushed work), all tracked,
staged, unstaged, untracked and **ignored** regular files, empty directories and
owner executable bits. It also includes this repository's manager mirror,
immutable marker, retained candidate refs and every registered source/candidate
bundle. File identities and logical Git state are checked across capture. No
fetch, push, checkout, hook, project execution or automatic resume occurs.

A bounded USTAR pack and independent canonical file manifest fit within the
existing ObjectArchive limit of 256 MiB, including archive overhead; at most
65,536 internal files/directories and a 16 MiB manifest are accepted. Files and
the deduplicated registered bundles share a combined metadata budget checked
before payload reads. Every payload read is also capped by its remaining archive
budget, even if Git's independent bundle allowance is larger. Files and
temporary pack bytes stay inside private 0700 directories with owner-only file
modes. Private Git config is encrypted with the other bytes and is never included
in public events or logs. Unsupported links, hardlinks, special files, embedded
repositories, linked worktrees, submodules, alternates, grafts, shallow/partial
clones, replacement refs, LFS and filter content fail closed. This conservative
layout contract must be satisfied rather than silently omitting data.

Capture is sealed before upload. An unknown archive object can reconcile only
its original exact encrypted object through the one replica. A missing unknown
object is never uploaded again; the original sealed bytes remain available for
operator recovery. Live source changes cannot replace a pending capture under
the same idempotency key. A metadata outage reconciles the original event and
never recaptures the workspace. Confirmed metadata acceptance removes that
command's plaintext upload staging; unknown/pending staging remains private
until the original operation can reconcile.

`restore` requires an authenticated, frozen DeliveryStore restored from its
encrypted snapshot and a fresh absent target under a private canonical parent.
It verifies event evidence, scope, archive receipts, all file hashes and modes,
the exact tar manifest, Git fsck/refs/HEAD/status and the original mirror config
marker before publishing a new `workspace/` and `manager/` recovery tree. It
manually creates safe files without tar extraction, rejects overwrites and
leaves the recovered store read only. Recovery never fetches a remote or resumes
tasks/publications. An operator must establish one active writer and reconcile
external outcomes before enabling later execution.

The SQLite replica alone does not contain Git bytes. Production acceptance still
requires a real independent encrypted replica, qualified Git runtime, trusted
workspace stopping/binding, HTTPS identity/authentication and a complete restore
exercise. Local tests establish the component contract only. Loss of the manager
root or config-digest drift requires operator recovery; do not initialize a
replacement mirror over an incomplete or foreign directory.

## Local validation

The tests use a private subclass replacing transport with local bare fixtures.
Production has no file/SSH transport switch. Fixtures exercise real Git and
faults around claim replication, one push, response loss, restart reconciliation,
concurrent publication, authorization/validation drift, bundle attacks, retained
refs, dirty files and exec time/output/environment controls. They do not contact
a hosted Git service, retrieve credentials, or authorize deployment.

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=tests:. python3 -m unittest test_codex_delivery_repo_manager
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=tests:. python3 -m unittest test_codex_delivery_repository_archive
```

The archive tests use real Git and AES-GCM with an independent local disk CAS.
They delete the entire primary fixture (including its original Git remote),
recover the frozen SQLite snapshot and all Git/workspace bytes, and assert zero
fetch/push, zero replica writes during recovery and no resumed mutation. They
also cover unknown-object reconciliation, missing objects, metadata outages,
scope/quiescence drift, changed source, corrupted ciphertext, sealed-pack
tampering, unsupported layouts, budgets and protection of existing destinations.
