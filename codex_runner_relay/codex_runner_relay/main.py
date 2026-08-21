"""Runtime entrypoint for the Runner Relay Add-on."""

from __future__ import annotations

import os

from aiohttp import web

from .app import RelayHub, create_app
from .controller import ControllerClient


def main() -> None:
    controller = ControllerClient(
        os.environ["RELAY_CONTROLLER_BASE_URL"],
        os.environ["RELAY_CONTROLLER_API_TOKEN"],
        timeout_seconds=int(os.environ.get("RELAY_CONTROLLER_TIMEOUT_SECONDS", "10")),
    )
    hub = RelayHub(
        controller,
        api_token=os.environ["RELAY_API_TOKEN"],
        max_connections=int(os.environ.get("RELAY_MAX_CONNECTIONS", "64")),
        max_message_bytes=int(os.environ.get("RELAY_MAX_MESSAGE_BYTES", "524288")),
        first_frame_timeout_seconds=int(os.environ.get("RELAY_FIRST_FRAME_TIMEOUT_SECONDS", "10")),
        messages_per_minute=int(os.environ.get("RELAY_MESSAGES_PER_MINUTE", "1200")),
    )
    web.run_app(create_app(hub), host="0.0.0.0", port=8098, access_log=None)


if __name__ == "__main__":
    main()
