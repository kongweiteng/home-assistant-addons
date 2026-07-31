"""Normalize mixed upstream types into a stable, privacy-minimized schema."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import math


MAX_HISTORY_RECORDS = 500
MAX_METERS = 50
MAX_STEPS = 20
MAX_TREND_POINTS = 120


@dataclass(frozen=True)
class ContractError(Exception):
    kind: str
    endpoint: str

    def __str__(self) -> str:
        return f"upstream contract error endpoint={self.endpoint} kind={self.kind}"


@dataclass(frozen=True)
class NormalizedResponse:
    data: dict | list
    issues: tuple[str, ...]
    empty: bool


def normalize_response(endpoint: str, payload: dict) -> NormalizedResponse:
    result = _validated_result(endpoint, payload)
    issues: list[str] = []
    if endpoint == "customer_info":
        data = _customer_info(result, issues, endpoint)
        empty = False
    elif endpoint == "water_records":
        data = _water_records(result, issues, endpoint)
        empty = not data
    elif endpoint == "payment_records":
        data = _payment_records(result, issues, endpoint)
        empty = not data
    elif endpoint == "steps":
        data = _steps(result, issues, endpoint)
        empty = not data
    elif endpoint == "payment_summary":
        data = _payment_summary(result, issues, endpoint)
        empty = False
    else:
        raise ValueError("endpoint is not allowlisted")
    return NormalizedResponse(data=data, issues=tuple(issues), empty=empty)


def _validated_result(endpoint: str, payload: dict):
    if payload.get("success") is not True:
        raise ContractError("upstream_rejected", endpoint)
    result_code = payload.get("resultCode")
    if result_code is not None and str(result_code) != "0000000":
        raise ContractError("unexpected_result_code", endpoint)
    if "result" not in payload:
        raise ContractError("missing_result", endpoint)
    return payload["result"]


def _customer_info(result, issues: list[str], endpoint: str) -> dict:
    if not isinstance(result, dict):
        raise ContractError("result_not_object", endpoint)
    customer = _object(result.get("customer"), "customer", issues)
    water = _object(result.get("waterInfo"), "waterInfo", issues)
    meters_raw = _bounded(
        _list(result.get("meterInfos"), "meterInfos", issues),
        MAX_METERS,
        "meterInfos",
        issues,
    )
    trend = _object(result.get("waterTrend"), "waterTrend", issues)
    return {
        "customer": {
            "name": _text(customer.get("customerName"), "customer.name", issues),
            "address": _text(customer.get("customerAddr"), "customer.address", issues),
            "meter_count": _number(
                customer.get("customerMeterNum"), "customer.meter_count", issues
            ),
        },
        "water": {
            "remaining": _number(water.get("remaining"), "water.remaining", issues),
            "arrears": _number(water.get("arrears"), "water.arrears", issues),
            "meter_number": _text(
                water.get("meterNumber"), "water.meter_number", issues
            ),
            "population": _number(
                water.get("customerPopulation"), "water.population", issues
            ),
            "total_use": _number(water.get("totalUse"), "water.total_use", issues),
            "step": _text(water.get("step"), "water.step", issues),
            "step_name": _text(water.get("stepName"), "water.step_name", issues),
            "use_kind_type": _text(
                water.get("useKindType"), "water.use_kind_type", issues
            ),
        },
        "meters": [
            {
                "registration_no": _text(
                    item.get("registNo"), f"meters[{index}].registration_no", issues
                ),
                "location": _text(
                    item.get("meterLocation"), f"meters[{index}].location", issues
                ),
                "latest_reading_date": _text(
                    item.get("latestReadingDate"),
                    f"meters[{index}].latest_reading_date",
                    issues,
                ),
                "latest_reading": _number(
                    item.get("latestReading"),
                    f"meters[{index}].latest_reading",
                    issues,
                ),
            }
            for index, item in enumerate(_objects(meters_raw, "meters", issues))
        ],
        "trend": {
            "available": _boolean(
                trend.get("hasWaterTrend"), "trend.available", issues
            ),
            "labels": [
                _text(value, f"trend.labels[{index}]", issues)
                for index, value in enumerate(
                    _bounded(
                        _list(trend.get("x"), "trend.x", issues),
                        MAX_TREND_POINTS,
                        "trend.x",
                        issues,
                    )
                )
            ],
            "values": [
                _number(value, f"trend.values[{index}]", issues)
                for index, value in enumerate(
                    _bounded(
                        _list(trend.get("y"), "trend.y", issues),
                        MAX_TREND_POINTS,
                        "trend.y",
                        issues,
                    )
                )
            ],
        },
    }


def _water_records(result, issues: list[str], endpoint: str) -> list[dict]:
    records = _result_list(
        result,
        ("records", "list", "rows", "waterRecords", "waterRecordList"),
        endpoint,
    )
    records = _bounded(records, MAX_HISTORY_RECORDS, "water_records", issues)
    return [
        {
            "registration_no": _text(
                item.get("registNo"), f"water_records[{index}].registration_no", issues
            ),
            "meter_location": _text(
                item.get("meterLocation"),
                f"water_records[{index}].meter_location",
                issues,
            ),
            "billing_month": _text(
                item.get("calculateMonth"),
                f"water_records[{index}].billing_month",
                issues,
            ),
            "reading_time": _text(
                item.get("senseTime"), f"water_records[{index}].reading_time", issues
            ),
            "usage": _number(
                item.get("accountAmount"), f"water_records[{index}].usage", issues
            ),
            "charge": _number(
                item.get("receivableCharge"),
                f"water_records[{index}].charge",
                issues,
            ),
        }
        for index, item in enumerate(_objects(records, "water_records", issues))
    ]


def _payment_records(result, issues: list[str], endpoint: str) -> list[dict]:
    records = _result_list(
        result,
        ("records", "list", "rows", "paymentRecords", "paymentRecordList"),
        endpoint,
    )
    records = _bounded(records, MAX_HISTORY_RECORDS, "payment_records", issues)
    return [
        {
            "payment_mode": _text(
                item.get("paymentMode"),
                f"payment_records[{index}].payment_mode",
                issues,
            ),
            "payment_time": _text(
                item.get("chargeTime"),
                f"payment_records[{index}].payment_time",
                issues,
            ),
            "amount": _number(
                item.get("paymentMoney"),
                f"payment_records[{index}].amount",
                issues,
            ),
        }
        for index, item in enumerate(_objects(records, "payment_records", issues))
    ]


def _steps(result, issues: list[str], endpoint: str) -> list[dict]:
    records = _result_list(result, ("records", "list", "rows", "steps", "stepList"), endpoint)
    records = _bounded(records, MAX_STEPS, "steps", issues)
    return [
        {
            "name": _text(item.get("name"), f"steps[{index}].name", issues),
            "start": _number(
                item.get("stepStartValue"), f"steps[{index}].start", issues
            ),
            "end": _number(item.get("stepEndValue"), f"steps[{index}].end", issues),
            "used": _number(item.get("used"), f"steps[{index}].used", issues),
            "capacity": _number(
                item.get("capacity"), f"steps[{index}].capacity", issues
            ),
        }
        for index, item in enumerate(_objects(records, "steps", issues))
    ]


def _payment_summary(result, issues: list[str], endpoint: str) -> dict:
    if not isinstance(result, dict):
        raise ContractError("result_not_object", endpoint)
    return {
        "customer_name": _text(
            result.get("customerName"), "payment_summary.customer_name", issues
        ),
        "address": _text(result.get("address"), "payment_summary.address", issues),
        "remaining": _number(
            result.get("remaining"), "payment_summary.remaining", issues
        ),
        "arrears": _number(result.get("arrears"), "payment_summary.arrears", issues),
        "can_recharge": _boolean(
            result.get("canRecharge"), "payment_summary.can_recharge", issues
        ),
        "minimum_recharge": _number(
            result.get("minReCharge"), "payment_summary.minimum_recharge", issues
        ),
        "maximum_recharge": _number(
            result.get("maxReCharge"), "payment_summary.maximum_recharge", issues
        ),
    }


def _result_list(result, candidates: tuple[str, ...], endpoint: str) -> list:
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        for name in candidates:
            if isinstance(result.get(name), list):
                return result[name]
    raise ContractError("result_not_list", endpoint)


def _object(value, path: str, issues: list[str]) -> dict:
    if isinstance(value, dict):
        return value
    issues.append(f"{path}:expected_object")
    return {}


def _list(value, path: str, issues: list[str]) -> list:
    if isinstance(value, list):
        return value
    if value is not None:
        issues.append(f"{path}:expected_list")
    return []


def _objects(values: list, path: str, issues: list[str]) -> list[dict]:
    result: list[dict] = []
    for index, value in enumerate(values):
        if isinstance(value, dict):
            result.append(value)
        else:
            issues.append(f"{path}[{index}]:expected_object")
    return result


def _bounded(values: list, limit: int, path: str, issues: list[str]) -> list:
    if len(values) > limit:
        issues.append(f"{path}:truncated_to_{limit}")
        return values[:limit]
    return values


def _text(value, path: str, issues: list[str]) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        issues.append(f"{path}:invalid_text")
        return None
    if isinstance(value, (str, int, float)):
        return str(value).strip() or None
    issues.append(f"{path}:invalid_text")
    return None


def _number(value, path: str, issues: list[str]) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        issues.append(f"{path}:invalid_number")
        return None
    try:
        decimal = Decimal(str(value).replace(",", "").strip())
        number = float(decimal)
    except (InvalidOperation, ValueError):
        issues.append(f"{path}:invalid_number")
        return None
    if not math.isfinite(number):
        issues.append(f"{path}:invalid_number")
        return None
    return number


def _boolean(value, path: str, issues: list[str]) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    if value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes"}:
            return True
        if normalized in {"false", "0", "no"}:
            return False
    issues.append(f"{path}:invalid_boolean")
    return None
