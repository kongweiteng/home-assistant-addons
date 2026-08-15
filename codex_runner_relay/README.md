# Codex Runner Relay

`codex_runner_relay` 是 Runner Center v2 的最小权限 WSS 传输 Add-on。外网 Runner 只发起出站 WSS；Relay 将连接严格绑定到一个 `runner_id`，并通过受认证的 Add-on 内部 HTTP 与 Codex Controller 交换 request/control/heartbeat/status/result。

Relay 不拥有 Runner Registry、凭据摘要、任务 lease、Git、Codex、项目策略或生产部署权限。它没有 Ingress、host network、privileged、Docker socket、主机目录挂载或宿主端口默认映射。

`0.2.0` 在既有 WSS 数据面之外增加受限 `/install/<ticket>`。Relay 使用独立内部 bearer 向 Controller 只读核验短期 ticket，返回摘要与大小固定的自包含 Runner `0.3.0` 安装脚本；不保存 ticket、不消费 enrollment、不回显失败 ticket，也不拥有 Runner Registry 或安装制品。
