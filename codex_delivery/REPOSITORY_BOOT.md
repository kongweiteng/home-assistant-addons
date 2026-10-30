# 固定仓库执行门禁与内部测试协议

此包只增加受信启动组件，不增加模型工具、凭据源、任务数据库、Runner、VM 或远端
权限。普通配置不能选择 Git、scanner、测试命令、工作区、observer 或打开 factory。
所有事实与幂等事件仍在唯一 DeliveryStore；恢复、pending、control 和 revision fencing
保持原约束。真正的构建/发布 ports 由同源受信启动器显式 attach，缺失保持不可用。

## 权限和固定源

镜像拥有的闭合 policy 固定 RepositoryPolicy、Git 路径及摘要、授权 scope/有效期、
scanner 规则摘要、测试 recipe/runtime/模板摘要及受信工作区绑定。每次 gate 重新核对
固定 policy 字节与当前 canonical owner/project/task/revision/repository/access/mode。
更换或删除 policy 会撤销现有 gate；配置中的 approved=true 不构成授权。Work 只读
源输入不能取得 import/publish 权限；publish 仍仅允许 deliver/deploy 与登记目的 ref。
Git 继续使用现有干净环境、无 hooks/credential helper/redirect 的固定命令集。

固定 workspace 绑定不接受任务路径，须对应完整 frozen repo 集。停写判断要求真实
操作系统只读挂载与原目录设备/inode、owner/private mode，以及 canonical 当前状态；
仅持有 flock、把文件 chmod 或写 stopped=true 不能证明停写。没有合格只读源时拒绝。
原 RepositoryArchive 仍会跨捕获逐文件及 Git 状态核验；主机管理员重挂载属于另外的操作。

scanner 实际检查被读取的 tracked regular bytes 和待发布 Git 历史 blob；固定规则和
已知正反样本自检属于其身份。它不执行项目代码，拒绝无法处理的数据和预算超限。
规则覆盖范围必须写明，不能宣称发现所有可能秘密。输入扫描和候选扫描分别绑定实际
manifest/bundle/OID/context，不能用文件名检查或对一个“通过”字符串取摘要代替。

## 内部测试任务的唯一来源

`RepositoryTestPort` 为同一 candidate 创建固定 recipe 的独立 Work。当前显式白名单为：

| recipe | interpreter | 固定行为 |
| --- | --- | --- |
| `canary-python-v1` | `python3` | 独立测试服务 A/B/C、数据恢复和重复 JSON 的 5 项 unittest |
| `python-unittest-v1` | `python3` | 从固定 source prefix 的 `tests/` 发现 `test_*.py`，运行 unittest |
| `node-test-v1` | `node` | 从固定 source prefix 的 `tests/` 收集 `*.test.js/cjs/mjs`，调用 Node 内置 `--test` 与 TAP reporter |

recipe、source prefix 和 runtime 由镜像受信装配固定；未知 recipe 拒绝，runtime 必须为
对应 interpreter 的 `work-qemu-v1`。不运行 npm scripts、安装 hooks、package.json 中的
命令或模型指定 shell。未装配相应 recipe 的项目不获得测试能力。

它不是原代码生成 Work；原 Work 成功、OCI 构建和 registry 图验证都不能代替测试。
稳定测试 identity 绑定 owner/project/原软件 task/revision/repo/bundle/base/candidate/
runtime/template/scanner/view、完整 frozen repo 集和实际 source tar 摘要，命令和程序
来自镜像固定模板，不能由模型选择。

同一 `repository.test.create` mutation 原子写入 project 绑定但 repo 集为空的内部 Work，
初始 paused。空 repo 集避免测试递归归档原 workspace；实际测试输入仅来自同一
candidate bundle 经 scanner 核验的 tracked view。原 project/原软件 task/完整 repo 关系
另外冻结在该 canonical event 中，不可由任务字段的任意标签选择内部身份。

按原 WorkSourceArchive 格式保存固定 program、task.json、candidate source tar 和完整
身份 manifest，使用同一个 ObjectArchive/Replica。`work.source_archive` 确认后，专门
`repository.test.ready` 事件才把 paused task 变 queued。这样普通 Coordinator 不可能
先领取无源材料的任务。未知副本只对账原对象；不能重写输入或创建另一个测试 identity。

唯一现有 Coordinator/Runner 使用原 lease/Registry/qualified runtime 运行测试，测试
创建立即返回 pending，软件 worker 释放调用栈，之后重读原结果；不占住 VM 等另一 VM，
不因 900 秒 pipeline deadline 延长测试租约。异常退出/recovery/取消不会自动重跑。
没有固定 recipe 的项目拒绝，不使用任意 shell/项目脚本作为默认测试。

## 内部结果接管与证明

