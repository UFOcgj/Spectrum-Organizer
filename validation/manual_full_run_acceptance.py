from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from typing import Callable, Iterable

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from spectrum_organizer.product_runner import (
    ApprovedPreExtractionRunContext,
    ExtractionCleanupBlockedError,
    ExtractionPhaseSummary,
    ExtractionSubprocessRunner,
    FinalProcessCountHook,
    prepare_approved_pre_extraction_context,
    _context_from_payload,
)
from spectrum_organizer.runtime_audit import RUNTIME_AUDIT_DIR_ENV
from spectrum_organizer.reporting.run_report import (
    APPROVED_OUTPUT_LEDGER_SECTION_TITLES,
)
from spectrum_organizer.safety.fingerprints import (
    SourceSnapshot,
    snapshot_sources,
    verify_sources_unchanged,
)
from spectrum_organizer.safety.identity_paths import (
    create_exclusive_held_directory,
    lexical_path_exists,
    remove_empty_owned_directory,
)
from spectrum_organizer.safety.owned_paths import cleanup_owned_temp_root
from spectrum_organizer.safety.process_boundary import (
    ProcessIdentity,
    WindowsOriginProcessController,
    default_origin_process_probe,
)
from spectrum_organizer.ui.dialog_port import QtManualDialogPort
from validation.evidence_lock import (
    OwnedDirectoryLockError,
    acquire_owned_directory_lock,
    release_owned_directory_lock,
)


DEFAULT_EVIDENCE_ROOT = ROOT / "docs" / "superpowers" / "evidence" / "product-smoke"
DEFAULT_SETTINGS = {
    "s1Limit": 2_000_000,
    "steadyEmissionY": "S1c",
    "allowMissingS1": False,
}
class ManualAcceptanceError(RuntimeError):
    pass


@dataclass(frozen=True)
class _CreatedEvidenceDirectory:
    path: Path
    identity: tuple[int, int]


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        if args.package_dir is not None:
            evidence_dir = run_guided_full_acceptance(
                package_dir=args.package_dir,
                evidence_root=DEFAULT_EVIDENCE_ROOT,
                cycle=args.cycle,
            )
            print(f"guided full-run evidence: {evidence_dir}")
            return 0
        if args.phase != "extraction-only":
            raise ManualAcceptanceError(f"Unsupported phase: {args.phase}")
        application = _ensure_qapplication()
        confirmed = _confirm_settings_with_qt()
        if confirmed is None:
            raise ManualAcceptanceError("Preflight settings were not confirmed")
        evidence_dir = run_extraction_only(
            evidence_root=DEFAULT_EVIDENCE_ROOT,
            settings_snapshot={
                "s1Limit": confirmed["s1_limit"],
                "steadyEmissionY": confirmed["steady_emission_y"],
                "allowMissingS1": confirmed["allow_missing_s1"],
            },
        )
        del application
    except Exception as exc:
        print(f"manual acceptance failed: {exc}", file=sys.stderr)
        for note in getattr(exc, "__notes__", ()):
            print(f"note: {note}", file=sys.stderr)
        return 1
    print(f"extraction-only evidence: {evidence_dir}")
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Spectrum Organizer manual acceptance helper")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--phase", choices=("extraction-only",))
    mode.add_argument("--package-dir", type=Path)
    parser.add_argument(
        "--cycle",
        choices=("success", "cancellation"),
        default="success",
    )
    args = parser.parse_args(argv)
    if args.phase is not None and args.cycle != "success":
        parser.error("--cycle is only valid with --package-dir")
    return args


def run_guided_full_acceptance(
    *,
    package_dir: Path,
    evidence_root: Path = DEFAULT_EVIDENCE_ROOT,
    cycle: str = "success",
    process_launcher=subprocess.Popen,
    final_origin_process_count: Callable[[], int] | None = None,
    final_product_process_count: Callable[[Path], int] | None = None,
    freshness_selector: Callable[[tuple[Path, ...]], Path] | None = None,
    timestamp_factory: Callable[[], str] | None = None,
) -> Path:
    if cycle not in {"success", "cancellation"}:
        raise ManualAcceptanceError(f"Unsupported guided acceptance cycle: {cycle}")
    package_dir = Path(package_dir).resolve()
    executable = package_dir / "Spectrum Organizer.exe"
    if not executable.is_file():
        raise ManualAcceptanceError(
            f"Packaged executable is missing: {executable}"
        )
    created_evidence = _create_guided_evidence_dir(
        Path(evidence_root),
        timestamp_factory,
        prefix=(
            "full-run-manual"
            if cycle == "success"
            else "full-run-manual-cancellation"
        ),
    )
    evidence_dir = created_evidence.path
    runtime_appdata = evidence_dir / "runtime-localappdata"
    runtime_appdata.mkdir()
    runtime_audit_dir = evidence_dir / "runtime-audit"
    runtime_audit_dir.mkdir()
    _write_text_exclusive(
        evidence_dir / "manual-acceptance-checklist.md",
        _guided_acceptance_checklist(cycle),
    )
    environment = os.environ.copy()
    environment["LOCALAPPDATA"] = str(runtime_appdata)
    environment[RUNTIME_AUDIT_DIR_ENV] = str(runtime_audit_dir)
    process = process_launcher(
        [str(executable)],
        cwd=str(package_dir),
        env=environment,
    )
    return_code = int(process.wait())
    if final_origin_process_count is None:
        audit_events = _read_runtime_audit_events(runtime_audit_dir)
        required_identity_roles = {"extraction"}
        if cycle == "success":
            required_identity_roles.update(("output", "verifier"))
        else:
            required_identity_roles.update(
                event["payload"].get("role")
                for event in audit_events
                if event["event_type"] == "origin_worker_target_attempt"
                and event["payload"].get("role") in {"output", "verifier"}
            )
        owned_origin_identities = _audited_origin_process_identities(
            audit_events,
            required_roles=frozenset(required_identity_roles),
        )
        process_count, visible_origin_process_count = (
            _settled_origin_process_counts(
                owned_identities=owned_origin_identities
            )
        )
    else:
        process_count = int(final_origin_process_count())
        visible_origin_process_count = 0
    product_process_count = int(
        (final_product_process_count or _final_product_process_count)(
            executable
        )
    )
    if return_code != 0:
        raise ManualAcceptanceError(
            f"Packaged application exited with code {return_code}; "
            f"evidence: {evidence_dir}"
        )
    if process_count != 0:
        raise ManualAcceptanceError(
            f"Hidden Origin worker process count is {process_count} after the guided run; "
            f"evidence: {evidence_dir}"
        )
    if product_process_count != 0:
        raise ManualAcceptanceError(
            f"Product worker process count is {product_process_count} after the guided run; "
            f"evidence: {evidence_dir}"
        )
    sample_library_evidence = _reconcile_zero_sample_library_write(
        runtime_appdata,
        runtime_audit_dir=runtime_audit_dir,
    )

    if cycle == "cancellation":
        evidence = _reconcile_guided_cancellation_evidence(
            runtime_audit_dir=runtime_audit_dir,
        )
        required_evidence = [
            "selected-source-fingerprints-before.json",
            "selected-source-fingerprints-after.json",
            "worker-open-targets.json",
            "cancellation-cleanup-summary.json",
            "manual-acceptance-checklist.md",
        ]
        _write_json_exclusive(
            evidence_dir / required_evidence[0],
            {"snapshots": evidence["fingerprints_before"]},
        )
        _write_json_exclusive(
            evidence_dir / required_evidence[1],
            {"snapshots": evidence["fingerprints_after"]},
        )
        _write_json_exclusive(
            evidence_dir / required_evidence[2],
            evidence["worker_open_targets"],
        )
        _write_json_exclusive(
            evidence_dir / required_evidence[3],
            evidence["cancellation_cleanup"],
        )
        _write_json_exclusive(
            evidence_dir / "manual-cancellation-summary.json",
            {
                "status": "cancellation_evidence_ready_manual_checks_pending",
                "package_dir": str(package_dir),
                "executable": str(executable),
                "runtime_appdata": str(runtime_appdata),
                "selected_source_paths": evidence["selected_source_paths"],
                "process_returncode": return_code,
                "final_origin_process_count": process_count,
                "final_visible_user_origin_process_count": visible_origin_process_count,
                "final_product_process_count": product_process_count,
                "no_publication_or_staging": True,
                "required_evidence": required_evidence,
            },
        )
        return evidence_dir

    evidence = _reconcile_guided_runtime_evidence(
        runtime_audit_dir=runtime_audit_dir,
        freshness_selector=freshness_selector or _prompt_freshness_attestation,
        evidence_root=Path(evidence_root),
    )
    required_evidence = [
        "selected-source-fingerprints-before.json",
        "selected-source-fingerprints-after.json",
        "worker-open-targets.json",
        "output-verifier-summary.json",
        "count-reconciliation-summary.json",
        "sample-library-write-summary.json",
        "manual-acceptance-checklist.md",
    ]
    _write_json_exclusive(
        evidence_dir / required_evidence[0],
        {"snapshots": evidence["fingerprints_before"]},
    )
    _write_json_exclusive(
        evidence_dir / required_evidence[1],
        {"snapshots": evidence["fingerprints_after"]},
    )
    _write_json_exclusive(
        evidence_dir / required_evidence[2],
        evidence["worker_open_targets"],
    )
    _write_json_exclusive(
        evidence_dir / required_evidence[3],
        evidence["output_verifier_summary"],
    )
    _write_json_exclusive(
        evidence_dir / required_evidence[4],
        evidence["count_reconciliation_summary"],
    )
    _write_json_exclusive(
        evidence_dir / required_evidence[5],
        sample_library_evidence,
    )
    summary = {
        "status": "automated_evidence_ready_manual_checks_pending",
        "package_dir": str(package_dir),
        "executable": str(executable),
        "runtime_appdata": str(runtime_appdata),
        "selected_source_paths": evidence["selected_source_paths"],
        "operator_freshness_attestation": evidence["freshness_attestation"],
        "process_returncode": return_code,
        "final_origin_process_count": process_count,
        "final_visible_user_origin_process_count": visible_origin_process_count,
        "final_product_process_count": product_process_count,
        "sample_library_zero_write": True,
        "required_evidence": required_evidence,
    }
    _write_json_exclusive(
        evidence_dir / "manual-full-run-summary.json",
        summary,
    )
    return evidence_dir


def _reconcile_zero_sample_library_write(
    runtime_appdata: Path,
    *,
    runtime_audit_dir: Path | None = None,
) -> dict[str, object]:
    data_dir = Path(runtime_appdata) / "Spectrum Organizer" / "data"
    database = data_dir / "sample_library.sqlite3"
    candidates = tuple(
        path
        for path in (
            database,
            Path(f"{database}-wal"),
            Path(f"{database}-shm"),
            Path(f"{database}-journal"),
            *(data_dir / "backups").glob("sample_library_*.sqlite3*"),
        )
        if lexical_path_exists(path)
    )
    if candidates:
        raise ManualAcceptanceError(
            "Isolated sample library was written during acceptance: "
            + ", ".join(str(path) for path in candidates)
        )
    mutation_events = (
        tuple(
            event
            for event in _read_runtime_audit_events(runtime_audit_dir)
            if event.get("event_type") == "sample_library_write_attempt"
        )
        if runtime_audit_dir is not None
        else ()
    )
    if mutation_events:
        raise ManualAcceptanceError(
            "Isolated sample library write was attempted during acceptance"
        )
    return {
        "status": "zero_write_verified",
        "database_path": str(database),
        "mutation_events": 0,
        "written_paths": [],
    }


def _create_guided_evidence_dir(
    evidence_root: Path,
    timestamp_factory: Callable[[], str] | None,
    *,
    prefix: str = "full-run-manual",
) -> _CreatedEvidenceDirectory:
    timestamp = (
        timestamp_factory()
        if timestamp_factory is not None
        else datetime.now().strftime("%Y%m%d_%H%M%S")
    )
    evidence_root.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "_01", "_02", "_03", "_04", "_05"):
        candidate = evidence_root / f"{prefix}-{timestamp}{suffix}"
        try:
            with create_exclusive_held_directory(candidate) as (
                created_path,
                identity,
            ):
                return _CreatedEvidenceDirectory(created_path, identity)
        except FileExistsError:
            continue
    raise ManualAcceptanceError(
        "Could not create a unique guided-acceptance evidence directory"
    )


