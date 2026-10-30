# 外部保留的签名恢复包

`recovery_kit.py` 是显式管理员工具，不是任务工具或启动时自动恢复路径。它为一个停写检查点生成签名 descriptor；恢复在全新进程中，只按 descriptor 的精确对象引用读取 NAS，解密、验签、验证全部列出文件后，发布到新的私有目录。恢复数据库永久保留 `recovery_required` 只读保护，不领取任务、不发送通知、不重放构建或部署。

本契约只覆盖 **HA 主目录完全不可用，fnOS 存活并仍可读取对象**。它不证明 NAS 断电后的介质稳定性、HA 与外部保留主机同时失效、完整 HAOS/Core/Add-on 恢复或应用业务数据完整恢复。故障域名称只是管理员登记，不证明物理独立性。真实环境验收与本地 fixture 测试必须分开记录。

## HA 之外必须长期保留的材料

| 材料 | 内容及作用 |
| --- | --- |
| `RK-<sha256>.json` | Ed25519 签名、精确 SQLite receipt 与事件链末端、NAS 绑定、显式 archive scope/receipt/完整文件 inventory、未覆盖列表；不含明文文件、AES key、签名私钥或 OAuth token |
| 公钥 anchor | 闭合 JSON：`version=1`、`signer_ref`、规范 Base64 的 32 字节 `public_key`；来自独立签名密钥登记，不能从待恢复 descriptor 自信任 |
| 独立 AES 凭据登记 | Broker JSON：`version=1`、`key_ref`、`failure_domain`、规范 Base64 的 32 字节 `key`；与 descriptor 的 key/domain 完全匹配 |
| 签名凭据登记 | 导出需要：`version=1`、`key_ref`、`algorithm=Ed25519`、规范 Base64 的 raw32 `private_key` 和 `public_key`；恢复不需要私钥 |
| 可用读取身份与程序 | 独立取得的短期 fnOS access token、当前受信恢复源码、支持 SQLite deserialize 的 Python 3.11+ 和 cryptography 依赖；不能只保留在失效的 HA 目录 |

descriptor 也含私有 NAS 定位、文件名和项目引用，只能留在批准的私有外部存储，不能提交公开 Git。外部保留根目录必须属于当前 UID、绝对路径、`0700`、无符号链接。descriptor、request、anchor 必须是同 UID 的 `0600` 普通文件，单硬链接；其直接父目录同样为 `0700`。AES 与签名密钥必须通过批准的 Broker 受控注入，禁止导出为普通文件或置于命令参数、聊天、环境转储及日志。

## 创建停写检查点

1. 由拥有持续授权的管理端停止该检查点范围内的写者，完成未确认事件的精确对账；保留原始 journal 和所有已有快照。工具不会自行停止服务，也不读取生产 SQLite 路径。
2. 冻结一个已有 SQLite snapshot 的精确 receipt；snapshot 必须绑定事件链末端。除快照自身尚未确认的 durability 行外，任何历史 pending 都拒绝导出。仅支持已审查的 schema5 和 schema6，完整性、外键与事件链均须通过。两版本均验证原 task/repository/release schema；schema6 另验证完整 group 表、索引与触发器定义，schema5 不要求尚未引入的 group schema。
3. 从同一 snapshot 的事件取得要保留的 archive，填写完整 scope 与 receipt。管理员提供以下闭合 request（实际值仅写私有文件）：

```text
version: 1
checkpoint_ref: 稳定检查点引用
signer_ref: 独立签名密钥引用
primary_domain: 原 HA 故障域
replica_domain: NAS 存活故障域
retention_domain: 外部 descriptor 保留故障域
fnos_file_settings: FnosFileSettings 的完整字段
database_receipt: {event_id, sha256, failure_domain, object_ref}
archives: [{archive_ref, event_id, scope, receipt}, ...]
uncovered_items: [非空且不重复的未覆盖类别引用, ...]
```

`scope` 严格为 `{owner_ref, task_ref, revision, checkpoint_ref}`；revision 必须为正整数。三个故障域必须不同。archives 最多 128 个，不允许重复名称或 receipt；每条必须真实存在于指定事件，owner/task 与事件一致，manifest 的 scope 摘要也必须匹配。

4. 在 Add-on 仓库根运行以下入口，由受信凭据注入器连接 stdin；下列参数路径均为示例，不能直接指向生产目录：

```sh
python3 -m codex_delivery.codex_delivery.recovery_kit export \
  --request /private/recovery-admin/request.json \
  --retention-root /private/external-retention \
  --scratch-root /private/recovery-scratch
```

安装为单层 Python 包时入口为 `python3 -m codex_delivery.recovery_kit`。stdin 是一个最多 16 KiB 的闭合 JSON：`{version:1, replica_key:<完整AES登记JSON>, access_token:<短期token>, signing_key:<完整签名登记JSON>}`。CLI 在读取凭据前将 core dump 硬限额设为 0，不打开 OAuth 会话、不刷新、不上传、不查找最新对象。固定总读期限为 300 秒，管理员每对象读取最多 120 秒，全部 DNS/连接/正文共享该对象期限；总期限始终优先，超时永久熔断该读取实例。这与在线写入的登记 settings 时限分开，避免独立恢复路径较慢时无法取回大 chunk，不改变写入超时或任何对象身份。

导出会实际读取、解密和恢复所有列出 archive 的每个 chunk，在私有 scratch 中核验后删除临时明文，才签名。descriptor 采用不可覆盖的内容寻址名称，以文件和目录 fsync 保留；重复保留必须字节完全相同。成功 stdout 仅含检查点引用、descriptor 摘要和 `retained=true`，不返回密钥或 token。应独立核实该文件确实在 HA 之外持久存在，并保留可信公钥 anchor；`retained=true` 本身不证明物理故障域。

