from decimal import Decimal
import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from spectrum_organizer.dry_run import (
    DryRunBook,
    _translate_review_choices,
    run_non_origin_dry_run,
)
from spectrum_organizer.domain.models import LiquidSample, NeatSample, SpectrumClass
from spectrum_organizer.origin.data_columns import Column, WorksheetData
from spectrum_organizer.store.sample_library import SampleLibrary


class DryRunPipelineTests(unittest.TestCase):
    def test_review_translation_rejects_cross_book_key_namespace_collision(self):
        mapping = {
            "legacy-a": "structured-a",
            "structured-a": "structured-b",
        }

        with self.assertRaisesRegex(ValueError, "ambiguous dry-run Book key namespace"):
            _translate_review_choices(
                {"review": "structured-a"},
                mapping,
            )

    def test_review_translation_rejects_legacy_and_structured_alias_collision(self):
        legacy = "S1|Folder|Book1"
        structured = '["S1","worksheet","Folder","Book1"]'
        mapping = {legacy: structured}
        legacy_review = json.dumps(
            ["emission_duplicate", "stage1", [legacy]],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        structured_review = json.dumps(
            ["emission_duplicate", "stage1", [structured]],
            ensure_ascii=False,
            separators=(",", ":"),
        )

        with self.assertRaisesRegex(ValueError, "ambiguous translated review key"):
            _translate_review_choices(
                {legacy_review: legacy, structured_review: structured},
                mapping,
            )

    def test_review_translation_rejects_duplicate_choice_aliases(self):
        legacy = "S1|Folder|Book1"
        structured = '["S1","worksheet","Folder","Book1"]'
        review = json.dumps(
            ["excitation_selection", None, [legacy]],
            ensure_ascii=False,
            separators=(",", ":"),
        )

        with self.assertRaisesRegex(ValueError, "duplicate translated Book key"):
            _translate_review_choices(
                {review: (legacy, structured)},
                {legacy: structured},
            )

    def test_full_non_origin_pipeline_excludes_noncopyable_books_and_publishes_report(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            output_parent = root / "chosen-output"
            library = SampleLibrary(root / "data" / "sample_library.sqlite3", root / "data" / "backups", clock=lambda: "20260629_120000")
            books = _fixture_books()
            first_emission_review = _review_key(("S1|MFL_RT|F270_a", "S1|MFL_RT|F270_b"), stage="stage1")
            first_excitation_review = _review_key(("S1|MFL_RT|Ex315", "S1|MFL_RT|Ex460"), kind="excitation_selection")

            result = run_non_origin_dry_run(
                books,
                attributions=_attributions(),
                library=library,
                output_parent=output_parent,
                timestamp="20260629_123456",
                run_id="task14",
                emission_duplicate_choices={first_emission_review: "S1|MFL_RT|F270_b"},
                excitation_choices={first_excitation_review: ("S1|MFL_RT|Ex315",)},
                ignored_duplicate_input_paths=(pathlib.Path(r"C:\raw\same.opj"),),
                allow_non_origin_publication=True,
            )

            self.assertEqual("completion", result.state.stage.value)
            self.assertEqual(8, result.attribution_target_count)
            self.assertTrue(result.publication.project_path.is_file())
            self.assertTrue(result.publication.report_path.is_file())
            self.assertEqual(output_parent / "Organized_Origin_Data_20260629_123456", result.publication.output_path)

            output_ids = result.copied_spectrum_ids
            self.assertNotIn("S1|MFL_RT|F270_a", output_ids)
            self.assertNotIn("S1|MFL_RT|Ex460", output_ids)
            self.assertNotIn("S1|Bad|MissingS1", output_ids)
            self.assertNotIn("S1|2D|Steady2D", output_ids)
            self.assertFalse(any(spectrum_id.startswith("S1|PFL_2D|") for spectrum_id in output_ids))
            self.assertFalse(any(spectrum_id.startswith("S1|PFL_DelayTime|") for spectrum_id in output_ids))

            folder_names = tuple(folder.name for folder in result.output_plan.folders)
            self.assertIn("F_Ex270_ExSlit2_EmSlit2_ALL_SAMPLES", folder_names)
            self.assertIn("P_Ex270_ExSlit2_EmSlit2_FD0.1_SW1_TPF0.1_FC100_ALL_SAMPLES", folder_names)
            self.assertIn("P_Em330_ExSlit2_EmSlit2_FD0.2_SW1_TPF0.1_FC100", folder_names)
            self.assertFalse(result.output_plan.folder("F_Ex270_ExSlit2_EmSlit2_ALL_SAMPLES").is_fallback)
            self.assertTrue(result.output_plan.folder("P_Em330_ExSlit2_EmSlit2_FD0.2_SW1_TPF0.1_FC100").is_fallback)
            self.assertEqual((), result.output_plan.incomplete_folders)

            mfl_book = result.output_plan.folder("F_Ex270_ExSlit2_EmSlit2_ALL_SAMPLES").books[0]
            self.assertEqual("MFL-mTHF-1×10^-4 M", mfl_book.display_name)
            self.assertEqual(
                (
                    ("x", "Em", (Decimal("300"), Decimal("300.5"), Decimal("301"))),
                    ("raw_y", "MFL-mTHF-1×10^-4 M-77 K_F270", (Decimal("10"), None, Decimal("20"))),
                    ("raw_y", "MFL-mTHF-1×10^-4 M-298 K_F270", (None, Decimal("5"), Decimal("15"))),
                ),
                tuple((column.kind, column.comment, column.values) for column in mfl_book.columns[:3]),
            )
            self.assertIn("MFL-mTHF-1×10^-4 M-298 K_FEx315", tuple(column.comment for column in mfl_book.raw_y_columns))

            report = result.publication.report_path.read_text(encoding="utf-8")
            self.assertIn("输入路径", report)
            self.assertIn("本次设置", report)
            self.assertIn("忽略的重复输入路径", report)
            self.assertIn(r"C:\raw\same.opj", report)
            self.assertIn("S1|Bad|MissingS1：missing S1", report)
            self.assertIn("S1|MFL_RT|F270_a：emission_duplicate_unselected", report)
            self.assertIn("S1|MFL_RT|Ex460：excitation_candidate_unselected", report)
            self.assertIn("二维稳态谱：S1|2D|Steady2D", report)
            self.assertIn("二维延迟谱：", report)
            self.assertIn("时间分辨延迟谱：", report)
            self.assertIn("人工选择", report)
            self.assertIn("数量核对", report)
            self.assertIn("输出 Folder/Book 映射", report)
            self.assertIn("齐全 Folder", report)
            self.assertIn("不齐全 Folder", report)
            self.assertIn("仅激发谱 Folder", report)

            self.assertEqual(20, len(result.sample_record_ids))
            self.assertEqual(8, len(set(result.sample_record_ids.values())))
            self.assertIn("S1|2D|Steady2D", result.sample_record_ids)


    def test_non_origin_publication_requires_explicit_opt_in(self):
        with tempfile.TemporaryDirectory() as temp:
            output_parent = pathlib.Path(temp) / "out"
            library = SampleLibrary(
                pathlib.Path(temp) / "db.sqlite3",
                pathlib.Path(temp) / "backups",
                clock=lambda: "20260629_120000",
            )
            with self.assertRaisesRegex(ValueError, "non-Origin publication requires explicit opt-in"):
                run_non_origin_dry_run(
                    _fixture_books(),
                    attributions=_attributions(),
                    library=library,
                    output_parent=output_parent,
                    timestamp="20260629_123456",
                    run_id="task14-no-fake-publish",
                    emission_duplicate_choices={_review_key(("S1|MFL_RT|F270_a", "S1|MFL_RT|F270_b"), stage="stage1"): "S1|MFL_RT|F270_b"},
                    excitation_choices={_review_key(("S1|MFL_RT|Ex315", "S1|MFL_RT|Ex460"), kind="excitation_selection"): ("S1|MFL_RT|Ex315",)},
                )

            self.assertFalse(output_parent.exists())
            self.assertFalse(library.path.exists())

    def test_dry_run_preserves_scan_fields_needed_to_disambiguate_excitation_comments(self):
        emission = _book(
            "F270",
            folder="Sample",
            spectrum_class=SpectrumClass.STEADY_EMISSION,
            fixed_excitation_wavelength="270",
        )
        first_excitation = _book(
            "Ex315a",
            folder="Sample",
            spectrum_class=SpectrumClass.STEADY_EXCITATION,
            fixed_receiving_wavelength="315",
            data=_excitation_data((250, 1), (251, 2)),
        )
        second_excitation = DryRunBook(
            **{
                **first_excitation.__dict__,
                "book_name": "Ex315b",
                "default_name": "Ex315b",
                "display_name": "User Ex315b",
                "scan_start": "260",
            }
        )
        books = (emission, first_excitation, second_excitation)
        sample = NeatSample("SAMPLE", "Solid", "298 K")
        excitation_review = _review_key(
            (first_excitation.book_key, second_excitation.book_key),
            kind="excitation_selection",
        )

        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            result = run_non_origin_dry_run(
                books,
                attributions={book.book_key: sample for book in books},
                library=SampleLibrary(
                    root / "db.sqlite3",
                    root / "backups",
                    clock=lambda: "20260731_120000",
                ),
                output_parent=root / "out",
                timestamp="20260731_120000",
                run_id="scan-comment-fields",
                excitation_choices={
                    excitation_review: (
                        first_excitation.book_key,
                        second_excitation.book_key,
                    )
                },
                allow_non_origin_publication=True,
            )

        comments = tuple(
            column.comment
            for folder in result.output_plan.folders
            for book in folder.books
            for column in book.raw_y_columns
        )
        self.assertIn("SAMPLE-Solid-298 K_FEx315_ExStart250", comments)
        self.assertIn("SAMPLE-Solid-298 K_FEx315_ExStart260", comments)

    def test_dry_run_preserves_structured_identity_for_book_name_conflicts(self):
        first = _book(
            "F270",
            folder="First",
            spectrum_class=SpectrumClass.STEADY_EMISSION,
            fixed_excitation_wavelength="270",
        )
        second = _book(
            "F280",
            folder="Second",
            spectrum_class=SpectrumClass.STEADY_EMISSION,
            fixed_excitation_wavelength="280",
        )
        first_sample = LiquidSample("A-B", "C", "1 M", "298 K")
        second_sample = LiquidSample("A", "B-C", "1 M", "298 K")

        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            with self.assertRaisesRegex(
                ValueError,
                "Book Long Name.*multiple sample system identities",
            ):
                run_non_origin_dry_run(
                    (first, second),
                    attributions={
                        first.book_key: first_sample,
                        second.book_key: second_sample,
                    },
                    library=SampleLibrary(
                        root / "db.sqlite3",
                        root / "backups",
                        clock=lambda: "20260731_120000",
                    ),
                    output_parent=root / "out",
                    timestamp="20260731_120000",
                    run_id="structured-identity-conflict",
                    allow_non_origin_publication=True,
                )

    def test_dry_run_book_keys_do_not_collide_across_page_types(self):
        worksheet = _book("D", folder="C", spectrum_class=SpectrumClass.STEADY_EMISSION)
        matrix = DryRunBook(**{**worksheet.__dict__, "page_type": "matrix"})

        self.assertNotEqual(worksheet.book_key, matrix.book_key)

    def test_matrix_page_type_survives_full_dry_run_attribution(self):
        worksheet = _book(
            "MatrixEmission",
            folder="MatrixFolder",
            spectrum_class=SpectrumClass.STEADY_EMISSION,
            fixed_excitation_wavelength="270",
        )
        matrix = DryRunBook(**{**worksheet.__dict__, "page_type": "matrix"})

        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            result = run_non_origin_dry_run(
                (matrix,),
                attributions={matrix.book_key: NeatSample("MATRIX", "Solid", "298 K")},
                library=SampleLibrary(
                    root / "data" / "sample_library.sqlite3",
                    root / "data" / "backups",
                    clock=lambda: "20260716_170000",
                ),
                output_parent=root / "out",
                timestamp="20260716_170000",
                run_id="matrix-page-type",
                allow_non_origin_publication=True,
            )

        self.assertEqual((matrix.book_key,), result.copied_spectrum_ids)
        self.assertEqual(1, result.attribution_target_count)

    def test_dry_run_book_keys_do_not_collide_when_identity_parts_contain_separator(self):
        first = _book("D", folder="C", spectrum_class=SpectrumClass.STEADY_EMISSION)
        second = DryRunBook(
            **{
                **first.__dict__,
                "source_id": "S1|C",
                "folder_path": "",
            }
        )
        first = DryRunBook(
            **{
                **first.__dict__,
                "source_id": "S1",
                "folder_path": "C|",
            }
        )
        self.assertNotEqual(first.book_key, second.book_key)

    def test_dry_run_rejects_conflicting_folder_attributions(self):
        first = _book(
            "F270",
            folder="MFL_RT",
            spectrum_class=SpectrumClass.STEADY_EMISSION,
            fixed_excitation_wavelength="270",
        )
        second = _book(
            "F280",
            folder="MFL_RT",
            spectrum_class=SpectrumClass.STEADY_EMISSION,
            fixed_excitation_wavelength="280",
        )

        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ValueError, "conflicting dry-run folder attributions"):
                run_non_origin_dry_run(
                    (first, second),
                    attributions={
                        first.book_key: NeatSample("FIRST", "Solid", "298 K"),
                        second.book_key: NeatSample("SECOND", "Solid", "298 K"),
                    },
                    library=SampleLibrary(
                        pathlib.Path(temp) / "db.sqlite3",
                        pathlib.Path(temp) / "backups",
                        clock=lambda: "20260629_120000",
                    ),
                    output_parent=pathlib.Path(temp) / "out",
                    timestamp="20260629_123456",
                    run_id="conflicting-folder-attribution",
                )

    def test_special_review_pending_decisions_block_pipeline(self):
        books = (
            _book("D2D300a", folder="PFL_2D", spectrum_class=SpectrumClass.DELAYED_EMISSION, fixed_excitation_wavelength="300", flash_delay="0.5", sample_window="1", time_per_flash="0.5", flash_count="100"),
            _book("D2D300b", folder="PFL_2D", spectrum_class=SpectrumClass.DELAYED_EMISSION, fixed_excitation_wavelength="300", flash_delay="0.5", sample_window="1", time_per_flash="0.5", flash_count="100"),
            *tuple(_book(f"D2D{w}", folder="PFL_2D", spectrum_class=SpectrumClass.DELAYED_EMISSION, fixed_excitation_wavelength=str(w), flash_delay="0.5", sample_window="1", time_per_flash="0.5", flash_count="100") for w in (305, 310, 315, 320)),
        )
        attributions = {book.book_key: NeatSample("PFL2D", "Film", "77 K") for book in books}

        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ValueError, "special duplicate"):
                run_non_origin_dry_run(
                    books,
                    attributions=attributions,
                    library=SampleLibrary(pathlib.Path(temp) / "db.sqlite3", pathlib.Path(temp) / "backups", clock=lambda: "20260629_120000"),
                    output_parent=pathlib.Path(temp) / "out",
                    timestamp="20260629_123456",
                    run_id="task14-pending-special",
                )



    def test_special_review_pending_overlap_blocks_pipeline(self):
        books = (
            _book("B300", folder="PFL_Overlap", spectrum_class=SpectrumClass.DELAYED_EMISSION, fixed_excitation_wavelength="300", flash_delay="0.1", sample_window="1", time_per_flash="1.1", flash_count="100"),
            _book("B305", folder="PFL_Overlap", spectrum_class=SpectrumClass.DELAYED_EMISSION, fixed_excitation_wavelength="305", flash_delay="0.1", sample_window="1", time_per_flash="1.1", flash_count="100"),
            _book("B310", folder="PFL_Overlap", spectrum_class=SpectrumClass.DELAYED_EMISSION, fixed_excitation_wavelength="310", flash_delay="0.1", sample_window="1", time_per_flash="1.1", flash_count="100"),
            _book("B315", folder="PFL_Overlap", spectrum_class=SpectrumClass.DELAYED_EMISSION, fixed_excitation_wavelength="315", flash_delay="0.1", sample_window="1", time_per_flash="1.1", flash_count="100"),
            _book("B320", folder="PFL_Overlap", spectrum_class=SpectrumClass.DELAYED_EMISSION, fixed_excitation_wavelength="320", flash_delay="0.1", sample_window="1", time_per_flash="1.1", flash_count="100"),
            _book("B300_D2", folder="PFL_Overlap", spectrum_class=SpectrumClass.DELAYED_EMISSION, fixed_excitation_wavelength="300", flash_delay="0.2", sample_window="1", time_per_flash="1.2", flash_count="100"),
            _book("B300_D3", folder="PFL_Overlap", spectrum_class=SpectrumClass.DELAYED_EMISSION, fixed_excitation_wavelength="300", flash_delay="0.3", sample_window="1", time_per_flash="1.3", flash_count="100"),
        )
        attributions = {book.book_key: NeatSample("PFL2D", "Film", "77 K") for book in books}

        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ValueError, "special overlap"):
                run_non_origin_dry_run(
                    books,
                    attributions=attributions,
                    library=SampleLibrary(pathlib.Path(temp) / "db.sqlite3", pathlib.Path(temp) / "backups", clock=lambda: "20260629_120000"),
                    output_parent=pathlib.Path(temp) / "out",
                    timestamp="20260629_123456",
                    run_id="task14-pending-overlap",
                )

