import importlib
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from spectrum_organizer.domain.models import SpectrumClass
from spectrum_organizer.origin.note_parser import NoteParseError, parse_book_note, ui_delay_units


class NoteParserTests(unittest.TestCase):
    def test_legacy_module_reexports_spectrum_class_identity(self):
        legacy_module = importlib.import_module(
            "spectrum_organizer.origin.note_parser"
        )

        self.assertIs(SpectrumClass, getattr(legacy_module, "SpectrumClass"))

    def test_type_must_come_from_declared_field_not_comment_text(self):
        with self.assertRaises(NoteParseError):
            parse_book_note(
                "[EXP_FD_FILE]\nExperiment Type = Unsupported\n"
                "Comment = Spectral Acquisition[Emission]"
            )

    def test_conflicting_declared_types_are_rejected(self):
        with self.assertRaises(NoteParseError):
            parse_book_note(
                "[EXP_FD_FILE]\nAcquisition Type = Spectral Acquisition[Emission]\n"
                "Experiment Type = Spectral Acquisition[Excitation]"
            )

    def test_repeated_same_type_field_with_conflicting_values_is_rejected(self):
        with self.assertRaises(NoteParseError):
            parse_book_note(
                "[EXP_FD_FILE]\nExperiment Type = Spectral Acquisition[Emission]\n"
                "Experiment Type = Spectral Acquisition[Excitation]"
            )

    def test_conflicting_duplicate_global_physical_fields_are_rejected(self):
        for values in (("270", "300"), ("300", "270")):
            with self.subTest(values=values), self.assertRaisesRegex(
                NoteParseError,
                "Conflicting Note field.*Excitation Wavelength",
            ):
                parse_book_note(
                    "[EXP_FD_FILE]\n"
                    "Experiment Type = Spectral Acquisition[Emission]\n"
                    f"Excitation Wavelength = {values[0]}\n"
                    f"excitation wavelength = {values[1]}\n"
                )

    def test_conflicting_duplicate_section_physical_fields_are_rejected(self):
        for values in (("270", "300"), ("300", "270")):
            with self.subTest(values=values), self.assertRaisesRegex(
                NoteParseError,
                "Conflicting Note field.*Park.*ex1",
            ):
                parse_book_note(
                    "[EXP_FD_FILE]\n"
                    "Experiment Type = Spectral Acquisition[Emission]\n"
                    "[EX1]\n"
                    f"Park = {values[0]}\n"
                    f"park = {values[1]}\n"
                )

    def test_identical_duplicates_are_allowed_per_scope(self):
        note = parse_book_note(
            "[EXP_FD_FILE]\n"
            "Experiment Type = Spectral Acquisition[Emission]\n"
            "Excitation Wavelength = 270\n"
            "excitation wavelength = 270\n"
            "[EX1]\n"
            "Park = 270\n"
            "park = 270\n"
            "[EM1]\n"
            "Park = 500\n"
        )

        self.assertEqual("270", note.fixed_excitation_wavelength)

    def test_parses_note_test_datetime_when_present(self):
        note = parse_book_note(
            "[EXP_FD_FILE]\nAcquisition Type = Spectral Acquisition[Emission]\n"
            "Test Date = 2026-06-27\nTest Time = 12:30:00"
        )

        self.assertEqual("2026-06-27 12:30:00", note.note_datetime)

    def test_rejects_non_book_local_note_without_required_prefix(self):
        with self.assertRaises(NoteParseError):
            parse_book_note("Spectral Acquisition[Emission]")

    def test_recognizes_exact_real_evidence_type_strings(self):
        cases = {
            "Spectral Acquisition[Emission]": SpectrumClass.STEADY_EMISSION,
            "Spectral Acquisition[Excitation]": SpectrumClass.STEADY_EXCITATION,
            "Phos Acquisition[Emission]": SpectrumClass.DELAYED_EMISSION,
            "Phos Acquisition[Excitation]": SpectrumClass.DELAYED_EXCITATION,
            "3D Acquisition[Excitation vs Emission vs Intensity]": SpectrumClass.STEADY_2D,
        }
        for label, expected in cases.items():
            with self.subTest(label=label):
                suffix = (
                    "\nFlash Delay = 0.1\nSample window = 1\nTime per Flash = 0.1\nFlash Count = 100"
                    if label.startswith("Phos ")
                    else ""
                )
                note = parse_book_note(f"[EXP_FD_FILE]\nAcquisition Type = {label}{suffix}")
                self.assertEqual(expected, note.spectrum_class)
                self.assertEqual(label, note.acquisition_type)

    def test_parses_delayed_labels_with_exact_sample_window_case_and_ui_units(self):
        note = parse_book_note(
            "[EXP_FD_FILE]\n"
            "Acquisition Type = Phos Acquisition[Emission]\n"
            "Excitation Wavelength = 270\n"
            "Emission Range = 300 - 650\n"
            "Flash Delay = 0.1\n"
            "Sample window = 1\n"
            "Time per Flash = 0.1\n"
            "Flash Count = 100\n"
        )
        self.assertEqual("0.1", note.delay.flash_delay)
        self.assertEqual("1", note.delay.sample_window)
        self.assertEqual("0.1", note.delay.time_per_flash)
        self.assertEqual("100", note.delay.flash_count)
        self.assertEqual({"Flash Delay": "ms", "Sample window": "ms", "Time per Flash": "ms"}, ui_delay_units())

    def test_emission_and_excitation_wavelength_semantics(self):
        emission = parse_book_note(
            "[EXP_FD_FILE]\nAcquisition Type = Spectral Acquisition[Emission]\n"
            "Excitation Wavelength = 315\nEmission Range = 350 - 700\nEmission Increment = 1"
        )
        self.assertEqual("315", emission.fixed_excitation_wavelength)
        self.assertEqual(("350", "700"), emission.emission_range)

        excitation = parse_book_note(
            "[EXP_FD_FILE]\nAcquisition Type = Spectral Acquisition[Excitation]\n"
            "Emission Wavelength = 520\nExcitation Range = 250 - 420\nExcitation Increment = 1"
        )
        self.assertEqual("520", excitation.fixed_emission_wavelength)
        self.assertEqual(("250", "420"), excitation.excitation_range)

    def test_range_endpoints_preserve_negative_scientific_exponents(self):
        note = parse_book_note(
            "[EXP_FD_FILE]\nAcquisition Type = Spectral Acquisition[Emission]\n"
            "Excitation Wavelength = 315\n"
            "Emission Range = 300500e-3 - 650250e-3\nEmission Increment = 5e-1"
        )

        self.assertEqual(("300500e-3", "650250e-3"), note.emission_range)

    def test_parses_real_section_scoped_emission_fields_and_slits(self):
        note = parse_book_note(
            "[EXP_FD_FILE]\n"
            "Experiment Type = Spectral Acquisition[Emission]\n"
            "[EX1]\n"
            "Park = 270\n"
            "Front Entrance Slit = 2\n"
            "Front Exit Slit = 2.0\n"
            "[EM1]\n"
            "Start = 285\n"
            "End = 650\n"
            "Increment = 1\n"
            "Front Entrance Slit = 4\n"
            "Front Exit Slit = 4.00\n"
        )

        self.assertEqual("270", note.fixed_excitation_wavelength)
        self.assertEqual(("285", "650"), note.emission_range)
        self.assertEqual("1", note.emission_increment)
        self.assertEqual(("2", "2.0"), note.excitation_slits)
        self.assertEqual(("4", "4.00"), note.emission_slits)

    def test_rejects_unequal_entrance_and_exit_slits_on_either_side(self):
        for section in ("EX1", "EM1"):
            with self.subTest(section=section):
                other_section = "EM1" if section == "EX1" else "EX1"
                with self.assertRaisesRegex(
                    NoteParseError,
                    f"{section} entrance and exit slit values conflict",
                ):
                    parse_book_note(
                        "[EXP_FD_FILE]\n"
                        "Experiment Type = Spectral Acquisition[Emission]\n"
                        f"[{section}]\n"
                        "Front Entrance Slit = 2\n"
                        "Front Exit Slit = 3\n"
                        f"[{other_section}]\n"
                        "Front Entrance Slit = 4\n"
                        "Front Exit Slit = 4\n"
                    )

    def test_parses_real_section_scoped_excitation_and_steady_2d_fields(self):
        excitation = parse_book_note(
            "[EXP_FD_FILE]\n"
            "Experiment Type = Spectral Acquisition[Excitation]\n"
            "[EX1]\nStart = 240\nEnd = 400\nIncrement = 0.5\n"
            "[EM1]\nPark = 520\n"
        )
        two_dimensional = parse_book_note(
            "[EXP_FD_FILE]\n"
            "Experiment Type = 3D Acquisition[Excitation vs Emission vs Intensity]\n"
            "[EX1]\nStart = 250\nEnd = 450\nIncrement = 5\n"
            "[EM1]\nStart = 300\nEnd = 700\nIncrement = 2\n"
        )

        self.assertEqual(("240", "400"), excitation.excitation_range)
        self.assertEqual("0.5", excitation.excitation_increment)
        self.assertEqual("520", excitation.fixed_emission_wavelength)
        self.assertEqual(("250", "450"), two_dimensional.excitation_range)
        self.assertEqual(("300", "700"), two_dimensional.emission_range)

    def test_parses_instrument_colon_scoped_wavelength_sections(self):
        cases = (
            (
                "Spectral Acquisition[Emission]",
                "EX1: Excitation 1 (Mono3)\nPark: 270.00nm\n"
                "Front Entrance Slit: 3.00 nmBandpass\nFront Exit Slit: 3.00 nmBandpass\n"
                "EM1: Emission 1 (Mono4)\nStart: 285.00nm\nEnd: 525.00nm\nIncrement: 1.00nm\n",
                "270.00",
                None,
                None,
                ("285.00", "525.00"),
            ),
            (
                "Spectral Acquisition[Excitation]",
                "EX1: Excitation 1 (Mono3)\nStart: 240.00nm\nEnd: 462.00nm\nIncrement: 1.00nm\n"
                "EM1: Emission 1 (Mono4)\nPark: 477.00nm\n",
                None,
                "477.00",
                ("240.00", "462.00"),
                None,
            ),
            (
                "Phos Acquisition[Emission]",
                "EX1: Excitation 1 (Mono3)\nPark: 300.00nm\n"
                "EM1: Emission 1 (Mono4)\nStart: 400.00nm\nEnd: 750.00nm\nIncrement: 1.00nm\n",
                "300.00",
                None,
                None,
                ("400.00", "750.00"),
            ),
            (
                "Phos Acquisition[Excitation]",
                "EX1: Excitation 1 (Mono3)\nStart: 200.00nm\nEnd: 461.00nm\nIncrement: 1.00nm\n"
                "EM1: Emission 1 (Mono4)\nPark: 476.00nm\n",
                None,
                "476.00",
                ("200.00", "461.00"),
                None,
            ),
        )
        delay_fields = (
            "Flash Delay: 1.00\nSample window: 20.00\n"
            "Time per Flash: 46.00\nFlash Count: 4\n"
        )

        for acquisition_type, sections, fixed_ex, fixed_em, ex_range, em_range in cases:
            with self.subTest(acquisition_type=acquisition_type):
                note = parse_book_note(
                    "[EXP_FD_FILE]\n"
                    f"Experiment Type: {acquisition_type}\n"
                    f"{delay_fields if acquisition_type.startswith('Phos ') else ''}"
                    f"{sections}"
                    "ACCESSORIES:\nPark: 999.00nm\n"
                )

                self.assertEqual(fixed_ex, note.fixed_excitation_wavelength)
                self.assertEqual(fixed_em, note.fixed_emission_wavelength)
                self.assertEqual(ex_range, note.excitation_range)
                self.assertEqual(em_range, note.emission_range)

    def test_parses_real_em1_with_signal_and_reference_detector_descriptors(self):
        note = parse_book_note(
            "[EXP_FD_FILE]\n"
            "Experiment Type: Spectral Acquisition[Emission]\n"
            "EX1: Excitation 1 (Mono3)\n"
            "Park: 300.00nm\n"
            "Front Entrance Slit: 2.00 nmBandpass\n"
            "Front Exit Slit: 2.00 nmBandpass\n"
            "EM1: Emission 1 (Mono4)\n"
            "Start: 315.00nm\n"
            "End: 585.00nm\n"
            "Increment: 1.00nm\n"
            "Front Entrance Slit: 2.00 nmBandpass\n"
            "Front Exit Slit: 2.00 nmBandpass\n"
            "Detector: S (SCD100)\n"
            "Units: Counts\n"
            "Detector: R (SCD101)\n"
            "Units: MicroAmps\n"
            "ACCESSORIES:\n"
        )

        self.assertEqual(SpectrumClass.STEADY_EMISSION, note.spectrum_class)
        self.assertEqual("300.00", note.fixed_excitation_wavelength)
        self.assertEqual(("315.00", "585.00"), note.emission_range)
        self.assertEqual(("2.00", "2.00"), note.emission_slits)

    def test_parses_real_em1_with_per_detector_correction_files(self):
        note = parse_book_note(
            "[EXP_FD_FILE]\n"
            "Experiment Type: Spectral Acquisition[Emission]\n"
            "EX1: Excitation 1 (Mono3)\n"
            "Park: 300.00nm\n"
            "Front Entrance Slit: 2.00 nmBandpass\n"
            "Front Exit Slit: 2.00 nmBandpass\n"
            "EM1: Emission 1 (Mono4)\n"
            "Start: 315.00nm\n"
            "End: 585.00nm\n"
            "Increment: 1.00nm\n"
            "Front Entrance Slit: 2.00 nmBandpass\n"
            "Front Exit Slit: 2.00 nmBandpass\n"
            "Detector: S (SCD100)\n"
            "Units: Counts\n"
            "Corrected: C:\\Users\\Public\\Documents\\Jobin Yvon\\Data\\Factory Configurations\\Corr\\S1_R928P_1200-500.SPC\n"
            "Detector: R (SCD101)\n"
            "Units: MicroAmps\n"
            "Corrected: C:\\Users\\Public\\Documents\\Jobin Yvon\\Data\\Factory Configurations\\Corr\\R1_PD_1200-330.SPC\n"
            "ACCESSORIES:\n"
        )

        self.assertEqual(SpectrumClass.STEADY_EMISSION, note.spectrum_class)
        self.assertEqual("300.00", note.fixed_excitation_wavelength)
        self.assertEqual(("315.00", "585.00"), note.emission_range)
        self.assertEqual(("2.00", "2.00"), note.emission_slits)

    def test_descriptive_non_optical_header_ends_optical_section_scope(self):
        note = parse_book_note(
            "[EXP_FD_FILE]\n"
            "Experiment Type: Spectral Acquisition[Excitation]\n"
            "EX1: Excitation 1 (Mono3)\nStart: 240.00nm\nEnd: 462.00nm\nIncrement: 1.00nm\n"
            "EM1: Emission 1 (Mono4)\nPark: 477.00nm\n"
            "ACCESSORIES: detector\nPark: 999.00nm\n"
        )

        self.assertEqual("477.00", note.fixed_emission_wavelength)


if __name__ == "__main__":
    unittest.main()
