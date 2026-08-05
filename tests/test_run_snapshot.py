from decimal import Decimal
from dataclasses import replace
from contextlib import closing
import gc
import hashlib
import json
import math
import os
import pathlib
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from spectrum_organizer.origin.extract_worker import (
    ExtractionOrchestrator,
    ExtractionSource,
    InfrastructureExtractionError,
    InventoryBook,
    TerminalBookResult,
    WorkerPreflightError,
    WorkerShutdownUnconfirmedError,
    validate_worker_open_target,
)
from spectrum_organizer.core.selection import CandidateConversionError, convert_extracted_results
from spectrum_organizer.store.run_snapshot import (
    ReconciliationError,
    RunSnapshot,
    SnapshotError,
    load_book_results_read_only,
    load_book_payload_read_only,
    snapshot_approval_sha256,
    validate_reconciled_sources,
)
from spectrum_organizer.store import run_snapshot as run_snapshot_module


class WorkspaceTempDir:
    def __init__(self):
        self.path = pathlib.Path(tempfile.mkdtemp(prefix="spectrum-organizer-run-snapshot-"))

    def __enter__(self):
        return self.path

    def __exit__(self, exc_type, exc, tb):
        last_error = None
        for _ in range(4):
            try:
                shutil.rmtree(self.path)
                last_error = None
                break
            except FileNotFoundError:
                last_error = None
                break
            except OSError as cleanup_error:
                last_error = cleanup_error
                gc.collect()
                time.sleep(0.05)
        if last_error is not None or self.path.exists():
            raise AssertionError(f"run-snapshot test cleanup failed: {self.path}") from last_error


class FakeWorker:
    active = 0
    max_active = 0

    def __init__(self, source_id, inventory, results, fail=False, events=None):
        self.source_id = source_id
        self.inventory = inventory
        self.results = results
        self.fail = fail
        self.events = events if events is not None else []

    def close(self):
        self.events.append(("close", self.source_id))

    def iter_inventory(self, source_path, allowlist):
        original_dir = source_path.parents[2] / "originals" / self.source_id
        validate_worker_open_target(
            source_path,
            allowlist,
            role="extraction",
            protected_paths=tuple(original_dir.iterdir()),
            allowed_children=(source_path.parent,),
        )
        FakeWorker.active += 1
        FakeWorker.max_active = max(FakeWorker.max_active, FakeWorker.active)
        self.events.append(("start", self.source_id))
        if self.fail:
            FakeWorker.active -= 1
            self.events.append(("fail", self.source_id))
            raise InfrastructureExtractionError("worker crashed")
        yield from self.inventory

    def iter_book_results(self):
        inventory_by_identity = {book.identity: book for book in self.inventory}
        for result in self.results:
            yield inventory_by_identity[result.identity], result
        FakeWorker.active -= 1
        self.events.append(("end", self.source_id))


class FakeWorkerFactory:
    def __init__(self, plans):
        self.plans = {key: list(value) for key, value in plans.items()}
        self.created = []
        self.events = []

    def create(self, source_id, attempt):
        self.created.append((source_id, attempt))
        inventory, results, fail = self.plans[source_id].pop(0)
        return FakeWorker(source_id, inventory, results, fail=fail, events=self.events)


class CloseFailingInfrastructureWorker(FakeWorker):
    def close(self):
        self.events.append(("close", self.source_id))
        raise RuntimeError("close failed")


class CloseFailingInfrastructureWorkerFactory:
    def __init__(self):
        self.events = []

    def create(self, source_id, attempt):
        return CloseFailingInfrastructureWorker(source_id, (), (), fail=True, events=self.events)


class CreateFailingWorkerFactory:
    def create(self, source_id, attempt):
        raise InfrastructureExtractionError("worker creation failed")


class StreamingWorker:
    def __init__(self, source_id, transactions, events, snapshot, fail_after_first=False, fail_close=False):
        self.source_id = source_id
        self.transactions = tuple(transactions)
        self.events = events
        self.snapshot = snapshot
        self.fail_after_first = fail_after_first
        self.fail_close = fail_close

    def close(self):
        self.events.append(("close", self.source_id))
        if isinstance(self.fail_close, Exception):
            raise self.fail_close
        if self.fail_close:
            raise RuntimeError("close failed")

    def iter_inventory(self, source_path, allowlist):
        original_dir = source_path.parents[2] / "originals" / self.source_id
        validate_worker_open_target(
            source_path,
            allowlist,
            role="extraction",
            protected_paths=tuple(original_dir.iterdir()),
            allowed_children=(source_path.parent,),
        )
        self.events.append(("stream-start", self.source_id))
        for book, _result in self.transactions:
            yield book

    def iter_book_results(self):
        for index, transaction in enumerate(self.transactions, start=1):
            yield transaction
            self.events.append(("after-yield", self.source_id, index, self.snapshot.result_count(self.source_id)))
            if self.fail_after_first and index == 1:
                raise ValueError("stream interrupted")
        self.events.append(("stream-end", self.source_id))


class StreamingWorkerFactory:
    def __init__(self, transactions, snapshot, fail_after_first=False, fail_close=False):
        self.transactions = tuple(transactions)
        self.snapshot = snapshot
        self.fail_after_first = fail_after_first
        self.fail_close = fail_close
        self.created = []
        self.events = []

    def create(self, source_id, attempt):
        self.created.append((source_id, attempt))
        return StreamingWorker(source_id, self.transactions, self.events, self.snapshot, self.fail_after_first, self.fail_close)


class RaisingWorker:
    def __init__(self, events):
        self.source_id = "S1"
        self.events = events

    def iter_inventory(self, source_path, allowlist):
        raise ValueError("parse bug")

    def iter_book_results(self):
        return iter(())

    def close(self):
        self.events.append(("close", self.source_id))


class RaisingWorkerFactory:
    def __init__(self):
        self.events = []

    def create(self, source_id, attempt):
        return RaisingWorker(self.events)


class FakeSourceManager:
    def __init__(self, verify_copy_error=None, verify_original_error=None, discard_failed_copy_error=None, refresh_copy_path=None):
        self.calls = []
        self.verify_copy_error = verify_copy_error
        self.verify_original_error = verify_original_error
        self.discard_failed_copy_error = discard_failed_copy_error
        self.refresh_copy_path = refresh_copy_path

    def verify_original(self, source_id):
        self.calls.append(("verify_original", source_id))
        if self.verify_original_error is not None:
            raise self.verify_original_error

    def verify_copy(self, source_id):
        self.calls.append(("verify_copy", source_id))
        if self.verify_copy_error is not None:
            raise self.verify_copy_error

    def discard_failed_copy(self, source_id):
        self.calls.append(("discard_failed_copy", source_id))
        if self.discard_failed_copy_error is not None:
            raise self.discard_failed_copy_error

    def refresh_copy(self, source_id):
        self.calls.append(("refresh_copy", source_id))
        return self.refresh_copy_path


class RetryShutdownWaiter:
    def __init__(self):
        self.calls = []

    def __call__(self, source_id, attempt):
        self.calls.append((source_id, attempt))


class CleanupFailingSnapshot:
    def __init__(self, delegate):
        self.delegate = delegate
        self.discard_calls = 0

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    def discard_source_partition(self, source_id):
        self.discard_calls += 1
        if self.discard_calls >= 2:
            raise RuntimeError("cleanup masked verifier")
        return self.delegate.discard_source_partition(source_id)


