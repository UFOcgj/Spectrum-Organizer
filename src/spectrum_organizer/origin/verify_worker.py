from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import re

from spectrum_organizer.origin.contracts import (
    AUTOMATIC_FORMULA_LOCK_STATE,
    OriginStructureMismatchError,
    ProjectArtifactEvidence,
    ProjectWriteContract,
)
from spectrum_organizer.origin.process_identity import (
    record_origin_session_identity,
)
from spectrum_organizer.safety.identity_paths import (
    file_sha256,
    hold_directory_identity,
    IdentityPathError,
    hold_file_identity,
    path_identity,
)


_ORIGIN_MODULE_NAME = "originpro"
_ORIGIN_SHORT_NAME_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_]*\Z")


class VerifierWorkerPreflightError(RuntimeError):
    pass


class InfrastructureVerificationError(RuntimeError):
    pass


class DeterministicVerificationError(RuntimeError):
    pass


class VerificationMismatchError(DeterministicVerificationError):
    def __init__(self, message: str, report: "MismatchReport | None"):
        detail = f": {format_mismatch_report(report)}" if report else ""
        super().__init__(f"{message}{detail}")
        self.report = report


@dataclass(frozen=True)
class RetryAttempt:
    attempt: int
    status: str
    message: str


class VerifierInfrastructureFailure(InfrastructureVerificationError):
    def __init__(self, attempts: tuple[RetryAttempt, ...]):
        super().__init__("verifier worker infrastructure failed after retry")
        self.attempts = attempts


@dataclass(frozen=True)
class VerifierRetryResult:
    attempts: tuple[RetryAttempt, ...]
    readback_spectrum_count: int
    readback_column_count: int
    verified_project_artifact: ProjectArtifactEvidence | None = None


@dataclass(frozen=True)
class VerifierWorkerCommand:
    approved_snapshot_id: str
    staged_project_path: Path
    mutation_copy_path: Path
    run_staging_root: Path
    run_staging_identity: tuple[int, int]
    allowed_open_targets: tuple[Path, ...]
    protected_paths: tuple[Path, ...]
    expected_contract: ProjectWriteContract
    expected_project_artifact: ProjectArtifactEvidence
    worker_role: str
    mutation_copy_identity: tuple[int, int] | None = None
    attempt: int = 1


@dataclass(frozen=True)
class MismatchReport:
    structural_path: str
    column: str | None
    row: int | None
    expected: object
    actual: object
    mismatch_class: str


def format_mismatch_report(report: MismatchReport) -> str:
    return (
        f"path={report.structural_path}; column={report.column}; "
        f"row={report.row}; class={report.mismatch_class}; "
        f"expected={report.expected!r}; actual={report.actual!r}"
    )


def run_verifier_worker(
    command: VerifierWorkerCommand,
    *,
    process_preflight,
    dependency_proof=None,
    origin_loader=None,
    cleanup_identity_callback=None,
) -> ProjectArtifactEvidence:
    process_preflight()
    validate_verifier_command(command)
    try:
        session = (origin_loader or _load_origin_session)()
    except InfrastructureVerificationError:
        raise
    except Exception as exc:
        raise InfrastructureVerificationError(
            f"Origin verifier session launch failed: {exc}"
        ) from exc
    pending_error = None
    mutation_identity = None
    try:
        record_origin_session_identity(
            getattr(session, "origin", None),
            role="verifier",
            attempt_binding={
                "approved_snapshot_id": command.approved_snapshot_id,
                "run_staging_root": str(command.run_staging_root),
                "attempt": command.attempt,
            },
        )
        if dependency_proof is None:
            from spectrum_organizer.origin.session_adapters import (
                OriginDependencyProof,
            )

            dependency_proof = OriginDependencyProof(session.origin)
        staged = Path(command.staged_project_path)
        expected_artifact = command.expected_project_artifact
        with (
            hold_directory_identity(
                command.run_staging_root,
                command.run_staging_identity,
            ),
            hold_file_identity(
                staged,
                expected_artifact.identity,
                allow_write=False,
            ),
        ):
            _require_project_artifact(
                staged,
                expected_artifact,
                "before verifier open",
            )
            session.open(staged, True)
            actual = session.read_project_contract()
            report = compare_project_contract(command.expected_contract, actual)
            if report is not None:
                raise VerificationMismatchError(
                    "Origin output verification mismatch",
                    report,
                )
            try:
                mutation_identity = prove_live_dependency_on_mutation_copy(
                    command,
                    dependency_proof,
                    cleanup_identity_callback=cleanup_identity_callback,
                )
            except (
                InfrastructureVerificationError,
                DeterministicVerificationError,
            ):
                raise
            except Exception as exc:
                raise DeterministicVerificationError(
                    "Verifier dependency proof failed after comparison"
                ) from exc
            verified_artifact = _project_artifact(staged)
            _require_project_artifact(
                staged,
                expected_artifact,
                "after verifier readback",
            )
    except OriginStructureMismatchError as exc:
        pending_error = DeterministicVerificationError(str(exc))
        raise pending_error from exc
    except (
        InfrastructureVerificationError,
        DeterministicVerificationError,
    ) as exc:
        pending_error = exc
        if mutation_identity is not None:
            setattr(
                pending_error,
                "owned_artifact_identity",
                mutation_identity,
            )
        raise
    except Exception as exc:
        pending_error = InfrastructureVerificationError(
            f"Origin verifier communication failed: {exc}"
        )
        owned_identity = getattr(
            exc,
            "owned_artifact_identity",
            mutation_identity,
        )
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
                    close_error = InfrastructureVerificationError(
                        f"Origin verifier session close failed: {exc}"
                    )
                    if mutation_identity is not None:
                        setattr(
                            close_error,
                            "owned_artifact_identity",
                            mutation_identity,
                        )
                    raise close_error from exc
    return verified_artifact


