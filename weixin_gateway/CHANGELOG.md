# 更新记录

## 0.1.0

- 新增最小 iLink 长轮询、文字发送、游标、`context_token` 和持久消息去重。
- 新增私聊 allowlist、群聊关闭、单 token 文件锁和 Hermes 停止确认门禁。
- 新增 AES-128-ECB 微信媒体、固定 CDN、防 SSRF、短期 spool 和附件引用。
- 新增 `weixin-ilink-identity@1` 加密身份包检查/导入和二维码备用登录。
- 新增 Codex Controller 异步作业与中文 Ingress 状态页。
- 默认关闭真实 poller，未授权任何正式凭据读取、微信切换或 Hermes 停止。
