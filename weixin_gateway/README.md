# Weixin Gateway 微信网关

Weixin Gateway 是一个最小、独立、可审计的个人微信 iLink 传输层。它承载当前有效的 ClawBot/iLink 身份，只负责长轮询、访问控制、游标、`context_token`、消息去重、媒体加解密、Codex Controller 异步作业，以及默认关闭的 owner-only Remote Work 消息适配；不包含模型、Shell、插件或 Home Assistant 权限。

## 当前阶段

- 固定参考 Hermes 上游提交 `d0b87dad77944c669b453385bb797d53fa33c4f7` 的 iLink 协议行为，重新实现最小客户端。
- 默认 `poller_enabled=false`，不会访问正式 iLink 长轮询。
- 启动真实 poller 还必须设置精确确认值 `HERMES_POLLER_STOPPED`。
- `0.1.2` 新增新扫码身份的一次性 owner 绑定；绑定前只识别管理员页面生成的高熵绑定码，其他消息不会进入 Codex。
- `0.1.3` 在 iLink 会话过期时同时停止轮询和全部微信出站，保留 Controller 已完成但尚未回传的持久结果，禁止 context 清除后的第二次发送尝试。
- `0.1.4` 内置可选的 MQTT v1 主动通知适配器，直接使用唯一 owner 的当前 iLink 上下文发送固定文本，完全绕过 Codex、模型和 Controller。
- `0.2.0` 在保持一套 iLink 身份和一个 Poller 的前提下增加唯一 owner、多 member、一次性成员邀请码、独立会话/Thread 短标识和管理员 Ingress 用户控制面。
- `0.2.1` 新增默认关闭的 `/work` Remote Work v1：只有精确 active owner 命令进入专用 MQTT，普通聊天仍进入 Controller，member、近似前缀、附件和 `/work deploy` 均失败关闭。
- `0.2.2` 支持 Controller completed job 的安全图片 artifact：先预取并复核 MIME、大小和 SHA-256，再发送一条中文统计摘要和微信原生图片；成功时不发送链接，图片明确失败或状态未知时才发送 HA Ingress 短期下载链接。
- 重新扫码可能使旧 iLink 凭据失效；也可以继续通过私有迁移包导入仍然有效的既有身份。
- 当前新 Gateway 是唯一真实 poller；旧 Hermes iLink 身份已失效，不能作为微信回滚目标。

## 安全边界

- 不申请 Home Assistant、Supervisor、Docker、host network、设备或 `/share` 权限；仅在主动通知显式启用时作为受限 MQTT 客户端连接既有 Broker。
- 群聊固定关闭；私聊只允许私有用户目录中的 active owner/member。身份文件的 `allowed_user_ids` 始终只镜像唯一 owner，不能用来添加成员。
- 新身份没有 owner 时必须显式启用配对 Poller；绑定码不落明文磁盘、15 分钟过期且只能绑定第一个正确发送者。
- 原始微信 ID、token、同步游标、`context_token` 和媒体明文只保留在 Add-on 私有 `/data`。
- 传给 Controller 的会话标识为域分隔 SHA-256，不包含原始微信 ID。
- 页面只展示私有 HMAC 生成的 `WX-*`、`CV-*`、`TH-*` 短标识；短标识只用于排障，不参与授权。
- member 必须先完成 Controller `job_capability_profile_v1` 能力协商，只能提交 `member_read_only`；旧 Controller 下成员消息失败关闭，owner 仍按旧契约兼容。
- 同一 token 由本地文件锁保护；正式切换仍必须人工确认 Hermes poller 已停止。
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
- 启用前必须先停止旧 Hermes notification bridge，避免两个不同 client ID 同时消费 request。
- 无论存在多少 member，主动通知始终只读取唯一 active owner；owner 转移会同步更新身份兼容镜像，member 不进入通知 audience。

## 多用户管理

- 管理员在 HA Ingress 生成 128 bit 高熵成员邀请码；明文只返回一次，私有 SQLite 仅保存随机盐和摘要，默认 15 分钟过期。
- 两个用户并发领取同一邀请码时最多一人成功；领取消息不会进入 Codex。
- 每位用户拥有独立 conversation key、`CV-*` 和 Controller Thread；页面不会返回原始微信 ID、完整 conversation key、Thread/job ID 或邀请码历史。
- owner 不可暂停或移除。active member 可暂停、恢复、移除、改名，或使用精确确认词 `TRANSFER_OWNER` 原子转为新 owner。
- suspended/revoked 用户的新消息立即拒绝；已提交作业不会盲目取消，但最终微信发送前会复核状态并抑制未发送结果。
- 0.1.x 遗留排队消息会在迁移时固化旧 owner 哈希和权限画像；owner 转移后重新授权只能保持或降低权限，不能让旧消息继承新 owner 权限。
- 暂停/移除、owner 转移、主动通知目标选择和分片出站在同一事件循环中线性化，固定锁顺序为出站锁后授权锁，避免死锁或发送给过期角色。

## Remote Work

- V1 命令固定为 `/work renovation-hub <任务>`、`/work status <task_id>`、`/work continue <task_id> <补充>` 和 `/work cancel <task_id>`；`/work deploy` 固定拒绝。
- 主题固定为 `home/codex-work/v1/request|control|status|result|agent`。前四类 QoS 1、非 retained；Agent 在线状态由 Mac Agent retained + LWT 发布。
- Gateway 账户只 publish request/control、subscribe status/result/agent；不得复用主动通知账户或 superuser。
- status/result 使用 `task_id + run_seq + sequence` 收敛乱序和重投；未知 task、旧序号和同序号不同正文均失败关闭。
- 结果发送前再次核对发起者仍是 active owner；owner 已转移、暂停或撤销时抑制回传。
- 此版本只提供默认关闭的本地代码候选，不连接真实 EMQX、不安装 Mac Agent、不发送真实微信，也不授权 HAOS、生产数据或部署动作。

## 本地验证

```bash
PYTHONPATH=weixin_gateway PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_weixin_gateway
PYTHONPATH=weixin_gateway PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_weixin_gateway_notification
PYTHONPATH=weixin_gateway PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_weixin_gateway_remote_work
```

配置、身份迁移和回滚说明见 [DOCS.md](DOCS.md)。
