# Codex Delivery 候选服务

本包只承载持久任务元数据、内部短请求入口与元数据 worker。它不在 HA 进程中运行生成代码，不安装浏览器，也不执行 Git、构建或部署。功能完整并完成独立沙箱、备份与恢复验收前，不应安装到正式实例或宣传为可执行平台。

## 运行边界

- 固定内部端口 `8131`；无 Ingress、宿主端口、host network、主机挂载、Supervisor/HA API、额外 capability 或 Docker socket。
- 私有状态目录 `/data/state`，目录 `0700`，数据库、快照与锁 `0600`；启动 `umask=077`。
- 业务调用同时要求独立服务 bearer 与可信来源签名；Controller 必须在模型参数之外签发与 actor、会话、来源事件、输入和调用参数绑定的 origin。
- GET `/healthz` 只提供脱敏运行摘要。`status=ok` 表示元数据服务运行，`execution_enabled=false` 和 `qualified_profiles=[]` 明确表示执行未启用。

## 配置

秘密值由批准的凭据源注入 HA 私有 options，不通过聊天、命令参数、Git 或普通日志传输。模型与任务程序不能取得这些值。

| 字段 | 语义 |
| --- | --- |
| `extension_bearer` | 独立内部服务认证值，至少 32 个 ASCII 可打印字符 |
| `origin_key` | 与 Controller 来源签发端相同的 UTF-8 签名键，至少 32 字节；不复用 bearer |
| `allowed_actors` | 管理员从 Controller 登记的精确 `actor:<64位小写SHA-256>` 列表，不接受聊天中自报的 owner |
| `local_domain` | 本地故障域标识，默认 `ha-delivery` |
| `replica_domain` | 已登记独立故障域；空值不提供离机确认，仅填写名称也不构成副本实现 |
| `upgrade_schema` | 默认 false；旧 schema 启动前拒绝，显式开启后先保存私有升级前数据库 |
| `worker_enabled` | 默认 true；只执行白名单元数据方法，恢复实例仍只读 |
| `worker_interval_seconds` | 1～30 秒；失败与队首待副本确认均按间隔核对，无忙循环 |
| `journal_capacity` | 1～1024，默认 128；本地持久入站队列上限 |
| `execution_enabled` | 必须为 false；true 会拒绝启动，不能用配置绕过节点资格 |
| `replica` | 默认 disabled；已有网络挂载的加密副本配置，详见下文；本身不会创建共享或挂载 |
| `fnos_file_replica` | 默认 disabled；独立 NAS 存活/主存储丢失恢复候选，与 `replica` 互斥；options 不授予资格或选择凭据文件 |
| `runner` | 默认 disabled；独立节点和 callback 私有配置，必须匹配镜像内固定策略及真实资格；当前 metadata 镜像无策略/资产，不能启用 |

受信启动、不可逆降权、Registry presence、canonical lease、同实例字节归档和关闭顺序见 [EXECUTION_COMPOSITION.md](EXECUTION_COMPOSITION.md)。配置字段不能指定命令或镜像资产路径；未知/过期身份、副本未资格、恢复模式或 worker 停用时不提供可用执行能力。

启用 `replica` 需同时登记不同于 `local_domain` 的 `replica_domain`。其闭合字段为 `enabled/root/fstype/source/mountpoint/key_ref/key/timeout_seconds`。`root` 必须是专用私有对象目录，位于精确 `mountpoint` 内；`fstype` 只能为 nfs/nfs4/cifs，`source` 是不带账号或参数的精确 server/export 或 UNC 共享身份；`key` 是独立 32 字节 AES 密钥的规范 Base64，`key_ref` 为受控凭据索引，禁止复用 bearer/origin key；超时 1～30 秒，默认 10 秒。

启动配置校验不探测网络存储；服务只在本地私有状态目录创建唯一 `replica-journal.json`。密文 I/O 由独立受信进程执行，密钥和任务明文留在 parent；整笔预算耗尽后熔断，不把终止请求当作 worker 已退出。重启保留挂载 pin、未知对象及独立进程锁；原事件只可 GET 同一对象对账，缺失仍 unknown，不重写或改 journal 绕过。健康信息只返回副本状态，不公开 source、路径或密钥。

