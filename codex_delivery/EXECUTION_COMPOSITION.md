# Trusted execution composition candidate

`RepositoryExecutionFactory` in `repository_composition.py` is the concrete
trusted RepoManager/source/Pipeline factory. Its exact same-Store attachment,
singleton worker, Release7 read bridges and remaining production builder/gate
prerequisites are documented in [SOFTWARE_EXECUTION.md](SOFTWARE_EXECUTION.md).
After the existing service constructs execution, `execution.source_archive`
exposes the original archive instance for `factory.attach(...)`; no duplicate
resolver, archive, task Store or runtime is created.

`execution_policy.py` and `execution_composition.py` assemble the existing
Delivery Store, fixed QEMU backend, execution journal, Runner identity and
Controller callbacks. They add no task database or model-callable launcher.
The current image contains no installed policy or QEMU assets. It remains a
metadata image; setting `runner.enabled` cannot make it executable.

## Fixed policy and private settings

The trusted entry point reads only `/opt/codex-delivery/execution-policy.json`.
The policy and every ancestor must be root owned, without group/other write or
symlinks. Closed JSON rejects duplicate keys, commands, import paths, arbitrary
asset directories, unpinned digests and unsupported profiles. Asset directories
derive from fixed runtime IDs under `/opt/codex-delivery/runtimes`.

Each runtime pins its manifest, native helper, isolation profile, interpreter
and exact profile budgets. The optional explicit `kind` defaults to the existing
work contract; a browser kind requires the distinct browser intent interpreter
and cannot claim work profiles. An explicitly pinned preview kind uses its own
intent interpreter, fixed preview budgets and the same canonical dispatcher;
it is optional and remains unavailable until its real runtime qualifies.
The canonical Registry policy binds these hashes, kind and interpreter,
the complete host policy, Delivery instance and extension identity. The
administrator enrolls that exact policy separately. Parsing a policy is not
host qualification: actual assets, host budget checks and native qualification
must pass before reporting `self_check_ok` or starting a Registry session.

`runner` options contain exactly `enabled/controller_url/runner_ref/runner_bearer/
callback_bearer/policy_revision/policy_digest`. They select no executable code.
The two bearers differ from each other, extension/origin credentials and the
replica key. Values stay in private configuration and trusted memory, never in
task programs, guest files, capabilities or logs. `execution_enabled=true`
continues to be rejected as a legacy bypass flag.

Before qualified assembly, `bootstrap.prepare_and_drop()` creates only a new
`/data/state` directory, owned by the fixed runtime UID with mode `0700`.
Existing root-owned or wrongly permissioned state is rejected and preserved;
there is no recursive chown, automatic migration or restore. The one-way drop
clears supplementary groups, saved root IDs, effective/permitted/ambient and
bounding capabilities, sets NNP and disables process dumpability. Initial
CHOWN/SETUID/SETGID/SETPCAP are needed only for this drop; none remain in the
service. The already parsed options stay in memory. This does not give QEMU
any host capability, Docker socket, device or Supervisor credential.

## Lifecycle and stop evidence

A lifetime file lock prevents two qualified services from opening sessions or
recovering the same execution journal concurrently. Existing executor dispatch
locks enforce concurrency one. Delivery alone supplies assignments and leases;
the assignment reader checks pending durability, project status, control intent,
Runner identity, lease and expiry. Presence/cache readers cannot issue a lease.

`PresenceBoundAdapter` confirms busy before executing. A stopped receipt is
checked against the real execution journal and immutable checkpoint before
reporting stopped. Registry write ambiguity freezes admission locally. A proven
receipt is still returned to the Coordinator for encrypted archival even if
the independent presence report fails. Control revisions may advance while
stopping: only the same current lease/epoch and pending pause/cancel/revise can
read the original execution revision; the archive retains that revision.

Preview VM creation stays on the persistent worker inside its lifetime guard.
Controller HTTP threads can only read an already running session or submit a
closed pixel input. The private RPC binds one Controller boot to one VM nonce;
unknown outcomes fuse access and cannot be retried or adopted after restart.
The fixed 10-second stop reserve permits checkpoint export only while the
original lease and an independent retiring-assignment gate remain valid.

