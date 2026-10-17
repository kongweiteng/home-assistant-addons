"""Exercise steer against the pinned binary and an entirely local Responses fixture."""
from __future__ import annotations

import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import struct
import zlib
from pathlib import Path
import tempfile
import threading
import unittest

from codex_controller.app_server import AppServerClient, AppServerError


class NativeFollowupRuntimeTests(unittest.TestCase):
    def test_exact_turn_precondition_and_text_image_steer(self) -> None:
        binary = os.environ.get("CODEX_BINARY", "")
        if not binary or not Path(binary).is_file():
            self.skipTest("CODEX_BINARY unavailable")
        arrived = threading.Event()
        release = threading.Event()
        completed = threading.Event()
        requests: list[dict] = []

        class Responses(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args: object) -> None:
                pass

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length))
                requests.append(body)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.end_headers()
                response_id = f"resp_fixture_{len(requests)}"
                def emit(kind: str, **fields: object) -> None:
                    event = {"type": kind, **fields}
                    self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode())
                    self.wfile.flush()
                try:
                    emit("response.created", response={"id": response_id, "status": "in_progress"})
                    arrived.set()
                    release.wait(15)
                    message = {"id": "msg_fixture", "type": "message", "role": "assistant",
                               "status": "completed", "content": [{"type": "output_text", "text": "combined fixture answer", "annotations": []}]}
                    emit("response.output_item.added", output_index=0,
                         item={**message, "status": "in_progress", "content": []})
                    emit("response.output_text.delta", item_id="msg_fixture", output_index=0,
                         content_index=0, delta="combined fixture answer")
                    emit("response.output_item.done", output_index=0, item=message)
                    emit("response.completed", response={"id": response_id, "status": "completed",
                         "output": [message], "usage": {"input_tokens": 10, "output_tokens": 3, "total_tokens": 13}})
                except (BrokenPipeError, ConnectionResetError):
                    pass
                self.close_connection = True

        server = ThreadingHTTPServer(("127.0.0.1", 0), Responses)
        server.daemon_threads = True
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        events: list[dict] = []
        def observe(event: dict) -> None:
            events.append(event)
            if event.get("method") == "turn/completed":
                completed.set()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            home = root / "codex-home"
            home.mkdir()
            (home / "config.toml").write_text(
                'model = "gpt-5.6-sol"\n'
                f'openai_base_url = "http://127.0.0.1:{server.server_port}/v1"\n'
                'model_reasoning_effort = "low"\n'
                '[features]\nresponses_websockets_v2 = false\n'
            )
            image = root / "fixture.png"
            def png_chunk(kind: bytes, data: bytes) -> bytes:
                return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
            image.write_bytes(b"\x89PNG\r\n\x1a\n" +
                png_chunk(b"IHDR", struct.pack(">IIBBBBB", 64, 64, 8, 2, 0, 0, 0)) +
                png_chunk(b"IDAT", zlib.compress((b"\x00" + b"\x00\x80\xff" * 64) * 64)) +
                png_chunk(b"IEND", b""))
            client = AppServerClient(
                [binary, "app-server", "--disable", "goals", "--disable", "shell_tool", "--disable", "unified_exec", "--listen", "stdio://"],
                codex_home=home, workspace=root / "workspace", request_timeout=15,
                notification_handler=observe,
            )
            try:
                client.start()
                client.request("account/login/start", {"type": "apiKey", "apiKey": "fixture-only-not-a-secret"})
                thread_id = client.start_thread()
                turn_id = client.start_turn(thread_id, "Initial fixture question", "fixture-initial")
                self.assertTrue(arrived.wait(10), "local Responses fixture not reached")
                with self.assertRaises(AppServerError):
                    client.request("turn/steer", {"threadId": thread_id, "expectedTurnId": "wrong-fixture-turn",
                        "input": [{"type": "text", "text": "must not apply"}]})
                result = client.request("turn/steer", {"threadId": thread_id, "expectedTurnId": turn_id,
                    "clientUserMessageId": "fixture-followup",
                    "input": [{"type": "text", "text": "Followup fixture correction"},
                              {"type": "localImage", "path": str(image), "detail": "auto"}]})
                self.assertEqual(result.get("turnId"), turn_id)
                release.set()
                self.assertTrue(completed.wait(15), "native turn did not finish")
                self.assertEqual(events[-1]["params"]["turn"]["id"], turn_id)
                serialized = json.dumps(requests)
                self.assertTrue("Followup fixture correction" in serialized, "steered text did not reach synthetic model")
                self.assertTrue("input_image" in serialized, "steered image did not reach synthetic model")
                self.assertTrue("must not apply" not in serialized, "mismatched-turn text leaked into model")
            finally:
                release.set()
                client.stop()
                server.shutdown()
                server.server_close()
                server_thread.join(timeout=3)
