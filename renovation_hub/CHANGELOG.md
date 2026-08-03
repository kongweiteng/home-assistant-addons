# 更新记录

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
