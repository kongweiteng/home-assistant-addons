# Weixin Gateway 微信网关

Weixin Gateway 是一个最小、独立、可审计的个人微信 iLink 传输层。它可以在同一 Add-on 进程中承载多个 ClawBot/iLink 身份，只负责每身份长轮询、访问控制、游标、`context_token`、消息去重、媒体加解密、Codex Controller 异步作业，以及默认关闭的 owner-only Remote Work 消息适配；不包含模型、Shell、插件或 Home Assistant 权限。

## 当前阶段

- 固定参考 Hermes 上游提交 `d0b87dad77944c669b453385bb797d53fa33c4f7` 的 iLink 协议行为，重新实现最小客户端。
- 默认 `poller_enabled=true`；如果 Gateway 没有身份凭据，会保持页面可用并显示 `credential_missing`，不会伪造轮询成功。
- Ingress 页面可以开启或关闭全部 Poller；页面操作以 SQLite 持久化覆盖保存，重启后不会意外恢复或关闭。
- `0.1.2` 新增新扫码身份的一次性 owner 绑定；绑定前只识别管理员页面生成的高熵绑定码，其他消息不会进入 Codex。
- `0.1.3` 在 iLink 会话过期时同时停止轮询和全部微信出站，保留 Controller 已完成但尚未回传的持久结果，禁止 context 清除后的第二次发送尝试。
- `0.1.4` 内置可选的 MQTT v1 主动通知适配器，直接使用唯一 owner 的当前 iLink 上下文发送固定文本，完全绕过 Codex、模型和 Controller。
- `0.2.0` 在保持一套 iLink 身份和一个 Poller 的前提下增加唯一 owner、多 member、一次性成员邀请码、独立会话/Thread 短标识和管理员 Ingress 用户控制面。
- `0.2.1` 新增默认关闭的 `/work` Remote Work v1：只有精确 active owner 命令进入专用 MQTT，普通聊天仍进入 Controller，member、近似前缀、附件和 `/work deploy` 均失败关闭。
- `0.2.2` 支持 Controller completed job 的安全图片 artifact：先预取并复核 MIME、大小和 SHA-256，再发送一条中文统计摘要和微信原生图片；成功时不发送链接，图片明确失败或状态未知时才发送 HA Ingress 短期下载链接。
- `0.2.3` 将 Ingress 明确划分为全局机器人身份、条件式 Owner 初始化和用户级权限管理；首次扫码不增加确认，已有身份的替换才提示影响，Owner 已绑定后不再显示首次绑定操作。
- `0.3.0` 升级为“一人一个 ClawBot”：每个 Owner/Member 使用独立 iLink 身份、Token 锁、Poller、游标、上下文和发送锁，但继续共享同一 Controller/Codex 与全局单活动 Turn。
- `0.4.0` 新增默认关闭的 Runner Manager v2 确定性路由：精确 owner `/work` 命令直接调用 Controller Runner Manager，不经过模型；member、近似命令、附件和 deploy 继续失败关闭。v2 请求失败不会回退到 v1 或普通聊天，避免双投和重复执行。
- `0.4.1` 新增持久化跨消息装修媒体归档：支持先发图片后说“刚才六张图片全部归档”，也支持先说“接下来六张图片归档到水电施工档案”再逐张发送；取消、15 分钟过期、精确数量、16 个上限、失败重试和重启恢复均由 Gateway 确定性处理。
- `0.4.2` 支持 Controller `file` artifact 的微信原生文件投递；账本 ZIP 与图片共用幂等发送、失败/未知状态和短期回退语义，内部路径不会进入微信正文。
- `0.4.4` 为 Runner Manager v2 增加持久任务跟踪与自动微信回传：start、continue、cancel 成功后会在 Gateway 重启后继续查询状态，自动发送阶段变化和最终结果；相同结果不会重复发送，终态送达后关闭跟踪。
- `0.4.5` 区分陈旧 `context_token` 与真实 iLink 限流：长任务回传遇到 `-2 + unknown error` 时清除该用户旧上下文并用同一 client ID 无 token 重试一次；真实限流改为有界指数退避，避免高频重试。
- `0.4.6` 修复首次 `dispatched` 回复与后台 watch 的并发重复发送：同一 watch 代次复用同一持久出站作业和微信 client ID，只有实际发送成功后才更新通知指纹，失败重试与 continue/cancel 新代次保持可用。
- `0.4.3` 修复多身份页面变量覆盖导致的 `document.createElement is not a function`，并新增不改长期 desired state 的有界维护暂停/恢复接口；维护超时或 Gateway 重启后会按原状态自动恢复。
- 新成员由 Owner 在 Ingress 生成独立二维码和一次性接入码；扫码微信与发送接入码的微信必须一致，绑定消息不会进入 Controller。
- 重新扫码可能使旧 iLink 凭据失效；也可以继续通过私有迁移包导入仍然有效的既有身份。
- 当前新 Gateway 是唯一真实 poller；旧 Hermes iLink 身份已失效且不再是运行依赖，不能作为微信回滚目标。

