from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
from io import BytesIO
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import unittest

import aiohttp
from aiohttp import web
from PIL import Image

from codex_controller.tool_proxy import ToolRouter
from renovation_hub.hub import RenovationHubStore
from renovation_hub.ledger import LedgerError
from renovation_hub.media import MediaService
from renovation_hub.web import create_app
from weixin_gateway.api import create_server as create_gateway_server
from weixin_gateway.store import GatewayStore


def key(label: str) -> str:
    return f"fixture-media-{label}-" + "0" * 20


def jpeg_bytes(color: str = "#B77945") -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (640, 360), color).save(buffer, "JPEG", quality=90)
    return buffer.getvalue()


class MediaServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = RenovationHubStore(self.root / "data" / "hub.sqlite3", data_dir=self.root / "data", share_dir=self.root / "share")
        self.store.set_writer_mode("read_only", force_initial=True)
        self.store.set_writer_mode("shadow_validated")
        self.store.set_writer_mode("cutover_ready")
        self.store.set_writer_mode("primary_writer")
        self.project = self.store.create_project({"idempotency_key": key("project"), "name": "合成项目"})["project"]
        self.media = MediaService(
            self.store,
            media_root=self.root / "media",
            preview_root=self.root / "previews",
            staging_root=self.root / "staging",
            max_media_bytes=50 * 1024 * 1024,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _ingest(self, content: bytes, *, filename: str, mime_type: str, label: str) -> dict:
        prepared = self.media.prepare_upload(
            idempotency_key=key(label),
            source_ref_hash=hashlib.sha256(label.encode()).hexdigest(),
            original_filename=filename,
            mime_type=mime_type,
            expected_bytes=len(content),
        )
        Path(prepared["path"]).write_bytes(content)
        return self.media.finalize_upload(
            prepared,
            received_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            expected_sha256=hashlib.sha256(content).hexdigest(),
            metadata={
                "idempotency_key": key(label),
                "source_ref_hash": hashlib.sha256(label.encode()).hexdigest(),
                "project_id": self.project["id"],
                "source": "fixture",
                "links": [],
            },
            actor_hash="sha256:fixture",
        )

    def test_image_is_content_addressed_and_previewed(self) -> None:
        content = jpeg_bytes()
        result = self._ingest(content, filename="现场.jpg", mime_type="image/jpeg", label="image")
        asset = result["media"]
        self.assertEqual(asset["processing_status"], "ready")
        self.assertEqual((asset["width"], asset["height"]), (640, 360))
        content_path, _ = self.media.content_path(asset["id"])
        preview_path, _ = self.media.content_path(asset["id"], preview=True)
        self.assertEqual(content_path.read_bytes(), content)
        self.assertTrue(preview_path.is_file())
        self.assertEqual(self.media.list({"project_id": self.project["id"], "media_type": "image"})[0]["id"], asset["id"])

    def test_video_is_probed_and_cover_is_generated(self) -> None:
        video_path = self.root / "fixture.mp4"
        subprocess.run(
            ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=#8b5cf6:s=320x180:d=1", "-c:v", "mpeg4", "-y", str(video_path)],
            check=True,
            timeout=30,
        )
        result = self._ingest(video_path.read_bytes(), filename="进度.mp4", mime_type="video/mp4", label="video")
        asset = result["media"]
        self.assertEqual(asset["processing_status"], "ready")
        self.assertEqual((asset["width"], asset["height"]), (320, 180))
        self.assertGreaterEqual(asset["duration_ms"], 900)
        self.assertTrue(self.media.content_path(asset["id"], preview=True)[0].is_file())

    def test_content_deduplication_limit_and_restore_keep_media_invariants(self) -> None:
        content = jpeg_bytes()
        first = self._ingest(content, filename="现场-a.jpg", mime_type="image/jpeg", label="dedupe-a")["media"]
        second = self._ingest(content, filename="现场-b.jpg", mime_type="image/jpeg", label="dedupe-b")["media"]
        self.assertNotEqual(first["id"], second["id"])
        originals = [path for path in self.media.media_root.rglob("*") if path.is_file()]
        self.assertEqual(len(originals), 1)
        self.assertEqual(len(self.media.list({"project_id": self.project["id"], "limit": 1})), 1)
        with self.assertRaises(LedgerError) as context:
            self.media.list({"project_id": self.project["id"], "limit": 1001})
        self.assertEqual(context.exception.code, "invalid_input")

        restore_root = self.root / "restore"
        restore_data = restore_root / "data"
        restore_data.mkdir(parents=True)
        source = sqlite3.connect(self.store.database_path)
        destination = sqlite3.connect(restore_data / "hub.sqlite3")
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()
        shutil.copytree(self.media.media_root, restore_root / "media")
        shutil.copytree(self.media.preview_root, restore_root / "previews")
        restored_store = RenovationHubStore(
            restore_data / "hub.sqlite3",
            data_dir=restore_data,
            share_dir=restore_root / "share",
        )
        restored_media = MediaService(
            restored_store,
            media_root=restore_root / "media",
            preview_root=restore_root / "previews",
            staging_root=restore_root / "staging",
            max_media_bytes=50 * 1024 * 1024,
        )
        restored = restored_media.list({"project_id": self.project["id"]})
        self.assertEqual({item["id"] for item in restored}, {first["id"], second["id"]})
        restored_path, _ = restored_media.content_path(first["id"])
        self.assertEqual(hashlib.sha256(restored_path.read_bytes()).hexdigest(), first["sha256"])

    def test_concurrent_uploads_are_serialized_without_losing_records(self) -> None:
        inputs = [("parallel-a", jpeg_bytes("#9A6B45")), ("parallel-b", jpeg_bytes("#607D5A"))]
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(
                executor.map(
                    lambda item: self._ingest(
                        item[1],
                        filename=f"{item[0]}.jpg",
                        mime_type="image/jpeg",
                        label=item[0],
                    ),
                    inputs,
                )
            )
        self.assertEqual(len({item["media"]["id"] for item in results}), 2)
        self.assertEqual(len(self.media.list({"project_id": self.project["id"]})), 2)

    def test_size_and_hash_failures_remove_staging_content(self) -> None:
        with self.assertRaises(LedgerError) as context:
            self.media.prepare_upload(
                idempotency_key=key("oversize"),
                source_ref_hash=hashlib.sha256(b"oversize").hexdigest(),
                original_filename="过大.jpg",
                mime_type="image/jpeg",
                expected_bytes=self.media.max_media_bytes + 1,
            )
        self.assertEqual(context.exception.code, "media_size_invalid")

        content = jpeg_bytes()
        prepared = self.media.prepare_upload(
            idempotency_key=key("bad-hash"),
            source_ref_hash=hashlib.sha256(b"bad-hash").hexdigest(),
            original_filename="摘要错误.jpg",
            mime_type="image/jpeg",
            expected_bytes=len(content),
        )
        staging_path = Path(prepared["path"])
        staging_path.write_bytes(content)
        with self.assertRaises(LedgerError) as context:
            self.media.finalize_upload(
                prepared,
                received_bytes=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
                expected_sha256="0" * 64,
                metadata={
                    "idempotency_key": key("bad-hash"),
                    "source_ref_hash": hashlib.sha256(b"bad-hash").hexdigest(),
                    "project_id": self.project["id"],
                    "source": "fixture",
                    "links": [],
                },
                actor_hash="sha256:fixture",
            )
        self.assertEqual(context.exception.code, "sha256_mismatch")
        self.assertFalse(staging_path.exists())


class MediaStreamingIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = RenovationHubStore(self.root / "hub" / "hub.sqlite3", data_dir=self.root / "hub", share_dir=self.root / "share")
        self.store.set_writer_mode("read_only", force_initial=True)
        self.store.set_writer_mode("shadow_validated")
        self.store.set_writer_mode("cutover_ready")
        self.store.set_writer_mode("primary_writer")
        self.project = self.store.create_project({"idempotency_key": key("stream-project"), "name": "流式项目"})["project"]
        self.media = MediaService(self.store, media_root=self.root / "media", preview_root=self.root / "previews", staging_root=self.root / "staging", max_media_bytes=50 * 1024 * 1024)
        self.hub_token = "h" * 32
        app = create_app(store=self.store, media=self.media, api_token=self.hub_token, max_request_bytes=64 * 1024 * 1024)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        self.hub_port = self.site._server.sockets[0].getsockname()[1]

        self.gateway_store = GatewayStore(self.root / "gateway" / "gateway.sqlite3", data_dir=self.root / "gateway")
        message = self.gateway_store.store_message(
            message_id="fixture-media-stream",
            sender_id="fixture-owner",
            conversation_key="sha256:fixture",
            text="施工现场",
            media=[({"media_type": "image", "filename": "现场.jpg", "mime_type": "image/jpeg"}, jpeg_bytes())],
        )
        self.attachment_ref = message["attachments"][0]["attachment_ref"]
        self.gateway_token = "g" * 32
        service = type("GatewayService", (), {"store": self.gateway_store, "poller_state": "disabled"})()
        self.gateway = create_gateway_server("127.0.0.1", 0, service=service, loop=None, attachment_api_token=self.gateway_token)  # type: ignore[arg-type]
        self.gateway_thread = threading.Thread(target=self.gateway.serve_forever, daemon=True)
        self.gateway_thread.start()

    async def asyncTearDown(self) -> None:
        self.gateway.shutdown()
        self.gateway.server_close()
        self.gateway_thread.join(timeout=2)
        await self.runner.cleanup()
        self.temporary.cleanup()

    async def test_controller_streams_gateway_media_and_replays_without_second_fetch(self) -> None:
        router = ToolRouter(
            ledger_base_url=f"http://localhost:{self.hub_port}",
            ledger_token=self.hub_token,
            gateway_base_url=f"http://localhost:{self.gateway.server_port}",
            gateway_token=self.gateway_token,
            max_media_bytes=50 * 1024 * 1024,
        )
        router.begin_job("fixture-media-job", "fixture-media-message")
        arguments = {
            "idempotency_key": key("stream"),
            "attachment_ref": self.attachment_ref,
            "project_id": self.project["id"],
            "links": [],
        }
        first = await asyncio.to_thread(router.call, "renovation_media_ingest", arguments)
        asset = first["result"]["media"]
        self.assertEqual(asset["processing_status"], "ready")
        second = await asyncio.to_thread(router.call, "renovation_media_ingest", arguments)
        self.assertTrue(second["result"]["idempotent_replay"])
        self.assertEqual(second["result"]["media"]["id"], asset["id"])
        async with aiohttp.ClientSession() as session:
            async with session.get(f"http://127.0.0.1:{self.hub_port}{asset['content_url']}", headers={"Range": "bytes=0-9"}) as response:
                self.assertEqual(response.status, 206)
                self.assertEqual(len(await response.read()), 10)


if __name__ == "__main__":
    unittest.main()
