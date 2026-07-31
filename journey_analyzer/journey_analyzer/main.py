"""Journey Analyzer add-on entrypoint."""

from __future__ import annotations

import logging
import os
import signal
import threading

from .api import create_server
from .config import AppConfig
from .ha_client import HomeAssistantClient
from .mqtt import MqttPublisher, MqttSettings
from .runtime import CollectorService, RuntimeState


def _as_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = AppConfig.load(os.environ["JOURNEY_OPTIONS_FILE"])
    db_path = os.environ.get("JOURNEY_DATABASE_PATH", "/data/journeys.db")
    runtime_state = RuntimeState()
    publisher = MqttPublisher(
        MqttSettings(
            host=os.environ["JOURNEY_MQTT_HOST"],
            port=int(os.environ.get("JOURNEY_MQTT_PORT", "1883")),
            username=os.environ.get("JOURNEY_MQTT_USERNAME", ""),
            password=os.environ.get("JOURNEY_MQTT_PASSWORD", ""),
            use_tls=_as_bool(os.environ.get("JOURNEY_MQTT_SSL", "false")),
        )
    )
    ha_client = HomeAssistantClient(
        os.environ.get("JOURNEY_HA_BASE_URL", "http://supervisor/core/api"),
        os.environ["JOURNEY_HA_TOKEN"],
    )
    publisher.connect()
    server = create_server("0.0.0.0", 8099, db_path, config, runtime_state)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    stop_event = threading.Event()

    def stop(signum, frame) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        CollectorService(
            config, db_path, ha_client, publisher, runtime_state
        ).run_forever(stop_event)
    finally:
        server.shutdown()
        server.server_close()
        publisher.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
