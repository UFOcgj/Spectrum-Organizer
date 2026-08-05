from decimal import Decimal
import pathlib
import re
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from spectrum_organizer.core.output_model import (
    OutputSpectrum,
    _natural_key,
    build_output_plan,
)
from spectrum_organizer.domain.models import SpectrumClass
class OutputModelTests(unittest.TestCase):
    def test_book_natural_sort_is_total_for_prefix_and_case_variants(self):
        spectra = tuple(
            _spectrum(
                f"em-{label}",
                SpectrumClass.STEADY_EMISSION,
                label,
                label,
                "298 K",
                key_wavelength="270",
            )
            for label in ("A1", "A")
        )

        plan = build_output_plan(spectra)

        self.assertEqual(
            ("A", "A1"),
            tuple(book.display_name for book in plan.folders[0].books),
        )
        self.assertEqual(
            ("A", "a", "A1", "A2", "A10", "²"),
            tuple(
                sorted(
                    ("A10", "²", "A2", "A1", "a", "A"),
                    key=_natural_key,
                )
            ),
        )

    def test_emission_folders_drive_copying_fallbacks_order_and_completeness(self):
        f_emission = _spectrum("f-em", SpectrumClass.STEADY_EMISSION, "A-298 K", "A", "298 K", key_wavelength="270")
        f_excitation = _spectrum(
            "f-ex",
            SpectrumClass.STEADY_EXCITATION,
            "A-298 K",
            "A",
            "298 K",
            key_wavelength="315",
            excitation_slit="9",
            emission_slit="9",
        )
        f_orphan_excitation = _spectrum(
            "f-orphan-ex",
            SpectrumClass.STEADY_EXCITATION,
            "B-298 K",
            "B",
            "298 K",
            key_wavelength="315",
        )
        p_emission = _spectrum(
            "p-em",
            SpectrumClass.DELAYED_EMISSION,
            "C-77 K",
            "C",
            "77 K",
            key_wavelength="270",
            flash_delay="0.100",
            sample_window="1.0",
            time_per_flash="0.1",
            flash_count="100",
        )
        p_excitation = _spectrum(
            "p-ex",
            SpectrumClass.DELAYED_EXCITATION,
            "C-77 K",
            "C",
            "77 K",
            key_wavelength="315",
            excitation_slit="7",
            emission_slit="8",
            flash_delay="0.1",
            sample_window="1",
            time_per_flash="0.10",
            flash_count="200",
        )

        plan = build_output_plan((f_orphan_excitation, p_excitation, f_excitation, p_emission, f_emission))

        self.assertEqual(
            (
                "F_Ex270_ExSlit2_EmSlit2",
                "F_Em315_ExSlit2_EmSlit2",
                "P_Ex270_ExSlit2_EmSlit2_FD0.1_SW1_TPF0.1_FC100_ALL_SAMPLES",
            ),
            tuple(folder.name for folder in plan.folders),
        )
        self.assertNotIn("F_Em315_ExSlit9_EmSlit9", tuple(folder.name for folder in plan.folders))

        f_folder = plan.folder("F_Ex270_ExSlit2_EmSlit2")
        self.assertFalse(f_folder.is_fallback)
        self.assertEqual(("A",), tuple(book.display_name for book in f_folder.books))
        self.assertEqual(("A-298 K",), plan.incomplete_folders[0].represented_labels)
        self.assertEqual(("B-298 K",), plan.incomplete_folders[0].missing_labels)

        f_columns = f_folder.books[0].columns
        self.assertIn("A-298 K_F270", tuple(column.comment for column in f_columns))
        self.assertIn("A-298 K_FEx315", tuple(column.comment for column in f_columns))

        fallback = plan.folder("F_Em315_ExSlit2_EmSlit2")
        self.assertTrue(fallback.is_fallback)
        self.assertEqual(("B",), tuple(book.display_name for book in fallback.books))
        self.assertNotIn(fallback.name, tuple(entry.folder_name for entry in plan.incomplete_folders))
        self.assertFalse(fallback.name.endswith("_ALL_SAMPLES"))

        p_folder = plan.folder("P_Ex270_ExSlit2_EmSlit2_FD0.1_SW1_TPF0.1_FC100_ALL_SAMPLES")
        self.assertEqual(("C-77 K_PEx315",), tuple(column.comment for column in p_folder.books[0].raw_y_columns if "_PEx" in column.comment))

    def test_books_merge_temperature_only_and_align_exact_x_unions_with_column_metadata(self):
        spectra = (
            _spectrum(
                "em-77",
                SpectrumClass.STEADY_EMISSION,
                "MFL-mTHF-1×10^-4 M-77 K",
                "MFL-mTHF-1×10^-4 M",
                "77 K",
                key_wavelength="270",
                x_y=(("300", "10"), ("301", "20")),
            ),
            _spectrum(
                "em-298",
                SpectrumClass.STEADY_EMISSION,
                "MFL-mTHF-1×10^-4 M-298 K",
                "MFL-mTHF-1×10^-4 M",
                "298 K",
                key_wavelength="270",
                x_y=(("300.5", "5"), ("301", "15")),
            ),
            _spectrum(
                "ex-77",
                SpectrumClass.STEADY_EXCITATION,
                "MFL-mTHF-1×10^-4 M-77 K",
                "MFL-mTHF-1×10^-4 M",
                "77 K",
                key_wavelength="315",
                x_y=(("250", "2"), ("251", "4")),
            ),
            _spectrum(
                "ex-298",
                SpectrumClass.STEADY_EXCITATION,
                "MFL-mTHF-1×10^-4 M-298 K",
                "MFL-mTHF-1×10^-4 M",
                "298 K",
                key_wavelength="315",
                x_y=(("250.5", "8"), ("251", "16")),
            ),
        )

        plan = build_output_plan(spectra)

        book = plan.folder("F_Ex270_ExSlit2_EmSlit2_ALL_SAMPLES").books[0]
        self.assertEqual("MFL-mTHF-1×10^-4 M", book.display_name)
        self.assertEqual(
            (
                ("x", "Em", (Decimal("300"), Decimal("300.5"), Decimal("301")), None, None),
                ("raw_y", "MFL-mTHF-1×10^-4 M-77 K_F270", (Decimal("10"), None, Decimal("20")), None, None),
                ("raw_y", "MFL-mTHF-1×10^-4 M-298 K_F270", (None, Decimal("5"), Decimal("15")), None, None),
                ("norm_y", "MFL-mTHF-1×10^-4 M-77 K_F270_Norm", (Decimal("0.5"), None, Decimal("1")), "Divided by Max of B", "col(B)/max(col(B))"),
                ("norm_y", "MFL-mTHF-1×10^-4 M-298 K_F270_Norm", (None, Decimal("0.3333333333333333333333333333"), Decimal("1")), "Divided by Max of C", "col(C)/max(col(C))"),
                ("x", "Ex", (Decimal("250"), Decimal("250.5"), Decimal("251")), None, None),
                ("raw_y", "MFL-mTHF-1×10^-4 M-77 K_FEx315", (Decimal("2"), None, Decimal("4")), None, None),
                ("raw_y", "MFL-mTHF-1×10^-4 M-298 K_FEx315", (None, Decimal("8"), Decimal("16")), None, None),
                ("norm_y", "MFL-mTHF-1×10^-4 M-77 K_FEx315_Norm", (Decimal("0.5"), None, Decimal("1")), "Divided by Max of G", "col(G)/max(col(G))"),
                ("norm_y", "MFL-mTHF-1×10^-4 M-298 K_FEx315_Norm", (None, Decimal("0.5"), Decimal("1")), "Divided by Max of H", "col(H)/max(col(H))"),
            ),
            tuple((column.kind, column.comment, column.values, column.method, column.formula) for column in book.columns),
        )
        self.assertTrue(all(column.short_name is None for column in book.columns))

    def test_no_folder_is_created_when_no_data_would_be_emitted(self):
        plan = build_output_plan(())

        self.assertEqual((), plan.folders)
        self.assertEqual((), plan.incomplete_folders)

    def test_non_positive_selected_raw_y_maximum_is_rejected_before_output_plan(self):
        for spectrum_id, values, measured_maximum in (
            ("zero", (("500", "0"), ("501", "0")), "0"),
            ("negative", (("500", "-2"), ("501", "-1")), "-1"),
        ):
            with self.subTest(spectrum_id=spectrum_id):
                spectrum = _spectrum(
                    spectrum_id,
                    SpectrumClass.STEADY_EMISSION,
                    "A-298 K",
                    "A",
                    "298 K",
                    key_wavelength="270",
                    x_y=values,
                )

                with self.assertRaisesRegex(
                    ValueError,
                    rf"{spectrum_id}.*maximum {re.escape(measured_maximum)}.*normalization is invalid",
                ):
                    build_output_plan((spectrum,))

    def test_output_spectrum_rejects_duplicate_x_before_column_alignment(self):
        with self.assertRaisesRegex(
            ValueError,
            "duplicate X.*duplicate-x",
        ):
            build_output_plan(
                (
                    _spectrum(
                        "duplicate-x",
                        SpectrumClass.STEADY_EMISSION,
                        "A-298 K",
                        "A",
                        "298 K",
                        key_wavelength="270",
                        x_y=(("500", "1"), ("500.0", "2")),
                    ),
                )
            )

    def test_output_metadata_rejects_unrenderable_magnitude_without_decimal_overflow(self):
        spectrum = _spectrum(
            "huge",
            SpectrumClass.STEADY_EMISSION,
            "A-298 K",
            "A",
            "298 K",
            key_wavelength="1e1000000",
        )

        with self.assertRaisesRegex(
            ValueError,
            "key_wavelength.*supported metadata numeric domain",
        ):
            build_output_plan((spectrum,))

    def test_excitation_fallback_is_decided_by_exact_canonical_label(self):
        emission = _spectrum(
            "em",
            SpectrumClass.STEADY_EMISSION,
            "MFL-mTHF-1×10^-4 M-298 K",
            "MFL-mTHF-1×10^-4 M",
            "298 K",
            key_wavelength="270",
        )
        different_label_excitation = _spectrum(
            "ex",
            SpectrumClass.STEADY_EXCITATION,
            "MFL-mTHF-1×10^-4 M-user-corrected-298 K",
            "MFL-mTHF-1×10^-4 M",
            "298 K",
            key_wavelength="315",
        )

        plan = build_output_plan((emission, different_label_excitation))

        self.assertEqual(
            (
                "F_Ex270_ExSlit2_EmSlit2",
                "F_Em315_ExSlit2_EmSlit2",
            ),
            tuple(folder.name for folder in plan.folders),
        )
        emission_book = plan.folder("F_Ex270_ExSlit2_EmSlit2").books[0]
        self.assertNotIn(
            "MFL-mTHF-1×10^-4 M-user-corrected-298 K_FEx315",
            tuple(column.comment for column in emission_book.columns),
        )
        self.assertEqual(
            ("MFL-mTHF-1×10^-4 M-user-corrected-298 K",),
            plan.incomplete_folders[0].missing_labels,
        )

    def test_sample_system_label_separates_books_for_different_sample_attributes(self):
        spectra = (
            _spectrum("liquid-a", SpectrumClass.STEADY_EMISSION, "MFL-mTHF-1×10^-4 M-298 K", "MFL-mTHF-1×10^-4 M", "298 K", key_wavelength="270"),
            _spectrum("liquid-b", SpectrumClass.STEADY_EMISSION, "MFL-mTHF-1×10^-5 M-298 K", "MFL-mTHF-1×10^-5 M", "298 K", key_wavelength="270"),
            _spectrum("solid", SpectrumClass.STEADY_EMISSION, "MFL-Solid-298 K", "MFL-Solid", "298 K", key_wavelength="270"),
            _spectrum("hosted", SpectrumClass.STEADY_EMISSION, "MFL-in-mCP-10 wt%-Film-298 K", "MFL-in-mCP-10 wt%-Film", "298 K", key_wavelength="270"),
        )

        plan = build_output_plan(spectra)

        self.assertEqual(
            (
                "MFL-in-mCP-10 wt%-Film",
                "MFL-mTHF-1×10^-4 M",
                "MFL-mTHF-1×10^-5 M",
                "MFL-Solid",
            ),
            tuple(book.display_name for book in plan.folder("F_Ex270_ExSlit2_EmSlit2_ALL_SAMPLES").books),
        )

    def test_rejects_same_book_long_name_for_multiple_sample_identities_in_one_folder(self):
        spectra = (
            _spectrum(
                "first-emission",
                SpectrumClass.STEADY_EMISSION,
                "A-B-C-1 M-298 K",
                "A-B-C-1 M",
                "298 K",
                key_wavelength="270",
                sample_system_identity='{"sample":"A-B","solvent":"C"}',
            ),
            _spectrum(
                "second-emission",
                SpectrumClass.STEADY_EMISSION,
                "A-B-C-1 M-298 K",
                "A-B-C-1 M",
                "298 K",
                key_wavelength="270",
                sample_system_identity='{"sample":"A","solvent":"B-C"}',
            ),
        )

        with self.assertRaisesRegex(
            ValueError,
            "Book Long Name.*multiple sample system identities",
        ):
            build_output_plan(spectra)

    def test_rejects_same_book_long_name_for_multiple_sample_identities_across_folders(self):
        spectra = (
            _spectrum(
                "first",
                SpectrumClass.STEADY_EMISSION,
                "A-B-C-298 K",
                "A-B-C",
                "298 K",
                key_wavelength="270",
                sample_system_identity="identity-a",
            ),
            _spectrum(
                "second",
                SpectrumClass.STEADY_EMISSION,
                "A-B-C-298 K",
                "A-B-C",
                "298 K",
                key_wavelength="300",
                sample_system_identity="identity-b",
            ),
        )

        with self.assertRaisesRegex(
            ValueError,
            "Book Long Name.*multiple sample system identities",
        ):
            build_output_plan(spectra)

    def test_same_sample_identity_reuses_exact_book_long_name_across_folders(self):
        spectra = (
            _spectrum(
                "first",
                SpectrumClass.STEADY_EMISSION,
                "A-B-C-298 K",
                "A-B-C",
                "298 K",
                key_wavelength="270",
                sample_system_identity="identity",
            ),
            _spectrum(
                "second",
                SpectrumClass.STEADY_EMISSION,
                "A-B-C-298 K",
                "A-B-C",
                "298 K",
                key_wavelength="300",
                sample_system_identity="identity",
            ),
        )

        plan = build_output_plan(spectra)

        self.assertEqual(
            ("A-B-C", "A-B-C"),
            tuple(
                folder.books[0].display_name
                for folder in plan.folders
            ),
        )

    def test_rejects_conflicting_book_long_names_for_one_sample_identity(self):
        spectra = (
            _spectrum(
                "first",
                SpectrumClass.STEADY_EMISSION,
                "A-298 K",
                "A",
                "298 K",
                key_wavelength="270",
                sample_system_identity="identity",
            ),
            _spectrum(
                "second",
                SpectrumClass.STEADY_EMISSION,
                "B-298 K",
                "B",
                "298 K",
                key_wavelength="300",
                sample_system_identity="identity",
            ),
        )

        with self.assertRaisesRegex(
            ValueError,
            "sample system identity.*conflicting Book Long Names",
        ):
            build_output_plan(spectra)

    def test_selected_excitation_copies_into_each_compatible_emission_condition_folder(self):
        excitation = _spectrum(
            "ex",
            SpectrumClass.STEADY_EXCITATION,
            "MFL-Solid-77 K",
            "MFL-Solid",
            "77 K",
            key_wavelength="315",
        )
        first_emission = _spectrum(
            "em-270",
            SpectrumClass.STEADY_EMISSION,
            "MFL-Solid-77 K",
            "MFL-Solid",
            "77 K",
            key_wavelength="270",
            excitation_slit="2",
            emission_slit="2",
        )
        second_emission = _spectrum(
            "em-300",
            SpectrumClass.STEADY_EMISSION,
            "MFL-Solid-77 K",
            "MFL-Solid",
            "77 K",
            key_wavelength="300",
            excitation_slit="4",
            emission_slit="5",
        )

        plan = build_output_plan((second_emission, excitation, first_emission))

        for folder_name in ("F_Ex270_ExSlit2_EmSlit2_ALL_SAMPLES", "F_Ex300_ExSlit4_EmSlit5_ALL_SAMPLES"):
            comments = tuple(column.comment for column in plan.folder(folder_name).books[0].raw_y_columns)
            self.assertIn("MFL-Solid-77 K_FEx315", comments)
        self.assertNotIn("F_Em315_ExSlit2_EmSlit2", tuple(folder.name for folder in plan.folders))

    def test_colliding_excitation_comments_add_only_required_condition_suffixes(self):
        emission = _spectrum(
            "em",
            SpectrumClass.DELAYED_EMISSION,
            "PFL-Film-77 K",
            "PFL-Film",
            "77 K",
            key_wavelength="270",
            flash_delay="0.1",
            sample_window="1",
            time_per_flash="0.1",
            flash_count="100",
        )
        excitations = (
            _spectrum(
                "ex-a",
                SpectrumClass.DELAYED_EXCITATION,
                "PFL-Film-77 K",
                "PFL-Film",
                "77 K",
                key_wavelength="315",
                excitation_slit="2",
                emission_slit="4",
                flash_delay="0.1",
                sample_window="1",
                time_per_flash="0.1",
                flash_count="100",
            ),
            _spectrum(
                "ex-b",
                SpectrumClass.DELAYED_EXCITATION,
                "PFL-Film-77 K",
                "PFL-Film",
                "77 K",
                key_wavelength="315",
                excitation_slit="3",
                emission_slit="4",
                flash_delay="0.1",
                sample_window="1",
                time_per_flash="0.1",
                flash_count="200",
            ),
        )

        plan = build_output_plan((emission, *excitations))

        raw_comments = tuple(
            column.comment
            for column in plan.folders[0].books[0].raw_y_columns
            if "_PEx" in column.comment
        )
        self.assertEqual(
            (
                "PFL-Film-77 K_PEx315_ExSlit2",
                "PFL-Film-77 K_PEx315_ExSlit3",
            ),
            raw_comments,
        )
        norm_comments = tuple(
            column.comment
            for column in plan.folders[0].books[0].columns
            if column.kind == "norm_y" and "_PEx" in column.comment
        )
        self.assertEqual(
            tuple(f"{comment}_Norm" for comment in raw_comments),
            norm_comments,
        )

    def test_colliding_excitation_comments_use_scan_identity_after_existing_suffixes(self):
        excitations = (
            _spectrum(
                "ex-a",
                SpectrumClass.STEADY_EXCITATION,
                "A-298 K",
                "A",
                "298 K",
                key_wavelength="315",
                scan_start="250",
                scan_stop="400",
                scan_step="1",
                x_y=(("250", "1"), ("400", "2")),
            ),
            _spectrum(
                "ex-b",
                SpectrumClass.STEADY_EXCITATION,
                "A-298 K",
                "A",
                "298 K",
                key_wavelength="315",
                scan_start="260",
                scan_stop="410",
                scan_step="1",
                x_y=(("260", "3"), ("410", "4")),
            ),
        )

        plan = build_output_plan(excitations)

        self.assertEqual(
            (
                "A-298 K_FEx315_ExStart250",
                "A-298 K_FEx315_ExStart260",
            ),
            tuple(
                column.comment
                for column in plan.folders[0].books[0].raw_y_columns
            ),
        )

    def test_folder_order_is_deterministic_across_family_fallback_and_condition_values(self):
        spectra = (
            _spectrum("p-fallback", SpectrumClass.DELAYED_EXCITATION, "P0-77 K", "P0", "77 K", key_wavelength="315", flash_delay="0.2", sample_window="1", time_per_flash="0.1", flash_count="100"),
            _spectrum("f-fallback", SpectrumClass.STEADY_EXCITATION, "F0-77 K", "F0", "77 K", key_wavelength="315", excitation_slit="2", emission_slit="3"),
            _spectrum("p-late", SpectrumClass.DELAYED_EMISSION, "P1-77 K", "P1", "77 K", key_wavelength="270", excitation_slit="2", emission_slit="2", flash_delay="0.2", sample_window="1", time_per_flash="0.1", flash_count="100"),
            _spectrum("p-early", SpectrumClass.DELAYED_EMISSION, "P2-77 K", "P2", "77 K", key_wavelength="270", excitation_slit="2", emission_slit="2", flash_delay="0.1", sample_window="1", time_per_flash="0.1", flash_count="100"),
            _spectrum("f-high-slit", SpectrumClass.STEADY_EMISSION, "F1-77 K", "F1", "77 K", key_wavelength="270", excitation_slit="4", emission_slit="1"),
            _spectrum("f-low-wave", SpectrumClass.STEADY_EMISSION, "F2-77 K", "F2", "77 K", key_wavelength="260", excitation_slit="2", emission_slit="1"),
        )

        plan = build_output_plan(spectra)

        self.assertEqual(
            (
                "F_Ex260_ExSlit2_EmSlit1",
                "F_Ex270_ExSlit4_EmSlit1",
                "F_Em315_ExSlit2_EmSlit3",
                "P_Ex270_ExSlit2_EmSlit2_FD0.1_SW1_TPF0.1_FC100",
                "P_Ex270_ExSlit2_EmSlit2_FD0.2_SW1_TPF0.1_FC100",
                "P_Em315_ExSlit2_EmSlit2_FD0.2_SW1_TPF0.1_FC100",
            ),
            tuple(folder.name for folder in plan.folders),
        )

    def test_incomplete_folder_ledger_follows_final_folder_order(self):
        spectra = (
            _spectrum(
                "high",
                SpectrumClass.STEADY_EMISSION,
                "B-298 K",
                "B",
                "298 K",
                key_wavelength="400",
            ),
            _spectrum(
                "low",
                SpectrumClass.STEADY_EMISSION,
                "A-298 K",
                "A",
                "298 K",
                key_wavelength="300",
            ),
        )

        plan = build_output_plan(spectra)

        self.assertEqual(
            tuple(folder.name for folder in plan.folders),
            tuple(entry.folder_name for entry in plan.incomplete_folders),
        )

    def test_rejects_compound_slits_before_folder_identity(self):
        for field_name, value in (
            ("excitation_slit", ("2", "3")),
            ("emission_slit", "4-5"),
        ):
            with self.subTest(field_name=field_name):
                overrides = {field_name: value}
                with self.assertRaisesRegex(
                    ValueError,
                    f"{field_name}.*one semantic value",
                ):
                    build_output_plan(
                        (
                            _spectrum(
                                field_name,
                                SpectrumClass.STEADY_EMISSION,
                                "A-298 K",
                                "A",
                                "298 K",
                                key_wavelength="270",
                                **overrides,
                            ),
                        )
                    )

    def test_one_side_output_uses_only_x1_y1_columns(self):
        emission_only = build_output_plan((
            _spectrum("em", SpectrumClass.STEADY_EMISSION, "A-298 K", "A", "298 K", key_wavelength="270"),
        ))
        excitation_only = build_output_plan((
            _spectrum("ex", SpectrumClass.STEADY_EXCITATION, "B-298 K", "B", "298 K", key_wavelength="315"),
        ))

        self.assertEqual(("Em", "A-298 K_F270", "A-298 K_F270_Norm"), tuple(column.comment for column in emission_only.folders[0].books[0].columns))
        self.assertEqual(("Ex", "B-298 K_FEx315", "B-298 K_FEx315_Norm"), tuple(column.comment for column in excitation_only.folders[0].books[0].columns))

    def test_delayed_compatibility_ignores_flash_count_and_slits_but_requires_delay_core(self):
        emission = _spectrum(
            "em",
            SpectrumClass.DELAYED_EMISSION,
            "PFL-Film-77 K",
            "PFL-Film",
            "77 K",
            key_wavelength="270",
            excitation_slit="2",
            emission_slit="2",
            flash_delay="0.1",
            sample_window="1",
            time_per_flash="0.1",
            flash_count="100",
        )
        compatible_excitation = _spectrum(
            "ex-compatible",
            SpectrumClass.DELAYED_EXCITATION,
            "PFL-Film-77 K",
            "PFL-Film",
            "77 K",
            key_wavelength="315",
            excitation_slit="9",
            emission_slit="9",
            flash_delay="0.10",
            sample_window="1.0",
            time_per_flash="0.10",
            flash_count="999",
        )
        different_delay_excitation = _spectrum(
            "ex-different-delay",
            SpectrumClass.DELAYED_EXCITATION,
            "PFL-Film-77 K",
            "PFL-Film",
            "77 K",
            key_wavelength="330",
            excitation_slit="2",
            emission_slit="2",
            flash_delay="0.2",
            sample_window="1",
            time_per_flash="0.1",
            flash_count="100",
        )

        plan = build_output_plan((different_delay_excitation, compatible_excitation, emission))

        emission_comments = tuple(column.comment for column in plan.folder("P_Ex270_ExSlit2_EmSlit2_FD0.1_SW1_TPF0.1_FC100_ALL_SAMPLES").books[0].raw_y_columns)
        fallback_comments = tuple(column.comment for column in plan.folder("P_Em330_ExSlit2_EmSlit2_FD0.2_SW1_TPF0.1_FC100").books[0].raw_y_columns)
        self.assertIn("PFL-Film-77 K_PEx315", emission_comments)
        self.assertNotIn("PFL-Film-77 K_PEx330", emission_comments)
        self.assertEqual(("PFL-Film-77 K_PEx330",), fallback_comments)

    def test_malformed_compatible_excitation_sort_metadata_raises_controlled_error(self):
        spectra = (
            _spectrum("em", SpectrumClass.STEADY_EMISSION, "A-298 K", "A", "298 K", key_wavelength="270"),
            _spectrum("ex-good", SpectrumClass.STEADY_EXCITATION, "A-298 K", "A", "298 K", key_wavelength="315"),
            _spectrum("ex-bad", SpectrumClass.STEADY_EXCITATION, "A-298 K", "A", "298 K", key_wavelength="bad"),
        )

        with self.assertRaisesRegex(ValueError, "key_wavelength.*bad"):
            build_output_plan(spectra)

    def test_malformed_numeric_folder_metadata_raises_controlled_error_before_sorting(self):
        spectra = (
            _spectrum("good", SpectrumClass.STEADY_EMISSION, "A-298 K", "A", "298 K", key_wavelength="270"),
            _spectrum("bad", SpectrumClass.STEADY_EMISSION, "B-298 K", "B", "298 K", key_wavelength="bad"),
        )

        with self.assertRaisesRegex(ValueError, "key_wavelength.*bad"):
            build_output_plan(spectra)

    def test_malformed_numeric_slit_and_delay_metadata_raise_controlled_errors(self):
        cases = (
            ("excitation_slit", {"excitation_slit": "two"}),
            ("emission_slit", {"emission_slit": "wide"}),
            ("flash_delay", {"flash_delay": "late"}),
            ("sample_window", {"sample_window": "short"}),
            ("time_per_flash", {"time_per_flash": "fast"}),
            ("flash_count", {"flash_count": "many"}),
        )
        for field_name, overrides in cases:
            with self.subTest(field_name=field_name):
                params = {
                    "key_wavelength": "270",
                    "flash_delay": "0.1",
                    "sample_window": "1",
                    "time_per_flash": "0.1",
                    "flash_count": "100",
                }
                params.update(overrides)
                with self.assertRaisesRegex(ValueError, field_name):
                    build_output_plan(
                        (
                            _spectrum(
                                field_name,
                                SpectrumClass.DELAYED_EMISSION,
                                "P-77 K",
                                "P",
                                "77 K",
                                **params,
                            ),
                        )
                    )

    def test_non_finite_folder_sort_metadata_raises_controlled_error_before_decimal_sorting(self):
        spectra = (
            _spectrum("good", SpectrumClass.STEADY_EMISSION, "A-298 K", "A", "298 K", key_wavelength="270"),
            _spectrum("nan", SpectrumClass.STEADY_EMISSION, "B-298 K", "B", "298 K", key_wavelength="NaN"),
        )

        with self.assertRaisesRegex(ValueError, "key_wavelength.*NaN"):
            build_output_plan(spectra)

    def test_non_finite_numeric_metadata_raises_controlled_errors(self):
        cases = (
            ("key_wavelength", {"key_wavelength": "NaN"}),
            ("excitation_slit", {"excitation_slit": "Infinity"}),
            ("flash_delay", {"flash_delay": "-Infinity"}),
        )
        for field_name, overrides in cases:
            with self.subTest(field_name=field_name):
                params = {
                    "key_wavelength": "270",
                    "flash_delay": "0.1",
                    "sample_window": "1",
                    "time_per_flash": "0.1",
                    "flash_count": "100",
                }
                params.update(overrides)
                with self.assertRaisesRegex(ValueError, f"{field_name}.*{overrides[field_name]}"):
                    build_output_plan(
                        (
                            _spectrum(
                                field_name,
                                SpectrumClass.DELAYED_EMISSION,
                                "P-77 K",
                                "P",
                                "77 K",
                                **params,
                            ),
                        )
                    )

def _spectrum(
    spectrum_id,
    spectrum_class,
    canonical_sample_label,
    sample_system_label,
    temperature,
    *,
    key_wavelength,
    excitation_slit="2",
    emission_slit="2",
    flash_delay=None,
    sample_window=None,
    time_per_flash=None,
    flash_count=None,
    scan_start=None,
    scan_stop=None,
    scan_step=None,
    sample_system_identity=None,
    x_y=(("500", "10"),),
):
    return OutputSpectrum(
        spectrum_id=spectrum_id,
        spectrum_class=spectrum_class,
        canonical_sample_label=canonical_sample_label,
        sample_system_label=sample_system_label,
        temperature=temperature,
        key_wavelength=key_wavelength,
        excitation_slit=excitation_slit,
        emission_slit=emission_slit,
        flash_delay=flash_delay,
        sample_window=sample_window,
        time_per_flash=time_per_flash,
        flash_count=flash_count,
        scan_start=scan_start,
        scan_stop=scan_stop,
        scan_step=scan_step,
        sample_system_identity=sample_system_identity,
        x_y=x_y,
    )


if __name__ == "__main__":
    unittest.main()
