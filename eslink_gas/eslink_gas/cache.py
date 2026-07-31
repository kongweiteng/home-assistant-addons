"""Small atomic private state cache for restart and stale-data handling."""

from __future__ import annotations

import json
import os
from pathlib import Path


CACHE_VERSION = 1


class StateCache:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load(self, account_ids: tuple[str, ...]) -> dict[str, dict]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}
        if not isinstance(raw, dict) or raw.get("version") != CACHE_VERSION:
            return {}
        accounts = raw.get("accounts")
        if not isinstance(accounts, dict):
            return {}
        allowed = set(account_ids)
        return {
            key: dict(value)
            for key, value in accounts.items()
            if key in allowed and isinstance(value, dict)
        }

    def save(self, accounts: dict[str, dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        payload = json.dumps(
            {"version": CACHE_VERSION, "accounts": accounts},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o600,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
