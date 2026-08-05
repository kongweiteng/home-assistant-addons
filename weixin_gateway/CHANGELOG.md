# 更新记录

## 0.2.2

- completed job 支持安全 `result_summary + artifacts[]`：Gateway 先通过 Controller bearer 预取并复核 PNG MIME、长度、大小和 SHA-256，再发送中文摘要和微信原生图片。
- `IlinkClient.send_media()` 接受调用方提供的确定性 client ID；新增 additive `outbound_artifacts` 状态，重启和重放复用同一媒体发送键，状态未知不盲目重发图片。
- 图片成功时不发送下载链接；预取、上传或发送失败时才使用独立确定性发送键发送 Controller HA Ingress 短期链接，最终发送状态未知使用明确中文提示。
- 新增私有 `controller_ingress_base_url` password option，严格限制为无凭据、query 和 fragment 的 HTTPS 基址；空值不会伪造链接。
- 摘要、图片和 fallback 统一沿用出站锁、授权锁、用户状态、owner 变化和 session-expired 门禁；新增成功、已知/未知失败、抑制、重启重放与 Hub→Controller→Gateway 合成测试。

## 0.2.1

- 新增默认关闭的 owner-only `/work` Remote Work v1；精确 start/status/continue/cancel 与普通 Controller 聊天确定性分流，member、近似前缀、附件、未知项目和 deploy 均失败关闭。
- 新增专用 `home/codex-work/v1/request|control|status|result|agent` MQTT v5 适配器，QoS 1、24 小时持久会话、入站持久化后 manual ack；配置和账户与主动通知隔离。
- 新增 additive task/outbox/event/agent SQLite 状态，覆盖 TTL、幂等正文冲突、乱序序号、终态回退、结果待发送和 owner 变化后抑制回传。
- 结果契约只允许摘要、分支、commit、测试摘要、变更路径数量、下一步和错误码，拒绝源码、完整 diff、raw JSONL、reasoning、提示词和完整日志。
- 本候选不连接真实 EMQX、不安装 Mac Agent/LaunchAgent、不发送真实微信，也不执行 HAOS、生产数据、部署或不可逆动作。

## 0.2.0

- 在一套 iLink 身份和一个 Poller 下新增唯一 owner、多 member 私有用户目录；每位用户拥有独立 conversation 和 Codex Thread 关联。
- 新增 128 bit 高熵一次性成员邀请码，磁盘只保存带盐摘要，覆盖过期、取消、并发领取和重放拒绝；领取消息不进入 Codex。
- 新增别名、暂停、恢复、移除和精确确认的原子 owner 转移；身份 `allowed_user_ids` 始终只镜像当前 owner，旧版回退不会把 member 误当通知接收人。
- Gateway 提交 additive `capability_profile`；member 固定为 `member_read_only`，只有 Controller capabilities 握手支持时才提交，旧 Controller 下失败关闭。
- suspended/revoked 用户的新消息立即拒绝；已提交结果在微信发送前再次复核状态并可记录 `reply_suppressed_user_inactive`。
- 迁移时为 0.1.x 遗留排队消息回填旧 owner 哈希和权限画像；owner 转移后重新授权只允许权限不变或降级，禁止旧消息继承新 owner 权限。
- 暂停/移除、owner 转移、主动通知目标选择和多分片发送在同一事件循环线性化，并固定“出站锁 -> 授权锁”顺序，避免死锁和过期角色收件。
- 新增 HMAC `WX-*`、`CV-*`、`TH-*` 会话排障标识，以及 JSON + CSRF + revision + request_id 的管理员 API 和完整中文 Ingress 交互。
- 新增 additive SQLite 用户、邀请、会话、管理幂等 schema；0/1/>1 旧 allowlist 分别不猜 owner、确定迁移、以 `owner_migration_ambiguous` 阻断。
- 扫码或导入切换到不同 iLink account 时失败关闭旧待处理消息并清空旧 principal 目录，避免新身份继承旧 owner/member。

## 0.1.4

- 新增默认关闭的 MQTT v1 主动通知适配器，兼容现有 request/result/status/HA birth 主题，直接经当前唯一 owner 的 iLink 上下文发送，不调用 Codex、Controller 或模型。
- 使用 MQTT v5、QoS 1、manual ack、固定 client ID、24 小时持久会话和 retained status/Discovery；只有最终非 retained result 成功发布后才 PUBACK。
- 新增只保存路由元数据的私有 SQLite 台账，覆盖幂等、业务去重、TTL、限流、重试、断线恢复和 `delivery_state_unknown`，不保存通知正文、MQTT 凭据或微信身份。
- 普通回复、owner 绑定确认和主动通知共用唯一异步微信出站锁；无 owner、多 owner、上下文缺失和 session expired 均失败关闭。

## 0.1.3

- iLink 返回会话过期后，Gateway 同时停止轮询和所有 Controller 结果微信出站，不再清除 context 后进行第二次发送。
- 已完成但尚未回传的 Controller 作业继续保持持久待回复状态；修复身份并恢复 Gateway 后仍使用原作业和确定性 client ID，不在会话失效期间重复发送。

## 0.1.2

- 新增新扫码身份的一次性 owner 绑定流程；绑定码仅在管理员 Ingress 返回一次，磁盘只保存带盐 SHA-256 和过期时间。
- 未绑定身份只能运行在 `pairing` 状态，普通消息、图片和错误绑定码均不会进入 Controller；正确绑定消息也不会作为 Codex 请求提交。
- 绑定成功后原子写入唯一 owner allowlist 和当次 `context_token`，随后自动进入正常 `polling`；已有 owner 的身份不能重复绑定。
- 真实 Poller 运行时禁止重新扫码或导入其他身份，避免在线替换凭据。

## 0.1.1

- 新增受认证的 `/internal/v1/attachments/<ref>/preview` 非消费预览接口，供 Controller 构造官方 Codex `localImage` 输入。
- 预览与正式消费共用路径越界、过期、大小和 SHA-256 校验；预览不会设置 `consumed_at`，原引用仍可用于 Ledger 或 Renovation Hub 归档。
- 正式 `/internal/v1/attachments/<ref>` 继续保持一次性消费语义，未扩大网络暴露或权限。

## 0.1.0

- 新增最小 iLink 长轮询、文字发送、游标、`context_token` 和持久消息去重。
- 新增私聊 allowlist、群聊关闭、单 token 文件锁和 Hermes 停止确认门禁。
- 新增 AES-128-ECB 微信媒体、固定 CDN、防 SSRF、短期 spool 和附件引用。
- 新增 `weixin-ilink-identity@1` 加密身份包检查/导入和二维码备用登录。
- 新增 Codex Controller 异步作业与中文 Ingress 状态页。
- 默认关闭真实 poller，未授权任何正式凭据读取、微信切换或 Hermes 停止。
