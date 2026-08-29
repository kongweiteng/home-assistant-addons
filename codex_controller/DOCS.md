# Codex Controller 使用说明

当前版本：`0.5.22`。

## 瞬态 Turn 安全重试

- `0.5.22` 从 `0.5.21` 精确重基；只把 Controller 内置 pinned manifest 与 Runner 版本常量升级为 `0.3.17`，用于登记已审计 Codex App `26.820.60940` / build `7119` / CLI `0.150.0-alpha.8` 的精确 runtime profile；M8 owner-only 备车、Desktop availability refresh、瞬态 Turn 重试、装修报价媒体意图、极速 agent 结果绑定修复和 53 个工具边界不变。
- `0.5.19` 从正式运行 `0.5.17` 的精确源码重基，完整保留 M8 owner-only 备车、Desktop availability refresh、Runner `0.3.12` manifest、装修报价媒体意图和现有工具目录。
- 只有 Codex `0.146.0` 明确分类为瞬态、当前尝试没有 agent 输出、工具/命令/文件/Web/子代理活动或 artifact，且总尝试少于 3 次时才持久延迟重排；其他错误明确终止或进入人工恢复。
- 重排保留 Thread、清除旧 Turn ID，并使用带 `+08:00` 的 `retry_not_before`。SQLite 迁移只增加列；raw error、详情、URL、prompt 和 token 不落库、不进入普通日志或微信回复。
- 回滚到 `0.5.17` 前必须关闭 intake，并确认不存在带 `retry_not_before` 的 queued 作业；回滚必须核对精确运行源码摘要，不能用旧 `0.5.14` 或已失效的 `0.5.18` 候选替代。

## M8 微信两阶段备车

- 只有显式 `owner` 作业可见并可调用三个固定工具：状态查询、创建确认、确认执行。`member_read_only` 和旧 `owner_legacy` fail closed。
- “备车/开始备车/停止备车”等明确文本只创建 2 分钟持久确认；同一 `conversation_key` 的下一条消息必须是动作一致的“确认备车”或“确认停止备车”。其他任何下一条消息原子取消旧确认。
- 请求 message ID 与确认 message ID 分别持久审计。相同 ID 重放返回原结果，不重复调用 Home Assistant；POST 结果未知时记录 `unknown` 并禁止自动重试。
- Controller 通过 `homeassistant_api: true` 的运行时 Supervisor token，只调用 `http://supervisor/core/api` 下固定实体状态和固定 `switch.turn_on|turn_off`。没有任意 URL、实体、domain、service、温度或时间参数。
- Home Assistant 2xx 只表示服务受理。微信必须明确提示以 AITO switch 的真实回读 `confirmed` 为准，不把提交成功当成车辆完成。

## Codex Desktop 原任务接管

- `0.5.17` 完整保留 `0.5.16` 的 `/desktop` 独立 Ingress 工作台、Owner/IPC availability refresh 和 M8 owner-only 固定工具，以及 `/api/desktop/v1/hosts`、`projects`、`threads`、单 Thread、事件长轮询和 `steer`、`interrupt`、`continue`、`archive`、`unarchive` 控制契约。页面支持多 Mac、多项目、多原 Thread、状态/标题筛选、任务历史、实时事件、当前 App 模型目录和能力感知控制；手机采用单栏，桌面采用三栏。
- Controller 恢复期对同 revision、同业务 snapshot 的 envelope 时间刷新按语义幂等处理；若只有顶层 `status/control_state` 因 Owner/IPC 可用性变化而改变，也按 semantic availability refresh 接受。去除这两个字段后仍有任何差异、精确 snapshot 未来 revision 缺失或绑定漂移时继续失败关闭；已被更新 revision 取代的旧 event 按 stale 消费。
- Controller 只接收 `HS-*`、`PJ-*`、`TH-*`、`TR-*` 脱敏引用、容量受限快照、事件 cursor 和幂等收据。原始 App `threadId`、`turnId`、Socket、绝对路径、凭据、隐藏 reasoning 和 developer instructions 不得进入 HAOS。
- Desktop host 必须来自 enabled、online、macOS 且声明 `desktop_takeover_v1` 的既有 Runner；Runner Center 全局关闭、Relay 未配置、host stale、协议降级、项目不在 Runner 白名单或缺少动作 capability 时全部拒绝写控制。
- 页面或 API 每次写入必须提交随机 `request_id` 和当前 `thread_revision`；steer/interrupt 还必须提交 `expected_turn_ref`。相同 request ID 与相同正文幂等返回原结果，不同正文冲突；同一 Thread 存在 pending、submitted、accepted 或 unknown 命令时不接受第二个控制。
- `steer` 默认 `mode=safe`：Runner 先按 expected Turn interrupt，独立读回，再在同一原 Thread 创建下一 Turn。`mode=native` 保持同 active Turn，但当前 App build 不强制调用方 expected Turn，只允许显式风险模式并要求前后快照对账。
- 模型选择默认“沿用原任务模型”。Controller 只接受 host 最新 `models[]` 中的净化 ID，并仅对 `continue` 或 `mode=safe` 创建的新 Turn 提交；Runner 执行前重新读取当前 App `model/list`，目录缺失、漂移或模型已移除时拒绝。native steer、interrupt、archive/unarchive 不接受 model，页面和 API 不开放 provider、endpoint、credential、service tier 或 reasoning。
- `continue` 只允许 idle/notLoaded/failed 且无 active Turn 的原 Thread；`archive` 只允许 idle，`unarchive` 只允许 archived。Runner 未配置固定非目标 archive 控制任务时不声明 `archive_control_v1`，Controller 因此直接拒绝 archive/unarchive。
- 事件接口使用 `after_cursor` 恢复，最多等待 25 秒；事件对应的精确 revision 快照必须先入库。断线、超时或发布结果未知时只允许重新读取和对账，不自动重放控制。
- 页面不使用 `localStorage`/`sessionStorage`，不接收原始 `thread_id`/`turn_id` 或原始 UUID；时间统一显示 `Asia/Shanghai`。390×844 与 1440×900 合成数据浏览器验收无水平溢出，native steer 竞态、recovery/protocol degraded、缺失 archive capability 和弱网重连均保持可见或 fail closed。

