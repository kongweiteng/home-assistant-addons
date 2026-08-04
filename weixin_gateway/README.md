# Weixin Gateway 微信网关

Weixin Gateway 是一个最小、独立、可审计的个人微信 iLink 传输层。它保留现有 ClawBot/iLink 身份，只负责长轮询、访问控制、游标、`context_token`、消息去重、媒体加解密和 Codex Controller 异步作业，不包含模型、Shell、插件或 Home Assistant 权限。

## 当前阶段

- 固定参考 Hermes 上游提交 `d0b87dad77944c669b453385bb797d53fa33c4f7` 的 iLink 协议行为，重新实现最小客户端。
- 默认 `poller_enabled=false`，不会访问正式 iLink 长轮询。
- 启动真实 poller 还必须设置精确确认值 `HERMES_POLLER_STOPPED`。
- `0.1.1` 新增受认证、非消费的附件预览接口，供 Controller 在不破坏原引用的情况下把微信图片交给 Codex。
- 正式 Hermes iLink 凭据必须通过私有迁移包或 HAOS 内部迁移助手导入；二维码备用登录可能产生不同身份。
- 正式切换前 Hermes 继续作为唯一真实 poller。

## 安全边界

- 不申请 Home Assistant、Supervisor、MQTT、Docker、host network、设备或 `/share` 权限。
- 群聊固定关闭；私聊只允许配置的 owner allowlist。
- 原始微信 ID、token、同步游标、`context_token` 和媒体明文只保留在 Add-on 私有 `/data`。
- 传给 Controller 的会话标识为域分隔 SHA-256，不包含原始微信 ID。
- 同一 token 由本地文件锁保护；正式切换仍必须人工确认 Hermes poller 已停止。
- 媒体仅允许固定微信 CDN、HTTPS、大小上限、AES-128-ECB 和短期私有 spool；预览与消费都重新校验正文摘要，只有正式消费接口会标记引用已消费。

## 本地验证

```bash
PYTHONPATH=weixin_gateway PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_weixin_gateway
```

配置、身份迁移和回滚说明见 [DOCS.md](DOCS.md)。
