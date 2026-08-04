"""Runtime entrypoint for the read-only Manager Executor shadow."""

from __future__ import annotations

import os

from .api import create_server
from .service import ShadowManager
from .supervisor import SupervisorClient


def main() -> None:
    api_token = os.environ["MANAGER_API_TOKEN"]
    allowlist = frozenset(
        item.strip()
        for item in os.environ.get("MANAGER_RESTART_ADDON_ALLOWLIST", "").split(",")
        if item.strip()
    )
    supervisor = SupervisorClient(
        os.environ["MANAGER_SUPERVISOR_TOKEN"],
        base_url=os.environ.get("MANAGER_SUPERVISOR_BASE_URL", "http://supervisor"),
        timeout_seconds=int(os.environ.get("MANAGER_SUPERVISOR_TIMEOUT_SECONDS", "5")),
    )
    manager = ShadowManager(supervisor=supervisor, restart_addon_allowlist=allowlist)
    server = create_server(
        "0.0.0.0",
        8099,
        api_token=api_token,
        max_request_bytes=int(os.environ.get("MANAGER_MAX_REQUEST_BYTES", "32768")),
        restart_shadow_handler=manager.restart_addon,
        allowlist_count=len(allowlist),
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
