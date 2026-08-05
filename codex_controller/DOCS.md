# Codex Controller 使用说明

当前版本：`0.2.1`。

## 通用微信会话

- 微信入口默认是通用 Codex 助手，可处理普通问答、讨论、分析、写作、规划和其他不需要外部执行的任务。
- 不会把所有消息默认解释为装修事项，也不会因为消息来自现有 Hermes/iLink 身份就自动调用账本。
- 只有用户意图确实需要装修账本或 Home Assistant 操作时，才调用对应的结构化 MCP 工具；Codex UI 不再二次审批固定内部工具，但角色权限、逐工具策略、幂等、writer 和 Operations 执行边界仍由各组件服务端强制。
- 每次新建或恢复持久 Thread 都会重新注入当前 developer instructions、只读 sandbox 和 `approvalPolicy=never`。即使历史会话曾讨论 Mac 代理、Hermes 或旧迁移状态，当前能力也必须以本轮 MCP 工具目录和实际调用结果为准。
- 微信 owner 发送无附件且文本精确为“打开新会话”或 `/new` 时，Controller 会在既有队列与幂等门禁内创建新 Thread，并返回确定性确认。近似文本或带附件消息不会触发重置；旧 Thread 不删除，下一条普通消息才进入新 Thread。
- 新 Thread 在当前 app-server 进程中已经处于加载状态，下一条消息不会重复调用 `thread/resume`；Controller/app-server 重启后进程内状态清空，持久 Thread 才会重新执行一次安全恢复。
- Ingress 的 MCP 工具控制台显示当前静态 Operations 工具与 Renovation Hub 动态 manifest 的并集，并分别展示内部服务配置、管理员策略、MCP 进程真实 `tools/list` 发布状态和当前可调用状态；不会显示 URL、bearer、完整 Thread 或会话标识。
- 工具旁的自然语言意图只是能力示例，不是固定关键词。Codex 根据整句话语义和本轮目录决定是否调用；普通讨论仍可直接回答。
- 管理员可逐工具开启或关闭。页面写请求必须同时携带短期 CSRF token、JSON、当前 catalog revision 和随机 request ID；并发旧 revision 会被拒绝，相同 request ID 的相同正文幂等返回原结果。
- `/new` 确认和内部作业状态会返回稳定 `TH-*` 短标识，便于排查旧 Thread；完整 Thread、Turn 和 conversation key 不进入页面 DTO。
- Renovation Hub 工具已配置时，账本是否连接、当前支出、汇总和明细问题必须先调用 `renovation_dashboard`、`ledger_summary`、`ledger_query` 等只读工具；用户自然语言提出查询、查看、核验、汇总或明细请求，即授权本次无副作用只读调用，不需要 Passkey、写入确认或额外征求授权。不得仅凭历史回复声称“未连接”，也不得要求用户重新发送已有账目。
- 对 owner，清晰的图表、导出、记账、退款、更正、撤销、导入检查和装修媒体/事件归档请求也视为本次匹配工具调用授权，不再询问“是否确认/授权”。只有缺少必填字段或语义确有多种合理解释时才澄清；讨论、假设、举例和方案比较不能推断为写入命令。

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
| `max_request_bytes` | 单个内部 JSON 请求上限 |
| `max_queue` | 排队与恢复中作业数量上限 |
| `max_result_chars` | 保存并返回微信的最终文本上限 |

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

Gateway 作业缺少 `capability_profile` 时按旧版唯一 owner 兼容为 `owner_legacy`；新版 owner 使用 `owner`。`member_read_only` 只允许 `ledger_show`、`ledger_query`、`ledger_summary`、`renovation_dashboard`、`renovation_project_list`、`renovation_stage_list`、`renovation_area_list` 和 `renovation_timeline`，其他账本写入、导出、媒体和 Operations 在 Controller 服务端返回 `tool_not_allowed_for_profile`。

私有 `config.toml` 为当前目录中的每个内部 MCP 工具写入单工具 `approval_mode="approve"`，同时 Thread/Turn 保持 `approvalPolicy=never`。运行期新增合法 Hub 工具会在 app-server 重新 `tools/list` 后出现，无需重启；这只消除微信无法响应的 Codex UI 审批步骤，不会因此获得额外业务权限。

