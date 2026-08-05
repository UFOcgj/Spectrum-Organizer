from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from typing import Callable

from spectrum_organizer.core.attribution import build_attribution_fields
from spectrum_organizer.domain.normalization import normalize_concentration_input, normalize_temperature
from spectrum_organizer.safety.name_policy import validate_user_origin_name_text
from spectrum_organizer.ui.dialogs import (
    DialogRequest,
    FinalReviewConflictEditor,
    FinalReviewConflictGroup,
    FinalReviewConflictSelection,
    FinalReviewDialogRequest,
    FinalReviewViewState,
)


class _FinalReviewConflictProjectionLane:
    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._pending: Callable[[], None] | None = None
        self._thread: threading.Thread | None = None

    def submit(self, callback: Callable[[], None]) -> None:
        with self._condition:
            self._pending = callback
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._run,
                    name="SpectrumOrganizerFinalReview",
                    daemon=True,
                )
                self._thread.start()
            self._condition.notify()

    def _run(self) -> None:
        while True:
            with self._condition:
                while self._pending is None:
                    self._condition.wait()
                callback = self._pending
                self._pending = None
            callback()


_FINAL_REVIEW_CONFLICT_PROJECTION_LANE = (
    _FinalReviewConflictProjectionLane()
)


@dataclass(frozen=True)
class DialogResponse:
    action: str
    selected_row_id: str = ""
    view_state: FinalReviewViewState = field(
        default_factory=FinalReviewViewState
    )
    conflict_selections: tuple[FinalReviewConflictSelection, ...] = ()
    conflict_pending_selections: tuple[
        FinalReviewConflictSelection,
        ...,
    ] = ()
    conflict_editing_group_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class AttributionDialogRequest:
    target_label: str
    source_filename: str
    book_display_names: tuple[str, ...]
    prefill: dict[str, str] = field(default_factory=dict)
    prefill_source: str = ""
    allow_apply_to_remaining_folder: bool = False
    allow_split_folder: bool = False
    allow_return_to_book_picker: bool = False
    allow_return_previous: bool = False
    targeted_correction: bool = False
    initial_scope: str = ""
    selected_book_display_name: str = ""
    affected_book_count: int = 0


@dataclass(frozen=True)
class AttributionDialogResponse:
    action: str
    sample_type: str = ""
    values: dict[str, str] = field(default_factory=dict)
    apply_to_remaining_folder: bool = False
    split_folder: bool = False
    attribution_scope: str = ""


@dataclass(frozen=True)
class AttributionBookSelectionRequest:
    folder_label: str
    source_filename: str
    choices: tuple[tuple[str, str], ...]
    allow_return_to_folder: bool = True


@dataclass(frozen=True)
class AttributionBookSelectionResponse:
    action: str
    book_key: str = ""


@dataclass(frozen=True)
class ConflictReviewChoice:
    book_key: str
    display_name: str
    fields: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class ConflictReviewGroup:
    group_key: str
    choices: tuple[ConflictReviewChoice, ...]
    initial_selection: str
    common_fields: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class ConflictReviewRequest:
    kind: str
    title: str
    instruction: str
    choices: tuple[ConflictReviewChoice, ...]
    selection_mode: str
    decision_subject: str = ""
    actions: tuple[str, ...] = ("confirm_selection", "cancel")
    single_select_groups: tuple[tuple[str, ...], ...] = ()
    initial_selection: tuple[str, ...] = ()
    choice_groups: tuple[ConflictReviewGroup, ...] = ()
    initial_active_group_key: str = ""
    initial_scroll_value: int = 0
    editing_existing_decisions: bool = False


@dataclass(frozen=True)
class ConflictReviewResponse:
    action: str
    selected_book_keys: tuple[str, ...] = ()
    group_selections: tuple[tuple[str, str], ...] = ()
    active_group_key: str = ""
    scroll_value: int = 0


ORGANIZER_DIALOG_STYLE_SHEET = """
    QDialog#organizer_dialog {
        background: #f5f7f6;
        color: #1f2928;
        border: 1px solid #7f9691;
        border-radius: 8px;
        font-family: 'Microsoft YaHei UI';
        font-size: 13px;
    }
    QFrame#dialog_header {
        background: #263332;
        border-top-left-radius: 8px;
        border-top-right-radius: 8px;
        border-bottom: 3px solid #147a6c;
        min-height: 46px;
    }
    QLabel#dialog_title {
        color: #ffffff;
        font-size: 15px;
        font-weight: 700;
    }
    QPushButton#dialog_close_button {
        background: transparent;
        color: #d8e2df;
        border: 1px solid #526260;
        border-radius: 4px;
        padding: 0;
        min-width: 28px;
        min-height: 26px;
    }
    QPushButton#dialog_close_button:hover {
        background: #334241;
    }
    QFrame#dialog_body {
        background: #f5f7f6;
    }
    QLabel#dialog_message {
        background: #f5f7f6;
        color: #1f2928;
        line-height: 1.5;
    }
    QLabel#dialog_help_text {
        color: #64716f;
        font-size: 12px;
    }
    QFrame#conflict_review_guidance {
        background: #eaf3f0;
        border: 1px solid #c8d9d4;
        border-left: 4px solid #147a6c;
        border-radius: 4px;
    }
    QLabel#conflict_review_guidance_text {
        background: transparent;
        color: #263332;
    }
    QLabel#conflict_review_subject {
        background: transparent;
        color: #0f655c;
        font-size: 15pt;
        font-weight: 700;
    }
    QFrame#conflict_review_detail {
        background: #ffffff;
        border: 1px solid #c8d2cf;
        border-radius: 4px;
    }
    QLabel#conflict_review_detail_title {
        background: transparent;
        color: #263332;
        font-weight: 700;
    }
    QLabel#conflict_review_detail_value {
        background: transparent;
        color: #53615f;
        font-size: 12px;
    }
    QFrame#conflict_review_footer,
    QFrame#final_review_conflict_footer {
        background: #f5f7f6;
        border-top: 1px solid #d6dfdc;
    }
    QScrollArea#dialog_message_scroll,
    QScrollArea#attribution_body_scroll,
    QScrollArea#attribution_picker_scroll {
        background: #f5f7f6;
        border: 0;
    }
    QScrollArea#dialog_message_scroll > QWidget#qt_scrollarea_viewport,
    QScrollArea#attribution_body_scroll > QWidget#qt_scrollarea_viewport,
    QScrollArea#attribution_picker_scroll > QWidget#qt_scrollarea_viewport {
        background: #f5f7f6;
    }
    QPushButton#dialog_button_primary,
    QPushButton#final_review_confirm,
    QPushButton#final_review_conflict_confirm {
        background: #263332;
        color: #ffffff;
        border: 1px solid #64716f;
        border-radius: 4px;
        padding: 6px 12px;
        min-width: 78px;
        min-height: 28px;
    }
    QPushButton#dialog_button_primary:hover,
    QPushButton#final_review_confirm:hover,
    QPushButton#final_review_conflict_confirm:hover {
        background: #334241;
    }
    QPushButton#final_review_conflict_confirm:disabled {
        background: #b8c5c1;
        color: #edf1ef;
        border-color: #b8c5c1;
    }
    QPushButton#dialog_button_secondary,
    QPushButton#final_review_modify_attribution,
    QPushButton#final_review_modify_conflicts,
    QPushButton#final_review_conflict_back,
    QPushButton#final_review_conflict_decision {
        background: #ffffff;
        color: #263332;
        border: 1px solid #b8c5c1;
        border-radius: 4px;
        padding: 6px 12px;
        min-width: 78px;
        min-height: 28px;
    }
    QPushButton#dialog_button_secondary:hover,
    QPushButton#final_review_modify_attribution:hover,
    QPushButton#final_review_modify_conflicts:hover,
    QPushButton#final_review_conflict_back:hover,
    QPushButton#final_review_conflict_decision:hover {
        background: #dcebe7;
    }
    QPushButton#final_review_conflict_decision:checked {
        background: #147a6c;
        color: #ffffff;
        border-color: #147a6c;
    }
    QPushButton#oxygen_environment_air,
    QPushButton#oxygen_environment_deo2 {
        background: #ffffff;
        color: #263332;
        border: 1px solid #b8c5c1;
        border-radius: 4px;
        padding: 6px 12px;
        min-height: 28px;
    }
    QPushButton#oxygen_environment_air:hover,
    QPushButton#oxygen_environment_deo2:hover {
        background: #edf1ef;
    }
    QPushButton#oxygen_environment_air:checked,
    QPushButton#oxygen_environment_deo2:checked {
        background: #147a6c;
        color: #ffffff;
        border-color: #147a6c;
    }
    QPushButton#attribution_folder_mode,
    QPushButton#attribution_book_mode {
        background: #ffffff;
        color: #263332;
        border: 1px solid #b8c5c1;
        border-radius: 4px;
        padding: 6px 12px;
        min-height: 28px;
    }
    QPushButton#attribution_folder_mode:hover,
    QPushButton#attribution_book_mode:hover {
        background: #edf1ef;
    }
    QPushButton#attribution_folder_mode:checked,
    QPushButton#attribution_book_mode:checked {
        background: #147a6c;
        color: #ffffff;
        border-color: #147a6c;
    }
    QListWidget#attribution_pending_book_list {
        background: #ffffff;
        color: #1f2928;
        border: 1px solid #b8c5c1;
        outline: 0;
    }
    QListWidget#attribution_pending_book_list::item {
        padding: 6px 8px;
    }
    QListWidget#attribution_pending_book_list::item:hover {
        background: #dcebe7;
        color: #263332;
    }
    QListWidget#attribution_pending_book_list::item:selected {
        background: #147a6c;
        color: #ffffff;
    }
    QTreeWidget#conflict_review_candidates {
        background: #ffffff;
        color: #1f2928;
        border: 1px solid #b8c5c1;
        outline: 0;
    }
    QTreeWidget#conflict_review_candidates::item {
        padding: 4px 8px;
    }
    QTreeWidget#conflict_review_candidates::item:hover {
        background: #dcebe7;
        color: #263332;
    }
    QTreeWidget#conflict_review_candidates::item:selected,
    QTreeWidget#conflict_review_candidates::item:selected:hover {
        background: #147a6c;
        color: #ffffff;
    }
    QTabWidget#final_review_tabs::pane {
        background: #f5f7f6;
        border: 1px solid #c8d2cf;
    }
    QTabBar#final_review_tabs_tabbar::tab {
        background: #edf1ef;
        color: #53615f;
        border: 1px solid #c8d2cf;
        border-bottom: 0;
        padding: 8px 18px;
        min-width: 96px;
    }
    QTabBar#final_review_tabs_tabbar::tab:selected {
        background: #ffffff;
        color: #0f655c;
        border-top: 3px solid #147a6c;
        font-weight: 700;
    }
    QTableWidget#final_review_table,
    QTreeWidget#final_review_output_tree,
    QTreeWidget#final_review_conflict_choices {
        background: #ffffff;
        color: #1f2928;
        border: 1px solid #b8c5c1;
        gridline-color: #d6dfdc;
        outline: 0;
    }
    QTableWidget#final_review_table::item,
    QTreeWidget#final_review_output_tree::item,
    QTreeWidget#final_review_conflict_choices::item {
        padding: 6px 8px;
    }
    QTableWidget#final_review_table::item:hover,
    QTreeWidget#final_review_output_tree::item:hover,
    QTreeWidget#final_review_conflict_choices::item:hover {
        background: #dcebe7;
        color: #263332;
    }
    QTableWidget#final_review_table::item:selected,
    QTableWidget#final_review_table::item:selected:hover,
    QTreeWidget#final_review_output_tree::item:selected,
    QTreeWidget#final_review_output_tree::item:selected:hover,
    QTreeWidget#final_review_conflict_choices::item:selected,
    QTreeWidget#final_review_conflict_choices::item:selected:hover {
        background: #147a6c;
        color: #ffffff;
    }
    QTreeWidget#final_review_conflict_choices::item:disabled,
    QTreeWidget#final_review_conflict_choices::item:disabled:hover {
        background: #fff3d6;
        color: #8a5a00;
    }
    QFrame#final_review_conflict_group {
        background: #ffffff;
        border: 1px solid #c8d2cf;
        border-radius: 4px;
    }
    QFrame#final_review_conflict_group[active_group="true"] {
        border-left: 4px solid #147a6c;
    }
    QLabel#final_review_conflict_group_title {
        color: #0f655c;
        font-weight: 700;
    }
    QLabel#final_review_conflict_warning {
        background: #fff3d6;
        color: #8a5a00;
        border-left: 4px solid #c88822;
        padding: 6px 8px;
    }
    QScrollArea#final_review_conflict_scroll,
    QScrollArea#final_review_conflict_scroll > QWidget#qt_scrollarea_viewport {
        background: #f5f7f6;
        border: 0;
    }
    QLabel#final_review_search_count {
        background: #eaf3f0;
        color: #0f655c;
        border: 1px solid #b8c5c1;
        border-radius: 4px;
        padding: 5px 8px;
        font-family: Consolas;
    }
    QToolButton#final_review_search_up,
    QToolButton#final_review_search_down {
        background: #ffffff;
        color: #263332;
        border: 1px solid #b8c5c1;
        border-radius: 4px;
    }
    QToolButton#final_review_search_up:hover,
    QToolButton#final_review_search_down:hover {
        background: #dcebe7;
    }
    QToolButton#final_review_search_up:disabled,
    QToolButton#final_review_search_down:disabled {
        background: #edf1ef;
        color: #9aa5a2;
    }
    QPushButton#dialog_button_danger,
    QPushButton#final_review_cancel,
    QPushButton#final_review_conflict_cancel {
        background: #ffffff;
        color: #8e312b;
        border: 1px solid #91635f;
        border-radius: 4px;
        padding: 6px 12px;
        min-width: 78px;
        min-height: 28px;
    }
    QPushButton#dialog_button_danger:hover,
    QPushButton#final_review_cancel:hover,
    QPushButton#final_review_conflict_cancel:hover {
        background: #fff1ef;
    }
    QFrame#conflict_review_footer QPushButton#dialog_button_primary,
    QFrame#conflict_review_footer QPushButton#dialog_button_secondary,
    QFrame#conflict_review_footer QPushButton#dialog_button_danger {
        padding: 6px 12px;
        min-width: 72px;
        min-height: 28px;
        font-size: 14px;
        font-weight: 600;
    }
    QPushButton#dialog_button_primary[compact_conflict_action="true"],
    QPushButton#dialog_button_secondary[compact_conflict_action="true"],
    QPushButton#dialog_button_danger[compact_conflict_action="true"] {
        padding-left: 4px;
        padding-right: 4px;
        min-width: 0;
    }
    QComboBox, QLineEdit {
        background: #ffffff;
        color: #1f2928;
        border: 1px solid #b8c5c1;
        border-radius: 4px;
        padding: 5px 10px;
        min-height: 28px;
        selection-background-color: #147a6c;
    }
    QLabel#attribution_source_name,
    QLabel#attribution_folder_name {
        background: transparent;
        color: #1f2928;
        border: 0;
        border-radius: 0;
        padding: 0;
        min-height: 24px;
        font-weight: 700;
        selection-background-color: #147a6c;
        selection-color: #ffffff;
    }
    QComboBox {
        padding-right: 28px;
    }
    QComboBox::drop-down {
        border: 0;
        width: 28px;
        background: transparent;
    }
    QComboBox::down-arrow {
        image: none;
        width: 0;
        height: 0;
    }
    QComboBox QAbstractItemView {
        background: #ffffff;
        color: #1f2928;
        border: 1px solid #b8c5c1;
        selection-background-color: #147a6c;
        selection-color: #ffffff;
        outline: 0;
    }
    QLabel#dialog_form_label {
        color: #263332;
        font-weight: 600;
    }
    QLabel#dialog_error_text {
        background: #fff2ef;
        color: #8f2f28;
        border: 1px solid #d46a5f;
        border-left: 4px solid #b64036;
        border-radius: 4px;
        padding: 7px 9px;
        font-size: 12px;
        font-weight: 600;
    }
    QCheckBox {
        color: #263332;
        spacing: 8px;
    }
    QScrollBar:vertical {
        background: transparent;
        width: 14px;
        margin: 0;
        border: 0;
    }
    QScrollBar::handle:vertical {
        background: #7f9691;
        border-radius: 5px;
        min-height: 36px;
        margin: 2px 3px;
    }
    QScrollBar::handle:vertical:hover {
        background: #5f7771;
    }
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
        height: 0;
        border: 0;
        background: transparent;
    }
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
        background: transparent;
    }
"""

_ACTION_LABELS = {
    "retry": "重新检测",
    "cancel": "取消",
    "confirm": "确认",
    "continue": "继续",
    "acknowledge": "知道了",
    "backup_new_empty": "备份旧库并新建空库",
    "confirm_close_hidden_origin": "确认关闭",
    "choose_another_parent": "重新选择",
    "open_output_folder": "打开输出文件夹",
    "start_new_task": "开始新任务",
    "exit": "退出",
    "select_one": "选择一条",
    "select_many": "选择多条",
    "return_to_attribution": "返回样品归属",
    "modify_attribution": "修改样品归属",
    "modify_conflicts": "修改冲突选择",
    "exclude": "排除",
    "regular": "常规谱",
    "keep": "继续运行",
}
_PRIMARY_ACTIONS = {
    "retry",
    "confirm",
    "continue",
    "acknowledge",
    "backup_new_empty",
    "confirm_close_hidden_origin",
    "choose_another_parent",
    "open_output_folder",
    "start_new_task",
    "select_one",
    "select_many",
    "继续运行",
}
_DANGER_ACTIONS = {"cancel", "exit", "取消并退出"}


class QtManualDialogPort:
    def __init__(
        self,
        *,
        parent: object | None = None,
        message_box_factory: Callable[[], object] | None = None,
        qt_flags: object | None = None,
        button_role: object | None = None,
    ):
        self._parent = parent
        self._message_box_factory = message_box_factory
        self._qt_flags = qt_flags
        self._button_role = button_role

    def choose(self, request: DialogRequest) -> DialogResponse:
        if self._message_box_factory is not None:
            return self._choose_message_box(request)
        qt_widgets, _ = _load_qt_modules()
        if not hasattr(qt_widgets, "QDialog"):
            return self._choose_message_box(request)
        return show_styled_dialog(request, parent=self._parent)

    def _choose_message_box(self, request: DialogRequest) -> DialogResponse:
        box = self._create_message_box()
        box.setWindowTitle(request.title)
        box.setText(request.message)
        self._apply_window_flags(box, request)

        buttons = {}
        for action in request.actions:
            button = box.addButton(_display_label(action, request.kind), self._action_role())
            if action == "confirm" and not request.can_confirm:
                button.setEnabled(False)
            buttons[button] = action

        box.exec()
        clicked = box.clickedButton()
        if clicked is None:
            return DialogResponse(action=_fallback_action(request.actions))
        return DialogResponse(action=buttons[clicked])

    def _create_message_box(self):
        if self._message_box_factory is not None:
            return self._message_box_factory()
        qt_widgets, _ = _load_qt_modules()
        _ensure_application(qt_widgets)
        return qt_widgets.QMessageBox()

    def _apply_window_flags(self, box: object, request: DialogRequest) -> None:
        flags = self._qt_flags
        if flags is None:
            _, qt_core = _load_qt_modules()
            flags = _QtFlags(qt_core)
        if request.topmost:
            box.setWindowFlag(flags.window_stays_on_top, True)
        if request.taskbar_visible:
            box.setWindowFlag(flags.window, True)

    def _action_role(self):
        if self._button_role is not None:
            return self._button_role
        if self._message_box_factory is not None:
            return None
        qt_widgets, _ = _load_qt_modules()
        try:
            return qt_widgets.QMessageBox.ButtonRole.ActionRole
        except AttributeError:
            return qt_widgets.QMessageBox.ActionRole


class QtAttributionDialogPort:
    def __init__(
        self,
        *,
        form_runner: Callable[[AttributionDialogRequest, object | None], AttributionDialogResponse] | None = None,
        book_picker_runner: Callable[
            [AttributionBookSelectionRequest, object | None], AttributionBookSelectionResponse
        ]
        | None = None,
    ):
        self._form_runner = form_runner
        self._book_picker_runner = book_picker_runner

    def choose(self, request: AttributionDialogRequest, *, parent: object | None = None) -> AttributionDialogResponse:
        if self._form_runner is not None:
            return self._form_runner(request, parent)
        return show_attribution_dialog(request, parent=parent)

    def choose_book(
        self,
        request: AttributionBookSelectionRequest,
        *,
        parent: object | None = None,
    ) -> AttributionBookSelectionResponse:
        if self._book_picker_runner is not None:
            return self._book_picker_runner(request, parent)
        return show_attribution_book_picker(request, parent=parent)


class QtConflictReviewDialogPort:
    def __init__(
        self,
        *,
        runner: Callable[
            [ConflictReviewRequest, object | None], ConflictReviewResponse
        ]
        | None = None,
    ):
        self._runner = runner

    def choose(
        self,
        request: ConflictReviewRequest,
        *,
        parent: object | None = None,
    ) -> ConflictReviewResponse:
        if self._runner is not None:
            return self._runner(request, parent)
        return show_conflict_review_dialog(request, parent=parent)


@dataclass(frozen=True)
class _QtFlags:
    qt_core: object

    @property
    def window_stays_on_top(self):
        return self.qt_core.Qt.WindowType.WindowStaysOnTopHint

    @property
    def window(self):
        return self.qt_core.Qt.WindowType.Window


_WINDOWS_USER32 = None


def _windows_user32() -> object:
    global _WINDOWS_USER32

    if _WINDOWS_USER32 is not None:
        return _WINDOWS_USER32

    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetWindowThreadProcessId.argtypes = (
        wintypes.HWND,
        ctypes.POINTER(wintypes.DWORD),
    )
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetClassNameW.argtypes = (
        wintypes.HWND,
        wintypes.LPWSTR,
        ctypes.c_int,
    )
    user32.GetClassNameW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = (
        wintypes.HWND,
        wintypes.LPWSTR,
        ctypes.c_int,
    )
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
    user32.ShowWindow.restype = wintypes.BOOL
    user32.SetParent.argtypes = (wintypes.HWND, wintypes.HWND)
    user32.SetParent.restype = wintypes.HWND
    user32.EnumWindows.argtypes = (ctypes.c_void_p, wintypes.LPARAM)
    user32.EnumWindows.restype = wintypes.BOOL
    user32.FindWindowExW.argtypes = (
        wintypes.HWND,
        wintypes.HWND,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
    )
    user32.FindWindowExW.restype = wintypes.HWND
    user32.MoveWindow.argtypes = (
        wintypes.HWND,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.BOOL,
    )
    user32.MoveWindow.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = (wintypes.HWND,)
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetWindow.argtypes = (wintypes.HWND, wintypes.UINT)
    user32.GetWindow.restype = wintypes.HWND
    user32.GetWindowLongPtrW.argtypes = (wintypes.HWND, ctypes.c_int)
    user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    user32.SetWindowLongPtrW.argtypes = (
        wintypes.HWND,
        ctypes.c_int,
        ctypes.c_ssize_t,
    )
    user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
    user32.DestroyWindow.argtypes = (wintypes.HWND,)
    user32.DestroyWindow.restype = wintypes.BOOL
    user32.UnhookWinEvent.argtypes = (wintypes.HANDLE,)
    user32.UnhookWinEvent.restype = wintypes.BOOL
    _WINDOWS_USER32 = user32
    return user32


def _quarantine_qt_titlebar_helper(hwnd: object) -> bool:
    if os.name != "nt":
        return False

    import ctypes
    from ctypes import wintypes

    user32 = _windows_user32()
    owner_process_id = wintypes.DWORD()
    ctypes.set_last_error(0)
    owner_thread_id = user32.GetWindowThreadProcessId(
        hwnd,
        ctypes.byref(owner_process_id),
    )
    owner_error = ctypes.get_last_error()
    if not owner_thread_id:
        if owner_error == 1400:
            return False
        raise RuntimeError(
            f"GetWindowThreadProcessId failed: {owner_error}"
        )
    if owner_process_id.value != os.getpid():
        return False
    class_name = ctypes.create_unicode_buffer(128)
    ctypes.set_last_error(0)
    class_name_length = user32.GetClassNameW(
        hwnd,
        class_name,
        len(class_name),
    )
    class_name_error = ctypes.get_last_error()
    if not class_name_length:
        if class_name_error == 1400:
            return False
        raise RuntimeError(f"GetClassNameW failed: {class_name_error}")
    if class_name.value != "_q_titlebar":
        return False
    user32.ShowWindow(hwnd, 0)
    ctypes.set_last_error(0)
    previous_parent = user32.SetParent(hwnd, wintypes.HWND(-3))
    parent_error = ctypes.get_last_error()
    if not previous_parent and parent_error:
        raise RuntimeError(f"SetParent failed: {parent_error}")
    return True


def _quarantine_qt_titlebar_helpers() -> None:
    if os.name != "nt":
        return

    import ctypes
    from ctypes import wintypes

    callback_type = ctypes.WINFUNCTYPE(
        wintypes.BOOL,
        wintypes.HWND,
        wintypes.LPARAM,
    )
    callback_errors: list[str] = []

    @callback_type
    def enumerate_titlebar(hwnd: object, _lparam: object) -> bool:
        try:
            _quarantine_qt_titlebar_helper(hwnd)
            return True
        except Exception as error:
            callback_errors.append(repr(error))
            return False

    user32 = _windows_user32()
    ctypes.set_last_error(0)
    enumeration_succeeded = user32.EnumWindows(enumerate_titlebar, 0)
    enumeration_error = ctypes.get_last_error()
    if callback_errors:
        raise RuntimeError(
            "titlebar enumeration callback failed: "
            + "; ".join(callback_errors)
        )
    if not enumeration_succeeded:
        raise RuntimeError(f"EnumWindows failed: {enumeration_error}")


def _record_native_titlebar_quarantine_error(
    application: object,
    error: Exception,
) -> None:
    callback_errors = getattr(
        application,
        "_native_titlebar_quarantine_errors",
        None,
    )
    if callback_errors is None:
        callback_errors = []
        application._native_titlebar_quarantine_errors = callback_errors
    callback_errors.append(repr(error))


def _install_native_titlebar_quarantine_hook(application: object) -> None:
    if os.name != "nt" or hasattr(
        application,
        "_native_titlebar_quarantine_hook",
    ):
        return

    import ctypes
    from ctypes import wintypes

    user32 = _windows_user32()
    if not hasattr(application, "_native_titlebar_quarantine_errors"):
        application._native_titlebar_quarantine_errors = []
    event_object_create = 0x8000
    event_object_show = 0x8002
    win_event_out_of_context = 0x0000
    callback_type = ctypes.WINFUNCTYPE(
        None,
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.HWND,
        wintypes.LONG,
        wintypes.LONG,
        wintypes.DWORD,
        wintypes.DWORD,
    )

    @callback_type
    def quarantine_titlebar_on_create_or_show(
        _hook: object,
        _event: object,
        hwnd: object,
        _object_id: object,
        _child_id: object,
        _thread_id: object,
        _event_time: object,
    ) -> None:
        try:
            if (
                _event in {event_object_create, event_object_show}
                and _object_id == 0
                and _child_id == 0
            ):
                _quarantine_qt_titlebar_helper(hwnd)
        except Exception as error:
            _record_native_titlebar_quarantine_error(application, error)

    user32.SetWinEventHook.argtypes = (
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HMODULE,
        callback_type,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
    )
    user32.SetWinEventHook.restype = wintypes.HANDLE
    ctypes.set_last_error(0)
    hook = user32.SetWinEventHook(
        event_object_create,
        event_object_show,
        0,
        quarantine_titlebar_on_create_or_show,
        os.getpid(),
        0,
        win_event_out_of_context,
    )
    hook_error = ctypes.get_last_error()
    if hook:
        application._native_titlebar_quarantine_hook = hook
        application._native_titlebar_quarantine_callback = (
            quarantine_titlebar_on_create_or_show
        )
    else:
        _record_native_titlebar_quarantine_error(
            application,
            RuntimeError(f"SetWinEventHook failed: {hook_error}"),
        )


