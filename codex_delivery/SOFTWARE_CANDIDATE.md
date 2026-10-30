# 软件核心源码候选与启用边界

此包可独立构建默认关闭的元数据服务镜像，并提供通用 Work、代码/浏览器/预览运行时库、
仓库归档/验证、软件绑定及构建/发布的受信启动候选。源码存在不表示这些能力已登记、
获得权限或完成真实部署；默认配置不执行任务，不取得 Git、Docker 或 HA 控制权限。

固定镜像 policy、合格运行资产、独立副本及私有 root 注册缺一不可。软件绑定必须复用
同一 Store/Archive/Coordinator/worker，不从模型接受 URL、路径、命令、运行后端或权限。
未知外部结果保留唯一 claim，只读对账；启动/发布/Git 写入不自动重放。源协议 v1/v2
与 Git 机器身份分别按各自文档启用，不自动回退或读取本机个人凭据。

生产依赖通过固定 HTTP 契约连接既有 Controller、Registry 和发布域；本次独立源码包
不包含这些其他域的新增执行功能。`ha_release.verify_backup()` 的受信 Supervisor
适配器仍延迟使用 Broker 的 backup evidence 契约，不能将它作为本镜像内的直接 HA
控制接口启用。Registry 的公开 metadata 继续关闭 host network，其新权限须单独批准。

完整开发集成快照在本地完成 702 项测试，24 项因环境条件跳过；该结果包含开发工作区
的其他域候选和 fixture，不是独立源码包或真实部署的全部验收。独立包另运行其可独立
导入的测试；依赖尚未发布 Controller/Broker/Manager/Backup/Weixin 功能的集成 fixture
留在开发工作区，不能通过夹带其他域的未完成实现来凑成独立发布。

独立发布工作区的 Delivery 组完成 356 项测试、跳过 3 项，其余通过；Registry 30 项
通过。它们是独立包范围的验证，不与完整开发组累计为新的总数。

默认 amd64/arm64 镜像及合成软件 amd64 镜像已实际构建，固定 Debian/TARGETARCH
不消费 Supervisor 的 BUILD_FROM 回退。真实容器核对源码摘要、Git/crypto、默认关闭、
不创建 state；policy、资产及凭据仍未取得生产资格。APT 版本未锁，不能称字节可复现。

仍需分别完成真实独立副本恢复、机器身份与权限、运行/源码及资源资格、用户 Passkey、
独立 canary 精确升级/失败/回滚和真实客户端交付。未完成这些门禁前，此版本只能按其
明确的默认关闭候选范围评审，不能宣称完整工作执行或自动部署已经上线。

相关契约：[生产接线](REPOSITORY_BOOT.md)、[软件入口](SOFTWARE_MANAGEMENT.md)、
[封存来源](SEALED_REPOSITORY_SOURCES.md)、[Git 身份](GIT_TRANSPORT.md)、
[Registry 构建](REGISTRY_BUILD.md)、[恢复](RECOVERY_KIT.md)。
