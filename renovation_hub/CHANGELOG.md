# 更新记录

## 0.2.6

- 将 `ledger_add_payment` 的模型可见 manifest/dispatch 契约收紧为 canonical v2，必填金额分、日期和九维 `grouped_tags`，不再暴露 legacy 分类、标签或格式版本字段。
- registry handler 在写事务前拒绝未知/legacy 字段、缺失 `grouped_tags` 和未知标签维度，并强制 `ledger_format_version=2`；`LedgerStore.add_payment` 的内部 v1 兼容路径继续保留。
- 分组标签 Schema 与 portable v2 的九个固定维度、24 项总上限和 40 字符单项上限共用同一常量，并增加失败零流水/零审计回归。
- 本版本只修改本地候选和测试；不部署、不重启、不写正式账本，也不改变 writer generation 或 active lease。

## 0.2.5

- 新增 Renovation Hub 单一业务工具 registry，由同一事实源生成受认证 MCP manifest 与 `/internal/v1/tools/call` dispatch，当前覆盖 30 个公开账本、项目、阶段、空间、时间线、搜索和媒体动作。
- 新增 manifest version/service/scope/revision/digest、完整 JSON Schema、风险与 transport 元数据；固定命名空间并拒绝任意 URL、SQL、文件、cutover、writer、恢复和清理等内部能力。
- 新增统一 `renovation_search`、`renovation_media_list` 和 `renovation_media_show`，按项目、阶段、空间、时间、类型和关键词查询，同时隐藏内部存储名、来源摘要和私有路径。
- 新增公开业务路由覆盖审计、manifest/digest/Schema/transport 反例和 registry/dispatch 单一事实源回归；不改变 writer generation、active lease、正式账本或媒体原件。

## 0.2.4

- 修复 HA 局域网普通 HTTP Ingress 中 `crypto.randomUUID()` 不可用，导致项目、空间、阶段、时间线和账目保存请求在发送前失败的问题。
- 安全上下文继续使用原生 UUID；非安全上下文改用 `crypto.getRandomValues()` 生成 RFC 4122 UUID v4 幂等键，不使用 `Math.random()`，不改变服务端幂等、CSRF、writer 或账本契约。
- 新增前端 API 回归测试，覆盖原生与 HTTP 回退路径、UUID version/variant 位和 `Idempotency-Key` 请求头。

## 0.2.3

- 修复 canonical v2 付款因 `main_category` 按契约为空而在资金账目页全部显示成“退款”的问题。
- 展示分类按“主题 → 专业 → v1 主分类 → 未分类”确定；付款、订金、退款改为独立交易类型徽标。
- 将账目备注作为主要用途显示并支持两行截断与全文提示，商家保留为次要信息；退款和撤销对话框复用同一分类逻辑。
- 扩充前端回归测试与纯合成预览数据，覆盖 v1/v2、付款、订金、退款、用途和商家显示；不修改正式账本数据或写入契约。

## 0.2.2

- 同步 Ingress 状态与内部 HTTP Server 的运行时版本为 `0.2.2`，确保 Supervisor、Controller 和迁移门禁核对的是实际运行候选而非旧版本字符串。
- 保留 `0.2.1` 的真实规模流水数值排序修复，不改变账本、manifest、writer generation 或 active lease 数据语义。

## 0.2.1

- 修复正式播种前 Hub 派生 `transaction_context` 使用文本流水 ID 排序的问题；真实账本超过 9 条流水时不再因 `1,10,100...` 与数值顺序不一致而误报 `invariant_mismatch`。
- 新增 12 条流水的数值排序回归测试；不改变 portable v1/v2、manifest、writer generation、active lease 或正式写入语义。

## 0.2.0

