"""Derive bounded year/month statistics from normalized account history."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import math
import re


SEPARATED_YEAR_MONTH = re.compile(
    r"(?<!\d)(?P<year>\d{4})\s*(?:年|[-/.])\s*(?P<month>\d{1,2})(?!\d)"
)
COMPACT_YEAR_MONTH = re.compile(
    r"^(?P<year>\d{4})(?P<month>\d{2})(?:\d{2})?(?:\D|$)"
)


def build_statistics(water_records, payment_records) -> dict:
    """Build JSON-safe statistics without turning missing values into zero."""
    buckets: dict[int, list[dict]] = {}
    unparsed_water_records = 0
    unparsed_payment_records = 0

    for record in _records(water_records):
        year_month = _year_month(record.get("billing_month")) or _year_month(
            record.get("reading_time")
        )
        if year_month is None:
            unparsed_water_records += 1
            continue
        year, month = year_month
        bucket = _bucket(buckets, year, month)
        bucket["water_record_count"] += 1
        _add_value(bucket, "usage", record.get("usage"))
        _add_value(bucket, "charge", record.get("charge"))

    for record in _records(payment_records):
        year_month = _year_month(record.get("payment_time"))
        if year_month is None:
            unparsed_payment_records += 1
            continue
        year, month = year_month
        bucket = _bucket(buckets, year, month)
        bucket["payment_record_count"] += 1
        _add_value(bucket, "payments", record.get("amount"))

    years = sorted(buckets, reverse=True)
    monthly_by_year: dict[str, list[dict]] = {}
    yearly: list[dict] = []
    for year in years:
        months = [_public_month(bucket) for bucket in buckets[year]]
        monthly_by_year[str(year)] = months
        yearly.append(_year_summary(year, months))

    summaries = {summary["year"]: summary for summary in yearly}
    for summary in yearly:
        previous = summaries.get(summary["year"] - 1)
        summary["usage_year_over_year_percent"] = _percentage_change(
            summary["usage"], previous.get("usage") if previous else None
        )

    return {
        "years": years,
        "latest_year": years[0] if years else None,
        "yearly": yearly,
        "monthly_by_year": monthly_by_year,
        "unparsed_water_records": unparsed_water_records,
        "unparsed_payment_records": unparsed_payment_records,
    }


def _records(value) -> list[dict]:
    if not isinstance(value, list):
        return []
    return [record for record in value if isinstance(record, dict)]


def _year_month(value) -> tuple[int, int] | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    match = SEPARATED_YEAR_MONTH.search(text) or COMPACT_YEAR_MONTH.search(text)
    if match is None:
        return None
    year = int(match.group("year"))
    month = int(match.group("month"))
    if not 1900 <= year <= 2200 or not 1 <= month <= 12:
        return None
    return year, month


def _bucket(buckets: dict[int, list[dict]], year: int, month: int) -> dict:
    if year not in buckets:
        buckets[year] = [_new_month(index) for index in range(1, 13)]
    return buckets[year][month - 1]


def _new_month(month: int) -> dict:
    return {
        "month": month,
        "water_record_count": 0,
        "payment_record_count": 0,
        "usage_total": Decimal("0"),
        "usage_value_count": 0,
        "charge_total": Decimal("0"),
        "charge_value_count": 0,
        "payments_total": Decimal("0"),
        "payments_value_count": 0,
    }


def _add_value(bucket: dict, name: str, value) -> None:
    number = _decimal(value)
    if number is None:
        return
    bucket[f"{name}_total"] += number
    bucket[f"{name}_value_count"] += 1


def _decimal(value) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not math.isfinite(float(number)):
        return None
    return number


def _public_month(bucket: dict) -> dict:
    return {
        "month": bucket["month"],
        "water_record_count": bucket["water_record_count"],
        "payment_record_count": bucket["payment_record_count"],
        "usage": _optional_number(bucket, "usage", 3),
        "usage_value_count": bucket["usage_value_count"],
        "charge": _optional_number(bucket, "charge", 2),
        "charge_value_count": bucket["charge_value_count"],
        "payments": _optional_number(bucket, "payments", 2),
        "payments_value_count": bucket["payments_value_count"],
    }


def _optional_number(bucket: dict, name: str, places: int) -> float | None:
    if bucket[f"{name}_value_count"] == 0:
        return None
    return round(float(bucket[f"{name}_total"]), places)


def _year_summary(year: int, months: list[dict]) -> dict:
    usage = _sum_known(months, "usage", 3)
    charge = _sum_known(months, "charge", 2)
    payments = _sum_known(months, "payments", 2)
    months_with_usage = sum(month["usage"] is not None for month in months)
    return {
        "year": year,
        "usage": usage,
        "charge": charge,
        "payments": payments,
        "average_monthly_usage": (
            round(usage / months_with_usage, 3)
            if usage is not None and months_with_usage
            else None
        ),
        "months_with_water_records": sum(
            month["water_record_count"] > 0 for month in months
        ),
        "months_with_payment_records": sum(
            month["payment_record_count"] > 0 for month in months
        ),
        "months_with_usage": months_with_usage,
        "water_record_count": sum(month["water_record_count"] for month in months),
        "payment_record_count": sum(
            month["payment_record_count"] for month in months
        ),
    }


def _sum_known(months: list[dict], name: str, places: int) -> float | None:
    values = [month[name] for month in months if month[name] is not None]
    if not values:
        return None
    return round(sum(values), places)


def _percentage_change(current: float | None, previous: float | None) -> float | None:
    if current is None or previous is None or previous == 0:
        return None
    return round((current - previous) / previous * 100, 1)
