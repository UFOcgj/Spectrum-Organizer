from __future__ import annotations

from dataclasses import dataclass
import hashlib
import io
import json
import os
from pathlib import Path

from spectrum_organizer.safety.identity_paths import (
    IdentityPathError,
    lexical_path_exists,
    path_identity,
    unlink_owned_path,
)
from spectrum_organizer.workflow.extraction_contracts import ProductRunnerError


@dataclass(frozen=True)
class JsonArtifactEvidence:
    identity: tuple[int, int]
    sha256: str


def _json_payload_bytes(payload: dict[str, object]) -> bytes:
    buffer = io.StringIO()
    json.dump(payload, buffer, ensure_ascii=False)
    return buffer.getvalue().encode("utf-8")


def _write_json_exclusive(
    path: Path,
    payload: dict[str, object],
) -> tuple[int, int]:
    return _write_json_exclusive_evidence(path, payload).identity


def _write_json_exclusive_evidence(
    path: Path,
    payload: dict[str, object],
) -> JsonArtifactEvidence:
    identity = None
    try:
        with path.open("xb") as stream:
            status = os.fstat(stream.fileno())
            identity = (status.st_dev, status.st_ino)
            encoded = _json_payload_bytes(payload)
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
            if path_identity(path) != identity:
                raise ProductRunnerError(
                    f"Exclusive JSON identity changed during creation: {path}"
                )
            return JsonArtifactEvidence(
                identity=identity,
                sha256=hashlib.sha256(encoded).hexdigest(),
            )
    except BaseException as exc:
        if identity is not None:
            try:
                unlink_owned_path(path, identity)
            except (OSError, IdentityPathError) as cleanup_exc:
                exc.retained_owned_identities = (
                    *getattr(exc, "retained_owned_identities", ()),
                    (Path(path), identity),
                )
                exc.add_note(str(cleanup_exc))
        raise


def _write_json_atomic_exclusive(
    path: Path,
    payload: dict[str, object],
) -> tuple[int, int]:
    return _write_json_atomic_exclusive_evidence(path, payload).identity


def _write_json_atomic_exclusive_evidence(
    path: Path,
    payload: dict[str, object],
) -> JsonArtifactEvidence:
    final_path = Path(path)
    pending_path = final_path.with_name(f"{final_path.name}.pending")
    if lexical_path_exists(final_path):
        raise FileExistsError(final_path)
    pending_created = False
    pending_identity = None
    try:
        with pending_path.open("xb") as stream:
            pending_created = True
            status = os.fstat(stream.fileno())
            pending_identity = (status.st_dev, status.st_ino)
            encoded = _json_payload_bytes(payload)
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
            if path_identity(pending_path) != pending_identity:
                raise ProductRunnerError(
                    f"Pending JSON identity changed during creation: {pending_path}"
                )
        os.link(pending_path, final_path)
        if path_identity(final_path) != pending_identity:
            raise ProductRunnerError(f"Published JSON identity changed: {final_path}")
    except BaseException as exc:
        if pending_created and pending_identity is not None:
            if (
                lexical_path_exists(final_path)
                and path_identity(final_path) == pending_identity
            ):
                exc.retained_owned_identities = (
                    *getattr(exc, "retained_owned_identities", ()),
                    (final_path, pending_identity),
                )
            try:
                unlink_owned_path(pending_path, pending_identity)
            except (OSError, IdentityPathError) as cleanup_exc:
                retained = list(getattr(exc, "retained_owned_identities", ()))
                retained.append((pending_path, pending_identity))
                exc.retained_owned_identities = tuple(retained)
                exc.add_note(str(cleanup_exc))
        raise
    try:
        unlink_owned_path(pending_path, pending_identity)
    except (OSError, IdentityPathError) as exc:
        exc.retained_owned_identities = (
            (final_path, pending_identity),
            (
                Path(getattr(exc, "retained_path", pending_path)),
                pending_identity,
            ),
        )
        raise
    return JsonArtifactEvidence(
        identity=pending_identity,
        sha256=hashlib.sha256(encoded).hexdigest(),
    )
