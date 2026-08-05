from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import time

from spectrum_organizer.safety.identity_paths import (
    create_exclusive_held_directory,
    create_exclusive_held_file,
    hold_directory_identity,
    hold_file_identity,
    IdentityPathError,
    lexical_path_exists,
    path_identity,
    quarantine_owned_path,
    remove_empty_owned_directory,
    restore_quarantined_path,
    unlink_owned_path,
)


OWNERSHIP_FILE = "ownership.json"
OWNERSHIP_TEMP_FILE = "ownership.json.tmp"
ACTIVE_LEASE_FILE = ".active.lock"
OWNERSHIP_ANCHOR_SUFFIX = ".ownership-anchor.json"
OWNERSHIP_ANCHOR_KEY = ".ownership-anchor.key"
_OWNERSHIP_WRITE_RETRY_DELAYS = (0.01, 0.02, 0.04, 0.08, 0.15, 0.25, 0.45)
class OwnershipError(RuntimeError):
    pass


class CleanupRefusedError(OwnershipError):
    pass


class CleanupFailedError(CleanupRefusedError):
    pass


class RunLease:
    def __init__(
        self,
        file_handle,
        identity: tuple[int, int],
        ownership: RunOwnership,
    ):
        self._file_handle = file_handle
        self.identity = identity
        self.ownership = ownership

    def close(self) -> None:
        if self._file_handle is None:
            return
        _unlock_file(self._file_handle)
        self._file_handle.close()
        self._file_handle = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()


@dataclass(frozen=True)
class RunOwnership:
    run_id: str
    marker_id: str
    temp_root: Path
    temp_root_identity: tuple[int, int]
    metadata_identity: tuple[int, int] | None
    allowed_children: tuple[Path, ...]
    allowed_child_identities: tuple[
        tuple[Path, tuple[int, int]], ...
    ]
    protected_paths: tuple[Path, ...]


def create_run_ownership(
    local_appdata: str | os.PathLike[str] | None,
    run_id: str,
    marker_id: str,
    protected_paths: list[Path],
) -> RunOwnership:
    base = Path(local_appdata) if local_appdata is not None else _local_appdata_from_env()
    temp_root = base / "Spectrum Organizer" / "temp" / run_id
    return create_run_ownership_at_root(
        temp_root,
        run_id,
        marker_id,
        protected_paths,
    )


def create_run_ownership_at_root(
    temp_root: Path,
    run_id: str,
    marker_id: str,
    protected_paths: list[Path],
) -> RunOwnership:
    temp_root = Path(temp_root)
    temp_root.parent.mkdir(parents=True, exist_ok=True)
    temp_root_identity = None
    anchor_identity = None
    try:
        with create_exclusive_held_directory(temp_root) as (
            _,
            temp_root_identity,
        ):
            ownership = _write_initial_ownership_under_created_root(
                RunOwnership(
                    run_id=run_id,
                    marker_id=marker_id,
                    temp_root=temp_root,
                    temp_root_identity=temp_root_identity,
                    metadata_identity=None,
                    allowed_children=(),
                    allowed_child_identities=(),
                    protected_paths=tuple(Path(path) for path in protected_paths),
                )
            )
            anchor_identity = _write_ownership_anchor(ownership)
    except (OSError, IdentityPathError, OwnershipError) as exc:
        try:
            if anchor_identity is not None:
                unlink_owned_path(
                    _ownership_anchor_path(temp_root),
                    anchor_identity,
                )
            if temp_root_identity is not None:
                _remove_unpublished_root(temp_root, temp_root_identity)
        except (OSError, IdentityPathError) as cleanup_error:
            exc.add_note(f"Could not remove unpublished run temp root: {cleanup_error}")
        if isinstance(exc, OwnershipError):
            raise
        raise OwnershipError(
            f"Could not create run temp root {temp_root}: {exc}"
        ) from exc
    return ownership


def _ownership_anchor_path(temp_root: Path) -> Path:
    root = Path(temp_root)
    return root.parent / f".{root.name}{OWNERSHIP_ANCHOR_SUFFIX}"


