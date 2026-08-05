from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
from typing import Iterable, Mapping

from spectrum_organizer.core.metadata_numeric import parse_metadata_decimal
from spectrum_organizer.core.note_parser import NoteParseError, ParsedNote, parse_book_note
from spectrum_organizer.domain.extracted import TerminalBookResult
from spectrum_organizer.domain.models import SpectrumClass


class CandidateConversionError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReviewCandidate:
    source_id: str
    source_filename: str
    page_type: str
    folder_path: str
    short_name: str
    display_name: str
    page_order: int | None
    spectrum_class: SpectrumClass
    role: str
    fixed_wavelength: str | None
    wavelength_range: tuple[str, str] | None
    scan_increment: str | None
    excitation_range: tuple[str, str] | None
    emission_range: tuple[str, str] | None
    excitation_increment: str | None
    emission_increment: str | None
    excitation_slits: tuple[str, str] | None
    emission_slits: tuple[str, str] | None
    flash_delay: str | None
    sample_window: str | None
    time_per_flash: str | None
    flash_count: str | None
    selected_y_column: str | None
    paired_x_column: str | None
    x_values: tuple[object, ...]
    y_values: tuple[object, ...]
    max_y: object | None
    x_at_max_y: object | None
    note_datetime: str | None
    payload_snapshot_path: Path | None = None
    payload_checksum: str | None = None

    @property
    def book_key(self) -> str:
        return _candidate_book_key(self.source_id, self.page_type, self.folder_path, self.short_name)


@dataclass(frozen=True)
class CandidateRejection:
    source_id: str
    source_filename: str
    page_type: str
    folder_path: str
    short_name: str
    display_name: str
    reason: str
    page_order: int | None = None
    spectrum_class: SpectrumClass | None = None
    selected_y_column: str | None = None
    paired_x_column: str | None = None
    s1_max: object | None = None
    x_at_s1_max: object | None = None
    max_y: object | None = None
    x_at_max_y: object | None = None
    payload_snapshot_path: Path | None = None
    payload_checksum: str | None = None

    @property
    def book_key(self) -> str:
        return _candidate_book_key(self.source_id, self.page_type, self.folder_path, self.short_name)


@dataclass(frozen=True)
class CandidateConversionResult:
    ordinary_candidates: tuple[ReviewCandidate, ...]
    steady_2d_candidates: tuple[ReviewCandidate, ...]
    rejections: tuple[CandidateRejection, ...]


def convert_extracted_results(
    results: Iterable[TerminalBookResult],
    *,
    source_filenames: Mapping[str, str],
    expected_source_ids: tuple[str, ...],
    cancel_check=None,
) -> CandidateConversionResult:
    ordinary: list[ReviewCandidate] = []
    steady_2d: list[ReviewCandidate] = []
    rejections: list[CandidateRejection] = []
    recognizable = {source_id: 0 for source_id in expected_source_ids}
    expected = set(expected_source_ids)
    try:
        missing_filenames = [source_id for source_id in expected_source_ids if source_id not in source_filenames]
        if missing_filenames:
            raise CandidateConversionError(
                f"Missing source filename provenance: {', '.join(missing_filenames)}"
            )
        for result in results:
            if cancel_check is not None:
                cancel_check()
            if result.source_id not in expected:
                raise CandidateConversionError(f"Snapshot contains unexpected source: {result.source_id}")
            source_filename = source_filenames[result.source_id]
            try:
                parsed = parse_book_note(result.note_text or "")
            except NoteParseError as exc:
                rejections.append(_candidate_rejection(result, source_filename, result.rejection_reason or str(exc), None))
                continue
            recognizable[result.source_id] += 1
            stored_class = _stored_spectrum_class(result)
            if stored_class is not None and stored_class != parsed.spectrum_class:
                rejections.append(_candidate_rejection(result, source_filename, "stored spectrum class does not match Note", parsed.spectrum_class))
                continue
            if result.status != "extracted":
                rejections.append(_candidate_rejection(result, source_filename, result.rejection_reason or result.status, parsed.spectrum_class))
                continue
            metadata_error = required_note_metadata_error(parsed)
            if metadata_error is not None:
                rejections.append(_candidate_rejection(result, source_filename, metadata_error, parsed.spectrum_class))
                continue
            candidate = _review_candidate(result, source_filename, parsed)
            if candidate.spectrum_class == SpectrumClass.STEADY_2D:
                steady_2d.append(candidate)
            else:
                ordinary.append(candidate)
    finally:
        close = getattr(results, "close", None)
        if callable(close):
            close()
    missing = [source_id for source_id, count in recognizable.items() if count == 0]
    if missing:
        raise CandidateConversionError(f"Selected source has zero recognizable supported Books: {', '.join(missing)}")
    return CandidateConversionResult(tuple(ordinary), tuple(steady_2d), tuple(rejections))


