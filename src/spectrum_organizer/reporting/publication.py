from __future__ import annotations

from dataclasses import dataclass, field, replace
import hashlib
import json
import os
from pathlib import Path

from spectrum_organizer.safety.identity_paths import (
    create_exclusive_held_directory,
    create_exclusive_held_file,
    file_sha256,
    hold_directory_identity,
    hold_file_identity,
    IdentityPathError,
    lexical_path_exists,
    path_identity,
    ProjectArtifactEvidence,
    quarantine_owned_path,
    remove_empty_owned_directory,
    restore_quarantined_path,
    unlink_owned_path,
)


_MARKER_SUFFIX = ".ownership.json"


def _commit_now(action):
    return action()


class PublicationError(RuntimeError):
    pass


class ParentUnavailableError(PublicationError):
    def __init__(self, path: Path, reason: str, *, cleanup_retry=None):
        self.path = Path(path)
        self.reason = reason
        self.cleanup_retry = cleanup_retry
        super().__init__(f"Output parent unavailable: {self.path}: {reason}")


class PublicationCollisionError(PublicationError):
    pass


class PublicationPostCommitCleanupError(PublicationError):
    def __init__(
        self,
        marker_path: Path,
        final_run_dir: Path,
        reason: str,
        *,
        run_id: str,
        marker_identity: tuple[int, int],
        final_run_dir_identity: tuple[int, int],
    ):
        self.marker_path = Path(marker_path)
        self.final_run_dir = Path(final_run_dir)
        self.run_id = run_id
        self.marker_identity = marker_identity
        self.final_run_dir_identity = final_run_dir_identity
        super().__init__(
            f"Committed output is valid, but staging ownership sidecar "
            f"cleanup failed: {self.marker_path}; final={self.final_run_dir}; "
            f"reason={reason}"
        )


class _CleanupIsolationError(PublicationError):
    def __init__(self, retained_path: Path, reason: str):
        self.retained_path = Path(retained_path)
        super().__init__(reason)


@dataclass(frozen=True)
class PublicationTargets:
    timestamp: str
    run_id: str
    output_parent: Path
    staging_dir: Path
    staging_identity: tuple[int, int]
    final_run_dir: Path
    staging_project_path: Path
    verifier_mutation_path: Path
    final_project_path: Path
    staging_report_path: Path
    final_report_path: Path
    staging_marker_identity: tuple[int, int] | None = None
    _artifact_identities: dict[str, tuple[int, int]] = field(
        default_factory=dict,
        compare=False,
        repr=False,
    )


@dataclass(frozen=True)
class CompletionSummary:
    output_path: Path
    project_path: Path
    report_path: Path
    project_count: int
    message: str
    post_commit_error: Exception | None = None


@dataclass(frozen=True)
class CleanupResult:
    deleted: tuple[Path, ...]
    retained_unknown: tuple[Path, ...]


def create_run_staging(output_parent: Path, timestamp: str, *, run_id: str) -> PublicationTargets:
    parent = Path(output_parent)
    _ensure_output_parent(parent)
    final_run_dir = parent / f"Organized_Origin_Data_{timestamp}"
    project_name = f"Organized_Spectra_{timestamp}.opju"
    report_name = f"Run_Report_{timestamp}.txt"
    staging_dir = _unique_staging_dir(parent, timestamp, run_id)
    staging_identity = None
    try:
        with create_exclusive_held_directory(staging_dir) as (
            _,
            staging_identity,
        ):
            targets = PublicationTargets(
                timestamp=timestamp,
                run_id=run_id,
                output_parent=parent,
                staging_dir=staging_dir,
                staging_identity=staging_identity,
                final_run_dir=final_run_dir,
                staging_project_path=staging_dir / project_name,
                verifier_mutation_path=(
                    staging_dir / f"Verifier_Mutation_{timestamp}.opju"
                ),
                final_project_path=final_run_dir / project_name,
                staging_report_path=staging_dir / report_name,
                final_report_path=final_run_dir / report_name,
            )
            targets = replace(
                targets,
                staging_marker_identity=_write_staging_marker(
                    targets,
                    identity_already_held=True,
                ),
            )
    except (OSError, IdentityPathError) as exc:
        cleanup_retry = None
        if staging_identity is not None:
            try:
                _remove_new_empty_staging(
                    staging_dir,
                    staging_identity,
                )
            except Exception as cleanup_exc:
                retained_staging = Path(
                    getattr(cleanup_exc, "retained_path", staging_dir)
                )
                cleanup_retry = lambda: _remove_new_empty_staging(
                    retained_staging,
                    staging_identity,
                )
                exc.add_note(
                    f"new staging cleanup also failed: {cleanup_exc}"
                )
        raise ParentUnavailableError(
            parent,
            str(exc),
            cleanup_retry=cleanup_retry,
        ) from exc
    return targets


