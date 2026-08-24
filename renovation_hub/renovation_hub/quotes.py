"""Inquiry and multi-supplier quotation domain for Renovation Hub."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import sqlite3
from typing import Any
import uuid
from zoneinfo import ZoneInfo

from .ledger import LedgerError, _idempotency_key, _text, _validate_date, canonical_json, utc_now


QUOTE_SCHEMA_VERSION = 1
REQUEST_STATUSES = {"inquiry", "quoted", "review_required", "selected", "purchased", "closed", "archived"}
OFFER_STATUSES = {"quoted", "review_required", "selected", "rejected", "expired", "purchased"}
MEDIA_ROLES = {"source", "product", "quote_sheet", "business_card", "address", "other"}
SHANGHAI = ZoneInfo("Asia/Shanghai")
MAX_CENTS = 100_000_000_000


def initialize_quote_schema(store: Any) -> None:
    """Install the additive quote schema without changing Ledger tables."""

    with store._connect() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS quote_requests (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(id),
                title TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                specification_json TEXT NOT NULL DEFAULT '{}',
                quantity_milli INTEGER CHECK(quantity_milli IS NULL OR quantity_milli > 0),
                unit TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL CHECK(status IN ('inquiry','quoted','review_required','selected','purchased','closed','archived')),
                follow_up_at TEXT,
                selected_offer_id TEXT,
                source_ref TEXT NOT NULL DEFAULT '',
                note TEXT NOT NULL DEFAULT '',
                version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS quote_requests_project_updated
                ON quote_requests(project_id, updated_at DESC);
            CREATE INDEX IF NOT EXISTS quote_requests_project_status
                ON quote_requests(project_id, status, updated_at DESC);
            CREATE TABLE IF NOT EXISTS quote_offers (
                id TEXT PRIMARY KEY,
                request_id TEXT NOT NULL REFERENCES quote_requests(id),
                supplier_name TEXT NOT NULL,
                contact_name TEXT NOT NULL DEFAULT '',
                contact_phone TEXT NOT NULL DEFAULT '',
                supplier_address TEXT NOT NULL DEFAULT '',
                quoted_at TEXT,
                valid_until TEXT,
                currency TEXT NOT NULL DEFAULT 'CNY' CHECK(currency='CNY'),
                subtotal_cents INTEGER CHECK(subtotal_cents IS NULL OR subtotal_cents >= 0),
                tax_cents INTEGER NOT NULL DEFAULT 0 CHECK(tax_cents >= 0),
                shipping_cents INTEGER NOT NULL DEFAULT 0 CHECK(shipping_cents >= 0),
                installation_cents INTEGER NOT NULL DEFAULT 0 CHECK(installation_cents >= 0),
                discount_cents INTEGER NOT NULL DEFAULT 0 CHECK(discount_cents >= 0),
                total_cents INTEGER CHECK(total_cents IS NULL OR total_cents >= 0),
                quantity_milli INTEGER CHECK(quantity_milli IS NULL OR quantity_milli > 0),
                unit TEXT NOT NULL DEFAULT '',
                unit_price_cents INTEGER CHECK(unit_price_cents IS NULL OR unit_price_cents >= 0),
                price_includes_tax INTEGER NOT NULL DEFAULT 0 CHECK(price_includes_tax IN (0,1)),
                lead_time_days INTEGER CHECK(lead_time_days IS NULL OR lead_time_days >= 0),
                brand TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '',
                specification_json TEXT NOT NULL DEFAULT '{}',
                payment_terms TEXT NOT NULL DEFAULT '',
                warranty TEXT NOT NULL DEFAULT '',
                note TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL CHECK(status IN ('quoted','review_required','selected','rejected','expired','purchased')),
                extraction_confidence INTEGER CHECK(extraction_confidence IS NULL OR (extraction_confidence >= 0 AND extraction_confidence <= 100)),
                source_ref TEXT NOT NULL DEFAULT '',
                version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS quote_offers_request_total
                ON quote_offers(request_id, total_cents, created_at);
            CREATE INDEX IF NOT EXISTS quote_offers_supplier
                ON quote_offers(supplier_name, updated_at DESC);
            CREATE TABLE IF NOT EXISTS quote_media_links (
                media_id TEXT NOT NULL REFERENCES media_assets(id),
                request_id TEXT NOT NULL REFERENCES quote_requests(id),
                offer_id TEXT REFERENCES quote_offers(id),
                role TEXT NOT NULL CHECK(role IN ('source','product','quote_sheet','business_card','address','other')),
                created_at TEXT NOT NULL,
                UNIQUE(media_id, request_id, offer_id, role)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS quote_media_unique
                ON quote_media_links(media_id, request_id, coalesce(offer_id,''), role);
            CREATE INDEX IF NOT EXISTS quote_media_request ON quote_media_links(request_id, created_at);
            CREATE INDEX IF NOT EXISTS quote_media_offer ON quote_media_links(offer_id, created_at);
            """
        )
        connection.execute(
            "INSERT OR IGNORE INTO metadata(key,value) VALUES ('quote_schema_version',?)",
            (str(QUOTE_SCHEMA_VERSION),),
        )
        connection.execute(
            "UPDATE metadata SET value=? WHERE key='quote_schema_version'",
            (str(QUOTE_SCHEMA_VERSION),),
        )


