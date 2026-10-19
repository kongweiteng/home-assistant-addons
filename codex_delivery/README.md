# Codex Delivery：持久化基础候选

当前为 `0.0.1` 本地开发基础库，尚无可安装的 Add-on、HTTP provider 或后台执行器。目录中的 Skill 尚未安装到运行实例；Python 函数只能由受信服务调用，不能直接接收模型参数。本候选不提供 Git fetch/push、Codex 执行、构建或部署权限。

已实现项目/仓库分别登记、多对多绑定、项目生命周期与配置版本、固定项目版本及仓库集合的任务、需求补充、任务控制、owner/project/task 归属校验、CAS、幂等输入、分页查询、不可变事件链和通知 outbox。多个项目可以引用同一仓库；子路径读取隔离尚未实现，因此这种绑定明确拒绝。

本地元数据在私有 SQLite/WAL 中保存，`synchronous=FULL`。变更在同一事务中写入实体、事件、幂等记录、待备份记录与通知 outbox，然后生成一次性的完整 SQLite 元数据快照。快照先落盘并 fsync，再交给受信 Replica 接口；同一事件的重试复用相同字节，避免“已上传但回执丢失”后产生冲突版本。

只有经校验的独立故障域回执才能返回 `accepted`。没有适配器、回执不符或上传状态不明时返回 `durability_pending`，保留本地输入并阻止后续业务变更；恢复通道后对账原事件。SQLite 事务不会跨越网络调用，跨进程写锁覆盖完整的保存、快照和回执过程。适配器必须在固定时限内返回；目前没有生产适配器或离机保存承诺。

恢复只接受摘要匹配的受信副本，写到新的私有目录，并校验 schema、SQLite 完整性、外键和事件链。恢复实例保持只读，不能领取通知、启动任务或恢复外部写入；唯一写者与现场结果对账后才能另行启用。测试中的模拟副本不证明真实 NAS 可恢复。

`origin.py` 提供版本 1 的可信来源签名/校验原语，绑定 actor、会话、来源事件、输入版本、原始输入摘要、调用名称与参数、受众及短有效期。它尚未接入 Controller；验证来源也不等于获得项目执行授权。旧 provider 协议保持不变。

## 私有持久目录

```text
<data>/state/delivery.sqlite3
<data>/state/delivery.sqlite3-wal
<data>/state/delivery.sqlite3-shm
<data>/state/delivery.sqlite3.writer.lock
<data>/state/delivery.sqlite3.snapshots/EV-….sqlite3
```

目录权限 `0700`，数据库、锁和快照为 `0600`。不要放在临时目录或容器可写层。快照包含私有项目元数据与需求；生产副本必须使用登记的加密备份域。此版本保存的是目录和任务元数据，不包含 Git 对象、脏文件、制品或生产数据，不得据此宣称源码已备份。

`remote_identity` 只接受管理员适配器规范化后的 provider 身份，例如 `github:example/sample`，不接受 URL、凭据或命令。实际 provider 的仓库 ID、大小写及别名规范化仍需登记适配器校验。`active` 是项目目录状态，不代表 Runner、构建、授权或部署已就绪；任务的 `execution_enabled` 固定为 false。

## 验证

需要 Python 3.11+，且 SQLite 提供 deserialize 支持。在 Add-on 仓库根目录运行：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python3 -m unittest discover -s tests -p 'test_codex_delivery_*.py'
```

测试覆盖重复输入、参数冲突、两个项目与共享仓库、跨 owner/project 访问、CAS、任务仓库集合冻结、暂停归档、离机超时、回执错误、并发写者、重启、恢复、损坏快照、通知未知不重放及上海时区边界。

## 下一步门禁

还需交付真实加密离机适配器与演练、可信来源及入站 journal 的 Controller/Gateway 集成、短请求 provider、HA Ingress 管理、目标及授权目录、后台通知服务身份。之后才是 Runner 交付协议、仓库/工作区管理、Git Publisher、构建和部署域。没有 `config.yaml` 是为了避免将基础库误安装为已经具备能力的 HA 服务。