## 安全边界

- 不申请 Home Assistant、Supervisor、Docker、host network、设备或 `/share` 权限；仅在主动通知显式启用时作为受限 MQTT 客户端连接既有 Broker。
- 群聊固定关闭；私聊只允许与该 ClawBot 一对一绑定的 active owner/member。每份身份文件的 `allowed_user_ids` 只镜像本身份绑定用户；SQLite principal/identity binding 才是权限与路由事实源。
- 新身份没有 owner 时必须显式启用配对 Poller；绑定码不落明文磁盘、15 分钟过期且只能绑定第一个正确发送者。
- 原始微信 ID、token、同步游标、`context_token` 和媒体明文只保留在 Add-on 私有 `/data`；页面和管理 API 不返回账号哈希或完整内部身份标识。
- 传给 Controller 的会话标识为域分隔 SHA-256，不包含原始微信 ID。
- 页面只展示私有 HMAC 生成的 `WX-*`、`CV-*`、`TH-*` 短标识；短标识只用于排障，不参与授权。
- member 必须先完成 Controller `job_capability_profile_v1` 能力协商，只能提交 `member_read_only`；旧 Controller 下成员消息失败关闭，owner 仍按旧契约兼容。
- 同一 token 由独立本地文件锁保护，第二个运行时进入 `token_conflict` 而不影响其他身份；Gateway 不依赖 Hermes 激活确认值。
- 媒体仅允许固定微信 CDN、HTTPS、大小上限、AES-128-ECB 和短期私有 spool；预览与消费都重新校验正文摘要，只有正式消费接口会标记引用已消费。
- 主动通知默认关闭，只允许 `owner` audience；通知正文只存在于 MQTT payload 和进程内存，不写 SQLite、身份文件或日志。
- Remote Work 默认关闭并使用独立 MQTT 账户；请求只包含项目别名、最小任务说明和脱敏 owner hash，不接受 path、Shell、model、sandbox、Git ref、remote 或 reply topic。
- Remote Work SQLite 只保存 task、outbox、状态序号和结果摘要；不保存源码、完整 diff、Codex JSONL、reasoning、系统提示、秘密或完整日志。
- 出站 artifact 使用 additive SQLite 状态和确定性 iLink client ID；重启、重放或发送结果落库前崩溃都复用同一发送键。状态未知不盲目生成新 key 重发图片，只发送一次独立确定性 fallback 链接。

## 主动通知

- 兼容现有 `home/notification/v1/request`、`home/notification/v1/result`、`home/notification/v1/status` 和 `homeassistant/status` 主题。
- 使用 MQTT v5、QoS 1、manual ack、固定 client ID 和 24 小时持久会话；只有最终 result 成功发布后才确认 request。
- 文本固定为 `【通知|警告|紧急】标题` 加正文，不调用模型、不改写内容。
- 支持 `message_id` 幂等、`dedupe_key`、TTL、来源/全局限流、有限重试和 `delivery_state_unknown` 故障语义。
- MQTT Broker host 默认留空，启用时必须显式填写实际 Broker；iLink 发送结果超时属于投递状态未知，不会自动重试。
- 启用前必须确认没有其他 notification consumer 同时消费 request，避免重复回执。
- 无论存在多少 member，主动通知始终解析当前 active owner 的独立身份；owner 转移会原子切换 `active.json` 兼容镜像，member 不进入通知 audience。

## 多身份与多用户管理