def _choice(value: Any, field: str, allowed: set[str], *, default: str | None = None) -> str:
    if value is None and default is not None:
        return default
    if not isinstance(value, str) or value not in allowed:
        raise LedgerError("invalid_input", f"{field} 不在允许范围")
    return value


def _version(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise LedgerError("version_required", "version 必须是正整数", status=409)
    return value


def _optional_datetime(value: Any, field: str) -> str | None:
    if value in {None, ""}:
        return None
    if not isinstance(value, str):
        raise LedgerError("invalid_datetime", f"{field} 必须是带时区 ISO 8601")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LedgerError("invalid_datetime", f"{field} 必须是带时区 ISO 8601") from exc
    if parsed.tzinfo is None:
        raise LedgerError("invalid_datetime", f"{field} 必须包含时区")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _optional_date(value: Any, field: str) -> str | None:
    if value in {None, ""}:
        return None
    return _validate_date(value, field)


def _optional_cents(value: Any, field: str, *, default: int | None = None) -> int | None:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > MAX_CENTS:
        raise LedgerError("invalid_amount", f"{field} 必须是非负整数分")
    return value


def _quantity(value: Any) -> int | None:
    if value in {None, ""}:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1 or value > 1_000_000_000:
        raise LedgerError("invalid_input", "quantity_milli 必须是正整数")
    return value


def _optional_integer(value: Any, field: str, maximum: int) -> int | None:
    if value in {None, ""}:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > maximum:
        raise LedgerError("invalid_input", f"{field} 超出允许范围")
    return value


def _boolean(value: Any, field: str, *, default: bool = False) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise LedgerError("invalid_input", f"{field} 必须是布尔值")
    return value


def _specification(value: Any) -> dict[str, str]:
    if value is None or value == "":
        return {}
    if not isinstance(value, dict) or len(value) > 32:
        raise LedgerError("invalid_input", "specification 必须是最多 32 项的对象")
    result: dict[str, str] = {}
    for key, item in value.items():
        clean_key = _text(key, "specification key", 40, required=True)
        clean_value = _text(item, f"specification.{clean_key}", 240)
        if clean_value:
            result[clean_key] = clean_value
    return result


class QuoteStoreMixin:
    """Store methods mixed into RenovationHubStore."""

    @staticmethod
    def _quote_request(connection: sqlite3.Connection, request_id: str) -> dict[str, Any]:
        row = connection.execute("SELECT * FROM quote_requests WHERE id=?", (request_id,)).fetchone()
        if row is None:
            raise LedgerError("quote_not_found", "询价不存在", status=404)
        result = dict(row)
        result["specification"] = json.loads(result.pop("specification_json"))
        return result

    @staticmethod
    def _quote_offer(connection: sqlite3.Connection, offer_id: str) -> dict[str, Any]:
        row = connection.execute("SELECT * FROM quote_offers WHERE id=?", (offer_id,)).fetchone()
        if row is None:
            raise LedgerError("quote_offer_not_found", "供应商报价不存在", status=404)
        result = dict(row)
        result["specification"] = json.loads(result.pop("specification_json"))
        result["price_includes_tax"] = bool(result["price_includes_tax"])
        result["effective_status"] = result["status"]
        if (
            result["valid_until"]
            and result["status"] in {"quoted", "review_required"}
            and result["valid_until"] < datetime.now(SHANGHAI).date().isoformat()
        ):
            result["effective_status"] = "expired"
        return result

    def _quote_media(self, connection: sqlite3.Connection, request_id: str) -> list[dict[str, Any]]:
        rows = connection.execute(
            """
            SELECT quote_media_links.offer_id,quote_media_links.role,media_assets.*
            FROM quote_media_links
            JOIN media_assets ON media_assets.id=quote_media_links.media_id
            WHERE quote_media_links.request_id=?
            ORDER BY quote_media_links.created_at,media_assets.id
            """,
            (request_id,),
        )
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["content_url"] = f"/api/v1/media/{item['id']}/content"
            item["preview_url"] = f"/api/v1/media/{item['id']}/preview" if item["preview_name"] else None
            for key in ("storage_name", "preview_name", "source_ref_hash"):
                item.pop(key, None)
            result.append(item)
        return result

    def create_quote(self, payload: dict[str, Any], *, actor_hash: str = "system") -> dict[str, Any]:
        key = _idempotency_key(payload.get("idempotency_key"))
        clean = {
            "project_id": _text(payload.get("project_id"), "project_id", 64, required=True),
            "title": _text(payload.get("title"), "title", 160, required=True),
            "category": _text(payload.get("category"), "category", 80),
            "description": _text(payload.get("description"), "description", 4000),
            "specification": _specification(payload.get("specification")),
            "quantity_milli": _quantity(payload.get("quantity_milli")),
            "unit": _text(payload.get("unit"), "unit", 40),
            "status": _choice(payload.get("status"), "status", REQUEST_STATUSES, default="inquiry"),
            "follow_up_at": _optional_datetime(payload.get("follow_up_at"), "follow_up_at"),
            "source_ref": _text(payload.get("source_ref"), "source_ref", 256),
            "note": _text(payload.get("note"), "note", 2000),
        }

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            self._validate_context_refs(connection, clean["project_id"], None, None)
            request_id = str(uuid.uuid4())
            now = utc_now()
            connection.execute(
                """
                INSERT INTO quote_requests(
                    id,project_id,title,category,description,specification_json,quantity_milli,unit,
                    status,follow_up_at,source_ref,note,created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    request_id, clean["project_id"], clean["title"], clean["category"],
                    clean["description"], canonical_json(clean["specification"]), clean["quantity_milli"],
                    clean["unit"], clean["status"], clean["follow_up_at"], clean["source_ref"],
                    clean["note"], now, now,
                ),
            )
            result = self._quote_request(connection, request_id)
            self._domain_audit(
                connection, action="create_quote", target_type="quote_request", target_id=request_id,
                actor_hash=actor_hash, idempotency_key=key, before=None, after=result,
            )
            return result

        result, replayed = self._run_idempotent(
            key=key, request={"tool": "renovation_quote_create", **clean}, operation=operation
        )
        return {"quote": result, "idempotent_replay": replayed}

    def update_quote(self, payload: dict[str, Any], *, actor_hash: str = "system") -> dict[str, Any]:
        key = _idempotency_key(payload.get("idempotency_key"))
        request_id = _text(payload.get("request_id"), "request_id", 64, required=True)
        expected_version = _version(payload.get("version"))
        changes = payload.get("changes")
        validators = {
            "title": lambda value: _text(value, "title", 160, required=True),
            "category": lambda value: _text(value, "category", 80),
            "description": lambda value: _text(value, "description", 4000),
            "specification": _specification,
            "quantity_milli": _quantity,
            "unit": lambda value: _text(value, "unit", 40),
            "status": lambda value: _choice(value, "status", REQUEST_STATUSES),
            "follow_up_at": lambda value: _optional_datetime(value, "follow_up_at"),
            "source_ref": lambda value: _text(value, "source_ref", 256),
            "note": lambda value: _text(value, "note", 2000),
        }
        if not isinstance(changes, dict) or not changes or set(changes) - set(validators):
            raise LedgerError("invalid_input", "changes 为空或包含不允许字段")
        clean = {field: validators[field](value) for field, value in changes.items()}
        columns = {("specification_json" if field == "specification" else field): (canonical_json(value) if field == "specification" else value) for field, value in clean.items()}

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            before = self._quote_request(connection, request_id)
            if before["version"] != expected_version:
                raise LedgerError("version_conflict", "询价已被其他请求修改", status=409)
            assignments = ",".join(f"{field}=?" for field in columns)
            cursor = connection.execute(
                f"UPDATE quote_requests SET {assignments},version=version+1,updated_at=? WHERE id=? AND version=?",
                (*columns.values(), utc_now(), request_id, expected_version),
            )
            if cursor.rowcount != 1:
                raise LedgerError("version_conflict", "询价已被其他请求修改", status=409)
            after = self._quote_request(connection, request_id)
            self._domain_audit(
                connection, action="update_quote", target_type="quote_request", target_id=request_id,
                actor_hash=actor_hash, idempotency_key=key, before=before, after=after,
            )
            return after

        result, replayed = self._run_idempotent(
            key=key,
            request={"tool": "renovation_quote_update", "request_id": request_id, "version": expected_version, "changes": clean},
            operation=operation,
        )
        return {"quote": result, "idempotent_replay": replayed}

    def add_quote_offer(self, payload: dict[str, Any], *, actor_hash: str = "system") -> dict[str, Any]:
        key = _idempotency_key(payload.get("idempotency_key"))
        clean = self._clean_offer_payload(payload, creating=True)

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            request = self._quote_request(connection, clean["request_id"])
            offer_id = str(uuid.uuid4())
            now = utc_now()
            connection.execute(
                """
                INSERT INTO quote_offers(
                    id,request_id,supplier_name,contact_name,contact_phone,supplier_address,
                    quoted_at,valid_until,currency,subtotal_cents,tax_cents,shipping_cents,
                    installation_cents,discount_cents,total_cents,quantity_milli,unit,unit_price_cents,
                    price_includes_tax,lead_time_days,brand,model,specification_json,payment_terms,
                    warranty,note,status,extraction_confidence,source_ref,created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    offer_id, clean["request_id"], clean["supplier_name"], clean["contact_name"],
                    clean["contact_phone"], clean["supplier_address"], clean["quoted_at"], clean["valid_until"],
                    "CNY", clean["subtotal_cents"], clean["tax_cents"], clean["shipping_cents"],
                    clean["installation_cents"], clean["discount_cents"], clean["total_cents"],
                    clean["quantity_milli"], clean["unit"], clean["unit_price_cents"],
                    int(clean["price_includes_tax"]), clean["lead_time_days"], clean["brand"], clean["model"],
                    canonical_json(clean["specification"]), clean["payment_terms"], clean["warranty"],
                    clean["note"], clean["status"], clean["extraction_confidence"], clean["source_ref"], now, now,
                ),
            )
            next_status = request["status"]
            if next_status in {"inquiry", "quoted", "review_required"}:
                next_status = "review_required" if clean["status"] == "review_required" else "quoted"
                connection.execute(
                    "UPDATE quote_requests SET status=?,version=version+1,updated_at=? WHERE id=?",
                    (next_status, now, clean["request_id"]),
                )
            offer = self._quote_offer(connection, offer_id)
            self._domain_audit(
                connection, action="add_quote_offer", target_type="quote_offer", target_id=offer_id,
                actor_hash=actor_hash, idempotency_key=key, before=None, after=offer,
            )
            return offer

        result, replayed = self._run_idempotent(
            key=key, request={"tool": "renovation_quote_add_offer", **clean}, operation=operation
        )
        return {"offer": result, "idempotent_replay": replayed}

    def _clean_offer_payload(self, payload: dict[str, Any], *, creating: bool) -> dict[str, Any]:
        subtotal = _optional_cents(payload.get("subtotal_cents"), "subtotal_cents")
        fees = {
            field: _optional_cents(payload.get(field), field, default=0) or 0
            for field in ("tax_cents", "shipping_cents", "installation_cents", "discount_cents")
        }
        total = _optional_cents(payload.get("total_cents"), "total_cents")
        if total is None and subtotal is not None:
            total = max(0, subtotal + fees["tax_cents"] + fees["shipping_cents"] + fees["installation_cents"] - fees["discount_cents"])
        quantity = _quantity(payload.get("quantity_milli"))
        unit_price = _optional_cents(payload.get("unit_price_cents"), "unit_price_cents")
        if unit_price is None and total is not None and quantity:
            unit_price = round(total * 1000 / quantity)
        return {
            "request_id": _text(payload.get("request_id"), "request_id", 64, required=True),
            "supplier_name": _text(payload.get("supplier_name"), "supplier_name", 200, required=creating),
            "contact_name": _text(payload.get("contact_name"), "contact_name", 120),
            "contact_phone": _text(payload.get("contact_phone"), "contact_phone", 80),
            "supplier_address": _text(payload.get("supplier_address"), "supplier_address", 500),
            "quoted_at": _optional_datetime(payload.get("quoted_at"), "quoted_at"),
            "valid_until": _optional_date(payload.get("valid_until"), "valid_until"),
            "subtotal_cents": subtotal,
            **fees,
            "total_cents": total,
            "quantity_milli": quantity,
            "unit": _text(payload.get("unit"), "unit", 40),
            "unit_price_cents": unit_price,
            "price_includes_tax": _boolean(payload.get("price_includes_tax"), "price_includes_tax"),
            "lead_time_days": _optional_integer(payload.get("lead_time_days"), "lead_time_days", 3650),
            "brand": _text(payload.get("brand"), "brand", 120),
            "model": _text(payload.get("model"), "model", 120),
            "specification": _specification(payload.get("specification")),
            "payment_terms": _text(payload.get("payment_terms"), "payment_terms", 1000),
            "warranty": _text(payload.get("warranty"), "warranty", 1000),
            "note": _text(payload.get("note"), "note", 2000),
            "status": _choice(payload.get("status"), "status", OFFER_STATUSES - {"selected"}, default="quoted"),
            "extraction_confidence": _optional_integer(payload.get("extraction_confidence"), "extraction_confidence", 100),
            "source_ref": _text(payload.get("source_ref"), "source_ref", 256),
        }

    def update_quote_offer(self, payload: dict[str, Any], *, actor_hash: str = "system") -> dict[str, Any]:
        key = _idempotency_key(payload.get("idempotency_key"))
        offer_id = _text(payload.get("offer_id"), "offer_id", 64, required=True)
        expected_version = _version(payload.get("version"))
        changes = payload.get("changes")
        if not isinstance(changes, dict) or not changes:
            raise LedgerError("invalid_input", "changes 不能为空")
        allowed = set(self._clean_offer_payload({"request_id": "placeholder", "supplier_name": "placeholder"}, creating=True)) - {"request_id"}
        if set(changes) - allowed:
            raise LedgerError("invalid_input", "changes 包含不允许字段")

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            before = self._quote_offer(connection, offer_id)
            merged = {**before, **changes, "request_id": before["request_id"]}
            component_fields = {"subtotal_cents", "tax_cents", "shipping_cents", "installation_cents", "discount_cents"}
            changed_components = {field for field in component_fields if field in changes and changes[field] != before[field]}
            total_changed = "total_cents" in changes and changes["total_cents"] != before["total_cents"]
            quantity_changed = "quantity_milli" in changes and changes["quantity_milli"] != before["quantity_milli"]
            unit_price_changed = "unit_price_cents" in changes and changes["unit_price_cents"] != before["unit_price_cents"]
            if changed_components and not total_changed:
                merged["total_cents"] = None
            if (changed_components or total_changed or quantity_changed) and not unit_price_changed:
                merged["unit_price_cents"] = None
            clean = self._clean_offer_payload(merged, creating=True)
            if clean["status"] == "selected":
                raise LedgerError("invalid_input", "选择报价必须使用专用操作")
            columns = {
                field: value
                for field, value in clean.items()
                if field not in {"request_id", "specification"} and field in changes
            }
            if "specification" in changes:
                columns["specification_json"] = canonical_json(clean["specification"])
            if changed_components and not total_changed:
                columns["total_cents"] = clean["total_cents"]
            if changed_components or total_changed or quantity_changed or unit_price_changed:
                columns["unit_price_cents"] = clean["unit_price_cents"]
            if "price_includes_tax" in columns:
                columns["price_includes_tax"] = int(bool(columns["price_includes_tax"]))
            assignments = ",".join(f"{field}=?" for field in columns)
            cursor = connection.execute(
                f"UPDATE quote_offers SET {assignments},version=version+1,updated_at=? WHERE id=? AND version=?",
                (*columns.values(), utc_now(), offer_id, expected_version),
            )
            if cursor.rowcount != 1:
                raise LedgerError("version_conflict", "报价已被其他请求修改", status=409)
            after = self._quote_offer(connection, offer_id)
            self._domain_audit(
                connection, action="update_quote_offer", target_type="quote_offer", target_id=offer_id,
                actor_hash=actor_hash, idempotency_key=key, before=before, after=after,
            )
            return after

        result, replayed = self._run_idempotent(
            key=key,
            request={"tool": "renovation_quote_update_offer", "offer_id": offer_id, "version": expected_version, "changes": changes},
            operation=operation,
        )
        return {"offer": result, "idempotent_replay": replayed}

    def list_quotes(self, filters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        filters = filters or {}
        project_id = _text(filters.get("project_id"), "project_id", 64, required=True)
        clauses = ["quote_requests.project_id=?"]
        values: list[Any] = [project_id]
        status = filters.get("status")
        if status:
            clauses.append("quote_requests.status=?")
            values.append(_choice(status, "status", REQUEST_STATUSES))
        keyword = _text(filters.get("keyword"), "keyword", 100)
        if keyword:
            escaped = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = f"%{escaped}%"
            clauses.append(
                "(quote_requests.title LIKE ? ESCAPE '\\' OR quote_requests.category LIKE ? ESCAPE '\\' "
                "OR quote_requests.description LIKE ? ESCAPE '\\' OR EXISTS (SELECT 1 FROM quote_offers qo "
                "WHERE qo.request_id=quote_requests.id AND (qo.supplier_name LIKE ? ESCAPE '\\' OR qo.brand LIKE ? ESCAPE '\\' OR qo.model LIKE ? ESCAPE '\\')))"
            )
            values.extend([pattern] * 6)
        limit = filters.get("limit", 200)
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > 1000:
            raise LedgerError("invalid_input", "limit 必须为 1 到 1000")
        values.append(limit)
        with self._connect() as connection:
            self._validate_context_refs(connection, project_id, None, None)
            rows = connection.execute(
                f"SELECT id FROM quote_requests WHERE {' AND '.join(clauses)} ORDER BY updated_at DESC,id DESC LIMIT ?",
                values,
            )
            return [self._quote_summary(connection, row["id"]) for row in rows]

    def _quote_summary(self, connection: sqlite3.Connection, request_id: str) -> dict[str, Any]:
        request = self._quote_request(connection, request_id)
        stats = connection.execute(
            "SELECT count(*) AS offer_count,min(total_cents) AS best_total_cents FROM quote_offers WHERE request_id=? AND status NOT IN ('rejected','expired')",
            (request_id,),
        ).fetchone()
        supplier_names = [
            row["supplier_name"]
            for row in connection.execute(
                "SELECT supplier_name FROM quote_offers WHERE request_id=? ORDER BY created_at,id",
                (request_id,),
            )
        ]
        cover = connection.execute(
            """
            SELECT media_assets.id,media_assets.original_filename,media_assets.preview_name
            FROM quote_media_links JOIN media_assets ON media_assets.id=quote_media_links.media_id
            WHERE quote_media_links.request_id=? AND media_assets.media_type='image'
            ORDER BY CASE quote_media_links.role WHEN 'product' THEN 0 WHEN 'quote_sheet' THEN 1 ELSE 2 END,quote_media_links.created_at
            LIMIT 1
            """,
            (request_id,),
        ).fetchone()
        request["offer_count"] = int(stats["offer_count"])
        request["best_total_cents"] = stats["best_total_cents"]
        request["supplier_names"] = supplier_names
        request["cover_media"] = (
            {
                "id": cover["id"],
                "original_filename": cover["original_filename"],
                "preview_url": f"/api/v1/media/{cover['id']}/preview" if cover["preview_name"] else f"/api/v1/media/{cover['id']}/content",
            }
            if cover else None
        )
        return request

    def show_quote(self, request_id: str) -> dict[str, Any]:
        request_id = _text(request_id, "request_id", 64, required=True)
        with self._connect() as connection:
            request = self._quote_summary(connection, request_id)
            offers = [self._quote_offer(connection, row["id"]) for row in connection.execute("SELECT id FROM quote_offers WHERE request_id=? ORDER BY created_at,id", (request_id,))]
            return {"quote": request, "offers": offers, "media": self._quote_media(connection, request_id)}

    def compare_quote(self, request_id: str) -> dict[str, Any]:
        result = self.show_quote(request_id)
        comparable = [offer for offer in result["offers"] if offer["total_cents"] is not None and offer["effective_status"] not in {"rejected", "expired"}]
        result["best_offer_id"] = min(comparable, key=lambda item: (item["total_cents"], item["id"]))["id"] if comparable else None
        units = {offer["unit"] for offer in comparable if offer["unit"] and offer["quantity_milli"]}
        result["unit_prices_comparable"] = len(units) <= 1 and all(offer["quantity_milli"] for offer in comparable)
        return result

    def select_quote_offer(self, payload: dict[str, Any], *, actor_hash: str = "system") -> dict[str, Any]:
        key = _idempotency_key(payload.get("idempotency_key"))
        request_id = _text(payload.get("request_id"), "request_id", 64, required=True)
        offer_id = _text(payload.get("offer_id"), "offer_id", 64, required=True)
        expected_version = _version(payload.get("version"))

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            before = self._quote_request(connection, request_id)
            offer = self._quote_offer(connection, offer_id)
            if before["version"] != expected_version:
                raise LedgerError("version_conflict", "询价已被其他请求修改", status=409)
            if offer["request_id"] != request_id or offer["effective_status"] in {"rejected", "expired"}:
                raise LedgerError("quote_offer_invalid", "报价不可选择", status=409)
            now = utc_now()
            connection.execute("UPDATE quote_offers SET status='quoted',version=version+1,updated_at=? WHERE request_id=? AND status='selected'", (now, request_id))
            connection.execute("UPDATE quote_offers SET status='selected',version=version+1,updated_at=? WHERE id=?", (now, offer_id))
            connection.execute(
                "UPDATE quote_requests SET selected_offer_id=?,status='selected',version=version+1,updated_at=? WHERE id=? AND version=?",
                (offer_id, now, request_id, expected_version),
            )
            after = self._quote_request(connection, request_id)
            self._domain_audit(
                connection, action="select_quote_offer", target_type="quote_request", target_id=request_id,
                actor_hash=actor_hash, idempotency_key=key, before=before, after=after,
            )
            return {"quote": after, "offer": self._quote_offer(connection, offer_id)}

        result, replayed = self._run_idempotent(
            key=key,
            request={"tool": "renovation_quote_select", "request_id": request_id, "offer_id": offer_id, "version": expected_version},
            operation=operation,
        )
        return {**result, "idempotent_replay": replayed}

    def attach_quote_media(self, payload: dict[str, Any], *, actor_hash: str = "system") -> dict[str, Any]:
        key = _idempotency_key(payload.get("idempotency_key"))
        request_id = _text(payload.get("request_id"), "request_id", 64, required=True)
        offer_id = _text(payload.get("offer_id"), "offer_id", 64) or None
        media_id = _text(payload.get("media_id"), "media_id", 64, required=True)
        role = _choice(payload.get("role"), "role", MEDIA_ROLES, default="source")

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            request = self._quote_request(connection, request_id)
            media = connection.execute("SELECT id,project_id,media_type,original_filename FROM media_assets WHERE id=?", (media_id,)).fetchone()
            if media is None or media["project_id"] != request["project_id"]:
                raise LedgerError("quote_media_invalid", "图片不存在或不属于询价项目", status=404)
            if offer_id:
                offer = self._quote_offer(connection, offer_id)
                if offer["request_id"] != request_id:
                    raise LedgerError("quote_media_invalid", "报价不属于该询价", status=404)
            now = utc_now()
            connection.execute(
                "INSERT OR IGNORE INTO quote_media_links(media_id,request_id,offer_id,role,created_at) VALUES (?,?,?,?,?)",
                (media_id, request_id, offer_id, role, now),
            )
            after = {"media_id": media_id, "request_id": request_id, "offer_id": offer_id, "role": role}
            self._domain_audit(
                connection, action="attach_quote_media", target_type="quote_media", target_id=media_id,
                actor_hash=actor_hash, idempotency_key=key, before=None, after=after,
            )
            return after

        result, replayed = self._run_idempotent(
            key=key, request={"tool": "renovation_quote_attach_media", "request_id": request_id, "offer_id": offer_id, "media_id": media_id, "role": role}, operation=operation
        )
        return {"link": result, "idempotent_replay": replayed}

    @staticmethod
    def quote_counts(connection: sqlite3.Connection) -> dict[str, int]:
        return {
            "quotes": connection.execute("SELECT count(*) FROM quote_requests WHERE status!='archived'").fetchone()[0],
            "quote_offers": connection.execute("SELECT count(*) FROM quote_offers").fetchone()[0],
            "quote_media": connection.execute("SELECT count(*) FROM quote_media_links").fetchone()[0],
        }
