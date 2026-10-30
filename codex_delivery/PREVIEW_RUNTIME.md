# Private pixel preview VM candidate

This local candidate is disabled by default. Explicit trusted Work composition,
runtime registration and Controller configuration are required; building these
assets does not enable the preview or change production.

The dedicated `preview-qemu-v1` manifest uses guest protocol4 and explicit
helper isolation/resource profiles. The trusted policy and hashed manifest must
select the same `cgroup-v1` or `native-resource-v1` profile; there is no automatic
fallback. The native profile uses the shared qualified launcher, inherited hard
AS3GiB/NPROC32/two-CPU affinity and `--exec-resource` with an explicit expected
parent PID. Its 2GiB guest plus1GiB QEMU address allowance is bounded by that
inherited AS ceiling. The launcher must pass its actual closed qualification
probe before launch. These are separate local qualification profiles, not proof
of HA/AppArmor or production acceptance. QEMU has no vNIC, monitor, host port, Docker
socket or host mount. Root guest supervisor alone owns `codex-preview` virtio
stdio. Payload UID65532 runs a Python/Node loopback server at fixed 8080; Chromium
UID65531 retains its own sandbox and a fresh private profile; fixed CDP pipe
driver UID65530 accepts only screenshot/click/text/key/scroll/reload. Task code
inherits none of these control FDs. No task HTML/JS/API response or user HA
identity, CSRF, access capability or host target enters the transport.

Initial qualification profile is 2 guest CPUs, 2 GiB RAM, 1 MiB workspace, 128
files and a bounded private tmp area; these are explicit candidate limits, not
promised deployed capacity. The small workspace bounds a graceful final archive.
Model-provided inputs cannot choose another profile, executable, URL or port.
The preview-specific TCG code cache is fixed128MiB; the native hard AS ceiling
remains3GiB. The shared work/browser cache default remains independent.
The outer fixture caps host processes at256. Guest application, Chromium and
driver each have a separate fixed UID with an NPROC ceiling of128; this is not
a claim that guest-wide process count is256. Guest `/tmp` is256 MiB and shared
memory64 MiB. Initial qualification covers small, dependency-complete local
projects, rather than arbitrary dependency installation or project sizes.

`CWP1\n` plus network-order length-prefixed strict JSON binds nonce and monotonic
sequence. One outstanding request, at most4096 frame/actions, 2/s burst4 and
64 MiB decoded pixel/text total. JSON frame ceiling6 MiB and live spool96 MiB
account for base64 expansion. Driver never exposes CDP/evaluate/navigation or
selectors. Pixels are PNG1024x768, <=4MiB, never active content.
Fixed screenshots select Chromium's `optimizeForSpeed` PNG encoding; the host
still enforces the same PNG byte, decoded-pixel and time budgets.
Startup is separately bounded by min(90s, remaining admission time). Each admitted
frame/action has at most5s total; setup, reads and access never extend the original
absolute expiry. Refresh dispatches one native Chromium reload and returns only
`{'state':'render_pending'}` after its CDP acknowledgement. That acknowledgement
is not pixels or task completion. Host, guest supervisor and driver block new
input until an independent `frame()` returns a new PNG. The driver records the
previous main-frame loader, requires a different current main-frame loader with
both `load` and `firstPaint` lifecycle events, captures it and verifies that the
loader did not change during capture. Old-document or foreign-session events
cannot unlock it. The UI may schedule one independent frame after a fixed5s
wait; the wait is outside either request budget. Each request still has at
most5s, and no access or acknowledgement renews the original lease. A known missing compositor surface permits another read-only
screenshot within the same deadline; it never repeats the input event. Any
unknown/late event result closes the session and retains the recovery fuse.
The internal closed `action_ack` message is permitted only for the exact pending
refresh sequence. Protocol3 assets are rejected, even with a correctly hashed
old manifest; work protocol1 and browser protocol2 remain distinct.

Host lifetime is bounded by the original wall-clock lease and monotonic claim
TTL<=1800s. Admission closes at `admission_deadline = deadline - 10s`; this fixed
reserve cannot be changed by HTTP or model input and never extends the lease.
A new session needs more than10s remaining. A watcher closes admission before
stopping. Cancellation, expiry and failed gate require pidfd-confirmed QEMU exit. Unknown actions are
never replayed, remain journaled and fuse recovery even if QEMU is then proven
stopped. Restart cannot adopt or replay a prior process/session. Execution journal
is not a task database; Delivery remains the sole claim/lease authority.

