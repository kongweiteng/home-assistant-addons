"""Renovation Hub runtime entrypoint."""

from __future__ import annotations

import json
import os
from pathlib import Path

from aiohttp import web

from .hub import RenovationHubStore
from .ledger import LedgerError
from .media import MediaService
from .web import create_app


def _load_options() -> dict[str, object]:
    path = Path(os.environ.get("RENOVATION_HUB_OPTIONS_FILE", "/data/options.json"))
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise LedgerError("options_invalid", "无法读取 Add-on options") from exc
    if not isinstance(value, dict):
        raise LedgerError("options_invalid", "Add-on options 必须是对象")
    return value


def main() -> None:
    options = _load_options()
    cutover_token = str(options.get("cutover_token") or os.environ.get("LEDGER_CUTOVER_TOKEN", ""))
    if cutover_token and len(cutover_token) < 32:
        raise LedgerError("options_invalid", "cutover_token 至少需要 32 个字符")
    store = RenovationHubStore(
        os.environ.get("LEDGER_DATABASE_PATH", "/data/ledger.sqlite3"),
        data_dir=os.environ.get("LEDGER_DATA_DIR", "/data"),
        share_dir=os.environ.get("LEDGER_SHARE_DIR", "/share/private/renovation-bookkeeping"),
        max_attachment_bytes=int(os.environ.get("LEDGER_MAX_ATTACHMENT_BYTES", str(20 * 1024 * 1024))),
        portable_history_limit=int(os.environ.get("LEDGER_PORTABLE_HISTORY_LIMIT", "20")),
        enforce_cutover_manifest=True,
    )
    configured = str(options.get("writer_mode") or os.environ.get("LEDGER_WRITER_MODE", "read_only"))
    store.coordinate_configured_writer_mode(configured)
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
        cutover_token=cutover_token,
        max_request_bytes=int(os.environ.get("LEDGER_MAX_REQUEST_BYTES", "1048576")),
        static_dir=os.environ.get("RENOVATION_STATIC_DIR", "/opt/renovation-hub/web"),
    )
    web.run_app(app, host="0.0.0.0", port=8101, access_log=None)


if __name__ == "__main__":
    main()