class RunSnapshotTests(unittest.TestCase):
    def test_loader_and_converter_abort_when_snapshot_contains_unexpected_source(self):
        with WorkspaceTempDir() as root:
            path = root / "snapshot.sqlite3"
            snapshot = RunSnapshot(path)
            for source_id in ("S1", "S2"):
                book = _book(source_id, "Folder", "Book1", "Display")
                snapshot.add_source(source_id, root / f"{source_id}.opj", f"hash-{source_id}")
                snapshot.record_book_transaction(source_id, book, _result(book))

            with self.assertRaises(CandidateConversionError):
                convert_extracted_results(
                    load_book_results_read_only(
                        path,
                        expected_snapshot_sha256=snapshot_approval_sha256(path),
                    ),
                    source_filenames={"S1": "selected.opj"},
                    expected_source_ids=("S1",),
                )

    def test_read_only_payload_loader_does_not_change_snapshot_bytes_mtime_or_schema(self):
        with WorkspaceTempDir() as root:
            path = root / "snapshot.sqlite3"
            snapshot = RunSnapshot(path)
            book = _book("S1", "Folder", "Book1", "Display")
            snapshot.add_source("S1", root / "copy.opj", "hash")
            snapshot.record_book_transaction("S1", book, _result(book))
            before_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            approved_sha256 = snapshot_approval_sha256(path)
            before_mtime = path.stat().st_mtime_ns
            with closing(
                sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
            ) as connection:
                before_schema = tuple(connection.execute("select type, name, sql from sqlite_master order by type, name"))

            results = load_book_results_read_only(
                path,
                expected_snapshot_sha256=approved_sha256,
            )

            loaded = tuple(results)
            self.assertEqual(("Book1",), tuple(result.short_name for result in loaded))
            self.assertEqual((), loaded[0].selected_x_values)
            self.assertEqual((), loaded[0].selected_y_values)
            self.assertEqual(path.resolve(), loaded[0].payload_snapshot_path)
            self.assertEqual(
                ((300.0, 301.0), (10.0, 12.0)),
                load_book_payload_read_only(
                    path,
                    source_id="S1",
                    page_type="worksheet",
                    folder_path="Folder",
                    short_name="Book1",
                    expected_payload_checksum=loaded[0].payload_checksum,
                ),
            )
            self.assertEqual(before_hash, hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertEqual(before_mtime, path.stat().st_mtime_ns)
            with closing(
                sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
            ) as connection:
                after_schema = tuple(connection.execute("select type, name, sql from sqlite_master order by type, name"))
            self.assertEqual(before_schema, after_schema)

    def test_batch_payload_loader_binds_reviewed_rows_to_approved_snapshot(self):
        from spectrum_organizer.store.run_snapshot import (
            load_book_payloads_read_only,
        )

        with WorkspaceTempDir() as root:
            path = root / "snapshot.sqlite3"
            snapshot = RunSnapshot(path)
            book = _book("S1", "Folder", "Book1", "Display")
            snapshot.add_source("S1", root / "copy.opj", "hash")
            snapshot.record_book_transaction("S1", book, _result(book))
            approved_sha256 = snapshot_approval_sha256(path)
            reviewed = tuple(
                load_book_results_read_only(
                    path,
                    expected_snapshot_sha256=approved_sha256,
                )
            )[0]

            payloads = load_book_payloads_read_only(
                path,
                expected_snapshot_sha256=approved_sha256,
                requests=(
                    (
                        reviewed.source_id,
                        reviewed.page_type,
                        reviewed.folder_path,
                        reviewed.short_name,
                        reviewed.payload_checksum,
                    ),
                ),
            )

        self.assertEqual(
            (((300.0, 301.0), (10.0, 12.0)),),
            payloads,
        )

    def test_metadata_loader_rejects_snapshot_replaced_after_extraction_approval(self):
        with WorkspaceTempDir() as root:
            path = root / "snapshot.sqlite3"
            approved = RunSnapshot(path)
            approved_book = _book("S1", "Folder", "Approved", "Approved")
            approved.add_source("S1", root / "copy.opj", "hash")
            approved.record_book_transaction("S1", approved_book, _result(approved_book))
            approved_sha256 = snapshot_approval_sha256(path)

            replacement_path = root / "replacement.sqlite3"
            replacement = RunSnapshot(replacement_path)
            replacement_book = _book("S1", "Folder", "Replacement", "Replacement")
            replacement.add_source("S1", root / "copy.opj", "hash")
            replacement.record_book_transaction("S1", replacement_book, _result(replacement_book))
            os.replace(replacement_path, path)

            with self.assertRaisesRegex(ReconciliationError, "approved snapshot"):
                tuple(
                    load_book_results_read_only(
                        path,
                        expected_snapshot_sha256=approved_sha256,
                        source_ids=("S1",),
                    )
                )

    def test_metadata_only_query_does_not_select_large_xy_json_columns(self):
        query = run_snapshot_module._ALL_BOOK_RESULTS_METADATA_QUERY.casefold()

        self.assertNotIn("selected_x_values_json", query)
        self.assertNotIn("selected_y_values_json", query)

    def test_tied_maximum_x_values_reconcile_and_round_trip_as_candidate_metadata(self):
        with WorkspaceTempDir() as root:
            path = root / "snapshot.sqlite3"
            snapshot = RunSnapshot(path)
            book = _book("S1", "Folder", "Book1", "Display")
            result = replace(
                _result(book),
                selected_y_values=(12.0, 12.0),
                max_planned_y=12.0,
                max_planned_y_x=(300.0, 301.0),
            )
            snapshot.add_source("S1", root / "copy.opj", "hash")
            snapshot.record_book_transaction("S1", book, result)

            snapshot.reconcile_source("S1")
            loaded = tuple(
                load_book_results_read_only(
                    path,
                    expected_snapshot_sha256=snapshot_approval_sha256(path),
                )
            )

            self.assertEqual([300.0, 301.0], loaded[0].max_planned_y_x)

    def test_terminal_payload_validation_streams_rows_without_fetchall(self):
        class Cursor:
            def __iter__(self):
                return iter(())

            def fetchall(self):
                raise AssertionError("terminal payload validation must stream rows")

        class Connection:
            def execute(self, query, parameters):
                del query, parameters
                return Cursor()

        run_snapshot_module._validate_terminal_payloads(Connection(), "S0001")

    def test_worker_preflight_rejects_wrong_role_non_allowlisted_and_missing_metadata(self):
        with WorkspaceTempDir() as root:
            owned = root / "owned"
            owned.mkdir()
            copy = owned / "copy.opju"
            copy.write_bytes(b"copy")
            original = root / "original.opju"
            original.write_bytes(b"original")
            with self.assertRaises(WorkerPreflightError):
                validate_worker_open_target(
                    copy,
                    {copy},
                    role="visible_detector",
                    protected_paths=(original,),
                    allowed_children=(copy.parent,),
                )
            with self.assertRaises(WorkerPreflightError):
                validate_worker_open_target(
                    original,
                    {copy},
                    role="extraction",
                    protected_paths=(original,),
                    allowed_children=(copy.parent,),
                )
            with self.assertRaises(WorkerPreflightError):
                validate_worker_open_target(copy, {copy}, role="extraction")

            validate_worker_open_target(
                copy,
                {copy},
                role="extraction",
                protected_paths=(original,),
                allowed_children=(copy.parent,),
            )

    def test_snapshot_records_inventory_result_and_reconciles(self):
        with WorkspaceTempDir() as root:
            snapshot = RunSnapshot(root / "run.sqlite3")
            snapshot.add_source("S1", pathlib.Path("copy-a.opju"), "abc")
            book = _book("S1", "Root", "Book1", "Display")
            result = _result(book)
            snapshot.record_inventory("S1", [book])
            snapshot.record_book_result(result, book)
            snapshot.reconcile_source("S1")
            self.assertEqual(1, snapshot.inventory_count("S1"))
            self.assertEqual(1, snapshot.result_count("S1"))

    def test_reconciliation_rejects_display_name_or_page_order_divergence(self):
        with WorkspaceTempDir() as root:
            snapshot = RunSnapshot(root / "run.sqlite3")
            snapshot.add_source("S1", pathlib.Path("copy-a.opju"), "abc")
            book = _book("S1", "Root", "Book1", "Display")

            for result in (
                replace(_result(book), display_name="Renamed"),
                replace(_result(book), page_order=2),
            ):
                snapshot.discard_source_partition("S1")
                snapshot.record_inventory_book("S1", book)
                snapshot.record_book_result(result, book)

                with self.assertRaises(ReconciliationError):
                    snapshot.reconcile_source("S1")

    def test_reconciliation_rejects_pass_two_sheet_metadata_divergence(self):
        with WorkspaceTempDir() as root:
            snapshot = RunSnapshot(root / "run.sqlite3")
            snapshot.add_source("S1", pathlib.Path("copy-a.opju"), "abc")
            pass_one = _book("S1", "Root", "Book1", "Display", sheets=("Note", "Data_A"))
            pass_two = replace(pass_one, sheet_names=("Note", "Data_B"))
            snapshot.record_inventory_book("S1", pass_one)
            snapshot.record_book_result(_result(pass_two), pass_two_book=pass_two)

            with self.assertRaisesRegex(ReconciliationError, "metadata"):
                snapshot.reconcile_source("S1")

    def test_selected_row_counts_round_trip_and_must_match_payload_lengths(self):
        with WorkspaceTempDir() as root:
            snapshot = RunSnapshot(root / "run.sqlite3")
            snapshot.add_source("S1", pathlib.Path("copy-a.opju"), "abc")
            book = _book("S1", "Root", "Book1", "Display")
            result = _result(book)
            if hasattr(result, "selected_x_row_count"):
                result = replace(result, selected_x_row_count=2, selected_y_row_count=2)
            snapshot.record_inventory_book("S1", book)
            snapshot.record_book_result(result, book)
            snapshot.reconcile_source("S1")

            persisted = snapshot.book_results("S1")[0]
            self.assertEqual((2, 2), (
                getattr(persisted, "selected_x_row_count", None),
                getattr(persisted, "selected_y_row_count", None),
            ))

            snapshot.discard_source_partition("S1")
            snapshot.record_inventory_book("S1", book)
            snapshot.record_book_result(replace(result, selected_x_row_count=1), book)
            with self.assertRaises(ReconciliationError):
                snapshot.reconcile_source("S1")

    def test_reconciliation_rejects_unequal_paired_x_y_lengths(self):
        with WorkspaceTempDir() as root:
            snapshot = RunSnapshot(root / "run.sqlite3")
            snapshot.add_source("S1", pathlib.Path("copy-a.opju"), "abc")
            book = _book("S1", "Root", "Book1", "Display")
            result = replace(
                _result(book),
                selected_y_values=(10.0, 12.0, 14.0),
                selected_y_row_count=3,
            )
            snapshot.record_inventory_book("S1", book)
            snapshot.record_book_result(result, book)

            with self.assertRaisesRegex(ReconciliationError, "row counts"):
                snapshot.reconcile_source("S1")

    def test_reconciliation_rejects_persisted_payload_checksum_mismatch(self):
        with WorkspaceTempDir() as root:
            path = root / "run.sqlite3"
            snapshot = RunSnapshot(path)
            snapshot.add_source("S1", pathlib.Path("copy-a.opju"), "abc")
            book = _book("S1", "Root", "Book1", "Display")
            snapshot.record_inventory_book("S1", book)
            snapshot.record_book_result(_result(book), book)
            with closing(sqlite3.connect(path)) as connection:
                connection.execute(
                    "update book_results set selected_y_values_json = '[999, 1000]' where source_id = 'S1'"
                )
                connection.commit()

            with self.assertRaisesRegex(ReconciliationError, "checksum"):
                snapshot.reconcile_source("S1")

    def test_reconciliation_rejects_payload_and_checksum_swapped_between_books(self):
        with WorkspaceTempDir() as root:
            path = root / "run.sqlite3"
            snapshot = RunSnapshot(path)
            snapshot.add_source("S1", pathlib.Path("copy-a.opju"), "abc")
            first = _book("S1", "Root", "Book1", "First")
            second = _book("S1", "Root", "Book2", "Second")
            snapshot.record_book_transaction("S1", first, _result(first))
            snapshot.record_book_transaction(
                "S1",
                second,
                replace(
                    _result(second),
                    selected_y_values=(20.0, 22.0),
                    max_planned_y=22.0,
                    data_checksum="second-checksum",
                ),
            )
            payload_columns = """
                status, rejection_reason, data_checksum, note_text, data_sheet_name, spectrum_class,
                available_columns_json, selected_y_column, paired_x_column,
                selected_x_values_json, selected_y_values_json, s1_x_values_json, s1_values_json,
                selected_x_row_count,
                selected_y_row_count, max_planned_y_json, max_planned_y_x_json,
                s1_max_for_limit_json, s1_max_for_limit_x_json, s1_limit_status, payload_checksum
            """
            with closing(sqlite3.connect(path)) as connection:
                connection.execute(
                    f"create temp table saved_payloads as "
                    f"select short_name, {payload_columns} from book_results where source_id = 'S1'"
                )
                for target, source in (("Book1", "Book2"), ("Book2", "Book1")):
                    connection.execute(
                        f"update book_results set ({payload_columns}) = "
                        f"(select {payload_columns} from saved_payloads where short_name = ?) "
                        "where source_id = 'S1' and short_name = ?",
                        (source, target),
                    )
                connection.commit()

            with self.assertRaisesRegex(ReconciliationError, "checksum"):
                snapshot.reconcile_source("S1")

    def test_lazy_payload_read_rechecks_checksum_after_reconciliation(self):
        with WorkspaceTempDir() as root:
            path = root / "run.sqlite3"
            snapshot = RunSnapshot(path)
            snapshot.add_source("S1", pathlib.Path("copy-a.opju"), "abc")
            book = _book("S1", "Root", "Book1", "Display")
            snapshot.record_inventory_book("S1", book)
            snapshot.record_book_result(_result(book), book)
            snapshot.reconcile_source("S1")
            reviewed_checksum = snapshot.book_results("S1")[0].payload_checksum
            with closing(sqlite3.connect(path)) as connection:
                connection.execute(
                    "update book_results set selected_x_values_json = '[999, 1000]' where source_id = 'S1'"
                )
                connection.commit()

            with self.assertRaisesRegex(ReconciliationError, "checksum"):
                load_book_payload_read_only(
                    path,
                    source_id="S1",
                    page_type="worksheet",
                    folder_path="Root",
                    short_name="Book1",
                    expected_payload_checksum=reviewed_checksum,
                )

    def test_reconciliation_rejects_tampered_raw_s1_payload_checksum(self):
        with WorkspaceTempDir() as root:
            path = root / "run.sqlite3"
            snapshot = RunSnapshot(path)
            snapshot.add_source("S1", pathlib.Path("copy-a.opju"), "abc")
            book = _book("S1", "Root", "Book1", "Display")
            snapshot.record_book_transaction("S1", book, _result(book))
            with closing(sqlite3.connect(path)) as connection:
                connection.execute(
                    "update book_results set s1_values_json = '[100, 999]' where source_id = 'S1'"
                )
                connection.commit()

            with self.assertRaisesRegex(ReconciliationError, "checksum"):
                snapshot.reconcile_source("S1")

    def test_lazy_payload_read_is_bound_to_reviewed_payload_version(self):
        with WorkspaceTempDir() as root:
            path = root / "run.sqlite3"
            snapshot = RunSnapshot(path)
            snapshot.add_source("S1", pathlib.Path("copy-a.opju"), "abc")
            book = _book("S1", "Root", "Book1", "Display")
            snapshot.record_inventory_book("S1", book)
            snapshot.record_book_result(_result(book), book)
            snapshot.reconcile_source("S1")
            reviewed = tuple(
                load_book_results_read_only(
                    path,
                    expected_snapshot_sha256=snapshot_approval_sha256(path),
                    source_ids=("S1",),
                )
            )[0]

            with closing(sqlite3.connect(path)) as connection:
                row = list(connection.execute(
                    """
                    select status, rejection_reason, data_checksum, note_text, data_sheet_name, spectrum_class,
                           available_columns_json, selected_y_column, paired_x_column,
                           selected_x_values_json, selected_y_values_json, s1_x_values_json,
                           s1_values_json, selected_x_row_count,
                           selected_y_row_count, max_planned_y_json, max_planned_y_x_json,
                           s1_max_for_limit_json, s1_max_for_limit_x_json, s1_limit_status,
                           column_metadata_json
                    from book_results
                    where source_id = 'S1' and page_type = 'worksheet'
                      and folder_path = 'Root' and short_name = 'Book1'
                    """
                ).fetchone())
                row[9] = "[900,901]"
                row[10] = "[100,200]"
                row[15] = "200"
                row[16] = "901"
                replacement_checksum = run_snapshot_module._payload_checksum(
                    "S1", "worksheet", "Root", "Book1", *row
                )
                connection.execute(
                    """
                    update book_results
                    set selected_x_values_json = ?, selected_y_values_json = ?,
                        max_planned_y_json = ?, max_planned_y_x_json = ?, payload_checksum = ?
                    where source_id = 'S1' and page_type = 'worksheet'
                      and folder_path = 'Root' and short_name = 'Book1'
                    """,
                    (row[9], row[10], row[15], row[16], replacement_checksum),
                )
                connection.commit()

            with self.assertRaisesRegex(ReconciliationError, "reviewed payload version"):
                load_book_payload_read_only(
                    path,
                    source_id="S1",
                    page_type="worksheet",
                    folder_path="Root",
                    short_name="Book1",
                    expected_payload_checksum=reviewed.payload_checksum,
                )

    def test_post_extraction_validation_rejects_tampered_candidate_metadata(self):
        with WorkspaceTempDir() as root:
            path = root / "run.sqlite3"
            snapshot = RunSnapshot(path)
            snapshot.add_source("S1", pathlib.Path("copy-a.opju"), "abc")
            book = _book("S1", "Root", "Book1", "Display")
            snapshot.record_inventory_book("S1", book)
            snapshot.record_book_result(_result(book), book)
            snapshot.reconcile_source("S1")
            with closing(sqlite3.connect(path)) as connection:
                connection.execute(
                    "update book_results set note_text = note_text || ' tampered' where source_id = 'S1'"
                )
                connection.commit()

            with self.assertRaisesRegex(ReconciliationError, "checksum"):
                validate_reconciled_sources(path, ("S1",))

    def test_validated_metadata_stream_uses_one_read_transaction(self):
        with WorkspaceTempDir() as root:
            path = root / "run.sqlite3"
            snapshot = RunSnapshot(path)
            snapshot.add_source("S1", pathlib.Path("copy-a.opju"), "abc")
            book = _book("S1", "Root", "Book1", "Display")
            original = _result(book)
            snapshot.record_inventory_book("S1", book)
            snapshot.record_book_result(original, book)
            snapshot.reconcile_source("S1")
            with closing(sqlite3.connect(path)) as connection:
                connection.execute("pragma journal_mode=wal")

            real_validate = run_snapshot_module._validate_source_reconciliation
            mutated = False

            def validate_then_mutate(connection, source_id, **kwargs):
                nonlocal mutated
                real_validate(connection, source_id, **kwargs)
                if not mutated:
                    mutated = True
                    with closing(sqlite3.connect(path)) as writer:
                        writer.execute(
                            "update book_results set note_text = note_text || ' changed' where source_id = ?",
                            (source_id,),
                        )
                        writer.commit()

            approved_sha256 = snapshot_approval_sha256(path)
            with (
                mock.patch.object(
                    run_snapshot_module,
                    "_validate_source_reconciliation",
                    side_effect=validate_then_mutate,
                ),
                self.assertRaisesRegex(ReconciliationError, "changed during approved snapshot"),
            ):
                tuple(
                    load_book_results_read_only(
                        path,
                        expected_snapshot_sha256=approved_sha256,
                        source_ids=("S1",),
                    )
                )

            self.assertTrue(mutated)

    def test_reconciliation_rejects_tampered_rejected_payload(self):
        with WorkspaceTempDir() as root:
            path = root / "run.sqlite3"
            snapshot = RunSnapshot(path)
            snapshot.add_source("S1", pathlib.Path("copy-a.opju"), "abc")
            rejected_book = _book("S1", "Root", "Book1", "Rejected")
            valid_book = _book("S1", "Root", "Book2", "Valid")
            snapshot.record_inventory_book("S1", rejected_book)
            snapshot.record_book_result(_result(rejected_book, status="rejected"), rejected_book)
            snapshot.record_inventory_book("S1", valid_book)
            snapshot.record_book_result(_result(valid_book), valid_book)
            with closing(sqlite3.connect(path)) as connection:
                connection.execute(
                    "update book_results set rejection_reason = 'tampered' where short_name = 'Book1'"
                )
                connection.commit()

            with self.assertRaisesRegex(ReconciliationError, "checksum"):
                snapshot.reconcile_source("S1")

    def test_missing_s1_payload_requires_matching_approved_snapshot_option(self):
        with WorkspaceTempDir() as root:
            path = root / "run.sqlite3"
            snapshot = RunSnapshot(path)
            snapshot.add_source("S1", pathlib.Path("copy-a.opju"), "abc")
            book = _book("S1", "Root", "Book1", "Valid without S1")
            result = replace(
                _result(book),
                available_columns=("X", "S1c"),
                column_metadata=(
                    ("A", "X", "X"),
                    ("B", "S1c", "Y"),
                ),
                s1_x_values=None,
                s1_values=None,
                s1_max_for_limit=None,
                s1_max_for_limit_x=None,
                s1_limit_status="missing_allowed",
            )
            snapshot.record_inventory_book("S1", book)
            snapshot.record_book_result(result, book)

            with self.assertRaisesRegex(ReconciliationError, "Missing S1 was not approved"):
                validate_reconciled_sources(
                    path,
                    ("S1",),
                    s1_limit=100,
                    steady_emission_y="S1c",
                    allow_missing_s1=False,
                )

            validate_reconciled_sources(
                path,
                ("S1",),
                s1_limit=100,
                steady_emission_y="S1c",
                allow_missing_s1=True,
            )

    def test_early_source_reconciliation_honors_approved_missing_s1_option(self):
        with WorkspaceTempDir() as root:
            path = root / "run.sqlite3"
            snapshot = RunSnapshot(path)
            snapshot.add_source("S1", pathlib.Path("copy-a.opju"), "abc")
            book = _book("S1", "Root", "Book1", "Valid without S1")
            result = replace(
                _result(book),
                available_columns=("X", "S1c"),
                column_metadata=(
                    ("A", "X", "X"),
                    ("B", "S1c", "Y"),
                ),
                s1_x_values=None,
                s1_values=None,
                s1_max_for_limit=None,
                s1_max_for_limit_x=None,
                s1_limit_status="missing_allowed",
            )
            snapshot.record_inventory_book("S1", book)
            snapshot.record_book_result(result, book)

            snapshot.reconcile_source(
                "S1",
                s1_limit=100,
                steady_emission_y="S1c",
                allow_missing_s1=True,
            )

    def test_approved_missing_s1_may_preserve_an_all_blank_s1_column_name(self):
        with WorkspaceTempDir() as root:
            path = root / "run.sqlite3"
            snapshot = RunSnapshot(path)
            snapshot.add_source("S1", pathlib.Path("copy-a.opju"), "abc")
            book = _book("S1", "Root", "Book1", "Blank S1 column")
            result = replace(
                _result(book),
                s1_values=(None, None),
                s1_max_for_limit=None,
                s1_max_for_limit_x=None,
                s1_limit_status="missing_allowed",
            )
            snapshot.record_inventory_book("S1", book)
            snapshot.record_book_result(result, book)

            validate_reconciled_sources(
                path,
                ("S1",),
                s1_limit=100,
                steady_emission_y="S1c",
                allow_missing_s1=True,
            )

    def test_approved_missing_s1_requires_authenticated_blank_raw_values(self):
        with WorkspaceTempDir() as root:
            path = root / "run.sqlite3"
            snapshot = RunSnapshot(path)
            snapshot.add_source("S1", pathlib.Path("copy-a.opju"), "abc")
            book = _book("S1", "Root", "Book1", "Unproven blank S1 column")
            result = replace(
                _result(book),
                available_columns=("X", "S1c", "S1"),
                s1_max_for_limit=None,
                s1_max_for_limit_x=None,
                s1_limit_status="missing_allowed",
            )
            snapshot.record_inventory_book("S1", book)
            snapshot.record_book_result(result, book)

            with self.assertRaisesRegex(ReconciliationError, "raw S1|S1 evidence"):
                validate_reconciled_sources(
                    path,
                    ("S1",),
                    s1_limit=100,
                    steady_emission_y="S1c",
                    allow_missing_s1=True,
                )

    def test_approved_missing_s1_rejects_ambiguous_physical_s1_columns(self):
        with WorkspaceTempDir() as root:
            path = root / "run.sqlite3"
            snapshot = RunSnapshot(path)
            snapshot.add_source("S1", pathlib.Path("copy-a.opju"), "abc")
            book = _book("S1", "Root", "Book1", "Ambiguous S1 columns")
            result = replace(
                _result(book),
                available_columns=("X", "S1c", "S1X", "S1", "OtherX", "S1"),
                s1_max_for_limit=None,
                s1_max_for_limit_x=None,
                s1_limit_status="missing_allowed",
            )
            snapshot.record_inventory_book("S1", book)
            snapshot.record_book_result(result, book)

            with self.assertRaisesRegex(ReconciliationError, "Ambiguous S1"):
                validate_reconciled_sources(
                    path,
                    ("S1",),
                    s1_limit=100,
                    steady_emission_y="S1c",
                    allow_missing_s1=True,
                )

    def test_missing_s1_opt_in_rejects_worker_that_still_reports_missing_s1(self):
        with WorkspaceTempDir() as root:
            path = root / "run.sqlite3"
            snapshot = RunSnapshot(path)
            snapshot.add_source("S1", pathlib.Path("copy-a.opju"), "abc")
            book = _book("S1", "Root", "Book1", "Rejected by old worker")
            rejected = replace(
                _result(book),
                status="rejected",
                rejection_reason="missing S1: S1",
                available_columns=("X", "S1c"),
                s1_max_for_limit=None,
                s1_max_for_limit_x=None,
                s1_limit_status="failed",
            )
            snapshot.record_inventory_book("S1", book)
            snapshot.record_book_result(rejected, book)

            with self.assertRaisesRegex(ReconciliationError, "worker.*missing S1"):
                validate_reconciled_sources(
                    path,
                    ("S1",),
                    s1_limit=100,
                    steady_emission_y="S1c",
                    allow_missing_s1=True,
                )

    def test_rejected_partial_pair_shape_is_not_reconciled(self):
        with WorkspaceTempDir() as root:
            snapshot = RunSnapshot(root / "run.sqlite3")
            snapshot.add_source("S1", pathlib.Path("copy-a.opju"), "abc")
            book = _book("S1", "Root", "Book1", "Display")
            rejected = replace(
                _result(book, status="rejected"),
                selected_x_values=(300.0,),
                selected_x_row_count=1,
            )
            valid_book = _book("S1", "Root", "Book2", "Valid")
            snapshot.record_inventory_book("S1", book)
            snapshot.record_book_result(rejected, book)
            snapshot.record_inventory_book("S1", valid_book)
            snapshot.record_book_result(_result(valid_book), valid_book)

            with self.assertRaisesRegex(ReconciliationError, "row counts"):
                snapshot.reconcile_source("S1")

    def test_rejected_payload_fields_must_remain_semantically_consistent(self):
        mutations = {
            "columns": {"selected_y_column": "missing Y"},
            "maximum": {"max_planned_y": 999.0},
            "S1 limit": {"s1_max_for_limit": 1.0},
        }
        for label, changes in mutations.items():
            with self.subTest(label=label), WorkspaceTempDir() as root:
                snapshot = RunSnapshot(root / "run.sqlite3")
                snapshot.add_source("S1", pathlib.Path("copy-a.opju"), "abc")
                book = _book("S1", "Root", "Book1", "Rejected")
                fields = {
                    "status": "rejected",
                    "rejection_reason": "S1 max exceeds limit",
                    "s1_max_for_limit": 1_000_001.0,
                    "s1_limit_status": "exceeds_limit",
                    **changes,
                }
                rejected = replace(_result(book), **fields)
                snapshot.record_inventory_book("S1", book)
                snapshot.record_book_result(rejected, book)
                try:
                    snapshot.reconcile_source("S1")
                except ReconciliationError:
                    continue

                with self.assertRaises(ReconciliationError):
                    run_snapshot_module.validate_reconciled_sources(
                        snapshot.path,
                        ("S1",),
                        s1_limit=1_000_000,
                    )

    def test_parent_rejects_forged_terminal_rejection_of_valid_data(self):
        with WorkspaceTempDir() as root:
            snapshot = RunSnapshot(root / "run.sqlite3")
            snapshot.add_source("S1", pathlib.Path("copy-a.opju"), "abc")
            book = _book("S1", "Root", "Book1", "Valid data")
            forged = replace(
                _result(book),
                status="rejected",
                rejection_reason="FORGED: trust me",
                s1_limit_status="failed",
            )
            snapshot.record_inventory_book("S1", book)
            snapshot.record_book_result(forged, book)

            with self.assertRaisesRegex(
                ReconciliationError,
                "rejection|Rejected",
            ):
                snapshot.reconcile_source(
                    "S1",
                    s1_limit=2_000_000,
                    steady_emission_y="S1c",
                )

    def test_parent_rejects_unknown_sparse_terminal_rejection_reason(self):
        with WorkspaceTempDir() as root:
            snapshot = RunSnapshot(root / "run.sqlite3")
            snapshot.add_source("S1", pathlib.Path("copy-a.opju"), "abc")
            book = _book("S1", "Root", "Book1", "Sparse forgery")
            forged = replace(
                _result(book, status="rejected"),
                rejection_reason="FORGED: trust me",
            )
            snapshot.record_inventory_book("S1", book)
            snapshot.record_book_result(forged, book)

            with self.assertRaisesRegex(
                ReconciliationError,
                "reason|failure",
            ):
                snapshot.reconcile_source("S1")

    def test_parent_rejects_forged_specific_reason_for_a_different_data_defect(self):
        with WorkspaceTempDir() as root:
            snapshot = RunSnapshot(root / "run.sqlite3")
            snapshot.add_source(
                "S1",
                pathlib.Path("copy-a.opju"),
                "abc",
            )
            book = _book(
                "S1",
                "Root",
                "Book1",
                "Forged specific rejection",
            )
            forged = replace(
                _result(book),
                status="rejected",
                rejection_reason=(
                    "blank in column NOT_THE_REAL_COLUMN at row 999"
                ),
                selected_x_values=(),
                selected_y_values=(),
                selected_x_row_count=None,
                selected_y_row_count=None,
                max_planned_y=None,
                max_planned_y_x=None,
                s1_limit_status="failed",
            )
            snapshot.record_inventory_book("S1", book)
            snapshot.record_book_result(forged, book)

            with self.assertRaisesRegex(
                ReconciliationError,
                "reason does not match",
            ):
                snapshot.reconcile_source(
                    "S1",
                    s1_limit=2_000_000,
                    steady_emission_y="S1c",
                )

    def test_steady_2d_requires_empty_selected_pair_shape(self):
        with WorkspaceTempDir() as root:
            snapshot = RunSnapshot(root / "run.sqlite3")
            snapshot.add_source("S1", pathlib.Path("copy-a.opju"), "abc")
            book = _book("S1", "Root", "Book2D", "2D")
            special = replace(
                _result(book),
                selected_y_column=None,
                paired_x_column=None,
                selected_x_values=(),
                selected_y_values=(1.0,),
                selected_x_row_count=None,
                selected_y_row_count=1,
                max_planned_y=None,
                max_planned_y_x=None,
                s1_max_for_limit=None,
                s1_limit_status="not_applicable",
            )
            snapshot.record_inventory_book("S1", book)
            snapshot.record_book_result(special, book)

            with self.assertRaisesRegex(ReconciliationError, "row counts"):
                snapshot.reconcile_source("S1")

    def test_rejected_steady_2d_cannot_contain_ordinary_spectrum_fields(self):
        with WorkspaceTempDir() as root:
            snapshot = RunSnapshot(root / "run.sqlite3")
            snapshot.add_source("S1", pathlib.Path("copy-a.opju"), "abc")
            book = _book("S1", "Root", "Book2D", "2D")
            rejected = replace(
                _result(book, status="rejected"),
                note_text=(
                    "[EXP_FD_FILE]\n"
                    "Acquisition Type = 3D Acquisition[Excitation vs Emission vs Intensity]"
                ),
                spectrum_class="steady_2d",
                selected_y_column="S1c",
                paired_x_column="X",
            )
            snapshot.record_inventory_book("S1", book)
            snapshot.record_book_result(rejected, book)

            with self.assertRaisesRegex(ReconciliationError, "2D|ordinary"):
                snapshot.reconcile_source("S1")

    def test_ordinary_spectrum_class_cannot_use_2d_not_applicable_shape(self):
        with WorkspaceTempDir() as root:
            snapshot = RunSnapshot(root / "run.sqlite3")
            snapshot.add_source("S1", pathlib.Path("copy-a.opju"), "abc")
            book = _book("S1", "Root", "Book1", "Ordinary")
            disguised = replace(
                _result(book),
                spectrum_class="steady_emission",
                selected_y_column=None,
                paired_x_column=None,
                selected_x_values=(),
                selected_y_values=(),
                selected_x_row_count=None,
                selected_y_row_count=None,
                max_planned_y=None,
                max_planned_y_x=None,
                s1_max_for_limit=None,
                s1_limit_status="not_applicable",
            )
            snapshot.record_inventory_book("S1", book)
            snapshot.record_book_result(disguised, book)

            with self.assertRaisesRegex(ReconciliationError, "spectrum class"):
                snapshot.reconcile_source("S1")

    def test_extracted_ordinary_spectrum_cannot_claim_s1_limit_failure(self):
        with WorkspaceTempDir() as root:
            snapshot = RunSnapshot(root / "run.sqlite3")
            snapshot.add_source("S1", pathlib.Path("copy-a.opju"), "abc")
            book = _book("S1", "Root", "Book1", "Saturated")
            contradictory = replace(
                _result(book),
                rejection_reason="S1 max exceeds limit",
                s1_max_for_limit=1_000_001,
                s1_limit_status="exceeds_limit",
            )
            snapshot.record_inventory_book("S1", book)
            snapshot.record_book_result(contradictory, book)

            with self.assertRaisesRegex(ReconciliationError, "S1 limit status"):
                snapshot.reconcile_source("S1")

    def test_unknown_spectrum_class_is_rejected(self):
        with WorkspaceTempDir() as root:
            snapshot = RunSnapshot(root / "run.sqlite3")
            snapshot.add_source("S1", pathlib.Path("copy-a.opju"), "abc")
            book = _book("S1", "Root", "Book1", "Unknown")
            snapshot.record_inventory_book("S1", book)
            snapshot.record_book_result(replace(_result(book), spectrum_class="not_a_spectrum_class"), book)

            with self.assertRaisesRegex(ReconciliationError, "spectrum class"):
                snapshot.reconcile_source("S1")

    def test_claimed_supported_class_requires_matching_instrument_note(self):
        with WorkspaceTempDir() as root:
            snapshot = RunSnapshot(root / "run.sqlite3")
            snapshot.add_source("S1", pathlib.Path("copy-a.opju"), "abc")
            book = _book("S1", "Root", "Book1", "Forged Note")
            snapshot.record_inventory_book("S1", book)
            snapshot.record_book_result(replace(_result(book), note_text="not an instrument Note"), book)

            with self.assertRaisesRegex(ReconciliationError, "Note|spectrum class"):
                snapshot.reconcile_source("S1")

    def test_parent_reconciliation_enforces_confirmed_s1_limit(self):
        with WorkspaceTempDir() as root:
            snapshot = RunSnapshot(root / "run.sqlite3")
            snapshot.add_source("S1", pathlib.Path("copy-a.opju"), "abc")
            book = _book("S1", "Root", "Book1", "Over Limit")
            snapshot.record_inventory_book("S1", book)
            snapshot.record_book_result(
                replace(
                    _result(book),
                    s1_values=(100.0, 2_000_000.0),
                    s1_max_for_limit=2_000_000.0,
                ),
                book,
            )
            snapshot.reconcile_source("S1")

            with self.assertRaisesRegex(ReconciliationError, "S1 limit"):
                run_snapshot_module.validate_reconciled_sources(
                    snapshot.path,
                    ("S1",),
                    s1_limit=1_000_000,
                )

    def test_parent_rejects_s1_summary_that_disagrees_with_raw_evidence(self):
        with WorkspaceTempDir() as root:
            snapshot = RunSnapshot(root / "run.sqlite3")
            snapshot.add_source("S1", pathlib.Path("copy-a.opju"), "abc")
            book = _book("S1", "Root", "Book1", "Forged S1 summary")
            snapshot.record_inventory_book("S1", book)
            snapshot.record_book_result(
                replace(_result(book), s1_max_for_limit=999.0),
                book,
            )

            with self.assertRaisesRegex(ReconciliationError, "S1 maximum semantics"):
                snapshot.reconcile_source("S1")

    def test_parent_preserves_every_x_at_a_tied_s1_maximum(self):
        with WorkspaceTempDir() as root:
            snapshot = RunSnapshot(root / "run.sqlite3")
            snapshot.add_source("S1", pathlib.Path("copy-a.opju"), "abc")
            book = _book("S1", "Root", "Book1", "Tied S1 maximum")
            snapshot.record_inventory_book("S1", book)
            snapshot.record_book_result(
                replace(
                    _result(book),
                    s1_values=(120.0, 120.0),
                    s1_max_for_limit=120.0,
                    s1_max_for_limit_x=(300.0, 301.0),
                ),
                book,
            )

            snapshot.reconcile_source("S1")

    def test_parent_rejects_a_scalar_x_for_a_tied_s1_maximum(self):
        with WorkspaceTempDir() as root:
            snapshot = RunSnapshot(root / "run.sqlite3")
            snapshot.add_source("S1", pathlib.Path("copy-a.opju"), "abc")
            book = _book("S1", "Root", "Book1", "Forged tied S1 maximum")
            snapshot.record_inventory_book("S1", book)
            snapshot.record_book_result(
                replace(
                    _result(book),
                    s1_values=(120.0, 120.0),
                    s1_max_for_limit=120.0,
                    s1_max_for_limit_x=300.0,
                ),
                book,
            )

            with self.assertRaisesRegex(ReconciliationError, "S1 maximum semantics"):
                snapshot.reconcile_source("S1")

    def test_parent_rejects_duplicate_selected_and_s1_x_evidence(self):
        cases = (
            (
                "selected",
                {
                    "selected_x_values": (300.0, 300.0),
                    "max_planned_y_x": 300.0,
                },
                "duplicate selected X",
            ),
            (
                "s1",
                {
                    "s1_x_values": (300.0, 300.0),
                    "s1_max_for_limit_x": 300.0,
                },
                "duplicate S1 X",
            ),
        )
        with WorkspaceTempDir() as root:
            for suffix, overrides, error in cases:
                with self.subTest(case=suffix):
                    snapshot = RunSnapshot(root / f"run-{suffix}.sqlite3")
                    snapshot.add_source(
                        "S1",
                        pathlib.Path("copy-a.opju"),
                        "abc",
                    )
                    book = _book(
                        "S1",
                        "Root",
                        "Book1",
                        "Duplicate X",
                    )
                    snapshot.record_inventory_book("S1", book)
                    snapshot.record_book_result(
                        replace(_result(book), **overrides),
                        book,
                    )

                    with self.assertRaisesRegex(
                        ReconciliationError,
                        error,
                    ):
                        snapshot.reconcile_source("S1")

    def test_parent_rejects_extracted_nonblank_s1_without_paired_x_evidence(self):
        with WorkspaceTempDir() as root:
            snapshot = RunSnapshot(root / "run.sqlite3")
            snapshot.add_source("S1", pathlib.Path("copy-a.opju"), "abc")
            book = _book("S1", "Root", "Book1", "S1 without paired X")
            snapshot.record_inventory_book("S1", book)
            snapshot.record_book_result(
                replace(
                    _result(book),
                    s1_x_values=None,
                    s1_max_for_limit_x=None,
                ),
                book,
            )

            with self.assertRaisesRegex(ReconciliationError, "paired S1 X"):
                snapshot.reconcile_source("S1")

    def test_parent_accepts_extra_trailing_s1_x_rows(self):
        with WorkspaceTempDir() as root:
            snapshot = RunSnapshot(root / "run.sqlite3")
            snapshot.add_source("S1", pathlib.Path("copy-a.opju"), "abc")
            book = _book("S1", "Root", "Book1", "S1 with trailing X")
            snapshot.record_inventory_book("S1", book)
            snapshot.record_book_result(
                replace(
                    _result(book),
                    s1_x_values=(300.0, 301.0, 302.0),
                ),
                book,
            )

            snapshot.reconcile_source("S1")

    def test_selected_columns_must_exist_in_available_columns(self):
        with WorkspaceTempDir() as root:
            for field, missing_name in (("selected_y_column", "MissingY"), ("paired_x_column", "MissingX")):
                with self.subTest(field=field):
                    snapshot = RunSnapshot(root / f"run-{field}.sqlite3")
                    snapshot.add_source("S1", pathlib.Path("copy-a.opju"), "abc")
                    book = _book("S1", "Root", "Book1", "Display")
                    snapshot.record_inventory_book("S1", book)
                    snapshot.record_book_result(replace(_result(book), **{field: missing_name}), book)

                    with self.assertRaisesRegex(ReconciliationError, "available columns"):
                        snapshot.reconcile_source("S1")

    def test_parent_rejects_self_checksummed_column_designation_swap(self):
        with WorkspaceTempDir() as root:
            path = root / "run.sqlite3"
            snapshot = RunSnapshot(path)
            snapshot.add_source("S1", pathlib.Path("copy-a.opju"), "abc")
            book = _book("S1", "Root", "Book1", "Display")
            snapshot.record_inventory_book("S1", book)
            snapshot.record_book_result(_result(book), book)
            with closing(sqlite3.connect(path)) as connection:
                row = list(
                    connection.execute(
                        run_snapshot_module._BOOK_PAYLOAD_QUERY,
                        ("S1", "worksheet", "Root", "Book1"),
                    ).fetchone()
                )
                metadata = json.loads(row[20])
                metadata[0][2], metadata[1][2] = (
                    metadata[1][2],
                    metadata[0][2],
                )
                row[20] = json.dumps(metadata)
                row[21] = run_snapshot_module._payload_checksum(
                    "S1",
                    "worksheet",
                    "Root",
                    "Book1",
                    *row[:-1],
                )
                connection.execute(
                    """
                    update book_results
                    set column_metadata_json = ?, payload_checksum = ?
                    where source_id = 'S1'
                    """,
                    (row[20], row[21]),
                )
                connection.commit()

            with self.assertRaisesRegex(
                ReconciliationError,
                "designation|physical column metadata",
            ):
                snapshot.reconcile_source("S1")

    def test_parent_rejects_selected_y_aliasing_the_physical_s1_column(self):
        with WorkspaceTempDir() as root:
            for short_name, long_name in (
                ("S1c", "S1"),
                ("S1", "S1c"),
            ):
                with self.subTest(
                    short_name=short_name,
                    long_name=long_name,
                ):
                    snapshot = RunSnapshot(
                        root / f"run-{short_name}.sqlite3"
                    )
                    snapshot.add_source(
                        "S1",
                        pathlib.Path("copy-a.opju"),
                        "abc",
                    )
                    book = _book(
                        "S1",
                        "Root",
                        "Book1",
                        "Aliased roles",
                    )
                    result = replace(
                        _result(book),
                        available_columns=("X", "S1c", "S1"),
                        column_metadata=(
                            ("A", "X", "X"),
                            (short_name, long_name, "Y"),
                        ),
                    )
                    snapshot.record_inventory_book("S1", book)
                    snapshot.record_book_result(result, book)

                    with self.assertRaisesRegex(
                        ReconciliationError,
                        "selected Y.*same physical.*S1|S1.*same physical.*selected Y",
                    ):
                        snapshot.reconcile_source("S1")

    def test_parent_rejects_non_x_designated_s1_predecessor(self):
        with WorkspaceTempDir() as root:
            snapshot = RunSnapshot(root / "run.sqlite3")
            snapshot.add_source(
                "S1",
                pathlib.Path("copy-a.opju"),
                "abc",
            )
            book = _book(
                "S1",
                "Root",
                "Book1",
                "Invalid S1 predecessor",
            )
            result = replace(
                _result(book),
                column_metadata=(
                    ("A", "X", "X"),
                    ("B", "S1c", "Y"),
                    ("C", "S1X", "Y"),
                    ("D", "S1", "Y"),
                ),
            )
            snapshot.record_inventory_book("S1", book)
            snapshot.record_book_result(result, book)

            with self.assertRaisesRegex(
                ReconciliationError,
                "S1 X/Y designations are invalid",
            ):
                snapshot.reconcile_source("S1")

    def test_parent_accepts_authenticated_selected_y_s1_alias_rejection(self):
        with WorkspaceTempDir() as root:
            snapshot = RunSnapshot(root / "run.sqlite3")
            snapshot.add_source(
                "S1",
                pathlib.Path("copy-a.opju"),
                "abc",
            )
            book = _book(
                "S1",
                "Root",
                "Book1",
                "Aliased roles",
            )
            result = replace(
                _result(book),
                status="rejected",
                rejection_reason=(
                    "selected Y and S1 resolve to the same physical "
                    "column: S1c"
                ),
                s1_limit_status="failed",
                available_columns=("X", "S1c", "S1"),
                column_metadata=(
                    ("A", "X", "X"),
                    ("S1c", "S1", "Y"),
                ),
            )
            snapshot.record_inventory_book("S1", book)
            snapshot.record_book_result(result, book)

            snapshot.reconcile_source("S1")

    def test_selected_ratio_column_accepts_instrument_spacing_alias(self):
        with WorkspaceTempDir() as root:
            snapshot = RunSnapshot(root / "run.sqlite3")
            snapshot.add_source("S1", pathlib.Path("copy-a.opju"), "abc")
            book = _book("S1", "Root", "Book1", "Excitation")
            result = replace(
                _result(book),
                note_text="[EXP_FD_FILE]\nAcquisition Type = Spectral Acquisition[Excitation]",
                spectrum_class="steady_excitation",
                available_columns=("Wavelength", "S1c/R1c", "S1X", "S1"),
                column_metadata=(
                    ("A", "Wavelength", "X"),
                    ("B", "S1c / R1c", "Y"),
                    ("C", "S1X", "X"),
                    ("D", "S1", "Y"),
                ),
                selected_y_column="S1c/R1c",
                paired_x_column="Wavelength",
            )
            snapshot.record_inventory_book("S1", book)
            snapshot.record_book_result(result, book)

            snapshot.reconcile_source("S1")

    def test_parent_requires_class_selected_y_and_its_preceding_x(self):
        with WorkspaceTempDir() as root:
            snapshot = RunSnapshot(root / "run.sqlite3")
            snapshot.add_source("S1", pathlib.Path("copy-a.opju"), "abc")
            book = _book("S1", "Root", "Book1", "Excitation")
            snapshot.record_inventory_book("S1", book)
            forged = replace(
                _result(book),
                note_text="[EXP_FD_FILE]\nAcquisition Type = Spectral Acquisition[Excitation]",
                spectrum_class="steady_excitation",
                available_columns=("WrongX", "WrongY", "RatioX", "S1c/R1c", "S1X", "S1"),
                selected_y_column="WrongY",
                paired_x_column="WrongX",
            )
            snapshot.record_book_result(forged, book)
            with self.assertRaisesRegex(ReconciliationError, "required selected Y|paired X"):
                snapshot.reconcile_source("S1")

    def test_parent_rejects_wrong_selected_y_even_when_rejected_payload_has_no_pair(self):
        with WorkspaceTempDir() as root:
            snapshot = RunSnapshot(root / "run.sqlite3")
            snapshot.add_source("S1", pathlib.Path("copy-a.opju"), "abc")
            book = _book("S1", "Root", "Book1", "Rejected")
            snapshot.record_inventory_book("S1", book)
            snapshot.record_book_result(
                replace(_result(book, status="rejected"), selected_y_column="WRONG_COLUMN"),
                book,
            )
            snapshot.reconcile_source("S1")

            with self.assertRaisesRegex(ReconciliationError, "required selected Y"):
                run_snapshot_module.validate_reconciled_sources(
                    snapshot.path,
                    ("S1",),
                    steady_emission_y="S1c",
                )

    def test_parent_rejects_missing_paired_x_claim_on_rejected_no_pair_payload(self):
        with WorkspaceTempDir() as root:
            snapshot = RunSnapshot(root / "run.sqlite3")
            snapshot.add_source("S1", pathlib.Path("copy-a.opju"), "abc")
            book = _book("S1", "Root", "Book1", "Rejected")
            snapshot.record_inventory_book("S1", book)
            snapshot.record_book_result(
                replace(
                    _result(book, status="rejected"),
                    available_columns=("X", "S1c", "S1"),
                    selected_y_column="S1c",
                    paired_x_column="MissingX",
                ),
                book,
            )
            with self.assertRaisesRegex(ReconciliationError, "available columns|paired X"):
                snapshot.reconcile_source("S1")

    def test_parent_rejects_exceeds_limit_without_measured_s1_maximum(self):
        with WorkspaceTempDir() as root:
            snapshot = RunSnapshot(root / "run.sqlite3")
            snapshot.add_source("S1", pathlib.Path("copy-a.opju"), "abc")
            book = _book("S1", "Root", "Book1", "Rejected")
            snapshot.record_inventory_book("S1", book)
            snapshot.record_book_result(
                replace(
                    _result(book, status="rejected"),
                    s1_limit_status="exceeds_limit",
                    s1_max_for_limit=None,
                ),
                book,
            )
            with self.assertRaisesRegex(ReconciliationError, "S1.*maximum|S1 limit"):
                snapshot.reconcile_source("S1")

    def test_parent_rejects_exceeds_limit_without_paired_s1_x_evidence(self):
        with WorkspaceTempDir() as root:
            snapshot = RunSnapshot(root / "run.sqlite3")
            snapshot.add_source("S1", pathlib.Path("copy-a.opju"), "abc")
            book = _book("S1", "Root", "Book1", "Rejected over limit")
            snapshot.record_inventory_book("S1", book)
            snapshot.record_book_result(
                replace(
                    _result(book, status="rejected"),
                    rejection_reason="S1 max exceeds limit",
                    available_columns=("X", "S1c", "S1"),
                    s1_values=(100.0, 120.0),
                    s1_x_values=None,
                    s1_max_for_limit=120.0,
                    s1_max_for_limit_x=None,
                    s1_limit_status="exceeds_limit",
                ),
                book,
            )

            with self.assertRaisesRegex(ReconciliationError, "paired S1 X"):
                snapshot.reconcile_source("S1", s1_limit=100)

    def test_parent_rejects_exceeds_limit_with_unlisted_s1_evidence(self):
        with WorkspaceTempDir() as root:
            snapshot = RunSnapshot(root / "run.sqlite3")
            snapshot.add_source("S1", pathlib.Path("copy-a.opju"), "abc")
            book = _book("S1", "Root", "Book1", "Rejected over limit")
            snapshot.record_inventory_book("S1", book)
            snapshot.record_book_result(
                replace(
                    _result(book, status="rejected"),
                    rejection_reason="S1 max exceeds limit",
                    available_columns=("X", "S1c"),
                    s1_values=(100.0, 120.0),
                    s1_x_values=(300.0, 301.0),
                    s1_max_for_limit=120.0,
                    s1_max_for_limit_x=301.0,
                    s1_limit_status="exceeds_limit",
                ),
                book,
            )

            with self.assertRaisesRegex(ReconciliationError, "unique S1|listed S1"):
                snapshot.reconcile_source("S1", s1_limit=100)

    def test_duplicate_identity_and_missing_terminal_result_abort(self):
        with WorkspaceTempDir() as root:
            snapshot = RunSnapshot(root / "run.sqlite3")
            snapshot.add_source("S1", pathlib.Path("copy-a.opju"), "abc")
            book = _book("S1", "Root", "Book1", "Display")
            with self.assertRaises(SnapshotError):
                snapshot.record_inventory("S1", [book, book])
            snapshot.discard_source_partition("S1")
            snapshot.record_inventory("S1", [book])
            with self.assertRaises(ReconciliationError):
                snapshot.reconcile_source("S1")

    def test_zero_supported_books_abort(self):
        with WorkspaceTempDir() as root:
            snapshot = RunSnapshot(root / "run.sqlite3")
            snapshot.add_source("S1", pathlib.Path("copy-a.opju"), "abc")
            snapshot.record_inventory("S1", [])
            with self.assertRaises(ReconciliationError):
                snapshot.reconcile_source("S1")

    def test_rejected_only_recognized_supported_inventory_reconciles(self):
        with WorkspaceTempDir() as root:
            snapshot = RunSnapshot(root / "run.sqlite3")
            snapshot.add_source("S1", pathlib.Path("copy-a.opju"), "abc")
            book = _book("S1", "Root", "Book1", "Display")
            snapshot.record_inventory("S1", [book])
            snapshot.record_book_result(_result(book, "rejected"), book)

            snapshot.reconcile_source("S1")

    def test_rejected_only_dual_detector_note_still_counts_as_supported_source(self):
        with WorkspaceTempDir() as root:
            snapshot = RunSnapshot(root / "run.sqlite3")
            snapshot.add_source("S1", pathlib.Path("copy-a.opju"), "abc")
            book = _book("S1", "Root", "Book1", "Display")
            note_text = (
                "[EXP_FD_FILE]\n"
                "Experiment Type: Spectral Acquisition[Emission]\n"
                "EM1: Emission 1 (Mono4)\n"
                "Detector: S (SCD100)\n"
                "Units: Counts\n"
                "Corrected: S1_R928P_1200-500.SPC\n"
                "Detector: R (SCD101)\n"
                "Units: MicroAmps\n"
                "Corrected: R1_PD_1200-330.SPC\n"
                "ACCESSORIES:\n"
            )
            result = replace(
                _result(book, "rejected"),
                note_text=note_text,
                spectrum_class="steady_emission",
            )
            snapshot.record_inventory("S1", [book])
            snapshot.record_book_result(result, book)

            snapshot.reconcile_source("S1")

    def test_rejected_supported_class_without_note_or_data_structure_aborts(self):
        with WorkspaceTempDir() as root:
            for suffix, overrides in (
                ("note", {"sheets": ("Data",), "has_note": False}),
                ("data", {"sheets": ("Note",), "has_data": False}),
            ):
                with self.subTest(missing=suffix):
                    snapshot = RunSnapshot(root / f"run-{suffix}.sqlite3")
                    snapshot.add_source("S1", pathlib.Path("copy-a.opju"), "abc")
                    book = _book("S1", "Root", "Book1", "Display", **overrides)
                    snapshot.record_inventory("S1", [book])
                    snapshot.record_book_result(_result(book, "rejected"), book)

                    with self.assertRaisesRegex(
                        ReconciliationError,
                        "zero recognizable supported raw-spectrum Books",
                    ):
                        snapshot.reconcile_source("S1")

    def test_note_and_data_flags_must_match_inventory_sheet_names(self):
        with WorkspaceTempDir() as root:
            for suffix, sheets in (("note", ("Data",)), ("data", ("Note",))):
                with self.subTest(missing=suffix):
                    snapshot = RunSnapshot(root / f"run-flags-{suffix}.sqlite3")
                    snapshot.add_source("S1", pathlib.Path("copy-a.opju"), "abc")
                    book = _book(
                        "S1",
                        "Root",
                        "Book1",
                        "Display",
                        sheets=sheets,
                        has_note=True,
                        has_data=True,
                    )
                    snapshot.record_inventory_book("S1", book)
                    snapshot.record_book_result(_result(book, "rejected"), book)

                    with self.assertRaisesRegex(
                        ReconciliationError,
                        "zero recognizable supported raw-spectrum Books|metadata",
                    ):
                        snapshot.reconcile_source("S1")

    def test_extracted_payload_rejects_nonfinite_and_inconsistent_numeric_semantics(self):
        mutations = (
            ("nonfinite-x", {"selected_x_values": (float("nan"), 301.0)}),
            ("nonfinite-y", {"selected_y_values": (10.0, float("inf"))}),
            ("oversized-y", {"selected_y_values": (10**400, 12.0)}),
            ("wrong-max-y", {"max_planned_y": 11.0}),
            ("wrong-max-x", {"max_planned_y_x": 300.0}),
            ("nonpositive-max-y", {"selected_y_values": (-2.0, -1.0), "max_planned_y": -1.0}),
            ("nonfinite-s1", {"s1_max_for_limit": float("nan")}),
        )
        with WorkspaceTempDir() as root:
            for suffix, overrides in mutations:
                with self.subTest(case=suffix):
                    snapshot = RunSnapshot(root / f"run-{suffix}.sqlite3")
                    snapshot.add_source("S1", pathlib.Path("copy-a.opju"), "abc")
                    book = _book("S1", "Root", "Book1", "Display")
                    snapshot.record_inventory("S1", [book])
                    snapshot.record_book_result(replace(_result(book), **overrides), book)

                    with self.assertRaisesRegex(ReconciliationError, "numeric|maximum|normalization"):
                        snapshot.reconcile_source("S1")

    def test_rejected_payload_rejects_nonfinite_present_numeric_fields(self):
        with WorkspaceTempDir() as root:
            for suffix, overrides in (
                (
                    "array",
                    {
                        "selected_x_values": (300.0,),
                        "selected_y_values": (float("nan"),),
                        "selected_x_row_count": 1,
                        "selected_y_row_count": 1,
                    },
                ),
                ("maximum", {"max_planned_y": float("inf")}),
            ):
                with self.subTest(case=suffix):
                    snapshot = RunSnapshot(root / f"run-rejected-{suffix}.sqlite3")
                    snapshot.add_source("S1", pathlib.Path("copy-a.opju"), "abc")
                    book = _book("S1", "Root", "Book1", "Display")
                    snapshot.record_inventory("S1", [book])
                    snapshot.record_book_result(replace(_result(book, "rejected"), **overrides), book)

                    with self.assertRaisesRegex(ReconciliationError, "numeric"):
                        snapshot.reconcile_source("S1")

    def test_steady_2d_requires_all_ordinary_numeric_fields_to_be_empty(self):
        with WorkspaceTempDir() as root:
            snapshot = RunSnapshot(root / "run.sqlite3")
            snapshot.add_source("S1", pathlib.Path("copy-a.opju"), "abc")
            book = _book("S1", "Root", "Book2D", "2D")
            special = replace(
                _result(book),
                spectrum_class="steady_2d",
                note_text=(
                    "[EXP_FD_FILE]\n"
                    "Acquisition Type = 3D Acquisition[Excitation vs Emission vs Intensity]"
                ),
                selected_y_column=None,
                paired_x_column=None,
                selected_x_values=(),
                selected_y_values=(),
                selected_x_row_count=None,
                selected_y_row_count=None,
                max_planned_y=1.0,
                max_planned_y_x=None,
                s1_max_for_limit=None,
                s1_limit_status="not_applicable",
            )
            snapshot.record_inventory("S1", [book])
            snapshot.record_book_result(special, book)

            with self.assertRaisesRegex(ReconciliationError, "2D|ordinary"):
                snapshot.reconcile_source("S1")

    def test_rejected_only_unrecognized_inventory_aborts_reconciliation(self):
        with WorkspaceTempDir() as root:
            snapshot = RunSnapshot(root / "run.sqlite3")
            snapshot.add_source("S1", pathlib.Path("copy-a.opju"), "abc")
            book = _book("S1", "Root", "Book1", "Display", sheets=("Note",), has_data=False)
            snapshot.record_inventory("S1", [book])
            snapshot.record_book_result(replace(_result(book, "rejected"), spectrum_class=None), book)

            with self.assertRaisesRegex(ReconciliationError, "zero recognizable supported raw-spectrum Books"):
                snapshot.reconcile_source("S1")

    def test_reconciliation_checks_cancellation_between_terminal_rows(self):
        with WorkspaceTempDir() as root:
            snapshot = RunSnapshot(root / "run.sqlite3")
            snapshot.add_source("S1", pathlib.Path("copy-a.opju"), "abc")
            books = [
                replace(_book("S1", "Root", f"Book{index}", f"Display{index}"), page_order=index)
                for index in range(1, 4)
            ]
            snapshot.record_inventory("S1", books)
            for book in books:
                snapshot.record_book_result(_result(book, "rejected"), book)
            checks = []

            def cancel_check():
                checks.append(None)
                if len(checks) == 3:
                    raise RuntimeError("cancelled")

            with self.assertRaisesRegex(RuntimeError, "cancelled"):
                run_snapshot_module.validate_reconciled_sources(
                    snapshot.path,
                    ("S1",),
                    cancel_check=cancel_check,
                )
            self.assertEqual(3, len(checks))

    def test_snapshot_approval_hash_checks_cancellation_between_rows(self):
        with closing(sqlite3.connect(":memory:")) as connection:
            connection.execute("create table payloads (value text)")
            connection.executemany(
                "insert into payloads (value) values (?)",
                ((f"row-{index}",) for index in range(20)),
            )
            checks = []

            def cancel_check():
                checks.append(None)
                if len(checks) == 4:
                    raise RuntimeError("cancelled during digest")

            with self.assertRaisesRegex(RuntimeError, "cancelled during digest"):
                run_snapshot_module._snapshot_content_sha256(
                    connection,
                    cancel_check=cancel_check,
                )

        self.assertEqual(4, len(checks))

    def test_snapshot_content_hash_is_independent_of_insertion_order(self):
        with (
            closing(sqlite3.connect(":memory:")) as first,
            closing(sqlite3.connect(":memory:")) as second,
        ):
            for connection in (first, second):
                connection.execute("create table payloads (sequence integer, value text)")
            rows = [(2, "beta"), (1, "alpha"), (3, "gamma")]
            first.executemany("insert into payloads values (?, ?)", rows)
            second.executemany("insert into payloads values (?, ?)", reversed(rows))

            self.assertEqual(
                run_snapshot_module._snapshot_content_sha256(first),
                run_snapshot_module._snapshot_content_sha256(second),
            )

    def test_snapshot_content_hash_orders_adjacent_real_values_independently_of_insertion(self):
        with (
            closing(sqlite3.connect(":memory:")) as first,
            closing(sqlite3.connect(":memory:")) as second,
        ):
            for connection in (first, second):
                connection.execute("create table payloads (value real)")
            rows = [(1.0,), (math.nextafter(1.0, 2.0),)]
            first.executemany("insert into payloads values (?)", rows)
            second.executemany("insert into payloads values (?)", reversed(rows))

            self.assertEqual(
                run_snapshot_module._snapshot_content_sha256(first),
                run_snapshot_module._snapshot_content_sha256(second),
            )

    def test_snapshot_content_hash_includes_legal_sqlite_prefix_without_underscore(self):
        with (
            closing(sqlite3.connect(":memory:")) as first,
            closing(sqlite3.connect(":memory:")) as second,
        ):
            for connection in (first, second):
                connection.execute("create table payloads (value text)")
                connection.execute("insert into payloads values ('same')")
            second.execute(
                "create trigger sqliteevil after insert on payloads begin select 1; end"
            )

            self.assertNotEqual(
                run_snapshot_module._snapshot_content_sha256(first),
                run_snapshot_module._snapshot_content_sha256(second),
            )

    def test_snapshot_approval_hash_checks_cancellation_while_sqlite_prepares_sort(self):
        class TrackingConnection(sqlite3.Connection):
            preparing_table_rows = False

            def execute(self, statement, parameters=()):
                is_table_read = " ".join(statement.lower().split()).startswith("select * from")
                if not is_table_read:
                    return super().execute(statement, parameters)
                self.preparing_table_rows = True
                try:
                    return super().execute(statement, parameters)
                finally:
                    self.preparing_table_rows = False

        with closing(
            sqlite3.connect(":memory:", factory=TrackingConnection)
        ) as connection:
            connection.execute("create table payloads (value text)")
            connection.executemany(
                "insert into payloads values (?)",
                ((f"{index:05d}-" + "x" * 256,) for index in range(5000, 0, -1)),
            )

            def cancel_check():
                if connection.preparing_table_rows:
                    raise RuntimeError("cancelled while SQLite prepared the sort")

            with self.assertRaisesRegex(
                RuntimeError,
                "cancelled while SQLite prepared the sort",
            ):
                run_snapshot_module._snapshot_content_sha256(
                    connection,
                    cancel_check=cancel_check,
                )

    def test_page_type_is_stored_and_used_in_identity(self):
        with WorkspaceTempDir() as root:
            snapshot = RunSnapshot(root / "run.sqlite3")
            snapshot.add_source("S1", pathlib.Path("copy-a.opju"), "abc")
            worksheet = _book("S1", "Root", "Book1", "Display", page_type="worksheet")
            matrix = _book("S1", "Root", "Book1", "Display", page_type="matrix")
            snapshot.record_book_transaction("S1", worksheet, _result(worksheet))
            snapshot.record_book_transaction("S1", matrix, _result(matrix, "rejected"))
            snapshot.reconcile_source("S1")

            self.assertEqual(
                [("S1", "matrix", "Root", "Book1"), ("S1", "worksheet", "Root", "Book1")],
                [row.identity for row in snapshot.inventory_rows("S1")],
            )
            self.assertEqual(
                [("S1", "matrix", "Root", "Book1"), ("S1", "worksheet", "Root", "Book1")],
                [row.identity for row in snapshot.book_results("S1")],
            )

    def test_parent_rejects_extracted_matrix_payload(self):
        with WorkspaceTempDir() as root:
            snapshot = RunSnapshot(root / "run.sqlite3")
            snapshot.add_source("S1", pathlib.Path("copy-a.opju"), "abc")
            matrix = _book("S1", "Root", "Matrix1", "Matrix", page_type="matrix")
            snapshot.record_inventory_book("S1", matrix)
            snapshot.record_book_result(_result(matrix), matrix)

            with self.assertRaisesRegex(ReconciliationError, "page type|matrix|worksheet"):
                snapshot.reconcile_source("S1")

    def test_legacy_snapshot_primary_key_is_migrated_to_include_page_type(self):
        with WorkspaceTempDir() as root:
            db_path = root / "legacy.sqlite3"
            _create_legacy_snapshot_schema(db_path)
            snapshot = RunSnapshot(db_path)
            snapshot.add_source("S1", pathlib.Path("copy-a.opju"), "abc")
            worksheet = _book("S1", "Root", "Book1", "Display", page_type="worksheet")
            matrix = _book("S1", "Root", "Book1", "Display", page_type="matrix")

            snapshot.record_book_transaction("S1", worksheet, _result(worksheet))
            snapshot.record_book_transaction("S1", matrix, _result(matrix, "rejected"))
            snapshot.reconcile_source("S1")

            self.assertEqual(
                [("S1", "matrix", "Root", "Book1"), ("S1", "worksheet", "Root", "Book1")],
                [row.identity for row in snapshot.inventory_rows("S1")],
            )

    def test_legacy_snapshot_with_nullable_page_type_values_migrates_to_worksheet(self):
        with WorkspaceTempDir() as root:
            db_path = root / "legacy-null-page-type.sqlite3"
            _create_legacy_snapshot_schema(db_path, include_page_type=True)
            with closing(sqlite3.connect(db_path)) as connection:
                connection.execute("insert into source_files (source_id, copy_path, sha256) values ('S1', 'copy-a.opju', 'abc')")
                connection.execute(
                    """
                    insert into inventory_rows (
                        source_id, page_type, folder_path, short_name, display_name, page_order,
                        sheet_names_json, has_note, has_data
                    ) values ('S1', null, 'Root', 'Book0', 'Display', 1, '["Note", "Data"]', 1, 1)
                    """
                )
                connection.commit()
            snapshot = RunSnapshot(db_path)
            matrix = _book("S1", "Root", "Book0", "Display", page_type="matrix")

            snapshot.record_book_transaction("S1", matrix, _result(matrix))

            self.assertEqual(
                [("S1", "matrix", "Root", "Book0"), ("S1", "worksheet", "Root", "Book0")],
                [row.identity for row in snapshot.inventory_rows("S1")],
            )

    def test_current_pk_snapshot_with_nullable_page_type_values_is_normalized(self):
        with WorkspaceTempDir() as root:
            db_path = root / "current-null-page-type.sqlite3"
            snapshot = RunSnapshot(db_path)
            snapshot.add_source("S1", pathlib.Path("copy-a.opju"), "abc")
            with closing(sqlite3.connect(db_path)) as connection:
                connection.execute(
                    """
                    insert into inventory_rows (
                        source_id, page_type, folder_path, short_name, display_name, page_order,
                        sheet_names_json, has_note, has_data
                    ) values ('S1', '', 'Root', 'Book0', 'Display', 1, '["Note", "Data"]', 1, 1)
                    """
                )
                connection.commit()

            reopened = RunSnapshot(db_path)
            matrix = _book("S1", "Root", "Book0", "Display", page_type="matrix")
            reopened.record_book_transaction("S1", matrix, _result(matrix))

            self.assertEqual(
                [("S1", "matrix", "Root", "Book0"), ("S1", "worksheet", "Root", "Book0")],
                [row.identity for row in reopened.inventory_rows("S1")],
            )


    def test_current_pk_snapshot_with_duplicate_blank_page_type_row_drops_duplicate(self):
        with WorkspaceTempDir() as root:
            db_path = root / "current-duplicate-page-type.sqlite3"
            snapshot = RunSnapshot(db_path)
            snapshot.add_source("S1", pathlib.Path("copy-a.opju"), "abc")
            with closing(sqlite3.connect(db_path)) as connection:
                connection.execute(
                    """
                    insert into inventory_rows (
                        source_id, page_type, folder_path, short_name, display_name, page_order,
                        sheet_names_json, has_note, has_data
                    ) values ('S1', 'worksheet', 'Root', 'Book0', 'Display', 1, '["Note", "Data"]', 1, 1)
                    """
                )
                connection.execute(
                    """
                    insert into inventory_rows (
                        source_id, page_type, folder_path, short_name, display_name, page_order,
                        sheet_names_json, has_note, has_data
                    ) values ('S1', '', 'Root', 'Book0', 'Display', 1, '["Note", "Data"]', 1, 1)
                    """
                )
                connection.commit()

            reopened = RunSnapshot(db_path)

            self.assertEqual(
                [("S1", "worksheet", "Root", "Book0")],
                [row.identity for row in reopened.inventory_rows("S1")],
            )

    def test_snapshot_persists_decimal_payload_values_without_type_error(self):
        with WorkspaceTempDir() as root:
            snapshot = RunSnapshot(root / "run.sqlite3")
            snapshot.add_source("S1", pathlib.Path("copy-a.opju"), "abc")
            book = _book("S1", "Root", "Book1", "Display")
            result = _result(book)
            result = TerminalBookResult(
                source_id=result.source_id,
                folder_path=result.folder_path,
                short_name=result.short_name,
                status=result.status,
                note_text=result.note_text,
                data_sheet_name=result.data_sheet_name,
                available_columns=result.available_columns,
                selected_y_column=result.selected_y_column,
                paired_x_column=result.paired_x_column,
                selected_x_values=(Decimal("300.5"),),
                selected_y_values=(Decimal("1.25"),),
                selected_x_row_count=1,
                selected_y_row_count=1,
                max_planned_y=Decimal("1.25"),
                max_planned_y_x=Decimal("300.5"),
                s1_max_for_limit=Decimal("2.5"),
                s1_limit_status=result.s1_limit_status,
                data_checksum=result.data_checksum,
                page_type=result.page_type,
            )

            snapshot.record_book_transaction("S1", book, result)
            persisted = snapshot.book_results("S1")[0]

            self.assertEqual(("300.5",), persisted.selected_x_values)
            self.assertEqual(("1.25",), persisted.selected_y_values)
            self.assertEqual("1.25", persisted.max_planned_y)

    def test_incomplete_book_transaction_aborts_without_partial_row(self):
        with WorkspaceTempDir() as root:
            snapshot = RunSnapshot(root / "run.sqlite3")
            snapshot.add_source("S1", pathlib.Path("copy-a.opju"), "abc")
            book = _book("S1", "Root", "Book1", "Display", page_type="worksheet")
            mismatched = _result(_book("S1", "Root", "Book1", "Display", page_type="matrix"))

            with self.assertRaises(SnapshotError):
                snapshot.record_book_transaction("S1", book, mismatched)

            self.assertEqual(0, snapshot.inventory_count("S1"))
            self.assertEqual(0, snapshot.result_count("S1"))

    def test_incomplete_extracted_payload_without_checksum_aborts_reconciliation(self):
        with WorkspaceTempDir() as root:
            snapshot = RunSnapshot(root / "run.sqlite3")
            snapshot.add_source("S1", pathlib.Path("copy-a.opju"), "abc")
            book = _book("S1", "Root", "Book1", "Display")
            snapshot.record_inventory("S1", [book])
            snapshot.record_book_result(
                TerminalBookResult("S1", "Root", "Book1", "extracted", note_text="note"),
                book,
            )

            with self.assertRaises(ReconciliationError):
                snapshot.reconcile_source("S1")

    def test_incomplete_extracted_payload_with_checksum_still_aborts_reconciliation(self):
        with WorkspaceTempDir() as root:
            snapshot = RunSnapshot(root / "run.sqlite3")
            snapshot.add_source("S1", pathlib.Path("copy-a.opju"), "abc")
            book = _book("S1", "Root", "Book1", "Display")
            snapshot.record_inventory("S1", [book])
            snapshot.record_book_result(
                TerminalBookResult("S1", "Root", "Book1", "extracted", data_checksum="checksum"),
                book,
            )

            with self.assertRaises(ReconciliationError):
                snapshot.reconcile_source("S1")

    def test_streaming_worker_commits_each_book_before_next_yield(self):
        with WorkspaceTempDir() as root:
            snapshot = RunSnapshot(root / "run.sqlite3")
            first = _book("S1", "Root", "Book1", "A")
            second = _book("S1", "Root", "Book2", "B")
            factory = StreamingWorkerFactory(((first, _result(first)), (second, _result(second))), snapshot)
            manager = FakeSourceManager()

            ExtractionOrchestrator(snapshot, factory, manager).run([
                _source(root, "S1", "copy-a.opju", "aaa")
            ])

            self.assertIn(("after-yield", "S1", 1, 1), factory.events)
            self.assertIn(("after-yield", "S1", 2, 2), factory.events)
            self.assertEqual(2, snapshot.result_count("S1"))

    def test_orchestrator_passes_approved_missing_s1_option_to_early_reconciliation(self):
        with WorkspaceTempDir() as root:
            snapshot = RunSnapshot(root / "run.sqlite3")
            book = _book("S1", "Root", "Book1", "Valid without S1")
            result = replace(
                _result(book),
                available_columns=("X", "S1c"),
                column_metadata=(
                    ("A", "X", "X"),
                    ("B", "S1c", "Y"),
                ),
                s1_x_values=None,
                s1_values=None,
                s1_max_for_limit=None,
                s1_max_for_limit_x=None,
                s1_limit_status="missing_allowed",
            )
            factory = StreamingWorkerFactory(((book, result),), snapshot)

            ExtractionOrchestrator(
                snapshot,
                factory,
                FakeSourceManager(),
                s1_limit=100,
                steady_emission_y="S1c",
                allow_missing_s1=True,
            ).run([_source(root, "S1", "copy-a.opju", "aaa")])

            self.assertEqual(1, snapshot.result_count("S1"))

    def test_orchestrator_rejects_legacy_materializing_worker_without_calling_it(self):
        with WorkspaceTempDir() as root:
            snapshot = RunSnapshot(root / "run.sqlite3")
            book = _book("S1", "Root", "Book1", "A")

            class LegacyWorker:
                def __init__(self):
                    self.extract_calls = 0

                def extract(self, copy_path, allowlist):
                    self.extract_calls += 1
                    return [book], [_result(book)]

                def close(self):
                    pass

            worker = LegacyWorker()

            class LegacyFactory:
                def create(self, source_id, attempt):
                    return worker

            with self.assertRaisesRegex(WorkerPreflightError, "streaming"):
                ExtractionOrchestrator(snapshot, LegacyFactory(), FakeSourceManager()).run([
                    _source(root, "S1", "copy-a.opju", "aaa")
                ])

            self.assertEqual(0, worker.extract_calls)

    def test_streaming_worker_failure_discards_incremental_partition(self):
        with WorkspaceTempDir() as root:
            snapshot = RunSnapshot(root / "run.sqlite3")
            book = _book("S1", "Root", "Book1", "A")
            factory = StreamingWorkerFactory(((book, _result(book)),), snapshot, fail_after_first=True)
            manager = FakeSourceManager()

            with self.assertRaises(ValueError):
                ExtractionOrchestrator(snapshot, factory, manager).run([
                    _source(root, "S1", "copy-a.opju", "aaa")
                ])

            self.assertEqual(0, snapshot.inventory_count("S1"))
            self.assertEqual(0, snapshot.result_count("S1"))
            self.assertEqual([("verify_copy", "S1"), ("verify_original", "S1")], manager.calls)

    def test_successful_streaming_worker_still_verifies_when_close_fails(self):
        with WorkspaceTempDir() as root:
            snapshot = RunSnapshot(root / "run.sqlite3")
            book = _book("S1", "Root", "Book1", "A")
            factory = StreamingWorkerFactory(((book, _result(book)),), snapshot, fail_close=True)
            manager = FakeSourceManager()

            with self.assertRaisesRegex(RuntimeError, "close failed"):
                ExtractionOrchestrator(snapshot, factory, manager).run([
                    _source(root, "S1", "copy-a.opju", "aaa")
                ])

            self.assertEqual(0, snapshot.inventory_count("S1"))
            self.assertEqual(0, snapshot.result_count("S1"))
            self.assertEqual(
                [("verify_copy", "S1"), ("verify_copy", "S1"), ("verify_original", "S1")],
                manager.calls,
            )
            with closing(sqlite3.connect(root / "run.sqlite3")) as connection:
                attempts = connection.execute(
                    "select attempt, status, message from worker_attempts where source_id = 'S1' order by rowid"
                ).fetchall()
            self.assertEqual((1, "started", ""), attempts[0])
            self.assertEqual(1, attempts[1][0])
            self.assertEqual("failed", attempts[1][1])
            self.assertIn("worker close failed: close failed", attempts[1][2])

    def test_infrastructure_close_failure_retries_with_one_failed_audit_row(self):
        with WorkspaceTempDir() as root:
            snapshot = RunSnapshot(root / "run.sqlite3")
            book = _book("S1", "Root", "Book1", "A")

            class CloseRetryFactory:
                def __init__(self):
                    self.created = []
                    self.events = []

                def create(self, source_id, attempt):
                    self.created.append((source_id, attempt))
                    close_error = InfrastructureExtractionError("Origin worker close failed") if attempt == 1 else False
                    return StreamingWorker(
                        source_id,
                        ((book, _result(book)),),
                        self.events,
                        snapshot,
                        fail_close=close_error,
                    )

            factory = CloseRetryFactory()
            manager = FakeSourceManager()
            shutdown_waiter = RetryShutdownWaiter()

            ExtractionOrchestrator(
                snapshot,
                factory,
                manager,
                worker_shutdown_waiter=shutdown_waiter,
            ).run([
                _source(root, "S1", "copy-a.opju", "aaa")
            ])

            self.assertEqual([("S1", 1), ("S1", 2)], factory.created)
            self.assertEqual([("S1", 1), ("S1", 2)], shutdown_waiter.calls)
            with closing(sqlite3.connect(root / "run.sqlite3")) as connection:
                failed = connection.execute(
                    "select attempt, status, message from worker_attempts where source_id = 'S1' and status = 'failed'"
                ).fetchall()
            self.assertEqual(1, len(failed))
            self.assertEqual(1, failed[0][0])
            self.assertEqual("failed", failed[0][1])
            self.assertIn("Origin worker close failed", failed[0][2])

    def test_two_infrastructure_failures_report_both_attempts(self):
        with WorkspaceTempDir() as root:
            snapshot = RunSnapshot(root / "run.sqlite3")
            factory = FakeWorkerFactory({"S1": [([], [], True), ([], [], True)]})
            manager = FakeSourceManager()

            with self.assertRaisesRegex(
                InfrastructureExtractionError,
                r"attempt 1.*worker crashed.*attempt 2.*worker crashed",
            ):
                ExtractionOrchestrator(snapshot, factory, manager).run([
                    _source(root, "S1", "copy-a.opju", "aaa")
                ])

    def test_retry_count_is_relative_to_nondefault_first_attempt(self):
        with WorkspaceTempDir() as root:
            snapshot = RunSnapshot(root / "run.sqlite3")
            factory = FakeWorkerFactory({"S1": [([], [], True), ([], [], True)]})

            with self.assertRaisesRegex(
                InfrastructureExtractionError,
                r"attempt 2.*worker crashed.*attempt 3.*worker crashed",
            ):
                ExtractionOrchestrator(
                    snapshot,
                    factory,
                    FakeSourceManager(),
                    max_attempts=2,
                    first_attempt=2,
                ).run([_source(root, "S1", "copy-a.opju", "aaa")])

            self.assertEqual([("S1", 2), ("S1", 3)], factory.created)

    def test_orchestrator_rejects_nonpositive_attempt_configuration(self):
        with WorkspaceTempDir() as root:
            snapshot = RunSnapshot(root / "run.sqlite3")
            for kwargs in ({"max_attempts": 0}, {"first_attempt": 0}):
                with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                    ExtractionOrchestrator(snapshot, RaisingWorkerFactory(), FakeSourceManager(), **kwargs)

    def test_unconfirmed_shutdown_aborts_without_starting_retry_worker(self):
        with WorkspaceTempDir() as root:
            snapshot = RunSnapshot(root / "run.sqlite3")
            book = _book("S1", "Root", "Book1", "A")
            factory = FakeWorkerFactory({"S1": [([], [], True), ([book], [_result(book)], False)]})
            manager = FakeSourceManager()

            def shutdown_not_confirmed(source_id, attempt):
                raise RuntimeError(f"Origin for {source_id}/{attempt} is still running")

            with self.assertRaisesRegex(Exception, "shutdown was not confirmed"):
                ExtractionOrchestrator(
                    snapshot,
                    factory,
                    manager,
                    worker_shutdown_waiter=shutdown_not_confirmed,
                ).run([_source(root, "S1", "copy-a.opju", "aaa")])

            self.assertEqual([("S1", 1)], factory.created)
            self.assertNotIn(("refresh_copy", "S1"), manager.calls)

    def test_generic_worker_error_preserves_unconfirmed_shutdown(self):
        with WorkspaceTempDir() as root:
            snapshot = RunSnapshot(root / "run.sqlite3")

            def shutdown_not_confirmed(source_id, attempt):
                raise RuntimeError(f"Origin for {source_id}/{attempt} is still running")

            with self.assertRaisesRegex(WorkerShutdownUnconfirmedError, "shutdown was not confirmed"):
                ExtractionOrchestrator(
                    snapshot,
                    RaisingWorkerFactory(),
                    FakeSourceManager(),
                    worker_shutdown_waiter=shutdown_not_confirmed,
                ).run([_source(root, "S1", "copy-a.opju", "aaa")])

    def test_successful_worker_still_reports_unconfirmed_shutdown_without_self_cause(self):
        with WorkspaceTempDir() as root:
            snapshot = RunSnapshot(root / "run.sqlite3")
            book = _book("S1", "Root", "Book1", "A")
            factory = FakeWorkerFactory({"S1": [([book], [_result(book)], False)]})

            def shutdown_not_confirmed(source_id, attempt):
                raise RuntimeError(f"Origin for {source_id}/{attempt} is still running")

            with self.assertRaises(WorkerShutdownUnconfirmedError) as caught:
                ExtractionOrchestrator(
                    snapshot,
                    factory,
                    FakeSourceManager(),
                    worker_shutdown_waiter=shutdown_not_confirmed,
                ).run([_source(root, "S1", "copy-a.opju", "aaa")])

            self.assertIsNot(caught.exception, caught.exception.__cause__)
            self.assertEqual(0, snapshot.inventory_count("S1"))
            self.assertEqual(0, snapshot.result_count("S1"))

    def test_unconfirmed_shutdown_survives_original_verification_failure(self):
        with WorkspaceTempDir() as root:
            snapshot = RunSnapshot(root / "run.sqlite3")

            def shutdown_not_confirmed(source_id, attempt):
                raise RuntimeError(f"Origin for {source_id}/{attempt} is still running")

            with self.assertRaises(WorkerShutdownUnconfirmedError) as caught:
                ExtractionOrchestrator(
                    snapshot,
                    RaisingWorkerFactory(),
                    FakeSourceManager(verify_original_error=RuntimeError("source changed")),
                    worker_shutdown_waiter=shutdown_not_confirmed,
                ).run([_source(root, "S1", "copy-a.opju", "aaa")])

            self.assertIn("shutdown was not confirmed", str(caught.exception))
            self.assertTrue(
                any("original source verification failed: source changed" in note for note in caught.exception.__notes__)
            )

    def test_streaming_worker_failure_still_cleans_up_when_close_fails(self):
        with WorkspaceTempDir() as root:
            snapshot = RunSnapshot(root / "run.sqlite3")
            book = _book("S1", "Root", "Book1", "A")
            factory = StreamingWorkerFactory(((book, _result(book)),), snapshot, fail_after_first=True, fail_close=True)
            manager = FakeSourceManager()

            with self.assertRaisesRegex(ValueError, "stream interrupted"):
                ExtractionOrchestrator(snapshot, factory, manager).run([
                    _source(root, "S1", "copy-a.opju", "aaa")
                ])

            self.assertEqual(0, snapshot.inventory_count("S1"))
            self.assertEqual(0, snapshot.result_count("S1"))
            self.assertEqual([("verify_copy", "S1"), ("verify_original", "S1")], manager.calls)
            with closing(sqlite3.connect(root / "run.sqlite3")) as connection:
                attempts = connection.execute(
                    "select attempt, status, message from worker_attempts where source_id = 'S1' order by rowid"
                ).fetchall()
            self.assertEqual((1, "started", ""), attempts[0])
            self.assertEqual(1, attempts[1][0])
            self.assertEqual("failed", attempts[1][1])
            self.assertIn("stream interrupted", attempts[1][2])

    def test_orchestrator_processes_sources_serially_with_fresh_worker_per_source(self):
        with WorkspaceTempDir() as root:
            FakeWorker.active = 0
            FakeWorker.max_active = 0
            snapshot = RunSnapshot(root / "run.sqlite3")
            sources = [
                _source(root, "S1", "copy-a.opju", "aaa"),
                _source(root, "S2", "copy-b.opju", "bbb"),
            ]
            plans = {
                "S1": [([_book("S1", "Root", "Book1", "A")], [_result(_book("S1", "Root", "Book1", "A"))], False)],
                "S2": [([_book("S2", "Folder/Sub", "Book1", "A")], [_result(_book("S2", "Folder/Sub", "Book1", "A"))], False)],
            }
            factory = FakeWorkerFactory(plans)
            manager = FakeSourceManager()
            ExtractionOrchestrator(snapshot, factory, manager).run(sources)
            self.assertEqual([("S1", 1), ("S2", 1)], factory.created)
            self.assertEqual(
                [("start", "S1"), ("end", "S1"), ("close", "S1"), ("start", "S2"), ("end", "S2"), ("close", "S2")],
                factory.events,
            )
            self.assertEqual(1, FakeWorker.max_active)
            self.assertEqual(
                [
                    ("verify_copy", "S1"),
                    ("verify_copy", "S1"),
                    ("verify_original", "S1"),
                    ("verify_copy", "S2"),
                    ("verify_copy", "S2"),
                    ("verify_original", "S2"),
                ],
                manager.calls,
            )

    def test_success_verification_failure_discards_partition_and_records_attempt(self):
        with WorkspaceTempDir() as root:
            snapshot = RunSnapshot(root / "run.sqlite3")
            book = _book("S1", "Root", "Book1", "A")
            factory = FakeWorkerFactory({"S1": [([book], [_result(book)], False)]})
            manager = FakeSourceManager(verify_copy_error=RuntimeError("copy mismatch"))

            with self.assertRaisesRegex(RuntimeError, "copy mismatch"):
                ExtractionOrchestrator(snapshot, factory, manager).run([
                    _source(root, "S1", "copy-a.opju", "aaa")
                ])

            self.assertEqual(0, snapshot.inventory_count("S1"))
            self.assertEqual(0, snapshot.result_count("S1"))
            self.assertEqual([("verify_copy", "S1"), ("verify_original", "S1")], manager.calls)
            with closing(sqlite3.connect(root / "run.sqlite3")) as connection:
                attempts = connection.execute(
                    "select attempt, status, message from worker_attempts where source_id = 'S1' order by rowid"
                ).fetchall()
            self.assertEqual((1, "started", ""), attempts[0])
            self.assertEqual((1, "failed", "copy mismatch"), attempts[1])

    def test_succeeded_attempt_audit_failure_discards_reconciled_partition(self):
        with WorkspaceTempDir() as root:
            stored = RunSnapshot(root / "run.sqlite3")

            class SucceededAuditFailingSnapshot:
                def __getattr__(self, name):
                    return getattr(stored, name)

                def add_worker_attempt(self, source_id, attempt, status, message):
                    if status == "succeeded":
                        raise RuntimeError("succeeded audit failed")
                    return stored.add_worker_attempt(source_id, attempt, status, message)

            book = _book("S1", "Root", "Book1", "A")
            factory = FakeWorkerFactory({"S1": [([book], [_result(book)], False)]})

            with self.assertRaisesRegex(RuntimeError, "succeeded audit failed"):
                ExtractionOrchestrator(SucceededAuditFailingSnapshot(), factory, FakeSourceManager()).run([
                    _source(root, "S1", "copy-a.opju", "aaa")
                ])

            self.assertEqual(0, stored.inventory_count("S1"))
            self.assertEqual(0, stored.result_count("S1"))

    def test_cleanup_failure_does_not_mask_copy_verifier_error(self):
        with WorkspaceTempDir() as root:
            stored_snapshot = RunSnapshot(root / "run.sqlite3")
            snapshot = CleanupFailingSnapshot(stored_snapshot)
            book = _book("S1", "Root", "Book1", "A")
            factory = FakeWorkerFactory({"S1": [([book], [_result(book)], False)]})
            manager = FakeSourceManager(verify_copy_error=ValueError("primary copy verifier"))

            with self.assertRaisesRegex(ValueError, "primary copy verifier"):
                ExtractionOrchestrator(snapshot, factory, manager).run([
                    _source(root, "S1", "copy-a.opju", "aaa")
                ])

            self.assertEqual([("verify_copy", "S1"), ("verify_original", "S1")], manager.calls)
            with closing(sqlite3.connect(root / "run.sqlite3")) as connection:
                attempts = connection.execute(
                    "select attempt, status, message from worker_attempts where source_id = 'S1' order by rowid"
                ).fetchall()
            self.assertEqual((1, "started", ""), attempts[0])
            self.assertEqual((1, "failed", "primary copy verifier"), attempts[1])

    def test_original_verification_failure_after_extraction_failure_is_preserved(self):
        with WorkspaceTempDir() as root:
            snapshot = RunSnapshot(root / "run.sqlite3")
            factory = RaisingWorkerFactory()
            manager = FakeSourceManager(verify_original_error=RuntimeError("original changed"))

            with self.assertRaisesRegex(RuntimeError, "original changed"):
                ExtractionOrchestrator(snapshot, factory, manager).run([
                    _source(root, "S1", "copy-a.opju", "aaa")
                ])

            self.assertEqual([("close", "S1")], factory.events)
            self.assertEqual([("verify_copy", "S1"), ("verify_original", "S1")], manager.calls)
            with closing(sqlite3.connect(root / "run.sqlite3")) as connection:
                attempts = connection.execute(
                    "select attempt, status, message from worker_attempts where source_id = 'S1' order by rowid"
                ).fetchall()
            self.assertEqual((1, "started", ""), attempts[0])
            self.assertEqual(1, attempts[1][0])
            self.assertEqual("failed", attempts[1][1])
            self.assertEqual("parse bug", attempts[1][2])

    def test_unexpected_worker_error_still_closes_worker_without_retry(self):
        with WorkspaceTempDir() as root:
            snapshot = RunSnapshot(root / "run.sqlite3")
            factory = RaisingWorkerFactory()
            manager = FakeSourceManager()
            with self.assertRaises(ValueError):
                ExtractionOrchestrator(snapshot, factory, manager).run([
                    _source(root, "S1", "copy-a.opju", "aaa")
                ])
            self.assertEqual([("close", "S1")], factory.events)
            self.assertEqual([("verify_copy", "S1"), ("verify_original", "S1")], manager.calls)

    def test_orchestrator_rejects_original_or_protected_path_as_copy_path(self):
        with WorkspaceTempDir() as root:
            original = root / "original.opju"
            original.write_bytes(b"original")
            source = ExtractionSource(
                source_id="S1",
                copy_path=original,
                sha256="aaa",
                original_path=original,
                allowed_children=(original.parent,),
                protected_paths=(original,),
            )
            snapshot = RunSnapshot(root / "run.sqlite3")
            factory = FakeWorkerFactory({"S1": [([], [], False)]})
            manager = FakeSourceManager()

            with self.assertRaises(WorkerPreflightError):
                ExtractionOrchestrator(snapshot, factory, manager).run([source])

            self.assertEqual(0, snapshot.inventory_count("S1"))
            self.assertEqual(0, snapshot.result_count("S1"))

    def test_worker_creation_failure_records_attempt_and_verifies_original(self):
        with WorkspaceTempDir() as root:
            snapshot = RunSnapshot(root / "run.sqlite3")
            manager = FakeSourceManager()

            with self.assertRaisesRegex(InfrastructureExtractionError, "worker creation failed"):
                ExtractionOrchestrator(snapshot, CreateFailingWorkerFactory(), manager, max_attempts=1).run([
                    _source(root, "S1", "copy-a.opju", "aaa")
                ])

            self.assertEqual(
                [("verify_copy", "S1"), ("discard_failed_copy", "S1"), ("verify_original", "S1")],
                manager.calls,
            )
            with closing(sqlite3.connect(root / "run.sqlite3")) as connection:
                attempts = connection.execute(
                    "select attempt, status, message from worker_attempts where source_id = 'S1' order by rowid"
                ).fetchall()
            self.assertEqual((1, "started", ""), attempts[0])
            self.assertEqual((1, "failed", "worker creation failed"), attempts[1])

    def test_final_infrastructure_failure_preserves_original_error_when_close_fails(self):
        with WorkspaceTempDir() as root:
            snapshot = RunSnapshot(root / "run.sqlite3")
            factory = CloseFailingInfrastructureWorkerFactory()
            manager = FakeSourceManager()

            with self.assertRaisesRegex(InfrastructureExtractionError, "worker crashed"):
                ExtractionOrchestrator(snapshot, factory, manager, max_attempts=1).run([
                    _source(root, "S1", "copy-a.opju", "aaa")
                ])

            self.assertEqual([("start", "S1"), ("fail", "S1"), ("close", "S1")], factory.events)
            self.assertEqual(
                [("verify_copy", "S1"), ("discard_failed_copy", "S1"), ("verify_original", "S1")],
                manager.calls,
            )
            with closing(sqlite3.connect(root / "run.sqlite3")) as connection:
                attempts = connection.execute(
                    "select attempt, status, message from worker_attempts where source_id = 'S1' order by rowid"
                ).fetchall()
            self.assertEqual((1, "started", ""), attempts[0])
            self.assertEqual(1, attempts[1][0])
            self.assertEqual("failed", attempts[1][1])
            self.assertIn("worker crashed", attempts[1][2])
            self.assertIn("worker close failed: close failed", attempts[1][2])

    def test_infrastructure_failure_revalidates_discards_refreshes_and_retries_once(self):
        with WorkspaceTempDir() as root:
            snapshot = RunSnapshot(root / "run.sqlite3")
            book = _book("S1", "Root", "Book1", "A")
            factory = FakeWorkerFactory({"S1": [([], [], True), ([book], [_result(book)], False)]})
            manager = FakeSourceManager()
            shutdown_waiter = RetryShutdownWaiter()
            ExtractionOrchestrator(
                snapshot,
                factory,
                manager,
                worker_shutdown_waiter=shutdown_waiter,
            ).run([
                _source(root, "S1", "copy-a.opju", "aaa")
            ])
            self.assertEqual([("S1", 1), ("S1", 2)], factory.created)
            self.assertEqual([("S1", 1), ("S1", 2)], shutdown_waiter.calls)
            self.assertEqual(
                [
                    ("verify_copy", "S1"),
                    ("discard_failed_copy", "S1"),
                    ("verify_original", "S1"),
                    ("refresh_copy", "S1"),
                    ("verify_copy", "S1"),
                    ("verify_copy", "S1"),
                    ("verify_original", "S1"),
                ],
                manager.calls,
            )
            self.assertEqual(1, snapshot.result_count("S1"))

    def test_failed_copy_deletion_error_does_not_prevent_infrastructure_retry(self):
        with WorkspaceTempDir() as root:
            snapshot = RunSnapshot(root / "run.sqlite3")
            book = _book("S1", "Root", "Book1", "A")
            factory = FakeWorkerFactory({"S1": [([], [], True), ([book], [_result(book)], False)]})
            manager = FakeSourceManager(discard_failed_copy_error=PermissionError("copy still locked"))

            ExtractionOrchestrator(snapshot, factory, manager).run([
                _source(root, "S1", "copy-a.opju", "aaa")
            ])

            self.assertEqual([("S1", 1), ("S1", 2)], factory.created)
            self.assertEqual(1, snapshot.result_count("S1"))
            self.assertEqual(
                [
                    ("verify_copy", "S1"),
                    ("discard_failed_copy", "S1"),
                    ("verify_original", "S1"),
                    ("refresh_copy", "S1"),
                    ("verify_copy", "S1"),
                    ("verify_copy", "S1"),
                    ("verify_original", "S1"),
                ],
                manager.calls,
            )

    def test_retry_copy_path_updates_snapshot_audit(self):
        with WorkspaceTempDir() as root:
            snapshot = RunSnapshot(root / "run.sqlite3")
            book = _book("S1", "Root", "Book1", "A")
            factory = FakeWorkerFactory({"S1": [([], [], True), ([book], [_result(book)], False)]})
            retry_copy = root / "copies" / "S1" / "copy-a.retry.opju"
            retry_copy.parent.mkdir(parents=True, exist_ok=True)
            retry_copy.write_bytes(b"retry")
            manager = FakeSourceManager(refresh_copy_path=retry_copy)

            ExtractionOrchestrator(snapshot, factory, manager).run([
                _source(root, "S1", "copy-a.opju", "aaa")
            ])

            self.assertEqual(retry_copy, snapshot.source_copy_path("S1"))

    def test_failed_source_persistence_discards_partial_partition(self):
        with WorkspaceTempDir() as root:
            snapshot = RunSnapshot(root / "run.sqlite3")
            book = _book("S1", "Root", "Book1", "A")
            factory = FakeWorkerFactory({"S1": [([book], [_result(book), _result(book)], False)]})
            manager = FakeSourceManager()

            with self.assertRaises(SnapshotError):
                ExtractionOrchestrator(snapshot, factory, manager).run([
                    _source(root, "S1", "copy-a.opju", "aaa")
                ])

            self.assertEqual(0, snapshot.inventory_count("S1"))
            self.assertEqual(0, snapshot.result_count("S1"))
    def test_synthetic_inventory_scales_have_no_fixed_book_limit(self):
        with WorkspaceTempDir() as root:
            snapshot = RunSnapshot(root / "run.sqlite3")
            for count in (1, 100, 500):
                source_id = f"S{count}"
                books = [_book(source_id, f"Folder{index // 50}", f"Book{index}", f"Display {index}") for index in range(count)]
                snapshot.add_source(source_id, pathlib.Path(f"copy-{count}.opju"), str(count))
                snapshot.record_inventory(source_id, books)
                for book in books:
                    snapshot.record_book_result(_result(book), book)
                snapshot.reconcile_source(source_id)
                self.assertEqual(count, snapshot.inventory_count(source_id))

    def test_steady_2d_book_is_one_inventory_row_with_thirteen_sheets(self):
        with WorkspaceTempDir() as root:
            snapshot = RunSnapshot(root / "run.sqlite3")
            sheets = tuple(f"Sheet{i}" for i in range(13))
            book = _book("S1", "Root", "Book2D", "2D", sheets=sheets, has_data=True)
            snapshot.add_source("S1", pathlib.Path("copy-a.opju"), "abc")
            snapshot.record_inventory("S1", [book])
            rows = snapshot.inventory_rows("S1")
            self.assertEqual(1, len(rows))
            self.assertEqual(sheets, rows[0].sheet_names)


def _create_legacy_snapshot_schema(path, *, include_page_type=False):
    inventory_page_type = "page_type text," if include_page_type else ""
    result_page_type = "page_type text," if include_page_type else ""
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("create table source_files (source_id text primary key, copy_path text not null, sha256 text not null)")
        connection.execute(
            f"""
            create table inventory_rows (
                source_id text not null,
                {inventory_page_type}
                folder_path text not null,
                short_name text not null,
                display_name text not null,
                page_order integer not null,
                sheet_names_json text not null,
                has_note integer not null,
                has_data integer not null,
                primary key (source_id, folder_path, short_name)
            )
            """
        )
        connection.execute(
            f"""
            create table book_results (
                source_id text not null,
                {result_page_type}
                folder_path text not null,
                short_name text not null,
                status text not null,
                note_text text,
                rejection_reason text,
                primary key (source_id, folder_path, short_name)
            )
            """
        )
        connection.commit()

def _source(root, source_id, filename, sha256):
    source_dir = root / "copies" / source_id
    source_dir.mkdir(parents=True, exist_ok=True)
    copy_path = source_dir / filename
    copy_path.write_bytes(b"copy")
    original_dir = root / "originals" / source_id
    original_dir.mkdir(parents=True, exist_ok=True)
    original_path = original_dir / filename
    original_path.write_bytes(b"original")
    return ExtractionSource(
        source_id=source_id,
        copy_path=copy_path,
        sha256=sha256,
        original_path=original_path,
        allowed_children=(source_dir,),
        protected_paths=(original_path,),
    )

def _book(source_id, folder, short, display, sheets=("Note", "Data"), has_note=True, has_data=True, page_type="worksheet"):
    return InventoryBook(
        source_id=source_id,
        folder_path=folder,
        short_name=short,
        display_name=display,
        page_order=1,
        sheet_names=tuple(sheets),
        has_note=has_note,
        has_data=has_data,
        page_type=page_type,
    )


def _result(book, status="extracted"):
    note_text = "[EXP_FD_FILE]\nAcquisition Type = Spectral Acquisition[Emission]"
    if status != "extracted":
        if book.page_type != "worksheet":
            rejection_reason = f"unsupported Origin page type: {book.page_type}"
            rejected_note_text = None
            rejected_spectrum_class = None
            data_sheet_name = None
        elif sum(name.casefold().startswith("note") for name in book.sheet_names) > 1:
            rejection_reason = "multiple Note sheets are ambiguous"
            rejected_note_text = None
            rejected_spectrum_class = None
            data_sheet_name = "Data" if book.has_data else None
        elif not book.has_note:
            rejection_reason = "missing Note"
            rejected_note_text = None
            rejected_spectrum_class = None
            data_sheet_name = "Data" if book.has_data else None
        elif not book.has_data:
            rejection_reason = "missing Data sheet"
            rejected_note_text = note_text
            rejected_spectrum_class = None
            data_sheet_name = None
        else:
            rejection_reason = "Data read failed: simulated"
            rejected_note_text = note_text
            rejected_spectrum_class = "steady_emission"
            data_sheet_name = "Data"
        return TerminalBookResult(
            source_id=book.source_id,
            folder_path=book.folder_path,
            short_name=book.short_name,
            status=status,
            note_text=rejected_note_text,
            rejection_reason=rejection_reason,
            display_name=book.display_name,
            page_order=book.page_order,
            spectrum_class=rejected_spectrum_class,
            data_sheet_name=data_sheet_name,
            page_type=book.page_type,
        )
    return TerminalBookResult(
        source_id=book.source_id,
        folder_path=book.folder_path,
        short_name=book.short_name,
        status=status,
        note_text=note_text,
        display_name=book.display_name,
        page_order=book.page_order,
        spectrum_class="steady_emission",
        page_type=book.page_type,
        data_sheet_name="Data",
        available_columns=("X", "S1c", "S1X", "S1"),
        column_metadata=(
            ("A", "X", "X"),
            ("B", "S1c", "Y"),
            ("C", "S1X", "X"),
            ("D", "S1", "Y"),
        ),
        selected_y_column="S1c",
        paired_x_column="X",
        selected_x_values=(300.0, 301.0),
        selected_y_values=(10.0, 12.0),
        s1_x_values=(300.0, 301.0),
        s1_values=(100.0, 120.0),
        selected_x_row_count=2,
        selected_y_row_count=2,
        max_planned_y=12.0,
        max_planned_y_x=301.0,
        s1_max_for_limit=120.0,
        s1_max_for_limit_x=301.0,
        s1_limit_status="ok",
        data_checksum="checksum",
    )


if __name__ == "__main__":
    unittest.main()
