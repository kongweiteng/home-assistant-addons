"""Atomic last-success cache keyed without storing configured customer numbers."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import tempfile


SCHEMA_VERSION = 1


class CacheStore:
    def __init__(self, state_path: str | Path, key_path: str | Path) -> None:
        self.state_path = Path(state_path)
        self.key_path = Path(key_path)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.key_path.parent.mkdir(parents=True, exist_ok=True)
        self._key = self._load_or_create_key()

    def account_ref(self, customer_no: str) -> str:
        return hmac.new(
            self._key, customer_no.encode("utf-8"), hashlib.sha256
        ).hexdigest()

    def load(self) -> dict:
        if not self.state_path.exists():
            return self.empty_state()
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return self.empty_state()
        if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
            return self.empty_state()
        if not isinstance(value.get("accounts"), dict):
            return self.empty_state()
        return value

    def save(self, state: dict) -> None:
        payload = json.dumps(
            state, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=".huaxin-state-", suffix=".tmp", dir=self.state_path.parent
        )
        try:
            os.fchmod(file_descriptor, 0o600)
            with os.fdopen(file_descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, self.state_path)
            os.chmod(self.state_path, 0o600)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)

    @staticmethod
    def empty_state() -> dict:
        return {"schema_version": SCHEMA_VERSION, "updated_at": None, "accounts": {}}

    def _load_or_create_key(self) -> bytes:
        try:
            value = self.key_path.read_bytes()
        except FileNotFoundError:
            value = secrets.token_bytes(32)
            try:
                descriptor = os.open(
                    self.key_path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
            except FileExistsError:
                value = self.key_path.read_bytes()
            else:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(value)
                    handle.flush()
                    os.fsync(handle.fileno())
        if len(value) < 32:
            raise ValueError("cache key is invalid")
        os.chmod(self.key_path, 0o600)
        return value