账本只读工具仍带明确的只读、非破坏、幂等和封闭世界 annotations。写工具只允许当前 active owner 作业/Turn，Controller 生成稳定幂等键，Renovation Hub 继续执行字段校验、单 writer、审计和业务约束；member 即使看见同一 app-server 配置，也只能调用 8 个 allowlist 查询。

app-server 本身继续运行在不继承宿主 `PYTHONPATH` 的净化环境中；MCP 配置仅为固定的本地代理进程注入 `/opt/codex-controller`，避免外部 Python 路径进入模型进程，同时保证官方 app-server 的真实 `tools/list` 能装载工具目录。

MCP 目录会按实际配置过滤：Renovation Hub 或 Operations Broker 的 URL/Token 未配置完整时，对应工具不会暴露。所有写工具必须处于当前 Controller 作业与 Turn 上下文中；Controller 忽略模型提供的写入幂等键，改用微信 `message_id`、工具名与排序后的规范化参数计算稳定 SHA-256。Turn 完成或启动失败后上下文立即清除。

微信图片在创建 Turn 前由 Controller 主进程从 Gateway 的受认证预览接口读取，严格核对作业元数据、MIME、大小和 SHA-256，再以随机受控文件名写入 `/data/turn-media/<job-id>/`。官方 app-server 收到 `{"type":"localImage","path":"...","detail":"auto"}`；Turn 完成、明确失败或 Controller 重启时清理私有暂存。预览不会消费原 `attachment_ref`，因此模型识别图片后仍可调用装修归档工具保存同一原件。

当前 `localImage` 只接受 JPEG、PNG 和 WebP。其他附件仍以受控引用元数据进入 Turn，正文只允许由已配置的确定性工具读取；不把 bearer、内部 URL 或任意宿主路径交给模型。

`ledger_attach` 保留 Legacy Ledger v1 桥接：模型只能提交 Gateway 生成的 `attachment_ref`。Controller 主进程使用独立 bearer 一次性读取附件，校验文件名、MIME、大小和 SHA-256，再转换为旧账本需要的 Base64 内容；第一版 Legacy 单附件限制为 20 MiB。

`renovation_media_ingest` 用于新图片/视频档案。Controller 先使用幂等键和引用摘要查询 Hub 是否已有结果；未命中时再从 Gateway 以二进制流读取正文，并直接转发到 Renovation Hub。该链路不会构造 Base64 JSON，不把 `attachment_ref`、bearer、内部 URL、路径或媒体正文交给 app-server 和模型，单文件上限由 `max_media_bytes` 控制。

Broker 工具固定为重启提案、Passkey 授权请求/状态、执行和执行状态查询。它们不依赖 Codex UI 审批，但是否允许真正执行仍由 Broker 的不可变提案、Passkey、固定 allowlist、一次性收据、状态机和默认关闭的 `execution_enabled` 决定；Controller 不能绕过这些门禁或提交任意 Supervisor 动作。

## 更新

Codex 版本在 `package.json` 与锁文件中固定。候选更新必须重新生成 Schema，比较认证、Thread、Turn 和通知字段，完成协议测试、队列恢复、MCP 回归和双架构构建后才能发布。正式 HAOS 升级另行确认。

镜像构建直接下载 npm 官方注册表中的 Linux 平台包，并使用锁文件对应的 SHA-512 校验；运行镜像只包含原生 Codex 二进制，不安装 Node 或 npm。升级版本时必须同步更新 `package.json`、`package-lock.json`、Dockerfile 中两个 Linux 平台摘要和真实 app-server smoke 基线。

## 回滚

1. 关闭新作业入口并记录活动作业、队列和 `recovery_required`。
2. 停止 Controller，保留 `/data/controller.sqlite3` 与 `/data/codex-home` 冷备份。
3. 恢复上一镜像与对应数据备份。
4. 核对账户类型、Thread 数、队列、已完成结果和未执行写操作。

使用自定义 URL 回滚时，同时清空 `openai_base_url` 和 `openai_api_key` 或恢复升级前备份；不要把旧 Key 复制到普通文件。

当前旧 Hermes iLink 身份已经失效，不能作为微信恢复目标。回滚 Controller 时应关闭 intake、保留队列和新 Gateway 身份；微信恢复必须修复当前 Gateway 或重新扫码并重新绑定。
