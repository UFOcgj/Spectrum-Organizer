from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import time
import uuid

from spectrum_organizer.origin.contracts import (
    BookWriteContract,
    ColumnWriteContract,
    FolderWriteContract,
    ProjectArtifactEvidence,
    ProjectWriteContract,
)
from spectrum_organizer.origin.output_worker import (
    DeterministicOutputError,
    InfrastructureOutputError,
    OutputContractWorkerCommand,
    OutputRetryResult,
    build_project_write_contract,
    classify_output_error,
    run_output_contract_worker,
    run_output_with_infrastructure_retry,
)
from spectrum_organizer.origin.process_identity import (
    ORIGIN_IDENTITY_HANDOFF_PATH_ENV,
    ORIGIN_IDENTITY_HANDOFF_TOKEN_ENV,
)
from spectrum_organizer.origin.verify_worker import (
    DeterministicVerificationError,
    InfrastructureVerificationError,
    MismatchReport,
    VerificationMismatchError,
    VerifierRetryResult,
    VerifierWorkerCommand,
    classify_verifier_error,
    run_verifier_with_infrastructure_retry,
    run_verifier_worker,
)
from spectrum_organizer.safety.process_job import (
    PARENT_START_GATE_ENV,
    PARENT_START_GATE_TOKEN_ENV,
    bind_process_to_job,
    close_bound_process_job,
    terminate_bound_process,
    wait_for_parent_start_gate,
)
from spectrum_organizer.safety.identity_paths import (
    create_exclusive_held_file,
    IdentityPathError,
    lexical_path_exists,
    path_identity,
    unlink_owned_path,
)
from spectrum_organizer.safety.process_boundary import (
    ProcessIdentity,
    WindowsOriginProcessController,
)
from spectrum_organizer.runtime_audit import (
    record_runtime_audit_event,
    runtime_audit_enabled,
    runtime_audit_file_identity,
)
from spectrum_organizer.reporting.run_report import (
    approved_output_report_ledger,
)


class OriginChildProcessError(RuntimeError):
    pass


def project_contract_to_payload(
    contract: ProjectWriteContract,
) -> dict[str, object]:
    _validate_origin_visible_book_names(contract)
    return {
        "root_path": contract.root_path,
        "folders": [
            {
                "path": folder.path,
                "books": [
                    {
                        "display_long_name": _origin_visible_text(
                            book.display_long_name
                        ),
                        "internal_short_name": book.internal_short_name,
                        "columns": [
                            {
                                "short_name": column.short_name,
                                "designation": column.designation,
                                "comment": _origin_visible_text(
                                    column.comment
                                ),
                                "values": [
                                    None if value is None else str(value)
                                    for value in column.values
                                ],
                                "formula": column.formula,
                                "method": column.method,
                            }
                            for column in book.columns
                        ],
                    }
                    for book in folder.books
                ],
            }
            for folder in contract.folders
        ],
    }


def _origin_visible_text(value: str) -> str:
    return value.replace("×", "x")


def _validate_origin_visible_book_names(contract: ProjectWriteContract) -> None:
    for folder in contract.folders:
        visible_names: set[str] = set()
        for book in folder.books:
            visible_name = _origin_visible_text(book.display_long_name)
            if visible_name in visible_names:
                raise ValueError(
                    "Origin-visible Book Long Name collision in Folder "
                    f"{folder.path}: {visible_name}"
                )
            visible_names.add(visible_name)


def project_contract_from_payload(
    payload: object,
) -> ProjectWriteContract:
    if not isinstance(payload, dict) or set(payload) != {
        "root_path",
        "folders",
    }:
        raise ValueError("Origin project contract fields are invalid")
    root_path = _required_text(payload["root_path"], "root_path")
    folders_payload = _required_list(payload["folders"], "folders")
    folders = []
    for folder_payload in folders_payload:
        _require_fields(folder_payload, {"path", "books"}, "folder")
        books = []
        for book_payload in _required_list(folder_payload["books"], "books"):
            _require_fields(
                book_payload,
                {"display_long_name", "internal_short_name", "columns"},
                "book",
            )
            columns = []
            for column_payload in _required_list(
                book_payload["columns"],
                "columns",
            ):
                _require_fields(
                    column_payload,
                    {
                        "short_name",
                        "designation",
                        "comment",
                        "values",
                        "formula",
                        "method",
                    },
                    "column",
                )
                columns.append(
                    ColumnWriteContract(
                        _required_text(
                            column_payload["short_name"],
                            "short_name",
                        ),
                        _required_text(
                            column_payload["designation"],
                            "designation",
                        ),
                        _required_text(
                            column_payload["comment"],
                            "comment",
                            allow_empty=True,
                        ),
                        tuple(
                            _decimal_or_none(value)
                            for value in _required_list(
                                column_payload["values"],
                                "values",
                            )
                        ),
                        _optional_text(column_payload["formula"], "formula"),
                        _optional_text(column_payload["method"], "method"),
                    )
                )
            books.append(
                BookWriteContract(
                    _required_text(
                        book_payload["display_long_name"],
                        "display_long_name",
                        allow_empty=True,
                    ),
                    _optional_text(
                        book_payload["internal_short_name"],
                        "internal_short_name",
                    ),
                    tuple(columns),
                )
            )
        folders.append(
            FolderWriteContract(
                _required_text(folder_payload["path"], "path"),
                tuple(books),
            )
        )
    return ProjectWriteContract(root_path, tuple(folders))


