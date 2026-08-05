from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Mapping

from spectrum_organizer.core.attribution import (
    AttributionBook,
    AttributionFields,
    AttributionSession,
    build_attribution_targets,
    commit_final_attributions,
)
from spectrum_organizer.core.output_model import OutputPlan, OutputSpectrum, build_output_plan
from spectrum_organizer.core.selection import (
    SelectionExclusion,
    SelectionSpectrum,
    filter_copyable_emissions_after_special,
    review_emission_duplicates,
    select_excitation_candidates,
)
from spectrum_organizer.core.special_groups import SpectrumBook, classify_special_groups
from spectrum_organizer.core.validity import validate_spectrum_data
from spectrum_organizer.domain.models import SpectrumClass
from spectrum_organizer.core.data_columns import WorksheetData, select_xy_pair
from spectrum_organizer.reporting.publication import CompletionSummary, create_run_staging, publish_completed_run
from spectrum_organizer.safety.identity_paths import file_sha256, path_identity
from spectrum_organizer.reporting.run_report import (
    ReportData,
    ReportItem,
    SampleAttribution,
    SpecialGroupSummary,
    build_success_report,
)
from spectrum_organizer.ui.state_machine import Stage, TaskStateMachine


@dataclass(frozen=True)
class DryRunBook:
    source_id: str
    source_filename: str
    folder_path: str
    book_name: str
    display_name: str
    default_name: str
    spectrum_class: SpectrumClass
    data: WorksheetData
    page_type: str = "worksheet"
    fixed_excitation_wavelength: str | None = None
    fixed_receiving_wavelength: str | None = None
    excitation_slit: str | None = "2"
    emission_slit: str | None = "2"
    flash_delay: str | None = None
    sample_window: str | None = None
    time_per_flash: str | None = None
    flash_count: str | None = None
    receiving_range: tuple[str, str] | None = None
    scan_start: str | None = None
    scan_stop: str | None = None
    scan_step: str | None = None
    mixed_folder: bool = False

    @property
    def book_key(self) -> str:
        if self.page_type != "worksheet" or any(
            "|" in part for part in (self.source_id, self.folder_path, self.book_name)
        ):
            return json.dumps(
                (self.source_id, self.page_type, self.folder_path, self.book_name),
                ensure_ascii=False,
                separators=(",", ":"),
            )
        return f"{self.source_id}|{self.folder_path}|{self.book_name}"


@dataclass(frozen=True)
class DryRunResult:
    state: TaskStateMachine
    publication: CompletionSummary
    output_plan: OutputPlan
    copied_spectrum_ids: tuple[str, ...]
    sample_record_ids: dict[str, int]
    attribution_target_count: int


