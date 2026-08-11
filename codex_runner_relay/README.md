# Codex Runner Relay

`codex_runner_relay` 是 Runner Center v2 的最小权限 WSS 传输 Add-on。外网 Runner 只发起出站 WSS；Relay 将连接严格绑定到一个 `runner_id`，并通过受认证的 Add-on 内部 HTTP 与 Codex Controller 交换 request/control/heartbeat/status/result。

Relay 不拥有 Runner Registry、凭据摘要、任务 lease、Git、Codex、项目策略或生产部署权限。它没有 Ingress、host network、privileged、Docker socket、主机目录挂载或宿主端口默认映射。

`0.1.1` 将 Relay 到 Controller 的内部地址固定为真实 HAOS hostname `http://local-codex-controller:8102`。公网 hostname、TLS、NPM、DNS、HAOS 安装和 Runner 连接仍必须按受控发布流程分别验证。