def _review_candidate(result: TerminalBookResult, source_filename: str, note: ParsedNote) -> ReviewCandidate:
    delayed = note.delay
    is_excitation = note.spectrum_class in {SpectrumClass.STEADY_EXCITATION, SpectrumClass.DELAYED_EXCITATION}
    is_two_dimensional = note.spectrum_class == SpectrumClass.STEADY_2D
    if is_two_dimensional:
        role = "two_dimensional"
    elif is_excitation:
        role = "excitation"
    else:
        role = "emission"
    fixed_wavelength = note.fixed_emission_wavelength if is_excitation else note.fixed_excitation_wavelength
    wavelength_range = note.excitation_range if is_excitation else note.emission_range
    scan_increment = note.excitation_increment if is_excitation else note.emission_increment
    return ReviewCandidate(
        source_id=result.source_id,
        source_filename=source_filename,
        page_type=result.page_type,
        folder_path=result.folder_path,
        short_name=result.short_name,
        display_name=result.display_name or "",
        page_order=result.page_order,
        spectrum_class=note.spectrum_class,
        role=role,
        fixed_wavelength=fixed_wavelength,
        wavelength_range=wavelength_range,
        scan_increment=scan_increment,
        excitation_range=note.excitation_range,
        emission_range=note.emission_range,
        excitation_increment=note.excitation_increment,
        emission_increment=note.emission_increment,
        excitation_slits=note.excitation_slits,
        emission_slits=note.emission_slits,
        flash_delay=None if delayed is None else delayed.flash_delay,
        sample_window=None if delayed is None else delayed.sample_window,
        time_per_flash=None if delayed is None else delayed.time_per_flash,
        flash_count=None if delayed is None else delayed.flash_count,
        selected_y_column=result.selected_y_column,
        paired_x_column=result.paired_x_column,
        x_values=result.selected_x_values,
        y_values=result.selected_y_values,
        max_y=result.max_planned_y,
        x_at_max_y=result.max_planned_y_x,
        note_datetime=note.note_datetime,
        payload_snapshot_path=result.payload_snapshot_path,
        payload_checksum=result.payload_checksum,
    )


def _candidate_rejection(
    result: TerminalBookResult,
    source_filename: str,
    reason: str,
    spectrum_class: SpectrumClass | None,
) -> CandidateRejection:
    return CandidateRejection(
        source_id=result.source_id,
        source_filename=source_filename,
        page_type=result.page_type,
        folder_path=result.folder_path,
        short_name=result.short_name,
        display_name=result.display_name or "",
        reason=reason,
        page_order=result.page_order,
        spectrum_class=spectrum_class,
        selected_y_column=result.selected_y_column,
        paired_x_column=result.paired_x_column,
        s1_max=result.s1_max_for_limit,
        x_at_s1_max=result.s1_max_for_limit_x,
        max_y=result.max_planned_y,
        x_at_max_y=result.max_planned_y_x,
        payload_snapshot_path=result.payload_snapshot_path,
        payload_checksum=result.payload_checksum,
    )


