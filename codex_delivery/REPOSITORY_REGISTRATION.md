# 固定仓库启动注册

`repository_boot.py` 提供生产入口使用的固定数据 loader。下文的目录来源门禁与策略表
描述 `directory-v1`；显式策略 v2 的 `managed-sealed-v2` 使用
[封存来源协议](SEALED_REPOSITORY_SOURCES.md)，不要求目录只读挂载。两个版本不自动互换。
本地候选已经实现，但没有提供有效期内的生产注册、对应版本的真实来源资格、运行时资格
和独立观察事实时，仓库软件执行保持关闭。
单测中的 Git、挂载、HTTP、VM 资格 fixture 均不构成 HA 现场资格。

## 固定来源和默认关闭

只有受信 main 可以调用 `RepositoryBootPlan.load(forbidden_credentials=...)`。没有注册文件，
或注册精确等于 `{"version":1,"enabled":false}` 时返回 `None`；存在的坏注册必须使启动失败。
文件路径不来自 options、task、模型、环境变量、插件名或可执行模块名：

| 来源 | 固定路径和要求 |
| --- | --- |
| 私有注册 | `/etc/codex-delivery/repository/registration.json`，root 拥有，父目录 0700，文件 0600，单硬链接 |
| 镜像策略 | `/opt/codex-delivery/repository-policy.json`，root 拥有，所有祖先和文件禁止 group/other 写入 |
| Git | `/usr/bin/git`，逐祖先 no-follow/root provenance，实际原字节 SHA-256 必须匹配策略 |
| 源工作区 | `/var/lib/codex-delivery/repository-workspaces/` 的严格子目录，仅镜像策略可绑定 |

私有注册采用闭合 schema，四个字段是 `version`（整数 1）、`enabled`（布尔 true）、
`policy_sha256`（镜像 policy 原字节摘要）及 `target_observation_token`。令牌必须为
32～256 个非空白 ASCII 字符，通过既有受控私有部署方式注入，不能写进 Git、任务或日志。
loader 拒绝它与调用方传入的 Registry/Release/fnOS 等凭据及 policy 禁用指纹复用；
plan 的 repr 不输出秘密。调用方必须传入本次启动已加载的全部相关凭据。

读取逐层 no-follow，拒绝符号链接、可写祖先、非普通文件、多硬链接、超限、读取期间
元数据变化和重复 JSON 键；缺少私有注册时先检查缺失，避免 macOS `/etc` 别名使默认
关闭的普通 metadata 启动受影响。纯 `parse()` 只检验数据，不证明 root 文件来源；
`create()` 会重新读取固定 policy/Git，不能直接凭测试 fixture 获得生产资格。

## 闭合镜像策略 v1

policy 顶层必须精确包含：

| 字段 | 类型与语义 |
| --- | --- |
| `version` | 整数 1，不能是 true |
| `service_uid` | 正整数，必须与实际 real/effective UID、原 execution policy 的 host UID 一致 |
| `expires_at` | 带 `+08:00` 时区、尚未到期的 ISO 时间；每个 gate 再检查 |
| `git_sha256` | 固定 `/usr/bin/git` 原字节的 `sha256:` 摘要 |
| `scanner_digest` | 必须等于内置 `SCANNER_DIGEST` |
| `runtime_qualification_digest` | 原 qualified runtime 注册身份；attach 时由既有 TestPort 对账 |
| `repositories` | 1～32 个完整 RepositoryPolicy，每项精确含 `remote_identity/https_url/baseline_ref/publish_targets/revision` |
| `grants` | 1～32 个固定 task/revision 与完整 repo 集，结构见下 |
| `test_bindings` | 1～32 个固定 RepositoryTestBinding，与需要软件写入的 owner/repo 集精确相等 |
| `target` | 固定独立测试服务与 Manager GET 观察绑定，结构见下 |
| `forbidden_token_sha256` | 最多 128 个互不重复的禁用凭据 SHA-256 指纹 |

RepositoryPolicy 的 `publish_targets` 是 `[alias, ref]` 二元数组的数组。远端仅为既有
RepoManager 接受的 HTTPS URL；启动注册本身不提供 Git 登录、credential helper、SSH、
网络代理或私人仓库凭据来源。独立认证 transport 的实现与验证见
[Git transport](GIT_TRANSPORT.md)，不能把公开仓库 fixture 当作私人仓库认证资格。

每个 grant 精确含 `grant_ref/owner_ref/project_ref/task_ref/task_revision/mode/repositories/`
`code_task_ref/code_revision`。mode 只能为 `work/develop/deliver/deploy`。没有软件→代码
关联时，两个 code 字段均为 null；有关联时必须指向同一 policy 中明确登记的同 owner、
同 project、完整 repo/policy/access 集一致的 `work` grant 和精确 revision。任务路径不能
替代该绑定。`bind_code_task` 另外核验这组关联及固定 target/permissions；Work 即使
canonical repo access 是 write，也只获得源归档与输入读取，不获得 assemble/import/publish。

每个 grant.repositories 项精确含 `repo_ref/policy_digest/access/workspace_ref/root/device/inode`。
access 只能为 read/write；policy_digest 必须引用上述 RepositoryPolicy 的规范摘要。
root 是固定工作区根的规范严格子路径，device/inode 是原目录身份。workspace_ref/root
全局不得重复，禁止把一个 workspace 偷换成不同任务或 revision 的现场源。task_repositories
必须与这组完整 repo/access/空 paths_json 精确相等，增加 repo、缩小到路径、改 access、
取消任务、revision/mode/project 漂移、恢复模式或 pending durability 都撤销 gate。

