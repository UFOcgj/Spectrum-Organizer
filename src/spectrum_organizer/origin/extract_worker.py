from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil

from spectrum_organizer.domain.extracted import (
    ExtractionSource,
    InventoryBook,
    TerminalBookResult,
)
from spectrum_organizer.origin.ipc_auth import (
    sidecar_content_hmac,
    validate_sidecar_auth_key,
)
from spectrum_organizer.runtime_audit import (
    record_runtime_audit_event,
    runtime_audit_enabled,
    runtime_audit_file_identity,
)
from spectrum_organizer.safety.identity_paths import (
    hold_file_identity,
    IdentityPathError,
    lexical_path_exists,
    path_identity,
    unlink_owned_path,
)


_ORIGIN_MODULE_NAME = "originpro"


class WorkerPreflightError(RuntimeError):
    pass


class InfrastructureExtractionError(RuntimeError):
    pass


class WorkerShutdownUnconfirmedError(RuntimeError):
    pass


class RuntimeSpaceError(WorkerPreflightError):
    pass


def validate_worker_open_target(
    path: Path,
    allowlist: set[Path],
    role: str,
    protected_paths: tuple[Path, ...] = (),
    allowed_children: tuple[Path, ...] = (),
    expected_sha256: str | None = None,
    expected_size_bytes: int | None = None,
) -> None:
    if role != "extraction":
        raise WorkerPreflightError("originpro may only be used by an extraction worker")
    if not allowed_children or (
        not protected_paths and (expected_sha256 is None or expected_size_bytes is None)
    ):
        raise WorkerPreflightError("Origin open target requires protected-path or verified-copy metadata")
    resolved = _path_key(path)
    allowed = {_path_key(item) for item in allowlist}
    if resolved not in allowed:
        raise WorkerPreflightError(f"Origin open target is not allowlisted: {path}")
    protected = {_path_key(item) for item in protected_paths}
    if resolved in protected:
        raise WorkerPreflightError(f"Origin open target is protected: {path}")
    for protected_path in protected_paths:
        try:
            if os.path.samefile(path, protected_path):
                raise WorkerPreflightError(f"Origin open target is protected: {path}")
        except FileNotFoundError as exc:
            raise WorkerPreflightError(f"Origin open target or protected path is missing: {exc}") from exc
        except OSError as exc:
            raise WorkerPreflightError(f"Origin open target identity check failed: {exc}") from exc
    if allowed_children and not any(_is_under(path, child) for child in allowed_children):
        raise WorkerPreflightError(f"Origin open target is outside owned temp children: {path}")
    if expected_sha256 is not None or expected_size_bytes is not None:
        try:
            if Path(path).stat().st_size != expected_size_bytes or _hash_path(Path(path)) != expected_sha256:
                raise WorkerPreflightError(f"Origin open target copy identity mismatch: {path}")
        except OSError as exc:
            raise WorkerPreflightError(f"Origin open target copy identity check failed: {exc}") from exc


