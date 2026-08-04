# HA Operations Broker

HA Operations Broker 是 Home Assistant 的独立操作授权与最小执行边界。`0.5.0` 在 restart-only 基础上增加恢复/备份证据凭据隔离、不可变策略/适配器/基线/备份证据绑定，以及 SQLite 持久 singleton/resource lease。唯一受支持的写动作仍是重启精确白名单中的 Add-on。

默认配置绝不执行写操作：

- `execution_enabled: false`
- `enabled_actions: []`
- `restart_addon_allowlist: []`

只有同时打开总开关、只启用 `restart_addon`、配置精确 slug 白名单，并消费同一 Broker 生成且经 Passkey 确认的未过期收据，执行接口才会调用固定的 Supervisor Add-on 重启端点。

## 安全边界

- 使用 `hassio_api: true` 和 `hassio_role: manager`，但不申请 Core API、backup/admin、host network、privileged、Docker socket、主机目录或宿主端口。
- 不接受任意 URL、Supervisor endpoint、HA service、Shell、命令、文件路径或自由参数。
- Controller 仍只提交 version 1 的固定 action/target/idempotency；Broker 服务端读取 Add-on 基线并生成 version 2 不可变提案，绑定 policy epoch/hash、allowlist hash、adapter/schema、baseline etag 和已登记的结构化 backup evidence ID，模型不能提供这些安全字段。
- 结构化 backup evidence 只保存 scope、逻辑 ID、完成/可读状态、时间、大小、双 SHA-256、baseline 和有效期，不保存路径、凭据或备份内容；相同逻辑 ID 只能幂等登记同一内容，SQLite trigger 禁止后续 UPDATE/DELETE。
- Passkey 授权只生成一次性收据；执行认领在同一 SQLite `BEGIN IMMEDIATE` 事务中消费收据、建立执行记录并取得 singleton/resource lease。
- 同一动作的精确重放优先返回既有结果，不会再次重启；进程内锁和持久租约共同阻断并发写入。
- policy、allowlist、adapter、schema、baseline 或 backup evidence 任一漂移都会在收据消费和 Supervisor 写调用前拒绝。
- 可选 Manager Executor shadow 使用独立内部 bearer 复核同一 proposal 和 baseline；shadow 只读且不成为第二个实际写执行器。
- 任一 `recovery_required` 尚未记录有效恢复结论时，所有新 execution 都在收据消费前返回 `unresolved_recovery`；联锁检查、收据消费和 execution 插入位于同一 SQLite 写事务。
- 启动时把遗留的 `authorized/executing/verifying` 状态标记为 `recovery_required`，不会在重启后自动重放。
- 恢复 API 使用独立 `recovery_api_token`；普通 `broker_api_token` 无法记录恢复结论。恢复只接受 `confirmed_healthy|compensated` 与 SHA-256 证据标识，不调用 Supervisor，也不会把原 execution 状态改写为成功。
- backup evidence 登记/查询使用第三个独立 `backup_evidence_api_token`；普通 bearer、recovery bearer、Ingress 和模型路径均不能登记或替换证据。
- 写调用前后都读取精确 Add-on 信息；后验必须确认 slug、`started` 状态和版本未变化，否则进入 `recovery_required`。
- 旧 Hermes/P4 envelope 仍可用于兼容只读预检和历史授权测试，但其收据不能进入执行器。
- v4 SQLite 数据库会幂等升级到 schema `user_version=6`；历史 proposal/receipt/execution/recovery 行不重算 hash、不改写状态或审计字段，新绑定列保持空值，新增 evidence registry 保持为空。

配置、API、Passkey 注册、状态机、验证与回滚边界见 [DOCS.md](DOCS.md)。
