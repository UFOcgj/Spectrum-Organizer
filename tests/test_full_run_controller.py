import json
import copy
import math
import os
import pathlib
import sqlite3
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from spectrum_organizer.settings import SettingsStore
from spectrum_organizer import product_runner
from spectrum_organizer.domain.models import SpectrumClass
from spectrum_organizer.origin.extract_worker import InventoryBook, TerminalBookResult
from spectrum_organizer.store.run_snapshot import (
    ReconciliationError,
    RunSnapshot,
    snapshot_approval_sha256,
)
from spectrum_organizer.ui import app as app_module
from spectrum_organizer.ui.app import FullRunUiController
from spectrum_organizer.ui.dialog_port import ConflictReviewResponse
from spectrum_organizer.ui.orchestrator import BookOnlyOrchestrator


def _wait_for_qt_event(qt_core, event, *, timeout_ms=2_000):
    loop = qt_core.QEventLoop()
    poll = qt_core.QTimer()
    poll.setInterval(1)

    def stop_when_ready():
        if event.is_set():
            loop.quit()

    poll.timeout.connect(stop_when_ready)
    timeout = qt_core.QTimer()
    timeout.setSingleShot(True)
    timeout.timeout.connect(loop.quit)
    poll.start()
    timeout.start(timeout_ms)
    loop.exec()
    poll.stop()
    timeout.stop()
    return event.is_set()


class FakeFileDialogs:
    def __init__(self, *, source_paths=(), output_parent=""):
        self.source_paths = list(source_paths)
        self.output_parent = output_parent
        self.source_calls = 0
        self.output_calls = 0
        self.initial_output_parent = ""

    def set_initial_output_parent(self, path):
        self.initial_output_parent = path

    def select_origin_sources(self, parent):
        self.source_calls += 1
        return list(self.source_paths)

    def select_output_parent(self, parent):
        self.output_calls += 1
        return self.output_parent


class FakeMessageBox:
    def __init__(self):
        self.errors = []

    def blocking_error(self, parent, *, title, message):
        self.errors.append((title, message))


class FakePreflightDialog:
    def __init__(self, result=None):
        self.result = result or {"s1_limit": 42, "steady_emission_y": "S1c/R1c"}
        self.calls = []

    def confirm(self, parent, *, default_s1_limit, steady_emission_y, allow_missing_s1=False):
        self.calls.append((default_s1_limit, steady_emission_y, allow_missing_s1))
        return self.result


class FakeManualDialogPort:
    def __init__(self, response_action):
        self.response_action = response_action
        self.requests = []

    def choose(self, request):
        from spectrum_organizer.ui.dialog_port import (
            AttributionDialogResponse,
            DialogResponse,
        )

        self.requests.append(request)
        return DialogResponse(action=self.response_action)


class FakeAttributionDialogPort:
    def __init__(self, responses=(), book_responses=()):
        self.responses = list(responses)
        self.book_responses = list(book_responses)
        self.requests = []
        self.book_requests = []

    def choose(self, request, *, parent=None):
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("unexpected attribution dialog")
        return self.responses.pop(0)

    def choose_book(self, request, *, parent=None):
        self.book_requests.append(request)
        if not self.book_responses:
            raise AssertionError("unexpected pending-Book picker")
        return self.book_responses.pop(0)


class FakeConflictReviewDialogPort:
    def __init__(self, responses=(), on_choose=None):
        self.responses = list(responses)
        self.requests = []
        self.on_choose = on_choose

    def choose(self, request, *, parent=None):
        self.requests.append(request)
        if self.on_choose is not None:
            self.on_choose(request)
        if self.responses:
            return self.responses.pop(0)
        if request.kind == "special_group":
            return ConflictReviewResponse(action="confirm_group")
        if request.selection_mode == "single":
            return ConflictReviewResponse(
                action="confirm_selection",
                selected_book_keys=(request.choices[0].book_key,),
            )
        return ConflictReviewResponse(
            action="confirm_selection",
            selected_book_keys=tuple(choice.book_key for choice in request.choices),
        )


class FakeScrollBar:
    def __init__(self):
        self.values = []

    def maximum(self):
        return 100

    def setValue(self, value):
        self.values.append(value)


class FakeRunLog:
    def __init__(self):
        self.lines = []
        self.scrollbar = FakeScrollBar()

    def appendPlainText(self, value):
        self.lines.append(value)

    def verticalScrollBar(self):
        return self.scrollbar

    def toPlainText(self):
        return "\n".join(self.lines)


class FakeLabel:
    def __init__(self):
        self.value = ""

    def setText(self, value):
        self.value = value

    def text(self):
        return self.value


class FakeButton(FakeLabel):
    def __init__(self, value=""):
        super().__init__()
        self.value = value
        self.properties = {}
        self.visible = True

    def setProperty(self, name, value):
        self.properties[name] = value

    def property(self, name):
        return self.properties.get(name)

    def setVisible(self, visible):
        self.visible = bool(visible)

class RecordingPreExtractionContextBuilder:
    def __init__(self, *, result=None, error=None):
        self.result = result if result is not None else object()
        self.error = error
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.result


class RecordingExtractionRunner:
    def __init__(self, *, result=None, error=None):
        self.result = result if result is not None else {
            "total_inventory_count": 2,
            "total_extracted_count": 1,
            "total_rejected_count": 1,
            "source_summaries": (
                {
                    "source_id": "S0001",
                    "original_path": "C:/raw/a.opju",
                    "inventory_count": 2,
                    "extracted_count": 1,
                    "rejected_count": 1,
                },
            ),
        }
        self.error = error
        self.calls = []

    def __call__(self, context):
        self.calls.append(context)
        if self.error is not None:
            raise self.error
        return self.result


class RecordingAsyncExtractionRunner:
    def __init__(self, *, result=None):
        self.result = result if result is not None else RecordingExtractionRunner().result
        self.calls = []
        self.success_callback = None
        self.error_callback = None

    def start(self, context, on_success, on_error):
        self.calls.append(context)
        self.success_callback = on_success
        self.error_callback = on_error

    def succeed(self):
        self.success_callback(self.result)

    def fail(self, message):
        self.error_callback(message)


class RecordingAsyncStartRunRunner:
    def __init__(self, *, context=None, summary=None):
        self.context = context if context is not None else {"run_id": "async-context"}
        self.summary = summary if summary is not None else RecordingExtractionRunner().result
        self.calls = []
        self.success_callback = None
        self.error_callback = None
        self.progress_callback = None

    def start(self, approved_inputs, on_success, on_error, on_progress=None):
        self.calls.append(approved_inputs)
        self.success_callback = on_success
        self.error_callback = on_error
        self.progress_callback = on_progress

    def succeed(self):
        self.success_callback((self.context, self.summary))

    def fail(self, message):
        self.error_callback(message)

    def progress(self, event):
        self.progress_callback(event)


class RecordingAsyncTask8Runner:
    def __init__(self):
        self.pending = []
        self.cancelled = False
        self.stopped_callback = None

    def start(self, operation, on_success, on_error):
        self.pending.append((operation, on_success, on_error))

    def succeed_next(self):
        operation, on_success, _on_error = self.pending.pop(0)
        on_success(operation(self._raise_if_cancelled))

    def fail_next(self, error):
        _operation, _on_success, on_error = self.pending.pop(0)
        on_error(error)

    def cancel(self, on_stopped=None):
        if not self.pending:
            return False
        self.cancelled = True
        self.stopped_callback = on_stopped
        return True

    def finish_cancel(self):
        self.pending.clear()
        callback = self.stopped_callback
        self.stopped_callback = None
        if callback is not None:
            callback()

    def _raise_if_cancelled(self):
        if self.cancelled:
            raise RuntimeError("Task 8 cancelled")


class RecordingAsyncOutputStageRunner:
    def __init__(self):
        self.calls = []
        self.success_callback = None
        self.error_callback = None
        self.progress_callback = None
        self.cancelled = False
        self.stopped_callback = None

    def start(self, request, on_success, on_error, on_progress=None):
        self.calls.append(request)
        self.success_callback = on_success
        self.error_callback = on_error
        self.progress_callback = on_progress

    def cancel(self, on_stopped=None):
        if not self.calls:
            return False
        self.cancelled = True
        self.stopped_callback = on_stopped
        return True

    def succeed(self, result):
        self.success_callback(result)

    def fail(self, error):
        self.error_callback(error)

    def progress(self, stage):
        self.progress_callback(stage)


class FullRunControllerTests(unittest.TestCase):
    def test_task9_source_reverification_forwards_cancel_check_to_hashing(self):
        before = (
            types.SimpleNamespace(path=pathlib.Path("C:/raw/source.opju")),
        )
        cancel_check = object()

        with mock.patch.object(
            app_module,
            "snapshot_sources",
            return_value=["after"],
        ) as snapshot:
            result = app_module._verify_approved_output_sources(
                before,
                cancel_check,
            )

        self.assertEqual(("after",), result)
        snapshot.assert_called_once_with(
            [pathlib.Path("C:/raw/source.opju")],
            [],
            cancel_check=cancel_check,
        )

    @staticmethod
    def _qt_blocking_ui_call(callback):
        class FakeSignal:
            def __init__(self, *_args):
                self.callback = None

            def connect(self, callback):
                self.callback = callback

            def emit(self, request):
                self.callback(request)

        class FakeQObject:
            def thread(self):
                return "ui-thread"

        class FakeQThread:
            @staticmethod
            def currentThread():
                return "worker-thread"

        qt_core = types.SimpleNamespace(QObject=FakeQObject, Signal=FakeSignal, QThread=FakeQThread)
        return app_module.QtBlockingUiCall(qt_core, callback)

    def test_qt_output_dialog_uses_remembered_parent_only_as_starting_location(self):
        calls = []

        class RecordingDialog:
            @staticmethod
            def getExistingDirectory(parent, title, initial):
                calls.append((parent, title, initial))
                return "C:/NewOutput"

        dialogs = app_module.QtFileDialogs(types.SimpleNamespace(QFileDialog=RecordingDialog))
        dialogs.set_initial_output_parent("C:/Remembered")

        selected = dialogs.select_output_parent("parent")

        self.assertEqual("C:/NewOutput", selected)
        self.assertEqual([("parent", "选择输出位置", "C:/Remembered")], calls)

    def test_run_main_window_uses_default_origin_process_boundary_dependencies(self):
        self.assertTrue(hasattr(app_module, "default_origin_process_probe"))
        self.assertTrue(hasattr(app_module, "WindowsOriginProcessController"))
        captured = {}
        process = types.SimpleNamespace(pid=4242)
        probe_results = iter(((), (process,)))

        def sentinel_probe(*, timeout):
            self.assertEqual(5.0, timeout)
            return next(probe_results)

        class FakeQApplication:
            @classmethod
            def instance(cls):
                return object()

        fake_pyside = types.SimpleNamespace(QtCore=object(), QtWidgets=types.SimpleNamespace(QApplication=FakeQApplication))
        created = []

        class FakeWindow:
            def show(self):
                pass

        class FakeController:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                process_controllers.append(self)

        class FakeDialogPort:
            def choose(_self, request):
                self.assertEqual("save_and_close_origin", request.kind)
                return types.SimpleNamespace(action="retry")

        def fake_create_main_window(*, dpi_percent, size_name):
            window = FakeWindow()
            created.append((dpi_percent, size_name, window))
            return window, {}

        fake_context_builder = object()
        process_controllers = []

        def build_context_runner(**kwargs):
            captured.update(kwargs)
            return fake_context_builder

        original_pyside = sys.modules.get("PySide6")
        original_create = app_module.create_production_main_window
        original_probe = app_module.default_origin_process_probe
        original_controller = app_module.WindowsOriginProcessController
        original_extraction_runner = product_runner.ExtractionSubprocessRunner
        original_context_runner = product_runner.PreExtractionSubprocessRunner

        def fake_extraction_runner(context):
            return {"context": context}

        extraction_runner_kwargs = {}

        def build_extraction_runner(**kwargs):
            extraction_runner_kwargs.update(kwargs)
            return fake_extraction_runner

        sys.modules["PySide6"] = fake_pyside
        app_module.create_production_main_window = fake_create_main_window
        app_module.default_origin_process_probe = sentinel_probe
        app_module.WindowsOriginProcessController = FakeController
        product_runner.ExtractionSubprocessRunner = build_extraction_runner
        product_runner.PreExtractionSubprocessRunner = build_context_runner
        try:
            result = app_module.run_main_window(
                settings_store=object(),
                file_dialogs=object(),
                message_box=object(),
                preflight_dialog=object(),
                manual_dialog_port=FakeDialogPort(),
            )
            controller = created[0][2]._spectrum_organizer_controller
        finally:
            product_runner.PreExtractionSubprocessRunner = original_context_runner
            product_runner.ExtractionSubprocessRunner = original_extraction_runner
            app_module.WindowsOriginProcessController = original_controller
            app_module.default_origin_process_probe = original_probe
            app_module.create_production_main_window = original_create
            if original_pyside is None:
                sys.modules.pop("PySide6", None)
            else:
                sys.modules["PySide6"] = original_pyside

        self.assertEqual(0, result)
        self.assertEqual([(100, "desktop", created[0][2])], created)
        self.assertEqual({"local_appdata": None, "protected_paths": ()}, captured)
        self.assertIs(fake_context_builder, controller.pre_extraction_context_builder)
        self.assertIs(fake_extraction_runner, controller.extraction_runner)
        self.assertEqual({}, extraction_runner_kwargs)
        self.assertIs(sentinel_probe, process_controllers[0].kwargs["process_probe"])
        self.assertTrue(hasattr(controller.start_run_runner, "run_func"))
        runtime_updates = []
        controller._runtime_update = lambda **kwargs: runtime_updates.append(kwargs)
        with self.assertRaisesRegex(product_runner.ProductRunnerError, "4242"):
            controller.start_run_runner.run_func.pre_origin_process_check()
        self.assertEqual("manual", runtime_updates[-1]["activity_mode"])
        self.assertEqual(
            "等待关闭 Origin 后重新检测",
            runtime_updates[-1]["runtime_status"],
        )
        self.assertIs(
            controller.candidate_loader,
            controller.start_run_runner.run_func.candidate_loader,
        )

    def _controller(
        self,
        *,
        source_paths=(),
        output_parent="C:/Out",
        pre_extraction_context_builder=None,
        extraction_runner=None,
        start_run_runner=None,
        task8_runner=None,
        output_stage_runner=None,
        manual_dialog_port=None,
        attribution_dialog_port=None,
        conflict_review_dialog_port=None,
        candidate_loader=None,
        schedule_call=None,
        extraction_activity_timer=None,
        monotonic_clock=None,
        open_path=None,
        initial_output_parent="",
    ):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        settings_path = pathlib.Path(temp_dir.name) / "settings.json"
        store = SettingsStore(settings_path)
        widgets = {
            "run_log": FakeRunLog(),
            "output_path_label": FakeLabel(),
            "preflight_settings_summary_label": FakeLabel(),
            "select_sources_button": FakeButton("选择 Origin 原始文件"),
            "select_output_parent_button": FakeButton("选择输出位置"),
            "start_run_button": FakeButton("开始任务"),
        }
        message_box = FakeMessageBox()
        preflight_dialog = FakePreflightDialog()
        if pre_extraction_context_builder is None:
            pre_extraction_context_builder = RecordingPreExtractionContextBuilder(result={"run_id": "test-run"})
        controller_kwargs = {
            "parent": None,
            "widgets": widgets,
            "orchestrator": BookOnlyOrchestrator(store),
            "file_dialogs": FakeFileDialogs(source_paths=source_paths, output_parent=output_parent),
            "message_box": message_box,
            "preflight_dialog": preflight_dialog,
            "pre_extraction_context_builder": pre_extraction_context_builder,
            "extraction_runner": extraction_runner or RecordingExtractionRunner(),
            "initial_output_parent": initial_output_parent,
        }
        if start_run_runner is not None:
            controller_kwargs["start_run_runner"] = start_run_runner
        if task8_runner is not None:
            controller_kwargs["task8_runner"] = task8_runner
        if output_stage_runner is not None:
            controller_kwargs["output_stage_runner"] = output_stage_runner
        controller_kwargs["manual_dialog_port"] = manual_dialog_port or FakeManualDialogPort("取消并退出")
        if attribution_dialog_port is not None:
            controller_kwargs["attribution_dialog_port"] = attribution_dialog_port
        controller_kwargs["conflict_review_dialog_port"] = (
            conflict_review_dialog_port or FakeConflictReviewDialogPort()
        )
        if candidate_loader is not None:
            controller_kwargs["candidate_loader"] = candidate_loader
        if schedule_call is not None:
            controller_kwargs["schedule_call"] = schedule_call
        if extraction_activity_timer is not None:
            controller_kwargs["extraction_activity_timer"] = extraction_activity_timer
        if monotonic_clock is not None:
            controller_kwargs["monotonic_clock"] = monotonic_clock
        if open_path is not None:
            controller_kwargs["open_path"] = open_path
        controller = FullRunUiController(**controller_kwargs)
        return controller, settings_path, widgets, message_box, preflight_dialog

    def test_source_dialog_selection_becomes_run_input_and_logs_duplicates(self):
        controller, _, widgets, message_box, _ = self._controller(
            source_paths=("C:/raw/a.opju", "C:/raw/sub/../a.opju", "C:/raw/b.OPJ")
        )

        result = controller.choose_source_files()

        self.assertTrue(result.ok)
        self.assertEqual(("C:/raw/a.opju", "C:/raw/b.OPJ"), controller.selected_source_paths)
        self.assertEqual(1, controller.file_dialogs.source_calls)
        self.assertEqual("已选择 2 个原始文件 · 重新选择", widgets["select_sources_button"].text())
        self.assertTrue(widgets["select_sources_button"].property("selection_confirmed"))
        self.assertFalse(widgets["start_run_button"].visible)
        log_text = widgets["run_log"].toPlainText()
        self.assertIn("已选择输入文件：a.opju", log_text)
        self.assertIn("已选择输入文件：b.OPJ", log_text)
        self.assertIn("已忽略重复文件：C:/raw/sub/../a.opju", widgets["run_log"].toPlainText())
        self.assertEqual([], message_box.errors)

    def test_run_log_scrolls_to_latest_entry_after_each_real_flow_update(self):
        controller, _, widgets, _, _ = self._controller(source_paths=("C:/raw/a.opju",), output_parent="C:/Out")

        controller.choose_source_files()
        controller.choose_output_parent()
        controller.apply_confirmed_preflight_settings(s1_limit=42, steady_emission_y="S1c")

        scrollbar = widgets["run_log"].verticalScrollBar()
        self.assertEqual(len(widgets["run_log"].lines), len(scrollbar.values))
        self.assertEqual(scrollbar.maximum(), scrollbar.values[-1])
        self.assertIn("预检设置已确认", widgets["run_log"].toPlainText())
        for line in widgets["run_log"].lines:
            self.assertRegex(line, r"^\d{2}:\d{2}:\d{2}  \S")

    def test_output_selection_updates_button_to_confirmed_change_state(self):
        controller, _, widgets, _, _ = self._controller(output_parent="C:/Organized")

        selected = controller.choose_output_parent()

        self.assertEqual("C:/Organized", selected)
        self.assertEqual("输出位置已选择 · 更改", widgets["select_output_parent_button"].text())
        self.assertTrue(widgets["select_output_parent_button"].property("selection_confirmed"))
        self.assertFalse(widgets["start_run_button"].visible)

    def test_valid_remembered_output_parent_only_seeds_chooser_and_is_not_confirmed(self):
        with tempfile.TemporaryDirectory() as directory:
            controller, _, widgets, _, _ = self._controller(initial_output_parent=directory)

            self.assertEqual("", controller.output_parent)
            self.assertNotIn("output_parent", controller.orchestrator.task_cache)
            self.assertEqual("选择输出位置", widgets["select_output_parent_button"].text())
            self.assertFalse(widgets["select_output_parent_button"].property("selection_confirmed"))
            self.assertEqual("", widgets["output_path_label"].text())
            self.assertEqual(directory, controller.file_dialogs.initial_output_parent)

    def test_unavailable_remembered_output_parent_is_cleared_and_warned(self):
        with tempfile.TemporaryDirectory() as directory:
            settings_path = pathlib.Path(directory) / "settings.json"
            store = SettingsStore(settings_path)
            missing = str(pathlib.Path(directory) / "missing-output")
            store.set_last_output_parent(missing)
            settings, _ = store.load()

            restored, notices = app_module._validated_remembered_output_parent(settings, store)

            self.assertEqual("", restored)
            self.assertTrue(any("上次输出位置不可用" in notice.message for notice in notices))
            persisted = json.loads(settings_path.read_text(encoding="utf-8"))
            self.assertEqual("", persisted["lastOutputParent"])

    def test_window_settings_are_loaded_once_when_no_startup_result_is_supplied(self):
        class CountingStore:
            def __init__(self):
                self.calls = 0

            def load(self):
                self.calls += 1
                return app_module.Settings(), []

        store = CountingStore()

        settings, notices = app_module._load_window_settings(store, None)

        self.assertEqual(1, store.calls)
        self.assertEqual(2_000_000, settings.s1Limit)
        self.assertEqual([], notices)

    def test_legacy_cleanup_warning_is_not_exposed_as_a_startup_notice(self):
        startup_result = types.SimpleNamespace(
            settings=app_module.Settings(),
            notices=[],
            warnings="legacy retained-temp diagnostic",
        )

        settings, notices = app_module._load_window_settings(object(), startup_result)

        self.assertEqual(2_000_000, settings.s1Limit)
        self.assertEqual([], notices)

    def test_startup_notices_are_delivered_only_after_main_window_is_visible(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6 import QtWidgets

        if QtWidgets.QApplication.instance() is None:
            QtWidgets.QApplication([])
        events = []

        class FakeWindow:
            def show(self):
                events.append("show")

        with tempfile.TemporaryDirectory() as directory:
            store = SettingsStore(pathlib.Path(directory) / "settings.json")
            startup_result = types.SimpleNamespace(
                settings=app_module.Settings(),
                settings_store=store,
                notices=[],
                warnings="startup warning",
                paths=None,
            )
            with mock.patch.object(
                app_module,
                "create_production_main_window",
                return_value=(FakeWindow(), {}),
            ), mock.patch.object(
                app_module,
                "_deliver_startup_notices",
                side_effect=lambda *args: events.append("notices"),
            ):
                app_module.run_main_window(
                    startup_result=startup_result,
                    settings_store=store,
                    file_dialogs=object(),
                    message_box=object(),
                    preflight_dialog=object(),
                )

        self.assertEqual(["show", "notices"], events)

    def test_run_main_window_restores_and_activates_on_second_launch(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6 import QtTest, QtWidgets

        QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        events = []
        probe_calls = []

        class TrackingWindow(QtWidgets.QWidget):
            def show(self):
                events.append("show")
                super().show()

            def isMinimized(self):
                return True

            def showNormal(self):
                events.append("restore")

            def raise_(self):
                events.append("raise")

            def activateWindow(self):
                events.append("activate")

        def activation_request_probe(*, timeout_ms):
            probe_calls.append(timeout_ms)
            return len(probe_calls) == 1

        with tempfile.TemporaryDirectory() as directory:
            store = SettingsStore(pathlib.Path(directory) / "settings.json")
            startup_result = types.SimpleNamespace(
                settings=app_module.Settings(),
                settings_store=store,
                notices=[],
                paths=None,
                activation_request_probe=activation_request_probe,
            )
            window = TrackingWindow()
            with mock.patch.object(
                app_module,
                "create_production_main_window",
                return_value=(window, {}),
            ):
                result = app_module.run_main_window(
                    startup_result=startup_result,
                    settings_store=store,
                    file_dialogs=object(),
                    message_box=object(),
                    preflight_dialog=object(),
                )
                QtTest.QTest.qWait(250)

        self.assertEqual(0, result)
        self.assertEqual(0, probe_calls[0])
        self.assertEqual(
            ["show", "restore", "raise", "activate"],
            events,
        )
        window.close()

    def test_activation_request_targets_active_modal_and_native_hwnd(self):
        events = []

        class Widget:
            def __init__(
                self,
                name,
                *,
                minimized=False,
                visible=True,
                hwnd=0,
            ):
                self.name = name
                self.minimized = minimized
                self.visible = visible
                self.hwnd = hwnd

            def isMinimized(self):
                return self.minimized

            def isVisible(self):
                return self.visible

            def showNormal(self):
                events.append((self.name, "restore"))

            def show(self):
                events.append((self.name, "show"))

            def raise_(self):
                events.append((self.name, "raise"))

            def activateWindow(self):
                events.append((self.name, "activate"))

            def winId(self):
                return self.hwnd

        main = Widget("main", minimized=True, hwnd=100)
        modal = Widget("modal", hwnd=200)
        qt_widgets = types.SimpleNamespace(
            QApplication=types.SimpleNamespace(
                activeModalWidget=lambda: modal,
                activeWindow=lambda: main,
            )
        )

        with mock.patch.object(
            app_module,
            "_bring_native_window_to_foreground",
            return_value=True,
        ) as native:
            target = app_module._activate_requested_window(
                main,
                qt_widgets,
            )

        self.assertIs(modal, target)
        self.assertEqual(
            [
                ("main", "restore"),
                ("modal", "raise"),
                ("modal", "activate"),
            ],
            events,
        )
        native.assert_called_once_with(200)

    @unittest.skipUnless(
        sys.platform == "win32",
        "requires native Windows foreground APIs",
    )
    def test_native_foreground_helper_preserves_current_foreground_hwnd(self):
        import ctypes

        hwnd = int(ctypes.windll.user32.GetForegroundWindow())
        if not hwnd:
            self.skipTest("no current foreground HWND")

        activated = app_module._bring_native_window_to_foreground(
            hwnd
        )

        self.assertTrue(activated)
        self.assertEqual(
            hwnd,
            int(ctypes.windll.user32.GetForegroundWindow()),
        )

    def test_run_main_window_closes_after_sample_library_recovery_is_cancelled(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6 import QtWidgets

        if QtWidgets.QApplication.instance() is None:
            QtWidgets.QApplication([])
        events = []

        class FakeWindow:
            def show(self):
                events.append("show")

            def close(self):
                events.append("close")

        sample_library = types.SimpleNamespace(
            check_health=lambda: types.SimpleNamespace(healthy=False, status="corrupt"),
            recover=lambda: self.fail("cancelled recovery must not replace the database"),
        )
        dialog = FakeManualDialogPort("cancel")
        with tempfile.TemporaryDirectory() as directory:
            store = SettingsStore(pathlib.Path(directory) / "settings.json")
            with mock.patch.object(
                app_module,
                "create_production_main_window",
                return_value=(FakeWindow(), {}),
            ):
                result = app_module.run_main_window(
                    settings_store=store,
                    file_dialogs=object(),
                    message_box=object(),
                    preflight_dialog=object(),
                    manual_dialog_port=dialog,
                    sample_library=sample_library,
                )

        self.assertEqual(0, result)
        self.assertEqual(["show", "close"], events)
        self.assertEqual(["database_recovery"], [request.kind for request in dialog.requests])

    def test_run_main_window_default_library_uses_owned_application_temp_root(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6 import QtWidgets

        if QtWidgets.QApplication.instance() is None:
            QtWidgets.QApplication([])
        created = []

        class FakeWindow:
            def show(self):
                pass

            def isVisible(self):
                return True

        def make_library(*args, **kwargs):
            created.append((args, kwargs))
            return types.SimpleNamespace(
                check_health=lambda **_kwargs: types.SimpleNamespace(
                    healthy=True,
                    status="absent",
                    exists=False,
                )
            )

        with tempfile.TemporaryDirectory() as directory:
            app_paths = app_module.ensure_app_paths(pathlib.Path(directory) / "localappdata")
            store = SettingsStore(app_paths.settings_file)
            startup_result = types.SimpleNamespace(
                settings=app_module.Settings(),
                settings_store=store,
                notices=[],
                paths=app_paths,
            )
            with mock.patch.object(
                app_module,
                "create_production_main_window",
                return_value=(FakeWindow(), {}),
            ), mock.patch.object(app_module, "SampleLibrary", side_effect=make_library):
                result = app_module.run_main_window(
                    startup_result=startup_result,
                    settings_store=store,
                    file_dialogs=object(),
                    message_box=object(),
                    preflight_dialog=object(),
                )

        self.assertEqual(0, result)
        self.assertEqual(app_paths.temp, created[0][1]["health_temp_root"])

    def test_run_main_window_closes_when_startup_health_check_is_cancelled(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6 import QtWidgets

        if QtWidgets.QApplication.instance() is None:
            QtWidgets.QApplication([])
        events = []

        class FakeWindow:
            def show(self):
                events.append("show")

            def isVisible(self):
                return False

            def close(self):
                events.append("close")

        def check_health(*, cancel_check=None):
            cancel_check()

        with tempfile.TemporaryDirectory() as directory:
            store = SettingsStore(pathlib.Path(directory) / "settings.json")
            with mock.patch.object(
                app_module,
                "create_production_main_window",
                return_value=(FakeWindow(), {}),
            ):
                result = app_module.run_main_window(
                    settings_store=store,
                    file_dialogs=object(),
                    message_box=object(),
                    preflight_dialog=object(),
                    sample_library=types.SimpleNamespace(check_health=check_health),
                )

        self.assertEqual(0, result)
        self.assertEqual(["show", "close"], events)

    def test_run_main_window_blocks_queued_source_selection_until_health_gate_succeeds(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6 import QtCore, QtWidgets

        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        window = QtWidgets.QWidget()
        widgets = {
            "select_sources_button": QtWidgets.QPushButton("选择 Origin 原始文件"),
            "select_output_parent_button": QtWidgets.QPushButton("选择输出位置"),
            "start_run_button": QtWidgets.QPushButton("开始任务"),
        }
        file_dialogs = FakeFileDialogs(source_paths=("C:/raw.opj",), output_parent="C:/Out")

        gui_thread_id = threading.get_ident()
        health_started = threading.Event()
        release_health = threading.Event()
        health_finished = threading.Event()
        health_thread_ids = []
        gui_tick_during_health = []

        def check_health(*, cancel_check=None):
            health_thread_ids.append(threading.get_ident())
            health_started.set()
            release_health.wait(0.3)
            health_finished.set()
            return types.SimpleNamespace(healthy=True, status="absent", exists=False)

        timer = QtCore.QTimer(window)
        timer.setInterval(1)

        def click_when_health_starts():
            if not health_started.is_set():
                return
            timer.stop()
            gui_tick_during_health.append(not health_finished.is_set())
            widgets["select_sources_button"].click()
            release_health.set()

        timer.timeout.connect(click_when_health_starts)
        timer.start()

        with tempfile.TemporaryDirectory() as directory:
            store = SettingsStore(pathlib.Path(directory) / "settings.json")
            with mock.patch.object(
                app_module,
                "create_production_main_window",
                return_value=(window, widgets),
            ):
                try:
                    result = app_module.run_main_window(
                        settings_store=store,
                        file_dialogs=file_dialogs,
                        message_box=FakeMessageBox(),
                        preflight_dialog=FakePreflightDialog(),
                        sample_library=types.SimpleNamespace(check_health=check_health),
                    )
                finally:
                    timer.stop()

        self.assertEqual(0, result)
        self.assertEqual([True], gui_tick_during_health)
        self.assertEqual(1, len(health_thread_ids))
        self.assertNotEqual(gui_thread_id, health_thread_ids[0])
        self.assertEqual(0, file_dialogs.source_calls)
        self.assertTrue(widgets["select_sources_button"].isEnabled())
        window.close()

    def test_settings_save_warning_reaches_live_log(self):
        controller, settings_path, widgets, _, _ = self._controller(output_parent="D:/Organized")

        with mock.patch("os.replace", side_effect=OSError("locked")):
            controller.choose_output_parent()

        self.assertIn("设置警告", widgets["run_log"].toPlainText())
        self.assertIn("无法保存", widgets["run_log"].toPlainText())

    def test_runtime_settings_damage_is_confirmed_before_replacement(self):
        dialog = FakeManualDialogPort("acknowledge")
        controller, settings_path, widgets, _, _ = self._controller(
            output_parent="D:/Organized",
            manual_dialog_port=dialog,
        )
        settings_path.write_text("{broken", encoding="utf-8")

        controller.choose_output_parent()

        self.assertEqual("settings_reset_notice", dialog.requests[-1].kind)
        persisted = json.loads(settings_path.read_text(encoding="utf-8"))
        self.assertEqual("D:/Organized", persisted["lastOutputParent"])
        self.assertIn("设置文件损坏", widgets["run_log"].toPlainText())

    def test_startup_reset_notice_is_shown_before_damaged_file_is_deleted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "settings.json"
            path.write_text("{bad", encoding="utf-8")
            startup_store = SettingsStore(path)
            _settings, notices = startup_store.load()
            events = []

            class RecordingDialogPort(FakeManualDialogPort):
                def choose(self, request):
                    events.append(("notice", path.exists(), request.kind))
                    return super().choose(request)

            controller, _, widgets, _, _ = self._controller(
                manual_dialog_port=RecordingDialogPort("acknowledge")
            )

            app_module._deliver_startup_notices(controller, startup_store, notices)

            self.assertEqual([("notice", True, "settings_reset_notice")], events)
            self.assertFalse(path.exists())
            self.assertIn("设置文件损坏", widgets["run_log"].toPlainText())

    def test_healthy_sample_library_startup_gate_is_silent(self):
        library = types.SimpleNamespace(
            check_health=lambda: types.SimpleNamespace(healthy=True),
        )
        dialog = FakeManualDialogPort("cancel")

        can_continue, notices = app_module._sample_library_startup_gate(library, dialog)

        self.assertTrue(can_continue)
        self.assertEqual((), notices)
        self.assertEqual([], dialog.requests)

    def test_health_snapshot_cleanup_failure_aborts_without_rebuilding_healthy_library(self):
        from spectrum_organizer.domain.models import LiquidSample
        from spectrum_organizer.store import sample_library as sample_library_module
        from spectrum_organizer.store.sample_library import SampleLibrary

        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            db = root / "sample_library.sqlite3"
            backups = root / "backups"
            library = SampleLibrary(db, backups, clock=lambda: "20260717_070000")
            library.save_final_records([LiquidSample("MFL", "mTHF", "1×10^-4 M", "298 K")])
            real_cleanup = sample_library_module.cleanup_owned_temp_root
            cleanup_calls = 0

            def cleanup_once_then_succeed(run_root, **kwargs):
                nonlocal cleanup_calls
                real_cleanup(run_root, **kwargs)
                cleanup_calls += 1
                if cleanup_calls == 1:
                    raise OSError("simulated cleanup failure")

            dialog = FakeManualDialogPort("backup_new_empty")
            with mock.patch(
                "spectrum_organizer.store.sample_library.cleanup_owned_temp_root",
                side_effect=cleanup_once_then_succeed,
            ):
                can_continue, notices = app_module._sample_library_startup_gate(library, dialog)

            connection = sqlite3.connect(db)
            try:
                record_count = connection.execute("select count(*) from sample_records").fetchone()[0]
            finally:
                connection.close()
            backup_files = tuple(backups.glob("*.sqlite3")) if backups.exists() else ()

            self.assertFalse(can_continue)
            self.assertEqual((), notices)
            self.assertEqual(1, record_count)
            self.assertEqual((), backup_files)
            self.assertEqual(["database_health_check_failed"], [request.kind for request in dialog.requests])

    def test_sample_library_startup_gate_forwards_cancellation_check(self):
        class StartupCancelled(RuntimeError):
            pass

        calls = []

        def cancel_check():
            calls.append(True)
            raise StartupCancelled("startup cancelled")

        def check_health(*, cancel_check=None):
            self.assertIsNotNone(cancel_check)
            cancel_check()

        library = types.SimpleNamespace(check_health=check_health)

        with self.assertRaisesRegex(StartupCancelled, "startup cancelled"):
            app_module._sample_library_startup_gate(
                library,
                FakeManualDialogPort("cancel"),
                cancel_check=cancel_check,
            )

        self.assertEqual([True], calls)

    def test_startup_cancel_check_pumps_events_and_stops_after_window_closes(self):
        events = []
        app = types.SimpleNamespace(processEvents=lambda: events.append("processed"))
        window = types.SimpleNamespace(isVisible=lambda: False)

        with self.assertRaises(app_module._StartupCancelled):
            app_module._pump_startup_events_or_cancel(app, window)

        self.assertEqual(["processed"], events)

    def test_damaged_sample_library_can_cancel_startup_without_recovery(self):
        recovered = []
        library = types.SimpleNamespace(
            path=pathlib.Path("C:/Users/test/AppData/Local/Spectrum Organizer/data/sample_library.sqlite3"),
            backups_dir=pathlib.Path("C:/Users/test/AppData/Local/Spectrum Organizer/data/backups"),
            clock=lambda: "20260716_150200",
            planned_backup_path=lambda: pathlib.Path(
                "C:/Users/test/AppData/Local/Spectrum Organizer/data/backups/"
                "sample_library_20260716_150200.sqlite3"
            ),
            check_health=lambda: types.SimpleNamespace(
                healthy=False,
                status="corrupt",
                detail="database disk image is malformed",
            ),
            recover=lambda: recovered.append(True),
        )
        dialog = FakeManualDialogPort("cancel")

        can_continue, notices = app_module._sample_library_startup_gate(library, dialog)

        self.assertFalse(can_continue)
        self.assertEqual((), notices)
        self.assertEqual([], recovered)
        self.assertEqual("database_recovery", dialog.requests[0].kind)
        self.assertIn("已损坏", dialog.requests[0].message)
        self.assertIn(str(library.path), dialog.requests[0].message)
        self.assertIn("database disk image is malformed", dialog.requests[0].message)
        self.assertIn(
            str(library.backups_dir / "sample_library_20260716_150200.sqlite3"),
            dialog.requests[0].message,
        )

    def test_damaged_sample_library_recovery_returns_backup_notice(self):
        recovery_revisions = []
        health_results = iter(
            (
                types.SimpleNamespace(
                    healthy=False,
                    status="schema-incompatible",
                    revision="approved-revision",
                ),
                types.SimpleNamespace(healthy=True, status="healthy", exists=True),
            )
        )
        library = types.SimpleNamespace(
            check_health=lambda: next(health_results),
            recover=lambda *, expected_revision: (
                recovery_revisions.append(expected_revision),
                pathlib.Path("C:/Backups/sample_library_20260715.sqlite3"),
            )[1],
        )
        dialog = FakeManualDialogPort("backup_new_empty")

        can_continue, notices = app_module._sample_library_startup_gate(library, dialog)

        self.assertTrue(can_continue)
        self.assertEqual(1, len(notices))
        self.assertEqual(["approved-revision"], recovery_revisions)
        self.assertIn("已备份并重建为空库", notices[0].message)
        self.assertIn("结构", dialog.requests[0].message)

    def test_damaged_sample_library_recovery_absent_result_blocks_startup(self):
        health_results = iter(
            (
                types.SimpleNamespace(
                    healthy=False,
                    status="schema-incompatible",
                    revision="approved-revision",
                ),
                types.SimpleNamespace(
                    healthy=True,
                    status="absent",
                    exists=False,
                    detail=None,
                ),
            )
        )
        library = types.SimpleNamespace(
            check_health=lambda: next(health_results),
            recover=lambda *, expected_revision: pathlib.Path("C:/Backups/sample_library.sqlite3"),
        )

        class RecoveryDialog(FakeManualDialogPort):
            def choose(self, request):
                self.requests.append(request)
                if request.kind == "database_recovery":
                    return types.SimpleNamespace(action="backup_new_empty")
                return types.SimpleNamespace(action="acknowledge")

        dialog = RecoveryDialog("backup_new_empty")

        can_continue, notices = app_module._sample_library_startup_gate(library, dialog)

        self.assertFalse(can_continue)
        self.assertEqual((), notices)
        self.assertEqual(
            ["database_recovery", "database_recovery_failed"],
            [request.kind for request in dialog.requests],
        )

    def test_damaged_sample_library_fresh_absent_result_blocks_startup_before_recovery(self):
        health_results = iter(
            (
                types.SimpleNamespace(
                    healthy=False,
                    status="locked",
                    revision=None,
                ),
                types.SimpleNamespace(
                    healthy=True,
                    status="absent",
                    exists=False,
                    revision=None,
                ),
            )
        )
        recovered = []
        library = types.SimpleNamespace(
            check_health=lambda: next(health_results),
            recover=lambda **kwargs: recovered.append(kwargs),
        )
        dialog = FakeManualDialogPort("backup_new_empty")

        can_continue, notices = app_module._sample_library_startup_gate(library, dialog)

        self.assertFalse(can_continue)
        self.assertEqual((), notices)
        self.assertEqual([], recovered)
        self.assertEqual(
            ["database_recovery", "database_recovery_failed"],
            [request.kind for request in dialog.requests],
        )

    def test_sample_library_recovery_still_unhealthy_blocks_startup(self):
        recovery_revisions = []
        library = types.SimpleNamespace(
            check_health=lambda: types.SimpleNamespace(
                healthy=False,
                status="schema-incompatible",
                revision="approved-revision",
            ),
            recover=lambda *, expected_revision: (
                recovery_revisions.append(expected_revision),
                pathlib.Path("C:/Backups/sample_library_20260715.sqlite3"),
            )[1],
        )

        class RecoveryDialog(FakeManualDialogPort):
            def choose(self, request):
                self.requests.append(request)
                if request.kind == "database_recovery":
                    return types.SimpleNamespace(action="backup_new_empty")
                return types.SimpleNamespace(action="acknowledge")

        dialog = RecoveryDialog("backup_new_empty")

        can_continue, notices = app_module._sample_library_startup_gate(library, dialog)

        self.assertFalse(can_continue)
        self.assertEqual((), notices)
        self.assertEqual(["approved-revision"], recovery_revisions)
        self.assertEqual(
            ["database_recovery", "database_recovery_failed"],
            [request.kind for request in dialog.requests],
        )

    def test_sample_library_recovery_failure_blocks_startup(self):
        recovery_revisions = []

        def fail_recovery(*, expected_revision):
            recovery_revisions.append(expected_revision)
            raise app_module.SampleLibraryError("backup blocked")

        library = types.SimpleNamespace(
            check_health=lambda: types.SimpleNamespace(
                healthy=False,
                status="locked",
                revision="approved-revision",
            ),
            recover=fail_recovery,
        )

        class RecoveryDialog(FakeManualDialogPort):
            def choose(self, request):
                self.requests.append(request)
                if request.kind == "database_recovery":
                    return types.SimpleNamespace(action="backup_new_empty")
                return types.SimpleNamespace(action="acknowledge")

        dialog = RecoveryDialog("backup_new_empty")

        can_continue, notices = app_module._sample_library_startup_gate(library, dialog)

        self.assertFalse(can_continue)
        self.assertEqual((), notices)
        self.assertEqual(["approved-revision"], recovery_revisions)
        self.assertEqual(
            ["database_recovery", "database_recovery_failed"],
            [request.kind for request in dialog.requests],
        )

    def test_sample_library_recovery_oserror_uses_standard_failure_dialog(self):
        def fail_recovery(*, expected_revision):
            raise PermissionError("database changed during recovery")

        library = types.SimpleNamespace(
            check_health=lambda: types.SimpleNamespace(
                healthy=False,
                status="unreadable",
                revision="approved-revision",
            ),
            recover=fail_recovery,
        )

        class RecoveryDialog(FakeManualDialogPort):
            def choose(self, request):
                self.requests.append(request)
                if request.kind == "database_recovery":
                    return types.SimpleNamespace(action="backup_new_empty")
                return types.SimpleNamespace(action="acknowledge")

        dialog = RecoveryDialog("backup_new_empty")

        can_continue, notices = app_module._sample_library_startup_gate(library, dialog)

        self.assertFalse(can_continue)
        self.assertEqual((), notices)
        self.assertEqual(
            ["database_recovery", "database_recovery_failed"],
            [request.kind for request in dialog.requests],
        )
        self.assertIn("database changed during recovery", dialog.requests[-1].message)

    def test_locked_library_requires_fresh_revision_and_confirmation_before_recovery(self):
        recovery_revisions = []
        health_results = iter(
            (
                types.SimpleNamespace(healthy=False, status="locked", revision=None),
                types.SimpleNamespace(
                    healthy=False,
                    status="schema-incompatible",
                    revision="fresh-revision",
                ),
                types.SimpleNamespace(
                    healthy=True,
                    status="healthy",
                    exists=True,
                    revision="rebuilt-revision",
                ),
            )
        )
        library = types.SimpleNamespace(
            check_health=lambda: next(health_results),
            recover=lambda *, expected_revision: (
                recovery_revisions.append(expected_revision),
                pathlib.Path("C:/Backups/sample_library_20260716.sqlite3"),
            )[1],
        )
        dialog = FakeManualDialogPort("backup_new_empty")

        can_continue, notices = app_module._sample_library_startup_gate(library, dialog)

        self.assertTrue(can_continue)
        self.assertEqual(["fresh-revision"], recovery_revisions)
        self.assertEqual(
            ["database_recovery", "database_recovery"],
            [request.kind for request in dialog.requests],
        )
        self.assertEqual(1, len(notices))

    def test_start_button_appears_only_after_sources_and_output_are_selected(self):
        controller, _, widgets, _, _ = self._controller(
            source_paths=("C:/raw/a.opju",),
            output_parent="C:/Organized",
        )

        self.assertFalse(widgets["start_run_button"].visible)
        controller.choose_source_files()
        self.assertFalse(widgets["start_run_button"].visible)
        controller.choose_output_parent()

        self.assertTrue(widgets["start_run_button"].visible)

    def test_input_selection_copy_progresses_to_ready_state(self):
        controller, _, widgets, _, _ = self._controller(
            source_paths=("C:/raw/a.opju", "C:/raw/b.opj"),
            output_parent="C:/Organized",
        )
        widgets["app_run_status"] = FakeLabel()
        widgets["current_task_title"] = FakeLabel()
        widgets["current_task_subtitle"] = FakeLabel()

        controller.choose_source_files()

        self.assertEqual("等待选择输出位置", widgets["app_run_status"].text())
        self.assertEqual("选择输出位置", widgets["current_task_title"].text())
        self.assertIn("已选择 2 个原始文件", widgets["current_task_subtitle"].text())

        controller.choose_output_parent()

        self.assertEqual("输入已就绪", widgets["app_run_status"].text())
        self.assertEqual("准备开始任务", widgets["current_task_title"].text())
        self.assertEqual(
            "原始文件和输出位置已确认，开始前将确认预检设置。",
            widgets["current_task_subtitle"].text(),
        )

    def test_start_button_stays_hidden_when_runtime_stage_hides_input_controls(self):
        controller, _, widgets, _, _ = self._controller(
            source_paths=("C:/raw/a.opju",),
            output_parent="C:/Organized",
        )
        controller.choose_source_files()
        controller.choose_output_parent()
        self.assertTrue(widgets["start_run_button"].visible)

        with mock.patch.object(app_module, "update_production_runtime_view"):
            controller.update_runtime_view(show_input_controls=False)
            self.assertFalse(widgets["start_run_button"].visible)

            controller.update_runtime_view(show_input_controls=True)
            self.assertTrue(widgets["start_run_button"].visible)

    def test_invalid_source_selection_blocks_and_keeps_previous_valid_input(self):
        controller, _, widgets, message_box, preflight_dialog = self._controller(source_paths=("C:/raw/a.opju",))
        controller.choose_source_files()
        controller.file_dialogs.source_paths = ["C:/raw/readme.txt"]

        result = controller.choose_source_files()

        self.assertFalse(result.ok)
        self.assertEqual(2, controller.file_dialogs.source_calls)
        self.assertEqual(("C:/raw/a.opju",), controller.selected_source_paths)
        self.assertEqual("unrecognized_source_file", result.reason)
        self.assertEqual(1, len(message_box.errors))
        self.assertIn("unrecognized_source_file", message_box.errors[0][1])
        self.assertIn("源文件选择失败", widgets["run_log"].toPlainText())
        self.assertFalse(controller.request_start_run())
        self.assertEqual([], preflight_dialog.calls)

    def test_empty_source_selection_returns_to_source_selection_without_error_dialog(self):
        controller, _, widgets, message_box, _ = self._controller(source_paths=())

        result = controller.choose_source_files()

        self.assertFalse(result.ok)
        self.assertEqual("no_source_files", result.reason)
        self.assertEqual((), controller.selected_source_paths)
        self.assertEqual([], message_box.errors)
        self.assertIn("未选择输入文件", widgets["run_log"].toPlainText())

    def test_output_parent_dialog_persists_immediately_and_updates_label(self):
        controller, settings_path, widgets, _, _ = self._controller(output_parent="D:/Organized")

        controller.choose_output_parent()
        controller.cancel_after_preferences()

        self.assertEqual(1, controller.file_dialogs.output_calls)
        self.assertEqual("D:/Organized", controller.output_parent)
        self.assertEqual("输出位置：D:/Organized", widgets["output_path_label"].text())
        self.assertIn('"lastOutputParent": "D:/Organized"', settings_path.read_text(encoding="utf-8"))

    def test_cancel_task_closes_main_window_after_recording_cancel(self):
        controller, _, widgets, _, _ = self._controller()

        class FakeParent:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        parent = FakeParent()
        controller.parent = parent

        controller.cancel_after_preferences()

        self.assertTrue(parent.closed)
        self.assertTrue(controller.orchestrator.cancelled)
        self.assertIn("任务已取消", widgets["run_log"].toPlainText())
        self.assertEqual("cancel_and_exit_confirmation", controller.manual_dialog_port.requests[0].kind)

    def test_cancel_task_continue_keeps_main_window_open_and_run_uncancelled(self):
        manual_dialog_port = FakeManualDialogPort("继续运行")
        controller, _, widgets, _, _ = self._controller(manual_dialog_port=manual_dialog_port)

        class FakeParent:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        parent = FakeParent()
        controller.parent = parent

        controller.cancel_after_preferences()

        self.assertFalse(parent.closed)
        self.assertFalse(controller.orchestrator.cancelled)
        self.assertIn("已继续运行", widgets["run_log"].toPlainText())
        self.assertEqual("cancel_and_exit_confirmation", manual_dialog_port.requests[0].kind)

    def test_cancel_confirmation_pauses_automatic_motion_and_restores_it_after_continue(self):
        runtime_updates = []
        observed_inside_confirmation = []

        class FakeProgress:
            def minimum(self):
                return 0

            def maximum(self):
                return 0

            def value(self):
                return -1

        class InspectingManualDialogPort:
            def choose(self, _request):
                from spectrum_organizer.ui.dialog_port import DialogResponse

                observed_inside_confirmation.append(runtime_updates[-1])
                return DialogResponse(action="继续运行")

        controller, _, widgets, _, _ = self._controller(
            manual_dialog_port=InspectingManualDialogPort(),
        )
        widgets.update(
            {
                "app_run_status": FakeLabel(),
                "run_progress": FakeProgress(),
                "runtime_activity_mode": "automatic",
            }
        )
        widgets["app_run_status"].setText("正在读取 2/3 个源文件")
        controller.update_runtime_view = lambda **kwargs: runtime_updates.append(kwargs)

        controller.cancel_after_preferences()

        self.assertEqual(
            {
                "runtime_status": "等待确认是否取消任务",
                "activity_mode": "manual",
                "progress": 0,
                "progress_busy": False,
            },
            observed_inside_confirmation[0],
        )
        self.assertEqual(
            {
                "runtime_status": "正在读取 2/3 个源文件",
                "activity_mode": "automatic",
                "progress_busy": True,
            },
            runtime_updates[-1],
        )

    def test_confirmed_preflight_dialog_values_persist_immediately(self):
        controller, settings_path, widgets, _, _ = self._controller()

        controller.apply_confirmed_preflight_settings(
            s1_limit=42,
            steady_emission_y="S1c/R1c",
            allow_missing_s1=True,
        )
        controller.cancel_after_preferences()

        stored = settings_path.read_text(encoding="utf-8")
        self.assertIn('"s1Limit": 42', stored)
        self.assertIn('"steadyEmissionY": "S1c/R1c"', stored)
        self.assertIn('"allowMissingS1": true', stored)
        self.assertEqual(
            "预检设置：S1 强度上限 42，发射谱 Y 列 S1c/R1c，缺少 S1 时继续 是",
            widgets["preflight_settings_summary_label"].text(),
        )

    def test_start_run_uses_combined_preflight_dialog_and_runs_extraction_from_gui(self):
        extraction_runner = RecordingExtractionRunner()
        controller, settings_path, widgets, message_box, preflight_dialog = self._controller(
            source_paths=("C:/raw/a.opju",),
            output_parent="D:/Organized",
            extraction_runner=extraction_runner,
        )
        controller.choose_source_files()
        controller.choose_output_parent()

        self.assertTrue(controller.request_start_run())

        self.assertEqual([(2000000, "S1c", False)], preflight_dialog.calls)
        self.assertTrue(controller.run_ready)
        self.assertEqual([{"run_id": "test-run"}], extraction_runner.calls)
        self.assertEqual(extraction_runner.result, controller.orchestrator.task_cache["extraction_summary"])
        self.assertEqual([], message_box.errors)
        self.assertIn('"s1Limit": 42', settings_path.read_text(encoding="utf-8"))
        log_text = widgets["run_log"].toPlainText()
        self.assertIn("已完成提取前安全检查，开始读取谱图数据", log_text)
        self.assertIn("谱图数据提取完成：检测到 2 个 Book，已提取 1 条，排除 1 条", log_text)

    def test_start_run_keeps_legacy_preflight_dialog_signature_compatible(self):
        class LegacyPreflightDialog:
            def __init__(self):
                self.calls = []

            def confirm(self, parent, *, default_s1_limit, steady_emission_y):
                self.calls.append((default_s1_limit, steady_emission_y))
                return {"s1_limit": 42, "steady_emission_y": "S1c"}

        controller, settings_path, _, message_box, _ = self._controller(
            source_paths=("C:/raw/a.opju",),
            output_parent="D:/Organized",
        )
        controller.default_allow_missing_s1 = True
        legacy_dialog = LegacyPreflightDialog()
        controller.preflight_dialog = legacy_dialog
        controller.choose_source_files()
        controller.choose_output_parent()

        self.assertTrue(controller.request_start_run())

        self.assertEqual([(2000000, "S1c")], legacy_dialog.calls)
        self.assertTrue(controller.default_allow_missing_s1)
        self.assertIn('"allowMissingS1": true', settings_path.read_text(encoding="utf-8"))
        self.assertEqual([], message_box.errors)

    def test_start_run_with_async_job_runner_does_not_call_pre_context_builder_synchronously(self):
        builder = RecordingPreExtractionContextBuilder(error=AssertionError("builder must run in background job"))
        start_run_runner = RecordingAsyncStartRunRunner()
        controller, _, widgets, message_box, _ = self._controller(
            source_paths=("C:/raw/a.opju",),
            output_parent="D:/Organized",
            pre_extraction_context_builder=builder,
            start_run_runner=start_run_runner,
        )
        controller.choose_source_files()
        controller.choose_output_parent()

        self.assertTrue(controller.request_start_run())

        self.assertEqual([], builder.calls)
        self.assertEqual(1, len(start_run_runner.calls))
        approved_inputs = start_run_runner.calls[0]
        self.assertEqual(("C:/raw/a.opju",), approved_inputs.selected_source_paths)
        self.assertEqual("D:/Organized", approved_inputs.output_parent)
        self.assertFalse(controller.run_ready)
        self.assertEqual([], message_box.errors)
        self.assertIn("开始提取前安全检查", widgets["run_log"].toPlainText())
        self.assertNotIn("已完成提取前安全检查", widgets["run_log"].toPlainText())

        start_run_runner.progress({"kind": "pre_extraction_completed"})
        self.assertIn("已完成提取前安全检查，开始读取谱图数据", widgets["run_log"].toPlainText())

        start_run_runner.succeed()
        self.assertTrue(controller.run_ready)
        self.assertEqual(start_run_runner.context, controller.orchestrator.task_cache["approved_pre_extraction_context"])
        self.assertEqual(start_run_runner.summary, controller.orchestrator.task_cache["extraction_summary"])

    def test_extraction_running_view_uses_real_selected_sources_and_runtime_stage(self):
        start_run_runner = RecordingAsyncStartRunRunner()
        controller, _, widgets, _, _ = self._controller(
            source_paths=("C:/raw/first.opju", "C:/raw/second.opj"),
            output_parent="D:/Organized",
            start_run_runner=start_run_runner,
        )
        widgets["app_run_status"] = FakeLabel()
        runtime_updates = []
        controller.update_runtime_view = lambda **kwargs: runtime_updates.append(kwargs)
        controller.choose_source_files()
        controller.choose_output_parent()

        self.assertTrue(controller.request_start_run())
        start_run_runner.progress({"kind": "pre_extraction_completed"})

        running = runtime_updates[-1]
        self.assertEqual("source_input", running["stage"])
        self.assertEqual("读取中", running["phase_detail"])
        self.assertEqual("automatic", running["activity_mode"])
        self.assertEqual(("0", "0", "0", "0"), running["summary_numbers"])
        self.assertEqual(("来源文件", "检测到的 Book", "处理状态"), running["review_headers"])
        self.assertEqual(
            (
                ("first.opju", "等待统计", "等待读取"),
                ("second.opj", "等待统计", "等待读取"),
            ),
            running["review_rows"],
        )

    def test_extraction_finished_view_hides_internal_source_ids_and_advances_to_attribution(self):
        summary = {
            "total_inventory_count": 158,
            "total_extracted_count": 157,
            "total_rejected_count": 1,
            "source_summaries": (
                {
                    "source_id": "S0001",
                    "original_path": "C:/raw/20241209_MFL_2DPho.opj",
                    "inventory_count": 158,
                    "extracted_count": 157,
                    "rejected_count": 1,
                },
            ),
        }
        controller, _, widgets, _, _ = self._controller()
        widgets["app_run_status"] = FakeLabel()
        runtime_updates = []
        controller.update_runtime_view = lambda **kwargs: runtime_updates.append(kwargs)

        controller._show_extraction_finished(summary)

        finished = runtime_updates[-1]
        self.assertEqual("attribution", finished["stage"])
        self.assertEqual("等待归属", finished["phase_detail"])
        self.assertEqual("manual", finished["activity_mode"])
        self.assertEqual(("158", "157", "0", "1"), finished["summary_numbers"])
        self.assertEqual(("来源文件", "检测到的 Book", "处理状态"), finished["review_headers"])
        self.assertEqual(
            (("20241209_MFL_2DPho.opj", "158", "已提取 157，排除 1"),),
            finished["review_rows"],
        )
        self.assertNotIn("S0001", repr(finished["review_rows"]))
        self.assertNotIn("Folder", repr(finished))

    def test_extraction_finished_keeps_concise_skipped_row_and_one_structured_guidance_source(self):
        summary = {
            "total_inventory_count": 7,
            "total_extracted_count": 7,
            "total_rejected_count": 0,
            "source_summaries": (
                {
                    "source_id": "S0001",
                    "original_path": "C:/raw/valid.opju",
                    "inventory_count": 7,
                    "extracted_count": 7,
                    "rejected_count": 0,
                },
            ),
            "source_input_issues": (
                {
                    "source_id": "S0002",
                    "original_path": "C:/raw/Paper.opju",
                    "reason": "未检测到受支持的 Origin 原始谱图。",
                    "recommendation": "请重新选择包含原始光谱 Book 的文件。",
                },
            ),
        }
        controller, _, widgets, _, _ = self._controller()
        widgets["app_run_status"] = FakeLabel()
        runtime_updates = []
        controller.update_runtime_view = lambda **kwargs: runtime_updates.append(kwargs)
        controller.orchestrator.task_cache["extraction_summary"] = summary

        controller._show_extraction_finished(summary)
        controller._runtime_update(
            stage="attribution",
            title="确认样品归属",
            show_attention=False,
        )

        finished, later = runtime_updates
        self.assertEqual(("7", "7", "0", "0"), finished["summary_numbers"])
        self.assertEqual(
            (
                ("valid.opju", "7", "已提取 7，排除 0"),
                (
                    "Paper.opju",
                    "—",
                    "已跳过：未检测到受支持的 Origin 原始谱图。",
                ),
            ),
            finished["review_rows"],
        )
        for update in (finished, later):
            self.assertTrue(update["show_attention"])
            issue_message = update["attention_message"]
            self.assertIn("<b>输入文件问题（1）</b>", issue_message)
            self.assertIn("<b>Paper.opju</b>", issue_message)
            self.assertIn("未检测到受支持的 Origin 原始谱图。", issue_message)
            self.assertIn("<br><b>处理建议</b><br>", issue_message)
            self.assertEqual(
                1,
                issue_message.count("请重新选择包含原始光谱 Book 的文件。"),
            )
            self.assertNotIn("S0002", issue_message)

    def test_all_invalid_sources_return_to_input_with_issue_list_and_chinese_blocker(self):
        issues = (
            product_runner.SourceInputIssue(
                source_id="S0001",
                original_path="C:/raw/Paper.opju",
                reason="未检测到受支持的 Origin 原始谱图。",
                recommendation="请重新选择包含原始光谱 Book 的文件。",
            ),
        )
        error = product_runner.AllSelectedSourcesInvalidError(issues)
        controller, _, widgets, message_box, _ = self._controller()
        widgets["app_run_status"] = FakeLabel()
        runtime_updates = []
        controller.update_runtime_view = lambda **kwargs: runtime_updates.append(kwargs)
        controller.run_in_progress = True

        controller._handle_start_run_failure(controller._run_generation, error)

        failed = runtime_updates[-1]
        self.assertTrue(failed["show_input_controls"])
        self.assertTrue(failed["show_attention"])
        self.assertIn("输入文件问题（1）", failed["attention_message"])
        self.assertIn("Paper.opju", failed["attention_message"])
        self.assertEqual(1, len(message_box.errors))
        title, message = message_box.errors[0]
        self.assertEqual("输入文件均无法处理", title)
        self.assertEqual(
            "所选 1 个输入文件均未进入后续流程。\n"
            "请查看右侧“输入文件问题”，处理后重新选择。",
            message,
        )
        self.assertNotIn("Paper.opju", message)
        self.assertNotIn("未检测到受支持", message)
        self.assertNotIn("建议", message)
        self.assertNotIn("S0001", message)
        self.assertTrue(
            widgets["run_log"].toPlainText().splitlines()[-1].endswith(
                "输入文件均无法处理，已返回输入文件选择。"
            )
        )

    def test_extraction_finished_rows_use_neutral_name_when_original_path_is_missing(self):
        summary = {
            "source_summaries": (
                {
                    "source_id": "S0001",
                    "original_path": "",
                    "inventory_count": 3,
                    "extracted_count": 2,
                    "rejected_count": 1,
                },
            ),
        }

        rows = app_module._summary_review_rows(summary)

        self.assertEqual((("来源文件 1", "3", "已提取 2，排除 1"),), rows)
        self.assertNotIn("S0001", repr(rows))

    def test_equal_source_basenames_remain_distinguishable_in_review_and_attribution(self):
        summary = {
            "snapshot_path": "C:/owned/run.sqlite3",
            "snapshot_sha256": "a" * 64,
            "source_summaries": (
                {
                    "source_id": "S1",
                    "original_path": "C:/first/source.opj",
                    "inventory_count": 3,
                    "extracted_count": 3,
                    "rejected_count": 0,
                },
                {
                    "source_id": "S2",
                    "original_path": "D:/second/source.opj",
                    "inventory_count": 4,
                    "extracted_count": 4,
                    "rejected_count": 0,
                },
            ),
        }
        converted = object()

        rows = app_module._summary_review_rows(summary)
        with (
            mock.patch.object(app_module, "load_book_results_read_only", return_value=()),
            mock.patch.object(app_module, "convert_extracted_results", return_value=converted) as convert,
        ):
            result = app_module._load_candidate_conversion(summary)

        self.assertIs(converted, result)
        row_labels = tuple(row[0] for row in rows)
        source_labels = convert.call_args.kwargs["source_filenames"]
        self.assertEqual(2, len(set(row_labels)))
        self.assertEqual(2, len(set(source_labels.values())))
        self.assertIn("C:/first/source.opj", row_labels)
        self.assertIn("D:/second/source.opj", row_labels)
        self.assertEqual(set(row_labels), set(source_labels.values()))

    def test_collision_display_path_does_not_influence_environment_inference(self):
        from spectrum_organizer.core.selection import CandidateConversionResult
        from spectrum_organizer.ui.dialog_port import AttributionDialogResponse

        source_label = "C:/archive/Air/source.opj"
        conversion = CandidateConversionResult(
            (_candidate("S1", source_label, "NDI_77K", "F270", "F270"),),
            (),
            (),
        )
        dialogs = FakeAttributionDialogPort((AttributionDialogResponse(action="cancel"),))
        controller, _, _, _, _ = self._controller(
            attribution_dialog_port=dialogs,
            candidate_loader=lambda _summary: conversion,
        )
        summary = {
            "total_inventory_count": 1,
            "total_extracted_count": 1,
            "total_rejected_count": 0,
            "source_summaries": (),
        }
        controller.orchestrator.task_cache["extraction_summary"] = summary

        controller._begin_attribution(summary, conversion)

        self.assertEqual(source_label, dialogs.requests[0].source_filename)
        self.assertNotIn("oxygen_environment", dialogs.requests[0].prefill)

    def test_attribution_combined_name_error_reopens_same_form_without_losing_task(self):
        from spectrum_organizer.core.selection import CandidateConversionResult
        from spectrum_organizer.ui.dialog_port import (
            AttributionDialogResponse,
        )

        candidate = _candidate(
            "S1",
            "source.opju",
            "Folder",
            "Em270",
            "Emission 270",
        )
        dialogs = FakeAttributionDialogPort(
            (
                AttributionDialogResponse(
                    action="confirm",
                    sample_type="solid",
                    values={
                        "sample": "A" * 130,
                        "state": "B" * 130,
                        "oxygen_environment": "Air",
                        "temperature": "298 K",
                    },
                ),
                AttributionDialogResponse(
                    action="confirm",
                    sample_type="solid",
                    values={
                        "sample": "MFL",
                        "state": "Solid",
                        "oxygen_environment": "Air",
                        "temperature": "298 K",
                    },
                ),
            )
        )
        controller, _, _, message_box, _ = self._controller(
            attribution_dialog_port=dialogs,
        )
        summary = {
            "total_inventory_count": 1,
            "total_extracted_count": 1,
            "total_rejected_count": 0,
            "source_summaries": (),
        }
        controller.orchestrator.task_cache["extraction_summary"] = summary

        controller._begin_attribution(
            summary,
            CandidateConversionResult((candidate,), (), ()),
        )

        self.assertEqual(2, len(dialogs.requests))
        self.assertEqual("A" * 130, dialogs.requests[1].prefill["sample"])
        self.assertEqual("B" * 130, dialogs.requests[1].prefill["state"])
        self.assertEqual("invalid_draft", dialogs.requests[1].prefill_source)
        self.assertIsNone(controller.orchestrator.last_failure)
        self.assertEqual(
            "MFL-Solid-Air-298 K",
            controller.orchestrator.task_cache[
                "attribution_assignments"
            ][candidate.book_key].sample.canonical_label,
        )
        self.assertEqual(
            "样品归属信息无效",
            message_box.errors[0][0],
        )

    def test_extraction_success_runs_task_local_folder_and_root_attribution_without_library_write(self):
        from spectrum_organizer.core.selection import CandidateConversionResult
        from spectrum_organizer.ui.dialog_port import (
            AttributionBookSelectionResponse,
            AttributionDialogResponse,
        )

        candidates = (
            _candidate("S1", "source.opj", "Folder_RT", "F270", "F270"),
            _candidate("S1", "source.opj", "Folder_RT", "Ex315", "Ex315"),
            _candidate("S1", "source.opj", "/", "RootF", "Root F"),
            _candidate("S1", "source.opj", "/", "RootEx", "Root Ex"),
        )
        conversion = CandidateConversionResult(candidates, (), ())
        dialogs = FakeAttributionDialogPort(
            (
                AttributionDialogResponse(
                    action="confirm",
                    sample_type="solid",
                    apply_to_remaining_folder=True,
                    values={
                        "sample": "PFL",
                        "state": "Solid",
                        "oxygen_environment": "Air",
                        "temperature": "RT",
                    },
                ),
                AttributionDialogResponse(
                    action="confirm",
                    sample_type="solid",
                    values={
                        "sample": "MFL",
                        "state": "Solid",
                        "oxygen_environment": "Air",
                        "temperature": "77 K",
                    },
                ),
            ),
            (
                AttributionBookSelectionResponse(
                    action="select_book",
                    book_key=candidates[3].book_key,
                ),
            ),
        )
        controller, _, widgets, _, _ = self._controller(
            attribution_dialog_port=dialogs,
            candidate_loader=lambda summary: conversion,
        )
        controller.selected_source_paths = ("C:/raw/source.opj",)
        summary = {
            "total_inventory_count": 4,
            "total_extracted_count": 4,
            "total_rejected_count": 0,
            "source_summaries": (),
        }
        controller.orchestrator.task_cache["extraction_summary"] = summary

        controller._begin_attribution(summary)

        assignments = controller.orchestrator.task_cache["attribution_assignments"]
        self.assertEqual(4, len(assignments))
        self.assertFalse(controller.orchestrator.task_cache["sample_library_persistence"])
        self.assertEqual(2, len(dialogs.requests))
        self.assertEqual("Root / Root Ex", dialogs.requests[0].target_label)
        self.assertTrue(dialogs.requests[0].allow_apply_to_remaining_folder)
        self.assertEqual("Folder_RT", dialogs.requests[1].target_label)
        self.assertEqual("298 K", dialogs.requests[1].prefill["temperature"])
        self.assertEqual("folder_heuristic", dialogs.requests[1].prefill_source)
        self.assertEqual(
            assignments[candidates[3].book_key],
            assignments[candidates[2].book_key],
        )
        self.assertEqual(1, len(dialogs.book_requests))
        self.assertEqual(
            {
                (candidates[3].book_key, "Root Ex"),
                (candidates[2].book_key, "Root F"),
            },
            set(dialogs.book_requests[0].choices),
        )
        self.assertFalse(dialogs.book_requests[0].allow_return_to_folder)
        self.assertIn("本阶段未写入样品库", widgets["run_log"].toPlainText())

    def test_attribution_return_previous_reopens_prior_target_with_confirmed_fields(self):
        from spectrum_organizer.core.selection import CandidateConversionResult
        from spectrum_organizer.ui.dialog_port import AttributionDialogResponse

        first = _candidate(
            "S1",
            "source.opj",
            "Folder_A",
            "EmA",
            "Emission A",
            spectrum_class=SpectrumClass.STEADY_EMISSION,
            fixed_wavelength="300",
        )
        second = _candidate(
            "S1",
            "source.opj",
            "Folder_B",
            "EmB",
            "Emission B",
            spectrum_class=SpectrumClass.STEADY_EMISSION,
            fixed_wavelength="350",
        )
        conversion = CandidateConversionResult((first, second), (), ())
        first_values = {
            "sample": "PFL",
            "solvent": "mTHF",
            "concentration": "1×10^-4",
            "temperature": "298 K",
        }
        revised_values = {
            **first_values,
            "sample": "MFL",
        }
        second_values = {
            **first_values,
            "sample": "DFL",
        }
        dialogs = FakeAttributionDialogPort(
            (
                AttributionDialogResponse(
                    action="confirm",
                    sample_type="solution",
                    values=first_values,
                ),
                AttributionDialogResponse(action="return_previous"),
                AttributionDialogResponse(
                    action="confirm",
                    sample_type="solution",
                    values=revised_values,
                ),
                AttributionDialogResponse(
                    action="confirm",
                    sample_type="solution",
                    values=second_values,
                ),
            )
        )
        controller, _, _, _, _ = self._controller(
            attribution_dialog_port=dialogs,
        )
        summary = {
            "total_inventory_count": 2,
            "total_extracted_count": 2,
            "source_summaries": (),
        }
        controller.orchestrator.task_cache["extraction_summary"] = summary

        controller._begin_attribution(summary, conversion)

        self.assertEqual(
            ["Folder_A", "Folder_B", "Folder_A", "Folder_B"],
            [request.target_label for request in dialogs.requests],
        )
        self.assertFalse(dialogs.requests[0].allow_return_previous)
        self.assertTrue(dialogs.requests[1].allow_return_previous)
        self.assertEqual("PFL", dialogs.requests[2].prefill["sample"])
        self.assertEqual("previous_attribution", dialogs.requests[2].prefill_source)
        assignments = controller.orchestrator.task_cache["attribution_assignments"]
        self.assertEqual("MFL", assignments[first.book_key].sample.sample)
        self.assertEqual("DFL", assignments[second.book_key].sample.sample)

    def test_task7_runs_duplicate_and_excitation_reviews_after_attribution(self):
        from spectrum_organizer.core.attribution import AttributionFields
        from spectrum_organizer.core.selection import CandidateConversionResult
        from spectrum_organizer.core.selection import review_emission_duplicates
        from spectrum_organizer.domain.models import NeatSample
        from spectrum_organizer.ui.dialog_port import AttributionDialogResponse

        emission_a = _candidate(
            "S1",
            "source.opj",
            "Folder",
            "EmA",
            "Emission A",
            spectrum_class=SpectrumClass.STEADY_EMISSION,
            fixed_wavelength="300",
        )
        emission_b = _candidate(
            "S1",
            "source.opj",
            "Folder",
            "EmB",
            "Emission B",
            spectrum_class=SpectrumClass.STEADY_EMISSION,
            fixed_wavelength="300",
        )
        excitation_a = _candidate(
            "S1",
            "source.opj",
            "Folder",
            "ExA",
            "Excitation A",
            spectrum_class=SpectrumClass.STEADY_EXCITATION,
            fixed_wavelength="450",
            wavelength_range=("300", "500"),
            scan_increment="1",
        )
        excitation_b = _candidate(
            "S1",
            "source.opj",
            "Folder",
            "ExB",
            "Excitation B",
            spectrum_class=SpectrumClass.STEADY_EXCITATION,
            fixed_wavelength="460",
            wavelength_range=("310", "510"),
            scan_increment="1",
        )
        conversion = CandidateConversionResult(
            (emission_a, emission_b, excitation_a, excitation_b),
            (),
            (),
        )
        attribution = FakeAttributionDialogPort(
            (
                AttributionDialogResponse(
                    action="confirm",
                    sample_type="solid",
                    values={
                        "sample": "PFL",
                        "state": "Solid",
                        "oxygen_environment": "Air",
                        "temperature": "298 K",
                    },
                ),
            )
        )
        runtime_updates = []
        popup_snapshots = []
        reviews = FakeConflictReviewDialogPort(
            (
                ConflictReviewResponse(
                    action="confirm_selection",
                    selected_book_keys=(emission_b.book_key,),
                ),
                ConflictReviewResponse(
                    action="confirm_selection",
                    selected_book_keys=(excitation_b.book_key, excitation_a.book_key),
                ),
            ),
            on_choose=lambda request: popup_snapshots.append(
                (
                    request.kind,
                    runtime_updates[-1] if runtime_updates else {},
                )
            ),
        )
        controller, _, widgets, _, _ = self._controller(
            attribution_dialog_port=attribution,
            conflict_review_dialog_port=reviews,
        )
        widgets["app_run_status"] = FakeLabel()
        controller.update_runtime_view = lambda **kwargs: runtime_updates.append(kwargs)
        summary = {
            "total_inventory_count": 4,
            "total_extracted_count": 4,
            "source_summaries": (),
        }
        controller.orchestrator.task_cache["extraction_summary"] = summary

        controller._begin_attribution(summary, conversion)

        self.assertEqual(
            ["emission_duplicate", "excitation_selection"],
            [request.kind for request in reviews.requests],
        )
        self.assertEqual(
            [
                ("emission_duplicate", ("4", "4", "4", "0")),
                ("excitation_selection", ("4", "4", "2", "0")),
            ],
            [
                (kind, snapshot.get("summary_numbers"))
                for kind, snapshot in popup_snapshots
            ],
        )
        self.assertIn("return_to_attribution", reviews.requests[0].actions)
        self.assertEqual(
            (excitation_a.book_key, excitation_b.book_key),
            reviews.requests[1].initial_selection,
        )
        duplicate_choices = reviews.requests[0].choices
        self.assertEqual(
            ("Emission A", "Emission B"),
            tuple(choice.display_name for choice in duplicate_choices),
        )
        displayed_fields = dict(duplicate_choices[0].fields)
        self.assertEqual("source.opj", displayed_fields["来源文件"])
        self.assertEqual("Folder", displayed_fields["Folder"])
        self.assertEqual("300 nm", displayed_fields["固定激发波长"])
        self.assertNotIn(emission_a.book_key, "\n".join(displayed_fields.values()))
        self.assertEqual(
            (
                emission_b.book_key,
                excitation_b.book_key,
                excitation_a.book_key,
            ),
            controller.orchestrator.task_cache["task7_selected_book_keys"],
        )
        self.assertTrue(
            controller.orchestrator.task_cache["task7_review_complete"]
        )
        self.assertFalse(
            controller.orchestrator.task_cache["sample_library_persistence"]
        )
        self.assertEqual("等待后续确认", runtime_updates[-1]["runtime_status"])
        self.assertNotIn("人工验收", widgets["run_log"].toPlainText())
        self.assertEqual(
            ("4", "4", "0", "1"),
            runtime_updates[-1]["summary_numbers"],
        )
        self.assertIn(
            (
                "source.opj",
                "Folder / Emission A",
                "已排除：重复发射谱审核未选中",
            ),
            runtime_updates[-1]["review_rows"],
        )
        colliding_label_a = AttributionFields(
            sample=NeatSample("A-B", "C", "298 K")
        )
        colliding_label_b = AttributionFields(
            sample=NeatSample("A", "B-C", "298 K")
        )
        spectrum_a = app_module._selection_spectrum_from_candidate(
            emission_a,
            colliding_label_a,
        )
        spectrum_b = app_module._selection_spectrum_from_candidate(
            emission_b,
            colliding_label_b,
        )
        special_a = app_module._special_book_from_candidate(
            emission_a,
            colliding_label_a,
        )
        special_b = app_module._special_book_from_candidate(
            emission_b,
            colliding_label_b,
        )
        self.assertNotEqual(spectrum_a.sample_system, spectrum_b.sample_system)
        self.assertNotEqual(special_a.sample_label, special_b.sample_label)
        self.assertEqual(
            (),
            review_emission_duplicates([spectrum_a, spectrum_b]).pending_reviews,
        )

    def test_exact_duplicate_excitation_request_states_zero_or_one_rule_on_first_view(self):
        from spectrum_organizer.core.attribution import AttributionFields
        from spectrum_organizer.core.selection import CandidateConversionResult
        from spectrum_organizer.domain.models import NeatSample

        first = _candidate(
            "S1",
            "source.opj",
            "Excitation",
            "ExA",
            "Excitation A",
            spectrum_class=SpectrumClass.STEADY_EXCITATION,
            fixed_wavelength="450",
            wavelength_range=("300", "500"),
            scan_increment="1",
        )
        duplicate = _candidate(
            "S1",
            "source.opj",
            "Excitation",
            "ExB",
            "Excitation B",
            spectrum_class=SpectrumClass.STEADY_EXCITATION,
            fixed_wavelength="450",
            wavelength_range=("300", "500"),
            scan_increment="1",
        )
        conversion = CandidateConversionResult((first, duplicate), (), ())
        reviews = FakeConflictReviewDialogPort(
            (
                ConflictReviewResponse(
                    action="confirm_selection",
                    selected_book_keys=(first.book_key,),
                ),
            )
        )
        controller, _, _, _, _ = self._controller(
            conflict_review_dialog_port=reviews,
        )
        attribution = AttributionFields(
            sample=NeatSample("PFL", "Solid", "298 K"),
        )
        summary = {
            "total_inventory_count": 2,
            "total_extracted_count": 2,
            "source_summaries": (),
        }

        controller._begin_conflict_review(
            summary,
            conversion,
            {
                first.book_key: attribution,
                duplicate.book_key: attribution,
            },
            attribution_rows=(),
            rejections=(),
        )

        request = reviews.requests[0]
        self.assertEqual("excitation_selection", request.kind)
        self.assertEqual(
            ((first.book_key, duplicate.book_key),),
            request.single_select_groups,
        )
        self.assertIn("本组最多选择 1 个", request.instruction)
        self.assertIn("不同发射波长可同时保留", request.instruction)
        self.assertNotIn("不同接收波长", request.instruction)
        for choice in request.choices:
            fields = dict(choice.fields)
            self.assertEqual("450 nm", fields["固定发射波长"])
            self.assertNotIn("固定接收波长", fields)

    def test_steady_2d_is_accepted_without_any_manual_review(self):
        from spectrum_organizer.core.attribution import AttributionFields
        from spectrum_organizer.domain.models import NeatSample

        special = _candidate(
            "S1",
            "source.opj",
            "Map",
            "Map1",
            "Renamed 2D Map",
            spectrum_class=SpectrumClass.STEADY_2D,
        )
        attribution = AttributionFields(
            sample=NeatSample("PFL", "Solid", "298 K"),
        )
        reviews = FakeConflictReviewDialogPort()
        pending_counts = []
        controller, _, _, _, _ = self._controller(
            conflict_review_dialog_port=reviews,
        )

        result = controller._review_special_groups(
            (special,),
            {special.book_key: attribution},
            {special.book_key: special},
            publish_pending=pending_counts.append,
        )

        self.assertEqual(1, len(result[0]))
        self.assertEqual("steady_2d", result[0][0].kind)
        self.assertEqual([], reviews.requests)
        self.assertEqual([], pending_counts)

    def test_delayed_special_group_publishes_pending_before_review(self):
        from spectrum_organizer.core.attribution import AttributionFields
        from spectrum_organizer.core.selection import CandidateConversionResult
        from spectrum_organizer.domain.models import NeatSample

        delayed = tuple(
            _candidate(
                "S1",
                "source.opj",
                "Delayed",
                f"B{w}",
                f"Book {w}",
                spectrum_class=SpectrumClass.DELAYED_EMISSION,
                fixed_wavelength=str(w),
                flash_delay="0.1",
                time_per_flash="1.1",
            )
            for w in (300, 305, 310, 315, 320)
        )
        delayed_updates = []
        delayed_popup_snapshots = []
        delayed_reviews = FakeConflictReviewDialogPort(
            (ConflictReviewResponse(action="confirm_group"),),
            on_choose=lambda _request: delayed_popup_snapshots.append(
                delayed_updates[-1] if delayed_updates else {}
            ),
        )
        delayed_controller, _, delayed_widgets, _, _ = self._controller(
            conflict_review_dialog_port=delayed_reviews,
        )
        delayed_widgets["app_run_status"] = FakeLabel()
        delayed_controller.update_runtime_view = (
            lambda **kwargs: delayed_updates.append(kwargs)
        )
        delayed_conversion = CandidateConversionResult(delayed, (), ())
        delayed_attribution = AttributionFields(
            sample=NeatSample("PFL", "Solid", "298 K"),
        )
        delayed_summary = {
            "total_inventory_count": 5,
            "total_extracted_count": 5,
            "source_summaries": (),
        }

        delayed_controller._begin_conflict_review(
            delayed_summary,
            delayed_conversion,
            {
                candidate.book_key: delayed_attribution
                for candidate in delayed
            },
            attribution_rows=(),
            rejections=(),
        )

        self.assertEqual(
            ("5", "5", "5", "0"),
            delayed_popup_snapshots[0].get("summary_numbers"),
        )

    def test_special_group_pending_count_includes_known_later_excitation_books(self):
        from spectrum_organizer.core.attribution import AttributionFields
        from spectrum_organizer.core.selection import CandidateConversionResult
        from spectrum_organizer.domain.models import NeatSample

        delayed = tuple(
            _candidate(
                "S1",
                "source.opj",
                "Delayed",
                f"B{w}",
                f"Book {w}",
                spectrum_class=SpectrumClass.DELAYED_EMISSION,
                fixed_wavelength=str(w),
                flash_delay="0.1",
                time_per_flash="1.1",
            )
            for w in (300, 305, 310, 315, 320)
        )
        excitations = tuple(
            _candidate(
                "S1",
                "source.opj",
                "Excitation",
                f"Ex{w}",
                f"Excitation {w}",
                spectrum_class=SpectrumClass.STEADY_EXCITATION,
                fixed_wavelength=str(w),
                wavelength_range=("250", "550"),
                scan_increment="1",
            )
            for w in (450, 460)
        )
        candidates = (*delayed, *excitations)
        conversion = CandidateConversionResult(candidates, (), ())
        attribution = AttributionFields(
            sample=NeatSample("PFL", "Solid", "298 K"),
        )
        runtime_updates = []
        popup_snapshots = []
        reviews = FakeConflictReviewDialogPort(
            (
                ConflictReviewResponse(action="confirm_group"),
                ConflictReviewResponse(
                    action="confirm_selection",
                    selected_book_keys=tuple(
                        candidate.book_key for candidate in excitations
                    ),
                ),
            ),
            on_choose=lambda request: popup_snapshots.append(
                (
                    request.kind,
                    runtime_updates[-1]["summary_numbers"][2],
                )
            ),
        )
        controller, _, widgets, _, _ = self._controller(
            conflict_review_dialog_port=reviews,
        )
        widgets["app_run_status"] = FakeLabel()
        controller.update_runtime_view = lambda **kwargs: runtime_updates.append(
            kwargs
        )
        summary = {
            "total_inventory_count": len(candidates),
            "total_extracted_count": len(candidates),
            "source_summaries": (),
        }

        controller._begin_conflict_review(
            summary,
            conversion,
            {
                candidate.book_key: attribution
                for candidate in candidates
            },
            attribution_rows=(),
            rejections=(),
        )

        self.assertEqual(
            [("special_group", "7"), ("excitation_selection", "2")],
            popup_snapshots,
        )

    def test_special_group_pending_count_includes_known_emission_and_excitation_books(self):
        from spectrum_organizer.core.attribution import AttributionFields
        from spectrum_organizer.core.selection import CandidateConversionResult
        from spectrum_organizer.domain.models import NeatSample

        delayed = tuple(
            _candidate(
                "S1",
                "source.opj",
                "Delayed",
                f"B{w}",
                f"Book {w}",
                spectrum_class=SpectrumClass.DELAYED_EMISSION,
                fixed_wavelength=str(w),
                flash_delay="0.1",
                time_per_flash="1.1",
            )
            for w in (300, 305, 310, 315, 320)
        )
        emissions = tuple(
            _candidate(
                "S1",
                "source.opj",
                "Emission",
                f"Em{index}",
                f"Emission {index}",
                spectrum_class=SpectrumClass.STEADY_EMISSION,
                fixed_wavelength="400",
            )
            for index in (1, 2)
        )
        excitations = tuple(
            _candidate(
                "S1",
                "source.opj",
                "Excitation",
                f"Ex{w}",
                f"Excitation {w}",
                spectrum_class=SpectrumClass.STEADY_EXCITATION,
                fixed_wavelength=str(w),
                wavelength_range=("250", "550"),
                scan_increment="1",
            )
            for w in (450, 460)
        )
        candidates = (*delayed, *emissions, *excitations)
        attribution = AttributionFields(
            sample=NeatSample("PFL", "Solid", "298 K"),
        )
        runtime_updates = []
        popup_snapshots = []
        reviews = FakeConflictReviewDialogPort(
            (
                ConflictReviewResponse(action="confirm_group"),
                ConflictReviewResponse(
                    action="confirm_selection",
                    selected_book_keys=(emissions[0].book_key,),
                ),
                ConflictReviewResponse(
                    action="confirm_selection",
                    selected_book_keys=tuple(
                        candidate.book_key for candidate in excitations
                    ),
                ),
            ),
            on_choose=lambda request: popup_snapshots.append(
                (
                    request.kind,
                    runtime_updates[-1]["summary_numbers"][2],
                )
            ),
        )
        controller, _, widgets, _, _ = self._controller(
            conflict_review_dialog_port=reviews,
        )
        widgets["app_run_status"] = FakeLabel()
        controller.update_runtime_view = lambda **kwargs: runtime_updates.append(
            kwargs
        )

        controller._begin_conflict_review(
            {
                "total_inventory_count": len(candidates),
                "total_extracted_count": len(candidates),
                "source_summaries": (),
            },
            CandidateConversionResult(candidates, (), ()),
            {
                candidate.book_key: attribution
                for candidate in candidates
            },
            attribution_rows=(),
            rejections=(),
        )

        self.assertEqual(
            [
                ("special_group", "9"),
                ("emission_duplicate", "4"),
                ("excitation_selection", "2"),
            ],
            popup_snapshots,
        )

    def test_special_overlap_options_use_standard_labels_and_show_candidate_conditions(self):
        from spectrum_organizer.core.attribution import AttributionFields
        from spectrum_organizer.domain.models import NeatSample

        candidates = [
            _candidate(
                "S1",
                "source.opj",
                "Delayed",
                f"B{w}",
                f"Book {w}",
                spectrum_class=SpectrumClass.DELAYED_EMISSION,
                fixed_wavelength=str(w),
                flash_delay="0.1",
                time_per_flash="1.1",
            )
            for w in (300, 305, 310, 315, 320)
        ]
        candidates.extend(
            (
                _candidate(
                    "S1",
                    "source.opj",
                    "Delayed",
                    "B300_D2",
                    "Book 300 D2",
                    spectrum_class=SpectrumClass.DELAYED_EMISSION,
                    fixed_wavelength="300",
                    flash_delay="0.2",
                    time_per_flash="1.2",
                ),
                _candidate(
                    "S1",
                    "source.opj",
                    "Delayed",
                    "B300_D3",
                    "Book 300 D3",
                    spectrum_class=SpectrumClass.DELAYED_EMISSION,
                    fixed_wavelength="300",
                    flash_delay="0.3",
                    time_per_flash="1.3",
                ),
            )
        )
        attribution = AttributionFields(
            sample=NeatSample("PFL", "Solid", "298 K"),
        )
        assignments = {
            candidate.book_key: attribution
            for candidate in candidates
        }
        class GroupedOverlapReviews(FakeConflictReviewDialogPort):
            def choose(self, request, *, parent=None):
                self.requests.append(request)
                if request.kind == "special_conflict_batch":
                    return ConflictReviewResponse(
                        action="confirm_all_conflicts",
                        group_selections=(
                            (
                                request.choice_groups[0].group_key,
                                "二维延迟谱",
                            ),
                        ),
                    )
                if request.kind == "special_group":
                    return ConflictReviewResponse(action="confirm_group")
                raise AssertionError(
                    f"unexpected review kind: {request.kind}"
                )

        reviews = GroupedOverlapReviews()
        controller, _, _, _, _ = self._controller(
            conflict_review_dialog_port=reviews,
        )

        result = controller._review_special_groups(
            candidates,
            assignments,
            {candidate.book_key: candidate for candidate in candidates},
        )

        self.assertIsNotNone(result)
        request = reviews.requests[0]
        self.assertEqual("special_conflict_batch", request.kind)
        self.assertFalse(request.editing_existing_decisions)
        self.assertEqual("确认特殊谱归类冲突", request.title)
        self.assertEqual("特殊谱归类冲突", request.decision_subject)
        self.assertEqual("grouped_single", request.selection_mode)
        self.assertIn("return_to_attribution", request.actions)
        self.assertEqual(1, len(request.choice_groups))
        group = request.choice_groups[0]
        self.assertEqual(
            (
                "二维延迟谱",
                "时间分辨延迟谱（变化轴：延迟时间—单次闪光周期）",
                "常规延迟谱",
            ),
            tuple(choice.display_name for choice in group.choices),
        )
        common_fields = dict(group.common_fields)
        self.assertEqual("source.opj", common_fields["来源文件"])
        self.assertEqual("Delayed", common_fields["Folder"])
        self.assertEqual("300 nm", common_fields["固定激发波长"])
        self.assertEqual("0.1 ms", common_fields["延迟时间"])
        self.assertEqual("1.1 ms", common_fields["单次闪光周期"])
        self.assertNotIn("延迟参数", common_fields)
        self.assertNotIn("Flash Delay", common_fields)
        self.assertNotIn("Time per Flash", common_fields)
        for choice in group.choices:
            self.assertEqual((), choice.fields)

    def test_final_special_overlap_editor_names_each_classification_difference(self):
        candidate = _candidate(
            "S1",
            "source.opj",
            "Delayed",
            "B300",
            "Book 300",
            spectrum_class=SpectrumClass.DELAYED_EMISSION,
            fixed_wavelength="300",
            flash_delay="0.1",
            time_per_flash="1.1",
        )
        decision = app_module._Task7ReviewDecision(
            "special_overlap",
            candidate.book_key,
            (candidate.book_key,),
        )

        choices, common_fields = app_module._final_conflict_choices(
            decision,
            {candidate.book_key: candidate},
        )

        self.assertIn("固定激发波长", dict(common_fields))
        self.assertEqual(
            {
                "归类：二维延迟谱",
                "归类：时间分辨延迟谱（变化轴：延迟时间—单次闪光周期）",
                "归类：常规延迟谱",
            },
            {choice.detail for choice in choices},
        )
        self.assertNotIn(
            "仅 Book 名不同",
            {choice.detail for choice in choices},
        )

    def test_delay_time_duplicate_editor_names_the_conflict_kind(self):
        from spectrum_organizer.core.attribution import AttributionFields
        from spectrum_organizer.domain.models import NeatSample

        candidates = tuple(
            _candidate(
                "S1",
                "source.opj",
                "Delayed",
                f"D{index}",
                f"Delay {index}",
                spectrum_class=SpectrumClass.DELAYED_EMISSION,
                fixed_wavelength="300",
                flash_delay=str(delay),
                time_per_flash=str(period),
            )
            for index, (delay, period) in enumerate(
                (
                    (0.05, 45.05),
                    (0.5, 45.5),
                    (1, 46),
                    (1, 46),
                    (5, 50),
                ),
                start=1,
            )
        )
        attribution = AttributionFields(
            sample=NeatSample("PFL", "Solid", "298 K"),
        )
        returned = []
        reviews = FakeConflictReviewDialogPort(
            (ConflictReviewResponse(action="return_to_attribution"),)
        )
        controller, _, _, _, _ = self._controller(
            conflict_review_dialog_port=reviews,
        )

        result = controller._review_special_groups(
            candidates,
            {
                candidate.book_key: attribution
                for candidate in candidates
            },
            {candidate.book_key: candidate for candidate in candidates},
            return_to_attribution=returned.append,
        )

        self.assertIsNone(result)
        request = reviews.requests[0]
        self.assertEqual(
            "确认时间分辨延迟谱重复 Book 冲突",
            request.title,
        )
        self.assertEqual(
            "时间分辨延迟谱重复 Book 冲突",
            request.decision_subject,
        )

    def test_delay_time_special_group_instruction_uses_standard_axis_term(self):
        from spectrum_organizer.core.attribution import AttributionFields
        from spectrum_organizer.domain.models import NeatSample

        candidates = [
            _candidate(
                "S1",
                "source.opj",
                "Delayed",
                f"D{index}",
                f"Delay {index}",
                spectrum_class=SpectrumClass.DELAYED_EMISSION,
                fixed_wavelength="300",
                flash_delay=str(delay),
                time_per_flash=str(delay + 1),
            )
            for index, delay in enumerate((0.1, 0.2, 0.3), start=1)
        ]
        attribution = AttributionFields(
            sample=NeatSample("PFL", "Solid", "298 K"),
        )
        reviews = FakeConflictReviewDialogPort()
        controller, _, _, _, _ = self._controller(
            conflict_review_dialog_port=reviews,
        )

        result = controller._review_special_groups(
            candidates,
            {
                candidate.book_key: attribution
                for candidate in candidates
            },
            {candidate.book_key: candidate for candidate in candidates},
        )

        self.assertIsNotNone(result)
        self.assertEqual("special_group", reviews.requests[0].kind)
        self.assertIn("return_to_attribution", reviews.requests[0].actions)
        self.assertEqual(
            "时间分辨延迟谱（变化轴：延迟时间—单次闪光周期）",
            reviews.requests[0].decision_subject,
        )

    def test_special_group_per_book_can_return_to_whole_group(self):
        from spectrum_organizer.core.attribution import AttributionFields
        from spectrum_organizer.domain.models import NeatSample

        candidates = tuple(
            _candidate(
                "S1",
                "source.opj",
                "Delayed",
                f"B{w}",
                f"Book {w}",
                spectrum_class=SpectrumClass.DELAYED_EMISSION,
                fixed_wavelength=str(w),
                flash_delay="0.1",
                time_per_flash="1.1",
            )
            for w in (300, 305, 310, 315, 320)
        )
        attribution = AttributionFields(
            sample=NeatSample("PFL", "Solid", "298 K"),
        )
        reviews = FakeConflictReviewDialogPort(
            (
                ConflictReviewResponse(action="review_books"),
                ConflictReviewResponse(action="return_to_group"),
                ConflictReviewResponse(action="confirm_group"),
            )
        )
        controller, _, _, _, _ = self._controller(
            conflict_review_dialog_port=reviews,
        )

        result = controller._review_special_groups(
            candidates,
            {candidate.book_key: attribution for candidate in candidates},
            {candidate.book_key: candidate for candidate in candidates},
        )

        self.assertEqual(1, len(result[0]))
        self.assertEqual(
            ["special_group", "special_group_books", "special_group"],
            [request.kind for request in reviews.requests],
        )
        self.assertIn(
            "return_to_group",
            reviews.requests[1].actions,
        )
        for request in reviews.requests:
            self.assertIn("return_to_attribution", request.actions)

    def test_special_group_per_book_return_restores_its_draft_selection(self):
        from spectrum_organizer.core.attribution import AttributionFields
        from spectrum_organizer.domain.models import NeatSample

        candidates = tuple(
            _candidate(
                "S1",
                "source.opj",
                "Delayed",
                f"B{w}",
                f"Book {w}",
                spectrum_class=SpectrumClass.DELAYED_EMISSION,
                fixed_wavelength=str(w),
                flash_delay="0.1",
                time_per_flash="1.1",
            )
            for w in (300, 305, 310, 315, 320)
        )
        selected = tuple(candidate.book_key for candidate in candidates[:2])
        attribution = AttributionFields(
            sample=NeatSample("PFL", "Solid", "298 K"),
        )
        reviews = FakeConflictReviewDialogPort(
            (
                ConflictReviewResponse(action="review_books"),
                ConflictReviewResponse(
                    action="return_to_group",
                    selected_book_keys=selected,
                ),
                ConflictReviewResponse(action="review_books"),
                ConflictReviewResponse(
                    action="confirm_selection",
                    selected_book_keys=tuple(
                        candidate.book_key for candidate in candidates
                    ),
                ),
            )
        )
        controller, _, _, _, _ = self._controller(
            conflict_review_dialog_port=reviews,
        )

        result = controller._review_special_groups(
            candidates,
            {candidate.book_key: attribution for candidate in candidates},
            {candidate.book_key: candidate for candidate in candidates},
        )

        self.assertIsNotNone(result)
        self.assertEqual(
            [
                "special_group",
                "special_group_books",
                "special_group",
                "special_group_books",
            ],
            [request.kind for request in reviews.requests],
        )
        self.assertEqual(selected, reviews.requests[3].initial_selection)

    def test_special_duplicate_can_return_to_attribution(self):
        from spectrum_organizer.core.attribution import AttributionFields
        from spectrum_organizer.domain.models import NeatSample

        candidates = tuple(
            _candidate(
                "S1",
                "source.opj",
                "Delayed",
                f"B{index}",
                f"Book {index}",
                spectrum_class=SpectrumClass.DELAYED_EMISSION,
                fixed_wavelength=str(wavelength),
                flash_delay="0.1",
                time_per_flash="1.1",
            )
            for index, wavelength in enumerate(
                (300, 300, 305, 310, 315, 320),
                start=1,
            )
        )
        attribution = AttributionFields(
            sample=NeatSample("PFL", "Solid", "298 K"),
        )
        returned = []
        reviews = FakeConflictReviewDialogPort(
            (ConflictReviewResponse(action="return_to_attribution"),)
        )
        controller, _, _, _, _ = self._controller(
            conflict_review_dialog_port=reviews,
        )

        result = controller._review_special_groups(
            candidates,
            {candidate.book_key: attribution for candidate in candidates},
            {candidate.book_key: candidate for candidate in candidates},
            return_to_attribution=returned.append,
        )

        self.assertIsNone(result)
        self.assertEqual("special_conflict_batch", reviews.requests[0].kind)
        self.assertIn(
            "return_to_attribution",
            reviews.requests[0].actions,
        )
        self.assertEqual(
            [tuple(candidate.book_key for candidate in candidates)],
            returned,
        )

    def test_special_group_can_reopen_latest_related_conflict_without_replaying_unrelated_one(self):
        from spectrum_organizer.core.attribution import AttributionFields
        from spectrum_organizer.domain.models import NeatSample

        group_a = tuple(
            _candidate(
                "S1",
                "source.opj",
                "Delayed A",
                f"A{index}",
                f"Group A {index}",
                spectrum_class=SpectrumClass.DELAYED_EMISSION,
                fixed_wavelength=str(wavelength),
                flash_delay="0.1",
                time_per_flash="1.1",
            )
            for index, wavelength in enumerate(
                (300, 300, 305, 310, 315, 320),
                start=1,
            )
        )
        group_b = tuple(
            _candidate(
                "S1",
                "source.opj",
                "Delayed B",
                f"B{index}",
                f"Group B {index}",
                spectrum_class=SpectrumClass.DELAYED_EMISSION,
                fixed_wavelength=str(wavelength),
                flash_delay="0.1",
                time_per_flash="1.1",
            )
            for index, wavelength in enumerate(
                (400, 400, 405, 410, 415, 420),
                start=1,
            )
        )
        candidates = (*group_a, *group_b)
        group_a_duplicate = tuple(
            candidate.book_key for candidate in group_a[:2]
        )
        group_b_duplicate = tuple(
            candidate.book_key for candidate in group_b[:2]
        )
        assignments = {
            candidate.book_key: AttributionFields(
                sample=NeatSample(
                    "Sample A" if candidate in group_a else "Sample B",
                    "Solid",
                    "298 K",
                ),
            )
            for candidate in candidates
        }
        class RelatedConflictReviews(FakeConflictReviewDialogPort):
            def __init__(self):
                super().__init__()
                self.group_visits = 0
                self.initial_batch_visits = 0

            def choose(self, request, *, parent=None):
                self.requests.append(request)
                if request.kind == "special_conflict_batch":
                    if "return_to_group" in request.actions:
                        return ConflictReviewResponse(
                            action="confirm_all_conflicts",
                            group_selections=(
                                (
                                    request.choice_groups[0].group_key,
                                    request.choice_groups[0].choices[1].book_key,
                                ),
                            ),
                        )
                    self.initial_batch_visits += 1
                    return ConflictReviewResponse(
                        action="confirm_all_conflicts",
                        group_selections=(
                            (
                                request.choice_groups[0].group_key,
                                request.choice_groups[0].choices[0].book_key,
                            ),
                        ),
                    )
                if request.kind == "special_group":
                    self.group_visits += 1
                    return ConflictReviewResponse(
                        action=(
                            "return_related_conflict"
                            if self.group_visits == 1
                            else "confirm_group"
                        )
                    )
                raise AssertionError(
                    f"unexpected review kind: {request.kind}"
                )

        reviews = RelatedConflictReviews()
        controller, _, _, _, _ = self._controller(
            conflict_review_dialog_port=reviews,
        )
        review_state = app_module._Task7ReviewState.empty()
        arguments = (
            candidates,
            assignments,
            {candidate.book_key: candidate for candidate in candidates},
        )

        while True:
            result = controller._review_special_groups(
                *arguments,
                review_state=review_state,
            )
            if result is not app_module._REVIEW_RESTART:
                break

        self.assertIsNotNone(result)
        self.assertEqual(
            [
                "special_conflict_batch",
                "special_conflict_batch",
                "special_group",
                "special_conflict_batch",
                "special_group",
                "special_group",
            ],
            [request.kind for request in reviews.requests],
        )
        first_group_request = reviews.requests[2]
        self.assertIn("return_previous", first_group_request.actions)
        self.assertIn(
            "return_related_conflict",
            first_group_request.actions,
        )
        self.assertEqual(
            group_a_duplicate,
            tuple(
                choice.book_key
                for choice in reviews.requests[3].choice_groups[0].choices
            ),
        )
        self.assertNotEqual(
            group_b_duplicate,
            tuple(
                choice.book_key
                for choice in reviews.requests[3].choice_groups[0].choices
            ),
        )
        self.assertFalse(reviews.requests[0].editing_existing_decisions)
        self.assertFalse(reviews.requests[1].editing_existing_decisions)
        self.assertTrue(reviews.requests[3].editing_existing_decisions)

    def test_special_group_reopens_one_grouped_editor_with_preserved_draft_state(self):
        from spectrum_organizer.core.attribution import AttributionFields
        from spectrum_organizer.domain.models import NeatSample

        candidates = _two_duplicate_special_candidates()
        assignments = {
            candidate.book_key: AttributionFields(
                sample=NeatSample("Sample", "Solid", "298 K"),
            )
            for candidate in candidates
        }

        class RelatedConflictReviews(FakeConflictReviewDialogPort):
            def __init__(self):
                super().__init__()
                self.group_visits = 0
                self.reentry_requests = []
                self.draft_selections = ()

            def choose(self, request, *, parent=None):
                self.requests.append(request)
                if request.kind == "special_conflict_batch":
                    if "return_to_group" not in request.actions:
                        return ConflictReviewResponse(
                            action="confirm_all_conflicts",
                            group_selections=tuple(
                                (group.group_key, group.choices[-1].book_key)
                                for group in request.choice_groups
                            ),
                        )
                    self.reentry_requests.append(request)
                    if len(self.reentry_requests) == 1:
                        self.draft_selections = (
                            (
                                request.choice_groups[0].group_key,
                                request.choice_groups[0].choices[0].book_key,
                            ),
                            (
                                request.choice_groups[1].group_key,
                                request.choice_groups[1].choices[-1].book_key,
                            ),
                        )
                        return ConflictReviewResponse(
                            action="return_to_group",
                            group_selections=self.draft_selections,
                            active_group_key=request.choice_groups[1].group_key,
                            scroll_value=73,
                        )
                    return ConflictReviewResponse(
                        action="confirm_all_conflicts",
                        group_selections=self.draft_selections,
                        active_group_key=request.choice_groups[1].group_key,
                        scroll_value=73,
                    )
                if request.kind == "special_group":
                    self.group_visits += 1
                    return ConflictReviewResponse(
                        action=(
                            "return_related_conflict"
                            if self.group_visits < 3
                            else "confirm_group"
                        )
                    )
                raise AssertionError(f"unexpected review kind: {request.kind}")

        reviews = RelatedConflictReviews()
        controller, _, _, _, _ = self._controller(
            conflict_review_dialog_port=reviews,
        )
        review_state = app_module._Task7ReviewState.empty()
        arguments = (
            candidates,
            assignments,
            {candidate.book_key: candidate for candidate in candidates},
        )

        while True:
            result = controller._review_special_groups(
                *arguments,
                review_state=review_state,
            )
            if result is not app_module._REVIEW_RESTART:
                break

        self.assertIsNotNone(result)
        self.assertEqual(
            [
                "special_conflict_batch",
                "special_group",
                "special_conflict_batch",
                "special_group",
                "special_conflict_batch",
                "special_group",
            ],
            [request.kind for request in reviews.requests],
        )
        first_reentry, reopened_reentry = reviews.reentry_requests
        self.assertEqual("grouped_single", first_reentry.selection_mode)
        self.assertEqual(2, len(first_reentry.choice_groups))
        self.assertIn("return_to_group", first_reentry.actions)
        self.assertEqual(
            reviews.draft_selections,
            tuple(
                (group.group_key, group.initial_selection)
                for group in reopened_reentry.choice_groups
            ),
        )
        self.assertEqual(
            first_reentry.choice_groups[1].group_key,
            reopened_reentry.initial_active_group_key,
        )
        self.assertEqual(73, reopened_reentry.initial_scroll_value)
        self.assertEqual(
            [candidates[0].book_key, candidates[5].book_key],
            list(review_state.special_duplicate_choices.values()),
        )

    def test_special_duplicate_points_in_one_context_use_one_grouped_editor(self):
        from spectrum_organizer.core.attribution import AttributionFields
        from spectrum_organizer.domain.models import NeatSample

        candidates = _two_duplicate_special_candidates()
        assignments = {
            candidate.book_key: AttributionFields(
                sample=NeatSample("Sample", "Solid", "298 K"),
            )
            for candidate in candidates
        }

        class GroupedConflictReviews(FakeConflictReviewDialogPort):
            def choose(self, request, *, parent=None):
                self.requests.append(request)
                if request.kind == "special_conflict_batch":
                    return ConflictReviewResponse(
                        action="confirm_all_conflicts",
                        group_selections=tuple(
                            (group.group_key, group.choices[-1].book_key)
                            for group in request.choice_groups
                        ),
                    )
                if request.kind == "special_group":
                    return ConflictReviewResponse(action="confirm_group")
                raise AssertionError(
                    f"unexpected review kind: {request.kind}"
                )

        reviews = GroupedConflictReviews()
        controller, _, _, _, _ = self._controller(
            conflict_review_dialog_port=reviews,
        )
        review_state = app_module._Task7ReviewState.empty()

        result = controller._review_special_groups(
            candidates,
            assignments,
            {candidate.book_key: candidate for candidate in candidates},
            review_state=review_state,
        )

        self.assertIsNotNone(result)
        self.assertEqual(
            ["special_conflict_batch", "special_group"],
            [request.kind for request in reviews.requests],
        )
        request = reviews.requests[0]
        self.assertEqual("grouped_single", request.selection_mode)
        self.assertEqual(2, len(request.choice_groups))
        self.assertEqual(
            [["300", "Pho300_10_10"], ["450", "Pho450_10_10"]],
            [
                [choice.display_name for choice in group.choices]
                for group in request.choice_groups
            ],
        )
        self.assertEqual(
            [group.choices[0].book_key for group in request.choice_groups],
            [group.initial_selection for group in request.choice_groups],
        )
        self.assertNotIn(
            "special_duplicate",
            [request.kind for request in reviews.requests],
        )

    def test_initial_grouped_editor_restores_draft_after_return_previous(self):
        from spectrum_organizer.core.attribution import AttributionFields
        from spectrum_organizer.domain.models import NeatSample

        candidates = _two_duplicate_special_candidates()
        assignments = {
            candidate.book_key: AttributionFields(
                sample=NeatSample("Sample", "Solid", "298 K"),
            )
            for candidate in candidates
        }

        class DraftReviews(FakeConflictReviewDialogPort):
            def __init__(self):
                super().__init__()
                self.batch_requests = []
                self.draft_selections = ()

            def choose(self, request, *, parent=None):
                self.requests.append(request)
                if request.kind == "special_conflict_batch":
                    self.batch_requests.append(request)
                    if len(self.batch_requests) == 1:
                        self.draft_selections = (
                            (
                                request.choice_groups[0].group_key,
                                request.choice_groups[0].choices[-1].book_key,
                            ),
                            (
                                request.choice_groups[1].group_key,
                                request.choice_groups[1].choices[0].book_key,
                            ),
                        )
                        return ConflictReviewResponse(
                            action="return_previous",
                            group_selections=self.draft_selections,
                            active_group_key=request.choice_groups[1].group_key,
                            scroll_value=61,
                        )
                    return ConflictReviewResponse(
                        action="confirm_all_conflicts",
                        group_selections=self.draft_selections,
                        active_group_key=request.choice_groups[1].group_key,
                        scroll_value=61,
                    )
                if request.kind == "special_group":
                    return ConflictReviewResponse(action="confirm_group")
                raise AssertionError(
                    f"unexpected review kind: {request.kind}"
                )

        reviews = DraftReviews()
        controller, _, _, _, _ = self._controller(
            conflict_review_dialog_port=reviews,
        )
        review_state = app_module._Task7ReviewState.empty()
        review_state.require(
            "emission",
            "earlier",
            ("old-book", "other-book"),
        )
        review_state.emission_choices["earlier"] = "old-book"
        review_state.remember(
            "emission",
            "earlier",
            ("old-book", "other-book"),
        )
        arguments = (
            candidates,
            assignments,
            {candidate.book_key: candidate for candidate in candidates},
        )

        while True:
            result = controller._review_special_groups(
                *arguments,
                review_state=review_state,
            )
            if result is not app_module._REVIEW_RESTART:
                break

        self.assertIsNotNone(result)
        self.assertEqual(2, len(reviews.batch_requests))
        reopened = reviews.batch_requests[1]
        self.assertEqual(
            reviews.draft_selections,
            tuple(
                (group.group_key, group.initial_selection)
                for group in reopened.choice_groups
            ),
        )
        self.assertEqual(
            reopened.choice_groups[1].group_key,
            reopened.initial_active_group_key,
        )
        self.assertEqual(61, reopened.initial_scroll_value)

    def test_return_previous_restores_the_whole_prior_conflict_batch(self):
        from spectrum_organizer.core.attribution import AttributionFields
        from spectrum_organizer.domain.models import NeatSample

        first_context = tuple(
            _candidate(
                "S1",
                "source.opj",
                "Delayed A",
                f"A{index}",
                f"Group A {index}",
                spectrum_class=SpectrumClass.DELAYED_EMISSION,
                fixed_wavelength=str(wavelength),
                flash_delay="0.1",
                time_per_flash="1.1",
            )
            for index, wavelength in enumerate(
                (450, 450, 460, 460, 470, 480, 490),
                start=1,
            )
        )
        second_context = tuple(
            _candidate(
                "S1",
                "source.opj",
                "Delayed B",
                f"B{index}",
                f"Group B {index}",
                spectrum_class=SpectrumClass.DELAYED_EMISSION,
                fixed_wavelength=str(wavelength),
                flash_delay="0.1",
                time_per_flash="1.1",
            )
            for index, wavelength in enumerate(
                (300, 300, 305, 310, 315, 320),
                start=1,
            )
        )
        candidates = (*first_context, *second_context)
        assignments = {
            candidate.book_key: AttributionFields(
                sample=NeatSample(
                    (
                        "Sample A"
                        if candidate in first_context
                        else "Sample B"
                    ),
                    "Solid",
                    "298 K",
                ),
            )
            for candidate in candidates
        }

        class BatchHistoryReviews(FakeConflictReviewDialogPort):
            def __init__(self):
                super().__init__()
                self.batch_requests = []
                self.first_batch_selections = ()

            def choose(self, request, *, parent=None):
                self.requests.append(request)
                if request.kind == "special_conflict_batch":
                    self.batch_requests.append(request)
                    selections = tuple(
                        (group.group_key, group.choices[-1].book_key)
                        for group in request.choice_groups
                    )
                    visit = len(self.batch_requests)
                    if visit == 1:
                        self.first_batch_selections = selections
                        return ConflictReviewResponse(
                            action="confirm_all_conflicts",
                            group_selections=selections,
                        )
                    if visit == 2:
                        return ConflictReviewResponse(
                            action="return_previous",
                            group_selections=selections,
                        )
                    if visit == 3:
                        return ConflictReviewResponse(
                            action="confirm_all_conflicts",
                            group_selections=self.first_batch_selections,
                        )
                    return ConflictReviewResponse(
                        action="confirm_all_conflicts",
                        group_selections=selections,
                    )
                if request.kind == "special_group":
                    return ConflictReviewResponse(action="confirm_group")
                raise AssertionError(
                    f"unexpected review kind: {request.kind}"
                )

        reviews = BatchHistoryReviews()
        controller, _, _, _, _ = self._controller(
            conflict_review_dialog_port=reviews,
        )
        review_state = app_module._Task7ReviewState.empty()
        arguments = (
            candidates,
            assignments,
            {candidate.book_key: candidate for candidate in candidates},
        )

        while True:
            result = controller._review_special_groups(
                *arguments,
                review_state=review_state,
            )
            if result is not app_module._REVIEW_RESTART:
                break

        self.assertIsNotNone(result)
        self.assertGreaterEqual(len(reviews.batch_requests), 4)
        first, second, reopened, resumed = reviews.batch_requests[:4]
        self.assertEqual(2, len(first.choice_groups))
        self.assertEqual(1, len(second.choice_groups))
        self.assertEqual(
            tuple(group.group_key for group in first.choice_groups),
            tuple(group.group_key for group in reopened.choice_groups),
        )
        self.assertEqual(
            reviews.first_batch_selections,
            tuple(
                (group.group_key, group.initial_selection)
                for group in reopened.choice_groups
            ),
        )
        self.assertEqual(
            "确认二维延迟谱重复 Book 冲突",
            reopened.title,
        )
        self.assertEqual(
            tuple(group.group_key for group in second.choice_groups),
            tuple(group.group_key for group in resumed.choice_groups),
        )

    def test_later_review_return_previous_restores_the_whole_conflict_batch(self):
        from spectrum_organizer.core.attribution import AttributionFields
        from spectrum_organizer.domain.models import NeatSample

        candidates = _two_duplicate_special_candidates()
        assignments = {
            candidate.book_key: AttributionFields(
                sample=NeatSample("Sample", "Solid", "298 K"),
            )
            for candidate in candidates
        }

        class LaterReturnReviews(FakeConflictReviewDialogPort):
            def __init__(self):
                super().__init__()
                self.batch_requests = []
                self.group_visits = 0

            def choose(self, request, *, parent=None):
                self.requests.append(request)
                if request.kind == "special_conflict_batch":
                    self.batch_requests.append(request)
                    return ConflictReviewResponse(
                        action="confirm_all_conflicts",
                        group_selections=tuple(
                            (group.group_key, group.choices[-1].book_key)
                            for group in request.choice_groups
                        ),
                    )
                if request.kind == "special_group":
                    self.group_visits += 1
                    return ConflictReviewResponse(
                        action=(
                            "return_previous"
                            if self.group_visits == 1
                            else "confirm_group"
                        )
                    )
                raise AssertionError(
                    f"unexpected review kind: {request.kind}"
                )

        reviews = LaterReturnReviews()
        controller, _, _, _, _ = self._controller(
            conflict_review_dialog_port=reviews,
        )
        review_state = app_module._Task7ReviewState.empty()
        arguments = (
            candidates,
            assignments,
            {candidate.book_key: candidate for candidate in candidates},
        )

        while True:
            result = controller._review_special_groups(
                *arguments,
                review_state=review_state,
            )
            if result is not app_module._REVIEW_RESTART:
                break

        self.assertIsNotNone(result)
        self.assertEqual(2, len(reviews.batch_requests))
        self.assertEqual(
            tuple(
                group.group_key
                for group in reviews.batch_requests[0].choice_groups
            ),
            tuple(
                group.group_key
                for group in reviews.batch_requests[1].choice_groups
            ),
        )

    def test_modified_conflict_batch_is_the_one_restored_by_return_previous(self):
        from spectrum_organizer.core.attribution import AttributionFields
        from spectrum_organizer.domain.models import NeatSample

        candidates = _two_duplicate_special_candidates()
        assignments = {
            candidate.book_key: AttributionFields(
                sample=NeatSample("Sample", "Solid", "298 K"),
            )
            for candidate in candidates
        }

        class ModifiedBatchReviews(FakeConflictReviewDialogPort):
            def __init__(self):
                super().__init__()
                self.batch_requests = []
                self.modified_selections = ()
                self.group_visits = 0

            def choose(self, request, *, parent=None):
                self.requests.append(request)
                if request.kind == "special_conflict_batch":
                    self.batch_requests.append(request)
                    visit = len(self.batch_requests)
                    if visit == 1:
                        selections = tuple(
                            (group.group_key, group.choices[-1].book_key)
                            for group in request.choice_groups
                        )
                    elif visit == 2:
                        selections = tuple(
                            (group.group_key, group.choices[0].book_key)
                            for group in request.choice_groups
                        )
                        self.modified_selections = selections
                    else:
                        selections = tuple(
                            (group.group_key, group.initial_selection)
                            for group in request.choice_groups
                        )
                    return ConflictReviewResponse(
                        action="confirm_all_conflicts",
                        group_selections=selections,
                    )
                if request.kind == "special_group":
                    self.group_visits += 1
                    return ConflictReviewResponse(
                        action=(
                            "return_related_conflict"
                            if self.group_visits == 1
                            else (
                                "return_previous"
                                if self.group_visits == 2
                                else "confirm_group"
                            )
                        )
                    )
                raise AssertionError(
                    f"unexpected review kind: {request.kind}"
                )

        reviews = ModifiedBatchReviews()
        controller, _, _, _, _ = self._controller(
            conflict_review_dialog_port=reviews,
        )
        review_state = app_module._Task7ReviewState.empty()
        arguments = (
            candidates,
            assignments,
            {candidate.book_key: candidate for candidate in candidates},
        )

        while True:
            result = controller._review_special_groups(
                *arguments,
                review_state=review_state,
            )
            if result is not app_module._REVIEW_RESTART:
                break

        self.assertIsNotNone(result)
        self.assertEqual(3, len(reviews.batch_requests))
        restored = reviews.batch_requests[2]
        self.assertEqual(
            reviews.modified_selections,
            tuple(
                (group.group_key, group.initial_selection)
                for group in restored.choice_groups
            ),
        )

    def test_related_conflict_lookup_uses_classifier_context_after_book_moves_out(self):
        review_state = app_module._Task7ReviewState.empty()
        review_state.require(
            "special_overlap",
            "moved-book",
            ("moved-book",),
            context_book_keys=("moved-book", "surviving-book"),
        )
        review_state.special_overlap_choices["moved-book"] = "regular"
        review_state.remember(
            "special_overlap",
            "moved-book",
            ("moved-book",),
            context_book_keys=("moved-book", "surviving-book"),
        )

        conflicts = review_state.related_special_conflicts(
            ("surviving-book",)
        )

        self.assertEqual(
            ("moved-book",),
            tuple(decision.key for decision in conflicts),
        )

    def test_related_overlap_rebind_uses_normalized_full_physical_point(self):
        from spectrum_organizer.core.special_groups import (
            SpectrumBook,
            spectrum_book_point_identity,
        )

        def book(name, *, wavelength, delay, time_per_flash):
            return SpectrumBook(
                source_id="S1",
                folder_path="Delayed",
                book_name=name,
                spectrum_class=SpectrumClass.DELAYED_EMISSION,
                sample_label="sample",
                fixed_excitation_wavelength=wavelength,
                receiving_range=("400", "750"),
                excitation_slit="10/10",
                emission_slit="10/10",
                flash_delay=delay,
                sample_window="20",
                time_per_flash=time_per_flash,
                flash_count="4",
            )

        original = book(
            "B300",
            wavelength="300",
            delay="0.1",
            time_per_flash="1.1",
        )
        equivalent_replacement = book(
            "B300dup",
            wavelength="300.0",
            delay="0.10",
            time_per_flash="1.10",
        )
        original_identity = spectrum_book_point_identity(original)
        duplicate = app_module._Task7ReviewDecision(
            "special_duplicate",
            "duplicate-300",
            (original.book_key, equivalent_replacement.book_key),
            test_point_label="300",
            physical_point_identity=original_identity,
        )
        overlap = app_module._Task7ReviewDecision(
            "special_overlap",
            original.book_key,
            (original.book_key,),
            test_point_label="300",
            physical_point_identity=original_identity,
        )
        state = app_module._Task7ReviewState.empty()
        state.special_duplicate_choices[duplicate.key] = original.book_key
        state.special_overlap_choices[original.book_key] = "delayed_2d"
        state.require(
            duplicate.bucket,
            duplicate.key,
            duplicate.book_keys,
            test_point_label=duplicate.test_point_label,
            physical_point_identity=duplicate.physical_point_identity,
        )
        state.require(
            overlap.bucket,
            overlap.key,
            overlap.book_keys,
            test_point_label=overlap.test_point_label,
            physical_point_identity=overlap.physical_point_identity,
        )
        state.history.extend((duplicate, overlap))
        batch = app_module._RelatedConflictBatch(
            group_book_keys=(
                original.book_key,
                equivalent_replacement.book_key,
            ),
            conflicts=(duplicate, overlap),
            selections=(
                (
                    app_module._related_conflict_id(duplicate),
                    original.book_key,
                ),
                (
                    app_module._related_conflict_id(overlap),
                    "delayed_2d",
                ),
            ),
            active_group_key=app_module._related_conflict_id(overlap),
        )
        state.related_conflict_batch = batch
        state.related_conflict_drafts[
            app_module._related_conflict_draft_key(batch.conflicts)
        ] = batch
        state.confirmed_related_conflict_batches.extend(
            (
                app_module._RelatedConflictBatch(
                    group_book_keys=batch.group_book_keys,
                    conflicts=(duplicate,),
                    selections=(batch.selections[0],),
                    active_group_key=batch.selections[0][0],
                    editor_open=False,
                    record_decisions=True,
                ),
                app_module._RelatedConflictBatch(
                    group_book_keys=batch.group_book_keys,
                    conflicts=(overlap,),
                    selections=(batch.selections[1],),
                    active_group_key=batch.selections[1][0],
                    editor_open=False,
                    record_decisions=True,
                ),
            )
        )
        selected_by_group = {
            app_module._related_conflict_id(duplicate):
                equivalent_replacement.book_key,
            app_module._related_conflict_id(overlap): "delayed_2d",
        }

        app_module._apply_related_conflict_selections(
            state,
            batch,
            selected_by_group,
            {
                original.book_key: original,
                equivalent_replacement.book_key: equivalent_replacement,
            },
        )

        self.assertEqual(
            equivalent_replacement.book_key,
            state.special_duplicate_choices[duplicate.key],
        )
        self.assertNotIn(
            original.book_key,
            state.special_overlap_choices,
        )
        self.assertEqual(
            "delayed_2d",
            state.special_overlap_choices[equivalent_replacement.book_key],
        )
        self.assertEqual(
            equivalent_replacement.book_key,
            state.history[1].key,
        )
        state.close_related_conflict_editor()
        self.assertTrue(state.recall_previous())
        restored = state.related_conflict_batch
        self.assertIsNotNone(restored)
        self.assertEqual(
            equivalent_replacement.book_key,
            restored.conflicts[1].key,
        )
        self.assertEqual(
            app_module._related_conflict_id(restored.conflicts[1]),
            restored.selections[1][0],
        )

    def test_return_previous_reopens_only_the_immediately_prior_review_decision(self):
        from spectrum_organizer.core.attribution import AttributionFields
        from spectrum_organizer.core.selection import CandidateConversionResult
        from spectrum_organizer.domain.models import NeatSample

        candidates = tuple(
            _candidate(
                "S1",
                "source.opj",
                f"Folder{group}",
                f"Em{group}{suffix}",
                f"Emission {group}{suffix}",
                spectrum_class=SpectrumClass.STEADY_EMISSION,
                fixed_wavelength=wavelength,
            )
            for group, wavelength in (("A", "300"), ("B", "350"))
            for suffix in ("1", "2")
        )
        first_group = tuple(candidate.book_key for candidate in candidates[:2])
        second_group = tuple(candidate.book_key for candidate in candidates[2:])
        runtime_updates = []
        pending_counts = []
        reviews = FakeConflictReviewDialogPort(
            (
                ConflictReviewResponse(
                    action="confirm_selection",
                    selected_book_keys=(first_group[0],),
                ),
                ConflictReviewResponse(action="return_previous"),
                ConflictReviewResponse(
                    action="confirm_selection",
                    selected_book_keys=(first_group[1],),
                ),
                ConflictReviewResponse(
                    action="confirm_selection",
                    selected_book_keys=(second_group[1],),
                ),
            ),
            on_choose=lambda _request: pending_counts.append(
                runtime_updates[-1]["summary_numbers"][2]
            ),
        )
        controller, _, widgets, _, _ = self._controller(
            conflict_review_dialog_port=reviews,
        )
        widgets["app_run_status"] = FakeLabel()
        controller.update_runtime_view = lambda **kwargs: runtime_updates.append(
            kwargs
        )
        attribution = AttributionFields(
            sample=NeatSample("PFL", "Solid", "298 K"),
        )

        controller._begin_conflict_review(
            {
                "total_inventory_count": 4,
                "total_extracted_count": 4,
            },
            CandidateConversionResult(candidates, (), ()),
            {candidate.book_key: attribution for candidate in candidates},
            attribution_rows=(),
            rejections=(),
        )

        self.assertEqual(
            [
                first_group,
                second_group,
                first_group,
                second_group,
            ],
            [
                tuple(choice.book_key for choice in request.choices)
                for request in reviews.requests
            ],
        )
        self.assertNotIn("return_previous", reviews.requests[0].actions)
        self.assertIn("return_previous", reviews.requests[1].actions)
        self.assertEqual(
            (first_group[0],),
            reviews.requests[2].initial_selection,
        )
        self.assertEqual(["4", "2", "4", "2"], pending_counts)
        self.assertEqual(
            (first_group[1], second_group[1]),
            controller.orchestrator.task_cache["task7_selected_book_keys"],
        )

    def test_return_to_attribution_preserves_unrelated_prior_review_decisions(self):
        from spectrum_organizer.core.selection import CandidateConversionResult
        from spectrum_organizer.ui.dialog_port import AttributionDialogResponse

        candidates = tuple(
            _candidate(
                "S1",
                "source.opj",
                f"Folder{group}",
                f"Em{group}{suffix}",
                f"Emission {group}{suffix}",
                spectrum_class=SpectrumClass.STEADY_EMISSION,
                fixed_wavelength=wavelength,
            )
            for group, wavelength in (("A", "300"), ("B", "350"))
            for suffix in ("1", "2")
        )
        first_group = tuple(candidate.book_key for candidate in candidates[:2])
        second_group = tuple(candidate.book_key for candidate in candidates[2:])
        attribution_values = {
            "sample": "PFL",
            "state": "Solid",
            "oxygen_environment": "Air",
            "temperature": "298 K",
        }
        attribution = FakeAttributionDialogPort(
            (
                AttributionDialogResponse(
                    action="confirm",
                    sample_type="solid",
                    values=attribution_values,
                ),
                AttributionDialogResponse(
                    action="confirm",
                    sample_type="solid",
                    values=attribution_values,
                ),
                AttributionDialogResponse(
                    action="confirm",
                    sample_type="solid",
                    values=attribution_values,
                ),
            )
        )
        reviews = FakeConflictReviewDialogPort(
            (
                ConflictReviewResponse(
                    action="confirm_selection",
                    selected_book_keys=(first_group[0],),
                ),
                ConflictReviewResponse(action="return_to_attribution"),
                ConflictReviewResponse(
                    action="confirm_selection",
                    selected_book_keys=(second_group[1],),
                ),
            )
        )
        scheduled = []
        controller, _, _, message_box, _ = self._controller(
            attribution_dialog_port=attribution,
            conflict_review_dialog_port=reviews,
            schedule_call=scheduled.append,
        )
        summary = {
            "total_inventory_count": 4,
            "total_extracted_count": 4,
        }
        controller.orchestrator.task_cache["extraction_summary"] = summary

        controller._begin_attribution(
            summary,
            CandidateConversionResult(candidates, (), ()),
        )

        self.assertEqual(
            1,
            len(scheduled),
            (
                [request.kind for request in reviews.requests],
                controller.orchestrator.last_failure,
                message_box.errors,
            ),
        )
        self.assertEqual(
            [first_group, second_group],
            [
                tuple(choice.book_key for choice in request.choices)
                for request in reviews.requests
            ],
        )

        scheduled[0]()

        self.assertEqual(
            [first_group, second_group, second_group],
            [
                tuple(choice.book_key for choice in request.choices)
                for request in reviews.requests
            ],
        )
        self.assertEqual(1, sum(request.target_label == "FolderA" for request in attribution.requests))
        self.assertEqual(2, sum(request.target_label == "FolderB" for request in attribution.requests))
        self.assertEqual(
            (first_group[0], second_group[1]),
            controller.orchestrator.task_cache["task7_selected_book_keys"],
        )
        self.assertTrue(
            controller.orchestrator.task_cache["task7_review_complete"]
        )

    def test_unexpected_task7_dialog_failure_clears_review_state_for_retry(self):
        from spectrum_organizer.core.selection import CandidateConversionResult
        from spectrum_organizer.ui.dialog_port import AttributionDialogResponse

        special = tuple(
            _candidate(
                "S1",
                "source.opj",
                "Delayed",
                f"B{w}",
                f"Book {w}",
                spectrum_class=SpectrumClass.DELAYED_EMISSION,
                fixed_wavelength=str(w),
                flash_delay="0.1",
                time_per_flash="1.1",
            )
            for w in (300, 305, 310, 315, 320)
        )
        conversion = CandidateConversionResult(special, (), ())

        class FailingReviewPort:
            def choose(self, _request, *, parent=None):
                raise RuntimeError("review renderer failed")

        controller, _, widgets, message_box, _ = self._controller(
            attribution_dialog_port=FakeAttributionDialogPort(
                (
                    AttributionDialogResponse(
                        action="confirm",
                        sample_type="solid",
                        values={
                            "sample": "PFL",
                            "state": "Solid",
                            "oxygen_environment": "Air",
                            "temperature": "298 K",
                        },
                    ),
                )
            ),
            conflict_review_dialog_port=FailingReviewPort(),
        )
        summary = {
            "total_inventory_count": 5,
            "source_summaries": (),
        }
        controller.orchestrator.task_cache.update(
            {
                "extraction_summary": summary,
                "approved_snapshot": object(),
                "output_model": object(),
                "sample_record_ids": {"stale": 1},
            }
        )

        controller._begin_attribution(summary, conversion)

        self.assertEqual(
            "review renderer failed",
            controller.orchestrator.last_failure,
        )
        for key in (
            "candidate_conversion",
            "attribution_assignments",
            "special_groups",
            "task7_review_complete",
            "approved_snapshot",
            "output_model",
            "sample_record_ids",
        ):
            self.assertNotIn(key, controller.orchestrator.task_cache)
        self.assertEqual(
            [("样品归属准备失败", "review renderer failed")],
            message_box.errors,
        )
        self.assertTrue(widgets["select_sources_button"].visible)

    def test_cross_source_return_to_attribution_reopens_only_affected_targets(self):
        from spectrum_organizer.core.attribution import AttributionSession
        from spectrum_organizer.core.selection import CandidateConversionResult
        from spectrum_organizer.ui.dialog_port import AttributionDialogResponse

        first = _candidate(
            "S1",
            "first.opj",
            "FolderA",
            "EmA",
            "Emission A",
            spectrum_class=SpectrumClass.STEADY_EMISSION,
            fixed_wavelength="300",
        )
        second = _candidate(
            "S2",
            "second.opj",
            "FolderB",
            "EmB",
            "Emission B",
            spectrum_class=SpectrumClass.STEADY_EMISSION,
            fixed_wavelength="300",
        )
        unrelated = _candidate(
            "S3",
            "third.opj",
            "FolderC",
            "EmC",
            "Emission C",
            spectrum_class=SpectrumClass.STEADY_EMISSION,
            fixed_wavelength="450",
        )
        conversion = CandidateConversionResult((first, second, unrelated), (), ())
        values = {
            "sample": "PFL",
            "state": "Solid",
            "oxygen_environment": "Air",
            "temperature": "298 K",
        }
        attribution = FakeAttributionDialogPort(
            (
                AttributionDialogResponse(
                    action="confirm",
                    sample_type="solid",
                    values=values,
                ),
                AttributionDialogResponse(
                    action="confirm",
                    sample_type="solid",
                    values=values,
                ),
                AttributionDialogResponse(
                    action="confirm",
                    sample_type="solid",
                    values=values,
                ),
                AttributionDialogResponse(
                    action="confirm",
                    sample_type="solid",
                    values=values,
                ),
                AttributionDialogResponse(
                    action="confirm",
                    sample_type="solid",
                    values=values,
                ),
            )
        )
        reviews = FakeConflictReviewDialogPort(
            (ConflictReviewResponse(action="return_to_attribution"),)
        )
        scheduled = []
        controller, _, _, _, _ = self._controller(
            attribution_dialog_port=attribution,
            conflict_review_dialog_port=reviews,
            schedule_call=scheduled.append,
        )
        controller.orchestrator.task_cache.update(
            {
                "extraction_summary": {"total_inventory_count": 3},
                "special_groups": {"stale": object()},
                "duplicate_choices": {"stale": object()},
                "excitation_pairing": {"stale": object()},
                "completeness": {"stale": object()},
                "approved_snapshot": object(),
                "output_model": object(),
                "sample_record_ids": {"stale": 1},
            }
        )

        controller._begin_attribution(
            controller.orchestrator.task_cache["extraction_summary"],
            conversion,
        )

        self.assertEqual(1, len(scheduled))
        for key in (
            "special_groups",
            "duplicate_choices",
            "excitation_pairing",
            "completeness",
            "approved_snapshot",
            "output_model",
            "sample_record_ids",
            "task7_review_complete",
        ):
            self.assertNotIn(key, controller.orchestrator.task_cache)
        self.assertEqual(
            (first.book_key, second.book_key),
            controller.orchestrator.task_cache["reopened_attribution_book_keys"],
        )

        scheduled[0]()
        target_labels = [
            request.target_label
            for request in attribution.requests
        ]

        self.assertEqual(2, target_labels.count("FolderA"))
        self.assertEqual(2, target_labels.count("FolderB"))
        self.assertEqual(1, target_labels.count("FolderC"))
        self.assertEqual(
            {first.book_key, second.book_key, unrelated.book_key},
            set(controller.orchestrator.task_cache["attribution_assignments"]),
        )
        self.assertNotIn(
            "reopened_attribution_book_keys",
            controller.orchestrator.task_cache,
        )

        def clone_session(source_session):
            clone = AttributionSession(list(source_session.targets))
            for book_key, assignment in source_session.assignments.items():
                clone.confirm(book_key, assignment)
            return clone

        def guarded_return_controller():
            queued = []
            guarded, _, _, _, _ = self._controller(
                schedule_call=queued.append,
            )
            session = clone_session(
                controller.orchestrator.task_cache["attribution_session"]
            )
            guarded.orchestrator.task_cache.update(
                {
                    "attribution_session": session,
                    "attribution_assignments": dict(session.assignments),
                    "special_groups": {"stale": object()},
                    "approved_snapshot": object(),
                    "output_model": object(),
                }
            )
            return guarded, session, queued

        guarded, guarded_session, guarded_scheduled = (
            guarded_return_controller()
        )
        guarded_before = dict(guarded.orchestrator.task_cache)
        guarded_assignments = dict(guarded_session.assignments)
        guarded._cancel_confirmation_pending = True
        guarded._return_to_attribution_from_review(
            controller.orchestrator.task_cache["extraction_summary"],
            conversion,
            (first.book_key,),
        )

        self.assertEqual(guarded_assignments, guarded_session.assignments)
        self.assertEqual(guarded_before, guarded.orchestrator.task_cache)
        self.assertEqual([], guarded_scheduled)
        self.assertEqual(
            1,
            len(guarded._deferred_cancel_confirmation_callbacks),
        )

        guarded._finish_cancel_confirmation(replay=True)

        self.assertNotIn(first.book_key, guarded_session.assignments)
        self.assertEqual(1, len(guarded_scheduled))
        for key in ("special_groups", "approved_snapshot", "output_model"):
            self.assertNotIn(key, guarded.orchestrator.task_cache)

        exiting, exiting_session, exiting_scheduled = (
            guarded_return_controller()
        )
        exiting_before = dict(exiting.orchestrator.task_cache)
        exiting_assignments = dict(exiting_session.assignments)
        exiting._cancel_confirmation_pending = True
        exiting._return_to_attribution_from_review(
            controller.orchestrator.task_cache["extraction_summary"],
            conversion,
            (first.book_key,),
        )
        exiting._mark_task_cancelled()
        exiting._finish_cancel_confirmation(replay=True)

        self.assertEqual(exiting_assignments, exiting_session.assignments)
        self.assertEqual(
            exiting_before["special_groups"],
            exiting.orchestrator.task_cache["special_groups"],
        )
        self.assertIs(
            exiting_before["approved_snapshot"],
            exiting.orchestrator.task_cache["approved_snapshot"],
        )
        self.assertIs(
            exiting_before["output_model"],
            exiting.orchestrator.task_cache["output_model"],
        )
        self.assertEqual([], exiting_scheduled)

        blocked_callbacks = []
        controller.schedule_call = blocked_callbacks.append
        controller._return_to_attribution_from_review(
            controller.orchestrator.task_cache["extraction_summary"],
            conversion,
            (first.book_key,),
        )
        self.assertEqual(1, len(blocked_callbacks))
        controller._begin_attribution = mock.Mock()
        controller._cancel_confirmation_pending = True

        blocked_callbacks[0]()

        controller._begin_attribution.assert_not_called()
        self.assertEqual(
            1,
            len(controller._deferred_cancel_confirmation_callbacks),
        )
        controller._finish_cancel_confirmation(replay=True)
        controller._begin_attribution.assert_called_once()
        controller._begin_attribution.reset_mock()
        controller.shutdown_pending = True
        controller.orchestrator.cancelled = True

        blocked_callbacks[0]()

        controller._begin_attribution.assert_not_called()

    def test_task8_final_confirmation_builds_approved_snapshot_and_output_plan_without_output_side_effects(self):
        from spectrum_organizer.core.selection import CandidateConversionResult
        from spectrum_organizer.safety.fingerprints import snapshot_sources
        from spectrum_organizer.ui.dialog_port import AttributionDialogResponse

        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = root / "unused-raw.opju"
            source.write_bytes(b"immutable raw source")
            before = tuple(snapshot_sources([source], []))
            context = types.SimpleNamespace(
                source_fingerprints_before=before,
                selected_source_paths=(source,),
                temp_root=root / "owned-temp",
                temp_root_identity=(101, 202),
                settings_snapshot={
                    "s1Limit": 1_000_000,
                    "steadyEmissionY": "S1c",
                    "allowMissingS1": False,
                },
            )
            candidate = _candidate(
                "S0001",
                source.name,
                "Emission",
                "Em270",
                "Emission 270",
                spectrum_class=SpectrumClass.STEADY_EMISSION,
                fixed_wavelength="270",
                x_values=(500, 501),
                y_values=(10, 20),
            )
            conversion = CandidateConversionResult((candidate,), (), ())
            snapshot_path = root / "run.sqlite3"
            snapshot_sha256 = _write_approval_snapshot(
                snapshot_path,
                (candidate,),
                source_snapshots=before,
            )
            final_dialog = FakeManualDialogPort("confirm")
            task8_runner = RecordingAsyncTask8Runner()
            controller, _, _, message_box, _ = self._controller(
                manual_dialog_port=final_dialog,
                task8_runner=task8_runner,
                attribution_dialog_port=FakeAttributionDialogPort(
                    (
                        AttributionDialogResponse(
                            action="confirm",
                            sample_type="solid",
                            values={
                                "sample": "MFL",
                                "state": "Solid",
                                "oxygen_environment": "Air",
                                "temperature": "298 K",
                            },
                        ),
                    )
                ),
            )
            summary = {
                "snapshot_path": str(snapshot_path),
                "snapshot_sha256": snapshot_sha256,
                "total_inventory_count": 1,
                "total_extracted_count": 1,
                "total_rejected_count": 0,
                "source_summaries": (),
            }
            controller.approved_pre_extraction_context = context
            controller.orchestrator.task_cache.update(
                {
                    "approved_pre_extraction_context": context,
                    "extraction_summary": summary,
                    "ignored_duplicate_input_paths": (
                        "C:/raw/duplicate.opju",
                    ),
                }
            )

            controller._begin_attribution(summary, conversion)
            self.assertEqual(1, len(task8_runner.pending))
            self.assertEqual([], final_dialog.requests)
            self.assertNotIn(
                "approved_snapshot",
                controller.orchestrator.task_cache,
            )

            task8_runner.succeed_next()
            self.assertEqual(1, len(task8_runner.pending))
            self.assertEqual(
                ["final_attribution_summary"],
                [request.kind for request in final_dialog.requests],
            )
            self.assertNotIn(
                "approved_snapshot",
                controller.orchestrator.task_cache,
            )

            task8_runner.succeed_next()

        approved = controller.orchestrator.task_cache["approved_snapshot"]
        plan = controller.orchestrator.task_cache["output_model"]
        self.assertEqual(
            ("F_Ex270_ExSlit2_EmSlit2_ALL_SAMPLES",),
            tuple(folder.name for folder in plan.folders),
        )
        self.assertEqual(plan, approved.output_plan)
        self.assertEqual(
            1_000_000,
            approved.settings_snapshot["s1Limit"],
        )
        self.assertEqual(
            (pathlib.Path("C:/raw/duplicate.opju"),),
            approved.ignored_duplicate_input_paths,
        )
        self.assertTrue(approved.count_reconciliation.is_closed)
        self.assertEqual(1, approved.count_reconciliation.accepted_ordinary_spectrum_count)
        self.assertEqual(1, approved.count_reconciliation.output_plan_spectrum_count)
        self.assertEqual(False, controller.orchestrator.task_cache["sample_library_persistence"])
        self.assertNotIn("sample_record_ids", controller.orchestrator.task_cache)
        self.assertNotIn("output_worker", controller.orchestrator.task_cache)
        self.assertEqual([], message_box.errors)
        self.assertEqual(["final_attribution_summary"], [request.kind for request in final_dialog.requests])
        review_request = final_dialog.requests[0]
        self.assertEqual((1, 0, 0, 1), review_request.counts)
        self.assertEqual(
            (
                "unused-raw.opju",
                "Emission",
                "Emission 270",
                "MFL-Solid-Air-298 K",
                "将写入输出计划",
            ),
            (
                review_request.rows[0].source_filename,
                review_request.rows[0].folder_path,
                review_request.rows[0].book_name,
                review_request.rows[0].attribution,
                review_request.rows[0].result,
            ),
        )
        self.assertEqual(
            "F_Ex270_ExSlit2_EmSlit2_ALL_SAMPLES",
            review_request.output_folders[0].folder_name,
        )
        self.assertIn(
            "列 1 [X] · Comment=Em",
            review_request.output_folders[0].books[0].column_order,
        )

    def test_task9_starts_output_delegate_from_the_frozen_snapshot_and_rejects_stale_callbacks(self):
        from spectrum_organizer.workflow.output_pipeline import (
            OutputStageRequest,
        )

        output_runner = RecordingAsyncOutputStageRunner()
        controller, _, _, _, _ = self._controller(
            output_parent="C:/Out",
            output_stage_runner=output_runner,
        )
        controller.output_parent = "C:/Out"
        reconciliation = types.SimpleNamespace(
            accepted_ordinary_spectrum_count=1,
            rejected_book_count=0,
            excluded_book_count=0,
        )
        approved = types.SimpleNamespace(
            snapshot_id="approved-1",
            output_plan=object(),
            source_fingerprints_before=(),
            count_reconciliation=reconciliation,
            recognized_books=(),
            attributions=(),
            accepted_spectra=(),
            rejections=(),
            exclusions=(),
        )
        draft = types.SimpleNamespace(
            reconciliation=reconciliation,
            extraction_summary={"total_inventory_count": 1},
            summary=types.SimpleNamespace(
                folder_count=1,
                book_count=1,
                column_count=3,
            ),
        )
        runtime_updates = []
        controller._runtime_update = lambda **kwargs: runtime_updates.append(kwargs)

        controller._handle_task8_seal_success(
            controller._run_generation,
            draft,
            approved,
        )

        self.assertEqual(1, len(output_runner.calls))
        self.assertEqual(
            OutputStageRequest(approved, pathlib.Path("C:/Out")),
            output_runner.calls[0],
        )
        self.assertTrue(controller.run_in_progress)
        self.assertEqual("output", runtime_updates[-1]["stage"])
        self.assertEqual(
            "正在确认 Origin 进程状态",
            runtime_updates[-1]["runtime_status"],
        )

        controller._run_generation += 1
        before = list(runtime_updates)
        output_runner.progress("verify_output")
        output_runner.succeed(types.SimpleNamespace(completion=object()))

        self.assertEqual(before, runtime_updates)

    def test_origin_process_wait_switches_main_status_to_manual_and_stops_loader(self):
        controller, _, _, _, _ = self._controller()
        runtime_updates = []
        controller._runtime_update = lambda **kwargs: runtime_updates.append(kwargs)

        for output_active, expected_stage, expected_progress in (
            (False, "source_input", 0),
            (True, "output", 92),
        ):
            with self.subTest(output_active=output_active):
                controller._output_stage_active = output_active
                runtime_updates.clear()

                controller.show_origin_process_wait()

                update = runtime_updates[-1]
                self.assertEqual(expected_stage, update["stage"])
                self.assertEqual("等待人工操作", update["phase_detail"])
                self.assertEqual("等待关闭 Origin 后重新检测", update["runtime_status"])
                self.assertEqual("manual", update["activity_mode"])
                self.assertEqual("等待关闭 Origin", update["title"])
                self.assertIn("点击弹窗中的“重新检测”", update["subtitle"])
                self.assertEqual(expected_progress, update["progress"])
                self.assertFalse(update["progress_busy"])

    def test_task9_progress_is_appended_to_live_log(self):
        output_runner = RecordingAsyncOutputStageRunner()
        controller, _, widgets, _, _ = self._controller(
            output_stage_runner=output_runner,
        )
        reconciliation = types.SimpleNamespace(
            accepted_ordinary_spectrum_count=1,
            rejected_book_count=0,
            excluded_book_count=0,
        )
        approved = types.SimpleNamespace(
            count_reconciliation=reconciliation,
        )
        controller._runtime_update = lambda **kwargs: None

        controller._handle_output_stage_progress(
            controller._run_generation,
            {"total_inventory_count": 1},
            approved,
            "verify_output",
        )

        self.assertIn(
            "独立校验输出",
            widgets["run_log"].toPlainText(),
        )

    def test_task9_cancel_after_commit_keeps_generation_and_success_callback_alive(self):
        output_runner = RecordingAsyncOutputStageRunner()
        controller, _, widgets, _, _ = self._controller(
            output_stage_runner=output_runner,
        )
        controller._output_stage_active = True
        controller.run_in_progress = True
        generation = controller._run_generation

        controller._handle_output_stage_progress(
            generation,
            {"total_inventory_count": 1},
            types.SimpleNamespace(
                count_reconciliation=types.SimpleNamespace(
                    accepted_ordinary_spectrum_count=1,
                    rejected_book_count=0,
                    excluded_book_count=0,
                )
            ),
            "committed",
        )
        controller.cancel_after_preferences()

        self.assertEqual(generation, controller._run_generation)
        self.assertFalse(controller.orchestrator.cancelled)
        self.assertFalse(output_runner.cancelled)
        self.assertIn(
            "输出已经提交，正在完成收尾",
            widgets["run_log"].toPlainText(),
        )

    def test_task9_complete_progress_does_not_regress_to_generic_output_state(self):
        controller, _, _, _, _ = self._controller()
        updates = []
        controller._runtime_update = lambda **kwargs: updates.append(kwargs)
        approved = types.SimpleNamespace(
            count_reconciliation=types.SimpleNamespace(
                accepted_ordinary_spectrum_count=1,
                rejected_book_count=0,
                excluded_book_count=0,
            )
        )

        controller._show_output_stage_progress(
            "complete",
            {"total_inventory_count": 1},
            approved,
        )

        self.assertEqual(100, updates[-1]["progress"])
        self.assertEqual("输出阶段已完成", updates[-1]["runtime_status"])
        self.assertEqual("complete", updates[-1]["stage"])

    def test_task9_commit_winning_cancel_handshake_keeps_generation_and_success_alive(self):
        class CommitWinsRunner(RecordingAsyncOutputStageRunner):
            def __init__(self):
                super().__init__()
                self.committed = False

            def cancel(self, on_stopped=None):
                del on_stopped
                self.committed = True
                return False

        output_runner = CommitWinsRunner()
        controller, _, widgets, _, _ = self._controller(
            output_stage_runner=output_runner,
        )
        controller._output_stage_active = True
        controller.run_in_progress = True
        generation = controller._run_generation

        controller.cancel_after_preferences()

        self.assertEqual(generation, controller._run_generation)
        self.assertFalse(controller.orchestrator.cancelled)
        self.assertTrue(controller._output_stage_active)
        self.assertIn(
            "输出已经提交，正在完成收尾",
            widgets["run_log"].toPlainText(),
        )

    def test_task9_cancel_cleanup_residue_blocks_exit_and_is_visible(self):
        from spectrum_organizer.workflow.output_pipeline import (
            OutputPipelineCancelled,
        )

        controller, _, _, message_box, _ = self._controller()
        controller._runtime_update = lambda **kwargs: None
        controller._output_stage_active = True
        controller.shutdown_pending = True
        error = OutputPipelineCancelled("cancelled")
        error.cleanup_result = types.SimpleNamespace(
            retained_unknown=(pathlib.Path("C:/Out/staging/unknown.bin"),)
        )
        error.cleanup_error = None

        controller._handle_output_stage_failure(
            controller._run_generation,
            types.SimpleNamespace(),
            types.SimpleNamespace(),
            error,
        )
        controller._finish_output_pending_shutdown()

        self.assertTrue(controller._shutdown_exit_blocked)
        self.assertIn("unknown.bin", controller._shutdown_error)
        self.assertTrue(message_box.errors)
        self.assertIn("unknown.bin", message_box.errors[-1][1])

    def test_task9_output_cleanup_retry_runs_before_exit_and_clears_the_guard(self):
        callbacks = []

        class OutputRunner:
            committed = False

            def retry_cleanup(self, callback):
                callbacks.append(callback)
                return True

        controller, _, _, _, _ = self._controller(
            output_stage_runner=OutputRunner(),
            start_run_runner=types.SimpleNamespace(cancel=lambda callback: False),
        )
        parent = types.SimpleNamespace(closed=False)
        parent.close = lambda: setattr(parent, "closed", True)
        controller.parent = parent
        controller._shutdown_exit_blocked = True
        controller._shutdown_cleanup_owner = "output"
        controller._shutdown_error = "Origin 子进程退出状态无法确认"

        controller.cancel_after_preferences()

        self.assertTrue(controller.shutdown_pending)
        self.assertFalse(parent.closed)
        self.assertEqual(1, len(callbacks))

        callbacks.pop()(None)

        self.assertFalse(controller._shutdown_exit_blocked)
        self.assertIsNone(controller._shutdown_cleanup_owner)
        self.assertTrue(parent.closed)

    def test_task9_failure_text_includes_exception_notes(self):
        error = RuntimeError("primary")
        error.add_note("Origin session close also failed: close blocked")

        text = app_module._exception_with_notes(error)

        self.assertIn("primary", text)
        self.assertIn("Origin session close also failed: close blocked", text)

    def test_task9_output_parent_failure_reroutes_the_same_approved_snapshot(self):
        from spectrum_organizer.reporting.publication import (
            ParentUnavailableError,
        )
        from spectrum_organizer.workflow.output_pipeline import (
            OutputPipelineFailure,
        )

        output_runner = RecordingAsyncOutputStageRunner()
        controller, _, _, message_box, _ = self._controller(
            output_parent="D:/NewOutput",
            output_stage_runner=output_runner,
        )
        controller.output_parent = "C:/Blocked"
        reconciliation = types.SimpleNamespace(
            accepted_ordinary_spectrum_count=1,
            rejected_book_count=0,
            excluded_book_count=0,
        )
        approved = types.SimpleNamespace(
            snapshot_id="approved-1",
            output_plan=object(),
            source_fingerprints_before=(),
            count_reconciliation=reconciliation,
            recognized_books=(),
            attributions=(),
            accepted_spectra=(),
            rejections=(),
            exclusions=(),
        )
        draft = types.SimpleNamespace(
            reconciliation=reconciliation,
            extraction_summary={"total_inventory_count": 1},
            summary=types.SimpleNamespace(
                folder_count=1,
                book_count=1,
                column_count=3,
            ),
        )
        controller._runtime_update = lambda **kwargs: None
        controller._handle_task8_seal_success(
            controller._run_generation,
            draft,
            approved,
        )

        output_runner.fail(
            OutputPipelineFailure(
                "publish",
                ParentUnavailableError(
                    pathlib.Path("C:/Blocked"),
                    "path is occupied by a file",
                ),
                failure_log_path=pathlib.Path("Failed_Run.txt"),
                cleanup_result=None,
            )
        )

        self.assertEqual(2, len(output_runner.calls))
        self.assertIs(
            approved,
            output_runner.calls[1].approved_snapshot,
        )
        self.assertEqual(
            pathlib.Path("D:/NewOutput"),
            output_runner.calls[1].output_parent,
        )
        self.assertEqual("D:/NewOutput", controller.output_parent)
        self.assertEqual(1, len(message_box.errors))
        self.assertIn("C:\\Blocked", message_box.errors[0][1])

    def test_task9_staging_creation_cleanup_retry_blocks_reroute_and_exit(self):
        from spectrum_organizer.reporting.publication import (
            ParentUnavailableError,
        )
        from spectrum_organizer.workflow.output_pipeline import (
            OutputPipelineFailure,
        )

        retry = lambda: None
        error = OutputPipelineFailure(
            "create_staging",
            ParentUnavailableError(
                pathlib.Path("C:/Blocked"),
                "marker write failed",
                cleanup_retry=retry,
            ),
            failure_log_path=pathlib.Path("Failed_Run.txt"),
            cleanup_result=None,
            cleanup_retry=retry,
        )

        self.assertTrue(app_module._output_cleanup_is_blocked(error))

    def test_task9_completion_actions_open_output_reset_task_and_exit_without_cancellation_dialog(self):
        opened = []
        controller, _, _, _, _ = self._controller(
            open_path=lambda path: opened.append(path),
        )
        completion = types.SimpleNamespace(
            output_path=pathlib.Path("C:/Out/Organized_Run"),
        )
        controller.orchestrator.task_cache.update(
            {
                "output_completion": completion,
                "approved_snapshot": object(),
            }
        )
        controller.selected_source_paths = ("C:/raw/source.opju",)
        controller.output_parent = "C:/Out"
        controller.approved_pre_extraction_context = object()

        controller.open_output_folder()
        controller.start_new_task()

        self.assertEqual([completion.output_path], opened)
        self.assertEqual({}, controller.orchestrator.task_cache)
        self.assertEqual((), controller.selected_source_paths)
        self.assertEqual("", controller.output_parent)
        self.assertIsNone(controller.approved_pre_extraction_context)

        class Parent:
            def __init__(self):
                self.closed = 0

            def close(self):
                self.closed += 1

        parent = Parent()
        controller.parent = parent
        controller.orchestrator.task_cache["output_completion"] = completion
        controller.approved_pre_extraction_context = object()

        controller.exit_application()

        self.assertEqual(1, parent.closed)
        self.assertIsNone(controller.approved_pre_extraction_context)
        self.assertEqual({}, controller.orchestrator.task_cache)

    def test_task9_post_commit_cleanup_error_is_visible_without_relabeling_success(self):
        controller, _, _, _, _ = self._controller()
        updates = []
        logs = []
        controller._runtime_update = lambda **kwargs: updates.append(kwargs)
        controller._log = lambda message: logs.append(message)
        reconciliation = types.SimpleNamespace(
            accepted_ordinary_spectrum_count=1,
            output_plan_spectrum_count=1,
            rejected_book_count=0,
            excluded_book_count=0,
        )
        completion = types.SimpleNamespace(
            output_path=pathlib.Path("C:/Out/Organized_Run"),
            project_path=pathlib.Path("C:/Out/Organized_Run/output.opju"),
            report_path=pathlib.Path("C:/Out/Organized_Run/report.txt"),
            project_count=1,
        )

        controller._handle_output_stage_success(
            controller._run_generation,
            types.SimpleNamespace(
                extraction_summary={"total_inventory_count": 1},
            ),
            types.SimpleNamespace(count_reconciliation=reconciliation),
            types.SimpleNamespace(
                completion=completion,
                post_commit_error=OSError("temporary cleanup blocked"),
            ),
        )

        self.assertEqual("complete", updates[-1]["stage"])
        self.assertTrue(updates[-1]["show_completion_actions"])
        self.assertTrue(updates[-1]["show_attention"])
        self.assertIn("收尾清理失败", updates[-1]["attention_message"])
        self.assertIn("输出完成", logs[0])
        self.assertIn("收尾清理失败", logs[1])
        self.assertIs(
            completion,
            controller.orchestrator.task_cache["output_completion"],
        )

    def test_committed_cleanup_must_finish_before_new_task_or_exit_discards_state(self):
        class CleanupRetryRunner:
            def __init__(self):
                self.callbacks = []

            def retry_cleanup(self, callback):
                self.callbacks.append(callback)
                return True

        def mark_pending(controller):
            controller.orchestrator.task_cache.update(
                {
                    "output_completion": object(),
                    "output_post_commit_error": OSError("temp locked"),
                    "output_post_commit_cleanup_pending": True,
                }
            )
            controller.selected_source_paths = ("C:/raw/source.opju",)
            controller.output_parent = "C:/Out"
            controller.approved_pre_extraction_context = object()
            controller._shutdown_exit_blocked = True
            controller._shutdown_cleanup_owner = "output"
            controller._output_committed = True

        new_task_runner = CleanupRetryRunner()
        controller, _, _, _, _ = self._controller(
            output_stage_runner=new_task_runner
        )
        mark_pending(controller)

        controller.start_new_task()

        self.assertEqual(("C:/raw/source.opju",), controller.selected_source_paths)
        self.assertIn(
            "output_post_commit_error",
            controller.orchestrator.task_cache,
        )
        self.assertEqual(1, len(new_task_runner.callbacks))
        new_task_runner.callbacks.pop()(None)
        self.assertEqual((), controller.selected_source_paths)
        self.assertEqual({}, controller.orchestrator.task_cache)

        exit_runner = CleanupRetryRunner()
        controller, _, _, _, _ = self._controller(
            output_stage_runner=exit_runner
        )
        mark_pending(controller)
        parent = types.SimpleNamespace(closed=False)
        parent.close = lambda: setattr(parent, "closed", True)
        controller.parent = parent

        controller.exit_application()

        self.assertFalse(parent.closed)
        self.assertIsNotNone(controller.approved_pre_extraction_context)
        self.assertEqual(1, len(exit_runner.callbacks))
        exit_runner.callbacks.pop()(None)
        self.assertTrue(parent.closed)
        self.assertIsNone(controller.approved_pre_extraction_context)
        self.assertEqual({}, controller.orchestrator.task_cache)

    def test_committed_cleanup_retry_delegates_publication_ownership_check(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            snapshot = types.SimpleNamespace(
                task_snapshot_path=root / "absent-temp" / "task.sqlite3",
                task_temp_root_identity=(101, 202),
            )
            completion = types.SimpleNamespace(post_commit_error=object())

            with mock.patch.object(
                app_module,
                "retry_post_commit_cleanup",
            ) as retry_publication, mock.patch.object(
                app_module,
                "_cleanup_temp_root_error",
                return_value=None,
            ) as cleanup_temp:
                app_module._retry_committed_output_cleanup(snapshot, completion)

            retry_publication.assert_called_once_with(completion)
            cleanup_temp.assert_called_once_with(
                snapshot.task_snapshot_path.parent,
                expected_root_identity=snapshot.task_temp_root_identity,
            )

    def test_task9_failure_dialog_exposes_cleanup_and_failure_log_errors(self):
        controller, _, _, message_box, _ = self._controller()
        controller._runtime_update = lambda **kwargs: None
        controller._output_stage_active = True
        controller.run_in_progress = True
        error = types.SimpleNamespace(
            stage="verify_output",
            cause=RuntimeError("numeric mismatch at /F/B column=B row=7"),
            failure_log_path=None,
            failure_log_error=OSError("log directory read-only"),
            cleanup_error=OSError("staging cleanup blocked"),
            cleanup_result=types.SimpleNamespace(
                retained_unknown=(pathlib.Path("C:/Out/unknown.bin"),),
            ),
        )

        controller._handle_output_stage_failure(
            controller._run_generation,
            types.SimpleNamespace(),
            types.SimpleNamespace(),
            error,
        )

        message = message_box.errors[0][1]
        self.assertIn("numeric mismatch", message)
        self.assertIn("失败日志写入失败：log directory read-only", message)
        self.assertIn("临时输出清理失败：staging cleanup blocked", message)
        self.assertIn("C:\\Out\\unknown.bin", message)
        self.assertTrue(controller._shutdown_exit_blocked)
        self.assertEqual("output", controller._shutdown_cleanup_owner)

    def test_task8_final_correction_of_earlier_review_preserves_canonical_ledger_order(self):
        from spectrum_organizer.core.selection import CandidateConversionResult
        from spectrum_organizer.safety.fingerprints import snapshot_sources
        from spectrum_organizer.ui.dialog_port import (
            AttributionDialogResponse,
            DialogResponse,
        )

        class SequencedFinalDialogPort:
            def __init__(self):
                self.actions = ["return_to_attribution", "confirm"]
                self.requests = []

            def choose(self, request):
                self.requests.append(request)
                return DialogResponse(action=self.actions.pop(0))

        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = root / "source.opju"
            source.write_bytes(b"immutable raw source")
            before = tuple(snapshot_sources([source], []))
            context = types.SimpleNamespace(
                source_fingerprints_before=before,
                selected_source_paths=(source,),
                temp_root=root / "owned-temp",
                temp_root_identity=(101, 202),
            )
            candidates = tuple(
                _candidate(
                    "S0001",
                    source.name,
                    folder,
                    f"Em{wavelength}{suffix}",
                    f"Emission {wavelength}{suffix}",
                    spectrum_class=SpectrumClass.STEADY_EMISSION,
                    fixed_wavelength=wavelength,
                    x_values=(500, 501),
                    y_values=(10, 20),
                )
                for folder, wavelength in (
                    ("A_EarlierReview", "300"),
                    ("Z_LaterReview", "350"),
                )
                for suffix in ("A", "B")
            )
            earlier_group = tuple(
                candidate.book_key for candidate in candidates[:2]
            )
            later_group = tuple(
                candidate.book_key for candidate in candidates[2:]
            )
            snapshot_path = root / "run.sqlite3"
            snapshot_sha256 = _write_approval_snapshot(
                snapshot_path,
                candidates,
                source_snapshots=before,
            )
            attribution_values = {
                "sample": "MFL",
                "state": "Solid",
                "oxygen_environment": "Air",
                "temperature": "298 K",
            }
            attribution_dialog = FakeAttributionDialogPort(
                tuple(
                    AttributionDialogResponse(
                        action="confirm",
                        sample_type="solid",
                        values=attribution_values,
                    )
                    for _ in range(3)
                )
            )
            conflict_dialog = FakeConflictReviewDialogPort(
                (
                    ConflictReviewResponse(
                        action="confirm_selection",
                        selected_book_keys=(earlier_group[0],),
                    ),
                    ConflictReviewResponse(
                        action="confirm_selection",
                        selected_book_keys=(later_group[0],),
                    ),
                    ConflictReviewResponse(
                        action="confirm_selection",
                        selected_book_keys=(earlier_group[1],),
                    ),
                )
            )
            scheduled = []
            final_dialog = SequencedFinalDialogPort()
            task8_runner = RecordingAsyncTask8Runner()
            controller, _, _, message_box, _ = self._controller(
                task8_runner=task8_runner,
                manual_dialog_port=final_dialog,
                attribution_dialog_port=attribution_dialog,
                conflict_review_dialog_port=conflict_dialog,
                schedule_call=scheduled.append,
            )
            summary = {
                "snapshot_path": str(snapshot_path),
                "snapshot_sha256": snapshot_sha256,
                "total_inventory_count": len(candidates),
                "total_extracted_count": len(candidates),
                "total_rejected_count": 0,
                "source_summaries": (),
            }
            controller.approved_pre_extraction_context = context
            controller.orchestrator.task_cache.update(
                {
                    "approved_pre_extraction_context": context,
                    "extraction_summary": summary,
                }
            )

            controller._begin_attribution(
                summary,
                CandidateConversionResult(candidates, (), ()),
            )
            controller.orchestrator.task_cache[
                "latest_attribution_decision_book_keys"
            ] = earlier_group
            task8_runner.succeed_next()
            self.assertEqual(1, len(scheduled))
            scheduled.pop(0)()
            task8_runner.succeed_next()
            task8_runner.succeed_next()

        approved = controller.orchestrator.task_cache["approved_snapshot"]
        self.assertEqual([], message_box.errors)
        self.assertEqual(
            (earlier_group, later_group),
            tuple(
                requirement.candidate_book_keys
                for requirement in approved.review_requirements
            ),
        )
        self.assertEqual(
            (earlier_group, later_group),
            tuple(
                choice.candidate_book_keys
                for choice in approved.review_choices
            ),
        )

    def test_task8_selected_row_uses_targeted_attribution_without_book_picker(self):
        from spectrum_organizer.core.selection import CandidateConversionResult
        from spectrum_organizer.safety.fingerprints import snapshot_sources
        from spectrum_organizer.ui.dialog_port import (
            AttributionDialogResponse,
            DialogResponse,
        )
        from spectrum_organizer.ui.dialogs import FinalReviewViewState

        class TargetedFinalDialogPort:
            def __init__(self, row_id, view_state):
                self.row_id = row_id
                self.view_state = view_state
                self.requests = []

            def choose(self, request):
                self.requests.append(request)
                return DialogResponse(
                    action="modify_attribution",
                    selected_row_id=self.row_id,
                    view_state=self.view_state,
                )

        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = root / "source.opju"
            source.write_bytes(b"immutable raw source")
            before = tuple(snapshot_sources([source], []))
            context = types.SimpleNamespace(
                source_fingerprints_before=before,
                selected_source_paths=(source,),
                temp_root=root / "owned-temp",
                temp_root_identity=(101, 202),
            )
            candidates = tuple(
                _candidate(
                    "S0001",
                    source.name,
                    "Emission",
                    f"Em{wavelength}",
                    f"Emission {wavelength}",
                    fixed_wavelength=wavelength,
                    x_values=(500, 501),
                    y_values=(10, 20),
                )
                for wavelength in ("270", "300")
            )
            snapshot_path = root / "run.sqlite3"
            snapshot_sha256 = _write_approval_snapshot(
                snapshot_path,
                candidates,
                source_snapshots=before,
            )
            initial_values = {
                "sample": "MFL",
                "state": "Solid",
                "oxygen_environment": "Air",
                "temperature": "298 K",
            }
            corrected_values = {
                "sample": "PFL",
                "state": "Solid",
                "oxygen_environment": "Air",
                "temperature": "77 K",
            }
            attribution_dialog = FakeAttributionDialogPort(
                (
                    AttributionDialogResponse(
                        action="confirm",
                        sample_type="solid",
                        values=initial_values,
                    ),
                    AttributionDialogResponse(
                        action="confirm",
                        sample_type="solid",
                        values=corrected_values,
                        attribution_scope="book",
                    ),
                )
            )
            view_state = FinalReviewViewState(
                active_tab="attribution",
                search_text="Emission 270",
                selected_row_id=candidates[0].book_key,
                attribution_scroll_value=17,
            )
            final_dialog = TargetedFinalDialogPort(
                candidates[0].book_key,
                view_state,
            )
            task8_runner = RecordingAsyncTask8Runner()
            controller, _, _, message_box, _ = self._controller(
                task8_runner=task8_runner,
                manual_dialog_port=final_dialog,
                attribution_dialog_port=attribution_dialog,
            )
            summary = {
                "snapshot_path": str(snapshot_path),
                "snapshot_sha256": snapshot_sha256,
                "total_inventory_count": 2,
                "total_extracted_count": 2,
                "total_rejected_count": 0,
                "source_summaries": (),
            }
            controller.approved_pre_extraction_context = context
            controller.orchestrator.task_cache.update(
                {
                    "approved_pre_extraction_context": context,
                    "extraction_summary": summary,
                }
            )

            controller._begin_attribution(
                summary,
                CandidateConversionResult(candidates, (), ()),
            )
            task8_runner.succeed_next()

            session = controller.orchestrator.task_cache[
                "attribution_session"
            ]
            targeted_request = attribution_dialog.requests[1]
            self.assertTrue(targeted_request.targeted_correction)
            self.assertEqual("folder", targeted_request.initial_scope)
            self.assertEqual(
                "Emission 270",
                targeted_request.selected_book_display_name,
            )
            self.assertEqual(
                ("Emission 270", "Emission 300"),
                targeted_request.book_display_names,
            )
            self.assertEqual([], attribution_dialog.book_requests)
            self.assertEqual(
                "PFL-Solid-Air-77 K",
                session.assignment_for(candidates[0].book_key).sample.canonical_label,
            )
            self.assertEqual(
                "MFL-Solid-Air-298 K",
                session.assignment_for(candidates[1].book_key).sample.canonical_label,
            )
            self.assertEqual(
                "book",
                session.confirmed_scope_for(candidates[0].book_key),
            )
            self.assertEqual(
                view_state,
                controller.orchestrator.task_cache[
                    "task8_final_review_view_state"
                ],
            )
            self.assertEqual(1, len(task8_runner.pending))
            self.assertEqual([], message_box.errors)

    def test_task8_selected_row_modifies_direct_conflicts_as_one_final_dialog_batch(self):
        from spectrum_organizer.core.selection import CandidateConversionResult
        from spectrum_organizer.safety.fingerprints import snapshot_sources
        from spectrum_organizer.ui.dialog_port import (
            AttributionDialogResponse,
            DialogResponse,
        )
        from spectrum_organizer.ui.dialogs import (
            FinalReviewConflictSelection,
            FinalReviewViewState,
        )

        class FinalDialogPort:
            def __init__(self, selected_row_id, replacement_key):
                self.selected_row_id = selected_row_id
                self.replacement_key = replacement_key
                self.requests = []
                self.editor_models = []

            def choose(self, request):
                self.requests.append(request)
                if len(self.requests) > 1:
                    return DialogResponse(action="confirm")
                self.assert_conflict_provider(request)
                model = request.conflict_editor_provider(
                    self.selected_row_id,
                    (),
                )
                self.editor_models.append(model)
                return DialogResponse(
                    action="modify_conflicts",
                    selected_row_id=self.selected_row_id,
                    view_state=FinalReviewViewState(
                        search_text="Emission",
                        selected_row_id=self.selected_row_id,
                        attribution_scroll_value=19,
                    ),
                    conflict_selections=(
                        FinalReviewConflictSelection(
                            model.groups[0].group_id,
                            (self.replacement_key,),
                        ),
                    ),
                )

            @staticmethod
            def assert_conflict_provider(request):
                if request.conflict_editor_provider is None:
                    raise AssertionError("missing final conflict provider")

        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = root / "source.opju"
            source.write_bytes(b"immutable raw source")
            before = tuple(snapshot_sources([source], []))
            context = types.SimpleNamespace(
                source_fingerprints_before=before,
                selected_source_paths=(source,),
                temp_root=root / "owned-temp",
                temp_root_identity=(101, 202),
            )
            candidates = tuple(
                _candidate(
                    "S0001",
                    source.name,
                    "Emission",
                    f"Em300{suffix}",
                    f"Emission 300 {suffix}",
                    spectrum_class=SpectrumClass.STEADY_EMISSION,
                    fixed_wavelength="300",
                    x_values=(500, 501),
                    y_values=(10, 20),
                )
                for suffix in ("A", "B")
            )
            snapshot_path = root / "run.sqlite3"
            snapshot_sha256 = _write_approval_snapshot(
                snapshot_path,
                candidates,
                source_snapshots=before,
            )
            attribution_dialog = FakeAttributionDialogPort(
                (
                    AttributionDialogResponse(
                        action="confirm",
                        sample_type="solid",
                        values={
                            "sample": "MFL",
                            "state": "Solid",
                            "oxygen_environment": "Air",
                            "temperature": "298 K",
                        },
                    ),
                )
            )
            initial_reviews = FakeConflictReviewDialogPort()
            final_dialog = FinalDialogPort(
                candidates[0].book_key,
                candidates[1].book_key,
            )
            task8_runner = RecordingAsyncTask8Runner()
            controller, _, _, message_box, _ = self._controller(
                task8_runner=task8_runner,
                manual_dialog_port=final_dialog,
                attribution_dialog_port=attribution_dialog,
                conflict_review_dialog_port=initial_reviews,
            )
            summary = {
                "snapshot_path": str(snapshot_path),
                "snapshot_sha256": snapshot_sha256,
                "total_inventory_count": 2,
                "total_extracted_count": 2,
                "total_rejected_count": 0,
                "source_summaries": (),
            }
            controller.approved_pre_extraction_context = context
            controller.orchestrator.task_cache.update(
                {
                    "approved_pre_extraction_context": context,
                    "extraction_summary": summary,
                }
            )

            controller._begin_attribution(
                summary,
                CandidateConversionResult(candidates, (), ()),
            )
            self.assertEqual(1, len(initial_reviews.requests))
            task8_runner.succeed_next()
            self.assertEqual(1, len(task8_runner.pending))
            self.assertEqual(1, len(initial_reviews.requests))
            task8_runner.succeed_next()
            task8_runner.succeed_next()

        approved = controller.orchestrator.task_cache["approved_snapshot"]
        self.assertEqual([], message_box.errors)
        self.assertEqual(2, len(final_dialog.requests))
        self.assertEqual(1, len(final_dialog.editor_models))
        group = final_dialog.editor_models[0].groups[0]
        self.assertEqual("single", group.selection_mode)
        self.assertEqual(
            source.name,
            dict(group.common_fields)["来源文件"],
        )
        self.assertTrue(
            all("来源文件" not in choice.detail for choice in group.choices)
        )
        self.assertEqual(
            {"仅 Book 名不同"},
            {choice.detail for choice in group.choices},
        )
        self.assertEqual(
            (candidates[0].book_key,),
            group.selected_keys,
        )
        manual_choices = tuple(
            choice
            for choice in approved.review_choices
            if choice.decision_source == "manual"
        )
        self.assertEqual(
            (candidates[1].book_key,),
            manual_choices[0].selected_book_keys,
        )

    def test_final_conflict_projection_recomputes_downstream_and_preserves_stale_choice(self):
        from spectrum_organizer.core.attribution import AttributionFields
        from spectrum_organizer.domain.models import NeatSample
        from spectrum_organizer.ui.dialogs import (
            FinalReviewConflictSelection,
        )

        candidates = _two_duplicate_special_candidates()
        assignments = {
            candidate.book_key: AttributionFields(
                sample=NeatSample("Sample", "Solid", "298 K"),
            )
            for candidate in candidates
        }

        class InitialReviews(FakeConflictReviewDialogPort):
            def choose(self, request, *, parent=None):
                self.requests.append(request)
                if request.kind == "special_conflict_batch":
                    return ConflictReviewResponse(
                        action="confirm_all_conflicts",
                        group_selections=tuple(
                            (
                                group.group_key,
                                group.choices[0].book_key,
                            )
                            for group in request.choice_groups
                        ),
                    )
                if request.kind == "special_group":
                    return ConflictReviewResponse(action="confirm_group")
                raise AssertionError(request.kind)

        reviews = InitialReviews()
        controller, _, _, _, _ = self._controller(
            conflict_review_dialog_port=reviews,
        )
        review_state = app_module._Task7ReviewState.empty()
        result = controller._review_special_groups(
            candidates,
            assignments,
            {candidate.book_key: candidate for candidate in candidates},
            review_state=review_state,
        )
        self.assertIsNotNone(result)
        special_requirement = next(
            requirement
            for requirement in review_state.completed_requirements()
            if requirement.bucket == "special_group"
        )
        review_state.special_group_choices[
            special_requirement.key
        ] = (
            "confirm_selection",
            special_requirement.book_keys,
        )

        initial = app_module._project_final_conflicts(
            candidates,
            assignments,
            review_state,
            row_id=candidates[0].book_key,
            target_book_keys=(candidates[0].book_key,),
            selections=(),
        )
        duplicate_group = next(
            group
            for group in initial.editor.groups
            if "重复 Book 冲突" in group.title
            and candidates[0].book_key
            in {choice.choice_key for choice in group.choices}
        )
        replacement = next(
            choice.choice_key
            for choice in duplicate_group.choices
            if choice.choice_key != candidates[0].book_key
        )
        changed = app_module._project_final_conflicts(
            candidates,
            assignments,
            review_state,
            row_id=candidates[0].book_key,
            target_book_keys=(candidates[0].book_key,),
            selections=(
                FinalReviewConflictSelection(
                    duplicate_group.group_id,
                    (replacement,),
                ),
            ),
        )

        stale_group = next(
            group
            for group in changed.editor.groups
            if group.selection_mode == "special_group"
        )
        self.assertFalse(changed.editor.can_confirm)
        self.assertIn(
            candidates[0].book_key,
            stale_group.stale_selected_keys,
        )
        self.assertEqual(
            len(stale_group.stale_selected_keys),
            len(stale_group.stale_choices),
        )
        stale_by_key = {
            choice.choice_key: choice
            for choice in stale_group.stale_choices
        }
        stale_choice = stale_by_key[candidates[0].book_key]
        self.assertEqual(
            app_module._visible_book_name(candidates[0]),
            stale_choice.display_name,
        )
        self.assertNotIn(
            candidates[0].book_key,
            stale_choice.display_name,
        )
        self.assertIn("nm", stale_choice.detail)
        self.assertEqual(
            "上游选择已改变，请重新确认本组",
            stale_group.warning,
        )
        current_keys = tuple(
            choice.choice_key
            for choice in stale_group.choices
        )
        completed = app_module._project_final_conflicts(
            candidates,
            assignments,
            review_state,
            row_id=candidates[0].book_key,
            target_book_keys=(candidates[0].book_key,),
            selections=(
                FinalReviewConflictSelection(
                    duplicate_group.group_id,
                    (replacement,),
                ),
                FinalReviewConflictSelection(
                    stale_group.group_id,
                    current_keys,
                    "confirm_selection",
                ),
            ),
        )
        self.assertTrue(completed.complete)
        self.assertTrue(completed.editor.can_confirm)

    def test_task8_many_cancel_continue_cycles_are_scheduled_without_recursion(self):
        from spectrum_organizer.ui.dialog_port import DialogResponse
        from spectrum_organizer.ui.dialogs import (
            FinalReviewRow,
            FinalReviewViewState,
        )

        cancel_cycles = 300

        class ContinueThenConfirmPort:
            def __init__(self):
                self.requests = []
                self.final_count = 0

            def choose(self, request):
                self.requests.append(request)
                if request.kind == "cancel_and_exit_confirmation":
                    return DialogResponse(action="继续运行")
                if request.kind == "final_attribution_summary":
                    self.final_count += 1
                    return DialogResponse(
                        action=(
                            "cancel"
                            if self.final_count <= cancel_cycles
                            else "confirm"
                        ),
                        view_state=FinalReviewViewState(
                            active_tab="output",
                            search_text="Book A",
                            selected_row_id="book-a",
                            attribution_scroll_value=37,
                            output_scroll_value=19,
                        ),
                    )
                raise AssertionError(request.kind)

        manual_dialog = ContinueThenConfirmPort()
        scheduled = []
        controller, _, _, _, _ = self._controller(
            manual_dialog_port=manual_dialog,
            schedule_call=scheduled.append,
        )
        runtime_updates = []
        controller.widgets["app_run_status"] = object()
        controller.update_runtime_view = (
            lambda **kwargs: runtime_updates.append(kwargs)
        )
        controller.run_in_progress = True
        controller._start_task8_seal = mock.Mock()
        draft = app_module._Task8ReviewDraft(
            extraction_summary={},
            conversion=types.SimpleNamespace(),
            output_spectra=(),
            output_plan=types.SimpleNamespace(),
            approved_rejections=(),
            approved_exclusions=(),
            approved_attributions=(),
            review_requirements=(),
            review_choices=(),
            reconciliation=types.SimpleNamespace(
                recognizable_book_count=1,
                rejected_book_count=0,
                excluded_book_count=0,
                accepted_ordinary_spectrum_count=1,
            ),
            summary=types.SimpleNamespace(),
            final_review_rows=(
                FinalReviewRow(
                    "book-a",
                    "source.opju",
                    "Folder",
                    "Book A",
                    "Sample-Solid-Air-298 K",
                    "将写入输出计划",
                ),
            ),
            output_folders=(),
            recognized_book_keys=("book-a",),
            recognized_books=(),
            source_ids=(),
            context=types.SimpleNamespace(),
            task_snapshot_sha256="sha256",
            task_snapshot_path=pathlib.Path("snapshot.sqlite3"),
        )

        controller._handle_task8_preparation_success(
            controller._run_generation,
            draft,
        )

        self.assertEqual(1, manual_dialog.final_count)
        self.assertEqual(1, len(scheduled))
        controller._start_task8_seal.assert_not_called()
        callback_count = 0
        maximum_pending = len(scheduled)
        while scheduled:
            callback = scheduled.pop(0)
            callback()
            callback_count += 1
            maximum_pending = max(maximum_pending, len(scheduled))
            self.assertLessEqual(
                callback_count,
                cancel_cycles,
                "Task 8 continuation did not converge",
            )

        request_kinds = tuple(
            request.kind for request in manual_dialog.requests
        )
        self.assertEqual(cancel_cycles, callback_count)
        self.assertEqual(cancel_cycles + 1, manual_dialog.final_count)
        self.assertEqual(1, maximum_pending)
        self.assertEqual(
            cancel_cycles + 1,
            request_kinds.count("final_attribution_summary"),
        )
        self.assertEqual(
            cancel_cycles,
            request_kinds.count("cancel_and_exit_confirmation"),
        )
        reopened = manual_dialog.requests[-1]
        waiting_updates = [
            update
            for update in runtime_updates
            if update.get("runtime_status") == "等待确认最终审核"
        ]
        self.assertEqual(cancel_cycles + 1, len(waiting_updates))
        self.assertTrue(
            all(
                update.get("activity_mode") == "manual"
                and update.get("progress_busy") is False
                and update.get("show_input_controls") is False
                for update in waiting_updates
            )
        )
        self.assertTrue(reopened.background_conflict_refresh)
        self.assertEqual("output", reopened.initial_view_state.active_tab)
        self.assertEqual("Book A", reopened.initial_view_state.search_text)
        self.assertEqual("book-a", reopened.initial_view_state.selected_row_id)
        controller._start_task8_seal.assert_called_once_with(
            controller._run_generation,
            draft,
        )

    def test_task8_many_targeted_attribution_cancel_continue_cycles_are_scheduled_without_recursion(self):
        from spectrum_organizer.core.attribution import (
            AttributionSession,
            AttributionTarget,
            build_attribution_fields,
        )
        from spectrum_organizer.core.selection import CandidateConversionResult
        from spectrum_organizer.ui.dialog_port import (
            AttributionDialogResponse,
            DialogResponse,
        )

        class ContinuePort:
            def __init__(self):
                self.requests = []

            def choose(self, request):
                self.requests.append(request)
                return DialogResponse(action="继续运行")

        candidate = _candidate(
            "S0001",
            "source.opju",
            "Emission",
            "BookA",
            "Book A",
        )
        attribution = build_attribution_fields(
            "solid",
            {
                "sample": "MFL",
                "state": "Solid",
                "oxygen_environment": "Air",
                "temperature": "298 K",
            },
        )
        session = AttributionSession(
            [
                AttributionTarget(
                    "book",
                    candidate.source_id,
                    candidate.folder_path,
                    (candidate.book_key,),
                )
            ]
        )
        session.confirm(candidate.book_key, attribution)
        cancel_cycles = 300

        class CancelThenReturnPort:
            def __init__(self):
                self.requests = []

            def choose(self, request, *, parent=None):
                del parent
                self.requests.append(request)
                if len(self.requests) <= cancel_cycles:
                    return AttributionDialogResponse(
                        action="cancel",
                        sample_type="solid",
                        values={
                            "sample": "PFL",
                            "state": "Solid",
                            "oxygen_environment": "Air",
                            "temperature": "77 K",
                        },
                        attribution_scope="folder",
                    )
                return AttributionDialogResponse(action="return_previous")

        attribution_dialog = CancelThenReturnPort()
        manual_dialog = ContinuePort()
        scheduled = []
        controller, _, _, _, _ = self._controller(
            manual_dialog_port=manual_dialog,
            attribution_dialog_port=attribution_dialog,
            schedule_call=scheduled.append,
        )
        controller.run_in_progress = True
        controller.orchestrator.task_cache["attribution_session"] = session
        draft = types.SimpleNamespace(
            conversion=CandidateConversionResult((candidate,), (), ()),
        )

        controller._begin_targeted_attribution_correction(
            controller._run_generation,
            draft,
            candidate.book_key,
        )

        self.assertEqual(1, len(attribution_dialog.requests))
        self.assertEqual(1, len(scheduled))
        maximum_pending = len(scheduled)
        for _cycle in range(cancel_cycles):
            callback = scheduled.pop(0)
            callback()
            maximum_pending = max(maximum_pending, len(scheduled))

        self.assertEqual(cancel_cycles + 1, len(attribution_dialog.requests))
        self.assertEqual(1, maximum_pending)
        self.assertTrue(
            all(request.targeted_correction for request in attribution_dialog.requests)
        )
        self.assertEqual(
            {"Book A"},
            {
                request.selected_book_display_name
                for request in attribution_dialog.requests
            },
        )
        reopened = attribution_dialog.requests[-1]
        self.assertEqual("solid", reopened.prefill["sample_type"])
        self.assertEqual("PFL", reopened.prefill["sample"])
        self.assertEqual("77 K", reopened.prefill["temperature"])
        self.assertEqual("folder", reopened.initial_scope)
        self.assertEqual(cancel_cycles, len(manual_dialog.requests))
        self.assertTrue(
            all(
                request.kind == "cancel_and_exit_confirmation"
                for request in manual_dialog.requests
            )
        )
        self.assertEqual(1, len(scheduled))

    def test_task8_conflict_cancel_then_continue_reopens_same_editor_draft(self):
        from spectrum_organizer.ui.dialog_port import DialogResponse
        from spectrum_organizer.ui.dialogs import (
            FinalReviewConflictSelection,
            FinalReviewRow,
            FinalReviewViewState,
        )

        preserved = (
            FinalReviewConflictSelection("group-a", ("book-b",)),
        )
        preserved_pending = (
            FinalReviewConflictSelection(
                "special-a",
                ("book-c",),
                "confirm_selection",
            ),
        )

        class ContinueThenConfirmPort:
            def __init__(self):
                self.requests = []
                self.final_count = 0

            def choose(self, request):
                self.requests.append(request)
                if request.kind == "cancel_and_exit_confirmation":
                    return DialogResponse(action="继续运行")
                if request.kind != "final_attribution_summary":
                    raise AssertionError(request.kind)
                self.final_count += 1
                if self.final_count == 1:
                    return DialogResponse(
                        action="cancel_conflicts",
                        selected_row_id="book-a",
                        view_state=FinalReviewViewState(
                            selected_row_id="book-a",
                            attribution_scroll_value=29,
                        ),
                        conflict_selections=preserved,
                        conflict_pending_selections=preserved_pending,
                        conflict_editing_group_ids=("special-a",),
                    )
                return DialogResponse(action="confirm")

        manual_dialog = ContinueThenConfirmPort()
        controller, _, _, _, _ = self._controller(
            manual_dialog_port=manual_dialog,
        )
        controller.run_in_progress = True
        controller._start_task8_seal = mock.Mock()
        draft = app_module._Task8ReviewDraft(
            extraction_summary={},
            conversion=types.SimpleNamespace(),
            output_spectra=(),
            output_plan=types.SimpleNamespace(),
            approved_rejections=(),
            approved_exclusions=(),
            approved_attributions=(),
            review_requirements=(),
            review_choices=(),
            reconciliation=types.SimpleNamespace(
                recognizable_book_count=1,
                rejected_book_count=0,
                excluded_book_count=0,
                accepted_ordinary_spectrum_count=1,
            ),
            summary=types.SimpleNamespace(),
            final_review_rows=(
                FinalReviewRow(
                    "book-a",
                    "source.opju",
                    "Folder",
                    "Book A",
                    "Sample-Solid-Air-298 K",
                    "将写入输出计划",
                    True,
                ),
            ),
            output_folders=(),
            recognized_book_keys=("book-a",),
            recognized_books=(),
            source_ids=(),
            context=types.SimpleNamespace(),
            task_snapshot_sha256="sha256",
            task_snapshot_path=pathlib.Path("snapshot.sqlite3"),
        )

        controller._handle_task8_preparation_success(
            controller._run_generation,
            draft,
        )

        self.assertEqual(
            (
                "final_attribution_summary",
                "cancel_and_exit_confirmation",
                "final_attribution_summary",
            ),
            tuple(request.kind for request in manual_dialog.requests),
        )
        reopened = manual_dialog.requests[-1]
        self.assertEqual("book-a", reopened.initial_conflict_row_id)
        self.assertEqual(preserved, reopened.initial_conflict_selections)
        self.assertEqual(
            preserved_pending,
            reopened.initial_conflict_pending_selections,
        )
        self.assertEqual(
            ("special-a",),
            reopened.initial_conflict_editing_group_ids,
        )
        self.assertEqual(
            29,
            reopened.initial_view_state.attribution_scroll_value,
        )
        controller._start_task8_seal.assert_called_once_with(
            controller._run_generation,
            draft,
        )

    def test_task8_whole_folder_attribution_change_combines_all_affected_conflicts(self):
        from spectrum_organizer.core.selection import CandidateConversionResult
        from spectrum_organizer.safety.fingerprints import snapshot_sources
        from spectrum_organizer.ui.dialog_port import (
            AttributionDialogResponse,
            DialogResponse,
        )
        from spectrum_organizer.ui.dialogs import (
            FinalReviewConflictSelection,
            FinalReviewViewState,
        )

        class FinalDialogPort:
            def __init__(self, selected_row_id):
                self.selected_row_id = selected_row_id
                self.requests = []
                self.combined_model = None

            def choose(self, request):
                self.requests.append(request)
                if len(self.requests) == 1:
                    return DialogResponse(
                        action="modify_attribution",
                        selected_row_id=self.selected_row_id,
                        view_state=FinalReviewViewState(
                            selected_row_id=self.selected_row_id,
                        ),
                    )
                if len(self.requests) == 2:
                    if not request.initial_conflict_row_id:
                        raise AssertionError(
                            "affected conflicts did not open immediately"
                        )
                    if (
                        request.conflict_back_action
                        != "discard_targeted_correction"
                    ):
                        raise AssertionError(
                            "whole-Folder conflict Back cannot restore attribution"
                        )
                    self.combined_model = request.conflict_editor_provider(
                        self.selected_row_id,
                        (),
                    )
                    return DialogResponse(
                        action="modify_conflicts",
                        selected_row_id=self.selected_row_id,
                        conflict_selections=tuple(
                            FinalReviewConflictSelection(
                                group.group_id,
                                group.selected_keys,
                                group.decision,
                            )
                            for group in self.combined_model.groups
                        ),
                    )
                return DialogResponse(action="confirm")

        class InitialConflictReviews(FakeConflictReviewDialogPort):
            def choose(self, request, *, parent=None):
                self.requests.append(request)
                if len(self.requests) > 2:
                    raise AssertionError(
                        "targeted Folder correction reopened legacy conflict dialogs"
                    )
                return self.responses.pop(0)

        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = root / "source.opju"
            source.write_bytes(b"immutable raw source")
            before = tuple(snapshot_sources([source], []))
            context = types.SimpleNamespace(
                source_fingerprints_before=before,
                selected_source_paths=(source,),
                temp_root=root / "owned-temp",
                temp_root_identity=(101, 202),
            )
            emissions = tuple(
                _candidate(
                    "S0001",
                    source.name,
                    "Mixed",
                    f"Em300{suffix}",
                    f"Emission 300 {suffix}",
                    spectrum_class=SpectrumClass.STEADY_EMISSION,
                    fixed_wavelength="300",
                    x_values=(500, 501),
                    y_values=(10, 20),
                )
                for suffix in ("A", "B")
            )
            excitations = tuple(
                _candidate(
                    "S0001",
                    source.name,
                    "Mixed",
                    f"Ex500{suffix}",
                    f"Excitation 500 {suffix}",
                    spectrum_class=SpectrumClass.STEADY_EXCITATION,
                    fixed_wavelength="500",
                    wavelength_range=("250", "400"),
                    x_values=(250, 251),
                    y_values=(8, 12),
                )
                for suffix in ("A", "B")
            )
            candidates = (*emissions, *excitations)
            snapshot_path = root / "run.sqlite3"
            snapshot_sha256 = _write_approval_snapshot(
                snapshot_path,
                candidates,
                source_snapshots=before,
            )
            attribution_dialog = FakeAttributionDialogPort(
                (
                    AttributionDialogResponse(
                        action="confirm",
                        sample_type="solid",
                        values={
                            "sample": "MFL",
                            "state": "Solid",
                            "oxygen_environment": "Air",
                            "temperature": "298 K",
                        },
                    ),
                    AttributionDialogResponse(
                        action="confirm",
                        sample_type="solid",
                        values={
                            "sample": "PFL",
                            "state": "Solid",
                            "oxygen_environment": "Air",
                            "temperature": "77 K",
                        },
                        attribution_scope="folder",
                    ),
                )
            )
            initial_reviews = InitialConflictReviews(
                (
                    ConflictReviewResponse(
                        action="confirm_selection",
                        selected_book_keys=(emissions[0].book_key,),
                    ),
                    ConflictReviewResponse(
                        action="confirm_selection",
                        selected_book_keys=(excitations[0].book_key,),
                    ),
                )
            )
            final_dialog = FinalDialogPort(emissions[0].book_key)
            task8_runner = RecordingAsyncTask8Runner()
            controller, _, _, message_box, _ = self._controller(
                task8_runner=task8_runner,
                manual_dialog_port=final_dialog,
                attribution_dialog_port=attribution_dialog,
                conflict_review_dialog_port=initial_reviews,
            )
            summary = {
                "snapshot_path": str(snapshot_path),
                "snapshot_sha256": snapshot_sha256,
                "total_inventory_count": 4,
                "total_extracted_count": 4,
                "total_rejected_count": 0,
                "source_summaries": (),
            }
            controller.approved_pre_extraction_context = context
            controller.orchestrator.task_cache.update(
                {
                    "approved_pre_extraction_context": context,
                    "extraction_summary": summary,
                }
            )

            controller._begin_attribution(
                summary,
                CandidateConversionResult(tuple(candidates), (), ()),
            )
            task8_runner.succeed_next()
            self.assertEqual(1, len(task8_runner.pending))
            task8_runner.succeed_next()
            task8_runner.succeed_next()

        self.assertEqual([], message_box.errors)
        self.assertEqual(3, len(final_dialog.requests))
        self.assertEqual(2, len(initial_reviews.requests))
        self.assertEqual(
            {"single", "multi"},
            {
                group.selection_mode
                for group in final_dialog.combined_model.groups
            },
        )

    def test_task8_preparation_updates_real_production_phase_rail_before_background_work(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6 import QtWidgets

        from spectrum_organizer.ui.qt_main_window import (
            create_production_main_window,
        )

        task8_runner = RecordingAsyncTask8Runner()
        controller, _, _, _, _ = self._controller(
            task8_runner=task8_runner,
        )
        window, widgets = create_production_main_window(
            dpi_percent=100,
            size_name="desktop",
            stage="conflict_review",
        )
        controller.parent = window
        controller.widgets = widgets
        controller.approved_pre_extraction_context = object()
        try:
            controller._begin_final_output_plan_review(
                {},
                object(),
                {},
                attribution_rows=(),
                rejections=(),
                candidate_by_key={},
            )
            QtWidgets.QApplication.processEvents()

            self.assertEqual(1, len(task8_runner.pending))
            self.assertEqual(
                "phase_text_active",
                widgets["phase_labels"]["output"].objectName(),
            )
            self.assertEqual(
                "正在后台准备最终输出审核",
                widgets["app_run_status"].text(),
            )
            self.assertEqual(
                "准备最终输出审核",
                widgets["current_task_title"].text(),
            )
        finally:
            window.close()
            QtWidgets.QApplication.processEvents()

    def test_task8_seal_updates_real_production_phase_rail_before_background_work(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6 import QtWidgets

        from spectrum_organizer.ui.qt_main_window import (
            create_production_main_window,
        )

        task8_runner = RecordingAsyncTask8Runner()
        controller, _, _, _, _ = self._controller(
            task8_runner=task8_runner,
        )
        window, widgets = create_production_main_window(
            dpi_percent=100,
            size_name="desktop",
            stage="conflict_review",
        )
        controller.parent = window
        controller.widgets = widgets
        try:
            controller._start_task8_seal(
                controller._run_generation,
                object(),
            )
            QtWidgets.QApplication.processEvents()

            self.assertEqual(1, len(task8_runner.pending))
            self.assertEqual(
                "phase_text_active",
                widgets["phase_labels"]["output"].objectName(),
            )
            self.assertEqual(
                "正在后台核对原件与审批账本",
                widgets["app_run_status"].text(),
            )
            self.assertEqual(
                "封存最终输出审核",
                widgets["current_task_title"].text(),
            )
        finally:
            window.close()
            QtWidgets.QApplication.processEvents()

    def test_task8_approved_snapshot_updates_real_production_phase_rail_without_claiming_output(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6 import QtWidgets

        from spectrum_organizer.ui.qt_main_window import (
            create_production_main_window,
        )

        controller, _, _, _, _ = self._controller()
        window, widgets = create_production_main_window(
            dpi_percent=100,
            size_name="desktop",
            stage="conflict_review",
        )
        reconciliation = types.SimpleNamespace(
            accepted_ordinary_spectrum_count=0,
            rejected_book_count=0,
            excluded_book_count=0,
        )
        approved_snapshot = types.SimpleNamespace(
            recognized_books=(),
            attributions=(),
            accepted_spectra=(),
            rejections=(),
            exclusions=(),
            count_reconciliation=reconciliation,
        )
        controller.parent = window
        controller.widgets = widgets
        controller.orchestrator.task_cache[
            "count_reconciliation"
        ] = reconciliation
        try:
            controller._show_task8_approved(
                {"total_inventory_count": 0},
                types.SimpleNamespace(
                    folder_count=0,
                    book_count=0,
                    column_count=0,
                ),
                approved_snapshot=approved_snapshot,
            )
            QtWidgets.QApplication.processEvents()

            self.assertEqual(
                "phase_text_active",
                widgets["phase_labels"]["output"].objectName(),
            )
            self.assertEqual(
                "等待人工验收",
                widgets["app_run_status"].text(),
            )
            self.assertEqual(
                "输出计划已确认",
                widgets["current_task_title"].text(),
            )
            self.assertIn(
                "尚未生成 Origin 输出",
                widgets["current_task_subtitle"].text(),
            )
        finally:
            window.close()
            QtWidgets.QApplication.processEvents()

    def test_task8_keeps_scan_identity_to_disambiguate_selected_excitations(self):
        from spectrum_organizer.core.attribution import build_attribution_fields
        from spectrum_organizer.core.output_model import build_output_plan

        attribution = build_attribution_fields(
            "solid",
            {
                "sample": "A",
                "state": "Solid",
                "oxygen_environment": "Air",
                "temperature": "298 K",
            },
        )
        first = _candidate(
            "S1",
            "source.opju",
            "Excitation",
            "ExA",
            "Excitation A",
            spectrum_class=SpectrumClass.STEADY_EXCITATION,
            fixed_wavelength="315",
            wavelength_range=("250", "400"),
            scan_increment="1",
            x_values=(250, 400),
            y_values=(1, 2),
        )
        second = _candidate(
            "S1",
            "source.opju",
            "Excitation",
            "ExB",
            "Excitation B",
            spectrum_class=SpectrumClass.STEADY_EXCITATION,
            fixed_wavelength="315",
            wavelength_range=("260", "410"),
            scan_increment="1",
            x_values=(260, 410),
            y_values=(3, 4),
        )

        plan = build_output_plan(
            tuple(
                app_module._output_spectrum_from_candidate(
                    candidate,
                    attribution,
                    selection_order=index,
                )
                for index, candidate in enumerate(
                    (first, second),
                    start=1,
                )
            )
        )

        self.assertEqual(
            (
                "A-Solid-Air-298 K_FEx315_ExStart250",
                "A-Solid-Air-298 K_FEx315_ExStart260",
            ),
            tuple(
                column.comment
                for column in plan.folders[0].books[0].raw_y_columns
            ),
        )

    def test_final_review_row_keeps_excluded_book_attribution_visible(self):
        from spectrum_organizer.core.attribution import build_attribution_fields
        from spectrum_organizer.product_runner import ApprovedAuditItem

        candidate = _candidate(
            "S1",
            "raw.opju",
            "Emission",
            "B1",
            "Visible1",
        )
        assignment = build_attribution_fields(
            "solid",
            {
                "sample": "Sample-A",
                "state": "Solid",
                "oxygen_environment": "Air",
                "temperature": "298 K",
            },
        )

        rows = app_module._final_review_rows(
            (candidate,),
            {candidate.book_key: assignment},
            accepted_book_keys=(),
            rejections=(),
            exclusions=(
                ApprovedAuditItem(
                    book_key=candidate.book_key,
                    detail="重复发射谱未被选择",
                ),
            ),
            review_requirements=(),
        )

        self.assertEqual(
            (
                "raw.opju",
                "Emission",
                "Visible1",
                "Sample-A-Solid-Air-298 K",
                "不输出：重复发射谱未被选择",
            ),
            (
                rows[0].source_filename,
                rows[0].folder_path,
                rows[0].book_name,
                rows[0].attribution,
                rows[0].result,
            ),
        )

    def test_final_review_rejected_row_hides_both_modification_actions(self):
        from spectrum_organizer.core.attribution import build_attribution_fields
        from spectrum_organizer.product_runner import ApprovedAuditItem

        candidate = _candidate(
            "S1",
            "raw.opju",
            "Emission",
            "B1",
            "Visible1",
        )
        assignment = build_attribution_fields(
            "solid",
            {
                "sample": "Sample-A",
                "state": "Solid",
                "oxygen_environment": "Air",
                "temperature": "298 K",
            },
        )

        rows = app_module._final_review_rows(
            (candidate,),
            {candidate.book_key: assignment},
            accepted_book_keys=(),
            rejections=(
                ApprovedAuditItem(
                    book_key=candidate.book_key,
                    detail="归一化最大值必须大于 0",
                ),
            ),
            exclusions=(),
            review_requirements=(
                product_runner.ApprovedReviewRequirement(
                    "emission",
                    "'manual-review'",
                    (candidate.book_key,),
                ),
            ),
        )

        self.assertEqual("拒绝，不输出：归一化最大值必须大于 0", rows[0].result)
        self.assertFalse(rows[0].can_modify_attribution)
        self.assertFalse(rows[0].has_related_conflicts)

    def test_final_review_row_falls_back_from_blank_long_name_to_short_name(self):
        candidate = _candidate(
            "S1",
            "raw.opju",
            "Emission",
            "B1",
            "   ",
        )

        rows = app_module._final_review_rows(
            (candidate,),
            {},
            accepted_book_keys=(candidate.book_key,),
            rejections=(),
            exclusions=(),
            review_requirements=(),
        )

        self.assertEqual("B1", rows[0].book_name)

    def test_final_review_rows_restore_original_source_and_page_order(self):
        from spectrum_organizer.core.selection import CandidateRejection
        from spectrum_organizer.product_runner import ApprovedAuditItem

        page_three = _candidate(
            "S0001",
            "first.opju",
            "Folder B",
            "B3",
            "Page 3",
            page_order=3,
        )
        page_one = _candidate(
            "S0001",
            "first.opju",
            "Folder A",
            "B1",
            "Page 1",
            page_order=1,
            spectrum_class=SpectrumClass.STEADY_2D,
        )
        second_source = _candidate(
            "S0002",
            "second.opju",
            "Folder A",
            "B1",
            "Second source",
            page_order=1,
        )
        rejected = CandidateRejection(
            source_id="S0001",
            source_filename="first.opju",
            page_type="worksheet",
            folder_path="Folder A",
            short_name="B2",
            display_name="Page 2",
            reason="invalid Note",
            page_order=2,
        )

        rows = app_module._final_review_rows(
            (page_three, second_source, page_one, rejected),
            {},
            accepted_book_keys=(
                page_three.book_key,
                page_one.book_key,
                second_source.book_key,
            ),
            rejections=(
                ApprovedAuditItem(
                    book_key=rejected.book_key,
                    detail="Note 无效",
                ),
            ),
            exclusions=(),
            review_requirements=(),
            source_order=("S0001", "S0002"),
        )

        self.assertEqual(
            ("Page 1", "Page 2", "Page 3", "Second source"),
            tuple(row.book_name for row in rows),
        )

    def test_review_cancel_requires_explicit_exit_confirmation(self):
        from spectrum_organizer.ui.dialog_port import DialogResponse

        class ContinueRunningPort:
            def __init__(self):
                self.requests = []

            def choose(self, request):
                self.requests.append(request)
                return DialogResponse(action="继续运行")

        manual_dialog_port = ContinueRunningPort()
        controller, _, _, _, _ = self._controller(
            manual_dialog_port=manual_dialog_port,
        )
        controller.run_in_progress = True

        controller._cancel_review()

        self.assertEqual(
            ("cancel_and_exit_confirmation",),
            tuple(request.kind for request in manual_dialog_port.requests),
        )
        self.assertTrue(controller.run_in_progress)
        self.assertFalse(controller.orchestrator.cancelled)

    def test_canonical_review_sync_removes_requirement_that_disappeared(self):
        from spectrum_organizer.core.attribution import build_attribution_fields

        candidates = tuple(
            _candidate(
                "S0001",
                "source.opju",
                "Emission",
                f"Em300{suffix}",
                f"Emission 300 {suffix}",
                fixed_wavelength="300",
            )
            for suffix in ("A", "B")
        )
        state = app_module._Task7ReviewState.empty()
        state.require(
            "emission",
            "old-review",
            tuple(candidate.book_key for candidate in candidates),
        )
        state.emission_choices["old-review"] = candidates[0].book_key
        state.remember(
            "emission",
            "old-review",
            tuple(candidate.book_key for candidate in candidates),
        )
        assignments = {
            candidate.book_key: build_attribution_fields(
                "solid",
                {
                    "sample": f"Sample-{index}",
                    "state": "Solid",
                    "oxygen_environment": "Air",
                    "temperature": "298 K",
                },
            )
            for index, candidate in enumerate(candidates, start=1)
        }

        app_module._canonicalize_task7_review_state(
            candidates,
            assignments,
            state,
        )

        self.assertEqual([], state.requirements)
        self.assertEqual({}, state.emission_choices)
        self.assertEqual([], state.history)

    def test_targeted_correction_back_restores_prior_attribution_scope(self):
        from spectrum_organizer.core.attribution import (
            AttributionSession,
            AttributionTarget,
            build_attribution_fields,
        )

        previous_session = AttributionSession(
            [AttributionTarget("book", "S0001", "Folder", ("book-a",))]
        )
        previous_session.confirm(
            "book-a",
            build_attribution_fields(
                "solid",
                {
                    "sample": "MFL",
                    "state": "Solid",
                    "oxygen_environment": "Air",
                    "temperature": "298 K",
                },
            ),
        )
        changed_session = copy.deepcopy(previous_session)
        changed_session.replace_assignments(
            ("book-a",),
            build_attribution_fields(
                "solid",
                {
                    "sample": "PFL",
                    "state": "Solid",
                    "oxygen_environment": "Air",
                    "temperature": "77 K",
                },
            ),
            scope="folder",
        )
        scheduled = []
        controller, _, _, _, _ = self._controller(
            schedule_call=scheduled.append,
        )
        controller.orchestrator.task_cache.update(
            {
                "attribution_session": changed_session,
                "attribution_assignments": dict(changed_session.assignments),
                "latest_attribution_decision_book_keys": ("book-a", "book-b"),
                "task8_targeted_attribution_rollback": (
                    app_module._Task8TargetedAttributionRollback(
                        session=previous_session,
                        latest_attribution_decision_book_keys=("prior-book",),
                    )
                ),
                "task8_final_conflict_target_book_keys": ("book-a",),
            }
        )

        controller._discard_pending_targeted_correction(
            controller._run_generation,
            object(),
        )

        self.assertIs(
            previous_session,
            controller.orchestrator.task_cache["attribution_session"],
        )
        self.assertEqual(
            ("prior-book",),
            controller.orchestrator.task_cache[
                "latest_attribution_decision_book_keys"
            ],
        )
        self.assertNotIn(
            "task8_targeted_attribution_rollback",
            controller.orchestrator.task_cache,
        )
        self.assertEqual(1, len(scheduled))

    def test_final_review_row_localizes_new_slit_and_physical_role_rejections(self):
        from spectrum_organizer.core.selection import CandidateRejection

        candidate = _candidate(
            "S1",
            "raw.opju",
            "Emission",
            "B1",
            "Visible1",
        )
        cases = (
            (
                "EX1 entrance and exit slit values conflict",
                "EX1 入口与出口狭缝数值不一致",
            ),
            (
                "selected Y and S1 resolve to the same physical column: S1c",
                "拟提取的 Y 列与 S1 指向同一物理列：S1c",
            ),
        )

        for reason, expected in cases:
            with self.subTest(reason=reason):
                rejection = CandidateRejection(
                    source_id=candidate.source_id,
                    source_filename=candidate.source_filename,
                    page_type=candidate.page_type,
                    folder_path=candidate.folder_path,
                    short_name=candidate.short_name,
                    display_name=candidate.display_name,
                    reason=reason,
                )
                rows = app_module._final_review_rows(
                    (candidate,),
                    {},
                    accepted_book_keys=(),
                    rejections=(
                        app_module._approved_audit_from_rejection(
                            rejection
                        ),
                    ),
                    exclusions=(),
                    review_requirements=(),
                )

                self.assertEqual(
                    f"拒绝，不输出：{expected}",
                    rows[0].result,
                )

    def test_task8_keeps_scientific_notation_slit_pairs_structured(self):
        from spectrum_organizer.core.attribution import build_attribution_fields
        from spectrum_organizer.core.output_model import build_output_plan

        candidate = _candidate(
            "S1",
            "source.opju",
            "Excitation",
            "Ex",
            "Excitation",
            spectrum_class=SpectrumClass.STEADY_EXCITATION,
            fixed_wavelength="315",
            excitation_slits=("1e-3", "1e-3"),
            emission_slits=("1e-3", "1e-3"),
        )
        attribution = build_attribution_fields(
            "solid",
            {
                "sample": "A",
                "state": "Solid",
                "oxygen_environment": "Air",
                "temperature": "298 K",
            },
        )

        output_spectrum = app_module._output_spectrum_from_candidate(
            candidate,
            attribution,
            selection_order=1,
        )
        plan = build_output_plan((output_spectrum,))

        self.assertEqual(
            attribution.sample.system_identity_json(),
            output_spectrum.sample_system_identity,
        )
        self.assertEqual(
            "F_Em315_ExSlit0.001_EmSlit0.001",
            plan.folders[0].name,
        )

    def test_task8_hydrates_reviewed_xy_payload_before_freezing_output_plan(self):
        from decimal import Decimal

        from spectrum_organizer.core.attribution import build_attribution_fields
        from spectrum_organizer.core.selection import CandidateConversionResult
        from spectrum_organizer.safety.fingerprints import snapshot_sources

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = pathlib.Path(temporary.name)
        snapshot_path = root / "run.sqlite3"
        source = root / "source.opju"
        source.write_bytes(b"immutable source")
        before = tuple(snapshot_sources([source], []))
        source_copy = root / "source-copy.opju"
        source_copy.write_bytes(source.read_bytes())
        snapshot = RunSnapshot(snapshot_path)
        snapshot.add_source(
            "S0001",
            source_copy,
            before[0].sha256,
            original_path=before[0].path,
            original_size_bytes=before[0].size_bytes,
            original_mtime_ns=before[0].mtime_ns,
        )
        inventory = InventoryBook(
            "S0001",
            "Emission",
            "Em270",
            "Emission 270",
            1,
            ("Note", "Data"),
            True,
            True,
        )
        result = TerminalBookResult(
            source_id="S0001",
            folder_path="Emission",
            short_name="Em270",
            display_name="Emission 270",
            page_order=1,
            spectrum_class="steady_emission",
            status="extracted",
            note_text=(
                "[EXP_FD_FILE]\n"
                "Acquisition Type = Spectral Acquisition[Emission]\n"
                "[EX1]\n"
                "Park = 270\n"
                "Front Entrance Slit = 2\n"
                "Front Exit Slit = 2\n"
                "[EM1]\n"
                "Start = 350\n"
                "End = 650\n"
                "Increment = 1\n"
                "Front Entrance Slit = 2\n"
                "Front Exit Slit = 2\n"
            ),
            data_sheet_name="Data",
            available_columns=("X", "S1c", "S1X", "S1"),
            column_metadata=(
                ("A", "X", "X"),
                ("B", "S1c", "Y"),
                ("C", "S1X", "X"),
                ("D", "S1", "Y"),
            ),
            selected_y_column="S1c",
            paired_x_column="X",
            selected_x_values=(500, 501),
            selected_y_values=(10, 20),
            s1_x_values=(500, 501),
            s1_values=(10, 20),
            selected_x_row_count=2,
            selected_y_row_count=2,
            max_planned_y=20,
            max_planned_y_x=501,
            s1_max_for_limit=20,
            s1_max_for_limit_x=501,
            s1_limit_status="ok",
            data_checksum="data-checksum",
        )
        snapshot.replace_source_partition(
            "S0001",
            [inventory],
            [result],
        )
        snapshot_sha256 = snapshot_approval_sha256(snapshot_path)
        summary = {
            "snapshot_path": str(snapshot_path),
            "snapshot_sha256": snapshot_sha256,
            "total_inventory_count": 1,
            "source_summaries": (
                {
                    "source_id": "S0001",
                    "original_path": str(source),
                },
            ),
        }
        conversion = app_module._load_candidate_conversion(
            summary,
            settings_snapshot={
                "s1Limit": 2_000_000,
                "steadyEmissionY": "S1c",
                "allowMissingS1": False,
            },
        )
        candidate = conversion.ordinary_candidates[0]
        attribution = build_attribution_fields(
            "solid",
            {
                "sample": "A",
                "state": "Solid",
                "oxygen_environment": "Air",
                "temperature": "298 K",
            },
        )
        controller, _, _, _, _ = self._controller(
            manual_dialog_port=FakeManualDialogPort("confirm"),
        )
        controller.approved_pre_extraction_context = types.SimpleNamespace(
            source_fingerprints_before=before,
            temp_root_identity=(101, 202),
        )
        controller.orchestrator.task_cache.update(
            {
                "task7_selected_book_keys": (candidate.book_key,),
                "task7_selection_exclusions": (),
            }
        )
        controller._begin_final_output_plan_review(
            summary,
            conversion,
            {candidate.book_key: attribution},
            attribution_rows=(),
            rejections=(),
            candidate_by_key={candidate.book_key: candidate},
        )
        plan = controller.orchestrator.task_cache["output_model"]
        self.assertEqual(
            (Decimal("10"), Decimal("20")),
            plan.folders[0].books[0].raw_y_columns[0].values,
        )

    def test_empty_accepted_ordinary_output_set_produces_failure_log_and_no_output_worker_launch(self):
        from spectrum_organizer.core.attribution import build_attribution_fields
        from spectrum_organizer.core.selection import CandidateConversionResult

        candidate = _candidate(
            "S0001",
            "source.opju",
            "Emission",
            "Em270",
            "Emission 270",
            spectrum_class=SpectrumClass.STEADY_EMISSION,
            fixed_wavelength="270",
            y_values=(0,),
            max_y=0,
        )
        conversion = CandidateConversionResult((candidate,), (), ())
        attribution = build_attribution_fields(
            "solid",
            {
                "sample": "MFL",
                "state": "Solid",
                "oxygen_environment": "Air",
                "temperature": "298 K",
            },
        )
        final_dialog = FakeManualDialogPort("confirm")
        controller, _, _, message_box, _ = self._controller(
            manual_dialog_port=final_dialog,
        )
        written = []
        failure_log = pathlib.Path("C:/LocalAppData/Spectrum Organizer/logs/Failed_Run_20260730_120000.txt")
        controller.failure_log_writer = lambda message: (
            written.append(message),
            failure_log,
        )[1]
        controller.approved_pre_extraction_context = types.SimpleNamespace(
            source_fingerprints_before=(),
            temp_root=pathlib.Path("C:/missing-owned-temp"),
            temp_root_identity=(101, 202),
        )
        controller.orchestrator.task_cache.update(
            {
                "task7_selected_book_keys": (candidate.book_key,),
                "task7_selection_exclusions": (),
                "sample_library_persistence": False,
                "selected_source_paths": ("C:/raw/source.opju",),
                "output_parent": "C:/output",
                "candidate_conversion": object(),
                "attribution_session": object(),
                "attribution_assignments": {
                    "same-book-key": attribution
                },
                "task7_review_state": app_module._Task7ReviewState.empty(),
                "special_groups": (),
                "duplicate_choices": {
                    "same-review-key": "same-book-key"
                },
                "excitation_pairing": {
                    "same-review-key": ("same-book-key",)
                },
            }
        )

        controller._begin_final_output_plan_review(
            {
                "snapshot_sha256": "a" * 64,
                "total_inventory_count": 1,
            },
            conversion,
            {candidate.book_key: attribution},
            attribution_rows=(),
            rejections=(),
            candidate_by_key={candidate.book_key: candidate},
        )

        self.assertEqual(1, len(written))
        self.assertIn("没有可输出的普通谱图", written[0])
        self.assertIn("无法归一化", written[0])
        self.assertEqual(
            {
                "selected_source_paths": ("C:/raw/source.opju",),
                "output_parent": "C:/output",
                "failed_run_log_path": failure_log,
            },
            controller.orchestrator.task_cache,
        )
        self.assertNotIn("output_worker", controller.orchestrator.task_cache)
        self.assertEqual([], final_dialog.requests)
        self.assertEqual("没有可输出的普通谱图", message_box.errors[0][0])
        self.assertIn(str(failure_log), message_box.errors[0][1])

    def test_no_usable_output_reports_failure_log_write_error_without_approval(self):
        controller, _, _, message_box, _ = self._controller()
        controller.failure_log_writer = mock.Mock(
            side_effect=OSError("disk denied")
        )
        controller.approved_pre_extraction_context = types.SimpleNamespace(
            temp_root=pathlib.Path("C:/missing-owned-temp"),
            temp_root_identity=(101, 202),
        )

        controller._fail_no_usable_output()

        self.assertFalse(controller.run_ready)
        self.assertEqual({}, controller.orchestrator.task_cache)
        self.assertEqual("没有可输出的普通谱图", message_box.errors[0][0])
        self.assertIn("失败日志写入失败：disk denied", message_box.errors[0][1])

    def test_overlong_book_long_name_is_rejected_before_task8(self):
        from spectrum_organizer.core.attribution import (
            build_attribution_fields,
        )
        from spectrum_organizer.safety.name_policy import (
            NamePolicyError,
        )

        with self.assertRaisesRegex(
            NamePolicyError,
            "canonical sample label exceeds",
        ):
            build_attribution_fields(
                "solution",
                {
                    "sample": "A" * 250,
                    "solvent": "C",
                    "concentration": "1 M",
                    "temperature": "298 K",
                },
            )

    def test_task8_return_to_attribution_invalidates_draft_and_reopens_assignment(self):
        from spectrum_organizer.core.attribution import (
            AttributionBook,
            AttributionSession,
            build_attribution_fields,
            build_attribution_targets,
        )
        from spectrum_organizer.core.selection import CandidateConversionResult

        candidate = _candidate(
            "S0001",
            "source.opju",
            "Emission",
            "Em270",
            "Emission 270",
            spectrum_class=SpectrumClass.STEADY_EMISSION,
            fixed_wavelength="270",
        )
        attribution = build_attribution_fields(
            "solid",
            {
                "sample": "MFL",
                "state": "Solid",
                "oxygen_environment": "Air",
                "temperature": "298 K",
            },
        )
        session = AttributionSession(
            build_attribution_targets(
                [
                    AttributionBook(
                        candidate.source_id,
                        candidate.folder_path,
                        candidate.short_name,
                    )
                ]
            )
        )
        session.confirm(candidate.book_key, attribution)
        scheduled = []
        final_dialog = FakeManualDialogPort("return_to_attribution")
        controller, _, _, _, _ = self._controller(
            manual_dialog_port=final_dialog,
            schedule_call=scheduled.append,
        )
        controller.approved_pre_extraction_context = types.SimpleNamespace(
            source_fingerprints_before=(object(),),
            temp_root_identity=(101, 202),
        )
        controller.orchestrator.task_cache.update(
            {
                "attribution_session": session,
                "attribution_assignments": dict(session.assignments),
                "task7_selected_book_keys": (candidate.book_key,),
                "task7_selection_exclusions": (),
                "latest_attribution_decision_book_keys": (
                    candidate.book_key,
                ),
                "special_groups": (),
                "duplicate_choices": {"review": candidate.book_key},
                "excitation_pairing": {},
                "approved_snapshot": object(),
                "output_model": object(),
                "count_reconciliation": object(),
                "task8_review_complete": True,
            }
        )
        conversion = CandidateConversionResult((candidate,), (), ())

        controller._begin_final_output_plan_review(
            {
                "snapshot_sha256": "a" * 64,
                "total_inventory_count": 1,
            },
            conversion,
            dict(session.assignments),
            attribution_rows=(),
            rejections=(),
            candidate_by_key={candidate.book_key: candidate},
        )

        self.assertEqual({}, session.assignments)
        self.assertEqual((candidate.book_key,), controller.orchestrator.task_cache["reopened_attribution_book_keys"])
        self.assertNotIn("approved_snapshot", controller.orchestrator.task_cache)
        self.assertNotIn("output_model", controller.orchestrator.task_cache)
        self.assertNotIn("count_reconciliation", controller.orchestrator.task_cache)
        self.assertNotIn("task8_review_complete", controller.orchestrator.task_cache)
        self.assertNotIn("duplicate_choices", controller.orchestrator.task_cache)
        self.assertEqual(1, len(scheduled))
        review_request = final_dialog.requests[0]
        self.assertEqual(candidate.book_key, review_request.rows[0].row_id)
        self.assertFalse(review_request.rows[0].has_related_conflicts)
        self.assertIn(
            "列 1 [X] · Comment=Em",
            review_request.output_folders[0].books[0].column_order,
        )

    def test_task8_special_group_summary_uses_user_facing_kind_and_book_location(self):
        candidate = _candidate(
            "S1",
            "source.opju",
            "Folder",
            "Map1",
            "Map 1",
            spectrum_class=SpectrumClass.STEADY_2D,
        )

        lines = app_module._review_decision_summary(
            (
                product_runner.ApprovedReviewChoice(
                    "special_group",
                    "1:steady_2d",
                    (candidate.book_key,),
                ),
            ),
            {candidate.book_key: candidate},
        )

        self.assertEqual(
            ("二维稳态谱整组确认：source.opju · Folder / Map 1",),
            lines,
        )

    def test_task8_approved_review_choices_preserve_every_special_decision(self):
        state = app_module._Task7ReviewState.empty()
        rejected_keys = ("book-r1", "book-r2")
        selected_keys = ("book-s1", "book-s2")
        rejected_group_key = ("steady_2d", rejected_keys)
        selected_group_key = ("delayed_2d", selected_keys)
        state.special_group_choices[rejected_group_key] = (
            "reject_group",
            (),
        )
        state.require(
            "special_group",
            rejected_group_key,
            rejected_keys,
        )
        state.remember(
            "special_group",
            rejected_group_key,
            rejected_keys,
        )
        state.special_group_choices[selected_group_key] = (
            "confirm_selection",
            ("book-s2",),
        )
        state.require(
            "special_group",
            selected_group_key,
            selected_keys,
        )
        state.remember(
            "special_group",
            selected_group_key,
            selected_keys,
        )
        state.special_duplicate_choices["duplicate-point"] = "book-s2"
        state.require(
            "special_duplicate",
            "duplicate-point",
            selected_keys,
            special_kind="delayed_2d",
        )
        state.remember(
            "special_duplicate",
            "duplicate-point",
            selected_keys,
            special_kind="delayed_2d",
        )
        state.special_overlap_choices["book-s2"] = (
            app_module.OVERLAP_CHOICES[1]
        )
        state.require(
            "special_overlap",
            "book-s2",
            ("book-s2",),
            special_kind="delayed_2d",
        )
        state.remember(
            "special_overlap",
            "book-s2",
            ("book-s2",),
            special_kind="delayed_2d",
        )

        choices = app_module._approved_review_choices_from_state(state)

        self.assertEqual(
            (
                (
                    "special_group",
                    "reject_group",
                    rejected_keys,
                    (),
                ),
                (
                    "special_group",
                    "confirm_selection",
                    selected_keys,
                    ("book-s2",),
                ),
                (
                    "special_duplicate",
                    "",
                    selected_keys,
                    ("book-s2",),
                ),
                (
                    "special_overlap",
                    "delay_time_series",
                    ("book-s2",),
                    ("book-s2",),
                ),
            ),
            tuple(
                (
                    choice.kind,
                    choice.decision,
                    choice.candidate_book_keys,
                    choice.selected_book_keys,
                )
                for choice in choices
            ),
        )

    def test_task8_required_review_is_registered_before_any_choice_history(self):
        state = app_module._Task7ReviewState.empty()
        state.require(
            "emission",
            "pending-review",
            ("book-a", "book-b"),
        )

        requirements = app_module._approved_review_requirements(
            {
                "task7_review_state": state,
                "special_groups": (),
            }
        )
        choices = app_module._approved_review_choices_from_state(state)

        self.assertEqual((), choices)
        self.assertEqual(
            (
                product_runner.ApprovedReviewRequirement(
                    "emission",
                    "'pending-review'",
                    ("book-a", "book-b"),
                ),
            ),
            requirements,
        )

    def test_task8_approved_review_choices_include_automatic_steady_2d(self):
        candidate = _candidate(
            "S0001",
            "source.opju",
            "Special",
            "Map1",
            "Map 1",
            spectrum_class=SpectrumClass.STEADY_2D,
        )
        task_cache = {
            "task7_review_state": app_module._Task7ReviewState.empty(),
            "special_groups": (
                types.SimpleNamespace(
                    kind="steady_2d",
                    book_keys=(candidate.book_key,),
                ),
            ),
        }

        choices = app_module._approved_review_choices(task_cache)

        self.assertEqual(1, len(choices))
        self.assertEqual("special_group", choices[0].kind)
        self.assertEqual("steady_2d", choices[0].subject)
        self.assertEqual("automatic", choices[0].decision_source)
        self.assertEqual(
            (candidate.book_key,),
            choices[0].candidate_book_keys,
        )

    def test_task8_source_ids_include_only_recognized_sources_in_context_order(self):
        context = types.SimpleNamespace(
            source_fingerprints_before=(object(), object()),
        )
        recognized_books = (
            types.SimpleNamespace(source_id="S0002"),
        )

        self.assertEqual(
            ("S0002",),
            app_module._approved_source_ids(
                context,
                recognized_books,
            ),
        )

    def test_task8_summary_renders_all_special_review_decision_types(self):
        first = _candidate(
            "S1",
            "source.opju",
            "Special",
            "Book1",
            "Book 1",
            spectrum_class=SpectrumClass.DELAYED_2D,
        )
        second = _candidate(
            "S1",
            "source.opju",
            "Special",
            "Book2",
            "Book 2",
            spectrum_class=SpectrumClass.DELAYED_2D,
        )
        candidates = {
            first.book_key: first,
            second.book_key: second,
        }
        group_keys = (first.book_key, second.book_key)
        choices = (
            product_runner.ApprovedReviewChoice(
                "special_group",
                "rejected",
                (),
                group_keys,
                "reject_group",
                "steady_2d",
            ),
            product_runner.ApprovedReviewChoice(
                "special_group",
                "selected",
                (second.book_key,),
                group_keys,
                "confirm_selection",
                "delayed_2d",
            ),
            product_runner.ApprovedReviewChoice(
                "special_duplicate",
                "duplicate",
                (second.book_key,),
                group_keys,
                "",
                "delayed_2d",
            ),
            product_runner.ApprovedReviewChoice(
                "special_overlap",
                "overlap",
                (second.book_key,),
                (second.book_key,),
                "delay_time_series",
                "delayed_2d",
            ),
        )

        lines = app_module._review_decision_summary(
            choices,
            candidates,
        )

        self.assertIn("二维稳态谱整组拒绝", lines[0])
        self.assertIn("二维延迟谱逐 Book 确认：保留", lines[1])
        self.assertIn("二维延迟谱相关重复选择：保留", lines[2])
        self.assertIn(
            "二维延迟谱重叠归属："
            "source.opju · Special / Book 2"
            " → 时间分辨延迟谱",
            lines[3],
        )

    def test_task8_final_return_reopens_only_latest_scope_and_keeps_unrelated_review(self):
        from spectrum_organizer.core.attribution import (
            AttributionBook,
            AttributionSession,
            build_attribution_fields,
            build_attribution_targets,
        )
        from spectrum_organizer.core.selection import CandidateConversionResult

        first = _candidate(
            "S0001",
            "source.opju",
            "First",
            "Em270",
            "Emission 270",
            fixed_wavelength="270",
        )
        second = _candidate(
            "S0001",
            "source.opju",
            "Second",
            "Em300",
            "Emission 300",
            fixed_wavelength="300",
        )
        third = _candidate(
            "S0001",
            "source.opju",
            "Third",
            "Em330",
            "Emission 330",
            fixed_wavelength="330",
        )
        attribution = build_attribution_fields(
            "solid",
            {
                "sample": "MFL",
                "state": "Solid",
                "oxygen_environment": "Air",
                "temperature": "298 K",
            },
        )
        session = AttributionSession(
            build_attribution_targets(
                [
                    AttributionBook(
                        candidate.source_id,
                        candidate.folder_path,
                        candidate.short_name,
                    )
                    for candidate in (first, second, third)
                ]
            )
        )
        session.confirm(first.book_key, attribution)
        session.confirm(second.book_key, attribution)
        session.confirm(third.book_key, attribution)
        review_state = app_module._Task7ReviewState.empty()
        review_state.require(
            "emission",
            "first-review",
            (first.book_key,),
        )
        review_state.emission_choices["first-review"] = first.book_key
        review_state.remember(
            "emission",
            "first-review",
            (first.book_key,),
        )
        review_state.require(
            "emission",
            "second-review",
            (second.book_key,),
        )
        review_state.emission_choices["second-review"] = second.book_key
        review_state.remember(
            "emission",
            "second-review",
            (second.book_key,),
        )
        review_state.require(
            "emission",
            "third-review",
            (third.book_key,),
        )
        review_state.emission_choices["third-review"] = third.book_key
        review_state.remember(
            "emission",
            "third-review",
            (third.book_key,),
        )
        scheduled = []
        controller, _, _, _, _ = self._controller(
            manual_dialog_port=FakeManualDialogPort(
                "return_to_attribution"
            ),
            schedule_call=scheduled.append,
        )
        controller.approved_pre_extraction_context = types.SimpleNamespace(
            source_fingerprints_before=(object(),),
            temp_root_identity=(101, 202),
        )
        controller.orchestrator.task_cache.update(
            {
                "attribution_session": session,
                "attribution_assignments": dict(session.assignments),
                "task7_review_state": review_state,
                "task7_selected_book_keys": (
                    first.book_key,
                    second.book_key,
                    third.book_key,
                ),
                "task7_selection_exclusions": (),
                "latest_attribution_decision_book_keys": (
                    second.book_key,
                    third.book_key,
                ),
            }
        )
        conversion = CandidateConversionResult(
            (first, second, third),
            (),
            (),
        )

        controller._begin_final_output_plan_review(
            {
                "snapshot_sha256": "a" * 64,
                "total_inventory_count": 3,
            },
            conversion,
            dict(session.assignments),
            attribution_rows=(),
            rejections=(),
            candidate_by_key={
                first.book_key: first,
                second.book_key: second,
                third.book_key: third,
            },
        )

        self.assertEqual(
            attribution,
            session.assignment_for(first.book_key),
        )
        self.assertIsNone(session.assignment_for(second.book_key))
        self.assertIsNone(session.assignment_for(third.book_key))
        self.assertEqual(
            {"first-review": first.book_key},
            review_state.emission_choices,
        )
        self.assertEqual(
            (second.book_key, third.book_key),
            controller.orchestrator.task_cache[
                "reopened_attribution_book_keys"
            ],
        )
        self.assertEqual(1, len(scheduled))

    def test_task8_failure_cleanup_removes_every_derived_approval_key(self):
        from spectrum_organizer.core.attribution import (
            AttributionBook,
            AttributionSession,
            build_attribution_fields,
            build_attribution_targets,
        )
        from spectrum_organizer.core.selection import CandidateConversionResult

        candidate = _candidate(
            "S1",
            "source.opju",
            "Emission",
            "Em270",
            "Emission 270",
            fixed_wavelength="270",
        )
        session = AttributionSession(
            build_attribution_targets(
                [
                    AttributionBook(
                        candidate.source_id,
                        candidate.folder_path,
                        candidate.short_name,
                    )
                ]
            )
        )
        session.confirm(
            candidate.book_key,
            build_attribution_fields(
                "solid",
                {
                    "sample": "MFL",
                    "state": "Solid",
                    "oxygen_environment": "Air",
                    "temperature": "298 K",
                },
            ),
        )
        controller, _, _, _, _ = self._controller()
        controller.approved_pre_extraction_context = types.SimpleNamespace(
            temp_root=None,
            temp_root_identity=None,
        )

        def fail_after_approval(*_args, **_kwargs):
            controller.orchestrator.task_cache.update(
                {
                    "approved_snapshot": object(),
                    "output_model": object(),
                    "count_reconciliation": object(),
                    "task8_review_complete": True,
                }
            )
            raise RuntimeError("runtime view failed")

        controller._begin_conflict_review = fail_after_approval

        with mock.patch.object(
            app_module,
            "_cleanup_temp_root_error",
            return_value="locked temp root",
        ):
            controller._begin_attribution(
                {"total_inventory_count": 1},
                CandidateConversionResult((candidate,), (), ()),
                resume_session=session,
            )

        for key in (
            "approved_snapshot",
            "output_model",
            "count_reconciliation",
            "task8_review_complete",
        ):
            self.assertNotIn(key, controller.orchestrator.task_cache)

    def test_task8_rejects_non_positive_raw_maximum_but_keeps_other_approved_spectra(self):
        from spectrum_organizer.core.attribution import build_attribution_fields
        from spectrum_organizer.core.selection import CandidateConversionResult
        from spectrum_organizer.safety.fingerprints import snapshot_sources

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        source = pathlib.Path(temporary.name) / "source.opju"
        source.write_bytes(b"immutable source")
        before = tuple(snapshot_sources([source], []))
        valid = _candidate(
            "S0001",
            "source.opju",
            "Emission",
            "Valid",
            "Valid",
            fixed_wavelength="270",
            x_values=(500, 501),
            y_values=(10, 20),
        )
        invalid = _candidate(
            "S0001",
            "source.opju",
            "Emission",
            "Invalid",
            "Invalid",
            fixed_wavelength="300",
            x_values=(500, 501),
            y_values=(0, 0),
            max_y=0,
        )
        attribution = build_attribution_fields(
            "solid",
            {
                "sample": "MFL",
                "state": "Solid",
                "oxygen_environment": "Air",
                "temperature": "298 K",
            },
        )
        final_dialog = FakeManualDialogPort("confirm")
        controller, _, _, message_box, _ = self._controller(
            manual_dialog_port=final_dialog,
        )
        runtime_updates = []
        controller.update_runtime_view = lambda **kwargs: (
            runtime_updates.append(kwargs)
        )
        controller.widgets["app_run_status"] = FakeLabel()
        controller.approved_pre_extraction_context = types.SimpleNamespace(
            source_fingerprints_before=before,
            temp_root_identity=(101, 202),
        )
        controller.orchestrator.task_cache.update(
            {
                "task7_selected_book_keys": (
                    valid.book_key,
                    invalid.book_key,
                ),
                "task7_selection_exclusions": (),
                "special_groups": (),
                "duplicate_choices": {},
                "excitation_pairing": {},
                "sample_library_persistence": False,
            }
        )
        conversion = CandidateConversionResult(
            (valid, invalid),
            (),
            (),
        )
        snapshot_path = pathlib.Path(temporary.name) / "run.sqlite3"
        snapshot_sha256 = _write_approval_snapshot(
            snapshot_path,
            (valid, invalid),
            source_snapshots=before,
        )

        controller._begin_final_output_plan_review(
            {
                "snapshot_path": str(snapshot_path),
                "snapshot_sha256": snapshot_sha256,
                "total_inventory_count": 2,
            },
            conversion,
            {
                valid.book_key: attribution,
                invalid.book_key: attribution,
            },
            attribution_rows=(),
            rejections=(),
            candidate_by_key={
                valid.book_key: valid,
                invalid.book_key: invalid,
            },
        )

        approved = controller.orchestrator.task_cache["approved_snapshot"]
        self.assertEqual((valid.book_key,), tuple(item.spectrum_id for item in approved.accepted_spectra))
        self.assertEqual(
            (
                (
                    invalid.book_key,
                    "拟提取 Y 列的最大值小于或等于 0，"
                    "无法归一化（最大值：0；对应 X：450）",
                ),
            ),
            tuple((item.book_key, item.detail) for item in approved.rejections),
        )
        self.assertEqual(2, approved.count_reconciliation.recognizable_book_count)
        self.assertEqual(1, approved.count_reconciliation.rejected_book_count)
        self.assertEqual(0, approved.count_reconciliation.excluded_book_count)
        self.assertEqual(1, approved.count_reconciliation.accepted_ordinary_spectrum_count)
        self.assertEqual(
            "拒绝，不输出：拟提取 Y 列的最大值小于或等于 0，"
            "无法归一化（最大值：0；对应 X：450）",
            next(
                row.result
                for row in final_dialog.requests[0].rows
                if row.row_id == invalid.book_key
            ),
        )
        self.assertIn(
            (
                "source.opju",
                "Emission / Invalid",
                "拒绝，不输出：拟提取 Y 列的最大值小于或等于 0，"
                "无法归一化（最大值：0；对应 X：450）",
            ),
            runtime_updates[-1]["review_rows"],
        )
        self.assertEqual([], message_box.errors)

    def test_task8_seals_mixed_accepted_and_original_rejection(self):
        from spectrum_organizer.core.attribution import (
            build_attribution_fields,
        )
        from spectrum_organizer.core.selection import (
            CandidateConversionResult,
            CandidateRejection,
        )
        from spectrum_organizer.safety.fingerprints import snapshot_sources

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = pathlib.Path(temporary.name)
        source = root / "source.opju"
        source.write_bytes(b"immutable source")
        before = tuple(snapshot_sources([source], []))
        accepted = _candidate(
            "S0001",
            source.name,
            "Emission",
            "Valid",
            "Valid",
            fixed_wavelength="270",
            x_values=(500, 501),
            y_values=(10, 20),
        )
        rejected = CandidateRejection(
            source_id="S0001",
            source_filename=source.name,
            page_type="worksheet",
            folder_path="Emission",
            short_name="Rejected",
            display_name="Rejected",
            reason="Note is missing spectrum class",
            payload_snapshot_path=root / "run.sqlite3",
            payload_checksum="d" * 64,
        )
        snapshot_path = root / "run.sqlite3"
        snapshot_sha256 = _write_approval_snapshot(
            snapshot_path,
            (accepted, rejected),
            source_snapshots=before,
        )
        attribution = build_attribution_fields(
            "solid",
            {
                "sample": "MFL",
                "state": "Solid",
                "oxygen_environment": "Air",
                "temperature": "298 K",
            },
        )
        controller, _, _, message_box, _ = self._controller(
            manual_dialog_port=FakeManualDialogPort("confirm"),
        )
        controller.approved_pre_extraction_context = types.SimpleNamespace(
            source_fingerprints_before=before,
            temp_root_identity=(101, 202),
        )
        controller.orchestrator.task_cache.update(
            {
                "task7_selected_book_keys": (accepted.book_key,),
                "task7_selection_exclusions": (),
            }
        )

        controller._begin_final_output_plan_review(
            {
                "snapshot_path": str(snapshot_path),
                "snapshot_sha256": snapshot_sha256,
                "total_inventory_count": 2,
            },
            CandidateConversionResult(
                (accepted,),
                (),
                (rejected,),
            ),
            {accepted.book_key: attribution},
            attribution_rows=(),
            rejections=(rejected,),
            candidate_by_key={accepted.book_key: accepted},
        )

        approved = controller.orchestrator.task_cache[
            "approved_snapshot"
        ]
        self.assertEqual(2, len(approved.recognized_books))
        self.assertEqual((rejected.book_key,), tuple(
            item.book_key for item in approved.rejections
        ))
        self.assertEqual([], message_box.errors)

    def test_task8_seals_valid_source_after_same_named_unsupported_source_is_skipped(self):
        from spectrum_organizer.core.attribution import (
            build_attribution_fields,
        )
        from spectrum_organizer.core.selection import (
            CandidateConversionResult,
        )
        from spectrum_organizer.safety.fingerprints import snapshot_sources

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = pathlib.Path(temporary.name)
        valid_source = root / "valid" / "same.opju"
        skipped_source = root / "unsupported" / "same.opju"
        valid_source.parent.mkdir()
        skipped_source.parent.mkdir()
        valid_source.write_bytes(b"valid immutable source")
        skipped_source.write_bytes(b"unsupported immutable source")
        before = tuple(
            snapshot_sources([valid_source, skipped_source], [])
        )
        accepted = _candidate(
            "S0001",
            valid_source.name,
            "Emission",
            "Valid",
            "Valid",
            fixed_wavelength="270",
            x_values=(500, 501),
            y_values=(10, 20),
        )
        snapshot_path = root / "run.sqlite3"
        snapshot_sha256 = _write_approval_snapshot(
            snapshot_path,
            (accepted,),
            source_snapshots=(before[0],),
        )
        attribution = build_attribution_fields(
            "solid",
            {
                "sample": "MFL",
                "state": "Solid",
                "oxygen_environment": "Air",
                "temperature": "298 K",
            },
        )
        controller, _, _, message_box, _ = self._controller(
            manual_dialog_port=FakeManualDialogPort("confirm"),
        )
        controller.approved_pre_extraction_context = (
            types.SimpleNamespace(
                source_fingerprints_before=before,
                temp_root_identity=(101, 202),
            )
        )
        controller.orchestrator.task_cache.update(
            {
                "task7_selected_book_keys": (accepted.book_key,),
                "task7_selection_exclusions": (),
            }
        )

        controller._begin_final_output_plan_review(
            {
                "snapshot_path": str(snapshot_path),
                "snapshot_sha256": snapshot_sha256,
                "total_inventory_count": 1,
                "source_summaries": (
                    {
                        "source_id": "S0001",
                        "original_path": str(valid_source),
                    },
                ),
                "source_input_issues": (
                    {
                        "source_id": "S0002",
                        "original_path": str(skipped_source),
                        "reason": "未检测到受支持的 Origin 原始谱图",
                        "recommendation": (
                            "请重新选择包含原始光谱 Book 的 Origin 项目文件。"
                        ),
                    },
                ),
            },
            CandidateConversionResult((accepted,), (), ()),
            {accepted.book_key: attribution},
            attribution_rows=(),
            rejections=(),
            candidate_by_key={accepted.book_key: accepted},
        )

        approved = controller.orchestrator.task_cache[
            "approved_snapshot"
        ]
        self.assertEqual(("S0001",), approved.source_ids)
        self.assertEqual(
            (before[0],),
            approved.source_fingerprints_before,
        )
        self.assertEqual(
            before,
            approved.selected_source_fingerprints_before,
        )
        self.assertEqual("same.opju", approved.recognized_books[0].source_filename)
        self.assertEqual(
            (str(skipped_source),),
            tuple(
                issue.original_path
                for issue in approved.source_input_issues
            ),
        )
        self.assertEqual([], message_box.errors)

    def test_task6_completion_copy_does_not_prompt_blocked_conflict_review(self):
        controller, _, widgets, _, _ = self._controller()
        widgets["app_run_status"] = FakeLabel()
        runtime_updates = []
        controller.update_runtime_view = lambda **kwargs: runtime_updates.append(kwargs)

        controller._show_attribution_finished(
            {"total_inventory_count": 1},
            (),
            1,
            usable_books=1,
            rejections=(),
        )

        finished = runtime_updates[-1]
        self.assertNotIn("冲突审核", finished["subtitle"])
        self.assertIn("验收", finished["subtitle"])

    def test_surviving_steady_2d_candidate_receives_task_local_attribution(self):
        from spectrum_organizer.core.selection import CandidateConversionResult
        from spectrum_organizer.ui.dialog_port import AttributionDialogResponse

        special = _candidate("S1", "source.opj", "Map_RT", "Map1", "二维稳态谱")
        conversion = CandidateConversionResult((), (special,), ())
        dialogs = FakeAttributionDialogPort(
            (
                AttributionDialogResponse(
                    action="confirm",
                    sample_type="solid",
                    values={
                        "sample": "MFL",
                        "state": "Solid",
                        "oxygen_environment": "Air",
                        "temperature": "RT",
                    },
                ),
            )
        )
        controller, _, _, _, _ = self._controller(
            attribution_dialog_port=dialogs,
            candidate_loader=lambda summary: conversion,
        )
        summary = {
            "total_inventory_count": 1,
            "total_extracted_count": 1,
            "total_rejected_count": 0,
            "source_summaries": (),
        }
        controller.orchestrator.task_cache["extraction_summary"] = summary

        controller._begin_attribution(summary)

        assignments = controller.orchestrator.task_cache["attribution_assignments"]
        self.assertEqual((special.book_key,), tuple(assignments))
        self.assertEqual((special,), controller.orchestrator.task_cache["candidate_conversion"].steady_2d_candidates)
        self.assertEqual("Map_RT", dialogs.requests[0].target_label)

    def test_candidate_snapshot_validation_runs_before_metadata_conversion(self):
        summary = {
            "snapshot_path": "C:/owned/run.sqlite3",
            "snapshot_sha256": "a" * 64,
            "source_summaries": (
                {"source_id": "S1", "original_path": "C:/raw/source.opj"},
            ),
        }

        with mock.patch.object(
            app_module,
            "load_book_results_read_only",
            side_effect=ReconciliationError("Book payload checksum mismatch for source S1"),
        ) as load:
            with self.assertRaisesRegex(ReconciliationError, "checksum"):
                app_module._load_candidate_conversion(
                    summary,
                    settings_snapshot={
                        "s1Limit": 2000000,
                        "steadyEmissionY": "S1c",
                        "allowMissingS1": True,
                    },
                )

        load.assert_called_once_with(
            pathlib.Path("C:/owned/run.sqlite3"),
            expected_snapshot_sha256="a" * 64,
            source_ids=("S1",),
            cancel_check=None,
            s1_limit=2000000,
            steady_emission_y="S1c",
            allow_missing_s1=True,
        )

    def test_production_candidate_adapter_converts_multiple_real_reconciled_sources(self):
        with tempfile.TemporaryDirectory() as raw_root:
            root = pathlib.Path(raw_root)
            snapshot_path = root / "run.sqlite3"
            snapshot = RunSnapshot(snapshot_path)
            root_book = InventoryBook(
                source_id="S1",
                folder_path="/",
                short_name="F270",
                display_name="MFL emission",
                page_order=1,
                sheet_names=("Note", "Data"),
                has_note=True,
                has_data=True,
            )
            root_result = TerminalBookResult(
                source_id="S1",
                folder_path="/",
                short_name="F270",
                status="extracted",
                note_text=(
                    "[EXP_FD_FILE]\nAcquisition Type = Spectral Acquisition[Emission]\n"
                    "[EX1]\nPark = 270\nFront Entrance Slit = 2\nFront Exit Slit = 2\n"
                    "[EM1]\nStart = 300\nEnd = 650\nIncrement = 1\n"
                    "Front Entrance Slit = 2\nFront Exit Slit = 2"
                ),
                display_name="MFL emission",
                page_order=1,
                spectrum_class="steady_emission",
                data_sheet_name="Data",
                available_columns=("X", "S1c", "S1X", "S1"),
                column_metadata=(
                    ("A", "X", "X"),
                    ("B", "S1c", "Y"),
                    ("C", "S1X", "X"),
                    ("D", "S1", "Y"),
                ),
                selected_y_column="S1c",
                paired_x_column="X",
                selected_x_values=(300.0, 301.0),
                selected_y_values=(10.0, 12.0),
                s1_x_values=(300.0, 301.0),
                s1_values=(100.0, 80.0),
                selected_x_row_count=2,
                selected_y_row_count=2,
                max_planned_y=12.0,
                max_planned_y_x=301.0,
                s1_max_for_limit=100.0,
                s1_max_for_limit_x=300.0,
                s1_limit_status="ok",
                data_checksum="s1-checksum",
            )
            rejected_book = InventoryBook(
                source_id="S1",
                folder_path="Rejected_RT",
                short_name="F300",
                display_name="Rejected emission",
                page_order=2,
                sheet_names=("Note", "Data"),
                has_note=True,
                has_data=True,
            )
            rejected_result = TerminalBookResult(
                source_id="S1",
                folder_path="Rejected_RT",
                short_name="F300",
                status="rejected",
                note_text=(
                    "[EXP_FD_FILE]\nAcquisition Type = Spectral Acquisition[Emission]\n"
                    "Excitation Wavelength = 300\nEmission Range = 320 - 700\n"
                    "Emission Increment = 1"
                ),
                rejection_reason="S1 max exceeds limit",
                display_name="Rejected emission",
                page_order=2,
                spectrum_class="steady_emission",
                available_columns=("X", "S1c", "S1X", "S1"),
                column_metadata=(
                    ("A", "X", "X"),
                    ("B", "S1c", "Y"),
                    ("C", "S1X", "X"),
                    ("D", "S1", "Y"),
                ),
                s1_x_values=(320.0, 321.0),
                s1_values=(3_000_000.0, 2_500_000.0),
                s1_max_for_limit=3_000_000.0,
                s1_max_for_limit_x=320.0,
                s1_limit_status="exceeds_limit",
                data_checksum="rejected-checksum",
            )
            second_book = InventoryBook(
                source_id="S2",
                folder_path="PFL_77K",
                short_name="F315",
                display_name="PFL emission",
                page_order=1,
                sheet_names=("Note", "Data"),
                has_note=True,
                has_data=True,
            )
            second_result = TerminalBookResult(
                source_id="S2",
                folder_path="PFL_77K",
                short_name="F315",
                status="extracted",
                note_text=(
                    "[EXP_FD_FILE]\nAcquisition Type = Spectral Acquisition[Emission]\n"
                    "[EX1]\nPark = 315\nFront Entrance Slit = 2\nFront Exit Slit = 2\n"
                    "[EM1]\nStart = 350\nEnd = 700\nIncrement = 1\n"
                    "Front Entrance Slit = 2\nFront Exit Slit = 2"
                ),
                display_name="PFL emission",
                page_order=1,
                spectrum_class="steady_emission",
                data_sheet_name="Data",
                available_columns=("X", "S1c", "S1X", "S1"),
                column_metadata=(
                    ("A", "X", "X"),
                    ("B", "S1c", "Y"),
                    ("C", "S1X", "X"),
                    ("D", "S1", "Y"),
                ),
                selected_y_column="S1c",
                paired_x_column="X",
                selected_x_values=(350.0, 351.0),
                selected_y_values=(8.0, 11.0),
                s1_x_values=(350.0, 351.0),
                s1_values=(90.0, 70.0),
                selected_x_row_count=2,
                selected_y_row_count=2,
                max_planned_y=11.0,
                max_planned_y_x=351.0,
                s1_max_for_limit=90.0,
                s1_max_for_limit_x=350.0,
                s1_limit_status="ok",
                data_checksum="s2-checksum",
            )
            snapshot.add_source("S1", root / "owned-copy.opj", "sha256")
            snapshot.record_book_transaction("S1", root_book, root_result)
            snapshot.record_book_transaction("S1", rejected_book, rejected_result)
            snapshot.add_source("S2", root / "owned-copy-two.opj", "sha256-two")
            snapshot.record_book_transaction("S2", second_book, second_result)
            summary = {
                "snapshot_path": str(snapshot_path),
                "snapshot_sha256": snapshot_approval_sha256(snapshot_path),
                "source_summaries": (
                    {"source_id": "S1", "original_path": "D:/new-fixtures/unseen-source.opj"},
                    {"source_id": "S2", "original_path": "E:/other/new-source.opju"},
                ),
            }

            conversion = app_module._load_candidate_conversion(summary)

            self.assertEqual(2, len(conversion.ordinary_candidates))
            self.assertEqual(
                (("unseen-source.opj", "/", "F270"), ("new-source.opju", "PFL_77K", "F315")),
                tuple(
                    (candidate.source_filename, candidate.folder_path, candidate.short_name)
                    for candidate in conversion.ordinary_candidates
                ),
            )
            for candidate in conversion.ordinary_candidates:
                self.assertEqual((), candidate.x_values)
                self.assertEqual((), candidate.y_values)
                self.assertEqual(snapshot_path.resolve(), candidate.payload_snapshot_path)
            self.assertEqual(1, len(conversion.rejections))
            self.assertEqual("S1 max exceeds limit", conversion.rejections[0].reason)

    def test_candidate_snapshot_read_failure_aborts_attribution_and_restores_retry_ui(self):
        def fail_candidate_load(_summary):
            raise sqlite3.DatabaseError("snapshot unreadable")

        controller, _, widgets, message_box, _ = self._controller(candidate_loader=fail_candidate_load)
        context = types.SimpleNamespace(
            temp_root=pathlib.Path("C:/owned/run"),
            temp_root_identity=(101, 202),
        )
        controller.approved_pre_extraction_context = context
        controller.orchestrator.task_cache["approved_pre_extraction_context"] = context
        widgets["app_run_status"] = FakeLabel()
        runtime_updates = []
        controller.update_runtime_view = lambda **kwargs: runtime_updates.append(kwargs)
        controller.run_ready = True
        summary = {
            "total_inventory_count": 1,
            "total_extracted_count": 1,
            "total_rejected_count": 0,
            "source_summaries": (),
        }

        with mock.patch.object(app_module, "_cleanup_temp_root_error", return_value=None) as cleanup:
            controller._begin_attribution(summary)

        cleanup.assert_called_once_with(
            context.temp_root,
            expected_root_identity=context.temp_root_identity,
        )
        self.assertIsNone(controller.approved_pre_extraction_context)
        self.assertNotIn("approved_pre_extraction_context", controller.orchestrator.task_cache)
        self.assertNotIn("extraction_summary", controller.orchestrator.task_cache)
        self.assertFalse(controller.run_ready)
        self.assertEqual("snapshot unreadable", controller.orchestrator.last_failure)
        self.assertEqual([("样品归属准备失败", "snapshot unreadable")], message_box.errors)
        self.assertEqual("source_input", runtime_updates[-1]["stage"])
        self.assertTrue(runtime_updates[-1]["show_input_controls"])

    def test_candidate_snapshot_read_failure_blocks_retry_when_owned_temp_cleanup_fails(self):
        def fail_candidate_load(_summary):
            raise sqlite3.DatabaseError("snapshot unreadable")

        controller, _, _, message_box, _ = self._controller(candidate_loader=fail_candidate_load)
        context = types.SimpleNamespace(
            temp_root=pathlib.Path("C:/owned/run"),
            temp_root_identity=(101, 202),
        )
        controller.approved_pre_extraction_context = context
        controller.orchestrator.task_cache["approved_pre_extraction_context"] = context
        summary = {
            "total_inventory_count": 1,
            "total_extracted_count": 1,
            "total_rejected_count": 0,
            "source_summaries": (),
        }
        controller.orchestrator.task_cache["extraction_summary"] = summary

        with mock.patch.object(app_module, "_cleanup_temp_root_error", return_value="locked"):
            controller._begin_attribution(summary)

        self.assertIs(context, controller.approved_pre_extraction_context)
        self.assertTrue(controller._shutdown_exit_blocked)
        self.assertFalse(controller.run_ready)
        self.assertEqual(
            [("样品归属准备失败", "snapshot unreadable\n临时文件清理失败：locked")],
            message_box.errors,
        )

    def test_unexpected_attribution_dialog_failure_cleans_owned_state_and_restores_retry_ui(self):
        from spectrum_organizer.core.selection import CandidateConversionResult
        from spectrum_organizer.ui.dialog_port import AttributionBookSelectionResponse

        conversion = CandidateConversionResult(
            (_candidate("S1", "source.opj", "/", "RootF", "Root F"),),
            (),
            (),
        )
        dialogs = mock.Mock()
        dialogs.choose_book.return_value = AttributionBookSelectionResponse(
            action="select_book",
            book_key=conversion.ordinary_candidates[0].book_key,
        )
        dialogs.choose.side_effect = RuntimeError("dialog backend failed")
        controller, _, widgets, message_box, _ = self._controller(
            attribution_dialog_port=dialogs,
            candidate_loader=lambda _summary: conversion,
        )
        context = types.SimpleNamespace(
            temp_root=pathlib.Path("C:/owned/run"),
            temp_root_identity=(101, 202),
        )
        controller.approved_pre_extraction_context = context
        controller.orchestrator.task_cache["approved_pre_extraction_context"] = context
        widgets["app_run_status"] = FakeLabel()
        summary = {
            "total_inventory_count": 1,
            "total_extracted_count": 1,
            "total_rejected_count": 0,
            "source_summaries": (),
        }
        controller.orchestrator.task_cache["extraction_summary"] = summary
        runtime_updates = []
        controller.update_runtime_view = lambda **kwargs: runtime_updates.append(kwargs)

        with mock.patch.object(app_module, "_cleanup_temp_root_error", return_value=None) as cleanup:
            controller._begin_attribution(summary)

        cleanup.assert_called_once_with(
            context.temp_root,
            expected_root_identity=context.temp_root_identity,
        )
        self.assertIsNone(controller.approved_pre_extraction_context)
        self.assertEqual({}, controller.orchestrator.task_cache)
        self.assertEqual("dialog backend failed", controller.orchestrator.last_failure)
        self.assertEqual([("样品归属准备失败", "dialog backend failed")], message_box.errors)
        self.assertEqual("source_input", runtime_updates[-1]["stage"])

    def test_background_candidate_result_is_not_loaded_again_on_success(self):
        from spectrum_organizer.core.selection import CandidateConversionResult

        conversion = CandidateConversionResult((), (), ())
        candidate_loader = mock.Mock(side_effect=AssertionError("candidate conversion loaded twice"))
        scheduled = []
        controller, _, _, _, _ = self._controller(
            candidate_loader=candidate_loader,
            schedule_call=scheduled.append,
        )
        controller.selected_source_paths = ("C:/raw/source.opj",)
        summary = {
            "total_inventory_count": 0,
            "total_extracted_count": 0,
            "total_rejected_count": 0,
            "source_summaries": (),
        }

        controller._handle_start_run_success(
            0,
            (
                types.SimpleNamespace(
                    temp_root=None,
                    temp_root_identity=None,
                ),
                summary,
                conversion,
            ),
        )

        candidate_loader.assert_not_called()
        self.assertEqual(1, len(scheduled))
        controller._begin_attribution = mock.Mock()

        scheduled[0]()

        controller._begin_attribution.assert_called_once_with(
            summary,
            conversion,
        )

    def test_attribution_callback_waits_until_cancel_confirmation_continues(self):
        holder = {}
        inside_confirmation = []

        class ReentrantManualDialogPort:
            def choose(self, _request):
                from spectrum_organizer.ui.dialog_port import DialogResponse

                holder["inside"] = True
                holder["controller"]._begin_attribution_if_current(0, {}, None)
                holder["inside"] = False
                return DialogResponse(action="继续运行")

        controller, _, _, _, _ = self._controller(
            manual_dialog_port=ReentrantManualDialogPort(),
        )
        holder.update(controller=controller, inside=False)
        controller._begin_attribution = lambda *_args: inside_confirmation.append(holder["inside"])

        controller.cancel_after_preferences()

        self.assertEqual([False], inside_confirmation)
        self.assertFalse(controller.orchestrator.cancelled)

    def test_attribution_callback_is_discarded_when_cancel_confirmation_exits(self):
        holder = {}
        attribution_calls = []

        class ReentrantManualDialogPort:
            def choose(self, _request):
                from spectrum_organizer.ui.dialog_port import DialogResponse

                holder["controller"]._begin_attribution_if_current(0, {}, None)
                return DialogResponse(action="取消并退出")

        controller, _, _, _, _ = self._controller(
            manual_dialog_port=ReentrantManualDialogPort(),
        )
        holder["controller"] = controller
        controller._begin_attribution = lambda *_args: attribution_calls.append(True)

        controller.cancel_after_preferences()

        self.assertEqual([], attribution_calls)
        self.assertTrue(controller.orchestrator.cancelled)

    def test_pre_origin_bridge_waits_until_cancel_confirmation_continues(self):
        controller, _, _, _, _ = self._controller()
        callback_calls = []
        deferred = threading.Event()
        bridge = self._qt_blocking_ui_call(lambda: callback_calls.append("origin-gate"))
        bridge.set_cancel_confirmation_guard(
            defer=lambda callback: deferred.set() or controller._defer_during_cancel_confirmation(callback),
            cancelled=lambda: controller.orchestrator.cancelled,
        )
        controller._cancel_confirmation_pending = True
        outcome = {}

        def call_bridge():
            try:
                outcome["result"] = bridge()
            except Exception as exc:
                outcome["error"] = exc

        worker = threading.Thread(target=call_bridge)
        worker.start()
        self.assertTrue(deferred.wait(1.0))
        self.assertTrue(worker.is_alive())
        self.assertEqual([], callback_calls)

        controller._finish_cancel_confirmation(replay=True)
        worker.join(1.0)

        self.assertFalse(worker.is_alive())
        self.assertEqual(["origin-gate"], callback_calls)
        self.assertNotIn("error", outcome)

    def test_pre_origin_bridge_unblocks_without_origin_when_confirmation_cancels(self):
        controller, _, _, _, _ = self._controller()
        callback_calls = []
        deferred = threading.Event()
        bridge = self._qt_blocking_ui_call(lambda: callback_calls.append("origin-gate"))
        bridge.set_cancel_confirmation_guard(
            defer=lambda callback: deferred.set() or controller._defer_during_cancel_confirmation(callback),
            cancelled=lambda: controller.orchestrator.cancelled,
        )
        controller._cancel_confirmation_pending = True
        outcome = {}

        def call_bridge():
            try:
                bridge()
            except Exception as exc:
                outcome["error"] = exc

        worker = threading.Thread(target=call_bridge)
        worker.start()
        self.assertTrue(deferred.wait(1.0))

        controller._mark_task_cancelled()
        controller._finish_cancel_confirmation(replay=True)
        worker.join(1.0)

        self.assertFalse(worker.is_alive())
        self.assertEqual([], callback_calls)
        self.assertIsInstance(outcome.get("error"), app_module.ProductRunnerError)
        self.assertEqual("谱图数据提取已取消", str(outcome["error"]))

    def test_cleanup_block_arriving_inside_cancel_confirmation_blocks_exit_until_retry(self):
        from spectrum_organizer.ui.dialog_port import DialogResponse

        holder = {}
        cleanup_callbacks = []

        class ReentrantManualDialogPort:
            def __init__(self):
                self.requests = []

            def choose(self, request):
                self.requests.append(request)
                if request.kind == "cancel_and_exit_confirmation":
                    controller = holder["controller"]
                    controller._handle_start_run_failure(
                        controller._run_generation,
                        app_module.ExtractionCleanupBlockedError("probe cleanup blocked"),
                    )
                    return DialogResponse(action="取消并退出")
                return DialogResponse(action="acknowledge")

        class CleanupRetryRunner:
            def retry_cleanup(self, callback):
                cleanup_callbacks.append(callback)
                return True

        controller, _, _, _, _ = self._controller(
            start_run_runner=CleanupRetryRunner(),
            manual_dialog_port=ReentrantManualDialogPort(),
        )
        holder["controller"] = controller
        parent = types.SimpleNamespace(closed=False)
        parent.close = lambda: setattr(parent, "closed", True)
        controller.parent = parent
        controller.run_in_progress = True

        controller.cancel_after_preferences()

        self.assertFalse(parent.closed)
        self.assertTrue(controller.shutdown_pending)
        self.assertTrue(controller._shutdown_exit_blocked)
        self.assertEqual("probe cleanup blocked", controller._shutdown_error)
        self.assertEqual(1, len(cleanup_callbacks))

        cleanup_callbacks.pop()(None)

        self.assertTrue(parent.closed)
        self.assertFalse(controller.shutdown_pending)
        self.assertFalse(controller._shutdown_exit_blocked)

    def test_queued_attribution_is_ignored_after_task_cancellation(self):
        from spectrum_organizer.core.selection import CandidateConversionResult

        conversion = CandidateConversionResult(
            (_candidate("S1", "source.opj", "/", "RootF", "Root F"),),
            (),
            (),
        )
        queued = []
        dialogs = FakeAttributionDialogPort()
        controller, _, _, _, _ = self._controller(
            attribution_dialog_port=dialogs,
            candidate_loader=lambda _summary: conversion,
            schedule_call=queued.append,
        )
        summary = {
            "total_inventory_count": 1,
            "total_extracted_count": 1,
            "total_rejected_count": 0,
            "source_summaries": (),
        }

        controller._handle_start_run_success(
            controller._run_generation,
            (
                types.SimpleNamespace(
                    temp_root=None,
                    temp_root_identity=None,
                ),
                summary,
                conversion,
            ),
        )
        self.assertEqual(1, len(queued))

        controller.cancel_after_preferences()
        queued.pop()()

        self.assertEqual([], dialogs.requests)
        self.assertNotIn("attribution_assignments", controller.orchestrator.task_cache)

    def test_queued_attribution_is_ignored_while_cancel_cleanup_is_blocked(self):
        from spectrum_organizer.core.selection import CandidateConversionResult

        conversion = CandidateConversionResult(
            (_candidate("S1", "source.opj", "/", "RootF", "Root F"),),
            (),
            (),
        )
        queued = []
        dialogs = FakeAttributionDialogPort()
        controller, _, _, _, _ = self._controller(
            attribution_dialog_port=dialogs,
            candidate_loader=lambda _summary: conversion,
            schedule_call=queued.append,
        )
        summary = {
            "total_inventory_count": 1,
            "total_extracted_count": 1,
            "total_rejected_count": 0,
            "source_summaries": (),
        }
        generation = controller._run_generation
        context = types.SimpleNamespace(
            temp_root=pathlib.Path("C:/owned/run"),
            temp_root_identity=(101, 202),
        )

        controller._handle_start_run_success(generation, (context, summary, conversion))
        self.assertEqual(1, len(queued))

        with mock.patch.object(app_module, "_cleanup_temp_root_error", return_value="locked"):
            controller.cancel_after_preferences()
            queued.pop()()

        self.assertTrue(controller._shutdown_exit_blocked)
        self.assertTrue(controller.orchestrator.cancelled)
        self.assertEqual(generation + 1, controller._run_generation)
        self.assertEqual([], dialogs.requests)
        self.assertNotIn("attribution_assignments", controller.orchestrator.task_cache)

    def test_attribution_cancel_with_blocked_cleanup_does_not_reopen_dialog(self):
        from spectrum_organizer.core.selection import CandidateConversionResult
        from spectrum_organizer.ui.dialog_port import (
            AttributionBookSelectionResponse,
            AttributionDialogResponse,
        )

        conversion = CandidateConversionResult(
            (_candidate("S1", "source.opj", "/", "RootF", "Root F"),),
            (),
            (),
        )
        dialogs = FakeAttributionDialogPort(
            (AttributionDialogResponse(action="cancel"),),
            (
                AttributionBookSelectionResponse(
                    action="select_book",
                    book_key=conversion.ordinary_candidates[0].book_key,
                ),
            ),
        )
        controller, _, _, message_box, _ = self._controller(
            attribution_dialog_port=dialogs,
            candidate_loader=lambda _summary: conversion,
        )
        controller.approved_pre_extraction_context = types.SimpleNamespace(
            temp_root=pathlib.Path("C:/owned/run"),
            temp_root_identity=(101, 202),
        )
        summary = {
            "total_inventory_count": 1,
            "total_extracted_count": 1,
            "total_rejected_count": 0,
            "source_summaries": (),
        }
        controller.orchestrator.task_cache["extraction_summary"] = summary

        with mock.patch.object(app_module, "_cleanup_temp_root_error", return_value="locked"):
            controller._begin_attribution(summary)

        self.assertEqual(1, len(dialogs.requests))
        self.assertEqual(1, len(dialogs.book_requests))
        self.assertTrue(controller._shutdown_exit_blocked)
        self.assertNotIn("attribution_assignments", controller.orchestrator.task_cache)
        self.assertEqual([("取消任务时发生错误", "取消后临时文件清理失败：locked")], message_box.errors)

    def test_attribution_cancel_aborts_directly_without_second_confirmation(self):
        from spectrum_organizer.core.selection import CandidateConversionResult
        from spectrum_organizer.ui.dialog_port import (
            AttributionBookSelectionResponse,
            AttributionDialogResponse,
        )

        conversion = CandidateConversionResult(
            (_candidate("S1", "source.opj", "/", "RootF", "Root F"),),
            (),
            (),
        )
        attribution_dialog = FakeAttributionDialogPort(
            (AttributionDialogResponse(action="cancel"),),
            (
                AttributionBookSelectionResponse(
                    action="select_book",
                    book_key=conversion.ordinary_candidates[0].book_key,
                ),
            ),
        )
        manual_dialog = FakeManualDialogPort("继续运行")
        controller, _, _, message_box, _ = self._controller(
            attribution_dialog_port=attribution_dialog,
            manual_dialog_port=manual_dialog,
            candidate_loader=lambda _summary: conversion,
        )
        summary = {
            "total_inventory_count": 1,
            "total_extracted_count": 1,
            "total_rejected_count": 0,
            "source_summaries": (),
        }
        controller.orchestrator.task_cache["extraction_summary"] = summary

        controller._begin_attribution(summary)

        self.assertEqual(1, len(attribution_dialog.requests), message_box.errors)
        self.assertEqual(1, len(attribution_dialog.book_requests), message_box.errors)
        self.assertEqual([], manual_dialog.requests)
        self.assertTrue(controller.orchestrator.cancelled)
        self.assertNotIn("attribution_assignments", controller.orchestrator.task_cache)

    def test_owner_close_during_attribution_does_not_cancel_twice(self):
        from spectrum_organizer.core.selection import CandidateConversionResult
        from spectrum_organizer.ui.dialog_port import AttributionDialogResponse

        class OwnerCloseAttributionDialogPort:
            controller = None

            def __init__(self):
                self.requests = []

            def choose(self, request, *, parent=None):
                self.requests.append(request)
                self.controller.cancel_after_preferences()
                return AttributionDialogResponse(action="cancel")

        conversion = CandidateConversionResult(
            (_candidate("S1", "source.opj", "MFL_RT", "F270", "F270"),),
            (),
            (),
        )
        dialogs = OwnerCloseAttributionDialogPort()
        controller, _, _, _, _ = self._controller(
            attribution_dialog_port=dialogs,
            manual_dialog_port=FakeManualDialogPort("取消并退出"),
            candidate_loader=lambda _summary: conversion,
        )
        dialogs.controller = controller
        summary = {
            "total_inventory_count": 1,
            "total_extracted_count": 1,
            "total_rejected_count": 0,
            "source_summaries": (),
        }
        controller.orchestrator.task_cache["extraction_summary"] = summary

        with mock.patch.object(
            controller,
            "_cancel_and_exit_after_preferences",
            wraps=controller._cancel_and_exit_after_preferences,
        ) as cancel_exit:
            controller._begin_attribution(summary, conversion)

        self.assertEqual(1, len(dialogs.requests))
        self.assertEqual(1, cancel_exit.call_count)
        self.assertTrue(controller.orchestrator.cancelled)

    def test_same_final_folder_name_reuse_prefills_but_still_requires_confirmation(self):
        from spectrum_organizer.core.selection import CandidateConversionResult
        from spectrum_organizer.ui.dialog_port import AttributionDialogResponse

        conversion = CandidateConversionResult(
            (
                _candidate("S1", "one.opj", "Parent/MFL_RT", "F270", "F270"),
                _candidate("S2", "two.opj", "Other/MFL-RT", "F270", "F270"),
            ),
            (),
            (),
        )
        response = AttributionDialogResponse(
            action="confirm",
            sample_type="solid",
            values={
                "sample": "MFL",
                "state": "Crystal",
                "oxygen_environment": "Air",
                "temperature": "298 K",
            },
        )
        dialogs = FakeAttributionDialogPort((response, response))
        controller, _, _, _, _ = self._controller(
            attribution_dialog_port=dialogs,
            candidate_loader=lambda summary: conversion,
        )
        summary = {
            "total_inventory_count": 2,
            "total_extracted_count": 2,
            "total_rejected_count": 0,
            "source_summaries": (),
        }
        controller.orchestrator.task_cache["extraction_summary"] = summary

        controller._begin_attribution(summary)

        self.assertEqual(2, len(dialogs.requests))
        self.assertEqual("solid", dialogs.requests[1].prefill["sample_type"])
        self.assertEqual("Crystal", dialogs.requests[1].prefill["state"])
        self.assertEqual("Air", dialogs.requests[1].prefill["oxygen_environment"])
        self.assertEqual("task_local_reuse", dialogs.requests[1].prefill_source)

    def test_task_local_environment_prefill_clears_when_later_source_inference_conflicts(self):
        from spectrum_organizer.core.selection import CandidateConversionResult
        from spectrum_organizer.ui.dialog_port import AttributionDialogResponse

        conversion = CandidateConversionResult(
            (
                _candidate("S1", "one_air.opj", "Parent/NDI_77K", "F270", "F270"),
                _candidate("S2", "two_vacuum.opj", "Other/NDI-77K", "F300", "F300"),
            ),
            (),
            (),
        )
        dialogs = FakeAttributionDialogPort(
            (
                AttributionDialogResponse(
                    action="confirm",
                    sample_type="solid",
                    values={
                        "sample": "NDI",
                        "state": "Solid",
                        "oxygen_environment": "Air",
                        "temperature": "77 K",
                    },
                ),
                AttributionDialogResponse(
                    action="confirm",
                    sample_type="solid",
                    values={
                        "sample": "NDI",
                        "state": "Solid",
                        "oxygen_environment": "DeO2",
                        "temperature": "77 K",
                    },
                ),
            )
        )
        controller, _, _, _, _ = self._controller(
            attribution_dialog_port=dialogs,
            candidate_loader=lambda _summary: conversion,
        )
        summary = {
            "total_inventory_count": 2,
            "total_extracted_count": 2,
            "total_rejected_count": 0,
            "source_summaries": (),
        }
        controller.orchestrator.task_cache["extraction_summary"] = summary

        controller._begin_attribution(summary)

        self.assertEqual(2, len(dialogs.requests))
        self.assertEqual("Air", dialogs.requests[0].prefill["oxygen_environment"])
        self.assertNotIn("oxygen_environment", dialogs.requests[1].prefill)
        self.assertEqual("task_local_reuse", dialogs.requests[1].prefill_source)

    def test_attribution_pending_summary_counts_books_not_folder_forms(self):
        from spectrum_organizer.core.selection import CandidateConversionResult
        from spectrum_organizer.ui.dialog_port import AttributionDialogResponse

        conversion = CandidateConversionResult(
            (
                _candidate("S1", "same.opj", "MFL_RT", "F270", "F270"),
                _candidate("S1", "same.opj", "MFL_RT", "F300", "F300"),
            ),
            (),
            (),
        )
        response = AttributionDialogResponse(
            action="confirm",
            sample_type="solid",
            values={
                "sample": "MFL",
                "state": "Solid",
                "oxygen_environment": "Air",
                "temperature": "298 K",
            },
        )
        controller, _, _, _, _ = self._controller(
            attribution_dialog_port=FakeAttributionDialogPort((response,)),
            candidate_loader=lambda _summary: conversion,
        )
        summary = {
            "total_inventory_count": 2,
            "total_extracted_count": 2,
            "total_rejected_count": 0,
            "source_summaries": (),
        }
        controller.orchestrator.task_cache["extraction_summary"] = summary
        updates = []
        controller.widgets["app_run_status"] = FakeLabel()
        controller.update_runtime_view = lambda **kwargs: updates.append(kwargs)

        controller._begin_attribution(summary)

        self.assertEqual("2", updates[0]["summary_numbers"][2])

    def test_attribution_preserves_candidate_rejection_counts_and_reason(self):
        from spectrum_organizer.core.selection import CandidateConversionResult, CandidateRejection
        from spectrum_organizer.ui.dialog_port import AttributionDialogResponse

        usable = _candidate("S1", "source.opj", "MFL_RT", "F270", "F270")
        rejected = CandidateRejection(
            source_id="S1",
            source_filename="source.opj",
            page_type="book",
            folder_path="BadFolder",
            short_name="BadBook",
            display_name="Rejected Display",
            reason="缺少必需的 Note 元数据",
        )
        conversion = CandidateConversionResult((usable,), (), (rejected,))
        response = AttributionDialogResponse(
            action="confirm",
            sample_type="solid",
            values={
                "sample": "MFL",
                "state": "Solid",
                "oxygen_environment": "Air",
                "temperature": "298 K",
            },
        )
        dialogs = FakeAttributionDialogPort((response,))
        controller, _, _, _, _ = self._controller(
            attribution_dialog_port=dialogs,
            candidate_loader=lambda _summary: conversion,
        )
        summary = {
            "total_inventory_count": 2,
            "total_extracted_count": 2,
            "total_rejected_count": 0,
            "source_summaries": (),
        }
        controller.orchestrator.task_cache["extraction_summary"] = summary
        updates = []
        controller.widgets["app_run_status"] = FakeLabel()
        controller.update_runtime_view = lambda **kwargs: updates.append(kwargs)

        controller._begin_attribution(summary)

        self.assertEqual(1, len(dialogs.requests))
        self.assertEqual(("F270",), dialogs.requests[0].book_display_names)
        self.assertEqual(("2", "1", "1", "1"), updates[0]["summary_numbers"])
        self.assertEqual(("来源文件", "Folder / Book", "排除原因"), updates[0]["review_headers"])
        self.assertEqual(
            (("source.opj", "BadFolder / Rejected Display", "缺少必需的 Note 元数据"),),
            updates[0]["review_rows"],
        )
        self.assertTrue(updates[0]["show_review_table"])
        self.assertEqual(("2", "2", "0", "1"), updates[-1]["summary_numbers"])
        self.assertEqual(
            ("来源文件", "归属范围 / Book", "归属或排除结果"),
            updates[-1]["review_headers"],
        )
        self.assertEqual(
            ("source.opj", "BadFolder / Rejected Display", "已排除：缺少必需的 Note 元数据"),
            updates[-1]["review_rows"][-1],
        )
        self.assertIs(conversion, controller.orchestrator.task_cache["candidate_conversion"])

    def test_rejection_rows_keep_duplicate_long_names_without_internal_suffixes(self):
        from spectrum_organizer.core.selection import CandidateRejection

        shared = {
            "source_id": "S1",
            "source_filename": "source.opj",
            "folder_path": "BadFolder",
            "short_name": "SameName",
            "display_name": "Same Display",
            "reason": "缺少必需数据",
        }
        rows = app_module._candidate_rejection_rows(
            (
                CandidateRejection(page_type="book", **shared),
                CandidateRejection(page_type="matrix", **shared),
            ),
            include_status=False,
        )

        self.assertEqual(
            (
                ("source.opj", "BadFolder / Same Display", "缺少必需数据"),
                ("source.opj", "BadFolder / Same Display", "缺少必需数据"),
            ),
            rows,
        )

    def test_ui_uses_short_name_only_when_origin_long_name_is_empty(self):
        from spectrum_organizer.core.selection import CandidateRejection

        candidate = _candidate(
            "S1",
            "source.opj",
            "Folder",
            "DfltEx1",
            "",
        )
        labels = app_module._attribution_book_labels((candidate,))
        rejection_row = app_module._candidate_rejection_row(
            CandidateRejection(
                source_id="S1",
                source_filename="source.opj",
                page_type="worksheet",
                folder_path="Folder",
                short_name="DfltEm4",
                display_name="",
                reason="missing Note",
            ),
            include_status=False,
        )

        self.assertEqual("DfltEx1", labels[candidate.book_key])
        self.assertEqual(
            ("source.opj", "Folder / DfltEm4", "缺少 Note"),
            rejection_row,
        )

    def test_ui_preserves_user_long_name_that_resembles_origin_default_name(self):
        candidate = _candidate(
            "S1",
            "source.opj",
            "Folder",
            "DfltEx1",
            "  DfltEx (01)465  ",
        )

        labels = app_module._attribution_book_labels((candidate,))

        self.assertEqual("  DfltEx (01)465  ", labels[candidate.book_key])

    def test_steady_2d_conflict_choice_shows_both_scan_axes_and_standard_name(self):
        candidate = _candidate(
            "S1",
            "source.opj",
            "Map",
            "Map1",
            "Renamed Map",
            spectrum_class=SpectrumClass.STEADY_2D,
            wavelength_range=None,
            scan_increment=None,
            excitation_range=("250", "450"),
            emission_range=("300", "700"),
            excitation_increment="5",
            emission_increment="2",
        )

        choice = app_module._conflict_choice(candidate)
        fields = dict(choice.fields)

        self.assertEqual("Renamed Map", choice.display_name)
        self.assertEqual("二维稳态谱", fields["谱图类型"])
        self.assertEqual("250 – 450 nm", fields["激发扫描范围"])
        self.assertEqual("5 nm", fields["激发扫描步长"])
        self.assertEqual("300 – 700 nm", fields["发射扫描范围"])
        self.assertEqual("2 nm", fields["发射扫描步长"])
        self.assertEqual("X=450 nm，Y=100", fields["峰值"])
        candidate.x_at_max_y = (302, 300, 301)
        self.assertEqual(
            "X=300, 301, 302 nm，Y=100",
            dict(app_module._conflict_choice(candidate).fields)["峰值"],
        )
        candidate.x_at_max_y = (303, 300, 302, 301)
        self.assertEqual(
            "X=300 - 303 nm（4 个并列最大值），Y=100",
            dict(app_module._conflict_choice(candidate).fields)["峰值"],
        )
        self.assertNotIn("扫描范围", fields)
        self.assertNotIn("扫描步长", fields)

    def test_conflict_peak_y_is_grouped_and_limited_to_two_decimals(self):
        candidate = _candidate(
            "S1",
            "source.opj",
            "Folder",
            "Book1",
            "Book 1",
        )
        candidate.max_y = 272813.628754616

        peak = dict(app_module._conflict_choice(candidate).fields)["峰值"]

        self.assertEqual("X=450 nm，Y=272,813.63", peak)

    def test_conflict_peak_y_preserves_small_positive_magnitudes(self):
        candidate = _candidate(
            "S1",
            "source.opj",
            "Folder",
            "Book1",
            "Book 1",
        )
        candidate.max_y = 0.004

        peak = dict(app_module._conflict_choice(candidate).fields)["峰值"]

        self.assertEqual("X=450 nm，Y=0.004", peak)

    def test_delayed_conflict_choice_keeps_delay_components_independent(self):
        candidate = _candidate(
            "S1",
            "source.opj",
            "Delayed",
            "P300-0.05ms",
            "P300-0.05ms",
            spectrum_class=SpectrumClass.DELAYED_EMISSION,
            flash_delay="0.05",
            sample_window="20.00",
            time_per_flash="45.05",
            flash_count="4",
        )

        fields = dict(app_module._conflict_choice(candidate).fields)

        self.assertNotIn("延迟参数", fields)
        self.assertEqual("300 nm", fields["固定激发波长"])
        self.assertEqual("350 – 650 nm", fields["扫描范围"])
        self.assertEqual("1 nm", fields["扫描步长"])
        self.assertEqual("Ex 2 / 2 nm / Em 2 / 2 nm", fields["狭缝"])
        self.assertEqual("0.05 ms", fields["延迟时间"])
        self.assertEqual("20.00 ms", fields["采样窗口"])
        self.assertEqual("45.05 ms", fields["单次闪光周期"])
        self.assertEqual("4", fields["闪光次数"])

    def test_s1_limit_rejection_is_rendered_as_complete_chinese_measurement(self):
        from spectrum_organizer.core.selection import CandidateRejection

        row = app_module._candidate_rejection_row(
            CandidateRejection(
                source_id="S1",
                source_filename="source.opj",
                page_type="worksheet",
                folder_path="Folder_77K",
                short_name="285_2_2",
                display_name="285_2_2",
                reason="S1 max exceeds limit",
                s1_max=2_345_678,
                x_at_s1_max=412,
            ),
            include_status=False,
        )

        self.assertEqual(
            (
                "source.opj",
                "Folder_77K / 285_2_2",
                "S1 最大值超过设定上限（最大值：2345678；对应 X：412）",
            ),
            row,
        )

    def test_audit_measurements_preserve_float_boundary_and_decimal_precision(self):
        from decimal import Decimal
        from spectrum_organizer.core.selection import CandidateRejection

        over_limit = math.nextafter(2_000_000.0, math.inf)
        row = app_module._candidate_rejection_row(
            CandidateRejection(
                source_id="S1",
                source_filename="source.opj",
                page_type="worksheet",
                folder_path="Folder",
                short_name="Book",
                display_name="Book",
                reason="S1 max exceeds limit",
                s1_max=over_limit,
                x_at_s1_max=Decimal(
                    "412.123456789012345678901234567890"
                ),
            ),
            include_status=False,
        )

        self.assertIn(
            "最大值：2000000.0000000002",
            row[2],
        )
        self.assertIn(
            "对应 X：412.123456789012345678901234567890",
            row[2],
        )

    def test_all_reachable_rejection_reason_families_are_rendered_in_chinese(self):
        from spectrum_organizer.core.selection import CandidateRejection

        cases = (
            ("unsupported Origin page type: matrix", "不支持的 Origin 页面类型：matrix"),
            ("missing Note", "缺少 Note"),
            (
                "Note read failed: locked",
                "读取 Note 失败（原始诊断：locked）",
            ),
            ("missing Data sheet", "缺少 Data 工作表"),
            (
                "Data read failed: locked",
                "读取 Data 失败（原始诊断：locked）",
            ),
            (
                "multiple Note sheets are ambiguous",
                "存在多个 Note 工作表，无法确定唯一来源",
            ),
            (
                "multiple Data sheets are ambiguous",
                "存在多个 Data 工作表，无法确定唯一来源",
            ),
            ("Book-local Note must start with [EXP_FD_FILE]", "Book 对应的 Note 未以 [EXP_FD_FILE] 开头"),
            ("Unsupported acquisition type", "Note 中的采集类型不受支持"),
            ("Conflicting acquisition types", "Note 中存在冲突的采集类型"),
            ("Missing delayed Note fields: Flash Delay", "延迟谱 Note 缺少字段：Flash Delay"),
            ("Invalid wavelength range: abc", "波长范围无效：abc"),
            ("Wavelength section requires both Start and End", "波长段必须同时包含 Start 和 End"),
            (
                "Wavelength section requires both Front Entrance Slit and Front Exit Slit",
                "波长段必须同时包含 Front Entrance Slit 和 Front Exit Slit",
            ),
            (
                "EX1 entrance and exit slit values conflict",
                "EX1 入口与出口狭缝数值不一致",
            ),
            (
                "EM1 entrance and exit slit values conflict",
                "EM1 入口与出口狭缝数值不一致",
            ),
            ("ambiguous S1: S1", "存在多个 S1 列：S1"),
            ("missing S1: S1", "缺少 S1 列"),
            ("Missing selected Y column: S1c", "缺少拟提取的 Y 列：S1c"),
            ("Ambiguous selected Y column: S1c", "拟提取的 Y 列不唯一：S1c"),
            ("Selected Y has no preceding X column: S1c", "拟提取的 Y 列前没有配套 X 列：S1c"),
            (
                "Selected Y column is not Y-designated: S1c",
                "拟提取的列未指定为 Y：S1c",
            ),
            (
                "Selected Y has no preceding X-designated column: S1c",
                "拟提取的 Y 列前没有指定为 X 的配套列：S1c",
            ),
            ("blank in column Em at row 3", "列 Em 第 3 行为空"),
            ("blank in column S1 at row 2: S1", "列 S1 第 2 行为空"),
            ("non-finite column S1c at row 4", "列 S1c 第 4 行不是有限数值"),
            (
                "duplicate value in column Em at row 3",
                "列 Em 第 3 行的 X 数值重复",
            ),
            ("column Em has 3 rows but column S1c has 2 rows", "列 Em 有 3 行，列 S1c 有 2 行，行数不一致"),
            ("ambiguous selected Y: S1c", "拟提取的 Y 列不唯一：S1c"),
            ("missing selected Y: S1c", "缺少拟提取的 Y 列：S1c"),
            (
                "selected Y and S1 resolve to the same physical column: S1c",
                "拟提取的 Y 列与 S1 指向同一物理列：S1c",
            ),
            ("missing selected X/Y", "缺少拟提取的 X/Y 数据"),
            ("selected Y max <= 0", "拟提取 Y 列的最大值小于或等于 0，无法归一化"),
            ("Note is missing excitation or emission scan range", "Note 缺少激发或发射扫描范围"),
            ("Note is missing excitation or emission scan increment", "Note 缺少激发或发射扫描步长"),
            ("Note is missing fixed emission wavelength", "Note 缺少固定发射波长"),
            ("Note is missing excitation scan range", "Note 缺少激发扫描范围"),
            ("Note is missing excitation scan increment", "Note 缺少激发扫描步长"),
            ("Note is missing fixed excitation wavelength", "Note 缺少固定激发波长"),
            ("Note is missing emission scan range", "Note 缺少发射扫描范围"),
            ("Note is missing emission scan increment", "Note 缺少发射扫描步长"),
            ("Note is missing delayed acquisition parameters", "Note 缺少延迟谱采集参数"),
            (
                "Note is missing excitation slits",
                "Note 缺少激发侧入口或出口狭缝",
            ),
            (
                "Note is missing emission slits",
                "Note 缺少发射侧入口或出口狭缝",
            ),
            (
                "stored spectrum class does not match Note",
                "快照中的谱图类型与 Note 不一致",
            ),
            ("Note has invalid numeric Flash Delay: abc", "Note 中的数值字段 Flash Delay 无效：abc"),
            (
                "Conflicting Note field 'Start' in Wavelength",
                "Note 字段 'Start' 在 Wavelength 中存在冲突",
            ),
            ("invalid data", "数据无效"),
            (
                "unrecognized internal rejection",
                "数据或元数据不符合可用谱图要求"
                "（原始诊断：unrecognized internal rejection）",
            ),
        )

        for reason, expected in cases:
            with self.subTest(reason=reason):
                rejection = CandidateRejection(
                    source_id="S1",
                    source_filename="source.opj",
                    page_type="worksheet",
                    folder_path="Folder",
                    short_name="Book1",
                    display_name="Book1",
                    reason=reason,
                )
                self.assertEqual(expected, app_module._candidate_rejection_reason(rejection))

    def test_non_positive_selected_max_rejection_keeps_value_and_x_evidence(self):
        from spectrum_organizer.core.selection import CandidateRejection

        reason = app_module._candidate_rejection_reason(
            CandidateRejection(
                source_id="S1",
                source_filename="source.opj",
                page_type="worksheet",
                folder_path="Folder",
                short_name="Book1",
                display_name="Book1",
                reason="selected Y max <= 0",
                max_y=0,
                x_at_max_y=(410, 412),
            )
        )

        self.assertEqual(
            "拟提取 Y 列的最大值小于或等于 0，无法归一化"
            "（最大值：0；对应 X：(410, 412)）",
            reason,
        )

    def test_source_filename_temperature_is_prefilled_before_attribution(self):
        from spectrum_organizer.core.selection import CandidateConversionResult
        from spectrum_organizer.ui.dialog_port import AttributionDialogResponse

        candidate = _candidate(
            "S1",
            "20240923_TMeFL_77K.opj",
            "DiMeFL_DCM",
            "285_2_2",
            "285_2_2",
        )
        dialogs = FakeAttributionDialogPort(
            (
                AttributionDialogResponse(
                    action="confirm",
                    sample_type="solid",
                    values={
                        "sample": "NDI",
                        "state": "Solid",
                        "oxygen_environment": "Air",
                        "temperature": "77 K",
                    },
                ),
            )
        )
        controller, _, _, _, _ = self._controller(
            attribution_dialog_port=dialogs,
            candidate_loader=lambda _summary: CandidateConversionResult((candidate,), (), ()),
        )
        summary = {
            "total_inventory_count": 1,
            "total_extracted_count": 1,
            "total_rejected_count": 0,
            "source_summaries": (),
        }
        controller.orchestrator.task_cache["extraction_summary"] = summary

        controller._begin_attribution(summary)

        self.assertEqual("77 K", dialogs.requests[0].prefill["temperature"])
        self.assertFalse(dialogs.requests[0].allow_split_folder)

    def test_folder_can_switch_to_per_book_attribution(self):
        from spectrum_organizer.core.selection import CandidateConversionResult
        from spectrum_organizer.ui.dialog_port import (
            AttributionBookSelectionResponse,
            AttributionDialogResponse,
        )

        candidates = (
            _candidate("S1", "mixed.opj", "Mixed", "F270", "F270"),
            _candidate("S1", "mixed.opj", "Mixed", "F300", "F300"),
        )
        conversion = CandidateConversionResult(candidates, (), ())
        dialogs = FakeAttributionDialogPort(
            (
                AttributionDialogResponse(action="split_folder", split_folder=True),
                AttributionDialogResponse(
                    action="confirm",
                    sample_type="solid",
                    values={
                        "sample": "MFL",
                        "state": "Solid",
                        "oxygen_environment": "Air",
                        "temperature": "298 K",
                    },
                ),
                AttributionDialogResponse(
                    action="confirm",
                    sample_type="solid",
                    values={
                        "sample": "PFL",
                        "state": "Solid",
                        "oxygen_environment": "Air",
                        "temperature": "298 K",
                    },
                ),
            ),
            (
                AttributionBookSelectionResponse(action="select_book", book_key=candidates[1].book_key),
                AttributionBookSelectionResponse(action="select_book", book_key=candidates[0].book_key),
            ),
        )
        controller, _, _, _, _ = self._controller(
            attribution_dialog_port=dialogs,
            candidate_loader=lambda summary: conversion,
        )
        summary = {
            "total_inventory_count": 2,
            "total_extracted_count": 2,
            "total_rejected_count": 0,
            "source_summaries": (),
        }
        controller.orchestrator.task_cache["extraction_summary"] = summary

        controller._begin_attribution(summary)

        assignments = controller.orchestrator.task_cache["attribution_assignments"]
        self.assertEqual(2, len(assignments))
        self.assertEqual(
            {"MFL-Solid-Air-298 K", "PFL-Solid-Air-298 K"},
            {item.sample.canonical_label for item in assignments.values()},
        )
        self.assertEqual(3, len(dialogs.requests))
        self.assertTrue(dialogs.requests[0].allow_split_folder)
        self.assertEqual(("F300",), dialogs.requests[1].book_display_names)
        self.assertTrue(dialogs.requests[1].allow_apply_to_remaining_folder)
        self.assertEqual(2, len(dialogs.book_requests))
        self.assertTrue(dialogs.book_requests[0].allow_return_to_folder)
        self.assertTrue(dialogs.book_requests[1].allow_return_to_folder)

    def test_temperature_prefill_ignores_book_in_folder_mode_and_uses_selected_book_in_per_book_mode(self):
        from spectrum_organizer.core.selection import CandidateConversionResult
        from spectrum_organizer.ui.dialog_port import (
            AttributionBookSelectionResponse,
            AttributionDialogResponse,
        )

        candidates = (
            _candidate("S1", "sample.opj", "Mixed", "F270", "Book_77K"),
            _candidate("S1", "sample.opj", "Mixed", "F300", "Book_RT"),
        )
        dialogs = FakeAttributionDialogPort(
            (
                AttributionDialogResponse(action="split_folder", split_folder=True),
                AttributionDialogResponse(
                    action="confirm",
                    sample_type="solid",
                    values={
                        "sample": "MFL",
                        "state": "Solid",
                        "oxygen_environment": "Air",
                        "temperature": "77 K",
                    },
                ),
                AttributionDialogResponse(
                    action="confirm",
                    sample_type="solid",
                    values={
                        "sample": "MFL",
                        "state": "Solid",
                        "oxygen_environment": "Air",
                        "temperature": "298 K",
                    },
                ),
            ),
            (
                AttributionBookSelectionResponse(action="select_book", book_key=candidates[0].book_key),
                AttributionBookSelectionResponse(action="select_book", book_key=candidates[1].book_key),
            ),
        )
        controller, _, _, _, _ = self._controller(
            attribution_dialog_port=dialogs,
            candidate_loader=lambda _summary: CandidateConversionResult(candidates, (), ()),
        )
        summary = {
            "total_inventory_count": 2,
            "total_extracted_count": 2,
            "total_rejected_count": 0,
            "source_summaries": (),
        }
        controller.orchestrator.task_cache["extraction_summary"] = summary

        controller._begin_attribution(summary)

        self.assertNotIn("temperature", dialogs.requests[0].prefill)
        self.assertEqual("77 K", dialogs.requests[1].prefill["temperature"])
        self.assertEqual("298 K", dialogs.requests[2].prefill["temperature"])

    def test_solution_concentration_prefill_uses_source_filename_evidence(self):
        from spectrum_organizer.core.selection import CandidateConversionResult
        from spectrum_organizer.ui.dialog_port import AttributionDialogResponse

        candidate = _candidate(
            "S1",
            "batch_0.1mM.opj",
            "Sample",
            "F270",
            "Book",
        )
        dialogs = FakeAttributionDialogPort((
            AttributionDialogResponse(
                action="confirm",
                sample_type="solution",
                values={
                    "sample": "MFL",
                    "solvent": "mTHF",
                    "concentration": "1×10^-4",
                    "temperature": "298 K",
                },
            ),
        ))
        controller, _, _, _, _ = self._controller(
            attribution_dialog_port=dialogs,
            candidate_loader=lambda _summary: CandidateConversionResult(
                (candidate,),
                (),
                (),
            ),
        )
        summary = {
            "total_inventory_count": 1,
            "total_extracted_count": 1,
            "total_rejected_count": 0,
            "source_summaries": (),
        }
        controller.orchestrator.task_cache["extraction_summary"] = summary

        controller._begin_attribution(summary)

        self.assertEqual(
            "1×10^-4",
            dialogs.requests[0].prefill["solution_concentration"],
        )

    def test_solution_concentration_prefill_accepts_hyphenated_folder_evidence(self):
        from spectrum_organizer.core.selection import CandidateConversionResult
        from spectrum_organizer.ui.dialog_port import AttributionDialogResponse

        candidate = _candidate(
            "S1",
            "20250412_MFL-mTHF_RT.opj",
            "PFL-10^-7M_RT",
            "F270",
            "F270",
        )
        dialogs = FakeAttributionDialogPort((
            AttributionDialogResponse(
                action="confirm",
                sample_type="solution",
                values={
                    "sample": "PFL",
                    "solvent": "mTHF",
                    "concentration": "1×10^-7",
                    "temperature": "298 K",
                },
            ),
        ))
        controller, _, _, _, _ = self._controller(
            attribution_dialog_port=dialogs,
            candidate_loader=lambda _summary: CandidateConversionResult(
                (candidate,),
                (),
                (),
            ),
        )
        summary = {
            "total_inventory_count": 1,
            "total_extracted_count": 1,
            "total_rejected_count": 0,
            "source_summaries": (),
        }
        controller.orchestrator.task_cache["extraction_summary"] = summary

        controller._begin_attribution(summary)

        self.assertEqual(
            "1×10^-7",
            dialogs.requests[0].prefill["solution_concentration"],
        )

    def test_per_book_attribution_uses_only_user_renamed_long_names(self):
        from spectrum_organizer.core.selection import CandidateConversionResult
        from spectrum_organizer.ui.dialog_port import (
            AttributionBookSelectionResponse,
            AttributionDialogResponse,
        )

        candidates = (
            _candidate("S1", "mixed.opj", "Mixed", "PE1", "Same Long Name"),
            _candidate("S1", "mixed.opj", "Mixed", "PE2", "Same Long Name"),
            _candidate("S1", "mixed.opj", "Mixed", "PE3", "Same Long Name (PE1)"),
        )
        conversion = CandidateConversionResult(candidates, (), ())
        dialogs = FakeAttributionDialogPort(
            (
                AttributionDialogResponse(action="split_folder", split_folder=True),
                AttributionDialogResponse(
                    action="confirm",
                    sample_type="solid",
                    values={
                        "sample": "CFL",
                        "state": "Solid",
                        "oxygen_environment": "Air",
                        "temperature": "298 K",
                    },
                ),
                AttributionDialogResponse(
                    action="confirm",
                    sample_type="solid",
                    values={
                        "sample": "PFL",
                        "state": "Solid",
                        "oxygen_environment": "Air",
                        "temperature": "298 K",
                    },
                ),
                AttributionDialogResponse(
                    action="confirm",
                    sample_type="solid",
                    values={
                        "sample": "MFL",
                        "state": "Solid",
                        "oxygen_environment": "Air",
                        "temperature": "298 K",
                    },
                ),
            ),
            (
                AttributionBookSelectionResponse(action="select_book", book_key=candidates[2].book_key),
                AttributionBookSelectionResponse(action="select_book", book_key=candidates[1].book_key),
                AttributionBookSelectionResponse(action="select_book", book_key=candidates[0].book_key),
            ),
        )
        controller, _, _, _, _ = self._controller(
            attribution_dialog_port=dialogs,
            candidate_loader=lambda summary: conversion,
        )
        summary = {
            "total_inventory_count": 3,
            "total_extracted_count": 3,
            "total_rejected_count": 0,
            "source_summaries": (),
        }
        controller.orchestrator.task_cache["extraction_summary"] = summary
        updates = []
        controller.widgets["app_run_status"] = FakeLabel()
        controller.update_runtime_view = lambda **kwargs: updates.append(kwargs)

        controller._begin_attribution(summary)

        self.assertEqual(
            (
                "Same Long Name",
                "Same Long Name",
                "Same Long Name (PE1)",
            ),
            dialogs.requests[0].book_display_names,
        )
        self.assertEqual(
            (
                (candidates[0].book_key, "Same Long Name"),
                (candidates[1].book_key, "Same Long Name"),
                (candidates[2].book_key, "Same Long Name (PE1)"),
            ),
            dialogs.book_requests[0].choices,
        )
        self.assertEqual(
            "Mixed / Same Long Name (PE1)",
            dialogs.requests[1].target_label,
        )
        self.assertEqual(
            ("Same Long Name (PE1)",),
            dialogs.requests[1].book_display_names,
        )
        self.assertEqual(
            {
                "Mixed / Same Long Name",
                "Mixed / Same Long Name (PE1)",
            },
            {row[1] for row in updates[-1]["review_rows"]},
        )
        visible_labels = [
            *(
                label
                for request in dialogs.requests
                for label in (request.target_label, *request.book_display_names)
            ),
            *(
                label
                for request in dialogs.book_requests
                for _book_key, label in request.choices
            ),
            *(cell for update in updates for row in update.get("review_rows", ()) for cell in row),
        ]
        visible_text = "\n".join(visible_labels)
        self.assertNotIn("PE2", visible_text)
        self.assertNotIn("(PE1) (PE3)", visible_text)
        assignments = controller.orchestrator.task_cache["attribution_assignments"]
        self.assertEqual("MFL-Solid-Air-298 K", assignments[candidates[0].book_key].sample.canonical_label)
        self.assertEqual("PFL-Solid-Air-298 K", assignments[candidates[1].book_key].sample.canonical_label)
        self.assertEqual("CFL-Solid-Air-298 K", assignments[candidates[2].book_key].sample.canonical_label)

    def test_duplicate_long_names_remain_long_name_only_across_scopes(self):
        from spectrum_organizer.ui.app import _attribution_book_labels

        candidates = (
            _candidate("S1", "first.opj", "Mixed", "PE1", "Same Long Name"),
            _candidate("S1", "first.opj", "Mixed", "PE2", "Same Long Name"),
            _candidate("S1", "first.opj", "Mixed", "PE5", "Unique Long Name"),
            _candidate("S1", "first.opj", "Other", "PE3", "Same Long Name"),
            _candidate("S2", "first.opj", "Mixed", "PE4", "Same Long Name"),
        )

        labels = _attribution_book_labels(candidates)

        self.assertEqual("Same Long Name", labels[candidates[0].book_key])
        self.assertEqual("Same Long Name", labels[candidates[1].book_key])
        self.assertEqual("Unique Long Name", labels[candidates[2].book_key])
        self.assertEqual("Same Long Name", labels[candidates[3].book_key])
        self.assertEqual("Same Long Name", labels[candidates[4].book_key])

    def test_attribution_book_labels_do_not_reveal_short_names_for_collisions(self):
        from spectrum_organizer.ui.app import _attribution_book_labels

        candidates = (
            _candidate("S1", "mixed.opj", "Mixed", "PE1", "Same"),
            _candidate("S1", "mixed.opj", "Mixed", "PE2", "Same"),
            _candidate("S1", "mixed.opj", "Mixed", "PE3", "Same (PE1)"),
        )

        labels = _attribution_book_labels(candidates)

        self.assertEqual("Same", labels[candidates[0].book_key])
        self.assertEqual("Same", labels[candidates[1].book_key])
        self.assertEqual("Same (PE1)", labels[candidates[2].book_key])

    def test_attribution_book_labels_do_not_reveal_page_type_when_long_names_match(self):
        from spectrum_organizer.ui.app import _attribution_book_labels

        candidates = (
            _candidate(
                "S1",
                "mixed.opj",
                "Mixed",
                "PE1",
                "Same",
                page_type="worksheet",
            ),
            _candidate(
                "S1",
                "mixed.opj",
                "Mixed",
                "PE1",
                "Same",
                page_type="matrix",
            ),
        )

        labels = _attribution_book_labels(candidates)

        self.assertEqual("Same", labels[candidates[0].book_key])
        self.assertEqual("Same", labels[candidates[1].book_key])

    def test_attribution_book_label_fallback_is_stable_when_candidates_reorder(self):
        from spectrum_organizer.ui.app import _attribution_book_labels

        candidate_b = _candidate(
            "S1",
            "mixed.opj",
            "Mixed",
            "PE1",
            "Same",
            page_type="worksheet",
        )
        candidate_b.book_key = "stable-B"
        candidate_a = _candidate(
            "S1",
            "mixed.opj",
            "Mixed",
            "PE1",
            "Same",
            page_type="worksheet",
        )
        candidate_a.book_key = "stable-A"

        forward = _attribution_book_labels((candidate_b, candidate_a))
        reversed_order = _attribution_book_labels((candidate_a, candidate_b))

        self.assertEqual(forward, reversed_order)
        self.assertEqual("Same", forward["stable-A"])
        self.assertEqual("Same", forward["stable-B"])

    def test_folder_environment_prefill_uses_source_and_folder_and_clears_conflicts(self):
        from spectrum_organizer.core.selection import CandidateConversionResult
        from spectrum_organizer.ui.dialog_port import AttributionDialogResponse

        conversion = CandidateConversionResult(
            (
                _candidate("S1", "batch_air.opj", "Sample_77K", "F270", "F270"),
                _candidate("S2", "batch_air.opj", "Sample_vacuum_77K", "F300", "F300"),
            ),
            (),
            (),
        )
        responses = (
            AttributionDialogResponse(
                action="confirm",
                sample_type="solid",
                values={
                    "sample": "NDI",
                    "state": "Solid",
                    "oxygen_environment": "Air",
                    "temperature": "77 K",
                },
            ),
            AttributionDialogResponse(
                action="confirm",
                sample_type="solid",
                values={
                    "sample": "NDI",
                    "state": "Solid",
                    "oxygen_environment": "DeO2",
                    "temperature": "77 K",
                },
            ),
        )
        dialogs = FakeAttributionDialogPort(responses)
        controller, _, _, _, _ = self._controller(
            attribution_dialog_port=dialogs,
            candidate_loader=lambda _summary: conversion,
        )
        summary = {
            "total_inventory_count": 2,
            "total_extracted_count": 2,
            "total_rejected_count": 0,
            "source_summaries": (),
        }
        controller.orchestrator.task_cache["extraction_summary"] = summary

        controller._begin_attribution(summary)

        requests = {request.target_label: request for request in dialogs.requests}
        self.assertEqual("Air", requests["Sample_77K"].prefill["oxygen_environment"])
        self.assertNotIn("oxygen_environment", requests["Sample_vacuum_77K"].prefill)

    def test_folder_environment_prefill_ignores_parent_folder_tokens(self):
        from spectrum_organizer.core.selection import CandidateConversionResult
        from spectrum_organizer.ui.dialog_port import AttributionDialogResponse

        candidate = _candidate(
            "S1",
            "batch.opj",
            "Parent_vacuum/PlainFolder",
            "F270",
            "F270",
        )
        dialogs = FakeAttributionDialogPort(
            (
                AttributionDialogResponse(
                    action="confirm",
                    sample_type="solid",
                    values={
                        "sample": "NDI",
                        "state": "Solid",
                        "oxygen_environment": "Air",
                        "temperature": "298 K",
                    },
                ),
            )
        )
        controller, _, _, _, _ = self._controller(
            attribution_dialog_port=dialogs,
            candidate_loader=lambda _summary: CandidateConversionResult((candidate,), (), ()),
        )
        summary = {
            "total_inventory_count": 1,
            "total_extracted_count": 1,
            "total_rejected_count": 0,
            "source_summaries": (),
        }
        controller.orchestrator.task_cache["extraction_summary"] = summary

        controller._begin_attribution(summary)

        self.assertNotIn("oxygen_environment", dialogs.requests[0].prefill)

    def test_per_book_environment_prefill_uses_source_and_book_but_not_folder(self):
        from spectrum_organizer.core.selection import CandidateConversionResult
        from spectrum_organizer.ui.dialog_port import (
            AttributionBookSelectionResponse,
            AttributionDialogResponse,
        )

        candidates = (
            _candidate("S1", "batch.opj", "Folder_air_77K", "F270", "Book_vacuum"),
            _candidate("S1", "batch.opj", "Folder_air_77K", "F300", "Book_plain"),
        )
        conversion = CandidateConversionResult(candidates, (), ())
        dialogs = FakeAttributionDialogPort(
            (
                AttributionDialogResponse(action="split_folder", split_folder=True),
                AttributionDialogResponse(
                    action="confirm",
                    sample_type="solid",
                    values={
                        "sample": "NDI",
                        "state": "Solid",
                        "oxygen_environment": "DeO2",
                        "temperature": "77 K",
                    },
                    apply_to_remaining_folder=True,
                ),
            ),
            (AttributionBookSelectionResponse(action="select_book", book_key=candidates[0].book_key),),
        )
        controller, _, _, _, _ = self._controller(
            attribution_dialog_port=dialogs,
            candidate_loader=lambda _summary: conversion,
        )
        summary = {
            "total_inventory_count": 2,
            "total_extracted_count": 2,
            "total_rejected_count": 0,
            "source_summaries": (),
        }
        controller.orchestrator.task_cache["extraction_summary"] = summary

        controller._begin_attribution(summary)

        self.assertEqual("Air", dialogs.requests[0].prefill["oxygen_environment"])
        self.assertEqual("DeO2", dialogs.requests[1].prefill["oxygen_environment"])

    def test_per_book_picker_can_return_to_folder_without_opening_a_book_form(self):
        from spectrum_organizer.core.selection import CandidateConversionResult
        from spectrum_organizer.ui.dialog_port import (
            AttributionBookSelectionResponse,
            AttributionDialogResponse,
        )

        candidates = (
            _candidate("S1", "mixed.opj", "Mixed", "F270", "F270"),
            _candidate("S1", "mixed.opj", "Mixed", "F300", "F300"),
        )
        conversion = CandidateConversionResult(candidates, (), ())
        dialogs = FakeAttributionDialogPort(
            (
                AttributionDialogResponse(action="split_folder", split_folder=True),
                AttributionDialogResponse(
                    action="confirm",
                    sample_type="solid",
                    values={
                        "sample": "MFL",
                        "state": "Solid",
                        "oxygen_environment": "Air",
                        "temperature": "298 K",
                    },
                ),
            ),
            (AttributionBookSelectionResponse(action="return_to_folder"),),
        )
        controller, _, _, _, _ = self._controller(
            attribution_dialog_port=dialogs,
            candidate_loader=lambda summary: conversion,
        )
        summary = {
            "total_inventory_count": 2,
            "total_extracted_count": 2,
            "total_rejected_count": 0,
            "source_summaries": (),
        }
        controller.orchestrator.task_cache["extraction_summary"] = summary

        controller._begin_attribution(summary)

        self.assertEqual(2, len(dialogs.requests))
        self.assertEqual(("F270", "F300"), dialogs.requests[1].book_display_names)
        self.assertEqual(1, len(dialogs.book_requests))
        self.assertEqual(2, len(controller.orchestrator.task_cache["attribution_assignments"]))

    def test_per_book_picker_can_return_to_folder_after_confirming_one_book(self):
        from spectrum_organizer.core.selection import CandidateConversionResult
        from spectrum_organizer.ui.dialog_port import (
            AttributionBookSelectionResponse,
            AttributionDialogResponse,
        )

        candidates = (
            _candidate("S1", "mixed.opj", "Mixed", "F270", "F270"),
            _candidate("S1", "mixed.opj", "Mixed", "F300", "F300"),
        )
        conversion = CandidateConversionResult(candidates, (), ())
        dialogs = FakeAttributionDialogPort(
            (
                AttributionDialogResponse(action="split_folder", split_folder=True),
                AttributionDialogResponse(
                    action="confirm",
                    sample_type="solid",
                    values={
                        "sample": "Temporary",
                        "state": "Solid",
                        "oxygen_environment": "Air",
                        "temperature": "77 K",
                    },
                ),
                AttributionDialogResponse(
                    action="confirm",
                    sample_type="solid",
                    values={
                        "sample": "Folder",
                        "state": "Solid",
                        "oxygen_environment": "DeO2",
                        "temperature": "298 K",
                    },
                ),
            ),
            (
                AttributionBookSelectionResponse(action="select_book", book_key=candidates[0].book_key),
                AttributionBookSelectionResponse(action="return_to_folder"),
            ),
        )
        controller, _, _, _, _ = self._controller(
            attribution_dialog_port=dialogs,
            candidate_loader=lambda summary: conversion,
        )
        summary = {
            "total_inventory_count": 2,
            "total_extracted_count": 2,
            "total_rejected_count": 0,
            "source_summaries": (),
        }
        controller.orchestrator.task_cache["extraction_summary"] = summary

        controller._begin_attribution(summary)

        assignments = controller.orchestrator.task_cache["attribution_assignments"]
        self.assertEqual(2, len(assignments))
        self.assertEqual(
            {"Folder-Solid-DeO2-298 K"},
            {item.sample.canonical_label for item in assignments.values()},
        )
        self.assertTrue(dialogs.book_requests[1].allow_return_to_folder)
        self.assertEqual("", dialogs.requests[2].prefill_source)
        self.assertNotIn("sample", dialogs.requests[2].prefill)

    def test_unconfirmed_book_form_can_return_to_picker_and_choose_another_book(self):
        from spectrum_organizer.core.selection import CandidateConversionResult
        from spectrum_organizer.ui.dialog_port import (
            AttributionBookSelectionResponse,
            AttributionDialogResponse,
        )

        candidates = (
            _candidate("S1", "mixed.opj", "Mixed", "F270", "F270"),
            _candidate("S1", "mixed.opj", "Mixed", "F300", "F300"),
        )
        conversion = CandidateConversionResult(candidates, (), ())
        dialogs = FakeAttributionDialogPort(
            (
                AttributionDialogResponse(action="split_folder", split_folder=True),
                AttributionDialogResponse(action="return_to_book_picker"),
                AttributionDialogResponse(
                    action="confirm",
                    sample_type="solid",
                    values={
                        "sample": "PFL",
                        "state": "Solid",
                        "oxygen_environment": "Air",
                        "temperature": "298 K",
                    },
                ),
                AttributionDialogResponse(
                    action="confirm",
                    sample_type="solid",
                    values={
                        "sample": "MFL",
                        "state": "Solid",
                        "oxygen_environment": "Air",
                        "temperature": "298 K",
                    },
                ),
            ),
            (
                AttributionBookSelectionResponse(action="select_book", book_key=candidates[0].book_key),
                AttributionBookSelectionResponse(action="select_book", book_key=candidates[1].book_key),
                AttributionBookSelectionResponse(action="select_book", book_key=candidates[0].book_key),
            ),
        )
        controller, _, _, _, _ = self._controller(
            attribution_dialog_port=dialogs,
            candidate_loader=lambda summary: conversion,
        )
        summary = {
            "total_inventory_count": 2,
            "total_extracted_count": 2,
            "total_rejected_count": 0,
            "source_summaries": (),
        }
        controller.orchestrator.task_cache["extraction_summary"] = summary

        controller._begin_attribution(summary)

        assignments = controller.orchestrator.task_cache["attribution_assignments"]
        self.assertEqual("MFL-Solid-Air-298 K", assignments[candidates[0].book_key].sample.canonical_label)
        self.assertEqual("PFL-Solid-Air-298 K", assignments[candidates[1].book_key].sample.canonical_label)
        self.assertTrue(dialogs.requests[1].allow_return_to_book_picker)
        self.assertTrue(dialogs.requests[2].allow_return_to_book_picker)
        self.assertTrue(dialogs.book_requests[0].allow_return_to_folder)
        self.assertTrue(dialogs.book_requests[1].allow_return_to_folder)
        self.assertTrue(dialogs.book_requests[2].allow_return_to_folder)

    def test_per_book_return_previous_preserves_folder_return_route(self):
        from spectrum_organizer.core.selection import CandidateConversionResult
        from spectrum_organizer.ui.dialog_port import (
            AttributionBookSelectionResponse,
            AttributionDialogResponse,
        )

        candidates = (
            _candidate("S1", "mixed_RT.opj", "Folder_A", "A", "A"),
            _candidate("S1", "mixed_RT.opj", "Folder_B", "B1", "B1"),
            _candidate("S1", "mixed_RT.opj", "Folder_B", "B2", "B2"),
        )
        conversion = CandidateConversionResult(candidates, (), ())

        def confirmed(sample, temperature="298 K"):
            return AttributionDialogResponse(
                action="confirm",
                sample_type="solid",
                values={
                    "sample": sample,
                    "state": "Solid",
                    "oxygen_environment": "Air",
                    "temperature": temperature,
                },
            )

        dialogs = FakeAttributionDialogPort(
            (
                confirmed("PFL", "350 K"),
                AttributionDialogResponse(action="split_folder", split_folder=True),
                AttributionDialogResponse(action="return_previous"),
                confirmed("MFL"),
                confirmed("DFL"),
            ),
            (
                AttributionBookSelectionResponse(
                    action="select_book",
                    book_key=candidates[1].book_key,
                ),
                AttributionBookSelectionResponse(action="return_to_folder"),
            ),
        )
        controller, _, _, _, _ = self._controller(
            attribution_dialog_port=dialogs,
            candidate_loader=lambda summary: conversion,
        )
        summary = {
            "total_inventory_count": 3,
            "total_extracted_count": 3,
            "total_rejected_count": 0,
            "source_summaries": (),
        }
        controller.orchestrator.task_cache["extraction_summary"] = summary

        controller._begin_attribution(summary)

        assignments = controller.orchestrator.task_cache["attribution_assignments"]
        self.assertTrue(dialogs.requests[2].allow_return_to_book_picker)
        self.assertTrue(dialogs.requests[2].allow_return_previous)
        self.assertEqual("previous_attribution", dialogs.requests[3].prefill_source)
        self.assertEqual("PFL", dialogs.requests[3].prefill["sample"])
        self.assertEqual("350 K", dialogs.requests[3].prefill["temperature"])
        self.assertTrue(dialogs.book_requests[1].allow_return_to_folder)
        self.assertEqual(
            "MFL-Solid-Air-298 K",
            assignments[candidates[0].book_key].sample.canonical_label,
        )
        self.assertEqual(
            "DFL-Solid-Air-298 K",
            assignments[candidates[1].book_key].sample.canonical_label,
        )
        self.assertEqual(
            "DFL-Solid-Air-298 K",
            assignments[candidates[2].book_key].sample.canonical_label,
        )

    def test_selected_book_attribution_can_apply_to_all_remaining_unconfirmed_books(self):
        from spectrum_organizer.core.selection import CandidateConversionResult
        from spectrum_organizer.ui.dialog_port import (
            AttributionBookSelectionResponse,
            AttributionDialogResponse,
        )

        candidates = (
            _candidate("S1", "mixed.opj", "Mixed", "F270", "F270"),
            _candidate("S1", "mixed.opj", "Mixed", "F300", "F300"),
            _candidate("S1", "mixed.opj", "Mixed", "F330", "F330"),
        )
        conversion = CandidateConversionResult(candidates, (), ())
        dialogs = FakeAttributionDialogPort(
            (
                AttributionDialogResponse(action="split_folder", split_folder=True),
                AttributionDialogResponse(
                    action="confirm",
                    sample_type="solid",
                    values={
                        "sample": "PFL",
                        "state": "Solid",
                        "oxygen_environment": "Air",
                        "temperature": "298 K",
                    },
                ),
                AttributionDialogResponse(
                    action="confirm",
                    sample_type="solid",
                    values={
                        "sample": "MFL",
                        "state": "Solid",
                        "oxygen_environment": "Air",
                        "temperature": "298 K",
                    },
                    apply_to_remaining_folder=True,
                ),
            ),
            (
                AttributionBookSelectionResponse(action="select_book", book_key=candidates[0].book_key),
                AttributionBookSelectionResponse(action="select_book", book_key=candidates[1].book_key),
            ),
        )
        controller, _, _, _, _ = self._controller(
            attribution_dialog_port=dialogs,
            candidate_loader=lambda summary: conversion,
        )
        summary = {
            "total_inventory_count": 3,
            "total_extracted_count": 3,
            "total_rejected_count": 0,
            "source_summaries": (),
        }
        controller.orchestrator.task_cache["extraction_summary"] = summary
        updates = []
        controller.widgets["app_run_status"] = FakeLabel()
        controller.update_runtime_view = lambda **kwargs: updates.append(kwargs)

        controller._begin_attribution(summary)

        assignments = controller.orchestrator.task_cache["attribution_assignments"]
        self.assertEqual(3, len(assignments))
        self.assertEqual("PFL-Solid-Air-298 K", assignments[candidates[0].book_key].sample.canonical_label)
        self.assertEqual("MFL-Solid-Air-298 K", assignments[candidates[1].book_key].sample.canonical_label)
        self.assertEqual("MFL-Solid-Air-298 K", assignments[candidates[2].book_key].sample.canonical_label)
        self.assertEqual(
            (
                candidates[1].book_key,
                candidates[2].book_key,
            ),
            controller.orchestrator.task_cache[
                "latest_attribution_decision_book_keys"
            ],
        )
        self.assertEqual(2, len(dialogs.book_requests))
        self.assertEqual(3, len(dialogs.requests))
        self.assertEqual(
            ["3", "2", "2", "0"],
            [update["summary_numbers"][2] for update in updates],
        )
        self.assertEqual("1", updates[-1]["summary_numbers"][3])
        review_rows = updates[-1]["review_rows"]
        self.assertEqual(4, len(review_rows))
        attribution_rows = tuple(
            row for row in review_rows if not row[2].startswith("已排除：")
        )
        self.assertEqual(
            {"PFL-Solid-Air-298 K", "MFL-Solid-Air-298 K"},
            {row[2] for row in attribution_rows},
        )
        self.assertEqual(
            {"Mixed / F270", "Mixed / F300", "Mixed / F330"},
            {row[1] for row in attribution_rows},
        )
        self.assertIn(
            (
                "mixed.opj",
                "Mixed / F330",
                "已排除：重复发射谱审核未选中",
            ),
            review_rows,
        )

    def test_async_job_failure_restores_input_controls_for_retry(self):
        start_run_runner = RecordingAsyncStartRunRunner()
        controller, _, widgets, message_box, _ = self._controller(
            source_paths=("C:/raw/a.opju",),
            output_parent="D:/Organized",
            start_run_runner=start_run_runner,
        )
        widgets["app_run_status"] = FakeLabel()
        runtime_updates = []
        controller.update_runtime_view = lambda **kwargs: runtime_updates.append(kwargs)
        controller.choose_source_files()
        controller.choose_output_parent()

        self.assertTrue(controller.request_start_run())
        start_run_runner.fail("real extraction failed")

        self.assertFalse(controller.run_ready)
        self.assertEqual("real extraction failed", controller.orchestrator.last_failure)
        self.assertEqual([("谱图数据提取失败", "real extraction failed")], message_box.errors)
        self.assertTrue(runtime_updates[-1]["show_input_controls"])
        self.assertFalse(runtime_updates[-1]["show_review_table"])
        self.assertEqual("source_input", runtime_updates[-1]["stage"])
        self.assertEqual("等待选择", runtime_updates[-1]["phase_detail"])
        self.assertFalse(runtime_updates[-1]["progress_busy"])
        self.assertIn("谱图数据提取失败", widgets["run_log"].toPlainText())

    def test_successful_retry_clears_previous_failure_state(self):
        start_run_runner = RecordingAsyncStartRunRunner()
        controller, _, _, _, _ = self._controller(
            source_paths=("C:/raw/a.opju",),
            output_parent="D:/Organized",
            start_run_runner=start_run_runner,
        )
        controller.choose_source_files()
        controller.choose_output_parent()

        self.assertTrue(controller.request_start_run())
        start_run_runner.fail("first attempt failed")
        self.assertEqual("first attempt failed", controller.orchestrator.last_failure)
        self.assertTrue(controller.request_start_run())
        start_run_runner.succeed()

        self.assertTrue(controller.run_ready)
        self.assertIsNone(controller.orchestrator.last_failure)

    def test_async_job_callback_after_cancel_is_ignored(self):
        start_run_runner = RecordingAsyncStartRunRunner()
        controller, _, widgets, message_box, _ = self._controller(
            source_paths=("C:/raw/a.opju",),
            output_parent="D:/Organized",
            start_run_runner=start_run_runner,
        )
        controller.choose_source_files()
        controller.choose_output_parent()
        self.assertTrue(controller.request_start_run())

        controller.cancel_after_preferences()
        start_run_runner.succeed()

        self.assertFalse(controller.run_ready)
        self.assertNotIn("extraction_summary", controller.orchestrator.task_cache)
        self.assertEqual([], message_box.errors)
        self.assertIn("任务已取消", widgets["run_log"].toPlainText())

    def test_stale_success_after_cancel_cleans_its_owned_temp_root(self):
        from spectrum_organizer.safety.owned_paths import create_run_ownership

        with tempfile.TemporaryDirectory() as directory:
            ownership = create_run_ownership(pathlib.Path(directory), "stale-run", "stale-marker", [])
            start_run_runner = RecordingAsyncStartRunRunner(
                context=types.SimpleNamespace(
                    temp_root=ownership.temp_root,
                    temp_root_identity=ownership.temp_root_identity,
                )
            )
            controller, _, _, _, _ = self._controller(
                source_paths=("C:/raw/a.opju",),
                output_parent="D:/Organized",
                start_run_runner=start_run_runner,
            )
            controller.choose_source_files()
            controller.choose_output_parent()
            self.assertTrue(controller.request_start_run())

            controller.cancel_after_preferences()
            start_run_runner.succeed()

            self.assertFalse(ownership.temp_root.exists())

    def test_cancel_active_async_job_requests_stop_and_defers_close_until_runner_finishes(self):
        class DeferredCancelRunner(RecordingAsyncStartRunRunner):
            def __init__(self):
                super().__init__()
                self.cancelled = False
                self.on_stopped = None

            def cancel(self, on_stopped):
                self.cancelled = True
                self.on_stopped = on_stopped
                return True

            def finish_cancel(self):
                self.on_stopped()

        class FakeParent:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        start_run_runner = DeferredCancelRunner()
        controller, _, _, _, _ = self._controller(
            source_paths=("C:/raw/a.opju",),
            output_parent="D:/Organized",
            start_run_runner=start_run_runner,
        )
        parent = FakeParent()
        controller.parent = parent
        controller.choose_source_files()
        controller.choose_output_parent()
        self.assertTrue(controller.request_start_run())

        controller.cancel_after_preferences()

        self.assertTrue(start_run_runner.cancelled)
        self.assertFalse(parent.closed)
        self.assertTrue(controller.shutdown_pending)
        controller.cancel_after_preferences()
        self.assertEqual(1, len(controller.manual_dialog_port.requests))
        self.assertFalse(parent.closed)
        start_run_runner.finish_cancel()
        self.assertFalse(controller.shutdown_pending)
        self.assertTrue(parent.closed)
        self.assertEqual(
            ["cancel_and_exit_confirmation", "cancelled_and_exited"],
            [request.kind for request in controller.manual_dialog_port.requests],
        )

    def test_cancel_active_task8_job_waits_before_temp_cleanup_and_close(self):
        from spectrum_organizer.safety.owned_paths import (
            create_run_ownership,
        )

        for phase in ("prepare", "seal"):
            with self.subTest(phase=phase):
                with tempfile.TemporaryDirectory() as directory:
                    ownership = create_run_ownership(
                        pathlib.Path(directory),
                        f"task8-{phase}",
                        "marker",
                        [],
                    )
                    task8_runner = RecordingAsyncTask8Runner()
                    task8_runner.start(
                        lambda _cancel_check: None,
                        lambda _result: None,
                        lambda _error: None,
                    )
                    controller, _, _, message_box, _ = self._controller(
                        task8_runner=task8_runner,
                    )
                    failure_log_writer = mock.Mock()
                    controller.failure_log_writer = failure_log_writer
                    context = types.SimpleNamespace(
                        temp_root=ownership.temp_root,
                        temp_root_identity=ownership.temp_root_identity,
                    )
                    controller.approved_pre_extraction_context = context
                    controller.orchestrator.task_cache[
                        "approved_pre_extraction_context"
                    ] = context
                    controller._task8_phase = phase
                    parent = types.SimpleNamespace(closed=False)
                    parent.close = lambda: setattr(
                        parent,
                        "closed",
                        True,
                    )
                    controller.parent = parent

                    controller.cancel_after_preferences()

                    self.assertTrue(task8_runner.cancelled)
                    self.assertTrue(controller.shutdown_pending)
                    self.assertTrue(ownership.temp_root.exists())
                    self.assertFalse(parent.closed)
                    failure_log_writer.assert_not_called()

                    task8_runner.finish_cancel()

                    self.assertFalse(controller.shutdown_pending)
                    self.assertFalse(ownership.temp_root.exists())
                    self.assertIsNone(
                        controller.approved_pre_extraction_context
                    )
                    self.assertTrue(parent.closed)
                    self.assertEqual([], message_box.errors)
                    failure_log_writer.assert_not_called()

    def test_task8_seal_failure_preserves_review_state_and_returns_for_retry(self):
        from spectrum_organizer.safety.owned_paths import (
            create_run_ownership,
        )

        with tempfile.TemporaryDirectory() as directory:
            ownership = create_run_ownership(
                pathlib.Path(directory),
                "task8-failure",
                "marker",
                [],
            )
            scheduled = []
            controller, _, _, message_box, _ = self._controller(
                schedule_call=scheduled.append,
            )
            failure_log = (
                pathlib.Path(directory)
                / "Failed_Run_20260730_120000.txt"
            )
            controller.failure_log_writer = mock.Mock(
                return_value=failure_log
            )
            context = types.SimpleNamespace(
                temp_root=ownership.temp_root,
                temp_root_identity=ownership.temp_root_identity,
            )
            controller.approved_pre_extraction_context = context
            controller.orchestrator.task_cache.update(
                {
                    "approved_pre_extraction_context": context,
                    "selected_source_paths": ("C:/raw/a.opju",),
                    "output_parent": "C:/Out",
                    "task7_review_state": "preserve-me",
                }
            )
            controller.run_ready = True
            controller.run_in_progress = True
            retry_draft = object()

            controller._handle_task8_operation_failure(
                controller._run_generation,
                "封存",
                RuntimeError("approval ledger mismatch"),
                retry_draft=retry_draft,
            )

            controller.failure_log_writer.assert_called_once()
            self.assertIn(
                "approval ledger mismatch",
                controller.failure_log_writer.call_args.args[0],
            )
            self.assertTrue(ownership.temp_root.exists())
            self.assertIs(
                context,
                controller.approved_pre_extraction_context,
            )
            self.assertEqual(
                "preserve-me",
                controller.orchestrator.task_cache[
                    "task7_review_state"
                ],
            )
            self.assertTrue(controller.run_ready)
            self.assertTrue(controller.run_in_progress)
            self.assertEqual(
                failure_log,
                controller.orchestrator.task_cache[
                    "failed_run_log_path"
                ],
            )
            self.assertIn(
                str(failure_log),
                message_box.errors[0][1],
            )
            self.assertIn("审核状态已保留", message_box.errors[0][1])
            self.assertEqual(1, len(scheduled))

    def test_cancel_cleanup_failure_is_shown_and_window_stays_open(self):
        class FakeParent:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        controller, _, _, message_box, _ = self._controller(
            source_paths=("C:/raw/a.opju",),
            output_parent="D:/Organized",
        )
        parent = FakeParent()
        controller.parent = parent
        controller.shutdown_pending = True
        controller._run_generation = 2

        controller._handle_start_run_failure(
            1,
            app_module.ExtractionCleanupBlockedError(
                "谱图数据提取已取消; 临时文件清理失败：存在未知路径"
            ),
        )
        controller._finish_pending_shutdown()

        self.assertEqual(
            [("取消任务时发生错误", "谱图数据提取已取消; 临时文件清理失败：存在未知路径")],
            message_box.errors,
        )
        self.assertFalse(parent.closed)

    def test_cancel_surviving_origin_failure_keeps_close_blocked_until_cleanup_retry_succeeds(self):
        controller, _, _, message_box, _ = self._controller()
        parent = types.SimpleNamespace(closed=False)

        def close():
            parent.closed = True

        parent.close = close
        controller.parent = parent
        controller.shutdown_pending = True
        controller.orchestrator.cancelled = True
        controller._run_generation = 2

        controller._handle_start_run_failure(
            1,
            app_module.ExtractionCleanupBlockedError("Origin 进程仍在运行，禁止清理临时文件：7654"),
        )
        controller._finish_pending_shutdown()

        self.assertEqual(
            [("取消任务时发生错误", "Origin 进程仍在运行，禁止清理临时文件：7654")],
            message_box.errors,
        )
        self.assertFalse(parent.closed)
        self.assertTrue(controller._shutdown_exit_blocked)

        event = types.SimpleNamespace(
            ignored=False,
            type=lambda: "close",
            ignore=lambda: setattr(event, "ignored", True),
        )
        controller.parent = types.SimpleNamespace(installEventFilter=lambda event_filter: setattr(controller, "event_filter", event_filter))
        fake_qt = types.SimpleNamespace(
            QObject=type("FakeQObject", (), {"__init__": lambda self, parent=None: None, "eventFilter": lambda self, watched, event: False}),
            QEvent=types.SimpleNamespace(Type=types.SimpleNamespace(Close="close")),
        )
        app_module._install_safe_close_filter(controller.parent, controller, fake_qt)
        self.assertTrue(controller.event_filter.eventFilter(controller.parent, event))
        self.assertTrue(event.ignored)

        retry_parent = types.SimpleNamespace(closed=False)
        retry_parent.close = lambda: setattr(retry_parent, "closed", True)
        controller.parent = retry_parent
        retry_callbacks = []

        class CleanupRetryRunner:
            def retry_cleanup(self, callback):
                retry_callbacks.append(callback)
                return True

        controller.start_run_runner = CleanupRetryRunner()
        controller.cancel_after_preferences()

        self.assertFalse(retry_parent.closed)
        self.assertTrue(controller.shutdown_pending)
        retry_callbacks.pop()(None)

        self.assertTrue(retry_parent.closed)
        self.assertFalse(controller._shutdown_exit_blocked)

    def test_active_run_cleanup_blocked_failure_latches_window_close_guard(self):
        controller, _, _, _, _ = self._controller()
        controller.run_in_progress = True
        generation = controller._run_generation

        controller._handle_start_run_failure(
            generation,
            app_module.ExtractionCleanupBlockedError("Origin 进程退出状态无法确认"),
        )

        self.assertFalse(controller.run_in_progress)
        self.assertTrue(controller._shutdown_exit_blocked)
        self.assertIn("Origin 进程退出状态无法确认", controller._shutdown_error)

    def test_cancel_after_completed_extraction_cleans_temp_root_and_task_cache_before_exit(self):
        from spectrum_organizer.safety.owned_paths import create_run_ownership

        controller, _, _, message_box, _ = self._controller()
        with tempfile.TemporaryDirectory() as directory:
            ownership = create_run_ownership(pathlib.Path(directory), "completed-run", "marker", [])
            context = types.SimpleNamespace(
                temp_root=ownership.temp_root,
                temp_root_identity=ownership.temp_root_identity,
            )
            controller.approved_pre_extraction_context = context
            controller.orchestrator.task_cache["approved_pre_extraction_context"] = context
            controller.orchestrator.task_cache["extraction_summary"] = {"total": 1}
            controller.run_ready = True
            parent = types.SimpleNamespace(closed=False)
            parent.close = lambda: setattr(parent, "closed", True)
            controller.parent = parent

            controller.cancel_after_preferences()

            self.assertFalse(ownership.temp_root.exists())
            self.assertEqual({}, controller.orchestrator.task_cache)
            self.assertIsNone(controller.approved_pre_extraction_context)
            self.assertTrue(parent.closed)
            self.assertEqual([], message_box.errors)

    def test_completed_run_cleanup_retry_retries_temp_root_before_closing(self):
        controller, _, _, _, _ = self._controller()
        context = types.SimpleNamespace(
            temp_root=pathlib.Path("C:/owned/run"),
            temp_root_identity=(101, 202),
        )
        controller.approved_pre_extraction_context = context
        controller.orchestrator.task_cache["approved_pre_extraction_context"] = context
        controller.run_ready = True
        parent = types.SimpleNamespace(closed=False)
        parent.close = lambda: setattr(parent, "closed", True)
        controller.parent = parent
        retry_callbacks = []

        class CleanupRetryRunner:
            def retry_cleanup(self, callback):
                retry_callbacks.append(callback)
                return True

        controller.start_run_runner = CleanupRetryRunner()
        cleanup_results = iter(("locked", None))
        with unittest.mock.patch.object(
            app_module,
            "_cleanup_temp_root_error",
            side_effect=lambda _path, **_kwargs: next(cleanup_results),
        ):
            controller.cancel_after_preferences()
            self.assertTrue(controller._shutdown_exit_blocked)
            self.assertFalse(parent.closed)

            controller.cancel_after_preferences()
            retry_callbacks.pop()(None)

        self.assertTrue(parent.closed)
        self.assertFalse(controller._shutdown_exit_blocked)
        self.assertIsNone(controller.approved_pre_extraction_context)
        self.assertEqual({}, controller.orchestrator.task_cache)

    def test_attribution_cancel_cleanup_retry_completes_one_cancel_lifecycle(self):
        from spectrum_organizer.core.selection import CandidateConversionResult
        from spectrum_organizer.safety.owned_paths import create_run_ownership
        from spectrum_organizer.ui.dialog_port import (
            AttributionBookSelectionResponse,
            AttributionDialogResponse,
        )

        conversion = CandidateConversionResult(
            (_candidate("S1", "source.opj", "/", "RootF", "Root F"),),
            (),
            (),
        )
        attribution_dialog = FakeAttributionDialogPort(
            (AttributionDialogResponse(action="cancel"),),
            (
                AttributionBookSelectionResponse(
                    action="select_book",
                    book_key=conversion.ordinary_candidates[0].book_key,
                ),
            ),
        )
        manual_dialog = FakeManualDialogPort("取消并退出")
        controller, _, _, _, _ = self._controller(
            attribution_dialog_port=attribution_dialog,
            manual_dialog_port=manual_dialog,
            candidate_loader=lambda _summary: conversion,
        )
        retry_callbacks = []

        class CleanupRetryRunner:
            def retry_cleanup(self, callback):
                retry_callbacks.append(callback)
                return True

        controller.start_run_runner = CleanupRetryRunner()
        close_calls = []
        controller.parent = types.SimpleNamespace(close=lambda: close_calls.append(None))
        summary = {
            "total_inventory_count": 1,
            "total_extracted_count": 1,
            "total_rejected_count": 0,
            "source_summaries": (),
        }
        initial_generation = controller._run_generation

        with tempfile.TemporaryDirectory() as directory:
            run_root = pathlib.Path(directory) / "owned-run"
            run_root.mkdir()
            ownership = create_run_ownership(run_root, "cleanup-retry", "marker", [])
            context = types.SimpleNamespace(
                temp_root=ownership.temp_root,
                temp_root_identity=ownership.temp_root_identity,
            )
            controller.approved_pre_extraction_context = context
            controller.orchestrator.task_cache["approved_pre_extraction_context"] = context
            controller.orchestrator.task_cache["extraction_summary"] = summary
            real_cleanup = app_module._cleanup_temp_root_error
            cleanup_calls = []

            def fail_once_then_cleanup(path, **kwargs):
                cleanup_calls.append(path)
                if len(cleanup_calls) == 1:
                    return "locked"
                return real_cleanup(path, **kwargs)

            with mock.patch.object(app_module, "_cleanup_temp_root_error", side_effect=fail_once_then_cleanup):
                controller._begin_attribution(summary, conversion)
                self.assertTrue(controller._shutdown_exit_blocked)
                self.assertEqual(1, len(attribution_dialog.requests))
                self.assertEqual(1, len(attribution_dialog.book_requests))

                controller.cancel_after_preferences()
                self.assertEqual(1, len(retry_callbacks))
                self.assertTrue(controller.shutdown_pending)

                stale_attribution = mock.Mock()
                controller._begin_attribution = stale_attribution
                retry_callbacks.pop()(None)
                controller._begin_attribution_if_current(initial_generation, summary, conversion)

            self.assertFalse(ownership.temp_root.exists())

        self.assertEqual(["cancelled_and_exited"], [request.kind for request in manual_dialog.requests])
        self.assertTrue(controller.orchestrator.cancelled)
        self.assertEqual(initial_generation + 1, controller._run_generation)
        self.assertFalse(controller.run_in_progress)
        self.assertFalse(controller.shutdown_pending)
        self.assertFalse(controller._shutdown_exit_blocked)
        self.assertIsNone(controller.approved_pre_extraction_context)
        self.assertEqual({}, controller.orchestrator.task_cache)
        self.assertEqual([None], close_calls)
        stale_attribution.assert_not_called()

    def test_start_run_with_async_extraction_runner_returns_before_completion(self):
        extraction_runner = RecordingAsyncExtractionRunner()
        controller, _, widgets, message_box, _ = self._controller(
            source_paths=("C:/raw/a.opju",),
            output_parent="D:/Organized",
            extraction_runner=extraction_runner,
        )
        controller.choose_source_files()
        controller.choose_output_parent()

        self.assertTrue(controller.request_start_run())

        self.assertFalse(controller.run_ready)
        self.assertEqual([{"run_id": "test-run"}], extraction_runner.calls)
        self.assertNotIn("extraction_summary", controller.orchestrator.task_cache)
        self.assertEqual([], message_box.errors)
        extraction_runner.succeed()
        self.assertTrue(controller.run_ready)
        self.assertEqual(extraction_runner.result, controller.orchestrator.task_cache["extraction_summary"])
        self.assertIn("谱图数据提取完成", widgets["run_log"].toPlainText())

    def test_async_extraction_failure_is_reported_without_marking_run_ready(self):
        extraction_runner = RecordingAsyncExtractionRunner()
        controller, _, widgets, message_box, _ = self._controller(
            source_paths=("C:/raw/a.opju",),
            output_parent="D:/Organized",
            extraction_runner=extraction_runner,
        )
        controller.choose_source_files()
        controller.choose_output_parent()

        self.assertTrue(controller.request_start_run())
        extraction_runner.fail("real extraction failed")

        self.assertFalse(controller.run_ready)
        self.assertEqual("real extraction failed", controller.orchestrator.last_failure)
        self.assertEqual([("谱图数据提取失败", "real extraction failed")], message_box.errors)
        self.assertIn("谱图数据提取失败：real extraction failed", widgets["run_log"].toPlainText())

    def test_legacy_qt_threaded_extraction_runner_deletes_finished_thread(self):
        events = []

        class FakeSignal:
            def __init__(self, *args):
                self.callbacks = []

            def connect(self, callback):
                self.callbacks.append(callback)

            def emit(self, value=None):
                for callback in list(self.callbacks):
                    if value is None:
                        callback()
                    else:
                        callback(value)

        class FakeQThread:
            def __init__(self):
                self.finished = FakeSignal()

            def start(self):
                self.run()
                self.finished.emit()

            def deleteLater(self):
                events.append("deleteLater")

        fake_qt_core = types.SimpleNamespace(QThread=FakeQThread, Signal=FakeSignal)
        runner = app_module.QtThreadedExtractionRunner(fake_qt_core, lambda context: {"ok": context})

        runner.start("context", lambda result: events.append(("success", result)), lambda error: events.append(("error", error)))

        self.assertEqual([("success", {"ok": "context"}), "deleteLater"], events)

    def test_qt_thread_runners_route_base_exceptions_to_error_callbacks(self):
        class FakeSignal:
            def __init__(self, *args):
                self.callbacks = []

            def connect(self, callback):
                self.callbacks.append(callback)

            def emit(self, value=None):
                for callback in list(self.callbacks):
                    callback() if value is None else callback(value)

        class FakeQThread:
            def __init__(self):
                self.finished = FakeSignal()

            def start(self):
                self.run()
                self.finished.emit()

            def deleteLater(self):
                pass

        def interrupted(_value):
            raise KeyboardInterrupt("stop")

        qt_core = types.SimpleNamespace(
            QThread=FakeQThread,
            Signal=FakeSignal,
        )
        extraction_events = []
        extraction = app_module.QtThreadedExtractionRunner(
            qt_core,
            interrupted,
        )
        extraction.start(
            "work",
            lambda result: extraction_events.append(("success", result)),
            lambda error: extraction_events.append(("error", error)),
        )
        start_events = []
        start = app_module.QtThreadedStartRunRunner(
            qt_core,
            interrupted,
        )
        start.start(
            "work",
            lambda result: start_events.append(("success", result)),
            lambda error: start_events.append(("error", error)),
        )

        self.assertIsInstance(extraction_events[0][1], KeyboardInterrupt)
        self.assertEqual("error", extraction_events[0][0])
        self.assertIsInstance(start_events[0][1], KeyboardInterrupt)
        self.assertEqual("error", start_events[0][0])

    def test_qt_thread_start_failure_rolls_back_active_thread_registration(self):
        class FakeSignal:
            def __init__(self, *args):
                self.callbacks = []

            def connect(self, callback):
                self.callbacks.append(callback)

        class FailingQThread:
            def __init__(self):
                self.finished = FakeSignal()

            def start(self):
                raise RuntimeError("thread start failed")

            def deleteLater(self):
                pass

        qt_core = types.SimpleNamespace(
            QThread=FailingQThread,
            Signal=FakeSignal,
        )
        runners = (
            (
                app_module.QtThreadedExtractionRunner(
                    qt_core,
                    lambda value: value,
                ),
                lambda runner: runner.start(
                    "work",
                    lambda result: None,
                    lambda error: None,
                ),
            ),
            (
                app_module.QtThreadedStartRunRunner(
                    qt_core,
                    lambda value: value,
                ),
                lambda runner: runner.start(
                    "work",
                    lambda result: None,
                    lambda error: None,
                ),
            ),
        )

        for runner, start in runners:
            with self.subTest(runner=type(runner).__name__):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "thread start failed",
                ):
                    start(runner)
                self.assertEqual([], runner._threads)

    def test_qt_threaded_extraction_runner_delivers_result_on_gui_thread(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6 import QtCore, QtWidgets

        QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        gui_thread_id = threading.get_ident()
        callback_thread_ids = []
        finished = threading.Event()
        runner = app_module.QtThreadedExtractionRunner(QtCore, lambda _context: "done")

        def on_success(result):
            self.assertEqual("done", result)
            callback_thread_ids.append(threading.get_ident())
            finished.set()

        runner.start("context", on_success, self.fail)

        self.assertTrue(_wait_for_qt_event(QtCore, finished))
        self.assertEqual([gui_thread_id], callback_thread_ids)

    def test_qt_start_runner_delivers_progress_and_result_on_gui_thread(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6 import QtCore, QtWidgets

        QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        gui_thread_id = threading.get_ident()
        progress_thread_ids = []
        result_thread_ids = []
        finished = threading.Event()

        class RunFunc:
            progress_callback = None

            def set_progress_callback(self, callback):
                self.progress_callback = callback

            def __call__(self, _approved_inputs):
                self.progress_callback("halfway")
                return "done"

        runner = app_module.QtThreadedStartRunRunner(QtCore, RunFunc())

        def on_success(result):
            self.assertEqual("done", result)
            result_thread_ids.append(threading.get_ident())
            finished.set()

        def on_progress(progress):
            self.assertEqual("halfway", progress)
            progress_thread_ids.append(threading.get_ident())

        runner.start("approved", on_success, self.fail, on_progress)

        self.assertTrue(_wait_for_qt_event(QtCore, finished))
        self.assertEqual([gui_thread_id], progress_thread_ids)
        self.assertEqual([gui_thread_id], result_thread_ids)

    def test_qt_blocking_ui_call_executes_callback_on_gui_thread(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6 import QtCore, QtWidgets

        QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        gui_thread_id = threading.get_ident()
        callback_thread_ids = []
        finished = threading.Event()

        def callback():
            callback_thread_ids.append(threading.get_ident())
            return "done"

        ui_call = app_module.QtBlockingUiCall(QtCore, callback)

        def invoke():
            self.assertEqual("done", ui_call())
            finished.set()

        thread = threading.Thread(target=invoke)
        thread.start()
        self.assertTrue(_wait_for_qt_event(QtCore, finished))
        thread.join(1.0)

        self.assertFalse(thread.is_alive())
        self.assertEqual([gui_thread_id], callback_thread_ids)

    def test_qt_start_runner_cancel_is_nonblocking_and_retains_thread_until_finished(self):
        events = []

        class FakeSignal:
            def __init__(self, *args):
                self.callbacks = []

            def connect(self, callback):
                self.callbacks.append(callback)

            def emit(self, value=None):
                for callback in list(self.callbacks):
                    callback() if value is None else callback(value)

        class FakeQThread:
            def __init__(self):
                self.finished = FakeSignal()

            def start(self):
                events.append("started")

            def requestInterruption(self):
                events.append("interrupted")

            def deleteLater(self):
                events.append("deleteLater")

        fake_qt_core = types.SimpleNamespace(QThread=FakeQThread, Signal=FakeSignal)
        runner = app_module.QtThreadedStartRunRunner(
            fake_qt_core,
            lambda inputs: inputs,
            cancel_func=lambda: events.append("cancel-child"),
        )
        runner.start("inputs", lambda result: None, lambda error: None)

        active = runner.cancel(lambda: events.append("safe-to-close"))

        self.assertTrue(active)
        self.assertEqual(["started", "cancel-child", "interrupted"], events)
        self.assertEqual(1, len(runner._threads))
        runner._threads[0].finished.emit()
        self.assertEqual(
            ["started", "cancel-child", "interrupted", "deleteLater", "safe-to-close"],
            events,
        )
        self.assertEqual([], runner._threads)

    def test_qt_start_runner_delivers_success_only_after_thread_finished(self):
        events = []

        class FakeSignal:
            def __init__(self, *args):
                self.callbacks = []

            def connect(self, callback):
                self.callbacks.append(callback)

            def emit(self, value=None):
                for callback in list(self.callbacks):
                    callback() if value is None else callback(value)

        class FakeQThread:
            def __init__(self):
                self.finished = FakeSignal()

            def start(self):
                self.run()

            def deleteLater(self):
                events.append("deleteLater")

        runner = app_module.QtThreadedStartRunRunner(
            types.SimpleNamespace(QThread=FakeQThread, Signal=FakeSignal),
            lambda value: value + "-done",
        )
        runner.start("work", lambda result: events.append(("success", result)), lambda error: events.append(("error", error)))

        self.assertEqual([], events)
        runner._threads[0].finished.emit()
        self.assertEqual([("success", "work-done"), "deleteLater"], events)

    def test_start_job_forwards_extraction_progress_callback(self):
        events = []

        class ProgressExtractionRunner:
            def set_progress_callback(self, callback):
                self.callback = callback

            def __call__(self, context):
                self.callback({"kind": "source_started", "source_id": "S0001"})
                return context

        extraction_runner = ProgressExtractionRunner()
        job = app_module.CancellableStartRunJob(
            lambda **kwargs: types.SimpleNamespace(
                temp_root=None,
                temp_root_identity=None,
            ),
            extraction_runner,
        )
        job.set_progress_callback(events.append)

        job(types.SimpleNamespace(selected_source_paths=(), output_parent=None, settings_snapshot=None))

        self.assertEqual(
            [
                {"kind": "pre_extraction_completed"},
                {"kind": "source_started", "source_id": "S0001"},
            ],
            events,
        )

    def test_start_job_prepares_candidates_in_background_and_returns_conversion(self):
        events = []
        context = types.SimpleNamespace(
            temp_root=None,
            temp_root_identity=None,
        )
        summary = {"snapshot_path": "owned.sqlite3"}
        conversion = object()

        approved_settings = {
            "s1Limit": 2000000,
            "steadyEmissionY": "S1c",
            "allowMissingS1": True,
        }

        def candidate_loader(received, *, cancel_check, settings_snapshot):
            events.append("candidate")
            self.assertIs(summary, received)
            self.assertEqual(approved_settings, settings_snapshot)
            cancel_check()
            return conversion

        job = app_module.CancellableStartRunJob(
            lambda **kwargs: context,
            lambda approved_context: summary,
            candidate_loader=candidate_loader,
        )
        job.set_progress_callback(lambda event: events.append(event["kind"]))

        result = job(
            types.SimpleNamespace(
                selected_source_paths=(),
                output_parent=None,
                settings_snapshot=approved_settings,
            )
        )

        self.assertEqual((context, summary, conversion), result)
        self.assertEqual(
            ["pre_extraction_completed", "candidate_validation_started", "candidate"],
            events,
        )

    def test_start_job_accepts_one_argument_candidate_loader(self):
        context = types.SimpleNamespace(
            temp_root=None,
            temp_root_identity=None,
        )
        summary = {"snapshot_path": "owned.sqlite3"}
        conversion = object()
        calls = []

        def candidate_loader(received):
            calls.append(received)
            return conversion

        job = app_module.CancellableStartRunJob(
            lambda **kwargs: context,
            lambda approved_context: summary,
            candidate_loader=candidate_loader,
        )

        result = job(
            types.SimpleNamespace(
                selected_source_paths=(),
                output_parent=None,
                settings_snapshot=None,
            )
        )

        self.assertEqual((context, summary, conversion), result)
        self.assertEqual([summary], calls)

    def test_candidate_loader_type_error_is_not_retried_without_cancel_keyword(self):
        context = types.SimpleNamespace(
            temp_root=None,
            temp_root_identity=None,
        )
        calls = []

        def candidate_loader(summary):
            calls.append(summary)
            raise TypeError("loader implementation failed")

        job = app_module.CancellableStartRunJob(
            lambda **kwargs: context,
            lambda approved_context: {"snapshot_path": "owned.sqlite3"},
            candidate_loader=candidate_loader,
        )

        with self.assertRaisesRegex(TypeError, "loader implementation failed"):
            job(
                types.SimpleNamespace(
                    selected_source_paths=(),
                    output_parent=None,
                    settings_snapshot=None,
                )
            )

        self.assertEqual(1, len(calls))

    def test_start_job_candidate_validation_observes_cancellation(self):
        context = types.SimpleNamespace(
            temp_root=None,
            temp_root_identity=None,
        )
        job = None

        def candidate_loader(_summary, *, cancel_check):
            job.cancel()
            cancel_check()

        job = app_module.CancellableStartRunJob(
            lambda **kwargs: context,
            lambda approved_context: {"snapshot_path": "owned.sqlite3"},
            candidate_loader=candidate_loader,
        )

        with self.assertRaisesRegex(product_runner.ProductRunnerError, "已取消"):
            job(types.SimpleNamespace(selected_source_paths=(), output_parent=None, settings_snapshot=None))

    def test_controller_candidate_validation_progress_is_visible(self):
        start_runner = RecordingAsyncStartRunRunner()
        controller, _, _, _, _ = self._controller(
            source_paths=("C:/raw/a.opju",),
            output_parent="D:/Organized",
            start_run_runner=start_runner,
        )
        controller.choose_source_files()
        controller.choose_output_parent()
        updates = []
        controller.widgets["app_run_status"] = FakeLabel()
        controller.update_runtime_view = lambda **kwargs: updates.append(kwargs)
        self.assertTrue(controller.request_start_run())

        start_runner.progress({"kind": "candidate_validation_started"})

        self.assertEqual("候选校验中", updates[-1]["phase_detail"])
        self.assertEqual("automatic", updates[-1]["activity_mode"])
        self.assertEqual("校验候选谱图", updates[-1]["title"])
        self.assertFalse(updates[-1]["show_input_controls"])

    def test_controller_updates_real_extraction_rows_and_counts_from_progress(self):
        start_runner = RecordingAsyncStartRunRunner()
        controller, _, _, _, _ = self._controller(
            source_paths=("C:/raw/a.opju", "C:/raw/b.opju"),
            output_parent="D:/Organized",
            start_run_runner=start_runner,
        )
        controller.choose_source_files()
        controller.choose_output_parent()
        updates = []
        controller.widgets["app_run_status"] = FakeLabel()
        controller.update_runtime_view = lambda **kwargs: updates.append(kwargs)

        self.assertTrue(controller.request_start_run())
        start_runner.progress({"kind": "pre_extraction_completed"})
        start_runner.progress({
            "kind": "source_started",
            "source_id": "S0001",
            "source_path": "C:/raw/a.opju",
            "source_index": 1,
            "source_total": 2,
            "completed_sources": 0,
        })
        start_runner.progress({
            "kind": "source_completed",
            "source_id": "S0001",
            "source_path": "C:/raw/a.opju",
            "source_index": 1,
            "source_total": 2,
            "completed_sources": 1,
            "inventory_count": 58,
            "extracted_count": 56,
            "rejected_count": 2,
            "total_inventory_count": 58,
            "total_extracted_count": 56,
            "total_rejected_count": 2,
        })

        full_table_updates = [
            update["review_rows"]
            for update in updates
            if update.get("review_rows")
        ]
        self.assertEqual(
            [
                (
                    ("a.opju", "等待统计", "等待读取"),
                    ("b.opju", "等待统计", "等待读取"),
                )
            ],
            full_table_updates,
        )
        self.assertEqual(
            [
                ("source_input", 0, ("a.opju", "等待统计", "正在读取")),
                ("source_input", 0, ("a.opju", "58", "已提取 56，排除 2")),
            ],
            [update["review_row_update"] for update in updates if "review_row_update" in update],
        )
        self.assertEqual(("58", "56", "0", "2"), updates[-1]["summary_numbers"])
        self.assertEqual(1, updates[-1]["progress"] // 50)
        self.assertIn("1/2", updates[-1]["runtime_status"])

    def test_controller_marks_skipped_source_without_adding_it_to_book_counts(self):
        start_runner = RecordingAsyncStartRunRunner()
        controller, _, _, _, _ = self._controller(
            source_paths=("C:/raw/valid.opju", "C:/raw/Paper.opju"),
            output_parent="D:/Organized",
            start_run_runner=start_runner,
        )
        controller.choose_source_files()
        controller.choose_output_parent()
        updates = []
        controller.widgets["app_run_status"] = FakeLabel()
        controller.update_runtime_view = lambda **kwargs: updates.append(kwargs)

        self.assertTrue(controller.request_start_run())
        start_runner.progress({"kind": "pre_extraction_completed"})
        start_runner.progress(
            {
                "kind": "source_started",
                "source_path": "C:/raw/Paper.opju",
                "source_index": 2,
                "source_total": 2,
                "completed_sources": 1,
            }
        )
        start_runner.progress(
            {
                "kind": "source_skipped",
                "source_path": "C:/raw/Paper.opju",
                "source_index": 2,
                "source_total": 2,
                "completed_sources": 2,
                "reason": "未检测到受支持的 Origin 原始谱图。",
                "recommendation": "请重新选择包含原始光谱 Book 的文件。",
                "total_inventory_count": 7,
                "total_extracted_count": 7,
                "total_rejected_count": 0,
            }
        )

        skipped = updates[-1]
        self.assertEqual(("7", "7", "0", "0"), skipped["summary_numbers"])
        self.assertEqual(
            (
                "source_input",
                1,
                (
                    "Paper.opju",
                    "—",
                    "已跳过：未检测到受支持的 Origin 原始谱图。",
                ),
            ),
            skipped["review_row_update"],
        )
        self.assertTrue(skipped["show_attention"])
        self.assertIn("Paper.opju", skipped["attention_message"])
        log_text = controller.widgets["run_log"].toPlainText()
        self.assertIn(
            "已跳过输入文件 Paper.opju：未检测到受支持的 Origin 原始谱图。",
            log_text,
        )
        self.assertNotIn("建议：请重新选择包含原始光谱 Book 的文件。", log_text)

    def test_controller_updates_truthful_elapsed_time_only_while_source_is_active(self):
        class FakeSignal:
            def __init__(self):
                self.callback = None

            def connect(self, callback):
                self.callback = callback

        class FakeTimer:
            def __init__(self):
                self.timeout = FakeSignal()
                self.active = False

            def start(self):
                self.active = True

            def stop(self):
                self.active = False

            def fire(self):
                self.timeout.callback()

        clock = [100.0]
        timer = FakeTimer()
        start_runner = RecordingAsyncStartRunRunner()
        controller, _, _, _, _ = self._controller(
            source_paths=("C:/raw/a.opju",),
            output_parent="D:/Organized",
            start_run_runner=start_runner,
            extraction_activity_timer=timer,
            monotonic_clock=lambda: clock[0],
        )
        controller.choose_source_files()
        controller.choose_output_parent()
        updates = []
        controller.widgets["app_run_status"] = FakeLabel()
        controller.update_runtime_view = lambda **kwargs: updates.append(kwargs)

        self.assertTrue(controller.request_start_run())
        start_runner.progress({"kind": "pre_extraction_completed"})
        start_runner.progress({
            "kind": "source_started",
            "source_path": "C:/raw/a.opju",
            "source_index": 1,
            "source_total": 1,
            "completed_sources": 0,
        })
        self.assertTrue(timer.active)

        clock[0] = 112.4
        timer.fire()
        self.assertEqual("总用时 00:12", updates[-1]["subtitle"])
        self.assertEqual(
            ("source_input", 0, ("a.opju", "等待统计", "正在读取 · 已用时 00:12")),
            updates[-1]["review_row_update"],
        )

        start_runner.progress({
            "kind": "source_completed",
            "source_path": "C:/raw/a.opju",
            "source_index": 1,
            "source_total": 1,
            "completed_sources": 1,
            "inventory_count": 7,
            "extracted_count": 6,
            "rejected_count": 1,
            "total_inventory_count": 7,
            "total_extracted_count": 6,
            "total_rejected_count": 1,
        })
        self.assertFalse(timer.active)
        self.assertNotIn("已用时", updates[-1]["subtitle"])
        self.assertEqual(
            ("source_input", 0, ("a.opju", "7", "已提取 6，排除 1")),
            updates[-1]["review_row_update"],
        )

        clock[0] = 200.0
        controller._cancel_confirmation_pending = True
        start_runner.progress({
            "kind": "source_started",
            "source_path": "C:/raw/a.opju",
            "source_index": 1,
            "source_total": 1,
            "completed_sources": 0,
        })
        self.assertTrue(timer.active)
        update_count = len(updates)
        clock[0] = 209.0
        timer.fire()
        self.assertEqual(update_count, len(updates))

        controller._finish_cancel_confirmation(replay=True)
        timer.fire()
        self.assertEqual("总用时 01:49", updates[-1]["subtitle"])

        controller._cancel_confirmation_pending = True
        start_runner.progress({
            "kind": "source_completed",
            "source_path": "C:/raw/a.opju",
            "source_index": 1,
            "source_total": 1,
            "completed_sources": 1,
            "inventory_count": 7,
            "extracted_count": 6,
            "rejected_count": 1,
            "total_inventory_count": 7,
            "total_extracted_count": 6,
            "total_rejected_count": 1,
        })
        self.assertFalse(timer.active)
        controller._finish_cancel_confirmation(replay=True)

        start_runner.progress({
            "kind": "source_started",
            "source_path": "C:/raw/a.opju",
            "source_index": 1,
            "source_total": 1,
            "completed_sources": 0,
        })
        self.assertTrue(timer.active)
        controller._cancel_confirmation_pending = True
        start_runner.fail("读取失败")
        self.assertFalse(timer.active)
        controller._finish_cancel_confirmation(replay=True)

        controller._start_extraction_activity(
            source_key=app_module._source_progress_key("C:/raw/a.opju"),
            source_index=1,
            source_total=1,
            completed_sources=0,
        )
        self.assertTrue(timer.active)
        controller._mark_task_cancelled()
        self.assertFalse(timer.active)

    def test_controller_preserves_source_start_when_preflight_and_start_arrive_during_modal(self):
        class FakeSignal:
            def __init__(self):
                self.callback = None

            def connect(self, callback):
                self.callback = callback

        class FakeTimer:
            def __init__(self):
                self.timeout = FakeSignal()
                self.active = False

            def start(self):
                self.active = True

            def stop(self):
                self.active = False

            def fire(self):
                self.timeout.callback()

        clock = [200.0]
        timer = FakeTimer()
        start_runner = RecordingAsyncStartRunRunner()
        controller, _, _, _, _ = self._controller(
            source_paths=("C:/raw/a.opju",),
            output_parent="D:/Organized",
            start_run_runner=start_runner,
            extraction_activity_timer=timer,
            monotonic_clock=lambda: clock[0],
        )
        controller.choose_source_files()
        controller.choose_output_parent()
        updates = []
        controller.widgets["app_run_status"] = FakeLabel()
        controller.update_runtime_view = lambda **kwargs: updates.append(kwargs)

        self.assertTrue(controller.request_start_run())
        controller._cancel_confirmation_pending = True
        update_count = len(updates)
        start_runner.progress({"kind": "pre_extraction_completed"})
        start_runner.progress({
            "kind": "source_started",
            "source_path": "C:/raw/a.opju",
            "source_index": 1,
            "source_total": 1,
            "completed_sources": 0,
        })
        self.assertTrue(timer.active)
        self.assertEqual(update_count, len(updates))

        clock[0] = 209.0
        timer.fire()
        self.assertEqual(update_count, len(updates))
        controller._finish_cancel_confirmation(replay=True)
        self.assertTrue(timer.active)
        timer.fire()
        self.assertEqual("总用时 00:09", updates[-1]["subtitle"])
        self.assertEqual(
            ("source_input", 0, ("a.opju", "等待统计", "正在读取 · 已用时 00:09")),
            updates[-1]["review_row_update"],
        )

    def test_progress_matches_equivalent_windows_path_spellings(self):
        start_runner = RecordingAsyncStartRunRunner()
        controller, _, _, _, _ = self._controller(
            source_paths=("C:/raw/sample.opju",),
            output_parent="C:/out",
            start_run_runner=start_runner,
        )
        controller.choose_source_files()
        controller.choose_output_parent()
        updates = []
        controller.widgets["app_run_status"] = FakeLabel()
        controller.update_runtime_view = lambda **kwargs: updates.append(kwargs)
        self.assertTrue(controller.request_start_run())
        start_runner.progress({"kind": "pre_extraction_completed"})

        start_runner.progress({
            "kind": "source_completed",
            "source_path": "C:\\raw\\sample.opju",
            "source_index": 1,
            "source_total": 1,
            "completed_sources": 1,
            "inventory_count": 7,
            "extracted_count": 6,
            "rejected_count": 1,
        })

        self.assertEqual(
            ("source_input", 0, ("sample.opju", "7", "已提取 6，排除 1")),
            updates[-1]["review_row_update"],
        )
        self.assertNotIn("review_rows", updates[-1])

    def test_start_job_cancel_after_context_creation_skips_child_and_cleans_context(self):
        from spectrum_organizer.safety.owned_paths import create_run_ownership

        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            ownership = create_run_ownership(base, "cancel-run", "cancel-marker", [])
            context = types.SimpleNamespace(
                temp_root=ownership.temp_root,
                temp_root_identity=ownership.temp_root_identity,
            )
            context_created = threading.Event()
            release_builder = threading.Event()
            extraction_calls = []
            errors = []

            def build_context(**kwargs):
                del kwargs
                context_created.set()
                release_builder.wait(5)
                return context

            job = app_module.CancellableStartRunJob(
                build_context,
                lambda approved_context: extraction_calls.append(approved_context),
            )
            job.prepare()
            thread = threading.Thread(
                target=lambda: self._capture_start_job_error(errors, job),
            )
            thread.start()
            self.assertTrue(context_created.wait(2))

            job.cancel()
            release_builder.set()
            thread.join(5)

            self.assertFalse(thread.is_alive())
            self.assertEqual([], extraction_calls)
            self.assertFalse(ownership.temp_root.exists())
            self.assertRegex(str(errors[0]), "取消")

    def test_start_job_keyboard_interrupt_still_cleans_owned_temp_root(self):
        from spectrum_organizer.safety.owned_paths import create_run_ownership

        with tempfile.TemporaryDirectory() as directory:
            ownership = create_run_ownership(
                pathlib.Path(directory),
                "interrupt-run",
                "interrupt-marker",
                [],
            )
            context = types.SimpleNamespace(
                temp_root=ownership.temp_root,
                temp_root_identity=ownership.temp_root_identity,
            )
            job = app_module.CancellableStartRunJob(
                lambda **kwargs: context,
                lambda approved_context: (_ for _ in ()).throw(
                    KeyboardInterrupt("stop")
                ),
            )

            with self.assertRaisesRegex(KeyboardInterrupt, "stop"):
                job(
                    types.SimpleNamespace(
                        selected_source_paths=(),
                        output_parent=None,
                        settings_snapshot=None,
                    )
                )

            self.assertFalse(ownership.temp_root.exists())

    def test_start_job_cancel_delegates_to_cancellable_context_builder(self):
        class CancellableBuilder:
            def __init__(self):
                self.cancelled = False

            def __call__(self, **kwargs):
                del kwargs
                return types.SimpleNamespace(
                    temp_root=None,
                    temp_root_identity=None,
                )

            def cancel(self):
                self.cancelled = True

        builder = CancellableBuilder()
        job = app_module.CancellableStartRunJob(builder, RecordingExtractionRunner())

        job.cancel()

        self.assertTrue(builder.cancelled)

    def test_start_job_prepare_resets_cancellable_context_builder(self):
        class ResettableBuilder:
            def __init__(self):
                self.reset_calls = 0

            def __call__(self, **kwargs):
                del kwargs
                return types.SimpleNamespace(
                    temp_root=None,
                    temp_root_identity=None,
                )

            def reset(self):
                self.reset_calls += 1

        builder = ResettableBuilder()
        job = app_module.CancellableStartRunJob(builder, RecordingExtractionRunner())

        job.prepare()

        self.assertEqual(1, builder.reset_calls)

    def test_start_job_rechecks_origin_processes_after_copy_and_before_extraction(self):
        events = []
        context = types.SimpleNamespace(
            temp_root=None,
            temp_root_identity=None,
        )
        job = app_module.CancellableStartRunJob(
            lambda **kwargs: events.append("context") or context,
            lambda approved_context: events.append("extract") or approved_context,
            pre_origin_process_check=lambda: events.append("origin-check"),
        )

        job(types.SimpleNamespace(selected_source_paths=(), output_parent=None, settings_snapshot=None))

        self.assertEqual(["context", "origin-check", "extract"], events)

    def test_start_job_reports_pre_extraction_complete_only_after_context_and_origin_check(self):
        events = []
        context = types.SimpleNamespace(
            temp_root=None,
            temp_root_identity=None,
        )
        job = app_module.CancellableStartRunJob(
            lambda **kwargs: events.append("context") or context,
            lambda approved_context: events.append("extract") or approved_context,
            pre_origin_process_check=lambda: events.append("origin-check"),
        )
        job.set_progress_callback(lambda event: events.append(event["kind"]))

        job(types.SimpleNamespace(selected_source_paths=(), output_parent=None, settings_snapshot=None))

        self.assertEqual(
            ["context", "origin-check", "pre_extraction_completed", "extract"],
            events,
        )

    def test_start_job_preserves_temp_root_when_process_tree_state_is_unknown(self):
        from spectrum_organizer.product_runner import ExtractionCleanupBlockedError
        from spectrum_organizer.safety.owned_paths import create_run_ownership

        with tempfile.TemporaryDirectory() as directory:
            ownership = create_run_ownership(pathlib.Path(directory), "blocked-run", "blocked-marker", [])
            context = types.SimpleNamespace(
                temp_root=ownership.temp_root,
                temp_root_identity=ownership.temp_root_identity,
            )
            job = app_module.CancellableStartRunJob(
                lambda **kwargs: context,
                lambda approved_context: (_ for _ in ()).throw(
                    ExtractionCleanupBlockedError("进程树状态无法确认")
                ),
            )

            with self.assertRaisesRegex(ExtractionCleanupBlockedError, "进程树状态无法确认"):
                job(types.SimpleNamespace(selected_source_paths=(), output_parent=None, settings_snapshot=None))

            self.assertTrue(ownership.temp_root.exists())

    def test_start_job_retry_cleans_retained_active_extraction_temp_root(self):
        from spectrum_organizer.product_runner import ExtractionCleanupBlockedError
        from spectrum_organizer.safety.owned_paths import create_run_ownership

        class RetryableExtractionRunner:
            def __init__(self):
                self.retry_calls = 0

            def __call__(self, context):
                del context
                raise ExtractionCleanupBlockedError("进程树状态无法确认")

            def retry_cancel_cleanup(self):
                self.retry_calls += 1

        with tempfile.TemporaryDirectory() as directory:
            ownership = create_run_ownership(pathlib.Path(directory), "retry-run", "retry-marker", [])
            context = types.SimpleNamespace(
                temp_root=ownership.temp_root,
                temp_root_identity=ownership.temp_root_identity,
            )
            extraction_runner = RetryableExtractionRunner()
            job = app_module.CancellableStartRunJob(lambda **kwargs: context, extraction_runner)

            with self.assertRaisesRegex(ExtractionCleanupBlockedError, "进程树状态无法确认"):
                job(types.SimpleNamespace(selected_source_paths=(), output_parent=None, settings_snapshot=None))

            self.assertTrue(ownership.temp_root.exists())
            job.retry_cleanup()

            self.assertEqual(1, extraction_runner.retry_calls)
            self.assertFalse(ownership.temp_root.exists())

    def test_start_job_retry_keeps_active_extraction_temp_root_when_delete_fails(self):
        from spectrum_organizer.product_runner import ExtractionCleanupBlockedError
        from spectrum_organizer.safety.owned_paths import create_run_ownership

        class RetryableExtractionRunner:
            def __call__(self, context):
                del context
                raise ExtractionCleanupBlockedError("进程树状态无法确认")

            def retry_cancel_cleanup(self):
                return None

        with tempfile.TemporaryDirectory() as directory:
            ownership = create_run_ownership(pathlib.Path(directory), "retry-run", "retry-marker", [])
            context = types.SimpleNamespace(
                temp_root=ownership.temp_root,
                temp_root_identity=ownership.temp_root_identity,
            )
            job = app_module.CancellableStartRunJob(lambda **kwargs: context, RetryableExtractionRunner())

            with self.assertRaises(ExtractionCleanupBlockedError):
                job(types.SimpleNamespace(selected_source_paths=(), output_parent=None, settings_snapshot=None))

            with mock.patch.object(app_module, "_cleanup_temp_root_error", return_value="locked"):
                with self.assertRaisesRegex(ExtractionCleanupBlockedError, "locked"):
                    job.retry_cleanup()

            self.assertTrue(ownership.temp_root.exists())

    def test_start_job_retry_with_real_runner_cleans_post_reader_temp_root(self):
        from spectrum_organizer.safety.owned_paths import create_run_ownership

        with tempfile.TemporaryDirectory() as directory:
            ownership = create_run_ownership(pathlib.Path(directory), "post-reader-run", "marker", [])
            extraction_runner = product_runner.ExtractionSubprocessRunner(
                process_factory=lambda *args, **kwargs: None,
            )
            extraction_runner._cleanup_blocked_reason = "后续验证失败且临时文件清理失败"
            self.assertIsNone(extraction_runner._current_process)

            job = app_module.CancellableStartRunJob(lambda **kwargs: None, extraction_runner)
            job._cleanup_temp_root = ownership.temp_root
            job._cleanup_temp_root_identity = ownership.temp_root_identity

            job.retry_cleanup()

            self.assertIsNone(extraction_runner._cleanup_blocked_reason)
            self.assertFalse(ownership.temp_root.exists())

    def test_window_close_event_cancels_active_run_and_is_ignored_until_safe(self):
        class FakeQObject:
            def __init__(self, parent=None):
                self.parent = parent

        class FakeQEvent:
            class Type:
                Close = "close"

        class FakeWindow:
            def installEventFilter(self, event_filter):
                self.event_filter = event_filter

        class FakeController:
            run_in_progress = True

            def __init__(self):
                self.cancel_calls = 0

            def cancel_after_preferences(self):
                self.cancel_calls += 1

        class FakeCloseEvent:
            def __init__(self):
                self.ignored = False

            def type(self):
                return "close"

            def ignore(self):
                self.ignored = True

        qt_core = types.SimpleNamespace(QObject=FakeQObject, QEvent=FakeQEvent)
        window = FakeWindow()
        controller = FakeController()
        event = FakeCloseEvent()
        app_module._install_safe_close_filter(window, controller, qt_core)

        handled = window.event_filter.eventFilter(window, event)

        self.assertTrue(handled)
        self.assertTrue(event.ignored)
        self.assertEqual(1, controller.cancel_calls)

    def test_window_close_event_stays_ignored_while_shutdown_is_pending(self):
        class FakeQObject:
            def __init__(self, parent=None):
                self.parent = parent

        class FakeQEvent:
            class Type:
                Close = "close"

        class FakeWindow:
            def installEventFilter(self, event_filter):
                self.event_filter = event_filter

        class FakeController:
            run_in_progress = False
            shutdown_pending = True

            def __init__(self):
                self.cancel_calls = 0

            def cancel_after_preferences(self):
                self.cancel_calls += 1

        class FakeCloseEvent:
            def __init__(self):
                self.ignored = False

            def type(self):
                return "close"

            def ignore(self):
                self.ignored = True

        qt_core = types.SimpleNamespace(QObject=FakeQObject, QEvent=FakeQEvent)
        window = FakeWindow()
        controller = FakeController()
        app_module._install_safe_close_filter(window, controller, qt_core)
        event = FakeCloseEvent()

        handled = window.event_filter.eventFilter(window, event)

        self.assertTrue(handled)
        self.assertTrue(event.ignored)
        self.assertEqual(0, controller.cancel_calls)

    def test_window_close_event_cleans_completed_extraction_context_before_exit(self):
        class FakeQObject:
            def __init__(self, parent=None):
                self.parent = parent

        class FakeQEvent:
            class Type:
                Close = "close"

        class FakeWindow:
            def installEventFilter(self, event_filter):
                self.event_filter = event_filter

        class FakeController:
            run_in_progress = False
            shutdown_pending = False
            _shutdown_exit_blocked = False
            approved_pre_extraction_context = object()

            def __init__(self):
                self.cancel_calls = 0
                self.orchestrator = types.SimpleNamespace(task_cache={})

            def cancel_after_preferences(self):
                self.cancel_calls += 1

        class FakeCloseEvent:
            def __init__(self):
                self.ignored = False

            def type(self):
                return "close"

            def ignore(self):
                self.ignored = True

        qt_core = types.SimpleNamespace(QObject=FakeQObject, QEvent=FakeQEvent)
        window = FakeWindow()
        controller = FakeController()
        event = FakeCloseEvent()
        app_module._install_safe_close_filter(window, controller, qt_core)

        handled = window.event_filter.eventFilter(window, event)

        self.assertTrue(handled)
        self.assertTrue(event.ignored)
        self.assertEqual(1, controller.cancel_calls)

    @staticmethod
    def _capture_start_job_error(errors, job):
        try:
            job(types.SimpleNamespace(
                selected_source_paths=(),
                output_parent="",
                settings_snapshot={},
            ))
        except Exception as exc:
            errors.append(exc)
    def test_start_run_builds_and_stores_approved_pre_extraction_context(self):
        builder = RecordingPreExtractionContextBuilder(result={"run_id": "run-1"})
        controller, settings_path, widgets, message_box, _ = self._controller(
            source_paths=("C:/raw/a.opju", "C:/raw/b.opj"),
            output_parent="D:/Organized",
            pre_extraction_context_builder=builder,
        )
        controller.choose_source_files()
        controller.choose_output_parent()

        self.assertTrue(controller.request_start_run())

        self.assertTrue(controller.run_ready)
        self.assertEqual({"run_id": "run-1"}, getattr(controller, "approved_pre_extraction_context", None))
        self.assertEqual({"run_id": "run-1"}, controller.orchestrator.task_cache["approved_pre_extraction_context"])
        self.assertIn("extraction_summary", controller.orchestrator.task_cache)
        self.assertEqual([], message_box.errors)
        self.assertEqual(1, len(builder.calls))
        self.assertEqual(("C:/raw/a.opju", "C:/raw/b.opj"), builder.calls[0]["selected_source_paths"])
        self.assertEqual("D:/Organized", builder.calls[0]["output_parent"])
        self.assertEqual(
            {"s1Limit": 42, "steadyEmissionY": "S1c/R1c", "allowMissingS1": False},
            builder.calls[0]["settings_snapshot"],
        )
        self.assertIn('"steadyEmissionY": "S1c/R1c"', settings_path.read_text(encoding="utf-8"))
        self.assertIn("已完成提取前安全检查", widgets["run_log"].toPlainText())

    def test_start_run_blocks_without_ready_when_extraction_fails(self):
        extraction_runner = RecordingExtractionRunner(error=RuntimeError("extraction crashed"))
        controller, _, widgets, message_box, _ = self._controller(
            source_paths=("C:/raw/a.opju",),
            output_parent="D:/Organized",
            extraction_runner=extraction_runner,
        )
        controller.choose_source_files()
        controller.choose_output_parent()

        self.assertFalse(controller.request_start_run())

        self.assertFalse(controller.run_ready)
        self.assertEqual([{"run_id": "test-run"}], extraction_runner.calls)
        self.assertEqual("extraction crashed", controller.orchestrator.last_failure)
        self.assertEqual([("谱图数据提取失败", "extraction crashed")], message_box.errors)
        self.assertIn("谱图数据提取失败：extraction crashed", widgets["run_log"].toPlainText())

    def test_start_run_blocks_without_ready_when_pre_extraction_context_fails(self):
        builder = RecordingPreExtractionContextBuilder(error=RuntimeError("temp space blocked"))
        controller, _, widgets, message_box, _ = self._controller(
            source_paths=("C:/raw/a.opju",),
            output_parent="D:/Organized",
            pre_extraction_context_builder=builder,
        )
        controller.choose_source_files()
        controller.choose_output_parent()

        self.assertFalse(controller.request_start_run())

        self.assertFalse(controller.run_ready)
        self.assertIsNone(getattr(controller, "approved_pre_extraction_context", None))
        self.assertEqual("temp space blocked", controller.orchestrator.last_failure)
        self.assertEqual([("谱图数据提取失败", "temp space blocked")], message_box.errors)
        self.assertIn("谱图数据提取失败：temp space blocked", widgets["run_log"].toPlainText())


def _candidate(
    source_id,
    source_filename,
    folder_path,
    short_name,
    display_name,
    *,
    page_type="worksheet",
    spectrum_class=SpectrumClass.STEADY_EMISSION,
    fixed_wavelength="300",
    wavelength_range=("350", "650"),
    scan_increment="1",
    excitation_range=None,
    emission_range=None,
    excitation_increment=None,
    emission_increment=None,
    excitation_slits=("2", "2"),
    emission_slits=("2", "2"),
    flash_delay=None,
    sample_window=None,
    time_per_flash=None,
    flash_count=None,
    x_values=(500,),
    y_values=(100,),
    max_y=100,
    payload_snapshot_path=None,
    payload_checksum=None,
    selected_y_column="S1c",
    paired_x_column="X",
    page_order=None,
):
    return types.SimpleNamespace(
        source_id=source_id,
        source_filename=source_filename,
        page_type=page_type,
        folder_path=folder_path,
        short_name=short_name,
        display_name=display_name,
        page_order=page_order,
        spectrum_class=spectrum_class,
        role=(
            "excitation"
            if spectrum_class
            in {
                SpectrumClass.STEADY_EXCITATION,
                SpectrumClass.DELAYED_EXCITATION,
            }
            else "emission"
        ),
        fixed_wavelength=fixed_wavelength,
        wavelength_range=wavelength_range,
        scan_increment=scan_increment,
        excitation_range=excitation_range,
        emission_range=emission_range,
        excitation_increment=excitation_increment,
        emission_increment=emission_increment,
        excitation_slits=excitation_slits,
        emission_slits=emission_slits,
        flash_delay=flash_delay,
        sample_window=sample_window,
        time_per_flash=time_per_flash,
        flash_count=flash_count,
        max_y=max_y,
        x_at_max_y=450,
        x_values=x_values,
        y_values=y_values,
        note_datetime="2026-07-26 12:00",
        payload_snapshot_path=payload_snapshot_path,
        payload_checksum=payload_checksum or ("c" * 64),
        selected_y_column=selected_y_column,
        paired_x_column=paired_x_column,
        book_key=json.dumps(
            [source_id, page_type, folder_path, short_name],
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    )


def _write_approval_snapshot(path, candidates, *, source_snapshots):
    connection = sqlite3.connect(path)
    try:
        source_ids = tuple(
            dict.fromkeys(candidate.source_id for candidate in candidates)
        )
        if len(source_ids) != len(source_snapshots):
            raise ValueError(
                "source snapshots do not match candidate source ids"
            )
        connection.execute(
            "create table source_files ("
            "source_id text primary key, copy_path text not null, "
            "sha256 text not null, original_path text, "
            "original_size_bytes integer, original_mtime_ns integer)"
        )
        connection.executemany(
            "insert into source_files values (?, ?, ?, ?, ?, ?)",
            tuple(
                (
                    source_id,
                    str(path.parent / f"{source_id}-copy.opju"),
                    source_snapshot.sha256,
                    os.path.normcase(
                        str(source_snapshot.path.resolve())
                    ),
                    source_snapshot.size_bytes,
                    source_snapshot.mtime_ns,
                )
                for source_id, source_snapshot in zip(
                    source_ids,
                    source_snapshots,
                    strict=True,
                )
            ),
        )
        connection.execute(
            "create table book_results ("
            "source_id text, page_type text, folder_path text, "
            "short_name text, display_name text, payload_checksum text, "
            "status text, rejection_reason text, "
            "selected_x_values_json text, selected_y_values_json text, "
            "note_text text, spectrum_class text, "
            "selected_y_column text, paired_x_column text, "
            "s1_max_for_limit_json text, "
            "s1_max_for_limit_x_json text, "
            "max_planned_y_json text, "
            "max_planned_y_x_json text)"
        )
        connection.executemany(
            "insert into book_results values "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                (
                    candidate.source_id,
                    candidate.page_type,
                    candidate.folder_path,
                    candidate.short_name,
                    candidate.display_name,
                    candidate.payload_checksum,
                    (
                        "rejected"
                        if hasattr(candidate, "reason")
                        else "extracted"
                    ),
                    getattr(candidate, "reason", None),
                    json.dumps(list(getattr(candidate, "x_values", ()))),
                    json.dumps(list(getattr(candidate, "y_values", ()))),
                    _candidate_note_text(candidate),
                    str(
                        getattr(
                            getattr(candidate, "spectrum_class", None),
                            "value",
                            "",
                        )
                        or ""
                    ),
                    str(
                        getattr(candidate, "selected_y_column", "")
                        or ""
                    ),
                    str(
                        getattr(candidate, "paired_x_column", "")
                        or ""
                    ),
                    (
                        None
                        if hasattr(candidate, "reason")
                        else json.dumps(candidate.max_y)
                    ),
                    (
                        None
                        if hasattr(candidate, "reason")
                        else json.dumps(candidate.x_at_max_y)
                    ),
                    (
                        None
                        if hasattr(candidate, "reason")
                        else json.dumps(candidate.max_y)
                    ),
                    (
                        None
                        if hasattr(candidate, "reason")
                        else json.dumps(candidate.x_at_max_y)
                    ),
                )
                for candidate in candidates
            ),
        )
        connection.commit()
    finally:
        connection.close()
    return snapshot_approval_sha256(path)


def _candidate_note_text(candidate):
    spectrum_class = getattr(candidate, "spectrum_class", None)
    if spectrum_class is None:
        return "invalid Note"
    acquisition_type = {
        SpectrumClass.STEADY_EMISSION: "Spectral Acquisition[Emission]",
        SpectrumClass.STEADY_EXCITATION: "Spectral Acquisition[Excitation]",
        SpectrumClass.DELAYED_EMISSION: "Phos Acquisition[Emission]",
        SpectrumClass.DELAYED_EXCITATION: "Phos Acquisition[Excitation]",
        SpectrumClass.STEADY_2D: (
            "3D Acquisition[Excitation vs Emission vs Intensity]"
        ),
    }[spectrum_class]
    lines = [
        "[EXP_FD_FILE]",
        f"Acquisition Type = {acquisition_type}",
    ]
    if spectrum_class in {
        SpectrumClass.DELAYED_EMISSION,
        SpectrumClass.DELAYED_EXCITATION,
    }:
        lines.extend(
            (
                f"Flash Delay = {candidate.flash_delay}",
                f"Sample window = {candidate.sample_window}",
                f"Time per Flash = {candidate.time_per_flash}",
                f"Flash Count = {candidate.flash_count}",
            )
        )
    excitation_lines = ["[EX1]"]
    emission_lines = ["[EM1]"]
    if spectrum_class in {
        SpectrumClass.STEADY_EXCITATION,
        SpectrumClass.DELAYED_EXCITATION,
    }:
        excitation_lines.extend(
            (
                f"Start = {candidate.wavelength_range[0]}",
                f"End = {candidate.wavelength_range[1]}",
                f"Increment = {candidate.scan_increment}",
            )
        )
        emission_lines.append(f"Park = {candidate.fixed_wavelength}")
    elif spectrum_class is SpectrumClass.STEADY_2D:
        excitation_lines.extend(
            (
                f"Start = {candidate.excitation_range[0]}",
                f"End = {candidate.excitation_range[1]}",
                f"Increment = {candidate.excitation_increment}",
            )
        )
        emission_lines.extend(
            (
                f"Start = {candidate.emission_range[0]}",
                f"End = {candidate.emission_range[1]}",
                f"Increment = {candidate.emission_increment}",
            )
        )
    else:
        excitation_lines.append(f"Park = {candidate.fixed_wavelength}")
        emission_lines.extend(
            (
                f"Start = {candidate.wavelength_range[0]}",
                f"End = {candidate.wavelength_range[1]}",
                f"Increment = {candidate.scan_increment}",
            )
        )
    for section, slits in (
        (excitation_lines, candidate.excitation_slits),
        (emission_lines, candidate.emission_slits),
    ):
        if slits is not None:
            section.extend(
                (
                    f"Front Entrance Slit = {slits[0]}",
                    f"Front Exit Slit = {slits[1]}",
                )
            )
    return "\n".join((*lines, *excitation_lines, *emission_lines))


def _two_duplicate_special_candidates():
    return tuple(
        _candidate(
            "S1",
            "source.opj",
            "Delayed",
            short_name,
            short_name,
            spectrum_class=SpectrumClass.DELAYED_EMISSION,
            fixed_wavelength=str(wavelength),
            flash_delay="0.1",
            time_per_flash="1.1",
        )
        for short_name, wavelength in (
            ("300", 300),
            ("Pho300_10_10", 300),
            ("350", 350),
            ("400", 400),
            ("450", 450),
            ("Pho450_10_10", 450),
            ("500", 500),
        )
    )

if __name__ == "__main__":
    unittest.main()