def publish_completed_run(
    targets: PublicationTargets,
    report_text: str,
    verifier_result=None,
    *,
    commit=_commit_now,
    verified_project_identity: tuple[int, int] | None = None,
    verified_project_sha256: str | None = None,
) -> CompletionSummary:
    verified_artifact = _verified_project_artifact(
        targets,
        verifier_result,
        verified_project_identity=verified_project_identity,
        verified_project_sha256=verified_project_sha256,
    )
    _require_owned_targets(targets)
    if not targets.staging_dir.is_dir():
        raise PublicationError(f"Staging directory is missing: {targets.staging_dir}")
    if not targets.staging_project_path.is_file():
        raise PublicationError(f"Staged project is missing: {targets.staging_project_path}")
    if lexical_path_exists(targets.verifier_mutation_path):
        raise PublicationError(
            "Verifier mutation copy still exists; refusing publication: "
            f"{targets.verifier_mutation_path}"
        )
    allowed_staging_names = _caller_held_staging_names(targets)
    unknown = _unknown_staging_children(
        targets.staging_dir,
        allowed_staging_names,
    )
    if unknown:
        raise PublicationError(
            "Unexpected staging artifact; refusing publication: "
            + ", ".join(str(path) for path in unknown)
        )
    register_staging_artifact_identity(
        targets,
        targets.staging_project_path,
        run_id=targets.run_id,
        expected_identity=verified_artifact.identity,
    )
    try:
        written_report_artifact = _write_text_exclusive(
            targets.staging_report_path,
            report_text,
        )
    except FileExistsError as exc:
        raise PublicationCollisionError(
            "Target already exists; refusing to overwrite: "
            f"{targets.staging_report_path}"
        ) from exc
    except OSError as exc:
        raise ParentUnavailableError(targets.output_parent, str(exc)) from exc
    try:
        register_staging_artifact_identity(
            targets,
            targets.staging_report_path,
            run_id=targets.run_id,
            expected_identity=written_report_artifact.identity,
        )
        _require_verified_project_artifact(
            targets.staging_project_path,
            verified_artifact,
        )
        _require_absent(targets.final_run_dir)
        _require_absent(targets.final_project_path)
        _require_absent(targets.final_report_path)
        staging_identity, marker_identity = _require_owned_targets(targets)
        allowed_names = set(allowed_staging_names)
        allowed_names.discard(targets.verifier_mutation_path.name)
        commit(
            lambda: _rename_and_validate_committed_output(
                targets,
                staging_identity=staging_identity,
                project_artifact=verified_artifact,
                report_artifact=written_report_artifact,
                allowed_names=allowed_names,
            )
        )
        marker_path = _staging_marker_path(targets.staging_dir)
        post_commit_error = None
        try:
            if lexical_path_exists(marker_path):
                _unlink_owned_marker(marker_path, marker_identity)
        except Exception as exc:
            retained_marker = Path(getattr(exc, "retained_path", marker_path))
            post_commit_error = PublicationPostCommitCleanupError(
                retained_marker,
                targets.final_run_dir,
                str(exc),
                run_id=targets.run_id,
                marker_identity=marker_identity,
                final_run_dir_identity=staging_identity,
            )
    except PublicationCollisionError:
        _remove_staged_success_report(targets, written_report_artifact.identity)
        raise
    except PublicationError:
        _remove_staged_success_report(targets, written_report_artifact.identity)
        raise
    except IdentityPathError as exc:
        _remove_staged_success_report(targets, written_report_artifact.identity)
        raise PublicationError(str(exc)) from exc
    except OSError as exc:
        _remove_staged_success_report(targets, written_report_artifact.identity)
        raise ParentUnavailableError(targets.output_parent, str(exc)) from exc
    return CompletionSummary(
        output_path=targets.final_run_dir,
        project_path=targets.final_project_path,
        report_path=targets.final_report_path,
        project_count=1,
        message=f"Created {targets.final_run_dir}. Report: {targets.final_report_path}",
        post_commit_error=post_commit_error,
    )


