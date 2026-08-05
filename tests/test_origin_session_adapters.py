from decimal import Decimal
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from spectrum_organizer.origin.output_worker import ColumnWriteContract
from spectrum_organizer.origin.contracts import OriginStructureMismatchError
from spectrum_organizer.origin.session_adapters import (
    OriginDependencyProof,
    OriginExtractionWorkerFactory,
    OriginOutputSession,
    OriginVerifierSession,
)


class OriginSessionAdapterTests(unittest.TestCase):
    def test_output_session_wraps_originpro_creation_save_and_column_api(self):
        op = FakeOriginModule()
        session = OriginOutputSession(op)
        column = ColumnWriteContract(
            short_name="B",
            designation="Y",
            comment="MFL-mTHF-77 K_F270",
            values=(Decimal("1.5"), None, Decimal("3")),
            formula="col(A)/max(col(A))",
            method="Divided by Max of A",
        )

        session.new()
        session.delete_default_template_book()
        root = session.root_folder_path()
        folder = session.add_folder(root, "F_Ex270_ExSlit2_EmSlit2")
        sheet = session.add_book(folder, "MFL-mTHF")
        session.write_column(sheet, column)
        method = session.method_row("B")
        session.save(pathlib.Path("run") / "Organized_Spectra_20260629_120000.opju")

        self.assertEqual("/", root)
        self.assertEqual("/F_Ex270_ExSlit2_EmSlit2", folder)
        self.assertEqual("MFL-mTHF", sheet.book.lname)
        self.assertEqual([1.5, None, 3.0], sheet.values[1])
        self.assertEqual("", sheet.labels[(1, "L")])
        self.assertEqual("MFL-mTHF-77 K_F270", sheet.labels[(1, "C")])
        self.assertEqual("Y", sheet.axes[1])
        self.assertEqual("col(A)/max(col(A))", sheet.formulas[1])
        self.assertEqual(1, sheet.obj[1].numeric_properties["svrm"])
        self.assertEqual("Divided by Max of A", method)
        self.assertIn(("lt_exec", "wrowheight [C:C] 5;"), sheet.events)
        self.assertIn(("lt_exec", "wrowheight [Method:Method] 2;"), sheet.events)
        self.assertIn(("lt_exec", "wrowheight [O:O] 2;"), sheet.events)
        self.assertIn(("new", False), op.events)
        self.assertIn(("destroy_default_book",), op.events)
        self.assertIn(("rename_default_folder", "Folder1", "F_Ex270_ExSlit2_EmSlit2"), op.events)
        self.assertIn(("new_book", "w", "", False), op.events)
        self.assertIn(("save", "run\\Organized_Spectra_20260629_120000.opju"), op.events)

    def test_verifier_session_opens_readonly_and_reads_project_contract(self):
        op = FakeOriginModule()
        session = OriginVerifierSession(op)
        contract = object()
        session._contract_reader = lambda: contract

        session.open(pathlib.Path("staged.opju"), True)

        self.assertEqual([("open", "staged.opju", True, True)], op.events)
        self.assertIs(contract, session.read_project_contract())

    def test_verifier_session_normalizes_origin_pe_folder_path_to_output_folder_name(self):
        op = FakeOriginModule()
        op.install_verifier_child_book(
            root_path="/Organized_Spectra_20260630_120149/",
            folder_path="/Organized_Spectra_20260630_120149/F_Ex270_ExSlit2_EmSlit2_ALL_SAMPLES/",
            short_name="Book1",
            long_name="MFL-mTHF-1x10^-4 M",
            worksheet=FakeReadbackSheet(),
        )
        session = OriginVerifierSession(op)

        contract = session.read_project_contract()

        self.assertEqual("/", contract.root_path)
        self.assertEqual("F_Ex270_ExSlit2_EmSlit2_ALL_SAMPLES", contract.folders[0].path)

    def test_verifier_reader_rejects_unexpected_pages_empty_folders_and_layer_counts(self):
        cases = []

        graph_origin = FakeOriginModule()
        graph_origin.install_verifier_child_book(
            root_path="/Organized/",
            folder_path="/Organized/F_Ex270/",
            short_name="Book1",
            long_name="A",
            worksheet=FakeReadbackSheet(),
        )
        graph_origin.po.RootFolder.Folders[0]._page_bases.append(
            FakePageBase("Graph1", "Unexpected graph", page_type=2)
        )
        cases.append(("page type", graph_origin, "unsupported page type"))

        empty_origin = FakeOriginModule()
        empty_origin.install_verifier_child_book(
            root_path="/Organized/",
            folder_path="/Organized/F_Ex270/",
            short_name="Book1",
            long_name="A",
            worksheet=FakeReadbackSheet(),
        )
        empty_origin.po.RootFolder.Folders.append(
            FakeFolderNode("/Organized/Empty/", (), ())
        )
        cases.append(("empty folder", empty_origin, "empty output folder"))

        layered_origin = FakeOriginModule()
        sheet = FakeReadbackSheet()
        layered_origin.install_verifier_child_book(
            root_path="/Organized/",
            folder_path="/Organized/F_Ex270/",
            short_name="Book1",
            long_name="A",
            worksheet=sheet,
        )
        layered_origin.po._page = FakePage((sheet, sheet))
        cases.append(("layer count", layered_origin, "exactly one worksheet layer"))

        for label, origin, error in cases:
            with self.subTest(label=label), self.assertRaisesRegex(
                OriginStructureMismatchError,
                error,
            ):
                OriginVerifierSession(origin).read_project_contract()

    def test_verifier_session_reads_origin_column_type_as_designation(self):
        op = FakeOriginModule()
        op.install_verifier_child_book(
            root_path="/Organized_Spectra_20260630_120651/",
            folder_path="/Organized_Spectra_20260630_120651/F_Ex270_ExSlit2_EmSlit2_ALL_SAMPLES/",
            short_name="Book1",
            long_name="MFL-mTHF-1x10^-4 M",
            worksheet=FakeReadbackSheet(
                columns=(
                    FakeReadbackColumn(origin_type=3),
                    FakeReadbackColumn(origin_type=0),
                ),
                labels=("A", "B"),
            ),
        )
        session = OriginVerifierSession(op)

        contract = session.read_project_contract()

        columns = contract.folders[0].books[0].columns
        self.assertEqual("X", columns[0].designation)
        self.assertEqual("Y", columns[1].designation)

    def test_verifier_session_normalizes_blank_method_to_none(self):
        op = FakeOriginModule()
        op.install_verifier_child_book(
            root_path="/Organized_Spectra_20260630_122158/",
            folder_path="/Organized_Spectra_20260630_122158/F_Ex270_ExSlit2_EmSlit2_ALL_SAMPLES/",
            short_name="Book1",
            long_name="MFL-mTHF-1x10^-4 M",
            worksheet=FakeReadbackSheet(method_labels=("",)),
        )
        session = OriginVerifierSession(op)

        contract = session.read_project_contract()

        self.assertIsNone(contract.folders[0].books[0].columns[0].method)

    def test_verifier_session_normalizes_origin_nan_blank_sentinel_to_none(self):
        op = FakeOriginModule()
        op.install_verifier_child_book(
            root_path="/Organized/",
            folder_path="/Organized/F_Ex270/",
            short_name="Book1",
            long_name="DFL-mTHF-1x10^-4 M",
            worksheet=FakeReadbackSheet(values=([1.0, float("nan"), 3.0],)),
        )

        contract = OriginVerifierSession(op).read_project_contract()

        self.assertEqual(
            (Decimal("1.0"), None, Decimal("3.0")),
            contract.folders[0].books[0].columns[0].values,
        )

    def test_dependency_proof_opens_mutation_and_asserts_raw_norm_pairs(self):
        origin = FakeOriginModule()
        origin.install_dependency_book("Book")
        proof = OriginDependencyProof(origin)

        proof.open(pathlib.Path("mutation.opju"), False)
        calculation_state = proof.assert_raw_to_norm_live(
            "/", "Book", "B", "C"
        )

        self.assertEqual(("open", "mutation.opju", False, False), proof.origin.events[0])
        self.assertEqual([1.0, 2.0], origin.dependency_sheet.to_list(1))
        self.assertEqual([0.5, 1.0], origin.dependency_sheet.to_list(2))
        self.assertEqual(
            [(1, [0.5, 2.0], 0), (1, [1.0, 2.0], 0)],
            origin.dependency_sheet.writes,
        )
        self.assertEqual("automatic", calculation_state.recalculation_mode)
        self.assertEqual("formula_lock", calculation_state.lock_state)

    def test_dependency_proof_reads_actual_formula_and_recalculation_state(self):
        origin = FakeOriginModule()
        origin.install_dependency_book("Book")
        norm_column = origin.dependency_sheet.obj[2]
        norm_column.string_properties["formula"] = ""
        norm_column.numeric_properties["svrm"] = 2

        state = OriginDependencyProof(origin).assert_raw_to_norm_live(
            "/", "Book", "B", "C"
        )

        self.assertEqual("manual", state.recalculation_mode)
        self.assertEqual("none", state.lock_state)

    def test_dependency_proof_rejects_a_stale_norm_column(self):
        origin = FakeOriginModule()
        sheet = FakeStaleDependencySheet()
        origin.install_dependency_books_in_folders(
            (("/", "Book", "Book1", sheet),)
        )
        proof = OriginDependencyProof(origin)

        with self.assertRaisesRegex(
            RuntimeError,
            "Raw-to-Norm dependency did not update",
        ):
            proof.assert_raw_to_norm_live("/", "Book", "B", "C")

    def test_dependency_proof_uses_an_existing_formula_row(self):
        origin = FakeOriginModule()
        sheet = FakeFixedFormulaRangeDependencySheet()
        origin.install_dependency_books_in_folders(
            (("/", "Book", "Book1", sheet),)
        )

        OriginDependencyProof(origin).assert_raw_to_norm_live(
            "/", "Book", "B", "C"
        )

        self.assertEqual(2, len(sheet.to_list(1)))
        self.assertEqual(2, len(sheet.to_list(2)))

    def test_dependency_proof_preserves_column_length_with_origin_set_data_semantics(self):
        origin = FakeOriginModule()
        sheet = FakeOriginSetDataDependencySheet()
        origin.install_dependency_books_in_folders(
            (("/", "Book", "Book1", sheet),)
        )

        OriginDependencyProof(origin).assert_raw_to_norm_live(
            "/", "Book", "B", "C"
        )

        self.assertEqual([1.0, 2.0], sheet.to_list(1))
        self.assertEqual([0.5, 1.0], sheet.to_list(2))
        self.assertEqual([2, 2], [len(values) for _, values, _ in sheet.writes])

    def test_dependency_proof_preserves_unchanged_nan_blank_rows(self):
        origin = FakeOriginModule()
        sheet = FakeDependencySheet([1.0, 2.0, float("nan")])
        origin.install_dependency_books_in_folders(
            (("/", "Book", "Book1", sheet),)
        )

        OriginDependencyProof(origin).assert_raw_to_norm_live(
            "/", "Book", "B", "C"
        )

    def test_dependency_proof_rejects_a_nan_blank_that_changes(self):
        origin = FakeOriginModule()
        sheet = FakeChangedBlankDependencySheet()
        origin.install_dependency_books_in_folders(
            (("/", "Book", "Book1", sheet),)
        )

        with self.assertRaisesRegex(
            RuntimeError, "Raw-to-Norm dependency did not update"
        ):
            OriginDependencyProof(origin).assert_raw_to_norm_live(
                "/", "Book", "B", "C"
            )

    def test_dependency_proof_rejects_a_silently_ignored_restore(self):
        origin = FakeOriginModule()
        sheet = FakeIgnoredRestoreDependencySheet()
        origin.install_dependency_books_in_folders(
            (("/", "Book", "Book1", sheet),)
        )

        with self.assertRaisesRegex(RuntimeError, "did not restore"):
            OriginDependencyProof(origin).assert_raw_to_norm_live(
                "/", "Book", "B", "C"
            )

    def test_dependency_proof_targets_book_in_requested_folder_when_book_names_repeat(self):
        origin = FakeOriginModule()
        first_sheet = FakeDependencySheet()
        second_sheet = FakeDependencySheet()
        origin.install_dependency_books_in_folders(
            (
                ("F_Ex270_ExSlit2_EmSlit2_ALL_SAMPLES", "Book", "Book1", first_sheet),
                ("F_Ex300_ExSlit2_EmSlit2_ALL_SAMPLES", "Book", "Book2", second_sheet),
            )
        )
        proof = OriginDependencyProof(origin)

        proof.assert_raw_to_norm_live("F_Ex300_ExSlit2_EmSlit2_ALL_SAMPLES", "Book", "B", "C")

        self.assertEqual([1.0, 2.0], first_sheet.to_list(1))
        self.assertEqual([], first_sheet.writes)
        self.assertEqual(2, len(second_sheet.writes))
        self.assertEqual([1.0, 2.0], second_sheet.to_list(1))

    def test_dependency_proof_rejects_a_single_raw_point(self):
        origin = FakeOriginModule()
        sheet = FakeDependencySheet([5.0])
        origin.install_dependency_books_in_folders(
            (("/", "Book", "Book1", sheet),)
        )

        with self.assertRaisesRegex(RuntimeError, "No independent Raw row"):
            OriginDependencyProof(origin).assert_raw_to_norm_live(
                "/", "Book", "B", "C"
            )

    def test_dependency_proof_handles_zero_and_huge_float_normalizations(self):
        for label, raw_values in (
            ("unique maximum plus zero", [5.0, 0.0]),
            ("huge floats", [1e308, 1e308]),
        ):
            with self.subTest(label=label):
                origin = FakeOriginModule()
                sheet = FakeDependencySheet(raw_values)
                origin.install_dependency_books_in_folders(
                    (("/", "Book", "Book1", sheet),)
                )
                before = sheet.to_list(2)
                proof = OriginDependencyProof(origin)

                proof.assert_raw_to_norm_live("/", "Book", "B", "C")

                self.assertEqual(before, sheet.to_list(2))
                self.assertEqual(2, len(sheet.writes))

    def test_dependency_proof_rejects_unrepresentable_raw_without_overflow(self):
        origin = FakeOriginModule()
        sheet = FakeDependencySheet.__new__(FakeDependencySheet)
        sheet.columns = {
            1: [10**400],
            2: [1.0],
        }
        origin.install_dependency_books_in_folders(
            (("/", "Book", "Book1", sheet),)
        )

        with self.assertRaisesRegex(RuntimeError, "No finite Raw value"):
            OriginDependencyProof(origin).assert_raw_to_norm_live(
                "/",
                "Book",
                "B",
                "C",
            )

    def test_extraction_worker_factory_creates_fresh_worker_per_attempt(self):
        factory = OriginExtractionWorkerFactory(lambda: FakeOriginModule())

        first = factory.create("source-a", 1)
        second = factory.create("source-a", 2)

        self.assertIsNot(first, second)
        self.assertEqual(("source-a", 1), (first.source_id, first.attempt))
        self.assertEqual(("source-a", 2), (second.source_id, second.attempt))

    def test_extraction_worker_records_origin_ownership_around_launch_and_open(self):
        events = []
        origin = FakeOriginModule()
        origin.install_extraction_book("Book1", "MFL emission", ("Note", "Data"))
        factory = OriginExtractionWorkerFactory(
            lambda: events.append("load") or origin,
            before_origin_launch=lambda: events.append("before"),
            after_origin_launch=lambda launched_origin: events.append(
                "owned" if launched_origin is origin else "wrong-origin"
            ),
            after_project_open=lambda path: events.append(("opened", path)),
        )
        worker = factory.create("S1", 1)
        self.assertEqual(["before", "load", "owned"], events)
        copy_path = pathlib.Path("owned") / "copy.opju"

        tuple(worker.iter_inventory(copy_path, {copy_path}))

        self.assertEqual(["before", "load", "owned", ("opened", copy_path)], events)

    def test_extraction_worker_closes_launched_origin_when_ownership_recording_fails(self):
        origin = FakeOriginModule()
        factory = OriginExtractionWorkerFactory(
            lambda: origin,
            after_origin_launch=lambda launched_origin: (_ for _ in ()).throw(
                RuntimeError("identity failed")
            ),
        )

        with self.assertRaisesRegex(Exception, "identity failed"):
            factory.create("S1", 1)

        self.assertIn(("exit",), origin.events)

    def test_extraction_worker_extracts_inventory_rows_and_records_open_targets(self):
        origin = FakeOriginModule()
        origin.install_extraction_book("Book1", "MFL emission", ("Note", "Data"))
        worker = OriginExtractionWorkerFactory(lambda: origin).create("S1", 1)
        copy_path = pathlib.Path("owned") / "copy.opju"

        inventory = tuple(worker.iter_inventory(copy_path, {copy_path}))
        results = tuple(result for _book, result in worker.iter_book_results())

        self.assertEqual([copy_path], worker.open_targets)
        self.assertEqual(1, len(inventory))
        self.assertEqual(("S1", "worksheet", "/", "Book1"), inventory[0].identity)
        self.assertEqual("worksheet", inventory[0].page_type)
        self.assertEqual("MFL emission", inventory[0].display_name)
        self.assertEqual(("Note", "Data"), inventory[0].sheet_names)
        self.assertTrue(inventory[0].has_note)
        self.assertTrue(inventory[0].has_data)
        self.assertEqual("extracted", results[0].status)

    def test_extraction_worker_rejects_selected_y_without_physical_x_designation(self):
        origin = FakeOriginModule()
        origin.install_extraction_book("Book1", "MFL emission", ("Note", "Data"))
        data_layer = origin.po._page.Layers[1]
        data_layer.obj[0]._origin_type = 0
        worker = OriginExtractionWorkerFactory(lambda: origin).create("S1", 1)
        copy_path = pathlib.Path("owned") / "copy.opju"

        tuple(worker.iter_inventory(copy_path, {copy_path}))
        result = tuple(item for _book, item in worker.iter_book_results())[0]

        self.assertEqual("rejected", result.status)
        self.assertIn("preceding X-designated", result.rejection_reason or "")

    def test_extraction_worker_rejects_multiple_note_or_data_layers(self):
        for label, sheet_names, reason in (
            (
                "multiple Notes",
                ("Note", "Note", "Data"),
                "multiple Note",
            ),
            (
                "multiple Data layers",
                ("Note", "Data", "Data"),
                "multiple Data",
            ),
        ):
            with self.subTest(label=label):
                origin = FakeOriginModule()
                origin.install_extraction_book(
                    "Book1",
                    "Ambiguous",
                    sheet_names,
                )
                worker = OriginExtractionWorkerFactory(
                    lambda: origin
                ).create("S1", 1)
                copy_path = pathlib.Path("owned") / "copy.opju"

                tuple(worker.iter_inventory(copy_path, {copy_path}))
                result = tuple(
                    item
                    for _book, item in worker.iter_book_results()
                )[0]

                self.assertEqual("rejected", result.status)
                self.assertIn(reason, result.rejection_reason or "")

    def test_extraction_worker_preserves_empty_origin_long_name(self):
        origin = FakeOriginModule()
        origin.install_extraction_book("DfltEx1", "", ("Note", "Data"))
        worker = OriginExtractionWorkerFactory(lambda: origin).create("S1", 1)
        copy_path = pathlib.Path("owned") / "copy.opju"

        inventory = tuple(worker.iter_inventory(copy_path, {copy_path}))
        results = tuple(result for _book, result in worker.iter_book_results())

        self.assertEqual("", inventory[0].display_name)
        self.assertEqual("", results[0].display_name)
        self.assertEqual("DfltEx1", inventory[0].short_name)

    def test_extraction_worker_inventories_nonworksheet_book_and_records_rejection(self):
        origin = FakeOriginModule()
        origin.install_extraction_book("Book1", "MFL emission", ("Note", "Data"))
        origin.po.RootFolder._page_bases.append(
            FakePageBase("Matrix1", "Matrix data", page_type=origin.po.OPT_MATRIX)
        )
        worker = OriginExtractionWorkerFactory(lambda: origin).create("S1", 1)
        copy_path = pathlib.Path("owned") / "copy.opju"

        inventory = tuple(worker.iter_inventory(copy_path, {copy_path}))
        transactions = tuple(worker.iter_book_results())

        self.assertEqual(2, len(inventory))
        self.assertEqual(("worksheet", "matrix"), tuple(book.page_type for book in inventory))
        self.assertEqual(2, len(transactions))
        matrix_book, matrix_result = transactions[1]
        self.assertEqual(inventory[1].identity, matrix_book.identity)
        self.assertEqual("rejected", matrix_result.status)
        self.assertRegex(matrix_result.rejection_reason or "", "unsupported.*matrix")

    def test_extraction_worker_inventories_excel_workbook_as_worksheet_book(self):
        origin = FakeOriginModule()
        origin.install_extraction_book("Excel1", "Imported Excel data", ("Note", "Data"))
        origin.po.RootFolder._page_bases[0].m_bIsExcel = True
        worker = OriginExtractionWorkerFactory(lambda: origin).create("S1", 1)
        copy_path = pathlib.Path("owned") / "copy.opju"

        inventory = tuple(worker.iter_inventory(copy_path, {copy_path}))
        transactions = tuple(worker.iter_book_results())

        self.assertEqual(1, len(inventory))
        self.assertEqual("worksheet", inventory[0].page_type)
        self.assertEqual("Imported Excel data", inventory[0].display_name)
        self.assertEqual(1, len(transactions))
        self.assertEqual("extracted", transactions[0][1].status)

