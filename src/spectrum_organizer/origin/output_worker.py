from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import math
import os
from pathlib import Path
import re

from spectrum_organizer.core.output_model import OutputPlan
from spectrum_organizer.origin.contracts import (
    BookWriteContract,
    ColumnWriteContract,
    FolderWriteContract,
    ProjectArtifactEvidence,
    ProjectWriteContract,
)
from spectrum_organizer.origin.process_identity import (
    record_origin_session_identity,
)
from spectrum_organizer.safety.identity_paths import (
    file_sha256,
    hold_directory_identity,
    hold_file_identity,
    IdentityPathError,
    path_identity,
)


_ORIGIN_MODULE_NAME = "originpro"
_OUTPUT_NAME = re.compile(r"^Organized_Spectra_\d{8}_\d{6}\.opju$")


class OutputWorkerPreflightError(RuntimeError):
    pass


class InfrastructureOutputError(RuntimeError):
    pass


class DeterministicOutputError(RuntimeError):
    pass


@dataclass(frozen=True)
class RetryAttempt:
    attempt: int
    status: str
    message: str


class OutputInfrastructureFailure(InfrastructureOutputError):
    def __init__(self, attempts: tuple[RetryAttempt, ...]):
        super().__init__("output worker infrastructure failed after retry")
        self.attempts = attempts


@dataclass(frozen=True)
class OutputRetryResult:
    contract: "ProjectWriteContract"
    attempts: tuple[RetryAttempt, ...]
    project_artifact: ProjectArtifactEvidence | None = None


@dataclass(frozen=True)
class OutputWorkerCommand:
    approved_snapshot_id: str
    approved_output_model: OutputPlan
    staging_project_path: Path
    run_staging_root: Path
    run_staging_identity: tuple[int, int]
    allowed_output_targets: tuple[Path, ...]
    worker_role: str
    staging_project_identity: tuple[int, int] | None = None
    attempt: int = 1


@dataclass(frozen=True)
class OutputContractWorkerCommand:
    approved_snapshot_id: str
    approved_contract: "ProjectWriteContract"
    staging_project_path: Path
    run_staging_root: Path
    run_staging_identity: tuple[int, int]
    allowed_output_targets: tuple[Path, ...]
    worker_role: str
    staging_project_identity: tuple[int, int] | None = None
    attempt: int = 1


def run_output_worker(command: OutputWorkerCommand, *, process_preflight, origin_loader=None) -> ProjectWriteContract:
    process_preflight()
    validate_output_command(command)
    try:
        with hold_directory_identity(
            command.run_staging_root,
            command.run_staging_identity,
        ):
            contract = build_project_write_contract(command.approved_output_model)
            _write_contract_with_session(
                contract,
                command.staging_project_path,
                origin_loader,
                expected_identity=command.staging_project_identity,
                attempt_binding=_output_attempt_binding(command),
            )
    except (IdentityPathError, OSError) as exc:
        raise OutputWorkerPreflightError(str(exc)) from exc
    return contract


def run_output_contract_worker(
    command: OutputContractWorkerCommand,
    *,
    process_preflight,
    origin_loader=None,
) -> ProjectArtifactEvidence:
    process_preflight()
    validate_output_contract_command(command)
    try:
        with hold_directory_identity(
            command.run_staging_root,
            command.run_staging_identity,
        ):
            return _write_contract_with_session(
                command.approved_contract,
                command.staging_project_path,
                origin_loader,
                expected_identity=command.staging_project_identity,
                attempt_binding=_output_attempt_binding(command),
            )
    except (IdentityPathError, OSError) as exc:
        raise OutputWorkerPreflightError(str(exc)) from exc


def run_output_with_infrastructure_retry(command: OutputWorkerCommand, worker_factory, cleanup_attempt) -> OutputRetryResult:
    attempts: list[RetryAttempt] = []
    for attempt_number in (1, 2):
        try:
            worker = worker_factory(attempt_number)
            contract = worker(command)
        except InfrastructureOutputError as exc:
            attempts.append(RetryAttempt(attempt_number, "infrastructure_failed", str(exc)))
            try:
                cleanup_attempt(attempt_number)
            except Exception as cleanup_exc:
                failure = OutputInfrastructureFailure(tuple(attempts))
                owned_identity = getattr(
                    cleanup_exc,
                    "owned_artifact_identity",
                    getattr(exc, "owned_artifact_identity", None),
                )
                if owned_identity is not None:
                    failure.owned_artifact_identity = owned_identity
                failure.add_note(
                    f"cleanup attempt {attempt_number} also failed: {cleanup_exc}"
                )
                raise failure from exc
            if attempt_number == 2:
                raise OutputInfrastructureFailure(tuple(attempts)) from exc
            continue
        attempts.append(RetryAttempt(attempt_number, "succeeded", ""))
        return OutputRetryResult(contract, tuple(attempts))
    raise AssertionError("unreachable output retry loop")