def retry_post_commit_cleanup(completion) -> None:
    error = getattr(completion, "post_commit_error", None)
    if not isinstance(error, PublicationPostCommitCleanupError):
        return
    marker = error.marker_path
    if not lexical_path_exists(marker):
        return
    if marker.is_symlink() or not marker.is_file():
        raise PublicationError(
            f"Publication ownership marker is not a regular file: {marker}"
        )
    if _path_identity(marker) != error.marker_identity:
        raise PublicationError(
            f"Publication ownership marker identity changed: {marker}"
        )
    final_dir = error.final_run_dir
    if (
        final_dir.is_symlink()
        or not final_dir.is_dir()
        or _path_identity(final_dir) != error.final_run_dir_identity
    ):
        raise PublicationError(
            f"Published output directory identity changed: {final_dir}"
        )
    payload = _read_marker_payload(marker)
    if (
        _identity_from_marker(payload, "marker_identity", marker)
        != error.marker_identity
        or _identity_from_marker(payload, "staging_identity", marker)
        != error.final_run_dir_identity
    ):
        raise PublicationError(
            f"Publication ownership marker identity binding changed: {marker}"
        )
    registered_final = payload.get("final_run_dir")
    if (
        payload.get("run_id") != error.run_id
        or not isinstance(registered_final, str)
        or Path(registered_final).resolve() != final_dir.resolve()
    ):
        raise PublicationError(
            f"Publication ownership marker does not match committed output: {marker}"
        )
    try:
        _unlink_owned_marker(marker, error.marker_identity)
    except Exception as exc:
        error.marker_path = Path(getattr(exc, "retained_path", marker))
        raise


def write_failure_log(
    timestamp: str,
    message: str,
    *,
    local_appdata: str | os.PathLike[str] | Path | None = None,
    output_attempts=(),
    verifier_attempts=(),
) -> Path:
    base = Path(local_appdata) if local_appdata is not None else _local_appdata_from_env()
    log_dir = base / "Spectrum Organizer" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    lines = [f"失败运行 {timestamp}", message]
    lines.extend(_attempt_lines("输出", output_attempts))
    lines.extend(_attempt_lines("验证", verifier_attempts))
    text = "\n".join(lines) + "\n"
    base_path = log_dir / f"Failed_Run_{timestamp}.txt"
    suffix = 0
    while True:
        path = (
            base_path
            if suffix == 0
            else log_dir / f"Failed_Run_{timestamp}_{suffix:03d}.txt"
        )
        try:
            with path.open("x", encoding="utf-8") as stream:
                stream.write(text)
            return path
        except FileExistsError:
            suffix += 1


def cleanup_owned_staging(paths, *, run_id: str) -> CleanupResult:
    deleted: list[Path] = []
    retained: list[Path] = []
    for item in tuple(paths):
        targets = item if isinstance(item, PublicationTargets) else None
        path = targets.staging_dir if targets is not None else Path(item)
        if not lexical_path_exists(path):
            marker = _staging_marker_path(path)
            if lexical_path_exists(marker):
                final_path = _registered_final_run_dir(path)
                if final_path is not None and lexical_path_exists(final_path):
                    retained.append(marker)
                    retained.append(final_path)
                elif targets is not None:
                    marker_identity = targets.staging_marker_identity
                    if marker_identity is None or targets.run_id != run_id:
                        retained.append(marker)
                        continue
                    try:
                        _unlink_owned_marker(marker, marker_identity)
                    except _CleanupIsolationError as exc:
                        retained.append(exc.retained_path)
                    else:
                        deleted.append(marker)
                else:
                    retained.append(marker)
            continue
        if targets is None or targets.run_id != run_id:
            retained.append(path)
            continue
        marker = _staging_marker_path(path)
        try:
            staging_identity, marker_identity = _require_owned_targets(targets)
        except PublicationError:
            retained.append(path)
            if lexical_path_exists(marker):
                retained.append(marker)
            continue
        allowed_names = _caller_held_staging_names(targets)
        artifact_identities = dict(targets._artifact_identities)
        unknown = _unknown_staging_children(path, allowed_names)
        if unknown:
            retained.extend(unknown)
            continue
        try:
            isolated_staging = _isolate_for_cleanup(path, staging_identity)
        except _CleanupIsolationError as exc:
            retained.extend((exc.retained_path, marker))
            continue
        try:
            isolated_marker = _isolate_for_cleanup(marker, marker_identity)
        except _CleanupIsolationError as exc:
            restored = _restore_isolated_path(
                isolated_staging,
                path,
                staging_identity,
            )
            retained.append(exc.retained_path)
            retained.append(path if restored else isolated_staging)
            continue
        except Exception:
            _restore_isolated_path(isolated_staging, path, staging_identity)
            raise
        isolated_unknown = tuple(
            child
            for child in isolated_staging.iterdir()
            if child.name not in allowed_names
        )
        if isolated_unknown:
            marker_restored = _restore_isolated_path(
                isolated_marker,
                marker,
                marker_identity,
            )
            staging_restored = _restore_isolated_path(
                isolated_staging,
                path,
                staging_identity,
            )
            retained.extend(
                (path / child.name for child in isolated_unknown)
                if staging_restored
                else isolated_unknown
            )
            if not marker_restored:
                retained.append(isolated_marker)
            continue
        replaced_or_unbound = tuple(
            child
            for child in isolated_staging.iterdir()
            if (
                child.is_dir()
                or artifact_identities.get(child.name) is None
                or _path_identity(child) != artifact_identities[child.name]
            )
        )
        if replaced_or_unbound:
            marker_restored = _restore_isolated_path(
                isolated_marker,
                marker,
                marker_identity,
            )
            staging_restored = _restore_isolated_path(
                isolated_staging,
                path,
                staging_identity,
            )
            retained.extend(
                (path / child.name for child in replaced_or_unbound)
                if staging_restored
                else replaced_or_unbound
            )
            if not marker_restored:
                retained.append(isolated_marker)
            continue
        try:
            for child in tuple(isolated_staging.iterdir()):
                unlink_owned_path(
                    child,
                    artifact_identities[child.name],
                )
            remove_empty_owned_directory(
                isolated_staging,
                staging_identity,
            )
        except Exception:
            _restore_isolated_path(
                isolated_marker,
                marker,
                marker_identity,
            )
            _restore_isolated_path(
                isolated_staging,
                path,
                staging_identity,
            )
            raise
        try:
            unlink_owned_path(isolated_marker, marker_identity)
        except Exception as exc:
            retained_marker = isolated_marker
            if _restore_isolated_path(
                isolated_marker,
                marker,
                marker_identity,
            ):
                retained_marker = marker
            setattr(exc, "retained_path", retained_marker)
            raise
        namespace_replacements = tuple(
            candidate
            for candidate in (path, marker)
            if lexical_path_exists(candidate)
        )
        if namespace_replacements:
            retained.extend(namespace_replacements)
        else:
            deleted.append(path)
            targets._artifact_identities.clear()
    return CleanupResult(tuple(deleted), tuple(retained))


