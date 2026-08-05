from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import time
from typing import Callable, Iterable
import uuid

from spectrum_organizer.safety.owned_paths import (
    OwnershipError,
    add_allowed_child,
    bind_allowed_child_identity,
    cleanup_owned_temp_root,
    create_run_ownership_at_root,
)
from spectrum_organizer.safety.identity_paths import create_exclusive_held_file
from spectrum_organizer.runtime_audit import record_runtime_audit_event
from spectrum_organizer.store.sqlite_digest import sqlite_content_sha256


_DATABASE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")
_BACKUP_LOCK_TIMEOUT_SECONDS = 0.5
_TRANSIENT_WINDOWS_FILE_ERRORS = {5, 32, 33, 1175}
_WINDOWS_FILE_RETRY_DELAYS = (0.03, 0.08, 0.15)
_OWNERSHIP_REVISION_SEPARATOR = "|ownership:"
_HEALTH_SNAPSHOT_MARKER_ID = "sample-library-health-v1"
_QUICK_CHECK_PROGRESS_HANDLER_STEPS = 1000
_EXPECTED_COLUMNS = {
    "id": ("INTEGER", False, None, 1),
    "sample_type": ("TEXT", True, None, 0),
    "identity_json": ("TEXT", True, None, 0),
    "canonical_label": ("TEXT", True, None, 0),
    "system_label": ("TEXT", True, None, 0),
    "created_order": ("INTEGER", True, "0", 0),
}
_EXPECTED_TABLE_SQL = """
    create table sample_records (
        id integer primary key,
        sample_type text not null,
        identity_json text not null unique,
        canonical_label text not null,
        system_label text not null,
        created_order integer not null default 0
    )
"""
_UNSUPPORTED_TABLE_SQL = re.compile(
    r"\b(?:check|references|collate|generated|autoincrement|strict)\b|\bwithout\s+rowid\b",
    re.IGNORECASE,
)


class SampleLibraryError(RuntimeError):
    pass


class BackupError(SampleLibraryError):
    pass


class _HealthSnapshotCleanupError(SampleLibraryError):
    pass


class _ExternalControlFlow(BaseException):
    def __init__(self, error: BaseException):
        super().__init__(str(error))
        self.error = error
        self.original_traceback = error.__traceback__


_PhysicalIdentity = tuple[int, int, int, int]


@dataclass(frozen=True)
class _OwnedPathState:
    identity: _PhysicalIdentity
    digest: str


@dataclass(frozen=True)
class _DetachedPath:
    source: Path
    staged: Path
    approved_state: _OwnedPathState

    def __iter__(self):
        yield self.source
        yield self.staged

    def __getitem__(self, index: int) -> Path:
        return (self.source, self.staged)[index]


@dataclass(frozen=True)
class SampleLibraryHealth:
    status: str
    exists: bool
    detail: str | None = None
    revision: str | None = None

    @property
    def healthy(self) -> bool:
        return self.status in {"absent", "healthy"}


