from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
import html
import inspect
import json
import os
from pathlib import Path
import re
import sys
import threading
import traceback
from time import monotonic
from typing import Any, Callable

from spectrum_organizer.app_paths import ensure_app_paths
from spectrum_organizer.core.attribution import (
    AttributionBook,
    AttributionCache,
    AttributionSession,
    build_attribution_fields,
    build_attribution_targets,
    reconcile_concentration_prefill,
    reconcile_oxygen_environment_prefill,
    reconcile_temperature_prefill,
    split_folder_target,
)
from spectrum_organizer.core.audit_details import (
    canonical_audit_detail,
    measurement_text as _canonical_measurement_text,
    selection_exclusion_detail,
)
from spectrum_organizer.core.metadata_numeric import format_raw_slit_fields
from spectrum_organizer.core.output_model import OutputSpectrum, build_output_plan
from spectrum_organizer.core.selection import (
    CandidateConversionError,
    SelectionExclusion,
    SelectionSpectrum,
    convert_extracted_results,
    filter_copyable_emissions_after_special,
    format_maximum_x,
    review_emission_duplicates,
    select_excitation_candidates,
)
from spectrum_organizer.core.special_groups import (
    OVERLAP_CHOICES,
    SpectrumBook,
    classify_special_groups,
    resolve_special_group_selection,
    spectrum_book_point_identity,
)
from spectrum_organizer.domain.models import (
    DopedSample,
    LiquidSample,
    NeatSample,
    SpectrumClass,
)
from spectrum_organizer.product_runner import (
    ApprovedAttribution,
    ApprovedAuditItem,
    ApprovedBookIdentity,
    ApprovedReviewChoice,
    ApprovedReviewRequirement,
    AllSelectedSourcesInvalidError,
    CountReconciliation,
    ExtractionCleanupBlockedError,
    ProductRunnerError,
    SourceInputIssue,
    _cleanup_temp_root_error,
    _confirmed_allow_missing_s1,
    _confirmed_s1_limit,
    _confirmed_steady_emission_y,
    approve_output_plan,
    complete_pre_extraction_origin_process_gate,
    prepare_approved_pre_extraction_context,
)
from spectrum_organizer.reporting.run_report import (
    build_approved_output_report,
    build_final_output_plan_summary,
)
from spectrum_organizer.reporting.publication import (
    cleanup_owned_staging,
    create_run_staging,
    publish_completed_run,
    register_staging_artifact_identity,
    reserve_staging_artifact_identity,
    remove_run_owned_artifact,
    retry_post_commit_cleanup,
    write_failure_log,
)
from spectrum_organizer.safety.fingerprints import (
    disambiguated_source_labels,
    snapshot_sources,
)
from spectrum_organizer.safety.name_policy import (
    GeneratedBookNamePolicyError,
    NamePolicyError,
)
from spectrum_organizer.safety.process_boundary import WindowsOriginProcessController, default_origin_process_probe
from spectrum_organizer.settings import Notice, Settings, SettingsStore
from spectrum_organizer.store.run_snapshot import (
    load_book_payloads_read_only,
    load_book_results_read_only,
)
from spectrum_organizer.store.sample_library import SampleLibrary, SampleLibraryError
from spectrum_organizer.ui.dialog_port import (
    ORGANIZER_DIALOG_STYLE_SHEET,
    AttributionBookSelectionRequest,
    AttributionDialogRequest,
    AttributionDialogResponse,
    ConflictReviewChoice,
    ConflictReviewGroup,
    ConflictReviewRequest,
    QtAttributionDialogPort,
    QtConflictReviewDialogPort,
    QtManualDialogPort,
    _make_windows_taskbar_window,
    apply_combo_popup_palette,
    apply_styled_dialog_chrome,
    configure_workflow_button,
    enable_title_bar_drag as _enable_dialog_drag,
    _format_conflict_difference_text,
    partition_conflict_choices,
    restore_dialog_position,
    show_styled_dialog,
)
from spectrum_organizer.ui.dialogs import (
    DialogRequest,
    FinalReviewConflictChoice,
    FinalReviewConflictEditor,
    FinalReviewConflictGroup,
    FinalReviewConflictSelection,
    FinalReviewOutputBook,
    FinalReviewOutputFolder,
    FinalReviewRow,
    FinalReviewViewState,
    cancel_and_exit_confirmation_dialog,
    cancelled_and_exited_dialog,
    database_recovery_dialog,
    final_attribution_summary_dialog,
)
from spectrum_organizer.ui.orchestrator import BookOnlyOrchestrator, SourceSelectionResult
from spectrum_organizer.ui.output_stage import (
    OutputStageUiCoordinator,
    output_cleanup_is_blocked,
    output_failure_diagnostics,
)
from spectrum_organizer.ui.qt_main_window import create_production_main_window, update_production_runtime_view
from spectrum_organizer.workflow.output_pipeline import (
    OutputPipelineCancelled,
)


def _invoke_candidate_loader(
    candidate_loader,
    extraction_summary,
    cancel_check,
    settings_snapshot=None,
):
    try:
        parameters = inspect.signature(candidate_loader).parameters
    except (TypeError, ValueError):
        return candidate_loader(
            extraction_summary,
            cancel_check=cancel_check,
            settings_snapshot=settings_snapshot,
        )
    accepts_keyword = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )
    kwargs = {}
    for name, value in (
        ("cancel_check", cancel_check),
        ("settings_snapshot", settings_snapshot),
    ):
        parameter = parameters.get(name)
        if accepts_keyword or (
            parameter is not None
            and parameter.kind is not inspect.Parameter.POSITIONAL_ONLY
        ):
            kwargs[name] = value
    return candidate_loader(extraction_summary, **kwargs)


def _open_directory(path: Path) -> None:
    os.startfile(str(path))


def _invoke_preflight_confirm(
    preflight_dialog,
    parent,
    *,
    default_s1_limit,
    steady_emission_y,
    allow_missing_s1,
):
    confirm = preflight_dialog.confirm
    kwargs = {
        "default_s1_limit": default_s1_limit,
        "steady_emission_y": steady_emission_y,
        "allow_missing_s1": allow_missing_s1,
    }
    try:
        parameters = inspect.signature(confirm).parameters
    except (TypeError, ValueError):
        return confirm(parent, **kwargs)
    if not any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    ):
        kwargs = {
            name: value
            for name, value in kwargs.items()
            if name in parameters
            and parameters[name].kind is not inspect.Parameter.POSITIONAL_ONLY
        }
    return confirm(parent, **kwargs)


@dataclass
class QtFileDialogs:
    qt_widgets: Any
    initial_output_parent: str = ""

    def set_initial_output_parent(self, path: str) -> None:
        self.initial_output_parent = str(path or "")

    def select_origin_sources(self, parent) -> list[str]:
        paths, _ = self.qt_widgets.QFileDialog.getOpenFileNames(
            parent,
            "选择 Origin 原始文件",
            "",
            "Origin projects (*.opj *.opju)",
        )
        return list(paths)

    def select_output_parent(self, parent) -> str:
        return self.qt_widgets.QFileDialog.getExistingDirectory(
            parent,
            "选择输出位置",
            self.initial_output_parent,
        )


@dataclass
class QtMessageBoxPort:
    qt_widgets: Any

    def blocking_error(self, parent, *, title: str, message: str) -> None:
        show_styled_dialog(
            DialogRequest(kind="blocking_error", title=title, message=message, actions=("acknowledge",)),
            parent=parent,
        )


class CancellableStartRunJob:
    def __init__(
        self,
        context_builder,
        extraction_runner,
        *,
        pre_origin_process_check=None,
        candidate_loader=None,
    ):
        self.context_builder = context_builder
        self.extraction_runner = extraction_runner
        self.pre_origin_process_check = pre_origin_process_check
        self.candidate_loader = candidate_loader
        self._cancelled = threading.Event()
        self._progress_callback = None
        self._cleanup_temp_root: Path | None = None
        self._cleanup_temp_root_identity: tuple[int, int] | None = None

    def set_progress_callback(self, callback) -> None:
        self._progress_callback = callback
        setter = getattr(self.extraction_runner, "set_progress_callback", None)
        if callable(setter):
            setter(callback)

    def prepare(self) -> None:
        self._cancelled.clear()
        reset_builder = getattr(self.context_builder, "reset", None)
        if callable(reset_builder):
            reset_builder()
        reset = getattr(self.extraction_runner, "reset", None)
        if callable(reset):
            reset()

    def cancel(self) -> None:
        self._cancelled.set()
        cancel_builder = getattr(self.context_builder, "cancel", None)
        if callable(cancel_builder):
            cancel_builder()
        cancel = getattr(self.extraction_runner, "cancel", None)
        if callable(cancel):
            cancel()

    def _raise_if_cancelled(self) -> None:
        if self._cancelled.is_set():
            raise ProductRunnerError("谱图数据提取已取消")

    def retry_cleanup(self) -> None:
        attempted = False
        for component in (self.context_builder, self.extraction_runner):
            retry = getattr(component, "retry_cancel_cleanup", None)
            if not callable(retry):
                continue
            retry()
            attempted = True
        if self._cleanup_temp_root is not None:
            attempted = True
            cleanup_error = _cleanup_temp_root_error(
                self._cleanup_temp_root,
                expected_root_identity=self._cleanup_temp_root_identity,
            )
            if cleanup_error is not None:
                raise ExtractionCleanupBlockedError(
                    f"取消后临时文件清理失败：{cleanup_error}"
                )
            self._cleanup_temp_root = None
            self._cleanup_temp_root_identity = None
        if not attempted:
            raise ExtractionCleanupBlockedError("当前谱图提取清理无法重试")

    def __call__(self, approved_inputs):
        context = None
        try:
            context = self.context_builder(
                selected_source_paths=approved_inputs.selected_source_paths,
                output_parent=approved_inputs.output_parent,
                settings_snapshot=approved_inputs.settings_snapshot,
            )
            self._raise_if_cancelled()
            if self.pre_origin_process_check is not None:
                self.pre_origin_process_check()
            self._raise_if_cancelled()
            if self._progress_callback is not None:
                self._progress_callback({"kind": "pre_extraction_completed"})
            result = self.extraction_runner(context)
            self._raise_if_cancelled()
            if self.candidate_loader is None:
                return context, result
            if self._progress_callback is not None:
                self._progress_callback({"kind": "candidate_validation_started"})
            conversion = _invoke_candidate_loader(
                self.candidate_loader,
                result,
                self._raise_if_cancelled,
                approved_inputs.settings_snapshot,
            )
            self._raise_if_cancelled()
            return context, result, conversion
        except BaseException as exc:
            if isinstance(exc, ExtractionCleanupBlockedError):
                self._cleanup_temp_root = getattr(context, "temp_root", None)
                self._cleanup_temp_root_identity = getattr(
                    context,
                    "temp_root_identity",
                    None,
                )
                raise
            cleanup_error = _cleanup_temp_root_error(
                getattr(context, "temp_root", None),
                expected_root_identity=getattr(
                    context,
                    "temp_root_identity",
                    None,
                ),
            )
            if cleanup_error is not None:
                self._cleanup_temp_root = getattr(context, "temp_root", None)
                self._cleanup_temp_root_identity = getattr(
                    context,
                    "temp_root_identity",
                    None,
                )
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    exc.add_note(
                        f"临时文件清理失败：{cleanup_error}"
                    )
                    raise
                raise ExtractionCleanupBlockedError(
                    f"{exc}; 临时文件清理失败：{cleanup_error}"
                ) from exc
            raise


class QtThreadedExtractionRunner:
    def __init__(self, qt_core: Any, extraction_func):
        self.qt_core = qt_core
        self.extraction_func = extraction_func
        self._threads: list[Any] = []

    def start(self, context, on_success, on_error) -> None:
        qt_core = self.qt_core
        extraction_func = self.extraction_func

        class ExtractionThread(qt_core.QThread):
            succeeded = qt_core.Signal(object)
            failed = qt_core.Signal(object)

            def run(self):
                try:
                    self.succeeded.emit(extraction_func(context))
                except BaseException as exc:
                    self.failed.emit(exc)

        thread = ExtractionThread()
        thread.succeeded.connect(on_success)
        thread.failed.connect(on_error)
        thread.finished.connect(lambda: self._forget_thread(thread))
        delete_later = getattr(thread, "deleteLater", None)
        if delete_later is not None:
            thread.finished.connect(delete_later)
        self._threads.append(thread)
        try:
            thread.start()
        except BaseException:
            self._forget_thread(thread)
            if callable(delete_later):
                delete_later()
            raise

    def _forget_thread(self, thread) -> None:
        if thread in self._threads:
            self._threads.remove(thread)


@dataclass
class QtThreadedStartRunRunner:
    def __init__(self, qt_core: Any, run_func, *, cancel_func=None):
        self.qt_core = qt_core
        self.run_func = run_func
        self.cancel_func = cancel_func or getattr(run_func, "cancel", None)
        self._threads: list[Any] = []
        self._stopped_callbacks: list[Any] = []

    def start(self, approved_inputs, on_success, on_error, on_progress=None) -> None:
        qt_core = self.qt_core
        run_func = self.run_func
        prepare = getattr(run_func, "prepare", None)
        if callable(prepare):
            prepare()

        class StartRunThread(qt_core.QThread):
            progress = qt_core.Signal(object)
            result = None
            error = None

            def run(self):
                try:
                    self.result = run_func(approved_inputs)
                except BaseException as exc:
                    self.error = exc

        thread = StartRunThread()
        set_progress_callback = getattr(run_func, "set_progress_callback", None)
        if callable(set_progress_callback):
            set_progress_callback(thread.progress.emit)
        if on_progress is not None:
            thread.progress.connect(on_progress)
        thread.finished.connect(
            lambda: on_error(thread.error) if thread.error is not None else on_success(thread.result)
        )
        delete_later = getattr(thread, "deleteLater", None)
        if delete_later is not None:
            thread.finished.connect(delete_later)
        thread.finished.connect(lambda: self._forget_thread(thread))
        self._threads.append(thread)
        try:
            thread.start()
        except BaseException:
            self._forget_thread(thread)
            if callable(delete_later):
                delete_later()
            raise

    def cancel(self, on_stopped=None) -> bool:
        if not self._threads:
            return False
        if on_stopped is not None:
            self._stopped_callbacks.append(on_stopped)
        if callable(self.cancel_func):
            self.cancel_func()
        for thread in tuple(self._threads):
            request_interruption = getattr(thread, "requestInterruption", None)
            if callable(request_interruption):
                request_interruption()
        return True

    def retry_cleanup(self, on_finished) -> bool:
        retry = getattr(self.run_func, "retry_cleanup", None)
        if not callable(retry) or self._threads:
            return False
        qt_core = self.qt_core

        class CleanupRetryThread(qt_core.QThread):
            error = None

            def run(self):
                try:
                    retry()
                except BaseException as exc:
                    self.error = exc

        thread = CleanupRetryThread()
        thread.finished.connect(lambda: on_finished(thread.error))
        delete_later = getattr(thread, "deleteLater", None)
        if delete_later is not None:
            thread.finished.connect(delete_later)
        thread.finished.connect(lambda: self._forget_thread(thread))
        self._threads.append(thread)
        try:
            thread.start()
        except BaseException:
            self._forget_thread(thread)
            if callable(delete_later):
                delete_later()
            raise
        return True

    def _forget_thread(self, thread) -> None:
        if thread in self._threads:
            self._threads.remove(thread)
        if self._threads:
            return
        callbacks = tuple(self._stopped_callbacks)
        self._stopped_callbacks.clear()
        for callback in callbacks:
            callback()


class _Task8OperationCancelled(ProductRunnerError):
    pass


class _InlineTask8Runner:
    def start(self, operation, on_success, on_error) -> None:
        try:
            result = operation(lambda: None)
        except BaseException as exc:
            on_error(exc)
            return
        on_success(result)

    def cancel(self, on_stopped=None) -> bool:
        return False


class QtThreadedTask8Runner:
    def __init__(self, qt_core: Any):
        self.qt_core = qt_core
        self._cancelled = threading.Event()
        self._threads: list[Any] = []
        self._stopped_callbacks: list[Any] = []

    def start(self, operation, on_success, on_error) -> None:
        if self._threads:
            raise RuntimeError("Task 8 background operation is already running")
        self._cancelled.clear()
        qt_core = self.qt_core

        class Task8Thread(qt_core.QThread):
            result = None
            error = None

            def run(thread_self):
                try:
                    thread_self.result = operation(
                        self._raise_if_cancelled
                    )
                except BaseException as exc:
                    thread_self.error = exc

        thread = Task8Thread()
        thread.finished.connect(
            lambda: self._complete_thread(
                thread,
                on_success,
                on_error,
            )
        )
        delete_later = getattr(thread, "deleteLater", None)
        if callable(delete_later):
            thread.finished.connect(delete_later)
        self._threads.append(thread)
        try:
            thread.start()
        except BaseException:
            self._forget_thread(thread)
            if callable(delete_later):
                delete_later()
            raise

    def cancel(self, on_stopped=None) -> bool:
        if not self._threads:
            return False
        if on_stopped is not None:
            self._stopped_callbacks.append(on_stopped)
        self._cancelled.set()
        for thread in tuple(self._threads):
            request_interruption = getattr(
                thread,
                "requestInterruption",
                None,
            )
            if callable(request_interruption):
                request_interruption()
        return True

    def _raise_if_cancelled(self) -> None:
        if self._cancelled.is_set():
            raise _Task8OperationCancelled(
                "最终输出审核已取消"
            )

    def _complete_thread(self, thread, on_success, on_error) -> None:
        self._forget_thread(thread)
        if thread.error is not None:
            on_error(thread.error)
        else:
            on_success(thread.result)

    def _forget_thread(self, thread) -> None:
        if thread in self._threads:
            self._threads.remove(thread)
        if self._threads:
            return
        callbacks = tuple(self._stopped_callbacks)
        self._stopped_callbacks.clear()
        for callback in callbacks:
            callback()


@dataclass(frozen=True)
class _Task8NoUsableOutput:
    rejections: tuple[ApprovedAuditItem, ...]
    exclusions: tuple[ApprovedAuditItem, ...]


@dataclass(frozen=True)
class _Task8ReturnToAttribution:
    extraction_summary: Any
    conversion: Any
    book_keys: tuple[str, ...]


@dataclass(frozen=True)
class _Task8ReviewDraft:
    extraction_summary: Any
    conversion: Any
    output_spectra: tuple[OutputSpectrum, ...]
    output_plan: Any
    approved_rejections: tuple[ApprovedAuditItem, ...]
    approved_exclusions: tuple[ApprovedAuditItem, ...]
    approved_attributions: tuple[ApprovedAttribution, ...]
    review_requirements: tuple[ApprovedReviewRequirement, ...]
    review_choices: tuple[ApprovedReviewChoice, ...]
    reconciliation: CountReconciliation
    summary: Any
    final_review_rows: tuple[FinalReviewRow, ...]
    output_folders: tuple[FinalReviewOutputFolder, ...]
    recognized_book_keys: tuple[str, ...]
    recognized_books: tuple[ApprovedBookIdentity, ...]
    source_ids: tuple[str, ...]
    context: Any
    task_snapshot_sha256: str
    task_snapshot_path: Path
    task_temp_root_identity: tuple[int, int] | None = None
    ignored_duplicate_input_paths: tuple[Path, ...] = ()
    source_input_issues: tuple[object, ...] = ()


@dataclass(frozen=True)
class _Task8TargetedAttributionRollback:
    session: AttributionSession
    latest_attribution_decision_book_keys: tuple[str, ...] | None


class QtBlockingUiCall:
    def __init__(self, qt_core: Any, callback):
        self.callback = callback
        self._bridge = None
        self._defer_during_cancel_confirmation = None
        self._cancelled = None
        if not all(hasattr(qt_core, name) for name in ("QObject", "Signal", "QThread")):
            return
        owner = self

        class Bridge(qt_core.QObject):
            requested = qt_core.Signal(object)

            def __init__(self):
                super().__init__()
                self.requested.connect(self.execute)

            def execute(self, request):
                if owner._defer_request(request):
                    return
                owner._complete_request(request)

        self._bridge = Bridge()
        self._qt_core = qt_core

    def set_cancel_confirmation_guard(self, *, defer, cancelled) -> None:
        self._defer_during_cancel_confirmation = defer
        self._cancelled = cancelled

    def _defer_request(self, request) -> bool:
        if self._defer_during_cancel_confirmation is None:
            return False
        return self._defer_during_cancel_confirmation(lambda: self._complete_request(request))

    def _complete_request(self, request) -> None:
        try:
            if self._cancelled is not None and self._cancelled():
                raise ProductRunnerError("谱图数据提取已取消")
            request["result"] = self.callback()
        except Exception as exc:
            request["error"] = exc
        finally:
            request["done"].set()

    def __call__(self):
        if self._bridge is None or self._qt_core.QThread.currentThread() is self._bridge.thread():
            return self.callback()
        request = {"done": threading.Event(), "result": None, "error": None}
        self._bridge.requested.emit(request)
        request["done"].wait()
        if request["error"] is not None:
            raise request["error"]
        return request["result"]


@dataclass
class QtPreflightDialogPort:
    qt_widgets: Any
    qt_core: Any

    def confirm(
        self,
        parent,
        *,
        default_s1_limit: int,
        steady_emission_y: str,
        allow_missing_s1: bool = False,
    ) -> dict[str, object] | None:
        qt_gui = _load_qt_gui()
        dialog = self.qt_widgets.QDialog(parent)
        dialog.setObjectName("organizer_dialog")
        dialog.setWindowTitle("预检设置")
        dialog.setModal(True)
        dialog.setWindowFlags(
            self.qt_core.Qt.WindowType.Window
            | self.qt_core.Qt.WindowType.FramelessWindowHint
            | self.qt_core.Qt.WindowType.WindowStaysOnTopHint
        )
        apply_styled_dialog_chrome(dialog, self.qt_core)
        dialog.setStyleSheet(ORGANIZER_DIALOG_STYLE_SHEET)
        dialog.setFont(_dialog_font(qt_gui, 13))

        root = self.qt_widgets.QVBoxLayout(dialog)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        header = self.qt_widgets.QFrame(dialog)
        header.setObjectName("dialog_header")
        header.setFixedHeight(50)
        header_layout = self.qt_widgets.QHBoxLayout(header)
        header_layout.setContentsMargins(16, 0, 10, 0)
        title = self.qt_widgets.QLabel("预检设置", header)
        title.setObjectName("dialog_title")
        title.setFont(_dialog_font(qt_gui, 15, bold=True))
        close_button = self.qt_widgets.QPushButton("×", header)
        close_button.setObjectName("dialog_close_button")
        close_button.setFixedSize(28, 26)
        close_button.setFocusPolicy(self.qt_core.Qt.FocusPolicy.NoFocus)
        close_button.clicked.connect(dialog.reject)
        header_layout.addWidget(title, 1)
        header_layout.addWidget(close_button, 0)
        root.addWidget(header)

        body = self.qt_widgets.QFrame(dialog)
        body.setObjectName("dialog_body")
        body_layout = self.qt_widgets.QVBoxLayout(body)
        body_layout.setContentsMargins(18, 16, 18, 16)
        body_layout.setSpacing(14)
        body_layout.setAlignment(self.qt_core.Qt.AlignmentFlag.AlignTop)
        decision_section = self.qt_widgets.QWidget(body)
        decision_layout = self.qt_widgets.QGridLayout(decision_section)
        decision_layout.setContentsMargins(0, 0, 0, 0)
        decision_layout.setHorizontalSpacing(12)
        decision_layout.setVerticalSpacing(4)
        decision_layout.setColumnStretch(1, 1)

        s1_limit = self.qt_widgets.QLineEdit(str(default_s1_limit), dialog)
        s1_limit.setValidator(qt_gui.QIntValidator(1, 2_147_483_647, s1_limit))
        s1_help = self.qt_widgets.QLabel(
            "适用于稳态谱和延迟谱；二维稳态谱不检查。",
            body,
        )
        s1_help.setObjectName("dialog_help_text")
        s1_help.setWordWrap(True)
        steady_y = self.qt_widgets.QComboBox(dialog)
        steady_y.addItems(("S1c", "S1c/R1c"))
        steady_y.setCurrentText(steady_emission_y)
        apply_combo_popup_palette(steady_y, qt_gui)
        steady_y_help = self.qt_widgets.QLabel(
            "仅影响稳态发射谱。稳态激发谱固定使用 S1c/R1c，延迟谱固定使用 S1c。",
            body,
        )
        steady_y_help.setObjectName("dialog_help_text")
        steady_y_help.setWordWrap(True)
        missing_s1 = self.qt_widgets.QCheckBox("允许缺少 S1 列", body)
        missing_s1.setChecked(allow_missing_s1)
        missing_s1_help = self.qt_widgets.QLabel(
            "仅在 S1 缺失时跳过强度上限检查；拟输出数据列及对应 X 仍必须存在。",
            body,
        )
        missing_s1_help.setObjectName("dialog_help_text")
        missing_s1_help.setWordWrap(True)
        s1_label = _dialog_label(self.qt_widgets, qt_gui, "S1 强度上限", body)
        steady_y_label = _dialog_label(self.qt_widgets, qt_gui, "发射谱 Y 列", body)
        value_label_width = max(
            s1_label.sizeHint().width(),
            steady_y_label.sizeHint().width(),
        )
        s1_label.setFixedWidth(value_label_width)
        steady_y_label.setFixedWidth(value_label_width)
        decision_layout.setColumnMinimumWidth(
            0,
            value_label_width + decision_layout.horizontalSpacing(),
        )
        label_alignment = (
            self.qt_core.Qt.AlignmentFlag.AlignRight
            | self.qt_core.Qt.AlignmentFlag.AlignVCenter
        )
        decision_layout.addWidget(missing_s1, 0, 1)
        decision_layout.addWidget(missing_s1_help, 1, 1)
        body_layout.addWidget(decision_section)

        s1_section = self.qt_widgets.QWidget(body)
        s1_section.setObjectName("preflight_s1_section")
        s1_section_layout = self.qt_widgets.QGridLayout(s1_section)
        s1_section_layout.setContentsMargins(0, 0, 0, 0)
        s1_section_layout.setHorizontalSpacing(12)
        s1_section_layout.setVerticalSpacing(4)
        s1_section_layout.setColumnStretch(1, 1)
        s1_section_layout.addWidget(s1_label, 0, 0, alignment=label_alignment)
        s1_section_layout.addWidget(s1_limit, 0, 1)
        s1_section_layout.addWidget(s1_help, 1, 1)
        body_layout.addWidget(s1_section)

        steady_section = self.qt_widgets.QWidget(body)
        steady_section_layout = self.qt_widgets.QGridLayout(steady_section)
        steady_section_layout.setContentsMargins(0, 0, 0, 0)
        steady_section_layout.setHorizontalSpacing(12)
        steady_section_layout.setVerticalSpacing(4)
        steady_section_layout.setColumnStretch(1, 1)
        steady_section_layout.addWidget(steady_y_label, 0, 0, alignment=label_alignment)
        steady_section_layout.addWidget(steady_y, 0, 1)
        steady_section_layout.addWidget(steady_y_help, 1, 1)
        body_layout.addWidget(steady_section)

        buttons = self.qt_widgets.QHBoxLayout()
        buttons.setSpacing(8)
        buttons.addStretch(1)
        confirm_button = self.qt_widgets.QPushButton("确认", body)
        confirm_button.setObjectName("dialog_button_primary")
        configure_workflow_button(confirm_button, qt_gui)
        cancel_button = self.qt_widgets.QPushButton("取消", body)
        cancel_button.setObjectName("dialog_button_danger")
        configure_workflow_button(cancel_button, qt_gui)

        def update_confirmation_state():
            confirm_button.setEnabled(missing_s1.isChecked() or s1_limit.hasAcceptableInput())

        def refit_to_visible_content():
            body_layout.invalidate()
            body_layout.activate()
            body.updateGeometry()
            root.invalidate()
            root.activate()
            target_height = dialog.sizeHint().height()
            if dialog.height() != target_height:
                dialog.resize(dialog.width(), target_height)

        def update_s1_visibility(allow_missing: bool):
            s1_section.setVisible(not allow_missing)
            update_confirmation_state()
            if dialog.isVisible():
                refit_to_visible_content()

        def accept_valid_settings():
            if missing_s1.isChecked() or s1_limit.hasAcceptableInput():
                dialog.accept()

        s1_limit.textChanged.connect(update_confirmation_state)
        missing_s1.toggled.connect(update_s1_visibility)
        confirm_button.clicked.connect(accept_valid_settings)
        cancel_button.clicked.connect(dialog.reject)
        buttons.addWidget(confirm_button)
        buttons.addWidget(cancel_button)
        body_layout.addLayout(buttons)
        body_margins = body_layout.contentsMargins()
        preferred_body_width = (
            520 - body_margins.left() - body_margins.right()
        )
        body_layout.addSpacerItem(
            self.qt_widgets.QSpacerItem(
                preferred_body_width,
                0,
                self.qt_widgets.QSizePolicy.Policy.Preferred,
                self.qt_widgets.QSizePolicy.Policy.Fixed,
            )
        )
        root.addWidget(body)
        dialog.setTabOrder(missing_s1, s1_limit)
        dialog.setTabOrder(s1_limit, steady_y)
        dialog.setTabOrder(steady_y, confirm_button)
        dialog.setTabOrder(confirm_button, cancel_button)
        _enable_dialog_drag(header, dialog, self.qt_core)
        update_s1_visibility(missing_s1.isChecked())
        missing_s1.setFocus(self.qt_core.Qt.FocusReason.OtherFocusReason)

        try:
            _make_windows_taskbar_window(dialog)
            self.qt_core.QTimer.singleShot(
                0,
                lambda: restore_dialog_position(
                    dialog,
                    self.qt_core,
                    qt_gui,
                ),
            )
            if dialog.exec() != self.qt_widgets.QDialog.DialogCode.Accepted:
                return None
            return {
                "s1_limit": (
                    int(s1_limit.text())
                    if s1_limit.hasAcceptableInput()
                    else int(default_s1_limit)
                ),
                "steady_emission_y": steady_y.currentText(),
                "allow_missing_s1": missing_s1.isChecked(),
            }
        finally:
            dialog.deleteLater()


_REVIEW_RESTART = object()
_RELATED_CONFLICTS_CONFIRMED = object()
_TASK8_DERIVED_CACHE_KEYS = (
    "approved_snapshot",
    "output_model",
    "count_reconciliation",
    "task8_review_complete",
)
_TASK8_CONFLICT_CORRECTION_CACHE_KEYS = (
    "task8_final_conflict_target_book_keys",
    "task8_final_conflict_initial_row_id",
    "task8_final_conflict_back_action",
    "task8_targeted_attribution_rollback",
)


@dataclass(frozen=True)
class _Task7ReviewDecision:
    bucket: str
    key: object
    book_keys: tuple[str, ...]
    test_point_label: str = ""
    physical_point_identity: tuple[object, ...] = ()
    context_book_keys: tuple[str, ...] = ()
    special_kind: str = ""


@dataclass
class _RelatedConflictBatch:
    group_book_keys: tuple[str, ...]
    conflicts: tuple[_Task7ReviewDecision, ...]
    selections: tuple[tuple[str, str], ...]
    active_group_key: str
    scroll_value: int = 0
    editor_open: bool = True
    record_decisions: bool = False


@dataclass(frozen=True)
class _AttributionDecision:
    assigned_book_keys: tuple[str, ...]
    completed_rows_start: int