def remove_run_owned_artifact(
    staging_dir: PublicationTargets,
    artifact_path: Path,
    *,
    run_id: str,
    expected_identity: tuple[int, int] | None,
) -> bool:
    targets = _require_publication_targets(staging_dir, run_id)
    staging = targets.staging_dir
    artifact = Path(artifact_path)
    _require_owned_targets(targets)
    if artifact.parent.resolve() != staging.resolve():
        raise PublicationError(
            f"Artifact is outside owned staging: {artifact}"
        )
    allowed_names = _caller_held_staging_names(targets)
    if artifact.name not in allowed_names:
        raise PublicationError(
            f"Artifact is not registered for retry cleanup: {artifact}"
        )
    if not lexical_path_exists(artifact):
        return False
    if artifact.is_dir() and not artifact.is_symlink():
        raise PublicationError(
            f"Registered staging artifact is unexpectedly a directory: {artifact}"
        )
    if expected_identity is None:
        raise PublicationError(
            f"Artifact cleanup identity is unavailable: {artifact}"
        )
    try:
        if _path_identity(artifact) != expected_identity:
            raise PublicationError(
                f"Registered staging artifact identity changed: {artifact}"
            )
    except IdentityPathError as exc:
        raise PublicationError(str(exc)) from exc
    _unlink_owned_file(
        artifact,
        expected_identity,
        label="registered staging artifact",
    )
    if targets._artifact_identities.get(artifact.name) == expected_identity:
        targets._artifact_identities.pop(artifact.name, None)
    return True


def reserve_staging_artifact_identity(
    staging_dir: PublicationTargets,
    artifact_path: Path,
    *,
    run_id: str,
) -> tuple[int, int]:
    targets = _require_publication_targets(staging_dir, run_id)
    staging = targets.staging_dir
    artifact = Path(artifact_path)
    staging_identity, _ = _require_owned_targets(targets)
    if artifact.parent.resolve() != staging.resolve():
        raise PublicationError(
            f"Artifact is outside owned staging: {artifact}"
        )
    if artifact.name not in _caller_held_staging_names(targets):
        raise PublicationError(
            f"Artifact is not registered for staging creation: {artifact}"
        )
    identity = None
    try:
        with hold_directory_identity(staging, staging_identity):
            with create_exclusive_held_file(artifact) as (_, identity):
                register_staging_artifact_identity(
                    targets,
                    artifact,
                    run_id=run_id,
                    expected_identity=identity,
                )
    except BaseException as exc:
        if identity is not None:
            try:
                unlink_owned_path(artifact, identity)
            except (OSError, IdentityPathError) as cleanup_exc:
                exc.owned_artifact_identity = identity
                exc.add_note(str(cleanup_exc))
            else:
                if targets._artifact_identities.get(artifact.name) == identity:
                    targets._artifact_identities.pop(artifact.name, None)
        raise
    return identity


