"""Allowlisted deterministic metadata rules for IDP."""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date
from typing import Mapping

from .models import FieldAcceptance, FieldOrigin, IDPContractError


@dataclass(frozen=True, slots=True)
class DerivedValue:
    field: str
    value: str
    origin: FieldOrigin = FieldOrigin.DERIVED
    acceptance: FieldAcceptance = FieldAcceptance.PROVISIONAL
    rule_id: str = ""
    rule_version: str = "1.0.0"
    inputs: tuple[str, ...] = ()
    parameters: Mapping[str, object] | None = None
    reason: str = "Estimated deterministic calendar anniversary; not a legal certification."


def _parse_date(value: object, name: str) -> date:
    if not isinstance(value, str):
        raise IDPContractError(f"{name} must be an ISO date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise IDPContractError(f"{name} must be an ISO date") from exc


def _positive_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1 or value > 1200:
        raise IDPContractError(f"{name} must be a positive bounded integer")
    return value


def add_calendar_months(start: date, months: int) -> date:
    months = _positive_integer(months, "months")
    index = start.year * 12 + start.month - 1 + months
    year, month_index = divmod(index, 12)
    month = month_index + 1
    return date(year, month, min(start.day, calendar.monthrange(year, month)[1]))


def add_calendar_years(start: date, years: int) -> date:
    years = _positive_integer(years, "years")
    year = start.year + years
    return date(year, start.month, min(start.day, calendar.monthrange(year, start.month)[1]))


def derive_anniversary(
    *,
    effective_date: object,
    duration_value: object,
    duration_unit: object,
    value_field: str = "estimated_anniversary_date",
) -> DerivedValue:
    start = _parse_date(effective_date, "effective_date")
    amount = _positive_integer(duration_value, "duration_value")
    if not isinstance(duration_unit, str):
        raise IDPContractError("duration_unit is unsupported")
    unit = duration_unit.strip().lower()
    try:
        if unit in {"month", "months", "mes", "meses"}:
            result = add_calendar_months(start, amount)
            rule_id = "ADD_CALENDAR_MONTHS_V1"
            normalized_unit = "months"
        elif unit in {"year", "years", "año", "años", "ano", "anos"}:
            result = add_calendar_years(start, amount)
            rule_id = "ADD_CALENDAR_YEARS_V1"
            normalized_unit = "years"
        else:
            raise IDPContractError("duration_unit is unsupported; business-day rules are not enabled")
    except (OverflowError, ValueError) as exc:
        raise IDPContractError("calendar rule result is outside the supported date range") from exc
    return DerivedValue(
        field=value_field,
        value=result.isoformat(),
        rule_id=rule_id,
        inputs=("effective_date", "initial_duration_value", "initial_duration_unit"),
        parameters={
            "effective_date": start.isoformat(),
            "duration_value": amount,
            "duration_unit": normalized_unit,
            "end_of_month_clamped": result.day != start.day,
        },
    )


class DerivedMetadataEngine:
    """Executes only named rules selected by backend code, never model formulas."""

    def derive(self, rule_id: str, *, effective_date: object, duration_value: object, duration_unit: object) -> DerivedValue:
        if rule_id not in {"ADD_CALENDAR_MONTHS_V1", "ADD_CALENDAR_YEARS_V1"}:
            raise IDPContractError("derived rule is not allowlisted")
        expected = "months" if rule_id == "ADD_CALENDAR_MONTHS_V1" else "years"
        unit = str(duration_unit).strip().lower() if isinstance(duration_unit, str) else duration_unit
        if expected == "months" and unit not in {"month", "months", "mes", "meses"}:
            raise IDPContractError("rule input unit does not match rule")
        if expected == "years" and unit not in {"year", "years", "año", "años", "ano", "anos"}:
            raise IDPContractError("rule input unit does not match rule")
        result = derive_anniversary(effective_date=effective_date, duration_value=duration_value, duration_unit=duration_unit)
        if result.rule_id != rule_id:
            raise IDPContractError("rule implementation mismatch")
        return result


__all__ = ["DerivedMetadataEngine", "DerivedValue", "add_calendar_months", "add_calendar_years", "derive_anniversary"]
