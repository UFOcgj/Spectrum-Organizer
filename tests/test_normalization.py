import pathlib
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from spectrum_organizer.domain.models import (
    DopedSample,
    LiquidSample,
    NeatSample,
    SampleValidationError,
    SpectrumClass,
)
from spectrum_organizer.domain.normalization import (
    ConcentrationError,
    normalize_concentration_input,
    normalize_doped_concentration,
    normalize_molarity,
    normalize_temperature,
)
import spectrum_organizer.domain.normalization as normalization_module


class NormalizationTests(unittest.TestCase):
    def test_spectrum_classes_are_explicit(self):
        self.assertEqual(
            {
                "steady_emission",
                "steady_excitation",
                "steady_2d",
                "delayed_emission",
                "delayed_excitation",
                "delayed_2d",
                "delay_time_series",
            },
            {member.value for member in SpectrumClass},
        )

    def test_temperature_normalization(self):
        self.assertEqual("298 K", normalize_temperature("RT"))
        self.assertEqual("298 K", normalize_temperature("room temperature"))
        self.assertEqual("298 K", normalize_temperature("298"))
        self.assertEqual("77.5 K", normalize_temperature("77.5 K"))
        self.assertEqual("298 K", normalize_temperature("298.0 K"))
        self.assertEqual("298 K", normalize_temperature("+298 K"))
        self.assertEqual("298 K", normalize_temperature("0298 K"))
        self.assertEqual("77.5 K", normalize_temperature("077.500 K"))
        with self.assertRaises(ValueError):
            normalize_temperature("25 C")

    def test_temperature_normalization_accepts_explicit_room_temperature_aliases(self):
        for value in ("room_temp", "room-temp", "RoomTemp", "室温"):
            with self.subTest(value=value):
                self.assertEqual("298 K", normalize_temperature(value))

    def test_molarity_decimal_thresholds_and_scientific_output(self):
        self.assertEqual("0.1 M", normalize_molarity("0.100 M"))
        self.assertEqual("1 M", normalize_molarity("1 M"))
        self.assertEqual("100 M", normalize_molarity("100 M"))
        self.assertEqual("1×10^-4 M", normalize_molarity("10^-4M"))
        self.assertEqual("1×10^-4 M", normalize_molarity("1e-4 M"))
        self.assertEqual("2×10^-5 M", normalize_molarity("2×10⁻⁵ M"))
        self.assertEqual("1.01×10^2 M", normalize_molarity("101 M"))

    def test_molarity_accepts_double_star_exponent(self):
        self.assertEqual("1×10^-5 M", normalize_molarity("10**-5M"))
        self.assertEqual("2×10^-5 M", normalize_molarity("2*10**-5 M"))

    def test_evidence_scanner_rejects_inner_tokens_from_malformed_numeric_operators(self):
        for value in (
            "PFL2x10M",
            "PFL2X10µM",
            "PFL2e10^-5M",
            "PFL2E10**-5M",
            "PFL2e10⁻⁵M",
            "sample_1ee5M",
            "sample_1xx5M",
            "PFL2 x 10M",
            "PFL2 X 10µM",
            "PFL2x 10M",
            "PFL2 × 10M",
            "PFL2 * 10M",
            "PFL2e 10^-5M",
            "PFL1e-- 3M",
            "PFL1e+- 3M",
            "PFL10^-- 3M",
            "PFL2x10^-- 3M",
        ):
            with self.subTest(value=value):
                entries, invalid_units = (
                    normalization_module.extract_concentration_evidence(value)
                )
                self.assertEqual((), entries)
                self.assertEqual(frozenset({"M"}), invalid_units)

        for value, expected in (
            ("Complex10µM", "1×10^-5 M"),
            ("Base1e-5M", "1×10^-5 M"),
            ("PFL2 x 10^-5M", "2×10^-5 M"),
            ("PFL2 * 10**-5M", "2×10^-5 M"),
            ("PFL2 × 10⁻⁵M", "2×10^-5 M"),
        ):
            with self.subTest(value=value):
                entries, invalid_units = (
                    normalization_module.extract_concentration_evidence(value)
                )
                self.assertEqual((expected,), tuple(entry.full_text for entry in entries))
                self.assertEqual(frozenset(), invalid_units)

    def test_evidence_scanner_treats_single_hyphen_after_a_name_as_a_separator(self):
        for value, expected in (
            ("PFL-10^-7M_RT", "1×10^-7 M"),
            ("Sample2-10µM", "1×10^-5 M"),
            ("样品-10nM", "1×10^-8 M"),
            ("Guest-5wt.%", "5 wt%"),
        ):
            with self.subTest(value=value):
                entries, invalid_units = (
                    normalization_module.extract_concentration_evidence(value)
                )
                self.assertEqual((expected,), tuple(entry.full_text for entry in entries))
                self.assertEqual(frozenset(), invalid_units)

        for value in (
            "-10^-7M",
            "PFL--10^-7M",
            "PFL_-10^-7M",
            "PFL/−10^-7M",
        ):
            with self.subTest(value=value):
                entries, invalid_units = (
                    normalization_module.extract_concentration_evidence(value)
                )
                self.assertEqual((), entries)
                self.assertEqual(frozenset({"M"}), invalid_units)

    def test_molarity_converts_explicit_scaled_and_moles_per_litre_units(self):
        cases = (
            ("0.1mM", "1×10^-4 M"),
            ("10 µM", "1×10^-5 M"),
            ("10 μM", "1×10^-5 M"),
            ("10uM", "1×10^-5 M"),
            ("1000 nM", "1×10^-6 M"),
            ("1 pM", "1×10^-12 M"),
            ("0.00001 mol/L", "1×10^-5 M"),
        )

        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(expected, normalize_molarity(value))
                entry = normalize_concentration_input(value, "M", ("M",))
                self.assertEqual(expected, entry.full_text)
                self.assertEqual("M", entry.unit)

    def test_molarity_preserves_distinct_high_precision_values(self):
        first = normalize_molarity("0.012345678901234567890123456781 M")
        second = normalize_molarity("0.012345678901234567890123456782 M")

        self.assertNotEqual(first, second)
        self.assertEqual("1.2345678901234567890123456781×10^-2 M", first)
        self.assertEqual("1.2345678901234567890123456782×10^-2 M", second)

    def test_extreme_caret_exponents_match_equivalent_e_notation(self):
        for caret, e_notation, expected in (
            ("1×10^1000000 M", "1e1000000 M", "1×10^1000000 M"),
            ("1×10^-2000000 M", "1e-2000000 M", "1×10^-2000000 M"),
        ):
            with self.subTest(caret=caret):
                self.assertEqual(expected, normalize_molarity(e_notation))
                self.assertEqual(expected, normalize_molarity(caret))

    def test_molarity_rejects_exponent_outside_decimal_scaling_capacity(self):
        with self.assertRaises(ConcentrationError):
            normalize_molarity("1e999999999999999999 M")

    def test_molarity_rejects_lowercase_m_and_invalid_values(self):
        for value in ["1 m", "0 M", "-1 M", "NaN M", "1 wt%", "10-4 M"]:
            with self.subTest(value=value):
                with self.assertRaises(ConcentrationError):
                    normalize_molarity(value)

    def test_doped_concentration_percentage_rules(self):
        self.assertEqual("0 wt%", normalize_doped_concentration("0 wt%"))
        self.assertEqual("100 mol%", normalize_doped_concentration("100 Mol%"))
        self.assertEqual("10 wt%", normalize_doped_concentration("10.0 WT %"))
        with self.assertRaises(ConcentrationError):
            normalize_doped_concentration("101 wt%")
        with self.assertRaises(ConcentrationError):
            normalize_doped_concentration("5 M")

    def test_doped_concentration_normalizes_explicit_percentage_unit_aliases(self):
        cases = (
            ("5 wt.%", "5 wt%"),
            ("5 weight%", "5 wt%"),
            ("5 mol.%", "5 mol%"),
            ("5 mole%", "5 mol%"),
        )

        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(expected, normalize_doped_concentration(value))
                entry = normalize_concentration_input(value, None, ("wt%", "mol%"))
                self.assertEqual(expected, entry.full_text)

    def test_doped_concentration_rejects_unbounded_plain_output_without_rounding(self):
        for value in ("1e-1000000 wt%", "1e-999999999999999999 wt%"):
            with self.subTest(value=value):
                with self.assertRaises(ConcentrationError):
                    normalize_doped_concentration(value)

    def test_doped_zero_short_circuits_fixed_point_formatting(self):
        with mock.patch.object(
            normalization_module,
            "_format_decimal_plain",
            side_effect=AssertionError("zero must not reach fixed-point formatting"),
        ):
            self.assertEqual("0 wt%", normalize_doped_concentration("0e-1000000 wt%"))
            self.assertEqual("0 mol%", normalize_doped_concentration("-0e+1000000 mol%"))

    def test_numeric_only_input_uses_selector_and_explicit_unit_syncs_selector(self):
        entry = normalize_concentration_input("10.0", selected_unit="wt%", allowed_units=("wt%", "mol%"))
        self.assertEqual("10", entry.value_text)
        self.assertEqual("wt%", entry.unit)
        self.assertEqual("10 wt%", entry.full_text)

        explicit = normalize_concentration_input("5 Mol%", selected_unit="wt%", allowed_units=("wt%", "mol%"))
        self.assertEqual("5", explicit.value_text)
        self.assertEqual("mol%", explicit.unit)
        self.assertEqual("5 mol%", explicit.full_text)

        liquid = normalize_concentration_input("10^-4", selected_unit="M", allowed_units=("M",))
        self.assertEqual("1×10^-4", liquid.value_text)
        self.assertEqual("M", liquid.unit)

    def test_doped_selector_initially_empty_blocks_numeric_only(self):
        with self.assertRaises(ConcentrationError):
            normalize_concentration_input("10", selected_unit=None, allowed_units=("wt%", "mol%"))

    def test_sample_labels_and_required_fields(self):
        liquid = LiquidSample("MFL", "mTHF", "1×10^-4 M", "298 K")
        self.assertEqual("MFL-mTHF-1×10^-4 M-298 K", liquid.canonical_label)
        self.assertEqual("MFL-mTHF-1×10^-4 M", liquid.system_label)

        neat = NeatSample("MFL", "Solid", "77 K")
        self.assertEqual("MFL-Solid-77 K", neat.canonical_label)
        self.assertEqual("MFL-Solid", neat.system_label)

        doped = DopedSample("MFL", "mCP", "0 wt%", "Film", "298 K")
        self.assertEqual("MFL-in-mCP-0 wt%-Film-298 K", doped.canonical_label)
        self.assertEqual("MFL-in-mCP-0 wt%-Film", doped.system_label)

        trimmed = LiquidSample(" MFL ", " mTHF solvent ", "1×10^-4 M", "298 K")
        self.assertEqual("MFL-mTHF solvent-1×10^-4 M-298 K", trimmed.canonical_label)

        with self.assertRaises(SampleValidationError):
            LiquidSample("MFL", "mTHF", "", "298 K")


if __name__ == "__main__":
    unittest.main()
