from __future__ import annotations

from pathlib import Path

from spectrum_organizer.reporting.publication import ParentUnavailableError
from spectrum_organizer.workflow.output_pipeline import (
    OutputPipelineCancelled,
    OutputStageRequest,
)


class OutputStageUiCoordinator:
    """Own the output-stage lifecycle while the main UI hosts presentation ports."""

    def __init__(self, host):
        self.host = host
        self.active = False
        self.committed = False

    def start(self, generation, draft, approved_snapshot) -> None:
        host = self.host
        request = OutputStageRequest(
            approved_snapshot,
            Path(host.output_parent),
        )
        host.run_in_progress = True
        self.active = True
        self.committed = False
        host._show_output_stage_progress(
            "process_gate",
            draft.extraction_summary,
            approved_snapshot,
        )
        try:
            host.output_stage_runner.start(
                request,
                lambda result: host._handle_output_stage_success(
                    generation,
                    draft,
                    approved_snapshot,
                    result,
                ),
                lambda error: host._handle_output_stage_failure(
                    generation,
                    draft,
                    approved_snapshot,
                    error,
                ),
                lambda stage: host._handle_output_stage_progress(
                    generation,
                    draft.extraction_summary,
                    approved_snapshot,
                    stage,
                ),
            )
        except Exception as exc:
            host._handle_output_stage_failure(
                generation,
                draft,
                approved_snapshot,
                exc,
            )

    def handle_progress(
        self,
        generation,
        extraction_summary,
        approved_snapshot,
        stage: str,
    ) -> None:
        host = self.host
        if host._defer_during_cancel_confirmation(
            lambda: host._handle_output_stage_progress(
                generation,
                extraction_summary,
                approved_snapshot,
                stage,
            )
        ):
            return
        if (
            generation != host._run_generation
            or host.orchestrator.cancelled
            or host.shutdown_pending
        ):
            return
        if stage == "committed":
            self.committed = True
        host._show_output_stage_progress(
            stage,
            extraction_summary,
            approved_snapshot,
        )

    def show_progress(
        self,
        stage: str,
        extraction_summary,
        approved_snapshot,
    ) -> None:
        host = self.host
        status, title, subtitle, progress = _OUTPUT_PROGRESS.get(
            stage,
            (
                "正在生成输出文件",
                "生成输出文件",
                "正在处理已确认的最终输出计划。",
                92,
            ),
        )
        reconciliation = approved_snapshot.count_reconciliation
        host._log(f"输出阶段：{title}；{status}")
        host._runtime_update(
            stage="complete" if stage == "complete" else "output",
            phase_detail="处理中",
            runtime_status=status,
            activity_mode="automatic",
            title=title,
            subtitle=subtitle,
            progress=progress,
            progress_busy=False,
            summary_numbers=(
                str(_summary_value(extraction_summary, "total_inventory_count", 0)),
                str(reconciliation.accepted_ordinary_spectrum_count),
                "0",
                str(
                    reconciliation.rejected_book_count
                    + reconciliation.excluded_book_count
                ),
            ),
            review_headers=("输出步骤", "项目数量", "当前状态"),
            review_rows=((title, "1", status),),
            show_review_table=True,
            show_attention=False,
            show_input_controls=False,
            show_completion_actions=False,
        )

    def handle_success(
        self,
        generation,
        draft,
        approved_snapshot,
        result,
    ) -> None:
        host = self.host
        if host._defer_during_cancel_confirmation(
            lambda: host._handle_output_stage_success(
                generation,
                draft,
                approved_snapshot,
                result,
            )
        ):
            return
        if (
            generation != host._run_generation
            or host.orchestrator.cancelled
            or host.shutdown_pending
        ):
            return
        host.run_in_progress = False
        self.active = False
        self.committed = True
        completion = result.completion
        host.orchestrator.task_cache["output_completion"] = completion
        post_commit_error = getattr(result, "post_commit_error", None)
        post_commit_cleanup_pending = bool(
            getattr(
                result,
                "post_commit_cleanup_pending",
                post_commit_error is not None,
            )
        )
        attention_message = ""
        if post_commit_error is not None:
            host.orchestrator.task_cache[
                "output_post_commit_error"
            ] = post_commit_error
            attention_message = (
                f"输出已提交，但收尾清理失败：{post_commit_error}。"
                "输出项目和运行报告仍然有效。"
            )
        if post_commit_cleanup_pending:
            host.orchestrator.task_cache[
                "output_post_commit_cleanup_pending"
            ] = True
            host._shutdown_error = attention_message
            host._shutdown_exit_blocked = True
            host._shutdown_cleanup_owner = "output"
        host._log(
            f"输出完成：{completion.project_path}；运行报告：{completion.report_path}"
        )
        if attention_message:
            host._log(attention_message)
        reconciliation = approved_snapshot.count_reconciliation
        host._runtime_update(
            stage="complete",
            phase_detail="已完成",
            runtime_status="任务完成",
            activity_mode="idle",
            title="任务完成",
            subtitle=f"输出已保存到：{completion.output_path}",
            progress=100,
            progress_busy=False,
            summary_numbers=(
                str(
                    _summary_value(
                        draft.extraction_summary,
                        "total_inventory_count",
                        0,
                    )
                ),
                str(reconciliation.accepted_ordinary_spectrum_count),
                str(completion.project_count),
                str(
                    reconciliation.rejected_book_count
                    + reconciliation.excluded_book_count
                ),
            ),
            review_headers=("输出文件", "已整理谱图", "结果"),
            review_rows=(
                (
                    str(completion.project_path),
                    str(reconciliation.output_plan_spectrum_count),
                    "已完成",
                ),
            ),
            show_review_table=True,
            show_attention=bool(attention_message),
            attention_message=attention_message,
            show_input_controls=False,
            show_completion_actions=True,
        )

    def handle_failure(
        self,
        generation,
        draft,
        approved_snapshot,
        error,
    ) -> None:
        host = self.host
        if host._defer_during_cancel_confirmation(
            lambda: host._handle_output_stage_failure(
                generation,
                draft,
                approved_snapshot,
                error,
            )
        ):
            return
        if generation != host._run_generation or host.orchestrator.cancelled:
            return
        if host.shutdown_pending and isinstance(error, OutputPipelineCancelled):
            diagnostics = output_failure_diagnostics(error)
            if diagnostics:
                host._shutdown_error = (
                    "取消输出时未能确认临时输出已完全清理："
                    f"{diagnostics.strip()}"
                )
                host._shutdown_exit_blocked = True
                host._shutdown_cleanup_owner = "output"
                host._log(host._shutdown_error)
            return
        host.run_in_progress = False
        self.active = False
        self.committed = False
        stage = getattr(error, "stage", "output")
        cause = getattr(error, "cause", error)
        failure_log_path = getattr(error, "failure_log_path", None)
        diagnostics = output_failure_diagnostics(error)
        cleanup_blocked = output_cleanup_is_blocked(error)
        if cleanup_blocked:
            host._shutdown_error = (
                "输出失败后未能确认临时输出已完全清理："
                f"{diagnostics.strip()}"
            )
            host._shutdown_exit_blocked = True
            host._shutdown_cleanup_owner = "output"
        if isinstance(cause, ParentUnavailableError) and not cleanup_blocked:
            self._handle_unavailable_parent(
                generation,
                draft,
                approved_snapshot,
                cause,
                failure_log_path,
                diagnostics,
            )
            return
        message = f"失败阶段：{stage}\n原因：{cause}"
        if failure_log_path is not None:
            message += f"\n失败日志：{failure_log_path}"
        message += diagnostics
        message += "\n请检查以上路径和原因后重新开始任务。"
        host._log(f"输出失败：{stage}：{cause}")
        host.message_box.blocking_error(
            host.parent,
            title="输出文件生成失败",
            message=message,
        )

    def _handle_unavailable_parent(
        self,
        generation,
        draft,
        approved_snapshot,
        cause,
        failure_log_path,
        diagnostics,
    ) -> None:
        host = self.host
        message = (
            f"输出位置不可用：{cause.path}\n"
            f"原因：{cause.reason}\n"
            "请选择另一个输出位置；已确认的样品归属、冲突选择和输出计划将保留。"
        )
        if failure_log_path is not None:
            message += f"\n失败日志：{failure_log_path}"
        message += diagnostics
        host.message_box.blocking_error(
            host.parent,
            title="输出位置不可用",
            message=message,
        )
        replacement = host.file_dialogs.select_output_parent(host.parent)
        if not replacement:
            return
        host._persist_setting_with_damage_recovery(
            lambda: host.orchestrator.select_output_parent(replacement)
        )
        host.output_parent = replacement
        setter = getattr(host.file_dialogs, "set_initial_output_parent", None)
        if callable(setter):
            setter(replacement)
        host._set_label("output_path_label", f"输出位置：{replacement}")
        host._log(
            f"输出位置已更改：{replacement}；继续使用同一 Approved Snapshot"
        )
        host._start_output_stage(generation, draft, approved_snapshot)

    def commit_has_completed(self) -> bool:
        if self.committed:
            return True
        return bool(
            self.active
            and getattr(self.host.output_stage_runner, "committed", False)
        )

    def finish_pending_shutdown(self) -> None:
        host = self.host
        host.shutdown_pending = False
        self.active = False
        host.run_in_progress = False
        if host._shutdown_exit_blocked:
            host.message_box.blocking_error(
                host.parent,
                title="取消任务时发生错误",
                message=host._shutdown_error,
            )
            return
        host._cancel_and_exit_after_preferences()

    def finish_cleanup_retry(self, error) -> None:
        host = self.host
        host.shutdown_pending = False
        if error is not None:
            host._shutdown_error = str(error)
            host._shutdown_exit_blocked = True
            host.message_box.blocking_error(
                host.parent,
                title="取消任务时发生错误",
                message=host._shutdown_error,
            )
            host._log(host._shutdown_error)
            return
        host._shutdown_error = None
        host._shutdown_exit_blocked = False
        host._shutdown_cleanup_owner = None
        host._cancel_and_exit_after_preferences()


