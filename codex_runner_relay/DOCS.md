# Codex Runner Relay 使用说明

## 内部配置

- `controller_base_url`：固定为 `http://local-codex-controller:8102`；旧短主机名、其他端口、HTTPS 和附加路径都会拒绝启动。
- Relay 到 Controller 的内部客户端固定使用 IPv4；这是为了兼容 HAOS Add-on DNS 可能只向单个容器返回不可用 IPv6 地址、而 Controller 当前只监听 IPv4 的运行环境。
- Controller 对已经进入不可覆盖终态的同一 assignment 返回 `runner_late_message` 时，Relay 只向 Runner 确认该传输事件已被权威拒绝，避免旧 outbox 队头阻塞 heartbeat。Controller 对恢复期旧 `desktop_event` 返回 `desktop_event_sequence_stale` 时也回对应 ACK，因为更高 event sequence 已成为权威事实，该旧事件不可能再次被接受；这一新增例外只绑定 `desktop_event`。Controller task 仍保持 `recovery_required`，Runner 本地 task/result 与 worktree 证据不删除、不重放，Desktop 冲突、绑定、隐私、摘要和其他错误继续关闭连接。
- `controller_api_token`：调用 Controller 内部 Runner Relay API 的 bearer。
- `relay_api_token`：Controller 向 Relay 发布 request/control 的 bearer。
- `max_message_bytes` 默认 `524288`，Schema 上限 `1048576`；用于有界 Desktop 快照/事件以及既有 Runner 帧，不允许用它传输原始日志、附件或 App JSONL。
- `messages_per_minute` 默认 `1200`，Schema 上限 `10000`；Desktop delta 仍必须先在 Runner 端净化、截断和合并，不能依靠放大 Relay 限额代替背压。
- 其余 options 只控制连接数、首帧超时和 Controller 超时。

所有 token 至少 32 字符，只保存在 Add-on 私有 options，不进入 URL、日志、状态或页面。

## 网络

- `/v1/runner`：经独立 NPM hostname 暴露的 WebSocket 数据面；token/credential 必须在第一条 JSON 帧发送。
- `/install/<ticket>`：同一独立 hostname 上的短期安装脚本入口。脚本响应为 `no-store/no-referrer/nosniff`，Relay access log 与 NPM 此路径 access log 必须关闭；错误响应不回显 ticket。
- `/internal/v1/runners/<runner_id>/<request|control|desktop_command>`：仅供 Controller 在 Add-on 内部网络发布；Desktop 下行仍按同一 runner_id/credential 一对一连接发送。
- `/healthz`：仅返回版本、连接数和容量，不返回 Runner ID、地址或凭据。

宿主端口默认不映射。NPM 应通过 Add-on 内部网络连接 `local-codex-runner-relay:8098`，只开放 `/v1/runner` 与 `/install/` 所需路径，不得暴露 HA `8123`、MQTT `1883`、SSH、Controller、Relay 内部发布 API或 NPM 管理端口。

## 安装链接边界

- ticket 与 Controller enrollment 使用同一短期 bearer 值，只能出现在 URL 路径和返回给请求方的安装脚本中，不得进入普通日志、健康状态或持久存储。
- Relay 调用 Controller 的 `install-bootstrap` 只检查 pending、有效期、撤销/领取状态、Runner 平台和项目白名单，不消费 enrollment。
- 返回脚本下载固定 installer，核对字节大小和 SHA-256，再下载并核对固定平台 bundle。当前滚动目录精确接受 Python `3.11.13`、Codex `0.146.0` 与 Runner `0.3.13`，同时保留已冻结的 `0.3.6/0.3.11/0.3.12` bootstrap；其他 Runner 版本仍拒绝。
- Controller 返回的 Registry `labels` 与 `policy_revision` 只作为摘要固定安装参数透传；Relay 不扩大、不缓存，也不据此拥有调度策略。
- ticket 过期、撤销、领取、Runner 状态不允许或 Controller 拒绝时，对外统一返回不可用，不泄露具体票据内容。

## 回滚

先撤销或等待短期安装链接过期，关闭 Desktop 页面入口和 Runner 的 `[desktop].enabled`，确认 Desktop 与普通 Runner outbox 已排空、没有 `pending/submitted/accepted/unknown` Desktop 命令、活动 lease 或 `recovery_required`，再停止 Relay。回退到 `0.2.7` 会失去 Desktop 消息类型并重新只接受 Runner `0.3.5`，因此必须同时回退 Controller 到 `0.5.10`/旧 manifest 和 Mac Runner 到 `0.3.5`。停止 Relay 不删除 Controller Registry、Desktop 审计、Runner credential 摘要、服务器 Agent、worktree、分支、Session 或 Codex 原 Thread。

继续回退到 `0.2.6` 会重新只接受 Runner `0.3.4`，必须继续同步回退 Controller manifest；不得继续生成更高版本安装链接。
