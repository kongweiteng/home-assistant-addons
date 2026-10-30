# fnOS FileReplica local candidate

`fnos_file_replica.py` is an independent encrypted replica core for the failure
model **primary storage lost while the NAS replica survives**. The core has no
HTTP, OAuth, credential, mount or NAS-discovery implementation. Its separate
`fnos_file_port.py` companion implements a candidate fixed HTTP protocol only;
`fnos_oauth_provider.py` supplies an access-only provider candidate through trusted
session/official-CLI Ports. There is no production OAuth adapter or qualification. No production service
or configuration enables it. `FnosFileReplica()` is disabled and writes nothing;
only trusted code can inject a fixed FilePort, administrator binding and AES key.
`status().qualified` remains false: local fixture success is not NAS qualification.

The existing `ObjectStore`, `NetworkMountObjectStore`, NFS/CIFS fsync requirements
and production settings retain their stronger contracts. This candidate does not
implement or impersonate that stable-write ObjectStore. An internal sentinel
rejects all storage I/O when constructing the shared `EncryptedReplica` codec;
only its pure receipt/`seal`/`decrypt` operations are used.

## Trusted fixed Port

```python
FileBinding(store_ref, namespace_ref, qualification_ref)

FilePort.binding: FileBinding
FilePort.get_exact(ref, *, deadline) -> bytes | None
FilePort.upload_once(ref, envelope, *, deadline) -> UploadResult

UploadResult.CREATED
UploadResult.EXISTING
```

The references are non-secret administrator identifiers. They do not prove a
real endpoint, namespace, ACL, physical failure domain or correct Port behavior.
The Port must be reviewed and qualified independently. Caller/model arguments
cannot choose it, its endpoint, directory, credentials or binding. Each configured
namespace has exactly one trusted writer and one journal across all processes.
No other application or operator writes into its object namespace while active.
The trusted administrator must maintain that condition; a process-local lock
cannot fence arbitrary other NAS clients or a second journal on another machine.

The HTTP file Port candidate has a closed mapping from the `ER-...` object
reference to one basename inside its fixed canonical directory. It has no public
URL/path, list-all, rename, replace, delete, shell or credential-returning method.
`None` means confirmed absence only, never a 403, timeout, incomplete directory
listing, unreadable object or unknown API error.

The installed fnOS Skill documents `file.checkUpload`, multipart `POST /upload`
with `trim-upload-file`, and the authenticated `multiple-download` POST/GET flow.
A production Port may perform at most one checkUpload and at most one upload per
claim, using skip/no-overwrite and zero offset. The precheck may allocate a
placeholder, so **both belong inside the one claimed upload_once call**. No CLI
automatic retry, cached resume, changed destination or rename fallback is allowed.
`skip`/`completed`, HTTP 200 or a CREATED/EXISTING result alone are insufficient.
For reads, mint the temporary download token for exactly one fixed object and
read original bytes with Bearer authentication. Keep tokens inside that Port;
never log URLs containing the temporary token. The HTTP candidate implements
these wire requirements; it does not implement OAuth login/refresh or establish
real-instance qualification.

## Fixed HTTPS protocol candidate

```python
FnosHTTPFilePort(binding, endpoint, directory, credential_provider=None,
    maxbytes=..., max_listing_bytes=..., max_entries=..., ssl_context=None)

credential_provider(*, deadline) -> raw_access_token
```

Endpoint, canonical directory, binding, CA trust and provider are fixed by trusted
construction. Missing provider is disabled. HTTP/userinfo/query/fragment endpoints,
insecure TLS contexts and noncanonical roots are rejected. TLS certificate and
hostname verification stay enabled. No environment proxy, Cookie, remote redirect,
OAuth refresh, session-file access, automatic login or wire retry is used.

`POST /ogh/ac/w` carries Bearer plus a JSON request with `req`, a fresh 16-hex
`reqid` and fixed path. `file.ls` is a bounded array of packet objects; only a last
`result:succ` completes the list. Intermediate packets are accumulated and duplicate
names, JSON keys, nonfinite values, errors and incomplete terminal data fail closed.
The response need not echo req/reqid; if present, they must match. Only absence of
the exact basename from a complete successful list means missing. A target file's
size must be a positive bounded integer and match downloaded bytes exactly.