Explicit graceful stop first kills all guest application/browser/driver UIDs,
then exports a bounded verified checkpoint and exits QEMU. Host validates archive
only after confirmed VM exit. A forced stop or uncertain cleanup never claims a
new checkpoint. Actual pidfd stop, guest cleanup, checkpoint and Work acceptance
are separate facts. Receipts preserve the original owner, dual revisions, lease
and epoch, including when pause/cancel/revise retires that assignment.

The trusted persistent worker may inject a separate `stop_gate(PreviewLease)`.
It must return an exact bool and authorize that same original retiring assignment,
owner, both revisions, exact lease and epoch, any allowed pending control, and
the still-unexpired absolute lease. Closing admission or setting cancellation
does not itself revoke this separate checkpoint authority. HTTP and model input
cannot supply it. Without `stop_gate`, export requires the original canonical
admission scope to remain current; canceled or invalid scope cannot export.
At the fixed drain boundary, a valid original scope may stop and export while
the10s reserve remains. Reaching the original wall or monotonic expiry always
forbids a new checkpoint, even if a callback returnsTrue.

Host checks stop authority before requesting export, while waiting for the
terminal result, and before and after archive import. Callback exceptions and
non-bool results make the outcome unknown, reject the checkpoint and retain the
recovery fuse. Holding the request I/O lock already counts as an unknown
in-flight operation, including while the durable pending-journal write is
blocked before the protocol pending flag is set. The sender checks closing,
admission and deadline again immediately before sending; a paused journal write
cannot release a command after stop. Ready/ACK persistence must also complete
inside the original request deadline. An in-flight unknown action or a lost creating-worker lifetime
also forbids export. The terminal protocol must be complete, without trailing or
additional frames, after confirmed VM exit. `checkpoint_directory()` returns
only a confirmed, nonuncertain checkpoint after rehashing it and confirming VM
exit again; a changed directory is rejected. The parent must copy and rehash it
under its canonical Work acceptance gate before accepting the candidate.

The standalone entry point is `QemuPreviewBackend.start(PreviewLease, program,
gate=..., stop_gate=None, worker_lifetime=..., inputs=..., restore=..., interpreter=...,
cancelled=..., now=...)`.
It returns a session with `frame(deadline=None)`, `action(event, deadline=None)`,
`stop(reason='stopped')` and `checkpoint_directory()`. Stop reason is limited to
`stopped`, `pause`, `cancel`, `revise` or `drain`. The canonical
gate must return promptly and verify the current owner, both revisions, exact
lease and lease epoch; it is supplied by the trusted parent composition, never
by task code. Backend journals and retained process/flock handles are execution
evidence, not a second claim authority. Receipts independently report actual
stop, uncertainty and checkpoint confirmation, bound to the original scope.
`admission_expires_at` is the original expiry minus10s, as an explicit+08:00 ISO
timestamp. `checkpoint_authority` is `stop_gate`, `admission_gate` ornull when
no checkpoint was confirmed. `session.deadline` is the original absolute
monotonic deadline; neither session access nor stopping renews it.

Linux parent-death fencing follows the creating thread. A trusted persistent
Coordinator/WorkExecutor worker must create QEMU inside
`with backend.worker_lifetime() as lifetime:` and pass that nonserializable
capability as `worker_lifetime=lifetime`. The worker holds the context for the
entire preview lease and explicitly stops the session before leaving it.
Missing, foreign-backend, cross-process, expired or cross-thread creating
capabilities are rejected. HTTP request threads may call frame/action, while
the backend continually checks that the creating worker is still alive.
Leaving the context with an active session closes it as unknown, stops QEMU and
does not confirm a checkpoint; it cannot turn a premature worker exit into clean
completion. A capability is service memory only and must never enter HTTP or
model input. A per-request thread must not mint a context to create a VM.

