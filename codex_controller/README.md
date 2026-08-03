# Codex Controller

Codex Controller 是一个基于 OpenAI 官方 `codex app-server` 的 Home Assistant Add-on。它负责 ChatGPT 认证、持久 Thread、全局单活动 Turn 队列、重启恢复和受限业务工具代理，用于逐步替换 Hermes 的模型与任务后端。

## 正式认证方式

- 唯一正式入口是官方 `account/login/start` 的 `chatgptDeviceCode`。
- 这与本机 Codex 使用 ChatGPT 账号登录属于同一类官方 managed 认证，但 HAOS Controller 会建立独立会话。
- 登录完成后必须读回 `authMode=chatgpt`；否则任务入口保持关闭。
- 不支持 API Key、PAT、外部 Token 注入或实验 Bedrock 登录。
- 不复制本机 Token、Cookie 或整个 `CODEX_HOME`。

## 当前阶段

- 固定官方 `@openai/codex@0.146.0`，按锁文件 SHA-512 校验平台包，镜像只保留原生 Codex 二进制并在构建时生成 app-server Schema。
- 默认 `intake_enabled=false`，不会接收正式微信任务。
- 尚未安装到正式 HAOS，也没有发起正式 ChatGPT 登录。
- Hermes 在正式切换验收前继续承担微信与记账任务。

## 安全边界

- 不申请 Home Assistant、Supervisor、Docker、host network、设备或 `/share` 权限。
- app-server 子进程只获得独立 `CODEX_HOME`、受限工作区和不含秘密的 Unix Socket 地址。
- Ledger 与 Operations Broker bearer 只保留在 Controller 主进程，不进入 app-server 环境、模型提示或日志。
- Gateway 附件 bearer 也只保留在 Controller 主进程；模型仅能提交短期 `attachment_ref`。
- app-server Thread 使用只读 sandbox 和 `approvalPolicy=never`；正式 HA 变更只能经 Broker。
- Controller 重启后，状态不确定的运行中作业进入 `recovery_required`，不会自动重放写操作。

## 本地验证

```bash
PYTHONPATH=codex_controller PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_codex_controller
```

配置、队列、认证和恢复说明见 [DOCS.md](DOCS.md)。
