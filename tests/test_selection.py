import json
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from spectrum_organizer.core.selection import (
    CandidateConversionError,
    SelectionSpectrum,
    build_candidate_display,
    build_review_candidate_display,
    convert_extracted_results,
    filter_copyable_emissions_after_special,
    review_emission_duplicates,
    select_excitation_candidates,
)
from spectrum_organizer.domain.models import SpectrumClass
from spectrum_organizer.origin.extract_worker import TerminalBookResult
from spectrum_organizer.ui.dialogs import (
    cross_source_emission_conflict_dialog,
    emission_duplicate_review_dialog,
)


class SelectionTests(unittest.TestCase):
    def test_candidate_conversion_checks_cancellation_between_results(self):
        results = (
            _terminal_result(short_name="Book1"),
            _terminal_result(short_name="Book2"),
        )
        checks = []

        def cancel_check():
            checks.append(None)
            if len(checks) == 2:
                raise RuntimeError("cancelled")

        with self.assertRaisesRegex(RuntimeError, "cancelled"):
            convert_extracted_results(
                results,
                source_filenames={"S1": "source.opj"},
                expected_source_ids=("S1",),
                cancel_check=cancel_check,
            )

        self.assertEqual(2, len(checks))

    def test_converts_extracted_terminal_payload_to_pre_attribution_candidate(self):
        result = _terminal_result(
            note_text=(
                "[EXP_FD_FILE]\nExperiment Type = Phos Acquisition[Emission]\n"
                "Flash Delay = 0.1\nSample window = 1\nTime per Flash = 0.2\nFlash Count = 100\n"
                "[EX1]\nPark = 315\nFront Entrance Slit = 2\nFront Exit Slit = 2\n"
                "[EM1]\nStart = 350\nEnd = 700\nIncrement = 1\n"
                "Front Entrance Slit = 4\nFront Exit Slit = 4\n"
            ),
            spectrum_class="delayed_emission",
        )

        converted = convert_extracted_results(
            (result,),
            source_filenames={"S1": "source.opj"},
            expected_source_ids=("S1",),
        )

        self.assertEqual(1, len(converted.ordinary_candidates))
        candidate = converted.ordinary_candidates[0]
        self.assertEqual("source.opj", candidate.source_filename)
        self.assertEqual("Folder", candidate.folder_path)
        self.assertEqual("Display", candidate.display_name)
        self.assertEqual("Book1", candidate.short_name)
        self.assertEqual(SpectrumClass.DELAYED_EMISSION, candidate.spectrum_class)
        self.assertEqual("emission", candidate.role)
        self.assertEqual("315", candidate.fixed_wavelength)
        self.assertEqual(("350", "700"), candidate.wavelength_range)
        self.assertEqual("1", candidate.scan_increment)
        self.assertEqual(("2", "2"), candidate.excitation_slits)
        self.assertEqual(("4", "4"), candidate.emission_slits)
        self.assertEqual("0.1", candidate.flash_delay)
        self.assertEqual(12, candidate.max_y)
        self.assertEqual(301, candidate.x_at_max_y)
        self.assertEqual((300, 301), candidate.x_values)
        self.assertEqual((10, 12), candidate.y_values)
        self.assertIsNone(candidate.payload_snapshot_path)
        self.assertEqual(
            ("source.opj", "Folder", "Display", "delayed_emission", "emission"),
            tuple(value for _key, value in build_review_candidate_display(candidate)[:5]),
        )
        self.assertEqual((), converted.rejections)

    def test_candidate_conversion_preserves_empty_origin_long_name(self):
        converted = convert_extracted_results(
            (_terminal_result(short_name="DfltEx1", display_name=""),),
            source_filenames={"S1": "source.opj"},
            expected_source_ids=("S1",),
        )

        candidate = converted.ordinary_candidates[0]
        self.assertEqual("", candidate.display_name)
        self.assertEqual("DfltEx1", candidate.short_name)
        self.assertEqual(
            "DfltEx1",
            dict(build_review_candidate_display(candidate))["book_name"],
        )

    def test_converts_instrument_colon_sections_with_known_units(self):
        result = _terminal_result(
            note_text=(
                "[EXP_FD_FILE]\nExperiment Type: Phos Acquisition[Emission]\n"
                "Flash Delay: 1.00\nSample window: 20.00\nTime per Flash: 46.00\nFlash Count: 4\n"
                "EX1: Excitation 1 (Mono3)\nPark: 300.00nm\n"
                "Front Entrance Slit: 10.00 nmBandpass\nFront Exit Slit: 10.00 nmBandpass\n"
                "EM1: Emission 1 (Mono4)\nStart: 400.00nm\nEnd: 750.00nm\nIncrement: 1.00nm\n"
                "Front Entrance Slit: 10.00 nmBandpass\nFront Exit Slit: 10.00 nmBandpass\n"
                "ACCESSORIES:\n"
            ),
            spectrum_class="delayed_emission",
        )

        converted = convert_extracted_results(
            (result,),
            source_filenames={"S1": "source.opj"},
            expected_source_ids=("S1",),
        )

        self.assertEqual((), converted.rejections)
        candidate = converted.ordinary_candidates[0]
        self.assertEqual("300.00", candidate.fixed_wavelength)
        self.assertEqual(("400.00", "750.00"), candidate.wavelength_range)
        self.assertEqual("1.00", candidate.scan_increment)
        self.assertEqual(("10.00", "10.00"), candidate.excitation_slits)
        self.assertEqual(("10.00", "10.00"), candidate.emission_slits)

    def test_rejects_instrument_slit_with_unknown_unit(self):
        result = _terminal_result(
            note_text=(
                "[EXP_FD_FILE]\nExperiment Type: Phos Acquisition[Emission]\n"
                "Flash Delay: 1.00\nSample window: 20.00\nTime per Flash: 46.00\nFlash Count: 4\n"
                "EX1: Excitation 1 (Mono3)\nPark: 300.00nm\n"
                "Front Entrance Slit: 10.00 bananas\nFront Exit Slit: 10.00 nmBandpass\n"
                "EM1: Emission 1 (Mono4)\nStart: 400.00nm\nEnd: 750.00nm\nIncrement: 1.00nm\n"
                "Front Entrance Slit: 10.00 nmBandpass\nFront Exit Slit: 10.00 nmBandpass\n"
            ),
            spectrum_class="delayed_emission",
        )

        converted = convert_extracted_results(
            (result,),
            source_filenames={"S1": "source.opj"},
            expected_source_ids=("S1",),
        )

        self.assertEqual((), converted.ordinary_candidates)
        self.assertEqual(1, len(converted.rejections))
        self.assertIn(
            "invalid numeric excitation front entrance slit",
            converted.rejections[0].reason,
        )

    def test_steady_2d_is_reported_separately_and_rejected_book_keeps_reason(self):
        two_dimensional = _terminal_result(
            short_name="Map",
            note_text=(
                "[EXP_FD_FILE]\nExperiment Type = 3D Acquisition[Excitation vs Emission vs Intensity]\n"
                "[EX1]\nStart = 250\nEnd = 450\nIncrement = 5\n"
                "Front Entrance Slit = 2\nFront Exit Slit = 2\n"
                "[EM1]\nStart = 300\nEnd = 700\nIncrement = 2\n"
                "Front Entrance Slit = 2\nFront Exit Slit = 2\n"
            ),
            spectrum_class="steady_2d",
            selected_x_values=(),
            selected_y_values=(),
        )
        rejected = _terminal_result(
            short_name="Bad",
            status="rejected",
            rejection_reason="S1 max exceeds limit",
        )

        converted = convert_extracted_results(
            (two_dimensional, rejected),
            source_filenames={"S1": "source.opj"},
            expected_source_ids=("S1",),
        )

        self.assertEqual(
            ('["S1","worksheet","Folder","Map"]',),
            tuple(item.book_key for item in converted.steady_2d_candidates),
        )
        candidate = converted.steady_2d_candidates[0]
        self.assertEqual(("250", "450"), candidate.excitation_range)
        self.assertEqual(("300", "700"), candidate.emission_range)
        display = dict(build_review_candidate_display(candidate))
        self.assertEqual("250 - 450", display["excitation_range"])
        self.assertEqual("5", display["excitation_increment"])
        self.assertEqual("300 - 700", display["emission_range"])
        self.assertEqual("2", display["emission_increment"])
        self.assertEqual(
            ('["S1","worksheet","Folder","Bad"]',),
            tuple(item.book_key for item in converted.rejections),
        )
        self.assertEqual("S1 max exceeds limit", converted.rejections[0].reason)
        self.assertEqual("S1c", converted.rejections[0].selected_y_column)
        self.assertEqual("X", converted.rejections[0].paired_x_column)
        self.assertEqual(100, converted.rejections[0].s1_max)
        self.assertEqual(12, converted.rejections[0].max_y)
        self.assertEqual(301, converted.rejections[0].x_at_max_y)

    def test_extracted_candidate_missing_required_note_wavelength_metadata_is_rejected(self):
        missing_fixed = _terminal_result(
            short_name="MissingFixed",
            note_text=(
                "[EXP_FD_FILE]\nAcquisition Type = Spectral Acquisition[Emission]\n"
                "Emission Range = 300 - 650"
            ),
        )
        missing_range = _terminal_result(
            short_name="MissingRange",
            note_text=(
                "[EXP_FD_FILE]\nAcquisition Type = Spectral Acquisition[Excitation]\n"
                "Emission Wavelength = 500"
            ),
            spectrum_class="steady_excitation",
        )

        converted = convert_extracted_results(
            (missing_fixed, missing_range),
            source_filenames={"S1": "source.opj"},
            expected_source_ids=("S1",),
        )

        self.assertEqual((), converted.ordinary_candidates)
        self.assertEqual(
            ("Note is missing fixed excitation wavelength", "Note is missing excitation scan range"),
            tuple(item.reason for item in converted.rejections),
        )

    def test_extracted_candidate_missing_required_slits_is_rejected(self):
        converted = convert_extracted_results(
            (
                _terminal_result(
                    note_text=(
                        "[EXP_FD_FILE]\n"
                        "Acquisition Type = Spectral Acquisition[Emission]\n"
                        "Excitation Wavelength = 270\n"
                        "Emission Range = 300 - 650\n"
                        "Emission Increment = 1"
                    ),
                ),
            ),
            source_filenames={"S1": "source.opj"},
            expected_source_ids=("S1",),
        )

        self.assertEqual((), converted.ordinary_candidates)
        self.assertEqual(
            ("Note is missing excitation slits",),
            tuple(item.reason for item in converted.rejections),
        )

    def test_extracted_candidate_with_non_numeric_required_note_metadata_is_rejected(self):
        delayed_note = (
            "[EXP_FD_FILE]\nAcquisition Type = Phos Acquisition[Emission]\n"
            "Flash Delay = 0.1\nSample Window = 1\nTime per Flash = 0.2\nFlash Count = 100\n"
            "Excitation Wavelength = 315\nEmission Range = 350 - 700\nEmission Increment = 1"
        )
        cases = (
            (
                "fixed excitation wavelength",
                "[EXP_FD_FILE]\nAcquisition Type = Spectral Acquisition[Emission]\n"
                "Excitation Wavelength = abc\nEmission Range = 300 - 650\nEmission Increment = 1",
                "steady_emission",
            ),
            (
                "emission scan range start",
                "[EXP_FD_FILE]\nAcquisition Type = Spectral Acquisition[Emission]\n"
                "Excitation Wavelength = 270\nEmission Range = foo - 650\nEmission Increment = 1",
                "steady_emission",
            ),
            (
                "emission scan increment",
                "[EXP_FD_FILE]\nAcquisition Type = Spectral Acquisition[Emission]\n"
                "Excitation Wavelength = 270\nEmission Range = 300 - 650\nEmission Increment = bogus",
                "steady_emission",
            ),
            (
                "fixed emission wavelength",
                "[EXP_FD_FILE]\nAcquisition Type = Spectral Acquisition[Excitation]\n"
                "Emission Wavelength = abc\nExcitation Range = 250 - 450\nExcitation Increment = 1",
                "steady_excitation",
            ),
            (
                "excitation scan range start",
                "[EXP_FD_FILE]\nAcquisition Type = 3D Acquisition[Excitation vs Emission vs Intensity]\n"
                "[EX1]\nStart = foo\nEnd = 450\nIncrement = 5\n"
                "[EM1]\nStart = 300\nEnd = 700\nIncrement = 2",
                "steady_2d",
            ),
            (
                "emission scan increment",
                "[EXP_FD_FILE]\nAcquisition Type = 3D Acquisition[Excitation vs Emission vs Intensity]\n"
                "[EX1]\nStart = 250\nEnd = 450\nIncrement = 5\n"
                "[EM1]\nStart = 300\nEnd = 700\nIncrement = bogus",
                "steady_2d",
            ),
            ("Flash Delay", delayed_note.replace("Flash Delay = 0.1", "Flash Delay = bad"), "delayed_emission"),
            ("Sample Window", delayed_note.replace("Sample Window = 1", "Sample Window = bad"), "delayed_emission"),
            ("Time per Flash", delayed_note.replace("Time per Flash = 0.2", "Time per Flash = bad"), "delayed_emission"),
            ("Flash Count", delayed_note.replace("Flash Count = 100", "Flash Count = bad"), "delayed_emission"),
        )

        for index, (field_label, note_text, spectrum_class) in enumerate(cases):
            with self.subTest(field_label=field_label):
                converted = convert_extracted_results(
                    (
                        _terminal_result(
                            short_name=f"Bad{index}",
                            note_text=note_text,
                            spectrum_class=spectrum_class,
                            selected_x_values=() if spectrum_class == "steady_2d" else (300, 301),
                            selected_y_values=() if spectrum_class == "steady_2d" else (10, 12),
                        ),
                    ),
                    source_filenames={"S1": "source.opj"},
                    expected_source_ids=("S1",),
                )

                self.assertEqual((), converted.ordinary_candidates)
                self.assertEqual((), converted.steady_2d_candidates)
                self.assertEqual(1, len(converted.rejections))
                self.assertIn("invalid numeric", converted.rejections[0].reason)
                self.assertIn(field_label, converted.rejections[0].reason)

    def test_required_note_numeric_metadata_preserves_valid_decimal_and_scientific_text(self):
        converted = convert_extracted_results(
            (
                _terminal_result(
                    note_text=(
                        "[EXP_FD_FILE]\nAcquisition Type = Spectral Acquisition[Emission]\n"
                        "[EX1]\nPark = 2.70e2\n"
                        "Front Entrance Slit = 1e-3\nFront Exit Slit = 1e-3\n"
                        "[EM1]\nStart = 300.5\nEnd = 6.5025e2\nIncrement = 5e-1\n"
                        "Front Entrance Slit = 3e-3\nFront Exit Slit = 3e-3"
                    ),
                ),
            ),
            source_filenames={"S1": "source.opj"},
            expected_source_ids=("S1",),
        )

        self.assertEqual(1, len(converted.ordinary_candidates))
        candidate = converted.ordinary_candidates[0]
        self.assertEqual("2.70e2", candidate.fixed_wavelength)
        self.assertEqual(("300.5", "6.5025e2"), candidate.wavelength_range)
        self.assertEqual("5e-1", candidate.scan_increment)
        self.assertEqual((), converted.rejections)

    def test_required_note_numeric_metadata_rejects_unrenderable_magnitude_and_negative_slit(self):
        cases = (
            (
                "magnitude",
                "[EXP_FD_FILE]\nAcquisition Type = Spectral Acquisition[Emission]\n"
                "[EX1]\nPark = 1e1000000\n"
                "Front Entrance Slit = 2\nFront Exit Slit = 2\n"
                "[EM1]\nStart = 300\nEnd = 650\nIncrement = 1\n"
                "Front Entrance Slit = 2\nFront Exit Slit = 2",
                "fixed excitation wavelength",
            ),
            (
                "negative slit",
                "[EXP_FD_FILE]\nAcquisition Type = Spectral Acquisition[Emission]\n"
                "[EX1]\nPark = 270\n"
                "Front Entrance Slit = -1\nFront Exit Slit = -1\n"
                "[EM1]\nStart = 300\nEnd = 650\nIncrement = 1\n"
                "Front Entrance Slit = 2\nFront Exit Slit = 2",
                "excitation front entrance slit",
            ),
        )

        for index, (label, note_text, field) in enumerate(cases):
            with self.subTest(label=label):
                converted = convert_extracted_results(
                    (
                        _terminal_result(
                            short_name=f"BadDomain{index}",
                            note_text=note_text,
                        ),
                    ),
                    source_filenames={"S1": "source.opj"},
                    expected_source_ids=("S1",),
                )

                self.assertEqual((), converted.ordinary_candidates)
                self.assertEqual(1, len(converted.rejections))
                self.assertIn("invalid numeric", converted.rejections[0].reason)
                self.assertIn(field, converted.rejections[0].reason)

    def test_required_note_numeric_metadata_rejects_underscore_forms(self):
        for index, value in enumerate(("_270_", "3__00", "650_", "1_e0")):
            with self.subTest(value=value):
                converted = convert_extracted_results(
                    (
                        _terminal_result(
                            short_name=f"BadUnderscore{index}",
                            note_text=(
                                "[EXP_FD_FILE]\nAcquisition Type = Spectral Acquisition[Emission]\n"
                                f"Excitation Wavelength = {value}\n"
                                "Emission Range = 300 - 650\nEmission Increment = 1"
                            ),
                        ),
                    ),
                    source_filenames={"S1": "source.opj"},
                    expected_source_ids=("S1",),
                )

                self.assertEqual((), converted.ordinary_candidates)
                self.assertEqual(1, len(converted.rejections))
                self.assertIn("invalid numeric fixed excitation wavelength", converted.rejections[0].reason)

    def test_required_note_numeric_metadata_rejects_non_ascii_digits(self):
        for index, value in enumerate(("\uff12\uff17\uff10", "\u0662\u0667\u0660", "2\u06670")):
            with self.subTest(value=value):
                converted = convert_extracted_results(
                    (
                        _terminal_result(
                            short_name=f"BadUnicodeDigits{index}",
                            note_text=(
                                "[EXP_FD_FILE]\nAcquisition Type = Spectral Acquisition[Emission]\n"
                                f"Excitation Wavelength = {value}\n"
                                "Emission Range = 300 - 650\nEmission Increment = 1"
                            ),
                        ),
                    ),
                    source_filenames={"S1": "source.opj"},
                    expected_source_ids=("S1",),
                )

                self.assertEqual((), converted.ordinary_candidates)
                self.assertEqual(1, len(converted.rejections))
                self.assertIn("invalid numeric fixed excitation wavelength", converted.rejections[0].reason)

    def test_zero_recognizable_supported_books_in_one_selected_source_aborts_entire_run_before_attribution(self):
        invalid = _terminal_result(
            source_id="S2",
            note_text="not an instrument Note",
            status="rejected",
            rejection_reason="Unsupported acquisition type",
        )

        with self.assertRaises(CandidateConversionError):
            convert_extracted_results(
                (invalid,),
                source_filenames={"S2": "organized.opju"},
                expected_source_ids=("S2",),
            )

    def test_one_zero_recognizable_source_aborts_multi_source_batch(self):
        valid = _terminal_result(source_id="S1")
        invalid = _terminal_result(
            source_id="S2",
            note_text="not an instrument Note",
            status="rejected",
            rejection_reason="Unsupported acquisition type",
        )

        with self.assertRaises(CandidateConversionError):
            convert_extracted_results(
                (valid, invalid),
                source_filenames={"S1": "raw-one.opj", "S2": "organized.opju"},
                expected_source_ids=("S1", "S2"),
            )

    def test_unexpected_source_aborts_candidate_conversion(self):
        with self.assertRaises(CandidateConversionError):
            convert_extracted_results(
                (_terminal_result(source_id="S1"), _terminal_result(source_id="S2")),
                source_filenames={"S1": "one.opj", "S2": "two.opj"},
                expected_source_ids=("S1",),
            )

    def test_missing_source_filename_provenance_aborts_candidate_conversion(self):
        with self.assertRaises(CandidateConversionError):
            convert_extracted_results(
                (_terminal_result(),),
                source_filenames={},
                expected_source_ids=("S1",),
            )

    def test_missing_source_filename_provenance_closes_closeable_result_stream(self):
        class CloseableResults:
            def __init__(self):
                self.closed = False

            def __iter__(self):
                return iter((_terminal_result(),))

            def close(self):
                self.closed = True

        results = CloseableResults()
        with self.assertRaises(CandidateConversionError):
            convert_extracted_results(
                results,
                source_filenames={},
                expected_source_ids=("S1",),
            )
        self.assertTrue(results.closed)

    def test_invalid_stored_spectrum_class_aborts_candidate_conversion(self):
        with self.assertRaises(CandidateConversionError):
            convert_extracted_results(
                (_terminal_result(spectrum_class="not-a-class"),),
                source_filenames={"S1": "source.opj"},
                expected_source_ids=("S1",),
            )

    def test_candidate_identity_includes_page_type(self):
        worksheet = _terminal_result(page_type="worksheet")
        matrix = _terminal_result(page_type="matrix")

        converted = convert_extracted_results(
            (worksheet, matrix),
            source_filenames={"S1": "source.opj"},
            expected_source_ids=("S1",),
        )

        self.assertEqual(
            (
                json.dumps(["S1", "worksheet", "Folder", "Book1"], ensure_ascii=False, separators=(",", ":")),
                json.dumps(["S1", "matrix", "Folder", "Book1"], ensure_ascii=False, separators=(",", ":")),
            ),
            tuple(candidate.book_key for candidate in converted.ordinary_candidates),
        )

    def test_downstream_selection_identity_includes_page_type(self):
        worksheet = _emission("Book1", page_type="worksheet")
        matrix = _emission("Book1", page_type="matrix")

        self.assertNotEqual(worksheet.book_key, matrix.book_key)

    def test_candidate_identity_encoding_cannot_collide_when_components_contain_pipes(self):
        first = _terminal_result(folder_path="F|A", short_name="B")
        second = _terminal_result(folder_path="F", short_name="A|B")

        converted = convert_extracted_results(
            (first, second),
            source_filenames={"S1": "source.opj"},
            expected_source_ids=("S1",),
        )

        keys = tuple(candidate.book_key for candidate in converted.ordinary_candidates)
        self.assertEqual(2, len(set(keys)))

    def test_selection_book_identity_cannot_collide_when_components_contain_pipes(self):
        first = _emission("B", source_id="S", folder="F|A")
        second = _emission("A|B", source_id="S", folder="F")

        self.assertNotEqual(first.book_key, second.book_key)

        pending = review_emission_duplicates([first, second])
        resolved = review_emission_duplicates(
            [first, second],
            choices={pending.pending_reviews[0].review_key: second.book_key},
        )

        self.assertEqual((), resolved.pending_reviews)
        self.assertEqual((second.book_key,), resolved.selected_book_keys)

    def test_candidate_display_formats_tied_maximum_x_values_without_losing_summary(self):
        three = convert_extracted_results(
            (_terminal_result(max_planned_y_x=(302, 300, 301)),),
            source_filenames={"S1": "source.opj"},
            expected_source_ids=("S1",),
        ).ordinary_candidates[0]
        four = convert_extracted_results(
            (_terminal_result(max_planned_y_x=(300, 301, 302, 303)),),
            source_filenames={"S1": "source.opj"},
            expected_source_ids=("S1",),
        ).ordinary_candidates[0]

        self.assertEqual("300, 301, 302", dict(build_review_candidate_display(three))["x_at_max_y"])
        self.assertEqual("300 - 303（4 个并列最大值）", dict(build_review_candidate_display(four))["x_at_max_y"])

    def test_candidate_conversion_closes_stream_when_it_aborts(self):
        closed = []

        def result_stream():
            try:
                yield _terminal_result(source_id="S2")
            finally:
                closed.append(True)

        with self.assertRaises(CandidateConversionError):
            convert_extracted_results(
                result_stream(),
                source_filenames={"S1": "source.opj"},
                expected_source_ids=("S1",),
            )

        self.assertEqual([True], closed)

    def test_emission_duplicates_resolve_inside_source_before_cross_source_review(self):
        internal_a = _emission("A", source_id="S1", folder="PE", excitation="300")
        internal_b = _emission("B", source_id="S1", folder="PE", excitation="300")
        cross = _emission("C", source_id="S2", folder="OtherPE", excitation="300")

        first = review_emission_duplicates([internal_a, internal_b, cross])

        self.assertEqual(1, len(first.pending_reviews))
        self.assertEqual("stage1", first.pending_reviews[0].stage)
        self.assertEqual((internal_a.book_key, internal_b.book_key), first.pending_reviews[0].book_keys)
        self.assertEqual((), first.selected_book_keys)

        second = review_emission_duplicates(
            [internal_a, internal_b, cross],
            choices={first.pending_reviews[0].review_key: internal_b.book_key},
        )

        self.assertEqual(1, len(second.pending_reviews))
        self.assertEqual("stage2", second.pending_reviews[0].stage)
        self.assertTrue(second.pending_reviews[0].allow_return_to_attribution)
        self.assertIn("返回样品归属步骤", second.pending_reviews[0].actions)
        self.assertEqual((internal_b.book_key, cross.book_key), second.pending_reviews[0].book_keys)
        self.assertEqual((), second.exclusions)

        resolved = review_emission_duplicates(
            [internal_a, internal_b, cross],
            choices={
                first.pending_reviews[0].review_key: internal_b.book_key,
                second.pending_reviews[0].review_key: internal_b.book_key,
            },
        )

        self.assertEqual((), resolved.pending_reviews)
        self.assertEqual((internal_a.book_key, cross.book_key), tuple(record.book_key for record in resolved.exclusions))
        self.assertEqual(("emission_duplicate_unselected", "emission_duplicate_unselected"), tuple(record.reason for record in resolved.exclusions))

    def test_same_source_emissions_in_different_folders_enter_second_stage_review(self):
        first = _emission("A", source_id="S1", folder="FolderA", excitation="300")
        second = _emission("B", source_id="S1", folder="FolderB", excitation="300")

        result = review_emission_duplicates([first, second])

        self.assertEqual(1, len(result.pending_reviews))
        self.assertEqual("stage2", result.pending_reviews[0].stage)
        self.assertTrue(result.pending_reviews[0].allow_return_to_attribution)
        self.assertEqual(
            (first.book_key, second.book_key),
            result.pending_reviews[0].book_keys,
        )
        self.assertEqual((), result.selected_book_keys)
        self.assertEqual((), result.exclusions)

    def test_review_keys_are_unambiguous_when_book_keys_contain_pipes_and_still_accept_choices(self):
        first_a = _excitation("A", source_id="S", folder="F", sample_system="CollisionOne", fixed_receiving_wavelength="450")
        first_b = _excitation("B|C", source_id="S", folder="F", sample_system="CollisionOne", fixed_receiving_wavelength="460")
        second_a = _excitation("A|S", source_id="S", folder="F", sample_system="CollisionTwo", fixed_receiving_wavelength="450")
        second_b = _excitation("C", source_id="F", folder="B", sample_system="CollisionTwo", fixed_receiving_wavelength="460")

        pending = select_excitation_candidates([first_a, first_b, second_a, second_b])

        self.assertEqual(2, len(pending.pending_reviews))
        self.assertNotEqual(pending.pending_reviews[0].review_key, pending.pending_reviews[1].review_key)

        resolved = select_excitation_candidates(
            [first_a, first_b, second_a, second_b],
            choices={
                pending.pending_reviews[0].review_key: (first_a.book_key,),
                pending.pending_reviews[1].review_key: (second_b.book_key,),
            },
        )

        self.assertEqual((), resolved.pending_reviews)
        self.assertEqual((first_a.book_key, second_b.book_key), resolved.selected_book_keys)

    def test_cross_source_conflict_popup_offers_return_to_attribution_path(self):
        request = cross_source_emission_conflict_dialog(("S1|PE|A", "S2|PE|A"))

        self.assertEqual("cross_source_emission_conflict", request.kind)
        self.assertIn("返回样品归属步骤", request.actions)

    def test_emission_duplicate_keys_include_steady_and_delayed_identity(self):
        steady_a = _emission("A", excitation="300", excitation_slit="2", emission_slit="3")
        steady_b = _emission("B", excitation="300", excitation_slit="2", emission_slit="3")
        different_slit = _emission("C", excitation="300", excitation_slit="2", emission_slit="5")
        delayed_a = _emission("D1", spectrum_class=SpectrumClass.DELAYED_EMISSION, flash_delay="0.1", sample_window="1", time_per_flash="1.1", flash_count="100")
        delayed_b = _emission("D2", spectrum_class=SpectrumClass.DELAYED_EMISSION, flash_delay="0.1", sample_window="1", time_per_flash="1.1", flash_count="200")

        result = review_emission_duplicates([steady_a, steady_b, different_slit, delayed_a, delayed_b])

        self.assertEqual(1, len(result.pending_reviews))
        self.assertEqual((steady_a.book_key, steady_b.book_key), result.pending_reviews[0].book_keys)
        equivalent = review_emission_duplicates(
            [
                _emission("NumericA", excitation="3e2", excitation_slit="2.0/2.00", emission_slit="3.00/3.0"),
                _emission("NumericB", excitation="300.00", excitation_slit="2/2", emission_slit="3/3"),
            ]
        )
        self.assertEqual(1, len(equivalent.pending_reviews))
        delayed_equivalent = review_emission_duplicates(
            [
                _emission(
                    "DelayedNumericA",
                    spectrum_class=SpectrumClass.DELAYED_EMISSION,
                    excitation="3e2",
                    excitation_slit="2.0/2.00",
                    emission_slit="3.00/3.0",
                    flash_delay="1e-1",
                    sample_window="1.0",
                    time_per_flash="1.10",
                    flash_count="1e2",
                ),
                _emission(
                    "DelayedNumericB",
                    spectrum_class=SpectrumClass.DELAYED_EMISSION,
                    excitation="300.00",
                    excitation_slit="2/2",
                    emission_slit="3/3",
                    flash_delay="0.10",
                    sample_window="1",
                    time_per_flash="1.1",
                    flash_count="100.0",
                ),
            ]
        )
        self.assertEqual(1, len(delayed_equivalent.pending_reviews))

    def test_candidate_display_order_includes_source_folder_names_type_slits_delays_peak_and_note_datetime(self):
        candidate = _emission(
            "A",
            source_filename="source.opju",
            folder="Folder/PE",
            display_name="  User Name  ",
            default_name="Book1",
            excitation_slit="2",
            emission_slit="3",
            flash_delay="0.1 ms",
            sample_window="1 ms",
            time_per_flash="1.1 ms",
            flash_count="100",
            x_at_max_y="520",
            max_y="1234",
            note_datetime="2026-06-27 12:30",
        )

        fields = build_candidate_display(candidate)

        self.assertEqual(
            (
                ("source_filename", "source.opju"),
                ("folder_path", "Folder/PE"),
                ("book_name", "  User Name  "),
                ("spectrum_type", "steady_emission"),
                ("slits", "Ex 2 / Em 3"),
                ("delay_parameters", "Flash Delay 0.1 ms; Sample Window 1 ms; Time per Flash 1.1 ms; Flash Count 100"),
                ("x_at_max_y", "520"),
                ("max_y", "1234"),
                ("note_datetime", "2026-06-27 12:30"),
            ),
            fields,
        )
        self.assertNotIn("Book1", dict(fields).values())

    def test_emission_duplicate_dialog_message_uses_candidate_display_order(self):
        candidate = _emission("A", source_filename="source.opju", x_at_max_y="520", max_y="1234")

        request = emission_duplicate_review_dialog("stage1", (candidate,))

        self.assertEqual("emission_duplicate_review", request.kind)
        self.assertEqual(("select_one",), request.actions)
        self.assertLess(request.message.index("source_filename: source.opju"), request.message.index("x_at_max_y: 520"))
        self.assertLess(request.message.index("x_at_max_y: 520"), request.message.index("max_y: 1234"))

    def test_steady_excitation_groups_by_sample_system_and_temperature_only(self):
        auto_candidate = _excitation("A", sample_system="MFL-film", temperature="298 K")
        manual_candidates = [
            _excitation("A", sample_system="MFL-film", temperature="298 K", fixed_receiving_wavelength="450", excitation_slit="1"),
            _excitation("B", sample_system="MFL-film", temperature="298 K", fixed_receiving_wavelength="460", excitation_slit="5"),
        ]
        auto = select_excitation_candidates([auto_candidate])
        manual = select_excitation_candidates(manual_candidates)

        self.assertEqual((auto_candidate.book_key,), auto.selected_book_keys)
        self.assertEqual((), auto.pending_reviews)
        self.assertEqual(1, len(manual.pending_reviews))
        self.assertEqual("excitation_selection", manual.pending_reviews[0].kind)
        self.assertEqual(tuple(candidate.book_key for candidate in manual_candidates), manual.pending_reviews[0].book_keys)
        self.assertEqual(("fixed_receiving_wavelength", "excitation_slit", "emission_slit", "flash_count"), manual.pending_reviews[0].comparison_fields[:4])

    def test_excitation_selection_preserves_the_explicit_choice_order(self):
        first = _excitation("A", fixed_receiving_wavelength="450")
        second = _excitation("B", fixed_receiving_wavelength="460")
        pending = select_excitation_candidates([first, second])

        resolved = select_excitation_candidates(
            [first, second],
            choices={
                pending.pending_reviews[0].review_key: (
                    second.book_key,
                    first.book_key,
                )
            },
        )

        self.assertEqual(
            (second.book_key, first.book_key),
            resolved.selected_book_keys,
        )

    def test_delayed_excitation_group_ignores_flash_count_and_slits_but_splits_delay_core(self):
        first = _excitation("A", spectrum_class=SpectrumClass.DELAYED_EXCITATION, flash_delay="0.1", sample_window="1", time_per_flash="1.1", flash_count="100", excitation_slit="1")
        second = _excitation("B", spectrum_class=SpectrumClass.DELAYED_EXCITATION, flash_delay="0.1", sample_window="1", time_per_flash="1.1", flash_count="200", excitation_slit="5")
        other_delay = _excitation("C", spectrum_class=SpectrumClass.DELAYED_EXCITATION, flash_delay="0.2", sample_window="1", time_per_flash="1.2")

        result = select_excitation_candidates([first, second, other_delay])

        self.assertEqual(1, len(result.pending_reviews))
        self.assertEqual((first.book_key, second.book_key), result.pending_reviews[0].book_keys)
        self.assertEqual((), result.selected_book_keys)
        self.assertEqual((), result.completeness_book_keys)

    def test_pending_excitation_review_does_not_expose_other_auto_selection_as_approved_baseline(self):
        auto = _excitation("A", sample_system="PFL-film")
        manual_a = _excitation("B", sample_system="MFL-film", fixed_receiving_wavelength="450")
        manual_b = _excitation("C", sample_system="MFL-film", fixed_receiving_wavelength="460")

        result = select_excitation_candidates([auto, manual_a, manual_b])

        self.assertEqual(1, len(result.pending_reviews))
        self.assertEqual((), result.selected_book_keys)
        self.assertEqual((), result.completeness_book_keys)

    def test_filter_copyable_emissions_after_special_keeps_steady_and_regular_delayed_only(self):
        steady = _emission("Steady", spectrum_class=SpectrumClass.STEADY_EMISSION)
        special_delayed = _emission("Special", spectrum_class=SpectrumClass.DELAYED_EMISSION)
        regular_delayed = _emission("Regular", spectrum_class=SpectrumClass.DELAYED_EMISSION)
        excitation = _excitation("Ex")

        filtered = filter_copyable_emissions_after_special(
            [steady, special_delayed, regular_delayed, excitation],
            regular_delayed_book_keys=(regular_delayed.book_key,),
            special_group_book_keys=(special_delayed.book_key,),
        )

        self.assertEqual((steady.book_key, regular_delayed.book_key), tuple(spectrum.book_key for spectrum in filtered))

    def test_exact_excitation_duplicate_requires_single_select_and_unselected_is_excluded(self):
        first = _excitation("A", scan_start="300", scan_stop="500", scan_step="1")
        duplicate = _excitation("B", scan_start="300", scan_stop="500", scan_step="1")

        pending = select_excitation_candidates([first, duplicate])

        self.assertEqual(1, len(pending.pending_reviews))
        self.assertEqual("excitation_selection", pending.pending_reviews[0].kind)
        self.assertEqual(("select_one_or_more",), pending.pending_reviews[0].actions)
        self.assertEqual(
            ((first.book_key, duplicate.book_key),),
            pending.pending_reviews[0].single_select_groups,
        )

        resolved = select_excitation_candidates(
            [first, duplicate],
            choices={pending.pending_reviews[0].review_key: (duplicate.book_key,)},
        )

        self.assertEqual((duplicate.book_key,), resolved.selected_book_keys)
        self.assertEqual((first.book_key,), tuple(record.book_key for record in resolved.exclusions))
        self.assertEqual(("exact_excitation_duplicate_unselected",), tuple(record.reason for record in resolved.exclusions))
        numeric_equivalent = select_excitation_candidates(
            [
                _excitation(
                    "NumericA",
                    fixed_receiving_wavelength="4.5e2",
                    excitation_slit="2.0/2.00",
                    emission_slit="3.00/3.0",
                    scan_start="3e2",
                    scan_stop="500.0",
                    scan_step="1.00",
                ),
                _excitation(
                    "NumericB",
                    fixed_receiving_wavelength="450.00",
                    excitation_slit="2/2",
                    emission_slit="3/3",
                    scan_start="300.0",
                    scan_stop="5e2",
                    scan_step="1",
                ),
            ]
        )
        self.assertEqual(
            (numeric_equivalent.pending_reviews[0].book_keys,),
            numeric_equivalent.pending_reviews[0].single_select_groups,
        )
        delayed_numeric_equivalent = select_excitation_candidates(
            [
                _excitation(
                    "DelayedNumericA",
                    spectrum_class=SpectrumClass.DELAYED_EXCITATION,
                    fixed_receiving_wavelength="4.5e2",
                    excitation_slit="2.0/2.00",
                    emission_slit="3.00/3.0",
                    flash_delay="1e-1",
                    sample_window="1.0",
                    time_per_flash="1.10",
                    flash_count="1e2",
                    scan_start="3e2",
                    scan_stop="500.0",
                    scan_step="1.00",
                ),
                _excitation(
                    "DelayedNumericB",
                    spectrum_class=SpectrumClass.DELAYED_EXCITATION,
                    fixed_receiving_wavelength="450.00",
                    excitation_slit="2/2",
                    emission_slit="3/3",
                    flash_delay="0.10",
                    sample_window="1",
                    time_per_flash="1.1",
                    flash_count="100.0",
                    scan_start="300.0",
                    scan_stop="5e2",
                    scan_step="1",
                ),
            ]
        )
        self.assertEqual(
            (delayed_numeric_equivalent.pending_reviews[0].book_keys,),
            delayed_numeric_equivalent.pending_reviews[0].single_select_groups,
        )

    def test_pending_excitation_review_does_not_expose_resolved_exact_duplicate_exclusions(self):
        exact_a = _excitation("ExactA", sample_system="MFL-film", fixed_receiving_wavelength="450")
        exact_b = _excitation("ExactB", sample_system="MFL-film", fixed_receiving_wavelength="450")
        manual_a = _excitation("ManualA", sample_system="PFL-film", fixed_receiving_wavelength="450")
        manual_b = _excitation("ManualB", sample_system="PFL-film", fixed_receiving_wavelength="460")
        initial = select_excitation_candidates([exact_a, exact_b, manual_a, manual_b])
        self.assertEqual(2, len(initial.pending_reviews))
        exact_review = next(
            review
            for review in initial.pending_reviews
            if exact_a.book_key in review.book_keys
        )
        manual_review = next(
            review
            for review in initial.pending_reviews
            if manual_a.book_key in review.book_keys
        )

        resolved = select_excitation_candidates(
            [exact_a, exact_b, manual_a, manual_b],
            choices={
                exact_review.review_key: (exact_b.book_key,),
                manual_review.review_key: (manual_a.book_key,),
            },
        )

        self.assertEqual((), resolved.pending_reviews)
        self.assertEqual((exact_a.book_key, manual_b.book_key), tuple(record.book_key for record in resolved.exclusions))

    def test_excitation_popup_allows_zero_or_one_from_exact_duplicate_subgroup(self):
        exact_a = _excitation("ExactA", fixed_receiving_wavelength="450")
        exact_b = _excitation("ExactB", fixed_receiving_wavelength="450")
        distinct = _excitation("Distinct", fixed_receiving_wavelength="460")
        pending = select_excitation_candidates([exact_a, exact_b, distinct])
        review = pending.pending_reviews[0]

        no_exact = select_excitation_candidates(
            [exact_a, exact_b, distinct],
            choices={review.review_key: (distinct.book_key,)},
        )
        two_exact = select_excitation_candidates(
            [exact_a, exact_b, distinct],
            choices={
                review.review_key: (
                    exact_a.book_key,
                    exact_b.book_key,
                    distinct.book_key,
                )
            },
        )

        self.assertEqual((), no_exact.pending_reviews)
        self.assertEqual((distinct.book_key,), no_exact.selected_book_keys)
        self.assertEqual((review,), two_exact.pending_reviews)

    def test_unselected_excitation_candidates_are_report_exclusions_not_baseline(self):
        first = _excitation("A", fixed_receiving_wavelength="450")
        second = _excitation("B", fixed_receiving_wavelength="460")

        pending = select_excitation_candidates([first, second])
        resolved = select_excitation_candidates(
            [first, second],
            choices={pending.pending_reviews[0].review_key: (first.book_key,)},
        )

        self.assertEqual((first.book_key,), resolved.selected_book_keys)
        self.assertEqual((first.book_key,), resolved.completeness_book_keys)
        self.assertEqual((second.book_key,), tuple(record.book_key for record in resolved.exclusions))
        self.assertEqual(("excitation_candidate_unselected",), tuple(record.reason for record in resolved.exclusions))


