# Weixin Gateway 微信网关

Weixin Gateway 是一个最小、独立、可审计的个人微信 iLink 传输层。它承载当前有效的 ClawBot/iLink 身份，只负责长轮询、访问控制、游标、`context_token`、消息去重、媒体加解密和 Codex Controller 异步作业，不包含模型、Shell、插件或 Home Assistant 权限。

## 当前阶段

- 固定参考 Hermes 上游提交 `d0b87dad77944c669b453385bb797d53fa33c4f7` 的 iLink 协议行为，重新实现最小客户端。
- 默认 `poller_enabled=false`，不会访问正式 iLink 长轮询。
- 启动真实 poller 还必须设置精确确认值 `HERMES_POLLER_STOPPED`。
- `0.1.2` 新增新扫码身份的一次性 owner 绑定；绑定前只识别管理员页面生成的高熵绑定码，其他消息不会进入 Codex。
- `0.1.3` 在 iLink 会话过期时同时停止轮询和全部微信出站，保留 Controller 已完成但尚未回传的持久结果，禁止 context 清除后的第二次发送尝试。
- `0.1.4` 内置可选的 MQTT v1 主动通知适配器，直接使用唯一 owner 的当前 iLink 上下文发送固定文本，完全绕过 Codex、模型和 Controller。
- 重新扫码可能使旧 iLink 凭据失效；也可以继续通过私有迁移包导入仍然有效的既有身份。
- 当前新 Gateway 是唯一真实 poller；旧 Hermes iLink 身份已失效，不能作为微信回滚目标。

## 安全边界

- 不申请 Home Assistant、Supervisor、Docker、host network、设备或 `/share` 权限；仅在主动通知显式启用时作为受限 MQTT 客户端连接既有 Broker。
- 群聊固定关闭；私聊只允许配置的 owner allowlist。
- 新身份没有 owner 时必须显式启用配对 Poller；绑定码不落明文磁盘、15 分钟过期且只能绑定第一个正确发送者。
- 原始微信 ID、token、同步游标、`context_token` 和媒体明文只保留在 Add-on 私有 `/data`。
- 传给 Controller 的会话标识为域分隔 SHA-256，不包含原始微信 ID。
- 同一 token 由本地文件锁保护；正式切换仍必须人工确认 Hermes poller 已停止。
- 媒体仅允许固定微信 CDN、HTTPS、大小上限、AES-128-ECB 和短期私有 spool；预览与消费都重新校验正文摘要，只有正式消费接口会标记引用已消费。
- 主动通知默认关闭，只允许 `owner` audience；通知正文只存在于 MQTT payload 和进程内存，不写 SQLite、身份文件或日志。

## 主动通知

- 兼容现有 `home/notification/v1/request`、`home/notification/v1/result`、`home/notification/v1/status` 和 `homeassistant/status` 主题。
- 使用 MQTT v5、QoS 1、manual ack、固定 client ID 和 24 小时持久会话；只有最终 result 成功发布后才确认 request。
- 文本固定为 `【通知|警告|紧急】标题` 加正文，不调用模型、不改写内容。
- 支持 `message_id` 幂等、`dedupe_key`、TTL、来源/全局限流、有限重试和 `delivery_state_unknown` 故障语义。
- MQTT Broker host 默认留空，启用时必须显式填写实际 Broker；iLink 发送结果超时属于投递状态未知，不会自动重试。
- 启用前必须先停止旧 Hermes notification bridge，避免两个不同 client ID 同时消费 request。

## 本地验证

```bash
PYTHONPATH=weixin_gateway PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_weixin_gateway
PYTHONPATH=weixin_gateway PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_weixin_gateway_notification
```

配置、身份迁移和回滚说明见 [DOCS.md](DOCS.md)。