Required local acceptance: real VM page/SPA resources and loopback API, PNG,
click/text/key/scroll/reload, UID/profile/sandbox/no-vNIC evidence, repeated input
ID rejection, timeout/unknown no replay, cancellation/TTL stopping, checkpoint
integrity and no leftover VM. Actual HA, mobile and end-to-end Work acceptance
remain separate. Build binaries, browser profiles and VM outputs must stay out
of Git. `.build/` is local staging only and must be excluded from delivery.

Local tests are `test_codex_qemu_preview.PreviewProtocolTests` and
`test_codex_qemu_preview.PreviewDriverTests`; the driver test uses a real Node
process with a synthetic CDP peer and proves a screenshot retry does not repeat
reload. It does not claim a real browser. Real VM tests opt in through
`CODEX_QEMU_PREVIEW_LIVE=1` and fixed preview assets at `/opt/qemu-runtime`.
Docker Desktop lacks HA AppArmor; its explicit test-only
`CODEX_QEMU_PREVIEW_DESKTOP_FIXTURE=1` verifies nonroot/caps/NNP and finite
4GiB/2CPU/256PID outer limits, but cannot qualify HA AppArmor. Production host
boundary validation remains strict. The fixture uses no network, host binds,
Docker socket or real task data. The outer seccomp is unconfined only to match
the reviewed Supervisor setting; the native helper still enforces its own
filter and isolation boundary.

`PreviewRuntime.Dockerfile` requires explicit pinned `TCG_FROM`, `GUEST_FROM` and
`LAUNCHER_FROM` build inputs. It copies `/isolate` from the reviewed launcher and
regenerates the hashed manifest from that exact helper. Native local VM tests
add `CODEX_QEMU_PREVIEW_NATIVE=1`; only their isolated container process lowers
its inherited hard limits and affinity. The Docker Desktop AppArmor limitation
still applies, and this flag is not production configuration.

Local evidence on 2026-10-29 (+08:00): protocol4/native-resource-v1 passed four
real offline VM tests in 244.512 s: separate refresh ACK/new frame, the complete
pixel/input sequence, graceful terminal checkpoint and unknown refresh ACK
stop/recovery fuse. The complete sequence used 1.445 s for a frame, 3.726/2.804 s
for clicks, 3.001 s for text, 2.633 s for Enter, 1.543 s for scroll, 1.109 s for refresh
ACK and 1.045 s for the independent new frame after the fixed 5 s wait. A separate
refresh-only VM measured 1.402 s/4.564 s for its two requests. The synthetic server
changes a fixed visible color marker on each document load, and the new frame
must differ from the prior PNG; its checkpoint also proves a second document
and script request. These are bounded fixture observations, not a latency
guarantee for arbitrary applications.

The protocol4/native-resource-v1 pending-render retirement group passed three
tests with six real VM starts in358.052s. Each pause/cancel/revise case and the
authorized drain first acknowledged a refresh without fetching its new PNG;
the original retiring scope still exported a confirmed checkpoint. Unknown
stop authority and the expired original lease refused export. A final exact-host
source group passed two tests in127.769s: the blocked pending-journal/stop race
retained the unknown fuse, proved VM exit and sent no post-stop command; the
complete pixel/input/refresh sequence then passed again with six distinct PNGs
and a verified terminal checkpoint. Its refresh ACK took0.691s and independent
new frame2.695s, each inside5s. Final protocol/driver/native/browser unit checks
passed41tests. These remain isolated Docker Desktop observations.

That qualification used guest image
`sha256:f4a176ea71e85b6094ab895f8a0751defbfe2096752b7768481a890fdbad0c92`,
runtime image
`sha256:96f37f5fcaf98f4734e2def2a5371b9c91c500cfcf74971fc4e9d0273be3e2c9`
and asset manifest
`sha256:cfaefafc51c360ded712303ca251e841211b7ebee7465eeef7dda527c83497ab`.
The final test image was
`sha256:1194ec49105eee0a30386e20ac0ac3599a116a6eef9662a6995de8f4a5971c95`.
Its exact `qemu_preview.py` SHA-256 was
`a1010f6c30f3e1918c5cec4dcdb4316df8b0dbac9883c96364c804701c5d056a`,
matching the frozen host source including the journal/stop race fence and the
preview128MiB cache implementation.
Diagnostic 20 s variants and serial profiling images are excluded from this
qualification and delivery. Actual HA AppArmor, Controller-to-real-VM and mobile
acceptance remain separate gates.
