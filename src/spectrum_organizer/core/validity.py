from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from spectrum_organizer.core.metadata_numeric import is_finite_real_number
from spectrum_organizer.domain.models import SpectrumClass
from spectrum_organizer.core.data_columns import (
    AmbiguousDataColumnError,
    DataColumnError,
    WorksheetData,
    select_xy_pair,
)


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    reason: str | None = None
    missing_column: str | None = None
    s1_max: float | int | None = None
    s1_max_x: Any | None = None
    selected_y_max: float | int | None = None
    x_at_max_y: Any | None = None


def format_validation_rejection_reason(
    reason: str | None,
    missing_column: str | None,
) -> str:
    if reason is None:
        return "invalid data"
    if missing_column and not reason.startswith(
        ("blank in column ", "non-finite column ", "column ")
    ):
        return f"{reason}: {missing_column}"
    return reason


def validate_spectrum_data(
    spectrum_class: SpectrumClass,
    data: WorksheetData,
    steady_emission_y: str,
    s1_limit: float,
    allow_missing_s1: bool = False,
) -> ValidationResult:
    if spectrum_class == SpectrumClass.STEADY_2D:
        return ValidationResult(ok=True)

    selected_y = selected_y_for_class(
        spectrum_class,
        steady_emission_y,
    )
    s1_columns = data.matching_columns("S1")
    if len(s1_columns) > 1:
        return ValidationResult(ok=False, reason="ambiguous S1", missing_column="S1")
    selected_y_columns = data.matching_columns(selected_y)
    if (
        len(s1_columns) == 1
        and len(selected_y_columns) == 1
        and s1_columns[0] is selected_y_columns[0]
    ):
        return ValidationResult(
            ok=False,
            reason="selected Y and S1 resolve to the same physical column",
            missing_column=selected_y,
        )
    if not s1_columns:
        if not allow_missing_s1:
            return ValidationResult(ok=False, reason="missing S1", missing_column="S1")
        s1_max = None
        s1_max_x = None
    else:
        s1_column = s1_columns[0]
        if all(_is_blank(value) for value in s1_column.values):
            if not allow_missing_s1:
                return ValidationResult(
                    ok=False,
                    reason="missing S1",
                    missing_column="S1",
                )
            s1_max = None
            s1_max_x = None
        else:
            try:
                pair = select_xy_pair(data, "S1")
                s1_x_name = pair.x_column.long_name or pair.x_column.name
                s1_name = pair.y_column.long_name or pair.y_column.name
                s1_x_values, s1_values = _effective_xy_values(
                    pair.x_column.values,
                    pair.y_column.values,
                    s1_x_name,
                    s1_name,
                )
                s1_max = max(s1_values)
                maximum_x_values = tuple(
                    x
                    for x, s1_value in zip(
                        s1_x_values,
                        s1_values,
                        strict=True,
                    )
                    if s1_value == s1_max
                )
                s1_max_x = (
                    maximum_x_values[0]
                    if len(maximum_x_values) == 1
                    else maximum_x_values
                )
            except DataColumnError as exc:
                return ValidationResult(ok=False, reason=str(exc), missing_column="S1")
            except ValueError as exc:
                return ValidationResult(ok=False, reason=str(exc))
            if s1_max > s1_limit:
                return ValidationResult(ok=False, reason="S1 max exceeds limit", s1_max=s1_max, s1_max_x=s1_max_x)

    try:
        pair = select_xy_pair(data, selected_y)
    except AmbiguousDataColumnError as exc:
        return ValidationResult(
            ok=False,
            reason="ambiguous selected Y",
            missing_column=exc.missing_column or selected_y,
            s1_max=s1_max,
            s1_max_x=s1_max_x,
        )
    except DataColumnError as exc:
        reason = (
            "missing selected Y"
            if str(exc).startswith("Missing selected Y column:")
            else str(exc)
        )
        return ValidationResult(ok=False, reason=reason, missing_column=exc.missing_column or selected_y, s1_max=s1_max, s1_max_x=s1_max_x)

    try:
        x_values, y_values = _effective_xy_values(
            pair.x_column.values,
            pair.y_column.values,
            pair.x_column.long_name or pair.x_column.name,
            pair.y_column.long_name or pair.y_column.name,
        )
    except ValueError as exc:
        return ValidationResult(ok=False, reason=str(exc), s1_max=s1_max, s1_max_x=s1_max_x)

    selected_y_max = max(y_values)
    if selected_y_max <= 0:
        return ValidationResult(ok=False, reason="selected Y max <= 0", s1_max=s1_max, s1_max_x=s1_max_x, selected_y_max=selected_y_max)
    x_at_max_y = tuple(x for x, y in zip(x_values, y_values) if y == selected_y_max)
    return ValidationResult(
        ok=True,
        s1_max=s1_max,
        s1_max_x=s1_max_x,
        selected_y_max=selected_y_max,
        x_at_max_y=x_at_max_y[0] if len(x_at_max_y) == 1 else x_at_max_y,
    )


def selected_y_for_class(spectrum_class: SpectrumClass, steady_emission_y: str) -> str:
    if spectrum_class == SpectrumClass.STEADY_EMISSION:
        return steady_emission_y
    if spectrum_class == SpectrumClass.STEADY_EXCITATION:
        return "S1c/R1c"
    return "S1c"


def effective_xy_values(x_values: list[Any], y_values: list[Any]) -> tuple[list[Any], list[float | int]]:
    return _effective_xy_values(x_values, y_values)


def _effective_xy_values(
    x_values: list[Any],
    y_values: list[Any],
    x_name: str = "selected X",
    y_name: str = "selected Y",
) -> tuple[list[Any], list[float | int]]:
    if len(x_values) != len(y_values):
        raise ValueError(f"column {x_name} has {len(x_values)} rows but column {y_name} has {len(y_values)} rows")
    x_effective = list(x_values)
    y_effective = list(y_values)
    while x_effective and _is_blank(x_effective[-1]) and _is_blank(y_effective[-1]):
        x_effective.pop()
        y_effective.pop()
    if not x_effective:
        raise ValueError("missing selected X/Y")
    checked_x = []
    checked_y = []
    seen_x = set()
    for row_index, (x, y) in enumerate(zip(x_effective, y_effective), start=1):
        if _is_blank(x):
            raise ValueError(f"blank in column {x_name} at row {row_index}")
        if _is_blank(y):
            raise ValueError(f"blank in column {y_name} at row {row_index}")
        checked_value = _finite_number(x, x_name, row_index)
        if checked_value in seen_x:
            raise ValueError(
                f"duplicate value in column {x_name} at row {row_index}"
            )
        seen_x.add(checked_value)
        checked_x.append(checked_value)
        checked_y.append(_finite_number(y, y_name, row_index))
    return checked_x, checked_y


def _finite_number(value: Any, field: str, row_index: int | None = None) -> float | int:
    if not is_finite_real_number(value):
        suffix = "" if row_index is None else f" at row {row_index}"
        raise ValueError(f"non-finite column {field}{suffix}")
    return value


def _is_blank(value: Any) -> bool:
    return value is None or value == ""
