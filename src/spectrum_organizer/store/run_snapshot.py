from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import hashlib
import json
import math
import os
import sqlite3

from spectrum_organizer.core.metadata_numeric import is_finite_real_number
from spectrum_organizer.core.data_columns import (
    Column,
    DataColumnError,
    WorksheetData,
    available_column_names,
    select_xy_pair,
)
from spectrum_organizer.core.note_parser import NoteParseError, parse_book_note
from spectrum_organizer.core.validity import (
    effective_xy_values,
    format_validation_rejection_reason,
    selected_y_for_class,
    validate_spectrum_data,
)
from spectrum_organizer.domain.extracted import InventoryBook, TerminalBookResult
from spectrum_organizer.domain.models import SpectrumClass
from spectrum_organizer.store.sqlite_digest import sqlite_content_sha256


class SnapshotError(RuntimeError):
    pass


class ReconciliationError(SnapshotError):
    pass


class UnsupportedSourceReconciliationError(ReconciliationError):
    """The selected project contains no supported raw-spectrum Books."""


_VALID_SPECTRUM_CLASSES = {spectrum_class.value for spectrum_class in SpectrumClass}


def _frozen_path_text(path: Path) -> str:
    return os.path.normcase(os.path.abspath(str(Path(path))))


class RunSnapshot:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            _ensure_schema(connection)

    def add_source(
        self,
        source_id: str,
        copy_path: Path,
        sha256: str,
        *,
        original_path: Path | None = None,
        original_size_bytes: int | None = None,
        original_mtime_ns: int | None = None,
    ) -> None:
        provenance = (
            original_path,
            original_size_bytes,
            original_mtime_ns,
        )
        if any(value is not None for value in provenance) and any(
            value is None for value in provenance
        ):
            raise SnapshotError(
                "Original source provenance must be complete"
            )
        canonical_original_path = (
            None
            if original_path is None
            else _frozen_path_text(original_path)
        )
        with self._connect() as connection:
            connection.execute(
                """
                insert into source_files (
                    source_id,
                    copy_path,
                    sha256,
                    original_path,
                    original_size_bytes,
                    original_mtime_ns
                )
                values (?, ?, ?, ?, ?, ?)
                on conflict(source_id) do update set
                    copy_path = excluded.copy_path,
                    sha256 = excluded.sha256,
                    original_path = coalesce(
                        source_files.original_path,
                        excluded.original_path
                    ),
                    original_size_bytes = coalesce(
                        source_files.original_size_bytes,
                        excluded.original_size_bytes
                    ),
                    original_mtime_ns = coalesce(
                        source_files.original_mtime_ns,
                        excluded.original_mtime_ns
                    )
                """,
                (
                    source_id,
                    str(copy_path),
                    sha256,
                    canonical_original_path,
                    original_size_bytes,
                    original_mtime_ns,
                ),
            )

    def bind_original_provenance(
        self,
        source_id: str,
        copy_path: Path,
        sha256: str,
        *,
        original_path: Path,
        original_size_bytes: int,
        original_mtime_ns: int,
    ) -> None:
        canonical_original_path = _frozen_path_text(original_path)
        with self._connect() as connection:
            row = connection.execute(
                """
                select copy_path, sha256, original_path,
                       original_size_bytes, original_mtime_ns
                from source_files
                where source_id = ?
                """,
                (source_id,),
            ).fetchone()
            if row is None:
                raise SnapshotError(
                    f"Cannot bind provenance for unknown source {source_id}"
                )
            if (row[0], row[1]) != (str(copy_path), sha256):
                raise SnapshotError(
                    f"Cannot bind provenance to changed source {source_id}"
                )
            if any(value is not None for value in row[2:]):
                raise SnapshotError(
                    f"Source provenance was already present for {source_id}"
                )
            connection.execute(
                """
                update source_files
                set original_path = ?,
                    original_size_bytes = ?,
                    original_mtime_ns = ?
                where source_id = ?
                """,
                (
                    canonical_original_path,
                    original_size_bytes,
                    original_mtime_ns,
                    source_id,
                ),
            )

    def update_source_copy_path(self, source_id: str, copy_path: Path) -> None:
        with self._connect() as connection:
            connection.execute(
                "update source_files set copy_path = ? where source_id = ?",
                (str(copy_path), source_id),
            )

    def source_copy_path(self, source_id: str) -> Path:
        with self._connect() as connection:
            row = connection.execute(
                "select copy_path from source_files where source_id = ?",
                (source_id,),
            ).fetchone()
        if row is None:
            raise SnapshotError(f"Unknown source {source_id}")
        return Path(row[0])

    def add_worker_attempt(self, source_id: str, attempt: int, status: str, message: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "insert into worker_attempts (source_id, attempt, status, message) values (?, ?, ?, ?)",
                (source_id, attempt, status, message),
            )

    def discard_source_partition(self, source_id: str) -> None:
        with self._connect() as connection:
            _discard_source_partition(connection, source_id)

    def discard_source(self, source_id: str) -> None:
        with self._connect() as connection:
            _discard_source_partition(connection, source_id)
            connection.execute(
                "delete from worker_attempts where source_id = ?",
                (source_id,),
            )
            connection.execute(
                "delete from source_files where source_id = ?",
                (source_id,),
            )

    def replace_source_partition(
        self,
        source_id: str,
        books: list[InventoryBook],
        results: list[TerminalBookResult],
    ) -> None:
        try:
            with self._connect() as connection:
                _discard_source_partition(connection, source_id)
                _record_inventory(connection, source_id, books)
                books_by_identity = {book.identity: book for book in books}
                for result in results:
                    try:
                        pass_two_book = books_by_identity[result.identity]
                    except KeyError as exc:
                        raise SnapshotError(f"Terminal result has no matching inventory Book: {result.identity}") from exc
                    _record_book_result(connection, result, pass_two_book)
                _reconcile_source(connection, source_id)
        except sqlite3.IntegrityError as exc:
            self.discard_source_partition(source_id)
            raise SnapshotError(f"Duplicate inventory or terminal identity for source {source_id}") from exc
        except Exception:
            self.discard_source_partition(source_id)
            raise

    def record_inventory(self, source_id: str, books: list[InventoryBook]) -> None:
        try:
            with self._connect() as connection:
                _record_inventory(connection, source_id, books)
        except sqlite3.IntegrityError as exc:
            raise SnapshotError(f"Duplicate inventory identity for source {source_id}") from exc

    def record_inventory_book(self, source_id: str, book: InventoryBook) -> None:
        self.record_inventory(source_id, [book])

    def record_book_result(self, result: TerminalBookResult, pass_two_book: InventoryBook) -> None:
        try:
            with self._connect() as connection:
                _record_book_result(connection, result, pass_two_book)
        except sqlite3.IntegrityError as exc:
            raise SnapshotError(f"Duplicate terminal result for {result.identity}") from exc

    def record_book_transaction(self, source_id: str, book: InventoryBook, result: TerminalBookResult) -> None:
        try:
            with self._connect() as connection:
                _record_book_transaction(connection, source_id, book, result)
        except sqlite3.IntegrityError as exc:
            raise SnapshotError(f"Duplicate book transaction for {result.identity}") from exc

    def reconcile_source(
        self,
        source_id: str,
        *,
        s1_limit: int | float | None = None,
        steady_emission_y: str | None = None,
        allow_missing_s1: bool = False,
    ) -> None:
        with self._connect() as connection:
            _reconcile_source(
                connection,
                source_id,
                s1_limit=s1_limit,
                steady_emission_y=steady_emission_y,
                allow_missing_s1=allow_missing_s1,
            )

    def inventory_count(self, source_id: str) -> int:
        return self._count("inventory_rows", source_id)

    def result_count(self, source_id: str) -> int:
        return self._count("book_results", source_id)

    def status_count(self, source_id: str, status: str) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "select count(*) from book_results where source_id = ? and status = ?",
                (source_id, status),
            ).fetchone()
        return int(row[0])

    def inventory_rows(self, source_id: str) -> list[InventoryBook]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                select source_id, page_type, folder_path, short_name, display_name, page_order,
                       sheet_names_json, has_note, has_data
                from inventory_rows
                where source_id = ?
                order by page_order, folder_path, short_name, page_type
                """,
                (source_id,),
            ).fetchall()
        return [
            InventoryBook(
                source_id=row[0],
                page_type=row[1],
                folder_path=row[2],
                short_name=row[3],
                display_name=row[4],
                page_order=int(row[5]),
                sheet_names=tuple(json.loads(row[6])),
                has_note=bool(row[7]),
                has_data=bool(row[8]),
            )
            for row in rows
        ]

    def book_results(self, source_id: str) -> list[TerminalBookResult]:
        with self._connect() as connection:
            rows = connection.execute(_BOOK_RESULTS_QUERY, (source_id,)).fetchall()
        return _terminal_results_from_rows(rows)

    def _count(self, table: str, source_id: str) -> int:
        with self._connect() as connection:
            row = connection.execute(f"select count(*) from {table} where source_id = ?", (source_id,)).fetchone()
        return int(row[0])

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path)
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


def load_book_results_read_only(
    path: Path,
    *,
    expected_snapshot_sha256: str,
    source_ids: tuple[str, ...] | None = None,
    cancel_check=None,
    s1_limit: int | float | None = None,
    steady_emission_y: str | None = None,
    allow_missing_s1: bool = False,
):
    """Validate and stream candidate metadata from one immutable read transaction."""
    resolved = Path(path).resolve()
    if (
        len(expected_snapshot_sha256) != 64
        or any(character not in "0123456789abcdef" for character in expected_snapshot_sha256.lower())
    ):
        raise ReconciliationError("Invalid approved snapshot SHA-256")
    expected_snapshot_sha256 = expected_snapshot_sha256.lower()
    connection = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True)
    try:
        connection.execute("begin")
        if _snapshot_content_sha256(connection, cancel_check=cancel_check) != expected_snapshot_sha256:
            raise ReconciliationError("Task snapshot does not match the approved snapshot SHA-256")
        if source_ids is not None:
            for source_id in source_ids:
                if cancel_check is not None:
                    cancel_check()
                _validate_source_reconciliation(
                    connection,
                    source_id,
                    cancel_check=cancel_check,
                    s1_limit=s1_limit,
                    steady_emission_y=steady_emission_y,
                    allow_missing_s1=allow_missing_s1,
                )
        cursor = connection.execute(_ALL_BOOK_RESULTS_METADATA_QUERY)
        for row in cursor:
            if cancel_check is not None:
                cancel_check()
            yield _terminal_result_from_metadata_row(row, payload_snapshot_path=resolved)
    finally:
        connection.close()
    if snapshot_approval_sha256(resolved, cancel_check=cancel_check) != expected_snapshot_sha256:
        raise ReconciliationError("Task snapshot changed during approved snapshot metadata read")


def snapshot_approval_sha256(path: Path, *, cancel_check=None) -> str:
    resolved = Path(path).resolve()
    try:
        connection = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise ReconciliationError(f"Could not open task snapshot for approval: {exc}") from exc
    try:
        connection.execute("begin")
        return _snapshot_content_sha256(connection, cancel_check=cancel_check)
    except sqlite3.Error as exc:
        raise ReconciliationError(f"Could not approve task snapshot contents: {exc}") from exc
    finally:
        connection.close()


def _snapshot_content_sha256(connection: sqlite3.Connection, *, cancel_check=None) -> str:
    return sqlite_content_sha256(connection, cancel_check=cancel_check)


_BOOK_RESULTS_QUERY = """
    select source_id, page_type, folder_path, short_name, status, note_text, rejection_reason,
           display_name, page_order, spectrum_class, data_sheet_name, available_columns_json,
           selected_y_column, paired_x_column, selected_x_values_json,
           selected_y_values_json, s1_x_values_json, s1_values_json,
           selected_x_row_count, selected_y_row_count,
           max_planned_y_json, max_planned_y_x_json,
           s1_max_for_limit_json, s1_max_for_limit_x_json, s1_limit_status, data_checksum,
           column_metadata_json, payload_checksum
    from book_results
    where source_id = ?
    order by folder_path, short_name, page_type
