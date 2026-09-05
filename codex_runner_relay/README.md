# Codex Runner Relay

`0.2.23` 增加 Runner `0.3.24` 安装滚动兼容，保留全部旧版本；图文命令仍通过既有受认证 WSS 和 512 KiB 帧边界，不新增下载入口。

`codex_runner_relay` 是 Runner Center v2 的最小权限 WSS 传输 Add-on。外网 Runner 只发起出站 WSS；Relay 将连接严格绑定到一个 `runner_id`，并通过受认证的 Add-on 内部 HTTP 与 Codex Controller 交换 request/control/heartbeat/status/result。

Relay 不拥有 Runner Registry、凭据摘要、任务 lease、Git、Codex、项目策略或生产部署权限。它没有 Ingress、host network、privileged、Docker socket、主机目录挂载或宿主端口默认映射。

`0.2.22` 在 `0.2.21` 数据面与 ACK 语义完全不变的前提下，将安装 renderer 的精确滚动兼容扩展到 Runner `0.3.23`，并保留已发布 `0.3.22`。Relay 继续原样转发 Desktop host 事件、送达收据、排队消息命令和 revision 字段，不解析其业务内容、不拥有 Desktop 状态，也不放宽任何失败关闭边界；`desktop_revision_conflict` 仍会关闭连接。

`0.2.16` 在既有 WSS 数据面上继续承载原任务接管的固定 `desktop_command` 下行与 `desktop_snapshot`、`desktop_event`、`desktop_receipt` 上行类型，并保留 Controller 明确判定为 `desktop_event_sequence_stale` 的旧 Desktop event 终态 ACK。该例外只绑定 `desktop_event`；冲突、绑定、隐私、摘要和其他 Controller 拒绝仍关闭连接并失败关闭。默认有界消息大小保持 `512 KiB`、每连接速率保持每分钟 `1200` 条。Relay 仍不解析原始 Thread/Turn ID，不拥有 Desktop 状态、权限或收据事实，也不会把 App Socket 暴露到网络。

安装 renderer 精确接受 Runner `0.3.23`，并保留已发布 `0.3.6`、`0.3.11`、`0.3.12`、`0.3.13`、`0.3.14`、`0.3.15`、`0.3.16`、`0.3.17`、`0.3.18`、`0.3.19`、`0.3.20`、`0.3.21` 与 `0.3.22` 的滚动兼容；除此之外的版本仍 fail closed。Relay 到 Controller 的 IPv4、Registry/lease 所有权、ticket/enrollment 不落盘、单 Runner 连接绑定和其他错误失败关闭边界保持不变。
