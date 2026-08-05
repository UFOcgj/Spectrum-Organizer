from __future__ import annotations

import json
import os
from pathlib import Path
import time
from uuid import uuid4

from spectrum_organizer.safety.identity_paths import (
    create_exclusive_held_file,
    hold_directory_identity,
    path_identity,
    unlink_owned_path,
)


RUNTIME_AUDIT_DIR_ENV = "SPECTRUM_ORGANIZER_RUNTIME_AUDIT_DIR"
_PROCESS_INSTANCE_ID = uuid4().hex


def runtime_audit_enabled() -> bool:
    return bool(os.environ.get(RUNTIME_AUDIT_DIR_ENV))


def runtime_audit_file_identity(path: Path) -> dict[str, object]:
    resolved = Path(path).resolve(strict=True)
    if not resolved.is_file():
        raise RuntimeError(f"Runtime audit target is not a file: {resolved}")
    status = resolved.stat()
    if status.st_ino == 0:
        raise RuntimeError(
            f"Runtime audit target has no stable file identity: {resolved}"
        )
    return {
        "path": str(resolved),
        "device_id": status.st_dev,
        "file_id": status.st_ino,
    }


def record_runtime_audit_event(
    event_type: str,
    payload: dict[str, object],
) -> Path | None:
    configured = os.environ.get(RUNTIME_AUDIT_DIR_ENV)
    if not configured:
        return None
    audit_dir = Path(configured)
    if not audit_dir.is_absolute() or not audit_dir.is_dir():
        raise RuntimeError("Runtime audit directory is invalid")
    audit_dir = audit_dir.resolve(strict=True)
    event_id = f"{time.time_ns()}-{os.getpid()}-{uuid4().hex}"
    event_path = audit_dir / f"{event_id}.json"
    pending_path = audit_dir / f"{event_id}.pending"
    event = {
        "schema_version": 1,
        "event_type": event_type,
        "recorded_time_ns": time.time_ns(),
        "process_id": os.getpid(),
        "process_instance_id": _PROCESS_INSTANCE_ID,
        "payload": payload,
    }
    event_bytes = json.dumps(
        event,
        ensure_ascii=False,
        sort_keys=True,
    ).encode("utf-8")
    audit_identity = path_identity(audit_dir)
    with hold_directory_identity(audit_dir, audit_identity):
        if path_identity(audit_dir) != audit_identity:
            raise RuntimeError("Runtime audit directory identity changed")
        return _publish_runtime_audit_event(
            audit_dir,
            event_path,
            pending_path,
            event_bytes,
            audit_identity,
        )


def _publish_runtime_audit_event(
    audit_dir: Path,
    event_path: Path,
    pending_path: Path,
    event_bytes: bytes,
    audit_identity: tuple[int, int],
) -> Path:
    pending_identity = None
    event_linked = False
    try:
        with create_exclusive_held_file(
            pending_path,
            share_write=False,
        ) as (stream, pending_identity):
            stream.write(event_bytes)
            stream.flush()
            os.fsync(stream.fileno())
            os.link(pending_path, event_path)
            event_linked = True
            if path_identity(audit_dir) != audit_identity:
                raise RuntimeError("Runtime audit directory identity changed")
            if path_identity(event_path) != pending_identity:
                raise RuntimeError(
                    f"Runtime audit event identity changed: {event_path}"
                )
        unlink_owned_path(pending_path, pending_identity)
        if path_identity(audit_dir) != audit_identity:
            raise RuntimeError("Runtime audit directory identity changed")
        return event_path
    except Exception:
        for candidate in (
            (event_path if event_linked else None),
            pending_path,
        ):
            if candidate is None or pending_identity is None:
                continue
            try:
                if path_identity(candidate) == pending_identity:
                    unlink_owned_path(candidate, pending_identity)
            except Exception:
                pass
        raise