"""


_ALL_BOOK_RESULTS_METADATA_QUERY = """
    select source_id, page_type, folder_path, short_name, status, note_text, rejection_reason,
           display_name, page_order, spectrum_class, data_sheet_name, available_columns_json,
           selected_y_column, paired_x_column, selected_x_row_count, selected_y_row_count,
           max_planned_y_json, max_planned_y_x_json,
           s1_max_for_limit_json, s1_max_for_limit_x_json, s1_limit_status, data_checksum,
           column_metadata_json, payload_checksum
    from book_results
    order by source_id, folder_path, short_name, page_type
"""


def _terminal_result_from_metadata_row(
    row,
    *,
    payload_snapshot_path: Path,
) -> TerminalBookResult:
    return TerminalBookResult(
        source_id=row[0],
        page_type=row[1],
        folder_path=row[2],
        short_name=row[3],
        status=row[4],
        note_text=row[5],
        rejection_reason=row[6],
        display_name=row[7],
        page_order=row[8],
        spectrum_class=row[9],
        data_sheet_name=row[10],
        available_columns=tuple(json.loads(row[11] or "[]")),
        selected_y_column=row[12],
        paired_x_column=row[13],
        selected_x_values=(),
        selected_y_values=(),
        selected_x_row_count=row[14],
        selected_y_row_count=row[15],
        max_planned_y=_json_value(row[16]),
        max_planned_y_x=_json_value(row[17]),
        s1_max_for_limit=_json_value(row[18]),
        s1_max_for_limit_x=_json_value(row[19]),
        s1_limit_status=row[20],
        data_checksum=row[21],
        column_metadata=_column_metadata_value(row[22]),
        payload_snapshot_path=payload_snapshot_path,
        payload_checksum=row[23],
    )


def _terminal_results_from_rows(rows) -> list[TerminalBookResult]:
    return [_terminal_result_from_row(row) for row in rows]


def _terminal_result_from_row(
    row,
    *,
    payload_snapshot_path: Path | None = None,
    load_arrays: bool = True,
) -> TerminalBookResult:
    return TerminalBookResult(
        source_id=row[0],
        page_type=row[1],
        folder_path=row[2],
        short_name=row[3],
        status=row[4],
        note_text=row[5],
        rejection_reason=row[6],
        display_name=row[7],
        page_order=row[8],
        spectrum_class=row[9],
        data_sheet_name=row[10],
        available_columns=tuple(json.loads(row[11] or "[]")),
        selected_y_column=row[12],
        paired_x_column=row[13],
        selected_x_values=tuple(json.loads(row[14] or "[]")) if load_arrays else (),
        selected_y_values=tuple(json.loads(row[15] or "[]")) if load_arrays else (),
        s1_x_values=None if row[16] is None else tuple(json.loads(row[16])),
        s1_values=None if row[17] is None else tuple(json.loads(row[17])),
        selected_x_row_count=row[18],
        selected_y_row_count=row[19],
        max_planned_y=_json_value(row[20]),
        max_planned_y_x=_json_value(row[21]),
        s1_max_for_limit=_json_value(row[22]),
        s1_max_for_limit_x=_json_value(row[23]),
        s1_limit_status=row[24],
        data_checksum=row[25],
        column_metadata=_column_metadata_value(row[26]),
        payload_snapshot_path=payload_snapshot_path,
        payload_checksum=row[27],
    )


_BOOK_PAYLOAD_QUERY = """
    select status, rejection_reason, data_checksum, note_text, data_sheet_name, spectrum_class,
           available_columns_json, selected_y_column, paired_x_column,
           selected_x_values_json, selected_y_values_json, s1_x_values_json, s1_values_json,
           selected_x_row_count,
           selected_y_row_count, max_planned_y_json, max_planned_y_x_json,
           s1_max_for_limit_json, s1_max_for_limit_x_json, s1_limit_status,
           column_metadata_json, payload_checksum
    from book_results
    where source_id = ? and page_type = ? and folder_path = ? and short_name = ?