def _write_ownership_anchor(ownership: RunOwnership) -> tuple[int, int]:
    anchor = _ownership_anchor_path(ownership.temp_root)
    metadata_identity, metadata_sha256 = _ownership_metadata_evidence(
        ownership.temp_root,
        expected_identity=ownership.metadata_identity,
    )
    payload = _authenticated_anchor_payload(
        ownership,
        metadata_identity=metadata_identity,
        metadata_sha256=metadata_sha256,
        key=_ownership_anchor_key(
            ownership.temp_root.parent,
            create=True,
        ),
    )
    encoded = json.dumps(payload, sort_keys=True).encode("utf-8")
    try:
        with create_exclusive_held_file(anchor) as (stream, identity):
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        return identity
    except (OSError, IdentityPathError) as exc:
        raise OwnershipError(
            f"Could not write external ownership anchor: {exc}"
        ) from exc


def _read_ownership_anchor(
    temp_root: Path,
) -> tuple[
    tuple[int, int],
    Path,
    tuple[int, int],
    str,
    str,
    tuple[int, int],
    str,
]:
    root = Path(temp_root)
    anchor = _ownership_anchor_path(root)
    if not anchor.is_file() or anchor.is_symlink():
        raise CleanupRefusedError(f"Missing external ownership anchor: {anchor}")
    try:
        with anchor.open("r", encoding="utf-8") as stream:
            status = os.fstat(stream.fileno())
            anchor_identity = (status.st_dev, status.st_ino)
            payload = json.load(stream)
            if path_identity(anchor) != anchor_identity:
                raise CleanupRefusedError(
                    f"External ownership anchor identity changed: {anchor}"
                )
    except (OSError, json.JSONDecodeError, IdentityPathError) as exc:
        raise CleanupRefusedError(
            f"Invalid external ownership anchor: {anchor}: {exc}"
        ) from exc
    if (
        not isinstance(payload, dict)
        or set(payload)
        != {
            "run_id",
            "marker_id",
            "temp_root",
            "temp_root_identity",
            "metadata_identity",
            "metadata_sha256",
            "auth_hmac",
        }
        or not isinstance(payload.get("run_id"), str)
        or not isinstance(payload.get("marker_id"), str)
        or Path(payload.get("temp_root", "")) != root
        or not _valid_identity_value(payload.get("temp_root_identity"))
        or not _valid_identity_value(payload.get("metadata_identity"))
        or not _valid_sha256(payload.get("metadata_sha256"))
        or not isinstance(payload.get("auth_hmac"), str)
    ):
        raise CleanupRefusedError(
            f"External ownership anchor mismatch: {anchor}"
        )
    base_payload = {
        key: payload[key]
        for key in (
            "run_id",
            "marker_id",
            "temp_root",
            "temp_root_identity",
            "metadata_identity",
            "metadata_sha256",
        )
    }
    expected_hmac = _anchor_auth_hmac(
        base_payload,
        _ownership_anchor_key(root.parent),
    )
    if not hmac.compare_digest(payload["auth_hmac"], expected_hmac):
        raise CleanupRefusedError(
            f"External ownership anchor authentication failed: {anchor}"
        )
    return (
        tuple(payload["temp_root_identity"]),
        anchor,
        anchor_identity,
        payload["run_id"],
        payload["marker_id"],
        tuple(payload["metadata_identity"]),
        payload["metadata_sha256"],
    )


def _authenticated_anchor_payload(
    ownership: RunOwnership,
    *,
    metadata_identity: tuple[int, int],
    metadata_sha256: str,
    key: bytes,
) -> dict[str, object]:
    base_payload = {
        "run_id": ownership.run_id,
        "marker_id": ownership.marker_id,
        "temp_root": str(ownership.temp_root),
        "temp_root_identity": list(ownership.temp_root_identity),
        "metadata_identity": list(metadata_identity),
        "metadata_sha256": metadata_sha256,
    }
    return {
        **base_payload,
        "auth_hmac": _anchor_auth_hmac(base_payload, key),
    }


