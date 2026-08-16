# HA Manager Executor

HA Manager Executor 是 Home Assistant Supervisor `manager` 权限域的独立执行器骨架。`0.1.1` 只提供经过内部 bearer 认证的 `restart_addon` 只读 shadow 预检：读取精确白名单 Add-on 的状态、兼容 HAOS 省略 `installed` 字段的响应、生成绑定摘要并验证基线，不调用 restart/start/stop/configure/install/update 等任何写端点。

默认配置没有目标白名单，也没有宿主端口、Ingress、Core API、backup/admin、host network、privileged、Docker socket 或主机目录权限。

当前版本不能执行真实 Home Assistant 操作。Auth Broker、Passkey 收据、持久 lease 和后续正式执行迁移仍由独立准备包控制。配置、接口、验证和回滚边界见 [DOCS.md](DOCS.md)。
