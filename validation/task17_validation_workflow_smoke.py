from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
for import_root in (ROOT, SRC):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from validation.packaged_workflow import run_packaged_non_origin_workflow
from validation.evidence_lock import (
    OwnedDirectoryLockError,
    acquire_owned_directory_lock,
    release_owned_directory_lock,
)

TMP_ROOT = Path(r"C:\tmp")
MAX_EVIDENCE_TEXT_BYTES = 1_000_000
TEXT_EVIDENCE_SUFFIXES = frozenset({".csv", ".json", ".jsonl", ".log", ".md", ".tsv", ".txt"})
LOCK_OWNER_FILENAME = "owner-token"


class EvidenceCollectionError(RuntimeError):
    pass


def run_validation_workflow_smoke(*, evidence_dir: Path) -> dict:
    evidence_dir = evidence_dir.resolve()
    evidence_dir.mkdir(parents=True, exist_ok=True)
    summary_path = evidence_dir / "validation-workflow-summary.json"
    with _exclusive_evidence_run(
        evidence_dir,
        published_summary_path=summary_path,
    ):
        _invalidate_previous_summary(summary_path)
        summary = _run_validation_workflow_smoke_locked(evidence_dir)
        _write_json_atomic(summary_path, summary)
    return summary


def _run_validation_workflow_smoke_locked(evidence_dir: Path) -> dict:
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    task_root = _new_task_root(timestamp)
    primary_error: BaseException | None = None
    summary: dict | None = None
    try:
        runtime_appdata = task_root / "runtime-localappdata"
        source_root = task_root / "selected-sources"
        output_parent = task_root / "chosen-output"
        source_root.mkdir(parents=True)
        sentinel = task_root / "preexisting-sentinel.txt"
        sentinel.write_bytes(b"validation-owned immutable sentinel")
        sentinel_before = _fingerprint(sentinel)
        source_a = source_root / "raw-a.opju"
        source_b = source_root / "raw-b.OPJ"
        source_a.write_bytes(b"packaged workflow source a")
        source_b.write_bytes(b"packaged workflow source b")
        before = {str(path): _fingerprint(path) for path in (source_a, source_b)}
        preexisting = {str(path.resolve()) for path in task_root.rglob("*")}

        workflow_summary = run_packaged_non_origin_workflow(
            (source_a, source_b),
            output_parent,
            local_appdata=runtime_appdata,
            timestamp=timestamp,
        )
        app_summary_path = Path(workflow_summary.summary_file)
        project_path = Path(workflow_summary.project_path)
        report_path = Path(workflow_summary.report_path)
        app_summary = asdict(workflow_summary)
        after = {str(path): _fingerprint(path) for path in (source_a, source_b)}
        sentinel_after = _fingerprint(sentinel) if sentinel.is_file() else None
        sentinel_unchanged = sentinel_before == sentinel_after
        evidence_text, evidence_bytes = _collect_text_with_byte_count(
            (runtime_appdata, output_parent),
            required_files=(app_summary_path, project_path, report_path),
            allowed_root=task_root,
        )
        created = {str(path.resolve()) for path in task_root.rglob("*")} - preexisting
        validation_failures = _validation_failures(
            runtime_text=evidence_text,
            preexisting_sentinel_unchanged=sentinel_unchanged,
            original_source_paths=(source_a, source_b),
        )
        summary = {
            "evidence_scope": "validation_only_non_origin",
            "task_root": str(task_root),
            "runtime_appdata": str(runtime_appdata),
            "source_paths": [str(source_a), str(source_b)],
            "output_parent": str(output_parent),
            "app_summary_path": str(app_summary_path),
            "app_summary": app_summary,
            "source_fingerprints_before": before,
            "source_fingerprints_after": after,
            "sources_unchanged": before == after,
            "preexisting_sentinel_unchanged": sentinel_unchanged,
            "created_paths_count": len(created),
            "evidence_text_bytes_checked": evidence_bytes,
            "validation_failures": validation_failures,
        }
        if (
            before != after
            or not sentinel_unchanged
            or validation_failures
            or app_summary.get("final_stage") != "completion"
        ):
            raise SystemExit(1)
        _validate_persisted_app_summary(app_summary_path, app_summary)
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        try:
            shutil.rmtree(task_root)
        except FileNotFoundError:
            pass
        except OSError as cleanup_error:
            if primary_error is None:
                raise
            primary_error.add_note(f"Could not clean validation-owned task root: {cleanup_error}")
    assert summary is not None
    return summary


