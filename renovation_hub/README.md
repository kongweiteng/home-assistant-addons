# Renovation Hub · 筑记装修档案

Renovation Hub 是一个独立、确定性、单写入者的 Home Assistant Add-on，用于逐步接管 Hermes 当前的装修记账能力，并扩展项目、装修阶段、空间、施工时间线和图片视频档案。

它保留付款、订金、退款、主分类、多标签、附件、修改、撤销、审计、查询、汇总、中文 PNG 图表和 `kanhuwan-renovation-ledger@1` 便携包语义，同时提供完整的桌面与手机管理页面。Codex 只能调用结构化工具，不能直接执行 SQL、访问数据库目录或取得媒体原件路径。

## 主要能力

- 六个独立应用页面：总览、时间线、资金账目、图片视频、装修阶段和设置。
- 页面可新增、编辑、退款和撤销账目，维护项目/空间/阶段，创建现场记录并上传图片或视频。
- 图片和视频按项目、时间、阶段、空间和类型查询；视频支持封面、时长探测和 Range 播放。
- 浏览器上传使用创建会话、流式正文、完成校验三段式协议；前端分块计算 SHA-256，不把整文件读入内存。
- Weixin Gateway 的一次性媒体引用由 Codex Controller 主进程流式转发，媒体正文、内部 bearer 和路径不进入模型或 app-server。
- 页面与 Codex 工具复用同一业务层、幂等键、乐观版本、单 writer 和审计规则。
- React 19、TypeScript 与 Vite 前端在镜像构建阶段编译，运行镜像只提供静态资源和 Python 服务。

## 当前阶段

- 版本：`0.1.1`，实验候选。
- 默认 `writer_mode=read_only`。
- P1～P6 本地源码、合成验证、完整前端门禁、恢复测试和 amd64/aarch64 镜像构建均已完成。
- 维护者 HAOS 已完成本地 Store 只读影子安装、Ingress、权限、真实未登录 Controller 路由和重启持久化验证；未创建 GitHub Release。
- 影子实例保持空库，没有读取或迁移正式账本、媒体、微信会话或凭据，也没有启用 Controller intake 或 Gateway poller。
- Hermes 在正式切换验收前继续作为唯一正式 writer。

## 安全边界

- 不申请 Home Assistant、Supervisor、MQTT、Docker、host network 或设备权限。
- Ingress 仅管理员可见，不映射宿主端口；内部 API 必须使用独立的至少 32 字符 bearer。
- 金额、商家、备注、账本正文和媒体可在管理员页面中查看，但不会写入 Git、MQTT、普通 HA 实体或普通日志。
- 原始媒体固定写入 `/media/renovation-hub/originals`；SQLite 只保存元数据、哈希和业务关联。
- `/share` 只使用 `private/renovation-bookkeeping` 固定子目录；大视频不进入账本便携 ZIP。
- 同一幂等键同一请求返回原结果；同键不同请求拒绝执行。
- `primary_writer` 不能仅靠修改 options 激活，必须经过独立切换流程。

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
  tests.test_codex_controller \
  tests.test_codex_hermes_contracts \
  tests.test_codex_gateway_ledger_integration
```

完整配置、接口、数据布局、恢复和本地预览说明见 [DOCS.md](DOCS.md)。视觉实现与融合源稿的验收证据见 [design-qa.md](design-qa.md)。
