# Codex Runner Relay

`codex_runner_relay` 是 Runner Center v2 的最小权限 WSS 传输 Add-on。外网 Runner 只发起出站 WSS；Relay 将连接严格绑定到一个 `runner_id`，并通过受认证的 Add-on 内部 HTTP 与 Codex Controller 交换 request/control/heartbeat/status/result。

Relay 不拥有 Runner Registry、凭据摘要、任务 lease、Git、Codex、项目策略或生产部署权限。它没有 Ingress、host network、privileged、Docker socket、主机目录挂载或宿主端口默认映射。

`0.2.8` 在既有 WSS 数据面上新增原任务接管的固定 `desktop_command` 下行与 `desktop_snapshot`、`desktop_event`、`desktop_receipt` 上行类型，并将默认有界消息大小提高到 `512 KiB`、每连接速率提高到每分钟 `1200` 条，以承载净化且容量受限的多 Thread 快照和实时事件。Relay 仍不解析原始 Thread/Turn ID，不拥有 Desktop 状态、权限或收据事实，也不会把 App Socket 暴露到网络。

安装脚本固定版本同步升级为 Runner `0.3.6`，与 Controller `0.5.11` 的四平台 manifest 一致。Relay 到 Controller 的 IPv4、Registry/lease 所有权、ticket/enrollment 不落盘、单 Runner 连接绑定和其他错误失败关闭边界保持不变。
