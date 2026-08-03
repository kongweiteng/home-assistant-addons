# Codex Controller

Codex Controller 是一个基于 OpenAI 官方 `codex app-server` 的 Home Assistant Add-on。它负责 Codex 认证、持久 Thread、全局单活动 Turn 队列、重启恢复和受限业务工具代理，用于逐步替换 Hermes 的模型与任务后端。

## 正式认证方式

- `auth_mode=chatgpt_device_code`：调用官方 `account/login/start` 的 `chatgptDeviceCode`。这与本机 Codex 使用 ChatGPT 账号登录属于同一类官方 managed 认证，但 HAOS Controller 会建立独立会话。
- `auth_mode=api_key`：从 Home Assistant Add-on 的 `password` option 读取 API Key，并调用官方 `account/login/start` 的 `apiKey` 模式。
- API Key 模式可设置 `openai_base_url`。空值使用 OpenAI 官方 API；自定义值只接受公开 HTTPS 的 Responses API 兼容地址，不支持 Chat Completions、URL 内凭据、HTTP 或内网目标。
- 可选 `codex_model` 用于固定自定义端点需要的模型名；空值使用 Codex 默认模型。该字段只在 API Key 模式生效。
- 认证模式必须在 options 中显式选择，登录后必须读回与配置匹配的账户类型；禁止自动降级、混用或静默切换。
- 页面只显示 Key/URL 是否已配置以及端点/模型模式，不提供输入框、不回显 URL 或 Key；Key 不进入状态、SQLite、普通日志、命令行参数或 app-server 子进程环境，完整 URL 也不进入状态、SQLite 或普通日志。
- 不支持 PAT、外部 Token 注入或实验 Bedrock 登录。
- 不复制本机 Token、Cookie 或整个 `CODEX_HOME`。

## 当前阶段

- 固定官方 `@openai/codex@0.146.0`，按锁文件 SHA-512 校验平台包，镜像只保留原生 Codex 二进制并在构建时生成 app-server Schema。
- 默认 `intake_enabled=false`，不会接收正式微信任务。
- `0.1.2` 增加 API Key 自定义 Responses API URL、严格公网 HTTPS 校验和脱敏状态；默认仍不启用正式微信任务入口。
- Hermes 在正式切换验收前继续承担微信与记账任务。

## 安全边界

- 不申请 Home Assistant、Supervisor、Docker、host network、设备或 `/share` 权限。
- app-server 子进程只获得独立 `CODEX_HOME`、受限工作区和不含秘密的 Unix Socket 地址。
- 自定义 API URL 经结构、模式、DNS 和公网地址校验后，只写入权限为 `0600` 的私有 `CODEX_HOME/config.toml`；API Key 继续通过匿名文件描述符和 app-server 账户 RPC 注入，不写入该配置文件。
- Ledger 与 Operations Broker bearer 只保留在 Controller 主进程，不进入 app-server 环境、模型提示或日志。
- Gateway 附件 bearer 也只保留在 Controller 主进程；模型仅能提交短期 `attachment_ref`。
- app-server Thread 使用只读 sandbox 和 `approvalPolicy=never`；正式 HA 变更只能经 Broker。
- Controller 重启后，状态不确定的运行中作业进入 `recovery_required`，不会自动重放写操作。

## 本地验证

```bash
PYTHONPATH=codex_controller PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_codex_controller
```

配置、队列、认证和恢复说明见 [DOCS.md](DOCS.md)。
