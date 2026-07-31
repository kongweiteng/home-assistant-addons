"""Behavior and packaging tests for the MQTT notification bridge."""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
ADDON = ROOT / "hermes_agent"
BRIDGE_PATH = ADDON / "notification_bridge.py"
BRIDGE_SHELL = ADDON / "notification-bridge.sh"
CONFIG = ADDON / "config.yaml"
RUN_SH = ADDON / "run.sh"
DOCKERFILE = ADDON / "Dockerfile"
README = ROOT / "README.md"
CHANGELOG = ADDON / "CHANGELOG.md"
BASH = "/bin/bash" if sys.platform == "darwin" else "bash"


def _load_bridge_module():
    spec = importlib.util.spec_from_file_location(
        "hermes_addon_notification_bridge", BRIDGE_PATH
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load notification bridge")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


BRIDGE = _load_bridge_module()


def _config(tmp: Path):
    return BRIDGE.BridgeConfig(
        mqtt_host="broker.example",
        mqtt_port=1883,
        mqtt_username="bridge",
        mqtt_password="not-a-real-secret",
        mqtt_tls=False,
        mqtt_client_id="hermes-notification-bridge-v1",
        allowed_audiences=frozenset({"owner"}),
        hermes_bin="/usr/local/bin/hermes",
        hermes_home=str(tmp / "profile"),
        ledger_path=tmp / "notification-ledger.sqlite3",
        addon_version="1.5.2",
    )


def _payload(
    now: dt.datetime,
    *,
    message_id: str = "ha-message-1",
    dedupe_key: str = "test_notice",
    ttl: int = 600,
):
    return {
        "version": 1,
        "message_id": message_id,
        "created_at": now.isoformat(),
        "level": "warning",
        "title": "Test title",
        "message": "Test message",
        "source": "automation.test_notice",
        "dedupe_key": dedupe_key,
        "ttl": ttl,
        "audience": "owner",
    }


class AddonPackagingTests(unittest.TestCase):
    def test_feature_is_opt_in_and_versioned(self):
        config = CONFIG.read_text()
        self.assertIn('version: "1.7.0"', config)
        self.assertIn("notification_bridge_enabled: false", config)
        self.assertIn('notification_mqtt_host: "core-mosquitto"', config)
        self.assertIn('notification_mqtt_username: "str?"', config)
        self.assertIn('notification_mqtt_password: "password?"', config)
        self.assertRegex(
            config,
            r'(?ms)notification_allowed_audiences:\n\s+- "owner"',
        )

    def test_container_pins_runtime_and_copies_bridge_files(self):
        dockerfile = DOCKERFILE.read_text()
        self.assertIn("paho-mqtt==2.1.0", dockerfile)
        self.assertIn("--break-system-packages", dockerfile)
        self.assertLess(
            dockerfile.index("/home/linuxbrew/.linuxbrew/bin/brew --version"),
            dockerfile.index("paho-mqtt==2.1.0"),
        )
        self.assertIn(
            "COPY notification_bridge.py /usr/local/bin/hermes-notification-bridge",
            dockerfile,
        )
        self.assertIn(
            "COPY notification-bridge.sh /usr/local/lib/hermes-notification-bridge.sh",
            dockerfile,
        )

    def test_run_script_manages_bridge_lifecycle(self):
        run = RUN_SH.read_text()
        self.assertIn("notification_bridge_validate_options", run)
        self.assertIn("notification_bridge_start", run)
        self.assertIn("notification_bridge_supervise", run)
        self.assertIn("notification_bridge_stop", run)
        self.assertNotIn("NOTIFICATION_MQTT_PASSWORD=\"$NOTIFICATION_MQTT_PASSWORD\"\nexport", run)

    def test_documentation_freezes_topics_and_model_free_path(self):
        readme = README.read_text()
        changelog = CHANGELOG.read_text()
        for text in (readme, changelog):
            self.assertIn("home/notification/v1/request", text)
            self.assertRegex(text, r"(?i)without model|does not call a model|model execution")
        self.assertIn("hermes send -q --to weixin --file -", readme)
        self.assertIn("non-retained request/result topics", changelog)

    def test_shell_validation_fails_closed_when_enabled_without_credentials(self):
        script = f"""
            set -euo pipefail
            source {BRIDGE_SHELL}
            NOTIFICATION_BRIDGE_ENABLED=true
            NOTIFICATION_MQTT_HOST=core-mosquitto
            NOTIFICATION_MQTT_PORT=1883
            NOTIFICATION_MQTT_USERNAME=''
            NOTIFICATION_MQTT_PASSWORD=''
            NOTIFICATION_ALLOWED_AUDIENCES=owner
            notification_bridge_validate_options
        """
        result = subprocess.run(
            [BASH, "-c", script], text=True, capture_output=True, check=False
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("username and password", result.stderr)

    def test_shell_validation_allows_disabled_feature_without_credentials(self):
        script = f"""
            set -euo pipefail
            source {BRIDGE_SHELL}
            NOTIFICATION_BRIDGE_ENABLED=false
            notification_bridge_validate_options
        """
        result = subprocess.run(
            [BASH, "-c", script], text=True, capture_output=True, check=False
        )
        self.assertEqual(result.returncode, 0, result.stderr)


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.now = dt.datetime(2026, 7, 30, 12, 0, tzinfo=dt.timezone.utc)

    def test_valid_request_and_deterministic_weixin_text(self):
        request = BRIDGE.parse_request(
            _payload(self.now), allowed_audiences={"owner"}, now=self.now
        )
        self.assertEqual(request.expires_at, self.now + dt.timedelta(seconds=600))
        self.assertEqual(
            BRIDGE.format_weixin_text(request),
            "【警告】Test title\nTest message",
        )

    def test_validation_reports_stable_error_codes_and_message_id(self):
        cases = [
            ({**_payload(self.now), "version": 2}, "unsupported_version"),
            ({**_payload(self.now), "ttl": 10}, "invalid_ttl"),
            ({**_payload(self.now), "level": "debug"}, "invalid_level"),
            ({**_payload(self.now), "audience": "unknown"}, "audience_not_allowed"),
            ({**_payload(self.now), "title": ""}, "invalid_payload"),
        ]
        for payload, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(BRIDGE.RequestValidationError) as raised:
                    BRIDGE.parse_request(
                        payload, allowed_audiences={"owner"}, now=self.now
                    )
                self.assertEqual(raised.exception.code, code)
                self.assertEqual(raised.exception.message_id, "ha-message-1")

    def test_naive_and_future_timestamps_are_rejected(self):
        naive = {**_payload(self.now), "created_at": "2026-07-30T12:00:00"}
        future = {
            **_payload(self.now),
            "created_at": (self.now + dt.timedelta(minutes=6)).isoformat(),
        }
        for payload in (naive, future):
            with self.assertRaises(BRIDGE.RequestValidationError):
                BRIDGE.parse_request(
                    payload, allowed_audiences={"owner"}, now=self.now
                )

    def test_discovery_contains_only_diagnostic_state_topics(self):
        messages = BRIDGE.discovery_messages("1.5.2")
        self.assertEqual(len(messages), 2)
        self.assertIn(
            "homeassistant/binary_sensor/hermes_notification_bridge_online/config",
            messages,
        )
        result = messages[
            "homeassistant/sensor/hermes_notification_last_result/config"
        ]
        self.assertEqual(result["state_topic"], BRIDGE.RESULT_TOPIC)
        self.assertEqual(result["availability_topic"], BRIDGE.STATUS_TOPIC)
        self.assertNotIn("message", json.dumps(messages))


class ProcessorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "profile").mkdir()
        self.config = _config(self.root)
        self.ledger = BRIDGE.Ledger(self.config.ledger_path)
        self.now = dt.datetime(2026, 7, 30, 12, 0, tzinfo=dt.timezone.utc)
        self.events = []
        self.commands = []

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def publish(self, payload):
        self.events.append(payload)
        return True

    def runner(self, args, **kwargs):
        self.commands.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, "", "")

    def processor(self, **kwargs):
        return BRIDGE.NotificationProcessor(
            config=self.config,
            ledger=self.ledger,
            publish_result=self.publish,
            runner=kwargs.pop("runner", self.runner),
            sleeper=kwargs.pop("sleeper", lambda _delay: None),
            clock=kwargs.pop("clock", lambda: self.now),
            retry_delays=kwargs.pop("retry_delays", ()),
            **kwargs,
        )

    def test_success_sends_exact_text_without_model_arguments(self):
        with mock.patch.dict(
            os.environ,
            {
                "NOTIFICATION_MQTT_USERNAME": "bridge",
                "NOTIFICATION_MQTT_PASSWORD": "bridge-secret",
                "NOTIFICATION_ALLOWED_AUDIENCES": "owner",
            },
        ):
            safe = self.processor().process(json.dumps(_payload(self.now)).encode())
        self.assertTrue(safe)
        self.assertEqual(self.events[-1]["status"], "sent")
        args, kwargs = self.commands[0]
        self.assertEqual(
            args,
            [
                "/usr/local/bin/hermes",
                "send",
                "-q",
                "--to",
                "weixin",
                "--file",
                "-",
            ],
        )
        self.assertEqual(kwargs["input"], "【警告】Test title\nTest message")
        self.assertNotIn("model", " ".join(args).lower())
        self.assertFalse(
            any(name.startswith("NOTIFICATION_") for name in kwargs["env"])
        )

    def test_same_message_id_is_never_sent_twice(self):
        processor = self.processor()
        payload = json.dumps(_payload(self.now)).encode()
        self.assertTrue(processor.process(payload))
        self.assertTrue(processor.process(payload))
        self.assertEqual(len(self.commands), 1)
        self.assertEqual(self.events[-1]["status"], "sent")

    def test_dedupe_key_blocks_a_second_business_notification(self):
        processor = self.processor()
        first = _payload(self.now, message_id="ha-message-1")
        second = _payload(self.now, message_id="ha-message-2")
        self.assertTrue(processor.process(json.dumps(first).encode()))
        self.assertTrue(processor.process(json.dumps(second).encode()))
        self.assertEqual(len(self.commands), 1)
        self.assertEqual(self.events[-1]["status"], "duplicate")
        self.assertEqual(self.events[-1]["error_code"], "dedupe_window")

    def test_expired_request_is_recorded_and_not_sent(self):
        payload = _payload(self.now - dt.timedelta(minutes=2), ttl=30)
        self.assertTrue(self.processor().process(json.dumps(payload).encode()))
        self.assertFalse(self.commands)
        self.assertEqual(self.events[-1]["status"], "expired")

    def test_unknown_inflight_state_fails_closed_without_resending(self):
        request = BRIDGE.parse_request(
            _payload(self.now), allowed_audiences={"owner"}, now=self.now
        )
        self.ledger.insert(request, status="sending", now_ts=self.now.timestamp())
        self.ledger.update(
            request.message_id,
            status="sending",
            attempts=1,
            error_code=None,
        )
        self.assertTrue(
            self.processor().process(json.dumps(_payload(self.now)).encode())
        )
        self.assertFalse(self.commands)
        self.assertEqual(self.events[-1]["error_code"], "delivery_state_unknown")

    def test_recoverable_failure_retries_then_succeeds(self):
        calls = []

        def flaky(args, **kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(
                args, 1 if len(calls) == 1 else 0, "", "temporary"
            )

        processor = self.processor(runner=flaky, retry_delays=(5,))
        self.assertTrue(processor.process(json.dumps(_payload(self.now)).encode()))
        self.assertEqual(len(calls), 2)
        self.assertIn("retrying", [event["status"] for event in self.events])
        self.assertEqual(self.events[-1]["status"], "sent")

    def test_invalid_json_returns_failed_result_without_sending(self):
        self.assertTrue(self.processor().process(b"not-json"))
        self.assertFalse(self.commands)
        self.assertEqual(self.events[-1]["error_code"], "invalid_json")


class RuntimeTests(unittest.TestCase):
    def test_runtime_preserves_the_mqtt_v5_session_on_first_connect(self):
        class FakeProperties:
            def __init__(self, packet_type):
                self.packet_type = packet_type

        class FakeClient:
            def __init__(self, **kwargs):
                self.init_kwargs = kwargs
                self.connect_call = None

            def username_pw_set(self, username, password):
                self.username = username
                self.password = password

            def reconnect_delay_set(self, **kwargs):
                self.reconnect_delay = kwargs

            def will_set(self, *args, **kwargs):
                self.will = (args, kwargs)

            def connect(self, *args, **kwargs):
                self.connect_call = (args, kwargs)

        class FakeMqtt:
            class PacketTypes:
                CONNECT = 1

            class CallbackAPIVersion:
                VERSION2 = 2

            MQTTv5 = 5
            Properties = FakeProperties
            Client = FakeClient

        with tempfile.TemporaryDirectory() as tmp:
            runtime = BRIDGE.BridgeRuntime(_config(Path(tmp)), FakeMqtt)
            try:
                runtime._connect()
                args, kwargs = runtime.client.connect_call
                self.assertEqual(args, ("broker.example", 1883))
                self.assertFalse(kwargs["clean_start"])
                self.assertEqual(kwargs["keepalive"], 60)
                self.assertEqual(
                    kwargs["properties"].SessionExpiryInterval,
                    24 * 60 * 60,
                )
                self.assertTrue(runtime.client.init_kwargs["manual_ack"])
            finally:
                runtime.ledger.close()


if __name__ == "__main__":
    unittest.main()
