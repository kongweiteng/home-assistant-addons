"""SMTP delivery, durable idempotency, and Sent-folder reconciliation."""
from __future__ import annotations

from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
import imaplib
import json
import re
import smtplib
import ssl
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .config import ConfigError, MailConfig, normalize_email
from .store import MailStore


SHANGHAI = ZoneInfo("Asia/Shanghai")
HEX64_RE = re.compile(r"^[a-f0-9]{64}$")
IDEMPOTENCY_RE = re.compile(r"^sha256:[a-f0-9]{64}$")


def now_iso() -> str:
    return datetime.now(SHANGHAI).isoformat(timespec="seconds")


class DeliveryError(RuntimeError):
    def __init__(self, code: str, definite: bool):
        self.code = code
        self.definite = definite
        super().__init__(code)


def validate_context(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {"job_hash", "turn_hash", "idempotency_key"}:
        raise ValueError("context_invalid")
    if not all(isinstance(value[key], str) for key in value):
        raise ValueError("context_invalid")
    if not HEX64_RE.fullmatch(value["job_hash"]) or not HEX64_RE.fullmatch(value["turn_hash"]) or not IDEMPOTENCY_RE.fullmatch(value["idempotency_key"]):
        raise ValueError("context_invalid")
    return value


def validated_arguments(value: object, config: MailConfig) -> dict[str, Any]:
    if not isinstance(value, dict) or not set(value) <= {"to", "cc", "subject", "text_body"} or not {"to", "subject", "text_body"} <= set(value):
        raise ValueError("arguments_invalid")
    to = value["to"]
    cc = value.get("cc", [])
    subject = value["subject"]
    body = value["text_body"]
    if not isinstance(to, list) or not 1 <= len(to) <= 10 or not isinstance(cc, list) or len(cc) > 10:
        raise ValueError("arguments_invalid")
    if not isinstance(subject, str) or not 1 <= len(subject) <= 200 or "\r" in subject or "\n" in subject:
        raise ValueError("arguments_invalid")
    if not isinstance(body, str) or not 1 <= len(body) <= 20000 or "\x00" in body:
        raise ValueError("arguments_invalid")
    try:
        normalized_to = [normalize_email(item) for item in to]
        normalized_cc = [normalize_email(item) for item in cc]
    except ConfigError as exc:
        raise ValueError("arguments_invalid") from exc
    recipients = normalized_to + normalized_cc
    if len({item.casefold() for item in recipients}) != len(recipients):
        raise ValueError("arguments_invalid")
    if any(item.casefold() not in config.allowed_recipients for item in recipients):
        raise PermissionError("recipient_not_allowed")
    return {"to": normalized_to, "cc": normalized_cc, "subject": subject, "text_body": body}


def smtp_send(config: MailConfig, message: EmailMessage) -> None:
    try:
        with smtplib.SMTP_SSL(config.smtp_host, config.smtp_port, timeout=8, context=ssl.create_default_context()) as client:
            client.login(config.username, config.authorization_code)
            refused = client.send_message(message)
    except smtplib.SMTPAuthenticationError as exc:
        raise DeliveryError("smtp_auth_failed", True) from exc
    except smtplib.SMTPSenderRefused as exc:
        raise DeliveryError("sender_refused", True) from exc
    except smtplib.SMTPRecipientsRefused as exc:
        raise DeliveryError("recipients_refused", True) from exc
    except (smtplib.SMTPException, OSError, TimeoutError) as exc:
        raise DeliveryError("delivery_uncertain", False) from exc
    if refused:
        raise DeliveryError("partial_recipient_failure", False)


def _sent_mailboxes(client: imaplib.IMAP4_SSL) -> list[bytes]:
    status, rows = client.list()
    if status != "OK" or not rows:
        return []
    result: list[bytes] = []
    for row in rows:
        if not isinstance(row, bytes) or b"\\Sent" not in row:
            continue
        match = re.search(rb'"([^"\\]*(?:\\.[^"\\]*)*)"\s*$', row)
        if match:
            result.append(match.group(1).replace(b'\\"', b'"'))
        else:
            result.append(row.rsplit(b" ", 1)[-1])
    return result


def sent_contains(config: MailConfig, message_id: str) -> bool:
    try:
        with imaplib.IMAP4_SSL(config.imap_host, config.imap_port, ssl_context=ssl.create_default_context(), timeout=5) as client:
            client.login(config.username, config.authorization_code)
            for mailbox in _sent_mailboxes(client):
                status, _ = client.select(mailbox, readonly=True)
                if status != "OK":
                    continue
                status, data = client.uid("search", None, "HEADER", "Message-ID", message_id)
                if status == "OK" and data and data[0].strip():
                    return True
    except (imaplib.IMAP4.error, OSError, TimeoutError, ssl.SSLError):
        return False
    return False


class MailService:
    def __init__(self, config: MailConfig, store: MailStore,
                 sender: Callable[[MailConfig, EmailMessage], None] = smtp_send,
                 sent_checker: Callable[[MailConfig, str], bool] = sent_contains):
        self.config = config
        self.store = store
        self.sender = sender
        self.sent_checker = sent_checker

    @staticmethod
    def _result(row: dict[str, Any]) -> dict[str, str]:
        return {
            "status": row["state"], "send_id": row["send_id"], "message_id": row["message_id"],
            "verification": row["verification"], "error_code": row["error_code"],
        }

    def send(self, arguments: object, context: object) -> dict[str, str]:
        ctx = validate_context(context)
        try:
            args = validated_arguments(arguments, self.config)
        except PermissionError:
            return {"status": "failed", "send_id": "not-created", "message_id": "not-created",
                    "verification": "not_sent", "error_code": "recipient_not_allowed"}
        except ValueError:
            return {"status": "failed", "send_id": "not-created", "message_id": "not-created",
                    "verification": "not_sent", "error_code": "arguments_invalid"}
        message_id = make_msgid(domain="qq.com")
        existing = self.store.claim(ctx["idempotency_key"], args, message_id, now_iso())
        if existing is not None:
            if existing["state"] == "unknown" and self.sent_checker(self.config, existing["message_id"]):
                self.store.finish(ctx["idempotency_key"], state="sent", verification="sent_folder_confirmed", error_code="", now=now_iso())
                existing.update(state="sent", verification="sent_folder_confirmed", error_code="")
            return self._result(existing)
        row = self.store.get(ctx["idempotency_key"])
        message = EmailMessage()
        message["From"] = formataddr((self.config.sender_name, self.config.username))
        message["To"] = ", ".join(args["to"])
        if args["cc"]:
            message["Cc"] = ", ".join(args["cc"])
        message["Subject"] = args["subject"]
        message["Message-ID"] = message_id
        message.set_content(args["text_body"], charset="utf-8")
        try:
            self.sender(self.config, message)
        except DeliveryError as exc:
            state = "failed" if exc.definite else "unknown"
            verification = "not_sent" if exc.definite else "unresolved"
            self.store.finish(ctx["idempotency_key"], state=state, verification=verification, error_code=exc.code, now=now_iso())
            row.update(state=state, verification=verification, error_code=exc.code)
            return self._result(row)
        self.store.finish(ctx["idempotency_key"], state="sent", verification="smtp_accepted", error_code="", now=now_iso())
        row.update(state="sent", verification="smtp_accepted", error_code="")
        return self._result(row)

    def recent_status(self) -> dict[str, list[dict[str, Any]]]:
        records = self.store.recent(10)
        items: list[dict[str, Any]] = []
        reconciled_one = False
        for row in records:
            if not reconciled_one and row["state"] == "unknown" and self.sent_checker(self.config, row["message_id"]):
                self.store.finish(row["idempotency_key"], state="sent", verification="sent_folder_confirmed", error_code="", now=now_iso())
                row.update(state="sent", verification="sent_folder_confirmed", error_code="", updated_at=now_iso())
                reconciled_one = True
            elif row["state"] == "unknown" and not reconciled_one:
                reconciled_one = True
            items.append({
                "send_id": row["send_id"], "status": row["state"], "verification": row["verification"],
                "error_code": row["error_code"], "recipients": json.loads(row["recipients_json"]),
                "subject": row["subject"], "created_at": row["created_at"], "updated_at": row["updated_at"],
            })
        return {"items": items}