For upload, one `file.checkUpload` uses the fixed full path, envelope size and
`overwrite:0`. `uploadName` may be the original basename or that basename followed
by `.~#0` through `.~#999999` (no multi-digit leading zero), optionally prefixed by
the identical fixed directory. Positive offsets and arbitrary names/paths fail.
Skip/completed returns EXISTING without issuing upload; the replica core still
requires authenticated full readback. Otherwise `POST /ogh/ac/h/upload` uses
Bearer, multipart `trim-upload-file`, zero Trim-From, Trim-Overwrite 0, Unix-second
Trim-Mtim and URI-encoded Trim-Path. The OAuth proxy uses **no Trim-Token header**.
The observed HTTP200/Content-Length0/no-Content-Type response is accepted only at
this exact upload endpoint as a candidate success; it is never a replica receipt.
Explicit JSON success objects/arrays are also parsed strictly; anything else is
unknown and cannot cause another upload.

Download creates a token with `POST /ogh/ac/h/multiple-download` using Bearer,
`accessToken`, one fixed path and `noZipWhenSingleFile:true`. This endpoint alone
may return JSON as text/plain; its payload still must be a root JSON object with
a valid token. There is no recursive token search or returned URL following.
The encoded token is used only in the fixed same-endpoint GET, still with Bearer.
The response must have the expected complete size and envelope magic; the core
subsequently authenticates the complete AES snapshot. HTML/ZIP/error payloads do
not become file backups.

All requests share the caller's absolute monotonic deadline. After DNS/TCP/TLS
connect, the deadline is rechecked before any HTTP request; a late connect cannot
dispatch. A timer shuts down this exact socket to interrupt slow status, headers
and body. Framing rejects duplicate headers, conflicting length/transfer coding,
compression and over-budget bodies, while allowing correctly terminated bounded
chunked responses. The core's persistent thread fuse supplies the final caller
wait bound for DNS or a stuck credential provider. Client exceptions contain only
stable codes and do not print raw bodies, paths, bearers or temporary tokens.

The local HTTPS fixture uses an ephemeral test certificate and real certificate
verification. It covers the official proxy shapes above, complete multi-packet
listing, no-overwrite/zero-suffix upload, root-token byte download, lost replies,
401/403/redirect rejection, duplicate framing, slow status/headers/body, late
credentials/connect, and real FileReplica unknown/restart/parent-fuse composition.
Those fixtures do not replace the separately recorded real NAS recovery evidence
or qualify a permanent HA OAuth provider, process identity, ACL or deployment.

## Independent settings and access-only OAuth candidate

`FnosFileSettings.from_dict(value)` validates a separate `fnos_file_replica`
administrator section without opening local storage or the network. Omission or
`{"enabled":false}` disables it. Enabled settings require `policy` exactly
`nas-live-primary-loss-v1`, a fixed canonical HTTPS endpoint and `/volN/...`
directory, plus `store_ref`, `namespace_ref`, `qualification_ref`,
`credential_ref` and `key_ref`. Optional `timeout_seconds` defaults to 10 (1–30);
`maxbytes` defaults to 128 MiB (at most 512 MiB). Secret-bearing/unknown fields
are rejected. Neither AES key nor OAuth session is accepted by these settings.

Explicit trusted construction is:

```python
settings.create(state_root, encryption_key=key,
    credential_provider=provider, failure_domain=domain, ssl_context=context)
```

This creates only the candidate and its distinct local
`fnos-file-replica-journal.json`. It neither selects a production backend nor
qualifies it. Trusted composition must reject enabling both mounted and fnOS
backends, verify the selected backend's independent qualification, and inject
the bound credentials/key. There is no fallback, old-journal adoption or
reinterpretation of mounted fsync durability.

The optional OAuth provider exposes:

```python
OAuthBinding(credential_ref, profile, endpoint, uid, device_id, scopes,
             client_id="YJNMPJUGA9")
OAuthAccessSession(endpoint, uid, client_id, device_id, scopes,
                   access_token, expires_at)
SessionLoader.load(deadline=...) -> OAuthAccessSession
OfficialRefreshPort.refresh_once(deadline=...) -> None
FnosOAuthProvider(binding, session_loader=None, refresh_port=None,
                  journal_path=None)(deadline=...) -> access_token
```

No loader means disabled and no journal/file access. There is deliberately no
implicit session path, Keychain access, raw-token export, guessed OAuth endpoint,
automatic login or automatically chosen file-storage downgrade. `OAuthBinding`
pins the exact HTTPS authority, NAS UID, official client ID, OAuth device ID,
profile and exact scope set, which must include file read/write. The loader
returns access-only facts, never the original refresh token. The token/session
dataclasses hide their fields in repr; errors contain stable codes only.

