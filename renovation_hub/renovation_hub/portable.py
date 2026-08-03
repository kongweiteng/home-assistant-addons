"""Strict reader for the framework-independent renovation ledger package."""

from __future__ import annotations

from collections import defaultdict
import csv
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import stat
import unicodedata
import zipfile
from typing import Any, Iterable


FORMAT_ID = "kanhuwan-renovation-ledger"
FORMAT_VERSION = 1
FORMAT_VERSION_V2 = 2
SUPPORTED_FORMAT_VERSIONS = {FORMAT_VERSION, FORMAT_VERSION_V2}
TAG_DIMENSIONS = (
    "主题",
    "空间",
    "专业",
    "性质",
    "渠道",
    "品牌",
    "生态",
    "阶段",
    "状态",
)
MAX_GROUPED_TAGS = 24
MAX_GROUPED_TAG_LENGTH = 40
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_EXPANDED_BYTES = 1024 * 1024 * 1024
MAX_MEMBER_COUNT = 10_000
MAX_JSON_BYTES = 128 * 1024 * 1024
REQUIRED_ROOT_FILES = {
    "manifest.json",
    "ledger.json",
    "transactions.csv",
    "transaction_tags.csv",
    "attachments.csv",
    "audit_log.jsonl",
    "schema.json",
    "FORMAT.md",
    "verify.py",
    "bookkeeping.sqlite3",
}


