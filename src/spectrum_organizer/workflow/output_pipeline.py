from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
import threading
from typing import Any, Callable, Protocol, runtime_checkable
import uuid

from spectrum_organizer.runtime_audit import (
    record_runtime_audit_event,
    runtime_audit_enabled,
)
from spectrum_organizer.safety.fingerprints import snapshot_sources


def _noop() -> None:
    return None


def _noop_post_commit(_snapshot, _completion) -> None:
    return None


def _noop_register_artifact(_targets, _artifact) -> None:
    return None


def _noop_register_failed_artifact(_targets, _stage, _identity) -> None:
    return None


def _unavailable_post_commit_retry(_snapshot, _completion) -> None:
    raise RuntimeError("Committed output cleanup cannot be retried")


@runtime_checkable
class ApprovedSnapshotView(Protocol):
    snapshot_id: str
    output_plan: object
    task_snapshot_path: Path
    task_temp_root_identity: tuple[int, int]
    source_fingerprints_before: tuple[object, ...]
    selected_source_fingerprints_before: tuple[object, ...]


class SourceFingerprintMismatchError(RuntimeError):
    pass


class OutputPipelineCancelled(RuntimeError):
    def __init__(self, message: str):
        super().__init__(message)
        self.cleanup_result = None
        self.cleanup_error = None
        self.cleanup_retry = None


class OutputPipelineFailure(RuntimeError):
    def __init__(
        self,
        stage: str,
        cause: Exception,
        *,
        failure_log_path: object | None,
        cleanup_result: object | None,
        cleanup_error: Exception | None = None,
        failure_log_error: Exception | None = None,
        cleanup_retry=None,
    ):
        super().__init__(f"{stage} failed: {cause}")
        self.stage = stage
        self.cause = cause
        self.failure_log_path = failure_log_path
        self.cleanup_result = cleanup_result
        self.cleanup_error = cleanup_error
        self.failure_log_error = failure_log_error
        self.cleanup_retry = cleanup_retry


@dataclass(frozen=True)
class OutputRunRequest:
    approved_snapshot: ApprovedSnapshotView
    targets: object


@dataclass(frozen=True)
class OutputStageRequest:
    approved_snapshot: ApprovedSnapshotView
    output_parent: Path


@dataclass(frozen=True)
class VerificationRunRequest:
    approved_snapshot: ApprovedSnapshotView
    targets: object
    expected_contract: object
    expected_project_artifact: object | None = None


@dataclass(frozen=True)
class ReportBuildRequest:
    approved_snapshot: ApprovedSnapshotView
    targets: object
    source_fingerprints_after: tuple[object, ...]
    output_result: object
    verifier_result: object


@dataclass(frozen=True)
class FailureLogRequest:
    timestamp: str
    run_id: str
    stage: str
    cause: Exception
    output_attempts: tuple[Any, ...]
    verifier_attempts: tuple[Any, ...]


@dataclass(frozen=True)
class OutputPipelinePorts:
    process_gate: Callable[[], None]
    create_staging: Callable[..., object]
    run_output: Callable[[OutputRunRequest], object]
    run_verifier: Callable[[VerificationRunRequest], object]
    verify_sources: Callable[
        [tuple[object, ...], Callable[[], None]],
        tuple[object, ...],
    ]
    build_report: Callable[[ReportBuildRequest], str]
    publish: Callable[[object, str, object], object]
    cleanup: Callable[..., object]
    write_failure: Callable[[object], object]
    register_artifact: Callable[[object, object], None] = _noop_register_artifact
    register_failed_artifact: Callable[[object, str, object], None] = (
        _noop_register_failed_artifact
    )
    reset_workers: Callable[[], None] = _noop
    cancel_workers: Callable[[], None] = _noop
    retry_workers: Callable[[], None] = _noop
    post_commit: Callable[[ApprovedSnapshotView, object], None] = (
        _noop_post_commit
    )
    retry_post_commit: Callable[[ApprovedSnapshotView, object], None] = (
        _unavailable_post_commit_retry
    )


@dataclass(frozen=True)
class OutputPipelineResult:
    completion: object
    source_fingerprints_after: tuple[object, ...]
    output_attempts: tuple[Any, ...]
    verifier_attempts: tuple[Any, ...]
    post_commit_error: Exception | None = None
    post_commit_cleanup_pending: bool = False


