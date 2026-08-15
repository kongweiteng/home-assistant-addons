# Codex Runner Relay 使用说明

## 内部配置

- `controller_base_url`：固定为 `http://local-codex-controller:8102`；旧短主机名、其他端口、HTTPS 和附加路径都会拒绝启动。
- `controller_api_token`：调用 Controller 内部 Runner Relay API 的 bearer。
- `relay_api_token`：Controller 向 Relay 发布 request/control 的 bearer。
- 其余 options 只控制连接数、消息大小、首帧超时、速率和 Controller 超时。

所有 token 至少 32 字符，只保存在 Add-on 私有 options，不进入 URL、日志、状态或页面。

## 网络

- `/v1/runner`：经独立 NPM hostname 暴露的 WebSocket 数据面；token/credential 必须在第一条 JSON 帧发送。
- `/install/<ticket>`：同一独立 hostname 上的短期安装脚本入口。脚本响应为 `no-store/no-referrer/nosniff`，Relay access log 与 NPM 此路径 access log 必须关闭；错误响应不回显 ticket。
- `/internal/v1/runners/<runner_id>/<request|control>`：仅供 Controller 在 Add-on 内部网络发布。
- `/healthz`：仅返回版本、连接数和容量，不返回 Runner ID、地址或凭据。

宿主端口默认不映射。NPM 应通过 Add-on 内部网络连接 `local-codex-runner-relay:8098`，只开放 `/v1/runner` 与 `/install/` 所需路径，不得暴露 HA `8123`、MQTT `1883`、SSH、Controller、Relay 内部发布 API或 NPM 管理端口。

## 安装链接边界

- ticket 与 Controller enrollment 使用同一短期 bearer 值，只能出现在 URL 路径和返回给请求方的安装脚本中，不得进入普通日志、健康状态或持久存储。
- Relay 调用 Controller 的 `install-bootstrap` 只检查 pending、有效期、撤销/领取状态、Runner 平台和项目白名单，不消费 enrollment。
- 返回脚本下载固定 installer，核对字节大小和 SHA-256，再下载并核对固定平台 bundle。bundle 内置 Python `3.11.13`、Runner `0.3.0` 和 Codex `0.146.0`。
- ticket 过期、撤销、领取、Runner 状态不允许或 Controller 拒绝时，对外统一返回不可用，不泄露具体票据内容。

## 回滚

先撤销或等待短期安装链接过期，关闭 Controller 的新 enrollment 与 Relay 配置，确认没有活动 lease 或 `recovery_required`，再停止 Relay。停止 Relay 不删除 Controller Registry、Runner credential 摘要、服务器 Agent、worktree、分支或 Session。
