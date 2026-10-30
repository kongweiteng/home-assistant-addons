# 变更记录

## 0.0.3 — 通用工作元数据与服务打包候选，未部署

- 生产 main/lifecycle 同 Store/Archive/Coordinator/Factory/worker 接线和默认关闭的 software_bind/software_status；元数据只确认请求，原软件线程唯一 claim 后绑定，unknown 不重放，关联 Work fencing 不阻塞普通 Work。
- 新 managed-sealed-v2 以原 confirmed 完整 bundle、真实 Linux memfd seals 和同 Archive snapshot 提供来源；完整停止 checkpoint 保留 dirty/untracked。恢复不依赖已丢失的本地主 bundle，仍验证原 export 事件和精确归档字节；本地 native arm64 资格不代替 HA。
- 固定独立 HTTPS read/publish 身份仅 root boot 加载，真实 UID/原 Git资格及闭合 ref 模板校验；环境注入不进 argv/config/guest。staging 只复制闭合非秘密 Git policy并对齐完整Repository范围；生产身份未登记。
- 固定双架构 Dockerfile FROM/TARGETARCH、OCI labels 和 APT 总期限，删除 Supervisor 不兼容的 digest build.yaml；忽略 BUILD_FROM 回退，保留 HA Debian 启动环境。

- 软件交付 staging 接受闭合非秘密 Repository/Registry policy 与精确 OCI base，核对 UID、runtime 与仓库/目标范围；仅从单一公开源码复制固定六模块 Registry 依赖并逐文件登记摘要/白名单。私有注册、token、会话和签名密钥不进入镜像。增加固定 Git 路径所需依赖，实际 Git SHA 仍由镜像 policy 核验；尚未正式资格化。
- 增加默认关闭且与挂载后端互斥的 fnOS FileReplica 启动候选：固定管理员策略/独立密钥、固定 UID 降权、精确校准对象只读核验、唯一 Store/Archive 实例；生产 file session 例外与长期恢复材料仍待验收。
- 闭合staging新增可选非秘密authority policy输入，同一parser校验、固定路径0444、source manifest/receipt精确SHA；禁止凭据字段，生成runtime ignore逐层排除并仅逐文件放行，真实COPY验收确认后加注册/会话文件被排除。相关61项回归及默认/合成staged amd64构建通过，仍未部署且缺source/release生产factory。
- 固定生产boot候选补root私有Release authority注册、镜像policy摘要/Manager DNS origin与三role白名单绑定；四独立token拒已加载或镜像禁止摘要凭据复用。Manager只读client按5秒绝对预算限制DNS/单地址connect/HTTP，2槽超时不提前释放、watchdog切断socket、拒代理/跳转/重试及重复JSON/报文头。check-config不联网不造state；source/release未接时仍503。
- 增加受信注入的同库 Release authority 与独立角色固定 HTTP 视图；任务scope实读当前状态，目标/源码观察Port缺失时关闭，不以配置或布尔值冒充构建及部署资格。
- HARelease/请求 schema2 与 typed artifact identity；运行镜像独立测量进入基线，旧记录启动跳过且不重算历史摘要；保持生产禁用。
- 增加通用工作任务元数据候选；沿用原任务、事件及离机确认契约，不建立第二份任务数据库。
- 增加最小权限 Add-on metadata、固定内部 HTTP 服务和元数据 worker 生命周期；不申请 Supervisor/HA API、宿主目录、额外 capability 或 Docker socket。
- 增加严格配置、显式 schema 升级、脱敏健康摘要及有界停止；执行开关为 true 时拒绝启动，未登记真实副本时保持 local_only。
- HA 沙箱、完整制品交付、开发/浏览器/预览、Git/构建/部署及恢复仍须分别实现和验收，本候选未安装或启用。

## 0.0.2 — 短请求与元数据队列候选，未部署

- 增加双重鉴权、封闭工具 schema、owner 查询投影和有界内部 HTTP 适配器。
- 增加本地持久入站 journal 与串行元数据 worker；入口不等待备份，独立副本确认后才返回业务 accepted。
- 稳定操作标识不依赖模型 context；中断、回执丢失和落盘后的存储异常可按原元数据键对账，不重复建任务。
- schema 升至 2；v1 数据库与旧快照仅在显式选择升级后转换，先保存私有升级前副本，事务失败回滚；恢复实例仍只读。
- 当前 Controller 不发送必需的 origin，候选拒绝旧请求；未启用任何线上扩展、开发、Git、构建或部署能力。

## 0.0.1 — 基础候选，未部署

- 增加多项目、多仓库目录、配置历史、任务归属、CAS 与幂等保存。
- 增加不可变事件、离机回执门禁、冻结的元数据快照与只读恢复。
- 增加通知未知不重放、可信来源原语和 software-delivery Skill 源文件。
- 无 Add-on 安装包；未启用开发、Git 写入、构建或部署。