def _fixture_books():
    return (
        _book("F270_a", folder="MFL_RT", spectrum_class=SpectrumClass.STEADY_EMISSION, fixed_excitation_wavelength="270", data=_steady_emission_data((300, 10), (301, 12))),
        _book("F270_b", folder="MFL_RT", spectrum_class=SpectrumClass.STEADY_EMISSION, fixed_excitation_wavelength="270", data=_steady_emission_data((300.5, 5), (301, 15))),
        _book("Ex315", folder="MFL_RT", spectrum_class=SpectrumClass.STEADY_EXCITATION, fixed_receiving_wavelength="315", data=_excitation_data((250, 2), (251, 4))),
        _book("Ex460", folder="MFL_RT", spectrum_class=SpectrumClass.STEADY_EXCITATION, fixed_receiving_wavelength="460", data=_excitation_data((250, 8), (251, 16))),
        _book("F270_77", folder="MFL_77K", spectrum_class=SpectrumClass.STEADY_EMISSION, fixed_excitation_wavelength="270", data=_steady_emission_data((300, 10), (301, 20))),
        _book("Ex315_77", folder="MFL_77K", spectrum_class=SpectrumClass.STEADY_EXCITATION, fixed_receiving_wavelength="315", data=_excitation_data((250.5, 3), (251, 6))),
        _book("MixedF270", folder="MixedFolder", spectrum_class=SpectrumClass.STEADY_EMISSION, fixed_excitation_wavelength="270", mixed_folder=True),
        _book("RootF270", folder="", spectrum_class=SpectrumClass.STEADY_EMISSION, fixed_excitation_wavelength="270"),
        _book("P270", folder="PFL_77K", spectrum_class=SpectrumClass.DELAYED_EMISSION, fixed_excitation_wavelength="270", flash_delay="0.1", sample_window="1", time_per_flash="0.1", flash_count="100"),
        _book("PEx315", folder="PFL_77K", spectrum_class=SpectrumClass.DELAYED_EXCITATION, fixed_receiving_wavelength="315", flash_delay="0.1", sample_window="1", time_per_flash="0.1", flash_count="999"),
        _book("PEx330", folder="PFL_77K", spectrum_class=SpectrumClass.DELAYED_EXCITATION, fixed_receiving_wavelength="330", flash_delay="0.2", sample_window="1", time_per_flash="0.1", flash_count="100"),
        _book("MissingS1", folder="Bad", spectrum_class=SpectrumClass.STEADY_EMISSION, fixed_excitation_wavelength="270", data=WorksheetData([Column("A", "Wavelength", [300], "X"), Column("S1c", "S1c", [1], "Y")])),
        _book("Steady2D", folder="2D", spectrum_class=SpectrumClass.STEADY_2D, data=WorksheetData([])),
        *tuple(_book(f"D2D{w}", folder="PFL_2D", spectrum_class=SpectrumClass.DELAYED_EMISSION, fixed_excitation_wavelength=str(w), flash_delay="0.5", sample_window="1", time_per_flash="0.5", flash_count="100") for w in (300, 305, 310, 315, 320)),
        *tuple(_book(f"DT{index}", folder="PFL_DelayTime", spectrum_class=SpectrumClass.DELAYED_EMISSION, fixed_excitation_wavelength="400", flash_delay=fd, sample_window="1", time_per_flash=tpf, flash_count="100") for index, (fd, tpf) in enumerate((("0.1", "1.1"), ("0.2", "1.2"), ("0.3", "1.3")), start=1)),
    )