def required_note_metadata_error(note: ParsedNote) -> str | None:
    if note.spectrum_class == SpectrumClass.STEADY_2D:
        if note.excitation_range is None or note.emission_range is None:
            return "Note is missing excitation or emission scan range"
        if note.excitation_increment is None or note.emission_increment is None:
            return "Note is missing excitation or emission scan increment"
        numeric_fields = (
            ("excitation scan range start", note.excitation_range[0]),
            ("excitation scan range end", note.excitation_range[1]),
            ("emission scan range start", note.emission_range[0]),
            ("emission scan range end", note.emission_range[1]),
            ("excitation scan increment", note.excitation_increment),
            ("emission scan increment", note.emission_increment),
        )
    elif note.spectrum_class in {SpectrumClass.STEADY_EXCITATION, SpectrumClass.DELAYED_EXCITATION}:
        if note.fixed_emission_wavelength is None:
            return "Note is missing fixed emission wavelength"
        if note.excitation_range is None:
            return "Note is missing excitation scan range"
        if note.excitation_increment is None:
            return "Note is missing excitation scan increment"
        numeric_fields = (
            ("fixed emission wavelength", note.fixed_emission_wavelength),
            ("excitation scan range start", note.excitation_range[0]),
            ("excitation scan range end", note.excitation_range[1]),
            ("excitation scan increment", note.excitation_increment),
        )
    else:
        if note.fixed_excitation_wavelength is None:
            return "Note is missing fixed excitation wavelength"
        if note.emission_range is None:
            return "Note is missing emission scan range"
        if note.emission_increment is None:
            return "Note is missing emission scan increment"
        numeric_fields = (
            ("fixed excitation wavelength", note.fixed_excitation_wavelength),
            ("emission scan range start", note.emission_range[0]),
            ("emission scan range end", note.emission_range[1]),
            ("emission scan increment", note.emission_increment),
        )
    if note.spectrum_class in {SpectrumClass.DELAYED_EMISSION, SpectrumClass.DELAYED_EXCITATION}:
        if note.delay is None:
            return "Note is missing delayed acquisition parameters"
        numeric_fields += (
            ("Flash Delay", note.delay.flash_delay),
            ("Sample Window", note.delay.sample_window),
            ("Time per Flash", note.delay.time_per_flash),
            ("Flash Count", note.delay.flash_count),
        )
    for label, value in numeric_fields:
        if not _is_finite_decimal(value):
            return f"Note has invalid numeric {label}: {value}"
    if note.excitation_slits is None:
        return "Note is missing excitation slits"
    if note.emission_slits is None:
        return "Note is missing emission slits"
    slit_fields = (
        ("excitation front entrance slit", note.excitation_slits[0]),
        ("excitation front exit slit", note.excitation_slits[1]),
        ("emission front entrance slit", note.emission_slits[0]),
        ("emission front exit slit", note.emission_slits[1]),
    )
    for label, value in slit_fields:
        if not _is_finite_decimal(value, nonnegative=True):
            return f"Note has invalid numeric {label}: {value}"
    return None


def _is_finite_decimal(
    value: str,
    *,
    nonnegative: bool = False,
) -> bool:
    try:
        parse_metadata_decimal(
            value,
            nonnegative=nonnegative,
        )
    except ValueError:
        return False
    return True


