import copy
import inspect
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import typing
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from spectrum_organizer.ui.dialogs import (
    DialogRequest,
    FinalReviewDialogRequest,
    FinalReviewOutputBook,
    FinalReviewOutputFolder,
    FinalReviewRow,
    attribution_dialog,
    batch_write_failure_dialog,
    cancel_and_exit_confirmation_dialog,
    cancelled_and_exited_dialog,
    completion_actions_dialog,
    database_recovery_dialog,
    duplicate_emission_dialog,
    excitation_selection_dialog,
    final_attribution_summary_dialog,
    hidden_origin_confirmation_dialog,
    output_can_be_inspected_dialog,
    output_parent_recovery_dialog,
    preflight_settings_dialog,
    save_and_close_origin_dialog,
    space_retry_cancel_dialog,
    special_group_confirmation_dialog,
)
from spectrum_organizer.ui.main_window import (
    FONT_FAMILY,
    FONT_SIZES_PX,
    build_main_window_contract,
    scaled_font_policy,
)
from spectrum_organizer.ui import app as app_module
from spectrum_organizer.ui import dialog_port as dialog_port_module
from spectrum_organizer.ui.dialog_port import ORGANIZER_DIALOG_STYLE_SHEET, apply_styled_dialog_chrome
from spectrum_organizer.ui import main_window as main_window_module
from spectrum_organizer.ui import qt_main_window as qt_main_window_module
from spectrum_organizer.ui.qt_main_window import create_production_main_window, update_production_runtime_view, _production_focus_order_matches
from spectrum_organizer.ui import orchestrator as orchestrator_module
from spectrum_organizer.ui.orchestrator import (
    BookOnlyOrchestrator,

    WorkflowMode,
    build_startup_workflows,
)


class FakeSettingsStore:
    def __init__(self):
        self.output_parent_writes = []
        self.preflight_writes = []

    def set_last_output_parent(self, value: str):
        self.output_parent_writes.append(value)
        return []

    def set_preflight_settings(self, s1_limit: int, steady_emission_y: str, allow_missing_s1: bool = False):
        self.preflight_writes.append((s1_limit, steady_emission_y, allow_missing_s1))
        return []


class LegacyTwoArgumentSettingsStore(FakeSettingsStore):
    def set_preflight_settings(self, s1_limit: int, steady_emission_y: str):
        self.preflight_writes.append((s1_limit, steady_emission_y))
        return []


def _document_text_colors(document):
    colors = set()
    block = document.firstBlock()
    while block.isValid():
        iterator = block.begin()
        while not iterator.atEnd():
            fragment = iterator.fragment()
            if fragment.isValid():
                color = fragment.charFormat().foreground().color()
                if color.isValid():
                    colors.add(color.name())
            iterator += 1
        block = block.next()
    return colors