class FakeOriginModule:
    def __init__(self):
        self.events = []
        self.pe = FakeProjectExplorer(self)
        self._default = FakeDefaultBook(self)
        self._books = []
        self.po = FakeOutputPO(self)
        self.dependency_sheet = None

    def new(self, asksave=False):
        self.events.append(("new", asksave))

    def exit(self):
        self.events.append(("exit",))

    def find_book(self, kind):
        return self._default if kind == "w" else None

    def new_book(self, kind, lname, hidden):
        book = FakeBook(lname)
        self._books.append(book)
        self.events.append(("new_book", kind, lname, hidden))
        return book

    def save(self, path):
        self.events.append(("save", path))
        return True

    def wait(self):
        self.events.append(("wait",))

    def open(self, path, readonly, asksave):
        self.events.append(("open", path, readonly, asksave))
        return True

    def install_dependency_book(self, book_lname):
        self.dependency_sheet = FakeDependencySheet()
        self.po = FakePO(book_lname, (self.dependency_sheet,))

    def install_dependency_books_in_folders(self, entries):
        self.po = FakePOWithDependencyFolders(entries)

    def install_extraction_book(self, short_name, long_name, sheet_names):
        layers = []
        for name in sheet_names:
            if name == "Note":
                layers.append(FakeExtractionNoteLayer())
            elif name == "Data":
                layers.append(FakeExtractionDataLayer())
            else:
                layers.append(FakeLayer(name))
        self.po = FakePO(long_name, tuple(layers), short_name=short_name)

    def install_verifier_child_book(self, root_path, folder_path, short_name, long_name, worksheet):
        self.po = FakePOWithChildFolder(root_path, folder_path, short_name, long_name, worksheet)

    def WSheet(self, layer):
        return layer