def _settled_origin_process_counts(
    *,
    process_probe=default_origin_process_probe,
    owned_identities: frozenset[ProcessIdentity] = frozenset(),
    timeout: float = 10.0,
    poll_interval: float = 0.25,
    monotonic=time.monotonic,
    sleep=time.sleep,
) -> tuple[int, int]:
    deadline = monotonic() + timeout
    while True:
        processes = tuple(process_probe())
        visible = tuple(
            process
            for process in processes
            if not (
                process.program_owned or process.identity in owned_identities
            )
            and (process.visible or process.taskbar_visible)
        )
        residual = tuple(
            process
            for process in processes
            if process.program_owned
            or process.identity in owned_identities
            or (not process.visible and not process.taskbar_visible)
        )
        if not residual or monotonic() >= deadline:
            return len(residual), len(visible)
        sleep(poll_interval)


def _audited_origin_process_identities(
    events: tuple[dict[str, object], ...],
    *,
    required_roles: frozenset[str],
) -> frozenset[ProcessIdentity]:
    allowed_roles = {"extraction", "output", "verifier"}
    if not required_roles or not required_roles.issubset(allowed_roles):
        raise ManualAcceptanceError(
            "Origin process identity audit coverage is invalid"
        )
    expected_attempts = set()
    for event in events:
        if event["event_type"] == "origin_extraction_target_attempt":
            payload = event["payload"]
            key = _origin_attempt_key(
                "extraction",
                {
                    "run_id": payload.get("run_id"),
                    "source_id": payload.get("source_id"),
                    "reader_attempt": payload.get("reader_attempt"),
                },
            )
        elif event["event_type"] == "origin_worker_target_attempt":
            payload = event["payload"]
            role = payload.get("role")
            if role not in {"output", "verifier"}:
                raise ManualAcceptanceError(
                    "Origin process identity audit coverage is invalid"
                )
            key = _origin_attempt_key(
                role,
                {
                    "approved_snapshot_id": payload.get(
                        "approved_snapshot_id"
                    ),
                    "run_staging_root": payload.get("run_staging_root"),
                    "attempt": payload.get("attempt"),
                },
            )
        else:
            continue
        if key in expected_attempts:
            raise ManualAcceptanceError(
                "Origin process identity audit coverage is invalid"
            )
        expected_attempts.add(key)

    identities = set()
    identity_by_attempt = {}
    attempt_by_identity = {}
    identity_workers = set()
    for event in events:
        if event["event_type"] != "origin_process_identity":
            continue
        payload = event["payload"]
        if (
            set(payload) != {"role", "pid", "start_time_ns", "attempt_binding"}
            or payload["role"] not in allowed_roles
            or isinstance(event.get("process_id"), bool)
            or not isinstance(event.get("process_id"), int)
            or event["process_id"] <= 0
            or isinstance(payload["pid"], bool)
            or not isinstance(payload["pid"], int)
            or payload["pid"] <= 0
            or isinstance(payload["start_time_ns"], bool)
            or not isinstance(payload["start_time_ns"], int)
            or payload["start_time_ns"] <= 0
        ):
            raise ManualAcceptanceError(
                "Origin process identity runtime audit is invalid"
            )
        attempt_key = _origin_attempt_key(
            payload["role"], payload["attempt_binding"]
        )
        worker_key = (
            payload["role"],
            event["process_id"],
            _runtime_process_instance_id(event),
        )
        identity = ProcessIdentity(
            pid=payload["pid"],
            start_time_ns=payload["start_time_ns"],
        )
        if (
            attempt_key in identity_by_attempt
            or identity in attempt_by_identity
            or worker_key in identity_workers
        ):
            raise ManualAcceptanceError(
                "Origin process identity audit coverage is invalid"
            )
        identity_by_attempt[attempt_key] = worker_key
        attempt_by_identity[identity] = attempt_key
        identity_workers.add(worker_key)
        identities.add(identity)

    if set(identity_by_attempt) != expected_attempts:
        raise ManualAcceptanceError(
            "Origin process identity audit coverage is incomplete"
        )
    observed_roles = {key[0] for key in identity_by_attempt}
    if not required_roles.issubset(observed_roles):
        raise ManualAcceptanceError(
            "Origin process identity audit coverage is incomplete"
        )

    for event in events:
        if event["event_type"] != "origin_worker_targets":
            continue
        payload = event["payload"]
        role = payload.get("role")
        process_id = event.get("process_id")
        process_instance_id = _runtime_process_instance_id(event)
        if role == "extraction":
            binding = {
                "run_id": payload.get("run_id"),
                "source_id": payload.get("source_id"),
                "reader_attempt": payload.get("reader_attempt"),
            }
        elif role in {"output", "verifier"}:
            binding = {
                "approved_snapshot_id": payload.get(
                    "approved_snapshot_id"
                ),
                "run_staging_root": payload.get("run_staging_root"),
                "attempt": payload.get("attempt"),
            }
        else:
            raise ManualAcceptanceError(
                "Origin process identity audit coverage is invalid"
            )
        attempt_key = _origin_attempt_key(role, binding)
        if (
            isinstance(process_id, bool)
            or not isinstance(process_id, int)
            or process_id <= 0
            or identity_by_attempt.get(attempt_key)
            != (role, process_id, process_instance_id)
        ):
            raise ManualAcceptanceError(
                "Origin process identity audit coverage is invalid"
            )
    return frozenset(identities)


def _origin_attempt_key(role: str, binding: object) -> tuple[object, ...]:
    if role == "extraction":
        fields = {"run_id", "source_id", "reader_attempt"}
        attempt_field = "reader_attempt"
    elif role in {"output", "verifier"}:
        fields = {"approved_snapshot_id", "run_staging_root", "attempt"}
        attempt_field = "attempt"
    else:
        raise ManualAcceptanceError(
            "Origin process identity audit coverage is invalid"
        )
    if not isinstance(binding, dict) or set(binding) != fields:
        raise ManualAcceptanceError(
            "Origin process identity audit coverage is invalid"
        )
    attempt = binding[attempt_field]
    if type(attempt) is not int or attempt not in {1, 2}:
        raise ManualAcceptanceError(
            "Origin process identity audit coverage is invalid"
        )
    if role == "extraction":
        run_id = binding["run_id"]
        source_id = binding["source_id"]
        if (
            not isinstance(run_id, str)
            or not run_id
            or not isinstance(source_id, str)
            or not source_id
        ):
            raise ManualAcceptanceError(
                "Origin process identity audit coverage is invalid"
            )
        return role, run_id, source_id, attempt
    snapshot_id = binding["approved_snapshot_id"]
    staging_root = binding["run_staging_root"]
    if (
        not isinstance(snapshot_id, str)
        or not snapshot_id
        or not isinstance(staging_root, str)
        or not Path(staging_root).is_absolute()
    ):
        raise ManualAcceptanceError(
            "Origin process identity audit coverage is invalid"
        )
    return role, snapshot_id, _path_key(Path(staging_root)), attempt


def _final_origin_process_count() -> int:
    residual_count, _visible_count = _settled_origin_process_counts()
    return residual_count


def _final_product_process_count(executable: Path) -> int:
    environment = os.environ.copy()
    environment["SPECTRUM_ORGANIZER_PROCESS_QUERY_TARGET"] = str(
        Path(executable).resolve()
    )
    script = (
        "$target=[IO.Path]::GetFullPath("
        "$env:SPECTRUM_ORGANIZER_PROCESS_QUERY_TARGET); "
        "@(Get-CimInstance Win32_Process -Filter \"Name = 'Spectrum Organizer.exe'\" | "
        "Where-Object { $_.ExecutablePath -and "
        "[IO.Path]::GetFullPath($_.ExecutablePath) -eq $target }).Count"
    )
    completed = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            script,
        ],
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )
    if completed.returncode != 0:
        raise ManualAcceptanceError(
            completed.stderr.strip() or "Product process query failed"
        )
    try:
        return int(completed.stdout.strip() or "0")
    except ValueError as exc:
        raise ManualAcceptanceError(
            "Product process query returned invalid output"
        ) from exc


def _reconcile_guided_runtime_evidence(
    *,
    runtime_audit_dir: Path,
    freshness_selector: Callable[[tuple[Path, ...]], Path],
    evidence_root: Path,
) -> dict[str, object]:
    events = _read_runtime_audit_events(runtime_audit_dir)
    context, selected_paths, before, after = _context_fingerprint_evidence(
        events,
        phase="guided run",
    )

    worker_evidence = _reconcile_worker_targets(
        events,
        selected_paths=selected_paths,
        selected_snapshots=after,
        context=context,
    )
    output_event = next(
        event
        for event in worker_evidence["events"]
        if event["role"] == "output"
    )
    attempt_summary = _reconcile_output_attempts(
        events,
        context=context,
        approved_snapshot_id=output_event["approved_snapshot_id"],
    )
    _bind_worker_attempts_to_output_stage(
        worker_evidence=worker_evidence,
        attempt_summary=attempt_summary,
    )
    output_summary = _reconcile_output_and_report(
        events,
        worker_evidence,
        selected_paths=selected_paths,
        fingerprints_before=before,
        fingerprints_after=after,
        attempt_summary=attempt_summary,
    )
    approved_report_ledger = _reconcile_approved_report_ledger(
            events,
            approved_snapshot_id=output_event["approved_snapshot_id"],
            report_text=output_summary["report_text"],
    )
    output_summary["approved_report_ledger"] = approved_report_ledger
    output_summary["owned_cleanup"] = _reconcile_owned_output_cleanup(
        context=context,
        attempt_summary=attempt_summary,
        phase="success",
    )
    count_summary = _reconcile_counts(
        events,
        output_summary=output_summary,
        worker_evidence=worker_evidence,
    )
    freshness_attestation = _validated_freshness_attestation(
        selected_path=Path(freshness_selector(selected_paths)),
        selected_paths=selected_paths,
        selected_snapshots=after,
        recognized_source_paths=tuple(
            Path(path)
            for path in approved_report_ledger[
                "recognized_source_paths"
            ]
        ),
        evidence_root=Path(evidence_root),
        current_evidence_dir=Path(runtime_audit_dir).parent,
    )

    return {
        "selected_source_paths": [str(path) for path in selected_paths],
        "fingerprints_before": [
            _snapshot_to_evidence(snapshot) for snapshot in before
        ],
        "fingerprints_after": [
            _snapshot_to_evidence(snapshot) for snapshot in after
        ],
        "worker_open_targets": worker_evidence,
        "output_verifier_summary": output_summary,
        "count_reconciliation_summary": count_summary,
        "freshness_attestation": freshness_attestation,
    }


def _reconcile_guided_cancellation_evidence(
    *,
    runtime_audit_dir: Path,
) -> dict[str, object]:
    events = _read_runtime_audit_events(runtime_audit_dir)
    context, selected_paths, before, after = _context_fingerprint_evidence(
        events,
        phase="cancellation cycle",
    )
    worker_evidence = _reconcile_worker_targets(
        events,
        selected_paths=selected_paths,
        selected_snapshots=after,
        context=context,
        require_completed_output=False,
    )
    attempt_summary = _reconcile_output_attempts(
        events,
        context=context,
        require_output_phase=True,
    )
    _require_cancellation_worker_attempts(
        worker_evidence=worker_evidence,
        attempt_summary=attempt_summary,
    )
    if any(
        event["event_type"] == "publication_committed"
        for event in events
    ):
        raise ManualAcceptanceError(
            "Cancellation audit contains a publication commit"
        )
    output_parent = Path(context.output_parent)
    if not output_parent.is_dir() or any(output_parent.iterdir()):
        raise ManualAcceptanceError(
            "Cancellation acceptance requires an empty output folder and must leave it empty"
        )
    if (
        not attempt_summary["output_parent_existed_before"]
        or attempt_summary["output_parent_entries_before"]
    ):
        raise ManualAcceptanceError(
            "Cancellation acceptance output folder was not empty before output started"
        )
    owned_cleanup = _reconcile_owned_output_cleanup(
        context=context,
        attempt_summary=attempt_summary,
        phase="cancellation",
    )
    return {
        "selected_source_paths": [str(path) for path in selected_paths],
        "fingerprints_before": [
            _snapshot_to_evidence(snapshot) for snapshot in before
        ],
        "fingerprints_after": [
            _snapshot_to_evidence(snapshot) for snapshot in after
        ],
        "worker_open_targets": worker_evidence,
        "cancellation_cleanup": {
            **attempt_summary,
            "output_parent_empty_after": True,
            **owned_cleanup,
            "publication_absent": True,
        },
    }


