import math
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from spectrum_organizer.core.validity import validate_spectrum_data
from spectrum_organizer.domain.models import SpectrumClass
from spectrum_organizer.origin.data_columns import Column, WorksheetData


class ValidityTests(unittest.TestCase):
    def test_valid_steady_emission_reports_selected_y_max_and_x_at_max(self):
        result = validate_spectrum_data(
            SpectrumClass.STEADY_EMISSION,
            _layout_s1c_s1(s1c=[1, 5, 3], s1=[10, 20, 30]),
            steady_emission_y="S1c",
            s1_limit=100,
        )
        self.assertTrue(result.ok)
        self.assertEqual(5, result.selected_y_max)
        self.assertEqual(301, result.x_at_max_y)
        self.assertEqual(30, result.s1_max)

    def test_tied_selected_y_max_preserves_every_matching_x(self):
        result = validate_spectrum_data(
            SpectrumClass.STEADY_EMISSION,
            _layout_s1c_s1(x=[300, 301, 302], s1c=[5, 5, 5], s1=[10, 20, 30]),
            steady_emission_y="S1c",
            s1_limit=100,
        )

        self.assertTrue(result.ok)
        self.assertEqual((300, 301, 302), result.x_at_max_y)

    def test_tied_s1_max_preserves_every_matching_x(self):
        result = validate_spectrum_data(
            SpectrumClass.STEADY_EMISSION,
            _layout_s1c_s1(
                x=[300, 301, 302],
                s1c=[1, 2, 3],
                s1=[101, 50, 101],
            ),
            steady_emission_y="S1c",
            s1_limit=100,
        )

        self.assertFalse(result.ok)
        self.assertEqual("S1 max exceeds limit", result.reason)
        self.assertEqual((300, 302), result.s1_max_x)

    def test_steady_excitation_requires_s1c_over_r1c(self):
        result = validate_spectrum_data(
            SpectrumClass.STEADY_EXCITATION,
            _layout_s1c_r1c_s1(ratio=[0.1, 0.2], s1=[10, 20]),
            steady_emission_y="S1c",
            s1_limit=100,
        )
        self.assertTrue(result.ok)
        self.assertEqual(0.2, result.selected_y_max)

    def test_delayed_uses_s1c_and_steady_2d_is_the_only_s1_exemption(self):
        delayed = validate_spectrum_data(
            SpectrumClass.DELAYED_EMISSION,
            _layout_s1c_s1(x=[300, 301], s1c=[2, 3], s1=[10, 20]),
            steady_emission_y="S1c/R1c",
            s1_limit=100,
        )
        self.assertTrue(delayed.ok)
        self.assertEqual(3, delayed.selected_y_max)

        steady_2d = validate_spectrum_data(
            SpectrumClass.STEADY_2D,
            WorksheetData([_x("A", "Wavelength", [300]), _y("S1c", "S1c", [1])]),
            steady_emission_y="S1c",
            s1_limit=1,
        )
        self.assertTrue(steady_2d.ok)

    def test_missing_s1_missing_y_and_s1_saturation_reject(self):
        missing_s1 = validate_spectrum_data(
            SpectrumClass.STEADY_EMISSION,
            WorksheetData([_x("A", "Wavelength", [300]), _y("S1c", "S1c", [1])]),
            steady_emission_y="S1c",
            s1_limit=100,
        )
        self.assertFalse(missing_s1.ok)
        self.assertEqual("missing S1", missing_s1.reason)

        missing_y = validate_spectrum_data(
            SpectrumClass.STEADY_EXCITATION,
            _layout_s1c_s1(x=[300], s1c=[1], s1=[10]),
            steady_emission_y="S1c",
            s1_limit=100,
        )
        self.assertFalse(missing_y.ok)
        self.assertEqual("missing selected Y", missing_y.reason)
        self.assertEqual("S1c/R1c", missing_y.missing_column)

        saturated = validate_spectrum_data(
            SpectrumClass.DELAYED_EXCITATION,
            _layout_s1c_s1(x=[300], s1c=[1], s1=[101]),
            steady_emission_y="S1c",
            s1_limit=100,
        )
        self.assertFalse(saturated.ok)
        self.assertEqual("S1 max exceeds limit", saturated.reason)
        self.assertEqual(101, saturated.s1_max)

    def test_missing_s1_can_be_explicitly_allowed_without_weakening_selected_xy(self):
        data = WorksheetData([
            _x("A", "Wavelength", [300, 301]),
            _y("S1c", "S1c", [1, 2]),
        ])

        allowed = validate_spectrum_data(
            SpectrumClass.STEADY_EMISSION,
            data,
            steady_emission_y="S1c",
            s1_limit=100,
            allow_missing_s1=True,
        )

        self.assertTrue(allowed.ok)
        self.assertIsNone(allowed.s1_max)
        self.assertEqual(2, allowed.selected_y_max)
        self.assertEqual(301, allowed.x_at_max_y)

        missing_selected_y = validate_spectrum_data(
            SpectrumClass.STEADY_EXCITATION,
            data,
            steady_emission_y="S1c",
            s1_limit=100,
            allow_missing_s1=True,
        )
        self.assertFalse(missing_selected_y.ok)
        self.assertEqual("missing selected Y", missing_selected_y.reason)

        missing_paired_x = validate_spectrum_data(
            SpectrumClass.STEADY_EMISSION,
            WorksheetData([_y("S1c", "S1c", [1, 2])]),
            steady_emission_y="S1c",
            s1_limit=100,
            allow_missing_s1=True,
        )
        self.assertFalse(missing_paired_x.ok)
        self.assertEqual(
            "Selected Y has no preceding X column: S1c",
            missing_paired_x.reason,
        )
        self.assertEqual("S1c", missing_paired_x.missing_column)

    def test_allow_missing_s1_does_not_bypass_an_existing_s1_limit(self):
        result = validate_spectrum_data(
            SpectrumClass.STEADY_EMISSION,
            _layout_s1c_s1(
                x=[300, 301],
                s1c=[1, 2],
                s1=[10, 101],
            ),
            steady_emission_y="S1c",
            s1_limit=100,
            allow_missing_s1=True,
        )

        self.assertFalse(result.ok)
        self.assertEqual("S1 max exceeds limit", result.reason)

    def test_allow_missing_s1_still_measures_a_short_name_only_s1_column(self):
        data = WorksheetData([
            _x("A", "Wavelength", [300, 301]),
            _y("S1c", "Corrected Signal", [1, 2]),
            _x("B", "Wavelength", [300, 301]),
            _y("S1", "Detector Counts", [10, 101]),
        ])

        result = validate_spectrum_data(
            SpectrumClass.STEADY_EMISSION,
            data,
            steady_emission_y="S1c",
            s1_limit=100,
            allow_missing_s1=True,
        )

        self.assertFalse(result.ok)
        self.assertEqual("S1 max exceeds limit", result.reason)
        self.assertEqual(101, result.s1_max)
        self.assertEqual(301, result.s1_max_x)

    def test_nonblank_physical_s1_without_paired_x_is_rejected(self):
        data = WorksheetData([
            _y("S1", "Detector Counts", [10, 20]),
            _x("A", "Wavelength", [300, 301]),
            _y("S1c", "Corrected Signal", [1, 2]),
        ])

        result = validate_spectrum_data(
            SpectrumClass.STEADY_EMISSION,
            data,
            steady_emission_y="S1c",
            s1_limit=100,
            allow_missing_s1=True,
        )

        self.assertFalse(result.ok)
        self.assertIn("no preceding X", result.reason)

    def test_allow_missing_s1_treats_an_all_blank_s1_column_as_missing(self):
        result = validate_spectrum_data(
            SpectrumClass.STEADY_EMISSION,
            _layout_s1c_s1(x=[300, 301], s1c=[1, 2], s1=[None, None]),
            steady_emission_y="S1c",
            s1_limit=100,
            allow_missing_s1=True,
        )

        self.assertTrue(result.ok)
        self.assertIsNone(result.s1_max)
        self.assertEqual(2, result.selected_y_max)
        self.assertEqual(301, result.x_at_max_y)

    def test_allow_missing_s1_does_not_accept_an_internal_s1_blank(self):
        result = validate_spectrum_data(
            SpectrumClass.STEADY_EMISSION,
            _layout_s1c_s1(x=[300, 301, 302], s1c=[1, 2, 3], s1=[10, None, 30]),
            steady_emission_y="S1c",
            s1_limit=100,
            allow_missing_s1=True,
        )

        self.assertFalse(result.ok)
        self.assertEqual("blank in column S1 at row 2", result.reason)

    def test_allow_missing_s1_rejects_ambiguous_physical_s1_columns(self):
        data = WorksheetData([
            _x("A", "Wavelength", [300, 301]),
            _y("S1c", "S1c", [1, 2]),
            _x("B", "Wavelength", [300, 301]),
            _y("S1", "S1", [None, None]),
            _x("C", "Wavelength", [300, 301]),
            _y("D", "S1", [10, 101]),
        ])

        result = validate_spectrum_data(
            SpectrumClass.STEADY_EMISSION,
            data,
            steady_emission_y="S1c",
            s1_limit=100,
            allow_missing_s1=True,
        )

        self.assertFalse(result.ok)
        self.assertEqual("ambiguous S1", result.reason)

    def test_rejects_ambiguous_physical_selected_y_columns(self):
        data = WorksheetData([
            _x("A", "Wavelength", [300, 301]),
            _y("S1c", "Corrected Signal", [1, 2]),
            _x("B", "Wavelength", [300, 301]),
            _y("D", "S1c", [10, 20]),
            _x("C", "Wavelength", [300, 301]),
            _y("S1", "Detector Counts", [10, 20]),
        ])

        result = validate_spectrum_data(
            SpectrumClass.STEADY_EMISSION,
            data,
            steady_emission_y="S1c",
            s1_limit=100,
        )

        self.assertFalse(result.ok)
        self.assertEqual("ambiguous selected Y", result.reason)
        self.assertEqual("S1c", result.missing_column)

    def test_rejects_internal_blank_nan_infinity_length_mismatch_and_nonpositive_y(self):
        cases = [
            (_layout_s1c_s1(x=[300, None, 302], s1c=[1, 2, 3], s1=[10, 20, 30]), "blank in column Wavelength at row 2"),
            (_layout_s1c_s1(s1c=[1, math.nan, 3], s1=[10, 20, 30]), "non-finite column S1c at row 2"),
            (_layout_s1c_s1(s1c=[1, math.inf, 3], s1=[10, 20, 30]), "non-finite column S1c at row 2"),
            (_layout_s1c_s1(s1c=[1, 10**400, 3], s1=[10, 20, 30]), "non-finite column S1c at row 2"),
            (_layout_s1c_s1(x=[300, 301, 302], s1c=[1, 2], s1=[10, 20, 30]), "column Wavelength has 3 rows but column S1c has 2 rows"),
            (_layout_s1c_s1(x=[300, 301], s1c=[0, -1], s1=[10, 20]), "selected Y max <= 0"),
        ]
        for data, reason in cases:
            with self.subTest(reason=reason):
                result = validate_spectrum_data(
                    SpectrumClass.STEADY_EMISSION,
                    data,
                    steady_emission_y="S1c",
                    s1_limit=100,
                )
                self.assertFalse(result.ok)
                self.assertEqual(reason, result.reason)

    def test_trailing_shared_blanks_are_ignored(self):
        data = _layout_s1c_s1(x=[300, 301, None, None], s1c=[1, 2, None, None], s1=[10, 20, None, None])
        result = validate_spectrum_data(SpectrumClass.STEADY_EMISSION, data, steady_emission_y="S1c", s1_limit=100)
        self.assertTrue(result.ok)
        self.assertEqual(2, result.selected_y_max)

    def test_s1_pair_rejects_one_sided_trailing_blanks(self):
        cases = (
            (
                [300, 301],
                [10, None],
                "blank in column S1 at row 2",
            ),
            (
                [300, None],
                [10, 20],
                "blank in column S1 Wavelength at row 2",
            ),
        )
        for s1_x, s1, expected_reason in cases:
            with self.subTest(expected_reason=expected_reason):
                data = WorksheetData([
                    _x("A", "Wavelength", [300, 301]),
                    _y("S1c", "S1c", [1, 2]),
                    _x("B", "S1 Wavelength", s1_x),
                    _y("S1", "S1", s1),
                ])

                result = validate_spectrum_data(
                    SpectrumClass.STEADY_EMISSION,
                    data,
                    steady_emission_y="S1c",
                    s1_limit=100,
                )

                self.assertFalse(result.ok)
                self.assertEqual(expected_reason, result.reason)

    def test_invalid_x_at_s1_max_is_a_book_validation_failure(self):
        data = _layout_s1c_s1(x=[300, "bad-x"], s1c=[1, 2], s1=[10, 20])

        result = validate_spectrum_data(
            SpectrumClass.STEADY_EMISSION,
            data,
            steady_emission_y="S1c",
            s1_limit=100,
        )

        self.assertFalse(result.ok)
        self.assertEqual("non-finite column Wavelength at row 2", result.reason)

    def test_duplicate_selected_x_is_rejected_before_output_rows_can_collapse(self):
        result = validate_spectrum_data(
            SpectrumClass.STEADY_EMISSION,
            _layout_s1c_s1(x=[300, 300.0], s1c=[1, 2], s1=[10, 20]),
            steady_emission_y="S1c",
            s1_limit=100,
        )

        self.assertFalse(result.ok)
        self.assertEqual(
            "duplicate value in column Wavelength at row 2",
            result.reason,
        )

    def test_selected_y_cannot_alias_the_same_physical_column_as_s1(self):
        for short_name, long_name in (
            ("S1c", "S1"),
            ("S1", "S1c"),
        ):
            with self.subTest(
                short_name=short_name,
                long_name=long_name,
            ):
                result = validate_spectrum_data(
                    SpectrumClass.STEADY_EMISSION,
                    WorksheetData(
                        [
                            _x("A", "Wavelength", [300, 301]),
                            _y(
                                short_name,
                                long_name,
                                [10, 20],
                            ),
                        ]
                    ),
                    steady_emission_y="S1c",
                    s1_limit=100,
                )

                self.assertFalse(result.ok)
                self.assertEqual(
                    "selected Y and S1 resolve to the same physical column",
                    result.reason,
                )


def _layout_s1c_s1(x=None, s1c=None, s1=None):
    return WorksheetData([
        _x("A", "Wavelength", x if x is not None else [300, 301, 302]),
        _y("S1c", "S1c", s1c if s1c is not None else [1, 2, 3]),
        _x("B", "Wavelength", x if x is not None else [300, 301, 302]),
        _y("S1", "S1", s1 if s1 is not None else [10, 20, 30]),
    ])


def _layout_s1c_r1c_s1(ratio, s1):
    return WorksheetData([
        _x("A", "Wavelength", [300, 301]),
        _y("S1c", "S1c", [1, 2]),
        _x("B", "Wavelength", [300, 301]),
        _y("S1cR1c", "S1c / R1c", ratio),
        _x("C", "Wavelength", [300, 301]),
        _y("S1", "S1", s1),
    ])


def _x(name, long_name, values):
    return Column(name, long_name, values, designation="X")


def _y(name, long_name, values):
    return Column(name, long_name, values, designation="Y")


if __name__ == "__main__":
    unittest.main()
