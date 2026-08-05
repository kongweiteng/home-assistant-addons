"""Trusted Renovation Hub business-tool registry and MCP manifest."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from .ledger import LedgerError
from .portable import MAX_GROUPED_TAG_LENGTH, MAX_GROUPED_TAGS, TAG_DIMENSIONS


MANIFEST_VERSION = 1
MANIFEST_SERVICE = "renovation_hub"
MANIFEST_SCOPE = "business"
BUSINESS_CATALOG_REVISION = 2
ALLOWED_TRANSPORTS = {"json", "gateway_attachment", "gateway_media_stream"}
ALLOWED_RISK_TYPES = {"read", "write"}

ToolHandler = Callable[[Any, Optional[Any], dict[str, Any], str], dict[str, Any]]


@dataclass(frozen=True)
class BusinessToolDefinition:
    """One model-facing business capability and its deterministic executor."""

    name: str
    display_name: str
    description: str
    risk_type: str
    transport: str
    exposure: str
    requires_job_context: bool
    idempotent_write: bool
    input_schema: dict[str, Any]
    annotations: dict[str, bool]
    business_actions: tuple[str, ...]
    handler: ToolHandler | None

    def manifest_document(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "description": self.description,
            "risk_type": self.risk_type,
            "transport": self.transport,
            "exposure": self.exposure,
            "requires_job_context": self.requires_job_context,
            "idempotent_write": self.idempotent_write,
            "inputSchema": deepcopy(self.input_schema),
            "annotations": dict(self.annotations),
        }


@dataclass(frozen=True)
class BusinessActionExclusion:
    """A public or adjacent action intentionally unavailable as an MCP tool."""

    action_id: str
    reason: str


def _string(maximum: int, *, minimum: int = 0) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "string", "maxLength": maximum}
    if minimum:
        schema["minLength"] = minimum
    return schema


def _enum(*values: str) -> dict[str, Any]:
    return {"type": "string", "enum": list(values)}


def _integer(minimum: int, maximum: int) -> dict[str, Any]:
    return {"type": "integer", "minimum": minimum, "maximum": maximum}


def _object(
    properties: dict[str, Any] | None = None,
    *,
    required: Iterable[str] = (),
) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": deepcopy(properties or {}),
        "additionalProperties": False,
    }
    required_fields = list(required)
    if required_fields:
        schema["required"] = required_fields
    return schema


ID = _string(64, minimum=1)
DATE = {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$"}
DATETIME = _string(40, minimum=1)
LIMIT = _integer(1, 1000)
TAG = _string(160, minimum=1)
TAGS = {"type": "array", "items": TAG, "maxItems": 64, "uniqueItems": True}
GROUPED_TAGS = _object(
    {
        dimension: {
            "type": "array",
            "items": _string(MAX_GROUPED_TAG_LENGTH, minimum=1),
            "maxItems": MAX_GROUPED_TAGS,
            "uniqueItems": True,
        }
        for dimension in TAG_DIMENSIONS
    }
)
MEDIA_LINK = _object(
    {
        "target_type": _enum("event", "transaction", "stage", "area"),
        "target_id": ID,
    },
    required=("target_type", "target_id"),
)
MEDIA_LINKS = {"type": "array", "items": MEDIA_LINK, "maxItems": 16}


def _tool(
    name: str,
    display_name: str,
    description: str,
    *,
    risk_type: str,
    input_schema: dict[str, Any],
    business_actions: tuple[str, ...],
    handler: ToolHandler | None,
    transport: str = "json",
    requires_job_context: bool | None = None,
    idempotent_write: bool | None = None,
    destructive: bool = False,
) -> BusinessToolDefinition:
    write = risk_type == "write"
    job_context = write if requires_job_context is None else requires_job_context
    idempotent = write if idempotent_write is None else idempotent_write
    return BusinessToolDefinition(
        name=name,
        display_name=display_name,
        description=description,
        risk_type=risk_type,
        transport=transport,
        exposure="mcp",
        requires_job_context=job_context,
        idempotent_write=idempotent,
        input_schema=input_schema,
        annotations={
            "readOnlyHint": not write,
            "destructiveHint": destructive,
            "idempotentHint": (not write) or idempotent,
            "openWorldHint": False,
        },
        business_actions=business_actions,
        handler=handler,
    )


def _store_call(method: str, *, result_key: str | None = None) -> ToolHandler:
    def handler(store: Any, _media: Any | None, arguments: dict[str, Any], actor_hash: str) -> dict[str, Any]:
        function = getattr(store, method)
        if method in {
            "add_payment",
            "add_refund",
            "correct_payment",
            "undo",
            "attach_content",
            "create_project",
            "update_project",
            "create_stage",
            "update_stage",
            "create_area",
            "update_area",
            "create_event",
            "update_event",
        }:
            result = function(arguments, actor_hash=actor_hash)
        else:
            result = function(arguments)
        return {result_key: result} if result_key else result

    return handler


_PAYMENT_V2_ALLOWED_FIELDS = {
    "idempotency_key",
    "amount_cents",
    "occurred_on",
    "grouped_tags",
    "merchant",
    "note",
    "is_deposit",
    "source_ref",
    "project_id",
    "stage_id",
    "area_id",
}
_PAYMENT_V2_REQUIRED_FIELDS = {"amount_cents", "occurred_on", "grouped_tags"}


def _ledger_add_payment_v2(
    store: Any,
    _media: Any | None,
    arguments: dict[str, Any],
    actor_hash: str,
) -> dict[str, Any]:
    unknown = sorted(set(arguments) - _PAYMENT_V2_ALLOWED_FIELDS)
    if unknown:
        raise LedgerError(
            "invalid_input",
            f"ledger_add_payment 不接受字段：{', '.join(unknown)}",
        )
    missing = sorted(_PAYMENT_V2_REQUIRED_FIELDS - set(arguments))
    if missing:
        raise LedgerError(
            "invalid_input",
            f"ledger_add_payment 缺少字段：{', '.join(missing)}",
        )
    payload = dict(arguments)
    payload["ledger_format_version"] = 2
    return store.add_payment(payload, actor_hash=actor_hash)


def _ledger_show(store: Any, _media: Any | None, arguments: dict[str, Any], _actor_hash: str) -> dict[str, Any]:
    return store.show(str(arguments.get("transaction_id") or ""))


def _ledger_verify_export(store: Any, _media: Any | None, _arguments: dict[str, Any], _actor_hash: str) -> dict[str, Any]:
    path = store.share_dir / "current" / "kanhuwan-renovation-ledger.zip"
    return store.verify_portable(path)


def _ledger_import(store: Any, arguments: dict[str, Any], *, shadow: bool) -> dict[str, Any]:
    reference = str(arguments.get("import_ref") or "")
    if Path(reference).name != reference or not reference.endswith(".zip"):
        raise LedgerError("import_invalid", "import_ref 非法")
    path = store.import_dir / reference
    return store.import_shadow(path) if shadow else store.inspect_import(path)


def _ledger_import_inspect(store: Any, _media: Any | None, arguments: dict[str, Any], _actor_hash: str) -> dict[str, Any]:
    return _ledger_import(store, arguments, shadow=False)


def _ledger_import_shadow(store: Any, _media: Any | None, arguments: dict[str, Any], _actor_hash: str) -> dict[str, Any]:
    return _ledger_import(store, arguments, shadow=True)


def _project_list(store: Any, _media: Any | None, arguments: dict[str, Any], _actor_hash: str) -> dict[str, Any]:
    return {"items": store.list_projects(arguments)}


def _stage_list(store: Any, _media: Any | None, arguments: dict[str, Any], _actor_hash: str) -> dict[str, Any]:
    return {"items": store.list_stages(str(arguments.get("project_id") or ""))}


def _area_list(store: Any, _media: Any | None, arguments: dict[str, Any], _actor_hash: str) -> dict[str, Any]:
    return {"items": store.list_areas(str(arguments.get("project_id") or ""))}


def _timeline(store: Any, _media: Any | None, arguments: dict[str, Any], _actor_hash: str) -> dict[str, Any]:
    return {"items": store.timeline(arguments)}


def _dashboard(store: Any, _media: Any | None, arguments: dict[str, Any], _actor_hash: str) -> dict[str, Any]:
    return store.dashboard(str(arguments.get("project_id") or ""))


def _require_media(media: Any | None) -> Any:
    if media is None:
        raise LedgerError("media_service_unavailable", "媒体服务不可用", status=503)
    return media


def _public_media_asset(asset: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in asset.items()
        if key not in {"storage_name", "preview_name", "source_ref_hash"}
    }


def search_business_data(store: Any, media: Any | None, arguments: dict[str, Any]) -> dict[str, Any]:
    """Search the three user-facing business collections with stable ordering."""

    media_service = _require_media(media)
    return {
        "ledger": store.query(arguments),
        "timeline": store.timeline(arguments),
        "media": [_public_media_asset(item) for item in media_service.list(arguments)],
    }


def _search(store: Any, media: Any | None, arguments: dict[str, Any], _actor_hash: str) -> dict[str, Any]:
    return search_business_data(store, media, arguments)


def _media_list(_store: Any, media: Any | None, arguments: dict[str, Any], _actor_hash: str) -> dict[str, Any]:
    items = _require_media(media).list(arguments)
    return {"items": [_public_media_asset(item) for item in items]}


def _media_show(_store: Any, media: Any | None, arguments: dict[str, Any], _actor_hash: str) -> dict[str, Any]:
    asset = _require_media(media).get(str(arguments.get("media_id") or ""))
    return _public_media_asset(asset)


def _stream_transport_only(_store: Any, _media: Any | None, _arguments: dict[str, Any], _actor_hash: str) -> dict[str, Any]:
    raise LedgerError("transport_required", "该工具必须使用受控媒体流传输", status=409)


LEDGER_FILTERS = {
    "type": _enum("payment", "refund"),
    "status": _enum("active", "voided"),
    "main_category": _string(80, minimum=1),
    "start": DATE,
    "end": DATE,
    "keyword": _string(100, minimum=1),
    "tag": TAG,
    "project_id": ID,
    "stage_id": ID,
    "area_id": ID,
    "limit": LIMIT,
}
TIMELINE_FILTERS = {
    "project_id": ID,
    "stage_id": ID,
    "area_id": ID,
    "event_type": _enum("progress", "note", "decision", "inspection", "milestone"),
    "status": _enum("active", "voided"),
    "start": DATETIME,
    "end": DATETIME,
    "keyword": _string(100, minimum=1),
    "limit": LIMIT,
}
MEDIA_FILTERS = {
    "project_id": ID,
    "media_type": _enum("image", "video"),
    "processing_status": _enum("uploaded", "validating", "ready", "failed", "quarantined"),
    "stage_id": ID,
    "area_id": ID,
    "event_id": ID,
    "transaction_id": ID,
    "start": DATETIME,
    "end": DATETIME,
    "keyword": _string(100, minimum=1),
    "limit": LIMIT,
}


BUSINESS_TOOL_REGISTRY: tuple[BusinessToolDefinition, ...] = (
    _tool(
        "ledger_add_payment",
        "新增装修付款",
        "记录一笔装修付款或订金。",
        risk_type="write",
        input_schema=_object(
            {
                "amount_cents": _integer(1, 100_000_000_000),
                "occurred_on": DATE,
                "grouped_tags": GROUPED_TAGS,
                "merchant": _string(200),
                "note": _string(2000),
                "is_deposit": {"type": "boolean"},
                "source_ref": _string(256),
                "project_id": ID,
                "stage_id": ID,
                "area_id": ID,
            },
            required=("amount_cents", "occurred_on", "grouped_tags"),
        ),
        business_actions=("ledger.transaction.create",),
        handler=_ledger_add_payment_v2,
    ),
    _tool(
        "ledger_add_refund",
        "新增装修退款",
        "为既有付款记录一笔退款。",
        risk_type="write",
        input_schema=_object(
            {
                "original_payment_id": ID,
                "amount_cents": _integer(1, 100_000_000_000),
                "occurred_on": DATE,
                "note": _string(2000),
                "source_ref": _string(256),
                "project_id": ID,
                "stage_id": ID,
                "area_id": ID,
            },
            required=("original_payment_id", "amount_cents", "occurred_on"),
        ),
        business_actions=("ledger.refund.create",),
        handler=_store_call("add_refund"),
    ),
    _tool(
        "ledger_correct_payment",
        "修正装修付款",
        "按对象版本修正既有付款字段。",
        risk_type="write",
        input_schema=_object(
            {
                "payment_id": ID,
                "version": _integer(1, 2_147_483_647),
                "changes": _object(
                    {
                        "amount_cents": _integer(1, 100_000_000_000),
                        "occurred_on": DATE,
                        "main_category": _string(80, minimum=1),
                        "merchant": _string(200),
                        "note": _string(2000),
                        "is_deposit": {"type": "boolean"},
                        "tags": TAGS,
                        "grouped_tags": GROUPED_TAGS,
                    }
                ),
                "reason": _string(500, minimum=1),
            },
            required=("payment_id", "changes", "reason"),
        ),
        business_actions=("ledger.transaction.update",),
        handler=_store_call("correct_payment"),
    ),
    _tool(
        "ledger_undo",
        "撤销装修流水",
        "审计化撤销一笔有效装修流水。",
        risk_type="write",
        input_schema=_object(
            {
                "transaction_id": ID,
                "version": _integer(1, 2_147_483_647),
                "reason": _string(500, minimum=1),
            },
            required=("transaction_id", "reason"),
        ),
        business_actions=("ledger.transaction.undo",),
        handler=_store_call("undo"),
        destructive=True,
    ),
    _tool(
        "ledger_attach",
        "添加账目附件",
        "把微信附件关联到指定装修流水。",
        risk_type="write",
        transport="gateway_attachment",
        input_schema=_object(
            {"transaction_id": ID, "attachment_ref": _string(160, minimum=1)},
            required=("transaction_id", "attachment_ref"),
        ),
        business_actions=("ledger.attachment.create",),
        handler=_store_call("attach_content"),
    ),
    _tool(
        "ledger_show",
        "查看装修流水",
        "查看一笔装修流水及其附件元数据。",
        risk_type="read",
        input_schema=_object({"transaction_id": ID}, required=("transaction_id",)),
        business_actions=("ledger.transaction.show",),
        handler=_ledger_show,
    ),
    _tool(
        "ledger_query",
        "查询装修账目",
        "按日期、类型、分类、标签、空间或关键词查询装修流水。",
        risk_type="read",
        input_schema=_object(LEDGER_FILTERS),
        business_actions=("ledger.collection.list", "ledger.transaction.list"),
        handler=_store_call("query", result_key="items"),
    ),
    _tool(
        "ledger_summary",
        "汇总装修账目",
        "按筛选条件汇总净支出、分类、标签与维度。",
        risk_type="read",
        input_schema=_object(LEDGER_FILTERS),
        business_actions=("ledger.report.summary",),
        handler=_store_call("summary"),
    ),
    _tool(
        "ledger_generate_chart",
        "生成装修账目图表",
        "生成可下载的中文装修账目 PNG 图表。",
        risk_type="write",
        input_schema=_object(LEDGER_FILTERS),
        business_actions=("ledger.chart.generate",),
        handler=_store_call("generate_chart"),
        requires_job_context=False,
        idempotent_write=False,
    ),
    _tool(
        "ledger_export",
        "导出装修账本",
        "生成当前装修账本便携包。",
        risk_type="write",
        input_schema=_object(),
        business_actions=("ledger.portable.export",),
        handler=lambda store, _media, _arguments, _actor: store.export_portable(),
        requires_job_context=False,
        idempotent_write=False,
    ),
    _tool(
        "ledger_verify_export",
        "校验装修账本导出",
        "校验当前便携包的结构、摘要与账本不变量。",
        risk_type="read",
        input_schema=_object(),
        business_actions=("ledger.portable.verify",),
        handler=_ledger_verify_export,
    ),
    _tool(
        "ledger_import_inspect",
        "检查装修账本导入包",
        "只读检查固定导入目录中的便携包。",
        risk_type="read",
        input_schema=_object({"import_ref": _string(255, minimum=5)}, required=("import_ref",)),
        business_actions=("ledger.portable.inspect",),
        handler=_ledger_import_inspect,
    ),
    _tool(
        "ledger_import_shadow",
        "导入装修账本影子",
        "把已验证便携包导入隔离的只读影子目录。",
        risk_type="write",
        input_schema=_object({"import_ref": _string(255, minimum=5)}, required=("import_ref",)),
        business_actions=("ledger.portable.shadow_import",),
        handler=_ledger_import_shadow,
        requires_job_context=False,
        idempotent_write=False,
    ),
    _tool(
        "renovation_project_create",
        "创建装修项目",
        "创建一个装修项目。",
        risk_type="write",
        input_schema=_object(
            {
                "name": _string(120, minimum=1),
                "timezone": _string(64, minimum=1),
                "budget_cents": _integer(0, 100_000_000_000),
                "status": _enum("active", "completed", "archived"),
            },
            required=("name",),
        ),
        business_actions=("project.create",),
        handler=_store_call("create_project"),
    ),
    _tool(
        "renovation_project_update",
        "更新装修项目",
        "按对象版本更新既有装修项目。",
        risk_type="write",
        input_schema=_object(
            {
                "project_id": ID,
                "version": _integer(1, 2_147_483_647),
                "changes": _object(
                    {
                        "name": _string(120, minimum=1),
                        "timezone": _string(64, minimum=1),
                        "budget_cents": _integer(0, 100_000_000_000),
                        "status": _enum("active", "completed", "archived"),
                    }
                ),
            },
            required=("project_id", "version", "changes"),
        ),
        business_actions=("project.update",),
        handler=_store_call("update_project"),
    ),
    _tool(
        "renovation_project_list",
        "查询装修项目",
        "列出装修项目，可按状态筛选。",
        risk_type="read",
        input_schema=_object({"status": _enum("active", "completed", "archived")}),
        business_actions=("project.list",),
        handler=_project_list,
    ),
    _tool(
        "renovation_stage_create",
        "创建装修阶段",
        "为项目创建装修阶段。",
        risk_type="write",
        input_schema=_object(
            {
                "project_id": ID,
                "name": _string(100, minimum=1),
                "position": _integer(0, 10000),
                "status": _enum("planned", "active", "completed", "archived"),
                "color": _string(32, minimum=1),
                "planned_start": DATE,
                "planned_end": DATE,
                "actual_start": DATE,
                "actual_end": DATE,
            },
            required=("project_id", "name"),
        ),
        business_actions=("stage.create",),
        handler=_store_call("create_stage"),
    ),
    _tool(
        "renovation_stage_update",
        "更新装修阶段",
        "按对象版本更新装修阶段。",
        risk_type="write",
        input_schema=_object(
            {
                "stage_id": ID,
                "version": _integer(1, 2_147_483_647),
                "changes": _object(
                    {
                        "name": _string(100, minimum=1),
                        "position": _integer(0, 10000),
                        "status": _enum("planned", "active", "completed", "archived"),
                        "color": _string(32, minimum=1),
                        "planned_start": {"type": ["string", "null"]},
                        "planned_end": {"type": ["string", "null"]},
                        "actual_start": {"type": ["string", "null"]},
                        "actual_end": {"type": ["string", "null"]},
                    }
                ),
            },
            required=("stage_id", "version", "changes"),
        ),
        business_actions=("stage.update",),
        handler=_store_call("update_stage"),
    ),
    _tool(
        "renovation_stage_list",
        "查询装修阶段",
        "按项目列出装修阶段。",
        risk_type="read",
        input_schema=_object({"project_id": ID}, required=("project_id",)),
        business_actions=("stage.list",),
        handler=_stage_list,
    ),
    _tool(
        "renovation_area_create",
        "创建装修空间",
        "为项目创建房间或施工空间。",
        risk_type="write",
        input_schema=_object(
            {
                "project_id": ID,
                "name": _string(100, minimum=1),
                "position": _integer(0, 10000),
                "status": _enum("active", "archived"),
            },
            required=("project_id", "name"),
        ),
        business_actions=("area.create",),
        handler=_store_call("create_area"),
    ),
    _tool(
        "renovation_area_update",
        "更新装修空间",
        "按对象版本更新房间或施工空间。",
        risk_type="write",
        input_schema=_object(
            {
                "area_id": ID,
                "version": _integer(1, 2_147_483_647),
                "changes": _object(
                    {
                        "name": _string(100, minimum=1),
                        "position": _integer(0, 10000),
                        "status": _enum("active", "archived"),
                    }
                ),
            },
            required=("area_id", "version", "changes"),
        ),
        business_actions=("area.update",),
        handler=_store_call("update_area"),
    ),
    _tool(
        "renovation_area_list",
        "查询装修空间",
        "按项目列出房间和施工空间。",
        risk_type="read",
        input_schema=_object({"project_id": ID}, required=("project_id",)),
        business_actions=("area.list",),
        handler=_area_list,
    ),
    _tool(
        "renovation_event_create",
        "创建装修记录",
        "创建施工、验收、决策或里程碑记录。",
        risk_type="write",
        input_schema=_object(
            {
                "project_id": ID,
                "stage_id": ID,
                "area_id": ID,
                "event_type": _enum("progress", "note", "decision", "inspection", "milestone"),
                "title": _string(160, minimum=1),
                "description": _string(4000),
                "occurred_at": DATETIME,
                "source_ref": _string(256),
            },
            required=("project_id", "title", "occurred_at"),
        ),
        business_actions=("event.create",),
        handler=_store_call("create_event"),
    ),
    _tool(
        "renovation_event_update",
        "更新装修记录",
        "按对象版本更新施工时间线记录。",
        risk_type="write",
        input_schema=_object(
            {
                "event_id": ID,
                "version": _integer(1, 2_147_483_647),
                "changes": _object(
                    {
                        "stage_id": {"type": ["string", "null"], "maxLength": 64},
                        "area_id": {"type": ["string", "null"], "maxLength": 64},
                        "event_type": _enum("progress", "note", "decision", "inspection", "milestone"),
                        "title": _string(160, minimum=1),
                        "description": _string(4000),
                        "occurred_at": DATETIME,
                        "status": _enum("active", "voided"),
                    }
                ),
            },
            required=("event_id", "version", "changes"),
        ),
        business_actions=("event.update",),
        handler=_store_call("update_event"),
    ),
    _tool(
        "renovation_timeline",
        "查询装修时间线",
        "按项目、阶段、空间、类型、时间或关键词查询装修记录。",
        risk_type="read",
        input_schema=_object(TIMELINE_FILTERS, required=("project_id",)),
        business_actions=("event.list", "timeline.list"),
        handler=_timeline,
    ),
    _tool(
        "renovation_dashboard",
        "查看装修总览",
        "查看项目预算、支出、阶段、空间和近期进度总览。",
        risk_type="read",
        input_schema=_object({"project_id": ID}, required=("project_id",)),
        business_actions=("dashboard.read",),
        handler=_dashboard,
    ),
    _tool(
        "renovation_media_ingest",
        "保存装修图片视频",
        "把微信中的图片或视频流式保存并关联到装修对象。",
        risk_type="write",
        transport="gateway_media_stream",
        input_schema=_object(
            {
                "attachment_ref": _string(160, minimum=1),
                "project_id": ID,
                "captured_at": DATETIME,
                "links": MEDIA_LINKS,
            },
            required=("attachment_ref", "project_id"),
        ),
        business_actions=("media.ingest", "upload.create", "upload.content", "upload.complete"),
        handler=_stream_transport_only,
    ),
    _tool(
        "renovation_search",
        "统一搜索装修档案",
        "按项目和关键词同时搜索账目、时间线和媒体元数据。",
        risk_type="read",
        input_schema=_object(
            {
                "project_id": ID,
                "keyword": _string(100, minimum=1),
                "stage_id": ID,
                "area_id": ID,
                "limit": LIMIT,
            },
            required=("project_id",),
        ),
        business_actions=("search.unified",),
        handler=_search,
    ),
    _tool(
        "renovation_media_list",
        "查询装修图片视频",
        "按项目、时间、阶段、空间、类型或关键词列出媒体元数据。",
        risk_type="read",
        input_schema=_object(MEDIA_FILTERS, required=("project_id",)),
        business_actions=("media.list",),
        handler=_media_list,
    ),
    _tool(
        "renovation_media_show",
        "查看装修媒体详情",
        "查看一项图片或视频的元数据、业务关联和受控内容地址。",
        risk_type="read",
        input_schema=_object({"media_id": ID}, required=("media_id",)),
        business_actions=("media.show",),
        handler=_media_show,
    ),
)


BUSINESS_ACTION_EXCLUSIONS: tuple[BusinessActionExclusion, ...] = (
    BusinessActionExclusion("session.read", "页面 CSRF 会话属于认证边界，不是业务数据工具。"),
    BusinessActionExclusion("media.content", "媒体正文只经受控二进制响应传输，不进入 MCP JSON 或模型上下文。"),
    BusinessActionExclusion("media.preview", "媒体预览只经受控二进制响应传输，不进入 MCP JSON 或模型上下文。"),
    BusinessActionExclusion("admin.writer_mode", "writer 状态是单写入者安全边界，必须保持独立管理流程。"),
    BusinessActionExclusion("admin.cutover", "正式迁移、激活、暂停和恢复必须保持独立 cutover 授权。"),
    BusinessActionExclusion("internal.media_replay", "媒体重放表是传输幂等实现细节，只允许 Controller 内部校验。"),
    BusinessActionExclusion("internal.download", "图表和便携包下载是受控制品传输，不接受模型构造任意路径。"),
    BusinessActionExclusion("internal.restore_purge", "恢复、清理和内部幂等表不属于公开业务数据。"),
)


def validate_business_tool_registry(
    registry: Iterable[BusinessToolDefinition] = BUSINESS_TOOL_REGISTRY,
) -> None:
    names: set[str] = set()
    actions: set[str] = set()
    for definition in registry:
        if definition.name in names:
            raise ValueError(f"duplicate business tool: {definition.name}")
        names.add(definition.name)
        if not definition.name.startswith(("ledger_", "renovation_")):
            raise ValueError(f"invalid business namespace: {definition.name}")
        if definition.risk_type not in ALLOWED_RISK_TYPES:
            raise ValueError(f"invalid risk type: {definition.name}")
        if definition.transport not in ALLOWED_TRANSPORTS:
            raise ValueError(f"invalid transport: {definition.name}")
        if definition.exposure != "mcp":
            raise ValueError(f"invalid exposure: {definition.name}")
        if definition.input_schema.get("type") != "object":
            raise ValueError(f"input schema must be object: {definition.name}")
        if definition.input_schema.get("additionalProperties") is not False:
            raise ValueError(f"input schema must reject unknown properties: {definition.name}")
        if set(definition.annotations) != {
            "readOnlyHint",
            "destructiveHint",
            "idempotentHint",
            "openWorldHint",
        }:
            raise ValueError(f"invalid annotations: {definition.name}")
        if definition.annotations["readOnlyHint"] != (definition.risk_type == "read"):
            raise ValueError(f"risk and read-only annotation disagree: {definition.name}")
        if definition.annotations["openWorldHint"]:
            raise ValueError(f"business tool cannot be open-world: {definition.name}")
        if definition.risk_type == "read" and definition.transport != "json":
            raise ValueError(f"read tool cannot request attachment transport: {definition.name}")
        if definition.risk_type == "read" and (
            definition.requires_job_context or definition.idempotent_write
        ):
            raise ValueError(f"read tool cannot request write context: {definition.name}")
        if definition.risk_type == "write" and (
            definition.requires_job_context != definition.idempotent_write
        ):
            raise ValueError(f"write context and idempotency must agree: {definition.name}")
        if definition.transport != "json" and not (
            definition.requires_job_context and definition.idempotent_write
        ):
            raise ValueError(f"attachment transport requires idempotent job context: {definition.name}")
        if (
            definition.transport == "gateway_media_stream"
            and definition.name != "renovation_media_ingest"
        ):
            raise ValueError(f"unsupported media stream tool: {definition.name}")
        if definition.handler is None:
            raise ValueError(f"missing registry dispatch handler: {definition.name}")
        if not definition.business_actions:
            raise ValueError(f"missing business action coverage: {definition.name}")
        for action in definition.business_actions:
            if action in actions:
                raise ValueError(f"duplicate business action: {action}")
            actions.add(action)

    exclusion_ids: set[str] = set()
    for exclusion in BUSINESS_ACTION_EXCLUSIONS:
        if not exclusion.reason.strip():
            raise ValueError(f"missing exclusion reason: {exclusion.action_id}")
        if exclusion.action_id in actions or exclusion.action_id in exclusion_ids:
            raise ValueError(f"duplicate business action exposure: {exclusion.action_id}")
        exclusion_ids.add(exclusion.action_id)


validate_business_tool_registry()
_TOOLS_BY_NAME = {definition.name: definition for definition in BUSINESS_TOOL_REGISTRY}


def get_business_tool(name: Any) -> BusinessToolDefinition:
    definition = _TOOLS_BY_NAME.get(name) if isinstance(name, str) else None
    if definition is None:
        raise LedgerError("unknown_tool", "工具不在允许清单", status=404)
    return definition


def dispatch_business_tool(
    store: Any,
    payload: dict[str, Any],
    *,
    media: Any | None = None,
) -> dict[str, Any]:
    arguments = payload.get("arguments", {})
    if not isinstance(arguments, dict):
        raise LedgerError("invalid_input", "arguments 必须是对象")
    definition = get_business_tool(payload.get("name"))
    actor_hash = str(payload.get("actor_hash") or "system")
    handler = definition.handler
    if handler is None:
        raise LedgerError("registry_invalid", "工具执行器未配置", status=500)
    return handler(store, media, arguments, actor_hash)


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def build_business_manifest(
    registry: Iterable[BusinessToolDefinition] = BUSINESS_TOOL_REGISTRY,
    *,
    catalog_revision: int = BUSINESS_CATALOG_REVISION,
) -> dict[str, Any]:
    definitions = tuple(registry)
    validate_business_tool_registry(definitions)
    if isinstance(catalog_revision, bool) or not isinstance(catalog_revision, int) or catalog_revision < 1:
        raise ValueError("catalog_revision must be a positive integer")
    unsigned = {
        "version": MANIFEST_VERSION,
        "service": MANIFEST_SERVICE,
        "scope": MANIFEST_SCOPE,
        "catalog_revision": catalog_revision,
        "tools": [
            definition.manifest_document()
            for definition in sorted(definitions, key=lambda item: item.name)
        ],
    }
    digest = hashlib.sha256(_canonical_json(unsigned)).hexdigest()
    return {**unsigned, "catalog_digest": f"sha256:{digest}"}


_BUSINESS_MANIFEST = build_business_manifest()


def business_manifest() -> dict[str, Any]:
    return deepcopy(_BUSINESS_MANIFEST)


def business_action_coverage() -> dict[str, dict[str, str]]:
    coverage: dict[str, dict[str, str]] = {}
    for definition in BUSINESS_TOOL_REGISTRY:
        for action in definition.business_actions:
            coverage[action] = {"exposure": "mcp", "tool": definition.name}
    for exclusion in BUSINESS_ACTION_EXCLUSIONS:
        coverage[exclusion.action_id] = {
            "exposure": "excluded",
            "reason": exclusion.reason,
        }
    return coverage


def validate_public_business_actions(action_ids: Iterable[str]) -> None:
    coverage = business_action_coverage()
    missing = sorted(set(action_ids) - set(coverage))
    if missing:
        raise ValueError(f"public business actions missing MCP coverage: {missing}")
