from __future__ import annotations

from pathlib import Path
import sys
from typing import Any

from spectrum_organizer.ui.main_window import (
    FONT_FAMILY,
    FONT_SIZES_PX,
    PRODUCTION_REQUIRED_OBJECT_NAMES,
    build_production_design_tokens,
)
from spectrum_organizer.ui.dialog_port import install_antialiased_window_surface


TASK16_DPI_PERCENTS = (100, 125, 150)
TASK16_TARGET_SIZES = {
    "desktop": (1180, 820),
    "compact": (980, 700),
}
TASK16_SUMMARY_PANE_MIN_WIDTH = 250
TASK16_ATTENTION_LABEL_MIN_WIDTH = 180
TASK16_ATTENTION_FRAME_INSET = 2
REVIEW_TABLE_MIN_CONTENT_WIDTH = 680
APP_ICON_PATH = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[3])) / "assets" / "spectrum-organizer.png"
PRODUCTION_STAGE_ORDER = ("source_input", "attribution", "data_check", "conflict_review", "output", "complete")
PRODUCTION_STAGE_CONFIG = {
    "source_input": {
        "phase_detail": "等待选择",
        "runtime_status": "等待选择输入文件",
        "title": "选择输入文件",
        "subtitle": "选择 Origin 原始文件和输出位置，然后开始任务。",
        "progress": 0,
        "summary_numbers": ("0", "0", "0", "0"),
        "review_headers": ("来源文件", "检测到的 Book", "处理状态"),
        "review_rows": (),
        "show_initial_log": False,
        "log_rows": (),
        "show_input_controls": True,
        "show_review_table": False,
        "show_attention": False,
    },
    "attribution": {
        "phase_detail": "等待",
        "runtime_status": "等待样品归属",
        "title": "确认样品归属",
        "subtitle": "正在读取文件夹信息，并复用本次任务中已确认的样品信息。",
        "progress": 0,
        "summary_numbers": ("0", "0", "0", "0"),
        "review_headers": ("来源文件", "Folder", "识别结果"),
        "review_rows": (),
        "show_initial_log": False,
        "log_rows": (),
        "show_input_controls": False,
        "show_review_table": True,
        "show_attention": False,
    },
    "data_check": {
        "phase_detail": "检查中",
        "runtime_status": "正在检查谱图数据",
        "title": "检查谱图数据",
        "subtitle": "正在检查列、强度上限、谱图类型和数据可用性。",
        "progress": 0,
        "summary_numbers": ("0", "0", "0", "0"),
        "review_headers": ("来源文件", "检测到的 Book", "检查结果"),
        "review_rows": (),
        "show_initial_log": False,
        "log_rows": (),
        "show_input_controls": False,
        "show_review_table": True,
        "show_attention": False,
    },
    "conflict_review": {
        "phase_detail": "等待确认",
        "runtime_status": "等待冲突审核",
        "title": "审核冲突谱图",
        "subtitle": "需要确认重复发射谱、激发谱选择和特殊谱图归类。",
        "progress": 0,
        "summary_numbers": ("0", "0", "0", "0"),
        "review_headers": ("冲突类型", "候选数量", "审核状态"),
        "review_rows": (),
        "show_initial_log": False,
        "log_rows": (),
        "show_input_controls": False,
        "show_review_table": True,
        "show_attention": False,
    },
    "output": {
        "phase_detail": "写入中",
        "runtime_status": "正在生成输出文件",
        "title": "生成输出文件",
        "subtitle": "正在创建全新的整理后 Origin 项目。",
        "progress": 0,
        "summary_numbers": ("0", "0", "0", "0"),
        "review_headers": ("输出步骤", "项目数量", "当前状态"),
        "review_rows": (),
        "show_initial_log": False,
        "log_rows": (),
        "show_input_controls": False,
        "show_review_table": True,
        "show_attention": False,
    },
    "complete": {
        "phase_detail": "可检查",
        "runtime_status": "任务完成",
        "title": "任务完成",
        "subtitle": "输出文件已生成，可以打开输出文件夹检查结果。",
        "progress": 100,
        "summary_numbers": ("0", "0", "0", "0"),
        "review_headers": ("输出文件", "已整理谱图", "结果"),
        "review_rows": (),
        "show_initial_log": False,
        "log_rows": (),
        "show_input_controls": False,
        "show_review_table": True,
        "show_attention": False,
    },
}


def _production_stage_log_rows(stage: str) -> tuple[tuple[str, str, str], ...]:
    if stage == "source_input":
        return ()
    rows: list[tuple[str, str, str]] = []
    for candidate in PRODUCTION_STAGE_ORDER:
        if candidate == "source_input":
            continue
        rows.extend(PRODUCTION_STAGE_CONFIG[candidate]["log_rows"])
        if candidate == stage:
            break
    return tuple(rows)


PRODUCTION_FOCUS_ORDER = (
    "select_sources_button",
    "select_output_parent_button",
    "start_run_button",
    "cancel_run_button",
    "open_output_folder_button",
    "start_new_task_button",
    "exit_application_button",
)

