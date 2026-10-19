# Codex Delivery：持久化与短请求候选

当前为 `0.0.2` 本地开发库，含内部 HTTP provider 适配器和元数据 worker，尚无可安装 Add-on、常驻服务入口或开发执行器。目录中的 Skill 尚未安装到运行实例。当前 Controller 缺少必需的可信来源字段，不能启用此候选；Python 函数只能由受信服务调用，不能直接接收模型参数。本候选不提供 Git fetch/push、Codex 执行、构建或部署权限。

已实现项目/仓库分别登记、多对多绑定、项目生命周期与配置版本、固定项目版本及仓库集合的任务、需求补充、任务控制、owner/project/task 归属校验、CAS、幂等输入、分页查询、不可变事件链和通知 outbox。多个项目可以引用同一仓库；子路径读取隔离尚未实现，因此这种绑定明确拒绝。

本地元数据在私有 SQLite/WAL 中保存，`synchronous=FULL`。变更在同一事务中写入实体、事件、幂等记录、待备份记录与通知 outbox，然后生成一次性的完整 SQLite 元数据快照。快照先落盘并 fsync，再交给受信 Replica 接口；同一事件的重试复用相同字节，避免“已上传但回执丢失”后产生冲突版本。

只有经校验的独立故障域回执才能返回 `accepted`。没有适配器、回执不符或上传状态不明时返回 `durability_pending`，保留本地输入并阻止后续业务变更；恢复通道后对账原事件。SQLite 事务不会跨越网络调用，跨进程写锁覆盖完整的保存、快照和回执过程。适配器必须在固定时限内返回；目前没有生产适配器或离机保存承诺。

恢复只接受摘要匹配的受信副本，写到新的私有目录，并校验 schema、SQLite 完整性、外键和事件链。恢复实例保持只读，不能领取通知、启动任务或恢复外部写入；唯一写者与现场结果对账后才能另行启用。测试中的模拟副本不证明真实 NAS 可恢复。

`origin.py` 提供版本 1 的可信来源签名/校验原语，绑定 actor、会话、来源事件、输入版本、原始输入摘要、调用名称与参数、受众及短有效期。Delivery provider 已强制校验，但 Controller 签发端尚未接入；验证来源也不等于获得项目执行授权。现有其他 provider 协议未改动。

## 短请求与元数据 worker

`DeliveryHTTPServer(address, provider)` 仅供受信启动器显式构造，没有默认监听器。唯一入口为 `POST /internal/v1/tools/call`；服务 Bearer 与独立来源签名都必须有效，查询也不例外。请求顶层严格为 `version/name/arguments/context/origin`，version 为 1；context 严格为 `job_hash/turn_hash/idempotency_key`，只作调用上下文，不决定 actor、授权或操作身份。来源签发必须在模型参数之外完成。禁止公网暴露此内部 HTTP 接口。

`provider.manifest()` 生成封闭输入/输出 schema，已通过现有 Controller schema 和注册工具校验；这只证明 schema 兼容，尚未注册或启用扩展。当前 Controller 的旧请求不含 origin，会被拒绝，不能靠安装 manifest 绕过这一缺口。

| 工具 | 行为 |
| --- | --- |
| `projects/repositories/list` | 当前 owner 的项目、仓库、任务目录；分页，不返回私有远端身份或原始需求 |
| `status/events/operation_status` | 归属校验后的本地任务、事件与操作状态；不隐式触发远端动作 |
| `plan/revise` | 保存任务需求或补充，固定项目/任务 revision |
| `control/project_control` | 保存暂停、恢复、取消或项目归档等元数据变更，不停止或启动真实执行器 |

写入口仅将原始参数、已校验来源和稳定操作 ID 事务性提交到 `input_journal`，返回 `durability_pending/local_only`。稳定 ID 绑定 actor、来源事件、输入版本、工具及资源；改变 context 幂等键不会制造新任务，替换已登记内容会冲突。队列默认上限 128，SQLite 入队竞争等待 250ms；不会等待离机上传或长时间占有元数据写锁。此 journal 属于 Delivery，**尚未实现 Gateway 推进消费游标前的通用可靠入站 journal**。