## 覆盖范围

SQLite 元数据及其完整事件链属于 descriptor 覆盖范围。当前 archive 收集识别认证事件内的 `source_archive`、`checkpoint_archive` 和 `archive` receipt，包括 `repository.archive`、`software.archive`、软件 build request 内的嵌套引用；同一 receipt 在多个事件出现时合并身份并保留全部事件 ID。

所有已识别但未选中的 receipt 必须出现在 `coverage.uncovered_archives`，不能默默忽略。管理员还必须以 `uncovered_items` 列明未归档 Git/工作区、活动任务最后检查点之后的输出、未登记制品、业务卷、OAuth 会话、其他 HA 服务等实际缺口。新类型对象或新字段不自动取得覆盖承诺；增加类型时必须同步更新识别和验收。空的 `uncovered_archives` 只说明这些已识别 archive 都列入，不代表整机或全部业务已备份。

仓库 archive 包含经过 Repo Manager 封存的恢复材料，工具只恢复这些原始文件；还原实际 Git 工作区需另行使用受信的 Repo Manager 离线恢复接口及其完整验证，不能把解包后的文件目录直接当作可运行项目。未入 archive 的 Git 对象、脏文件和忽略文件不在本工具承诺内。

## 主目录失效后的恢复

先在可信管理环境取回外部 descriptor、独立 anchor、Broker AES 登记及短期 fnOS 读取 token。读取 token 应通过官方认证流程独立取得；凭据过期必须停止并重新认证，不能靠本工具刷新失效的 HA 会话。

```sh
python3 -m codex_delivery.codex_delivery.recovery_kit recover \
  --descriptor /private/external-retention/RK-<sha256>.json \
  --anchor /private/external-retention/anchor.json \
  --destination /private/recovery-parent/new-tree \
  --local-domain new-recovery-domain
```

恢复 stdin 为 `{version:1, replica_key:<完整AES登记JSON>, access_token:<短期token>}`，不允许 `signing_key`。destination 必须不存在，父目录必须为私有 `0700`；同一父目录不能有其他并发管理写者。先验签和闭合协议、再读取精确对象；事件链末端、scope、完整 inventory、全部 chunk 摘要及未覆盖清单都重新从 NAS 字节核对。布尔值不能冒充协议整数，时间必须带 `+08:00`。

历史 schema5 descriptor 可按其原始字节、公钥和签名只读验证；不改写或重新签名，
也不创建新 anchor。当前恢复目标为 schema6，恢复 schema5 默认返回
`schema_upgrade_required`，在读取 stdin 凭据、连接 NAS 或创建恢复临时树之前停止。
管理员明确选择迁移时，在上述 `recover` 命令追加 `--upgrade-schema`；仅此选项允许
沿原 `DeliveryStore.restore(upgrade_schema=True)` 迁移。它先在新私有树内写入持久
恢复保护，再保存 `0600` 的 schema5 升级前数据库副本并迁移到 schema6。该副本已含
恢复保护；外部原 descriptor、签名及 NAS 密文对象保持原样。恢复数据库重启后仍只读，
选项不授予执行、通知或上传权限；未知版本仍拒绝，不能借迁移修补损坏的 schema6。

恢复摘要增加 `source_schema_version`、`schema_version`、`schema_upgraded` 和
`schema_backup`（目标树内的升级前副本文件名；无需迁移时为 null）。CLI 仅返回稳定
错误码，例如 `schema_upgrade_required` 或 `schema_group_invalid`；失败不发布目标，
不把缺少恢复数据或错误定义当作可以重新上传、补签或自动修复的理由。

全部验证成功才将同一父目录内的临时树 rename 至 destination，父目录 fsync；这是无并发管理写者前提下的发布，不宣称内核级 no-replace 原语。结果包括：

```text
new-tree/delivery.sqlite3             恢复保护持久存在
new-tree/archives/<archive_ref>/      全部校验后的原始文件及空目录
new-tree/recovery-summary.json        摘要、事件数、覆盖和明确只读状态
new-tree/delivery.sqlite3.v5-*.sqlite3 显式迁移时的私有升级前副本，保留恢复保护
```

恢复不连接原数据库或 writer journal，不取得写对象能力，不修改 NAS 对象，也不启用执行器。成功后应逐项核对恢复摘要和业务缺口，后续接管唯一写者、重新部署、发消息、恢复业务卷均是另外的操作与验收。

任何缺对象、错误 key/anchor、签名/绑定/时区/清单漂移、超时或权限异常都失败关闭，保留原数据，不发布不完整目标。正常异常会清理临时明文；进程被强制终止时可能留下私有临时目录，由管理员确认后清理。不要重试写入或自行选择“看起来最新”的 descriptor。签名不证明新鲜度，灾前应把所选 checkpoint_ref 与 descriptor 摘要纳入独立受信登记，恢复时核对允许的检查点。

## 本地验证入口

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=.:tests python3 -m unittest \
  test_codex_delivery_recovery_kit \
  test_codex_delivery_object_archive \
  test_codex_delivery_fnos_file_replica \
  test_codex_delivery_store
```

恢复包测试使用真实 SQLite/AES、磁盘 NAS fixture 和全新 OS 子进程，删除整个 primary 后验证只读恢复；wire transport 由 fixture 替换。它们不证明实际 HA→NAS 网络、真实 Broker 长期密钥恢复、NAS 断电或生产灾难恢复。正式验收必须另外从长期外部 descriptor、公钥和独立密钥启动全新进程，并记录读取来源、精确检查点及覆盖范围。
