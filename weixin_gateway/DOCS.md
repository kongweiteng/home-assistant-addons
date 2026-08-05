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
| `allowed_user_ids` | 仅用于首次引导和旧版本回退的唯一 owner 镜像；不要在这里添加 member |
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

若扫码或私有导入得到的是不同 `account_id`，Gateway 会失败关闭旧身份尚未提交/未回传的消息，并清空旧身份的用户、邀请和会话关联；新身份必须重新完成 owner 绑定。相同账号的 token 更新保留私有用户目录，但不会从导入 allowlist 扩展 member 权限。

## 新身份 Owner 绑定

1. 保持 `poller_enabled=false` 完成扫码，确认页面显示身份已就绪。
2. 停止旧 Poller 后设置 `owner_pairing_enabled=true`、`poller_enabled=true` 和精确激活确认值。
3. 新 Gateway 进入 `pairing`，普通消息、图片和错误绑定码全部丢弃，不提交 Controller。
4. 在管理员 Ingress 点击“生成一次性绑定码”；明文只在该次响应中显示，磁盘仅保存带盐 SHA-256，15 分钟后失效。
5. owner 在新机器人私聊中原样发送绑定码。Gateway 原子保存唯一 owner ID 和当次 `context_token`，绑定消息本身不会进入 Codex。
6. 页面变为 `owner_pairing=bound`、`poller_state=polling` 后，再执行普通文字和图片验收。

已有 owner 的身份不能再次执行首次绑定；需要增加、替换或移除 owner 时必须走单独的权限变更与重新验收流程。

## 多用户与会话管理

`0.2.0` 仍然只有一套 iLink 机器人身份、一个同步游标和一个 Poller。多用户是同一机器人下的多个私聊用户，不会创建第二个 token 或第二个 Poller。

1. 管理员在 Ingress 的“多用户接入”生成一次性成员邀请码。明文只显示一次，默认 15 分钟过期；页面关闭后不能再次取回明文。
2. 新用户在机器人私聊中原样发送邀请码。Gateway 在普通访问拒绝之前原子领取邀请码，保存独立用户和会话，绑定消息不会提交 Controller。
3. 新成员默认角色为 `member`、状态为 `active`，提交作业时固定携带 `member_read_only`。只有 Controller 受认证 capabilities 接口明确支持 `job_capability_profile_v1` 时才会提交；旧 Controller 下失败关闭。
4. 管理员可在页面修改别名、暂停、恢复或移除 member。唯一 owner 不可暂停或移除，避免失去管理入口。
5. Owner 转移只允许目标为 active member，并要求精确确认词 `TRANSFER_OWNER`。SQLite 角色交换、全局 revision 和旧版身份 owner 镜像在同一管理动作中收口；失败时补偿回原 owner。

成员只允许普通讨论和 Controller 定义的安全装修只读工具，不自动获得账本写入、媒体归档/导出、Operations、HA 管理或主动通知权限。扩大成员权限属于新的权限设计和发布，不通过页面临时放开。

从 0.1.x 升级时，已经排队但尚未提交 Controller 的旧消息会回填当时 owner 的私有哈希和 capability profile。若随后发生 owner 转移，旧 owner 的排队消息重新授权时最多降级为 `member_read_only`，不会自动继承新 owner 权限。

出站发送与权限管理都运行在同一 asyncio loop。暂停/移除、owner 转移、主动通知目标选择和多分片发送统一按“出站锁 -> 授权锁”顺序执行；因此管理动作完成后不会继续启动新的分片或把通知发送给已经失去对应角色的用户。

### 页面短标识

- `WX-*`：微信用户短标识。
- `CV-*`：用户独立 conversation 短标识。
- `TH-*`：Controller 返回的当前 Codex Thread 短标识。

短标识由 Add-on 私有随机密钥通过 HMAC-SHA256 + Base32 截断生成，重启后稳定，只用于排障。页面和管理 API 不返回原始微信 ID、完整 conversation key、Thread/Turn/job ID、context token 或邀请码历史。

### 管理 API 安全

- 读接口：`GET /api/users`、`GET /api/conversations`。
- 写接口：创建/取消邀请码、修改别名、暂停/恢复/移除成员和 owner 转移。
- 所有写请求必须为 JSON，携带同源状态页取得的短期 `X-CSRF-Token`、当前 users revision 和高熵 `request_id`。
- revision 不匹配返回 `revision_conflict`；相同 request_id 与相同正文不重复改变状态，不同正文返回 `idempotency_conflict`。
- 页面由 HA `panel_admin` Ingress 提供，不映射新的宿主端口。

## 消息与媒体

- `getupdates` 默认 35 秒长轮询；正常超时直接续轮询。
- 游标只在消息已持久化后推进；跨重启以 SQLite `message_id` 去重。
- 原始微信 ID 只存在 Gateway 私有身份/SQLite；Controller 收到 `sha256("weixin:" + user_id)` 和角色权限画像。
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
- owner 必须在私有用户表中精确存在一个 active 记录、身份 `allowed_user_ids` 必须只镜像该 owner，并且已有当前 `context_token`；任一不变量不满足均失败关闭。
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

回退到 `0.1.4` 时，新用户/邀请/会话表会被旧版本忽略；身份 `allowed_user_ids` 仍只有当前 owner，因此 member 自动失去入口，主动通知仍保持唯一 owner。回退不会删除 `0.2.0` 私有表，重新升级后可继续核对。