@dataclass
class _Task7ReviewState:
    special_duplicate_choices: dict[str, str]
    special_overlap_choices: dict[str, str]
    special_group_choices: dict[
        tuple[str, tuple[str, ...]],
        tuple[str, tuple[str, ...]],
    ]
    emission_choices: dict[str, str]
    excitation_choices: dict[str, tuple[str, ...]]
    requirements: list[_Task7ReviewDecision]
    history: list[_Task7ReviewDecision]
    recalled: dict[tuple[str, object], tuple[object, tuple[str, ...]]]
    related_conflict_batch: _RelatedConflictBatch | None
    related_conflict_drafts: dict[tuple[str, ...], _RelatedConflictBatch]
    confirmed_related_conflict_batches: list[_RelatedConflictBatch]

    @classmethod
    def empty(cls) -> _Task7ReviewState:
        return cls({}, {}, {}, {}, {}, [], [], {}, None, {}, [])

    def require(
        self,
        bucket: str,
        key: object,
        book_keys: tuple[str, ...],
        *,
        test_point_label: str = "",
        physical_point_identity: tuple[object, ...] = (),
        context_book_keys: tuple[str, ...] = (),
        special_kind: str = "",
    ) -> None:
        requirement = _Task7ReviewDecision(
            bucket,
            key,
            book_keys,
            test_point_label,
            physical_point_identity,
            context_book_keys,
            special_kind,
        )
        identity = (bucket, key)
        for index, current in enumerate(self.requirements):
            if (current.bucket, current.key) != identity:
                continue
            if current != requirement:
                self.requirements[index] = requirement
            return
        self.requirements.append(requirement)

    def remember(
        self,
        bucket: str,
        key: object,
        book_keys: tuple[str, ...],
        *,
        test_point_label: str = "",
        physical_point_identity: tuple[object, ...] = (),
        context_book_keys: tuple[str, ...] = (),
        special_kind: str = "",
    ) -> None:
        decision = _Task7ReviewDecision(
            bucket,
            key,
            book_keys,
            test_point_label,
            physical_point_identity,
            context_book_keys,
            special_kind,
        )
        if decision not in self.requirements:
            raise ValueError("Review decision has no registered requirement")
        self.recalled.pop((bucket, key), None)
        self.history.append(decision)

    def recall_previous(self) -> bool:
        if self._restore_previous_related_conflict_batch():
            return True
        return self.recall_last()

    def recall_last(self) -> bool:
        if not self.history:
            return False
        decision = self.history.pop()
        choices = self._choices_for(decision.bucket)
        if decision.key in choices:
            self.recalled[(decision.bucket, decision.key)] = (
                choices.pop(decision.key),
                decision.book_keys,
            )
        return True

    def has_related_special_conflict(
        self,
        book_keys: tuple[str, ...],
    ) -> bool:
        return self._latest_related_special_conflict_index(book_keys) is not None

    def related_special_conflicts(
        self,
        book_keys: tuple[str, ...],
    ) -> tuple[_Task7ReviewDecision, ...]:
        related = set(book_keys)
        return tuple(
            decision
            for decision in self.history
            if (
                decision.bucket in {"special_duplicate", "special_overlap"}
                and related.intersection(_decision_context_book_keys(decision))
                and decision.key in self._choices_for(decision.bucket)
            )
        )

    def open_related_conflict_editor(
        self,
        book_keys: tuple[str, ...],
        conflicts: tuple[_Task7ReviewDecision, ...],
        *,
        initial_selections: tuple[tuple[str, str], ...] = (),
        record_decisions: bool = False,
    ) -> None:
        if record_decisions:
            for decision in conflicts:
                self.require(
                    decision.bucket,
                    decision.key,
                    decision.book_keys,
                    test_point_label=decision.test_point_label,
                    physical_point_identity=decision.physical_point_identity,
                    context_book_keys=decision.context_book_keys,
                    special_kind=decision.special_kind,
                )
        draft_key = _related_conflict_draft_key(conflicts)
        batch = self.related_conflict_drafts.get(draft_key)
        if (
            batch is not None
            and batch.group_book_keys == book_keys
            and batch.conflicts == conflicts
            and batch.record_decisions == record_decisions
        ):
            batch.editor_open = True
            self.related_conflict_batch = batch
            return
        selections = initial_selections or tuple(
            (
                _related_conflict_id(decision),
                self._choices_for(decision.bucket)[decision.key],
            )
            for decision in conflicts
        )
        batch = _RelatedConflictBatch(
            group_book_keys=book_keys,
            conflicts=conflicts,
            selections=selections,
            active_group_key=selections[0][0],
            record_decisions=record_decisions,
        )
        self.related_conflict_drafts[draft_key] = batch
        self.related_conflict_batch = batch

    def save_related_conflict_editor(
        self,
        selections: tuple[tuple[str, str], ...],
        *,
        active_group_key: str,
        scroll_value: int,
    ) -> None:
        batch = self.related_conflict_batch
        if batch is None:
            raise ValueError("Related-conflict batch is unavailable")
        batch.selections = selections
        batch.active_group_key = active_group_key
        batch.scroll_value = scroll_value

    def synchronize_related_conflict_editor(
        self,
        conflicts: tuple[_Task7ReviewDecision, ...],
    ) -> None:
        batch = self.related_conflict_batch
        if batch is None:
            raise ValueError("Related-conflict batch is unavailable")
        previous_conflicts = batch.conflicts
        previous_draft_key = _related_conflict_draft_key(previous_conflicts)
        previous_group_keys = tuple(
            _related_conflict_id(decision)
            for decision in previous_conflicts
        )
        current_group_keys = tuple(
            _related_conflict_id(decision)
            for decision in conflicts
        )
        group_key_map = dict(zip(previous_group_keys, current_group_keys))
        retained_archives = []
        matched_conflicts = []
        if not batch.record_decisions:
            for archived in self.confirmed_related_conflict_batches:
                retained_conflicts = tuple(
                    decision
                    for decision in archived.conflicts
                    if decision not in previous_conflicts
                )
                matched_conflicts.extend(
                    decision
                    for decision in archived.conflicts
                    if decision in previous_conflicts
                )
                if len(retained_conflicts) == len(archived.conflicts):
                    retained_archives.append(archived)
                    continue
                if retained_conflicts:
                    retained_selections = tuple(
                        (group_key, selection)
                        for group_key, selection in archived.selections
                        if group_key
                        in {
                            _related_conflict_id(decision)
                            for decision in retained_conflicts
                        }
                    )
                    retained_archives.append(
                        _RelatedConflictBatch(
                            group_book_keys=archived.group_book_keys,
                            conflicts=retained_conflicts,
                            selections=retained_selections,
                            active_group_key=retained_selections[0][0],
                            scroll_value=archived.scroll_value,
                            editor_open=False,
                            record_decisions=True,
                        )
                    )
            if any(
                decision not in matched_conflicts
                for decision in previous_conflicts
            ):
                raise ValueError(
                    "Confirmed related-conflict batch is unavailable"
                )
        batch.conflicts = conflicts
        batch.selections = tuple(
            (group_key_map[group_key], selection)
            for group_key, selection in batch.selections
        )
        batch.active_group_key = group_key_map[batch.active_group_key]
        current_draft_key = _related_conflict_draft_key(conflicts)
        if previous_draft_key != current_draft_key:
            if self.related_conflict_drafts.get(previous_draft_key) is batch:
                self.related_conflict_drafts.pop(previous_draft_key)
            self.related_conflict_drafts[current_draft_key] = batch
        if batch.record_decisions:
            return
        previous_requirements = {
            decision: index
            for index, decision in enumerate(self.requirements)
            if decision in previous_conflicts
        }
        if len(previous_requirements) != len(previous_conflicts):
            raise ValueError(
                "Confirmed related-conflict requirements are unavailable"
            )
        for previous, current in zip(
            previous_conflicts,
            conflicts,
            strict=True,
        ):
            self.requirements[previous_requirements[previous]] = current
        self.history = [
            decision
            for decision in self.history
            if decision not in previous_conflicts
        ]
        self.history.extend(conflicts)
        retained_archives.append(
            _RelatedConflictBatch(
                group_book_keys=batch.group_book_keys,
                conflicts=batch.conflicts,
                selections=batch.selections,
                active_group_key=batch.active_group_key,
                scroll_value=batch.scroll_value,
                editor_open=False,
                record_decisions=True,
            )
        )
        self.confirmed_related_conflict_batches = retained_archives

    def hide_related_conflict_editor(self) -> None:
        batch = self.related_conflict_batch
        if batch is None:
            raise ValueError("Related-conflict batch is unavailable")
        batch.editor_open = False
        self.related_conflict_batch = None

    def close_related_conflict_editor(self) -> None:
        batch = self.related_conflict_batch
        if batch is not None:
            self.related_conflict_drafts.pop(
                _related_conflict_draft_key(batch.conflicts),
                None,
            )
        self.related_conflict_batch = None

    def archive_related_conflict_editor(self) -> None:
        batch = self.related_conflict_batch
        if batch is None:
            raise ValueError("Related-conflict batch is unavailable")
        if not batch.record_decisions:
            return
        self.confirmed_related_conflict_batches.append(
            _RelatedConflictBatch(
                group_book_keys=batch.group_book_keys,
                conflicts=batch.conflicts,
                selections=batch.selections,
                active_group_key=batch.active_group_key,
                scroll_value=batch.scroll_value,
                editor_open=False,
                record_decisions=True,
            )
        )

    def _restore_previous_related_conflict_batch(self) -> bool:
        if not self.confirmed_related_conflict_batches:
            return False
        batch = self.confirmed_related_conflict_batches[-1]
        count = len(batch.conflicts)
        if (
            count > len(self.history)
            or tuple(self.history[-count:]) != batch.conflicts
        ):
            return False
        self.confirmed_related_conflict_batches.pop()
        for decision in reversed(batch.conflicts):
            self.history.pop()
            self._choices_for(decision.bucket).pop(decision.key, None)
        batch.editor_open = True
        self.related_conflict_drafts[
            _related_conflict_draft_key(batch.conflicts)
        ] = batch
        self.related_conflict_batch = batch
        return True

    def _latest_related_special_conflict_index(
        self,
        book_keys: tuple[str, ...],
    ) -> int | None:
        related = set(book_keys)
        for index in range(len(self.history) - 1, -1, -1):
            decision = self.history[index]
            if (
                decision.bucket not in {"special_duplicate", "special_overlap"}
                or not related.intersection(
                    _decision_context_book_keys(decision)
                )
            ):
                continue
            choices = self._choices_for(decision.bucket)
            if decision.key not in choices:
                continue
            return index
        return None

    def recalled_selection(
        self,
        bucket: str,
        key: object,
        default: tuple[str, ...] = (),
    ) -> tuple[str, ...]:
        draft = self.recalled.get((bucket, key))
        if draft is None:
            return default
        value = draft[0]
        if bucket == "special_group":
            action, selected = value
            return tuple(selected) if action == "confirm_selection" else default
        if isinstance(value, str):
            return (value,)
        return tuple(value)

    def remember_draft(
        self,
        bucket: str,
        key: object,
        value: object,
        book_keys: tuple[str, ...],
    ) -> None:
        self.recalled[(bucket, key)] = (value, book_keys)

    def discard_books(self, book_keys: tuple[str, ...]) -> None:
        affected = set(book_keys)
        stale_draft_keys = tuple(
            draft_key
            for draft_key, batch in self.related_conflict_drafts.items()
            if affected.intersection(batch.group_book_keys)
        )
        for draft_key in stale_draft_keys:
            self.related_conflict_drafts.pop(draft_key, None)
        self.confirmed_related_conflict_batches = [
            batch
            for batch in self.confirmed_related_conflict_batches
            if not affected.intersection(batch.group_book_keys)
        ]
        batch = self.related_conflict_batch
        if batch is not None and affected.intersection(batch.group_book_keys):
            self.related_conflict_batch = None
        retained = []
        for decision in self.history:
            if affected.intersection(_decision_context_book_keys(decision)):
                self._choices_for(decision.bucket).pop(decision.key, None)
            else:
                retained.append(decision)
        self.history = retained
        self.recalled = {
            key: draft
            for key, draft in self.recalled.items()
            if not affected.intersection(draft[1])
        }

    def completed_requirements(self) -> tuple[_Task7ReviewDecision, ...]:
        return tuple(
            decision
            for decision in self.requirements
            if decision.key in self._choices_for(decision.bucket)
        )

    def _choices_for(self, bucket: str):
        return {
            "special_duplicate": self.special_duplicate_choices,
            "special_overlap": self.special_overlap_choices,
            "special_group": self.special_group_choices,
            "emission": self.emission_choices,
            "excitation": self.excitation_choices,
        }[bucket]


@dataclass(frozen=True)
class _Task7ConflictProjection:
    editor: FinalReviewConflictEditor
    requirements: tuple[_Task7ReviewDecision, ...]
    special_duplicate_choices: dict[str, str]
    special_overlap_choices: dict[str, str]
    special_group_choices: dict[
        tuple[str, tuple[str, ...]],
        tuple[str, tuple[str, ...]],
    ]
    emission_choices: dict[str, str]
    excitation_choices: dict[str, tuple[str, ...]]
    complete: bool


def _task7_review_actions(
    state: _Task7ReviewState,
    *actions: str,
) -> tuple[str, ...]:
    if state.history and "return_to_group" not in actions:
        return ("return_previous", *actions)
    return actions