class FakeDefaultBook:
    def __init__(self, op):
        self.op = op

    def destroy(self):
        self.op.events.append(("destroy_default_book",))
        self.op._default = None


class FakeProjectExplorer:
    def __init__(self, op):
        self.op = op
        self._path = "/"

    def root_folder(self):
        return FakeFolder("/")

    def cd(self, path):
        self._path = path
        self.op.events.append(("cd", path))

    def mkdir(self, name, chk=True):
        path = f"{self._path.rstrip('/')}/{name}"
        self.op.events.append(("mkdir", name, chk, path))
        return path


class FakeOutputPO:
    def __init__(self, op):
        self.RootFolder = FakeOutputRootFolder(op)


class FakeOutputRootFolder:
    def __init__(self, op):
        self.Folders = [FakeOutputFolder(op, "Folder1")]


class FakeOutputFolder:
    def __init__(self, op, name):
        self.op = op
        self._name = name
        self.Folders = []

    def GetName(self):
        return self._name

    def PageBases(self):
        return []

    def SetName(self, name):
        self.op.events.append(("rename_default_folder", self._name, name))
        self._name = name

    def GetPEPath(self):
        return f"/{self._name}/"


class FakeFolder:
    def __init__(self, path):
        self.path = path


class FakeBook:
    def __init__(self, lname):
        self.lname = lname
        self.sheets = [FakeSheet(self)]

    def __getitem__(self, index):
        return self.sheets[index]


