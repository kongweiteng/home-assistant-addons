"""aiohttp web application for Renovation Hub."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
from typing import Any

from aiohttp import web

from .api import dispatch_tool, render_dashboard
from .business_tools import (
    business_manifest,
    get_business_tool,
    search_business_data,
    validate_public_business_actions,
)
from .hub import RenovationHubStore
from .ledger import LedgerError
from .media import MediaService


def _make_app_key(name: str, value_type: type[Any]) -> Any:
    app_key = getattr(web, "AppKey", None)
    return app_key(name, value_type) if callable(app_key) else name


STORE_KEY = _make_app_key("store", RenovationHubStore)
MEDIA_KEY = _make_app_key("media", MediaService)
API_TOKEN_KEY = _make_app_key("api_token", str)
CUTOVER_TOKEN_KEY = _make_app_key("cutover_token", str)
STATIC_DIR_KEY = _make_app_key("static_dir", object)
CSRF_TOKEN_KEY = _make_app_key("csrf_token", str)
PAGE_ACTOR = "sha256:renovation-hub-ingress-admin"
BUSINESS_ROUTE_PREFIX = "business__"
CHART_REFERENCE_RE = re.compile(r"^summary-[a-f0-9]{32}\.png$")


def _business_route_name(action_id: str) -> str:
    return BUSINESS_ROUTE_PREFIX + action_id.replace(".", "__")


def business_action_from_route_name(name: str | None) -> str | None:
    if not name or not name.startswith(BUSINESS_ROUTE_PREFIX):
        return None
    return name[len(BUSINESS_ROUTE_PREFIX) :].replace("__", ".")


def _add_business_route(
    app: web.Application,
    method: str,
    path: str,
    handler: Any,
    action_id: str,
) -> None:
    name = _business_route_name(action_id)
    if method == "GET":
        app.router.add_get(path, handler, name=name)
    else:
        app.router.add_route(method, path, handler, name=name)


def create_app(
    *,
    store: RenovationHubStore,
    media: MediaService,
    api_token: str,
    cutover_token: str = "",
    max_request_bytes: int,
    static_dir: str | Path | None = None,
) -> web.Application:
    app = web.Application(
        client_max_size=max_request_bytes,
        middlewares=[security_headers_middleware, error_middleware],
    )
    app[STORE_KEY] = store
    app[MEDIA_KEY] = media
    app[API_TOKEN_KEY] = api_token
    app[CUTOVER_TOKEN_KEY] = cutover_token
    app[STATIC_DIR_KEY] = Path(static_dir) if static_dir else None
    app[CSRF_TOKEN_KEY] = secrets.token_urlsafe(32)

    app.router.add_get("/healthz", health)
    app.router.add_get("/api/status", health)
    business_routes = (
        ("GET", "/api/v1/session", page_session, "session.read"),
        ("GET", "/api/v1/projects", projects, "project.list"),
        ("POST", "/api/v1/projects", project_create, "project.create"),
        ("PATCH", "/api/v1/projects/{project_id}", project_update, "project.update"),
        ("GET", "/api/v1/stages", stages, "stage.list"),
        ("POST", "/api/v1/stages", stage_create, "stage.create"),
        ("PATCH", "/api/v1/stages/{stage_id}", stage_update, "stage.update"),
        ("GET", "/api/v1/areas", areas, "area.list"),
        ("POST", "/api/v1/areas", area_create, "area.create"),
        ("PATCH", "/api/v1/areas/{area_id}", area_update, "area.update"),
        ("GET", "/api/v1/events", timeline, "event.list"),
        ("POST", "/api/v1/events", event_create, "event.create"),
        ("PATCH", "/api/v1/events/{event_id}", event_update, "event.update"),
        ("GET", "/api/v1/timeline", timeline, "timeline.list"),
        ("GET", "/api/v1/dashboard", dashboard, "dashboard.read"),
        ("GET", "/api/v1/ledger", ledger, "ledger.collection.list"),
        ("GET", "/api/v1/ledger/transactions", ledger, "ledger.transaction.list"),
        ("POST", "/api/v1/ledger/transactions", ledger_create, "ledger.transaction.create"),
        ("PATCH", "/api/v1/ledger/transactions/{transaction_id}", ledger_update, "ledger.transaction.update"),
        ("POST", "/api/v1/ledger/refunds", ledger_refund, "ledger.refund.create"),
        ("POST", "/api/v1/ledger/transactions/{transaction_id}/undo", ledger_undo, "ledger.transaction.undo"),
        ("GET", "/api/v1/reports/summary", report_summary, "ledger.report.summary"),
        ("GET", "/api/v1/search", search, "search.unified"),
        ("GET", "/api/v1/media", media_list, "media.list"),
        ("GET", "/api/v1/media/{media_id}/content", media_content, "media.content"),
        ("GET", "/api/v1/media/{media_id}/preview", media_preview, "media.preview"),
        ("POST", "/api/v1/uploads", upload_create, "upload.create"),
        ("PUT", "/api/v1/uploads/{upload_id}/content", upload_content, "upload.content"),
        ("POST", "/api/v1/uploads/{upload_id}/complete", upload_complete, "upload.complete"),
    )
    validate_public_business_actions(item[3] for item in business_routes)
    for method, path, handler, action_id in business_routes:
        _add_business_route(app, method, path, handler, action_id)

    app.router.add_get("/internal/v1/status", internal_status)
    app.router.add_get("/internal/v1/mcp/manifest", mcp_manifest)
    app.router.add_post("/internal/v1/tools/call", tools_call)
    app.router.add_post("/internal/v1/admin/writer-mode", writer_mode)
    app.router.add_post("/internal/v1/admin/cutover/prepare", cutover_prepare)
    app.router.add_post("/internal/v1/admin/cutover/freeze", cutover_freeze)
    app.router.add_post("/internal/v1/admin/cutover/seed", cutover_seed)
    app.router.add_post("/internal/v1/admin/cutover/ready", cutover_ready)
    app.router.add_post("/internal/v1/admin/cutover/activate", cutover_activate)
    app.router.add_post("/internal/v1/admin/cutover/suspend", cutover_suspend)
    app.router.add_get("/internal/v1/downloads/chart/{reference}", chart_download)
    app.router.add_get("/internal/v1/media/replay", media_replay)
    app.router.add_post("/internal/v1/media/ingest", media_ingest)
    app.router.add_get("/{tail:.*}", spa)
    return app


@web.middleware
async def error_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
    try:
        return await handler(request)
    except LedgerError as exc:
        return web.json_response({"error": {"code": exc.code, "message": str(exc)}}, status=exc.status)
    except web.HTTPException:
        raise
    except Exception:
        return web.json_response(
            {"error": {"code": "internal_error", "message": "装修档案操作失败，未返回私有详情。"}},
            status=500,
        )


@web.middleware
async def security_headers_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
    response = await handler(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; "
        "img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; "
        "object-src 'none'; base-uri 'none'; frame-ancestors 'self'; form-action 'self'"
    )
    return response


def _store(request: web.Request) -> RenovationHubStore:
    return request.app[STORE_KEY]


def _media(request: web.Request) -> MediaService:
    return request.app[MEDIA_KEY]


def _authorized(request: web.Request) -> None:
    expected = f"Bearer {request.app[API_TOKEN_KEY]}"
    if not hmac.compare_digest(request.headers.get("Authorization", ""), expected):
        raise web.HTTPUnauthorized(
            text=json.dumps({"error": {"code": "not_authorized"}}),
            content_type="application/json",
        )


def _cutover_authorized(request: web.Request) -> None:
    expected = request.app[CUTOVER_TOKEN_KEY]
    supplied = request.headers.get("X-Cutover-Token", "")
    if not expected:
        raise LedgerError("cutover_disabled", "未配置独立 cutover token", status=503)
    if not hmac.compare_digest(supplied, expected):
        raise LedgerError("cutover_not_authorized", "cutover token 无效", status=403)


def _require_csrf(request: web.Request) -> None:
    expected = request.app[CSRF_TOKEN_KEY]
    supplied = request.headers.get("X-CSRF-Token", "")
    cookie = request.cookies.get("renovation_hub_csrf")
    if not hmac.compare_digest(supplied, expected) or (cookie and not hmac.compare_digest(cookie, expected)):
        raise LedgerError("csrf_invalid", "页面会话已过期，请刷新后重试", status=403)
    if request.headers.get("Sec-Fetch-Site") == "cross-site":
        raise LedgerError("csrf_invalid", "拒绝跨站写请求", status=403)


def _query(request: web.Request) -> dict[str, Any]:
    result: dict[str, Any] = dict(request.query)
    if "limit" in result:
        try:
            result["limit"] = int(result["limit"])
        except ValueError as exc:
            raise LedgerError("invalid_input", "limit 必须是整数") from exc
    return result


async def _json(request: web.Request, *, internal: bool = False) -> dict[str, Any]:
    if internal:
        _authorized(request)
    if request.content_length is None or request.content_length < 1:
        raise LedgerError("request_size_invalid", "请求正文大小无效", status=400)
    try:
        result = await request.json(loads=json.loads)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise LedgerError("invalid_json", "请求 JSON 无效") from exc
    if not isinstance(result, dict):
        raise LedgerError("json_object_required", "请求必须是 JSON 对象")
    return result


async def _page_json(request: web.Request, *, idempotent: bool = True) -> dict[str, Any]:
    _require_csrf(request)
    payload = await _json(request)
    if idempotent:
        payload["idempotency_key"] = request.headers.get("Idempotency-Key", "")
    return payload


def _result(value: Any, *, status: int = 200) -> web.Response:
    return web.json_response({"version": 1, "result": value}, status=status)


async def health(request: web.Request) -> web.Response:
    return web.json_response(_store(request).status())


async def page_session(request: web.Request) -> web.Response:
    status = _store(request).status()
    token = request.app[CSRF_TOKEN_KEY]
    response = _result(
        {
            "csrf_token": token,
            "writer_mode": status["writer_mode"],
            "writable": status["writer_mode"] == "primary_writer",
            "portable_export_state": status["portable_export_state"],
        }
    )
    response.set_cookie(
        "renovation_hub_csrf",
        token,
        secure=True,
        httponly=False,
        samesite="Strict",
        path="/",
    )
    return response


async def projects(request: web.Request) -> web.Response:
    return _result({"items": _store(request).list_projects(_query(request))})


async def project_create(request: web.Request) -> web.Response:
    return _result(_store(request).create_project(await _page_json(request), actor_hash=PAGE_ACTOR), status=201)


async def project_update(request: web.Request) -> web.Response:
    payload = await _page_json(request)
    payload["project_id"] = request.match_info["project_id"]
    return _result(_store(request).update_project(payload, actor_hash=PAGE_ACTOR))


async def stages(request: web.Request) -> web.Response:
    return _result({"items": _store(request).list_stages(request.query.get("project_id", ""))})


async def stage_create(request: web.Request) -> web.Response:
    return _result(_store(request).create_stage(await _page_json(request), actor_hash=PAGE_ACTOR), status=201)


async def stage_update(request: web.Request) -> web.Response:
    payload = await _page_json(request)
    payload["stage_id"] = request.match_info["stage_id"]
    return _result(_store(request).update_stage(payload, actor_hash=PAGE_ACTOR))


async def areas(request: web.Request) -> web.Response:
    return _result({"items": _store(request).list_areas(request.query.get("project_id", ""))})


async def area_create(request: web.Request) -> web.Response:
    return _result(_store(request).create_area(await _page_json(request), actor_hash=PAGE_ACTOR), status=201)


async def area_update(request: web.Request) -> web.Response:
    payload = await _page_json(request)
    payload["area_id"] = request.match_info["area_id"]
    return _result(_store(request).update_area(payload, actor_hash=PAGE_ACTOR))


async def timeline(request: web.Request) -> web.Response:
    return _result({"items": _store(request).timeline(_query(request))})


async def event_create(request: web.Request) -> web.Response:
    return _result(_store(request).create_event(await _page_json(request), actor_hash=PAGE_ACTOR), status=201)


async def event_update(request: web.Request) -> web.Response:
    payload = await _page_json(request)
    payload["event_id"] = request.match_info["event_id"]
    return _result(_store(request).update_event(payload, actor_hash=PAGE_ACTOR))


async def dashboard(request: web.Request) -> web.Response:
    return _result(_store(request).dashboard(request.query.get("project_id", "")))


async def ledger(request: web.Request) -> web.Response:
    query = _query(request)
    return _result({"items": _store(request).query(query), "summary": _store(request).summary(query)})


async def ledger_create(request: web.Request) -> web.Response:
    return _result(_store(request).add_payment(await _page_json(request), actor_hash=PAGE_ACTOR), status=201)


async def ledger_update(request: web.Request) -> web.Response:
    payload = await _page_json(request)
    payload["payment_id"] = request.match_info["transaction_id"]
    return _result(_store(request).correct_payment(payload, actor_hash=PAGE_ACTOR))


async def ledger_refund(request: web.Request) -> web.Response:
    return _result(_store(request).add_refund(await _page_json(request), actor_hash=PAGE_ACTOR), status=201)


async def ledger_undo(request: web.Request) -> web.Response:
    payload = await _page_json(request)
    payload["transaction_id"] = request.match_info["transaction_id"]
    return _result(_store(request).undo(payload, actor_hash=PAGE_ACTOR))


async def report_summary(request: web.Request) -> web.Response:
    return _result(_store(request).summary(_query(request)))


async def search(request: web.Request) -> web.Response:
    query = _query(request)
    return _result(search_business_data(_store(request), _media(request), query))


async def media_list(request: web.Request) -> web.Response:
    return _result({"items": _media(request).list(_query(request))})


async def media_content(request: web.Request) -> web.StreamResponse:
    path, mime = _media(request).content_path(request.match_info["media_id"])
    return web.FileResponse(path, headers={"Content-Type": mime, "Accept-Ranges": "bytes"})


async def media_preview(request: web.Request) -> web.StreamResponse:
    path, mime = _media(request).content_path(request.match_info["media_id"], preview=True)
    return web.FileResponse(path, headers={"Content-Type": mime})


async def upload_create(request: web.Request) -> web.Response:
    return _result(_media(request).create_browser_upload(await _page_json(request)), status=201)


async def upload_content(request: web.Request) -> web.Response:
    _require_csrf(request)
    upload = _media(request).browser_upload(request.match_info["upload_id"])
    if upload["state"] not in {"created", "uploading"}:
        raise LedgerError("upload_state_conflict", "上传会话不可写入", status=409)
    if request.content_length is None:
        raise LedgerError("media_size_invalid", "媒体必须提供 Content-Length", status=411)
    if request.content_length != upload["expected_bytes"]:
        raise LedgerError("media_size_invalid", "媒体正文大小与上传会话不一致", status=400)
    if request.content_type != upload["mime_type"]:
        raise LedgerError("media_type_rejected", "媒体类型与上传会话不一致", status=415)
    _media(request).mark_uploading(upload["id"])
    digest = hashlib.sha256()
    received = 0
    try:
        with Path(upload["path"]).open("xb") as handle:
            async for chunk in request.content.iter_chunked(1024 * 1024):
                received += len(chunk)
                if received > upload["expected_bytes"]:
                    raise LedgerError("media_size_invalid", "媒体正文超过声明大小", status=413)
                digest.update(chunk)
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        received_sha256 = digest.hexdigest()
        if received != upload["expected_bytes"]:
            raise LedgerError("upload_incomplete", "媒体上传不完整", status=400)
        if received_sha256 != upload["expected_sha256"]:
            raise LedgerError("sha256_mismatch", "媒体摘要不一致", status=400)
        _media(request).mark_browser_uploaded(
            upload["id"],
            received_bytes=received,
            received_sha256=received_sha256,
        )
    except Exception:
        _media(request).fail_upload(upload["id"], "upload_failed")
        raise
    return _result({"upload_id": upload["id"], "state": "uploaded", "received_bytes": received})


async def upload_complete(request: web.Request) -> web.Response:
    _require_csrf(request)
    return _result(
        await asyncio.to_thread(
            _media(request).complete_browser_upload,
            request.match_info["upload_id"],
            actor_hash=PAGE_ACTOR,
        )
    )


async def internal_status(request: web.Request) -> web.Response:
    _authorized(request)
    return _result(_store(request).status())


async def chart_download(request: web.Request) -> web.StreamResponse:
    """Serve one authenticated chart artifact to the Controller."""
    _authorized(request)
    reference = request.match_info["reference"]
    if not CHART_REFERENCE_RE.fullmatch(reference):
        return web.json_response(
            {"error": {"code": "invalid_reference"}},
            status=400,
        )

    charts_dir = _store(request).charts_dir.resolve()
    target = (charts_dir / reference).resolve()
    try:
        target.relative_to(charts_dir)
    except ValueError:
        return web.json_response(
            {"error": {"code": "invalid_reference"}},
            status=400,
        )
    if not target.is_file():
        return web.json_response(
            {"error": {"code": "not_found"}},
            status=404,
        )
    return web.FileResponse(target, headers={"Content-Type": "image/png"})


async def mcp_manifest(request: web.Request) -> web.Response:
    _authorized(request)
    manifest = business_manifest()
    return web.json_response(
        manifest,
        headers={"ETag": f'"{manifest["catalog_digest"]}"'},
    )


async def tools_call(request: web.Request) -> web.Response:
    payload = await _json(request, internal=True)
    return _result(dispatch_tool(_store(request), payload, media=_media(request)))


async def writer_mode(request: web.Request) -> web.Response:
    payload = await _json(request, internal=True)
    target = payload.get("target")
    if target == "suspended":
        return _result(_store(request).suspend_writer(str(payload.get("reason") or "admin_suspend")))
    if target == "read_only" and _store(request).writer_mode() == "read_only":
        return _result({"previous": "read_only", "current": "read_only"})
    raise LedgerError("cutover_manifest_required", "writer 状态只能通过 cutover manifest 推进", status=409)


async def _cutover_payload(request: web.Request) -> dict[str, Any]:
    payload = await _json(request, internal=True)
    _cutover_authorized(request)
    return payload


async def cutover_prepare(request: web.Request) -> web.Response:
    payload = await _cutover_payload(request)
    return _result(
        _store(request).prepare_primary_migration(
            str(payload.get("path") or ""),
            payload.get("evidence") or {},
        )
    )


async def cutover_freeze(request: web.Request) -> web.Response:
    payload = await _cutover_payload(request)
    return _result(
        _store(request).mark_source_frozen(
            str(payload.get("manifest_id") or ""), payload.get("evidence") or {}
        )
    )


async def cutover_seed(request: web.Request) -> web.Response:
    payload = await _cutover_payload(request)
    return _result(_store(request).seed_primary(str(payload.get("manifest_id") or "")))


async def cutover_ready(request: web.Request) -> web.Response:
    payload = await _cutover_payload(request)
    return _result(
        _store(request).mark_cutover_ready(
            str(payload.get("manifest_id") or ""), payload.get("evidence") or {}
        )
    )


async def cutover_activate(request: web.Request) -> web.Response:
    payload = await _cutover_payload(request)
    return _result(
        _store(request).activate_primary_writer(
            str(payload.get("manifest_id") or ""),
            str(payload.get("confirmation") or ""),
        )
    )


async def cutover_suspend(request: web.Request) -> web.Response:
    payload = await _cutover_payload(request)
    return _result(_store(request).suspend_writer(str(payload.get("reason") or "admin_suspend")))


async def media_replay(request: web.Request) -> web.Response:
    _authorized(request)
    result = _media(request).replay(
        request.query.get("idempotency_key", ""),
        request.query.get("source_ref_hash", ""),
    )
    if result is None:
        raise web.HTTPNotFound(
            text=json.dumps({"error": {"code": "not_found"}}),
            content_type="application/json",
        )
    return _result(result)


def _decode_header(value: str, field: str, maximum: int) -> str:
    try:
        decoded = base64.urlsafe_b64decode(value.encode("ascii")).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise LedgerError("media_invalid", f"{field} 头无效") from exc
    if not decoded or len(decoded) > maximum:
        raise LedgerError("media_invalid", f"{field} 头无效")
    return decoded


async def media_ingest(request: web.Request) -> web.Response:
    _authorized(request)
    definition = get_business_tool("renovation_media_ingest")
    if definition.transport != "gateway_media_stream":
        raise LedgerError("registry_invalid", "媒体工具传输契约无效", status=500)
    filename = _decode_header(request.headers.get("X-Attachment-Filename", ""), "filename", 255)
    metadata_text = _decode_header(request.headers.get("X-Renovation-Metadata", ""), "metadata", 8192)
    try:
        metadata = json.loads(metadata_text)
    except json.JSONDecodeError as exc:
        raise LedgerError("media_invalid", "媒体元数据无效") from exc
    if not isinstance(metadata, dict):
        raise LedgerError("media_invalid", "媒体元数据必须是对象")
    expected_bytes = request.content_length
    if expected_bytes is None:
        raise LedgerError("media_size_invalid", "媒体必须提供 Content-Length", status=411)
    expected_digest = request.headers.get("X-Attachment-Sha256", "")
    if expected_digest.startswith("sha256:"):
        expected_digest = expected_digest.split(":", 1)[1]
    if len(expected_digest) != 64:
        raise LedgerError("media_invalid", "媒体摘要头无效")
    prepared = _media(request).prepare_upload(
        idempotency_key=metadata.get("idempotency_key"),
        source_ref_hash=metadata.get("source_ref_hash"),
        original_filename=filename,
        mime_type=request.content_type,
        expected_bytes=expected_bytes,
    )
    if prepared["replay"]:
        result = dict(prepared["result"])
        result["idempotent_replay"] = True
        return _result(result)
    upload_id = prepared["upload_id"]
    _media(request).mark_uploading(upload_id)
    digest = hashlib.sha256()
    received = 0
    try:
        with Path(prepared["path"]).open("xb") as handle:
            async for chunk in request.content.iter_chunked(1024 * 1024):
                received += len(chunk)
                if received > prepared["expected_bytes"]:
                    raise LedgerError("media_size_invalid", "媒体正文超过声明大小", status=413)
                digest.update(chunk)
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        _media(request).fail_upload(upload_id, "upload_failed")
        raise
    metadata["idempotency_key"] = metadata.get("idempotency_key")
    metadata["source_ref_hash"] = metadata.get("source_ref_hash")
    result = await asyncio.to_thread(
        _media(request).finalize_upload,
        prepared,
        received_bytes=received,
        sha256=digest.hexdigest(),
        expected_sha256=expected_digest,
        metadata=metadata,
        actor_hash="sha256:codex-controller",
    )
    return _result(result)


async def spa(request: web.Request) -> web.StreamResponse:
    static_dir = request.app[STATIC_DIR_KEY]
    if isinstance(static_dir, Path):
        requested = request.match_info.get("tail", "")
        candidate = (static_dir / requested).resolve()
        try:
            candidate.relative_to(static_dir.resolve())
        except ValueError:
            raise web.HTTPNotFound()
        if candidate.is_file():
            return web.FileResponse(candidate)
        index = static_dir / "index.html"
        if index.is_file():
            return web.FileResponse(index)
    return web.Response(
        text=render_dashboard(_store(request).status()),
        content_type="text/html",
        charset="utf-8",
    )
