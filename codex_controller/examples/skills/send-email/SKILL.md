---
name: send-email
description: 通过微信助手发送或核对电子邮件；用户明确要求发邮件、发送邮件、查发信结果时使用。写邮件草稿、改写或翻译但未要求发送时不调用发信工具。
---

使用 `ext_mail_send` 发送文本正文和真正的文件附件，使用 `ext_mail_recent_status` 核对最近发送记录。

发送前从当前完整对话中确定收件人、主题、正文与所需附件。用户要求“整理账单并发邮件”时可据任务组织主题和简洁正文，无需让用户再次撰写。不能确定收件地址或文件时只询问缺失内容；模糊的联系人必须先取得明确邮箱地址。用户已经明确说“发送/发邮件”且内容可确定时直接调用，不重复确认。草拟、润色、讨论或翻译邮件不构成发送授权。

收件人只能来自管理员配置的允许列表。不要让用户指定 SMTP 主机、端口、发件账号、授权码、内部地址或文件路径。附件必须作为 MIME 附件发送，不能把路径或链接写进正文冒充附件。当前支持 XLSX、PDF、PNG/JPEG/WebP、ZIP、DOCX、PPTX、CSV、TXT；每个不超过 5 MiB、总计不超过 10 MiB、最多 10 个，不能发送可执行文件。

`attachments` 的每一项包含 `source`、`reference` 和可选 `format`：

- 当前会话已生成文件：`source=artifact`，`reference` 使用实际工具返回的 `artifact_id`，`format=original`。可引用同一会话尚未过期的生成文件，不能猜测其他会话引用。
- 当前消息或本轮追加消息上传的文件：`source=upload`，`reference` 使用 `attachment_ref`，`format=original`。旧消息文件无法定位时请用户重新附上。
- 用户要求把装修账本整理为 Excel：先调用 `ledger_export` 取得完整账本快照；再发送 `source=artifact`、对应的 `artifact_id`、`format=ledger_xlsx`。系统将快照转换成真正的 `.xlsx`，包括汇总、全部明细、日期、类型、数值金额、商家、说明、分类、标签、退款关联和作废状态。不要用有限分页数据或纯文本简表冒充全部账本；不截断记录。

文件内容与摘要由 Controller 校验并传给 Provider；模型不读取 Base64，也不提供 URL 或文件路径。附件准备失败时邮件不会发送，应说明具体限制。`sent` 的 `attachments` 返回已接受附件的文件名、类型、大小和摘要；只有该列表包含请求的文件才能声明附件已发送。

工具返回 `sent` 时说明 SMTP 已接受；`verification=sent_folder_confirmed` 才能说明 Sent 文件夹已核对。返回 `failed` 时报告稳定错误码并允许用户修正明确问题。返回 `unknown` 时先调用 `ext_mail_recent_status`；仍为 unknown 时请用户人工查看 QQ 邮箱“已发送”，不要自动重发。只有用户确认未发送并再次明确要求发送，才可创建新的发送请求。

回复中保留收件人、主题、`send_id` 和结果，不复述授权码、内部服务信息或完整正文。
