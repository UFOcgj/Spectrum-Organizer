import math
import pathlib
import shutil
import sys
import unittest
import unittest.mock
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from spectrum_organizer.origin.extract_worker import (
    ExtractionOrchestrator,
    ExtractionSource,
    InfrastructureExtractionError,
    build_origin_extraction_worker_factory,
)
from spectrum_organizer.core.selection import convert_extracted_results
from spectrum_organizer.origin.session_adapters import OriginExtractionWorkerFactory
from spectrum_organizer.store.run_snapshot import RunSnapshot, validate_reconciled_sources


class WorkspaceTempDir:
    def __init__(self):
        self.root = ROOT / ".test-tmp" / "task4-real-extraction"
        self.path = self.root / f"case-{uuid.uuid4().hex}"

    def __enter__(self):
        self.path.mkdir(parents=True)
        return self.path

    def __exit__(self, exc_type, exc, tb):
        shutil.rmtree(self.path, ignore_errors=True)
        if self.root.exists() and not any(self.root.iterdir()):
            self.root.rmdir()


def _extract(worker, copy_path):
    inventory = tuple(worker.iter_inventory(copy_path, {copy_path}))
    results = tuple(result for _book, result in worker.iter_book_results())
    return inventory, results


class NoOpSourceManager:
    def verify_copy(self, source_id):
        pass

    def verify_original(self, source_id):
        pass


