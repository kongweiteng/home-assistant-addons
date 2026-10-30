# Explicit Work Runner presence candidate

`work_runner_presence.PresenceMonitor` is a trusted launcher component. It uses
one independent Runner credential/client, one process lock and one private local
journal. It creates no task/lease database and never reads task rows to infer a
stop. Production composition and real-node acceptance remain separate.

Construct it with `client`, `journal_path`, and keyword-only `runner_ref`,
`boot_ref`, `policy_revision`, `policy_digest`, `qualification_digest`, mandatory
`receipt_gate(task_ref, receipt)`, `refresh_seconds` (at most 0.5), and
`io_timeout_seconds` (at most 0.75). The supplied client's total timeout must be
no larger. The receipt gate must return exactly `stop_ref` and `checkpoint_ref`
after checking the original canonical assignment and actual executor journal/
checkpoint. Match the receipt's input revision and original lease epoch; later
control revisions, control_pending and lease expiry after stopping must not
incorrectly prevent retaining valid stopped evidence.

`start(recovery_mode=False)` explicitly opens one session and returns its epoch.
With recovery_mode=True a confirmed recovery session returns its epoch frozen,
without idle/self-check/clean-stop writes or a periodic writer. An already frozen
journal with a known epoch and no unknown session request returns that local epoch
read-only in recovery mode; it does not open/retry a session. Unknown session/epoch
still raises. Returning an epoch never grants execution. Metadata query/retention
services may remain available while this execution boundary stays frozen.
`begin(task_ref)` durably records and obtains the busy acknowledgment before any
program may start. The production wrapper must then refresh/check its own busy
identity and canonical Delivery lease again. `settled(task_ref, receipt)` first
retains the original receipt (strict JSON, at most 60 KiB) in an immutable private
file and the journal, then calls the receipt gate and acknowledges stopped with
its exact session/task/stop/checkpoint evidence. A caller must also retain/pass
the receipt to Coordinator when settled raises; an identity failure never makes
a real process receipt disappear. `frozen` and `qualified` are memory-only reads.
`uncertain(task_ref)` locally freezes an unknown runtime outcome without a remote
write and preserves active task/evidence. The wrapper must call it when execution
throws. After a real receipt, a settled failure must still return/pass that receipt
to Coordinator for archival while presence remains frozen. `identity()` returns only the last short-lived Controller identity snapshot and
can feed CachedWorkRunnerIdentity; it never performs network I/O.

One lock serializes sequences, explicit methods and periodic writes. Before each
write the journal records the exact pending request, sequence and active/receipt
state, using 0600 files, fsync, atomic replace and directory fsync. Only a checked
acknowledgment plus a durable local completion clears pending. Any write result
unknown or local storage/evidence failure freezes the instance and preserves the
pending payload/active task/receipt. Frozen instances issue no further writes,
retry, new sequence or new session. A read failure only removes cached readiness.
The periodic thread reports its own proven state; it cannot manufacture stopped
evidence from an empty task query or a missing process.

`close()` reports stopped without task evidence only when no active task,
unsettled request or freeze remains; a confirmed clean close is persisted before
releasing the lock. Closing with active work freezes without claiming it stopped.
On a new process, only no previous journal or an exact clean-closed journal permits
an explicit new boot. Any unclean session/active/pending/frozen journal remains
frozen. Closed journals and receipts remain immutable historical objects. There
is no reset or recovery API in this version. Recovery needs a separately reviewed
offline tool, actual stop/checkpoint verification, Registry epoch recovery and
Delivery's independent recovery gate; deleting the journal is not recovery.

Registry accepts stopped as a presence observation. Without recovery it exposes
idle availability, while preserving the original stopped/evidence report. Drain
still becomes disabled. A fresh session/self-check is not administrator enable.
Local tests use synthetic receipts/SQLite/loopback HTTP and do not enroll a real
node, establish host isolation or prove production recovery/phone delivery.
