"""Validated runtime configuration without logging private option values."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from urllib.parse import parse_qs, urlsplit


ACCOUNT_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
USER_NO = re.compile(r"^[0-9]{6,32}$")
PORTAL_HOST = "cloudselfhelp-mobile.eslink.cc"


@dataclass(frozen=True)
class AccountConfig:
    id: str
    user_no: str
    user_name: str


@dataclass(frozen=True)
class AppConfig:
    accounts: tuple[AccountConfig, ...]
    portal_url: str
    poll_interval_s: int
    page_timeout_s: int
    stale_after_s: int
    include_personal_details: bool

    @classmethod
    def load(cls, path: str | Path) -> "AppConfig":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("Options must be a JSON object")
        if raw.get("allow_insecure_http") is not True:
            raise ValueError("allow_insecure_http must be explicitly enabled")
        portal_url = _validate_portal_url(raw.get("portal_url"))
        accounts_raw = raw.get("accounts")
        if not isinstance(accounts_raw, list) or not accounts_raw:
            raise ValueError("At least one account is required")
        if len(accounts_raw) > 20:
            raise ValueError("At most 20 accounts are supported")
        accounts: list[AccountConfig] = []
        seen_ids: set[str] = set()
        seen_numbers: set[str] = set()
        for item in accounts_raw:
            if not isinstance(item, dict):
                raise ValueError("Each account must be an object")
            account_id = item.get("id")
            user_no = item.get("user_no")
            user_name = item.get("user_name")
            if not isinstance(account_id, str) or not ACCOUNT_ID.fullmatch(account_id):
                raise ValueError("Invalid account id")
            if not isinstance(user_no, str) or not USER_NO.fullmatch(user_no):
                raise ValueError("Invalid user number")
            if not isinstance(user_name, str) or not user_name.strip() or len(user_name) > 64:
                raise ValueError("A bounded user name is required for each account")
            if account_id in seen_ids or user_no in seen_numbers:
                raise ValueError("Account ids and user numbers must be unique")
            seen_ids.add(account_id)
            seen_numbers.add(user_no)
            accounts.append(AccountConfig(account_id, user_no, user_name.strip()))
        poll_minutes = _bounded_int(raw.get("poll_interval_minutes", 30), 5, 1440)
        page_timeout = _bounded_int(raw.get("page_timeout_seconds", 25), 10, 60)
        stale_minutes = _bounded_int(raw.get("stale_after_minutes", 180), 10, 10080)
        include_personal = raw.get("include_personal_details", False)
        if not isinstance(include_personal, bool):
            raise ValueError("include_personal_details must be boolean")
        return cls(
            accounts=tuple(accounts),
            portal_url=portal_url,
            poll_interval_s=poll_minutes * 60,
            page_timeout_s=page_timeout,
            stale_after_s=stale_minutes * 60,
            include_personal_details=include_personal,
        )


def _validate_portal_url(value) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("portal_url is required")
    parsed = urlsplit(value)
    if parsed.scheme != "http" or parsed.hostname != PORTAL_HOST or parsed.path not in {"", "/"}:
        raise ValueError("portal_url must use the fixed ESLink service hall host")
    route, separator, query = parsed.fragment.partition("?")
    params = parse_qs(query, keep_blank_values=True) if separator else {}
    token = params.get("token", [""])[0]
    if route not in {"/index", "index"} or not re.fullmatch(r"[A-Za-z0-9_-]{16,256}", token):
        raise ValueError("portal_url must contain the service hall index route and token")
    return value


def _bounded_int(value, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise ValueError("Boolean is not an integer option")
    try:
        result = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError("Invalid integer option") from error
    if result < minimum or result > maximum:
        raise ValueError("Integer option is outside the supported range")
    return result
