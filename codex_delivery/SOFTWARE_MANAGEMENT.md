# 软件绑定与状态的受信入口候选

这两个闭合工具复用原 Delivery Provider、InputJournal、MetadataWorker、Store、
SoftwareDeliveryWorker 和 Pipeline，不新建任务、数据库、Runner 或执行线程。
它们仍需兼容 Controller 的可信 owner 来源及既有扩展权限；本次没有修改 Controller
默认启用权限，也没有注册生产凭据或部署服务。

| 工具 | 闭合参数 | 行为 |
| --- | --- | --- |
| `software_bind` | task_ref、expected_revision、code_task_ref、expected_code_revision、release_ref、version、可选 publish_alias | 只提交软件绑定元数据请求 |
| `software_status` | task_ref | 只读当前 owner 的 canonical 请求/绑定/Release 状态 |

target、permissions、owner/project 和完整仓库策略集合来自已验证的镜像 Repository /
Registry boot plans，不由模型参数选择。publish_alias 只能引用每个已冻结仓库都登记
的别名；develop 不接受 publish_alias，deliver/deploy 才可引用已有发布策略。接口不
接受 URL、路径、Shell、凭据、任意 ref 或独立 sync/push 命令。原 Repo 授权 gate
仍会独立核验 software task 与 code Work 的同 owner/project、精确 revision、完整
repo 集及 mode；来源签名不代替权限。

默认 `provider.software_management`、`metadata_worker.software_management` 和软件
worker 的 `request_handler` 均为空。只有同一生产 lifecycle 完成 Repository validation、
Registry 和原软件 worker 接线后，才单次注入同 Store 的管理 Port，再启动 listener /
schedulers。缺 Port 时新工具返回 `tool_unavailable`。Git 认证也是独立默认关闭的
boot plan，只有已登记且合格的固定身份才能注入原 GitRuntime；不从个人凭据回退。

## 接收、绑定和执行的不同事实

Provider 强制检查既有服务 Bearer 及 HMAC owner origin。工具入站只写原 InputJournal，
立即返回 `durability_pending/local_only`。原 MetadataWorker 重新核对当前冻结任务和
策略，只写同 Store 的 `software.bind.request`，独立副本确认后返回 `accepted`；这只
证明请求持久受理，不证明绑定、源码准备、程序执行、Git 发布或部署已完成。

原 SoftwareDeliveryWorker 在自己的同一线程处理已确认请求：先写唯一持久
`software.bind.claim`，确认后调用原 Pipeline.bind。其固定 Git source sync/export
及原 `software.bind` 事实保持原实现，完成后写 `software.bind.outcome`。长时源读取
不在 HTTP 或 MetadataWorker 中运行，不持 SQLite 事务或 metadata writer 锁，其他
普通元数据仍可处理。

请求已冻结的 code Work 在同一 Store 的 `claim_work` 事务中被单独 fencing；只有
原 binding_key 下的 `software.bind` 参数与请求完全一致且已离机确认才放行。
Coordinator 跳过这个暂未可领取的 Work，继续其他普通 Work；新请求不构成全局暂停。
如果 Coordinator 在请求落库前已领取 code Work，绑定请求拒绝，不能反向伪造 queued。

绑定阶段使用原软件 worker 的固定 job 绝对期限。Pipeline 在各 repo/source 阶段边界
复核期限，迟到后不继续下一操作；已经进入的 Git sync 保留它自己的原有固定子进程
预算，这不是对该同步调用的即时取消保证。关闭未结算时原 lifecycle 保留资源和锁。

## 未知与恢复

只有本次写入并确认 claim 的原调用栈可以调用一次 bind；再次进入时观察到已有 claim，
不再调用 bind。重启或回复丢失时只读其原 canonical binding；
存在且确认则记录 bound，不存在则 `binding_unknown`。claim 回执 pending 后即使后来
对账成功，也不据此猜测尚未启动并重放。绑定事件 pending 时只对账原事件，不能创建
另一个 binding_key。scope/策略在处理前撤销记为 `binding_rejected`，不进入源操作。
同 code revision 或 Release 的另一个请求拒绝，普通重发不能绕过未知状态。

`software_status` 不 fetch、build、reconcile 或 publish，只返回有限逻辑引用和
`binding_pending/bound/binding_unknown/binding_rejected/release_ready`。`release_ready`
只表示原 Pipeline 的确切已确认 Release 事件，不表示部署、业务后验或手机实收。
原 request/claim/outcome、Git 来源、构建与 Release 的事件链和恢复证据全部保留。

本地验证使用真实 HMAC、SQLite、Git、AES 与 OS 锁，policy/runtime/target 为明确
fixture。管理入口及原 Work/Coordinator/Pipeline/Factory/Execution/Provider/Service
统一回归快照 **138 项通过，93.831 秒**，与先前 57/97 项有重叠，不相加。
最后补齐非法 Release/alias/revision/code scope 与重复来源请求拒绝后，管理入口、main、
默认可选依赖及 lifecycle 定向组 **39 项通过，20.669 秒**，同样不与历史分组合计。
实际镜像
策略、源输入资格、Git 身份、Runner、Passkey、离机恢复以及 HA
canary 发布/回滚仍须各自现场验收；本接口候选不构成完整通用平台上线。
