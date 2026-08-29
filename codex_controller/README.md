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
- `0.5.20` 完整保留正式 `0.5.19` 的业务源码、53 个工具、瞬态 Turn 重试、M8 owner-only 备车和 Desktop availability refresh；当前 P5-R8 候选把 Controller 内置 pinned manifest 与 Runner 版本常量精确同步到 Runner `0.3.15`。仅修改 options URL/SHA 不能替换内置 manifest；版本、原始字节 SHA-256 或四平台目录任一不匹配仍 fail closed。
- `0.5.19` 从正式运行 `0.5.17` 的精确源码重基，只叠加明确瞬态 Turn 的安全有界重排；完整保留 M8 owner-only 备车、Desktop availability refresh、Runner `0.3.12` manifest、装修报价媒体意图和现有工具目录。任何 agent 输出、工具/命令/文件/Web/子代理活动或 artifact 都会阻断自动重试。
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
- Gateway 作业可携带 `owner` 或 `member_read_only` 能力画像；成员只允许 8 个确定性安全装修查询和 `memo_list` 家庭备忘录只读查询。Thread、会话、作业和 Turn 的页面诊断使用私有 HMAC 短标识，`/new` 回复会附带 `TH-*`。
- 同一 app-server 进程中，Thread 的角色或有效工具上下文变化时会替换 conversation 的 Thread：尚未发生 Turn 的空 Thread 不能被官方 `thread/fork`，因此重新 `thread/start`；已经发生 Turn 并持久化的 Thread 才使用官方 `thread/fork` 保留历史。服务端工具门禁始终独立生效。
- `0.2.0` 为全部 32 个固定内部 MCP 工具写入单工具 `approval_mode="approve"`，保持 Thread/Turn 的 `approvalPolicy=never`，使 owner 的清晰查询、图表、导出、记账、退款、更正和归档请求不再依赖无法在微信中操作的 Codex 审批弹窗。该配置只移除模型层二次审批，不改变 member allowlist、逐工具策略、Hub writer/幂等或 Operations Broker 门禁。
- `0.2.1` 从 Renovation Hub 的受认证 business manifest 动态加载完整工具 Schema，并保存 last-good；Hub 暂时不可达或返回非法目录时继续使用已验证目录，首次启动则使用内置兼容 bootstrap。
- Hub manifest revision/digest 变化会更新 Controller catalog revision、发出 `listChanged` 并让 app-server 在不重启的情况下重新 `tools/list`。未来合法的 `ledger_*` / `renovation_*` 工具自动对 owner/owner_legacy 开放；member 永远只保留服务端固定 8 个装修只读工具和 `memo_list`。
- `0.2.2` 在 `ledger_generate_chart` 成功后立即从 Hub 固定下载接口读取 PNG，校验引用、MIME、大小、PNG 签名和 SHA-256，再以 `0700/0600` 权限私有固化到 `/data/job-artifacts`。completed job 只返回确定性中文摘要和安全 artifact DTO，不返回 Hub `download_ref`、bearer、内部 URL 或文件路径。
- `0.2.3` 使用 `codex app-server --disable goals --listen stdio://` 启动官方进程，禁止普通微信 Turn 派生后台 Goal 与后续消息竞争作业所有权；持续监控必须交给独立自动化服务。
- `0.2.3` 的 bootstrap 付款目录与 Hub canonical v2 契约一致；Hub 返回白名单内、有界且结构化的校验错误时保留 `invalid_input` / `invalid_tags` 等可纠正语义，非 JSON、超长或未知错误仍统一脱敏。
- `0.2.4` 将普通附件与装修档案意图分离：图片、视频和文件默认只用于识别，只有明确请求保存到装修/施工/工地档案时才暴露并允许媒体归档工具；媒体从 Gateway 非消费流式读取，Hub 成功后才 ACK 消费。
- `0.4.2` 保持 Runner Center v2 管理面默认开启，并将 Controller 到 Relay 的内部契约固定为真实 HAOS hostname `http://local-codex-runner-relay:8098`；独立 Relay adapter、Relay enrollment/auth/event 内部接口和摘要固定的 Runner `0.2.0` 安装制品目录保持不变。Runner 调度仍是确定性控制面，不经过 Codex app-server。
- `0.4.3` 新增 5 个家庭备忘录 MCP 工具。Controller 只调用现有 HAOS Node-RED 的认证 API，不访问 SQLite；新增事项的微信来源和幂等 ID 由当前 Gateway 消息 ID 确定性派生。成员仅可查询，owner 的修改、完成和取消在候选不唯一时必须先消歧。
- Controller 在开放 intake 前预热并校验 app-server 的真实 MCP 目录；目录尚未包含全部当前可用工具时保持不可接单，确保重启后的第一条微信不会先误报能力未接入。
- `memo_create` 携带 `due_at` 时调用的是 Node-RED 独立持久化、调度和通知服务，不属于 Codex Goal 或当前 Turn 后台等待。owner 明确说“记一下”或“提醒我”并给出可确定时间时必须调用该工具，不得误报不能主动提醒或改荐手机日历。
- 为避免持久会话历史继续覆盖工具提示，Controller 会对明确的“记一下/提醒我 + 可确定时间”、未完成/今天/逾期查询，以及 owner 的明确完成命令执行确定性路由：使用 `received_at` 按 `Asia/Shanghai` 解析相对日期与中文时间，直接调用受认证的 `memo_create`、`memo_list` 和唯一匹配后的 `memo_complete`，不启动模型 Turn；多个完成候选仍要求消歧，Node-RED 幂等、审计和提醒状态机保持不变。
- `0.5.0` 将 Runner Center 安装链升级到自包含 Runner `0.3.0`：四个平台制品固定 Python `3.11.13`、Runner 与 Codex `0.146.0`，目标机不再依赖预装 Python、pip、venv、pyenv 或 Homebrew。Runner 调度仍是确定性控制面，不经过 Codex app-server。
- `0.5.1` 将 Registry 的真实标签和 policy revision 绑定到 install-bootstrap 与首次 enrollment；安装器不再写入固定标签。Runner 上报未登记标签或过期策略会在领取长期凭据前 fail closed，并配套 Runner `0.3.1` 的跨重启持久心跳序号。
- `0.5.2` 区分 Relay 明确的 `runner_offline` 与未知发布结果。明确未送达时只释放尚未运行的 lease，并等待真实 heartbeat 后使用新 epoch 重排；未知结果继续保留原 lease，禁止双执行。Relay 离线事实会立即覆盖旧心跳宽限窗口，成功发布清除旧错误。
- `0.5.3` 固定 Runner `0.3.2`。新 Runner 不再使用超时后不可继续读取的 Python socket file；正常 heartbeat 等待超时后仍保持同一 WSS 会话并继续接收 ACK、ping 和任务帧，避免在线状态短暂刷新但 Relay 实际连接数持续为零。
- `0.5.4` 在 Controller 镜像内置与公开 Runner `0.3.2` Release 同字节、同 SHA-256 的 manifest，HAOS 即使暂时无法访问 GitHub，页面仍可用本地固定目录生成一次性安装链接。目标机下载 installer 和 bundle 时继续校验 HTTPS、文件大小和 SHA-256，不通过 Controller 代理资产。
- `0.5.5` 固定 Runner `0.3.3`，修复 Codex Responses API 对结构化输出 Schema 的严格校验：`error_code` 现在必填且允许为 `null`，成功任务不再在执行前因无效 `codex_output_schema` 进入 `recovery_required`。
- `0.5.6` 固定 Runner `0.3.4`，允许 Codex 在保持 `workspace-write` 的同时写入已登记仓库与任务 linked worktree 共同的 Git metadata，从而完成本地 commit。Runner 会先核对两处绝对 Git common dir 完全一致；不一致时 fail closed，远端请求仍不能选择路径、沙箱或 Git ref。
- `0.5.7` 先核验已认证旧 Runner result 的摘要、Runner、task、assignment epoch 和 lease；仅当 task 已是终态或 `recovery_required` 时改判为 `runner_late_message`，使 Relay 可安全 ACK 并解除本地 outbox 队头。活动任务的旧 Schema 或坏数据仍返回 `runner_payload_invalid`。
- `0.5.7` 新增管理员 CSRF/revision/request ID 保护的 Runner recovery 确认失败操作。它只将已核对的 `recovery_required` 任务记为 `failed`、释放 lease 并让 Runner 回到 idle；任务、审计、worktree 和 Session 全部保留，不删除、不重放。
- `0.5.9` 固定 Runner `0.3.5`，并让有效 active-task `busy` heartbeat 原子续期任务和 active lease。已运行任务只有在 Runner offline 且 lease 过期后才进入 `recovery_required`；默认 lease 为 600 秒，保持原 assignment/epoch、不自动转移，既有迟到结果和人工恢复边界不变。
- `0.5.10` 将 `awaiting_confirmation` 明确视为已经接收结果的等待状态，不再参与离线 sweep；真正 recovery 会保持 task/Runner 关联。旧版本形成的孤立 recovery 仅在 task 仍属于该 Runner、Runner idle 且没有其他活动任务时允许审计式确认失败。
- `0.5.15` 新增 owner-only 的 `aito_prepare_car_status`、`aito_prepare_car_request`、`aito_prepare_car_execute`。微信“备车/停止备车”只创建同会话 2 分钟确认，下一条动作一致的明确确认才单次调用固定 `switch.wen_jie_m8zeng_cheng_max`；任意其他下一条消息取消确认，重复 message ID 不重复调用，结果未知不盲重试。
- M8 备车使用 `homeassistant_api: true` 提供的运行时 Supervisor token，只访问固定 Core API 状态路径和 `switch.turn_on|turn_off`。不接受任意实体、domain、service、温度或出发时间，`member_read_only` 与 `owner_legacy` 均无备车能力，HA service 受理不会被表述为车辆成功。
- `0.5.15` 完整保留 `0.5.14` 的当前 App 实时模型目录和同 Thread 新 Turn 模型覆盖，并把镜像内置安装目录同步到正式 Runner `0.3.11` Release，修复已配置新摘要却读取旧 `0.3.6` manifest 的 `installer_manifest_digest_mismatch`。该修复不安装、升级、重启或改写现有 Runner。
- `0.5.16` 修复同一 App revision 下仅由 Owner/IPC 可用性派生的 `status/control_state` 变化被误判为业务冲突的问题。只有去除这两个顶层字段后 snapshot 完全相同才允许刷新；标题、摘要、Turn、active Turn、history、project、binding 或其他差异继续失败关闭。
- `0.5.17` 在完整保留 M8 `0.5.16` 能力的基础上同步内置 Runner `0.3.12` 四平台固定 manifest；其逐 ACK 公平发送修复必须与 Relay `0.2.11` 一起发布，不能让只接受 `0.3.6/0.3.11` 的旧 Relay 接收新 Runner。
- `0.5.14` 在既有 macOS Codex Desktop 原任务接管上增加当前 App 实时模型目录与同 Thread 新 Turn 模型覆盖：默认沿用原任务模型，仅允许 `continue` 和安全调整从净化目录选择模型，Controller 与 Runner 双层校验；native steer、非法/过期模型和目录漂移全部失败关闭。装修报价媒体意图、恢复期幂等与 Relay `0.2.9` 边界保持不变。
- `0.5.13` 统一保留 macOS Codex Desktop 原任务接管和装修报价媒体意图：Controller 提供 `/desktop` 与 `/api/desktop/v1/**`，接收既有 Runner 的脱敏 host/project/thread 快照、事件和控制收据；同时仅在用户明确要求保存询价、报价单、供应商名片或商品规格时开放对应媒体归档工具。恢复期语义相同快照按幂等刷新，已被更新 revision 取代的旧事件按 stale 消费，真正冲突继续失败关闭。
- Desktop 写控制仅对 enabled、online、macOS 且声明 `desktop_takeover_v1` 的既有 Runner 开放；Runner Center 全局关闭时 Desktop 状态和写 API 同时禁用。默认 steer 是 interrupt + 独立读回 + 同 Thread continue，native steer 仅作为显式竞态模式；archive capability 只有 Runner 配置固定非目标控制任务后才可发布。
- `/api/status` 发布运行包实际 Python 源码的 `source_identity` SHA-256；部署和回滚必须核对版本与摘要，不能再以相同版本号替代源码身份。历史上两个不同源码的 `0.5.11` 候选已由 `0.5.12` 收敛。
- 当前候选已完成 P1～P4 本地代码和自动化验证，并提供独立 `/desktop` Ingress 工作台；390×844 手机单栏与 1440×900 桌面三栏合成数据视觉/交互验收通过。正式 HAOS Controller-only 修复、真实 2 项目×2 Thread 和手机 E2E 仍未完成。
- 页面只在 HTTPS manifest、固定 SHA-256/文件大小和公开 WSS URL 全部校验成功后允许创建 Runner；创建结果同时提供短期一次性 HTTPS 安装链接和可复制终端命令，不再返回分散的 `runner_id + enrollment token`。链接 15 分钟后自动失效，支持复制、打开、撤销和重新生成；过期、领取或撤销后，页面立即清除内存中的链接与命令。
- Relay 通过受限 `/install/<ticket>` 返回 `no-store/no-referrer/nosniff` 的摘要固定 shell，并使用受认证的 Controller 非消费式 bootstrap 检查；真正 enrollment 仍只在 Runner 首次 WSS 注册时单次消费。Relay 不保存 ticket，不把错误 ticket 回显到响应。
- Relay 内部 URL 和两个最小权限 token 未配置时继续显示 `relay_configured=false`，等待任务不会被伪发布；`runner_relay_api_token` 只用于 Controller -> Relay 发布，`runner_relay_controller_api_token` 只用于 Relay -> Controller 回调，三项必须同时配置且 token 不得相同，否则拒绝启动。installer 未配置或摘要不匹配时仅关闭“生成安装命令”，既有 Runner Registry、列表和管理操作仍可使用。需要整体降级时可显式设置 `runner_center_v2_enabled=false`，普通微信、Controller Thread、MCP、装修账本、Operations 和 Remote Work v1 行为不变。
- macOS 无 Developer ID 签名，安装包可自包含运行环境，但首次运行仍可能出现系统信任提示，不能承诺完全消除系统确认。
- artifact 下载分为 Gateway bearer 内部接口和 HA Ingress 高熵短期 token 接口；默认保留 24 小时，单图 20 MiB、总配额 100 MiB、每 job 最多 4 个，过期和孤立文件自动清理。模型被明确禁止自行构造图片或下载链接。
- 默认仍不启用正式微信任务入口。
- 旧 Hermes iLink 身份已经失效；正式装修 writer 已迁移到 Renovation Hub，Hermes 已停止，微信恢复不能依赖恢复旧 Hermes 进程，也不得形成双 poller 或双 writer。

