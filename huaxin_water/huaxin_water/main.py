"""Huaxin Water add-on entrypoint."""

from __future__ import annotations

import logging
import os
import signal
import threading

from .api import create_server
from .cache import CacheStore
from .client import HuaxinClient
from .config import AppConfig
from .mqtt import MqttPublisher, MqttSettings
from .runtime import WaterService


def _as_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = AppConfig.load(os.environ["HUAXIN_OPTIONS_FILE"])
    publisher = MqttPublisher(
        MqttSettings(
            host=os.environ["HUAXIN_MQTT_HOST"],
            port=int(os.environ.get("HUAXIN_MQTT_PORT", "1883")),
            username=os.environ.get("HUAXIN_MQTT_USERNAME", ""),
            password=os.environ.get("HUAXIN_MQTT_PASSWORD", ""),
            use_tls=_as_bool(os.environ.get("HUAXIN_MQTT_SSL", "false")),
        ),
        tuple(account.account_id for account in config.accounts),
        os.environ.get("HUAXIN_MQTT_TOPICS_PATH", "/data/mqtt-topics.json"),
    )
    publisher.connect()
    cache = CacheStore(
        os.environ.get("HUAXIN_STATE_PATH", "/data/state.json"),
        os.environ.get("HUAXIN_CACHE_KEY_PATH", "/data/cache.key"),
    )
    service = WaterService(
        config,
        HuaxinClient(config.base_url, config.request_timeout_seconds),
        cache,
        publisher,
    )
    server = create_server("0.0.0.0", 8098, service)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    stop_event = threading.Event()

    def stop(signum, frame) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        service.run_forever(stop_event)
    finally:
        server.shutdown()
        server.server_close()
        publisher.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
