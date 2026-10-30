# 停止检查点的加密字节归档

这是默认未接入正式存储的本地候选。真实网络挂载、独立故障域、凭据恢复和设备丢失演练仍须单独验收；元数据副本确认不能代替源码、输入和制品字节的归档确认。

`ObjectArchive` 使用既有 Replica 的 AES-256-GCM 不可变对象和原样读回校验。Coordinator 只能注入与 `WorkStore.replica` **同一实例**、同一 local/replica domain 的 archive；Bounded Replica 的未知对象 journal 继续只有一份。主启动配置尚未因这个库自动创建或启用真实存储。

## 接线与任务顺序

正式候选装配同时注入 `WorkSourceArchive`，在 claim 前单独归档原程序、已解析输入及任务规格。guest 只读 `/bundle/program` 和输入不会自动进入输出检查点，不能以输出归档代替源材料归档。

源材料先写入 `<database>.sources/<owner-task-revision-hash>/`，目录 `0700`、文件 `0600`，逐件 fsync；同一 revision 的已有字节只允许完全一致，不覆盖。每组最多 256 文件/256 MiB（含程序和 task.json），最多保留 1024 组，满额失败关闭；不自动清理。加密 manifest scope 使用原 task revision 和内容派生的 `SRC-...`，随后 `work.source_archive` 事件进入原元数据链。两类确认都通过后才 claim/执行。字节对象未知只对精确对象 GET 对账，元数据 pending 继续原冻结 snapshot，不发放新执行机会。

源 descriptor 只保存在可信事件和加密副本中，不放入模型可见状态、任务代码或 guest。即使执行器在生成输出前崩溃，已确认的源材料仍可恢复；执行结果保持未知，不声称输出检查点存在。此项覆盖真正交给执行器的程序和输入，不代表尚未由 Repo Manager 提供的完整 Git 对象库已归档。

可信启动代码构造 `ObjectArchive(store.replica, local_domain=store.local_domain, replica_domain=store.replica_domain)`，再通过 `WorkCoordinator(..., checkpoint_archive=archive)` 注入。不能从模型参数选择 Replica、目录、加密 key 或故障域。

1. 执行适配器返回已确认停止的 receipt。Coordinator 校验 workspace、原执行 revision、lease epoch、退出/终止事实、检查点引用和文件清单。
2. `WorkStore.stage_stopped_checkpoint` 在私有本地文件中不可变保存并 fsync 这份停止事实，再通过 `record_stopped_checkpoint` 进入原元数据事件链和副本门禁。这个暂存没有任务状态、权限或副本 journal，不是离机回执，也不允许重跑程序。
3. 元数据确认后，任务保持 `running`、phase=`checkpoint_archive`。Coordinator 将原检查点的完整文件清单交给 ObjectArchive，包含清单中的源码、输入、非交付文件及空目录。
4. 各 chunk 通过后，最后保存 manifest；manifest 回执写回同一任务/事件链，并得到元数据副本确认。
5. 正常结果才进入 `artifact_upload`；暂停、取消、追加需求、失败和关闭期间的已确认停止同样先归档，再确认对应状态。暂停/取消/追加的 `control_pending` 在归档等待中保留，不通过记录 receipt 抹除。

归档 scope 为 `owner_ref/task_ref/revision/checkpoint_ref`，其中 revision 固定取停止 receipt 的**原执行 revision**，不能被后续任务 revision 或来源 input revision 覆盖。最终证据保留 `checkpoint_archive`、`checkpoint_digest` 和 `checkpoint_revision`；完成事件也包含 descriptor。模型可见 `work_status` 仍仅返回已允许的业务结果字段，不暴露对象引用、lease、执行 receipt 或内部存储位置。

## 存储失败与重启