def _ownership_metadata_evidence(
    temp_root: Path,
    *,
    expected_identity: tuple[int, int] | None = None,
) -> tuple[tuple[int, int], str]:
    metadata = Path(temp_root) / OWNERSHIP_FILE
    try:
        with metadata.open("rb", buffering=0) as stream:
            status = os.fstat(stream.fileno())
            identity = (status.st_dev, status.st_ino)
            content = stream.read()
            if path_identity(metadata) != identity:
                raise CleanupRefusedError(
                    f"Ownership metadata identity changed at {metadata}"
                )
    except (OSError, IdentityPathError) as exc:
        raise CleanupRefusedError(
            f"Ownership metadata could not be authenticated: {metadata}: {exc}"
        ) from exc
    if expected_identity is not None and identity != expected_identity:
        raise CleanupRefusedError(
            f"Ownership metadata identity does not match its caller-held generation: {metadata}"
        )
    return identity, hashlib.sha256(content).hexdigest()


def _read_authenticated_ownership_metadata(
    temp_root: Path,
    *,
    expected_identity: tuple[int, int],
    expected_sha256: str,
) -> tuple[tuple[int, int], dict[str, object]]:
    metadata = Path(temp_root) / OWNERSHIP_FILE
    try:
        with hold_file_identity(
            metadata,
            expected_identity,
            allow_write=False,
        ):
            with metadata.open("rb", buffering=0) as stream:
                status = os.fstat(stream.fileno())
                identity = (status.st_dev, status.st_ino)
                content = stream.read()
                if (
                    identity != expected_identity
                    or path_identity(metadata) != identity
                ):
                    raise CleanupRefusedError(
                        f"Ownership metadata identity changed at {metadata}"
                    )
    except (OSError, IdentityPathError) as exc:
        raise CleanupRefusedError(
            f"Ownership metadata could not be authenticated: {metadata}: {exc}"
        ) from exc
    if hashlib.sha256(content).hexdigest() != expected_sha256:
        raise CleanupRefusedError(
            f"Ownership metadata does not match external anchor: {metadata}"
        )
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CleanupRefusedError(
            f"Invalid ownership metadata: {metadata}: {exc}"
        ) from exc
    return identity, payload


def _rewrite_ownership_anchor(ownership: RunOwnership) -> None:
    (
        anchored_root_identity,
        anchor,
        anchor_identity,
        anchored_run_id,
        anchored_marker_id,
        _anchored_metadata_identity,
        _anchored_metadata_sha256,
    ) = _read_ownership_anchor(ownership.temp_root)
    if (
        anchored_root_identity != ownership.temp_root_identity
        or anchored_run_id != ownership.run_id
        or anchored_marker_id != ownership.marker_id
    ):
        raise OwnershipError(
            f"External ownership anchor changed before metadata update: {anchor}"
        )
    metadata_identity, metadata_sha256 = _ownership_metadata_evidence(
        ownership.temp_root,
        expected_identity=ownership.metadata_identity,
    )
    payload = _authenticated_anchor_payload(
        ownership,
        metadata_identity=metadata_identity,
        metadata_sha256=metadata_sha256,
        key=_ownership_anchor_key(ownership.temp_root.parent),
    )
    encoded = json.dumps(payload, sort_keys=True).encode("utf-8")
    try:
        with hold_file_identity(anchor, anchor_identity, allow_write=True):
            with anchor.open("r+b", buffering=0) as stream:
                status = os.fstat(stream.fileno())
                if (status.st_dev, status.st_ino) != anchor_identity:
                    raise OwnershipError(
                        f"External ownership anchor identity changed: {anchor}"
                    )
                stream.seek(0)
                stream.truncate()
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
    except (OSError, IdentityPathError) as exc:
        raise OwnershipError(
            f"Could not update external ownership anchor: {anchor}: {exc}"
        ) from exc


def _ownership_anchor_key(parent: Path, *, create: bool = False) -> bytes:
    key_path = _ownership_anchor_key_path(parent)
    if create:
        if not key_path.parent.is_dir():
            key_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with create_exclusive_held_file(key_path) as (stream, _identity):
                key = secrets.token_bytes(32)
                stream.write(key)
                stream.flush()
                os.fsync(stream.fileno())
                return key
        except FileExistsError:
            pass
    try:
        with key_path.open("rb", buffering=0) as stream:
            status = os.fstat(stream.fileno())
            identity = (status.st_dev, status.st_ino)
            key = stream.read()
            if path_identity(key_path) != identity or len(key) != 32:
                raise CleanupRefusedError(
                    f"External ownership anchor key is invalid: {key_path}"
                )
            return key
    except (OSError, IdentityPathError) as exc:
        raise CleanupRefusedError(
            f"External ownership anchor key could not be read: {key_path}: {exc}"
        ) from exc