## 运行源码身份

- `/api/status` 返回 `source_identity.schema_version`、`algorithm`、`digest` 和 `file_count`；摘要覆盖容器实际加载的 `codex_controller/**/*.py`，使用带路径和长度边界的 SHA-256。
- 正式升级和回滚不能只检查 `version`。Controller-only helper 必须在 staging 时冻结期望摘要，在容器启动后独立读取实际摘要并精确比较；字段缺失、摘要不匹配或文件数异常一律视为错误源码并回滚。
- `0.5.11` 曾存在 Desktop 与装修报价两条不同源码的同版本候选。`0.5.12` 将两项能力统一，今后同版本源码漂移不得降级为告警继续运行。

## 通用微信会话

- 微信入口默认是通用 Codex 助手，可处理普通问答、讨论、分析、写作、规划和其他不需要外部执行的任务。
- 不会把所有消息默认解释为装修事项，也不会因为消息来自现有 Hermes/iLink 身份就自动调用账本。
- 只有用户意图确实需要装修账本、家庭备忘录或 Home Assistant 操作时，才调用对应的结构化 MCP 工具；Codex UI 不再二次审批固定内部工具，但角色权限、逐工具策略、幂等、writer 和 Operations 执行边界仍由各组件服务端强制。
- 每次新建或恢复持久 Thread 都会重新注入当前 developer instructions、只读 sandbox 和 `approvalPolicy=never`。即使历史会话曾讨论 Mac 代理、Hermes 或旧迁移状态，当前能力也必须以本轮 MCP 工具目录和实际调用结果为准。
- 微信 owner 发送无附件且文本精确为“打开新会话”或 `/new` 时，Controller 会在既有队列与幂等门禁内创建新 Thread，并返回确定性确认。近似文本或带附件消息不会触发重置；旧 Thread 不删除，下一条普通消息才进入新 Thread。
- 新 Thread 在当前 app-server 进程中已经处于加载状态，下一条消息不会重复调用 `thread/resume`；Controller/app-server 重启后进程内状态清空，持久 Thread 才会重新执行一次安全恢复。
- Ingress 的 MCP 工具控制台显示当前静态 Operations 工具与 Renovation Hub 动态 manifest 的并集，并分别展示内部服务配置、管理员策略、MCP 进程真实 `tools/list` 发布状态和当前可调用状态；不会显示 URL、bearer、完整 Thread 或会话标识。
- app-server 协议初始化后，Controller 在开放 intake 前主动调用 `mcpServerStatus/list`，并要求 `home_assistant_tools` 已包含当前 owner 可用工具；目录缺失或不完整时启动失败关闭，不让首条消息承担 MCP 懒加载。
- 工具旁的自然语言意图只是能力示例，不是固定关键词。Codex 根据整句话语义和本轮目录决定是否调用；普通讨论仍可直接回答。
- 管理员可逐工具开启或关闭。页面写请求必须同时携带短期 CSRF token、JSON、当前 catalog revision 和随机 request ID；并发旧 revision 会被拒绝，相同 request ID 的相同正文幂等返回原结果。
- `/new` 确认和内部作业状态会返回稳定 `TH-*` 短标识，便于排查旧 Thread；完整 Thread、Turn 和 conversation key 不进入页面 DTO。
- Renovation Hub 工具已配置时，账本是否连接、当前支出、汇总和明细问题必须先调用 `renovation_dashboard`、`ledger_summary`、`ledger_query` 等只读工具；用户自然语言提出查询、查看、核验、汇总或明细请求，即授权本次无副作用只读调用，不需要 Passkey、写入确认或额外征求授权。不得仅凭历史回复声称“未连接”，也不得要求用户重新发送已有账目。
- 对 owner，清晰的图表、导出、记账、退款、更正、撤销、导入检查和装修媒体/事件归档请求也视为本次匹配工具调用授权，不再询问“是否确认/授权”。只有缺少必填字段或语义确有多种合理解释时才澄清；讨论、假设、举例和方案比较不能推断为写入命令。收到图片、视频或文件本身不构成装修归档授权；只有文本明确包含装修/施工/工地档案目标和正向归档动作时，Controller 才向本轮 Codex 暴露媒体归档工具。
- 家庭备忘录工具固定调用 Node-RED `/endpoint/api/memos`。新增时 Controller 使用当前 Gateway `message_id` 的 SHA-256 派生 `source_message_id`，不会把原始消息 ID、Basic Auth 或模块 Token 交给模型。`memo_create + due_at` 由 Node-RED 独立持久化、调度和通知，不属于 Codex Goal 或当前 Turn 后台等待；owner 明确说“记一下”或“提醒我”并给出可确定时间时必须调用，不得误报不能主动提醒或建议改用手机日历。完成、取消或修改只有自然语言标题时先查询 pending 候选；唯一匹配才执行，多个候选必须消歧。所有到期时间使用 `Asia/Shanghai` 和 `+08:00`。
- “记一下/提醒我 + 可确定时间”、明确的未完成/今天/逾期查询，以及 owner 的明确完成命令由 Controller 在进入 app-server 前确定性处理，用于消除旧 Thread 历史对模型工具选择的影响。创建只接受明确前缀、无附件、合法相对/绝对日期和中文或数字时间；查询调用有界 `memo_list`；完成先查询 pending，唯一匹配才调用 `memo_complete`，零条或多条直接返回提示而不修改。其他讨论、模糊日期和成员写请求仍走原有能力与澄清边界。
- app-server 启动时显式禁用 `goals`。普通微信 Turn 不得创建 Codex Goal，也不得让当前 Turn 自身在后台持续监控或稍后主动跟进；该限制不禁止调用具有独立生命周期和通知通道的家庭备忘录等自动化服务。
- bootstrap `ledger_add_payment` 与 Hub manifest 一致，只发布 canonical v2 金额分、日期和九维 `grouped_tags`。Hub 的白名单结构化校验错误会在长度、JSON 形态、错误码和消息控制字符检查后保留；非结构化、超长、未知或敏感错误继续返回通用上游拒绝。