class ExtractionOrchestrator:
    def __init__(
        self,
        snapshot,
        worker_factory,
        source_manager,
        max_attempts: int = 2,
        *,
        first_attempt: int = 1,
        worker_shutdown_waiter=None,
        runtime_space_guard=None,
        s1_limit: int | float | None = None,
        steady_emission_y: str | None = None,
        allow_missing_s1: bool = False,
    ):
        if not isinstance(max_attempts, int) or isinstance(max_attempts, bool) or max_attempts < 1:
            raise ValueError("max_attempts must be a positive integer")
        if not isinstance(first_attempt, int) or isinstance(first_attempt, bool) or first_attempt < 1:
            raise ValueError("first_attempt must be a positive integer")
        self.snapshot = snapshot
        self.worker_factory = worker_factory
        self.source_manager = source_manager
        self.max_attempts = max_attempts
        self.first_attempt = first_attempt
        self.worker_shutdown_waiter = worker_shutdown_waiter or (lambda source_id, attempt: None)
        self.runtime_space_guard = runtime_space_guard or self._default_runtime_space_guard
        self.s1_limit = s1_limit
        self.steady_emission_y = steady_emission_y
        self.allow_missing_s1 = allow_missing_s1

    def run(self, sources) -> None:
        for source in sources:
            self._run_source(source)

    def _run_source(self, source: ExtractionSource) -> None:
        source_id = source.source_id
        copy_path = Path(source.copy_path)
        protected_paths = tuple(Path(path) for path in source.protected_paths)
        if source.original_path is not None:
            protected_paths = (Path(source.original_path),) + protected_paths
        validate_worker_open_target(
            copy_path,
            {copy_path},
            role="extraction",
            protected_paths=protected_paths,
            allowed_children=tuple(Path(path) for path in source.allowed_children),
            expected_sha256=None if protected_paths else source.sha256,
            expected_size_bytes=None if protected_paths else source.size_bytes,
        )
        self._check_runtime_space("snapshot_add_source")
        has_original_provenance = (
            source.original_canonical_path is not None
            and source.size_bytes is not None
            and source.original_mtime_ns is not None
        )
        self.snapshot.add_source(
            source_id,
            copy_path,
            source.sha256,
            original_path=(
                source.original_canonical_path
                if has_original_provenance
                else None
            ),
            original_size_bytes=(
                source.size_bytes
                if has_original_provenance
                else None
            ),
            original_mtime_ns=(
                source.original_mtime_ns
                if has_original_provenance
                else None
            ),
        )
        attempt_failures: list[str] = []
        last_attempt = self.first_attempt + self.max_attempts - 1
        for attempt in range(self.first_attempt, self.first_attempt + self.max_attempts):
            self._check_runtime_space("snapshot_worker_attempt")
            self.snapshot.add_worker_attempt(source_id, attempt, "started", "")
            worker = None
            worker_closed = False
            close_error = None
            failure_recorded = False
            success_ready = False
            retry_requested = False
            authoritative_shutdown_error = None
            try:
                self.source_manager.verify_copy(source_id)
                worker = self.worker_factory.create(source_id, attempt)
                self._check_runtime_space("snapshot_discard_partition")
                self.snapshot.discard_source_partition(source_id)
                self._persist_worker_output(source_id, worker, copy_path)
                close_error = _close_worker_and_wait_safely(worker, self.worker_shutdown_waiter, source_id, attempt)
                worker_closed = True
                self.source_manager.verify_copy(source_id)
                self._check_runtime_space("snapshot_reconcile")
                self.snapshot.reconcile_source(
                    source_id,
                    s1_limit=self.s1_limit,
                    steady_emission_y=self.steady_emission_y,
                    allow_missing_s1=self.allow_missing_s1,
                )
                if close_error is not None:
                    _best_effort_failed_attempt(
                        self.snapshot,
                        source_id,
                        attempt,
                        f"worker close failed: {close_error}",
                    )
                    failure_recorded = True
                    raise close_error
                success_ready = True
            except InfrastructureExtractionError as exc:
                if not worker_closed:
                    close_error = _close_worker_and_wait_safely(worker, self.worker_shutdown_waiter, source_id, attempt)
                    worker_closed = True
                failure_message = _attempt_failure_message(exc, close_error)
                attempt_failures.append(f"attempt {attempt}: {failure_message}")
                if isinstance(close_error, WorkerShutdownUnconfirmedError):
                    authoritative_shutdown_error = close_error
                    _best_effort_failed_attempt(
                        self.snapshot,
                        source_id,
                        attempt,
                        failure_message,
                    )
                    failure_recorded = True
                    raise close_error
                if not failure_recorded:
                    _best_effort_failed_attempt(
                        self.snapshot,
                        source_id,
                        attempt,
                        failure_message,
                    )
                    failure_recorded = True
                try:
                    self.source_manager.discard_failed_copy(source_id)
                except Exception:
                    pass
                if attempt >= last_attempt:
                    raise InfrastructureExtractionError("; ".join(attempt_failures)) from exc
                retry_requested = True
            except Exception as exc:
                if not worker_closed:
                    close_error = _close_worker_and_wait_safely(worker, self.worker_shutdown_waiter, source_id, attempt)
                    worker_closed = True
                if not failure_recorded:
                    _best_effort_failed_attempt(
                        self.snapshot,
                        source_id,
                        attempt,
                        _attempt_failure_message(exc, close_error),
                    )
                    failure_recorded = True
                if isinstance(close_error, WorkerShutdownUnconfirmedError):
                    authoritative_shutdown_error = close_error
                    if authoritative_shutdown_error is exc:
                        raise
                    raise authoritative_shutdown_error from exc
                raise
            finally:
                verify_original = getattr(self.source_manager, "verify_original", None)
                if verify_original is not None:
                    try:
                        verify_original(source_id)
                    except Exception as verify_exc:
                        message = f"original source verification failed: {verify_exc}"
                        if not failure_recorded:
                            _best_effort_failed_attempt(self.snapshot, source_id, attempt, message)
                        if authoritative_shutdown_error is not None:
                            authoritative_shutdown_error.add_note(message)
                        else:
                            raise
            if success_ready:
                try:
                    self._check_runtime_space("snapshot_worker_attempt")
                    self.snapshot.add_worker_attempt(source_id, attempt, "succeeded", "")
                except Exception as exc:
                    _best_effort_failed_attempt(
                        self.snapshot,
                        source_id,
                        attempt,
                        f"succeeded audit failed: {exc}",
                    )
                    raise
                return
            if retry_requested:
                refreshed_copy = self.source_manager.refresh_copy(source_id)
                if refreshed_copy is not None:
                    copy_path = Path(refreshed_copy)
                    validate_worker_open_target(
                        copy_path,
                        {copy_path},
                        role="extraction",
                        protected_paths=protected_paths,
                        allowed_children=tuple(Path(path) for path in source.allowed_children),
                        expected_sha256=None if protected_paths else source.sha256,
                        expected_size_bytes=None if protected_paths else source.size_bytes,
                    )
                    self._check_runtime_space("snapshot_update_copy")
                    self.snapshot.update_source_copy_path(source_id, copy_path)
                continue

    def _check_runtime_space(self, operation: str) -> None:
        self.runtime_space_guard(operation)

    def _default_runtime_space_guard(self, _operation: str) -> None:
        snapshot_path = Path(self.snapshot.path)
        available = shutil.disk_usage(snapshot_path.parent).free
        reserve = 64 * 1024 * 1024
        if available < reserve:
            raise RuntimeSpaceError(
                f"Runtime temporary space is insufficient at {snapshot_path.parent}: "
                f"required reserve {reserve}, available {available}"
            )
    def _persist_worker_output(self, source_id: str, worker: object, copy_path: Path) -> None:
        inventory_iterator = getattr(worker, "iter_inventory", None)
        result_iterator = getattr(worker, "iter_book_results", None)
        if not callable(inventory_iterator) or not callable(result_iterator):
            raise WorkerPreflightError("Extraction worker must provide streaming inventory and Book-result passes")
        inventory = iter(inventory_iterator(copy_path, {copy_path}))
        while True:
            self._check_runtime_space("inventory_read")
            try:
                book = next(inventory)
            except StopIteration:
                break
            self._check_runtime_space("snapshot_inventory")
            self.snapshot.record_inventory_book(source_id, book)
        results = iter(result_iterator())
        while True:
            self._check_runtime_space("result_read")
            try:
                book, result = next(results)
            except StopIteration:
                break
            if book.identity != result.identity:
                raise WorkerPreflightError("Streaming Book result identity does not match its inventory Book")
            self._check_runtime_space("snapshot_result")
            self.snapshot.record_book_result(result, pass_two_book=book)