@contextmanager
def _exclusive_evidence_run(
    evidence_dir: Path,
    *,
    published_summary_path: Path | None = None,
):
    lock_path = evidence_dir / ".validation-workflow.lock"
    try:
        lock = acquire_owned_directory_lock(
            lock_path,
            owner_filename=LOCK_OWNER_FILENAME,
            label="Validation workflow",
        )
    except OwnedDirectoryLockError as exc:
        raise EvidenceCollectionError(str(exc)) from exc

    primary_error: BaseException | None = None
    try:
        yield
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        retain_lock = primary_error is not None and bool(
            getattr(primary_error, "retain_validation_lock", False)
        )
        if not retain_lock:
            try:
                release_owned_directory_lock(
                    lock,
                    ownership_error_check=_lock_ownership_error,
                    release_error_cleanup=lambda: _withdraw_published_summary(
                        published_summary_path
                    ),
                )
            except (OwnedDirectoryLockError, EvidenceCollectionError) as cleanup_error:
                lock_error = (
                    cleanup_error
                    if isinstance(cleanup_error, EvidenceCollectionError)
                    else EvidenceCollectionError(str(cleanup_error))
                )
                if primary_error is None:
                    _withdraw_published_summary(published_summary_path)
                    raise lock_error from cleanup_error
                primary_error.add_note(str(lock_error))


def _lock_ownership_error(owner_path: Path, expected_token: str) -> EvidenceCollectionError | None:
    try:
        actual_token = owner_path.read_text(encoding="ascii")
    except FileNotFoundError:
        if not owner_path.parent.exists():
            return EvidenceCollectionError(
                f"Validation workflow lock disappeared before release: {owner_path.parent}"
            )
        return EvidenceCollectionError(
            f"Validation workflow lock ownership changed before release: {owner_path.parent}"
        )
    except (OSError, UnicodeError) as exc:
        return EvidenceCollectionError(
            f"Could not verify validation workflow lock ownership: {owner_path.parent}: {exc}"
        )
    if actual_token != expected_token:
        return EvidenceCollectionError(
            f"Validation workflow lock ownership changed before release: {owner_path.parent}"
        )
    return None


def _invalidate_previous_summary(path: Path) -> None:
    _remove_public_summary(path, action="invalidate previous validation workflow success summary")


def _withdraw_published_summary(path: Path | None) -> None:
    if path is None:
        return
    _remove_public_summary(path, action="withdraw validation workflow success summary")


def _remove_public_summary(path: Path, *, action: str) -> None:
    withdrawn = path.with_name(f".{path.name}.{os.getpid()}.{uuid4().hex}.invalid")
    try:
        os.replace(path, withdrawn)
    except FileNotFoundError:
        return
    except OSError as move_error:
        try:
            path.unlink(missing_ok=True)
        except OSError as unlink_error:
            error = EvidenceCollectionError(f"Could not {action}: {path}")
            error.retain_validation_lock = True
            error.add_note(f"Atomic move failed first: {move_error}")
            raise error from unlink_error
        return
    try:
        withdrawn.unlink(missing_ok=True)
    except OSError:
        # The fixed success name is already gone. A uniquely named invalid
        # tombstone is safer than restoring a summary that claims success.
        pass


def _validate_persisted_app_summary(path: Path, expected: dict) -> None:
    try:
        persisted = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EvidenceCollectionError(f"Persisted app summary is not valid JSON: {path}") from exc

    normalized_expected = json.loads(json.dumps(expected, ensure_ascii=False))
    if persisted != normalized_expected:
        raise EvidenceCollectionError(
            f"Persisted app summary does not match returned workflow summary: {path}"
        )


def _write_json_atomic(path: Path, payload: dict) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _new_task_root(timestamp: str) -> Path:
    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    for _ in range(20):
        candidate = TMP_ROOT / f"SpectrumOrganizerTask17Workflow-{timestamp}-{os.getpid()}-{uuid4().hex[:8]}"
        try:
            candidate.mkdir()
        except FileExistsError:
            continue
        return candidate
    raise RuntimeError("Could not create unique C:\\tmp validation workflow root")


def _collect_runtime_text(runtime_appdata: Path) -> str:
    return _collect_text((runtime_appdata,))


def _collect_text(
    paths: tuple[Path, ...],
    *,
    required_files: tuple[Path, ...] = (),
    allowed_root: Path | None = None,
) -> str:
    return _collect_text_with_byte_count(
        paths,
        required_files=required_files,
        allowed_root=allowed_root,
    )[0]


