"""Polling, account isolation, freshness and manual-refresh coordination."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import logging
import threading
import time

from .cache import CacheStore
from .client import ENDPOINT_PATHS, HuaxinClient, UpstreamError
from .config import AccountConfig, AppConfig
from .normalize import ContractError, normalize_response
from .mqtt import mqtt_state_from_account
from .statistics import build_statistics


LOGGER = logging.getLogger(__name__)
UTC = timezone.utc


class WaterService:
    def __init__(
        self,
        config: AppConfig,
        client: HuaxinClient,
        cache: CacheStore,
        publisher=None,
    ) -> None:
        self.config = config
        self.client = client
        self.cache = cache
        self.publisher = publisher
        self._lock = threading.RLock()
        self._busy: set[str] = set()
        self._last_manual_refresh: dict[str, float] = {}
        self._state = self._reconcile(cache.load())
        for account in self.config.accounts:
            self._publish_account(account.account_id)

    def run_forever(self, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            self.run_once()
            stop_event.wait(self.config.poll_interval_seconds)

    def run_once(self) -> None:
        for account in self.config.accounts:
            with self._lock:
                suspended = self._state["accounts"][account.account_id].get(
                    "auth_required", False
                )
            if not suspended:
                self._refresh_account(account)

    def request_refresh(self, account_id: str) -> tuple[bool, str, int]:
        account = self.config.account(account_id)
        if account is None:
            return False, "not_found", 0
        current = time.monotonic()
        with self._lock:
            if account_id in self._busy:
                return False, "already_refreshing", 1
            elapsed = current - self._last_manual_refresh.get(account_id, float("-inf"))
            if elapsed < self.config.manual_refresh_cooldown_seconds:
                remaining = int(self.config.manual_refresh_cooldown_seconds - elapsed) + 1
                return False, "cooldown", remaining
            self._last_manual_refresh[account_id] = current
            self._busy.add(account_id)
            self._state["accounts"][account_id]["refreshing"] = True
            self._state["accounts"][account_id]["auth_required"] = False
        thread = threading.Thread(
            target=self._refresh_account,
            args=(account,),
            kwargs={"preclaimed": True},
            daemon=True,
            name=f"refresh-{account_id}",
        )
        thread.start()
        return True, "accepted", 0

    def health(self) -> dict:
        with self._lock:
            statuses = [
                account.get("status", "starting")
                for account in self._state["accounts"].values()
            ]
            status = _service_status(statuses)
            mqtt_connected = self.publisher is None or self.publisher.connected
            if not mqtt_connected and status == "good":
                status = "degraded"
            return {
                "service": "huaxin_water",
                "version": "0.3.2",
                "status": status,
                "configured_accounts": len(statuses),
                "refreshing_accounts": len(self._busy),
                "updated_at": self._state.get("updated_at"),
                "insecure_http_enabled": self.config.base_url.startswith("http://"),
                "mqtt_connected": mqtt_connected,
            }

    def accounts_snapshot(self) -> dict:
        with self._lock:
            accounts = [
                self._public_account(self._state["accounts"][account.account_id], False)
                for account in self.config.accounts
            ]
        return {"accounts": accounts}

    def account_snapshot(self, account_id: str) -> dict | None:
        with self._lock:
            value = self._state["accounts"].get(account_id)
            if value is None:
                return None
            return self._public_account(value, True)

    def _refresh_account(self, account: AccountConfig, preclaimed: bool = False) -> None:
        if not preclaimed:
            with self._lock:
                if account.account_id in self._busy:
                    return
                self._busy.add(account.account_id)
                self._state["accounts"][account.account_id]["refreshing"] = True
        try:
            auth_required = False
            for endpoint in ENDPOINT_PATHS:
                if auth_required:
                    break
                started = time.monotonic()
                attempted_at = _now_iso()
                try:
                    payload = self.client.fetch(account.customer_no, endpoint)
                    normalized = normalize_response(endpoint, payload)
                except (UpstreamError, ContractError) as error:
                    kind = error.kind
                    http_status = getattr(error, "http_status", None)
                    auth_required = kind == "auth_required"
                    self._record_failure(
                        account.account_id,
                        endpoint,
                        attempted_at,
                        kind,
                        http_status,
                    )
                    LOGGER.warning(
                        "account=%s customer=****%s endpoint=%s result=%s duration_ms=%d",
                        account.account_id,
                        account.customer_no[-4:],
                        endpoint,
                        kind,
                        int((time.monotonic() - started) * 1000),
                    )
                except Exception as error:
                    kind = type(error).__name__
                    self._record_failure(
                        account.account_id,
                        endpoint,
                        attempted_at,
                        "internal_error",
                        None,
                    )
                    LOGGER.error(
                        "account=%s customer=****%s endpoint=%s result=internal_error error_type=%s duration_ms=%d",
                        account.account_id,
                        account.customer_no[-4:],
                        endpoint,
                        kind,
                        int((time.monotonic() - started) * 1000),
                    )
                else:
                    self._record_success(
                        account.account_id, endpoint, attempted_at, normalized
                    )
                    LOGGER.info(
                        "account=%s customer=****%s endpoint=%s result=%s issues=%d duration_ms=%d",
                        account.account_id,
                        account.customer_no[-4:],
                        endpoint,
                        "empty" if normalized.empty else "ok",
                        len(normalized.issues),
                        int((time.monotonic() - started) * 1000),
                    )
            with self._lock:
                value = self._state["accounts"][account.account_id]
                value["auth_required"] = auth_required
                value["last_refresh_at"] = _now_iso()
                self._update_account_status(value)
                self._state["updated_at"] = _now_iso()
                self.cache.save(self._state)
        finally:
            with self._lock:
                self._busy.discard(account.account_id)
                value = self._state["accounts"].get(account.account_id)
                if value is not None:
                    value["refreshing"] = False
            self._publish_account(account.account_id)

    def _publish_account(self, account_id: str) -> None:
        if self.publisher is None:
            return
        try:
            with self._lock:
                value = self._state["accounts"].get(account_id)
                if value is None:
                    return
                snapshot = self._public_account(value, True)
            self.publisher.publish_snapshot(
                account_id, mqtt_state_from_account(snapshot)
            )
        except Exception as error:
            LOGGER.warning(
                "account=%s mqtt_publish=%s", account_id, type(error).__name__
            )

    def _record_success(self, account_id, endpoint, attempted_at, normalized) -> None:
        with self._lock:
            value = self._state["accounts"][account_id]
            value["endpoints"][endpoint] = {
                "status": "empty" if normalized.empty else "ok",
                "data": normalized.data,
                "contract_issues": list(normalized.issues),
                "last_attempt_at": attempted_at,
                "last_success_at": attempted_at,
                "error": None,
            }

    def _record_failure(
        self,
        account_id: str,
        endpoint: str,
        attempted_at: str,
        kind: str,
        http_status: int | None,
    ) -> None:
        with self._lock:
            account = self._state["accounts"][account_id]
            previous = account["endpoints"].get(endpoint, {})
            has_cache = previous.get("last_success_at") is not None and "data" in previous
            account["endpoints"][endpoint] = {
                **previous,
                "status": "stale" if has_cache else "error",
                "last_attempt_at": attempted_at,
                "error": {"kind": kind, "http_status": http_status},
            }

    def _reconcile(self, state: dict) -> dict:
        now = datetime.now(UTC)
        existing = state.get("accounts", {})
        accounts: dict[str, dict] = {}
        for account in self.config.accounts:
            account_ref = self.cache.account_ref(account.customer_no)
            previous = existing.get(account.account_id, {})
            if previous.get("account_ref") != account_ref:
                previous = {}
            endpoints = deepcopy(previous.get("endpoints", {}))
            for endpoint_state in endpoints.values():
                last_success = _parse_iso(endpoint_state.get("last_success_at"))
                if last_success is None:
                    endpoint_state["status"] = "error"
                elif (now - last_success).total_seconds() > self.config.stale_after_seconds:
                    endpoint_state["status"] = "stale"
                else:
                    endpoint_state["status"] = "cached"
            value = {
                "account_ref": account_ref,
                "account_id": account.account_id,
                "masked_customer_no": account.masked_customer_no,
                "status": "starting",
                "refreshing": False,
                "auth_required": False,
                "last_refresh_at": previous.get("last_refresh_at"),
                "endpoints": endpoints,
            }
            self._update_account_status(value)
            accounts[account.account_id] = value
        return {
            "schema_version": 1,
            "updated_at": state.get("updated_at"),
            "accounts": accounts,
        }

    @staticmethod
    def _update_account_status(account: dict) -> None:
        endpoints = account.get("endpoints", {})
        if account.get("auth_required"):
            account["status"] = "auth_required"
            return
        if not endpoints:
            account["status"] = "starting"
            return
        states = [value.get("status") for value in endpoints.values()]
        has_data = any("data" in value for value in endpoints.values())
        has_issues = any(value.get("contract_issues") for value in endpoints.values())
        if len(endpoints) == len(ENDPOINT_PATHS) and all(
            state in {"ok", "empty"} for state in states
        ):
            account["status"] = "degraded" if has_issues else "good"
        elif has_data:
            account["status"] = "degraded"
        else:
            account["status"] = "unavailable"

    @staticmethod
    def _public_account(value: dict, include_endpoints: bool) -> dict:
        endpoints = deepcopy(value.get("endpoints", {}))
        info = endpoints.get("customer_info", {}).get("data", {})
        payment = endpoints.get("payment_summary", {}).get("data", {})
        customer = info.get("customer", {}) if isinstance(info, dict) else {}
        water = info.get("water", {}) if isinstance(info, dict) else {}
        meters = info.get("meters", []) if isinstance(info, dict) else []
        result = {
            "id": value.get("account_id"),
            "masked_customer_no": value.get("masked_customer_no"),
            "status": value.get("status"),
            "refreshing": value.get("refreshing", False),
            "last_refresh_at": value.get("last_refresh_at"),
            "last_success_at": _latest_success(endpoints),
            "summary": {
                "name": customer.get("name") or payment.get("customer_name"),
                "address": customer.get("address") or payment.get("address"),
                "remaining": payment.get("remaining", water.get("remaining")),
                "arrears": payment.get("arrears", water.get("arrears")),
                "meter_count": len(meters),
                "latest_reading": _latest_reading(meters),
            },
        }
        if include_endpoints:
            result["endpoints"] = endpoints
            result["statistics"] = build_statistics(
                endpoints.get("water_records", {}).get("data"),
                endpoints.get("payment_records", {}).get("data"),
            )
        return result


def _latest_success(endpoints: dict) -> str | None:
    values = [
        endpoint.get("last_success_at")
        for endpoint in endpoints.values()
        if endpoint.get("last_success_at")
    ]
    return max(values) if values else None


def _latest_reading(meters: list) -> float | None:
    dated = [
        (str(meter.get("latest_reading_date") or ""), meter.get("latest_reading"))
        for meter in meters
        if isinstance(meter, dict) and meter.get("latest_reading") is not None
    ]
    return max(dated, default=("", None))[1]


def _service_status(statuses: list[str]) -> str:
    if not statuses or all(status == "starting" for status in statuses):
        return "starting"
    if all(status == "good" for status in statuses):
        return "good"
    if any(status in {"good", "degraded"} for status in statuses):
        return "degraded"
    return "unavailable"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _parse_iso(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)
