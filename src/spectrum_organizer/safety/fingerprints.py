from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import stat


class SnapshotError(RuntimeError):
    pass


class SnapshotMismatchError(SnapshotError):
    pass


@dataclass(frozen=True)
class SourceSnapshot:
    path: Path
    sha256: str
    size_bytes: int
    mtime_ns: int
    canonical_path: Path | None = None
    device_id: int | None = None
    file_id: int | None = None


def disambiguated_source_labels(paths) -> tuple[str, ...]:
    normalized = tuple(
        str(path).replace("\\", "/")
        for path in paths
    )
    basenames = tuple(
        path.rsplit("/", 1)[-1]
        for path in normalized
    )
    counts: dict[str, int] = {}
    for basename in basenames:
        key = basename.casefold()
        counts[key] = counts.get(key, 0) + 1
    return tuple(
        path if counts[basename.casefold()] > 1 else basename
        for path, basename in zip(
            normalized,
            basenames,
            strict=True,
        )
    )


def snapshot_sources(
    source_paths: list[Path],
    protected_paths: list[Path],
    *,
    cancel_check=None,
) -> list[SourceSnapshot]:
    if cancel_check is not None:
        cancel_check()
    protected = _resolve_existing_paths(protected_paths)
    protected_identities = _existing_file_identities(protected_paths)
    source_keys: set[str] = set()
    source_identities: set[tuple[int, int]] = set()
    snapshots: list[SourceSnapshot] = []
    for path in source_paths:
        if cancel_check is not None:
            cancel_check()
        source_path = Path(path)
        source_key = _path_key(source_path)
        source_identity = _file_identity(source_path)
        if source_key in source_keys:
            raise SnapshotError(f"Duplicate source path selected: {source_path}")
        if source_identity is not None and source_identity in source_identities:
            raise SnapshotError(f"Selected source paths refer to the same physical file: {source_path}")
        if source_key in protected or (
            source_identity is not None and source_identity in protected_identities
        ):
            raise SnapshotError(f"Source path overlaps a protected path: {source_path}")
        source_keys.add(source_key)
        if source_identity is not None:
            source_identities.add(source_identity)
        snapshots.append(
            _snapshot_source(
                source_path,
                cancel_check=cancel_check,
            )
        )
    return snapshots


def verify_sources_unchanged(snapshots: list[SourceSnapshot], *, cancel_check=None) -> None:
    for snapshot in snapshots:
        if cancel_check is not None:
            cancel_check()
        current = _snapshot_source(snapshot.path, cancel_check=cancel_check)
        if current != snapshot:
            raise SnapshotMismatchError(f"Source changed after snapshot: {snapshot.path}")


def hash_file(path: Path, *, cancel_check=None) -> str:
    try:
        with Path(path).open("rb") as file:
            return _hash_stream(file, cancel_check=cancel_check)
    except OSError as exc:
        raise SnapshotError(f"Could not hash source file {path}: {exc}") from exc


def _snapshot_source(path: Path, *, cancel_check=None) -> SourceSnapshot:
    selected_path = Path(path)
    try:
        canonical_path = selected_path.resolve(strict=True)
        with canonical_path.open("rb") as file:
            before = os.fstat(file.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise SnapshotError(f"Source path is not a file: {selected_path}")
            sha256 = _hash_stream(file, cancel_check=cancel_check)
            after = os.fstat(file.fileno())
        canonical_after = selected_path.resolve(strict=True)
    except OSError as exc:
        raise SnapshotError(f"Could not snapshot source file {selected_path}: {exc}") from exc
    identity_before = (before.st_dev, before.st_ino)
    stable_before_after = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ) == (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    )
    if (
        before.st_ino == 0
        or not stable_before_after
        or os.path.normcase(str(canonical_after))
        != os.path.normcase(str(canonical_path))
    ):
        raise SnapshotError(
            f"Source changed while snapshotting: {selected_path}"
        )
    return SourceSnapshot(
        path=selected_path,
        sha256=sha256,
        size_bytes=before.st_size,
        mtime_ns=before.st_mtime_ns,
        canonical_path=canonical_path,
        device_id=identity_before[0],
        file_id=identity_before[1],
    )


def _hash_stream(file, *, cancel_check=None) -> str:
    digest = hashlib.sha256()
    for chunk in iter(lambda: file.read(1024 * 1024), b""):
        if cancel_check is not None:
            cancel_check()
        digest.update(chunk)
    return digest.hexdigest()


def _resolve_existing_paths(paths: list[Path]) -> set[str]:
    resolved: set[str] = set()
    for path in paths:
        try:
            protected = Path(path)
            if not protected.exists():
                raise SnapshotError(f"Protected path does not exist: {path}")
            resolved.add(_path_key(protected))
        except OSError as exc:
            raise SnapshotError(f"Could not verify protected path {path}: {exc}") from exc
    return resolved


def _path_key(path: Path) -> str:
    return os.path.normcase(str(Path(path).resolve()))


def _existing_file_identities(paths: list[Path]) -> set[tuple[int, int]]:
    identities: set[tuple[int, int]] = set()
    for path in paths:
        identity = _file_identity(Path(path))
        if identity is not None:
            identities.add(identity)
    return identities


def _file_identity(path: Path) -> tuple[int, int] | None:
    try:
        stat = Path(path).stat()
    except OSError:
        return None
    if stat.st_ino == 0:
        return None
    return (stat.st_dev, stat.st_ino)


def file_identity(path: Path) -> tuple[int, int]:
    identity = _file_identity(Path(path))
    if identity is None:
        raise SnapshotError(f"Could not determine source file identity: {path}")
    return identity
