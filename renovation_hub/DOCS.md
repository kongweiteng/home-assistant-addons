# Renovation Hub 使用说明

## 配置

| 配置项 | 说明 |
| --- | --- |
| `api_token` | Controller 调用内部 API 的独立 Token，至少 32 个字符 |
| `writer_mode` | 默认 `read_only`；`primary_writer` 只能在数据内已完成独立切换后继续运行 |
| `max_request_bytes` | 单个页面或内部请求的总大小上限 |
| `max_attachment_bytes` | 旧 Ledger v1 Base64 附件的单文件明文上限 |
| `max_media_bytes` | 新图片/视频流式上传的单文件上限，默认 1 GiB |
| `portable_history_limit` | `/share` 历史便携包保留数量 |

Add-on 使用管理员 Ingress，不映射 `8101/tcp` 到宿主机，也不申请 Home Assistant、Supervisor、MQTT、Docker、host network 或设备权限。

## 页面

- **总览**：预算、净支出、当前阶段、媒体统计、施工进度、空间影像、近期动态和资金构成。
- **时间线**：按发生时间查看施工、验收、决策、里程碑和普通记录；可新增和编辑记录。
- **资金账目**：查看付款与退款明细，可新增、纠正、退款和撤销，并保留审计与版本冲突保护。
- **图片视频**：按阶段、空间、类型、时间和关键词筛选，查看图片或播放视频，并上传多个现场文件。
- **装修阶段**：维护阶段名称、状态、颜色、计划/实际时间；同一项目最多一个进行中阶段。
- **设置**：维护项目与空间，查看 writer、导出和媒体存储状态。

页面写请求必须同时满足管理员 Ingress、CSRF、`Idempotency-Key`、writer 状态和对象版本要求。旧版本写入返回 `409 version_conflict`，不会覆盖较新数据。

## 数据位置

- `/data/ledger.sqlite3`：账本、项目、阶段、空间、时间线、媒体元数据、幂等和审计数据库。
- `/data/attachments`：Legacy Ledger v1 内容寻址附件。
- `/data/charts`：短期中文统计图。
- `/data/import`、`/data/shadow`：便携包只读导入候选与影子数据库。
- `/data/media-previews`：图片缩略图和视频封面。
- `/data/media-staging`：尚未完成校验的浏览器或 Controller 流式上传文件。
- `/media/renovation-hub/originals`：图片和视频原件，只使用服务端生成的内容寻址文件名。
- `/share/private/renovation-bookkeeping/current/kanhuwan-renovation-ledger.zip`：Hermes 当前稳定便携包；Hub 只读入口支持格式 v1/v2，不包含大视频。

原始媒体不写入 SQLite 或便携账本 ZIP。正式部署前必须单独确认 `/media` 后端、容量、备份与恢复方式。

## 便携包只读影子导入

正式 Hermes 包使用 `format_id=kanhuwan-renovation-ledger`、`currency=CNY` 和 `amount_unit=integer_cents`。Hub `0.1.3` 支持 `format_version=1` 与 `2`，导入时不会运行 ZIP 内的 `verify.py`，而是使用自身固定实现完成以下检查：

- ZIP 路径、重复项、符号链接、文件数量、解压大小和压缩率限制。
- manifest 文件全集、每个普通文件的大小和 SHA-256。
- SQLite `integrity_check`、外键、流水、退款关系、有效/撤销状态、订金和退款上限。
- `ledger.json`、三个 CSV、`audit_log.jsonl` 与 SQLite 的逐字段一致性。
- 分类、标签、月份汇总、附件元数据/文件哈希、审计数量/顺序/前后值。
- v2 的 SQLite Schema 3、无主分类约束、九个固定标签维度、标签数量/长度/顺序、`grouped_tags`、`grouped_tags_json` 和 `tags + dimensions` 汇总。

成功导入后，`/data/shadow/<来源 SHA-256>/` 保存只读来源快照、全部附件、Hub 兼容 `ledger.sqlite3` 和 `report.json`。报告只包含结构计数、校验布尔值和摘要哈希，不包含金额、商家、备注、附件正文或绝对私有路径。重复导入会重新校验来源快照、规范化数据库和附件，不会仅返回旧报告。