## 安全边界

- 不申请 Home Assistant、Supervisor、Docker、host network、设备或 `/share` 权限。
- app-server 子进程只获得独立 `CODEX_HOME`、受限工作区和不含秘密的 Unix Socket 地址。
- 自定义 API URL 经结构、模式、DNS 和公网地址校验后，只写入权限为 `0600` 的私有 `CODEX_HOME/config.toml`；API Key 继续通过匿名文件描述符和 app-server 账户 RPC 注入，不写入该配置文件。
- Ledger 与 Operations Broker bearer 只保留在 Controller 主进程，不进入 app-server 环境、模型提示或日志。
- Node-RED 家庭备忘录 Basic Auth 与模块 Token 只保留在 Controller 主进程，不进入 app-server 环境、模型提示、URL、SQLite 或日志；模型不能设置 `source_message_id`。
- Gateway 附件 bearer 也只保留在 Controller 主进程；模型仅能提交短期 `attachment_ref`。图片预览文件固定写入私有 `/data/turn-media`，权限为 `0600`，不使用微信文件名构造路径。询价图片的归档仍要求用户明确表达保存或关联意图，不因收到名片或报价单附件自动落库。
- Hub 图表 bearer 和原始 `download_ref` 只存在于 Controller 主进程；持久 job DTO 只暴露随机 artifact ID、类型、大小、摘要、尺寸和不含内部路径的短期 fallback path。下载 token 由私有 HMAC 密钥确定性派生，SQLite 只保存其 SHA-256。
- app-server Thread 使用只读 sandbox 和 `approvalPolicy=never`；正式 HA 变更只能经 Broker。
- 当前动态发布的内部 MCP 工具逐个使用 `approve`，因此微信路径不依赖 Codex UI 审批；这不是权限放宽。manifest 校验、`member_read_only` 固定 allowlist、逐工具策略、当前作业/Turn、稳定幂等键、Hub 单 writer 以及 Broker 的提案、Passkey、一次性收据、allowlist 和 execution gate 继续在服务端强制。
- Controller 重启后，状态不确定的运行中作业进入 `recovery_required`，不会自动重放写操作。
- 写工具只在当前活动 Turn 的上下文中可调用；Turn 结束后上下文立即清除，同一微信消息的同语义调用复用相同幂等键，不同消息生成不同幂等键。

## 本地验证

```bash
PYTHONPATH=codex_controller:renovation_hub PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_codex_controller tests.test_codex_dynamic_mcp tests.test_codex_family_memo
PYTHONPATH=codex_controller PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p 'test_codex_runner_*.py'
```

配置、队列、认证和恢复说明见 [DOCS.md](DOCS.md)。
