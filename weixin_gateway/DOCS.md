# Weixin Gateway 使用说明

## 配置

| 配置项 | 说明 |
| --- | --- |
| `attachment_api_token` | Ledger/Controller 读取一次性附件的独立 Token，至少 32 个字符 |
| `poller_enabled` | 是否启动真实 iLink 长轮询；默认关闭 |
| `owner_pairing_enabled` | 是否允许新身份进入一次性 owner 绑定状态；默认关闭 |
| `activation_confirmation` | 真实切换时必须精确填写 `HERMES_POLLER_STOPPED` |
| `controller_base_url` | Codex Controller 固定内部服务根地址 |
| `controller_api_token` | Gateway 提交和查询作业使用的独立 bearer |
| `account_id`、`ilink_token` | 仅用于首次私有引导；推荐正式迁移使用加密身份包 |
| `allowed_user_ids` | 允许进入 Controller 的个人私聊 ID；只存于 HA 私有 options 和 Gateway 数据 |
| `max_media_bytes` | 单个解密媒体上限 |
| `spool_ttl_seconds` | 未消费媒体的最大保留时间 |

## 单 poller 门禁

真实启动必须同时满足：

1. 身份、Controller 和本地持久化检查通过；已有 allowlist，或显式进入一次性 owner 绑定状态。
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

重新扫码会生成当前有效的 iLink 机器人身份，并可能使旧凭据失效。扫码只完成机器人认证，不会自动信任任何私聊用户。

## 新身份 Owner 绑定

1. 保持 `poller_enabled=false` 完成扫码，确认页面显示身份已就绪。
2. 停止旧 Poller 后设置 `owner_pairing_enabled=true`、`poller_enabled=true` 和精确激活确认值。
3. 新 Gateway 进入 `pairing`，普通消息、图片和错误绑定码全部丢弃，不提交 Controller。
4. 在管理员 Ingress 点击“生成一次性绑定码”；明文只在该次响应中显示，磁盘仅保存带盐 SHA-256，15 分钟后失效。
5. owner 在新机器人私聊中原样发送绑定码。Gateway 原子保存唯一 owner ID 和当次 `context_token`，绑定消息本身不会进入 Codex。
6. 页面变为 `owner_pairing=bound`、`poller_state=polling` 后，再执行普通文字和图片验收。

已有 owner 的身份不能再次执行首次绑定；需要增加、替换或移除 owner 时必须走单独的权限变更与重新验收流程。

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