def run_verifier_with_infrastructure_retry(
    command: VerifierWorkerCommand,
    verifier_factory,
    cleanup_attempt=lambda _attempt: None,
) -> VerifierRetryResult:
    attempts: list[RetryAttempt] = []
    for attempt_number in (1, 2):
        try:
            verifier = verifier_factory(attempt_number)
            verifier_result = verifier(command)
        except InfrastructureVerificationError as exc:
            attempts.append(RetryAttempt(attempt_number, "infrastructure_failed", str(exc)))
            try:
                cleanup_attempt(attempt_number)
            except Exception as cleanup_exc:
                failure = VerifierInfrastructureFailure(tuple(attempts))
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
                raise VerifierInfrastructureFailure(tuple(attempts)) from exc
            continue
        attempts.append(RetryAttempt(attempt_number, "succeeded", ""))
        return VerifierRetryResult(
            tuple(attempts),
            len(_raw_norm_pairs(command.expected_contract)),
            sum(
                len(book.columns)
                for folder in command.expected_contract.folders
                for book in folder.books
            ),
            verified_project_artifact=(
                verifier_result
                if isinstance(verifier_result, ProjectArtifactEvidence)
                else None
            ),
        )
    raise AssertionError("unreachable verifier retry loop")


def validate_verifier_command(command: VerifierWorkerCommand) -> None:
    if command.worker_role != "verifier":
        raise VerifierWorkerPreflightError("originpro may only be used by a verifier worker")
    if not command.approved_snapshot_id:
        raise VerifierWorkerPreflightError("approved snapshot id is required before verification")
    if not isinstance(
        command.expected_project_artifact,
        ProjectArtifactEvidence,
    ):
        raise VerifierWorkerPreflightError(
            "expected staged project artifact evidence is required"
        )
    if command.attempt not in {1, 2}:
        raise VerifierWorkerPreflightError("verifier worker attempt is invalid")
    try:
        if path_identity(command.run_staging_root) != command.run_staging_identity:
            raise VerifierWorkerPreflightError(
                "verifier staging root identity changed"
            )
    except IdentityPathError as exc:
        raise VerifierWorkerPreflightError(str(exc)) from exc
    _validate_open_target(command.staged_project_path, command.run_staging_root, command.allowed_open_targets, command.protected_paths)
    if not Path(command.staged_project_path).exists():
        raise VerifierWorkerPreflightError(f"Staged project does not exist for verification: {command.staged_project_path}")
    _validate_open_target(command.mutation_copy_path, command.run_staging_root, command.allowed_open_targets, command.protected_paths)
    if command.mutation_copy_identity is None:
        raise VerifierWorkerPreflightError(
            "Verifier mutation reservation identity is missing"
        )
    try:
        if (
            path_identity(command.mutation_copy_path)
            != command.mutation_copy_identity
            or Path(command.mutation_copy_path).stat().st_size != 0
        ):
            raise VerifierWorkerPreflightError(
                "Verifier mutation reservation changed"
            )
    except IdentityPathError as exc:
        raise VerifierWorkerPreflightError(str(exc)) from exc


