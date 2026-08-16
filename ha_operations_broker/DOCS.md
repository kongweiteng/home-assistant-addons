# HA Operations Broker 使用说明

## 当前状态

版本 `0.5.3` 提供只读预检、Broker 自有 Passkey 授权根、默认关闭的 restart-only 执行器、恢复/备份证据凭据隔离、策略/适配器/基线/证据绑定和持久租约，并兼容 HAOS Add-on 信息接口省略 `installed` 字段的响应。Ingress iframe 内只用于核对提案并引导打开同一会话、带显式顶层上下文标记的安全窗口；Passkey 注册与签名固定在该顶层 WebAuthn 上下文中完成。唯一实现的写动作仍是 `restart_addon`；HACS、官方 Integration、页面整理、备份、Recorder、缓存和其他 Add-on 生命周期动作仍未实现。

源码与本地测试通过不等于已经在正式 HAOS 完成权限、Passkey、重启或恢复验收。

## 配置

- `broker_api_token`：至少 32 字符的内部 bearer，只保存在 Add-on 私有 options。
- `recovery_api_token`：至少 32 字符且必须与普通 bearer 不同，只供独立恢复 helper 使用；Controller 不需要也不应持有。
- `backup_evidence_api_token`：至少 32 字符且必须与前两个 bearer 都不同，只供备份验证 helper 登记/查询结构化证据；Controller、模型和 recovery helper 不持有。
- `trusted_owner_identity_hashes`：旧 P4 envelope 兼容预检所需的结构性 owner 哈希；Broker 原生提案不依赖它批准执行。
- `webauthn_rp_id` / `webauthn_allowed_origins`：稳定且精确的 HTTPS WebAuthn RP 与 origin。
- `passkey_enrollment_token`：临时注册令牌；完成注册后必须清空并重启。
- `passkey_challenge_ttl_seconds`：一次性 challenge 有效期，60–600 秒。
- `proposal_ttl_seconds`：Broker 原生提案和对应授权收据有效期，60–1800 秒。
- `policy_epoch` / `policy_hash`：当前 restart-only 策略的版本与 SHA-256 指纹。
- `adapter_version` / `adapter_schema_version`：固定重启适配器及其 schema 版本。
- `lease_ttl_seconds`：持久租约心跳有效期，5–300 秒；过期不会自动接管或重放。
- `manager_shadow_enabled`：是否启用独立 Manager Executor 只读 shadow；默认 `false`。
- `manager_executor_base_url` / `manager_executor_api_token`：shadow 的固定 Add-on 内部地址和独立 bearer；不传给 Controller 或模型。
- `execution_enabled`：总执行开关，默认 `false`。
- `enabled_actions`：动作开关，当前只允许空列表或仅包含 `restart_addon`，默认空。
- `restart_addon_allowlist`：可重启的精确 Add-on slug，默认空。

即使 `hassio_role` 为 `manager`，下面三个执行条件缺一不可：

1. `execution_enabled: true`
2. `enabled_actions: [restart_addon]`
3. 目标 slug 位于 `restart_addon_allowlist`

## Broker 原生提案

`POST /v1/proposals` 使用内部 bearer，只接受以下精确 JSON：

```json
{
  "version": 1,
  "action_type": "restart_addon",
  "target": "example_addon",
  "idempotency_key": "sha256:64位小写十六进制"
}
```

不接受 `parameters`、policy、allowlist、adapter、baseline、backup evidence、URL、路径、命令、配置、自由文本或其他动作。Broker 自行读取白名单目标，选择覆盖完整 proposal/receipt 有效期且 baseline 匹配的结构化备份证据，生成 `action_id`、风险、备份要求、验证/回滚说明、到期时间和 version 2 `proposal_hash`，并将所有安全绑定持久化。没有合格证据时返回 `backup_evidence_required`，不会创建可授权 proposal。相同幂等键与相同最小 intent 返回原不可变提案；执行前仍重新核对当前绑定和基线。

## 结构化备份证据

`POST /v1/backup-evidence` 只接受独立 `backup_evidence_api_token` 和以下精确 JSON：

