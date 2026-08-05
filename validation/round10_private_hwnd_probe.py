from __future__ import annotations

import argparse
import ctypes
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import uuid
from ctypes import wintypes


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
CREATE_NO_WINDOW = 0x08000000
DESKTOP_ALL_ACCESS = 0x000F01FF
EVENT_OBJECT_CREATE = 0x8000
EVENT_OBJECT_DESTROY = 0x8001
EVENT_OBJECT_SHOW = 0x8002
EVENT_OBJECT_HIDE = 0x8003
EVENT_OBJECT_REORDER = 0x8004
EVENT_OBJECT_FOCUS = 0x8005
EVENT_OBJECT_SELECTION = 0x8006
EVENT_OBJECT_SELECTIONADD = 0x8007
EVENT_OBJECT_SELECTIONREMOVE = 0x8008
EVENT_OBJECT_SELECTIONWITHIN = 0x8009
EVENT_OBJECT_STATECHANGE = 0x800A
EVENT_OBJECT_LOCATIONCHANGE = 0x800B
EVENT_OBJECT_NAMECHANGE = 0x800C
EVENT_OBJECT_DESCRIPTIONCHANGE = 0x800D
EVENT_OBJECT_VALUECHANGE = 0x800E
EVENT_OBJECT_PARENTCHANGE = 0x800F
GW_OWNER = 4
GWL_EXSTYLE = -20
GWL_STYLE = -16
STILL_ACTIVE = 259
WAIT_OBJECT_0 = 0
WAIT_FAILED = 0xFFFFFFFF
WAIT_TIMEOUT = 258
WH_CBT = 5
WINEVENT_OUTOFCONTEXT = 0x0000
CBT_EVENT_NAMES = {
    0: "movesize",
    1: "minmax",
    3: "create",
    4: "destroy",
    5: "activate",
    9: "focus",
}
WIN_EVENT_NAMES = {
    EVENT_OBJECT_CREATE: "create",
    EVENT_OBJECT_DESTROY: "destroy",
    EVENT_OBJECT_SHOW: "show",
    EVENT_OBJECT_HIDE: "hide",
    EVENT_OBJECT_REORDER: "reorder",
    EVENT_OBJECT_FOCUS: "focus",
    EVENT_OBJECT_SELECTION: "selection",
    EVENT_OBJECT_SELECTIONADD: "selection_add",
    EVENT_OBJECT_SELECTIONREMOVE: "selection_remove",
    EVENT_OBJECT_SELECTIONWITHIN: "selection_within",
    EVENT_OBJECT_STATECHANGE: "state_change",
    EVENT_OBJECT_LOCATIONCHANGE: "location",
    EVENT_OBJECT_NAMECHANGE: "name_change",
    EVENT_OBJECT_DESCRIPTIONCHANGE: "description_change",
    EVENT_OBJECT_VALUECHANGE: "value_change",
    EVENT_OBJECT_PARENTCHANGE: "parent",
}
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_MOUSEMOVE = 0x0200
MK_LBUTTON = 0x0001
IGNORED_WINDOW_CLASSES = {
    "CicLoaderWndClass",
    "CiceroUIWndFrame",
    "IME",
    "MSCTFIME UI",
    "Qt6111ScreenChangeObserverWindow",
    "Qt6111ThemeChangeObserverWindow",
    "Touch Tooltip Window",
    "UAC Input Indicator",
    "UAC_InputIndicatorOverlayWnd",
}
EXPECTED_PROBE_STAGES = (
    "preflight",
    "return_folder",
    "apply_remaining",
    "final_remaining",
    "task7_special_duplicate",
    "task7_special_overlap",
    "task7_special_whole",
    "task7_special_books",
    "task7_special_group_books",
    "task7_special_reject",
    "task7_emission_duplicate",
    "task7_excitation_selection",
    "task7_return_attribution",
    "generic_manual",
)
EXPECTED_PROBE_STAGE_SEQUENCE = (
    "startup",
    "preflight",
    "return_folder",
    "return_folder:0:split",
    "return_folder:1:select",
    "return_folder:2:confirm",
    "return_folder:3:return_folder",
    "return_folder:4:confirm",
    "apply_remaining",
    "apply_remaining:0:split",
    "apply_remaining:1:select",
    "apply_remaining:2:confirm",
    "apply_remaining:3:select",
    "apply_remaining:4:confirm_apply",
    "final_remaining",
    "final_remaining:0:split",
    "final_remaining:1:select",
    "final_remaining:2:confirm",
    "final_remaining:3:select",
    "final_remaining:4:confirm",
    "final_remaining:5:select",
    "final_remaining:6:confirm",
    "task7_special_duplicate",
    "task7_special_overlap",
    "task7_special_whole",
    "task7_special_books",
    "task7_special_group_books",
    "task7_special_reject",
    "task7_emission_duplicate",
    "task7_excitation_selection",
    "task7_return_attribution",
    "generic_manual",
    "complete",
)
EXPECTED_DIALOG_TITLES_BY_STAGE = {
    "preflight": "预检设置",
    "return_folder": "确认样品归属",
    "return_folder:0:split": "选择要归属的 Book",
    "return_folder:1:select": "确认样品归属",
    "return_folder:2:confirm": "选择要归属的 Book",
    "return_folder:3:return_folder": "确认样品归属",
    "return_folder:4:confirm": "确认样品归属",
    "apply_remaining": "确认样品归属",
    "apply_remaining:0:split": "选择要归属的 Book",
    "apply_remaining:1:select": "确认样品归属",
    "apply_remaining:2:confirm": "选择要归属的 Book",
    "apply_remaining:3:select": "确认样品归属",
    "apply_remaining:4:confirm_apply": "确认样品归属",
    "final_remaining": "确认样品归属",
    "final_remaining:0:split": "选择要归属的 Book",
    "final_remaining:1:select": "确认样品归属",
    "final_remaining:2:confirm": "选择要归属的 Book",
    "final_remaining:3:select": "确认样品归属",
    "final_remaining:4:confirm": "选择要归属的 Book",
    "final_remaining:5:select": "确认样品归属",
    "final_remaining:6:confirm": "确认样品归属",
    "task7_special_duplicate": "选择特殊组重复点",
    "task7_special_overlap": "确认特殊组归属",
    "task7_special_whole": "确认特殊谱组",
    "task7_special_books": "确认特殊谱组",
    "task7_special_group_books": "逐 Book 确认特殊谱组",
    "task7_special_reject": "确认特殊谱组",
    "task7_emission_duplicate": "选择重复发射谱",
    "task7_excitation_selection": "选择激发谱",
    "task7_return_attribution": "选择重复发射谱",
    "generic_manual": "验证通用提示",
}
TRANSITION_DIALOG_STAGES = {
    stage
    for stage in EXPECTED_DIALOG_TITLES_BY_STAGE
    if stage.startswith(
        ("return_folder:", "apply_remaining:", "final_remaining:")
    )
    and stage
    not in {
        "return_folder:4:confirm",
        "apply_remaining:4:confirm_apply",
        "final_remaining:6:confirm",
    }
}
ALLOWED_DIALOG_TITLES_BY_STAGE = {
    stage: (
        {"确认样品归属", "选择要归属的 Book"}
        if stage in TRANSITION_DIALOG_STAGES
        else {title}
    )
    for stage, title in EXPECTED_DIALOG_TITLES_BY_STAGE.items()
}
EXPECTED_DIALOG_SHOW_TITLES_BY_STAGE = {
    stage: title
    for stage, title in EXPECTED_DIALOG_TITLES_BY_STAGE.items()
    if stage
    not in {
        "return_folder:4:confirm",
        "apply_remaining:4:confirm_apply",
        "final_remaining:6:confirm",
    }
}


