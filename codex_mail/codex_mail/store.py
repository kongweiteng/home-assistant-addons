"""Durable mail idempotency and delivery audit store."""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def payload_digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode()).hexdigest()


class MailStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with closing(self._connect()) as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA synchronous=FULL;
                CREATE TABLE IF NOT EXISTS sends (
                    idempotency_key TEXT PRIMARY KEY,
                    send_id TEXT NOT NULL UNIQUE,
                    payload_digest TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    recipients_json TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (state IN ('sent','failed','unknown')),
                    verification TEXT NOT NULL,
                    error_code TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            columns = {row[1] for row in connection.execute("PRAGMA table_info(sends)")}
            if "attachments_json" not in columns:
                connection.execute("ALTER TABLE sends ADD COLUMN attachments_json TEXT NOT NULL DEFAULT '[]'")
        try:
            path.chmod(0o600)
        except OSError:
            pass

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def claim(self, key: str, arguments: dict[str, Any], message_id: str, now: str) -> dict[str, Any] | None:
        digest = payload_digest(arguments)
        send_id = hashlib.sha256(key.encode()).hexdigest()[:20]
        recipients = list(arguments["to"]) + list(arguments.get("cc", []))
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM sends WHERE idempotency_key=?", (key,)).fetchone()
            if row is not None:
                connection.execute("COMMIT")
                if row["payload_digest"] != digest:
                    raise RuntimeError("idempotency_conflict")
                return dict(row)
            connection.execute(
                "INSERT INTO sends(idempotency_key,send_id,payload_digest,message_id,recipients_json,subject,state,verification,error_code,created_at,updated_at,attachments_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (key, send_id, digest, message_id, canonical_json(recipients), arguments["subject"],
                 "unknown", "unresolved", "delivery_uncertain", now, now, canonical_json(arguments.get("attachments", []))),
            )
            connection.execute("COMMIT")
        return None

    def finish(self, key: str, *, state: str, verification: str, error_code: str, now: str) -> None:
        if state not in {"sent", "failed", "unknown"}:
            raise ValueError("state_invalid")
        with closing(self._connect()) as connection:
            connection.execute(
                "UPDATE sends SET state=?,verification=?,error_code=?,updated_at=? WHERE idempotency_key=?",
                (state, verification, error_code, now, key),
            )

    def get(self, key: str) -> dict[str, Any]:
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT * FROM sends WHERE idempotency_key=?", (key,)).fetchone()
        if row is None:
            raise RuntimeError("send_record_missing")
        return dict(row)

    def recent(self, limit: int = 10) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute("SELECT * FROM sends ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]
