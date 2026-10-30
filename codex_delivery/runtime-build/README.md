# Credential-free runtime build assets

These sources build a disposable x86-64 Linux guest and a host-native QEMU TCG asset bundle for the independent `codex_work_runtime` library. They do not form an installed Add-on, register a node, receive credentials, or enable execution in the metadata service. The same guest can run on amd64 or aarch64 hosts without KVM, devices, host mounts, a Docker socket, or a network interface.

The build chain is the pinned HA Debian base in `Dockerfile`, optional `DocumentGuest.Dockerfile`, `WorkGuest.Dockerfile`, the static `Launcher.Dockerfile`, and `WorkRuntime.Dockerfile`. Use this directory as the Docker context and supply verified image digests for the architecture-specific `*_FROM` arguments. Package downloads keep normal HTTPS and Debian signature checks. `DEBIAN_MIRROR` is a trusted build setting; its URL shape follows each Dockerfile's documented default. Keep package versions, upstream licenses, image digests, and generated manifest digests with release evidence. The generated package inventory is inside the guest; no generated image, archive, binary, or instance data belongs in Git.

`Launcher.Dockerfile` has two explicit compile profiles. Its default `landlock-v1` requires Landlock ABI >= 3 and never falls back. `ISOLATION_PROFILE=readonly-jail-v1` builds a separate helper for kernels without Landlock. That profile requires a fixed readonly jail, no capabilities, locked securebits, no inherited directory descriptors, exact stdout/export identity, and native seccomp rejection of file creation/metadata mutation. Its reviewed syscall universe ends at native syscall 452; newer calls and other architectures fail closed. Only amd64 and aarch64 are supported.

`prepare_runtime.py` hashes every registered asset and records the helper's compiled profiles in the manifest. A caller must pin the full manifest digest. Runtime startup checks root-owned assets, no extra entries or links, actual host uid/capabilities/NNP/AppArmor and the explicitly selected resource boundary, then runs the fixed native qualification before task code. The default `cgroup-v1` requires actual finite cgroup budgets. The independent `RESOURCE_PROFILE=native-resource-v1` launcher and manifest instead require finite, matching trusted-parent AS/NPROC limits and CPU affinity; they never infer those limits from cgroup configuration or select themselves as a fallback. Native per-VM AS is guest memory plus explicit address overhead, bounded by both the native maximum and inherited hard limit. Affinity bounds simultaneously usable logical CPUs rather than average CPU quota, AS is virtual address space rather than RSS, and NPROC accounting follows kernel UID/ucount semantics rather than a per-task cgroup. `qualify-native-resource.py` verifies locked limits, actual thread exhaustion, affinity and expected-parent binding. Full target-kernel VM and broker tests remain mandatory before using this candidate in a permanent composition.

Task directories contain independent asset copies. After the VM stops, only the bounded rehashed export archive can become a checkpoint; cancellation or lease loss must not publish a new checkpoint. The parent alone restores its private staging directory permissions during cleanup. The native launcher checks its supplied expected creator PID before and after setting irreversible parent-death SIGKILL, and checks it again after namespace/chroot setup. A launcher adopted during creator death refuses to start QEMU.

`qualify-launcher.py` and `qualify-isolated-qemu.py` are synthetic, offline fixtures. The base guest's fixed counter/ENOSPC/memory programs are qualification data, not an execution API. Real working protocol, checkpoint, cancellation and document acceptance tests live in the full development integration workspace's `tests/test_codex_qemu_*.py`; those cross-domain tests are not part of this independent default-disabled source package. They require an explicitly prepared disposable fixture. Docker Desktop tests cannot stand in for actual HA AppArmor/cgroup qualification.

Production installation still needs a separately reviewed service composition, selected runtime digest, private persistent state, independent backup, recovery, and full Controller-to-artifact acceptance. Copying this library into the metadata image does not enable any of those capabilities.

`stage_execution.py` accepts optional `--repository-policy`, and the paired
`--registry-build-policy` / `--registry-base` inputs. These are closed nonsecret
image data: the real boot parsers check schema, fixed service UID, registered test
runtime, exact base OCI graph and, when both policies are supplied, repository,
project/owner and target/permission consistency. Private registration, tokens,
session files and signing keys are rejected as policy input and are never read.
Policy/base aliases, changed source bytes and missing base graphs stop staging
before it can publish a complete receipt.

The Registry build option copies exactly six public Registry Python modules from
their canonical sibling source into the image's nested namespace package. The
staging manifest records every copied dependency, policy and base hash; the
generated Docker ignore lists individual files so a later registration/key cannot
enter the context. Default metadata-only packaging has no optional Registry
dependency and remains disabled. Software packaging also installs Git; the real
image's `/usr/bin/git` bytes must match the repository policy before activation.
Optional `--repository-git-policy` adds only the closed nonsecret HTTPS identity
policy, with exactly the same repository policies, service UID and Git digest as
`--repository-policy`. It contains usernames and forbidden token fingerprints;
private read/publish tokens and their root registration remain runtime-only.
Debian package versions remain repository-selected, so a pinned base alone does
not make this a byte-reproducible build.

These packaging checks do not register a workspace, authorize Git publication,
approve a Registry listener, initialize Passkey delegation or qualify an HA VM.
The final image and complete live chain still require their own acceptance.

Delivery's service Dockerfile pins both architecture bases directly and selects
them with Docker's `TARGETARCH`. There is no `build.yaml`: Supervisor's rejected
digest-form `build_from` fallback cannot select Alpine. Its `BUILD_FROM` argument
is deliberately ignored. APT update/install have fixed total ceilings as well
as per-request TLS timeouts and no automatic retry. This fixes packaging only;
the runtime assets and registrations still need their separate qualifications.