@dataclass(frozen=True)
class SampleLibrary:
    path: Path
    backups_dir: Path
    clock: Callable[[], str]
    copy_file: Callable[[Path, Path], None] = shutil.copy2
    health_temp_root: Path | None = None

    def check_health(self, *, cancel_check=None) -> SampleLibraryHealth:
        try:
            return self._check_health(cancel_check=_wrap_external_cancel(cancel_check))
        except _ExternalControlFlow as control_flow:
            for note in getattr(control_flow, "__notes__", ()):
                control_flow.error.add_note(note)
            raise control_flow.error.with_traceback(control_flow.original_traceback) from None

    def _check_health(self, *, cancel_check=None) -> SampleLibraryHealth:
        _check_cancel(cancel_check)
        components = _existing_database_components(self.path)
        if not components:
            _check_cancel(cancel_check)
            components = _existing_database_components(self.path)
        if self.path not in components:
            if components:
                try:
                    revision = (
                        f"{_database_revision(self.path, cancel_check=cancel_check)}"
                        f"{_OWNERSHIP_REVISION_SEPARATOR}"
                        f"{_database_ownership_revision(self.path, cancel_check=cancel_check)}"
                    )
                except SampleLibraryError as exc:
                    return SampleLibraryHealth(
                        "unreadable",
                        exists=True,
                        detail=str(exc),
                    )
                return SampleLibraryHealth(
                    "corrupt",
                    exists=True,
                    detail="sample library main file is missing while sidecar files remain",
                    revision=revision,
                )
            return SampleLibraryHealth("absent", exists=False)
        revision: str | None = None
        try:
            with _canonical_database_shared_lock(self.path) as lock_acquired:
                if not lock_acquired:
                    return SampleLibraryHealth(
                        "locked",
                        exists=True,
                        detail="sample library is exclusively locked",
                    )
                physical_revision = _physical_database_revision(
                    self.path,
                    cancel_check=cancel_check,
                )
                ownership_revision = _database_ownership_revision(
                    self.path,
                    cancel_check=cancel_check,
                )
                health_temp_root = self.health_temp_root or (
                    self.path.parent / ".sample-library-health-temp"
                )
                with _health_check_snapshot(
                    self.path,
                    temp_root=health_temp_root,
                    cancel_check=cancel_check,
                ) as snapshot_path:
                    try:
                        revision = (
                            f"logical:{_database_content_digest(snapshot_path, cancel_check=cancel_check)}"
                            f"{_OWNERSHIP_REVISION_SEPARATOR}{ownership_revision}"
                        )
                    except SampleLibraryError:
                        revision = (
                            f"{physical_revision}"
                            f"{_OWNERSHIP_REVISION_SEPARATOR}{ownership_revision}"
                        )
                    with _connect_read_only(snapshot_path) as connection:
                        quick_check = _run_quick_check(
                            connection,
                            cancel_check=cancel_check,
                        )
                        if quick_check != [("ok",)]:
                            detail = "; ".join(str(row[0]) for row in quick_check)
                            return SampleLibraryHealth(
                                "corrupt",
                                exists=True,
                                detail=detail,
                                revision=revision,
                            )
                        try:
                            _require_compatible_schema(connection)
                        except SampleLibraryError as exc:
                            return SampleLibraryHealth(
                                "schema-incompatible",
                                exists=True,
                                detail=str(exc),
                                revision=revision,
                            )
        except sqlite3.Error as exc:
            return SampleLibraryHealth(
                _sqlite_error_status(exc),
                exists=True,
                detail=str(exc),
                revision=revision,
            )
        except OSError as exc:
            return SampleLibraryHealth(
                "unreadable",
                exists=True,
                detail=str(exc),
                revision=revision,
            )
        except _HealthSnapshotCleanupError as exc:
            return SampleLibraryHealth(
                "health-check-failed",
                exists=True,
                detail=str(exc),
                revision=None,
            )
        except SampleLibraryError as exc:
            return SampleLibraryHealth(
                "unreadable",
                exists=True,
                detail=str(exc),
                revision=revision,
            )
        return SampleLibraryHealth("healthy", exists=True, revision=revision)

    def save_final_records(self, records: Iterable, fail_after: int | None = None) -> list[int]:
        records = list(records)
        record_runtime_audit_event(
            "sample_library_write_attempt",
            {
                "database_path": str(self.path.resolve()),
                "record_count": len(records),
            },
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        existed = self.path.exists()
        with _connect(self.path) as connection:
            if existed:
                _require_compatible_schema(connection)
            else:
                _ensure_schema(connection)
            missing = [record for record in records if _find_record_id(connection, record) is None]
            if existed and missing:
                self._backup_existing_database()
            with connection:
                for index, record in enumerate(records, start=1):
                    _insert_or_get_record(connection, record)
                    if fail_after is not None and index >= fail_after:
                        raise sqlite3.DatabaseError("injected batch failure")
        with _connect(self.path) as connection:
            return [_find_record_id(connection, record) for record in records]

    def planned_backup_path(self) -> Path:
        stem = f"sample_library_{self.clock()}"
        candidate = self.backups_dir / f"{stem}.sqlite3"
        suffix = 2
        while any(path.exists() for path in (candidate, *_database_sidecars(candidate))):
            candidate = self.backups_dir / f"{stem}_{suffix}.sqlite3"
            suffix += 1
        return candidate

    def recover(
        self,
        *,
        expected_revision: str | None = None,
        backup_path: Path | None = None,
    ) -> Path:
        try:
            return self._recover(
                expected_revision=expected_revision,
                backup_path=backup_path,
            )
        except OSError as exc:
            raise SampleLibraryError(
                f"Could not verify sample library during recovery: {exc}"
            ) from exc

    def _recover(
        self,
        *,
        expected_revision: str | None = None,
        backup_path: Path | None = None,
    ) -> Path:
        if not self.path.exists() and not any(sidecar.exists() for sidecar in _database_sidecars(self.path)):
            raise SampleLibraryError(f"Cannot recover missing sample library: {self.path}")
        expected_content_revision, expected_ownership_revision = _split_expected_revision(
            expected_revision
        )
        physical_revision = bool(
            expected_content_revision and expected_content_revision.startswith("physical:")
        )
        if (
            expected_ownership_revision is not None
            and _database_ownership_revision(self.path) != expected_ownership_revision
        ):
            raise SampleLibraryError(
                "Sample library changed after health check; the current database was preserved"
            )
        if physical_revision:
            backup = self._backup_existing_database(backup_path, force_physical=True)
        elif backup_path is not None:
            backup = self._backup_existing_database(backup_path)
        else:
            backup = self._backup_existing_database()
        if (
            expected_ownership_revision is not None
            and _database_ownership_revision(self.path) != expected_ownership_revision
        ):
            raise SampleLibraryError(
                "Sample library changed during backup; the current database was preserved"
            )
        if expected_content_revision is not None:
            backup_revision = (
                _physical_database_revision(backup)
                if physical_revision
                else _database_revision(backup)
            )
            if backup_revision != expected_content_revision:
                raise SampleLibraryError(
                    "Sample library changed after health check; the current database was preserved"
                )
        if not self.path.exists():
            self._replace_orphaned_sidecars(backup)
            health = self.check_health()
            if health.status != "healthy" or not health.exists:
                raise SampleLibraryError(
                    f"Rebuilt sample library failed verification ({health.status}): {health.detail}"
                )
            return backup
        if physical_revision:
            self._replace_corrupt_database(backup, physical_revision=True)
        else:
            try:
                self._reset_valid_database(
                    backup,
                    expected_ownership_revision=expected_ownership_revision,
                )
            except sqlite3.Error as exc:
                if _sqlite_error_status(exc) == "corrupt":
                    self._replace_corrupt_database(
                        backup,
                        expected_ownership_revision=expected_ownership_revision,
                    )
                else:
                    status = _sqlite_error_status(exc)
                    raise SampleLibraryError(
                        f"Could not lock sample library for recovery ({status}); the current database was preserved: {exc}"
                    ) from exc
        health = self.check_health()
        if health.status != "healthy" or not health.exists:
            detail = f": {health.detail}" if health.detail else ""
            raise SampleLibraryError(
                f"Rebuilt sample library failed verification ({health.status}){detail}"
            )
        return backup

    def _reset_valid_database(
        self,
        backup: Path,
        *,
        expected_ownership_revision: str | None = None,
    ) -> None:
        connection = sqlite3.connect(self.path, timeout=0, isolation_level=None)
        try:
            connection.execute("begin exclusive")
            if (
                expected_ownership_revision is not None
                and _database_ownership_revision(self.path) != expected_ownership_revision
            ):
                raise SampleLibraryError(
                    "Sample library changed during recovery; the current database was preserved"
                )
            current_digest = _database_content_digest_from_connection(connection)
            backup_digest = _database_content_digest(backup)
            if current_digest != backup_digest:
                raise SampleLibraryError("Sample library changed during recovery; the current database was preserved")
            _drop_user_schema(connection)
            _ensure_schema(connection)
            connection.commit()
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def _replace_corrupt_database(
        self,
        backup: Path,
        *,
        physical_revision: bool = False,
        expected_ownership_revision: str | None = None,
    ) -> None:
        with _canonical_database_replacement_guard(self.path) as guard_acquired:
            if not guard_acquired:
                raise SampleLibraryError(
                    "Sample library is in use during recovery; the current database was preserved"
                )
            if (
                expected_ownership_revision is not None
                and _database_ownership_revision(self.path) != expected_ownership_revision
            ):
                raise SampleLibraryError(
                    "Sample library changed during recovery; the current database was preserved"
                )
            revision = _physical_database_revision if physical_revision else _database_revision
            if revision(self.path) != revision(backup):
                raise SampleLibraryError(
                    "Sample library changed during recovery; the current database was preserved"
                )
            temp_path: Path | None = None
            temp_cleanup_entry: tuple[Path, _OwnedPathState] | None = None
            staged_sidecars: list[_DetachedPath] = []
            staged_main: _DetachedPath | None = None
            staged_main_placeholder: tuple[Path, _OwnedPathState] | None = None
            published_main_entry: tuple[Path, _OwnedPathState] | None = None
            replacement_succeeded = False
            replacement_error: Exception | None = None
            restore_error: SampleLibraryError | None = None
            cleanup_error: SampleLibraryError | None = None
            staged_cleanup_entries: list[tuple[Path, _OwnedPathState]] = []
            try:
                descriptor, temp_name = tempfile.mkstemp(
                    prefix=f".{self.path.name}.",
                    suffix=".tmp",
                    dir=self.path.parent,
                )
                os.close(descriptor)
                temp_path = Path(temp_name)
                with _connect(temp_path) as connection:
                    _ensure_schema(connection)
                temp_cleanup_entry = (temp_path, _owned_path_state(temp_path))
                staged_sidecars = _detach_database_sidecars(self.path)
                if physical_revision:
                    current_revision = _physical_database_revision_with_staged_sidecars(
                        self.path,
                        staged_sidecars,
                    )
                    if current_revision != _physical_database_revision(backup):
                        raise SampleLibraryError(
                            "Sample library changed during recovery; the current database was preserved"
                        )
                if any(sidecar.exists() for sidecar in _database_sidecars(self.path)):
                    raise SampleLibraryError(
                        "Sample-library sidecars appeared during recovery; the current database was preserved"
                    )
                approved_detached_revision = _physical_database_revision_with_staged_sidecars(
                    self.path,
                    staged_sidecars,
                )
                if approved_detached_revision != _physical_database_revision(backup):
                    raise SampleLibraryError(
                        "Sample library changed during recovery; the current database was preserved"
                    )
                descriptor, staged_main_name = tempfile.mkstemp(
                    prefix=f".{self.path.name}.retained.",
                    suffix=".tmp",
                    dir=self.path.parent,
                )
                os.close(descriptor)
                staged_main_path = Path(staged_main_name)
                staged_main_placeholder = (
                    staged_main_path,
                    _owned_path_state(staged_main_path),
                )
                approved_main_state = _owned_path_state(self.path)
                _retry_transient_windows_file_operation(
                    lambda: os.replace(self.path, staged_main_path),
                    unchanged=lambda: (
                        self.path.exists()
                        and staged_main_path.exists()
                        and _owned_path_state(staged_main_path) == staged_main_placeholder[1]
                        and _owned_path_state(self.path) == approved_main_state
                        and _physical_database_revision_with_staged_sidecars(
                            self.path,
                            staged_sidecars,
                        )
                        == approved_detached_revision
                    ),
                    changed_message="Sample library changed while Windows was retrying main-file detachment",
                )
                staged_main = _DetachedPath(self.path, staged_main_path, approved_main_state)
                if _owned_path_state(staged_main_path) != approved_main_state:
                    raise SampleLibraryError(
                        "Sample-library main file changed during detachment; "
                        f"preserved retained path at {staged_main_path}"
                    )
                if (
                    _physical_database_revision_with_staged_main(
                        self.path,
                        staged_main_path,
                        staged_sidecars,
                    )
                    != _physical_database_revision(backup)
                ):
                    raise SampleLibraryError(
                        "Sample library changed during recovery; the current database was preserved"
                    )
                if _owned_path_state(staged_main.staged) != staged_main.approved_state or any(
                    _owned_path_state(record.staged) != record.approved_state
                    for record in staged_sidecars
                ):
                    raise SampleLibraryError(
                        "Sample-library staged content changed during recovery; retained paths were preserved"
                    )
                if self.path.exists() or any(
                    sidecar.exists() for sidecar in _database_sidecars(self.path)
                ):
                    raise SampleLibraryError(
                        "Sample-library state appeared during recovery; the concurrent state was preserved"
                    )
                staged_cleanup_entries = [
                    (record.staged, record.approved_state)
                    for record in staged_sidecars
                ]
                staged_cleanup_entries.append((staged_main.staged, staged_main.approved_state))
                temp_identity = temp_cleanup_entry[1]
                os.link(temp_path, self.path)
                published_main_entry = (self.path, temp_identity)
                if _owned_path_state(self.path) != temp_identity:
                    raise SampleLibraryError(
                        "Published sample-library main file changed during recovery"
                    )
                if any(sidecar.exists() for sidecar in _database_sidecars(self.path)):
                    raise SampleLibraryError(
                        "Sample-library sidecar appeared during recovery; the concurrent state was preserved"
                    )
                replacement_succeeded = True
            except (OSError, sqlite3.Error, SampleLibraryError) as exc:
                replacement_error = exc
            finally:
                if not replacement_succeeded:
                    if published_main_entry is not None:
                        retained = _cleanup_recovery_owned_paths([published_main_entry])
                        if retained:
                            cleanup_error = SampleLibraryError(
                                "Sample-library recovery cleanup incomplete; retained paths: "
                                + ", ".join(str(path) for path in retained)
                            )
                    try:
                        restore_pairs = list(staged_sidecars)
                        if staged_main is not None:
                            restore_pairs.append(staged_main)
                        _restore_database_sidecars(restore_pairs)
                    except SampleLibraryError as exc:
                        restore_error = exc
                else:
                    retained = _cleanup_recovery_owned_paths(staged_cleanup_entries)
                    if retained:
                        cleanup_error = SampleLibraryError(
                            "Sample-library recovery cleanup incomplete; retained paths: "
                            + ", ".join(str(path) for path in retained)
                        )
                if staged_main is None and staged_main_placeholder is not None:
                    retained = _cleanup_published_paths([staged_main_placeholder])
                    if retained:
                        cleanup_error = SampleLibraryError(
                            "Sample-library recovery cleanup incomplete; retained paths: "
                            + ", ".join(str(path) for path in retained)
                        )
                if temp_cleanup_entry is not None:
                    retained = _cleanup_published_paths([temp_cleanup_entry])
                    if retained:
                        temp_error = SampleLibraryError(
                            "Sample-library replacement-temp cleanup incomplete; retained paths: "
                            + ", ".join(str(path) for path in retained)
                        )
                        cleanup_error = (
                            temp_error
                            if cleanup_error is None
                            else SampleLibraryError(f"{cleanup_error}; {temp_error}")
                        )
            if replacement_error is not None:
                message = f"Could not replace sample library with an empty database: {replacement_error}"
                if restore_error is not None:
                    message = f"{message}; sidecar restoration also failed: {restore_error}"
                if cleanup_error is not None:
                    message = f"{message}; recovery cleanup also failed: {cleanup_error}"
                raise SampleLibraryError(message) from replacement_error
            if restore_error is not None:
                raise restore_error
            if cleanup_error is not None:
                raise cleanup_error

    def _replace_orphaned_sidecars(self, backup: Path) -> None:
        approved_revision = _physical_database_revision(backup)
        if _physical_database_revision(self.path) != approved_revision:
            raise SampleLibraryError(
                "Sample library changed during recovery; the current database was preserved"
            )
        temp_path: Path | None = None
        temp_cleanup_entry: tuple[Path, _OwnedPathState] | None = None
        staged_sidecars: list[_DetachedPath] = []
        replacement_succeeded = False
        replacement_error: Exception | None = None
        restore_error: SampleLibraryError | None = None
        cleanup_error: SampleLibraryError | None = None
        staged_cleanup_entries: list[tuple[Path, _OwnedPathState]] = []
        published_main_entry: tuple[Path, _OwnedPathState] | None = None
        try:
            descriptor, temp_name = tempfile.mkstemp(
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                dir=self.path.parent,
            )
            os.close(descriptor)
            temp_path = Path(temp_name)
            with _connect(temp_path) as connection:
                _ensure_schema(connection)
            temp_cleanup_entry = (temp_path, _owned_path_state(temp_path))
            staged_sidecars = _detach_database_sidecars(self.path)
            if (
                _physical_database_revision_with_staged_sidecars(self.path, staged_sidecars)
                != approved_revision
            ):
                raise SampleLibraryError(
                    "Sample library changed during recovery; the current database was preserved"
                )
            if any(
                _owned_path_state(record.staged) != record.approved_state
                for record in staged_sidecars
            ):
                raise SampleLibraryError(
                    "Sample-library staged content changed during recovery; retained paths were preserved"
                )
            if self.path.exists():
                raise SampleLibraryError(
                    "Sample library appeared during recovery; the current database was preserved"
                )
            staged_cleanup_entries = [
                (record.staged, record.approved_state)
                for record in staged_sidecars
            ]
            temp_identity = temp_cleanup_entry[1]
            os.link(temp_path, self.path)
            published_main_entry = (self.path, temp_identity)
            if _owned_path_state(self.path) != temp_identity:
                raise SampleLibraryError(
                    "Published sample-library main file changed during recovery"
                )
            if any(sidecar.exists() for sidecar in _database_sidecars(self.path)):
                raise SampleLibraryError(
                    "Sample-library sidecar appeared during recovery; "
                    "the concurrent state was preserved"
                )
            replacement_succeeded = True
        except (OSError, sqlite3.Error, SampleLibraryError) as exc:
            replacement_error = exc
        finally:
            if not replacement_succeeded:
                if published_main_entry is not None:
                    retained = _cleanup_recovery_owned_paths([published_main_entry])
                    if retained:
                        cleanup_error = SampleLibraryError(
                            "Sample-library recovery cleanup incomplete; retained paths: "
                            + ", ".join(str(path) for path in retained)
                        )
                try:
                    _restore_database_sidecars(staged_sidecars)
                except SampleLibraryError as exc:
                    restore_error = exc
            else:
                retained = _cleanup_recovery_owned_paths(staged_cleanup_entries)
                if retained:
                    cleanup_error = SampleLibraryError(
                        "Sample-library recovery cleanup incomplete; retained paths: "
                        + ", ".join(str(path) for path in retained)
                    )
            if temp_cleanup_entry is not None:
                retained = _cleanup_published_paths([temp_cleanup_entry])
                if retained:
                    temp_error = SampleLibraryError(
                        "Sample-library replacement-temp cleanup incomplete; retained paths: "
                        + ", ".join(str(path) for path in retained)
                    )
                    cleanup_error = (
                        temp_error
                        if cleanup_error is None
                        else SampleLibraryError(f"{cleanup_error}; {temp_error}")
                    )
        if replacement_error is not None:
            message = (
                "Could not replace orphaned sample-library sidecars with an empty database: "
                f"{replacement_error}"
            )
            if restore_error is not None:
                message = f"{message}; sidecar restoration also failed: {restore_error}"
            if cleanup_error is not None:
                message = f"{message}; published-main cleanup also failed: {cleanup_error}"
            raise SampleLibraryError(message) from replacement_error
        if restore_error is not None:
            raise restore_error
        if cleanup_error is not None:
            raise cleanup_error

    def _backup_existing_database(
        self,
        target: Path | None = None,
        *,
        force_physical: bool = False,
    ) -> Path:
        target = target or self.planned_backup_path()
        final_paths = (target, *_database_sidecars(target))
        staged_pairs: list[tuple[Path, Path]] = []
        staged_paths: list[Path] = []
        staged_cleanup_entries: dict[Path, _OwnedPathState] = {}
        published_paths: list[tuple[Path, _OwnedPathState]] = []

        def new_stage_path(label: str) -> Path:
            descriptor, temp_name = tempfile.mkstemp(
                prefix=f".{target.name}.",
                suffix=f".{label}.tmp",
                dir=self.backups_dir,
            )
            os.close(descriptor)
            path = Path(temp_name)
            staged_paths.append(path)
            staged_cleanup_entries[path] = _owned_path_state(path)
            return path

        def refresh_stage_identity(path: Path, *, expected_digest: str | None = None) -> None:
            previous = staged_cleanup_entries[path]
            current = _owned_path_state(path)
            if current.identity[:2] != previous.identity[:2]:
                raise BackupError(f"Backup staging path changed concurrently: {path}")
            staged_cleanup_entries[path] = current
            if expected_digest is not None and current.digest != expected_digest:
                raise BackupError(f"Backup staging content does not match its source: {path}")

        def copy_to_stage(source: Path, staged: Path) -> None:
            source_state = _owned_path_state(source)
            try:
                self.copy_file(source, staged)
            finally:
                if staged.exists():
                    refresh_stage_identity(staged)
            if _owned_path_state(source) != source_state:
                raise BackupError(f"Sample-library source changed during backup: {source}")
            if staged_cleanup_entries[staged].digest != source_state.digest:
                raise BackupError(f"Backup staging content does not match its source: {staged}")

        def cleanup_error(paths: Iterable[Path]) -> str | None:
            entries = [
                (path, staged_cleanup_entries[path])
                for path in paths
                if path in staged_cleanup_entries
            ]
            residues = _cleanup_published_paths(entries)
            if not residues:
                return None
            return "cleanup incomplete; retained owned paths: " + ", ".join(str(path) for path in residues)

        try:
            self.backups_dir.mkdir(parents=True, exist_ok=True)
            existing = next((path for path in final_paths if path.exists()), None)
            if existing is not None:
                raise BackupError(f"Backup already exists: {existing}")
            coherent_backup = False
            if self.path.exists() and not force_physical:
                temp_path = new_stage_path("main")
                try:
                    with _connect_read_only(self.path) as source:
                        destination = sqlite3.connect(temp_path)
                        try:
                            _bounded_backup(source, destination)
                            if destination.execute("pragma quick_check").fetchall() != [("ok",)]:
                                raise sqlite3.DatabaseError("coherent backup failed quick_check")
                            destination.commit()
                        finally:
                            destination.close()
                    refresh_stage_identity(temp_path)
                    coherent_backup = True
                except sqlite3.Error as exc:
                    if _sqlite_error_status(exc) != "corrupt":
                        raise BackupError(
                            f"Could not create coherent sample-library backup {target}: {exc}"
                        ) from exc
            if coherent_backup:
                staged = new_stage_path("publish-main")
                copy_to_stage(temp_path, staged)
                with _connect_read_only(staged) as backup:
                    if backup.execute("pragma quick_check").fetchall() != [("ok",)]:
                        raise sqlite3.DatabaseError("staged backup failed quick_check")
                staged_pairs.append((staged, target))
            else:
                for index, (source_sidecar, target_sidecar) in enumerate(zip(
                    _database_sidecars(self.path), _database_sidecars(target)
                )):
                    if source_sidecar.exists():
                        staged = new_stage_path(f"sidecar-{index}")
                        copy_to_stage(source_sidecar, staged)
                        staged_pairs.append((staged, target_sidecar))
                if self.path.exists():
                    staged = new_stage_path("main")
                    copy_to_stage(self.path, staged)
                    staged_pairs.append((staged, target))

            for staged, final in staged_pairs:
                staged_identity = staged_cleanup_entries[staged]
                if _owned_path_state(staged) != staged_identity:
                    raise BackupError(f"Backup staging content changed concurrently: {staged}")
                os.link(staged, final)
                published_paths.append((final, staged_identity))
                if _owned_path_state(final) != staged_identity:
                    raise BackupError(f"Published backup component changed concurrently: {final}")
        except (BackupError, OSError, sqlite3.Error) as exc:
            published_residues = _cleanup_published_paths(reversed(published_paths))
            cleanup = cleanup_error(staged_paths)
            if published_residues:
                published_cleanup = "cleanup incomplete; retained owned paths: " + ", ".join(
                    str(path) for path in published_residues
                )
                cleanup = f"{cleanup}; {published_cleanup}" if cleanup else published_cleanup
            message = str(exc) if isinstance(exc, BackupError) else f"Could not create sample-library backup {target}: {exc}"
            if cleanup:
                message = f"{message}; {cleanup}"
            raise BackupError(message) from exc

        cleanup = cleanup_error(staged_paths)
        if cleanup:
            raise BackupError(f"Created sample-library backup {target}, but {cleanup}")
        return target


def _require_compatible_schema(connection: sqlite3.Connection) -> None:
    schema_rows = connection.execute(
        "select type, name, tbl_name, sql from sqlite_schema "
        "where lower(name) not glob 'sqlite_*' order by type, name"
    ).fetchall()
    table_rows = [row for row in schema_rows if row[0] == "table"]
    if len(table_rows) != 1 or str(table_rows[0][1]).lower() != "sample_records":
        raise SampleLibraryError("Existing sample library schema is incompatible")
    table_sql = table_rows[0][3] or ""
    if (
        _UNSUPPORTED_TABLE_SQL.search(table_sql)
        or _schema_signature(table_sql) != _schema_signature(_EXPECTED_TABLE_SQL)
    ):
        raise SampleLibraryError("Existing sample library schema is incompatible")
    for object_type, name, table_name, _sql in schema_rows:
        if object_type == "table":
            continue
        if object_type != "index" or str(table_name).lower() != "sample_records":
            raise SampleLibraryError("Existing sample library schema is incompatible")
        if not _is_unique_identity_index(connection, name):
            raise SampleLibraryError("Existing sample library schema is incompatible")
    if connection.execute("pragma foreign_key_list(sample_records)").fetchall():
        raise SampleLibraryError("Existing sample library schema is incompatible")

    rows = connection.execute("pragma table_xinfo(sample_records)").fetchall()
    if len(rows) != len(_EXPECTED_COLUMNS):
        raise SampleLibraryError("Existing sample library schema is incompatible")
    for row in rows:
        expected = _EXPECTED_COLUMNS.get(row[1])
        actual = (
            str(row[2]).strip().upper(),
            bool(row[3]),
            _normalize_default(row[4]),
            int(row[5]),
        )
        if expected is None or actual != expected or int(row[6]) != 0:
            raise SampleLibraryError("Existing sample library schema is incompatible")
    if not _has_unique_identity_index(connection):
        raise SampleLibraryError("Existing sample library schema is incompatible")


def _has_unique_identity_index(connection: sqlite3.Connection) -> bool:
    for row in connection.execute("pragma index_list(sample_records)").fetchall():
        if _is_unique_identity_index(connection, row[1], index_list_row=row):
            return True
    return False


def _is_unique_identity_index(
    connection: sqlite3.Connection,
    index_name: str,
    *,
    index_list_row=None,
) -> bool:
    if index_list_row is None:
        index_list_row = next(
            (
                row
                for row in connection.execute("pragma index_list(sample_records)").fetchall()
                if row[1] == index_name
            ),
            None,
        )
    if index_list_row is None or not bool(index_list_row[2]) or bool(index_list_row[4]):
        return False
    key_columns = [
        (info[0], info[1])
        for info in connection.execute(
            "select name, coll from pragma_index_xinfo(?) where key = 1 order by seqno",
            (index_name,),
        ).fetchall()
    ]
    return key_columns == [("identity_json", "BINARY")]


def _normalize_default(value) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    while normalized.startswith("(") and normalized.endswith(")"):
        normalized = normalized[1:-1].strip()
    return normalized


def _schema_signature(sql: str) -> tuple[str, ...]:
    return tuple(re.findall(r"[a-z_][a-z0-9_]*|\d+|[^\s]", sql.lower()))


def _database_sidecars(path: Path) -> tuple[Path, ...]:
    return tuple(Path(f"{path}{suffix}") for suffix in _DATABASE_SIDECAR_SUFFIXES)


@contextmanager
def _canonical_database_shared_lock(path: Path):
    if os.name != "nt":
        yield True
        return

    import ctypes
    from ctypes import wintypes

    class Overlapped(ctypes.Structure):
        _fields_ = (
            ("Internal", ctypes.c_size_t),
            ("InternalHigh", ctypes.c_size_t),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        )

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.LockFileEx.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(Overlapped),
    )
    kernel32.LockFileEx.restype = wintypes.BOOL
    kernel32.UnlockFileEx.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(Overlapped),
    )
    kernel32.UnlockFileEx.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)

    handle = kernel32.CreateFileW(
        str(path),
        0x80000000,
        0x00000001 | 0x00000002 | 0x00000004,
        None,
        3,
        0x00000080,
        None,
    )
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())

    shared_lock = Overlapped()
    shared_lock.Offset = 0x40000002
    acquired = False
    try:
        if kernel32.LockFileEx(handle, 0x00000001, 0, 1, 0, ctypes.byref(shared_lock)):
            acquired = True
            yield True
        else:
            error = ctypes.get_last_error()
            if error in {32, 33}:
                yield False
            else:
                raise ctypes.WinError(error)
    finally:
        if acquired:
            kernel32.UnlockFileEx(handle, 0, 1, 0, ctypes.byref(shared_lock))
        kernel32.CloseHandle(handle)