class StartupInfo(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(ctypes.c_ubyte)),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class ProcessInformation(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


def _windows_libraries() -> tuple[ctypes.WinDLL, ctypes.WinDLL, ctypes.WinDLL]:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
    return user32, kernel32, dwmapi


def _configure_windows_api(
    user32: ctypes.WinDLL,
    kernel32: ctypes.WinDLL,
    dwmapi: ctypes.WinDLL,
) -> None:
    user32.CreateDesktopW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
    ]
    user32.CreateDesktopW.restype = wintypes.HANDLE
    user32.EnumDesktopWindows.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        wintypes.LPARAM,
    ]
    user32.EnumDesktopWindows.restype = wintypes.BOOL
    user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetWindow.restype = wintypes.HWND
    user32.GetParent.argtypes = [wintypes.HWND]
    user32.GetParent.restype = wintypes.HWND
    user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    user32.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND,
        ctypes.POINTER(wintypes.DWORD),
    ]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetClassNameW.argtypes = [
        wintypes.HWND,
        wintypes.LPWSTR,
        ctypes.c_int,
    ]
    user32.GetClassNameW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [
        wintypes.HWND,
        wintypes.LPWSTR,
        ctypes.c_int,
    ]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetWindowRect.argtypes = [
        wintypes.HWND,
        ctypes.POINTER(wintypes.RECT),
    ]
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.PostMessageW.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    user32.PostMessageW.restype = wintypes.BOOL
    user32.SendMessageW.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    user32.SendMessageW.restype = ctypes.c_ssize_t
    user32.UnhookWindowsHookEx.argtypes = [wintypes.HANDLE]
    user32.UnhookWindowsHookEx.restype = wintypes.BOOL
    user32.UnhookWinEvent.argtypes = [wintypes.HANDLE]
    user32.UnhookWinEvent.restype = wintypes.BOOL
    user32.CloseDesktop.argtypes = [wintypes.HANDLE]
    user32.CloseDesktop.restype = wintypes.BOOL
    kernel32.CreateProcessW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.BOOL,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.LPCWSTR,
        ctypes.POINTER(StartupInfo),
        ctypes.POINTER(ProcessInformation),
    ]
    kernel32.CreateProcessW.restype = wintypes.BOOL
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    kernel32.WaitForSingleObject.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
    ]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.TerminateProcess.argtypes = [
        wintypes.HANDLE,
        wintypes.UINT,
    ]
    kernel32.TerminateProcess.restype = wintypes.BOOL
    kernel32.GetExitCodeProcess.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    dwmapi.DwmGetWindowAttribute.argtypes = [
        wintypes.HWND,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long


def _pack_point(x: int, y: int) -> int:
    return (x & 0xFFFF) | ((y & 0xFFFF) << 16)


class _WindowSnapshotRace(RuntimeError):
    pass


def _window_event_identity(
    user32: object,
    hwnd: int,
) -> tuple[str, str]:
    class_name = ctypes.create_unicode_buffer(256)
    title = ctypes.create_unicode_buffer(512)
    ctypes.set_last_error(0)
    class_length = user32.GetClassNameW(
        hwnd,
        class_name,
        len(class_name),
    )
    class_error = ctypes.get_last_error()
    if not class_length:
        if class_error == 1400:
            raise _WindowSnapshotRace("GetClassNameW lost HWND")
        raise RuntimeError(f"GetClassNameW failed: {class_error}")
    ctypes.set_last_error(0)
    title_length = user32.GetWindowTextW(
        hwnd,
        title,
        len(title),
    )
    title_error = ctypes.get_last_error()
    if not title_length and title_error:
        if title_error == 1400:
            raise _WindowSnapshotRace("GetWindowTextW lost HWND")
        raise RuntimeError(f"GetWindowTextW failed: {title_error}")
    return class_name.value, title.value


def _child_dialog(title: str):
    from PySide6 import QtWidgets

    application = QtWidgets.QApplication.instance()
    if application is None:
        raise RuntimeError("QApplication is not running")
    return next(
        (
            widget
            for widget in application.topLevelWidgets()
            if isinstance(widget, QtWidgets.QDialog)
            and widget.isVisible()
            and widget.windowTitle() == title
        ),
        None,
    )


def _run_child(result_path: Path) -> int:
    sys.path.insert(0, str(SRC))

    from PySide6 import QtCore, QtTest, QtWidgets

    from spectrum_organizer.core.selection import CandidateConversionResult
    from spectrum_organizer.domain.models import SpectrumClass
    from spectrum_organizer.settings import SettingsStore
    from spectrum_organizer.ui.app import FullRunUiController, QtPreflightDialogPort
    from spectrum_organizer.ui.dialog_port import (
        ConflictReviewChoice,
        ConflictReviewRequest,
        QtConflictReviewDialogPort,
        QtManualDialogPort,
    )
    from spectrum_organizer.ui.dialogs import DialogRequest
    from spectrum_organizer.ui.orchestrator import BookOnlyOrchestrator
    from spectrum_organizer.ui.qt_main_window import create_production_main_window

    user32, kernel32, dwmapi = _windows_libraries()
    _configure_windows_api(user32, kernel32, dwmapi)
    lifecycle_events: list[dict[str, object]] = []
    qt_object_events: list[dict[str, object]] = []
    qt_top_level_samples: list[dict[str, object]] = []
    callback_errors: list[str] = []
    stage_state = {"value": "startup"}
    native_show_sampler = None

    def now_us() -> int:
        return time.perf_counter_ns() // 1_000

    stage_transitions = [{"time_us": now_us(), "stage": "startup"}]

    win_event_callback_type = ctypes.WINFUNCTYPE(
        None,
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.HWND,
        wintypes.LONG,
        wintypes.LONG,
        wintypes.DWORD,
        wintypes.DWORD,
    )

    @win_event_callback_type
    def observe_win_event(
        _hook: int,
        event: int,
        hwnd: int,
        object_id: int,
        child_id: int,
        _thread_id: int,
        _event_time: int,
    ) -> None:
        try:
            if not hwnd or object_id != 0 or child_id != 0:
                return
            class_name, title = _window_event_identity(user32, hwnd)
            lifecycle_events.append(
                {
                    "time_us": now_us(),
                    "stage": stage_state["value"],
                    "source": "winevent",
                    "event": WIN_EVENT_NAMES.get(int(event), hex(int(event))),
                    "hwnd": int(hwnd),
                    "class": class_name,
                    "title": title,
                }
            )
            if (
                WIN_EVENT_NAMES.get(int(event)) == "show"
                and native_show_sampler is not None
            ):
                native_show_sampler()
        except _WindowSnapshotRace:
            return
        except Exception as error:
            callback_errors.append(f"WinEvent callback failed: {error!r}")

    user32.SetWinEventHook.argtypes = [
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HMODULE,
        win_event_callback_type,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    user32.SetWinEventHook.restype = wintypes.HANDLE
    win_event_hook = user32.SetWinEventHook(
        EVENT_OBJECT_CREATE,
        EVENT_OBJECT_PARENTCHANGE,
        None,
        observe_win_event,
        os.getpid(),
        0,
        WINEVENT_OUTOFCONTEXT,
    )
    if not win_event_hook:
        raise ctypes.WinError(ctypes.get_last_error())

    cbt_callback_type = ctypes.WINFUNCTYPE(
        ctypes.c_ssize_t,
        ctypes.c_int,
        wintypes.WPARAM,
        wintypes.LPARAM,
    )

    @cbt_callback_type
    def observe_cbt(code: int, wparam: int, lparam: int) -> int:
        try:
            if code in CBT_EVENT_NAMES and wparam:
                lifecycle_events.append(
                    {
                        "time_us": now_us(),
                        "stage": stage_state["value"],
                        "source": "cbt",
                        "event": CBT_EVENT_NAMES[code],
                        "hwnd": int(wparam),
                        "class": "",
                        "title": "",
                    }
                )
            return user32.CallNextHookEx(None, code, wparam, lparam)
        except Exception as error:
            callback_errors.append(f"CBT callback failed: {error!r}")
            return 0

    user32.CallNextHookEx.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    user32.CallNextHookEx.restype = ctypes.c_ssize_t
    user32.SetWindowsHookExW.argtypes = [
        ctypes.c_int,
        cbt_callback_type,
        wintypes.HINSTANCE,
        wintypes.DWORD,
    ]
    user32.SetWindowsHookExW.restype = wintypes.HANDLE
    cbt_hook = user32.SetWindowsHookExW(
        WH_CBT,
        observe_cbt,
        None,
        kernel32.GetCurrentThreadId(),
    )
    if not cbt_hook:
        user32.UnhookWinEvent(win_event_hook)
        raise ctypes.WinError(ctypes.get_last_error())

    application = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    application.setQuitOnLastWindowClosed(False)

    class QtLifecycleFilter(QtCore.QObject):
        def eventFilter(self, watched, event) -> bool:
            if isinstance(watched, QtWidgets.QWidget) and event.type() in {
                QtCore.QEvent.Type.Show,
                QtCore.QEvent.Type.Hide,
                QtCore.QEvent.Type.ParentChange,
                QtCore.QEvent.Type.ParentAboutToChange,
                QtCore.QEvent.Type.WinIdChange,
            }:
                parent_widget = watched.parentWidget()
                qt_object_events.append(
                    {
                        "time_us": now_us(),
                        "stage": stage_state["value"],
                        "event": event.type().name,
                        "class": watched.metaObject().className(),
                        "object_name": watched.objectName(),
                        "title": watched.windowTitle(),
                        "hwnd": int(watched.internalWinId() or 0),
                        "is_window": watched.isWindow(),
                        "flags": int(watched.windowFlags()),
                        "visible": watched.isVisible(),
                        "parent_class": (
                            parent_widget.metaObject().className()
                            if parent_widget is not None
                            else ""
                        ),
                        "parent_object_name": (
                            parent_widget.objectName()
                            if parent_widget is not None
                            else ""
                        ),
                        "geometry": [
                            watched.x(),
                            watched.y(),
                            watched.width(),
                            watched.height(),
                        ],
                    }
                )
            return False

    qt_lifecycle_filter = QtLifecycleFilter(application)
    application.installEventFilter(qt_lifecycle_filter)

    top_level_state: dict[tuple[str, int], dict[str, object]] = {}

    def sample_qt_top_levels(*, force_visible: bool = False) -> None:
        current: dict[tuple[str, int], dict[str, object]] = {}
        for widget in application.topLevelWidgets():
            if not widget.isVisible():
                continue
            handle = widget.windowHandle()
            snapshot = {
                "kind": "widget",
                "qt_id": id(widget),
                "hwnd": int(handle.winId()) if handle is not None else 0,
                "class": widget.metaObject().className(),
                "object_name": widget.objectName(),
                "title": widget.windowTitle(),
                "flags": int(widget.windowFlags()),
                "geometry": [
                    widget.x(),
                    widget.y(),
                    widget.width(),
                    widget.height(),
                ],
                "size_hint": [
                    widget.sizeHint().width(),
                    widget.sizeHint().height(),
                ],
            }
            current[("widget", id(widget))] = snapshot
        for window in application.topLevelWindows():
            if not window.isVisible():
                continue
            snapshot = {
                "kind": "window",
                "qt_id": id(window),
                "hwnd": int(window.winId()),
                "class": window.metaObject().className(),
                "object_name": window.objectName(),
                "title": window.title(),
                "flags": int(window.flags()),
                "geometry": [
                    window.x(),
                    window.y(),
                    window.width(),
                    window.height(),
                ],
            }
            current[("window", id(window))] = snapshot
        timestamp_us = now_us()
        for key, snapshot in current.items():
            if force_visible or top_level_state.get(key) != snapshot:
                qt_top_level_samples.append(
                    {
                        "time_us": timestamp_us,
                        "stage": stage_state["value"],
                        "state": "visible",
                        **snapshot,
                    }
                )
        for key, snapshot in top_level_state.items():
            if key not in current:
                qt_top_level_samples.append(
                    {
                        "time_us": timestamp_us,
                        "stage": stage_state["value"],
                        "state": "not_visible",
                        **snapshot,
                    }
                )
        top_level_state.clear()
        top_level_state.update(current)

    def sample_after_native_show() -> None:
        sample_qt_top_levels(force_visible=True)

    native_show_sampler = sample_after_native_show
    qt_top_level_timer = QtCore.QTimer(application)
    qt_top_level_timer.setInterval(1)
    qt_top_level_timer.timeout.connect(sample_qt_top_levels)
    qt_top_level_timer.start()

    parent, _widgets = create_production_main_window(
        dpi_percent=100,
        size_name="desktop",
        stage="attribution",
    )
    parent.move(240, 180)
    parent.show()
    application.processEvents()
    ready_path = result_path.with_suffix(result_path.suffix + ".ready")
    observing_path = result_path.with_suffix(
        result_path.suffix + ".observing"
    )
    ready_path.write_text(
        "ready",
        encoding="ascii",
    )
    observing_deadline = time.monotonic() + 8.0
    while not observing_path.is_file():
        if time.monotonic() >= observing_deadline:
            raise RuntimeError(
                "parent did not begin private-desktop observation"
            )
        QtTest.QTest.qWait(1)

    completed_stages: list[str] = []

    def mark(stage: str) -> None:
        sample_qt_top_levels()
        stage_state["value"] = stage
        stage_transitions.append({"time_us": now_us(), "stage": stage})
        parent.setWindowTitle(f"ROUND10|{stage}")
        top_level_state.clear()
        application.processEvents()
        sample_qt_top_levels()
        QtTest.QTest.qWait(12)

    def find_button(dialog, text: str):
        return next(
            (
                button
                for button in dialog.findChildren(QtWidgets.QPushButton)
                if button.text() == text
            ),
            None,
        )

    def native_click(dialog, widget) -> None:
        point = widget.mapTo(
            dialog,
            QtCore.QPoint(6, widget.rect().center().y()),
        )
        packed = _pack_point(point.x(), point.y())
        hwnd = int(dialog.winId())
        user32.PostMessageW(hwnd, WM_LBUTTONDOWN, MK_LBUTTON, packed)
        user32.PostMessageW(hwnd, WM_LBUTTONUP, 0, packed)

    def drive_preflight() -> None:
        dialog = application.activeModalWidget()
        if dialog is None:
            raise RuntimeError("preflight dialog did not become modal")
        header = dialog.findChild(QtWidgets.QFrame, "dialog_header")
        checkbox = next(
            (
                box
                for box in dialog.findChildren(QtWidgets.QCheckBox)
                if box.text() == "允许缺少 S1 列"
            ),
            None,
        )
        confirm = find_button(dialog, "确认")
        if header is None or checkbox is None or confirm is None:
            raise RuntimeError("preflight controls are incomplete")

        hwnd = int(dialog.winId())
        start = header.mapTo(dialog, header.rect().center())
        finish = start + QtCore.QPoint(28, 18)
        before = dialog.frameGeometry().topLeft()
        user32.SendMessageW(
            hwnd,
            WM_LBUTTONDOWN,
            MK_LBUTTON,
            _pack_point(start.x(), start.y()),
        )
        user32.SendMessageW(
            hwnd,
            WM_MOUSEMOVE,
            MK_LBUTTON,
            _pack_point(finish.x(), finish.y()),
        )
        user32.SendMessageW(
            hwnd,
            WM_LBUTTONUP,
            0,
            _pack_point(finish.x(), finish.y()),
        )
        application.processEvents()
        if dialog.frameGeometry().topLeft() == before:
            raise RuntimeError("native preflight drag did not move the dialog")
        native_click(dialog, checkbox)
        QtCore.QTimer.singleShot(80, confirm.click)

    mark("preflight")
    QtCore.QTimer.singleShot(80, drive_preflight)
    preflight = QtPreflightDialogPort(QtWidgets, QtCore).confirm(
        parent,
        default_s1_limit=1_000_000,
        steady_emission_y="S1c",
    )
    if preflight is None or not preflight["allow_missing_s1"]:
        raise RuntimeError(f"unexpected preflight result: {preflight!r}")
    completed_stages.append("preflight")
    QtTest.QTest.qWait(25)

    class InertFileDialogs:
        def select_origin_sources(self, _parent):
            return []

        def select_output_parent(self, _parent):
            return ""

    class InertMessageBox:
        def __init__(self) -> None:
            self.errors: list[tuple[str, str]] = []

        def blocking_error(self, _parent, *, title: str, message: str) -> None:
            self.errors.append((title, message))

    def candidate(index: int) -> SimpleNamespace:
        short_name = f"Book{index}"
        folder_path = "Folder A"
        return SimpleNamespace(
            source_id="source-1",
            source_filename="source.opj",
            page_type="worksheet",
            folder_path=folder_path,
            short_name=short_name,
            display_name=short_name,
            spectrum_class=SpectrumClass.STEADY_EMISSION,
            role="emission",
            fixed_wavelength=str(300 + index),
            wavelength_range=("350", "650"),
            scan_increment="1",
            excitation_slits=("2", "2"),
            emission_slits=("2", "2"),
            flash_delay=None,
            sample_window=None,
            time_per_flash=None,
            flash_count=None,
            max_y=100,
            x_at_max_y=450,
            note_datetime="2026-07-26 12:00",
            book_key=json.dumps(
                ["source-1", "worksheet", folder_path, short_name],
                separators=(",", ":"),
            ),
        )

    def fill_and_confirm(dialog, *, apply_to_remaining: bool) -> None:
        sample_type = next(
            (
                combo
                for combo in dialog.findChildren(QtWidgets.QComboBox)
                if combo.findData("solution") >= 0
            ),
            None,
        )
        if sample_type is None:
            raise RuntimeError("sample-type selector is missing")
        sample_type.setCurrentIndex(sample_type.findData("solution"))
        application.processEvents()
        fields = [
            field
            for field in dialog.findChildren(QtWidgets.QLineEdit)
            if field.isVisible()
        ]
        values = ("NDI", "DCM", "1e-4", "298")
        if len(fields) != len(values):
            raise RuntimeError(f"unexpected visible fields: {len(fields)}")
        for field, value in zip(fields, values):
            field.setText(value)
        if apply_to_remaining:
            checkbox = next(
                (
                    box
                    for box in dialog.findChildren(QtWidgets.QCheckBox)
                    if "其余未确认 Book" in box.text()
                ),
                None,
            )
            if checkbox is None or not checkbox.isVisible():
                raise RuntimeError("apply-to-remaining checkbox is missing")
            checkbox.setChecked(True)
        confirm = find_button(dialog, "确认")
        if confirm is None:
            raise RuntimeError("attribution confirm button is missing")
        confirm.click()

    scenarios = (
        (
            "return_folder",
            ("split", "select", "confirm", "return_folder", "confirm"),
        ),
        (
            "apply_remaining",
            ("split", "select", "confirm", "select", "confirm_apply"),
        ),
        (
            "final_remaining",
            (
                "split",
                "select",
                "confirm",
                "select",
                "confirm",
                "select",
                "confirm",
            ),
        ),
    )

    for scenario_name, actions in scenarios:
        candidates = tuple(candidate(index) for index in range(1, 4))
        conversion = CandidateConversionResult(candidates, (), ())
        summary = {
            "total_inventory_count": len(candidates),
            "total_extracted_count": len(candidates),
            "total_rejected_count": 0,
            "source_summaries": (),
        }
        settings_path = result_path.with_name(
            f"{result_path.stem}-{scenario_name}-settings.json"
        )
        orchestrator = BookOnlyOrchestrator(SettingsStore(settings_path))
        orchestrator.task_cache["extraction_summary"] = summary
        message_box = InertMessageBox()
        controller = FullRunUiController(
            parent=parent,
            widgets=_widgets,
            orchestrator=orchestrator,
            file_dialogs=InertFileDialogs(),
            message_box=message_box,
            preflight_dialog=QtPreflightDialogPort(QtWidgets, QtCore),
        )
        action_state = {
            "index": 0,
            "marked_index": None,
            "error": None,
        }

        def drive_controller_dialog() -> None:
            if action_state["error"] is not None:
                return
            index = action_state["index"]
            if index >= len(actions):
                return
            action = actions[index]
            expected_title = (
                "选择要归属的 Book"
                if action in {"select", "return_folder"}
                else "确认样品归属"
            )
            dialog = _child_dialog(expected_title)
            if dialog is None:
                return
            try:
                if action_state["marked_index"] != index:
                    mark(f"{scenario_name}:{index}:{action}")
                    action_state["marked_index"] = index
                    return
                if action == "split":
                    button = find_button(dialog, "逐 Book")
                    if button is None:
                        raise RuntimeError("per-Book button is missing")
                    button.click()
                elif action == "select":
                    book_list = dialog.findChild(
                        QtWidgets.QListWidget,
                        "attribution_pending_book_list",
                    )
                    if book_list is None:
                        raise RuntimeError("pending Book list is missing")
                    book_list.setCurrentRow(0)
                    button = find_button(dialog, "确认选择")
                    if button is None:
                        raise RuntimeError("Book confirm button is missing")
                    button.click()
                elif action == "return_folder":
                    button = find_button(dialog, "返回 Folder 统一归属")
                    if button is None:
                        raise RuntimeError("Folder return button is missing")
                    button.click()
                else:
                    fill_and_confirm(
                        dialog,
                        apply_to_remaining=action == "confirm_apply",
                    )
                action_state["index"] += 1
                action_state["marked_index"] = None
            except Exception as error:
                action_state["error"] = error
                dialog.reject()

        driver = QtCore.QTimer()
        driver.setInterval(40)
        driver.timeout.connect(drive_controller_dialog)
        mark(scenario_name)
        driver.start()
        controller._begin_attribution(summary, conversion=conversion)
        driver.stop()
        if action_state["error"] is not None:
            raise action_state["error"]
        if action_state["index"] != len(actions):
            raise RuntimeError(
                f"{scenario_name} stopped at action "
                f"{action_state['index']}/{len(actions)}"
            )
        if message_box.errors:
            raise RuntimeError(
                f"{scenario_name} controller errors: {message_box.errors!r}"
            )
        assignments = orchestrator.task_cache.get("attribution_assignments", {})
        if len(assignments) != len(candidates):
            raise RuntimeError(
                f"{scenario_name} assignments={len(assignments)}"
            )
        completed_stages.append(scenario_name)
        QtTest.QTest.qWait(40)

    conflict_choices = (
        ConflictReviewChoice("book-a", "Book A", (("条件", "A"),)),
        ConflictReviewChoice("book-b", "Book B", (("条件", "B"),)),
        ConflictReviewChoice("book-c", "Book C", (("条件", "C"),)),
    )

    def run_conflict_scenario(
        stage_name: str,
        request: ConflictReviewRequest,
        *,
        button_text: str,
        selected_rows: tuple[int, ...] = (),
        expected_action: str,
    ) -> None:
        error = {"value": None}

        def drive() -> None:
            dialog = _child_dialog(request.title)
            if dialog is None:
                return
            try:
                tree = dialog.findChild(
                    QtWidgets.QTreeWidget,
                    "conflict_review_candidates",
                )
                if tree is None:
                    raise RuntimeError("conflict-review table is missing")
                tree.clearSelection()
                for row in selected_rows:
                    tree.topLevelItem(row).setSelected(True)
                button = find_button(dialog, button_text)
                if button is None:
                    raise RuntimeError(
                        f"conflict-review action is missing: {button_text}"
                    )
                button.click()
            except Exception as failure:
                error["value"] = failure
                dialog.reject()

        mark(stage_name)
        driver = QtCore.QTimer()
        driver.setInterval(40)
        driver.timeout.connect(drive)
        driver.start()
        response = QtConflictReviewDialogPort().choose(
            request,
            parent=parent,
        )
        driver.stop()
        if error["value"] is not None:
            raise error["value"]
        if response.action != expected_action:
            raise RuntimeError(
                f"{stage_name} returned {response.action!r}"
            )
        completed_stages.append(stage_name)
        QtTest.QTest.qWait(25)

    run_conflict_scenario(
        "task7_special_duplicate",
        ConflictReviewRequest(
            kind="special_duplicate",
            title="选择特殊组重复点",
            instruction="同一点存在重复 Book，必须保留一个。",
            choices=conflict_choices[:2],
            selection_mode="single",
        ),
        button_text="确认选择",
        selected_rows=(0,),
        expected_action="confirm_selection",
    )
    run_conflict_scenario(
        "task7_special_overlap",
        ConflictReviewRequest(
            kind="special_overlap",
            title="确认特殊组归属",
            instruction="请选择唯一归属。",
            choices=conflict_choices[:2],
            selection_mode="single",
        ),
        button_text="确认选择",
        selected_rows=(0,),
        expected_action="confirm_selection",
    )

    special_request = ConflictReviewRequest(
        kind="special_group",
        title="确认特殊谱组",
        instruction="请选择处理方式。",
        choices=conflict_choices,
        selection_mode="none",
        actions=("confirm_group", "review_books", "reject_group"),
    )
    for stage_name, button_text, expected_action in (
        ("task7_special_whole", "确认整个组", "confirm_group"),
        ("task7_special_books", "逐 Book 审核", "review_books"),
    ):
        run_conflict_scenario(
            stage_name,
            special_request,
            button_text=button_text,
            expected_action=expected_action,
        )

    run_conflict_scenario(
        "task7_special_group_books",
        ConflictReviewRequest(
            kind="special_group_books",
            title="逐 Book 确认特殊谱组",
            instruction="保留属于该特殊组的 Book。",
            choices=conflict_choices,
            selection_mode="multi",
            initial_selection=("book-a", "book-b"),
        ),
        button_text="确认选择",
        selected_rows=(0, 1),
        expected_action="confirm_selection",
    )
    run_conflict_scenario(
        "task7_special_reject",
        special_request,
        button_text="拒绝整个组",
        expected_action="reject_group",
    )

    run_conflict_scenario(
        "task7_emission_duplicate",
        ConflictReviewRequest(
            kind="emission_duplicate",
            title="选择重复发射谱",
            instruction="必须且只能保留一个。",
            choices=conflict_choices[:2],
            selection_mode="single",
        ),
        button_text="确认选择",
        selected_rows=(0,),
        expected_action="confirm_selection",
    )
    run_conflict_scenario(
        "task7_excitation_selection",
        ConflictReviewRequest(
            kind="excitation_selection",
            title="选择激发谱",
            instruction="至少保留一个；完全重复组必须且只能保留一个。",
            choices=conflict_choices,
            selection_mode="multi",
            single_select_groups=(("book-a", "book-b"),),
        ),
        button_text="确认选择",
        selected_rows=(0, 2),
        expected_action="confirm_selection",
    )
    run_conflict_scenario(
        "task7_return_attribution",
        ConflictReviewRequest(
            kind="emission_duplicate",
            title="选择重复发射谱",
            instruction="可返回样品归属。",
            choices=conflict_choices[:2],
            selection_mode="single",
            actions=(
                "confirm_selection",
                "return_to_attribution",
                "cancel",
            ),
        ),
        button_text="返回样品归属",
        expected_action="return_to_attribution",
    )

    def continue_generic_dialog() -> None:
        dialog = _child_dialog("验证通用提示")
        if dialog is None:
            raise RuntimeError("generic manual dialog is missing")
        button = find_button(dialog, "继续")
        if button is None:
            raise RuntimeError("generic manual action is missing")
        button.click()

    mark("generic_manual")
    QtCore.QTimer.singleShot(80, continue_generic_dialog)
    generic_response = QtManualDialogPort(parent=parent).choose(
        DialogRequest(
            kind="inspect",
            title="验证通用提示",
            message="验证通用人工决策弹窗。",
            actions=("continue",),
            topmost=True,
            taskbar_visible=True,
        )
    )
    if generic_response.action != "continue":
        raise RuntimeError(
            f"generic manual dialog returned {generic_response.action!r}"
        )
    completed_stages.append("generic_manual")
    QtTest.QTest.qWait(25)

    mark("complete")
    QtTest.QTest.qWait(120)
    parent.hide()
    parent.destroy(True, True)
    application.processEvents()
    if not user32.UnhookWindowsHookEx(cbt_hook):
        raise ctypes.WinError(ctypes.get_last_error())
    if not user32.UnhookWinEvent(win_event_hook):
        raise ctypes.WinError(ctypes.get_last_error())
    callback_errors.extend(
        getattr(application, "_native_titlebar_quarantine_errors", ())
    )
    if callback_errors:
        raise RuntimeError("; ".join(callback_errors))
    result_path.write_text(
        json.dumps(
            {
                "status": "ok",
                "completed_stages": completed_stages,
                "lifecycle_events": lifecycle_events,
                "qt_object_events": qt_object_events,
                "qt_top_level_samples": qt_top_level_samples,
                "stage_transitions": stage_transitions,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return 0


def _window_snapshot(
    hwnd: int,
    user32: ctypes.WinDLL,
    dwmapi: ctypes.WinDLL,
) -> dict[str, object]:
    def checked_call(
        name: str,
        function: object,
        *args: object,
        zero_is_valid: bool,
    ) -> object:
        ctypes.set_last_error(0)
        result = function(*args)
        error = ctypes.get_last_error()
        if not result and error == 1400:
            raise _WindowSnapshotRace(f"{name} lost HWND")
        if not result and (error or not zero_is_valid):
            raise RuntimeError(f"{name} failed: {error}")
        return result

    class_name = ctypes.create_unicode_buffer(256)
    title = ctypes.create_unicode_buffer(512)
    rectangle = wintypes.RECT()
    cloaked = wintypes.DWORD()
    checked_call(
        "GetClassNameW",
        user32.GetClassNameW,
        hwnd,
        class_name,
        len(class_name),
        zero_is_valid=False,
    )
    checked_call(
        "GetWindowTextW",
        user32.GetWindowTextW,
        hwnd,
        title,
        len(title),
        zero_is_valid=True,
    )
    checked_call(
        "GetWindowRect",
        user32.GetWindowRect,
        hwnd,
        ctypes.byref(rectangle),
        zero_is_valid=False,
    )
    dwm_result = int(
        dwmapi.DwmGetWindowAttribute(
            hwnd,
            14,
            ctypes.byref(cloaked),
            ctypes.sizeof(cloaked),
        )
    )
    if dwm_result < 0:
        dwm_error = dwm_result & 0xFFFFFFFF
        if dwm_error == 0x80070006:
            raise _WindowSnapshotRace("DwmGetWindowAttribute lost HWND")
        raise RuntimeError(
            "DwmGetWindowAttribute failed: "
            f"0x{dwm_error:08X}"
        )
    style = int(
        checked_call(
            "GetWindowLongPtrW",
            user32.GetWindowLongPtrW,
            hwnd,
            GWL_STYLE,
            zero_is_valid=True,
        )
    )
    exstyle = int(
        checked_call(
            "GetWindowLongPtrW",
            user32.GetWindowLongPtrW,
            hwnd,
            GWL_EXSTYLE,
            zero_is_valid=True,
        )
    )
    owner = int(
        checked_call(
            "GetWindow",
            user32.GetWindow,
            hwnd,
            GW_OWNER,
            zero_is_valid=True,
        )
        or 0
    )
    parent = int(
        checked_call(
            "GetParent",
            user32.GetParent,
            hwnd,
            zero_is_valid=True,
        )
        or 0
    )
    visible = bool(user32.IsWindowVisible(hwnd))
    if visible != bool(style & 0x10000000):
        raise _WindowSnapshotRace(
            "window visibility changed during snapshot"
        )
    return {
        "hwnd": int(hwnd),
        "class": class_name.value,
        "title": title.value,
        "rect": [
            rectangle.left,
            rectangle.top,
            rectangle.right,
            rectangle.bottom,
        ],
        "style": style,
        "exstyle": exstyle,
        "owner": owner,
        "parent": parent,
        "visible": visible,
        "cloaked": int(cloaked.value),
    }


def _report_violations(report: dict[str, object]) -> tuple[str, ...]:
    violations: list[str] = []
    child_result = report.get("child_result")
    child = child_result if isinstance(child_result, dict) else {}
    visible_events = report.get("visible_events")
    events = visible_events if isinstance(visible_events, list) else []

    def is_nonnegative_int(value: object) -> bool:
        return type(value) is int and value >= 0

    def is_positive_int(value: object) -> bool:
        return type(value) is int and value > 0

    def is_native_qt_window_class(value: object) -> bool:
        if not isinstance(value, str) or not value.startswith("Qt"):
            return False
        return any(
            value.removeprefix("Qt").removesuffix(suffix).isdigit()
            for suffix in ("QWindowIcon", "QWindowToolSaveBits")
            if value.endswith(suffix)
        )

    def lifecycle_source_matches_event(event: dict[str, object]) -> bool:
        source = event.get("source")
        name = event.get("event")
        return (
            source == "winevent"
            and name in WIN_EVENT_NAMES.values()
        ) or (
            source == "cbt"
            and name in CBT_EVENT_NAMES.values()
        )

    raw_titlebar_events = report.get("titlebar_events")
    titlebar_events = (
        raw_titlebar_events
        if isinstance(raw_titlebar_events, list)
        else []
    )
    if not isinstance(raw_titlebar_events, list) or not all(
        isinstance(event, dict)
        and is_nonnegative_int(event.get("time_us"))
        and isinstance(event.get("stage"), str)
        and event.get("state") in {"present", "not_top_level"}
        and is_positive_int(event.get("hwnd"))
        and event.get("class") == "_q_titlebar"
        and isinstance(event.get("title"), str)
        and type(event.get("visible")) is bool
        and is_nonnegative_int(event.get("cloaked"))
        and is_nonnegative_int(event.get("owner"))
        and is_nonnegative_int(event.get("parent"))
        for event in titlebar_events
    ):
        violations.append("titlebar event evidence is invalid")
    if any(
        event["state"] == "present"
        and event["visible"]
        and event["cloaked"] == 0
        and event["parent"] == 0
        for event in titlebar_events
        if isinstance(event, dict)
    ):
        violations.append("visible top-level _q_titlebar was observed")

    if type(report.get("exit_code")) is not int or report.get("exit_code") != 0:
        violations.append("child exit code is not zero")
    if child.get("status") != "ok":
        violations.append("child status is not ok")
    if not isinstance(visible_events, list) or not visible_events:
        violations.append("visible window evidence is missing")
    callback_errors = report.get("callback_errors")
    if not isinstance(callback_errors, list) or not all(
        isinstance(error, str) for error in callback_errors
    ):
        violations.append("callback error evidence is missing")
    elif callback_errors:
        violations.append("callback errors were recorded")
    lookup_failures = report.get("window_lookup_failures")
    if not is_nonnegative_int(lookup_failures):
        violations.append("window ownership lookup evidence is missing")
    elif lookup_failures:
        violations.append("window ownership lookup failures were recorded")
    lookup_races = report.get("window_lookup_races")
    if not is_nonnegative_int(lookup_races):
        violations.append("window ownership race evidence is missing")

    raw_lifecycle_events = child.get("lifecycle_events")
    lifecycle_events = (
        raw_lifecycle_events
        if isinstance(raw_lifecycle_events, list)
        else []
    )
    if not lifecycle_events or not all(
        isinstance(event, dict)
        and is_positive_int(event.get("hwnd"))
        and is_nonnegative_int(event.get("time_us"))
        and isinstance(event.get("stage"), str)
        and isinstance(event.get("source"), str)
        and isinstance(event.get("event"), str)
        and lifecycle_source_matches_event(event)
        and isinstance(event.get("class"), str)
        and isinstance(event.get("title"), str)
        for event in lifecycle_events
    ):
        violations.append("native lifecycle evidence is missing")

    raw_object_events = child.get("qt_object_events")
    object_events = (
        raw_object_events if isinstance(raw_object_events, list) else []
    )
    if not object_events or not all(
        isinstance(event, dict)
        and is_nonnegative_int(event.get("time_us"))
        and isinstance(event.get("stage"), str)
        and isinstance(event.get("event"), str)
        and isinstance(event.get("class"), str)
        and isinstance(event.get("object_name"), str)
        and isinstance(event.get("title"), str)
        and is_nonnegative_int(event.get("hwnd"))
        and type(event.get("is_window")) is bool
        and type(event.get("visible")) is bool
        and isinstance(event.get("parent_class"), str)
        and isinstance(event.get("parent_object_name"), str)
        and isinstance(event.get("geometry"), list)
        for event in object_events
    ):
        violations.append("Qt object lifecycle evidence is missing")

    raw_qt_top_level_samples = child.get("qt_top_level_samples")
    qt_top_level_samples = (
        raw_qt_top_level_samples
        if isinstance(raw_qt_top_level_samples, list)
        else []
    )
    if not qt_top_level_samples:
        violations.append("Qt top-level evidence is missing")

    raw_stage_transitions = child.get("stage_transitions")
    stage_transitions = (
        raw_stage_transitions
        if isinstance(raw_stage_transitions, list)
        else []
    )
    typed_stage_transitions = (
        bool(stage_transitions)
        and all(
            isinstance(transition, dict)
            and isinstance(transition.get("stage"), str)
            and is_nonnegative_int(transition.get("time_us"))
            for transition in stage_transitions
        )
    )
    stage_names = (
        [str(transition["stage"]) for transition in stage_transitions]
        if typed_stage_transitions
        else []
    )
    valid_stage_transitions = (
        typed_stage_transitions
        and stage_names == list(EXPECTED_PROBE_STAGE_SEQUENCE)
        and all(
            int(previous["time_us"]) < int(current["time_us"])
            for previous, current in zip(
                stage_transitions,
                stage_transitions[1:],
            )
        )
    )
    if not valid_stage_transitions:
        violations.append("stage transition evidence is invalid")

    stage_intervals = {
        str(transition["stage"]): (
            int(transition["time_us"]),
            (
                int(stage_transitions[index + 1]["time_us"])
                if index + 1 < len(stage_transitions)
                else None
            ),
        )
        for index, transition in enumerate(stage_transitions)
        if valid_stage_transitions
    }

    def evidence_time_is_in_stage(stage: object, time_us: object) -> bool:
        if not isinstance(stage, str) or not is_nonnegative_int(time_us):
            return False
        interval = stage_intervals.get(stage)
        if interval is None:
            return False
        start, end = interval
        return int(time_us) >= start and (
            end is None or int(time_us) < end
        )

    if valid_stage_transitions and any(
        not evidence_time_is_in_stage(
            event.get("stage"),
            event.get("time_us"),
        )
        for event in lifecycle_events + object_events + qt_top_level_samples
        if isinstance(event, dict)
    ):
        violations.append("lifecycle stage timing evidence is invalid")

    expected_stages = EXPECTED_PROBE_STAGES
    completed_stages = child.get("completed_stages")
    if completed_stages != list(expected_stages):
        violations.append("child stages are incomplete")

    observed_stages = {
        str(event.get("stage", ""))
        for event in events
        if isinstance(event, dict)
        and event.get("state") == "present"
        and event.get("visible")
        and not event.get("cloaked")
    }
    for expected in EXPECTED_DIALOG_TITLES_BY_STAGE:
        if expected not in observed_stages:
            violations.append(f"stage was not observed: {expected}")

    raw_window_samples = report.get("window_samples")
    window_samples = (
        raw_window_samples if isinstance(raw_window_samples, list) else []
    )
    if not window_samples:
        violations.append("window sample evidence is missing")

    def is_main_window(window: object) -> bool:
        if not isinstance(window, dict):
            return False
        title = str(window.get("title", ""))
        return (
            window.get("owner") == 0
            and window.get("parent") == 0
            and is_native_qt_window_class(window.get("class"))
            and (
                title == "Spectrum Organizer"
                or title.startswith("ROUND10|")
            )
        )

    def window_signature(window: object) -> tuple[object, ...] | None:
        if not isinstance(window, dict):
            return None
        hwnd = window.get("hwnd")
        owner = window.get("owner")
        parent = window.get("parent")
        style = window.get("style")
        visible = window.get("visible")
        cloaked = window.get("cloaked")
        if (
            not is_positive_int(hwnd)
            or not is_nonnegative_int(owner)
            or not is_nonnegative_int(parent)
            or not is_nonnegative_int(style)
            or not is_nonnegative_int(window.get("exstyle"))
            or type(visible) is not bool
            or not visible
            or not is_nonnegative_int(cloaked)
            or cloaked != 0
            or not int(style) & 0x10000000
            or not isinstance(window.get("class"), str)
            or not isinstance(window.get("title"), str)
        ):
            return None
        return (
            hwnd,
            window.get("class"),
            window.get("title"),
            owner,
            parent,
            style,
            window.get("exstyle"),
            visible,
            cloaked,
        )

    invalid_window_samples = False
    invalid_window_sample_timing = False
    for sample in window_samples:
        if (
            not isinstance(sample, dict)
            or not is_nonnegative_int(sample.get("time_us"))
            or not isinstance(sample.get("stage"), str)
            or not isinstance(sample.get("windows"), list)
        ):
            invalid_window_samples = True
            continue
        if not evidence_time_is_in_stage(
            sample["stage"],
            sample["time_us"],
        ):
            invalid_window_sample_timing = True
        signatures = [
            window_signature(window)
            for window in sample["windows"]
        ]
        handles = [
            signature[0]
            for signature in signatures
            if signature is not None
        ]
        if (
            any(signature is None for signature in signatures)
            or len(handles) != len(set(handles))
        ):
            invalid_window_samples = True
    if invalid_window_samples:
        violations.append("window sample evidence is invalid")
    if invalid_window_sample_timing:
        violations.append("window sample stage timing evidence is invalid")

    if any(
        not any(
            isinstance(event, dict)
            and event.get("state") == "present"
            and event.get("visible")
            and not event.get("cloaked")
            and event.get("stage") == sample.get("stage")
            and event.get("time_us") == sample.get("time_us")
            and window_signature(event) == window_signature(window)
            for event in events
        )
        for sample in window_samples
        if isinstance(sample, dict)
        and isinstance(sample.get("windows"), list)
        for window in sample["windows"]
    ):
        violations.append("window sample evidence is unbound")

    sample_stage_time_signatures = {
        (
            str(sample.get("stage", "")),
            sample.get("time_us"),
            window_signature(window),
        )
        for sample in window_samples
        if isinstance(sample, dict)
        and isinstance(sample.get("windows"), list)
        for window in sample["windows"]
        if window_signature(window) is not None
    }
    if any(
        (
            str(event.get("stage", "")),
            event.get("time_us"),
            window_signature(event),
        )
        not in sample_stage_time_signatures
        for event in events
        if isinstance(event, dict)
        and event.get("state") == "present"
        and event.get("visible")
        and not event.get("cloaked")
    ):
        violations.append("visible window evidence is unbound")

    expected_dialog_titles = EXPECTED_DIALOG_TITLES_BY_STAGE
    allowed_dialog_titles_by_stage = ALLOWED_DIALOG_TITLES_BY_STAGE
    allowed_dialog_titles = {
        title
        for titles in allowed_dialog_titles_by_stage.values()
        for title in titles
    }

    def stage_matches(stage: str, expected: str) -> bool:
        return stage == expected or stage.startswith(f"{expected}:")

    def expected_stage_for(stage: str) -> str | None:
        return next(
            (
                expected
                for expected in expected_stages
                if stage_matches(stage, expected)
            ),
            None,
        )

    def main_title_matches_stage(stage: str, title: str) -> bool:
        return title == "Spectrum Organizer" or title == f"ROUND10|{stage}"

    def dialog_title_matches_stage(stage: str, title: str) -> bool:
        return title in allowed_dialog_titles_by_stage.get(stage, set())

    invalid_taskbar_sample = False
    for sample in window_samples:
        if not isinstance(sample, dict):
            continue
        stage = str(sample.get("stage", ""))
        expected_titles = allowed_dialog_titles_by_stage.get(stage)
        if expected_titles is None:
            continue
        windows = sample.get("windows")
        if not isinstance(windows, list):
            continue
        main_windows = [window for window in windows if is_main_window(window)]
        dialog_windows = [
            window for window in windows if not is_main_window(window)
        ]
        if not dialog_windows:
            continue
        if len(main_windows) != 1:
            invalid_taskbar_sample = True
            continue
        main_hwnd = main_windows[0].get("hwnd")
        for window in dialog_windows:
            if (
                not isinstance(window, dict)
                or not is_native_qt_window_class(window.get("class"))
                or str(window.get("title", "")) not in expected_titles
                or window.get("owner") != main_hwnd
                or window.get("parent") != main_hwnd
                or window.get("hwnd") == main_hwnd
                or not is_nonnegative_int(window.get("style"))
                or not is_nonnegative_int(window.get("exstyle"))
                or bool(int(window.get("style", 0)) & 0x40000000)
                or bool(int(window.get("exstyle", 0)) & 0x80)
                or not bool(int(window.get("exstyle", 0)) & 0x40000)
                or not bool(int(window.get("exstyle", 0)) & 0x8)
            ):
                invalid_taskbar_sample = True

    if any(
        isinstance(event, dict)
        and event.get("state") == "present"
        and event.get("visible")
        and not event.get("cloaked")
        and event.get("owner") == 0
        and event.get("parent") == 0
        and main_title_matches_stage(
            str(event.get("stage", "")),
            str(event.get("title", "")),
        )
        and not is_native_qt_window_class(event.get("class"))
        for event in events
    ):
        violations.append("native main window class is invalid")

    main_hwnds = {
        event.get("hwnd")
        for event in events
        if isinstance(event, dict) and is_main_window(event)
    }
    if len(main_hwnds) > 1:
        violations.append("multiple production main HWNDs were observed")

    def is_concrete_production_main(
        record: object,
        stage: object,
    ) -> bool:
        return (
            isinstance(record, dict)
            and record.get("hwnd") in main_hwnds
            and is_native_qt_window_class(record.get("class"))
            and main_title_matches_stage(
                str(stage),
                str(record.get("title", "")),
            )
        )

    if any(
        is_concrete_production_main(event, event.get("stage", ""))
        and (event.get("owner") != 0 or event.get("parent") != 0)
        for event in events
        if isinstance(event, dict)
    ) or any(
        is_concrete_production_main(window, sample.get("stage", ""))
        and (window.get("owner") != 0 or window.get("parent") != 0)
        for sample in window_samples
        if isinstance(sample, dict)
        and isinstance(sample.get("windows"), list)
        for window in sample["windows"]
    ):
        violations.append("production main ownership evidence is invalid")

    for event in events:
        if (
            not isinstance(event, dict)
            or event.get("state") != "present"
            or not event.get("visible")
            or event.get("cloaked")
            or str(event.get("title", "")) not in allowed_dialog_titles
        ):
            continue
        stage = str(event.get("stage", ""))
        expected_titles = allowed_dialog_titles_by_stage.get(stage)
        exstyle = event.get("exstyle")
        if (
            expected_titles is None
            or str(event.get("title", "")) not in expected_titles
            or not is_native_qt_window_class(event.get("class"))
            or event.get("owner") not in main_hwnds
            or event.get("parent") != event.get("owner")
            or not is_nonnegative_int(event.get("style"))
            or not is_nonnegative_int(exstyle)
            or bool(int(event.get("style", 0)) & 0x40000000)
            or bool(int(exstyle or 0) & 0x80)
            or not bool(int(exstyle or 0) & 0x40000)
            or not bool(int(exstyle or 0) & 0x8)
        ):
            invalid_taskbar_sample = True
    if invalid_taskbar_sample:
        violations.append("manual dialog taskbar evidence is invalid")

    def event_matches(
        *,
        stage: str,
        signature: tuple[object, ...] | None,
        time_us: object | None = None,
    ) -> bool:
        return signature is not None and any(
            isinstance(event, dict)
            and event.get("state") == "present"
            and event.get("visible")
            and not event.get("cloaked")
            and str(event.get("stage", "")) == stage
            and is_nonnegative_int(event.get("time_us"))
            and (time_us is None or event.get("time_us") == time_us)
            and window_signature(event) == signature
            for event in events
        )

    def sample_bound_main_hwnd(
        sample: object,
        expected_stage: str,
    ) -> object | None:
        if not isinstance(sample, dict):
            return None
        stage = str(sample.get("stage", ""))
        if stage != expected_stage:
            return None
        sample_time = sample.get("time_us")
        if not is_nonnegative_int(sample_time):
            return None
        windows = sample.get("windows")
        if not isinstance(windows, list):
            return None
        main_windows = [window for window in windows if is_main_window(window)]
        if len(main_windows) != 1:
            return None
        main = main_windows[0]
        if str(main.get("title", "")) != f"ROUND10|{stage}":
            return None
        if not event_matches(
            stage=stage,
            signature=window_signature(main),
            time_us=sample_time,
        ):
            return None
        main_hwnd = main.get("hwnd")
        has_dialog = any(
            isinstance(window, dict)
            and not is_main_window(window)
            and is_native_qt_window_class(window.get("class"))
            and str(window.get("title", ""))
            == expected_dialog_titles[expected_stage]
            and window.get("owner") == main_hwnd
            and window.get("parent") == main_hwnd
            and window.get("hwnd") != main_hwnd
            and is_nonnegative_int(window.get("exstyle"))
            and not int(window["exstyle"]) & 0x80
            and bool(int(window["exstyle"]) & 0x40000)
            and bool(int(window["exstyle"]) & 0x8)
            and event_matches(
                stage=stage,
                signature=window_signature(window),
                time_us=sample_time,
            )
            for window in windows
        )
        return main_hwnd if has_dialog else None

    observed_main_hwnds: set[object] = set()
    for expected in expected_dialog_titles:
        stage_main_hwnds: set[object] = set()
        for sample in window_samples:
            main_hwnd = sample_bound_main_hwnd(sample, expected)
            if main_hwnd is not None:
                stage_main_hwnds.add(main_hwnd)
        if not stage_main_hwnds:
            violations.append(
                f"stage dialog was not observed: {expected}"
            )
        observed_main_hwnds.update(stage_main_hwnds)

    if len(observed_main_hwnds) > 1:
        violations.append("main window changed across stages")

    lifecycle_stage_bound = True
    qt_object_stage_bound = True
    for expected, expected_show_title in (
        EXPECTED_DIALOG_SHOW_TITLES_BY_STAGE.items()
    ):
        dialog_samples: list[tuple[str, int, str, str]] = []
        for sample in window_samples:
            if not isinstance(sample, dict):
                continue
            stage = str(sample.get("stage", ""))
            sample_time = sample.get("time_us")
            windows = sample.get("windows")
            if (
                stage != expected
                or not is_nonnegative_int(sample_time)
                or not isinstance(windows, list)
            ):
                continue
            main_windows = [
                window for window in windows if is_main_window(window)
            ]
            if len(main_windows) != 1:
                continue
            main_hwnd = main_windows[0].get("hwnd")
            for window in windows:
                if (
                    isinstance(window, dict)
                    and window.get("owner") == main_hwnd
                    and window.get("parent") == main_hwnd
                    and is_positive_int(window.get("hwnd"))
                    and str(window.get("title", "")) == expected_show_title
                    and is_native_qt_window_class(window.get("class"))
                ):
                    dialog_samples.append(
                        (
                            stage,
                            int(window["hwnd"]),
                            str(window["title"]),
                            str(window["class"]),
                        )
                    )
        if not dialog_samples or not all(
            any(
                isinstance(event, dict)
                and event.get("source") == "winevent"
                and str(event.get("event", "")).casefold() == "show"
                and event.get("stage") == stage
                and event.get("hwnd") == hwnd
                and event.get("title") == title
                and event.get("class") == native_class
                and evidence_time_is_in_stage(
                    event.get("stage"),
                    event.get("time_us"),
                )
                for event in lifecycle_events
            )
            and any(
                isinstance(event, dict)
                and event.get("event") == "Show"
                and event.get("is_window")
                and event.get("visible")
                and event.get("class") == "QDialog"
                and event.get("object_name") == "organizer_dialog"
                and event.get("stage") == stage
                and event.get("hwnd") == hwnd
                and event.get("title") == title
                and evidence_time_is_in_stage(
                    event.get("stage"),
                    event.get("time_us"),
                )
                for event in object_events
            )
            for stage, hwnd, title, native_class in dialog_samples
        ):
            lifecycle_stage_bound = False
            qt_object_stage_bound = False
    dialog_observations: dict[
        int,
        list[tuple[int, str, str, str]],
    ] = {}
    for sample in window_samples:
        if not isinstance(sample, dict):
            continue
        stage = str(sample.get("stage", ""))
        expected_titles = allowed_dialog_titles_by_stage.get(stage)
        sample_time = sample.get("time_us")
        windows = sample.get("windows")
        if (
            expected_titles is None
            or not is_nonnegative_int(sample_time)
            or not isinstance(windows, list)
        ):
            continue
        main_windows = [window for window in windows if is_main_window(window)]
        if len(main_windows) != 1:
            continue
        main_hwnd = main_windows[0].get("hwnd")
        for window in windows:
            if (
                isinstance(window, dict)
                and is_positive_int(window.get("hwnd"))
                and window.get("owner") == main_hwnd
                and window.get("parent") == main_hwnd
                and str(window.get("title", "")) in expected_titles
            ):
                dialog_observations.setdefault(
                    int(window["hwnd"]),
                    [],
                ).append(
                    (
                        int(sample_time),
                        str(window["title"]),
                        stage,
                        str(window.get("class", "")),
                    )
                )

    for hwnd, observations in dialog_observations.items():
        first_time, first_title, first_stage, first_class = min(observations)
        native_birth_times = [
            int(event["time_us"])
            for event in lifecycle_events
            if (
                isinstance(event, dict)
                and event.get("source") == "winevent"
                and str(event.get("event", "")).casefold() == "show"
                and event.get("stage") == first_stage
                and event.get("hwnd") == hwnd
                and event.get("title") == first_title
                and event.get("class") == first_class
                and is_nonnegative_int(event.get("time_us"))
                and evidence_time_is_in_stage(
                    event.get("stage"),
                    event.get("time_us"),
                )
            )
        ]
        qt_birth_times = [
            int(event["time_us"])
            for event in object_events
            if (
                isinstance(event, dict)
                and event.get("event") == "Show"
                and event.get("is_window")
                and event.get("visible")
                and event.get("class") == "QDialog"
                and event.get("object_name") == "organizer_dialog"
                and event.get("stage") == first_stage
                and event.get("hwnd") == hwnd
                and event.get("title") == first_title
                and is_nonnegative_int(event.get("time_us"))
                and evidence_time_is_in_stage(
                    event.get("stage"),
                    event.get("time_us"),
                )
            )
        ]
        if (
            not native_birth_times
            or not qt_birth_times
            or min(qt_birth_times) > first_time
        ):
            lifecycle_stage_bound = False
            qt_object_stage_bound = False
    if not lifecycle_stage_bound:
        violations.append("native lifecycle evidence is unbound")
    if not qt_object_stage_bound:
        violations.append("Qt object lifecycle evidence is unbound")

    dialog_hwnd_reused_without_teardown = False
    stage_positions = {
        stage: index
        for index, stage in enumerate(EXPECTED_PROBE_STAGE_SEQUENCE)
    }

    def native_dialog_teardown_matches(
        event: object,
        hwnd: int,
        titles: set[str],
        observation_start_time: int,
    ) -> bool:
        if not isinstance(event, dict):
            return False
        event_name = str(event.get("event", "")).casefold()
        event_time = event.get("time_us")
        if not is_nonnegative_int(event_time):
            return False
        if event.get("hwnd") != hwnd:
            return False
        if (
            event.get("source") == "cbt"
            and event_name == "destroy"
            and event.get("class") == ""
            and event.get("title") == ""
        ):
            return True
        event_class = str(event.get("class", ""))
        event_title = str(event.get("title", ""))
        return (
            event_name in {"hide", "destroy"}
            and event_title in titles
            and any(
                isinstance(observation, dict)
                and observation.get("hwnd") == hwnd
                and observation.get("class") == event_class
                and observation.get("title") == event_title
                and is_nonnegative_int(observation.get("time_us"))
                and observation_start_time
                <= int(observation["time_us"])
                <= int(event_time)
                for observation in events
            )
        )

    def qt_teardown_times_between(
        hwnd: int,
        title: str,
        start_time: int,
        end_time: int,
        *,
        widget_class: str,
        object_name: str,
        allowed_expected_stages: set[str | None] | None = None,
    ) -> list[int]:
        def identity_matches(sample: object) -> bool:
            return isinstance(sample, dict) and (
                (
                    sample.get("kind") == "widget"
                    and sample.get("class") == widget_class
                    and sample.get("object_name") == object_name
                )
                or (
                    sample.get("kind") == "window"
                    and sample.get("class") == "QWidgetWindow"
                    and sample.get("object_name") == f"{object_name}Window"
                )
            )

        sample_times = [
            int(sample["time_us"])
            for sample in qt_top_level_samples
            if identity_matches(sample)
            and sample.get("hwnd") == hwnd
            and sample.get("state") == "not_visible"
            and sample.get("title") == title
            and (
                allowed_expected_stages is None
                or expected_stage_for(str(sample.get("stage", "")))
                in allowed_expected_stages
            )
            and is_nonnegative_int(sample.get("time_us"))
            and start_time <= int(sample["time_us"]) <= end_time
        ]
        object_hide_times = [
            int(event["time_us"])
            for event in object_events
            if isinstance(event, dict)
            and event.get("event") == "Hide"
            and event.get("is_window")
            and not event.get("visible")
            and event.get("class") == widget_class
            and event.get("object_name") == object_name
            and event.get("hwnd") == hwnd
            and event.get("title") == title
            and (
                allowed_expected_stages is None
                or expected_stage_for(str(event.get("stage", "")))
                in allowed_expected_stages
            )
            and is_nonnegative_int(event.get("time_us"))
            and start_time <= int(event["time_us"]) <= end_time
            and any(
                identity_matches(sample)
                and sample.get("hwnd") == hwnd
                and sample.get("state") == "visible"
                and is_nonnegative_int(sample.get("time_us"))
                and int(event["time_us"])
                < int(sample["time_us"])
                <= end_time
                for sample in qt_top_level_samples
            )
        ]
        return sample_times + object_hide_times

    def visibility_transition_between(
        hwnd: int,
        previous_time: int,
        current_time: int,
        previous_title: str,
        current_title: str,
    ) -> tuple[bool, bool]:
        native_teardown_events = [
            event
            for event in lifecycle_events
            if native_dialog_teardown_matches(
                event,
                hwnd,
                {previous_title},
                previous_time,
            )
            and previous_time <= int(event["time_us"]) <= current_time
        ]
        native_teardown_times = [
            int(event["time_us"]) for event in native_teardown_events
        ]
        qt_teardown_times = qt_teardown_times_between(
            hwnd,
            previous_title,
            previous_time,
            current_time,
            widget_class="QDialog",
            object_name="organizer_dialog",
        )
        native_show_times = [
            int(event["time_us"])
            for event in lifecycle_events
            if isinstance(event, dict)
            and event.get("hwnd") == hwnd
            and str(event.get("event", "")).casefold() == "show"
            and str(event.get("class", "")).startswith("Qt")
            and event.get("title") == current_title
            and is_nonnegative_int(event.get("time_us"))
            and previous_time <= int(event["time_us"]) <= current_time
        ]
        qt_show_times = [
            int(event["time_us"])
            for event in object_events
            if isinstance(event, dict)
            and event.get("hwnd") == hwnd
            and event.get("event") == "Show"
            and event.get("is_window")
            and event.get("visible")
            and event.get("class") == "QDialog"
            and event.get("object_name") == "organizer_dialog"
            and event.get("title") == current_title
            and is_nonnegative_int(event.get("time_us"))
            and previous_time <= int(event["time_us"]) <= current_time
        ]
        boundary_times = native_teardown_times + qt_teardown_times
        has_boundary = bool(boundary_times)
        latest_teardown = max(boundary_times, default=None)
        native_reappeared = bool(
            latest_teardown is not None
            and native_show_times
            and max(native_show_times) > latest_teardown
        )
        qt_reappeared = bool(
            latest_teardown is not None
            and qt_show_times
            and max(qt_show_times) > latest_teardown
        )
        missing_reappearance_show = (
            bool(boundary_times)
            and (
                not native_teardown_times
                or not qt_teardown_times
                or not native_reappeared
                or not qt_reappeared
            )
        )
        return has_boundary, missing_reappearance_show

    dialog_visible_after_teardown_without_show = False
    for hwnd, observations in dialog_observations.items():
        epochs: list[list[tuple[int, str, str, str]]] = []
        for observation in sorted(observations):
            current_time, title, stage, _native_class = observation
            if not epochs:
                epochs.append([observation])
                continue
            (
                previous_time,
                previous_title,
                previous_stage,
                _previous_native_class,
            ) = epochs[-1][-1]
            stage_is_continuous = (
                title == previous_title
                and (
                    stage == previous_stage
                    or stage_positions.get(stage)
                    == stage_positions.get(previous_stage, -2) + 1
                )
            )
            (
                visibility_boundary,
                missing_reappearance_show,
            ) = visibility_transition_between(
                hwnd,
                previous_time,
                current_time,
                previous_title,
                title,
            )
            if missing_reappearance_show:
                dialog_visible_after_teardown_without_show = True
            if stage_is_continuous and not visibility_boundary:
                epochs[-1].append(observation)
            else:
                epochs.append([observation])
        for previous_observations, current_observations in zip(
            epochs,
            epochs[1:],
        ):
            previous_last = max(
                observation[0] for observation in previous_observations
            )
            current_first = min(
                observation[0] for observation in current_observations
            )
            previous_titles = {
                observation[1] for observation in previous_observations
            }
            previous_expected_stages = {
                expected_stage_for(observation[2])
                for observation in previous_observations
            }
            native_teardown = any(
                native_dialog_teardown_matches(
                    event,
                    hwnd,
                    previous_titles,
                    previous_last,
                )
                and expected_stage_for(str(event.get("stage", "")))
                in previous_expected_stages
                and previous_last <= int(event["time_us"]) <= current_first
                for event in lifecycle_events
            )
            qt_teardown = any(
                qt_teardown_times_between(
                    hwnd,
                    title,
                    previous_last,
                    current_first,
                    widget_class="QDialog",
                    object_name="organizer_dialog",
                    allowed_expected_stages=previous_expected_stages,
                )
                for title in previous_titles
            )
            if not native_teardown or not qt_teardown:
                dialog_hwnd_reused_without_teardown = True
    if dialog_hwnd_reused_without_teardown:
        violations.append(
            "dialog HWND was reused across stages without teardown"
        )
    if dialog_visible_after_teardown_without_show:
        violations.append(
            "dialog became visible after teardown without SHOW"
        )

    main_observations: dict[
        int,
        list[tuple[int, str, str, str]],
    ] = {}
    for sample in window_samples:
        if (
            not isinstance(sample, dict)
            or not is_nonnegative_int(sample.get("time_us"))
            or not isinstance(sample.get("windows"), list)
        ):
            continue
        for window in sample["windows"]:
            if is_main_window(window):
                main_observations.setdefault(
                    int(window["hwnd"]),
                    [],
                ).append(
                    (
                        int(sample["time_us"]),
                        str(sample.get("stage", "")),
                        str(window["title"]),
                        str(window["class"]),
                    )
                )

    main_visible_after_teardown_without_show = False
    native_main_birth_bound = True
    qt_main_birth_bound = True
    for hwnd, observations in main_observations.items():
        ordered_observations = sorted(observations)
        first_time, _first_stage, _first_title, first_class = (
            ordered_observations[0]
        )
        if not any(
            isinstance(event, dict)
            and event.get("source") == "winevent"
            and str(event.get("event", "")).casefold() == "show"
            and event.get("hwnd") == hwnd
            and event.get("class") == first_class
            and main_title_matches_stage(
                str(event.get("stage", "")),
                str(event.get("title", "")),
            )
            and is_nonnegative_int(event.get("time_us"))
            and int(event["time_us"]) <= first_time
            and evidence_time_is_in_stage(
                event.get("stage"),
                event.get("time_us"),
            )
            for event in lifecycle_events
        ):
            native_main_birth_bound = False
        if not any(
            isinstance(event, dict)
            and event.get("event") == "Show"
            and event.get("is_window")
            and event.get("visible")
            and event.get("class") == "QMainWindow"
            and event.get("object_name") == "production_main_window"
            and event.get("hwnd") == hwnd
            and main_title_matches_stage(
                str(event.get("stage", "")),
                str(event.get("title", "")),
            )
            and is_nonnegative_int(event.get("time_us"))
            and int(event["time_us"]) <= first_time
            and evidence_time_is_in_stage(
                event.get("stage"),
                event.get("time_us"),
            )
            for event in object_events
        ):
            qt_main_birth_bound = False
        for previous, current in zip(
            ordered_observations,
            ordered_observations[1:],
        ):
            previous_time, _previous_stage, previous_title, _ = previous
            current_time, _current_stage, current_title, current_class = (
                current
            )
            native_teardown_times = [
                int(event["time_us"])
                for event in lifecycle_events
                if native_dialog_teardown_matches(
                    event,
                    hwnd,
                    {previous_title},
                    previous_time,
                )
                and previous_time
                <= int(event["time_us"])
                <= current_time
            ]
            qt_teardown_times = qt_teardown_times_between(
                hwnd,
                previous_title,
                previous_time,
                current_time,
                widget_class="QMainWindow",
                object_name="production_main_window",
            )
            boundary_times = native_teardown_times + qt_teardown_times
            if not boundary_times:
                continue
            latest_teardown = max(boundary_times)
            native_reappeared = any(
                isinstance(event, dict)
                and event.get("source") == "winevent"
                and str(event.get("event", "")).casefold() == "show"
                and event.get("hwnd") == hwnd
                and event.get("class") == current_class
                and event.get("title") == current_title
                and is_nonnegative_int(event.get("time_us"))
                and latest_teardown
                < int(event["time_us"])
                <= current_time
                for event in lifecycle_events
            )
            qt_reappeared = any(
                isinstance(event, dict)
                and event.get("event") == "Show"
                and event.get("is_window")
                and event.get("visible")
                and event.get("class") == "QMainWindow"
                and event.get("object_name") == "production_main_window"
                and event.get("hwnd") == hwnd
                and event.get("title") == current_title
                and is_nonnegative_int(event.get("time_us"))
                and latest_teardown
                < int(event["time_us"])
                <= current_time
                for event in object_events
            )
            if (
                not native_teardown_times
                or not qt_teardown_times
                or not native_reappeared
                or not qt_reappeared
            ):
                main_visible_after_teardown_without_show = True
    if not native_main_birth_bound:
        violations.append("native main lifecycle birth evidence is unbound")
    if not qt_main_birth_bound:
        violations.append("Qt main lifecycle birth evidence is unbound")
    if main_visible_after_teardown_without_show:
        violations.append(
            "main window became visible after teardown without SHOW"
        )

    if any(
        isinstance(sample, dict)
        and isinstance(sample.get("windows"), list)
        and len(sample["windows"]) > 2
        for sample in window_samples
    ):
        violations.append(
            "more than two visible target windows were observed"
        )

    expected_main_window = any(
        isinstance(event, dict)
        and event.get("state") == "present"
        and event.get("visible")
        and not event.get("cloaked")
        and event.get("owner") == 0
        and event.get("parent") == 0
        and (
            event.get("title") == "Spectrum Organizer"
            or str(event.get("title", "")).startswith("ROUND10|")
        )
        for event in events
    )
    if not expected_main_window:
        violations.append("no trustworthy main-window observation")

    unexpected_ownerless = [
        event
        for event in events
        if isinstance(event, dict)
        and event.get("state") == "present"
        and str(event.get("class", "")).startswith("Qt")
        and event.get("visible")
        and not event.get("cloaked")
        and event.get("owner") == 0
        and event.get("parent") == 0
        and event.get("title") != "Spectrum Organizer"
        and not str(event.get("title", "")).startswith("ROUND10|")
    ]
    if unexpected_ownerless:
        violations.append("unexpected ownerless Qt window was observed")

    if any(
        not isinstance(event, dict)
        or event.get("state") != "present"
        or not is_nonnegative_int(event.get("time_us"))
        or not isinstance(event.get("stage"), str)
        or window_signature(event) is None
        for event in events
    ):
        violations.append("visible window evidence is invalid")
    if any(
        isinstance(event, dict)
        and isinstance(event.get("stage"), str)
        and is_nonnegative_int(event.get("time_us"))
        and not evidence_time_is_in_stage(
            event["stage"],
            event["time_us"],
        )
        for event in events
    ):
        violations.append("visible window stage timing evidence is invalid")

    def is_expected_qt_top_level(sample: object) -> bool:
        if (
            not isinstance(sample, dict)
            or sample.get("state") not in {"visible", "not_visible"}
            or sample.get("kind") not in {"widget", "window"}
            or not is_positive_int(sample.get("hwnd"))
            or not is_nonnegative_int(sample.get("time_us"))
            or not isinstance(sample.get("stage"), str)
            or not isinstance(sample.get("class"), str)
            or not isinstance(sample.get("object_name"), str)
            or not isinstance(sample.get("title"), str)
        ):
            return False
        title = str(sample["title"])
        stage = str(sample["stage"])
        identity = (
            sample["kind"],
            sample["class"],
            sample["object_name"],
        )
        if identity in {
            (
                "widget",
                "QMainWindow",
                "production_main_window",
            ),
            (
                "window",
                "QWidgetWindow",
                "production_main_windowWindow",
            ),
        }:
            return main_title_matches_stage(stage, title)
        if identity in {
            ("widget", "QDialog", "organizer_dialog"),
            (
                "window",
                "QWidgetWindow",
                "organizer_dialogWindow",
            ),
        }:
            return dialog_title_matches_stage(stage, title)
        return False

    if qt_top_level_samples and any(
        not is_expected_qt_top_level(sample)
        for sample in qt_top_level_samples
    ):
        violations.append("unexpected Qt top-level window was observed")

    def is_qt_production_main(sample: object) -> bool:
        return isinstance(sample, dict) and (
            (
                sample.get("kind") == "widget"
                and sample.get("class") == "QMainWindow"
                and sample.get("object_name") == "production_main_window"
            )
            or (
                sample.get("kind") == "window"
                and sample.get("class") == "QWidgetWindow"
                and sample.get("object_name")
                == "production_main_windowWindow"
            )
        )

    if any(
        is_qt_production_main(sample)
        and sample.get("hwnd") not in main_hwnds
        for sample in qt_top_level_samples
    ) or any(
        isinstance(event, dict)
        and event.get("event") == "Show"
        and event.get("is_window")
        and event.get("visible")
        and event.get("class") == "QMainWindow"
        and event.get("object_name") == "production_main_window"
        and is_positive_int(event.get("hwnd"))
        and event.get("hwnd") not in main_hwnds
        for event in object_events
    ):
        violations.append("Qt production main HWND evidence is invalid")

    def qt_object_evidence_is_in_episode(
        show_event: dict[str, object],
        evidence_time: object,
    ) -> bool:
        if not is_nonnegative_int(evidence_time):
            return False
        show_time = int(show_event["time_us"])
        teardown_times = [
            int(event["time_us"])
            for event in object_events
            if isinstance(event, dict)
            and event.get("hwnd") == show_event.get("hwnd")
            and event.get("event") == "Hide"
            and is_nonnegative_int(event.get("time_us"))
        ]
        if show_time in teardown_times:
            return False
        next_teardown = min(
            (time_us for time_us in teardown_times if time_us > show_time),
            default=None,
        )
        return int(evidence_time) >= show_time and (
            next_teardown is None
            or int(evidence_time) < next_teardown
        )

    def is_expected_qt_object_show(event: object) -> bool:
        if not isinstance(event, dict):
            return False
        identity = (
            event.get("class"),
            event.get("object_name"),
        )
        title = str(event.get("title", ""))
        stage = str(event.get("stage", ""))
        if identity == (
            "QMainWindow",
            "production_main_window",
        ):
            return main_title_matches_stage(stage, title)
        if identity == ("QDialog", "organizer_dialog"):
            return dialog_title_matches_stage(stage, title)
        return False

    if any(
        not is_expected_qt_object_show(event)
        or not any(
            isinstance(sample, dict)
            and sample.get("state") == "visible"
            and sample.get("hwnd") == event.get("hwnd")
            and sample.get("stage") == event.get("stage")
            and sample.get("title") == event.get("title")
            and evidence_time_is_in_stage(
                sample.get("stage"),
                sample.get("time_us"),
            )
            and qt_object_evidence_is_in_episode(
                event,
                sample.get("time_us"),
            )
            for sample in qt_top_level_samples
        )
        or not any(
            isinstance(native_event, dict)
            and native_event.get("state") == "present"
            and native_event.get("visible")
            and not native_event.get("cloaked")
            and native_event.get("hwnd") == event.get("hwnd")
            and native_event.get("stage") == event.get("stage")
            and native_event.get("title") == event.get("title")
            and evidence_time_is_in_stage(
                native_event.get("stage"),
                native_event.get("time_us"),
            )
            and qt_object_evidence_is_in_episode(
                event,
                native_event.get("time_us"),
            )
            for native_event in events
        )
        or not evidence_time_is_in_stage(
            event.get("stage"),
            event.get("time_us"),
        )
        for event in object_events
        if isinstance(event, dict)
        and event.get("event") == "Show"
        and event.get("is_window")
        and event.get("visible")
    ):
        violations.append("Qt object lifecycle evidence is unbound")

    native_visible_hwnds = {
        int(event["hwnd"])
        for event in events
        if isinstance(event, dict)
        and event.get("state") == "present"
        and event.get("visible")
        and not event.get("cloaked")
        and is_positive_int(event.get("hwnd"))
    }
    qt_visible_hwnds = {
        int(sample["hwnd"])
        for sample in qt_top_level_samples
        if isinstance(sample, dict)
        and sample.get("state") == "visible"
        and is_positive_int(sample.get("hwnd"))
    }
    if (
        native_visible_hwnds
        and qt_visible_hwnds
        and native_visible_hwnds != qt_visible_hwnds
    ):
        violations.append("Qt/native visible-window evidence disagrees")

    native_stage_windows = {
        (str(event["stage"]), int(event["hwnd"]))
        for event in events
        if isinstance(event, dict)
        and event.get("state") == "present"
        and event.get("visible")
        and not event.get("cloaked")
        and is_positive_int(event.get("hwnd"))
        and expected_stage_for(str(event.get("stage", ""))) is not None
    }
    qt_stage_windows = {
        (str(sample["stage"]), int(sample["hwnd"]))
        for sample in qt_top_level_samples
        if isinstance(sample, dict)
        and sample.get("state") == "visible"
        and is_positive_int(sample.get("hwnd"))
        and expected_stage_for(str(sample.get("stage", ""))) is not None
    }
    qt_stage_time_is_bound = all(
        any(
            isinstance(event, dict)
            and event.get("state") == "present"
            and event.get("visible")
            and not event.get("cloaked")
            and event.get("stage") == sample.get("stage")
            and event.get("hwnd") == sample.get("hwnd")
            and evidence_time_is_in_stage(
                event.get("stage"),
                event.get("time_us"),
            )
            for event in events
        )
        and evidence_time_is_in_stage(
            sample.get("stage"),
            sample.get("time_us"),
        )
        for sample in qt_top_level_samples
        if isinstance(sample, dict)
        and sample.get("state") == "visible"
        and is_positive_int(sample.get("hwnd"))
        and is_nonnegative_int(sample.get("time_us"))
        and expected_stage_for(str(sample.get("stage", ""))) is not None
    )
    if (
        native_stage_windows
        and qt_stage_windows
        and (
            native_stage_windows != qt_stage_windows
            or not qt_stage_time_is_bound
        )
    ):
        violations.append("Qt/native stage-window evidence disagrees")

    def lifecycle_show_is_bound(
        event_index: int,
        event: object,
    ) -> bool:
        if not isinstance(event, dict):
            return False
        if (
            event.get("source") != "winevent"
            or not is_native_qt_window_class(event.get("class"))
        ):
            return False
        event_time = event.get("time_us")
        if not is_nonnegative_int(event_time):
            return False
        teardown_times = [
            int(candidate["time_us"])
            for candidate in lifecycle_events
            if isinstance(candidate, dict)
            and candidate.get("hwnd") == event.get("hwnd")
            and str(candidate.get("event", "")).casefold()
            in {"hide", "destroy"}
            and is_nonnegative_int(candidate.get("time_us"))
        ]
        if int(event_time) in teardown_times:
            return False
        next_teardown = min(
            (
                time_us
                for time_us in teardown_times
                if time_us > int(event_time)
            ),
            default=None,
        )

        def is_in_visibility_episode(time_us: object) -> bool:
            return (
                is_nonnegative_int(time_us)
                and int(time_us) >= int(event_time)
            ) and (
                next_teardown is None
                or int(time_us) < next_teardown
            )

        def evidence_stage_follows_show(stage: object) -> bool:
            show_stage = str(event.get("stage", ""))
            evidence_stage = str(stage)
            return evidence_stage == show_stage or (
                stage_positions.get(evidence_stage)
                == stage_positions.get(show_stage, -2) + 1
            )

        native_window_bound = any(
            isinstance(native_event, dict)
            and native_event.get("state") == "present"
            and native_event.get("visible")
            and not native_event.get("cloaked")
            and native_event.get("hwnd") == event.get("hwnd")
            and evidence_stage_follows_show(native_event.get("stage"))
            and native_event.get("class") == event.get("class")
            and native_event.get("title") == event.get("title")
            and evidence_time_is_in_stage(
                native_event.get("stage"),
                native_event.get("time_us"),
            )
            and is_in_visibility_episode(native_event.get("time_us"))
            for native_event in events
        )
        qt_window_bound = any(
            isinstance(sample, dict)
            and sample.get("state") == "visible"
            and sample.get("hwnd") == event.get("hwnd")
            and evidence_stage_follows_show(sample.get("stage"))
            and sample.get("title") == event.get("title")
            and is_expected_qt_top_level(sample)
            and evidence_time_is_in_stage(
                sample.get("stage"),
                sample.get("time_us"),
            )
            and is_in_visibility_episode(sample.get("time_us"))
            for sample in qt_top_level_samples
        )
        first_native_show_key = min(
            (
                (int(candidate["time_us"]), candidate_index)
                for candidate_index, candidate in enumerate(lifecycle_events)
                if isinstance(candidate, dict)
                and candidate.get("hwnd") == event.get("hwnd")
                and str(candidate.get("event", "")).casefold() == "show"
                and is_nonnegative_int(candidate.get("time_us"))
            ),
            default=None,
        )
        is_first_native_show = first_native_show_key == (
            int(event_time),
            event_index,
        )
        qt_child_bound = any(
            isinstance(object_event, dict)
            and object_event.get("event") == "WinIdChange"
            and not object_event.get("is_window")
            and object_event.get("visible")
            and object_event.get("class") == "QWidget"
            and object_event.get("object_name") == "production_central"
            and object_event.get("parent_class") == "QMainWindow"
            and object_event.get("parent_object_name")
            == "production_main_window"
            and object_event.get("hwnd") == event.get("hwnd")
            and object_event.get("stage") == event.get("stage")
            and event.get("stage") == "preflight"
            and is_first_native_show
            and str(event.get("class", "")).startswith("Qt")
            and str(event.get("class", "")).endswith("QWindowIcon")
            and event.get("title") == "python"
            and evidence_time_is_in_stage(
                object_event.get("stage"),
                object_event.get("time_us"),
            )
            and int(object_event["time_us"]) < int(event_time)
            for object_event in object_events
        )
        return evidence_time_is_in_stage(
            event.get("stage"),
            event_time,
        ) and (native_window_bound or qt_window_bound or qt_child_bound)

    if any(
        isinstance(event, dict)
        and str(event.get("event", "")).casefold() == "show"
        and str(event.get("class", "")) != "_q_titlebar"
        and str(event.get("class", "")) not in IGNORED_WINDOW_CLASSES
        and not lifecycle_show_is_bound(event_index, event)
        for event_index, event in enumerate(lifecycle_events)
    ):
        violations.append("native lifecycle evidence is unbound")

    if any(
        isinstance(event, dict)
        and event.get("object_name") == "dialog_form_label"
        and event.get("is_window")
        for event in object_events
    ):
        violations.append("top-level dialog_form_label was observed")

    return tuple(violations)


def _capture_cycle_with_artifacts(
    cycle: int,
    result_root: Path,
) -> dict[str, object]:
    user32, kernel32, dwmapi = _windows_libraries()
    _configure_windows_api(user32, kernel32, dwmapi)
    desktop_name = f"OriginAutoRound10_{uuid.uuid4().hex}"
    desktop = user32.CreateDesktopW(
        desktop_name,
        None,
        None,
        0,
        DESKTOP_ALL_ACCESS,
        None,
    )
    if not desktop:
        raise ctypes.WinError(ctypes.get_last_error())

    result_path = result_root / f"cycle-{cycle}-{uuid.uuid4().hex}.json"
    process = ProcessInformation()
    events: list[dict[str, object]] = []
    window_samples: list[dict[str, object]] = []
    last: dict[int, dict[str, object]] = {}
    last_window_sample_signature: object = None
    successful_enumerations = 0
    window_lookup_failures = 0
    window_lookup_races = 0
    last_main_hwnd = 0
    stage = "startup"

    try:
        command = ctypes.create_unicode_buffer(
            f'"{sys.executable}" "{Path(__file__).resolve()}" '
            f'--child-result "{result_path}"'
        )
        startup = StartupInfo()
        startup.cb = ctypes.sizeof(startup)
        startup.lpDesktop = f"WinSta0\\{desktop_name}"
        if not kernel32.CreateProcessW(
            None,
            command,
            None,
            None,
            False,
            CREATE_NO_WINDOW,
            None,
            str(ROOT),
            ctypes.byref(startup),
            ctypes.byref(process),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        ready_path = result_path.with_suffix(result_path.suffix + ".ready")
        observing_path = result_path.with_suffix(
            result_path.suffix + ".observing"
        )
        observation_started = False
        ready_deadline = time.monotonic() + 8.0
        while not ready_path.is_file():
            wait_result = kernel32.WaitForSingleObject(process.hProcess, 0)
            if wait_result == WAIT_OBJECT_0:
                raise RuntimeError(
                    "child exited before private-desktop observation was ready"
                )
            if wait_result != WAIT_TIMEOUT:
                raise RuntimeError(
                    f"WaitForSingleObject failed: {wait_result}"
                )
            if time.monotonic() >= ready_deadline:
                raise RuntimeError(
                    "child did not become ready for private-desktop observation"
                )
            time.sleep(0.001)

        callback_type = ctypes.WINFUNCTYPE(
            wintypes.BOOL,
            wintypes.HWND,
            wintypes.LPARAM,
        )

        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            wait_result = kernel32.WaitForSingleObject(
                process.hProcess,
                0,
            )
            if wait_result == WAIT_OBJECT_0:
                break
            if wait_result != WAIT_TIMEOUT:
                raise RuntimeError(
                    f"WaitForSingleObject failed: {wait_result}"
                )
            if (
                stage == "complete"
                and last_main_hwnd
                and not user32.IsWindowVisible(last_main_hwnd)
            ):
                wait_result = kernel32.WaitForSingleObject(
                    process.hProcess,
                    5_000,
                )
                if wait_result == WAIT_OBJECT_0:
                    break
                if wait_result == WAIT_TIMEOUT:
                    raise RuntimeError(
                        "child did not exit after hiding the main window"
                    )
                raise RuntimeError(
                    f"WaitForSingleObject failed: {wait_result}"
                )
            current: dict[int, dict[str, object]] = {}
            callback_errors: list[str] = []

            @callback_type
            def visit(hwnd: int, _lparam: int) -> bool:
                nonlocal window_lookup_failures, window_lookup_races
                try:
                    process_id = wintypes.DWORD()
                    ctypes.set_last_error(0)
                    thread_id = user32.GetWindowThreadProcessId(
                        hwnd,
                        ctypes.byref(process_id),
                    )
                    lookup_error = ctypes.get_last_error()
                    if not thread_id:
                        if lookup_error == 1400:
                            window_lookup_races += 1
                            return True
                        window_lookup_failures += 1
                        raise RuntimeError(
                            "GetWindowThreadProcessId failed: "
                            f"{lookup_error}"
                        )
                    if process_id.value == process.dwProcessId:
                        current[int(hwnd)] = _window_snapshot(
                            int(hwnd),
                            user32,
                            dwmapi,
                        )
                    return True
                except _WindowSnapshotRace:
                    window_lookup_races += 1
                    return True
                except Exception as error:
                    callback_errors.append(repr(error))
                    return False

            ctypes.set_last_error(0)
            enumeration_succeeded = user32.EnumDesktopWindows(desktop, visit, 0)
            enumeration_error = ctypes.get_last_error()
            if callback_errors:
                raise RuntimeError(
                    "EnumDesktopWindows callback failed: "
                    + "; ".join(callback_errors)
                )
            if not enumeration_succeeded:
                raise RuntimeError(
                    f"EnumDesktopWindows failed: {enumeration_error}"
                )
            main_entry = next(
                (
                    (hwnd, snapshot)
                    for hwnd, snapshot in current.items()
                    if str(snapshot["title"]).startswith("ROUND10|")
                ),
                None,
            )
            if main_entry is not None:
                last_main_hwnd, main = main_entry
                stage = str(main["title"]).partition("|")[2]

            visible_target_windows = [
                {"hwnd": hwnd, **snapshot}
                for hwnd, snapshot in current.items()
                if snapshot["visible"]
                and not snapshot["cloaked"]
                and snapshot["class"] not in IGNORED_WINDOW_CLASSES
            ]
            if enumeration_succeeded and visible_target_windows:
                successful_enumerations += 1
            elif stage not in {"startup", "complete"}:
                raise RuntimeError(
                    f"target windows missing during active stage: {stage}"
                )
            elif (
                stage == "complete"
                and last_main_hwnd
                and user32.IsWindowVisible(last_main_hwnd)
            ):
                raise RuntimeError(
                    "target windows missing during completion "
                    "while the main window remained visible"
                )

            timestamp_us = time.perf_counter_ns() // 1_000
            window_sample_signature = (
                stage,
                tuple(
                    (
                        window["hwnd"],
                        window["class"],
                        window["title"],
                        window["owner"],
                        window["parent"],
                        window["style"],
                        window["exstyle"],
                    )
                    for window in visible_target_windows
                ),
            )
            window_sample_changed = (
                window_sample_signature != last_window_sample_signature
            )
            if window_sample_changed:
                window_samples.append(
                    {
                        "time_us": timestamp_us,
                        "stage": stage,
                        "windows": visible_target_windows,
                    }
                )
                last_window_sample_signature = window_sample_signature

            if window_sample_changed:
                for snapshot in current.values():
                    events.append(
                        {
                            "time_us": timestamp_us,
                            "stage": stage,
                            "state": "present",
                            **snapshot,
                        }
                    )
            for hwnd, snapshot in last.items():
                if hwnd not in current:
                    events.append(
                        {
                            "time_us": timestamp_us,
                            "stage": stage,
                            "state": "not_top_level",
                            **snapshot,
                        }
                    )
            if (
                not observation_started
                and stage == "startup"
                and any(
                    window["title"] == "Spectrum Organizer"
                    for window in visible_target_windows
                )
            ):
                observing_path.write_text("observing", encoding="ascii")
                observation_started = True
            last = current

            wait_result = kernel32.WaitForSingleObject(process.hProcess, 0)
            if wait_result == WAIT_OBJECT_0:
                break
            if wait_result != WAIT_TIMEOUT:
                raise RuntimeError(f"WaitForSingleObject failed: {wait_result}")
            time.sleep(0.001)

        wait_result = kernel32.WaitForSingleObject(process.hProcess, 0)
        if wait_result != WAIT_OBJECT_0:
            if wait_result != WAIT_TIMEOUT:
                raise RuntimeError(f"WaitForSingleObject failed: {wait_result}")
            terminated = kernel32.TerminateProcess(process.hProcess, 92)
            if (
                not terminated
                and kernel32.WaitForSingleObject(process.hProcess, 2_000)
                != WAIT_OBJECT_0
            ):
                raise RuntimeError("TerminateProcess failed")
            if (
                terminated
                and kernel32.WaitForSingleObject(process.hProcess, 2_000)
                != WAIT_OBJECT_0
            ):
                raise RuntimeError("terminated child process did not exit")
            raise RuntimeError(
                f"child timed out during private-desktop stage: {stage}"
            )

        exit_code = wintypes.DWORD(STILL_ACTIVE)
        if not kernel32.GetExitCodeProcess(
            process.hProcess,
            ctypes.byref(exit_code),
        ):
            raise RuntimeError("GetExitCodeProcess failed")
        if not successful_enumerations:
            raise RuntimeError("EnumDesktopWindows failed throughout the cycle")
        child_result = (
            json.loads(result_path.read_text(encoding="utf-8"))
            if result_path.is_file()
            else {"status": "missing"}
        )
    finally:
        cleanup_errors: list[str] = []
        if process.hProcess:
            wait_result = kernel32.WaitForSingleObject(process.hProcess, 0)
            if wait_result == WAIT_FAILED:
                cleanup_errors.append("WaitForSingleObject")
            if wait_result != WAIT_OBJECT_0:
                terminated = kernel32.TerminateProcess(
                    process.hProcess,
                    93,
                )
                if (
                    not terminated
                    and kernel32.WaitForSingleObject(
                        process.hProcess,
                        2_000,
                    )
                    != WAIT_OBJECT_0
                ):
                    cleanup_errors.append("TerminateProcess")
                elif terminated and (
                    kernel32.WaitForSingleObject(process.hProcess, 2_000)
                    != WAIT_OBJECT_0
                ):
                    cleanup_errors.append("WaitForSingleObject")
            if process.hThread and not kernel32.CloseHandle(process.hThread):
                cleanup_errors.append("CloseHandle(thread)")
            if not kernel32.CloseHandle(process.hProcess):
                cleanup_errors.append("CloseHandle(process)")
        if not user32.CloseDesktop(desktop):
            cleanup_errors.append("CloseDesktop")
        if cleanup_errors:
            raise RuntimeError(
                "private-desktop cleanup failed: " + ", ".join(cleanup_errors)
            )

    visible_events = [
        event
        for event in events
        if event["state"] == "present"
        and event["visible"]
        and not event["cloaked"]
        and event["class"] not in IGNORED_WINDOW_CLASSES
    ]
    report = {
        "cycle": cycle,
        "pid": int(process.dwProcessId),
        "exit_code": int(exit_code.value),
        "child_result": child_result,
        "callback_errors": [],
        "window_lookup_failures": window_lookup_failures,
        "window_lookup_races": window_lookup_races,
        "visible_events": visible_events,
        "window_samples": window_samples,
        "titlebar_events": [
            event for event in events if event["class"] == "_q_titlebar"
        ],
    }
    violations = _report_violations(report)
    if violations:
        raise RuntimeError(
            "private-desktop observation failed: " + "; ".join(violations)
        )
    return report


def _capture_cycle(cycle: int) -> dict[str, object]:
    with tempfile.TemporaryDirectory(
        prefix="spectrum-organizer-round10-",
    ) as result_root:
        return _capture_cycle_with_artifacts(cycle, Path(result_root))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--child-result", type=Path)
    parser.add_argument("--cycles", type=int, default=1)
    args = parser.parse_args()
    if args.cycles < 1:
        parser.error("--cycles must be a positive integer")

    if args.child_result is not None:
        try:
            return _run_child(args.child_result)
        except Exception as error:
            args.child_result.write_text(
                json.dumps(
                    {"status": "error", "error": repr(error)},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            return 1

    if os.name != "nt":
        raise SystemExit("Windows-only probe")
    reports = []
    for cycle in range(1, args.cycles + 1):
        try:
            reports.append(_capture_cycle(cycle))
        except Exception as error:
            reports.append(
                {
                    "cycle": cycle,
                    "exit_code": 1,
                    "child_result": {"status": "error"},
                    "callback_errors": [repr(error)],
                    "visible_events": [],
                    "window_samples": [],
                    "titlebar_events": [],
                }
            )
    print(json.dumps(reports, ensure_ascii=False, indent=2))
    return 0 if all(not _report_violations(report) for report in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