class FullRunUiController:
    def __init__(
        self,
        *,
        parent,
        widgets: dict[str, Any],
        orchestrator: BookOnlyOrchestrator,
        file_dialogs,
        message_box,
        preflight_dialog,
        manual_dialog_port=None,
        pre_extraction_context_builder=None,
        extraction_runner=None,
        start_run_runner=None,
        task8_runner=None,
        output_stage_runner=None,
        origin_process_gate=None,
        candidate_loader=None,
        attribution_dialog_port=None,
        conflict_review_dialog_port=None,
        schedule_call=None,
        extraction_activity_timer=None,
        monotonic_clock=None,
        failure_log_writer=None,
        open_path=None,
        default_s1_limit: int = 2_000_000,
        default_steady_emission_y: str = "S1c",
        default_allow_missing_s1: bool = False,
        initial_output_parent: str = "",
    ):
        self.parent = parent
        self.widgets = widgets
        self.orchestrator = orchestrator
        self.file_dialogs = file_dialogs
        self.message_box = message_box
        self.preflight_dialog = preflight_dialog
        self.manual_dialog_port = manual_dialog_port or QtManualDialogPort(
            parent=parent
        )
        self.pre_extraction_context_builder = pre_extraction_context_builder or _missing_pre_extraction_context_builder
        self.extraction_runner = extraction_runner or _missing_extraction_runner
        self.start_run_runner = start_run_runner
        self.task8_runner = task8_runner or _InlineTask8Runner()
        self.output_stage_runner = output_stage_runner
        self.origin_process_gate = origin_process_gate
        self.candidate_loader = candidate_loader
        self.attribution_dialog_port = attribution_dialog_port or QtAttributionDialogPort()
        self.conflict_review_dialog_port = (
            conflict_review_dialog_port or QtConflictReviewDialogPort()
        )
        self.schedule_call = schedule_call or (lambda callback: callback())
        self._extraction_activity_timer = extraction_activity_timer
        self._monotonic_clock = monotonic_clock or monotonic
        self.failure_log_writer = (
            failure_log_writer or _write_task8_failure_log
        )
        self.open_path = open_path or _open_directory
        self.default_s1_limit = default_s1_limit
        self.default_steady_emission_y = default_steady_emission_y
        self.default_allow_missing_s1 = default_allow_missing_s1
        self.selected_source_paths: tuple[str, ...] = ()
        self.output_parent = ""
        self.source_selection_blocked = False
        self.run_ready = False
        self.approved_pre_extraction_context = None
        self.run_in_progress = False
        self.shutdown_pending = False
        self._shutdown_error: str | None = None
        self._shutdown_exit_blocked = False
        self._shutdown_cleanup_temp_root = None
        self._shutdown_cleanup_temp_root_identity = None
        self._shutdown_cleanup_owner = None
        self._run_generation = 0
        self._task8_phase: str | None = None
        self._output_stage_coordinator = OutputStageUiCoordinator(self)
        self._output_stage_active = False
        self._output_committed = False
        self._cancel_confirmation_pending = False
        self._deferred_cancel_confirmation_callbacks: list[Callable[[], None]] = []
        self._input_controls_visible = True
        self._startup_health_gate_pending = False
        self._extraction_progress_by_path: dict[str, dict[str, object]] = {}
        self._active_extraction_source_key: str | None = None
        self._active_extraction_started_at: float | None = None
        self._extraction_run_started_at: float | None = None
        self._active_extraction_source_index = 0
        self._active_extraction_source_total = 0
        self._active_extraction_completed_sources = 0
        self._active_source_input_issues: list[object] = []
        if self._extraction_activity_timer is not None:
            self._extraction_activity_timer.timeout.connect(self._refresh_extraction_activity)
        self._connect_buttons()
        if initial_output_parent:
            setter = getattr(self.file_dialogs, "set_initial_output_parent", None)
            if callable(setter):
                setter(initial_output_parent)
        self._refresh_start_button_visibility()

    @property
    def _output_stage_active(self) -> bool:
        return self._output_stage_coordinator.active

    @_output_stage_active.setter
    def _output_stage_active(self, value: bool) -> None:
        self._output_stage_coordinator.active = bool(value)

    @property
    def _output_committed(self) -> bool:
        return self._output_stage_coordinator.committed

    @_output_committed.setter
    def _output_committed(self, value: bool) -> None:
        self._output_stage_coordinator.committed = bool(value)

    def choose_source_files(self) -> SourceSelectionResult:
        if self._startup_health_gate_pending:
            return SourceSelectionResult(ok=False, reason="startup_health_pending")
        paths = self.file_dialogs.select_origin_sources(self.parent)
        result = self.orchestrator.select_sources(paths)
        if not result.ok:
            if result.reason == "no_source_files":
                self._log("未选择输入文件；返回输入文件选择。")
            else:
                self.source_selection_blocked = True
                self._refresh_start_button_visibility()
                self._log(f"源文件选择失败：{result.reason}")
                self.message_box.blocking_error(
                    self.parent,
                    title="源文件选择失败",
                    message=f"源文件选择失败：{result.reason}",
                )
            return result
        self.source_selection_blocked = False
        self.selected_source_paths = result.source_paths
        self._active_source_input_issues = []
        self.orchestrator.task_cache.pop("extraction_summary", None)
        self._set_selection_button_confirmed(
            "select_sources_button",
            f"已选择 {len(result.source_paths)} 个原始文件 · 重新选择",
        )
        self._refresh_start_button_visibility()
        self._update_input_selection_copy()
        self._log(f"已选择 {len(result.source_paths)} 个 Origin 原始文件")
        for source_path in result.source_paths:
            self._log(f"已选择输入文件：{Path(source_path).name}")
        for duplicate_path in result.duplicate_paths:
            self._log(f"已忽略重复文件：{duplicate_path}")
        return result

    def choose_output_parent(self) -> str:
        if self._startup_health_gate_pending:
            return ""
        path = self.file_dialogs.select_output_parent(self.parent)
        if not path:
            self._log("未选择输出位置。")
            return ""
        self._persist_setting_with_damage_recovery(
            lambda: self.orchestrator.select_output_parent(path)
        )
        self.output_parent = path
        setter = getattr(self.file_dialogs, "set_initial_output_parent", None)
        if callable(setter):
            setter(path)
        self._set_label("output_path_label", f"输出位置：{path}")
        self._set_selection_button_confirmed("select_output_parent_button", "输出位置已选择 · 更改")
        self._refresh_start_button_visibility()
        self._update_input_selection_copy()
        self._log(f"输出位置已选择：{path}")
        return path

    def request_start_run(self) -> bool:
        if self._startup_health_gate_pending:
            return False
        if self._shutdown_exit_blocked:
            self._blocking_error("任务已锁定", self._shutdown_error or "Origin 进程退出状态无法确认")
            return False
        if self.run_in_progress:
            self._log("任务正在运行，请等待当前步骤完成")
            return True
        if self.source_selection_blocked:
            self._blocking_error("源文件选择失败", "源文件选择失败：unrecognized_source_file")
            return False
        if not self.selected_source_paths:
            self._blocking_error("需要选择输入文件", "no_source_files")
            return False
        if not self.output_parent:
            self._blocking_error("需要选择输出位置", "output_parent_missing")
            return False
        confirmed = _invoke_preflight_confirm(
            self.preflight_dialog,
            self.parent,
            default_s1_limit=self.default_s1_limit,
            steady_emission_y=self.default_steady_emission_y,
            allow_missing_s1=self.default_allow_missing_s1,
        )
        if confirmed is None:
            self._log("预检设置已取消")
            return False
        self.apply_confirmed_preflight_settings(
            s1_limit=int(confirmed["s1_limit"]),
            steady_emission_y=str(confirmed["steady_emission_y"]),
            allow_missing_s1=bool(
                confirmed.get("allow_missing_s1", self.default_allow_missing_s1)
            ),
        )
        try:
            approved_inputs = self.orchestrator.approved_pre_extraction_inputs()
            if self.origin_process_gate is not None:
                self.origin_process_gate()
        except Exception as exc:
            self._handle_pre_extraction_failure(str(exc))
            return False
        self._log("开始提取前安全检查")
        self._show_pre_extraction_running()
        self.run_ready = False
        self.run_in_progress = True
        self._run_generation += 1
        generation = self._run_generation
        if self.start_run_runner is not None:
            try:
                self.start_run_runner.start(
                    approved_inputs,
                    lambda result: self._handle_start_run_success(generation, result),
                    lambda message: self._handle_start_run_failure(generation, message),
                    lambda event: self._handle_start_run_progress(generation, event),
                )
            except Exception as exc:
                self._handle_start_run_failure(generation, exc)
                return False
            return True
        return self._run_legacy_extraction_path(generation, approved_inputs)

    def _run_legacy_extraction_path(self, generation: int, approved_inputs) -> bool:
        try:
            context = self.pre_extraction_context_builder(
                selected_source_paths=approved_inputs.selected_source_paths,
                output_parent=approved_inputs.output_parent,
                settings_snapshot=approved_inputs.settings_snapshot,
            )
            self._log("已完成提取前安全检查，开始读取谱图数据")
            self._show_extraction_running()
            if callable(getattr(self.extraction_runner, "start", None)):
                self.extraction_runner.start(
                    context,
                    lambda summary: self._handle_start_run_success(generation, (context, summary)),
                    lambda message: self._handle_start_run_failure(generation, message),
                )
                return True
            extraction_summary = self.extraction_runner(context)
        except Exception as exc:
            self._handle_start_run_failure(generation, exc)
            return False
        self._handle_start_run_success(generation, (context, extraction_summary))
        return True

    def _handle_pre_extraction_failure(self, message: str) -> None:
        self.approved_pre_extraction_context = None
        self.run_ready = False
        self.run_in_progress = False
        self.orchestrator.fail_after_preferences(message)
        self._log(f"提取前安全检查失败：{message}")
        self.message_box.blocking_error(self.parent, title="提取前安全检查失败", message=message)

    def _handle_start_run_success(self, generation: int, result) -> None:
        if generation == self._run_generation:
            self._stop_extraction_run_activity()
        if self._defer_during_cancel_confirmation(
            lambda: self._handle_start_run_success(generation, result)
        ):
            return
        if generation != self._run_generation or self.orchestrator.cancelled:
            context = result[0] if isinstance(result, tuple) and result else None
            cleanup_error = _cleanup_temp_root_error(
                getattr(context, "temp_root", None),
                expected_root_identity=getattr(
                    context,
                    "temp_root_identity",
                    None,
                ),
            )
            if cleanup_error is not None:
                self._shutdown_cleanup_temp_root = getattr(context, "temp_root", None)
                self._shutdown_cleanup_temp_root_identity = getattr(
                    context,
                    "temp_root_identity",
                    None,
                )
                self._shutdown_cleanup_owner = "extraction"
                self._shutdown_error = f"取消后临时文件清理失败：{cleanup_error}"
                self._shutdown_exit_blocked = True
                self._log(self._shutdown_error)
            return
        context, extraction_summary = result[:2]
        conversion = result[2] if len(result) > 2 else None
        self.approved_pre_extraction_context = context
        self.orchestrator.task_cache["approved_pre_extraction_context"] = context
        self.orchestrator.task_cache["extraction_summary"] = extraction_summary
        self.orchestrator.last_failure = None
        self.run_ready = True
        self.run_in_progress = False
        self._show_extraction_finished(extraction_summary)
        if self.candidate_loader is not None:
            self.schedule_call(
                lambda: self._begin_attribution_if_current(
                    generation,
                    extraction_summary,
                    conversion,
                )
            )

    def _begin_attribution_if_current(self, generation, extraction_summary, conversion) -> None:
        if self._defer_during_cancel_confirmation(
            lambda: self._begin_attribution_if_current(generation, extraction_summary, conversion)
        ):
            return
        if generation != self._run_generation or self.orchestrator.cancelled:
            return
        self._begin_attribution(extraction_summary, conversion)

    def _handle_start_run_failure(self, generation: int, message) -> None:
        if generation == self._run_generation:
            self._stop_extraction_run_activity()
        if self._defer_during_cancel_confirmation(
            lambda: self._handle_start_run_failure(generation, message)
        ):
            return
        if generation != self._run_generation or self.orchestrator.cancelled:
            message_text = str(message)
            if isinstance(message, ExtractionCleanupBlockedError):
                self._shutdown_error = message_text
                self._shutdown_exit_blocked = True
                self._log(message_text)
            elif self.shutdown_pending and message_text != "谱图数据提取已取消":
                self._shutdown_error = message_text
                self._log(message_text)
            return
        self.run_ready = False
        self.run_in_progress = False
        if isinstance(message, ExtractionCleanupBlockedError):
            self._shutdown_error = str(message)
            self._shutdown_exit_blocked = True
        self.orchestrator.fail_after_preferences(str(message))
        if isinstance(message, AllSelectedSourcesInvalidError):
            self._active_source_input_issues = list(message.source_input_issues)
            self._show_extraction_failed_for_retry(
                issues=message.source_input_issues,
            )
            self._log("输入文件均无法处理，已返回输入文件选择。")
            self.message_box.blocking_error(
                self.parent,
                title="输入文件均无法处理",
                message=(
                    f"{message}\n"
                    "请查看右侧“输入文件问题”，处理后重新选择。"
                ),
            )
            return
        self._show_extraction_failed_for_retry()
        self._log(f"谱图数据提取失败：{message}")
        self.message_box.blocking_error(self.parent, title="谱图数据提取失败", message=str(message))

    def _start_extraction_activity(
        self,
        *,
        source_key: str,
        source_index: int,
        source_total: int,
        completed_sources: int,
    ) -> None:
        self._stop_extraction_activity()
        self._active_extraction_source_key = source_key
        self._active_extraction_started_at = float(self._monotonic_clock())
        self._active_extraction_source_index = source_index
        self._active_extraction_source_total = source_total
        self._active_extraction_completed_sources = completed_sources
        if self._extraction_activity_timer is not None:
            self._extraction_activity_timer.start()

    def _stop_extraction_activity(self) -> None:
        if self._extraction_activity_timer is not None:
            self._extraction_activity_timer.stop()
        self._active_extraction_source_key = None
        self._active_extraction_started_at = None
        self._active_extraction_source_index = 0
        self._active_extraction_source_total = 0
        self._active_extraction_completed_sources = 0

    def _stop_extraction_run_activity(self) -> None:
        self._stop_extraction_activity()
        self._extraction_run_started_at = None

    def _refresh_extraction_activity(self) -> None:
        if self._cancel_confirmation_pending:
            return
        source_key = self._active_extraction_source_key
        started_at = self._active_extraction_started_at
        if source_key is None or started_at is None:
            return
        state = self._extraction_progress_by_path.get(source_key)
        if state is None:
            self._stop_extraction_activity()
            return
        now = float(self._monotonic_clock())
        elapsed = _format_elapsed_seconds(now - started_at)
        run_started_at = self._extraction_run_started_at or started_at
        run_elapsed = _format_elapsed_seconds(now - run_started_at)
        row_index = int(state["row_index"])
        source_path = self.selected_source_paths[row_index]
        source_name = _display_source_name(source_path, self.selected_source_paths)
        self._runtime_update(
            stage="source_input",
            phase_detail=(
                f"{self._active_extraction_completed_sources}/{self._active_extraction_source_total} 文件"
            ),
            source_count=len(self.selected_source_paths),
            runtime_status=(
                f"正在读取 {self._active_extraction_source_index}/"
                f"{self._active_extraction_source_total} 个源文件"
            ),
            activity_mode="automatic",
            title="读取谱图数据",
            subtitle=f"总用时 {run_elapsed}",
            review_row_update=(
                "source_input",
                row_index,
                (source_name, str(state["inventory_count"]), f"正在读取 · 已用时 {elapsed}"),
            ),
            show_review_table=True,
            show_attention=False,
            show_input_controls=False,
            show_completion_actions=False,
        )

    def _handle_start_run_progress(
        self,
        generation: int,
        event,
        *,
        _activity_recorded: bool = False,
    ) -> None:
        if generation != self._run_generation or self.orchestrator.cancelled or not self.run_in_progress:
            return
        if not isinstance(event, dict):
            return
        kind = event.get("kind")
        if not _activity_recorded:
            if kind in {
                "batch_completed",
                "candidate_validation_started",
                "source_completed",
                "source_skipped",
            }:
                self._stop_extraction_activity()
            elif kind == "pre_extraction_completed":
                self._reset_extraction_progress_state()
            elif kind == "source_started":
                source_path = str(event.get("source_path") or "")
                source_key = _source_progress_key(source_path) if source_path else ""
                if source_key and source_key not in self._extraction_progress_by_path:
                    self._reset_extraction_progress_state()
                if not source_key or source_key not in self._extraction_progress_by_path:
                    return
                self._start_extraction_activity(
                    source_key=source_key,
                    source_index=int(event.get("source_index") or 0),
                    source_total=max(
                        1,
                        int(event.get("source_total") or len(self.selected_source_paths) or 1),
                    ),
                    completed_sources=int(event.get("completed_sources") or 0),
                )
        if self._defer_during_cancel_confirmation(
            lambda: self._handle_start_run_progress(
                generation,
                event,
                _activity_recorded=True,
            )
        ):
            return
        if generation != self._run_generation or self.orchestrator.cancelled or not self.run_in_progress:
            return
        if kind == "batch_completed":
            return
        if kind == "pre_extraction_completed":
            self._log("已完成提取前安全检查，开始读取谱图数据")
            self._show_extraction_running(reset_progress=False)
            return
        if kind == "candidate_validation_started":
            self._log("谱图数据读取完成，正在校验候选谱图")
            self._runtime_update(
                stage="source_input",
                phase_detail="候选校验中",
                source_count=len(self.selected_source_paths),
                runtime_status="正在校验候选谱图",
                activity_mode="automatic",
                title="校验候选谱图",
                subtitle="正在核对任务快照完整性并准备样品归属。",
                progress=68,
                progress_busy=True,
                show_review_table=True,
                show_attention=False,
                show_input_controls=False,
            )
            return
        source_path = str(event.get("source_path") or "")
        if not source_path:
            return
        source_key = _source_progress_key(source_path)
        state = self._extraction_progress_by_path.get(source_key)
        if state is None:
            return
        row_index = int(state["row_index"])
        source_index = int(event.get("source_index") or 0)
        source_total = max(1, int(event.get("source_total") or len(self.selected_source_paths) or 1))
        completed_sources = int(event.get("completed_sources") or 0)
        if kind == "source_started":
            state["status"] = "正在读取"
            self._log(
                f"开始读取 {source_index}/{source_total}："
                f"{_display_source_name(source_path, self.selected_source_paths)}"
            )
        elif kind == "source_completed":
            inventory_count = int(event.get("inventory_count") or 0)
            extracted_count = int(event.get("extracted_count") or 0)
            rejected_count = int(event.get("rejected_count") or 0)
            state["inventory_count"] = str(inventory_count)
            state["status"] = f"已提取 {extracted_count}，排除 {rejected_count}"
            self._log(
                f"读取完成 {completed_sources}/{source_total}："
                f"{_display_source_name(source_path, self.selected_source_paths)}，"
                f"检测到 {inventory_count} 个 Book"
            )
        elif kind == "source_skipped":
            reason = str(event.get("reason") or "该文件未进入后续流程。")
            recommendation = str(
                event.get("recommendation")
                or "请检查文件内容后重新选择。"
            )
            state["inventory_count"] = "—"
            state["status"] = f"已跳过：{reason}"
            issue = {
                "source_id": str(event.get("source_id") or ""),
                "original_path": source_path,
                "reason": reason,
                "recommendation": recommendation,
            }
            self._active_source_input_issues = [
                existing
                for existing in self._active_source_input_issues
                if _source_progress_key(
                    _summary_value(existing, "original_path", "")
                ) != source_key
            ]
            self._active_source_input_issues.append(issue)
            self._log(
                f"已跳过输入文件 "
                f"{_display_source_name(source_path, self.selected_source_paths)}："
                f"{reason}"
            )
        else:
            return
        total_inventory = int(event.get("total_inventory_count") or 0)
        total_extracted = int(event.get("total_extracted_count") or 0)
        total_rejected = int(event.get("total_rejected_count") or 0)
        progress = min(100, max(0, round(completed_sources * 100 / source_total)))
        row_update = (
            "source_input",
            row_index,
            (
                _display_source_name(self.selected_source_paths[row_index], self.selected_source_paths),
                str(state["inventory_count"]),
                str(state["status"]),
            ),
        )
        self._runtime_update(
            stage="source_input",
            phase_detail=f"{completed_sources}/{source_total} 文件",
            source_count=len(self.selected_source_paths),
            runtime_status=f"正在读取 {max(1, source_index)}/{source_total} 个源文件",
            activity_mode="automatic",
            title="读取谱图数据",
            subtitle=f"正在处理 {_display_source_name(source_path, self.selected_source_paths)}",
            progress=progress,
            progress_busy=False,
            summary_numbers=(str(total_inventory), str(total_extracted), "0", str(total_rejected)),
            review_row_update=row_update,
            show_review_table=True,
            show_attention=False,
            show_input_controls=False,
        )

    def _handle_extraction_success(self, extraction_summary) -> None:
        self._handle_start_run_success(self._run_generation, (self.approved_pre_extraction_context, extraction_summary))

    def _handle_extraction_failure(self, message: str) -> None:
        self._handle_start_run_failure(self._run_generation, str(message))

    def _reset_extraction_progress_state(self) -> None:
        self._stop_extraction_activity()
        if self._extraction_run_started_at is None:
            self._extraction_run_started_at = float(self._monotonic_clock())
        self._extraction_progress_by_path = {
            _source_progress_key(path): {
                "row_index": row_index,
                "inventory_count": "等待统计",
                "status": "等待读取",
            }
            for row_index, path in enumerate(self.selected_source_paths)
        }

    def _show_extraction_running(self, *, reset_progress: bool = True) -> None:
        if reset_progress:
            self._reset_extraction_progress_state()
        rows = tuple(
            (_display_source_name(path, self.selected_source_paths), "等待统计", "等待读取")
            for path in self.selected_source_paths
        )
        self._runtime_update(
            stage="source_input",
            phase_detail="读取中",
            source_count=len(self.selected_source_paths),
            runtime_status="正在读取谱图数据",
            activity_mode="automatic",
            title="读取谱图数据",
            subtitle="正在从临时副本读取 Note 和 Data。",
            progress_busy=True,
            summary_numbers=("0", "0", "0", "0"),
            review_headers=("来源文件", "检测到的 Book", "处理状态"),
            review_rows=rows,
            show_review_table=True,
            show_attention=False,
            show_input_controls=False,
            show_completion_actions=False,
        )

    def _show_pre_extraction_running(self) -> None:
        self._runtime_update(
            stage="source_input",
            phase_detail="安全检查中",
            source_count=len(self.selected_source_paths),
            runtime_status="正在执行提取前安全检查",
            activity_mode="automatic",
            title="准备谱图数据",
            subtitle="正在校验原始文件、临时空间和只读副本。",
            progress_busy=True,
            summary_numbers=("0", "0", "0", "0"),
            review_rows=(),
            show_review_table=False,
            show_attention=False,
            show_input_controls=False,
        )

    def show_origin_process_wait(self) -> None:
        output_stage = self._output_stage_active
        update = {
            "stage": "output" if output_stage else "source_input",
            "phase_detail": "等待人工操作",
            "source_count": len(self.selected_source_paths),
            "runtime_status": "等待关闭 Origin 后重新检测",
            "activity_mode": "manual",
            "title": "等待关闭 Origin",
            "subtitle": (
                "请保存并关闭正在使用的 Origin，然后点击弹窗中的“重新检测”；"
                "任务会在此等待。"
            ),
            "progress": 92 if output_stage else 0,
            "progress_busy": False,
            "show_attention": False,
            "show_input_controls": False,
            "show_completion_actions": False,
        }
        if output_stage:
            update.update(
                review_headers=("输出步骤", "项目数量", "当前状态"),
                review_rows=(("确认 Origin 进程状态", "1", "等待关闭后重新检测"),),
                show_review_table=True,
            )
        self._runtime_update(**update)
        self._log("等待用户关闭 Origin 并点击“重新检测”")

    def _show_extraction_failed_for_retry(self, *, issues=()) -> None:
        issue_rows = _input_issue_review_rows(issues)
        self._runtime_update(
            stage="source_input",
            phase_detail="等待选择",
            runtime_status="等待重新开始",
            activity_mode="manual",
            title="选择输入文件",
            subtitle=(
                "未找到可处理的输入文件。"
                if issues
                else "谱图数据提取失败，请检查输入后重新开始。"
            ),
            progress=0,
            progress_busy=False,
            summary_numbers=("0", "0", "0", "0"),
            review_headers=("来源文件", "检测到的 Book", "处理状态"),
            review_rows=issue_rows,
            show_review_table=bool(issue_rows),
            show_attention=bool(issues),
            show_input_controls=True,
        )

    def _show_extraction_finished(self, extraction_summary) -> None:
        total_inventory = _summary_value(extraction_summary, "total_inventory_count", 0)
        total_extracted = _summary_value(extraction_summary, "total_extracted_count", 0)
        total_rejected = _summary_value(extraction_summary, "total_rejected_count", 0)
        self._log(
            f"谱图数据提取完成：检测到 {total_inventory} 个 Book，"
            f"已提取 {total_extracted} 条，排除 {total_rejected} 条"
        )
        rows = _summary_review_rows(extraction_summary)
        self._active_source_input_issues = list(
            _summary_value(extraction_summary, "source_input_issues", ()) or ()
        )
        self._runtime_update(
            stage="attribution",
            phase_detail="等待归属",
            source_count=len(self.selected_source_paths),
            runtime_status="谱图数据提取完成",
            activity_mode="manual",
            title="谱图数据读取完成",
            subtitle="已从临时副本读取 Note 和 Data，等待下一步候选归类。",
            progress=72,
            progress_busy=False,
            summary_numbers=(str(total_inventory), str(total_extracted), "0", str(total_rejected)),
            review_headers=("来源文件", "检测到的 Book", "处理状态"),
            review_rows=rows,
            show_review_table=True,
            show_attention=False,
            show_input_controls=False,
        )

    def _begin_attribution(
        self,
        extraction_summary,
        conversion=None,
        *,
        resume_session=None,
        reopened_attributions=None,
    ) -> None:
        try:
            if conversion is None:
                conversion = _invoke_candidate_loader(
                    self.candidate_loader,
                    extraction_summary,
                    None,
                    self.orchestrator.task_cache.get("settings_snapshot", {}),
                )
            candidates = (
                *tuple(conversion.ordinary_candidates),
                *tuple(conversion.steady_2d_candidates),
            )
            rejections = tuple(conversion.rejections)
            books = [
                AttributionBook(
                    source_id=candidate.source_id,
                    folder_path=candidate.folder_path,
                    book_name=candidate.short_name,
                    page_type=candidate.page_type,
                )
                for candidate in candidates
            ]
            targets = build_attribution_targets(books)
            session = resume_session or AttributionSession(targets)
            targets = session.targets
            cache = AttributionCache()
            candidate_by_key = {candidate.book_key: candidate for candidate in candidates}
            book_labels = _attribution_book_labels(candidates)
            queue = list(targets)
            completed_rows: list[tuple[str, str, str]] = []
            attribution_history: list[_AttributionDecision] = []
            returnable_folder_targets = {}
            invalid_attribution_drafts: dict[tuple[str, ...], dict[str, str]] = {}
            total_targets = len(queue)
            reopened_attributions = dict(reopened_attributions or {})
            existing_assignments = session.assignments
            for target in targets:
                remembered = _shared_attribution(
                    target.book_keys,
                    existing_assignments,
                )
                if target.folder_path and remembered is not None:
                    cache.remember(target.folder_path, remembered)

            def publish_progress(members, *, skip_if_complete: bool = False) -> None:
                pending_books = len(
                    {
                        book_key
                        for pending_target in session.targets
                        for book_key in pending_target.book_keys
                        if session.assignment_for(book_key) is None
                    }
                )
                if skip_if_complete and pending_books == 0:
                    return
                completed_targets = sum(
                    all(session.assignment_for(book_key) is not None for book_key in pending_target.book_keys)
                    for pending_target in session.targets
                )
                self._show_attribution_progress(
                    members,
                    completed=completed_targets,
                    total=total_targets,
                    pending_books=pending_books,
                    usable_books=len(candidates),
                    rejections=rejections,
                )

            def choose_target(
                target,
                members,
                *,
                allow_split_folder: bool,
                allow_return_to_book_picker: bool = False,
            ):
                prefill = dict(target.prefill)
                prefill_source = "folder_heuristic" if prefill else ""
                if target.folder_path:
                    remembered = cache.lookup(target.folder_path)
                    if remembered is not None:
                        prefill.update(_sample_form_prefill(remembered.sample))
                        prefill_source = "task_local_reuse"
                prefill = reconcile_concentration_prefill(
                    prefill,
                    members[0].source_filename,
                    target.folder_path or "",
                    book_name=members[0].display_name if target.scope == "book" else "",
                )
                inference_name = (
                    _display_source_name(target.folder_path or "")
                    if target.scope == "folder"
                    else members[0].display_name
                )
                prefill = reconcile_oxygen_environment_prefill(
                    prefill,
                    _display_source_name(members[0].source_filename),
                    inference_name,
                )
                prefill = reconcile_temperature_prefill(
                    prefill,
                    members[0].source_filename,
                    target.folder_path or "",
                    book_name=members[0].display_name if target.scope == "book" else "",
                )
                previous = _shared_attribution(
                    target.book_keys,
                    reopened_attributions,
                )
                if previous is not None:
                    prefill.update(_sample_form_prefill(previous.sample))
                    prefill_source = "previous_attribution"
                invalid_draft = invalid_attribution_drafts.get(
                    target.book_keys
                )
                if invalid_draft is not None:
                    prefill.update(invalid_draft)
                    prefill_source = "invalid_draft"
                return self.attribution_dialog_port.choose(
                    AttributionDialogRequest(
                        target_label=_attribution_target_label(target, members, book_labels),
                        source_filename=members[0].source_filename,
                        book_display_names=tuple(book_labels[member.book_key] for member in members),
                        prefill=prefill,
                        prefill_source=prefill_source,
                        allow_apply_to_remaining_folder=(target.scope == "book"),
                        allow_split_folder=allow_split_folder,
                        allow_return_to_book_picker=allow_return_to_book_picker,
                        allow_return_previous=bool(attribution_history),
                    ),
                    parent=self.parent,
                )

            def confirm_target(target, members, response) -> bool:
                try:
                    attribution = build_attribution_fields(
                        response.sample_type,
                        response.values,
                    )
                except NamePolicyError as exc:
                    invalid_attribution_drafts[target.book_keys] = {
                        "sample_type": response.sample_type,
                        **dict(response.values),
                    }
                    self.message_box.blocking_error(
                        self.parent,
                        title="样品归属信息无效",
                        message=(
                            "样品字段组合后不能安全用作 Origin 名称，"
                            f"请缩短当前字段后重试。\n{exc}"
                        ),
                    )
                    return False
                invalid_attribution_drafts.pop(target.book_keys, None)
                completed_rows_start = len(completed_rows)
                assigned_before = set(session.assignments)
                session.confirm(
                    target.book_keys[0],
                    attribution,
                    apply_to_remaining_folder=response.apply_to_remaining_folder,
                )
                if target.folder_path:
                    cache.remember(target.folder_path, attribution)
                newly_assigned = set(session.assignments) - assigned_before
                assigned_now = tuple(
                    book_key
                    for assigned_target in session.targets
                    for book_key in assigned_target.book_keys
                    if book_key in newly_assigned
                )
                if target.scope == "folder":
                    completed_rows.append(
                        (
                            members[0].source_filename,
                            _attribution_target_label(target, members, book_labels),
                            attribution.sample.canonical_label,
                        )
                    )
                    attribution_history.append(
                        _AttributionDecision(
                            assigned_book_keys=assigned_now,
                            completed_rows_start=completed_rows_start,
                        )
                    )
                    publish_progress(members, skip_if_complete=True)
                    return True
                for assigned_target in session.targets:
                    for book_key in assigned_target.book_keys:
                        if book_key not in assigned_now:
                            continue
                        member = candidate_by_key[book_key]
                        completed_rows.append(
                            (
                                member.source_filename,
                                _attribution_target_label(assigned_target, (member,), book_labels),
                                attribution.sample.canonical_label,
                            )
                        )
                attribution_history.append(
                    _AttributionDecision(
                        assigned_book_keys=assigned_now,
                        completed_rows_start=completed_rows_start,
                    )
                )
                publish_progress(members, skip_if_complete=True)
                return True

            def return_to_previous(current_targets) -> None:
                nonlocal queue
                if not attribution_history:
                    raise ValueError("Previous attribution is unavailable")
                decision = attribution_history.pop()
                reopened_keys, previous = session.reopen(
                    decision.assigned_book_keys
                )
                reopened_attributions.update(previous)
                del completed_rows[decision.completed_rows_start:]
                reopened = set(reopened_keys)
                replay = [
                    candidate
                    for candidate in session.targets
                    if reopened.intersection(candidate.book_keys)
                ]
                reordered = [*replay, *current_targets, *queue]
                queue = []
                seen = set()
                for candidate in reordered:
                    identity = (
                        candidate.scope,
                        candidate.source_id,
                        candidate.folder_path,
                        candidate.book_keys,
                    )
                    if identity in seen:
                        continue
                    seen.add(identity)
                    queue.append(candidate)

            def run_book_picker(
                group_target,
                group_members,
                book_targets,
                *,
                allow_return_to_folder: bool,
                split_folder_on_first_confirm: bool,
            ) -> str:
                nonlocal total_targets
                active_targets = list(book_targets)
                split_committed = not split_folder_on_first_confirm
                completed_rows_start = len(completed_rows)
                attribution_history_start = len(attribution_history)
                cached_attribution = (
                    cache.lookup(group_target.folder_path)
                    if group_target.folder_path
                    else None
                )
                group_key = (
                    group_target.source_id,
                    (group_target.folder_path or "").strip("/"),
                )
                while True:
                    pending_keys = tuple(
                        book_key
                        for pending_target in active_targets
                        for book_key in pending_target.book_keys
                        if session.assignment_for(book_key) is None
                    )
                    if not pending_keys:
                        return "complete"
                    folder_label = (group_target.folder_path or "").strip("/") or "Root"
                    selection = self.attribution_dialog_port.choose_book(
                        AttributionBookSelectionRequest(
                            folder_label=folder_label,
                            source_filename=group_members[0].source_filename,
                            choices=tuple(
                                (book_key, book_labels[book_key])
                                for book_key in pending_keys
                            ),
                            allow_return_to_folder=allow_return_to_folder,
                        ),
                        parent=self.parent,
                    )
                    if selection.action == "cancel":
                        return "cancel"
                    if selection.action == "return_to_folder":
                        if not allow_return_to_folder:
                            raise ValueError("Cannot return to Folder attribution")
                        if split_committed:
                            session.restore_folder(group_target)
                            total_targets -= len(active_targets) - 1
                            del completed_rows[completed_rows_start:]
                            del attribution_history[attribution_history_start:]
                            cache.restore(group_target.folder_path, cached_attribution)
                            returnable_folder_targets.pop(group_key, None)
                        return "return_to_folder"
                    if selection.action != "select_book" or selection.book_key not in pending_keys:
                        raise ValueError("Pending Book selection is invalid")
                    selected_target = next(
                        item for item in active_targets if selection.book_key in item.book_keys
                    )
                    selected_members = (candidate_by_key[selection.book_key],)
                    book_response = choose_target(
                        selected_target,
                        selected_members,
                        allow_split_folder=False,
                        allow_return_to_book_picker=True,
                    )
                    if book_response.action == "cancel":
                        return "cancel"
                    if book_response.action == "return_previous":
                        if not split_committed:
                            active_targets = session.split_folder(group_target)
                            total_targets += len(active_targets) - 1
                            split_committed = True
                        if allow_return_to_folder:
                            returnable_folder_targets[group_key] = group_target
                        return_to_previous(active_targets)
                        return "return_previous"
                    if book_response.action == "return_to_book_picker":
                        continue
                    if book_response.action != "confirm":
                        raise ValueError("Book attribution response is invalid")
                    if not split_committed:
                        active_targets = session.split_folder(group_target)
                        total_targets += len(active_targets) - 1
                        split_committed = True
                        returnable_folder_targets[group_key] = group_target
                        selected_target = next(
                            item for item in active_targets if selection.book_key in item.book_keys
                        )
                    if not confirm_target(
                        selected_target,
                        selected_members,
                        book_response,
                    ):
                        continue

            while queue:
                target = queue.pop(0)
                if all(session.assignment_for(book_key) is not None for book_key in target.book_keys):
                    continue
                members = tuple(candidate_by_key[book_key] for book_key in target.book_keys)
                if target.scope == "book":
                    group_key = (target.source_id, (target.folder_path or "").strip("/"))
                    returnable_folder_target = returnable_folder_targets.get(group_key)
                    grouped_targets = [target]
                    remaining_queue = []
                    for pending_target in queue:
                        pending_group_key = (
                            pending_target.source_id,
                            (pending_target.folder_path or "").strip("/"),
                        )
                        if pending_target.scope == "book" and pending_group_key == group_key:
                            grouped_targets.append(pending_target)
                        else:
                            remaining_queue.append(pending_target)
                    queue = remaining_queue
                    grouped_members = tuple(
                        candidate_by_key[book_key]
                        for grouped_target in grouped_targets
                        for book_key in grouped_target.book_keys
                    )
                    publish_progress(grouped_members)
                    result = run_book_picker(
                        returnable_folder_target or target,
                        grouped_members,
                        grouped_targets,
                        allow_return_to_folder=returnable_folder_target is not None,
                        split_folder_on_first_confirm=False,
                    )
                    if result == "cancel":
                        if not (self.orchestrator.cancelled or self.shutdown_pending):
                            self._cancel_and_exit_after_preferences()
                        return
                    if result == "return_to_folder":
                        queue.insert(0, returnable_folder_target)
                    continue
                publish_progress(members)
                response = choose_target(
                    target,
                    members,
                    allow_split_folder=(target.scope == "folder" and len(target.book_keys) > 1),
                )
                if response.action == "cancel":
                    if not (self.orchestrator.cancelled or self.shutdown_pending):
                        self._cancel_and_exit_after_preferences()
                    return
                if response.action == "return_previous":
                    return_to_previous((target,))
                    continue
                if response.split_folder or response.action == "split_folder":
                    result = run_book_picker(
                        target,
                        members,
                        split_folder_target(target),
                        allow_return_to_folder=True,
                        split_folder_on_first_confirm=True,
                    )
                    if result == "cancel":
                        if not (self.orchestrator.cancelled or self.shutdown_pending):
                            self._cancel_and_exit_after_preferences()
                        return
                    if result == "return_to_folder":
                        queue.insert(0, target)
                        continue
                    continue
                if not confirm_target(target, members, response):
                    queue.insert(0, target)
            assignments = dict(session.assignments)
            self.orchestrator.task_cache["candidate_conversion"] = conversion
            self.orchestrator.task_cache["attribution_session"] = session
            self.orchestrator.task_cache["attribution_assignments"] = assignments
            self.orchestrator.task_cache["sample_library_persistence"] = False
            if attribution_history:
                self.orchestrator.task_cache[
                    "latest_attribution_decision_book_keys"
                ] = attribution_history[-1].assigned_book_keys
            self.orchestrator.task_cache.pop(
                "reopened_attribution_book_keys",
                None,
            )
            self._log(
                f"样品归属完成：已归属 {len(assignments)} 个可用 Book，"
                f"保留 {len(rejections)} 条排除记录；本阶段未写入样品库"
            )
            attribution_rows = tuple(completed_rows)
            if resume_session is not None:
                attribution_rows = _attribution_rows_from_session(
                    session,
                    candidate_by_key,
                    book_labels,
                )
            self._begin_conflict_review(
                extraction_summary,
                conversion,
                assignments,
                attribution_rows=attribution_rows,
                rejections=rejections,
            )
        except Exception as exc:
            self.run_ready = False
            self.orchestrator.fail_after_preferences(str(exc))
            for key in _TASK8_DERIVED_CACHE_KEYS:
                self.orchestrator.task_cache.pop(key, None)
            context = self.approved_pre_extraction_context or self.orchestrator.task_cache.get(
                "approved_pre_extraction_context"
            )
            temp_root = getattr(context, "temp_root", None)
            temp_root_identity = getattr(
                context,
                "temp_root_identity",
                None,
            )
            cleanup_error = _cleanup_temp_root_error(
                temp_root,
                expected_root_identity=temp_root_identity,
            )
            message = str(exc)
            if cleanup_error is None:
                self.approved_pre_extraction_context = None
                for key in (
                    "approved_pre_extraction_context",
                    "extraction_summary",
                    "candidate_conversion",
                    "attribution_session",
                    "attribution_assignments",
                    "selection_spectra",
                    "special_groups",
                    "rejected_special_book_keys",
                    "duplicate_choices",
                    "excitation_pairing",
                    "task7_selection_exclusions",
                    "task7_selected_book_keys",
                    "completeness",
                    "task7_review_complete",
                    "task7_review_state",
                    "reopened_attribution_book_keys",
                    "latest_attribution_decision_book_keys",
                    "sample_record_ids",
                    "sample_library_persistence",
                ):
                    self.orchestrator.task_cache.pop(key, None)
                self._show_extraction_failed_for_retry()
            else:
                self._shutdown_cleanup_temp_root = temp_root
                self._shutdown_cleanup_temp_root_identity = (
                    temp_root_identity
                )
                self._shutdown_error = f"临时文件清理失败：{cleanup_error}"
                self._shutdown_exit_blocked = True
                message = f"{message}\n{self._shutdown_error}"
            self._log(f"样品归属准备失败：{message}")
            self.message_box.blocking_error(self.parent, title="样品归属准备失败", message=message)

    def _begin_conflict_review(
        self,
        extraction_summary,
        conversion,
        assignments,
        *,
        attribution_rows,
        rejections,
    ) -> None:
        total_inventory = _summary_value(
            extraction_summary,
            "total_inventory_count",
            0,
        )
        total_extracted = _summary_value(
            extraction_summary,
            "total_extracted_count",
            0,
        )

        def publish_pending(pending_book_count: int) -> None:
            self._runtime_update(
                stage="conflict_review",
                phase_detail="审核中",
                runtime_status="等待人工处理",
                activity_mode="manual",
                title="审核谱图冲突",
                subtitle="请完成特殊组、重复发射谱和激发谱选择。",
                progress=85,
                progress_busy=False,
                summary_numbers=(
                    str(total_inventory),
                    str(total_extracted),
                    str(pending_book_count),
                    str(len(rejections)),
                ),
                review_headers=("来源文件", "归属范围 / Book", "归属或排除结果"),
                review_rows=(
                    *tuple(attribution_rows),
                    *_candidate_rejection_rows(rejections),
                ),
                show_review_table=bool(attribution_rows or rejections),
                show_attention=False,
                show_input_controls=False,
            )

        candidates = (
            *tuple(conversion.ordinary_candidates),
            *tuple(conversion.steady_2d_candidates),
        )
        candidate_by_key = {candidate.book_key: candidate for candidate in candidates}
        spectra = tuple(
            _selection_spectrum_from_candidate(
                candidate,
                assignments[candidate.book_key],
            )
            for candidate in candidates
        )
        review_state = self.orchestrator.task_cache.get(
            "task7_review_state"
        )
        if not isinstance(review_state, _Task7ReviewState):
            review_state = _Task7ReviewState.empty()
            self.orchestrator.task_cache["task7_review_state"] = review_state

        def return_to_attribution(book_keys: tuple[str, ...]) -> None:
            self._return_to_attribution_from_review(
                extraction_summary,
                conversion,
                book_keys,
            )

        while True:
            pending_emission_book_keys = _pending_review_book_keys(
                review_emission_duplicates(
                    list(spectra),
                    choices=review_state.emission_choices,
                ).pending_reviews
            )
            pending_excitation_book_keys = _pending_review_book_keys(
                select_excitation_candidates(
                    list(spectra),
                    choices=review_state.excitation_choices,
                ).pending_reviews
            )
            downstream_pending_book_keys = tuple(
                dict.fromkeys(
                    (
                        *pending_emission_book_keys,
                        *pending_excitation_book_keys,
                    )
                )
            )
            special_review = self._review_special_groups(
                candidates,
                assignments,
                candidate_by_key,
                review_state=review_state,
                publish_pending=publish_pending,
                return_to_attribution=return_to_attribution,
                additional_pending_book_keys=downstream_pending_book_keys,
            )
            if special_review is _REVIEW_RESTART:
                continue
            if special_review is None:
                return
            (
                accepted_special_groups,
                regular_delayed_keys,
                rejected_special_keys,
            ) = special_review
            special_keys = tuple(
                key
                for group in accepted_special_groups
                for key in group.book_keys
            )
            copyable_emissions = filter_copyable_emissions_after_special(
                list(spectra),
                regular_delayed_book_keys=tuple(
                    dict.fromkeys(regular_delayed_keys)
                ),
                special_group_book_keys=special_keys,
            )
            duplicate_review = self._review_emission_duplicates(
                extraction_summary,
                conversion,
                copyable_emissions,
                candidate_by_key,
                review_state=review_state,
                publish_pending=publish_pending,
                additional_pending_book_keys=pending_excitation_book_keys,
            )
            if duplicate_review is _REVIEW_RESTART:
                continue
            if duplicate_review is None:
                return
            duplicate_result, duplicate_choices = duplicate_review
            excitation_review = self._review_excitations(
                extraction_summary,
                conversion,
                spectra,
                candidate_by_key,
                review_state=review_state,
                publish_pending=publish_pending,
            )
            if excitation_review is _REVIEW_RESTART:
                continue
            if excitation_review is None:
                return
            excitation_result, excitation_choices = excitation_review
            break

        _canonicalize_task7_review_state(
            candidates,
            assignments,
            review_state,
        )

        regular_delayed_key_set = set(regular_delayed_keys)
        special_exclusions = tuple(
            SelectionExclusion(key, "special_group_rejected")
            for key in dict.fromkeys(rejected_special_keys)
            if key not in regular_delayed_key_set
        )
        selected_book_keys = (
            *duplicate_result.selected_book_keys,
            *excitation_result.selected_book_keys,
        )
        selection_exclusions = (
            *special_exclusions,
            *duplicate_result.exclusions,
            *excitation_result.exclusions,
        )
        self.orchestrator.task_cache.update(
            {
                "selection_spectra": spectra,
                "special_groups": tuple(accepted_special_groups),
                "rejected_special_book_keys": tuple(
                    dict.fromkeys(rejected_special_keys)
                ),
                "duplicate_choices": dict(duplicate_choices),
                "excitation_pairing": dict(excitation_choices),
                "task7_selection_exclusions": selection_exclusions,
                "task7_selected_book_keys": selected_book_keys,
                "completeness": {
                    "book_keys": (
                        *duplicate_result.completeness_book_keys,
                        *excitation_result.completeness_book_keys,
                    )
                },
                "task7_review_complete": True,
                "sample_library_persistence": False,
            }
        )
        if self.approved_pre_extraction_context is not None:
            self._begin_final_output_plan_review(
                extraction_summary,
                conversion,
                assignments,
                attribution_rows=attribution_rows,
                rejections=rejections,
                candidate_by_key=candidate_by_key,
            )
            return
        self._show_conflict_review_finished(
            extraction_summary,
            selected_count=len(selected_book_keys),
            special_count=len(accepted_special_groups),
            attribution_rows=attribution_rows,
            rejections=rejections,
            selection_exclusion_rows=_selection_exclusion_rows(
                selection_exclusions,
                candidate_by_key,
            ),
        )

    def _begin_final_output_plan_review(
        self,
        extraction_summary,
        conversion,
        assignments,
        *,
        attribution_rows,
        rejections,
        candidate_by_key,
    ) -> None:
        context = self.approved_pre_extraction_context
        if context is None:
            raise RuntimeError(
                "Approved pre-extraction context is unavailable"
            )
        selected_book_keys = tuple(
            self.orchestrator.task_cache.get("task7_selected_book_keys", ())
        )
        selection_exclusions = tuple(
            self.orchestrator.task_cache.get(
                "task7_selection_exclusions",
                (),
            )
        )
        review_choices = _approved_review_choices(
            self.orchestrator.task_cache
        )
        review_requirements = _approved_review_requirements(
            self.orchestrator.task_cache
        )
        ignored_duplicate_input_paths = tuple(
            Path(path)
            for path in self.orchestrator.task_cache.get(
                "ignored_duplicate_input_paths",
                (),
            )
        )
        generation = self._run_generation
        self._task8_phase = "prepare"
        self._runtime_update(
            stage="output",
            phase_detail="准备最终审核",
            runtime_status="正在后台准备最终输出审核",
            activity_mode="automatic",
            title="准备最终输出审核",
            subtitle="正在读取已选谱图数据并构建只读输出模型。",
            progress=90,
            progress_busy=True,
            show_input_controls=False,
        )
        try:
            self.task8_runner.start(
                lambda cancel_check: self._prepare_final_output_plan_review(
                    extraction_summary,
                    conversion,
                    dict(assignments),
                    tuple(rejections),
                    dict(candidate_by_key),
                    selected_book_keys=selected_book_keys,
                    selection_exclusions=selection_exclusions,
                    review_choices=review_choices,
                    review_requirements=review_requirements,
                    ignored_duplicate_input_paths=(
                        ignored_duplicate_input_paths
                    ),
                    context=context,
                    cancel_check=cancel_check,
                ),
                lambda result: self._handle_task8_preparation_success(
                    generation,
                    result,
                ),
                lambda error: self._handle_task8_operation_failure(
                    generation,
                    "准备",
                    error,
                ),
            )
        except BaseException as exc:
            self._handle_task8_operation_failure(
                generation,
                "准备",
                exc,
            )

    def _prepare_final_output_plan_review(
        self,
        extraction_summary,
        conversion,
        assignments,
        rejections,
        candidate_by_key,
        *,
        selected_book_keys,
        selection_exclusions,
        review_choices,
        review_requirements,
        ignored_duplicate_input_paths,
        context,
        cancel_check,
    ):
        cancel_check()
        selected_key_set = set(selected_book_keys)
        ordinary_by_key = {
            candidate.book_key: candidate
            for candidate in conversion.ordinary_candidates
        }
        if (
            len(selected_key_set) != len(selected_book_keys)
            or any(
                book_key not in ordinary_by_key
                for book_key in selected_book_keys
            )
        ):
            raise ValueError(
                "Task 7 selected Book order is invalid"
            )
        selected_candidates = tuple(
            ordinary_by_key[book_key]
            for book_key in selected_book_keys
        )
        accepted_candidates = []
        normalization_rejections = []
        for candidate in selected_candidates:
            cancel_check()
            try:
                maximum = Decimal(str(candidate.max_y))
            except InvalidOperation as exc:
                raise ValueError(
                    f"selected raw Y maximum is invalid for {candidate.book_key}: {candidate.max_y}"
                ) from exc
            if not maximum.is_finite():
                raise ValueError(
                    f"selected raw Y maximum is invalid for {candidate.book_key}: {candidate.max_y}"
                )
            if maximum <= 0:
                evidence = (
                    ("max_y", _measurement_text(maximum)),
                    (
                        "x_at_max_y",
                        _measurement_text(candidate.x_at_max_y),
                    ),
                )
                normalization_rejections.append(
                    _approved_audit_from_candidate(
                        candidate,
                        detail=canonical_audit_detail(
                            "normalization_nonpositive_max",
                            evidence,
                        ),
                        reason_code="normalization_nonpositive_max",
                        evidence=evidence,
                        decision_source="automatic",
                    )
                )
                continue
            accepted_candidates.append(candidate)
        all_candidates = (
            *tuple(conversion.ordinary_candidates),
            *tuple(conversion.steady_2d_candidates),
        )
        cancel_check()
        excluded_candidates = tuple(
            candidate
            for candidate in all_candidates
            if candidate.book_key not in selected_key_set
        )
        approved_rejections = tuple(
            _approved_audit_from_rejection(rejection)
            for rejection in rejections
        ) + tuple(normalization_rejections)
        approved_exclusions = _approved_exclusions(
            excluded_candidates,
            selection_exclusions,
            review_choices,
        )
        cancel_check()
        reviewed_payloads = _load_reviewed_output_payloads(
            tuple(accepted_candidates),
            extraction_summary,
            cancel_check=cancel_check,
        )
        output_spectra = []
        for index, candidate in enumerate(
            accepted_candidates,
            start=1,
        ):
            cancel_check()
            output_spectra.append(
                _output_spectrum_from_candidate(
                    candidate,
                    assignments[candidate.book_key],
                    selection_order=index,
                    reviewed_payload=reviewed_payloads.get(
                        candidate.book_key
                    ),
                )
            )
        output_spectra = tuple(output_spectra)
        try:
            output_plan = build_output_plan(output_spectra)
        except GeneratedBookNamePolicyError:
            return _Task8ReturnToAttribution(
                extraction_summary,
                conversion,
                tuple(assignments)
            )
        cancel_check()
        if not output_spectra or not output_plan.folders:
            return _Task8NoUsableOutput(
                approved_rejections,
                approved_exclusions,
            )
        plan_spectrum_count = sum(
            len(book.raw_y_columns)
            for folder in output_plan.folders
            for book in folder.books
        )
        plan_column_count = sum(
            len(book.columns)
            for folder in output_plan.folders
            for book in folder.books
        )
        reconciliation = CountReconciliation(
            recognizable_book_count=len(all_candidates) + len(rejections),
            rejected_book_count=(
                len(rejections) + len(normalization_rejections)
            ),
            excluded_book_count=len(excluded_candidates),
            accepted_ordinary_spectrum_count=len(output_spectra),
            output_plan_spectrum_count=plan_spectrum_count,
            output_plan_column_count=plan_column_count,
        )
        summary = build_final_output_plan_summary(
            output_plan,
            reconciliation,
            review_decisions=_review_decision_summary(
                review_choices,
                candidate_by_key,
            ),
        )
        final_review_rows = _final_review_rows(
            (*all_candidates, *rejections),
            assignments,
            accepted_book_keys=tuple(
                spectrum.spectrum_id
                for spectrum in output_spectra
            ),
            rejections=approved_rejections,
            exclusions=approved_exclusions,
            review_requirements=review_requirements,
            source_order=_context_source_ids(context),
        )
        output_folders = _final_review_output_folders(output_plan)
        approved_attributions = []
        for book_key, attribution in sorted(assignments.items()):
            cancel_check()
            approved_attributions.append(
                _approved_attribution(
                    candidate_by_key[book_key],
                    attribution,
                )
            )
        recognized_books = []
        for candidate in (*all_candidates, *rejections):
            cancel_check()
            recognized_books.append(
                _approved_book_identity(candidate)
            )
        return _Task8ReviewDraft(
            extraction_summary=extraction_summary,
            conversion=conversion,
            output_spectra=output_spectra,
            output_plan=output_plan,
            approved_rejections=approved_rejections,
            approved_exclusions=approved_exclusions,
            approved_attributions=tuple(approved_attributions),
            review_requirements=review_requirements,
            review_choices=review_choices,
            reconciliation=reconciliation,
            summary=summary,
            final_review_rows=final_review_rows,
            output_folders=output_folders,
            recognized_book_keys=tuple(
                candidate.book_key
                for candidate in all_candidates
            )
            + tuple(rejection.book_key for rejection in rejections),
            recognized_books=tuple(recognized_books),
            source_ids=_approved_source_ids(
                context,
                recognized_books,
            ),
            context=context,
            task_snapshot_sha256=str(
                _summary_value(
                    extraction_summary,
                    "snapshot_sha256",
                    "",
                )
            ),
            task_snapshot_path=Path(
                _summary_value(
                    extraction_summary,
                    "snapshot_path",
                    "",
                )
            ),
            task_temp_root_identity=context.temp_root_identity,
            ignored_duplicate_input_paths=(
                ignored_duplicate_input_paths
            ),
            source_input_issues=tuple(
                _approved_source_input_issue(issue)
                for issue in (
                    _summary_value(
                        extraction_summary,
                        "source_input_issues",
                        (),
                    )
                    or ()
                )
            ),
        )

    def _handle_task8_preparation_success(
        self,
        generation: int,
        result,
        *,
        resume_conflict_row_id: str = "",
        resume_conflict_selections: tuple[
            FinalReviewConflictSelection,
            ...,
        ] = (),
        resume_conflict_pending_selections: tuple[
            FinalReviewConflictSelection,
            ...,
        ] = (),
        resume_conflict_editing_group_ids: tuple[str, ...] = (),
    ) -> None:
        if self._defer_during_cancel_confirmation(
            lambda: self._handle_task8_preparation_success(
                generation,
                result,
                resume_conflict_row_id=resume_conflict_row_id,
                resume_conflict_selections=resume_conflict_selections,
                resume_conflict_pending_selections=(
                    resume_conflict_pending_selections
                ),
                resume_conflict_editing_group_ids=(
                    resume_conflict_editing_group_ids
                ),
            )
        ):
            return
        if (
            generation != self._run_generation
            or self.orchestrator.cancelled
            or self.shutdown_pending
        ):
            return
        self._task8_phase = None
        try:
            if isinstance(result, _Task8NoUsableOutput):
                self._fail_no_usable_output(
                    rejections=result.rejections,
                    exclusions=result.exclusions,
                )
                return
            if isinstance(result, _Task8ReturnToAttribution):
                self._log(
                    "生成的 Book 名称过长；已返回样品归属，请缩短相关样品字段。"
                )
                self._return_to_attribution_from_review(
                    result.extraction_summary,
                    result.conversion,
                    result.book_keys,
                )
                return
            if not isinstance(result, _Task8ReviewDraft):
                raise TypeError(
                    "Task 8 preparation returned an invalid result"
                )
            initial_view_state = self.orchestrator.task_cache.get(
                "task8_final_review_view_state"
            )
            if not isinstance(initial_view_state, FinalReviewViewState):
                initial_view_state = FinalReviewViewState()
            conflict_target_book_keys = tuple(
                self.orchestrator.task_cache.get(
                    "task8_final_conflict_target_book_keys",
                    (),
                )
            )
            initial_conflict_row_id = str(
                resume_conflict_row_id
                or self.orchestrator.task_cache.get(
                    "task8_final_conflict_initial_row_id",
                    "",
                )
            )
            conflict_back_action = str(
                self.orchestrator.task_cache.get(
                    "task8_final_conflict_back_action",
                    "local",
                )
            )
            reconciliation = result.reconciliation
            self._runtime_update(
                stage="output",
                phase_detail="等待确认",
                runtime_status="等待确认最终审核",
                activity_mode="manual",
                title="确认最终归属与输出计划",
                subtitle=(
                    "审核窗口已打开；本阶段尚未启动 Origin"
                    "或创建输出文件。"
                ),
                progress=90,
                progress_busy=False,
                summary_numbers=(
                    str(reconciliation.recognizable_book_count),
                    str(reconciliation.accepted_ordinary_spectrum_count),
                    "0",
                    str(
                        reconciliation.rejected_book_count
                        + reconciliation.excluded_book_count
                    ),
                ),
                review_headers=(
                    "来源文件",
                    "原 Folder / Book",
                    "最终归属与结果",
                ),
                review_rows=tuple(
                    (
                        row.source_filename,
                        f"{row.folder_path} / {row.book_name}",
                        f"{row.attribution} · {row.result}",
                    )
                    for row in result.final_review_rows
                ),
                show_review_table=bool(result.final_review_rows),
                show_attention=False,
                show_input_controls=False,
            )
            response = self.manual_dialog_port.choose(
                final_attribution_summary_dialog(
                    result.final_review_rows,
                    recognized_count=(
                        result.reconciliation.recognizable_book_count
                    ),
                    rejected_count=(
                        result.reconciliation.rejected_book_count
                    ),
                    excluded_count=(
                        result.reconciliation.excluded_book_count
                    ),
                    accepted_count=(
                        result.reconciliation.accepted_ordinary_spectrum_count
                    ),
                    output_folders=result.output_folders,
                    initial_view_state=initial_view_state,
                    conflict_editor_provider=(
                        lambda row_id, selections: (
                            self._final_conflict_editor_model(
                                result,
                                row_id,
                                selections,
                                target_book_keys=(
                                    conflict_target_book_keys
                                    or (row_id,)
                                ),
                            )
                        )
                    ),
                    background_conflict_refresh=True,
                    initial_conflict_row_id=initial_conflict_row_id,
                    initial_conflict_selections=(
                        resume_conflict_selections
                    ),
                    initial_conflict_pending_selections=(
                        resume_conflict_pending_selections
                    ),
                    initial_conflict_editing_group_ids=(
                        resume_conflict_editing_group_ids
                    ),
                    conflict_back_action=conflict_back_action,
                )
            )
            self.orchestrator.task_cache[
                "task8_final_review_view_state"
            ] = response.view_state
            if response.action == "cancel":
                self._cancel_review(
                    on_continue=lambda: self.schedule_call(
                        lambda: self._handle_task8_preparation_success(
                            generation,
                            result,
                        )
                    )
                )
                return
            if response.action == "cancel_conflicts":
                self._cancel_review(
                    on_continue=lambda: self.schedule_call(
                        lambda: self._handle_task8_preparation_success(
                            generation,
                            result,
                            resume_conflict_row_id=response.selected_row_id,
                            resume_conflict_selections=response.conflict_selections,
                            resume_conflict_pending_selections=(
                                response.conflict_pending_selections
                            ),
                            resume_conflict_editing_group_ids=(
                                response.conflict_editing_group_ids
                            ),
                        )
                    )
                )
                return
            if response.action == "modify_attribution":
                assignment_keys = {
                    item.book_key
                    for item in result.approved_attributions
                }
                if response.selected_row_id not in assignment_keys:
                    raise RuntimeError(
                        "Selected final-review attribution row is unavailable"
                    )
                self._begin_targeted_attribution_correction(
                    generation,
                    result,
                    response.selected_row_id,
                )
                return
            if response.action == "modify_conflicts":
                self._apply_final_conflict_correction(
                    generation,
                    result,
                    response.selected_row_id,
                    response.conflict_selections,
                    target_book_keys=(
                        conflict_target_book_keys
                        or (response.selected_row_id,)
                    ),
                )
                return
            if response.action == "discard_targeted_correction":
                self._discard_pending_targeted_correction(
                    generation,
                    result,
                )
                return
            if response.action == "return_to_attribution":
                latest_attribution_scope = tuple(
                    self.orchestrator.task_cache.get(
                        "latest_attribution_decision_book_keys",
                        (),
                    )
                )
                assignment_keys = {
                    item.book_key
                    for item in result.approved_attributions
                }
                if (
                    not latest_attribution_scope
                    or any(
                        book_key not in assignment_keys
                        for book_key in latest_attribution_scope
                    )
                ):
                    raise RuntimeError(
                        "Latest attribution confirmation scope is unavailable"
                    )
                self._return_to_attribution_from_review(
                    result.extraction_summary,
                    result.conversion,
                    latest_attribution_scope,
                )
                return
            if response.action != "confirm":
                raise ValueError(
                    "Final attribution summary returned "
                    f"{response.action}"
                )
            self._start_task8_seal(generation, result)
        except BaseException as exc:
            self._handle_task8_operation_failure(
                generation,
                "确认",
                exc,
                retry_draft=result,
            )

    def _start_task8_seal(
        self,
        generation: int,
        draft: _Task8ReviewDraft,
    ) -> None:
        self._task8_phase = "seal"
        self._runtime_update(
            stage="output",
            phase_detail="封存审批快照",
            runtime_status="正在后台核对原件与审批账本",
            activity_mode="automatic",
            title="封存最终输出审核",
            subtitle="正在重算源文件与任务快照；此步骤不会写入输出。",
            progress=96,
            progress_busy=True,
            show_input_controls=False,
        )
        try:
            self.task8_runner.start(
                lambda cancel_check: self._seal_task8_review(
                    draft,
                    cancel_check,
                ),
                lambda approved: self._handle_task8_seal_success(
                    generation,
                    draft,
                    approved,
                ),
                lambda error: self._handle_task8_operation_failure(
                    generation,
                    "封存",
                    error,
                    retry_draft=draft,
                ),
            )
        except BaseException as exc:
            self._handle_task8_operation_failure(
                generation,
                "封存",
                exc,
                retry_draft=draft,
            )

    def _seal_task8_review(
        self,
        draft: _Task8ReviewDraft,
        cancel_check,
    ):
        cancel_check()
        context = draft.context
        all_source_fingerprints_before = tuple(
            context.source_fingerprints_before
        )
        all_source_fingerprints_after = tuple(
            snapshot_sources(
                [
                    snapshot.path
                    for snapshot in all_source_fingerprints_before
                ],
                [],
                cancel_check=cancel_check,
            )
        )
        if (
            all_source_fingerprints_after
            != all_source_fingerprints_before
        ):
            raise ProductRunnerError(
                "source fingerprints changed before approved snapshot"
            )
        context_source_ids = _context_source_ids(context)
        fingerprints_by_id = dict(
            zip(
                context_source_ids,
                all_source_fingerprints_before,
                strict=True,
            )
        )
        try:
            source_fingerprints_before = tuple(
                fingerprints_by_id[source_id]
                for source_id in draft.source_ids
            )
        except KeyError as exc:
            raise ProductRunnerError(
                "approved source id is not present in pre-extraction context"
            ) from exc
        cancel_check()
        return approve_output_plan(
            task_snapshot_sha256=draft.task_snapshot_sha256,
            recognized_book_keys=draft.recognized_book_keys,
            accepted_spectra=draft.output_spectra,
            rejections=draft.approved_rejections,
            exclusions=draft.approved_exclusions,
            attributions=draft.approved_attributions,
            review_requirements=draft.review_requirements,
            review_choices=draft.review_choices,
            output_plan=draft.output_plan,
            source_fingerprints_before=source_fingerprints_before,
            source_fingerprints_after=source_fingerprints_before,
            count_reconciliation=draft.reconciliation,
            recognized_books=draft.recognized_books,
            source_ids=draft.source_ids,
            task_snapshot_path=draft.task_snapshot_path,
            task_temp_root_identity=draft.task_temp_root_identity,
            cancel_check=cancel_check,
            settings_snapshot=getattr(
                context,
                "settings_snapshot",
                {},
            ),
            ignored_duplicate_input_paths=(
                draft.ignored_duplicate_input_paths
            ),
            source_input_issues=draft.source_input_issues,
            selected_source_fingerprints_before=(
                all_source_fingerprints_before
            ),
        )

    def _handle_task8_seal_success(
        self,
        generation: int,
        draft: _Task8ReviewDraft,
        approved_snapshot,
    ) -> None:
        if self._defer_during_cancel_confirmation(
            lambda: self._handle_task8_seal_success(
                generation,
                draft,
                approved_snapshot,
            )
        ):
            return
        if (
            generation != self._run_generation
            or self.orchestrator.cancelled
            or self.shutdown_pending
        ):
            return
        self._task8_phase = None
        self.orchestrator.task_cache.update(
            {
                "approved_snapshot": approved_snapshot,
                "output_model": approved_snapshot.output_plan,
                "count_reconciliation": draft.reconciliation,
                "task8_review_complete": True,
                "sample_library_persistence": False,
            }
        )
        if self.output_stage_runner is not None:
            self._start_output_stage(
                generation,
                draft,
                approved_snapshot,
            )
            return
        self._show_task8_approved(
            draft.extraction_summary,
            draft.summary,
            approved_snapshot=approved_snapshot,
        )

    def _start_output_stage(
        self,
        generation: int,
        draft: _Task8ReviewDraft,
        approved_snapshot,
    ) -> None:
        self._output_stage_coordinator.start(
            generation,
            draft,
            approved_snapshot,
        )

    def _handle_output_stage_progress(
        self,
        generation: int,
        extraction_summary,
        approved_snapshot,
        stage: str,
    ) -> None:
        self._output_stage_coordinator.handle_progress(
            generation,
            extraction_summary,
            approved_snapshot,
            stage,
        )

    def _show_output_stage_progress(
        self,
        stage: str,
        extraction_summary,
        approved_snapshot,
    ) -> None:
        self._output_stage_coordinator.show_progress(
            stage,
            extraction_summary,
            approved_snapshot,
        )

    def _handle_output_stage_success(
        self,
        generation: int,
        draft: _Task8ReviewDraft,
        approved_snapshot,
        result,
    ) -> None:
        self._output_stage_coordinator.handle_success(
            generation,
            draft,
            approved_snapshot,
            result,
        )

    def _handle_output_stage_failure(
        self,
        generation: int,
        draft: _Task8ReviewDraft,
        approved_snapshot,
        error,
    ) -> None:
        self._output_stage_coordinator.handle_failure(
            generation,
            draft,
            approved_snapshot,
            error,
        )

    def _handle_task8_operation_failure(
        self,
        generation: int,
        phase: str,
        error,
        *,
        retry_draft: _Task8ReviewDraft | None = None,
    ) -> None:
        if self._defer_during_cancel_confirmation(
            lambda: self._handle_task8_operation_failure(
                generation,
                phase,
                error,
                retry_draft=retry_draft,
            )
        ):
            return
        if (
            generation != self._run_generation
            or self.orchestrator.cancelled
            or isinstance(error, _Task8OperationCancelled)
        ):
            return
        self._task8_phase = None
        message = f"最终输出审核{phase}失败：{error}"
        failure_log_path = None
        try:
            failure_log_path = self.failure_log_writer(
                _task8_failure_diagnostic(
                    phase,
                    error,
                    retry_draft,
                )
            )
        except Exception as exc:
            message = f"{message}\n失败日志写入失败：{exc}"
        if retry_draft is not None:
            if failure_log_path is not None:
                self.orchestrator.task_cache[
                    "failed_run_log_path"
                ] = failure_log_path
            concise_error = " ".join(str(error).split())
            recovery_message = (
                f"最终输出审核{phase}失败，审核状态已保留，可重试或取消。\n"
                f"原因：{concise_error}"
            )
            if failure_log_path is not None:
                recovery_message += f"\n失败日志：{failure_log_path}"
            elif "失败日志写入失败" in message:
                recovery_message += "\n" + message.rsplit("\n", 1)[-1]
            self._log(recovery_message)
            self.message_box.blocking_error(
                self.parent,
                title="最终输出审核失败",
                message=recovery_message,
            )
            self.schedule_call(
                lambda: self._handle_task8_preparation_success(
                    generation,
                    retry_draft,
                )
            )
            return
        self.run_ready = False
        self.run_in_progress = False
        self.orchestrator.fail_after_preferences(message)
        retry_inputs = {
            key: self.orchestrator.task_cache[key]
            for key in ("selected_source_paths", "output_parent")
            if key in self.orchestrator.task_cache
        }
        self.orchestrator.task_cache.clear()
        self.orchestrator.task_cache.update(retry_inputs)
        if failure_log_path is not None:
            self.orchestrator.task_cache[
                "failed_run_log_path"
            ] = failure_log_path
        context = self.approved_pre_extraction_context
        cleanup_error = _cleanup_temp_root_error(
            getattr(context, "temp_root", None),
            expected_root_identity=getattr(
                context,
                "temp_root_identity",
                None,
            ),
        )
        if cleanup_error is None:
            self.approved_pre_extraction_context = None
        else:
            self._shutdown_cleanup_temp_root = getattr(
                context,
                "temp_root",
                None,
            )
            self._shutdown_cleanup_temp_root_identity = getattr(
                context,
                "temp_root_identity",
                None,
            )
            self._shutdown_error = (
                f"临时文件清理失败：{cleanup_error}"
            )
            self._shutdown_exit_blocked = True
            message = f"{message}\n{self._shutdown_error}"
        self._show_extraction_failed_for_retry()
        if failure_log_path is not None:
            message = f"{message}\n失败日志：{failure_log_path}"
        self._log(message)
        self.message_box.blocking_error(
            self.parent,
            title="最终输出审核失败",
            message=message,
        )

    def _fail_no_usable_output(
        self,
        *,
        rejections: tuple[ApprovedAuditItem, ...] = (),
        exclusions: tuple[ApprovedAuditItem, ...] = (),
    ) -> None:
        message = (
            "没有可输出的普通谱图：过滤、特殊谱分类、重复审核、"
            "激发谱选择和人工排除后，未留下可复制的普通发射谱或激发谱。"
        )
        if rejections or exclusions:
            details = "\n".join(
                (
                    f"{item.source_filename} · "
                    f"{_approved_identity_location(item)}："
                    f"{disposition}：{item.detail}"
                )
                for disposition, items in (
                    ("拒绝", rejections),
                    ("不输出", exclusions),
                )
                for item in items
            )
            message = f"{message}\n{details}"
        failure_log_path = None
        try:
            failure_log_path = self.failure_log_writer(message)
        except Exception as exc:
            message = f"{message}\n失败日志写入失败：{exc}"
        self.run_ready = False
        self.orchestrator.fail_after_preferences(message)
        retry_inputs = {
            key: self.orchestrator.task_cache[key]
            for key in ("selected_source_paths", "output_parent")
            if key in self.orchestrator.task_cache
        }
        self.orchestrator.task_cache.clear()
        self.orchestrator.task_cache.update(retry_inputs)
        if failure_log_path is not None:
            self.orchestrator.task_cache[
                "failed_run_log_path"
            ] = failure_log_path
        context = self.approved_pre_extraction_context
        cleanup_error = _cleanup_temp_root_error(
            getattr(context, "temp_root", None),
            expected_root_identity=getattr(
                context,
                "temp_root_identity",
                None,
            ),
        )
        if cleanup_error is None:
            self.approved_pre_extraction_context = None
            self.orchestrator.task_cache.pop(
                "approved_pre_extraction_context",
                None,
            )
        else:
            message = f"{message}\n临时文件清理失败：{cleanup_error}"
            self._shutdown_cleanup_temp_root = getattr(
                context,
                "temp_root",
                None,
            )
            self._shutdown_cleanup_temp_root_identity = getattr(
                context,
                "temp_root_identity",
                None,
            )
            self._shutdown_error = cleanup_error
            self._shutdown_exit_blocked = True
        self._show_extraction_failed_for_retry()
        if failure_log_path is None:
            self._log("没有可输出的普通谱图；失败日志写入失败")
            failure_log_message = ""
        else:
            self._log(
                f"没有可输出的普通谱图；失败日志：{failure_log_path}"
            )
            failure_log_message = f"\n失败日志：{failure_log_path}"
        self.message_box.blocking_error(
            self.parent,
            title="没有可输出的普通谱图",
            message=(
                f"{message}{failure_log_message}\n"
                "请返回输入文件或审核选择后重试。"
            ),
        )

    def _review_related_conflict_batch(
        self,
        review_state: _Task7ReviewState,
        batch: _RelatedConflictBatch | None,
        candidate_by_key,
        special_book_by_key: dict[str, SpectrumBook],
        *,
        return_to_attribution: Callable[[tuple[str, ...]], None] | None,
    ):
        if batch is None:
            raise ValueError("Related-conflict batch is unavailable")
        navigation_actions = (
            () if batch.record_decisions else ("return_to_group",)
        )
        decision_subject = _related_conflict_subject(batch.conflicts)
        request = ConflictReviewRequest(
            kind="special_conflict_batch",
            title=(
                f"确认{decision_subject}"
                if batch.record_decisions
                else f"修改{decision_subject}"
            ),
            decision_subject=decision_subject,
            instruction="请为每个冲突保留一个选择。",
            choices=(),
            selection_mode="grouped_single",
            actions=_task7_review_actions(
                review_state,
                *navigation_actions,
                "confirm_all_conflicts",
                "return_to_attribution",
                "cancel",
            ),
            choice_groups=_related_conflict_groups(
                batch,
                candidate_by_key,
            ),
            initial_active_group_key=batch.active_group_key,
            initial_scroll_value=batch.scroll_value,
            editing_existing_decisions=not batch.record_decisions,
        )
        response = self.conflict_review_dialog_port.choose(
            request,
            parent=self.parent,
        )
        if response.action == "cancel":
            self._cancel_review()
            return None
        if response.action == "return_to_attribution":
            if return_to_attribution is None:
                raise ValueError(
                    "Return-to-attribution handler is unavailable"
                )
            return_to_attribution(batch.group_book_keys)
            return None
        if response.action not in {
            "return_previous",
            "return_to_group",
            "confirm_all_conflicts",
        }:
            raise ValueError("Related-conflict editor response is invalid")
        selections = _validated_group_selections(request, response)
        review_state.save_related_conflict_editor(
            selections,
            active_group_key=(
                response.active_group_key or batch.active_group_key
            ),
            scroll_value=response.scroll_value,
        )
        if response.action == "return_previous":
            review_state.hide_related_conflict_editor()
            if not review_state.recall_previous():
                raise ValueError("Previous review decision is unavailable")
            return _REVIEW_RESTART
        if response.action == "return_to_group":
            review_state.hide_related_conflict_editor()
            return _REVIEW_RESTART
        _apply_related_conflict_selections(
            review_state,
            batch,
            dict(selections),
            special_book_by_key,
        )
        if batch.record_decisions:
            for decision in batch.conflicts:
                review_state.remember(
                    decision.bucket,
                    decision.key,
                    decision.book_keys,
                    test_point_label=decision.test_point_label,
                    physical_point_identity=decision.physical_point_identity,
                    context_book_keys=decision.context_book_keys,
                    special_kind=decision.special_kind,
                )
            review_state.archive_related_conflict_editor()
        review_state.close_related_conflict_editor()
        return _RELATED_CONFLICTS_CONFIRMED

    def _review_special_groups(
        self,
        candidates,
        assignments,
        candidate_by_key,
        *,
        review_state: _Task7ReviewState | None = None,
        publish_pending: Callable[[int], None] | None = None,
        return_to_attribution: Callable[[tuple[str, ...]], None] | None = None,
        additional_pending_book_keys: tuple[str, ...] = (),
    ):
        if review_state is None:
            review_state = _Task7ReviewState.empty()
        special_books = [
            _special_book_from_candidate(
                candidate,
                assignments[candidate.book_key],
            )
            for candidate in candidates
        ]
        special_book_by_key = {
            book.book_key: book for book in special_books
        }
        batch = review_state.related_conflict_batch
        if batch is not None and batch.editor_open:
            outcome = self._review_related_conflict_batch(
                review_state,
                batch,
                candidate_by_key,
                special_book_by_key,
                return_to_attribution=return_to_attribution,
            )
            if outcome is _RELATED_CONFLICTS_CONFIRMED:
                return _REVIEW_RESTART
            return outcome
        duplicate_point_choices = review_state.special_duplicate_choices
        overlap_choices = review_state.special_overlap_choices
        while True:
            result = classify_special_groups(
                special_books,
                duplicate_choices=duplicate_point_choices,
                overlap_choices=overlap_choices,
            )
            if result.pending_duplicate_reviews:
                context_book_keys = (
                    result.pending_duplicate_reviews[0].context_book_keys
                )
                pending_reviews = tuple(
                    pending
                    for pending in result.pending_duplicate_reviews
                    if pending.context_book_keys == context_book_keys
                )
                if publish_pending is not None:
                    publish_pending(
                        len(
                            {
                                *additional_pending_book_keys,
                                *_pending_review_book_keys(
                                    result.pending_duplicate_reviews
                                ),
                            }
                        )
                    )
                conflicts = tuple(
                    _Task7ReviewDecision(
                        bucket="special_duplicate",
                        key=pending.choice_key,
                        book_keys=pending.book_keys,
                        test_point_label=pending.point_label,
                        context_book_keys=pending.context_book_keys,
                        special_kind=pending.kind,
                    )
                    for pending in pending_reviews
                )
                initial_selections = tuple(
                    (
                        _related_conflict_id(decision),
                        _initial_grouped_selection(
                            duplicate_point_choices.get(decision.key),
                            review_state.recalled_selection(
                                decision.bucket,
                                decision.key,
                            ),
                            decision.book_keys,
                        ),
                    )
                    for decision in conflicts
                )
                review_state.open_related_conflict_editor(
                    context_book_keys,
                    conflicts,
                    initial_selections=initial_selections,
                    record_decisions=True,
                )
                outcome = self._review_related_conflict_batch(
                    review_state,
                    review_state.related_conflict_batch,
                    candidate_by_key,
                    special_book_by_key,
                    return_to_attribution=return_to_attribution,
                )
                if outcome is _RELATED_CONFLICTS_CONFIRMED:
                    continue
                return outcome
            if result.pending_overlap_assignments:
                context_book_keys = (
                    result.pending_overlap_assignments[0].context_book_keys
                )
                pending_assignments = tuple(
                    pending
                    for pending in result.pending_overlap_assignments
                    if pending.context_book_keys == context_book_keys
                )
                conflicts = tuple(
                    _Task7ReviewDecision(
                        bucket="special_overlap",
                        key=pending.book_key,
                        book_keys=(pending.book_key,),
                        test_point_label=(
                            candidate_by_key[pending.book_key].fixed_wavelength
                            or _visible_book_name(
                                candidate_by_key[pending.book_key]
                            )
                        ),
                        physical_point_identity=spectrum_book_point_identity(
                            special_book_by_key[pending.book_key]
                        ),
                        context_book_keys=pending.context_book_keys,
                    )
                    for pending in pending_assignments
                )
                initial_selections = tuple(
                    (
                        _related_conflict_id(decision),
                        _initial_grouped_selection(
                            overlap_choices.get(decision.key),
                            review_state.recalled_selection(
                                decision.bucket,
                                decision.key,
                            ),
                            OVERLAP_CHOICES,
                        ),
                    )
                    for decision in conflicts
                )
                if publish_pending is not None:
                    publish_pending(
                        len(
                            {
                                *additional_pending_book_keys,
                                *(
                                    assignment.book_key
                                    for assignment
                                    in result.pending_overlap_assignments
                                ),
                            }
                        )
                    )
                review_state.open_related_conflict_editor(
                    context_book_keys,
                    conflicts,
                    initial_selections=initial_selections,
                    record_decisions=True,
                )
                outcome = self._review_related_conflict_batch(
                    review_state,
                    review_state.related_conflict_batch,
                    candidate_by_key,
                    special_book_by_key,
                    return_to_attribution=return_to_attribution,
                )
                if outcome is _RELATED_CONFLICTS_CONFIRMED:
                    continue
                return outcome
            break

        accepted_groups = []
        regular_delayed_keys = list(result.regular_delayed_book_keys)
        rejected_keys = []
        for index, group in enumerate(result.groups):
            if group.kind == "steady_2d":
                accepted_groups.append(group)
                continue
            group_key = (group.kind, group.book_keys)
            decision = review_state.special_group_choices.get(group_key)
            if decision is None:
                review_state.require(
                    "special_group",
                    group_key,
                    group.book_keys,
                )
                while True:
                    if publish_pending is not None:
                        publish_pending(
                            len(
                                {
                                    *additional_pending_book_keys,
                                    *(
                                        key
                                        for remaining_group
                                        in result.groups[index:]
                                        if remaining_group.kind != "steady_2d"
                                        for key in remaining_group.book_keys
                                    ),
                                }
                            )
                        )
                    response = self.conflict_review_dialog_port.choose(
                        ConflictReviewRequest(
                            kind="special_group",
                            title="确认特殊谱组",
                            decision_subject=_special_kind_review_label(
                                group.kind
                            ),
                            instruction="这些 Book 共同组成一个候选特殊谱。",
                            choices=_conflict_choices(
                                group.book_keys,
                                candidate_by_key,
                            ),
                            selection_mode="none",
                            actions=_task7_review_actions(
                                review_state,
                                *(
                                    ("return_related_conflict",)
                                    if review_state.has_related_special_conflict(
                                        group.book_keys
                                    )
                                    else ()
                                ),
                                "confirm_group",
                                "review_books",
                                "reject_group",
                                "return_to_attribution",
                            ),
                        ),
                        parent=self.parent,
                    )
                    if response.action == "cancel":
                        self._cancel_review()
                        return None
                    if response.action == "return_previous":
                        if not review_state.recall_previous():
                            raise ValueError(
                                "Previous review decision is unavailable"
                            )
                        return _REVIEW_RESTART
                    if response.action == "return_related_conflict":
                        related_conflicts = _ordered_related_conflicts(
                            review_state,
                            group.book_keys,
                        )
                        if not related_conflicts:
                            raise ValueError(
                                "Related special conflict is unavailable"
                            )
                        review_state.open_related_conflict_editor(
                            group.book_keys,
                            related_conflicts,
                        )
                        return _REVIEW_RESTART
                    if response.action == "return_to_attribution":
                        if return_to_attribution is None:
                            raise ValueError(
                                "Return-to-attribution handler is unavailable"
                            )
                        return_to_attribution(group.book_keys)
                        return None
                    if response.action in {"confirm_group", "reject_group"}:
                        decision = (response.action, ())
                        break
                    if response.action != "review_books":
                        raise ValueError(
                            "Special-group review response is invalid"
                        )
                    detail_response = self.conflict_review_dialog_port.choose(
                        ConflictReviewRequest(
                            kind="special_group_books",
                            title="逐 Book 确认特殊谱组",
                            decision_subject=(
                                f"{_special_kind_review_label(group.kind)}"
                                " · 逐 Book"
                            ),
                            instruction=(
                                "保留属于该特殊谱的 Book；未选项返回普通审核。"
                            ),
                            choices=_conflict_choices(
                                group.book_keys,
                                candidate_by_key,
                            ),
                            selection_mode="multi",
                            actions=_task7_review_actions(
                                review_state,
                                "confirm_selection",
                                "return_to_group",
                                "return_to_attribution",
                                "cancel",
                            ),
                            initial_selection=review_state.recalled_selection(
                                "special_group",
                                group_key,
                                group.book_keys,
                            ),
                        ),
                        parent=self.parent,
                    )
                    if detail_response.action == "cancel":
                        self._cancel_review()
                        return None
                    if detail_response.action == "return_to_group":
                        review_state.remember_draft(
                            "special_group",
                            group_key,
                            (
                                "confirm_selection",
                                detail_response.selected_book_keys,
                            ),
                            group.book_keys,
                        )
                        continue
                    if detail_response.action == "return_to_attribution":
                        if return_to_attribution is None:
                            raise ValueError(
                                "Return-to-attribution handler is unavailable"
                            )
                        return_to_attribution(group.book_keys)
                        return None
                    if detail_response.action != "confirm_selection":
                        raise ValueError(
                            "Special-group Book review response is invalid"
                        )
                    selected = detail_response.selected_book_keys
                    if (
                        not selected
                        or len(selected) != len(set(selected))
                        or any(key not in group.book_keys for key in selected)
                    ):
                        raise ValueError(
                            "Special-group Book selection is invalid"
                        )
                    decision = ("confirm_selection", selected)
                    break
                review_state.special_group_choices[group_key] = decision
                review_state.remember(
                    "special_group",
                    group_key,
                    group.book_keys,
                )

            action, selected = decision
            if action == "confirm_group":
                accepted_groups.append(group)
                continue
            if action == "reject_group":
                rejected_keys.extend(group.book_keys)
                regular_delayed_keys.extend(
                    _delayed_emission_keys(
                        group.book_keys,
                        candidate_by_key,
                    )
                )
                continue
            if action == "confirm_selection":
                accepted, ordinary_keys = resolve_special_group_selection(
                    group,
                    selected,
                )
                if accepted is None:
                    rejected_keys.extend(group.book_keys)
                else:
                    accepted_groups.append(accepted)
                regular_delayed_keys.extend(
                    _delayed_emission_keys(ordinary_keys, candidate_by_key)
                )
                continue
            raise ValueError("Stored special-group review decision is invalid")
        return accepted_groups, regular_delayed_keys, rejected_keys

    def _review_emission_duplicates(
        self,
        extraction_summary,
        conversion,
        spectra,
        candidate_by_key,
        *,
        review_state: _Task7ReviewState | None = None,
        publish_pending: Callable[[int], None] | None = None,
        additional_pending_book_keys: tuple[str, ...] = (),
    ):
        if review_state is None:
            review_state = _Task7ReviewState.empty()
        choices = review_state.emission_choices
        while True:
            result = review_emission_duplicates(
                list(spectra),
                choices=choices,
            )
            if not result.pending_reviews:
                return result, choices
            pending = result.pending_reviews[0]
            review_state.require(
                "emission",
                pending.review_key,
                pending.book_keys,
            )
            if publish_pending is not None:
                publish_pending(
                    len(
                        {
                            *additional_pending_book_keys,
                            *_pending_review_book_keys(result.pending_reviews),
                        }
                    )
                )
            response = self.conflict_review_dialog_port.choose(
                ConflictReviewRequest(
                    kind="emission_duplicate",
                    title="选择重复发射谱",
                    decision_subject="重复发射谱",
                    instruction="同一样品和测试条件下存在重复候选，请保留一个。",
                    choices=_conflict_choices(
                        pending.book_keys,
                        candidate_by_key,
                    ),
                    selection_mode="single",
                    actions=_task7_review_actions(
                        review_state,
                        "confirm_selection",
                        "return_to_attribution",
                        "cancel",
                    ),
                    initial_selection=review_state.recalled_selection(
                        "emission",
                        pending.review_key,
                    ),
                ),
                parent=self.parent,
            )
            if response.action == "cancel":
                self._cancel_review()
                return None
            if response.action == "return_previous":
                if not review_state.recall_previous():
                    raise ValueError(
                        "Previous review decision is unavailable"
                    )
                return _REVIEW_RESTART
            if response.action == "return_to_attribution":
                self._return_to_attribution_from_review(
                    extraction_summary,
                    conversion,
                    pending.book_keys,
                )
                return None
            if (
                response.action != "confirm_selection"
                or len(response.selected_book_keys) != 1
                or response.selected_book_keys[0] not in pending.book_keys
            ):
                raise ValueError("Emission-duplicate review response is invalid")
            choices[pending.review_key] = response.selected_book_keys[0]
            review_state.remember(
                "emission",
                pending.review_key,
                pending.book_keys,
            )

    def _review_excitations(
        self,
        extraction_summary,
        conversion,
        spectra,
        candidate_by_key,
        *,
        review_state: _Task7ReviewState | None = None,
        publish_pending: Callable[[int], None] | None = None,
    ):
        if review_state is None:
            review_state = _Task7ReviewState.empty()
        choices = review_state.excitation_choices
        while True:
            result = select_excitation_candidates(
                list(spectra),
                choices=choices,
            )
            if not result.pending_reviews:
                return result, choices
            pending = result.pending_reviews[0]
            review_state.require(
                "excitation",
                pending.review_key,
                pending.book_keys,
            )
            if publish_pending is not None:
                publish_pending(
                    len(_pending_review_book_keys(result.pending_reviews))
                )
            instruction = "选择进入后续输出的候选；不同发射波长可同时保留。"
            if pending.single_select_groups:
                instruction += " 标有“完全重复组”的候选，本组最多选择 1 个。"
            response = self.conflict_review_dialog_port.choose(
                ConflictReviewRequest(
                    kind="excitation_selection",
                    title="选择激发谱",
                    decision_subject="激发谱候选",
                    instruction=instruction,
                    choices=_conflict_choices(
                        pending.book_keys,
                        candidate_by_key,
                    ),
                    selection_mode="multi",
                    actions=_task7_review_actions(
                        review_state,
                        "confirm_selection",
                        "return_to_attribution",
                        "cancel",
                    ),
                    single_select_groups=pending.single_select_groups,
                    initial_selection=review_state.recalled_selection(
                        "excitation",
                        pending.review_key,
                        _default_excitation_selection(pending),
                    ),
                ),
                parent=self.parent,
            )
            if response.action == "cancel":
                self._cancel_review()
                return None
            if response.action == "return_previous":
                if not review_state.recall_previous():
                    raise ValueError(
                        "Previous review decision is unavailable"
                    )
                return _REVIEW_RESTART
            if response.action == "return_to_attribution":
                self._return_to_attribution_from_review(
                    extraction_summary,
                    conversion,
                    pending.book_keys,
                )
                return None
            if response.action != "confirm_selection":
                raise ValueError("Excitation review response is invalid")
            choices[pending.review_key] = response.selected_book_keys
            review_state.remember(
                "excitation",
                pending.review_key,
                pending.book_keys,
            )

    def _choose_conflict_review(
        self,
        request: ConflictReviewRequest,
        *,
        return_to_attribution: Callable[[], None] | None = None,
        review_state: _Task7ReviewState | None = None,
    ):
        response = self.conflict_review_dialog_port.choose(
            request,
            parent=self.parent,
        )
        if response.action == "cancel":
            self._cancel_review()
            return None
        if response.action == "return_previous":
            if review_state is None or not review_state.recall_previous():
                raise ValueError("Previous review decision is unavailable")
            return _REVIEW_RESTART
        if response.action == "return_to_attribution":
            if return_to_attribution is None:
                raise ValueError("Return-to-attribution handler is unavailable")
            return_to_attribution()
            return None
        if request.selection_mode == "grouped_single":
            if response.action != "confirm_all_conflicts":
                raise ValueError("Grouped conflict-review response is invalid")
            _validated_group_selections(request, response)
            return response
        if response.action != "confirm_selection":
            raise ValueError("Conflict-review response is invalid")
        selected = response.selected_book_keys
        valid_keys = {choice.book_key for choice in request.choices}
        if (
            not selected
            or len(selected) != len(set(selected))
            or any(key not in valid_keys for key in selected)
            or (request.selection_mode == "single" and len(selected) != 1)
        ):
            raise ValueError("Conflict-review selection is invalid")
        return response

    def _cancel_review(self, *, on_continue=None) -> None:
        if self.orchestrator.cancelled or self.shutdown_pending:
            return
        if on_continue is None:
            self.cancel_after_preferences()
        else:
            self.cancel_after_preferences(on_continue=on_continue)

    def _final_conflict_editor_model(
        self,
        draft: _Task8ReviewDraft,
        selected_book_key: str,
        selections: tuple[FinalReviewConflictSelection, ...],
        *,
        target_book_keys: tuple[str, ...] | None = None,
    ) -> FinalReviewConflictEditor:
        review_state = self.orchestrator.task_cache.get(
            "task7_review_state"
        )
        if not isinstance(review_state, _Task7ReviewState):
            raise RuntimeError("Conflict-review state is unavailable")
        candidates = (
            *tuple(draft.conversion.ordinary_candidates),
            *tuple(draft.conversion.steady_2d_candidates),
        )
        session = self.orchestrator.task_cache.get("attribution_session")
        if not isinstance(session, AttributionSession):
            raise RuntimeError("Attribution session is unavailable")
        projection = _project_final_conflicts(
            candidates,
            session.assignments,
            review_state,
            row_id=selected_book_key,
            target_book_keys=(
                target_book_keys or (selected_book_key,)
            ),
            selections=selections,
        )
        if not projection.editor.groups:
            raise RuntimeError(
                "Selected final-review row has no direct conflict"
            )
        return projection.editor

    def _apply_final_conflict_correction(
        self,
        generation: int,
        draft: _Task8ReviewDraft,
        selected_book_key: str,
        selections: tuple[FinalReviewConflictSelection, ...],
        *,
        target_book_keys: tuple[str, ...] | None = None,
    ) -> None:
        if (
            generation != self._run_generation
            or self.orchestrator.cancelled
            or self.shutdown_pending
        ):
            return
        review_state = self.orchestrator.task_cache.get(
            "task7_review_state"
        )
        if not isinstance(review_state, _Task7ReviewState):
            raise RuntimeError("Conflict-review state is unavailable")
        session = self.orchestrator.task_cache.get("attribution_session")
        if not isinstance(session, AttributionSession):
            raise RuntimeError("Attribution session is unavailable")
        candidates = (
            *tuple(draft.conversion.ordinary_candidates),
            *tuple(draft.conversion.steady_2d_candidates),
        )
        projection = _project_final_conflicts(
            candidates,
            session.assignments,
            review_state,
            row_id=selected_book_key,
            target_book_keys=(
                target_book_keys or (selected_book_key,)
            ),
            selections=selections,
        )
        if not projection.complete or not projection.editor.can_confirm:
            raise ValueError("Final conflict correction is incomplete")
        _apply_conflict_projection_to_state(review_state, projection)
        for key in _TASK8_CONFLICT_CORRECTION_CACHE_KEYS:
            self.orchestrator.task_cache.pop(key, None)
        for key in (
            "selection_spectra",
            "special_groups",
            "rejected_special_book_keys",
            "duplicate_choices",
            "excitation_pairing",
            "task7_selection_exclusions",
            "task7_selected_book_keys",
            "completeness",
            "task7_review_complete",
            *_TASK8_DERIVED_CACHE_KEYS,
            "sample_record_ids",
        ):
            self.orchestrator.task_cache.pop(key, None)
        candidate_by_key = {
            candidate.book_key: candidate
            for candidate in candidates
        }
        assignments = dict(session.assignments)
        self.orchestrator.task_cache["attribution_assignments"] = assignments
        self._begin_conflict_review(
            draft.extraction_summary,
            draft.conversion,
            assignments,
            attribution_rows=_attribution_rows_from_session(
                session,
                candidate_by_key,
                _attribution_book_labels(tuple(candidate_by_key.values())),
            ),
            rejections=tuple(draft.conversion.rejections),
        )

    def _discard_pending_targeted_correction(
        self,
        generation: int,
        draft: _Task8ReviewDraft,
    ) -> None:
        rollback = self.orchestrator.task_cache.get(
            "task8_targeted_attribution_rollback"
        )
        if not isinstance(rollback, _Task8TargetedAttributionRollback):
            raise RuntimeError(
                "Targeted attribution rollback is unavailable"
            )
        self.orchestrator.task_cache[
            "attribution_session"
        ] = rollback.session
        self.orchestrator.task_cache[
            "attribution_assignments"
        ] = dict(rollback.session.assignments)
        if rollback.latest_attribution_decision_book_keys is None:
            self.orchestrator.task_cache.pop(
                "latest_attribution_decision_book_keys",
                None,
            )
        else:
            self.orchestrator.task_cache[
                "latest_attribution_decision_book_keys"
            ] = rollback.latest_attribution_decision_book_keys
        for key in _TASK8_CONFLICT_CORRECTION_CACHE_KEYS:
            self.orchestrator.task_cache.pop(key, None)
        self.schedule_call(
            lambda: self._handle_task8_preparation_success(
                generation,
                draft,
            )
        )

    def _begin_targeted_attribution_correction(
        self,
        generation: int,
        draft: _Task8ReviewDraft,
        selected_book_key: str,
        *,
        resume_draft: AttributionDialogResponse | None = None,
    ) -> None:
        session = self.orchestrator.task_cache.get("attribution_session")
        if not isinstance(session, AttributionSession):
            raise RuntimeError("Attribution session is unavailable")
        candidates = (
            *tuple(draft.conversion.ordinary_candidates),
            *tuple(draft.conversion.steady_2d_candidates),
        )
        candidate_by_key = {
            candidate.book_key: candidate
            for candidate in candidates
        }
        selected = candidate_by_key.get(selected_book_key)
        current = session.assignment_for(selected_book_key)
        if selected is None or current is None:
            raise RuntimeError(
                "Selected final-review attribution row is unavailable"
            )
        folder_candidates = tuple(
            candidate
            for candidate in candidates
            if candidate.source_id == selected.source_id
            and candidate.folder_path == selected.folder_path
        )
        folder_book_keys = tuple(
            candidate.book_key
            for candidate in folder_candidates
        )
        book_labels = _attribution_book_labels(candidates)
        prefill = _sample_form_prefill(current.sample)
        initial_scope = (
            session.confirmed_scope_for(selected_book_key)
            or "book"
        )
        if resume_draft is not None:
            prefill = {
                "sample_type": resume_draft.sample_type,
                **resume_draft.values,
            }
            initial_scope = resume_draft.attribution_scope or initial_scope
        response = self.attribution_dialog_port.choose(
            AttributionDialogRequest(
                target_label=(
                    f"{selected.folder_path or 'Root'} / "
                    f"{book_labels[selected_book_key]}"
                ),
                source_filename=selected.source_filename,
                book_display_names=tuple(
                    book_labels[candidate.book_key]
                    for candidate in folder_candidates
                ),
                prefill=prefill,
                prefill_source=(
                    "unconfirmed_draft"
                    if resume_draft is not None
                    else "previous_attribution"
                ),
                allow_split_folder=len(folder_book_keys) > 1,
                allow_return_previous=True,
                targeted_correction=True,
                initial_scope=initial_scope,
                selected_book_display_name=book_labels[selected_book_key],
                affected_book_count=len(folder_book_keys),
            ),
            parent=self.parent,
        )
        if response.action == "return_previous":
            self.schedule_call(
                lambda: self._handle_task8_preparation_success(
                    generation,
                    draft,
                )
            )
            return
        if response.action == "cancel":
            self._cancel_review(
                on_continue=lambda: self.schedule_call(
                    lambda: self._begin_targeted_attribution_correction(
                        generation,
                        draft,
                        selected_book_key,
                        resume_draft=response,
                    )
                )
            )
            return
        if response.action != "confirm":
            raise ValueError("Targeted attribution response is invalid")
        if response.attribution_scope not in {"book", "folder"}:
            raise ValueError("Targeted attribution scope is invalid")
        attribution = build_attribution_fields(
            response.sample_type,
            response.values,
        )
        affected_book_keys = (
            folder_book_keys
            if response.attribution_scope == "folder"
            else (selected_book_key,)
        )
        previous_latest_scope = self.orchestrator.task_cache.get(
            "latest_attribution_decision_book_keys"
        )
        rollback = _Task8TargetedAttributionRollback(
            session=copy.deepcopy(session),
            latest_attribution_decision_book_keys=(
                tuple(previous_latest_scope)
                if previous_latest_scope is not None
                else None
            ),
        )
        session.replace_assignments(
            affected_book_keys,
            attribution,
            scope=response.attribution_scope,
        )
        review_state = self.orchestrator.task_cache.get(
            "task7_review_state"
        )
        if not isinstance(review_state, _Task7ReviewState):
            raise RuntimeError("Conflict-review state is unavailable")
        projection = _project_final_conflicts(
            candidates,
            session.assignments,
            review_state,
            row_id=selected_book_key,
            target_book_keys=affected_book_keys,
            selections=(),
        )
        self.orchestrator.task_cache[
            "attribution_assignments"
        ] = dict(session.assignments)
        self.orchestrator.task_cache[
            "latest_attribution_decision_book_keys"
        ] = affected_book_keys
        if projection.editor.groups:
            self.orchestrator.task_cache.update(
                {
                    "task8_final_conflict_target_book_keys": (
                        affected_book_keys
                    ),
                    "task8_final_conflict_initial_row_id": (
                        selected_book_key
                    ),
                    "task8_final_conflict_back_action": (
                        "discard_targeted_correction"
                    ),
                    "task8_targeted_attribution_rollback": rollback,
                }
            )
            self.schedule_call(
                lambda: self._handle_task8_preparation_success(
                    generation,
                    draft,
                )
            )
            return
        if not projection.complete:
            raise ValueError("Targeted attribution conflict state is incomplete")
        _apply_conflict_projection_to_state(review_state, projection)
        for key in (
            "selection_spectra",
            "special_groups",
            "rejected_special_book_keys",
            "duplicate_choices",
            "excitation_pairing",
            "task7_selection_exclusions",
            "task7_selected_book_keys",
            "completeness",
            "task7_review_complete",
            *_TASK8_DERIVED_CACHE_KEYS,
            "sample_record_ids",
        ):
            self.orchestrator.task_cache.pop(key, None)
        assignments = dict(session.assignments)
        attribution_rows = _attribution_rows_from_session(
            session,
            candidate_by_key,
            book_labels,
        )
        self._begin_conflict_review(
            draft.extraction_summary,
            draft.conversion,
            assignments,
            attribution_rows=attribution_rows,
            rejections=tuple(draft.conversion.rejections),
        )

    def _return_to_attribution_from_review(
        self,
        extraction_summary,
        conversion,
        book_keys,
    ) -> None:
        session = self.orchestrator.task_cache.get("attribution_session")
        if not isinstance(session, AttributionSession):
            raise RuntimeError("Attribution session is unavailable")
        requested_keys = tuple(book_keys)
        generation = self._run_generation

        def return_to_attribution() -> None:
            if (
                generation != self._run_generation
                or self.orchestrator.cancelled
                or self.shutdown_pending
                or self.orchestrator.task_cache.get("attribution_session")
                is not session
            ):
                return
            reopened_keys, previous = session.reopen(requested_keys)
            review_state = self.orchestrator.task_cache.get(
                "task7_review_state"
            )
            if isinstance(review_state, _Task7ReviewState):
                review_state.discard_books(reopened_keys)
            for key in (
                "selection_spectra",
                "special_groups",
                "rejected_special_book_keys",
                "duplicate_choices",
                "excitation_pairing",
                "task7_selection_exclusions",
                "task7_selected_book_keys",
                "completeness",
                "task7_review_complete",
                *_TASK8_DERIVED_CACHE_KEYS,
                "latest_attribution_decision_book_keys",
                "sample_record_ids",
            ):
                self.orchestrator.task_cache.pop(key, None)
            self.orchestrator.task_cache["attribution_assignments"] = dict(
                session.assignments
            )
            self.orchestrator.task_cache[
                "reopened_attribution_book_keys"
            ] = reopened_keys
            self._log(
                "已返回当前项的样品归属；无关的既有审核决定已保留。"
            )

            def resume_attribution() -> None:
                if self._defer_during_cancel_confirmation(
                    resume_attribution
                ):
                    return
                if (
                    generation != self._run_generation
                    or self.orchestrator.cancelled
                    or self.shutdown_pending
                    or self.orchestrator.task_cache.get(
                        "attribution_session"
                    )
                    is not session
                ):
                    return
                self._begin_attribution(
                    extraction_summary,
                    conversion,
                    resume_session=session,
                    reopened_attributions=previous,
                )

            self.schedule_call(resume_attribution)

        if self._defer_during_cancel_confirmation(return_to_attribution):
            return
        return_to_attribution()

    def _show_conflict_review_finished(
        self,
        extraction_summary,
        *,
        selected_count: int,
        special_count: int,
        attribution_rows,
        rejections,
        selection_exclusion_rows,
    ) -> None:
        total_inventory = _summary_value(
            extraction_summary,
            "total_inventory_count",
            0,
        )
        total_extracted = _summary_value(
            extraction_summary,
            "total_extracted_count",
            0,
        )
        exclusion_rows = (
            *_candidate_rejection_rows(rejections),
            *selection_exclusion_rows,
        )
        self._log(
            f"冲突审核完成：保留 {selected_count} 个普通谱，"
            f"确认 {special_count} 个特殊组，"
            f"排除 {len(exclusion_rows)} 条记录；等待后续确认"
        )
        self._runtime_update(
            stage="conflict_review",
            phase_detail="已完成",
            runtime_status="等待后续确认",
            activity_mode="manual",
            title="冲突审核已完成",
            subtitle=(
                "特殊组、重复发射谱和激发谱选择仅保存在本次任务中；"
                "尚未生成输出，也未写入样品库。"
            ),
            progress=90,
            progress_busy=False,
            summary_numbers=(
                str(total_inventory),
                str(total_extracted),
                "0",
                str(len(exclusion_rows)),
            ),
            review_headers=("来源文件", "归属范围 / Book", "归属或排除结果"),
            review_rows=(
                *tuple(attribution_rows),
                *exclusion_rows,
            ),
            show_review_table=bool(attribution_rows or exclusion_rows),
            show_attention=False,
            show_input_controls=False,
        )

    def _show_task8_approved(
        self,
        extraction_summary,
        summary,
        *,
        approved_snapshot,
    ) -> None:
        review_rows = _approved_snapshot_review_rows(
            approved_snapshot
        )
        reconciliation = approved_snapshot.count_reconciliation
        self._log(
            "最终归属与输出计划已确认；Approved Snapshot 已冻结。"
            "本阶段未启动 Origin、未创建 .opju、未写入样品库。"
        )
        self._runtime_update(
            stage="output",
            phase_detail="已确认",
            runtime_status="等待人工验收",
            activity_mode="manual",
            title="输出计划已确认",
            subtitle=(
                f"{summary.folder_count} 个 Folder · "
                f"{summary.book_count} 个 Book · "
                f"{summary.column_count} 列；"
                "尚未生成 Origin 输出。"
            ),
            progress=92,
            progress_busy=False,
            summary_numbers=(
                str(
                    _summary_value(
                        extraction_summary,
                        "total_inventory_count",
                        0,
                    )
                ),
                str(
                    self.orchestrator.task_cache[
                        "count_reconciliation"
                    ].accepted_ordinary_spectrum_count
                ),
                "0",
                str(
                    reconciliation.rejected_book_count
                    + reconciliation.excluded_book_count
                ),
            ),
            review_headers=(
                "来源文件",
                "归属范围 / Book",
                "最终归属或排除结果",
            ),
            review_rows=review_rows,
            show_review_table=bool(review_rows),
            show_attention=False,
            show_input_controls=False,
        )

    def _show_attribution_progress(
        self,
        members,
        *,
        completed: int,
        total: int,
        pending_books: int,
        usable_books: int,
        rejections,
    ) -> None:
        target_text = (
            members[0].folder_path
            or f"Root / {_visible_book_name(members[0])}"
        )
        rejection_rows = _candidate_rejection_rows(
            rejections,
            include_status=False,
        )
        self._runtime_update(
            stage="attribution",
            phase_detail=f"{completed}/{total} 项",
            runtime_status="等待确认样品归属",
            activity_mode="manual",
            title="确认样品归属",
            subtitle=f"正在确认 {members[0].source_filename} · {target_text}",
            progress=72 if total == 0 else 72 + int(8 * completed / total),
            progress_busy=False,
            summary_numbers=(
                str(_summary_value(self.orchestrator.task_cache["extraction_summary"], "total_inventory_count", 0)),
                str(usable_books),
                str(pending_books),
                str(len(rejections)),
            ),
            review_headers=("来源文件", "Folder / Book", "排除原因"),
            review_rows=rejection_rows,
            show_review_table=bool(rejection_rows),
            show_attention=False,
            show_input_controls=False,
        )

    def _show_attribution_finished(
        self,
        extraction_summary,
        rows,
        assignment_count: int,
        *,
        usable_books: int,
        rejections,
    ) -> None:
        total_inventory = _summary_value(extraction_summary, "total_inventory_count", 0)
        rejection_rows = _candidate_rejection_rows(rejections)
        review_rows = (*tuple(rows), *rejection_rows)
        self._log(
            f"样品归属完成：已归属 {assignment_count} 个可用 Book，"
            f"保留 {len(rejections)} 条排除记录；本阶段未写入样品库"
        )
        self._runtime_update(
            stage="attribution",
            phase_detail="已完成",
            runtime_status="样品归属已完成",
            activity_mode="manual",
            title="样品归属已完成",
            subtitle="归属结果仅保存在本次任务中；请先完成本阶段验收，再决定是否继续。",
            progress=80,
            progress_busy=False,
            summary_numbers=(str(total_inventory), str(usable_books), "0", str(len(rejections))),
            review_headers=("来源文件", "归属范围 / Book", "归属或排除结果"),
            review_rows=review_rows,
            show_review_table=bool(review_rows),
            show_attention=False,
            show_input_controls=False,
        )

    def _runtime_update(self, **kwargs) -> None:
        if "app_run_status" not in self.widgets:
            return
        issues = _active_source_input_issues(
            self.orchestrator.task_cache.get("extraction_summary"),
            self._active_source_input_issues,
        )
        if issues:
            issue_message = _input_issue_message(issues)
            explicit_message = str(kwargs.get("attention_message") or "").strip()
            if explicit_message and explicit_message != issue_message:
                issue_message = f"{explicit_message}\n\n{issue_message}"
            kwargs["attention_message"] = issue_message
            kwargs["show_attention"] = True
        self.update_runtime_view(**kwargs)

    def _pause_runtime_for_cancel_confirmation(self):
        progress = self.widgets.get("run_progress")
        status = self.widgets.get("app_run_status")
        if progress is None or status is None:
            return None
        was_busy = progress.minimum() == 0 and progress.maximum() == 0
        snapshot = {
            "runtime_status": status.text(),
            "activity_mode": self.widgets.get("runtime_activity_mode", "manual"),
            "progress_busy": was_busy,
            "progress": max(0, progress.value()),
        }
        self._runtime_update(
            runtime_status="等待确认是否取消任务",
            activity_mode="manual",
            progress=0 if was_busy else snapshot["progress"],
            progress_busy=False,
        )
        return snapshot

    def _restore_runtime_after_cancel_confirmation(self, snapshot) -> None:
        if snapshot is None:
            return
        update = {
            "runtime_status": snapshot["runtime_status"],
            "activity_mode": snapshot["activity_mode"],
            "progress_busy": snapshot["progress_busy"],
        }
        if not snapshot["progress_busy"]:
            update["progress"] = snapshot["progress"]
        self._runtime_update(**update)

    def apply_confirmed_preflight_settings(
        self,
        *,
        s1_limit: int,
        steady_emission_y: str,
        allow_missing_s1: bool = False,
    ) -> None:
        self._persist_setting_with_damage_recovery(
            lambda: self.orchestrator.confirm_preflight_settings(
                s1_limit=s1_limit,
                steady_emission_y=steady_emission_y,
                allow_missing_s1=allow_missing_s1,
            )
        )
        self.default_s1_limit = s1_limit
        self.default_steady_emission_y = steady_emission_y
        self.default_allow_missing_s1 = allow_missing_s1
        self._set_label(
            "preflight_settings_summary_label",
            f"预检设置：S1 强度上限 {s1_limit}，发射谱 Y 列 {steady_emission_y}，"
            f"缺少 S1 时继续 {'是' if allow_missing_s1 else '否'}",
        )
        self._log("预检设置已确认")

    def _persist_setting_with_damage_recovery(self, operation) -> None:
        notices = list(operation() or ())
        damage_notices = [notice for notice in notices if notice.severity == "conspicuous"]
        if not damage_notices:
            self._publish_notices(notices)
            return
        for notice in damage_notices:
            self.manual_dialog_port.choose(
                DialogRequest(
                    kind="settings_reset_notice",
                    title="设置文件损坏",
                    message=notice.message,
                    actions=("acknowledge",),
                )
            )
        store = self.orchestrator.settings_store
        discard_notices = list(store.discard_damaged_file() or ())
        self._publish_notices([*notices, *discard_notices])
        if not getattr(store, "damaged_file_pending", False):
            self._publish_notices(operation())

    def _defer_during_cancel_confirmation(self, callback: Callable[[], None]) -> bool:
        if not self._cancel_confirmation_pending:
            return False
        self._deferred_cancel_confirmation_callbacks.append(callback)
        return True

    def _finish_cancel_confirmation(self, *, replay: bool) -> None:
        callbacks = tuple(self._deferred_cancel_confirmation_callbacks)
        self._deferred_cancel_confirmation_callbacks.clear()
        self._cancel_confirmation_pending = False
        if replay:
            for callback in callbacks:
                callback()

    def _mark_task_cancelled(self) -> None:
        if self.orchestrator.cancelled:
            return
        self._stop_extraction_run_activity()
        self.orchestrator.cancel_after_preferences()
        self._run_generation += 1

    def _output_commit_has_completed(self) -> bool:
        return self._output_stage_coordinator.commit_has_completed()

    def cancel_after_preferences(self, *, on_continue=None) -> None:
        if self.shutdown_pending:
            return
        if self._output_commit_has_completed():
            self._output_committed = True
            self._log("输出已经提交，正在完成收尾；此时不能再取消任务。")
            return
        if self._shutdown_exit_blocked:
            self._cancel_and_exit_after_preferences()
            return
        self._cancel_confirmation_pending = True
        runtime_snapshot = self._pause_runtime_for_cancel_confirmation()
        try:
            response = self.manual_dialog_port.choose(cancel_and_exit_confirmation_dialog())
        except Exception:
            self._restore_runtime_after_cancel_confirmation(runtime_snapshot)
            self._finish_cancel_confirmation(replay=True)
            raise
        if response.action != "取消并退出":
            self._restore_runtime_after_cancel_confirmation(runtime_snapshot)
            self._log("已继续运行")
            self._finish_cancel_confirmation(replay=True)
            if on_continue is not None:
                on_continue()
            return
        if self._output_commit_has_completed():
            self._output_committed = True
            self._finish_cancel_confirmation(replay=True)
            self._log("输出已经提交，正在完成收尾；此时不能再取消任务。")
            return
        if self._output_stage_active:
            self._cancel_and_exit_after_preferences()
            return
        self._mark_task_cancelled()
        self._finish_cancel_confirmation(replay=True)
        self._cancel_and_exit_after_preferences()

    def _cancel_and_exit_after_preferences(self) -> None:
        if self.shutdown_pending:
            return
        if self._output_commit_has_completed():
            self._output_committed = True
            self._log("输出已经提交，正在完成收尾；此时不能再取消任务。")
            return
        if self._shutdown_exit_blocked:
            retry_cleanup = (
                getattr(self.output_stage_runner, "retry_cleanup", None)
                if self._shutdown_cleanup_owner == "output"
                else getattr(self.start_run_runner, "retry_cleanup", None)
            )
            if callable(retry_cleanup):
                self.shutdown_pending = True
                finish_retry = (
                    self._finish_output_cleanup_retry
                    if self._shutdown_cleanup_owner == "output"
                    else self._finish_cleanup_retry
                )
                if retry_cleanup(finish_retry):
                    if self._shutdown_cleanup_owner == "output":
                        self._log("正在重试安全结束输出与校验子进程")
                    else:
                        self._log("正在重试安全结束谱图提取子进程")
                    return
                self.shutdown_pending = False
            self.message_box.blocking_error(
                self.parent,
                title="暂时无法退出",
                message=self._shutdown_error or "Origin 进程退出状态仍无法确认，请稍后重试取消任务",
            )
            return
        cancel_output = getattr(
            self.output_stage_runner,
            "cancel",
            None,
        )
        if self._output_stage_active and callable(cancel_output):
            self.shutdown_pending = True
            if cancel_output(self._finish_output_pending_shutdown):
                self._finish_cancel_confirmation(replay=False)
                self._log("正在安全结束输出与校验子进程")
                return
            self.shutdown_pending = False
            if self._output_commit_has_completed():
                self._output_committed = True
                self._finish_cancel_confirmation(replay=True)
                self._log("输出已经提交，正在完成收尾；此时不能再取消任务。")
                return
            self._output_stage_active = False
            self._mark_task_cancelled()
            self._finish_cancel_confirmation(replay=True)
        cancel_task8 = getattr(
            self.task8_runner,
            "cancel",
            None,
        )
        if callable(cancel_task8):
            self.shutdown_pending = True
            if cancel_task8(
                self._finish_task8_pending_shutdown
            ):
                self._log(
                    "正在安全结束最终输出审核后台任务"
                )
                return
            self.shutdown_pending = False
        if not self.run_in_progress:
            context = self.approved_pre_extraction_context or self.orchestrator.task_cache.get(
                "approved_pre_extraction_context"
            )
            self._mark_task_cancelled()
            cleanup_error = _cleanup_temp_root_error(
                getattr(context, "temp_root", None),
                expected_root_identity=getattr(
                    context,
                    "temp_root_identity",
                    None,
                ),
            )
            if cleanup_error is not None:
                self._shutdown_cleanup_temp_root = getattr(context, "temp_root", None)
                self._shutdown_cleanup_temp_root_identity = getattr(
                    context,
                    "temp_root_identity",
                    None,
                )
                self._shutdown_error = f"取消后临时文件清理失败：{cleanup_error}"
                self._shutdown_exit_blocked = True
                self.message_box.blocking_error(
                    self.parent,
                    title="取消任务时发生错误",
                    message=self._shutdown_error,
                )
                return
            self.approved_pre_extraction_context = None
            self.orchestrator.task_cache.clear()
            self._shutdown_cleanup_temp_root = None
            self._shutdown_cleanup_temp_root_identity = None
            self._shutdown_cleanup_owner = None
        self._mark_task_cancelled()
        self._shutdown_error = None
        self._shutdown_exit_blocked = False
        self._shutdown_cleanup_owner = None
        self.run_in_progress = False
        self._log("任务已取消，已确认的偏好设置保留")
        close = getattr(self.parent, "close", None)
        cancel = getattr(self.start_run_runner, "cancel", None)
        if callable(cancel):
            self.shutdown_pending = True
            if cancel(self._finish_pending_shutdown):
                self._log("正在安全结束谱图提取子进程")
                return
            self.shutdown_pending = False
        if close is not None:
            self.manual_dialog_port.choose(cancelled_and_exited_dialog())
            close()

    def _finish_task8_pending_shutdown(self) -> None:
        self.shutdown_pending = False
        self._task8_phase = None
        self._cancel_and_exit_after_preferences()

    def _finish_output_pending_shutdown(self) -> None:
        self._output_stage_coordinator.finish_pending_shutdown()

    def _finish_output_cleanup_retry(self, error) -> None:
        self._output_stage_coordinator.finish_cleanup_retry(error)

    def _finish_pending_shutdown(self) -> None:
        self.shutdown_pending = False
        shutdown_error = self._shutdown_error
        exit_blocked = self._shutdown_exit_blocked
        self._shutdown_error = None
        if shutdown_error:
            self.message_box.blocking_error(
                self.parent,
                title="取消任务时发生错误",
                message=shutdown_error,
            )
        if exit_blocked:
            self._shutdown_exit_blocked = True
            self._log("安全清理状态暂时无法确认；临时文件已保留，可再次取消任务以重试安全清理")
            return
        self._shutdown_exit_blocked = False
        self._shutdown_cleanup_owner = None
        close = getattr(self.parent, "close", None)
        if close is not None:
            self.manual_dialog_port.choose(cancelled_and_exited_dialog())
            close()

    def _finish_cleanup_retry(self, error) -> None:
        self.shutdown_pending = False
        if error is not None:
            self._shutdown_error = str(error)
            self._shutdown_exit_blocked = True
            self.message_box.blocking_error(
                self.parent,
                title="取消任务时发生错误",
                message=self._shutdown_error,
            )
            self._log(self._shutdown_error)
            return
        cleanup_error = _cleanup_temp_root_error(
            self._shutdown_cleanup_temp_root,
            expected_root_identity=(
                self._shutdown_cleanup_temp_root_identity
            ),
        )
        if cleanup_error is not None:
            self._shutdown_error = f"取消后临时文件清理失败：{cleanup_error}"
            self._shutdown_exit_blocked = True
            self.message_box.blocking_error(
                self.parent,
                title="取消任务时发生错误",
                message=self._shutdown_error,
            )
            self._log(self._shutdown_error)
            return
        if self._shutdown_cleanup_temp_root is not None:
            self.approved_pre_extraction_context = None
            self.orchestrator.task_cache.clear()
            self._shutdown_cleanup_temp_root = None
            self._shutdown_cleanup_temp_root_identity = None
        if not self.orchestrator.cancelled:
            self.orchestrator.cancel_after_preferences()
            self._run_generation += 1
        self.run_in_progress = False
        self._shutdown_error = None
        self._shutdown_exit_blocked = False
        self._shutdown_cleanup_owner = None
        self._log("任务已取消，已确认的偏好设置保留")
        close = getattr(self.parent, "close", None)
        if close is not None:
            self.manual_dialog_port.choose(cancelled_and_exited_dialog())
            close()

    def update_runtime_view(self, **kwargs) -> None:
        update_production_runtime_view(self.widgets, **kwargs)
        if kwargs.get("show_input_controls") is not None:
            self._input_controls_visible = bool(kwargs["show_input_controls"])
        self._refresh_start_button_visibility()

    def _connect_buttons(self) -> None:
        self._connect_clicked("select_sources_button", self.choose_source_files)
        self._connect_clicked("select_output_parent_button", self.choose_output_parent)
        self._connect_clicked("start_run_button", self.request_start_run)
        self._connect_clicked("cancel_run_button", self.cancel_after_preferences)
        self._connect_clicked(
            "open_output_folder_button",
            self.open_output_folder,
        )
        self._connect_clicked(
            "start_new_task_button",
            self.start_new_task,
        )
        self._connect_clicked(
            "exit_application_button",
            self.exit_application,
        )

    def open_output_folder(self) -> None:
        completion = self.orchestrator.task_cache.get(
            "output_completion"
        )
        if completion is None:
            self._blocking_error(
                "无法打开输出文件夹",
                "当前任务没有已发布的输出位置。",
            )
            return
        try:
            self.open_path(Path(completion.output_path))
        except Exception as exc:
            self._blocking_error(
                "无法打开输出文件夹",
                f"{completion.output_path}\n{exc}",
            )

    def start_new_task(self) -> None:
        if self._retry_committed_cleanup_before(self.start_new_task):
            return
        self._run_generation += 1
        self._stop_extraction_run_activity()
        self.orchestrator.start_new_task()
        self.selected_source_paths = ()
        self.output_parent = ""
        self.source_selection_blocked = False
        self.run_ready = False
        self.run_in_progress = False
        self.shutdown_pending = False
        self.approved_pre_extraction_context = None
        self._task8_phase = None
        self._output_stage_active = False
        self._output_committed = False
        self._input_controls_visible = True
        self._set_selection_button_unconfirmed(
            "select_sources_button",
            "选择 Origin 原始文件",
        )
        self._set_selection_button_unconfirmed(
            "select_output_parent_button",
            "选择输出位置",
        )
        self._runtime_update(
            stage="source_input",
            phase_detail="等待选择",
            runtime_status="等待选择输入文件",
            activity_mode="idle",
            title="选择输入文件",
            subtitle="选择 Origin 原始文件和输出位置，然后开始任务。",
            progress=0,
            progress_busy=False,
            summary_numbers=("0", "0", "0", "0"),
            review_rows=(),
            show_review_table=False,
            show_attention=False,
            show_input_controls=True,
            show_completion_actions=False,
        )
        self._log("已开始新任务")

    def exit_application(self) -> None:
        if self._retry_committed_cleanup_before(self.exit_application):
            return
        self._run_generation += 1
        self.run_in_progress = False
        self._output_stage_active = False
        self._output_committed = False
        self.approved_pre_extraction_context = None
        self.orchestrator.start_new_task()
        close = getattr(self.parent, "close", None)
        if callable(close):
            close()

    def _retry_committed_cleanup_before(self, continuation) -> bool:
        if not self.orchestrator.task_cache.get(
            "output_post_commit_cleanup_pending"
        ):
            return False
        retry_cleanup = getattr(
            self.output_stage_runner,
            "retry_cleanup",
            None,
        )
        if not callable(retry_cleanup) or self.shutdown_pending:
            self._blocking_error(
                "暂时无法继续",
                "已提交输出的收尾清理尚未完成，请稍后重试。",
            )
            return True
        self.shutdown_pending = True

        def finish(error) -> None:
            self.shutdown_pending = False
            if error is not None:
                self._shutdown_error = str(error)
                self._shutdown_exit_blocked = True
                self.orchestrator.task_cache[
                    "output_post_commit_error"
                ] = error
                self._blocking_error(
                    "收尾清理仍未完成",
                    str(error),
                )
                return
            self.orchestrator.task_cache.pop(
                "output_post_commit_error",
                None,
            )
            self.orchestrator.task_cache.pop(
                "output_post_commit_cleanup_pending",
                None,
            )
            self._shutdown_error = None
            self._shutdown_exit_blocked = False
            self._shutdown_cleanup_owner = None
            continuation()

        if retry_cleanup(finish):
            self._log("正在重试已提交输出的收尾清理")
            return True
        self.shutdown_pending = False
        self._blocking_error(
            "暂时无法继续",
            "已提交输出的收尾清理当前无法启动，请稍后重试。",
        )
        return True

    def _connect_clicked(self, widget_name: str, handler) -> None:
        widget = self.widgets.get(widget_name)
        clicked = getattr(widget, "clicked", None)
        if clicked is not None:
            clicked.connect(handler)

    def _blocking_error(self, title: str, message: str) -> None:
        self._log(message)
        self.message_box.blocking_error(self.parent, title=title, message=message)

    def _log(self, message: str) -> None:
        log_widget = self.widgets.get("run_log")
        if log_widget is not None:
            timestamped_message = f"{datetime.now().strftime('%H:%M:%S')}  {message}"
            append_plain = getattr(log_widget, "appendPlainText", None)
            if append_plain is not None:
                append_plain(timestamped_message)
            else:
                log_widget.append(timestamped_message)
            scrollbar = getattr(log_widget, "verticalScrollBar", lambda: None)()
            if scrollbar is not None:
                scrollbar.setValue(scrollbar.maximum())

    def _publish_notices(self, notices) -> None:
        for notice in notices or ():
            if notice.severity == "conspicuous":
                prefix = "设置文件损坏"
            else:
                prefix = "设置警告"
            message = f"{prefix}：{notice.message}"
            self._log(message)
            if notice.severity in {"conspicuous", "warning"} and "app_run_status" in self.widgets:
                self._runtime_update(attention_message=message, show_attention=True)

    def _set_label(self, widget_name: str, value: str) -> None:
        widget = self.widgets.get(widget_name)
        if widget is not None:
            widget.setText(value)
            show = getattr(widget, "show", None)
            if show is not None:
                show()

    def _set_selection_button_confirmed(self, widget_name: str, value: str) -> None:
        widget = self.widgets.get(widget_name)
        if widget is None:
            return
        widget.setText(value)
        widget.setProperty("selection_confirmed", True)
        style = getattr(widget, "style", lambda: None)()
        if style is not None:
            style.unpolish(widget)
            style.polish(widget)
        update = getattr(widget, "update", None)
        if update is not None:
            update()

    def _set_selection_button_unconfirmed(
        self,
        widget_name: str,
        value: str,
    ) -> None:
        widget = self.widgets.get(widget_name)
        if widget is None:
            return
        widget.setText(value)
        widget.setProperty("selection_confirmed", False)
        style = getattr(widget, "style", lambda: None)()
        if style is not None:
            style.unpolish(widget)
            style.polish(widget)
        update = getattr(widget, "update", None)
        if callable(update):
            update()

    def _refresh_start_button_visibility(self) -> None:
        button = self.widgets.get("start_run_button")
        if button is None:
            return
        should_show = (
            bool(self.selected_source_paths)
            and bool(self.output_parent)
            and not self.source_selection_blocked
            and not self._shutdown_exit_blocked
            and self._input_controls_visible
            and not self._startup_health_gate_pending
        )
        set_workflow_visible = getattr(button, "set_workflow_visible", None)
        if callable(set_workflow_visible):
            set_workflow_visible(should_show)
        else:
            button.setVisible(should_show)

    def set_startup_health_gate_pending(self, pending: bool) -> None:
        self._startup_health_gate_pending = bool(pending)
        for name in (
            "select_sources_button",
            "select_output_parent_button",
            "start_run_button",
        ):
            widget = self.widgets.get(name)
            set_enabled = getattr(widget, "setEnabled", None)
            if callable(set_enabled):
                set_enabled(not self._startup_health_gate_pending)
        self._refresh_start_button_visibility()

    def _update_input_selection_copy(self) -> None:
        if not self._input_controls_visible:
            return
        source_count = len(self.selected_source_paths)
        if source_count and self.output_parent:
            status = "输入已就绪"
            title = "准备开始任务"
            subtitle = "原始文件和输出位置已确认，开始前将确认预检设置。"
            phase_detail = "准备就绪"
        elif source_count:
            status = "等待选择输出位置"
            title = "选择输出位置"
            subtitle = f"已选择 {source_count} 个原始文件，请确认输出位置。"
            phase_detail = "等待输出位置"
        elif self.output_parent:
            status = "等待选择输入文件"
            title = "选择输入文件"
            subtitle = "输出位置已确认，请选择 Origin 原始文件。"
            phase_detail = "等待原始文件"
        else:
            return
        self._set_label("app_run_status", status)
        self._set_label("current_task_title", title)
        self._set_label("current_task_subtitle", subtitle)
        phase_labels = self.widgets.get("phase_labels")
        if isinstance(phase_labels, dict) and phase_labels.get("source_input") is not None:
            phase_labels["source_input"].setText(f"输入文件\n{phase_detail}")


