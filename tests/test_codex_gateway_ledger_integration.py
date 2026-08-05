from __future__ import annotations

import asyncio
import hashlib
from http.server import ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest

import aiohttp

from codex_controller.api import create_server as create_controller_server
from codex_controller.store import ControllerStore
from codex_controller.tool_catalog import MEMBER_READ_ONLY_TOOL_NAMES
from codex_controller.tool_proxy import ToolProxyError, ToolRouter
from renovation_hub.api import create_server as create_ledger_server
from renovation_hub.ledger import LedgerStore
from weixin_gateway.api import create_server as create_gateway_server
from weixin_gateway.service import ControllerClient, GatewayService
from weixin_gateway.store import GatewayStore, IdentityStore


def controller_job(message_id: str, *, profile: str | None = None) -> dict:
    payload = {
        "version": 1,
        "message_id": message_id,
        "conversation_key": "sha256:" + hashlib.sha256(f"weixin:{message_id}".encode()).hexdigest(),
        "received_at": "2026-08-05T12:00:00+08:00",
        "text": "查询装修支出",
        "attachments": [],
        "reply_capabilities": ["text"],
    }
    if profile is not None:
        payload["capability_profile"] = profile
    return payload


class AttachmentBridgeIntegrationTests(unittest.TestCase):
    def test_gateway_reference_reaches_ledger_without_exposing_gateway_token(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            gateway_store = GatewayStore(root / "gateway" / "gateway.sqlite3", data_dir=root / "gateway")
            message = gateway_store.store_message(
                message_id="fixture-integration-message",
                sender_id="fixture-owner",
                conversation_key="sha256:fixture",
                text="记录附件",
                media=[
                    (
                        {"media_type": "image", "filename": "receipt.jpg", "mime_type": "image/jpeg"},
                        b"synthetic-image-content",
                    )
                ],
            )
            attachment_ref = message["attachments"][0]["attachment_ref"]

            ledger_store = LedgerStore(
                root / "ledger" / "ledger.sqlite3",
                data_dir=root / "ledger",
                share_dir=root / "share",
            )
            ledger_store.set_writer_mode("read_only", force_initial=True)
            ledger_store.set_writer_mode("shadow_validated")
            ledger_store.set_writer_mode("cutover_ready")
            ledger_store.set_writer_mode("primary_writer")
            payment = ledger_store.add_payment(
                {
                    "idempotency_key": "fixture-payment-integration",
                    "amount_cents": 100,
                    "occurred_on": "2026-08-03",
                    "main_category": "测试",
                }
            )["transaction"]

            gateway_token = "g" * 32
            ledger_token = "l" * 32
            gateway_service = SimpleNamespace(store=gateway_store, poller_state="disabled")
            gateway = create_gateway_server(
                "127.0.0.1",
                0,
                service=gateway_service,  # type: ignore[arg-type]
                loop=None,  # type: ignore[arg-type]
                attachment_api_token=gateway_token,
            )
            ledger = create_ledger_server(
                "127.0.0.1",
                0,
                store=ledger_store,
                api_token=ledger_token,
                max_request_bytes=32 * 1024 * 1024,
            )
            servers: list[ThreadingHTTPServer] = [gateway, ledger]
            threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in servers]
            for thread in threads:
                thread.start()
            try:
                router = ToolRouter(
                    ledger_base_url=f"http://localhost:{ledger.server_port}",
                    ledger_token=ledger_token,
                    gateway_base_url=f"http://localhost:{gateway.server_port}",
                    gateway_token=gateway_token,
                )
                router.begin_job("fixture-integration-job", "fixture-integration-message")
                result = router.call(
                    "ledger_attach",
                    {
                        "idempotency_key": "fixture-attachment-integration",
                        "transaction_id": payment["id"],
                        "attachment_ref": attachment_ref,
                    },
                )
                self.assertEqual(result["result"]["attachment"]["original_filename"], "receipt.jpg")
                shown = ledger_store.show(payment["id"])
                self.assertEqual(len(shown["attachments"]), 1)
                self.assertEqual(shown["attachments"][0]["mime_type"], "image/jpeg")
                self.assertNotIn("attachment_ref", result["result"]["attachment"])
            finally:
                for server in servers:
                    server.shutdown()
                    server.server_close()
                for thread in threads:
                    thread.join(timeout=2)


