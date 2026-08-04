# HA Operations Broker

HA Operations Broker 是 Home Assistant 的独立操作授权与最小执行边界。`0.3.0` 在既有只读预检和 Passkey 授权基础上，增加了 Broker 原生不可变提案，以及唯一受支持的写动作：重启精确白名单中的 Add-on。

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
- 同一动作重放只返回既有结果，不会再次重启；并发执行由全局非阻塞锁拒绝。
- 启动时把遗留的 `authorized/executing/verifying` 状态标记为 `recovery_required`，不会在重启后自动重放。
- 写调用前后都读取精确 Add-on 信息；后验必须确认 slug、`started` 状态和版本未变化，否则进入 `recovery_required`。
- 旧 Hermes/P4 envelope 仍可用于兼容只读预检和历史授权测试，但其收据不能进入执行器。

配置、API、Passkey 注册、状态机、验证与回滚边界见 [DOCS.md](DOCS.md)。