def install_antialiased_window_surface(
    widget: object,
    qt_core: object,
) -> None:
    application = qt_core.QCoreApplication.instance()
    if application is not None:
        _install_native_titlebar_quarantine_hook(application)
    if (
        application is not None
        and not hasattr(application, "_native_titlebar_quarantine_filter")
    ):
        quarantine_scan_state = {"pending": False}

        def run_scheduled_quarantine_scan() -> None:
            quarantine_scan_state["pending"] = False
            try:
                _quarantine_qt_titlebar_helpers()
            except Exception as error:
                _record_native_titlebar_quarantine_error(
                    application,
                    error,
                )

        class NativeTitlebarQuarantineFilter(qt_core.QObject):
            def eventFilter(self, watched: object, event: object) -> bool:
                if event.type() in {
                    qt_core.QEvent.Type.WinIdChange,
                    qt_core.QEvent.Type.Show,
                } and not quarantine_scan_state["pending"]:
                    quarantine_scan_state["pending"] = True
                    qt_core.QTimer.singleShot(
                        0,
                        run_scheduled_quarantine_scan,
                    )
                return super().eventFilter(watched, event)

        titlebar_filter = NativeTitlebarQuarantineFilter(application)
        application._native_titlebar_quarantine_filter = titlebar_filter
        application.installEventFilter(titlebar_filter)
    widget.setAttribute(qt_core.Qt.WidgetAttribute.WA_TranslucentBackground, True)
    widget.clearMask()


def apply_styled_dialog_chrome(dialog: object, qt_core: object) -> None:
    install_antialiased_window_surface(dialog, qt_core)


def apply_combo_popup_palette(combo: object, qt_gui: object) -> None:
    palette = combo.view().palette()
    palette.setColor(
        qt_gui.QPalette.ColorRole.Highlight,
        qt_gui.QColor("#147a6c"),
    )
    palette.setColor(
        qt_gui.QPalette.ColorRole.HighlightedText,
        qt_gui.QColor("#ffffff"),
    )
    combo.view().setPalette(palette)
    combo.view().setStyleSheet(
        """
        QAbstractItemView {
            background: #ffffff;
            color: #1f2928;
            border: 1px solid #b8c5c1;
            outline: 0;
        }
        QAbstractItemView::item:hover {
            background: #dcebe7;
            color: #263332;
        }
        QAbstractItemView::item:selected {
            background: #147a6c;
            color: #ffffff;
        }
        """
    )


def _set_dialog_error(label: object, message: str) -> None:
    label.setText(message)
    label.setVisible(bool(message))


def _clear_dialog_error(label: object) -> None:
    label.clear()
    label.hide()


def _wrap_anywhere_label_type(
    qt_widgets: object,
    qt_core: object,
    qt_gui: object,
) -> type:
    class WrapAnywhereLabel(qt_widgets.QLabel):
        def _text_flags(self) -> object:
            return (
                qt_core.Qt.AlignmentFlag.AlignLeft
                | qt_core.Qt.AlignmentFlag.AlignVCenter
                | qt_core.Qt.TextFlag.TextWrapAnywhere
            )

        def hasHeightForWidth(self) -> bool:
            return True

        def heightForWidth(self, width: int) -> int:
            return self.fontMetrics().boundingRect(
                qt_core.QRect(0, 0, max(1, width), 10000),
                self._text_flags(),
                self.text(),
            ).height()

        def paintEvent(self, _event: object) -> None:
            painter = qt_gui.QPainter(self)
            painter.setPen(self.palette().color(qt_gui.QPalette.ColorRole.WindowText))
            painter.drawText(self.contentsRect(), self._text_flags(), self.text())

    return WrapAnywhereLabel


def _create_semantic_text_delegate(
    qt_widgets: object,
    qt_core: object,
    qt_gui: object,
    view: object,
    *,
    minimum_height: int,
    divider_role: object | None = None,
) -> object:
    class SemanticTextDelegate(
        _ConflictCellLayoutMixin,
        qt_widgets.QStyledItemDelegate,
    ):
        def __init__(self, parent: object) -> None:
            super().__init__(parent)
            self._semantic_line_cache = {}

        @staticmethod
        def _style_and_text_rect(
            styled: object,
        ) -> tuple[object, object]:
            style = (
                styled.widget.style()
                if styled.widget is not None
                else qt_widgets.QApplication.style()
            )
            text_rect = style.subElementRect(
                qt_widgets.QStyle.SubElement.SE_ItemViewItemText,
                styled,
                styled.widget,
            )
            return style, text_rect

        @classmethod
        def _atomic_missing_lines(
            cls,
            text: str,
            metrics: object,
            width: int,
        ) -> tuple[str, ...]:
            prefix = "缺少："
            payload = text[len(prefix) :]
            items = []
            item_start = 0
            for position, character in enumerate(payload, start=1):
                if character != "；":
                    continue
                items.append(payload[item_start:position])
                item_start = position
            if item_start < len(payload):
                items.append(payload[item_start:])
            if not items:
                return (prefix,)
            lines = []
            current = prefix
            for token in items:
                if metrics.horizontalAdvance(token) > width:
                    if current and current != prefix:
                        lines.append(current)
                    wrapped = cls._wrap_overwide_field(
                        (prefix if current == prefix else "") + token,
                        metrics,
                        width,
                    )
                    lines.extend(wrapped[:-1])
                    current = wrapped[-1]
                    continue
                candidate = current + token
                if metrics.horizontalAdvance(candidate) > width:
                    lines.append(current)
                    current = token
                else:
                    current = candidate
            lines.append(current)
            return tuple(line for line in lines if line)

        @classmethod
        def _semantic_lines(
            cls,
            text: str,
            metrics: object,
            width: int,
        ) -> tuple[str, ...]:
            lines = []
            for source_line in tuple(text.splitlines()) or ("",):
                if source_line.startswith("缺少："):
                    lines.extend(
                        cls._atomic_missing_lines(
                            source_line,
                            metrics,
                            max(1, width),
                        )
                    )
                    continue
                lines.extend(
                    cls._wrap_overwide_field(
                        source_line,
                        metrics,
                        max(1, width),
                    )
                )
            return tuple(lines) or ("",)

        def _cached_semantic_lines(
            self,
            text: str,
            metrics: object,
            width: int,
            font_key: str,
        ) -> tuple[str, ...]:
            cache_key = (text, max(1, width), font_key)
            cached = self._semantic_line_cache.get(cache_key)
            if cached is not None:
                return cached
            lines = self._semantic_lines(text, metrics, max(1, width))
            if len(self._semantic_line_cache) >= 4096:
                self._semantic_line_cache.clear()
            self._semantic_line_cache[cache_key] = lines
            return lines

        def sizeHint(self, option: object, index: object) -> object:
            size = super().sizeHint(option, index)
            styled = qt_widgets.QStyleOptionViewItem(option)
            self.initStyleOption(styled, index)
            text = styled.text
            styled.text = ""
            item = view.itemFromIndex(index)
            item_width = (
                max(1, view.viewport().width() - view.indentation())
                if (
                    index.column() == 0
                    and item is not None
                    and hasattr(item, "isFirstColumnSpanned")
                    and item.isFirstColumnSpanned()
                )
                else view.columnWidth(index.column())
            )
            styled.rect = qt_core.QRect(
                0,
                0,
                item_width,
                max(minimum_height, styled.rect.height()),
            )
            _style, text_rect = self._style_and_text_rect(styled)
            lines = self._cached_semantic_lines(
                text,
                styled.fontMetrics,
                text_rect.width(),
                styled.font.key(),
            )
            text_height = (
                styled.fontMetrics.lineSpacing() * len(lines)
                + self._line_gap * max(0, len(lines) - 1)
            )
            size.setWidth(item_width)
            size.setHeight(
                max(
                    minimum_height,
                    text_height + self._vertical_padding,
                )
            )
            return size

        def paint(
            self,
            painter: object,
            option: object,
            index: object,
        ) -> None:
            styled = qt_widgets.QStyleOptionViewItem(option)
            self.initStyleOption(styled, index)
            text = styled.text
            styled.text = ""
            style, text_rect = self._style_and_text_rect(styled)
            style.drawControl(
                qt_widgets.QStyle.ControlElement.CE_ItemViewItem,
                styled,
                painter,
                styled.widget,
            )
            lines = self._cached_semantic_lines(
                text,
                styled.fontMetrics,
                text_rect.width(),
                styled.font.key(),
            )
            text_height = (
                styled.fontMetrics.lineSpacing() * len(lines)
                + self._line_gap * max(0, len(lines) - 1)
            )
            baseline = text_rect.top() + styled.fontMetrics.ascent()
            if not (
                styled.displayAlignment
                & qt_core.Qt.AlignmentFlag.AlignTop
            ):
                baseline += max(
                    0,
                    (text_rect.height() - text_height) // 2,
                )
            color_group = (
                qt_gui.QPalette.ColorGroup.Disabled
                if not (
                    styled.state
                    & qt_widgets.QStyle.StateFlag.State_Enabled
                )
                else qt_gui.QPalette.ColorGroup.Active
            )
            color_role = (
                qt_gui.QPalette.ColorRole.HighlightedText
                if styled.state
                & qt_widgets.QStyle.StateFlag.State_Selected
                else qt_gui.QPalette.ColorRole.Text
            )
            painter.save()
            painter.setFont(styled.font)
            painter.setClipRect(text_rect)
            painter.setPen(
                styled.palette.color(color_group, color_role)
            )
            for line in lines:
                painter.drawText(text_rect.left(), baseline, line)
                baseline += (
                    styled.fontMetrics.lineSpacing() + self._line_gap
                )
            painter.restore()
            if divider_role is None or not index.data(divider_role):
                return
            painter.save()
            painter.setPen(qt_gui.QPen(qt_gui.QColor("#b8c5c1"), 1))
            painter.drawLine(
                option.rect.topLeft(),
                option.rect.topRight(),
            )
            painter.restore()

    return SemanticTextDelegate(view)


def _final_review_conflict_target_width(
    available_width: int,
    preferred_width: int,
) -> int:
    screen_bound = max(1, round(available_width * 0.66))
    upper_bound = min(screen_bound, 1120)
    lower_bound = min(720, upper_bound)
    return max(
        lower_bound,
        min(max(1, preferred_width), upper_bound),
    )


