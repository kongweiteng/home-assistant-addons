"""Owner-only persistent model selection against the current trusted catalog."""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
from datetime import datetime
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Callable
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo


DEFAULT_CODEX_MODEL = "gpt-6.1-sol"
MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
MAX_CATALOG_BYTES = 256 * 1024


class ModelControlError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: Any, msg: Any, headers: Any, newurl: Any) -> None:
        return None


def backend_catalog_provider(base_url: str, api_key: str) -> Callable[[], dict[str, Any]]:
    """Use only main's validated provider URL and in-memory key; never follow redirects."""
    endpoint = base_url.rstrip("/") + "/models"

    def fetch() -> dict[str, Any]:
        if not api_key:
            raise ModelControlError("model_catalog_unavailable", "当前后端模型目录不可用，未修改默认模型")
        try:
            request = Request(endpoint, headers={"Authorization": "Bearer " + api_key, "Accept": "application/json"})
            with build_opener(_NoRedirect()).open(request, timeout=15) as response:
                body = response.read(MAX_CATALOG_BYTES + 1)
            if len(body) > MAX_CATALOG_BYTES:
                raise ValueError("catalog size")
            document = json.loads(body)
            if not isinstance(document, dict):
                raise ValueError("catalog shape")
            # A malformed backend response must not turn a credential into a
            # model ID visible in the tool result. Extra backend metadata is
            # discarded by ModelManager rather than forwarded to the model.
            for item in document.get("data", []):
                if isinstance(item, dict) and isinstance(item.get("id"), str) and api_key in item["id"]:
                    raise ValueError("catalog credential")
            return document
        except Exception as error:
            if isinstance(error, HTTPError):
                error.close()
            raise ModelControlError("model_catalog_unavailable", "当前后端模型目录读取失败，未修改默认模型") from None

    return fetch