@contextmanager
def _canonical_database_replacement_guard(path: Path):
    if os.name != "nt":
        yield True
        return

    import ctypes
    from ctypes import wintypes

    class Overlapped(ctypes.Structure):
        _fields_ = (
            ("Internal", ctypes.c_size_t),
            ("InternalHigh", ctypes.c_size_t),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        )

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.LockFileEx.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(Overlapped),
    )
    kernel32.LockFileEx.restype = wintypes.BOOL
    kernel32.UnlockFileEx.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(Overlapped),
    )
    kernel32.UnlockFileEx.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)

    lock_path = path.with_name(f".{path.name}.replacement.lock")
    lock_handle = kernel32.CreateFileW(
        str(lock_path),
        0x80000000 | 0x40000000,
        0x00000001 | 0x00000002,
        None,
        4,
        0x00000080,
        None,
    )
    if lock_handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())

    replacement_lock = Overlapped()
    replacement_lock_acquired = False
    handle = None
    try:
        if not kernel32.LockFileEx(
            lock_handle,
            0x00000001 | 0x00000002,
            0,
            1,
            0,
            ctypes.byref(replacement_lock),
        ):
            error = ctypes.get_last_error()
            if error in {32, 33}:
                yield False
                return
            raise ctypes.WinError(error)
        replacement_lock_acquired = True

        handle = kernel32.CreateFileW(
            str(path),
            0x80000000,
            0x00000001 | 0x00000004,
            None,
            3,
            0x00000080,
            None,
        )
        if handle != ctypes.c_void_p(-1).value:
            yield True
            return
        error = ctypes.get_last_error()
        if error in {32, 33}:
            yield False
            return
        raise ctypes.WinError(error)
    finally:
        if handle not in {None, ctypes.c_void_p(-1).value}:
            kernel32.CloseHandle(handle)
        if replacement_lock_acquired:
            kernel32.UnlockFileEx(
                lock_handle,
                0,
                1,
                0,
                ctypes.byref(replacement_lock),
            )
        kernel32.CloseHandle(lock_handle)