_OUTPUT_PROGRESS = {
    "process_gate": (
        "正在确认 Origin 进程状态",
        "准备生成输出",
        "正在重新执行输出阶段的 Origin 安全检查。",
        92,
    ),
    "create_staging": (
        "正在准备输出目录",
        "准备生成输出",
        "正在创建本次任务专属的临时输出目录。",
        93,
    ),
    "write_output": (
        "正在创建输出项目",
        "生成 Origin 输出",
        "正在写入全新的整理后 Origin 项目。",
        95,
    ),
    "verify_output": (
        "正在独立校验输出",
        "校验 Origin 输出",
        "正在由独立校验进程回读结构、数据和公式依赖。",
        97,
    ),
    "verify_sources": (
        "正在复核原始文件",
        "复核原始文件",
        "正在确认所有已选原始文件保持不变。",
        98,
    ),
    "build_report": (
        "正在生成运行报告",
        "生成运行报告",
        "正在写入完整审核、计数和文件指纹记录。",
        99,
    ),
    "publish": (
        "正在发布输出文件",
        "发布最终输出",
        "正在原子提交 Origin 项目与运行报告。",
        99,
    ),
    "committed": (
        "输出已提交，正在完成收尾",
        "完成输出收尾",
        "输出项目和运行报告已经提交，正在清理本次任务临时文件。",
        100,
    ),
    "complete": (
        "输出阶段已完成",
        "输出阶段已完成",
        "正在显示最终输出路径和本次任务结果。",
        100,
    ),
}


