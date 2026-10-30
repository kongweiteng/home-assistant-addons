# Explicit software execution handoff

`repository_execution.py` uses the actual `RepoManager`, original `WorkSourceArchive`, `ObjectArchive`, existing Delivery command/event chain and one-attempt Publisher. `repository_composition.py` supplies concrete boot assembly and a singleton worker. It installs no builder, grants no credentials, exposes no new route, changes no task modes and deploys no service.

**Production is incomplete** until image boot supplies a qualified builder, independent build/target observers, and actual authorization, secret scanning, test and workspace gates. The fixed offline OCI assembler is now implemented in [OCI_BUILDER.md](OCI_BUILDER.md), with real Git and complete graph verification. It produces offline artifacts only; registry/Engine identity, Supervisor exact consumption and actual qualification remain separate gates. Configuration flags and fixtures are not build facts. Tests use actual disposable Git and AES; their builder/target Ports are explicitly synthetic.

## Explicit source and task binding

An authorized original `develop`, `deliver` or `deploy` task binds a separate `work` task with profile `code`, in the same Store. Both require the same owner, active project, exact full frozen repository set, write access and exact revisions, with no path-only binding or pending control. Work gains no repository write/publish authority.

Before code execution, the trusted caller invokes:

```python
pipeline.bind(owner, idempotency_key,
    task_ref=software_task, revision=software_revision,
    code_task_ref=code_task, code_revision=code_revision,
    release_ref=release_ref, version=version, target_ref=target_ref,
    permissions_digest=permissions_digest,
    publish_targets={repo_ref: approved_alias}, deadline=original_deadline)
```

Actual sync/export freezes every baseline and reuses any confirmed original export. Mandatory `bind_code_task` authorization binds the full request. `develop` requires an empty publish map; `deliver`/`deploy` require one fixed approved alias per repository. Release references are unique across the Store.

Guest inputs use the independently scanned tracked-file view in [REPO_MANAGER.md](REPO_MANAGER.md). It excludes **all untracked and ignored files**. Supporting existing untracked source requires a separately approved source scope/scanner. Explicit change packages support legitimate new code without automatically exposing local untracked/ignored data.

## Code output and actual pipeline

The code task declares `candidate.zip`. Its outer ZIP contains exactly `repositories/<repo_ref>.zip` for every frozen repository. Each inner ZIP contains canonical `changes.json` plus exactly its referenced regular file payloads:

```json
{"baseline_oid":"<full original OID>","changes":[{"entry":"files/0","mode":420,"operation":"write","path":"src/application.py","sha256":"sha256:<digest>"},{"operation":"delete","path":"old.txt"}],"repo_ref":"<repo_ref>","version":1}
```

JSON uses Delivery canonical encoding/ordering. Writes use mode 0644 or 0755. Duplicate/case-colliding paths, `.git`, links, special/encrypted entries, extra files, missing deletes, wrong baselines and oversized content fail closed. Outer/inner content is bounded to 32 MiB and 4096 changes. Existing manager bundle/history/unsafe-Git/validation limits also apply.

The trusted compiler writes bare Git objects and a deterministic descendant commit as data. It never checks out or executes source, hooks or build scripts on the host. Actual stage/import still require independent full-source scan/test receipts. The user's checkout remains untouched.

`ArchivedCodeChanges` verifies exact code revision/spec/workspace and source baselines, successful stopped execution, original confirmed `work.checkpoint` and output manifest, then decrypts the original candidate bytes. It does not accept Controller upload URLs, live files, resolver output or guest success flags as evidence.

`pipeline.run(owner, binding_key=..., deadline=...)` assembles/imports every candidate, seals original source bundles/candidate bundles/full manifest through the same `ObjectArchive`, then reads them back. The build request binds every baseline/candidate OID, bundle/validation/policy digest, original owner/project/tasks/revisions, target, permissions and version.

Image-owned `build_executor(request, files, *, deadline)` must invoke a fixed qualified build template. The guest cannot select builder/observer/credential/qualification. Delivery confirms `software.build.claim` off-device **before its only build attempt**. After timeout/crash/unknown, restart calls only `build_observer(request, *, deadline)`; absence does not authorize redispatch.

An independent observer returns `BuildObservation` from actual immutable evidence: exact request digest, build reference, typed Docker image ID or OCI repository digest, actual permissions digest, fixed runtime qualification digest and evidence digest. First executor/observer receipts must match; later observations must match the original confirmed receipt. Failed/missing/unknown/changed facts raise.

