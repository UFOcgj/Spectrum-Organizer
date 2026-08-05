from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
import hashlib
import json
import math
from typing import Callable

from spectrum_organizer.core.metadata_numeric import is_finite_real_number
from spectrum_organizer.core.validity import (
    effective_xy_values,
    format_validation_rejection_reason,
    selected_y_for_class,
    validate_spectrum_data,
)
from spectrum_organizer.core.data_columns import (
    Column,
    DataColumnError,
    WorksheetData,
    available_column_names,
    column_metadata,
    select_xy_pair,
)
from spectrum_organizer.domain.extracted import InventoryBook, TerminalBookResult
from spectrum_organizer.origin.extract_worker import (
    InfrastructureExtractionError,
    WorkerPreflightError,
)
from spectrum_organizer.core.note_parser import NoteParseError, parse_book_note
from spectrum_organizer.origin.contracts import (
    BookWriteContract,
    ColumnWriteContract,
    FolderWriteContract,
    FormulaCalculationState,
    OriginStructureMismatchError,
    ProjectWriteContract,
)


COMMENTS_LABEL_ROW_HEIGHT_LINES = 5
METHOD_LABEL_ROW_HEIGHT_LINES = 2
FORMULA_LABEL_ROW_HEIGHT_LINES = 2


def _column_letter(position: int) -> str:
    if position < 1:
        raise ValueError("column position must be positive")
    result = ""
    while position:
        position, remainder = divmod(position - 1, 26)
        result = chr(ord("A") + remainder) + result
    return result


class OriginOutputSession:
    def __init__(self, origin_module: object):
        self.origin = origin_module
        self._last_sheet = None

    def new(self) -> None:
        self.origin.new(asksave=False)

    def delete_default_template_book(self) -> None:
        find_book = getattr(self.origin, "find_book", None)
        if find_book is None:
            return
        default_book = find_book("w")
        if default_book is not None:
            default_book.destroy()

    def root_folder_path(self) -> str:
        return self.origin.pe.root_folder().path

    def add_folder(self, root_path: str, folder_path: str) -> str:
        current = root_path
        self.origin.pe.cd(current)
        for part in _folder_parts(folder_path):
            self.origin.pe.cd(current)
            reused = _rename_empty_default_folder(self.origin, current, part)
            current = _folder_handle_path(reused or self.origin.pe.mkdir(part, chk=True))
            self.origin.pe.cd(current)
        return current

    def add_book(self, folder_handle: str, display_long_name: str):
        self.origin.pe.cd(folder_handle)
        book = self.origin.new_book("w", lname="", hidden=False)
        book.lname = display_long_name
        sheet = book[0]
        sheet.name = "Data"
        sheet.lname = "Data"
        self._last_sheet = sheet
        return sheet

    def write_column(self, book_handle: object, column: ColumnWriteContract) -> None:
        index = _column_index(column.short_name)
        if hasattr(book_handle, "cols") and book_handle.cols < index + 1:
            book_handle.cols = index + 1
        book_handle.from_list(
            index,
            [_origin_value(value) for value in column.values],
            lname="",
            comments=column.comment,
            axis=column.designation,
        )
        _set_label_row_heights(book_handle)
        if column.method is not None:
            book_handle.set_label(index, column.method, "Method")
        if column.formula is not None:
            book_handle.set_formula(index, column.formula)
            _set_formula_recalculation_auto(book_handle, index)
        self._last_sheet = book_handle

    def method_row(self, column_short_name: str) -> str | None:
        if self._last_sheet is None:
            return None
        return self._last_sheet.get_label(_column_index(column_short_name), "Method")

    def save(self, path: Path) -> None:
        if not self.origin.save(str(path)):
            raise RuntimeError(f"Origin save failed: {path}")
        wait = getattr(self.origin, "wait", None)
        if wait is not None:
            wait()

    def close(self) -> None:
        exit_origin = getattr(self.origin, "exit", None)
        if callable(exit_origin):
            exit_origin()


