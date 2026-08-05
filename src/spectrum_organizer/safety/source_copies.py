from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
import ctypes
import os
import shutil
import sys
from pathlib import Path
from typing import Callable

from spectrum_organizer.safety.fingerprints import SourceSnapshot, file_identity, hash_file
from spectrum_organizer.safety.identity_paths import (
    create_exclusive_held_directory,
    create_exclusive_held_file,
    hold_file_identity,
    IdentityPathError,
    path_identity,
)
from spectrum_organizer.safety.owned_paths import (
    RunOwnership,
    add_allowed_child,
    bind_allowed_child_identity,
    bind_held_allowed_child_identity,
)


GIB = 1024**3
MIB = 1024**2
INT64_MAX = 2**63 - 1


class SpaceRequirementError(RuntimeError):
    pass


class InsufficientSpaceError(RuntimeError):
    def __init__(self, payload: "InsufficientSpacePayload"):
        super().__init__("Insufficient space for source copies")
        self.payload = payload


class CopyVerificationError(RuntimeError):
    pass


@dataclass(frozen=True)
class InsufficientSpacePayload:
    temp_root: Path
    input_total_bytes: int
    required_bytes: int
    available_bytes: int
    actions: tuple[str, str] = ("retry", "cancel")


@dataclass(frozen=True)
class SourceCopy:
    snapshot: SourceSnapshot
    path: Path


@dataclass(frozen=True)
class CopyResult:
    ownership: RunOwnership
    copies: list[SourceCopy]


def required_temp_bytes(total_selected_input_bytes: int) -> int:
    if total_selected_input_bytes < 0:
        raise SpaceRequirementError("Input byte total cannot be negative")
    scaled = (5 * total_selected_input_bytes + 1) // 2
    if scaled > INT64_MAX - 64 * MIB:
        raise SpaceRequirementError("Required temp bytes exceed signed 64-bit range")
    return max(GIB, scaled + 64 * MIB)


def ensure_sufficient_space(
    temp_root: Path,
    input_total_bytes: int,
    free_bytes_provider: Callable[[Path], int] | None = None,
) -> None:
    temp_root = Path(temp_root)
    required = required_temp_bytes(input_total_bytes)
    available = free_bytes_provider(temp_root) if free_bytes_provider else shutil.disk_usage(temp_root).free
    if available < required:
        raise InsufficientSpaceError(
            InsufficientSpacePayload(
                temp_root=Path(temp_root),
                input_total_bytes=input_total_bytes,
                required_bytes=required,
                available_bytes=available,
            )
        )


def copy_sources(
    snapshots: list[SourceSnapshot],
    ownership: RunOwnership,
    free_bytes_provider: Callable[[Path], int] | None = None,
    copy_file: Callable[[Path, Path], None] | None = None,
) -> CopyResult:
    copy_file = copy_file or shutil.copy2
    input_total = sum(snapshot.size_bytes for snapshot in snapshots)
    copies: list[SourceCopy] = []
    current_ownership = ownership

    for index, snapshot in enumerate(snapshots, start=1):
        ensure_sufficient_space(current_ownership.temp_root, input_total, free_bytes_provider)
        source_dir = current_ownership.temp_root / f"source-{index:04d}-{snapshot.sha256[:12]}"
        current_ownership = add_allowed_child(current_ownership, source_dir)
        try:
            with create_exclusive_held_directory(source_dir) as (
                _,
                source_dir_identity,
            ):
                current_ownership = bind_held_allowed_child_identity(
                    current_ownership,
                    source_dir,
                    source_dir_identity,
                )
                target = source_dir / snapshot.path.name
                current_ownership = add_allowed_child(current_ownership, target)
                source_path = snapshot.canonical_path
                if source_path is None:
                    raise CopyVerificationError(
                        f"Source snapshot lacks a canonical path: {snapshot.path}"
                    )
                if copy_file is shutil.copy2:
                    def bind_created_copy(_path, identity):
                        nonlocal current_ownership
                        current_ownership = bind_allowed_child_identity(
                            current_ownership,
                            target,
                            expected_identity=identity,
                        )

                    _copy_source_exclusive(
                        source_path,
                        target,
                        creation_callback=bind_created_copy,
                    )
                else:
                    copy_file(source_path, target)
                    target_identity = path_identity(target)
                    with hold_file_identity(
                        target,
                        target_identity,
                        allow_write=False,
                    ):
                        current_ownership = bind_allowed_child_identity(
                            current_ownership,
                            target,
                            expected_identity=target_identity,
                        )
                _verify_copy(snapshot, target)
        except (OSError, IdentityPathError) as exc:
            raise CopyVerificationError(
                f"Could not copy source {snapshot.path}: {exc}"
            ) from exc
        copies.append(SourceCopy(snapshot=snapshot, path=target))

    return CopyResult(ownership=current_ownership, copies=copies)


def _copy_source_exclusive(
    source: Path,
    target: Path,
    *,
    creation_callback,
) -> tuple[int, int]:
    with Path(source).open("rb", buffering=0) as reader:
        with create_exclusive_held_file(
            Path(target),
            share_write=False,
        ) as (writer, identity):
            while chunk := reader.read(1024 * 1024):
                writer.write(chunk)
            writer.flush()
            os.fsync(writer.fileno())
            shutil.copystat(source, target)
            creation_callback(Path(target), identity)
            return identity


def _verify_copy(snapshot: SourceSnapshot, target: Path) -> None:
    try:
        size_bytes = target.stat().st_size
    except OSError as exc:
        raise CopyVerificationError(f"Could not stat copied source {target}: {exc}") from exc
    if size_bytes != snapshot.size_bytes or hash_file(target) != snapshot.sha256:
        raise CopyVerificationError(f"Source copy mismatch: {target}")


@contextmanager
def locked_verified_source_copy(
    path: Path,
    *,
    expected_identity: tuple[int, int],
    expected_size_bytes: int,
    expected_sha256: str,
):
    copy_path = Path(path)
    handle = None
    stream = None
    try:
        if sys.platform == "win32":
            create_file = ctypes.windll.kernel32.CreateFileW
            create_file.argtypes = (
                ctypes.c_wchar_p,
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.c_void_p,
            )
            create_file.restype = ctypes.c_void_p
            handle = create_file(str(copy_path), 0x80000000, 0x00000001, None, 3, 0x80, None)
            if handle == ctypes.c_void_p(-1).value:
                raise CopyVerificationError(
                    f"Could not lock approved source copy: {copy_path} (WinError {ctypes.get_last_error()})"
                )
        else:
            stream = copy_path.open("rb")

        if file_identity(copy_path) != expected_identity:
            raise CopyVerificationError(f"Approved source copy identity changed: {copy_path}")
        if copy_path.stat().st_size != expected_size_bytes or hash_file(copy_path) != expected_sha256:
            raise CopyVerificationError(f"Approved source copy content changed: {copy_path}")
        yield copy_path
    finally:
        if stream is not None:
            stream.close()
        if handle not in (None, ctypes.c_void_p(-1).value):
            ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(handle))