def _collect_text_with_byte_count(
    paths: tuple[Path, ...],
    *,
    required_files: tuple[Path, ...] = (),
    allowed_root: Path | None = None,
) -> tuple[str, int]:
    chunks: list[str] = []
    byte_count = 0
    files: set[Path] = set()
    required: set[Path] = set()
    allowed_root_resolved = allowed_root.resolve(strict=True) if allowed_root is not None else None
    for path in required_files:
        try:
            resolved = path.resolve(strict=True)
            mode = path.stat().st_mode
        except OSError as exc:
            raise EvidenceCollectionError(f"Required evidence is missing: {path}") from exc
        if allowed_root_resolved is not None and not resolved.is_relative_to(allowed_root_resolved):
            raise EvidenceCollectionError(f"Required evidence is outside the task root: {path}")
        if not stat.S_ISREG(mode):
            raise EvidenceCollectionError(f"Required evidence is not a file: {path}")
        required.add(resolved)
        files.add(resolved)
    for path in paths:
        try:
            mode = path.stat().st_mode
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise EvidenceCollectionError(f"Runtime evidence cannot be inspected: {path}") from exc
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise EvidenceCollectionError(f"Runtime evidence cannot be inspected: {path}") from exc
        if stat.S_ISREG(mode):
            if allowed_root_resolved is not None and not resolved.is_relative_to(allowed_root_resolved):
                raise EvidenceCollectionError(f"Runtime evidence is outside the task root: {path}")
            if resolved in required or resolved.suffix.lower() in TEXT_EVIDENCE_SUFFIXES:
                files.add(resolved)
        elif stat.S_ISDIR(mode):
            for candidate in resolved.rglob("*"):
                try:
                    candidate_mode = candidate.stat().st_mode
                    candidate_resolved = candidate.resolve(strict=True)
                except OSError as exc:
                    raise EvidenceCollectionError(
                        f"Runtime evidence cannot be inspected: {candidate}"
                    ) from exc
                if (
                    allowed_root_resolved is not None
                    and not candidate_resolved.is_relative_to(allowed_root_resolved)
                ):
                    raise EvidenceCollectionError(f"Runtime evidence is outside the task root: {candidate}")
                if (
                    not stat.S_ISREG(candidate_mode)
                    or candidate_resolved.suffix.lower() not in TEXT_EVIDENCE_SUFFIXES
                ):
                    continue
                files.add(candidate_resolved)
    for path in sorted(files):
        is_required = path in required
        try:
            size = path.stat().st_size
        except OSError as exc:
            kind = "Required" if is_required else "Runtime"
            raise EvidenceCollectionError(f"{kind} evidence cannot be inspected: {path}") from exc
        if size > MAX_EVIDENCE_TEXT_BYTES:
            kind = "Required" if is_required else "Runtime"
            raise EvidenceCollectionError(f"{kind} evidence exceeds the size limit: {path}")
        try:
            raw = path.read_bytes()
            if len(raw) > MAX_EVIDENCE_TEXT_BYTES:
                kind = "Required" if is_required else "Runtime"
                raise EvidenceCollectionError(f"{kind} evidence exceeds the size limit: {path}")
            chunks.append(raw.decode("utf-8"))
            byte_count += len(raw)
        except (OSError, UnicodeError) as exc:
            kind = "Required" if is_required else "Runtime"
            raise EvidenceCollectionError(f"{kind} evidence cannot be read as UTF-8: {path}") from exc
    return "\n".join(chunks), byte_count


def _fingerprint(path: Path) -> dict:
    stat = path.stat()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"sha256": digest, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def _validation_failures(
    *,
    runtime_text: str,
    preexisting_sentinel_unchanged: bool,
    original_source_paths: tuple[Path, ...],
) -> tuple[str, ...]:
    failures: list[str] = []
    normalized_text = re.sub(r"[\\/]+", "/", runtime_text.casefold())
    normalized_root = re.sub(r"[\\/]+", "/", str(ROOT).casefold()).rstrip("/")
    workspace_pattern = (
        r"(?<![a-z0-9._-])"
        + re.escape(normalized_root)
        + r"(?![a-z0-9._-])"
    )
    if re.search(workspace_pattern, normalized_text):
        failures.append("runtime references workspace path")
    if not preexisting_sentinel_unchanged:
        failures.append("preexisting validation sentinel changed")
    if any(not path.is_file() for path in original_source_paths):
        failures.append("validation-owned source is missing after workflow")
    return tuple(failures)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-dir", required=True)
    args = parser.parse_args(argv)
    run_validation_workflow_smoke(evidence_dir=Path(args.evidence_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