class OriginVerifierSession:
    def __init__(self, origin_module: object):
        self.origin = origin_module
        self._contract_reader = self._read_project_contract

    def open(self, path: Path, readonly: bool) -> None:
        if not self.origin.open(str(path), readonly=readonly, asksave=True):
            raise RuntimeError(f"Origin open failed: {path}")

    def read_project_contract(self) -> ProjectWriteContract:
        return self._contract_reader()

    def _read_project_contract(self) -> ProjectWriteContract:
        folders: list[FolderWriteContract] = []
        root_folder = self.origin.po.RootFolder
        root_path = _folder_contract_path(root_folder)
        self._visit_folder(root_folder, folders, root_path, is_root=True)
        return ProjectWriteContract("/", tuple(folders))

    def _visit_folder(
        self,
        folder: object,
        folders: list[FolderWriteContract],
        root_path: str,
        *,
        is_root: bool = False,
    ) -> None:
        books = []
        for page_base in list(folder.PageBases()):
            if page_base.GetType() != self.origin.po.OPT_WORKSHEET:
                raise OriginStructureMismatchError(
                    f"Verifier encountered unsupported page type in {_folder_contract_path(folder)}"
                )
            page = self.origin.po.Pages(page_base.GetName())
            layers = list(page.Layers)
            if len(layers) != 1:
                raise OriginStructureMismatchError(
                    f"Verifier requires exactly one worksheet layer in {page_base.GetName()}"
                )
            worksheet = self.origin.WSheet(layers[0])
            books.append(_read_book_contract(page_base, worksheet))
        if not is_root and not books:
            raise OriginStructureMismatchError(
                f"Verifier encountered empty output folder: {_folder_contract_path(folder)}"
            )
        if books:
            folders.append(FolderWriteContract(_relative_project_folder_path(folder, root_path), tuple(books)))
        for child in list(folder.Folders):
            self._visit_folder(child, folders, root_path)

    def close(self) -> None:
        exit_origin = getattr(self.origin, "exit", None)
        if callable(exit_origin):
            exit_origin()


@dataclass(frozen=True)
class OriginDependencyProof:
    origin: object

    def open(self, path: Path, readonly: bool) -> None:
        if not self.origin.open(str(path), readonly=readonly, asksave=False):
            raise RuntimeError(f"Origin open failed: {path}")

    def assert_raw_to_norm_live(self, folder_path: str, book_display_name: str, raw_column_short_name: str, norm_column_short_name: str):
        sheet = _find_sheet_by_folder_and_book_long_name(self.origin, folder_path, book_display_name)
        raw_index = _column_index(raw_column_short_name)
        norm_index = _column_index(norm_column_short_name)
        before = list(sheet.to_list(norm_index))
        raw_values = list(sheet.to_list(raw_index))
        if len(raw_values) != len(before):
            raise RuntimeError(
                "Raw and Norm row counts differ before dependency proof"
            )
        finite_rows = [
            (index, value)
            for index, value in enumerate(raw_values)
            if is_finite_real_number(value)
        ]
        if not finite_rows:
            raise RuntimeError(
                "No finite Raw value available for dependency proof"
            )
        maximum = max(value for _, value in finite_rows)
        if maximum <= 0:
            raise RuntimeError(
                "No positive Raw maximum available for dependency proof"
            )
        if len(finite_rows) < 2:
            raise RuntimeError(
                "No independent Raw row available for dependency proof"
            )
        maximum_rows = [
            index for index, value in finite_rows if value == maximum
        ]
        target_row = (
            maximum_rows[0]
            if len(maximum_rows) > 1
            else next(index for index, value in finite_rows if value != maximum)
        )
        original_value = raw_values[target_row]
        probe = None
        for ratio in (0.5, 0.25, 0.75):
            replacement = maximum * ratio
            expected_value = Decimal(str(ratio))
            if replacement == original_value:
                continue
            if (
                is_finite_real_number(before[target_row])
                and Decimal(str(before[target_row])) == expected_value
            ):
                continue
            probe = replacement, expected_value
            break
        if probe is None:
            raise RuntimeError(
                "No distinguishable Raw probe value available for dependency proof"
            )
        replacement, expected_value = probe
        wait = getattr(self.origin, "wait", None)
        probe_values = list(raw_values)
        probe_values[target_row] = replacement
        try:
            sheet.from_list(raw_index, probe_values)
            if wait is not None:
                wait()
            after = list(sheet.to_list(norm_index))
            other_rows_changed = any(
                index != target_row and not _same_dependency_value(old, new)
                for index, (old, new) in enumerate(zip(before, after))
            )
            new_value = after[target_row] if len(after) == len(before) else None
            if (
                other_rows_changed
                or not is_finite_real_number(new_value)
                or Decimal(str(new_value)) != expected_value
            ):
                raise RuntimeError(f"Raw-to-Norm dependency did not update for {folder_path}/{book_display_name}")
        finally:
            sheet.from_list(raw_index, raw_values)
            if wait is not None:
                wait()
            restored_raw = list(sheet.to_list(raw_index))
            restored_norm = list(sheet.to_list(norm_index))
            if (
                not _same_dependency_values(raw_values, restored_raw)
                or not _same_dependency_values(before, restored_norm)
            ):
                raise RuntimeError(
                    f"Raw-to-Norm dependency proof did not restore {folder_path}/{book_display_name}"
                )
        norm_column = sheet.obj[norm_index]
        formula = norm_column.GetStrProp("formula")
        mode = int(norm_column.GetNumProp("svrm"))
        return FormulaCalculationState(
            recalculation_mode={0: "none", 1: "automatic", 2: "manual"}.get(
                mode,
                f"unknown:{mode}",
            ),
            lock_state="formula_lock" if formula else "none",
        )