def _replace_database_file(source: Path, target: Path) -> None:
    if os.name != "nt":
        os.replace(source, target)
        return

    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.ReplaceFileW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
    )
    kernel32.ReplaceFileW.restype = wintypes.BOOL
    if not kernel32.ReplaceFileW(str(target), str(source), None, 0x00000001, None, None):
        raise ctypes.WinError(ctypes.get_last_error())


@contextmanager
def _health_check_snapshot(path: Path, *, temp_root: Path, cancel_check=None):
    before = _database_file_snapshot(path, cancel_check=cancel_check)
    temp_root = Path(temp_root)
    run_id = f"sample-library-health-{uuid.uuid4().hex}"
    run_root = temp_root / run_id
    try:
        ownership = create_run_ownership_at_root(
            run_root,
            run_id,
            _HEALTH_SNAPSHOT_MARKER_ID,
            [path.parent / name for name in before],
        )
    except (OSError, OwnershipError) as exc:
        try:
            run_root.rmdir()
        except OSError:
            pass
        raise SampleLibraryError(f"Could not create owned sample-library health snapshot: {exc}") from exc

    primary_error: BaseException | None = None
    primary_traceback = None
    try:
        allowed_names = dict.fromkeys(
            (
                *before,
                path.name,
                *(f"{path.name}{suffix}" for suffix in _DATABASE_SIDECAR_SUFFIXES),
            )
        )
        for name in allowed_names:
            ownership = add_allowed_child(ownership, run_root / name)
        for name in before:
            _check_cancel(cancel_check)
            target = run_root / name

            def bind_created_snapshot(_path, identity):
                nonlocal ownership
                ownership = bind_allowed_child_identity(
                    ownership,
                    target,
                    expected_identity=identity,
                )

            _copy_health_snapshot_component(
                path.parent / name,
                target,
                cancel_check=cancel_check,
                creation_callback=bind_created_snapshot,
            )
        for suffix in ("-wal", "-shm"):
            snapshot_sidecar = run_root / f"{path.name}{suffix}"
            if snapshot_sidecar.exists():
                continue
            with create_exclusive_held_file(
                snapshot_sidecar,
                share_write=True,
            ) as (_stream, identity):
                ownership = bind_allowed_child_identity(
                    ownership,
                    snapshot_sidecar,
                    expected_identity=identity,
                )
        if _database_file_snapshot(path, cancel_check=cancel_check) != before:
            raise sqlite3.OperationalError("sample library is busy or changed during health check")
        yield run_root / path.name
        if _database_file_snapshot(path, cancel_check=cancel_check) != before:
            raise sqlite3.OperationalError("sample library is busy or changed during health check")
    except BaseException as exc:
        primary_error = exc
        primary_traceback = exc.__traceback__

    cleanup_error: _HealthSnapshotCleanupError | None = None
    try:
        cleanup_owned_temp_root(
            run_root,
            expected_root_identity=ownership.temp_root_identity,
        )
    except (OSError, OwnershipError) as exc:
        cleanup_error = _HealthSnapshotCleanupError(
            f"Could not clean owned sample-library health snapshot {run_root}: {exc}"
        )

    if primary_error is not None:
        if cleanup_error is not None:
            if isinstance(primary_error, (sqlite3.Error, OSError, SampleLibraryError)):
                raise cleanup_error from primary_error.with_traceback(primary_traceback)
            primary_error.add_note(str(cleanup_error))
        raise primary_error.with_traceback(primary_traceback)
    if cleanup_error is not None:
        raise cleanup_error