Coordinator 的默认 None `internal_result_handler(task,evidence)` 只在数据库存在原
`repository.test.create` 时可接管，而且必须在原 stopped checkpoint 已加密离机确认后。
没有 handler 时这种已登记内部任务失败关闭，不落入 Controller/Gateway 回调。
普通 Work 的行为不改变；任务参数不能选择“内部完成”。

handler 重新验证 create/ready/source/checkpoint 的 canonical command/event/durability、
精确 runtime/template/原输入、真实 executor journal/lease/epoch/stopped receipt、原
加密 checkpoint 和测试报告。固定模板运行上述白名单测试，零测试、失败、错误、
跳过测试或非零退出均不通过；canary 配方额外要求恰好运行 5 项。扫描、授权、软件
任务取消或 revision 漂移也会阻止后续 import/publish。
报告只是补充语义；通过的信任根仍是固定程序、实际 runtime 和独立记录的退出证据。

通过或失败都写同库 `repository.test.result` 并终结内部任务，保留原 execution 与
checkpoint receipt。`delivery=internal_archive` 只表示本地内部结果已离机确认，不写
gateway_sent，不发送文件/通知，不冒充手机送达。结果事件尚 pending 时不授予验证。
Store 仅对 `repository.test.create` 以及同 owner/task 下存在该 canonical create 事件的
内部 Work 事件抑制 notification_outbox；普通 Work 即使具有相同程序和输出名，也
保持原通知行为。Coordinator 不调用内部任务的 Gateway 上传、完成或状态查询接口。
重启只观察原 task/事件/归档，不能凭 absent/unknown 构造新的测试并重跑。

RepoManager 在分配随机 candidate/quarantine 前完成 scan/test preflight，pending 或
unknown 不创建新的隔离目录；原实际 Git 检查保留，最终落库前重新检查当前 task
context 与同一 validation receipt，控制或 revision 漂移不能借用早先通过的结果。

## 显式一次性启动接线

`RepositoryExecutionFactory(..., validation_gate=None)` 明确选择延迟装配，未装配时
manager validation 关闭，派生 software worker 也不能启动。旧的显式可信 callable
构造行为保持兼容，不能被 `attach_validation` 替换；两种模式都只供镜像受信代码使用，
不从 options、任务字段或模型参数选择。

受信启动器必须在单一线程中按以下依赖顺序接线，所有固定 ports 和 policy 均须已通过
各自资格门禁。以下顺序已接入生产 main 的源码，固定注册缺失时仍关闭，不代表现场启用：

1. 创建延迟 factory，传给 `QualifiedExecution(..., repository_factory=factory)`。
2. 构造原 `DeliveryService(..., coordinator_factory=execution, ...)`。它调用原 execution，
   创建同 Store/ObjectArchive 的 RepositorySources、WorkSourceArchive 和 Coordinator，
   最后才将 execution 标记为 qualified。此时不调用 `service.start()`。
3. 调用 `factory.attach_validation(execution, service.coordinator, policy=固定策略,
   bindings=固定绑定元组, scanner_digest=固定摘要,
   runtime_qualification_digest=原运行时资格摘要)`。每个冻结 `RepositoryTestBinding`
   包含 owner_ref、repo_ref、repository_policy_digest、runtime_id、recipe、source_prefix、
   template_digest；核对同一 Store、source/archive、adapter、runner、factory、实际
   runtime/interpreter 和完整摘要。该方法不请求测试、不创建任务、不运行 VM。
4. 装配成功后调用 `factory.attach(execution.source_archive)` 取得原软件 worker；也允许
   先创建但尚未调度的 worker，再执行第 3 步。全部装配成功后才启动原 service 与该
   worker。没有第二个 Coordinator、Runner、WorkExecutor 或任务库。

不能在 factory 的 `__call__` 中调用 `attach_execution` 或请求测试：当时原
WorkSourceArchive 尚未创建，execution 也尚未 qualified，会形成启动依赖环。测试
请求遇到 pending 立即释放软件 pipeline 调用栈，由原 Coordinator 随后领取内部任务；
不能持有 VM 等待另一台 VM，也不能为等待而延长原 lease。

`attach_validation` 只允许一次尝试；失败后该 factory 实例永久关闭，不能改成另一
输入或身份重试。成功后的 repo→port/gate 映射只读，内部结果仅根据同库 canonical
`repository.test.create` 的 owner/repo/runtime/recipe/template identity 路由，未知路由
拒绝。已有显式 gate、重复 attach、跨 execution/store/source/archive、错误摘要、已经
调度的 Coordinator/worker 或已经启动/关闭的 worker 均拒绝。

