from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class InventoryBook:
    source_id: str
    folder_path: str
    short_name: str
    display_name: str
    page_order: int
    sheet_names: tuple[str, ...]
    has_note: bool
    has_data: bool
    page_type: str = "worksheet"

    @property
    def identity(self) -> tuple[str, str, str, str]:
        return (self.source_id, self.page_type, self.folder_path, self.short_name)


@dataclass(frozen=True)
class TerminalBookResult:
    source_id: str
    folder_path: str
    short_name: str
    status: str
    note_text: str | None = None
    rejection_reason: str | None = None
    display_name: str | None = None
    page_order: int | None = None
    spectrum_class: str | None = None
    data_sheet_name: str | None = None
    available_columns: tuple[str, ...] = ()
    column_metadata: tuple[tuple[str, str, str | None], ...] = ()
    selected_y_column: str | None = None
    paired_x_column: str | None = None
    selected_x_values: tuple[object, ...] = ()
    selected_y_values: tuple[object, ...] = ()
    s1_x_values: tuple[object, ...] | None = None
    s1_values: tuple[object, ...] | None = None
    selected_x_row_count: int | None = None
    selected_y_row_count: int | None = None
    max_planned_y: object | None = None
    max_planned_y_x: object | None = None
    s1_max_for_limit: object | None = None
    s1_max_for_limit_x: object | None = None
    s1_limit_status: str | None = None
    data_checksum: str | None = None
    page_type: str = "worksheet"
    payload_snapshot_path: Path | None = None
    payload_checksum: str | None = None

    @property
    def identity(self) -> tuple[str, str, str, str]:
        return (self.source_id, self.page_type, self.folder_path, self.short_name)

    @property
    def book_status(self) -> str:
        return self.status


@dataclass(frozen=True)
class ExtractionSource:
    source_id: str
    copy_path: Path
    sha256: str
    allowed_children: tuple[Path, ...]
    original_path: Path | None = None
    original_canonical_path: Path | None = None
    protected_paths: tuple[Path, ...] = ()
    size_bytes: int | None = None
    original_mtime_ns: int | None = None