class OriginExtractionWorkerFactory:
    def __init__(
        self,
        origin_loader: Callable[[], object],
        *,
        s1_limit: int = 2_000_000,
        steady_emission_y: str = "S1c",
        allow_missing_s1: bool = False,
        before_origin_launch: Callable[[], None] | None = None,
        after_origin_launch: Callable[[object], None] | None = None,
        after_project_open: Callable[[Path], None] | None = None,
    ):
        self._origin_loader = origin_loader
        self._s1_limit = s1_limit
        self._steady_emission_y = steady_emission_y
        self._allow_missing_s1 = allow_missing_s1
        self._before_origin_launch = before_origin_launch
        self._after_origin_launch = after_origin_launch
        self._after_project_open = after_project_open

    def create(self, source_id: str, attempt: int) -> "OriginExtractionWorker":
        origin = None
        try:
            if self._before_origin_launch is not None:
                self._before_origin_launch()
            origin = self._origin_loader()
            if self._after_origin_launch is not None:
                self._after_origin_launch(origin)
        except Exception as exc:
            if origin is not None:
                exit_origin = getattr(origin, "exit", None)
                if exit_origin is not None:
                    try:
                        exit_origin()
                    except Exception:
                        pass
            raise InfrastructureExtractionError(f"Origin worker launch failed: {exc}") from exc
        return OriginExtractionWorker(
            source_id,
            attempt,
            origin,
            self._s1_limit,
            self._steady_emission_y,
            self._allow_missing_s1,
            self._after_project_open,
        )


@dataclass
class OriginExtractionWorker:
    source_id: str
    attempt: int
    origin: object
    s1_limit: int = 2_000_000
    steady_emission_y: str = "S1c"
    allow_missing_s1: bool = False
    after_project_open: Callable[[Path], None] | None = None

    def __post_init__(self) -> None:
        self.open_targets: list[Path] = []

    def iter_inventory(self, copy_path: Path, allowlist: set[Path]):
        _validate_allowlisted_copy(Path(copy_path), allowlist)
        try:
            if not self.origin.open(str(copy_path), readonly=True, asksave=True):
                raise InfrastructureExtractionError(f"Origin open failed: {copy_path}")
            self.open_targets.append(Path(copy_path))
            if self.after_project_open is not None:
                self.after_project_open(Path(copy_path))
            yield from _iter_inventory_books(self.origin, self.source_id)
        except InfrastructureExtractionError:
            raise
        except Exception as exc:
            raise InfrastructureExtractionError(f"Origin extraction API failed: {exc}") from exc

    def iter_book_results(self):
        try:
            yield from _iter_payload_transactions(
                self.origin,
                self.source_id,
                self.steady_emission_y,
                self.s1_limit,
                self.allow_missing_s1,
            )
        except InfrastructureExtractionError:
            raise
        except Exception as exc:
            raise InfrastructureExtractionError(f"Origin extraction API failed: {exc}") from exc

    def close(self) -> None:
        exit_origin = getattr(self.origin, "exit", None)
        if exit_origin is not None:
            try:
                exit_origin()
            except Exception as exc:
                raise InfrastructureExtractionError(f"Origin worker close failed: {exc}") from exc


