# 更新记录

## 0.1.1

- 将 Relay 到 Controller 的内部 URL 固定为真实 HAOS Add-on hostname `http://local-codex-controller:8102`，并同步收紧启动校验和 `NO_PROXY`。
- NPM upstream 明确使用 `local-codex-runner-relay:8098`；WSS 协议、Runner `0.2.0`、凭据、限流和内部 API 均不改变。

## 0.1.0

- 新增出站 WSS Runner 数据面、第一帧 enrollment/auth、单 Runner 连接绑定和受认证内部 request/control 发布。
- 新增 Controller enrollment/auth/heartbeat/status/result 转发、消息大小、连接容量、首帧超时、每连接速率限制和无秘密健康检查。
- 默认无宿主端口、Ingress、host network、privileged、Docker socket 或主机挂载；不包含真实 HAOS/NPM/DNS 部署。