def _emission(
    name,
    *,
    source_id="S1",
    source_filename="source.opju",
    folder="PE",
    spectrum_class=SpectrumClass.STEADY_EMISSION,
    sample_system="MFL-film",
    temperature="298 K",
    excitation="300",
    excitation_slit="2",
    emission_slit="3",
    display_name=None,
    default_name=None,
    flash_delay=None,
    sample_window=None,
    time_per_flash=None,
    flash_count=None,
    x_at_max_y=None,
    max_y=None,
    note_datetime=None,
    page_type="worksheet",
):
    return SelectionSpectrum(
        source_id=source_id,
        source_filename=source_filename,
        folder_path=folder,
        book_name=name,
        display_name=display_name or name,
        default_name=default_name or name,
        spectrum_class=spectrum_class,
        sample_system=sample_system,
        temperature=temperature,
        fixed_excitation_wavelength=excitation,
        excitation_slit=excitation_slit,
        emission_slit=emission_slit,
        flash_delay=flash_delay,
        sample_window=sample_window,
        time_per_flash=time_per_flash,
        flash_count=flash_count,
        x_at_max_y=x_at_max_y,
        max_y=max_y,
        note_datetime=note_datetime,
        page_type=page_type,
    )


def _excitation(
    name,
    *,
    source_id="S1",
    folder="Ex",
    spectrum_class=SpectrumClass.STEADY_EXCITATION,
    sample_system="MFL-film",
    temperature="298 K",
    fixed_receiving_wavelength="450",
    excitation_slit="2",
    emission_slit="3",
    flash_delay=None,
    sample_window=None,
    time_per_flash=None,
    flash_count=None,
    scan_start="300",
    scan_stop="500",
    scan_step="1",
):
    return SelectionSpectrum(
        source_id=source_id,
        source_filename="source.opju",
        folder_path=folder,
        book_name=name,
        display_name=name,
        default_name=name,
        spectrum_class=spectrum_class,
        sample_system=sample_system,
        temperature=temperature,
        fixed_receiving_wavelength=fixed_receiving_wavelength,
        excitation_slit=excitation_slit,
        emission_slit=emission_slit,
        flash_delay=flash_delay,
        sample_window=sample_window,
        time_per_flash=time_per_flash,
        flash_count=flash_count,
        scan_start=scan_start,
        scan_stop=scan_stop,
        scan_step=scan_step,
    )