def _copy_health_snapshot_component(
    source: Path,
    target: Path,
    *,
    cancel_check=None,
    creation_callback,
) -> None:
    _check_cancel(cancel_check)
    with source.open("rb", buffering=0) as reader:
        with create_exclusive_held_file(
            target,
            share_write=False,
        ) as (writer, identity):
            while chunk := reader.read(1024 * 1024):
                _check_cancel(cancel_check)
                writer.write(chunk)
            writer.flush()
            os.fsync(writer.fileno())
            shutil.copystat(source, target)
            creation_callback(target, identity)
    _check_cancel(cancel_check)


def _existing_database_components(path: Path) -> tuple[Path, ...]:
    return tuple(
        candidate
        for candidate in (path, *_database_sidecars(path))
        if candidate.exists()
    )


def _database_file_snapshot(
    path: Path,
    *,
    cancel_check=None,
) -> dict[str, tuple[int, int, int, int, str]]:
    snapshot: dict[str, tuple[int, int, int, int, str]] = {}
    for candidate in (path, *_database_sidecars(path)):
        try:
            if not candidate.exists():
                continue
            state = _owned_path_state(candidate, cancel_check=cancel_check)
        except OSError as exc:
            raise SampleLibraryError(
                f"Could not verify sample library during recovery: {exc}"
            ) from exc
        snapshot[candidate.name] = (*state.identity, state.digest)
    return snapshot


