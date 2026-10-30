# 跨项目交付组元数据入口

这是本地候选能力，复用原 Delivery Store、可信来源签名、InputJournal 和
MetadataWorker。它记录同一 owner 下不同项目的固定 Task/Release、依赖 DAG
和本地历史推送事实。它不签发部署授权，也不推进、取消或回滚成员任务。

## 固定工具

| 工具 | 输入与结果 |
| --- | --- |
| `group_create` | `members`、`edges`、`acceptance_ref`；先返回本地 operation，原副本确认后返回 `group_ref/revision`。 |
| `group_status` | 精确 `group_ref`；返回组控制、分项状态、依赖和历史事实摘要。 |
| `group_list` | `offset/limit`；仅当前 owner 的组摘要，带 total/has_more。 |
| `group_events` | `group_ref/after/limit`；只返回事件元数据与副本状态，不返回原请求正文。 |
| `group_control` | `group_ref/expected_revision/action`，action 为 pause/resume/cancel；仅修改组控制意图。 |

所有工具都要求原服务 bearer 与已签名来源。owner 从来源取得，参数不能选择
owner、读取器、URL、路径、命令或提供 `success/approved`。没有模型可调用的
`group_observe`；可信 Python 观察只读取原库，不能刷新远端 Git 或部署现场。

成员为 2～32 项，至少跨两个项目；每项固定 `member_ref/project_ref/task_ref/`
`task_revision/release_ref/version/release_digest/goal`，可带不含秘密的
`authorization_ref/target_ref`。边最多 128 条，字段只有 `before/after`，拒绝
重复、悬空和循环依赖。创建后不能替换成员、版本或 DAG；改变计划要新建组。
任务及 Release 必须已存在并能从原已确认事件核验归属，不能靠引用字符串造事实。

首包只支持 `goal=published` 的本地历史投影。`goal=deployed` 和部署引用明确
返回 `group_goal_unavailable`，待精确 HA/Release 模型适配完成再启用。保存
`authorization_ref` 不代表当前授权有效，成员明确报告 `not_evaluated`。

## 结果与边界

写入先显示 `durability_pending/local_only`；只有原副本回执确认后才是
`accepted/replicated`。重复输入使用原 operation/组事实对账，不创建替代组。
未知外部结果继续保留，查询不重试推送，不解除原任务或发布锁。

详情最多返回每成员前三条事实引用，并附 `fact_count/facts_truncated`；完整
事实保留在同库可信接口中。列表省略成员与边，避免大组放大分页响应。工具
均保留 128 KiB 响应上限，隐藏原 request、策略、路径和认证材料。

依赖阻塞仅是组状态投影，不拦截成员在原执行链中的独立操作。历史成功与
失败、unknown 分别保留；部分完成不能冒充整组原子回滚。全部成员达到目标
仍为 `awaiting_acceptance`，`combination_accepted=false`；组合验收签发与自动
编排尚未实现。暂停、取消及未知均不会触发成员控制或补偿。

schema v6 需显式迁移旧库，恢复后仍禁止写入。详见
[迁移说明](SCHEMA_MIGRATION.md)和[完整实施边界](DELIVERY_GROUP_PLAN.md)。
本地 SQLite、签名、HTTP 和副本 fixture 测试不能代替真实 Git、离机恢复、
HA 发布回滚或手机验收。
