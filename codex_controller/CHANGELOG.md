# 更新记录

## 0.2.1

- Renovation Hub 工具目录改为读取受认证 business manifest；完整 Schema、中文元数据、风险、transport 和 annotations 不再依赖 Controller 静态复制。
- 新增 bootstrap、SQLite last-good、digest/revision 校验、运行期刷新和 retired 工具处理；Hub 暂时不可达或返回非法 manifest 时不会清空已验证能力。
- 合法 manifest 变化会更新 catalog revision、发送 `notifications/tools/list_changed`，并通过真实 Codex `0.146.0` app-server 在不重启的情况下刷新 `tools/list`。
- owner/owner_legacy 自动获得当前全部合法 Hub 业务工具及单工具 `approval_mode=approve`；member 继续固定为 8 个服务端只读工具，未来新增工具不会扩展 member 权限。
- 新增未知未来读写工具、last-good/restart、retired/disabled、旧 Thread、完整动态 Schema、真实双架构 app-server 和运行期调用回归；不改变 Hub writer、Operations Broker 或正式微信开关。

## 0.2.0

- 新增全部 32 个 MCP 工具的唯一元数据事实源，统一中文名称、服务归属、只读/写入/受控类型、Schema 和自然语言意图示例；Ingress 明确意图示例不是固定关键词。
- 新增 SQLite 逐工具策略、catalog revision、管理 request ID 幂等、真实 MCP `tools/list` 心跳和有界脱敏调用审计；升级为 additive schema，既有工具默认开启并可在重启后保持策略。
- MCP 声明 `listChanged` 并在策略 revision 变化时发送 `notifications/tools/list_changed`；页面的“已发布”只来自真实 `tools/list` 回报，不再使用 Router 本地清单推断。
- 已加载 Thread 的角色或有效工具上下文变化时替换 conversation Thread：尚未发生 Turn 的空 Thread 重新 `thread/start`，已有持久 Turn 的 Thread 使用官方 `thread/fork` 保留历史；兼容 Codex `0.146.0` 对空 Thread 返回 `no rollout found` 的行为。
- 工具关闭同时作用于 `tools/list` 和 `tools/call`。旧 Thread、缓存目录、策略损坏和未知策略均不能绕过服务端 fail-closed 门禁。
- 新增 `owner_legacy`、`owner` 和 `member_read_only` 作业能力画像；成员只允许 8 个确定性安全装修查询，写账、媒体、导出和 Operations 保持拒绝。
- 新增 HMAC `CV-*`、`TH-*`、`JB-*`、`TN-*` 排障短标识；`/new` 固定确认携带新 Thread 短标识，状态与作业 DTO 不返回完整会话、Thread 或 Turn ID。
- 新增 JSON + 短期 CSRF + revision + request ID 工具管理 API和完整深色 Ingress 交互；错误、调用审计和页面均不回显 URL、bearer、API Key 或工具参数正文。
- 修复 `item/completed` 在 Turn 绑定前被缓存、而 `turn/completed` 在绑定后抢先完成作业的竞态；同一 Turn 的缓存事件固定先落最终文本再完成状态，避免微信偶发取得空结果。
- 私有 Codex 配置为全部 32 个固定内部 MCP 工具写入 `approval_mode="approve"`，覆盖 `ledger_generate_chart`、`ledger_add_refund` 和 Operations；保持 `approvalPolicy=never`，移除微信入口无法交互的 Codex 二次审批。
- owner 的清晰查询、图表、导出、记账、退款、更正、撤销和归档请求本身即为匹配工具调用授权；仅在必填字段缺失或语义确实歧义时澄清，讨论/假设不得推断为写入。member、逐工具策略、Hub writer/幂等和 Broker Passkey/收据/allowlist/execution 门禁不变。

## 0.1.9

