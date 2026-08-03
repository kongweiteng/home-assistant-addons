# 更新记录

## 0.1.0

- 新增 Weixin Gateway 一次性附件到 Renovation Hub 兼容账本模块的受限桥接，模型和 app-server 不接触 bearer 或内部路径。
- 新增 `renovation_*` 项目、阶段、空间、时间线、驾驶舱和媒体工具代理；图片/视频正文从 Gateway 流式转发到 Hub，不再进入 Base64 JSON。
- Codex 运行镜像改为按 SHA-512 校验官方 npm 平台包并只保留原生二进制，移除运行时 Node/npm 依赖。
- 固定官方 `@openai/codex@0.146.0`，使用稳定 stdio JSONL app-server。
- 新增仅允许 `chatgptDeviceCode` 且要求 `authMode=chatgpt` 的正式认证门禁。
- 新增 SQLite 持久队列、多 Thread、全局单活动 Turn 和 `recovery_required` 恢复语义。
- 新增无秘密 Unix Socket MCP 代理和中文 Ingress 状态页。
- 默认关闭正式任务入口，未授权任何 HAOS 安装、账号登录或 Hermes 切换。