class FakeFormulaColumn:
    def __init__(self):
        self.string_properties = {}
        self.numeric_properties = {}

    def GetStrProp(self, prop):
        return self.string_properties.get(prop, "")

    def GetNumProp(self, prop):
        return self.numeric_properties.get(prop, 0)

    def SetNumProp(self, prop, value):
        self.numeric_properties[prop] = value


class FakeSheet:
    def __init__(self, book):
        self.book = book
        self.name = None
        self.lname = None
        self.cols = 0
        self.values = {}
        self.labels = {}
        self.axes = {}
        self.formulas = {}
        self.events = []
        self.obj = [FakeFormulaColumn() for _ in range(26)]

    def from_list(self, index, values, lname, comments, axis):
        self.values[index] = values
        self.labels[(index, "L")] = lname
        self.labels[(index, "C")] = comments
        self.axes[index] = axis

    def set_label(self, index, value, label):
        self.labels[(index, label)] = value

    def get_label(self, index, label):
        return self.labels.get((index, label))

    def set_formula(self, index, formula):
        self.formulas[index] = formula

    def lt_exec(self, labtalk):
        self.events.append(("lt_exec", labtalk))


class FakePO:
    OPT_WORKSHEET = 1
    OPT_MATRIX = 2

    def __init__(self, book_lname, layers, *, short_name="Book1"):
        self.RootFolder = FakeRootFolder(short_name, book_lname)
        self._page = FakePage(layers)

    def Pages(self, name):
        return self._page