## Runner Center v2

`0.5.17` 继续默认启用 Controller 内确定性的 Runner Manager 和中文 Ingress 管理页。它使用独立 additive SQLite 表保存 Runner 注册、一次性 enrollment、凭据摘要、心跳、Relay 连接事实、任务、lease 和审计，不修改普通 Codex job/Thread/MCP 队列。Controller 到 Relay 的内部 URL 只接受 `http://local-codex-runner-relay:8098`，旧短主机名会 fail closed。

- 无需额外 option 即可使用 Runner 页面、API、Registry 和管理 CRUD；未配置 Relay 时页面明确显示 `relay_configured=false`，任务不会被伪发布。
- 显式设置 `runner_center_v2_enabled=false` 会关闭 Runner API 和调度，作为快速降级开关；现有 Controller、普通微信、Renovation Hub、通知、Operations 与 Remote Work v1 不受影响。
- 页面只有在 installer manifest URL、options 固定 SHA-256 和公开 WSS Relay URL 全部可用且校验通过时才启用“生成安装命令”。Controller 镜像内置与 Runner `0.3.17` 四平台候选完全同字节的 manifest，启动期无需访问 GitHub；服务端仍先核对原始字节 SHA-256、版本、完整平台目录和公网 HTTPS URL，再创建 enrollment。任一不匹配都会 fail closed，不会留下无法安装的 Runner 记录。
- manifest v2 固定 Runner `0.3.17`、Codex `0.146.0`、Python `3.11.13` 和 `self_contained=true`，并要求 `linux-amd64`、`linux-aarch64`、`macos-amd64`、`macos-aarch64` 四个平台资产及 installer 自身都有 HTTPS URL、SHA-256 和受限文件大小。Runner 的结构化结果 Schema 要求全部属性都在 `required` 中，`error_code` 成功时为 `null`、失败时为稳定错误码。
- Runner 在 Relay 返回 `controller_unavailable`、`controller_client_not_started` 或连接竞争时保留当前进程，并按配置执行指数退避重连；凭据错误和身份不匹配仍立即退出。已发送未 ACK 的心跳保留在持久 outbox，只替换未发送心跳；过期心跳清理后，其迟到 ACK 会安全忽略。Codex 执行在工作线程内进行，长任务期间 WSS 主循环持续发送 `busy` 心跳。
- Runner 启动 Codex 前会读取任务 worktree 与本机登记仓库的绝对 Git common dir；只有两者完全相同时，才把该 Git metadata 目录通过 Codex `--add-dir` 加入 `workspace-write`。这使 linked worktree 可以创建 index、object、ref 和 reflog 并完成本地 commit，但不会开放 `danger-full-access`，远端消息仍不能注入路径或修改沙箱策略。
- 创建 API 返回一次性 HTTPS 安装链接、完整的一行终端命令、平台/版本和过期时间，不返回独立 enrollment 字段。页面可复制链接、打开链接或复制命令；Clipboard API 不可用时使用受限回退。15 分钟倒计时归零、撤销或 enrollment 被领取后，页面立即清除内存中的链接和命令。
- Relay 的 `/install/<ticket>` 先通过独立 bearer 调用 Controller 的 `/internal/v2/runner-relay/install-bootstrap`。该检查只确认 enrollment 仍 pending、未过期、未撤销、未领取且 Runner 可安装，不消费 enrollment；真正的单次领取仍发生在 Runner 首次 WSS enroll。
- install-bootstrap 同时下发 Registry 当前的 `labels` 与 `policy_revision`。Runner 首次 enroll 必须回报未越过 Registry 的标签和完全一致的策略版本；安装脚本或本地配置擅自扩大标签、使用过期策略时不会领取长期凭据。
- 返回脚本使用 `Cache-Control: no-store`、`Referrer-Policy: no-referrer` 和 `X-Content-Type-Options: nosniff`，只下载 manifest 固定的 installer/资产并核对文件大小和 SHA-256。自包含 bundle 带固定 Python、Runner 和 Codex，不调用目标机 pip、venv、pyenv 或 Homebrew；目标机仍需 `curl`、`tar`、SHA-256 命令、Git 和已存在的项目工作区。
- enrollment 状态固定为 `pending`、`claimed`、`expired` 或 `revoked`。pending 可以撤销或重新生成；重新生成会吊销所有未领取旧 token。已领取长期凭据的 Runner 只能使用凭据轮换，不能重新生成 enrollment。相同 request ID 重放只返回脱敏状态，不恢复命令、token 或凭据。
- 页面可启用、停用、排空停用、紧急停用、轮换凭据和吊销删除。凭据轮换值仍只显示一次；删除只吊销并归档，不删除服务器、Agent、worktree、分支或 Codex Session。
- Scheduler 只向 `enabled + online + idle` 且项目、标签、能力和 policy revision 匹配的 Runner 原子分配一个 lease。已运行任务失联进入 `recovery_required`，禁止自动转移；未运行且 lease 过期的任务才可递增 assignment epoch 重新调度。
- `awaiting_confirmation` 表示 Controller 已经持久化结构化结果并释放 lease，只等待 owner 补充或取消。它不会因 Runner 后续离线进入 recovery；继续操作会在原 Runner/Session 上重新激活同一 lease。
- 旧 Runner 在 task 已进入终态或 `recovery_required` 后重发旧 Schema result 时，Controller 只在凭据、body digest、Runner/task、assignment epoch 和 lease 全部匹配后返回 `runner_late_message`；Relay 可仅对该明确不可覆盖结果 ACK。活动任务仍先执行完整当前 Schema 校验。
- 管理员在已核对 Runner 本地状态和 worktree 后，可调用 `POST /api/runners/<runner_id>/recovery-resolution`，正文仅接受 `task_id`、`resolution=confirmed_failed`、当前 Runner `revision` 和幂等 `request_id`。该操作仅保留并确认失败结果、释放 lease 和清除 Runner 的 recovery 忙状态；不删除或重放任务、worktree、分支或 Session。
- 页面与 API 不显示源码、diff、raw Codex JSONL、完整主机路径、私钥或独立 enrollment token；一次性 token 只存在于当前页面内存中的完整安装命令，列表、详情、状态、幂等重放和日志均不回显。写操作继续使用 HA 管理员 Ingress、短期 CSRF、revision 和 request ID。
- Controller 使用带独立 bearer 的固定内部 HTTP URL 向 Relay 发布 request/control；Relay 通过受 bearer 保护的 `/internal/v2/runner-relay/enroll`、`authenticate` 和 heartbeat/status/result 入口回传。Relay 只负责传输，Registry、enrollment、长期凭据、lease、审计和任务状态仍由 Controller 单独拥有。
- macOS 安装为当前桌面用户 LaunchAgent。由于没有 Developer ID 签名，首次运行可能仍需系统信任确认；这不属于 Python/Codex 运行环境缺失。

