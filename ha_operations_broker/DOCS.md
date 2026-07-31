# HA Operations Broker documentation

## Status

Version `0.2.0` is a source-only P6-A canary. It adds a Broker-owned Passkey
authorization root to the P5 read-only preflight service. Do not install it as
a production operations executor. It has no execution endpoint and always
returns `execution_allowed: false`.

## Configuration

- `broker_api_token`: random value of at least 32 characters, stored only in
  Home Assistant private app options.
- `trusted_owner_identity_hashes`: lowercase SHA-256 hashes of exact
  `weixin:user_id` identities. These remain structural P4 evidence only.
- `webauthn_rp_id`: the exact WebAuthn relying-party domain, without scheme,
  port, path, or wildcard.
- `webauthn_allowed_origins`: one or more exact HTTPS origins whose host equals
  the RP ID or is a subdomain of it. Do not include paths, queries, fragments,
  credentials, or wildcards.
- `passkey_enrollment_token`: temporary random value of at least 32 characters.
  Configure it only while enrolling intended HA admin users, then clear it and
  restart the app. It must never be sent through Hermes or Weixin.
- `passkey_challenge_ttl_seconds`: one-time in-memory WebAuthn challenge TTL,
  default 180 and limited to 60-600 seconds.
- `max_passkeys`: maximum independent HA operator credentials, default 8.
- `max_pending_passkey_flows`: in-memory challenge cap, default 100.
- `max_request_bytes`: bounded JSON body size, default 32768.
- `supervisor_timeout_seconds`: read-only Supervisor information timeout.

The app is manual and experimental. Ingress uses internal port `8098`; the port
is not mapped to the host. `panel_admin: true` makes the sidebar entry available
only to Home Assistant admins. Supervisor supplies the authenticated
`X-Remote-User-Id`; the Broker persists only
`sha256("ha-user:" + X-Remote-User-Id)`.

Passkeys require a browser secure context. The configured Home Assistant origin
must use HTTPS and remain stable. Example placeholders:

```yaml
webauthn_rp_id: example.invalid
webauthn_allowed_origins:
  - https://ha.example.invalid
```

Do not copy these placeholders as real configuration.

## Enrollment

1. Generate a random enrollment token outside Hermes and store it in the
   private app options.
2. Start the app and open **HA 操作审批** as the intended HA admin.
3. Enter the enrollment token in the Ingress page and register the platform
   Passkey. Registration requires browser user verification.
4. Repeat for each intended HA admin operator, up to `max_passkeys`.
5. Clear `passkey_enrollment_token` from app options and restart. Existing
   credentials remain in `/data/authorization/passkeys.sqlite3`.

The database directory is mode `0700`, the SQLite file is mode `0600`, and
SQLite uses `journal_mode=DELETE`, `synchronous=FULL`, and transactions. It
stores credential verification material, signature counters, HA user hashes,
immutable proposal summaries, and receipts. It does not store private keys,
biometrics, raw HA/Weixin identities, enrollment tokens, or WebAuthn challenge
state.

## Internal API

All `/v1/*` requests require
`Authorization: Bearer <broker_api_token>` and JSON request-size limits.

### `POST /v1/preflight`

Accepts the existing P4 proposal/approval envelope, performs only fixed
Supervisor GET observations, and returns structural assurance with execution
disabled.

### `POST /v1/authorization/requests`

Accepts the same P4 envelope. The Broker revalidates the immutable proposal,
code-owned risk, backup requirement, owner hash, state, hashes, and TTL, then
creates an opaque `approval_id`. A repeated action ID with the same proposal
hash is idempotent; a different proposal hash is rejected.

The returned request includes only the bounded, secret-rejected proposal fields
needed for human review. It cannot approve or execute an operation.

### `GET /v1/authorization/requests/<approval_id>`

Returns the request state and, after a successful Passkey assertion, the local
`passkey_verified` receipt. The receipt binds the action ID, proposal hash,
hashed HA user, hashed credential ID, authorization time, and expiry. It always
contains `execution_allowed: false`.

## Ingress API

- `GET /api/context?approval_id=<opaque-id>` returns a review document only to
  an authenticated HA Ingress user. Its optional receipt summary exposes only
  the opaque receipt ID, authorization time, and assurance; HA user hashes,
  credential hashes, and the full internal receipt remain on the bearer API.
- `POST /api/passkeys/register/begin|complete` requires the authenticated HA
  user, an exact configured Origin header, and the private enrollment token.
- `POST /api/approvals/<approval_id>/begin|complete` requires the authenticated
  HA user, exact origin, an enrolled credential for that user, user verification,
  and a valid one-time challenge.

The page displays the action ID, type, exact logical target, risk, backup
requirement, parameter summary, expected change, validation, rollback, and
expiry before requesting a Passkey assertion. Challenges are stored only in
memory and are bound server-side to the approval ID, proposal hash, HA user
hash, and expiry. Restart, timeout, user mismatch, origin/RP mismatch, invalid
signature, counter rollback, or replay fails closed.

## Dependency integrity

The image downloads `fido2 2.2.1` as a fixed wheel and verifies SHA-256
`ed397da981b9ab133da6ead7309e41f924b566b749956129efe286fae097749f`
before installation. Debian's packaged `python3-cryptography` supplies the
cryptographic backend. No WebAuthn signature algorithm is implemented locally.

## Current limitations

- Hermes is not yet connected to the authorization-request API.
- The app has not been installed or browser-tested on a real HAOS Ingress
  origin.
- Passkey receipts are not consumed because no write executor exists.
- Supervisor remains `default`; manager, backup, Core, HACS, and cleanup writes
  remain outside the source.

## Rollback

Because `0.2.0` is not a production executor, source rollback means keeping the
app uninstalled or stopped. A future authorized canary can be rolled back by
stopping/uninstalling it and removing its private options. Deleting the private
authorization database removes enrolled Passkeys and receipts and must be a
separately authorized production-data action.