v2 的 Hub 兼容 shadow 数据库会把 `main_category` 保留为空占位，并完整保存全部分组标签。该数据库只用于只读比对，不能作为主库迁移或页面 writer 的输入；正式迁移 v2 需要后续独立数据模型设计和授权。

## Writer 状态

正常迁移顺序为：

`uninitialized -> read_only -> shadow_validated -> cutover_ready -> primary_writer`

发现完整性、空间、权限或导出异常时进入 `suspended`。任何时刻都不得与 Hermes 同时写正式账本。页面存在写按钮不代表正式 writer 已切换；`read_only` 时写入口会被禁用或显式拒绝。

## 页面 API

读取入口包括：

- `GET /api/v1/session`
- `GET /api/v1/projects`
- `GET /api/v1/dashboard`
- `GET /api/v1/stages`
- `GET /api/v1/areas`
- `GET /api/v1/timeline`
- `GET /api/v1/ledger/transactions`
- `GET /api/v1/media`
- `GET /api/v1/search`

项目、阶段、空间、事件和账目通过 `POST`/`PATCH` 页面 API 写入。退款和撤销使用独立动作端点。

浏览器媒体上传使用：

1. `POST /api/v1/uploads` 创建上传会话并提交文件名、MIME、大小、SHA-256、拍摄时间和业务关联。
2. `PUT /api/v1/uploads/{id}/content` 以原始二进制流上传正文。
3. `POST /api/v1/uploads/{id}/complete` 完成大小、哈希、图片或视频探测、封面生成、原子归档和数据库提交。

媒体内容通过 `GET /api/v1/media/{id}/content` 读取；aiohttp 文件响应支持 Range。预览通过 `GET /api/v1/media/{id}/preview` 读取。

## 内部 API 与 Codex 工具

所有内部请求使用：

```text
Authorization: Bearer <独立 Token>
Content-Type: application/json
```

兼容工具入口为 `POST /internal/v1/tools/call`。Ledger v1 工具名继续以 `contracts/renovation_ledger_tools_v1.json` 为准；项目、阶段、空间、事件、驾驶舱和媒体工具以 `contracts/renovation_hub_tools_v1.json` 为准。

`renovation_media_ingest` 只允许模型提交一次性 `attachment_ref` 和结构化元数据。Controller 主进程先检查 Hub 幂等结果，再从 Gateway 流式读取正文并转发到 `/internal/v1/media/ingest`；不会生成 Base64 JSON，也不会把 Gateway bearer、Hub bearer、内部路径或媒体正文交给模型。

## 支持媒体

- 图片：JPEG、PNG、WebP、HEIC/HEIF。
- 视频：MP4、MOV、WebM。
- 图片使用 Pillow 校验并生成预览；视频使用 FFprobe 获取尺寸和时长，使用 FFmpeg 生成封面。
- 文件大小、声明 MIME、实际探测、SHA-256、业务关联和固定存储边界全部通过后才进入 `ready`。
- 上传失败进入明确失败状态并清理 staging，不创建零字节或伪成功媒体。

## 本地合成预览

前端构建完成后，可使用纯合成账目和媒体启动预览：

```bash
PYTHONPATH=. PYTHONDONTWRITEBYTECODE=1 \
python3 scripts/fixture_preview.py --static-dir frontend/dist --port 8101
```

预览会在临时目录创建 disposable 数据，并把 writer 切到仅供本地交互验证的 `primary_writer`。它不会读取正式 Hermes 或 HAOS 数据。

## 备份、恢复与降级

1. 停止新写入并记录 writer mode、数据库状态、最后流水、媒体数量和便携包 SHA-256。
2. 使用 HAOS 冷备份恢复 `/data`；媒体原件还必须从已验证的 `/media` 独立备份恢复。
3. 如需从便携包重建账本，先在 `read_only` 影子目录执行全不变量校验和导入。
4. 对比付款、退款、订金、有效/撤销状态、分类、标签、月份、附件、审计、项目/阶段/空间/事件和媒体 manifest 后，另行批准 writer 切换。
5. 切换后若已经产生新真实账目或媒体，禁止使用旧数据库直接覆盖；必须先导出增量并执行无损反向迁移。

本地源码、合成测试或双架构构建通过不能替代 HAOS Ingress、大文件、存储容量、重启恢复和真实微信链路验收。