class RealExtractionContractTests(unittest.TestCase):
    def test_production_origin_session_is_hidden_before_worker_is_returned(self):
        events = []

        class FakePo:
            def LT_execute(self, command):
                events.append(("wait", command))

        class FakeOriginModule:
            oext = True
            po = FakePo()

            def set_show(self, show):
                events.append(("show", show))

        origin = FakeOriginModule()
        original_import = __import__

        def import_origin(name, *args, **kwargs):
            if name == "originpro":
                events.append(("import", name))
                return origin
            return original_import(name, *args, **kwargs)

        factory = build_origin_extraction_worker_factory()
        with unittest.mock.patch("builtins.__import__", side_effect=import_origin):
            worker = factory.create("SRC1", 1)

        self.assertIs(origin, worker.origin)
        self.assertEqual(
            [
                ("import", "originpro"),
                ("wait", "sec -poc"),
                ("show", False),
            ],
            events,
        )

    def test_origin_project_paths_are_stored_relative_to_the_project_root(self):
        note = _note("Spectral Acquisition[Emission]")
        data = FakeDataLayer(
            "Data",
            (("A", "Wavelength", [300]), ("S1c", "S1c", [1]), ("S1", "S1", [1])),
        )
        root_book = FakeBook("/RawProject/", "RootBook", "root", (FakeNoteLayer(note), data))
        child_book = FakeBook(
            "/RawProject/MFL_RT/",
            "ChildBook",
            "child",
            (FakeNoteLayer(note), data),
        )
        origin = FakeOrigin([root_book, child_book])
        origin.po.RootFolder = FakeFolder("/RawProject/", [root_book])
        origin.po.RootFolder.Folders = [FakeFolder("/RawProject/MFL_RT/", [child_book])]
        worker = OriginExtractionWorkerFactory(lambda: origin).create("SRC1", 1)

        inventory, results = _extract(worker, pathlib.Path("owned") / "copy.opju")

        self.assertEqual(["/", "MFL_RT"], [book.folder_path for book in inventory])
        self.assertEqual(["/", "MFL_RT"], [result.folder_path for result in results])

    def test_worker_exposes_only_two_streaming_passes(self):
        worker = OriginExtractionWorkerFactory(lambda: FakeOrigin([])).create("SRC1", 1)

        self.assertFalse(hasattr(worker, "extract"))
        self.assertFalse(hasattr(worker, "iter_book_transactions"))

    def test_worker_completes_inventory_pass_before_payload_pass(self):
        note = _note("Spectral Acquisition[Emission]")
        books = [
            FakeBook(
                "/",
                short_name,
                short_name,
                (
                    FakeNoteLayer(note),
                    FakeDataLayer(
                        "Data",
                        (("A", "Wavelength", [300]), ("S1c", "S1c", [1]), ("S1", "S1", [1])),
                    ),
                ),
            )
            for short_name in ("Book1", "Book2")
        ]
        origin = FakeOrigin(books)
        worker = OriginExtractionWorkerFactory(lambda: origin).create("SRC1", 1)

        inventory = tuple(worker.iter_inventory(pathlib.Path("owned") / "copy.opju", {pathlib.Path("owned") / "copy.opju"}))
        self.assertEqual([("page", "Book1"), ("page", "Book2")], [event for event in origin.events if event[0] == "page"])
        results = tuple(result for _book, result in worker.iter_book_results())

        self.assertEqual(2, len(inventory))
        self.assertEqual(2, len(results))
        page_events = [event for event in origin.events if event[0] == "page"]
        self.assertEqual([("page", "Book1"), ("page", "Book2"), ("page", "Book1"), ("page", "Book2")], page_events)

    def test_production_origin_failures_are_classified_for_infrastructure_retry(self):
        def failed_loader():
            raise RuntimeError("launch failed")

        with self.assertRaisesRegex(InfrastructureExtractionError, "Origin worker launch failed"):
            OriginExtractionWorkerFactory(failed_loader).create("SRC1", 1)

        origin = FakeOrigin([])
        origin.open = lambda *args, **kwargs: False
        worker = OriginExtractionWorkerFactory(lambda: origin).create("SRC1", 1)
        with self.assertRaisesRegex(InfrastructureExtractionError, "Origin open failed"):
            tuple(worker.iter_inventory(pathlib.Path("owned") / "copy.opju", {pathlib.Path("owned") / "copy.opju"}))

    def test_origin_session_disconnect_during_column_designation_is_infrastructure_failure(self):
        class DisconnectedColumn(FakeColumnObject):
            def GetType(self):
                error = RuntimeError("RPC disconnected")
                error.hresult = -2147417848
                raise error

        note = _note("Spectral Acquisition[Emission]")
        data = FakeDataLayer(
            "Data",
            (
                ("A", "Wavelength", [300]),
                ("S1c", "S1c", [1]),
                ("B", "Wavelength", [300]),
                ("S1", "S1", [1]),
            ),
        )
        data.obj = [
            DisconnectedColumn(
                column.short_name,
                column.long_name,
                origin_type=column._origin_type,
            )
            for column in data.obj
        ]
        origin = FakeOrigin([
            FakeBook(
                "/",
                "Book1",
                "designation disconnect",
                (FakeNoteLayer(note), data),
            )
        ])
        worker = OriginExtractionWorkerFactory(
            lambda: origin,
            s1_limit=100,
            steady_emission_y="S1c",
        ).create("SRC1", 1)
        copy_path = pathlib.Path("owned") / "copy.opju"

        tuple(worker.iter_inventory(copy_path, {copy_path}))
        with self.assertRaisesRegex(
            InfrastructureExtractionError,
            "Origin data session failed",
        ):
            tuple(worker.iter_book_results())

    def test_data_checksum_binds_origin_column_designations(self):
        note = _note("Spectral Acquisition[Emission]")

        def extract_with_designations(
            x_designation,
            y_designation,
        ):
            data = FakeDataLayer(
                "Data",
                (
                    ("A", "Wavelength", [300], x_designation),
                    ("S1c", "S1c", [1], y_designation),
                    ("B", "S1 X", [300], 3),
                    ("S1", "S1", [1], 0),
                ),
            )
            origin = FakeOrigin([
                FakeBook(
                    "/",
                    "Book1",
                    "designation checksum",
                    (FakeNoteLayer(note), data),
                )
            ])
            worker = OriginExtractionWorkerFactory(
                lambda: origin,
                s1_limit=100,
                steady_emission_y="S1c",
            ).create("SRC1", 1)
            _inventory, results = _extract(
                worker,
                pathlib.Path("owned") / "copy.opju",
            )
            return results[0]

        valid = extract_with_designations(3, 0)
        swapped = extract_with_designations(0, 3)

        self.assertNotEqual(
            valid.data_checksum,
            swapped.data_checksum,
        )

    def test_worker_extracts_note_data_payload_and_snapshot_persists_it(self):
        note = _note("Spectral Acquisition[Emission]")
        data = FakeDataLayer(
            "Data",
            (
                ("A", "Wavelength S1c", [300, 301, 302]),
                ("S1c", "S1c", [10, 20, 15]),
                ("B", "Wavelength ratio", [300, 301, 302]),
                ("S1cR1c", "S1c/R1c", [0.1, 0.3, 0.2]),
                ("C", "Wavelength S1", [300, 301, 302]),
                ("S1", "S1", [40, 50, 45]),
            ),
        )
        origin = FakeOrigin([FakeBook("/", "Book1", "MFL emission", (FakeNoteLayer(note), data))])
        worker = OriginExtractionWorkerFactory(lambda: origin, s1_limit=100, steady_emission_y="S1c/R1c").create("SRC1", 1)
        copy_path = pathlib.Path("owned") / "copy.opju"

        inventory, results = _extract(worker, copy_path)

        self.assertEqual(1, len(inventory))
        self.assertEqual("worksheet", inventory[0].page_type)
        result = results[0]
        self.assertEqual("worksheet", result.page_type)
        self.assertEqual("SRC1", result.source_id)
        self.assertEqual("/", result.folder_path)
        self.assertEqual("Book1", result.short_name)
        self.assertEqual("MFL emission", result.display_name)
        self.assertEqual(1, result.page_order)
        self.assertEqual(note, result.note_text)
        self.assertEqual("Data", result.data_sheet_name)
        self.assertEqual(
            ("Wavelength S1c", "S1c", "Wavelength ratio", "S1c/R1c", "Wavelength S1", "S1"),
            result.available_columns,
        )
        self.assertEqual("S1c/R1c", result.selected_y_column)
        self.assertEqual("Wavelength ratio", result.paired_x_column)
        self.assertEqual((300, 301, 302), result.selected_x_values)
        self.assertEqual((0.1, 0.3, 0.2), result.selected_y_values)
        self.assertEqual(0.3, result.max_planned_y)
        self.assertEqual(301, result.max_planned_y_x)
        self.assertEqual((300, 301, 302), result.s1_x_values)
        self.assertEqual((40, 50, 45), result.s1_values)
        self.assertEqual(50, result.s1_max_for_limit)
        self.assertEqual("ok", result.s1_limit_status)
        self.assertEqual("extracted", result.book_status)
        self.assertEqual("extracted", result.status)
        self.assertIsNone(result.rejection_reason)

        with WorkspaceTempDir() as root:
            snapshot = RunSnapshot(root / "run.sqlite3")
            snapshot.add_source("SRC1", copy_path, "sha")
            snapshot.replace_source_partition("SRC1", list(inventory), list(results))
            persisted = snapshot.book_results("SRC1")[0]

        self.assertEqual(result.page_type, persisted.page_type)
        self.assertEqual(result.note_text, persisted.note_text)
        self.assertEqual(result.data_sheet_name, persisted.data_sheet_name)
        self.assertEqual(result.available_columns, persisted.available_columns)
        self.assertEqual(result.column_metadata, persisted.column_metadata)
        self.assertEqual(result.selected_y_column, persisted.selected_y_column)
        self.assertEqual(result.paired_x_column, persisted.paired_x_column)
        self.assertEqual(result.selected_x_values, persisted.selected_x_values)
        self.assertEqual(result.selected_y_values, persisted.selected_y_values)
        self.assertEqual(result.s1_x_values, persisted.s1_x_values)
        self.assertEqual(result.s1_values, persisted.s1_values)
        self.assertEqual(result.max_planned_y, persisted.max_planned_y)
        self.assertEqual(result.max_planned_y_x, persisted.max_planned_y_x)
        self.assertEqual(result.s1_max_for_limit, persisted.s1_max_for_limit)
        self.assertEqual(result.s1_limit_status, persisted.s1_limit_status)
        self.assertEqual(result.data_checksum, persisted.data_checksum)

    def test_rejected_spectrum_preserves_all_x_values_at_tied_selected_y_maximum(self):
        note = _note("Spectral Acquisition[Emission]")
        data = FakeDataLayer(
            "Data",
            (
                ("A", "Wavelength S1c", [300, 301, 302]),
                ("S1c", "S1c", [20, 20, 10]),
                ("B", "Wavelength S1", [300, 301, 302]),
                ("S1", "S1", [90, 150, 80]),
            ),
        )
        origin = FakeOrigin([FakeBook("/", "Book1", "rejected tied maximum", (FakeNoteLayer(note), data))])
        worker = OriginExtractionWorkerFactory(
            lambda: origin,
            s1_limit=100,
            steady_emission_y="S1c",
        ).create("SRC1", 1)
        copy_path = pathlib.Path("owned") / "copy.opju"

        inventory, results = _extract(worker, copy_path)

        self.assertEqual("rejected", results[0].status)
        self.assertEqual("exceeds_limit", results[0].s1_limit_status)
        self.assertEqual((300, 301), results[0].max_planned_y_x)
        with WorkspaceTempDir() as root:
            snapshot = RunSnapshot(root / "run.sqlite3")
            snapshot.add_source("SRC1", copy_path, "sha")
            snapshot.replace_source_partition("SRC1", list(inventory), list(results))

            self.assertEqual([300, 301], snapshot.book_results("SRC1")[0].max_planned_y_x)

    def test_worker_reads_real_origin_note_text_from_note_worksheet_first_cell(self):
        note = _note("Phos Acquisition[Emission]", delayed=True).replace("Sample window", "Sample Window")
        origin = FakeOrigin([
            FakeBook(
                "/",
                "Book1",
                "real-note-shape",
                (
                    FakeWorksheetNoteLayer(note),
                    FakeDataLayer(
                        "Data",
                        (
                            ("A", "Wavelength", [300, 301]),
                            ("S1c", "S1c", [5, 10]),
                            ("B", "S1 X", [300, 301]),
                            ("S1", "S1", [50, 60]),
                        ),
                    ),
                ),
            )
        ])
        worker = OriginExtractionWorkerFactory(lambda: origin, s1_limit=100, steady_emission_y="S1c").create("SRC1", 1)

        _inventory, results = _extract(worker, pathlib.Path("owned") / "copy.opju")

        self.assertEqual("extracted", results[0].status)
        self.assertEqual(note, results[0].note_text)
        self.assertEqual("ok", results[0].s1_limit_status)

    def test_worker_ignores_blank_gettext_and_falls_back_to_note_worksheet(self):
        note = _note("Phos Acquisition[Emission]", delayed=True).replace("Sample window", "Sample Window")
        origin = FakeOrigin([
            FakeBook(
                "/",
                "Book1",
                "real-note-blank-gettext",
                (
                    FakeWorksheetNoteLayer(note, gettext=""),
                    FakeDataLayer(
                        "Data",
                        (
                            ("A", "Wavelength", [300, 301]),
                            ("S1c", "S1c", [5, 10]),
                            ("B", "S1 X", [300, 301]),
                            ("S1", "S1", [50, 60]),
                        ),
                    ),
                ),
            )
        ])
        worker = OriginExtractionWorkerFactory(lambda: origin, s1_limit=100, steady_emission_y="S1c").create("SRC1", 1)

        _inventory, results = _extract(worker, pathlib.Path("owned") / "copy.opju")

        self.assertEqual("extracted", results[0].status)
        self.assertEqual(note, results[0].note_text)

    def test_note_property_errors_do_not_block_worksheet_fallback(self):
        note = _note("Phos Acquisition[Emission]", delayed=True).replace("Sample window", "Sample Window")
        origin = FakeOrigin([
            FakeBook(
                "/",
                "Book1",
                "property-broken-note",
                (
                    FakePropertyBrokenWorksheetNoteLayer(note),
                    FakeDataLayer(
                        "Data",
                        (
                            ("A", "Wavelength", [300, 301]),
                            ("S1c", "S1c", [5, 10]),
                            ("B", "S1 X", [300, 301]),
                            ("S1", "S1", [50, 60]),
                        ),
                    ),
                ),
            )
        ])
        worker = OriginExtractionWorkerFactory(lambda: origin, s1_limit=100, steady_emission_y="S1c").create("SRC1", 1)

        _inventory, results = _extract(worker, pathlib.Path("owned") / "copy.opju")

        self.assertEqual("extracted", results[0].status)
        self.assertEqual(note, results[0].note_text)

    def test_gettext_property_error_does_not_block_worksheet_fallback(self):
        note = _note("Phos Acquisition[Emission]", delayed=True).replace("Sample window", "Sample Window")
        origin = FakeOrigin([
            FakeBook(
                "/",
                "Book1",
                "gettext-property-broken-note",
                (
                    FakeGetTextPropertyBrokenWorksheetNoteLayer(note),
                    FakeDataLayer(
                        "Data",
                        (
                            ("A", "Wavelength", [300, 301]),
                            ("S1c", "S1c", [5, 10]),
                            ("B", "S1 X", [300, 301]),
                            ("S1", "S1", [50, 60]),
                        ),
                    ),
                ),
            )
        ])
        worker = OriginExtractionWorkerFactory(lambda: origin, s1_limit=100, steady_emission_y="S1c").create("SRC1", 1)

        _inventory, results = _extract(worker, pathlib.Path("owned") / "copy.opju")

        self.assertEqual("extracted", results[0].status)
        self.assertEqual(note, results[0].note_text)

    def test_note_read_errors_are_reported_instead_of_collapsed_to_missing_note(self):
        origin = FakeOrigin([
            FakeBook(
                "/",
                "Book1",
                "broken-note",
                (
                    FakeBrokenNoteLayer(),
                    FakeDataLayer(
                        "Data",
                        (
                            ("A", "Wavelength", [300, 301]),
                            ("S1c", "S1c", [5, 10]),
                            ("B", "S1 X", [300, 301]),
                            ("S1", "S1", [50, 60]),
                        ),
                    ),
                ),
            )
        ])
        worker = OriginExtractionWorkerFactory(lambda: origin, s1_limit=100, steady_emission_y="S1c").create("SRC1", 1)

        _inventory, results = _extract(worker, pathlib.Path("owned") / "copy.opju")

        self.assertEqual("rejected", results[0].status)
        self.assertIn("Note read failed", results[0].rejection_reason)
        self.assertIn("GetText exploded", results[0].rejection_reason)
        self.assertIn("to_list exploded", results[0].rejection_reason)

    def test_data_read_error_rejects_only_that_book_and_continues_source(self):
        note = _note("Spectral Acquisition[Emission]")
        origin = FakeOrigin([
            FakeBook(
                "/",
                "BrokenData",
                "broken-data",
                (FakeNoteLayer(note), FakeBrokenDataLayer("Data")),
            ),
            FakeBook(
                "/",
                "GoodData",
                "good-data",
                (
                    FakeNoteLayer(note),
                    FakeDataLayer(
                        "Data",
                        (
                            ("A", "Wavelength", [300]),
                            ("S1c", "S1c", [5]),
                            ("B", "S1 X", [300]),
                            ("S1", "S1", [50]),
                        ),
                    ),
                ),
            ),
        ])
        worker = OriginExtractionWorkerFactory(
            lambda: origin,
            s1_limit=100,
            steady_emission_y="S1c",
        ).create("SRC1", 1)

        _inventory, results = _extract(worker, pathlib.Path("owned") / "copy.opju")

        self.assertEqual(2, len(results))
        self.assertEqual("rejected", results[0].status)
        self.assertIn("Data read failed", results[0].rejection_reason)
        self.assertIn("data to_list exploded", results[0].rejection_reason)
        self.assertEqual("extracted", results[1].status)

    def test_origin_session_disconnect_during_data_read_is_infrastructure_failure(self):
        note = _note("Spectral Acquisition[Emission]")

        class DisconnectedComError(Exception):
            hresult = -2147417848

        class DisconnectedOrigin(FakeOrigin):
            def WSheet(self, layer):
                del layer
                raise DisconnectedComError("RPC_E_DISCONNECTED")

        origin = DisconnectedOrigin([
            FakeBook(
                "/",
                "Book1",
                "book-1",
                (FakeNoteLayer(note), FakeDataLayer("Data", (("A", "Wavelength", [300]),))),
            )
        ])
        worker = OriginExtractionWorkerFactory(
            lambda: origin,
            s1_limit=100,
            steady_emission_y="S1c",
        ).create("SRC1", 1)
        copy_path = pathlib.Path("owned") / "copy.opju"
        tuple(worker.iter_inventory(copy_path, {copy_path}))

        with self.assertRaises(InfrastructureExtractionError):
            tuple(worker.iter_book_results())

    def test_origin_session_disconnect_during_note_read_is_infrastructure_failure(self):
        class DisconnectedComError(Exception):
            hresult = -2147417848

        class DisconnectedNoteLayer(FakeNoteLayer):
            def GetText(self):
                raise DisconnectedComError("RPC_E_DISCONNECTED")

        note = _note("Spectral Acquisition[Emission]")
        origin = FakeOrigin([
            FakeBook(
                "/",
                "Book1",
                "book-1",
                (
                    DisconnectedNoteLayer(note),
                    FakeDataLayer("Data", (("A", "Wavelength", [300]),)),
                ),
            )
        ])
        worker = OriginExtractionWorkerFactory(lambda: origin).create("SRC1", 1)
        copy_path = pathlib.Path("owned") / "copy.opju"
        tuple(worker.iter_inventory(copy_path, {copy_path}))

        with self.assertRaises(InfrastructureExtractionError):
            tuple(worker.iter_book_results())

    def test_missing_selected_y_rejects_with_payload_metadata(self):
        origin = FakeOrigin([
            FakeBook(
                "/",
                "Book1",
                "excitation",
                (
                    FakeNoteLayer(_note("Spectral Acquisition[Excitation]")),
                    FakeDataLayer(
                        "Data",
                        (
                            ("A", "Wavelength", [300]),
                            ("S1c", "S1c", [1]),
                            ("B", "S1 X", [300]),
                            ("S1", "S1", [10]),
                        ),
                    ),
                ),
            )
        ])
        worker = OriginExtractionWorkerFactory(lambda: origin, s1_limit=100, steady_emission_y="S1c").create("SRC1", 1)

        _inventory, results = _extract(worker, pathlib.Path("owned") / "copy.opju")

        result = results[0]
        self.assertEqual("rejected", result.status)
        self.assertEqual("S1c/R1c", result.selected_y_column)
        self.assertIn("missing selected Y", result.rejection_reason)

    def test_missing_s1_requires_explicit_opt_in_but_keeps_selected_y_and_x(self):
        def run_case(allow_missing_s1):
            origin = FakeOrigin([
                FakeBook(
                    "/",
                    "Book1",
                    "steady emission",
                    (
                        FakeNoteLayer(_note("Spectral Acquisition[Emission]")),
                        FakeDataLayer(
                            "Data",
                            (
                                ("A", "Wavelength", [300, 301]),
                                ("S1c", "S1c", [5, 10]),
                            ),
                        ),
                    ),
                )
            ])
            worker = OriginExtractionWorkerFactory(
                lambda: origin,
                s1_limit=100,
                steady_emission_y="S1c",
                allow_missing_s1=allow_missing_s1,
            ).create("SRC1", 1)
            return _extract(worker, pathlib.Path("owned") / "copy.opju")[1][0]

        rejected = run_case(False)
        allowed = run_case(True)

        self.assertEqual("rejected", rejected.status)
        self.assertIn("missing S1", rejected.rejection_reason)
        self.assertEqual("extracted", allowed.status)
        self.assertEqual("missing_allowed", allowed.s1_limit_status)
        self.assertEqual("S1c", allowed.selected_y_column)
        self.assertEqual("Wavelength", allowed.paired_x_column)
        self.assertEqual((300, 301), allowed.selected_x_values)
        self.assertEqual((5, 10), allowed.selected_y_values)
        self.assertIsNone(allowed.s1_max_for_limit)
        self.assertIsNone(allowed.s1_max_for_limit_x)

    def test_all_blank_s1_is_extracted_as_approved_missing_s1(self):
        origin = FakeOrigin([
            FakeBook(
                "/",
                "Book1",
                "steady emission",
                (
                    FakeNoteLayer(_note("Spectral Acquisition[Emission]")),
                    FakeDataLayer(
                        "Data",
                        (
                            ("A", "Wavelength", [300, 301]),
                            ("S1c", "S1c", [5, 10]),
                            ("B", "S1", [None, None]),
                        ),
                    ),
                ),
            )
        ])
        worker = OriginExtractionWorkerFactory(
            lambda: origin,
            s1_limit=100,
            steady_emission_y="S1c",
            allow_missing_s1=True,
        ).create("SRC1", 1)

        _inventory, results = _extract(worker, pathlib.Path("owned") / "copy.opju")

        self.assertEqual("extracted", results[0].status)
        self.assertEqual("missing_allowed", results[0].s1_limit_status)
        self.assertIn("S1", results[0].available_columns)
        self.assertIsNone(results[0].s1_max_for_limit)

    def test_short_name_only_s1_is_measured_and_reconciles_from_snapshot(self):
        origin = FakeOrigin([
            FakeBook(
                "/",
                "Book1",
                "steady emission",
                (
                    FakeNoteLayer(_note("Spectral Acquisition[Emission]")),
                    FakeDataLayer(
                        "Data",
                        (
                            ("A", "Wavelength", [300, 301]),
                            ("S1c", "S1c", [5, 10]),
                            ("B", "Wavelength", [300, 301]),
                            ("S1", "Detector Counts", [10, 90]),
                        ),
                    ),
                ),
            )
        ])
        worker = OriginExtractionWorkerFactory(
            lambda: origin,
            s1_limit=100,
            steady_emission_y="S1c",
            allow_missing_s1=True,
        ).create("SRC1", 1)

        inventory, results = _extract(worker, pathlib.Path("owned") / "copy.opju")
        result = results[0]

        self.assertEqual("extracted", result.status)
        self.assertEqual("ok", result.s1_limit_status)
        self.assertEqual(90, result.s1_max_for_limit)
        self.assertEqual(301, result.s1_max_for_limit_x)
        self.assertEqual(1, result.available_columns.count("S1"))

        with WorkspaceTempDir() as root:
            path = root / "run.sqlite3"
            snapshot = RunSnapshot(path)
            snapshot.add_source("SRC1", pathlib.Path("copy.opju"), "sha")
            snapshot.replace_source_partition("SRC1", list(inventory), list(results))
            validate_reconciled_sources(
                path,
                ("SRC1",),
                s1_limit=100,
                steady_emission_y="S1c",
                allow_missing_s1=True,
            )

    def test_short_name_only_selected_y_keeps_identity_and_reconciles_from_snapshot(self):
        origin = FakeOrigin([
            FakeBook(
                "/",
                "Book1",
                "steady emission",
                (
                    FakeNoteLayer(_note("Spectral Acquisition[Emission]")),
                    FakeDataLayer(
                        "Data",
                        (
                            ("A", "Wavelength", [300, 301]),
                            ("S1c", "Corrected Signal", [5, 10]),
                            ("B", "Wavelength", [300, 301]),
                            ("S1", "Detector Counts", [10, 90]),
                        ),
                    ),
                ),
            )
        ])
        worker = OriginExtractionWorkerFactory(
            lambda: origin,
            s1_limit=100,
            steady_emission_y="S1c",
        ).create("SRC1", 1)

        inventory, results = _extract(worker, pathlib.Path("owned") / "copy.opju")
        result = results[0]

        self.assertEqual("extracted", result.status)
        self.assertEqual(1, result.available_columns.count("S1c"))
        self.assertNotIn("Corrected Signal", result.available_columns)
        self.assertEqual((300, 301), result.selected_x_values)
        self.assertEqual((5, 10), result.selected_y_values)

        with WorkspaceTempDir() as root:
            path = root / "run.sqlite3"
            snapshot = RunSnapshot(path)
            snapshot.add_source("SRC1", pathlib.Path("copy.opju"), "sha")
            snapshot.replace_source_partition("SRC1", list(inventory), list(results))
            validate_reconciled_sources(
                path,
                ("SRC1",),
                s1_limit=100,
                steady_emission_y="S1c",
                allow_missing_s1=False,
            )

    def test_one_physical_column_cannot_supply_both_s1_and_selected_y(self):
        origin = FakeOrigin([
            FakeBook(
                "/",
                "Book1",
                "steady emission",
                (
                    FakeNoteLayer(_note("Spectral Acquisition[Emission]")),
                    FakeDataLayer(
                        "Data",
                        (
                            ("A", "Wavelength", [300, 301]),
                            ("S1", "S1c", [5, 10]),
                        ),
                    ),
                ),
            )
        ])

        with WorkspaceTempDir() as root:
            copy_dir = root / "copies" / "SRC1"
            copy_dir.mkdir(parents=True)
            copy_path = copy_dir / "copy.bin"
            copy_path.write_bytes(b"copy")
            original_path = root / "original.bin"
            original_path.write_bytes(b"original")
            snapshot = RunSnapshot(root / "run.sqlite3")
            source = ExtractionSource(
                source_id="SRC1",
                copy_path=copy_path,
                sha256="sha",
                original_path=original_path,
                allowed_children=(copy_dir,),
                protected_paths=(original_path,),
            )
            worker_factory = OriginExtractionWorkerFactory(
                lambda: origin,
                s1_limit=100,
                steady_emission_y="S1c",
            )

            ExtractionOrchestrator(
                snapshot,
                worker_factory,
                NoOpSourceManager(),
                max_attempts=1,
                s1_limit=100,
                steady_emission_y="S1c",
            ).run((source,))

            self.assertEqual(1, snapshot.result_count("SRC1"))
            result = snapshot.book_results("SRC1")[0]
            self.assertEqual("rejected", result.status)
            self.assertEqual(
                "selected Y and S1 resolve to the same physical column: "
                "S1c",
                result.rejection_reason,
            )

    def test_duplicate_physical_selected_y_columns_are_rejected_as_ambiguous(self):
        origin = FakeOrigin([
            FakeBook(
                "/",
                "Book1",
                "steady emission",
                (
                    FakeNoteLayer(_note("Spectral Acquisition[Emission]")),
                    FakeDataLayer(
                        "Data",
                        (
                            ("A", "Wavelength", [300, 301]),
                            ("S1c", "Corrected Signal", [5, 10]),
                            ("B", "Wavelength", [300, 301]),
                            ("D", "S1c", [50, 100]),
                            ("C", "Wavelength", [300, 301]),
                            ("S1", "Detector Counts", [10, 90]),
                        ),
                    ),
                ),
            )
        ])
        worker = OriginExtractionWorkerFactory(
            lambda: origin,
            s1_limit=100,
            steady_emission_y="S1c",
        ).create("SRC1", 1)

        inventory, results = _extract(worker, pathlib.Path("owned") / "copy.opju")
        result = results[0]

        self.assertEqual("rejected", result.status)
        self.assertIn("ambiguous selected Y", result.rejection_reason)
        self.assertEqual(2, result.available_columns.count("S1c"))

        with WorkspaceTempDir() as root:
            path = root / "run.sqlite3"
            snapshot = RunSnapshot(path)
            snapshot.add_source("SRC1", pathlib.Path("copy.opju"), "sha")
            snapshot.replace_source_partition("SRC1", list(inventory), list(results))
            validate_reconciled_sources(
                path,
                ("SRC1",),
                s1_limit=100,
                steady_emission_y="S1c",
                allow_missing_s1=False,
            )

    def test_duplicate_selected_x_reaches_a_terminal_worker_rejection(self):
        origin = FakeOrigin([
            FakeBook(
                "/",
                "Book1",
                "duplicate X",
                (
                    FakeNoteLayer(_note("Spectral Acquisition[Emission]")),
                    FakeDataLayer(
                        "Data",
                        (
                            ("A", "Wavelength", [300, 300]),
                            ("S1c", "Corrected Signal", [5, 10]),
                            ("B", "Wavelength", [300, 301]),
                            ("S1", "Detector Counts", [10, 20]),
                        ),
                    ),
                ),
            )
        ])
        worker = OriginExtractionWorkerFactory(
            lambda: origin,
            s1_limit=100,
            steady_emission_y="S1c",
        ).create("SRC1", 1)

        inventory, results = _extract(
            worker,
            pathlib.Path("owned") / "copy.opju",
        )

        self.assertEqual("rejected", results[0].status)
        self.assertEqual(
            "duplicate value in column Wavelength at row 2",
            results[0].rejection_reason,
        )
        self.assertEqual((300, 300), results[0].selected_x_values)
        self.assertEqual((5, 10), results[0].selected_y_values)
        self.assertEqual(2, results[0].selected_x_row_count)
        self.assertEqual(2, results[0].selected_y_row_count)

        with WorkspaceTempDir() as root:
            path = root / "run.sqlite3"
            snapshot = RunSnapshot(path)
            snapshot.add_source("SRC1", pathlib.Path("copy.opju"), "sha")
            snapshot.replace_source_partition(
                "SRC1",
                list(inventory),
                list(results),
            )
            validate_reconciled_sources(
                path,
                ("SRC1",),
                s1_limit=100,
                steady_emission_y="S1c",
                allow_missing_s1=False,
            )

    def test_missing_slits_reaches_candidate_rejection_after_real_extraction(self):
        note = (
            "[EXP_FD_FILE]\n"
            "Acquisition Type = Spectral Acquisition[Emission]\n"
            "[EX1]\n"
            "Park = 270\n"
            "[EM1]\n"
            "Start = 300\n"
            "End = 650\n"
            "Increment = 1"
        )
        origin = FakeOrigin([
            FakeBook(
                "/",
                "Book1",
                "missing slits",
                (
                    FakeNoteLayer(note),
                    FakeDataLayer(
                        "Data",
                        (
                            ("A", "Wavelength", [300, 301]),
                            ("S1c", "Corrected Signal", [5, 10]),
                            ("B", "Wavelength", [300, 301]),
                            ("S1", "Detector Counts", [10, 20]),
                        ),
                    ),
                ),
            )
        ])
        worker = OriginExtractionWorkerFactory(
            lambda: origin,
            s1_limit=100,
            steady_emission_y="S1c",
        ).create("SRC1", 1)
        _inventory, results = _extract(
            worker,
            pathlib.Path("owned") / "copy.opju",
        )

        converted = convert_extracted_results(
            results,
            source_filenames={"SRC1": "source.opju"},
            expected_source_ids=("SRC1",),
        )

        self.assertEqual("extracted", results[0].status)
        self.assertEqual((), converted.ordinary_candidates)
        self.assertEqual(
            ("Note is missing excitation slits",),
            tuple(item.reason for item in converted.rejections),
        )

    def test_origin_nan_s1_blanks_follow_missing_and_internal_blank_rules(self):
        def run_case(values):
            origin = FakeOrigin([
                FakeBook(
                    "/",
                    "Book1",
                    "steady emission",
                    (
                        FakeNoteLayer(_note("Spectral Acquisition[Emission]")),
                        FakeDataLayer(
                            "Data",
                            (
                                ("A", "Wavelength", [300, 301, 302]),
                                ("S1c", "S1c", [5, 10, 15]),
                                ("B", "Wavelength", [300, 301, 302]),
                                ("S1", "S1", values),
                            ),
                        ),
                    ),
                )
            ])
            worker = OriginExtractionWorkerFactory(
                lambda: origin,
                s1_limit=100,
                steady_emission_y="S1c",
                allow_missing_s1=True,
            ).create("SRC1", 1)
            return _extract(worker, pathlib.Path("owned") / "copy.opju")[1][0]

        all_blank = run_case([math.nan, math.nan, math.nan])
        internal_blank = run_case([10, math.nan, 30])

        self.assertEqual("extracted", all_blank.status)
        self.assertEqual("missing_allowed", all_blank.s1_limit_status)
        self.assertIsNone(all_blank.s1_max_for_limit)
        self.assertEqual("rejected", internal_blank.status)
        self.assertEqual("blank in column S1 at row 2", internal_blank.rejection_reason)

    def test_origin_shared_trailing_nan_rows_are_trimmed(self):
        origin = FakeOrigin([
            FakeBook(
                "/",
                "Book1",
                "steady emission",
                (
                    FakeNoteLayer(_note("Spectral Acquisition[Emission]")),
                    FakeDataLayer(
                        "Data",
                        (
                            ("A", "Wavelength", [300, 301, math.nan]),
                            ("S1c", "S1c", [5, 10, math.nan]),
                            ("B", "Wavelength", [300, 301, math.nan]),
                            ("S1", "S1", [10, 90, math.nan]),
                        ),
                    ),
                ),
            )
        ])
        worker = OriginExtractionWorkerFactory(
            lambda: origin,
            s1_limit=100,
            steady_emission_y="S1c",
        ).create("SRC1", 1)

        _inventory, results = _extract(worker, pathlib.Path("owned") / "copy.opju")

        self.assertEqual("extracted", results[0].status)
        self.assertEqual((300, 301), results[0].selected_x_values)
        self.assertEqual((5, 10), results[0].selected_y_values)
        self.assertEqual(90, results[0].s1_max_for_limit)

    def test_duplicate_physical_s1_columns_are_rejected_as_ambiguous(self):
        origin = FakeOrigin([
            FakeBook(
                "/",
                "Book1",
                "steady emission",
                (
                    FakeNoteLayer(_note("Spectral Acquisition[Emission]")),
                    FakeDataLayer(
                        "Data",
                        (
                            ("A", "Wavelength", [300, 301]),
                            ("S1c", "S1c", [5, 10]),
                            ("B", "Wavelength", [300, 301]),
                            ("S1", "S1", [None, None]),
                            ("C", "Wavelength", [300, 301]),
                            ("D", "S1", [10, 101]),
                        ),
                    ),
                ),
            )
        ])
        worker = OriginExtractionWorkerFactory(
            lambda: origin,
            s1_limit=100,
            steady_emission_y="S1c",
            allow_missing_s1=True,
        ).create("SRC1", 1)

        _inventory, results = _extract(worker, pathlib.Path("owned") / "copy.opju")

        self.assertEqual("rejected", results[0].status)
        self.assertEqual("failed", results[0].s1_limit_status)
        self.assertIn("ambiguous S1", results[0].rejection_reason)
        self.assertEqual(2, results[0].available_columns.count("S1"))

    def test_s1_saturation_rejects_with_actual_s1_max_and_x(self):
        origin = FakeOrigin([
            FakeBook(
                "/",
                "Book1",
                "delayed emission",
                (
                    FakeNoteLayer(_note("Phos Acquisition[Emission]", delayed=True)),
                    FakeDataLayer(
                        "Data",
                        (
                            ("A", "Wavelength", [300, 301]),
                            ("S1c", "S1c", [1, 2]),
                            ("B", "S1 X", [400, 401]),
                            ("S1", "S1", [90, 150]),
                        ),
                    ),
                ),
            )
        ])
        worker = OriginExtractionWorkerFactory(lambda: origin, s1_limit=100, steady_emission_y="S1c/R1c").create("SRC1", 1)

        _inventory, results = _extract(worker, pathlib.Path("owned") / "copy.opju")

        result = results[0]
        self.assertEqual("rejected", result.status)
        self.assertEqual(150, result.s1_max_for_limit)
        self.assertEqual(301, result.max_planned_y_x)
        self.assertEqual(401, result.s1_max_for_limit_x)
        self.assertEqual("exceeds_limit", result.s1_limit_status)
        self.assertIn("S1 max exceeds limit", result.rejection_reason)

    def test_steady_2d_is_one_special_book_and_skips_s1_limit(self):
        origin = FakeOrigin([
            FakeBook(
                "/",
                "Book2D",
                "steady 2D",
                (
                    FakeNoteLayer(_note("3D Acquisition[Excitation vs Emission vs Intensity]")),
                    FakeDataLayer("Data_S1c", (("A", "Ex", [300]), ("S1c", "S1c", [1]))),
                    FakeDataLayer("Data_S1", (("A", "Ex", [300]), ("S1", "S1", [9_000_000]))),
                ),
            )
        ])
        worker = OriginExtractionWorkerFactory(lambda: origin, s1_limit=1, steady_emission_y="S1c").create("SRC1", 1)

        inventory, results = _extract(worker, pathlib.Path("owned") / "copy.opju")

        self.assertEqual(1, len(inventory))
        self.assertEqual(("Note", "Data_S1c", "Data_S1"), inventory[0].sheet_names)
        self.assertEqual("extracted", results[0].status)
        self.assertEqual("not_applicable", results[0].s1_limit_status)
        self.assertIsNone(results[0].rejection_reason)