## 配置

| 配置项 | 说明 |
| --- | --- |
| `internal_api_token` | Weixin Gateway 调用作业 API 的独立 Token，至少 32 个字符 |
| `intake_enabled` | 是否接收新作业；默认关闭，正式切换前不得开启 |
| `auth_mode` | 显式选择 `chatgpt_device_code` 或 `api_key`；默认设备码，禁止自动降级 |
| `openai_api_key` | 仅在 `api_key` 模式使用的 Supervisor 私有 `password` option；页面和状态不会回显 |
| `openai_base_url` | API Key 模式的可选 Responses API 根地址；空值使用官方端点，完整值不在页面、状态或日志回显 |
| `codex_model` | API Key 模式的可选模型名；空值使用 Codex 默认模型，自定义端点需要固定模型时填写 |
| `ledger_base_url` | Renovation Hub 的固定内部服务根地址；为空时禁用兼容 Ledger 工具 |
| `ledger_api_token` | Ledger 独立 bearer；不会传给 app-server |
| `gateway_base_url` | Weixin Gateway 的固定内部服务根地址；用于图片非消费预览和工具一次性附件读取 |
| `gateway_attachment_token` | Gateway 附件 bearer；不会传给 app-server、模型或 Ledger |
| `max_media_bytes` | Gateway 到 Renovation Hub 流式媒体的单文件上限，默认 1 GiB |
| `operations_base_url` | HA Operations Broker 的固定内部服务根地址 |
| `operations_api_token` | Broker 独立 bearer；不会传给 app-server |
| `memo_base_url` | Node-RED 的固定内部 HTTP 根地址；只允许主机名和可选端口，不含 `/endpoint` 路径 |
| `memo_http_username` | Node-RED `http_node` Basic Auth 用户名；不会传给 app-server 或模型 |
| `memo_http_password` | Node-RED `http_node` Basic Auth 密码；Supervisor `password` option，不回显 |
| `memo_api_token` | 家庭备忘录写 API Token；通过 `X-Family-Memo-Token` 发送，不进入 URL |
| `max_request_bytes` | 单个内部 JSON 请求上限 |
| `max_queue` | 排队与恢复中作业数量上限 |
| `max_result_chars` | 保存并返回微信的最终文本上限 |
| `runner_center_v2_enabled` | 是否启用 Runner Center v2 API、页面和调度；默认开启，显式 false 可降级 |
| `runner_online_seconds` | 心跳保持 online 的新鲜阈值 |
| `runner_offline_seconds` | 超过该阈值后判定 offline；中间状态为 stale 且禁止新任务 |
| `runner_lease_ttl_seconds` | 单次 assignment lease TTL，默认 600 秒；有效 active-task heartbeat 会原子续期任务与 active lease，已运行任务仅在 Runner offline 且 lease 过期后进入人工恢复，绝不自动转移 |
| `runner_task_ttl_seconds` | 等待或执行任务的总 TTL；终态与 recovery 仍按状态机处理 |
| `runner_relay_base_url` | Relay Add-on 固定内部 HTTP 根地址，必须带显式端口且不能包含路径；与两个最小权限 token 同时配置 |
| `runner_relay_api_token` | 仅供 Controller -> Relay 发布 request/control 的 bearer，至少 32 字符 |
| `runner_relay_controller_api_token` | 仅供 Relay -> Controller 调用 enroll/authenticate/heartbeat/status/result 的 bearer，至少 32 字符 |
| `runner_relay_public_url` | Runner 出站连接的公开 `wss://` URL；禁止凭据、query、fragment、内部域名或非公网解析结果 |
| `runner_installer_manifest_url` | Runner `0.3.17` 自包含安装制品 manifest v2 的公开 HTTPS URL |
| `runner_installer_manifest_sha256` | 对 manifest 原始字节固定的 64 位小写 SHA-256；不接受浮动 latest |
| `runner_relay_timeout_seconds` | Relay 发布超时；兼容未内置 manifest 的旧目录读取，范围 2 到 60 秒 |

