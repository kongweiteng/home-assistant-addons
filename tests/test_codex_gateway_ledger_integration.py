from __future__ import annotations

from http.server import ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest

from codex_controller.tool_proxy import ToolRouter
from renovation_ledger.api import create_server as create_ledger_server
from renovation_ledger.core import LedgerStore
from weixin_gateway.api import create_server as create_gateway_server
from weixin_gateway.store import GatewayStore


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


if __name__ == "__main__":
    unittest.main()