def build_review_candidate_display(candidate: ReviewCandidate) -> tuple[tuple[str, str], ...]:
    rows = [
        ("source_filename", candidate.source_filename),
        ("folder_path", candidate.folder_path),
        (
            "book_name",
            _visible_candidate_name(candidate.display_name, candidate.short_name),
        ),
        ("spectrum_type", candidate.spectrum_class.value),
        ("role", candidate.role),
        ("fixed_wavelength", candidate.fixed_wavelength or ""),
        ("wavelength_range", _pair_text(candidate.wavelength_range)),
        ("scan_increment", candidate.scan_increment or ""),
        ("slits", _review_slit_display(candidate)),
        ("delay_parameters", _review_delay_display(candidate)),
        ("x_at_max_y", format_maximum_x(candidate.x_at_max_y)),
        ("max_y", "" if candidate.max_y is None else str(candidate.max_y)),
        ("note_datetime", candidate.note_datetime or ""),
    ]
    if candidate.spectrum_class == SpectrumClass.STEADY_2D:
        insert_at = 9
        rows[insert_at:insert_at] = [
            ("excitation_range", _pair_text(candidate.excitation_range)),
            ("excitation_increment", candidate.excitation_increment or ""),
            ("emission_range", _pair_text(candidate.emission_range)),
            ("emission_increment", candidate.emission_increment or ""),
        ]
    return tuple(rows)