def _selection_spectrum_from_candidate(
    candidate,
    attribution,
) -> SelectionSpectrum:
    is_excitation = candidate.spectrum_class in {
        SpectrumClass.STEADY_EXCITATION,
        SpectrumClass.DELAYED_EXCITATION,
    }
    wavelength_range = candidate.wavelength_range or (None, None)
    return SelectionSpectrum(
        source_id=candidate.source_id,
        source_filename=candidate.source_filename,
        folder_path=candidate.folder_path,
        book_name=candidate.short_name,
        display_name=_visible_book_name(candidate),
        default_name=candidate.short_name,
        spectrum_class=candidate.spectrum_class,
        sample_system=attribution.sample.identity_json(),
        temperature=attribution.sample.temperature,
        page_type=candidate.page_type,
        fixed_excitation_wavelength=(
            None if is_excitation else candidate.fixed_wavelength
        ),
        fixed_receiving_wavelength=(
            candidate.fixed_wavelength if is_excitation else None
        ),
        excitation_slit=format_raw_slit_fields(candidate.excitation_slits),
        emission_slit=format_raw_slit_fields(candidate.emission_slits),
        flash_delay=candidate.flash_delay,
        sample_window=candidate.sample_window,
        time_per_flash=candidate.time_per_flash,
        flash_count=candidate.flash_count,
        scan_start=wavelength_range[0],
        scan_stop=wavelength_range[1],
        scan_step=candidate.scan_increment,
        x_at_max_y=format_maximum_x(candidate.x_at_max_y),
        max_y=_measurement_text(candidate.max_y),
        note_datetime=candidate.note_datetime,
    )


