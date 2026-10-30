# Managed sealed repository sources v2

本协议显式新增 `managed-sealed-v2`，不修改或冒充 `directory-v1` 的只读目录门禁。
依据 DESIGN-0036 的停写一致性和宿主不执行候选代码要求，managed 模式不要求宿主
mount、SYS_ADMIN、Docker socket、设备或可写共享 checkout。没有固定注册仍默认关闭。

## 来源和边界

首轮源只接受原 RepoManager 在同 Store 确认的精确 `repository.export` bundle：冻结
owner/project/task/revision/repo/policy/baseline、导出事件及完整 repository_set。读取
完整 bundle 原字节并校验摘要/大小，通过固定 bare Git 命令验证 bundle、fsck、baseline
和全部历史；tracked view 由 ls-tree/cat-file 读取，宿主不 checkout、不执行项目脚本。
这个源明确是 clean baseline，没有包含外部 checkout 的 dirty/untracked 或用户电脑文件。

所有模型改动只沿原软件绑定的代码 Work→真实停止收据→同库已确认 `work.checkpoint`
→同一个 ObjectArchive 的完整文件恢复→candidate.zip 路径。完整 checkpoint 清单的
每个文件、大小和摘要均重新核对；候选包仍通过原授权/扫描/独立内部测试/import。
工作区里的 dirty/untracked 字节作为原完整 checkpoint 保存，不以 clean bundle 冒充。
没有停止、checkpoint 未确认、身份/revision/lease 漂移、额外或缺失文件都拒绝。

## 内核封存和持久化

每个受信生成 snapshot 通过 Linux `memfd_create(MFD_ALLOW_SEALING|MFD_CLOEXEC)`：
写入有界数据后设置 `F_SEAL_WRITE|F_SEAL_GROW|F_SEAL_SHRINK|F_SEAL_SEAL`，重新读取
`F_GET_SEALS`，保存并核对 fd 的 device/inode/size/type，封存后计算实际 SHA-256。
消费同一个 fd 时再次核对身份/seals/长度/摘要；写入、增减文件、writable shared mmap
和更改 seals 必须由内核拒绝。缺 syscall/常量/内核支持或自资格失败一律关闭；macOS
不以模拟 seal 或 chmod 宣称支持。

snapshot 包含原完整 Git bundle、规范元数据和已扫描 tracked view。临时 memfd 不是
长期备份：原字节复制到 `/data` 下原 Store 派生的私有 staging，经同一个 ObjectArchive/
Replica 保存，只有 descriptor 和事件确认后才交执行器。重启只读原事件和原 descriptor，
恢复后必须重新封存并验证原摘要；pending/unknown 不重选 baseline、不替换字节或 identity。
本地 artifact 文件只作为按已记录摘要校验的输入，不能代替离机确认。
已确认 snapshot 的重启恢复不依赖 RepoManager 的原本地 export bundle：只读同库原
confirmed export 的身份、授权和摘要记录，再以 snapshot 内保存的原 bundle 对账。
原 bundle 和本地 snapshot staging 同时丢失时，不 fetch、不重建 artifact、不新增事件，
仍可从原加密 archive 恢复；snapshot 内 bundle 的大小或摘要不符则拒绝。

## 固定接线与 schema

`RepositoryExecutionFactory` 仅接受固定 enum `directory-v1` 或 `managed-sealed-v2`，
不接受 import 路径或可执行插件。v2 直接装配专用 RepositorySources 子类和完整停止
checkpoint 检查的 ArchivedCodeChanges 子类，复用原 Store/Archive/Coordinator/worker。
v1 的 workspace/quiescence ports 保留；v2 不调用它们，也不构造恒 true 的停写 gate。

镜像 policy v2 新增固定 `source_kind=managed-sealed-v2`；grant.repositories 精确字段为
repo_ref/policy_digest/access/workspace_ref，来源路径和 inode 不再由 policy 或 task 提供。
其他授权、配方、独立目标观察及凭据防复用约束与 v1 相同。policy v1 不接受该字段；
未知版本/来源拒绝，不自动 fallback。动态任务仍需受信固定范围绑定，不从模型取得授权。

Linux 本地内核资格与真实 HA 资格分别记录。macOS 平台应明确得到 unsupported；跳过
Linux 专项不算通过。真实 VM、离机恢复、Git 认证发布及生产启用仍须各自验收。

## 本地验证记录

2026-10-31 上海时间，修复原 export bundle 丢失后的恢复依赖后，最终同源快照在本机
Docker Desktop **native Linux arm64** 容器运行封存来源、启动注册与 Factory composition
联组：**62 项，4.849 秒，OK，skipped=2**。新增两项实际 Linux 回归分别删除原 export
bundle 与 snapshot staging 后恢复、篡改归档内 bundle 拒绝；恢复不增加 fetch/event，
也不重建本地 artifact。此组没有修改 LocalGit HOME 或生产 GitRuntime，没有 Rosetta
豁免。仅只读挂载公开 codex_delivery、tests、codex_controller、ha_artifact_registry 四个
必要目录，其余隔离与下文相同。这仍是本机 Linux 验证，未证明 HA 现场资格。

此前快照在本机 Docker Desktop 的 Linux amd64 容器运行封存来源、
启动注册与 Factory composition 联组：**60 项，99.098 秒，OK，skipped=2**。跳过项为
仅供非 Linux 执行的 unsupported 边界，以及缺少 Node fixture。容器禁网、根文件系统
只读、cap-drop ALL、no-new-privileges、固定非 root UID，源码仅只读挂载；不授予
SYS_ADMIN、宿主目录写入或 Docker socket。真实 memfd 用例由容器 Linux 内核执行，
这不是 HA 现场运行时资格。

Docker Desktop 的 amd64 Git 受 Rosetta 影响，会向 GitRuntime 的专用空 HOME 写入缓存。
本地联组仅为 `LocalGit` 测试 fixture 分配独立私有空临时 HOME/XDG_CACHE_HOME，保持
原 XDG_CONFIG_HOME/GIT_CONFIG_* 和禁网约束；真实 seals 用例不使用该 fixture。
生产 GitRuntime 的空 HOME 门禁没有放松。联组和历史专项存在重叠，不累计历史计数。

同日恢复修复前的 macOS 封存来源专项：**19 项，0.002 秒，OK，skipped=15**；实际执行 4 项
闭合 schema、固定 Factory dispatch 和 unsupported 失败关闭边界。15 项 Linux 用例的
跳过不作为 macOS 支持证据。新增模块及接线的 AST 语法和空白检查通过。
