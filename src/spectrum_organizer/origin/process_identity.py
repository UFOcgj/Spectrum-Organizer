from __future__ import annotations

import json
import os
from pathlib import Path

from spectrum_organizer.runtime_audit import (
    record_runtime_audit_event,
    runtime_audit_enabled,
)
from spectrum_organizer.safety.identity_paths import (
    hold_file_identity,
    path_identity,
)
from spectrum_organizer.safety.process_boundary import (
    ProcessIdentity,
    default_origin_process_probe,
)


ORIGIN_PID_HELPER_PATH = Path(__file__).with_name("origin_current_pid.c")
ORIGIN_IDENTITY_HANDOFF_PATH_ENV = (
    "SPECTRUM_ORGANIZER_ORIGIN_IDENTITY_HANDOFF_PATH"
)
ORIGIN_IDENTITY_HANDOFF_TOKEN_ENV = (
    "SPECTRUM_ORGANIZER_ORIGIN_IDENTITY_HANDOFF_TOKEN"
)


def record_origin_session_identity(
    origin: object,
    *,
    role: str,
    attempt_binding: dict[str, object],
) -> ProcessIdentity | None:
    audit_enabled = runtime_audit_enabled()
    handoff_path = os.environ.get(ORIGIN_IDENTITY_HANDOFF_PATH_ENV)
    handoff_token = os.environ.get(ORIGIN_IDENTITY_HANDOFF_TOKEN_ENV)
    if not audit_enabled and handoff_path is None and handoff_token is None:
        return None
    if bool(handoff_path) != bool(handoff_token):
        raise RuntimeError("Origin identity handoff configuration is invalid")
    if role not in {"extraction", "output", "verifier"}:
        raise RuntimeError(f"Unknown Origin worker role: {role}")
    binding = _validated_attempt_binding(role, attempt_binding)
    helper = ORIGIN_PID_HELPER_PATH.resolve(strict=True)
    helper_identity = path_identity(helper)
    with hold_file_identity(helper, helper_identity, allow_write=False):
        helper_text = str(helper).replace("\\", "\\\\").replace('"', '\\"')
        load_result = origin.lt_int(f'run.LoadOC("{helper_text}", 0)')
    if load_result != 0:
        raise RuntimeError(f"Origin session PID helper load failed: {load_result}")
    pid = origin.lt_int("spectrum_organizer_current_pid()")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise RuntimeError("Origin session returned an invalid PID")
    matches = tuple(
        process
        for process in default_origin_process_probe(timeout=5.0)
        if process.pid == pid
    )
    if len(matches) != 1:
        raise RuntimeError(f"Could not verify Origin session PID: {pid}")
    identity = matches[0].identity
    payload = {
        "role": role,
        "pid": identity.pid,
        "start_time_ns": identity.start_time_ns,
        "attempt_binding": binding,
    }
    if audit_enabled:
        record_runtime_audit_event(
            "origin_process_identity",
            payload,
        )
    if handoff_path is not None:
        _publish_identity_handoff(
            Path(handoff_path),
            {
                "schema_version": 1,
                "token": handoff_token,
                **payload,
            },
        )
    return identity


def _publish_identity_handoff(
    path: Path,
    payload: dict[str, object],
) -> None:
    target = Path(path).resolve(strict=True)
    expected_identity = path_identity(target)
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
    ).encode("utf-8")
    with target.open("r+b", buffering=0) as stream:
        status = os.fstat(stream.fileno())
        if (status.st_dev, status.st_ino) != expected_identity:
            raise RuntimeError("Origin identity handoff file identity changed")
        stream.seek(0)
        stream.write(encoded)
        stream.truncate()
        stream.flush()
        os.fsync(stream.fileno())
    if path_identity(target) != expected_identity:
        raise RuntimeError("Origin identity handoff file identity changed")


def _validated_attempt_binding(
    role: str,
    binding: dict[str, object],
) -> dict[str, object]:
    if not isinstance(binding, dict):
        raise RuntimeError("Origin worker attempt binding is invalid")
    if role == "extraction":
        required = {"run_id", "source_id", "reader_attempt"}
        attempt_field = "reader_attempt"
    else:
        required = {"approved_snapshot_id", "run_staging_root", "attempt"}
        attempt_field = "attempt"
    if (
        set(binding) != required
        or any(
            not isinstance(binding[field], str) or not binding[field]
            for field in required - {attempt_field}
        )
        or type(binding[attempt_field]) is not int
        or binding[attempt_field] not in {1, 2}
    ):
        raise RuntimeError("Origin worker attempt binding is invalid")
    return dict(binding)
