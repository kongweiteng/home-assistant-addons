# HA Operations Broker

HA Operations Broker 是 Home Assistant 的独立操作授权与最小执行边界。`0.4.0` 在既有只读预检、Passkey 授权和 restart-only 执行器基础上，增加了未解决恢复的全局执行联锁，以及只记录恢复结论的内部 API。唯一受支持的写动作仍是重启精确白名单中的 Add-on。

默认配置绝不执行写操作：

- `execution_enabled: false`
- `enabled_actions: []`
- `restart_addon_allowlist: []`

只有同时打开总开关、只启用 `restart_addon`、配置精确 slug 白名单，并消费同一 Broker 生成且经 Passkey 确认的未过期收据，执行接口才会调用固定的 Supervisor Add-on 重启端点。

## 安全边界

- 使用 `hassio_api: true` 和 `hassio_role: manager`，但不申请 Core API、backup/admin、host network、privileged、Docker socket、主机目录或宿主端口。
- 不接受任意 URL、Supervisor endpoint、HA service、Shell、命令、文件路径或自由参数。
- Broker 原生提案只接受 `version`、`action_type=restart_addon`、精确 `target` 和 SHA-256 `idempotency_key`。
- Passkey 授权只生成一次性收据；执行认领在 SQLite `BEGIN IMMEDIATE` 事务中消费收据并建立持久执行记录。
- 同一动作的精确重放优先返回既有结果，不会再次重启；进程内并发执行由非阻塞锁拒绝。
- 任一 `recovery_required` 尚未记录有效恢复结论时，所有新 execution 都在收据消费前返回 `unresolved_recovery`；联锁检查、收据消费和 execution 插入位于同一 SQLite 写事务。
- 启动时把遗留的 `authorized/executing/verifying` 状态标记为 `recovery_required`，不会在重启后自动重放。
- 内部 bearer API 只接受 `confirmed_healthy` 或 `compensated` 与 SHA-256 证据标识；记录结论不会调用 Supervisor，也不会把原 execution 状态改写为成功。
- 写调用前后都读取精确 Add-on 信息；后验必须确认 slug、`started` 状态和版本未变化，否则进入 `recovery_required`。
- 旧 Hermes/P4 envelope 仍可用于兼容只读预检和历史授权测试，但其收据不能进入执行器。
- `0.3.0` SQLite 数据库会幂等增加恢复元数据列并升级到 schema `user_version=4`，既有提案、收据、execution 状态和审计字段保持不变。

配置、API、Passkey 注册、状态机、验证与回滚边界见 [DOCS.md](DOCS.md)。
