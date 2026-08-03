"""Renovation Ledger runtime entrypoint."""

from __future__ import annotations

import os

from .api import create_server
from .core import LedgerError, LedgerStore


def main() -> None:
    store = LedgerStore(
        os.environ.get("LEDGER_DATABASE_PATH", "/data/ledger.sqlite3"),
        data_dir=os.environ.get("LEDGER_DATA_DIR", "/data"),
        share_dir=os.environ.get("LEDGER_SHARE_DIR", "/share/private/renovation-bookkeeping"),
        max_attachment_bytes=int(os.environ.get("LEDGER_MAX_ATTACHMENT_BYTES", str(20 * 1024 * 1024))),
        portable_history_limit=int(os.environ.get("LEDGER_PORTABLE_HISTORY_LIMIT", "20")),
    )
    configured = os.environ.get("LEDGER_WRITER_MODE", "read_only")
    current = store.writer_mode()
    if current == "uninitialized":
        store.set_writer_mode("read_only", force_initial=True)
        current = "read_only"
    if configured == "suspended" and current != "suspended":
        store.set_writer_mode("suspended")
    elif configured == "primary_writer" and current != "primary_writer":
        raise LedgerError("writer_activation_required", "options 不能直接激活 primary_writer，请使用独立切换流程")
    server = create_server(
        "0.0.0.0",
        8101,
        store=store,
        api_token=os.environ["LEDGER_API_TOKEN"],
        max_request_bytes=int(os.environ.get("LEDGER_MAX_REQUEST_BYTES", "1048576")),
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