def _database_content_digest(path: Path, *, cancel_check=None) -> str:
    try:
        with _connect_read_only(path) as connection:
            return _database_content_digest_from_connection(
                connection,
                cancel_check=cancel_check,
            )
    except sqlite3.Error as exc:
        if _sqlite_error_status(exc) != "corrupt" or any(sidecar.exists() for sidecar in _database_sidecars(path)):
            raise SampleLibraryError(f"Could not verify sample library during recovery: {exc}") from exc
        try:
            return _owned_path_state(path, cancel_check=cancel_check).digest
        except (OSError, SampleLibraryError) as io_exc:
            raise SampleLibraryError(
                f"Could not verify sample library during recovery: {io_exc}"
            ) from io_exc


def _database_revision(path: Path, *, cancel_check=None) -> str:
    try:
        return f"logical:{_database_content_digest(path, cancel_check=cancel_check)}"
    except SampleLibraryError:
        return _physical_database_revision(path, cancel_check=cancel_check)


def _split_expected_revision(revision: str | None) -> tuple[str | None, str | None]:
    if revision is None or _OWNERSHIP_REVISION_SEPARATOR not in revision:
        return revision, None
    content_revision, ownership_revision = revision.split(
        _OWNERSHIP_REVISION_SEPARATOR,
        1,
    )
    return content_revision, ownership_revision


def _database_ownership_revision(path: Path, *, cancel_check=None) -> str:
    digest = hashlib.sha256()
    found = False
    try:
        for component, candidate in zip(
            ("main", *_DATABASE_SIDECAR_SUFFIXES),
            (path, *_database_sidecars(path)),
        ):
            if not candidate.exists():
                continue
            found = True
            state = _owned_path_state(candidate, cancel_check=cancel_check)
            digest.update(component.encode("ascii"))
            digest.update(repr(state.identity).encode("ascii"))
            digest.update(state.digest.encode("ascii"))
    except OSError as exc:
        raise SampleLibraryError(
            f"Could not verify sample library during recovery: {exc}"
        ) from exc
    if not found:
        raise SampleLibraryError("Cannot calculate ownership revision for missing sample library")
    return digest.hexdigest()


def _physical_database_revision(path: Path, *, cancel_check=None) -> str:
    return _physical_revision_for_components(
        (path, *_database_sidecars(path)),
        cancel_check=cancel_check,
    )


def _physical_database_revision_with_staged_sidecars(
    path: Path,
    staged_sidecars: Iterable[_DetachedPath | tuple[Path, Path]],
) -> str:
    staged_by_source = dict(staged_sidecars)
    return _physical_revision_for_components(
        (
            path,
            *(staged_by_source.get(sidecar, sidecar) for sidecar in _database_sidecars(path)),
        ),
    )


def _physical_database_revision_with_staged_main(
    path: Path,
    staged_main: Path,
    staged_sidecars: Iterable[_DetachedPath | tuple[Path, Path]],
) -> str:
    staged_by_source = dict(staged_sidecars)
    return _physical_revision_for_components(
        (
            staged_main,
            *(staged_by_source.get(sidecar, sidecar) for sidecar in _database_sidecars(path)),
        ),
    )


def _physical_revision_for_components(candidates: tuple[Path, ...], *, cancel_check=None) -> str:
    digest = hashlib.sha256()
    found = False
    for component, candidate in zip(
        ("main", *_DATABASE_SIDECAR_SUFFIXES),
        candidates,
    ):
        try:
            if not candidate.exists():
                continue
            state = _owned_path_state(candidate, cancel_check=cancel_check)
        except OSError as exc:
            raise SampleLibraryError(
                f"Could not verify sample library during recovery: {exc}"
            ) from exc
        found = True
        digest.update(component.encode("ascii"))
        digest.update(state.identity[2].to_bytes(8, "big"))
        digest.update(state.digest.encode("ascii"))
    if not found:
        raise SampleLibraryError("Cannot calculate revision for missing sample library")
    return f"physical:{digest.hexdigest()}"


def _drop_user_schema(connection: sqlite3.Connection) -> None:
    rows = connection.execute(
        "select type, name from sqlite_schema where lower(name) not glob 'sqlite_*' "
        "order by case type "
        "when 'trigger' then 0 when 'view' then 1 when 'index' then 2 when 'table' then 3 else 4 end"
    ).fetchall()
    for object_type, name in rows:
        if object_type not in {"trigger", "view", "index", "table"}:
            raise SampleLibraryError(
                f"Unsupported sample-library schema object during recovery: {object_type}"
            )
        quoted_name = '"' + str(name).replace('"', '""') + '"'
        connection.execute(f"drop {object_type} if exists {quoted_name}")