class PortableArchiveError(ValueError):
    """Raised when a portable package is unsafe or internally inconsistent."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def normalized_member_name(value: str, *, allow_directory: bool = False) -> str:
    if not value or "\\" in value or value.startswith("/"):
        raise PortableArchiveError("便携包包含不安全路径")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise PortableArchiveError("便携包包含不安全路径")
    if any(part.startswith("._") for part in path.parts):
        raise PortableArchiveError("便携包包含不允许的元数据文件")
    normalized = path.as_posix()
    if value.endswith("/"):
        if not allow_directory:
            raise PortableArchiveError("便携包出现意外目录条目")
        normalized += "/"
    return normalized


def _safe_zip_members(archive: zipfile.ZipFile) -> tuple[list[zipfile.ZipInfo], list[zipfile.ZipInfo]]:
    infos = archive.infolist()
    if len(infos) > MAX_MEMBER_COUNT:
        raise PortableArchiveError("便携包文件数量超过上限")
    files: list[zipfile.ZipInfo] = []
    directories: list[zipfile.ZipInfo] = []
    seen: set[str] = set()
    expanded = 0
    for info in infos:
        normalized = normalized_member_name(info.filename, allow_directory=info.is_dir())
        if normalized in seen:
            raise PortableArchiveError("便携包包含重复路径")
        seen.add(normalized)
        mode = (info.external_attr >> 16) & 0xFFFF
        if mode and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
            raise PortableArchiveError("便携包只允许普通文件和目录")
        if info.is_dir():
            directories.append(info)
            continue
        expanded += info.file_size
        if expanded > MAX_EXPANDED_BYTES:
            raise PortableArchiveError("便携包解压后大小超过上限")
        if info.file_size > 16 * 1024 * 1024 and info.compress_size > 0:
            if info.file_size / info.compress_size > 1000:
                raise PortableArchiveError("便携包压缩率异常")
        files.append(info)
    if "attachments/" not in seen:
        raise PortableArchiveError("便携包缺少 attachments 目录")
    return files, directories


def extract_zip_safely(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=False, mode=0o700)
    try:
        with zipfile.ZipFile(source, "r") as archive:
            files, directories = _safe_zip_members(archive)
            for info in directories:
                target = destination.joinpath(*PurePosixPath(info.filename).parts)
                target.mkdir(parents=True, exist_ok=True, mode=0o700)
            for info in files:
                target = destination.joinpath(*PurePosixPath(info.filename).parts)
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with archive.open(info, "r") as source_handle, target.open("wb") as target_handle:
                    shutil.copyfileobj(source_handle, target_handle, length=1024 * 1024)
                os.chmod(target, 0o600)
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise PortableArchiveError("无法安全读取便携包") from exc


def _inspect_directory(root: Path) -> set[str]:
    if not root.is_dir() or root.is_symlink():
        raise PortableArchiveError("便携包解压目录无效")
    attachments = root / "attachments"
    if not attachments.is_dir() or attachments.is_symlink():
        raise PortableArchiveError("便携包缺少安全的 attachments 目录")
    files: set[str] = set()
    for current, directories, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        for directory in directories:
            candidate = current_path / directory
            if candidate.is_symlink():
                raise PortableArchiveError("便携包解压目录不允许符号链接")
            normalized_member_name(candidate.relative_to(root).as_posix())
        for filename in filenames:
            candidate = current_path / filename
            if candidate.is_symlink() or not candidate.is_file():
                raise PortableArchiveError("便携包解压目录只允许普通文件")
            files.add(normalized_member_name(candidate.relative_to(root).as_posix()))
    return files


def _load_json(path: Path) -> Any:
    if not path.is_file() or path.stat().st_size > MAX_JSON_BYTES:
        raise PortableArchiveError("便携包 JSON 文件缺失或过大")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PortableArchiveError("便携包 JSON 无法解析") from exc


def _load_csv(path: Path) -> list[dict[str, str]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle))
    except (OSError, UnicodeError, csv.Error) as exc:
        raise PortableArchiveError("便携包 CSV 无法解析") from exc


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise PortableArchiveError("便携包审计记录不是对象")
                result.append(value)
    except PortableArchiveError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PortableArchiveError("便携包审计记录无法解析") from exc
    return result


def _sqlite_rows(
    connection: sqlite3.Connection,
    statement: str,
    parameters: Iterable[Any] = (),
) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(statement, tuple(parameters)).fetchall()]


def grouped_tags(values: Iterable[str]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for value in values:
        dimension, separator, label = str(value).partition(":")
        if not separator or dimension not in TAG_DIMENSIONS or not label:
            raise PortableArchiveError("便携包存在无效分组标签")
        result.setdefault(dimension, []).append(label)
    return result


def summary_from_transactions(
    transactions: list[dict[str, Any]],
    category_order: list[str],
) -> dict[str, Any]:
    active_payments = [
        row for row in transactions if row["kind"] == "payment" and row["status"] == "active"
    ]
    active_refunds = [
        row for row in transactions if row["kind"] == "refund" and row["status"] == "active"
    ]
    category_data: dict[str, dict[str, int]] = {
        category: {
            "payments_count": 0,
            "refunds_count": 0,
            "deposit_count": 0,
            "deposit_cents": 0,
            "payments_cents": 0,
            "refunds_cents": 0,
        }
        for category in category_order
    }
    tag_data: dict[str, dict[str, Any]] = {}
    for row in active_payments:
        category = str(row["effective_category"])
        if category not in category_data:
            raise PortableArchiveError("便携包汇总缺少付款主分类")
        bucket = category_data[category]
        bucket["payments_count"] += 1
        bucket["payments_cents"] += int(row["amount_cents"])
        if row["is_deposit"]:
            bucket["deposit_count"] += 1
            bucket["deposit_cents"] += int(row["amount_cents"])
        for tag in row["tags"]:
            key = unicodedata.normalize("NFKC", str(tag)).casefold()
            tag_bucket = tag_data.setdefault(
                key,
                {
                    "tag": str(tag),
                    "payments_count": 0,
                    "refunds_count": 0,
                    "payments_cents": 0,
                    "refunds_cents": 0,
                },
            )
            tag_bucket["tag"] = min(str(tag_bucket["tag"]), str(tag))
            tag_bucket["payments_count"] += 1
            tag_bucket["payments_cents"] += int(row["amount_cents"])
    for row in active_refunds:
        category = str(row["effective_category"])
        if category not in category_data:
            raise PortableArchiveError("便携包汇总缺少退款主分类")
        bucket = category_data[category]
        bucket["refunds_count"] += 1
        bucket["refunds_cents"] += int(row["amount_cents"])
        for tag in row["tags"]:
            key = unicodedata.normalize("NFKC", str(tag)).casefold()
            tag_bucket = tag_data.setdefault(
                key,
                {
                    "tag": str(tag),
                    "payments_count": 0,
                    "refunds_count": 0,
                    "payments_cents": 0,
                    "refunds_cents": 0,
                },
            )
            tag_bucket["tag"] = min(str(tag_bucket["tag"]), str(tag))
            tag_bucket["refunds_count"] += 1
            tag_bucket["refunds_cents"] += int(row["amount_cents"])
    categories: list[dict[str, Any]] = []
    for category in category_order:
        item = {"category": category, **category_data[category]}
        item["net_cents"] = item["payments_cents"] - item["refunds_cents"]
        categories.append(item)
    tags: list[dict[str, Any]] = []
    for item in tag_data.values():
        value = dict(item)
        value["net_cents"] = value["payments_cents"] - value["refunds_cents"]
        tags.append(value)
    tags.sort(key=lambda item: (-item["net_cents"], item["tag"]))
    dates = [str(row["date"]) for row in transactions if row["status"] == "active"]
    payments_cents = sum(int(row["amount_cents"]) for row in active_payments)
    refunds_cents = sum(int(row["amount_cents"]) for row in active_refunds)
    return {
        "from": None,
        "to": None,
        "first_date": min(dates) if dates else None,
        "last_date": max(dates) if dates else None,
        "payments_count": len(active_payments),
        "refunds_count": len(active_refunds),
        "deposit_count": sum(1 for row in active_payments if row["is_deposit"]),
        "deposit_cents": sum(
            int(row["amount_cents"]) for row in active_payments if row["is_deposit"]
        ),
        "payments_cents": payments_cents,
        "refunds_cents": refunds_cents,
        "net_cents": payments_cents - refunds_cents,
        "categories": categories,
        "tags": tags,
    }


def summary_from_grouped_transactions(transactions: list[dict[str, Any]]) -> dict[str, Any]:
    active_payments = [
        row for row in transactions if row["kind"] == "payment" and row["status"] == "active"
    ]
    active_refunds = [
        row for row in transactions if row["kind"] == "refund" and row["status"] == "active"
    ]
    tag_data: dict[str, dict[str, Any]] = {}
    for row, kind in [
        *((item, "payment") for item in active_payments),
        *((item, "refund") for item in active_refunds),
    ]:
        for tag in row["tags"]:
            tag_key = unicodedata.normalize("NFKC", str(tag)).casefold()
            dimension, _, value = str(tag).partition(":")
            bucket = tag_data.setdefault(
                tag_key,
                {
                    "tag": str(tag),
                    "dimension": dimension,
                    "value": value,
                    "payments_count": 0,
                    "refunds_count": 0,
                    "payments_cents": 0,
                    "refunds_cents": 0,
                },
            )
            bucket[f"{kind}s_count"] += 1
            bucket[f"{kind}s_cents"] += int(row["amount_cents"])
    tags: list[dict[str, Any]] = []
    dimensions: dict[str, list[dict[str, Any]]] = {
        dimension: [] for dimension in TAG_DIMENSIONS
    }
    for item in tag_data.values():
        value = dict(item)
        value["net_cents"] = value["payments_cents"] - value["refunds_cents"]
        tags.append(value)
        dimensions[value["dimension"]].append(value)
    tags.sort(key=lambda item: (-item["net_cents"], item["tag"]))
    for items in dimensions.values():
        items.sort(key=lambda item: (-item["net_cents"], item["value"]))
    dates = [str(row["date"]) for row in transactions if row["status"] == "active"]
    payments_cents = sum(int(row["amount_cents"]) for row in active_payments)
    refunds_cents = sum(int(row["amount_cents"]) for row in active_refunds)
    return {
        "from": None,
        "to": None,
        "first_date": min(dates) if dates else None,
        "last_date": max(dates) if dates else None,
        "payments_count": len(active_payments),
        "refunds_count": len(active_refunds),
        "deposit_count": sum(1 for row in active_payments if row["is_deposit"]),
        "deposit_cents": sum(
            int(row["amount_cents"]) for row in active_payments if row["is_deposit"]
        ),
        "payments_cents": payments_cents,
        "refunds_cents": refunds_cents,
        "net_cents": payments_cents - refunds_cents,
        "tags": tags,
        "dimensions": dimensions,
    }


def monthly_summary(transactions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, int]] = {}
    for row in transactions:
        if row["status"] != "active":
            continue
        month = str(row["date"])[:7]
        bucket = buckets.setdefault(
            month,
            {
                "payments_count": 0,
                "refunds_count": 0,
                "deposit_count": 0,
                "payments_cents": 0,
                "refunds_cents": 0,
            },
        )
        if row["kind"] == "payment":
            bucket["payments_count"] += 1
            bucket["payments_cents"] += int(row["amount_cents"])
            if row["is_deposit"]:
                bucket["deposit_count"] += 1
        else:
            bucket["refunds_count"] += 1
            bucket["refunds_cents"] += int(row["amount_cents"])
    result = []
    for month in sorted(buckets):
        item = {"month": month, **buckets[month]}
        item["net_cents"] = item["payments_cents"] - item["refunds_cents"]
        result.append(item)
    return result


def _snapshot_state(database: Path, expected_summary: dict[str, Any]) -> dict[str, Any]:
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        if str(connection.execute("PRAGMA integrity_check").fetchone()[0]) != "ok":
            raise PortableArchiveError("便携包 SQLite 完整性检查失败")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise PortableArchiveError("便携包 SQLite 外键检查失败")
        transactions = _sqlite_rows(
            connection,
            """
            SELECT t.*, p.category AS linked_category
            FROM transactions t
            LEFT JOIN transactions p ON p.id=t.payment_id
            ORDER BY t.id
            """,
        )
        tag_rows = _sqlite_rows(
            connection,
            """
            SELECT id,transaction_id,tag,tag_key,position,created_at
            FROM transaction_tags ORDER BY transaction_id,position,id
            """,
        )
        attachment_rows = _sqlite_rows(
            connection,
            """
            SELECT id,transaction_id,original_filename,relative_path,sha256,
                   size_bytes,media_type,created_at
            FROM attachments ORDER BY id
            """,
        )
        audit_rows = _sqlite_rows(
            connection,
            """
            SELECT id,transaction_id,action,actor,before_json,after_json,reason,created_at
            FROM audit_log ORDER BY id
            """,
        )
        metadata = {
            str(row["key"]): str(row["value"])
            for row in connection.execute("SELECT key,value FROM metadata ORDER BY key")
        }
    except PortableArchiveError:
        raise
    except sqlite3.Error as exc:
        raise PortableArchiveError("便携包 SQLite 结构不兼容") from exc
    finally:
        if connection is not None:
            connection.close()

    payment_ids = {int(row["id"]) for row in transactions if row["kind"] == "payment"}
    tags_by_payment: dict[int, list[str]] = defaultdict(list)
    tag_keys_by_payment: dict[int, set[str]] = defaultdict(set)
    positions_by_payment: dict[int, set[int]] = defaultdict(set)
    for row in tag_rows:
        transaction_id = int(row["transaction_id"])
        if transaction_id not in payment_ids:
            raise PortableArchiveError("便携包标签关联到非付款流水")
        tag = str(row["tag"])
        tag_key = str(row["tag_key"])
        position = int(row["position"])
        if not tag.strip() or tag != tag.strip() or len(tag) > 20:
            raise PortableArchiveError("便携包标签文本不符合约束")
        if unicodedata.normalize("NFKC", tag).casefold() != tag_key:
            raise PortableArchiveError("便携包标签规范键不一致")
        if tag_key in tag_keys_by_payment[transaction_id] or position in positions_by_payment[transaction_id]:
            raise PortableArchiveError("便携包付款存在重复标签或位置")
        tag_keys_by_payment[transaction_id].add(tag_key)
        positions_by_payment[transaction_id].add(position)
        tags_by_payment[transaction_id].append(tag)
    for transaction_id, tags in tags_by_payment.items():
        if len(tags) > 8 or positions_by_payment[transaction_id] != set(range(len(tags))):
            raise PortableArchiveError("便携包付款标签数量或顺序不符合约束")

    by_id = {int(row["id"]): row for row in transactions}
    refunds_by_payment: dict[int, int] = defaultdict(int)
    serialized: list[dict[str, Any]] = []
    for row in transactions:
        transaction_id = int(row["id"])
        amount_cents = int(row["amount_cents"])
        if amount_cents <= 0:
            raise PortableArchiveError("便携包存在非正金额")
        kind = str(row["kind"])
        status = str(row["status"])
        if status not in {"active", "void"}:
            raise PortableArchiveError("便携包存在未知流水状态")
        payment_id = int(row["payment_id"]) if row["payment_id"] is not None else None
        if kind == "payment":
            if payment_id is not None or row["category"] is None:
                raise PortableArchiveError("便携包付款结构无效")
            effective_category = str(row["category"])
            tags = tags_by_payment.get(transaction_id, [])
        elif kind == "refund":
            parent = by_id.get(payment_id or -1)
            if parent is None or parent["kind"] != "payment":
                raise PortableArchiveError("便携包退款缺少有效原付款")
            if status == "active" and parent["status"] != "active":
                raise PortableArchiveError("便携包有效退款关联已撤销付款")
            effective_category = str(parent["category"])
            tags = tags_by_payment.get(int(parent["id"]), [])
            if status == "active":
                refunds_by_payment[int(parent["id"])] += amount_cents
        else:
            raise PortableArchiveError("便携包存在未知流水类型")
        serialized.append(
            {
                "id": transaction_id,
                "kind": kind,
                "payment_id": payment_id,
                "amount_cents": amount_cents,
                "amount": f"{amount_cents // 100}.{amount_cents % 100:02d}",
                "date": str(row["txn_date"]),
                "category": row["category"],
                "effective_category": effective_category,
                "vendor": str(row["vendor"]),
                "description": str(row["description"]),
                "is_deposit": bool(row["is_deposit"]),
                "status": status,
                "void_reason": str(row["void_reason"]),
                "created_at": str(row["created_at"]),
                "updated_at": str(row["updated_at"]),
                "tags": list(tags),
            }
        )
    for payment_id, refunded_cents in refunds_by_payment.items():
        if refunded_cents > int(by_id[payment_id]["amount_cents"]):
            raise PortableArchiveError("便携包累计退款超过原付款")

    parsed_audit: list[dict[str, Any]] = []
    for row in audit_rows:
        try:
            before = json.loads(row["before_json"]) if row["before_json"] else None
            after = json.loads(row["after_json"])
        except json.JSONDecodeError as exc:
            raise PortableArchiveError("便携包审计前后值无效") from exc
        parsed_audit.append(
            {
                "id": int(row["id"]),
                "transaction_id": int(row["transaction_id"]),
                "action": str(row["action"]),
                "actor": str(row["actor"]),
                "before": before,
                "after": after,
                "reason": str(row["reason"]),
                "created_at": str(row["created_at"]),
            }
        )

    active_payments = [row for row in serialized if row["kind"] == "payment" and row["status"] == "active"]
    active_refunds = [row for row in serialized if row["kind"] == "refund" and row["status"] == "active"]
    payments_cents = sum(int(row["amount_cents"]) for row in active_payments)
    refunds_cents = sum(int(row["amount_cents"]) for row in active_refunds)
    invariants = {
        "transaction_count": len(serialized),
        "active_payment_count": len(active_payments),
        "active_refund_count": len(active_refunds),
        "active_deposit_count": sum(1 for row in active_payments if row["is_deposit"]),
        "void_transaction_count": sum(1 for row in serialized if row["status"] == "void"),
        "transaction_tag_count": len(tag_rows),
        "attachment_count": len(attachment_rows),
        "audit_count": len(parsed_audit),
        "max_transaction_id": max((int(row["id"]) for row in serialized), default=0),
        "payments_cents": payments_cents,
        "refunds_cents": refunds_cents,
        "net_cents": payments_cents - refunds_cents,
    }
    categories = expected_summary.get("categories")
    if not isinstance(categories, list):
        raise PortableArchiveError("便携包缺少分类汇总")
    category_order = [str(item.get("category")) for item in categories if isinstance(item, dict)]
    if len(category_order) != len(categories) or len(category_order) != len(set(category_order)):
        raise PortableArchiveError("便携包分类汇总结构无效")
    summary = summary_from_transactions(serialized, category_order)
    return {
        "metadata": metadata,
        "transactions": serialized,
        "transaction_tags": tag_rows,
        "attachments": attachment_rows,
        "audit_log": parsed_audit,
        "invariants": invariants,
        "summary": summary,
        "monthly_summary": monthly_summary(serialized),
    }


def _snapshot_state_v2(database: Path) -> dict[str, Any]:
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        if str(connection.execute("PRAGMA integrity_check").fetchone()[0]) != "ok":
            raise PortableArchiveError("便携包 SQLite 完整性检查失败")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise PortableArchiveError("便携包 SQLite 外键检查失败")
        transaction_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(transactions)")
        }
        if "category" in transaction_columns:
            raise PortableArchiveError("版本 2 便携账本不应包含主分类字段")
        transactions = _sqlite_rows(connection, "SELECT * FROM transactions ORDER BY id")
        tag_rows = _sqlite_rows(
            connection,
            """
            SELECT id,transaction_id,tag,tag_key,position,created_at
            FROM transaction_tags ORDER BY transaction_id,position,id
            """,
        )
        attachment_rows = _sqlite_rows(
            connection,
            """
            SELECT id,transaction_id,original_filename,relative_path,sha256,
                   size_bytes,media_type,created_at
            FROM attachments ORDER BY id
            """,
        )
        audit_rows = _sqlite_rows(
            connection,
            """
            SELECT id,transaction_id,action,actor,before_json,after_json,reason,created_at
            FROM audit_log ORDER BY id
            """,
        )
        metadata = {
            str(row["key"]): str(row["value"])
            for row in connection.execute("SELECT key,value FROM metadata ORDER BY key")
        }
    except PortableArchiveError:
        raise
    except sqlite3.Error as exc:
        raise PortableArchiveError("便携包 SQLite 结构不兼容") from exc
    finally:
        if connection is not None:
            connection.close()

    if metadata.get("schema_version") != "3":
        raise PortableArchiveError("版本 2 便携账本 SQLite schema_version 不是 3")
    payment_ids = {int(row["id"]) for row in transactions if row["kind"] == "payment"}
    tags_by_payment: dict[int, list[str]] = defaultdict(list)
    tag_keys_by_payment: dict[int, set[str]] = defaultdict(set)
    positions_by_payment: dict[int, set[int]] = defaultdict(set)
    for row in tag_rows:
        transaction_id = int(row["transaction_id"])
        if transaction_id not in payment_ids:
            raise PortableArchiveError("便携包标签关联到非付款流水")
        tag = str(row["tag"])
        tag_key = str(row["tag_key"])
        position = int(row["position"])
        if not tag.strip() or tag != tag.strip() or len(tag) > MAX_GROUPED_TAG_LENGTH:
            raise PortableArchiveError("便携包分组标签文本不符合约束")
        grouped_tags([tag])
        if unicodedata.normalize("NFKC", tag).casefold() != tag_key:
            raise PortableArchiveError("便携包分组标签规范键不一致")
        if tag_key in tag_keys_by_payment[transaction_id]:
            raise PortableArchiveError("便携包付款存在重复分组标签")
        if position in positions_by_payment[transaction_id]:
            raise PortableArchiveError("便携包付款存在重复标签位置")
        tag_keys_by_payment[transaction_id].add(tag_key)
        positions_by_payment[transaction_id].add(position)
        tags_by_payment[transaction_id].append(tag)
    for transaction_id, tags in tags_by_payment.items():
        if len(tags) > MAX_GROUPED_TAGS:
            raise PortableArchiveError("便携包付款分组标签数量超过上限")
        if positions_by_payment[transaction_id] != set(range(len(tags))):
            raise PortableArchiveError("便携包付款分组标签顺序不连续")

    by_id = {int(row["id"]): row for row in transactions}
    refunds_by_payment: dict[int, int] = defaultdict(int)
    serialized: list[dict[str, Any]] = []
    for row in transactions:
        transaction_id = int(row["id"])
        amount_cents = int(row["amount_cents"])
        if amount_cents <= 0:
            raise PortableArchiveError("便携包存在非正金额")
        kind = str(row["kind"])
        status = str(row["status"])
        if status not in {"active", "void"}:
            raise PortableArchiveError("便携包存在未知流水状态")
        payment_id = int(row["payment_id"]) if row["payment_id"] is not None else None
        if kind == "payment":
            if payment_id is not None:
                raise PortableArchiveError("便携包付款结构无效")
            effective_tags = tags_by_payment.get(transaction_id, [])
        elif kind == "refund":
            parent = by_id.get(payment_id or -1)
            if parent is None or parent["kind"] != "payment":
                raise PortableArchiveError("便携包退款缺少有效原付款")
            if status == "active" and parent["status"] != "active":
                raise PortableArchiveError("便携包有效退款关联已撤销付款")
            effective_tags = tags_by_payment.get(int(parent["id"]), [])
            if status == "active":
                refunds_by_payment[int(parent["id"])] += amount_cents
        else:
            raise PortableArchiveError("便携包存在未知流水类型")
        serialized.append(
            {
                "id": transaction_id,
                "kind": kind,
                "payment_id": payment_id,
                "amount_cents": amount_cents,
                "amount": f"{amount_cents // 100}.{amount_cents % 100:02d}",
                "date": str(row["txn_date"]),
                "vendor": str(row["vendor"]),
                "description": str(row["description"]),
                "is_deposit": bool(row["is_deposit"]),
                "status": status,
                "void_reason": str(row["void_reason"]),
                "created_at": str(row["created_at"]),
                "updated_at": str(row["updated_at"]),
                "tags": list(effective_tags),
                "grouped_tags": grouped_tags(effective_tags),
            }
        )
    for payment_id, refunded_cents in refunds_by_payment.items():
        if refunded_cents > int(by_id[payment_id]["amount_cents"]):
            raise PortableArchiveError("便携包累计退款超过原付款")

    parsed_audit: list[dict[str, Any]] = []
    for row in audit_rows:
        try:
            before = json.loads(row["before_json"]) if row["before_json"] else None
            after = json.loads(row["after_json"])
        except json.JSONDecodeError as exc:
            raise PortableArchiveError("便携包审计前后值无效") from exc
        parsed_audit.append(
            {
                "id": int(row["id"]),
                "transaction_id": int(row["transaction_id"]),
                "action": str(row["action"]),
                "actor": str(row["actor"]),
                "before": before,
                "after": after,
                "reason": str(row["reason"]),
                "created_at": str(row["created_at"]),
            }
        )

    active_payments = [
        row for row in serialized if row["kind"] == "payment" and row["status"] == "active"
    ]
    active_refunds = [
        row for row in serialized if row["kind"] == "refund" and row["status"] == "active"
    ]
    payments_cents = sum(int(row["amount_cents"]) for row in active_payments)
    refunds_cents = sum(int(row["amount_cents"]) for row in active_refunds)
    invariants = {
        "transaction_count": len(serialized),
        "active_payment_count": len(active_payments),
        "active_refund_count": len(active_refunds),
        "active_deposit_count": sum(1 for row in active_payments if row["is_deposit"]),
        "void_transaction_count": sum(1 for row in serialized if row["status"] == "void"),
        "transaction_tag_count": len(tag_rows),
        "attachment_count": len(attachment_rows),
        "audit_count": len(parsed_audit),
        "max_transaction_id": max((int(row["id"]) for row in serialized), default=0),
        "payments_cents": payments_cents,
        "refunds_cents": refunds_cents,
        "net_cents": payments_cents - refunds_cents,
    }
    return {
        "metadata": metadata,
        "transactions": serialized,
        "transaction_tags": tag_rows,
        "attachments": attachment_rows,
        "audit_log": parsed_audit,
        "invariants": invariants,
        "summary": summary_from_grouped_transactions(serialized),
        "monthly_summary": monthly_summary(serialized),
    }


def _expected_transaction_csv(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    fields = (
        "id",
        "kind",
        "payment_id",
        "amount_cents",
        "amount",
        "date",
        "category",
        "effective_category",
        "vendor",
        "description",
        "is_deposit",
        "status",
        "void_reason",
        "created_at",
        "updated_at",
        "tags_json",
    )
    result: list[dict[str, str]] = []
    for row in rows:
        values = dict(row)
        values["tags_json"] = json.dumps(row["tags"], ensure_ascii=False, separators=(",", ":"))
        values.pop("tags", None)
        result.append(
            {field: "" if values.get(field) is None else str(values.get(field)) for field in fields}
        )
    return result


def _expected_transaction_csv_v2(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    fields = (
        "id",
        "kind",
        "payment_id",
        "amount_cents",
        "amount",
        "date",
        "vendor",
        "description",
        "is_deposit",
        "status",
        "void_reason",
        "created_at",
        "updated_at",
        "tags_json",
        "grouped_tags_json",
    )
    result: list[dict[str, str]] = []
    for row in rows:
        values = dict(row)
        values["tags_json"] = json.dumps(
            row["tags"], ensure_ascii=False, separators=(",", ":")
        )
        values["grouped_tags_json"] = json.dumps(
            row["grouped_tags"], ensure_ascii=False, separators=(",", ":")
        )
        values.pop("tags", None)
        values.pop("grouped_tags", None)
        result.append(
            {field: "" if values.get(field) is None else str(values.get(field)) for field in fields}
        )
    return result


def _expected_tag_csv(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    fields = ("id", "transaction_id", "tag", "tag_key", "position", "created_at")
    return [{field: str(row[field]) for field in fields} for row in rows]


def _expected_attachment_csv(rows: list[dict[str, Any]], included: bool) -> list[dict[str, str]]:
    fields = (
        "id",
        "transaction_id",
        "original_filename",
        "relative_path",
        "export_path",
        "sha256",
        "size_bytes",
        "media_type",
        "created_at",
        "included",
    )
    result: list[dict[str, str]] = []
    for row in rows:
        values = dict(row)
        values["export_path"] = f"attachments/{row['relative_path']}"
        values["included"] = included
        result.append({field: str(values[field]) for field in fields})
    return result


def verify_extracted(root: Path) -> dict[str, Any]:
    actual_files = _inspect_directory(root)
    missing_roots = REQUIRED_ROOT_FILES - actual_files
    if missing_roots:
        raise PortableArchiveError("便携包缺少固定文件")
    manifest = _load_json(root / "manifest.json")
    if not isinstance(manifest, dict):
        raise PortableArchiveError("便携包 manifest 不是对象")
    format_version = manifest.get("format_version")
    if manifest.get("format_id") != FORMAT_ID or format_version not in SUPPORTED_FORMAT_VERSIONS:
        raise PortableArchiveError("便携包格式 ID 或版本不受支持")
    if manifest.get("currency") != "CNY" or manifest.get("amount_unit") != "integer_cents":
        raise PortableArchiveError("便携包币种或金额单位无效")
    if manifest.get("hermes_required") is not False:
        raise PortableArchiveError("便携包未声明框架无关")
    semantics = manifest.get("semantics")
    if not isinstance(semantics, dict):
        raise PortableArchiveError("便携包账务语义声明不完整")
    if format_version == FORMAT_VERSION:
        if any(
            semantics.get(key) is not True
            for key in (
                "primary_category_single",
                "refunds_link_to_payments",
                "refunds_inherit_category_and_tags",
                "tag_totals_overlap",
                "tag_totals_must_not_be_summed",
                "void_records_are_retained",
            )
        ):
            raise PortableArchiveError("便携包版本 1 账务语义声明不完整")
    elif (
        semantics.get("primary_category_single") is not False
        or semantics.get("grouped_multi_tags") is not True
        or semantics.get("tag_totals_overlap") is not True
        or semantics.get("total_ledger_deduplicates_transactions") is not True
    ):
        raise PortableArchiveError("便携包版本 2 账务语义声明不完整")
    export = manifest.get("export")
    if not isinstance(export, dict) or export.get("manifest_self_excluded_from_hashes") is not True:
        raise PortableArchiveError("便携包 manifest 哈希规则不明确")
    manifest_files = manifest.get("files")
    if not isinstance(manifest_files, list):
        raise PortableArchiveError("便携包 manifest.files 不是数组")
    expected_files = {"manifest.json"}
    for item in manifest_files:
        if not isinstance(item, dict):
            raise PortableArchiveError("便携包 manifest 文件项无效")
        relative = normalized_member_name(str(item.get("path", "")))
        if relative == "manifest.json" or relative in expected_files:
            raise PortableArchiveError("便携包 manifest 包含重复文件项")
        path = root.joinpath(*PurePosixPath(relative).parts)
        if not path.is_file() or path.is_symlink():
            raise PortableArchiveError("便携包 manifest 文件不存在")
        digest, size = sha256_file(path)
        if digest != item.get("sha256") or size != item.get("size_bytes"):
            raise PortableArchiveError("便携包文件哈希或大小不匹配")
        expected_files.add(relative)
    if actual_files != expected_files:
        raise PortableArchiveError("便携包文件集合与 manifest 不一致")

    ledger = _load_json(root / "ledger.json")
    schema = _load_json(root / "schema.json")
    if not isinstance(ledger, dict) or not isinstance(schema, dict):
        raise PortableArchiveError("便携包账本或 Schema 不是对象")
    if schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
        raise PortableArchiveError("便携包 Schema 版本无效")
    if ledger.get("format_id") != FORMAT_ID or ledger.get("format_version") != format_version:
        raise PortableArchiveError("便携包 ledger 格式无效")
    if format_version == FORMAT_VERSION and (
        ledger.get("currency") != "CNY" or ledger.get("amount_unit") != "integer_cents"
    ):
        raise PortableArchiveError("便携包 ledger 币种或金额单位无效")
    if not (root / "FORMAT.md").read_text(encoding="utf-8").strip():
        raise PortableArchiveError("便携包格式说明为空")
    expected_summary = ledger.get("summary")
    if not isinstance(expected_summary, dict):
        raise PortableArchiveError("便携包缺少汇总")
    state = (
        _snapshot_state(root / "bookkeeping.sqlite3", expected_summary)
        if format_version == FORMAT_VERSION
        else _snapshot_state_v2(root / "bookkeeping.sqlite3")
    )
    for key in ("metadata", "transactions", "transaction_tags", "attachments", "audit_log", "summary"):
        if ledger.get(key) != state[key]:
            raise PortableArchiveError(f"便携包 {key} 与 SQLite 不一致")
    if ledger.get("invariants") != state["invariants"] or manifest.get("invariants") != state["invariants"]:
        raise PortableArchiveError("便携包账务不变量与 SQLite 不一致")
    expected_transactions_csv = (
        _expected_transaction_csv(state["transactions"])
        if format_version == FORMAT_VERSION
        else _expected_transaction_csv_v2(state["transactions"])
    )
    if _load_csv(root / "transactions.csv") != expected_transactions_csv:
        raise PortableArchiveError("便携包 transactions.csv 与 SQLite 不一致")
    if _load_csv(root / "transaction_tags.csv") != _expected_tag_csv(state["transaction_tags"]):
        raise PortableArchiveError("便携包 transaction_tags.csv 与 SQLite 不一致")
    attachments_included = export.get("attachments_included")
    if not isinstance(attachments_included, bool):
        raise PortableArchiveError("便携包未声明附件包含状态")
    if _load_csv(root / "attachments.csv") != _expected_attachment_csv(
        state["attachments"], attachments_included
    ):
        raise PortableArchiveError("便携包 attachments.csv 与 SQLite 不一致")
    if _load_jsonl(root / "audit_log.jsonl") != state["audit_log"]:
        raise PortableArchiveError("便携包 audit_log.jsonl 与 SQLite 不一致")

    attachment_paths: set[str] = set()
    for row in state["attachments"]:
        relative = normalized_member_name(f"attachments/{row['relative_path']}")
        attachment_paths.add(relative)
        path = root.joinpath(*PurePosixPath(relative).parts)
        if attachments_included:
            if not path.is_file() or path.is_symlink():
                raise PortableArchiveError("便携包缺少附件文件")
            digest, size = sha256_file(path)
            if digest != row["sha256"] or size != int(row["size_bytes"]):
                raise PortableArchiveError("便携包附件哈希或大小不匹配")
        elif path.exists():
            raise PortableArchiveError("未包含附件的便携包出现附件文件")
    actual_attachments = {path for path in actual_files if path.startswith("attachments/")}
    if actual_attachments != (attachment_paths if attachments_included else set()):
        raise PortableArchiveError("便携包附件文件集合与元数据不一致")
    return {
        "format_id": FORMAT_ID,
        "format_version": format_version,
        "attachments_included": attachments_included,
        "verified_file_count": len(actual_files),
        "state": state,
        "digests": {
            "transactions": digest_json(state["transactions"]),
            "transaction_tags": digest_json(state["transaction_tags"]),
            "attachments": digest_json(state["attachments"]),
            "audit_log": digest_json(state["audit_log"]),
            "summary": digest_json(state["summary"]),
            "monthly_summary": digest_json(state["monthly_summary"]),
            "invariants": digest_json(state["invariants"]),
        },
    }


def verify_and_extract(source: Path, destination: Path) -> dict[str, Any]:
    source = source.resolve()
    if not source.is_file() or source.stat().st_size > MAX_ARCHIVE_BYTES:
        raise PortableArchiveError("便携包不存在或超过大小上限")
    extract_zip_safely(source, destination)
    result = verify_extracted(destination)
    archive_sha256, archive_size = sha256_file(source)
    result["archive_sha256"] = archive_sha256
    result["archive_size_bytes"] = archive_size
    return result
