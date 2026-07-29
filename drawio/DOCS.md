# DrawIO

## 使用方法

1. 安装并启动 DrawIO。
2. 打开应用信息页，点击 **打开 Web UI**。
3. 选择 **Create New Diagram** 新建图纸，或选择 **Open Existing Diagram** 打开已有 `.drawio` 文件。
4. 编辑完成后使用 DrawIO 的保存或下载功能，把重要图纸保存为 `.drawio` 文件。

## 数据保存提醒

DrawIO 是浏览器绘图界面，不是服务端图纸仓库：

- 浏览器本地存储不等于可靠备份。
- Home Assistant 备份不会自动包含仅保存在浏览器或电脑下载目录中的图纸。
- 重要图纸应另存为 `.drawio` 文件，并复制到 fnOS/NAS 或其他具有备份策略的位置。

## 网络说明

应用只通过 Home Assistant Ingress 访问，没有映射独立的宿主机端口，也不需要额外的 DrawIO 用户名或密码。远程使用时仍应通过现有的 Home Assistant 安全访问入口，不要直接把容器暴露到公网。