def _database_content_digest_from_connection(connection: sqlite3.Connection, *, cancel_check=None) -> str:
    return sqlite_content_sha256(connection, cancel_check=cancel_check)


def _bounded_backup(source: sqlite3.Connection, destination: sqlite3.Connection) -> None:
    lock_started: float | None = None

    def progress(status: int, _remaining: int, _total: int) -> None:
        nonlocal lock_started
        if status not in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
            lock_started = None
            return
        now = time.monotonic()
        if lock_started is None:
            lock_started = now
        elif now - lock_started >= _BACKUP_LOCK_TIMEOUT_SECONDS:
            raise sqlite3.OperationalError("sample-library backup timed out while the database was locked")

    source.backup(destination, pages=64, progress=progress, sleep=0.01)


def _detach_database_sidecars(path: Path) -> list[_DetachedPath]:
    detached: list[_DetachedPath] = []
    try:
        for sidecar in _database_sidecars(path):
            if not sidecar.exists():
                continue
            descriptor, temp_name = tempfile.mkstemp(
                prefix=f".{sidecar.name}.",
                suffix=".tmp",
                dir=sidecar.parent,
            )
            os.close(descriptor)
            staged_path = Path(temp_name)
            placeholder_state = _owned_path_state(staged_path)
            approved_sidecar_state = _owned_path_state(sidecar)
            approved_revision = _physical_database_revision_with_staged_sidecars(path, detached)
            try:
                _retry_transient_windows_file_operation(
                    lambda: os.replace(sidecar, staged_path),
                    unchanged=lambda: (
                        staged_path.exists()
                        and _owned_path_state(staged_path) == placeholder_state
                        and _owned_path_state(sidecar) == approved_sidecar_state
                        and _physical_database_revision_with_staged_sidecars(path, detached)
                        == approved_revision
                    ),
                    changed_message="Sample library changed while Windows was retrying sidecar detachment",
                )
            except (OSError, SampleLibraryError) as exc:
                retained = _cleanup_published_paths([(staged_path, placeholder_state)])
                if retained:
                    raise SampleLibraryError(
                        f"Sidecar detachment failed: {exc}; placeholder cleanup incomplete; "
                        "retained paths: " + ", ".join(str(item) for item in retained)
                    ) from exc
                raise
            record = _DetachedPath(sidecar, staged_path, approved_sidecar_state)
            detached.append(record)
            if _owned_path_state(staged_path) != approved_sidecar_state:
                raise SampleLibraryError(
                    "Sample-library sidecar changed during detachment; "
                    f"preserved retained path at {staged_path}"
                )
    except (OSError, SampleLibraryError) as detach_error:
        try:
            _restore_database_sidecars(detached)
        except SampleLibraryError as restore_error:
            raise SampleLibraryError(
                f"Sidecar detachment failed: {detach_error}; "
                f"sidecar restoration also failed: {restore_error}"
            ) from detach_error
        raise
    return detached


def _restore_database_sidecars(
    sidecars: Iterable[_DetachedPath | tuple[Path, Path]],
) -> None:
    errors: list[str] = []
    records = list(sidecars)
    for item in reversed(records):
        if isinstance(item, _DetachedPath):
            record = item
            sidecar = record.source
            staged_path = record.staged
        else:
            sidecar, staged_path = item
            record = None
        if not staged_path.exists():
            errors.append(
                f"Could not restore sample-library sidecar {sidecar}; "
                f"missing retained path {staged_path}"
            )
            continue
        try:
            approved_identity = (
                record.approved_state
                if record is not None
                else _owned_path_state(staged_path)
            )
            if _owned_path_state(staged_path) != approved_identity:
                errors.append(
                    "Sample-library staged sidecar changed before restoration; "
                    f"preserved retained path at {staged_path}"
                )
                continue
            _retry_transient_windows_file_operation(
                lambda: os.link(staged_path, sidecar),
                unchanged=lambda: (
                    not sidecar.exists()
                    and staged_path.exists()
                    and _owned_path_state(staged_path) == approved_identity
                ),
                changed_message="Sample-library sidecar state changed while Windows was retrying restoration",
            )
            if _owned_path_state(sidecar) != approved_identity:
                removed = _unlink_if_owned_identity_matches(sidecar, approved_identity)
                retained_detail = "" if removed else f"; target path also changed and was preserved at {sidecar}"
                errors.append(
                    "Sample-library staged sidecar changed during restoration; "
                    f"preserved retained path at {staged_path}{retained_detail}"
                )
                continue
        except FileExistsError:
            errors.append(
                f"Sample-library sidecar appeared during recovery; preserved staged data at {staged_path}"
            )
            continue
        except (OSError, SampleLibraryError) as exc:
            errors.append(
                f"Could not restore sample-library sidecar {sidecar} "
                f"from retained path {staged_path}: {exc}"
            )
            continue
        try:
            removed = _unlink_if_owned_identity_matches(staged_path, approved_identity)
            if not removed:
                errors.append(
                    f"Restored sample-library sidecar {sidecar}, but retained path changed "
                    f"and was preserved at {staged_path}"
                )
        except (OSError, SampleLibraryError) as exc:
            errors.append(
                f"Restored sample-library sidecar {sidecar}, but could not remove retained duplicate "
                f"at {staged_path}: {exc}"
            )
    if errors:
        raise SampleLibraryError("; ".join(errors))


def _cleanup_owned_paths(paths: Iterable[Path]) -> tuple[Path, ...]:
    residues: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        if path in seen:
            continue
        seen.add(path)
        try:
            path.unlink(missing_ok=True)
        except (OSError, SampleLibraryError):
            residues.append(path)
    return tuple(residues)


def _identity_from_stat(stat: os.stat_result) -> _PhysicalIdentity:
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)


def _owned_path_identity(path: Path) -> _PhysicalIdentity:
    return _identity_from_stat(path.stat(follow_symlinks=False))


def _owned_path_state(path: Path, *, cancel_check=None) -> _OwnedPathState:
    _check_cancel(cancel_check)
    path_identity = _owned_path_identity(path)
    digest = hashlib.sha256()
    with path.open("rb", buffering=0) as stream:
        before = _identity_from_stat(os.fstat(stream.fileno()))
        if before != path_identity:
            raise SampleLibraryError(f"File changed while its ownership was being recorded: {path}")
        while chunk := stream.read(1024 * 1024):
            _check_cancel(cancel_check)
            digest.update(chunk)
        after = _identity_from_stat(os.fstat(stream.fileno()))
    if after != before:
        raise SampleLibraryError(f"File changed while its ownership was being recorded: {path}")
    return _OwnedPathState(before, digest.hexdigest())


def _check_cancel(cancel_check) -> None:
    if cancel_check is not None:
        cancel_check()


def _wrap_external_cancel(cancel_check):
    if cancel_check is None:
        return None

    def wrapped_cancel_check() -> None:
        try:
            cancel_check()
        except _ExternalControlFlow:
            raise
        except BaseException as exc:
            raise _ExternalControlFlow(exc) from None

    return wrapped_cancel_check


def _run_quick_check(connection, *, cancel_check=None):
    pending_cancel_error: BaseException | None = None

    def check_progress() -> int:
        nonlocal pending_cancel_error
        try:
            _check_cancel(cancel_check)
        except BaseException as exc:
            pending_cancel_error = exc
            return 1
        return 0

    if cancel_check is not None:
        connection.set_progress_handler(
            check_progress,
            _QUICK_CHECK_PROGRESS_HANDLER_STEPS,
        )
    try:
        return connection.execute("pragma quick_check").fetchall()
    except sqlite3.OperationalError:
        if pending_cancel_error is not None:
            raise pending_cancel_error
        raise
    finally:
        if cancel_check is not None:
            connection.set_progress_handler(None, 0)


