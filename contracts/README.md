# Codex、微信与装修账本共享契约

本目录保存 Codex Controller、Weixin Gateway、Renovation Hub 和 HA Operations Broker 之间的公开、脱敏、版本化契约。

- `codex_weixin_job_v1.schema.json`：微信消息进入 Controller 的作业格式。
- `unified_notification_v1.schema.json`：HA 到微信主动通知的 MQTT request、result 与 status 消息格式。
- `renovation_ledger_tools_v1.json`：Renovation Hub 保留给 Codex 的确定性 Ledger v1 兼容工具清单。
- `renovation_ledger_tools_v2.json`：Renovation Hub 正式 writer 使用的 v2 分组标签、稳定 portable ID 与写入幂等工具清单。
- `renovation_hub_tools_v1.json`：项目、阶段、空间、施工事件、时间线和驾驶舱工具清单。
- `ha_operations_receipt_v1.schema.json`：HA Operations Broker 执行或验证结果的收据格式。

规则：

1. `message_id` 是微信消息到 Codex Turn、账本写入和 HA 操作的幂等根。
2. 原始微信用户 ID、Token、账目正文、附件内容和内部 bearer 不得进入契约 fixture。
3. 删除字段、收紧类型或改变错误语义必须升级契约版本并重新完成开发前评审。
4. JSON Schema 只描述跨组件边界；每个服务仍需在运行时执行大小、权限、路径和业务不变量校验。
