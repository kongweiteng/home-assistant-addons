# Codex Runner Relay

`codex_runner_relay` 是 Runner Center v2 的最小权限 WSS 传输 Add-on。外网 Runner 只发起出站 WSS；Relay 将连接严格绑定到一个 `runner_id`，并通过受认证的 Add-on 内部 HTTP 与 Codex Controller 交换 request/control/heartbeat/status/result。

Relay 不拥有 Runner Registry、凭据摘要、任务 lease、Git、Codex、项目策略或生产部署权限。它没有 Ingress、host network、privileged、Docker socket、主机目录挂载或宿主端口默认映射。

`0.2.4` 在既有 WSS 数据面和受限 `/install/<ticket>` 基础上，将 Controller 返回的 Registry 标签与 policy revision 原样写入摘要固定的 Runner `0.3.4` 安装命令。该 Runner 允许同一登记仓库的 linked worktree 访问受限 Git common metadata，从而可以在 `workspace-write` 沙箱内完成本地提交；Relay 不保存或改写策略字段，不消费 enrollment、不回显失败 ticket，也不拥有 Runner Registry 或安装制品。