```json
{
  "version": 1,
  "scope": "addon",
  "logical_id": "opaque-backup-id",
  "completed": true,
  "created_at": "2026-08-05T00:00:00+00:00",
  "size": 1,
  "sha256": "sha256:64位小写十六进制",
  "off_device_sha256": "sha256:64位小写十六进制",
  "readable": true,
  "baseline": "sha256:64位小写十六进制",
  "expires_at": "2026-08-06T00:00:00+00:00"
}
```

- `scope` 只允许 `full|addon|dashboard|recorder`；当前 restart-only proposal 优先选择精确 `addon`，没有时允许 baseline 匹配的 `full` 完整备份证据。
- `completed/readable` 必须为 `true`，`size` 必须为正整数，三个 hash 必须为标准 SHA-256，证据必须已创建且未过期。
- 不接受路径、URL、备注、凭据、备份内容或附加字段；登记结果只保存结构化摘要。
- 相同 `logical_id` 和完全相同内容为幂等成功；相同 ID 的不同内容返回 `409 backup_evidence_conflict`。当前没有 update/delete 接口，数据库 trigger 也拒绝 UPDATE/DELETE。
- `GET /v1/backup-evidence/<logical_id>` 使用同一独立 bearer 查询结构化摘要。普通 Broker bearer、recovery bearer 和 Ingress 均返回 401。

## Passkey 授权

1. `POST /v1/authorization/requests` 提交 `{"version":1,"action_id":"..."}`。
2. HA 管理员从 Ingress 打开对应 `approval_id`，核对提案后点击“在安全窗口中打开”。
3. 同一已登录会话的顶层安全窗口保留原 `approval_id`，已注册的 Passkey 在精确 RP/origin 下完成用户验证；不得绕过 Touch ID 或安全密钥。
4. Broker 生成绑定 action、proposal hash、HA 用户哈希、credential 哈希和有效期的一次性收据。

授权本身不会调用 Supervisor。浏览器也没有执行按钮；执行只能由内部 bearer API 发起。

## 执行 API

`POST /v1/executions` 只接受：

```json
{
  "version": 1,
  "receipt_id": "RCPT-...",
  "action_id": "OPS-...",
  "proposal_hash": "sha256:...",
  "idempotency_key": "sha256:..."
}
```

执行过程：

1. 校验运行开关、动作开关、目标白名单、当前 policy/allowlist/adapter/schema 指纹和不可变 backup evidence。
2. 精确重放直接返回既有 execution，不读取或调用 Supervisor。
3. 读取 `/addons/<slug>/info` 并计算 baseline etag；漂移或无效状态在收据消费前拒绝。
4. 启用 Manager shadow 时，使用独立内部 bearer 调用固定 shadow endpoint，再次证明 action/target/adapter/baseline 完全一致；该步骤只读且发生在收据消费前。
5. 在同一 SQLite `BEGIN IMMEDIATE` 事务中重新查询 evidence，复核 ID/scope/baseline/完成/可读/有效期及 proposal/receipt 绑定，然后消费收据、建立 `authorized` execution，并取得 `singleton:operations` 与 `addon:<slug>` 两把持久租约。
6. 心跳续租后由当前唯一实际执行器调用固定 `/addons/<slug>/restart`。
7. 再次读取精确 Add-on 信息，要求状态恢复为 `started` 且版本未变化；成功或写前失败释放租约，结果未知则租约和 execution 一并进入 recovery。

`GET /v1/executions/<action_id>` 查询持久状态。相同执行请求重放只返回既有记录，不重复调用 Supervisor；即使系统中存在未解决 recovery，同一 action 的精确重放仍优先返回原记录。

## 状态与恢复

- `authorized`：收据已单次消费，尚未开始 Supervisor 写调用。
- `executing`：正在调用固定重启端点。
- `verifying`：重启调用已返回，正在执行后验。
- `succeeded`：后验通过。
- `failed`：写调用前的预检失败，没有发出重启。
- `recovery_required`：写调用或后验结果不确定、后验不匹配，或 Broker 在中间状态重启。状态响应包含脱敏的 `recovery` 元数据。