def show_final_review_dialog(
    request: FinalReviewDialogRequest,
    *,
    parent: object | None = None,
) -> DialogResponse:
    qt_widgets, qt_core = _load_qt_modules()
    qt_gui = _load_qt_gui()
    _ensure_application(qt_widgets)
    dialog_parent = parent or qt_widgets.QApplication.activeWindow()

    dialog = qt_widgets.QDialog(dialog_parent)
    dialog.setObjectName("organizer_dialog")
    dialog.setWindowTitle(request.title)
    dialog.setModal(True)
    flags = (
        qt_core.Qt.WindowType.Window
        | qt_core.Qt.WindowType.FramelessWindowHint
    )
    if request.topmost:
        flags |= qt_core.Qt.WindowType.WindowStaysOnTopHint
    dialog.setWindowFlags(flags)
    dialog.setAttribute(
        qt_core.Qt.WidgetAttribute.WA_DeleteOnClose,
        False,
    )
    apply_styled_dialog_chrome(dialog, qt_core)
    dialog.setStyleSheet(ORGANIZER_DIALOG_STYLE_SHEET)
    dialog.setFont(_font(qt_gui, 13))
    available = dialog.screen().availableGeometry()
    screen_width_limit = max(1, available.width() - 24)
    metrics = dialog.fontMetrics()
    root_headers = (
        "来源文件",
        "原 Folder / Book",
        "最终样品归属",
        "结果 / 原因",
    )
    root_width_limits = (220, 300, 300, 380)
    root_columns = tuple(
        tuple(
            line
            for row in request.rows
            for line in (
                row.source_filename,
                f"{row.folder_path}\n{row.book_name}",
                row.attribution,
                row.result,
            )[column].splitlines()
        )
        for column in range(4)
    )
    content_width = 72 + sum(
        min(
            limit,
            max(
                metrics.horizontalAdvance(header),
                max(
                    (
                        metrics.horizontalAdvance(line)
                        for line in root_columns[column]
                    ),
                    default=0,
                ),
            )
            + 24,
        )
        for column, (header, limit) in enumerate(
            zip(root_headers, root_width_limits, strict=True)
        )
    )
    target_width = max(
        1,
        min(
            screen_width_limit,
            max(
                min(720, screen_width_limit),
                min(1280, content_width),
            ),
        ),
    )
    height_cap = max(1, round(available.height() * 0.84))
    target_height = max(
        1,
        min(
            height_cap,
            290 + 48 * min(max(len(request.rows), 1), 12),
        ),
    )
    dialog.resize(target_width, target_height)
    dialog.setMinimumSize(
        min(600, target_width),
        min(330, target_height),
    )
    dialog.setMaximumSize(available.size())

    root = qt_widgets.QVBoxLayout(dialog)
    root.setContentsMargins(0, 0, 0, 0)
    root.setSpacing(0)

    header = qt_widgets.QFrame(dialog)
    header.setObjectName("dialog_header")
    header.setFixedHeight(50)
    header_layout = qt_widgets.QHBoxLayout(header)
    header_layout.setContentsMargins(16, 0, 10, 0)
    header_layout.setSpacing(8)
    title = qt_widgets.QLabel(request.title, header)
    title.setObjectName("dialog_title")
    title.setFont(_font(qt_gui, 15, bold=True))
    close_button = qt_widgets.QPushButton("×", header)
    close_button.setObjectName("dialog_close_button")
    close_button.setFixedSize(28, 26)
    close_button.setFocusPolicy(qt_core.Qt.FocusPolicy.NoFocus)
    header_layout.addWidget(title, 1)
    header_layout.addWidget(close_button, 0)
    root.addWidget(header)

    body = qt_widgets.QFrame(dialog)
    body.setObjectName("dialog_body")
    body_layout = qt_widgets.QVBoxLayout(body)
    body_layout.setContentsMargins(18, 14, 18, 14)
    body_layout.setSpacing(10)

    review_panel = qt_widgets.QWidget(body)
    review_panel.setObjectName("final_review_root")
    review_layout = qt_widgets.QVBoxLayout(review_panel)
    review_layout.setContentsMargins(0, 0, 0, 0)
    review_layout.setSpacing(10)

    counts_layout = qt_widgets.QHBoxLayout()
    counts_layout.setSpacing(18)
    for object_suffix, label_text, value in zip(
        ("recognized", "rejected", "excluded", "accepted"),
        ("识别 Book", "拒绝", "排除", "接受"),
        request.counts,
        strict=True,
    ):
        label = qt_widgets.QLabel(
            f"{label_text}  {value}",
            body,
        )
        label.setObjectName(f"final_review_count_{object_suffix}")
        label.setFont(_font(qt_gui, 13, bold=True))
        counts_layout.addWidget(label)
    counts_layout.addStretch(1)
    review_layout.addLayout(counts_layout)

    tabs = qt_widgets.QTabWidget(body)
    tabs.setObjectName("final_review_tabs")
    tabs.tabBar().setObjectName("final_review_tabs_tabbar")
    attribution_tab = qt_widgets.QWidget(tabs)
    output_tab = qt_widgets.QWidget(tabs)
    tabs.addTab(attribution_tab, "最终归属")
    tabs.addTab(output_tab, "输出结构")
    review_layout.addWidget(tabs, 1)

    attribution_layout = qt_widgets.QVBoxLayout(attribution_tab)
    attribution_layout.setContentsMargins(10, 10, 10, 10)
    attribution_layout.setSpacing(8)
    search_layout = qt_widgets.QHBoxLayout()
    search_layout.setSpacing(6)
    search_layout.addStretch(1)
    search = qt_widgets.QLineEdit(attribution_tab)
    search.setObjectName("final_review_search")
    search.setPlaceholderText("搜索来源、Folder、Book、样品或原因")
    search.setClearButtonEnabled(True)
    search.setMaximumWidth(420)
    search.setFixedHeight(40)
    search.setFont(_font(qt_gui, 13))
    search_icon = qt_gui.QPixmap(16, 16)
    search_icon.fill(qt_core.Qt.GlobalColor.transparent)
    painter = qt_gui.QPainter(search_icon)
    painter.setRenderHint(qt_gui.QPainter.RenderHint.Antialiasing, True)
    painter.setPen(qt_gui.QPen(qt_gui.QColor("#64716f"), 1.6))
    painter.drawEllipse(qt_core.QRectF(2.0, 2.0, 8.0, 8.0))
    painter.drawLine(qt_core.QPointF(9.0, 9.0), qt_core.QPointF(14.0, 14.0))
    painter.end()
    search.addAction(
        qt_gui.QIcon(search_icon),
        qt_widgets.QLineEdit.ActionPosition.LeadingPosition,
    )
    search_count = qt_widgets.QLabel("0 / 0", attribution_tab)
    search_count.setObjectName("final_review_search_count")
    search_count.setAlignment(qt_core.Qt.AlignmentFlag.AlignCenter)
    search_count.setFixedSize(72, 40)
    search_count_font = _font(qt_gui, 13)
    search_count_font.setFamily("Consolas")
    search_count.setFont(search_count_font)

    def chevron_icon(*, points: tuple[tuple[int, int], ...]):
        pixmap = qt_gui.QPixmap(14, 14)
        pixmap.fill(qt_core.Qt.GlobalColor.transparent)
        icon_painter = qt_gui.QPainter(pixmap)
        icon_painter.setRenderHint(
            qt_gui.QPainter.RenderHint.Antialiasing,
            True,
        )
        pen = qt_gui.QPen(qt_gui.QColor("#53615f"), 1.8)
        pen.setCapStyle(qt_core.Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(qt_core.Qt.PenJoinStyle.RoundJoin)
        icon_painter.setPen(pen)
        icon_painter.drawPolyline(
            qt_gui.QPolygonF(
                [qt_core.QPointF(x, y) for x, y in points]
            )
        )
        icon_painter.end()
        return qt_gui.QIcon(pixmap)

    search_up = qt_widgets.QToolButton(attribution_tab)
    search_up.setObjectName("final_review_search_up")
    search_up.setIcon(
        chevron_icon(points=((2, 9), (7, 4), (12, 9)))
    )
    search_up.setIconSize(qt_core.QSize(14, 14))
    search_up.setFixedSize(40, 40)
    search_up.setToolTip("上一个匹配项")
    search_up.setAccessibleName("上一个匹配项")
    search_down = qt_widgets.QToolButton(attribution_tab)
    search_down.setObjectName("final_review_search_down")
    search_down.setIcon(
        chevron_icon(points=((2, 5), (7, 10), (12, 5)))
    )
    search_down.setIconSize(qt_core.QSize(14, 14))
    search_down.setFixedSize(40, 40)
    search_down.setToolTip("下一个匹配项")
    search_down.setAccessibleName("下一个匹配项")
    search_layout.addWidget(search)
    search_layout.addWidget(search_count)
    search_layout.addWidget(search_up)
    search_layout.addWidget(search_down)
    attribution_layout.addLayout(search_layout)

    table = qt_widgets.QTableWidget(len(request.rows), 4, attribution_tab)
    table.setObjectName("final_review_table")
    table.setHorizontalHeaderLabels(
        ("来源文件", "原 Folder / Book", "最终样品归属", "结果 / 原因")
    )
    table.verticalHeader().setVisible(False)
    table.horizontalHeader().setStretchLastSection(False)
    for column in range(4):
        table.horizontalHeader().setSectionResizeMode(
            column,
            qt_widgets.QHeaderView.ResizeMode.Stretch,
        )
    table.setSelectionBehavior(
        qt_widgets.QAbstractItemView.SelectionBehavior.SelectRows
    )
    table.setSelectionMode(
        qt_widgets.QAbstractItemView.SelectionMode.SingleSelection
    )
    table.setEditTriggers(
        qt_widgets.QAbstractItemView.EditTrigger.NoEditTriggers
    )
    table.setWordWrap(True)
    table.setTextElideMode(qt_core.Qt.TextElideMode.ElideNone)
    table.setHorizontalScrollBarPolicy(
        qt_core.Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    )
    table.setVerticalScrollMode(
        qt_widgets.QAbstractItemView.ScrollMode.ScrollPerPixel
    )
    table.verticalHeader().setDefaultSectionSize(48)
    source_divider_role = int(qt_core.Qt.ItemDataRole.UserRole) + 1
    table.setItemDelegate(
        _create_semantic_text_delegate(
            qt_widgets,
            qt_core,
            qt_gui,
            table,
            minimum_height=48,
            divider_role=source_divider_role,
        )
    )
    row_ids = []
    searchable_rows = []
    for row_index, row in enumerate(request.rows):
        row_ids.append(row.row_id)
        values = (
            row.source_filename,
            f"{row.folder_path}\n{row.book_name}",
            row.attribution,
            row.result,
        )
        searchable_rows.append("\n".join(values).casefold())
        for column, value in enumerate(values):
            item = qt_widgets.QTableWidgetItem(value)
            item.setFlags(
                qt_core.Qt.ItemFlag.ItemIsEnabled
                | qt_core.Qt.ItemFlag.ItemIsSelectable
            )
            item.setTextAlignment(
                int(
                    qt_core.Qt.AlignmentFlag.AlignLeft
                    | qt_core.Qt.AlignmentFlag.AlignVCenter
                )
            )
            item.setData(
                qt_core.Qt.ItemDataRole.UserRole,
                row.row_id,
            )
            item.setData(
                source_divider_role,
                row_index > 0
                and row.source_filename
                != request.rows[row_index - 1].source_filename,
            )
            table.setItem(row_index, column, item)
    table_row_fit = {
        "next_row": 0,
        "scheduled": False,
        "restore_row": None,
    }

    def fit_table_row_batch() -> None:
        table_row_fit["scheduled"] = False
        if not dialog.isVisible():
            return
        start = table_row_fit["next_row"]
        stop = min(table.rowCount(), start + 8)
        for row_index in range(start, stop):
            table.resizeRowToContents(row_index)
        table_row_fit["next_row"] = stop
        if isinstance(table_row_fit["restore_row"], int):
            table.scrollToItem(
                table.item(table_row_fit["restore_row"], 0),
                qt_widgets.QAbstractItemView.ScrollHint.EnsureVisible,
            )
        if stop < table.rowCount():
            table_row_fit["scheduled"] = True
            qt_core.QTimer.singleShot(0, fit_table_row_batch)

    def schedule_table_row_fit() -> None:
        table_row_fit["next_row"] = 0
        if table_row_fit["scheduled"]:
            return
        table_row_fit["scheduled"] = True
        qt_core.QTimer.singleShot(0, fit_table_row_batch)

    table.horizontalHeader().sectionResized.connect(
        lambda _section, _old, _new: schedule_table_row_fit()
    )

    def clear_table_restore_row(*_args: object) -> None:
        table_row_fit["restore_row"] = None

    def clear_changed_table_restore_row() -> None:
        restore_row = table_row_fit["restore_row"]
        if isinstance(restore_row, int) and table.currentRow() != restore_row:
            clear_table_restore_row()

    table.verticalScrollBar().actionTriggered.connect(
        clear_table_restore_row
    )
    table.itemSelectionChanged.connect(clear_changed_table_restore_row)
    table.clearSelection()
    table.setCurrentCell(-1, -1)
    attribution_layout.addWidget(table, 1)

    output_layout = qt_widgets.QVBoxLayout(output_tab)
    output_layout.setContentsMargins(10, 10, 10, 10)
    output_tree = qt_widgets.QTreeWidget(output_tab)
    output_tree.setObjectName("final_review_output_tree")
    output_tree.setColumnCount(4)
    output_headers = ("Folder", "Book", "列顺序", "完整性")
    output_tree.setHeaderLabels(output_headers)
    output_tree.setEditTriggers(
        qt_widgets.QAbstractItemView.EditTrigger.NoEditTriggers
    )
    output_tree.setSelectionMode(
        qt_widgets.QAbstractItemView.SelectionMode.NoSelection
    )
    output_tree.setWordWrap(True)
    output_tree.setTextElideMode(qt_core.Qt.TextElideMode.ElideNone)
    output_tree.setHorizontalScrollBarPolicy(
        qt_core.Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    )
    output_tree.setVerticalScrollMode(
        qt_widgets.QAbstractItemView.ScrollMode.ScrollPerPixel
    )
    output_tree.setUniformRowHeights(False)
    output_header = output_tree.header()
    output_header.setStretchLastSection(False)
    for column in (0, 1, 3):
        output_header.setSectionResizeMode(
            column,
            qt_widgets.QHeaderView.ResizeMode.Fixed,
        )
    output_header.setSectionResizeMode(
        2,
        qt_widgets.QHeaderView.ResizeMode.Stretch,
    )
    output_folder_identity_role = int(qt_core.Qt.ItemDataRole.UserRole) + 2
    collapsed = set(request.initial_view_state.collapsed_output_folders)
    for folder in request.output_folders:
        completeness_status = (
            "完整"
            if not folder.missing_items
            else f"缺少 {len(folder.missing_items)} 项"
        )
        folder_item = qt_widgets.QTreeWidgetItem(
            (
                f"{folder.folder_name} · {completeness_status}",
                "",
                "",
                completeness_status,
            )
        )
        folder_item.setFlags(qt_core.Qt.ItemFlag.ItemIsEnabled)
        folder_item.setData(
            0,
            output_folder_identity_role,
            folder.folder_name,
        )
        font = folder_item.font(0)
        font.setWeight(qt_gui.QFont.Weight.DemiBold)
        folder_item.setFont(0, font)
        folder_item.setBackground(0, qt_gui.QColor("#eaf3f0"))
        folder_item.setForeground(0, qt_gui.QColor("#0f655c"))
        output_tree.addTopLevelItem(folder_item)
        folder_item.setFirstColumnSpanned(True)
        if folder.missing_items:
            audit_item = qt_widgets.QTreeWidgetItem(
                ("缺少：" + "；".join(folder.missing_items), "", "", "")
            )
            audit_item.setFlags(qt_core.Qt.ItemFlag.ItemIsEnabled)
            audit_item.setBackground(0, qt_gui.QColor("#fff3d6"))
            audit_item.setForeground(0, qt_gui.QColor("#8a5a00"))
            folder_item.addChild(audit_item)
            audit_item.setFirstColumnSpanned(True)
        for book in folder.books:
            book_item = qt_widgets.QTreeWidgetItem(
                ("", book.book_name, "\n".join(book.column_order), "")
            )
            book_item.setFlags(qt_core.Qt.ItemFlag.ItemIsEnabled)
            for column in (1, 2):
                book_item.setTextAlignment(
                    column,
                    int(
                        qt_core.Qt.AlignmentFlag.AlignLeft
                        | qt_core.Qt.AlignmentFlag.AlignTop
                    ),
                )
            folder_item.addChild(book_item)
        folder_item.setExpanded(folder.folder_name not in collapsed)

    output_tree.setItemDelegate(
        _create_semantic_text_delegate(
            qt_widgets,
            qt_core,
            qt_gui,
            output_tree,
            minimum_height=36,
        )
    )

    output_items = tuple(
        item
        for folder_index in range(output_tree.topLevelItemCount())
        for folder_item in (output_tree.topLevelItem(folder_index),)
        for item in (
            folder_item,
            *tuple(
                folder_item.child(child_index)
                for child_index in range(folder_item.childCount())
            ),
        )
    )

    metrics = output_tree.fontMetrics()

    def required_output_width(column: int) -> int:
        semantic_units = []
        for item in output_items:
            if item.isFirstColumnSpanned():
                continue
            semantic_units.extend(item.text(column).splitlines())
        return max(
            metrics.horizontalAdvance(output_headers[column]),
            max(
                (
                    metrics.horizontalAdvance(line)
                    for line in semantic_units
                ),
                default=0,
            ),
        ) + (36 if column == 0 else 22)

    required_output_widths = {
        column: required_output_width(column)
        for column in (0, 1, 3)
    }
    output_layout_state = {"applying": False}
    output_layout_timer = qt_core.QTimer(output_tree)
    output_layout_timer.setSingleShot(True)
    output_layout_timer.setInterval(80)
    output_tree._final_review_output_layout_timer = output_layout_timer

    def apply_output_column_widths() -> None:
        viewport_width = max(1, output_tree.viewport().width())

        fixed_widths = {
            0: min(
                required_output_widths[0],
                max(56, round(viewport_width * 0.16)),
            ),
            1: min(
                required_output_widths[1],
                max(88, round(viewport_width * 0.30)),
            ),
            3: min(
                required_output_widths[3],
                max(68, round(viewport_width * 0.12)),
            ),
        }
        output_layout_state["applying"] = True
        try:
            for column, width in fixed_widths.items():
                output_tree.setColumnWidth(column, width)
        finally:
            output_layout_state["applying"] = False

    def arrange_output_columns() -> None:
        apply_output_column_widths()
        output_layout_state["applying"] = True
        try:
            output_tree.doItemsLayout()
        finally:
            output_layout_state["applying"] = False

    def schedule_output_layout(*_args: object) -> None:
        if not output_layout_state["applying"]:
            output_layout_timer.start()

    output_layout_timer.timeout.connect(arrange_output_columns)
    output_tree.header().sectionResized.connect(schedule_output_layout)

    original_output_resize_event = output_tree.resizeEvent

    def output_resize_event(event: object) -> None:
        original_output_resize_event(event)
        schedule_output_layout()

    output_tree.resizeEvent = output_resize_event
    qt_core.QTimer.singleShot(
        0,
        lambda: (
            schedule_table_row_fit(),
            schedule_output_layout(),
        ),
    )
    output_layout.addWidget(output_tree)

    def output_folder_identity(item: object) -> str:
        return str(item.data(0, output_folder_identity_role))

    footer = qt_widgets.QFrame(body)
    footer.setObjectName("conflict_review_footer")
    footer_layout = qt_widgets.QVBoxLayout(footer)
    footer_layout.setContentsMargins(0, 10, 0, 0)
    footer_layout.setSpacing(8)
    root_wide_row = qt_widgets.QWidget(footer)
    root_wide_layout = qt_widgets.QHBoxLayout(root_wide_row)
    root_wide_layout.setContentsMargins(0, 0, 0, 0)
    root_wide_layout.setSpacing(8)
    root_compact_modify_row = qt_widgets.QWidget(footer)
    root_compact_modify_layout = qt_widgets.QHBoxLayout(
        root_compact_modify_row
    )
    root_compact_modify_layout.setContentsMargins(0, 0, 0, 0)
    root_compact_modify_layout.setSpacing(8)
    root_compact_decision_row = qt_widgets.QWidget(footer)
    root_compact_decision_layout = qt_widgets.QHBoxLayout(
        root_compact_decision_row
    )
    root_compact_decision_layout.setContentsMargins(0, 0, 0, 0)
    root_compact_decision_layout.setSpacing(8)
    footer_layout.addWidget(root_wide_row)
    footer_layout.addWidget(root_compact_modify_row)
    footer_layout.addWidget(root_compact_decision_row)
    modify_attribution = qt_widgets.QPushButton(
        "修改样品归属",
        footer,
    )
    modify_attribution.setObjectName("final_review_modify_attribution")
    modify_attribution.setProperty("choice_action", True)
    configure_workflow_button(modify_attribution, qt_gui)
    modify_conflicts = qt_widgets.QPushButton(
        "修改冲突选择",
        footer,
    )
    modify_conflicts.setObjectName("final_review_modify_conflicts")
    modify_conflicts.setProperty("choice_action", True)
    configure_workflow_button(modify_conflicts, qt_gui)
    confirm = qt_widgets.QPushButton(
        "确认并冻结本次审核",
        footer,
    )
    confirm.setObjectName("final_review_confirm")
    configure_workflow_button(confirm, qt_gui)
    cancel = qt_widgets.QPushButton("取消并退出", footer)
    cancel.setObjectName("final_review_cancel")
    configure_workflow_button(cancel, qt_gui)

    def clear_root_action_layout(layout: object) -> None:
        while layout.count():
            layout.takeAt(0)

    def arrange_root_footer(width: int) -> None:
        for layout in (
            root_wide_layout,
            root_compact_modify_layout,
            root_compact_decision_layout,
        ):
            clear_root_action_layout(layout)
        root_buttons = (
            modify_attribution,
            modify_conflicts,
            confirm,
            cancel,
        )
        required_width = (
            sum(button.sizeHint().width() for button in root_buttons)
            + root_wide_layout.spacing() * (len(root_buttons) - 1)
            + body_layout.contentsMargins().left()
            + body_layout.contentsMargins().right()
        )
        if width >= required_width:
            root_wide_layout.addWidget(modify_attribution)
            root_wide_layout.addWidget(modify_conflicts)
            root_wide_layout.addStretch(1)
            root_wide_layout.addWidget(confirm)
            root_wide_layout.addWidget(cancel)
            root_wide_row.show()
            root_compact_modify_row.hide()
            root_compact_decision_row.hide()
            return
        root_compact_modify_layout.addWidget(modify_attribution)
        root_compact_modify_layout.addWidget(modify_conflicts)
        root_compact_modify_layout.addStretch(1)
        root_compact_decision_layout.addStretch(1)
        root_compact_decision_layout.addWidget(confirm)
        root_compact_decision_layout.addWidget(cancel)
        root_wide_row.hide()
        root_compact_modify_row.show()
        root_compact_decision_row.show()

    arrange_root_footer(dialog.width())
    review_layout.addWidget(footer)

    conflict_editor = qt_widgets.QWidget(body)
    conflict_editor.setObjectName("final_review_conflict_editor")
    conflict_layout = qt_widgets.QVBoxLayout(conflict_editor)
    conflict_layout.setContentsMargins(0, 0, 0, 0)
    conflict_layout.setSpacing(10)
    conflict_instruction = qt_widgets.QLabel(conflict_editor)
    conflict_instruction.setObjectName("final_review_conflict_instruction")
    conflict_instruction.setWordWrap(True)
    conflict_layout.addWidget(conflict_instruction)
    conflict_scroll = qt_widgets.QScrollArea(conflict_editor)
    conflict_scroll.setObjectName("final_review_conflict_scroll")
    conflict_scroll.setWidgetResizable(True)
    conflict_scroll.setHorizontalScrollBarPolicy(
        qt_core.Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    )
    conflict_container = qt_widgets.QWidget(conflict_scroll)
    conflict_groups_layout = qt_widgets.QVBoxLayout(conflict_container)
    conflict_groups_layout.setContentsMargins(0, 0, 0, 0)
    conflict_groups_layout.setSpacing(10)
    conflict_groups_layout.addStretch(1)
    conflict_scroll.setWidget(conflict_container)
    conflict_layout.addWidget(conflict_scroll, 1)
    conflict_footer_frame = qt_widgets.QFrame(conflict_editor)
    conflict_footer_frame.setObjectName("final_review_conflict_footer")
    conflict_footer = qt_widgets.QVBoxLayout(conflict_footer_frame)
    conflict_footer.setContentsMargins(0, 10, 0, 0)
    conflict_footer.setSpacing(8)
    conflict_wide_row = qt_widgets.QWidget(conflict_footer_frame)
    conflict_wide_layout = qt_widgets.QHBoxLayout(conflict_wide_row)
    conflict_wide_layout.setContentsMargins(0, 0, 0, 0)
    conflict_wide_layout.setSpacing(8)
    conflict_compact_mode_row = qt_widgets.QWidget(
        conflict_footer_frame
    )
    conflict_compact_mode_layout = qt_widgets.QHBoxLayout(
        conflict_compact_mode_row
    )
    conflict_compact_mode_layout.setContentsMargins(0, 0, 0, 0)
    conflict_compact_mode_layout.setSpacing(8)
    conflict_compact_workflow_row = qt_widgets.QWidget(
        conflict_footer_frame
    )
    conflict_compact_workflow_layout = qt_widgets.QHBoxLayout(
        conflict_compact_workflow_row
    )
    conflict_compact_workflow_layout.setContentsMargins(0, 0, 0, 0)
    conflict_compact_workflow_layout.setSpacing(8)
    conflict_footer.addWidget(conflict_wide_row)
    conflict_footer.addWidget(conflict_compact_mode_row)
    conflict_footer.addWidget(conflict_compact_workflow_row)
    conflict_decision_buttons = {}
    for decision, label in (
        ("confirm_group", "整组确认"),
        ("confirm_selection", "逐 Book 确认"),
        ("reject_group", "整组拒绝"),
    ):
        button = qt_widgets.QPushButton(label, conflict_footer_frame)
        button.setObjectName("final_review_conflict_decision")
        button.setCheckable(True)
        button.setProperty("choice_item", True)
        configure_workflow_button(button, qt_gui)
        button.hide()
        conflict_decision_buttons[decision] = button
    conflict_back = qt_widgets.QPushButton(
        "返回上一步",
        conflict_footer_frame,
    )
    conflict_back.setObjectName("final_review_conflict_back")
    configure_workflow_button(conflict_back, qt_gui)
    conflict_confirm = qt_widgets.QPushButton(
        "确认修改",
        conflict_footer_frame,
    )
    conflict_confirm.setObjectName("final_review_conflict_confirm")
    configure_workflow_button(conflict_confirm, qt_gui)
    conflict_cancel = qt_widgets.QPushButton(
        "取消并退出",
        conflict_footer_frame,
    )
    conflict_cancel.setObjectName("final_review_conflict_cancel")
    configure_workflow_button(conflict_cancel, qt_gui)
    for button in (
        modify_attribution,
        modify_conflicts,
        confirm,
        cancel,
        *conflict_decision_buttons.values(),
        conflict_back,
        conflict_confirm,
        conflict_cancel,
    ):
        button.setAutoDefault(False)
        button.setDefault(False)

    def clear_conflict_action_layout(layout: object) -> None:
        while layout.count():
            layout.takeAt(0)

    def arrange_conflict_footer(width: int) -> None:
        for layout in (
            conflict_wide_layout,
            conflict_compact_mode_layout,
            conflict_compact_workflow_layout,
        ):
            clear_conflict_action_layout(layout)
        if width >= 700:
            for button in conflict_decision_buttons.values():
                conflict_wide_layout.addWidget(button)
            conflict_wide_layout.addStretch(1)
            conflict_wide_layout.addWidget(conflict_back)
            conflict_wide_layout.addWidget(conflict_confirm)
            conflict_wide_layout.addWidget(conflict_cancel)
            conflict_wide_row.show()
            conflict_compact_mode_row.hide()
            conflict_compact_workflow_row.hide()
            return
        for button in conflict_decision_buttons.values():
            conflict_compact_mode_layout.addWidget(button)
        conflict_compact_mode_layout.addStretch(1)
        conflict_compact_workflow_layout.addStretch(1)
        conflict_compact_workflow_layout.addWidget(conflict_back)
        conflict_compact_workflow_layout.addWidget(conflict_confirm)
        conflict_compact_workflow_layout.addWidget(conflict_cancel)
        conflict_wide_row.hide()
        conflict_compact_mode_row.show()
        conflict_compact_workflow_row.show()

    arrange_conflict_footer(dialog.width())
    conflict_layout.addWidget(conflict_footer_frame)

    body_layout.addWidget(review_panel, 1)
    body_layout.addWidget(conflict_editor, 1)
    conflict_editor.hide()
    root.addWidget(body, 1)

    class FinalReviewSizeGrip(qt_widgets.QSizeGrip):
        def mousePressEvent(self, event: object) -> None:
            self._final_review_drag_active = True
            conflict_resize_state["user_resized"] = True
            super().mousePressEvent(event)

        def mouseReleaseEvent(self, event: object) -> None:
            super().mouseReleaseEvent(event)
            if not getattr(self, "_final_review_drag_active", False):
                return
            size_key = (
                "final_review_conflict"
                if conflict_resize_state["active"]
                else "final_review"
            )
            _remember_dialog_size(dialog, size_key, qt_core)
            self._final_review_drag_active = False

        def paintEvent(self, event: object) -> None:
            del event
            painter = qt_gui.QPainter(self)
            painter.setRenderHint(
                qt_gui.QPainter.RenderHint.Antialiasing,
                True,
            )
            painter.setPen(qt_gui.QPen(qt_gui.QColor("#6c827e"), 1.2))
            for inset in (4, 8, 12):
                painter.drawLine(
                    self.width() - inset,
                    self.height() - 2,
                    self.width() - 2,
                    self.height() - inset,
                )

    size_grip = FinalReviewSizeGrip(dialog)
    size_grip.setObjectName("final_review_size_grip")
    size_grip.setFixedSize(18, 18)
    size_grip.setToolTip("拖动调整窗口大小")
    dialog._final_review_size_grip = size_grip

    def place_size_grip() -> None:
        size_grip.move(
            dialog.width() - size_grip.width(),
            dialog.height() - size_grip.height(),
        )
        size_grip.raise_()

    conflict_resize_state = {
        "active": False,
        "programmatic": False,
        "user_resized": False,
        "refit": None,
    }
    original_dialog_resize_event = dialog.resizeEvent

    def refit_after_user_resize() -> None:
        callback = conflict_resize_state["refit"]
        if conflict_resize_state["active"] and callable(callback):
            callback()

    conflict_resize_timer = qt_core.QTimer(dialog)
    conflict_resize_timer.setSingleShot(True)
    conflict_resize_timer.setInterval(80)
    conflict_resize_timer.timeout.connect(refit_after_user_resize)
    dialog._final_review_conflict_resize_timer = conflict_resize_timer

    def final_review_resize_event(event: object) -> None:
        original_dialog_resize_event(event)
        place_size_grip()
        arrange_root_footer(dialog.width())
        arrange_conflict_footer(dialog.width())
        if (
            not conflict_resize_state["active"]
            or conflict_resize_state["programmatic"]
            or not conflict_resize_state["user_resized"]
        ):
            return
        conflict_resize_timer.start()

    dialog.resizeEvent = final_review_resize_event
    place_size_grip()

    selected_response = {"value": None}
    provider_failure = {"error": None}
    matches = {"rows": [], "position": None}
    output_anchor_state = {
        "folder": request.initial_view_state.output_anchor_folder,
        "offset": request.initial_view_state.output_anchor_offset,
    }
    output_restore_pending = {
        "value": bool(
            request.initial_view_state.output_anchor_folder
            or request.initial_view_state.output_scroll_value
        )
    }
    active_tab_state = {"index": tabs.currentIndex()}
    conflict_state = {
        "active": False,
        "forced": False,
        "row_id": "",
        "draft": {},
        "pending_special": {},
        "editing_special": set(),
        "model": None,
        "rendering": False,
        "review_size": None,
        "review_minimum_size": None,
        "active_group_id": "",
        "group_frames": {},
        "provider_work": None,
        "pending_provider_request": None,
        "closing": False,
    }
    dialog.finished.connect(
        lambda _result: conflict_state.__setitem__("closing", True)
    )

    def selected_row_index() -> int:
        return table.currentRow()

    def update_modification_actions() -> None:
        row_index = selected_row_index()
        on_attribution_tab = tabs.currentIndex() == 0
        has_row = 0 <= row_index < len(request.rows)
        modify_attribution.setVisible(
            on_attribution_tab
            and has_row
            and request.rows[row_index].can_modify_attribution
        )
        modify_conflicts.setVisible(
            on_attribution_tab
            and has_row
            and request.rows[row_index].has_related_conflicts
        )

    def update_match_readout() -> None:
        match_rows = matches["rows"]
        position = matches["position"]
        if not match_rows:
            search_count.setText("0 / 0")
        elif position is None:
            search_count.setText(f"0 / {len(match_rows)}")
        else:
            search_count.setText(f"{position + 1} / {len(match_rows)}")
        enabled = bool(match_rows)
        search_up.setEnabled(enabled)
        search_down.setEnabled(enabled)

    def select_match(position: int) -> None:
        match_rows = matches["rows"]
        if not match_rows:
            matches["position"] = None
            update_match_readout()
            return
        normalized = position % len(match_rows)
        row_index = match_rows[normalized]
        matches["position"] = normalized
        table.setCurrentCell(row_index, 0)
        table.selectRow(row_index)
        table.scrollToItem(
            table.item(row_index, 0),
            qt_widgets.QAbstractItemView.ScrollHint.PositionAtCenter,
        )
        update_match_readout()

    def refresh_matches(text: str, *, jump: bool = True) -> None:
        query = text.strip().casefold()
        matches["rows"] = (
            []
            if not query
            else [
                index
                for index, searchable in enumerate(searchable_rows)
                if query in searchable
            ]
        )
        matches["position"] = None
        if jump and matches["rows"]:
            select_match(0)
        else:
            update_match_readout()

    def move_match(step: int) -> None:
        if not matches["rows"]:
            return
        position = matches["position"]
        if position is None:
            position = -1 if step > 0 else 0
        select_match(position + step)

    class SearchKeyFilter(qt_core.QObject):
        def eventFilter(self, watched: object, event: object) -> bool:
            if event.type() != qt_core.QEvent.Type.KeyPress:
                return False
            key = event.key()
            if key == qt_core.Qt.Key.Key_Down:
                move_match(1)
                return True
            if key == qt_core.Qt.Key.Key_Up:
                move_match(-1)
                return True
            if key in {
                qt_core.Qt.Key.Key_Return,
                qt_core.Qt.Key.Key_Enter,
            }:
                step = (
                    -1
                    if event.modifiers()
                    & qt_core.Qt.KeyboardModifier.ShiftModifier
                    else 1
                )
                move_match(step)
                return True
            return False

    class FinalReviewKeyFilter(qt_core.QObject):
        def eventFilter(self, watched: object, event: object) -> bool:
            if (
                event.type() == qt_core.QEvent.Type.KeyPress
                and event.key() == qt_core.Qt.Key.Key_Escape
            ):
                if conflict_state["active"]:
                    return_from_conflict_editor()
                else:
                    finish("cancel")
                return True
            return False

    def capture_output_view_anchor(*, allow_hidden: bool = False) -> None:
        if tabs.currentIndex() != 1 and not allow_hidden:
            return
        anchor_item = None
        for index in range(output_tree.topLevelItemCount()):
            item = output_tree.topLevelItem(index)
            rect = output_tree.visualItemRect(item)
            if rect.top() <= 0:
                anchor_item = item
                continue
            if anchor_item is None:
                anchor_item = item
            break
        if anchor_item is None:
            output_anchor_state.update(folder="", offset=0)
            return
        output_anchor_state.update(
            folder=output_folder_identity(anchor_item),
            offset=output_tree.visualItemRect(anchor_item).top(),
        )

    def output_view_anchor() -> tuple[str, int]:
        capture_output_view_anchor()
        return (
            str(output_anchor_state["folder"]),
            int(output_anchor_state["offset"]),
        )

    def restore_initial_output_view_position() -> None:
        if not output_restore_pending["value"] or tabs.currentIndex() != 1:
            return
        initial_state = request.initial_view_state
        output_tree.doItemsLayout()
        output_tree.verticalScrollBar().setValue(
            max(0, initial_state.output_scroll_value)
        )
        if initial_state.output_anchor_folder:
            anchor_item = next(
                (
                    output_tree.topLevelItem(index)
                    for index in range(output_tree.topLevelItemCount())
                    if output_folder_identity(
                        output_tree.topLevelItem(index)
                    )
                    == initial_state.output_anchor_folder
                ),
                None,
            )
            if anchor_item is not None:
                output_tree.scrollToItem(
                    anchor_item,
                    qt_widgets.QAbstractItemView.ScrollHint.PositionAtTop,
                )
                output_tree.doItemsLayout()
                current_top = output_tree.visualItemRect(anchor_item).top()
                output_tree.verticalScrollBar().setValue(
                    output_tree.verticalScrollBar().value()
                    + current_top
                    - initial_state.output_anchor_offset
                )
        output_restore_pending["value"] = False
        capture_output_view_anchor()

    def handle_tab_change(index: int) -> None:
        if active_tab_state["index"] == 1:
            capture_output_view_anchor(allow_hidden=True)
        active_tab_state["index"] = index
        if index == 1:
            apply_output_column_widths()
            schedule_output_layout()
            if output_restore_pending["value"]:
                qt_core.QTimer.singleShot(
                    0,
                    restore_initial_output_view_position,
                )
        update_modification_actions()

    def current_view_state() -> FinalReviewViewState:
        row_index = selected_row_index()
        selected_row_id = (
            request.rows[row_index].row_id
            if 0 <= row_index < len(request.rows)
            else ""
        )
        collapsed_folders = tuple(
            output_folder_identity(output_tree.topLevelItem(index))
            for index in range(output_tree.topLevelItemCount())
            if not output_tree.topLevelItem(index).isExpanded()
        )
        output_anchor_folder, output_anchor_offset = output_view_anchor()
        return FinalReviewViewState(
            active_tab=(
                "output" if tabs.currentIndex() == 1 else "attribution"
            ),
            search_text=search.text(),
            selected_row_id=selected_row_id,
            attribution_scroll_value=table.verticalScrollBar().value(),
            output_scroll_value=output_tree.verticalScrollBar().value(),
            output_anchor_folder=output_anchor_folder,
            output_anchor_offset=output_anchor_offset,
            collapsed_output_folders=collapsed_folders,
        )

    def clear_conflict_groups() -> None:
        while conflict_groups_layout.count() > 1:
            item = conflict_groups_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        conflict_state["group_frames"] = {}

    def refit_conflict_trees() -> None:
        if not conflict_state["active"]:
            return
        dialog.layout().activate()
        conflict_layout.activate()
        for index in range(conflict_groups_layout.count() - 1):
            frame = conflict_groups_layout.itemAt(index).widget()
            if frame is None:
                continue
            frame.layout().activate()
            for tree in frame.findChildren(
                qt_widgets.QTreeWidget,
                "final_review_conflict_choices",
            ):
                fit_tree = getattr(
                    tree,
                    "_fit_final_review_height",
                    None,
                )
                if callable(fit_tree):
                    fit_tree()
            frame.layout().activate()
        conflict_groups_layout.activate()

    conflict_resize_state["refit"] = refit_conflict_trees

    def programmatic_conflict_resize(width: int, height: int) -> None:
        conflict_resize_state["programmatic"] = True
        try:
            dialog.resize(width, height)
        finally:
            conflict_resize_state["programmatic"] = False

    def resize_for_conflict_editor() -> None:
        if not conflict_state["active"]:
            return
        available = dialog.screen().availableGeometry()
        target_width = dialog.width()
        if not conflict_resize_state["user_resized"]:
            preferred_width = max(
                (
                    int(
                        getattr(
                            tree,
                            "_final_review_preferred_dialog_width",
                            0,
                        )
                    )
                    for tree in conflict_editor.findChildren(
                        qt_widgets.QTreeWidget,
                        "final_review_conflict_choices",
                    )
                ),
                default=0,
            )
            preferred_width = max(
                preferred_width,
                conflict_footer_frame.sizeHint().width() + 40,
            )
            target_width = _final_review_conflict_target_width(
                available.width(),
                preferred_width,
            )
            programmatic_conflict_resize(
                target_width,
                dialog.height(),
            )
        conflict_resize_state["programmatic"] = True
        try:
            refit_conflict_trees()
        finally:
            conflict_resize_state["programmatic"] = False
        if conflict_resize_state["user_resized"]:
            return
        frame_heights = [
            frame.sizeHint().height()
            for index in range(conflict_groups_layout.count() - 1)
            for frame in (conflict_groups_layout.itemAt(index).widget(),)
            if frame is not None
        ]
        group_spacing = max(0, len(frame_heights) - 1) * (
            conflict_groups_layout.spacing()
        )
        groups_height = sum(frame_heights) + group_spacing
        visible_groups_height = min(
            groups_height,
            round(available.height() * 0.45),
        )
        body_margins = body_layout.contentsMargins()
        target_height = (
            header.height()
            + body_margins.top()
            + body_margins.bottom()
            + conflict_instruction.sizeHint().height()
            + visible_groups_height
            + conflict_footer.sizeHint().height()
            + conflict_layout.spacing() * 2
        )
        target_height = max(
            min(440, available.height()),
            min(target_height, round(available.height() * 0.66)),
        )
        dialog.setMinimumSize(
            min(520, target_width),
            min(420, target_height),
        )
        programmatic_conflict_resize(target_width, target_height)

    def conflict_scroll_anchor() -> tuple[str, int] | None:
        scroll_value = conflict_scroll.verticalScrollBar().value()
        for index in range(conflict_groups_layout.count() - 1):
            frame = conflict_groups_layout.itemAt(index).widget()
            if frame is None:
                continue
            frame_top = frame.mapTo(
                conflict_container,
                qt_core.QPoint(0, 0),
            ).y()
            if frame_top + frame.height() > scroll_value:
                return (
                    str(frame.property("group_id") or ""),
                    frame_top - scroll_value,
                )
        return None

    def restore_conflict_scroll_anchor(
        anchor: tuple[str, int] | None,
    ) -> None:
        if anchor is None:
            return
        group_id, offset = anchor
        for index in range(conflict_groups_layout.count() - 1):
            frame = conflict_groups_layout.itemAt(index).widget()
            if (
                frame is None
                or str(frame.property("group_id") or "") != group_id
            ):
                continue
            frame_top = frame.mapTo(
                conflict_container,
                qt_core.QPoint(0, 0),
            ).y()
            conflict_scroll.verticalScrollBar().setValue(
                frame_top - offset
            )
            return

    def conflict_draft_values() -> tuple[FinalReviewConflictSelection, ...]:
        return tuple(conflict_state["draft"].values())

    def conflict_pending_values() -> tuple[
        FinalReviewConflictSelection,
        ...,
    ]:
        return tuple(
            FinalReviewConflictSelection(
                group_id,
                selected_keys,
                "confirm_selection",
            )
            for group_id, selected_keys in conflict_state[
                "pending_special"
            ].items()
        )

    def conflict_editing_group_ids() -> tuple[str, ...]:
        model = conflict_state["model"]
        if not isinstance(model, FinalReviewConflictEditor):
            return ()
        editing = conflict_state["editing_special"]
        return tuple(
            group.group_id
            for group in model.groups
            if group.group_id in editing
        )

    def update_conflict_draft(
        group: FinalReviewConflictGroup,
        selected_keys: tuple[str, ...],
        *,
        decision: str | None = None,
        refresh: bool = True,
    ) -> None:
        if conflict_state["rendering"]:
            return
        current = conflict_state["draft"].get(
            group.group_id,
            FinalReviewConflictSelection(group.group_id),
        )
        conflict_state["draft"][group.group_id] = (
            FinalReviewConflictSelection(
                group.group_id,
                selected_keys,
                current.decision if decision is None else decision,
            )
        )
        if refresh:
            refresh_conflict_editor()

    def choose_conflict_decision(
        group: FinalReviewConflictGroup,
        decision: str,
    ) -> None:
        current = conflict_state["draft"].get(
            group.group_id,
            FinalReviewConflictSelection(group.group_id),
        )
        pending_special = conflict_state["pending_special"]
        editing_special = conflict_state["editing_special"]
        if (
            decision == "confirm_selection"
            and group.group_id not in editing_special
        ):
            editing_special.add(group.group_id)
            pending_special[group.group_id] = current.selected_keys
            render_conflict_editor(conflict_state["model"])
            return
        pending_selected_keys = pending_special.pop(
            group.group_id,
            current.selected_keys,
        )
        editing_special.discard(group.group_id)
        selected_keys = (
            pending_selected_keys
            if decision == "confirm_selection"
            else ()
        )
        update_conflict_draft(
            group,
            selected_keys,
            decision=decision,
        )

    def active_conflict_group() -> FinalReviewConflictGroup | None:
        model = conflict_state["model"]
        if not isinstance(model, FinalReviewConflictEditor):
            return None
        return next(
            (
                group
                for group in model.groups
                if group.group_id == conflict_state["active_group_id"]
            ),
            None,
        )

    def activate_conflict_group(group_id: str) -> None:
        conflict_state["active_group_id"] = group_id
        for current_id, current_frame in conflict_state[
            "group_frames"
        ].items():
            current_frame.setProperty(
                "active_group",
                current_id == group_id,
            )
            current_frame.style().unpolish(current_frame)
            current_frame.style().polish(current_frame)
        group = active_conflict_group()
        special_group = (
            group is not None
            and group.selection_mode == "special_group"
        )
        special_editing = (
            special_group
            and group.group_id in conflict_state["editing_special"]
        )
        draft = (
            conflict_state["draft"].get(group.group_id)
            if group is not None
            else None
        )
        active_decision = (
            "confirm_selection"
            if special_editing
            else (
                draft.decision
                if isinstance(draft, FinalReviewConflictSelection)
                else (group.decision if group is not None else "")
            )
        )
        for decision, button in conflict_decision_buttons.items():
            button.setVisible(special_group)
            button.setChecked(special_group and active_decision == decision)
            button.setEnabled(
                decision != "confirm_selection"
                or not special_editing
                or bool(
                    conflict_state["pending_special"].get(
                        group.group_id,
                        (),
                    )
                )
            )

    def choose_active_conflict_decision(decision: str) -> None:
        group = active_conflict_group()
        if group is None or group.selection_mode != "special_group":
            return
        choose_conflict_decision(group, decision)

    for decision, button in conflict_decision_buttons.items():
        button.clicked.connect(
            lambda _checked=False, value=decision: (
                choose_active_conflict_decision(value)
            )
        )

    def add_conflict_group(group: FinalReviewConflictGroup) -> None:
        frame = qt_widgets.QFrame(conflict_container)
        frame.setObjectName("final_review_conflict_group")
        frame.setProperty("group_id", group.group_id)
        frame.setProperty("selection_mode", group.selection_mode)
        frame.setProperty("active_group", False)
        conflict_state["group_frames"][group.group_id] = frame
        layout = qt_widgets.QVBoxLayout(frame)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(6)
        heading = qt_widgets.QLabel(group.title, frame)
        heading.setObjectName("final_review_conflict_group_title")
        heading.setFont(_font(qt_gui, 14, bold=True))

        def activate_current_group(event: object) -> None:
            activate_conflict_group(group.group_id)
            event.accept()

        heading.mousePressEvent = activate_current_group
        layout.addWidget(heading)
        warning_text = group.warning
        if group.stale_selected_keys or group.stale_decision:
            warning_text = warning_text or (
                "原选择（已失效）：上游选择已改变，请重新确认本组"
            )
        if warning_text:
            warning = qt_widgets.QLabel(warning_text, frame)
            warning.setObjectName("final_review_conflict_warning")
            warning.setWordWrap(True)
            layout.addWidget(warning)

        tree = qt_widgets.QTreeWidget(frame)
        tree.setObjectName("final_review_conflict_choices")
        tree.setProperty("group_id", group.group_id)
        tree.setColumnCount(2)
        tree.setHeaderLabels(("选择项", "关键差异"))
        tree.setRootIsDecorated(False)
        tree.setEditTriggers(
            qt_widgets.QAbstractItemView.EditTrigger.NoEditTriggers
        )
        tree.setSelectionBehavior(
            qt_widgets.QAbstractItemView.SelectionBehavior.SelectRows
        )
        if (
            group.selection_mode == "special_group"
            and group.group_id not in conflict_state["editing_special"]
        ):
            selection_mode = (
                qt_widgets.QAbstractItemView.SelectionMode.NoSelection
            )
        elif group.selection_mode == "single":
            selection_mode = (
                qt_widgets.QAbstractItemView.SelectionMode.SingleSelection
            )
        else:
            selection_mode = (
                qt_widgets.QAbstractItemView.SelectionMode.MultiSelection
            )
        tree.setSelectionMode(selection_mode)
        tree.setWordWrap(False)
        tree.setUniformRowHeights(False)
        tree.setMouseTracking(True)
        tree.setTextElideMode(qt_core.Qt.TextElideMode.ElideNone)
        tree.setHorizontalScrollBarPolicy(
            qt_core.Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        tree.setVerticalScrollBarPolicy(
            qt_core.Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        common_lines = frozenset(
            f"{label}：{value}"
            for label, value in group.common_fields
        )

        def visible_choice_detail(choice: object) -> str:
            return "\n".join(
                line
                for line in choice.detail.splitlines()
                if line and line not in common_lines
            ) or "仅 Book 名不同"

        candidate_field_matrix = tuple(
            tuple(
                field
                for field in visible_choice_detail(choice).splitlines()
                if field
            )
            or ("仅 Book 名不同",)
            for choice in (*group.choices, *group.stale_choices)
        )
        stale_role = qt_core.Qt.ItemDataRole.UserRole + 20
        tree.setItemDelegate(
            _create_conflict_item_delegate(
                qt_widgets,
                qt_core,
                qt_gui,
                tree,
                candidate_field_matrix,
                stale_role=stale_role,
            )
        )
        choice_by_key = {
            choice.choice_key: choice
            for choice in group.choices
        }
        stale_choice_by_key = {
            choice.choice_key: choice
            for choice in group.stale_choices
        }
        selected_keys = conflict_state["pending_special"].get(
            group.group_id,
            group.selected_keys,
        )
        for choice in group.choices:
            item = qt_widgets.QTreeWidgetItem(
                (
                    choice.display_name,
                    visible_choice_detail(choice),
                )
            )
            item.setData(
                0,
                qt_core.Qt.ItemDataRole.UserRole,
                choice.choice_key,
            )
            item.setFlags(
                qt_core.Qt.ItemFlag.ItemIsEnabled
                | qt_core.Qt.ItemFlag.ItemIsSelectable
            )
            tree.addTopLevelItem(item)
            if choice.choice_key in selected_keys:
                item.setSelected(True)
        for stale_key in group.stale_selected_keys:
            stale_choice = (
                stale_choice_by_key.get(stale_key)
                or choice_by_key.get(stale_key)
            )
            stale_label = (
                stale_choice.display_name
                if stale_choice is not None
                else "原选择详情不可用"
            )
            stale_detail = (
                visible_choice_detail(stale_choice)
                if stale_choice is not None
                else ""
            )
            item = qt_widgets.QTreeWidgetItem(
                (
                    f"原选择（已失效）：{stale_label}",
                    stale_detail,
                )
            )
            item.setFlags(qt_core.Qt.ItemFlag.NoItemFlags)
            item.setData(0, stale_role, True)
            item.setBackground(0, qt_gui.QColor("#fff3d6"))
            item.setBackground(1, qt_gui.QColor("#fff3d6"))
            item.setForeground(0, qt_gui.QColor("#8a5a00"))
            item.setForeground(1, qt_gui.QColor("#8a5a00"))
            tree.addTopLevelItem(item)

        metrics = tree.fontMetrics()
        separator_width = metrics.horizontalAdvance(
            _ConflictCellLayoutMixin._field_separator
        )
        preferred_detail_width = max(
            metrics.horizontalAdvance("关键差异") + 18,
            max(
                (
                    sum(
                        metrics.horizontalAdvance(field)
                        for field in fields
                    )
                    + separator_width * max(0, len(fields) - 1)
                    + 18
                    for fields in candidate_field_matrix
                ),
                default=0,
            ),
        )
        tree._final_review_preferred_dialog_width = (
            min(
                max(
                    _conflict_book_required_width(tree, "选择项"),
                    140,
                ),
                320,
            )
            + min(max(preferred_detail_width, 320), 700)
            + 96
        )

        def fit_tree_height() -> None:
            _arrange_conflict_columns(
                tree,
                qt_widgets,
                available_width=max(2, tree.viewport().width()),
                book_required_width=_conflict_book_required_width(
                    tree,
                    "选择项",
                ),
                detail_required_width=_conflict_detail_atomic_width(
                    tree,
                    candidate_field_matrix,
                ),
            )
            tree.doItemsLayout()
            tree.setFixedHeight(
                tree.header().sizeHint().height()
                + 2 * tree.frameWidth()
                + sum(
                    max(1, tree.sizeHintForRow(index))
                    for index in range(tree.topLevelItemCount())
                )
            )
            tree.doItemsLayout()

        tree._fit_final_review_height = fit_tree_height
        fit_tree_height()

        synchronizing_exact_group = {"active": False}

        def selection_changed(
            current_tree=tree,
            current_group=group,
        ) -> None:
            if synchronizing_exact_group["active"]:
                return
            if (
                current_group.selection_mode == "special_group"
                and current_group.group_id
                not in conflict_state["editing_special"]
            ):
                return
            activate_conflict_group(current_group.group_id)
            current_item = current_tree.currentItem()
            current_key = (
                str(
                    current_item.data(
                        0,
                        qt_core.Qt.ItemDataRole.UserRole,
                    )
                    or ""
                )
                if current_item is not None
                and current_item.isSelected()
                else ""
            )
            exact_group = next(
                (
                    keys
                    for keys in current_group.single_select_groups
                    if current_key in keys
                ),
                (),
            )
            if exact_group:
                synchronizing_exact_group["active"] = True
                try:
                    for index in range(current_tree.topLevelItemCount()):
                        item = current_tree.topLevelItem(index)
                        item_key = str(
                            item.data(
                                0,
                                qt_core.Qt.ItemDataRole.UserRole,
                            )
                            or ""
                        )
                        if (
                            item is not current_item
                            and item_key in exact_group
                        ):
                            item.setSelected(False)
                finally:
                    synchronizing_exact_group["active"] = False
            selected_keys = tuple(
                str(
                    item.data(
                        0,
                        qt_core.Qt.ItemDataRole.UserRole,
                    )
                )
                for index in range(current_tree.topLevelItemCount())
                for item in (current_tree.topLevelItem(index),)
                if item.isSelected()
                if item.data(
                    0,
                    qt_core.Qt.ItemDataRole.UserRole,
                )
                is not None
            )
            requires_explicit_decision = (
                current_group.selection_mode == "special_group"
            )
            if requires_explicit_decision:
                conflict_state["pending_special"][
                    current_group.group_id
                ] = selected_keys
                conflict_confirm.setEnabled(False)
                activate_conflict_group(current_group.group_id)
                return
            update_conflict_draft(
                current_group,
                selected_keys,
            )

        tree.itemSelectionChanged.connect(selection_changed)
        layout.addWidget(tree)
        common_detail, common_left, common_right = (
            _create_conflict_common_detail(
                qt_widgets,
                qt_core,
                frame,
            )
        )
        _set_conflict_common_detail(
            common_left,
            common_right,
            group.common_fields,
        )
        layout.addWidget(common_detail)
        conflict_groups_layout.insertWidget(
            conflict_groups_layout.count() - 1,
            frame,
        )

    def render_conflict_editor(
        model: FinalReviewConflictEditor,
    ) -> None:
        conflict_state["rendering"] = True
        try:
            active_group_id = conflict_state["active_group_id"]
            clear_conflict_groups()
            conflict_instruction.setText(model.instruction)
            conflict_state["model"] = model
            group_by_id = {
                group.group_id: group
                for group in model.groups
                if group.selection_mode == "special_group"
            }
            editing_special = conflict_state["editing_special"]
            editing_special.intersection_update(group_by_id)
            pending_special = conflict_state["pending_special"]
            for group_id in tuple(pending_special):
                if group_id not in editing_special:
                    pending_special.pop(group_id)
                    continue
                valid_choice_keys = {
                    choice.choice_key
                    for choice in group_by_id[group_id].choices
                }
                pending_special[group_id] = tuple(
                    key
                    for key in pending_special[group_id]
                    if key in valid_choice_keys
                )
            for group in model.groups:
                if group.group_id not in conflict_state["draft"]:
                    conflict_state["draft"][group.group_id] = (
                        FinalReviewConflictSelection(
                            group.group_id,
                            group.selected_keys
                            or group.stale_selected_keys,
                            group.decision or group.stale_decision,
                        )
                    )
                add_conflict_group(group)
            visible_group_ids = {
                group.group_id for group in model.groups
            }
            conflict_confirm.setEnabled(
                model.can_confirm
                and not any(
                    group_id in visible_group_ids
                    for group_id in conflict_state["pending_special"]
                )
            )
            if active_group_id not in visible_group_ids:
                active_group_id = (
                    model.groups[0].group_id if model.groups else ""
                )
            activate_conflict_group(active_group_id)
        finally:
            conflict_state["rendering"] = False
        qt_core.QTimer.singleShot(0, resize_for_conflict_editor)

    class ConflictProviderWork:
        def __init__(
            self,
            provider: object,
            row_id: str,
            selections: tuple[FinalReviewConflictSelection, ...],
            scroll_anchor: tuple[str, int] | None,
        ) -> None:
            self.provider = provider
            self.row_id = row_id
            self.selections = selections
            self.scroll_anchor = scroll_anchor
            self.result = None
            self.error = None

        def run(self) -> None:
            try:
                self.result = self.provider(
                    self.row_id,
                    self.selections,
                )
            except BaseException as exc:
                self.error = exc

            try:
                provider_bridge.completed.emit(self)
            except RuntimeError:
                pass

    class ConflictProviderBridge(qt_core.QObject):
        completed = qt_core.Signal(object)

    provider_bridge = ConflictProviderBridge(dialog)
    dialog._final_review_conflict_provider_bridge = provider_bridge

    def complete_provider_work(work: ConflictProviderWork) -> None:
        if conflict_state["provider_work"] is work:
            conflict_state["provider_work"] = None
        if conflict_state["closing"] or not conflict_state["active"]:
            conflict_state["pending_provider_request"] = None
            return
        pending = conflict_state["pending_provider_request"]
        conflict_state["pending_provider_request"] = None
        if pending is not None:
            start_background_conflict_refresh(*pending)
            return
        if work.error is not None:
            provider_failure["error"] = work.error
            dialog.reject()
            return
        model = work.result
        if not isinstance(model, FinalReviewConflictEditor):
            provider_failure["error"] = TypeError(
                "Final-review conflict editor model is invalid"
            )
            dialog.reject()
            return
        render_conflict_editor(model)
        qt_core.QTimer.singleShot(
            0,
            lambda: restore_conflict_scroll_anchor(
                work.scroll_anchor
            ),
        )

    provider_bridge.completed.connect(
        complete_provider_work,
        qt_core.Qt.ConnectionType.QueuedConnection,
    )

    def start_background_conflict_refresh(
        provider: object,
        row_id: str,
        selections: tuple[FinalReviewConflictSelection, ...],
        scroll_anchor: tuple[str, int] | None,
    ) -> None:
        conflict_instruction.setText("正在更新冲突选择…")
        conflict_confirm.setEnabled(False)
        work = ConflictProviderWork(
            provider,
            row_id,
            selections,
            scroll_anchor,
        )
        conflict_state["provider_work"] = work
        try:
            _FINAL_REVIEW_CONFLICT_PROJECTION_LANE.submit(work.run)
        except BaseException:
            conflict_state["provider_work"] = None
            raise

    def refresh_conflict_editor() -> None:
        provider = request.conflict_editor_provider
        if provider is None:
            raise ValueError("Final-review conflict editor is unavailable")
        scroll_anchor = conflict_scroll_anchor()
        selections = conflict_draft_values()
        if request.background_conflict_refresh:
            provider_request = (
                provider,
                conflict_state["row_id"],
                selections,
                scroll_anchor,
            )
            if conflict_state["provider_work"] is not None:
                conflict_state["pending_provider_request"] = (
                    provider_request
                )
                conflict_instruction.setText(
                    "正在更新冲突选择…"
                )
                conflict_confirm.setEnabled(False)
                return
            start_background_conflict_refresh(*provider_request)
            return
        model = provider(
            conflict_state["row_id"],
            selections,
        )
        if not isinstance(model, FinalReviewConflictEditor):
            raise TypeError("Final-review conflict editor model is invalid")
        render_conflict_editor(model)
        qt_core.QTimer.singleShot(
            0,
            lambda: restore_conflict_scroll_anchor(scroll_anchor),
        )

    def open_conflict_editor(
        row_id: str = "",
        *,
        force: bool = False,
        initial_selections: tuple[
            FinalReviewConflictSelection,
            ...,
        ] = (),
        initial_pending_selections: tuple[
            FinalReviewConflictSelection,
            ...,
        ] = (),
        initial_editing_group_ids: tuple[str, ...] = (),
    ) -> None:
        if row_id in row_ids:
            row_index = row_ids.index(row_id)
            table.setCurrentCell(row_index, 0)
            table.selectRow(row_index)
        else:
            row_index = selected_row_index()
        if (
            request.conflict_editor_provider is None
            or not 0 <= row_index < len(request.rows)
            or (
                not force
                and not request.rows[row_index].has_related_conflicts
            )
        ):
            raise ValueError("Selected conflict correction is unavailable")
        conflict_state["active"] = True
        conflict_resize_state["active"] = True
        conflict_resize_state["user_resized"] = False
        conflict_resize_timer.stop()
        conflict_state["forced"] = force
        conflict_state["row_id"] = request.rows[row_index].row_id
        conflict_state["review_size"] = dialog.size()
        conflict_state["review_minimum_size"] = dialog.minimumSize()
        available = dialog.screen().availableGeometry()
        dialog.setMinimumSize(
            min(520, available.width()),
            min(420, available.height()),
        )
        conflict_state["draft"] = {
            selection.group_id: selection
            for selection in initial_selections
        }
        conflict_state["pending_special"] = {
            selection.group_id: selection.selected_keys
            for selection in initial_pending_selections
        }
        conflict_state["editing_special"] = set(
            initial_editing_group_ids
        )
        for group_id in initial_editing_group_ids:
            conflict_state["pending_special"].setdefault(group_id, ())
        refresh_conflict_editor()
        review_panel.hide()
        conflict_editor.show()
        title.setText("修改冲突选择")
        if _restore_dialog_size(
            dialog,
            "final_review_conflict",
            qt_core,
        ):
            conflict_resize_state["user_resized"] = True

    def return_from_conflict_editor() -> None:
        if (
            conflict_state["forced"]
            and request.conflict_back_action != "local"
        ):
            finish(request.conflict_back_action)
            return
        conflict_state["active"] = False
        conflict_resize_state["active"] = False
        conflict_resize_state["user_resized"] = False
        conflict_resize_timer.stop()
        conflict_state["forced"] = False
        conflict_state["row_id"] = ""
        conflict_state["draft"] = {}
        conflict_state["pending_special"] = {}
        conflict_state["editing_special"] = set()
        conflict_state["model"] = None
        conflict_state["active_group_id"] = ""
        conflict_state["pending_provider_request"] = None
        for button in conflict_decision_buttons.values():
            button.hide()
        clear_conflict_groups()
        conflict_editor.hide()
        review_panel.show()
        title.setText(request.title)
        review_minimum_size = conflict_state["review_minimum_size"]
        review_size = conflict_state["review_size"]
        if review_minimum_size is not None:
            dialog.setMinimumSize(review_minimum_size)
        if review_size is not None:
            dialog.resize(review_size)
        conflict_state["review_size"] = None
        conflict_state["review_minimum_size"] = None

    def finish(
        action: str,
        *,
        conflict_selections: tuple[FinalReviewConflictSelection, ...] = (),
        conflict_pending_selections: tuple[
            FinalReviewConflictSelection,
            ...,
        ] = (),
        conflict_editing_group_ids: tuple[str, ...] = (),
    ) -> None:
        conflict_state["closing"] = True
        conflict_resize_state["active"] = False
        conflict_resize_timer.stop()
        state = current_view_state()
        selected_response["value"] = DialogResponse(
            action=action,
            selected_row_id=state.selected_row_id,
            view_state=state,
            conflict_selections=conflict_selections,
            conflict_pending_selections=conflict_pending_selections,
            conflict_editing_group_ids=conflict_editing_group_ids,
        )
        dialog.accept()

    search_filter = SearchKeyFilter(search)
    search._final_review_key_filter = search_filter
    search.installEventFilter(search_filter)
    search.textChanged.connect(refresh_matches)
    search_up.clicked.connect(lambda: move_match(-1))
    search_down.clicked.connect(lambda: move_match(1))
    table.itemSelectionChanged.connect(update_modification_actions)
    tabs.currentChanged.connect(handle_tab_change)
    modify_attribution.clicked.connect(lambda: finish("modify_attribution"))
    modify_conflicts.clicked.connect(lambda: open_conflict_editor())
    confirm.clicked.connect(lambda: finish("confirm"))
    cancel.clicked.connect(lambda: finish("cancel"))
    conflict_back.clicked.connect(return_from_conflict_editor)
    conflict_confirm.clicked.connect(
        lambda: finish(
            "modify_conflicts",
            conflict_selections=conflict_draft_values(),
        )
    )
    conflict_cancel.clicked.connect(
        lambda: finish(
            "cancel_conflicts",
            conflict_selections=conflict_draft_values(),
            conflict_pending_selections=conflict_pending_values(),
            conflict_editing_group_ids=conflict_editing_group_ids(),
        )
    )
    close_button.clicked.connect(
        lambda: (
            return_from_conflict_editor()
            if conflict_state["active"]
            else finish("cancel")
        )
    )
    dialog_key_filter = FinalReviewKeyFilter(dialog)
    dialog._final_review_key_filter = dialog_key_filter
    dialog.installEventFilter(dialog_key_filter)

    original_close_event = dialog.closeEvent

    def final_review_close_event(event: object) -> None:
        if conflict_state["active"]:
            event.ignore()
            return_from_conflict_editor()
            return
        conflict_state["closing"] = True
        original_close_event(event)

    dialog.closeEvent = final_review_close_event

    initial_state = request.initial_view_state
    search.blockSignals(True)
    search.setText(initial_state.search_text)
    search.blockSignals(False)
    refresh_matches(initial_state.search_text, jump=False)
    if initial_state.selected_row_id in row_ids:
        initial_row = row_ids.index(initial_state.selected_row_id)
        table.setCurrentCell(initial_row, 0)
        table.selectRow(initial_row)
        if initial_row in matches["rows"]:
            matches["position"] = matches["rows"].index(initial_row)
            update_match_readout()
    update_modification_actions()

    def restore_initial_view_position() -> None:
        tabs.setCurrentIndex(1 if initial_state.active_tab == "output" else 0)
        if initial_state.active_tab == "output":
            output_layout_timer.stop()
            arrange_output_columns()
        table.verticalScrollBar().setValue(
            max(0, initial_state.attribution_scroll_value)
        )
        if initial_state.selected_row_id in row_ids:
            row_index = row_ids.index(initial_state.selected_row_id)
            table_row_fit["restore_row"] = row_index
            item = table.item(row_index, 0)
            if not table.visualItemRect(item).intersects(
                table.viewport().rect()
            ):
                table.scrollToItem(
                    item,
                    qt_widgets.QAbstractItemView.ScrollHint.EnsureVisible,
                )
        restore_initial_output_view_position()

    qt_core.QTimer.singleShot(
        120 if initial_state.active_tab == "output" else 0,
        restore_initial_view_position,
    )
    if request.initial_conflict_row_id:
        qt_core.QTimer.singleShot(
            0,
            lambda: open_conflict_editor(
                request.initial_conflict_row_id,
                force=True,
                initial_selections=request.initial_conflict_selections,
                initial_pending_selections=(
                    request.initial_conflict_pending_selections
                ),
                initial_editing_group_ids=(
                    request.initial_conflict_editing_group_ids
                ),
            ),
        )

    _enable_title_bar_drag(
        header,
        dialog,
        qt_core,
        allow_partial_offscreen=True,
    )
    _restore_dialog_size(dialog, "final_review", qt_core)
    try:
        if request.taskbar_visible:
            _make_windows_taskbar_window(dialog)
        qt_core.QTimer.singleShot(
            0,
            lambda: restore_dialog_position(dialog, qt_core, qt_gui),
        )
        result = dialog.exec()
        if provider_failure["error"] is not None:
            raise provider_failure["error"]
        response = selected_response["value"]
        if (
            result == qt_widgets.QDialog.DialogCode.Accepted
            and isinstance(response, DialogResponse)
        ):
            return response
        state = current_view_state()
        return DialogResponse(
            action="cancel",
            selected_row_id=state.selected_row_id,
            view_state=state,
        )
    finally:
        _dispose_nonmodal_dialog(dialog, qt_core)


def show_styled_dialog(request: DialogRequest, *, parent: object | None = None) -> DialogResponse:
    if isinstance(request, FinalReviewDialogRequest):
        return show_final_review_dialog(request, parent=parent)
    qt_widgets, qt_core = _load_qt_modules()
    qt_gui = _load_qt_gui()
    _ensure_application(qt_widgets)
    dialog_parent = parent or qt_widgets.QApplication.activeWindow()

    dialog = qt_widgets.QDialog(dialog_parent)
    dialog.setObjectName("organizer_dialog")
    dialog.setWindowTitle(request.title)
    dialog.setModal(True)
    flags = qt_core.Qt.WindowType.Window | qt_core.Qt.WindowType.FramelessWindowHint
    if request.topmost:
        flags |= qt_core.Qt.WindowType.WindowStaysOnTopHint
    dialog.setWindowFlags(flags)
    dialog.setAttribute(qt_core.Qt.WidgetAttribute.WA_DeleteOnClose, False)
    apply_styled_dialog_chrome(dialog, qt_core)
    dialog.setStyleSheet(ORGANIZER_DIALOG_STYLE_SHEET)
    dialog.setFont(_font(qt_gui, 13))
    available = dialog.screen().availableGeometry()
    safe_margin = 24
    max_width = max(1, available.width() - safe_margin)
    dialog.setMinimumWidth(min(420, max_width))
    dialog.setMaximumWidth(max_width)
    dialog.setMaximumHeight(max(1, available.height() - safe_margin))

    root = qt_widgets.QVBoxLayout(dialog)
    root.setContentsMargins(0, 0, 0, 0)
    root.setSpacing(0)

    header = qt_widgets.QFrame(dialog)
    header.setObjectName("dialog_header")
    header.setFixedHeight(50)
    header_layout = qt_widgets.QHBoxLayout(header)
    header_layout.setContentsMargins(16, 0, 10, 0)
    header_layout.setSpacing(8)
    title = qt_widgets.QLabel(request.title, header)
    title.setObjectName("dialog_title")
    title.setFont(_font(qt_gui, 15, bold=True))
    close_button = qt_widgets.QPushButton("×", header)
    close_button.setObjectName("dialog_close_button")
    close_button.setFixedSize(28, 26)
    close_button.setFocusPolicy(qt_core.Qt.FocusPolicy.NoFocus)
    close_button.clicked.connect(dialog.reject)
    header_layout.addWidget(title, 1)
    header_layout.addWidget(close_button, 0)
    root.addWidget(header)

    body = qt_widgets.QFrame(dialog)
    body.setObjectName("dialog_body")
    body_layout = qt_widgets.QVBoxLayout(body)
    body_layout.setContentsMargins(18, 16, 18, 16)
    body_layout.setSpacing(14)

    scroll = qt_widgets.QScrollArea(body)
    scroll.setObjectName("dialog_message_scroll")
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(qt_widgets.QFrame.Shape.NoFrame)
    scroll.setHorizontalScrollBarPolicy(
        qt_core.Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    )

    WrapAnywhereLabel = _wrap_anywhere_label_type(qt_widgets, qt_core, qt_gui)
    message = WrapAnywhereLabel(request.message, scroll)
    message.setObjectName("dialog_message")
    message.setWordWrap(True)
    message.setMinimumWidth(0)
    message.setSizePolicy(
        qt_widgets.QSizePolicy.Policy.Ignored,
        qt_widgets.QSizePolicy.Policy.Preferred,
    )
    message.setTextInteractionFlags(qt_core.Qt.TextInteractionFlag.NoTextInteraction)
    message.setFont(_font(qt_gui, 13))
    scroll.setWidget(message)
    scroll.setMaximumHeight(220)
    body_layout.addWidget(scroll)

    buttons_row = qt_widgets.QHBoxLayout()
    buttons_row.setSpacing(8)
    buttons_row.addStretch(1)
    selected = {"action": None}
    for action in request.actions:
        button = qt_widgets.QPushButton(_display_label(action, request.kind), body)
        button.setObjectName(_button_object_name(action))
        configure_workflow_button(button, qt_gui)
        button.setEnabled(not (action == "confirm" and not request.can_confirm))
        button.clicked.connect(lambda checked=False, selected_action=action: _accept_dialog(dialog, selected, selected_action))
        buttons_row.addWidget(button)
    body_layout.addLayout(buttons_row)
    root.addWidget(body)

    _enable_title_bar_drag(header, dialog, qt_core)
    try:
        if request.taskbar_visible:
            _make_windows_taskbar_window(dialog)
        qt_core.QTimer.singleShot(
            0,
            lambda: restore_dialog_position(dialog, qt_core, qt_gui),
        )
        result = dialog.exec()
        if (
            result == qt_widgets.QDialog.DialogCode.Accepted
            and selected["action"] is not None
        ):
            return DialogResponse(action=str(selected["action"]))
        return DialogResponse(action=_fallback_action(request.actions))
    finally:
        _dispose_nonmodal_dialog(dialog, qt_core)


_DIALOG_POSITION_PROPERTY = "_spectrum_organizer_dialog_position"
_DIALOG_SIZE_MEMORY_ATTRIBUTE = "_spectrum_organizer_dialog_sizes"


def _dialog_session_owner(dialog: object) -> object | None:
    owner = dialog.parentWidget()
    if owner is None:
        return None
    return owner.window()


def _remember_dialog_position(dialog: object, qt_core: object) -> None:
    owner = _dialog_session_owner(dialog)
    if owner is None:
        return
    owner.setProperty(
        _DIALOG_POSITION_PROPERTY,
        qt_core.QPoint(dialog.frameGeometry().topLeft()),
    )


def _remember_dialog_size(
    dialog: object,
    size_key: str,
    qt_core: object,
) -> None:
    owner = _dialog_session_owner(dialog)
    if owner is None:
        return
    remembered = getattr(owner, _DIALOG_SIZE_MEMORY_ATTRIBUTE, None)
    if remembered is None:
        remembered = {}
        setattr(owner, _DIALOG_SIZE_MEMORY_ATTRIBUTE, remembered)
    remembered[size_key] = qt_core.QSize(dialog.size())


def _restore_dialog_size(
    dialog: object,
    size_key: str,
    qt_core: object,
) -> bool:
    owner = _dialog_session_owner(dialog)
    if owner is None:
        return False
    remembered = getattr(owner, _DIALOG_SIZE_MEMORY_ATTRIBUTE, None)
    if not isinstance(remembered, dict):
        return False
    stored = remembered.get(size_key)
    if not isinstance(stored, qt_core.QSize):
        return False
    available = dialog.screen().availableGeometry().size()
    minimum = dialog.minimumSize()
    maximum = dialog.maximumSize().boundedTo(available)
    width = min(max(stored.width(), minimum.width()), maximum.width())
    height = min(max(stored.height(), minimum.height()), maximum.height())
    dialog.resize(width, height)
    return True


def restore_dialog_position(
    dialog: object,
    qt_core: object,
    qt_gui: object | None = None,
) -> None:
    owner = _dialog_session_owner(dialog)
    if owner is None:
        return
    anchor = owner.property(_DIALOG_POSITION_PROPERTY)
    if not isinstance(anchor, qt_core.QPoint):
        return
    qt_gui = qt_gui or _load_qt_gui()
    screen = qt_gui.QGuiApplication.screenAt(anchor) or dialog.screen()
    available = screen.availableGeometry()
    frame = dialog.frameGeometry()
    target_x = min(
        max(anchor.x(), available.left()),
        max(available.left(), available.right() - frame.width() + 1),
    )
    target_y = min(
        max(anchor.y(), available.top()),
        max(available.top(), available.bottom() - frame.height() + 1),
    )
    dialog.move(
        dialog.pos()
        + qt_core.QPoint(
            target_x - frame.left(),
            target_y - frame.top(),
        )
    )


def _run_topmost_nonmodal_dialog(dialog: object, qt_core: object) -> int:
    loop = qt_core.QEventLoop(dialog)
    owner = dialog.parentWidget()

    class OwnerLifecycleFilter(qt_core.QObject):
        def reject_if_owner_closed(self) -> None:
            try:
                if owner is None or not owner.isVisible():
                    dialog.reject()
            except RuntimeError:
                loop.quit()

        def eventFilter(self, watched: object, event: object) -> bool:
            if event.type() == qt_core.QEvent.Type.DeferredDelete:
                try:
                    dialog.reject()
                except RuntimeError:
                    loop.quit()
            elif event.type() == qt_core.QEvent.Type.Close:
                qt_core.QTimer.singleShot(0, self.reject_if_owner_closed)
            return False

    def finish_dialog_loop(_value: object | None = None) -> None:
        try:
            dialog.hide()
        except RuntimeError:
            pass
        loop.quit()

    dialog.finished.connect(finish_dialog_loop)
    dialog.destroyed.connect(finish_dialog_loop)
    owner_filter = None
    if owner is not None:
        owner_filter = OwnerLifecycleFilter(dialog)
        owner.installEventFilter(owner_filter)
        dialog._owner_lifecycle_filter = owner_filter
    dialog.show()
    restore_dialog_position(dialog, qt_core)
    dialog.raise_()
    dialog.activateWindow()
    if dialog.isVisible():
        loop.exec()
    try:
        return int(dialog.result())
    except RuntimeError:
        return 0


def _make_windows_taskbar_window(dialog: object) -> None:
    if os.name != "nt":
        return

    import ctypes

    qt_gui = _load_qt_gui()
    if qt_gui.QGuiApplication.platformName().casefold() != "windows":
        return

    user32 = _windows_user32()
    hwnd = int(dialog.winId())
    ctypes.set_last_error(0)
    ex_style = int(user32.GetWindowLongPtrW(hwnd, -20))
    read_error = ctypes.get_last_error()
    if not ex_style and read_error:
        raise RuntimeError(f"GetWindowLongPtrW failed: {read_error}")

    ctypes.set_last_error(0)
    previous_style = user32.SetWindowLongPtrW(
        hwnd,
        -20,
        (ex_style & ~0x80) | 0x40000,
    )
    write_error = ctypes.get_last_error()
    if not previous_style and write_error:
        raise RuntimeError(f"SetWindowLongPtrW failed: {write_error}")


def _dispose_nonmodal_dialog(dialog: object, qt_core: object) -> None:
    try:
        dialog.hide()
        dialog.destroy(True, True)
        dialog.deleteLater()
    except RuntimeError:
        return
    qt_core.QCoreApplication.sendPostedEvents(
        dialog,
        qt_core.QEvent.Type.DeferredDelete,
    )


def show_attribution_book_picker(
    request: AttributionBookSelectionRequest,
    *,
    parent: object | None = None,
) -> AttributionBookSelectionResponse:
    qt_widgets, qt_core = _load_qt_modules()
    qt_gui = _load_qt_gui()
    _ensure_application(qt_widgets)

    dialog = qt_widgets.QDialog(parent)
    dialog.setObjectName("organizer_dialog")
    dialog.setWindowTitle("选择要归属的 Book")
    dialog.setModal(False)
    dialog.setWindowModality(qt_core.Qt.WindowModality.NonModal)
    dialog.setWindowFlags(
        qt_core.Qt.WindowType.Tool
        | qt_core.Qt.WindowType.FramelessWindowHint
        | qt_core.Qt.WindowType.WindowStaysOnTopHint
    )
    apply_styled_dialog_chrome(dialog, qt_core)
    dialog.setStyleSheet(ORGANIZER_DIALOG_STYLE_SHEET)
    dialog.setFont(_font(qt_gui, 13))
    available = dialog.screen().availableGeometry()
    safe_margin = 24
    dialog.setFixedWidth(min(560, max(1, available.width() - safe_margin)))
    dialog.setMaximumHeight(max(1, available.height() - safe_margin))

    root = qt_widgets.QVBoxLayout(dialog)
    root.setContentsMargins(0, 0, 0, 0)
    root.setSpacing(0)
    header = qt_widgets.QFrame(dialog)
    header.setObjectName("dialog_header")
    header.setFixedHeight(50)
    header_layout = qt_widgets.QHBoxLayout(header)
    header_layout.setContentsMargins(16, 0, 10, 0)
    title = qt_widgets.QLabel("选择要归属的 Book", header)
    title.setObjectName("dialog_title")
    title.setFont(_font(qt_gui, 15, bold=True))
    close_button = qt_widgets.QPushButton("×", header)
    close_button.setObjectName("dialog_close_button")
    close_button.setFixedSize(28, 26)
    close_button.setFocusPolicy(qt_core.Qt.FocusPolicy.NoFocus)
    close_button.clicked.connect(dialog.reject)
    header_layout.addWidget(title, 1)
    header_layout.addWidget(close_button)
    root.addWidget(header)

    body = qt_widgets.QFrame(dialog)
    body.setObjectName("dialog_body")
    body_layout = qt_widgets.QVBoxLayout(body)
    body_layout.setContentsMargins(20, 16, 20, 16)
    body_layout.setSpacing(12)

    WrapAnywhereLabel = _wrap_anywhere_label_type(qt_widgets, qt_core, qt_gui)
    context = WrapAnywhereLabel(
        f"{request.source_filename}  ·  {request.folder_label}\n"
        "请选择一个尚未确认归属的 Book。",
        body,
    )
    context.setObjectName("attribution_picker_context")
    context.setWordWrap(True)
    context.setMinimumWidth(0)
    context.setSizePolicy(
        qt_widgets.QSizePolicy.Policy.Ignored,
        qt_widgets.QSizePolicy.Policy.Preferred,
    )
    body_layout.addWidget(context)

    book_list = qt_widgets.QListWidget(body)
    book_list.setObjectName("attribution_pending_book_list")
    book_palette = book_list.palette()
    book_palette.setColor(qt_gui.QPalette.ColorRole.Highlight, qt_gui.QColor("#147a6c"))
    book_palette.setColor(qt_gui.QPalette.ColorRole.HighlightedText, qt_gui.QColor("#ffffff"))
    book_list.setPalette(book_palette)
    book_list.setMinimumHeight(180)
    book_list.setWordWrap(True)
    book_list.setTextElideMode(qt_core.Qt.TextElideMode.ElideNone)
    book_list.setResizeMode(qt_widgets.QListView.ResizeMode.Adjust)
    book_list.setUniformItemSizes(False)
    book_list.setHorizontalScrollBarPolicy(
        qt_core.Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    )
    book_list.setSelectionMode(qt_widgets.QAbstractItemView.SelectionMode.SingleSelection)

    class WrapAnywhereItemDelegate(qt_widgets.QStyledItemDelegate):
        def sizeHint(self, option: object, index: object) -> object:
            width = max(1, book_list.viewport().width() - 8)
            text = str(index.data(qt_core.Qt.ItemDataRole.DisplayRole) or "")
            bounds = option.fontMetrics.boundingRect(
                qt_core.QRect(0, 0, max(1, width - 12), 10000),
                qt_core.Qt.TextFlag.TextWrapAnywhere,
                text,
            )
            return qt_core.QSize(width, max(option.fontMetrics.lineSpacing(), bounds.height()) + 12)

        def paint(self, painter: object, option: object, index: object) -> None:
            styled = qt_widgets.QStyleOptionViewItem(option)
            self.initStyleOption(styled, index)
            text = styled.text
            styled.text = ""
            style = styled.widget.style() if styled.widget is not None else qt_widgets.QApplication.style()
            style.drawControl(qt_widgets.QStyle.ControlElement.CE_ItemViewItem, styled, painter)
            if styled.state & qt_widgets.QStyle.StateFlag.State_Selected:
                painter.fillRect(
                    styled.rect,
                    styled.palette.color(qt_gui.QPalette.ColorRole.Highlight),
                )
            elif styled.state & qt_widgets.QStyle.StateFlag.State_MouseOver:
                painter.fillRect(styled.rect, qt_gui.QColor("#dcebe7"))
            text_rect = style.subElementRect(
                qt_widgets.QStyle.SubElement.SE_ItemViewItemText,
                styled,
                styled.widget,
            )
            role = (
                qt_gui.QPalette.ColorRole.HighlightedText
                if styled.state & qt_widgets.QStyle.StateFlag.State_Selected
                else qt_gui.QPalette.ColorRole.Text
            )
            painter.save()
            painter.setPen(styled.palette.color(role))
            painter.drawText(
                text_rect,
                qt_core.Qt.AlignmentFlag.AlignLeft
                | qt_core.Qt.AlignmentFlag.AlignVCenter
                | qt_core.Qt.TextFlag.TextWrapAnywhere,
                text,
            )
            painter.restore()

    book_list.setItemDelegate(WrapAnywhereItemDelegate(book_list))
    for book_key, display_name in request.choices:
        item = qt_widgets.QListWidgetItem(display_name)
        item.setData(qt_core.Qt.ItemDataRole.UserRole, book_key)
        book_list.addItem(item)
    book_list.setCurrentRow(-1)
    body_layout.addWidget(book_list)

    error_text = qt_widgets.QLabel("", body)
    error_text.setObjectName("dialog_error_text")
    error_text.setWordWrap(True)
    error_text.hide()
    body_layout.addWidget(error_text)

    selected: dict[str, object] = {"response": None}
    if request.allow_return_to_folder:
        return_button = qt_widgets.QPushButton("返回 Folder 统一归属", body)
        return_button.setObjectName("dialog_button_secondary")
        configure_workflow_button(return_button, qt_gui)
        body_layout.addWidget(return_button)
    else:
        return_button = None
    buttons = qt_widgets.QHBoxLayout()
    buttons.setSpacing(8)
    buttons.addStretch(1)
    confirm_button = qt_widgets.QPushButton("确认选择", body)
    confirm_button.setObjectName("dialog_button_primary")
    configure_workflow_button(confirm_button, qt_gui)
    confirm_button.setEnabled(False)
    cancel_button = qt_widgets.QPushButton("取消并退出", body)
    cancel_button.setObjectName("dialog_button_danger")
    configure_workflow_button(cancel_button, qt_gui)
    buttons.addWidget(confirm_button)
    buttons.addWidget(cancel_button)
    body_layout.addLayout(buttons)

    body_layout.setSizeConstraint(
        qt_widgets.QLayout.SizeConstraint.SetMinAndMaxSize
    )
    body_layout.activate()
    natural_body_height = body_layout.heightForWidth(dialog.width())
    if natural_body_height < 0:
        natural_body_height = body_layout.sizeHint().height()
    body.resize(dialog.width(), natural_body_height)
    body_scroll = qt_widgets.QScrollArea(dialog)
    body_scroll.setObjectName("attribution_picker_scroll")
    body_scroll.setWidgetResizable(True)
    body_scroll.setFrameShape(qt_widgets.QFrame.Shape.NoFrame)
    body_scroll.setHorizontalScrollBarPolicy(
        qt_core.Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    )
    body_scroll.setVerticalScrollBarPolicy(
        qt_core.Qt.ScrollBarPolicy.ScrollBarAsNeeded
    )
    body_scroll.setWidget(body)
    root.addWidget(body_scroll, 1)
    natural_height = header.height() + natural_body_height
    dialog.resize(
        dialog.width(),
        min(natural_height, max(1, available.height() - safe_margin)),
    )

    def confirm_selection() -> None:
        item = book_list.currentItem()
        if item is None:
            _set_dialog_error(error_text, "请先选择一个 Book。")
            book_list.setFocus()
            return
        selected["response"] = AttributionBookSelectionResponse(
            action="select_book",
            book_key=str(item.data(qt_core.Qt.ItemDataRole.UserRole) or ""),
        )
        dialog.accept()

    def return_to_folder() -> None:
        selected["response"] = AttributionBookSelectionResponse(action="return_to_folder")
        dialog.accept()

    def reveal_book_list() -> None:
        body_scroll.ensureWidgetVisible(book_list, 16, 16)

    def book_selection_changed() -> None:
        confirm_button.setEnabled(book_list.currentItem() is not None)
        _clear_dialog_error(error_text)
        reveal_book_list()

    book_list.itemSelectionChanged.connect(book_selection_changed)
    book_list.itemDoubleClicked.connect(lambda _item: confirm_selection())
    book_list.itemActivated.connect(lambda _item: confirm_selection())
    confirm_button.clicked.connect(confirm_selection)
    cancel_button.clicked.connect(dialog.reject)
    if return_button is not None:
        return_button.clicked.connect(return_to_folder)
    _ignore_escape_key(dialog, qt_core)
    _enable_title_bar_drag(header, dialog, qt_core)

    def focus_unselected_book_list() -> None:
        book_list.setFocus()
        book_list.clearSelection()
        book_list.setCurrentIndex(qt_core.QModelIndex())
        reveal_book_list()

    focus_unselected_book_list()

    try:
        _make_windows_taskbar_window(dialog)
        qt_core.QTimer.singleShot(0, focus_unselected_book_list)
        result = _run_topmost_nonmodal_dialog(dialog, qt_core)
        response = selected["response"]
        if result == qt_widgets.QDialog.DialogCode.Accepted and isinstance(
            response, AttributionBookSelectionResponse
        ):
            return response
        return AttributionBookSelectionResponse(action="cancel")
    finally:
        _dispose_nonmodal_dialog(dialog, qt_core)


def _conflict_review_subject(request: ConflictReviewRequest) -> str:
    if request.decision_subject:
        return request.decision_subject
    return {
        "special_group": "特殊谱组",
        "special_group_books": "特殊谱组 · 逐 Book",
        "emission_duplicate": "重复发射谱",
        "excitation_selection": "激发谱候选",
    }.get(request.kind, request.title)


def partition_conflict_choices(
    choices: tuple[ConflictReviewChoice, ...],
) -> tuple[
    tuple[tuple[str, str], ...],
    dict[str, tuple[tuple[str, str], ...]],
]:
    labels: list[str] = []
    fields_by_key: dict[str, dict[str, str]] = {}
    for choice in choices:
        fields = {
            str(label): str(value).strip()
            for label, value in choice.fields
            if str(value).strip()
        }
        fields_by_key[choice.book_key] = fields
        for label in fields:
            if label not in labels:
                labels.append(label)

    common: list[tuple[str, str]] = []
    varying_labels: list[str] = []
    for label in labels:
        values = tuple(fields.get(label, "") for fields in fields_by_key.values())
        if values and all(value == values[0] and value for value in values):
            common.append((label, values[0]))
        else:
            varying_labels.append(label)
    varying = {
        choice.book_key: tuple(
            (label, fields_by_key[choice.book_key].get(label, "") or "—")
            for label in varying_labels
        )
        for choice in choices
    }
    return tuple(common), varying


def _conflict_common_columns(
    fields: tuple[tuple[str, str], ...],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    left_markers = ("来源文件", "Folder", "谱图类型", "波长", "范围", "步长")
    rows = tuple((label, f"{label}：{value}") for label, value in fields)
    left = tuple(
        text
        for label, text in rows
        if any(marker in label for marker in left_markers)
    )
    right = tuple(
        text
        for label, text in rows
        if not any(marker in label for marker in left_markers)
    )
    return left, right


def _format_conflict_difference_text(
    fields: tuple[tuple[str, str], ...],
    *,
    empty_message: str,
) -> str:
    provenance = []
    comparable = []
    peaks = []
    for label, value in fields:
        text = f"{label}：{value}"
        if label in {"来源文件", "Folder"}:
            provenance.append(text)
        elif "峰值" in label:
            peaks.append(text)
        else:
            comparable.append(text)
    fields_in_display_order = provenance + comparable + peaks
    return "\n".join(fields_in_display_order or [empty_message])


def _create_conflict_common_detail(
    qt_widgets: object,
    qt_core: object,
    parent: object,
) -> tuple[object, object, object]:
    detail = qt_widgets.QFrame(parent)
    detail.setObjectName("conflict_review_detail")
    detail.setSizePolicy(
        qt_widgets.QSizePolicy.Policy.Preferred,
        qt_widgets.QSizePolicy.Policy.Maximum,
    )
    detail_layout = qt_widgets.QVBoxLayout(detail)
    detail_layout.setContentsMargins(12, 8, 12, 8)
    detail_layout.setSpacing(4)
    detail_title = qt_widgets.QLabel("共同条件", detail)
    detail_title.setObjectName("conflict_review_detail_title")
    detail_layout.addWidget(detail_title)
    detail_columns = qt_widgets.QWidget(detail)
    detail_columns_layout = qt_widgets.QGridLayout(detail_columns)
    detail_columns_layout.setContentsMargins(0, 0, 0, 0)
    detail_columns_layout.setHorizontalSpacing(24)
    detail_columns_layout.setVerticalSpacing(4)
    values = []
    for _index in range(2):
        value = _create_conflict_common_value_label(
            qt_widgets,
            qt_core,
            detail_columns,
        )
        value.setObjectName("conflict_review_detail_value")
        value.setTextInteractionFlags(
            qt_core.Qt.TextInteractionFlag.TextSelectableByMouse
        )
        values.append(value)

    def arrange_columns() -> None:
        for value in values:
            detail_columns_layout.removeWidget(value)
        usable_width = max(1, detail_columns.contentsRect().width())
        half_width = max(
            1,
            (usable_width - detail_columns_layout.horizontalSpacing()) // 2,
        )
        stack = any(
            value.text()
            and value._minimum_atomic_width() > half_width
            for value in values
        )
        if stack:
            for row, value in enumerate(values):
                detail_columns_layout.addWidget(
                    value,
                    row,
                    0,
                    1,
                    2,
                    qt_core.Qt.AlignmentFlag.AlignTop,
                )
        else:
            for column, value in enumerate(values):
                detail_columns_layout.addWidget(
                    value,
                    0,
                    column,
                    qt_core.Qt.AlignmentFlag.AlignTop,
                )
        detail_columns.updateGeometry()

    detail_columns._arrange_columns = arrange_columns
    original_resize_event = detail_columns.resizeEvent

    def resize_event(event: object) -> None:
        original_resize_event(event)
        arrange_columns()

    detail_columns.resizeEvent = resize_event
    detail_layout.addWidget(detail_columns)
    return detail, values[0], values[1]


def _set_conflict_common_detail(
    left: object,
    right: object,
    fields: tuple[tuple[str, str], ...],
) -> None:
    common_left, common_right = _conflict_common_columns(fields)
    left.setText("\n".join(common_left) or "无共同条件")
    right.setText("\n".join(common_right))
    arrange_columns = getattr(left.parentWidget(), "_arrange_columns", None)
    if callable(arrange_columns):
        arrange_columns()


class _ConflictCellLayoutMixin:
    _field_separator = " · "
    _line_gap = 4
    _vertical_padding = 14
    _semantic_breaks = frozenset(("/", "\\", "_", " ", "；", "-"))
    _value_units = (
        "MicroAmps",
        "counts",
        "mol/L",
        "mol%",
        "vol%",
        "wt%",
        "µs",
        "μs",
        "nm",
        "ms",
        "ns",
        "ps",
        "Hz",
        "mW",
        "°C",
        "K",
        "M",
        "s",
        "V",
        "A",
        "%",
    )
    _keep_value_together_labels = frozenset(
        (
            "峰值",
            "固定激发波长",
            "固定发射波长",
            "扫描范围",
            "扫描步长",
            "激发扫描范围",
            "激发扫描步长",
            "发射扫描范围",
            "发射扫描步长",
            "狭缝",
            "延迟时间",
            "采样窗口",
            "单次闪光周期",
            "闪光次数",
            "浓度",
            "温度",
            "Note 时间",
        )
    )

    @classmethod
    def _is_atomic_numeric_value(cls, value: str) -> bool:
        stripped = value.strip()
        unit = next(
            (
                candidate
                for candidate in cls._value_units
                if stripped.endswith(candidate)
            ),
            "",
        )
        magnitude = (
            stripped[: -len(unit)].rstrip()
            if unit
            else stripped
        )
        numeric_punctuation = ".,+-−–—×^eE/():="
        return bool(magnitude) and any(
            character.isdigit() for character in magnitude
        ) and all(
            character.isdigit()
            or character.isspace()
            or character in numeric_punctuation
            for character in magnitude
        )

    @classmethod
    def _can_break_after(
        cls,
        text: str,
        position: int,
    ) -> bool:
        character = text[position - 1]
        if character == "-":
            if (
                position > 2
                and text[position - 2] in "eE"
                and text[position - 3].isdigit()
            ):
                return False
            return position > 1 and text[position - 2].isalnum()
        if character != " ":
            return True
        before = text[: position - 1].rstrip()
        after = text[position:].lstrip()
        if not before or not before[-1].isdigit():
            return True
        unit_delimiters = "-_/\\；,)] ·"
        return not any(
            after == unit
            or (
                after.startswith(unit)
                and len(after) > len(unit)
                and after[len(unit)] in unit_delimiters
            )
            for unit in cls._value_units
        )

    @classmethod
    def _wrap_overwide_field(
        cls,
        field: str,
        metrics: object,
        max_width: int,
    ) -> tuple[str, ...]:
        if metrics.horizontalAdvance(field) <= max_width:
            return (field,)
        label, separator, value = field.partition("：")
        if separator and label in cls._keep_value_together_labels:
            if label == "峰值":
                values = tuple(
                    part.strip()
                    for part in value.split("，")
                    if part.strip()
                )
                return (f"{label}：", *values)
            if label == "狭缝":
                segments = tuple(
                    part.strip()
                    for part in value.split("/")
                    if part.strip()
                )
                if not segments:
                    return (f"{label}：",)
                semantic_segments = (
                    segments[0],
                    *(f"/ {part}" for part in segments[1:]),
                )
                value_lines = []
                current = ""
                for segment in semantic_segments:
                    candidate = (
                        segment
                        if not current
                        else f"{current} {segment}"
                    )
                    if (
                        current
                        and metrics.horizontalAdvance(candidate) > max_width
                    ):
                        value_lines.append(current)
                        current = segment
                    else:
                        current = candidate
                if current:
                    value_lines.append(current)
                return (f"{label}：", *value_lines)
            if not value:
                return (f"{label}：",)
            if (
                metrics.horizontalAdvance(value) <= max_width
                or cls._is_atomic_numeric_value(value)
            ):
                return (f"{label}：", value)
            return (
                f"{label}：",
                *cls._wrap_overwide_field(
                    value,
                    metrics,
                    max_width,
                ),
            )
        lines = []
        remaining = field
        while remaining and metrics.horizontalAdvance(remaining) > max_width:
            fitted = 0
            last_semantic_break = 0
            for position, character in enumerate(remaining, start=1):
                if metrics.horizontalAdvance(remaining[:position]) > max_width:
                    break
                fitted = position
                if (
                    character in cls._semantic_breaks
                    and cls._can_break_after(remaining, position)
                ):
                    last_semantic_break = position
            split_at = last_semantic_break or fitted
            if split_at <= 0:
                split_at = 1
            lines.append(remaining[:split_at].rstrip())
            remaining = remaining[split_at:].lstrip()
        if remaining:
            lines.append(remaining)
        return tuple(lines)

    @staticmethod
    def _slot_rows(
        slot_widths: tuple[int, ...],
        separator_width: int,
        max_width: int,
    ) -> tuple[tuple[int, ...], ...]:
        rows = []
        current = []
        used_width = 0
        for slot, slot_width in enumerate(slot_widths):
            required = slot_width
            if current:
                required += separator_width
            if current and used_width + required > max_width:
                rows.append(tuple(current))
                current = []
                used_width = 0
                required = slot_width
            current.append(slot)
            used_width += required
            if slot_width > max_width:
                rows.append(tuple(current))
                current = []
                used_width = 0
        if current:
            rows.append(tuple(current))
        return tuple(rows)

    def _visual_layout(
        self,
        text: str,
        metrics: object,
        max_width: int,
    ) -> tuple[tuple[tuple[str, int], ...], ...]:
        fields = tuple(line for line in text.splitlines() if line) or ("",)
        available_width = max(1, max_width)
        field_count = max(
            len(row) for row in self._candidate_field_matrix
        )
        slot_widths = tuple(
            max(
                metrics.horizontalAdvance(
                    row[slot] if slot < len(row) else ""
                )
                for row in self._candidate_field_matrix
            )
            for slot in range(field_count)
        )
        separator_width = metrics.horizontalAdvance(self._field_separator)
        rows = self._slot_rows(
            slot_widths,
            separator_width,
            available_width,
        )
        layout = []
        for row in rows:
            if len(row) == 1 and slot_widths[row[0]] > available_width:
                slot = row[0]
                wrapped_by_candidate = tuple(
                    self._wrap_overwide_field(
                        candidate_fields[slot]
                        if slot < len(candidate_fields)
                        else "",
                        metrics,
                        available_width,
                    )
                    for candidate_fields in self._candidate_field_matrix
                )
                wrapped = self._wrap_overwide_field(
                    fields[slot] if slot < len(fields) else "",
                    metrics,
                    available_width,
                )
                aligned_line_count = max(
                    len(wrapped),
                    *(len(parts) for parts in wrapped_by_candidate),
                )
                for line_index in range(aligned_line_count):
                    part = (
                        wrapped[line_index]
                        if line_index < len(wrapped)
                        else ""
                    )
                    layout.append(((part, 0),) if part else ())
                continue
            x = 0
            positioned_fields = []
            for slot in row:
                field = fields[slot] if slot < len(fields) else ""
                if field:
                    positioned_fields.append((field, x))
                x += slot_widths[slot] + separator_width
            if positioned_fields:
                layout.append(tuple(positioned_fields))
        return tuple(layout) or ((("", 0),),)

    def _cell_layout(
        self,
        text: str,
        metrics: object,
        max_width: int,
        column: int,
    ) -> tuple[tuple[tuple[str, int], ...], ...]:
        if column == 1:
            return self._visual_layout(text, metrics, max_width)
        lines = []
        for field in tuple(text.splitlines()) or ("",):
            lines.extend(
                ((part, 0),)
                for part in self._wrap_overwide_field(
                    field,
                    metrics,
                    max(1, max_width),
                )
            )
        return tuple(lines)


def _create_conflict_common_value_label(
    qt_widgets: object,
    qt_core: object,
    parent: object,
) -> object:
    qt_gui = _load_qt_gui()

    class ConflictCommonValueLabel(
        _ConflictCellLayoutMixin,
        qt_widgets.QLabel,
    ):
        def __init__(self, label_parent: object) -> None:
            super().__init__("", label_parent)
            self._candidate_field_matrix = (("",),)
            self.setWordWrap(True)
            policy = qt_widgets.QSizePolicy(
                qt_widgets.QSizePolicy.Policy.Ignored,
                qt_widgets.QSizePolicy.Policy.Maximum,
            )
            policy.setHeightForWidth(True)
            self.setSizePolicy(policy)

        def _layout(
            self,
            width: int,
        ) -> tuple[tuple[tuple[str, int], ...], ...]:
            return self._cell_layout(
                self.text(),
                self.fontMetrics(),
                max(1, width),
                0,
            )

        def _minimum_atomic_width(self) -> int:
            metrics = self.fontMetrics()
            parts = tuple(
                part
                for field in self.text().splitlines()
                for part in self._wrap_overwide_field(
                    field,
                    metrics,
                    1,
                )
                if part
            )
            return max(
                (metrics.horizontalAdvance(part) for part in parts),
                default=0,
            )

        def hasHeightForWidth(self) -> bool:
            return True

        def heightForWidth(self, width: int) -> int:
            layout = self._layout(width)
            metrics = self.fontMetrics()
            return (
                metrics.lineSpacing() * len(layout)
                + self._line_gap * max(0, len(layout) - 1)
            )

        def sizeHint(self) -> object:
            width = max(1, self.width())
            return qt_core.QSize(0, self.heightForWidth(width))

        def minimumSizeHint(self) -> object:
            return qt_core.QSize(0, self.fontMetrics().lineSpacing())

        def paintEvent(self, _event: object) -> None:
            painter = qt_gui.QPainter(self)
            painter.setPen(
                self.palette().color(qt_gui.QPalette.ColorRole.WindowText)
            )
            metrics = self.fontMetrics()
            rect = self.contentsRect()
            baseline = rect.top() + metrics.ascent()
            for line in self._layout(rect.width()):
                for field, x in line:
                    painter.drawText(rect.left() + x, baseline, field)
                baseline += metrics.lineSpacing() + self._line_gap

    return ConflictCommonValueLabel(parent)


def _create_conflict_item_delegate(
    qt_widgets: object,
    qt_core: object,
    qt_gui: object,
    tree: object,
    candidate_field_matrix: tuple[tuple[str, ...], ...],
    *,
    spacer_role: object | None = None,
    group_key_role: object | None = None,
    group_first_role: object | None = None,
    group_last_role: object | None = None,
    active_group: dict[str, str] | None = None,
    stale_role: object | None = None,
) -> object:
    class ConflictItemDelegate(
        _ConflictCellLayoutMixin,
        qt_widgets.QStyledItemDelegate,
    ):
        def __init__(self, parent: object) -> None:
            super().__init__(parent)
            self._candidate_field_matrix = candidate_field_matrix

        @staticmethod
        def _style_and_text_rect(
            styled: object,
        ) -> tuple[object, object]:
            style = (
                styled.widget.style()
                if styled.widget is not None
                else qt_widgets.QApplication.style()
            )
            text_rect = style.subElementRect(
                qt_widgets.QStyle.SubElement.SE_ItemViewItemText,
                styled,
                styled.widget,
            )
            return style, text_rect

        @staticmethod
        def _has_role(index: object, role: object | None) -> bool:
            return role is not None and bool(index.data(role))

        def sizeHint(self, option: object, index: object) -> object:
            row_index = index.sibling(index.row(), 0)
            if self._has_role(row_index, spacer_role):
                return qt_core.QSize(1, 8)
            size = super().sizeHint(option, index)
            text = str(
                index.data(qt_core.Qt.ItemDataRole.DisplayRole) or ""
            )
            styled = qt_widgets.QStyleOptionViewItem(option)
            self.initStyleOption(styled, index)
            styled.text = ""
            styled.rect = qt_core.QRect(
                0,
                0,
                tree.columnWidth(index.column()),
                max(36, styled.rect.height()),
            )
            _style, text_rect = self._style_and_text_rect(styled)
            metrics = styled.fontMetrics
            cell_layout = self._cell_layout(
                text,
                metrics,
                text_rect.width(),
                index.column(),
            )
            size.setWidth(tree.columnWidth(index.column()))
            text_height = (
                metrics.lineSpacing() * len(cell_layout)
                + self._line_gap * max(0, len(cell_layout) - 1)
            )
            size.setHeight(max(36, text_height + self._vertical_padding))
            return size

        def paint(
            self,
            painter: object,
            option: object,
            index: object,
        ) -> None:
            row_index = index.sibling(index.row(), 0)
            if self._has_role(row_index, spacer_role):
                painter.fillRect(
                    option.rect,
                    option.palette.brush(qt_gui.QPalette.ColorRole.Base),
                )
                return
            styled = qt_widgets.QStyleOptionViewItem(option)
            self.initStyleOption(styled, index)
            text = styled.text
            full_rect = styled.rect
            is_selected = bool(
                styled.state & qt_widgets.QStyle.StateFlag.State_Selected
            )
            is_hovered = bool(
                styled.state & qt_widgets.QStyle.StateFlag.State_MouseOver
            )
            is_stale = self._has_role(row_index, stale_role)
            styled.text = ""
            styled.state &= ~(
                qt_widgets.QStyle.StateFlag.State_Selected
                | qt_widgets.QStyle.StateFlag.State_MouseOver
            )
            style, text_rect = self._style_and_text_rect(styled)
            painter.fillRect(
                full_rect,
                styled.palette.brush(qt_gui.QPalette.ColorRole.Base),
            )
            style.drawControl(
                qt_widgets.QStyle.ControlElement.CE_ItemViewItem,
                styled,
                painter,
                styled.widget,
            )
            if is_stale:
                background_color = qt_gui.QColor("#fff3d6")
                text_color = qt_gui.QColor("#8a5a00")
            elif is_selected:
                background_color = qt_gui.QColor("#147a6c")
                text_color = qt_gui.QColor("#ffffff")
            elif is_hovered:
                background_color = qt_gui.QColor("#dcebe7")
                text_color = qt_gui.QColor("#263332")
            else:
                background_color = None
                text_color = styled.palette.color(
                    qt_gui.QPalette.ColorRole.Text
                )
            if background_color is not None:
                painter.fillRect(styled.rect, background_color)
            metrics = styled.fontMetrics
            cell_layout = self._cell_layout(
                text,
                metrics,
                text_rect.width(),
                index.column(),
            )
            line_height = (
                metrics.lineSpacing() * len(cell_layout)
                + self._line_gap * max(0, len(cell_layout) - 1)
            )
            baseline = (
                text_rect.top()
                + max(0, (text_rect.height() - line_height) // 2)
                + metrics.ascent()
            )
            painter.save()
            painter.setClipRect(text_rect)
            painter.setPen(text_color)
            separator_width = metrics.horizontalAdvance(
                self._field_separator
            )
            for line in cell_layout:
                for field_index, (field, x) in enumerate(line):
                    if field_index:
                        painter.drawText(
                            text_rect.left() + x - separator_width,
                            baseline,
                            self._field_separator,
                        )
                    painter.drawText(
                        text_rect.left() + x,
                        baseline,
                        field,
                    )
                baseline += metrics.lineSpacing() + self._line_gap
            painter.restore()
            row_group_key = (
                str(row_index.data(group_key_role) or "")
                if group_key_role is not None
                else ""
            )
            if not row_group_key:
                return
            boundary = qt_gui.QPen(qt_gui.QColor("#b8c8c4"))
            boundary.setWidth(1)
            painter.save()
            painter.setPen(boundary)
            bounded = full_rect.adjusted(0, 0, -1, -1)
            if self._has_role(row_index, group_first_role):
                painter.drawLine(bounded.topLeft(), bounded.topRight())
            if self._has_role(row_index, group_last_role):
                painter.drawLine(
                    bounded.bottomLeft(),
                    bounded.bottomRight(),
                )
            if index.column() == 0:
                painter.drawLine(bounded.topLeft(), bounded.bottomLeft())
            elif index.column() == 1:
                painter.drawLine(bounded.topRight(), bounded.bottomRight())
            if (
                index.column() == 0
                and active_group is not None
                and row_group_key == active_group["key"]
            ):
                painter.fillRect(
                    qt_core.QRect(
                        full_rect.left(),
                        full_rect.top(),
                        3,
                        full_rect.height(),
                    ),
                    qt_gui.QColor("#0b8b7c"),
                )
            painter.restore()

    return ConflictItemDelegate(tree)


def _conflict_book_required_width(
    tree: object,
    first_column_label: str,
) -> int:
    metrics = tree.fontMetrics()
    return max(
        metrics.horizontalAdvance(first_column_label) + 18,
        max(
            metrics.horizontalAdvance(line)
            for index in range(tree.topLevelItemCount())
            for line in tree.topLevelItem(index).text(0).splitlines()
        )
        + 18,
    )


def _conflict_detail_atomic_width(
    tree: object,
    candidate_field_matrix: tuple[tuple[str, ...], ...],
) -> int:
    metrics = tree.fontMetrics()
    return max(
        metrics.horizontalAdvance("关键差异") + 18,
        max(
            (
                metrics.horizontalAdvance(part) + 18
                for fields in candidate_field_matrix
                for field in fields
                for part in _ConflictCellLayoutMixin._wrap_overwide_field(
                    field,
                    metrics,
                    1,
                )
                if part
            ),
            default=0,
        ),
    )


def _arrange_conflict_columns(
    tree: object,
    qt_widgets: object,
    *,
    available_width: int,
    book_required_width: int,
    detail_required_width: int = 0,
) -> None:
    usable_width = max(2, available_width)
    minimum_book_width = min(175, max(90, usable_width // 3))
    normal_book_width = min(
        max(minimum_book_width, book_required_width),
        max(minimum_book_width, int(usable_width * 0.28)),
    )
    book_width = min(
        normal_book_width,
        max(1, usable_width - max(1, detail_required_width)),
    )
    book_width = min(book_width, usable_width - 1)
    header = tree.header()
    header.setSectionResizeMode(
        0,
        qt_widgets.QHeaderView.ResizeMode.Fixed,
    )
    header.setSectionResizeMode(
        1,
        qt_widgets.QHeaderView.ResizeMode.Stretch,
    )
    tree.setColumnWidth(0, book_width)


def show_conflict_review_dialog(
    request: ConflictReviewRequest,
    *,
    parent: object | None = None,
) -> ConflictReviewResponse:
    qt_widgets, qt_core = _load_qt_modules()
    qt_gui = _load_qt_gui()
    _ensure_application(qt_widgets)
    if request.selection_mode not in {"none", "single", "multi", "grouped_single"}:
        raise ValueError(f"Unsupported conflict-review selection mode: {request.selection_mode}")
    grouped_selection = request.selection_mode == "grouped_single"
    displayed_rows = (
        tuple(
            (group.group_key, choice)
            for group in request.choice_groups
            for choice in group.choices
        )
        if grouped_selection
        else tuple(("", choice) for choice in request.choices)
    )
    displayed_choices = tuple(choice for _group_key, choice in displayed_rows)
    group_keys = tuple(group.group_key for group in request.choice_groups)
    active_group = {
        "key": (
            request.initial_active_group_key
            if request.initial_active_group_key in group_keys
            else (group_keys[0] if group_keys else "")
        )
    }

    dialog = qt_widgets.QDialog(parent)
    dialog.setObjectName("organizer_dialog")
    dialog.setWindowTitle(request.title)
    dialog.setModal(False)
    dialog.setWindowModality(qt_core.Qt.WindowModality.NonModal)
    dialog.setWindowFlags(
        qt_core.Qt.WindowType.Window
        | qt_core.Qt.WindowType.FramelessWindowHint
        | qt_core.Qt.WindowType.WindowStaysOnTopHint
    )
    apply_styled_dialog_chrome(dialog, qt_core)
    dialog.setStyleSheet(ORGANIZER_DIALOG_STYLE_SHEET)
    dialog.setFont(_font(qt_gui, 13))
    available = dialog.screen().availableGeometry()
    safe_margin = 24
    maximum_dialog_width = max(1, available.width() - safe_margin)
    preferred_dialog_width = min(900, maximum_dialog_width)
    dialog.resize(
        preferred_dialog_width,
        min(740, max(1, available.height() - safe_margin)),
    )
    dialog.setMaximumHeight(max(1, available.height() - safe_margin))

    root = qt_widgets.QVBoxLayout(dialog)
    root.setContentsMargins(0, 0, 0, 0)
    root.setSpacing(0)
    header = qt_widgets.QFrame(dialog)
    header.setObjectName("dialog_header")
    header.setFixedHeight(50)
    header_layout = qt_widgets.QHBoxLayout(header)
    header_layout.setContentsMargins(16, 0, 10, 0)
    title = qt_widgets.QLabel(request.title, header)
    title.setObjectName("dialog_title")
    title.setFont(_font(qt_gui, 15, bold=True))
    close_button = qt_widgets.QPushButton("×", header)
    close_button.setObjectName("dialog_close_button")
    close_button.setFixedSize(28, 26)
    close_button.setFocusPolicy(qt_core.Qt.FocusPolicy.NoFocus)
    close_button.clicked.connect(dialog.reject)
    header_layout.addWidget(title, 1)
    header_layout.addWidget(close_button)
    root.addWidget(header)

    body = qt_widgets.QFrame(dialog)
    body.setObjectName("dialog_body")
    body_layout = qt_widgets.QVBoxLayout(body)
    body_layout.setContentsMargins(20, 14, 20, 10)
    body_layout.setSpacing(10)
    common_fields_by_group: dict[str, tuple[tuple[str, str], ...]] = {}
    if grouped_selection:
        common_fields_by_group = {
            group.group_key: group.common_fields
            for group in request.choice_groups
        }
        varying_fields_by_row = {
            (group.group_key, choice.book_key): choice.fields
            for group in request.choice_groups
            for choice in group.choices
        }
        common_fields = ()
    else:
        common_fields, varying_fields_by_key = partition_conflict_choices(
            displayed_choices
        )
        varying_fields_by_row = {
            ("", book_key): fields
            for book_key, fields in varying_fields_by_key.items()
        }
    guidance = qt_widgets.QFrame(body)
    guidance.setObjectName("conflict_review_guidance")
    guidance_layout = qt_widgets.QVBoxLayout(guidance)
    guidance_layout.setContentsMargins(12, 8, 12, 8)
    guidance_layout.setSpacing(3)
    subject = qt_widgets.QLabel(_conflict_review_subject(request), guidance)
    subject.setObjectName("conflict_review_subject")
    subject.setFont(_font(qt_gui, 15, bold=True))
    instruction = qt_widgets.QLabel(request.instruction, guidance)
    instruction.setObjectName("conflict_review_guidance_text")
    instruction.setWordWrap(True)
    guidance_layout.addWidget(subject)
    guidance_layout.addWidget(instruction)
    body_layout.addWidget(guidance)

    candidates = qt_widgets.QTreeWidget(body)
    candidates.setObjectName("conflict_review_candidates")
    first_column_label = "Book"
    candidates.setHeaderLabels((first_column_label, "关键差异"))
    candidates.setRootIsDecorated(False)
    candidates.setAlternatingRowColors(False)
    candidates.setUniformRowHeights(False)
    candidates.setWordWrap(False)
    candidates.setTextElideMode(qt_core.Qt.TextElideMode.ElideNone)
    candidates.setHorizontalScrollBarPolicy(
        qt_core.Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    )
    candidates.setVerticalScrollMode(
        qt_widgets.QAbstractItemView.ScrollMode.ScrollPerPixel
    )
    candidates.setSelectionBehavior(
        qt_widgets.QAbstractItemView.SelectionBehavior.SelectRows
    )
    candidates.setMouseTracking(True)
    if request.selection_mode == "none":
        selection_mode = qt_widgets.QAbstractItemView.SelectionMode.NoSelection
    elif request.selection_mode == "single":
        selection_mode = qt_widgets.QAbstractItemView.SelectionMode.SingleSelection
    else:
        selection_mode = qt_widgets.QAbstractItemView.SelectionMode.MultiSelection
    candidates.setSelectionMode(selection_mode)
    candidates.setMinimumHeight(360)
    candidates.setColumnWidth(0, min(240, max(175, dialog.width() // 4)))
    duplicate_group_by_key = {
        book_key: group_number
        for group_number, group in enumerate(
            request.single_select_groups,
            start=1,
        )
        for book_key in group
    }
    empty_difference_message = "仅 Book 名不同"
    difference_text_by_row = {
        (group_key, choice.book_key): _format_conflict_difference_text(
            varying_fields_by_row[(group_key, choice.book_key)],
            empty_message=empty_difference_message,
        )
        for group_key, choice in displayed_rows
    }
    candidate_field_matrix = tuple(
        tuple(
            field
            for field in difference_text_by_row[
                (group_key, choice.book_key)
            ].splitlines()
            if field
        )
        or ("",)
        for group_key, choice in displayed_rows
    )
    group_key_role = qt_core.Qt.ItemDataRole.UserRole + 1
    spacer_role = qt_core.Qt.ItemDataRole.UserRole + 2
    group_first_role = qt_core.Qt.ItemDataRole.UserRole + 3
    group_last_role = qt_core.Qt.ItemDataRole.UserRole + 4

    candidates.setItemDelegate(
        _create_conflict_item_delegate(
            qt_widgets,
            qt_core,
            qt_gui,
            candidates,
            candidate_field_matrix,
            spacer_role=spacer_role,
            group_key_role=group_key_role,
            group_first_role=group_first_role,
            group_last_role=group_last_role,
            active_group=active_group,
        )
    )
    initial_selection_by_group = {
        group.group_key: group.initial_selection
        for group in request.choice_groups
    }
    initial_selection = set(request.initial_selection)
    previous_group_key = ""
    for row_number, (group_key, choice) in enumerate(displayed_rows):
        if grouped_selection and row_number and group_key != previous_group_key:
            spacer = qt_widgets.QTreeWidgetItem(("", ""))
            spacer.setData(0, spacer_role, True)
            spacer.setFlags(qt_core.Qt.ItemFlag.NoItemFlags)
            candidates.addTopLevelItem(spacer)
        next_group_key = (
            displayed_rows[row_number + 1][0]
            if row_number + 1 < len(displayed_rows)
            else ""
        )
        group_number = duplicate_group_by_key.get(choice.book_key)
        display_name = choice.display_name
        if group_number is not None:
            display_name = f"[完全重复组 {group_number}] {display_name}"
        difference_text = difference_text_by_row[(group_key, choice.book_key)]
        item = qt_widgets.QTreeWidgetItem(
            (
                display_name,
                difference_text,
            )
        )
        item.setData(0, qt_core.Qt.ItemDataRole.UserRole, choice.book_key)
        item.setData(
            0,
            group_key_role,
            group_key,
        )
        item.setData(
            0,
            group_first_role,
            grouped_selection and group_key != previous_group_key,
        )
        item.setData(
            0,
            group_last_role,
            grouped_selection and group_key != next_group_key,
        )
        for column in range(2):
            item.setToolTip(column, item.text(column))
        candidates.addTopLevelItem(item)
        item.setSelected(
            (
                initial_selection_by_group.get(group_key) == choice.book_key
                if grouped_selection
                else choice.book_key in initial_selection
            )
        )
        previous_group_key = group_key
    metrics = candidates.fontMetrics()
    book_required_width = _conflict_book_required_width(
        candidates,
        first_column_label,
    )
    detail_required_width = max(
        metrics.horizontalAdvance("关键差异") + 18,
        max(
            metrics.horizontalAdvance(line)
            for index in range(candidates.topLevelItemCount())
            for line in candidates.topLevelItem(index).text(1).splitlines()
        )
        + 18,
    )
    normal_minimum_book_width = min(
        175,
        max(90, (preferred_dialog_width - 72) // 3),
    )
    required_dialog_width = (
        max(normal_minimum_book_width, book_required_width)
        + detail_required_width
        + 72
    )
    if required_dialog_width > dialog.width():
        dialog.resize(
            min(maximum_dialog_width, required_dialog_width),
            dialog.height(),
        )

    def arrange_candidate_columns(width: int) -> None:
        _arrange_conflict_columns(
            candidates,
            qt_widgets,
            available_width=max(2, width - 72),
            book_required_width=book_required_width,
            detail_required_width=_conflict_detail_atomic_width(
                candidates,
                candidate_field_matrix,
            ),
        )

    arrange_candidate_columns(dialog.width())
    body_layout.addWidget(candidates, 1)

    detail, detail_left, detail_right = _create_conflict_common_detail(
        qt_widgets,
        qt_core,
        body,
    )

    def update_common_detail() -> None:
        visible_common_fields = (
            common_fields_by_group.get(active_group["key"], ())
            if grouped_selection
            else common_fields
        )
        _set_conflict_common_detail(
            detail_left,
            detail_right,
            visible_common_fields,
        )

    update_common_detail()
    body_layout.addWidget(detail)

    error_text = qt_widgets.QLabel("", body)
    error_text.setObjectName("dialog_error_text")
    error_text.setWordWrap(True)
    error_text.hide()
    body_layout.addWidget(error_text)
    root.addWidget(body, 1)

    selected: dict[str, object] = {"response": None}
    footer = qt_widgets.QFrame(dialog)
    footer.setObjectName("conflict_review_footer")
    footer_layout = qt_widgets.QVBoxLayout(footer)
    footer_layout.setContentsMargins(20, 8, 20, 14)
    footer_layout.setSpacing(8)
    wide_action_row = qt_widgets.QWidget(footer)
    wide_action_layout = qt_widgets.QHBoxLayout(wide_action_row)
    wide_action_layout.setContentsMargins(0, 0, 0, 0)
    wide_action_layout.setSpacing(8)
    compact_navigation_row = qt_widgets.QWidget(footer)
    compact_navigation_layout = qt_widgets.QHBoxLayout(
        compact_navigation_row
    )
    compact_navigation_layout.setContentsMargins(0, 0, 0, 0)
    compact_navigation_layout.setSpacing(8)
    compact_decision_row = qt_widgets.QWidget(footer)
    compact_decision_layout = qt_widgets.QHBoxLayout(
        compact_decision_row
    )
    compact_decision_layout.setContentsMargins(0, 0, 0, 0)
    compact_decision_layout.setSpacing(8)
    footer_layout.addWidget(wide_action_row)
    footer_layout.addWidget(compact_navigation_row)
    footer_layout.addWidget(compact_decision_row)
    action_buttons: dict[str, object] = {}
    navigation_actions = {
        "return_previous",
        "return_related_conflict",
        "return_to_group",
        "return_to_attribution",
    }
    decision_action_count = sum(
        action not in navigation_actions
        for action in request.actions
    )
    for action in request.actions:
        label = {
            "confirm_selection": "确认选择",
            "confirm_all_conflicts": (
                "确认全部修改"
                if request.editing_existing_decisions
                else "确认全部选择"
            ),
            "confirm_group": "确认整个组",
            "review_books": "逐 Book 审核",
            "reject_group": "拒绝整个组",
            "return_previous": "返回上一步",
            "return_related_conflict": "修改相关冲突",
            "return_to_group": "返回整组确认",
            "return_to_attribution": "返回样品归属",
            "cancel": "取消并退出",
        }.get(action, action)
        button = qt_widgets.QPushButton(label, footer)
        button.setObjectName(
            "dialog_button_danger"
            if action in {"cancel", "reject_group"}
            else (
                "dialog_button_primary"
                if action in {
                    "confirm_selection",
                    "confirm_all_conflicts",
                    "confirm_group",
                }
                else "dialog_button_secondary"
            )
        )
        if (
            action not in navigation_actions
            and decision_action_count >= 3
        ):
            button.setProperty("compact_conflict_action", True)
        configure_workflow_button(button, qt_gui)
        action_buttons[action] = button

    def clear_layout(layout: object) -> None:
        while layout.count():
            layout.takeAt(0)

    def arrange_footer_actions(width: int) -> None:
        if "review_books" in action_buttons:
            action_buttons["review_books"].setText(
                "逐 Book" if width < 400 else "逐 Book 审核"
            )
        for layout in (
            wide_action_layout,
            compact_navigation_layout,
            compact_decision_layout,
        ):
            clear_layout(layout)
        if width >= 700:
            for action in request.actions:
                if action in navigation_actions:
                    wide_action_layout.addWidget(action_buttons[action])
            wide_action_layout.addStretch(1)
            for action in request.actions:
                if action not in navigation_actions:
                    wide_action_layout.addWidget(action_buttons[action])
            wide_action_row.show()
            compact_navigation_row.hide()
            compact_decision_row.hide()
            return
        for action in request.actions:
            if action in navigation_actions:
                compact_navigation_layout.addWidget(action_buttons[action])
        compact_navigation_layout.addStretch(1)
        compact_decision_layout.addStretch(1)
        for action in request.actions:
            if action not in navigation_actions:
                compact_decision_layout.addWidget(action_buttons[action])
        wide_action_row.hide()
        compact_navigation_row.setVisible(
            any(action in navigation_actions for action in request.actions)
        )
        compact_decision_row.show()

    arrange_footer_actions(dialog.width())
    original_resize_event = dialog.resizeEvent

    def resize_event(event: object) -> None:
        original_resize_event(event)
        arrange_footer_actions(dialog.width())
        arrange_candidate_columns(dialog.width())
        candidates.doItemsLayout()

    dialog.resizeEvent = resize_event
    root.addWidget(footer)

    def row_selected_keys() -> tuple[str, ...]:
        keys = []
        for index in range(candidates.topLevelItemCount()):
            item = candidates.topLevelItem(index)
            if item.isSelected():
                keys.append(
                    str(
                        item.data(
                            0,
                            qt_core.Qt.ItemDataRole.UserRole,
                        )
                        or ""
                    )
                )
        return tuple(keys)

    initially_selected = row_selected_keys()
    selected_key_set = set(initially_selected)
    selection_chronology = [
        key
        for key in request.initial_selection
        if key in selected_key_set
    ]
    selection_chronology.extend(
        key
        for key in initially_selected
        if key not in selection_chronology
    )
    previous_selected = selected_key_set

    def track_selection_chronology() -> None:
        nonlocal previous_selected
        row_keys = row_selected_keys()
        current_keys = set(row_keys)
        selection_chronology[:] = [
            key
            for key in selection_chronology
            if key in current_keys
        ]
        newly_selected = current_keys - previous_selected
        current = candidates.currentItem()
        current_key = (
            str(
                current.data(
                    0,
                    qt_core.Qt.ItemDataRole.UserRole,
                )
                or ""
            )
            if current is not None
            else ""
        )
        if current_key in newly_selected:
            selection_chronology.append(current_key)
            newly_selected.remove(current_key)
        selection_chronology.extend(
            key
            for key in row_keys
            if key in newly_selected
        )
        previous_selected = current_keys

    def selected_keys() -> tuple[str, ...]:
        if request.selection_mode != "multi":
            return row_selected_keys()
        track_selection_chronology()
        return tuple(selection_chronology)

    def selected_group_pairs() -> tuple[tuple[str, str], ...]:
        selected_by_group: dict[str, list[str]] = {
            group.group_key: [] for group in request.choice_groups
        }
        for index in range(candidates.topLevelItemCount()):
            item = candidates.topLevelItem(index)
            if not item.isSelected():
                continue
            group_key = str(
                item.data(
                    0,
                    group_key_role,
                )
                or ""
            )
            if group_key in selected_by_group:
                selected_by_group[group_key].append(
                    str(
                        item.data(
                            0,
                            qt_core.Qt.ItemDataRole.UserRole,
                        )
                        or ""
                    )
                )
        pairs = []
        for group in request.choice_groups:
            selected_in_group = selected_by_group[group.group_key]
            if len(selected_in_group) == 1:
                pairs.append((group.group_key, selected_in_group[0]))
        return tuple(pairs)

    def finish(action: str) -> None:
        keys = selected_keys()
        if action == "confirm_all_conflicts":
            grouped_pairs = selected_group_pairs()
            if len(grouped_pairs) != len(request.choice_groups):
                _set_dialog_error(
                    error_text,
                    "每个冲突都必须且只能选择一个候选 Book。",
                )
                candidates.setFocus()
                return
        elif action == "confirm_selection":
            if request.selection_mode == "single" and len(keys) != 1:
                _set_dialog_error(error_text, "必须且只能选择一个候选 Book。")
                candidates.setFocus()
                return
            if request.selection_mode == "multi" and not keys:
                _set_dialog_error(
                    error_text,
                    "至少选择一个候选 Book。",
                )
                candidates.setFocus()
                return
            for exact_group in request.single_select_groups:
                if sum(key in keys for key in exact_group) > 1:
                    _set_dialog_error(
                        error_text,
                        "每个完全重复的激发谱组最多保留一个 Book。",
                    )
                    candidates.setFocus()
                    return
        selected["response"] = ConflictReviewResponse(
            action=action,
            selected_book_keys=keys,
            group_selections=selected_group_pairs(),
            active_group_key=active_group["key"],
            scroll_value=candidates.verticalScrollBar().value(),
        )
        dialog.accept()

    for action, button in action_buttons.items():
        if action == "cancel":
            button.clicked.connect(dialog.reject)
        else:
            button.clicked.connect(lambda _checked=False, action=action: finish(action))
    syncing_exact_group = {"active": False}

    def synchronize_exact_group() -> None:
        _clear_dialog_error(error_text)
        if syncing_exact_group["active"]:
            return
        current = candidates.currentItem()
        if current is None or not current.isSelected():
            return
        current_key = str(
            current.data(0, qt_core.Qt.ItemDataRole.UserRole) or ""
        )
        group_number = duplicate_group_by_key.get(current_key)
        if group_number is None:
            return
        syncing_exact_group["active"] = True
        try:
            for index in range(candidates.topLevelItemCount()):
                item = candidates.topLevelItem(index)
                item_key = str(
                    item.data(0, qt_core.Qt.ItemDataRole.UserRole) or ""
                )
                if (
                    item is not current
                    and duplicate_group_by_key.get(item_key) == group_number
                ):
                    item.setSelected(False)
        finally:
            syncing_exact_group["active"] = False

    def synchronize_grouped_selection(item: object, _column: int) -> None:
        _clear_dialog_error(error_text)
        group_key = str(
            item.data(
                0,
                group_key_role,
            )
            or ""
        )
        if not group_key:
            return
        active_group["key"] = group_key
        syncing_exact_group["active"] = True
        try:
            item.setSelected(True)
            for index in range(candidates.topLevelItemCount()):
                other = candidates.topLevelItem(index)
                if (
                    other is not item
                    and str(
                        other.data(
                            0,
                            group_key_role,
                        )
                        or ""
                    )
                    == group_key
                ):
                    other.setSelected(False)
        finally:
            syncing_exact_group["active"] = False
        update_common_detail()
        candidates.viewport().update()

    def synchronize_ungrouped_selection() -> None:
        track_selection_chronology()
        synchronize_exact_group()

    if grouped_selection:
        candidates.itemClicked.connect(synchronize_grouped_selection)
    else:
        candidates.itemSelectionChanged.connect(
            synchronize_ungrouped_selection
        )
    initial_item = next(
        (
            candidates.topLevelItem(index)
            for index in range(candidates.topLevelItemCount())
            if candidates.topLevelItem(index).isSelected()
            and (
                not grouped_selection
                or str(
                    candidates.topLevelItem(index).data(
                        0,
                        group_key_role,
                    )
                    or ""
                )
                == active_group["key"]
            )
        ),
        (
            candidates.topLevelItem(0)
            if candidates.topLevelItemCount()
            else None
        ),
    )
    if initial_item is not None:
        candidates.setCurrentItem(
            initial_item,
            0,
            qt_core.QItemSelectionModel.SelectionFlag.NoUpdate,
        )
    if request.selection_mode == "single" and "confirm_selection" in action_buttons:
        candidates.itemDoubleClicked.connect(
            lambda _item, _column: finish("confirm_selection")
        )
        candidates.itemActivated.connect(
            lambda _item, _column: finish("confirm_selection")
        )
    _ignore_escape_key(dialog, qt_core)
    _enable_title_bar_drag(header, dialog, qt_core)
    candidates.setFocus()
    root.activate()
    body_layout.activate()
    natural_height = root.sizeHint().height()
    dialog.resize(
        dialog.width(),
        min(
            max(740, natural_height),
            740,
            max(1, available.height() - safe_margin),
        ),
    )
    root.setSizeConstraint(qt_widgets.QLayout.SizeConstraint.SetNoConstraint)
    candidates.setMinimumHeight(50)
    detail.setMinimumHeight(0)
    dialog.setMinimumSize(1, 1)
    candidates.doItemsLayout()
    if grouped_selection:
        candidates.verticalScrollBar().setValue(
            max(0, request.initial_scroll_value)
        )

    try:
        _make_windows_taskbar_window(dialog)
        result = _run_topmost_nonmodal_dialog(dialog, qt_core)
        response = selected["response"]
        if result == qt_widgets.QDialog.DialogCode.Accepted and isinstance(
            response,
            ConflictReviewResponse,
        ):
            return response
        return ConflictReviewResponse(action="cancel")
    finally:
        _dispose_nonmodal_dialog(dialog, qt_core)


def show_attribution_dialog(
    request: AttributionDialogRequest,
    *,
    parent: object | None = None,
) -> AttributionDialogResponse:
    qt_widgets, qt_core = _load_qt_modules()
    qt_gui = _load_qt_gui()
    _ensure_application(qt_widgets)

    dialog = qt_widgets.QDialog(parent)
    dialog.setObjectName("organizer_dialog")
    dialog.setWindowTitle("确认样品归属")
    dialog.setModal(False)
    dialog.setWindowModality(qt_core.Qt.WindowModality.NonModal)
    dialog.setWindowFlags(
        qt_core.Qt.WindowType.Tool
        | qt_core.Qt.WindowType.FramelessWindowHint
        | qt_core.Qt.WindowType.WindowStaysOnTopHint
    )
    apply_styled_dialog_chrome(dialog, qt_core)
    dialog.setStyleSheet(ORGANIZER_DIALOG_STYLE_SHEET)
    dialog.setFont(_font(qt_gui, 13))
    safe_margin = 24
    available = dialog.screen().availableGeometry()
    max_dialog_width = max(1, available.width() - safe_margin)
    use_two_column_form = available.height() < 700 and max_dialog_width >= 700
    dialog_width = min(720 if use_two_column_form else 560, max_dialog_width)
    dialog.setFixedWidth(dialog_width)
    dialog.setMaximumHeight(max(1, available.height() - safe_margin))
    root = qt_widgets.QVBoxLayout(dialog)
    root.setContentsMargins(0, 0, 0, 0)
    root.setSpacing(0)
    header = qt_widgets.QFrame(dialog)
    header.setObjectName("dialog_header")
    header.setFixedHeight(50)
    header_layout = qt_widgets.QHBoxLayout(header)
    header_layout.setContentsMargins(16, 0, 10, 0)
    title = qt_widgets.QLabel("确认样品归属", header)
    title.setObjectName("dialog_title")
    title.setFont(_font(qt_gui, 15, bold=True))
    close_button = qt_widgets.QPushButton("×", header)
    close_button.setObjectName("dialog_close_button")
    close_button.setFixedSize(28, 26)
    close_button.setFocusPolicy(qt_core.Qt.FocusPolicy.NoFocus)
    header_layout.addWidget(title, 1)
    header_layout.addWidget(close_button)
    root.addWidget(header)

    body = qt_widgets.QFrame(dialog)
    body.setObjectName("dialog_body")
    body.setSizePolicy(
        qt_widgets.QSizePolicy.Policy.Preferred,
        qt_widgets.QSizePolicy.Policy.Minimum,
    )
    body_layout = qt_widgets.QVBoxLayout(body)
    body_layout.setContentsMargins(20, 16, 20, 16)
    body_layout.setSpacing(12)
    body_layout.setSizeConstraint(qt_widgets.QLayout.SizeConstraint.SetDefaultConstraint)
    attribution_context = qt_widgets.QWidget(body)
    attribution_context_layout = qt_widgets.QGridLayout(attribution_context)
    attribution_context_layout.setContentsMargins(0, 0, 0, 0)
    attribution_context_layout.setHorizontalSpacing(10)
    attribution_context_layout.setVerticalSpacing(4)

    WrapAnywhereLabel = _wrap_anywhere_label_type(qt_widgets, qt_core, qt_gui)

    source_caption = qt_widgets.QLabel("原始文件", attribution_context)
    source_caption.setObjectName("dialog_help_text")
    source_name = WrapAnywhereLabel(request.source_filename, attribution_context)
    source_name.setObjectName("attribution_source_name")
    source_name.setFont(_font(qt_gui, 13, bold=True))
    folder_caption = qt_widgets.QLabel("Folder", attribution_context)
    folder_caption.setObjectName("dialog_help_text")
    folder_name = WrapAnywhereLabel(request.target_label, attribution_context)
    folder_name.setObjectName("attribution_folder_name")
    folder_name.setFont(_font(qt_gui, 13, bold=True))
    for field, full_text in (
        (source_name, request.source_filename),
        (folder_name, request.target_label),
    ):
        field.setWordWrap(True)
        field.setMinimumWidth(0)
        field.setSizePolicy(
            qt_widgets.QSizePolicy.Policy.Ignored,
            qt_widgets.QSizePolicy.Policy.Preferred,
        )
        field.setToolTip(full_text)

    included_books = WrapAnywhereLabel(
        f"包含：{', '.join(request.book_display_names)}",
        attribution_context,
    )
    included_books.setObjectName("attribution_included_books")
    included_books.setWordWrap(True)
    included_books.setMinimumWidth(0)
    included_books.setSizePolicy(
        qt_widgets.QSizePolicy.Policy.Ignored,
        qt_widgets.QSizePolicy.Policy.Preferred,
    )
    selected_book = WrapAnywhereLabel(
        (
            f"当前修改：{request.selected_book_display_name}"
            if request.targeted_correction
            else ""
        ),
        attribution_context,
    )
    selected_book.setObjectName("attribution_selected_book")
    selected_book.setWordWrap(True)
    selected_book.setVisible(request.targeted_correction)
    attribution_context_layout.addWidget(source_caption, 0, 0)
    attribution_context_layout.addWidget(source_name, 0, 1)
    attribution_context_layout.addWidget(folder_caption, 1, 0)
    attribution_context_layout.addWidget(folder_name, 1, 1)
    attribution_context_layout.addWidget(included_books, 2, 0, 1, 2)
    attribution_context_layout.addWidget(selected_book, 3, 0, 1, 2)
    attribution_context_layout.setColumnStretch(1, 1)
    body_layout.addWidget(attribution_context)

    form_container = qt_widgets.QWidget(body)
    form = qt_widgets.QGridLayout(form_container)
    form.setObjectName("attribution_form_layout")
    form.setContentsMargins(0, 0, 0, 0)
    form.setHorizontalSpacing(12 if use_two_column_form else 14)
    form.setVerticalSpacing(8 if use_two_column_form else 10)
    label_width = 92 if use_two_column_form else 108
    form.setColumnMinimumWidth(0, label_width)
    form.setColumnStretch(1, 1)
    if use_two_column_form:
        form.setColumnMinimumWidth(2, label_width)
        form.setColumnStretch(3, 1)
    sample_type = qt_widgets.QComboBox(body)
    sample_type.addItem("请选择样品类型", "")
    sample_type.addItem("溶液样品", "solution")
    sample_type.addItem("固体样品", "solid")
    sample_type.addItem("主客体掺杂固体", "doped")
    apply_combo_popup_palette(sample_type, qt_gui)
    sample_type_label = _form_label(qt_widgets, "样品类型", form_container)

    attribution_mode_row = qt_widgets.QWidget(body)
    attribution_mode_layout = qt_widgets.QHBoxLayout(attribution_mode_row)
    attribution_mode_layout.setContentsMargins(0, 0, 0, 0)
    attribution_mode_layout.setSpacing(8)
    attribution_mode_group = qt_widgets.QButtonGroup(attribution_mode_row)
    attribution_mode_group.setExclusive(True)
    folder_mode_button = qt_widgets.QPushButton("整个 Folder", attribution_mode_row)
    folder_mode_button.setObjectName("attribution_folder_mode")
    configure_embedded_choice_button(folder_mode_button, qt_gui)
    folder_mode_button.setCheckable(True)
    folder_mode_button.setChecked(
        not request.targeted_correction
        or request.initial_scope != "book"
    )
    book_mode_button = qt_widgets.QPushButton("逐 Book", attribution_mode_row)
    book_mode_button.setObjectName("attribution_book_mode")
    configure_embedded_choice_button(book_mode_button, qt_gui)
    book_mode_button.setCheckable(True)
    book_mode_button.setChecked(
        request.targeted_correction
        and request.initial_scope == "book"
    )
    attribution_mode_group.addButton(folder_mode_button)
    attribution_mode_group.addButton(book_mode_button)
    attribution_mode_layout.addWidget(folder_mode_button, 1)
    attribution_mode_layout.addWidget(book_mode_button, 1)
    attribution_mode_label = _form_label(qt_widgets, "归属方式", form_container)
    attribution_mode_label.setVisible(request.allow_split_folder)
    attribution_mode_row.setVisible(request.allow_split_folder)

    targeted_scope_notice = qt_widgets.QLabel("", body)
    targeted_scope_notice.setObjectName("attribution_targeted_scope_notice")
    targeted_scope_notice.setWordWrap(True)
    targeted_scope_notice.setText(
        "确认后将更新本 Folder 内 "
        f"{request.affected_book_count or len(request.book_display_names)} 个 Book"
    )
    targeted_scope_notice.setVisible(
        request.targeted_correction
        and request.allow_split_folder
        and folder_mode_button.isChecked()
    )

    fields = {
        "sample": qt_widgets.QLineEdit(body),
        "solvent": qt_widgets.QLineEdit(body),
        "host": qt_widgets.QLineEdit(body),
        "concentration": qt_widgets.QLineEdit(body),
        "state": qt_widgets.QLineEdit(body),
        "temperature": qt_widgets.QLineEdit(body),
    }
    guidance = {
        "sample": "例如：NDI",
        "solvent": "例如：DCM",
        "host": "请输入主体成分",
        "concentration": "例如：1×10^-4",
        "state": "例如：Solid、Film 或 Crystal",
        "temperature": "例如：RT、77 K 或 298",
    }
    for name, field in fields.items():
        field.setPlaceholderText(guidance[name])
        field.setToolTip(guidance[name])
    unit = qt_widgets.QComboBox(body)
    unit.setToolTip("溶液固定为 M；主客体掺杂须选择 wt% 或 mol%")
    apply_combo_popup_palette(unit, qt_gui)
    concentration_row = qt_widgets.QWidget(body)
    concentration_layout = qt_widgets.QHBoxLayout(concentration_row)
    concentration_layout.setContentsMargins(0, 0, 0, 0)
    concentration_layout.setSpacing(8)
    concentration_layout.addWidget(fields["concentration"], 1)
    concentration_layout.addWidget(unit, 0)
    oxygen_environment_row = qt_widgets.QWidget(body)
    oxygen_environment_row.setObjectName("oxygen_environment_selector")
    oxygen_environment_layout = qt_widgets.QHBoxLayout(oxygen_environment_row)
    oxygen_environment_layout.setContentsMargins(0, 0, 0, 0)
    oxygen_environment_layout.setSpacing(8)
    oxygen_environment_group = qt_widgets.QButtonGroup(oxygen_environment_row)
    oxygen_environment_group.setExclusive(True)
    oxygen_environment_air = qt_widgets.QPushButton("空气中", oxygen_environment_row)
    oxygen_environment_air.setObjectName("oxygen_environment_air")
    configure_embedded_choice_button(oxygen_environment_air, qt_gui)
    oxygen_environment_air.setCheckable(True)
    oxygen_environment_air.setFocusPolicy(qt_core.Qt.FocusPolicy.StrongFocus)
    oxygen_environment_deo2 = qt_widgets.QPushButton("绝氧", oxygen_environment_row)
    oxygen_environment_deo2.setObjectName("oxygen_environment_deo2")
    configure_embedded_choice_button(oxygen_environment_deo2, qt_gui)
    oxygen_environment_deo2.setCheckable(True)
    oxygen_environment_deo2.setFocusPolicy(qt_core.Qt.FocusPolicy.StrongFocus)
    oxygen_environment_group.addButton(oxygen_environment_air)
    oxygen_environment_group.addButton(oxygen_environment_deo2)
    oxygen_environment_layout.addWidget(oxygen_environment_air, 1)
    oxygen_environment_layout.addWidget(oxygen_environment_deo2, 1)
    rows = {
        "sample": (_form_label(qt_widgets, "样品名称", form_container), fields["sample"]),
        "solvent": (_form_label(qt_widgets, "溶剂或状态", form_container), fields["solvent"]),
        "host": (_form_label(qt_widgets, "主体成分", form_container), fields["host"]),
        "concentration": (_form_label(qt_widgets, "浓度", form_container), concentration_row),
        "state": (_form_label(qt_widgets, "固体状态", form_container), fields["state"]),
        "oxygen_environment": (
            _form_label(qt_widgets, "测量环境", form_container),
            oxygen_environment_row,
        ),
        "temperature": (_form_label(qt_widgets, "温度", form_container), fields["temperature"]),
    }
    form.addWidget(sample_type_label, 0, 0)
    form.addWidget(sample_type, 0, 1)
    if use_two_column_form:
        form.addWidget(attribution_mode_label, 0, 2)
        form.addWidget(attribution_mode_row, 0, 3)
    else:
        form.addWidget(attribution_mode_label, 1, 0)
        form.addWidget(attribution_mode_row, 1, 1)
        for row_index, (label, widget) in enumerate(rows.values(), start=2):
            form.addWidget(label, row_index, 0)
            form.addWidget(widget, row_index, 1)
    form_labels = [sample_type_label, attribution_mode_label, *(label for label, _widget in rows.values())]
    for label in form_labels:
        label.setAlignment(qt_core.Qt.AlignmentFlag.AlignRight | qt_core.Qt.AlignmentFlag.AlignVCenter)
        label.setFixedWidth(label_width)
    body_layout.addWidget(form_container)

    apply_remaining = qt_widgets.QCheckBox("将本次归属应用到此 Folder 中其余未确认 Book", body)
    apply_remaining.setVisible(
        request.allow_apply_to_remaining_folder
        and not request.targeted_correction
    )
    body_layout.addWidget(apply_remaining)
    body_layout.addWidget(targeted_scope_notice)
    help_text = qt_widgets.QLabel(
        "浓度可输入普通数字或科学计数法；温度可输入 RT、77 K 或数值；样品信息不可输入换行。",
        body,
    )
    help_text.setObjectName("dialog_help_text")
    help_text.setWordWrap(True)
    error_text = qt_widgets.QLabel("", body)
    error_text.setObjectName("dialog_error_text")
    error_text.setWordWrap(True)
    error_text.hide()
    body_layout.addWidget(help_text)
    body_layout.addWidget(error_text)

    def set_error(message: str) -> None:
        help_text.hide()
        _set_dialog_error(error_text, message)

    def clear_error(*_args: object) -> None:
        help_text.show()
        _clear_dialog_error(error_text)

    selected: dict[str, object] = {"response": None}
    return_previous_button = qt_widgets.QPushButton("返回上一步", body)
    return_previous_button.setObjectName("dialog_button_secondary")
    configure_workflow_button(return_previous_button, qt_gui)
    return_previous_button.setVisible(request.allow_return_previous)
    return_to_book_picker_button = qt_widgets.QPushButton("返回选择 Book", body)
    return_to_book_picker_button.setObjectName("dialog_button_secondary")
    configure_workflow_button(return_to_book_picker_button, qt_gui)
    return_to_book_picker_button.setVisible(request.allow_return_to_book_picker)
    confirm_button = qt_widgets.QPushButton(
        "确认修改" if request.targeted_correction else "确认",
        body,
    )
    confirm_button.setObjectName("dialog_button_primary")
    configure_workflow_button(confirm_button, qt_gui)
    cancel_button = qt_widgets.QPushButton("取消并退出", body)
    cancel_button.setObjectName("dialog_button_danger")
    configure_workflow_button(cancel_button, qt_gui)

    action_rows = qt_widgets.QWidget(body)
    action_rows_layout = qt_widgets.QVBoxLayout(action_rows)
    action_rows_layout.setContentsMargins(0, 0, 0, 0)
    action_rows_layout.setSpacing(8)
    wide_action_row = qt_widgets.QWidget(action_rows)
    wide_action_layout = qt_widgets.QHBoxLayout(wide_action_row)
    wide_action_layout.setContentsMargins(0, 0, 0, 0)
    wide_action_layout.setSpacing(8)
    compact_navigation_row = qt_widgets.QWidget(action_rows)
    compact_navigation_layout = qt_widgets.QHBoxLayout(
        compact_navigation_row
    )
    compact_navigation_layout.setContentsMargins(0, 0, 0, 0)
    compact_navigation_layout.setSpacing(8)
    compact_decision_row = qt_widgets.QWidget(action_rows)
    compact_decision_layout = qt_widgets.QHBoxLayout(
        compact_decision_row
    )
    compact_decision_layout.setContentsMargins(0, 0, 0, 0)
    compact_decision_layout.setSpacing(8)
    action_rows_layout.addWidget(wide_action_row)
    action_rows_layout.addWidget(compact_navigation_row)
    action_rows_layout.addWidget(compact_decision_row)
    body_layout.addWidget(action_rows)

    def clear_action_layout(layout: object) -> None:
        while layout.count():
            layout.takeAt(0)

    def arrange_attribution_actions(width: int) -> None:
        for layout in (
            wide_action_layout,
            compact_navigation_layout,
            compact_decision_layout,
        ):
            clear_action_layout(layout)
        navigation_buttons = (
            (return_previous_button, request.allow_return_previous),
            (
                return_to_book_picker_button,
                request.allow_return_to_book_picker,
            ),
        )
        if width >= 460:
            for button, enabled in navigation_buttons:
                if enabled:
                    wide_action_layout.addWidget(button)
            wide_action_layout.addStretch(1)
            wide_action_layout.addWidget(confirm_button)
            wide_action_layout.addWidget(cancel_button)
            wide_action_row.show()
            compact_navigation_row.hide()
            compact_decision_row.hide()
            return
        for button, enabled in navigation_buttons:
            if enabled:
                compact_navigation_layout.addWidget(button)
        compact_navigation_layout.addStretch(1)
        compact_decision_layout.addStretch(1)
        compact_decision_layout.addWidget(confirm_button)
        compact_decision_layout.addWidget(cancel_button)
        wide_action_row.hide()
        compact_navigation_row.setVisible(
            any(enabled for _button, enabled in navigation_buttons)
        )
        compact_decision_row.show()

    arrange_attribution_actions(dialog.width())
    body_scroll = qt_widgets.QScrollArea(dialog)
    body_scroll.setObjectName("attribution_body_scroll")
    body_scroll.setFrameShape(qt_widgets.QFrame.Shape.NoFrame)
    body_scroll.setWidgetResizable(True)
    body_scroll.setHorizontalScrollBarPolicy(
        qt_core.Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    )
    body_scroll.setVerticalScrollBarPolicy(
        qt_core.Qt.ScrollBarPolicy.ScrollBarAsNeeded
    )
    root.addWidget(body_scroll, 1)

    previous_type = {"value": ""}
    rejected_doped_prefill_unit = (
        request.prefill.get("sample_type", "") == "doped"
        and request.prefill.get("concentration_unit", "") not in {"wt%", "mol%"}
        and not (
            request.prefill_source == "unconfirmed_draft"
            and request.prefill.get("concentration_unit", "") == ""
        )
    )
    pending_prefill_unit = {
        "value": request.prefill.get("concentration_unit", "")
        if request.prefill.get("sample_type", "") == "doped"
        and request.prefill.get("concentration_unit", "") in {"wt%", "mol%"}
        else ""
    }
    solution_concentration = (
        str(request.prefill.get("solution_concentration", "")).strip()
        or "1×10^-4"
    )
    doped_concentration = str(
        request.prefill.get("doped_concentration", "")
    ).strip()
    doped_concentration_unit = str(
        request.prefill.get("doped_concentration_unit", "")
    ).strip()
    required_fields = {
        "solution": ("sample", "solvent", "concentration", "temperature"),
        "solid": ("sample", "state", "oxygen_environment", "temperature"),
        "doped": (
            "sample",
            "host",
            "concentration",
            "state",
            "oxygen_environment",
            "temperature",
        ),
    }
    layout_state = {"two_column": use_two_column_form}
    stable_dialog_size = {"value": None}

    def oxygen_environment_value() -> str:
        if oxygen_environment_air.isChecked():
            return "Air"
        if oxygen_environment_deo2.isChecked():
            return "DeO2"
        return ""

    def set_oxygen_environment_value(value: str) -> None:
        canonical = str(value or "").strip()
        oxygen_environment_group.setExclusive(False)
        oxygen_environment_air.setChecked(canonical == "Air")
        oxygen_environment_deo2.setChecked(canonical == "DeO2")
        oxygen_environment_group.setExclusive(True)

    def set_unit_value(value: str) -> None:
        index = unit.findData(value)
        unit.setCurrentIndex(index)

    def replace_unit_options(options: tuple[str, ...], selected_value: str = "") -> None:
        blocker = qt_core.QSignalBlocker(unit)
        unit.clear()
        for option in options:
            unit.addItem(option, option)
        if selected_value:
            set_unit_value(selected_value)
        else:
            unit.setCurrentIndex(-1)
        del blocker

    def canonicalize_concentration() -> bool:
        current_type = str(sample_type.currentData() or "")
        text = fields["concentration"].text().strip()
        if not text or current_type not in {"solution", "doped"}:
            return True
        selected_unit = "M" if current_type == "solution" else str(unit.currentData() or "") or None
        allowed_units = ("M",) if current_type == "solution" else ("wt%", "mol%")
        try:
            entry = normalize_concentration_input(text, selected_unit, allowed_units)
        except (ValueError, TypeError) as exc:
            set_error(_attribution_validation_message(exc))
            return False
        fields["concentration"].setText(entry.value_text)
        set_unit_value(entry.unit)
        clear_error()
        return True

    def focus_field(name: str) -> None:
        if name == "oxygen_environment":
            focus_and_reveal(oxygen_environment_air)
        elif (
            name == "concentration"
            and str(sample_type.currentData() or "") == "doped"
            and not str(unit.currentData() or "")
        ):
            focus_and_reveal(unit)
        else:
            focus_and_reveal(fields[name])

    def focus_and_reveal(widget: object) -> None:
        widget.setFocus()

        def reveal_after_layout() -> None:
            try:
                if error_text.isVisible():
                    resize_to_visible_content()
                body_layout.invalidate()
                body_layout.activate()
                body_scroll.ensureWidgetVisible(widget, 16, 16)
                if error_text.text():
                    content = body_scroll.widget()
                    viewport_height = body_scroll.viewport().height()
                    widget_top = widget.mapTo(content, qt_core.QPoint(0, 0)).y()
                    error_top = error_text.mapTo(content, qt_core.QPoint(0, 0)).y()
                    minimum_scroll = max(
                        widget_top + widget.height() - viewport_height,
                        error_top + error_text.height() - viewport_height,
                    )
                    maximum_scroll = min(widget_top, error_top)
                    if minimum_scroll <= maximum_scroll:
                        scrollbar = body_scroll.verticalScrollBar()
                        scrollbar.setValue(
                            max(
                                minimum_scroll,
                                min(scrollbar.value(), maximum_scroll),
                            )
                        )
            except RuntimeError:
                return

        reveal_after_layout()
        qt_core.QTimer.singleShot(0, reveal_after_layout)

    def reveal_error() -> None:
        def reveal_after_layout() -> None:
            try:
                resize_to_visible_content()
                body_layout.invalidate()
                body_layout.activate()
                body_scroll.ensureWidgetVisible(error_text, 16, 16)
            except RuntimeError:
                return

        reveal_after_layout()
        qt_core.QTimer.singleShot(0, reveal_after_layout)

    def reveal_validation_error(name: str, *, focus_on_error: bool) -> None:
        if focus_on_error:
            focus_field(name)
        else:
            reveal_error()

    def validate_field(name: str, *, focus_on_error: bool = False) -> bool:
        current_type = str(sample_type.currentData() or "")
        if name not in required_fields.get(current_type, ()):
            return True
        if name == "oxygen_environment":
            if oxygen_environment_value():
                clear_error()
                return True
            set_error("请选择测量环境。")
            reveal_validation_error(name, focus_on_error=focus_on_error)
            return False
        text = fields[name].text().strip()
        try:
            if not text:
                raise ValueError(f"{name} is required")
            if name == "concentration":
                if not canonicalize_concentration():
                    reveal_validation_error(name, focus_on_error=focus_on_error)
                    return False
            elif name == "temperature":
                normalize_temperature(text)
            else:
                validate_user_origin_name_text(text, field_name=name)
        except (ValueError, TypeError) as exc:
            set_error(_attribution_validation_message(exc))
            reveal_validation_error(name, focus_on_error=focus_on_error)
            return False
        clear_error()
        return True

    def reflow_form_layout(two_column: bool) -> None:
        if layout_state["two_column"] == two_column:
            return
        for widget in (
            sample_type_label,
            sample_type,
            attribution_mode_label,
            attribution_mode_row,
        ):
            form.removeWidget(widget)
        for label, widget in rows.values():
            form.removeWidget(label)
            form.removeWidget(widget)

        label_width = 92 if two_column else 108
        form.setHorizontalSpacing(12 if two_column else 14)
        form.setVerticalSpacing(8 if two_column else 10)
        for column in range(4):
            form.setColumnMinimumWidth(column, 0)
            form.setColumnStretch(column, 0)
        form.setColumnMinimumWidth(0, label_width)
        form.setColumnStretch(1, 1)
        form.addWidget(sample_type_label, 0, 0)
        form.addWidget(sample_type, 0, 1)
        if two_column:
            form.setColumnMinimumWidth(2, label_width)
            form.setColumnStretch(3, 1)
            form.addWidget(attribution_mode_label, 0, 2)
            form.addWidget(attribution_mode_row, 0, 3)
        else:
            form.addWidget(attribution_mode_label, 1, 0)
            form.addWidget(attribution_mode_row, 1, 1)
            for row_index, (label, widget) in enumerate(rows.values(), start=2):
                form.addWidget(label, row_index, 0)
                form.addWidget(widget, row_index, 1)
        for label in form_labels:
            label.setFixedWidth(label_width)

        layout_state["two_column"] = two_column

    def set_row_visibility(current_type: str) -> None:
        visible_names = required_fields.get(current_type, ())
        if layout_state["two_column"]:
            for label, widget in rows.values():
                form.removeWidget(label)
                form.removeWidget(widget)
                label.hide()
                widget.hide()
            for index, name in enumerate(visible_names):
                label, widget = rows[name]
                label_column = 0 if index % 2 == 0 else 2
                form.addWidget(label, 1 + index // 2, label_column)
                form.addWidget(widget, 1 + index // 2, label_column + 1)
                label.show()
                widget.show()
            form.invalidate()
            form.activate()
            return
        visible = set(visible_names)
        for name, (label, widget) in rows.items():
            label.setVisible(name in visible)
            widget.setVisible(name in visible)

    def set_type_rows(current_type: str) -> None:
        set_row_visibility(current_type)
        if current_type == "solution":
            fields["concentration"].setPlaceholderText("例如：1×10^-4 或 0.1")
            replace_unit_options(("M",), "M")
            unit.setEnabled(False)
            if not fields["concentration"].text().strip():
                fields["concentration"].setText(solution_concentration)
        elif current_type == "doped":
            fields["concentration"].setPlaceholderText("例如：10、1.5 或 0")
            unit.setEnabled(True)
            prefilled_unit = pending_prefill_unit["value"]
            pending_prefill_unit["value"] = ""
            if (
                not fields["concentration"].text().strip()
                and doped_concentration
                and doped_concentration_unit in {"wt%", "mol%"}
            ):
                fields["concentration"].setText(doped_concentration)
                prefilled_unit = doped_concentration_unit
            replace_unit_options(("wt%", "mol%"), prefilled_unit)
        else:
            unit.setEnabled(False)
            replace_unit_options(())

    def refresh_sample_type_choices() -> None:
        current_type = str(sample_type.currentData() or "")
        for index in range(sample_type.count()):
            item_type = str(sample_type.itemData(index) or "")
            hidden = not item_type or item_type == current_type
            sample_type.view().setRowHidden(index, hidden)
            item = sample_type.model().item(index)
            item.setEnabled(not hidden)
            item.setSelectable(not hidden)

    def request_per_book_attribution() -> None:
        if request.targeted_correction:
            targeted_scope_notice.hide()
            return
        selected["response"] = AttributionDialogResponse(action="split_folder", split_folder=True)
        dialog.accept()

    def select_folder_scope() -> None:
        if request.targeted_correction:
            targeted_scope_notice.setVisible(request.allow_split_folder)

    def return_to_book_picker() -> None:
        selected["response"] = AttributionDialogResponse(action="return_to_book_picker")
        dialog.accept()

    def return_to_previous() -> None:
        selected["response"] = AttributionDialogResponse(action="return_previous")
        dialog.accept()

    def cancel_with_draft() -> None:
        current_type = str(sample_type.currentData() or "")
        names = required_fields.get(current_type, ())
        values = {
            name: (
                oxygen_environment_value()
                if name == "oxygen_environment"
                else fields[name].text()
            )
            for name in names
        }
        if current_type == "doped":
            values["concentration_unit"] = str(unit.currentData() or "")
        selected["response"] = AttributionDialogResponse(
            action="cancel",
            sample_type=current_type,
            values=values,
            attribution_scope=(
                "folder" if folder_mode_button.isChecked() else "book"
            ),
        )
        dialog.reject()

    def change_type(_index: int) -> None:
        current_type = str(sample_type.currentData() or "")
        old_type = previous_type["value"]
        if old_type and current_type != old_type:
            old_specific = {
                "solution": ("solvent", "concentration"),
                "solid": ("state",),
                "doped": ("host", "concentration", "state"),
            }[old_type]
            for name in old_specific:
                if name == "state" and {old_type, current_type} <= {"solid", "doped"}:
                    continue
                fields[name].clear()
        if current_type == "solution":
            set_oxygen_environment_value("")
        previous_type["value"] = current_type
        set_type_rows(current_type)
        refresh_sample_type_choices()
        clear_error()

    def confirm() -> None:
        current_type = str(sample_type.currentData() or "")
        if not current_type:
            set_error("请先选择样品类型。")
            focus_and_reveal(sample_type)
            return
        names = required_fields[current_type]
        for name in names:
            if not validate_field(name, focus_on_error=True):
                return
        values = {
            name: (
                oxygen_environment_value()
                if name == "oxygen_environment"
                else fields[name].text().strip()
            )
            for name in names
        }
        if current_type == "doped":
            values["concentration_unit"] = str(unit.currentData() or "")
        try:
            build_attribution_fields(current_type, values)
        except (ValueError, TypeError) as exc:
            focus_name = names[0]
            message = _attribution_validation_message(exc)
            if _is_combined_sample_label_length_error(exc):
                editable_names = tuple(name for name in names if name != "oxygen_environment")
                focus_name = max(editable_names, key=lambda name: len(values[name]))
                field_label = rows[focus_name][0].text()
                message = (
                    "样品信息组合后超过 Origin 名称长度上限，"
                    f"请缩短{field_label}。"
                )
            set_error(message)
            focus_field(focus_name)
            return
        attribution_scope = ""
        if request.targeted_correction:
            attribution_scope = (
                "folder" if folder_mode_button.isChecked() else "book"
            )
        selected["response"] = AttributionDialogResponse(
            action="confirm",
            sample_type=current_type,
            values=values,
            apply_to_remaining_folder=apply_remaining.isVisible() and apply_remaining.isChecked(),
            attribution_scope=attribution_scope,
        )
        dialog.accept()

    for name, value in request.prefill.items():
        if name in fields:
            if rejected_doped_prefill_unit and name == "concentration":
                continue
            fields[name].setText(value)
    set_oxygen_environment_value(request.prefill.get("oxygen_environment", ""))
    sample_type.currentIndexChanged.connect(change_type)
    book_mode_button.clicked.connect(request_per_book_attribution)
    folder_mode_button.clicked.connect(select_folder_scope)
    return_previous_button.clicked.connect(return_to_previous)
    return_to_book_picker_button.clicked.connect(return_to_book_picker)
    for name, field in fields.items():
        field.textChanged.connect(clear_error)
        field.editingFinished.connect(lambda name=name: validate_field(name))
    unit.currentIndexChanged.connect(clear_error)
    oxygen_environment_air.clicked.connect(clear_error)
    oxygen_environment_deo2.clicked.connect(clear_error)
    initial_type = request.prefill.get("sample_type", "")
    if initial_type:
        sample_type.setCurrentIndex(sample_type.findData(initial_type))
    else:
        set_type_rows("")
        refresh_sample_type_choices()
    confirm_button.clicked.connect(confirm)
    cancel_button.clicked.connect(
        cancel_with_draft
        if request.targeted_correction
        else dialog.reject
    )
    close_button.clicked.connect(
        return_to_previous
        if request.targeted_correction
        else dialog.reject
    )
    confirm_button.setDefault(True)
    if request.targeted_correction:
        def targeted_close_event(event: object) -> None:
            event.ignore()
            return_to_previous()

        dialog.closeEvent = targeted_close_event

        class TargetedBackFilter(qt_core.QObject):
            def eventFilter(self, watched: object, event: object) -> bool:
                if (
                    event.type() == qt_core.QEvent.Type.KeyPress
                    and event.key() == qt_core.Qt.Key.Key_Escape
                ):
                    return_to_previous()
                    return True
                return False

        targeted_back_filter = TargetedBackFilter(dialog)
        dialog._targeted_back_filter = targeted_back_filter
        dialog.installEventFilter(targeted_back_filter)
    else:
        _ignore_escape_key(dialog, qt_core)
    _enable_title_bar_drag(header, dialog, qt_core)

    current_type = str(sample_type.currentData() or "")
    body.setMinimumSize(0, 0)
    body_scroll.setWidget(body)
    body_layout.setSizeConstraint(qt_widgets.QLayout.SizeConstraint.SetMinAndMaxSize)

    def expand_to_remove_avoidable_scroll() -> None:
        try:
            overflow = body_scroll.verticalScrollBar().maximum()
            if overflow <= 0:
                return
            available = dialog.screen().availableGeometry()
            inset = safe_margin // 2
            safe_area = available.adjusted(
                inset,
                inset,
                -inset,
                -inset,
            )
            available_height = max(1, safe_area.height())
            expanded_height = min(
                available_height,
                dialog.height() + overflow + 1,
            )
            if expanded_height > dialog.height():
                dialog.resize(dialog.width(), expanded_height)
            frame = dialog.frameGeometry()
            target_x = min(
                max(frame.left(), safe_area.left()),
                max(safe_area.left(), safe_area.right() - frame.width() + 1),
            )
            target_y = min(
                max(frame.top(), safe_area.top()),
                max(safe_area.top(), safe_area.bottom() - frame.height() + 1),
            )
            if (target_x, target_y) != (frame.left(), frame.top()):
                dialog.move(
                    dialog.pos()
                    + qt_core.QPoint(
                        target_x - frame.left(),
                        target_y - frame.top(),
                    )
                )
        except RuntimeError:
            return

    def resize_to_visible_content() -> None:
        try:
            available = dialog.screen().availableGeometry()
            max_dialog_width = max(1, available.width() - safe_margin)
            live_two_column = (
                available.height() < 700 and max_dialog_width >= 700
            )
            if live_two_column != layout_state["two_column"]:
                reflow_form_layout(live_two_column)
                set_row_visibility(str(sample_type.currentData() or ""))
            preferred_dialog_width = 720 if live_two_column else 560
            current_dialog_width = min(preferred_dialog_width, max_dialog_width)
            dialog.setFixedWidth(current_dialog_width)
            dialog.setMaximumHeight(max(1, available.height() - safe_margin))
            arrange_attribution_actions(current_dialog_width)
            attribution_mode_layout.setDirection(
                qt_widgets.QBoxLayout.Direction.TopToBottom
                if current_dialog_width < 460
                else qt_widgets.QBoxLayout.Direction.LeftToRight
            )
            horizontal_body_margin = 10 if current_dialog_width < 560 else 20
            body_layout.setContentsMargins(
                horizontal_body_margin,
                16,
                horizontal_body_margin,
                16,
            )
            body_layout.setSpacing(6 if available.height() < 700 else 12)
            form.setVerticalSpacing(
                6
                if available.height() < 700
                else (8 if live_two_column else 10)
            )
            form.invalidate()
            form.activate()
            form_container.adjustSize()
            body_layout.invalidate()
            body_layout.activate()
            natural_body_height = body_layout.heightForWidth(current_dialog_width)
            if natural_body_height < 0:
                natural_body_height = body_layout.sizeHint().height()
            body.resize(current_dialog_width, natural_body_height)
            natural_height = header.height() + natural_body_height
            dialog.resize(
                current_dialog_width,
                min(natural_height, max(1, available.height() - safe_margin)),
            )
            stable_dialog_size["value"] = dialog.size()
            qt_core.QTimer.singleShot(
                0,
                expand_to_remove_avoidable_scroll,
            )
        except RuntimeError:
            return

    def refit_after_type_change(_index: int) -> None:
        stable_size = stable_dialog_size["value"] or dialog.size()
        available = dialog.screen().availableGeometry()
        max_dialog_width = max(1, available.width() - safe_margin)
        live_two_column = (
            available.height() < 700 and max_dialog_width >= 700
        )
        if (
            layout_state["two_column"]
            or live_two_column
            or available.height() >= 700
        ):
            resize_to_visible_content()
            return
        body_layout.setSpacing(6)
        form.setVerticalSpacing(6)
        form.invalidate()
        form.activate()
        form_container.adjustSize()
        body_layout.invalidate()
        body_layout.activate()
        qt_core.QTimer.singleShot(
            0,
            lambda: dialog.resize(stable_size),
        )

    sample_type.currentIndexChanged.connect(refit_after_type_change)
    resize_to_visible_content()
    initial_focus = sample_type
    if current_type:
        initial_focus = fields[required_fields[current_type][0]]
    focus_and_reveal(initial_focus)

    try:
        _make_windows_taskbar_window(dialog)
        result = _run_topmost_nonmodal_dialog(dialog, qt_core)
        response = selected["response"]
        if isinstance(response, AttributionDialogResponse) and (
            result == qt_widgets.QDialog.DialogCode.Accepted
            or response.action == "cancel"
        ):
            return response
        return AttributionDialogResponse(action="cancel")
    finally:
        _dispose_nonmodal_dialog(dialog, qt_core)


def _is_combined_sample_label_length_error(error: Exception) -> bool:
    message = str(error).casefold()
    return "canonical sample label" in message and "length limit" in message


def _attribution_validation_message(error: Exception) -> str:
    if _is_combined_sample_label_length_error(error):
        return "样品信息组合后超过 Origin 名称长度上限，请缩短样品名称或溶剂/状态。"
    message = str(error).casefold()
    if any(token in message for token in ("concentration", "molarity", "unit", "percentage")):
        return "浓度格式无效：溶液请输入数字或科学计数法；主客体掺杂还须明确选择 wt% 或 mol%。"
    if any(token in message for token in ("temperature", "kelvin")):
        return "温度格式无效：请输入 RT、77 K 或大于 0 的 Kelvin 数值。"
    if "empty" in message or "required" in message:
        return "请完整填写当前样品类型的所有必填项。"
    if "forbidden" in message or "character" in message:
        return "输入包含 Origin 名称不支持的字符，请按字段提示修改。"
    return "输入内容不符合要求，请检查各字段格式。"


def _accept_dialog(dialog: object, selected: dict[str, object], action: str) -> None:
    selected["action"] = action
    dialog.accept()


def _display_label(action: str, request_kind: str | None = None) -> str:
    if request_kind == "database_recovery" and action == "cancel":
        return "取消并退出"
    if request_kind == "final_attribution_summary" and action == "cancel":
        return "取消并退出"
    if request_kind == "final_attribution_summary" and action == "confirm":
        return "确认并冻结本次审核"
    return _ACTION_LABELS.get(action, action)


def _button_object_name(action: str) -> str:
    if action in _DANGER_ACTIONS:
        return "dialog_button_danger"
    if action in _PRIMARY_ACTIONS:
        return "dialog_button_primary"
    return "dialog_button_secondary"


def _fallback_action(actions: tuple[str, ...]) -> str:
    if "cancel" in actions:
        return "cancel"
    return actions[0]


def _ensure_application(qt_widgets: object) -> None:
    application = qt_widgets.QApplication
    if application.instance() is None:
        application([])


def _font(qt_gui: object, pixel_size: int, *, bold: bool = False):
    font = qt_gui.QFont("Microsoft YaHei UI")
    font.setPixelSize(pixel_size)
    font.setBold(bold)
    return font


def configure_workflow_button(button: object, qt_gui: object) -> None:
    font = _font(qt_gui, 14)
    font.setWeight(qt_gui.QFont.Weight.DemiBold)
    button.setFont(font)
    button.setFixedHeight(42)


def configure_embedded_choice_button(button: object, qt_gui: object) -> None:
    button.setFont(_font(qt_gui, 13))
    button.setFixedHeight(42)


def _form_label(qt_widgets: object, text: str, parent: object):
    label = qt_widgets.QLabel(text, parent)
    label.setObjectName("dialog_form_label")
    label.setSizePolicy(
        qt_widgets.QSizePolicy.Policy.Fixed,
        qt_widgets.QSizePolicy.Policy.Preferred,
    )
    return label


def _ignore_escape_key(dialog: object, qt_core: object) -> None:
    original = dialog.keyPressEvent

    def key_press(event: object) -> None:
        if event.key() == qt_core.Qt.Key.Key_Escape:
            event.ignore()
            return
        original(event)

    dialog.keyPressEvent = key_press


def _enable_title_bar_drag(
    header: object,
    window: object,
    qt_core: object,
    qt_gui: object | None = None,
    *,
    allow_partial_offscreen: bool = False,
) -> None:
    qt_gui = qt_gui or _load_qt_gui()
    drag_state: dict[str, object] = {
        "point": None,
        "size": None,
        "fixed_size": False,
        "moved": False,
    }

    def fit_to_screen(available: object) -> None:
        preferred_size = drag_state["size"] or window.size()
        frame = window.frameGeometry()
        decoration_width = max(0, frame.width() - window.width())
        decoration_height = max(0, frame.height() - window.height())
        fitted_width = min(preferred_size.width(), max(1, available.width() - decoration_width))
        fitted_height = min(preferred_size.height(), max(1, available.height() - decoration_height))
        if drag_state["fixed_size"]:
            window.setFixedSize(fitted_width, fitted_height)
        else:
            window.resize(fitted_width, fitted_height)

    def mouse_press(event: object) -> None:
        if event.button() == qt_core.Qt.MouseButton.LeftButton:
            drag_state["point"] = (
                event.globalPosition().toPoint() - window.frameGeometry().topLeft()
            )
            drag_state["size"] = window.size()
            drag_state["fixed_size"] = window.minimumSize() == window.maximumSize()
            drag_state["moved"] = False
            event.accept()

    def mouse_move(event: object) -> None:
        if drag_state["point"] is not None and event.buttons() & qt_core.Qt.MouseButton.LeftButton:
            pointer = event.globalPosition().toPoint()
            target = pointer - drag_state["point"]
            screen = qt_gui.QGuiApplication.screenAt(pointer) or window.screen()
            available = screen.availableGeometry()
            frame = window.frameGeometry()
            if allow_partial_offscreen:
                visible_header_width = min(72, frame.width())
                visible_header_height = min(header.height(), frame.height())
                min_x = available.left() - frame.width() + visible_header_width
                max_x = available.right() - visible_header_width + 1
                min_y = available.top()
                max_y = available.bottom() - visible_header_height + 1
            else:
                fit_to_screen(available)
                frame = window.frameGeometry()
                min_x = available.left()
                max_x = max(
                    available.left(),
                    available.right() - frame.width() + 1,
                )
                min_y = available.top()
                max_y = max(
                    available.top(),
                    available.bottom() - frame.height() + 1,
                )
            window.move(
                min(max(target.x(), min_x), max_x),
                min(max(target.y(), min_y), max_y),
            )
            drag_state["moved"] = True
            event.accept()

    def mouse_release(event: object) -> None:
        if drag_state["moved"]:
            _remember_dialog_position(window, qt_core)
        drag_state["point"] = None
        drag_state["size"] = None
        drag_state["fixed_size"] = False
        drag_state["moved"] = False
        event.accept()

    header.mousePressEvent = mouse_press
    header.mouseMoveEvent = mouse_move
    header.mouseReleaseEvent = mouse_release


enable_title_bar_drag = _enable_title_bar_drag


def _load_qt_modules():
    from PySide6 import QtCore, QtWidgets

    return QtWidgets, QtCore


def _load_qt_gui():
    from PySide6 import QtGui

    return QtGui