class UiContractTests(unittest.TestCase):
    def tearDown(self):
        try:
            from PySide6 import QtCore, QtWidgets
        except ImportError:
            return

        app = QtWidgets.QApplication.instance()
        if app is None:
            return
        for widget in app.topLevelWidgets():
            widget.deleteLater()
        for _ in range(2):
            QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)
            app.processEvents()

    def test_dialog_request_field_values_accept_boolean_controls(self):
        self.assertEqual(
            dict[str, str | bool],
            typing.get_type_hints(DialogRequest)["field_values"],
        )

    def test_production_source_has_no_workspace_sample_identities(self):
        forbidden = (
            "20241209_MFL_2DPho.opj",
            "20250412_MFL-mTHF_RT.opj",
            "20250507_PFLDelay.OPJ",
            "Paper.opju",
            "例如：MFL",
            "例如：mTHF",
        )
        hits = {
            value: tuple(
                path.relative_to(SRC).as_posix()
                for path in SRC.rglob("*.py")
                if value.casefold() in path.read_text(encoding="utf-8-sig").casefold()
            )
            for value in forbidden
        }

        self.assertEqual({value: () for value in forbidden}, hits)

        dialog_source = (SRC / "spectrum_organizer" / "ui" / "dialog_port.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('"sample": "例如：NDI"', dialog_source)

    def test_production_stage_defaults_do_not_embed_preview_records(self):
        for stage, config in qt_main_window_module.PRODUCTION_STAGE_CONFIG.items():
            with self.subTest(stage=stage):
                self.assertEqual((), config["review_rows"])
                self.assertEqual((), config["log_rows"])
                self.assertEqual(("0", "0", "0", "0"), config["summary_numbers"])

    def test_complete_stage_and_key_summary_metric_have_real_semantics(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6 import QtWidgets

        self.assertEqual(100, qt_main_window_module.PRODUCTION_STAGE_CONFIG["complete"]["progress"])
        window, widgets = create_production_main_window(
            dpi_percent=100,
            size_name="desktop",
            stage="source_input",
        )
        try:
            self.assertIs(
                widgets["summary_number_labels"][0],
                widgets["key_summary_number"],
            )
            self.assertEqual("key_summary_number", widgets["key_summary_number"].objectName())
        finally:
            window.close()
            QtWidgets.QApplication.processEvents()

    def test_preview_window_entrypoints_are_not_exposed_by_production_module(self):
        for preview_name in (
            "create_task16_main_window",
            "inspect_task16_window",
            "build_task16_window_spec",
            "inspect_production_main_window",
            "Task16WindowSpec",
            "Task16WindowInspection",
        ):
            with self.subTest(preview_name=preview_name):
                self.assertFalse(hasattr(qt_main_window_module, preview_name))

    def test_orchestrator_module_does_not_export_test_fake_settings_store(self):
        self.assertFalse(hasattr(orchestrator_module, "FakeSettingsStore"))

    def test_layout_a_uses_fixed_lab_instrument_regions_and_fonts(self):
        contract = build_main_window_contract()

        self.assertEqual(
            ("left_phase_rail", "central_task_area", "right_summary_attention_pane", "bottom_log_progress_area"),
            contract.regions,
        )
        self.assertEqual("dark_green_lab_instrument", contract.style)
        self.assertFalse(contract.decorative_gradients)
        self.assertFalse(contract.decorative_orbs)
        self.assertTrue(contract.main_window_visible_throughout)
        self.assertEqual("Microsoft YaHei UI", FONT_FAMILY)
        self.assertEqual(
            {"supporting": 12, "body": 13, "table": 13, "control": 13, "current_task_title": 20, "key_summary_number": 26},
            FONT_SIZES_PX,
        )

    def test_program_dialog_controls_do_not_use_native_spinner_or_combo_arrows(self):
        preflight_source = inspect.getsource(app_module.QtPreflightDialogPort.confirm)

        self.assertNotIn("QSpinBox", preflight_source)
        self.assertIn("QLineEdit", preflight_source)
        self.assertIn("QComboBox::down-arrow", ORGANIZER_DIALOG_STYLE_SHEET)
        self.assertIn("image: none", ORGANIZER_DIALOG_STYLE_SHEET)

    def test_combo_popup_distinguishes_selected_and_hovered_rows(self):
        from PySide6 import QtCore, QtGui, QtWidgets

        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        observed = {}

        def inspect_dialog():
            dialog = app.activeModalWidget()
            combo = dialog.findChild(QtWidgets.QComboBox)
            palette = combo.view().palette()
            observed["highlight"] = palette.color(
                QtGui.QPalette.ColorRole.Highlight
            ).name()
            observed["highlighted_text"] = palette.color(
                QtGui.QPalette.ColorRole.HighlightedText
            ).name()
            observed["view_style"] = combo.view().styleSheet()
            dialog.reject()

        QtCore.QTimer.singleShot(0, inspect_dialog)
        app_module.QtPreflightDialogPort(QtWidgets, QtCore).confirm(
            None,
            default_s1_limit=2_000_000,
            steady_emission_y="S1c",
        )

        self.assertIn("QComboBox QAbstractItemView", ORGANIZER_DIALOG_STYLE_SHEET)
        self.assertIn("selection-background-color: #147a6c", ORGANIZER_DIALOG_STYLE_SHEET)
        self.assertIn("selection-color: #ffffff", ORGANIZER_DIALOG_STYLE_SHEET)
        self.assertEqual("#147a6c", observed["highlight"])
        self.assertEqual("#ffffff", observed["highlighted_text"])
        self.assertIn("QAbstractItemView::item:selected", observed["view_style"])
        self.assertIn("QAbstractItemView::item:hover", observed["view_style"])
        self.assertIn("background: #dcebe7", observed["view_style"])
        self.assertIn("color: #263332", observed["view_style"])
        self.assertIn("background: #147a6c", observed["view_style"])
        self.assertIn("color: #ffffff", observed["view_style"])

    def test_attribution_dialog_uses_integrated_mode_alignment_and_approved_selection_palette(self):
        source = inspect.getsource(dialog_port_module.show_attribution_dialog)

        self.assertIn("QGridLayout", source)
        self.assertNotIn("QFormLayout", source)
        self.assertIn("归属方式", source)
        self.assertIn("整个 Folder", source)
        self.assertIn("逐 Book", source)
        self.assertNotIn("改为逐 Book 归属", source)
        self.assertIn("样品信息不可输入换行", source)
        self.assertIn("QListWidget#attribution_pending_book_list::item:selected", ORGANIZER_DIALOG_STYLE_SHEET)
        self.assertIn("background: #147a6c", ORGANIZER_DIALOG_STYLE_SHEET)
        self.assertIn("color: #ffffff", ORGANIZER_DIALOG_STYLE_SHEET)
    def test_production_main_window_contract_exposes_required_widgets_and_actions(self):
        contract = build_main_window_contract()
        tokens = main_window_module.build_production_design_tokens()

        self.assertEqual("#17332F", tokens["instrument_green"])
        self.assertEqual("#0F2522", tokens["deep_panel"])
        self.assertEqual("#66D6BF", tokens["mint_signal"])
        self.assertEqual("#F3F7F5", tokens["work_surface"])
        self.assertEqual("#A8BBB4", tokens["rule_line"])
        self.assertEqual("#B86B10", tokens["warning_amber"])
        self.assertEqual("#A33A32", tokens["danger_red"])
        self.assertEqual(
            (
                "select_sources_button",
                "select_output_parent_button",
                "preflight_settings_summary_label",
                "start_run_button",
                "cancel_run_button",
                "phase_rail",
                "attention_pane",
                "run_log",
                "output_path_label",
            ),
            contract.required_object_names,
        )
        self.assertEqual(
            ("select_sources", "select_output_parent", "confirm_preflight_settings", "start_run", "cancel", "close"),
            contract.available_actions,
        )
        self.assertNotIn("preflight_s1_limit", contract.required_object_names)
        self.assertNotIn("preflight_steady_emission_y", contract.required_object_names)

    def test_run_main_window_launches_production_window_not_task16_fixture(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6 import QtCore, QtTest, QtWidgets

        if QtWidgets.QApplication.instance() is None:
            QtWidgets.QApplication([])
        calls = []

        class FakeWindow:
            def show(self):
                calls.append("show")

        with mock.patch.object(
            app_module,
            "create_production_main_window",
            new=lambda *, dpi_percent, size_name: (
                calls.append((dpi_percent, size_name)) or FakeWindow(),
                {},
            ),
        ):
            result = app_module.run_main_window(
                settings_store=FakeSettingsStore(),
                file_dialogs=object(),
                message_box=object(),
            )

        self.assertEqual(0, result)
        self.assertEqual([(100, "desktop"), "show"], calls)

    def test_production_qt_window_uses_accepted_brainstorming_layout_a_text(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6 import QtWidgets

        def collect_visible_text(window):
            texts = []
            for widget in window.findChildren(QtWidgets.QWidget):
                if hasattr(widget, "text"):
                    value = widget.text()
                    if value:
                        texts.append(value)
                if hasattr(widget, "toPlainText"):
                    value = widget.toPlainText()
                    if value:
                        texts.append(value)
            for table in window.findChildren(QtWidgets.QTableWidget):
                for row in range(table.rowCount()):
                    for column in range(table.columnCount()):
                        item = table.item(row, column)
                        if item is not None:
                            texts.append(item.text())
            return "\n".join(texts)

        window, _ = create_production_main_window(dpi_percent=100, size_name="desktop")
        attribution_window, _ = create_production_main_window(dpi_percent=100, size_name="desktop", stage="attribution")
        try:
            missing = [
                name
                for name in build_main_window_contract().required_object_names
                if window.findChild(QtWidgets.QWidget, name) is None
            ]
            forbidden = [
                name
                for name in ("preflight_s1_limit", "preflight_steady_emission_y")
                if window.findChild(QtWidgets.QWidget, name) is not None
            ]
            visible_text = collect_visible_text(window) + "\n" + collect_visible_text(attribution_window)
        finally:
            window.close()
            attribution_window.close()
            QtWidgets.QApplication.processEvents()

        self.assertEqual([], missing)
        self.assertEqual([], forbidden)
        for formal_text in (
            "处理阶段",
            "输入文件",
            "样品归属",
            "当前任务",
            "确认样品归属",
            "正在读取文件夹信息，并复用本次任务中已确认的样品信息。",
            "本次运行",
            "检测到的 Book",
            "已提取 Book",
            "等待人工处理",
            "已排除记录",
            "运行记录",
            "实时更新",
        ):
            self.assertIn(formal_text, visible_text)
        for workspace_sample_identity in (
            "20241209_MFL_2DPho.opj",
            "20250412_MFL-mTHF_RT.opj",
            "20250507_PFLDelay.OPJ",
            "Paper.opju",
        ):
            self.assertNotIn(workspace_sample_identity, visible_text)
        for placeholder_text in (
            "Prepare Origin input",
            "Ready for manual full-run setup",
            "Choose Origin raw files",
            "Choose output folder",
            "Attention",
            "Ready. Select sources and output folder.",
        ):
            self.assertNotIn(placeholder_text, visible_text)

    def test_production_frameless_window_has_rounded_chrome_contract(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6 import QtCore, QtWidgets

        window, widgets = create_production_main_window(dpi_percent=100, size_name="desktop", stage="attribution")
        try:
            style = window.styleSheet()
            self.assertTrue(window.testAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground))
            self.assertEqual(10, window.property("rounded_chrome_radius"))
            self.assertIn("QWidget#production_central { background: #f5f7f6; color: #1f2928; border-radius: 10px; }", style)
            self.assertIn("QFrame#title_status_bar", style)
            self.assertIn("border-top-left-radius: 10px", style)
            self.assertIn("border-top-right-radius: 10px", style)
            self.assertIn("QFrame#bottom_log_progress_area", style)
            self.assertIn("border-bottom-right-radius: 10px", style)
            self.assertNotIn(
                "QFrame#bottom_log_progress_area { background: #222c2b; border: 0; border-top: 1px solid #bcc7c4; border-bottom-left-radius",
                style,
            )
            self.assertIn('QPushButton[selection_confirmed="true"]', style)
            self.assertIn(
                'QPushButton[selection_confirmed="true"] { background: #147a6c; border-color: #147a6c;',
                style,
            )
            self.assertIn(
                'QPushButton[selection_confirmed="true"]:hover { background: #176f64; border-color: #176f64;',
                style,
            )
            self.assertTrue(window.property("rounded_chrome_antialias_enabled"))
            self.assertFalse(window.property("rounded_window_mask_enabled"))
            mask = window.mask()
            self.assertTrue(mask.isEmpty())
            self.assertIn("rounded_window_antialias_enabled", widgets)
            self.assertTrue(widgets["rounded_window_antialias_enabled"])
        finally:
            window.close()
            QtWidgets.QApplication.processEvents()
    def test_production_window_uses_custom_chrome_with_minimize_only(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6 import QtCore, QtTest, QtWidgets

        window, widgets = create_production_main_window(dpi_percent=100, size_name="desktop", stage="attribution")
        try:
            flags = window.windowFlags()
            self.assertTrue(bool(flags & QtCore.Qt.WindowType.FramelessWindowHint))
            self.assertFalse(bool(flags & QtCore.Qt.WindowType.WindowMaximizeButtonHint))
            self.assertIn("minimize_window_button", widgets)
            self.assertIsNotNone(window.findChild(QtWidgets.QPushButton, "minimize_window_button"))
            self.assertIsNone(window.findChild(QtWidgets.QPushButton, "maximize_window_button"))
            self.assertEqual("最小化", widgets["minimize_window_button"].toolTip())
            self.assertEqual("取消任务", widgets["cancel_run_button"].text())
            self.assertFalse(window.windowIcon().isNull())
            available = window.screen().availableGeometry()
            self.assertEqual(min(1180, available.width()), window.width())
            self.assertEqual(min(820, available.height()), window.height())
            self.assertEqual(136, widgets["phase_rail"].minimumWidth())
            self.assertEqual(250, widgets["summary_panel"].minimumWidth())
            self.assertGreaterEqual(widgets["run_log"].minimumHeight(), 150)
        finally:
            window.close()
            QtWidgets.QApplication.processEvents()
    def test_production_window_matches_accepted_layout_a_visual_tokens(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6 import QtCore, QtWidgets

        window, widgets = create_production_main_window(dpi_percent=100, size_name="desktop", stage="attribution")
        try:
            update_production_runtime_view(
                widgets,
                runtime_status="正在处理 3 个合成测试文件",
                summary_numbers=("36", "31", "3", "5"),
                review_rows=(
                    ("synthetic_A.opj", "Sample_A_RT", "已确认"),
                    ("synthetic_B.opj", "Sample_B_77K", "已确认"),
                    ("synthetic_C.opj", "Root / Book7", "等待人工确认"),
                    ("synthetic_D.opj", "Sample_D_RT", "需要归属"),
                ),
                attention_message="需要你的确认：合成测试 Folder 的样品状态无法可靠推断。",
                show_attention=True,
            )
            widgets["run_log"].setHtml(
                "<p style=\"margin:0; line-height:165%; color:#c9d5d2;\"><span style=\"color:#83928f;\">10:42:15</span> 已读取 synthetic_A.opj</p>"
                "<p style=\"margin:0; line-height:165%; color:#c9d5d2;\"><span style=\"color:#83928f;\">10:42:16</span> 检测到 14 个普通谱</p>"
                "<p style=\"margin:0; line-height:165%; color:#efbc74;\"><span style=\"color:#83928f;\">10:42:17</span> 等待样品归属</p>"
                "<p style=\"margin:0; line-height:165%; color:#c9d5d2;\"><span style=\"color:#83928f;\">10:42:18</span> 主任务等待人工确认</p>"
            )
            window.show()
            QtWidgets.QApplication.processEvents()
            style = window.styleSheet()
            table = widgets["review_table"]
            output_label = widgets["output_path_label"]

            self.assertIsNotNone(window.findChild(QtWidgets.QWidget, "phase_dot_active"))
            self.assertIsNotNone(window.findChild(QtWidgets.QWidget, "phase_connector_active"))
            self.assertIsNotNone(window.findChild(QtWidgets.QWidget, "run_status_group"))
            self.assertIsNotNone(window.findChild(QtWidgets.QWidget, "run_status_dot"))
            status_group = window.findChild(QtWidgets.QWidget, "run_status_group")
            self.assertLess(abs(status_group.geometry().center().x() - window.rect().center().x()), 90)
            self.assertIn("#b97116", style)
            self.assertIn("#fff2dc", style)
            self.assertIs(
                widgets["run_status_indicator"],
                window.findChild(QtWidgets.QWidget, "run_status_dot"),
            )
            self.assertIsNone(status_group.graphicsEffect())
            self.assertEqual("#147a6c", table.item(0, 2).foreground().color().name())
            self.assertEqual("#b97116", table.item(2, 2).foreground().color().name())
            self.assertTrue(table.item(2, 2).font().bold())
            self.assertFalse(table.showGrid())
            self.assertGreaterEqual(table.rowHeight(0), 40)
            self.assertGreater(table.height(), 238)
            self.assertEqual(0, table.verticalScrollBar().maximum())
            self.assertIn("QTableWidget#review_table QScrollBar:vertical", style)
            self.assertIn("QTableWidget#review_table QScrollBar::handle:vertical", style)
            self.assertIn("QTextEdit#run_log QScrollBar:vertical", style)
            self.assertIn("QTextEdit#run_log QScrollBar::handle:vertical", style)
            self.assertIn("QTableWidget#review_table QScrollBar:vertical { background: transparent; width: 16px;", style)
            self.assertIn("QTableWidget#review_table QScrollBar::handle:vertical { background: #91a9a3;", style)
            self.assertIn("QTextEdit#run_log QScrollBar:vertical { background: #1b2423; width: 12px;", style)
            self.assertIn("QTextEdit#run_log QScrollBar::handle:vertical { background: #7f9691;", style)
            self.assertNotIn("\n        QScrollBar:vertical {", style)
            self.assertEqual(table.horizontalHeader().defaultAlignment() & int(QtCore.Qt.AlignmentFlag.AlignLeft), int(QtCore.Qt.AlignmentFlag.AlignLeft))
            for column in range(table.columnCount()):
                self.assertEqual(
                    table.horizontalHeaderItem(column).textAlignment() & int(QtCore.Qt.AlignmentFlag.AlignLeft),
                    int(QtCore.Qt.AlignmentFlag.AlignLeft),
                )
            self.assertIsNotNone(window.findChild(QtWidgets.QWidget, "log_head_title"))
            self.assertIsNotNone(window.findChild(QtWidgets.QWidget, "log_head_realtime"))
            self.assertNotIn("                                      ", window.findChild(QtWidgets.QWidget, "log_head_title").text())
            self.assertTrue(output_label.isHidden())
            self.assertGreater(widgets["attention_pane"].maximumHeight(), 190)
            self.assertEqual(0, widgets["attention_pane"].verticalScrollBar().maximum())
            self.assertEqual("transparent", widgets["attention_body"].property("surface_role"))
            self.assertGreaterEqual(window.findChild(QtWidgets.QWidget, "bottom_log_progress_area").minimumHeight(), 170)
            self.assertGreaterEqual(widgets["run_log"].minimumHeight(), 118)
            self.assertGreaterEqual(widgets["run_log"].toHtml().replace(" ", "").count("line-height:165%"), 4)
            log_colors = _document_text_colors(widgets["run_log"].document())
            self.assertIn("#83928f", log_colors)
            self.assertIn("#efbc74", log_colors)
            self.assertGreaterEqual(table.columnWidth(1), table.columnWidth(2))
            self.assertGreater(table.columnViewportPosition(2), int(table.viewport().width() * 0.6))
            self.assertTrue(widgets["select_sources_button"].isHidden())
            self.assertTrue(widgets["select_output_parent_button"].isHidden())
            self.assertTrue(widgets["start_run_button"].isHidden())
            self.assertTrue(widgets["preflight_settings_summary_label"].isHidden())
        finally:
            window.close()
            QtWidgets.QApplication.processEvents()

    def test_production_stage_visibility_matches_workflow_step(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6 import QtCore, QtTest, QtWidgets

        source_window, source_widgets = create_production_main_window(dpi_percent=100, size_name="desktop", stage="source_input")
        attribution_window, attribution_widgets = create_production_main_window(dpi_percent=100, size_name="desktop", stage="attribution")
        other_windows = []
        try:
            source_window.show()
            QtWidgets.QApplication.processEvents()
            self.assertFalse(source_widgets["select_sources_button"].isHidden())
            self.assertFalse(source_widgets["select_output_parent_button"].isHidden())
            self.assertTrue(source_widgets["start_run_button"].isHidden())
            self.assertFalse(source_widgets["preflight_settings_summary_label"].isHidden())
            preflight_summary = source_widgets["preflight_settings_summary_label"].text()
            self.assertIn("S1 强度上限", preflight_summary)
            self.assertIn("发射谱 Y 列", preflight_summary)
            self.assertNotIn("稳态发射谱强度数据列", preflight_summary)
            self.assertIn("二维稳态谱不检查", preflight_summary)
            self.assertNotIn("S1 limit", preflight_summary)
            self.assertNotIn("稳态发射 Y", preflight_summary)
            for button_name in ("select_sources_button", "select_output_parent_button", "start_run_button"):
                self.assertGreaterEqual(source_widgets[button_name].minimumHeight(), 44)
            self.assertTrue(source_widgets["review_table"].isHidden())
            self.assertTrue(source_widgets["attention_pane"].isHidden())
            self.assertEqual("选择输入文件", source_widgets["current_task_title"].text())
            self.assertEqual("等待选择输入文件", source_widgets["app_run_status"].text())
            self.assertEqual("", source_widgets["run_log"].toPlainText().strip())
            self.assertEqual(["0", "0", "0", "0"], [label.text() for label in source_widgets["summary_number_labels"]])

            start_button = source_widgets["start_run_button"]
            self.assertTrue(callable(getattr(start_button, "set_workflow_visible", None)))
            self.assertEqual(220, start_button.property("workflow_reveal_duration_ms"))
            start_button.set_workflow_visible(True)
            QtWidgets.QApplication.processEvents()
            self.assertTrue(start_button.isVisible())
            self.assertLess(start_button.maximumWidth(), 16_777_215)
            QtTest.QTest.qWait(260)
            self.assertEqual(16_777_215, start_button.maximumWidth())
            action_widths = [
                source_widgets[name].width()
                for name in ("select_sources_button", "select_output_parent_button", "start_run_button")
            ]
            self.assertLessEqual(max(action_widths) - min(action_widths), 1)

            self.assertTrue(attribution_widgets["select_sources_button"].isHidden())
            self.assertTrue(attribution_widgets["select_output_parent_button"].isHidden())
            self.assertTrue(attribution_widgets["start_run_button"].isHidden())
            self.assertTrue(attribution_widgets["preflight_settings_summary_label"].isHidden())
            self.assertFalse(attribution_widgets["review_table"].isHidden())
            self.assertTrue(attribution_widgets["attention_pane"].isHidden())
            self.assertEqual(
                QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff,
                attribution_widgets["attention_pane"].horizontalScrollBarPolicy(),
            )
            self.assertEqual("确认样品归属", attribution_widgets["current_task_title"].text())
            self.assertEqual("等待样品归属", attribution_widgets["app_run_status"].text())
            self.assertEqual("", attribution_widgets["run_log"].toPlainText())
            self.assertEqual(0, attribution_widgets["review_table"].rowCount())
            self.assertEqual(["0", "0", "0", "0"], [label.text() for label in attribution_widgets["summary_number_labels"]])
            self.assertEqual(QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded, attribution_widgets["run_log"].verticalScrollBarPolicy())
            self.assertEqual(0, attribution_widgets["run_log"].verticalScrollBar().maximum())
            self.assertTrue(attribution_widgets["run_log"].verticalScrollBar().isHidden())

            for stage in ("data_check", "conflict_review", "output", "complete"):
                window, widgets = create_production_main_window(dpi_percent=100, size_name="desktop", stage=stage)
                window.show()
                QtWidgets.QApplication.processEvents()
                self.assertTrue(widgets["attention_pane"].isHidden())
                number_labels = widgets["summary_number_labels"]
                self.assertLess(number_labels[-1].geometry().bottom() - number_labels[0].geometry().top(), 210)
                QtWidgets.QApplication.processEvents()
                other_windows.append(window)
                self.assertTrue(widgets["select_sources_button"].isHidden(), stage)
                self.assertTrue(widgets["select_output_parent_button"].isHidden(), stage)
                self.assertTrue(widgets["start_run_button"].isHidden(), stage)
                self.assertTrue(widgets["preflight_settings_summary_label"].isHidden(), stage)
                self.assertEqual("", widgets["run_log"].toPlainText(), stage)
                self.assertEqual(0, widgets["review_table"].rowCount(), stage)
                self.assertEqual(["0", "0", "0", "0"], [label.text() for label in number_labels], stage)
                self.assertEqual(QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded, widgets["run_log"].verticalScrollBarPolicy(), stage)
                self.assertEqual(0, widgets["run_log"].verticalScrollBar().maximum(), stage)
                self.assertTrue(widgets["run_log"].verticalScrollBar().isHidden(), stage)
            self.assertIn("QTableWidget#review_table QScrollBar:vertical", attribution_window.styleSheet())
            self.assertIn("QTextEdit#run_log QScrollBar:vertical", attribution_window.styleSheet())
        finally:
            source_window.close()
            attribution_window.close()
            for window in other_windows:
                window.close()
            QtWidgets.QApplication.processEvents()

    def test_production_runtime_view_can_refresh_all_visible_progress_regions(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6 import QtWidgets

        window, widgets = create_production_main_window(dpi_percent=100, size_name="desktop", stage="attribution")
        try:
            window.show()
            QtWidgets.QApplication.processEvents()

            update_production_runtime_view(
                widgets,
                stage="data_check",
                phase_detail="检查中",
                runtime_status="正在检查谱图数据",
                title="检查谱图数据",
                subtitle="正在检查 12 条普通谱。",
                progress=72,
                summary_numbers=("12", "11", "2", "1"),
                review_rows=(
                    ("20250701_A.opj", "MFL_RT", "已确认"),
                    ("20250701_B.opj", "PFL_RT", "等待人工确认"),
                ),
                review_headers=("来源文件", "检测到的 Book", "处理状态"),
                attention_message="PFL_RT - 等待确认样品归属。",
                show_review_table=True,
                show_attention=True,
            )

            self.assertEqual("正在检查谱图数据", widgets["app_run_status"].text())
            self.assertEqual("检查谱图数据", widgets["current_task_title"].text())
            self.assertEqual("正在检查 12 条普通谱。", widgets["current_task_subtitle"].text())
            self.assertEqual(72, widgets["run_progress"].value())
            self.assertEqual(["12", "11", "2", "1"], [label.text() for label in widgets["summary_number_labels"]])
            self.assertEqual(
                ["检测到的 Book", "已提取 Book", "等待人工处理", "已排除记录"],
                [label.text() for label in widgets["summary_text_labels"]],
            )
            for metric_widget, text_label, number_label in zip(
                widgets["summary_metric_widgets"],
                widgets["summary_text_labels"],
                widgets["summary_number_labels"],
            ):
                metric_layout = metric_widget.layout()
                self.assertIs(text_label, metric_layout.itemAt(0).widget())
                self.assertIs(number_label, metric_layout.itemAt(1).widget())
            self.assertEqual(2, widgets["review_table"].rowCount())
            self.assertEqual("20250701_B.opj", widgets["review_table"].item(1, 0).text())
            self.assertEqual("等待人工确认", widgets["review_table"].item(1, 2).text())
            self.assertEqual("检测到的 Book", widgets["review_table"].horizontalHeaderItem(1).text())
            self.assertEqual("检查中", widgets["phase_labels"]["data_check"].text().splitlines()[-1])
            self.assertEqual("phase_text_active", widgets["phase_labels"]["data_check"].objectName())
            self.assertEqual("phase_text_done", widgets["phase_labels"]["source_input"].objectName())
            self.assertFalse(widgets["review_table"].isHidden())
            self.assertFalse(widgets["attention_pane"].isHidden())
            self.assertIn("PFL_RT", widgets["attention_labels"][0].text())

            update_production_runtime_view(
                widgets,
                runtime_status="正在生成输出文件",
                title="生成输出文件",
                subtitle="正在写入新的 Origin 项目。",
                progress=92,
                summary_numbers=("12", "11", "0", "1"),
                review_rows=(),
                attention_message="",
                show_review_table=False,
                show_attention=False,
                show_input_controls=False,
            )

            self.assertTrue(widgets["select_sources_button"].isHidden())
            self.assertTrue(widgets["select_output_parent_button"].isHidden())
            self.assertTrue(widgets["start_run_button"].isHidden())
            self.assertTrue(widgets["preflight_settings_summary_label"].isHidden())
            self.assertEqual("正在生成输出文件", widgets["app_run_status"].text())
            self.assertEqual("生成输出文件", widgets["current_task_title"].text())
            self.assertEqual(92, widgets["run_progress"].value())
            self.assertEqual(["12", "11", "0", "1"], [label.text() for label in widgets["summary_number_labels"]])
            self.assertEqual(0, widgets["review_table"].rowCount())
            self.assertTrue(widgets["review_table"].isHidden())
            self.assertTrue(widgets["attention_pane"].isHidden())
        finally:
            window.close()
            QtWidgets.QApplication.processEvents()

    def test_source_input_issue_pane_renders_reason_and_advice_as_separate_lines(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6 import QtCore, QtWidgets

        issue_message = app_module._input_issue_message(
            (
                {
                    "source_id": "S0001",
                    "original_path": 'C:/raw/Paper<&".opju',
                    "reason": "原因 <b>未处理</b> & 待确认。",
                    "recommendation": '请选择 "原始光谱" <Book>。',
                },
            )
        )
        window, widgets = create_production_main_window(
            dpi_percent=100,
            size_name="desktop",
            stage="source_input",
        )
        try:
            update_production_runtime_view(
                widgets,
                attention_message=issue_message,
                show_attention=True,
            )
            window.show()
            QtWidgets.QApplication.processEvents()

            label = widgets["attention_labels"][0]
            pane = widgets["attention_pane"]
            self.assertEqual(QtCore.Qt.TextFormat.RichText, label.textFormat())
            self.assertIn(
                "<b>Paper&lt;&amp;&quot;.opju</b><br>"
                "原因 &lt;b&gt;未处理&lt;/b&gt; &amp; 待确认。<br>"
                "<b>处理建议</b><br>"
                "请选择 &quot;原始光谱&quot; &lt;Book&gt;。",
                label.text(),
            )
            self.assertNotIn("<b>未处理</b>", label.text())
            self.assertNotIn("<Book>", label.text())
            self.assertGreaterEqual(
                label.sizeHint().height(),
                6 * label.fontMetrics().lineSpacing(),
            )
            self.assertEqual(0, pane.horizontalScrollBar().maximum())
        finally:
            window.close()
            QtWidgets.QApplication.processEvents()

    def test_runtime_status_and_active_phase_use_loaders_without_dimming_text(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6 import QtGui, QtTest, QtWidgets

        def rendered_pixels(widget):
            image = widget.grab().toImage().convertToFormat(
                QtGui.QImage.Format.Format_RGBA8888
            )
            return image.bits().tobytes()

        window, widgets = create_production_main_window(
            dpi_percent=100,
            size_name="desktop",
            stage="source_input",
        )
        try:
            window.show()
            QtWidgets.QApplication.processEvents()
            status_group = window.findChild(QtWidgets.QWidget, "run_status_group")
            status_label = widgets["app_run_status"]
            status_loader = widgets["run_status_indicator"]
            active_dot = widgets["phase_dots"]["source_input"]

            update_production_runtime_view(
                widgets,
                runtime_status="读取源文件 3/3",
                activity_mode="automatic",
                stage="source_input",
                phase_detail="3/3 文件",
                progress_busy=True,
            )
            QtTest.QTest.qWait(80)
            status_geometry = status_group.geometry()
            label_geometry = status_label.geometry()
            loader_geometry = status_loader.geometry()
            dot_geometry = active_dot.geometry()
            self.assertIsNone(status_group.graphicsEffect())
            self.assertIsNone(status_label.graphicsEffect())
            automatic_samples = []
            phase_samples = []
            for _ in range(4):
                QtTest.QTest.qWait(90)
                automatic_samples.append(rendered_pixels(status_loader))
                phase_samples.append(rendered_pixels(active_dot))
            self.assertEqual(status_geometry, status_group.geometry())
            self.assertEqual(label_geometry, status_label.geometry())
            self.assertEqual(loader_geometry, status_loader.geometry())
            self.assertEqual(dot_geometry, active_dot.geometry())
            self.assertGreater(len(set(automatic_samples)), 1)
            self.assertGreater(len(set(phase_samples)), 1)
            self.assertEqual((0, 0), (
                widgets["run_progress"].minimum(),
                widgets["run_progress"].maximum(),
            ))

            update_production_runtime_view(
                widgets,
                runtime_status="等待确认样品归属",
                activity_mode="manual",
                stage="attribution",
                phase_detail="0/3 项",
                progress=73,
                progress_busy=False,
            )
            manual_dot = widgets["phase_dots"]["attribution"]
            QtWidgets.QApplication.processEvents()
            manual_geometry = manual_dot.geometry()
            manual_status_samples = []
            manual_phase_samples = []
            manual_progress_values = []
            for _ in range(4):
                QtTest.QTest.qWait(140)
                manual_status_samples.append(rendered_pixels(status_loader))
                manual_phase_samples.append(rendered_pixels(manual_dot))
                manual_progress_values.append(widgets["run_progress"].value())

            self.assertEqual(1, len(set(manual_status_samples)))
            self.assertEqual(1, len(set(manual_phase_samples)))
            self.assertEqual([73, 73, 73, 73], manual_progress_values)
            self.assertEqual((0, 100), (
                widgets["run_progress"].minimum(),
                widgets["run_progress"].maximum(),
            ))
            self.assertEqual(manual_geometry, manual_dot.geometry())

            update_production_runtime_view(
                widgets,
                runtime_status="本次运行已完成",
                activity_mode="idle",
                stage="complete",
                phase_detail="已完成",
            )
            complete_dot = widgets["phase_dots"]["complete"]
            QtTest.QTest.qWait(80)
            idle_label_geometry = status_label.geometry()
            idle_loader_geometry = status_loader.geometry()
            idle_dot_geometry = complete_dot.geometry()
            idle_status_samples = []
            idle_phase_samples = []
            for _ in range(4):
                QtTest.QTest.qWait(140)
                idle_status_samples.append(rendered_pixels(status_loader))
                idle_phase_samples.append(rendered_pixels(complete_dot))

            self.assertEqual(1, len(set(idle_status_samples)))
            self.assertEqual(1, len(set(idle_phase_samples)))
            self.assertEqual(idle_label_geometry, status_label.geometry())
            self.assertEqual(idle_loader_geometry, status_loader.geometry())
            self.assertEqual(idle_dot_geometry, complete_dot.geometry())
        finally:
            window.close()
            QtWidgets.QApplication.processEvents()

    def test_runtime_review_row_updates_patch_only_the_target_row(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6 import QtTest, QtWidgets

        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

        class CountingTable(QtWidgets.QTableWidget):
            def __init__(self):
                self.row_count_calls = 0
                self.resize_rows_calls = 0
                self.resize_row_calls = []
                self.item_writes = []
                super().__init__(0, 3)

            def setRowCount(self, count):
                self.row_count_calls += 1
                super().setRowCount(count)

            def resizeRowsToContents(self):
                self.resize_rows_calls += 1
                super().resizeRowsToContents()

            def resizeRowToContents(self, row):
                self.resize_row_calls.append(row)
                super().resizeRowToContents(row)

            def setItem(self, row, column, item):
                self.item_writes.append((row, column, item.text()))
                super().setItem(row, column, item)

            def reset_counts(self):
                self.row_count_calls = 0
                self.resize_rows_calls = 0
                self.resize_row_calls.clear()
                self.item_writes.clear()

        table = CountingTable()
        widgets = {
            "review_table": table,
            "runtime_stage": "source_input",
            "table_space_filler": QtWidgets.QWidget(),
        }
        initial_rows = (
            ("a.opju", "等待统计", "等待读取"),
            ("b.opju", "等待统计", "等待读取"),
        )

        update_production_runtime_view(widgets, review_rows=initial_rows)

        self.assertEqual(1, table.row_count_calls)
        self.assertEqual(0, table.resize_rows_calls)
        self.assertEqual([], table.resize_row_calls)
        self.assertEqual(6, len(table.item_writes))
        QtTest.QTest.qWait(200)
        self.assertEqual(0, table.resize_rows_calls)
        self.assertEqual({0, 1}, set(table.resize_row_calls))

        table.reset_counts()
        update_production_runtime_view(
            widgets,
            review_row_update=("source_input", 1, ("b.opju", "等待统计", "正在读取")),
            show_review_table=True,
        )
        QtTest.QTest.qWait(120)
        self.assertEqual(0, table.row_count_calls)
        self.assertEqual(0, table.resize_rows_calls)
        self.assertEqual([1], table.resize_row_calls)
        self.assertEqual(
            [
                (1, 0, "b.opju"),
                (1, 1, "等待统计"),
                (1, 2, "正在读取"),
            ],
            table.item_writes,
        )

        table.reset_counts()
        update_production_runtime_view(
            widgets,
            review_row_update=("source_input", 1, ("b.opju", "7", "已提取 6，排除 1")),
            show_review_table=True,
        )
        self.assertEqual(0, table.row_count_calls)
        self.assertEqual(0, table.resize_rows_calls)
        self.assertEqual([1], table.resize_row_calls)
        self.assertEqual(3, len(table.item_writes))
        self.assertEqual("7", table.item(1, 1).text())
        self.assertEqual("已提取 6，排除 1", table.item(1, 2).text())

        table.reset_counts()
        widgets["runtime_stage"] = "attribution"
        update_production_runtime_view(
            widgets,
            review_row_update=("source_input", 0, ("a.opju", "99", "错误阶段")),
        )
        self.assertEqual([], table.item_writes)
        self.assertEqual([], table.resize_row_calls)
        self.assertEqual("等待统计", table.item(0, 1).text())
        table.close()
        app.processEvents()

    def test_review_header_refresh_reuses_wrap_delegate(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6 import QtWidgets

        window, widgets = create_production_main_window(
            dpi_percent=100,
            size_name="desktop",
            stage="attribution",
        )
        table = widgets["review_table"]
        initial_delegate = table.itemDelegate()
        initial_delegate_count = len(table.findChildren(QtWidgets.QStyledItemDelegate))
        try:
            for headers in (
                ("来源文件", "Folder", "识别结果"),
                ("来源文件", "Folder / Book", "排除原因"),
                ("来源文件", "检测到的 Book", "检查结果"),
            ):
                update_production_runtime_view(widgets, review_headers=headers)
            self.assertIs(initial_delegate, table.itemDelegate())
            self.assertEqual(
                initial_delegate_count,
                len(table.findChildren(QtWidgets.QStyledItemDelegate)),
            )
        finally:
            window.close()
            QtWidgets.QApplication.processEvents()

    def test_production_dynamic_regions_scroll_when_content_overflows(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6 import QtCore, QtWidgets

        window, widgets = create_production_main_window(dpi_percent=100, size_name="desktop", stage="attribution")
        try:
            update_production_runtime_view(widgets, show_attention=True)
            window.show()
            QtWidgets.QApplication.processEvents()

            run_log = widgets["run_log"]
            for index in range(80):
                append_plain = getattr(run_log, "appendPlainText", None)
                if append_plain is not None:
                    append_plain(f"10:42:{index:02d}  动态日志测试 {index}")
                else:
                    run_log.append(f"10:42:{index:02d}  动态日志测试 {index}")

            table = widgets["review_table"]
            table.setRowCount(80)
            for row in range(4, 80):
                table.setItem(row, 0, QtWidgets.QTableWidgetItem(f"source-{row}"))
                table.setItem(row, 1, QtWidgets.QTableWidgetItem(f"Folder_{row}"))
                table.setItem(row, 2, QtWidgets.QTableWidgetItem("等待人工确认"))

            attention_layout = widgets["attention_body"].layout()
            for index in range(40):
                label = QtWidgets.QLabel(f"需要你的确认\n动态提醒 {index} - PFL_RT 的样品状态无法可靠推断。")
                label.setWordWrap(True)
                label.setMinimumWidth(280)
                label.setMinimumHeight(72)
                attention_layout.insertWidget(attention_layout.count() - 1, label)

            QtWidgets.QApplication.processEvents()

            self.assertEqual(QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded, run_log.verticalScrollBarPolicy())
            self.assertGreater(run_log.verticalScrollBar().maximum(), 0)
            self.assertTrue(run_log.verticalScrollBar().isVisible())
            self.assertGreater(table.verticalScrollBar().maximum(), 0)
            self.assertGreater(widgets["attention_pane"].verticalScrollBar().maximum(), 0)
        finally:
            window.close()
            QtWidgets.QApplication.processEvents()

    def test_rejection_table_allocates_readable_space_and_preserves_full_reason(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6 import QtWidgets

        window, widgets = create_production_main_window(
            dpi_percent=100,
            size_name="desktop",
            stage="attribution",
        )
        reason = "S1 最大值超过设定上限（最大值：2345678；对应 X：412）"
        try:
            window.show()
            update_production_runtime_view(
                widgets,
                review_headers=("来源文件", "Folder / Book", "排除原因"),
                review_rows=(("source.opj", "Folder_77K / 285_2_2", reason),),
                show_review_table=True,
            )
            QtWidgets.QApplication.processEvents()

            table = widgets["review_table"]
            self.assertEqual(reason, table.item(0, 2).text())
            self.assertEqual(reason, table.item(0, 2).toolTip())
            self.assertGreaterEqual(table.columnWidth(2), table.columnWidth(1))
            self.assertGreaterEqual(table.rowHeight(0), 40)
        finally:
            window.close()
            QtWidgets.QApplication.processEvents()

    def test_rejection_table_recomputes_wrapped_row_heights_after_final_layout(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6 import QtTest, QtWidgets

        window, widgets = create_production_main_window(
            dpi_percent=100,
            size_name="desktop",
            stage="attribution",
        )
        reason = (
            "S1 最大值超过设定上限（最大值：2345678；对应 X：412；"
            "该谱图不参与后续归属与输出；请检查原始测试条件与预检设置）"
        )
        rows = tuple(
            (f"source-{index}.opj", f"Folder_77K / Book_{index}_with_a_long_name", reason)
            for index in range(12)
        )
        try:
            window.resize(760, 650)
            window.show()
            update_production_runtime_view(
                widgets,
                review_headers=("来源文件", "Folder / Book", "排除原因"),
                review_rows=rows,
                show_review_table=True,
            )
            QtTest.QTest.qWait(200)

            table = widgets["review_table"]
            rendered_heights = tuple(
                table.rowHeight(row) for row in range(table.rowCount())
            )
            table.resizeRowsToContents()
            QtWidgets.QApplication.processEvents()
            for row, rendered_height in enumerate(rendered_heights):
                self.assertGreaterEqual(
                    rendered_height,
                    table.rowHeight(row),
                    row,
                )
        finally:
            window.close()
            QtWidgets.QApplication.processEvents()

    def test_runtime_source_rows_use_available_height_and_scroll_only_on_real_overflow(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6 import QtWidgets

        for dpi_percent in (100,):
            for row_count in (1, 4, 8, 20):
                rows = tuple(
                    (f"source-{index}.opj", "等待统计", "等待读取")
                    for index in range(row_count)
                )
                window, widgets = create_production_main_window(
                    dpi_percent=dpi_percent,
                    size_name="desktop",
                    stage="source_input",
                )
                try:
                    window.show()
                    QtWidgets.QApplication.processEvents()

                    update_production_runtime_view(
                        widgets,
                        stage="source_input",
                        review_rows=rows,
                        show_review_table=True,
                        show_input_controls=False,
                    )
                    QtWidgets.QApplication.processEvents()

                    table = widgets["review_table"]
                    scrollbar = table.verticalScrollBar()
                    self.assertEqual(
                        QtWidgets.QAbstractItemView.ScrollMode.ScrollPerPixel,
                        table.verticalScrollMode(),
                    )
                    self.assertLessEqual(table.verticalScrollBar().singleStep(), 8)
                    content_height = sum(table.rowHeight(row) for row in range(row_count))
                    self.assertGreater(table.height(), 300, (dpi_percent, row_count))
                    if content_height <= table.viewport().height():
                        self.assertEqual(0, scrollbar.maximum(), (dpi_percent, row_count))
                    else:
                        self.assertGreater(scrollbar.maximum(), 0, (dpi_percent, row_count))
                        table.scrollToItem(
                            table.item(row_count - 1, 0),
                            QtWidgets.QAbstractItemView.ScrollHint.PositionAtBottom,
                        )
                        QtWidgets.QApplication.processEvents()

                    last_item_rect = table.visualItemRect(table.item(row_count - 1, 0))
                    self.assertGreaterEqual(last_item_rect.top(), 0, (dpi_percent, row_count))
                    self.assertLess(
                        last_item_rect.bottom(),
                        table.viewport().height(),
                        (dpi_percent, row_count),
                    )
                finally:
                    window.close()
                    QtWidgets.QApplication.processEvents()

    def test_runtime_tables_wrap_complete_dynamic_status_and_rejection_context(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6 import QtCore, QtTest, QtWidgets

        window, widgets = create_production_main_window(
            dpi_percent=100,
            size_name="desktop",
            stage="source_input",
        )
        try:
            window.show()
            QtWidgets.QApplication.processEvents()
            status = (
                "正在读取 · 已用时 00:34 · 正在核对工作表、数据列、"
                "配对横坐标以及计划提取的强度数据"
            )
            update_production_runtime_view(
                widgets,
                stage="source_input",
                review_headers=("来源文件", "检测到的 Book", "处理状态"),
                review_rows=(("source.opj", "等待统计", status),),
                show_review_table=True,
                show_input_controls=False,
            )
            QtTest.QTest.qWait(80)
            table = widgets["review_table"]
            self.assertTrue(table.wordWrap())
            self.assertGreater(table.columnWidth(2), table.columnWidth(1))
            self.assertEqual(status, table.item(0, 2).text())
            self.assertEqual(status, table.item(0, 2).toolTip())
            self.assertGreater(table.rowHeight(0), 40)

            def assert_rendered_text_fits(row, column):
                item = table.item(row, column)
                rect = table.visualItemRect(item).adjusted(8, 4, -8, -4)
                bounds = table.fontMetrics().boundingRect(
                    QtCore.QRect(0, 0, max(1, rect.width()), 10_000),
                    QtCore.Qt.TextFlag.TextWrapAnywhere,
                    item.text(),
                )
                self.assertGreaterEqual(rect.height(), bounds.height())

            assert_rendered_text_fits(0, 2)

            folder_book = (
                "Root/Folder_with_a_very_long_unbroken_identity_"
                "Book_with_an_equally_long_user_renamed_identity_285_2_2"
            )
            reason = "缺少拟提取的 Y 列：S1c/R1c；该记录不能进入后续归属"
            update_production_runtime_view(
                widgets,
                stage="attribution",
                review_headers=("来源文件", "Folder / Book", "排除原因"),
                review_rows=(("source.opj", folder_book, reason),),
            )
            QtTest.QTest.qWait(80)
            self.assertGreater(table.columnWidth(2), table.columnWidth(0))
            self.assertEqual(folder_book, table.item(0, 1).text())
            self.assertEqual(folder_book, table.item(0, 1).toolTip())
            self.assertEqual(reason, table.item(0, 2).text())
            self.assertGreater(table.rowHeight(0), 40)
            self.assertEqual(
                QtCore.Qt.TextElideMode.ElideNone,
                table.textElideMode(),
            )
            assert_rendered_text_fits(0, 1)
            assert_rendered_text_fits(0, 2)
        finally:
            window.close()
            QtWidgets.QApplication.processEvents()

    def test_production_window_never_exceeds_available_screen_geometry(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6 import QtWidgets

        window, _widgets = create_production_main_window(
            dpi_percent=150,
            size_name="desktop",
            stage="source_input",
        )
        try:
            window.show()
            QtWidgets.QApplication.processEvents()
            available = window.screen().availableGeometry()
            self.assertLessEqual(window.width(), available.width())
            self.assertLessEqual(window.height(), available.height())
            self.assertTrue(available.contains(window.frameGeometry()))
        finally:
            window.close()
            QtWidgets.QApplication.processEvents()

    def test_main_window_title_drag_can_move_partly_beyond_screen_edges(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6 import QtCore, QtWidgets

        class PointerEvent:
            def __init__(self, point, *, pressed):
                self._point = point
                self._pressed = pressed

            def button(self):
                return QtCore.Qt.MouseButton.LeftButton

            def buttons(self):
                return (
                    QtCore.Qt.MouseButton.LeftButton
                    if self._pressed
                    else QtCore.Qt.MouseButton.NoButton
                )

            def globalPosition(self):
                return QtCore.QPointF(self._point)

            def accept(self):
                pass

        window, _widgets = create_production_main_window(
            dpi_percent=100,
            size_name="desktop",
            stage="source_input",
        )
        try:
            window.show()
            QtWidgets.QApplication.processEvents()
            window.move(100, 100)
            header = window.findChild(QtWidgets.QFrame, "title_status_bar")
            start = window.frameGeometry().topLeft() + QtCore.QPoint(40, 20)
            header.mousePressEvent(PointerEvent(start, pressed=True))
            header.mouseMoveEvent(
                PointerEvent(QtCore.QPoint(-120, -80), pressed=True)
            )
            header.mouseReleaseEvent(
                PointerEvent(QtCore.QPoint(-120, -80), pressed=False)
            )
            QtWidgets.QApplication.processEvents()

            self.assertLess(window.frameGeometry().left(), 0)
            self.assertLess(window.frameGeometry().top(), 0)
        finally:
            window.close()
            QtWidgets.QApplication.processEvents()

    def test_window_refit_round_trip_restores_preferred_size_and_minimum(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6 import QtCore, QtWidgets

        window, _widgets = create_production_main_window(
            dpi_percent=100,
            size_name="desktop",
            stage="source_input",
        )
        try:
            window.setMinimumSize(960, 680)
            window.resize(1180, 820)
            available = QtCore.QRect(1920, 0, 533, 533)
            desired = QtCore.QPoint(2100, 100)
            qt_main_window_module._fit_window_to_available_geometry(
                window,
                available,
                desired,
            )
            self.assertLessEqual(window.width(), available.width())
            self.assertLessEqual(window.height(), available.height())
            self.assertTrue(available.contains(window.frameGeometry()))

            larger_available = QtCore.QRect(0, 0, 1920, 1080)
            qt_main_window_module._fit_window_to_available_geometry(
                window,
                larger_available,
                QtCore.QPoint(100, 80),
            )
            self.assertEqual(QtCore.QSize(1180, 820), window.size())
            self.assertEqual(QtCore.QSize(960, 680), window.minimumSize())
            self.assertTrue(larger_available.contains(window.frameGeometry()))
        finally:
            window.close()
            QtWidgets.QApplication.processEvents()

    def test_window_screen_tracking_resizes_without_reclamping_position(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6 import QtCore, QtWidgets

        class Signal:
            def __init__(self):
                self.callbacks = []

            def connect(self, callback):
                self.callbacks.append(callback)

            def disconnect(self, callback):
                self.callbacks.remove(callback)

            def emit(self, *args):
                for callback in tuple(self.callbacks):
                    callback(*args)

        class Screen:
            def __init__(self, geometry):
                self.geometry = geometry
                self.availableGeometryChanged = Signal()

            def availableGeometry(self):
                return self.geometry

        class WindowHandle:
            def __init__(self, screen):
                self._screen = screen
                self.screenChanged = Signal()

            def screen(self):
                return self._screen

            def change_screen(self, screen):
                self._screen = screen
                self.screenChanged.emit(screen)

        window, _widgets = create_production_main_window(
            dpi_percent=100,
            size_name="desktop",
            stage="source_input",
        )
        first_screen = Screen(QtCore.QRect(0, 0, 1920, 1080))
        second_screen = Screen(QtCore.QRect(1920, 0, 700, 600))
        handle = WindowHandle(first_screen)
        try:
            window.move(100, 80)
            qt_main_window_module._enable_screen_geometry_tracking(window, handle)
            handle.change_screen(second_screen)
            self.assertEqual(
                QtCore.QPoint(100, 80),
                window.frameGeometry().topLeft(),
            )
            self.assertLessEqual(window.width(), second_screen.geometry.width())
            self.assertLessEqual(window.height(), second_screen.geometry.height())

            second_screen.geometry = QtCore.QRect(1920, 40, 640, 500)
            second_screen.availableGeometryChanged.emit(second_screen.geometry)
            self.assertEqual(
                QtCore.QPoint(100, 80),
                window.frameGeometry().topLeft(),
            )
            self.assertLessEqual(window.width(), second_screen.geometry.width())
            self.assertLessEqual(window.height(), second_screen.geometry.height())
            self.assertEqual(1, len(second_screen.availableGeometryChanged.callbacks))
        finally:
            window.close()
            QtWidgets.QApplication.processEvents()

    def test_queued_review_table_resize_is_safe_after_window_deletion(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6 import QtCore, QtTest, QtWidgets

        uncaught = []
        original_hook = sys.excepthook
        sys.excepthook = lambda *details: uncaught.append(details)
        window, widgets = create_production_main_window(
            dpi_percent=100,
            size_name="desktop",
            stage="attribution",
        )
        try:
            window.show()
            update_production_runtime_view(
                widgets,
                review_rows=(("source.opj", "Folder / Book", "排除原因"),),
                show_review_table=True,
            )
            window.deleteLater()
            QtCore.QCoreApplication.sendPostedEvents(
                window,
                QtCore.QEvent.Type.DeferredDelete,
            )
            QtTest.QTest.qWait(120)
            self.assertEqual([], uncaught)
        finally:
            sys.excepthook = original_hook
            try:
                window.close()
            except RuntimeError:
                pass
            QtWidgets.QApplication.processEvents()

    def test_production_window_is_exercised_at_real_qt_scale_factors(self):
        script = r'''
import pathlib
import sys

root = pathlib.Path.cwd()
sys.path.insert(0, str(root / "src"))

from PySide6 import QtTest, QtWidgets
from spectrum_organizer.ui.qt_main_window import (
    create_production_main_window,
    update_production_runtime_view,
)

dpi_percent = int(sys.argv[1])
window, widgets = create_production_main_window(
    dpi_percent=dpi_percent,
    size_name="desktop",
    stage="source_input",
)
window.show()
QtWidgets.QApplication.processEvents()
long_identity = "Root/" + ("Folder_with_a_long_unbroken_identity_" * 5)
rows = (("source-0.opj", long_identity, "等待读取"),) + tuple(
    (f"source-{index}.opj", "等待统计", "等待读取") for index in range(1, 20)
)
update_production_runtime_view(
    widgets,
    stage="source_input",
    review_rows=rows,
    show_review_table=True,
    show_input_controls=False,
)
QtTest.QTest.qWait(120)
table = widgets["review_table"]
delegate = table.itemDelegate()
required_height = max(
    delegate.sizeHint(
        QtWidgets.QStyleOptionViewItem(),
        table.model().index(0, column),
    ).height()
    for column in range(table.columnCount())
)
if table.rowHeight(0) < required_height:
    raise SystemExit(
        f"wrapped row is clipped: row={table.rowHeight(0)}, required={required_height}"
    )
if table.verticalScrollMode() != QtWidgets.QAbstractItemView.ScrollMode.ScrollPerPixel:
    raise SystemExit("review table must scroll per pixel")
scrollbar = table.verticalScrollBar()
scrollbar.setValue(0)
QtWidgets.QApplication.processEvents()
first_rect = table.visualItemRect(table.item(0, 1))
if first_rect.top() < 0 or first_rect.top() >= table.viewport().height():
    raise SystemExit(
        f"wrapped row top is not reachable: rect={first_rect}, viewport={table.viewport().height()}"
    )
scrollbar.setValue(max(0, table.rowHeight(0) - table.viewport().height() + 1))
QtWidgets.QApplication.processEvents()
first_rect = table.visualItemRect(table.item(0, 1))
if first_rect.bottom() <= 0 or first_rect.bottom() >= table.viewport().height():
    raise SystemExit(
        f"wrapped row bottom is not reachable: rect={first_rect}, viewport={table.viewport().height()}"
    )
table.scrollToItem(
    table.item(19, 0),
    QtWidgets.QAbstractItemView.ScrollHint.PositionAtBottom,
)
QtTest.QTest.qWait(40)
last_rect = table.visualItemRect(table.item(19, 0))
if last_rect.bottom() <= 0 or last_rect.bottom() >= table.viewport().height():
    raise SystemExit(
        f"last row is not reachable: rect={last_rect}, viewport={table.viewport().height()}"
    )
available = window.screen().availableGeometry()
print(
    f"{window.devicePixelRatioF():.3f}|{table.height()}|{table.viewport().height()}|"
    f"{window.width()}|{window.height()}|{available.width()}|{available.height()}|"
    f"{table.viewport().width()}|{table.columnWidth(1)}|{table.rowHeight(0)}|"
    f"{table.horizontalScrollBar().maximum()}"
)
window.close()
QtWidgets.QApplication.processEvents()
'''
        for dpi_percent, scale_factor in ((100, "1"), (125, "1.25"), (150, "1.5")):
            with self.subTest(dpi_percent=dpi_percent):
                env = os.environ.copy()
                env["QT_QPA_PLATFORM"] = "offscreen"
                env["QT_SCALE_FACTOR"] = scale_factor
                env["QT_SCALE_FACTOR_ROUNDING_POLICY"] = "PassThrough"
                completed = subprocess.run(
                    [sys.executable, "-c", script, str(dpi_percent)],
                    cwd=ROOT,
                    env=env,
                    text=True,
                    capture_output=True,
                    timeout=15,
                    check=False,
                )
                self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
                (
                    dpr_text,
                    table_height,
                    viewport_height,
                    window_width,
                    window_height,
                    available_width,
                    available_height,
                    viewport_width,
                    identity_column_width,
                    long_row_height,
                    horizontal_scroll_maximum,
                ) = completed.stdout.strip().split("|")
                self.assertAlmostEqual(float(scale_factor), float(dpr_text), places=2)
                self.assertGreaterEqual(int(table_height), 100)
                self.assertGreaterEqual(int(viewport_height), 60)
                self.assertLessEqual(int(window_width), int(available_width))
                self.assertLessEqual(int(window_height), int(available_height))
                self.assertGreaterEqual(int(identity_column_width), 140)
                if int(viewport_width) < 680:
                    self.assertGreater(int(horizontal_scroll_maximum), 0)
    def test_production_focus_order_rejects_extra_focusable_controls(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6 import QtWidgets

        window, widgets = create_production_main_window(dpi_percent=100, size_name="desktop")
        extra = QtWidgets.QPushButton("Unexpected")
        extra.setObjectName("unexpected_focus_button")
        task_panel = window.findChild(QtWidgets.QWidget, "central_task_area")
        task_panel.layout().addWidget(extra)
        QtWidgets.QWidget.setTabOrder(widgets["select_output_parent_button"], extra)
        QtWidgets.QWidget.setTabOrder(extra, widgets["start_run_button"])
        try:
            window.show()
            QtWidgets.QApplication.processEvents()
            focus_ok = _production_focus_order_matches(widgets)
        finally:
            window.close()
            QtWidgets.QApplication.processEvents()

        self.assertFalse(focus_ok)

    def test_production_completion_focus_order_includes_all_three_actions(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6 import QtWidgets

        window, widgets = create_production_main_window(
            dpi_percent=100,
            size_name="desktop",
            stage="complete",
        )
        try:
            window.show()
            QtWidgets.QApplication.processEvents()
            self.assertTrue(_production_focus_order_matches(widgets))
        finally:
            window.close()
            QtWidgets.QApplication.processEvents()

    def test_dpi_policy_reflows_without_shrinking_fonts_below_minima(self):
        for dpi_percent in (100, 125, 150):
            with self.subTest(dpi_percent=dpi_percent):
                policy = scaled_font_policy(dpi_percent)

                self.assertEqual(dpi_percent, policy.dpi_percent)
                self.assertEqual(FONT_SIZES_PX, policy.font_sizes_px)
                self.assertTrue(policy.reflow_or_scroll)
                self.assertFalse(policy.clip_text)

    def test_book_only_startup_has_no_graph_template_prompt(self):
        workflows = build_startup_workflows()

        self.assertEqual(WorkflowMode.BOOK_ONLY, workflows.enabled_workflow)
        self.assertTrue(workflows.book_only_enabled)
        self.assertFalse(workflows.graph_generation_enabled)
        self.assertFalse(workflows.graph_generation_runnable)
        self.assertFalse(workflows.prompt_for_otpu)

    def test_source_selection_keeps_first_duplicate_and_aborts_on_unrecognized_file(self):
        orchestrator = BookOnlyOrchestrator(FakeSettingsStore())

        accepted = orchestrator.select_sources(["C:/raw/a.opju", "C:/raw/sub/../a.opju", "C:/raw/b.OPJ"])

        self.assertTrue(accepted.ok)
        self.assertEqual(("C:/raw/a.opju", "C:/raw/b.OPJ"), accepted.source_paths)
        self.assertEqual(("C:/raw/sub/../a.opju",), accepted.duplicate_paths)
        self.assertEqual(
            ("C:/raw/sub/../a.opju",),
            orchestrator.task_cache["ignored_duplicate_input_paths"],
        )

        aborted = orchestrator.select_sources(["C:/raw/a.opju", "C:/raw/readme.txt"])
        self.assertFalse(aborted.ok)
        self.assertEqual("unrecognized_source_file", aborted.reason)
        self.assertEqual((), aborted.source_paths)

    def test_source_selection_deduplicates_hard_links_to_the_same_file(self):
        orchestrator = BookOnlyOrchestrator(FakeSettingsStore())
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_root:
            original = pathlib.Path(temp_root) / "original.opju"
            alias = pathlib.Path(temp_root) / "alias.opju"
            original.write_bytes(b"origin project placeholder")
            os.link(original, alias)

            accepted = orchestrator.select_sources([str(original), str(alias)])

        self.assertTrue(accepted.ok)
        self.assertEqual((str(original),), accepted.source_paths)
        self.assertEqual((str(alias),), accepted.duplicate_paths)

    def test_combined_preflight_dialog_exposes_editable_s1_limit_and_steady_emission_choice(self):
        request = preflight_settings_dialog(default_s1_limit=1000000, steady_emission_y="S1c")

        self.assertEqual("preflight_settings", request.kind)
        self.assertIn("S1 强度上限：1000000", request.message)
        self.assertIn("发射谱 Y 列：S1c", request.message)
        self.assertIn("适用于稳态谱和延迟谱；二维稳态谱不检查。", request.message)
        self.assertNotIn("稳态发射谱、稳态激发谱", request.message)
        self.assertIn("二维稳态谱不检查", request.message)
        self.assertIn("稳态激发谱固定使用 S1c/R1c", request.message)
        self.assertIn("延迟谱固定使用 S1c", request.message)
        self.assertNotIn("S1 limit", request.message)
        self.assertNotIn("稳态发射 Y", request.message)
        self.assertEqual(("confirm", "cancel"), request.actions)
        self.assertTrue(request.can_confirm)
        self.assertEqual("1000000", request.field_values["s1Limit"])
        self.assertEqual("S1c", request.field_values["steadyEmissionY"])

    def test_preflight_dialog_disables_confirmation_while_s1_limit_is_empty(self):
        from PySide6 import QtCore, QtWidgets

        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        observed = {}

        def inspect_dialog():
            dialog = app.activeModalWidget()
            self.assertIsInstance(dialog, QtWidgets.QDialog)
            self.assertEqual("预检设置", dialog.windowTitle())
            observed["visible_text"] = "\n".join(
                label.text() for label in dialog.findChildren(QtWidgets.QLabel)
            )
            line_edit = dialog.findChild(QtWidgets.QLineEdit)
            confirm_button = next(
                button for button in dialog.findChildren(QtWidgets.QPushButton) if button.text() == "确认"
            )
            line_edit.clear()
            observed["enabled"] = confirm_button.isEnabled()
            dialog.reject()

        QtCore.QTimer.singleShot(0, inspect_dialog)
        result = app_module.QtPreflightDialogPort(QtWidgets, QtCore).confirm(
            None,
            default_s1_limit=1_000_000,
            steady_emission_y="S1c",
        )

        self.assertIsNone(result)
        self.assertFalse(observed["enabled"])
        visible_text = observed["visible_text"]
        self.assertIn("S1 强度上限", visible_text)
        self.assertIn("发射谱 Y 列", visible_text)
        self.assertNotIn("稳态发射谱强度数据列", visible_text)
        self.assertIn("适用于稳态谱和延迟谱；二维稳态谱不检查。", visible_text)
        self.assertNotIn("稳态发射谱、稳态激发谱", visible_text)
        self.assertIn("二维稳态谱不检查", visible_text)
        self.assertIn("稳态激发谱固定使用 S1c/R1c", visible_text)
        self.assertIn("延迟谱固定使用 S1c", visible_text)
        self.assertNotIn("S1 limit", visible_text)
        self.assertNotIn("稳态发射 Y", visible_text)

    def test_preflight_actions_match_shared_workflow_weight_height_and_gap(self):
        from PySide6 import QtCore, QtWidgets

        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        observed = {}

        def inspect_dialog():
            dialog = app.activeModalWidget()
            buttons = {
                button.text(): button
                for button in dialog.findChildren(QtWidgets.QPushButton)
                if button.text() in {"确认", "取消"}
            }
            app.processEvents()
            confirm = buttons["确认"]
            cancel = buttons["取消"]
            left, right = sorted((confirm, cancel), key=lambda button: button.x())
            observed["heights"] = {button.height() for button in buttons.values()}
            observed["font_sizes"] = {
                button.font().pixelSize() for button in buttons.values()
            }
            observed["font_weights"] = {
                button.font().weight() for button in buttons.values()
            }
            observed["gap"] = right.x() - left.geometry().right() - 1
            dialog.reject()

        QtCore.QTimer.singleShot(0, inspect_dialog)
        app_module.QtPreflightDialogPort(QtWidgets, QtCore).confirm(
            None,
            default_s1_limit=1_000_000,
            steady_emission_y="S1c",
        )

        self.assertEqual({42}, observed["heights"])
        self.assertEqual({14}, observed["font_sizes"])
        self.assertEqual({600}, observed["font_weights"])
        self.assertEqual(8, observed["gap"])

    def test_preflight_dialog_reuses_screen_clamped_title_drag(self):
        self.assertIs(app_module._enable_dialog_drag, dialog_port_module._enable_title_bar_drag)

    def test_preflight_keyboard_focus_starts_on_first_decision_and_skips_close(self):
        from PySide6 import QtCore, QtTest, QtWidgets

        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        observed = {}

        def inspect_dialog():
            dialog = app.activeModalWidget()
            close_button = next(
                button
                for button in dialog.findChildren(QtWidgets.QPushButton)
                if button.text() == "×"
            )
            missing_s1 = next(
                checkbox
                for checkbox in dialog.findChildren(QtWidgets.QCheckBox)
                if checkbox.text() == "允许缺少 S1 列"
            )
            s1_limit = dialog.findChild(QtWidgets.QLineEdit)
            observed["close_focus_policy"] = close_button.focusPolicy()
            observed["initial_focus"] = dialog.focusWidget()
            observed["missing_s1"] = missing_s1
            observed["s1_limit"] = s1_limit
            QtTest.QTest.keyClick(dialog, QtCore.Qt.Key.Key_Tab)
            app.processEvents()
            observed["after_tab"] = dialog.focusWidget()
            dialog.reject()

        QtCore.QTimer.singleShot(0, inspect_dialog)
        app_module.QtPreflightDialogPort(QtWidgets, QtCore).confirm(
            None,
            default_s1_limit=2_000_000,
            steady_emission_y="S1c",
        )

        self.assertEqual(
            QtCore.Qt.FocusPolicy.NoFocus,
            observed["close_focus_policy"],
        )
        self.assertIs(observed["initial_focus"], observed["missing_s1"])
        self.assertIs(observed["after_tab"], observed["s1_limit"])

    def test_preflight_labels_align_with_their_input_controls(self):
        from PySide6 import QtCore, QtWidgets

        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        observed = {}

        def inspect_dialog():
            dialog = app.activeModalWidget()
            labels = {label.text(): label for label in dialog.findChildren(QtWidgets.QLabel)}
            line_edit = dialog.findChild(QtWidgets.QLineEdit)
            combo = dialog.findChild(QtWidgets.QComboBox)
            observed["label_texts"] = set(labels)
            observed["s1_delta"] = abs(
                labels["S1 强度上限"].geometry().center().y() - line_edit.geometry().center().y()
            )
            steady_label = labels.get("发射谱 Y 列")
            if steady_label is not None:
                observed["steady_delta"] = abs(
                    steady_label.geometry().center().y()
                    - combo.geometry().center().y()
                )
                observed["label_text_width_delta"] = abs(
                    labels["S1 强度上限"].fontMetrics().horizontalAdvance("S1 强度上限")
                    - steady_label.fontMetrics().horizontalAdvance(
                        "发射谱 Y 列"
                    )
                )
            dialog.reject()

        QtCore.QTimer.singleShot(0, inspect_dialog)
        app_module.QtPreflightDialogPort(QtWidgets, QtCore).confirm(
            None,
            default_s1_limit=2_000_000,
            steady_emission_y="S1c",
        )

        self.assertIn("发射谱 Y 列", observed["label_texts"])
        self.assertNotIn("稳态发射谱强度数据列", observed["label_texts"])
        self.assertLessEqual(observed["s1_delta"], 1)
        self.assertLessEqual(observed["steady_delta"], 1)
        self.assertLessEqual(observed["label_text_width_delta"], 8)

    @unittest.skipUnless(os.name == "nt", "native Windows font metrics only")
    def test_preflight_label_copy_is_balanced_on_native_windows(self):
        script = r'''
from PySide6 import QtCore, QtTest, QtWidgets

from spectrum_organizer.ui.app import QtPreflightDialogPort

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
observed = {}

def inspect_dialog():
    dialog = app.activeModalWidget()
    if dialog is None:
        observed["error"] = "preflight dialog did not become modal"
        app.quit()
        return
    labels = {
        label.text(): label
        for label in dialog.findChildren(QtWidgets.QLabel)
    }
    s1_label = labels.get("S1 强度上限")
    steady_label = labels.get("发射谱 Y 列")
    if s1_label is None or steady_label is None:
        observed["error"] = f"unexpected labels: {sorted(labels)}"
    else:
        observed["s1_width"] = s1_label.fontMetrics().horizontalAdvance(
            s1_label.text()
        )
        observed["steady_width"] = steady_label.fontMetrics().horizontalAdvance(
            steady_label.text()
        )
    dialog.reject()

QtCore.QTimer.singleShot(0, inspect_dialog)
QtPreflightDialogPort(QtWidgets, QtCore).confirm(
    None,
    default_s1_limit=2_000_000,
    steady_emission_y="S1c",
)
if "error" in observed:
    raise SystemExit(observed["error"])
delta = abs(observed["s1_width"] - observed["steady_width"])
print(f'{observed["s1_width"]}|{observed["steady_width"]}|{delta}')
if delta > 8:
    raise SystemExit(f"native label width delta {delta} exceeds 8 px")
'''
        env = os.environ.copy()
        env["PYTHONPATH"] = str(SRC)
        env["QT_QPA_PLATFORM"] = "windows"
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )

        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        s1_width, steady_width, delta = map(
            int,
            completed.stdout.strip().split("|"),
        )
        self.assertEqual(abs(s1_width - steady_width), delta)
        self.assertLessEqual(delta, 8)

    def test_preflight_value_controls_share_one_aligned_width(self):
        from PySide6 import QtCore, QtWidgets

        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        observed = {}

        def inspect_dialog():
            dialog = app.activeModalWidget()
            line_edit = dialog.findChild(QtWidgets.QLineEdit)
            combo = dialog.findChild(QtWidgets.QComboBox)
            observed["line_edit"] = QtCore.QRect(
                line_edit.mapTo(dialog, QtCore.QPoint(0, 0)),
                line_edit.size(),
            )
            observed["combo"] = QtCore.QRect(
                combo.mapTo(dialog, QtCore.QPoint(0, 0)),
                combo.size(),
            )
            dialog.reject()

        QtCore.QTimer.singleShot(0, inspect_dialog)
        app_module.QtPreflightDialogPort(QtWidgets, QtCore).confirm(
            None,
            default_s1_limit=2_000_000,
            steady_emission_y="S1c",
        )

        self.assertEqual(observed["line_edit"].left(), observed["combo"].left())
        self.assertEqual(observed["line_edit"].right(), observed["combo"].right())

    def test_preflight_missing_s1_content_aligns_with_value_controls(self):
        from PySide6 import QtCore, QtWidgets

        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        observed = {}

        def inspect_dialog():
            dialog = app.activeModalWidget()
            checkbox = next(
                item
                for item in dialog.findChildren(QtWidgets.QCheckBox)
                if item.text() == "允许缺少 S1 列"
            )
            missing_s1_help = next(
                item
                for item in dialog.findChildren(QtWidgets.QLabel)
                if item.text().startswith("仅在 S1 缺失时")
            )
            line_edit = dialog.findChild(QtWidgets.QLineEdit)
            combo = dialog.findChild(QtWidgets.QComboBox)
            for name, widget in {
                "checkbox": checkbox,
                "missing_s1_help": missing_s1_help,
                "line_edit": line_edit,
                "combo": combo,
            }.items():
                observed[name] = QtCore.QRect(
                    widget.mapTo(dialog, QtCore.QPoint(0, 0)),
                    widget.size(),
                )
            dialog.reject()

        QtCore.QTimer.singleShot(0, inspect_dialog)
        app_module.QtPreflightDialogPort(QtWidgets, QtCore).confirm(
            None,
            default_s1_limit=2_000_000,
            steady_emission_y="S1c",
        )

        expected_left = observed["line_edit"].left()
        expected_right = observed["line_edit"].right()
        self.assertEqual(expected_left, observed["combo"].left())
        self.assertEqual(expected_right, observed["combo"].right())
        self.assertEqual(expected_left, observed["checkbox"].left())
        self.assertEqual(expected_right, observed["checkbox"].right())
        self.assertEqual(expected_left, observed["missing_s1_help"].left())
        self.assertEqual(expected_right, observed["missing_s1_help"].right())

    def test_preflight_commits_visibility_and_geometry_synchronously(self):
        source = inspect.getsource(app_module.QtPreflightDialogPort.confirm)

        self.assertNotIn("dialog.adjustSize()", source)
        self.assertNotIn("refit_timer", source)
        self.assertIn("body.updateGeometry()", source)
        self.assertIn("refit_to_visible_content()", source)
        self.assertIn("dialog.resize(dialog.width(), target_height)", source)

    def test_preflight_controls_remain_reachable_in_compact_viewport(self):
        from PySide6 import QtCore, QtWidgets

        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        observed = {}

        def exercise_dialog():
            dialog = app.activeModalWidget()
            dialog.resize(400, 400)
            app.processEvents()
            observed["frame_width"] = dialog.frameGeometry().width()
            observed["frame_height"] = dialog.frameGeometry().height()
            checkbox = next(
                item
                for item in dialog.findChildren(QtWidgets.QCheckBox)
                if item.text() == "允许缺少 S1 列"
            )
            line_edit = dialog.findChild(QtWidgets.QLineEdit)
            combo = dialog.findChild(QtWidgets.QComboBox)
            confirm = next(
                item
                for item in dialog.findChildren(QtWidgets.QPushButton)
                if item.text() == "确认"
            )
            for name, widget in {
                "checkbox": checkbox,
                "line_edit": line_edit,
                "combo": combo,
                "confirm": confirm,
            }.items():
                top_left = widget.mapTo(dialog, QtCore.QPoint(0, 0))
                observed[name] = dialog.contentsRect().contains(
                    QtCore.QRect(top_left, widget.size())
                )
            line_edit.setText("2100000")
            combo.setCurrentText("S1c/R1c")
            confirm.click()

        QtCore.QTimer.singleShot(0, exercise_dialog)
        result = app_module.QtPreflightDialogPort(QtWidgets, QtCore).confirm(
            None,
            default_s1_limit=2_000_000,
            steady_emission_y="S1c",
        )

        self.assertTrue(all(observed.values()), observed)
        self.assertLessEqual(observed["frame_width"], 400)
        self.assertLessEqual(observed["frame_height"], 400)
        self.assertEqual(2_100_000, result["s1_limit"])
        self.assertEqual("S1c/R1c", result["steady_emission_y"])
        self.assertFalse(result["allow_missing_s1"])

    def test_preflight_missing_s1_option_defaults_off_and_returns_user_choice(self):
        from PySide6 import QtCore, QtWidgets

        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

        observed = {}

        def choose_missing_s1_option():
            dialog = app.activeModalWidget()
            checkbox = next(
                box
                for box in dialog.findChildren(QtWidgets.QCheckBox)
                if box.text() == "允许缺少 S1 列"
            )
            observed["initial_checked"] = checkbox.isChecked()
            checkbox.setChecked(True)
            next(
                button
                for button in dialog.findChildren(QtWidgets.QPushButton)
                if button.text() == "确认"
            ).click()

        QtCore.QTimer.singleShot(0, choose_missing_s1_option)
        result = app_module.QtPreflightDialogPort(QtWidgets, QtCore).confirm(
            None,
            default_s1_limit=2_000_000,
            steady_emission_y="S1c",
            allow_missing_s1=False,
        )

        self.assertFalse(observed["initial_checked"])
        self.assertEqual(
            {
                "s1_limit": 2_000_000,
                "steady_emission_y": "S1c",
                "allow_missing_s1": True,
            },
            result,
        )

    def test_preflight_missing_s1_shrinks_window_to_visible_content(self):
        from PySide6 import QtCore, QtWidgets

        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        observed = {}

        def inspect_collapsed_dialog():
            dialog = app.activeModalWidget()
            body = dialog.findChild(QtWidgets.QFrame, "dialog_body")
            checkbox = next(
                box
                for box in dialog.findChildren(QtWidgets.QCheckBox)
                if box.text() == "允许缺少 S1 列"
            )
            confirm = next(
                button
                for button in dialog.findChildren(QtWidgets.QPushButton)
                if button.text() == "确认"
            )
            observed["expanded_height"] = dialog.height()
            checkbox.setChecked(True)
            app.processEvents()
            observed["collapsed_height"] = dialog.height()
            observed["collapsed_hint"] = dialog.sizeHint().height()
            confirm_bottom = confirm.mapTo(
                dialog,
                confirm.rect().bottomLeft(),
            ).y()
            body_bottom = body.mapTo(
                dialog,
                body.rect().bottomLeft(),
            ).y()
            observed["blank_below_buttons"] = body_bottom - confirm_bottom
            dialog.reject()

        QtCore.QTimer.singleShot(0, inspect_collapsed_dialog)
        result = app_module.QtPreflightDialogPort(QtWidgets, QtCore).confirm(
            None,
            default_s1_limit=2_000_000,
            steady_emission_y="S1c",
            allow_missing_s1=False,
        )

        self.assertIsNone(result)
        self.assertLess(
            observed["collapsed_height"],
            observed["expanded_height"],
            observed,
        )
        self.assertLessEqual(
            abs(observed["collapsed_height"] - observed["collapsed_hint"]),
            2,
            observed,
        )
        self.assertLessEqual(observed["blank_below_buttons"], 20, observed)

    def test_preflight_missing_s1_burst_discards_stale_native_geometry(self):
        from PySide6 import QtCore, QtTest, QtWidgets

        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        observed = {}

        class DelayedNativeDialog(QtWidgets.QDialog):
            def adjustSize(self):
                target_height = self.sizeHint().height()
                s1_section = self.findChild(
                    QtWidgets.QWidget,
                    "preflight_s1_section",
                )
                delay_ms = 60 if s1_section.isVisible() else 10
                QtCore.QTimer.singleShot(
                    delay_ms,
                    lambda height=target_height: QtWidgets.QDialog.resize(
                        self,
                        self.width(),
                        height,
                    ),
                )

        class QtWidgetsProxy:
            QDialog = DelayedNativeDialog

            def __getattr__(self, name):
                return getattr(QtWidgets, name)

        def exercise_burst_toggles():
            dialog = app.activeModalWidget()
            checkbox = next(
                box
                for box in dialog.findChildren(QtWidgets.QCheckBox)
                if box.text() == "允许缺少 S1 列"
            )
            confirm = next(
                button
                for button in dialog.findChildren(QtWidgets.QPushButton)
                if button.text() == "确认"
            )
            cancel = next(
                button
                for button in dialog.findChildren(QtWidgets.QPushButton)
                if button.text() == "取消"
            )
            try:
                expanded_height = dialog.height()
                for _index in range(9):
                    checkbox.click()
                QtTest.QTest.qWait(120)
                app.processEvents()
                observed.update(
                    {
                        "checked": checkbox.isChecked(),
                        "expanded_height": expanded_height,
                        "height": dialog.height(),
                        "hint": dialog.sizeHint().height(),
                        "minimum": dialog.minimumHeight(),
                        "maximum": dialog.maximumHeight(),
                        "confirm_hit": dialog.childAt(
                            confirm.mapTo(dialog, confirm.rect().center())
                        )
                        is confirm,
                        "cancel_hit": dialog.childAt(
                            cancel.mapTo(dialog, cancel.rect().center())
                        )
                        is cancel,
                    }
                )
            finally:
                dialog.reject()

        QtCore.QTimer.singleShot(0, exercise_burst_toggles)
        result = app_module.QtPreflightDialogPort(
            QtWidgetsProxy(),
            QtCore,
        ).confirm(
            None,
            default_s1_limit=2_000_000,
            steady_emission_y="S1c",
            allow_missing_s1=False,
        )

        self.assertIsNone(result)
        self.assertTrue(observed["checked"], observed)
        self.assertLess(observed["height"], observed["expanded_height"], observed)
        self.assertLessEqual(
            abs(observed["height"] - observed["hint"]),
            2,
            observed,
        )
        self.assertLessEqual(observed["minimum"], observed["height"], observed)
        self.assertGreaterEqual(observed["maximum"], observed["height"], observed)
        self.assertTrue(observed["confirm_hit"], observed)
        self.assertTrue(observed["cancel_hit"], observed)

    @unittest.skipUnless(os.name == "nt", "native Windows geometry only")
    def test_preflight_burst_toggles_are_live_on_native_windows(self):
        script = r'''
import ctypes

from PySide6 import QtCore, QtWidgets

from spectrum_organizer.ui.app import QtPreflightDialogPort

user32 = ctypes.windll.user32
wm_mousemove = 0x0200
wm_lbuttondown = 0x0201
wm_lbuttonup = 0x0202
mk_lbutton = 0x0001
messages = []
QtCore.qInstallMessageHandler(
    lambda _kind, _context, message: messages.append(message)
)
app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
errors = []
controls = {}

def post_native_click(widget, count=1):
    dialog = controls["dialog"]
    point = widget.mapTo(dialog, QtCore.QPoint(6, widget.rect().center().y()))
    packed_point = (
        (point.x() & 0xffff)
        | ((point.y() & 0xffff) << 16)
    )
    hwnd = int(dialog.winId())
    for _index in range(count):
        user32.PostMessageW(hwnd, wm_lbuttondown, mk_lbutton, packed_point)
        user32.PostMessageW(hwnd, wm_lbuttonup, 0, packed_point)

def send_native_drag():
    dialog = controls["dialog"]
    header = controls["header"]
    hwnd = int(dialog.winId())
    start = header.mapTo(dialog, header.rect().center())
    finish = start + QtCore.QPoint(24, 16)
    start_packed = (start.x() & 0xffff) | ((start.y() & 0xffff) << 16)
    finish_packed = (finish.x() & 0xffff) | ((finish.y() & 0xffff) << 16)
    before = dialog.frameGeometry().topLeft()
    user32.SendMessageW(hwnd, wm_lbuttondown, mk_lbutton, start_packed)
    user32.SendMessageW(hwnd, wm_mousemove, mk_lbutton, finish_packed)
    user32.SendMessageW(hwnd, wm_lbuttonup, 0, finish_packed)
    app.processEvents()
    if dialog.frameGeometry().topLeft() == before:
        errors.append("native title drag did not move the dialog")

def header_divider_y():
    header = controls["header"]
    return header.mapToGlobal(header.rect().bottomLeft()).y()

def exercise_dialog():
    dialog = app.activeModalWidget()
    try:
        checkbox = next(
            box
            for box in dialog.findChildren(QtWidgets.QCheckBox)
            if box.text() == "允许缺少 S1 列"
        )
        confirm = next(
            button
            for button in dialog.findChildren(QtWidgets.QPushButton)
            if button.text() == "确认"
        )
        cancel = next(
            button
            for button in dialog.findChildren(QtWidgets.QPushButton)
            if button.text() == "取消"
        )
        header = dialog.findChild(QtWidgets.QFrame, "dialog_header")
        body = dialog.findChild(QtWidgets.QFrame, "dialog_body")
        controls.update(
            dialog=dialog,
            checkbox=checkbox,
            confirm=confirm,
            cancel=cancel,
            header=header,
            body=body,
            expanded_height=dialog.height(),
        )
        initial_minimum = dialog.minimumSize()
        initial_maximum = dialog.maximumSize()
        send_native_drag()
        if dialog.minimumSize() != initial_minimum:
            errors.append(
                f"native drag changed minimum size "
                f"{initial_minimum!r} -> {dialog.minimumSize()!r}"
            )
        if dialog.maximumSize() != initial_maximum:
            errors.append(
                f"native drag changed maximum size "
                f"{initial_maximum!r} -> {dialog.maximumSize()!r}"
            )
        controls["header_bottom"] = header_divider_y()
        post_native_click(checkbox, 101)
        QtCore.QTimer.singleShot(300, inspect_final_state)
    except Exception as exc:
        errors.append(repr(exc))
        if dialog is not None and dialog.isVisible():
            dialog.reject()

def inspect_final_state():
    dialog = controls["dialog"]
    checkbox = controls["checkbox"]
    confirm = controls["confirm"]
    cancel = controls["cancel"]
    body = controls["body"]
    try:
        if not checkbox.isChecked():
            errors.append("burst did not end in checked state")
        if dialog.height() >= controls["expanded_height"]:
            errors.append(
                f"checked burst retained expanded height "
                f"{dialog.height()} >= {controls['expanded_height']}"
            )
        if abs(dialog.height() - dialog.sizeHint().height()) > 2:
            errors.append(
                f"final height={dialog.height()} "
                f"hint={dialog.sizeHint().height()}"
            )
        if not (
            dialog.minimumHeight()
            <= dialog.height()
            <= dialog.maximumHeight()
        ):
            errors.append(
                f"final bounds={dialog.minimumHeight()}.."
                f"{dialog.maximumHeight()} height={dialog.height()}"
            )
        if header_divider_y() != controls["header_bottom"]:
            errors.append("checked state moved the header divider")
        blank_below_buttons = (
            body.mapTo(dialog, body.rect().bottomLeft()).y()
            - confirm.mapTo(dialog, confirm.rect().bottomLeft()).y()
        )
        if blank_below_buttons > 20:
            errors.append(
                f"checked state left {blank_below_buttons}px below buttons"
            )
        for button in (confirm, cancel):
            hit = app.widgetAt(button.mapToGlobal(button.rect().center()))
            if hit is not button:
                errors.append(f"final {button.text()} hit={hit!r}")
        controls["collapsed_height"] = dialog.height()
        post_native_click(checkbox)
        QtCore.QTimer.singleShot(100, inspect_expanded_state)
    except Exception as exc:
        errors.append(repr(exc))
        if dialog is not None and dialog.isVisible():
            dialog.reject()

def inspect_expanded_state():
    dialog = controls["dialog"]
    checkbox = controls["checkbox"]
    confirm = controls["confirm"]
    cancel = controls["cancel"]
    try:
        if checkbox.isChecked():
            errors.append("final expansion did not end in unchecked state")
        if dialog.height() <= controls["collapsed_height"]:
            errors.append(
                f"unchecked state retained collapsed height "
                f"{dialog.height()} <= {controls['collapsed_height']}"
            )
        if abs(dialog.height() - dialog.sizeHint().height()) > 2:
            errors.append(
                f"expanded height={dialog.height()} "
                f"hint={dialog.sizeHint().height()}"
            )
        if header_divider_y() != controls["header_bottom"]:
            errors.append("expanded state moved the header divider")
        for button in (confirm, cancel):
            hit = app.widgetAt(button.mapToGlobal(button.rect().center()))
            if hit is not button:
                errors.append(f"expanded {button.text()} hit={hit!r}")
        post_native_click(cancel)
        QtCore.QTimer.singleShot(100, inspect_cancel_state)
    except Exception as exc:
        errors.append(repr(exc))
    finally:
        if errors and dialog is not None and dialog.isVisible():
            dialog.reject()

def inspect_cancel_state():
    dialog = controls["dialog"]
    if dialog.isVisible():
        errors.append("native cancel click did not close the dialog")
        dialog.reject()

QtCore.QTimer.singleShot(0, exercise_dialog)
QtCore.QTimer.singleShot(5000, app.quit)
result = QtPreflightDialogPort(QtWidgets, QtCore).confirm(
    None,
    default_s1_limit=2_000_000,
    steady_emission_y="S1c",
)
if result is not None:
    errors.append(f"unexpected result: {result!r}")
geometry_warnings = [
    message for message in messages
    if "QWindowsWindow::setGeometry" in message
]
if geometry_warnings:
    errors.extend(geometry_warnings)
if errors:
    raise SystemExit("\n".join(errors))
print("NATIVE_PREFLIGHT_TOGGLE_OK")
'''
        env = os.environ.copy()
        env["PYTHONPATH"] = str(SRC)
        env["QT_QPA_PLATFORM"] = "windows"
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )

        self.assertEqual(
            0,
            completed.returncode,
            completed.stdout + completed.stderr,
        )
        self.assertEqual(
            "NATIVE_PREFLIGHT_TOGGLE_OK",
            completed.stdout.strip(),
        )

    def test_preflight_missing_s1_decision_precedes_and_hides_only_s1_limit(self):
        from PySide6 import QtCore, QtWidgets

        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        observed = {}

        def inspect_missing_s1_mode():
            dialog = app.activeModalWidget()
            checkbox = next(
                box
                for box in dialog.findChildren(QtWidgets.QCheckBox)
                if box.text() == "允许缺少 S1 列"
            )
            missing_s1_help = next(
                label
                for label in dialog.findChildren(QtWidgets.QLabel)
                if label.text().startswith("仅在 S1 缺失时")
            )
            s1_label = next(
                label
                for label in dialog.findChildren(QtWidgets.QLabel)
                if label.text() == "S1 强度上限"
            )
            s1_help = next(
                label
                for label in dialog.findChildren(QtWidgets.QLabel)
                if label.text().startswith("适用于稳态谱和延迟谱")
            )
            steady_label = next(
                label
                for label in dialog.findChildren(QtWidgets.QLabel)
                if label.text() == "发射谱 Y 列"
            )
            s1_edit = dialog.findChildren(QtWidgets.QLineEdit)[0]
            steady_combo = dialog.findChildren(QtWidgets.QComboBox)[0]
            s1_section = dialog.findChild(QtWidgets.QWidget, "preflight_s1_section")
            observed["grouped_s1_section"] = s1_section is not None
            observed["atomic_transition"] = (
                dialog.findChild(
                    QtCore.QPropertyAnimation,
                    "preflight_s1_transition",
                )
                is None
            )

            observed["checkbox_first"] = (
                checkbox.mapTo(dialog, QtCore.QPoint(0, 0)).y()
                < s1_label.mapTo(dialog, QtCore.QPoint(0, 0)).y()
            )
            missing_help_bottom = (
                missing_s1_help.mapTo(dialog, QtCore.QPoint(0, 0)).y()
                + missing_s1_help.height()
            )
            s1_top = s1_section.mapTo(dialog, QtCore.QPoint(0, 0)).y()
            s1_bottom = s1_top + s1_section.height()
            steady_section = steady_label.parentWidget()
            steady_top = steady_section.mapTo(dialog, QtCore.QPoint(0, 0)).y()
            observed["expanded_gaps"] = (
                s1_top - missing_help_bottom,
                steady_top - s1_bottom,
            )
            checkbox.setChecked(True)
            s1_edit.clear()
            app.processEvents()
            observed["s1_hidden"] = not any(
                widget.isVisible() for widget in (s1_label, s1_edit, s1_help)
            )
            observed["steady_visible"] = steady_label.isVisible() and steady_combo.isVisible()
            collapsed_steady_top = steady_section.mapTo(dialog, QtCore.QPoint(0, 0)).y()
            observed["collapsed_gap"] = collapsed_steady_top - missing_help_bottom
            confirm = next(
                button
                for button in dialog.findChildren(QtWidgets.QPushButton)
                if button.text() == "确认"
            )
            observed["confirm_enabled"] = confirm.isEnabled()
            if confirm.isEnabled():
                confirm.click()
            else:
                dialog.reject()

        QtCore.QTimer.singleShot(0, inspect_missing_s1_mode)
        result = app_module.QtPreflightDialogPort(QtWidgets, QtCore).confirm(
            None,
            default_s1_limit=2_000_000,
            steady_emission_y="S1c",
            allow_missing_s1=False,
        )

        self.assertTrue(observed["checkbox_first"])
        self.assertTrue(observed["grouped_s1_section"])
        self.assertTrue(observed["atomic_transition"])
        self.assertTrue(observed["s1_hidden"])
        self.assertTrue(observed["steady_visible"])
        self.assertTrue(
            all(6 <= gap <= 16 for gap in observed["expanded_gaps"]),
            observed,
        )
        self.assertGreaterEqual(observed["collapsed_gap"], 6)
        self.assertLessEqual(observed["collapsed_gap"], 16, observed)
        self.assertTrue(observed["confirm_enabled"])
        self.assertEqual(
            {
                "s1_limit": 2_000_000,
                "steady_emission_y": "S1c",
                "allow_missing_s1": True,
            },
            result,
        )

    def test_preflight_dialog_is_destroyed_after_each_cancel(self):
        from PySide6 import QtCore, QtWidgets

        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        port = app_module.QtPreflightDialogPort(QtWidgets, QtCore)

        for _ in range(2):
            QtCore.QTimer.singleShot(0, lambda: app.activeModalWidget().reject())
            self.assertIsNone(
                port.confirm(None, default_s1_limit=1_000_000, steady_emission_y="S1c")
            )
            QtCore.QCoreApplication.sendPostedEvents(
                None,
                QtCore.QEvent.Type.DeferredDelete,
            )
            app.processEvents()
            dialogs = [
                widget
                for widget in app.topLevelWidgets()
                if isinstance(widget, QtWidgets.QDialog) and widget.windowTitle() == "预检设置"
            ]
            self.assertEqual([], dialogs)

        with mock.patch.object(
            app_module,
            "_make_windows_taskbar_window",
            side_effect=RuntimeError("INJECTED_TASKBAR_FAILURE"),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "INJECTED_TASKBAR_FAILURE",
            ):
                port.confirm(
                    None,
                    default_s1_limit=1_000_000,
                    steady_emission_y="S1c",
                )
        QtCore.QCoreApplication.sendPostedEvents(
            None,
            QtCore.QEvent.Type.DeferredDelete,
        )
        app.processEvents()
        self.assertFalse(
            any(
                isinstance(widget, QtWidgets.QDialog)
                and widget.windowTitle() == "预检设置"
                for widget in app.topLevelWidgets()
            )
        )

    def test_output_parent_and_preflight_settings_persist_immediately_without_rollback(self):
        store = FakeSettingsStore()
        orchestrator = BookOnlyOrchestrator(store)

        orchestrator.select_output_parent("C:/Organized")
        orchestrator.confirm_preflight_settings(s1_limit=42, steady_emission_y="S1c/R1c")
        orchestrator.cancel_after_preferences()
        orchestrator.fail_after_preferences("simulated failure")

        self.assertEqual(["C:/Organized"], store.output_parent_writes)
        self.assertEqual([(42, "S1c/R1c", False)], store.preflight_writes)

    def test_preflight_confirmation_remains_compatible_with_two_argument_settings_writer(self):
        store = LegacyTwoArgumentSettingsStore()
        orchestrator = BookOnlyOrchestrator(store)

        orchestrator.confirm_preflight_settings(
            s1_limit=42,
            steady_emission_y="S1c/R1c",
            allow_missing_s1=False,
        )

        self.assertEqual([(42, "S1c/R1c")], store.preflight_writes)
        self.assertEqual(False, orchestrator.task_cache["settings_snapshot"]["allowMissingS1"])

    def test_completion_new_task_clears_task_cache_and_keeps_settings(self):
        store = FakeSettingsStore()
        orchestrator = BookOnlyOrchestrator(store)
        orchestrator.task_cache["review"] = object()
        orchestrator.select_output_parent("C:/Organized")

        orchestrator.start_new_task()

        self.assertEqual({}, orchestrator.task_cache)
        self.assertEqual(["C:/Organized"], store.output_parent_writes)

    def test_program_owned_dialog_chrome_uses_antialiased_alpha_surface(self):
        from PySide6 import QtCore, QtGui, QtWidgets

        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        dialog = QtWidgets.QDialog()
        dialog.resize(420, 220)

        apply_styled_dialog_chrome(dialog, QtCore)

        self.assertTrue(
            dialog.testAttribute(
                QtCore.Qt.WidgetAttribute.WA_TranslucentBackground
            )
        )
        mask = dialog.mask()
        self.assertTrue(mask.isEmpty())
        app.processEvents()

    @unittest.skipUnless(os.name == "nt", "native Windows window style only")
    def test_program_windows_quarantine_qt_titlebar_helpers_on_native_windows(self):
        self.assertIs(
            dialog_port_module._windows_user32(),
            dialog_port_module._windows_user32(),
        )
        self.assertFalse(
            dialog_port_module._quarantine_qt_titlebar_helper(0x1_0000_0000)
        )

        def fail_owner_lookup(error_code):
            def failure(*_args):
                import ctypes

                ctypes.set_last_error(error_code)
                return 0

            return failure

        owner_lookup_failure_user32 = mock.Mock()
        owner_lookup_failure_user32.GetWindowThreadProcessId.side_effect = (
            fail_owner_lookup(5)
        )
        with mock.patch.object(
            dialog_port_module,
            "_windows_user32",
            return_value=owner_lookup_failure_user32,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "GetWindowThreadProcessId failed: 5",
            ):
                dialog_port_module._quarantine_qt_titlebar_helper(123)

        vanished_helper_user32 = mock.Mock()
        vanished_helper_user32.GetWindowThreadProcessId.side_effect = (
            fail_owner_lookup(1400)
        )
        with mock.patch.object(
            dialog_port_module,
            "_windows_user32",
            return_value=vanished_helper_user32,
        ):
            self.assertFalse(
                dialog_port_module._quarantine_qt_titlebar_helper(123)
            )
        vanished_helper_user32.GetClassNameW.assert_not_called()

        def set_current_owner(_hwnd, process_id_pointer):
            import ctypes
            from ctypes import wintypes

            ctypes.cast(
                process_id_pointer,
                ctypes.POINTER(wintypes.DWORD),
            ).contents.value = os.getpid()
            return 1

        def fail_class_lookup(error_code):
            def failure(*_args):
                import ctypes

                ctypes.set_last_error(error_code)
                return 0

            return failure

        class_lookup_failure_user32 = mock.Mock()
        class_lookup_failure_user32.GetWindowThreadProcessId.side_effect = (
            set_current_owner
        )
        class_lookup_failure_user32.GetClassNameW.side_effect = (
            fail_class_lookup(5)
        )
        with mock.patch.object(
            dialog_port_module,
            "_windows_user32",
            return_value=class_lookup_failure_user32,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "GetClassNameW failed: 5",
            ):
                dialog_port_module._quarantine_qt_titlebar_helper(123)

        vanished_class_user32 = mock.Mock()
        vanished_class_user32.GetWindowThreadProcessId.side_effect = (
            set_current_owner
        )
        vanished_class_user32.GetClassNameW.side_effect = fail_class_lookup(
            1400
        )
        with mock.patch.object(
            dialog_port_module,
            "_windows_user32",
            return_value=vanished_class_user32,
        ):
            self.assertFalse(
                dialog_port_module._quarantine_qt_titlebar_helper(123)
            )
        vanished_class_user32.ShowWindow.assert_not_called()

        failed_set_parent_user32 = mock.Mock()

        def set_current_process_id(_hwnd, process_id_pointer):
            import ctypes
            from ctypes import wintypes

            ctypes.cast(
                process_id_pointer,
                ctypes.POINTER(wintypes.DWORD),
            ).contents.value = os.getpid()
            return 1

        def set_titlebar_class(_hwnd, class_name, _length):
            class_name.value = "_q_titlebar"
            return len(class_name.value)

        def fail_set_parent(*_args):
            import ctypes

            ctypes.set_last_error(5)
            return 0

        failed_set_parent_user32.GetWindowThreadProcessId.side_effect = (
            set_current_process_id
        )
        failed_set_parent_user32.GetClassNameW.side_effect = (
            set_titlebar_class
        )
        failed_set_parent_user32.SetParent.side_effect = fail_set_parent
        with mock.patch.object(
            dialog_port_module,
            "_windows_user32",
            return_value=failed_set_parent_user32,
        ):
            with self.assertRaisesRegex(RuntimeError, "SetParent failed: 5"):
                dialog_port_module._quarantine_qt_titlebar_helper(123)

        zero_parent_user32 = mock.Mock()
        zero_parent_user32.GetWindowThreadProcessId.side_effect = (
            set_current_process_id
        )
        zero_parent_user32.GetClassNameW.side_effect = set_titlebar_class
        zero_parent_user32.SetParent.return_value = 0
        with mock.patch.object(
            dialog_port_module,
            "_windows_user32",
            return_value=zero_parent_user32,
        ):
            self.assertTrue(
                dialog_port_module._quarantine_qt_titlebar_helper(123)
            )

        taskbar_api_failure_user32 = mock.Mock()
        taskbar_api_failure_user32.GetWindowLongPtrW.return_value = 0x80

        def fail_set_window_long_ptr(*_args):
            import ctypes

            ctypes.set_last_error(5)
            return 0

        taskbar_api_failure_user32.SetWindowLongPtrW.side_effect = (
            fail_set_window_long_ptr
        )
        fake_dialog = mock.Mock()
        fake_dialog.winId.return_value = 123
        native_qt_gui = mock.Mock()
        native_qt_gui.QGuiApplication.platformName.return_value = "windows"
        with (
            mock.patch.object(
                dialog_port_module,
                "_load_qt_gui",
                return_value=native_qt_gui,
            ),
            mock.patch.object(
                dialog_port_module,
                "_windows_user32",
                return_value=taskbar_api_failure_user32,
            ),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "SetWindowLongPtrW failed: 5",
            ):
                dialog_port_module._make_windows_taskbar_window(fake_dialog)

        taskbar_read_failure_user32 = mock.Mock()

        def fail_get_window_long_ptr(*_args):
            import ctypes

            ctypes.set_last_error(5)
            return 0

        taskbar_read_failure_user32.GetWindowLongPtrW.side_effect = (
            fail_get_window_long_ptr
        )
        with (
            mock.patch.object(
                dialog_port_module,
                "_load_qt_gui",
                return_value=native_qt_gui,
            ),
            mock.patch.object(
                dialog_port_module,
                "_windows_user32",
                return_value=taskbar_read_failure_user32,
            ),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "GetWindowLongPtrW failed: 5",
            ):
                dialog_port_module._make_windows_taskbar_window(fake_dialog)
        taskbar_read_failure_user32.SetWindowLongPtrW.assert_not_called()

        taskbar_zero_style_user32 = mock.Mock()
        taskbar_zero_style_user32.GetWindowLongPtrW.return_value = 0
        taskbar_zero_style_user32.SetWindowLongPtrW.return_value = 0
        with (
            mock.patch.object(
                dialog_port_module,
                "_load_qt_gui",
                return_value=native_qt_gui,
            ),
            mock.patch.object(
                dialog_port_module,
                "_windows_user32",
                return_value=taskbar_zero_style_user32,
            ),
        ):
            dialog_port_module._make_windows_taskbar_window(fake_dialog)
        taskbar_zero_style_user32.SetWindowLongPtrW.assert_called_once_with(
            123,
            -20,
            0x40000,
        )

        enumerating_user32 = mock.Mock()
        enumerating_user32.EnumWindows.side_effect = (
            lambda callback, _lparam: callback(1, 0)
        )
        with (
            mock.patch.object(
                dialog_port_module,
                "_windows_user32",
                return_value=enumerating_user32,
            ),
            mock.patch.object(
                dialog_port_module,
                "_quarantine_qt_titlebar_helper",
                side_effect=RuntimeError("INJECTED_ENUM_CALLBACK_FAILURE"),
            ),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "titlebar enumeration callback failed.*INJECTED_ENUM_CALLBACK_FAILURE",
            ):
                dialog_port_module._quarantine_qt_titlebar_helpers()

        enum_api_failure_user32 = mock.Mock()

        def fail_enum_windows(*_args):
            import ctypes

            ctypes.set_last_error(5)
            return 0

        enum_api_failure_user32.EnumWindows.side_effect = fail_enum_windows
        with mock.patch.object(
            dialog_port_module,
            "_windows_user32",
            return_value=enum_api_failure_user32,
        ):
            with self.assertRaisesRegex(RuntimeError, "EnumWindows failed: 5"):
                dialog_port_module._quarantine_qt_titlebar_helpers()

        class FakeApplication:
            pass

        hooked_user32 = mock.Mock()
        captured_callback = {}

        def capture_hook(*args):
            captured_callback["value"] = args[3]
            return 1

        hooked_user32.SetWinEventHook.side_effect = capture_hook
        application = FakeApplication()
        with mock.patch.object(
            dialog_port_module,
            "_windows_user32",
            return_value=hooked_user32,
        ):
            dialog_port_module._install_native_titlebar_quarantine_hook(application)
        with mock.patch.object(
            dialog_port_module,
            "_quarantine_qt_titlebar_helper",
            side_effect=RuntimeError("INJECTED_HOOK_CALLBACK_FAILURE"),
        ):
            captured_callback["value"](0, 0x8000, 1, 0, 0, 0, 0)
        self.assertEqual(
            ["RuntimeError('INJECTED_HOOK_CALLBACK_FAILURE')"],
            application._native_titlebar_quarantine_errors,
        )

        hook_api_failure_user32 = mock.Mock()

        def fail_set_win_event_hook(*_args):
            import ctypes

            ctypes.set_last_error(5)
            return 0

        hook_api_failure_user32.SetWinEventHook.side_effect = (
            fail_set_win_event_hook
        )
        failed_application = FakeApplication()
        with mock.patch.object(
            dialog_port_module,
            "_windows_user32",
            return_value=hook_api_failure_user32,
        ):
            dialog_port_module._install_native_titlebar_quarantine_hook(
                failed_application
            )
        self.assertFalse(
            hasattr(
                failed_application,
                "_native_titlebar_quarantine_hook",
            )
        )
        self.assertEqual(
            ["RuntimeError('SetWinEventHook failed: 5')"],
            failed_application._native_titlebar_quarantine_errors,
        )
        hook_api_failure_user32.SetWinEventHook.side_effect = None
        hook_api_failure_user32.SetWinEventHook.return_value = 123
        with mock.patch.object(
            dialog_port_module,
            "_windows_user32",
            return_value=hook_api_failure_user32,
        ):
            dialog_port_module._install_native_titlebar_quarantine_hook(
                failed_application
            )
        self.assertEqual(
            ["RuntimeError('SetWinEventHook failed: 5')"],
            failed_application._native_titlebar_quarantine_errors,
        )

        script = r'''
import ctypes
import os
from ctypes import wintypes

from PySide6 import QtCore, QtTest, QtWidgets

from spectrum_organizer.ui.dialog_port import (
    AttributionDialogRequest,
    _windows_user32,
    show_attribution_dialog,
)
from spectrum_organizer.ui.qt_main_window import create_production_main_window

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
user32 = _windows_user32()
process_id = os.getpid()
callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

def top_level_qt_titlebars():
    handles = []
    @callback_type
    def callback(hwnd, _lparam):
        owner_process_id = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner_process_id))
        class_name = ctypes.create_unicode_buffer(128)
        user32.GetClassNameW(hwnd, class_name, len(class_name))
        if owner_process_id.value == process_id and class_name.value == "_q_titlebar":
            handles.append(int(hwnd))
        return True
    user32.EnumWindows(callback, 0)
    return handles

def has_antialiased_corner(widget):
    image = widget.grab().toImage()
    alphas = [
        image.pixelColor(x, y).alpha()
        for y in range(16)
        for x in range(16)
    ]
    return min(alphas) == 0 and max(alphas) == 255 and any(
        0 < alpha < 255 for alpha in alphas
    )

window, _widgets = create_production_main_window(
    dpi_percent=100,
    size_name="desktop",
    stage="attribution",
)
window.show()
app.processEvents()
if not has_antialiased_corner(window):
    raise SystemExit("production main window corner lacks alpha antialiasing")
if top_level_qt_titlebars():
    raise SystemExit("production main window left a top-level _q_titlebar helper")

helper = user32.FindWindowExW(
    wintypes.HWND(-3),
    0,
    "_q_titlebar",
    None,
)
if not helper:
    raise SystemExit("quarantined _q_titlebar helper was not found")
user32.SetParent(helper, 0)
user32.MoveWindow(helper, -3000, -3000, 120, 39, False)
user32.ShowWindow(helper, 5)
QtTest.QTest.qWait(60)
app.processEvents()
escaped_helpers = top_level_qt_titlebars()
if escaped_helpers:
    user32.ShowWindow(helper, 0)
    user32.SetParent(helper, wintypes.HWND(-3))
    raise SystemExit(
        f"native helper escaped quarantine: {escaped_helpers}"
    )

observed_during_dialog = []
dialog_corner_antialiased = []
def inspect_dialog():
    dialog = next(
        candidate
        for candidate in app.topLevelWidgets()
        if isinstance(candidate, QtWidgets.QDialog) and candidate.isVisible()
    )
    dialog_corner_antialiased.append(has_antialiased_corner(dialog))
    combo = dialog.findChild(QtWidgets.QComboBox)
    combo.showPopup()
    app.processEvents()
    observed_during_dialog.extend(top_level_qt_titlebars())
    for candidate in app.topLevelWidgets():
        if isinstance(candidate, QtWidgets.QDialog) and candidate.isVisible():
            candidate.reject()

QtCore.QTimer.singleShot(0, inspect_dialog)
show_attribution_dialog(
    AttributionDialogRequest(
        target_label="Folder",
        source_filename="source.opj",
        book_display_names=("Book",),
    ),
    parent=window,
)
if observed_during_dialog:
    raise SystemExit("attribution dialog left a top-level _q_titlebar helper")
if dialog_corner_antialiased != [True]:
    raise SystemExit("attribution dialog corner lacks alpha antialiasing")

window.close()
app.processEvents()
'''
        env = os.environ.copy()
        env["PYTHONPATH"] = str(SRC)
        env["QT_QPA_PLATFORM"] = "windows"
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )

        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)

    @unittest.skipUnless(os.name == "nt", "native Windows window lifecycle only")
    def test_production_dialog_transitions_never_show_ownerless_blank_window(self):
        import ctypes
        from ctypes import wintypes

        from validation import round10_private_hwnd_probe

        required_task7_dialog_stages = {
            "task7_special_duplicate",
            "task7_special_overlap",
            "task7_special_group_books",
        }
        with self.subTest("all Task 7 dialog-producing branches are probed"):
            self.assertTrue(
                required_task7_dialog_stages.issubset(
                    round10_private_hwnd_probe.EXPECTED_PROBE_STAGES
                )
            )
            self.assertTrue(
                required_task7_dialog_stages.issubset(
                    round10_private_hwnd_probe.EXPECTED_PROBE_STAGE_SEQUENCE
                )
            )
            self.assertTrue(
                required_task7_dialog_stages.issubset(
                    round10_private_hwnd_probe.EXPECTED_DIALOG_TITLES_BY_STAGE
                )
            )

        qpa_environment = mock.patch.dict(
            os.environ,
            {"QT_QPA_PLATFORM": "windows"},
        )
        qpa_environment.start()
        self.addCleanup(qpa_environment.stop)

        user32, kernel32, dwmapi = round10_private_hwnd_probe._windows_libraries()
        round10_private_hwnd_probe._configure_windows_api(
            user32,
            kernel32,
            dwmapi,
        )
        pointer_sized_apis = (
            user32.UnhookWindowsHookEx,
            user32.UnhookWinEvent,
            user32.PostMessageW,
            user32.SendMessageW,
            kernel32.WaitForSingleObject,
            kernel32.TerminateProcess,
            kernel32.GetExitCodeProcess,
            kernel32.CloseHandle,
        )
        for api in pointer_sized_apis:
            self.assertEqual(wintypes.HANDLE, api.argtypes[0])

        def snapshot_apis():
            snapshot_user32 = mock.Mock()
            snapshot_dwmapi = mock.Mock()

            def class_name(_hwnd, buffer, _size):
                buffer.value = "Qt6111QWindowIcon"
                return len(buffer.value)

            def window_text(_hwnd, buffer, _size):
                buffer.value = "确认样品归属"
                return len(buffer.value)

            def window_rect(_hwnd, rectangle_pointer):
                rectangle = ctypes.cast(
                    rectangle_pointer,
                    ctypes.POINTER(wintypes.RECT),
                ).contents
                rectangle.left = 1
                rectangle.top = 2
                rectangle.right = 101
                rectangle.bottom = 102
                return 1

            def window_attribute(
                _hwnd,
                _attribute,
                value_pointer,
                _size,
            ):
                ctypes.cast(
                    value_pointer,
                    ctypes.POINTER(wintypes.DWORD),
                ).contents.value = 0
                return 0

            snapshot_user32.GetClassNameW.side_effect = class_name
            snapshot_user32.GetWindowTextW.side_effect = window_text
            snapshot_user32.GetWindowRect.side_effect = window_rect
            snapshot_user32.GetWindowLongPtrW.return_value = 0x10000000
            snapshot_user32.GetWindow.return_value = 0
            snapshot_user32.GetParent.return_value = 0
            snapshot_user32.IsWindowVisible.return_value = 1
            snapshot_dwmapi.DwmGetWindowAttribute.side_effect = (
                window_attribute
            )
            return snapshot_user32, snapshot_dwmapi

        def fail_with_last_error(error_code):
            def failure(*_args):
                ctypes.set_last_error(error_code)
                return 0

            return failure

        for api_name in (
            "GetClassNameW",
            "GetWindowTextW",
            "GetWindowRect",
            "GetWindowLongPtrW",
            "GetWindow",
            "GetParent",
        ):
            with self.subTest(snapshot_api=api_name):
                snapshot_user32, snapshot_dwmapi = snapshot_apis()
                getattr(snapshot_user32, api_name).side_effect = (
                    fail_with_last_error(5)
                )
                with self.assertRaisesRegex(
                    RuntimeError,
                    rf"{api_name} failed: 5",
                ):
                    round10_private_hwnd_probe._window_snapshot(
                        100,
                        snapshot_user32,
                        snapshot_dwmapi,
                    )

        for style, visible in ((0, 1), (0x10000000, 0)):
            with self.subTest(
                inconsistent_style=style,
                inconsistent_visibility=visible,
            ):
                snapshot_user32, snapshot_dwmapi = snapshot_apis()
                snapshot_user32.GetWindowLongPtrW.return_value = style
                snapshot_user32.IsWindowVisible.return_value = visible
                with self.assertRaisesRegex(
                    round10_private_hwnd_probe._WindowSnapshotRace,
                    "window visibility changed during snapshot",
                ):
                    round10_private_hwnd_probe._window_snapshot(
                        100,
                        snapshot_user32,
                        snapshot_dwmapi,
                    )

        snapshot_user32, snapshot_dwmapi = snapshot_apis()
        snapshot_dwmapi.DwmGetWindowAttribute.return_value = -2147024809
        snapshot_dwmapi.DwmGetWindowAttribute.side_effect = None
        with self.assertRaisesRegex(
            RuntimeError,
            "DwmGetWindowAttribute failed",
        ):
            round10_private_hwnd_probe._window_snapshot(
                100,
                snapshot_user32,
                snapshot_dwmapi,
            )

        snapshot_user32, snapshot_dwmapi = snapshot_apis()
        snapshot_dwmapi.DwmGetWindowAttribute.return_value = -2147024890
        snapshot_dwmapi.DwmGetWindowAttribute.side_effect = None
        with self.assertRaises(
            round10_private_hwnd_probe._WindowSnapshotRace
        ):
            round10_private_hwnd_probe._window_snapshot(
                100,
                snapshot_user32,
                snapshot_dwmapi,
            )

        violating_report = {
            "cycle": 1,
            "exit_code": 0,
            "child_result": {
                "status": "ok",
                "completed_stages": [
                    "preflight",
                    "return_folder",
                    "apply_remaining",
                    "final_remaining",
                ],
                "qt_object_events": [
                    {"object_name": "dialog_form_label", "is_window": True}
                ],
            },
            "callback_errors": [],
            "visible_events": [
                {
                    "state": "present",
                    "stage": "preflight",
                    "class": "Qt6111QWindowIcon",
                    "visible": True,
                    "cloaked": 0,
                    "owner": 0,
                    "parent": 0,
                    "title": "",
                    "rect": [0, 0, 136, 54],
                }
            ],
            "titlebar_events": [],
        }
        with (
            mock.patch.object(
                round10_private_hwnd_probe,
                "_capture_cycle",
                return_value=violating_report,
            ),
            mock.patch.object(sys, "argv", ["probe", "--cycles", "1"]),
            mock.patch("builtins.print"),
        ):
            self.assertEqual(1, round10_private_hwnd_probe.main())
        with mock.patch.object(
            sys,
            "argv",
            ["probe", "--cycles", "0"],
        ):
            with self.assertRaises(SystemExit):
                round10_private_hwnd_probe.main()

        expected_stages = (
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
        expected_stage_sequence = (
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
        active_dialog_stages = expected_stage_sequence[1:-1]
        stage_start_times = {
            stage: index * 100
            for index, stage in enumerate(expected_stage_sequence)
        }
        dialog_title_by_stage = {
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

        def visible_window(hwnd, title, *, owner=0, exstyle=None):
            if exstyle is None:
                exstyle = 0x40008 if owner else 0x40000
            return {
                "hwnd": hwnd,
                "class": "Qt6111QWindowIcon",
                "title": title,
                "visible": True,
                "cloaked": 0,
                "owner": owner,
                "parent": owner,
                "style": 0x10000000,
                "exstyle": exstyle,
            }

        def qt_top_level_sample(
            hwnd,
            title,
            *,
            stage,
            time_us,
            main=False,
        ):
            return {
                "time_us": time_us,
                "stage": stage,
                "state": "visible",
                "kind": "widget",
                "hwnd": hwnd,
                "class": "QMainWindow" if main else "QDialog",
                "object_name": (
                    "production_main_window"
                    if main
                    else "organizer_dialog"
                ),
                "title": title,
            }

        def lifecycle_event(hwnd, title, *, stage, time_us):
            return {
                "time_us": time_us,
                "stage": stage,
                "source": "winevent",
                "event": "show",
                "hwnd": hwnd,
                "class": "Qt6111QWindowIcon",
                "title": title,
            }

        def qt_object_event(
            hwnd,
            title,
            *,
            stage,
            time_us,
            main=False,
        ):
            return {
                "time_us": time_us,
                "stage": stage,
                "event": "Show",
                "class": "QMainWindow" if main else "QDialog",
                "object_name": (
                    "production_main_window"
                    if main
                    else "organizer_dialog"
                ),
                "title": title,
                "hwnd": hwnd,
                "is_window": True,
                "visible": True,
                "parent_class": "",
                "parent_object_name": "",
                "geometry": [0, 0, 100, 100],
            }

        main_only_report = {
            "cycle": 2,
            "exit_code": 0,
            "child_result": {
                "status": "ok",
                "completed_stages": list(expected_stages),
                "qt_object_events": [
                    {
                        "object_name": "production_main_window",
                        "is_window": True,
                    }
                ],
                "lifecycle_events": [
                    {"event": "show", "hwnd": 100},
                ],
                "qt_top_level_samples": [
                    qt_top_level_sample(
                        100,
                        "ROUND10|preflight",
                        stage="preflight",
                        time_us=1,
                        main=True,
                    ),
                ],
            },
            "callback_errors": [],
            "window_lookup_failures": 0,
            "window_lookup_races": 0,
            "visible_events": [
                {
                    "time_us": index,
                    "stage": stage,
                    "state": "present",
                    **visible_window(100, f"ROUND10|{stage}"),
                }
                for index, stage in enumerate(expected_stages, start=1)
            ],
            "window_samples": [
                {
                    "stage": stage,
                    "windows": [
                        visible_window(100, f"ROUND10|{stage}")
                    ],
                }
                for stage in expected_stages
            ],
            "titlebar_events": [],
        }
        self.assertIn(
            "stage dialog was not observed: preflight",
            round10_private_hwnd_probe._report_violations(main_only_report),
        )

        unbound_helper_report = {
            **main_only_report,
            "window_samples": [
                {
                    "stage": stage,
                    "windows": [
                        visible_window(100, f"ROUND10|{stage}"),
                        {
                            **visible_window(
                                200,
                                "Not a dialog",
                                owner=777,
                            ),
                            "class": "_q_fake_owned_helper",
                        },
                    ],
                }
                for stage in expected_stages
            ],
        }
        self.assertIn(
            "stage dialog was not observed: preflight",
            round10_private_hwnd_probe._report_violations(
                unbound_helper_report
            ),
        )

        relabelled_stage_report = {
            **main_only_report,
            "visible_events": [
                {
                    "time_us": 1,
                    "stage": stage,
                    "state": "present",
                    **visible_window(100, f"ROUND10|{stage}"),
                }
                for stage in expected_stages
            ]
            + [
                {
                    "time_us": 2,
                    "stage": "return_folder",
                    "state": "present",
                    **visible_window(
                        200,
                        "确认样品归属",
                        owner=100,
                    ),
                }
            ],
            "window_samples": [
                {
                    "time_us": 2,
                    "stage": stage,
                    "windows": [
                        visible_window(100, f"ROUND10|{stage}"),
                        visible_window(
                            200,
                            "确认样品归属",
                            owner=100,
                        ),
                    ],
                }
                for stage in expected_stages
            ],
        }
        relabelled_violations = (
            round10_private_hwnd_probe._report_violations(
                relabelled_stage_report
            )
        )
        for expected in (
            "preflight",
            "apply_remaining",
            "final_remaining",
        ):
            self.assertIn(
                f"stage dialog was not observed: {expected}",
                relabelled_violations,
            )

        changing_main_report = {
            **main_only_report,
            "visible_events": [
                event
                for index, stage in enumerate(expected_stages)
                for event in (
                    {
                        "time_us": index,
                        "stage": stage,
                        "state": "present",
                        **visible_window(
                            100 + index,
                            f"ROUND10|{stage}",
                        ),
                    },
                    {
                        "time_us": index,
                        "stage": stage,
                        "state": "present",
                        **visible_window(
                            200 + index,
                            (
                                "预检设置"
                                if stage == "preflight"
                                else "确认样品归属"
                            ),
                            owner=100 + index,
                        ),
                    },
                )
            ],
            "window_samples": [
                {
                    "time_us": index,
                    "stage": stage,
                    "windows": [
                        visible_window(
                            100 + index,
                            f"ROUND10|{stage}",
                        ),
                        visible_window(
                            200 + index,
                            (
                                "预检设置"
                                if stage == "preflight"
                                else "确认样品归属"
                            ),
                            owner=100 + index,
                        ),
                    ],
                }
                for index, stage in enumerate(expected_stages)
            ],
        }
        self.assertIn(
            "main window changed across stages",
            round10_private_hwnd_probe._report_violations(
                changing_main_report
            ),
        )

        lookup_failure_report = {
            **main_only_report,
            "window_lookup_failures": 999,
        }
        self.assertIn(
            "window ownership lookup failures were recorded",
            round10_private_hwnd_probe._report_violations(
                lookup_failure_report
            ),
        )

        coherent_report = {
            **main_only_report,
            "child_result": {
                **main_only_report["child_result"],
                "lifecycle_events": [
                    event
                    for index, stage in enumerate(
                        active_dialog_stages,
                        start=1,
                    )
                    for event in (
                        lifecycle_event(
                            100,
                            f"ROUND10|{stage}",
                            stage=stage,
                            time_us=stage_start_times[stage] + 10,
                        ),
                        lifecycle_event(
                            200 + index,
                            dialog_title_by_stage[stage],
                            stage=stage,
                            time_us=stage_start_times[stage] + 10,
                        ),
                    )
                ],
                "qt_object_events": [
                    event
                    for index, stage in enumerate(
                        active_dialog_stages,
                        start=1,
                    )
                    for event in (
                        qt_object_event(
                            100,
                            f"ROUND10|{stage}",
                            stage=stage,
                            time_us=stage_start_times[stage] + 10,
                            main=True,
                        ),
                        qt_object_event(
                            200 + index,
                            dialog_title_by_stage[stage],
                            stage=stage,
                            time_us=stage_start_times[stage] + 10,
                        ),
                    )
                ],
                "qt_top_level_samples": [
                    qt_top_level_sample(
                        100,
                        f"ROUND10|{stage}",
                        stage=stage,
                        time_us=stage_start_times[stage] + 10,
                        main=True,
                    )
                    for index, stage in enumerate(
                        active_dialog_stages,
                        start=1,
                    )
                ]
                + [
                    qt_top_level_sample(
                        200 + index,
                        dialog_title_by_stage[stage],
                        stage=stage,
                        time_us=stage_start_times[stage] + 10,
                    )
                    for index, stage in enumerate(
                        active_dialog_stages,
                        start=1,
                    )
                ],
                "stage_transitions": [
                    {
                        "time_us": stage_start_times[stage],
                        "stage": stage,
                    }
                    for stage in expected_stage_sequence
                ],
            },
            "window_lookup_failures": 0,
            "window_lookup_races": 0,
            "visible_events": [
                event
                for index, stage in enumerate(active_dialog_stages, start=1)
                for event in (
                    {
                        "time_us": stage_start_times[stage] + 10,
                        "stage": stage,
                        "state": "present",
                        **visible_window(100, f"ROUND10|{stage}"),
                    },
                    {
                        "time_us": stage_start_times[stage] + 10,
                        "stage": stage,
                        "state": "present",
                        **visible_window(
                            200 + index,
                            dialog_title_by_stage[stage],
                            owner=100,
                        ),
                    },
                )
            ],
            "window_samples": [
                {
                    "time_us": stage_start_times[stage] + 10,
                    "stage": stage,
                    "windows": [
                        visible_window(100, f"ROUND10|{stage}"),
                        visible_window(
                            200 + index,
                            dialog_title_by_stage[stage],
                            owner=100,
                        ),
                    ],
                }
                for index, stage in enumerate(active_dialog_stages, start=1)
            ],
        }
        self.assertEqual(
            (),
            round10_private_hwnd_probe._report_violations(
                coherent_report
            ),
        )

        def append_native_dialog_observation(
            report,
            stage,
            dialog,
            time_us,
        ):
            report["visible_events"].extend(
                (
                    {
                        "time_us": time_us,
                        "stage": stage,
                        "state": "present",
                        **visible_window(100, f"ROUND10|{stage}"),
                    },
                    {
                        "time_us": time_us,
                        "stage": stage,
                        "state": "present",
                        **dialog,
                    },
                )
            )
            report["window_samples"].append(
                {
                    "time_us": time_us,
                    "stage": stage,
                    "windows": [
                        visible_window(100, f"ROUND10|{stage}"),
                        dialog,
                    ],
                }
            )

        def append_dialog_observation(report, stage, dialog, time_us):
            append_native_dialog_observation(
                report,
                stage,
                dialog,
                time_us,
            )
            report["child_result"]["qt_top_level_samples"].append(
                qt_top_level_sample(
                    dialog["hwnd"],
                    dialog["title"],
                    stage=stage,
                    time_us=time_us,
                )
            )

        delayed_birth_report = copy.deepcopy(coherent_report)
        delayed_birth_stage = "generic_manual"
        delayed_birth_dialog = next(
            window
            for sample in delayed_birth_report["window_samples"]
            if sample["stage"] == delayed_birth_stage
            for window in sample["windows"]
            if window["hwnd"] != 100
        )
        delayed_birth_time = stage_start_times[delayed_birth_stage] + 20
        later_observation_time = delayed_birth_time + 10
        for key in ("lifecycle_events", "qt_object_events"):
            for record in delayed_birth_report["child_result"][key]:
                if (
                    record.get("stage") == delayed_birth_stage
                    and record.get("hwnd") == delayed_birth_dialog["hwnd"]
                ):
                    record["time_us"] = delayed_birth_time
        append_dialog_observation(
            delayed_birth_report,
            delayed_birth_stage,
            delayed_birth_dialog,
            later_observation_time,
        )
        with self.subTest("dialog birth SHOW precedes first visibility"):
            delayed_birth_violations = (
                round10_private_hwnd_probe._report_violations(
                    delayed_birth_report
                )
            )
            self.assertIn(
                "native lifecycle evidence is unbound",
                delayed_birth_violations,
            )
            self.assertIn(
                "Qt object lifecycle evidence is unbound",
                delayed_birth_violations,
            )

        native_callback_lag_report = copy.deepcopy(coherent_report)
        native_callback_lag_time = (
            stage_start_times[delayed_birth_stage] + 20
        )
        for record in native_callback_lag_report["child_result"][
            "lifecycle_events"
        ]:
            if (
                record.get("stage") == delayed_birth_stage
                and record.get("hwnd") == delayed_birth_dialog["hwnd"]
            ):
                record["time_us"] = native_callback_lag_time
        append_dialog_observation(
            native_callback_lag_report,
            delayed_birth_stage,
            delayed_birth_dialog,
            native_callback_lag_time + 10,
        )
        with self.subTest("native callback may trail first visibility poll"):
            self.assertEqual(
                (),
                round10_private_hwnd_probe._report_violations(
                    native_callback_lag_report
                ),
            )

        object_hide_report = copy.deepcopy(coherent_report)
        object_hide_stage = "task7_special_whole"
        object_hide_dialog = next(
            window
            for sample in object_hide_report["window_samples"]
            if sample["stage"] == object_hide_stage
            for window in sample["windows"]
            if window["hwnd"] != 100
        )
        object_hide_time = stage_start_times[object_hide_stage] + 20
        object_hide_report["child_result"]["qt_object_events"].append(
            qt_object_event(
                object_hide_dialog["hwnd"],
                object_hide_dialog["title"],
                stage=object_hide_stage,
                time_us=object_hide_time,
            )
            | {"event": "Hide", "visible": False}
        )
        append_dialog_observation(
            object_hide_report,
            object_hide_stage,
            object_hide_dialog,
            object_hide_time + 10,
        )
        with self.subTest("QDialog Hide ends the visibility episode"):
            self.assertIn(
                "dialog became visible after teardown without SHOW",
                round10_private_hwnd_probe._report_violations(
                    object_hide_report
                ),
            )

        object_hide_reappearance_report = copy.deepcopy(coherent_report)
        reappearance_dialog = next(
            window
            for sample in object_hide_reappearance_report["window_samples"]
            if sample["stage"] == object_hide_stage
            for window in sample["windows"]
            if window["hwnd"] != 100
        )
        reappearance_hide_time = (
            stage_start_times[object_hide_stage] + 20
        )
        reappearance_show_time = reappearance_hide_time + 5
        reappearance_observation_time = reappearance_show_time + 5
        object_hide_reappearance_report["child_result"][
            "lifecycle_events"
        ].extend(
            (
                lifecycle_event(
                    reappearance_dialog["hwnd"],
                    reappearance_dialog["title"],
                    stage=object_hide_stage,
                    time_us=reappearance_hide_time,
                )
                | {"event": "hide"},
                lifecycle_event(
                    reappearance_dialog["hwnd"],
                    reappearance_dialog["title"],
                    stage=object_hide_stage,
                    time_us=reappearance_show_time,
                ),
            )
        )
        object_hide_reappearance_report["child_result"][
            "qt_object_events"
        ].extend(
            (
                qt_object_event(
                    reappearance_dialog["hwnd"],
                    reappearance_dialog["title"],
                    stage=object_hide_stage,
                    time_us=reappearance_hide_time,
                )
                | {"event": "Hide", "visible": False},
                qt_object_event(
                    reappearance_dialog["hwnd"],
                    reappearance_dialog["title"],
                    stage=object_hide_stage,
                    time_us=reappearance_show_time,
                ),
            )
        )
        append_dialog_observation(
            object_hide_reappearance_report,
            object_hide_stage,
            reappearance_dialog,
            reappearance_observation_time,
        )
        with self.subTest("exact Qt Hide accepts a fresh dual SHOW episode"):
            self.assertEqual(
                (),
                round10_private_hwnd_probe._report_violations(
                    object_hide_reappearance_report
                ),
            )

        trailing_native_poll_report = copy.deepcopy(coherent_report)
        trailing_poll_stage = "generic_manual"
        trailing_poll_dialog = next(
            window
            for sample in trailing_native_poll_report["window_samples"]
            if sample["stage"] == trailing_poll_stage
            for window in sample["windows"]
            if window["hwnd"] != 100
        )
        trailing_hide_time = stage_start_times[trailing_poll_stage] + 20
        trailing_poll_time = trailing_hide_time + 10
        trailing_teardown_time = trailing_poll_time + 10
        trailing_native_poll_report["child_result"][
            "qt_object_events"
        ].append(
            qt_object_event(
                trailing_poll_dialog["hwnd"],
                trailing_poll_dialog["title"],
                stage=trailing_poll_stage,
                time_us=trailing_hide_time,
            )
            | {"event": "Hide", "visible": False}
        )
        append_native_dialog_observation(
            trailing_native_poll_report,
            trailing_poll_stage,
            trailing_poll_dialog,
            trailing_poll_time,
        )
        trailing_native_poll_report["child_result"][
            "lifecycle_events"
        ].append(
            lifecycle_event(
                trailing_poll_dialog["hwnd"],
                trailing_poll_dialog["title"],
                stage=trailing_poll_stage,
                time_us=trailing_teardown_time,
            )
            | {"event": "hide"}
        )
        trailing_native_poll_report["child_result"][
            "qt_top_level_samples"
        ].append(
            {
                **qt_top_level_sample(
                    trailing_poll_dialog["hwnd"],
                    trailing_poll_dialog["title"],
                    stage=trailing_poll_stage,
                    time_us=trailing_teardown_time,
                ),
                "state": "not_visible",
            }
        )
        with self.subTest("terminal Qt Hide permits one native polling tail"):
            self.assertEqual(
                (),
                round10_private_hwnd_probe._report_violations(
                    trailing_native_poll_report
                ),
            )

        main_hide_report = copy.deepcopy(coherent_report)
        main_hide_stage = "task7_special_whole"
        main_hide_dialog = next(
            window
            for sample in main_hide_report["window_samples"]
            if sample["stage"] == main_hide_stage
            for window in sample["windows"]
            if window["hwnd"] != 100
        )
        main_hide_time = stage_start_times[main_hide_stage] + 20
        main_hide_report["child_result"]["qt_object_events"].append(
            qt_object_event(
                100,
                f"ROUND10|{main_hide_stage}",
                stage=main_hide_stage,
                time_us=main_hide_time,
                main=True,
            )
            | {"event": "Hide", "visible": False}
        )
        append_dialog_observation(
            main_hide_report,
            main_hide_stage,
            main_hide_dialog,
            main_hide_time + 10,
        )
        main_hide_report["child_result"]["qt_top_level_samples"].append(
            qt_top_level_sample(
                100,
                f"ROUND10|{main_hide_stage}",
                stage=main_hide_stage,
                time_us=main_hide_time + 10,
                main=True,
            )
        )
        with self.subTest("QMainWindow Hide ends the visibility episode"):
            self.assertIn(
                "main window became visible after teardown without SHOW",
                round10_private_hwnd_probe._report_violations(
                    main_hide_report
                ),
            )

        missing_main_birth_report = copy.deepcopy(coherent_report)
        first_main_stage = active_dialog_stages[0]
        for key in ("lifecycle_events", "qt_object_events"):
            missing_main_birth_report["child_result"][key] = [
                record
                for record in missing_main_birth_report["child_result"][key]
                if not (
                    record.get("stage") == first_main_stage
                    and record.get("hwnd") == 100
                    and str(record.get("event", "")).casefold() == "show"
                )
            ]
        with self.subTest("production main owns dual birth SHOW evidence"):
            missing_main_birth_violations = (
                round10_private_hwnd_probe._report_violations(
                    missing_main_birth_report
                )
            )
            self.assertIn(
                "native main lifecycle birth evidence is unbound",
                missing_main_birth_violations,
            )
            self.assertIn(
                "Qt main lifecycle birth evidence is unbound",
                missing_main_birth_violations,
            )

        owned_main_observation_report = copy.deepcopy(coherent_report)
        owned_main_time = stage_start_times["startup"] + 10
        owned_main_observation = visible_window(
            100,
            "ROUND10|startup",
            owner=999,
        )
        owned_main_observation_report["visible_events"].append(
            {
                "time_us": owned_main_time,
                "stage": "startup",
                "state": "present",
                **owned_main_observation,
            }
        )
        owned_main_observation_report["window_samples"].append(
            {
                "time_us": owned_main_time,
                "stage": "startup",
                "windows": [owned_main_observation],
            }
        )
        with self.subTest("every concrete production main is ownerless"):
            self.assertIn(
                "production main ownership evidence is invalid",
                round10_private_hwnd_probe._report_violations(
                    owned_main_observation_report
                ),
            )

        split_show_channels_report = copy.deepcopy(coherent_report)
        split_stage = "task7_special_whole"
        split_sample = next(
            sample
            for sample in coherent_report["window_samples"]
            if sample["stage"] == split_stage
        )
        original_dialog = next(
            window
            for window in split_sample["windows"]
            if window["hwnd"] != 100
        )
        original_dialog_hwnd = original_dialog["hwnd"]
        split_show_channels_report["child_result"]["qt_object_events"] = [
            event
            for event in split_show_channels_report["child_result"][
                "qt_object_events"
            ]
            if not (
                event.get("stage") == split_stage
                and event.get("hwnd") == original_dialog_hwnd
                and event.get("event") == "Show"
            )
        ]
        replacement_hwnd = 9_000_001
        replacement_time = stage_start_times[split_stage] + 20
        replacement_dialog = visible_window(
            replacement_hwnd,
            dialog_title_by_stage[split_stage],
            owner=100,
        )
        split_show_channels_report["window_samples"].append(
            {
                "time_us": replacement_time,
                "stage": split_stage,
                "windows": [
                    visible_window(100, f"ROUND10|{split_stage}"),
                    replacement_dialog,
                ],
            }
        )
        split_show_channels_report["visible_events"].extend(
            (
                {
                    "time_us": replacement_time,
                    "stage": split_stage,
                    "state": "present",
                    **visible_window(100, f"ROUND10|{split_stage}"),
                },
                {
                    "time_us": replacement_time,
                    "stage": split_stage,
                    "state": "present",
                    **replacement_dialog,
                },
            )
        )
        split_show_channels_report["child_result"][
            "qt_top_level_samples"
        ].append(
            qt_top_level_sample(
                replacement_hwnd,
                dialog_title_by_stage[split_stage],
                stage=split_stage,
                time_us=replacement_time,
            )
        )
        split_show_channels_report["child_result"]["qt_object_events"].append(
            qt_object_event(
                replacement_hwnd,
                dialog_title_by_stage[split_stage],
                stage=split_stage,
                time_us=replacement_time,
            )
        )
        with self.subTest("native and Qt SHOW bind one dialog HWND"):
            split_show_violations = (
                round10_private_hwnd_probe._report_violations(
                    split_show_channels_report
                )
            )
            self.assertIn(
                "native lifecycle evidence is unbound",
                split_show_violations,
            )
            self.assertIn(
                "Qt object lifecycle evidence is unbound",
                split_show_violations,
            )

        def report_with_extra_dialog(
            hwnd,
            *,
            native_show,
            qt_show,
        ):
            report = copy.deepcopy(coherent_report)
            observed_time = stage_start_times[split_stage] + 20
            dialog = visible_window(
                hwnd,
                dialog_title_by_stage[split_stage],
                owner=100,
            )
            report["window_samples"].append(
                {
                    "time_us": observed_time,
                    "stage": split_stage,
                    "windows": [
                        visible_window(100, f"ROUND10|{split_stage}"),
                        dialog,
                    ],
                }
            )
            report["visible_events"].extend(
                (
                    {
                        "time_us": observed_time,
                        "stage": split_stage,
                        "state": "present",
                        **visible_window(100, f"ROUND10|{split_stage}"),
                    },
                    {
                        "time_us": observed_time,
                        "stage": split_stage,
                        "state": "present",
                        **dialog,
                    },
                )
            )
            report["child_result"]["qt_top_level_samples"].append(
                qt_top_level_sample(
                    hwnd,
                    dialog_title_by_stage[split_stage],
                    stage=split_stage,
                    time_us=observed_time,
                )
            )
            if native_show:
                report["child_result"]["lifecycle_events"].append(
                    lifecycle_event(
                        hwnd,
                        dialog_title_by_stage[split_stage],
                        stage=split_stage,
                        time_us=observed_time,
                    )
                )
            if qt_show:
                report["child_result"]["qt_object_events"].append(
                    qt_object_event(
                        hwnd,
                        dialog_title_by_stage[split_stage],
                        stage=split_stage,
                        time_us=observed_time,
                    )
                )
            return report

        for label, native_show, qt_show, hwnd in (
            ("native-only extra dialog", True, False, 9_000_002),
            ("Qt-only extra dialog", False, True, 9_000_003),
        ):
            with self.subTest(label):
                extra_dialog_violations = (
                    round10_private_hwnd_probe._report_violations(
                        report_with_extra_dialog(
                            hwnd,
                            native_show=native_show,
                            qt_show=qt_show,
                        )
                    )
                )
                self.assertIn(
                    "native lifecycle evidence is unbound",
                    extra_dialog_violations,
                )
                self.assertIn(
                    "Qt object lifecycle evidence is unbound",
                    extra_dialog_violations,
                )

        unbound_visible_event_report = copy.deepcopy(coherent_report)
        moved_show = next(
            event
            for event in unbound_visible_event_report["child_result"][
                "lifecycle_events"
            ]
            if event.get("stage") == split_stage
            and event.get("hwnd") == original_dialog_hwnd
        )
        moved_show["time_us"] = stage_start_times[split_stage] + 99
        unbound_visible_event_report["visible_events"].append(
            {
                "time_us": moved_show["time_us"],
                "stage": split_stage,
                "state": "present",
                **original_dialog,
            }
        )
        with self.subTest("visible event binds an exact-time window sample"):
            self.assertIn(
                "visible window evidence is unbound",
                round10_private_hwnd_probe._report_violations(
                    unbound_visible_event_report
                ),
            )

        invalid_lifecycle_source_report = copy.deepcopy(coherent_report)
        invalid_source_event = next(
            event
            for event in invalid_lifecycle_source_report["child_result"][
                "lifecycle_events"
            ]
            if event.get("stage") == "task7_special_duplicate"
            and event.get("hwnd") != 100
        )
        invalid_source_event["source"] = "cbt"
        with self.subTest("native lifecycle source and event agree"):
            self.assertIn(
                "native lifecycle evidence is missing",
                round10_private_hwnd_probe._report_violations(
                    invalid_lifecycle_source_report
                ),
            )

        wrong_native_show_class_report = copy.deepcopy(coherent_report)
        wrong_show_event = next(
            event
            for event in wrong_native_show_class_report["child_result"][
                "lifecycle_events"
            ]
            if event.get("stage") == split_stage
            and event.get("hwnd") == original_dialog_hwnd
        )
        wrong_show_event["class"] = "Qt9999WrongDialogClass"
        with self.subTest("native SHOW class binds the observed dialog"):
            self.assertIn(
                "native lifecycle evidence is unbound",
                round10_private_hwnd_probe._report_violations(
                    wrong_native_show_class_report
                ),
            )

        wrong_main_class_report = copy.deepcopy(coherent_report)
        for event in wrong_main_class_report["visible_events"]:
            if event.get("hwnd") == 100:
                event["class"] = "Qt9999WrongMainClass"
        for sample in wrong_main_class_report["window_samples"]:
            for window in sample["windows"]:
                if window.get("hwnd") == 100:
                    window["class"] = "Qt9999WrongMainClass"
        with self.subTest("production main native class is recognized"):
            self.assertIn(
                "native main window class is invalid",
                round10_private_hwnd_probe._report_violations(
                    wrong_main_class_report
                ),
            )

        wrong_dialog_class_report = copy.deepcopy(coherent_report)
        for event in wrong_dialog_class_report["visible_events"]:
            if event.get("hwnd") == original_dialog_hwnd:
                event["class"] = "Qt9999WrongDialogClass"
        for sample in wrong_dialog_class_report["window_samples"]:
            for window in sample["windows"]:
                if window.get("hwnd") == original_dialog_hwnd:
                    window["class"] = "Qt9999WrongDialogClass"
        with self.subTest("manual dialog native class is recognized"):
            self.assertIn(
                "manual dialog taskbar evidence is invalid",
                round10_private_hwnd_probe._report_violations(
                    wrong_dialog_class_report
                ),
            )

        missing_topmost_report = copy.deepcopy(coherent_report)
        for event in missing_topmost_report["visible_events"]:
            if event.get("hwnd") == original_dialog_hwnd:
                event["exstyle"] &= ~0x8
        for sample in missing_topmost_report["window_samples"]:
            for window in sample["windows"]:
                if window.get("hwnd") == original_dialog_hwnd:
                    window["exstyle"] &= ~0x8
        with self.subTest("manual dialog is topmost"):
            self.assertIn(
                "manual dialog taskbar evidence is invalid",
                round10_private_hwnd_probe._report_violations(
                    missing_topmost_report
                ),
            )

        duplicate_main_report = copy.deepcopy(coherent_report)
        startup_time = stage_start_times["startup"] + 10
        duplicate_main_report["window_samples"].append(
            {
                "time_us": startup_time,
                "stage": "startup",
                "windows": [
                    visible_window(100, "ROUND10|startup"),
                    visible_window(101, "ROUND10|startup"),
                ],
            }
        )
        duplicate_main_report["visible_events"].extend(
            {
                "time_us": startup_time,
                "stage": "startup",
                "state": "present",
                **visible_window(hwnd, "ROUND10|startup"),
            }
            for hwnd in (100, 101)
        )
        duplicate_main_report["child_result"][
            "lifecycle_events"
        ].extend(
            lifecycle_event(
                hwnd,
                "ROUND10|startup",
                stage="startup",
                time_us=startup_time,
            )
            for hwnd in (100, 101)
        )
        duplicate_main_report["child_result"][
            "qt_object_events"
        ].extend(
            qt_object_event(
                hwnd,
                "ROUND10|startup",
                stage="startup",
                time_us=startup_time,
                main=True,
            )
            for hwnd in (100, 101)
        )
        duplicate_main_report["child_result"][
            "qt_top_level_samples"
        ].extend(
            qt_top_level_sample(
                hwnd,
                "ROUND10|startup",
                stage="startup",
                time_us=startup_time,
                main=True,
            )
            for hwnd in (100, 101)
        )
        self.assertIn(
            "multiple production main HWNDs were observed",
            round10_private_hwnd_probe._report_violations(
                duplicate_main_report
            ),
        )

        terminal_new_hwnd_report = copy.deepcopy(coherent_report)
        terminal_stage = "return_folder:4:confirm"
        terminal_dialog = next(
            window
            for sample in terminal_new_hwnd_report["window_samples"]
            if sample["stage"] == terminal_stage
            for window in sample["windows"]
            if window["hwnd"] != 100
        )
        terminal_old_hwnd = terminal_dialog["hwnd"]
        terminal_new_hwnd = 9_000_004
        for key in ("lifecycle_events", "qt_object_events"):
            terminal_new_hwnd_report["child_result"][key] = [
                record
                for record in terminal_new_hwnd_report["child_result"][key]
                if not (
                    record.get("stage") == terminal_stage
                    and record.get("hwnd") == terminal_old_hwnd
                )
            ]
        for record in terminal_new_hwnd_report["child_result"][
            "qt_top_level_samples"
        ]:
            if (
                record.get("stage") == terminal_stage
                and record.get("hwnd") == terminal_old_hwnd
            ):
                record["hwnd"] = terminal_new_hwnd
        for record in terminal_new_hwnd_report["visible_events"]:
            if (
                record.get("stage") == terminal_stage
                and record.get("hwnd") == terminal_old_hwnd
            ):
                record["hwnd"] = terminal_new_hwnd
        for sample in terminal_new_hwnd_report["window_samples"]:
            if sample.get("stage") != terminal_stage:
                continue
            for window in sample["windows"]:
                if window.get("hwnd") == terminal_old_hwnd:
                    window["hwnd"] = terminal_new_hwnd
        with self.subTest("new terminal dialog HWND requires dual SHOW"):
            terminal_new_hwnd_violations = (
                round10_private_hwnd_probe._report_violations(
                    terminal_new_hwnd_report
                )
            )
            self.assertIn(
                "native lifecycle evidence is unbound",
                terminal_new_hwnd_violations,
            )
            self.assertIn(
                "Qt object lifecycle evidence is unbound",
                terminal_new_hwnd_violations,
            )

        alternate_dialog_report = copy.deepcopy(coherent_report)
        alternate_stage = "return_folder:0:split"
        alternate_time = stage_start_times[alternate_stage] + 20
        alternate_hwnd = 9_000_005
        alternate_title = "确认样品归属"
        alternate_dialog_report["visible_events"].extend(
            (
                {
                    "time_us": alternate_time,
                    "stage": alternate_stage,
                    "state": "present",
                    **visible_window(
                        100,
                        f"ROUND10|{alternate_stage}",
                    ),
                },
                {
                    "time_us": alternate_time,
                    "stage": alternate_stage,
                    "state": "present",
                    **visible_window(
                        alternate_hwnd,
                        alternate_title,
                        owner=100,
                    ),
                },
            )
        )
        alternate_dialog_report["window_samples"].append(
            {
                "time_us": alternate_time,
                "stage": alternate_stage,
                "windows": [
                    visible_window(
                        100,
                        f"ROUND10|{alternate_stage}",
                    ),
                    visible_window(
                        alternate_hwnd,
                        alternate_title,
                        owner=100,
                    ),
                ],
            }
        )
        alternate_dialog_report["child_result"][
            "qt_top_level_samples"
        ].append(
            qt_top_level_sample(
                alternate_hwnd,
                alternate_title,
                stage=alternate_stage,
                time_us=alternate_time,
            )
        )
        with self.subTest("new allowed-title dialog requires dual SHOW"):
            alternate_dialog_violations = (
                round10_private_hwnd_probe._report_violations(
                    alternate_dialog_report
                )
            )
            self.assertIn(
                "native lifecycle evidence is unbound",
                alternate_dialog_violations,
            )
            self.assertIn(
                "Qt object lifecycle evidence is unbound",
                alternate_dialog_violations,
            )

        qt_teardown_reappearance_report = copy.deepcopy(coherent_report)
        reappearance_stage = "task7_special_whole"
        reappearance_time = stage_start_times[reappearance_stage] + 30
        reappearance_dialog = next(
            window
            for sample in qt_teardown_reappearance_report["window_samples"]
            if sample["stage"] == reappearance_stage
            for window in sample["windows"]
            if window["hwnd"] != 100
        )
        qt_teardown_reappearance_report["child_result"][
            "qt_top_level_samples"
        ].extend(
            (
                {
                    **qt_top_level_sample(
                        reappearance_dialog["hwnd"],
                        reappearance_dialog["title"],
                        stage=reappearance_stage,
                        time_us=reappearance_time - 10,
                    ),
                    "state": "not_visible",
                },
                qt_top_level_sample(
                    reappearance_dialog["hwnd"],
                    reappearance_dialog["title"],
                    stage=reappearance_stage,
                    time_us=reappearance_time,
                ),
            )
        )
        qt_teardown_reappearance_report["visible_events"].extend(
            (
                {
                    "time_us": reappearance_time,
                    "stage": reappearance_stage,
                    "state": "present",
                    **visible_window(
                        100,
                        f"ROUND10|{reappearance_stage}",
                    ),
                },
                {
                    "time_us": reappearance_time,
                    "stage": reappearance_stage,
                    "state": "present",
                    **reappearance_dialog,
                },
            )
        )
        qt_teardown_reappearance_report["window_samples"].append(
            {
                "time_us": reappearance_time,
                "stage": reappearance_stage,
                "windows": [
                    visible_window(
                        100,
                        f"ROUND10|{reappearance_stage}",
                    ),
                    reappearance_dialog,
                ],
            }
        )
        with self.subTest("Qt teardown ends the dialog visibility episode"):
            self.assertIn(
                "dialog became visible after teardown without SHOW",
                round10_private_hwnd_probe._report_violations(
                    qt_teardown_reappearance_report
                ),
            )

        main_reappearance_report = copy.deepcopy(coherent_report)
        main_reappearance_stage = "task7_special_whole"
        main_reappearance_time = (
            stage_start_times[main_reappearance_stage] + 30
        )
        main_reappearance_report["child_result"][
            "lifecycle_events"
        ].append(
            lifecycle_event(
                100,
                f"ROUND10|{main_reappearance_stage}",
                stage=main_reappearance_stage,
                time_us=main_reappearance_time - 10,
            )
            | {"event": "hide"}
        )
        main_reappearance_report["child_result"][
            "qt_top_level_samples"
        ].append(
            qt_top_level_sample(
                100,
                f"ROUND10|{main_reappearance_stage}",
                stage=main_reappearance_stage,
                time_us=main_reappearance_time,
                main=True,
            )
        )
        main_reappearance_report["visible_events"].append(
            {
                "time_us": main_reappearance_time,
                "stage": main_reappearance_stage,
                "state": "present",
                **visible_window(
                    100,
                    f"ROUND10|{main_reappearance_stage}",
                ),
            }
        )
        main_reappearance_report["window_samples"].append(
            {
                "time_us": main_reappearance_time,
                "stage": main_reappearance_stage,
                "windows": [
                    visible_window(
                        100,
                        f"ROUND10|{main_reappearance_stage}",
                    )
                ],
            }
        )
        with self.subTest("main teardown requires a fresh visibility episode"):
            self.assertIn(
                "main window became visible after teardown without SHOW",
                round10_private_hwnd_probe._report_violations(
                    main_reappearance_report
                ),
            )

        owned_qt_main_report = copy.deepcopy(coherent_report)
        owned_main_hwnd = 9_000_006
        startup_time = stage_start_times["startup"] + 10
        owned_qt_main_report["visible_events"].extend(
            (
                {
                    "time_us": startup_time,
                    "stage": "startup",
                    "state": "present",
                    **visible_window(100, "ROUND10|startup"),
                },
                {
                    "time_us": startup_time,
                    "stage": "startup",
                    "state": "present",
                    **visible_window(
                        owned_main_hwnd,
                        "ROUND10|startup",
                        owner=100,
                    ),
                },
            )
        )
        owned_qt_main_report["window_samples"].append(
            {
                "time_us": startup_time,
                "stage": "startup",
                "windows": [
                    visible_window(100, "ROUND10|startup"),
                    visible_window(
                        owned_main_hwnd,
                        "ROUND10|startup",
                        owner=100,
                    ),
                ],
            }
        )
        owned_qt_main_report["child_result"][
            "qt_top_level_samples"
        ].extend(
            (
                qt_top_level_sample(
                    100,
                    "ROUND10|startup",
                    stage="startup",
                    time_us=startup_time,
                    main=True,
                ),
                qt_top_level_sample(
                    owned_main_hwnd,
                    "ROUND10|startup",
                    stage="startup",
                    time_us=startup_time,
                    main=True,
                ),
            )
        )
        with self.subTest("Qt production main is the ownerless native main"):
            self.assertIn(
                "Qt production main HWND evidence is invalid",
                round10_private_hwnd_probe._report_violations(
                    owned_qt_main_report
                ),
            )

        visible_titlebar_report = copy.deepcopy(coherent_report)
        visible_titlebar_report["titlebar_events"].append(
            {
                "time_us": stage_start_times["preflight"] + 10,
                "stage": "preflight",
                "state": "present",
                **visible_window(999_999_997, ""),
                "class": "_q_titlebar",
            }
        )
        self.assertIn(
            "visible top-level _q_titlebar was observed",
            round10_private_hwnd_probe._report_violations(
                visible_titlebar_report
            ),
        )

        out_of_stage_sample_report = copy.deepcopy(coherent_report)
        source_sample = next(
            sample
            for sample in coherent_report["window_samples"]
            if sample["stage"] == "task7_special_whole"
        )
        forged_sample = copy.deepcopy(source_sample)
        forged_sample["time_us"] = 0
        out_of_stage_sample_report["window_samples"].append(forged_sample)
        source_events = [
            event
            for event in coherent_report["visible_events"]
            if event["stage"] == "task7_special_whole"
            and event["time_us"] == source_sample["time_us"]
        ]
        out_of_stage_sample_report["visible_events"].extend(
            {**event, "time_us": 0}
            for event in source_events
        )
        self.assertIn(
            "window sample stage timing evidence is invalid",
            round10_private_hwnd_probe._report_violations(
                out_of_stage_sample_report
            ),
        )

        missing_concrete_stage_report = copy.deepcopy(coherent_report)
        missing_stage = "return_folder:1:select"
        for key in (
            "lifecycle_events",
            "qt_object_events",
            "qt_top_level_samples",
        ):
            missing_concrete_stage_report["child_result"][key] = [
                record
                for record in missing_concrete_stage_report["child_result"][key]
                if record.get("stage") != missing_stage
            ]
        for key in ("visible_events", "window_samples"):
            missing_concrete_stage_report[key] = [
                record
                for record in missing_concrete_stage_report[key]
                if record.get("stage") != missing_stage
            ]
        self.assertIn(
            f"stage dialog was not observed: {missing_stage}",
            round10_private_hwnd_probe._report_violations(
                missing_concrete_stage_report
            ),
        )

        wrong_concrete_title_report = copy.deepcopy(coherent_report)
        wrong_title_stage = "return_folder:0:split"
        wrong_title = "确认样品归属"
        for key in ("lifecycle_events", "qt_object_events"):
            for record in wrong_concrete_title_report["child_result"][key]:
                if (
                    record.get("stage") == wrong_title_stage
                    and record.get("hwnd") != 100
                ):
                    record["title"] = wrong_title
        for record in wrong_concrete_title_report["child_result"][
            "qt_top_level_samples"
        ]:
            if (
                record.get("stage") == wrong_title_stage
                and record.get("hwnd") != 100
            ):
                record["title"] = wrong_title
        for record in wrong_concrete_title_report["visible_events"]:
            if (
                record.get("stage") == wrong_title_stage
                and record.get("hwnd") != 100
            ):
                record["title"] = wrong_title
        for sample in wrong_concrete_title_report["window_samples"]:
            if sample.get("stage") != wrong_title_stage:
                continue
            for window in sample["windows"]:
                if window.get("hwnd") != 100:
                    window["title"] = wrong_title
        self.assertIn(
            f"stage dialog was not observed: {wrong_title_stage}",
            round10_private_hwnd_probe._report_violations(
                wrong_concrete_title_report
            ),
        )

        later_native_show_without_fresh_evidence = copy.deepcopy(
            coherent_report
        )
        later_native_show_without_fresh_evidence["child_result"][
            "lifecycle_events"
        ].append(
            lifecycle_event(
                201,
                "预检设置",
                stage="preflight",
                time_us=stage_start_times["preflight"] + 20,
            )
        )
        self.assertIn(
            "native lifecycle evidence is unbound",
            round10_private_hwnd_probe._report_violations(
                later_native_show_without_fresh_evidence
            ),
        )

        central_hwnd = 999_999_991
        stale_win_id_change_report = copy.deepcopy(coherent_report)
        stale_win_id_change_report["child_result"][
            "lifecycle_events"
        ].extend(
            (
                lifecycle_event(
                    central_hwnd,
                    "python",
                    stage="preflight",
                    time_us=stage_start_times["preflight"] + 30,
                ),
                lifecycle_event(
                    central_hwnd,
                    "python",
                    stage="preflight",
                    time_us=stage_start_times["preflight"] + 30,
                ),
            )
        )
        stale_win_id_change_report["child_result"][
            "qt_object_events"
        ].append(
            {
                **qt_object_event(
                    central_hwnd,
                    "",
                    stage="preflight",
                    time_us=stage_start_times["preflight"] + 25,
                ),
                "event": "WinIdChange",
                "class": "QWidget",
                "object_name": "production_central",
                "is_window": False,
                "parent_class": "QMainWindow",
                "parent_object_name": "production_main_window",
            }
        )
        self.assertIn(
            "native lifecycle evidence is unbound",
            round10_private_hwnd_probe._report_violations(
                stale_win_id_change_report
            ),
        )

        aliased_first_show_report = copy.deepcopy(coherent_report)
        aliased_first_show = lifecycle_event(
            central_hwnd,
            "python",
            stage="preflight",
            time_us=stage_start_times["preflight"] + 30,
        )
        aliased_first_show_report["child_result"][
            "lifecycle_events"
        ].extend((aliased_first_show, aliased_first_show))
        aliased_first_show_report["child_result"][
            "qt_object_events"
        ].append(
            {
                **qt_object_event(
                    central_hwnd,
                    "",
                    stage="preflight",
                    time_us=stage_start_times["preflight"] + 25,
                ),
                "event": "WinIdChange",
                "class": "QWidget",
                "object_name": "production_central",
                "is_window": False,
                "parent_class": "QMainWindow",
                "parent_object_name": "production_main_window",
            }
        )
        self.assertIn(
            "native lifecycle evidence is unbound",
            round10_private_hwnd_probe._report_violations(
                aliased_first_show_report
            ),
        )

        post_show_win_id_change_report = copy.deepcopy(coherent_report)
        post_show_win_id_change_report["child_result"][
            "lifecycle_events"
        ].append(
            lifecycle_event(
                central_hwnd,
                "python",
                stage="preflight",
                time_us=stage_start_times["preflight"] + 20,
            )
        )
        post_show_win_id_change_report["child_result"][
            "qt_object_events"
        ].append(
            {
                **qt_object_event(
                    central_hwnd,
                    "",
                    stage="preflight",
                    time_us=stage_start_times["preflight"] + 21,
                ),
                "event": "WinIdChange",
                "class": "QWidget",
                "object_name": "production_central",
                "is_window": False,
                "parent_class": "QMainWindow",
                "parent_object_name": "production_main_window",
            }
        )
        self.assertIn(
            "native lifecycle evidence is unbound",
            round10_private_hwnd_probe._report_violations(
                post_show_win_id_change_report
            ),
        )

        later_qt_show_without_fresh_evidence = copy.deepcopy(coherent_report)
        later_qt_show_without_fresh_evidence["child_result"][
            "qt_object_events"
        ].append(
            qt_object_event(
                201,
                "预检设置",
                stage="preflight",
                time_us=stage_start_times["preflight"] + 20,
            )
        )
        self.assertIn(
            "Qt object lifecycle evidence is unbound",
            round10_private_hwnd_probe._report_violations(
                later_qt_show_without_fresh_evidence
            ),
        )

        split_after_hide_report = copy.deepcopy(coherent_report)
        split_stage = "return_folder:0:split"
        split_time = stage_start_times[split_stage] + 20
        split_after_hide_report["child_result"]["lifecycle_events"].append(
            lifecycle_event(
                202,
                "确认样品归属",
                stage="return_folder",
                time_us=stage_start_times["return_folder"] + 50,
            )
            | {"event": "hide"}
        )
        split_after_hide_report["child_result"]["lifecycle_events"].append(
            lifecycle_event(
                202,
                "确认样品归属",
                stage=split_stage,
                time_us=split_time,
            )
        )
        split_after_hide_report["child_result"]["qt_object_events"].append(
            qt_object_event(
                202,
                "确认样品归属",
                stage=split_stage,
                time_us=split_time,
            )
        )
        split_after_hide_report["child_result"][
            "qt_top_level_samples"
        ].append(
            qt_top_level_sample(
                202,
                "确认样品归属",
                stage=split_stage,
                time_us=split_time,
            )
        )
        split_after_hide_report["visible_events"].append(
            {
                "time_us": split_time,
                "stage": split_stage,
                "state": "present",
                **visible_window(
                    202,
                    "确认样品归属",
                    owner=100,
                ),
            }
        )
        split_after_hide_report["window_samples"].append(
            {
                "time_us": split_time,
                "stage": split_stage,
                "windows": [
                    visible_window(100, f"ROUND10|{split_stage}"),
                    visible_window(
                        202,
                        "确认样品归属",
                        owner=100,
                    ),
                ],
            }
        )
        self.assertIn(
            "dialog HWND was reused across stages without teardown",
            round10_private_hwnd_probe._report_violations(
                split_after_hide_report
            ),
        )

        reordered_stage_report = copy.deepcopy(coherent_report)
        reordered_transitions = reordered_stage_report["child_result"][
            "stage_transitions"
        ]
        preflight_transition = next(
            transition
            for transition in reordered_transitions
            if transition["stage"] == "preflight"
        )
        return_transition = next(
            transition
            for transition in reordered_transitions
            if transition["stage"] == "return_folder"
        )
        preflight_transition["stage"] = "return_folder"
        return_transition["stage"] = "preflight"
        for key in (
            "lifecycle_events",
            "qt_object_events",
            "qt_top_level_samples",
        ):
            for record in reordered_stage_report["child_result"][key]:
                if record["stage"] == "preflight":
                    record["time_us"] = stage_start_times["return_folder"] + 10
                elif record["stage"] == "return_folder":
                    record["time_us"] = stage_start_times["preflight"] + 10
        for key in ("visible_events", "window_samples"):
            for record in reordered_stage_report[key]:
                if record["stage"] == "preflight":
                    record["time_us"] = stage_start_times["return_folder"] + 10
                elif record["stage"] == "return_folder":
                    record["time_us"] = stage_start_times["preflight"] + 10
        self.assertIn(
            "stage transition evidence is invalid",
            round10_private_hwnd_probe._report_violations(
                reordered_stage_report
            ),
        )

        rogue_stage_report = copy.deepcopy(coherent_report)
        rogue_stage_report["child_result"]["stage_transitions"].insert(
            2,
            {"time_us": 150, "stage": "rogue_unobserved"},
        )
        self.assertIn(
            "stage transition evidence is invalid",
            round10_private_hwnd_probe._report_violations(
                rogue_stage_report
            ),
        )

        wrong_stage_title_report = copy.deepcopy(coherent_report)
        wrong_stage_title_report["child_result"][
            "qt_top_level_samples"
        ].append(
            {
                **qt_top_level_sample(
                    201,
                    "选择激发谱",
                    stage="preflight",
                    time_us=stage_start_times["preflight"] + 20,
                ),
            }
        )
        self.assertIn(
            "unexpected Qt top-level window was observed",
            round10_private_hwnd_probe._report_violations(
                wrong_stage_title_report
            ),
        )

        later_unobserved_show_report = copy.deepcopy(coherent_report)
        later_unobserved_show_report["child_result"]["lifecycle_events"].extend(
            (
                lifecycle_event(
                    201,
                    "预检设置",
                    stage="preflight",
                    time_us=stage_start_times["preflight"] + 20,
                )
                | {"event": "hide"},
                lifecycle_event(
                    201,
                    "预检设置",
                    stage="preflight",
                    time_us=stage_start_times["preflight"] + 30,
                ),
            )
        )
        self.assertIn(
            "native lifecycle evidence is unbound",
            round10_private_hwnd_probe._report_violations(
                later_unobserved_show_report
            ),
        )

        later_unobserved_qt_show_report = copy.deepcopy(coherent_report)
        later_unobserved_qt_show_report["child_result"][
            "qt_object_events"
        ].extend(
            (
                qt_object_event(
                    201,
                    "预检设置",
                    stage="preflight",
                    time_us=stage_start_times["preflight"] + 20,
                )
                | {"event": "Hide", "visible": False},
                qt_object_event(
                    201,
                    "预检设置",
                    stage="preflight",
                    time_us=stage_start_times["preflight"] + 30,
                ),
            )
        )
        self.assertIn(
            "Qt object lifecycle evidence is unbound",
            round10_private_hwnd_probe._report_violations(
                later_unobserved_qt_show_report
            ),
        )

        reused_concrete_stage_report = copy.deepcopy(coherent_report)
        concrete_stage = "return_folder:1:select"
        concrete_time = stage_start_times[concrete_stage] + 10
        reused_concrete_stage_report["child_result"][
            "lifecycle_events"
        ].append(
            lifecycle_event(
                202,
                "选择要归属的 Book",
                stage=concrete_stage,
                time_us=concrete_time,
            )
        )
        reused_concrete_stage_report["child_result"][
            "qt_object_events"
        ].append(
            qt_object_event(
                202,
                "选择要归属的 Book",
                stage=concrete_stage,
                time_us=concrete_time,
            )
        )
        reused_concrete_stage_report["child_result"][
            "qt_top_level_samples"
        ].extend(
            (
                qt_top_level_sample(
                    100,
                    f"ROUND10|{concrete_stage}",
                    stage=concrete_stage,
                    time_us=concrete_time,
                    main=True,
                ),
                qt_top_level_sample(
                    202,
                    "选择要归属的 Book",
                    stage=concrete_stage,
                    time_us=concrete_time,
                ),
            )
        )
        reused_concrete_stage_report["visible_events"].extend(
            (
                {
                    "time_us": concrete_time,
                    "stage": concrete_stage,
                    "state": "present",
                    **visible_window(100, f"ROUND10|{concrete_stage}"),
                },
                {
                    "time_us": concrete_time,
                    "stage": concrete_stage,
                    "state": "present",
                    **visible_window(
                        202,
                        "选择要归属的 Book",
                        owner=100,
                    ),
                },
            )
        )
        reused_concrete_stage_report["window_samples"].append(
            {
                "time_us": concrete_time,
                "stage": concrete_stage,
                "windows": [
                    visible_window(100, f"ROUND10|{concrete_stage}"),
                    visible_window(
                        202,
                        "选择要归属的 Book",
                        owner=100,
                    ),
                ],
            }
        )
        self.assertIn(
            "dialog HWND was reused across stages without teardown",
            round10_private_hwnd_probe._report_violations(
                reused_concrete_stage_report
            ),
        )

        wrong_identity_teardown_report = copy.deepcopy(
            reused_concrete_stage_report
        )
        teardown_stage = "return_folder:0:split"
        teardown_time = stage_start_times[teardown_stage] + 50
        wrong_identity_teardown_report["child_result"][
            "lifecycle_events"
        ].append(
            lifecycle_event(
                202,
                f"ROUND10|{teardown_stage}",
                stage=teardown_stage,
                time_us=teardown_time,
            )
            | {"event": "hide"}
        )
        wrong_identity_teardown_report["child_result"][
            "qt_top_level_samples"
        ].append(
            {
                **qt_top_level_sample(
                    202,
                    f"ROUND10|{teardown_stage}",
                    stage=teardown_stage,
                    time_us=teardown_time,
                    main=True,
                ),
                "state": "not_visible",
            }
        )
        self.assertIn(
            "dialog HWND was reused across stages without teardown",
            round10_private_hwnd_probe._report_violations(
                wrong_identity_teardown_report
            ),
        )

        correct_teardown_report = copy.deepcopy(reused_concrete_stage_report)
        correct_teardown_report["child_result"]["lifecycle_events"].append(
            lifecycle_event(
                202,
                "确认样品归属",
                stage=teardown_stage,
                time_us=teardown_time,
            )
            | {"event": "hide"}
        )
        correct_teardown_report["child_result"][
            "qt_top_level_samples"
        ].append(
            {
                **qt_top_level_sample(
                    202,
                    "确认样品归属",
                    stage=teardown_stage,
                    time_us=teardown_time,
                ),
                "state": "not_visible",
            }
        )
        self.assertEqual(
            (),
            round10_private_hwnd_probe._report_violations(
                correct_teardown_report
            ),
        )

        foreign_cbt_teardown_report = copy.deepcopy(correct_teardown_report)
        target_teardown = next(
            event
            for event in foreign_cbt_teardown_report["child_result"][
                "lifecycle_events"
            ]
            if event["hwnd"] == 202
            and event["stage"] == teardown_stage
            and event["event"] == "hide"
            and event["title"] == "确认样品归属"
        )
        target_teardown.update(
            {
                "source": "cbt",
                "event": "destroy",
                "hwnd": 999_999_998,
                "class": "",
                "title": "",
            }
        )
        self.assertIn(
            "dialog HWND was reused across stages without teardown",
            round10_private_hwnd_probe._report_violations(
                foreign_cbt_teardown_report
            ),
        )

        previous_stage = "return_folder:3:return_folder"
        terminal_stage = "return_folder:4:confirm"
        previous_dialog = next(
            window
            for sample in coherent_report["window_samples"]
            if sample["stage"] == previous_stage
            for window in sample["windows"]
            if window["owner"]
        )
        terminal_dialog = next(
            window
            for sample in coherent_report["window_samples"]
            if sample["stage"] == terminal_stage
            for window in sample["windows"]
            if window["owner"]
        )
        shared_hwnd = previous_dialog["hwnd"]
        replaced_hwnd = terminal_dialog["hwnd"]

        def reuse_terminal_dialog(record):
            if (
                record.get("stage") == terminal_stage
                and record.get("hwnd") == replaced_hwnd
            ):
                return {**record, "hwnd": shared_hwnd}
            return record

        teardown_without_show_report = {
            **coherent_report,
            "child_result": {
                **coherent_report["child_result"],
                "lifecycle_events": [
                    reuse_terminal_dialog(event)
                    for event in coherent_report["child_result"][
                        "lifecycle_events"
                    ]
                    if not (
                        event.get("stage") == terminal_stage
                        and event.get("hwnd") == replaced_hwnd
                        and str(event.get("event", "")).casefold() == "show"
                    )
                ],
                "qt_object_events": [
                    reuse_terminal_dialog(event)
                    for event in coherent_report["child_result"][
                        "qt_object_events"
                    ]
                    if not (
                        event.get("stage") == terminal_stage
                        and event.get("hwnd") == replaced_hwnd
                        and event.get("event") == "Show"
                    )
                ],
                "qt_top_level_samples": [
                    reuse_terminal_dialog(sample)
                    for sample in coherent_report["child_result"][
                        "qt_top_level_samples"
                    ]
                ],
            },
            "visible_events": [
                reuse_terminal_dialog(event)
                for event in coherent_report["visible_events"]
            ],
            "window_samples": [
                {
                    **sample,
                    "windows": [
                        (
                            {**window, "hwnd": shared_hwnd}
                            if sample["stage"] == terminal_stage
                            and window.get("hwnd") == replaced_hwnd
                            else window
                        )
                        for window in sample["windows"]
                    ],
                }
                for sample in coherent_report["window_samples"]
            ],
        }
        self.assertEqual(
            (),
            round10_private_hwnd_probe._report_violations(
                teardown_without_show_report
            ),
        )
        teardown_time = stage_start_times[terminal_stage] + 1
        native_hide_without_qt_report = copy.deepcopy(
            teardown_without_show_report
        )
        native_hide_without_qt_report["child_result"][
            "lifecycle_events"
        ].append(
            lifecycle_event(
                shared_hwnd,
                terminal_dialog["title"],
                stage=terminal_stage,
                time_us=teardown_time,
            )
            | {"event": "hide"}
        )
        self.assertIn(
            "dialog became visible after teardown without SHOW",
            round10_private_hwnd_probe._report_violations(
                native_hide_without_qt_report
            ),
        )

        native_destroy_without_qt_report = copy.deepcopy(
            teardown_without_show_report
        )
        native_destroy_without_qt_report["child_result"][
            "lifecycle_events"
        ].append(
            lifecycle_event(
                shared_hwnd,
                terminal_dialog["title"],
                stage=terminal_stage,
                time_us=teardown_time,
            )
            | {"event": "destroy"}
        )
        self.assertIn(
            "dialog became visible after teardown without SHOW",
            round10_private_hwnd_probe._report_violations(
                native_destroy_without_qt_report
            ),
        )

        teardown_without_show_report["child_result"][
            "lifecycle_events"
        ].append(
            lifecycle_event(
                shared_hwnd,
                terminal_dialog["title"],
                stage=terminal_stage,
                time_us=teardown_time,
            )
            | {"event": "hide"}
        )
        teardown_without_show_report["child_result"][
            "qt_top_level_samples"
        ].append(
            {
                **qt_top_level_sample(
                    shared_hwnd,
                    terminal_dialog["title"],
                    stage=terminal_stage,
                    time_us=teardown_time,
                ),
                "state": "not_visible",
            }
        )
        self.assertIn(
            "dialog became visible after teardown without SHOW",
            round10_private_hwnd_probe._report_violations(
                teardown_without_show_report
            ),
        )

        child_show_after_teardown_report = copy.deepcopy(
            teardown_without_show_report
        )
        child_show_after_teardown_report["child_result"][
            "qt_object_events"
        ].append(
            {
                "time_us": teardown_time + 1,
                "stage": terminal_stage,
                "event": "Show",
                "class": "QLabel",
                "object_name": "dialog_form_label",
                "title": "",
                "hwnd": shared_hwnd,
                "is_window": False,
                "flags": 0,
                "visible": True,
                "parent_class": "QDialog",
                "parent_object_name": "organizer_dialog",
                "geometry": [0, 0, 1, 1],
            }
        )
        self.assertIn(
            "dialog became visible after teardown without SHOW",
            round10_private_hwnd_probe._report_violations(
                child_show_after_teardown_report
            ),
        )

        equal_time_show_before_teardown_report = copy.deepcopy(
            teardown_without_show_report
        )
        equal_time_native_events = equal_time_show_before_teardown_report[
            "child_result"
        ]["lifecycle_events"]
        equal_time_native_hide = next(
            event
            for event in equal_time_native_events
            if event.get("stage") == terminal_stage
            and event.get("hwnd") == shared_hwnd
            and event.get("event") == "hide"
            and event.get("time_us") == teardown_time
        )
        equal_time_native_events.remove(equal_time_native_hide)
        equal_time_native_events.extend(
            (
                lifecycle_event(
                    shared_hwnd,
                    terminal_dialog["title"],
                    stage=terminal_stage,
                    time_us=teardown_time,
                ),
                equal_time_native_hide,
            )
        )
        equal_time_native_events.sort(key=lambda event: event["time_us"])
        equal_time_qt_events = equal_time_show_before_teardown_report[
            "child_result"
        ]["qt_object_events"]
        equal_time_qt_events.extend(
            (
                qt_object_event(
                    shared_hwnd,
                    terminal_dialog["title"],
                    stage=terminal_stage,
                    time_us=teardown_time,
                ),
                {
                    **qt_object_event(
                        shared_hwnd,
                        terminal_dialog["title"],
                        stage=terminal_stage,
                        time_us=teardown_time,
                    ),
                    "event": "Hide",
                    "visible": False,
                },
            )
        )
        equal_time_qt_events.sort(key=lambda event: event["time_us"])
        self.assertIn(
            "dialog became visible after teardown without SHOW",
            round10_private_hwnd_probe._report_violations(
                equal_time_show_before_teardown_report
            ),
        )

        native_show_before_qt_teardown_report = copy.deepcopy(
            teardown_without_show_report
        )
        native_show_before_qt_teardown_report["child_result"][
            "lifecycle_events"
        ].append(
            lifecycle_event(
                shared_hwnd,
                terminal_dialog["title"],
                stage=terminal_stage,
                time_us=teardown_time + 1,
            )
        )
        native_show_before_qt_teardown_report["child_result"][
            "qt_top_level_samples"
        ].append(
            {
                **qt_top_level_sample(
                    shared_hwnd,
                    terminal_dialog["title"],
                    stage=terminal_stage,
                    time_us=teardown_time + 1,
                ),
                "state": "not_visible",
            }
        )
        native_show_before_qt_teardown_report["child_result"][
            "qt_object_events"
        ].extend(
            (
                {
                    **qt_object_event(
                        shared_hwnd,
                        terminal_dialog["title"],
                        stage=terminal_stage,
                        time_us=teardown_time + 1,
                    ),
                    "event": "Hide",
                    "visible": False,
                },
                qt_object_event(
                    shared_hwnd,
                    terminal_dialog["title"],
                    stage=terminal_stage,
                    time_us=teardown_time + 2,
                ),
            )
        )
        self.assertIn(
            "dialog became visible after teardown without SHOW",
            round10_private_hwnd_probe._report_violations(
                native_show_before_qt_teardown_report
            ),
        )

        qt_show_before_native_teardown_report = copy.deepcopy(
            teardown_without_show_report
        )
        qt_show_before_native_teardown_report["child_result"][
            "lifecycle_events"
        ].extend(
            (
                lifecycle_event(
                    shared_hwnd,
                    terminal_dialog["title"],
                    stage=terminal_stage,
                    time_us=teardown_time + 1,
                )
                | {"event": "hide"},
                lifecycle_event(
                    shared_hwnd,
                    terminal_dialog["title"],
                    stage=terminal_stage,
                    time_us=teardown_time + 2,
                ),
            )
        )
        qt_show_before_native_teardown_report["child_result"][
            "qt_object_events"
        ].append(
            qt_object_event(
                shared_hwnd,
                terminal_dialog["title"],
                stage=terminal_stage,
                time_us=teardown_time + 1,
            )
        )
        self.assertIn(
            "dialog became visible after teardown without SHOW",
            round10_private_hwnd_probe._report_violations(
                qt_show_before_native_teardown_report
            ),
        )

        strictly_later_show_report = copy.deepcopy(
            teardown_without_show_report
        )
        strictly_later_show_report["child_result"][
            "lifecycle_events"
        ].append(
            lifecycle_event(
                shared_hwnd,
                terminal_dialog["title"],
                stage=terminal_stage,
                time_us=teardown_time + 1,
            )
        )
        strictly_later_show_report["child_result"][
            "qt_object_events"
        ].append(
            qt_object_event(
                shared_hwnd,
                terminal_dialog["title"],
                stage=terminal_stage,
                time_us=teardown_time + 1,
            )
        )
        self.assertEqual(
            (),
            round10_private_hwnd_probe._report_violations(
                strictly_later_show_report
            ),
        )

        wrong_native_teardown_identity_report = copy.deepcopy(
            strictly_later_show_report
        )
        wrong_native_teardown = next(
            event
            for event in wrong_native_teardown_identity_report[
                "child_result"
            ]["lifecycle_events"]
            if event.get("stage") == terminal_stage
            and event.get("hwnd") == shared_hwnd
            and event.get("event") == "hide"
            and event.get("time_us") == teardown_time
        )
        wrong_native_teardown["class"] = "Qt9999WrongDialogClass"
        future_native_show = next(
            event
            for event in wrong_native_teardown_identity_report[
                "child_result"
            ]["lifecycle_events"]
            if event.get("stage") == terminal_stage
            and event.get("hwnd") == shared_hwnd
            and event.get("event") == "show"
            and event.get("time_us") > teardown_time
        )
        future_native_show["class"] = "Qt9999WrongDialogClass"
        for event in wrong_native_teardown_identity_report[
            "visible_events"
        ]:
            if (
                event.get("stage") == terminal_stage
                and event.get("hwnd") == shared_hwnd
                and event.get("time_us") > teardown_time
            ):
                event["class"] = "Qt9999WrongDialogClass"
        for sample in wrong_native_teardown_identity_report[
            "window_samples"
        ]:
            if (
                sample.get("stage") == terminal_stage
                and sample.get("time_us") > teardown_time
            ):
                for window in sample["windows"]:
                    if window.get("hwnd") == shared_hwnd:
                        window["class"] = "Qt9999WrongDialogClass"
        self.assertIn(
            "dialog HWND was reused across stages without teardown",
            round10_private_hwnd_probe._report_violations(
                wrong_native_teardown_identity_report
            ),
        )

        delayed_stage_start_times = {
            stage: index * 1_000_000
            for index, stage in enumerate(expected_stage_sequence)
        }

        def move_within_stage(record, offset):
            return {
                **record,
                "time_us": delayed_stage_start_times[record["stage"]] + offset,
            }

        delayed_channel_report = {
            **coherent_report,
            "child_result": {
                **coherent_report["child_result"],
                "lifecycle_events": [
                    move_within_stage(event, 10)
                    for event in coherent_report["child_result"][
                        "lifecycle_events"
                    ]
                ],
                "qt_object_events": [
                    move_within_stage(event, 10)
                    for event in coherent_report["child_result"][
                        "qt_object_events"
                    ]
                ],
                "qt_top_level_samples": [
                    move_within_stage(sample, 600_000)
                    for sample in coherent_report["child_result"][
                        "qt_top_level_samples"
                    ]
                ],
                "stage_transitions": [
                    {
                        "time_us": delayed_stage_start_times[stage],
                        "stage": stage,
                    }
                    for stage in expected_stage_sequence
                ],
            },
            "visible_events": [
                move_within_stage(event, 600_000)
                for event in coherent_report["visible_events"]
            ],
            "window_samples": [
                move_within_stage(sample, 600_000)
                for sample in coherent_report["window_samples"]
            ],
        }
        self.assertEqual(
            (),
            round10_private_hwnd_probe._report_violations(
                delayed_channel_report
            ),
        )

        arbitrary_child_binding_report = {
            **coherent_report,
            "child_result": {
                **coherent_report["child_result"],
                "lifecycle_events": (
                    coherent_report["child_result"]["lifecycle_events"]
                    + [
                        lifecycle_event(
                            999_999_999,
                            "FORGED_NATIVE_SHOW",
                            stage="preflight",
                            time_us=1,
                        )
                        | {"class": "Qt9999UnknownNativeWindow"}
                    ]
                ),
                "qt_object_events": (
                    coherent_report["child_result"]["qt_object_events"]
                    + [
                        qt_object_event(
                            999_999_999,
                            "",
                            stage="preflight",
                            time_us=1,
                        )
                        | {
                            "event": "ParentChange",
                            "class": "QLabel",
                            "object_name": "arbitrary_child",
                            "is_window": False,
                            "parent_class": "QWidget",
                            "parent_object_name": "unrelated_parent",
                        }
                    ]
                ),
            },
        }
        self.assertIn(
            "native lifecycle evidence is unbound",
            round10_private_hwnd_probe._report_violations(
                arbitrary_child_binding_report
            ),
        )

        invisible_sample_report = {
            **coherent_report,
            "window_samples": [
                {
                    **sample,
                    "windows": [
                        {
                            **window,
                            "visible": False,
                            "cloaked": 1,
                        }
                        for window in sample["windows"]
                    ],
                }
                for sample in coherent_report["window_samples"]
            ],
        }
        self.assertIn(
            "window sample evidence is invalid",
            round10_private_hwnd_probe._report_violations(
                invisible_sample_report
            ),
        )

        hidden_style_report = {
            **coherent_report,
            "visible_events": [
                {
                    **event,
                    "style": int(event["style"]) & ~0x10000000,
                }
                for event in coherent_report["visible_events"]
            ],
            "window_samples": [
                {
                    **sample,
                    "windows": [
                        {
                            **window,
                            "style": int(window["style"]) & ~0x10000000,
                        }
                        for window in sample["windows"]
                    ],
                }
                for sample in coherent_report["window_samples"]
            ],
        }
        self.assertIn(
            "window sample evidence is invalid",
            round10_private_hwnd_probe._report_violations(
                hidden_style_report
            ),
        )

        dialog_hwnds = set(
            range(201, 201 + len(active_dialog_stages))
        )

        def reuse_dialog_hwnd(record):
            if record.get("hwnd") not in dialog_hwnds:
                return record
            return {**record, "hwnd": 200}

        reused_dialog_hwnd_report = {
            **coherent_report,
            "child_result": {
                **coherent_report["child_result"],
                "lifecycle_events": [
                    reuse_dialog_hwnd(event)
                    for event in coherent_report["child_result"][
                        "lifecycle_events"
                    ]
                ],
                "qt_object_events": [
                    reuse_dialog_hwnd(event)
                    for event in coherent_report["child_result"][
                        "qt_object_events"
                    ]
                ],
                "qt_top_level_samples": [
                    reuse_dialog_hwnd(sample)
                    for sample in coherent_report["child_result"][
                        "qt_top_level_samples"
                    ]
                ],
            },
            "visible_events": [
                reuse_dialog_hwnd(event)
                for event in coherent_report["visible_events"]
            ],
            "window_samples": [
                {
                    **sample,
                    "windows": [
                        reuse_dialog_hwnd(window)
                        for window in sample["windows"]
                    ],
                }
                for sample in coherent_report["window_samples"]
            ],
        }
        self.assertIn(
            "dialog HWND was reused across stages without teardown",
            round10_private_hwnd_probe._report_violations(
                reused_dialog_hwnd_report
            ),
        )

        forged_lifecycle_report = {
            **coherent_report,
            "child_result": {
                **coherent_report["child_result"],
                "lifecycle_events": [
                    lifecycle_event(
                        999_999_999,
                        "FORGED",
                        stage="forged-native",
                        time_us=1,
                    )
                    | {"event": "create"}
                ],
                "qt_object_events": [
                    qt_object_event(
                        888_888_888,
                        "FORGED",
                        stage="forged-qt",
                        time_us=2,
                    )
                ],
            },
        }
        self.assertIn(
            "native lifecycle evidence is unbound",
            round10_private_hwnd_probe._report_violations(
                forged_lifecycle_report
            ),
        )

        later_bad_style_event_report = {
            **coherent_report,
            "visible_events": (
                coherent_report["visible_events"]
                + [
                    {
                        **coherent_report["visible_events"][1],
                        "time_us": 100,
                        "exstyle": 0x80,
                    }
                ]
            ),
        }
        self.assertIn(
            "manual dialog taskbar evidence is invalid",
            round10_private_hwnd_probe._report_violations(
                later_bad_style_event_report
            ),
        )

        dialog_without_main_report = {
            **coherent_report,
            "window_samples": (
                coherent_report["window_samples"]
                + [
                    {
                        "time_us": 100,
                        "stage": "preflight",
                        "windows": [
                            visible_window(
                                201,
                                "预检设置",
                                owner=0,
                                exstyle=0x80,
                            )
                        ],
                    }
                ]
            ),
        }
        self.assertIn(
            "manual dialog taskbar evidence is invalid",
            round10_private_hwnd_probe._report_violations(
                dialog_without_main_report
            ),
        )

        later_wrong_title_event_report = {
            **coherent_report,
            "visible_events": (
                coherent_report["visible_events"]
                + [
                    {
                        **coherent_report["visible_events"][1],
                        "time_us": 100,
                        "title": "恶意错误标题",
                    }
                ]
            ),
        }
        self.assertIn(
            "visible window evidence is unbound",
            round10_private_hwnd_probe._report_violations(
                later_wrong_title_event_report
            ),
        )

        later_wrong_style_sample = {
            **coherent_report["window_samples"][0],
            "time_us": 100,
            "windows": [
                (
                    {
                        **window,
                        "style": int(window["style"]) ^ 0x40000000,
                    }
                    if window["owner"]
                    else window
                )
                for window in coherent_report["window_samples"][0][
                    "windows"
                ]
            ],
        }
        later_wrong_style_sample_report = {
            **coherent_report,
            "window_samples": (
                coherent_report["window_samples"]
                + [later_wrong_style_sample]
            ),
        }
        self.assertIn(
            "window sample evidence is unbound",
            round10_private_hwnd_probe._report_violations(
                later_wrong_style_sample_report
            ),
        )

        paired_forged_show_report = {
            **coherent_report,
            "child_result": {
                **coherent_report["child_result"],
                "lifecycle_events": (
                    coherent_report["child_result"]["lifecycle_events"]
                    + [
                        lifecycle_event(
                            999_999_999,
                            "UNKNOWN_NATIVE_SHOW",
                            stage="preflight",
                            time_us=1,
                        )
                        | {"class": "Qt9999UnknownNativeWindow"}
                    ]
                ),
                "qt_object_events": (
                    coherent_report["child_result"]["qt_object_events"]
                    + [
                        qt_object_event(
                            999_999_999,
                            "UNKNOWN_QT_SHOW",
                            stage="preflight",
                            time_us=1,
                        )
                        | {
                            "class": "QUnknownDialog",
                            "object_name": "unexpected_unknown_show",
                        }
                    ]
                ),
            },
        }
        paired_forged_show_violations = (
            round10_private_hwnd_probe._report_violations(
                paired_forged_show_report
            )
        )
        self.assertIn(
            "native lifecycle evidence is unbound",
            paired_forged_show_violations,
        )
        self.assertIn(
            "Qt object lifecycle evidence is unbound",
            paired_forged_show_violations,
        )

        toolwindow_report = {
            **coherent_report,
            "visible_events": [
                (
                    {
                        **event,
                        "exstyle": 0x80,
                    }
                    if event["owner"]
                    else event
                )
                for event in coherent_report["visible_events"]
            ],
            "window_samples": [
                {
                    **sample,
                    "windows": [
                        (
                            {
                                **window,
                                "exstyle": 0x80,
                            }
                            if window["owner"]
                            else window
                        )
                        for window in sample["windows"]
                    ],
                }
                for sample in coherent_report["window_samples"]
            ],
        }
        self.assertIn(
            "stage dialog was not observed: preflight",
            round10_private_hwnd_probe._report_violations(
                toolwindow_report
            ),
        )
        later_toolwindow_report = {
            **coherent_report,
            "window_samples": (
                coherent_report["window_samples"]
                + [
                    {
                        "time_us": 100,
                        "stage": "preflight",
                        "windows": [
                            visible_window(
                                100,
                                "ROUND10|preflight",
                            ),
                            visible_window(
                                201,
                                "预检设置",
                                owner=100,
                                exstyle=0x80,
                            ),
                        ],
                    }
                ]
            ),
        }
        self.assertIn(
            "manual dialog taskbar evidence is invalid",
            round10_private_hwnd_probe._report_violations(
                later_toolwindow_report
            ),
        )

        unknown_show_report = {
            **coherent_report,
            "child_result": {
                **coherent_report["child_result"],
                "lifecycle_events": (
                    coherent_report["child_result"]["lifecycle_events"]
                    + [
                        lifecycle_event(
                            999,
                            "未知窗口",
                            stage="preflight",
                            time_us=1,
                        )
                    ]
                ),
            },
        }
        self.assertIn(
            "native lifecycle evidence is unbound",
            round10_private_hwnd_probe._report_violations(
                unknown_show_report
            ),
        )

        wrong_stage_qt_report = {
            **coherent_report,
            "child_result": {
                **coherent_report["child_result"],
                "qt_top_level_samples": [
                    (
                        {
                            **sample,
                            "stage": "return_folder",
                        }
                        if sample.get("hwnd") == 201
                        else sample
                    )
                    for sample in coherent_report["child_result"][
                        "qt_top_level_samples"
                    ]
                ],
            },
        }
        self.assertIn(
            "Qt/native stage-window evidence disagrees",
            round10_private_hwnd_probe._report_violations(
                wrong_stage_qt_report
            ),
        )

        invalid_qt_object_report = {
            **coherent_report,
            "child_result": {
                **coherent_report["child_result"],
                "qt_object_events": [{}],
            },
        }
        self.assertIn(
            "Qt object lifecycle evidence is missing",
            round10_private_hwnd_probe._report_violations(
                invalid_qt_object_report
            ),
        )

        impossible_identity_report = {
            **coherent_report,
            "child_result": {
                "status": "ok",
                "completed_stages": list(expected_stages),
            },
            "visible_events": [
                event
                for stage in expected_stages
                for event in (
                    {
                        "time_us": True,
                        "stage": stage,
                        "state": "present",
                        **visible_window(
                            True,
                            f"ROUND10|{stage}",
                        ),
                    },
                    {
                        "time_us": True,
                        "stage": stage,
                        "state": "present",
                        **visible_window(
                            True,
                            (
                                "预检设置"
                                if stage == "preflight"
                                else "确认样品归属"
                            ),
                            owner=True,
                        ),
                    },
                )
            ],
            "window_samples": [
                {
                    "time_us": True,
                    "stage": stage,
                    "windows": [
                        visible_window(
                            True,
                            f"ROUND10|{stage}",
                        ),
                        visible_window(
                            True,
                            (
                                "预检设置"
                                if stage == "preflight"
                                else "确认样品归属"
                            ),
                            owner=True,
                        ),
                    ],
                }
                for stage in expected_stages
            ],
        }
        impossible_identity_violations = (
            round10_private_hwnd_probe._report_violations(
                impossible_identity_report
            )
        )
        self.assertIn(
            "window sample evidence is invalid",
            impossible_identity_violations,
        )
        self.assertIn(
            "Qt top-level evidence is missing",
            impossible_identity_violations,
        )

        third_qt_window_report = {
            **coherent_report,
            "child_result": {
                **coherent_report["child_result"],
                "lifecycle_events": (
                    coherent_report["child_result"]["lifecycle_events"]
                    + [{"event": "show", "hwnd": 999}]
                ),
                "qt_top_level_samples": (
                    coherent_report["child_result"][
                        "qt_top_level_samples"
                    ]
                    + [
                        {
                            "state": "visible",
                            "kind": "widget",
                            "hwnd": 999,
                            "class": "QLabel",
                            "object_name": "unexpected_third",
                            "title": "",
                        }
                    ]
                ),
            },
        }
        self.assertIn(
            "unexpected Qt top-level window was observed",
            round10_private_hwnd_probe._report_violations(
                third_qt_window_report
            ),
        )

        main_time_mismatch_report = {
            **coherent_report,
            "visible_events": [
                {
                    **event,
                    "time_us": (
                        int(event["time_us"]) + 100
                        if event["owner"] == 0
                        else event["time_us"]
                    ),
                }
                for event in coherent_report["visible_events"]
            ],
        }
        self.assertIn(
            "stage dialog was not observed: preflight",
            round10_private_hwnd_probe._report_violations(
                main_time_mismatch_report
            ),
        )

        missing_callback_evidence = dict(coherent_report)
        missing_callback_evidence.pop("callback_errors")
        self.assertIn(
            "callback error evidence is missing",
            round10_private_hwnd_probe._report_violations(
                missing_callback_evidence
            ),
        )
        missing_lookup_evidence = dict(coherent_report)
        missing_lookup_evidence.pop("window_lookup_failures")
        self.assertIn(
            "window ownership lookup evidence is missing",
            round10_private_hwnd_probe._report_violations(
                missing_lookup_evidence
            ),
        )
        missing_race_evidence = dict(coherent_report)
        missing_race_evidence.pop("window_lookup_races")
        self.assertIn(
            "window ownership race evidence is missing",
            round10_private_hwnd_probe._report_violations(
                missing_race_evidence
            ),
        )

        owned_third_report = {
            **main_only_report,
            "window_samples": [
                {
                    "stage": stage,
                    "windows": [
                        visible_window(100, f"ROUND10|{stage}"),
                        visible_window(200, "确认样品归属", owner=100),
                        *(
                            [visible_window(300, "额外窗口", owner=100)]
                            if stage == "preflight"
                            else []
                        ),
                    ],
                }
                for stage in expected_stages
            ],
        }
        self.assertIn(
            "more than two visible target windows were observed",
            round10_private_hwnd_probe._report_violations(
                owned_third_report
            ),
        )

        event_user32 = mock.Mock()

        def event_class_name(_hwnd, buffer, _size):
            buffer.value = "Qt6111QWindowIcon"
            return len(buffer.value)

        def event_window_text(_hwnd, buffer, _size):
            buffer.value = "预检设置"
            return len(buffer.value)

        event_user32.GetClassNameW.side_effect = event_class_name
        event_user32.GetWindowTextW.side_effect = event_window_text
        self.assertEqual(
            ("Qt6111QWindowIcon", "预检设置"),
            round10_private_hwnd_probe._window_event_identity(
                event_user32,
                100,
            ),
        )
        event_user32.GetClassNameW.side_effect = fail_with_last_error(5)
        with self.assertRaisesRegex(RuntimeError, "GetClassNameW failed: 5"):
            round10_private_hwnd_probe._window_event_identity(
                event_user32,
                100,
            )
        event_user32.GetClassNameW.side_effect = event_class_name
        event_user32.GetWindowTextW.side_effect = fail_with_last_error(5)
        with self.assertRaisesRegex(RuntimeError, "GetWindowTextW failed: 5"):
            round10_private_hwnd_probe._window_event_identity(
                event_user32,
                100,
            )
        event_user32.GetWindowTextW.side_effect = lambda *_args: 0
        self.assertEqual(
            ("Qt6111QWindowIcon", ""),
            round10_private_hwnd_probe._window_event_identity(
                event_user32,
                100,
            ),
        )

        probe_artifact_root = (
            ROOT / ".test-tmp" / "round10-private-hwnd"
        )
        probe_artifacts_before = (
            {path.name for path in probe_artifact_root.iterdir()}
            if probe_artifact_root.is_dir()
            else set()
        )
        original_window_snapshot = round10_private_hwnd_probe._window_snapshot
        callback_failure = {"injected": False}

        def fail_first_window_snapshot(*args, **kwargs):
            if not callback_failure["injected"]:
                callback_failure["injected"] = True
                raise RuntimeError("INJECTED_CALLBACK_FAILURE")
            return original_window_snapshot(*args, **kwargs)

        with mock.patch.object(
            round10_private_hwnd_probe,
            "_window_snapshot",
            side_effect=fail_first_window_snapshot,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "EnumDesktopWindows callback failed.*INJECTED_CALLBACK_FAILURE",
            ):
                round10_private_hwnd_probe._capture_cycle(901)
        self.assertTrue(callback_failure["injected"])

        with mock.patch.object(
            round10_private_hwnd_probe,
            "_windows_libraries",
            return_value=(user32, kernel32, dwmapi),
        ):
            with mock.patch.object(user32, "EnumDesktopWindows", return_value=0):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "EnumDesktopWindows failed",
                ):
                    round10_private_hwnd_probe._capture_cycle(902)

        original_enum_desktop_windows = user32.EnumDesktopWindows
        enumeration_state = {"successful": False, "forced_failures": 0}

        def succeed_once_then_fail(*args):
            if enumeration_state["successful"]:
                enumeration_state["forced_failures"] += 1
                return 0
            result = original_enum_desktop_windows(*args)
            if result:
                enumeration_state["successful"] = True
            return result

        with mock.patch.object(
            round10_private_hwnd_probe,
            "_windows_libraries",
            return_value=(user32, kernel32, dwmapi),
        ):
            with mock.patch.object(
                user32,
                "EnumDesktopWindows",
                side_effect=succeed_once_then_fail,
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "EnumDesktopWindows failed"
                    "|private-desktop observation failed",
                ):
                    round10_private_hwnd_probe._capture_cycle(904)
        self.assertTrue(enumeration_state["successful"])
        self.assertGreater(enumeration_state["forced_failures"], 0)

        completion_failure_state = {
            "complete_observed": False,
            "forced_failures": 0,
        }
        import ctypes
        from ctypes import wintypes
        original_get_window_process_id = user32.GetWindowThreadProcessId

        def identify_foreign_window(hwnd, process_id):
            if int(hwnd) == 1:
                ctypes.cast(
                    process_id,
                    ctypes.POINTER(wintypes.DWORD),
                ).contents.value = os.getpid()
                return 1
            return original_get_window_process_id(hwnd, process_id)

        active_empty_state = {
            "inject_next": False,
            "injected": False,
        }

        def enumerate_active_then_foreign_only(desktop, callback, lparam):
            if active_empty_state["inject_next"]:
                active_empty_state["inject_next"] = False
                active_empty_state["injected"] = True
                callback(1, lparam)
                return 1

            def observe_active_title(hwnd, callback_lparam):
                title = ctypes.create_unicode_buffer(256)
                user32.GetWindowTextW(hwnd, title, len(title))
                if (
                    title.value.startswith("ROUND10|")
                    and title.value
                    not in {"ROUND10|startup", "ROUND10|complete"}
                ):
                    active_empty_state["inject_next"] = True
                return callback(hwnd, callback_lparam)

            observing_callback = ctypes.WINFUNCTYPE(
                wintypes.BOOL,
                wintypes.HWND,
                wintypes.LPARAM,
            )(observe_active_title)
            return original_enum_desktop_windows(
                desktop,
                observing_callback,
                lparam,
            )

        with mock.patch.object(
            round10_private_hwnd_probe,
            "_windows_libraries",
            return_value=(user32, kernel32, dwmapi),
        ):
            with (
                mock.patch.object(
                    user32,
                    "EnumDesktopWindows",
                    side_effect=enumerate_active_then_foreign_only,
                ),
                mock.patch.object(
                    user32,
                    "GetWindowThreadProcessId",
                    side_effect=identify_foreign_window,
                ),
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "target windows missing during active stage",
                ):
                    round10_private_hwnd_probe._capture_cycle(906)
        self.assertTrue(active_empty_state["injected"])

        def enumerate_until_complete_then_fail(desktop, callback, lparam):
            if completion_failure_state["complete_observed"]:
                completion_failure_state["forced_failures"] += 1
                callback(1, lparam)
                return 1

            def observe_title(hwnd, callback_lparam):
                title = ctypes.create_unicode_buffer(256)
                user32.GetWindowTextW(hwnd, title, len(title))
                if title.value == "ROUND10|complete":
                    completion_failure_state["complete_observed"] = True
                return callback(hwnd, callback_lparam)

            observing_callback = ctypes.WINFUNCTYPE(
                wintypes.BOOL,
                wintypes.HWND,
                wintypes.LPARAM,
            )(observe_title)
            return original_enum_desktop_windows(
                desktop,
                observing_callback,
                lparam,
            )

        with mock.patch.object(
            round10_private_hwnd_probe,
            "_windows_libraries",
            return_value=(user32, kernel32, dwmapi),
        ):
            with mock.patch.object(
                user32,
                "EnumDesktopWindows",
                side_effect=enumerate_until_complete_then_fail,
            ), mock.patch.object(
                user32,
                "GetWindowThreadProcessId",
                side_effect=identify_foreign_window,
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "target windows missing during completion",
                ):
                    round10_private_hwnd_probe._capture_cycle(907)
        self.assertTrue(completion_failure_state["complete_observed"])
        self.assertGreater(completion_failure_state["forced_failures"], 0)

        with mock.patch.object(
            round10_private_hwnd_probe,
            "_windows_libraries",
            return_value=(user32, kernel32, dwmapi),
        ):
            with mock.patch.object(
                user32,
                "GetWindowThreadProcessId",
                return_value=0,
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "EnumDesktopWindows failed"
                    "|private-desktop observation failed"
                    "|GetWindowThreadProcessId failed",
                ):
                    round10_private_hwnd_probe._capture_cycle(905)

        original_close_handle = kernel32.CloseHandle
        original_close_desktop = user32.CloseDesktop

        def close_handle_then_fail(handle):
            original_close_handle(handle)
            return 0

        def close_desktop_then_fail(handle):
            original_close_desktop(handle)
            return 0

        with mock.patch.object(
            round10_private_hwnd_probe,
            "_windows_libraries",
            return_value=(user32, kernel32, dwmapi),
        ):
            with (
                mock.patch.object(user32, "EnumDesktopWindows", return_value=0),
                mock.patch.object(
                    kernel32,
                    "CloseHandle",
                    side_effect=close_handle_then_fail,
                ),
                mock.patch.object(
                    user32,
                    "CloseDesktop",
                    side_effect=close_desktop_then_fail,
                ),
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "private-desktop cleanup failed.*CloseHandle.*CloseDesktop",
                ):
                    round10_private_hwnd_probe._capture_cycle(903)

        original_wait_for_single_object = kernel32.WaitForSingleObject
        original_get_exit_code = kernel32.GetExitCodeProcess
        cleanup_wait_state = {"body_complete": False, "injected": False}

        def mark_body_complete(*args):
            result = original_get_exit_code(*args)
            cleanup_wait_state["body_complete"] = True
            return result

        def fail_cleanup_wait_once(*args):
            if (
                cleanup_wait_state["body_complete"]
                and not cleanup_wait_state["injected"]
            ):
                cleanup_wait_state["injected"] = True
                return 0xFFFFFFFF
            return original_wait_for_single_object(*args)

        with mock.patch.object(
            round10_private_hwnd_probe,
            "_windows_libraries",
            return_value=(user32, kernel32, dwmapi),
        ):
            with (
                mock.patch.object(
                    kernel32,
                    "GetExitCodeProcess",
                    side_effect=mark_body_complete,
                ),
                mock.patch.object(
                    kernel32,
                    "WaitForSingleObject",
                    side_effect=fail_cleanup_wait_once,
                ),
                mock.patch.object(kernel32, "TerminateProcess", return_value=1),
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "private-desktop cleanup failed.*WaitForSingleObject",
                ):
                    round10_private_hwnd_probe._capture_cycle(906)
        self.assertTrue(cleanup_wait_state["injected"])

        env = os.environ.copy()
        env["QT_QPA_PLATFORM"] = "windows"
        completed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "validation" / "round10_private_hwnd_probe.py"),
                "--cycles",
                "1",
            ],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(
            0,
            completed.returncode,
            completed.stdout + completed.stderr,
        )
        self.assertEqual("", completed.stderr)
        reports = json.loads(completed.stdout)
        blank_events = [
            event
            for report in reports
            for event in report["visible_events"]
            if event["state"] == "present"
            and event["class"] == "Qt6111QWindowIcon"
            and event["visible"]
            and not event["cloaked"]
            and event["owner"] == 0
            and event["parent"] == 0
            and event["rect"][2] - event["rect"][0] == 136
            and event["rect"][3] - event["rect"][1] == 54
        ]
        self.assertEqual([], blank_events)
        unexpected_ownerless_windows = [
            event
            for report in reports
            for event in report["visible_events"]
            if event["state"] == "present"
            and event["class"].startswith("Qt")
            and event["visible"]
            and not event["cloaked"]
            and event["owner"] == 0
            and event["parent"] == 0
            and event["title"] != "Spectrum Organizer"
            and not event["title"].startswith("ROUND10|")
        ]
        self.assertEqual([], unexpected_ownerless_windows)
        top_level_form_labels = [
            event
            for report in reports
            for event in report["child_result"]["qt_object_events"]
            if event["object_name"] == "dialog_form_label"
            and event["is_window"]
        ]
        self.assertEqual([], top_level_form_labels)
        probe_artifacts_after = (
            {path.name for path in probe_artifact_root.iterdir()}
            if probe_artifact_root.is_dir()
            else set()
        )
        self.assertEqual(probe_artifacts_before, probe_artifacts_after)

    @unittest.skipUnless(os.name == "nt", "native Windows window style only")
    def test_qt_titlebar_is_hidden_during_native_creation(self):
        script = r'''
import ctypes
import os
from ctypes import wintypes

from PySide6 import QtCore, QtWidgets

from spectrum_organizer.ui.dialog_port import (
    _windows_user32,
    apply_styled_dialog_chrome,
)

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
user32 = _windows_user32()
process_id = os.getpid()

seed = QtWidgets.QDialog()
seed.resize(120, 60)
seed.move(-3000, -3000)
seed.show()
app.processEvents()
seed.close()
app.processEvents()

visible_during_create = []
event_callback_type = ctypes.WINFUNCTYPE(
    None,
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.HWND,
    wintypes.LONG,
    wintypes.LONG,
    wintypes.DWORD,
    wintypes.DWORD,
)

@event_callback_type
def observe_helper_create(
    _hook,
    _event,
    hwnd,
    _object_id,
    _child_id,
    _thread_id,
    _event_time,
):
    owner_process_id = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner_process_id))
    class_name = ctypes.create_unicode_buffer(128)
    title = ctypes.create_unicode_buffer(128)
    user32.GetClassNameW(hwnd, class_name, len(class_name))
    user32.GetWindowTextW(hwnd, title, len(title))
    if (
        owner_process_id.value == process_id
        and class_name.value == "_q_titlebar"
        and title.value == "zero-frame-probe"
    ):
        visible_during_create.append(bool(user32.IsWindowVisible(hwnd)))

user32.SetWinEventHook.argtypes = (
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.HMODULE,
    event_callback_type,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
)
user32.SetWinEventHook.restype = wintypes.HANDLE
observer = user32.SetWinEventHook(
    0x8000,
    0x8000,
    0,
    observe_helper_create,
    process_id,
    0,
    0,
)

surface = QtWidgets.QDialog()
apply_styled_dialog_chrome(surface, QtCore)
user32.CreateWindowExW.argtypes = (
    wintypes.DWORD,
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
    wintypes.DWORD,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.HWND,
    wintypes.HMENU,
    wintypes.HINSTANCE,
    wintypes.LPVOID,
)
user32.CreateWindowExW.restype = wintypes.HWND
helper = user32.CreateWindowExW(
    0,
    "_q_titlebar",
    "zero-frame-probe",
    0x10CF0000,
    -3000,
    -3000,
    120,
    39,
    0,
    0,
    0,
    0,
)
app.processEvents()

if helper:
    user32.DestroyWindow(helper)
if observer:
    user32.UnhookWinEvent(observer)
if visible_during_create != [False]:
    raise SystemExit(
        f"_q_titlebar was visible during CREATE: {visible_during_create}"
    )
print("NATIVE_TITLEBAR_CREATE_HIDDEN")
'''
        env = os.environ.copy()
        env["PYTHONPATH"] = str(SRC)
        env["QT_QPA_PLATFORM"] = "windows"
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )

        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        self.assertEqual(
            "NATIVE_TITLEBAR_CREATE_HIDDEN",
            completed.stdout.strip(),
        )

    def test_dialog_catalog_uses_topmost_taskbar_manual_requests(self):
        requests = (
            save_and_close_origin_dialog(),
            output_can_be_inspected_dialog(),
            special_group_confirmation_dialog("Book A"),
            duplicate_emission_dialog(("Book A", "Book B")),
            excitation_selection_dialog(("Ex A",), duplicate_mode="multi"),
            excitation_selection_dialog(("Ex A",), duplicate_mode="single"),
            database_recovery_dialog(
                "locked",
                "C:/Spectrum Organizer/data/sample_library.sqlite3",
                "C:/Spectrum Organizer/data/backups/sample_library_20260716.sqlite3",
            ),
            hidden_origin_confirmation_dialog((1234,)),
            space_retry_cancel_dialog("C:/Temp", required_bytes=10, available_bytes=1),
            output_parent_recovery_dialog("C:/Out", "denied"),
            completion_actions_dialog("C:/Out/Run"),
            cancel_and_exit_confirmation_dialog(),
            cancelled_and_exited_dialog(),
        )

        self.assertEqual(("continue", "cancel"), output_can_be_inspected_dialog().actions)
        self.assertIn("继续运行", cancel_and_exit_confirmation_dialog().actions)
        self.assertIn("取消并退出", cancel_and_exit_confirmation_dialog().actions)
        self.assertEqual(("acknowledge",), cancelled_and_exited_dialog().actions)

        origin_wait = save_and_close_origin_dialog()
        self.assertEqual("请关闭 Origin 后继续", origin_wait.title)
        self.assertEqual(("retry", "cancel"), origin_wait.actions)
        self.assertIn("点击下方“重新检测”", origin_wait.message)
        self.assertIn("任务会停在这里", origin_wait.message)
        self.assertEqual(("select_many", "return_to_attribution"), excitation_selection_dialog(("Ex A",), duplicate_mode="multi").actions)
        self.assertEqual(("select_one", "return_to_attribution"), excitation_selection_dialog(("Ex A",), duplicate_mode="single").actions)
        with self.assertRaises(ValueError):
            excitation_selection_dialog(("Ex A",), duplicate_mode="typo")
        english_fragments = (
            "Final",
            "Sample record",
            "Special spectrum",
            "Cross-source",
            "Output can",
            "Forbidden",
            "confirmation blocked",
            "Duplicate emission",
            "Preflight settings",
            "Excitation selection",
            "Hidden Origin confirmation",
            "Insufficient temporary",
            "Output parent",
            "Completed",
            "Cancel task",
            "Closing this",
            "required=",
            "available=",
        )
        for request in requests:
            with self.subTest(kind=request.kind):
                self.assertTrue(request.topmost)
                self.assertTrue(request.taskbar_visible)
                visible_text = f"{request.title}\n{request.message}"
                for fragment in english_fragments:
                    self.assertNotIn(fragment, visible_text)

    def test_attribution_dialog_displays_shared_forbidden_characters_and_blocks_without_rewriting(self):
        request = attribution_dialog({"sample": "MFL\nSolid"})

        self.assertEqual("attribution", request.kind)
        self.assertIn("样品信息不可输入换行", request.message)
        self.assertNotIn("LF", request.message)
        self.assertNotIn("CR", request.message)
        self.assertIn("sample: MFL", request.message)
        self.assertIn("确认被阻止", request.message)
        self.assertEqual("MFL\nSolid", request.field_values["sample"])
        self.assertFalse(request.can_confirm)

        too_long = attribution_dialog({"sample": "X" * 256})
        self.assertFalse(too_long.can_confirm)

    def test_final_attribution_summary_dialog_exposes_output_plan_and_return_action(self):
        request = final_attribution_summary_dialog(
            (
                FinalReviewRow(
                    row_id="book-1",
                    source_filename="source.opju",
                    folder_path="Emission",
                    book_name="Em270",
                    attribution="MFL-film-298 K",
                    result="将写入输出计划",
                    has_related_conflicts=True,
                ),
                FinalReviewRow(
                    row_id="book-2",
                    source_filename="source.opju",
                    folder_path="Emission",
                    book_name="Em300",
                    attribution="PFL-film-77 K",
                    result="不输出：用户未选择",
                ),
            ),
            recognized_count=2,
            rejected_count=0,
            excluded_count=1,
            accepted_count=1,
            output_folders=(
                FinalReviewOutputFolder(
                    folder_name="F_Ex270",
                    books=(
                        FinalReviewOutputBook(
                            book_name="MFL-film-298 K",
                            column_order=(
                                "列 1 [X] · Comment=Em",
                                "列 2 [Raw Y] · Comment=Em270",
                            ),
                        ),
                    ),
                    missing_items=("PFL-film-77 K",),
                ),
            ),
        )

        self.assertIsInstance(request, FinalReviewDialogRequest)
        self.assertEqual("final_attribution_summary", request.kind)
        self.assertEqual(
            (
                "confirm",
                "modify_attribution",
                "modify_conflicts",
                "cancel",
            ),
            request.actions,
        )
        self.assertFalse(request.topmost)
        self.assertTrue(request.taskbar_visible)
        self.assertEqual((2, 0, 1, 1), request.counts)
        self.assertEqual("book-1", request.rows[0].row_id)
        self.assertTrue(request.rows[0].has_related_conflicts)
        self.assertEqual(
            ("列 1 [X] · Comment=Em", "列 2 [Raw Y] · Comment=Em270"),
            request.output_folders[0].books[0].column_order,
        )
        self.assertEqual(
            ("PFL-film-77 K",),
            request.output_folders[0].missing_items,
        )

    def test_batch_write_failure_dialog_offers_retry_or_cancel_without_advancing(self):
        request = batch_write_failure_dialog("database locked")

        self.assertEqual("sample_record_commit_failed", request.kind)
        self.assertEqual(("retry", "cancel"), request.actions)
        self.assertIn("database locked", request.message)

    def test_no_pause_action_is_exposed_by_main_contract_or_dialogs(self):
        contract = build_main_window_contract()
        dialogs = (
            cancel_and_exit_confirmation_dialog(),
            completion_actions_dialog("C:/Out/Run"),
            final_attribution_summary_dialog(
                (
                    FinalReviewRow(
                        row_id="book-1",
                        source_filename="source.opju",
                        folder_path="Emission",
                        book_name="Em270",
                        attribution="MFL",
                        result="将写入输出计划",
                    ),
                ),
                recognized_count=1,
                rejected_count=0,
                excluded_count=0,
                accepted_count=1,
            ),
        )

        self.assertNotIn("pause", contract.available_actions)
        for request in dialogs:
            self.assertNotIn("pause", request.actions)


if __name__ == "__main__":
    unittest.main()