class ModelManager:
    def __init__(
        self,
        database_path: str | Path,
        app_server: Any,
        *,
        configured_model: str = "",
        catalog_provider: Callable[[], dict[str, Any]] | None = None,
        fallback_model: str = DEFAULT_CODEX_MODEL,
    ):
        self.database_path = str(database_path)
        self.app_server = app_server
        self.configured_model = configured_model
        self.fallback_model = fallback_model
        self.catalog_provider = catalog_provider
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS controller_model_setting (
                    singleton INTEGER PRIMARY KEY CHECK (singleton=1),
                    model TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS controller_model_changes (
                    conversation_key TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    model TEXT NOT NULL,
                    previous_model TEXT NOT NULL,
                    changed_at TEXT NOT NULL,
                    PRIMARY KEY (conversation_key, message_id)
                );
            """)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=15)
        connection.row_factory = sqlite3.Row
        return connection

    def get_default(self) -> str:
        try:
            with self._connect() as connection:
                row = connection.execute("SELECT model FROM controller_model_setting WHERE singleton=1").fetchone()
            return row["model"] if row else self.configured_model or self.fallback_model
        except sqlite3.Error:
            raise ModelControlError("model_setting_unavailable", "默认模型配置暂不可读取") from None

    def list_models(self) -> dict[str, Any]:
        try:
            if self.catalog_provider is not None:
                document = self.catalog_provider()
                rows = document.get("data")
                source = "current_backend_models"
            else:
                rows = []
                cursor = None
                seen: set[str] = set()
                for _ in range(20):
                    params = {"limit": 100, "includeHidden": False}
                    if cursor is not None:
                        params["cursor"] = cursor
                    page = self.app_server.request("model/list", params)
                    page_rows = page.get("data") if isinstance(page, dict) else None
                    if not isinstance(page_rows, list):
                        raise ValueError("catalog shape")
                    rows.extend(page_rows)
                    cursor = page.get("nextCursor")
                    if not cursor:
                        break
                    if not isinstance(cursor, str) or cursor in seen:
                        raise ValueError("catalog cursor")
                    seen.add(cursor)
                else:
                    raise ValueError("catalog pages")
                source = "codex_runtime_model_list"
            if not isinstance(rows, list) or not 0 < len(rows) <= 2000:
                raise ValueError("catalog size")
            models = set()
            for item in rows:
                if not isinstance(item, dict):
                    raise ValueError("catalog row")
                # Runtime's model field is the turn/start slug; id may be a catalog identity.
                model = item.get("id") if self.catalog_provider else item.get("model", item.get("id"))
                if isinstance(model, str) and MODEL_ID_RE.fullmatch(model):
                    models.add(model)
            if not models:
                raise ValueError("catalog empty")
            return {"models": sorted(models), "default_model": self.get_default(), "catalog_source": source,
                    "verified_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
                    "availability_note": "目录列出不等于推理请求已成功验证"}
        except Exception:
            raise ModelControlError("model_catalog_unavailable", "当前后端模型目录不可验证，未修改默认模型") from None

    def call(
        self, name: str, arguments: dict[str, Any], context: dict[str, Any] | None,
        *, authorization_lock: Any = None, context_is_current: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        if context is None or context.get("capability_profile") != "owner":
            raise ModelControlError("tool_not_allowed_for_profile", "仅已绑定所有者可管理默认模型")
        if not isinstance(arguments, dict):
            raise ModelControlError("invalid_arguments", "模型工具参数无效")

        @contextmanager
        def authorized():
            # Only local database work holds the router's lifecycle lock. In
            # particular, model/list must leave the app-server reader free to
            # process a terminal notification before its RPC response.
            with authorization_lock if authorization_lock is not None else nullcontext():
                if context_is_current is not None and not context_is_current():
                    raise ModelControlError("tool_context_unavailable", "模型设置上下文已失效，未执行")
                yield

        if name == "controller_model_list":
            if arguments:
                raise ModelControlError("invalid_arguments", "模型目录查询不接受参数")
            with authorized():
                pass
            result = self.list_models()
            with authorized():
                return result
        if name != "controller_model_set_default":
            raise ModelControlError("unknown_tool", "模型工具不存在")
        model = arguments.get("model")
        if set(arguments) != {"model"} or not isinstance(model, str) or not MODEL_ID_RE.fullmatch(model):
            raise ModelControlError("model_invalid", "模型须为目录中的精确型号，不接受 URL、配置或凭据")
        if not all(isinstance(context.get(key), str) and context[key] for key in ("job_id", "message_id", "conversation_key")):
            raise ModelControlError("tool_context_unavailable", "切换模型需要当前微信消息上下文")
        key = (context["conversation_key"], context["message_id"])
        try:
            with authorized():
                with self._connect() as connection:
                    existing = connection.execute("SELECT * FROM controller_model_changes WHERE conversation_key=? AND message_id=?", key).fetchone()
                if existing:
                    return self._receipt(existing, model, replayed=True)
            catalog = self.list_models()
            if model not in catalog["models"]:
                raise ModelControlError("model_not_available", "当前后端目录未列出该精确型号，未修改默认模型")
            now = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()
            with authorized():
                with self._connect() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    existing = connection.execute("SELECT * FROM controller_model_changes WHERE conversation_key=? AND message_id=?", key).fetchone()
                    if existing:
                        return self._receipt(existing, model, replayed=True)
                    row = connection.execute("SELECT model FROM controller_model_setting WHERE singleton=1").fetchone()
                    previous = row["model"] if row else self.configured_model or self.fallback_model
                    connection.execute("INSERT INTO controller_model_changes VALUES (?,?,?,?,?)", (*key, model, previous, now))
                    connection.execute("INSERT INTO controller_model_setting VALUES (1,?,?) ON CONFLICT(singleton) DO UPDATE SET model=excluded.model,updated_at=excluded.updated_at", (model, now))
            return {"selected_model": model, "previous_model": previous, "default_model": model,
                    "effective": "next_turn", "changed_at": now, "replayed": False,
                    "catalog_source": catalog["catalog_source"]}
        except sqlite3.Error:
            raise ModelControlError("model_setting_unavailable", "默认模型保存结果需核对，请勿重新发送切换请求") from None

    def _receipt(self, row: sqlite3.Row, model: str, *, replayed: bool) -> dict[str, Any]:
        if row["model"] != model:
            raise ModelControlError("model_request_conflict", "同一条微信消息已处理其他模型设置，未再次修改")
        return {"selected_model": row["model"], "previous_model": row["previous_model"],
                "default_model": self.get_default(), "effective": "next_turn", "changed_at": row["changed_at"], "replayed": replayed}