class OutputPipelineJob:
    def __init__(
        self,
        *,
        ports: OutputPipelinePorts,
        clock,
        run_id_factory=lambda: uuid.uuid4().hex,
    ):
        self.ports = ports
        self.clock = clock
        self.run_id_factory = run_id_factory
        self._cancelled = threading.Event()
        self._committed = threading.Event()
        self._committing = threading.Event()
        self._commit_lock = threading.Lock()
        self._progress_callback = lambda _stage: None
        self._cancel_request_error = None
        self._cleanup_retry = None

    def set_progress_callback(self, callback) -> None:
        self._progress_callback = callback

    def prepare(self) -> None:
        if self._cleanup_retry is not None:
            raise RuntimeError(
                "Output cleanup remains pending; retry cleanup first"
            )
        self.ports.reset_workers()
        self._cancelled.clear()
        self._committed.clear()
        self._committing.clear()
        self._cancel_request_error = None
        self._cleanup_retry = None

    def cancel(self) -> bool:
        if not self._commit_lock.acquire(blocking=False):
            return False
        try:
            if self._committing.is_set() or self._committed.is_set():
                return False
            self._cancelled.set()
        finally:
            self._commit_lock.release()
        try:
            self.ports.cancel_workers()
        except BaseException as exc:
            self._cancel_request_error = exc
        return True

    @property
    def committed(self) -> bool:
        return self._committing.is_set() or self._committed.is_set()

    def __call__(self, request: OutputStageRequest) -> OutputPipelineResult:
        try:
            self._raise_if_cancelled()
            result = run_output_pipeline(
                request.approved_snapshot,
                request.output_parent,
                run_id=self.run_id_factory(),
                ports=replace(
                    self.ports,
                    publish=self._publish_with_commit_gate,
                ),
                clock=self.clock,
                cancel_check=self._raise_if_cancelled,
                progress=self._emit_progress,
            )
            if result.post_commit_cleanup_pending:
                self._cleanup_retry = lambda: self.ports.retry_post_commit(
                    request.approved_snapshot,
                    result.completion,
                )
            return result
        except (OutputPipelineCancelled, OutputPipelineFailure) as exc:
            self._cleanup_retry = exc.cleanup_retry
            if self._cancel_request_error is not None:
                exc.add_note(
                    "worker cancellation request also failed: "
                    f"{self._cancel_request_error}"
                )
            raise

    def retry_cleanup(self):
        self.ports.retry_workers()
        result = self._cleanup_retry() if self._cleanup_retry else None
        retained = tuple(getattr(result, "retained_unknown", ()) or ())
        if retained:
            raise RuntimeError(
                "Output cleanup retained unknown objects: "
                + "; ".join(str(path) for path in retained)
            )
        self._cleanup_retry = None
        self._cancel_request_error = None
        return result

    def _emit_progress(self, stage: str) -> None:
        self._progress_callback(stage)

    def _publish_with_commit_gate(
        self,
        targets,
        report_text,
        verifier_result,
    ):
        return self.ports.publish(
            targets,
            report_text,
            verifier_result,
            commit=self._commit_publication,
        )

    def _commit_publication(self, action):
        self._committing.set()
        try:
            with self._commit_lock:
                self._raise_if_cancelled()
            completion = action()
        except BaseException:
            self._committing.clear()
            raise
        self._committed.set()
        self._committing.clear()
        return completion

    def _raise_if_cancelled(self) -> None:
        if self._cancelled.is_set():
            error = OutputPipelineCancelled(
                "output pipeline cancelled by user"
            )
            if self._cancel_request_error is not None:
                error.add_note(
                    "worker cancellation request also failed: "
                    f"{self._cancel_request_error}"
                )
            raise error