def _validate_allowlisted_copy(copy_path: Path, allowlist: set[Path]) -> None:
    if Path(copy_path).resolve() not in {Path(path).resolve() for path in allowlist}:
        raise WorkerPreflightError(f"Origin extraction target is not allowlisted: {copy_path}")


def _iter_inventory_books(
    origin: object,
    source_id: str,
):
    for page_order, folder, page_base in _book_page_bases(origin):
        book, _layers = _inventory_book_from_page_base(origin, source_id, page_order, folder, page_base)
        yield book


def _iter_payload_transactions(
    origin: object,
    source_id: str,
    steady_emission_y: str,
    s1_limit: int,
    allow_missing_s1: bool = False,
):
    for page_order, folder, page_base in _book_page_bases(origin):
        book, layers = _inventory_book_from_page_base(origin, source_id, page_order, folder, page_base)
        if book.page_type != "worksheet":
            yield book, TerminalBookResult(
                source_id=book.source_id,
                folder_path=book.folder_path,
                short_name=book.short_name,
                status="rejected",
                rejection_reason=f"unsupported Origin page type: {book.page_type}",
                display_name=book.display_name,
                page_order=book.page_order,
                page_type=book.page_type,
            )
            continue
        yield book, _extract_book_result(
            origin,
            book,
            layers,
            steady_emission_y,
            s1_limit,
            allow_missing_s1,
        )


def _inventory_book_from_page_base(origin: object, source_id: str, page_order: int, folder: object, page_base: object) -> tuple[InventoryBook, list[object]]:
    page = origin.po.Pages(page_base.GetName())
    layers = list(page.Layers)
    sheet_names = tuple(_sheet_name(layer) for layer in layers)
    book = InventoryBook(
        source_id=source_id,
        folder_path=_relative_project_folder_path(folder, _folder_contract_path(origin.po.RootFolder)),
        short_name=page_base.GetName(),
        display_name=page_base.GetLongName() or "",
        page_order=page_order,
        sheet_names=sheet_names,
        has_note=any(name.casefold().startswith("note") for name in sheet_names),
        has_data=any(name.casefold().startswith("data") for name in sheet_names),
        page_type=_book_page_type(origin, page_base),
    )
    return book, layers