def run_non_origin_dry_run(
    books: tuple[DryRunBook, ...] | list[DryRunBook],
    *,
    attributions: Mapping[str, object],
    library,
    output_parent: Path,
    timestamp: str,
    run_id: str,
    emission_duplicate_choices: Mapping[str, str] | None = None,
    excitation_choices: Mapping[str, object] | None = None,
    ignored_duplicate_input_paths: tuple[Path, ...] = (),
    allow_non_origin_publication: bool = False,
) -> DryRunResult:
    books = tuple(books)
    _require_unique_dry_run_book_keys(books)
    machine = TaskStateMachine()
    _advance(machine, Stage.PREFLIGHT)
    _advance(machine, Stage.SAVE_AND_CLOSE_ORIGIN)
    _advance(machine, Stage.EXTRACTION)

    validation = {
        book.book_key: validate_spectrum_data(book.spectrum_class, book.data, "S1c", 2_000_000)
        for book in books
    }
    valid_books = tuple(book for book in books if validation[book.book_key].ok)

    _advance(machine, Stage.ATTRIBUTION)
    attribution_targets = build_attribution_targets([
        AttributionBook(
            book.source_id,
            book.folder_path,
            book.book_name,
            mixed_folder=book.mixed_folder,
            page_type=book.page_type,
        )
        for book in valid_books
    ])
    assignments = _confirm_attributions(valid_books, attributions, attribution_targets)

    _advance(machine, Stage.SPECIAL_REVIEW)
    special_result = classify_special_groups([_special_book(book, assignments[book.book_key]) for book in valid_books])
    if special_result.pending_duplicate_reviews:
        raise ValueError("dry-run pending special duplicate review")
    if special_result.pending_overlap_assignments:
        raise ValueError("dry-run pending special overlap assignment")
    special_keys = tuple(key for group in special_result.groups for key in group.book_keys)

    spectra = tuple(_selection_spectrum(book, assignments[book.book_key], validation[book.book_key]) for book in valid_books)
    selection_key_by_dry_key = {
        book.book_key: spectrum.book_key
        for book, spectrum in zip(valid_books, spectra, strict=True)
    }
    _require_disjoint_book_key_namespaces(selection_key_by_dry_key)
    dry_key_by_selection_key = {value: key for key, value in selection_key_by_dry_key.items()}
    _advance(machine, Stage.DUPLICATE_REVIEW)
    copyable = filter_copyable_emissions_after_special(
        list(spectra),
        regular_delayed_book_keys=_translate_book_keys(
            special_result.regular_delayed_book_keys,
            selection_key_by_dry_key,
        ),
        special_group_book_keys=_translate_book_keys(special_keys, selection_key_by_dry_key),
    )
    duplicate_result = review_emission_duplicates(
        list(copyable),
        choices=_translate_review_choices(emission_duplicate_choices or {}, selection_key_by_dry_key),
    )
    if duplicate_result.pending_reviews:
        raise ValueError("dry-run emission duplicate choices did not resolve all reviews")

    _advance(machine, Stage.EXCITATION_SELECTION)
    excitation_result = select_excitation_candidates(
        list(spectra),
        choices=_translate_review_choices(excitation_choices or {}, selection_key_by_dry_key),
    )
    if excitation_result.pending_reviews:
        raise ValueError("dry-run excitation choices did not resolve all reviews")

    _advance(machine, Stage.FINAL_ATTRIBUTION_SUMMARY)
    if not allow_non_origin_publication:
        raise ValueError("non-Origin publication requires explicit opt-in")
    _advance(machine, Stage.SAMPLE_RECORD_COMMIT)
    sample_record_ids = _save_distinct_records(library, valid_books, assignments)
    machine.record_sample_commit_success(sample_record_ids)

    selected_selection_keys = duplicate_result.selected_book_keys + excitation_result.selected_book_keys
    selected_keys = tuple(dry_key_by_selection_key[key] for key in selected_selection_keys)
    output_spectra = tuple(
        _output_spectrum(book, assignments[book.book_key])
        for book in valid_books
        if selection_key_by_dry_key[book.book_key] in selected_selection_keys
    )
    output_plan = build_output_plan(output_spectra)
    machine.approved_snapshot = {"book_keys": selected_keys}
    machine.output_model = output_plan

    _advance(machine, Stage.OUTPUT_STAGING)
    targets = create_run_staging(Path(output_parent), timestamp, run_id=run_id)
    targets.staging_project_path.write_text("NON-ORIGIN dry-run placeholder", encoding="utf-8")

    report = build_success_report(
        ReportData(
            output_path=targets.final_run_dir,
            ignored_duplicate_input_paths=ignored_duplicate_input_paths,
            rejections=tuple(
                ReportItem(book.book_key, validation[book.book_key].reason or "invalid")
                for book in books
                if not validation[book.book_key].ok
            ),
            exclusions=_report_exclusions(
                duplicate_result.exclusions + excitation_result.exclusions,
                dry_key_by_selection_key,
            ),
            warnings=(),
            special_groups=tuple(
                SpecialGroupSummary(
                    group.kind,
                    tuple(
                        dry_key_by_selection_key.get(book_key, book_key)
                        for book_key in group.book_keys
                    ),
                )
                for group in special_result.groups
            ),
            final_attributions=tuple(
                SampleAttribution(record.canonical_label, Path("<dry-run>"), "accepted")
                for record in _records_for_commit(valid_books, assignments)
            ),
            output_plan=output_plan,
            input_paths=tuple(
                dict.fromkeys(
                    Path(book.source_filename)
                    for book in books
                )
            ),
            settings=(
                ReportItem("S1 强度上限", "2000000"),
                ReportItem("稳态发射强度列", "S1c"),
                ReportItem("运行模式", "非 Origin dry-run"),
            ),
            manual_selections=tuple(
                ReportItem("保留谱图", book_key)
                for book_key in selected_keys
            ),
            count_reconciliation=(
                ReportItem("识别 Book", str(len(books))),
                ReportItem(
                    "拒绝 Book",
                    str(sum(not item.ok for item in validation.values())),
                ),
                ReportItem(
                    "排除 Book",
                    str(
                        len(duplicate_result.exclusions)
                        + len(excitation_result.exclusions)
                    ),
                ),
                ReportItem("接受普通谱", str(len(output_spectra))),
                ReportItem("输出 Folder", str(len(output_plan.folders))),
                ReportItem(
                    "输出 Book",
                    str(sum(len(folder.books) for folder in output_plan.folders)),
                ),
                ReportItem(
                    "输出列",
                    str(
                        sum(
                            len(book.columns)
                            for folder in output_plan.folders
                            for book in folder.books
                        )
                    ),
                ),
            ),
        )
    )

    _advance(machine, Stage.VERIFICATION)
    _advance(machine, Stage.PUBLICATION)
    publication = publish_completed_run(
        targets,
        report,
        verified_project_identity=path_identity(targets.staging_project_path),
        verified_project_sha256=file_sha256(targets.staging_project_path),
    )
    machine.output_published = True
    _advance(machine, Stage.OUTPUT_INSPECTION)
    _advance(machine, Stage.COMPLETION)

    return DryRunResult(
        state=machine,
        publication=publication,
        output_plan=output_plan,
        copied_spectrum_ids=selected_keys,
        sample_record_ids=sample_record_ids,
        attribution_target_count=len(attribution_targets),
    )


