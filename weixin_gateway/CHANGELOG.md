# 更新记录

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