def _extract_book_result(
    origin: object,
    book: InventoryBook,
    layers: list[object],
    steady_emission_y: str,
    s1_limit: int,
    allow_missing_s1: bool = False,
) -> TerminalBookResult:
    note_layers = [
        layer
        for layer in layers
        if _sheet_name(layer).casefold().startswith("note")
    ]
    data_layers = [
        layer
        for layer in layers
        if _sheet_name(layer).casefold().startswith("data")
    ]
    note_read = (
        _read_note_text(origin, note_layers)
        if len(note_layers) == 1
        else _NoteReadResult(None)
    )
    note_text = note_read.text
    data_layer = data_layers[0] if data_layers else None
    base = {
        "source_id": book.source_id,
        "folder_path": book.folder_path,
        "short_name": book.short_name,
        "display_name": book.display_name,
        "page_order": book.page_order,
        "note_text": note_text,
        "data_sheet_name": _sheet_name(data_layer) if data_layer is not None else None,
        "page_type": book.page_type,
    }
    if len(note_layers) > 1:
        return TerminalBookResult(
            status="rejected",
            rejection_reason="multiple Note sheets are ambiguous",
            **base,
        )
    if note_text is None:
        reason = "missing Note" if not note_read.errors else "Note read failed: " + "; ".join(note_read.errors)
        return TerminalBookResult(status="rejected", rejection_reason=reason, **base)
    if data_layer is None:
        return TerminalBookResult(status="rejected", rejection_reason="missing Data sheet", **base)
    try:
        parsed = parse_book_note(note_text)
    except NoteParseError as exc:
        return TerminalBookResult(status="rejected", rejection_reason=str(exc), **base)
    base["spectrum_class"] = parsed.spectrum_class.value
    if len(data_layers) > 1 and parsed.spectrum_class.name != "STEADY_2D":
        return TerminalBookResult(
            status="rejected",
            rejection_reason="multiple Data sheets are ambiguous",
            **base,
        )

    try:
        data = _read_worksheet_data(origin, data_layer)
    except InfrastructureExtractionError:
        raise
    except Exception as exc:
        if _is_origin_session_infrastructure_error(exc):
            raise InfrastructureExtractionError(f"Origin data session failed: {exc}") from exc
        return TerminalBookResult(
            status="rejected",
            rejection_reason=f"Data read failed: {exc}",
            **base,
        )
    checksum = _data_checksum(data)
    metadata = column_metadata(data)
    if parsed.spectrum_class.name == "STEADY_2D":
        return TerminalBookResult(
            status="extracted",
            rejection_reason=None,
            available_columns=available_column_names(data),
            column_metadata=metadata,
            s1_limit_status="not_applicable",
            data_checksum=checksum,
            **base,
        )

    selected_y = selected_y_for_class(parsed.spectrum_class, steady_emission_y)
    available_columns = available_column_names(data, selected_y)
    s1_x_values, s1_values = _unique_s1_evidence(data)
    validation = validate_spectrum_data(
        parsed.spectrum_class,
        data,
        steady_emission_y,
        s1_limit,
        allow_missing_s1=allow_missing_s1,
    )
    pair = _try_selected_pair(data, selected_y)
    x_values: tuple[object, ...] = ()
    y_values: tuple[object, ...] = ()
    x_row_count = None
    y_row_count = None
    max_y = None
    max_y_x = None
    paired_x_column = pair.x_column.long_name if pair is not None else None
    if pair is not None:
        x_values = tuple(pair.x_column.values)
        y_values = tuple(pair.y_column.values)
        x_row_count = len(x_values)
        y_row_count = len(y_values)
        try:
            effective_x, effective_y = effective_xy_values(pair.x_column.values, pair.y_column.values)
        except ValueError:
            pass
        else:
            x_values = tuple(effective_x)
            y_values = tuple(effective_y)
            x_row_count = len(x_values)
            y_row_count = len(y_values)
            if effective_y:
                max_y = max(effective_y)
                tied_max_x = tuple(x for x, y in zip(effective_x, effective_y, strict=True) if y == max_y)
                max_y_x = tied_max_x[0] if len(tied_max_x) == 1 else tied_max_x
    if not validation.ok:
        status = "exceeds_limit" if validation.reason == "S1 max exceeds limit" else "failed"
        return TerminalBookResult(
            status="rejected",
            rejection_reason=format_validation_rejection_reason(
                validation.reason,
                validation.missing_column,
            ),
            available_columns=available_columns,
            column_metadata=metadata,
            selected_y_column=selected_y,
            paired_x_column=paired_x_column,
            selected_x_values=x_values,
            selected_y_values=y_values,
            s1_x_values=s1_x_values,
            s1_values=s1_values,
            selected_x_row_count=x_row_count,
            selected_y_row_count=y_row_count,
            max_planned_y=max_y,
            max_planned_y_x=max_y_x,
            s1_max_for_limit=validation.s1_max,
            s1_max_for_limit_x=validation.s1_max_x,
            s1_limit_status=status,
            data_checksum=checksum,
            **base,
        )
    return TerminalBookResult(
        status="extracted",
        rejection_reason=None,
        available_columns=available_columns,
        column_metadata=metadata,
        selected_y_column=selected_y,
        paired_x_column=paired_x_column,
        selected_x_values=x_values,
        selected_y_values=y_values,
        s1_x_values=s1_x_values,
        s1_values=s1_values,
        selected_x_row_count=x_row_count,
        selected_y_row_count=y_row_count,
        max_planned_y=validation.selected_y_max,
        max_planned_y_x=validation.x_at_max_y,
        s1_max_for_limit=validation.s1_max,
        s1_max_for_limit_x=validation.s1_max_x,
        s1_limit_status="missing_allowed" if validation.s1_max is None else "ok",
        data_checksum=checksum,
        **base,
    )


@dataclass(frozen=True)
class _NoteReadResult:
    text: str | None
    errors: tuple[str, ...] = ()


def _read_note_text(origin: object, layers: list[object]) -> _NoteReadResult:
    fallback: str | None = None
    errors: list[str] = []
    for layer in layers:
        if not _sheet_name(layer).casefold().startswith("note"):
            continue
        candidates, direct_errors = _direct_note_text_candidates(layer)
        errors.extend(direct_errors)
        for candidate in candidates:
            text = _usable_note_text(candidate)
            if text is None:
                continue
            if text.startswith("[EXP_FD_FILE]"):
                return _NoteReadResult(text, tuple(errors))
            if fallback is None:
                fallback = text
        worksheet_text, worksheet_errors = _read_note_worksheet_text(origin, layer)
        errors.extend(worksheet_errors)
        text = _usable_note_text(worksheet_text)
        if text is None:
            continue
        if text.startswith("[EXP_FD_FILE]"):
            return _NoteReadResult(text, tuple(errors))
        if fallback is None:
            fallback = text
    return _NoteReadResult(fallback, tuple(errors))


