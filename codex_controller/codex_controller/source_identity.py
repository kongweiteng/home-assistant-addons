"""Deterministic identity for the Python source loaded by the Controller runtime."""

from __future__ import annotations

from functools import lru_cache
import hashlib
from pathlib import Path
from typing import Any


SOURCE_IDENTITY_SCHEMA_VERSION = 1


def calculate_source_identity(package_root: Path) -> dict[str, Any]:
    """Hash every Python source file using an unambiguous path/length framing."""

    root = package_root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Controller source identity root must be a directory")
    source_files = sorted(
        path for path in root.rglob("*.py") if path.is_file() and not path.is_symlink()
    )
    if not source_files:
        raise ValueError("Controller source identity has no Python files")

    digest = hashlib.sha256()
    for path in source_files:
        relative = path.relative_to(root).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return {
        "schema_version": SOURCE_IDENTITY_SCHEMA_VERSION,
        "algorithm": "sha256",
        "digest": digest.hexdigest(),
        "file_count": len(source_files),
    }


@lru_cache(maxsize=1)
def runtime_source_identity() -> dict[str, Any]:
    """Return the immutable source identity for this Controller process."""

    return calculate_source_identity(Path(__file__).resolve().parent)


__all__ = ["calculate_source_identity", "runtime_source_identity"]
