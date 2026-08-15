from __future__ import annotations

from pathlib import Path
import subprocess
import unittest

import aiohttp
from aiohttp.test_utils import TestServer

from codex_runner_relay import __version__
from codex_runner_relay.app import ConnectionRate, RelayHub, create_app
from codex_runner_relay.controller import validate_controller_base_url
from codex_runner_relay.protocol import (
    RelayProtocolError,
    validate_event_message,
    validate_first_message,
    validate_publish,
)


RUNNER_ID = "RN-ABCDEFGHIJKLMNOPQRST"
CREDENTIAL = "CRED-" + "A" * 48
TOKEN = "ENROLL-" + "B" * 48


class RelayProtocolUnitTests(unittest.TestCase):
    def test_addon_version_and_controller_hostname_contract_are_exact(self) -> None:
        root = Path(__file__).resolve().parents[1] / "codex_runner_relay"
        config = (root / "config.yaml").read_text(encoding="utf-8")
        run_script = (root / "run.sh").read_text(encoding="utf-8")
        self.assertEqual(__version__, "0.2.4")
        self.assertIn('version: "0.2.4"', config)
        self.assertIn('controller_base_url: "http://local-codex-controller:8102"', config)
        self.assertIn("local-codex-controller", run_script)
        self.assertNotIn("http://codex-controller:8102", config + run_script)
        self.assertEqual(
            validate_controller_base_url("http://local-codex-controller:8102/"),
            "http://local-codex-controller:8102",
        )
        for value in (
            "http://codex-controller:8102",
            "https://local-codex-controller:8102",
            "http://local-codex-controller",
            "http://local-codex-controller:8103",
            "http://local-codex-controller:8102/admin",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_controller_base_url(value)

    def test_first_frame_never_accepts_secret_in_url_or_extra_fields(self) -> None:
        authenticated = validate_first_message(
            {"type": "authenticate", "runner_id": RUNNER_ID, "credential": CREDENTIAL}
        )
        self.assertEqual(authenticated["runner_id"], RUNNER_ID)
        with self.assertRaises(RelayProtocolError):
            validate_first_message(
                {
                    "type": "authenticate",
                    "runner_id": RUNNER_ID,
                    "credential": CREDENTIAL,
                    "query_token": TOKEN,
                }
            )

    def test_event_and_publish_are_bound_to_one_runner(self) -> None:
        event_type, document = validate_event_message(
            {
                "type": "event",
                "event_type": "heartbeat",
                "document": {"message_type": "heartbeat", "runner_id": RUNNER_ID},
            },
            runner_id=RUNNER_ID,
        )
        self.assertEqual((event_type, document["runner_id"]), ("heartbeat", RUNNER_ID))
        with self.assertRaises(RelayProtocolError):
            validate_publish(
                "request",
                RUNNER_ID,
                {
                    "document": {
                        "message_type": "request",
                        "runner_id": "RN-ZYXWVUTSRQPONMLKJIHG",
                    }
                },
            )

    def test_rate_limit_is_bounded_per_connection(self) -> None:
        rate = ConnectionRate(2)
        self.assertTrue(rate.allow(1.0))
        self.assertTrue(rate.allow(2.0))
        self.assertFalse(rate.allow(3.0))
        self.assertTrue(rate.allow(62.0))


class FakeController:
    def __init__(self) -> None:
        self.started = False
        self.closed = False
        self.enrollments: list[dict] = []
        self.authentications: list[tuple[str, str]] = []
        self.events: list[tuple[str, dict, str]] = []
        self.install_tickets: list[str] = []

    async def start(self) -> None:
        self.started = True

    async def close(self) -> None:
        self.closed = True

    async def enroll(self, payload: dict) -> dict:
        self.enrollments.append(dict(payload))
        return {
            "runner": {"runner_id": payload["runner_id"], "admin_state": "pending"},
            "credential": {"credential_id": "CR-" + "C" * 24, "secret": CREDENTIAL},
        }

    async def install_bootstrap(self, ticket: str) -> dict:
        self.install_tickets.append(ticket)
        return {
            "runner_id": RUNNER_ID,
            "enrollment_token": ticket,
            "relay_url": "wss://runner.example.com/v1/runner",
            "os": "macos",
            "arch": "aarch64",
            "projects": ["renovation-hub"],
            "labels": ["local", "macos"],
            "policy_revision": 2,
            "asset_url": "https://downloads.example.com/codex-runner-0.3.4-macos-aarch64.tar.gz",
            "asset_sha256": "a" * 64,
            "asset_size": 123456,
            "installer_url": "https://downloads.example.com/codex-runner-installer-2.sh",
            "installer_sha256": "b" * 64,
            "installer_size": 4567,
            "runner_version": "0.3.4",
            "codex_version": "0.146.0",
            "python_version": "3.11.13",
            "self_contained": True,
        }

    async def authenticate(self, runner_id: str, credential: str) -> dict:
        self.authentications.append((runner_id, credential))
        return {"authenticated": True, "runner_id": runner_id}

    async def event(self, event_type: str, document: dict, *, credential: str) -> dict:
        self.events.append((event_type, dict(document), credential))
        return {"accepted": True}


class RelayIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.controller = FakeController()
        self.hub = RelayHub(
            self.controller,  # type: ignore[arg-type]
            api_token="R" * 32,
            max_connections=4,
            max_message_bytes=32768,
            first_frame_timeout_seconds=2,
            messages_per_minute=10,
        )
        self.server = TestServer(create_app(self.hub))
        await self.server.start_server()
        self.session = aiohttp.ClientSession()

    async def asyncTearDown(self) -> None:
        await self.session.close()
        await self.server.close()

    def ws_url(self) -> str:
        return str(self.server.make_url("/v1/runner")).replace("http://", "ws://", 1)

    async def enroll(self) -> aiohttp.ClientWebSocketResponse:
        ws = await self.session.ws_connect(self.ws_url())
        await ws.send_json(
            {
                "type": "enroll",
                "runner_id": RUNNER_ID,
                "token": TOKEN,
                "payload": {
                    "runner_id": RUNNER_ID,
                    "protocol_version": 2,
                    "agent_version": "0.3.4",
                    "labels": ["local", "macos"],
                    "policy_revision": 2,
                },
            }
        )
        response = await ws.receive_json(timeout=2)
        self.assertEqual(response["type"], "enrolled")
        self.assertEqual(response["credential"], CREDENTIAL)
        return ws

    async def test_enrollment_publish_and_event_round_trip(self) -> None:
        ws = await self.enroll()
        request = {"message_type": "request", "runner_id": RUNNER_ID, "task_id": "RW-FIXTURE"}
        async with self.session.post(
            self.server.make_url(f"/internal/v1/runners/{RUNNER_ID}/request"),
            json={"document": request},
            headers={"Authorization": "Bearer " + "R" * 32},
        ) as response:
            self.assertEqual(response.status, 202)
        delivered = await ws.receive_json(timeout=2)
        self.assertEqual(delivered, {"type": "request", "document": request})

        heartbeat = {
            "message_type": "heartbeat",
            "runner_id": RUNNER_ID,
            "body_digest": "sha256:" + "a" * 64,
        }
        await ws.send_json({"type": "event", "event_type": "heartbeat", "document": heartbeat})
        ack = await ws.receive_json(timeout=2)
        self.assertEqual(ack["type"], "ack")
        self.assertEqual(self.controller.events, [("heartbeat", heartbeat, CREDENTIAL)])
        await ws.close()

    async def test_internal_publish_requires_bearer_and_runner_must_be_online(self) -> None:
        async with self.session.post(
            self.server.make_url(f"/internal/v1/runners/{RUNNER_ID}/request"),
            json={"document": {"message_type": "request", "runner_id": RUNNER_ID}},
        ) as response:
            self.assertEqual(response.status, 401)
        async with self.session.post(
            self.server.make_url(f"/internal/v1/runners/{RUNNER_ID}/request"),
            json={"document": {"message_type": "request", "runner_id": RUNNER_ID}},
            headers={"Authorization": "Bearer " + "R" * 32},
        ) as response:
            self.assertEqual(response.status, 503)

    async def test_second_connection_for_same_runner_is_rejected(self) -> None:
        first = await self.enroll()
        second = await self.session.ws_connect(self.ws_url())
        await second.send_json(
            {"type": "authenticate", "runner_id": RUNNER_ID, "credential": CREDENTIAL}
        )
        error = await second.receive_json(timeout=2)
        self.assertEqual(error, {"type": "error", "code": "runner_already_connected"})
        self.assertEqual(self.controller.authentications, [])
        await first.close()
        await second.close()

    async def test_duplicate_enrollment_is_rejected_before_token_consumption(self) -> None:
        first = await self.enroll()
        second = await self.session.ws_connect(self.ws_url())
        await second.send_json(
            {
                "type": "enroll",
                "runner_id": RUNNER_ID,
                "token": TOKEN,
                "payload": {
                    "runner_id": RUNNER_ID,
                    "protocol_version": 2,
                    "agent_version": "0.3.4",
                    "labels": ["local", "macos"],
                    "policy_revision": 2,
                },
            }
        )
        error = await second.receive_json(timeout=2)
        self.assertEqual(error, {"type": "error", "code": "runner_already_connected"})
        self.assertEqual(len(self.controller.enrollments), 1)
        await first.close()
        await second.close()

    async def test_pending_first_frame_counts_toward_connection_capacity(self) -> None:
        self.hub.max_connections = 1
        first = await self.session.ws_connect(self.ws_url())
        with self.assertRaises(aiohttp.WSServerHandshakeError) as raised:
            await self.session.ws_connect(self.ws_url())
        self.assertEqual(raised.exception.status, 503)
        await first.close()

    async def test_health_contains_no_runner_identity_or_secret(self) -> None:
        ws = await self.enroll()
        async with self.session.get(self.server.make_url("/healthz")) as response:
            body = await response.text()
            self.assertEqual(response.status, 200)
            self.assertNotIn(RUNNER_ID, body)
            self.assertNotIn(CREDENTIAL, body)
        await ws.close()

    async def test_install_link_renders_no_store_digest_pinned_shell_without_logging_ticket(self) -> None:
        async with self.session.get(self.server.make_url(f"/install/{TOKEN}")) as response:
            body = await response.text()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
            self.assertTrue(response.headers["Content-Type"].startswith("text/x-shellscript"))
        self.assertEqual(self.controller.install_tickets, [TOKEN])
        self.assertIn("CODEX_RUNNER_ENROLLMENT_TOKEN", body)
        self.assertIn(TOKEN, body)
        self.assertIn("codex-runner-installer-2.sh", body)
        self.assertIn("codex-runner-0.3.4-macos-aarch64.tar.gz", body)
        self.assertIn("--asset-size 123456", body)
        self.assertIn("--projects renovation-hub", body)
        self.assertIn("--labels local,macos", body)
        self.assertIn("--policy-revision 2", body)
        self.assertNotIn("runner_id=", body)
        syntax = subprocess.run(
            ["sh", "-n"], input=body, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        self.assertEqual(syntax.returncode, 0, syntax.stderr)

        async with self.session.get(self.server.make_url("/install/not-a-ticket")) as response:
            invalid = await response.text()
            self.assertEqual(response.status, 404)
            self.assertNotIn("not-a-ticket", invalid)


if __name__ == "__main__":
    unittest.main()