def _direct_note_text_candidates(layer: object) -> tuple[list[object], list[str]]:
    candidates: list[object] = []
    errors: list[str] = []
    try:
        getter = getattr(layer, "GetText", None)
    except Exception as exc:
        _raise_note_infrastructure_error(exc)
        getter = None
        errors.append(f"GetText: {exc}")
    if getter is not None:
        try:
            candidates.append(getter())
        except Exception as exc:
            _raise_note_infrastructure_error(exc)
            errors.append(f"GetText: {exc}")
    for attr in ("text", "note_text"):
        try:
            value = getattr(layer, attr, None)
        except Exception as exc:
            _raise_note_infrastructure_error(exc)
            errors.append(f"{attr}: {exc}")
            continue
        if value is not None:
            candidates.append(value)
    return candidates, errors


def _usable_note_text(value: object | None) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _read_note_worksheet_text(origin: object, layer: object) -> tuple[str | None, list[str]]:
    errors: list[str] = []
    try:
        worksheet = origin.WSheet(layer)
        column_count = _column_count(worksheet)
    except Exception as exc:
        _raise_note_infrastructure_error(exc)
        return None, [f"worksheet: {exc}"]
    candidates: list[str] = []
    for index in range(column_count):
        try:
            values = worksheet.to_list(index)
        except Exception as exc:
            _raise_note_infrastructure_error(exc)
            errors.append(f"to_list({index}): {exc}")
            continue
        for value in values:
            if value is None:
                continue
            text = str(value)
            if text:
                candidates.append(text)
                if text.startswith("[EXP_FD_FILE]"):
                    return text, errors
    return (candidates[0] if candidates else None), errors


def _raise_note_infrastructure_error(exc: Exception) -> None:
    if _is_origin_session_infrastructure_error(exc):
        raise InfrastructureExtractionError(f"Origin note session failed: {exc}") from exc

def _read_worksheet_data(origin: object, layer: object) -> WorksheetData:
    worksheet = origin.WSheet(layer)
    columns = []
    for index in range(_column_count(worksheet)):
        column_obj = worksheet.obj[index] if hasattr(worksheet, "obj") else None
        name = _column_object_name(column_obj) or _column_letter(index + 1)
        long_name = worksheet.get_label(index, "L") or _column_object_long_name(column_obj) or name
        values = [_normalize_origin_value(value) for value in worksheet.to_list(index)]
        columns.append(
            Column(
                name=name,
                long_name=long_name,
                values=values,
                designation=_origin_designation(worksheet, index, column_obj),
            )
        )
    return WorksheetData(columns)


def _unique_s1_evidence(
    data: WorksheetData,
) -> tuple[tuple[object, ...] | None, tuple[object, ...] | None]:
    matches = data.matching_columns("S1")
    if len(matches) != 1:
        return None, None
    pair = _try_selected_pair(data, "S1")
    x_values = None if pair is None else tuple(pair.x_column.values)
    return x_values, tuple(matches[0].values)


def _normalize_origin_value(value: object) -> object | None:
    try:
        return None if math.isnan(value) else value
    except (TypeError, ValueError):
        return value


_ORIGIN_SESSION_FAILURE_HRESULTS = {
    0x80010001,  # RPC_E_CALL_REJECTED
    0x80010007,  # RPC_E_SERVER_DIED
    0x80010108,  # RPC_E_DISCONNECTED
    0x8001010A,  # RPC_E_SERVERCALL_RETRYLATER
    0x800401FD,  # CO_E_OBJNOTCONNECTED
    0x800706BA,  # RPC_S_SERVER_UNAVAILABLE
}


def _is_origin_session_infrastructure_error(exc: Exception) -> bool:
    values = [getattr(exc, "hresult", None)]
    if getattr(exc, "args", None):
        values.append(exc.args[0])
    return any(
        isinstance(value, int) and (value & 0xFFFFFFFF) in _ORIGIN_SESSION_FAILURE_HRESULTS
        for value in values
    )


