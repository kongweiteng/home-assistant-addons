"""Polling, partial failure isolation, freshness and persistent state."""

from __future__ import annotations

from datetime import datetime, timezone
import logging
import threading

from .cache import StateCache
from .client import AuthRequiredError, FetchResult
from .config import AppConfig
from .normalize import normalize_account


LOGGER = logging.getLogger(__name__)


class RuntimeState:
    def __init__(self, initial: dict[str, dict] | None = None) -> None:
        self._lock = threading.Lock()
        self._accounts = dict(initial or {})
        self._generated_at: str | None = None

    def update(self, accounts: dict[str, dict], generated_at: str) -> None:
        with self._lock:
            self._accounts = {key: dict(value) for key, value in accounts.items()}
            self._generated_at = generated_at

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "generated_at": self._generated_at,
                "accounts": {
                    key: dict(value) for key, value in self._accounts.items()
                },
            }


class GasMonitor:
    def __init__(
        self,
        config: AppConfig,
        client,
        cache: StateCache,
        publisher,
        runtime_state: RuntimeState,
    ) -> None:
        self.config = config
        self.client = client
        self.cache = cache
        self.publisher = publisher
        self.runtime_state = runtime_state
        self._accounts_by_id = {account.id: account for account in config.accounts}
        self._state = cache.load(tuple(self._accounts_by_id))
        if self._state:
            self.runtime_state.update(self._state, _utc_text(datetime.now(timezone.utc)))

    def run_once(self, now: datetime | None = None) -> dict[str, dict]:
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        generated_at = _utc_text(current)
        fetched = {result.account_id: result for result in self.client.fetch_accounts()}
        next_state: dict[str, dict] = {}
        for account_id, account in self._accounts_by_id.items():
            result = fetched.get(account_id)
            if result is not None and result.payload is not None:
                try:
                    snapshot = normalize_account(
                        result.payload,
                        account,
                        include_personal_details=self.config.include_personal_details,
                        fetched_at=current,
                    )
                except Exception as error:
                    result = FetchResult(account_id, error=error)
                else:
                    next_state[account_id] = snapshot
                    self.publisher.publish_snapshot(account_id, snapshot)
                    continue
            error = result.error if result is not None else RuntimeError("missing_result")
            snapshot = self._failure_snapshot(account_id, error, current)
            next_state[account_id] = snapshot
            self.publisher.publish_snapshot(account_id, snapshot)
        self._state = next_state
        self.cache.save(next_state)
        self.runtime_state.update(next_state, generated_at)
        return next_state

    def run_forever(self, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            try:
                self.run_once()
            except Exception as error:
                LOGGER.error("Polling cycle failed (%s)", type(error).__name__)
            stop_event.wait(self.config.poll_interval_s)

    def _failure_snapshot(
        self, account_id: str, error: Exception, current: datetime
    ) -> dict:
        previous = dict(self._state.get(account_id, {}))
        last_success = _parse_time(previous.get("last_success_at"))
        age = None if last_success is None else (current - last_success).total_seconds()
        if isinstance(error, AuthRequiredError):
            status = "auth_required"
        elif previous and age is not None and age <= self.config.stale_after_s:
            status = "degraded"
        elif previous:
            status = "stale"
        else:
            status = "unavailable"
        previous.update(
            {
                "account_id": account_id,
                "status": status,
                "available": status == "degraded",
                "fetched_at": _utc_text(current),
                "last_error": type(error).__name__,
            }
        )
        previous.setdefault("balance", None)
        previous.setdefault("meter_count", None)
        previous.setdefault("meter_status", None)
        previous.setdefault("meters", [])
        return previous


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_time(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)