def compare_project_contract(expected: ProjectWriteContract, actual: ProjectWriteContract) -> MismatchReport | None:
    if expected.root_path != actual.root_path:
        return MismatchReport("root", None, None, expected.root_path, actual.root_path, "structure")
    if len(expected.folders) != len(actual.folders):
        return MismatchReport("/", None, None, len(expected.folders), len(actual.folders), "structure")
    expected_folders = tuple(
        sorted(expected.folders, key=lambda folder: folder.path)
    )
    actual_folders = tuple(
        sorted(actual.folders, key=lambda folder: folder.path)
    )
    for folder_index, expected_folder in enumerate(expected_folders):
        actual_folder = actual_folders[folder_index]
        if expected_folder.path != actual_folder.path:
            return MismatchReport(expected_folder.path, None, None, expected_folder.path, actual_folder.path, "structure")
        if len(expected_folder.books) != len(actual_folder.books):
            return MismatchReport(expected_folder.path, None, None, len(expected_folder.books), len(actual_folder.books), "structure")
        expected_books = _books_by_display_identity(expected_folder.books)
        actual_books = _books_by_display_identity(actual_folder.books)
        for book_index, expected_book in enumerate(expected_books):
            actual_book = actual_books[book_index]
            structural_path = f"{expected_folder.path}/{expected_book.display_long_name}"
            if expected_book.display_long_name != actual_book.display_long_name:
                return MismatchReport(structural_path, None, None, expected_book.display_long_name, actual_book.display_long_name, "structure")
            if len(expected_book.columns) != len(actual_book.columns):
                return MismatchReport(structural_path, None, None, len(expected_book.columns), len(actual_book.columns), "structure")

    internal_short_name_report = _validate_actual_book_short_names(actual)
    if internal_short_name_report is not None:
        return internal_short_name_report

    for folder_index, expected_folder in enumerate(expected_folders):
        actual_folder = actual_folders[folder_index]
        expected_books = _books_by_display_identity(expected_folder.books)
        actual_books = _books_by_display_identity(actual_folder.books)
        for book_index, expected_book in enumerate(expected_books):
            actual_book = actual_books[book_index]
            structural_path = f"{expected_folder.path}/{expected_book.display_long_name}"
            report = _compare_book(structural_path, expected_book.columns, actual_book.columns)
            if report is not None:
                return report
    return None


def _books_by_display_identity(books):
    return tuple(
        sorted(
            books,
            key=lambda book: book.display_long_name,
        )
    )


def _validate_actual_book_short_names(actual: ProjectWriteContract) -> MismatchReport | None:
    seen: set[str] = set()
    for folder in actual.folders:
        for book in folder.books:
            structural_path = f"{folder.path}/{book.display_long_name}"
            short_name = book.internal_short_name
            if (
                not isinstance(short_name, str)
                or _ORIGIN_SHORT_NAME_PATTERN.fullmatch(short_name) is None
            ):
                return MismatchReport(
                    structural_path,
                    "internal_short_name",
                    None,
                    "ASCII Origin short name beginning with a letter",
                    short_name,
                    "metadata",
                )
            short_name_key = short_name.casefold()
            if short_name_key in seen:
                return MismatchReport(
                    structural_path,
                    "internal_short_name",
                    None,
                    "case-insensitively unique Origin short name",
                    short_name,
                    "metadata",
                )
            seen.add(short_name_key)
    return None

