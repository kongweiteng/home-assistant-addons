from __future__ import annotations

import hashlib
from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import aiohttp
from aiohttp import web
from PIL import Image

from renovation_hub.hub import RenovationHubStore
from renovation_hub.media import MediaService
from renovation_hub.web import _make_app_key, create_app


def jpeg_bytes() -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (480, 320), "#a8784e").save(buffer, "JPEG", quality=88)
    return buffer.getvalue()


class RenovationAiohttpCompatibilityTests(unittest.TestCase):
    def test_app_key_falls_back_for_bookworm_aiohttp(self) -> None:
        with mock.patch.object(web, "AppKey", None):
            self.assertEqual(_make_app_key("store", object), "store")


class RenovationPageApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.store = RenovationHubStore(root / "data" / "hub.sqlite3", data_dir=root / "data", share_dir=root / "share")
        self.store.set_writer_mode("read_only", force_initial=True)
        self.store.set_writer_mode("shadow_validated")
        self.store.set_writer_mode("cutover_ready")
        self.store.set_writer_mode("primary_writer")
        self.project = self.store.create_project(
            {
                "idempotency_key": "fixture-page-project-000000000000",
                "name": "页面项目",
                "budget_cents": 5_000_000,
            }
        )["project"]
        self.stage = self.store.create_stage(
            {
                "idempotency_key": "fixture-page-stage-0000000000000",
                "project_id": self.project["id"],
                "name": "水电施工",
                "status": "active",
            }
        )["stage"]
        self.area = self.store.create_area(
            {
                "idempotency_key": "fixture-page-area-00000000000000",
                "project_id": self.project["id"],
                "name": "厨房",
            }
        )["area"]
        self.media = MediaService(
            self.store,
            media_root=root / "media",
            preview_root=root / "preview",
            staging_root=root / "staging",
            max_media_bytes=20 * 1024 * 1024,
        )
        app = create_app(
            store=self.store,
            media=self.media,
            api_token="t" * 32,
            cutover_token="c" * 32,
            max_request_bytes=32 * 1024 * 1024,
        )
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        self.port = self.site._server.sockets[0].getsockname()[1]
        self.session = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
        async with self.session.get(f"http://127.0.0.1:{self.port}/api/v1/session") as response:
            self.assertEqual(response.status, 200)
            self.csrf = (await response.json())["result"]["csrf_token"]

    async def asyncTearDown(self) -> None:
        await self.session.close()
        await self.runner.cleanup()
        self.temporary.cleanup()

    def headers(self, key: str | None = None) -> dict[str, str]:
        result = {"X-CSRF-Token": self.csrf}
        if key:
            result["Idempotency-Key"] = key
        return result

    async def test_page_writes_require_csrf_and_preserve_project_context_versions(self) -> None:
        payload = {
            "amount_cents": 125_000,
            "occurred_on": "2026-08-03",
            "main_category": "水电工程",
            "merchant": "示例商家",
            "project_id": self.project["id"],
            "stage_id": self.stage["id"],
            "area_id": self.area["id"],
            "tags": ["材料"],
        }
        async with self.session.post(f"http://127.0.0.1:{self.port}/api/v1/ledger/transactions", json=payload, headers={"Idempotency-Key": "page-payment-no-csrf-000000"}) as response:
            self.assertEqual(response.status, 403)
        async with self.session.post(f"http://127.0.0.1:{self.port}/api/v1/ledger/transactions", json=payload, headers=self.headers("page-payment-with-csrf-000000")) as response:
            self.assertEqual(response.status, 201)
            transaction = (await response.json())["result"]["transaction"]
        self.assertEqual(transaction["context"]["project_id"], self.project["id"])
        self.assertEqual(transaction["version"], 1)
        correction = {"version": 1, "changes": {"amount_cents": 130_000}, "reason": "页面修正"}
        async with self.session.patch(f"http://127.0.0.1:{self.port}/api/v1/ledger/transactions/{transaction['id']}", json=correction, headers=self.headers("page-payment-correct-00000000")) as response:
            self.assertEqual(response.status, 200)
            corrected = (await response.json())["result"]["transaction"]
        self.assertEqual(corrected["version"], 2)
        async with self.session.patch(f"http://127.0.0.1:{self.port}/api/v1/ledger/transactions/{transaction['id']}", json=correction, headers=self.headers("page-payment-stale-000000000")) as response:
            self.assertEqual(response.status, 409)
            self.assertEqual((await response.json())["error"]["code"], "version_conflict")
        async with self.session.get(f"http://127.0.0.1:{self.port}/api/v1/ledger/transactions?project_id={self.project['id']}") as response:
            items = (await response.json())["result"]["items"]
        self.assertEqual([item["id"] for item in items], [transaction["id"]])

    async def test_browser_upload_is_streamed_completed_and_previewed(self) -> None:
        content = jpeg_bytes()
        digest = hashlib.sha256(content).hexdigest()
        create_payload = {
            "project_id": self.project["id"],
            "original_filename": "厨房现场.jpg",
            "mime_type": "image/jpeg",
            "size_bytes": len(content),
            "sha256": digest,
            "captured_at": "2026-08-03T10:00:00+08:00",
            "links": [
                {"target_type": "stage", "target_id": self.stage["id"]},
                {"target_type": "area", "target_id": self.area["id"]},
            ],
        }
        async with self.session.post(f"http://127.0.0.1:{self.port}/api/v1/uploads", json=create_payload, headers=self.headers("page-upload-image-000000000000")) as response:
            self.assertEqual(response.status, 201)
            upload = (await response.json())["result"]
        async with self.session.put(
            f"http://127.0.0.1:{self.port}{upload['content_url']}",
            data=content,
            headers={**self.headers(), "Content-Type": "image/jpeg"},
        ) as response:
            self.assertEqual(response.status, 200)
        async with self.session.post(f"http://127.0.0.1:{self.port}{upload['complete_url']}", headers=self.headers()) as response:
            self.assertEqual(response.status, 200)
            asset = (await response.json())["result"]["media"]
        self.assertEqual(asset["processing_status"], "ready")
        self.assertEqual(asset["links"][0]["target_id"], self.area["id"])
        async with self.session.get(f"http://127.0.0.1:{self.port}{asset['preview_url']}") as response:
            self.assertEqual(response.status, 200)
            self.assertGreater(len(await response.read()), 100)

    async def test_internal_auth_and_upload_mime_fail_closed_without_staging_file(self) -> None:
        async with self.session.get(f"http://127.0.0.1:{self.port}/internal/v1/status") as response:
            self.assertEqual(response.status, 401)
        async with self.session.get(
            f"http://127.0.0.1:{self.port}/internal/v1/status",
            headers={"Authorization": f"Bearer {'t' * 32}"},
        ) as response:
            self.assertEqual(response.status, 200)

        content = jpeg_bytes()
        payload = {
            "project_id": self.project["id"],
            "original_filename": "类型不匹配.jpg",
            "mime_type": "image/jpeg",
            "size_bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "links": [],
        }
        async with self.session.post(
            f"http://127.0.0.1:{self.port}/api/v1/uploads",
            json=payload,
            headers=self.headers("page-upload-mime-0000000000000"),
        ) as response:
            self.assertEqual(response.status, 201)
            upload = (await response.json())["result"]
        async with self.session.put(
            f"http://127.0.0.1:{self.port}{upload['content_url']}",
            data=content,
            headers={**self.headers(), "Content-Type": "image/png"},
        ) as response:
            self.assertEqual(response.status, 415)
            self.assertEqual((await response.json())["error"]["code"], "media_type_rejected")
        upload_state = self.media.browser_upload(upload["upload_id"])
        self.assertEqual(upload_state["state"], "created")
        self.assertFalse(Path(upload_state["path"]).exists())

    async def test_internal_chart_download_returns_authenticated_png_not_spa_html(self) -> None:
        reference = "summary-" + "a" * 32 + ".png"
        content = b"\x89PNG\r\n\x1a\nfixture-chart"
        chart_path = self.store.charts_dir / reference
        chart_path.write_bytes(content)
        bearer = {"Authorization": f"Bearer {'t' * 32}"}

        async with self.session.get(
            f"http://127.0.0.1:{self.port}/internal/v1/downloads/chart/{reference}"
        ) as response:
            self.assertEqual(response.status, 401)

        async with self.session.get(
            f"http://127.0.0.1:{self.port}/internal/v1/downloads/chart/{reference}",
            headers=bearer,
        ) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["Content-Type"], "image/png")
            self.assertEqual(int(response.headers["Content-Length"]), len(content))
            self.assertEqual(await response.read(), content)

        async with self.session.get(
            f"http://127.0.0.1:{self.port}/internal/v1/downloads/chart/not-a-chart.png",
            headers=bearer,
        ) as response:
            self.assertEqual(response.status, 400)
            self.assertEqual((await response.json())["error"]["code"], "invalid_reference")

        missing = "summary-" + "b" * 32 + ".png"
        async with self.session.get(
            f"http://127.0.0.1:{self.port}/internal/v1/downloads/chart/{missing}",
            headers=bearer,
        ) as response:
            self.assertEqual(response.status, 404)
            self.assertEqual((await response.json())["error"]["code"], "not_found")

    async def test_cutover_requires_independent_token_and_old_writer_endpoint_cannot_activate(self) -> None:
        bearer = {"Authorization": f"Bearer {'t' * 32}"}
        async with self.session.post(
            f"http://127.0.0.1:{self.port}/internal/v1/admin/writer-mode",
            json={"target": "primary_writer", "confirmation": "ACTIVATE_PRIMARY_WRITER"},
            headers=bearer,
        ) as response:
            self.assertEqual(response.status, 409)
            self.assertEqual((await response.json())["error"]["code"], "cutover_manifest_required")
        async with self.session.post(
            f"http://127.0.0.1:{self.port}/internal/v1/admin/cutover/seed",
            json={"manifest_id": "missing"},
            headers={**bearer, "X-Cutover-Token": "wrong"},
        ) as response:
            self.assertEqual(response.status, 403)
            self.assertEqual((await response.json())["error"]["code"], "cutover_not_authorized")
        async with self.session.post(
            f"http://127.0.0.1:{self.port}/internal/v1/admin/cutover/seed",
            json={"manifest_id": "missing"},
            headers={**bearer, "X-Cutover-Token": "c" * 32},
        ) as response:
            self.assertEqual(response.status, 404)
            self.assertEqual((await response.json())["error"]["code"], "manifest_not_found")


if __name__ == "__main__":
    unittest.main()
