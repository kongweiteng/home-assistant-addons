# Weixin Gateway 微信网关

Weixin Gateway 是一个最小、独立、可审计的个人微信 iLink 传输层。它承载当前有效的 ClawBot/iLink 身份，只负责长轮询、访问控制、游标、`context_token`、消息去重、媒体加解密和 Codex Controller 异步作业，不包含模型、Shell、插件或 Home Assistant 权限。

## 当前阶段

- 固定参考 Hermes 上游提交 `d0b87dad77944c669b453385bb797d53fa33c4f7` 的 iLink 协议行为，重新实现最小客户端。
- 默认 `poller_enabled=false`，不会访问正式 iLink 长轮询。
- 启动真实 poller 还必须设置精确确认值 `HERMES_POLLER_STOPPED`。
- `0.1.2` 新增新扫码身份的一次性 owner 绑定；绑定前只识别管理员页面生成的高熵绑定码，其他消息不会进入 Codex。
- 重新扫码可能使旧 iLink 凭据失效；也可以继续通过私有迁移包导入仍然有效的既有身份。
- 正式切换前 Hermes 继续作为唯一真实 poller。

## 安全边界

- 不申请 Home Assistant、Supervisor、MQTT、Docker、host network、设备或 `/share` 权限。
- 群聊固定关闭；私聊只允许配置的 owner allowlist。
- 新身份没有 owner 时必须显式启用配对 Poller；绑定码不落明文磁盘、15 分钟过期且只能绑定第一个正确发送者。
- 原始微信 ID、token、同步游标、`context_token` 和媒体明文只保留在 Add-on 私有 `/data`。
- 传给 Controller 的会话标识为域分隔 SHA-256，不包含原始微信 ID。
- 同一 token 由本地文件锁保护；正式切换仍必须人工确认 Hermes poller 已停止。
- 媒体仅允许固定微信 CDN、HTTPS、大小上限、AES-128-ECB 和短期私有 spool；预览与消费都重新校验正文摘要，只有正式消费接口会标记引用已消费。

## 本地验证

```bash
PYTHONPATH=weixin_gateway PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_weixin_gateway
```

配置、身份迁移和回滚说明见 [DOCS.md](DOCS.md)。
