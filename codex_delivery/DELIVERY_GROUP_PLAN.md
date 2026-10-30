# 跨项目 delivery_group 后续实施方案

状态：**首包本地源码候选**。[delivery_groups.py](codex_delivery/delivery_groups.py) 已提供同一 Store 的可信 Python 元数据接口、不可变成员/DAG、历史事实投影和 schema v6；下文明确区分已实现范围及后续缺口。没有启用跨项目自动执行，不能作为完整平台或 HA 验收的证据。

设计依据为私有主设计 DESIGN-0036 的“调度、并发与跨项目协作”“最小数据模型”和“跨项目交付”条款。公开组件不依赖私有设计路径。当前实现与状态分别见 [README](README.md)、[软件管理](SOFTWARE_MANAGEMENT.md)、[Release 权限启动](RELEASE_AUTHORITY_BOOT.md)和 [schema 迁移](SCHEMA_MIGRATION.md)。

## 首包选择

首包仅实现同一 owner 下跨项目交付组的持久元数据、固定依赖 DAG、成员事实引用和组合状态投影。组引用各项目原有 Task/Release，不生成替代 Task，不复制 Broker 授权收据事实，不建立第二数据库、执行队列或后台执行器。仅支持 `goal=published` 的本地历史事实；`deployed` 明确返回 `group_goal_unavailable`，没有猜测 Release/HARelease 模型映射。

首包不执行 Git、构建、部署、远程观察、成员 Task 控制或补偿；不接入 WorkCoordinator、公平调度或管理员 UI。已有成员任务继续经过其原有授权和执行链。`dependency_blocked` 仅投影前驱尚无成功事实时的依赖阻塞，不能覆盖 unknown/failed 原事实或抹去已经发生的成员成功；它不拦截成员在原链路中的独立执行。后续接入实际执行前必须补齐这项门禁。

## 已实现的可信 Python 接口

`DeliveryGroups(store)` 构造只验证原 Store 的组 schema，不安装或修复原库 DDL；只依赖标准库和 core Store。面向模型的入口仍必须经过 [provider.py](codex_delivery/provider.py) 的封闭 schema、可信 origin 和 owner 身份，以及 [journal.py](codex_delivery/journal.py) 的原 InputJournal/MetadataWorker；可信 Python 方法本身不是外部鉴权入口。

- `create(owner,key,*,members,edges,acceptance_ref)`：保存固定成员、依赖边、组合验收引用，沿用原 mutation 结果返回 `group_ref/revision/event_id/status/durability`。
- `status(owner,group_ref)`、`list(owner,*,offset=0,limit=20)`、`events(owner,group_ref,*,after=0,limit=20)`：仅查询当前 owner 的组；list 只返回组摘要，status 提供完整内部事实引用。查询不写库、不访问远程。
- `control(owner,key,*,group_ref,expected_revision,action)`：以 CAS 执行 pause/resume/cancel，只改变组控制意图，不级联修改成员 Task 或恢复已撤销授权。
- 内部受信 `observe(owner,key,*,group_ref,expected_revision)`：仅从固定本地事实收集结果，以组 revision/CAS 追加观察事件。不能接受模型提供的 success/approved、任意 URL、路径、可执行代码或读取器名称。该方法不作为模型可调用的结果上报工具。

每个成员固定 `member_ref/project_ref/task_ref/task_revision/release_ref`、预期版本、Release 七字段规范文档摘要和目标结果 `published|deployed`。部署成员另固定 `target_ref/deployment_ref`；授权仅保存不含秘密的 `authorization_ref`。Release 摘要必须有明确文档类型，不能与 source_digest、Docker ImageID、RepoDigest 或 HARelease digest 混用。

首包要求至少两个不同项目，所有成员属于同一 owner 且 task_scope=project，Task mode 为 deliver/deploy。拒绝不存在或不可见成员、重复成员、重复任务、悬空边、自依赖、循环、版本冲突和超限输入。成员为 2～32 项，边为 0～128 项，list/events 每页 1～100 项。成员列表、Release 版本、DAG 和验收引用创建后不可改写；创建事务末尾封存组，数据库触发器同时禁止后续追加、更新、删除成员或边，变更计划创建新组。

