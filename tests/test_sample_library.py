from contextlib import closing, contextmanager
import hashlib
import gc
import os
import pathlib
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from spectrum_organizer.domain.models import LiquidSample, NeatSample
from spectrum_organizer.store.sample_library import (
    BackupError,
    SampleLibrary,
    SampleLibraryError,
    SampleLibraryHealth,
)
from spectrum_organizer.store import sample_library as sample_library_module


class WorkspaceTempDir:
    def __init__(self):
        self.path = pathlib.Path(tempfile.mkdtemp(prefix="spectrum-organizer-sample-library-"))

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
            raise AssertionError(f"sample-library test cleanup failed: {self.path}") from last_error


class SampleLibraryTests(unittest.TestCase):
    def test_health_check_hashing_does_not_materialize_database_files(self):
        with WorkspaceTempDir() as root:
            db = root / "sample_library.sqlite3"
            library = SampleLibrary(db, root / "backups", clock=lambda: "20260716_193000")
            library.save_final_records([LiquidSample("MFL", "mTHF", "1×10^-4 M", "298 K")])

            with patch.object(
                pathlib.Path,
                "read_bytes",
                side_effect=AssertionError("health hashing must stream files"),
            ):
                health = library.check_health()

            self.assertTrue(health.healthy)

    def test_database_content_digest_streams_rows_without_fetchall(self):
        class StreamingCursor:
            def __init__(self, cursor):
                self.cursor = cursor

            def __iter__(self):
                return iter(self.cursor)

            def fetchall(self):
                raise AssertionError("logical digest must stream rows")

        class StreamingConnection:
            def __init__(self, connection):
                self.connection = connection

            def execute(self, statement, parameters=()):
                return StreamingCursor(self.connection.execute(statement, parameters))

            def create_function(self, *args, **kwargs):
                return self.connection.create_function(*args, **kwargs)

            def set_progress_handler(self, *args, **kwargs):
                return self.connection.set_progress_handler(*args, **kwargs)

        with closing(sqlite3.connect(":memory:")) as connection:
            connection.execute("create table sample_records (value text)")
            connection.executemany(
                "insert into sample_records (value) values (?)",
                ((f"row-{index}",) for index in range(20)),
            )

            digest = sample_library_module._database_content_digest_from_connection(
                StreamingConnection(connection)
            )

        self.assertEqual(64, len(digest))

    def test_ownership_revision_translates_concurrent_path_oserror(self):
        with WorkspaceTempDir() as root:
            db = root / "sample_library.sqlite3"
            db.write_bytes(b"database")

            with patch(
                "spectrum_organizer.store.sample_library._owned_path_state",
                side_effect=PermissionError("database changed during inspection"),
            ):
                with self.assertRaisesRegex(
                    SampleLibraryError,
                    "Could not verify sample library during recovery",
                ):
                    sample_library_module._database_ownership_revision(db)

    def test_health_check_absent_is_healthy_without_creating_files_or_directories(self):
        with WorkspaceTempDir() as root:
            db = root / "data" / "sample_library.sqlite3"
            backups = root / "backups"
            health = SampleLibrary(db, backups, clock=lambda: "20260715_090000").check_health()

            self.assertTrue(health.healthy)
            self.assertEqual("absent", health.status)
            self.assertFalse(health.exists)
            self.assertFalse(db.parent.exists())
            self.assertFalse(backups.exists())

    def test_health_check_rechecks_absence_if_database_appears_during_probe(self):
        with WorkspaceTempDir() as root:
            seed = root / "seed.sqlite3"
            SampleLibrary(seed, root / "seed-backups", clock=lambda: "unused").save_final_records(
                [LiquidSample("MFL", "mTHF", "1×10^-4 M", "298 K")]
            )
            db = root / "data" / "sample_library.sqlite3"
            db.parent.mkdir()
            library = SampleLibrary(db, root / "backups", clock=lambda: "unused")
            real_exists = pathlib.Path.exists
            first_probe = True

            def appear_during_first_probe(candidate):
                nonlocal first_probe
                if candidate == db and first_probe:
                    first_probe = False
                    shutil.copy2(seed, db)
                    return False
                return real_exists(candidate)

            with patch.object(pathlib.Path, "exists", new=appear_during_first_probe):
                health = library.check_health()

            self.assertEqual("healthy", health.status)
            self.assertTrue(health.exists)

    def test_health_check_rechecks_absence_after_final_cancel_callback(self):
        with WorkspaceTempDir() as root:
            seed = root / "seed.sqlite3"
            SampleLibrary(seed, root / "seed-backups", clock=lambda: "unused").save_final_records(
                [LiquidSample("MFL", "mTHF", "1×10^-4 M", "298 K")]
            )
            db = root / "data" / "sample_library.sqlite3"
            db.parent.mkdir()
            library = SampleLibrary(db, root / "backups", clock=lambda: "unused")
            callback_count = 0

            def appear_during_final_callback():
                nonlocal callback_count
                callback_count += 1
                if callback_count == 2:
                    shutil.copy2(seed, db)

            health = library.check_health(cancel_check=appear_during_final_callback)

            self.assertGreaterEqual(callback_count, 2)
            self.assertEqual("healthy", health.status)
            self.assertTrue(health.exists)

    def test_health_check_snapshot_uses_owned_application_temp_root(self):
        with WorkspaceTempDir() as root:
            db = root / "sample_library.sqlite3"
            health_temp_root = root / "app-temp"
            health_temp_root.mkdir()
            library = SampleLibrary(
                db,
                root / "backups",
                clock=lambda: "unused",
                health_temp_root=health_temp_root,
            )
            library.save_final_records([LiquidSample("MFL", "mTHF", "1×10^-4 M", "298 K")])
            copied_targets = []
            from spectrum_organizer.store import sample_library

            real_copy = sample_library._copy_health_snapshot_component

            def record_owned_copy(source, target, *args, **kwargs):
                target = pathlib.Path(target)
                copied_targets.append(target)
                self.assertIn(health_temp_root, target.parents)
                self.assertTrue((target.parent / "ownership.json").is_file())
                return real_copy(source, target, *args, **kwargs)

            with patch(
                "spectrum_organizer.store.sample_library._copy_health_snapshot_component",
                side_effect=record_owned_copy,
            ):
                health = library.check_health()

            self.assertEqual("healthy", health.status)
            self.assertTrue(copied_targets)
            self.assertEqual(
                [".ownership-anchor.key"],
                [path.name for path in health_temp_root.iterdir()],
            )

    def test_health_check_propagates_cancellation_during_digest_work(self):
        class HealthCheckCancelled(RuntimeError):
            pass

        with WorkspaceTempDir() as root:
            db = root / "sample_library.sqlite3"
            library = SampleLibrary(db, root / "backups", clock=lambda: "unused")
            library.save_final_records([LiquidSample("MFL", "mTHF", "1×10^-4 M", "298 K")])
            calls = []

            def cancel_check():
                calls.append(True)
                if len(calls) >= 2:
                    raise HealthCheckCancelled("health check cancelled")

            with self.assertRaisesRegex(HealthCheckCancelled, "health check cancelled"):
                library.check_health(cancel_check=cancel_check)

            self.assertGreaterEqual(len(calls), 2)

    def test_health_check_propagates_cancellation_during_quick_check(self):
        class HealthCheckCancelled(RuntimeError):
            pass

        class QuickCheckConnection:
            def __init__(self):
                self.progress_handler = None
                self.executing = False

            def set_progress_handler(self, callback, steps):
                self.progress_handler = callback

            def execute(self, statement):
                self.assert_statement = statement
                self.executing = True
                try:
                    if self.progress_handler is not None and self.progress_handler():
                        raise sqlite3.OperationalError("interrupted")
                finally:
                    self.executing = False
                return self

            def fetchall(self):
                return [("ok",)]

        with WorkspaceTempDir() as root:
            db = root / "sample_library.sqlite3"
            library = SampleLibrary(db, root / "backups", clock=lambda: "unused")
            library.save_final_records([LiquidSample("MFL", "mTHF", "1×10^-4 M", "298 K")])
            connection = QuickCheckConnection()

            @contextmanager
            def connect_read_only(_path):
                yield connection

            @contextmanager
            def health_snapshot(_path, **_kwargs):
                yield db

            def cancel_check():
                if connection.executing:
                    raise HealthCheckCancelled("quick check cancelled")

            with (
                patch(
                    "spectrum_organizer.store.sample_library._physical_database_revision",
                    return_value="physical",
                ),
                patch(
                    "spectrum_organizer.store.sample_library._database_ownership_revision",
                    return_value="ownership",
                ),
                patch(
                    "spectrum_organizer.store.sample_library._health_check_snapshot",
                    side_effect=health_snapshot,
                ),
                patch(
                    "spectrum_organizer.store.sample_library._database_content_digest",
                    return_value="logical",
                ),
                patch(
                    "spectrum_organizer.store.sample_library._connect_read_only",
                    side_effect=connect_read_only,
                ),
                patch(
                    "spectrum_organizer.store.sample_library._require_compatible_schema"
                ),
            ):
                with self.assertRaisesRegex(HealthCheckCancelled, "quick check cancelled"):
                    library.check_health(cancel_check=cancel_check)

            self.assertEqual("pragma quick_check", connection.assert_statement)

    def test_health_snapshot_cleanup_failure_does_not_mask_cancellation(self):
        class HealthCheckCancelled(RuntimeError):
            pass

        with WorkspaceTempDir() as root:
            db = root / "sample_library.sqlite3"
            library = SampleLibrary(db, root / "backups", clock=lambda: "unused")
            library.save_final_records([LiquidSample("MFL", "mTHF", "1×10^-4 M", "298 K")])
            real_cleanup = sample_library_module.cleanup_owned_temp_root

            def cleanup_then_fail(run_root, **kwargs):
                real_cleanup(run_root, **kwargs)
                raise OSError("simulated cleanup failure")

            with (
                patch(
                    "spectrum_organizer.store.sample_library._database_content_digest",
                    side_effect=HealthCheckCancelled("health check cancelled"),
                ),
                patch(
                    "spectrum_organizer.store.sample_library.cleanup_owned_temp_root",
                    side_effect=cleanup_then_fail,
                ),
            ):
                with self.assertRaisesRegex(HealthCheckCancelled, "health check cancelled") as caught:
                    library.check_health()

            self.assertTrue(
                any("Could not clean owned sample-library health snapshot" in note for note in caught.exception.__notes__)
            )

    def test_health_snapshot_cleanup_failure_preserves_callback_origin_for_colliding_types(self):
        with WorkspaceTempDir() as root:
            db = root / "sample_library.sqlite3"
            library = SampleLibrary(db, root / "backups", clock=lambda: "unused")
            library.save_final_records([LiquidSample("MFL", "mTHF", "1×10^-4 M", "298 K")])
            real_cleanup = sample_library_module.cleanup_owned_temp_root

            for error_name, cancellation in (
                ("os-error", OSError("callback cancelled")),
                ("sqlite-error", sqlite3.OperationalError("callback cancelled")),
                ("sample-library-error", SampleLibraryError("callback cancelled")),
                ("keyboard-interrupt", KeyboardInterrupt("callback cancelled")),
                ("system-exit", SystemExit("callback cancelled")),
            ):
                with self.subTest(error_name=error_name):
                    callback_armed = False

                    def cancel_check():
                        if callback_armed:
                            raise cancellation

                    def digest_then_cancel(_path, *, cancel_check=None):
                        nonlocal callback_armed
                        callback_armed = True
                        cancel_check()
                        self.fail("cancellation callback did not raise")

                    def cleanup_then_fail(run_root, **kwargs):
                        real_cleanup(run_root, **kwargs)
                        raise OSError("simulated cleanup failure")

                    with (
                        patch(
                            "spectrum_organizer.store.sample_library._database_content_digest",
                            side_effect=digest_then_cancel,
                        ),
                        patch(
                            "spectrum_organizer.store.sample_library.cleanup_owned_temp_root",
                            side_effect=cleanup_then_fail,
                        ),
                    ):
                        try:
                            library.check_health(cancel_check=cancel_check)
                        except type(cancellation) as caught:
                            self.assertIs(cancellation, caught)
                            frames = traceback.extract_tb(caught.__traceback__)
                            self.assertEqual("cancel_check", frames[-1].name)
                            self.assertEqual(
                                pathlib.Path(__file__).resolve(),
                                pathlib.Path(frames[-1].filename).resolve(),
                            )
                            self.assertTrue(
                                any(
                                    "Could not clean owned sample-library health snapshot" in note
                                    for note in caught.__notes__
                                )
                            )
                        else:
                            self.fail("cancellation callback did not propagate")

    def test_healthy_database_snapshot_cleanup_failure_is_not_recoverable_corruption(self):
        with WorkspaceTempDir() as root:
            db = root / "sample_library.sqlite3"
            library = SampleLibrary(db, root / "backups", clock=lambda: "unused")
            library.save_final_records([LiquidSample("MFL", "mTHF", "1×10^-4 M", "298 K")])
            real_cleanup = sample_library_module.cleanup_owned_temp_root

            def cleanup_then_fail(run_root, **kwargs):
                real_cleanup(run_root, **kwargs)
                raise OSError("simulated cleanup failure")

            with patch(
                "spectrum_organizer.store.sample_library.cleanup_owned_temp_root",
                side_effect=cleanup_then_fail,
            ):
                health = library.check_health()

            self.assertEqual("health-check-failed", health.status)
            self.assertFalse(health.healthy)
            self.assertIsNone(health.revision)
            with closing(sqlite3.connect(db)) as connection:
                self.assertEqual(1, connection.execute("select count(*) from sample_records").fetchone()[0])

    def test_snapshot_cleanup_failure_overrides_non_cancellation_inspection_error(self):
        with WorkspaceTempDir() as root:
            db = root / "sample_library.sqlite3"
            library = SampleLibrary(db, root / "backups", clock=lambda: "unused")
            library.save_final_records([LiquidSample("MFL", "mTHF", "1×10^-4 M", "298 K")])
            real_cleanup = sample_library_module.cleanup_owned_temp_root

            def cleanup_then_fail(run_root, **kwargs):
                real_cleanup(run_root, **kwargs)
                raise OSError("simulated cleanup failure")

            with (
                patch(
                    "spectrum_organizer.store.sample_library._run_quick_check",
                    side_effect=sqlite3.OperationalError("simulated inspection failure"),
                ),
                patch(
                    "spectrum_organizer.store.sample_library.cleanup_owned_temp_root",
                    side_effect=cleanup_then_fail,
                ),
            ):
                health = library.check_health()

            self.assertEqual("health-check-failed", health.status)
            self.assertFalse(health.healthy)
            self.assertIsNone(health.revision)
            self.assertIn("Could not clean owned sample-library health snapshot", health.detail)
            with closing(sqlite3.connect(db)) as connection:
                self.assertEqual(1, connection.execute("select count(*) from sample_records").fetchone()[0])

    def test_health_check_healthy_database_does_not_change_hash_or_mtime(self):
        with WorkspaceTempDir() as root:
            db = root / "sample_library.sqlite3"
            library = SampleLibrary(db, root / "backups", clock=lambda: "20260715_090000")
            library.save_final_records([LiquidSample("MFL", "mTHF", "1×10^-4 M", "298 K")])
            before_hash = _file_hash(db)
            before_mtime = db.stat().st_mtime_ns

            health = library.check_health()

            self.assertTrue(health.healthy)
            self.assertEqual("healthy", health.status)
            self.assertTrue(health.exists)
            self.assertEqual(before_hash, _file_hash(db))
            self.assertEqual(before_mtime, db.stat().st_mtime_ns)

    def test_health_check_detects_exclusive_lock_without_modifying_database(self):
        with WorkspaceTempDir() as root:
            db = root / "sample_library.sqlite3"
            library = SampleLibrary(db, root / "backups", clock=lambda: "20260716_120000")
            library.save_final_records([LiquidSample("MFL", "mTHF", "1×10^-4 M", "298 K")])
            before_hash = _file_hash(db)
            before_mtime = db.stat().st_mtime_ns

            connection = sqlite3.connect(db, timeout=0, isolation_level=None)
            try:
                connection.execute("begin exclusive")
                health = library.check_health()
            finally:
                connection.rollback()
                connection.close()

            self.assertFalse(health.healthy)
            self.assertEqual("locked", health.status)
            self.assertEqual(before_hash, _file_hash(db))
            self.assertEqual(before_mtime, db.stat().st_mtime_ns)

    @unittest.skipUnless(os.name == "nt", "Windows SQLite byte-range locking contract")
    def test_health_check_holds_shared_lock_through_snapshot_copy(self):
        with WorkspaceTempDir() as root:
            db = root / "sample_library.sqlite3"
            library = SampleLibrary(db, root / "backups", clock=lambda: "20260716_121000")
            library.save_final_records([LiquidSample("MFL", "mTHF", "1×10^-4 M", "298 K")])
            real_snapshot = sample_library_module._health_check_snapshot
            writer_outcomes = []

            @contextmanager
            def snapshot_with_competing_writer(path, **kwargs):
                with closing(sqlite3.connect(db, timeout=0, isolation_level=None)) as writer:
                    try:
                        writer.execute("begin exclusive")
                    except sqlite3.OperationalError:
                        writer_outcomes.append("blocked")
                    else:
                        writer_outcomes.append("acquired")
                        writer.rollback()
                with real_snapshot(path, **kwargs) as snapshot_path:
                    yield snapshot_path

            with patch(
                "spectrum_organizer.store.sample_library._health_check_snapshot",
                new=snapshot_with_competing_writer,
            ):
                health = library.check_health()

            self.assertTrue(health.healthy, health.detail)
            self.assertEqual(["blocked"], writer_outcomes)

    def test_health_check_wal_database_does_not_create_or_change_canonical_sidecars(self):
        with WorkspaceTempDir() as root:
            db = root / "sample_library.sqlite3"
            subprocess.run(
                [
                    sys.executable,
                    "-c",
                    """
import os
import sqlite3
import sys

connection = sqlite3.connect(sys.argv[1])
connection.execute("pragma journal_mode=wal")
connection.execute("pragma wal_autocheckpoint=0")
connection.execute('''
    create table sample_records (
        id integer primary key,
        sample_type text not null,
        identity_json text not null unique,
        canonical_label text not null,
        system_label text not null,
        created_order integer not null default 0
    )
''')
connection.commit()
connection.execute("pragma wal_checkpoint(truncate)")
connection.execute(
    "insert into sample_records "
    "(sample_type, identity_json, canonical_label, system_label) values (?, ?, ?, ?)",
    ("liquid", "committed-in-wal", "MFL / mTHF", "MFL / mTHF / 298 K"),
)
connection.commit()
os._exit(0)
""",
                    str(db),
                ],
                check=True,
            )
            wal = pathlib.Path(f"{db}-wal")
            shm = pathlib.Path(f"{db}-shm")
            self.assertTrue(wal.exists())
            shm.unlink()
            before = {
                path.name: (_file_hash(path), path.stat().st_mtime_ns)
                for path in (db, wal)
            }

            health = SampleLibrary(
                db,
                root / "backups",
                clock=lambda: "20260715_090000",
            ).check_health()

            self.assertTrue(health.healthy, health.detail)
            self.assertEqual(
                before,
                {
                    path.name: (_file_hash(path), path.stat().st_mtime_ns)
                    for path in root.glob(f"{db.name}*")
                },
            )

    def test_health_check_detects_corrupt_database(self):
        with WorkspaceTempDir() as root:
            db = root / "sample_library.sqlite3"
            db.write_bytes(b"not a sqlite database")

            health = SampleLibrary(db, root / "backups", clock=lambda: "20260715_090000").check_health()

            self.assertFalse(health.healthy)
            self.assertEqual("corrupt", health.status)
            self.assertTrue(health.exists)

    def test_health_check_reports_corrupt_database_with_sidecar(self):
        with WorkspaceTempDir() as root:
            db = root / "sample_library.sqlite3"
            db.write_bytes(b"not a sqlite database")
            pathlib.Path(f"{db}-wal").write_bytes(b"")

            health = SampleLibrary(db, root / "backups", clock=lambda: "20260716_140000").check_health()

            self.assertFalse(health.healthy)
            self.assertEqual("corrupt", health.status)
            self.assertIsNotNone(health.revision)

    def test_health_check_bounds_unreadable_physical_sidecars(self):
        for main_exists in (True, False):
            with self.subTest(main_exists=main_exists), WorkspaceTempDir() as root:
                db = root / "sample_library.sqlite3"
                wal = pathlib.Path(f"{db}-wal")
                if main_exists:
                    db.write_bytes(b"not a sqlite database")
                wal.write_bytes(b"physical evidence")
                original_open = pathlib.Path.open

                def fail_sidecar_read(path, *args, **kwargs):
                    if path == wal:
                        raise OSError("sidecar unreadable")
                    return original_open(path, *args, **kwargs)

                with patch("pathlib.Path.open", autospec=True, side_effect=fail_sidecar_read):
                    health = SampleLibrary(
                        db,
                        root / "backups",
                        clock=lambda: "20260716_150050",
                    ).check_health()

                self.assertFalse(health.healthy)
                self.assertEqual("unreadable", health.status)
                self.assertIsNone(health.revision)

    def test_recovery_preserves_corrupt_database_and_sidecars_before_rebuild(self):
        with WorkspaceTempDir() as root:
            db = root / "sample_library.sqlite3"
            wal = pathlib.Path(f"{db}-wal")
            db.write_bytes(b"not a sqlite database")
            wal.write_bytes(b"corrupt wal evidence")
            library = SampleLibrary(db, root / "backups", clock=lambda: "20260716_150000")
            health = library.check_health()

            backup = library.recover(expected_revision=health.revision)

            self.assertEqual(b"not a sqlite database", backup.read_bytes())
            self.assertEqual(b"corrupt wal evidence", pathlib.Path(f"{backup}-wal").read_bytes())
            self.assertTrue(library.check_health().healthy)
            self.assertFalse(wal.exists())

    def test_orphan_sidecar_is_unhealthy_and_can_be_preserved_before_rebuild(self):
        with WorkspaceTempDir() as root:
            db = root / "sample_library.sqlite3"
            wal = pathlib.Path(f"{db}-wal")
            wal.write_bytes(b"orphan wal evidence")
            library = SampleLibrary(db, root / "backups", clock=lambda: "20260716_150100")

            health = library.check_health()

            self.assertFalse(health.healthy)
            self.assertEqual("corrupt", health.status)
            self.assertTrue(health.exists)
            self.assertIsNotNone(health.revision)

            backup = library.recover(expected_revision=health.revision)

            self.assertFalse(backup.exists())
            self.assertEqual(b"orphan wal evidence", pathlib.Path(f"{backup}-wal").read_bytes())
            self.assertTrue(library.check_health().healthy)
            self.assertFalse(wal.exists())

    def test_orphan_recovery_rejects_byte_identical_replacement_after_health_confirmation(self):
        with WorkspaceTempDir() as root:
            db = root / "sample_library.sqlite3"
            wal = pathlib.Path(f"{db}-wal")
            wal.write_bytes(b"approved orphan wal")
            library = SampleLibrary(db, root / "backups", clock=lambda: "20260716_213000")
            health = library.check_health()
            replacement = root / "byte-identical-orphan-wal"
            shutil.copy2(wal, replacement)
            os.replace(replacement, wal)

            with self.assertRaisesRegex(SampleLibraryError, "changed after health check"):
                library.recover(expected_revision=health.revision)

            self.assertFalse(db.exists())
            self.assertEqual(b"approved orphan wal", wal.read_bytes())

    def test_orphan_sidecar_recovery_does_not_overwrite_concurrent_new_state(self):
        with WorkspaceTempDir() as root:
            db = root / "sample_library.sqlite3"
            wal = pathlib.Path(f"{db}-wal")
            wal.write_bytes(b"approved orphan wal")
            library = SampleLibrary(db, root / "backups", clock=lambda: "20260716_150200")
            health = library.check_health()

            def create_concurrent_database(_source, _target):
                db.write_bytes(b"concurrent main")
                wal.write_bytes(b"concurrent wal")
                raise FileExistsError("concurrent database appeared")

            with patch("spectrum_organizer.store.sample_library.os.link", side_effect=create_concurrent_database):
                with self.assertRaises(SampleLibraryError):
                    library.recover(expected_revision=health.revision)

            self.assertEqual(b"concurrent main", db.read_bytes())
            self.assertEqual(b"concurrent wal", wal.read_bytes())

    def test_orphan_sidecar_recovery_revalidates_revision_after_detachment(self):
        with WorkspaceTempDir() as root:
            db = root / "sample_library.sqlite3"
            wal = pathlib.Path(f"{db}-wal")
            wal.write_bytes(b"approved orphan wal")
            library = SampleLibrary(db, root / "backups", clock=lambda: "20260716_150250")
            health = library.check_health()
            real_detach = sample_library_module._detach_database_sidecars
            staged_path = None

            def detach_then_change_staged_state(path):
                nonlocal staged_path
                detached = real_detach(path)
                staged_path = pathlib.Path(detached[0][1])
                staged_path.write_bytes(b"concurrent orphan wal")
                return detached

            with patch(
                "spectrum_organizer.store.sample_library._detach_database_sidecars",
                side_effect=detach_then_change_staged_state,
            ):
                with self.assertRaisesRegex(SampleLibraryError, "changed during recovery"):
                    library.recover(expected_revision=health.revision)

            self.assertFalse(db.exists())
            self.assertFalse(wal.exists())
            self.assertIsNotNone(staged_path)
            self.assertTrue(staged_path.exists())
            self.assertEqual(b"concurrent orphan wal", staged_path.read_bytes())

    def test_orphan_recovery_preserves_staging_replacement_after_revision_validation(self):
        with WorkspaceTempDir() as root:
            db = root / "sample_library.sqlite3"
            wal = pathlib.Path(f"{db}-wal")
            wal.write_bytes(b"approved orphan wal")
            library = SampleLibrary(db, root / "backups", clock=lambda: "20260716_150260")
            health = library.check_health()
            real_revision = sample_library_module._physical_database_revision_with_staged_sidecars
            staged_path = None

            def revision_then_replace_staging(path, staged_sidecars):
                nonlocal staged_path
                revision = real_revision(path, staged_sidecars)
                if staged_sidecars and staged_path is None:
                    staged_path = pathlib.Path(staged_sidecars[0][1])
                    staged_path.unlink()
                    staged_path.write_bytes(b"concurrent staging replacement")
                return revision

            with patch(
                "spectrum_organizer.store.sample_library._physical_database_revision_with_staged_sidecars",
                side_effect=revision_then_replace_staging,
            ):
                with self.assertRaisesRegex(SampleLibraryError, "changed|retained"):
                    library.recover(expected_revision=health.revision)

            self.assertFalse(db.exists())
            self.assertIsNotNone(staged_path)
            self.assertTrue(staged_path.exists())
            self.assertEqual(b"concurrent staging replacement", staged_path.read_bytes())

    @unittest.skipUnless(os.name == "nt", "Windows transient sidecar detachment contract")
    def test_sidecar_detachment_retry_state_change_cleans_placeholder(self):
        with WorkspaceTempDir() as root:
            db = root / "sample_library.sqlite3"
            wal = pathlib.Path(f"{db}-wal")
            wal.write_bytes(b"approved orphan wal")
            real_replace = os.replace
            failed_once = False

            def transient_failure_then_change(source, target):
                nonlocal failed_once
                if pathlib.Path(source) == wal and not failed_once:
                    failed_once = True
                    wal.write_bytes(b"changed orphan wal")
                    error = OSError("transient sidecar detachment failure")
                    error.winerror = 5
                    raise error
                return real_replace(source, target)

            with patch(
                "spectrum_organizer.store.sample_library.os.replace",
                side_effect=transient_failure_then_change,
            ):
                with self.assertRaisesRegex(SampleLibraryError, "changed while Windows was retrying"):
                    sample_library_module._detach_database_sidecars(db)

            self.assertEqual(b"changed orphan wal", wal.read_bytes())
            self.assertEqual([], list(root.glob(f".{wal.name}.*.tmp")))

    def test_orphan_sidecar_recovery_rejects_sidecar_created_during_main_publication(self):
        with WorkspaceTempDir() as root:
            db = root / "sample_library.sqlite3"
            wal = pathlib.Path(f"{db}-wal")
            wal.write_bytes(b"approved orphan wal")
            library = SampleLibrary(db, root / "backups", clock=lambda: "20260716_150275")
            health = library.check_health()
            real_link = os.link

            def publish_then_create_sidecar(source, target):
                result = real_link(source, target)
                if pathlib.Path(target) == db:
                    wal.write_bytes(b"concurrent new wal")
                return result

            with patch(
                "spectrum_organizer.store.sample_library.os.link",
                side_effect=publish_then_create_sidecar,
            ):
                with self.assertRaisesRegex(SampleLibraryError, "sidecar.*appeared"):
                    library.recover(expected_revision=health.revision)

            self.assertFalse(db.exists())
            self.assertEqual(b"concurrent new wal", wal.read_bytes())

    def test_orphan_publication_identity_failure_rolls_back_linked_main(self):
        with WorkspaceTempDir() as root:
            db = root / "sample_library.sqlite3"
            wal = pathlib.Path(f"{db}-wal")
            wal.write_bytes(b"approved orphan wal")
            library = SampleLibrary(db, root / "backups", clock=lambda: "20260716_150280")
            health = library.check_health()
            real_identity = sample_library_module._owned_path_identity

            def fail_main_identity(path):
                if pathlib.Path(path) == db:
                    raise PermissionError("published identity unavailable")
                return real_identity(path)

            with patch(
                "spectrum_organizer.store.sample_library._owned_path_identity",
                side_effect=fail_main_identity,
            ):
                with self.assertRaisesRegex(SampleLibraryError, "published identity unavailable"):
                    library.recover(expected_revision=health.revision)

            self.assertFalse(db.exists())
            self.assertEqual(b"approved orphan wal", wal.read_bytes())

    def test_orphan_recovery_reports_replacement_temp_cleanup_residue(self):
        with WorkspaceTempDir() as root:
            db = root / "sample_library.sqlite3"
            wal = pathlib.Path(f"{db}-wal")
            wal.write_bytes(b"approved orphan wal")
            library = SampleLibrary(db, root / "backups", clock=lambda: "20260716_150285")
            health = library.check_health()
            original_unlink = sample_library_module._unlink_if_owned_identity_matches

            def deny_replacement_temp_cleanup(path, identity):
                if path.parent == root and path.name.startswith(f".{db.name}.") and path.suffix == ".tmp":
                    raise PermissionError("replacement temp cleanup denied")
                return original_unlink(path, identity)

            with patch(
                "spectrum_organizer.store.sample_library._unlink_if_owned_identity_matches",
                side_effect=deny_replacement_temp_cleanup,
            ):
                with self.assertRaisesRegex(SampleLibraryError, "cleanup incomplete"):
                    library.recover(expected_revision=health.revision)

            self.assertEqual(1, len(list(root.glob(f".{db.name}.*.tmp"))))

    def test_physical_backup_copy_failure_removes_partial_sidecar_artifact(self):
        with WorkspaceTempDir() as root:
            db = root / "sample_library.sqlite3"
            wal = pathlib.Path(f"{db}-wal")
            db.write_bytes(b"not a sqlite database")
            wal.write_bytes(b"approved wal")
            target = root / "backups" / "sample_library_fixed.sqlite3"

            def partially_copy(source, destination):
                destination = pathlib.Path(destination)
                if pathlib.Path(source) == wal:
                    destination.write_bytes(b"partial")
                    raise OSError("sidecar copy interrupted")
                shutil.copy2(source, destination)

            library = SampleLibrary(
                db,
                target.parent,
                clock=lambda: "unused",
                copy_file=partially_copy,
            )
            with self.assertRaises(BackupError):
                library._backup_existing_database(target, force_physical=True)

            self.assertFalse(target.exists())
            self.assertFalse(pathlib.Path(f"{target}-wal").exists())

    def test_backup_name_collision_preserves_existing_backup_and_sidecars(self):
        with WorkspaceTempDir() as root:
            db = root / "sample_library.sqlite3"
            db.write_bytes(b"new damaged database")
            pathlib.Path(f"{db}-wal").write_bytes(b"new wal")
            target = root / "backups" / "sample_library_fixed.sqlite3"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"existing backup")
            target_sidecars = tuple(pathlib.Path(f"{target}{suffix}") for suffix in ("-wal", "-shm", "-journal"))
            for index, sidecar in enumerate(target_sidecars):
                sidecar.write_bytes(f"existing sidecar {index}".encode())
            before = {path: path.read_bytes() for path in (target, *target_sidecars)}

            library = SampleLibrary(db, target.parent, clock=lambda: "unused")
            with self.assertRaisesRegex(BackupError, "Backup already exists"):
                library._backup_existing_database(target, force_physical=True)

            self.assertEqual(before, {path: path.read_bytes() for path in before})

    def test_partial_backup_cleanup_preserves_concurrently_replaced_component(self):
        with WorkspaceTempDir() as root:
            db = root / "sample_library.sqlite3"
            wal = pathlib.Path(f"{db}-wal")
            db.write_bytes(b"damaged database")
            wal.write_bytes(b"approved wal")
            target = root / "backups" / "sample_library_fixed.sqlite3"
            target_wal = pathlib.Path(f"{target}-wal")
            real_link = os.link

            def replace_published_wal_before_main_collision(source, destination):
                destination = pathlib.Path(destination)
                if destination == target_wal:
                    return real_link(source, destination)
                if destination == target:
                    target_wal.unlink()
                    target_wal.write_bytes(b"concurrent replacement")
                    target.write_bytes(b"concurrent main")
                    raise FileExistsError("concurrent backup appeared")
                return real_link(source, destination)

            library = SampleLibrary(db, target.parent, clock=lambda: "unused")
            with patch(
                "spectrum_organizer.store.sample_library.os.link",
                side_effect=replace_published_wal_before_main_collision,
            ):
                with self.assertRaises(BackupError):
                    library._backup_existing_database(target, force_physical=True)

            self.assertEqual(b"concurrent main", target.read_bytes())
            self.assertEqual(b"concurrent replacement", target_wal.read_bytes())

    def test_partial_backup_cleanup_preserves_concurrently_replaced_staging_path(self):
        with WorkspaceTempDir() as root:
            db = root / "sample_library.sqlite3"
            wal = pathlib.Path(f"{db}-wal")
            db.write_bytes(b"damaged database")
            wal.write_bytes(b"approved wal")
            target = root / "backups" / "sample_library_fixed.sqlite3"
            target_wal = pathlib.Path(f"{target}-wal")
            real_link = os.link
            staged_wal = None

            def replace_staging_path_before_main_collision(source, destination):
                nonlocal staged_wal
                source = pathlib.Path(source)
                destination = pathlib.Path(destination)
                if destination == target_wal:
                    staged_wal = source
                    return real_link(source, destination)
                if destination == target:
                    staged_wal.unlink()
                    staged_wal.write_bytes(b"concurrent staging replacement")
                    target.write_bytes(b"concurrent main")
                    raise FileExistsError("concurrent backup appeared")
                return real_link(source, destination)

            library = SampleLibrary(db, target.parent, clock=lambda: "unused")
            with patch(
                "spectrum_organizer.store.sample_library.os.link",
                side_effect=replace_staging_path_before_main_collision,
            ):
                with self.assertRaisesRegex(BackupError, "cleanup incomplete"):
                    library._backup_existing_database(target, force_physical=True)

            self.assertIsNotNone(staged_wal)
            self.assertEqual(b"concurrent staging replacement", staged_wal.read_bytes())

    def test_partial_backup_cleanup_delegates_to_identity_bound_unlink(self):
        with WorkspaceTempDir() as root:
            target = root / "published.sqlite3"
            target.write_bytes(b"published")
            approved = sample_library_module._owned_path_identity(target)

            with patch(
                "spectrum_organizer.store.sample_library._unlink_if_owned_identity_matches",
                return_value=False,
            ) as guarded_unlink:
                residues = sample_library_module._cleanup_published_paths([(target, approved)])

            guarded_unlink.assert_called_once_with(target, approved)
            self.assertEqual((target,), residues)
            self.assertEqual(b"published", target.read_bytes())

    def test_backup_publication_identity_failure_rolls_back_linked_component(self):
        with WorkspaceTempDir() as root:
            db = root / "sample_library.sqlite3"
            db.write_bytes(b"damaged database")
            target = root / "backups" / "sample_library_fixed.sqlite3"
            real_identity = sample_library_module._owned_path_identity

            def fail_final_identity(path):
                if pathlib.Path(path) == target:
                    raise PermissionError("published identity unavailable")
                return real_identity(path)

            library = SampleLibrary(db, target.parent, clock=lambda: "unused")
            with patch(
                "spectrum_organizer.store.sample_library._owned_path_identity",
                side_effect=fail_final_identity,
            ):
                with self.assertRaisesRegex(BackupError, "published identity unavailable"):
                    library._backup_existing_database(target, force_physical=True)

            self.assertFalse(target.exists())

    def test_backup_publication_rejects_same_metadata_content_change(self):
        with WorkspaceTempDir() as root:
            db = root / "sample_library.sqlite3"
            db.write_bytes(b"approved damaged database")
            target = root / "backups" / "sample_library_fixed.sqlite3"
            real_link = os.link
            changed_staging = None

            def change_staging_then_publish(source, destination):
                nonlocal changed_staging
                source = pathlib.Path(source)
                destination = pathlib.Path(destination)
                if destination == target:
                    changed_staging = source
                    stat = source.stat()
                    source.write_bytes(b"X" * stat.st_size)
                    os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns))
                return real_link(source, destination)

            library = SampleLibrary(db, target.parent, clock=lambda: "unused")
            with patch(
                "spectrum_organizer.store.sample_library.os.link",
                side_effect=change_staging_then_publish,
            ):
                with self.assertRaisesRegex(BackupError, "changed|cleanup incomplete"):
                    library._backup_existing_database(target, force_physical=True)

            self.assertIsNotNone(changed_staging)
            self.assertTrue(changed_staging.exists())
            self.assertTrue(target.exists())
            self.assertEqual(b"X" * len(b"approved damaged database"), target.read_bytes())

    def test_failed_physical_backup_reports_owned_staging_cleanup_residue(self):
        with WorkspaceTempDir() as root:
            db = root / "sample_library.sqlite3"
            wal = pathlib.Path(f"{db}-wal")
            db.write_bytes(b"not a sqlite database")
            wal.write_bytes(b"approved wal")
            target = root / "backups" / "sample_library_fixed.sqlite3"
            original_unlink = sample_library_module._unlink_if_owned_identity_matches

            def fail_sidecar_copy(source, destination):
                destination = pathlib.Path(destination)
                if pathlib.Path(source) == wal:
                    destination.write_bytes(b"partial staged sidecar")
                    raise OSError("sidecar copy interrupted")
                shutil.copy2(source, destination)

            def block_owned_temp_cleanup(path, identity):
                if path.parent == target.parent and path.name.startswith(f".{target.name}."):
                    raise PermissionError("owned staging cleanup blocked")
                return original_unlink(path, identity)

            library = SampleLibrary(
                db,
                target.parent,
                clock=lambda: "unused",
                copy_file=fail_sidecar_copy,
            )
            with patch(
                "spectrum_organizer.store.sample_library._unlink_if_owned_identity_matches",
                side_effect=block_owned_temp_cleanup,
            ):
                with self.assertRaisesRegex(BackupError, "cleanup incomplete"):
                    library._backup_existing_database(target, force_physical=True)

            self.assertFalse(target.exists())
            target_sidecars = tuple(pathlib.Path(f"{target}{suffix}") for suffix in ("-wal", "-shm", "-journal"))
            self.assertTrue(all(not sidecar.exists() for sidecar in target_sidecars))

    def test_corrupt_recovery_rejects_wal_change_before_sidecar_detach(self):
        with WorkspaceTempDir() as root:
            db = root / "sample_library.sqlite3"
            wal = pathlib.Path(f"{db}-wal")
            db.write_bytes(b"not a sqlite database")
            wal.write_bytes(b"approved wal")
            library = SampleLibrary(db, root / "backups", clock=lambda: "20260716_150300")
            health = library.check_health()
            real_detach = sample_library_module._detach_database_sidecars

            def update_then_detach(path):
                wal.write_bytes(b"new wal after approval")
                return real_detach(path)

            with patch(
                "spectrum_organizer.store.sample_library._detach_database_sidecars",
                side_effect=update_then_detach,
            ):
                with self.assertRaisesRegex(SampleLibraryError, "changed during recovery"):
                    library.recover(expected_revision=health.revision)

            self.assertEqual(b"not a sqlite database", db.read_bytes())
            self.assertEqual(b"new wal after approval", wal.read_bytes())

    @unittest.skipUnless(os.name == "nt", "Windows transient sidecar access contract")
    def test_corrupt_recovery_retries_transient_sidecar_access_denial(self):
        with WorkspaceTempDir() as root:
            db = root / "sample_library.sqlite3"
            wal = pathlib.Path(f"{db}-wal")
            db.write_bytes(b"not a sqlite database")
            wal.write_bytes(b"approved wal")
            library = SampleLibrary(db, root / "backups", clock=lambda: "20260716_151000")
            health = library.check_health()
            real_replace = os.replace
            failed_once = False

            def deny_first_wal_detach(source, target):
                nonlocal failed_once
                if pathlib.Path(source) == wal and not failed_once:
                    failed_once = True
                    error = PermissionError("transient sidecar access denial")
                    error.winerror = 5
                    raise error
                return real_replace(source, target)

            with patch(
                "spectrum_organizer.store.sample_library.os.replace",
                side_effect=deny_first_wal_detach,
            ):
                library.recover(expected_revision=health.revision)

            self.assertTrue(failed_once)
            self.assertTrue(library.check_health().healthy)

    @unittest.skipUnless(os.name == "nt", "Windows transient main detachment contract")
    def test_corrupt_recovery_retries_transient_main_detachment_after_identity_recheck(self):
        with WorkspaceTempDir() as root:
            db = root / "sample_library.sqlite3"
            db.write_bytes(b"not a sqlite database")
            library = SampleLibrary(db, root / "backups", clock=lambda: "20260716_151100")
            real_replace = os.replace
            attempts = 0

            def fail_once(source, target):
                nonlocal attempts
                if pathlib.Path(source) == db:
                    attempts += 1
                if pathlib.Path(source) == db and attempts == 1:
                    error = OSError("transient main detachment failure")
                    error.winerror = 1175
                    raise error
                return real_replace(source, target)

            with patch(
                "spectrum_organizer.store.sample_library.os.replace",
                side_effect=fail_once,
            ):
                library.recover()

            self.assertGreaterEqual(attempts, 2)
            self.assertLessEqual(attempts, 4)
            self.assertTrue(library.check_health().healthy)

    @unittest.skipUnless(os.name == "nt", "Windows retry identity contract")
    def test_windows_retry_revalidates_identity_after_delay_before_second_attempt(self):
        attempts = 0
        unchanged = True

        def fail_first_attempt():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                error = PermissionError("transient access denial")
                error.winerror = 5
                raise error

        def change_identity_during_delay(_delay):
            nonlocal unchanged
            unchanged = False

        with patch(
            "spectrum_organizer.store.sample_library.time.sleep",
            side_effect=change_identity_during_delay,
        ):
            with self.assertRaisesRegex(SampleLibraryError, "changed during retry"):
                sample_library_module._retry_transient_windows_file_operation(
                    fail_first_attempt,
                    unchanged=lambda: unchanged,
                    changed_message="Sample library changed during retry",
                )

        self.assertEqual(1, attempts)

    def test_health_check_detects_unreadable_database_path(self):
        with WorkspaceTempDir() as root:
            db = root / "sample_library.sqlite3"
            db.mkdir()

            health = SampleLibrary(db, root / "backups", clock=lambda: "20260715_090000").check_health()

            self.assertFalse(health.healthy)
            self.assertEqual("unreadable", health.status)
            self.assertTrue(health.exists)

    def test_health_check_detects_incompatible_schema(self):
        with WorkspaceTempDir() as root:
            db = root / "sample_library.sqlite3"
            with closing(sqlite3.connect(db)) as connection:
                connection.execute("create table something_else (id integer primary key)")
                connection.commit()

            health = SampleLibrary(db, root / "backups", clock=lambda: "20260715_090000").check_health()

            self.assertFalse(health.healthy)
            self.assertEqual("schema-incompatible", health.status)
            self.assertTrue(health.exists)

    def test_health_check_rejects_incompatible_column_types_and_constraints(self):
        valid_schema = """
            create table sample_records (
                id integer primary key,
                sample_type text not null,
                identity_json text not null unique,
                canonical_label text not null,
                system_label text not null,
                created_order integer not null default 0
            )
        """
        cases = {
            "id type": valid_schema.replace("id integer primary key", "id text primary key"),
            "primary key": valid_schema.replace("id integer primary key", "id integer not null"),
            "not null": valid_schema.replace("sample_type text not null", "sample_type text"),
            "created_order type": valid_schema.replace(
                "created_order integer not null default 0",
                "created_order text not null default 0",
            ),
            "created_order default": valid_schema.replace(
                "created_order integer not null default 0",
                "created_order integer not null default 1",
            ),
        }

        for label, schema in cases.items():
            with self.subTest(label=label), WorkspaceTempDir() as root:
                db = root / "sample_library.sqlite3"
                with closing(sqlite3.connect(db)) as connection:
                    connection.execute(schema)
                    connection.commit()
                before_hash = _file_hash(db)
                before_mtime = db.stat().st_mtime_ns

                health = SampleLibrary(db, root / "backups", clock=lambda: "20260715_090000").check_health()

                self.assertFalse(health.healthy)
                self.assertEqual("schema-incompatible", health.status)
                self.assertEqual(before_hash, _file_hash(db))
                self.assertEqual(before_mtime, db.stat().st_mtime_ns)

    def test_health_check_rejects_partial_unique_identity_index(self):
        with WorkspaceTempDir() as root:
            db = root / "sample_library.sqlite3"
            with closing(sqlite3.connect(db)) as connection:
                connection.execute("""
                    create table sample_records (
                        id integer primary key,
                        sample_type text not null,
                        identity_json text not null,
                        canonical_label text not null,
                        system_label text not null,
                        created_order integer not null default 0
                    )
                """)
                connection.execute(
                    "create unique index unique_identity on sample_records(identity_json) where id > 0"
                )
                connection.commit()

            health = SampleLibrary(db, root / "backups", clock=lambda: "20260715_090000").check_health()

            self.assertFalse(health.healthy)
            self.assertEqual("schema-incompatible", health.status)

    def test_health_check_rejects_non_binary_identity_collations(self):
        valid_schema = """
            create table sample_records (
                id integer primary key,
                sample_type text not null,
                identity_json text not null,
                canonical_label text not null,
                system_label text not null,
                created_order integer not null default 0
            )
        """
        cases = {
            "column": valid_schema.replace(
                "identity_json text not null,",
                "identity_json text collate nocase not null unique,",
            ),
            "index": valid_schema + ";\ncreate unique index unique_identity on sample_records(identity_json collate nocase)",
        }

        for label, schema in cases.items():
            with self.subTest(label=label), WorkspaceTempDir() as root:
                db = root / "sample_library.sqlite3"
                with closing(sqlite3.connect(db)) as connection:
                    connection.executescript(schema)
                    connection.commit()

                health = SampleLibrary(db, root / "backups", clock=lambda: "20260715_090000").check_health()

                self.assertFalse(health.healthy)
                self.assertEqual("schema-incompatible", health.status)

    def test_health_check_rejects_extra_behavioral_constraints_and_triggers(self):
        schema_with_check = """
            create table sample_records (
                id integer primary key,
                sample_type text not null check(sample_type = 'liquid'),
                identity_json text not null unique,
                canonical_label text not null,
                system_label text not null,
                created_order integer not null default 0
            )
        """
        standard_schema_with_trigger = """
            create table sample_records (
                id integer primary key,
                sample_type text not null,
                identity_json text not null unique,
                canonical_label text not null,
                system_label text not null,
                created_order integer not null default 0
            );
            create trigger reject_neat before insert on sample_records
            when new.sample_type = 'neat'
            begin
                select raise(abort, 'neat blocked');
            end;
        """

        for label, schema in {
            "check constraint": schema_with_check,
            "trigger": standard_schema_with_trigger,
            "trigger named sqliteevil": standard_schema_with_trigger.replace(
                "reject_neat",
                "sqliteevil",
            ),
        }.items():
            with self.subTest(label=label), WorkspaceTempDir() as root:
                db = root / "sample_library.sqlite3"
                with closing(sqlite3.connect(db)) as connection:
                    connection.executescript(schema)
                    connection.commit()

                health = SampleLibrary(
                    db,
                    root / "backups",
                    clock=lambda: "20260716_122000",
                ).check_health()

                self.assertFalse(health.healthy)
                self.assertEqual("schema-incompatible", health.status)

    def test_health_check_rejects_shape_equivalent_behavioral_schema_variants(self):
        standard_schema = """
            create table sample_records (
                id integer primary key,
                sample_type text not null,
                identity_json text not null unique,
                canonical_label text not null,
                system_label text not null,
                created_order integer not null default 0
            )
        """
        cases = {
            "descending primary key": standard_schema.replace(
                "id integer primary key",
                "id integer primary key desc",
            ),
            "replace unique conflict": standard_schema.replace(
                "identity_json text not null unique",
                "identity_json text not null unique on conflict replace",
            ),
            "ignore not-null conflict": standard_schema.replace(
                "sample_type text not null",
                "sample_type text not null on conflict ignore",
            ),
        }

        for label, schema in cases.items():
            with self.subTest(label=label), WorkspaceTempDir() as root:
                db = root / "sample_library.sqlite3"
                with closing(sqlite3.connect(db)) as connection:
                    connection.execute(schema)
                    connection.commit()

                health = SampleLibrary(
                    db,
                    root / "backups",
                    clock=lambda: "20260716_130000",
                ).check_health()

                self.assertFalse(health.healthy)
                self.assertEqual("schema-incompatible", health.status)

    def test_recovery_backs_up_damaged_database_and_replaces_it_with_empty_compatible_schema(self):
        with WorkspaceTempDir() as root:
            db = root / "sample_library.sqlite3"
            damaged = b"not a sqlite database"
            db.write_bytes(damaged)
            backups = root / "backups"
            backups.mkdir()
            old_backup = backups / "old.sqlite3"
            old_backup.write_bytes(b"old backup")
            library = SampleLibrary(db, backups, clock=lambda: "20260715_091500")

            backup = library.recover()

            self.assertEqual(backups / "sample_library_20260715_091500.sqlite3", backup)
            self.assertEqual(damaged, backup.read_bytes())
            self.assertEqual(b"old backup", old_backup.read_bytes())
            self.assertTrue(library.check_health().healthy)
            self.assertEqual(0, _count_records(db))

    def test_recovery_replaces_incompatible_sample_records_view(self):
        with WorkspaceTempDir() as root:
            db = root / "sample_library.sqlite3"
            with closing(sqlite3.connect(db)) as connection:
                connection.execute("create table source_data (value text)")
                connection.execute("insert into source_data values ('preserved')")
                connection.execute("create view sample_records as select value from source_data")
                connection.commit()
            library = SampleLibrary(db, root / "backups", clock=lambda: "20260715_091700")

            backup = library.recover()

            self.assertTrue(backup.exists())
            self.assertTrue(library.check_health().healthy)
            self.assertEqual(0, _count_records(db))
            with closing(sqlite3.connect(backup)) as connection:
                self.assertEqual(("preserved",), connection.execute("select value from source_data").fetchone())
            with closing(sqlite3.connect(db)) as connection:
                self.assertEqual(
                    [("sample_records",)],
                    connection.execute(
                        "select name from sqlite_schema "
                        "where type = 'table' and lower(name) not glob 'sqlite_*' order by name"
                    ).fetchall(),
                )

    def test_recovery_replaces_case_variant_sample_records_table(self):
        with WorkspaceTempDir() as root:
            db = root / "sample_library.sqlite3"
            with closing(sqlite3.connect(db)) as connection:
                connection.execute("create table SAMPLE_RECORDS (value text)")
                connection.execute("insert into SAMPLE_RECORDS values ('preserved-in-backup')")
                connection.commit()
            library = SampleLibrary(db, root / "backups", clock=lambda: "20260715_091750")

            backup = library.recover()

            self.assertTrue(backup.exists())
            self.assertTrue(library.check_health().healthy)
            self.assertEqual(0, _count_records(db))
            with closing(sqlite3.connect(backup)) as connection:
                self.assertEqual(
                    ("preserved-in-backup",),
                    connection.execute("select value from SAMPLE_RECORDS").fetchone(),
                )

    def test_schema_recovery_removes_all_unrelated_user_objects_from_current_database(self):
        with WorkspaceTempDir() as root:
            db = root / "sample_library.sqlite3"
            with closing(sqlite3.connect(db)) as connection:
                connection.executescript(
                    """
                    create table sample_records (value text);
                    create table unrelated_secret (value text);
                    insert into unrelated_secret values ('preserve only in backup');
                    create view unrelated_view as select value from unrelated_secret;
                    create trigger unrelated_trigger after insert on unrelated_secret
                    begin
                        update unrelated_secret set value = new.value;
                    end;
                    """
                )
                connection.commit()
            library = SampleLibrary(db, root / "backups", clock=lambda: "20260716_122500")

            backup = library.recover()

            self.assertTrue(library.check_health().healthy)
            self.assertEqual(["sample_records"], _table_names(db))
            with closing(sqlite3.connect(db)) as connection:
                current_objects = connection.execute(
                    "select type, name from sqlite_schema "
                    "where lower(name) not glob 'sqlite_*' order by type, name"
                ).fetchall()
            self.assertEqual([("table", "sample_records")], current_objects)
            with closing(sqlite3.connect(backup)) as connection:
                self.assertEqual(
                    ("preserve only in backup",),
                    connection.execute("select value from unrelated_secret").fetchone(),
                )

    def test_schema_recovery_handles_virtual_table_shadow_objects(self):
        with WorkspaceTempDir() as root:
            db = root / "sample_library.sqlite3"
            with closing(sqlite3.connect(db)) as connection:
                try:
                    connection.execute("create virtual table extra_search using fts5(value)")
                except sqlite3.OperationalError as exc:
                    if "no such module" in str(exc).lower():
                        self.skipTest("SQLite FTS5 is unavailable")
                    raise
                connection.execute("insert into extra_search values ('preserve in backup')")
                connection.commit()
            library = SampleLibrary(db, root / "backups", clock=lambda: "20260716_130100")

            backup = library.recover()

            self.assertTrue(library.check_health().healthy)
            self.assertEqual(0, _count_records(db))
            with closing(sqlite3.connect(backup)) as connection:
                self.assertEqual(
                    ("preserve in backup",),
                    connection.execute("select value from extra_search").fetchone(),
                )

    def test_recovery_rejects_database_repaired_after_health_confirmation(self):
        with WorkspaceTempDir() as root:
            db = root / "sample_library.sqlite3"
            with closing(sqlite3.connect(db)) as connection:
                connection.execute("create table incompatible (value text)")
                connection.commit()
            library = SampleLibrary(db, root / "backups", clock=lambda: "20260716_130200")
            inspected_health = library.check_health()
            self.assertEqual("schema-incompatible", inspected_health.status)

            db.unlink()
            replacement = SampleLibrary(db, root / "backups", clock=lambda: "unused")
            replacement.save_final_records([LiquidSample("MFL", "mTHF", "1×10^-4 M", "298 K")])

            with self.assertRaisesRegex(SampleLibraryError, "changed after health check"):
                library.recover(expected_revision=inspected_health.revision)

            self.assertEqual(1, _count_records(db))

    def test_recovery_requires_rebuilt_database_to_remain_present(self):
        with WorkspaceTempDir() as root:
            db = root / "sample_library.sqlite3"
            db.write_bytes(b"not a sqlite database")
            library = SampleLibrary(db, root / "backups", clock=lambda: "20260716_170000")
            health = library.check_health()

            with patch.object(
                SampleLibrary,
                "check_health",
                return_value=SampleLibraryHealth("absent", exists=False),
            ):
                with self.assertRaisesRegex(SampleLibraryError, "absent"):
                    library.recover(expected_revision=health.revision)

    def test_valid_recovery_rejects_logically_equal_physical_replacement_after_backup(self):
        with WorkspaceTempDir() as root:
            db = root / "sample_library.sqlite3"
            library = SampleLibrary(db, root / "backups", clock=lambda: "20260716_170100")
            library.save_final_records([LiquidSample("MFL", "mTHF", "1×10^-4 M", "298 K")])
            health = library.check_health()
            replacement = root / "same-content-replacement.sqlite3"
            shutil.copy2(db, replacement)
            original_backup = library._backup_existing_database

            def backup_then_replace(_library, target=None, *, force_physical=False):
                backup = original_backup(target, force_physical=force_physical)
                os.replace(replacement, db)
                return backup

            with patch.object(
                SampleLibrary,
                "_backup_existing_database",
                autospec=True,
                side_effect=backup_then_replace,
            ):
                with self.assertRaisesRegex(SampleLibraryError, "changed"):
                    library.recover(expected_revision=health.revision)

            self.assertEqual(1, _count_records(db))

    def test_corrupt_replacement_error_reports_simultaneous_cleanup_residue(self):
        with WorkspaceTempDir() as root:
            db = root / "sample_library.sqlite3"
            backup = root / "backup.sqlite3"
            db.write_bytes(b"not a sqlite database")
            backup.write_bytes(b"not a sqlite database")
            library = SampleLibrary(db, root / "backups", clock=lambda: "20260716_170200")
            real_link = os.link
            failed_once = False

            def fail_first_publication(source, target):
                nonlocal failed_once
                if pathlib.Path(target) == db and not failed_once:
                    failed_once = True
                    raise PermissionError("replacement publication blocked")
                return real_link(source, target)

            def retain_cleanup_targets(entries):
                return tuple(path for path, _state in entries)

            with (
                patch(
                    "spectrum_organizer.store.sample_library.os.link",
                    side_effect=fail_first_publication,
                ),
                patch(
                    "spectrum_organizer.store.sample_library._cleanup_published_paths",
                    side_effect=retain_cleanup_targets,
                ),
            ):
                with self.assertRaises(SampleLibraryError) as raised:
                    library._replace_corrupt_database(backup)

            message = str(raised.exception)
            self.assertIn("replacement publication blocked", message)
            self.assertIn("replacement-temp cleanup incomplete", message)
            self.assertIn(str(db.parent), message)

    def test_corrupt_recovery_wraps_raw_read_failure(self):
        with WorkspaceTempDir() as root:
            db = root / "sample_library.sqlite3"
            db.write_bytes(b"not a sqlite database")
            backup = root / "backup.sqlite3"
            backup.write_bytes(b"not a sqlite database")
            library = SampleLibrary(db, root / "backups", clock=lambda: "20260715_091800")
            original_open = pathlib.Path.open

            def fail_current_database_read(path, *args, **kwargs):
                if path == db:
                    raise OSError("database became unreadable")
                return original_open(path, *args, **kwargs)

            with (
                patch.object(SampleLibrary, "_backup_existing_database", return_value=backup),
                patch.object(
                    SampleLibrary,
                    "_reset_valid_database",
                    side_effect=sqlite3.DatabaseError("file is not a database"),
                ),
                patch("pathlib.Path.open", autospec=True, side_effect=fail_current_database_read),
            ):
                with self.assertRaisesRegex(SampleLibraryError, "Could not verify sample library"):
                    library.recover()

    def test_corrupt_recovery_wraps_replacement_temp_file_creation_failure(self):
        with WorkspaceTempDir() as root:
            db = root / "sample_library.sqlite3"
            db.write_bytes(b"not a sqlite database")
            backups = root / "backups"
            library = SampleLibrary(db, backups, clock=lambda: "20260716_120000")
            real_mkstemp = tempfile.mkstemp

            def fail_replacement_temp(*args, **kwargs):
                if pathlib.Path(kwargs["dir"]) == db.parent:
                    raise PermissionError("replacement temp blocked")
                return real_mkstemp(*args, **kwargs)

            with patch(
                "spectrum_organizer.store.sample_library.tempfile.mkstemp",
                side_effect=fail_replacement_temp,
            ):
                with self.assertRaisesRegex(
                    SampleLibraryError,
                    "Could not replace sample library with an empty database",
                ):
                    library.recover()

    def test_recovery_backup_failure_preserves_damaged_database(self):
        with WorkspaceTempDir() as root:
            db = root / "sample_library.sqlite3"
            damaged = b"not a sqlite database"
            db.write_bytes(damaged)
            before_hash = _file_hash(db)
            library = SampleLibrary(
                db,
                root / "backups",
                clock=lambda: "20260715_091500",
                copy_file=lambda source, target: (_ for _ in ()).throw(OSError("copy blocked")),
            )

            with self.assertRaises(BackupError):
                library.recover()

            self.assertEqual(before_hash, _file_hash(db))
            self.assertEqual(damaged, db.read_bytes())

    def test_recovery_aborts_if_database_changes_after_backup(self):
        with WorkspaceTempDir() as root:
            db = root / "sample_library.sqlite3"
            library = SampleLibrary(db, root / "backups", clock=lambda: "20260715_091700")
            library.save_final_records([LiquidSample("MFL", "mTHF", "1×10^-4 M", "298 K")])
            original_backup = SampleLibrary._backup_existing_database

            def backup_then_modify(instance):
                backup = original_backup(instance)
                with closing(sqlite3.connect(db)) as connection:
                    connection.execute("update sample_records set canonical_label = 'changed-after-backup'")
                    connection.commit()
                return backup

            with patch.object(SampleLibrary, "_backup_existing_database", new=backup_then_modify):
                with self.assertRaisesRegex(SampleLibraryError, "changed during recovery"):
                    library.recover()

            with closing(sqlite3.connect(db)) as connection:
                label = connection.execute("select canonical_label from sample_records").fetchone()[0]
            self.assertEqual("changed-after-backup", label)
            self.assertEqual(1, _count_records(db))

    @unittest.skipUnless(os.name == "nt", "Windows replacement sharing contract")
    def test_corrupt_recovery_blocks_database_change_through_replace(self):
        with WorkspaceTempDir() as root:
            db = root / "sample_library.sqlite3"
            damaged = b"not a sqlite database"
            db.write_bytes(damaged)
            library = SampleLibrary(db, root / "backups", clock=lambda: "20260716_121500")
            real_digest = sample_library_module._database_content_digest
            writer_outcomes = []

            def digest_then_repair(path, *, cancel_check=None):
                digest = real_digest(path)
                if pathlib.Path(path) == db and not writer_outcomes:
                    try:
                        db.write_bytes(b"concurrent repair must not be overwritten")
                    except OSError:
                        writer_outcomes.append("blocked")
                    else:
                        writer_outcomes.append("committed")
                return digest

            with patch(
                "spectrum_organizer.store.sample_library._database_content_digest",
                side_effect=digest_then_repair,
            ):
                library.recover()

            self.assertEqual(["blocked"], writer_outcomes)
            self.assertTrue(library.check_health().healthy)

    @unittest.skipUnless(os.name == "nt", "Windows replacement sharing contract")
    def test_replacement_guard_prevents_companion_path_displacement(self):
        with WorkspaceTempDir() as root:
            db = root / "sample_library.sqlite3"
            db.write_bytes(b"not a sqlite database")
            lock_path = root / ".sample_library.sqlite3.replacement.lock"
            displaced_path = root / "displaced.lock"

            with sample_library_module._canonical_database_replacement_guard(db) as acquired:
                self.assertTrue(acquired)
                with self.assertRaises(OSError):
                    lock_path.rename(displaced_path)

            self.assertTrue(lock_path.exists())
            self.assertFalse(displaced_path.exists())

    @unittest.skipUnless(os.name == "nt", "Windows replacement sharing contract")
    def test_concurrent_corrupt_replacements_are_serialized_across_canonical_rename(self):
        with WorkspaceTempDir() as root:
            db = root / "sample_library.sqlite3"
            damaged = b"not a sqlite database"
            db.write_bytes(damaged)
            backup_one = root / "backup-one.sqlite3"
            backup_two = root / "backup-two.sqlite3"
            backup_one.write_bytes(damaged)
            backup_two.write_bytes(damaged)
            library_one = SampleLibrary(
                db,
                root / "backups-one",
                clock=lambda: "20260718_193000",
            )
            library_two = SampleLibrary(
                db,
                root / "backups-two",
                clock=lambda: "20260718_193001",
            )
            first_inside_guard = threading.Event()
            second_finished = threading.Event()
            second_entered_guard = threading.Event()
            errors: dict[str, BaseException] = {}
            real_revision = sample_library_module._database_revision

            def overlapping_revision(path):
                if pathlib.Path(path) == db:
                    if threading.current_thread().name == "replacement-first":
                        first_inside_guard.set()
                        self.assertTrue(second_finished.wait(timeout=10))
                    else:
                        second_entered_guard.set()
                        raise RuntimeError("second replacement entered the canonical guard")
                return real_revision(path)

            def replace_first():
                try:
                    library_one._replace_corrupt_database(backup_one)
                except BaseException as exc:
                    errors["first"] = exc

            def replace_second():
                try:
                    self.assertTrue(first_inside_guard.wait(timeout=10))
                    library_two._replace_corrupt_database(backup_two)
                except BaseException as exc:
                    errors["second"] = exc
                finally:
                    second_finished.set()

            with patch(
                "spectrum_organizer.store.sample_library._database_revision",
                side_effect=overlapping_revision,
            ):
                first = threading.Thread(target=replace_first, name="replacement-first")
                second = threading.Thread(target=replace_second, name="replacement-second")
                first.start()
                self.assertTrue(first_inside_guard.wait(timeout=10))
                second.start()
                first.join(timeout=10)
                second.join(timeout=10)

            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertNotIn("first", errors)
            self.assertIsInstance(errors.get("second"), SampleLibraryError)
            self.assertIn("in use", str(errors["second"]).lower())
            self.assertFalse(second_entered_guard.is_set())
            self.assertTrue(library_one.check_health().healthy)

    def test_recovery_excludes_a_writer_after_the_final_content_check(self):
        with WorkspaceTempDir() as root:
            db = root / "sample_library.sqlite3"
            library = SampleLibrary(db, root / "backups", clock=lambda: "20260715_091800")
            library.save_final_records([LiquidSample("MFL", "mTHF", "1×10^-4 M", "298 K")])
            original_digest = __import__(
                "spectrum_organizer.store.sample_library",
                fromlist=["_database_content_digest"],
            )._database_content_digest
            backup_path = root / "backups" / "sample_library_20260715_091800.sqlite3"
            writer_outcomes = []

            def digest_then_try_late_write(path, *, cancel_check=None):
                digest = original_digest(path)
                if pathlib.Path(path) == backup_path and not writer_outcomes:
                    try:
                        with closing(sqlite3.connect(db, timeout=0)) as connection:
                            connection.execute(
                                "update sample_records set canonical_label = 'late-write-must-not-disappear'"
                            )
                            connection.commit()
                    except sqlite3.OperationalError:
                        writer_outcomes.append("blocked")
                    else:
                        writer_outcomes.append("committed")
                return digest

            with patch(
                "spectrum_organizer.store.sample_library._database_content_digest",
                side_effect=digest_then_try_late_write,
            ):
                library.recover()

            self.assertEqual(["blocked"], writer_outcomes)
            self.assertEqual(0, _count_records(db))

    def test_recovery_of_exclusively_locked_database_returns_without_hanging(self):
        with WorkspaceTempDir() as root:
            db = root / "sample_library.sqlite3"
            library = SampleLibrary(db, root / "backups", clock=lambda: "20260715_091900")
            library.save_final_records([LiquidSample("MFL", "mTHF", "1×10^-4 M", "298 K")])
            with closing(sqlite3.connect(db, timeout=0, isolation_level=None)) as holder:
                holder.execute("begin exclusive")
                completed = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        (
                            "from pathlib import Path; "
                            "from spectrum_organizer.store.sample_library import SampleLibrary, SampleLibraryError; "
                            "library=SampleLibrary(Path(r'%s'), Path(r'%s'), clock=lambda: '20260715_091900'); "
                            "\ntry: library.recover()\n"
                            "except SampleLibraryError as exc: print(exc)\n"
                            "else: raise SystemExit('recovery unexpectedly succeeded')"
                        )
                        % (db, root / "backups-child"),
                    ],
                    cwd=ROOT,
                    env={**os.environ, "PYTHONPATH": str(SRC)},
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                holder.rollback()

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertIn("locked", completed.stdout.lower())
            self.assertEqual(1, _count_records(db))

    def test_recovery_preserves_committed_wal_data_without_deleting_unowned_stale_files(self):
        with WorkspaceTempDir() as root:
            db = root / "sample_library.sqlite3"
            subprocess.run(
                [
                    sys.executable,
                    "-c",
                    """
import os
import sqlite3
import sys

connection = sqlite3.connect(sys.argv[1])
connection.execute("pragma journal_mode=wal")
connection.execute("pragma wal_autocheckpoint=0")
connection.execute('''
    create table sample_records (
        id integer primary key,
        sample_type text not null,
        identity_json text not null unique,
        canonical_label text not null,
        system_label text not null,
        created_order integer not null default 0
    )
''')
connection.commit()
connection.execute("pragma wal_checkpoint(truncate)")
connection.execute(
    "insert into sample_records "
    "(sample_type, identity_json, canonical_label, system_label) values (?, ?, ?, ?)",
    ("liquid", "committed-in-wal", "MFL / mTHF", "MFL / mTHF / 298 K"),
)
connection.commit()
os._exit(0)
""",
                    str(db),
                ],
                check=True,
            )
            wal = pathlib.Path(f"{db}-wal")
            shm = pathlib.Path(f"{db}-shm")
            journal = pathlib.Path(f"{db}-journal")
            self.assertTrue(wal.exists())
            self.assertGreater(wal.stat().st_size, 0)
            self.assertTrue(shm.exists())
            journal.touch()
            library = SampleLibrary(db, root / "backups", clock=lambda: "20260715_093000")

            backup = library.recover()

            self.assertEqual(1, _count_records(backup))
            self.assertEqual("committed-in-wal", _identity_json(backup))
            self.assertTrue(library.check_health().healthy)
            self.assertEqual(0, _count_records(db))
            self.assertFalse(wal.exists())
            self.assertFalse(shm.exists())
            self.assertTrue(journal.exists())

    def test_corrupt_recovery_sidecar_detach_failure_preserves_original_database(self):
        with WorkspaceTempDir() as root:
            db = root / "sample_library.sqlite3"
            library = SampleLibrary(db, root / "backups", clock=lambda: "20260715_093500")
            damaged = b"not a sqlite database"
            db.write_bytes(damaged)
            backup = root / "backup.sqlite3"
            backup.write_bytes(damaged)
            wal = pathlib.Path(f"{db}-wal")
            shm = pathlib.Path(f"{db}-shm")
            journal = pathlib.Path(f"{db}-journal")
            wal.touch()
            shm.touch()
            journal.touch()
            before_hash = _file_hash(db)
            original_replace = os.replace

            def fail_canonical_journal_detach(source, target):
                if pathlib.Path(source) == journal:
                    raise OSError("journal is locked")
                return original_replace(source, target)

            with (
                patch.object(SampleLibrary, "_backup_existing_database", return_value=backup),
                patch.object(
                    SampleLibrary,
                    "_reset_valid_database",
                    side_effect=sqlite3.DatabaseError("file is not a database"),
                ),
                patch("spectrum_organizer.store.sample_library._database_content_digest", return_value="same"),
                patch("spectrum_organizer.store.sample_library.os.replace", side_effect=fail_canonical_journal_detach),
            ):
                with self.assertRaises(SampleLibraryError):
                    library.recover()

            self.assertEqual(before_hash, _file_hash(db))
            self.assertTrue(wal.exists())
            self.assertTrue(shm.exists())
            self.assertTrue(journal.exists())
            self.assertEqual(damaged, db.read_bytes())

    def test_recovery_windows_replace_and_restore_failure_preserves_staged_sidecar(self):
        with WorkspaceTempDir() as root:
            db = root / "sample_library.sqlite3"
            library = SampleLibrary(db, root / "backups", clock=lambda: "20260715_093700")
            library.save_final_records([LiquidSample("MFL", "mTHF", "1x10^-4 M", "298 K")])
            journal = pathlib.Path(f"{db}-journal")
            staged_data = b"recoverable staged sidecar"
            journal.write_bytes(staged_data)
            backup = root / "backup.sqlite3"
            shutil.copy2(db, backup)
            pathlib.Path(f"{backup}-journal").write_bytes(staged_data)
            original_replace = os.replace
            original_link = os.link
            staged_path = None

            def fail_locked_replacements(source, target):
                nonlocal staged_path
                source = pathlib.Path(source)
                target = pathlib.Path(target)
                if source == journal:
                    staged_path = target
                    return original_replace(source, target)
                return original_replace(source, target)

            def fail_locked_restore(source, target):
                if pathlib.Path(target) == db:
                    raise PermissionError("main database is locked")
                if pathlib.Path(target) == journal:
                    raise PermissionError("sidecar restore is locked")
                return original_link(source, target)

            with (
                patch.object(SampleLibrary, "_backup_existing_database", return_value=backup),
                patch.object(
                    SampleLibrary,
                    "_reset_valid_database",
                    side_effect=sqlite3.DatabaseError("file is not a database"),
                ),
                patch("spectrum_organizer.store.sample_library._database_content_digest", return_value="same"),
                patch("spectrum_organizer.store.sample_library.os.replace", side_effect=fail_locked_replacements),
                patch("spectrum_organizer.store.sample_library.os.link", side_effect=fail_locked_restore),
            ):
                with self.assertRaises(SampleLibraryError) as caught:
                    library.recover()

            self.assertIsNotNone(staged_path)
            self.assertIn("main database is locked", str(caught.exception))
            self.assertIn("sidecar restore is locked", str(caught.exception))
            self.assertIn(str(staged_path), str(caught.exception))
            self.assertFalse(journal.exists())
            self.assertTrue(staged_path.exists())
            self.assertEqual(staged_data, staged_path.read_bytes())

    def test_sidecar_restore_collision_preserves_concurrent_target_and_staged_data(self):
        with WorkspaceTempDir() as root:
            sidecar = root / "sample_library.sqlite3-wal"
            staged = root / ".sample_library.sqlite3-wal.staged.tmp"
            staged.write_bytes(b"staged recovery data")
            original_link = os.link
            def link_with_collision(source, target):
                sidecar.write_bytes(b"concurrent target")
                return original_link(source, target)

            with patch(
                "spectrum_organizer.store.sample_library.os.link",
                side_effect=link_with_collision,
            ):
                with self.assertRaisesRegex(SampleLibraryError, "appeared during recovery"):
                    sample_library_module._restore_database_sidecars([(sidecar, staged)])

            self.assertEqual(b"concurrent target", sidecar.read_bytes())
            self.assertEqual(b"staged recovery data", staged.read_bytes())

    def test_sidecar_restore_rejects_staged_path_replaced_before_link(self):
        with WorkspaceTempDir() as root:
            sidecar = root / "sample_library.sqlite3-wal"
            staged = root / ".sample_library.sqlite3-wal.staged.tmp"
            staged.write_bytes(b"approved staged data")
            real_link = os.link
            def replace_staged_then_link(source, target):
                pathlib.Path(source).unlink()
                pathlib.Path(source).write_bytes(b"concurrent replacement")
                return real_link(source, target)

            with patch(
                "spectrum_organizer.store.sample_library.os.link",
                side_effect=replace_staged_then_link,
            ):
                with self.assertRaisesRegex(SampleLibraryError, "changed during restoration"):
                    sample_library_module._restore_database_sidecars([(sidecar, staged)])

            self.assertEqual(b"concurrent replacement", sidecar.read_bytes())
            self.assertEqual(b"concurrent replacement", staged.read_bytes())

    def test_sidecar_restore_preserves_staged_path_replaced_after_link(self):
        with WorkspaceTempDir() as root:
            sidecar = root / "sample_library.sqlite3-wal"
            staged = root / ".sample_library.sqlite3-wal.staged.tmp"
            staged.write_bytes(b"approved staged data")
            real_link = os.link
            def link_then_replace_staged(source, target):
                result = real_link(source, target)
                pathlib.Path(source).unlink()
                pathlib.Path(source).write_bytes(b"concurrent replacement")
                return result

            with patch(
                "spectrum_organizer.store.sample_library.os.link",
                side_effect=link_then_replace_staged,
            ):
                with self.assertRaisesRegex(SampleLibraryError, "retained path changed"):
                    sample_library_module._restore_database_sidecars([(sidecar, staged)])

            self.assertEqual(b"approved staged data", sidecar.read_bytes())
            self.assertEqual(b"concurrent replacement", staged.read_bytes())

    def test_sidecar_restore_preserves_target_replaced_after_link(self):
        with WorkspaceTempDir() as root:
            sidecar = root / "sample_library.sqlite3-wal"
            staged = root / ".sample_library.sqlite3-wal.staged.tmp"
            staged.write_bytes(b"approved staged data")
            real_link = os.link
            def link_then_replace_target(source, target):
                result = real_link(source, target)
                pathlib.Path(target).unlink()
                pathlib.Path(target).write_bytes(b"concurrent target replacement")
                return result

            with patch(
                "spectrum_organizer.store.sample_library.os.link",
                side_effect=link_then_replace_target,
            ):
                with self.assertRaisesRegex(SampleLibraryError, "changed during restoration"):
                    sample_library_module._restore_database_sidecars([(sidecar, staged)])

            self.assertEqual(b"concurrent target replacement", sidecar.read_bytes())
            self.assertEqual(b"approved staged data", staged.read_bytes())

    def test_corrupt_recovery_preserves_main_replaced_before_detachment(self):
        with WorkspaceTempDir() as root:
            db = root / "sample_library.sqlite3"
            db.write_bytes(b"approved damaged database")
            library = SampleLibrary(db, root / "backups", clock=lambda: "20260716_150295")
            health = library.check_health()
            real_detach = sample_library_module._detach_database_sidecars

            def replace_main_then_detach(path):
                path.unlink()
                path.write_bytes(b"concurrent replacement database")
                return real_detach(path)

            with patch(
                "spectrum_organizer.store.sample_library._detach_database_sidecars",
                side_effect=replace_main_then_detach,
            ):
                with self.assertRaisesRegex(SampleLibraryError, "changed during recovery"):
                    library.recover(expected_revision=health.revision)

            self.assertEqual(b"concurrent replacement database", db.read_bytes())

    def test_orphan_recovery_rejects_same_metadata_staged_sidecar_change(self):
        with WorkspaceTempDir() as root:
            db = root / "sample_library.sqlite3"
            wal = pathlib.Path(f"{db}-wal")
            wal.write_bytes(b"approved orphan wal")
            library = SampleLibrary(db, root / "backups", clock=lambda: "20260716_150297")
            health = library.check_health()
            real_revision = sample_library_module._physical_database_revision_with_staged_sidecars
            staged_sidecar = None

            def revision_then_change_staged_sidecar(path, staged_sidecars):
                nonlocal staged_sidecar
                revision = real_revision(path, staged_sidecars)
                if staged_sidecars and staged_sidecar is None:
                    staged_sidecar = pathlib.Path(staged_sidecars[0][1])
                    stat = staged_sidecar.stat()
                    staged_sidecar.write_bytes(b"Y" * stat.st_size)
                    os.utime(staged_sidecar, ns=(stat.st_atime_ns, stat.st_mtime_ns))
                return revision

            with patch(
                "spectrum_organizer.store.sample_library._physical_database_revision_with_staged_sidecars",
                side_effect=revision_then_change_staged_sidecar,
            ):
                with self.assertRaisesRegex(SampleLibraryError, "changed|retained"):
                    library.recover(expected_revision=health.revision)

            self.assertFalse(db.exists())
            self.assertIsNotNone(staged_sidecar)
            self.assertTrue(staged_sidecar.exists())
            self.assertEqual(b"Y" * len(b"approved orphan wal"), staged_sidecar.read_bytes())

    def test_successful_recovery_reports_owned_sidecar_cleanup_failure(self):
        with WorkspaceTempDir() as root:
            db = root / "sample_library.sqlite3"
            wal = pathlib.Path(f"{db}-wal")
            db.write_bytes(b"not a sqlite database")
            wal.write_bytes(b"approved wal")
            library = SampleLibrary(db, root / "backups", clock=lambda: "20260716_150290")
            health = library.check_health()
            real_unlink = sample_library_module._unlink_if_owned_identity_matches

            def fail_staged_cleanup(path, identity):
                if pathlib.Path(path).name.endswith(".tmp") and "-wal." in pathlib.Path(path).name:
                    raise PermissionError("staged sidecar cleanup denied")
                return real_unlink(path, identity)

            with patch(
                "spectrum_organizer.store.sample_library._unlink_if_owned_identity_matches",
                side_effect=fail_staged_cleanup,
            ):
                with self.assertRaisesRegex(SampleLibraryError, "cleanup.*retained"):
                    library.recover(expected_revision=health.revision)

            retained = list(root.glob(".*-wal.*.tmp"))
            self.assertEqual(1, len(retained))
            self.assertEqual(b"approved wal", retained[0].read_bytes())

    def test_sidecar_restore_reports_every_retained_staged_path(self):
        with WorkspaceTempDir() as root:
            wal = root / "sample_library.sqlite3-wal"
            shm = root / "sample_library.sqlite3-shm"
            staged_wal = root / ".wal.staged.tmp"
            staged_shm = root / ".shm.staged.tmp"
            wal.write_bytes(b"concurrent wal")
            shm.write_bytes(b"concurrent shm")
            staged_wal.write_bytes(b"retained wal")
            staged_shm.write_bytes(b"retained shm")
            with self.assertRaises(SampleLibraryError) as caught:
                sample_library_module._restore_database_sidecars(
                    [(wal, staged_wal), (shm, staged_shm)]
                )

            self.assertIn(str(staged_wal), str(caught.exception))
            self.assertIn(str(staged_shm), str(caught.exception))
            self.assertEqual(b"concurrent wal", wal.read_bytes())
            self.assertEqual(b"concurrent shm", shm.read_bytes())

    def test_sidecar_restore_reports_missing_staged_path(self):
        with WorkspaceTempDir() as root:
            sidecar = root / "sample_library.sqlite3-wal"
            staged = root / ".missing-wal.staged.tmp"
            with self.assertRaisesRegex(SampleLibraryError, "missing retained path"):
                sample_library_module._restore_database_sidecars([(sidecar, staged)])

            self.assertFalse(sidecar.exists())

    def test_coherent_backup_closes_destination_before_temp_cleanup(self):
        with WorkspaceTempDir() as root:
            db = root / "sample_library.sqlite3"
            library = SampleLibrary(db, root / "backups", clock=lambda: "20260715_094000")
            library.save_final_records([LiquidSample("MFL", "mTHF", "1x10^-4 M", "298 K")])
            original_connect = sqlite3.connect
            closed_paths = set()

            class TrackingConnection(sqlite3.Connection):
                def __init__(self, database, *args, **kwargs):
                    self.database_path = pathlib.Path(database) if not str(database).startswith("file:") else None
                    super().__init__(database, *args, **kwargs)

                def close(self):
                    if self.database_path is not None:
                        closed_paths.add(self.database_path)
                    super().close()

            def tracking_connect(database, *args, **kwargs):
                return original_connect(database, *args, factory=TrackingConnection, **kwargs)

            with patch("spectrum_organizer.store.sample_library.sqlite3.connect", side_effect=tracking_connect):
                backup = library._backup_existing_database()

            self.assertEqual(root / "backups" / "sample_library_20260715_094000.sqlite3", backup)
            self.assertEqual([], list((root / "backups").glob("*.tmp")))
            self.assertTrue(any(path.suffix == ".tmp" for path in closed_paths))

    def test_first_save_creates_database_without_backup_and_reuses_unique_rows(self):
        with WorkspaceTempDir() as root:
            db = root / "sample_library.sqlite3"
            backups = root / "backups"
            library = SampleLibrary(db, backups, clock=lambda: "20260627_090000")
            records = [LiquidSample("MFL", "mTHF", "1×10^-4 M", "298 K")]
            ids1 = library.save_final_records(records)
            ids2 = library.save_final_records(records)
            self.assertEqual(ids1, ids2)
            self.assertEqual([], list(backups.glob("*.sqlite3")))
            self.assertEqual(1, _count_records(db))

    def test_save_records_audits_write_attempt_before_touching_database(self):
        with WorkspaceTempDir() as root:
            db = root / "data" / "sample_library.sqlite3"
            library = SampleLibrary(
                db,
                root / "backups",
                clock=lambda: "20260627_090000",
            )
            record = LiquidSample("MFL", "mTHF", "1×10^-4 M", "298 K")

            with patch.object(
                sample_library_module,
                "record_runtime_audit_event",
                side_effect=RuntimeError("audit unavailable"),
            ) as audit:
                with self.assertRaisesRegex(RuntimeError, "audit unavailable"):
                    library.save_final_records([record])

            audit.assert_called_once_with(
                "sample_library_write_attempt",
                {
                    "database_path": str(db.resolve()),
                    "record_count": 1,
                },
            )
            self.assertFalse(db.exists())

    def test_existing_database_is_backed_up_before_actual_new_write_and_old_backups_remain(self):
        with WorkspaceTempDir() as root:
            db = root / "sample_library.sqlite3"
            backups = root / "backups"
            library = SampleLibrary(db, backups, clock=lambda: "20260627_090000")
            library.save_final_records([LiquidSample("MFL", "mTHF", "1×10^-4 M", "298 K")])
            old = backups / "old.sqlite3"
            backups.mkdir(exist_ok=True)
            old.write_text("old", encoding="utf-8")

            library = SampleLibrary(db, backups, clock=lambda: "20260627_091500")
            library.save_final_records([NeatSample("PFL", "Solid", "77 K")])

            self.assertTrue(old.exists())
            backup = backups / "sample_library_20260627_091500.sqlite3"
            self.assertTrue(backup.exists())
            self.assertEqual(2, _count_records(db))
            self.assertEqual(1, _count_records(backup))

    def test_multiple_writes_in_same_second_use_distinct_backup_names(self):
        with WorkspaceTempDir() as root:
            db = root / "sample_library.sqlite3"
            backups = root / "backups"
            library = SampleLibrary(db, backups, clock=lambda: "20260716_160000")
            library.save_final_records([LiquidSample("MFL", "mTHF", "1x10^-4 M", "298 K")])
            library.save_final_records([NeatSample("PFL", "Solid", "77 K")])
            library.save_final_records([NeatSample("TFL", "Solid", "298 K")])

            self.assertEqual(3, _count_records(db))
            self.assertEqual(
                [
                    "sample_library_20260716_160000.sqlite3",
                    "sample_library_20260716_160000_2.sqlite3",
                ],
                sorted(path.name for path in backups.glob("*.sqlite3")),
            )

    def test_backup_failure_blocks_write(self):
        with WorkspaceTempDir() as root:
            db = root / "sample_library.sqlite3"
            backups = root / "backups"
            library = SampleLibrary(db, backups, clock=lambda: "20260627_090000")
            library.save_final_records([LiquidSample("MFL", "mTHF", "1×10^-4 M", "298 K")])

            failing = SampleLibrary(
                db,
                backups,
                clock=lambda: "20260627_091500",
                copy_file=lambda source, target: (_ for _ in ()).throw(OSError("copy blocked")),
            )
            with self.assertRaises(BackupError):
                failing.save_final_records([NeatSample("PFL", "Solid", "77 K")])
            self.assertEqual(1, _count_records(db))

    def test_batch_rollback_on_insert_failure(self):
        with WorkspaceTempDir() as root:
            db = root / "sample_library.sqlite3"
            library = SampleLibrary(db, root / "backups", clock=lambda: "20260627_090000")
            good = LiquidSample("MFL", "mTHF", "1×10^-4 M", "298 K")
            bad = LiquidSample("PFL", "mTHF", "1×10^-4 M", "298 K")
            with self.assertRaises(sqlite3.DatabaseError):
                library.save_final_records([good, bad], fail_after=1)
            self.assertEqual(0, _count_records(db))

    def test_incompatible_existing_database_is_not_modified(self):
        with WorkspaceTempDir() as root:
            db = root / "sample_library.sqlite3"
            with closing(sqlite3.connect(db)) as connection:
                connection.execute("create table something_else (id integer primary key)")
                connection.commit()
            before_tables = _table_names(db)
            library = SampleLibrary(db, root / "backups", clock=lambda: "20260627_090000")
            with self.assertRaises(SampleLibraryError):
                library.save_final_records([LiquidSample("MFL", "mTHF", "1×10^-4 M", "298 K")])
            self.assertEqual(before_tables, _table_names(db))

    def test_existing_database_without_identity_unique_constraint_is_incompatible(self):
        with WorkspaceTempDir() as root:
            db = root / "sample_library.sqlite3"
            with closing(sqlite3.connect(db)) as connection:
                connection.execute("""
                    create table sample_records (
                        id integer primary key,
                        sample_type text not null,
                        identity_json text not null,
                        canonical_label text not null,
                        system_label text not null,
                        created_order integer not null default 0
                    )
                """)
                connection.commit()
            library = SampleLibrary(db, root / "backups", clock=lambda: "20260627_090000")

            with self.assertRaises(SampleLibraryError):
                library.save_final_records([LiquidSample("MFL", "mTHF", "1×10^-4 M", "298 K")])

            self.assertEqual(0, _count_records(db))


def _count_records(path):
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute("select count(*) from sample_records").fetchone()[0]


def _table_names(path):
    with closing(sqlite3.connect(path)) as connection:
        rows = connection.execute("select name from sqlite_master where type = 'table' order by name").fetchall()
    return [row[0] for row in rows]


def _identity_json(path):
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute("select identity_json from sample_records").fetchone()[0]


def _file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


if __name__ == "__main__":
    unittest.main()