def _context_fingerprint_evidence(
    events: tuple[dict[str, object], ...],
    *,
    phase: str,
) -> tuple[
    ApprovedPreExtractionRunContext,
    tuple[Path, ...],
    tuple[SourceSnapshot, ...],
    tuple[SourceSnapshot, ...],
]:
    context_event = _single_event(events, "pre_extraction_context")
    try:
        context = _context_from_payload(context_event["payload"])
    except Exception as exc:
        raise ManualAcceptanceError(
            f"Invalid pre-extraction runtime audit: {exc}"
        ) from exc
    selected_paths = tuple(context.selected_source_paths)
    if not selected_paths:
        raise ManualAcceptanceError(
            "The pre-extraction runtime audit contains no selected sources"
        )
    before = tuple(context.source_fingerprints_before)
    after = tuple(snapshot_sources(list(selected_paths), []))
    if before != after:
        raise ManualAcceptanceError(
            f"Selected source fingerprints changed during the {phase}"
        )
    return context, selected_paths, before, after


def _read_runtime_audit_events(audit_dir: Path) -> tuple[dict[str, object], ...]:
    events = []
    for path in sorted(Path(audit_dir).glob("*.json")):
        event = _read_runtime_audit_event(path)
        _runtime_process_instance_id(event, path=path)
        events.append(event)
    return tuple(events)


def _read_historical_runtime_audit_events(
    audit_dir: Path,
) -> tuple[dict[str, object], ...]:
    return tuple(
        _read_runtime_audit_event(path)
        for path in sorted(Path(audit_dir).glob("*.json"))
    )


def _read_runtime_audit_event(path: Path) -> dict[str, object]:
    try:
        event = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ManualAcceptanceError(
            f"Runtime audit evidence cannot be read: {path}"
        ) from exc
    if (
        not isinstance(event, dict)
        or event.get("schema_version") != 1
        or not isinstance(event.get("event_type"), str)
        or not isinstance(event.get("payload"), dict)
    ):
        raise ManualAcceptanceError(
            f"Runtime audit evidence is invalid: {path}"
        )
    return event


def _runtime_process_instance_id(
    event: dict[str, object],
    *,
    path: Path | None = None,
) -> str:
    process_instance_id = event.get("process_instance_id")
    if (
        not isinstance(process_instance_id, str)
        or len(process_instance_id) != 32
        or any(
            character not in "0123456789abcdef"
            for character in process_instance_id
        )
    ):
        location = f": {path}" if path is not None else ""
        raise ManualAcceptanceError(
            f"Runtime audit process instance is invalid{location}"
        )
    return process_instance_id


def _single_event(
    events: tuple[dict[str, object], ...],
    event_type: str,
) -> dict[str, object]:
    matches = tuple(
        event for event in events if event["event_type"] == event_type
    )
    if len(matches) != 1:
        label = (
            "pre-extraction"
            if event_type == "pre_extraction_context"
            else event_type.replace("_", "-")
        )
        raise ManualAcceptanceError(
            f"Expected exactly one {label} runtime audit event"
        )
    return matches[0]


def _reconcile_worker_targets(
    events: tuple[dict[str, object], ...],
    *,
    selected_paths: tuple[Path, ...],
    context,
    selected_snapshots: tuple[SourceSnapshot, ...] | None = None,
    require_completed_output: bool = True,
) -> dict[str, object]:
    worker_events = tuple(
        event["payload"]
        for event in events
        if event["event_type"] == "origin_worker_targets"
    )
    target_attempts = tuple(
        event["payload"]
        for event in events
        if event["event_type"] == "origin_worker_target_attempt"
    )
    retry_cleanups = tuple(
        event["payload"]
        for event in events
        if event["event_type"] == "origin_worker_retry_cleanup"
    )
    extraction_attempts = tuple(
        event["payload"]
        for event in events
        if event["event_type"]
        == "origin_extraction_target_attempt"
    )
    extraction_retry_cleanups = tuple(
        event["payload"]
        for event in events
        if event["event_type"]
        == "origin_extraction_retry_cleanup"
    )
    grouped = {
        role: tuple(event for event in worker_events if event.get("role") == role)
        for role in ("extraction", "output", "verifier")
    }
    if any(
        event.get("role") not in grouped
        for event in worker_events
        if isinstance(event, dict)
    ):
        raise ManualAcceptanceError(
            "Worker open-target audit contains an unknown role"
        )
    if not grouped["extraction"]:
        raise ManualAcceptanceError("Extraction worker open-target evidence is missing")
    extraction_targets, extraction_identities = (
        _validate_extraction_worker_events(
            grouped["extraction"],
            extraction_attempts=extraction_attempts,
            extraction_retry_cleanups=extraction_retry_cleanups,
            context=context,
            selected_paths=selected_paths,
        )
    )
    if require_completed_output:
        if (
            not grouped["output"]
            or len(grouped["output"]) != len(grouped["verifier"])
        ):
            raise ManualAcceptanceError(
                "Expected paired successful output and verifier worker audit events"
            )
    elif (
        len(grouped["verifier"]) > len(grouped["output"])
        or len(grouped["output"]) - len(grouped["verifier"]) > 1
    ):
        raise ManualAcceptanceError(
            "Cancellation worker audit contains an invalid output/verifier sequence"
        )
    expected_source_ids = {
        f"S{index:04d}" for index in range(1, len(selected_paths) + 1)
    }
    observed_source_ids = {
        str(event.get("source_id")) for event in grouped["extraction"]
    }
    if observed_source_ids != expected_source_ids:
        raise ManualAcceptanceError(
            "Extraction worker audit does not cover every selected source"
        )
    original_keys = {_path_key(path) for path in selected_paths}
    original_identities = {
        (snapshot.device_id, snapshot.file_id)
        for snapshot in (selected_snapshots or ())
        if snapshot.device_id is not None and snapshot.file_id is not None
    }
    all_targets = list(extraction_targets)
    target_identities = list(extraction_identities)
    for event in worker_events:
        targets = event.get("open_targets")
        identities = event.get("open_target_identities")
        if (
            not isinstance(targets, list)
            or not targets
            or any(not isinstance(target, str) or not Path(target).is_absolute() for target in targets)
        ):
            raise ManualAcceptanceError("Worker open-target audit is invalid")
        if (
            not isinstance(identities, list)
            or len(identities) != len(targets)
        ):
            raise ManualAcceptanceError(
                "Worker open-target identity audit is missing or invalid"
            )
        for target, identity in zip(targets, identities, strict=True):
            if not _valid_target_identity(identity, target):
                raise ManualAcceptanceError(
                    "Worker open-target identity audit is missing or invalid"
                )
            target_identities.append(
                (identity["device_id"], identity["file_id"])
            )
        all_targets.extend(targets)
    _reconcile_worker_attempts(
        target_attempts,
        retry_cleanups,
        completed_events=worker_events,
        all_targets=all_targets,
        target_identities=target_identities,
    )
    source_hits = sorted(
        target for target in all_targets if _path_key(Path(target)) in original_keys
    )
    if source_hits:
        raise ManualAcceptanceError(
            "Worker open-target audit contains an original source path"
        )
    if original_identities.intersection(target_identities):
        raise ManualAcceptanceError(
            "Worker open-target audit contains an original source physical identity"
        )
    output_by_root = {
        event.get("run_staging_root"): event
        for event in grouped["output"]
    }
    verifier_by_root = {
        event.get("run_staging_root"): event
        for event in grouped["verifier"]
    }
    if (
        len(output_by_root) != len(grouped["output"])
        or len(verifier_by_root) != len(grouped["verifier"])
        or not set(verifier_by_root).issubset(output_by_root)
        or (
            require_completed_output
            and set(output_by_root) != set(verifier_by_root)
        )
    ):
        raise ManualAcceptanceError(
            "Output and verifier worker audits are not paired by staging root"
        )
    for root, verifier_event in verifier_by_root.items():
        output_event = output_by_root[root]
        if (
            output_event.get("approved_snapshot_id")
            != verifier_event.get("approved_snapshot_id")
            or verifier_event["open_targets"][0]
            != output_event["open_targets"][0]
        ):
            raise ManualAcceptanceError(
                "Output and verifier worker audits do not identify one approved staging project"
            )
    return {
        "original_source_hit": False,
        "selected_source_paths": [str(path) for path in selected_paths],
        "events": list(worker_events),
        "attempts": list(target_attempts),
        "retry_cleanups": list(retry_cleanups),
        "extraction_attempts": list(extraction_attempts),
        "extraction_retry_cleanups": list(
            extraction_retry_cleanups
        ),
    }