- 任一对象失败时不上传、不创建完成事件，也不重新运行程序。已停止的本地检查点保留，后续运行只重新验证同一文件清单和对象身份。
- 重启遇到 fused 的稍后 chunk 或 manifest，先对该组内**精确未知对象**做 GET-only reconcile，再处理其他对象；缺失、损坏、不可读或属于另一组的未知对象不能清除、重置或改为 PUT。
- 新 DB mutation 在 fused 状态前置拒绝，避免先创建一个 DB pending、再与未知字节对象互相阻塞。已有 DB pending 仍由原元数据 worker 对原冻结 snapshot 对账。
- 若执行停止时 DB 已 pending，receipt 的私有本地暂存保留该已知事实。DB 恢复后先把同一 receipt 写入原事件链；不会把它当成尚未执行的任务，也不先转换成 `recovery_required`。`durability_pending`、writer busy 和副本暂不可用均作为暂时等待。
- 本地暂存目录 `<database>.stopped/` 为 `0700`、文件为 `0600`，按 owner/task/lease/epoch 定址，最多 1024 件、每件最多 69632 字节，不自动清理。损坏、权限不符、配额或本地磁盘故障会失败关闭；清理与保留策略须另行设计。

如果没有可信停止 receipt，或者程序/执行器在保存停止事实前崩溃，仍只能进入执行结果未知的恢复流程；不能编造 checkpoint 或承诺其字节已备份。未知对象的源字节也丢失时，不能靠盲目重写恢复副本资格。

## 归档边界

- 源检查点须为规范绝对路径和私有 `0700` 根目录。最多 256 文件、256 MiB 总字节、4096 目录，路径深度最多 16、整个相对路径 UTF-8 最多 255 字节。
- 拒绝绝对路径、空段、`.`/`..`、反斜杠、控制字符、非 NFC 名称、casefold 冲突、符号链接、文件硬链接、非普通文件及 setuid/setgid 项。目录和文件以 `O_NOFOLLOW`/目录 fd 打开；读取前后核验设备、inode、大小及时间，提交 manifest 前再次核验源树。
- chunk 固定最大 4 MiB，manifest 最大 1 MiB；恢复严格检查每个 chunk 的数量、长度、摘要及同 scope 身份，不允许用很多微小 chunk 扩大读取预算。
- 每次 save/restore 在处理与对象调用边界检查 120 秒预算；单次远程操作继续受既有 Bounded Replica 整笔时限约束，不在预算耗尽后开始下一个对象。本地文件系统调用无法靠这个同步检查强制中断。
- 单组文件和明文 chunk 在进程内有界保留，归档对象中不保存明文。文件名和目录树在加密 manifest 内；ArchiveReceipt 仅含 scope 摘要与 manifest 回执。

这些限制适用于当前候选，不代表任意大型 Git 仓库、带符号链接的项目、浏览器 profile 或操作系统权限已经具备完整恢复能力。超限须明确拒绝，不能省略源文件后报告成功。

## 只恢复到新目录

受信恢复代码先用 `ArchiveReceipt.from_document` 解析 descriptor，再调用 `archive.restore(new_directory, scope=original_scope, receipt=receipt)`。需要同一登记加密身份和可读对象；不提供面向模型的任意路径/对象浏览入口。

恢复先 GET 并验证完整 manifest、全部 chunk、scope、路径和层级，再创建一个全新的目录；既有目录、非私有父目录或符号链接目标拒绝，用户已有内容保留。输出目录为 `0700`，文件为 `0600`，文件与目录完成 fsync。恢复不调用 save、不启动程序、不投递消息，也不启用任务或恢复写者身份。

返回 `recovery_read_only=true` 表示恢复流程没有恢复任务执行或远程写权限，不表示把新文件系统挂成只读。恢复写入阶段若发生磁盘/权限故障会返回安全错误，可能保留该次新建的不完整目录供检查；不会清理或覆盖它，重试应选择另一个新目录。

## 本地验证层级

合成对象存储上的真实 AES 加解密测试覆盖完整源文件丢失、空目录、多个 chunk、错误 scope/receipt、密文损坏、路径/权限/预算、源树变化及 fused 精确 GET。Coordinator 组合测试使用真实 Controller HTTP 验证归档先于上传、原执行 revision、暂停/取消/追加/失败、存储断开、DB pending、重启无重跑，以及模型状态隐藏 descriptor。它们不代表真实 NAS、容器网络挂载、手机实收或设备丢失演练通过。