`runner_relay_base_url`、`runner_relay_api_token` 和 `runner_relay_controller_api_token` 必须同时为空或同时配置，且两个 token 不得相同；任一缺失、过短、URL 非精确 Add-on 地址或身份复用都会拒绝启动。两个 token 均不进入页面、状态或日志，也不得复用 Gateway `internal_api_token`。

内部服务地址只允许 `http://` 加固定主机名和可选端口，不允许用户信息、路径、查询、片段或 IP 字面量。模型不能提交或改变目标 URL。

## 认证

先在 Add-on options 中显式选择认证模式。运行时不会从一种模式自动降级或切换到另一种模式。

ChatGPT Device Code：

1. 设置 `auth_mode=chatgpt_device_code`，通过 Home Assistant Ingress 打开 Controller 页面。
2. 点击“开始设备码登录”。Controller 只会向官方 app-server 发送 `{"type":"chatgptDeviceCode"}`。
3. 在页面显示的官方验证地址登录与本机 Codex 相同的 ChatGPT 账号，并输入短期用户码。
4. Controller 读回账户状态；只有账户类型为 `chatgpt` 才标记为已就绪。

API Key：

1. 设置 `auth_mode=api_key`，把 Key 直接填入 Add-on options 的 `openai_api_key`；不要通过微信、Ingress、日志或聊天发送。
2. `openai_base_url` 留空时使用 OpenAI 官方 API；需要中转时填写完整公开 HTTPS Responses API 根地址，例如 `https://api.example.com/v1`。不要把 Key、query 或 fragment 放进 URL。
3. 自定义 URL 会拒绝 HTTP、userinfo、query、fragment、控制字符、localhost、HA 内部服务名、私网/回环/链路本地/保留地址和解析到任一非公网地址的域名；仅支持 Responses，不会降级为 Chat Completions。
4. `codex_model` 留空时使用 Codex 默认模型；自定义端点要求固定模型时填写其 Responses 兼容模型名。
5. 启动时 Controller 会在未登录状态下调用官方 `account/login/start {"type":"apiKey","apiKey":"..."}`，但不会把 Key 放进 app-server 子进程环境或命令行。
6. Controller 随后读回 `account.type=apiKey`；类型不匹配、Key 缺失、URL/模型无效或请求失败时，任务入口保持关闭。
7. 修正 options 后可重启 Add-on，或在 Ingress 点击“重试 API Key 登录”。页面只显示 Key/URL 是否已配置和端点/模型模式，不显示 URL 或 Key。

Controller 与本机 Codex 是两个独立会话。不要复制本机 Token、Cookie 或 `CODEX_HOME`，也不要通过微信发送设备码或任何凭据。

正式设备码或 API Key 登录会改变外部账号会话并可能产生 API 计费；安装源码或通过本地测试不等于已经登录或通过真实模型验收。

## 队列与恢复