def _note(acquisition_type, delayed=False):
    lines = ["[EXP_FD_FILE]", f"Acquisition Type = {acquisition_type}"]
    if delayed:
        lines.extend([
            "Flash Delay = 0.1",
            "Sample window = 1",
            "Time per Flash = 0.1",
            "Flash Count = 100",
        ])
    return "\n".join(lines)


class FakeOrigin:
    def __init__(self, books):
        self.events = []
        self.po = FakePO(books, self.events)

    def open(self, path, readonly, asksave):
        self.events.append(("open", path, readonly, asksave))
        return True

    def WSheet(self, layer):
        return layer


class FakePO:
    OPT_WORKSHEET = 1

    def __init__(self, books, events=None):
        self.RootFolder = FakeFolder("/", books)
        self._pages = {book.short_name: FakePage(book.layers) for book in books}
        self._events = events if events is not None else []

    def Pages(self, name):
        self._events.append(("page", name))
        return self._pages[name]


class FakeFolder:
    def __init__(self, path, books):
        self._path = path
        self.Folders = []
        self._books = books

    def GetPEPath(self):
        return self._path

    def PageBases(self):
        return [FakePageBase(book.short_name, book.display_name) for book in self._books]


class FakeBook:
    def __init__(self, folder_path, short_name, display_name, layers):
        self.folder_path = folder_path
        self.short_name = short_name
        self.display_name = display_name
        self.layers = layers