def output_process_main(
    argv=None,
    *,
    stdin=None,
    stdout=None,
    output_runner=None,
    verifier_runner=None,
) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args not in (["output"], ["verifier"]):
        return 2
    input_stream = stdin or sys.stdin
    output_stream = stdout or sys.stdout
    role = args[0]
    command = None
    cleanup_identity = None
    try:
        wait_for_parent_start_gate()
        payload = json.load(input_stream)
        if role == "output":
            command = _output_command_from_payload(payload)
            project_artifact = (output_runner or _run_output_child)(command)
            cleanup_identity = project_artifact.identity
        else:
            command = _verifier_command_from_payload(payload)
            project_artifact, cleanup_identity = _verifier_child_result(
                (verifier_runner or _run_verifier_child)(command)
            )
        _record_successful_worker_targets(role, command)
        result = {
            "ok": True,
            "project_artifact": _project_artifact_to_payload(
                project_artifact
            ),
        }
        if cleanup_identity is not None:
            result["owned_artifact_identity"] = list(
                cleanup_identity
            )
        return_code = 0
    except Exception as exc:
        classifier = (
            classify_output_error
            if role == "output"
            else classify_verifier_error
        )
        result = {
            "ok": False,
            "classification": classifier(exc),
            "error": str(exc),
            "error_type": type(exc).__name__,
            "error_notes": tuple(
                str(note)
                for note in getattr(exc, "__notes__", ())
            ),
        }
        cleanup_identity = getattr(
            exc,
            "owned_artifact_identity",
            cleanup_identity,
        )
        if cleanup_identity is not None:
            result["owned_artifact_identity"] = list(
                cleanup_identity
            )
        if isinstance(exc, VerificationMismatchError):
            result["mismatch_report"] = _mismatch_report_to_payload(
                exc.report
            )
        return_code = 1
    json.dump(result, output_stream, ensure_ascii=False)
    output_stream.flush()
    return return_code


def _verifier_child_result(result) -> tuple[
    ProjectArtifactEvidence,
    tuple[int, int],
]:
    if not isinstance(result, tuple) or len(result) != 2:
        raise OriginChildProcessError(
            "Origin verifier did not return creation-bound cleanup identity"
        )
    project_artifact, cleanup_identity = result
    if (
        not isinstance(project_artifact, ProjectArtifactEvidence)
        or not isinstance(cleanup_identity, tuple)
        or len(cleanup_identity) != 2
        or any(not isinstance(part, int) for part in cleanup_identity)
        or cleanup_identity[1] == 0
    ):
        raise OriginChildProcessError(
            "Origin verifier returned an invalid cleanup identity"
        )
    return project_artifact, cleanup_identity


def _cleanup_artifact_identity_from_result(
    result: object,
) -> tuple[int, int] | None:
    if not isinstance(result, dict):
        return None
    payload = result.get("owned_artifact_identity")
    if payload is None:
        return None
    try:
        return _identity_from_payload(
            payload,
            "owned_artifact_identity",
        )
    except ValueError as exc:
        raise OriginChildProcessError(
            "Origin child returned an invalid cleanup artifact identity"
        ) from exc


def _record_successful_worker_targets(role: str, command) -> None:
    if role == "output":
        targets = (command.staging_project_path,)
        contract = command.approved_contract
    else:
        targets = (
            command.staged_project_path,
            command.mutation_copy_path,
        )
        contract = command.expected_contract
    spectrum_count, column_count = _contract_counts(contract)
    target_identities = (
        [runtime_audit_file_identity(path) for path in targets]
        if runtime_audit_enabled()
        else []
    )
    record_runtime_audit_event(
        "origin_worker_targets",
        {
            "role": role,
            "approved_snapshot_id": command.approved_snapshot_id,
            "run_staging_root": str(command.run_staging_root),
            "attempt": command.attempt,
            "open_targets": [str(path) for path in targets],
            "open_target_identities": target_identities,
            "spectrum_count": spectrum_count,
            "column_count": column_count,
        },
    )


def _contract_counts(contract) -> tuple[int, int]:
    columns = tuple(
        column
        for folder in contract.folders
        for book in folder.books
        for column in book.columns
    )
    return sum(column.formula is not None for column in columns), len(columns)


@dataclass
class _OriginIdentityHandoff:
    path: Path
    identity: tuple[int, int]
    token: str
    role: str
    attempt_binding: dict[str, object]
    hold: object
    stream: object
    origin_identity: ProcessIdentity | None = None
    origin_may_have_started: bool = False
    origin_cleanup_required: bool = False
    origin_cleanup_complete: bool = False


def _origin_identity_attempt_binding(
    payload: dict[str, object],
) -> dict[str, object]:
    return {
        field: payload.get(field)
        for field in (
            "approved_snapshot_id",
            "run_staging_root",
            "attempt",
        )
    }