def run_output_pipeline(
    approved_snapshot: ApprovedSnapshotView,
    output_parent: Path,
    *,
    run_id: str,
    ports: OutputPipelinePorts,
    clock: Callable[[], object],
    cancel_check: Callable[[], None],
    progress: Callable[[str], None],
) -> OutputPipelineResult:
    timestamp = clock().strftime("%Y%m%d_%H%M%S")
    targets = None
    output_result = None
    verifier_result = None

    _record_output_attempt(
        approved_snapshot,
        output_parent=Path(output_parent),
        run_id=run_id,
    )

    def begin(stage: str) -> None:
        nonlocal current_stage
        current_stage = stage
        cancel_check()
        record_runtime_audit_event(
            "output_stage_progress",
            {
                "approved_snapshot_id": approved_snapshot.snapshot_id,
                "run_id": run_id,
                "stage": stage,
            },
        )
        progress(stage)

    def cleanup_staging() -> object | None:
        if targets is None:
            return None
        return ports.cleanup(
            (targets,),
            run_id=run_id,
        )

    def register_failed_artifact(cause: BaseException) -> None:
        identity = getattr(cause, "owned_artifact_identity", None)
        if targets is not None and identity is not None:
            ports.register_failed_artifact(
                targets,
                current_stage,
                identity,
            )

    def cleanup_after_failure(
        cause: BaseException,
    ) -> tuple[object | None, Exception | None]:
        try:
            register_failed_artifact(cause)
        except Exception as registration_exc:
            cause.add_note(
                "failed artifact identity registration also failed: "
                f"{registration_exc}"
            )
        try:
            return cleanup_staging(), None
        except Exception as cleanup_exc:
            cause.add_note(
                f"staging cleanup also failed: {cleanup_exc}"
            )
            return None, cleanup_exc

    current_stage = "not_started"
    try:
        begin("process_gate")
        ports.process_gate()

        begin("create_staging")
        targets = ports.create_staging(
            Path(output_parent),
            timestamp,
            run_id=run_id,
        )
        _record_staging_created(approved_snapshot, targets)

        begin("write_output")
        output_result = ports.run_output(
            OutputRunRequest(approved_snapshot, targets)
        )
        ports.register_artifact(
            targets,
            output_result.project_artifact,
        )

        begin("verify_output")
        verifier_result = ports.run_verifier(
            VerificationRunRequest(
                approved_snapshot,
                targets,
                output_result.contract,
                getattr(output_result, "project_artifact", None),
            )
        )

        begin("verify_sources")
        selected_source_fingerprints_before = tuple(
            getattr(
                approved_snapshot,
                "selected_source_fingerprints_before",
                (),
            )
            or approved_snapshot.source_fingerprints_before
        )
        source_fingerprints_after = tuple(
            ports.verify_sources(
                selected_source_fingerprints_before,
                cancel_check,
            )
        )
        if (
            source_fingerprints_after
            != selected_source_fingerprints_before
        ):
            raise SourceFingerprintMismatchError(
                _source_fingerprint_mismatch_message(
                    selected_source_fingerprints_before,
                    source_fingerprints_after,
                )
            )

        begin("build_report")
        report_text = ports.build_report(
            ReportBuildRequest(
                approved_snapshot,
                targets,
                source_fingerprints_after,
                output_result,
                verifier_result,
            )
        )

        begin("publish")
        completion = ports.publish(
            targets,
            report_text,
            verifier_result,
        )
    except OutputPipelineCancelled as exc:
        cleanup_result, cleanup_error = cleanup_after_failure(exc)
        exc.cleanup_result = cleanup_result
        exc.cleanup_error = cleanup_error
        exc.cleanup_retry = _pending_cleanup_retry(
            cleanup_result,
            cleanup_error,
            cleanup_staging,
            fallback=getattr(exc, "cleanup_retry", None),
        )
        raise
    except Exception as exc:
        cleanup_result, cleanup_error = cleanup_after_failure(exc)
        failure_log_path = None
        failure_log_error = None
        output_attempts = tuple(
            getattr(output_result, "attempts", ())
        )
        verifier_attempts = tuple(
            getattr(verifier_result, "attempts", ())
        )
        if current_stage == "write_output":
            output_attempts = tuple(getattr(exc, "attempts", ()))
        elif current_stage == "verify_output":
            verifier_attempts = tuple(getattr(exc, "attempts", ()))
        try:
            failure_log_path = ports.write_failure(
                FailureLogRequest(
                    timestamp=timestamp,
                    run_id=run_id,
                    stage=current_stage,
                    cause=exc,
                    output_attempts=output_attempts,
                    verifier_attempts=verifier_attempts,
                )
            )
        except Exception as log_exc:
            failure_log_error = log_exc
        raise OutputPipelineFailure(
            current_stage,
            exc,
            failure_log_path=failure_log_path,
            cleanup_result=cleanup_result,
            cleanup_error=cleanup_error,
            failure_log_error=failure_log_error,
            cleanup_retry=_pending_cleanup_retry(
                cleanup_result,
                cleanup_error,
                cleanup_staging,
                fallback=getattr(exc, "cleanup_retry", None),
            ),
        ) from exc

    post_commit_error = getattr(
        completion,
        "post_commit_error",
        None,
    )
    post_commit_cleanup_pending = post_commit_error is not None
    try:
        _record_publication_committed(
            approved_snapshot,
            targets,
            completion,
        )
    except Exception as exc:
        post_commit_error = _retain_post_commit_error(
            post_commit_error,
            exc,
            "publication audit also failed",
        )
    try:
        progress("committed")
    except Exception as exc:
        post_commit_error = _retain_post_commit_error(
            post_commit_error,
            exc,
            "commit notification also failed",
        )
    try:
        ports.post_commit(approved_snapshot, completion)
    except Exception as exc:
        post_commit_cleanup_pending = True
        post_commit_error = _retain_post_commit_error(
            post_commit_error,
            exc,
            "post-commit cleanup also failed",
        )
    try:
        progress("complete")
    except Exception as exc:
        post_commit_error = _retain_post_commit_error(
            post_commit_error,
            exc,
            "completion notification also failed",
        )
    return OutputPipelineResult(
        completion=completion,
        source_fingerprints_after=source_fingerprints_after,
        output_attempts=tuple(output_result.attempts),
        verifier_attempts=tuple(verifier_result.attempts),
        post_commit_error=post_commit_error,
        post_commit_cleanup_pending=post_commit_cleanup_pending,
    )