def _anchor_auth_hmac(payload: dict[str, object], key: bytes) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hmac.new(key, encoded, hashlib.sha256).hexdigest()


def _ownership_anchor_key_path(parent: Path) -> Path:
    anchor_parent = Path(parent)
    if (
        anchor_parent.name == "temp"
        and anchor_parent.parent.name == "Spectrum Organizer"
    ):
        return anchor_parent.parent / "data" / OWNERSHIP_ANCHOR_KEY
    return anchor_parent / OWNERSHIP_ANCHOR_KEY


def _write_initial_ownership_under_created_root(
    ownership: RunOwnership,
) -> RunOwnership:
    metadata = ownership.temp_root / OWNERSHIP_FILE
    payload = {
        "run_id": ownership.run_id,
        "marker_id": ownership.marker_id,
        "temp_root": str(ownership.temp_root),
        "temp_root_identity": list(ownership.temp_root_identity),
        "allowed_children": [],
        "allowed_child_identities": [],
        "protected_paths": [str(path) for path in ownership.protected_paths],
    }
    encoded = json.dumps(payload, sort_keys=True).encode("utf-8")
    try:
        with create_exclusive_held_file(metadata) as (stream, identity):
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        return replace(ownership, metadata_identity=identity)
    except (OSError, IdentityPathError) as exc:
        raise OwnershipError(
            f"Could not write initial ownership metadata: {exc}"
        ) from exc


def add_allowed_child(ownership: RunOwnership, child: Path) -> RunOwnership:
    child = Path(child)
    _require_child_under_root(ownership.temp_root, child)
    children = ownership.allowed_children
    if child not in children:
        children = (*children, child)
    identities = dict(ownership.allowed_child_identities)
    if lexical_path_exists(child):
        try:
            identities[child] = path_identity(child)
        except IdentityPathError as exc:
            raise OwnershipError(str(exc)) from exc
    updated = replace(
        ownership,
        allowed_children=children,
        allowed_child_identities=tuple(identities.items()),
    )
    return write_ownership(updated)


def bind_allowed_child_identity(
    ownership: RunOwnership,
    child: Path,
    expected_identity: tuple[int, int] | None = None,
) -> RunOwnership:
    child = Path(child)
    if child not in ownership.allowed_children:
        raise OwnershipError(
            f"Cannot bind identity for unregistered temp path: {child}"
        )
    try:
        identity = path_identity(child)
    except IdentityPathError as exc:
        raise OwnershipError(str(exc)) from exc
    if expected_identity is not None and identity != expected_identity:
        raise OwnershipError(
            f"Registered temp path identity changed before binding: {child}"
        )
    identities = dict(ownership.allowed_child_identities)
    identities[child] = identity
    return write_ownership(
        replace(
            ownership,
            allowed_child_identities=tuple(identities.items()),
        )
    )


def bind_held_allowed_child_identity(
    ownership: RunOwnership,
    child: Path,
    identity: tuple[int, int],
) -> RunOwnership:
    child = Path(child)
    if child not in ownership.allowed_children:
        raise OwnershipError(
            f"Cannot bind identity for unregistered temp path: {child}"
        )
    if (
        not isinstance(identity, tuple)
        or len(identity) != 2
        or any(not isinstance(part, int) for part in identity)
        or identity[1] == 0
    ):
        raise OwnershipError(f"Invalid held temp path identity: {child}")
    identities = dict(ownership.allowed_child_identities)
    identities[child] = identity
    return write_ownership(
        replace(
            ownership,
            allowed_child_identities=tuple(identities.items()),
        )
    )


def write_ownership(ownership: RunOwnership) -> RunOwnership:
    for delay in (*_OWNERSHIP_WRITE_RETRY_DELAYS, None):
        try:
            with hold_directory_identity(
                ownership.temp_root,
                ownership.temp_root_identity,
            ):
                return _write_ownership_under_lock(ownership)
        except IdentityPathError as exc:
            if (
                delay is None
                or not _is_windows_sharing_violation(exc)
                or not _ownership_write_generation_is_unchanged(ownership)
            ):
                raise OwnershipError(str(exc)) from exc
            time.sleep(delay)
    raise AssertionError("ownership write retry loop did not return")


