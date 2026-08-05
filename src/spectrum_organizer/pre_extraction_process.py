from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

from spectrum_organizer.workflow.extraction_contracts import (
    _context_to_payload,
)
from spectrum_organizer.workflow.extraction_ipc import (
    _write_json_atomic_exclusive_evidence,
)
from spectrum_organizer.workflow.pre_extraction_service import (
    prepare_extraction_context,
)
from spectrum_organizer.runtime_audit import record_runtime_audit_event
from spectrum_organizer.safety.identity_paths import read_held_file_bytes
from spectrum_organizer.safety.owned_paths import acquire_run_lease, read_ownership
from spectrum_organizer.safety.process_job import wait_for_parent_start_gate


def pre_extraction_process_main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 3:
        return 2
    expected_manifest_sha256, manifest_arg, result_arg = args
    manifest_path, result_path = map(Path, (manifest_arg, result_arg))
    lease = None
    result_path_is_owned = False
    try:
        wait_for_parent_start_gate()
        temp_root = manifest_path.resolve().parent
        ownership = read_ownership(temp_root)
        allowed_children = {path.resolve() for path in ownership.allowed_children}
        result_pending_path = result_path.with_name(f"{result_path.name}.pending")
        if (
            result_path.resolve().parent != temp_root
            or manifest_path.resolve() not in allowed_children
            or result_path.resolve() not in allowed_children
            or result_pending_path.resolve() not in allowed_children
        ):
            raise ValueError("Pre-extraction IPC path is not registered")
        result_path_is_owned = True
        manifest_identity = dict(ownership.allowed_child_identities).get(
            manifest_path.resolve()
        )
        if manifest_identity is None:
            raise ValueError("Pre-extraction manifest creation identity is not registered")
        manifest_bytes = read_held_file_bytes(
            manifest_path,
            manifest_identity,
        )
        if hashlib.sha256(manifest_bytes).hexdigest() != expected_manifest_sha256:
            raise ValueError("Pre-extraction manifest content authentication failed")
        payload = json.loads(manifest_bytes.decode("utf-8"))
        if (
            Path(payload["temp_root"]).resolve() != temp_root
            or ownership.run_id != payload["run_id"]
            or ownership.marker_id != payload["marker_id"]
        ):
            raise RuntimeError("Pre-extraction ownership identity mismatch")
        lease = acquire_run_lease(ownership.temp_root)
        ownership = lease.ownership
        context = prepare_extraction_context(
            selected_source_paths=payload["selected_source_paths"],
            output_parent=payload["output_parent"],
            settings_snapshot=payload["settings_snapshot"],
            protected_paths=payload["protected_paths"],
            ownership=ownership,
            timestamp=str(payload["timestamp"]),
        )
        context_payload = _context_to_payload(context)
        record_runtime_audit_event(
            "pre_extraction_context",
            context_payload,
        )
        result = {"ok": True, "context": context_payload}
        return_code = 0
    except Exception as exc:
        result = {"ok": False, "error": str(exc), "error_type": type(exc).__name__}
        return_code = 1
    created_temp_identities = []
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
    raise SystemExit(pre_extraction_process_main())