def _validate_extraction_worker_events(
    events: tuple[dict[str, object], ...],
    *,
    extraction_attempts: tuple[dict[str, object], ...],
    extraction_retry_cleanups: tuple[dict[str, object], ...],
    context,
    selected_paths: tuple[Path, ...],
) -> tuple[list[str], list[tuple[int, int]]]:
    owned_copies = tuple(
        Path(path) for path in context.run_owned_source_copy_paths
    )
    if len(owned_copies) != len(selected_paths):
        raise ManualAcceptanceError(
            "Extraction worker audit does not match run-owned source copies"
        )
    expected = {
        f"S{index:04d}": copy_path
        for index, copy_path in enumerate(owned_copies, start=1)
    }
    completed_by_key: dict[tuple[str, int], dict[str, object]] = {}
    for event in events:
        if (
            not isinstance(event, dict)
            or set(event)
            != {
                "role",
                "run_id",
                "source_id",
                "reader_attempt",
                "open_targets",
                "open_target_identities",
            }
            or event["role"] != "extraction"
            or event["run_id"] != context.run_id
            or event["source_id"] not in expected
            or type(event["reader_attempt"]) is not int
            or event["reader_attempt"] not in {1, 2}
            or not isinstance(event["open_targets"], list)
            or len(event["open_targets"]) != 1
            or not isinstance(event["open_targets"][0], str)
            or not Path(event["open_targets"][0]).is_absolute()
            or not isinstance(
                event["open_target_identities"],
                list,
            )
            or len(event["open_target_identities"]) != 1
            or not _valid_target_identity(
                event["open_target_identities"][0],
                event["open_targets"][0],
            )
        ):
            raise ManualAcceptanceError(
                "Extraction worker audit is not bound to this run and source copy"
            )
        key = (event["source_id"], event["reader_attempt"])
        if key in completed_by_key:
            raise ManualAcceptanceError(
                "Extraction worker reader-attempt audit is missing or duplicated"
            )
        completed_by_key[key] = event
    if {source_id for source_id, _attempt in completed_by_key} != set(expected):
        raise ManualAcceptanceError(
            "Extraction worker reader-attempt audit is missing or duplicated"
        )
    if not extraction_attempts:
        raise ManualAcceptanceError("Extraction attempt audit is missing")

    attempts_by_source: dict[
        str,
        dict[int, dict[str, object]],
    ] = {}
    for attempt in extraction_attempts:
        if (
            not isinstance(attempt, dict)
            or set(attempt)
            != {
                "run_id",
                "source_id",
                "reader_attempt",
                "copy_path",
                "copy_identity",
            }
            or attempt["run_id"] != context.run_id
            or attempt["source_id"] not in expected
            or type(attempt["reader_attempt"]) is not int
            or attempt["reader_attempt"] not in {1, 2}
            or not isinstance(attempt["copy_path"], str)
            or not Path(attempt["copy_path"]).is_absolute()
            or not _valid_target_identity(
                attempt["copy_identity"],
                attempt["copy_path"],
            )
        ):
            raise ManualAcceptanceError(
                "Extraction target-attempt audit is invalid"
            )
        source_attempts = attempts_by_source.setdefault(
            attempt["source_id"],
            {},
        )
        if attempt["reader_attempt"] in source_attempts:
            raise ManualAcceptanceError(
                "Extraction target-attempt audit is duplicated"
            )
        source_attempts[attempt["reader_attempt"]] = attempt
    if set(attempts_by_source) != set(expected):
        raise ManualAcceptanceError(
            "Extraction target-attempt audit does not cover every selected source"
        )

    cleanup_by_key: dict[tuple[str, int], dict[str, object]] = {}
    for cleanup in extraction_retry_cleanups:
        if (
            not isinstance(cleanup, dict)
            or set(cleanup)
            != {
                "run_id",
                "source_id",
                "reader_attempt",
                "failed_copy_path",
                "replacement_copy_path",
                "replacement_copy_identity",
                "completed",
            }
            or cleanup["run_id"] != context.run_id
            or cleanup["source_id"] not in expected
            or type(cleanup["reader_attempt"]) is not int
            or cleanup["reader_attempt"] not in {1, 2}
            or type(cleanup["completed"]) is not bool
            or not cleanup["completed"]
            or not isinstance(cleanup["failed_copy_path"], str)
            or not Path(cleanup["failed_copy_path"]).is_absolute()
            or not isinstance(cleanup["replacement_copy_path"], str)
            or not Path(cleanup["replacement_copy_path"]).is_absolute()
            or not _valid_target_identity(
                cleanup["replacement_copy_identity"],
                cleanup["replacement_copy_path"],
            )
        ):
            raise ManualAcceptanceError(
                "Extraction retry-cleanup audit is invalid"
            )
        key = (cleanup["source_id"], cleanup["reader_attempt"])
        if key in cleanup_by_key:
            raise ManualAcceptanceError(
                "Extraction retry-cleanup audit is duplicated"
            )
        cleanup_by_key[key] = cleanup

    for source_id, source_attempts in attempts_by_source.items():
        attempt_numbers = sorted(source_attempts)
        if attempt_numbers != list(range(1, len(attempt_numbers) + 1)):
            raise ManualAcceptanceError(
                "Extraction target-attempt audit is not contiguous"
            )
        if source_attempts[1]["copy_path"] != str(
            expected[source_id].resolve()
        ):
            raise ManualAcceptanceError(
                "Extraction first attempt is not bound to the approved source copy"
            )
        for attempt_number in attempt_numbers[:-1]:
            cleanup = cleanup_by_key.get((source_id, attempt_number))
            current = source_attempts[attempt_number]
            following = source_attempts[attempt_number + 1]
            if cleanup is None:
                raise ManualAcceptanceError(
                    "Extraction retry cleanup evidence is missing"
                )
            if (
                _path_key(Path(current["copy_path"]))
                == _path_key(Path(following["copy_path"]))
                or _same_file_identity(
                    following["copy_identity"],
                    (
                        current["copy_identity"]["device_id"],
                        current["copy_identity"]["file_id"],
                    ),
                )
            ):
                raise ManualAcceptanceError(
                    "Extraction retry must register a distinct source-copy generation"
                )
            if (
                _path_key(Path(cleanup["failed_copy_path"]))
                != _path_key(Path(current["copy_path"]))
                or _path_key(Path(cleanup["replacement_copy_path"]))
                != _path_key(Path(following["copy_path"]))
                or not _same_file_identity(
                    cleanup["replacement_copy_identity"],
                    (
                        following["copy_identity"]["device_id"],
                        following["copy_identity"]["file_id"],
                    ),
                )
            ):
                raise ManualAcceptanceError(
                    "Extraction retry cleanup does not bind consecutive attempts"
                )
        final_attempt = attempt_numbers[-1]
        if (source_id, final_attempt) in cleanup_by_key:
            raise ManualAcceptanceError(
                "Extraction successful final attempt must not have retry cleanup"
            )
        completed = completed_by_key.get((source_id, final_attempt))
        if completed is None:
            raise ManualAcceptanceError(
                "Extraction final attempt has no worker open-target evidence"
            )
        for (completed_source, completed_attempt), event in completed_by_key.items():
            if completed_source != source_id:
                continue
            recorded = source_attempts.get(completed_attempt)
            if recorded is None:
                raise ManualAcceptanceError(
                    "Extraction worker event has no parent target attempt"
                )
            if (
                event["open_targets"] != [recorded["copy_path"]]
                or not _same_file_identity(
                    event["open_target_identities"][0],
                    (
                        recorded["copy_identity"]["device_id"],
                        recorded["copy_identity"]["file_id"],
                    ),
                )
            ):
                raise ManualAcceptanceError(
                    "Extraction worker event does not match its parent target attempt"
                )
    unknown_cleanup_keys = set(cleanup_by_key).difference(
        (source_id, attempt)
        for source_id, attempts in attempts_by_source.items()
        for attempt in attempts
    )
    if unknown_cleanup_keys:
        raise ManualAcceptanceError(
            "Extraction retry cleanup has no matching attempt"
        )
    return (
        [attempt["copy_path"] for attempt in extraction_attempts],
        [
            (
                attempt["copy_identity"]["device_id"],
                attempt["copy_identity"]["file_id"],
            )
            for attempt in extraction_attempts
        ],
    )


def _reconcile_worker_attempts(
    attempts: tuple[dict[str, object], ...],
    cleanups: tuple[dict[str, object], ...],
    *,
    completed_events: tuple[dict[str, object], ...],
    all_targets: list[str],
    target_identities: list[tuple[int, int]],
) -> None:
    groups: dict[tuple[str, str], list[dict[str, object]]] = {}
    attempt_by_key: dict[tuple[str, str, int], dict[str, object]] = {}
    for attempt in attempts:
        if (
            not isinstance(attempt, dict)
            or set(attempt)
            != {
                "role",
                "attempt",
                "approved_snapshot_id",
                "run_staging_root",
                "target_states",
            }
            or attempt["role"] not in {"output", "verifier"}
            or type(attempt["attempt"]) is not int
            or attempt["attempt"] not in {1, 2}
            or not isinstance(attempt["approved_snapshot_id"], str)
            or not isinstance(attempt["run_staging_root"], str)
            or not Path(attempt["run_staging_root"]).is_absolute()
            or not isinstance(attempt["target_states"], list)
            or not attempt["target_states"]
        ):
            raise ManualAcceptanceError(
                "Worker target-attempt audit is invalid"
            )
        expected_target_count = 1 if attempt["role"] == "output" else 2
        if len(attempt["target_states"]) != expected_target_count:
            raise ManualAcceptanceError(
                "Worker target-attempt audit has an invalid role target set"
            )
        target_paths = {
            _path_key(Path(state["path"]))
            for state in attempt["target_states"]
            if isinstance(state, dict) and isinstance(state.get("path"), str)
        }
        if len(target_paths) != expected_target_count:
            raise ManualAcceptanceError(
                "Worker target-attempt audit has an invalid role target set"
            )
        for state in attempt["target_states"]:
            if (
                not isinstance(state, dict)
                or set(state) != {"path", "existed_before", "identity"}
                or not isinstance(state["path"], str)
                or not Path(state["path"]).is_absolute()
                or type(state["existed_before"]) is not bool
            ):
                raise ManualAcceptanceError(
                    "Worker target-attempt audit is invalid"
                )
            if _path_key(Path(state["path"]).parent) != _path_key(
                Path(attempt["run_staging_root"])
            ):
                raise ManualAcceptanceError(
                    "Worker target-attempt path is outside its staging root"
                )
            identity = state["identity"]
            if state["existed_before"]:
                if not _valid_target_identity(identity, state["path"]):
                    raise ManualAcceptanceError(
                        "Worker target-attempt identity audit is invalid"
                    )
                target_identities.append(
                    (identity["device_id"], identity["file_id"])
                )
            elif identity is not None:
                raise ManualAcceptanceError(
                    "Worker target-attempt identity audit is invalid"
                )
            all_targets.append(state["path"])
        key = (attempt["role"], attempt["run_staging_root"])
        groups.setdefault(key, []).append(attempt)
        attempt_key = (*key, attempt["attempt"])
        if attempt_key in attempt_by_key:
            raise ManualAcceptanceError(
                "Worker target-attempt audit is duplicated"
            )
        attempt_by_key[attempt_key] = attempt

    cleanup_by_key: dict[tuple[str, str, int], dict[str, object]] = {}
    for cleanup in cleanups:
        if (
            not isinstance(cleanup, dict)
            or set(cleanup)
            != {
                "role",
                "attempt",
                "approved_snapshot_id",
                "run_staging_root",
                "artifact_path",
                "artifact_identity",
                "completed",
                "error",
            }
            or cleanup["role"] not in {"output", "verifier"}
            or type(cleanup["attempt"]) is not int
            or cleanup["attempt"] not in {1, 2}
            or not isinstance(cleanup["approved_snapshot_id"], str)
            or not isinstance(cleanup["run_staging_root"], str)
            or not Path(cleanup["run_staging_root"]).is_absolute()
            or not isinstance(cleanup["artifact_path"], str)
            or not Path(cleanup["artifact_path"]).is_absolute()
            or _path_key(Path(cleanup["artifact_path"]).parent)
            != _path_key(Path(cleanup["run_staging_root"]))
            or type(cleanup["completed"]) is not bool
            or (
                cleanup["completed"]
                and not _valid_target_identity(
                    cleanup["artifact_identity"],
                    cleanup["artifact_path"],
                )
            )
            or (
                not cleanup["completed"]
                and cleanup["artifact_identity"] is not None
                and not _valid_target_identity(
                    cleanup["artifact_identity"],
                    cleanup["artifact_path"],
                )
            )
            or (
                cleanup["completed"]
                and cleanup["error"] is not None
            )
            or (
                not cleanup["completed"]
                and not isinstance(cleanup["error"], str)
            )
        ):
            raise ManualAcceptanceError(
                "Worker retry-cleanup audit is invalid"
            )
        key = (
            cleanup["role"],
            cleanup["run_staging_root"],
            cleanup["attempt"],
        )
        if key in cleanup_by_key:
            raise ManualAcceptanceError(
                "Worker retry-cleanup audit is duplicated"
            )
        matching_attempt = attempt_by_key.get(key)
        if matching_attempt is None:
            raise ManualAcceptanceError(
                "Worker retry-cleanup audit has no existing attempt"
            )
        if (
            cleanup["approved_snapshot_id"]
            != matching_attempt["approved_snapshot_id"]
        ):
            raise ManualAcceptanceError(
                "Worker retry-cleanup snapshot does not match its attempt"
            )
        matching_state = next(
            (
                state
                for state in matching_attempt["target_states"]
                if _path_key(Path(state["path"]))
                == _path_key(Path(cleanup["artifact_path"]))
            ),
            None,
        )
        if cleanup["completed"] and (
            matching_state is None
            or not matching_state["existed_before"]
            or not _same_file_identity(
                cleanup["artifact_identity"],
                (
                    matching_state["identity"]["device_id"],
                    matching_state["identity"]["file_id"],
                ),
            )
        ):
            raise ManualAcceptanceError(
                "Worker retry cleanup does not retire its exact attempt generation"
            )
        cleanup_by_key[key] = cleanup

    completed_groups = {
        (event["role"], event["run_staging_root"])
        for event in completed_events
        if event.get("role") in {"output", "verifier"}
    }
    if not completed_groups.issubset(groups):
        raise ManualAcceptanceError(
            "Successful worker target-attempt audit is missing"
        )
    completed_by_group = {
        (event["role"], event["run_staging_root"]): event
        for event in completed_events
        if event.get("role") in {"output", "verifier"}
    }
    for (role, root), group in groups.items():
        ordered = sorted(group, key=lambda item: item["attempt"])
        numbers = [item["attempt"] for item in ordered]
        if numbers != list(range(1, len(numbers) + 1)):
            raise ManualAcceptanceError(
                "Worker target-attempt sequence is invalid"
            )
        snapshot_ids = {
            item["approved_snapshot_id"] for item in ordered
        }
        if len(snapshot_ids) != 1:
            raise ManualAcceptanceError(
                "Worker target-attempt snapshot changed across retry"
            )
        snapshot_id = ordered[0]["approved_snapshot_id"]
        target_sets = {
            frozenset(
                _path_key(Path(state["path"]))
                for state in item["target_states"]
            )
            for item in ordered
        }
        if len(target_sets) != 1:
            raise ManualAcceptanceError(
                "Worker target-attempt target set changed across retry"
            )
        completed = tuple(
            event
            for event in completed_events
            if event.get("role") == role
            and event.get("run_staging_root") == root
        )
        for event in completed:
            if event.get("approved_snapshot_id") != snapshot_id:
                raise ManualAcceptanceError(
                    "Completed worker snapshot does not match its attempt"
                )
            completed_targets = {
                _path_key(Path(path)) for path in event["open_targets"]
            }
            if completed_targets != next(iter(target_sets)):
                raise ManualAcceptanceError(
                    "Completed worker target set does not match its attempt"
                )
        if completed and (role, root, numbers[-1]) in cleanup_by_key:
            raise ManualAcceptanceError(
                "Worker retry cleanup is attached to a successful final attempt"
            )
        for attempt_number in numbers[:-1]:
            cleanup = cleanup_by_key.get(
                (role, root, attempt_number)
            )
            if cleanup is None or not cleanup["completed"]:
                raise ManualAcceptanceError(
                    "Failed worker retry has no successful cleanup evidence"
                )
            following = attempt_by_key[(role, root, attempt_number + 1)]
            following_state = next(
                (
                    state
                    for state in following["target_states"]
                    if _path_key(Path(state["path"]))
                    == _path_key(Path(cleanup["artifact_path"]))
                ),
                None,
            )
            retired_identity = cleanup["artifact_identity"]
            if (
                following_state is None
                or not following_state["existed_before"]
                or not _valid_target_identity(
                    following_state["identity"],
                    following_state["path"],
                )
                or _same_file_identity(
                    following_state["identity"],
                    (
                        retired_identity["device_id"],
                        retired_identity["file_id"],
                    ),
                )
            ):
                raise ManualAcceptanceError(
                    "Worker retry must register a distinct artifact generation"
                )
    if any(not cleanup["completed"] for cleanup in cleanups):
        raise ManualAcceptanceError(
            "Worker retry cleanup did not complete"
        )
    for root in {
        group_root
        for role, group_root in completed_by_group
        if role == "verifier"
        and ("output", group_root) in completed_by_group
    }:
        output_event = completed_by_group[("output", root)]
        verifier_event = completed_by_group[("verifier", root)]
        project_path = output_event["open_targets"][0]
        output_identity = output_event["open_target_identities"][0]
        verifier_attempts = groups[("verifier", root)]
        verifier_project_identities = [
            state["identity"]
            for attempt in verifier_attempts
            for state in attempt["target_states"]
            if _path_key(Path(state["path"]))
            == _path_key(Path(project_path))
        ]
        verifier_identity_by_path = {
            _path_key(Path(path)): identity
            for path, identity in zip(
                verifier_event["open_targets"],
                verifier_event["open_target_identities"],
                strict=True,
            )
        }
        project_identity = verifier_identity_by_path.get(
            _path_key(Path(project_path))
        )
        mutation_identities = [
            identity
            for path, identity in verifier_identity_by_path.items()
            if path != _path_key(Path(project_path))
        ]
        expected_identity = (
            output_identity["device_id"],
            output_identity["file_id"],
        )
        if (
            not verifier_project_identities
            or any(
                not _same_file_identity(identity, expected_identity)
                for identity in verifier_project_identities
            )
            or not _same_file_identity(project_identity, expected_identity)
            or any(
                _same_file_identity(identity, expected_identity)
                for identity in mutation_identities
            )
        ):
            raise ManualAcceptanceError(
                "Worker project identity continuity is invalid"
            )


