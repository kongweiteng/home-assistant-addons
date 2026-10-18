"""Strict provider configuration loaded from Home Assistant Add-on options."""
from __future__ import annotations

from dataclasses import dataclass
from email.utils import parseaddr
import json
from pathlib import Path
import re


EMAIL_RE = re.compile(r"^[^\s@\x00-\x1f\x7f]{1,64}@[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")


class ConfigError(RuntimeError):
    pass


def normalize_email(value: object) -> str:
    if not isinstance(value, str) or not 3 <= len(value) <= 254 or parseaddr(value)[1] != value or not EMAIL_RE.fullmatch(value):
        raise ConfigError("email_invalid")
    local, domain = value.rsplit("@", 1)
    try:
        domain = domain.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ConfigError("email_invalid") from exc
    return f"{local}@{domain}"


@dataclass(frozen=True)
class MailConfig:
    username: str
    authorization_code: str
    sender_name: str
    allowed_recipients: frozenset[str]
    extension_bearer: str
    smtp_host: str = "smtp.qq.com"
    smtp_port: int = 465
    imap_host: str = "imap.qq.com"
    imap_port: int = 993

    @classmethod
    def load(cls, path: Path) -> "MailConfig":
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ConfigError("options_invalid") from exc
        expected = {"smtp_username", "smtp_authorization_code", "sender_name", "allowed_recipients", "extension_bearer"}
        if not isinstance(document, dict) or set(document) != expected:
            raise ConfigError("options_invalid")
        username = normalize_email(document["smtp_username"])
        if not username.lower().endswith("@qq.com"):
            raise ConfigError("smtp_account_unsupported")
        code = document["smtp_authorization_code"]
        bearer = document["extension_bearer"]
        sender_name = document["sender_name"]
        recipients = document["allowed_recipients"]
        if not isinstance(code, str) or not 12 <= len(code) <= 128 or any(ord(ch) < 33 or ord(ch) > 126 for ch in code):
            raise ConfigError("smtp_authorization_code_invalid")
        if not isinstance(bearer, str) or not 32 <= len(bearer) <= 256 or any(ord(ch) < 33 or ord(ch) > 126 for ch in bearer):
            raise ConfigError("extension_bearer_invalid")
        if not isinstance(sender_name, str) or not 1 <= len(sender_name) <= 80 or any(ord(ch) < 32 for ch in sender_name):
            raise ConfigError("sender_name_invalid")
        if not isinstance(recipients, list) or not 1 <= len(recipients) <= 50:
            raise ConfigError("allowed_recipients_invalid")
        normalized = frozenset(normalize_email(item).casefold() for item in recipients)
        if len(normalized) != len(recipients):
            raise ConfigError("allowed_recipients_invalid")
        return cls(username, code, sender_name, normalized, bearer)
