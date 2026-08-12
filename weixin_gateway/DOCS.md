# Weixin Gateway 使用说明

当前版本：`0.4.1`。

## 配置

| 配置项 | 说明 |
| --- | --- |
| `attachment_api_token` | Ledger/Controller 读取一次性附件的独立 Token，至少 32 个字符 |
| `poller_enabled` | 是否启动真实 iLink 长轮询；默认开启。Ingress 页面可以用持久化覆盖临时关闭或重新开启 |
| `owner_pairing_enabled` | 是否允许新身份进入一次性 owner 绑定状态；默认关闭 |
| `controller_base_url` | Codex Controller 固定内部服务根地址 |
| `controller_api_token` | Gateway 提交和查询作业使用的独立 bearer |
| `controller_ingress_base_url` | 仅用于图片失败链接的 Controller HTTPS Ingress 基址；作为私有 password option 填写，成功发图时不会使用或发送 |
| `account_id`、`ilink_token` | 仅用于首次私有引导；推荐正式迁移使用加密身份包 |
| `allowed_user_ids` | 仅用于首次引导和旧版本回退的唯一 owner 镜像；不要在这里添加 member |
| `max_media_bytes` | 单个解密媒体上限 |
| `max_active_identities` | 允许保留的非 revoked ClawBot 身份上限，默认 5、范围 1～32 |
| `spool_ttl_seconds` | 未消费媒体的最大保留时间 |
| `notification_bridge_enabled` | 是否启用 MQTT v1 主动通知；默认关闭 |
| `notification_mqtt_host`、`notification_mqtt_port` | 既有 MQTT Broker 地址和端口；host 默认留空，必须显式填写实际 Broker（例如 EMQX），不会假设 `core-mosquitto` |
| `notification_mqtt_username`、`notification_mqtt_password` | 主动通知专用 MQTT 凭据；启用时必填 |
| `notification_mqtt_tls` | 是否使用 MQTT TLS |
| `notification_allowed_audiences` | v1 固定只能为 `owner` |
| `remote_work_enabled` | 是否启用 owner-only Remote Work MQTT 适配；默认关闭 |
| `runner_manager_v2_enabled` | 是否把精确 owner `/work` 命令确定性路由到 Controller Runner Manager v2；默认关闭，启用后不再双投 v1 |
| `remote_work_mqtt_host`、`remote_work_mqtt_port` | 专用 Remote Work Broker 地址和端口；不得猜测或复用通知配置 |
| `remote_work_mqtt_username`、`remote_work_mqtt_password` | Gateway Remote Work 专用最小 ACL 凭据；启用时必填 |
| `remote_work_mqtt_tls` | 是否使用 MQTT TLS |
| `remote_work_ttl_seconds` | 新 request/control 的 TTL，60～3600 秒，默认 1800 秒 |

## 多身份与单 Token Poller 门禁

真实启动必须同时满足：

1. Owner 身份、Controller 和本地持久化检查通过；已有 Owner allowlist，或显式进入一次性 Owner 绑定状态。
2. 有效 Poller desired state 为 `enabled`（无页面覆盖时跟随 `poller_enabled` 默认值）。
3. 每个准备启动的身份分别取得 token 哈希对应的本地独占锁；冲突身份进入 `token_conflict`，其他身份继续运行。
4. 页面关闭操作会持久化 `disabled`，页面开启操作会持久化 `enabled`；两者都使用 CSRF、revision 和 request_id。

发布、备份和升级脚本不得调用页面的长期关闭接口。`0.4.1` 提供独立的维护暂停租约：`POST /api/poller/maintenance/pause` 只停止当前进程的 Poller，不修改 SQLite desired state；`resume` 恢复进入维护前的状态。租约最长 30 分钟，超时自动恢复；进程重启后也按原长期 desired state 启动。页面会显示维护暂停截止时间。

本地锁只保护当前 Gateway 进程，不能证明其他 Add-on 或主机没有使用同一 token；正式发布前仍需完成一次外部运行态清点，确认同一身份只有一个有效 Poller。

## 等待回复时的输入状态

进入 Controller 的普通消息会先调用 iLink getconfig 获取该用户的 typing_ticket，随后调用 sendtyping 的 status=1 显示“正在输入”。等待期间每 5 秒续发；最终文字/图片、失败、取消、会话过期、Poller 停止或身份移除时发送 status=2。

