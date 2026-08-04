# 变更日志

## [0.1.0] - 2026-08-05

### 新增

- 建立独立 Supervisor `manager` 权限域 Add-on 骨架。
- 新增 bearer 认证的固定 `restart_addon` shadow 预检。
- 新增精确 allowlist、adapter/schema、proposal hash 和 baseline etag 校验。
- 只读取白名单化 Add-on 信息；不包含任何 Supervisor 写调用。

### 安全

- 默认白名单为空，无 Ingress 和宿主端口。
- 拒绝附加字段、任意 URL/endpoint、配置、Shell、路径和自由参数。
- 健康与错误响应不回显 Token、目标白名单或原始 Supervisor 内容。
