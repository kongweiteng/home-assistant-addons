# 变更日志

## [0.5.1] - 2026-08-16

### 修复

- 兼容 Home Assistant OS Supervisor 的 `/addons/<slug>/info` 成功响应省略或返回空 `installed` 字段；仅在该固定已安装 Add-on 信息端点内规范化为 `true`，使 baseline、Manager shadow 和执行前复核保持一致。
- 显式布尔值仍原样保留；未扩大 action、target、Supervisor endpoint 或执行权限，默认执行开关、动作列表和白名单继续关闭。

## [0.5.0] - 2026-08-05

### 新增

- 新增独立 `recovery_api_token`；普通 Broker bearer 固定不能调用 recovery resolution。
- 新增第三个独立 `backup_evidence_api_token` 和由 SQLite trigger 保护的不可变结构化 evidence registry；普通 Broker bearer、recovery bearer、Ingress 与模型路径不能登记或替换证据。
- restart proposal 优先绑定精确 `addon` 证据，也允许 baseline 匹配且覆盖授权窗口的 `full` 完整备份证据。
- Controller 继续提交最小 version 1 intent；Broker 服务端读取目标基线、选择覆盖授权窗口的匹配证据并生成 version 2 提案，proposal/request/receipt/execution 持久绑定 policy、allowlist、adapter、schema、baseline 和 evidence ID。
- 新增 SQLite singleton/resource lease、instance、epoch、heartbeat 和 expiry；收据消费、execution claim 与两把租约在同一事务完成。
- 新增可选 Manager Executor 固定私有 client；在收据消费前执行只读 shadow 等价复核，不形成第二个写执行器。

### 安全

- policy、allowlist、adapter、schema、baseline 或 evidence 漂移在收据消费和 Supervisor 写调用前拒绝。
- 模型和 Controller 不能提交 policy、adapter、baseline 或 backup evidence 绑定字段。
- 双实例共享同一数据库时，持久租约阻断第二次写入；租约过期或 Broker 重启只进入 `recovery_required`，不自动接管或重放。
- 默认 `execution_enabled=false`、动作和白名单为空；唯一正式 action 仍为 `restart_addon`。

### 兼容

- SQLite schema 升级到 `user_version=6`；v4 历史 proposal/receipt/execution/recovery 不重算 hash、不改写状态或审计字段，新增 evidence registry 初始为空。

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