Broker 启动时不会自动恢复或重放中间状态。任何 `authorized/executing/verifying` 记录都会变为 `recovery_required`。任一未解决记录存在时，所有新 execution 返回 `409 unresolved_recovery`，且新收据不会被消费。

### 内部恢复结论 API

`POST /v1/executions/<action_id>/recovery-resolution` 只接受独立 `recovery_api_token`，普通 `broker_api_token` 固定返回 401，只接受精确 JSON：

```json
{
  "version": 1,
  "resolution": "confirmed_healthy",
  "evidence_hash": "sha256:64位小写十六进制"
}
```

- `resolution` 只允许 `confirmed_healthy` 或 `compensated`。
- `confirmed_healthy` 表示独立只读检查已经证明目标和业务不变量健康。
- `compensated` 表示已经通过另行授权的人工恢复步骤恢复健康。
- `evidence_hash` 只保存证据材料的 SHA-256 标识，不接受备注、URL、路径、命令或其他自由字段。
- API 只原子记录 `resolution`、`evidence_hash` 和 `resolved_at`；不会调用 Supervisor、不会重放 restart，也不会把原 execution 的 `state=recovery_required` 改写为 `succeeded`。
- 同一 recovery 只允许记录一次；并发请求最多一个成功，其余返回 `409 recovery_already_resolved`。
- 非 `recovery_required` execution 返回 `409 recovery_not_required`。记录有效结论后，只有在数据库中不存在其他未解决 recovery 时，新 execution 才能继续。
- 该接口是运维内部接口，不暴露为 Codex Controller MCP 工具，模型不能自行宣告 recovery 已解决。

### v4 到 v6 数据迁移

首次启动 `0.5.0` 时，Broker 幂等增加 policy/allowlist/adapter/schema/baseline/backup binding、execution lease 元数据、`operation_leases` 和不可变 `backup_evidence` registry，并设置 `PRAGMA user_version=6`。v4 历史提案、收据、execution、recovery、错误码、时间和 hash 均不改写；新增绑定列对历史行保持空值，registry 初始为空。重复启动不会重复建列、重算历史审计或改写证据。

## Passkey 数据与隐私

数据库目录权限为 `0700`，文件为 `0600`，SQLite 使用 `journal_mode=DELETE`、`synchronous=FULL` 和事务。不会保存原始 HA/微信身份、enrollment token、生物特征、私钥或 challenge state。

Ingress 上下文只显示最小收据摘要；完整哈希和执行状态只通过内部 bearer API 返回。日志不会回显 options、Token、Supervisor 响应全文或私有标识。

## 正式启用前门禁

- 核对当前 Home Assistant Supervisor 版本的 `manager` 权限矩阵。
- 完成真实 HTTPS Ingress 的 Passkey 注册、确认、过期、重放、未解决 recovery 阻断、恢复结论和重启恢复 E2E。
- 准备新鲜完整备份、验证离机副本，并通过独立 evidence bearer 登记匹配当前 baseline 且覆盖授权窗口的结构化摘要；备份恢复仍是独立高风险动作。
- Manager Executor shadow 必须通过私有路由、独立 bearer、无宿主端口、结果等价和无 Supervisor 写调用验证。
- 首次 canary 只允许一个精确、处于只读业务模式的 Add-on slug。
- 验证执行前后版本、状态、Ingress/health、业务 writer 模式、数据计数和权限/端口均未扩大。
- 未完成这些生产门禁时必须保持 `execution_enabled: false`。

## 回滚

本地源码回滚可恢复旧版本并保持执行关闭。正式 HAOS 回滚需要独立授权：先关闭 `execution_enabled`，确认没有中间执行，再停止/降级 Add-on。`0.4.0` 不理解 v6 策略绑定、证据 registry 和持久租约，因此降级后不得重新启用 execution；新增表和 nullable 列应保留，不得删除或重建数据库。删除授权数据库会移除 Passkey、证据、提案、收据和审计记录，属于生产数据清理，不得自动执行。