绑定确认、/work 立即回复和被访问控制拒绝的消息不进入此状态链。ticket 只保留在当前 Gateway 进程内，错误只记录脱敏错误码，不阻塞最终回复。

## 身份迁移

正式格式为 `weixin-ilink-identity@1`：ZIP 包含 `manifest.json` 和 AES-256-GCM 加密的 `identity.enc`。明文只包含 iLink 账号、token、固定服务地址、自身 ID、allowlist、同步游标和按会话保存的 `context_token`。

- 迁移包只允许放入 `/data/migration`。
- 一次性密钥通过 Ingress 在导入时输入，不写入 Git、日志或普通配置。
- 导入先检查和解密，并在写文件前核对仍是当前 Owner 的同一 `account_id`，再以 `0600` 原子更新私有身份文件；不同账号失败关闭且不留下新身份文件。
- 正式 Hermes 凭据与备份保留到整个迁移验收结束。

重新扫码会生成当前有效的 iLink 机器人身份，并可能使旧凭据失效。扫码只完成机器人认证，不会自动信任任何私聊用户。

已有 Owner 时，Owner 二维码和私有导入都只允许刷新同一 `account_id`。不同账号会返回 `owner_identity_mismatch`，不会清空用户、邀请、会话、待处理消息或现有身份文件；新增成员必须使用成员接入流程。

## 新身份 Owner 绑定

Ingress 将此流程标记为“身份初始化”，而不是普通用户管理。页面不会预先选择一个微信用户；第一个在机器人私聊中发送正确绑定码的用户成为 Owner。绑定完成后首次绑定区域隐藏，当前 Owner 以别名和 `WX-*` 短标识显示，后续更换必须使用用户列表中的 Owner 转移。

1. 使用默认开启的 Poller 完成扫码；没有身份时页面仍可用并显示凭据状态。
2. 若新身份需要首次绑定，设置 `owner_pairing_enabled=true`，然后在页面保持 Poller 开启。
3. 新 Gateway 进入 `pairing`，普通消息、图片和错误绑定码全部丢弃，不提交 Controller。
4. 在管理员 Ingress 点击“生成一次性绑定码”；明文只在该次响应中显示，磁盘仅保存带盐 SHA-256，15 分钟后失效。
5. owner 在新机器人私聊中原样发送绑定码。Gateway 原子保存唯一 owner ID 和当次 `context_token`，绑定消息本身不会进入 Codex。
6. 页面变为 `owner_pairing=bound`、`poller_state=polling` 后，再执行普通文字和图片验收。

已有 owner 的身份不能再次执行首次绑定；需要增加、替换或移除 owner 时必须走单独的权限变更与重新验收流程。

## 一人一个 ClawBot 与会话管理

`0.3.1` 在 `0.3.0` 多身份模型上增加全局 Poller desired-state 持久化和 Ingress 开关。每个 principal 只能有一个 primary ClawBot 绑定；旧共享成员在升级后保留为 `legacy_shared`，直到通过成员接入向导绑定自己的独立 ClawBot。

每个身份独占 `IlinkClient`、TokenLock、Poller、同步游标、context 字典和发送锁。SQLite `identity_bindings` 保存身份与 principal 的一对一关系；入站消息以 `identity_id + upstream_message_id` 域分隔去重，Controller、图片、通知和 Remote Work 结果均使用原身份回传，不允许跨身份 fallback。

1. Owner 在 Ingress 填写成员别名并生成成员接入二维码和一次性接入码。明文接入码只显示一次，默认 15 分钟过期；同一时刻只允许一个进行中的 onboarding。
2. 成员用自己的微信扫描二维码。若微信要求数字验证码，由 Owner 在同一向导提交；`scaned_but_redirect` 只接受微信 allowlist 内的 HTTPS 重定向。
3. 扫码确认后凭据以 `pending_pairing` 状态写入私有 `/data`，只启动 pairing-only Poller。扫码用户必须亲自在新 ClawBot 私聊中原样发送接入码；其他用户或普通消息不会进入 Controller。
4. 正确接入码在一个 `BEGIN IMMEDIATE` 事务中创建或升级 principal、绑定 identity 并激活 Member；绑定消息本身不创建 Codex 作业。错误码达到上限、二维码过期、验证码阻断或取消会撤销 pending identity、停止运行时、释放 Token 锁并清理未完成凭据。
5. 新成员固定为 `member_read_only`。只有 Controller 受认证 capabilities 接口明确支持 `job_capability_profile_v1` 时才会提交；旧 Controller 下失败关闭。
6. 管理员可修改别名、暂停、恢复或移除 Member。暂停只停止该身份 Poller；恢复重新取得同 Token 锁；移除撤销 binding/identity 并删除该成员凭据，不影响其他 ClawBot。
7. Owner 转移只允许目标为 active Member 且其独立身份为 active，并要求精确确认词 `TRANSFER_OWNER`。SQLite 角色交换和 `active.json` Owner 兼容镜像共同收口；镜像失败时补偿回原 Owner。

