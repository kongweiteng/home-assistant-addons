# HA Manager Executor 使用说明

## 当前状态

版本 `0.1.1` 是 Manager 权限域的只读 shadow。它只读取 `/addons/<slug>/info`，并把该成功响应中缺失或为空的 `installed` 规范化为 `true`，不调用任何 Supervisor 写端点，也不接受任意 URL、endpoint、参数、配置、Shell、路径或自由 JSON。

## 配置

- `manager_api_token`：至少 32 字符的内部 bearer，只保存在 Add-on 私有 options。
- `restart_addon_allowlist`：允许 shadow 的精确 Add-on slug；默认空。
- `max_request_bytes`：内部 JSON 请求上限。
- `supervisor_timeout_seconds`：固定 Supervisor GET 超时。

端口 `8099` 只用于 Add-on 内部网络，保持宿主映射为空。该 Add-on 没有 Ingress 页面。

## Shadow 接口

`POST /internal/v1/shadow/restart-addon` 只接受：

```json
{
  "version": 1,
  "action_id": "OPS-20260805-A1B2C3D4E5F6",
  "proposal_hash": "sha256:64位小写十六进制",
  "action_type": "restart_addon",
  "target": "example_addon",
  "adapter_version": "manager-restart-v1",
  "adapter_schema_version": 1,
  "baseline_etag": "sha256:64位小写十六进制"
}
```

执行器会重新读取精确 Add-on 信息，白名单化字段并计算 `baseline_etag`。摘要不一致时返回 `baseline_drift`；一致时返回 `mode=shadow`、`execution_allowed=false` 和脱敏证据。任何附加字段、非法 slug、非白名单目标或错误 adapter 都会被拒绝。

`GET /healthz` 不需要 bearer，只返回版本、shadow 模式、写关闭和白名单数量，不返回目标、Token 或 Supervisor 内容。

## 安全边界

- 源码没有 Supervisor POST/PUT/PATCH/DELETE 调用。
- bearer、Supervisor token、原始响应和完整私有配置不会写入日志或响应。
- shadow 成功只表示固定只读观察一致，不代表 Passkey、正式执行或 HAOS 验收完成。
- 后续真实 restart 必须由 Auth Broker 的一次性收据、持久 lease、写前复核和 recovery 联锁控制，并保证只有一个实际执行器。

## 回滚

当前版本没有写副作用。停止或卸载 shadow Add-on 即可回滚其运行组件；不得据此修改 Auth Broker 权限或删除审计数据。正式 HAOS 停止、卸载和权限调整仍需单独授权。
