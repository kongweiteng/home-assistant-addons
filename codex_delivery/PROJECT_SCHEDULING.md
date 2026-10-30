# Single-Runner project turns

This local candidate gives equally ranked Work projects turns on the original
single Runner. It adds no configurable priority, concurrency, resource budget,
Runner, execution authority or external service. It remains subject to the
existing default-disabled execution composition and real qualification gates.

A bucket is `(owner, project)`; repository-free `owner_work` has one separate
bucket per owner. Neither one owner's personal backlog nor a shared repository
merges different projects' turns. Each bucket retains the original
`created_at, task_id` FIFO order. An unavailable head does not hide later work:
attempts interleave one task per bucket, then repeat for remaining tasks.

The last **confirmed original `work.claim` event for this Runner** is the
service fact. A read-only join to the canonical immutable task owner/project
recovers it, verifying the event and command identity/digests. Pending claims,
failed CAS, busy locks, input lookups and qualification checks do not advance a
turn. A process restart reconstructs exactly the same order from the original
Store; there is no memory cursor, extra database or new scheduling event.
Missing or multiple command links, missing task/durability links and mismatched
selected event facts fail closed for new admissions. Running reconciliation
precedes these fairness reads, so corrupt queued metadata cannot prevent stopping
an unknown execution. Idle queues do not read history. The current read-only
history projection has no new index; large-history performance remains a future
measurement, rather than a claimed scheduling latency bound.

Normally the bucket with the oldest last confirmed claim sequence goes first;
unserved buckets use sequence zero, with FIFO heads and owner/project as stable
ties. Waiting promotion has one bounded level, after 300 seconds since the later
of the bucket head's creation and its last confirmed claim. Promoted buckets go
before younger buckets; promoted buckets still rotate by claim sequence, rather
than repeatedly preferring one project's old backlog. A confirmed claim resets
that bucket's waiting origin. Wall-clock movement can change promotion but
cannot fabricate or remove its confirmed turn sequence.

The threshold does not promise a maximum wall-clock wait. A running task,
unavailable runtime/input, pending durability, revoked project or unknown outcome
can delay execution. Running/upload/checkpoint/delivery reconciliation keeps its
existing priority. The same dispatch flock, current authorization, source
archive, revision CAS and lease fencing still decide whether a selected attempt
may execute. Only a confirmed claim counts even if its process subsequently
crashes; recovery handles that claim before new work and never replays it.

Tests use a real disposable SQLite Store and synthetic replica/adapter data.
They verify ordering and persistence locally, not HA fairness under load,
runtime qualification, actual backup or end-to-end delivery.
