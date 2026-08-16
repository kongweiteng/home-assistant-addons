"""Validated add-on options with a fail-closed transport policy."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from urllib.parse import urlparse


ACCOUNT_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
CUSTOMER_NO_PATTERN = re.compile(r"^[0-9]{6,32}$")
MAX_ACCOUNTS = 20


@dataclass(frozen=True)
class AccountConfig:
    account_id: str
    customer_no: str

    @property
    def masked_customer_no(self) -> str:
        return f"****{self.customer_no[-4:]}"


@dataclass(frozen=True)
class AppConfig:
    accounts: tuple[AccountConfig, ...]
    base_url: str
    allow_insecure_http: bool
    poll_interval_seconds: int
    request_timeout_seconds: int
    stale_after_seconds: int
    manual_refresh_cooldown_seconds: int

    @classmethod
    def load(cls, path: str | Path) -> "AppConfig":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("options must be a JSON object")

        account_values = raw.get("accounts", [])
        if not isinstance(account_values, list) or not account_values:
            raise ValueError("accounts must contain at least one account")
        if len(account_values) > MAX_ACCOUNTS:
            raise ValueError(f"accounts cannot contain more than {MAX_ACCOUNTS} entries")
        accounts: list[AccountConfig] = []
        seen_ids: set[str] = set()
        seen_numbers: set[str] = set()
        for value in account_values:
            if not isinstance(value, dict):
                raise ValueError("each account must be an object")
            account_id = value.get("id")
            customer_no = value.get("customer_no")
            if not isinstance(account_id, str) or not ACCOUNT_ID_PATTERN.fullmatch(account_id):
                raise ValueError("an account id is invalid")
            if not isinstance(customer_no, str) or not CUSTOMER_NO_PATTERN.fullmatch(customer_no):
                raise ValueError(f"account {account_id} has an invalid customer number")
            if account_id in seen_ids:
                raise ValueError(f"account id {account_id} is duplicated")
            if customer_no in seen_numbers:
                raise ValueError("the same customer number is configured more than once")
            seen_ids.add(account_id)
            seen_numbers.add(customer_no)
            accounts.append(AccountConfig(account_id, customer_no))

        base_url = _validate_base_url(str(raw.get("base_url", "")))
        allow_insecure_http = _strict_bool(
            raw.get("allow_insecure_http", False), "allow_insecure_http"
        )
        if urlparse(base_url).scheme == "http" and not allow_insecure_http:
            raise ValueError(
                "plain HTTP is blocked; explicitly enable allow_insecure_http or use HTTPS"
            )

        return cls(
            accounts=tuple(accounts),
            base_url=base_url,
            allow_insecure_http=allow_insecure_http,
            poll_interval_seconds=_bounded_int(
                raw, "poll_interval_minutes", 360, 10, 10080
            )
            * 60,
            request_timeout_seconds=_bounded_int(
                raw, "request_timeout_seconds", 15, 3, 50
            ),
            stale_after_seconds=_bounded_int(
                raw, "stale_after_minutes", 1440, 60, 43200
            )
            * 60,
            manual_refresh_cooldown_seconds=_bounded_int(
                raw, "manual_refresh_cooldown_seconds", 60, 30, 3600
            ),
        )

    def account(self, account_id: str) -> AccountConfig | None:
        return next(
            (account for account in self.accounts if account.account_id == account_id),
            None,
        )


def _validate_base_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("base_url must be an absolute HTTP or HTTPS URL")
    if parsed.username or parsed.password:
        raise ValueError("base_url must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("base_url must not contain a query or fragment")
    path = parsed.path.rstrip("/")
    if not path.endswith("/api"):
        raise ValueError("base_url path must end with /api")
    return value.rstrip("/")


def _strict_bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")
    return value


def _bounded_int(
    raw: dict, name: str, default: int, minimum: int, maximum: int
) -> int:
    try:
        value = int(raw.get(name, default))
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be an integer") from error
    if value < minimum or value > maximum:
        raise ValueError(f"{name} is outside its allowed range")
    return value