class FakePageBase:
    def __init__(self, short_name, display_name):
        self._short_name = short_name
        self._display_name = display_name

    def GetType(self):
        return FakePO.OPT_WORKSHEET

    def GetName(self):
        return self._short_name

    def GetLongName(self):
        return self._display_name


class FakePage:
    def __init__(self, layers):
        self.Layers = list(layers)


class FakeNoteLayer:
    def __init__(self, text):
        self.text = text

    def GetLongName(self):
        return "Note"

    def GetName(self):
        return "Note"

    def GetText(self):
        return self.text


class FakeWorksheetNoteLayer:
    def __init__(self, text, gettext=None):
        self._text = text
        self._gettext = gettext
        self.cols = 1

    def GetLongName(self):
        return "Note"

    def GetName(self):
        return "Note"

    def GetText(self):
        return self._gettext

    def to_list(self, index):
        if index != 0:
            return []
        return [self._text]

    def get_label(self, index, label):
        return None


class FakeGetTextPropertyBrokenWorksheetNoteLayer:
    def __init__(self, note):
        self.short_name = "Note"
        self._note = note
        self.cols = 1

    def GetLongName(self):
        return "Note"

    def GetName(self):
        return "Note"

    @property
    def GetText(self):
        raise RuntimeError("GetText attribute failed")

    def to_list(self, index):
        if index != 0:
            return []
        return [self._note]

    def get_label(self, index, label):
        return None


