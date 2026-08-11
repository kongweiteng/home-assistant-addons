# Codex Runner Relay 使用说明

## 内部配置

- `controller_base_url`：固定为 `http://local-codex-controller:8102`；旧短主机名、其他端口、HTTPS 和附加路径都会拒绝启动。
- `controller_api_token`：调用 Controller 内部 Runner Relay API 的 bearer。
- `relay_api_token`：Controller 向 Relay 发布 request/control 的 bearer。
- 其余 options 只控制连接数、消息大小、首帧超时、速率和 Controller 超时。

所有 token 至少 32 字符，只保存在 Add-on 私有 options，不进入 URL、日志、状态或页面。

## 网络

- `/v1/runner`：经独立 NPM hostname 暴露的 WebSocket 数据面；token/credential 必须在第一条 JSON 帧发送。
- `/internal/v1/runners/<runner_id>/<request|control>`：仅供 Controller 在 Add-on 内部网络发布。
- `/healthz`：仅返回版本、连接数和容量，不返回 Runner ID、地址或凭据。

宿主端口默认不映射。NPM 应通过 Add-on 内部网络连接 `local-codex-runner-relay:8098`，不得暴露 HA `8123`、MQTT `1883`、SSH、Controller、Relay 内部发布 API或 NPM 管理端口。

## 回滚

先关闭 Controller 的 Relay 配置，确认没有活动 lease 或 `recovery_required`，再停止 Relay。停止 Relay 不删除 Controller Registry、Runner credential 摘要、服务器 Agent、worktree、分支或 Session。