def output_failure_diagnostics(error) -> str:
    lines = []
    failure_log_error = getattr(error, "failure_log_error", None)
    cleanup_error = getattr(error, "cleanup_error", None)
    cleanup_result = getattr(error, "cleanup_result", None)
    retained = tuple(
        getattr(cleanup_result, "retained_unknown", ())
        if cleanup_result is not None
        else ()
    )
    if failure_log_error is not None:
        lines.append(f"失败日志写入失败：{failure_log_error}")
    if cleanup_error is not None:
        lines.append(f"临时输出清理失败：{cleanup_error}")
    if retained:
        lines.append(
            "未自动清理的对象："
            + "；".join(str(path) for path in retained)
        )
    lines.extend(
        f"附加错误：{note}" for note in getattr(error, "__notes__", ())
    )
    return "" if not lines else "\n" + "\n".join(lines)


def output_cleanup_is_blocked(error) -> bool:
    cleanup_result = getattr(error, "cleanup_result", None)
    retained = tuple(
        getattr(cleanup_result, "retained_unknown", ())
        if cleanup_result is not None
        else ()
    )
    return bool(
        getattr(error, "cleanup_error", None)
        or callable(getattr(error, "cleanup_retry", None))
        or retained
        or (
            isinstance(error, OutputPipelineCancelled)
            and getattr(error, "__notes__", ())
        )
    )