def _require_cancellation_worker_attempts(
    *,
    worker_evidence: dict[str, object],
    attempt_summary: dict[str, object],
) -> None:
    _bind_worker_attempts_to_output_stage(
        worker_evidence=worker_evidence,
        attempt_summary=attempt_summary,
    )
    stages = set(attempt_summary["entered_stages"])
    required_roles = {
        role
        for stage, role in (
            ("write_output", "output"),
            ("verify_output", "verifier"),
        )
        if stage in stages
    }
    observed_roles = {
        attempt.get("role")
        for attempt in worker_evidence["attempts"]
        if isinstance(attempt, dict)
    }
    if not required_roles.issubset(observed_roles):
        raise ManualAcceptanceError(
            "Cancellation entered output/verifier execution without the "
            "corresponding worker target-attempt audit"
        )


def _bind_worker_attempts_to_output_stage(
    *,
    worker_evidence: dict[str, object],
    attempt_summary: dict[str, object],
) -> None:
    expected_snapshot_id = attempt_summary["approved_snapshot_id"]
    staging_by_root = {
        _path_key(Path(item["staging_dir"])): item
        for item in attempt_summary["staging_targets"]
    }
    for attempt in worker_evidence["attempts"]:
        root_key = _path_key(Path(attempt["run_staging_root"]))
        staging = staging_by_root.get(root_key)
        if (
            attempt["approved_snapshot_id"] != expected_snapshot_id
            or staging is None
        ):
            raise ManualAcceptanceError(
                "Worker target-attempt does not match its output-stage attempt"
            )
        required_stage = (
            "write_output"
            if attempt["role"] == "output"
            else "verify_output"
        )
        if required_stage not in staging["entered_stages"]:
            raise ManualAcceptanceError(
                "Worker target-attempt does not match its output-stage run"
            )
        expected_targets = {
            _path_key(Path(staging["staging_project_path"]))
        }
        if attempt["role"] == "verifier":
            expected_targets.add(
                _path_key(Path(staging["verifier_mutation_path"]))
            )
        states_by_path = {
            _path_key(Path(state["path"])): state
            for state in attempt["target_states"]
        }
        if set(states_by_path) != expected_targets:
            raise ManualAcceptanceError(
                "Worker target-attempt does not match its staging target"
            )
        project_state = states_by_path[
            _path_key(Path(staging["staging_project_path"]))
        ]
        if attempt["role"] == "output":
            valid_state = _valid_reserved_target_state(project_state)
        else:
            mutation_state = states_by_path[
                _path_key(Path(staging["verifier_mutation_path"]))
            ]
            valid_state = (
                _valid_reserved_target_state(project_state)
                and _valid_reserved_target_state(mutation_state)
            )
        if not valid_state:
            raise ManualAcceptanceError(
                "Worker target-attempt prelaunch state is invalid"
            )


def _valid_reserved_target_state(state: dict[str, object]) -> bool:
    return bool(state["existed_before"]) and _valid_target_identity(
        state["identity"],
        state["path"],
    )


def _valid_target_identity(identity: object, target: str) -> bool:
    return (
        isinstance(identity, dict)
        and set(identity) == {"path", "device_id", "file_id"}
        and isinstance(identity["path"], str)
        and type(identity["device_id"]) is int
        and type(identity["file_id"]) is int
        and identity["file_id"] != 0
        and _path_key(Path(identity["path"])) == _path_key(Path(target))
    )


def _same_file_identity(
    identity: object,
    expected: tuple[int, int],
) -> bool:
    return (
        isinstance(identity, dict)
        and (identity.get("device_id"), identity.get("file_id")) == expected
    )


def _reconcile_output_and_report(
    events: tuple[dict[str, object], ...],
    worker_evidence: dict[str, object],
    *,
    selected_paths: tuple[Path, ...],
    fingerprints_before: tuple[SourceSnapshot, ...],
    fingerprints_after: tuple[SourceSnapshot, ...],
    attempt_summary: dict[str, object],
) -> dict[str, object]:
    events_by_role = {
        event["role"]: event for event in worker_evidence["events"]
        if event["role"] in {"output", "verifier"}
    }
    output_event = events_by_role["output"]
    verifier_event = events_by_role["verifier"]
    staging_project = Path(output_event["open_targets"][0])
    staging_root = Path(output_event["run_staging_root"])
    if staging_project.parent != staging_root:
        raise ManualAcceptanceError(
            "Output worker target is outside its recorded staging root"
        )
    match = re.fullmatch(r"Organized_Spectra_(.+)\.opju", staging_project.name)
    if match is None:
        raise ManualAcceptanceError("Output worker staging project name is invalid")
    expected_final_dir = (
        staging_project.parent.parent
        / f"Organized_Origin_Data_{match.group(1)}"
    )
    publication = _reconcile_publication_event(
        events,
        approved_snapshot_id=output_event["approved_snapshot_id"],
        run_staging_root=staging_root,
        expected_project_identity=(
            output_event["open_target_identities"][0]["device_id"],
            output_event["open_target_identities"][0]["file_id"],
        ),
    )
    final_dir = Path(publication["final_run_dir"])
    project_path = Path(publication["final_project_path"])
    report_path = Path(publication["final_report_path"])
    if (
        final_dir != expected_final_dir
        or project_path != final_dir / staging_project.name
        or report_path
        != final_dir / f"Run_Report_{match.group(1)}.txt"
        or _path_key(Path(publication["output_parent"]))
        != _path_key(Path(attempt_summary["effective_output_parent"]))
    ):
        raise ManualAcceptanceError(
            "Publication audit does not match the approved staging/output-parent evidence"
        )
    if not project_path.is_file() or not report_path.is_file():
        raise ManualAcceptanceError(
            "The exact published project/report pair cannot be found"
        )
    if len(tuple(final_dir.glob("*.opj*"))) != 1 or len(tuple(final_dir.glob("Run_Report_*.txt"))) != 1:
        raise ManualAcceptanceError(
            "The published run directory does not contain exactly one project/report pair"
        )
    report_text = report_path.read_text(encoding="utf-8")
    sections = _report_sections(report_text)
    report_inputs = tuple(Path(entry) for entry in sections.get("输入路径", ()))
    if {_path_key(path) for path in report_inputs} != {
        _path_key(path) for path in selected_paths
    }:
        raise ManualAcceptanceError(
            "Run report input paths do not match the actual UI selection"
        )
    if sections.get("输出路径") != (str(final_dir),):
        raise ManualAcceptanceError(
            "Run report output path does not match the published run directory"
        )
    expected_fingerprints = {
        str(before.path): (
            f"提交前 SHA-256={before.sha256}；"
            f"输出后 SHA-256={after.sha256}；"
            f"大小={after.size_bytes}；"
            f"UTC mtime_ns={after.mtime_ns}；未改变"
        )
        for before, after in zip(
            fingerprints_before,
            fingerprints_after,
            strict=True,
        )
    }
    if _report_items(sections.get("源文件指纹", ())) != expected_fingerprints:
        raise ManualAcceptanceError(
            "Run report source fingerprints do not match independent before/after evidence"
        )
    return {
        "approved_snapshot_id": output_event["approved_snapshot_id"],
        "staging_project_path": str(staging_project),
        "verifier_open_targets": list(verifier_event["open_targets"]),
        "project_path": str(project_path),
        "report_path": str(report_path),
        "project_report_pair_found": True,
        "independent_verifier_passed": True,
        "publication": publication,
        "output_parent_reconciliation": attempt_summary,
        "report_text": report_text,
    }