def register_staging_artifact_identity(
    staging_dir: PublicationTargets,
    artifact_path: Path,
    *,
    run_id: str,
    expected_identity: tuple[int, int],
) -> None:
    targets = _require_publication_targets(staging_dir, run_id)
    staging = targets.staging_dir
    artifact = Path(artifact_path)
    _, marker_identity = _require_owned_targets(targets)
    if artifact.parent.resolve() != staging.resolve():
        raise PublicationError(
            f"Artifact is outside owned staging: {artifact}"
        )
    allowed_names = _caller_held_staging_names(targets)
    if artifact.name not in allowed_names:
        raise PublicationError(
            f"Artifact is not registered for staging cleanup: {artifact}"
        )
    if _path_identity(artifact) != expected_identity:
        raise PublicationError(
            f"Staging artifact identity changed before registration: {artifact}"
        )
    active_identity = targets._artifact_identities.get(artifact.name)
    if active_identity is not None and active_identity != expected_identity:
        raise PublicationError(
            f"Staging artifact has an unretired active generation: {artifact}"
        )
    targets._artifact_identities[artifact.name] = expected_identity
    recorded = _registered_staging_artifact_identities(
        staging,
        run_id=run_id,
        allowed_names=allowed_names,
    )
    if recorded.get(artifact.name) == expected_identity:
        return
    marker = _staging_marker_path(staging)
    with hold_file_identity(
        marker,
        marker_identity,
        allow_write=True,
    ):
        with marker.open("a", encoding="utf-8") as stream:
            status = os.fstat(stream.fileno())
            if (status.st_dev, status.st_ino) != marker_identity:
                raise PublicationError(
                    f"Staging marker identity changed before artifact registration: {marker}"
                )
            stream.write(
                json.dumps(
                    {
                        "artifact_name": artifact.name,
                        "identity": list(expected_identity),
                    },
                    sort_keys=True,
                )
                + "\n"
            )
            stream.flush()
            os.fsync(stream.fileno())
        if _path_identity(artifact) != expected_identity:
            raise PublicationError(
                f"Staging artifact identity changed during registration: {artifact}"
            )


def _rename_staging_directory(staging_dir: Path, final_run_dir: Path) -> None:
    staging_dir.rename(final_run_dir)


def _rename_and_validate_committed_output(
    targets: PublicationTargets,
    *,
    staging_identity: tuple[int, int],
    project_artifact: ProjectArtifactEvidence,
    report_artifact: ProjectArtifactEvidence,
    allowed_names: set[str],
) -> None:
    if (
        targets.staging_dir.is_symlink()
        or not targets.staging_dir.is_dir()
        or _path_identity(targets.staging_dir) != staging_identity
    ):
        raise PublicationError(
            f"Staging directory identity changed before commit: {targets.staging_dir}"
        )
    for path, expected_identity in (
        (targets.staging_project_path, project_artifact.identity),
        (targets.staging_report_path, report_artifact.identity),
    ):
        if (
            path.is_symlink()
            or not path.is_file()
            or _path_identity(path) != expected_identity
        ):
            raise PublicationError(
                f"Staging artifact identity changed before commit: {path}"
            )
    with hold_file_identity(
        targets.staging_project_path,
        project_artifact.identity,
        allow_write=False,
    ):
        _require_verified_project_artifact(
            targets.staging_project_path,
            project_artifact,
        )
    _require_file_artifact(
        targets.staging_report_path,
        report_artifact,
        label="staged report",
    )
    unknown = _children_outside_allowlist(
        targets.staging_dir,
        allowed_names,
    )
    if unknown:
        raise PublicationError(
            "Unexpected staging artifact; refusing publication: "
            + ", ".join(str(path) for path in unknown)
        )
    _require_verified_project_artifact(
        targets.staging_project_path,
        project_artifact,
    )
    _require_absent(targets.final_run_dir)
    _rename_staging_directory(targets.staging_dir, targets.final_run_dir)
    try:
        if (
            targets.final_run_dir.is_symlink()
            or not targets.final_run_dir.is_dir()
            or _path_identity(targets.final_run_dir) != staging_identity
        ):
            raise PublicationError(
                "Committed output directory identity changed: "
                f"{targets.final_run_dir}"
            )
        for path, expected_identity in (
            (targets.final_project_path, project_artifact.identity),
            (targets.final_report_path, report_artifact.identity),
        ):
            if (
                path.is_symlink()
                or not path.is_file()
                or _path_identity(path) != expected_identity
            ):
                raise PublicationError(
                    f"Committed output artifact identity changed: {path}"
                )
        _require_verified_project_artifact(
            targets.final_project_path,
            project_artifact,
        )
        _require_file_artifact(
            targets.final_report_path,
            report_artifact,
            label="committed report",
        )
        unknown = _children_outside_allowlist(
            targets.final_run_dir,
            allowed_names,
        )
        if unknown:
            raise PublicationError(
                "Unexpected staging artifact; refusing publication: "
                + ", ".join(str(path) for path in unknown)
            )
    except Exception as exc:
        try:
            _require_absent(targets.staging_dir)
            if (
                targets.final_run_dir.is_symlink()
                or not targets.final_run_dir.is_dir()
                or _path_identity(targets.final_run_dir) != staging_identity
            ):
                raise PublicationError(
                    "Committed output cannot be rolled back because its "
                    f"directory identity changed: {targets.final_run_dir}"
                )
            targets.final_run_dir.rename(targets.staging_dir)
        except Exception as rollback_exc:
            exc.add_note(f"publication rollback also failed: {rollback_exc}")
        raise


