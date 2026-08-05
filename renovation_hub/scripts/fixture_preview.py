#!/usr/bin/env python3
"""Run Renovation Hub with synthetic, disposable preview data only."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import subprocess
import tempfile

from aiohttp import web

from renovation_hub.hub import RenovationHubStore
from renovation_hub.media import MediaService
from renovation_hub.web import create_app


def key(label: str) -> str:
    return f"fixture-preview-{label}-" + "0" * 24


def ingest(
    media: MediaService,
    content: bytes,
    *,
    filename: str,
    mime_type: str,
    label: str,
    project_id: str,
    stage_id: str,
    area_id: str,
    event_id: str,
) -> None:
    digest = hashlib.sha256(content).hexdigest()
    prepared = media.prepare_upload(
        idempotency_key=key(f"media-{label}"),
        source_ref_hash=hashlib.sha256(f"source-{label}".encode()).hexdigest(),
        original_filename=filename,
        mime_type=mime_type,
        expected_bytes=len(content),
    )
    Path(prepared["path"]).write_bytes(content)
    media.finalize_upload(
        prepared,
        received_bytes=len(content),
        sha256=digest,
        expected_sha256=digest,
        metadata={
            "idempotency_key": key(f"media-{label}"),
            "source_ref_hash": hashlib.sha256(f"source-{label}".encode()).hexdigest(),
            "project_id": project_id,
            "source": "fixture",
            "captured_at": "2026-05-13T10:30:00+08:00",
            "links": [
                {"target_type": "stage", "target_id": stage_id},
                {"target_type": "area", "target_id": area_id},
                {"target_type": "event", "target_id": event_id},
            ],
        },
        actor_hash="sha256:fixture-preview",
    )


def build_fixture(root: Path, addon_root: Path) -> tuple[RenovationHubStore, MediaService]:
    store = RenovationHubStore(root / "data" / "hub.sqlite3", data_dir=root / "data", share_dir=root / "share")
    store.set_writer_mode("read_only", force_initial=True)
    store.set_writer_mode("shadow_validated")
    store.set_writer_mode("cutover_ready")
    store.set_writer_mode("primary_writer")
    project = store.create_project(
        {
            "idempotency_key": key("project"),
            "name": "绿洲花园 8-2-501",
            "budget_cents": 26_000_000,
        }
    )["project"]
    stage_specs = [
        ("设计阶段", "completed", "2026-03-20", "2026-04-11", "#5f8f55"),
        ("拆除阶段", "completed", "2026-04-12", "2026-04-20", "#5f8f55"),
        ("水电施工", "active", "2026-05-05", "2026-05-25", "#5f8f55"),
        ("泥木施工", "planned", "2026-05-26", "2026-06-20", "#b37b45"),
        ("油漆施工", "planned", "2026-06-21", "2026-07-15", "#b57462"),
        ("安装收尾", "planned", "2026-07-16", "2026-08-10", "#6e7d96"),
        ("竣工验收", "planned", "2026-08-11", "2026-08-20", "#82579a"),
    ]
    stages = []
    for position, (name, status, start, end, color) in enumerate(stage_specs, 1):
        stages.append(
            store.create_stage(
                {
                    "idempotency_key": key(f"stage-{position}"),
                    "project_id": project["id"],
                    "name": name,
                    "position": position,
                    "status": status,
                    "color": color,
                    "planned_start": start,
                    "planned_end": end,
                    "actual_start": start if status != "planned" else None,
                    "actual_end": end if status == "completed" else None,
                }
            )["stage"]
        )
    area_names = ["厨房", "客厅", "主卧", "卫生间", "过道", "材料堆放区"]
    areas = [
        store.create_area(
            {
                "idempotency_key": key(f"area-{index}"),
                "project_id": project["id"],
                "name": name,
                "position": index,
            }
        )["area"]
        for index, name in enumerate(area_names, 1)
    ]
    event_specs = [
        ("电路验收", "全屋强弱电布线完成，进行回路测试，符合规范要求。", "inspection", "2026-05-13T10:35:00+08:00", 0),
        ("水路打压测试", "打压 0.8MPa，稳压 30 分钟无掉压，测试合格。", "inspection", "2026-05-12T16:20:00+08:00", 3),
        ("给排水布管", "厨房、卫生间给排水管布设完成。", "progress", "2026-05-10T09:15:00+08:00", 4),
        ("强电布线", "强电箱位置确认，网线与电视线铺设完成。", "progress", "2026-05-08T14:40:00+08:00", 1),
        ("材料进场复核", "电线、水管和辅材批次已核对。", "note", "2026-05-06T11:10:00+08:00", 5),
        ("水电定位确认", "插座、开关与灯位完成现场复核。", "decision", "2026-05-05T09:30:00+08:00", 2),
    ]
    events = []
    for index, (title, description, event_type, occurred_at, area_index) in enumerate(event_specs):
        events.append(
            store.create_event(
                {
                    "idempotency_key": key(f"event-{index}"),
                    "project_id": project["id"],
                    "stage_id": stages[2]["id"],
                    "area_id": areas[area_index]["id"],
                    "title": title,
                    "description": description,
                    "event_type": event_type,
                    "occurred_at": occurred_at,
                }
            )["event"]
        )
    v2_payment = store.add_payment(
        {
            "idempotency_key": key("payment-v2-door"),
            "ledger_format_version": 2,
            "amount_cents": 1_280_000,
            "occurred_on": "2026-05-05",
            "merchant": "TATA 木门",
            "note": "购买客厅和卧室木门、门套及五金安装服务",
            "grouped_tags": {"主题": ["门窗"], "专业": ["木作"], "性质": ["设备"]},
            "project_id": project["id"],
            "stage_id": stages[2]["id"],
            "area_id": areas[1]["id"],
        }
    )["transaction"]
    store.add_payment(
        {
            "idempotency_key": key("payment-v2-deposit"),
            "ledger_format_version": 2,
            "amount_cents": 500_000,
            "occurred_on": "2026-05-06",
            "merchant": "水电班组",
            "note": "水电施工进场订金",
            "is_deposit": True,
            "grouped_tags": {"专业": ["水电"], "性质": ["人工"]},
            "project_id": project["id"],
            "stage_id": stages[2]["id"],
            "area_id": areas[0]["id"],
        }
    )
    store.add_refund(
        {
            "idempotency_key": key("refund-v2-door"),
            "original_payment_id": v2_payment["id"],
            "amount_cents": 80_000,
            "occurred_on": "2026-05-07",
            "note": "木门五金差价退回",
            "project_id": project["id"],
            "stage_id": stages[2]["id"],
            "area_id": areas[1]["id"],
        }
    )
    payments = [
        ("设计费用", 1_200_000, "设计工作室", "全屋平面方案与施工图设计费", ["设计"], stages[0], areas[1]),
        ("拆除工程", 850_000, "施工队", "厨房、卫生间及过道拆除人工费", ["人工"], stages[1], areas[4]),
        ("其他费用", 355_000, "材料市场", "水电辅材和现场保护材料", ["辅材"], stages[2], areas[5]),
    ]
    for index, (category, amount, merchant, note, tags, stage, area) in enumerate(payments):
        store.add_payment(
            {
                "idempotency_key": key(f"payment-v1-{index}"),
                "amount_cents": amount,
                "occurred_on": f"2026-05-{index + 8:02d}",
                "main_category": category,
                "merchant": merchant,
                "note": note,
                "tags": tags,
                "project_id": project["id"],
                "stage_id": stage["id"],
                "area_id": area["id"],
            }
        )
    media = MediaService(store, media_root=root / "media", preview_root=root / "previews", staging_root=root / "staging", max_media_bytes=100 * 1024 * 1024)
    asset_dir = addon_root / "renovation_hub" / "web" / "assets"
    image_files = [
        "kitchen-rough-in.png",
        "living-room-plaster.png",
        "primary-bedroom-protection.png",
        "bathroom-waterproofing.png",
        "hallway-conduits.png",
        "materials-staging.png",
    ]
    for index, filename in enumerate(image_files):
        ingest(
            media,
            (asset_dir / filename).read_bytes(),
            filename=filename,
            mime_type="image/png",
            label=f"image-{index}",
            project_id=project["id"],
            stage_id=stages[2]["id"],
            area_id=areas[index]["id"],
            event_id=events[index]["id"],
        )
    video_path = root / "site-walkthrough.mp4"
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-loop", "1", "-i", str(asset_dir / image_files[1]),
            "-t", "2", "-vf", "scale=960:-2", "-c:v", "mpeg4", "-y", str(video_path),
        ],
        check=True,
        timeout=60,
    )
    ingest(
        media,
        video_path.read_bytes(),
        filename="客厅巡检.mp4",
        mime_type="video/mp4",
        label="video",
        project_id=project["id"],
        stage_id=stages[2]["id"],
        area_id=areas[1]["id"],
        event_id=events[0]["id"],
    )
    return store, media


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8101)
    parser.add_argument("--static-dir", type=Path, required=True)
    arguments = parser.parse_args()
    addon_root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="renovation-hub-preview-") as temporary:
        store, media = build_fixture(Path(temporary), addon_root)
        app = create_app(
            store=store,
            media=media,
            api_token="fixture-preview-token-0000000000000000",
            max_request_bytes=128 * 1024 * 1024,
            static_dir=arguments.static_dir,
        )
        web.run_app(app, host="127.0.0.1", port=arguments.port, access_log=None)


if __name__ == "__main__":
    main()