def _cleanup_published_paths(
    paths: Iterable[tuple[Path, _OwnedPathState | _PhysicalIdentity]],
) -> tuple[Path, ...]:
    residues: list[Path] = []
    for path, approved_identity in paths:
        try:
            if not _unlink_if_owned_identity_matches(path, approved_identity):
                residues.append(path)
        except FileNotFoundError:
            continue
        except OSError:
            residues.append(path)
    return tuple(residues)


def _cleanup_recovery_owned_paths(
    paths: Iterable[tuple[Path, _OwnedPathState | _PhysicalIdentity]],
) -> tuple[Path, ...]:
    residues: list[Path] = []
    for path, approved_identity in paths:
        try:
            if not _unlink_if_owned_identity_matches(path, approved_identity):
                residues.append(path)
        except (FileNotFoundError, OSError, SampleLibraryError):
            residues.append(path)
    return tuple(residues)


def _unlink_if_owned_identity_matches(
    path: Path,
    approved_identity: _OwnedPathState | _PhysicalIdentity,
) -> bool:
    approved_state = approved_identity if isinstance(approved_identity, _OwnedPathState) else None
    approved_physical_identity = (
        approved_state.identity if approved_state is not None else approved_identity
    )
    if os.name != "nt":
        current = _owned_path_state(path) if approved_state is not None else _owned_path_identity(path)
        if current != approved_identity:
            return False
        path.unlink()
        return True

    import ctypes
    from ctypes import wintypes

    class FileId128(ctypes.Structure):
        _fields_ = (("Identifier", ctypes.c_ubyte * 16),)

    class FileIdInfo(ctypes.Structure):
        _fields_ = (
            ("VolumeSerialNumber", ctypes.c_ulonglong),
            ("FileId", FileId128),
        )

    class FileBasicInfo(ctypes.Structure):
        _fields_ = (
            ("CreationTime", ctypes.c_longlong),
            ("LastAccessTime", ctypes.c_longlong),
            ("LastWriteTime", ctypes.c_longlong),
            ("ChangeTime", ctypes.c_longlong),
            ("FileAttributes", wintypes.DWORD),
        )

    class FileStandardInfo(ctypes.Structure):
        _fields_ = (
            ("AllocationSize", ctypes.c_longlong),
            ("EndOfFile", ctypes.c_longlong),
            ("NumberOfLinks", wintypes.DWORD),
            ("DeletePending", wintypes.BOOL),
            ("Directory", wintypes.BOOL),
        )

    class FileDispositionInfo(ctypes.Structure):
        _fields_ = (("DeleteFile", wintypes.BOOL),)

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.GetFileInformationByHandleEx.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    )
    kernel32.GetFileInformationByHandleEx.restype = wintypes.BOOL
    kernel32.SetFileInformationByHandle.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    )
    kernel32.SetFileInformationByHandle.restype = wintypes.BOOL
    kernel32.ReadFile.argtypes = (
        wintypes.HANDLE,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
    )
    kernel32.ReadFile.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)

    handle = kernel32.CreateFileW(
        str(path),
        0x00010000 | 0x00000080 | (0x80000000 if approved_state is not None else 0),
        0x00000001 | 0x00000004 | (0x00000002 if approved_state is None else 0),
        None,
        3,
        0x00000080 | 0x00200000,
        None,
    )
    if handle == ctypes.c_void_p(-1).value:
        error = ctypes.get_last_error()
        if error in {2, 3}:
            raise FileNotFoundError(error, os.strerror(error), str(path))
        raise ctypes.WinError(error)
    try:
        file_id = FileIdInfo()
        basic = FileBasicInfo()
        standard = FileStandardInfo()
        for info_class, info in ((18, file_id), (0, basic), (1, standard)):
            if not kernel32.GetFileInformationByHandleEx(
                handle,
                info_class,
                ctypes.byref(info),
                ctypes.sizeof(info),
            ):
                raise ctypes.WinError(ctypes.get_last_error())
        current_identity = (
            int(file_id.VolumeSerialNumber),
            int.from_bytes(bytes(file_id.FileId.Identifier), "little"),
            int(standard.EndOfFile),
            (int(basic.LastWriteTime) - 116444736000000000) * 100,
        )
        if current_identity != approved_physical_identity:
            return False
        if approved_state is not None:
            digest = hashlib.sha256()
            buffer = (ctypes.c_ubyte * (64 * 1024))()
            while True:
                bytes_read = wintypes.DWORD()
                if not kernel32.ReadFile(
                    handle,
                    buffer,
                    len(buffer),
                    ctypes.byref(bytes_read),
                    None,
                ):
                    raise ctypes.WinError(ctypes.get_last_error())
                if bytes_read.value == 0:
                    break
                digest.update(bytes(buffer[: bytes_read.value]))
            if digest.hexdigest() != approved_state.digest:
                return False
        disposition = FileDispositionInfo(True)
        if not kernel32.SetFileInformationByHandle(
            handle,
            4,
            ctypes.byref(disposition),
            ctypes.sizeof(disposition),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return True
    finally:
        kernel32.CloseHandle(handle)


def _retry_transient_windows_file_operation(
    operation: Callable[[], None],
    *,
    unchanged: Callable[[], bool],
    changed_message: str,
) -> None:
    retry_error: OSError | None = None
    for delay in (*_WINDOWS_FILE_RETRY_DELAYS, None):
        if retry_error is not None and not unchanged():
            raise SampleLibraryError(f"{changed_message}; the current database was preserved") from retry_error
        try:
            operation()
            return
        except OSError as exc:
            error_code = getattr(exc, "winerror", None)
            if os.name != "nt" or error_code not in _TRANSIENT_WINDOWS_FILE_ERRORS or delay is None:
                raise
            retry_error = exc
            time.sleep(delay)


@contextmanager
def _connect(path: Path):
    connection = sqlite3.connect(path)
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


@contextmanager
def _connect_read_only(path: Path):
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=0)
    try:
        yield connection
    finally:
        connection.close()


def _sqlite_error_status(exc: sqlite3.Error) -> str:
    error_code = getattr(exc, "sqlite_errorcode", None)
    primary_code = error_code & 0xFF if isinstance(error_code, int) else None
    if primary_code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
        return "locked"
    if primary_code in {sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB}:
        return "corrupt"
    message = str(exc).lower()
    if "locked" in message or "busy" in message:
        return "locked"
    if "malformed" in message or "not a database" in message:
        return "corrupt"
    return "unreadable"


def _ensure_schema(connection: sqlite3.Connection) -> None:
    connection.execute(_EXPECTED_TABLE_SQL.replace("create table", "create table if not exists", 1))


def _insert_or_get_record(connection: sqlite3.Connection, record) -> int:
    existing = _find_record_id(connection, record)
    if existing is not None:
        return existing
    cursor = connection.execute(
        """
        insert into sample_records (sample_type, identity_json, canonical_label, system_label)
        values (?, ?, ?, ?)
        """,
        (record.sample_type, record.identity_json(), record.canonical_label, record.system_label),
    )
    return int(cursor.lastrowid)


def _find_record_id(connection: sqlite3.Connection, record) -> int | None:
    row = connection.execute(
        "select id from sample_records where identity_json = ?",
        (record.identity_json(),),
    ).fetchone()
    return None if row is None else int(row[0])