"""


def load_book_payload_read_only(
    path: Path,
    *,
    source_id: str,
    page_type: str,
    folder_path: str,
    short_name: str,
    expected_payload_checksum: str,
) -> tuple[tuple[object, ...], tuple[object, ...]]:
    resolved = Path(path).resolve()
    connection = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True)
    try:
        row = connection.execute(
            _BOOK_PAYLOAD_QUERY,
            (source_id, page_type, folder_path, short_name),
        ).fetchone()
    finally:
        connection.close()
    return _validated_book_payload(
        row,
        source_id=source_id,
        page_type=page_type,
        folder_path=folder_path,
        short_name=short_name,
        expected_payload_checksum=expected_payload_checksum,
    )


def load_book_payloads_read_only(
    path: Path,
    *,
    expected_snapshot_sha256: str,
    requests: tuple[tuple[str, str, str, str, str], ...],
    cancel_check=None,
) -> tuple[tuple[tuple[object, ...], tuple[object, ...]], ...]:
    if (
        len(expected_snapshot_sha256) != 64
        or any(
            character not in "0123456789abcdef"
            for character in expected_snapshot_sha256.lower()
        )
    ):
        raise ReconciliationError("Invalid approved snapshot SHA-256")
    expected_snapshot_sha256 = expected_snapshot_sha256.lower()
    resolved = Path(path).resolve()
    connection = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True)
    payloads = []
    seen: set[tuple[str, str, str, str]] = set()
    try:
        connection.execute("begin")
        if (
            _snapshot_content_sha256(
                connection,
                cancel_check=cancel_check,
            )
            != expected_snapshot_sha256
        ):
            raise ReconciliationError(
                "Task snapshot does not match the approved snapshot SHA-256"
            )
        for source_id, page_type, folder_path, short_name, checksum in requests:
            if cancel_check is not None:
                cancel_check()
            identity = (source_id, page_type, folder_path, short_name)
            if identity in seen:
                raise ReconciliationError(
                    f"Duplicate reviewed Book payload request: {identity}"
                )
            seen.add(identity)
            row = connection.execute(
                _BOOK_PAYLOAD_QUERY,
                identity,
            ).fetchone()
            payloads.append(
                _validated_book_payload(
                    row,
                    source_id=source_id,
                    page_type=page_type,
                    folder_path=folder_path,
                    short_name=short_name,
                    expected_payload_checksum=checksum,
                )
            )
    finally:
        connection.close()
    if (
        snapshot_approval_sha256(
            resolved,
            cancel_check=cancel_check,
        )
        != expected_snapshot_sha256
    ):
        raise ReconciliationError(
            "Task snapshot changed during approved Book payload read"
        )
    return tuple(payloads)


def _validated_book_payload(
    row,
    *,
    source_id: str,
    page_type: str,
    folder_path: str,
    short_name: str,
    expected_payload_checksum: str,
) -> tuple[tuple[object, ...], tuple[object, ...]]:
    if row is None:
        raise SnapshotError(f"Unknown candidate payload: {(source_id, page_type, folder_path, short_name)}")
    _validate_payload_checksum(source_id, page_type, folder_path, short_name, row)
    if row[-1] != expected_payload_checksum:
        raise ReconciliationError(
            f"Book payload changed after the reviewed payload version for source {source_id}"
        )
    return tuple(json.loads(row[9] or "[]")), tuple(json.loads(row[10] or "[]"))


def validate_reconciled_sources(
    path: Path,
    source_ids: tuple[str, ...],
    *,
    cancel_check=None,
    s1_limit: int | float | None = None,
    steady_emission_y: str | None = None,
    allow_missing_s1: bool = False,
) -> None:
    if s1_limit is not None and (
        isinstance(s1_limit, bool)
        or not isinstance(s1_limit, (int, float))
        or not is_finite_real_number(s1_limit)
        or s1_limit <= 0
    ):
        raise ReconciliationError("Confirmed S1 limit is invalid")
    resolved = Path(path).resolve()
    connection = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True)
    try:
        for source_id in source_ids:
            if cancel_check is not None:
                cancel_check()
            _validate_source_reconciliation(
                connection,
                source_id,
                cancel_check=cancel_check,
                s1_limit=s1_limit,
                steady_emission_y=steady_emission_y,
                allow_missing_s1=allow_missing_s1,
            )
    finally:
        connection.close()


def _discard_source_partition(connection: sqlite3.Connection, source_id: str) -> None:
    connection.execute("delete from reconciliation_results where source_id = ?", (source_id,))
    connection.execute("delete from book_results where source_id = ?", (source_id,))
    connection.execute("delete from inventory_rows where source_id = ?", (source_id,))


def _record_inventory(connection: sqlite3.Connection, source_id: str, books: list[InventoryBook]) -> None:
    for book in books:
        if book.source_id != source_id:
            raise SnapshotError("Inventory book source_id mismatch")
        connection.execute(
            """
            insert into inventory_rows (
                source_id, page_type, folder_path, short_name, display_name, page_order,
                sheet_names_json, has_note, has_data
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                book.source_id,
                book.page_type,
                book.folder_path,
                book.short_name,
                book.display_name,
                book.page_order,
                json.dumps(list(book.sheet_names), ensure_ascii=False),
                int(book.has_note),
                int(book.has_data),
            ),
        )


def _record_book_transaction(
    connection: sqlite3.Connection,
    source_id: str,
    book: InventoryBook,
    result: TerminalBookResult,
) -> None:
    if book.source_id != source_id or result.source_id != source_id:
        raise SnapshotError("Book transaction source_id mismatch")
    if book.identity != result.identity:
        raise SnapshotError("Book transaction identity mismatch")
    _record_inventory(connection, source_id, [book])
    _record_book_result(connection, result, book)