def _is_windows_sharing_violation(error: BaseException) -> bool:
    current = error
    while current is not None:
        if isinstance(current, OSError) and getattr(current, "winerror", None) in {
            32,
            33,
        }:
            return True
        current = current.__cause__
    return False


def _ownership_write_generation_is_unchanged(ownership: RunOwnership) -> bool:
    metadata = ownership.temp_root / OWNERSHIP_FILE
    pending = ownership.temp_root / OWNERSHIP_TEMP_FILE
    if lexical_path_exists(pending):
        return False
    if ownership.metadata_identity is None:
        return not lexical_path_exists(metadata)
    try:
        return path_identity(metadata) == ownership.metadata_identity
    except IdentityPathError:
        return False


def _write_ownership_under_lock(
    ownership: RunOwnership,
) -> RunOwnership:
    payload = {
        "run_id": ownership.run_id,
        "marker_id": ownership.marker_id,
        "temp_root": str(ownership.temp_root),
        "temp_root_identity": list(ownership.temp_root_identity),
        "allowed_children": [str(path) for path in ownership.allowed_children],
        "allowed_child_identities": [
            {
                "path": str(path),
                "identity": list(identity),
            }
            for path, identity in ownership.allowed_child_identities
        ],
        "protected_paths": [str(path) for path in ownership.protected_paths],
    }
    metadata = ownership.temp_root / OWNERSHIP_FILE
    pending = ownership.temp_root / OWNERSHIP_TEMP_FILE
    pending_identity = None
    try:
        with pending.open("x", encoding="utf-8") as file:
            status = os.fstat(file.fileno())
            pending_identity = (status.st_dev, status.st_ino)
            json.dump(payload, file, sort_keys=True)
            file.flush()
            os.fsync(file.fileno())
        _replace_ownership_metadata(
            pending,
            pending_identity,
            metadata,
            ownership.metadata_identity,
        )
        updated = replace(ownership, metadata_identity=pending_identity)
        _rewrite_ownership_anchor(updated)
        return updated
    except BaseException as exc:
        if pending_identity is not None:
            try:
                unlink_owned_path(pending, pending_identity)
            except IdentityPathError as cleanup_exc:
                exc.add_note(str(cleanup_exc))
        if isinstance(exc, OSError):
            raise OwnershipError(f"Could not write ownership metadata: {exc}") from exc
        raise


def _replace_ownership_metadata(
    pending: Path,
    pending_identity: tuple[int, int],
    metadata: Path,
    metadata_identity: tuple[int, int] | None,
) -> None:
    parked = None
    if metadata_identity is not None:
        parked = quarantine_owned_path(metadata, metadata_identity)
    elif lexical_path_exists(metadata):
        raise FileExistsError(metadata)
    published = False
    try:
        os.link(pending, metadata)
        published = True
        if path_identity(metadata) != pending_identity:
            raise IdentityPathError(
                metadata,
                f"Published ownership metadata identity changed: {metadata}",
            )
        unlink_owned_path(pending, pending_identity)
        if parked is not None:
            unlink_owned_path(parked, metadata_identity)
    except Exception:
        if published:
            try:
                unlink_owned_path(metadata, pending_identity)
            except (OSError, IdentityPathError):
                pass
        if parked is not None:
            restore_quarantined_path(
                parked,
                metadata,
                metadata_identity,
            )
        raise