创建只保存已经确认的 Release 引用；尚未生成 Release 的跨项目计划编排属于后续范围。不能用组创建把 Task 标记为运行、发布或完成。

## 原事实接口与缺口

| 事实 | 当前真实接口 | 首包使用边界及待补工作 |
| --- | --- | --- |
| Task/Project | [store.py](codex_delivery/store.py) 的 `task()`、`_project()` 及原 tasks/projects/project_versions | 校对 owner/project/task_scope、创建时 task revision 与 project revision。观察时版本或控制状态变化形成阻塞，不能静默跟随最新版本。 |
| Release | [repository_execution.py](codex_delivery/repository_execution.py) 的 `SoftwareDeliveryPipeline._release_record()`、`release_reader()` | `release_reader()` 只返回七字段，缺少成员归属；组已实现独立本地投影，检查原 software.bind/build/release 的命令、事件摘要、参数摘要与 confirmed durability，校对 binding 的 owner/project/task/revision/version 和规范 Release 七字段。不能只检查 release_ref 全局存在。 |
| Git 已推送 | [repo_manager.py](codex_delivery/repo_manager.py) 的 `publication_status()`、原 repository_publications 与事件 | 组核对本地 publication/candidate 归属、固定候选/策略/OID，原 prepare 绑定和 result/reconcile 事件确认。`sending` 或缺少已确认结果按 unknown 展示；本地历史 confirmed 不证明远端现在仍保持该 OID。`observe_publication()` 会调用 Git 远程读取，组不调用它。 |
| HA 部署 | [ha_release.py](codex_delivery/ha_release.py) 的 `HAReleaseCoordinator.status()`、原 `ha_release_operations/ha_release_steps` | **尚未接入**，deployed goal 拒绝。后续受信投影须读取并核对原 request_json 的 schema、owner/project/task/revision、target、精确 Release/制品及后验。仅 completed 字符串、HTTP 200、已发出请求或镜像标签均不够。 |
| 当前动作权限 | [release_authority.py](codex_delivery/release_authority.py)、HAReleaseCoordinator `_gate()` 与既有 Broker | 组没有签发/消费执行收据的权力。原逐成员动作门禁继续负责有效期、撤销、权限摘要和实时状态。首包只显示 not_evaluated/unavailable 等明确边界，不把保存 authorization_ref 当作授权有效。 |

Release 七字段与 HARelease/HA 请求是不同模型；跨模型匹配须经过受信转换和精确字段校对，不能假定摘要相同。若部署事实实际来自其他 Manager/Broker journal，后续只能经其固定认证观察接口读取，不直接打开其他组件数据库，也不接受调用者自报结果。

本地投影须固定原 event_id/operation_ref、摘要及观察时间，验证事件链和对应 durability。原 facts 才是事实源；组观察是带时间的汇总，不能覆盖、修复或升级原证据。数据库 pending、事实缺失、重复匹配、未知版本或内容漂移均不能提升为成功。

## 状态、可见性与授权

成员至少区分 pending、blocked、succeeded、failed、unknown，并分别保留已推送、已部署和补偿事实；不能压成一个可覆盖历史的成功布尔值。

- 有成功成员，同时存在失败或 unknown：`partially_completed`，另给出 `requires_reconciliation=true` 和未知成员引用。
- 尚无成功且存在 unknown：`recovery_required`；无副作用的权限/版本阻塞与未知外部结果分别展示。
- 全部成员达到固定目标但组合验收未确认：等待组合验收；不能直接 completed。
- completed 要求全部精确目标和固定组合验收均具备受信、已确认的证据。补偿成功也不抹去原失败和已发生副作用。

已实现上述部分完成/unknown 状态及等待验收投影；`combination_accepted` 固定 false，不提供 completed 路径。组合验收证据的格式、签发身份和本地读取器尚未实现，不能接受自由文本“已验收”来关闭该缺口。当前授权状态始终为 not_evaluated。

历史成功仅从最近已 confirmed 的 group.create/group.observe 事件继承。最新观察 pending 时，顶层 durability 显示 local_only，但不能遮住更早已确认的成功事实。组状态不将入队、metadata accepted 或历史 Git 回执描述成当前生产部署成功。