def _record_book_result(
    connection: sqlite3.Connection,
    result: TerminalBookResult,
    pass_two_book: InventoryBook,
) -> None:
    if pass_two_book.identity != result.identity:
        raise SnapshotError("Pass-two inventory/result identity mismatch")
    pass_two_sheet_names_json = json.dumps(list(pass_two_book.sheet_names), ensure_ascii=False)
    available_columns_json = json.dumps(list(result.available_columns), ensure_ascii=False)
    column_metadata_json = json.dumps(
        [
            list(column)
            for column in result.column_metadata
        ],
        ensure_ascii=False,
    )
    selected_x_values_json = json.dumps(list(result.selected_x_values), ensure_ascii=False, default=str)
    selected_y_values_json = json.dumps(list(result.selected_y_values), ensure_ascii=False, default=str)
    s1_x_values_json = (
        None
        if result.s1_x_values is None
        else json.dumps(list(result.s1_x_values), ensure_ascii=False, default=str)
    )
    s1_values_json = (
        None
        if result.s1_values is None
        else json.dumps(list(result.s1_values), ensure_ascii=False, default=str)
    )
    max_planned_y_json = _json_dump(result.max_planned_y)
    max_planned_y_x_json = _json_dump(result.max_planned_y_x)
    s1_max_for_limit_json = _json_dump(result.s1_max_for_limit)
    s1_max_for_limit_x_json = _json_dump(result.s1_max_for_limit_x)
    payload_checksum = _payload_checksum(
        result.source_id,
        result.page_type,
        result.folder_path,
        result.short_name,
        result.status,
        result.rejection_reason,
        result.data_checksum,
        result.note_text,
        result.data_sheet_name,
        result.spectrum_class,
        available_columns_json,
        result.selected_y_column,
        result.paired_x_column,
        selected_x_values_json,
        selected_y_values_json,
        s1_x_values_json,
        s1_values_json,
        result.selected_x_row_count,
        result.selected_y_row_count,
        max_planned_y_json,
        max_planned_y_x_json,
        s1_max_for_limit_json,
        s1_max_for_limit_x_json,
        result.s1_limit_status,
        column_metadata_json,
    )
    connection.execute(
        """
        insert into book_results (
            source_id, page_type, folder_path, short_name, status, note_text, rejection_reason,
            display_name, page_order, spectrum_class, data_sheet_name, available_columns_json,
            column_metadata_json,
            selected_y_column, paired_x_column, selected_x_values_json,
            selected_y_values_json, s1_x_values_json, s1_values_json,
            max_planned_y_json, max_planned_y_x_json,
            selected_x_row_count, selected_y_row_count, s1_max_for_limit_json, s1_max_for_limit_x_json,
            s1_limit_status, data_checksum, payload_checksum,
            pass_two_sheet_names_json, pass_two_has_note, pass_two_has_data
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            result.source_id,
            result.page_type,
            result.folder_path,
            result.short_name,
            result.status,
            result.note_text,
            result.rejection_reason,
            result.display_name,
            result.page_order,
            result.spectrum_class,
            result.data_sheet_name,
            available_columns_json,
            column_metadata_json,
            result.selected_y_column,
            result.paired_x_column,
            selected_x_values_json,
            selected_y_values_json,
            s1_x_values_json,
            s1_values_json,
            max_planned_y_json,
            max_planned_y_x_json,
            result.selected_x_row_count,
            result.selected_y_row_count,
            s1_max_for_limit_json,
            s1_max_for_limit_x_json,
            result.s1_limit_status,
            result.data_checksum,
            payload_checksum,
            pass_two_sheet_names_json,
            int(pass_two_book.has_note),
            int(pass_two_book.has_data),
        ),
    )


def _json_dump(value: object | None) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, default=str)


def _json_value(value: str | None) -> object | None:
    if value is None:
        return None
    return json.loads(value)


def _reconcile_source(
    connection: sqlite3.Connection,
    source_id: str,
    *,
    s1_limit: int | float | None = None,
    steady_emission_y: str | None = None,
    allow_missing_s1: bool = False,
) -> None:
    _validate_source_reconciliation(
        connection,
        source_id,
        s1_limit=s1_limit,
        steady_emission_y=steady_emission_y,
        allow_missing_s1=allow_missing_s1,
    )
    connection.execute(
        "insert into reconciliation_results (source_id, status, message) values (?, ?, ?)",
        (source_id, "ok", ""),
    )


def _validate_source_reconciliation(
    connection: sqlite3.Connection,
    source_id: str,
    *,
    cancel_check=None,
    s1_limit: int | float | None = None,
    steady_emission_y: str | None = None,
    allow_missing_s1: bool = False,
) -> None:
    inventory = _identity_set(connection, "inventory_rows", source_id, cancel_check=cancel_check)
    results = _identity_set(connection, "book_results", source_id, cancel_check=cancel_check)
    if not inventory:
        raise UnsupportedSourceReconciliationError(
            f"Source has zero recognizable Books: {source_id}"
        )
    missing = inventory - results
    extra = results - inventory
    if missing or extra:
        raise ReconciliationError(f"Source terminal results do not reconcile: {source_id}")
    if _has_stable_identity_divergence(connection, source_id, cancel_check=cancel_check):
        raise ReconciliationError(f"Source terminal metadata does not reconcile: {source_id}")
    _validate_terminal_payloads(
        connection,
        source_id,
        cancel_check=cancel_check,
        s1_limit=s1_limit,
        steady_emission_y=steady_emission_y,
        allow_missing_s1=allow_missing_s1,
    )
    if not _has_recognized_supported_book(connection, source_id, cancel_check=cancel_check):
        raise UnsupportedSourceReconciliationError(
            f"Source has zero recognizable supported raw-spectrum Books: {source_id}"
        )


def _has_recognized_supported_book(
    connection: sqlite3.Connection,
    source_id: str,
    *,
    cancel_check=None,
) -> bool:
    rows = connection.execute(
        """
        select result.spectrum_class, result.note_text, inventory.sheet_names_json,
               inventory.has_note, inventory.has_data
        from book_results as result
        join inventory_rows as inventory
          using (source_id, page_type, folder_path, short_name)
        where result.source_id = ?
          and inventory.has_note = 1
          and inventory.has_data = 1
          and result.note_text is not null
          and trim(result.note_text) <> ''
        """,
        (source_id,),
    )
    for row in rows:
        if cancel_check is not None:
            cancel_check()
        if (
            row[0] in _VALID_SPECTRUM_CLASSES
            and _parsed_note_spectrum_class(row[1]) == row[0]
            and _sheet_flags_match(row[2], row[3], row[4])
            and bool(row[3])
            and bool(row[4])
        ):
            return True
    return False


def _validate_terminal_payloads(
    connection: sqlite3.Connection,
    source_id: str,
    *,
    cancel_check=None,
    s1_limit: int | float | None = None,
    steady_emission_y: str | None = None,
    allow_missing_s1: bool = False,
) -> None:
    rows = connection.execute(
        """
        select result.status, result.rejection_reason, result.data_checksum,
               result.note_text, result.data_sheet_name, result.spectrum_class,
               result.available_columns_json, result.selected_y_column,
               result.paired_x_column, result.selected_x_values_json,
               result.selected_y_values_json, result.s1_x_values_json,
               result.s1_values_json, result.selected_x_row_count,
               result.selected_y_row_count, result.max_planned_y_json,
               result.max_planned_y_x_json, result.s1_max_for_limit_json,
               result.s1_max_for_limit_x_json, result.s1_limit_status,
               result.column_metadata_json, result.payload_checksum,
               result.page_type, result.folder_path, result.short_name,
               inventory.sheet_names_json
        from book_results as result
        join inventory_rows as inventory
          using (source_id, page_type, folder_path, short_name)
        where result.source_id = ?
        """,
        (source_id,),
    )
    for row in rows:
        if cancel_check is not None:
            cancel_check()
        payload_row = row[:22]
        status = payload_row[0]
        rejection_reason = payload_row[1]
        if status not in {"extracted", "rejected"}:
            raise ReconciliationError(f"Invalid terminal status for source {source_id}")
        if status == "extracted" and row[22] != "worksheet":
            raise ReconciliationError(f"Unsupported extracted page type for source {source_id}: {row[22]}")
        _validate_optional_pair_shape(source_id, payload_row)
        _validate_payload_checksum(source_id, row[22], row[23], row[24], payload_row)
        _validate_present_numeric_fields(source_id, payload_row)
        if payload_row[5] and _parsed_note_spectrum_class(payload_row[3]) != payload_row[5]:
            raise ReconciliationError(f"Note spectrum class does not match payload for source {source_id}")
        if payload_row[5] == "steady_2d":
            _validate_steady_2d_ordinary_fields(source_id, payload_row)
        if status == "rejected":
            if not rejection_reason:
                raise ReconciliationError(f"Rejected Book is missing reason for source {source_id}")
            _validate_rejected_payload(
                source_id,
                payload_row,
                s1_limit=s1_limit,
                steady_emission_y=steady_emission_y,
                allow_missing_s1=allow_missing_s1,
            )
            _validate_column_metadata(source_id, payload_row)
            _validate_rejection_claim(
                source_id,
                payload_row,
                page_type=row[22],
                sheet_names_json=row[25],
                s1_limit=s1_limit,
                steady_emission_y=steady_emission_y,
                allow_missing_s1=allow_missing_s1,
            )
            continue
        _validate_extracted_payload(
            source_id,
            payload_row,
            s1_limit=s1_limit,
            steady_emission_y=steady_emission_y,
            allow_missing_s1=allow_missing_s1,
        )
        _validate_column_metadata(source_id, payload_row)


def _validate_extracted_payload(
    source_id: str,
    row: tuple,
    *,
    s1_limit: int | float | None = None,
    steady_emission_y: str | None = None,
    allow_missing_s1: bool = False,
) -> None:
    (
        _status,
        _rejection_reason,
        data_checksum,
        note_text,
        data_sheet_name,
        spectrum_class,
        available_columns_json,
        selected_y_column,
        paired_x_column,
        selected_x_values_json,
        selected_y_values_json,
        s1_x_values_json,
        s1_values_json,
        selected_x_row_count,
        selected_y_row_count,
        max_planned_y_json,
        max_planned_y_x_json,
        s1_max_for_limit_json,
        s1_max_for_limit_x_json,
        s1_limit_status,
        _column_metadata_json,
        payload_checksum,
    ) = row
    common_required = (data_checksum, note_text, data_sheet_name, spectrum_class)
    if any(not value for value in common_required) or not _json_array_nonempty(available_columns_json):
        raise ReconciliationError(f"Incomplete extracted payload for source {source_id}")
    if spectrum_class not in _VALID_SPECTRUM_CLASSES:
        raise ReconciliationError(f"Invalid spectrum class for source {source_id}")
    if s1_limit_status == "not_applicable":
        if spectrum_class != "steady_2d":
            raise ReconciliationError(f"2D payload spectrum class mismatch for source {source_id}")
        return
    if spectrum_class == "steady_2d":
        raise ReconciliationError(f"2D payload spectrum class mismatch for source {source_id}")
    if s1_limit_status not in {"ok", "missing_allowed"}:
        raise ReconciliationError(f"Invalid extracted S1 limit status for source {source_id}")
    ordinary_required = (
        selected_y_column,
        paired_x_column,
        selected_x_values_json,
        selected_y_values_json,
        max_planned_y_json,
        max_planned_y_x_json,
        s1_limit_status,
    )
    if any(value is None or value == "" for value in ordinary_required):
        raise ReconciliationError(f"Incomplete extracted payload for source {source_id}")
    try:
        available_columns = json.loads(available_columns_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ReconciliationError(f"Invalid available columns for source {source_id}") from exc
    s1_column_count = _column_count("S1", available_columns)
    if s1_column_count > 1:
        raise ReconciliationError(f"Ambiguous S1 columns for source {source_id}")
    s1_values, s1_x_values = _validated_s1_evidence(
        source_id,
        s1_values_json=s1_values_json,
        s1_x_values_json=s1_x_values_json,
    )
    if s1_limit_status == "missing_allowed":
        if not allow_missing_s1:
            raise ReconciliationError(f"Missing S1 was not approved for source {source_id}")
        if s1_max_for_limit_json is not None or s1_max_for_limit_x_json is not None:
            raise ReconciliationError(f"Missing-S1 payload contains S1 measurements for source {source_id}")
        if s1_column_count == 0 and s1_values is not None:
            raise ReconciliationError(f"Missing-S1 payload contains unlisted S1 evidence for source {source_id}")
        if s1_column_count == 1 and s1_values is None:
            raise ReconciliationError(f"Missing-S1 payload lacks raw S1 evidence for source {source_id}")
        if s1_values:
            raise ReconciliationError(
                f"Missing-S1 payload contains nonblank raw S1 evidence for source {source_id}"
            )
        s1_max_for_limit = None
    else:
        if s1_column_count != 1:
            raise ReconciliationError(f"Extracted payload lacks a unique S1 column for source {source_id}")
        if not s1_values:
            raise ReconciliationError(f"Extracted payload lacks nonblank raw S1 evidence for source {source_id}")
        if s1_x_values is None:
            raise ReconciliationError(f"Extracted payload lacks paired S1 X evidence for source {source_id}")
        if s1_max_for_limit_json is None or s1_max_for_limit_json == "":
            raise ReconciliationError(f"Extracted payload lacks S1 maximum for source {source_id}")
        s1_max_for_limit = _finite_numeric_value(s1_max_for_limit_json, source_id)
        measured_s1_max = max(s1_values)
        measured_s1_max_x = _s1_x_at_maximum(
            source_id,
            s1_values,
            s1_x_values,
            measured_s1_max,
        )
        reported_s1_max_x = (
            None
            if s1_max_for_limit_x_json is None
            else _finite_numeric_value_or_ties(
                s1_max_for_limit_x_json,
                source_id,
            )
        )
        if s1_max_for_limit != measured_s1_max or reported_s1_max_x != measured_s1_max_x:
            raise ReconciliationError(f"S1 maximum semantics do not match raw evidence for source {source_id}")
    if not _column_is_available(selected_y_column, available_columns) or not _column_is_available(
        paired_x_column,
        available_columns,
    ):
        raise ReconciliationError(f"Selected columns are not present in available columns for source {source_id}")
    _validate_required_selected_columns(
        source_id,
        note_text,
        available_columns,
        selected_y_column,
        paired_x_column,
        steady_emission_y,
    )
    if not _json_array_nonempty(selected_x_values_json) or not _json_array_nonempty(selected_y_values_json):
        raise ReconciliationError(f"Incomplete extracted payload for source {source_id}")
    if (
        not isinstance(selected_x_row_count, int)
        or not isinstance(selected_y_row_count, int)
        or selected_x_row_count != _json_array_length(selected_x_values_json)
        or selected_y_row_count != _json_array_length(selected_y_values_json)
        or selected_x_row_count != selected_y_row_count
    ):
        raise ReconciliationError(f"Selected row counts do not match extracted payload for source {source_id}")
    x_values = _finite_numeric_array(selected_x_values_json, source_id)
    y_values = _finite_numeric_array(selected_y_values_json, source_id)
    _require_unique_x_values(
        x_values,
        source_id,
        "selected",
    )
    max_planned_y = _finite_numeric_value(max_planned_y_json, source_id)
    max_planned_y_x = _finite_numeric_value_or_ties(max_planned_y_x_json, source_id)
    if s1_limit is not None and s1_max_for_limit is not None and s1_max_for_limit > s1_limit:
        raise ReconciliationError(f"Extracted payload exceeds confirmed S1 limit for source {source_id}")
    measured_max = max(y_values)
    if measured_max <= 0:
        raise ReconciliationError(f"Selected Y maximum makes normalization invalid for source {source_id}")
    expected_max_x = _x_values_at_maximum(x_values, y_values, measured_max)
    if max_planned_y != measured_max or max_planned_y_x != expected_max_x:
        raise ReconciliationError(f"Selected Y maximum semantics do not match extracted payload for source {source_id}")


def _validate_rejected_payload(
    source_id: str,
    row: tuple,
    *,
    s1_limit: int | float | None,
    steady_emission_y: str | None,
    allow_missing_s1: bool,
) -> None:
    rejection_reason = row[1]
    note_text = row[3]
    available_columns_json = row[6]
    selected_y_column = row[7]
    paired_x_column = row[8]
    selected_x_values_json = row[9]
    selected_y_values_json = row[10]
    s1_x_values_json = row[11]
    s1_values_json = row[12]
    max_planned_y_json = row[15]
    max_planned_y_x_json = row[16]
    s1_max_for_limit_json = row[17]
    s1_max_for_limit_x_json = row[18]
    s1_limit_status = row[19]
    if (
        allow_missing_s1
        and s1_limit_status == "failed"
        and str(rejection_reason or "").casefold().startswith("missing s1")
    ):
        raise ReconciliationError(
            f"Extraction worker rejected approved missing S1 data for source {source_id}"
        )
    if s1_limit_status not in {None, "failed", "exceeds_limit"}:
        raise ReconciliationError(f"Invalid rejected S1 limit status for source {source_id}")
    s1_max = (
        None
        if s1_max_for_limit_json is None
        else _finite_numeric_value(s1_max_for_limit_json, source_id)
    )
    if s1_max is not None:
        try:
            available_columns = json.loads(available_columns_json)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ReconciliationError(f"Invalid available columns for source {source_id}") from exc
        if not isinstance(available_columns, list) or _column_count("S1", available_columns) != 1:
            raise ReconciliationError(f"Rejected payload lacks a unique listed S1 column for source {source_id}")
        s1_values, s1_x_values = _validated_s1_evidence(
            source_id,
            s1_values_json=s1_values_json,
            s1_x_values_json=s1_x_values_json,
        )
        if not s1_values:
            raise ReconciliationError(f"Rejected payload lacks raw S1 evidence for source {source_id}")
        if s1_limit_status == "exceeds_limit" and s1_x_values is None:
            raise ReconciliationError(
                f"Rejected over-limit payload lacks paired S1 X evidence for source {source_id}"
            )
        measured_s1_max = max(s1_values)
        measured_s1_max_x = _s1_x_at_maximum(
            source_id,
            s1_values,
            s1_x_values,
            measured_s1_max,
        )
        reported_s1_max_x = (
            None
            if s1_max_for_limit_x_json is None
            else _finite_numeric_value_or_ties(
                s1_max_for_limit_x_json,
                source_id,
            )
        )
        if s1_max != measured_s1_max or reported_s1_max_x != measured_s1_max_x:
            raise ReconciliationError(f"S1 maximum semantics do not match raw evidence for source {source_id}")
    if s1_limit_status == "exceeds_limit" and s1_max is None:
        raise ReconciliationError(f"Rejected S1 limit status lacks measured maximum for source {source_id}")
    if s1_limit is not None and s1_max is not None:
        exceeds_limit = s1_max > s1_limit
        if exceeds_limit != (s1_limit_status == "exceeds_limit"):
            raise ReconciliationError(f"Rejected S1 limit semantics do not match payload for source {source_id}")
    has_pair = row[13] is not None or row[14] is not None
    if not has_pair:
        if paired_x_column is not None:
            try:
                available_columns = json.loads(available_columns_json)
            except (TypeError, json.JSONDecodeError) as exc:
                raise ReconciliationError(f"Invalid available columns for source {source_id}") from exc
            if (
                not isinstance(available_columns, list)
                or not _column_is_available(selected_y_column, available_columns)
                or not _column_is_available(paired_x_column, available_columns)
            ):
                raise ReconciliationError(
                    f"Selected columns are not present in available columns for source {source_id}"
                )
            _validate_required_selected_columns(
                source_id,
                note_text,
                available_columns,
                selected_y_column,
                paired_x_column,
                steady_emission_y,
            )
        elif selected_y_column is not None:
            _validate_required_selected_y(
                source_id,
                note_text,
                selected_y_column,
                steady_emission_y,
            )
        if max_planned_y_json is not None or max_planned_y_x_json is not None:
            raise ReconciliationError(f"Rejected maximum fields lack selected X/Y payload for source {source_id}")
        return
    try:
        available_columns = json.loads(available_columns_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ReconciliationError(f"Invalid available columns for source {source_id}") from exc
    if (
        not isinstance(available_columns, list)
        or not _column_is_available(selected_y_column, available_columns)
        or not _column_is_available(paired_x_column, available_columns)
    ):
        raise ReconciliationError(f"Selected columns are not present in available columns for source {source_id}")
    _validate_required_selected_columns(
        source_id,
        note_text,
        available_columns,
        selected_y_column,
        paired_x_column,
        steady_emission_y,
    )
    has_maximum = max_planned_y_json is not None or max_planned_y_x_json is not None
    if has_maximum and (
        max_planned_y_json is None
        or max_planned_y_x_json is None
    ):
        raise ReconciliationError(
            f"Rejected selected X/Y payload has incomplete maximum fields for source {source_id}"
        )
    try:
        x_values, y_values = effective_xy_values(
            _json_array_value(selected_x_values_json, source_id),
            _json_array_value(selected_y_values_json, source_id),
        )
    except ValueError:
        if has_maximum:
            raise ReconciliationError(
                f"Rejected maximum fields do not match invalid X/Y payload for source {source_id}"
            )
        return
    if not has_maximum:
        raise ReconciliationError(
            f"Rejected selected X/Y payload is missing maximum fields for source {source_id}"
        )
    measured_max = max(y_values)
    if (
        _finite_numeric_value(max_planned_y_json, source_id) != measured_max
        or _finite_numeric_value_or_ties(max_planned_y_x_json, source_id)
        != _x_values_at_maximum(x_values, y_values, measured_max)
    ):
        raise ReconciliationError(f"Selected Y maximum semantics do not match rejected payload for source {source_id}")


def _validate_rejection_claim(
    source_id: str,
    row: tuple,
    *,
    page_type: str,
    sheet_names_json: str,
    s1_limit: int | float | None,
    steady_emission_y: str | None,
    allow_missing_s1: bool,
) -> None:
    reason = str(row[1])
    note_text = row[3]
    data_checksum = row[2]
    sheet_names = _sheet_names(sheet_names_json)
    if sheet_names is None:
        raise ReconciliationError(
            f"Rejected Book has invalid sheet evidence for source {source_id}"
        )
    note_count = sum(
        name.casefold().startswith("note")
        for name in sheet_names
    )
    data_count = sum(
        name.casefold().startswith("data")
        for name in sheet_names
    )
    if page_type != "worksheet":
        if reason != f"unsupported Origin page type: {page_type}":
            raise ReconciliationError(
                f"Rejected Book reason does not match page type for source {source_id}"
            )
        return
    if note_count > 1:
        if reason != "multiple Note sheets are ambiguous":
            raise ReconciliationError(
                f"Rejected Book reason does not match Note inventory for source {source_id}"
            )
        return
    if note_text is None:
        note_reason_matches = (
            reason == "missing Note"
            or (
                note_count == 1
                and reason.startswith("Note read failed: ")
                and bool(reason.removeprefix("Note read failed: ").strip())
            )
        )
        if not note_reason_matches:
            raise ReconciliationError(
                f"Rejected Book reason does not match missing Note evidence for source {source_id}"
            )
        return
    if data_count == 0:
        if reason != "missing Data sheet":
            raise ReconciliationError(
                f"Rejected Book reason does not match Data inventory for source {source_id}"
            )
        return
    try:
        parsed = parse_book_note(note_text)
    except NoteParseError as exc:
        if reason != str(exc):
            raise ReconciliationError(
                f"Rejected Book reason does not match Note parsing for source {source_id}"
            ) from exc
        return
    if data_count > 1 and parsed.spectrum_class != SpectrumClass.STEADY_2D:
        if reason != "multiple Data sheets are ambiguous":
            raise ReconciliationError(
                f"Rejected Book reason does not match Data inventory for source {source_id}"
            )
        return
    if data_checksum is None:
        if not (
            reason.startswith("Data read failed: ")
            and bool(reason.removeprefix("Data read failed: ").strip())
        ):
            raise ReconciliationError(
                f"Rejected Book lacks authenticated Data-read failure for source {source_id}"
            )
        return
    _validate_data_rejection_reason(
        source_id,
        row,
        s1_limit=s1_limit,
        steady_emission_y=steady_emission_y,
        allow_missing_s1=allow_missing_s1,
    )
    _reject_extractable_terminal_rejection(
        source_id,
        row,
        s1_limit=s1_limit,
        steady_emission_y=steady_emission_y,
        allow_missing_s1=allow_missing_s1,
    )


def _validate_data_rejection_reason(
    source_id: str,
    row: tuple,
    *,
    s1_limit: int | float | None,
    steady_emission_y: str | None,
    allow_missing_s1: bool,
) -> None:
    reason = str(row[1])
    data = _reconstructed_validation_data(source_id, row)
    try:
        spectrum_class = SpectrumClass(row[5])
    except ValueError as exc:
        raise ReconciliationError(
            f"Rejected Book has invalid spectrum class for source {source_id}"
        ) from exc
    effective_s1_limit = s1_limit
    if effective_s1_limit is None:
        effective_s1_limit = math.inf
        if reason == "S1 max exceeds limit":
            measured_s1_max = _finite_numeric_value(row[17], source_id)
            effective_s1_limit = math.nextafter(
                float(measured_s1_max),
                -math.inf,
            )
    validation = validate_spectrum_data(
        spectrum_class,
        data,
        steady_emission_y or row[7] or "S1c",
        effective_s1_limit,
        allow_missing_s1=allow_missing_s1,
    )
    expected_reason = format_validation_rejection_reason(
        validation.reason,
        validation.missing_column,
    )
    expected_status = (
        "exceeds_limit"
        if validation.reason == "S1 max exceeds limit"
        else "failed"
    )
    if (
        validation.ok
        or reason != expected_reason
        or row[19] != expected_status
    ):
        raise ReconciliationError(
            f"Rejected Book reason does not match raw data evidence for source {source_id}"
        )


def _reconstructed_validation_data(
    source_id: str,
    row: tuple,
) -> WorksheetData:
    data = WorksheetData(
        [
            Column(name, long_name, [], designation)
            for name, long_name, designation in _column_metadata_value(row[20])
        ]
    )
    bound_columns: set[int] = set()

    def bind(column: Column, values: list[object]) -> None:
        if id(column) not in bound_columns:
            column.values.extend(values)
            bound_columns.add(id(column))

    s1_values = (
        None
        if row[12] is None
        else _json_array_value(row[12], source_id)
    )
    s1_x_values = (
        None
        if row[11] is None
        else _json_array_value(row[11], source_id)
    )
    if s1_x_values is not None and s1_values is None:
        raise ReconciliationError(
            f"Rejected S1 X evidence has no S1 values for source {source_id}"
        )
    if s1_values is not None:
        s1_columns = data.matching_columns("S1")
        if len(s1_columns) != 1:
            raise ReconciliationError(
                f"Rejected S1 evidence does not match column metadata for source {source_id}"
            )
        bind(s1_columns[0], s1_values)
    if s1_x_values is not None:
        try:
            s1_pair = select_xy_pair(data, "S1")
        except DataColumnError as exc:
            raise ReconciliationError(
                f"Rejected S1 evidence does not match column metadata for source {source_id}"
            ) from exc
        bind(s1_pair.x_column, s1_x_values)

    if row[13] is not None or row[14] is not None:
        try:
            selected_pair = select_xy_pair(data, row[7])
        except DataColumnError as exc:
            raise ReconciliationError(
                f"Rejected selected X/Y evidence does not match column metadata for source {source_id}"
            ) from exc
        bind(
            selected_pair.x_column,
            _json_array_value(row[9], source_id),
        )
        bind(
            selected_pair.y_column,
            _json_array_value(row[10], source_id),
        )
    return data


def _reject_extractable_terminal_rejection(
    source_id: str,
    row: tuple,
    *,
    s1_limit: int | float | None,
    steady_emission_y: str | None,
    allow_missing_s1: bool,
) -> None:
    candidate = list(row)
    candidate[0] = "extracted"
    candidate[1] = None
    if candidate[5] == SpectrumClass.STEADY_2D.value:
        candidate[19] = "not_applicable"
    else:
        try:
            available_columns = json.loads(candidate[6])
        except (TypeError, json.JSONDecodeError):
            return
        if not isinstance(available_columns, list):
            return
        s1_max = (
            None
            if candidate[17] is None
            else _finite_numeric_value(candidate[17], source_id)
        )
        if candidate[19] == "exceeds_limit" and s1_limit is None:
            return
        if s1_max is None:
            if not allow_missing_s1:
                return
            candidate[19] = "missing_allowed"
        else:
            if s1_limit is not None and s1_max > s1_limit:
                return
            candidate[19] = "ok"
    try:
        _validate_extracted_payload(
            source_id,
            tuple(candidate),
            s1_limit=s1_limit,
            steady_emission_y=steady_emission_y,
            allow_missing_s1=allow_missing_s1,
        )
        _validate_column_metadata(source_id, tuple(candidate))
    except ReconciliationError:
        return
    raise ReconciliationError(
        f"Rejected Book payload is independently valid for extraction for source {source_id}"
    )


def _parsed_note_spectrum_class(note_text: str | None) -> str | None:
    if not note_text:
        return None
    try:
        return parse_book_note(note_text).spectrum_class.value
    except NoteParseError:
        return None


def _validate_required_selected_columns(
    source_id: str,
    note_text: str,
    available_columns: list[str],
    selected_y_column: str,
    paired_x_column: str,
    steady_emission_y: str | None,
) -> None:
    required_y = _validate_required_selected_y(
        source_id,
        note_text,
        selected_y_column,
        steady_emission_y,
    )
    required_key = _canonical_column_name(required_y)
    matching_indexes = [
        index
        for index, name in enumerate(available_columns)
        if _canonical_column_name(name) == required_key
    ]
    if len(matching_indexes) != 1 or matching_indexes[0] == 0:
        raise ReconciliationError(f"Required selected Y has no unique paired X for source {source_id}")
    expected_x = available_columns[matching_indexes[0] - 1]
    if _canonical_column_name(paired_x_column) != _canonical_column_name(expected_x):
        raise ReconciliationError(f"Payload does not use the selected Y paired X for source {source_id}")


def _validate_required_selected_y(
    source_id: str,
    note_text: str,
    selected_y_column: str,
    steady_emission_y: str | None,
) -> str:
    try:
        spectrum_class = parse_book_note(note_text).spectrum_class
    except NoteParseError as exc:
        raise ReconciliationError(f"Cannot derive required selected Y for source {source_id}") from exc
    if spectrum_class == SpectrumClass.STEADY_EMISSION and steady_emission_y is None:
        required_y = selected_y_column
    else:
        required_y = selected_y_for_class(spectrum_class, steady_emission_y or "S1c")
    required_key = _canonical_column_name(required_y)
    selected_key = _canonical_column_name(selected_y_column)
    if selected_key != required_key:
        raise ReconciliationError(f"Payload does not use required selected Y for source {source_id}")
    return required_y


def _canonical_column_name(name: str) -> str:
    compact = str(name).replace(" ", "").replace("/", "").casefold()
    return "s1c/r1c" if compact == "s1cr1c" else compact


def _column_is_available(selected_name: str, available_columns: list[str]) -> bool:
    return _column_count(selected_name, available_columns) > 0


def _column_count(selected_name: str, available_columns: list[str]) -> int:
    selected_key = _canonical_column_name(selected_name)
    return sum(_canonical_column_name(name) == selected_key for name in available_columns)


def _validate_optional_pair_shape(source_id: str, row: tuple) -> None:
    selected_x_values_json = row[9]
    selected_y_values_json = row[10]
    selected_x_row_count = row[13]
    selected_y_row_count = row[14]
    x_length = _json_array_length(selected_x_values_json)
    y_length = _json_array_length(selected_y_values_json)
    has_pair_data = x_length > 0 or y_length > 0 or selected_x_row_count is not None or selected_y_row_count is not None
    if not has_pair_data:
        return
    if row[0] == "rejected":
        if (
            x_length < 0
            or y_length < 0
            or not isinstance(selected_x_row_count, int)
            or not isinstance(selected_y_row_count, int)
            or selected_x_row_count != x_length
            or selected_y_row_count != y_length
        ):
            raise ReconciliationError(
                f"Selected row counts do not match rejected payload for source {source_id}"
            )
        return
    if (
        x_length <= 0
        or y_length <= 0
        or not isinstance(selected_x_row_count, int)
        or not isinstance(selected_y_row_count, int)
        or selected_x_row_count != x_length
        or selected_y_row_count != y_length
        or selected_x_row_count != selected_y_row_count
    ):
        raise ReconciliationError(f"Selected row counts do not match extracted payload for source {source_id}")


def _validate_column_metadata(
    source_id: str,
    row: tuple,
) -> None:
    data_checksum = row[2]
    metadata = _column_metadata_value(row[20])
    if data_checksum is None:
        if metadata:
            raise ReconciliationError(
                f"Column metadata has no data checksum for source {source_id}"
            )
        return
    if not metadata:
        raise ReconciliationError(
            f"Data payload lacks column metadata for source {source_id}"
        )
    data = WorksheetData(
        [
            Column(name, long_name, [], designation)
            for name, long_name, designation in metadata
        ]
    )
    try:
        available_columns = json.loads(row[6])
    except (TypeError, json.JSONDecodeError) as exc:
        raise ReconciliationError(
            f"Invalid available columns for source {source_id}"
        ) from exc
    if (
        not isinstance(available_columns, list)
        or any(
            not isinstance(name, str)
            for name in available_columns
        )
        or tuple(available_columns)
        != available_column_names(data, row[7])
    ):
        raise ReconciliationError(
            f"Available columns do not match physical column metadata for source {source_id}"
        )
    has_selected_pair = (
        row[0] == "extracted"
        and row[5] != "steady_2d"
    ) or row[8] is not None or _json_array_length(row[9]) > 0
    if has_selected_pair:
        try:
            pair = select_xy_pair(data, row[7])
        except DataColumnError as exc:
            raise ReconciliationError(
                f"Selected X/Y designations are invalid for source {source_id}"
            ) from exc
        if pair.x_column.long_name != row[8]:
            raise ReconciliationError(
                f"Selected X does not match physical column metadata for source {source_id}"
            )
        if row[0] == "extracted":
            s1_columns = data.matching_columns("S1")
            if len(s1_columns) == 1:
                if pair.y_column is s1_columns[0]:
                    raise ReconciliationError(
                        "selected Y and S1 resolve to the same physical "
                        f"column for source {source_id}"
                    )
                try:
                    select_xy_pair(data, "S1")
                except DataColumnError as exc:
                    raise ReconciliationError(
                        "S1 X/Y designations are invalid for source "
                        f"{source_id}"
                    ) from exc


def _validate_present_numeric_fields(source_id: str, row: tuple) -> None:
    for value in (row[9], row[10]):
        if value is None:
            continue
        try:
            decoded = json.loads(value)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ReconciliationError(f"Invalid numeric array for source {source_id}") from exc
        if not isinstance(decoded, list):
            raise ReconciliationError(f"Invalid numeric array for source {source_id}")
        if row[0] == "rejected" and row[2] is not None:
            continue
        for item in decoded:
            if not is_finite_real_number(item):
                raise ReconciliationError(f"Invalid numeric array for source {source_id}")
    for value in (row[15], row[17]):
        if value is not None:
            _finite_numeric_value(value, source_id)
    for value in (row[16], row[18]):
        if value is not None:
            _finite_numeric_value_or_ties(value, source_id)


def _column_metadata_value(
    value: str | None,
) -> tuple[tuple[str, str, str | None], ...]:
    try:
        decoded = json.loads(value or "[]")
    except (TypeError, json.JSONDecodeError) as exc:
        raise ReconciliationError("Invalid column metadata") from exc
    if not isinstance(decoded, list):
        raise ReconciliationError("Invalid column metadata")
    metadata = []
    for item in decoded:
        if (
            not isinstance(item, list)
            or len(item) != 3
            or not isinstance(item[0], str)
            or not item[0]
            or not isinstance(item[1], str)
            or not item[1]
            or item[2] not in {"X", "Y", None}
        ):
            raise ReconciliationError("Invalid column metadata")
        metadata.append((item[0], item[1], item[2]))
    return tuple(metadata)


def _validate_payload_checksum(
    source_id: str,
    page_type: str,
    folder_path: str,
    short_name: str,
    row: tuple,
) -> None:
    payload_checksum = row[-1]
    expected_checksum = _payload_checksum(source_id, page_type, folder_path, short_name, *row[:-1])
    if not payload_checksum or payload_checksum != expected_checksum:
        raise ReconciliationError(f"Book payload checksum mismatch for source {source_id}")


def _payload_checksum(*values: object) -> str:
    text = json.dumps(values, ensure_ascii=False, default=str, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _json_array_nonempty(value: str | None) -> bool:
    if not value:
        return False
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return False
    return isinstance(decoded, list) and bool(decoded)


def _json_array_length(value: str | None) -> int:
    if not value:
        return -1
    try:
        decoded = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return -1
    return len(decoded) if isinstance(decoded, list) else -1


def _json_array_empty_or_missing(value: str | None) -> bool:
    if value is None:
        return True
    try:
        decoded = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return False
    return decoded == []


def _json_array_value(
    value: str | None,
    source_id: str,
) -> list[object]:
    try:
        decoded = json.loads(value) if value is not None else None
    except (TypeError, json.JSONDecodeError) as exc:
        raise ReconciliationError(
            f"Invalid raw data array for source {source_id}"
        ) from exc
    if not isinstance(decoded, list):
        raise ReconciliationError(
            f"Invalid raw data array for source {source_id}"
        )
    return decoded


def _finite_numeric_array(value: str | None, source_id: str) -> list[float | int]:
    try:
        decoded = json.loads(value) if value is not None else None
    except (TypeError, json.JSONDecodeError) as exc:
        raise ReconciliationError(f"Invalid numeric array for source {source_id}") from exc
    if not isinstance(decoded, list) or not decoded:
        raise ReconciliationError(f"Invalid numeric array for source {source_id}")
    for item in decoded:
        if not is_finite_real_number(item):
            raise ReconciliationError(f"Invalid numeric array for source {source_id}")
    return decoded


def _require_unique_x_values(
    values: list[float | int],
    source_id: str,
    label: str,
) -> None:
    if len(values) != len(set(values)):
        raise ReconciliationError(
            f"duplicate {label} X values for source {source_id}"
        )


def _finite_numeric_value(value: str | None, source_id: str) -> float | int:
    try:
        decoded = json.loads(value) if value is not None else None
    except (TypeError, json.JSONDecodeError) as exc:
        raise ReconciliationError(f"Invalid numeric value for source {source_id}") from exc
    if not is_finite_real_number(decoded):
        raise ReconciliationError(f"Invalid numeric value for source {source_id}")
    return decoded


def _finite_numeric_value_or_ties(value: str | None, source_id: str) -> float | int | list[float | int]:
    try:
        decoded = json.loads(value) if value is not None else None
    except (TypeError, json.JSONDecodeError) as exc:
        raise ReconciliationError(f"Invalid numeric maximum X for source {source_id}") from exc
    if isinstance(decoded, list):
        if not decoded:
            raise ReconciliationError(f"Invalid numeric maximum X for source {source_id}")
        for item in decoded:
            if not is_finite_real_number(item):
                raise ReconciliationError(f"Invalid numeric maximum X for source {source_id}")
        return decoded
    if not is_finite_real_number(decoded):
        raise ReconciliationError(f"Invalid numeric maximum X for source {source_id}")
    return decoded


def _validated_s1_evidence(
    source_id: str,
    *,
    s1_values_json: str | None,
    s1_x_values_json: str | None,
) -> tuple[tuple[float | int, ...] | None, tuple[object, ...] | None]:
    if s1_values_json is None:
        if s1_x_values_json is not None:
            raise ReconciliationError(f"S1 X evidence lacks S1 values for source {source_id}")
        return None, None
    try:
        raw_s1_values = json.loads(s1_values_json)
        raw_s1_x_values = None if s1_x_values_json is None else json.loads(s1_x_values_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ReconciliationError(f"Invalid raw S1 evidence for source {source_id}") from exc
    if not isinstance(raw_s1_values, list):
        raise ReconciliationError(f"Invalid raw S1 evidence for source {source_id}")
    if raw_s1_x_values is not None and not isinstance(raw_s1_x_values, list):
        raise ReconciliationError(f"S1 X evidence length mismatch for source {source_id}")
    effective_length = len(raw_s1_values)
    while effective_length and _snapshot_value_is_blank(raw_s1_values[effective_length - 1]):
        effective_length -= 1
    if raw_s1_x_values is not None and len(raw_s1_x_values) < effective_length:
        raise ReconciliationError(f"S1 X evidence length mismatch for source {source_id}")
    checked_values: list[float | int] = []
    for row_index, value in enumerate(raw_s1_values[:effective_length], start=1):
        if _snapshot_value_is_blank(value):
            raise ReconciliationError(f"Blank inside raw S1 evidence for source {source_id} at row {row_index}")
        if not is_finite_real_number(value):
            raise ReconciliationError(f"Invalid raw S1 evidence for source {source_id} at row {row_index}")
        checked_values.append(value)
    checked_x_values = None
    if raw_s1_x_values is not None:
        checked_x: list[float | int] = []
        for row_index, value in enumerate(raw_s1_x_values[:effective_length], start=1):
            if _snapshot_value_is_blank(value):
                raise ReconciliationError(
                    f"Blank inside raw S1 X evidence for source {source_id} at row {row_index}"
                )
            if not is_finite_real_number(value):
                raise ReconciliationError(
                    f"Invalid raw S1 X evidence for source {source_id} at row {row_index}"
                )
            checked_x.append(value)
        _require_unique_x_values(
            checked_x,
            source_id,
            "S1",
        )
        checked_x_values = tuple(checked_x)
    return tuple(checked_values), checked_x_values


def _s1_x_at_maximum(
    source_id: str,
    s1_values: tuple[float | int, ...],
    s1_x_values: tuple[object, ...] | None,
    maximum: float | int,
) -> float | int | list[float | int] | None:
    if s1_x_values is None:
        return None
    tied = [
        x
        for x, s1_value in zip(s1_x_values, s1_values)
        if s1_value == maximum
    ]
    for value in tied:
        if not is_finite_real_number(value):
            raise ReconciliationError(f"Invalid S1 maximum X evidence for source {source_id}")
    return tied[0] if len(tied) == 1 else tied


def _snapshot_value_is_blank(value: object) -> bool:
    return value is None or value == ""


def _x_values_at_maximum(
    x_values: list[float | int],
    y_values: list[float | int],
    maximum: float | int,
) -> float | int | list[float | int]:
    tied = [x for x, y in zip(x_values, y_values) if y == maximum]
    return tied[0] if len(tied) == 1 else tied


def _has_stable_identity_divergence(
    connection: sqlite3.Connection,
    source_id: str,
    *,
    cancel_check=None,
) -> bool:
    rows = connection.execute(
        """
        select inventory.display_name, inventory.page_order, inventory.sheet_names_json,
               inventory.has_note, inventory.has_data,
               result.display_name, result.page_order, result.pass_two_sheet_names_json,
               result.pass_two_has_note, result.pass_two_has_data
        from inventory_rows as inventory
        join book_results as result using (source_id, page_type, folder_path, short_name)
        where inventory.source_id = ?
        """,
        (source_id,),
    )
    for row in rows:
        if cancel_check is not None:
            cancel_check()
        if (
            row[0] != row[5]
            or row[1] != row[6]
            or _sheet_names(row[2]) != _sheet_names(row[7])
            or bool(row[3]) != bool(row[8])
            or bool(row[4]) != bool(row[9])
            or not _sheet_flags_match(row[2], row[3], row[4])
            or not _sheet_flags_match(row[7], row[8], row[9])
        ):
            return True
    return False


def _sheet_names(value: str | None) -> tuple[str, ...] | None:
    try:
        decoded = json.loads(value) if value is not None else None
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(decoded, list) or any(not isinstance(item, str) for item in decoded):
        return None
    return tuple(decoded)


def _sheet_flags_match(sheet_names_json: str | None, has_note: object, has_data: object) -> bool:
    sheet_names = _sheet_names(sheet_names_json)
    if sheet_names is None:
        return False
    expected_note = any(name.casefold().startswith("note") for name in sheet_names)
    expected_data = any(name.casefold().startswith("data") for name in sheet_names)
    return bool(has_note) == expected_note and bool(has_data) == expected_data


def _validate_steady_2d_ordinary_fields(source_id: str, row: tuple) -> None:
    ordinary_fields = (
        row[7],
        row[8],
        row[11],
        row[12],
        row[13],
        row[14],
        row[15],
        row[16],
        row[17],
        row[18],
    )
    if any(value is not None for value in ordinary_fields) or not (
        _json_array_empty_or_missing(row[9]) and _json_array_empty_or_missing(row[10])
    ):
        raise ReconciliationError(f"2D payload contains ordinary spectrum fields for source {source_id}")


def _identity_set(
    connection: sqlite3.Connection,
    table: str,
    source_id: str,
    *,
    cancel_check=None,
) -> set[tuple[str, str, str, str]]:
    rows = connection.execute(
        f"select source_id, page_type, folder_path, short_name from {table} where source_id = ?",
        (source_id,),
    )
    identities = set()
    for row in rows:
        if cancel_check is not None:
            cancel_check()
        identities.add((row[0], row[1], row[2], row[3]))
    return identities


def _ensure_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        create table if not exists source_files (
            source_id text primary key,
            copy_path text not null,
            sha256 text not null,
            original_path text,
            original_size_bytes integer,
            original_mtime_ns integer
        )
        """
    )
    source_columns = _table_columns(connection, "source_files")
    for name, column_type in {
        "original_path": "text",
        "original_size_bytes": "integer",
        "original_mtime_ns": "integer",
    }.items():
        if name not in source_columns:
            connection.execute(
                f"alter table source_files add column {name} {column_type}"
            )
    _ensure_inventory_table(connection)
    _ensure_book_results_table(connection)
    connection.execute(
        """
        create table if not exists worker_attempts (
            id integer primary key,
            source_id text not null,
            attempt integer not null,
            status text not null,
            message text not null
        )
        """
    )
    connection.execute(
        """
        create table if not exists reconciliation_results (
            source_id text primary key,
            status text not null,
            message text not null
        )
        """
    )