def _try_selected_pair(data: WorksheetData, selected_y: str):
    try:
        return select_xy_pair(data, selected_y)
    except DataColumnError:
        return None


def _data_checksum(data: WorksheetData) -> str:
    payload = [
        {
            "name": column.name,
            "long_name": column.long_name,
            "designation": column.designation,
            "values": column.values,
        }
        for column in data.columns
    ]
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _column_object_name(column_obj: object | None) -> str | None:
    if column_obj is None:
        return None
    getter = getattr(column_obj, "GetName", None)
    if getter is not None:
        value = getter()
        if value:
            return str(value)
    value = getattr(column_obj, "short_name", None)
    return str(value) if value else None


def _column_object_long_name(column_obj: object | None) -> str | None:
    if column_obj is None:
        return None
    getter = getattr(column_obj, "GetLongName", None)
    if getter is not None:
        value = getter()
        if value:
            return str(value)
    value = getattr(column_obj, "long_name", None)
    return str(value) if value else None

def _book_page_bases(origin: object):
    page_order = 0
    book_types = {origin.po.OPT_WORKSHEET}
    matrix_type = getattr(origin.po, "OPT_MATRIX", None)
    if matrix_type is not None:
        book_types.add(matrix_type)
    for folder in _walk_folders(origin.po.RootFolder):
        for page_base in list(folder.PageBases()):
            if page_base.GetType() not in book_types:
                continue
            page_order += 1
            yield page_order, folder, page_base


def _book_page_type(origin: object, page_base: object) -> str:
    page_type = page_base.GetType()
    if page_type == origin.po.OPT_WORKSHEET:
        return "worksheet"
    if page_type == getattr(origin.po, "OPT_MATRIX", None):
        return "matrix"
    return f"origin-{page_type}"


def _sheet_name(layer: object) -> str:
    getter = getattr(layer, "GetLongName", None)
    if getter is not None:
        value = getter()
        if value:
            return value
    getter = getattr(layer, "GetName", None)
    if getter is not None:
        return getter()
    return getattr(layer, "lname", None) or getattr(layer, "name", "")
def _read_book_contract(page_base: object, worksheet: object) -> BookWriteContract:
    columns = []
    for index in range(_column_count(worksheet)):
        column_obj = worksheet.obj[index] if hasattr(worksheet, "obj") else None
        formula = _column_str_property(column_obj, "formula")
        columns.append(
            ColumnWriteContract(
                short_name=_column_letter(index + 1),
                designation=_designation(worksheet, index, column_obj),
                comment=worksheet.get_label(index, "C"),
                values=tuple(_decimal_or_none(value) for value in worksheet.to_list(index)),
                formula=formula or None,
                method=worksheet.get_label(index, "Method") or None,
            )
        )
    return BookWriteContract(page_base.GetLongName(), page_base.GetName(), tuple(columns))


def _rename_empty_default_folder(origin: object, current_path: str, target_name: str) -> str | None:
    project = getattr(origin, "po", None)
    root = getattr(project, "RootFolder", None)
    if root is None or _normalized_pe_path(current_path) != _normalized_pe_path(_folder_contract_path(root)):
        return None
    folders = list(getattr(root, "Folders", ()))
    if any(_default_folder_name(folder) == target_name for folder in folders):
        return None
    for folder in folders:
        if _default_folder_name(folder) != "Folder1" or not _is_empty_folder(folder):
            continue
        rename = getattr(folder, "SetName", None)
        if rename is None:
            return None
        rename(target_name)
        return _folder_contract_path(folder)
    return None


def _is_empty_folder(folder: object) -> bool:
    if list(getattr(folder, "Folders", ())):
        return False
    page_bases = getattr(folder, "PageBases", None)
    return page_bases is None or not list(page_bases())


def _normalized_pe_path(path: str) -> str:
    return str(path or "/").strip("/")


def _default_folder_name(folder: object) -> str:
    getter = getattr(folder, "GetName", None)
    if getter is not None:
        return getter()
    return getattr(folder, "name", "")


def _set_label_row_heights(sheet: object) -> None:
    lt_exec = getattr(sheet, "lt_exec", None)
    if lt_exec is not None:
        lt_exec(f"wrowheight [C:C] {COMMENTS_LABEL_ROW_HEIGHT_LINES};")
        lt_exec(f"wrowheight [Method:Method] {METHOD_LABEL_ROW_HEIGHT_LINES};")
        lt_exec(f"wrowheight [O:O] {FORMULA_LABEL_ROW_HEIGHT_LINES};")


