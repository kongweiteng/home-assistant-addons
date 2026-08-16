# Codex Runner Relay

`codex_runner_relay` 是 Runner Center v2 的最小权限 WSS 传输 Add-on。外网 Runner 只发起出站 WSS；Relay 将连接严格绑定到一个 `runner_id`，并通过受认证的 Add-on 内部 HTTP 与 Codex Controller 交换 request/control/heartbeat/status/result。

Relay 不拥有 Runner Registry、凭据摘要、任务 lease、Git、Codex、项目策略或生产部署权限。它没有 Ingress、host network、privileged、Docker socket、主机目录挂载或宿主端口默认映射。

`0.2.7` 在既有 WSS 数据面和受限 `/install/<ticket>` 基础上，将安装脚本固定版本升级为 Runner `0.3.5`，使 Controller `0.5.8` 的四平台制品可以通过严格版本身份校验。Relay 到 Controller 的 IPv4、迟到终态 ACK、Registry/lease 所有权、ticket 不落盘和其他错误失败关闭边界保持不变。