def _terminal_result(**overrides):
    values = {
        "source_id": "S1",
        "folder_path": "Folder",
        "short_name": "Book1",
        "status": "extracted",
        "note_text": (
            "[EXP_FD_FILE]\nAcquisition Type = Spectral Acquisition[Emission]\n"
            "[EX1]\nPark = 270\nFront Entrance Slit = 2\nFront Exit Slit = 2\n"
            "[EM1]\nStart = 300\nEnd = 650\nIncrement = 1\n"
            "Front Entrance Slit = 2\nFront Exit Slit = 2"
        ),
        "display_name": "Display",
        "page_order": 1,
        "spectrum_class": "steady_emission",
        "data_sheet_name": "Data",
        "available_columns": ("X", "S1c", "S1"),
        "selected_y_column": "S1c",
        "paired_x_column": "X",
        "selected_x_values": (300, 301),
        "selected_y_values": (10, 12),
        "selected_x_row_count": 2,
        "selected_y_row_count": 2,
        "max_planned_y": 12,
        "max_planned_y_x": 301,
        "s1_max_for_limit": 100,
        "s1_limit_status": "ok",
        "data_checksum": "checksum",
    }
    values.update(overrides)
    return TerminalBookResult(**values)


if __name__ == "__main__":
    unittest.main()