def build_runtime_space_guard(
    temp_root: Path,
    *,
    required_total_bytes: int,
    free_bytes_provider=None,
):
    root = Path(temp_root).resolve()
    if required_total_bytes < 0:
        raise ValueError("required_total_bytes cannot be negative")

    def guard(operation: str) -> None:
        materialized = _materialized_bytes(root)
        remaining = max(0, required_total_bytes - materialized)
        available = (
            int(free_bytes_provider(root))
            if free_bytes_provider is not None
            else shutil.disk_usage(root).free
        )
        if available < remaining:
            raise RuntimeSpaceError(
                f"Runtime temporary space is insufficient during {operation} at {root}: "
                f"required remaining {remaining}, available {available}"
            )

    return guard


def _materialized_bytes(root: Path) -> int:
    total = 0
    for directory, _subdirectories, filenames in os.walk(root):
        for filename in filenames:
            try:
                total += (Path(directory) / filename).stat().st_size
            except FileNotFoundError:
                continue
    return total


def _hash_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def build_origin_extraction_worker_factory(
    *,
    settings_snapshot: dict[str, object] | None = None,
    s1_limit: int | None = None,
    steady_emission_y: str | None = None,
    allow_missing_s1: bool | None = None,
    origin_launch_path: Path | None = None,
    origin_identity_path: Path | None = None,
    origin_open_target_path: Path | None = None,
    run_id: str | None = None,
    marker_id: str | None = None,
    source_id: str | None = None,
    reader_attempt: int | None = None,
    cleanup_identity_callback=None,
    sidecar_auth_key: str | None = None,
):
    from spectrum_organizer.origin.session_adapters import OriginExtractionWorkerFactory

    if settings_snapshot is not None:
        s1_limit = int(settings_snapshot["s1Limit"])
        steady_emission_y = str(settings_snapshot["steadyEmissionY"])
        allow_missing_s1 = bool(settings_snapshot.get("allowMissingS1", False))
    pid_helper_path = (
        None if origin_identity_path is None else Path(origin_identity_path).with_suffix(".c")
    )
    helper_identity = None
    launch_identity = None
    if sidecar_auth_key is not None:
        validate_sidecar_auth_key(sidecar_auth_key)

    def record_launch_baseline():
        nonlocal helper_identity, launch_identity
        launch_identity, helper_identity = _record_origin_launch_baseline(
            origin_launch_path,
            helper_path=pid_helper_path,
            run_id=run_id,
            marker_id=marker_id,
            source_id=source_id,
            reader_attempt=reader_attempt,
            cleanup_identity_callback=cleanup_identity_callback,
            auth_key=sidecar_auth_key,
        )

    def record_owned_origin(origin):
        _record_owned_origin_identity(
            origin_launch_path,
            origin_identity_path,
            origin,
            launch_identity=launch_identity,
            helper_path=pid_helper_path,
            helper_identity=helper_identity,
            run_id=run_id,
            marker_id=marker_id,
            source_id=source_id,
            reader_attempt=reader_attempt,
            cleanup_identity_callback=cleanup_identity_callback,
            auth_key=sidecar_auth_key,
        )

    return OriginExtractionWorkerFactory(
        _load_origin_session,
        s1_limit=2_000_000 if s1_limit is None else s1_limit,
        steady_emission_y="S1c" if steady_emission_y is None else steady_emission_y,
        allow_missing_s1=False if allow_missing_s1 is None else allow_missing_s1,
        before_origin_launch=(
            None
            if origin_launch_path is None
            else record_launch_baseline
        ),
        after_origin_launch=(
            None
            if origin_launch_path is None or origin_identity_path is None
            else record_owned_origin
        ),
        after_project_open=(
            None
            if origin_open_target_path is None
            else lambda open_target: _record_origin_open_target(
                origin_open_target_path,
                open_target,
                run_id=run_id,
                marker_id=marker_id,
                source_id=source_id,
                reader_attempt=reader_attempt,
                cleanup_identity_callback=cleanup_identity_callback,
                auth_key=sidecar_auth_key,
            )
        ),
    )


