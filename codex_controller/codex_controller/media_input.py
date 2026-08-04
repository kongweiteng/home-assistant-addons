"""Private attachment materialization for official app-server localImage inputs."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Callable


IMAGE_SUFFIXES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
JOB_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,160}$")


class MediaInputError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class TurnMediaManager:
    """Materialize image previews under a private root until a Turn completes."""

    def __init__(
        self,
        root: str | Path,
        preview_attachment: Callable[[str], tuple[dict[str, Any], bytes]],
    ) -> None:
        requested_root = Path(root)
        if requested_root.exists() and requested_root.is_symlink():
            raise RuntimeError("Turn 媒体目录不能是符号链接")
        self.root = requested_root.resolve()
        self.preview_attachment = preview_attachment
        self._job_paths: dict[str, list[Path]] = {}
        self._turn_paths: dict[str, list[Path]] = {}
        self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(self.root, 0o700)
        self.cleanup_orphans()

    def prepare(self, job_id: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
        if not JOB_ID_RE.fullmatch(job_id):
            raise MediaInputError("media_job_invalid", "媒体作业标识无效")
        attachments = payload.get("attachments")
        if not isinstance(attachments, list):
            raise MediaInputError("media_input_invalid", "附件元数据无效")
        text = self._text_with_attachment_context(str(payload.get("text") or ""), attachments)
        input_items: list[dict[str, Any]] = [{"type": "text", "text": text}]
        image_attachments = [item for item in attachments if item.get("media_type") == "image"]
        if not image_attachments:
            return input_items

        job_dir = self.root / job_id
        if job_dir.is_symlink():
            raise MediaInputError("media_path_invalid", "Turn 媒体路径不能是符号链接")
        if job_dir.exists():
            self._remove_tree(job_dir)
        job_dir.mkdir(mode=0o700)
        paths: list[Path] = []
        try:
            for index, attachment in enumerate(image_attachments):
                reference = str(attachment.get("attachment_ref") or "")
                try:
                    metadata, content = self.preview_attachment(reference)
                except Exception as exc:
                    code = getattr(exc, "code", "attachment_preview_unavailable")
                    raise MediaInputError(str(code), "微信图片预览不可用") from exc
                self._verify_preview(attachment, metadata, content)
                suffix = IMAGE_SUFFIXES.get(str(metadata.get("mime_type") or ""))
                if suffix is None:
                    continue
                target = job_dir / f"{index:02d}-{hashlib.sha256(content).hexdigest()[:16]}{suffix}"
                self._atomic_write(target, content)
                paths.append(target)
                input_items.append({"type": "localImage", "path": str(target), "detail": "auto"})
        except Exception:
            self._remove_tree(job_dir)
            raise
        if paths:
            self._job_paths[job_id] = paths
        else:
            self._remove_tree(job_dir)
        return input_items

    def bind_turn(self, job_id: str, turn_id: str) -> None:
        paths = self._job_paths.pop(job_id, [])
        if paths:
            self._turn_paths[turn_id] = paths

    def cleanup_job(self, job_id: str) -> None:
        paths = self._job_paths.pop(job_id, [])
        self._cleanup_paths(paths)

    def cleanup_turn(self, turn_id: str) -> None:
        paths = self._turn_paths.pop(turn_id, [])
        self._cleanup_paths(paths)

    def cleanup_orphans(self) -> None:
        for child in list(self.root.iterdir()):
            if child.is_symlink() or child.is_file():
                child.unlink()
            elif child.is_dir():
                self._remove_tree(child)

    @staticmethod
    def _text_with_attachment_context(text: str, attachments: list[dict[str, Any]]) -> str:
        if not attachments:
            return text
        lines = [text, "", "当前微信消息包含以下受控附件引用："]
        for attachment in attachments:
            lines.append(
                "- attachment_ref={reference}; media_type={media_type}; size_bytes={size}; sha256={digest}".format(
                    reference=attachment.get("attachment_ref", ""),
                    media_type=attachment.get("media_type", ""),
                    size=attachment.get("size_bytes", ""),
                    digest=attachment.get("sha256", ""),
                )
            )
        lines.append("图片已通过受控 localImage 输入提供；需要归档时仅把对应 attachment_ref 交给已配置工具。")
        return "\n".join(lines)

    @staticmethod
    def _verify_preview(attachment: dict[str, Any], metadata: dict[str, Any], content: bytes) -> None:
        digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
        if (
            not content
            or metadata.get("size_bytes") != len(content)
            or attachment.get("size_bytes") != len(content)
            or metadata.get("sha256") != digest
            or attachment.get("sha256") != digest
            or metadata.get("mime_type") not in IMAGE_SUFFIXES
        ):
            raise MediaInputError("attachment_preview_invalid", "微信图片预览摘要、大小或类型不一致")

    @staticmethod
    def _atomic_write(target: Path, content: bytes) -> None:
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".image.", delete=False) as stream:
                temporary_path = Path(stream.name)
                os.chmod(stream.fileno(), 0o600)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, target)
            os.chmod(target, 0o600)
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()

    def _cleanup_paths(self, paths: list[Path]) -> None:
        parents: set[Path] = set()
        for path in paths:
            if path.parent.parent != self.root:
                continue
            parents.add(path.parent)
            if path.is_file() or path.is_symlink():
                path.unlink()
        for parent in parents:
            if parent.is_dir():
                try:
                    parent.rmdir()
                except OSError:
                    self._remove_tree(parent)

    def _remove_tree(self, directory: Path) -> None:
        if directory.parent != self.root or directory == self.root:
            raise RuntimeError("拒绝清理 Turn 媒体根目录之外的路径")
        for child in sorted(directory.rglob("*"), key=lambda path: len(path.parts), reverse=True):
            if child.is_symlink() or child.is_file():
                child.unlink()
            elif child.is_dir():
                child.rmdir()
        directory.rmdir()