def validate_output_command(command: OutputWorkerCommand) -> None:
    if command.worker_role != "output":
        raise OutputWorkerPreflightError("originpro may only be used by an output worker")
    if not command.approved_snapshot_id:
        raise OutputWorkerPreflightError("approved snapshot id is required before output creation")
    if command.attempt not in {1, 2}:
        raise OutputWorkerPreflightError("output worker attempt is invalid")
    _validate_reserved_staging_target(
        command.staging_project_path,
        command.run_staging_root,
        command.run_staging_identity,
        command.allowed_output_targets,
        command.staging_project_identity,
    )


def validate_output_contract_command(
    command: OutputContractWorkerCommand,
) -> None:
    if command.worker_role != "output":
        raise OutputWorkerPreflightError(
            "originpro may only be used by an output worker"
        )
    if not command.approved_snapshot_id:
        raise OutputWorkerPreflightError(
            "approved snapshot id is required before output creation"
        )
    if command.attempt not in {1, 2}:
        raise OutputWorkerPreflightError("output worker attempt is invalid")
    _validate_reserved_staging_target(
        command.staging_project_path,
        command.run_staging_root,
        command.run_staging_identity,
        command.allowed_output_targets,
        command.staging_project_identity,
    )


def build_project_write_contract(output_model: OutputPlan) -> ProjectWriteContract:
    folders = []
    for folder in output_model.folders:
        books = []
        for book in folder.books:
            columns = []
            unmatched_raw_columns: list[ColumnWriteContract] = []
            for index, column in enumerate(book.columns, start=1):
                short_name = _column_letter(index)
                designation = "X" if column.kind == "x" else "Y"
                formula = None
                method = column.method
                values = _origin_roundtrip_values(column.values)
                if column.kind == "norm_y":
                    raw_column = _raw_column_for_norm(
                        index,
                        unmatched_raw_columns,
                    )
                    raw_short_name = raw_column.short_name
                    formula = f"col({raw_short_name})/max(col({raw_short_name}))"
                    method = method or f"Divided by Max of {raw_short_name}"
                    values = _origin_normalized_values(
                        raw_column.values
                    )
                contract_column = ColumnWriteContract(
                    short_name=short_name,
                    designation=designation,
                    comment=column.comment,
                    values=values,
                    formula=formula,
                    method=method,
                )
                columns.append(contract_column)
                if column.kind == "raw_y":
                    unmatched_raw_columns.append(contract_column)
            books.append(BookWriteContract(book.display_name, None, tuple(columns)))
        folders.append(FolderWriteContract(folder.name, tuple(books)))
    return ProjectWriteContract("/", tuple(folders))


def classify_output_error(exc: BaseException) -> str:
    if isinstance(exc, InfrastructureOutputError):
        return "retry_once_later"
    return "non_retryable"


def _write_project(
    session,
    contract: ProjectWriteContract,
    staging_project_path: Path,
    *,
    expected_identity: tuple[int, int] | None,
) -> ProjectArtifactEvidence:
    target = Path(staging_project_path)
    identity = expected_identity
    if identity is None:
        raise OutputWorkerPreflightError(
            "Output staging project reservation identity is missing"
        )
    try:
        with hold_file_identity(target, identity, allow_write=True):
            if target.stat().st_size != 0:
                raise DeterministicOutputError(
                    f"Reserved output staging project is not empty: {target}"
                )
            session.new()
            session.delete_default_template_book()
            root_path = session.root_folder_path()
            for folder in contract.folders:
                folder_handle = session.add_folder(root_path, folder.path)
                for book in folder.books:
                    book_handle = session.add_book(
                        folder_handle,
                        book.display_long_name,
                    )
                    for column in book.columns:
                        session.write_column(book_handle, column)
                        if (
                            column.method is not None
                            and session.method_row(column.short_name)
                            != column.method
                        ):
                            raise DeterministicOutputError(
                                "Method row mismatch for column "
                                f"{column.short_name}"
                            )
            session.save(target)
            if path_identity(target) != identity:
                raise DeterministicOutputError(
                    f"Output staging project identity changed during save: {target}"
                )
            return ProjectArtifactEvidence(
                identity=identity,
                sha256=file_sha256(target),
                size=target.stat().st_size,
            )
    except IdentityPathError as exc:
        raise DeterministicOutputError(str(exc)) from exc
    except BaseException as exc:
        if identity is not None:
            setattr(exc, "owned_artifact_identity", identity)
        raise