def _output_spectrum_from_candidate(
    candidate,
    attribution,
    *,
    selection_order: int,
    reviewed_payload=None,
) -> OutputSpectrum:
    if reviewed_payload is None:
        x_values = tuple(candidate.x_values)
        y_values = tuple(candidate.y_values)
    else:
        x_values = tuple(reviewed_payload[0])
        y_values = tuple(reviewed_payload[1])
    if len(x_values) != len(y_values):
        raise ValueError(
            f"selected X/Y length mismatch for {candidate.book_key}"
        )
    wavelength_range = candidate.wavelength_range or (None, None)
    sample = attribution.sample
    return OutputSpectrum(
        spectrum_id=candidate.book_key,
        spectrum_class=candidate.spectrum_class,
        canonical_sample_label=sample.canonical_label,
        sample_system_label=sample.system_label,
        sample_system_identity=sample.system_identity_json(),
        temperature=sample.temperature,
        key_wavelength=str(candidate.fixed_wavelength or ""),
        x_y=tuple(zip(x_values, y_values, strict=True)),
        excitation_slit=candidate.excitation_slits,
        emission_slit=candidate.emission_slits,
        flash_delay=candidate.flash_delay,
        sample_window=candidate.sample_window,
        time_per_flash=candidate.time_per_flash,
        flash_count=candidate.flash_count,
        scan_start=wavelength_range[0],
        scan_stop=wavelength_range[1],
        scan_step=candidate.scan_increment,
        selection_order=selection_order,
    )


