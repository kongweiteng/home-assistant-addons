"""Controller orchestration for authentication, queue scheduling and app-server events."""

from __future__ import annotations

import re
import threading
import time
from typing import Any

from .app_server import AppServerClient, AppServerError
from .media_input import MediaInputError, TurnMediaManager
from .store import ControllerStore, StoreError


NEW_THREAD_COMMANDS = frozenset({"打开新会话", "/new"})
NEW_THREAD_RESULT = "新会话已建立。下一条消息将使用当前 Codex 配置和工具目录。"
MEDIA_ARCHIVE_ACTION_RE = re.compile(r"(?:归档|存档|保存|添加|加入|关联|记录)")
MEDIA_ARCHIVE_TARGET_RE = re.compile(r"(?:装修|施工|工地|现场).{0,12}(?:档案|记录|媒体库|资料库)")
MEDIA_ARCHIVE_MEDIA_ACTION_RE = re.compile(
    r"(?:装修|施工|工地|现场).{0,12}(?:照片|图片|视频|媒体).{0,8}(?:归档|存档|归入|收录|记录)"
    r"|(?:归档|存档|归入|收录|记录).{0,8}(?:装修|施工|工地|现场).{0,12}(?:照片|图片|视频|媒体)"
)
MEDIA_ARCHIVE_NEGATION_RE = re.compile(r"(?:不要|别|无需|不用|不需要).{0,12}(?:归档|存档|保存|添加|加入|关联|记录)")


def has_explicit_media_archive_intent(text: str, attachments: list[dict[str, Any]] | None = None) -> bool:
    """Allow media writes only for an explicit positive renovation-archive request."""

    if not isinstance(text, str) or not text.strip() or not attachments:
        return False
    normalized = re.sub(r"\s+", "", text)
    if MEDIA_ARCHIVE_NEGATION_RE.search(normalized):
        return False
    return bool(
        MEDIA_ARCHIVE_ACTION_RE.search(normalized)
        and (MEDIA_ARCHIVE_TARGET_RE.search(normalized) or MEDIA_ARCHIVE_MEDIA_ACTION_RE.search(normalized))
    )


def is_new_thread_command(payload: dict[str, Any]) -> bool:
    """Recognize the exact attachment-free user control command."""
    text = payload.get("text")
    attachments = payload.get("attachments")
    return isinstance(text, str) and text.strip() in NEW_THREAD_COMMANDS and attachments == []


