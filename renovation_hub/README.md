# Renovation Hub · 筑记装修档案

Renovation Hub 是一个独立、确定性、单写入者的 Home Assistant Add-on，用于逐步接管 Hermes 当前的装修记账能力，并扩展项目、装修阶段、空间、施工时间线和图片视频档案。

它保留付款、订金、退款、主分类、多标签、附件、修改、撤销、审计、查询、汇总、中文 PNG 图表和 Ledger v1 主库语义，同时提供完整的桌面与手机管理页面。正式 Hermes 便携包的只读影子入口同时支持 `kanhuwan-renovation-ledger` v1/v2；Hub 早期合成包的 `kanhuwan-renovation-ledger@1` 读取兼容仍保留。Codex 只能调用结构化工具，不能直接执行 SQL、访问数据库目录或取得媒体原件路径。

## 主要能力

- 六个独立应用页面：总览、时间线、资金账目、图片视频、装修阶段和设置。
- 资金账目页将 v2 分组标签按“主题 → 专业 → v1 主分类 → 未分类”解析为展示分类，并把付款、订金、退款与用途、商家分层显示，不再把空主分类误显示成退款。
- 页面写请求在局域网普通 HTTP Ingress 下使用 Web Crypto 安全随机回退生成 UUID v4 幂等键，不依赖仅在安全上下文提供的 `crypto.randomUUID()`。
- 页面可新增、编辑、退款和撤销账目，维护项目/空间/阶段，创建现场记录并上传图片或视频。
- 图片和视频按项目、时间、阶段、空间和类型查询；视频支持封面、时长探测和 Range 播放。
- 浏览器上传使用创建会话、流式正文、完成校验三段式协议；前端分块计算 SHA-256，不把整文件读入内存。
- Weixin Gateway 的一次性媒体引用由 Codex Controller 主进程流式转发，媒体正文、内部 bearer 和路径不进入模型或 app-server。
- 页面与 Codex 工具复用同一业务层、幂等键、乐观版本、单 writer 和审计规则。
- 便携包导入不执行包内 `verify.py`，由 Hub 自身交叉核对 SQLite、JSON、CSV、JSONL、manifest、附件、退款、分类、标签、月份汇总和审计顺序。
- v2 分组式多标签现已成为正式迁移后的原生主库格式：完整保留九个维度的 `维度:值` 标签、退款继承、`grouped_tags`、维度汇总、稳定导出 ID、附件和审计；v1 本地兼容语义继续保留。
- 正式包只写入按来源 SHA-256 隔离的私有影子目录；原始快照、附件和 Hub 兼容数据库同时保留，重复导入会重新校验而不是盲信缓存报告。
- 正式切换使用持久 `cutover manifest + writer generation + active lease`，依次经过 `migration_prepared -> source_frozen -> primary_seeded -> cutover_ready -> primary_writer`；裸 bearer 不能推进状态。
- 每次紧急停写都会原子轮换 writer generation；任何停写前生成的激活确认串在重启后也不能重放。
- `options=primary_writer` 但 lease 无效或处于暂停态时，服务以 degraded/recovery-required 方式保持管理 API 在线，普通业务写继续失败关闭。
- Legacy 附件在 writer、幂等键和目标流水校验通过后才落盘，并使用可恢复标记清理提交中断产生的临时文件或孤儿。
- Hub 默认项目和账目空间上下文由 canonical 来源确定性派生并重新校验；staging 中额外、缺失或篡改的项目/阶段/空间/context 不会进入主库。
- v2 主库会输出由固定 verifier 自验的 canonical v2 便携包，重复导出的业务摘要与不变量保持一致。
- React 19、TypeScript 与 Vite 前端在镜像构建阶段编译，运行镜像只提供静态资源和 Python 服务。

## 当前阶段

- 版本：`0.2.4`，实验候选。
- 默认 `writer_mode=read_only`。
- P1～P6 本地源码与合成验证已完成；最终完整门禁和 amd64/aarch64 镜像证据以当前交付报告为准，不复用旧候选结果。
- 维护者 HAOS 已完成本地 Store 只读影子安装、Ingress、权限、真实未登录 Controller 路由和重启持久化验证；未创建 GitHub Release。
- 已具备 verified staging、来源冻结、空主库播种、播种中断确认性恢复、动态确认激活、重启租约恢复和 options 紧急停写/显式恢复的本地实现；本次源码交付没有执行正式迁移。
- 真实规模来源账本的 `transaction_context` 校验按数值流水 ID 排序，避免 `1,10,100...` 的文本顺序误报不变量漂移。
- Hermes 是否仍为唯一正式 writer 必须以当前 HAOS 运行证据为准；代码存在不代表 writer 已切换。

## 安全边界

- 不申请 Home Assistant、Supervisor、MQTT、Docker、host network 或设备权限。
- Ingress 仅管理员可见，不映射宿主端口；内部 API 必须使用独立的至少 32 字符 bearer。
- 金额、商家、备注、账本正文和媒体可在管理员页面中查看，但不会写入 Git、MQTT、普通 HA 实体或普通日志。
- 原始媒体固定写入 `/media/renovation-hub/originals`；SQLite 只保存元数据、哈希和业务关联。
- `/share` 只使用 `private/renovation-bookkeeping` 固定子目录；大视频不进入账本便携 ZIP。
- 同一幂等键同一请求返回原结果；同键不同请求拒绝执行。
- `primary_writer` 不能仅靠修改 options 激活，必须经过独立切换流程。
- 管理切换除了内部 bearer，还必须提交独立 `cutover_token`；未配置时切换 API fail closed。

## 本地验证

```bash
cd frontend
npm ci
npm run lint
npm run typecheck
npm test
npm run build

cd ../..
PYTHONPATH=renovation_hub:codex_controller:weixin_gateway \
PYTHONDONTWRITEBYTECODE=1 \
python3 -m unittest \
  tests.test_renovation_web \
  tests.test_renovation_media \
  tests.test_renovation_hub \
  tests.test_renovation_ledger \
  tests.test_renovation_portable \
  tests.test_codex_controller \
  tests.test_codex_hermes_contracts \
  tests.test_codex_gateway_ledger_integration
```

完整配置、接口、数据布局、恢复和本地预览说明见 [DOCS.md](DOCS.md)。视觉实现与融合源稿的验收证据见 [design-qa.md](design-qa.md)。