Before build/publication, the independent target observer must bind exact target/project/permissions to a current evidence digest. Actual prepare/publish then enforces original mode and per-repository alias. Unknown publication is observed against its original exact remote ref, never pushed again. Baseline/absence stays unknown; a later exact candidate OID can confirm the original attempt.

## Release7 bridges

Stable boot interfaces are `factory.release_reader(release_ref, *, deadline)` and `factory.source_qualifier(target_ref, release_ref, *, deadline)`.

`release_reader` returns exactly `schema_version=2`, `release_ref`, `version`, `source_digest`, `artifact_identity`, `permissions_digest`, `qualification_digest`. It reads confirmed original build and release events, verifies their chain and derived immutable facts, and returns owned primitives. It never builds or copies mutable release options.

`source_qualifier` independently rechecks current original task/project/revisions/full repository set, decrypts exact source/candidate archives, revalidates actual scan/test receipts, reads each current published remote OID and obtains current independent build/target observations. All facts must match the frozen build. Missing bytes, drift, unconfirmed durability or expired deadline fails closed. This reader never pushes, rebuilds, changes tasks or reconciles DB events. Ports receive the original deadline without renewal.

## Concrete boot and remaining production work

Image-owned code constructs `RepositoryExecutionFactory` with fixed policies/Git binary and actual trusted Ports. It creates `GitRuntime`, `RepoManager`, `RepositorySources` on the exact Store/ObjectArchive supplied by `QualifiedExecution`. Required schema migration is a prior explicit offline step.

```python
factory = RepositoryExecutionFactory(
    policies=trusted_policies, git_binary=fixed_git_binary,
    authorization_gate=actual_authorization, validation_gate=actual_scan_and_tests,
    workspace_bindings=actual_workspace_registry,
    quiescence_gate=actual_stopped_workspace_observer,
    input_gate=actual_source_exposure_scanner,
    build_executor=actual_fixed_builder,
    build_observer=independent_actual_build_observer,
    target_observer=independent_actual_target_observer)
execution = QualifiedExecution(policy, settings, state_root, repository_factory=factory)
# Existing DeliveryService constructs execution(store)/WorkCoordinator first.
worker = factory.attach(execution.source_archive)
# CanonicalReleaseAuthority receives factory's stable readers.
worker.start()
```

Main owns worker lifecycle/`close()`. A private `flock` prevents replacement while its original trusted Port remains unsettled. The worker consumes only explicit confirmed `software.bind` events whose exact code task has stopped. It creates no task/mode/implicit authority. Failures back off without exposing provider/source data. No task-selected import path or generic shell exists.

Reusable existing components are Store, encrypted archives, RepoManager/Publisher, QEMU code isolation, this composition/worker and release read contracts. **Missing production components: a fixed OCI builder and independent durable build observer; deployment-specific authorization/scan/workspace providers with credential/qualification setup; trusted bind entrypoint and main wiring.** Main must supply these before advertising software build/publication. No default Port self-approves its configuration. Actual architecture builds, remote Git credentials, service deployment/rollback and end-to-end acceptance remain production gates.

## Supervisor build investigation

The official Supervisor source was inspected read-only through its GitHub contents API. The inspected `supervisor/apps/app.py` blob was `312f0d4ff48b6a322f4f52fe836bfc0ff51cb716`; `supervisor/api/apps.py` was `5511bf5ab099a231c78c51b3d826b9b7e7cedd5e`; `supervisor/docker/app.py` was `4dcb3c53feeddc6f433e458db44db7ca9134d6b6`. These are source observations, not claims about a currently deployed Supervisor version.

[App install/update/rebuild](https://github.com/home-assistant/supervisor/blob/main/supervisor/apps/app.py) shows that install builds/pulls and registers an app without an explicit start; update restores its earlier running state; rebuild removes the existing container/image and installs again, restarting it when previously running. [The rebuild API](https://github.com/home-assistant/supervisor/blob/main/supervisor/api/apps.py) accepts its fixed force schema, not Delivery's frozen source digest/OID/artifact manifest. [Docker app install/build](https://github.com/home-assistant/supervisor/blob/main/supervisor/docker/app.py) selects a private build from `AppBuild.create(app)` and store context.

Rebuild therefore cannot serve as an independent non-starting build fixture. A separately approved canary install may produce an image without a start, but still needs independent proof that its actual Supervisor build context exactly equals the frozen candidate. A running image's GET-observed ID alone cannot prove that source relationship. A fixed independent builder producing immutable OCI artifacts, or a new constrained Supervisor context/OID observation and registration contract, is required. Delivery receives no Docker socket through either investigation or this candidate composition.