def _reconcile_output_attempts(
    events: tuple[dict[str, object], ...],
    *,
    context,
    approved_snapshot_id: str | None = None,
    require_output_phase: bool = False,
) -> dict[str, object]:
    attempts = tuple(
        event["payload"]
        for event in events
        if event["event_type"] == "output_stage_attempt"
    )
    if not attempts:
        raise ManualAcceptanceError("Output-stage attempt audit is missing")
    expected_temp_root = _path_key(Path(context.temp_root))
    run_ids = []
    for attempt in attempts:
        required = {
            "approved_snapshot_id",
            "run_id",
            "output_parent",
            "output_parent_existed_before",
            "output_parent_entries_before",
            "task_temp_root",
        }
        if (
            not isinstance(attempt, dict)
            or set(attempt) != required
            or not isinstance(attempt["approved_snapshot_id"], str)
            or not isinstance(attempt["run_id"], str)
            or not isinstance(attempt["output_parent"], str)
            or type(attempt["output_parent_existed_before"]) is not bool
            or not isinstance(attempt["output_parent_entries_before"], list)
            or any(
                not isinstance(name, str)
                for name in attempt["output_parent_entries_before"]
            )
            or _path_key(Path(attempt["task_temp_root"]))
            != expected_temp_root
            or (
                approved_snapshot_id is not None
                and attempt["approved_snapshot_id"] != approved_snapshot_id
            )
        ):
            raise ManualAcceptanceError("Output-stage attempt audit is invalid")
        run_ids.append(attempt["run_id"])
    if len(run_ids) != len(set(run_ids)):
        raise ManualAcceptanceError("Output-stage attempt run IDs are duplicated")
    snapshot_ids = {
        attempt["approved_snapshot_id"]
        for attempt in attempts
    }
    if len(snapshot_ids) != 1:
        raise ManualAcceptanceError(
            "Output-stage attempts do not preserve one approved snapshot"
        )
    if _path_key(Path(attempts[0]["output_parent"])) != _path_key(
        Path(context.output_parent)
    ):
        raise ManualAcceptanceError(
            "Initial output parent does not match the UI-selected parent"
        )
    progress_events = tuple(
        event["payload"]
        for event in events
        if event["event_type"] == "output_stage_progress"
    )
    if any(
        not isinstance(event, dict)
        or set(event) != {"approved_snapshot_id", "run_id", "stage"}
        or event.get("run_id") not in run_ids
        or event.get("approved_snapshot_id")
        not in snapshot_ids
        or event.get("stage")
        not in {
            "process_gate",
            "create_staging",
            "write_output",
            "verify_output",
            "verify_sources",
            "build_report",
            "publish",
        }
        for event in progress_events
    ):
        raise ManualAcceptanceError("Output-stage progress audit is invalid")
    if require_output_phase and not any(
        event["stage"] in {"write_output", "verify_output"}
        for event in progress_events
    ):
        raise ManualAcceptanceError(
            "Cancellation did not enter output or verifier execution"
        )
    staging_events = tuple(
        event["payload"]
        for event in events
        if event["event_type"] == "output_staging_created"
    )
    if any(
        not isinstance(event, dict)
        or set(event)
        != {
            "approved_snapshot_id",
            "run_id",
            "output_parent",
            "staging_dir",
            "staging_project_path",
            "verifier_mutation_path",
        }
        or event.get("run_id") not in run_ids
        or event.get("approved_snapshot_id")
        not in snapshot_ids
        or not isinstance(event.get("output_parent"), str)
        or not isinstance(event.get("staging_dir"), str)
        or not isinstance(event.get("staging_project_path"), str)
        or not isinstance(event.get("verifier_mutation_path"), str)
        for event in staging_events
    ):
        raise ManualAcceptanceError("Output staging audit is invalid")
    attempts_by_run = {attempt["run_id"]: attempt for attempt in attempts}
    staging_by_run = {
        event["run_id"]: event for event in staging_events
    }
    if len(staging_by_run) != len(staging_events):
        raise ManualAcceptanceError("Output staging audit is duplicated")
    progress_by_run = {
        run_id: [
            event["stage"]
            for event in progress_events
            if event["run_id"] == run_id
        ]
        for run_id in run_ids
    }
    for run_id, event in staging_by_run.items():
        attempt = attempts_by_run[run_id]
        staging_dir = Path(event["staging_dir"])
        staging_project = Path(event["staging_project_path"])
        verifier_mutation = Path(event["verifier_mutation_path"])
        if (
            _path_key(Path(event["output_parent"]))
            != _path_key(Path(attempt["output_parent"]))
            or _path_key(staging_dir.parent)
            != _path_key(Path(event["output_parent"]))
            or _path_key(staging_project.parent)
            != _path_key(staging_dir)
            or _path_key(verifier_mutation.parent)
            != _path_key(staging_dir)
            or _path_key(staging_project) == _path_key(verifier_mutation)
        ):
            raise ManualAcceptanceError(
                "Output staging audit is not bound to its output-stage attempt"
            )
    if require_output_phase and not staging_events:
        raise ManualAcceptanceError(
            "Cancellation output-stage audit has no created staging directory"
        )
    return {
        "approved_snapshot_id": attempts[-1]["approved_snapshot_id"],
        "attempt_run_ids": run_ids,
        "initial_output_parent": attempts[0]["output_parent"],
        "effective_output_parent": attempts[-1]["output_parent"],
        "rerouted": _path_key(Path(attempts[0]["output_parent"]))
        != _path_key(Path(attempts[-1]["output_parent"])),
        "approved_snapshot_preserved": True,
        "output_parent_existed_before": attempts[0][
            "output_parent_existed_before"
        ],
        "output_parent_entries_before": list(
            attempts[0]["output_parent_entries_before"]
        ),
        "staging_paths": [
            event["staging_dir"] for event in staging_events
        ],
        "staging_targets": [
            {
                **event,
                "entered_stages": progress_by_run[event["run_id"]],
            }
            for event in staging_events
        ],
        "entered_stages": [event["stage"] for event in progress_events],
    }


def _reconcile_publication_event(
    events: tuple[dict[str, object], ...],
    *,
    approved_snapshot_id: str,
    run_staging_root: Path,
    expected_project_identity: tuple[int, int],
) -> dict[str, object]:
    payload = _single_event(events, "publication_committed")["payload"]
    required = {
        "approved_snapshot_id",
        "run_id",
        "output_parent",
        "final_run_dir",
        "final_project_path",
        "final_report_path",
        "artifacts",
    }
    if (
        set(payload) != required
        or payload["approved_snapshot_id"] != approved_snapshot_id
        or not all(
            isinstance(payload[field], str)
            for field in required - {"artifacts"}
        )
    ):
        raise ManualAcceptanceError("Publication commit audit is invalid")
    staging_matches = tuple(
        event["payload"]
        for event in events
        if event["event_type"] == "output_staging_created"
        and event["payload"].get("run_id") == payload["run_id"]
    )
    if (
        len(staging_matches) != 1
        or _path_key(Path(staging_matches[0]["staging_dir"]))
        != _path_key(Path(run_staging_root))
    ):
        raise ManualAcceptanceError(
            "Publication commit audit is not bound to the worker staging run"
        )
    project_path = Path(payload["final_project_path"])
    report_path = Path(payload["final_report_path"])
    observed = tuple(snapshot_sources([project_path, report_path], []))
    expected = payload["artifacts"]
    if (
        not isinstance(expected, list)
        or expected
        != [_snapshot_to_evidence(snapshot) for snapshot in observed]
        or project_path.parent != Path(payload["final_run_dir"])
        or report_path.parent != Path(payload["final_run_dir"])
        or Path(payload["final_run_dir"]).parent
        != Path(payload["output_parent"])
    ):
        raise ManualAcceptanceError(
            "Publication commit artifact fingerprints are invalid"
        )
    if (
        (observed[0].device_id, observed[0].file_id)
        != expected_project_identity
    ):
        raise ManualAcceptanceError(
            "Published project does not preserve the staged project identity"
        )
    return dict(payload)


def _reconcile_approved_report_ledger(
    events: tuple[dict[str, object], ...],
    *,
    approved_snapshot_id: str,
    report_text: str,
) -> dict[str, object]:
    payload = _identical_event_payload(events, "approved_report_ledger")
    if (
        set(payload)
        != {
            "approved_snapshot_id",
            "recognized_source_paths",
            "sections",
        }
        or payload["approved_snapshot_id"] != approved_snapshot_id
        or not isinstance(payload["recognized_source_paths"], list)
        or any(
            not isinstance(path, str) or not Path(path).is_absolute()
            for path in payload["recognized_source_paths"]
        )
        or not isinstance(payload["sections"], dict)
        or set(payload["sections"])
        != set(APPROVED_OUTPUT_LEDGER_SECTION_TITLES)
        or any(
            not isinstance(entries, list)
            or any(not isinstance(entry, str) for entry in entries)
            for entries in payload["sections"].values()
        )
    ):
        raise ManualAcceptanceError(
            "Approved snapshot ledger runtime audit is invalid"
        )
    report_sections = _report_sections(report_text)
    observed = {
        title: list(report_sections.get(title, ()))
        for title in APPROVED_OUTPUT_LEDGER_SECTION_TITLES
    }
    if observed != payload["sections"]:
        raise ManualAcceptanceError(
            "Run report does not match the approved snapshot ledger"
        )
    return {
        "approved_snapshot_id": approved_snapshot_id,
        "recognized_source_paths": list(
            payload["recognized_source_paths"]
        ),
        "sections": dict(payload["sections"]),
        "report_cross_check_passed": True,
    }


def _reconcile_owned_output_cleanup(
    *,
    context,
    attempt_summary: dict[str, object],
    phase: str,
) -> dict[str, object]:
    staging_paths = tuple(
        Path(path) for path in attempt_summary["staging_paths"]
    )
    ownership_sidecars = tuple(
        path.with_name(f"{path.name}.ownership.json")
        for path in staging_paths
    )
    retained = [
        path
        for path in (
            Path(context.temp_root),
            *staging_paths,
            *ownership_sidecars,
        )
        if lexical_path_exists(path)
    ]
    if retained:
        raise ManualAcceptanceError(
            f"{phase} left a task temp root, staging path, or ownership sidecar: "
            + "; ".join(str(path) for path in retained)
        )
    return {
        "task_temp_root_absent": True,
        "staging_paths_absent": True,
        "staging_ownership_sidecars_absent": True,
    }


def _validated_freshness_attestation(
    *,
    selected_path: Path,
    selected_paths: tuple[Path, ...],
    selected_snapshots: tuple[SourceSnapshot, ...],
    recognized_source_paths: tuple[Path, ...],
    evidence_root: Path,
    current_evidence_dir: Path | None = None,
) -> dict[str, object]:
    selected_by_key = {
        _path_key(path): path for path in selected_paths
    }
    selected_key = _path_key(Path(selected_path))
    if selected_key not in selected_by_key:
        raise ManualAcceptanceError(
            "Freshness attestation must identify an actually selected source"
        )
    recognized_keys = {
        _path_key(path) for path in recognized_source_paths
    }
    if selected_key not in recognized_keys:
        raise ManualAcceptanceError(
            "Freshness attestation must identify a recognized valid raw source"
        )
    snapshot_by_key = {
        _path_key(Path(snapshot.path)): snapshot
        for snapshot in selected_snapshots
    }
    snapshot = snapshot_by_key.get(selected_key)
    if snapshot is None:
        raise ManualAcceptanceError(
            "Freshness attestation source has no selected fingerprint"
        )
    _require_fresh_external_snapshot(
        (snapshot,),
        Path(evidence_root),
        current_evidence_dir=current_evidence_dir,
    )
    attested_path = selected_by_key[selected_key]
    return {
        "source_path": str(attested_path),
        "sha256": snapshot.sha256,
        "statement": (
            "Operator attested that this recognized valid raw Origin project "
            "had not previously been used for manual acceptance."
        ),
        "recorded_at": datetime.now().astimezone().isoformat(),
    }


def _identical_event_payload(
    events: tuple[dict[str, object], ...],
    event_type: str,
) -> dict[str, object]:
    matches = tuple(
        event["payload"]
        for event in events
        if event["event_type"] == event_type
    )
    label = event_type.replace("_", "-")
    if not matches:
        raise ManualAcceptanceError(
            f"Expected at least one {label} runtime audit event"
        )
    if any(payload != matches[0] for payload in matches[1:]):
        raise ManualAcceptanceError(
            f"Repeated {label} runtime audit events disagree"
        )
    return matches[0]


_COUNT_LABELS = {
    "识别 Book": "recognizable_book_count",
    "拒绝 Book": "rejected_book_count",
    "排除 Book": "excluded_book_count",
    "接受普通谱": "accepted_ordinary_spectrum_count",
    "输出计划谱图": "output_plan_spectrum_count",
    "输出计划列": "output_plan_column_count",
    "验证回读谱图": "verifier_readback_spectrum_count",
    "验证回读列": "verifier_readback_column_count",
}
_APPROVED_COUNT_FIELDS = tuple(_COUNT_LABELS.values())[:6]


def _reconcile_counts(
    events: tuple[dict[str, object], ...],
    *,
    output_summary: dict[str, object],
    worker_evidence: dict[str, object],
) -> dict[str, object]:
    approved = dict(
        _identical_event_payload(
            events,
            "approved_count_reconciliation",
        )
    )
    if set(approved) != set(_APPROVED_COUNT_FIELDS) or any(
        type(value) is not int or value < 0 for value in approved.values()
    ):
        raise ManualAcceptanceError("Approved count reconciliation audit is invalid")
    worker_events = {
        event["role"]: event for event in worker_evidence["events"]
        if event["role"] in {"output", "verifier"}
    }
    output_event = worker_events["output"]
    verifier_event = worker_events["verifier"]
    independent = {
        **approved,
        "verifier_readback_spectrum_count": verifier_event.get("spectrum_count"),
        "verifier_readback_column_count": verifier_event.get("column_count"),
    }
    if (
        output_event.get("spectrum_count") != approved["output_plan_spectrum_count"]
        or output_event.get("column_count") != approved["output_plan_column_count"]
        or independent["verifier_readback_spectrum_count"]
        != approved["output_plan_spectrum_count"]
        or independent["verifier_readback_column_count"]
        != approved["output_plan_column_count"]
    ):
        raise ManualAcceptanceError(
            "Output/verifier worker counts do not match the approved output plan"
        )
    report_sections = _report_sections(output_summary.pop("report_text"))
    report_items = _report_items(report_sections.get("数量核对", ()))
    try:
        report_counts = {
            _COUNT_LABELS[label]: int(value)
            for label, value in report_items.items()
        }
    except (KeyError, ValueError) as exc:
        raise ManualAcceptanceError("Run report count reconciliation is invalid") from exc
    if set(report_items) != set(_COUNT_LABELS) or report_counts != independent:
        raise ManualAcceptanceError(
            "Run report counts do not match independent runtime evidence"
        )
    counts_closed = (
        independent["recognizable_book_count"]
        == independent["rejected_book_count"]
        + independent["excluded_book_count"]
        + independent["accepted_ordinary_spectrum_count"]
        and independent["output_plan_spectrum_count"]
        == independent["verifier_readback_spectrum_count"]
        and independent["output_plan_column_count"]
        == independent["verifier_readback_column_count"]
    )
    if not counts_closed:
        raise ManualAcceptanceError("Independent count reconciliation does not close")
    return {
        **independent,
        "counts_closed": True,
        "report_counts": report_counts,
        "report_cross_check_passed": True,
    }


