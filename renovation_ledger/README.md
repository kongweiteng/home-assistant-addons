# Renovation Ledger 装修账本

Renovation Ledger 是一个独立、确定性、单写入者的 Home Assistant Add-on，用于接管 Hermes 当前的装修记账能力。

它负责付款、订金、退款、主分类、多标签、附件、修改、撤销、审计、查询、汇总、中文 PNG 图表，以及 `kanhuwan-renovation-ledger@1` 便携包的导入、导出和校验。Codex 只能调用结构化工具，不能直接执行 SQL 或访问账本目录。

## 当前阶段

- 版本：`0.1.0`，实验候选。
- 默认 `writer_mode=read_only`。
- 尚未安装到正式 HAOS，也没有读取或迁移正式账本。
- Hermes 在正式切换验收前继续作为唯一正式 writer。

## 安全边界

- 不申请 Home Assistant、Supervisor、MQTT、Docker、host network 或设备权限。
- 内部 API 必须使用独立的至少 32 字符 bearer。
- Ingress 页面默认不展示金额、商家、备注、附件或完整审计正文。
- `/share` 只使用 `private/renovation-bookkeeping` 固定子目录。
- 同一幂等键同一请求返回原结果；同键不同请求拒绝执行。
- `primary_writer` 不能仅靠修改 options 激活，必须经过独立切换流程。

## 本地验证

```bash
PYTHONPATH=renovation_ledger PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_renovation_ledger
```

完整配置、接口、导入和恢复说明见 [DOCS.md](DOCS.md)。