`MetadataWorker.run_once()` 以独立跨进程锁串行处理白名单元数据方法，并沿用数据库幂等键、CAS 和离机回执门禁；没有网络服务/自动调度循环。收到独立副本确认后才将操作标为 accepted；失败会保留输入。语义冲突可拒绝，提交后存储异常则保留结果待确认。进程在元数据提交后退出，重启沿原键对账；这项重试仅覆盖本地元数据，不允许重放外部写操作。队首未确认时不跳过；已有管理员操作若待备份，受信管理端须先对账该事件。

HTTP 请求体上限 64KiB、输出上限 128KiB、同时处理上限 8，读取头部及正文共享默认 5 秒总时限。拒绝重复 JSON 键、非有限数值、重复鉴权/长度头、分块或压缩请求；不输出请求、鉴权、原始异常日志。生产反向代理、TLS/网络隔离、服务生命周期与真实端到端响应时间仍需验收。

## 私有持久目录

```text
<data>/state/delivery.sqlite3
<data>/state/delivery.sqlite3-wal
<data>/state/delivery.sqlite3-shm
<data>/state/delivery.sqlite3.writer.lock
<data>/state/delivery.sqlite3.metadata-worker.lock
<data>/state/delivery.sqlite3.snapshots/EV-….sqlite3
<data>/state/delivery.sqlite3.v1-backup-….sqlite3
```

目录权限 `0700`，数据库、锁和快照为 `0600`。不要放在临时目录或容器可写层。快照包含私有项目元数据与需求；生产副本必须使用登记的加密备份域。此版本保存的是目录和任务元数据，不包含 Git 对象、脏文件、制品或生产数据，不得据此宣称源码已备份。

`remote_identity` 只接受管理员适配器规范化后的 provider 身份，例如 `github:example/sample`，不接受 URL、凭据或命令。实际 provider 的仓库 ID、大小写及别名规范化仍需登记适配器校验。`active` 是项目目录状态，不代表 Runner、构建、授权或部署已就绪；任务的 `execution_enabled` 固定为 false。

## schema 迁移与恢复

数据库 schema 当前为 2。打开 v1 数据库默认返回 `schema_upgrade_required`；受信管理员显式传入 `DeliveryStore(..., upgrade_schema=True)`，才会先生成 `0600`、fsync 的升级前 SQLite 副本，再事务性迁移。失败回滚，保留副本；不要在活动旧写者仍运行时升级。升级前副本包含私有元数据，不可提交 Git。

恢复 v1 快照默认同样拒绝，且不创建目标文件；显式 `DeliveryStore.restore(..., upgrade_schema=True)` 才允许升级。恢复保护在转换之前写入并持久保留，转换、重启均不恢复执行或通知权限。原快照和升级前副本应留存；后续版本降级不能直接打开已迁移的数据库。

## 验证

需要 Python 3.11+，且 SQLite 提供 deserialize 支持。在 Add-on 仓库根目录运行：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python3 -m unittest discover -s tests -p 'test_codex_delivery_*.py'
```

测试覆盖重复输入、参数冲突、两个项目与共享仓库、跨 owner/project 访问、CAS、任务仓库集合冻结、暂停归档、离机超时、回执错误、并发写者、真实子进程退出、落盘后异常、重启、恢复、损坏快照、schema 升级/失败回滚、HTTP framing/总读取时限、通知未知不重放及上海时区边界。副本及身份均为本地 fixture，不代表真实备份或手机验收。

## 下一步门禁

还需交付真实加密离机适配器与演练、可信来源及通用入站 journal 的 Controller/Gateway 集成、HA Ingress 管理、目标及授权目录、后台通知服务身份和受控服务入口。之后才是 Runner 交付协议、仓库/工作区管理、Git Publisher、构建和部署域。缺少开发主机不阻止独立本地开发；在合格沙箱和节点登记验收前，保持 execution_enabled=false。没有 `config.yaml` 是为了避免将候选库误安装为已经具备能力的 HA 服务。