def _load_reviewed_output_payloads(
    candidates,
    extraction_summary,
    *,
    cancel_check=None,
):
    pending = []
    for candidate in candidates:
        if cancel_check is not None:
            cancel_check()
        has_x = bool(candidate.x_values)
        has_y = bool(candidate.y_values)
        if has_x != has_y:
            raise CandidateConversionError(
                f"reviewed selected X/Y payload is incomplete for {candidate.book_key}"
            )
        if has_x:
            continue
        snapshot_path = getattr(candidate, "payload_snapshot_path", None)
        checksum = getattr(candidate, "payload_checksum", None)
        if snapshot_path is None or not checksum:
            raise CandidateConversionError(
                f"reviewed selected X/Y payload is unavailable for {candidate.book_key}"
            )
        pending.append(candidate)
    if not pending:
        return {}

    snapshot_path_value = _summary_value(
        extraction_summary,
        "snapshot_path",
        "",
    )
    snapshot_sha256 = _summary_value(
        extraction_summary,
        "snapshot_sha256",
        "",
    )
    if not snapshot_path_value or not isinstance(snapshot_sha256, str):
        raise CandidateConversionError(
            "提取结果缺少任务快照路径或校验值"
        )
    snapshot_path = Path(snapshot_path_value)
    resolved_snapshot_path = snapshot_path.resolve()
    for candidate in pending:
        if Path(candidate.payload_snapshot_path).resolve() != resolved_snapshot_path:
            raise CandidateConversionError(
                f"reviewed Book payload snapshot changed for {candidate.book_key}"
            )
    requests = tuple(
        (
            candidate.source_id,
            candidate.page_type,
            candidate.folder_path,
            candidate.short_name,
            candidate.payload_checksum,
        )
        for candidate in pending
    )
    payloads = load_book_payloads_read_only(
        snapshot_path,
        expected_snapshot_sha256=snapshot_sha256,
        requests=requests,
        cancel_check=cancel_check,
    )
    hydrated = {}
    for candidate, payload in zip(pending, payloads, strict=True):
        if cancel_check is not None:
            cancel_check()
        if not payload[0] or not payload[1]:
            raise CandidateConversionError(
                f"reviewed selected X/Y payload is empty for {candidate.book_key}"
            )
        hydrated[candidate.book_key] = payload
    return hydrated


def _write_task8_failure_log(message: str) -> Path:
    return write_failure_log(
        datetime.now().strftime("%Y%m%d_%H%M%S"),
        message,
    )


def _verify_approved_output_sources(before, cancel_check):
    return tuple(
        snapshot_sources(
            [Path(item.path) for item in before],
            [],
            cancel_check=cancel_check,
        )
    )


def _cleanup_committed_output(snapshot, _completion) -> None:
    temp_root = Path(snapshot.task_snapshot_path).parent
    cleanup_error = _cleanup_temp_root_error(
        temp_root,
        expected_root_identity=snapshot.task_temp_root_identity,
    )
    if cleanup_error is not None:
        raise ExtractionCleanupBlockedError(
            f"输出已提交，但任务临时文件清理失败：{cleanup_error}"
        )


def _retry_committed_output_cleanup(snapshot, completion) -> None:
    retry_post_commit_cleanup(completion)
    _cleanup_committed_output(snapshot, completion)


def _output_failure_diagnostics(error) -> str:
    return output_failure_diagnostics(error)


def _output_cleanup_is_blocked(error) -> bool:
    return output_cleanup_is_blocked(error)


def _exception_with_notes(error: BaseException) -> str:
    lines = [str(error)]
    lines.extend(
        f"附加错误：{note}"
        for note in getattr(error, "__notes__", ())
    )
    return "\n".join(lines)


def _task8_failure_diagnostic(
    phase: str,
    error,
    draft: _Task8ReviewDraft | None,
) -> str:
    lines = [
        f"phase={phase}",
        f"error_type={type(error).__name__}",
        f"error={error}",
        "traceback=",
        "".join(
            traceback.format_exception(
                type(error),
                error,
                getattr(error, "__traceback__", None),
            )
        ).rstrip(),
    ]
    if draft is None:
        return "\n".join(lines)
    requirements = tuple(
        getattr(draft, "review_requirements", ())
    )
    choices = tuple(getattr(draft, "review_choices", ()))
    details = [
        *lines,
        f"review_requirement_count={len(requirements)}",
        f"review_choice_count={len(choices)}",
    ]
    details.extend(
        f"requirement[{index}]={requirement!r}"
        for index, requirement in enumerate(requirements)
    )
    details.extend(
        f"choice[{index}]={choice!r}"
        for index, choice in enumerate(choices)
    )
    return "\n".join(details)


def _approved_exclusions(
    excluded_candidates,
    selection_exclusions,
    review_choices,
) -> tuple[ApprovedAuditItem, ...]:
    reasons = {
        exclusion.book_key: str(exclusion.reason)
        for exclusion in selection_exclusions
    }
    items = []
    for candidate in excluded_candidates:
        reason_code = reasons.get(
            candidate.book_key,
            "special_group_not_copied_to_ordinary_output",
        )
        expected_kind = {
            "emission_duplicate_unselected": "emission",
            "exact_excitation_duplicate_unselected": "excitation",
            "excitation_candidate_unselected": "excitation",
            "special_group_rejected": "special_group",
            "special_group_not_copied_to_ordinary_output": "special_group",
            "special_duplicate_unselected": "special_duplicate",
        }[reason_code]
        matching_choices = tuple(
            choice
            for choice in review_choices
            if choice.kind == expected_kind
            and candidate.book_key in choice.candidate_book_keys
            and (
                (
                    reason_code
                    == "special_group_not_copied_to_ordinary_output"
                    and candidate.book_key in choice.selected_book_keys
                )
                or (
                    reason_code
                    != "special_group_not_copied_to_ordinary_output"
                    and candidate.book_key not in choice.selected_book_keys
                )
            )
        )
        if not matching_choices:
            raise ValueError(
                "selection exclusion does not map to a causative review "
                f"choice: {candidate.book_key}"
            )
        choice = matching_choices[-1]
        evidence = tuple(
            (name, value)
            for name, value in (
                ("review_kind", choice.kind),
                ("review_key", choice.review_key),
                ("decision", choice.decision),
                ("subject", choice.subject),
            )
            if value
        )
        items.append(
            _approved_audit_from_candidate(
                candidate,
                detail=selection_exclusion_detail(reason_code),
                reason_code=reason_code,
                evidence=evidence,
                decision_source=choice.decision_source,
            )
        )
    return tuple(items)


def _approved_source_input_issue(issue) -> SourceInputIssue:
    values = tuple(
        str(_summary_value(issue, name, "") or "").strip()
        for name in (
            "source_id",
            "original_path",
            "reason",
            "recommendation",
        )
    )
    if any(not value for value in values):
        raise ValueError("source input issue is incomplete")
    return SourceInputIssue(*values)


def _approved_audit_from_rejection(rejection) -> ApprovedAuditItem:
    evidence = tuple(
        (name, _measurement_text(value))
        for name, value in (
            ("s1_max", rejection.s1_max),
            ("x_at_s1_max", rejection.x_at_s1_max),
            ("max_y", rejection.max_y),
            ("x_at_max_y", rejection.x_at_max_y),
        )
        if value is not None
    )
    return _approved_audit_from_candidate(
        rejection,
        detail=canonical_audit_detail(str(rejection.reason), evidence),
        reason_code=str(rejection.reason),
        evidence=evidence,
        decision_source="automatic",
    )


def _approved_audit_from_candidate(
    candidate,
    *,
    detail: str,
    reason_code: str,
    evidence: tuple[tuple[str, str], ...] = (),
    decision_source: str,
) -> ApprovedAuditItem:
    return ApprovedAuditItem(
        book_key=candidate.book_key,
        detail=detail,
        source_id=str(candidate.source_id),
        source_filename=str(candidate.source_filename),
        page_type=str(candidate.page_type),
        folder_path=str(candidate.folder_path),
        short_name=str(candidate.short_name),
        display_name=_visible_book_name(candidate),
        reason_code=reason_code,
        evidence=evidence,
        decision_source=decision_source,
    )


def _approved_attribution(candidate, attribution) -> ApprovedAttribution:
    sample = attribution.sample
    return ApprovedAttribution(
        book_key=candidate.book_key,
        canonical_sample_label=sample.canonical_label,
        sample_system_label=sample.system_label,
        temperature=sample.temperature,
        sample_system_identity=sample.system_identity_json(),
        source_id=str(candidate.source_id),
        source_filename=str(candidate.source_filename),
        page_type=str(candidate.page_type),
        folder_path=str(candidate.folder_path),
        short_name=str(candidate.short_name),
        display_name=_visible_book_name(candidate),
        payload_checksum=str(
            getattr(candidate, "payload_checksum", "") or ""
        ),
    )


def _approved_book_identity(candidate) -> ApprovedBookIdentity:
    return ApprovedBookIdentity(
        book_key=candidate.book_key,
        source_id=str(candidate.source_id),
        source_filename=str(candidate.source_filename),
        page_type=str(candidate.page_type),
        folder_path=str(candidate.folder_path),
        short_name=str(candidate.short_name),
        display_name=_visible_book_name(candidate),
        payload_checksum=str(
            getattr(candidate, "payload_checksum", "") or ""
        ),
        raw_display_name=str(
            getattr(candidate, "display_name", "") or ""
        ),
        spectrum_class=str(
            getattr(
                getattr(candidate, "spectrum_class", None),
                "value",
                "",
            )
            or ""
        ),
        selected_y_column=str(
            getattr(candidate, "selected_y_column", "") or ""
        ),
        paired_x_column=str(
            getattr(candidate, "paired_x_column", "") or ""
        ),
    )


def _approved_review_choices(
    task_cache,
) -> tuple[ApprovedReviewChoice, ...]:
    review_state = task_cache.get("task7_review_state")
    if isinstance(review_state, _Task7ReviewState):
        choices = list(_approved_review_choices_from_state(review_state))
    else:
        choices = []
        for index, group in enumerate(
            task_cache.get("special_groups", ()),
            start=1,
        ):
            choices.append(
                ApprovedReviewChoice(
                    "special_group",
                    f"{index}:{group.kind}",
                    tuple(group.book_keys),
                    tuple(group.book_keys),
                    "confirm_group",
                    str(group.kind),
                    (
                        "automatic"
                        if group.kind == "steady_2d"
                        else "manual"
                    ),
                )
            )
        for review_key, selected_book_key in sorted(
            task_cache.get("duplicate_choices", {}).items()
        ):
            choices.append(
                ApprovedReviewChoice(
                    "emission",
                    str(review_key),
                    (str(selected_book_key),),
                )
            )
        for review_key, selected_book_keys in sorted(
            task_cache.get("excitation_pairing", {}).items()
        ):
            choices.append(
                ApprovedReviewChoice(
                    "excitation",
                    str(review_key),
                    tuple(selected_book_keys),
                )
            )
    covered_special_groups = {
        choice.candidate_book_keys
        for choice in choices
        if choice.kind == "special_group"
    }
    for group in task_cache.get("special_groups", ()):
        book_keys = tuple(group.book_keys)
        if group.kind != "steady_2d" or book_keys in covered_special_groups:
            continue
        choices.append(
            ApprovedReviewChoice(
                kind="special_group",
                review_key=f"automatic:{book_keys!r}",
                selected_book_keys=book_keys,
                candidate_book_keys=book_keys,
                decision="confirm_group",
                subject="steady_2d",
                decision_source="automatic",
            )
        )
    return tuple(choices)


def _context_source_ids(context) -> tuple[str, ...]:
    return tuple(
        f"S{index:04d}"
        for index, _snapshot in enumerate(
            context.source_fingerprints_before,
            start=1,
        )
    )


def _approved_source_ids(
    context,
    recognized_books,
) -> tuple[str, ...]:
    context_source_ids = _context_source_ids(context)
    recognized_source_ids = {
        book.source_id
        for book in recognized_books
    }
    unknown_source_ids = (
        recognized_source_ids - set(context_source_ids)
    )
    if unknown_source_ids:
        raise ProductRunnerError(
            "recognized Book source is not present in "
            "pre-extraction context"
        )
    return tuple(
        source_id
        for source_id in context_source_ids
        if source_id in recognized_source_ids
    )


def _approved_review_requirements(
    task_cache,
) -> tuple[ApprovedReviewRequirement, ...]:
    review_state = task_cache.get("task7_review_state")
    requirements = []
    if isinstance(review_state, _Task7ReviewState):
        requirements.extend(
            ApprovedReviewRequirement(
                kind=decision.bucket,
                review_key=repr(decision.key),
                candidate_book_keys=decision.book_keys,
            )
            for decision in review_state.requirements
        )
    else:
        requirements.extend(
            ApprovedReviewRequirement(
                kind=choice.kind,
                review_key=choice.review_key,
                candidate_book_keys=choice.candidate_book_keys,
                decision_source=choice.decision_source,
            )
            for choice in _approved_review_choices(task_cache)
            if choice.decision_source == "manual"
        )
    covered_special_groups = {
        requirement.candidate_book_keys
        for requirement in requirements
        if requirement.kind == "special_group"
    }
    for group in task_cache.get("special_groups", ()):
        book_keys = tuple(group.book_keys)
        if group.kind != "steady_2d" or book_keys in covered_special_groups:
            continue
        requirements.append(
            ApprovedReviewRequirement(
                kind="special_group",
                review_key=f"automatic:{book_keys!r}",
                candidate_book_keys=book_keys,
                decision_source="automatic",
            )
        )
    return tuple(requirements)


def _approved_review_choices_from_state(
    review_state: _Task7ReviewState,
) -> tuple[ApprovedReviewChoice, ...]:
    choices = []
    for review_decision in review_state.completed_requirements():
        current_choices = review_state._choices_for(
            review_decision.bucket
        )
        if review_decision.key not in current_choices:
            raise ValueError(
                "completed review history has no current decision"
            )
        current = current_choices[review_decision.key]
        subject = review_decision.special_kind
        decision = ""
        if review_decision.bucket == "special_group":
            decision, selected = current
            subject = str(review_decision.key[0])
            if decision == "confirm_group":
                selected = review_decision.book_keys
        elif review_decision.bucket == "special_overlap":
            selected = (str(review_decision.key),)
            decision = _approved_overlap_decision(str(current))
        elif isinstance(current, str):
            selected = (current,)
        else:
            selected = tuple(current)
        choices.append(
            ApprovedReviewChoice(
                kind=review_decision.bucket,
                review_key=repr(review_decision.key),
                selected_book_keys=tuple(selected),
                candidate_book_keys=review_decision.book_keys,
                decision=decision,
                subject=subject,
            )
        )
    return tuple(choices)


def _approved_overlap_decision(value: str) -> str:
    return {
        "二维延迟谱": "delayed_2d",
        "时间分辨延迟谱": "delay_time_series",
        "regular": "regular",
        "delayed_2d": "delayed_2d",
        "delay_time_series": "delay_time_series",
    }.get(value, value)


def _review_decision_summary(
    review_choices: tuple[ApprovedReviewChoice, ...],
    candidate_by_key,
) -> tuple[str, ...]:
    return tuple(
        _review_decision_summary_line(choice, candidate_by_key)
        for choice in review_choices
    )


def _review_decision_summary_line(choice, candidate_by_key) -> str:
    selected = _review_book_summary_labels(
        choice.selected_book_keys,
        candidate_by_key,
    )
    candidates = _review_book_summary_labels(
        choice.candidate_book_keys or choice.selected_book_keys,
        candidate_by_key,
    )
    if choice.kind == "special_group":
        special_kind = (
            choice.subject
            or choice.review_key.split(":", 1)[-1]
        )
        label = _special_kind_label(special_kind)
        if choice.decision_source == "automatic":
            return f"{label}自动分类并排除普通输出：{candidates}"
        action = choice.decision or "confirm_group"
        if action == "reject_group":
            return f"{label}整组拒绝：{candidates}"
        if action == "confirm_selection":
            return f"{label}逐 Book 确认：保留 {selected}；组内 {candidates}"
        return f"{label}整组确认：{candidates}"
    if choice.kind == "special_duplicate":
        label = _special_kind_label(choice.subject)
        return f"{label}相关重复选择：保留 {selected}；候选 {candidates}"
    if choice.kind == "special_overlap":
        label = _special_kind_label(choice.subject)
        destination = _special_kind_label(choice.decision)
        return f"{label}重叠归属：{selected} → {destination}"
    if choice.kind == "emission":
        return f"重复发射谱选择：{selected}"
    return f"激发谱选择：{selected}"


def _review_book_summary_labels(book_keys, candidate_by_key) -> str:
    return "、".join(
        _review_book_summary_label(candidate_by_key.get(book_key))
        for book_key in book_keys
    )


