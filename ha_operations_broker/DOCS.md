# HA Operations Broker 使用说明

## 当前状态

版本 `0.3.0` 提供只读预检、Broker 自有 Passkey 授权根和一个默认关闭的最小写执行器。唯一实现的写动作是 `restart_addon`；HACS、官方 Integration、页面整理、备份、Recorder、缓存和其他 Add-on 生命周期动作仍未实现。

源码与本地测试通过不等于已经在正式 HAOS 完成权限、Passkey、重启或恢复验收。

## 配置

- `broker_api_token`：至少 32 字符的内部 bearer，只保存在 Add-on 私有 options。
- `trusted_owner_identity_hashes`：旧 P4 envelope 兼容预检所需的结构性 owner 哈希；Broker 原生提案不依赖它批准执行。
- `webauthn_rp_id` / `webauthn_allowed_origins`：稳定且精确的 HTTPS WebAuthn RP 与 origin。
- `passkey_enrollment_token`：临时注册令牌；完成注册后必须清空并重启。
- `passkey_challenge_ttl_seconds`：一次性 challenge 有效期，60–600 秒。
- `proposal_ttl_seconds`：Broker 原生提案和对应授权收据有效期，60–1800 秒。
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

不接受 `parameters`、URL、路径、命令、配置、自由文本或其他动作。Broker 生成 `action_id`、风险、备份要求、验证/回滚说明、到期时间和 `proposal_hash`，并将其持久化。相同幂等键和相同 intent 返回同一提案；相同键绑定不同目标时拒绝。

## Passkey 授权

1. `POST /v1/authorization/requests` 提交 `{"version":1,"action_id":"..."}`。
2. HA 管理员从 Ingress 打开对应 `approval_id`。
3. 已注册的 Passkey 在精确 RP/origin 下完成用户验证。
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

1. 校验运行开关、动作开关和当前目标白名单。
2. 读取 Broker 原生提案并核对 action/hash/idempotency。
3. 在同一 SQLite 写事务中确认 Passkey 收据未过期、未消费且来自 Broker 原生提案，然后立即消费并建立 `authorized` 执行记录。
4. 读取 `/addons/<slug>/info`，要求精确 slug、已安装且状态为 `started`。
5. 只调用固定 `/addons/<slug>/restart`。
6. 再次读取精确 Add-on 信息，要求状态恢复为 `started` 且版本未变化。

`GET /v1/executions/<action_id>` 查询持久状态。相同执行请求重放只返回既有记录，不重复调用 Supervisor。

## 状态与恢复

- `authorized`：收据已单次消费，尚未开始 Supervisor 写调用。
- `executing`：正在调用固定重启端点。
- `verifying`：重启调用已返回，正在执行后验。
- `succeeded`：后验通过。
- `failed`：写调用前的预检失败，没有发出重启。
- `recovery_required`：写调用或后验结果不确定、后验不匹配，或 Broker 在中间状态重启。

Broker 启动时不会自动恢复或重放中间状态。任何 `authorized/executing/verifying` 记录都会变为 `recovery_required`，后续 `start`、恢复备份或其他补偿动作必须另行设计和授权。

## Passkey 数据与隐私

数据库目录权限为 `0700`，文件为 `0600`，SQLite 使用 `journal_mode=DELETE`、`synchronous=FULL` 和事务。不会保存原始 HA/微信身份、enrollment token、生物特征、私钥或 challenge state。

Ingress 上下文只显示最小收据摘要；完整哈希和执行状态只通过内部 bearer API 返回。日志不会回显 options、Token、Supervisor 响应全文或私有标识。

## 正式启用前门禁

- 核对当前 Home Assistant Supervisor 版本的 `manager` 权限矩阵。
- 完成真实 HTTPS Ingress 的 Passkey 注册、确认、过期、重放和重启恢复 E2E。
- 准备新鲜完整备份并验证离机副本；备份恢复仍是独立高风险动作。
- 首次 canary 只允许一个精确、处于只读业务模式的 Add-on slug。
- 验证执行前后版本、状态、Ingress/health、业务 writer 模式、数据计数和权限/端口均未扩大。
- 未完成这些生产门禁时必须保持 `execution_enabled: false`。

## 回滚

本地源码回滚可恢复 `0.2.0` 并保持执行关闭。正式 HAOS 回滚需要独立授权：先关闭 `execution_enabled`，确认没有中间执行，再停止/降级 Add-on。删除授权数据库会移除 Passkey、提案、收据和审计记录，属于生产数据清理，不得自动执行。