class JsonOriginChildProcessRunner:
    def __init__(
        self,
        *,
        process_factory=None,
        cancellation_error_factory=OriginChildProcessError,
        poll_interval: float = 0.1,
        termination_timeout: float = 30.0,
        monotonic=time.monotonic,
        origin_process_controller=None,
    ):
        self.process_factory = process_factory or subprocess.Popen
        self._require_process_job = (
            process_factory is None and sys.platform == "win32"
        )
        self.cancellation_error_factory = cancellation_error_factory
        self.poll_interval = poll_interval
        self.termination_timeout = termination_timeout
        self.monotonic = monotonic
        self.origin_process_controller = (
            origin_process_controller or WindowsOriginProcessController()
        )
        self._cancelled = threading.Event()
        self._lock = threading.Lock()
        self._current_process = None
        self._termination_deadline = None
        self._pending_start_gate = None
        self._current_identity_handoff = None
        self._pending_identity_handoff = None

    def reset(self) -> None:
        with self._lock:
            if (
                self._pending_start_gate is not None
                or self._pending_identity_handoff is not None
            ):
                raise OriginChildProcessError(
                    "Origin child sidecar cleanup is still pending"
                )
            process = self._current_process
        if process is not None and process.poll() is None:
            raise OriginChildProcessError(
                "Origin child process is still running"
            )
        self._complete_retained_origin_cleanup()
        if process is not None:
            close_bound_process_job(process)
            with self._lock:
                if self._current_process is process:
                    self._current_process = None
                    self._termination_deadline = None
        self._release_current_identity_handoff()
        with self._lock:
            self._cancelled.clear()

    def cancel(self) -> None:
        self._cancelled.set()
        with self._lock:
            process = self._current_process
            identity_handoff = self._current_identity_handoff
            if process is not None and self._termination_deadline is None:
                self._termination_deadline = (
                    self.monotonic() + self.termination_timeout
                )
        if (
            process is not None
            and process.poll() is None
            and identity_handoff is None
        ):
            terminate_bound_process(process)

    def retry_cleanup(self) -> None:
        with self._lock:
            process = self._current_process
            pending_start_gate = self._pending_start_gate
            pending_identity_handoff = self._pending_identity_handoff
            if process is not None and process.poll() is None:
                self._termination_deadline = (
                    self.monotonic() + self.termination_timeout
                )
        if process is not None:
            if process.poll() is None:
                while not self._terminate_process_with_origin_cleanup(
                    process
                ):
                    if self._remaining_termination_time() <= 0:
                        raise self._termination_timeout_error()
                    time.sleep(self.poll_interval)
                if process.poll() is None:
                    try:
                        process.wait(timeout=self._remaining_termination_time())
                    except subprocess.TimeoutExpired as exc:
                        raise self._termination_timeout_error() from exc
            if process.poll() is None:
                raise self._termination_timeout_error()
            close_bound_process_job(process)
            with self._lock:
                if self._current_process is process:
                    self._current_process = None
                    self._termination_deadline = None
        self._complete_retained_origin_cleanup()
        self._release_current_identity_handoff()
        if pending_start_gate is not None:
            gate_path, gate_identity = pending_start_gate
            unlink_owned_path(gate_path, gate_identity)
            with self._lock:
                if self._pending_start_gate == pending_start_gate:
                    self._pending_start_gate = None
        if pending_identity_handoff is not None:
            path, identity = pending_identity_handoff
            unlink_owned_path(path, identity)
            with self._lock:
                if self._pending_identity_handoff == pending_identity_handoff:
                    self._pending_identity_handoff = None

    def __call__(self, role: str, payload: dict[str, object]):
        if role not in {"output", "verifier"}:
            raise ValueError(f"Unsupported Origin child role: {role}")
        if self._cancelled.is_set():
            raise self.cancellation_error_factory(
                "Origin output stage was cancelled"
            )
        with self._lock:
            previous = self._current_process
            pending_start_gate = self._pending_start_gate
            pending_identity_handoff = self._pending_identity_handoff
        if (
            pending_start_gate is not None
            or pending_identity_handoff is not None
        ):
            raise OriginChildProcessError(
                "Previous Origin child sidecar cleanup is still pending"
            )
        if previous is not None:
            if previous.poll() is None:
                raise OriginChildProcessError(
                    "Previous Origin child process cleanup is still pending"
                )
            self._complete_retained_origin_cleanup()
            close_bound_process_job(previous)
            self._release_current_identity_handoff()
            with self._lock:
                if self._current_process is previous:
                    self._current_process = None
                    self._termination_deadline = None
        start_gate_path = None
        start_gate_token = None
        start_gate_hold = None
        start_gate_identity = None
        identity_handoff_path = None
        identity_handoff_token = None
        identity_handoff_hold = None
        identity_handoff = None
        if self._require_process_job:
            child_marker = uuid.uuid4().hex
            start_gate_token = uuid.uuid4().hex
            temp_root = Path(os.environ.get("TEMP", "."))
            start_gate_path = temp_root / (
                f"SpectrumOrganizer_{child_marker}.gate"
            )
            identity_handoff_path = temp_root / (
                f"SpectrumOrganizer_{child_marker}.origin.json"
            )
            identity_handoff_token = start_gate_token
        env = os.environ.copy()
        if start_gate_path is not None:
            env[PARENT_START_GATE_ENV] = str(start_gate_path)
            env[PARENT_START_GATE_TOKEN_ENV] = start_gate_token
            env[ORIGIN_IDENTITY_HANDOFF_PATH_ENV] = str(
                identity_handoff_path
            )
            env[ORIGIN_IDENTITY_HANDOFF_TOKEN_ENV] = (
                identity_handoff_token
            )
        process = self.process_factory(
            _origin_process_command(role),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        with self._lock:
            self._current_process = process
        pending_error = None
        try:
            if self._cancelled.is_set():
                raise self.cancellation_error_factory(
                    "Origin output stage was cancelled"
                )
            bind_process_to_job(
                process,
                required=self._require_process_job,
            )
            if start_gate_path is not None:
                identity_handoff_hold = create_exclusive_held_file(
                    identity_handoff_path
                )
                handoff_stream, handoff_identity = (
                    identity_handoff_hold.__enter__()
                )
                identity_handoff = _OriginIdentityHandoff(
                    path=identity_handoff_path,
                    identity=handoff_identity,
                    token=identity_handoff_token,
                    role=role,
                    attempt_binding=_origin_identity_attempt_binding(
                        payload
                    ),
                    hold=identity_handoff_hold,
                    stream=handoff_stream,
                )
                with self._lock:
                    self._current_identity_handoff = identity_handoff
                if self._cancelled.is_set():
                    raise self.cancellation_error_factory(
                        "Origin output stage was cancelled"
                    )
                start_gate_hold = create_exclusive_held_file(
                    start_gate_path
                )
                gate_stream, start_gate_identity = (
                    start_gate_hold.__enter__()
                )
                gate_stream.write(start_gate_token.encode("ascii"))
                gate_stream.flush()
                os.fsync(gate_stream.fileno())
                identity_handoff.origin_may_have_started = True
            stdout_text, stderr_text = self._communicate(
                process,
                json.dumps(payload, ensure_ascii=False),
            )
            if self._cancelled.is_set():
                raise self.cancellation_error_factory(
                    "Origin output stage was cancelled"
                )
            try:
                result = json.loads(stdout_text)
            except json.JSONDecodeError as exc:
                detail = stderr_text.strip() or stdout_text.strip()
                raise OriginChildProcessError(
                    f"Origin {role} child returned invalid JSON: {detail}"
                ) from exc
            if not isinstance(result, dict):
                raise OriginChildProcessError(
                    f"Origin {role} child returned an invalid result"
                )
            result_ok = result.get("ok")
            if not isinstance(result_ok, bool):
                raise OriginChildProcessError(
                    f"Origin {role} child returned an invalid result"
                )
            expected_returncode = 0 if result_ok else 1
            if process.returncode != expected_returncode:
                detail = stderr_text.strip() or f"exit code {process.returncode}"
                raise OriginChildProcessError(
                    f"Origin {role} child result conflicts with {detail}"
                )
            return result
        except BaseException as exc:
            pending_error = exc
            raise
        finally:
            cleanup_errors = []
            try:
                if process.poll() is None:
                    cleanup_started = (
                        self._terminate_process_with_origin_cleanup(
                            process
                        )
                    )
                    if not cleanup_started:
                        raise self._termination_timeout_error()
                    if process.poll() is None:
                        process.wait(
                            timeout=self._remaining_termination_time()
                        )
                    if process.poll() is None:
                        raise self._termination_timeout_error()
                if (
                    pending_error is not None
                    and identity_handoff is not None
                    and identity_handoff.origin_may_have_started
                ):
                    published_identity = (
                        self._read_origin_identity_handoff(
                            identity_handoff
                        )
                    )
                    if published_identity is not None:
                        identity_handoff.origin_identity = (
                            published_identity
                        )
                        identity_handoff.origin_cleanup_required = True
                self._complete_retained_origin_cleanup()
            except subprocess.TimeoutExpired:
                cleanup_errors.append(
                    (
                        "child process termination",
                        self._termination_timeout_error(),
                    )
                )
            except Exception as exc:
                cleanup_errors.append(
                    ("child process termination", exc)
                )
            if process.poll() is not None:
                try:
                    close_bound_process_job(process)
                except Exception as exc:
                    cleanup_errors.append(("child process job close", exc))
                else:
                    with self._lock:
                        if self._current_process is process:
                            self._current_process = None
                            self._termination_deadline = None
                    try:
                        self._release_current_identity_handoff()
                    except Exception as exc:
                        cleanup_errors.append(
                            ("Origin identity handoff cleanup", exc)
                        )
            if start_gate_hold is not None and start_gate_identity is not None:
                try:
                    start_gate_hold.__exit__(None, None, None)
                except Exception as exc:
                    cleanup_errors.append(("child start gate close", exc))
                try:
                    unlink_owned_path(
                        start_gate_path,
                        start_gate_identity,
                    )
                except Exception as exc:
                    retained_path = Path(
                        getattr(exc, "retained_path", start_gate_path)
                    )
                    with self._lock:
                        self._pending_start_gate = (
                            retained_path,
                            start_gate_identity,
                        )
                    cleanup_errors.append(("child start gate cleanup", exc))
                else:
                    with self._lock:
                        self._pending_start_gate = None
            if cleanup_errors:
                if pending_error is not None:
                    for label, exc in cleanup_errors:
                        pending_error.add_note(
                            f"{label} also failed: {exc}"
                        )
                else:
                    label, exc = cleanup_errors[0]
                    if self._cancelled.is_set():
                        failure = self.cancellation_error_factory(
                            f"Origin {role} {label} failed: {exc}"
                        )
                        for extra_label, extra_exc in cleanup_errors[1:]:
                            failure.add_note(
                                f"{extra_label} also failed: {extra_exc}"
                            )
                        raise failure from exc
                    failure = OriginChildProcessError(
                        f"Origin {role} {label} failed: {exc}"
                    )
                    for extra_label, extra_exc in cleanup_errors[1:]:
                        failure.add_note(
                            f"{extra_label} also failed: {extra_exc}"
                        )
                    raise failure from exc

    def _communicate(self, process, input_text: str) -> tuple[str, str]:
        pending_input = input_text
        while True:
            try:
                return process.communicate(
                    input=pending_input,
                    timeout=self.poll_interval,
                )
            except subprocess.TimeoutExpired:
                pending_input = None
                if self._cancelled.is_set() and process.poll() is None:
                    self._terminate_process_with_origin_cleanup(process)
                    if (
                        process.poll() is None
                        and self.monotonic()
                        >= self._ensure_termination_deadline()
                    ):
                        raise self.cancellation_error_factory(
                            "Origin child process did not exit within the termination deadline"
                        )

    def _terminate_process_with_origin_cleanup(self, process) -> bool:
        with self._lock:
            handoff = self._current_identity_handoff
        if handoff is None:
            terminate_bound_process(process)
            return True
        if not handoff.origin_may_have_started:
            terminate_bound_process(process)
            return True
        if handoff.origin_identity is None:
            handoff.origin_identity = self._read_origin_identity_handoff(
                handoff
            )
        if handoff.origin_identity is None:
            return False
        if process.poll() is None:
            terminate_bound_process(process)
            handoff.origin_cleanup_required = True
        self._complete_retained_origin_cleanup()
        return True

    def _read_origin_identity_handoff(
        self,
        handoff: _OriginIdentityHandoff,
    ) -> ProcessIdentity | None:
        if path_identity(handoff.path) != handoff.identity:
            raise OriginChildProcessError(
                "Origin identity handoff file identity changed"
            )
        handoff.stream.seek(0)
        encoded = handoff.stream.read()
        if not encoded:
            return None
        try:
            payload = json.loads(encoded.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        expected_fields = {
            "schema_version",
            "token",
            "role",
            "attempt_binding",
            "pid",
            "start_time_ns",
        }
        if (
            not isinstance(payload, dict)
            or set(payload) != expected_fields
            or payload["schema_version"] != 1
            or not isinstance(payload["token"], str)
            or not secrets.compare_digest(
                payload["token"],
                handoff.token,
            )
            or payload["role"] != handoff.role
            or payload["attempt_binding"] != handoff.attempt_binding
            or type(payload["pid"]) is not int
            or payload["pid"] <= 0
            or type(payload["start_time_ns"]) is not int
            or payload["start_time_ns"] <= 0
        ):
            raise OriginChildProcessError(
                "Origin identity handoff payload is invalid"
            )
        return ProcessIdentity(
            pid=payload["pid"],
            start_time_ns=payload["start_time_ns"],
        )

    def _complete_retained_origin_cleanup(self) -> None:
        with self._lock:
            handoff = self._current_identity_handoff
        if (
            handoff is None
            or not handoff.origin_cleanup_required
            or handoff.origin_cleanup_complete
        ):
            return
        identity = handoff.origin_identity
        if identity is None:
            identity = self._read_origin_identity_handoff(handoff)
            handoff.origin_identity = identity
        if identity is None:
            raise self._termination_timeout_error()
        timeout = self._remaining_termination_time()
        if timeout <= 0:
            raise self._termination_timeout_error()
        self.origin_process_controller.close_program_owned(
            identity,
            timeout=timeout,
        )
        handoff.origin_cleanup_complete = True

    def _release_current_identity_handoff(self) -> None:
        with self._lock:
            handoff = self._current_identity_handoff
        if handoff is None:
            return
        if (
            handoff.origin_cleanup_required
            and not handoff.origin_cleanup_complete
        ):
            raise OriginChildProcessError(
                "Owned Origin cleanup is still pending"
            )
        if handoff.hold is not None:
            handoff.hold.__exit__(None, None, None)
            handoff.hold = None
            handoff.stream = None
        try:
            unlink_owned_path(handoff.path, handoff.identity)
        except Exception:
            with self._lock:
                self._current_identity_handoff = None
                self._pending_identity_handoff = (
                    handoff.path,
                    handoff.identity,
                )
            raise
        with self._lock:
            if self._current_identity_handoff is handoff:
                self._current_identity_handoff = None

    def _ensure_termination_deadline(self) -> float:
        with self._lock:
            if self._termination_deadline is None:
                self._termination_deadline = (
                    self.monotonic() + self.termination_timeout
                )
            return self._termination_deadline

    def _remaining_termination_time(self) -> float:
        deadline = self._ensure_termination_deadline()
        return max(0.0, deadline - self.monotonic())

    def _termination_timeout_error(self) -> Exception:
        message = (
            "Origin child process did not exit within the termination deadline"
        )
        if self._cancelled.is_set():
            return self.cancellation_error_factory(message)
        return OriginChildProcessError(message)


class OriginWorkerProcessPort:
    def __init__(
        self,
        *,
        child_runner,
        prepare_output,
        prepare_verifier,
        cleanup_output,
        cleanup_verifier,
        cancellation_exception=(),
    ):
        self.child_runner = child_runner
        self.prepare_output = prepare_output
        self.prepare_verifier = prepare_verifier
        self.cleanup_output = cleanup_output
        self.cleanup_verifier = cleanup_verifier
        self.cancellation_exception = cancellation_exception

    def reset(self) -> None:
        reset = getattr(self.child_runner, "reset", None)
        if callable(reset):
            reset()

    def cancel(self) -> None:
        cancel = getattr(self.child_runner, "cancel", None)
        if callable(cancel):
            cancel()

    def retry_cleanup(self) -> None:
        retry_cleanup = getattr(self.child_runner, "retry_cleanup", None)
        if callable(retry_cleanup):
            retry_cleanup()

    def run_output(self, request) -> OutputRetryResult:
        contract = build_project_write_contract(
            request.approved_snapshot.output_plan
        )
        _record_approved_counts(request.approved_snapshot)
        command = request.targets

        project_artifact = None
        cleanup_artifact_identity = None

        def worker_factory(_attempt):
            def worker(_command):
                nonlocal project_artifact, cleanup_artifact_identity
                cleanup_artifact_identity = self.prepare_output(
                    request.targets,
                )
                attempt_command = replace(
                    _command,
                    staging_project_identity=cleanup_artifact_identity,
                    attempt=_attempt,
                )
                _record_worker_target_attempt(
                    "output",
                    attempt_command,
                    _attempt,
                )
                try:
                    result = self._run_child(
                        "output",
                        _output_payload(
                            request.approved_snapshot.snapshot_id,
                            contract,
                            attempt_command,
                        ),
                    )
                except BaseException as exc:
                    exc.owned_artifact_identity = cleanup_artifact_identity
                    raise
                reported_identity = _cleanup_artifact_identity_from_result(result)
                if (
                    reported_identity is not None
                    and reported_identity != cleanup_artifact_identity
                ):
                    raise OriginChildProcessError(
                        "Origin output child reported a different reserved artifact identity"
                    )
                success = _raise_child_failure(result, "output")
                project_artifact = _project_artifact_from_payload(
                    success.get("project_artifact")
                )
                return contract

            return worker

        retry_command = _output_command_from_targets(
            request.approved_snapshot,
            command,
        )
        def cleanup_attempt(_attempt):
            nonlocal cleanup_artifact_identity
            retired_identity = cleanup_artifact_identity
            try:
                self.retry_cleanup()
                self.cleanup_output(
                    request.targets,
                    retired_identity,
                )
            except Exception as exc:
                _record_worker_retry_cleanup(
                    "output",
                    retry_command,
                    _attempt,
                    artifact_path=request.targets.staging_project_path,
                    artifact_identity=retired_identity,
                    completed=False,
                    error=exc,
                )
                raise
            cleanup_artifact_identity = None
            try:
                _record_worker_retry_cleanup(
                    "output",
                    retry_command,
                    _attempt,
                    artifact_path=request.targets.staging_project_path,
                    artifact_identity=retired_identity,
                    completed=True,
                )
            except Exception as exc:
                exc.owned_artifact_identity = None
                raise

        retry_result = run_output_with_infrastructure_retry(
            retry_command,
            worker_factory,
            cleanup_attempt,
        )
        if project_artifact is None:
            raise OriginChildProcessError(
                "Origin output child omitted project artifact evidence"
            )
        return replace(
            retry_result,
            project_artifact=project_artifact,
        )
    def run_verifier(self, request) -> VerifierRetryResult:
        command = _verifier_command_from_request(request)
        verified_project_artifact = None
        cleanup_artifact_identity = None

        def verifier_factory(_attempt):
            def verifier(_command):
                nonlocal verified_project_artifact, cleanup_artifact_identity
                cleanup_artifact_identity = self.prepare_verifier(
                    request.targets,
                )
                attempt_command = replace(
                    _command,
                    mutation_copy_identity=cleanup_artifact_identity,
                    attempt=_attempt,
                )
                _record_worker_target_attempt(
                    "verifier",
                    attempt_command,
                    _attempt,
                )
                try:
                    result = self._run_child(
                        "verifier",
                        _verifier_payload(attempt_command),
                    )
                except BaseException as exc:
                    exc.owned_artifact_identity = cleanup_artifact_identity
                    raise
                reported_identity = _cleanup_artifact_identity_from_result(result)
                if (
                    reported_identity is not None
                    and reported_identity != cleanup_artifact_identity
                ):
                    raise OriginChildProcessError(
                        "Origin verifier child reported a different reserved artifact identity"
                    )
                success = _raise_child_failure(result, "verifier")
                verified_project_artifact = _project_artifact_from_payload(
                    success.get("project_artifact")
                )
                return verified_project_artifact

            return verifier

        def cleanup_attempt(_attempt):
            nonlocal cleanup_artifact_identity
            retired_identity = cleanup_artifact_identity
            try:
                self.retry_cleanup()
                self.cleanup_verifier(
                    request.targets,
                    retired_identity,
                )
            except Exception as exc:
                _record_worker_retry_cleanup(
                    "verifier",
                    command,
                    _attempt,
                    artifact_path=request.targets.verifier_mutation_path,
                    artifact_identity=retired_identity,
                    completed=False,
                    error=exc,
                )
                raise
            cleanup_artifact_identity = None
            try:
                _record_worker_retry_cleanup(
                    "verifier",
                    command,
                    _attempt,
                    artifact_path=request.targets.verifier_mutation_path,
                    artifact_identity=retired_identity,
                    completed=True,
                )
            except Exception as exc:
                exc.owned_artifact_identity = None
                raise

        result = run_verifier_with_infrastructure_retry(
            command,
            verifier_factory,
            cleanup_attempt=cleanup_attempt,
        )
        self.cleanup_verifier(
            request.targets,
            cleanup_artifact_identity,
        )
        if verified_project_artifact is None:
            raise OriginChildProcessError(
                "Origin verifier child omitted project artifact evidence"
            )
        return replace(
            result,
            verified_project_artifact=verified_project_artifact,
        )

    def _run_child(self, role: str, payload: dict[str, object]):
        try:
            return self.child_runner(role, payload)
        except self.cancellation_exception:
            raise
        except Exception as exc:
            error_type = (
                InfrastructureOutputError
                if role == "output"
                else InfrastructureVerificationError
            )
            notes = tuple(str(note) for note in getattr(exc, "__notes__", ()))
            detail = str(exc)
            if notes:
                detail += "; " + "; ".join(notes)
            wrapped = error_type(
                f"Origin {role} child process failed: {detail}"
            )
            for note in notes:
                wrapped.add_note(note)
            raise wrapped from exc


def _record_approved_counts(approved_snapshot) -> None:
    if not runtime_audit_enabled():
        return
    counts = approved_snapshot.count_reconciliation
    record_runtime_audit_event(
        "approved_count_reconciliation",
        {
            "recognizable_book_count": counts.recognizable_book_count,
            "rejected_book_count": counts.rejected_book_count,
            "excluded_book_count": counts.excluded_book_count,
            "accepted_ordinary_spectrum_count": (
                counts.accepted_ordinary_spectrum_count
            ),
            "output_plan_spectrum_count": (
                counts.output_plan_spectrum_count
            ),
            "output_plan_column_count": counts.output_plan_column_count,
        },
    )
    record_runtime_audit_event(
        "approved_report_ledger",
        {
            "approved_snapshot_id": approved_snapshot.snapshot_id,
            "recognized_source_paths": [
                str(source.snapshot.path)
                for source in approved_snapshot.approved_sources
            ],
            "sections": {
                title: list(entries)
                for title, entries in approved_output_report_ledger(
                    approved_snapshot
                ).items()
            },
        },
    )


def _record_worker_target_attempt(role: str, command, attempt: int) -> None:
    if not runtime_audit_enabled():
        return
    targets = _worker_target_paths(role, command)
    states = []
    for path in targets:
        exists = lexical_path_exists(path)
        states.append(
            {
                "path": str(path),
                "existed_before": exists,
                "identity": (
                    runtime_audit_file_identity(path) if exists else None
                ),
            }
        )
    record_runtime_audit_event(
        "origin_worker_target_attempt",
        {
            "role": role,
            "attempt": attempt,
            "approved_snapshot_id": command.approved_snapshot_id,
            "run_staging_root": str(command.run_staging_root),
            "target_states": states,
        },
    )


def _record_worker_retry_cleanup(
    role: str,
    command,
    attempt: int,
    *,
    artifact_path: Path,
    artifact_identity: tuple[int, int] | None,
    completed: bool,
    error: Exception | None = None,
) -> None:
    if not runtime_audit_enabled():
        return
    record_runtime_audit_event(
        "origin_worker_retry_cleanup",
        {
            "role": role,
            "attempt": attempt,
            "approved_snapshot_id": command.approved_snapshot_id,
            "run_staging_root": str(command.run_staging_root),
            "artifact_path": str(artifact_path),
            "artifact_identity": (
                None
                if artifact_identity is None
                else {
                    "path": str(artifact_path),
                    "device_id": artifact_identity[0],
                    "file_id": artifact_identity[1],
                }
            ),
            "completed": completed,
            "error": None if error is None else str(error),
        },
    )


def _worker_target_paths(role: str, command) -> tuple[Path, ...]:
    if role == "output":
        return (Path(command.staging_project_path),)
    return (
        Path(command.staged_project_path),
        Path(command.mutation_copy_path),
    )


def _run_output_child(
    command: OutputContractWorkerCommand,
) -> ProjectArtifactEvidence:
    return run_output_contract_worker(
        command,
        process_preflight=lambda: None,
    )


def _run_verifier_child(
    command: VerifierWorkerCommand,
) -> tuple[ProjectArtifactEvidence, tuple[int, int]]:
    cleanup_identities = []
    project_artifact = run_verifier_worker(
        command,
        process_preflight=lambda: None,
        cleanup_identity_callback=cleanup_identities.append,
    )
    if len(cleanup_identities) != 1:
        raise OriginChildProcessError(
            "Origin verifier did not attest one cleanup artifact identity"
        )
    return project_artifact, cleanup_identities[0]


def _output_command_from_targets(snapshot, targets):
    from spectrum_organizer.origin.output_worker import OutputWorkerCommand

    return OutputWorkerCommand(
        approved_snapshot_id=snapshot.snapshot_id,
        approved_output_model=snapshot.output_plan,
        staging_project_path=targets.staging_project_path,
        run_staging_root=targets.staging_dir,
        run_staging_identity=targets.staging_identity,
        allowed_output_targets=(targets.staging_project_path,),
        worker_role="output",
        staging_project_identity=None,
    )


def _verifier_command_from_request(request) -> VerifierWorkerCommand:
    protected_sources = tuple(
        getattr(
            request.approved_snapshot,
            "selected_source_fingerprints_before",
            (),
        )
        or request.approved_snapshot.source_fingerprints_before
    )
    return VerifierWorkerCommand(
        approved_snapshot_id=request.approved_snapshot.snapshot_id,
        staged_project_path=request.targets.staging_project_path,
        mutation_copy_path=request.targets.verifier_mutation_path,
        run_staging_root=request.targets.staging_dir,
        run_staging_identity=request.targets.staging_identity,
        allowed_open_targets=(
            request.targets.staging_project_path,
            request.targets.verifier_mutation_path,
        ),
        protected_paths=tuple(
            item.path
            for item in protected_sources
        ),
        expected_contract=request.expected_contract,
        expected_project_artifact=getattr(
            request,
            "expected_project_artifact",
            None,
        ),
        worker_role="verifier",
        mutation_copy_identity=None,
    )


def _output_payload(snapshot_id, contract, command) -> dict[str, object]:
    return {
        "approved_snapshot_id": snapshot_id,
        "contract": project_contract_to_payload(contract),
        "staging_project_path": str(command.staging_project_path),
        "staging_project_identity": list(command.staging_project_identity),
        "run_staging_root": str(command.run_staging_root),
        "run_staging_identity": list(command.run_staging_identity),
        "allowed_output_targets": [
            str(path) for path in command.allowed_output_targets
        ],
        "attempt": command.attempt,
    }


def _verifier_payload(command: VerifierWorkerCommand) -> dict[str, object]:
    return {
        "approved_snapshot_id": command.approved_snapshot_id,
        "contract": project_contract_to_payload(command.expected_contract),
        "staged_project_path": str(command.staged_project_path),
        "mutation_copy_path": str(command.mutation_copy_path),
        "mutation_copy_identity": list(command.mutation_copy_identity),
        "run_staging_root": str(command.run_staging_root),
        "run_staging_identity": list(command.run_staging_identity),
        "allowed_open_targets": [
            str(path) for path in command.allowed_open_targets
        ],
        "protected_paths": [str(path) for path in command.protected_paths],
        "expected_project_artifact": _project_artifact_to_payload(
            command.expected_project_artifact
        ),
        "attempt": command.attempt,
    }


def _output_command_from_payload(payload: object) -> OutputContractWorkerCommand:
    _require_fields(
        payload,
        {
            "approved_snapshot_id",
            "contract",
            "staging_project_path",
            "staging_project_identity",
            "run_staging_root",
            "run_staging_identity",
            "allowed_output_targets",
            "attempt",
        },
        "output payload",
    )
    return OutputContractWorkerCommand(
        approved_snapshot_id=_required_text(
            payload["approved_snapshot_id"],
            "approved_snapshot_id",
        ),
        approved_contract=project_contract_from_payload(payload["contract"]),
        staging_project_path=Path(
            _required_text(payload["staging_project_path"], "staging_project_path")
        ),
        run_staging_root=Path(
            _required_text(payload["run_staging_root"], "run_staging_root")
        ),
        run_staging_identity=_identity_from_payload(
            payload["run_staging_identity"],
            "run_staging_identity",
        ),
        allowed_output_targets=tuple(
            Path(_required_text(path, "allowed_output_target"))
            for path in _required_list(
                payload["allowed_output_targets"],
                "allowed_output_targets",
            )
        ),
        worker_role="output",
        staging_project_identity=_identity_from_payload(
            payload["staging_project_identity"],
            "staging_project_identity",
        ),
        attempt=_attempt_from_payload(payload["attempt"]),
    )


def _verifier_command_from_payload(payload: object) -> VerifierWorkerCommand:
    _require_fields(
        payload,
        {
            "approved_snapshot_id",
            "contract",
            "staged_project_path",
            "mutation_copy_path",
            "mutation_copy_identity",
            "run_staging_root",
            "run_staging_identity",
            "allowed_open_targets",
            "protected_paths",
            "expected_project_artifact",
            "attempt",
        },
        "verifier payload",
    )
    return VerifierWorkerCommand(
        approved_snapshot_id=_required_text(
            payload["approved_snapshot_id"],
            "approved_snapshot_id",
        ),
        staged_project_path=Path(
            _required_text(payload["staged_project_path"], "staged_project_path")
        ),
        mutation_copy_path=Path(
            _required_text(payload["mutation_copy_path"], "mutation_copy_path")
        ),
        run_staging_root=Path(
            _required_text(payload["run_staging_root"], "run_staging_root")
        ),
        run_staging_identity=_identity_from_payload(
            payload["run_staging_identity"],
            "run_staging_identity",
        ),
        allowed_open_targets=tuple(
            Path(_required_text(path, "allowed_open_target"))
            for path in _required_list(
                payload["allowed_open_targets"],
                "allowed_open_targets",
            )
        ),
        protected_paths=tuple(
            Path(_required_text(path, "protected_path"))
            for path in _required_list(
                payload["protected_paths"],
                "protected_paths",
            )
        ),
        expected_contract=project_contract_from_payload(payload["contract"]),
        expected_project_artifact=_project_artifact_from_payload(
            payload["expected_project_artifact"]
        ),
        worker_role="verifier",
        mutation_copy_identity=_identity_from_payload(
            payload["mutation_copy_identity"],
            "mutation_copy_identity",
        ),
        attempt=_attempt_from_payload(payload["attempt"]),
    )


def _raise_child_failure(result: object, role: str) -> dict[str, object]:
    if not isinstance(result, dict):
        raise OriginChildProcessError(
            f"Origin {role} child returned an invalid result"
        )
    if result.get("ok") is True:
        return result
    cleanup_identity = _cleanup_artifact_identity_from_result(result)
    message = str(result.get("error") or f"Origin {role} child failed")
    retryable = result.get("classification") == "retry_once_later"
    if role == "output":
        error_type = InfrastructureOutputError if retryable else DeterministicOutputError
    else:
        if (
            not retryable
            and result.get("error_type") == "VerificationMismatchError"
        ):
            error = VerificationMismatchError(
                message,
                _mismatch_report_from_payload(
                    result.get("mismatch_report")
                ),
            )
            if cleanup_identity is not None:
                error.owned_artifact_identity = cleanup_identity
            _restore_error_notes(error, result)
            raise error
        error_type = (
            InfrastructureVerificationError
            if retryable
            else DeterministicVerificationError
        )
    error = error_type(message)
    if cleanup_identity is not None:
        error.owned_artifact_identity = cleanup_identity
    _restore_error_notes(error, result)
    raise error


def _project_artifact_to_payload(
    artifact: ProjectArtifactEvidence,
) -> dict[str, object]:
    if not isinstance(artifact, ProjectArtifactEvidence):
        raise ValueError("project artifact evidence is invalid")
    return {
        "identity": list(artifact.identity),
        "sha256": artifact.sha256,
        "size": artifact.size,
    }


def _project_artifact_from_payload(
    payload: object,
) -> ProjectArtifactEvidence:
    _require_fields(
        payload,
        {"identity", "sha256", "size"},
        "project_artifact",
    )
    identity = _identity_from_payload(
        payload["identity"],
        "project_artifact.identity",
    )
    if (
        isinstance(payload["size"], bool)
        or not isinstance(payload["size"], int)
        or payload["size"] < 0
    ):
        raise ValueError("project artifact identity or size is invalid")
    sha256 = _required_text(payload["sha256"], "project_artifact.sha256")
    if len(sha256) != 64 or any(character not in "0123456789abcdef" for character in sha256):
        raise ValueError("project artifact sha256 is invalid")
    return ProjectArtifactEvidence(
        identity=identity,
        sha256=sha256,
        size=payload["size"],
    )


def _identity_from_payload(
    payload: object,
    label: str,
) -> tuple[int, int]:
    identity = _required_list(payload, label)
    if (
        len(identity) != 2
        or any(
            isinstance(value, bool) or not isinstance(value, int)
            for value in identity
        )
    ):
        raise ValueError(f"{label} is invalid")
    return identity[0], identity[1]


def _attempt_from_payload(value: object) -> int:
    if type(value) is not int or value not in {1, 2}:
        raise ValueError("worker attempt is invalid")
    return value


def _mismatch_report_to_payload(
    report: MismatchReport | None,
) -> dict[str, object] | None:
    if report is None:
        return None
    return {
        "structural_path": report.structural_path,
        "column": report.column,
        "row": report.row,
        "expected": _json_mismatch_value(report.expected),
        "actual": _json_mismatch_value(report.actual),
        "mismatch_class": report.mismatch_class,
    }


def _mismatch_report_from_payload(
    payload: object,
) -> MismatchReport | None:
    if payload is None:
        return None
    _require_fields(
        payload,
        {
            "structural_path",
            "column",
            "row",
            "expected",
            "actual",
            "mismatch_class",
        },
        "mismatch_report",
    )
    mismatch_class = _required_text(
        payload["mismatch_class"],
        "mismatch_class",
    )
    return MismatchReport(
        _required_text(
            payload["structural_path"],
            "structural_path",
        ),
        _optional_text(payload["column"], "column"),
        _optional_row(payload["row"]),
        _mismatch_value_from_payload(
            payload["expected"],
            mismatch_class,
        ),
        _mismatch_value_from_payload(
            payload["actual"],
            mismatch_class,
        ),
        mismatch_class,
    )


def _json_mismatch_value(value: object) -> object:
    return str(value) if isinstance(value, Decimal) else value


def _mismatch_value_from_payload(
    value: object,
    mismatch_class: str,
) -> object:
    if mismatch_class == "metadata" and isinstance(value, list):
        return tuple(value)
    if mismatch_class in {"numeric", "finite"} and isinstance(value, str):
        try:
            return Decimal(value)
        except InvalidOperation:
            return value
    return value


def _restore_error_notes(error: BaseException, result: dict[str, object]) -> None:
    notes = result.get("error_notes", ())
    if not isinstance(notes, (list, tuple)):
        return
    for note in notes:
        if isinstance(note, str):
            error.add_note(note)


def _optional_row(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise OriginChildProcessError("row must be a non-negative integer")
    return value


def _origin_process_command(role: str) -> list[str]:
    if getattr(sys, "frozen", False):
        flag = {
            "output": "--origin-output-worker",
            "verifier": "--origin-verifier-worker",
        }.get(role)
        if flag is None:
            raise ValueError(f"Unsupported Origin child role: {role}")
        return [sys.executable, flag]
    return [
        sys.executable,
        "-m",
        "spectrum_organizer.origin.output_process",
        role,
    ]


def _required_list(value: object, name: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a list")
    return value


def _require_fields(value: object, fields: set[str], name: str) -> None:
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError(f"{name} fields are invalid")


def _required_text(
    value: object,
    name: str,
    *,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise ValueError(f"{name} must be text")
    return value


def _optional_text(value: object, name: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, name, allow_empty=True)


def _decimal_or_none(value: object) -> Decimal | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("contract numeric values must be decimal strings")
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("contract numeric value is invalid") from exc


if __name__ == "__main__":
    raise SystemExit(output_process_main())