def _candidate_book_key(source_id: str, page_type: str, folder_path: str, short_name: str) -> str:
    return json.dumps(
        [source_id, page_type, folder_path, short_name],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def format_maximum_x(value: object | None) -> str:
    if value is None:
        return ""
    if not isinstance(value, (list, tuple)):
        return str(value)
    ordered = sorted(value)
    if len(ordered) <= 3:
        return ", ".join(str(item) for item in ordered)
    return f"{ordered[0]} - {ordered[-1]}（{len(ordered)} 个并列最大值）"


def _pair_text(values: tuple[str, str] | None) -> str:
    return "" if values is None else f"{values[0]} - {values[1]}"


def _review_slit_display(candidate: ReviewCandidate) -> str:
    parts = []
    if candidate.excitation_slits is not None:
        parts.append(f"EX {candidate.excitation_slits[0]}/{candidate.excitation_slits[1]}")
    if candidate.emission_slits is not None:
        parts.append(f"EM {candidate.emission_slits[0]}/{candidate.emission_slits[1]}")
    return "; ".join(parts)


def _review_delay_display(candidate: ReviewCandidate) -> str:
    if candidate.flash_delay is None:
        return ""
    return "; ".join(
        (
            f"Flash Delay {candidate.flash_delay} ms",
            f"Sample Window {candidate.sample_window or ''} ms",
            f"Time per Flash {candidate.time_per_flash or ''} ms",
            f"Flash Count {candidate.flash_count or ''}",
        )
    )


def _stored_spectrum_class(result: TerminalBookResult) -> SpectrumClass | None:
    if result.spectrum_class is None:
        return None
    try:
        return SpectrumClass(result.spectrum_class)
    except ValueError as exc:
        raise CandidateConversionError(
            f"Snapshot contains invalid spectrum class for {result.identity}: {result.spectrum_class}"
        ) from exc


@dataclass(frozen=True)
class SelectionSpectrum:
    source_id: str
    source_filename: str
    folder_path: str
    book_name: str
    display_name: str
    default_name: str
    spectrum_class: SpectrumClass
    sample_system: str
    temperature: str
    page_type: str = "worksheet"
    fixed_excitation_wavelength: str | None = None
    fixed_receiving_wavelength: str | None = None
    excitation_slit: str | None = None
    emission_slit: str | None = None
    flash_delay: str | None = None
    sample_window: str | None = None
    time_per_flash: str | None = None
    flash_count: str | None = None
    scan_start: str | None = None
    scan_stop: str | None = None
    scan_step: str | None = None
    x_at_max_y: str | None = None
    max_y: str | None = None
    note_datetime: str | None = None

    @property
    def book_key(self) -> str:
        return json.dumps(
            [self.source_id, self.page_type, self.folder_path, self.book_name],
            ensure_ascii=False,
            separators=(",", ":"),
        )


@dataclass(frozen=True)
class ReviewRequest:
    kind: str
    review_key: str
    book_keys: tuple[str, ...]
    actions: tuple[str, ...]
    stage: str | None = None
    allow_return_to_attribution: bool = False
    comparison_fields: tuple[str, ...] = ()
    single_select_groups: tuple[tuple[str, ...], ...] = ()


@dataclass(frozen=True)
class SelectionExclusion:
    book_key: str
    reason: str


@dataclass(frozen=True)
class SelectionResult:
    selected_book_keys: tuple[str, ...]
    pending_reviews: tuple[ReviewRequest, ...] = ()
    exclusions: tuple[SelectionExclusion, ...] = ()
    completeness_book_keys: tuple[str, ...] = ()


def review_emission_duplicates(
    spectra: list[SelectionSpectrum],
    *,
    choices: Mapping[str, str] | None = None,
) -> SelectionResult:
    choices = choices or {}
    candidates = [spectrum for spectrum in spectra if spectrum.spectrum_class in {SpectrumClass.STEADY_EMISSION, SpectrumClass.DELAYED_EMISSION}]
    pending: list[ReviewRequest] = []
    exclusions: list[SelectionExclusion] = []
    stage1_survivors = _resolve_duplicate_stage(
        candidates,
        choices,
        pending,
        exclusions,
        stage="stage1",
        scope_key=lambda spectrum: (spectrum.source_id, spectrum.folder_path),
        allow_return_to_attribution=False,
    )
    if pending:
        return SelectionResult((), tuple(pending), (), ())

    stage2_survivors = _resolve_duplicate_stage(
        stage1_survivors,
        choices,
        pending,
        exclusions,
        stage="stage2",
        scope_key=lambda _spectrum: (),
        allow_return_to_attribution=True,
    )
    if pending:
        return SelectionResult((), tuple(pending), (), ())
    selected = tuple(spectrum.book_key for spectrum in stage2_survivors)
    return SelectionResult(selected, (), tuple(exclusions), selected)


def select_excitation_candidates(
    spectra: list[SelectionSpectrum],
    *,
    choices: Mapping[str, object] | None = None,
) -> SelectionResult:
    choices = choices or {}
    candidates = [spectrum for spectrum in spectra if spectrum.spectrum_class in {SpectrumClass.STEADY_EXCITATION, SpectrumClass.DELAYED_EXCITATION}]
    pending: list[ReviewRequest] = []
    exclusions: list[SelectionExclusion] = []
    selected: list[SelectionSpectrum] = []

    for group in _groups(candidates, _excitation_group_key).values():
        if len(group) == 1:
            selected.extend(group)
            continue
        single_select_groups = tuple(
            tuple(spectrum.book_key for spectrum in exact_group)
            for exact_group in _groups(group, _exact_excitation_key).values()
            if len(exact_group) > 1
        )
        review = _review(
            "excitation_selection",
            None,
            group,
            ("select_one_or_more",),
            _excitation_comparison_fields(),
            single_select_groups=single_select_groups,
        )
        choice = choices.get(review.review_key)
        selected_keys = _choice_tuple(choice)
        valid_keys = {spectrum.book_key for spectrum in group}
        if (
            not selected_keys
            or len(selected_keys) != len(set(selected_keys))
            or any(key not in valid_keys for key in selected_keys)
            or any(
                sum(key in selected_keys for key in exact_group) > 1
                for exact_group in single_select_groups
            )
        ):
            pending.append(review)
            continue
        spectrum_by_key = {
            spectrum.book_key: spectrum
            for spectrum in group
        }
        selected.extend(
            spectrum_by_key[key]
            for key in selected_keys
        )
        exact_duplicate_keys = {
            key for exact_group in single_select_groups for key in exact_group
        }
        exclusions.extend(
            SelectionExclusion(
                spectrum.book_key,
                (
                    "exact_excitation_duplicate_unselected"
                    if spectrum.book_key in exact_duplicate_keys
                    else "excitation_candidate_unselected"
                ),
            )
            for spectrum in group
            if spectrum.book_key not in selected_keys
        )

    selected_keys = tuple(spectrum.book_key for spectrum in selected)
    if pending:
        return SelectionResult((), tuple(pending), (), ())
    return SelectionResult(selected_keys, (), tuple(exclusions), selected_keys)


def filter_copyable_emissions_after_special(
    spectra: list[SelectionSpectrum],
    *,
    regular_delayed_book_keys: tuple[str, ...],
    special_group_book_keys: tuple[str, ...],
) -> tuple[SelectionSpectrum, ...]:
    regular_delayed = set(regular_delayed_book_keys)
    special_grouped = set(special_group_book_keys)
    return tuple(
        spectrum
        for spectrum in spectra
        if spectrum.spectrum_class == SpectrumClass.STEADY_EMISSION
        or (
            spectrum.spectrum_class == SpectrumClass.DELAYED_EMISSION
            and spectrum.book_key in regular_delayed
            and spectrum.book_key not in special_grouped
        )
    )


def build_candidate_display(candidate: SelectionSpectrum) -> tuple[tuple[str, str], ...]:
    return (
        ("source_filename", candidate.source_filename),
        ("folder_path", candidate.folder_path),
        (
            "book_name",
            _visible_candidate_name(candidate.display_name, candidate.default_name),
        ),
        ("spectrum_type", candidate.spectrum_class.value),
        ("slits", _slit_display(candidate)),
        ("delay_parameters", _delay_display(candidate)),
        ("x_at_max_y", candidate.x_at_max_y or ""),
        ("max_y", candidate.max_y or ""),
        ("note_datetime", candidate.note_datetime or ""),
    )


def _visible_candidate_name(display_name: str, default_name: str) -> str:
    display_text = str(display_name or "")
    if display_text.strip():
        return display_text
    default_text = str(default_name or "")
    return default_text if default_text.strip() else "未命名 Book"


def _resolve_duplicate_stage(
    spectra: list[SelectionSpectrum],
    choices: Mapping[str, str],
    pending: list[ReviewRequest],
    exclusions: list[SelectionExclusion],
    *,
    stage: str,
    scope_key,
    allow_return_to_attribution: bool,
) -> list[SelectionSpectrum]:
    survivors: list[SelectionSpectrum] = []
    for group in _groups(spectra, lambda spectrum: (scope_key(spectrum), spectrum.sample_system, spectrum.temperature, spectrum.spectrum_class, _emission_identity(spectrum))).values():
        if len(group) == 1:
            survivors.extend(group)
            continue
        actions = ("select_one", "返回样品归属步骤") if allow_return_to_attribution else ("select_one",)
        review = _review(
            "emission_duplicate",
            stage,
            group,
            actions,
            (),
            allow_return_to_attribution=allow_return_to_attribution,
        )
        choice = choices.get(review.review_key)
        if choice not in {spectrum.book_key for spectrum in group}:
            pending.append(review)
            continue
        chosen = _by_key(group, choice)
        survivors.append(chosen)
        exclusions.extend(
            SelectionExclusion(spectrum.book_key, "emission_duplicate_unselected")
            for spectrum in group
            if spectrum.book_key != chosen.book_key
        )
    return survivors


def _emission_identity(spectrum: SelectionSpectrum) -> tuple[object, ...]:
    identity = (
        _numeric_identity(spectrum.fixed_excitation_wavelength),
        _numeric_identity(spectrum.excitation_slit),
        _numeric_identity(spectrum.emission_slit),
    )
    if spectrum.spectrum_class == SpectrumClass.DELAYED_EMISSION:
        identity += (
            _numeric_identity(spectrum.flash_delay),
            _numeric_identity(spectrum.sample_window),
            _numeric_identity(spectrum.time_per_flash),
            _numeric_identity(spectrum.flash_count),
        )
    return identity


def _excitation_group_key(spectrum: SelectionSpectrum) -> tuple[object, ...]:
    key = (spectrum.spectrum_class, spectrum.sample_system, spectrum.temperature)
    if spectrum.spectrum_class == SpectrumClass.DELAYED_EXCITATION:
        key += (
            _numeric_identity(spectrum.flash_delay),
            _numeric_identity(spectrum.sample_window),
            _numeric_identity(spectrum.time_per_flash),
        )
    return key


def _exact_excitation_key(spectrum: SelectionSpectrum) -> tuple[object, ...]:
    key = (
        spectrum.spectrum_class,
        spectrum.sample_system,
        spectrum.temperature,
        _numeric_identity(spectrum.scan_start),
        _numeric_identity(spectrum.scan_stop),
        _numeric_identity(spectrum.scan_step),
        _numeric_identity(spectrum.fixed_receiving_wavelength),
        _numeric_identity(spectrum.excitation_slit),
        _numeric_identity(spectrum.emission_slit),
    )
    if spectrum.spectrum_class == SpectrumClass.DELAYED_EXCITATION:
        key += (
            _numeric_identity(spectrum.flash_delay),
            _numeric_identity(spectrum.sample_window),
            _numeric_identity(spectrum.time_per_flash),
            _numeric_identity(spectrum.flash_count),
        )
    return key


def _numeric_identity(value: object | None) -> object | None:
    if value is None:
        return None
    text = str(value).strip()
    if "/" in text:
        return tuple(_numeric_identity(part) for part in text.split("/"))
    try:
        number = Decimal(text)
    except (InvalidOperation, ValueError):
        return text
    return number if number.is_finite() else text


def _review(
    kind: str,
    stage: str | None,
    group: list[SelectionSpectrum],
    actions: tuple[str, ...],
    comparison_fields: tuple[str, ...],
    *,
    allow_return_to_attribution: bool = False,
    single_select_groups: tuple[tuple[str, ...], ...] = (),
) -> ReviewRequest:
    keys = tuple(spectrum.book_key for spectrum in group)
    review_key = json.dumps([kind, stage, list(keys)], ensure_ascii=False, separators=(",", ":"))
    return ReviewRequest(
        kind,
        review_key,
        keys,
        actions,
        stage=stage,
        allow_return_to_attribution=allow_return_to_attribution,
        comparison_fields=comparison_fields,
        single_select_groups=single_select_groups,
    )


def _excitation_comparison_fields() -> tuple[str, ...]:
    return (
        "fixed_receiving_wavelength",
        "excitation_slit",
        "emission_slit",
        "flash_count",
        "scan_start",
        "scan_stop",
        "scan_step",
        "flash_delay",
        "sample_window",
        "time_per_flash",
    )


def _groups(spectra: list[SelectionSpectrum], key_for) -> dict[tuple[object, ...], list[SelectionSpectrum]]:
    groups: dict[tuple[object, ...], list[SelectionSpectrum]] = {}
    for spectrum in spectra:
        groups.setdefault(key_for(spectrum), []).append(spectrum)
    return groups


def _by_key(spectra: list[SelectionSpectrum], book_key: str) -> SelectionSpectrum:
    for spectrum in spectra:
        if spectrum.book_key == book_key:
            return spectrum
    raise KeyError(book_key)


def _choice_tuple(choice: object) -> tuple[str, ...]:
    if isinstance(choice, str):
        return (choice,)
    if isinstance(choice, tuple) and all(isinstance(item, str) for item in choice):
        return choice
    if isinstance(choice, list) and all(isinstance(item, str) for item in choice):
        return tuple(choice)
    return ()


def _slit_display(candidate: SelectionSpectrum) -> str:
    return f"Ex {candidate.excitation_slit or ''} / Em {candidate.emission_slit or ''}"


def _delay_display(candidate: SelectionSpectrum) -> str:
    return "; ".join(
        (
            f"Flash Delay {candidate.flash_delay or ''}",
            f"Sample Window {candidate.sample_window or ''}",
            f"Time per Flash {candidate.time_per_flash or ''}",
            f"Flash Count {candidate.flash_count or ''}",
        )
    )
