# Codex Runner Relay

`codex_runner_relay` 是 Runner Center v2 的最小权限 WSS 传输 Add-on。外网 Runner 只发起出站 WSS；Relay 将连接严格绑定到一个 `runner_id`，并通过受认证的 Add-on 内部 HTTP 与 Codex Controller 交换 request/control/heartbeat/status/result。

Relay 不拥有 Runner Registry、凭据摘要、任务 lease、Git、Codex、项目策略或生产部署权限。它没有 Ingress、host network、privileged、Docker socket、主机目录挂载或宿主端口默认映射。

`0.2.6` 在既有 WSS 数据面和受限 `/install/<ticket>` 基础上，固定 Relay 到 Controller 使用 IPv4，并处理 Controller 明确拒绝的迟到终态事件。只有 `runner_late_message` 会被视为已经由 Controller 权威消费并向 Runner 返回传输 ACK，避免旧 outbox 事件永久阻塞后续 heartbeat；Controller 的 `recovery_required`、Runner 本地 task/result 证据和 worktree 都不改变，其他 Controller 错误继续失败关闭。