- Owner 二维码只用于首次建立 Owner ClawBot，或在 Poller 安全停止后重新认证同一 ClawBot；已有 Owner 时必须由当前 Owner 扫码，不能借机更换账号或人员。
- Owner 在 HA Ingress 为成员生成独立 ClawBot 二维码与 128 bit 高熵一次性接入码；明文只返回一次，私有 SQLite 仅保存随机盐和摘要，默认 15 分钟过期。
- 扫码确认后，新身份只运行 pairing-only Poller。只有扫码用户本人发送正确接入码才会原子建立 Member principal/binding；错误用户、错误码、过期、取消、重复身份和验证码阻断均失败关闭。
- 每身份独立 client、TokenLock、Poller、cursor、context、发送锁和故障状态；相同上游消息 ID 会按身份域分隔，不会互相去重或串回复。
- 每位用户拥有独立 conversation key、`CV-*` 和 Controller Thread；页面不会返回原始微信 ID、完整 conversation key、Thread/job ID 或邀请码历史。
- owner 不可暂停或移除。active member 可暂停、恢复、移除、改名，或在其独立 ClawBot 可用时使用精确确认词 `TRANSFER_OWNER` 原子转为新 owner。
- suspended/revoked 用户的新消息立即拒绝；已提交作业不会盲目取消，但最终微信发送前会复核状态并抑制未发送结果。
- 暂停只停止该成员 Poller；移除、接入取消、接入过期或尝试超限会释放 Token 锁并清理不再允许恢复的成员凭据，不影响其他身份。
- 0.1.x 遗留排队消息会在迁移时固化旧 owner 哈希和权限画像；owner 转移后重新授权只能保持或降低权限，不能让旧消息继承新 owner 权限。
- 暂停/移除、owner 转移、主动通知目标选择和分片出站在同一事件循环中线性化，固定锁顺序为出站锁后授权锁，避免死锁或发送给过期角色。

## Remote Work

- V1 命令固定为 `/work renovation-hub <任务>`、`/work status <task_id>`、`/work continue <task_id> <补充>` 和 `/work cancel <task_id>`；`/work deploy` 固定拒绝。
- 主题固定为 `home/codex-work/v1/request|control|status|result|agent`。前四类 QoS 1、非 retained；Agent 在线状态由 Mac Agent retained + LWT 发布。
- Gateway 账户只 publish request/control、subscribe status/result/agent；不得复用主动通知账户或 superuser。
- status/result 使用 `task_id + run_seq + sequence` 收敛乱序和重投；未知 task、旧序号和同序号不同正文均失败关闭。
- 结果发送前再次核对发起者仍是 active owner；owner 已转移、暂停或撤销时抑制回传。
- `0.3.1` 代码候选不代表真实多人微信已验收；真实二维码、多个手机、长期多 Poller、通知、媒体和 Remote Work 仍需独立 HAOS 发布与真机验收。
- `0.3.2` 为进入 Controller 的消息增加 iLink “正在输入”状态；每用户缓存短期 typing_ticket，处理中续发，最终回复、失败、会话过期和身份停止时清理。
- `0.3.3` 新增受认证的非消费流式附件读取和幂等 ACK；Controller/Hub 失败时引用在 TTL 内保持可重试，旧一次性附件接口继续兼容。
- `0.4.1` 的归档请求按 identity + principal + conversation 隔离，只选择未消费、未过期且未被活动/完成请求占用的附件。附件本身仍不构成归档授权；数量不匹配、取消、过期或模糊指令不会把图片提交给媒体写工具。

Runner Manager v2 使用与 v1 相同的精确命令外形，但由 `runner_manager_v2_enabled` 单独控制并调用 Controller 的确定性 API。启用 v2 时不会同时向 v1 MQTT 发布；Controller 返回错误、超时或契约不匹配时只发送一次有界失败回复，不回退到 v1 或普通 Controller job。`0.4.4` 起，start、continue、cancel 会持久登记 task 和原微信路由，后台自动查询并回传状态变化与终态；Gateway 重启后继续跟踪，相同结果使用确定性指纹最多发送一次。`0.4.5` 起，陈旧单用户上下文会使用同一 client ID 无 token 降级一次，真正的限流按 5 秒起步、最高 1 分钟退避。`0.4.6` 起，首次立即回复与后台 watch 对同一结果复用同一个持久出站键，消除并发窗口中的重复阶段消息。默认关闭时现有 v1、普通聊天、通知、Poller 和多身份链路不变。

## 本地验证

```bash
PYTHONPATH=weixin_gateway PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_weixin_gateway
PYTHONPATH=weixin_gateway PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_weixin_gateway_notification
PYTHONPATH=weixin_gateway PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_weixin_gateway_remote_work
PYTHONPATH=.:weixin_gateway PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_weixin_gateway_remote_work_v2
```

配置、身份迁移和回滚说明见 [DOCS.md](DOCS.md)。
