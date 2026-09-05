"""Bounded, read-only directory pages; policy revisions remain the CAS authority."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any
from urllib.parse import parse_qs

from .store import StoreError


SERVICE_LABELS = {
    "renovation_hub": "Renovation Hub",
    "ha_operations_broker": "Operations Broker",
    "family_memo": "家庭备忘录",
    "home_assistant_prepare_car": "用车准备",
}
SERVICES = frozenset({"all", *SERVICE_LABELS})
RISKS = frozenset({"all", "read_only", "write", "controlled"})
MAX_PAGE_SIZE = 48


def with_catalog_revision(document: dict[str, Any]) -> dict[str, Any]:
    """Ignore observation heartbeats but invalidate on any visible tool change."""
    content = {
        "revision": document["revision"],
        "policy_error": document["policy_error"],
        "tools": sorted(document["tools"], key=lambda tool: tool["name"]),
    }
    revision = hashlib.sha256(
        json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return {
        **document,
        "catalog_revision": revision,
        "summary": {**document["summary"], "catalog_revision": revision},
    }


def parse_page_query(query: str) -> dict[str, Any]:
    def invalid() -> StoreError:
        return StoreError("tool_catalog_query_invalid", "工具目录分页或筛选参数无效", status=400)

    if len(query) > 4096:
        raise invalid()
    try:
        values = parse_qs(query, keep_blank_values=True, strict_parsing=True, max_num_fields=7)
    except ValueError as exc:
        raise invalid() from exc
    if set(values) - {"limit", "offset", "search", "service", "risk", "catalog_revision"}:
        raise invalid()
    if any(len(items) != 1 or items[0] == "" for items in values.values()):
        raise invalid()
    params = {key: items[0] for key, items in values.items()}
    limit, offset = params.get("limit", "12"), params.get("offset", "0")
    if not re.fullmatch(r"[1-9][0-9]{0,2}", limit) or not re.fullmatch(r"0|[1-9][0-9]{0,6}", offset):
        raise invalid()
    if int(limit) > MAX_PAGE_SIZE or int(offset) > 1_000_000:
        raise invalid()
    service, risk = params.get("service", "all"), params.get("risk", "all")
    search = params.get("search", "").strip()
    revision = params.get("catalog_revision")
    if service not in SERVICES or risk not in RISKS:
        raise invalid()
    if len(search) > 200 or any(ord(character) < 32 for character in search):
        raise invalid()
    if revision is not None and not re.fullmatch(r"[0-9a-f]{64}", revision):
        raise invalid()
    return {"limit": int(limit), "offset": int(offset), "search": search, "service": service,
            "risk": risk, "catalog_revision": revision}


def tool_catalog_page(document: dict[str, Any], query: str) -> dict[str, Any]:
    params = parse_page_query(query)
    catalog = with_catalog_revision(document)
    if params["catalog_revision"] is not None and params["catalog_revision"] != catalog["catalog_revision"]:
        raise StoreError("tool_catalog_changed", "工具目录已更新，请重新载入当前页", status=409)
    search = params["search"].casefold()
    tools = [tool for tool in catalog["tools"]
             if (params["service"] == "all" or tool["service"] == params["service"])
             and (params["risk"] == "all" or tool["risk_type"] == params["risk"])
             and (not search or search in " ".join([
                 tool["name"], tool["display_name"], SERVICE_LABELS.get(tool["service"], tool["service"]),
                 *tool.get("intent_examples", []),
             ]).casefold())]
    tools.sort(key=lambda tool: (tool["service"], tool["display_name"].casefold(), tool["name"]))
    total = len(tools)
    limit = params["limit"]
    # Deletions/changed filters can remove the last page. Return the last valid page.
    offset = min(params["offset"], ((total - 1) // limit) * limit) if total else 0
    return {
        **catalog,
        "tools": tools[offset:offset + limit],
        "page": {
            "offset": offset, "limit": limit, "total": total,
            "callable": sum(bool(tool["callable"]) for tool in tools),
            "previous_offset": max(0, offset - limit) if offset else None,
            "next_offset": offset + limit if offset + limit < total else None,
            "search": params["search"], "service": params["service"], "risk": params["risk"],
        },
    }
