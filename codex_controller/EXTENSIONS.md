# 通用扩展与 skill

Controller 0.5.59 提供稳定的扩展入口。以后符合本契约的新能力只改插件自身、安装目录和注册配置，微信 Gateway 与 Controller 核心不变。新增微信消息格式或变更公共契约仍可能需要通道升级。

## 流程 skill

将标准 skill 目录安装到 `/data/skills/<folder>/SKILL.md`，或 Codex 持久安装目录 `/data/codex-home/skills/<folder>/SKILL.md`。另支持 `/data/codex-home/.agents/skills` 和 `/data/workspace/.agents/skills`。这些目录随 Add-on 私有数据备份，不复制 Mac 的登录数据。

每个 SKILL.md 使用 `---` 前置信息，包含 `name`（小写字母、数字、连字符，最长 64）与 `description`（最多 1000 字）。支持单行、引号和 `|`/`>` 多行描述。复杂 YAML 元数据不作为权限或依赖配置；安装者需要人工确认所需工具已登记。名称重复时拒绝重复项，不任意选择。

每轮刷新名称/描述索引，Codex 匹配后通过 `controller_skill_read` 读取完整正文或 references 下的 `.md/.txt/.json/.csv/.yaml/.yml` 文本。model 没有任意文件读取或脚本执行权；需要 Shell/CLI 的 skill 必须由独立受控 provider 提供结构化执行工具。指令、资源都由可信管理员安装，不能存放凭据。单文件 64 KiB、最多 128 个目录；索引提示预算 8000 字，完整列表可用 `controller_skill_list` 获取。

`examples/skills/report-summary` 是可安装的无外部操作示例。技能 reader 默认可供精确 owner 使用，也可在工具控制台关闭。成员与 legacy owner 无权读取。

## 执行类扩展

独立服务实现固定 `POST /internal/v1/tools/call`，由 Controller 主进程代理；这不是直接连接任意原生 MCP server。现有 MCP/CLI 可由独立服务包装成本契约，认证和执行依赖留在该服务中。

安装文件：

```text
/data/extensions/registry.json
/data/extensions/demo/manifest.json
/data/extension-secrets/demo.json
```

manifest 示例见 `examples/extensions/demo/manifest.json`。管理员将 manifest 原始 UTF-8 字节的 SHA-256 固定在注册配置中（不要在 hash 后重新格式化文件）：

```json
{
  "version": 1,
  "extensions": [
    {
      "id": "demo",
      "enabled": true,
      "base_url": "http://local-demo:8129",
      "manifest_sha256": "sha256:<替换为实际64位摘要>",
      "allow_write": false
    }
  ]
}
```

id 为最多 24 位小写字母/数字且字母开头；工具公共名称为 `ext_<id>_<name>`。只允许明确端口的内部容器 DNS 名（单标签），禁止 IP、任意 URL 路径、用户信息、HA/Supervisor 主机和重定向。不要将服务公开到公网。本版本传输契约仅用于可信内部网络；外部平台认证由 provider 自行管理。

秘密目录为 0700，`demo.json` 为 0600，且只包含 `bearer` 字段。它是目标必需的最小运行副本，长期凭据必须先登记 Bitwarden，再无回显注入 Controller 和 provider；不要把真实秘密填进示例、Git、命令参数或日志。provider 使用自己的最小权限账号，不复用 HA/Supervisor 管理凭据。

注册与 credential 就绪后，新工具出现在现有控制台，**默认关闭**。管理员逐工具启用后，owner 的下一次微信请求可按语义调用。manifest、地址或工具 Schema/风险变化时旧启用状态被撤销，需重新审核启用。`enabled=false`、移除文件或配置损坏会撤销当前目录及旧缓存调用权；已有历史与审计保留。

最多登记 32 个服务，每个最多 64 个工具，合计最多 256 个工具。每次调度及扩展调用前重新读取配置；后台目录检查沿用 30 秒周期并通过 MCP list_changed 通知更新。无需重启 Controller。文件应先写临时文件再原子替换；不完整更新会暂时关闭扩展，不能保留旧权限冒充配置仍有效。

## provider 契约

请求示例（标识由 Controller 生成，模型不能传入）：

```json
{
  "version": 1,
  "name": "status",
  "arguments": {},
  "context": {
    "job_hash": "<不可逆摘要>",
    "turn_hash": "<不可逆摘要>",
    "idempotency_key": "sha256:<稳定摘要>"
  }
}
```

成功返回 HTTP 200 与符合 outputSchema 的 JSON 对象，例如 `{"status":"available"}`。响应正文有 128 KiB 上限和 15 秒超时；3xx/非 200/非法响应均拒绝，不转发错误正文或跟随重定向。Controller 拒绝敏感字段名和已知 bearer 值，但这不能替代 provider 的完整脱敏；provider 必须只返回可向微信用户展示的业务结果，不返回 Token、内部 URL、路径、原始日志或家庭敏感信息。

Schema 支持显式 type、description、enum、object/properties/required/additionalProperties=false、array/items/minItems/maxItems、string/minLength/maxLength、number/integer/minimum/maximum、boolean/null。object 必须闭合；string/array 必须有最大长度；深度最多 8。其他关键字（包括 $ref、pattern、oneOf）拒绝，不静默忽略约束。

write 工具还需要注册项 `allow_write=true`、管理员逐工具启用和 owner 活动 Turn。skill/manifest 不授予真实设备、删除、网络或账号变更权限；provider 负责冻结具体业务目标、校验实际授权、设备安全、原有 Broker 门禁及跨系统幂等。

Controller 在发送 write 前持久 claim。同会话/消息/工具/参数的已确认结果直接复用；超时、失败、非法或不安全响应、进程崩溃均保留 unknown 并拒绝自动重放。provider 必须独立持久去重 idempotency_key。改写消息或参数不能用于绕过未知结果；应人工核验实际状态后处理。read_only 声明必须符合实际无副作用行为，不能把写操作标成查询。

`examples/extension_provider.py` 是独立只读服务示例；认证仅从 provider 自身 `EXTENSION_BEARER` 环境取得。示例不会查询 HA 或控制真实设备。部署、网络、凭据登记、运行服务与微信手机验证需具体授权，不由安装示例自动执行。

## 验证与回滚

本地测试覆盖两个服务的动态接入、目录增删、策略持久化、风险/地址变更撤权、成员拒绝、Schema/凭据隔离、skill/reference 路径和写 unknown 重启防重放。正式验收应由用户通过微信查询新只读示例，分别记录 Controller 调用、Gateway 接受与手机实收；不能把本地模拟写成手机验收。

关闭注册项/逐工具策略可撤销能力，不删除持久数据。源码回退保留新增 SQLite 表和审计；旧版本回退前关闭 intake 并核对是否存在未知扩展写入，不能盲目重放。飞牛 0.5.58 专用候选仍独立保存，本版本未自动迁移其未提交内容。
