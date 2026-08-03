# Renovation Ledger 使用说明

## 配置

| 配置项 | 说明 |
| --- | --- |
| `api_token` | Controller 调用内部 API 的独立 Token，至少 32 个字符 |
| `writer_mode` | 默认 `read_only`；`primary_writer` 只能在数据内已完成独立切换后继续运行 |
| `max_request_bytes` | 单个内部 JSON 请求大小上限；默认 32 MiB，可承载 20 MiB 附件的 Base64 包装 |
| `max_attachment_bytes` | 单个附件明文大小上限 |
| `portable_history_limit` | `/share` 历史便携包保留数量 |

## 数据位置

- `/data/ledger.sqlite3`：主 SQLite 数据库。
- `/data/attachments`：内容寻址附件。
- `/data/charts`：短期中文统计图。
- `/data/import`：只读导入候选。
- `/data/shadow`：影子导入数据库与报告。
- `/share/private/renovation-bookkeeping/current/kanhuwan-renovation-ledger.zip`：稳定便携包。

## Writer 状态

正常迁移顺序为：

`uninitialized -> read_only -> shadow_validated -> cutover_ready -> primary_writer`

发现完整性、空间、权限或导出异常时进入 `suspended`。任何时刻都不得与 Hermes 同时写正式账本。

## 内部 API

所有内部请求使用：

```text
Authorization: Bearer <独立 Token>
Content-Type: application/json
```

工具入口为 `POST /internal/v1/tools/call`，请求包含 `name`、`arguments` 和脱敏 `actor_hash`。工具名以 `contracts/renovation_ledger_tools_v1.json` 为准。

## 恢复

1. 停止新写入并记录最后流水和便携包 SHA-256。
2. 优先从 HAOS 冷备份恢复 `/data`。
3. 如需从便携包重建，先在 `read_only` 影子目录校验和导入。
4. 对比付款、退款、分类、标签、附件、审计和净额后，另行批准 writer 切换。

切换后若已经产生新真实账目，禁止使用旧数据库直接覆盖，必须先导出增量并执行无损反向迁移。