def _attributions():
    sample_298 = LiquidSample("MFL", "mTHF", "1×10^-4 M", "298 K")
    sample_77 = LiquidSample("MFL", "mTHF", "1×10^-4 M", "77 K")
    return {
        "S1|MFL_RT|F270_a": sample_298,
        "S1|MFL_RT|F270_b": sample_298,
        "S1|MFL_RT|Ex315": sample_298,
        "S1|MFL_RT|Ex460": sample_298,
        "S1|MFL_77K|F270_77": sample_77,
        "S1|MFL_77K|Ex315_77": sample_77,
        "S1|MixedFolder|MixedF270": NeatSample("MIX", "Solid", "298 K"),
        "S1||RootF270": NeatSample("ROOT", "Solid", "298 K"),
        "S1|PFL_77K|P270": NeatSample("PFL", "Film", "77 K"),
        "S1|PFL_77K|PEx315": NeatSample("PFL", "Film", "77 K"),
        "S1|PFL_77K|PEx330": NeatSample("PFL", "Film", "77 K"),
        "S1|2D|Steady2D": NeatSample("TWO-D", "Film", "298 K"),
        **{f"S1|PFL_2D|D2D{w}": NeatSample("PFL2D", "Film", "77 K") for w in (300, 305, 310, 315, 320)},
        **{f"S1|PFL_DelayTime|DT{index}": NeatSample("PFLT", "Film", "77 K") for index in (1, 2, 3)},
    }