成员只允许普通讨论和 Controller 定义的安全装修只读工具，不自动获得账本写入、媒体归档/导出、Operations、HA 管理或主动通知权限。扩大成员权限属于新的权限设计和发布，不通过页面临时放开。

从 0.1.x 升级时，已经排队但尚未提交 Controller 的旧消息会回填当时 owner 的私有哈希和 capability profile。若随后发生 owner 转移，旧 owner 的排队消息重新授权时最多降级为 `member_read_only`，不会自动继承新 owner 权限。

出站发送与权限管理都运行在同一 asyncio loop。暂停/移除、owner 转移、主动通知目标选择和多分片发送统一按“出站锁 -> 授权锁”顺序执行；因此管理动作完成后不会继续启动新的分片或把通知发送给已经失去对应角色的用户。

### 页面短标识

- `WX-*`：微信用户短标识。
- `CB-*`：ClawBot 身份短标识。
- `CV-*`：用户独立 conversation 短标识。
- `TH-*`：Controller 返回的当前 Codex Thread 短标识。
- `OB-*`：成员接入会话短标识。

短标识由 Add-on 私有随机密钥通过 HMAC-SHA256 + Base32 截断生成，重启后稳定，只用于排障。页面和管理 API 不返回原始微信 ID、完整 identity/principal/account hash、Token、conversation key、Thread/Turn/job ID、context token、二维码正文或接入码历史。

### 管理 API 安全

- 读接口：`GET /api/status`、`GET /api/users`、`GET /api/conversations`、Owner/成员二维码图片。
- 写接口：Owner 二维码开始/验证码、Owner 首次绑定、成员 onboarding 开始/验证码/取消、修改别名、暂停/恢复/移除成员和 Owner 转移。
- 所有写请求必须为 JSON，携带同源状态页取得的短期 `X-CSRF-Token`、当前 users revision 和高熵 `request_id`。
- revision 不匹配返回 `revision_conflict`；相同 request_id 与相同正文不重复改变状态，不同正文返回 `idempotency_conflict`。
- 页面由 HA `panel_admin` Ingress 提供，不映射新的宿主端口。

## 消息与媒体

- `getupdates` 默认 35 秒长轮询；正常超时直接续轮询。
- 每身份游标只在消息已持久化后推进；跨重启以 `(identity_id, upstream_message_id)` 唯一约束和路由后的 `message_id` 去重。
- 原始微信 ID 只存在 Gateway 私有身份/SQLite；Controller 收到 `sha256("weixin:" + user_id)` 和角色权限画像。
- 入站图片、文件、视频和语音使用固定微信 CDN、大小限制与 AES 解密，生成短期一次性 `attachment_ref`。
- Controller 可通过同一 bearer 调用 `/internal/v1/attachments/<ref>/preview` 非消费读取正文，用于官方 Codex `localImage`；预览后原引用仍可由账本或媒体归档工具消费。
- `/internal/v1/attachments/<ref>` 保持一次性消费语义。新媒体归档使用 `/internal/v1/attachments/<ref>/stream` 非消费流式读取，并在 Renovation Hub 成功后调用 `/internal/v1/attachments/<ref>/ack` 消费引用；流式失败或 Hub 拒绝时，原引用在 TTL 内保持可重试。所有接口都核验文件路径、大小和 SHA-256。
- 出站文本按最多 4000 字符分块，并使用确定性 client ID，重试不会生成新发送键。
- Controller completed job 含 `artifacts[]` 时，Gateway 先用内部 bearer 预取图片，并再次核验 `image/png`、Content-Length、响应 SHA-256、DTO 大小/摘要和 PNG 正文；临时文件写入私有 outbound spool，权限 `0600`，发送或抑制后立即删除。
- 预取完成后在同一“出站锁 -> 授权锁”临界区重新核对用户，再发送一条 `result_summary` 中文摘要，随后调用 iLink 原生图片上传/发送。文本、图片和失败链接分别使用持久确定性 client ID；成功图片不附下载链接。
- 图片预取、上传或发送明确失败时，发送“图片发送失败”短期链接；最终 send 请求超时或传输结果未知时记录 `delivery_state_unknown`，不盲目重发图片，改发“状态暂无法确认”链接。链接只允许由私有 `controller_ingress_base_url` 与 Controller 固定 `/downloads/artifacts/<opaque-token>` 拼接。
- `controller_ingress_base_url` 必须为无 userinfo/query/fragment 的 HTTPS 地址，生产发布前必须在 Add-on options 直接填写真实 Controller Ingress 基址，不得写入 Git、聊天或普通日志。为空时图片失败回退会保持未完成并返回 `artifact_fallback_unconfigured`。
- suspended/revoked、owner 变化或权限画像不匹配时，摘要、图片和链接全部抑制；session expired 时停止所有出站，不把失败链接当作第二条绕过路径。
- 任一微信发送或长轮询返回 iLink 会话过期时，Gateway 立即进入 `session_expired`，停止所有微信出站；不会先清除 `context_token` 再尝试第二次发送。Controller 已完成结果保持 `controller_submitted`，等待身份修复后恢复。