def cleanup_owned_temp_root(
    temp_root: Path,
    *,
    expected_root_identity: tuple[int, int] | None = None,
) -> list[Path]:
    (
        anchored_root_identity,
        anchor,
        anchor_identity,
        anchored_run_id,
        anchored_marker_id,
        anchored_metadata_identity,
        anchored_metadata_sha256,
    ) = _read_ownership_anchor(temp_root)
    ownership = read_ownership(temp_root)
    metadata_identity, metadata_sha256 = _ownership_metadata_evidence(
        ownership.temp_root,
        expected_identity=ownership.metadata_identity,
    )
    if (
        ownership.temp_root_identity != anchored_root_identity
        or (
            expected_root_identity is not None
            and ownership.temp_root_identity != expected_root_identity
        )
        or ownership.run_id != anchored_run_id
        or ownership.marker_id != anchored_marker_id
        or metadata_identity != anchored_metadata_identity
        or metadata_sha256 != anchored_metadata_sha256
    ):
        raise CleanupRefusedError(
            f"Ownership metadata does not match external anchor: {anchor}"
        )
    allowed = set(ownership.allowed_children)
    children = [
        path
        for path in ownership.temp_root.iterdir()
        if path.name not in {OWNERSHIP_FILE, OWNERSHIP_TEMP_FILE}
    ]
    unknown = [path for path in children if path not in allowed]
    if unknown:
        raise CleanupRefusedError(f"Refusing cleanup for unknown temp paths: {unknown}")
    for child in allowed:
        _require_child_under_root(ownership.temp_root, child)
    expected_children = dict(ownership.allowed_child_identities)
    for child in children:
        expected_identity = expected_children.get(child)
        if expected_identity is None:
            raise CleanupRefusedError(
                f"Refusing cleanup for temp path without creation identity: {child}"
            )
        try:
            if path_identity(child) != expected_identity:
                raise CleanupRefusedError(
                    f"Refusing cleanup for replaced temp path: {child}"
                )
        except IdentityPathError as exc:
            raise CleanupRefusedError(str(exc)) from exc
    metadata = ownership.temp_root / OWNERSHIP_FILE
    pending = ownership.temp_root / OWNERSHIP_TEMP_FILE
    if lexical_path_exists(pending):
        raise CleanupRefusedError(
            f"Refusing cleanup while ownership update is pending: {pending}"
        )
    try:
        if path_identity(metadata) != ownership.metadata_identity:
            raise CleanupRefusedError(
                f"Refusing cleanup for replaced ownership metadata: {metadata}"
            )
    except IdentityPathError as exc:
        raise CleanupRefusedError(str(exc)) from exc
    deleted = list(allowed)
    try:
        isolated_root = quarantine_owned_path(
            ownership.temp_root,
            ownership.temp_root_identity,
        )
    except IdentityPathError as exc:
        raise CleanupRefusedError(str(exc)) from exc
    try:
        isolated_allowed = {
            isolated_root / child.relative_to(ownership.temp_root)
            for child in allowed
        }
        isolated_children = {
            child
            for child in isolated_root.iterdir()
            if child.name not in {OWNERSHIP_FILE, OWNERSHIP_TEMP_FILE}
        }
        isolated_unknown = isolated_children - isolated_allowed
        if isolated_unknown:
            raise CleanupRefusedError(
                "Refusing cleanup for unknown quarantined temp paths: "
                f"{sorted(isolated_unknown, key=str)}"
            )
        for child in isolated_children:
            original_child = ownership.temp_root / child.name
            expected_identity = expected_children.get(original_child)
            if expected_identity is None or path_identity(child) != expected_identity:
                raise CleanupRefusedError(
                    f"Refusing cleanup for replaced quarantined temp path: {child}"
                )
        isolated_metadata = isolated_root / OWNERSHIP_FILE
        if path_identity(isolated_metadata) != ownership.metadata_identity:
            raise CleanupRefusedError(
                "Refusing cleanup for replaced quarantined ownership metadata: "
                f"{isolated_metadata}"
            )
    except (CleanupRefusedError, IdentityPathError) as exc:
        restored = restore_quarantined_path(
            isolated_root,
            ownership.temp_root,
            ownership.temp_root_identity,
        )
        retained = ownership.temp_root if restored else isolated_root
        raise CleanupRefusedError(
            f"{exc}; retained at {retained}"
        ) from exc
    try:
        expected_descendants = {
            child.relative_to(ownership.temp_root): identity
            for child, identity in ownership.allowed_child_identities
        }
        expected_descendants[Path(OWNERSHIP_FILE)] = ownership.metadata_identity
        _remove_owned_tree(
            isolated_root,
            ownership.temp_root_identity,
            expected_descendants=expected_descendants,
        )
    except (OSError, IdentityPathError) as exc:
        restored = restore_quarantined_path(
            isolated_root,
            ownership.temp_root,
            ownership.temp_root_identity,
        )
        retained = ownership.temp_root if restored else isolated_root
        raise CleanupFailedError(
            f"Could not clean owned temp root; retained at {retained}: {exc}"
        ) from exc
    if lexical_path_exists(ownership.temp_root):
        raise CleanupRefusedError(
            f"Unknown filesystem object appeared at cleaned temp root: {ownership.temp_root}"
        )
    try:
        unlink_owned_path(anchor, anchor_identity)
    except (OSError, IdentityPathError) as exc:
        raise CleanupFailedError(
            f"Owned temp root was removed but its external anchor was retained at {anchor}: {exc}"
        ) from exc
    return deleted