def _final_review_rows(
    recognized_books,
    assignments,
    *,
    accepted_book_keys: tuple[str, ...],
    rejections: tuple[ApprovedAuditItem, ...],
    exclusions: tuple[ApprovedAuditItem, ...],
    review_requirements: tuple[ApprovedReviewRequirement, ...],
    source_order: tuple[str, ...] = (),
) -> tuple[FinalReviewRow, ...]:
    accepted = set(accepted_book_keys)
    rejection_by_key = {
        item.book_key: item
        for item in rejections
    }
    exclusion_by_key = {
        item.book_key: item
        for item in exclusions
    }
    source_positions = {
        source_id: index
        for index, source_id in enumerate(source_order)
    }
    indexed_books = tuple(enumerate(recognized_books))

    def row_order_key(item) -> tuple[int, int, int]:
        index, book = item
        page_order = getattr(book, "page_order", None)
        return (
            source_positions.get(
                book.source_id,
                len(source_positions),
            ),
            page_order if isinstance(page_order, int) else index,
            index,
        )

    rows = []
    for _index, book in sorted(indexed_books, key=row_order_key):
        attribution = assignments.get(book.book_key)
        is_rejected = book.book_key in rejection_by_key
        attribution_label = (
            attribution.sample.canonical_label
            if attribution is not None
            else "—"
        )
        if is_rejected:
            result = (
                "拒绝，不输出："
                f"{rejection_by_key[book.book_key].detail}"
            )
        elif book.book_key in exclusion_by_key:
            result = "不输出：" + exclusion_by_key[book.book_key].detail
        elif book.book_key in accepted:
            result = "将写入输出计划"
        else:
            raise ValueError(
                f"Book has no final disposition: {book.book_key}"
            )
        rows.append(
            FinalReviewRow(
                row_id=book.book_key,
                source_filename=str(book.source_filename),
                folder_path=str(book.folder_path or "/"),
                book_name=_visible_book_name(book),
                attribution=attribution_label,
                result=result,
                has_related_conflicts=(
                    not is_rejected
                    and any(
                        requirement.decision_source == "manual"
                        and book.book_key in requirement.candidate_book_keys
                        for requirement in review_requirements
                    )
                ),
                can_modify_attribution=(
                    not is_rejected and attribution is not None
                ),
            )
        )
    return tuple(rows)


def _final_review_output_folders(
    output_plan,
) -> tuple[FinalReviewOutputFolder, ...]:
    missing_by_folder = {
        item.folder_name: tuple(item.missing_labels)
        for item in output_plan.incomplete_folders
    }
    return tuple(
        FinalReviewOutputFolder(
            folder_name=folder.name,
            books=tuple(
                FinalReviewOutputBook(
                    book_name=book.display_name,
                    column_order=tuple(
                        _final_review_column_text(index, column)
                        for index, column in enumerate(
                            book.columns,
                            start=1,
                        )
                    ),
                )
                for book in folder.books
            ),
            missing_items=missing_by_folder.get(folder.name, ()),
        )
        for folder in output_plan.folders
    )


def _final_review_column_text(index: int, column) -> str:
    kind = {
        "x": "X",
        "raw_y": "原始 Y",
        "norm_y": "归一化 Y",
    }.get(column.kind, column.kind)
    details = [f"Comment={column.comment}"]
    if column.method:
        details.append(f"Method={column.method}")
    if column.formula:
        details.append(f"F(x)={column.formula}")
    return f"列 {index} [{kind}] · " + " · ".join(details)


def _approved_snapshot_review_rows(
    approved_snapshot,
) -> tuple[tuple[str, str, str], ...]:
    identity_by_key = {
        item.book_key: item
        for item in approved_snapshot.recognized_books
    }
    attribution_by_key = {
        item.book_key: item
        for item in approved_snapshot.attributions
    }
    rows = []
    for spectrum in approved_snapshot.accepted_spectra:
        identity = identity_by_key[spectrum.spectrum_id]
        attribution = attribution_by_key[spectrum.spectrum_id]
        rows.append(
            (
                identity.source_filename,
                _approved_identity_location(identity),
                f"将输出：{attribution.canonical_sample_label}",
            )
        )
    rows.extend(
        (
            item.source_filename,
            _approved_identity_location(item),
            f"拒绝，不输出：{item.detail}",
        )
        for item in approved_snapshot.rejections
    )
    rows.extend(
        (
            item.source_filename,
            _approved_identity_location(item),
            f"不输出：{item.detail}",
        )
        for item in approved_snapshot.exclusions
    )
    return tuple(rows)


def _approved_identity_location(item) -> str:
    folder = str(item.folder_path).strip("/")
    if folder:
        return f"{folder} / {item.display_name}"
    return f"Root / {item.display_name}"


def _review_book_summary_label(candidate) -> str:
    if candidate is None:
        return "已确认 Book"
    folder = candidate.folder_path.strip("/")
    location = (
        f"{folder} / {_visible_book_name(candidate)}"
        if folder
        else f"Root / {_visible_book_name(candidate)}"
    )
    return f"{candidate.source_filename} · {location}"


def _special_book_from_candidate(candidate, attribution) -> SpectrumBook:
    return SpectrumBook(
        source_id=candidate.source_id,
        folder_path=candidate.folder_path,
        book_name=candidate.short_name,
        spectrum_class=candidate.spectrum_class,
        sample_label=attribution.sample.identity_json(),
        page_type=candidate.page_type,
        fixed_excitation_wavelength=(
            candidate.fixed_wavelength
            if candidate.spectrum_class == SpectrumClass.DELAYED_EMISSION
            else None
        ),
        receiving_range=candidate.wavelength_range,
        excitation_slit=format_raw_slit_fields(candidate.excitation_slits),
        emission_slit=format_raw_slit_fields(candidate.emission_slits),
        flash_delay=candidate.flash_delay,
        sample_window=candidate.sample_window,
        time_per_flash=candidate.time_per_flash,
        flash_count=candidate.flash_count,
    )


