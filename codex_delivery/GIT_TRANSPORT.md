# 固定 HTTPS Git 身份传输候选

此组件补齐受信 RepoManager 的 HTTPS 认证能力，不把 Git Token 交给模型、VM、
Controller 或项目程序。它不授予任何仓库/分支的业务写权限；原同库授权、候选验证、
唯一 push claim、未知结果只读对账和禁止重推仍由 RepoManager 执行。

固定镜像策略 `/opt/codex-delivery/repository-git-policy.json` 登记服务 UID、期限、
完整 RepositoryPolicy 与其认证用户名、禁止 Token 指纹。私有 root 注册仅在
`/etc/codex-delivery/repository-git/registration.json` 保存对应策略摘要及每仓库独立的
read/publish Token，父目录0700、文件0600、单硬链接。缺失或精确 disabled 时不启用；
错误注册失败关闭。策略和注册均为闭合数据，不能选择 URL 之外的代码、helper或路径。
私有 Token 必须先有独立机器凭据登记；此候选不把本机个人 Git 凭据复制到 HA。

`load()` 是唯一建立 root 注册来源的入口；纯 `parse()` 结果不能 `create()`。
root 注册在降权前读取，运行时不尝试由 service UID 重开 root 的0700/0600目录。
文件读取逐层使用 `openat/O_NOFOLLOW`，检查 root 所有权、可写位、常规文件和单链接，
读取后同时比较打开文件与原目录项的身份、模式、大小和修改时间，替换/硬链接竞态拒绝。
`parse_policy(raw)` 只校验非秘密的闭合镜像策略，不读取文件、不取得凭据或建立资格。

认证 runtime 只在固定 `fetch`、`ls-remote`、`push` 参数模板、精确 HTTPS URL 与
登记 ref 匹配时选取角色。读操作使用独立 read Token；push仅使用publish Token。
fetch 的远端只能是登记 baseline，目标严格为原 RepoManager 生成的
`refs/codex/fetch/SYNC-<32位小写hex>`，不能写其他本地 ref。`ls-remote` 只查询登记
baseline或发布分支；push只接受完整40/64位小写对象ID和登记发布分支，不接受额外flag。
所有仓库的 read/publish Token 均须彼此不同，publish可为 `null` 以拒绝发布。
原 Repository 观察身份及其禁止指纹也不能复用；`reserved_credentials`只供受信启动器
在内存中继续执行跨组件去重，不是面向任务、API或日志的凭据导出接口。
作用域为精确 URL 的 HTTP Basic header 仅通过受信 Git 子进程的私有环境注入，
不进入URL、argv、仓库config、文件、任务或日志；token不被guest继承。Git继续清理
ambient环境、关闭helper/hooks/redirect、强制TLS校验和固定命令预算，原stderr只计量
后丢弃，不将远端回显的认证材料带出；异常的成功stdout若含Token或Basic编码也拒绝。
每次子进程前后重新核验实际UID/EUID、镜像policy及原RepositoryBootPlan的固定Git资格，
验证消耗仍计入原总deadline。到期、policy字节变更/禁止指纹更新、Git身份变更或远端
撤销Token均会阻断后续操作；删除boot注册文件本身不撤销已加载进程的内存身份，须停用
对应进程或更新其镜像policy/撤销远端Token。未知push不自动重试；publication claim与
仅ls-remote对账仍由原RepoManager负责，此transport不会取代它的唯一写入门禁。

只有受信启动器可以构造此runtime，并必须把原RepositoryBootPlan的Git/policy核验
能力传入；源码数据 `parse()` 不能证明root来源或正式资格。注册的期限、实际仓库权限、
独立测试分支的fetch/push/remote OID、撤权与未知写恢复仍须现场验收。新增机器身份的
签发与安装需要明确限定目标和权限，不以本地fixture成功代替授权。

本地专属测试使用可信临时可执行程序与合成身份，实际启动子进程验证env-only角色、
无ambient继承、固定语法、总输出/超时预算、stderr丢弃、后置撤权及一次失败push后的
只读角色查询。文件门禁同时覆盖真实内核nofollow/单链接/替换竞态，并可用
`REGISTRY_GIT_AUTH_IMAGE=<本地arm64候选镜像>`开启无网络、无host mount、read-only
root、drop-all、no-new-privileges的真实root容器文件检查。未开启该变量时此项明确跳过；
macOS文件测试中的root所有权投影不等于现场root来源。均不代表真实Git服务的认证、
仓库权限、TLS、撤权或端到端发布验收。
