# Codex Controller

Codex Controller 是一个基于 OpenAI 官方 `codex app-server` 的 Home Assistant Add-on。它负责 Codex 认证、持久 Thread、全局单活动 Turn 队列、重启恢复和受限业务工具代理，用于逐步替换 Hermes 的模型与任务后端。

## 正式认证方式

- `auth_mode=chatgpt_device_code`：调用官方 `account/login/start` 的 `chatgptDeviceCode`。这与本机 Codex 使用 ChatGPT 账号登录属于同一类官方 managed 认证，但 HAOS Controller 会建立独立会话。
- `auth_mode=api_key`：从 Home Assistant Add-on 的 `password` option 读取 API Key，并调用官方 `account/login/start` 的 `apiKey` 模式。
- 认证模式必须在 options 中显式选择，登录后必须读回与配置匹配的账户类型；禁止自动降级、混用或静默切换。
- API Key 页面只显示“是否已配置”，不提供输入框、不回显内容；Key 不进入状态、SQLite、普通日志、命令行参数或 app-server 子进程环境。
- 不支持 PAT、外部 Token 注入或实验 Bedrock 登录。
- 不复制本机 Token、Cookie 或整个 `CODEX_HOME`。

## 当前阶段

- 固定官方 `@openai/codex@0.146.0`，按锁文件 SHA-512 校验平台包，镜像只保留原生 Codex 二进制并在构建时生成 app-server Schema。
- 默认 `intake_enabled=false`，不会接收正式微信任务。
- `0.1.1` 候选正在进行本地、双架构和 HAOS 只读影子验证；本阶段不会配置真实 API Key，也不会发起正式登录。
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
