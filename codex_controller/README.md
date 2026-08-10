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
- `0.1.3` 增加微信图片的受控 `localImage` 输入：Controller 通过 Gateway 非消费预览读取图片，在私有目录校验并暂存，Turn 完成后自动清理；原附件引用仍可由装修工具一次性消费。
- `0.1.4` 明确微信是通用 Codex 入口：普通问答、讨论、分析、写作和规划默认直接回答，只有确实需要装修账本或 Home Assistant 操作时才调用对应结构化工具。
- `0.1.5` 增加 app-server 运行态 fail-closed、watchdog 故障状态和 `recovery_required` 人工核对阻塞；状态未知的 Turn 不会自动重放或允许后续作业越过。
- `0.1.6` 增加 Operations Broker 固定路由、按实际配置过滤工具，以及由 Controller 基于微信消息上下文生成的稳定写入幂等键。
- `0.1.7` 在每次恢复持久 Thread 时重新注入当前 developer instructions 和只读安全策略；提示词按实际工具目录声明 Renovation Hub/Operations 能力，并要求账本连接状态、汇总和明细先做结构化只读核验，禁止沿用旧 Mac 代理或 Hermes 上下文误报“未连接”。
- `0.1.8` 支持精确微信控制命令“打开新会话”和 `/new`：Controller 确定性创建新 Thread 并原子替换当前映射，不让旧 Thread 或模型伪装成已经切换；下一条消息使用当前工具目录。
- `0.1.9` 修复同一 app-server 进程内新建 Thread 被下一条消息重复 `thread/resume` 的问题；当前进程已加载的 Thread 直接进入 `turn/start`，Controller/app-server 重启后才按持久映射恢复。
- `0.1.9` 同时修复净化后的 app-server 环境无法导入本地 MCP 代理的问题；MCP 子进程只获得固定模块路径和无秘密 Unix Socket，真实 `tools/list` 必须能返回当前装修/运维工具目录。
- `0.1.9` 将账本汇总、明细、单条流水以及装修项目/阶段/空间/时间线/驾驶舱查询明确标记为无副作用只读工具，并在 Codex MCP 配置中只预批准这些查询工具。用户自然语言提出查询或汇总即授权本次只读调用，不需要 Passkey 或额外确认；写账、退款、修改、撤销、归档和 Operations 权限不放宽。
- `0.2.0` 新增 MCP 工具控制台：页面列出全部 32 个已知工具的中文名称、所属服务、风险类型、配置/策略/实际发布/可调用状态、自然语言意图示例和脱敏最近调用结果。意图示例不是固定关键词。
- 每个工具都有 SQLite 持久开关。关闭会同时从下一次 MCP `tools/list` 隐藏并在 `tools/call` 服务端立即拒绝，因此旧 Thread 或缓存目录不能绕过；策略损坏时目录和调用全部 fail closed。
- MCP 进程声明并发送标准 `notifications/tools/list_changed`，Controller 只把真实 `tools/list` 回报记录为“已发布”，不再用主进程本地清单冒充 app-server 实际装载。
- Gateway 作业可携带 `owner` 或 `member_read_only` 能力画像；成员只允许 8 个确定性安全装修查询。Thread、会话、作业和 Turn 的页面诊断使用私有 HMAC 短标识，`/new` 回复会附带 `TH-*`。
- 同一 app-server 进程中，Thread 的角色或有效工具上下文变化时会替换 conversation 的 Thread：尚未发生 Turn 的空 Thread 不能被官方 `thread/fork`，因此重新 `thread/start`；已经发生 Turn 并持久化的 Thread 才使用官方 `thread/fork` 保留历史。服务端工具门禁始终独立生效。
- `0.2.0` 为全部 32 个固定内部 MCP 工具写入单工具 `approval_mode="approve"`，保持 Thread/Turn 的 `approvalPolicy=never`，使 owner 的清晰查询、图表、导出、记账、退款、更正和归档请求不再依赖无法在微信中操作的 Codex 审批弹窗。该配置只移除模型层二次审批，不改变 member allowlist、逐工具策略、Hub writer/幂等或 Operations Broker 门禁。
- `0.2.1` 从 Renovation Hub 的受认证 business manifest 动态加载完整工具 Schema，并保存 last-good；Hub 暂时不可达或返回非法目录时继续使用已验证目录，首次启动则使用内置兼容 bootstrap。
- Hub manifest revision/digest 变化会更新 Controller catalog revision、发出 `listChanged` 并让 app-server 在不重启的情况下重新 `tools/list`。未来合法的 `ledger_*` / `renovation_*` 工具自动对 owner/owner_legacy 开放；member 永远只保留服务端固定 8 个只读工具。
- `0.2.2` 在 `ledger_generate_chart` 成功后立即从 Hub 固定下载接口读取 PNG，校验引用、MIME、大小、PNG 签名和 SHA-256，再以 `0700/0600` 权限私有固化到 `/data/job-artifacts`。completed job 只返回确定性中文摘要和安全 artifact DTO，不返回 Hub `download_ref`、bearer、内部 URL 或文件路径。
- `0.2.3` 使用 `codex app-server --disable goals --listen stdio://` 启动官方进程，禁止普通微信 Turn 派生后台 Goal 与后续消息竞争作业所有权；持续监控必须交给独立自动化服务。
- `0.2.3` 的 bootstrap 付款目录与 Hub canonical v2 契约一致；Hub 返回白名单内、有界且结构化的校验错误时保留 `invalid_input` / `invalid_tags` 等可纠正语义，非 JSON、超长或未知错误仍统一脱敏。
- `0.2.4` 将普通附件与装修档案意图分离：图片、视频和文件默认只用于识别，只有明确请求保存到装修/施工/工地档案时才暴露并允许媒体归档工具；媒体从 Gateway 非消费流式读取，Hub 成功后才 ACK 消费。
- `0.3.0` 新增默认关闭的 Runner Center v2：在现有中文 Ingress 中管理 pending/enabled/draining/disabled/revoked Runner，使用独立 SQLite 注册表、一次性 enrollment、revision/request_id、原子 lease/assignment epoch 和脱敏任务审计。Runner 调度是确定性控制面，不经过 Codex app-server。
- Runner Center v2 只有在 `runner_center_v2_enabled=true` 且后续配置独立 Relay adapter 时才可能分发任务；本版本未内置真实公网 Relay，默认关闭时普通微信、Controller Thread、MCP、装修账本、Operations 和 Remote Work v1 行为不变。
- artifact 下载分为 Gateway bearer 内部接口和 HA Ingress 高熵短期 token 接口；默认保留 24 小时，单图 20 MiB、总配额 100 MiB、每 job 最多 4 个，过期和孤立文件自动清理。模型被明确禁止自行构造图片或下载链接。
- 默认仍不启用正式微信任务入口。
- 旧 Hermes iLink 身份已经失效；正式装修 writer 已迁移到 Renovation Hub，Hermes 已停止，微信恢复不能依赖恢复旧 Hermes 进程，也不得形成双 poller 或双 writer。