The installed official CLI documents platform secure storage as the default;
`TRIM_CLI_SESSION_STORAGE=file` is for test/CI isolation and `ask-file` requires
human confirmation of a storage downgrade. An isolated probe observed
`<TRIM_CLI_CONFIG_DIR>/profiles/<profile>.json`, 0600, beneath a private 0700 root;
that is a test fact, not a production authorization or a promise across CLI
versions. No loader/runner for that file backend is enabled by this candidate.
Trusted boot must independently qualify its actual session backend and fixed
official CLI executable, profile, UID, environment, endpoint and session lifetime.

The separate `fnos_oauth_runtime.py` provides explicitly gated local adapter
candidates, both disabled unless trusted boot supplies `allow_file_backend=True`:

```python
FileSessionLoader(binding, runtime_root, allow_file_backend=False)
FixedCLIRefreshPort(loader, executable, executable_sha256,
                    allow_file_backend=False)
```

They do not select or authorize production file storage. The loader reads only
the registered root's `profiles/<fixed-profile>.json`; root and profiles are
current-UID 0700 directories and the exact session is a current-UID, regular,
single-link 0600 file, limited to 64 KiB. Opened paths never follow symlinks.
Duplicate/nonfinite/invalid JSON, unexpected session fields, endpoint/TLS/UID/
client/device/scope drift and root/profiles inode replacement fail closed.
Atomic replacement of a valid profile file by the CLI is allowed. The loader
returns access-only facts; it does not expose or copy refreshToken anywhere.

The dedicated runtime tree also receives a streaming audit capped at 256 entries
and four directory levels: all existing directories must be private 0700 and
all data/lock/temp entries private, single-link 0600 regular files no larger than
64 KiB. This covers the observed `profiles/<profile>.refresh.lock` and
`oauth-device-id` without guessing temporary names. It rejects symlinked locks,
hardlinks, special files, executable helper scripts and unrelated state. Trusted
provisioning must keep auxiliary login scripts/status and business data outside
this directory. Concurrent unrelated writers are excluded by qualification.

Refresh executes one fixed native ELF/Mach-O official CLI directly, never the
Skill wrapper or a shell. The binary and every ancestor directory must be root
owned, free of group/world write permission and symlink traversal; the binary
must be single-link, executable, at most 256 MiB, match the exact registered
SHA-256, and have no setuid/setgid/sticky bits. Its inode/hash are checked before
and after the call. The only argv is `--profile <fixed> login --refresh`.
Its environment contains only a fixed system PATH, C UTF-8 locale, Shanghai
timezone and the explicit isolated TRIM session/config-directory settings.
HOME/CODEX_HOME, proxies, bearer values and caller environment are neither
overridden nor inherited. stdin/stdout/stderr go to DEVNULL; no CLI output or
raw exception is returned. No command string or shell is used.

A shared absolute deadline covers preflight, subprocess wait and validation.
The child starts a separate process group; timeout/failure kills that group and
uses a daemon reaper without granting a second wait budget. A local spent fuse
prevents invocation replay after an unknown result, and the enclosing provider
retains the persistent refresh claim. Actual native-subprocess fixtures cover
argv/environment/output isolation, session renewal, failed/no-change renewal,
process-group timeout and post-exit binary drift. Unprivileged fixtures simulate
only root ownership; a real root-owned system binary separately exercises the
unmocked pin/process path. These tests do not validate the actual official CLI's
HA build, certificate trust, file-session compatibility or storage policy.

The private local OAuth journal stores only expected binding and ready/inflight/
fused state. A directory flock stays held across loader and refresh calls, so a
second provider/process cannot bypass an active refresh claim. Tokens do not
enter this journal. An access token valid for more than 60 seconds returns
without refresh. Otherwise one durable inflight claim precedes one official-CLI
refresh; success requires a second independently loaded, valid, differently valued
access token and a durably committed ready journal. Unknown exit, timeout, late
success, session read/persistence failure or bookkeeping failure closes the
provider. A new process sees inflight/fused and cannot retry the old refresh,
even if a new token was actually written. The same process additionally poisons
the canonical journal path. There is no reset/recovery method or 401 retry; an
operator must investigate the exact credential state through an explicit trusted
recovery procedure. An upload is never replayed for credential recovery.

Session/refresh calls rely on the enclosing FileReplica hard wait bound. Each
call rechecks the deadline before refresh dispatch and after trusted work; a late
loader cannot start refresh, and a late refresh cannot release a token or clear
the persistent unknown state. Responsive private local journal storage and a
single trusted session writer remain assumptions. CLI automatic token renewal
by unrelated processes must be excluded by the qualified runtime arrangement.

## AES, receipt and journal