## MQTT 主动通知

主动通知不经过 Codex Controller，也不调用任何模型。请求字段保持现有 v1 契约：`version=1`、稳定 `message_id`、带时区 `created_at`、`info|warning|critical`、标题、正文、稳定 `source`/`dedupe_key`、`ttl=30..86400` 和 `audience=owner`。

- request：`home/notification/v1/request`，QoS 1，retain false。
- result：`home/notification/v1/result`，QoS 1，retain false。
- status：`home/notification/v1/status`，QoS 1，retain true。
- HA birth：`homeassistant/status`；收到 `online` 后重新发布 retained Discovery。
- 固定 MQTT client ID 为 `weixin-gateway-notification-v1`，`clean_start=false`，Session Expiry 为 24 小时。
- SQLite 台账只保存 `message_id`、`dedupe_key`、`source`、时间、状态、attempt 和 `error_code`；不保存标题、正文、MQTT 密码、微信 ID、token 或 `context_token`。
- owner 必须在私有用户表中精确存在一个 active 记录、其 primary identity 必须 active 且身份 `allowed_user_ids` 只镜像该 owner，并且已有当前 `context_token`；任一不变量不满足均失败关闭。
- `sending`/`retrying` 状态下进程中断后不会盲目重发，重投结果为 `failed/delivery_state_unknown`。
- 等待 iLink 发送结果超时也会直接进入 `failed/delivery_state_unknown`，不会自动重试，避免状态不确定时产生重复微信消息。
- 只有微信明确返回限流且确认未发送时才允许有限重试；HTTP 5xx、传输超时和未知运行时异常统一视为投递状态未知。
- iLink session expired 只尝试一次，立即停止后续微信出站并发布失败结果。

切换顺序必须是：保持新适配器关闭安装并重启验证；确认 request 主题只有一个 consumer；再启用新适配器并执行文字、重复、过期、重启、MQTT 断线和 session-expired 真机验收。失败时关闭新适配器，保留当前有效的新 Gateway 身份。

## Remote Work MQTT

Remote Work 不经过 Codex Controller，也不会把 Gateway 变成远程终端。普通聊天保持原链路；只有以下精确 active-owner 命令会被确定性分流：

- `/work renovation-hub <明确开发任务>`
- `/work status <task_id>`
- `/work continue <task_id> <补充要求>`
- `/work cancel <task_id>`

member、`/workx` 等近似前缀、未知项目、缺失参数、附件绑定和 `/work deploy` 均不会创建远程开发任务。V1 request/control 不接受路径、Shell、model、sandbox、Git ref、remote 或自定义 reply topic。

固定 MQTT 主题为：

- `home/codex-work/v1/request`
- `home/codex-work/v1/control`
- `home/codex-work/v1/status`
- `home/codex-work/v1/result`
- `home/codex-work/v1/agent`

request/control/status/result 使用 MQTT v5、QoS 1、非 retained；Gateway 以 24 小时持久会话订阅 status/result/agent，并在把合法事件持久化后才 ACK。Agent 状态由 Mac Agent retained + LWT 发布。Gateway 专用账户只允许 publish request/control 和 subscribe status/result/agent，必须与主动通知账户分开。

