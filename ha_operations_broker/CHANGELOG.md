# 变更日志

## [0.3.0] - 2026-08-04

### 新增

- 新增 Broker 原生不可变提案 API，只接受封闭结构的 `restart_addon` intent。
- 新增默认关闭的 `execution_enabled`、`enabled_actions` 和精确 `restart_addon_allowlist`。
- 新增 Passkey 收据单次消费、持久执行台账、全局执行锁、幂等重放和重启恢复保护。
- 新增唯一执行器：固定 Supervisor `/addons/<slug>/restart`，并在执行前后读取精确 Add-on 状态。
- 新增 `authorized -> executing -> verifying -> succeeded|failed|recovery_required` 状态机和执行状态查询。

### 安全

- 只有 Broker 原生提案的 Passkey 收据可以执行；旧 Hermes/P4 envelope 收据保持不可执行。
- 默认配置不执行任何写操作；任意附加字段、非法 slug、非白名单目标、过期或已消费收据、并发执行和重启中断全部 fail closed。
- 仍禁止任意 URL、Shell、文件路径、HA service、Supervisor endpoint、HACS、Integration、页面整理和磁盘清理入口。

## [0.2.0] - 2026-07-31

### 新增

- 增加 HA 管理员专用 Ingress、Passkey 注册与确认、私有 SQLite 凭据/提案/收据存储。
- 增加精确 HTTPS RP/origin、一次性 challenge、用户验证、重放和计数器回退保护。
- 增加内部授权请求创建与状态查询 API；当时所有结果固定 `execution_allowed=false`。

## [0.1.0] - 2026-07-31

### 新增

- 建立独立只读 Operations Broker canary。
- 对旧 P4 提案 envelope 复核 schema、hash、owner、风险、备份要求和 TTL。
- 仅允许固定 Supervisor/Core/Add-on 信息 GET，并对响应字段做白名单化。