- 同一 `message_id` 重投返回原作业；同一 ID 携带不同正文时拒绝。
- 多个微信会话分别映射到持久 Codex Thread。
- 全局只有一个 `running` 作业；其他作业按创建顺序排队。
- 作业在发起 `turn/start` 前先进入受保护运行态。请求超时、进程退出或重启导致副作用未知时，作业进入 `recovery_required`。
- 任一 `recovery_required` 会阻断后续 queued 作业调度；必须先核对 Turn、Ledger 幂等记录或 Broker 收据，不能自动重放。
- 核对完成后，由管理员使用内部 bearer 调用 `POST /internal/v1/jobs/<job_id>/recovery-resolution`，正文只允许 `{"resolution":"confirmed_completed"}`、`confirmed_failed` 或 `cancelled`。结论会写入作业事件台账，然后队列才可继续。
- app-server 进程退出、未初始化或发生协议错误时，`intake_enabled` 立即失效且 `/healthz` 返回 `503`；仅尚未登录或认证模式待修正、但 app-server 运行正常时返回 `200`，避免登录期间被 watchdog 反复重启。

## 工具代理

app-server 只启动一个无秘密的本地 MCP 进程。MCP 通过 `/data/runtime/tool-proxy.sock` 把当前动态目录中的工具调用交给 Controller 主进程，再由主进程使用各自 bearer 访问 Renovation Hub 或 Broker。

Operations 工具继续由 Controller 本地定义；Renovation Hub 工具从受认证 `GET /internal/v1/mcp/manifest` 获取完整 Schema、中文名称、风险、transport 和 annotations。Controller 只接受固定 `service=renovation_hub`、`scope=business`、`ledger_*` / `renovation_*` 命名空间、封闭 Schema、允许的 transport 和正确 digest。SQLite 保存 last-good manifest、全局 catalog revision、逐工具开关、管理幂等、真实 MCP 目录心跳和最多 1000 条脱敏调用审计；审计不保存参数、返回正文或凭据。

启动时先加载 last-good；不存在时使用内置 bootstrap。后台刷新取得新的合法 digest 后原子更新目录和 revision，并触发 `notifications/tools/list_changed`。Hub 暂时不可达、返回非法 digest/Schema/transport 或撤回工具时不会清空 last-good；撤回工具不再发布，既有关闭策略在其未来重新出现时仍保持关闭。

MCP `tools/list` 只返回“内部服务已配置且策略开启”的交集，并把本次实际发布目录回报给主进程。策略 revision 变化后 MCP 发出标准 `notifications/tools/list_changed`；在 app-server 刷新目录前页面显示“等待 MCP 刷新”。无论目录是否刷新，`tools/call` 都会重新读取当前策略，关闭工具立即返回 `tool_disabled`。

已加载 Thread 只在 developer instructions 指纹未变化时复用。角色或有效工具上下文变化时，Controller 会替换当前 conversation 的 Thread：刚由 `/new` 或首次接入创建、尚未发生 Turn 的空 Thread 在官方 Codex `0.146.0` 中没有可 fork 的 rollout，因此重新执行 `thread/start`；已经发生 Turn 并持久化的 Thread 才执行官方 `thread/fork` 保留既有历史。两条路径都会生成新的 `TH-*`，避免旧角色或旧工具提示继续影响下一轮。

Gateway 作业缺少 `capability_profile` 时按旧版唯一 owner 兼容为 `owner_legacy`；新版 owner 使用 `owner`。`member_read_only` 只允许 `ledger_show`、`ledger_query`、`ledger_summary`、`renovation_dashboard`、`renovation_project_list`、`renovation_stage_list`、`renovation_area_list`、`renovation_timeline` 和 `memo_list`，其他账本/备忘录写入、导出、媒体和 Operations 在 Controller 服务端返回 `tool_not_allowed_for_profile`。

私有 `config.toml` 为当前目录中的每个内部 MCP 工具写入单工具 `approval_mode="approve"`，同时 Thread/Turn 保持 `approvalPolicy=never`。运行期新增合法 Hub 工具会在 app-server 重新 `tools/list` 后出现，无需重启；这只消除微信无法响应的 Codex UI 审批步骤，不会因此获得额外业务权限。

账本和家庭备忘录只读工具仍带明确的只读、非破坏、幂等和封闭世界 annotations。写工具只允许当前 active owner 作业/Turn，Controller 生成稳定幂等键，Renovation Hub 与 Node-RED 继续执行各自字段校验、幂等、审计和业务约束；member 即使看见同一 app-server 配置，也只能调用 8 个装修 allowlist 查询和 `memo_list`。

app-server 本身继续运行在不继承宿主 `PYTHONPATH` 的净化环境中；MCP 配置仅为固定的本地代理进程注入 `/opt/codex-controller`，避免外部 Python 路径进入模型进程，同时保证官方 app-server 的真实 `tools/list` 能装载工具目录。

MCP 目录会按实际配置过滤：Renovation Hub 或 Operations Broker 的 URL/Token 未配置完整时，对应工具不会暴露。所有写工具必须处于当前 Controller 作业与 Turn 上下文中；Controller 忽略模型提供的写入幂等键，改用微信 `message_id`、工具名与排序后的规范化参数计算稳定 SHA-256。Turn 完成或启动失败后上下文立即清除。

微信图片在创建 Turn 前由 Controller 主进程从 Gateway 的受认证预览接口读取，严格核对作业元数据、MIME、大小和 SHA-256，再以随机受控文件名写入 `/data/turn-media/<job-id>/`。官方 app-server 收到 `{"type":"localImage","path":"...","detail":"auto"}`；Turn 完成、明确失败或 Controller 重启时清理私有暂存。预览不会消费原 `attachment_ref`，因此模型识别图片后仍可调用装修归档工具保存同一原件。