SQLite additive 表只保存 task、outbox、状态序号、Agent 摘要和受限结果字段。status/result 按 `task_id + run_seq + sequence` 收敛；旧序号不能覆盖新状态，同序号不同正文返回 conflict。结果正文只允许摘要、分支、commit、测试摘要、变更路径数量、下一步和错误码；源码、完整 diff、raw JSONL、reasoning、系统提示和完整日志被拒绝。

`remote_work_enabled=false` 时普通微信、Controller 和主动通知不受影响。正式启用必须另行完成专用 EMQX 用户/ACL、Mac Agent 安装、LaunchAgent、Gateway 升级、真实微信和睡眠/重启/TTL/断线验收；本地代码开发授权不包含这些动作。

### Runner Manager v2

当 `runner_manager_v2_enabled=true` 时，Gateway 仍只接受上述四种精确 owner 命令，但请求改为通过既有 Controller 内部 bearer 调用 Runner Manager v2。Gateway 不选择 Runner、不保存 Runner 凭据，也不接触 Relay。

- v2 start/status/continue/cancel 使用有界 JSON、稳定 request ID、owner principal hash 和严格响应契约。
- v2 命令不会同时进入 v1 MQTT；Controller 失败、超时、返回非法 DTO 或没有匹配 Runner 时，Gateway 只回复一次脱敏错误，不回退 v1 或普通聊天。
- `runner_manager_v2_enabled=false` 时维持原 v1/普通聊天分流；`remote_work_enabled=false` 与 v2 开关相互独立，但正式迁移时必须保证同一精确命令只有一个执行路径。
- 当前版本只提供本地候选和确定性路由；真实 Relay、Runner、HAOS options 和微信验收属于后续受控发布。

## 回滚

1. 关闭新 Gateway 的 intake 和全部身份 Poller，等待当前长轮询退出并释放所有 Token 锁。
2. 核对最后同步游标、待提交消息和待回复作业。
3. 关闭 Controller intake，保留 Owner 与成员身份、游标、context 和待回复队列；不要启动已失效身份的 Hermes poller。
4. 回退到 `0.2.3` 时，旧版本只读取 `active.json` 指向的当前 Owner 身份；成员身份文件和 additive SQLite 表保留离线，不会由旧版本启动。恢复 `0.3.1` 后再逐身份核对。
5. 修复当前 Owner 身份，或重新认证同一 ClawBot；恢复后核对每个 token 最多一个 Poller，并确认待回复消息没有重复或跨身份发送。
6. 不删除 Gateway 私有数据，直到确认没有未回传消息、附件或需要恢复的成员身份。

从 `0.3.3` 回退到 `0.3.2` 不需要数据库迁移；先停止全部 Poller 并确认没有 Controller 正在读取附件。旧版本会忽略新的非消费流式读取与 ACK 接口，原有一次性附件接口和私有 spool 数据保持兼容。

从 `0.4.0` 回退到 `0.3.3` 前先关闭 `runner_manager_v2_enabled`，确认没有尚未回传的 v2 `/work` 回复。v2 路由不新增 Gateway Runner 凭据或服务器数据；旧版本会忽略新开关，普通聊天、Poller、通知、媒体和 Remote Work v1 数据保持兼容。

从 `0.4.1` 回退到 `0.4.0` 前先确认没有活动维护暂停。`0.4.0` 不识别维护租约 API，但长期 Poller desired state、身份、消息、通知和 Remote Work 状态均兼容；回退后发布脚本必须避免调用持久 `/api/poller/stop` 作为临时维护动作。

真实凭据导入、停止 Hermes、启动新 poller 和微信端到端测试均属于独立 L3 人工闸门。

回退到 `0.2.0` 时，Remote Work additive 表会被旧版本忽略，普通 Controller/通知/多用户链路继续运行；必须先保持 `remote_work_enabled=false`，并确认没有仍可能被 Mac Agent 接收的未过期 request。回退不会删除 task/worktree，状态未知任务保持人工核对。

从 `0.2.2` 回退图片 artifact 链路时，先回退 Controller 到不再产生 `artifacts[]` 的版本，再回退 Gateway 到 `0.2.1`；additive `outbound_artifacts` 表会被旧版本忽略。回退前核对没有待发图片或待发 fallback，保留 Gateway/Controller 私有数据，禁止用旧 Hermes 身份恢复。