A settled preview checkpoint includes a reserved host `report.txt` for delivery.
On resume, WorkExecutor supplies the prior settled journal receipt. The preview
backend verifies the original checkpoint hash, complete file manifest, workspace,
preview execution marker and exact host report bytes, then builds a new guest
restore view without that host output. It rechecks the guest 1MiB/128-file limit;
the original 2MiB/129-file checkpoint remains unchanged. Guest-created or
unproven reserved output is rejected. Source input files are a separate tree.

The Coordinator's `ObjectArchive` uses **the same Replica instance** as the
Store. Real configured storage remains mandatory. Capabilities are unavailable
while the replica is unqualified, fused, busy or recovering, while identity is
stale/frozen/disabled, or while the service worker is disabled/stopping.
An unclean executor journal or restored Store opens only a frozen recovery
session; this cannot clear either Registry or Delivery recovery protection.

Qualified assembly also installs `WorkSourceArchive`. Before claiming a task,
it freezes the exact original program, resolved input bytes and canonical task
specification into a private immutable revision directory. The existing
encrypted archive confirms those bytes; `work.source_archive` then records its
descriptor in Delivery's existing event chain and passes the metadata replica
gate. Neither an unknown object nor an unconfirmed event can launch a program.
The source archive is independent of the guest output checkpoint: a crash may
leave execution unknown while its original source remains restorable. This
does not claim that unrelated repository objects or live project files were
archived. Trusted boot may supply the fixed
`repository_factory(store, archive, state_root)` constructor argument to
`QualifiedExecution`; it must return RepositorySources bound to exactly that
Store and ObjectArchive. Model options cannot name/import a factory. Without
one, tasks with repositories remain unavailable while owner-only work can run.

The production factory must provide the real RepoManager policies/runtime,
current authorization/validation gates, exact private stopped workspace bindings
and an independently qualified source-content scanner. Assembly injects this
provider into WorkSourceArchive rather than leaving an unconnected component.
The provider syncs/exports one exact baseline per canonical revision, archives
the complete original repository, then derives a separate reviewed tracked-source
USTAR view from those sealed encrypted bytes. Credentials, raw Git metadata,
ignored/untracked files and live checkout paths never become guest inputs.

The coordinator now reads confirmed archived source inputs before invoking the
resolver. After a first source save, it authenticates that archive before claim;
after restart it does the same without resolving or recapturing current data.
Unknown/corrupt/pending source evidence keeps the task queued. Public-browser
tasks remain unable to acquire implicit private repository inputs. Exact source
packages participate in the existing readonly guest input tree, executor lease,
encrypted checkpoint and original-chat artifact flow. This wiring does not grant
ordinary Work tasks Git publication or deployment authority.

Shutdown stops claiming, requests an exact cancellation and waits up to 150
seconds for VM stop and the bounded archive. Only then does the launcher close
presence and the identity cache. Active/unknown work cannot produce a clean
stopped report. A deployment must also configure a sufficient Supervisor stop
grace; the current metadata packaging has not qualified that lifecycle.

## Evidence and remaining gates

Local tests combine real Registry/Controller HTTP, canonical Delivery leases,
the real executor journal/checkpoint, AES archive and send receipt. Their VM
backend is synthetic, so they do not prove production QEMU assembly. Separate
container tests exercise the actual irreversible privilege drop, private state
write and refusal to migrate existing root-owned data.

Browser composition routes an explicitly pinned browser runtime to
`BrowserWorkBackend` and the fixed QEMU browser, using the same dispatcher,
canonical task reader, leases and archive instances. It does not send private
inputs or prior checkpoints into that VM. The browser claim lasts at most 120
seconds; preview and code have separate 1800/3600-second limits. The current
image still contains no installed browser assets or production policy. Preview
uses the same source/output archives. The owner viewer accepts task and revision
selectors only; actual identity comes from the qualified ingress mapping. Its
operations inherit the incoming absolute five-second deadline rather than
renewing a budget after body reading or authentication. The production identity
verifier is still absent, so the default entry point disables all preview routes.

Actual immutable image assets, HA resource profile, persistent paths and grace,
real node enrollment, independent storage and recovery, browser/preview and
deployment acceptance remain required before unified installation. A running
HTTP listener or a successful configuration check satisfies none of those gates.