class FakeRootFolder:
    def __init__(self, short_name, book_lname):
        self.Folders = []
        self._page_bases = [FakePageBase(short_name, book_lname)]

    def PageBases(self):
        return self._page_bases


class FakePageBase:
    def __init__(self, short_name, lname, page_type=FakePO.OPT_WORKSHEET):
        self._short_name = short_name
        self._lname = lname
        self._page_type = page_type

    def GetType(self):
        return self._page_type

    def GetLongName(self):
        return self._lname

    def GetName(self):
        return self._short_name


class FakePage:
    def __init__(self, layers):
        self.Layers = list(layers)


class FakeLayer:
    def __init__(self, name):
        self._name = name

    def GetLongName(self):
        return self._name

    def GetName(self):
        return self._name


class FakeExtractionNoteLayer(FakeLayer):
    def __init__(self):
        super().__init__("Note")
        self.text = "[EXP_FD_FILE]\nAcquisition Type = Spectral Acquisition[Emission]"

    def GetText(self):
        return self.text


class FakeExtractionDataLayer(FakeLayer):
    def __init__(self):
        super().__init__("Data")
        self.cols = 4
        self.obj = [
            FakeExtractionColumn("A", "Wavelength", origin_type=3),
            FakeExtractionColumn("S1c", "S1c", origin_type=0),
            FakeExtractionColumn("B", "S1 X", origin_type=3),
            FakeExtractionColumn("S1", "S1", origin_type=0),
        ]
        self.values = [[300, 301], [1, 2], [300, 301], [10, 20]]

    def to_list(self, index):
        return list(self.values[index])

    def get_label(self, index, label):
        if label == "L":
            return self.obj[index].long_name
        return None


