# 更新记录

## 0.1.0

- 新增单写入者 SQLite 装修账本、幂等付款/退款、修改、撤销、标签和审计。
- 新增附件内容寻址存储、中文 PNG 汇总图和 Ingress 状态页。
- 新增 `kanhuwan-renovation-ledger@1` 原子便携导出、独立校验和只读影子导入。
- 默认保持 `read_only`，未授权任何正式账本迁移或 writer 切换。