def _verified_project_artifact(
    targets: PublicationTargets,
    verifier_result,
    *,
    verified_project_identity: tuple[int, int] | None,
    verified_project_sha256: str | None,
) -> ProjectArtifactEvidence:
    artifact = getattr(
        verifier_result,
        "verified_project_artifact",
        None,
    )
    if isinstance(artifact, ProjectArtifactEvidence):
        return artifact
    if (
        isinstance(verified_project_identity, tuple)
        and len(verified_project_identity) == 2
        and isinstance(verified_project_sha256, str)
        and len(verified_project_sha256) == 64
    ):
        return ProjectArtifactEvidence(
            identity=verified_project_identity,
            sha256=verified_project_sha256,
            size=targets.staging_project_path.stat().st_size,
        )
    raise PublicationError(
        "Verified staged project artifact evidence is required for publication"
    )


def _require_verified_project_artifact(
    path: Path,
    expected: ProjectArtifactEvidence,
) -> None:
    _require_file_artifact(
        path,
        expected,
        label="verified staged project",
    )


def _file_artifact(path: Path) -> ProjectArtifactEvidence:
    target = Path(path)
    return ProjectArtifactEvidence(
        identity=_path_identity(target),
        sha256=file_sha256(target),
        size=target.stat().st_size,
    )


def _require_file_artifact(
    path: Path,
    expected: ProjectArtifactEvidence,
    *,
    label: str,
) -> None:
    target = Path(path)
    actual = _file_artifact(target)
    if actual != expected:
        raise PublicationError(
            f"{label.capitalize()} identity or digest changed: {target}; "
            f"expected={expected!r}; actual={actual!r}"
        )


def _children_outside_allowlist(
    directory: Path,
    allowed_names: set[str],
) -> tuple[Path, ...]:
    return tuple(
        child
        for child in Path(directory).iterdir()
        if child.name not in allowed_names
    )


def _path_identity(path: Path) -> tuple[int, int]:
    try:
        return path_identity(Path(path))
    except IdentityPathError as exc:
        raise PublicationError(str(exc)) from exc


def _isolate_for_cleanup(
    path: Path,
    expected_identity: tuple[int, int],
) -> Path:
    try:
        return quarantine_owned_path(Path(path), expected_identity)
    except IdentityPathError as exc:
        raise _CleanupIsolationError(
            exc.retained_path,
            str(exc),
        ) from exc


def _restore_isolated_path(
    isolated_path: Path,
    original_path: Path,
    expected_identity: tuple[int, int],
) -> bool:
    return restore_quarantined_path(
        Path(isolated_path),
        Path(original_path),
        expected_identity,
    )


def _unlink_owned_file(
    path: Path,
    expected_identity: tuple[int, int],
    *,
    label: str,
) -> None:
    target = Path(path)
    if (
        target.is_symlink()
        or not target.is_file()
        or _path_identity(target) != expected_identity
    ):
        raise PublicationError(
            f"{label.capitalize()} identity changed: {target}"
        )
    try:
        unlink_owned_path(target, expected_identity)
    except IdentityPathError as exc:
        raise _CleanupIsolationError(
            exc.retained_path,
            str(exc),
        ) from exc


def _unlink_owned_marker(
    marker_path: Path,
    expected_identity: tuple[int, int],
) -> None:
    _unlink_owned_file(
        marker_path,
        expected_identity,
        label="publication ownership marker",
    )


