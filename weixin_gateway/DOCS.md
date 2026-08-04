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
| `notification_bridge_enabled` | 是否启用 MQTT v1 主动通知；默认关闭 |
| `notification_mqtt_host`、`notification_mqtt_port` | 既有 MQTT Broker 地址和端口；host 默认留空，必须显式填写实际 Broker（例如 EMQX），不会假设 `core-mosquitto` |
| `notification_mqtt_username`、`notification_mqtt_password` | 主动通知专用 MQTT 凭据；启用时必填 |
| `notification_mqtt_tls` | 是否使用 MQTT TLS |
| `notification_allowed_audiences` | v1 固定只能为 `owner` |

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
- 任一微信发送或长轮询返回 iLink 会话过期时，Gateway 立即进入 `session_expired`，停止所有微信出站；不会先清除 `context_token` 再尝试第二次发送。Controller 已完成结果保持 `controller_submitted`，等待身份修复后恢复。

## MQTT 主动通知

主动通知不经过 Codex Controller，也不调用任何模型。请求字段保持现有 v1 契约：`version=1`、稳定 `message_id`、带时区 `created_at`、`info|warning|critical`、标题、正文、稳定 `source`/`dedupe_key`、`ttl=30..86400` 和 `audience=owner`。

- request：`home/notification/v1/request`，QoS 1，retain false。
- result：`home/notification/v1/result`，QoS 1，retain false。
- status：`home/notification/v1/status`，QoS 1，retain true。
- HA birth：`homeassistant/status`；收到 `online` 后重新发布 retained Discovery。
- 固定 MQTT client ID 为 `weixin-gateway-notification-v1`，`clean_start=false`，Session Expiry 为 24 小时。
- SQLite 台账只保存 `message_id`、`dedupe_key`、`source`、时间、状态、attempt 和 `error_code`；不保存标题、正文、MQTT 密码、微信 ID、token 或 `context_token`。
- owner 必须精确绑定一个，并且已有当前 `context_token`；无 owner、多 owner 或上下文缺失均失败关闭。
- `sending`/`retrying` 状态下进程中断后不会盲目重发，重投结果为 `failed/delivery_state_unknown`。
- 等待 iLink 发送结果超时也会直接进入 `failed/delivery_state_unknown`，不会自动重试，避免状态不确定时产生重复微信消息。
- 只有微信明确返回限流且确认未发送时才允许有限重试；HTTP 5xx、传输超时和未知运行时异常统一视为投递状态未知。
- iLink session expired 只尝试一次，立即停止后续微信出站并发布失败结果。

切换顺序必须是：保持新适配器关闭安装并重启验证；停止旧 Hermes notification bridge consumer；确认 request 主题只有一个 consumer；再启用新适配器并执行文字、重复、过期、重启、MQTT 断线和 session-expired 真机验收。失败时关闭新适配器，保留当前有效的新 Gateway 身份。

## 回滚

1. 关闭新 Gateway poller，等待当前长轮询退出并释放锁。
2. 核对最后同步游标、待提交消息和待回复作业。
3. 关闭 Controller intake，保留当前新身份、owner、游标、context 和待回复队列；不要启动已失效身份的 Hermes poller。
4. 修复当前 Gateway 身份，或重新扫码并重新绑定；恢复后核对只存在一个 poller，并确认待回复消息没有重复发送。
5. 不删除新 Gateway 私有数据，直到确认没有未回传消息或附件。

真实凭据导入、停止 Hermes、启动新 poller 和微信端到端测试均属于独立 L3 人工闸门。
