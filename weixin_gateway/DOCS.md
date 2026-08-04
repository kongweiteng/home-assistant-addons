# Weixin Gateway 使用说明

## 配置

| 配置项 | 说明 |
| --- | --- |
| `attachment_api_token` | Ledger/Controller 读取一次性附件的独立 Token，至少 32 个字符 |
| `poller_enabled` | 是否启动真实 iLink 长轮询；默认关闭 |
| `activation_confirmation` | 真实切换时必须精确填写 `HERMES_POLLER_STOPPED` |
| `controller_base_url` | Codex Controller 固定内部服务根地址 |
| `controller_api_token` | Gateway 提交和查询作业使用的独立 bearer |
| `account_id`、`ilink_token` | 仅用于首次私有引导；推荐正式迁移使用加密身份包 |
| `allowed_user_ids` | 允许进入 Controller 的个人私聊 ID；只存于 HA 私有 options 和 Gateway 数据 |
| `max_media_bytes` | 单个解密媒体上限 |
| `spool_ttl_seconds` | 未消费媒体的最大保留时间 |

## 单 poller 门禁

真实启动必须同时满足：

1. 身份、allowlist、Controller 和本地持久化检查通过。
2. `poller_enabled=true`。
3. `activation_confirmation=HERMES_POLLER_STOPPED`。
4. 取得 token 哈希对应的本地独占锁。
5. 维护窗口中人工确认 Hermes 不再轮询同一个 token。

本地锁无法跨 Add-on 或跨主机证明 Hermes 已停止，所以精确人工确认不能省略。

## 身份迁移

正式格式为 `weixin-ilink-identity@1`：ZIP 包含 `manifest.json` 和 AES-256-GCM 加密的 `identity.enc`。明文只包含 iLink 账号、token、固定服务地址、自身 ID、allowlist、同步游标和按会话保存的 `context_token`。

- 迁移包只允许放入 `/data/migration`。
- 一次性密钥通过 Ingress 在导入时输入，不写入 Git、日志或普通配置。
- 导入先检查和解密，再以 `0600` 原子写入私有身份文件；不会自动启动 poller。
- 正式 Hermes 凭据与备份保留到整个迁移验收结束。

如果原身份包无法可靠迁移，可以使用二维码备用登录；这可能生成不同身份，不能自动视为“保留原身份”。

## 消息与媒体

- `getupdates` 默认 35 秒长轮询；正常超时直接续轮询。
- 游标只在消息已持久化后推进；跨重启以 SQLite `message_id` 去重。
- 原始微信 ID 只用于 Gateway allowlist；Controller 收到 `sha256("weixin:" + user_id)`。
- 入站图片、文件、视频和语音使用固定微信 CDN、大小限制与 AES 解密，生成短期一次性 `attachment_ref`。
- Controller 可通过同一 bearer 调用 `/internal/v1/attachments/<ref>/preview` 非消费读取正文，用于官方 Codex `localImage`；预览后原引用仍可由账本或媒体归档工具消费。
- `/internal/v1/attachments/<ref>` 保持一次性消费语义。预览与消费都核验文件路径、大小和 SHA-256；引用已消费或过期后，两种接口都返回不可用。
- 出站文本按最多 4000 字符分块，并使用确定性 client ID，重试不会生成新发送键。

## 回滚

1. 关闭新 Gateway poller，等待当前长轮询退出并释放锁。
2. 核对最后同步游标、待提交消息和待回复作业。
3. 恢复 Hermes poller，确认微信文字、图片、通知和 `context_token` 连续性。
4. 不删除新 Gateway 私有数据，直到确认没有未回传消息或附件。

真实凭据导入、停止 Hermes、启动新 poller 和微信端到端测试均属于独立 L3 人工闸门。