def _write_contract_with_session(
    contract: ProjectWriteContract,
    staging_project_path: Path,
    origin_loader,
    *,
    expected_identity: tuple[int, int] | None,
    attempt_binding: dict[str, object],
) -> ProjectArtifactEvidence:
    try:
        session = (origin_loader or _load_origin_session)()
    except InfrastructureOutputError:
        raise
    except Exception as exc:
        raise InfrastructureOutputError(
            f"Origin output session launch failed: {exc}"
        ) from exc
    pending_error = None
    completed_artifact = None
    try:
        record_origin_session_identity(
            getattr(session, "origin", None),
            role="output",
            attempt_binding=attempt_binding,
        )
        completed_artifact = _write_project(
            session,
            contract,
            staging_project_path,
            expected_identity=expected_identity,
        )
        return completed_artifact
    except (InfrastructureOutputError, DeterministicOutputError) as exc:
        pending_error = exc
        raise
    except Exception as exc:
        pending_error = InfrastructureOutputError(
            f"Origin output communication failed: {exc}"
        )
        owned_identity = getattr(exc, "owned_artifact_identity", None)
        if owned_identity is not None:
            setattr(
                pending_error,
                "owned_artifact_identity",
                owned_identity,
            )
        raise pending_error from exc
    finally:
        close = getattr(session, "close", None)
        if callable(close):
            try:
                close()
            except Exception as exc:
                if pending_error is not None:
                    pending_error.add_note(
                        f"Origin session close also failed: {exc}"
                    )
                else:
                    close_error = InfrastructureOutputError(
                        f"Origin output session close failed: {exc}"
                    )
                    if completed_artifact is not None:
                        setattr(
                            close_error,
                            "owned_artifact_identity",
                            completed_artifact.identity,
                        )
                    raise close_error from exc


def _output_attempt_binding(command) -> dict[str, object]:
    return {
        "approved_snapshot_id": command.approved_snapshot_id,
        "run_staging_root": str(command.run_staging_root),
        "attempt": command.attempt,
    }


def _load_origin_session():
    from spectrum_organizer.origin.session_adapters import OriginOutputSession

    return OriginOutputSession(__import__(_ORIGIN_MODULE_NAME))


def _validate_reserved_staging_target(
    path: Path,
    run_staging_root: Path,
    run_staging_identity: tuple[int, int],
    allowed_output_targets: tuple[Path, ...],
    expected_identity: tuple[int, int] | None,
) -> None:
    resolved = Path(path)
    if not _OUTPUT_NAME.match(resolved.name):
        raise OutputWorkerPreflightError(f"Output staging project has invalid name: {path}")
    if expected_identity is None:
        raise OutputWorkerPreflightError(
            "Output staging project reservation identity is missing"
        )
    try:
        if path_identity(resolved) != expected_identity:
            raise OutputWorkerPreflightError(
                f"Output staging project reservation identity changed: {path}"
            )
        if resolved.stat().st_size != 0:
            raise OutputWorkerPreflightError(
                f"Output staging project reservation is not empty: {path}"
            )
    except IdentityPathError as exc:
        raise OutputWorkerPreflightError(str(exc)) from exc
    root = Path(run_staging_root)
    try:
        if (
            root.is_symlink()
            or not root.is_dir()
            or path_identity(root) != run_staging_identity
        ):
            raise OutputWorkerPreflightError(
                f"Output staging root identity changed: {root}"
            )
    except IdentityPathError as exc:
        raise OutputWorkerPreflightError(str(exc)) from exc
    _validate_open_target(resolved, run_staging_root, allowed_output_targets)


def _validate_open_target(path: Path, run_staging_root: Path, allowed_output_targets: tuple[Path, ...]) -> None:
    if _path_key(path) not in {_path_key(item) for item in allowed_output_targets}:
        raise OutputWorkerPreflightError(f"Origin output target is not allowlisted: {path}")
    if not _is_under(path, run_staging_root):
        raise OutputWorkerPreflightError(f"Origin open target is outside run-owned staging: {path}")


def _raw_column_for_norm(
    index: int,
    unmatched_raw_columns: list[ColumnWriteContract],
) -> ColumnWriteContract:
    if not unmatched_raw_columns:
        raise DeterministicOutputError(f"Norm column has no matching Raw column at position {index}")
    return unmatched_raw_columns.pop(0)


def _origin_roundtrip_values(
    values: tuple[Decimal | None, ...],
) -> tuple[Decimal | None, ...]:
    converted = []
    for value in values:
        if value is None:
            converted.append(None)
            continue
        try:
            origin_value = float(value)
        except (TypeError, ValueError, OverflowError):
            raise DeterministicOutputError(
                "Output value is outside Origin finite numeric range"
            ) from None
        if not math.isfinite(origin_value):
            raise DeterministicOutputError(
                "Output value is outside Origin finite numeric range"
            )
        converted.append(Decimal(str(origin_value)))
    return tuple(converted)


def _origin_normalized_values(
    raw_values: tuple[Decimal | None, ...],
) -> tuple[Decimal | None, ...]:
    finite_values = [
        float(value)
        for value in raw_values
        if value is not None
    ]
    maximum = max(finite_values)
    return tuple(
        None
        if value is None
        else Decimal(str(float(value) / maximum))
        for value in raw_values
    )


def _column_letter(position: int) -> str:
    letters = ""
    while position:
        position, remainder = divmod(position - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


def _path_key(path: Path) -> str:
    return os.path.normcase(str(Path(path).resolve()))


def _is_under(path: Path, parent: Path) -> bool:
    resolved_path = Path(path).resolve()
    resolved_parent = Path(parent).resolve()
    return resolved_path == resolved_parent or resolved_parent in resolved_path.parents