当前 `localImage` 只接受 JPEG、PNG 和 WebP。其他附件仍以受控引用元数据进入 Turn，正文只允许由已配置的确定性工具读取；不把 bearer、内部 URL 或任意宿主路径交给模型。

### 装修统计图 artifact

- `ledger_generate_chart` 只能在活动 Controller 作业上下文中用于自动微信交付。Hub 返回后，Controller 只接受 `summary-<32 hex>.png` 固定引用，并从受认证 `/internal/v1/downloads/chart/<ref>` 读取。
- Controller 同时核验 HTTP `image/png`、声明长度、20 MiB 上限、PNG 文件签名、Hub `size_bytes` 和 SHA-256。任一不一致都会让工具调用失败，不把未校验图片交给 Gateway。
- 通过校验的图片原子写入 `/data/job-artifacts`，目录 `0700`、文件 `0600`；默认 TTL 24 小时、总配额 100 MiB、每作业最多 4 个。同一 job + SHA-256 幂等复用，过期与孤立文件自动清理。
- completed job 新增 `result_summary` 和 `artifacts[]`。DTO 不含 Hub 引用、bearer 或 Controller 路径；Gateway 内部读取使用既有 Controller bearer，失败下载使用 `/downloads/artifacts/<opaque-token>`。
- opaque token 由 Controller 私有 HMAC 密钥和 artifact/到期时间派生，SQLite 只保存 token SHA-256。该路径只应经 HA Ingress 暴露；不新增宿主端口、匿名对象存储或 NPM 路由。
- 模型工具结果不含 `fallback_path`，developer instructions 明确禁止输出 `download_ref`、内部 URL、路径、Bearer 或 Base64。图片、摘要和失败链接的最终顺序由 Gateway 决定。

`ledger_attach` 保留 Legacy Ledger v1 桥接：模型只能提交 Gateway 生成的 `attachment_ref`。Controller 主进程使用独立 bearer 一次性读取附件，校验文件名、MIME、大小和 SHA-256，再转换为旧账本需要的 Base64 内容；第一版 Legacy 单附件限制为 20 MiB。

`renovation_media_ingest` 用于新图片/视频档案。收到附件默认只用于本轮识别，不自动归档；Controller 只有在作业文本明确请求归档到装修/施工/工地档案，或明确保存询价、报价单、价格单、供应商名片、商品规格资料时才暴露该工具，并在调用时再次执行服务端门禁。Controller 先使用幂等键和引用摘要查询 Hub 是否已有结果；未命中时从 Gateway 的非消费流式接口读取正文，直接转发到 Renovation Hub，Hub 成功后再 ACK 消费 Gateway 引用。Hub 失败时原引用保持可重试，不要求用户重新发送。该链路不会构造 Base64 JSON，不把 `attachment_ref`、bearer、内部 URL、路径或媒体正文交给 app-server 和模型，单文件上限由 `max_media_bytes` 控制。

Hub `0.3.0` 的九个 `renovation_quote_*` 工具通过受认证动态 manifest 自动进入 owner/owner_legacy 的微信工具目录，支持创建询价、增加与修改供应商报价、列表、详情、比较、选择和媒体关联。选择报价只写 Hub 报价表，不创建 Ledger 流水；图片识别出的供应商、地址、规格和价格建议先保持 `review_required`，由用户复核后再选择。member 的固定只读 allowlist 不随动态目录扩张。

Broker 工具固定为重启提案、Passkey 授权请求/状态、执行和执行状态查询。它们不依赖 Codex UI 审批，但是否允许真正执行仍由 Broker 的不可变提案、Passkey、固定 allowlist、一次性收据、状态机和默认关闭的 `execution_enabled` 决定；Controller 不能绕过这些门禁或提交任意 Supervisor 动作。

## 更新

Codex 版本在 `package.json` 与锁文件中固定。候选更新必须重新生成 Schema，比较认证、Thread、Turn 和通知字段，完成协议测试、队列恢复、MCP 回归和双架构构建后才能发布。正式 HAOS 升级另行确认。

镜像构建直接下载 npm 官方注册表中的 Linux 平台包，并使用锁文件对应的 SHA-512 校验；运行镜像只包含原生 Codex 二进制，不安装 Node 或 npm。升级版本时必须同步更新 `package.json`、`package-lock.json`、Dockerfile 中两个 Linux 平台摘要和真实 app-server smoke 基线。

## 回滚

1. 关闭新作业入口并记录活动作业、队列和 `recovery_required`。
2. 停止 Controller，保留 `/data/controller.sqlite3` 与 `/data/codex-home` 冷备份。
3. 恢复上一镜像与对应数据备份。
4. 核对账户类型、Thread 数、队列、已完成结果和未执行写操作。