def prove_live_dependency_on_mutation_copy(
    command: VerifierWorkerCommand,
    proof_adapter,
    *,
    cleanup_identity_callback=None,
) -> tuple[int, int]:
    validate_verifier_command(command)
    try:
        with _copy_mutation_exclusive(command) as mutation_identity:
            if cleanup_identity_callback is not None:
                cleanup_identity_callback(mutation_identity)
            try:
                _validate_open_target(
                    command.mutation_copy_path,
                    command.run_staging_root,
                    command.allowed_open_targets,
                    (
                        Path(command.staged_project_path),
                        *command.protected_paths,
                    ),
                )
            except VerifierWorkerPreflightError as exc:
                raise DeterministicVerificationError(str(exc)) from exc
            mutation_copy = Path(command.mutation_copy_path)
            if mutation_copy.is_symlink() or not mutation_copy.is_file():
                raise DeterministicVerificationError(
                    f"Verifier mutation copy is not a regular file: {mutation_copy}"
                )
            if path_identity(mutation_copy) != mutation_identity:
                raise DeterministicVerificationError(
                    f"Verifier mutation copy identity changed: {mutation_copy}"
                )
            proof_adapter.open(mutation_copy, False)
    except IdentityPathError as exc:
        raise DeterministicVerificationError(str(exc)) from exc
    except (InfrastructureVerificationError, DeterministicVerificationError):
        raise
    except Exception as exc:
        raise InfrastructureVerificationError(
            f"Verifier mutation-copy setup failed: {exc}"
        ) from exc
    for folder_path, book_display_name, raw_column, norm_column in _raw_norm_pairs(command.expected_contract):
        try:
            calculation_state = proof_adapter.assert_raw_to_norm_live(
                folder_path,
                book_display_name,
                raw_column,
                norm_column,
            )
        except InfrastructureVerificationError:
            raise
        except Exception as exc:
            report = MismatchReport(
                f"{folder_path}/{book_display_name}",
                raw_column,
                None,
                f"live dependency {raw_column}->{norm_column}",
                str(exc),
                "dependency",
            )
            raise VerificationMismatchError(
                "Verifier dependency proof failed after comparison",
                report,
            ) from exc
        if calculation_state != AUTOMATIC_FORMULA_LOCK_STATE:
            report = MismatchReport(
                f"{folder_path}/{book_display_name}",
                norm_column,
                None,
                "automatic/formula_lock",
                repr(calculation_state),
                "calculation_state",
            )
            raise VerificationMismatchError(
                "Verifier calculation state mismatch after comparison",
                report,
            )
    return mutation_identity


@contextmanager
def _copy_mutation_exclusive(
    command: VerifierWorkerCommand,
):
    staged = Path(command.staged_project_path)
    mutation = Path(command.mutation_copy_path)
    mutation_identity = None
    try:
        with staged.open("rb", buffering=0) as reader:
            source_status = os.fstat(reader.fileno())
            source_identity = (source_status.st_dev, source_status.st_ino)
            if path_identity(staged) != source_identity:
                raise DeterministicVerificationError(
                    f"Staged project identity changed before mutation copy: {staged}"
                )
            for protected in command.protected_paths:
                try:
                    protected_identity = path_identity(Path(protected))
                except IdentityPathError as exc:
                    raise DeterministicVerificationError(str(exc)) from exc
                if protected_identity == source_identity:
                    raise DeterministicVerificationError(
                        f"Staged project is a physical alias of a protected file: {staged}"
                    )
            mutation_identity = command.mutation_copy_identity
            if mutation_identity is None:
                raise DeterministicVerificationError(
                    "Verifier mutation reservation identity is missing"
                )
            with (
                hold_file_identity(
                    mutation,
                    mutation_identity,
                    allow_write=True,
                ),
                mutation.open("r+b", buffering=0) as writer,
            ):
                writer.seek(0)
                writer.truncate()
                while chunk := reader.read(1024 * 1024):
                    writer.write(chunk)
                writer.flush()
                os.fsync(writer.fileno())
                yield mutation_identity
    except DeterministicVerificationError as exc:
        if mutation_identity is not None:
            setattr(exc, "owned_artifact_identity", mutation_identity)
        raise
    except OSError as exc:
        failure = InfrastructureVerificationError(
            f"Verifier mutation-copy write failed: {exc}"
        )
        if mutation_identity is not None:
            setattr(
                failure,
                "owned_artifact_identity",
                mutation_identity,
            )
        raise failure from exc
    except BaseException as exc:
        if mutation_identity is not None:
            setattr(exc, "owned_artifact_identity", mutation_identity)
        raise


def _project_artifact(path: Path) -> ProjectArtifactEvidence:
    target = Path(path)
    return ProjectArtifactEvidence(
        identity=path_identity(target),
        sha256=file_sha256(target),
        size=target.stat().st_size,
    )


def _require_project_artifact(
    path: Path,
    expected: ProjectArtifactEvidence,
    stage: str,
) -> None:
    actual = _project_artifact(path)
    if actual != expected:
        raise DeterministicVerificationError(
            f"Staged project artifact changed {stage}: {path}; "
            f"expected={expected!r}; actual={actual!r}"
        )