Trusted construction supplies the existing 32-byte AES key and its key reference,
failure domain, private local journal path, fixed Port/binding and total timeout.
No credentials are accepted by FileReplica. The Port receives only a validated
object reference and ciphertext. The key and plaintext remain in the trusted
replica process; guest code does not receive any replica capability.

The API is compatible with the current `Replica` and `ObjectArchive` callers:

```python
replica.save(snapshot) -> ReplicaReceipt
replica.get(receipt) -> Snapshot
replica.reconcile(snapshot) -> ReplicaReceipt
replica.ciphertext_digest(receipt) -> "sha256:..."
replica.status() -> dict
```

The original receipt binds event, plaintext digest, failure domain and exact
object reference. AES-GCM authenticates these fields plus key reference and size.
ObjectArchive separately derives event/scope identity from owner, task, revision,
checkpoint and bytes. Copying another owner's authenticated envelope to a new
object name does not give it that owner's authority. Repeated save reads and
verifies the existing object without generating a new nonce or uploading again.
An existing valid encryption of the same snapshot may use another nonce; compare
authenticated snapshots, not random ciphertext equality.

The pre-existing local parent directory must be canonical, owned by the current
UID and mode 0700. Journal and lock files are regular, single-link 0600 files,
opened without following symlinks. Atomic journal replacement includes file and
directory fsync. The journal records fixed binding, state, exact object reference
and `may_write`; it stores no key, plaintext, bearer, remote path or raw error.
Deleting journal/lock files, binding drift or invalid state does not re-enroll a
writer or repair permissions. Local journal storage must remain responsive.

Before dispatching upload_once, the journal is durably inflight with may_write.
After any normally returned upload or promptly completed exception, the core may
perform one exact full GET within the same deadline. It authenticates and compares
the entire snapshot before confirming. A timeout with a still-running thread
cannot take this immediate reconciliation path. Missing/corrupt/unreadable data
leaves the journal fused; ordinary save/get cannot clear it or write another
object. Restart converts inflight to fused. Explicit reconcile reads only the
original exact object; absence remains unknown and never authorizes another upload.

## Executable deadline and stuck calls

One absolute monotonic deadline covers lock acquisition checks, all Port calls,
crypto validation and final bookkeeping checks. Each potentially blocking Port
call (including binding access) and bounded crypto operation runs in one daemon
thread while holding the separate cross-process worker flock. The parent waits
only the remaining budget; it does not rely on the Port obeying the deadline.

A timeout permanently poisons that journal in the current process. No subsequent
save/get/reconcile/hash, newly constructed replica instance, replacement Port or
replacement thread can bypass this process fuse. A late result is never adopted.
The original thread retains its worker lock until it actually exits. A different
process cannot even start a GET while that lock is retained. Once the old worker
has exited (or its process is gone), a separate trusted process may reconcile the
same recorded object with GET only. It may not use the restart to retry upload.

Python cannot kill the stuck thread or undo server-side work. The thread is daemon
so it cannot hold process shutdown open; local journal filesystem operations and
the trusted Python runtime are assumed responsive. This is a bound on caller wait,
not a promise to forcibly terminate remote NAS work or an unresponsive kernel.

## Recovery and acceptance

`recovery_only=True` supports an explicitly provisioned **new local recovery
directory**. It permits receipt reads and ciphertext hashes, rejects save and
reconcile, and does not adopt an existing unknown writer. ObjectArchive can then
restore into new private directories; DeliveryStore.restore remains recovery-only.
The operator still needs independently retained key, binding and trusted recovery
receipt/descriptor. This core does not discover the latest DB object, recover a
lost key or manufacture a recovery catalog.

Complete readback proves authenticated remote bytes were retrievable at that time.
It does not prove NAS power-loss stable media, atomic server CAS, protected NAS
ACLs, physical independence or persistence across NAS failure. That is a different
failure guarantee from the existing mounted fsync adapter. No invented fsync API
or successful local fake-NAS test supplies those facts.

The local suite covers real AES, full readback, lost replies, restart, exact-only
unknown recovery, client serialization without server CAS, real subprocess flock
exclusion, bounded stuck calls, authority/permissions and complete primary-tree
loss. Its retained local fake NAS is not an independently deployed failure domain.
Real acceptance still requires fixed-endpoint/TLS/OAuth/namespace/ACL verification,
no-overwrite and multi-packet/absence semantics on the actual fnOS version,
loss/timeout/restart behavior, and recovery after losing the primary from the
surviving NAS. No NAS restart, power interruption, new account, permission change
or HA storage registration is authorized or performed by these tests.

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=.:tests \
  python3 -m unittest test_codex_delivery_fnos_file_replica
```