def _require_unique_dry_run_book_keys(books: tuple[DryRunBook, ...]) -> None:
    seen: set[str] = set()
    for book in books:
        if book.book_key in seen:
            raise ValueError(f"ambiguous dry-run book key: {book.book_key}")
        seen.add(book.book_key)


def _advance(machine: TaskStateMachine, stage: Stage) -> None:
    machine.advance_to(stage)


def _confirm_attributions(
    books: tuple[DryRunBook, ...],
    attributions: Mapping[str, object],
    targets,
) -> dict[str, AttributionFields]:
    session = AttributionSession(targets)
    dry_run_book_by_attribution_key = {
        AttributionBook(
            book.source_id,
            book.folder_path,
            book.book_name,
            mixed_folder=book.mixed_folder,
            page_type=book.page_type,
        ).book_key: book
        for book in books
    }
    for target in targets:
        dry_run_keys = tuple(
            dry_run_book_by_attribution_key[key].book_key
            for key in target.book_keys
        )
        if any(key not in attributions for key in dry_run_keys):
            continue
        if target.scope == "folder":
            first_attribution = attributions[dry_run_keys[0]]
            if any(attributions[key] != first_attribution for key in dry_run_keys[1:]):
                raise ValueError(f"conflicting dry-run folder attributions: {target.folder_path}")
        fields = AttributionFields(sample=attributions[dry_run_keys[0]])
        session.confirm(target.book_keys[0], fields, apply_to_remaining_folder=target.scope == "folder")
    return {
        book.book_key: session.assignment_for(attribution_key)
        for attribution_key, book in dry_run_book_by_attribution_key.items()
        if session.assignment_for(attribution_key) is not None
    }


def _special_book(book: DryRunBook, attribution: AttributionFields) -> SpectrumBook:
    return SpectrumBook(
        source_id=book.source_id,
        folder_path=book.folder_path,
        book_name=book.book_name,
        spectrum_class=book.spectrum_class,
        sample_label=attribution.sample.identity_json(),
        page_type=book.page_type,
        fixed_excitation_wavelength=book.fixed_excitation_wavelength,
        receiving_range=book.receiving_range,
        excitation_slit=book.excitation_slit,
        emission_slit=book.emission_slit,
        flash_delay=book.flash_delay,
        sample_window=book.sample_window,
        time_per_flash=book.time_per_flash,
        flash_count=book.flash_count,
    )


def _selection_spectrum(book: DryRunBook, attribution: AttributionFields, validation) -> SelectionSpectrum:
    sample = attribution.sample
    return SelectionSpectrum(
        source_id=book.source_id,
        source_filename=book.source_filename,
        folder_path=book.folder_path,
        book_name=book.book_name,
        display_name=book.display_name,
        default_name=book.default_name,
        spectrum_class=book.spectrum_class,
        sample_system=sample.identity_json(),
        temperature=sample.temperature,
        page_type=book.page_type,
        fixed_excitation_wavelength=book.fixed_excitation_wavelength,
        fixed_receiving_wavelength=book.fixed_receiving_wavelength,
        excitation_slit=book.excitation_slit,
        emission_slit=book.emission_slit,
        flash_delay=book.flash_delay,
        sample_window=book.sample_window,
        time_per_flash=book.time_per_flash,
        flash_count=book.flash_count,
        scan_start=book.scan_start,
        scan_stop=book.scan_stop,
        scan_step=book.scan_step,
        x_at_max_y="" if validation.x_at_max_y is None else str(validation.x_at_max_y),
        max_y="" if validation.selected_y_max is None else str(validation.selected_y_max),
    )


def _output_spectrum(book: DryRunBook, attribution: AttributionFields) -> OutputSpectrum:
    sample = attribution.sample
    return OutputSpectrum(
        spectrum_id=book.book_key,
        spectrum_class=book.spectrum_class,
        canonical_sample_label=sample.canonical_label,
        sample_system_label=sample.system_label,
        temperature=sample.temperature,
        key_wavelength=_key_wavelength(book),
        x_y=_selected_xy(book),
        excitation_slit=book.excitation_slit,
        emission_slit=book.emission_slit,
        flash_delay=book.flash_delay,
        sample_window=book.sample_window,
        time_per_flash=book.time_per_flash,
        flash_count=book.flash_count,
        scan_start=book.scan_start,
        scan_stop=book.scan_stop,
        scan_step=book.scan_step,
        sample_system_identity=sample.system_identity_json(),
    )


