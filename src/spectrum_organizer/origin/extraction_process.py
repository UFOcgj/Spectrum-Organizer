from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys

from spectrum_organizer.origin.reader_service import (
    run_reader_source_extraction_phase,
)
from spectrum_organizer.workflow.extraction_contracts import (
    READER_SIDECAR_AUTH_ENV,
    _reader_command_from_payload,
    _reader_summary_to_payload,
)
from spectrum_organizer.workflow.extraction_ipc import (
    _write_json_atomic_exclusive_evidence,
)
from spectrum_organizer.origin.ipc_auth import validate_sidecar_auth_key
from spectrum_organizer.safety.identity_paths import read_held_file_bytes
from spectrum_organizer.safety.owned_paths import (
    acquire_run_lease,
    bind_allowed_child_identity,
    read_ownership,
)
from spectrum_organizer.safety.process_job import wait_for_parent_start_gate


def extraction_process_main(argv=None, *, extraction_runner=run_reader_source_extraction_phase) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 3:
        return 2
    expected_manifest_sha256, manifest_arg, result_arg = args
    manifest_path, result_path = map(Path, (manifest_arg, result_arg))
    lease = None
    result_path_is_owned = False
    created_temp_identities = []
    try:
        wait_for_parent_start_gate()
        temp_root = manifest_path.resolve().parent
        ownership = read_ownership(temp_root)
        result_pending_path = result_path.with_name(f"{result_path.name}.pending")
        if (
            manifest_path.resolve().parent != temp_root
            or result_path.resolve().parent != temp_root
            or manifest_path.resolve() not in ownership.allowed_children
            or result_path.resolve() not in ownership.allowed_children
            or result_pending_path.resolve() not in ownership.allowed_children
        ):
            raise ValueError("谱图提取 reader IPC 路径未登记")
        result_path_is_owned = True
        manifest_identity = dict(ownership.allowed_child_identities).get(
            manifest_path.resolve()
        )
        if manifest_identity is None:
            raise ValueError("谱图提取 reader manifest 创建身份未登记")
        manifest_bytes = read_held_file_bytes(
            manifest_path,
            manifest_identity,
        )
        if hashlib.sha256(manifest_bytes).hexdigest() != expected_manifest_sha256:
            raise ValueError("谱图提取 reader manifest 内容认证失败")
        payload = json.loads(manifest_bytes.decode("utf-8"))
        command = _reader_command_from_payload(payload)
        if command.snapshot_path.resolve().parent != temp_root:
            raise ValueError("谱图提取 reader snapshot 与 IPC 不属于同一任务")
        lease = acquire_run_lease(temp_root)
        ownership = lease.ownership
        if extraction_runner is run_reader_source_extraction_phase:
            sidecar_auth_key = os.environ.get(READER_SIDECAR_AUTH_ENV)
            validate_sidecar_auth_key(sidecar_auth_key)

            def bind_created_temp(path, identity):
                nonlocal ownership
                created_path = Path(path)
                ownership = bind_allowed_child_identity(
                    ownership,
                    created_path,
                    expected_identity=identity,
                )
                created_temp_identities.append((created_path, identity))

            summary = extraction_runner(
                command,
                cleanup_identity_callback=bind_created_temp,
                sidecar_auth_key=sidecar_auth_key,
            )
        else:
            summary = extraction_runner(command)
        result = {"ok": True, "summary": _reader_summary_to_payload(summary)}
        return_code = 0
    except Exception as exc:
        result = {
            "ok": False,
            "error": str(exc),
            "error_type": type(exc).__name__,
            "error_notes": list(getattr(exc, "__notes__", ())),
        }
        return_code = 1
    try:
        if result_path_is_owned:
            try:
                result_evidence = _write_json_atomic_exclusive_evidence(
                    result_path,
                    result,
                )
            except Exception as exc:
                created_temp_identities.extend(
                    getattr(exc, "retained_owned_identities", ())
                )
                result_evidence = None
                return_code = 1
            sys.stdout.write(
                json.dumps(
                    {
                        "result_identity": (
                            None
                            if result_evidence is None
                            else list(result_evidence.identity)
                        ),
                        "result_sha256": (
                            None
                            if result_evidence is None
                            else result_evidence.sha256
                        ),
                        "created_temp_identities": [
                            {"path": str(path), "identity": list(identity)}
                            for path, identity in created_temp_identities
                        ],
                    }
                )
            )
            sys.stdout.flush()
    except Exception:
        return_code = 1
    finally:
        if lease is not None:
            lease.close()
    return return_code


if __name__ == "__main__":
    raise SystemExit(extraction_process_main())
