import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from spectrum_organizer.origin.data_columns import Column, DataColumnError, WorksheetData, select_xy_pair


class DataColumnsTests(unittest.TestCase):
    def test_matching_columns_finds_a_short_name_when_long_name_is_unrelated(self):
        short_name_only = Column("S1", "Detector Counts", [10, 20], designation="Y")
        data = WorksheetData([short_name_only])

        self.assertEqual((short_name_only,), data.matching_columns("S1"))

    def test_selects_immediately_preceding_x_for_s1c_layout(self):
        data = WorksheetData([
            Column("A", "Wavelength", [300, 301], designation="X"),
            Column("S1c", "S1c", [10, 20], designation="Y"),
            Column("B", "Wavelength", [300, 301], designation="X"),
            Column("S1", "S1", [100, 200], designation="Y"),
        ])
        pair = select_xy_pair(data, "S1c")
        self.assertEqual("A", pair.x_column.name)
        self.assertEqual("S1c", pair.y_column.name)

    def test_selects_s1c_over_r1c_by_short_or_long_name_and_its_own_x(self):
        data = WorksheetData([
            Column("A", "Wavelength", [300, 301], designation="X"),
            Column("S1c", "S1c", [10, 20], designation="Y"),
            Column("B", "Wavelength", [300, 301], designation="X"),
            Column("S1cR1c", "S1c / R1c", [1, 2], designation="Y"),
            Column("C", "Wavelength", [300, 301], designation="X"),
            Column("S1", "S1", [100, 200], designation="Y"),
        ])
        pair = select_xy_pair(data, "S1c/R1c")
        self.assertEqual("B", pair.x_column.name)
        self.assertEqual("S1cR1c", pair.y_column.name)

    def test_missing_selected_y_is_reported(self):
        data = WorksheetData([
            Column("A", "Wavelength", [300], designation="X"),
            Column("S1", "S1", [100], designation="Y"),
        ])
        with self.assertRaises(DataColumnError):
            select_xy_pair(data, "S1c/R1c")

    def test_duplicate_physical_selected_y_columns_are_ambiguous(self):
        data = WorksheetData([
            Column("A", "Wavelength", [300, 301], designation="X"),
            Column("S1c", "Corrected Signal", [10, 20], designation="Y"),
            Column("B", "Wavelength", [300, 301], designation="X"),
            Column("D", "S1c", [100, 200], designation="Y"),
        ])

        with self.assertRaisesRegex(DataColumnError, "Ambiguous selected Y"):
            select_xy_pair(data, "S1c")

    def test_rejects_immediately_preceding_non_x_designated_column(self):
        data = WorksheetData([
            Column("A", "Other signal", [1, 2], designation="Y"),
            Column("S1c", "S1c", [10, 20], designation="Y"),
        ])

        with self.assertRaisesRegex(DataColumnError, "preceding X-designated"):
            select_xy_pair(data, "S1c")


if __name__ == "__main__":
    unittest.main()