def _remove_owned_tree(
    root: Path,
    expected_identity: tuple[int, int],
    *,
    expected_descendants: dict[Path, tuple[int, int]] | None = None,
    relative_root: Path = Path(),
) -> None:
    root = Path(root)
    expected_descendants = expected_descendants or {}
    with hold_directory_identity(root, expected_identity):
        children = tuple(root.iterdir())
        for child in children:
            relative_child = relative_root / child.name
            child_identity = expected_descendants.get(relative_child)
            if child_identity is None:
                raise IdentityPathError(
                    child,
                    f"Owned directory contains a child without creation identity: {child}",
                )
            if path_identity(child) != child_identity:
                raise IdentityPathError(
                    child,
                    f"Owned directory child identity changed before deletion: {child}",
                )
            if child.is_dir() and not child.is_symlink():
                _remove_owned_tree(
                    child,
                    child_identity,
                    expected_descendants=expected_descendants,
                    relative_root=relative_child,
                )
            else:
                unlink_owned_path(child, child_identity)
    try:
        remove_empty_owned_directory(root, expected_identity)
    except OSError as exc:
        raise IdentityPathError(
            root,
            f"Owned directory could not be removed; retained at {root}: {exc}",
        ) from exc


def acquire_run_lease(temp_root: Path) -> RunLease:
    lease_path = Path(temp_root) / ACTIVE_LEASE_FILE
    ownership = read_ownership(temp_root)
    if lease_path not in ownership.allowed_children:
        raise OwnershipError(f"Run lease path is not registered: {lease_path}")
    expected_identity = dict(ownership.allowed_child_identities).get(lease_path)
    file_handle = lease_path.open("x+b" if expected_identity is None else "r+b")
    try:
        status = os.fstat(file_handle.fileno())
        identity = (status.st_dev, status.st_ino)
        if expected_identity is not None and identity != expected_identity:
            raise OwnershipError(
                f"Run lease identity changed before acquisition: {lease_path}"
            )
        if path_identity(lease_path) != identity:
            raise OwnershipError(
                f"Run lease path changed before acquisition: {lease_path}"
            )
        file_handle.seek(0, os.SEEK_END)
        if file_handle.tell() == 0:
            file_handle.write(b"\0")
            file_handle.flush()
            os.fsync(file_handle.fileno())
        file_handle.seek(0)
        _lock_file(file_handle)
        if expected_identity is None:
            ownership = bind_allowed_child_identity(
                ownership,
                lease_path,
                expected_identity=identity,
            )
    except Exception:
        file_handle.close()
        raise
    return RunLease(file_handle, identity, ownership)


def run_lease_is_held(temp_root: Path) -> bool:
    lease_path = Path(temp_root) / ACTIVE_LEASE_FILE
    if not lease_path.exists():
        return False
    try:
        file_handle = lease_path.open("r+b")
        try:
            file_handle.seek(0)
            _lock_file(file_handle)
        except Exception:
            file_handle.close()
            raise
    except OSError:
        return True
    status = os.fstat(file_handle.fileno())
    lease = RunLease(
        file_handle,
        (status.st_dev, status.st_ino),
        read_ownership(temp_root),
    )
    lease.close()
    return False