- 修复 `/new` 创建的新 Thread 在同一 app-server 进程内被下一条消息重复 `thread/resume`、导致请求在 `turn/start` 前失败的问题。
- Controller 仅在进程内记录已经由 `thread/start` 或成功 `thread/resume` 加载的 Thread；已加载 Thread 直接进入后续 Turn，未知或重启后恢复的持久 Thread 仍执行完整安全恢复。
- 加载状态以锁串行保护并在 Controller/app-server 启停时清空，不改变 SQLite Thread 映射、单活动 Turn、队列、MCP 工具、认证或写入边界。
- 修复 app-server 的净化环境未向 MCP 子进程提供受控 Python 模块路径、导致页面显示已配置工具但真实 Thread 工具目录为空的问题；MCP 配置只注入固定 `/opt/codex-controller`，不会恢复对外部 `PYTHONPATH` 的继承。
- 新增净化环境下真实启动 MCP、执行 `initialize` 与 `tools/list` 的回归测试，确保装修工具不是只存在于 Controller 本地清单。
- 修复自然语言账本查询被误判为需要额外授权的问题：明确只读工具的无副作用描述和 MCP annotations，并仅为账本/装修的确定性查询工具配置单工具预批准。
- developer instructions 明确用户提出查询、查看、核验、汇总或明细即授权本次只读调用；写账、退款、修改、撤销、附件/媒体和 Operations 仍保留原有服务端门禁。

## 0.1.8

- 将无附件且文本精确为“打开新会话”或 `/new` 的微信作业识别为 Controller 控制命令，不再把它交给旧 Codex Thread 当普通问题处理。
- 控制命令通过官方 app-server 创建新 Thread，并在同一 SQLite 事务中替换当前会话映射、完成作业和写入审计；模型不会自行宣称是否创建成功。
- 旧 Thread 不删除、不重放；下一条普通消息恢复新 Thread，重新使用当前 developer instructions 和 MCP 工具目录。队列、幂等、单活动 Turn 与 `recovery_required` 阻断保持不变。
- 状态 API 和 Ingress 新增脱敏工具目录计数及 Renovation Hub/Operations 配置状态，便于区分旧 Thread、无工具目录和外部路由失败，不显示 bearer 或 URL。

## 0.1.7

- 修复持久 Thread 只在首次创建时注入 developer instructions、恢复时继续沿用旧架构上下文的问题；`thread/resume` 现在按官方 app-server Schema 重新传入当前 developer instructions、只读 sandbox、工作目录和 `approvalPolicy=never`。
- developer instructions 按启动时实际 MCP 工具目录声明 Renovation Hub 与 Operations 能力。账本连接状态、支出、汇总和明细必须先调用结构化只读工具核验，禁止沿用旧 Mac 代理或 Hermes 对话误报“未连接”。
- 保持微信通用助手、单活动 Turn、写工具幂等、秘密隔离和 Broker 审批边界不变。

## 0.1.6

- 未配置 Renovation Hub 或 Operations Broker 时不再向 Codex 暴露对应工具，避免模型看到不可执行能力。
- Operations 工具收敛为固定 propose、authorization request/status、execute 和 execution status 路由；参数 schema 禁止额外字段。
- Controller 使用微信 `message_id`、工具名和规范化参数生成写工具幂等键；同一消息同一语义稳定复用，不同消息不会共享写入键。
- 工具上下文只在当前活动作业和 Turn 内有效，Turn 完成或启动失败后立即清除。

## 0.1.5

- Controller intake 现在同时要求 app-server 进程运行、完成初始化且没有协议错误；子进程故障后立即停止接收和调度新作业。
- `/healthz` 在 app-server 启动、退出或协议故障时返回 `503` 以触发 watchdog；仅认证未完成而运行时正常时继续返回 `200`，避免受控登录状态形成重启风暴。
- 任一 `recovery_required` 作业会阻断后续队列调度。新增受 bearer 保护的人工恢复核对接口，只允许明确确认完成、确认失败或取消，不自动重放 Turn。

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
