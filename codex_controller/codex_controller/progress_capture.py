"""Deterministic orchestration for multi-message renovation progress capture."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from typing import Any


SHA256_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
SESSION_ID_RE = re.compile(r"^PCS-[A-Z2-7]{26}$")
ATTACHMENT_REF_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
CAPTION_RE = re.compile(r"^第\s*(\d+)\s*(?:张|个|段)?\s*[：:]\s*(.+)$", re.S)
AMBIGUOUS_CODES = {
    "capture_project_ambiguous": "我已保留本次图片/视频，但存在多个装修项目。请回复具体项目名称，例如“瞰湖湾装修”。",
    "capture_stage_ambiguous": "我已保留本次图片/视频，但阶段不够明确。请回复具体施工阶段名称。",
    "capture_area_ambiguous": "我已保留本次图片/视频，但区域不够明确。请回复具体空间名称。",
}


@dataclass(frozen=True)
class CaptureOutcome:
    text: str
    suppress_reply: bool = False
    item_type: str = "progressCapture"


def _idempotency(*parts: str) -> str:
    material = "\n".join(parts)
    return "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest()


class ProgressCaptureCoordinator:
    MAX_REGISTER_BATCH = 16

    def __init__(self, router: Any) -> None:
        self.router = router

    @staticmethod
    def recognizes(payload: dict[str, Any]) -> bool:
        context = payload.get("progress_capture_context")
        return isinstance(context, dict) and context.get("version") == 1 and bool(context.get("action"))

    def handle(self, payload: dict[str, Any]) -> CaptureOutcome:
        context = self._context(payload.get("progress_capture_context"))
        action = context["action"]
        if action == "confirmation_required":
            return CaptureOutcome(
                "看起来这可能是装修进度。需要我进入连续归档模式吗？回复“开始记录”即可；确认前普通图片不会写入装修档案。",
                item_type="progressCaptureConfirmation",
            )
        if action == "confirmation_pending_silent":
            return CaptureOutcome("等待用户确认是否开始装修进度归档。", suppress_reply=True)
        if action == "paused_media_ignored":
            return CaptureOutcome(
                "当前装修进度记录已暂停，这条媒体没有归档。回复“继续记录”后再发送即可。",
                item_type="progressCapturePaused",
            )
        if action in {"no_active_session", "nothing_to_cancel"}:
            return CaptureOutcome("当前没有进行中的装修进度记录。说“开始记录装修进度”即可开始。")
        if action in {"paused", "cancelled"}:
            try:
                status = self._action({"action": "status", "session_id": context["session_id"]})
            except Exception as exc:
                if str(getattr(exc, "code", "")) != "capture_session_not_found":
                    raise
                if action == "paused":
                    return CaptureOutcome(
                        f"已暂停本次装修进度记录；Gateway 当前暂存 {context['received_count']} 项，"
                        "尚未建立 Hub 草稿。回复“继续记录”并补充项目名称即可恢复。"
                    )
                return CaptureOutcome(
                    "已取消本次采集；尚未写入 Hub 的暂存媒体不会归档，并会按附件保留策略自动过期。"
                )
            target_action = "pause" if action == "paused" else "cancel"
            result = self._action(
                {
                    "action": target_action,
                    "session_id": context["session_id"],
                    "idempotency_key": self._key(payload, target_action),
                }
            )
            count = result.get("reconciliation", status.get("reconciliation", {})).get("stored", 0)
            if action == "paused":
                return CaptureOutcome(
                    f"已暂停本次装修进度记录；当前已归档 {count} 项。回复“继续记录”即可恢复。"
                )
            return CaptureOutcome(f"已取消继续采集；此前已经安全存入 Hub 的 {count} 项原件会保留在已取消草稿中。")

        try:
            session = self._ensure_session(payload, context)
        except Exception as exc:  # Safe deterministic error mapping; private details stay upstream.
            code = str(getattr(exc, "code", ""))
            if code in AMBIGUOUS_CODES:
                return CaptureOutcome(AMBIGUOUS_CODES[code], item_type="progressCaptureDisambiguation")
            raise

        if action == "resumed":
            self._action(
                {
                    "action": "resume",
                    "session_id": context["session_id"],
                    "idempotency_key": self._key(payload, "resume"),
                }
            )
        note_text = str(context.get("note_text") or "").strip()
        if note_text and action in {"note", "started", "media_received", "resumed"}:
            self._save_note(payload, context, note_text)

        failures = self._sync_attachments(payload, context, session)
        if action == "finalize_requested":
            return self._finalize(payload, context, failures)
        if action == "status":
            status = self._action({"action": "status", "session_id": context["session_id"]})
            counts = status.get("reconciliation", {})
            return CaptureOutcome(self._status_text(status, counts, failures))

        status = self._action({"action": "status", "session_id": context["session_id"]})
        counts = status.get("reconciliation", {})
        if failures:
            return CaptureOutcome(
                f"本次会话已收到 {context['received_count']} 项，已归档 {counts.get('stored', 0)} 项；"
                f"另有 {len(failures)} 项暂未完成，我已保留引用，完成前不会伪报成功。",
                item_type="progressCaptureRetryRequired",
            )
        if action == "started":
            stored = counts.get("stored", 0)
            suffix = f"，并已保存 {stored} 项" if stored else ""
            return CaptureOutcome(
                f"已开始记录“{status.get('session', {}).get('title', '装修进度')}”{suffix}。"
                "你可以继续发送图片、视频和说明；完成时说“记录完成”，也可说“暂停记录”或“记录状态”。"
            )
        stored = int(counts.get("stored", 0))
        suppress = action == "media_received" and not note_text and stored not in {1} and stored % 5 != 0
        return CaptureOutcome(
            f"本次装修进度已收到 {context['received_count']} 项，Hub 已安全归档 {stored} 项。",
            suppress_reply=suppress,
        )

    def _ensure_session(self, payload: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        start_text: list[str] = []
        for value in (
            str(context.get("intent_text") or "").strip(),
            str(context.get("note_text") or "").strip(),
            str(payload.get("text") or "").strip(),
        ):
            if value and value not in start_text:
                start_text.append(value)
        result = self._action(
            {
                "action": "start",
                "session_id": context["session_id"],
                "scope_hash": context["scope_hash"],
                "source_message_hash": context["source_message_hash"],
                "text": "\n".join(start_text)[:1000],
                "occurred_at": payload.get("received_at"),
                "idempotency_key": _idempotency("progress-capture-start", context["session_id"]),
            }
        )
        return result.get("session", result)

    def _sync_attachments(
        self,
        payload: dict[str, Any],
        context: dict[str, Any],
        session: dict[str, Any],
    ) -> list[str]:
        attachments = context["source_attachments"]
        if not attachments:
            return []
        registered: dict[str, dict[str, Any]] = {}
        for offset in range(0, len(attachments), self.MAX_REGISTER_BATCH):
            batch = attachments[offset : offset + self.MAX_REGISTER_BATCH]
            result = self._action(
                {
                    "action": "register_items",
                    "session_id": context["session_id"],
                    "items": [
                        {key: value for key, value in item.items() if key != "attachment_ref"}
                        for item in batch
                    ],
                    "idempotency_key": _idempotency(
                        "progress-capture-register",
                        context["session_id"],
                        ",".join(str(item["source_ref_hash"]) for item in batch),
                    ),
                }
            )
            for item in result.get("items", []):
                if isinstance(item, dict) and isinstance(item.get("source_ref_hash"), str):
                    registered[item["source_ref_hash"]] = item

        failures: list[str] = []
        for attachment in attachments:
            item = registered.get(attachment["source_ref_hash"])
            if item is None:
                failures.append(f"第{attachment['position']}项登记失败")
                continue
            if item.get("state") == "failed" and item.get("media_id"):
                retry = self._action(
                    {
                        "action": "retry_failed",
                        "session_id": context["session_id"],
                        "idempotency_key": _idempotency(
                            "progress-capture-reprocess",
                            context["session_id"],
                            str(item["id"]),
                        ),
                    }
                )
                retried = next(
                    (
                        value
                        for value in retry.get("items", [])
                        if isinstance(value, dict) and value.get("id") == item.get("id")
                    ),
                    None,
                )
                if isinstance(retried, dict) and retried.get("state") == "stored":
                    item = retried
            already_stored = item.get("state") == "stored"
            try:
                result = self.router.progress_capture_media(
                    attachment["attachment_ref"],
                    {
                        "session_id": context["session_id"],
                        "item_id": item["id"],
                        "project_id": session["project_id"],
                        "event_id": session["event_id"],
                        "source_ref_hash": attachment["source_ref_hash"],
                        "captured_at": payload.get("received_at"),
                        "idempotency_key": _idempotency(
                            "progress-capture-media",
                            context["session_id"],
                            str(item["id"]),
                        ),
                    },
                )
                consumption = (
                    result.get("result", {}).get("attachment_consumption")
                    if isinstance(result, dict) and isinstance(result.get("result"), dict)
                    else None
                )
                if consumption == "pending":
                    failures.append(f"第{attachment['position']}项附件确认待重试")
            except Exception as exc:
                code = str(getattr(exc, "code", "capture_media_failed"))[:80]
                failures.append(f"第{attachment['position']}项 {code}")
                if already_stored:
                    continue
                try:
                    self._action(
                        {
                            "action": "mark_failed",
                            "session_id": context["session_id"],
                            "item_id": item["id"],
                            "error_code": code,
                        }
                    )
                except Exception:
                    pass
        return failures

    def _save_note(self, payload: dict[str, Any], context: dict[str, Any], text: str) -> None:
        target_position = None
        match = CAPTION_RE.match(text)
        if match:
            target_position = int(match.group(1))
        self._action(
            {
                "action": "note",
                "session_id": context["session_id"],
                "text": text,
                "target_position": target_position,
                "source_message_hash": context["source_message_hash"],
                "idempotency_key": self._key(payload, "note"),
            }
        )

    def _finalize(
        self,
        payload: dict[str, Any],
        context: dict[str, Any],
        failures: list[str],
    ) -> CaptureOutcome:
        try:
            self._action(
                {
                    "action": "retry_failed",
                    "session_id": context["session_id"],
                    "idempotency_key": self._key(payload, "retry-failed"),
                }
            )
            result = self._action(
                {
                    "action": "finalize",
                    "session_id": context["session_id"],
                    "expected_received_count": context["received_count"],
                    "idempotency_key": self._key(payload, "finalize"),
                }
            )
        except Exception as exc:
            if str(getattr(exc, "code", "")) == "capture_reconciliation_pending":
                status = self._action({"action": "status", "session_id": context["session_id"]})
                counts = status.get("reconciliation", {})
                return CaptureOutcome(
                    f"暂未结束：Gateway 收到 {context['received_count']} 项，Hub 已存 {counts.get('stored', 0)} 项，"
                    f"事件已关联 {counts.get('linked', 0)} 项。未完成项会保留并重试；原件缺失时我才会请你重发。",
                    item_type="progressCaptureReconciliationPending",
                )
            raise
        counts = result["reconciliation"]
        if not (
            counts["received"] == counts["registered"] == counts["stored"] == counts["linked"]
            and counts["failed"] == 0
            and counts["pending"] == 0
            and not failures
        ):
            return CaptureOutcome(
                "暂未结束：最终数量没有完全一致，已保留会话等待重试。",
                item_type="progressCaptureReconciliationPending",
            )
        self.router.progress_capture_ack(
            {
                "session_id": context["session_id"],
                "received_count": counts["received"],
                "stored_count": counts["stored"],
                "linked_count": counts["linked"],
            }
        )
        title = result.get("session", {}).get("title", "装修进度")
        return CaptureOutcome(
            f"“{title}”记录完成：共 {counts['stored']} 项图片/视频已全部保存，并与最终进度事件完成精确关联。",
            item_type="progressCaptureCompleted",
        )

    @staticmethod
    def _status_text(result: dict[str, Any], counts: dict[str, Any], failures: list[str]) -> str:
        session = result.get("session", {})
        suffix = f"；另有 {len(failures)} 项本轮重试失败" if failures else ""
        state = {
            "active": "记录中",
            "paused": "已暂停",
            "finalizing": "正在核对",
            "completed": "已完成",
            "cancelled": "已取消",
        }.get(str(session.get("state") or ""), "未知")
        return (
            f"当前记录“{session.get('title', '装修进度')}”状态为“{state}”："
            f"收到 {counts.get('received', 0)}，已存 {counts.get('stored', 0)}，"
            f"已关联 {counts.get('linked', 0)}，失败 {counts.get('failed', 0)}{suffix}。"
        )

    def _action(self, payload: dict[str, Any]) -> dict[str, Any]:
        result = self.router.progress_capture_action(payload)
        if isinstance(result, dict) and result.get("version") == 1 and isinstance(result.get("result"), dict):
            return result["result"]
        if not isinstance(result, dict):
            raise RuntimeError("progress capture upstream response invalid")
        return result

    @staticmethod
    def _key(payload: dict[str, Any], action: str) -> str:
        return _idempotency(
            "progress-capture-action",
            action,
            str(payload.get("message_id") or ""),
        )

    @staticmethod
    def _context(value: Any) -> dict[str, Any]:
        return validate_progress_capture_context(value)


def validate_progress_capture_context(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("version") != 1:
        raise ValueError("progress_capture_context invalid")
    required = {
        "session_id",
        "scope_hash",
        "state",
        "action",
        "received_count",
        "source_message_hash",
        "source_attachments",
    }
    if not required.issubset(value):
        raise ValueError("progress_capture_context fields missing")
    if not isinstance(value["session_id"], str) or not SESSION_ID_RE.fullmatch(value["session_id"]):
        raise ValueError("progress capture session id invalid")
    for field in ("scope_hash", "source_message_hash"):
        if not isinstance(value[field], str) or not SHA256_RE.fullmatch(value[field]):
            raise ValueError(f"progress capture {field} invalid")
    if value["state"] not in {
        "confirmation_required",
        "active",
        "paused",
        "finalizing",
        "completed",
        "cancelled",
        "expired",
    }:
        raise ValueError("progress capture state invalid")
    if value["action"] not in {
        "confirmation_required",
        "confirmation_pending_silent",
        "started",
        "media_received",
        "note",
        "paused",
        "resumed",
        "paused_media_ignored",
        "status",
        "finalize_requested",
        "cancelled",
        "no_active_session",
        "nothing_to_cancel",
    }:
        raise ValueError("progress capture action invalid")
    for field, maximum in (("intent_text", 1000), ("note_text", 2000)):
        text = value.get(field, "")
        if not isinstance(text, str) or len(text) > maximum:
            raise ValueError(f"progress capture {field} invalid")
    count = value["received_count"]
    attachments = value["source_attachments"]
    if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= 256:
        raise ValueError("progress capture count invalid")
    if not isinstance(attachments, list) or len(attachments) > 256:
        raise ValueError("progress capture attachments invalid")
    normalized: list[dict[str, Any]] = []
    seen_references: set[str] = set()
    seen_positions: set[int] = set()
    for item in attachments:
        if not isinstance(item, dict):
            raise ValueError("progress capture attachment invalid")
        reference = item.get("attachment_ref")
        if (
            not isinstance(reference, str)
            or not ATTACHMENT_REF_RE.fullmatch(reference)
            or reference in seen_references
        ):
            raise ValueError("progress capture attachment ref invalid")
        seen_references.add(reference)
        for field in ("source_message_hash", "source_ref_hash", "sha256"):
            if not isinstance(item.get(field), str) or not SHA256_RE.fullmatch(item[field]):
                raise ValueError("progress capture attachment digest invalid")
        position = item.get("position")
        size = item.get("size_bytes")
        if (
            isinstance(position, bool)
            or not isinstance(position, int)
            or not 1 <= position <= count
            or position in seen_positions
        ):
            raise ValueError("progress capture attachment position invalid")
        seen_positions.add(position)
        if isinstance(size, bool) or not isinstance(size, int) or size < 1:
            raise ValueError("progress capture attachment size invalid")
        if item.get("media_type") not in {"image", "video"}:
            raise ValueError("progress capture attachment type invalid")
        display_name = item.get("display_name")
        if not isinstance(display_name, str) or not 1 <= len(display_name) <= 255:
            raise ValueError("progress capture attachment name invalid")
        normalized.append(dict(item))
    context = dict(value)
    context["source_attachments"] = sorted(normalized, key=lambda item: item["position"])
    return context
