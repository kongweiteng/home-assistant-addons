# AGENT.md

本文件适用于 `infrastructure/home-assistant-addons/`。本目录是独立 Git 仓库，并继承根目录和 `infrastructure/AGENT.md` 的安全、授权与隐私规则。

## 1. 仓库定位

这是 `kongweiteng/home-assistant-addons` 的本地工作副本，是自维护 Home Assistant Add-on 的源码和公开发布边界。

- 官方或成熟社区 Add-on 继续使用原始仓库，不为统一形式而复制维护。
- 只有需要自行封装、修补、收敛权限或控制发布节奏的应用才进入本仓库。
- 本仓库可能公开，任何提交内容都按公开信息处理。

## 2. 开始修改前

1. 阅读本文件、仓库 `README.md` 和目标 Add-on 的 README、DOCS、CHANGELOG、`config.yaml`、`build.yaml`。
2. 检查当前分支、工作区状态、本地与远端关系，保护用户已有提交和修改。
3. 不自动 pull、rebase、merge、reset、commit、push 或发布；这些动作需要用户明确要求。
4. 明确本次修改是本地代码验证、仓库发布，还是正式 HAOS 升级；三者必须分别授权和汇报。

## 3. 公开仓库隐私边界

不得提交：

- 家庭内网地址、公网 IP、个人域名、邮箱、用户名和设备清单。
- HA token、MQTT 密码、Cookie、OAuth token、私钥、证书密钥、WireGuard 配置或二维码。
- 运行实例 options、真实 Add-on 数据目录、日志、备份、浏览器状态和私有补丁产物。
- 只适用于当前家庭环境且未脱敏的测试数据。

测试中使用文档保留地址、占位域名、临时 fixture 和示例凭据。日志和错误信息必须避免回显 options、环境变量和请求头中的秘密。

## 4. Add-on 设计规则

每个 Add-on 应具备：

- 稳定且不冲突的 slug、名称和版本。
- 明确的上游来源、许可证、固定版本和必要校验值。
- 正确的 `config.yaml` schema、支持架构、端口、Ingress、持久化和权限声明。
- 最小权限运行；不无理由使用 host network、privileged、Docker socket、主机设备或宽泛目录挂载。
- 明确的启动失败、健康检查、数据目录、升级和回滚语义。
- README、DOCS 和 CHANGELOG 与实际行为一致。

应用需要代理时，代理选项默认关闭，并正确设置本地地址、Supervisor、Home Assistant 和 MQTT 的 `NO_PROXY`。不得让本地核心功能依赖外部代理。

## 5. 版本和发布

- 行为、依赖、上游版本或用户可见配置发生变化时，按项目约定更新版本和 CHANGELOG。
- 引用上游二进制或源码包时固定版本和 SHA-256；不得长期依赖浮动 `latest`。
- 发布前检查仓库根 `repository.yaml`、Add-on metadata、支持架构和文档链接。
- GitHub `main` 是发布源，但不得因为代码已通过测试就自动 push 或触发正式升级。
- 正式 HAOS 保持自动更新关闭；发布新版本与升级运行实例是两个独立动作。

## 6. 验证

根据修改范围运行：

- `git diff --check`
- Shell 语法检查和 Python 语法检查。
- `python3 -m unittest discover -s tests -p 'test_*.py'`
- 目标 Add-on 的配置、构建和多架构检查。
- Ingress、持久化、升级、重启和回滚验证。

无法进行目标架构构建或真实 HAOS 验证时，必须明确说明只完成了源码或本地测试，不能声称 Add-on 已在正式实例验收。

## 7. 部署边界

以下动作需要单独授权：

- commit、push、创建 release 或修改 GitHub 发布状态。
- 在 HA 中刷新自定义仓库、安装或升级 Add-on。
- 修改正式 Add-on options、停止、重启、卸载或恢复数据。

部署前需要 HA 备份和回滚版本；部署后验证启动日志、Ingress、数据持久化、权限、网络暴露和旧版本回退路径。
