"""Controller orchestration for authentication, queue scheduling and app-server events."""

from __future__ import annotations

import threading
import time
from typing import Any

from .app_server import AppServerClient, AppServerError
from .store import ControllerStore, StoreError


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
        return self.store.create_job(payload)

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
        if self._account_matches(app):
            self.pending_login = None
        return {
            "version": "0.1.2",
            "codex_version": "0.146.0",
            "configured_auth_mode": self.configured_auth_mode,
            "api_key_configured": bool(self._api_key),
            "api_base_mode": self.api_base_mode,
            "api_base_configured": self.api_base_mode == "custom",
            "api_base_error": None,
            "codex_model_mode": self.codex_model_mode,
            "auth_error": self.auth_error,
            "intake_configured": self.configured_intake_enabled,
            "intake_enabled": self.intake_enabled,
            "ready": bool(
                app["running"]
                and app["initialized"]
                and self._account_matches(app)
                and self.start_error is None
                and self.auth_error is None
            ),
            "start_error": self.start_error,
            "app_server": app,
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
        while not self._stop.wait(0.5):
            if not self.intake_enabled or self.start_error is not None:
                continue
            job = self.store.claim_next()
            if job is None:
                continue
            self._dispatch(job)

    def _dispatch(self, job: dict[str, Any]) -> None:
        try:
            payload = job["input"]
            thread_id = self.store.conversation_thread(job["conversation_key"])
            if thread_id is None:
                thread_id = self.app_server.start_thread()
                self.store.assign_thread(job["job_id"], thread_id)
            else:
                self.app_server.resume_thread(thread_id)
                self.store.assign_thread(job["job_id"], thread_id)
            turn_id = self.app_server.start_turn(thread_id, payload["text"], job["message_id"])
            self.store.assign_turn(job["job_id"], turn_id)
            self._flush_turn_events(turn_id)
        except AppServerError as exc:
            if exc.code == "app_server_overloaded" and exc.definitive and self.store.retry_overloaded(job["job_id"]):
                time.sleep(min(2 ** max(job["attempt"], 1), 8))
                return
            self.store.fail_claimed(job["job_id"], exc.code, uncertain=not exc.definitive)
        except StoreError as exc:
            self.store.fail_claimed(job["job_id"], exc.code, uncertain=True)
        except Exception:
            self.store.fail_claimed(job["job_id"], "controller_internal_error", uncertain=True)

    def _buffer_event(self, turn_id: str, message: dict[str, Any]) -> None:
        with self._event_lock:
            events = self._pending_turn_events.setdefault(turn_id, [])
            if len(events) < 32:
                events.append(message)

    def _flush_turn_events(self, turn_id: str) -> None:
        with self._event_lock:
            events = self._pending_turn_events.pop(turn_id, [])
        for message in events:
            self.handle_notification(message, allow_buffer=False)

    @property
    def intake_enabled(self) -> bool:
        return bool(
            self.configured_intake_enabled
            and self.app_server.account_ready
            and self._account_matches()
            and self.auth_error is None
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
