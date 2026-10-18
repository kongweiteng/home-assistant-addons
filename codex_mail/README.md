# Codex Mail Provider

该 Add-on 为 Codex Controller 通用扩展提供固定 QQ SMTP 发信服务。它不开放宿主端口，不接收模型指定的 SMTP 地址或凭据，只允许管理员预先配置的收件地址。

安装后配置发件 QQ 邮箱、QQ 邮箱生成的 SMTP 授权码、允许收件人和独立扩展 bearer。再将 `codex_controller/examples/extensions/mail/manifest.json` 与 `examples/skills/send-email` 安装到 Controller 私有数据目录，并按 `codex_controller/EXTENSIONS.md` 注册、审核和启用工具。

发送记录保存在 Add-on 私有 SQLite 数据库中。每次写入先登记 Controller 提供的幂等键；结果未知时不会自动重发。`recent_status` 会尝试在 QQ 邮箱的 Sent 文件夹按 `Message-ID` 核对未知结果。

`0.2.0` 支持真正的 MIME 附件：Excel XLSX、PDF、PNG/JPEG/WebP、ZIP、DOCX、PPTX、CSV 和 TXT；单个最多 5 MiB、总计 10 MiB、最多 10 个。模型只提供当前会话内的受控引用；文件由 Controller 校验并以内网认证载荷传入，不开放任意路径或 URL 下载。账本 Excel 从完整导出快照转换，包含汇总和全部明细。发送结果与最近状态保留文件名、类型、大小与 SHA-256，不持久保存文件正文或 Base64。