class FakeExtractionColumn:
    def __init__(self, short_name, long_name, *, origin_type):
        self.short_name = short_name
        self.long_name = long_name
        self._origin_type = origin_type

    def GetName(self):
        return self.short_name

    def GetLongName(self):
        return self.long_name

    def GetType(self):
        return self._origin_type


class FakePOWithChildFolder:
    OPT_WORKSHEET = 1

    def __init__(self, root_path, folder_path, short_name, long_name, worksheet):
        self.RootFolder = FakeFolderNode(root_path, (), (FakeFolderNode(folder_path, (FakePageBase(short_name, long_name),), ()),))
        self._page = FakePage((worksheet,))

    def Pages(self, name):
        return self._page


class FakePOWithDependencyFolders:
    OPT_WORKSHEET = 1

    def __init__(self, entries):
        folders = []
        self._pages = {}
        for folder_path, book_lname, short_name, worksheet in entries:
            folders.append(FakeFolderNode(f"/{folder_path}/", (FakePageBase(short_name, book_lname),), ()))
            self._pages[short_name] = FakePage((worksheet,))
        self.RootFolder = FakeFolderNode("/", (), tuple(folders))

    def Pages(self, name):
        return self._pages[name]


class FakeFolderNode:
    def __init__(self, pe_path, page_bases, folders):
        self._pe_path = pe_path
        self._page_bases = list(page_bases)
        self.Folders = list(folders)

    def GetPEPath(self):
        return self._pe_path

    def PageBases(self):
        return self._page_bases