def _summary_value(summary: object, key: str, default: object = None) -> object:
    if isinstance(summary, dict):
        return summary.get(key, default)
    return getattr(summary, key, default)


class QtOutputStageRunner:
    def __init__(self, qt_core, run_func):
        self.qt_core = qt_core
        self.run_func = run_func
        self._threads = []
        self._stopped_callbacks = []

    def start(
        self,
        request,
        on_success,
        on_error,
        on_progress=None,
    ) -> None:
        if self._threads:
            raise RuntimeError("Output stage is already running")
        prepare = getattr(self.run_func, "prepare", None)
        if callable(prepare):
            prepare()
        qt_core = self.qt_core
        run_func = self.run_func

        class OutputStageThread(qt_core.QThread):
            progress = qt_core.Signal(object)
            result = None
            error = None

            def run(thread_self):
                try:
                    thread_self.result = run_func(request)
                except BaseException as exc:
                    thread_self.error = exc

        thread = OutputStageThread()
        set_progress_callback = getattr(
            run_func,
            "set_progress_callback",
            None,
        )
        if callable(set_progress_callback):
            set_progress_callback(thread.progress.emit)
        if on_progress is not None:
            thread.progress.connect(on_progress)
        thread.finished.connect(
            lambda *_args: self._complete_thread(
                thread,
                on_success,
                on_error,
            )
        )
        delete_later = getattr(thread, "deleteLater", None)
        if callable(delete_later):
            thread.finished.connect(lambda *_args: delete_later())
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
        cancel = getattr(self.run_func, "cancel", None)
        if callable(cancel):
            accepted = cancel()
            if accepted is False:
                return False
        if on_stopped is not None:
            self._stopped_callbacks.append(on_stopped)
        for thread in tuple(self._threads):
            request_interruption = getattr(
                thread,
                "requestInterruption",
                None,
            )
            if callable(request_interruption):
                request_interruption()
        return True

    def retry_cleanup(self, on_complete) -> bool:
        if self._threads:
            return False
        retry_cleanup = getattr(self.run_func, "retry_cleanup", None)
        if not callable(retry_cleanup):
            return False
        qt_core = self.qt_core

        class CleanupThread(qt_core.QThread):
            error = None

            def run(thread_self):
                try:
                    retry_cleanup()
                except BaseException as exc:
                    thread_self.error = exc

        thread = CleanupThread()
        thread.finished.connect(
            lambda *_args: self._complete_cleanup_thread(
                thread,
                on_complete,
            )
        )
        delete_later = getattr(thread, "deleteLater", None)
        if callable(delete_later):
            thread.finished.connect(lambda *_args: delete_later())
        self._threads.append(thread)
        try:
            thread.start()
        except BaseException:
            self._forget_thread(thread)
            if callable(delete_later):
                delete_later()
            raise
        return True

    @property
    def committed(self) -> bool:
        return bool(getattr(self.run_func, "committed", False))

    def _complete_thread(self, thread, on_success, on_error) -> None:
        callbacks = self._forget_thread(thread, notify=False)
        try:
            if thread.error is not None:
                on_error(thread.error)
            else:
                on_success(thread.result)
        finally:
            for callback in callbacks:
                callback()

    def _complete_cleanup_thread(self, thread, on_complete) -> None:
        callbacks = self._forget_thread(thread, notify=False)
        try:
            on_complete(thread.error)
        finally:
            for callback in callbacks:
                callback()

    def _forget_thread(self, thread, *, notify=True):
        if thread in self._threads:
            self._threads.remove(thread)
        if self._threads:
            return ()
        callbacks = tuple(self._stopped_callbacks)
        self._stopped_callbacks.clear()
        if notify:
            for callback in callbacks:
                callback()
            return ()
        return callbacks