def read_ownership(temp_root: Path) -> RunOwnership:
    temp_root = Path(temp_root)
    metadata = temp_root / OWNERSHIP_FILE
    (
        anchored_root_identity,
        _anchor,
        _anchor_identity,
        anchored_run_id,
        anchored_marker_id,
        anchored_metadata_identity,
        anchored_metadata_sha256,
    ) = _read_ownership_anchor(temp_root)
    metadata_identity, payload = _read_authenticated_ownership_metadata(
        temp_root,
        expected_identity=anchored_metadata_identity,
        expected_sha256=anchored_metadata_sha256,
    )
    required = {
        "run_id",
        "marker_id",
        "temp_root",
        "temp_root_identity",
        "allowed_children",
        "allowed_child_identities",
        "protected_paths",
    }
    if (
        not isinstance(payload, dict)
        or set(payload) != required
        or not _has_valid_payload_types(payload)
        or Path(payload["temp_root"]) != temp_root
    ):
        raise CleanupRefusedError(f"Ownership metadata mismatch at {metadata}")
    if (
        tuple(payload["temp_root_identity"]) != anchored_root_identity
        or payload["run_id"] != anchored_run_id
        or payload["marker_id"] != anchored_marker_id
    ):
        raise CleanupRefusedError(
            f"Ownership metadata does not match external anchor: {metadata}"
        )
    temp_root_identity = tuple(payload["temp_root_identity"])
    try:
        if path_identity(temp_root) != temp_root_identity:
            raise CleanupRefusedError(
                f"Ownership root identity changed at {temp_root}"
            )
    except IdentityPathError as exc:
        raise CleanupRefusedError(str(exc)) from exc
    ownership = RunOwnership(
        run_id=payload["run_id"],
        marker_id=payload["marker_id"],
        temp_root=temp_root,
        temp_root_identity=temp_root_identity,
        metadata_identity=metadata_identity,
        allowed_children=tuple(Path(path) for path in payload["allowed_children"]),
        allowed_child_identities=tuple(
            (
                Path(item["path"]),
                tuple(item["identity"]),
            )
            for item in payload["allowed_child_identities"]
        ),
        protected_paths=tuple(Path(path) for path in payload["protected_paths"]),
    )
    identity_paths = tuple(
        path for path, _identity in ownership.allowed_child_identities
    )
    if (
        len(identity_paths) != len(set(identity_paths))
        or not set(identity_paths).issubset(ownership.allowed_children)
    ):
        raise CleanupRefusedError(
            f"Ownership child identity mismatch at {metadata}"
        )
    for child in ownership.allowed_children:
        _require_child_under_root(ownership.temp_root, child)
    return ownership


def _valid_identity_value(value: object) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 2
        and all(isinstance(part, int) for part in value)
        and value[1] != 0
    )


def _valid_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _has_valid_payload_types(payload) -> bool:
    return (
        isinstance(payload.get("run_id"), str)
        and isinstance(payload.get("marker_id"), str)
        and isinstance(payload.get("temp_root"), str)
        and isinstance(payload.get("temp_root_identity"), list)
        and len(payload.get("temp_root_identity", [])) == 2
        and all(
            isinstance(value, int) and not isinstance(value, bool)
            for value in payload.get("temp_root_identity", [])
        )
        and isinstance(payload.get("allowed_children"), list)
        and all(isinstance(path, str) for path in payload.get("allowed_children", []))
        and isinstance(payload.get("allowed_child_identities"), list)
        and all(
            isinstance(item, dict)
            and set(item) == {"path", "identity"}
            and isinstance(item["path"], str)
            and isinstance(item["identity"], list)
            and len(item["identity"]) == 2
            and all(
                isinstance(value, int) and not isinstance(value, bool)
                for value in item["identity"]
            )
            for item in payload.get("allowed_child_identities", [])
        )
        and isinstance(payload.get("protected_paths"), list)
        and all(isinstance(path, str) for path in payload.get("protected_paths", []))
    )


def _remove_unpublished_root(
    temp_root: Path,
    expected_identity: tuple[int, int],
) -> None:
    isolated = quarantine_owned_path(temp_root, expected_identity)
    try:
        remove_empty_owned_directory(isolated, expected_identity)
    except (OSError, IdentityPathError):
        restore_quarantined_path(
            isolated,
            temp_root,
            expected_identity,
        )
        raise


def _local_appdata_from_env() -> Path:
    value = os.environ.get("LOCALAPPDATA")
    if not value:
        raise OwnershipError("LOCALAPPDATA is required; no fallback temp root is allowed")
    return Path(value)


def _require_child_under_root(root: Path, child: Path) -> None:
    root = root.resolve()
    child = child.resolve()
    if child == root or root not in child.parents:
        raise CleanupRefusedError(f"Temp path is outside owned root: {child}")


def _lock_file(file_handle) -> None:
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(file_handle.fileno(), msvcrt.LK_NBLCK, 1)
        return
    import fcntl

    fcntl.flock(file_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(file_handle) -> None:
    try:
        file_handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(file_handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(file_handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