def _remove_new_empty_staging(
    staging_dir: Path,
    expected_identity: tuple[int, int],
) -> CleanupResult:
    path = Path(staging_dir)
    if not lexical_path_exists(path):
        return CleanupResult((), ())
    if (
        path.is_symlink()
        or not path.is_dir()
        or _path_identity(path) != expected_identity
    ):
        raise PublicationError(
            f"New staging directory identity changed before cleanup: {path}"
        )
    isolated = _isolate_for_cleanup(path, expected_identity)
    try:
        if any(isolated.iterdir()):
            raise PublicationError(
                f"New staging directory is not empty; refusing cleanup: {isolated}"
            )
        remove_empty_owned_directory(isolated, expected_identity)
    except Exception as exc:
        retained = isolated
        if _restore_isolated_path(isolated, path, expected_identity):
            retained = path
        setattr(exc, "retained_path", retained)
        raise
    return CleanupResult((path,), ())


def _remove_staged_success_report(
    targets: PublicationTargets,
    expected_identity: tuple[int, int],
) -> None:
    try:
        _require_owned_targets(targets)
        if targets.staging_report_path.exists():
            _unlink_owned_file(
                targets.staging_report_path,
                expected_identity,
                label="staged success report",
            )
    except (OSError, PublicationError):
        pass


def _ensure_output_parent(parent: Path) -> None:
    if parent.exists() and not parent.is_dir():
        raise ParentUnavailableError(parent, "path is occupied by a file")
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ParentUnavailableError(parent, str(exc)) from exc
    if not parent.is_dir():
        raise ParentUnavailableError(parent, "path is not a directory")


def _unique_staging_dir(parent: Path, timestamp: str, run_id: str) -> Path:
    base = parent / f".SpectrumOrganizer_staging_{timestamp}_{run_id}"
    candidate = base
    suffix = 1
    while candidate.exists() or _staging_marker_path(candidate).exists():
        candidate = parent / f"{base.name}_{suffix:03d}"
        suffix += 1
    return candidate


def _write_staging_marker(
    targets: PublicationTargets,
    *,
    identity_already_held: bool = False,
) -> tuple[int, int]:
    marker_path = _staging_marker_path(targets.staging_dir)
    if (
        not identity_already_held
        and _path_identity(targets.staging_dir) != targets.staging_identity
    ):
        raise PublicationError(
            f"Staging directory identity changed: {targets.staging_dir}"
        )
    with create_exclusive_held_file(marker_path) as (stream, marker_identity):
        marker_status = os.fstat(stream.fileno())
        if marker_status.st_ino == 0:
            raise PublicationError(
                f"Filesystem identity is unavailable: {marker_path}"
            )
        payload = {
            "run_id": targets.run_id,
            "timestamp": targets.timestamp,
            "final_run_dir": str(targets.final_run_dir),
            "project_name": targets.staging_project_path.name,
            "verifier_mutation_name": targets.verifier_mutation_path.name,
            "report_name": targets.staging_report_path.name,
            "staging_identity": list(targets.staging_identity),
            "marker_identity": [
                marker_status.st_dev,
                marker_status.st_ino,
            ],
        }
        stream.write((json.dumps(payload, sort_keys=True) + "\n").encode("utf-8"))
        stream.flush()
        os.fsync(stream.fileno())
    return marker_identity


def _require_publication_targets(
    value: object,
    run_id: str,
) -> PublicationTargets:
    if not isinstance(value, PublicationTargets) or value.run_id != run_id:
        raise PublicationError(
            "Staging mutation requires its creation-bound publication targets"
        )
    return value


def _require_owned_targets(
    targets: PublicationTargets,
) -> tuple[tuple[int, int], tuple[int, int]]:
    marker = _staging_marker_path(targets.staging_dir)
    payload = _read_marker_payload(marker)
    staging_identity = _identity_from_marker(
        payload,
        "staging_identity",
        marker,
    )
    marker_identity = _identity_from_marker(
        payload,
        "marker_identity",
        marker,
    )
    if (
        targets.staging_dir.is_symlink()
        or not targets.staging_dir.is_dir()
        or staging_identity != targets.staging_identity
        or marker_identity != targets.staging_marker_identity
        or _path_identity(targets.staging_dir) != staging_identity
        or marker.is_symlink()
        or not marker.is_file()
        or _path_identity(marker) != marker_identity
    ):
        raise PublicationError(
            f"Staging ownership identity changed: {targets.staging_dir}"
        )
    registered_final = payload.get("final_run_dir")
    if (
        payload.get("run_id") != targets.run_id
        or not isinstance(registered_final, str)
        or Path(registered_final).resolve() != targets.final_run_dir.resolve()
        or payload.get("project_name") != targets.staging_project_path.name
        or payload.get("verifier_mutation_name")
        != targets.verifier_mutation_path.name
        or payload.get("report_name") != targets.staging_report_path.name
    ):
        raise PublicationError(
            f"Staging ownership marker does not match targets: {marker}"
        )
    return staging_identity, marker_identity