class FakeReadbackSheet:
    def __init__(self, columns=None, labels=None, method_labels=None, values=None):
        self.obj = list(columns or (FakeReadbackColumn(),))
        self._labels = tuple(labels or ("A",) * len(self.obj))
        self._method_labels = tuple(method_labels or (None,) * len(self.obj))
        self._values = tuple(values or ([1],) * len(self.obj))
        self.cols = len(self.obj)

    def to_list(self, index):
        return list(self._values[index])

    def get_label(self, index, label):
        if label == "C":
            return "Em"
        if label == "G":
            return self._labels[index]
        if label == "Method":
            return self._method_labels[index]
        return None


class FakeReadbackColumn:
    def __init__(self, origin_type=0):
        self._origin_type = origin_type

    def GetType(self):
        return self._origin_type

    def GetStrProp(self, prop):
        return ""


class FakeDependencySheet:
    def __init__(self, raw_values=None):
        raw_values = list(raw_values or (1.0, 2.0))
        maximum = max(raw_values)
        self.columns = {
            1: raw_values,
            2: [value / maximum for value in raw_values],
        }
        self.obj = [FakeFormulaColumn() for _ in range(26)]
        self.obj[2].string_properties["formula"] = "col(B)/max(col(B))"
        self.obj[2].numeric_properties["svrm"] = 1
        self.writes = []

    def to_list(self, index):
        return list(self.columns[index])

    def from_list(self, index, values, start=0):
        self.writes.append((index, list(values), start))
        column = list(self.columns[index])
        required = start + len(values)
        if required > len(column):
            column.extend([None] * (required - len(column)))
        column[start:required] = values
        self.columns[index] = column
        if index == 1:
            maximum = max(value for value in column if value is not None)
            self.columns[2] = [None if value is None else value / maximum for value in column]