每次列表、详情、事件和观察都核对组 owner 与逐成员归属；越权统一 not_found，不返回隐藏成员名称、版本、目标或证据详情。首包不支持跨 owner 共享组。若未来增加项目级可见性，必须逐成员授权过滤，不能因可见组而获得全部项目数据。

观察时重读成员 Task revision、项目状态和原事实；任何实际动作前还须由原执行域重新核对逐成员授权、目标/制品/权限摘要、过期/撤销和资源锁。不能跨网络持有 SQLite 事务，也不能拿预检授权替代动作时检查。历史成功在任务修改、授权撤销后仍保留，但不能据此执行下一动作。

暂停、取消、失败或 unknown 均不触发自动补偿。后续补偿必须是独立、精确授权的执行，前提包括已知现场结果、数据兼容性和适用恢复资产；保留原操作与补偿操作分别可追溯。unknown 不重放，不释放原执行域的锁。

## 同一 Store 的 schema v6 方案

当前 [schema.sql](codex_delivery/schema.sql) 与 DeliveryStore 已升为 v6，新增 `delivery_groups`、`delivery_group_members`、`delivery_group_edges`，全部位于原 SQLite 数据库：

- groups：稳定 group_id、owner、revision、控制意图、固定组合验收引用和时间。
- members：固定成员标识、owner/project/task 与 revision、Release/目标/操作引用。使用 `(owner_ref,project_id,task_id)` 组合外键，并校验任务 scope；不能只外键到全局 task_id。
- edges：前后成员均以 `(group_id,member_ref)` 引用同组成员。无环由事务内服务端校验；成员和边不可改写。

组操作通过原 `DeliveryStore._mutation()` 保存 commands、append-only events、durability 和 notification_outbox。result 携带 `group_ref`，组事件 project_id/task_id 为空。组提供 owner-scoped 的事件查询；原 `events(project_id=...)` 不承担跨项目查询。成员观察结果保留于事件引用，避免新增第二套可独立写入的部署状态。

v5→v6 须显式 upgrade_schema；先验证已有 task/repository/release schema、完整性和外键，再生成原机制的 0600/fsync 升级前备份，单事务安装组 DDL、验证全部定义并更新 user_version。不能通过把 5 加入旧版本列表就绕过已有 v5 schema 校验。v1～v4 保留原迁移/历史兼容语义，再安装组 schema。新建数据库一次事务安装全部表；helper/读取器构造时禁止隐式 CREATE 或修复。

已有 v5 事件、commands、未知步骤、publication、持久资源锁、快照和通知均保留。当前 v6 缺表或被削弱定义应拒绝，不能自动修复。迁移失败事务回滚并保留备份；不支持原地降级。

恢复仍接受匹配的受信快照，在新私有命名空间检查/显式迁移并保持 recovery_required。恢复不清锁、不重放、不消费通知、不恢复组执行。组的独立本地投影允许展示原库历史证据并标记 recovery_read_only，不调用 `_release_record()`、source_qualifier 或任何远程观察，不生成当前执行资格。唯一写者和现场结果对账另行完成。

## 本地验收与后续工作

[专属本地测试](../tests/test_codex_delivery_groups.py) 覆盖真实 Store 成员关联、owner/project 越权、DAG/限额/封存、Task 与 Release 漂移、CAS/幂等、副本未知/重启精确对账、原事件错绑、deployed 和伪造结果拒绝、依赖标记及部分完成不丢历史成功、组合验收缺失不完成、暂停/取消不改成员、v1～v5 显式迁移/备份/失败回滚、未知 HA 步骤及锁保留、DDL 漂移和恢复只读。旧 Store/Work/Release/Repository 迁移测试需要同步回归，不能只运行新增测试。

测试需使用原 Store 的真实事件/回执链和明确受信的本地 fixture，不依赖未发布 Controller 集成 fixture，不用调用者直接插入一条 completed 数据替代原事实校验。必须验证组接口不会触发网络、执行器、Git 或 HA 写。该本地验收仍不代表手机、部署或生产故障恢复验收。

后续独立工作包括：未产出 Release 的计划成员绑定、管理员 UI、跨项目逐成员权限展示、原执行链 DAG 门禁、Broker 授权重读与收据、资源互斥、真实远程事实刷新、受信组合验收、人工对账以及经批准的独立补偿。完成并验收这些步骤前，不宣称跨项目自动发布或完整通用交付功能完成。