每个 test_bindings 项精确含 `owner_ref/repo_ref/repository_policy_digest/runtime_id/recipe/`
`source_prefix/template_digest`。recipe 仅接受 [既有固定配方](REPOSITORY_BOOT.md)，
template_digest 必须是该 recipe 和 source_prefix 内置程序的真实摘要；不能传 shell、
npm script、可执行路径或自行声明 passed。该表覆盖非 Work grant 的全部 write owner/repo，
不能有重复或额外身份。runtime 必须由原 execution 注册为匹配 interpreter 的 work-qemu-v1。

target 精确含 `origin/target_ref/project_ref/permissions_digest/observation`。observation
精确采用 release-authority 的 TARGET_FIELDS：target、instance、slug、project、初始 release、
baseline_etag、config_digest、synthetic_data_digest、manager/backup principal、备份存储
reference/domain 和必须为 true 的 canary_no_business_data。所有 grant 限于这个 project。
调用使用内置、限时、无 ambient proxy 的 ManagerTargetObservationClient，只做固定 Manager
GET，不从任务选择 URL、角色或返回字段。实际结果除额外 qualification_expires_at 以外
必须与固定 observation 完全相等，期限必须有效；结果摘要绑定真实响应。整个 candidate
repo/policy 集与原 canonical scope 在网络读取前后复查，不能缓存 approved=true。

## 真正停写、扫描和撤销

工作区的每一层必须通过目录 fd 的 no-follow 打开；根目录必须为服务 UID、0700，
device/inode 必须匹配。内核 fstatvfs 的 ST_RDONLY 和 `/proc/self/mountinfo` 的实际挂载
以及底层 superblock 都必须只读，所有位于源下的嵌套 mount 也必须只读。仅 ro bind
挂到仍可写的底层文件系统会被拒绝，因为另一 writable alias 仍能改变同一批字节。
chmod、flock、任务 stopped 字段和虚构 mount 表都不构成生产停写证明。

loader 不执行 mount/remount，也不取得 SYS_ADMIN。管理员须在独立授权的准备阶段提供
真实只读源；缺少该源就拒绝 create/capture，不回退普通可写目录。原 RepositoryArchive
继续跨捕获核验逐文件和 Git 状态；源从不作为宿主项目执行目录。

输入扫描实际读取被封装的 tracked regular bytes，候选扫描继续读取 Git 历史 blob。
内置 scanner 具有固定规则摘要和正反自检，拒绝二进制、未知编码、敏感路径、已知秘密
模式和超限。范围是保守的 UTF-8/已知模式检查，不能保证识别全部秘密。SourceExposureReceipt
绑定原 context/manifest 和实际 scan，不以名字或“检查通过”的字符串代替实际扫描。

每次 gate 重读 root policy 原字节与固定 Git 原字节并检查期限，替换、删除或到期立即
撤销运行中的 gate。私有 registration 的停用在下一次进程启动读取；需要立即撤销时
使用受控停服或撤销/替换非敏感镜像 policy。不能声称进程能在降权后重新读取 root 私有令牌目录。

## 唯一原执行器接线

1. root 阶段 load 各固定 plans，拒绝独立凭据复用；对齐各 plan 与 execution 的 service_uid。
2. 原统一降权后，先构造 Registry ports；`plan.create(build_executor=ports.execute, build_observer=ports.observe)`
   只允许一次尝试，失败不能换输入重试；它创建延迟 Factory，并不启动任务或网络请求。
3. 将 Factory 传入原 QualifiedExecution，再构造原 DeliveryService。Factory 在第一次
   `__call__` 绑定同一个 Store/Archive；不额外创建 Store、Runner、Coordinator 或 VM。
4. 既有 Registry ports attach 到同一 Factory 的 manager/archive 后，调用
   `plan.attach_validation(factory, execution, service.coordinator)`，由原 Factory/TestPort
   验证真实 runtime 与原 source/archive/coordinator；随后 attach 原 software worker。
5. 所有装配成功后才能启动 service/worker。关闭遵守 [原生命周期](REPOSITORY_BOOT.md)：
   worker 未结算返回 false 时保留 singleton 锁和 execution，不能提前释放并替换。

固定 schema 与 loader 的本地验证不代表其对应生产 policy 已签发、对应版本的真实来源可用、
测试运行时已验收，或整个软件交付已经部署。现场仍须完成该同源 recipe 的真实 VM
运行、源/checkpoint 离机确认、进程重启恢复与发布/回滚闭环，分别保留证据。

本地专项为 **24 项通过，7.401 秒**，含闭合 schema/凭据复用/策略撤销、真实同库
取消与完整 repo/access fencing、明确标注 fixture 的 Linux mount/Manager 边界、真实
临时 Git/AES 软件 pipeline，以及 loader 创建的 Factory→原 QualifiedExecution/
Coordinator→内部测试结果→候选 import 的同源接线。专项实际测试模板只执行测试文件
构造的本地源码；挂载和 runtime facade 仍为 fixture。此前与 Factory composition
联组的 40 项结果有重叠，不把历史计数相加。新模块/测试的语法、文档引用和空白检查通过。
