# Changelog

## 0.2.0

- 支持 Controller 校验后传入的 MIME 文件附件；单个 5 MiB、总计 10 MiB、最多 10 个，校验类型、文件名、大小与 SHA-256。
- 发送和最近状态返回附件元数据，SQLite 仅保存附件元数据与内容摘要，不保存文件或正文；保留旧记录与未知结果防重发。

## 0.1.0

- Add a fixed QQ SMTP/IMAP provider for the Controller extension contract.
- Enforce an administrator-managed recipient allowlist and an isolated bearer.
- Persist idempotency claims before SMTP delivery and prevent automatic replay of unknown results.
- Add read-only recent-send status with Sent-folder reconciliation by `Message-ID`.