class ControllerService:
    AUTH_MODES = {"chatgpt_device_code", "api_key"}

    def __init__(
        self,
        store: ControllerStore,
        app_server: AppServerClient,
        *,
        intake_enabled: bool,
        auth_mode: str = "chatgpt_device_code",
        api_key: str = "",
        api_base_mode: str = "official",
        codex_model_mode: str = "default",
        turn_media: TurnMediaManager | None = None,
        tool_context: Any | None = None,
    ):
        if auth_mode not in self.AUTH_MODES:
            raise ValueError("Controller auth_mode 不受支持")
        if api_base_mode not in {"official", "custom"}:
            raise ValueError("Controller api_base_mode 不受支持")
        if codex_model_mode not in {"default", "custom"}:
            raise ValueError("Controller codex_model_mode 不受支持")
        self.store = store
        self.app_server = app_server
        self.configured_intake_enabled = intake_enabled
        self.configured_auth_mode = auth_mode
        self._api_key = api_key if auth_mode == "api_key" else ""
        self.api_base_mode = api_base_mode
        self.codex_model_mode = codex_model_mode
        self.turn_media = turn_media
        self.tool_context = tool_context
        self.auth_error: str | None = None
        self.pending_login: dict[str, Any] | None = None
        self.start_error: str | None = None
        self._stop = threading.Event()
        self._scheduler: threading.Thread | None = None
        self._pending_turn_events: dict[str, list[dict[str, Any]]] = {}
        self._event_lock = threading.Lock()
        self.app_server.notification_handler = self.handle_notification

    def start(self) -> None:
        self.store.recover_running()
        try:
            self.app_server.start()
        except AppServerError as exc:
            self.start_error = exc.code
        else:
            self._reconcile_initial_auth()
        self._scheduler = threading.Thread(target=self._scheduler_loop, name="codex-controller-scheduler", daemon=True)
        self._scheduler.start()

    def stop(self) -> None:
        self._stop.set()
        self.app_server.stop()

    def submit(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.intake_enabled:
            raise StoreError("intake_disabled", "正式任务入口尚未启用", status=409)
        return self.store.public_job(self.store.create_job(payload))

    def capabilities(self) -> dict[str, Any]:
        return {
            "capabilities": [
                "job_capability_profile_v1",
                "thread_short_v1",
                "mcp_tool_policy_v1",
                "job_artifacts_v1",
            ],
        }

    def tool_status(self) -> dict[str, Any]:
        if (
            self.tool_context is not None
            and getattr(self.tool_context, "store", None) is not None
            and hasattr(self.tool_context, "tool_status")
        ):
            return self.tool_context.tool_status()
        configured = (
            self.tool_context.configured_tools()
            if self.tool_context is not None and hasattr(self.tool_context, "configured_tools")
            else frozenset()
        )
        callable_names = (
            self.tool_context.route_ready_tools()
            if self.tool_context is not None and hasattr(self.tool_context, "route_ready_tools")
            else configured
        )
        return self.store.tool_control_document(configured, callable_names)

    def update_tool_policy(
        self,
        tool_name: str,
        *,
        enabled: bool,
        revision: int,
        request_id: str,
    ) -> dict[str, Any]:
        if (
            self.tool_context is not None
            and getattr(self.tool_context, "store", None) is not None
            and hasattr(self.tool_context, "update_tool_policy")
        ):
            return self.tool_context.update_tool_policy(
                tool_name,
                enabled=enabled,
                revision=revision,
                request_id=request_id,
            )
        return self.store.update_tool_policy(
            tool_name,
            enabled=enabled,
            revision=revision,
            request_id=request_id,
        )

    def begin_device_login(self) -> dict[str, Any]:
        if self.configured_auth_mode != "chatgpt_device_code":
            raise AppServerError("auth_mode_rejected", "当前 options 未选择设备码登录", definitive=True)
        login = self.app_server.start_device_login()
        self.pending_login = login
        self.auth_error = None
        return login

    def begin_api_key_login(self) -> dict[str, Any]:
        if self.configured_auth_mode != "api_key":
            raise AppServerError("auth_mode_rejected", "当前 options 未选择 API Key 登录", definitive=True)
        if not self._api_key:
            self.auth_error = "api_key_missing"
            raise AppServerError("api_key_missing", "请先在 Add-on options 中配置 API Key", definitive=True)
        try:
            result = self.app_server.start_api_key_login(self._api_key)
        except AppServerError as exc:
            self.auth_error = exc.code
            raise
        self.auth_error = None
        self.pending_login = None
        return result

    def cancel_device_login(self) -> dict[str, Any]:
        if self.pending_login is None:
            raise AppServerError("login_not_pending", "没有待完成的设备码登录", definitive=True)
        login_id = self.pending_login["loginId"]
        self.app_server.cancel_login(login_id)
        self.pending_login = None
        return {"cancelled": True}

    def logout(self) -> dict[str, Any]:
        self.app_server.logout()
        self.pending_login = None
        return {"logged_out": True}

    def status(self) -> dict[str, Any]:
        app = self.app_server.status()
        tool_status = self.tool_status()
        effective_names = [tool["name"] for tool in tool_status["tools"] if tool["callable"]]
        if self._account_matches(app):
            self.pending_login = None
        return {
            "version": "0.2.4",
            "codex_version": "0.146.0",
            "configured_auth_mode": self.configured_auth_mode,
            "api_key_configured": bool(self._api_key),
            "api_base_mode": self.api_base_mode,
            "api_base_configured": self.api_base_mode == "custom",
            "api_base_error": None,
            "codex_model_mode": self.codex_model_mode,
            "auth_error": self.auth_error,
            "intake_configured": self.configured_intake_enabled,
            "intake_enabled": self._intake_enabled(app),
            "ready": bool(
                self._app_server_operational(app)
                and self._account_matches(app)
                and self.start_error is None
                and self.auth_error is None
            ),
            "start_error": self.start_error,
            "app_server": app,
            "tools": {
                **tool_status["summary"],
                "count": len(effective_names),
                "names": effective_names,
                "renovation_hub": any(
                    name.startswith("ledger_") or name.startswith("renovation_")
                    for name in effective_names
                ),
                "operations": any(name.startswith("ha_operations_") for name in effective_names),
                "mcp": tool_status["mcp"],
                "policy_error": tool_status["policy_error"],
                "hub_manifest": tool_status.get("hub_manifest"),
            },
            "pending_login": self.pending_login,
            "queue": self.store.status(),
        }

    def handle_notification(self, message: dict[str, Any], *, allow_buffer: bool = True) -> None:
        method = message.get("method")
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        if method == "item/completed":
            item = params.get("item") if isinstance(params.get("item"), dict) else {}
            turn_id = params.get("turnId")
            if isinstance(turn_id, str) and item.get("type") == "agentMessage" and isinstance(item.get("text"), str):
                handled = self.store.set_result_text(turn_id, item["text"], item_type="agentMessage")
                if not handled and allow_buffer:
                    self._buffer_event(turn_id, message)
        elif method == "turn/completed":
            turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
            turn_id = turn.get("id") or params.get("turnId")
            status = turn.get("status")
            if isinstance(turn_id, str) and isinstance(status, str):
                if allow_buffer:
                    with self._event_lock:
                        pending = self._pending_turn_events.get(turn_id)
                        if pending:
                            if len(pending) < 32:
                                pending.append(message)
                            return
                if self.tool_context is not None:
                    self.tool_context.end_turn(turn_id)
                if self.turn_media is not None:
                    self.turn_media.cleanup_turn(turn_id)
                error = turn.get("error") if isinstance(turn.get("error"), dict) else {}
                error_code = "turn_failed" if status == "failed" else None
                if error.get("codexErrorInfo") == "contextWindowExceeded":
                    error_code = "context_window_exceeded"
                handled = self.store.complete_turn(turn_id, status, error_code=error_code)
                if not handled and allow_buffer:
                    self._buffer_event(turn_id, message)
        elif method == "account/updated":
            expected = "chatgpt" if self.configured_auth_mode == "chatgpt_device_code" else "apikey"
            actual = params.get("authMode")
            if actual is None:
                self.auth_error = None
            elif actual != expected:
                self.auth_error = "auth_mode_mismatch"
            else:
                self.auth_error = None

    def _scheduler_loop(self) -> None:
        next_artifact_cleanup = 0.0
        while not self._stop.wait(0.5):
            if time.monotonic() >= next_artifact_cleanup:
                try:
                    self.store.cleanup_artifacts()
                except (StoreError, OSError):
                    pass
                next_artifact_cleanup = time.monotonic() + 60
            if not self.intake_enabled or self.start_error is not None:
                continue
            job = self.store.claim_next()
            if job is None:
                continue
            self._dispatch(job)

    def _dispatch(self, job: dict[str, Any]) -> None:
        turn_started = False
        try:
            payload = job["input"]
            capability_profile = payload.get("capability_profile", "owner_legacy")
            media_archive_authorized = has_explicit_media_archive_intent(
                payload.get("text", ""), payload.get("attachments")
            )
            effective_tools = (
                self.tool_context.available_tools(
                    capability_profile,
                    media_archive_authorized=media_archive_authorized,
                )
                if self.tool_context is not None
                else []
            )
            if hasattr(self.app_server, "configure_developer_context"):
                if (
                    getattr(self.app_server, "supports_dynamic_tool_definitions", False)
                    and self.tool_context is not None
                    and hasattr(self.tool_context, "tool_definitions_by_name")
                ):
                    self.app_server.configure_developer_context(
                        effective_tools,
                        capability_profile,
                        self.tool_context.tool_definitions_by_name(),
                    )
                else:
                    self.app_server.configure_developer_context(effective_tools, capability_profile)
            if is_new_thread_command(payload):
                thread_id = self.app_server.start_thread()
                thread_short = self.store.short_id("TH", thread_id)
                result = f"{NEW_THREAD_RESULT}\nThread：{thread_short}"
                self.store.complete_new_thread(job["job_id"], thread_id, result)
                return
            if self.tool_context is not None:
                self.tool_context.begin_job(
                    job["job_id"],
                    payload["message_id"],
                    capability_profile,
                    media_archive_authorized=media_archive_authorized,
                )
            thread_id = self.store.conversation_thread(job["conversation_key"])
            if thread_id is None:
                thread_id = self.app_server.start_thread()
                self.store.assign_thread(job["job_id"], thread_id)
            else:
                loaded_thread_id = self.app_server.resume_thread(thread_id)
                if isinstance(loaded_thread_id, str) and loaded_thread_id:
                    thread_id = loaded_thread_id
                self.store.assign_thread(job["job_id"], thread_id)
            input_items = None
            if self.turn_media is not None:
                input_items = self.turn_media.prepare(job["job_id"], payload)
            turn_id = self.app_server.start_turn(
                thread_id,
                payload["text"],
                job["message_id"],
                input_items=input_items,
            )
            if self.tool_context is not None:
                self.tool_context.bind_turn(job["job_id"], turn_id)
            turn_started = True
            if self.turn_media is not None:
                self.turn_media.bind_turn(job["job_id"], turn_id)
            self.store.assign_turn(job["job_id"], turn_id)
            self._flush_turn_events(turn_id)
        except AppServerError as exc:
            if exc.code == "app_server_overloaded" and exc.definitive and self.store.retry_overloaded(job["job_id"]):
                time.sleep(min(2 ** max(job["attempt"], 1), 8))
                return
            self.store.fail_claimed(job["job_id"], exc.code, uncertain=not exc.definitive)
        except StoreError as exc:
            self.store.fail_claimed(job["job_id"], exc.code, uncertain=True)
        except MediaInputError as exc:
            self.store.fail_claimed(job["job_id"], exc.code, uncertain=False)
        except Exception:
            self.store.fail_claimed(job["job_id"], "controller_internal_error", uncertain=True)
        finally:
            if not turn_started:
                if self.tool_context is not None:
                    self.tool_context.clear_job(job["job_id"])
                if self.turn_media is not None:
                    self.turn_media.cleanup_job(job["job_id"])

    def _buffer_event(self, turn_id: str, message: dict[str, Any]) -> None:
        with self._event_lock:
            events = self._pending_turn_events.setdefault(turn_id, [])
            if len(events) < 32:
                events.append(message)

    def _flush_turn_events(self, turn_id: str) -> None:
        with self._event_lock:
            events = self._pending_turn_events.pop(turn_id, [])
        events.sort(key=lambda message: message.get("method") == "turn/completed")
        for message in events:
            self.handle_notification(message, allow_buffer=False)

    @property
    def intake_enabled(self) -> bool:
        return self._intake_enabled()

    def watchdog_healthy(self, app_status: dict[str, Any] | None = None) -> bool:
        return bool(self.start_error is None and self._app_server_operational(app_status))

    def _intake_enabled(self, app_status: dict[str, Any] | None = None) -> bool:
        return bool(
            self.configured_intake_enabled
            and self._app_server_operational(app_status)
            and self.app_server.account_ready
            and self._account_matches(app_status)
            and self.auth_error is None
        )

    def _app_server_operational(self, app_status: dict[str, Any] | None = None) -> bool:
        app = self.app_server.status() if app_status is None else app_status
        return bool(
            app.get("running") is True
            and app.get("initialized") is True
            and app.get("protocol_error") is None
        )

    def _expected_account_type(self) -> str:
        return "chatgpt" if self.configured_auth_mode == "chatgpt_device_code" else "apiKey"

    def _account_matches(self, app_status: dict[str, Any] | None = None) -> bool:
        if app_status is None:
            actual = getattr(self.app_server, "auth_mode", None)
        else:
            account = app_status.get("account") if isinstance(app_status, dict) else None
            actual = account.get("auth_mode") if isinstance(account, dict) else None
        return actual == self._expected_account_type()

    def _reconcile_initial_auth(self) -> None:
        if self.configured_auth_mode == "chatgpt_device_code":
            if self.app_server.account_ready and not self._account_matches():
                self.auth_error = "auth_mode_mismatch"
            return
        if not self._api_key:
            self.auth_error = "api_key_missing"
            return
        if self.app_server.account_ready:
            self.auth_error = None if self._account_matches() else "auth_mode_mismatch"
            return
        try:
            self.app_server.start_api_key_login(self._api_key)
        except AppServerError as exc:
            self.auth_error = exc.code
        else:
            self.auth_error = None