class FakeStaleDependencySheet(FakeDependencySheet):
    def from_list(self, index, values, start=0):
        self.writes.append((index, list(values), start))
        column = list(self.columns[index])
        column[start:start + len(values)] = values
        self.columns[index] = column


class FakeFixedFormulaRangeDependencySheet(FakeDependencySheet):
    def from_list(self, index, values, start=0):
        self.writes.append((index, list(values), start))
        column = list(self.columns[index])
        required = start + len(values)
        if required > len(column):
            column.extend([None] * (required - len(column)))
        column[start:required] = values
        self.columns[index] = column
        if index == 1:
            formula_rows = len(self.columns[2])
            maximum = max(value for value in column if value is not None)
            self.columns[2] = [
                None if value is None else value / maximum
                for value in column[:formula_rows]
            ]


class FakeOriginSetDataDependencySheet(FakeDependencySheet):
    def from_list(self, index, values, start=0):
        self.writes.append((index, list(values), start))
        column = self.columns[index][:start] + list(values)
        self.columns[index] = column
        if index == 1:
            maximum = max(value for value in column if value is not None)
            self.columns[2] = [
                None if value is None else value / maximum
                for value in column
            ]


class FakeChangedBlankDependencySheet(FakeDependencySheet):
    def __init__(self):
        super().__init__([1.0, 2.0, float("nan")])

    def from_list(self, index, values, start=0):
        super().from_list(index, values, start)
        if index == 1 and len(self.writes) == 1:
            self.columns[2][2] = 0.0


class FakeIgnoredRestoreDependencySheet(FakeDependencySheet):
    def from_list(self, index, values, start=0):
        if self.writes:
            self.writes.append((index, list(values), start))
            return
        super().from_list(index, values, start)
if __name__ == "__main__":
    unittest.main()