def create_production_main_window(*, dpi_percent: int = 100, size_name: str = "desktop", stage: str = "source_input"):
    if dpi_percent not in TASK16_DPI_PERCENTS:
        raise ValueError(f"Unsupported DPI percent: {dpi_percent}")
    if size_name not in TASK16_TARGET_SIZES:
        raise ValueError(f"Unsupported production size: {size_name}")
    if stage not in PRODUCTION_STAGE_CONFIG:
        raise ValueError(f"Unsupported production stage: {stage}")
    stage_config = PRODUCTION_STAGE_CONFIG[stage]
    active_stage_index = PRODUCTION_STAGE_ORDER.index(stage)
    qt_widgets, qt_core, qt_gui = _load_qt_modules()
    _ensure_application(qt_widgets, qt_gui)
    tokens = build_production_design_tokens()

    class ActivityIndicator(qt_widgets.QWidget):
        def __init__(self, *, role: str, state: str = "active", diameter: int):
            super().__init__()
            self._role = role
            self._state = state
            self._activity_mode = "idle"
            self._angle = 0
            self._timer = qt_core.QTimer(self)
            self._timer.timeout.connect(self._advance)
            self.setFixedSize(diameter, diameter)
            self.setSizePolicy(
                qt_widgets.QSizePolicy.Policy.Fixed,
                qt_widgets.QSizePolicy.Policy.Fixed,
            )

        def set_state(self, state: str) -> None:
            self._state = state
            self._sync_timer()
            self.update()

        def set_activity_mode(self, mode: str) -> None:
            self._activity_mode = mode
            self._sync_timer()
            self.update()

        def _sync_timer(self) -> None:
            active = (
                self._activity_mode == "automatic"
                and (self._role == "status" or self._state == "active")
            )
            if active:
                self._timer.start(70)
            else:
                self._timer.stop()

        def _advance(self) -> None:
            self._angle = (self._angle + 30) % 360
            self.update()

        def paintEvent(self, _event: Any) -> None:
            painter = qt_gui.QPainter(self)
            painter.setRenderHint(
                qt_gui.QPainter.RenderHint.Antialiasing,
                True,
            )
            rect = qt_core.QRectF(self.rect()).adjusted(1.5, 1.5, -1.5, -1.5)
            if self._role == "phase" and self._state == "done":
                painter.setPen(qt_core.Qt.PenStyle.NoPen)
                painter.setBrush(qt_gui.QColor("#147a6c"))
                painter.drawEllipse(rect)
                return
            if self._role == "phase" and self._state == "pending":
                painter.setBrush(qt_gui.QColor("#edf1ef"))
                painter.setPen(qt_gui.QPen(qt_gui.QColor("#aebbb7"), 2))
                painter.drawEllipse(rect)
                return
            if self._activity_mode != "automatic":
                if self._role == "phase" and self._state == "active":
                    painter.setBrush(qt_gui.QColor("#f5f7f6"))
                    painter.setPen(qt_gui.QPen(qt_gui.QColor("#b97116"), 2))
                    painter.drawEllipse(rect)
                    return
                painter.setPen(qt_core.Qt.PenStyle.NoPen)
                painter.setBrush(qt_gui.QColor("#6ed0bb"))
                painter.drawEllipse(rect)
                return
            track_color = "#526461" if self._role == "status" else "#ddc89e"
            accent_color = "#6ed0bb" if self._role == "status" else "#b97116"
            painter.setBrush(qt_core.Qt.BrushStyle.NoBrush)
            painter.setPen(qt_gui.QPen(qt_gui.QColor(track_color), 2))
            painter.drawEllipse(rect)
            accent_pen = qt_gui.QPen(qt_gui.QColor(accent_color), 2.4)
            accent_pen.setCapStyle(qt_core.Qt.PenCapStyle.RoundCap)
            painter.setPen(accent_pen)
            painter.drawArc(
                rect,
                int((90 - self._angle) * 16),
                -110 * 16,
            )

    window = qt_widgets.QMainWindow()
    window.setObjectName("production_main_window")
    window.setWindowTitle("Spectrum Organizer")
    window.setWindowFlags(
        qt_core.Qt.WindowType.Window
        | qt_core.Qt.WindowType.FramelessWindowHint
        | qt_core.Qt.WindowType.WindowMinimizeButtonHint
    )
    window.setProperty("rounded_chrome_radius", 10)
    window.setProperty("rounded_chrome_antialias_enabled", True)
    window.setProperty("rounded_window_mask_enabled", False)
    requested_width, requested_height = TASK16_TARGET_SIZES[size_name]
    window._preferred_window_size = qt_core.QSize(requested_width, requested_height)
    window._preferred_minimum_size = qt_core.QSize(
        min(960, requested_width),
        min(680, requested_height),
    )
    screen = window.screen() or qt_gui.QGuiApplication.primaryScreen()
    available = screen.availableGeometry()
    target_width = min(requested_width, available.width())
    target_height = min(requested_height, available.height())
    window.setMinimumSize(min(960, target_width), min(680, target_height))
    window.resize(target_width, target_height)
    install_antialiased_window_surface(window, qt_core)
    window.move(
        available.x() + max(0, (available.width() - target_width) // 2),
        available.y() + max(0, (available.height() - target_height) // 2),
    )
    app_icon = qt_gui.QIcon(str(APP_ICON_PATH))
    window.setWindowIcon(app_icon)
    application = qt_widgets.QApplication.instance()
    if application is not None:
        application.setWindowIcon(app_icon)
    window.setFont(_font(qt_gui, FONT_SIZES_PX["body"]))
    window.setStyleSheet(_production_style_sheet(tokens))

    central = qt_widgets.QWidget()
    central.setObjectName("production_central")
    root = qt_widgets.QVBoxLayout(central)
    root.setContentsMargins(0, 0, 0, 0)
    root.setSpacing(0)
    widgets: dict[str, Any] = {"text_widgets": [], "layout_widgets": []}

    header = qt_widgets.QFrame()
    header.setObjectName("title_status_bar")
    header_layout = qt_widgets.QGridLayout(header)
    header_layout.setContentsMargins(18, 0, 18, 0)
    header_layout.setHorizontalSpacing(12)
    for column in range(3):
        header_layout.setColumnStretch(column, 1)
    title = qt_widgets.QLabel("Spectrum Organizer")
    title.setObjectName("app_brand")
    title.setFont(_font(qt_gui, 16, bold=True))
    status_group = qt_widgets.QFrame()
    status_group.setObjectName("run_status_group")
    status_layout = qt_widgets.QHBoxLayout(status_group)
    status_layout.setContentsMargins(0, 0, 0, 0)
    status_layout.setSpacing(8)
    run_status_dot = ActivityIndicator(role="status", diameter=12)
    run_status_dot.setObjectName("run_status_dot")
    status = qt_widgets.QLabel(str(stage_config["runtime_status"]))
    status.setObjectName("app_run_status")
    status.setFont(_font(qt_gui, FONT_SIZES_PX["body"]))
    status_layout.addWidget(run_status_dot, 0, qt_core.Qt.AlignmentFlag.AlignVCenter)
    status_layout.addWidget(status, 0, qt_core.Qt.AlignmentFlag.AlignVCenter)
    minimize_window = qt_widgets.QPushButton("−")
    minimize_window.setObjectName("minimize_window_button")
    minimize_window.setToolTip("最小化")
    minimize_window.setFixedSize(32, 30)
    minimize_window.setFocusPolicy(qt_core.Qt.FocusPolicy.NoFocus)
    minimize_window.setFont(_font(qt_gui, FONT_SIZES_PX["body"], bold=True))
    minimize_window.clicked.connect(window.showMinimized)
    cancel_run = qt_widgets.QPushButton("取消任务")
    cancel_run.setObjectName("cancel_run_button")
    cancel_run.setFont(_font(qt_gui, FONT_SIZES_PX["supporting"]))
    window_actions = qt_widgets.QFrame()
    window_actions.setObjectName("window_actions")
    window_actions_layout = qt_widgets.QHBoxLayout(window_actions)
    window_actions_layout.setContentsMargins(0, 0, 0, 0)
    window_actions_layout.setSpacing(8)
    window_actions_layout.addWidget(minimize_window)
    window_actions_layout.addWidget(cancel_run)
    _enable_title_bar_drag(header, window, qt_core)
    header_layout.addWidget(title, 0, 0, qt_core.Qt.AlignmentFlag.AlignLeft | qt_core.Qt.AlignmentFlag.AlignVCenter)
    header_layout.addWidget(status_group, 0, 1, qt_core.Qt.AlignmentFlag.AlignCenter)
    header_layout.addWidget(window_actions, 0, 2, qt_core.Qt.AlignmentFlag.AlignRight | qt_core.Qt.AlignmentFlag.AlignVCenter)
    root.addWidget(header, 0)
    widgets["text_widgets"].extend(
        (
            (title, 16),
            (status, FONT_SIZES_PX["body"]),
            (minimize_window, FONT_SIZES_PX["body"]),
            (cancel_run, FONT_SIZES_PX["supporting"]),
        )
    )

    body = qt_widgets.QHBoxLayout()
    body.setContentsMargins(0, 0, 0, 0)
    body.setSpacing(0)
    root.addLayout(body, 1)

    phase_rail = qt_widgets.QFrame()
    phase_rail.setObjectName("phase_rail")
    phase_rail.setMinimumWidth(136)
    phase_rail.setMaximumWidth(136)
    phase_layout = qt_widgets.QVBoxLayout(phase_rail)
    phase_layout.setContentsMargins(14, 18, 14, 18)
    phase_layout.setSpacing(10)
    rail_label = qt_widgets.QLabel("处理阶段")
    rail_label.setObjectName("rail_label")
    rail_label.setFont(_font(qt_gui, FONT_SIZES_PX["supporting"]))
    phase_layout.addWidget(rail_label)
    widgets["text_widgets"].append((rail_label, FONT_SIZES_PX["supporting"]))
    phase_names = ("输入文件", "样品归属", "数据检查", "冲突审核", "生成输出", "完成")
    phase_items = []
    for index, (stage_key, name) in enumerate(zip(PRODUCTION_STAGE_ORDER, phase_names)):
        if index < active_stage_index:
            state = "done"
            detail = "3 个项目" if stage_key == "source_input" else "已完成"
        elif index == active_stage_index:
            state = "active"
            detail = str(stage_config["phase_detail"])
        else:
            state = "pending"
            detail = "等待"
        phase_items.append((stage_key, name, detail, state))
    widgets["phase_labels"] = {}
    widgets["phase_items"] = {}
    widgets["phase_dots"] = {}
    widgets["phase_connectors"] = {}
    widgets["phase_names"] = dict(zip(PRODUCTION_STAGE_ORDER, phase_names))
    for stage_key, name, detail, state in phase_items:
        item = qt_widgets.QFrame()
        item.setObjectName(f"phase_item_{state}")
        item_layout = qt_widgets.QHBoxLayout(item)
        item_layout.setContentsMargins(0, 0, 0, 0)
        item_layout.setSpacing(9)
        marker_stack = qt_widgets.QVBoxLayout()
        marker_stack.setContentsMargins(0, 0, 0, 0)
        marker_stack.setSpacing(2)
        dot = ActivityIndicator(role="phase", state=state, diameter=13)
        dot.setObjectName(f"phase_dot_{state}")
        marker_stack.addWidget(dot, 0, qt_core.Qt.AlignmentFlag.AlignHCenter)
        if name != "完成":
            connector = qt_widgets.QFrame()
            connector.setObjectName(f"phase_connector_{state}")
            connector.setFixedWidth(1)
            connector.setMinimumHeight(42)
            marker_stack.addWidget(connector, 1, qt_core.Qt.AlignmentFlag.AlignHCenter)
            widgets["phase_connectors"][stage_key] = connector
        label = qt_widgets.QLabel(f"{name}\n{detail}")
        label.setObjectName(f"phase_text_{state}")
        label.setFont(_font(qt_gui, FONT_SIZES_PX["body"], bold=(state == "active")))
        label.setWordWrap(True)
        item_layout.addLayout(marker_stack)
        item_layout.addWidget(label, 1)
        phase_layout.addWidget(item)
        widgets["phase_labels"][stage_key] = label
        widgets["phase_items"][stage_key] = item
        widgets["phase_dots"][stage_key] = dot
        widgets["text_widgets"].append((label, FONT_SIZES_PX["body"]))
    phase_layout.addStretch(1)
    body.addWidget(phase_rail, 0)
    widgets["phase_rail"] = phase_rail
    widgets["layout_widgets"].append(phase_rail)

    work_area = qt_widgets.QWidget()
    work_area.setObjectName("work_area")
    work_layout = qt_widgets.QVBoxLayout(work_area)
    work_layout.setContentsMargins(0, 0, 0, 0)
    work_layout.setSpacing(0)
    body.addWidget(work_area, 1)

    work_upper = qt_widgets.QHBoxLayout()
    work_upper.setContentsMargins(0, 0, 0, 0)
    work_upper.setSpacing(0)
    work_layout.addLayout(work_upper, 1)

    task_panel = qt_widgets.QFrame()
    task_panel.setObjectName("central_task_area")
    task_layout = qt_widgets.QVBoxLayout(task_panel)
    task_layout.setContentsMargins(22, 20, 22, 18)
    task_layout.setSpacing(10)
    eyebrow = qt_widgets.QLabel("当前任务")
    eyebrow.setObjectName("current_task_eyebrow")
    eyebrow.setFont(_font(qt_gui, FONT_SIZES_PX["supporting"], bold=True))
    current_task = qt_widgets.QLabel(str(stage_config["title"]))
    current_task.setObjectName("current_task_title")
    current_task.setFont(_font(qt_gui, FONT_SIZES_PX["current_task_title"], bold=True))
    subtitle = qt_widgets.QLabel(str(stage_config["subtitle"]))
    subtitle.setObjectName("current_task_subtitle")
    subtitle.setFont(_font(qt_gui, FONT_SIZES_PX["body"]))
    subtitle.setWordWrap(True)
    progress = qt_widgets.QProgressBar()
    progress.setObjectName("run_progress")
    progress.setRange(0, 100)
    progress.setValue(int(stage_config["progress"]))
    progress.setTextVisible(False)
    task_layout.addWidget(eyebrow)
    task_layout.addWidget(current_task)
    task_layout.addWidget(subtitle)
    task_layout.addWidget(progress)
    widgets["text_widgets"].extend(
        (
            (eyebrow, FONT_SIZES_PX["supporting"]),
            (current_task, FONT_SIZES_PX["current_task_title"]),
            (subtitle, FONT_SIZES_PX["body"]),
        )
    )

    review_rows = tuple(stage_config["review_rows"])
    table = qt_widgets.QTableWidget(len(review_rows), 3)
    table.setObjectName("review_table")
    table.setHorizontalHeaderLabels(tuple(stage_config["review_headers"]))
    table.horizontalHeader().setDefaultAlignment(qt_core.Qt.AlignmentFlag.AlignLeft | qt_core.Qt.AlignmentFlag.AlignVCenter)
    for column in range(table.columnCount()):
        table.horizontalHeaderItem(column).setTextAlignment(qt_core.Qt.AlignmentFlag.AlignLeft | qt_core.Qt.AlignmentFlag.AlignVCenter)
    table.verticalHeader().setVisible(False)
    table.setSelectionMode(qt_widgets.QAbstractItemView.SelectionMode.NoSelection)
    table.setFocusPolicy(qt_core.Qt.FocusPolicy.NoFocus)
    table.setEditTriggers(qt_widgets.QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setVerticalScrollMode(qt_widgets.QAbstractItemView.ScrollMode.ScrollPerPixel)
    table.verticalScrollBar().setSingleStep(8)
    table.setHorizontalScrollMode(qt_widgets.QAbstractItemView.ScrollMode.ScrollPerPixel)
    table.horizontalScrollBar().setSingleStep(12)
    table.horizontalHeader().sectionResized.connect(
        lambda *_: _schedule_review_table_row_resize(table, qt_core)
    )
    _install_review_table_resize_tracking(table, qt_core)
    for row, values in enumerate(review_rows):
        for column, value in enumerate(values):
            item = qt_widgets.QTableWidgetItem(value)
            if column == 2:
                status_color = "#147a6c" if value in {"已确认", "已检查", "已完成"} else "#b97116"
                item.setForeground(qt_gui.QBrush(qt_gui.QColor(status_color)))
                font = item.font()
                font.setBold(True)
                item.setFont(font)
            table.setItem(row, column, item)
    table.setShowGrid(False)
    table.horizontalHeader().setStretchLastSection(False)
    table.horizontalHeader().setMinimumHeight(36)
    _configure_review_table_layout(
        table,
        tuple(stage_config["review_headers"]),
        qt_widgets,
        qt_core,
        qt_gui,
    )
    table.verticalHeader().setDefaultSectionSize(40)
    for row in range(table.rowCount()):
        table.setRowHeight(row, 40)
    table.setSizePolicy(qt_widgets.QSizePolicy.Policy.Expanding, qt_widgets.QSizePolicy.Policy.Expanding)
    table.setFont(_font(qt_gui, FONT_SIZES_PX["table"]))
    task_layout.addWidget(table, 1)
    widgets["review_table"] = table
    widgets["text_widgets"].append((table, FONT_SIZES_PX["table"]))

    action_row = qt_widgets.QHBoxLayout()
    action_row.setSpacing(8)
    select_sources = qt_widgets.QPushButton("选择 Origin 原始文件")
    select_sources.setObjectName("select_sources_button")
    select_output = qt_widgets.QPushButton("选择输出位置")
    select_output.setObjectName("select_output_parent_button")
    class WorkflowRevealButton(qt_widgets.QPushButton):
        def __init__(self, text: str):
            super().__init__(text)
            self._workflow_visible = True
            self.setProperty("workflow_reveal_duration_ms", 220)
            self._opacity_effect = qt_widgets.QGraphicsOpacityEffect(self)
            self._opacity_effect.setOpacity(1.0)
            self.setGraphicsEffect(self._opacity_effect)
            self._reveal_group = qt_core.QParallelAnimationGroup(self)
            self._width_animation = qt_core.QPropertyAnimation(self, b"maximumWidth", self._reveal_group)
            self._opacity_animation = qt_core.QPropertyAnimation(
                self._opacity_effect,
                b"opacity",
                self._reveal_group,
            )
            for animation in (self._width_animation, self._opacity_animation):
                animation.setDuration(220)
                animation.setEasingCurve(qt_core.QEasingCurve.Type.OutCubic)
            self._reveal_group.addAnimation(self._width_animation)
            self._reveal_group.addAnimation(self._opacity_animation)
            self._reveal_group.finished.connect(self._finish_reveal)

        def set_workflow_visible(self, visible: bool) -> None:
            visible = bool(visible)
            if self._workflow_visible == visible and self.isVisible() == visible:
                return
            self._reveal_group.stop()
            self._workflow_visible = visible
            if not visible:
                self.hide()
                self.setMaximumWidth(16_777_215)
                self._opacity_effect.setOpacity(1.0)
                return
            if not self.window().isVisible():
                self.show()
                self.setMaximumWidth(16_777_215)
                self._opacity_effect.setOpacity(1.0)
                return
            parent = self.parentWidget()
            siblings = (
                [
                    child
                    for child in parent.children()
                    if isinstance(child, qt_widgets.QPushButton) and child is not self and child.isVisible()
                ]
                if parent is not None
                else []
            )
            occupied_width = sum(button.width() for button in siblings) + 8 * max(0, len(siblings) - 1)
            target_width = max(1, (occupied_width - 16) // 3)
            self.setMaximumWidth(0)
            self._opacity_effect.setOpacity(0.0)
            self.show()
            self._width_animation.setStartValue(0)
            self._width_animation.setEndValue(target_width)
            self._opacity_animation.setStartValue(0.0)
            self._opacity_animation.setEndValue(1.0)
            self._reveal_group.start()

        def _finish_reveal(self) -> None:
            if self._workflow_visible:
                self.setMaximumWidth(16_777_215)
                self._opacity_effect.setOpacity(1.0)

    start_run = WorkflowRevealButton("开始任务")
    start_run.setObjectName("start_run_button")
    for button in (select_sources, select_output, start_run):
        button.setMinimumHeight(44)
        button.setFont(_font(qt_gui, FONT_SIZES_PX["control"]))
        button.setSizePolicy(qt_widgets.QSizePolicy.Policy.Ignored, qt_widgets.QSizePolicy.Policy.Fixed)
        action_row.addWidget(button, 1)
        widgets["text_widgets"].append((button, FONT_SIZES_PX["control"]))
    task_layout.addLayout(action_row)
    completion_action_row = qt_widgets.QHBoxLayout()
    completion_action_row.setSpacing(8)
    completion_action_row.addStretch(1)
    open_output_folder = qt_widgets.QPushButton("打开输出文件夹")
    open_output_folder.setObjectName("open_output_folder_button")
    start_new_task = qt_widgets.QPushButton("开始新任务")
    start_new_task.setObjectName("start_new_task_button")
    start_new_task.setProperty("completion_primary", True)
    exit_application = qt_widgets.QPushButton("退出")
    exit_application.setObjectName("exit_application_button")
    exit_application.setProperty("completion_exit", True)
    completion_buttons = (
        open_output_folder,
        start_new_task,
        exit_application,
    )
    for button in completion_buttons:
        button.setMinimumHeight(40)
        button.setMinimumWidth(112)
        button.setFont(_font(qt_gui, FONT_SIZES_PX["control"]))
        completion_action_row.addWidget(button)
        widgets["text_widgets"].append(
            (button, FONT_SIZES_PX["control"])
        )
        button.setVisible(stage == "complete")
    task_layout.addLayout(completion_action_row)
    preflight_summary = qt_widgets.QLabel(
        "预检设置：开始任务前确认 S1 强度上限和发射谱 Y 列；二维稳态谱不检查 S1 上限。"
    )
    preflight_summary.setObjectName("preflight_settings_summary_label")
    preflight_summary.setWordWrap(True)
    preflight_summary.setFont(_font(qt_gui, FONT_SIZES_PX["supporting"]))
    task_layout.addWidget(preflight_summary)
    widgets["text_widgets"].append((preflight_summary, FONT_SIZES_PX["supporting"]))
    for widget in (select_sources, select_output, preflight_summary):
        widget.setVisible(bool(stage_config["show_input_controls"]))
    start_run.set_workflow_visible(False)
    table.setVisible(bool(stage_config["show_review_table"]))
    table_space_filler = qt_widgets.QWidget()
    table_space_filler.setObjectName("table_space_filler")
    table_space_filler.setSizePolicy(
        qt_widgets.QSizePolicy.Policy.Preferred,
        qt_widgets.QSizePolicy.Policy.Expanding,
    )
    table_space_filler.setVisible(not bool(stage_config["show_review_table"]))
    task_layout.addWidget(table_space_filler, 1)
    widgets["table_space_filler"] = table_space_filler
    work_upper.addWidget(task_panel, 1)
    widgets["layout_widgets"].append(task_panel)

    summary_panel = qt_widgets.QFrame()
    summary_panel.setObjectName("right_summary_attention_pane")
    summary_panel.setMinimumWidth(TASK16_SUMMARY_PANE_MIN_WIDTH)
    summary_layout = qt_widgets.QVBoxLayout(summary_panel)
    summary_layout.setContentsMargins(16, 18, 16, 18)
    summary_layout.setSpacing(8)
    summary_stats = qt_widgets.QWidget()
    summary_stats.setObjectName("summary_stats")
    summary_stats.setSizePolicy(qt_widgets.QSizePolicy.Policy.Preferred, qt_widgets.QSizePolicy.Policy.Maximum)
    summary_stats_layout = qt_widgets.QVBoxLayout(summary_stats)
    summary_stats_layout.setContentsMargins(0, 0, 0, 0)
    summary_stats_layout.setSpacing(10)
    summary_title = qt_widgets.QLabel("本次运行")
    summary_title.setFont(_font(qt_gui, FONT_SIZES_PX["body"], bold=True))
    summary_stats_layout.addWidget(summary_title)
    widgets["text_widgets"].append((summary_title, FONT_SIZES_PX["body"]))
    summary_number_labels = []
    summary_text_labels = []
    summary_metric_widgets = []
    for metric_index, (number, label_text) in enumerate(zip(
        stage_config["summary_numbers"],
        ("检测到的 Book", "已提取 Book", "等待人工处理", "已排除记录"),
    )):
        metric_widget = qt_widgets.QWidget()
        metric_widget.setObjectName("summary_metric")
        metric_layout = qt_widgets.QVBoxLayout(metric_widget)
        metric_layout.setContentsMargins(0, 0, 0, 0)
        metric_layout.setSpacing(2)
        summary_metric_widgets.append(metric_widget)
        text_label = qt_widgets.QLabel(label_text)
        text_label.setObjectName("summary_metric_text")
        text_label.setFont(_font(qt_gui, FONT_SIZES_PX["supporting"]))
        summary_text_labels.append(text_label)
        number_label = qt_widgets.QLabel(str(number))
        number_label.setObjectName("summary_metric_number")
        summary_number_labels.append(number_label)
        if metric_index == 0:
            number_label.setObjectName("key_summary_number")
        number_label.setFont(_font(qt_gui, FONT_SIZES_PX["key_summary_number"], bold=True))
        metric_layout.addWidget(text_label)
        metric_layout.addWidget(number_label)
        summary_stats_layout.addWidget(metric_widget)
        widgets["text_widgets"].extend(
            (
                (number_label, FONT_SIZES_PX["key_summary_number"]),
                (text_label, FONT_SIZES_PX["supporting"]),
            )
        )
        if metric_index == 0:
            widgets["key_summary_number"] = number_label
    summary_layout.addWidget(summary_stats)
    attention_pane = qt_widgets.QScrollArea()
    attention_pane.setObjectName("attention_pane")
    attention_pane.setFocusPolicy(qt_core.Qt.FocusPolicy.NoFocus)
    attention_pane.setWidgetResizable(True)
    attention_pane.setHorizontalScrollBarPolicy(qt_core.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    attention_pane.setFrameShape(qt_widgets.QFrame.Shape.NoFrame)
    attention_body = qt_widgets.QWidget()
    attention_body.setObjectName("attention_body")
    attention_body.setProperty("surface_role", "transparent")
    attention_layout = qt_widgets.QVBoxLayout(attention_body)
    attention_layout.setSizeConstraint(qt_widgets.QLayout.SizeConstraint.SetMinAndMaxSize)
    attention_layout.setContentsMargins(
        TASK16_ATTENTION_FRAME_INSET,
        TASK16_ATTENTION_FRAME_INSET,
        TASK16_ATTENTION_FRAME_INSET,
        TASK16_ATTENTION_FRAME_INSET,
    )
    attention = qt_widgets.QLabel(
        '<span style="color:#7c4c0d; font-weight:600;">需要你的确认</span><br>'
        "等待需要用户确认的信息。"
    )
    attention.setObjectName("attention_message")
    attention.setTextFormat(qt_core.Qt.TextFormat.RichText)
    attention.setWordWrap(True)
    attention.setMinimumWidth(TASK16_ATTENTION_LABEL_MIN_WIDTH)
    attention.setMinimumHeight(72)
    attention.setFont(_font(qt_gui, FONT_SIZES_PX["body"]))
    attention_layout.addWidget(attention)
    attention_layout.addStretch(1)
    attention_pane.setWidget(attention_body)
    attention_pane.setVisible(bool(stage_config["show_attention"]))
    if stage_config["show_attention"]:
        summary_layout.addWidget(attention_pane, 1)
    else:
        summary_layout.addWidget(attention_pane)
        summary_layout.addStretch(1)
    work_upper.addWidget(summary_panel, 0)
    widgets["layout_widgets"].append(summary_panel)
    widgets["summary_panel"] = summary_panel
    widgets["attention_pane"] = attention_pane
    widgets["attention_body"] = attention_body
    widgets["attention_labels"] = [attention]
    widgets["text_widgets"].append((attention, FONT_SIZES_PX["body"]))

    log_panel = qt_widgets.QFrame()
    log_panel.setObjectName("bottom_log_progress_area")
    log_panel.setMinimumHeight(205)
    log_layout = qt_widgets.QVBoxLayout(log_panel)
    log_layout.setContentsMargins(16, 13, 16, 14)
    log_layout.setSpacing(6)
    output_path_label = qt_widgets.QLabel("输出位置：未选择")
    output_path_label.setObjectName("output_path_label")
    output_path_label.setWordWrap(True)
    output_path_label.setFont(_font(qt_gui, FONT_SIZES_PX["supporting"]))
    output_path_label.hide()
    log_head_row = qt_widgets.QHBoxLayout()
    log_head_row.setContentsMargins(0, 0, 0, 0)
    log_head_title = qt_widgets.QLabel("运行记录")
    log_head_title.setObjectName("log_head_title")
    log_head_title.setFont(_font(qt_gui, FONT_SIZES_PX["supporting"]))
    log_head_realtime = qt_widgets.QLabel("实时更新")
    log_head_realtime.setObjectName("log_head_realtime")
    log_head_realtime.setFont(_font(qt_gui, FONT_SIZES_PX["supporting"]))
    log_head_row.addWidget(log_head_title)
    log_head_row.addStretch(1)
    log_head_row.addWidget(log_head_realtime)
    run_log = qt_widgets.QTextEdit()
    run_log.setObjectName("run_log")
    run_log.setFocusPolicy(qt_core.Qt.FocusPolicy.NoFocus)
    run_log.setReadOnly(True)
    run_log.setMinimumHeight(150)
    run_log.setMaximumHeight(170)
    run_log.setFont(_font(qt_gui, FONT_SIZES_PX["supporting"]))
    run_log.document().setDocumentMargin(0)
    run_log.setVerticalScrollBarPolicy(qt_core.Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    run_log_scrollbar = run_log.verticalScrollBar()
    run_log_scrollbar.rangeChanged.connect(
        lambda minimum, maximum: run_log_scrollbar.setVisible(maximum > minimum)
    )
    if bool(stage_config["show_initial_log"]):
        _set_initial_run_log(qt_gui, run_log, _production_stage_log_rows(stage))
    else:
        run_log.clear()
    run_log_scrollbar.setVisible(run_log_scrollbar.maximum() > run_log_scrollbar.minimum())
    log_layout.addWidget(output_path_label)
    log_layout.addLayout(log_head_row)
    log_layout.addWidget(run_log)
    work_layout.addWidget(log_panel, 0)
    widgets["layout_widgets"].append(log_panel)
    widgets["text_widgets"].extend(
        (
            (output_path_label, FONT_SIZES_PX["supporting"]),
            (log_head_title, FONT_SIZES_PX["supporting"]),
            (log_head_realtime, FONT_SIZES_PX["supporting"]),
            (run_log, FONT_SIZES_PX["supporting"]),
        )
    )

    widgets.update(
        {
            "select_sources_button": select_sources,
            "select_output_parent_button": select_output,
            "preflight_settings_summary_label": preflight_summary,
            "start_run_button": start_run,
            "open_output_folder_button": open_output_folder,
            "start_new_task_button": start_new_task,
            "exit_application_button": exit_application,
            "minimize_window_button": minimize_window,
            "cancel_run_button": cancel_run,
            "app_run_status": status,
            "run_status_group": status_group,
            "run_status_indicator": run_status_dot,
            "summary_number_labels": summary_number_labels,
            "summary_text_labels": summary_text_labels,
            "summary_metric_widgets": summary_metric_widgets,
            "current_task_title": current_task,
            "current_task_subtitle": subtitle,
            "run_progress": progress,
            "output_path_label": output_path_label,
            "run_log": run_log,
            "window": window,
            "runtime_stage": stage,
            "runtime_activity_mode": (
                "idle" if stage == "complete" else "manual"
            ),
            "rounded_window_antialias_enabled": True,
        }
    )
    _update_activity_motion(widgets)
    cancel_run.setVisible(stage != "complete")
    for first, second in zip(PRODUCTION_FOCUS_ORDER, PRODUCTION_FOCUS_ORDER[1:]):
        qt_widgets.QWidget.setTabOrder(widgets[first], widgets[second])
    window.setCentralWidget(central)
    _enable_screen_geometry_tracking(window)
    return window, widgets

def _review_table_item(qt_widgets: Any, qt_gui: Any, text: str, column: int) -> Any:
    item = qt_widgets.QTableWidgetItem(text)
    item.setToolTip(text)
    if column == 2:
        if text == "已确认":
            item.setForeground(qt_gui.QColor("#147a6c"))
        elif "等待" in text or "需要" in text:
            item.setForeground(qt_gui.QColor("#b97116"))
        font = item.font()
        font.setBold(True)
        item.setFont(font)
    return item


def _configure_review_table_layout(
    table: Any,
    headers: tuple[str, str, str],
    qt_widgets: Any,
    qt_core: Any,
    qt_gui: Any,
) -> None:
    class WrapAnywhereDelegate(qt_widgets.QStyledItemDelegate):
        horizontal_padding = 10
        vertical_padding = 6

        def paint(self, painter: Any, option: Any, index: Any) -> None:
            styled = qt_widgets.QStyleOptionViewItem(option)
            self.initStyleOption(styled, index)
            text = styled.text
            styled.text = ""
            style = styled.widget.style() if styled.widget else qt_widgets.QApplication.style()
            style.drawControl(
                qt_widgets.QStyle.ControlElement.CE_ItemViewItem,
                styled,
                painter,
                styled.widget,
            )
            painter.save()
            selected = bool(styled.state & qt_widgets.QStyle.StateFlag.State_Selected)
            role = (
                qt_gui.QPalette.ColorRole.HighlightedText
                if selected
                else qt_gui.QPalette.ColorRole.Text
            )
            painter.setPen(styled.palette.color(role))
            painter.setFont(styled.font)
            painter.drawText(
                styled.rect.adjusted(
                    self.horizontal_padding,
                    self.vertical_padding,
                    -self.horizontal_padding,
                    -self.vertical_padding,
                ),
                int(
                    qt_core.Qt.AlignmentFlag.AlignLeft
                    | qt_core.Qt.AlignmentFlag.AlignVCenter
                    | qt_core.Qt.TextFlag.TextWrapAnywhere
                ),
                text,
            )
            painter.restore()

        def sizeHint(self, option: Any, index: Any) -> Any:
            styled = qt_widgets.QStyleOptionViewItem(option)
            self.initStyleOption(styled, index)
            width = max(
                1,
                table.columnWidth(index.column()) - (2 * self.horizontal_padding),
            )
            bounds = qt_gui.QFontMetrics(styled.font).boundingRect(
                qt_core.QRect(0, 0, width, 10_000),
                int(
                    qt_core.Qt.AlignmentFlag.AlignLeft
                    | qt_core.Qt.TextFlag.TextWrapAnywhere
                ),
                styled.text,
            )
            base = super().sizeHint(option, index)
            return qt_core.QSize(
                max(base.width(), bounds.width() + (2 * self.horizontal_padding)),
                max(40, bounds.height() + (2 * self.vertical_padding)),
            )

    header = table.horizontalHeader()
    table.setWordWrap(True)
    table.setTextElideMode(qt_core.Qt.TextElideMode.ElideNone)
    delegate = getattr(table, "_review_wrap_delegate", None)
    if delegate is None:
        delegate = WrapAnywhereDelegate(table)
        table.setItemDelegate(delegate)
        table._review_wrap_delegate = delegate
    table._review_headers = headers
    for column in range(3):
        header.setSectionResizeMode(column, qt_widgets.QHeaderView.ResizeMode.Fixed)
    _resize_review_table_columns(table)


def _resize_review_table_columns(table: Any) -> None:
    try:
        headers = getattr(table, "_review_headers", ("", "", ""))
        if headers[2] == "排除原因":
            ratios = (0.28, 0.30, 0.42)
        elif headers == ("来源文件", "Folder", "识别结果"):
            ratios = (0.38, 0.34, 0.28)
        else:
            ratios = (0.38, 0.22, 0.40)
        width = max(REVIEW_TABLE_MIN_CONTENT_WIDTH, table.viewport().width())
        first = round(width * ratios[0])
        second = round(width * ratios[1])
        table.setColumnWidth(0, first)
        table.setColumnWidth(1, second)
        table.setColumnWidth(2, max(1, width - first - second))
    except RuntimeError:
        return


def _resize_review_table_row_batch(table: Any, qt_core: Any) -> None:
    try:
        start = int(getattr(table, "_review_row_resize_next", 0))
        stop = min(table.rowCount(), start + 8)
        for row in range(start, stop):
            table.resizeRowToContents(row)
            table.setRowHeight(row, max(40, table.rowHeight(row)))
        table._review_row_resize_next = stop
        if stop < table.rowCount():
            _start_owned_table_timer(
                table,
                qt_core,
                "_review_row_resize_batch_timer",
                0,
                lambda current: _resize_review_table_row_batch(
                    current,
                    qt_core,
                ),
            )
    except RuntimeError:
        return


def _restart_review_table_row_resize(table: Any, qt_core: Any) -> None:
    try:
        table._review_row_resize_next = 0
        _start_owned_table_timer(
            table,
            qt_core,
            "_review_row_resize_batch_timer",
            0,
            lambda current: _resize_review_table_row_batch(
                current,
                qt_core,
            ),
        )
    except RuntimeError:
        return


def _schedule_review_table_row_resize(table: Any, qt_core: Any) -> None:
    _restart_review_table_row_resize(table, qt_core)
    _start_owned_table_timer(
        table,
        qt_core,
        "_review_row_resize_final_timer",
        80,
        lambda current: _restart_review_table_row_resize(
            current,
            qt_core,
        ),
    )


def _start_owned_table_timer(
    table: Any,
    qt_core: Any,
    attribute: str,
    interval_ms: int,
    callback: Any,
) -> None:
    timer = getattr(table, attribute, None)
    if timer is None:
        timer = qt_core.QTimer(table)
        timer.setSingleShot(True)
        timer.timeout.connect(lambda: callback(table))
        setattr(table, attribute, timer)
    timer.start(interval_ms)


def _install_review_table_resize_tracking(table: Any, qt_core: Any) -> None:
    class ReviewTableResizeFilter(qt_core.QObject):
        def eventFilter(self, watched: Any, event: Any) -> bool:
            if event.type() == qt_core.QEvent.Type.Resize:
                _start_owned_table_timer(
                    table,
                    qt_core,
                    "_review_column_resize_timer",
                    0,
                    _resize_review_table_columns,
                )
                _schedule_review_table_row_resize(table, qt_core)
            return False

    resize_filter = ReviewTableResizeFilter(table)
    table.viewport().installEventFilter(resize_filter)
    table._review_resize_filter = resize_filter


def update_production_runtime_view(
    widgets: dict[str, Any],
    *,
    runtime_status: str | None = None,
    activity_mode: str | None = None,
    stage: str | None = None,
    phase_detail: str | None = None,
    source_count: int | None = None,
    title: str | None = None,
    subtitle: str | None = None,
    progress: int | None = None,
    progress_busy: bool | None = None,
    summary_numbers: tuple[str, str, str, str] | None = None,
    review_rows: tuple[tuple[str, str, str], ...] | None = None,
    review_row_update: tuple[str, int, tuple[str, str, str]] | None = None,
    review_headers: tuple[str, str, str] | None = None,
    attention_message: str | None = None,
    show_review_table: bool | None = None,
    show_attention: bool | None = None,
    show_input_controls: bool | None = None,
    show_completion_actions: bool | None = None,
) -> None:
    qt_widgets, qt_core, qt_gui = _load_qt_modules()
    row_update = None
    if review_row_update is not None:
        expected_stage, row, values = review_row_update
        if widgets.get("runtime_stage") == expected_stage:
            row_update = (row, values)
    if stage is not None:
        _update_phase_rail(widgets, stage, phase_detail, source_count)
        widgets["runtime_stage"] = stage
    if runtime_status is not None:
        status_label = widgets["app_run_status"]
        status_group = widgets["run_status_group"]
        status_label.setText(runtime_status)
        status_label.updateGeometry()
        status_layout = status_group.layout()
        status_layout.invalidate()
        status_layout.activate()
        status_group.updateGeometry()
        parent_layout = status_group.parentWidget().layout()
        parent_layout.invalidate()
        parent_layout.activate()
    if activity_mode is not None:
        if activity_mode not in {"automatic", "manual", "idle"}:
            raise ValueError(f"Unsupported activity mode: {activity_mode}")
        widgets["runtime_activity_mode"] = activity_mode
    if stage is not None or activity_mode is not None:
        _update_activity_motion(widgets)
    if title is not None:
        widgets["current_task_title"].setText(title)
    if subtitle is not None:
        widgets["current_task_subtitle"].setText(subtitle)
    if progress_busy is True:
        widgets["run_progress"].setRange(0, 0)
    elif progress_busy is False:
        widgets["run_progress"].setRange(0, 100)
    if progress is not None and progress_busy is not True:
        widgets["run_progress"].setValue(progress)
    if summary_numbers is not None:
        for label, number in zip(widgets["summary_number_labels"], summary_numbers):
            label.setText(number)
    if review_headers is not None:
        table = widgets["review_table"]
        table.setHorizontalHeaderLabels(review_headers)
        _configure_review_table_layout(
            table,
            review_headers,
            qt_widgets,
            qt_core,
            qt_gui,
        )
        for column in range(table.columnCount()):
            table.horizontalHeaderItem(column).setTextAlignment(
                qt_core.Qt.AlignmentFlag.AlignLeft | qt_core.Qt.AlignmentFlag.AlignVCenter
            )
        _schedule_review_table_row_resize(table, qt_core)
    if review_rows is not None:
        table = widgets["review_table"]
        table.setRowCount(len(review_rows))
        for row, values in enumerate(review_rows):
            for column, value in enumerate(values):
                table.setItem(row, column, _review_table_item(qt_widgets, qt_gui, value, column))
        _schedule_review_table_row_resize(table, qt_core)
    if row_update is not None:
        table = widgets["review_table"]
        row, values = row_update
        if 0 <= row < table.rowCount():
            for column, value in enumerate(values):
                table.setItem(row, column, _review_table_item(qt_widgets, qt_gui, value, column))
            table.resizeRowToContents(row)
            table.setRowHeight(row, max(40, table.rowHeight(row)))
    if attention_message is not None:
        labels = widgets["attention_labels"]
        if labels:
            labels[0].setText(attention_message)
    if show_review_table is not None:
        review_table = widgets["review_table"]
        was_hidden = review_table.isHidden()
        review_table.setVisible(show_review_table)
        widgets["table_space_filler"].setVisible(not show_review_table)
        if show_review_table and was_hidden and row_update is None:
            _schedule_review_table_row_resize(review_table, qt_core)
    if show_attention is not None:
        widgets["attention_pane"].setVisible(show_attention)
    if show_input_controls is not None:
        for name in ("select_sources_button", "select_output_parent_button", "start_run_button", "preflight_settings_summary_label"):
            widgets[name].setVisible(show_input_controls)
    if show_completion_actions is not None:
        for name in (
            "open_output_folder_button",
            "start_new_task_button",
            "exit_application_button",
        ):
            widgets[name].setVisible(show_completion_actions)
        widgets["cancel_run_button"].setVisible(
            not show_completion_actions
        )


def _update_phase_rail(widgets: dict[str, Any], active_stage: str, phase_detail: str | None, source_count: int | None) -> None:
    _, qt_core, qt_gui = _load_qt_modules()
    active_index = PRODUCTION_STAGE_ORDER.index(active_stage)
    for index, stage_key in enumerate(PRODUCTION_STAGE_ORDER):
        if index < active_index:
            state = "done"
            detail = f"{source_count} 个项目" if stage_key == "source_input" and source_count is not None else "已完成"
        elif index == active_index:
            state = "active"
            detail = phase_detail or str(PRODUCTION_STAGE_CONFIG[stage_key]["phase_detail"])
        else:
            state = "pending"
            detail = "等待"
        item = widgets["phase_items"][stage_key]
        dot = widgets["phase_dots"][stage_key]
        label = widgets["phase_labels"][stage_key]
        item.setObjectName(f"phase_item_{state}")
        dot.setObjectName(f"phase_dot_{state}")
        dot.set_state(state)
        connector = widgets["phase_connectors"].get(stage_key)
        if connector is not None:
            connector.setObjectName(f"phase_connector_{state}")
        label.setObjectName(f"phase_text_{state}")
        label.setText(f"{widgets['phase_names'][stage_key]}\n{detail}")
        label.setFont(_font(qt_gui, FONT_SIZES_PX["body"], bold=(state == "active")))
        for widget in (item, dot, connector, label):
            if widget is not None:
                widget.style().unpolish(widget)
                widget.style().polish(widget)


def _update_activity_motion(widgets: dict[str, Any]) -> None:
    stage = widgets.get("runtime_stage")
    mode = widgets["runtime_activity_mode"]
    widgets["run_status_indicator"].set_activity_mode(mode)
    for stage_key, dot in widgets["phase_dots"].items():
        dot.set_activity_mode(mode if stage_key == stage else "idle")


def _set_initial_run_log(qt_gui: Any, run_log: Any, rows: tuple[tuple[str, str, str], ...]) -> None:
    row_style = "margin:0; line-height:165%; font-family:'Microsoft YaHei UI'; font-size:12px; color:#c9d5d2;"
    html_rows = []
    for timestamp, message, tone in rows:
        color = "#efbc74" if tone == "warn" else "#c9d5d2"
        weight = "600" if tone == "warn" else "400"
        html_rows.append(
            f'<p style="{row_style}"><span style="color:#83928f;">{timestamp}</span>&nbsp;&nbsp;'
            f'<span style="color:{color}; font-weight:{weight};">{message}</span></p>'
        )
    run_log.setHtml("\n".join(html_rows))
    run_log.moveCursor(qt_gui.QTextCursor.MoveOperation.End)
    scrollbar = run_log.verticalScrollBar()
    scrollbar.setValue(scrollbar.maximum())

def _fit_window_to_available_geometry(
    window: Any,
    available: Any,
    desired_top_left: Any,
    *,
    clamp_position: bool = True,
) -> None:
    preferred_size = getattr(window, "_preferred_window_size", window.size())
    preferred_minimum = getattr(window, "_preferred_minimum_size", window.minimumSize())
    target_width = min(preferred_size.width(), available.width())
    target_height = min(preferred_size.height(), available.height())
    window.setMinimumSize(
        min(preferred_minimum.width(), target_width),
        min(preferred_minimum.height(), target_height),
    )
    window.resize(target_width, target_height)
    if not clamp_position:
        window.move(desired_top_left)
        return
    frame = window.frameGeometry()
    max_x = available.right() - frame.width() + 1
    max_y = available.bottom() - frame.height() + 1
    window.move(
        max(available.left(), min(desired_top_left.x(), max_x)),
        max(available.top(), min(desired_top_left.y(), max_y)),
    )


def _enable_screen_geometry_tracking(window: Any, window_handle: Any = None) -> None:
    if window_handle is None:
        window.winId()
        window_handle = window.windowHandle()
    if window_handle is None:
        return

    previous_screen = getattr(window, "_tracked_screen", None)
    previous_refit = getattr(window, "_screen_geometry_refit", None)
    if previous_screen is not None and previous_refit is not None:
        previous_screen.availableGeometryChanged.disconnect(previous_refit)
    previous_handle = getattr(window, "_screen_geometry_handle", None)
    previous_bind = getattr(window, "_screen_geometry_bind", None)
    if previous_handle is not None and previous_bind is not None:
        previous_handle.screenChanged.disconnect(previous_bind)
    window._tracked_screen = None

    def refit_current_screen(*_args: Any) -> None:
        screen = getattr(window, "_tracked_screen", None)
        if screen is not None:
            _fit_window_to_available_geometry(
                window,
                screen.availableGeometry(),
                window.frameGeometry().topLeft(),
                clamp_position=False,
            )

    def bind_screen(screen: Any) -> None:
        previous = getattr(window, "_tracked_screen", None)
        if previous is screen:
            refit_current_screen()
            return
        if previous is not None:
            previous.availableGeometryChanged.disconnect(refit_current_screen)
        window._tracked_screen = screen
        if screen is not None:
            screen.availableGeometryChanged.connect(refit_current_screen)
            refit_current_screen()

    window._screen_geometry_refit = refit_current_screen
    window._screen_geometry_bind = bind_screen
    window._screen_geometry_handle = window_handle
    window_handle.screenChanged.connect(bind_screen)
    bind_screen(window_handle.screen() or window.screen())


def _enable_title_bar_drag(header: Any, window: Any, qt_core: Any) -> None:
    drag_offset: dict[str, Any] = {"point": None}

    def mouse_press(event: Any) -> None:
        if event.button() == qt_core.Qt.MouseButton.LeftButton:
            drag_offset["point"] = event.globalPosition().toPoint() - window.frameGeometry().topLeft()
            event.accept()

    def mouse_move(event: Any) -> None:
        if drag_offset["point"] is not None and event.buttons() & qt_core.Qt.MouseButton.LeftButton:
            desired = event.globalPosition().toPoint() - drag_offset["point"]
            window.move(desired)
            event.accept()

    def mouse_release(event: Any) -> None:
        drag_offset["point"] = None
        event.accept()

    header.mousePressEvent = mouse_press
    header.mouseMoveEvent = mouse_move
    header.mouseReleaseEvent = mouse_release


def _production_style_sheet(tokens: dict[str, str]) -> str:
    return f"""
        QMainWindow {{ background: transparent; }}
        QWidget#production_central {{ background: #f5f7f6; color: #1f2928; border-radius: 10px; }}
        QFrame#title_status_bar {{ background: #263332; color: white; border-bottom: 3px solid #147a6c; border-top-left-radius: 10px; border-top-right-radius: 10px; min-height: 52px; max-height: 52px; }}
        QLabel#app_brand {{ color: white; }}
        QLabel#app_run_status {{ color: #d8e2df; }}
        QFrame#phase_rail {{ background: #edf1ef; border: 0; border-right: 1px solid #d6ddda; border-bottom-left-radius: 10px; }}
        QLabel#rail_label {{ color: #687573; }}
        QFrame#phase_item_done, QFrame#phase_item_active, QFrame#phase_item_pending {{ border: 0; min-height: 58px; }}
        QLabel#phase_text_done {{ color: #147a6c; }}
        QLabel#phase_text_active {{ color: #1f2928; }}
        QLabel#phase_text_pending {{ color: #65716f; }}
        QFrame#phase_connector_done, QFrame#phase_connector_active, QFrame#phase_connector_pending {{ background: #bcc8c5; border: 0; }}
        QFrame#central_task_area {{ background: white; border: 0; }}
        QLabel#current_task_eyebrow {{ color: #147a6c; }}
        QLabel#current_task_subtitle {{ color: #687573; }}
        QProgressBar#run_progress {{ background: #dce4e1; border: 0; border-radius: 3px; min-height: 6px; max-height: 6px; }}
        QProgressBar#run_progress::chunk {{ background: #147a6c; border-radius: 3px; }}
        QTableWidget#review_table {{ background: white; border: 0; gridline-color: transparent; }}
        QTableWidget#review_table::item {{ border-bottom: 1px solid #e3e8e6; padding: 8px 10px; }}
        QHeaderView::section {{ background: white; color: #697673; border: 0; border-bottom: 1px solid #ccd6d2; padding: 9px 10px; }}
        QPushButton {{ background: #263332; color: white; border: 1px solid #64716f; border-radius: 4px; padding: 6px 10px; }}
        QPushButton:hover {{ background: #334241; }}
        QPushButton[selection_confirmed="true"] {{ background: #147a6c; border-color: #147a6c; color: white; font-weight: 600; }}
        QPushButton[selection_confirmed="true"]:hover {{ background: #176f64; border-color: #176f64; }}
        QPushButton#open_output_folder_button {{ background: white; color: #263332; border-color: #9aa9a5; }}
        QPushButton#open_output_folder_button:hover {{ background: #edf3f1; border-color: #147a6c; }}
        QPushButton[completion_primary="true"] {{ background: #147a6c; border-color: #147a6c; font-weight: 600; }}
        QPushButton[completion_primary="true"]:hover {{ background: #176f64; border-color: #176f64; }}
        QPushButton[completion_exit="true"] {{ background: white; color: #8d312c; border-color: #b76d68; }}
        QPushButton[completion_exit="true"]:hover {{ background: #fff1ef; border-color: #8d312c; }}
        QPushButton#minimize_window_button {{ color: #d8e2df; border-color: #526260; padding: 0; font-weight: 700; }}
        QPushButton#cancel_run_button {{ color: #ffd9d6; border-color: #91635f; }}
        QFrame#right_summary_attention_pane {{ background: #f8faf9; border: 0; border-left: 1px solid #d6ddda; }}
        QLabel#key_summary_number {{ color: #1f2928; }}
        QScrollArea#attention_pane, QScrollArea#attention_pane > QWidget > QWidget, QWidget#attention_body {{ background: transparent; border: 0; }}
        QLabel#attention_message {{ background: #fff2dc; border-left: 3px solid #b97116; padding: 12px; color: #1f2928; line-height: 1.5; }}
        QFrame#bottom_log_progress_area {{ background: #222c2b; border: 0; border-top: 1px solid #bcc7c4; border-bottom-right-radius: 10px; }}
        QLabel#output_path_label, QLabel#log_head_title, QLabel#log_head_realtime {{ color: #7fb9ac; }}
        QTextEdit#run_log {{ background: #222c2b; color: #c9d5d2; border: 0; selection-background-color: #147a6c; }}
        QTableWidget#review_table QScrollBar:vertical {{ background: transparent; width: 16px; margin: 0; border: 0; }}
        QTableWidget#review_table QScrollBar::handle:vertical {{ background: #91a9a3; border-radius: 5px; min-height: 46px; margin: 3px 4px; }}
        QTableWidget#review_table QScrollBar::handle:vertical:hover {{ background: #6f8983; }}
        QTableWidget#review_table QScrollBar::add-line:vertical, QTableWidget#review_table QScrollBar::sub-line:vertical {{ height: 0; border: 0; background: transparent; }}
        QTableWidget#review_table QScrollBar::add-page:vertical, QTableWidget#review_table QScrollBar::sub-page:vertical {{ background: transparent; }}
        QTextEdit#run_log QScrollBar:vertical {{ background: #1b2423; width: 12px; margin: 0; border: 0; }}
        QTextEdit#run_log QScrollBar::handle:vertical {{ background: #7f9691; border-radius: 5px; min-height: 34px; margin: 2px; }}
        QTextEdit#run_log QScrollBar::handle:vertical:hover {{ background: #9dc8bd; }}
        QTextEdit#run_log QScrollBar::add-line:vertical, QTextEdit#run_log QScrollBar::sub-line:vertical {{ height: 0; border: 0; background: transparent; }}
        QTextEdit#run_log QScrollBar::add-page:vertical, QTextEdit#run_log QScrollBar::sub-page:vertical {{ background: transparent; }}
    """

def _production_focus_order_matches(widgets: dict[str, Any]) -> bool:
    _, qt_core, _ = _load_qt_modules()
    expected = tuple(name for name in PRODUCTION_FOCUS_ORDER if widgets[name].isVisible())
    first = widgets[expected[0]]
    current = first
    seen: list[str] = []
    for _ in range(200):
        if (
            current.focusPolicy() != qt_core.Qt.FocusPolicy.NoFocus
            and current.isEnabled()
            and current.isVisible()
            and current.window() is first.window()
        ):
            seen.append(current.objectName() or f"<{current.__class__.__name__}>")
        current = current.nextInFocusChain()
        if current is first:
            break
    return tuple(seen) == expected


def _font(qt_gui: Any, pixel_size: int, *, bold: bool = False):
    font = qt_gui.QFont(FONT_FAMILY)
    font.setPixelSize(pixel_size)
    font.setBold(bold)
    return font


def _ensure_application(qt_widgets: Any, qt_gui: Any) -> None:
    if qt_widgets.QApplication.instance() is None:
        app = qt_widgets.QApplication([])
        app.setFont(_font(qt_gui, FONT_SIZES_PX["body"]))


def _load_qt_modules():
    from PySide6 import QtCore, QtGui, QtWidgets

    return QtWidgets, QtCore, QtGui