class FakePropertyBrokenWorksheetNoteLayer:
    def __init__(self, text):
        self._text = text
        self.cols = 1

    def GetLongName(self):
        return "Note"

    def GetName(self):
        return "Note"

    def GetText(self):
        raise RuntimeError("GetText exploded")

    @property
    def text(self):
        raise RuntimeError("text property failed")

    @property
    def note_text(self):
        raise RuntimeError("note_text property failed")

    def to_list(self, index):
        if index != 0:
            return []
        return [self._text]

    def get_label(self, index, label):
        return None

class FakeBrokenNoteLayer:
    cols = 1

    def GetLongName(self):
        return "Note"

    def GetName(self):
        return "Note"

    def GetText(self):
        raise RuntimeError("GetText exploded")

    def to_list(self, index):
        raise RuntimeError("to_list exploded")

    def get_label(self, index, label):
        return None

class FakeDataLayer:
    def __init__(self, name, columns):
        self._name = name
        self._columns = tuple(columns)
        self.cols = len(self._columns)
        self.obj = [
            FakeColumnObject(
                column[0],
                column[1],
                origin_type=(
                    column[3]
                    if len(column) == 4
                    else _default_origin_column_type(column[0], column[1])
                ),
            )
            for column in self._columns
        ]

    def GetLongName(self):
        return self._name

    def GetName(self):
        return self._name

    def to_list(self, index):
        return list(self._columns[index][2])

    def get_label(self, index, label):
        if label == "L":
            return self._columns[index][1]
        return None


class FakeBrokenDataLayer(FakeDataLayer):
    def __init__(self, name):
        super().__init__(name, (("A", "Wavelength", [300]),))

    def to_list(self, index):
        raise RuntimeError("data to_list exploded")


class FakeColumnObject:
    def __init__(self, short_name, long_name, *, origin_type):
        self.short_name = short_name
        self.long_name = long_name
        self._origin_type = origin_type

    def GetLongName(self):
        return self.long_name

    def GetName(self):
        return self.short_name

    def GetType(self):
        return self._origin_type


def _default_origin_column_type(short_name, long_name):
    normalized_long_name = str(long_name).strip().casefold()
    if (
        "wavelength" in normalized_long_name
        or normalized_long_name.endswith(" x")
        or normalized_long_name == "ex"
    ):
        return 3
    role = f"{short_name} {long_name}".replace(" ", "").replace("/", "").casefold()
    return 0 if "s1" in role else 3


if __name__ == "__main__":
    unittest.main()