def _retain_post_commit_error(
    primary: Exception | None,
    secondary: Exception,
    label: str,
) -> Exception:
    if primary is None:
        return secondary
    primary.add_note(f"{label}: {secondary}")
    for note in getattr(secondary, "__notes__", ()):
        primary.add_note(str(note))
    return primary


def _pending_cleanup_retry(
    cleanup_result,
    cleanup_error,
    retry,
    *,
    fallback=None,
):
    retained = tuple(
        getattr(cleanup_result, "retained_unknown", ()) or ()
    )
    if cleanup_error is not None or retained:
        return retry
    return fallback


def _record_output_attempt(
    approved_snapshot,
    *,
    output_parent: Path,
    run_id: str,
) -> None:
    if not runtime_audit_enabled():
        return
    parent = Path(output_parent).resolve()
    existed = parent.is_dir()
    entries = sorted(path.name for path in parent.iterdir()) if existed else []
    record_runtime_audit_event(
        "output_stage_attempt",
        {
            "approved_snapshot_id": approved_snapshot.snapshot_id,
            "run_id": run_id,
            "output_parent": str(parent),
            "output_parent_existed_before": existed,
            "output_parent_entries_before": entries,
            "task_temp_root": str(
                Path(approved_snapshot.task_snapshot_path).parent.resolve()
            ),
        },
    )


def _record_staging_created(approved_snapshot, targets) -> None:
    if not runtime_audit_enabled():
        return
    record_runtime_audit_event(
        "output_staging_created",
        {
            "approved_snapshot_id": approved_snapshot.snapshot_id,
            "run_id": targets.run_id,
            "output_parent": str(Path(targets.output_parent).resolve()),
            "staging_dir": str(Path(targets.staging_dir).resolve()),
            "staging_project_path": str(
                Path(targets.staging_project_path).resolve()
            ),
            "verifier_mutation_path": str(
                Path(targets.verifier_mutation_path).resolve()
            ),
        },
    )


def _record_publication_committed(
    approved_snapshot,
    targets,
    completion,
) -> None:
    if not runtime_audit_enabled():
        return
    snapshots = snapshot_sources(
        [Path(completion.project_path), Path(completion.report_path)],
        [],
    )
    record_runtime_audit_event(
        "publication_committed",
        {
            "approved_snapshot_id": approved_snapshot.snapshot_id,
            "run_id": targets.run_id,
            "output_parent": str(Path(targets.output_parent).resolve()),
            "final_run_dir": str(Path(completion.output_path).resolve()),
            "final_project_path": str(Path(completion.project_path).resolve()),
            "final_report_path": str(Path(completion.report_path).resolve()),
            "artifacts": [
                {
                    "path": str(snapshot.path),
                    "canonical_path": str(snapshot.canonical_path),
                    "sha256": snapshot.sha256,
                    "size_bytes": snapshot.size_bytes,
                    "mtime_ns": snapshot.mtime_ns,
                    "device_id": snapshot.device_id,
                    "file_id": snapshot.file_id,
                }
                for snapshot in snapshots
            ],
        },
    )


def _source_fingerprint_mismatch_message(expected, actual) -> str:
    if len(expected) != len(actual):
        return (
            "selected source fingerprint count changed after output "
            f"verification: expected={len(expected)}; actual={len(actual)}"
        )
    for index, (before, after) in enumerate(zip(expected, actual, strict=True)):
        before_path = getattr(before, "path", None)
        after_path = getattr(after, "path", None)
        if before_path != after_path:
            return (
                "selected source path changed after output verification: "
                f"index={index}; expected={before_path}; actual={after_path}"
            )
        for field in (
            "sha256",
            "size_bytes",
            "mtime_ns",
            "device_id",
            "file_id",
        ):
            before_value = getattr(before, field, None)
            after_value = getattr(after, field, None)
            if before_value != after_value:
                return (
                    "selected source fingerprint changed after output "
                    f"verification: path={before_path}; field={field}; "
                    f"expected={before_value}; actual={after_value}"
                )
        if before != after:
            return (
                "selected source fingerprint changed after output "
                f"verification: path={before_path}; expected={before!r}; "
                f"actual={after!r}"
            )
    return "selected source fingerprints changed after output verification"