def _set_formula_recalculation_auto(sheet: object, index: int) -> None:
    try:
        column = sheet.obj[index]
        column.SetNumProp("svrm", 1)
    except (AttributeError, IndexError, TypeError) as exc:
        raise RuntimeError(
            "Origin column does not expose the formula recalculation mode"
        ) from exc


def _folder_handle_path(path: str) -> str:
    text = str(path)
    return "/" if text == "/" else text.rstrip("/")


def _folder_parts(folder_path: str) -> tuple[str, ...]:
    return tuple(part for part in folder_path.split("/") if part)


def _folder_contract_path(folder: object) -> str:
    path_getter = getattr(folder, "GetPEPath", None)
    if path_getter is None:
        return getattr(folder, "path", "/")
    return path_getter() or "/"


def _relative_project_folder_path(folder: object, root_path: str) -> str:
    folder_path = str(_folder_contract_path(folder)).strip("/")
    root = str(root_path or "/").strip("/")
    if not folder_path or folder_path == root:
        return "/"
    if root and folder_path.startswith(root + "/"):
        folder_path = folder_path[len(root) + 1:]
    return folder_path or "/"


def _column_index(short_name: str) -> int:
    total = 0
    for char in short_name.upper():
        total = total * 26 + ord(char) - ord("A") + 1
    return total - 1


def _origin_value(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


def _decimal_or_none(value: object) -> Decimal | None:
    if value is None:
        return None
    decimal_value = Decimal(str(value))
    return None if decimal_value.is_nan() else decimal_value


def _same_dependency_value(left: object, right: object) -> bool:
    return left == right or (
        isinstance(left, float)
        and isinstance(right, float)
        and math.isnan(left)
        and math.isnan(right)
    )


def _same_dependency_values(left: list[object], right: list[object]) -> bool:
    return len(left) == len(right) and all(
        _same_dependency_value(old, new)
        for old, new in zip(left, right)
    )


def _column_count(worksheet: object) -> int:
    if hasattr(worksheet, "cols"):
        return int(worksheet.cols)
    if hasattr(worksheet, "obj"):
        return len(list(worksheet.obj))
    raise RuntimeError("Unable to determine Origin worksheet column count")


def _designation(worksheet: object, index: int, column_obj: object | None = None) -> str:
    return _origin_designation(worksheet, index, column_obj) or "Y"


def _origin_designation(
    worksheet: object,
    index: int,
    column_obj: object | None = None,
) -> str | None:
    if column_obj is not None:
        try:
            origin_type = column_obj.GetType()
        except Exception as exc:
            if _is_origin_session_infrastructure_error(exc):
                raise InfrastructureExtractionError(
                    f"Origin data session failed: {exc}"
                ) from exc
            origin_type = None
        if str(origin_type) == "3":
            return "X"
        if str(origin_type) == "0":
            return "Y"
    axis = worksheet.get_label(index, "G")
    return axis if axis in {"X", "Y"} else None


def _column_str_property(column_obj: object | None, prop: str) -> str | None:
    if column_obj is None:
        return None
    try:
        return column_obj.GetStrProp(prop)
    except Exception:
        return None


def _find_sheet_by_folder_and_book_long_name(origin: object, folder_path: str, book_display_name: str) -> object:
    for folder in _walk_folders(origin.po.RootFolder):
        if not _folder_path_matches(_folder_contract_path(folder), folder_path):
            continue
        for page_base in list(folder.PageBases()):
            if page_base.GetType() == origin.po.OPT_WORKSHEET and page_base.GetLongName() == book_display_name:
                page = origin.po.Pages(page_base.GetName())
                return origin.WSheet(list(page.Layers)[0])
    raise RuntimeError(f"Book not found in mutation copy: {folder_path}/{book_display_name}")


def _folder_path_matches(actual_path: str, expected_path: str) -> bool:
    actual_parts = _folder_parts(actual_path)
    expected_parts = _folder_parts(expected_path)
    if not expected_parts:
        return not actual_parts
    if len(actual_parts) < len(expected_parts):
        return False
    return actual_parts[-len(expected_parts):] == expected_parts


def _walk_folders(root: object):
    yield root
    for child in list(root.Folders):
        yield from _walk_folders(child)
