"""Runtime entrypoint for the HA Operations Broker P5 canary."""

from __future__ import annotations

import os

from .api import create_server
from .contract import parse_owner_hashes, utc_now
from .service import preflight
from .supervisor import SupervisorClient


def main() -> None:
    api_token = os.environ["BROKER_API_TOKEN"]
    owner_hashes = parse_owner_hashes(os.environ["BROKER_OWNER_HASHES"])
    max_request_bytes = int(os.environ.get("BROKER_MAX_REQUEST_BYTES", "32768"))
    timeout_seconds = int(os.environ.get("BROKER_SUPERVISOR_TIMEOUT_SECONDS", "5"))
    supervisor = SupervisorClient(
        os.environ["BROKER_SUPERVISOR_TOKEN"],
        base_url=os.environ.get("BROKER_SUPERVISOR_BASE_URL", "http://supervisor"),
        timeout_seconds=timeout_seconds,
    )

    def handler(payload):
        return preflight(
            payload,
            trusted_owner_hashes=owner_hashes,
            supervisor=supervisor,
            clock=utc_now,
        )

    server = create_server(
        "0.0.0.0",
        8098,
        api_token=api_token,
        max_request_bytes=max_request_bytes,
        preflight_handler=handler,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
