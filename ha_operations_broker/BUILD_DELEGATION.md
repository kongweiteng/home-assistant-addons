# Project build delegation v1

Status: local implementation contract; disabled until a real existing Passkey ceremony,
qualified immutable runtime policy, and explicit startup composition succeed.

The first image has no Release7. Build authority therefore never reads a release or
calls a release-plan start operation. A separate project delegation freezes owner,
project, exact repository/policy set, canary target/instance/slug, registry namespace,
architecture, complete recipe/base identity, permissions and qualification. It also
freezes an expiry (at most 30 days), build-count, reserved-byte and per-build time
budgets. It cannot deploy, change a repository policy, execute arbitrary build code,
or modify Manager/Supervisor state.

Creation produces an original operation proposal and pending authorization request.
Existing begin/complete Passkey verification, owner binding and signed-counter checks
produce the original authorization receipt. Activation consumes that exact receipt
once and records an original operation execution identity in the same SQLite DB.
A fixture authenticator or a chat approval is never production authorization.

Each confirmed Delivery software.build.claim may reserve once under this delegation.
Broker independently GETs the same-Store input projection: original confirmed claim,
source archive receipt, actual off-machine source readback, source manifest and repo
policy identities. It also GETs the registered Manager target. No request carries
expected evidence or a desired artifact. The count and maximum artifact bytes are
charged conservatively when reserving, including unknown outcomes. A short narrow
build:<request-digest> lease uses the existing lease table and delegation execution
identity; no singleton operations lease is held between builds. The reservation
freezes a nonextendable deadline. A prior lease or recovery-required execution blocks
new reservations; revocation and expiry prevent signing new grants.

Delivery runs the fixed offline OCI assembler once, archives the actual artifact and
its exact builder receipt/request/recipe using the same ObjectArchive and replica
as its WorkStore, and restores every byte from that replica. Its separate read-only
artifact projection recomputes the original OCI evidence, verifies the restored
archive and actual config/layers/graph and exposes only digests/receipt/size. Broker
reads this projection independently, binds it to the original input/reservation and
Manager target, and signs an exact Registry grant with a Broker-only Ed25519 key.
Delivery never receives the private key. Grant ref, repository/version and signed
envelope are immutable and unique; the exact envelope is durably recorded before
returning. HTTP unknowns are reconciled only with GET of the original request/ref.

Delivery registers that envelope, then independently GETs and verifies the exact
same envelope before publishing. A separate reader fetches the actual version and
immutable manifest/config/layers and verifies the graph. BuildObservation binds the
original request, builder evidence, real archive receipt, signed grant and actual
Registry readback. Runtime installation/health/rollback remain separate acceptance.

Only a fresh pipeline invocation executes build/save/reserve/issue/register/publish.
Every recovery and source_qualifier uses original read-only observations; it never
rebuilds, rearchives, extends a reservation, reissues or retries an unknown write.
Missing local receipt, interrupted archive, absent grant or incomplete Registry state
stays unavailable. This deliberately may require operator recovery after a crash.

Schema v8 is explicit and opt-in for this feature, with strict frozen v7 verification,
private SQLite backup, original FK/row preservation and transactional rollback. The
v7 default remains supported. Production migration and startup wiring are separate
from local candidate tests. No remote change is performed by these modules.