def _raw_norm_pairs(contract: ProjectWriteContract) -> tuple[tuple[str, str, str, str], ...]:
    pairs: list[tuple[str, str, str, str]] = []
    for folder in contract.folders:
        for book in folder.books:
            for column in book.columns:
                if column.formula is None:
                    continue
                match = re.fullmatch(r"col\(([^)]+)\)/max\(col\(\1\)\)", column.formula)
                if match is None:
                    raise DeterministicVerificationError(f"Unparseable Raw-to-Norm formula for {folder.path}/{book.display_long_name}/{column.short_name}: {column.formula}")
                pairs.append((folder.path, book.display_long_name, match.group(1), column.short_name))
    if not pairs:
        raise DeterministicVerificationError("No Raw-to-Norm formula pairs available for live dependency proof")
    return tuple(pairs)


def classify_verifier_error(exc: BaseException) -> str:
    if isinstance(exc, InfrastructureVerificationError):
        return "retry_once_later"
    return "non_retryable"


def _compare_book(structural_path: str, expected_columns, actual_columns) -> MismatchReport | None:
    for expected_column, actual_column in zip(expected_columns, actual_columns):
        if (
            expected_column.short_name != actual_column.short_name
            or expected_column.designation != actual_column.designation
            or expected_column.comment != actual_column.comment
        ):
            return MismatchReport(
                structural_path,
                expected_column.short_name,
                None,
                (expected_column.short_name, expected_column.designation, expected_column.comment),
                (actual_column.short_name, actual_column.designation, actual_column.comment),
                "metadata",
            )
        if len(expected_column.values) != len(actual_column.values):
            return MismatchReport(structural_path, expected_column.short_name, None, len(expected_column.values), len(actual_column.values), "structure")
        for row_index, (expected_value, actual_value) in enumerate(zip(expected_column.values, actual_column.values), start=1):
            if (expected_value is None) != (actual_value is None):
                return MismatchReport(structural_path, expected_column.short_name, row_index, expected_value, actual_value, "blank_mask")
        for row_index, (expected_value, actual_value) in enumerate(zip(expected_column.values, actual_column.values), start=1):
            if not _finite(expected_value) or not _finite(actual_value):
                return MismatchReport(structural_path, expected_column.short_name, row_index, expected_value, actual_value, "finite")
        for row_index, (expected_value, actual_value) in enumerate(zip(expected_column.values, actual_column.values), start=1):
            if not _same_origin_numeric_value(expected_value, actual_value):
                return MismatchReport(structural_path, expected_column.short_name, row_index, expected_value, actual_value, "numeric")
        if expected_column.formula != actual_column.formula:
            return MismatchReport(structural_path, expected_column.short_name, None, expected_column.formula, actual_column.formula, "formula")
        if expected_column.method != actual_column.method:
            return MismatchReport(structural_path, expected_column.short_name, None, expected_column.method, actual_column.method, "method")
    return None


def _validate_open_target(path: Path, run_staging_root: Path, allowed_open_targets: tuple[Path, ...], protected_paths: tuple[Path, ...]) -> None:
    resolved = _path_key(path)
    if resolved not in {_path_key(item) for item in allowed_open_targets}:
        raise VerifierWorkerPreflightError(f"Origin open target is not allowlisted: {path}")
    for protected_path in protected_paths:
        if resolved == _path_key(protected_path):
            raise VerifierWorkerPreflightError(f"Origin open target is protected: {path}")
        if not Path(path).exists() or not Path(protected_path).exists():
            continue
        try:
            same_file = Path(path).samefile(protected_path)
        except OSError as exc:
            raise VerifierWorkerPreflightError(
                f"Origin open target physical identity could not be verified: {path}"
            ) from exc
        if same_file:
            raise VerifierWorkerPreflightError(
                f"Origin open target is a physical alias of a protected file: {path}"
            )
    if not _is_under(path, run_staging_root):
        raise VerifierWorkerPreflightError(f"Origin open target is outside run-owned staging: {path}")


def _finite(value) -> bool:
    return value is None or value.is_finite()


def _same_origin_numeric_value(expected_value, actual_value) -> bool:
    return expected_value == actual_value


def _load_origin_session():
    from spectrum_organizer.origin.session_adapters import OriginVerifierSession

    return OriginVerifierSession(__import__(_ORIGIN_MODULE_NAME))


def _is_under(path: Path, parent: Path) -> bool:
    resolved_path = Path(path).resolve()
    resolved_parent = Path(parent).resolve()
    return resolved_path == resolved_parent or resolved_parent in resolved_path.parents


def _path_key(path: Path) -> str:
    return os.path.normcase(str(Path(path).resolve()))