def _book(
    name,
    *,
    folder,
    spectrum_class,
    fixed_excitation_wavelength=None,
    fixed_receiving_wavelength=None,
    excitation_slit="2",
    emission_slit="2",
    flash_delay=None,
    sample_window=None,
    time_per_flash=None,
    flash_count=None,
    mixed_folder=False,
    data=None,
):
    return DryRunBook(
        source_id="S1",
        source_filename="synthetic.opju",
        folder_path=folder,
        book_name=name,
        display_name=f"User {name}",
        default_name=name,
        spectrum_class=spectrum_class,
        data=data or _delayed_or_steady_data(),
        fixed_excitation_wavelength=fixed_excitation_wavelength,
        fixed_receiving_wavelength=fixed_receiving_wavelength,
        excitation_slit=excitation_slit,
        emission_slit=emission_slit,
        flash_delay=flash_delay,
        sample_window=sample_window,
        time_per_flash=time_per_flash,
        flash_count=flash_count,
        receiving_range=("450", "650"),
        scan_start="250",
        scan_stop="450",
        scan_step="1",
        mixed_folder=mixed_folder,
    )


def _steady_emission_data(*points):
    x = [point[0] for point in points]
    y = [point[1] for point in points]
    return WorksheetData([Column("A", "Wavelength", x, "X"), Column("S1c", "S1c", y, "Y"), Column("B", "Wavelength", x, "X"), Column("S1", "S1", y, "Y")])


def _excitation_data(*points):
    x = [point[0] for point in points]
    y = [point[1] for point in points]
    return WorksheetData([Column("A", "Wavelength", x, "X"), Column("S1c", "S1c", y, "Y"), Column("B", "Wavelength", x, "X"), Column("S1cR1c", "S1c / R1c", y, "Y"), Column("C", "Wavelength", x, "X"), Column("S1", "S1", y, "Y")])


def _delayed_or_steady_data():
    return _steady_emission_data((500, 10), (501, 20))


def _review_key(book_keys, *, kind="emission_duplicate", stage=None):
    return json.dumps([kind, stage, list(book_keys)], ensure_ascii=False, separators=(",", ":"))




if __name__ == "__main__":
    unittest.main()
