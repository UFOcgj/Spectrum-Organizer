from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class DataColumnError(ValueError):
    def __init__(self, message: str, missing_column: str | None = None):
        super().__init__(message)
        self.missing_column = missing_column


class AmbiguousDataColumnError(DataColumnError):
    pass


@dataclass(frozen=True)
class Column:
    name: str
    long_name: str
    values: list[Any]
    designation: str | None


@dataclass(frozen=True)
class WorksheetData:
    columns: list[Column]

    def column(self, requested: str) -> Column | None:
        matches = self.matching_columns(requested)
        return matches[0] if matches else None

    def matching_columns(self, requested: str) -> tuple[Column, ...]:
        requested_key = _canonical_column_name(requested)
        return tuple(
            column
            for column in self.columns
            if _matches_column_name(column, requested_key)
        )


def available_column_names(
    data: WorksheetData,
    selected_y: str | None = None,
) -> tuple[str, ...]:
    physical_s1_columns = {
        id(column)
        for column in data.matching_columns("S1")
    }
    selected_y_columns = (
        {
            id(column)
            for column in data.matching_columns(selected_y)
        }
        if selected_y is not None
        else set()
    )
    available_names = []
    for column in data.columns:
        role_names = []
        if (
            selected_y is not None
            and id(column) in selected_y_columns
        ):
            role_names.append(selected_y)
        if id(column) in physical_s1_columns:
            if "s1" not in {
                name.casefold()
                for name in role_names
            }:
                role_names.append("S1")
        available_names.extend(
            role_names
            or (column.long_name or column.name,)
        )
    return tuple(available_names)


def column_metadata(
    data: WorksheetData,
) -> tuple[tuple[str, str, str | None], ...]:
    return tuple(
        (
            column.name,
            column.long_name,
            column.designation,
        )
        for column in data.columns
    )


@dataclass(frozen=True)
class XYPair:
    x_column: Column
    y_column: Column


def select_xy_pair(data: WorksheetData, selected_y: str) -> XYPair:
    selected_key = _canonical_column_name(selected_y)
    matching_indexes = [
        index
        for index, column in enumerate(data.columns)
        if _matches_column_name(column, selected_key)
    ]
    if not matching_indexes:
        raise DataColumnError(f"Missing selected Y column: {selected_y}", missing_column=selected_y)
    if len(matching_indexes) > 1:
        raise AmbiguousDataColumnError(
            f"Ambiguous selected Y column: {selected_y}",
            missing_column=selected_y,
        )
    index = matching_indexes[0]
    if index == 0:
        raise DataColumnError(f"Selected Y has no preceding X column: {selected_y}")
    y_column = data.columns[index]
    if y_column.designation != "Y":
        raise DataColumnError(f"Selected Y column is not Y-designated: {selected_y}")
    x_column = data.columns[index - 1]
    if x_column.designation != "X":
        raise DataColumnError(
            f"Selected Y has no preceding X-designated column: {selected_y}"
        )
    return XYPair(x_column=x_column, y_column=y_column)


def _matches_column_name(column: Column, requested_key: str) -> bool:
    return (
        _canonical_column_name(column.name) == requested_key
        or _canonical_column_name(column.long_name) == requested_key
    )


def _canonical_column_name(name: str) -> str:
    compact = str(name).replace(" ", "").replace("/", "").casefold()
    if compact == "s1cr1c":
        return "s1c/r1c"
    return compact