def _origin_sidecar_binding(*, run_id, marker_id, source_id, reader_attempt) -> dict[str, object]:
    if (
        not isinstance(run_id, str)
        or not run_id
        or not isinstance(marker_id, str)
        or not marker_id
        or not isinstance(source_id, str)
        or not source_id
        or isinstance(reader_attempt, bool)
        or not isinstance(reader_attempt, int)
        or reader_attempt not in {1, 2}
    ):
        raise WorkerPreflightError("Origin 进程身份记录缺少当前 reader 任务绑定")
    return {
        "schema_version": 1,
        "run_id": run_id,
        "marker_id": marker_id,
        "source_id": source_id,
        "reader_attempt": reader_attempt,
    }


def _write_owned_json_atomic(
    path: Path,
    payload: dict[str, object],
    *,
    cleanup_identity_callback=None,
    auth_key: str | None = None,
) -> tuple[int, int]:
    final_path = Path(path)
    pending_path = final_path.with_name(f"{final_path.name}.pending")
    if lexical_path_exists(final_path):
        raise FileExistsError(final_path)
    pending_created = False
    pending_identity = None
    try:
        with pending_path.open("x", encoding="utf-8") as stream:
            pending_created = True
            status = os.fstat(stream.fileno())
            pending_identity = (status.st_dev, status.st_ino)
            document = dict(payload)
            document["creation_identity"] = list(pending_identity)
            if auth_key is not None:
                document["content_hmac"] = sidecar_content_hmac(
                    document,
                    auth_key,
                )
            json.dump(document, stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
            os.link(pending_path, final_path)
            if path_identity(final_path) != pending_identity:
                raise IdentityPathError(
                    final_path,
                    f"Published Origin sidecar identity changed: {final_path}",
                )
            if cleanup_identity_callback is not None:
                cleanup_identity_callback(final_path, pending_identity)
    except BaseException as exc:
        if pending_created and pending_identity is not None:
            try:
                unlink_owned_path(pending_path, pending_identity)
            except (OSError, IdentityPathError) as cleanup_exc:
                retained = list(getattr(exc, "retained_owned_identities", ()))
                retained.append((pending_path, pending_identity))
                exc.retained_owned_identities = tuple(retained)
                if cleanup_identity_callback is not None:
                    cleanup_identity_callback(pending_path, pending_identity)
                exc.add_note(str(cleanup_exc))
        raise
    try:
        unlink_owned_path(pending_path, pending_identity)
    except (OSError, IdentityPathError) as exc:
        exc.retained_owned_identities = ((pending_path, pending_identity),)
        if cleanup_identity_callback is not None:
            cleanup_identity_callback(pending_path, pending_identity)
        raise
    return pending_identity


def _record_origin_open_target(
    path: Path,
    open_target: Path,
    *,
    run_id,
    marker_id,
    source_id,
    reader_attempt,
    cleanup_identity_callback=None,
    auth_key: str | None = None,
) -> None:
    payload = _origin_sidecar_binding(
        run_id=run_id,
        marker_id=marker_id,
        source_id=source_id,
        reader_attempt=reader_attempt,
    )
    payload["open_target"] = str(Path(open_target).resolve())
    _write_owned_json_atomic(
        path,
        payload,
        cleanup_identity_callback=cleanup_identity_callback,
        auth_key=auth_key,
    )
    target_identities = (
        [runtime_audit_file_identity(open_target)]
        if runtime_audit_enabled()
        else []
    )
    record_runtime_audit_event(
        "origin_worker_targets",
        {
            "role": "extraction",
            "run_id": run_id,
            "source_id": source_id,
            "reader_attempt": reader_attempt,
            "open_targets": [payload["open_target"]],
            "open_target_identities": target_identities,
        },
    )


def _record_origin_launch_baseline(
    path: Path,
    *,
    helper_path: Path | None = None,
    run_id,
    marker_id,
    source_id,
    reader_attempt,
    cleanup_identity_callback=None,
    auth_key: str | None = None,
) -> tuple[tuple[int, int], tuple[int, int] | None]:
    from spectrum_organizer.safety.process_boundary import default_origin_process_probe

    processes = tuple(default_origin_process_probe(timeout=5.0))
    payload = {
        **_origin_sidecar_binding(
            run_id=run_id,
            marker_id=marker_id,
            source_id=source_id,
            reader_attempt=reader_attempt,
        ),
        "processes": [
            {"pid": process.pid, "start_time_ns": process.start_time_ns}
            for process in processes
        ]
    }
    if processes:
        payload["launch_state"] = "prelaunch_rejected"
        _write_owned_json_atomic(
            path,
            payload,
            cleanup_identity_callback=cleanup_identity_callback,
            auth_key=auth_key,
        )
        raise WorkerPreflightError("Origin worker 启动前检测到已有 Origin 进程")
    helper_identity = None
    if helper_path is not None:
        helper = Path(helper_path)
        try:
            with helper.open("x", encoding="ascii", newline="\n") as stream:
                status = os.fstat(stream.fileno())
                helper_identity = (status.st_dev, status.st_ino)
                stream.write(
                    "#include <Origin.h>\n\n"
                    "int spectrum_organizer_current_pid()\n"
                    "{\n"
                    "    return (int)GetCurrentProcessId();\n"
                    "}\n"
                )
                stream.flush()
                os.fsync(stream.fileno())
                if path_identity(helper) != helper_identity:
                    raise IdentityPathError(
                        helper,
                        f"Origin PID helper identity changed: {helper}",
                    )
                if cleanup_identity_callback is not None:
                    cleanup_identity_callback(helper, helper_identity)
        except BaseException as exc:
            if helper_identity is not None:
                try:
                    unlink_owned_path(helper, helper_identity)
                except (OSError, IdentityPathError) as cleanup_exc:
                    exc.add_note(str(cleanup_exc))
            raise
    payload["launch_state"] = "launch_allowed"
    launch_identity = _write_owned_json_atomic(
        path,
        payload,
        cleanup_identity_callback=cleanup_identity_callback,
        auth_key=auth_key,
    )
    return launch_identity, helper_identity


def _record_owned_origin_identity(
    launch_path: Path,
    identity_path: Path,
    origin,
    *,
    launch_identity: tuple[int, int] | None,
    helper_path: Path | None,
    helper_identity: tuple[int, int] | None,
    run_id,
    marker_id,
    source_id,
    reader_attempt,
    cleanup_identity_callback=None,
    auth_key: str | None = None,
) -> None:
    from spectrum_organizer.safety.process_boundary import default_origin_process_probe

    launch = Path(launch_path)
    if launch_identity is None:
        raise WorkerPreflightError("Origin 启动基线创建身份无效")
    with hold_file_identity(
        launch,
        launch_identity,
        allow_write=False,
    ):
        baseline = json.loads(launch.read_text(encoding="utf-8"))
    if tuple(baseline.get("creation_identity", ())) != launch_identity:
        raise WorkerPreflightError("Origin 启动基线创建身份无效")
    if auth_key is not None and (
        baseline.get("content_hmac")
        != sidecar_content_hmac(baseline, auth_key)
    ):
        raise WorkerPreflightError("Origin 启动基线内容认证失败")
    baseline_identities = {
        (int(item["pid"]), int(item["start_time_ns"]))
        for item in baseline.get("processes", ())
    }
    if helper_path is None or helper_identity is None or not Path(helper_path).is_file():
        raise WorkerPreflightError("Origin 会话 PID helper 不存在")
    with hold_file_identity(
        Path(helper_path),
        helper_identity,
        allow_write=False,
    ):
        helper_text = str(Path(helper_path).resolve()).replace("\\", "\\\\").replace('"', '\\"')
        load_result = origin.lt_int(f'run.LoadOC("{helper_text}", 0)')
    if load_result != 0:
        raise WorkerPreflightError(f"Origin 会话 PID helper 加载失败：{load_result}")
    pid = origin.lt_int("spectrum_organizer_current_pid()")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise WorkerPreflightError("Origin 会话未返回有效 PID")
    current = tuple(default_origin_process_probe(timeout=5.0))
    matches = tuple(process for process in current if process.pid == pid)
    if len(matches) != 1:
        raise WorkerPreflightError(f"无法核验本次 Origin 会话 PID：{pid}")
    process = matches[0]
    if (process.pid, process.start_time_ns) in baseline_identities:
        raise WorkerPreflightError("本次 Origin 会话身份已存在于启动前基线")
    if process.visible or process.taskbar_visible:
        raise WorkerPreflightError("本次任务创建的 Origin 进程意外变为可见状态")
    _write_owned_json_atomic(
        identity_path,
        {
            **_origin_sidecar_binding(
                run_id=run_id,
                marker_id=marker_id,
                source_id=source_id,
                reader_attempt=reader_attempt,
            ),
            "pid": process.pid,
            "start_time_ns": process.start_time_ns,
        },
        cleanup_identity_callback=cleanup_identity_callback,
        auth_key=auth_key,
    )
    if runtime_audit_enabled():
        record_runtime_audit_event(
            "origin_process_identity",
            {
                "role": "extraction",
                "pid": process.pid,
                "start_time_ns": process.start_time_ns,
                "attempt_binding": {
                    "run_id": run_id,
                    "source_id": source_id,
                    "reader_attempt": reader_attempt,
                },
            },
        )


def _close_worker(worker) -> None:
    close = getattr(worker, "close", None)
    if close is not None:
        close()


def _close_worker_safely(worker) -> Exception | None:
    try:
        _close_worker(worker)
    except Exception as exc:
        return exc
    return None


def _close_worker_and_wait_safely(worker, waiter, source_id: str, attempt: int) -> Exception | None:
    close_error = _close_worker_safely(worker)
    try:
        waiter(source_id, attempt)
    except Exception as exc:
        message = f"Origin worker shutdown was not confirmed: {exc}"
        if close_error is not None:
            message = f"{close_error}; {message}"
        return WorkerShutdownUnconfirmedError(message)
    return close_error


def _attempt_failure_message(exc: Exception, close_error: Exception | None) -> str:
    message = str(exc)
    if close_error is not None:
        return f"{message}; worker close failed: {close_error}"
    return message


def _best_effort_failed_attempt(snapshot, source_id: str, attempt: int, message: str) -> None:
    try:
        snapshot.discard_source_partition(source_id)
    except Exception:
        pass
    try:
        snapshot.add_worker_attempt(source_id, attempt, "failed", message)
    except Exception:
        pass


def _load_origin_session():
    origin = __import__(_ORIGIN_MODULE_NAME)
    try:
        if getattr(origin, "oext", False):
            origin.po.LT_execute("sec -poc")
        origin.set_show(False)
    except Exception:
        try:
            origin.exit()
        except Exception:
            pass
        raise
    return origin


def _path_key(path: Path) -> str:
    return os.path.normcase(str(Path(path).resolve()))


def _is_under(path: Path, parent: Path) -> bool:
    resolved_path = Path(path).resolve()
    resolved_parent = Path(parent).resolve()
    return resolved_path == resolved_parent or resolved_parent in resolved_path.parents