## 安全边界

- 不申请 Home Assistant、Supervisor、Docker、host network、设备或 `/share` 权限。
- app-server 子进程只获得独立 `CODEX_HOME`、受限工作区和不含秘密的 Unix Socket 地址。
- 自定义 API URL 经结构、模式、DNS 和公网地址校验后，只写入权限为 `0600` 的私有 `CODEX_HOME/config.toml`；API Key 继续通过匿名文件描述符和 app-server 账户 RPC 注入，不写入该配置文件。
- Ledger 与 Operations Broker bearer 只保留在 Controller 主进程，不进入 app-server 环境、模型提示或日志。
- Gateway 附件 bearer 也只保留在 Controller 主进程；模型仅能提交短期 `attachment_ref`。图片预览文件固定写入私有 `/data/turn-media`，权限为 `0600`，不使用微信文件名构造路径。
- Hub 图表 bearer 和原始 `download_ref` 只存在于 Controller 主进程；持久 job DTO 只暴露随机 artifact ID、类型、大小、摘要、尺寸和不含内部路径的短期 fallback path。下载 token 由私有 HMAC 密钥确定性派生，SQLite 只保存其 SHA-256。
- app-server Thread 使用只读 sandbox 和 `approvalPolicy=never`；正式 HA 变更只能经 Broker。
- 当前动态发布的内部 MCP 工具逐个使用 `approve`，因此微信路径不依赖 Codex UI 审批；这不是权限放宽。manifest 校验、`member_read_only` 固定 allowlist、逐工具策略、当前作业/Turn、稳定幂等键、Hub 单 writer 以及 Broker 的提案、Passkey、一次性收据、allowlist 和 execution gate 继续在服务端强制。
- Controller 重启后，状态不确定的运行中作业进入 `recovery_required`，不会自动重放写操作。
- 写工具只在当前活动 Turn 的上下文中可调用；Turn 结束后上下文立即清除，同一微信消息的同语义调用复用相同幂等键，不同消息生成不同幂等键。

## 本地验证

```bash
PYTHONPATH=codex_controller:renovation_hub PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_codex_controller tests.test_codex_dynamic_mcp
```

配置、队列、认证和恢复说明见 [DOCS.md](DOCS.md)。