_INVENTORY_COLUMNS = {
    "source_id": "text not null",
    "page_type": "text not null default 'worksheet'",
    "folder_path": "text not null",
    "short_name": "text not null",
    "display_name": "text not null",
    "page_order": "integer not null",
    "sheet_names_json": "text not null",
    "has_note": "integer not null",
    "has_data": "integer not null",
}

_BOOK_RESULT_COLUMNS = {
    "source_id": "text not null",
    "page_type": "text not null default 'worksheet'",
    "folder_path": "text not null",
    "short_name": "text not null",
    "status": "text not null",
    "note_text": "text",
    "rejection_reason": "text",
    "display_name": "text",
    "page_order": "integer",
    "spectrum_class": "text",
    "data_sheet_name": "text",
    "available_columns_json": "text",
    "selected_y_column": "text",
    "paired_x_column": "text",
    "selected_x_values_json": "text",
    "selected_y_values_json": "text",
    "s1_x_values_json": "text",
    "s1_values_json": "text",
    "selected_x_row_count": "integer",
    "selected_y_row_count": "integer",
    "max_planned_y_json": "text",
    "max_planned_y_x_json": "text",
    "s1_max_for_limit_json": "text",
    "s1_max_for_limit_x_json": "text",
    "s1_limit_status": "text",
    "data_checksum": "text",
    "column_metadata_json": "text",
    "payload_checksum": "text",
    "pass_two_sheet_names_json": "text not null default '[]'",
    "pass_two_has_note": "integer not null default 0",
    "pass_two_has_data": "integer not null default 0",
}