从 `0.5.17` 回滚时，只允许恢复升级前 Controller-only 备份中精确记录的 M8 `0.5.16` 源码树与运行摘要 `70267329cfdb18e7aa6331ae942e7cb0779cb51f44b282c0fe1ab0cfe108f7b0`；不能仅指定版本号。回滚不修改 Runner credential、options、数据目录、Mac refs store 或任何 Codex 原 Thread，并必须在恢复后重新核对运行源码摘要。`0.5.16` 内置 Runner `0.3.11` manifest，回滚时必须同时恢复 Relay `0.2.10` 与 Runner `0.3.11` 的精确兼容边界；不得为保留 `0.3.12` 安装入口而放宽摘要或版本校验。

如果有意继续降级到不含 Desktop 的 `0.5.10`，先关闭 Desktop 页面入口和 Runner 的 `[desktop].enabled`，等待所有 Desktop 命令离开 `pending/submitted/accepted/unknown`，确认 Relay 与 Runner Desktop outbox 已排空，再同时回退 Relay 到 `0.2.7` 和 Runner manifest 到 `0.3.5`。旧版会忽略 additive Desktop 表，但不得删除 Controller 数据目录、Desktop 审计、Mac 本地 refs store 或任何 Codex 原 Thread；未知收据必须保留为只读核对证据。

从 `0.5.10` 回退到 `0.5.9` 会重新引入 `awaiting_confirmation` 被离线 sweep 误判为 recovery、以及孤立 recovery 无法审计收口的问题；回退前先确认没有等待确认或 recovery 任务。继续从 `0.5.9` 回退到 `0.5.8` 会失去 heartbeat 原子续租和“offline 且 lease 过期后才恢复”的保护，并把新安装默认 lease 恢复为 60 秒；回退前先停止新增 `/work`，等待活动任务结束并确认 Runner/Relay outbox 已排空。继续从 `0.5.8` 回退到 `0.5.7` 会恢复 Runner `0.3.4` manifest，并失去进程内临时认证重连、慢心跳 ACK 保留和长任务稳定心跳修复；回退前先确认没有活动长任务，且 Runner/Relay outbox 已排空。从 `0.5.7` 回退到 `0.5.6` 会失去旧 Schema result 的安全 late ACK 与 Runner recovery 确认失败 API。回退前先确认 Relay outbox 无积压，且没有待核对的 `recovery_required` 任务。继续从 `0.5.6` 回退到 `0.5.5` 会恢复 Runner `0.3.3` manifest；该 Runner 可以执行模型并产生文件，但 linked worktree 无法在 `workspace-write` 中写入仓库外的 Git metadata，因此不得再分配要求本地 commit 的任务。继续从 `0.5.5` 回退到 `0.5.4` 会恢复 Runner `0.3.2` manifest；该版本的结构化结果 Schema 与当前 Codex API 不兼容，不得再分配真实任务。继续从 `0.5.4` 回退到 `0.5.3` 会恢复启动期在线读取 manifest；若 HAOS 无法访问 GitHub，页面安装入口将关闭。继续从 `0.5.3` 回退到 `0.5.2` 前先关闭新增任务入口并恢复 Runner `0.3.1` 的固定 manifest；从 `0.5.2` 回退到 `0.5.1` 前核对没有活动 lease 或 `recovery_required` 任务。从 `0.5.1` 回退到 `0.4.2` 前还要撤销或等待所有一次性安装链接过期、停止 `/install/` 外部流量，并同时恢复 Relay `0.1.1` 与 Runner `0.2.0` manifest 配置。不要删除已注册 Runner、数据库审计、服务器 worktree 或 Codex Session。

继续从 `0.4.2` 回退到 `0.3.1` 前，必须等待所有旧式未领取命令超过 15 分钟有效期，或把对应 Runner 吊销归档；`0.3.1` 不识别 enrollment 的 `revoked_at`。只设置 `runner_center_v2_enabled=false` 不能让旧版理解撤销状态。

继续从 `0.3.1` 回退到 `0.2.4` 前保持 `runner_center_v2_enabled=false`，确认没有 v2 活动 lease 或 `recovery_required` 任务。旧版本会忽略 additive Runner 表；不要删除 Controller 数据目录、Runner 审计或服务器上的 worktree/Session。普通 job/Thread/MCP 数据结构保持兼容。

使用自定义 URL 回滚时，同时清空 `openai_base_url` 和 `openai_api_key` 或恢复升级前备份；不要把旧 Key 复制到普通文件。

当前旧 Hermes iLink 身份已经失效，不能作为微信恢复目标。回滚 Controller 时应关闭 intake、保留队列和新 Gateway 身份；微信恢复必须修复当前 Gateway 或重新扫码并重新绑定。

从 `0.2.4` 回退到 `0.2.3` 不需要数据库迁移；先关闭 intake 并排空活动作业，确认没有正在流式读取或等待 ACK 的装修媒体。回退后普通附件仍可用于识别，但旧版没有“明确装修档案意图后才暴露工具”的服务端门禁，也没有 Hub 成功后再消费 Gateway 引用的链路。

从 `0.2.3` 回退到 `0.2.2` 不需要数据库迁移；回退会恢复 Goals 可用和通用 Hub HTTP 错误语义，因此应先关闭 intake、排空活动作业并确认没有依赖持续监控的请求。若继续回退到 `0.2.1`，旧版本会忽略 additive `result_summary`、`job_artifacts` 表和私有 artifact 文件；不要删除 `/data/job-artifacts`，待确认没有未发送或仍需下载的图片后再单独清理。