def _key_wavelength(book: DryRunBook) -> str:
    if book.spectrum_class in {SpectrumClass.STEADY_EMISSION, SpectrumClass.DELAYED_EMISSION}:
        return str(book.fixed_excitation_wavelength or "")
    return str(book.fixed_receiving_wavelength or "")


def _selected_xy(book: DryRunBook) -> tuple[tuple[Any, Any], ...]:
    pair = select_xy_pair(book.data, _selected_y_for_class(book.spectrum_class))
    x_values = list(pair.x_column.values)
    y_values = list(pair.y_column.values)
    while x_values and y_values and _is_blank(x_values[-1]) and _is_blank(y_values[-1]):
        x_values.pop()
        y_values.pop()
    return tuple(zip(x_values, y_values))


def _selected_y_for_class(spectrum_class: SpectrumClass) -> str:
    if spectrum_class == SpectrumClass.STEADY_EXCITATION:
        return "S1c/R1c"
    return "S1c"


def _is_blank(value: Any) -> bool:
    return value is None or value == ""


def _report_exclusions(
    exclusions: tuple[SelectionExclusion, ...],
    dry_key_by_selection_key: Mapping[str, str],
) -> tuple[ReportItem, ...]:
    return tuple(
        ReportItem(dry_key_by_selection_key.get(item.book_key, item.book_key), item.reason)
        for item in exclusions
    )


def _translate_book_keys(
    book_keys: tuple[str, ...],
    selection_key_by_dry_key: Mapping[str, str],
) -> tuple[str, ...]:
    return tuple(selection_key_by_dry_key.get(key, key) for key in book_keys)


def _translate_review_choices(
    choices: Mapping[str, object],
    selection_key_by_dry_key: Mapping[str, str],
) -> dict[str, object]:
    _require_disjoint_book_key_namespaces(selection_key_by_dry_key)
    translated: dict[str, object] = {}
    for review_key, choice in choices.items():
        translated_review_key = _translate_review_key(review_key, selection_key_by_dry_key)
        if isinstance(choice, str):
            translated_choice: object = selection_key_by_dry_key.get(choice, choice)
        elif isinstance(choice, tuple):
            translated_choice = _translate_book_keys(choice, selection_key_by_dry_key)
            _require_unique_translated_book_keys(translated_choice)
        elif isinstance(choice, list):
            translated_keys = _translate_book_keys(tuple(choice), selection_key_by_dry_key)
            _require_unique_translated_book_keys(translated_keys)
            translated_choice = list(translated_keys)
        else:
            translated_choice = choice
        if translated_review_key in translated:
            raise ValueError(f"ambiguous translated review key: {translated_review_key}")
        translated[translated_review_key] = translated_choice
    return translated


def _require_disjoint_book_key_namespaces(selection_key_by_dry_key: Mapping[str, str]) -> None:
    ambiguous = {
        key
        for key in selection_key_by_dry_key.keys() & set(selection_key_by_dry_key.values())
        if selection_key_by_dry_key[key] != key
    }
    if ambiguous:
        raise ValueError(f"ambiguous dry-run Book key namespace: {min(ambiguous)}")


def _translate_review_key(
    review_key: str,
    selection_key_by_dry_key: Mapping[str, str],
) -> str:
    try:
        kind, stage, book_keys = json.loads(review_key)
    except (TypeError, ValueError):
        return review_key
    if not isinstance(book_keys, list) or not all(isinstance(key, str) for key in book_keys):
        return review_key
    translated_keys = [selection_key_by_dry_key.get(key, key) for key in book_keys]
    _require_unique_translated_book_keys(tuple(translated_keys))
    return json.dumps([kind, stage, translated_keys], ensure_ascii=False, separators=(",", ":"))


def _require_unique_translated_book_keys(book_keys: tuple[str, ...]) -> None:
    if len(set(book_keys)) != len(book_keys):
        raise ValueError("duplicate translated Book key")


def _save_distinct_records(library, books, assignments) -> dict[str, int]:
    return commit_final_attributions(library, {book.book_key: assignments[book.book_key] for book in books})


def _records_for_commit(books, assignments):
    committed_keys = {book.book_key for book in books}
    records = []
    seen = set()
    for key in sorted(committed_keys):
        record = assignments[key].sample
        identity = record.identity_json()
        if identity in seen:
            continue
        seen.add(identity)
        records.append(record)
    return tuple(records)
