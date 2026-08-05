"""Stable metadata and schemas for every Controller MCP tool."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


GENERIC_INPUT_SCHEMA: dict[str, Any] = {"type": "object", "additionalProperties": True}


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    display_name: str
    service: str
    risk_type: str
    description: str
    intent_examples: tuple[str, ...]
    input_schema: dict[str, Any] | None = None
    transport: str = "json"
    requires_job_context: bool = False
    idempotent_write: bool = False
    annotations: dict[str, bool] | None = None

    def __post_init__(self) -> None:
        if self.input_schema is None:
            object.__setattr__(self, "input_schema", GENERIC_INPUT_SCHEMA)

    @property
    def read_only(self) -> bool:
        return self.risk_type == "read_only"

    def mcp_document(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }
        annotations = self.annotations
        if annotations is None and self.read_only:
            annotations = {
                "readOnlyHint": True,
                "destructiveHint": False,
                "idempotentHint": True,
                "openWorldHint": False,
            }
        if annotations is not None:
            document["annotations"] = dict(annotations)
        return document

    def public_metadata(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "service": self.service,
            "risk_type": self.risk_type,
            "intent_examples": list(self.intent_examples),
            "transport": self.transport,
            "requires_job_context": self.requires_job_context,
            "idempotent_write": self.idempotent_write,
        }


def _hub(
    name: str,
    display_name: str,
    risk_type: str,
    description: str,
    *intent_examples: str,
    input_schema: dict[str, Any] | None = None,
    transport: str = "json",
) -> ToolDefinition:
    read_only = risk_type == "read_only"
    return ToolDefinition(
        name=name,
        display_name=display_name,
        service="renovation_hub",
        risk_type=risk_type,
        description=description,
        intent_examples=tuple(intent_examples),
        input_schema=input_schema,
        transport=transport,
        requires_job_context=not read_only,
        idempotent_write=not read_only,
        annotations=(
            {
                "readOnlyHint": True,
                "destructiveHint": False,
                "idempotentHint": True,
                "openWorldHint": False,
            }
            if read_only
            else None
        ),
    )


def _operation(
    name: str,
    display_name: str,
    description: str,
    schema: dict[str, Any],
    *intent_examples: str,
) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        display_name=display_name,
        service="ha_operations_broker",
        risk_type="controlled",
        description=description,
        intent_examples=tuple(intent_examples),
        input_schema=schema,
    )


PAYMENT_TAG_DIMENSIONS = (
    "主题",
    "空间",
    "专业",
    "性质",
    "渠道",
    "品牌",
    "生态",
    "阶段",
    "状态",
)
PAYMENT_V2_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "amount_cents": {"type": "integer", "minimum": 1, "maximum": 100_000_000_000},
        "occurred_on": {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$"},
        "grouped_tags": {
            "type": "object",
            "properties": {
                dimension: {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1, "maxLength": 40},
                    "maxItems": 24,
                    "uniqueItems": True,
                }
                for dimension in PAYMENT_TAG_DIMENSIONS
            },
            "additionalProperties": False,
        },
        "merchant": {"type": "string", "maxLength": 200},
        "note": {"type": "string", "maxLength": 2000},
        "is_deposit": {"type": "boolean"},
        "source_ref": {"type": "string", "maxLength": 256},
        "project_id": {"type": "string", "minLength": 1, "maxLength": 64},
        "stage_id": {"type": "string", "minLength": 1, "maxLength": 64},
        "area_id": {"type": "string", "minLength": 1, "maxLength": 64},
    },
    "required": ["amount_cents", "occurred_on", "grouped_tags"],
    "additionalProperties": False,
}


BOOTSTRAP_HUB_DEFINITIONS: tuple[ToolDefinition, ...] = (
    _hub(
        "ledger_add_payment",
        "新增付款",
        "write",
        "向 Renovation Hub 装修账本新增 canonical v2 付款；必须使用金额分、日期和九维 grouped_tags，写操作受单 writer 和稳定幂等键约束。",
        "记一笔瓷砖付款",
        "新增装修支出",
        input_schema=PAYMENT_V2_INPUT_SCHEMA,
    ),
    _hub("ledger_add_refund", "新增退款", "write", "向 Renovation Hub 装修账本新增退款；写入受单 writer 和稳定幂等键约束。", "登记一笔退款", "记录商家返款"),
    _hub("ledger_correct_payment", "更正付款", "write", "更正既有装修付款；必须通过结构化工具并受服务端写入边界约束。", "把这笔付款金额改正", "更正付款分类"),
    _hub("ledger_undo", "撤销账本操作", "write", "撤销允许回退的既有账本操作；受服务端审计和幂等边界约束。", "撤销刚才那笔记账", "回退上一项账本修改"),
    _hub("ledger_attach", "关联账本附件", "write", "把 Gateway 的一次性附件关联到既有账本记录；会消费受控附件引用。", "把这张发票附到付款", "给这笔记录添加附件", transport="gateway_attachment"),
    _hub("ledger_show", "查看单条流水", "read_only", "只读查看 Renovation Hub 装修账本的一条既有流水及附件元数据；不会创建、修改、消费或删除数据，无需 Passkey 或额外确认。", "查看这笔付款详情", "显示某条账本记录"),
    _hub("ledger_query", "查询账本明细", "read_only", "只读查询 Renovation Hub 装修账本明细和筛选结果；不会创建、修改或删除数据，无需 Passkey 或额外确认。", "查询本月装修支出明细", "看看门窗付款记录"),
    _hub("ledger_summary", "汇总装修账本", "read_only", "只读汇总 Renovation Hub 装修账本的净支出、交易数量和分类统计；不会创建、修改或删除数据，无需 Passkey 或额外确认。", "装修一共花了多少钱", "按分类汇总支出"),
    _hub("ledger_generate_chart", "生成账本图表", "read_only", "基于既有装修账本数据生成统计图表；不会改变账本流水。", "生成装修支出图表", "画一张分类占比图"),
    _hub("ledger_export", "导出装修账本", "read_only", "导出既有装修账本的脱敏数据制品；不会修改账本流水。", "导出装修账本", "生成账本备份文件"),
    _hub("ledger_verify_export", "核验账本导出", "read_only", "核验既有账本导出制品的结构和摘要；不会修改账本。", "检查这个账本导出是否完整", "核验导出文件"),
    _hub("ledger_import_inspect", "检查待导入账本", "read_only", "只读检查待导入账本的结构、统计和风险；不会写入正式账本。", "检查这份账本能否导入", "预览导入内容"),
    _hub("ledger_import_shadow", "影子导入账本", "write", "把账本数据写入隔离影子区进行迁移验证；不会直接替代正式账本，但仍属于受控写入。", "把导入数据放到影子区验证", "执行账本影子导入"),
    _hub("renovation_project_create", "创建装修项目", "write", "在 Renovation Hub 创建装修项目；受服务端写入边界和审计约束。", "新建一个装修项目", "创建二期改造项目"),
    _hub("renovation_project_update", "更新装修项目", "write", "更新既有装修项目；受服务端写入边界和审计约束。", "修改项目名称", "更新装修项目状态"),
    _hub("renovation_project_list", "列出装修项目", "read_only", "只读列出 Renovation Hub 装修项目；不会创建、修改或删除数据，无需 Passkey 或额外确认。", "有哪些装修项目", "查看当前项目"),
    _hub("renovation_stage_create", "创建装修阶段", "write", "在装修项目中创建阶段；受服务端写入边界和审计约束。", "新增水电阶段", "创建木工阶段"),
    _hub("renovation_stage_update", "更新装修阶段", "write", "更新既有装修阶段；受服务端写入边界和审计约束。", "把水电阶段标记完成", "修改阶段时间"),
    _hub("renovation_stage_list", "列出装修阶段", "read_only", "只读列出 Renovation Hub 装修阶段；不会创建、修改或删除数据，无需 Passkey 或额外确认。", "装修现在有哪些阶段", "查看水电阶段"),
    _hub("renovation_area_create", "创建装修空间", "write", "在装修项目中创建空间；受服务端写入边界和审计约束。", "新增主卧空间", "创建厨房区域"),
    _hub("renovation_area_update", "更新装修空间", "write", "更新既有装修空间；受服务端写入边界和审计约束。", "修改厨房信息", "更新主卧施工状态"),
    _hub("renovation_area_list", "列出装修空间", "read_only", "只读列出 Renovation Hub 装修空间；不会创建、修改或删除数据，无需 Passkey 或额外确认。", "有哪些装修空间", "查看厨房记录"),
    _hub("renovation_event_create", "创建装修事件", "write", "创建装修进度或现场事件；受服务端写入边界和审计约束。", "记录今天水电验收", "新增现场进度"),
    _hub("renovation_event_update", "更新装修事件", "write", "更新既有装修事件；受服务端写入边界和审计约束。", "修改昨天的施工记录", "补充验收说明"),
    _hub("renovation_timeline", "查询装修时间线", "read_only", "只读查询 Renovation Hub 装修时间线；不会创建、修改或删除数据，无需 Passkey 或额外确认。", "最近装修有什么进展", "查看装修时间线"),
    _hub("renovation_dashboard", "查看装修驾驶舱", "read_only", "只读返回 Renovation Hub 装修驾驶舱、进度和统计数据；不会修改项目或账本，无需 Passkey 或额外确认。", "装修整体进度怎么样", "查看装修概况"),
    _hub("renovation_media_ingest", "归档装修媒体", "write", "把受控微信图片或视频流式归档到 Renovation Hub；会消费附件引用并受稳定幂等键约束。", "把这段施工视频归档", "保存这张现场照片", transport="gateway_media_stream"),
)


OPERATION_DEFINITIONS: tuple[ToolDefinition, ...] = (
    _operation(
        "ha_operations_propose_restart",
        "创建重启提案",
        "为一个精确 Add-on slug 创建不可变重启提案；不会执行重启。",
        {"type": "object", "additionalProperties": False, "required": ["target"], "properties": {"target": {"type": "string", "pattern": "^[a-z0-9][a-z0-9_]{0,63}$"}}},
        "准备重启某个 Add-on",
    ),
    _operation(
        "ha_operations_authorization_request",
        "发起操作授权",
        "为 Broker 已创建的精确提案生成 Passkey 授权请求；不会执行操作。",
        {"type": "object", "additionalProperties": False, "required": ["action_id"], "properties": {"action_id": {"type": "string", "pattern": "^OPS-[0-9]{8}-[A-F0-9]{12}$"}}},
        "为这个重启提案申请授权",
    ),
    _operation(
        "ha_operations_authorization_status",
        "查询操作授权",
        "读取已有授权请求和一次性收据状态。",
        {"type": "object", "additionalProperties": False, "required": ["approval_id"], "properties": {"approval_id": {"type": "string", "minLength": 8, "maxLength": 160}}},
        "查看 Passkey 授权状态",
    ),
    _operation(
        "ha_operations_execute_restart",
        "执行已授权重启",
        "消费已完成 Passkey 授权的一次性收据，并仅执行提案中的精确 Add-on 重启。",
        {"type": "object", "additionalProperties": False, "required": ["receipt_id", "action_id", "proposal_hash", "idempotency_key"], "properties": {"receipt_id": {"type": "string", "pattern": "^RCPT-[A-F0-9]{32}$"}, "action_id": {"type": "string", "pattern": "^OPS-[0-9]{8}-[A-F0-9]{12}$"}, "proposal_hash": {"type": "string", "pattern": "^sha256:[a-f0-9]{64}$"}, "idempotency_key": {"type": "string", "pattern": "^sha256:[a-f0-9]{64}$"}}},
        "执行已经授权的 Add-on 重启",
    ),
    _operation(
        "ha_operations_execution_status",
        "查询重启执行状态",
        "读取精确 Add-on 重启执行的状态与脱敏验证结果。",
        {"type": "object", "additionalProperties": False, "required": ["action_id"], "properties": {"action_id": {"type": "string", "pattern": "^OPS-[0-9]{8}-[A-F0-9]{12}$"}}},
        "查看重启执行结果",
    ),
)


TOOL_DEFINITIONS: tuple[ToolDefinition, ...] = BOOTSTRAP_HUB_DEFINITIONS + OPERATION_DEFINITIONS


TOOL_BY_NAME = {definition.name: definition for definition in TOOL_DEFINITIONS}
ALL_TOOL_NAMES = frozenset(TOOL_BY_NAME)
NATURAL_QUERY_READ_ONLY_TOOLS = frozenset(
    {
        "ledger_show",
        "ledger_query",
        "ledger_summary",
        "renovation_dashboard",
        "renovation_project_list",
        "renovation_stage_list",
        "renovation_area_list",
        "renovation_timeline",
    }
)
MEMBER_READ_ONLY_TOOL_NAMES = NATURAL_QUERY_READ_ONLY_TOOLS
LEDGER_TOOLS = frozenset(name for name in ALL_TOOL_NAMES if name.startswith("ledger_"))
RENOVATION_TOOLS = frozenset(name for name in ALL_TOOL_NAMES if name.startswith("renovation_"))
OPERATIONS_TOOLS = frozenset(name for name in ALL_TOOL_NAMES if name.startswith("ha_operations_"))
LEDGER_WRITE_TOOLS = frozenset(
    name for name in LEDGER_TOOLS if TOOL_BY_NAME[name].risk_type == "write"
)
RENOVATION_WRITE_TOOLS = frozenset(
    name for name in RENOVATION_TOOLS if TOOL_BY_NAME[name].risk_type == "write"
)


def mcp_tool_catalog(enabled_names: list[str] | tuple[str, ...] | set[str] | None = None) -> list[dict[str, Any]]:
    enabled = ALL_TOOL_NAMES if enabled_names is None else set(enabled_names)
    return [definition.mcp_document() for definition in TOOL_DEFINITIONS if definition.name in enabled]


def tool_definition_from_manifest(document: dict[str, Any]) -> ToolDefinition:
    """Convert one already-validated Hub manifest entry into Controller metadata."""
    return ToolDefinition(
        name=document["name"],
        display_name=document["display_name"],
        service="renovation_hub",
        risk_type="read_only" if document["risk_type"] == "read" else "write",
        description=document["description"],
        intent_examples=tuple(document.get("intent_examples", ())),
        input_schema=document["inputSchema"],
        transport=document["transport"],
        requires_job_context=document["requires_job_context"],
        idempotent_write=document["idempotent_write"],
        annotations=dict(document["annotations"]),
    )
