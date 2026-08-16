# Codex Runner Relay

`codex_runner_relay` 是 Runner Center v2 的最小权限 WSS 传输 Add-on。外网 Runner 只发起出站 WSS；Relay 将连接严格绑定到一个 `runner_id`，并通过受认证的 Add-on 内部 HTTP 与 Codex Controller 交换 request/control/heartbeat/status/result。

Relay 不拥有 Runner Registry、凭据摘要、任务 lease、Git、Codex、项目策略或生产部署权限。它没有 Ingress、host network、privileged、Docker socket、主机目录挂载或宿主端口默认映射。

`0.2.5` 在既有 WSS 数据面和受限 `/install/<ticket>` 基础上，强制 Relay 到 Controller 的内部 aiohttp 客户端使用 IPv4。这样即使 HAOS 内部 DNS 同时发布不可用的 IPv6 Add-on 地址，也不会让只监听 IPv4 的 Controller 被误判为不可用。固定 hostname、双 token、WSS、Registry/lease 所有权和 Runner `0.3.4` 安装制品均不改变。
