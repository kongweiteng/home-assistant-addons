"""Renovation Hub runtime entrypoint."""

from __future__ import annotations

import os

from aiohttp import web

from .hub import RenovationHubStore
from .ledger import LedgerError
from .media import MediaService
from .web import create_app


def main() -> None:
    store = RenovationHubStore(
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
    media = MediaService(
        store=store,
        media_root=os.environ.get("RENOVATION_MEDIA_ROOT", "/media/renovation-hub/originals"),
        preview_root=os.environ.get("RENOVATION_PREVIEW_ROOT", "/data/media-previews"),
        staging_root=os.environ.get("RENOVATION_STAGING_ROOT", "/data/media-staging"),
        max_media_bytes=int(os.environ.get("RENOVATION_MAX_MEDIA_BYTES", str(1024 * 1024 * 1024))),
    )
    app = create_app(
        store=store,
        media=media,
        api_token=os.environ["LEDGER_API_TOKEN"],
        max_request_bytes=int(os.environ.get("LEDGER_MAX_REQUEST_BYTES", "1048576")),
        static_dir=os.environ.get("RENOVATION_STATIC_DIR", "/opt/renovation-hub/web"),
    )
    web.run_app(app, host="0.0.0.0", port=8101, access_log=None)


if __name__ == "__main__":
    main()
