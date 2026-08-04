# 变更日志

## [0.4.0] - 2026-08-04

### 新增

- 新增未解决 `recovery_required` 的全局 execution 联锁；新执行会在收据消费前返回 `unresolved_recovery`。
- 新增内部 bearer 恢复结论 API，只接受 `confirmed_healthy|compensated` 和 SHA-256 证据标识。
- execution 状态新增脱敏 `recovery` 元数据，保留原始 `state=recovery_required` 和既有审计字段。

### 兼容

- 空数据库和既有 `0.3.0` SQLite 数据库都会幂等增加三列恢复元数据，并升级为 `user_version=4`。
- 迁移不改写既有 execution 状态、错误码和时间；重复初始化保持一致。

### 安全

- 同一 action 的精确幂等重放仍优先返回原记录；只有新 execution 受全局 recovery 联锁阻断。
- 联锁检查、收据消费和 execution 插入保持在同一 `BEGIN IMMEDIATE` 事务，阻断时不会消费新收据。
- 恢复结论不会调用 Supervisor、不会重放 restart、不会接受自由文本，也不暴露为 Codex Controller MCP 工具。
- 并发恢复结论最多一个成功；未解决 recovery 只有记录有效结论后才解除对应联锁。

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