def _report_sections(text: str) -> dict[str, tuple[str, ...]]:
    sections: dict[str, list[str]] = {}
    current = None
    for line in text.splitlines():
        if not line:
            current = None
        elif line.startswith("- "):
            if current is None:
                raise ManualAcceptanceError("Run report entry has no section")
            sections[current].append(line[2:])
        elif line[:1].isspace():
            if current is None or not sections[current]:
                raise ManualAcceptanceError(
                    "Run report continuation has no entry"
                )
            sections[current][-1] += "\n" + line
        else:
            if line in sections:
                raise ManualAcceptanceError(f"Run report section is duplicated: {line}")
            current = line
            sections[current] = []
    return {title: tuple(entries) for title, entries in sections.items()}


def _report_items(entries: tuple[str, ...]) -> dict[str, str]:
    items = {}
    for entry in entries:
        subject, separator, detail = entry.partition("：")
        if not separator or not subject or subject in items:
            raise ManualAcceptanceError("Run report item is invalid or duplicated")
        items[subject] = detail
    return items


def _snapshot_to_evidence(snapshot: SourceSnapshot) -> dict[str, object]:
    return {
        "path": str(snapshot.path),
        "canonical_path": str(snapshot.canonical_path),
        "sha256": snapshot.sha256,
        "size_bytes": snapshot.size_bytes,
        "mtime_ns": snapshot.mtime_ns,
        "device_id": snapshot.device_id,
        "file_id": snapshot.file_id,
    }


def _path_key(path: Path) -> str:
    return os.path.normcase(str(Path(path).resolve()))


def _prompt_freshness_attestation(
    selected_paths: tuple[Path, ...],
) -> Path:
    print("请选择一份此前未用于本验收、且位于工作区外的有效原始项目：")
    for index, path in enumerate(selected_paths, start=1):
        print(f"  {index}. {path}")
    response = input("输入编号并确认该陈述属实：").strip()
    try:
        selected_index = int(response) - 1
        return selected_paths[selected_index]
    except (ValueError, IndexError) as exc:
        raise ManualAcceptanceError("Freshness attestation selection is invalid") from exc


def _guided_acceptance_checklist(cycle: str = "success") -> str:
    if cycle == "cancellation":
        return """# Spectrum Organizer packaged cancellation acceptance

本次为独立发布前取消周期；请选择一个新建的空输出 Folder。

- [ ] 由用户在生产 UI 中选择原始文件和空输出 Folder；helper 不传入源文件、输出路径或 Book。
- [ ] 每个选中原件的前后 SHA-256、字节大小和 UTC mtime_ns 完全一致。
- [ ] `worker-open-targets.json` 的路径和物理文件身份均未命中原件。
- [ ] 界面已实际进入输出创建或独立验证，再测试 `继续运行` 保持活动任务。
- [ ] 随后确认 `取消并退出`，输出 Folder 前后均为空。
- [ ] `cancellation-cleanup-summary.json` 证明任务 temp root 和 staging 均不存在，且没有 publication commit。
- [ ] 关闭程序后隐藏 Origin/产品 worker 进程数均为零；人工打开的可见 Origin 检查窗口单独记录。

当前文件只建立并记录取消验收现场；上述证据未齐全前不得标记为通过。
"""
    return """# Spectrum Organizer packaged full-run manual acceptance

本次为成功发布周期。

- [ ] 由用户在生产 UI 中选择原始文件；helper 不传入任何源文件、输出路径或 Book。
- [ ] 至少一个有效外部原始项目此前未用于本验收，并记录 freshness attestation。
- [ ] 每个选中原件的前后 SHA-256、字节大小和 UTC mtime_ns 完全一致。
- [ ] `worker-open-targets.json` 的路径和物理文件身份均未命中原件。
- [ ] `output-verifier-summary.json` 证明成功后任务 temp root 和每个记录的 staging 均不存在。
- [ ] `output-verifier-summary.json` 绑定本次 publication commit、run/snapshot ID、最终文件指纹、初始/有效输出父目录和 reroute 状态。
- [ ] approved-snapshot ledger 与 `Run_Report_*.txt` 的设置、归属、审核、排除、Folder/Book 映射、完整性和缺少样品状态逐项一致。
- [ ] `count-reconciliation-summary.json` 与报告的识别、拒绝、排除、接受、输出和验证回读数量一致并闭合。
- [ ] `sample-library-write-summary.json` 证明隔离的 `sample_library.sqlite3` 及其 sidecar/backup 零写入。
- [ ] 人工打开新 `.opju`，检查 Folder/Book 顺序、全局唯一 Short Name、Long Name、列顺序/指定/注释、blank mask、Norm 公式、Method/F(x) 和无空 `Folder1`。
- [ ] 若执行输出父目录 reroute，同一 Approved Snapshot 和已完成审核选择保持不变。
- [ ] 成功后主窗口仍显示三个完成动作，Origin 不自动打开。
- [ ] 关闭程序后隐藏 Origin/产品 worker 进程数均为零；人工打开的可见 Origin 检查窗口单独记录。

当前文件只建立并记录成功周期验收现场；上述证据未齐全前不得标记为通过。
"""


def _write_text_exclusive(path: Path, text: str) -> None:
    with Path(path).open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(text)


def run_extraction_only(
    *,
    evidence_root: Path = DEFAULT_EVIDENCE_ROOT,
    source_selector: Callable[[], tuple[Path, ...]] | None = None,
    output_selector: Callable[[], Path | None] | None = None,
    dialog_port=None,
    context_builder: Callable[..., ApprovedPreExtractionRunContext] = prepare_approved_pre_extraction_context,
    extraction_runner=None,
    final_process_count_hook=None,
    origin_process_probe: Callable[..., Iterable[object]] | None = None,
    process_controller: object | None = None,
    settings_snapshot: dict[str, object] | None = None,
    timestamp_factory: Callable[[], str] | None = None,
) -> Path:
    settings = _validate_confirmed_settings(settings_snapshot)
    sources = tuple(Path(path) for path in (source_selector or _select_sources_with_qt)())
    if not sources:
        raise ManualAcceptanceError("No Origin source files were selected")
    _reject_non_origin_sources(sources)
    _require_external_source_path(sources)
    selected_output_parent = (output_selector or _select_output_parent_with_qt)()
    if selected_output_parent is None:
        raise ManualAcceptanceError("No output parent folder was selected")
    output_parent = Path(selected_output_parent)
    evidence_root = Path(evidence_root)
    evidence_root.mkdir(parents=True, exist_ok=True)
    authoritative_snapshots = tuple(snapshot_sources(list(sources), []))
    try:
        reservation = acquire_owned_directory_lock(
            evidence_root / ".manual-acceptance-history.lock",
            owner_filename="owner.token",
            label="Manual acceptance reservation",
        )
    except OwnedDirectoryLockError as exc:
        raise ManualAcceptanceError(str(exc)) from exc

    primary_error = None
    result = None
    try:
        _require_fresh_external_snapshot(authoritative_snapshots, evidence_root)
        result = _run_reserved_extraction(
            evidence_root=evidence_root,
            sources=sources,
            output_parent=output_parent,
            settings=settings,
            dialog_port=dialog_port,
            context_builder=context_builder,
            extraction_runner=extraction_runner,
            final_process_count_hook=final_process_count_hook,
            origin_process_probe=origin_process_probe,
            process_controller=process_controller,
            timestamp_factory=timestamp_factory,
            authoritative_snapshots=authoritative_snapshots,
        )
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        try:
            release_owned_directory_lock(reservation)
        except BaseException as exc:
            if primary_error is not None:
                primary_error.add_note(f"Manual acceptance reservation release failed: {exc}")
            elif result is None:
                raise ManualAcceptanceError(
                    f"Manual acceptance reservation release failed: {exc}"
                ) from exc
    return result


def _run_reserved_extraction(
    *,
    evidence_root: Path,
    sources: tuple[Path, ...],
    output_parent: Path,
    settings: dict[str, object],
    dialog_port,
    context_builder: Callable[..., ApprovedPreExtractionRunContext],
    extraction_runner,
    final_process_count_hook,
    origin_process_probe: Callable[..., Iterable[object]] | None,
    process_controller: object | None,
    timestamp_factory: Callable[[], str] | None,
    authoritative_snapshots: tuple[SourceSnapshot, ...],
) -> Path:
    created_evidence = _create_evidence_dir(evidence_root, timestamp_factory)
    evidence_dir = created_evidence.path
    contexts: list[ApprovedPreExtractionRunContext] = []
    try:
        return _run_reserved_extraction_in_evidence_dir(
            evidence_dir=evidence_dir,
            contexts=contexts,
            sources=sources,
            output_parent=output_parent,
            settings=settings,
            dialog_port=dialog_port,
            context_builder=context_builder,
            extraction_runner=extraction_runner,
            final_process_count_hook=final_process_count_hook,
            origin_process_probe=origin_process_probe,
            process_controller=process_controller,
            authoritative_snapshots=authoritative_snapshots,
        )
    except BaseException as exc:
        if contexts:
            try:
                _discard_context_temp_root(contexts[-1])
            except BaseException as cleanup_error:
                _add_secondary_error(exc, cleanup_error)
        try:
            if lexical_path_exists(evidence_dir):
                remove_empty_owned_directory(
                    evidence_dir,
                    created_evidence.identity,
                )
        except BaseException as cleanup_error:
            _add_secondary_error(exc, cleanup_error)
        raise


def _run_reserved_extraction_in_evidence_dir(
    *,
    evidence_dir: Path,
    contexts: list[ApprovedPreExtractionRunContext],
    sources: tuple[Path, ...],
    output_parent: Path,
    settings: dict[str, object],
    dialog_port,
    context_builder: Callable[..., ApprovedPreExtractionRunContext],
    extraction_runner,
    final_process_count_hook,
    origin_process_probe: Callable[..., Iterable[object]] | None,
    process_controller: object | None,
    authoritative_snapshots: tuple[SourceSnapshot, ...],
) -> Path:
    dialog = dialog_port or QtManualDialogPort()
    process_probe = origin_process_probe or default_origin_process_probe
    controller = process_controller or WindowsOriginProcessController(process_probe=process_probe)

    try:
        context = context_builder(
            selected_source_paths=sources,
            output_parent=output_parent,
            settings_snapshot=settings,
            local_appdata=evidence_dir / "Spectrum Organizer",
            protected_paths=(),
            dialog_port=dialog,
            origin_process_probe=process_probe,
            process_controller=controller,
        )
    except KeyboardInterrupt as exc:
        raise ManualAcceptanceError("谱图数据提取已取消") from exc
    contexts.append(context)
    if not _snapshots_match(authoritative_snapshots, context.source_fingerprints_before):
        _discard_context_temp_root(context)
        raise ManualAcceptanceError(
            "Source snapshots changed while the approved extraction context was prepared"
        )
    runner = extraction_runner or ExtractionSubprocessRunner(
        origin_process_probe=process_probe,
        origin_process_controller=controller,
    )
    process_count_hook = final_process_count_hook or FinalProcessCountHook(process_probe)
    phase_summary = None
    run_error = None
    try:
        phase_summary = _run_with_cancellation(runner, context)
    except BaseException as exc:
        run_error = exc

    safety_error = None
    try:
        verify_sources_unchanged(list(context.source_fingerprints_before))
    except BaseException as exc:
        safety_error = exc
    summary_error = None
    if isinstance(phase_summary, ExtractionPhaseSummary):
        try:
            _validated_worker_open_targets(context, phase_summary)
        except BaseException as exc:
            summary_error = exc
    elif run_error is None:
        summary_error = ManualAcceptanceError(
            "Production extraction runner returned an invalid summary"
        )
    process_probe_error = None
    remaining_origin_processes = None
    try:
        remaining_origin_processes = int(process_count_hook())
    except BaseException as exc:
        process_probe_error = exc
    residual_process_error = None
    if remaining_origin_processes not in (None, 0):
        residual_process_error = ManualAcceptanceError(
            f"Origin process count is {remaining_origin_processes} after extraction; evidence was not published"
        )
    if safety_error is not None:
        _add_secondary_error(safety_error, run_error)
        _add_secondary_error(safety_error, summary_error)
        _add_secondary_error(safety_error, residual_process_error)
        _add_secondary_error(safety_error, process_probe_error)
        raise safety_error
    if summary_error is not None:
        _add_secondary_error(summary_error, run_error)
        _add_secondary_error(summary_error, residual_process_error)
        _add_secondary_error(summary_error, process_probe_error)
        raise summary_error
    if residual_process_error is not None:
        _add_secondary_error(residual_process_error, run_error)
        _add_secondary_error(residual_process_error, process_probe_error)
        raise residual_process_error
    if run_error is not None:
        _add_secondary_error(run_error, process_probe_error)
        raise run_error
    if process_probe_error is not None:
        raise process_probe_error

    summary = _build_summary(evidence_dir, context, phase_summary)
    _write_json_exclusive(evidence_dir / "extraction-only-summary.json", summary)
    return evidence_dir


