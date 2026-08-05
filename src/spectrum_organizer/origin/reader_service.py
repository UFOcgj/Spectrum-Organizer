from __future__ import annotations

from pathlib import Path
import time

from spectrum_organizer.domain.extracted import ExtractionSource
from spectrum_organizer.origin import extract_worker
from spectrum_organizer.safety.fingerprints import hash_file
from spectrum_organizer.safety.owned_paths import OWNERSHIP_FILE, read_ownership
from spectrum_organizer.safety.process_boundary import default_origin_process_probe
from spectrum_organizer.safety.source_copies import (
    CopyVerificationError,
    locked_verified_source_copy,
)
from spectrum_organizer.store.run_snapshot import (
    RunSnapshot,
    UnsupportedSourceReconciliationError,
)
from spectrum_organizer.workflow.extraction_contracts import (
    ExtractionCleanupBlockedError,
    ProductRunnerError,
    ReaderProcessCommand,
    ReaderSourceExtractionSummary,
    UnsupportedSourceInputError,
    _confirmed_allow_missing_s1,
    _confirmed_s1_limit,
    _confirmed_steady_emission_y,
)


class _ReaderCopySourceManager:
    def __init__(self, source: object):
        self._source = source

    def verify_copy(self, source_id: str) -> None:
        if source_id != self._source.source_id:
            raise ProductRunnerError(f"Invalid extraction source id: {source_id}")
        copy_path = Path(self._source.copy_path)
        if (
            copy_path.stat().st_size != self._source.size_bytes
            or hash_file(copy_path) != self._source.sha256
        ):
            raise ProductRunnerError(f"Source copy changed or mismatched: {copy_path}")

    def discard_failed_copy(self, source_id: str) -> None:
        if source_id != self._source.source_id:
            raise ProductRunnerError(f"Invalid extraction source id: {source_id}")

    def refresh_copy(self, source_id: str) -> None:
        raise ProductRunnerError(
            f"Reader child cannot create a retry copy: {source_id}"
        )


