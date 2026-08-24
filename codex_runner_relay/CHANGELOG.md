# 更新记录

## 0.2.10

- `/install/<ticket>` 当前固定目录升级为正式 Runner `0.3.11`，与 Controller `0.5.15` 的内置 manifest、公开 Release 字节和 SHA-256 一致。
- 为允许先升级 Relay、再升级 Controller，安装 renderer 只接受精确版本集合 `0.3.6` 与 `0.3.11`；其他版本、Codex/Python 漂移、摘要或大小不匹配继续 fail closed。
- WSS、Desktop stale ACK、IPv4 Controller 客户端、ticket/enrollment 不落盘、Registry/lease 所有权、现有 Runner 与网络边界不变。

## 0.2.9

- Controller 已保存更高 Desktop event sequence 并返回 `desktop_event_sequence_stale` 时，Relay 对精确的 `desktop_event` 返回传输 ACK，使恢复期旧 event 可从 Runner outbox 安全删除，不再反复关闭 WSS 连接。
- 新增终态消费例外只绑定 `desktop_event`；`desktop_event_sequence_stale` 不适用于 snapshot/receipt，既有 `runner_late_message` 行为保持兼容，冲突、绑定、隐私、摘要和其他 Controller 拒绝仍失败关闭。Runner 身份、credential、Desktop 原 Thread、Controller 数据和网络边界不变。

## 0.2.8

- 新增 `desktop_command` 下行和 `desktop_snapshot`、`desktop_event`、`desktop_receipt` 上行消息类型，继续按已认证 runner_id 与单一 WSS 连接一对一转发；Relay 不解析或保存原始 App Thread/Turn ID。
- 默认消息上限提高到 `512 KiB`、每连接速率提高到每分钟 `1200` 条，Schema 上限分别为 `1 MiB` 和 `10000`；Runner 端净化、截断、outbox 背压以及 Controller digest/revision/receipt 校验仍是硬门禁。
- `/install/<ticket>` 固定升级到 Runner `0.3.6`，与 Controller `0.5.11` 的内置 manifest 和四平台自包含制品保持一致。Registry、lease、ticket/enrollment 不落盘、IPv4 Controller 客户端和最小权限网络边界不变。

## 0.2.7

- `/install/<ticket>` 固定升级到 Runner `0.3.5`，与 Controller `0.5.8` 的内置 manifest 和四平台自包含制品保持一致。
- WSS、IPv4 Controller 客户端、迟到终态 ACK、ticket/enrollment 不落盘、Registry/lease 所有权和最小权限数据面不变。

## 0.2.6

- Controller 已将 assignment 置为不可覆盖终态并返回 `runner_late_message` 时，Relay 向 Runner 返回对应 event ACK，避免旧 result 永久阻塞 heartbeat 和在线状态。
- Controller 的 `recovery_required`、Runner 本地 task/result 与 worktree 证据保持不变；其他 Controller 拒绝仍关闭连接并失败关闭。

## 0.2.5

- Relay 到 Controller 的内部 aiohttp 客户端固定使用 IPv4，避免 HAOS 容器 DNS 只返回 IPv6 Add-on 地址时把实际 ready 的 IPv4 Controller 误判为 `controller_unavailable`。
- 固定 `local-codex-controller:8102`、双 token、WSS、安装链接、Runner Registry/lease 所有权和最小权限边界不变。

## 0.2.4

- `/install/<ticket>` 固定升级到 Runner `0.3.4`，使同一登记仓库的 linked worktree 可以在 `workspace-write` 下访问受限 Git common metadata 并完成本地提交。
- 不匹配的 Git common dir 仍即时拒绝；WSS、限流、ticket/enrollment 不落盘、Controller 回调和最小权限数据面不变。

## 0.2.3

- `/install/<ticket>` 固定升级到 Runner `0.3.3`，允许新的必填可空 `error_code` 结构化结果 Schema；旧 `0.3.2` 资产不再通过 install-bootstrap 版本校验。
- WSS、限流、ticket/enrollment 不落盘、Controller 回调和最小权限数据面不变。

## 0.2.2

- `/install/<ticket>` 固定升级到 Runner `0.3.2`，兼容接收超时后继续读取 ACK、ping 与任务帧的 WSS 修复。
- 安装脚本继续固定字段、版本、大小和 SHA-256；Relay 不保存 ticket、标签、策略或 enrollment，也不改变 Registry/lease 所有权。

## 0.2.1

- `/install/<ticket>` 将 Controller bootstrap 中的真实 Runner 标签和 policy revision 传给 Runner `0.3.1` 安装器，移除固定标签造成的首次心跳策略拒绝。
- 安装脚本继续固定字段、版本、大小和 SHA-256；Relay 不保存 ticket、标签、策略或 enrollment，也不改变 Registry/lease 所有权。

## 0.2.0

- 新增 `GET /install/<ticket>`，通过独立内部 bearer 向 Controller 非消费式核验短期 enrollment，并返回自包含 Runner `0.3.0` 的摘要固定安装脚本。
- 安装响应强制 `no-store/no-referrer/nosniff`；Relay 不记录 access log、不保存 ticket，失败响应不回显 ticket 或 Controller 内部错误。
- installer 与平台 bundle 同时校验文件大小和 SHA-256；WSS enrollment/auth、单 Runner 连接、限流、内部发布和 Registry/lease 所有权边界不变。

## 0.1.1

- 将 Relay 到 Controller 的内部 URL 固定为真实 HAOS Add-on hostname `http://local-codex-controller:8102`，并同步收紧启动校验和 `NO_PROXY`。
- NPM upstream 明确使用 `local-codex-runner-relay:8098`；WSS 协议、Runner `0.2.0`、凭据、限流和内部 API 均不改变。

## 0.1.0

- 新增出站 WSS Runner 数据面、第一帧 enrollment/auth、单 Runner 连接绑定和受认证内部 request/control 发布。
- 新增 Controller enrollment/auth/heartbeat/status/result 转发、消息大小、连接容量、首帧超时、每连接速率限制和无秘密健康检查。
- 默认无宿主端口、Ingress、host network、privileged、Docker socket 或主机挂载；不包含真实 HAOS/NPM/DNS 部署。
