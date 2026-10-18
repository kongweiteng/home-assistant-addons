# Changelog

## 0.1.0

- Add a fixed QQ SMTP/IMAP provider for the Controller extension contract.
- Enforce an administrator-managed recipient allowlist and an isolated bearer.
- Persist idempotency claims before SMTP delivery and prevent automatic replay of unknown results.
- Add read-only recent-send status with Sent-folder reconciliation by `Message-ID`.