class ControllerGatewayCapabilityIntegrationTests(unittest.TestCase):
    def test_controller_capability_shape_is_consumed_and_old_gateway_payload_stays_compatible(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = ControllerStore(Path(temporary) / "controller.sqlite3")

            class Service:
                def __init__(self) -> None:
                    self.store = store

                @staticmethod
                def capabilities() -> dict:
                    return {
                        "capabilities": [
                            "job_capability_profile_v1",
                            "thread_short_v1",
                            "mcp_tool_policy_v1",
                        ]
                    }

                @staticmethod
                def submit(payload: dict) -> dict:
                    return store.public_job(store.create_job(payload))

            token = "c" * 32
            server = create_controller_server(
                "127.0.0.1",
                0,
                service=Service(),  # type: ignore[arg-type]
                api_token=token,
                max_request_bytes=1024 * 1024,
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()

            async def scenario() -> None:
                async with aiohttp.ClientSession(trust_env=False) as session:
                    client = ControllerClient(
                        f"http://localhost:{server.server_port}",
                        token,
                        session=session,
                    )
                    self.assertTrue(await client.supports_capability("job_capability_profile_v1"))

                    legacy = await client.submit(controller_job("legacy-owner-message"))
                    self.assertRegex(legacy["job_id"], r"^[0-9a-f-]{36}$")
                    private_legacy = store.get_job(legacy["job_id"])
                    self.assertEqual(private_legacy["capability_profile"], "owner_legacy")
                    claimed = store.claim_next()
                    assert claimed is not None
                    store.assign_thread(claimed["job_id"], "thread-private-legacy")
                    public_legacy = await client.job(legacy["job_id"])
                    self.assertRegex(public_legacy["thread_short"], r"^TH-[A-Z2-7]{10}$")
                    serialized = str(public_legacy)
                    for forbidden in (
                        "thread-private-legacy",
                        private_legacy["conversation_key"],
                        private_legacy["message_id"],
                        "owner_legacy",
                    ):
                        self.assertNotIn(forbidden, serialized)

                    member = await client.submit(
                        controller_job("member-message", profile="member_read_only")
                    )
                    self.assertEqual(
                        store.get_job(member["job_id"])["capability_profile"],
                        "member_read_only",
                    )

            try:
                asyncio.run(scenario())
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_member_profile_has_exact_eight_tools_and_rejects_writer_before_upstream(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = ControllerStore(Path(temporary) / "controller.sqlite3")
            upstream_calls: list[tuple] = []

            def request_json(*args, **kwargs):
                upstream_calls.append((args, kwargs))
                return {"result": {}}

            router = ToolRouter(
                ledger_base_url="http://renovation-hub:8101",
                ledger_token="l" * 32,
                store=store,
                request_json=request_json,
            )
            router.begin_job(
                "fixture-member-job",
                "fixture-member-message",
                "member_read_only",
            )
            self.assertEqual(set(router.available_tools("member_read_only")), set(MEMBER_READ_ONLY_TOOL_NAMES))
            self.assertEqual(len(MEMBER_READ_ONLY_TOOL_NAMES), 8)
            with self.assertRaises(ToolProxyError) as context:
                router.call(
                    "ledger_add_payment",
                    {
                        "amount_cents": 100,
                        "occurred_on": "2026-08-05",
                        "main_category": "测试",
                    },
                )
            self.assertEqual(context.exception.code, "tool_not_allowed_for_profile")
            self.assertEqual(upstream_calls, [])


class ChartArtifactDeliveryIntegrationTests(unittest.TestCase):
    def test_hub_chart_is_captured_by_controller_and_sent_summary_then_image(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ledger_store = LedgerStore(
                root / "ledger" / "ledger.sqlite3",
                data_dir=root / "ledger",
                share_dir=root / "share",
            )
            ledger_store.set_writer_mode("read_only", force_initial=True)
            ledger_store.set_writer_mode("shadow_validated")
            ledger_store.set_writer_mode("cutover_ready")
            ledger_store.set_writer_mode("primary_writer")
            ledger_store.add_payment(
                {
                    "idempotency_key": "chart-artifact-payment",
                    "amount_cents": 12345,
                    "occurred_on": "2026-08-05",
                    "main_category": "家具",
                }
            )
            ledger_token = "l" * 32
            ledger = create_ledger_server(
                "127.0.0.1",
                0,
                store=ledger_store,
                api_token=ledger_token,
                max_request_bytes=1024 * 1024,
            )
            ledger_thread = threading.Thread(target=ledger.serve_forever, daemon=True)
            ledger_thread.start()
            controller_store = ControllerStore(root / "controller" / "controller.sqlite3")
            payload = controller_job("chart-artifact-message")
            payload["reply_capabilities"] = ["text", "image"]
            controller_store.create_job(payload)
            running = controller_store.claim_next()
            assert running is not None
            router = ToolRouter(
                ledger_base_url=f"http://localhost:{ledger.server_port}",
                ledger_token=ledger_token,
                store=controller_store,
            )
            router.begin_job(running["job_id"], payload["message_id"], "owner")
            tool_result = router.call("ledger_generate_chart", {})
            self.assertEqual(tool_result["delivery"], "weixin_gateway_automatic")
            self.assertNotIn("download_ref", str(tool_result))
            controller_store.assign_turn(running["job_id"], "chart-artifact-turn")
            controller_store.complete_turn("chart-artifact-turn", "completed")

            class ControllerService:
                def __init__(self) -> None:
                    self.store = controller_store

                @staticmethod
                def capabilities() -> dict:
                    return {"capabilities": ["job_artifacts_v1"]}

            controller_token = "c" * 32
            controller = create_controller_server(
                "127.0.0.1",
                0,
                service=ControllerService(),  # type: ignore[arg-type]
                api_token=controller_token,
                max_request_bytes=1024 * 1024,
            )
            controller_thread = threading.Thread(target=controller.serve_forever, daemon=True)
            controller_thread.start()

            class Ilink:
                def __init__(self) -> None:
                    self.events: list[tuple[str, object]] = []

                async def send_text(self, _user: str, text: str, _context: str | None, client_id: str) -> dict:
                    self.events.append(("text", {"text": text, "client_id": client_id}))
                    return {"ret": 0}

                async def send_media(self, _user: str, path: Path, _context: str | None, client_id: str) -> str:
                    self.events.append(("media", {"content": path.read_bytes(), "client_id": client_id}))
                    return client_id

                async def close(self) -> None:
                    return None

            async def scenario() -> None:
                async with aiohttp.ClientSession(trust_env=False) as session:
                    controller_client = ControllerClient(
                        f"http://localhost:{controller.server_port}",
                        controller_token,
                        session=session,
                    )
                    public_job = await controller_client.job(running["job_id"])
                    self.assertEqual(public_job["state"], "completed")
                    self.assertEqual(len(public_job["artifacts"]), 1)
                    identity_store = IdentityStore(root / "gateway")
                    identity_store.save_identity(
                        {
                            "account_id": "fixture-account",
                            "token": "fixture-ilink-token-0000000000000000",
                            "base_url": "https://ilinkai.weixin.qq.com",
                            "cdn_base_url": "https://novac2c.cdn.weixin.qq.com/c2c",
                            "user_id": "fixture-bot-user",
                            "allowed_user_ids": ["fixture-owner"],
                            "get_updates_buf": "",
                            "context_tokens": {},
                        }
                    )
                    gateway_store = GatewayStore(
                        root / "gateway" / "gateway.sqlite3",
                        data_dir=root / "gateway",
                    )
                    gateway_service = GatewayService(
                        identity_store=identity_store,
                        store=gateway_store,
                        controller=controller_client,
                        bootstrap_identity={},
                        poller_enabled=False,
                        owner_pairing_enabled=False,
                        activation_confirmation="",
                        max_media_bytes=20 * 1024 * 1024,
                        controller_ingress_base_url="https://ha.example/api/hassio_ingress/controller",
                    )
                    ilink = Ilink()
                    gateway_service.client = ilink  # type: ignore[assignment]
                    owner = gateway_store.user_by_private_id("fixture-owner")
                    assert owner is not None
                    suppression = await gateway_service._send_completed_job(
                        {
                            "controller_job_id": running["job_id"],
                            "sender_id": "fixture-owner",
                            "user_hash": owner["user_hash"],
                            "capability_profile": "owner",
                        },
                        public_job,
                    )
                    self.assertIsNone(suppression)
                    self.assertEqual([event[0] for event in ilink.events], ["text", "media"])
                    self.assertIn("共 1 笔记录", ilink.events[0][1]["text"])
                    self.assertTrue(ilink.events[1][1]["content"].startswith(b"\x89PNG\r\n\x1a\n"))
                    self.assertNotIn("http", ilink.events[0][1]["text"])

            try:
                asyncio.run(scenario())
            finally:
                controller.shutdown()
                controller.server_close()
                controller_thread.join(timeout=2)
                ledger.shutdown()
                ledger.server_close()
                ledger_thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