该配置不自动授予挂载权限。候选 metadata Add-on 当前没有网络存储目录映射，真实部署前还须按已选独立存储完成最小映射、实际读写/超时及恢复验证；本地目录、名字相同的本机目录或仅填 `replica_domain` 均不能取得离机回执。同步本地 journal 的存储可用性是前置条件，不能宣称 Python 能约束所有内核磁盘故障耗时。

`python3 -m codex_delivery.main --check-config` 只校验配置，不初始化数据库；输出不含秘密。`--print-manifest` 输出工具契约，也不初始化服务。未知配置、重复 JSON 键、非有限数值及无效 actor 均拒绝。

## 独立 fnOS 候选

`fnos_file_replica` 只提供 `nas-live-primary-loss-v1`，要求 NAS 在主存储丢失期间继续存活。它不证明 NAS 同时断电后的介质稳定性，也不替代 NFS/CIFS 后端的 fsync 契约。最小禁用值为 `{enabled: false}`；启用时必须完整提供 schema 中的 policy、固定 HTTPS endpoint、专用 directory、store/namespace/qualification/credential/key 引用，可选 timeout_seconds（1～30）与 maxbytes。独立 `replica_domain` 必须非空。

生产候选入口仅从固定 `/opt/codex-delivery/fnos-policy.json` 读取 root-owned 策略、`/data/fnos-replica-key.json` 读取独立 root-owned 0600 AES key；在不可逆降至 UID65534 后，使用该 UID 已预置的 `/data/fnos-auth` 私有 OAuth 目录及固定 SHA 校验的 `/opt/codex-delivery/fnos/bin/trim-cli`。不自动创建授权、导入会话、修复权限、递归 chown 或清除未知 journal。guest 无权接触会话或密钥。

策略绑定精确设置、官方 CLI 摘要、私有后端授权引用、校准回执及有期限的恢复证据。启动只 GET 精确校准对象；摘要/解密/绑定失败拒绝启动，不上传或寻找 latest。Store 与源码/检查点归档共用唯一副本，过期或 fused 不提供写资格。`--check-config` 不访问 NAS、不降权或创建状态，不等于已取得运行资格。

本候选默认关闭。官方 Skill 将 file session 限于测试/CI；长期生产的例外授权须按具体部署明确登记，本源码包不携带现场授权证明，不能仅写入 policy 的授权引用宣称已获用户许可。还需验收长期 key/recovery kit、正式镜像中的 CLI 与资产、现场启动及刷新；本地 TLS/AES 测试和临时 HA→NAS 合成恢复均不能替代这些步骤。

## 持久性与停止

发布域事实入口由受信启动器以 `release_authority_factory(store)` 注入，严格复用同一 `WorkStore`，默认没有该能力。固定 `/internal/v2/release-authority` 提供唯一任务scope、broker/manager/backup各自target视图、Release和精确source资格；每个角色使用与业务扩展、来源及Runner分离的token和白名单。任务scope不含需求正文、会话或秘密，直接读取当前revision/state/mode/control_pending；待副本确认、恢复状态和项目停用时拒绝。目标与构建观察Port缺失或超时返回503；固定启动装配见 [RELEASE_AUTHORITY_BOOT.md](RELEASE_AUTHORITY_BOOT.md)：降权前读取root私有注册和镜像固定策略，构建服务时接同库factory；目标事实从Manager独立只读接口取得。来源/Release reader仍需可信同库factory显式注入，缺失保持503，不能用fixture或options生成资格。

HTTP 入口将输入保存后返回 `durability_pending/local_only`。元数据 worker 循环沿稳定操作键核对既有结果，只有受信独立副本的校验回执才能变为 `accepted/replicated`。可由受信启动器注入副本适配器，或显式配置上述加密网络副本候选；默认关闭，真实存储尚未验收，不把本地目录、测试 fixture 或同一主机路径当作离机副本。

SIGTERM/SIGINT 先停止后续元数据处理并关闭 HTTP 监听，有界等待正在进行的处理；无法停止会报 `service_shutdown_incomplete`。重启保留输入、任务、快照和事件，未知外部动作不重放。备份保持 cold，升级或降级前冻结写者并保存现有数据；旧 schema 不覆盖最新业务状态。

## 验证

在 Add-on 仓库根目录：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python3 -m unittest discover -s tests -p 'test_codex_delivery_*.py'
bash -n codex_delivery/run.sh
```

本地 HTTP、配置、停止与模拟副本测试不等于 HA 功能验收。上线仍须记录精确镜像、备份/回滚、合格执行节点、双端副本与恢复演练及原会话文件交付证据。