_EXPECTED_IDENTITY_PK = ("source_id", "page_type", "folder_path", "short_name")


def _ensure_inventory_table(connection: sqlite3.Connection) -> None:
    _ensure_identity_table(
        connection,
        "inventory_rows",
        _INVENTORY_COLUMNS,
        _EXPECTED_IDENTITY_PK,
    )


def _ensure_book_results_table(connection: sqlite3.Connection) -> None:
    _ensure_identity_table(
        connection,
        "book_results",
        _BOOK_RESULT_COLUMNS,
        _EXPECTED_IDENTITY_PK,
    )


def _ensure_identity_table(
    connection: sqlite3.Connection,
    table: str,
    columns: dict[str, str],
    primary_key: tuple[str, ...],
) -> None:
    if not _table_exists(connection, table):
        _create_identity_table(connection, table, columns, primary_key)
        return
    existing = _table_columns(connection, table)
    for name, column_type in columns.items():
        if name not in existing:
            connection.execute(f"alter table {table} add column {name} {column_type}")
    if _primary_key_columns(connection, table) != primary_key:
        _migrate_identity_table(connection, table, columns, primary_key)
    _normalize_identity_page_type(connection, table)


def _normalize_identity_page_type(connection: sqlite3.Connection, table: str) -> None:
    if "page_type" not in _table_columns(connection, table):
        return
    connection.execute(
        f"""
        delete from {table}
        where (page_type is null or page_type = '')
          and exists (
            select 1 from {table} as clean
            where clean.source_id = {table}.source_id
              and clean.page_type = 'worksheet'
              and clean.folder_path = {table}.folder_path
              and clean.short_name = {table}.short_name
          )
        """
    )
    connection.execute(f"update {table} set page_type = 'worksheet' where page_type is null or page_type = ''")