def _run_with_cancellation(
    runner,
    context: ApprovedPreExtractionRunContext,
) -> ExtractionPhaseSummary:
    try:
        return runner(context)
    except KeyboardInterrupt as exc:
        try:
            runner.cancel()
        except ExtractionCleanupBlockedError:
            raise
        except BaseException as cleanup_error:
            cancelled = ManualAcceptanceError("谱图数据提取已取消")
            _add_secondary_error(cancelled, cleanup_error)
            raise cancelled from exc
        cancelled = ManualAcceptanceError("谱图数据提取已取消")
        raise cancelled from exc


def _snapshots_match(
    authoritative: tuple[SourceSnapshot, ...],
    prepared: tuple[SourceSnapshot, ...],
) -> bool:
    return authoritative == prepared


def _discard_context_temp_root(context: ApprovedPreExtractionRunContext) -> None:
    temp_root = Path(context.temp_root)
    if not lexical_path_exists(temp_root):
        return
    try:
        cleanup_owned_temp_root(
            temp_root,
            expected_root_identity=context.temp_root_identity,
        )
    except BaseException as exc:
        raise ManualAcceptanceError(
            f"Could not discard the changed-source temporary context: {temp_root}"
        ) from exc


def _add_secondary_error(primary: BaseException, secondary: BaseException | None) -> None:
    if secondary is not None:
        primary.add_note(str(secondary))
        for note in getattr(secondary, "__notes__", ()):
            primary.add_note(str(note))


def _validated_worker_open_targets(
    context: ApprovedPreExtractionRunContext,
    phase_summary: ExtractionPhaseSummary,
) -> list[str]:
    open_targets = list(phase_summary.worker_open_targets)
    if not open_targets:
        raise ManualAcceptanceError("Production extraction summary has no worker-open targets")
    protected_paths = tuple(item.path for item in context.source_fingerprints_before)
    protected_hits = [
        target
        for target in open_targets
        if any(_paths_refer_to_same_file(Path(target), protected) for protected in protected_paths)
    ]
    if protected_hits:
        raise ManualAcceptanceError(
            f"Worker opened a selected original source; evidence was not published: {protected_hits[0]}"
        )
    return open_targets


def _build_summary(
    evidence_dir: Path,
    context: ApprovedPreExtractionRunContext,
    phase_summary: ExtractionPhaseSummary,
) -> dict[str, object]:
    source_summaries = [asdict(item) for item in phase_summary.source_summaries]
    open_targets = _validated_worker_open_targets(context, phase_summary)
    return {
        "phase": "extraction-only",
        "evidence_dir": str(evidence_dir),
        "selected_source_paths": [str(path) for path in context.selected_source_paths],
        "output_parent": str(context.output_parent),
        "settings_snapshot": dict(context.settings_snapshot),
        "run_owned_temp_root": str(context.temp_root),
        "worker_open_targets": open_targets,
        "source_fingerprints_before": [_snapshot_payload(snapshot) for snapshot in context.source_fingerprints_before],
        "source_summaries": source_summaries,
        "snapshot_path": str(phase_summary.snapshot_path),
        "snapshot_sha256": phase_summary.snapshot_sha256,
        "total_inventory_count": phase_summary.total_inventory_count,
        "total_result_count": phase_summary.total_result_count,
        "total_extracted_count": phase_summary.total_extracted_count,
        "total_rejected_count": phase_summary.total_rejected_count,
        "final_origin_process_count": 0,
        "protected_source_open_target_hits": [],
    }


def _paths_refer_to_same_file(left: Path, right: Path) -> bool:
    if os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right)):
        return True
    try:
        return os.path.samefile(left, right)
    except OSError:
        return False


def _snapshot_payload(snapshot: SourceSnapshot) -> dict[str, object]:
    payload = asdict(snapshot)
    payload["path"] = str(snapshot.path)
    payload["canonical_path"] = str(snapshot.canonical_path)
    return payload


def _validate_confirmed_settings(
    settings_snapshot: dict[str, object] | None,
) -> dict[str, object]:
    if settings_snapshot is None:
        raise ManualAcceptanceError("Confirmed preflight settings are required")
    if set(settings_snapshot) != {"s1Limit", "steadyEmissionY", "allowMissingS1"}:
        raise ManualAcceptanceError("Confirmed preflight settings are incomplete")
    s1_limit = settings_snapshot["s1Limit"]
    steady_emission_y = settings_snapshot["steadyEmissionY"]
    allow_missing_s1 = settings_snapshot["allowMissingS1"]
    if isinstance(s1_limit, bool) or not isinstance(s1_limit, int) or s1_limit <= 0:
        raise ManualAcceptanceError("Confirmed S1 limit is invalid")
    if steady_emission_y not in {"S1c", "S1c/R1c"}:
        raise ManualAcceptanceError("Confirmed steady-emission Y column is invalid")
    if not isinstance(allow_missing_s1, bool):
        raise ManualAcceptanceError("Confirmed missing-S1 choice is invalid")
    return dict(settings_snapshot)


def _confirm_settings_with_qt() -> dict[str, object] | None:
    from PySide6 import QtCore, QtWidgets

    from spectrum_organizer.ui.app import QtPreflightDialogPort

    _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return QtPreflightDialogPort(QtWidgets, QtCore).confirm(
        None,
        default_s1_limit=int(DEFAULT_SETTINGS["s1Limit"]),
        steady_emission_y=str(DEFAULT_SETTINGS["steadyEmissionY"]),
        allow_missing_s1=bool(DEFAULT_SETTINGS["allowMissingS1"]),
    )


def _ensure_qapplication():
    from PySide6 import QtWidgets

    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _select_sources_with_qt() -> tuple[Path, ...]:
    from PySide6 import QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    paths, _ = QtWidgets.QFileDialog.getOpenFileNames(
        None,
        "选择 Origin 原始文件",
        str(Path.home()),
        "Origin Projects (*.opj *.opju)",
    )
    return tuple(Path(path) for path in paths)


def _select_output_parent_with_qt() -> Path | None:
    from PySide6 import QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    path = QtWidgets.QFileDialog.getExistingDirectory(None, "选择输出位置", str(Path.home()))
    return Path(path) if path else None


def _reject_non_origin_sources(paths: tuple[Path, ...]) -> None:
    invalid = [str(path) for path in paths if path.suffix.casefold() not in {".opj", ".opju"}]
    if invalid:
        raise ManualAcceptanceError("Selected files are not Origin projects: " + ", ".join(invalid))


def _require_external_source_path(paths: tuple[Path, ...]) -> None:
    workspace = ROOT.resolve()
    if not any(
        path.resolve() != workspace and workspace not in path.resolve().parents
        for path in paths
    ):
        raise ManualAcceptanceError("At least one selected source must be outside the workspace")


def _require_fresh_external_snapshot(
    snapshots: tuple[SourceSnapshot, ...],
    evidence_root: Path,
    *,
    current_evidence_dir: Path | None = None,
) -> None:
    workspace = ROOT.resolve()
    external_hashes = {
        snapshot.sha256.casefold()
        for snapshot in snapshots
        if snapshot.path.resolve() != workspace
        and workspace not in snapshot.path.resolve().parents
    }
    if not external_hashes:
        raise ManualAcceptanceError("Authoritative source snapshots contain no external source")
    prior_hashes = _prior_acceptance_source_hashes(
        Path(evidence_root),
        current_evidence_dir=current_evidence_dir,
    )
    if external_hashes <= prior_hashes:
        raise ManualAcceptanceError(
            "At least one external source must not have been previously used for manual acceptance"
        )


def _prior_acceptance_source_hashes(
    evidence_root: Path,
    *,
    current_evidence_dir: Path | None = None,
) -> set[str]:
    hashes: set[str] = set()
    current_key = (
        _path_key(Path(current_evidence_dir))
        if current_evidence_dir is not None
        else None
    )

    def is_current(path: Path) -> bool:
        return (
            current_key is not None
            and _path_key(Path(path)) == current_key
        )

    def collect(snapshot_payloads: object) -> None:
        if not isinstance(snapshot_payloads, list) or not snapshot_payloads:
            raise ValueError(
                "source_fingerprints_before is missing or empty"
            )
        for snapshot in snapshot_payloads:
            if not isinstance(snapshot, dict):
                raise ValueError("source fingerprint is not an object")
            digest = snapshot.get("sha256")
            if not isinstance(digest, str) or len(digest) != 64 or any(
                character not in "0123456789abcdefABCDEF"
                for character in digest
            ):
                raise ValueError(
                    "source fingerprint has an invalid sha256"
                )
            hashes.add(digest.casefold())

    evidence_files = [
        *(
            (path, "source_fingerprints_before")
            for path in Path(evidence_root).glob(
                "full-run-extraction-*/extraction-only-summary.json"
            )
        ),
        *(
            (path, "snapshots")
            for path in Path(evidence_root).glob(
                "full-run-manual-*/selected-source-fingerprints-before.json"
            )
        ),
    ]
    for summary_path, snapshots_key in evidence_files:
        if is_current(summary_path.parent):
            continue
        try:
            payload = json.loads(summary_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("summary is not an object")
            collect(payload.get(snapshots_key))
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            raise ManualAcceptanceError(
                f"Could not inspect previous manual-acceptance evidence: {summary_path}"
            ) from exc
    for audit_dir in Path(evidence_root).glob(
        "full-run-manual-*/runtime-audit"
    ):
        if is_current(audit_dir.parent):
            continue
        try:
            for event in _read_historical_runtime_audit_events(audit_dir):
                if event["event_type"] == "pre_extraction_context":
                    collect(
                        event["payload"].get(
                            "source_fingerprints_before"
                        )
                    )
        except ValueError as exc:
            raise ManualAcceptanceError(
                "Could not inspect previous manual-acceptance evidence: "
                f"{audit_dir}"
            ) from exc
    return hashes


def _create_evidence_dir(
    evidence_root: Path,
    timestamp_factory: Callable[[], str] | None,
) -> _CreatedEvidenceDirectory:
    timestamp = timestamp_factory() if timestamp_factory else datetime.now().strftime("%Y%m%d_%H%M%S")
    root = Path(evidence_root)
    root.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "_01", "_02", "_03", "_04", "_05"):
        candidate = root / f"full-run-extraction-{timestamp}{suffix}"
        try:
            with create_exclusive_held_directory(candidate) as (
                created_path,
                identity,
            ):
                return _CreatedEvidenceDirectory(created_path, identity)
        except FileExistsError:
            continue
    raise ManualAcceptanceError("Could not create a unique extraction evidence directory")


def _write_json_exclusive(path: Path, payload: dict[str, object]) -> None:
    destination = Path(path)
    temp_path: Path | None = None
    committed = False
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temp_path, destination)
        committed = True
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                if not committed:
                    raise


if __name__ == "__main__":
    raise SystemExit(main())