def run_reader_source_extraction_phase(
    command: ReaderProcessCommand,
    *,
    worker_factory_builder=None,
    free_bytes_provider=None,
    cleanup_identity_callback=None,
    sidecar_auth_key: str | None = None,
) -> ReaderSourceExtractionSummary:
    temp_root = Path(command.snapshot_path).resolve().parent
    ownership = read_ownership(temp_root)
    if ownership.run_id != command.run_id or ownership.marker_id != command.marker_id:
        raise ProductRunnerError("谱图提取 reader ownership 身份不一致")
    if ownership.protected_paths:
        raise ProductRunnerError("谱图提取 reader ownership 仍包含父进程专属路径")

    snapshot_path = Path(command.snapshot_path).resolve()
    copy_path = Path(command.source_copy.copy_path).resolve()
    if snapshot_path.parent != temp_root or snapshot_path.name == OWNERSHIP_FILE:
        raise ProductRunnerError("谱图提取 reader snapshot 路径无效")
    if snapshot_path not in ownership.allowed_children:
        raise ProductRunnerError("谱图提取 reader snapshot 未登记")
    if not _path_is_registered(copy_path, ownership.allowed_children):
        raise ProductRunnerError("谱图提取 reader source copy 未登记")

    try:
        with locked_verified_source_copy(
            copy_path,
            expected_identity=(
                command.source_copy.device_id,
                command.source_copy.file_id,
            ),
            expected_size_bytes=command.source_copy.size_bytes,
            expected_sha256=command.source_copy.sha256,
        ):
            source = ExtractionSource(
                source_id=command.source_copy.source_id,
                copy_path=copy_path,
                sha256=command.source_copy.sha256,
                allowed_children=ownership.allowed_children,
                size_bytes=command.source_copy.size_bytes,
            )
            extract_worker.validate_worker_open_target(
                copy_path,
                {copy_path},
                role="extraction",
                allowed_children=ownership.allowed_children,
                expected_sha256=source.sha256,
                expected_size_bytes=source.size_bytes,
            )

            runtime_space_guard = extract_worker.build_runtime_space_guard(
                temp_root,
                required_total_bytes=command.required_temp_bytes,
                free_bytes_provider=free_bytes_provider,
            )
            runtime_space_guard("reader_snapshot_open")
            snapshot = RunSnapshot(snapshot_path)
            production_worker_factory = worker_factory_builder is None
            if production_worker_factory:
                worker_factory_builder = (
                    extract_worker.build_origin_extraction_worker_factory
                )
            origin_launch_path = temp_root / (
                f"origin_launch.{command.source_copy.source_id}."
                f"attempt{command.reader_attempt}.json"
            )
            origin_identity_path = temp_root / (
                f"origin_identity.{command.source_copy.source_id}."
                f"attempt{command.reader_attempt}.json"
            )
            origin_open_target_path = temp_root / (
                f"origin_open_target.{command.source_copy.source_id}."
                f"attempt{command.reader_attempt}.json"
            )
            if production_worker_factory:
                worker_factory = worker_factory_builder(
                    settings_snapshot=command.settings_snapshot,
                    origin_launch_path=origin_launch_path,
                    origin_identity_path=origin_identity_path,
                    origin_open_target_path=origin_open_target_path,
                    run_id=command.run_id,
                    marker_id=command.marker_id,
                    source_id=command.source_copy.source_id,
                    reader_attempt=command.reader_attempt,
                    cleanup_identity_callback=cleanup_identity_callback,
                    sidecar_auth_key=sidecar_auth_key,
                )
            else:
                worker_factory = worker_factory_builder(
                    settings_snapshot=command.settings_snapshot
                )
            source_manager = _ReaderCopySourceManager(source)
            extract_worker.ExtractionOrchestrator(
                snapshot,
                worker_factory,
                source_manager,
                max_attempts=1,
                first_attempt=command.reader_attempt,
                worker_shutdown_waiter=(
                    _production_worker_shutdown_waiter
                    if production_worker_factory
                    else None
                ),
                runtime_space_guard=runtime_space_guard,
                s1_limit=_confirmed_s1_limit(command.settings_snapshot),
                steady_emission_y=_confirmed_steady_emission_y(
                    command.settings_snapshot
                ),
                allow_missing_s1=_confirmed_allow_missing_s1(
                    command.settings_snapshot
                ),
            ).run((source,))
            source_manager.verify_copy(source.source_id)
            inventory_count = snapshot.inventory_count(source.source_id)
            result_count = snapshot.result_count(source.source_id)
            extracted_count = snapshot.status_count(source.source_id, "extracted")
            rejected_count = snapshot.status_count(source.source_id, "rejected")
            return ReaderSourceExtractionSummary(
                snapshot_path=snapshot_path,
                source_id=source.source_id,
                inventory_count=inventory_count,
                result_count=result_count,
                extracted_count=extracted_count,
                rejected_count=rejected_count,
            )
    except UnsupportedSourceReconciliationError as exc:
        raise UnsupportedSourceInputError(
            "未检测到受支持的 Origin 原始谱图。"
        ) from exc
    except CopyVerificationError as exc:
        raise ProductRunnerError(
            f"谱图提取 reader source copy 身份验证失败：{exc}"
        ) from exc


def _path_is_registered(path: Path, allowed_children: tuple[Path, ...]) -> bool:
    resolved = Path(path).resolve()
    return any(
        resolved == registered or registered in resolved.parents
        for registered in (Path(child).resolve() for child in allowed_children)
    )


def _production_worker_shutdown_waiter(source_id: str, attempt: int) -> None:
    del source_id, attempt
    deadline = time.monotonic() + 12.0
    consecutive_empty_probes = 0
    last_processes = ()
    last_probe_error = None
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            detail = ""
            if last_processes:
                pids = ", ".join(
                    str(getattr(item, "pid", "unknown"))
                    for item in last_processes
                )
                detail = f"；最后观测到的 Origin PID：{pids}"
            if last_probe_error is not None:
                detail += f"；最后一次进程检测失败：{last_probe_error}"
            raise ExtractionCleanupBlockedError(
                f"等待 Origin 退出超时，禁止清理临时文件{detail}"
            )
        try:
            processes = tuple(default_origin_process_probe(timeout=min(5.0, remaining)))
        except Exception as exc:
            consecutive_empty_probes = 0
            last_probe_error = exc
            time.sleep(min(0.1, remaining))
            continue
        last_probe_error = None
        if processes:
            last_processes = processes
            consecutive_empty_probes = 0
        else:
            consecutive_empty_probes += 1
            if consecutive_empty_probes >= 2:
                return
        time.sleep(min(0.1, remaining))