def _create_identity_table(
    connection: sqlite3.Connection,
    table: str,
    columns: dict[str, str],
    primary_key: tuple[str, ...],
) -> None:
    column_sql = ",\n            ".join(f"{name} {column_type}" for name, column_type in columns.items())
    pk_sql = ", ".join(primary_key)
    connection.execute(
        f"""
        create table if not exists {table} (
            {column_sql},
            primary key ({pk_sql})
        )
        """
    )


def _migrate_identity_table(
    connection: sqlite3.Connection,
    table: str,
    columns: dict[str, str],
    primary_key: tuple[str, ...],
) -> None:
    temporary = f"{table}__new"
    connection.execute(f"drop table if exists {temporary}")
    _create_identity_table(connection, temporary, columns, primary_key)
    existing = _table_columns(connection, table)
    target_columns = tuple(columns.keys())
    select_sql = ", ".join(_migration_select_expression(name, existing) for name in target_columns)
    insert_sql = ", ".join(target_columns)
    connection.execute(f"insert into {temporary} ({insert_sql}) select {select_sql} from {table}")
    connection.execute(f"drop table {table}")
    connection.execute(f"alter table {temporary} rename to {table}")


def _migration_select_expression(name: str, existing: set[str]) -> str:
    if name == "page_type":
        if name in existing:
            return "coalesce(nullif(page_type, ''), 'worksheet')"
        return "'worksheet'"
    if name in existing:
        return name
    if name in {"display_name", "status"}:
        return "''"
    if name == "page_order":
        return "0"
    if name in {
        "sheet_names_json",
        "available_columns_json",
        "column_metadata_json",
        "selected_x_values_json",
        "selected_y_values_json",
    }:
        return "'[]'"
    if name in {"has_note", "has_data"}:
        return "0"
    return "null"


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    row = connection.execute("select 1 from sqlite_master where type = 'table' and name = ?", (table,)).fetchone()
    return row is not None


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f"pragma table_info({table})").fetchall()}


def _primary_key_columns(connection: sqlite3.Connection, table: str) -> tuple[str, ...]:
    rows = connection.execute(f"pragma table_info({table})").fetchall()
    keyed = sorted((int(row[5]), row[1]) for row in rows if int(row[5]) > 0)
    return tuple(name for _order, name in keyed)
