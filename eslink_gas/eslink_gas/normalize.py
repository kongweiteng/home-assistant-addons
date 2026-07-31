"""Normalize the observed ESLink user-info response without inventing zeroes."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import math

from .client import ContractError
from .config import AccountConfig


def normalize_account(
    payload: dict,
    account: AccountConfig,
    *,
    include_personal_details: bool,
    fetched_at: datetime | None = None,
) -> dict:
    if payload.get("success") is not True:
        raise ContractError("success_flag_missing")
    returned_user_no = payload.get("userNo")
    if not isinstance(returned_user_no, str) or returned_user_no != account.user_no:
        raise ContractError("user_number_mismatch")
    meters_raw = payload.get("meterList")
    if not isinstance(meters_raw, list) or len(meters_raw) > 20:
        raise ContractError("meter_list_invalid")
    issues: list[str] = []
    meters: list[dict] = []
    for index, raw in enumerate(meters_raw):
        if not isinstance(raw, dict):
            issues.append(f"meter_{index}_not_object")
            continue
        balance = _decimal_text(raw.get("acctBalance"))
        if raw.get("acctBalance") not in {None, ""} and balance is None:
            issues.append(f"meter_{index}_balance_invalid")
        meters.append(
            {
                "balance": balance,
                "balance_description": _bounded_text(raw.get("acctBalanceDesc")),
                "meter_no_masked": _mask_identifier(raw.get("meterNo")),
                "meter_status": _bounded_text(raw.get("meterStatus")),
                "meter_status_id": _bounded_text(raw.get("meterStatusId")),
                "meter_type": _bounded_text(raw.get("meterType")),
                "meter_class": _bounded_text(raw.get("meterClass")),
                "price_name": _bounded_text(raw.get("priceName")),
                "purchase_command_status": _bounded_text(
                    raw.get("purchCommandStateDes")
                ),
            }
        )
    primary = meters[0] if meters else {}
    timestamp = (fetched_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    personal = _personal_fields(payload, include_personal_details)
    return {
        "account_id": account.id,
        "status": "ok" if meters else "no_meter",
        "available": True,
        "fetched_at": timestamp.isoformat().replace("+00:00", "Z"),
        "last_success_at": timestamp.isoformat().replace("+00:00", "Z"),
        "user_no_masked": _mask_identifier(returned_user_no),
        "account_organization": _bounded_text(payload.get("acctOrg")),
        "address_status": _bounded_text(payload.get("addrStatus")),
        "customer_class": _bounded_text(payload.get("custClass")),
        "service_point_rule": _bounded_text(payload.get("servicePointRuler")),
        "balance": primary.get("balance"),
        "meter_status": primary.get("meter_status"),
        "meter_count": len(meters),
        "meters": meters,
        "contract_issues": issues,
        "last_error": None,
        **personal,
    }


def _personal_fields(payload: dict, include: bool) -> dict:
    name = _bounded_text(payload.get("custName"), 128)
    address = _bounded_text(payload.get("addrDesc"), 512)
    mobile = _bounded_text(payload.get("custMobile"), 64)
    if include:
        return {
            "customer_name": name,
            "customer_address": address,
            "customer_mobile": mobile,
        }
    return {
        "customer_name": _mask_name(name),
        "customer_address": _mask_address(address),
        "customer_mobile": _mask_mobile(mobile),
    }


def _decimal_text(value) -> str | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not result.is_finite():
        return None
    text = format(result, "f")
    return "0" if text in {"-0", "-0.0"} else text


def _bounded_text(value, limit: int = 256) -> str | None:
    if value is None:
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    text = str(value).strip()
    return text[:limit] if text else None


def _mask_identifier(value) -> str | None:
    text = _bounded_text(value, 64)
    if text is None:
        return None
    if len(text) <= 4:
        return "*" * len(text)
    return "*" * (len(text) - 4) + text[-4:]


def _mask_name(value: str | None) -> str | None:
    if not value:
        return None
    if len(value) == 1:
        return "*"
    return value[0] + "*" * max(1, len(value) - 2) + value[-1]


def _mask_mobile(value: str | None) -> str | None:
    if not value:
        return None
    if len(value) < 7:
        return "*" * len(value)
    return value[:3] + "*" * (len(value) - 7) + value[-4:]


def _mask_address(value: str | None) -> str | None:
    if not value:
        return None
    if len(value) <= 8:
        return value[0] + "*" * max(1, len(value) - 2) + value[-1]
    return value[:4] + "***" + value[-4:]
