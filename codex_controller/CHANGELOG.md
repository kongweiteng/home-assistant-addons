# 更新记录

## 0.1.4

- 明确微信入口是通用 Codex 助手，而不是装修专用机器人。
- 普通问答、讨论、分析、写作和规划默认由 Codex 直接回答；仅在用户意图确实需要装修账本或 Home Assistant 操作时调用结构化 MCP 工具。
- 保持只读 sandbox、`approvalPolicy=never`、单活动 Turn、最小权限工具和秘密隔离边界不变。

## 0.1.3

- 新增 Gateway 非消费附件预览到官方 app-server `localImage` 的微信图片输入链路。
- 图片在 Controller 私有 `/data/turn-media` 中以 `0700` 目录和 `0600` 文件暂存，严格校验 MIME、大小和 SHA-256；Turn 完成、失败或重启时清理。
- Turn 文本附带受控 `attachment_ref` 元数据，图片识别后仍可由 Ledger 或 Renovation Hub 工具一次性消费原引用，避免预览提前破坏归档链路。
- 当前只接受 JPEG、PNG 和 WebP；其他图片类型、Gateway 不可用或摘要不一致时作业明确失败，不伪装为已识别。

## 0.1.2

- API Key 模式新增可选 `openai_base_url`，空值保持 OpenAI 官方端点，自定义值继续使用官方 Codex 内置 `openai` provider 和 Responses API。
- 新增可选 `codex_model`，用于让自定义端点固定与既有 Codex 相同的模型；空值保持 Codex 默认模型。
- 新增 HTTPS、URL 结构、认证模式、DNS 和公网地址 fail-closed 校验；拒绝 HTTP、URL 内凭据、query/fragment、内部服务名、私网/回环/链路本地/保留地址和 Chat Completions 降级。
- 自定义 URL 只写入权限为 `0600` 的私有 Codex 配置；API Key 继续通过匿名文件描述符和 app-server `apiKey` 账户 RPC 注入，不进入配置、环境变量、命令行、状态、SQLite 或普通日志。
- Ingress 与状态 API 只显示 `official/custom`、URL/Key 是否已配置和脱敏错误，不回显完整 URL 或 Key。
- 默认继续保持 `intake_enabled=false`；升级和认证本身不会启用正式微信、装修 writer 或 HA Operations。

## 0.1.1

- 新增 `chatgpt_device_code` 与 `api_key` 两种官方认证模式，必须通过 Add-on options 显式选择，禁止自动降级和混用。
- API Key 使用 Supervisor `password` option，并通过匿名文件描述符进入 Controller 主进程；不进入命令行、状态、SQLite、普通日志或 app-server 子进程环境。
- 新增 API Key 启动应用、手工重试、缺失/拒绝/账户类型不匹配的 fail-closed 状态。
- Ingress 根据配置显示设备码或 API Key 状态；不提供 Key 输入框，也不显示 Key 内容。
- 保持 `intake_enabled=false` 默认值；本版本不配置真实 Key、不执行正式登录、不切换微信或 Operations。

## 0.1.0

- 交付审计后以包含 Renovation Hub 路由的运行时源码修订 `13764ccd0c2370d118597f452c20f6f2b62404e5` 重新构建 Controller 双架构本地镜像：amd64 `sha256:cdbc59d888f9780437bf3a16087e9dcc711ecfc2d8be6d47be7c1a6698f5dac9`，aarch64 `sha256:bcb2bd316c2ad4875c99f933d85190be14a445572434a10cce730d6be47a2e5a`。
- 两架构均以全新临时 `CODEX_HOME` 启动真实 `codex-cli 0.146.0 app-server`，只执行 `initialize`、`initialized` 和 `account/read`，确认保持未登录且未触发设备码登录；同时验证 14 个 `renovation_*` 工具、`renovation_dashboard`、`renovation_media_ingest`、`http://renovation-hub:8101` 和媒体大小上限配置。
- 新增 Weixin Gateway 一次性附件到 Renovation Hub 兼容账本模块的受限桥接，模型和 app-server 不接触 bearer 或内部路径。
- 新增 `renovation_*` 项目、阶段、空间、时间线、驾驶舱和媒体工具代理；图片/视频正文从 Gateway 流式转发到 Hub，不再进入 Base64 JSON。
- Codex 运行镜像改为按 SHA-512 校验官方 npm 平台包并只保留原生二进制，移除运行时 Node/npm 依赖。
- 固定官方 `@openai/codex@0.146.0`，使用稳定 stdio JSONL app-server。
- 新增仅允许 `chatgptDeviceCode` 且要求 `authMode=chatgpt` 的正式认证门禁。
- 新增 SQLite 持久队列、多 Thread、全局单活动 Turn 和 `recovery_required` 恢复语义。
- 新增无秘密 Unix Socket MCP 代理和中文 Ingress 状态页。
- 默认关闭正式任务入口，未授权任何 HAOS 安装、账号登录或 Hermes 切换。