- 新增 v2 原生主库字段：账本格式版本、稳定流水/附件 portable ID，以及九维分组标签的 dimension/value/created_at。
- 新增持久 cutover manifest、writer generation 和单 active lease；正式写入同时校验三者，旧裸 writer-mode API 不再允许推进状态。
- 新增 verified staging、来源冻结证据、空主库播种、默认历史项目/context、cutover ready、动态确认激活和幂等恢复。
- Add-on 新增独立 `cutover_token`；未配置或错误时切换管理 API fail closed。
- options=`read_only`/`suspended` 可紧急撤销 lease 并停写；options=`primary_writer` 仅在持久状态完整时恢复。
- 播种在数据库提交与附件原子替换之间中断时，可利用既有 rollback baseline 和 staging 自动完成确认性恢复；无法证明一致时保持停写并返回 `seed_recovery_required`。
- 紧急停写只撤销 lease 和运行态 writer，不破坏 manifest 的已验证阶段；恢复必须重新提交动态确认串并重建唯一 lease。
- 每次紧急停写会原子轮换 metadata 与 manifest 的 writer generation，旧动态确认串在同进程或重启后均失效。
- `options=primary_writer` 遇到暂停态或无效 lease 时不再让服务在 Web API 创建前退出；服务保持 degraded/recovery-required 管理面在线，业务写继续失败关闭。
- Legacy 附件改为先验证 writer、幂等键和目标流水，再以恢复标记、临时文件、数据库事务和内容寻址切换落盘；失败请求不再制造孤儿文件，启动只清理有效标记覆盖的未引用文件。
- 正式播种不再复制 staging 中未受来源 manifest 覆盖的 Hub 扩展表；默认项目和 context 改为确定性重建并全量校验，额外、缺失或篡改的项目/阶段/空间/context 均拒绝。
- 增加附件零副作用、停写代际轮换、暂停态恢复管理面、Hub 派生表篡改和播种多断点中断恢复回归测试。
- v2 付款不再需要主分类，支持九个固定维度、最多 24 个且单项最长 40 字符；退款继承版本与全部标签。
- 新增 canonical v2 自验导出，保持附件、审计、退款关系、稳定 ID、摘要和不变量。
- 本版本只交付本地源码和合成测试，不代表已执行正式账本迁移、writer 切换或 Hermes 退役。

## 0.1.3

- 便携包固定读取器新增 `kanhuwan-renovation-ledger` v2 / SQLite Schema 3 支持，同时保留 v1 和早期 legacy 包兼容。
- v2 会验证九个固定维度的分组标签、退款继承、`grouped_tags`、CSV、维度汇总、附件和审计，并完整恢复到私有只读 shadow。
- v2 不迁移 Hub 主库、页面或 writer；兼容 shadow 的 `main_category` 为空占位，全部来源标签和来源版本均保留并重新校验。
- 新增合成 v2 成功、无效分组标签、重复导入和 shadow 恢复回归。

## 0.1.2

- 新增正式 `kanhuwan-renovation-ledger` v1 便携包的全不变量验证，不执行包内脚本，并逐项核对 SQLite、JSON、CSV、JSONL、manifest、退款关系、分类、标签、月份汇总、附件和审计前后值。
- 只读影子导入现在保留原始 SQLite 快照和全部附件，同时建立 Hub 兼容影子数据库，保留 legacy ID、撤销原因、标签顺序、退款关系和可审计的来源引用。
- 影子报告仅包含结构计数、校验布尔值和摘要哈希，不返回金额、商家、备注、附件正文或绝对私有路径。
- 新增恶意 ZIP、字段漂移、附件篡改、审计重排、幂等重放、缓存影子篡改和重启恢复测试。

## 0.1.1

- 兼容 HAOS Bookworm 自带的 `aiohttp 3.8.x`：在不支持 `web.AppKey` 时安全回退到应用字符串键，修复 Add-on 启动即退出。
- 新增对应兼容性回归测试；不改变数据契约、权限、Ingress、writer 模式或正式账本边界。

## 0.1.0

- 将未部署的 Renovation Ledger 候选收敛为 Renovation Hub，并保留全部 `ledger_*` v1 工具和 `kanhuwan-renovation-ledger@1` 兼容边界。
- 新增单写入者 SQLite 装修账本、幂等付款/退款、修改、撤销、标签和审计。
- 新增项目、装修阶段、空间、施工事件、统一时间线、驾驶舱统计、乐观版本和共享审计。
- 新增图片/视频流式上传、内容寻址原件、图片预览、视频探测/封面、Range 播放、关联筛选和幂等重放。
- 新增 React 19、TypeScript、Vite 独立管理页面，覆盖总览、时间线、资金账目、图片视频、装修阶段和设置，并适配桌面与手机。
- 前端构建阶段固定 Node 22 Alpine 多架构 OCI digest，并允许构建环境用同 digest 的公开镜像代理覆盖来源。
- 将 Vite 升级到修复安全公告的 `6.4.3`，并把 Vite 与 React 构建插件收敛为仅构建期依赖。
- 页面支持账目新增/编辑/退款/撤销、媒体上传、现场记录、项目/空间/阶段维护和筛选查询。
- 新增 CSRF 页面会话、管理员 Ingress 写入保护和三段式浏览器上传协议。
- 新增 Gateway→Controller→Hub 二进制流式媒体链路；模型和 app-server 不接触正文、bearer 或内部路径。
- 保留 Legacy 附件内容寻址存储、中文 PNG 汇总图和账本 API。
- 新增 `kanhuwan-renovation-ledger@1` 原子便携导出、独立校验和只读影子导入。
- Product Design 同视口视觉 QA 通过，桌面和手机关键交互已用合成数据验证。
- 默认保持 `read_only`，未授权任何正式账本迁移或 writer 切换。
