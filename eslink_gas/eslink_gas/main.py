"""ESLink Gas add-on entrypoint."""

from __future__ import annotations

import logging
import os
import signal
import threading

from .api import create_server
from .cache import StateCache
from .client import EslinkBrowserClient
from .config import AppConfig
from .ha_client import HomeAssistantClient
from .mqtt import HomeAssistantMqttPublisher, MqttPublisher, MqttSettings
from .runtime import GasMonitor, RuntimeState


def _as_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = AppConfig.load(os.environ["ESLINK_OPTIONS_FILE"])
    account_ids = tuple(account.id for account in config.accounts)
    runtime_state = RuntimeState()
    ha_client = HomeAssistantClient(
        os.environ.get("ESLINK_HA_BASE_URL", "http://supervisor/core/api"),
        os.environ["ESLINK_HA_TOKEN"],
    )
    if os.environ.get("ESLINK_PUBLISHER", "ha_mqtt") == "direct_mqtt":
        publisher = MqttPublisher(
            MqttSettings(
                host=os.environ["ESLINK_MQTT_HOST"],
                port=int(os.environ.get("ESLINK_MQTT_PORT", "1883")),
                username=os.environ.get("ESLINK_MQTT_USERNAME", ""),
                password=os.environ.get("ESLINK_MQTT_PASSWORD", ""),
                use_tls=_as_bool(os.environ.get("ESLINK_MQTT_SSL", "false")),
            ),
            account_ids,
        )
    else:
        publisher = HomeAssistantMqttPublisher(ha_client, account_ids)
    publisher.connect()
    monitor = GasMonitor(
        config,
        EslinkBrowserClient(
            config,
            os.environ.get("ESLINK_BROWSER_PROFILE", "/data/chromium-profile"),
            os.environ.get("ESLINK_BROWSER_BINARY", "/usr/bin/chromium"),
        ),
        StateCache(os.environ.get("ESLINK_STATE_PATH", "/data/state.json")),
        publisher,
        runtime_state,
    )
    server = create_server("0.0.0.0", 8097, runtime_state)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    stop_event = threading.Event()

    def stop(signum, frame) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        monitor.run_forever(stop_event)
    finally:
        server.shutdown()
        server.server_close()
        publisher.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