装配持有原 Coordinator 的同一 dispatch 文件锁；进程内 `_dispatch_started` 标记
区分已经调度的对象。持久锁文件存在本身不能阻止正常新进程接线。外部 service 线程
已经 start 但尚未调用首次 `run_once` 的窗口不能由 Coordinator 对象独立证明，仍由
受信启动器保证在 `service.start()` 前串行装配，不把这项调用契约写成已验证的线程事实。

关闭时先停止 software worker 的新工作，并处理 `worker.close(timeout_seconds=...)`
返回值，再按原 service 关闭流程停止/归档 Coordinator。原 build Port 尚未返回时
`worker.close` 返回 false 并保留 singleton 锁；不得忽略 false、提前释放 execution
资源或启动替代 worker。所有 worker 与原执行均已结算后才能关闭 QualifiedExecution。

## 本地验证与接线边界

本次 Store/Work、源码与 checkpoint 归档、Coordinator、RepoManager、仓库变更和软件
执行联组为 **164 项通过**；最后模板更新后的专项为 **28 项通过**。两组有重叠，
不得相加为独立测试总数。专项使用真实临时 bare Git、真实 AES-GCM/ObjectArchive 与
同一 Store，执行 adapter/qualification 为明确标注的本地 fixture；固定 Python/Node
模板只运行测试文件中受信构造的候选。语法检查和 `git diff --check` 通过。

随后一次性 Factory 接线、运行时装配、原 Coordinator、软件 pipeline 和上述专项的
**80 项联组通过**（84.208 秒），其中新增 17 项 Factory/生命周期用例。该联组与前述
164/28 项也有重叠，不作为可相加的独立总数。覆盖旧显式 gate 兼容、失败不可重试、
跨 Store/source/adapter、锁竞争与装配期间调度、正常持久锁重用、多 owner/repo 配方
路由、实际本地 Node 模板输出以及 pending 释放调用栈供原 Runner 继续领取测试。

已覆盖 pending 重试不增长 quarantine、完整 import→内部测试→结果→import 接线、
原始源码先归档、checkpoint 副本中断与恢复、结果元数据 pending、原任务与内部
任务取消/revision 漂移、策略撤销、错误报告身份、零测试、未知执行不重跑、内部零
Gateway/通知及普通 Work 通知兼容。

后续生产 main 源码已通过 `ProductionLifecycle` 接入上述顺序：降权前只读加载固定
Repository/Registry 注册并核对固定服务 UID；降权后创建延迟 factory，原
DeliveryService 构造唯一 Store/执行/源归档，随后一次性 attach validation 与 Registry，
最后启动原 service 和 software worker。Release authority 的 release/source Ports
指向该 factory；构建 evidence 的两个只读 GET 使用原 bounded HTTP listener。

缺失或精确 disabled 的注册保持 metadata-only，轻量注册选择器不导入可选 Registry
依赖；错误注册、Repository/Registry 缺一、UID 不一致或任何 attach 失败均拒绝启动。
固定 `RepositoryBootPlan` 的具体输入见 [REPOSITORY_REGISTRATION.md](REPOSITORY_REGISTRATION.md)。
没有 options、动态模块、模型给出的命令或 fixture gate 可以替代这些材料。

关闭由同一 lifecycle 单次负责，在一个原绝对关闭期限内先关闭 software worker，再
关闭原 service。worker 返回 false/异常或 service 未结算时，保留 execution 资源及锁，
返回 `service_shutdown_incomplete`，finally 不重复清理或创建替代 worker。

本轮 main/lifecycle、真实 HTTP、Registry boot、Repository factory/固定 loader，以及
既有 observation/authority/fnOS/service 相关组 **97 项通过（40.968 秒）**。包含默认包
形状的隔离子进程配置检查（拒绝任何 Registry sibling 导入）及真实 blocked worker/OS
flock 锁保留；与历史分组有重叠，不累加。执行资格、网络目标与管理 Ports 仍为明确本地
fixture，未进行实际 VM/HA 平台验收。

后续 Provider/MetadataWorker 已增加默认关闭的[软件绑定与查询候选](SOFTWARE_MANAGEMENT.md)：
MetadataWorker 仅确认 canonical request，由原 software worker 在唯一持久 claim 后
进行原绑定，未知不重放；只 fencing 关联 code Work，不阻塞普通 Work。生产 lifecycle
完成同源接线后才注入 Ports，未修改 Controller 默认权限。现场固定策略、源输入、
原任务/仓库集合、独立 credentials、真实 Runner 与恢复/发布链仍须登记和验收，不能
据源码接线宣称完整软件交付已启用。上述 97 项是管理入口开发前的历史快照。

源码单测和本地受信 fixture 的模板运行不等于真实 HA 沙箱资格。最终还需同一固定
recipe 在正式 qualified runtime 执行并完成原 source/checkpoint/重启恢复验收；没有
这项现场证据不能宣称整个仓库执行平台已完成生产验收。
