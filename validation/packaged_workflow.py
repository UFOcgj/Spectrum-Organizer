from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import time

from spectrum_organizer.app_paths import ensure_app_paths
from spectrum_organizer.domain.models import LiquidSample, SpectrumClass
from spectrum_organizer.dry_run import DryRunBook, run_non_origin_dry_run
from spectrum_organizer.origin.data_columns import Column, WorksheetData
from spectrum_organizer.settings import SettingsStore
from spectrum_organizer.store.sample_library import SampleLibrary
from spectrum_organizer.ui.orchestrator import BookOnlyOrchestrator


@dataclass(frozen=True)
class PackagedWorkflowSummary:
    selected_source_paths: tuple[str, ...]
    duplicate_source_paths: tuple[str, ...]
    output_parent: str
    settings_file: str
    summary_file: str
    final_run_dir: str
    project_path: str
    report_path: str
    final_stage: str
    copied_spectrum_ids: tuple[str, ...]


def packaged_workflow_main(argv: list[str] | tuple[str, ...], *, local_appdata=None, output=None) -> int:
    parser = argparse.ArgumentParser(prog="validation.packaged_workflow")
    parser.add_argument("--source", action="append", required=True)
    parser.add_argument("--output-parent", required=True)
    parser.add_argument("--timestamp")
    args = parser.parse_args(list(argv))
    summary = run_packaged_non_origin_workflow(
        tuple(args.source),
        Path(args.output_parent),
        local_appdata=local_appdata,
        timestamp=args.timestamp,
    )
    if output is not None:
        output.write(json.dumps(asdict(summary), ensure_ascii=False, sort_keys=True) + "\n")
    return 0


def run_packaged_non_origin_workflow(
    source_paths: tuple[str | Path, ...],
    output_parent: Path,
    *,
    local_appdata=None,
    timestamp: str | None = None,
) -> PackagedWorkflowSummary:
    if local_appdata is None:
        raise ValueError("validation local_appdata must be explicit")
    app_paths = ensure_app_paths(local_appdata)
    timestamp = timestamp or time.strftime("%Y%m%d_%H%M%S")
    sources = tuple(Path(path) for path in source_paths)
    missing = tuple(str(path) for path in sources if not path.is_file())
    if missing:
        raise ValueError(f"Selected source file does not exist: {missing[0]}")

    settings_store = SettingsStore(app_paths.settings_file)
    orchestrator = BookOnlyOrchestrator(settings_store)
    selected = orchestrator.select_sources([str(path) for path in sources])
    if not selected.ok:
        raise ValueError(f"Source selection failed: {selected.reason}")
    orchestrator.select_output_parent(str(output_parent))
    orchestrator.confirm_preflight_settings(s1_limit=2_000_000, steady_emission_y="S1c")

    library = SampleLibrary(
        app_paths.data / "sample_library.sqlite3",
        app_paths.backups,
        clock=lambda: timestamp,
        health_temp_root=app_paths.temp,
    )
    selected_sources = tuple(Path(path) for path in selected.source_paths)
    books = _books_for_sources(selected_sources)
    result = run_non_origin_dry_run(
        books,
        attributions=_attributions_for_books(books),
        library=library,
        output_parent=Path(output_parent),
        timestamp=timestamp,
        run_id="packaged-non-origin",
        ignored_duplicate_input_paths=tuple(Path(path) for path in selected.duplicate_paths),
        allow_non_origin_publication=True,
    )

    summary_path = app_paths.logs / f"Packaged_Non_Origin_Workflow_{timestamp}.json"
    summary = PackagedWorkflowSummary(
        selected_source_paths=tuple(str(path) for path in selected_sources),
        duplicate_source_paths=selected.duplicate_paths,
        output_parent=str(output_parent),
        settings_file=str(app_paths.settings_file),
        summary_file=str(summary_path),
        final_run_dir=str(result.publication.output_path),
        project_path=str(result.publication.project_path),
        report_path=str(result.publication.report_path),
        final_stage=result.state.stage.value,
        copied_spectrum_ids=result.copied_spectrum_ids,
    )
    summary_path.write_text(json.dumps(asdict(summary), ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return summary


def _books_for_sources(source_paths: tuple[Path, ...]) -> tuple[DryRunBook, ...]:
    books: list[DryRunBook] = []
    for index, path in enumerate(source_paths, start=1):
        source_id = f"SRC{index}"
        folder = f"PackagedSmoke{index}"
        books.append(
            _book(
                source_id,
                path.name,
                folder,
                "F270",
                SpectrumClass.STEADY_EMISSION,
                fixed_excitation_wavelength="270",
                data=_steady_emission_data((300, 10 + index), (301, 20 + index)),
            )
        )
        books.append(
            _book(
                source_id,
                path.name,
                folder,
                "Ex315",
                SpectrumClass.STEADY_EXCITATION,
                fixed_receiving_wavelength="315",
                data=_excitation_data((250, 5 + index), (251, 10 + index)),
            )
        )
    return tuple(books)


def _book(
    source_id: str,
    source_filename: str,
    folder: str,
    name: str,
    spectrum_class: SpectrumClass,
    *,
    fixed_excitation_wavelength: str | None = None,
    fixed_receiving_wavelength: str | None = None,
    data: WorksheetData,
) -> DryRunBook:
    return DryRunBook(
        source_id=source_id,
        source_filename=source_filename,
        folder_path=folder,
        book_name=name,
        display_name=name,
        default_name=name,
        spectrum_class=spectrum_class,
        data=data,
        fixed_excitation_wavelength=fixed_excitation_wavelength,
        fixed_receiving_wavelength=fixed_receiving_wavelength,
        excitation_slit="2",
        emission_slit="2",
        receiving_range=("450", "650"),
        scan_start="250",
        scan_stop="450",
        scan_step="1",
    )


def _attributions_for_books(books: tuple[DryRunBook, ...]) -> dict[str, LiquidSample]:
    return {
        book.book_key: LiquidSample(f"PKG{book.source_id}", "mTHF", "1×10^-4 M", "298 K")
        for book in books
    }


def _steady_emission_data(*points) -> WorksheetData:
    x = [point[0] for point in points]
    y = [point[1] for point in points]
    return WorksheetData([
        Column("A", "Wavelength", x, "X"),
        Column("S1c", "S1c", y, "Y"),
        Column("B", "Wavelength", x, "X"),
        Column("S1", "S1", y, "Y"),
    ])


def _excitation_data(*points) -> WorksheetData:
    x = [point[0] for point in points]
    y = [point[1] for point in points]
    return WorksheetData([
        Column("A", "Wavelength", x, "X"),
        Column("S1c", "S1c", y, "Y"),
        Column("B", "Wavelength", x, "X"),
        Column("S1cR1c", "S1c / R1c", y, "Y"),
        Column("C", "Wavelength", x, "X"),
        Column("S1", "S1", y, "Y"),
    ])