def _final_conflict_group_id(decision: _Task7ReviewDecision) -> str:
    return json.dumps(
        (
            decision.bucket,
            repr(decision.key),
            decision.book_keys,
        ),
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _final_conflict_choices(
    decision: _Task7ReviewDecision,
    candidate_by_key,
) -> tuple[
    tuple[FinalReviewConflictChoice, ...],
    tuple[tuple[str, str], ...],
]:
    if decision.bucket == "special_overlap":
        candidate = candidate_by_key[decision.key]
        candidate_fields = _conflict_choice(candidate).fields
        raw_choices = []
        for choice in OVERLAP_CHOICES:
            display_name = (
                "常规延迟谱"
                if choice == "regular"
                else _special_kind_review_label(choice)
            )
            raw_choices.append(
                ConflictReviewChoice(
                    book_key=choice,
                    display_name=display_name,
                    fields=(*candidate_fields, ("归类", display_name)),
                )
            )
        raw_choices = tuple(raw_choices)
    else:
        raw_choices = _conflict_choices(
            decision.book_keys,
            candidate_by_key,
        )
    common_fields, varying_fields = partition_conflict_choices(raw_choices)
    choices = tuple(
        FinalReviewConflictChoice(
            choice.book_key,
            choice.display_name,
            _format_conflict_difference_text(
                varying_fields[choice.book_key],
                empty_message="仅 Book 名不同",
            ),
        )
        for choice in raw_choices
    )
    return choices, common_fields


def _final_stale_conflict_choices(
    decision: _Task7ReviewDecision,
    selected_keys: tuple[str, ...],
    candidate_by_key,
) -> tuple[FinalReviewConflictChoice, ...]:
    reference_choices, _common_fields = _final_conflict_choices(
        decision,
        candidate_by_key,
    )
    reference_by_key = {
        choice.choice_key: choice
        for choice in reference_choices
    }
    snapshots = []
    for choice_key in selected_keys:
        reference = reference_by_key.get(choice_key)
        candidate = candidate_by_key.get(choice_key)
        if candidate is None and decision.bucket == "special_overlap":
            candidate = candidate_by_key.get(decision.key)
        display_name = (
            reference.display_name
            if reference is not None
            else (
                _visible_book_name(candidate)
                if candidate is not None
                else "原选择详情不可用"
            )
        )
        detail = (
            _format_conflict_difference_text(
                _conflict_choice(candidate).fields,
                empty_message="原选择条件不可用",
            )
            if candidate is not None
            else (
                reference.detail
                if reference is not None
                else "原选择条件不可用"
            )
        )
        snapshots.append(
            FinalReviewConflictChoice(
                choice_key,
                display_name,
                detail,
            )
        )
    return tuple(snapshots)


def _final_conflict_selection_value(
    bucket: str,
    current,
) -> tuple[tuple[str, ...], str]:
    if bucket == "special_group":
        decision, selected = current
        return tuple(selected), str(decision)
    if isinstance(current, str):
        return (current,), ""
    return tuple(current), ""


def _final_conflict_selection_mode(bucket: str) -> str:
    if bucket == "special_group":
        return "special_group"
    if bucket == "excitation":
        return "multi"
    return "single"


def _final_conflict_title(decision: _Task7ReviewDecision) -> str:
    if decision.bucket == "special_duplicate":
        return f"{_special_kind_label(decision.special_kind)}重复 Book 冲突"
    if decision.bucket == "special_overlap":
        return "特殊谱归类冲突"
    if decision.bucket == "special_group":
        return f"{_special_kind_label(str(decision.key[0]))}组确认"
    if decision.bucket == "emission":
        return "重复发射谱"
    return "激发谱候选"


def _final_conflict_context(
    decision: _Task7ReviewDecision,
    candidate_by_key,
) -> str:
    lines = []
    for book_key in decision.book_keys:
        candidate = candidate_by_key.get(book_key)
        if candidate is None:
            lines.append(book_key)
            continue
        lines.append(
            " / ".join(
                (
                    str(candidate.source_filename),
                    str(candidate.folder_path or "/"),
                    _visible_book_name(candidate),
                )
            )
        )
    return "\n".join(lines)


def _valid_final_conflict_selection(
    bucket: str,
    selected_keys: tuple[str, ...],
    decision: str,
    choices: tuple[FinalReviewConflictChoice, ...],
    single_select_groups: tuple[tuple[str, ...], ...],
) -> bool:
    valid_keys = {
        choice.choice_key
        for choice in choices
    }
    if (
        len(selected_keys) != len(set(selected_keys))
        or any(key not in valid_keys for key in selected_keys)
    ):
        return False
    if bucket == "special_group":
        if decision in {"confirm_group", "reject_group"}:
            return not selected_keys
        return decision == "confirm_selection" and bool(selected_keys)
    if decision:
        return False
    if bucket == "excitation":
        return bool(selected_keys) and all(
            sum(key in selected_keys for key in group) <= 1
            for group in single_select_groups
        )
    return len(selected_keys) == 1


def _project_final_conflicts(
    candidates,
    assignments,
    review_state: _Task7ReviewState,
    *,
    row_id: str,
    target_book_keys: tuple[str, ...],
    selections: tuple[FinalReviewConflictSelection, ...],
) -> _Task7ConflictProjection:
    candidate_by_key = {
        candidate.book_key: candidate
        for candidate in candidates
    }
    target_keys = set(target_book_keys)
    if not target_keys or any(
        book_key not in candidate_by_key
        for book_key in target_keys
    ):
        raise RuntimeError(
            "Selected final-review conflict target is unavailable"
        )
    overrides = {}
    for selection in selections:
        if selection.group_id in overrides:
            raise ValueError("Duplicate final conflict selection")
        overrides[selection.group_id] = selection
    previous_requirements = review_state.completed_requirements()
    previous_by_identity = {
        (decision.bucket, decision.key): decision
        for decision in previous_requirements
    }
    matched_previous: set[tuple[str, object]] = set()
    requirements = []
    editor_groups = []
    duplicate_choices: dict[str, str] = {}
    overlap_choices: dict[str, str] = {}
    special_group_choices = {}
    emission_choices: dict[str, str] = {}
    excitation_choices: dict[str, tuple[str, ...]] = {}
    complete = True

    spectra = tuple(
        _selection_spectrum_from_candidate(
            candidate,
            assignments[candidate.book_key],
        )
        for candidate in candidates
    )
    special_books = tuple(
        _special_book_from_candidate(
            candidate,
            assignments[candidate.book_key],
        )
        for candidate in candidates
    )

    def previous_for(
        decision: _Task7ReviewDecision,
    ) -> _Task7ReviewDecision | None:
        identity = (decision.bucket, decision.key)
        exact = previous_by_identity.get(identity)
        if exact is not None:
            matched_previous.add(identity)
            return exact
        candidates_for_match = []
        for index, previous in enumerate(previous_requirements):
            previous_identity = (previous.bucket, previous.key)
            if (
                previous_identity in matched_previous
                or previous.bucket != decision.bucket
            ):
                continue
            overlap = len(
                set(previous.book_keys).intersection(decision.book_keys)
            )
            if not overlap:
                continue
            if (
                decision.bucket == "special_group"
                and str(previous.key[0]) != str(decision.key[0])
            ):
                continue
            candidates_for_match.append((-overlap, index, previous))
        if not candidates_for_match:
            return None
        previous = min(candidates_for_match)[2]
        matched_previous.add((previous.bucket, previous.key))
        return previous

    def resolve(
        decision: _Task7ReviewDecision,
        *,
        single_select_groups: tuple[tuple[str, ...], ...] = (),
    ) -> tuple[bool, tuple[str, ...], str]:
        nonlocal complete
        requirements.append(decision)
        previous = previous_for(decision)
        group_id = (
            _final_conflict_group_id(previous)
            if previous is not None
            else _final_conflict_group_id(decision)
        )
        current = None
        if previous is not None:
            current_choices = review_state._choices_for(previous.bucket)
            current = current_choices.get(previous.key)
        selected_keys: tuple[str, ...] = ()
        selected_decision = ""
        if current is not None:
            selected_keys, selected_decision = (
                _final_conflict_selection_value(
                    decision.bucket,
                    current,
                )
            )
        override = overrides.get(group_id)
        if override is None:
            override = overrides.get(_final_conflict_group_id(decision))
        if override is not None:
            selected_keys = override.selected_keys
            selected_decision = override.decision
        choices, common_fields = _final_conflict_choices(
            decision,
            candidate_by_key,
        )
        valid = _valid_final_conflict_selection(
            decision.bucket,
            selected_keys,
            selected_decision,
            choices,
            single_select_groups,
        )
        complete = complete and valid
        if (
            target_keys.intersection(decision.book_keys)
            or override is not None
            or not valid
        ):
            editor_groups.append(
                FinalReviewConflictGroup(
                    group_id=group_id,
                    title=_final_conflict_title(decision),
                    context=_final_conflict_context(
                        decision,
                        candidate_by_key,
                    ),
                    selection_mode=_final_conflict_selection_mode(
                        decision.bucket
                    ),
                    choices=choices,
                    common_fields=common_fields,
                    selected_keys=selected_keys if valid else (),
                    decision=selected_decision if valid else "",
                    stale_selected_keys=(
                        selected_keys if not valid else ()
                    ),
                    stale_choices=(
                        _final_stale_conflict_choices(
                            previous or decision,
                            selected_keys,
                            candidate_by_key,
                        )
                        if not valid
                        else ()
                    ),
                    stale_decision=(
                        selected_decision if not valid else ""
                    ),
                    single_select_groups=single_select_groups,
                    warning=(
                        "上游选择已改变，请重新确认本组"
                        if not valid and (selected_keys or selected_decision)
                        else ("待选择" if not valid else "")
                    ),
                )
            )
        return valid, selected_keys, selected_decision

    while True:
        special_result = classify_special_groups(
            list(special_books),
            duplicate_choices=duplicate_choices,
            overlap_choices=overlap_choices,
        )
        if special_result.pending_duplicate_reviews:
            context_book_keys = (
                special_result.pending_duplicate_reviews[0]
                .context_book_keys
            )
            pending_batch = tuple(
                pending
                for pending in special_result.pending_duplicate_reviews
                if pending.context_book_keys == context_book_keys
            )
            batch_valid = True
            for pending in pending_batch:
                decision = _Task7ReviewDecision(
                    "special_duplicate",
                    pending.choice_key,
                    pending.book_keys,
                    test_point_label=pending.point_label,
                    context_book_keys=pending.context_book_keys,
                    special_kind=pending.kind,
                )
                valid, selected_keys, _selected_decision = resolve(
                    decision
                )
                batch_valid = batch_valid and valid
                if valid:
                    duplicate_choices[pending.choice_key] = (
                        selected_keys[0]
                    )
            if not batch_valid:
                break
            continue
        if special_result.pending_overlap_assignments:
            context_book_keys = (
                special_result.pending_overlap_assignments[0]
                .context_book_keys
            )
            pending_batch = tuple(
                pending
                for pending in special_result.pending_overlap_assignments
                if pending.context_book_keys == context_book_keys
            )
            batch_valid = True
            for pending in pending_batch:
                decision = _Task7ReviewDecision(
                    "special_overlap",
                    pending.book_key,
                    (pending.book_key,),
                    context_book_keys=pending.context_book_keys,
                )
                valid, selected_keys, _selected_decision = resolve(
                    decision
                )
                batch_valid = batch_valid and valid
                if valid:
                    overlap_choices[pending.book_key] = selected_keys[0]
            if not batch_valid:
                break
            continue
        break

    accepted_special_groups = []
    regular_delayed_keys = []
    if complete:
        regular_delayed_keys.extend(
            special_result.regular_delayed_book_keys
        )
        group_stage_valid = True
        for group in special_result.groups:
            if group.kind == "steady_2d":
                accepted_special_groups.append(group)
                continue
            group_key = (group.kind, group.book_keys)
            decision = _Task7ReviewDecision(
                "special_group",
                group_key,
                group.book_keys,
                special_kind=group.kind,
            )
            valid, selected_keys, selected_decision = resolve(decision)
            group_stage_valid = group_stage_valid and valid
            if not valid:
                continue
            special_group_choices[group_key] = (
                selected_decision,
                selected_keys,
            )
            if selected_decision == "confirm_group":
                accepted_special_groups.append(group)
            elif selected_decision == "reject_group":
                regular_delayed_keys.extend(
                    _delayed_emission_keys(
                        group.book_keys,
                        candidate_by_key,
                    )
                )
            else:
                accepted, ordinary_keys = resolve_special_group_selection(
                    group,
                    selected_keys,
                )
                if accepted is not None:
                    accepted_special_groups.append(accepted)
                regular_delayed_keys.extend(
                    _delayed_emission_keys(
                        ordinary_keys,
                        candidate_by_key,
                    )
                )
        complete = complete and group_stage_valid

    if complete:
        special_keys = tuple(
            book_key
            for group in accepted_special_groups
            for book_key in group.book_keys
        )
        copyable_emissions = filter_copyable_emissions_after_special(
            list(spectra),
            regular_delayed_book_keys=tuple(
                dict.fromkeys(regular_delayed_keys)
            ),
            special_group_book_keys=special_keys,
        )
        while True:
            emission_result = review_emission_duplicates(
                list(copyable_emissions),
                choices=emission_choices,
            )
            if not emission_result.pending_reviews:
                break
            pending = emission_result.pending_reviews[0]
            decision = _Task7ReviewDecision(
                "emission",
                pending.review_key,
                pending.book_keys,
            )
            valid, selected_keys, _selected_decision = resolve(decision)
            if not valid:
                break
            emission_choices[pending.review_key] = selected_keys[0]

    if complete:
        while True:
            excitation_result = select_excitation_candidates(
                list(spectra),
                choices=excitation_choices,
            )
            if not excitation_result.pending_reviews:
                break
            pending = excitation_result.pending_reviews[0]
            decision = _Task7ReviewDecision(
                "excitation",
                pending.review_key,
                pending.book_keys,
            )
            valid, selected_keys, _selected_decision = resolve(
                decision,
                single_select_groups=pending.single_select_groups,
            )
            if not valid:
                break
            excitation_choices[pending.review_key] = selected_keys

    editor = FinalReviewConflictEditor(
        row_id=row_id,
        groups=tuple(editor_groups),
        can_confirm=complete and bool(editor_groups),
    )
    return _Task7ConflictProjection(
        editor=editor,
        requirements=tuple(requirements),
        special_duplicate_choices=duplicate_choices,
        special_overlap_choices=overlap_choices,
        special_group_choices=special_group_choices,
        emission_choices=emission_choices,
        excitation_choices=excitation_choices,
        complete=complete,
    )


def _apply_conflict_projection_to_state(
    review_state: _Task7ReviewState,
    projection: _Task7ConflictProjection,
) -> None:
    if not projection.complete:
        raise ValueError("Final conflict correction is incomplete")
    review_state.special_duplicate_choices = dict(
        projection.special_duplicate_choices
    )
    review_state.special_overlap_choices = dict(
        projection.special_overlap_choices
    )
    review_state.special_group_choices = dict(
        projection.special_group_choices
    )
    review_state.emission_choices = dict(projection.emission_choices)
    review_state.excitation_choices = dict(
        projection.excitation_choices
    )
    review_state.requirements = list(projection.requirements)
    review_state.history = list(projection.requirements)
    review_state.recalled.clear()
    review_state.related_conflict_batch = None
    review_state.related_conflict_drafts.clear()
    review_state.confirmed_related_conflict_batches.clear()


def _canonicalize_task7_review_state(
    candidates,
    assignments,
    review_state: _Task7ReviewState,
) -> None:
    candidate_keys = tuple(
        candidate.book_key
        for candidate in candidates
    )
    if not candidate_keys:
        return
    projection = _project_final_conflicts(
        candidates,
        assignments,
        review_state,
        row_id=candidate_keys[0],
        target_book_keys=candidate_keys,
        selections=(),
    )
    if not projection.complete:
        raise RuntimeError("Completed conflict review is inconsistent")
    _apply_conflict_projection_to_state(review_state, projection)


def _related_conflict_id(decision: _Task7ReviewDecision) -> str:
    return json.dumps(
        [
            decision.bucket,
            decision.test_point_label,
            decision.book_keys,
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _related_conflict_draft_key(
    conflicts: tuple[_Task7ReviewDecision, ...],
) -> tuple[str, ...]:
    return tuple(_related_conflict_id(decision) for decision in conflicts)


def _related_conflict_subject(
    conflicts: tuple[_Task7ReviewDecision, ...],
) -> str:
    buckets = {decision.bucket for decision in conflicts}
    kind_labels = tuple(
        dict.fromkeys(
            _special_kind_label(decision.special_kind)
            for decision in conflicts
            if decision.special_kind
        )
    )
    if buckets == {"special_duplicate"} and kind_labels:
        return f"{'、'.join(kind_labels)}重复 Book 冲突"
    if buckets == {"special_overlap"}:
        return "特殊谱归类冲突"
    if kind_labels:
        return f"{'、'.join(kind_labels)}选择冲突"
    return "特殊谱选择冲突"


def _decision_context_book_keys(
    decision: _Task7ReviewDecision,
) -> tuple[str, ...]:
    return decision.context_book_keys or decision.book_keys


def _initial_grouped_selection(
    current: str | None,
    recalled: tuple[str, ...],
    book_keys: tuple[str, ...],
) -> str:
    if current:
        return current
    if recalled:
        return recalled[0]
    return book_keys[0]


def _ordered_related_conflicts(
    review_state: _Task7ReviewState,
    group_book_keys: tuple[str, ...],
) -> tuple[_Task7ReviewDecision, ...]:
    conflicts = review_state.related_special_conflicts(group_book_keys)
    group_order = {
        book_key: index
        for index, book_key in enumerate(group_book_keys)
    }

    def order_key(indexed):
        original_index, decision = indexed
        if decision.bucket == "special_duplicate":
            retained_key = review_state.special_duplicate_choices.get(
                decision.key
            )
        else:
            retained_key = decision.key
        return (
            group_order.get(retained_key, len(group_order)),
            original_index,
        )

    return tuple(
        decision
        for _index, decision in sorted(
            enumerate(conflicts),
            key=order_key,
        )
    )


def _related_conflict_groups(
    batch: _RelatedConflictBatch,
    candidate_by_key,
) -> tuple[ConflictReviewGroup, ...]:
    selected_by_group = dict(batch.selections)
    groups = []
    for decision in batch.conflicts:
        group_key = _related_conflict_id(decision)
        if decision.bucket == "special_duplicate":
            choices = _conflict_choices(
                decision.book_keys,
                candidate_by_key,
            )
        else:
            candidate_fields = _conflict_choice(
                candidate_by_key[decision.key]
            ).fields
            choices = tuple(
                ConflictReviewChoice(
                    book_key=choice,
                    display_name=(
                        "常规延迟谱"
                        if choice == "regular"
                        else _special_kind_review_label(choice)
                    ),
                    fields=candidate_fields,
                )
                for choice in OVERLAP_CHOICES
            )
        common_fields, varying_fields = partition_conflict_choices(choices)
        groups.append(
            ConflictReviewGroup(
                group_key=group_key,
                choices=tuple(
                    ConflictReviewChoice(
                        book_key=choice.book_key,
                        display_name=choice.display_name,
                        fields=varying_fields[choice.book_key],
                    )
                    for choice in choices
                ),
                initial_selection=selected_by_group[group_key],
                common_fields=common_fields,
            )
        )
    return tuple(groups)


def _validated_group_selections(
    request: ConflictReviewRequest,
    response,
) -> tuple[tuple[str, str], ...]:
    selections = response.group_selections
    if (
        len(selections) != len(request.choice_groups)
        or len({group_key for group_key, _choice in selections})
        != len(selections)
    ):
        raise ValueError("Grouped conflict-review selection is invalid")
    selected_by_group = dict(selections)
    for group in request.choice_groups:
        valid_keys = {choice.book_key for choice in group.choices}
        if selected_by_group.get(group.group_key) not in valid_keys:
            raise ValueError("Grouped conflict-review selection is invalid")
    return tuple(
        (group.group_key, selected_by_group[group.group_key])
        for group in request.choice_groups
    )


def _apply_related_conflict_selections(
    review_state: _Task7ReviewState,
    batch: _RelatedConflictBatch,
    selected_by_group: dict[str, str],
    special_book_by_key: dict[str, SpectrumBook],
) -> None:
    updated_conflicts = []
    duplicate_selections = {
        decision: selected_by_group[_related_conflict_id(decision)]
        for decision in batch.conflicts
        if decision.bucket == "special_duplicate"
    }
    for decision, selected in duplicate_selections.items():
        review_state.special_duplicate_choices[decision.key] = selected

    for decision in batch.conflicts:
        if decision.bucket != "special_overlap":
            updated_conflicts.append(decision)
            continue
        selected_book_key = decision.key
        for duplicate_decision, retained_key in duplicate_selections.items():
            if decision.key not in duplicate_decision.book_keys:
                continue
            retained_identity = spectrum_book_point_identity(
                special_book_by_key[retained_key]
            )
            if retained_identity == decision.physical_point_identity:
                selected_book_key = retained_key
                break
        selected_group = selected_by_group[_related_conflict_id(decision)]
        review_state.special_overlap_choices.pop(decision.key, None)
        review_state.special_overlap_choices[selected_book_key] = selected_group
        if selected_book_key == decision.key:
            updated_conflicts.append(decision)
            continue
        replacement = _Task7ReviewDecision(
            bucket=decision.bucket,
            key=selected_book_key,
            book_keys=(selected_book_key,),
            test_point_label=decision.test_point_label,
            physical_point_identity=decision.physical_point_identity,
            context_book_keys=decision.context_book_keys,
            special_kind=decision.special_kind,
        )
        updated_conflicts.append(replacement)
    review_state.synchronize_related_conflict_editor(
        tuple(updated_conflicts)
    )


def _conflict_choices(book_keys, candidate_by_key) -> tuple[ConflictReviewChoice, ...]:
    return tuple(
        _conflict_choice(candidate_by_key[book_key])
        for book_key in book_keys
    )


def _delayed_emission_keys(book_keys, candidate_by_key) -> tuple[str, ...]:
    return tuple(
        book_key
        for book_key in book_keys
        if candidate_by_key[book_key].spectrum_class
        == SpectrumClass.DELAYED_EMISSION
    )


def _pending_review_book_keys(reviews) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            book_key
            for review in reviews
            for book_key in review.book_keys
        )
    )


def _default_excitation_selection(review) -> tuple[str, ...]:
    exact_group_keys = {
        book_key
        for group in review.single_select_groups
        for book_key in group
    }
    selected = {group[0] for group in review.single_select_groups}
    selected.update(
        book_key
        for book_key in review.book_keys
        if book_key not in exact_group_keys
    )
    return tuple(book_key for book_key in review.book_keys if book_key in selected)


def _conflict_choice(candidate) -> ConflictReviewChoice:
    wavelength_label = (
        "固定发射波长"
        if candidate.spectrum_class
        in {SpectrumClass.STEADY_EXCITATION, SpectrumClass.DELAYED_EXCITATION}
        else "固定激发波长"
    )
    fields = [
        ("来源文件", candidate.source_filename),
        ("Folder", candidate.folder_path or "Root"),
        ("谱图类型", _spectrum_class_label(candidate.spectrum_class)),
    ]
    if candidate.spectrum_class == SpectrumClass.STEADY_2D:
        fields.extend(
            (
                (
                    "激发扫描范围",
                    _range_text(candidate.excitation_range, "nm"),
                ),
                (
                    "激发扫描步长",
                    _conflict_measurement_text(
                        candidate.excitation_increment,
                        "nm",
                    ),
                ),
                (
                    "发射扫描范围",
                    _range_text(candidate.emission_range, "nm"),
                ),
                (
                    "发射扫描步长",
                    _conflict_measurement_text(
                        candidate.emission_increment,
                        "nm",
                    ),
                ),
            )
        )
    else:
        fields.extend(
            (
                (
                    wavelength_label,
                    _conflict_measurement_text(
                        candidate.fixed_wavelength,
                        "nm",
                    ),
                ),
                (
                    "扫描范围",
                    _range_text(candidate.wavelength_range, "nm"),
                ),
                (
                    "扫描步长",
                    _conflict_measurement_text(
                        candidate.scan_increment,
                        "nm",
                    ),
                ),
            )
        )
    fields.append(("狭缝", _slit_summary(candidate)))
    fields.extend(
        (label, _conflict_measurement_text(value, "ms"))
        for label, value in (
            ("延迟时间", candidate.flash_delay),
            ("采样窗口", candidate.sample_window),
            ("单次闪光周期", candidate.time_per_flash),
        )
        if value not in {None, ""}
    )
    if candidate.flash_count not in {None, ""}:
        fields.append(("闪光次数", str(candidate.flash_count)))
    fields.extend(
        (
            ("峰值", _peak_summary(candidate)),
            ("Note 时间", candidate.note_datetime or ""),
        )
    )
    return ConflictReviewChoice(
        book_key=candidate.book_key,
        display_name=_visible_book_name(candidate),
        fields=tuple(fields),
    )


def _range_text(value, unit: str) -> str:
    return _conflict_measurement_text(
        " – ".join(part for part in (value or ()) if part),
        unit,
    )


def _conflict_measurement_text(value, unit: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    head, marker, suffix = text.partition("（")
    measured = f"{head.rstrip()} {unit}"
    return f"{measured}（{suffix}" if marker else measured


def _special_kind_label(kind: str) -> str:
    return {
        "steady_2d": "二维稳态谱",
        "delayed_2d": "二维延迟谱",
        "delay_time_series": "时间分辨延迟谱",
    }.get(kind, kind)


def _special_kind_review_label(kind: str) -> str:
    label = _special_kind_label(kind)
    if label == "时间分辨延迟谱":
        return f"{label}（变化轴：延迟时间—单次闪光周期）"
    return label


def _spectrum_class_label(spectrum_class: SpectrumClass) -> str:
    return {
        SpectrumClass.STEADY_EMISSION: "稳态发射谱",
        SpectrumClass.STEADY_EXCITATION: "稳态激发谱",
        SpectrumClass.STEADY_2D: "二维稳态谱",
        SpectrumClass.DELAYED_EMISSION: "延迟发射谱",
        SpectrumClass.DELAYED_EXCITATION: "延迟激发谱",
        SpectrumClass.DELAYED_2D: "二维延迟谱",
        SpectrumClass.DELAY_TIME_SERIES: "时间分辨延迟谱",
    }[spectrum_class]


def _slit_summary(candidate) -> str:
    excitation = _conflict_measurement_text(
        format_raw_slit_fields(candidate.excitation_slits).replace(
            "/",
            " / ",
        ),
        "nm",
    )
    emission = _conflict_measurement_text(
        format_raw_slit_fields(candidate.emission_slits).replace(
            "/",
            " / ",
        ),
        "nm",
    )
    if not excitation and not emission:
        return ""
    return f"Ex {excitation or '—'} / Em {emission or '—'}"


def _peak_summary(candidate) -> str:
    maximum = _peak_y_text(candidate.max_y)
    x_at_maximum = _conflict_measurement_text(
        format_maximum_x(candidate.x_at_max_y),
        "nm",
    )
    if not maximum and not x_at_maximum:
        return ""
    return f"X={x_at_maximum or '—'}，Y={maximum or '—'}"


def _peak_y_text(value) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return str(value)
    if number == 0:
        return "0"
    if abs(number) < 1:
        return format(number, ".4g")
    if number.is_integer():
        return f"{int(number):,}"
    return f"{number:,.2f}".rstrip("0").rstrip(".")


def _summary_value(summary: object, key: str, default: object = None) -> object:
    if isinstance(summary, dict):
        return summary.get(key, default)
    return getattr(summary, key, default)


def _format_elapsed_seconds(value: float) -> str:
    elapsed_seconds = max(0, int(value))
    hours, remainder = divmod(elapsed_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def _display_source_name(path: object, all_paths: tuple[object, ...] = ()) -> str:
    if all_paths:
        label = _display_source_labels(all_paths).get(_source_progress_key(path))
        if label is not None:
            return label
    return str(path).replace("\\", "/").rsplit("/", 1)[-1]


def _display_source_labels(paths: tuple[object, ...]) -> dict[str, str]:
    return {
        _source_progress_key(path): label
        for path, label in zip(
            paths,
            disambiguated_source_labels(paths),
            strict=True,
        )
    }


def _source_progress_key(path: object) -> str:
    return os.path.normcase(os.path.abspath(os.path.normpath(str(path))))


def _summary_review_rows(summary: object) -> tuple[tuple[str, str, str], ...]:
    source_summaries = _summary_value(summary, "source_summaries", ()) or ()
    issues = _summary_value(summary, "source_input_issues", ()) or ()
    original_paths = tuple(
        _summary_value(source, "original_path", "")
        for source in (*tuple(source_summaries), *tuple(issues))
    )
    rows: list[tuple[str, str, str]] = []
    for index, source in enumerate(source_summaries, start=1):
        original_path = _summary_value(source, "original_path", "")
        source_name = _display_source_name(original_path, original_paths) or f"来源文件 {index}"
        extracted = _summary_value(source, "extracted_count", 0)
        rejected = _summary_value(source, "rejected_count", 0)
        inventory = _summary_value(source, "inventory_count", 0)
        rows.append((source_name, str(inventory), f"已提取 {extracted}，排除 {rejected}"))
    rows.extend(_input_issue_review_rows(issues, original_paths=original_paths))
    return tuple(rows)


def _active_source_input_issues(summary: object, active_issues) -> tuple[object, ...]:
    summary_issues = _summary_value(summary, "source_input_issues", ()) or ()
    return tuple(summary_issues or active_issues or ())


def _input_issue_review_rows(
    issues,
    *,
    original_paths: tuple[object, ...] | None = None,
) -> tuple[tuple[str, str, str], ...]:
    issues = tuple(issues or ())
    if original_paths is None:
        original_paths = tuple(
            _summary_value(issue, "original_path", "")
            for issue in issues
        )
    rows = []
    for index, issue in enumerate(issues, start=1):
        original_path = _summary_value(issue, "original_path", "")
        source_name = (
            _display_source_name(original_path, original_paths)
            or f"来源文件 {index}"
        )
        reason = str(_summary_value(issue, "reason", "该文件未进入后续流程。"))
        rows.append(
            (
                source_name,
                "—",
                f"已跳过：{reason}",
            )
        )
    return tuple(rows)


def _input_issue_message(issues) -> str:
    issues = tuple(issues or ())
    original_paths = tuple(
        _summary_value(issue, "original_path", "")
        for issue in issues
    )
    blocks = [f"<b>输入文件问题（{len(issues)}）</b>"]
    for index, issue in enumerate(issues, start=1):
        original_path = _summary_value(issue, "original_path", "")
        source_name = (
            _display_source_name(original_path, original_paths)
            or f"来源文件 {index}"
        )
        reason = str(_summary_value(issue, "reason", "该文件未进入后续流程。"))
        recommendation = str(
            _summary_value(
                issue,
                "recommendation",
                "请检查文件内容后重新选择。",
            )
        )
        blocks.append(
            "<br><br>"
            f"<b>{html.escape(source_name)}</b><br>"
            f"{html.escape(reason)}<br>"
            "<b>处理建议</b><br>"
            f"{html.escape(recommendation)}"
        )
    return "".join(blocks)


def _load_candidate_conversion(
    extraction_summary,
    *,
    cancel_check=None,
    settings_snapshot=None,
):
    snapshot_path = _summary_value(extraction_summary, "snapshot_path")
    if not snapshot_path:
        raise CandidateConversionError("提取结果缺少任务快照路径")
    snapshot_sha256 = _summary_value(extraction_summary, "snapshot_sha256")
    if not isinstance(snapshot_sha256, str) or len(snapshot_sha256) != 64:
        raise CandidateConversionError("提取结果缺少已批准的任务快照校验值")
    source_summaries = tuple(_summary_value(extraction_summary, "source_summaries", ()) or ())
    original_paths = tuple(_summary_value(source, "original_path", "") for source in source_summaries)
    source_filenames = {
        str(_summary_value(source, "source_id", "")): _display_source_name(
            _summary_value(source, "original_path", ""),
            original_paths,
        )
        for source in source_summaries
    }
    expected_source_ids = tuple(source_filenames)
    read_kwargs = {}
    if settings_snapshot is not None:
        approved_settings = dict(settings_snapshot)
        read_kwargs = {
            "s1_limit": _confirmed_s1_limit(approved_settings),
            "steady_emission_y": _confirmed_steady_emission_y(approved_settings),
            "allow_missing_s1": _confirmed_allow_missing_s1(approved_settings),
        }
    return convert_extracted_results(
        load_book_results_read_only(
            Path(snapshot_path),
            expected_snapshot_sha256=snapshot_sha256,
            source_ids=expected_source_ids,
            cancel_check=cancel_check,
            **read_kwargs,
        ),
        source_filenames=source_filenames,
        expected_source_ids=expected_source_ids,
        cancel_check=cancel_check,
    )


def _sample_form_prefill(sample: object) -> dict[str, str]:
    if isinstance(sample, LiquidSample):
        concentration = sample.concentration.removesuffix(" M").strip()
        return {
            "sample_type": "solution",
            "sample": sample.sample,
            "solvent": sample.solvent,
            "concentration": concentration,
            "temperature": sample.temperature,
        }
    if isinstance(sample, NeatSample):
        prefill = {
            "sample_type": "solid",
            "sample": sample.sample,
            "state": sample.state,
            "temperature": sample.temperature,
        }
        if sample.oxygen_environment:
            prefill["oxygen_environment"] = sample.oxygen_environment
        return prefill
    if isinstance(sample, DopedSample):
        concentration, unit = sample.concentration.rsplit(" ", 1)
        prefill = {
            "sample_type": "doped",
            "sample": sample.guest,
            "host": sample.host,
            "concentration": concentration,
            "concentration_unit": unit,
            "state": sample.state,
            "temperature": sample.temperature,
        }
        if sample.oxygen_environment:
            prefill["oxygen_environment"] = sample.oxygen_environment
        return prefill
    raise TypeError(f"Unsupported sample record: {type(sample).__name__}")


def _visible_book_name(book) -> str:
    for value in (book.display_name, book.short_name):
        name = str(value or "")
        if name.strip():
            return name
    return "未命名 Book"


def _attribution_book_labels(candidates) -> dict[str, str]:
    return {
        candidate.book_key: _visible_book_name(candidate)
        for candidate in candidates
    }


def _shared_attribution(book_keys, assignments):
    values = [assignments.get(book_key) for book_key in book_keys]
    if not values or any(value is None for value in values):
        return None
    first = values[0]
    return first if all(value == first for value in values[1:]) else None


def _attribution_rows_from_session(
    session,
    candidate_by_key,
    book_labels,
) -> tuple[tuple[str, str, str], ...]:
    rows = []
    assignments = session.assignments
    for target in session.targets:
        attribution = _shared_attribution(
            target.book_keys,
            assignments,
        )
        if attribution is None:
            for book_key in target.book_keys:
                book_attribution = assignments.get(book_key)
                if book_attribution is None:
                    continue
                member = candidate_by_key[book_key]
                rows.append(
                    (
                        member.source_filename,
                        f"{target.folder_path or 'Root'} / "
                        f"{book_labels[book_key]}",
                        book_attribution.sample.canonical_label,
                    )
                )
            continue
        members = tuple(
            candidate_by_key[book_key]
            for book_key in target.book_keys
        )
        rows.append(
            (
                members[0].source_filename,
                _attribution_target_label(target, members, book_labels),
                attribution.sample.canonical_label,
            )
        )
    return tuple(rows)


def _attribution_target_label(target, members, book_labels=None) -> str:
    if target.scope == "folder":
        return target.folder_path or "Root"
    folder = target.folder_path or "Root"
    member = members[0]
    label = (
        _visible_book_name(member)
        if book_labels is None
        else book_labels[member.book_key]
    )
    return f"{folder} / {label}"


def _candidate_rejection_row(rejection, *, include_status: bool = True) -> tuple[str, str, str]:
    display_name = _visible_book_name(rejection)
    folder = rejection.folder_path.strip("/")
    location = f"{folder} / {display_name}" if folder else f"Root / {display_name}"
    reason = _candidate_rejection_reason(rejection)
    result = f"已排除：{reason}" if include_status else reason
    return rejection.source_filename, location, result


def _candidate_rejection_reason(rejection) -> str:
    evidence = tuple(
        (name, _measurement_text(value))
        for name, value in (
            ("s1_max", rejection.s1_max),
            ("x_at_s1_max", rejection.x_at_s1_max),
            ("max_y", rejection.max_y),
            ("x_at_max_y", rejection.x_at_max_y),
        )
        if value is not None
    )
    return canonical_audit_detail(str(rejection.reason), evidence)


def _measurement_text(value) -> str:
    return _canonical_measurement_text(value)


def _candidate_rejection_rows(rejections, *, include_status: bool = True) -> tuple[tuple[str, str, str], ...]:
    return tuple(
        _candidate_rejection_row(rejection, include_status=include_status)
        for rejection in rejections
    )


_SELECTION_EXCLUSION_REASON_TEXT = {
    "special_group_rejected": "特殊组审核已拒绝",
    "emission_duplicate_unselected": "重复发射谱审核未选中",
    "exact_excitation_duplicate_unselected": "完全重复激发谱审核未选中",
    "excitation_candidate_unselected": "激发谱审核未选中",
}


def _selection_exclusion_rows(
    exclusions,
    candidate_by_key,
) -> tuple[tuple[str, str, str], ...]:
    rows = []
    for exclusion in exclusions:
        candidate = candidate_by_key[exclusion.book_key]
        folder = candidate.folder_path.strip("/")
        display_name = _visible_book_name(candidate)
        location = (
            f"{folder} / {display_name}"
            if folder
            else f"Root / {display_name}"
        )
        rows.append(
            (
                candidate.source_filename,
                location,
                f"已排除：{_SELECTION_EXCLUSION_REASON_TEXT[exclusion.reason]}",
            )
        )
    return tuple(rows)


def _dialog_label(qt_widgets: Any, qt_gui: Any, text: str, parent: Any) -> Any:
    label = qt_widgets.QLabel(text, parent)
    label.setObjectName("dialog_form_label")
    label.setFont(_dialog_font(qt_gui, 13, bold=True))
    return label


def _dialog_font(qt_gui: Any, pixel_size: int, *, bold: bool = False) -> Any:
    font = qt_gui.QFont("Microsoft YaHei UI")
    font.setPixelSize(pixel_size)
    font.setBold(bold)
    return font


def _load_qt_gui() -> Any:
    from PySide6 import QtGui

    return QtGui
def _missing_pre_extraction_context_builder(**kwargs):
    del kwargs
    raise ProductRunnerError("pre-extraction context builder is not configured")


def _missing_extraction_runner(context):
    del context
    raise ProductRunnerError("extraction runner is not configured")


def _build_pre_extraction_context_builder(
    *,
    local_appdata,
    dialog_port,
    origin_process_probe,
    process_controller,
    protected_paths=(),
    free_bytes_provider=None,
    copy_file=None,
):
    if origin_process_probe is None:
        origin_process_probe = default_origin_process_probe
    if process_controller is None:
        process_controller = WindowsOriginProcessController(process_probe=origin_process_probe)

    def build_context(**approved_inputs):
        return prepare_approved_pre_extraction_context(
            **approved_inputs,
            local_appdata=local_appdata,
            protected_paths=protected_paths,
            dialog_port=dialog_port,
            origin_process_probe=origin_process_probe,
            process_controller=process_controller,
            free_bytes_provider=free_bytes_provider,
            copy_file=copy_file,
            run_origin_process_preflight=False,
        )

    return build_context

def _load_window_settings(settings_store, startup_result) -> tuple[Settings, list[Notice]]:
    if startup_result is not None and startup_result.settings is not None:
        return startup_result.settings, list(startup_result.notices)
    if not hasattr(settings_store, "load"):
        return Settings(), []
    settings, notices = settings_store.load()
    return settings, list(notices)


def _validated_remembered_output_parent(settings, settings_store) -> tuple[str, list[Notice]]:
    remembered = str(getattr(settings, "lastOutputParent", "") or "")
    if not remembered:
        return "", []
    path = Path(remembered)
    if path.is_dir() and os.access(path, os.W_OK):
        return remembered, []
    notices = [Notice(severity="warning", message=f"上次输出位置不可用，已清除：{remembered}")]
    setter = getattr(settings_store, "set_last_output_parent", None)
    if callable(setter):
        notices.extend(setter("") or ())
    return "", notices


def _deliver_startup_notices(controller, settings_store, notices) -> None:
    for notice in notices or ():
        if notice.severity == "conspicuous":
            controller.manual_dialog_port.choose(
                DialogRequest(
                    kind="settings_reset_notice",
                    title="设置文件损坏",
                    message=notice.message,
                    actions=("acknowledge",),
                )
            )
            discard = getattr(settings_store, "discard_damaged_file", None)
            if callable(discard):
                controller._publish_notices(discard())
        controller._publish_notices((notice,))


class _StartupCancelled(RuntimeError):
    pass


def _pump_startup_events_or_cancel(app, window) -> None:
    app.processEvents()
    if not window.isVisible():
        raise _StartupCancelled("startup window was closed")


def _check_sample_library_health(sample_library, cancel_check=None):
    check_health = sample_library.check_health
    if cancel_check is None:
        return check_health()
    try:
        parameters = inspect.signature(check_health).parameters
    except (TypeError, ValueError):
        return check_health(cancel_check=cancel_check)
    accepts_cancel_keyword = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    ) or (
        "cancel_check" in parameters
        and parameters["cancel_check"].kind is not inspect.Parameter.POSITIONAL_ONLY
    )
    if accepts_cancel_keyword:
        return check_health(cancel_check=cancel_check)
    return check_health()


def _run_startup_storage_operation(qt_core, window, operation, *, cancellable):
    cancel_requested = threading.Event()
    done = threading.Event()
    result: dict[str, object] = {}

    def worker_cancel_check() -> None:
        if cancellable and cancel_requested.is_set():
            raise _StartupCancelled("startup window was closed")

    def work() -> None:
        try:
            result["value"] = operation(worker_cancel_check)
        except BaseException as exc:
            result["error"] = exc
        finally:
            done.set()

    worker = threading.Thread(target=work, name="sample-library-startup", daemon=False)
    worker.start()
    loop = qt_core.QEventLoop()
    timer = qt_core.QTimer()
    timer.setInterval(10)

    def window_is_visible() -> bool:
        is_visible = getattr(window, "isVisible", None)
        return True if not callable(is_visible) else bool(is_visible())

    def poll() -> None:
        if not window_is_visible():
            cancel_requested.set()
        if done.is_set():
            loop.quit()

    timer.timeout.connect(poll)
    timer.start()
    if not done.is_set():
        loop.exec()
    timer.stop()
    worker.join()
    if "error" in result:
        raise result["error"]
    if not window_is_visible():
        raise _StartupCancelled("startup window was closed")
    return result.get("value")


def _is_present_compatible_sample_library(health) -> bool:
    return getattr(health, "status", None) == "healthy" and getattr(health, "exists", None) is True


def _sample_library_startup_gate(
    sample_library,
    dialog_port,
    *,
    cancel_check=None,
    operation_runner=None,
) -> tuple[bool, tuple[Notice, ...]]:
    def check_health():
        if operation_runner is None:
            return _check_sample_library_health(sample_library, cancel_check)
        return operation_runner(
            lambda worker_cancel: _check_sample_library_health(sample_library, worker_cancel),
            cancellable=True,
        )

    health = check_health()
    status_labels = {
        "corrupt": "数据库文件已损坏",
        "locked": "数据库文件正被占用",
        "schema-incompatible": "数据库结构与当前程序不兼容",
        "unreadable": "数据库文件无法读取",
    }

    while not health.healthy:
        if health.status == "health-check-failed":
            detail = getattr(health, "detail", None)
            suffix = f"\n诊断信息：{detail}" if detail else ""
            dialog_port.choose(
                DialogRequest(
                    kind="database_health_check_failed",
                    title="样品数据库检查失败",
                    message=f"无法安全完成样品数据库检查，程序不会修改该数据库并将退出。{suffix}",
                    actions=("acknowledge",),
                )
            )
            return False, ()
        reason = status_labels.get(health.status, "数据库无法正常使用")
        detail = getattr(health, "detail", None)
        if detail:
            reason = f"{reason}\n诊断信息：{detail}"
        backup_planner = getattr(sample_library, "planned_backup_path", None)
        planned_backup_is_bound = callable(backup_planner)
        if planned_backup_is_bound:
            backup_path = backup_planner()
        else:
            backup_path = Path("将在程序数据目录中创建带时间戳的备份")
        database_path = getattr(sample_library, "path", Path("样品数据库"))
        response = dialog_port.choose(
            database_recovery_dialog(reason, str(database_path), str(backup_path))
        )
        if response.action != "backup_new_empty":
            return False, ()
        if health.revision is not None:
            break
        health = check_health()
        if _is_present_compatible_sample_library(health):
            return True, ()
        if health.revision is None:
            dialog_port.choose(
                DialogRequest(
                    kind="database_recovery_failed",
                    title="样品数据库恢复失败",
                    message="无法确认样品数据库的确切状态，程序不会修改该数据库并将退出。",
                    actions=("acknowledge",),
                )
            )
            return False, ()

    if health.healthy:
        return True, ()

    try:
        def recover(_cancel_check):
            if planned_backup_is_bound:
                return sample_library.recover(
                    expected_revision=health.revision,
                    backup_path=backup_path,
                )
            return sample_library.recover(expected_revision=health.revision)

        if operation_runner is None:
            backup = recover(None)
        else:
            backup = operation_runner(recover, cancellable=False)
        recovered_health = check_health()
        if not _is_present_compatible_sample_library(recovered_health):
            detail = getattr(recovered_health, "detail", None)
            suffix = f": {detail}" if detail else ""
            raise SampleLibraryError(
                f"Rebuilt sample library failed verification ({recovered_health.status}){suffix}"
            )
    except (SampleLibraryError, OSError) as exc:
        dialog_port.choose(
            DialogRequest(
                kind="database_recovery_failed",
                title="样品数据库恢复失败",
                message=f"无法安全备份并重建样品数据库，程序将退出。\n{exc}",
                actions=("acknowledge",),
            )
        )
        return False, ()
    return True, (
        Notice(
            severity="warning",
            message=f"样品数据库已备份并重建为空库。备份位置：{backup}",
        ),
    )


def _install_safe_close_filter(window, controller, qt_core) -> None:
    if not hasattr(qt_core, "QObject") or not hasattr(window, "installEventFilter"):
        return

    class SafeCloseFilter(qt_core.QObject):
        def eventFilter(self, watched, event):
            close_type = qt_core.QEvent.Type.Close
            if watched is window and event.type() == close_type and (
                getattr(controller, "shutdown_pending", False)
                or getattr(controller, "_shutdown_exit_blocked", False)
            ):
                event.ignore()
                return True
            retained_context = getattr(controller, "approved_pre_extraction_context", None)
            if retained_context is None:
                orchestrator = getattr(controller, "orchestrator", None)
                retained_context = getattr(orchestrator, "task_cache", {}).get(
                    "approved_pre_extraction_context"
                )
            startup_pending = getattr(controller, "_startup_health_gate_pending", False)
            orchestrator = getattr(controller, "orchestrator", None)
            startup_cancelled = getattr(orchestrator, "cancelled", False)
            if watched is window and event.type() == close_type and (
                controller.run_in_progress
                or retained_context is not None
                or (startup_pending and not startup_cancelled)
            ):
                event.ignore()
                controller.cancel_after_preferences()
                return True
            return super().eventFilter(watched, event)

    close_filter = SafeCloseFilter(window)
    window.installEventFilter(close_filter)
    window._spectrum_organizer_close_filter = close_filter


def _install_activation_request_poller(
    window,
    startup_result,
    qt_core,
    *,
    log,
    qt_widgets=None,
):
    probe = getattr(
        startup_result,
        "activation_request_probe",
        None,
    )
    if not callable(probe):
        return None
    timer = qt_core.QTimer(window)
    timer.setInterval(100)

    def poll() -> None:
        try:
            requested = probe(timeout_ms=0)
        except Exception as exc:
            timer.stop()
            log(f"单实例激活监听失败：{exc}")
            return
        if not requested:
            return
        _activate_requested_window(
            window,
            qt_widgets,
        )

    timer.timeout.connect(poll)
    timer.start()
    window._spectrum_organizer_activation_timer = timer
    return timer


def _activate_requested_window(window, qt_widgets=None):
    target = window
    application = getattr(
        qt_widgets,
        "QApplication",
        None,
    )
    if application is not None:
        for getter_name in (
            "activeModalWidget",
            "activeWindow",
        ):
            getter = getattr(application, getter_name, None)
            if not callable(getter):
                continue
            candidate = getter()
            if candidate is not None:
                target = candidate
                break
    _restore_activation_widget(window)
    if target is not window:
        _restore_activation_widget(target)
    target.raise_()
    target.activateWindow()
    win_id = getattr(target, "winId", None)
    if callable(win_id):
        try:
            hwnd = int(win_id())
        except (TypeError, ValueError):
            hwnd = 0
        if hwnd:
            _bring_native_window_to_foreground(hwnd)
    return target


def _restore_activation_widget(widget) -> None:
    if widget.isMinimized():
        widget.showNormal()
    elif not widget.isVisible():
        widget.show()


def _bring_native_window_to_foreground(hwnd: int) -> bool:
    if sys.platform != "win32" or not hwnd:
        return False
    try:
        import ctypes

        user32 = ctypes.windll.user32
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)
        user32.BringWindowToTop(hwnd)
        activated = bool(user32.SetForegroundWindow(hwnd))
        return activated or int(
            user32.GetForegroundWindow()
        ) == int(hwnd)
    except (AttributeError, OSError, TypeError, ValueError):
        return False


def run_main_window(
    *,
    startup_result=None,
    settings_store=None,
    file_dialogs=None,
    message_box=None,
    preflight_dialog=None,
    local_appdata=None,
    pre_extraction_context_builder=None,
    extraction_runner=None,
    start_run_runner=None,
    output_stage_runner=None,
    manual_dialog_port=None,
    attribution_dialog_port=None,
    candidate_loader=None,
    sample_library=None,
    origin_process_probe=None,
    process_controller=None,
    protected_paths=(),
    free_bytes_provider=None,
    copy_file=None,
) -> int:
    from PySide6 import QtCore, QtWidgets

    app = QtWidgets.QApplication.instance()
    owns_app = app is None
    if app is None:
        app = QtWidgets.QApplication(sys.argv)
    window, widgets = create_production_main_window(dpi_percent=100, size_name="desktop")
    app_paths = None
    if startup_result is not None:
        app_paths = getattr(startup_result, "paths", None)
    if app_paths is None and local_appdata is not None:
        app_paths = ensure_app_paths(local_appdata)
    if settings_store is None:
        if startup_result is not None and startup_result.settings_store is not None:
            settings_store = startup_result.settings_store
        else:
            app_paths = app_paths or ensure_app_paths(local_appdata)
            settings_store = SettingsStore(app_paths.settings_file)
    if sample_library is None and app_paths is not None:
        sample_library = SampleLibrary(
            app_paths.data / "sample_library.sqlite3",
            app_paths.backups,
            clock=lambda: datetime.now().strftime("%Y%m%d_%H%M%S"),
            health_temp_root=app_paths.temp,
        )
    if file_dialogs is None:
        file_dialogs = QtFileDialogs(QtWidgets)
    if message_box is None:
        message_box = QtMessageBoxPort(QtWidgets)
    if preflight_dialog is None:
        preflight_dialog = QtPreflightDialogPort(QtWidgets, QtCore)
    if manual_dialog_port is None:
        manual_dialog_port = QtManualDialogPort(parent=window)
    if attribution_dialog_port is None:
        attribution_dialog_port = QtAttributionDialogPort()
    if candidate_loader is None:
        candidate_loader = _load_candidate_conversion
    use_default_origin_process_probe = origin_process_probe is None
    if pre_extraction_context_builder is None:
        if free_bytes_provider is None and copy_file is None:
            from spectrum_organizer.product_runner import PreExtractionSubprocessRunner

            pre_extraction_context_builder = PreExtractionSubprocessRunner(
                local_appdata=local_appdata,
                protected_paths=protected_paths,
            )
        else:
            pre_extraction_context_builder = _build_pre_extraction_context_builder(
                local_appdata=local_appdata,
                dialog_port=manual_dialog_port,
                origin_process_probe=origin_process_probe,
                process_controller=process_controller,
                protected_paths=protected_paths,
                free_bytes_provider=free_bytes_provider,
                copy_file=copy_file,
            )
    if origin_process_probe is None:
        origin_process_probe = default_origin_process_probe
    if process_controller is None:
        process_controller = WindowsOriginProcessController(process_probe=origin_process_probe)
    if extraction_runner is None:
        from spectrum_organizer.product_runner import ExtractionSubprocessRunner

        if use_default_origin_process_probe:
            extraction_runner = ExtractionSubprocessRunner()
        else:
            extraction_runner = ExtractionSubprocessRunner(origin_process_probe=origin_process_probe)

    controller = None

    def origin_process_gate():
        if controller is not None:
            controller.show_origin_process_wait()
        complete_pre_extraction_origin_process_gate(manual_dialog_port, origin_process_probe, process_controller)

    origin_process_gate_on_ui = QtBlockingUiCall(QtCore, origin_process_gate)

    def pre_origin_process_check():
        origin_process_gate_on_ui()
        processes = tuple(origin_process_probe(timeout=5.0))
        if processes:
            pids = ", ".join(str(process.pid) for process in processes)
            raise ProductRunnerError(f"准备启动读取 Worker 时检测到 Origin 进程：{pids}；请重新开始并完成 Origin 检查")

    if output_stage_runner is None and hasattr(QtCore, "QThread"):
        from spectrum_organizer.origin.output_process import (
            JsonOriginChildProcessRunner,
            OriginWorkerProcessPort,
        )
        from spectrum_organizer.ui.output_stage import (
            QtOutputStageRunner,
        )
        from spectrum_organizer.workflow.output_pipeline import (
            FailureLogRequest,
            OutputPipelineCancelled,
            OutputPipelineJob,
            OutputPipelinePorts,
            ReportBuildRequest,
        )

        child_runner = JsonOriginChildProcessRunner(
            cancellation_error_factory=OutputPipelineCancelled,
        )

        def remove_output_attempt(targets, expected_identity):
            remove_run_owned_artifact(
                targets,
                targets.staging_project_path,
                run_id=targets.run_id,
                expected_identity=expected_identity,
            )

        def prepare_output_attempt(targets):
            return reserve_staging_artifact_identity(
                targets,
                targets.staging_project_path,
                run_id=targets.run_id,
            )

        def remove_verifier_attempt(targets, expected_identity):
            remove_run_owned_artifact(
                targets,
                targets.verifier_mutation_path,
                run_id=targets.run_id,
                expected_identity=expected_identity,
            )

        def prepare_verifier_attempt(targets):
            return reserve_staging_artifact_identity(
                targets,
                targets.verifier_mutation_path,
                run_id=targets.run_id,
            )

        def register_output_artifact(targets, artifact):
            register_staging_artifact_identity(
                targets,
                targets.staging_project_path,
                run_id=targets.run_id,
                expected_identity=artifact.identity,
            )

        def register_failed_artifact(targets, stage, expected_identity):
            artifact_path = (
                targets.staging_project_path
                if stage == "write_output"
                else targets.verifier_mutation_path
            )
            register_staging_artifact_identity(
                targets,
                artifact_path,
                run_id=targets.run_id,
                expected_identity=expected_identity,
            )

        origin_worker_port = OriginWorkerProcessPort(
            child_runner=child_runner,
            prepare_output=prepare_output_attempt,
            prepare_verifier=prepare_verifier_attempt,
            cleanup_output=remove_output_attempt,
            cleanup_verifier=remove_verifier_attempt,
            cancellation_exception=OutputPipelineCancelled,
        )

        def build_output_report(request: ReportBuildRequest):
            return build_approved_output_report(
                request.approved_snapshot,
                output_path=request.targets.final_run_dir,
                source_fingerprints_after=(
                    request.source_fingerprints_after
                ),
                verifier_readback_spectrum_count=(
                    request.verifier_result.readback_spectrum_count
                ),
                verifier_readback_column_count=(
                    request.verifier_result.readback_column_count
                ),
            )

        def write_output_failure(request: FailureLogRequest):
            return write_failure_log(
                request.timestamp,
                f"失败阶段：{request.stage}\n原因：{_exception_with_notes(request.cause)}",
                local_appdata=local_appdata,
                output_attempts=request.output_attempts,
                verifier_attempts=request.verifier_attempts,
            )

        output_job = OutputPipelineJob(
            ports=OutputPipelinePorts(
                process_gate=pre_origin_process_check,
                create_staging=create_run_staging,
                run_output=origin_worker_port.run_output,
                run_verifier=origin_worker_port.run_verifier,
                verify_sources=_verify_approved_output_sources,
                build_report=build_output_report,
                publish=publish_completed_run,
                cleanup=cleanup_owned_staging,
                write_failure=write_output_failure,
                register_artifact=register_output_artifact,
                register_failed_artifact=register_failed_artifact,
                reset_workers=origin_worker_port.reset,
                cancel_workers=origin_worker_port.cancel,
                retry_workers=origin_worker_port.retry_cleanup,
                post_commit=_cleanup_committed_output,
                retry_post_commit=_retry_committed_output_cleanup,
            ),
            clock=datetime.now,
        )
        output_stage_runner = QtOutputStageRunner(
            QtCore,
            output_job,
        )

    if start_run_runner is None:
        start_run_job = CancellableStartRunJob(
            pre_extraction_context_builder,
            extraction_runner,
            pre_origin_process_check=pre_origin_process_check,
            candidate_loader=candidate_loader,
        )
        start_run_runner = QtThreadedStartRunRunner(
            QtCore,
            start_run_job,
            cancel_func=start_run_job.cancel,
        )

    startup_settings, startup_notices = _load_window_settings(
        settings_store,
        startup_result,
    )
    initial_output_parent, output_parent_notices = _validated_remembered_output_parent(
        startup_settings,
        settings_store,
    )
    startup_notices.extend(output_parent_notices)
    timer_type = getattr(QtCore, "QTimer", None)
    extraction_activity_timer = timer_type() if callable(timer_type) else None
    if extraction_activity_timer is not None:
        extraction_activity_timer.setInterval(1000)
    controller = FullRunUiController(
        parent=window,
        widgets=widgets,
        orchestrator=BookOnlyOrchestrator(settings_store),
        file_dialogs=file_dialogs,
        message_box=message_box,
        preflight_dialog=preflight_dialog,
        manual_dialog_port=manual_dialog_port,
        attribution_dialog_port=attribution_dialog_port,
        candidate_loader=candidate_loader,
        schedule_call=lambda callback: QtCore.QTimer.singleShot(0, callback),
        extraction_activity_timer=extraction_activity_timer,
        pre_extraction_context_builder=pre_extraction_context_builder,
        extraction_runner=extraction_runner,
        start_run_runner=start_run_runner,
        task8_runner=QtThreadedTask8Runner(QtCore),
        output_stage_runner=output_stage_runner,
        origin_process_gate=None,
        default_s1_limit=startup_settings.s1Limit,
        default_steady_emission_y=startup_settings.steadyEmissionY,
        default_allow_missing_s1=startup_settings.allowMissingS1,
        initial_output_parent=initial_output_parent,
    )
    origin_process_gate_on_ui.set_cancel_confirmation_guard(
        defer=controller._defer_during_cancel_confirmation,
        cancelled=lambda: controller.orchestrator.cancelled,
    )
    window._spectrum_organizer_controller = controller
    controller.set_startup_health_gate_pending(sample_library is not None)
    _install_safe_close_filter(window, controller, QtCore)
    _install_activation_request_poller(
        window,
        startup_result,
        QtCore,
        log=controller._log,
        qt_widgets=QtWidgets,
    )
    window.show()
    _deliver_startup_notices(
        controller,
        settings_store,
        startup_notices,
    )
    if sample_library is not None:
        try:
            can_continue, sample_library_notices = _sample_library_startup_gate(
                sample_library,
                manual_dialog_port,
                operation_runner=lambda operation, *, cancellable: _run_startup_storage_operation(
                    QtCore,
                    window,
                    operation,
                    cancellable=cancellable,
                ),
            )
        except _StartupCancelled:
            window.close()
            return 0
        finally:
            controller.set_startup_health_gate_pending(False)
        controller._publish_notices(sample_library_notices)
        if not can_continue:
            window.close()
            return 0
    if owns_app:
        return int(app.exec())
    return 0