def _read_marker_payload(marker: Path) -> dict[str, object]:
    try:
        lines = Path(marker).read_text(encoding="utf-8").splitlines()
        payload = json.loads(lines[0])
    except (OSError, IndexError, json.JSONDecodeError) as exc:
        raise PublicationError(
            f"Staging marker could not be read: {marker}"
        ) from exc
    if not isinstance(payload, dict):
        raise PublicationError(f"Staging marker is invalid: {marker}")
    return payload


def _registered_staging_artifact_identities(
    path: Path,
    *,
    run_id: str,
    allowed_names: set[str],
) -> dict[str, tuple[int, int]]:
    staging = Path(path)
    marker = _staging_marker_path(staging)
    payload = _read_marker_payload(marker)
    if payload.get("run_id") != run_id:
        raise PublicationError(
            f"Staging marker run id changed: {marker}"
        )
    marker_names = _registered_staging_names(staging)
    if marker_names != allowed_names:
        raise PublicationError(
            f"Staging marker artifact names changed: {marker}"
        )
    try:
        lines = marker.read_text(encoding="utf-8").splitlines()[1:]
    except OSError as exc:
        raise PublicationError(
            f"Staging artifact identity ledger could not be read: {marker}"
        ) from exc
    identities: dict[str, tuple[int, int]] = {}
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise PublicationError(
                f"Staging artifact identity ledger is invalid: {marker}"
            ) from exc
        if (
            not isinstance(record, dict)
            or set(record) != {"artifact_name", "identity"}
            or record.get("artifact_name") not in allowed_names
        ):
            raise PublicationError(
                f"Staging artifact identity record is invalid: {marker}"
            )
        name = record["artifact_name"]
        identity = _identity_from_marker(record, "identity", marker)
        identities[name] = identity
    return identities


def _identity_from_marker(
    payload: dict[str, object],
    field: str,
    marker: Path,
) -> tuple[int, int]:
    value = payload.get(field)
    if (
        not isinstance(value, list)
        or len(value) != 2
        or any(not isinstance(part, int) for part in value)
        or value[1] == 0
    ):
        raise PublicationError(
            f"Staging marker contains invalid {field}: {marker}"
        )
    return value[0], value[1]


def _write_text_exclusive(
    path: Path,
    text: str,
) -> ProjectArtifactEvidence:
    encoded = text.encode("utf-8")
    with create_exclusive_held_file(Path(path)) as (stream, identity):
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
        return ProjectArtifactEvidence(
            identity=identity,
            sha256=hashlib.sha256(encoded).hexdigest(),
            size=len(encoded),
        )


def _require_absent(path: Path) -> None:
    if Path(path).exists():
        raise PublicationCollisionError(f"Target already exists; refusing to overwrite: {path}")


def _attempt_lines(kind: str, attempts) -> list[str]:
    lines = []
    for attempt in attempts:
        lines.append(
            f"{kind}尝试 {attempt.attempt}: "
            f"{attempt.status} - {attempt.message}"
        )
    return lines


def _unknown_staging_children(
    path: Path,
    allowed_names: set[str],
) -> tuple[Path, ...]:
    return tuple(child for child in path.iterdir() if child.name not in allowed_names)


def _caller_held_staging_names(targets: PublicationTargets) -> set[str]:
    return {
        targets.staging_project_path.name,
        targets.verifier_mutation_path.name,
        targets.staging_report_path.name,
    }


def _registered_staging_names(path: Path) -> set[str]:
    marker = _staging_marker_path(Path(path))
    payload = _read_marker_payload(marker)
    names = {
        payload.get("project_name"),
        payload.get("verifier_mutation_name"),
        payload.get("report_name"),
    }
    if any(not isinstance(name, str) or not name for name in names):
        raise PublicationError(
            f"Staging marker contains invalid artifact names: {marker}"
        )
    return names


def _registered_final_run_dir(staging_dir: Path) -> Path | None:
    marker = _staging_marker_path(staging_dir)
    try:
        payload = _read_marker_payload(marker)
    except PublicationError:
        return None
    final_run_dir = payload.get("final_run_dir")
    return Path(final_run_dir) if isinstance(final_run_dir, str) else None


def _staging_marker_path(staging_dir: Path) -> Path:
    path = Path(staging_dir)
    return path.with_name(f"{path.name}{_MARKER_SUFFIX}")


def _local_appdata_from_env() -> Path:
    value = os.environ.get("LOCALAPPDATA")
    if not value:
        raise PublicationError("LOCALAPPDATA is required for failure logs")
    return Path(value)
