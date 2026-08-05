from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Callable
from uuid import uuid4


class OwnedDirectoryLockError(RuntimeError):
    pass


@dataclass(frozen=True)
class OwnedDirectoryLock:
    path: Path
    gate_path: Path
    owner_filename: str
    owner_token: str
    label: str


OwnershipErrorCheck = Callable[[Path, str], BaseException | None]
ReleaseErrorCleanup = Callable[[], None]


def acquire_owned_directory_lock(
    lock_path: Path,
    *,
    owner_filename: str,
    label: str,
) -> OwnedDirectoryLock:
    lock_path = Path(lock_path)
    token = uuid4().hex
    gate_path = lock_path.with_name(f"{lock_path.name}.gate")
    prepared = lock_path.with_name(
        f".{lock_path.name}.{os.getpid()}.{token}.acquire"
    )
    owner_path = prepared / owner_filename
    prepared_by_this_call = False
    gate_created_by_this_call = False
    try:
        gate_path.mkdir()
        gate_created_by_this_call = True
        (gate_path / owner_filename).write_text(token, encoding="ascii")
    except OSError as exc:
        if gate_created_by_this_call:
            _remove_prepared_lock(gate_path, owner_filename)
        if gate_path.exists():
            raise OwnedDirectoryLockError(
                f"{label} is already running for evidence directory: {lock_path.parent}"
            ) from exc
        raise OwnedDirectoryLockError(
            f"Could not record {label.lower()} gate ownership: {gate_path}"
        ) from exc
    try:
        prepared.mkdir()
        prepared_by_this_call = True
        owner_path.write_text(token, encoding="ascii")
    except OSError as exc:
        if prepared_by_this_call:
            _remove_prepared_lock(prepared, owner_filename)
        _remove_prepared_lock(gate_path, owner_filename)
        raise OwnedDirectoryLockError(
            f"Could not record {label.lower()} lock ownership: {lock_path}"
        ) from exc
    try:
        os.rename(prepared, lock_path)
    except OSError as exc:
        _remove_prepared_lock(prepared, owner_filename)
        _remove_prepared_lock(gate_path, owner_filename)
        if lock_path.exists():
            raise OwnedDirectoryLockError(
                f"{label} is already running for evidence directory: {lock_path.parent}"
            ) from exc
        raise OwnedDirectoryLockError(f"Could not acquire {label.lower()} lock: {lock_path}") from exc
    return OwnedDirectoryLock(
        path=lock_path,
        gate_path=gate_path,
        owner_filename=owner_filename,
        owner_token=token,
        label=label,
    )


def release_owned_directory_lock(
    lock: OwnedDirectoryLock,
    *,
    ownership_error_check: OwnershipErrorCheck | None = None,
    release_error_cleanup: ReleaseErrorCleanup | None = None,
) -> None:
    captured = lock.path.with_name(
        f".{lock.path.name}.{os.getpid()}.{lock.owner_token}.release"
    )
    try:
        try:
            os.replace(lock.path, captured)
        except FileNotFoundError as exc:
            raise OwnedDirectoryLockError(
                f"{lock.label} lock disappeared before release: {lock.path}"
            ) from exc
        except OSError as exc:
            raise OwnedDirectoryLockError(
                f"Could not release {lock.label.lower()} lock: {lock.path}"
            ) from exc

        checker = ownership_error_check or _default_ownership_error_check(lock.label)
        ownership_error = checker(captured / lock.owner_filename, lock.owner_token)
        if ownership_error is not None:
            try:
                os.rename(captured, lock.path)
            except OSError as restore_error:
                ownership_error.add_note(
                    f"Captured foreign lock was retained at {captured}: {restore_error}"
                )
            raise ownership_error

        try:
            (captured / lock.owner_filename).unlink()
            captured.rmdir()
        except OSError as exc:
            raise OwnedDirectoryLockError(
                f"Could not release {lock.label.lower()} lock: {lock.path}"
            ) from exc
    except BaseException as release_error:
        if release_error_cleanup is not None:
            try:
                release_error_cleanup()
            except BaseException as cleanup_error:
                release_error.add_note(f"Release-error cleanup also failed: {cleanup_error}")
        raise

    try:
        gate_error = _default_ownership_error_check(lock.label)(
            lock.gate_path / lock.owner_filename,
            lock.owner_token,
        )
        if gate_error is not None:
            raise gate_error
        (lock.gate_path / lock.owner_filename).unlink()
        lock.gate_path.rmdir()
    except BaseException as gate_release_error:
        if release_error_cleanup is not None:
            try:
                release_error_cleanup()
            except BaseException as cleanup_error:
                gate_release_error.add_note(
                    f"Release-error cleanup also failed: {cleanup_error}"
                )
        raise


def _default_ownership_error_check(label: str) -> OwnershipErrorCheck:
    def check(owner_path: Path, expected_token: str) -> BaseException | None:
        try:
            actual_token = owner_path.read_text(encoding="ascii")
        except (OSError, UnicodeError) as exc:
            return OwnedDirectoryLockError(
                f"Could not verify {label.lower()} lock ownership: {owner_path.parent}: {exc}"
            )
        if actual_token != expected_token:
            return OwnedDirectoryLockError(
                f"{label} lock ownership changed before release: {owner_path.parent}"
            )
        return None

    return check


def _remove_prepared_lock(path: Path, owner_filename: str) -> None:
    try:
        (path / owner_filename).unlink(missing_ok=True)
        path.rmdir()
    except OSError:
        pass
