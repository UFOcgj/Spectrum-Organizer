import os
import pathlib
import subprocess
import sys
import unittest
from dataclasses import replace
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from spectrum_organizer.ui import dialog_port as dialog_port_module
from spectrum_organizer.ui.dialog_port import (
    AttributionBookSelectionRequest,
    AttributionBookSelectionResponse,
    AttributionDialogRequest,
    AttributionDialogResponse,
    ConflictReviewChoice,
    ConflictReviewRequest,
    ConflictReviewResponse,
    DialogResponse,
    QtAttributionDialogPort,
    QtConflictReviewDialogPort,
    QtManualDialogPort,
)
from spectrum_organizer.ui.dialogs import (
    DialogRequest,
    FinalReviewConflictChoice,
    FinalReviewConflictEditor,
    FinalReviewConflictGroup,
    FinalReviewConflictSelection,
    database_recovery_dialog,
)


def _run_qt_script(script, *, scale_factor=None):
    env = os.environ.copy()
    env["QT_QPA_PLATFORM"] = "offscreen"
    if scale_factor is not None:
        env["QT_SCALE_FACTOR"] = scale_factor
    env["PYTHONPATH"] = str(SRC)
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )


class DialogPortTests(unittest.TestCase):
    def test_grouped_conflict_primary_action_names_first_confirmation_and_later_edit(self):
        script = r'''
import json
from PySide6 import QtCore, QtWidgets
from spectrum_organizer.ui.dialog_port import (
    ConflictReviewChoice,
    ConflictReviewGroup,
    ConflictReviewRequest,
    show_conflict_review_dialog,
)

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
observed = []

def run_dialog(*, editing):
    def inspect():
        dialog = next(
            widget
            for widget in app.topLevelWidgets()
            if isinstance(widget, QtWidgets.QDialog) and widget.isVisible()
        )
        observed.append(
            [button.text() for button in dialog.findChildren(QtWidgets.QPushButton)]
        )
        dialog.reject()

    QtCore.QTimer.singleShot(40, inspect)
    show_conflict_review_dialog(
        ConflictReviewRequest(
            kind="special_conflict_batch",
            title=("修改相关冲突" if editing else "确认相关冲突"),
            instruction="请为每个冲突保留一个选择。",
            choices=(),
            selection_mode="grouped_single",
            actions=("confirm_all_conflicts", "cancel"),
            choice_groups=(
                ConflictReviewGroup(
                    "group-a",
                    (
                        ConflictReviewChoice("book-a", "Book A"),
                        ConflictReviewChoice("book-b", "Book B"),
                    ),
                    "book-a",
                    (),
                ),
            ),
            editing_existing_decisions=editing,
        )
    )

run_dialog(editing=False)
run_dialog(editing=True)
print(json.dumps(observed, ensure_ascii=False))
'''
        completed = _run_qt_script(script)

        self.assertEqual(
            0,
            completed.returncode,
            completed.stdout + completed.stderr,
        )
        first_buttons, edit_buttons = __import__("json").loads(
            completed.stdout.strip()
        )
        self.assertIn("确认全部选择", first_buttons)
        self.assertNotIn("确认全部修改", first_buttons)
        self.assertIn("确认全部修改", edit_buttons)

    def test_final_review_output_resize_coalesces_expensive_layout_and_keeps_heartbeat_alive(self):
        script = r'''
import json
import time
from PySide6 import QtCore, QtTest, QtWidgets
from spectrum_organizer.ui.dialog_port import show_styled_dialog
from spectrum_organizer.ui.dialogs import (
    FinalReviewOutputBook,
    FinalReviewOutputFolder,
    FinalReviewRow,
    FinalReviewViewState,
    final_attribution_summary_dialog,
)

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
main = QtWidgets.QMainWindow()
main.show()
observed = {}
heartbeat = {"ticks": 0}
timer = QtCore.QTimer(main)
timer.setInterval(5)
timer.timeout.connect(lambda: heartbeat.__setitem__("ticks", heartbeat["ticks"] + 1))
timer.start()

folders = tuple(
    FinalReviewOutputFolder(
        f"F_Ex{230 + folder}_ExSlit2_EmSlit2",
        tuple(
            FinalReviewOutputBook(
                f"Sample-{folder}-{book}-1×10^-4 M-298 K",
                tuple(
                    f"列 {column} [原始 Y] · Comment=Sample-{folder}-{book}-F270_Norm · Method=Divided by Max"
                    for column in range(1, 11)
                ),
            )
            for book in range(8)
        ),
        (f"Missing-{folder}-1×10^-4 M-77 K",),
    )
    for folder in range(8)
)

def start_resize():
    observed["first_event_elapsed"] = time.perf_counter() - started
    dialog = app.activeModalWidget()
    tabs = dialog.findChild(QtWidgets.QTabWidget, "final_review_tabs")
    tabs.setCurrentIndex(1)
    tree = dialog.findChild(QtWidgets.QTreeWidget, "final_review_output_tree")
    grip = dialog.findChild(QtWidgets.QSizeGrip, "final_review_size_grip")
    original_layout = tree.doItemsLayout
    observed["layout_calls"] = 0

    def recording_layout():
        observed["layout_calls"] += 1
        original_layout()

    tree.doItemsLayout = recording_layout
    observed["baseline_ticks"] = heartbeat["ticks"]
    QtTest.QTest.mousePress(
        grip,
        QtCore.Qt.MouseButton.LeftButton,
        pos=QtCore.QPoint(grip.width() - 2, grip.height() - 2),
    )
    sizes = tuple(
        (dialog.width() - 30 + (index % 3) * 15, dialog.height() - 20 + (index % 2) * 20)
        for index in range(18)
    )

    def resize_one(index=0):
        if index == len(sizes):
            QtTest.QTest.mouseRelease(grip, QtCore.Qt.MouseButton.LeftButton)
            QtCore.QTimer.singleShot(180, finish)
            return
        dialog.resize(*sizes[index])
        QtCore.QTimer.singleShot(6, lambda: resize_one(index + 1))

    resize_one()

def finish():
    dialog = app.activeModalWidget()
    tree = dialog.findChild(QtWidgets.QTreeWidget, "final_review_output_tree")
    observed["heartbeat_delta"] = heartbeat["ticks"] - observed["baseline_ticks"]
    observed["horizontal_range"] = tree.horizontalScrollBar().maximum()
    observed["folder_text"] = tree.topLevelItem(0).text(0)
    dialog.reject()

started = time.perf_counter()
QtCore.QTimer.singleShot(100, start_resize)
show_styled_dialog(
    final_attribution_summary_dialog(
        (
            FinalReviewRow(
                "book-a", "source.opju", "Folder", "Book A",
                "Sample-1×10^-4 M-298 K", "将写入输出计划", True,
            ),
        ),
        recognized_count=64,
        rejected_count=0,
        excluded_count=0,
        accepted_count=64,
        output_folders=folders,
        initial_view_state=FinalReviewViewState(active_tab="output"),
    ),
    parent=main,
)
timer.stop()
main.close()
print(json.dumps(observed, ensure_ascii=False))
'''
        completed = _run_qt_script(script)

        self.assertEqual(
            0,
            completed.returncode,
            completed.stdout + completed.stderr,
        )
        observed = __import__("json").loads(completed.stdout.strip())
        self.assertLess(observed["first_event_elapsed"], 0.8, observed)
        self.assertLessEqual(observed["layout_calls"], 3, observed)
        self.assertGreaterEqual(observed["heartbeat_delta"], 20, observed)
        self.assertEqual(0, observed["horizontal_range"])
        self.assertIn("F_Ex230_ExSlit2_EmSlit2", observed["folder_text"])

    def test_final_review_conflict_width_tracks_content_instead_of_fixed_screen_ratio(self):
        target_width = dialog_port_module._final_review_conflict_target_width

        self.assertEqual(720, target_width(2048, 640))
        self.assertEqual(980, target_width(2048, 980))
        self.assertEqual(1120, target_width(2048, 1400))
        self.assertEqual(528, target_width(800, 680))
        self.assertEqual(528, target_width(800, 900))

    def test_conflict_review_port_preserves_structured_candidates_and_response(self):
        captured = []
        expected = ConflictReviewResponse(
            action="confirm_selection",
            selected_book_keys=("book-b",),
        )
        port = QtConflictReviewDialogPort(
            runner=lambda request, parent: captured.append((request, parent)) or expected
        )
        request = ConflictReviewRequest(
            kind="emission_duplicate",
            title="选择重复发射谱",
            instruction="必须保留一条。",
            choices=(
                ConflictReviewChoice(
                    book_key="book-a",
                    display_name="A",
                    fields=(("来源文件", "source.opj"),),
                ),
                ConflictReviewChoice(
                    book_key="book-b",
                    display_name="B",
                    fields=(("来源文件", "source.opj"),),
                ),
            ),
            selection_mode="single",
        )

        response = port.choose(request, parent="owner")

        self.assertEqual(expected, response)
        self.assertEqual([(request, "owner")], captured)

    def test_conflict_review_inner_surface_boundary_is_rectangular_when_rendered(self):
        script = r'''
import json
from PySide6 import QtCore, QtWidgets
from spectrum_organizer.ui.dialog_port import (
    ConflictReviewChoice,
    ConflictReviewRequest,
    show_conflict_review_dialog,
)

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
observed = {}

def inspect():
    dialog = next(
        widget
        for widget in app.topLevelWidgets()
        if isinstance(widget, QtWidgets.QDialog) and widget.isVisible()
    )
    body = dialog.findChild(QtWidgets.QFrame, "dialog_body")
    footer = dialog.findChild(QtWidgets.QFrame, "conflict_review_footer")
    app.processEvents()
    image = dialog.grab().toImage()
    body_rect = QtCore.QRect(
        body.mapTo(dialog, QtCore.QPoint(0, 0)),
        body.size(),
    )
    footer_rect = QtCore.QRect(
        footer.mapTo(dialog, QtCore.QPoint(0, 0)),
        footer.size(),
    )
    boundary_y = footer_rect.top()
    sample_x = (2, dialog.width() // 2, dialog.width() - 3)
    observed["body_full_width"] = (
        body_rect.left() == 0 and body_rect.right() == dialog.width() - 1
    )
    observed["footer_full_width"] = (
        footer_rect.left() == 0 and footer_rect.right() == dialog.width() - 1
    )
    observed["surfaces_touch"] = body_rect.bottom() + 1 == footer_rect.top()
    observed["boundary_colors"] = [
        image.pixelColor(x, boundary_y).name().lower()
        for x in sample_x
    ]
    dialog.reject()

QtCore.QTimer.singleShot(100, inspect)
show_conflict_review_dialog(
    ConflictReviewRequest(
        kind="duplicate_emission",
        title="选择重复发射谱",
        instruction="必须保留一条。",
        choices=(
            ConflictReviewChoice("a", "A", (("峰值", "1,000"),)),
            ConflictReviewChoice("b", "B", (("峰值", "2,000"),)),
        ),
        selection_mode="single",
    )
)
print(json.dumps(observed))
'''
        completed = _run_qt_script(script)

        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        observed = __import__("json").loads(completed.stdout.strip())
        self.assertTrue(observed["body_full_width"])
        self.assertTrue(observed["footer_full_width"])
        self.assertTrue(observed["surfaces_touch"])
        self.assertEqual(
            ["#d6dfdc", "#d6dfdc", "#d6dfdc"],
            observed["boundary_colors"],
        )

    def test_excitation_multi_popup_toggles_rows_without_ctrl_and_replaces_exact_choice(self):
        script = r'''
import json
from PySide6 import QtCore, QtTest, QtWidgets
from spectrum_organizer.ui.dialog_port import (
    ConflictReviewChoice,
    ConflictReviewRequest,
    show_conflict_review_dialog,
)

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
observed = {}

def interact():
    dialog = next(
        widget
        for widget in app.topLevelWidgets()
        if isinstance(widget, QtWidgets.QDialog) and widget.isVisible()
    )
    tree = dialog.findChild(QtWidgets.QTreeWidget, "conflict_review_candidates")
    confirm = next(
        button
        for button in dialog.findChildren(QtWidgets.QPushButton)
        if button.text() == "确认选择"
    )
    observed["initial"] = sorted(
        item.data(0, QtCore.Qt.ItemDataRole.UserRole)
        for item in tree.selectedItems()
    )
    row = tree.visualItemRect(tree.topLevelItem(1))
    QtTest.QTest.mouseClick(
        tree.viewport(),
        QtCore.Qt.MouseButton.LeftButton,
        QtCore.Qt.KeyboardModifier.NoModifier,
        row.center(),
    )
    app.processEvents()
    observed["after_click"] = sorted(
        item.data(0, QtCore.Qt.ItemDataRole.UserRole)
        for item in tree.selectedItems()
    )
    confirm.click()

QtCore.QTimer.singleShot(0, interact)
response = show_conflict_review_dialog(
    ConflictReviewRequest(
        kind="excitation_selection",
        title="选择激发谱",
        instruction="至少保留一个；完全重复组只能保留一个。",
        choices=(
            ConflictReviewChoice("a", "A", (("固定发射波长", "450"),)),
            ConflictReviewChoice("b", "B", (("固定发射波长", "450"),)),
            ConflictReviewChoice("c", "C", (("固定发射波长", "460"),)),
        ),
        selection_mode="multi",
        single_select_groups=(("a", "b"),),
        initial_selection=("a", "c"),
    )
)
observed["action"] = response.action
observed["selected"] = list(response.selected_book_keys)
print(json.dumps(observed))
'''
        completed = _run_qt_script(script)

        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        observed = __import__("json").loads(completed.stdout.strip())
        self.assertEqual(["a", "c"], observed["initial"])
        self.assertEqual(["b", "c"], observed["after_click"])
        self.assertEqual("confirm_selection", observed["action"])
        self.assertEqual(["c", "b"], observed["selected"])

    def test_excitation_multi_popup_can_clear_an_exact_group_when_another_candidate_remains(self):
        script = r'''
import json
from PySide6 import QtCore, QtTest, QtWidgets
from spectrum_organizer.ui.dialog_port import (
    ConflictReviewChoice,
    ConflictReviewRequest,
    show_conflict_review_dialog,
)

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
observed = {}

def interact():
    dialog = next(
        widget
        for widget in app.topLevelWidgets()
        if isinstance(widget, QtWidgets.QDialog) and widget.isVisible()
    )
    tree = dialog.findChild(QtWidgets.QTreeWidget, "conflict_review_candidates")
    confirm = next(
        button
        for button in dialog.findChildren(QtWidgets.QPushButton)
        if button.text() == "确认选择"
    )
    exact_row = tree.visualItemRect(tree.topLevelItem(0))
    QtTest.QTest.mouseClick(
        tree.viewport(),
        QtCore.Qt.MouseButton.LeftButton,
        QtCore.Qt.KeyboardModifier.NoModifier,
        exact_row.center(),
    )
    app.processEvents()
    observed["after_clear"] = sorted(
        item.data(0, QtCore.Qt.ItemDataRole.UserRole)
        for item in tree.selectedItems()
    )
    confirm.click()
    QtCore.QTimer.singleShot(
        100,
        lambda: dialog.reject() if dialog.isVisible() else None,
    )

QtCore.QTimer.singleShot(0, interact)
response = show_conflict_review_dialog(
    ConflictReviewRequest(
        kind="excitation_selection",
        title="选择激发谱",
        instruction="至少保留一个；完全重复组最多保留一个。",
        choices=(
            ConflictReviewChoice("a", "A", (("固定发射波长", "450"),)),
            ConflictReviewChoice("b", "B", (("固定发射波长", "450"),)),
            ConflictReviewChoice("c", "C", (("固定发射波长", "460"),)),
        ),
        selection_mode="multi",
        single_select_groups=(("a", "b"),),
        initial_selection=("a", "c"),
    )
)
observed["action"] = response.action
observed["selected"] = list(response.selected_book_keys)
print(json.dumps(observed))
'''
        completed = _run_qt_script(script)

        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        observed = __import__("json").loads(completed.stdout.strip())
        self.assertEqual(["c"], observed["after_clear"])
        self.assertEqual("confirm_selection", observed["action"])
        self.assertEqual(["c"], observed["selected"])

    def test_excitation_multi_popup_returns_user_click_chronology(self):
        script = r'''
import json
from PySide6 import QtCore, QtTest, QtWidgets
from spectrum_organizer.ui.dialog_port import (
    ConflictReviewChoice,
    ConflictReviewRequest,
    show_conflict_review_dialog,
)

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

def interact():
    dialog = next(
        widget
        for widget in app.topLevelWidgets()
        if isinstance(widget, QtWidgets.QDialog) and widget.isVisible()
    )
    tree = dialog.findChild(QtWidgets.QTreeWidget, "conflict_review_candidates")
    for index in (1, 0):
        row = tree.visualItemRect(tree.topLevelItem(index))
        QtTest.QTest.mouseClick(
            tree.viewport(),
            QtCore.Qt.MouseButton.LeftButton,
            QtCore.Qt.KeyboardModifier.NoModifier,
            row.center(),
        )
        app.processEvents()
    next(
        button
        for button in dialog.findChildren(QtWidgets.QPushButton)
        if button.text() == "确认选择"
    ).click()

QtCore.QTimer.singleShot(0, interact)
response = show_conflict_review_dialog(
    ConflictReviewRequest(
        kind="excitation_selection",
        title="选择激发谱",
        instruction="至少保留一个。",
        choices=(
            ConflictReviewChoice("a", "A"),
            ConflictReviewChoice("b", "B"),
            ConflictReviewChoice("c", "C"),
        ),
        selection_mode="multi",
    )
)
print(json.dumps(list(response.selected_book_keys)))
'''
        completed = _run_qt_script(script)

        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        self.assertEqual(
            ["b", "a"],
            __import__("json").loads(completed.stdout.strip()),
        )

    def test_exact_duplicate_excitation_rows_state_their_zero_or_one_rule(self):
        script = r'''
import json
from PySide6 import QtCore, QtTest, QtWidgets
from spectrum_organizer.ui.dialog_port import (
    ConflictReviewChoice,
    ConflictReviewRequest,
    show_conflict_review_dialog,
)

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
observed = {}

def inspect():
    dialog = next(
        widget
        for widget in app.topLevelWidgets()
        if isinstance(widget, QtWidgets.QDialog) and widget.isVisible()
    )
    tree = dialog.findChild(QtWidgets.QTreeWidget, "conflict_review_candidates")
    observed["conditions"] = [
        tree.topLevelItem(index).text(1)
        for index in range(tree.topLevelItemCount())
    ]
    observed["names"] = [
        tree.topLevelItem(index).text(0)
        for index in range(tree.topLevelItemCount())
    ]
    next(
        button
        for button in dialog.findChildren(QtWidgets.QPushButton)
        if button.text() == "取消并退出"
    ).click()

QtCore.QTimer.singleShot(100, inspect)
show_conflict_review_dialog(
    ConflictReviewRequest(
        kind="excitation_selection",
        title="选择激发谱",
        instruction="选择候选。",
        choices=(
            ConflictReviewChoice("a", "A", (("固定发射波长", "450"),)),
            ConflictReviewChoice("b", "B", (("固定发射波长", "450"),)),
            ConflictReviewChoice("c", "C", (("固定发射波长", "460"),)),
        ),
        selection_mode="multi",
        single_select_groups=(("a", "b"),),
        initial_selection=("a", "c"),
    )
)
print(json.dumps(observed, ensure_ascii=False))
'''
        completed = _run_qt_script(script)

        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        observed = __import__("json").loads(completed.stdout.strip())
        self.assertNotIn("本组最多选择 1 个", observed["conditions"][0])
        self.assertNotIn("本组最多选择 1 个", observed["conditions"][1])
        self.assertNotIn("本组最多选择 1 个", observed["conditions"][2])
        self.assertTrue(observed["names"][0].startswith("[完全重复组 1] "))
        self.assertTrue(observed["names"][1].startswith("[完全重复组 1] "))
        self.assertNotIn("本组最多选择 1 个", observed["names"][0])
        self.assertNotIn("本组最多选择 1 个", observed["names"][1])
        self.assertNotIn("本组最多选择 1 个", observed["names"][2])

    def test_return_to_group_action_uses_explicit_whole_group_label(self):
        script = r'''
import json
from PySide6 import QtCore, QtWidgets
from spectrum_organizer.ui.dialog_port import (
    ConflictReviewChoice,
    ConflictReviewRequest,
    show_conflict_review_dialog,
)

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
observed = {}

def inspect():
    dialog = next(
        widget
        for widget in app.topLevelWidgets()
        if isinstance(widget, QtWidgets.QDialog) and widget.isVisible()
    )
    observed["buttons"] = [
        button.text()
        for button in dialog.findChildren(QtWidgets.QPushButton)
        if button.isVisible()
    ]
    next(
        button
        for button in dialog.findChildren(QtWidgets.QPushButton)
        if button.text() == "取消并退出"
    ).click()

QtCore.QTimer.singleShot(100, inspect)
show_conflict_review_dialog(
    ConflictReviewRequest(
        kind="special_group_books",
        title="逐 Book 确认特殊谱组",
        instruction="确认归属。",
        choices=(
            ConflictReviewChoice("a", "A", (("固定激发波长", "300"),)),
            ConflictReviewChoice("b", "B", (("固定激发波长", "305"),)),
        ),
        selection_mode="multi",
        actions=("return_to_group", "confirm_selection", "cancel"),
    )
)
print(json.dumps(observed, ensure_ascii=False))
'''
        completed = _run_qt_script(script)

        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        buttons = __import__("json").loads(completed.stdout.strip())["buttons"]
        self.assertIn("返回整组确认", buttons)
        self.assertNotIn("返回上一步", buttons)

    def test_conflict_review_table_distinguishes_selected_hovered_and_neutral_rows(self):
        script = r'''
from PySide6 import QtCore, QtTest, QtWidgets
from spectrum_organizer.ui.dialog_port import (
    ConflictReviewChoice,
    ConflictReviewRequest,
    show_conflict_review_dialog,
)

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

def inspect():
    dialog = next(
        widget
        for widget in app.topLevelWidgets()
        if isinstance(widget, QtWidgets.QDialog) and widget.isVisible()
    )
    tree = dialog.findChild(QtWidgets.QTreeWidget, "conflict_review_candidates")
    tree.topLevelItem(0).setSelected(True)
    app.processEvents()
    hovered = tree.visualItemRect(tree.topLevelItem(1))
    QtTest.QTest.mouseMove(tree.viewport(), hovered.center())
    QtTest.QTest.qWait(50)
    image = tree.viewport().grab().toImage()

    def row_color(row):
        rect = tree.visualItemRect(tree.topLevelItem(row))
        return image.pixelColor(rect.right() - 12, rect.center().y()).name().lower()

    print(f"SELECTED={row_color(0)}", flush=True)
    print(f"HOVERED={row_color(1)}", flush=True)
    print(f"NEUTRAL={row_color(2)}", flush=True)
    print(f"NEUTRAL_ODD={row_color(3)}", flush=True)
    dialog.reject()

QtCore.QTimer.singleShot(100, inspect)
show_conflict_review_dialog(
    ConflictReviewRequest(
        kind="excitation_selection",
        title="选择激发谱",
        instruction="选择候选。",
        choices=(
            ConflictReviewChoice("a", "A", (("条件", "450"),)),
            ConflictReviewChoice("b", "B", (("条件", "460"),)),
            ConflictReviewChoice("c", "C", (("条件", "470"),)),
            ConflictReviewChoice("d", "D", (("条件", "480"),)),
        ),
        selection_mode="multi",
    )
)
'''
        completed = _run_qt_script(script)

        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        self.assertIn("SELECTED=#147a6c", completed.stdout)
        self.assertIn("HOVERED=#dcebe7", completed.stdout)
        self.assertIn("NEUTRAL=#ffffff", completed.stdout)
        self.assertIn("NEUTRAL_ODD=#ffffff", completed.stdout)

    def test_conflict_review_selected_and_hovered_rows_share_content_bounds_and_text_center(self):
        script = r'''
import json
from PySide6 import QtCore, QtTest, QtWidgets
from spectrum_organizer.ui.dialog_port import (
    ConflictReviewChoice,
    ConflictReviewRequest,
    show_conflict_review_dialog,
)

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
observed = {}

def inspect():
    dialog = next(
        widget
        for widget in app.topLevelWidgets()
        if isinstance(widget, QtWidgets.QDialog) and widget.isVisible()
    )
    tree = dialog.findChild(QtWidgets.QTreeWidget, "conflict_review_candidates")
    selected_item = tree.topLevelItem(0)
    hovered_item = tree.topLevelItem(1)
    tree.clearSelection()
    tree.setCurrentItem(selected_item)
    selected_item.setSelected(True)
    app.processEvents()
    selected_rect = tree.visualItemRect(selected_item)
    hovered_rect = tree.visualItemRect(hovered_item)
    QtTest.QTest.mouseMove(tree.viewport(), hovered_rect.center())
    QtTest.QTest.qWait(50)
    image = tree.viewport().grab().toImage()
    sample_x = tree.viewport().width() - 18

    def colored_offsets(rect, color):
        return [
            offset
            for offset in range(rect.height())
            if image.pixelColor(sample_x, rect.top() + offset).name().lower()
            == color
        ]

    def ink_offsets(rect, selected, background_offsets):
        x_start = tree.columnViewportPosition(1) + 12
        x_stop = min(tree.viewport().width() - 4, x_start + 330)
        offsets = []
        for offset in background_offsets:
            y = rect.top() + offset
            for x in range(x_start, x_stop):
                color = image.pixelColor(x, y)
                if selected:
                    is_ink = (
                        color.red() > 210
                        and color.green() > 210
                        and color.blue() > 210
                    )
                else:
                    is_ink = (
                        color.red() < 95
                        and color.green() < 105
                        and color.blue() < 105
                    )
                if is_ink:
                    offsets.append(offset)
                    break
        return offsets

    selected_background = colored_offsets(selected_rect, "#147a6c")
    hovered_background = colored_offsets(hovered_rect, "#dcebe7")
    selected_ink = ink_offsets(selected_rect, True, selected_background)
    hovered_ink = ink_offsets(hovered_rect, False, hovered_background)
    expected_content = list(range(selected_rect.height()))
    observed.update(
        {
            "selected_background": selected_background,
            "hovered_background": hovered_background,
            "expected_content": expected_content,
            "selected_ink": selected_ink,
            "hovered_ink": hovered_ink,
            "selected_center_delta": abs(
                (selected_background[0] + selected_background[-1]) / 2
                - (selected_ink[0] + selected_ink[-1]) / 2
            ),
            "hovered_center_delta": abs(
                (hovered_background[0] + hovered_background[-1]) / 2
                - (hovered_ink[0] + hovered_ink[-1]) / 2
            ),
        }
    )
    dialog.reject()

QtCore.QTimer.singleShot(100, inspect)
show_conflict_review_dialog(
    ConflictReviewRequest(
        kind="special_group_books",
        title="确认特殊谱组",
        instruction="请选择候选。",
        choices=(
            ConflictReviewChoice(
                "book-a",
                "300",
                (("峰值", "X=458.0，Y=144,652.65"),),
            ),
            ConflictReviewChoice(
                "book-b",
                "Pho300_10_10_Re",
                (("峰值", "X=459.0，Y=36,313.95"),),
            ),
        ),
        selection_mode="multi",
    )
)
print(json.dumps(observed, ensure_ascii=False))
'''
        completed = _run_qt_script(script)

        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        observed = __import__("json").loads(completed.stdout.strip())
        self.assertEqual(
            observed["expected_content"],
            observed["selected_background"],
            observed,
        )
        self.assertEqual(
            observed["expected_content"],
            observed["hovered_background"],
            observed,
        )
        self.assertEqual(
            observed["selected_ink"],
            observed["hovered_ink"],
            observed,
        )
        self.assertLessEqual(observed["selected_center_delta"], 4.0, observed)
        self.assertLessEqual(observed["hovered_center_delta"], 4.0, observed)

    def test_conflict_review_dialog_uses_wrapped_differences_common_detail_and_fixed_footer(self):
        script = r'''
import json
from PySide6 import QtCore, QtWidgets
from spectrum_organizer.ui.dialog_port import (
    ConflictReviewChoice,
    ConflictReviewRequest,
    show_conflict_review_dialog,
)

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
observed = {}

def interact():
    dialog = next(
        widget
        for widget in app.topLevelWidgets()
        if isinstance(widget, QtWidgets.QDialog) and widget.isVisible()
    )
    tree = dialog.findChild(QtWidgets.QTreeWidget, "conflict_review_candidates")
    detail = dialog.findChild(QtWidgets.QFrame, "conflict_review_detail")
    footer = dialog.findChild(QtWidgets.QFrame, "conflict_review_footer")
    tree.topLevelItem(0).setSelected(True)
    tree.setCurrentItem(tree.topLevelItem(0))
    app.processEvents()
    observed["headers"] = [
        tree.headerItem().text(column)
        for column in range(tree.columnCount())
    ]
    observed["row_text"] = [
        tree.topLevelItem(index).text(1)
        for index in range(tree.topLevelItemCount())
    ]
    observed["row_heights"] = [
        tree.visualItemRect(tree.topLevelItem(index)).height()
        for index in range(tree.topLevelItemCount())
    ]
    observed["word_wrap"] = tree.wordWrap()
    observed["elide_none"] = (
        tree.textElideMode() == QtCore.Qt.TextElideMode.ElideNone
    )
    observed["detail_visible"] = detail is not None and detail.isVisible()
    observed["detail_title"] = next(
        label.text()
        for label in detail.findChildren(QtWidgets.QLabel)
        if label.objectName() == "conflict_review_detail_title"
    )
    observed["detail_text"] = "\n".join(
        label.text()
        for label in detail.findChildren(QtWidgets.QLabel)
        if label.objectName() == "conflict_review_detail_value"
    )
    observed["footer_visible"] = (
        footer is not None
        and footer.isVisible()
        and dialog.rect().contains(
            QtCore.QRect(
                footer.mapTo(dialog, QtCore.QPoint(0, 0)),
                footer.size(),
            )
        )
    )
    next(
        button
        for button in dialog.findChildren(QtWidgets.QPushButton)
        if button.text() == "确认选择"
    ).click()

choices = tuple(
    ConflictReviewChoice(
        f"book-{index}",
        f"Book {index}",
        (
            ("来源文件", "20241209_MFL_2DPho.opj"),
            ("Folder", "MeFL_mTHF"),
            ("谱图类型", "二维延迟谱"),
            ("固定激发波长", str(300 + index * 5)),
            ("扫描范围", "350.00 – 650.00"),
            ("扫描步长", "1.00"),
            ("狭缝", "Ex 10.00/10.00 / Em 10.00/10.00"),
            ("延迟参数", "Flash Delay 10.00; Sample Window 20.00; Time per Flash 55.00; Flash Count 4"),
            ("峰值", f"X={458 + index}.0，Y={14652.65 + index:.2f}"),
            ("Note 时间", "2026-07-27 11:00"),
        ),
    )
    for index in range(5)
)

QtCore.QTimer.singleShot(0, interact)
response = show_conflict_review_dialog(
    ConflictReviewRequest(
        kind="emission_duplicate",
        title="选择特殊组重复点",
        instruction="这一步决定重复测量中保留哪一个 Book。",
        choices=choices,
        selection_mode="single",
    )
)
observed["action"] = response.action
print(json.dumps(observed, ensure_ascii=False))
'''
        completed = _run_qt_script(script)

        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        observed = __import__("json").loads(completed.stdout.strip())
        self.assertEqual(
            ["Book", "关键差异"],
            observed["headers"],
        )
        self.assertFalse(observed["word_wrap"])
        self.assertTrue(observed["elide_none"])
        self.assertTrue(
            all(height >= 36 for height in observed["row_heights"]),
            observed["row_heights"],
        )
        self.assertTrue(
            all("固定激发波长" in text and "峰值" in text for text in observed["row_text"]),
            observed["row_text"],
        )
        self.assertTrue(all("…" not in text for text in observed["row_text"]))
        self.assertTrue(all("来源文件" not in text for text in observed["row_text"]))
        self.assertTrue(all("Folder" not in text for text in observed["row_text"]))
        self.assertTrue(observed["detail_visible"])
        self.assertEqual("共同条件", observed["detail_title"])
        self.assertIn("来源文件：20241209_MFL_2DPho.opj", observed["detail_text"])
        self.assertIn("Folder：MeFL_mTHF", observed["detail_text"])
        self.assertIn("扫描步长：1.00", observed["detail_text"])
        self.assertIn("Flash Delay 10.00", observed["detail_text"])
        self.assertNotIn("固定激发波长", observed["detail_text"])
        self.assertNotIn("峰值", observed["detail_text"])
        self.assertTrue(observed["footer_visible"])
        self.assertEqual("confirm_selection", observed["action"])

    def test_conflict_review_rows_show_complete_differing_folder_without_elision(self):
        script = r'''
import json
from PySide6 import QtCore, QtWidgets
from spectrum_organizer.ui.dialog_port import (
    ConflictReviewChoice,
    ConflictReviewRequest,
    show_conflict_review_dialog,
)

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
observed = {}
folders = (
    "DiMeFL_EID_mTHF_with_a_long_distinguishing_suffix_A",
    "DiMeFL_EID_mTHF_with_a_long_distinguishing_suffix_B",
)

def inspect():
    dialog = next(
        widget
        for widget in app.topLevelWidgets()
        if isinstance(widget, QtWidgets.QDialog) and widget.isVisible()
    )
    tree = dialog.findChild(QtWidgets.QTreeWidget, "conflict_review_candidates")
    app.processEvents()
    observed["rows"] = [
        tree.topLevelItem(index).text(1)
        for index in range(tree.topLevelItemCount())
    ]
    observed["elide_none"] = (
        tree.textElideMode() == QtCore.Qt.TextElideMode.ElideNone
    )
    observed["heights"] = [
        tree.visualItemRect(tree.topLevelItem(index)).height()
        for index in range(tree.topLevelItemCount())
    ]
    dialog.reject()

QtCore.QTimer.singleShot(100, inspect)
show_conflict_review_dialog(
    ConflictReviewRequest(
        kind="emission_duplicate",
        title="选择重复发射谱",
        instruction="请选择保留项。",
        choices=tuple(
            ConflictReviewChoice(
                f"book-{index}",
                f"Book {index}",
                (
                    ("来源文件", "source.opj"),
                    ("Folder", folder),
                    ("谱图类型", "稳态发射谱"),
                    ("固定激发波长", "300.00"),
                ),
            )
            for index, folder in enumerate(folders)
        ),
        selection_mode="single",
    )
)
print(json.dumps(observed, ensure_ascii=False))
'''
        completed = _run_qt_script(script)

        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        observed = __import__("json").loads(completed.stdout.strip())
        self.assertTrue(observed["elide_none"])
        self.assertEqual(
            [
                "Folder：DiMeFL_EID_mTHF_with_a_long_distinguishing_suffix_A",
                "Folder：DiMeFL_EID_mTHF_with_a_long_distinguishing_suffix_B",
            ],
            observed["rows"],
        )
        self.assertTrue(all("…" not in text for text in observed["rows"]))
        self.assertTrue(all(height >= 36 for height in observed["heights"]))

    def test_conflict_review_rows_break_complete_fields_at_semantic_boundaries(self):
        script = r'''
import json
from PySide6 import QtCore, QtWidgets
from spectrum_organizer.ui.dialog_port import (
    ConflictReviewChoice,
    ConflictReviewRequest,
    show_conflict_review_dialog,
)

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
observed = {}

def inspect():
    dialog = next(
        widget
        for widget in app.topLevelWidgets()
        if isinstance(widget, QtWidgets.QDialog) and widget.isVisible()
    )
    dialog.resize(460, dialog.height())
    tree = dialog.findChild(QtWidgets.QTreeWidget, "conflict_review_candidates")
    app.processEvents()
    observed["rows"] = [
        tree.topLevelItem(index).text(1)
        for index in range(tree.topLevelItemCount())
    ]
    observed["heights"] = [
        tree.visualItemRect(tree.topLevelItem(index)).height()
        for index in range(tree.topLevelItemCount())
    ]
    metrics = tree.fontMetrics()
    observed["line_spacing"] = metrics.lineSpacing()
    observed["word_wrap"] = tree.wordWrap()
    observed["detail_width"] = tree.columnWidth(1)
    observed["viewport_width"] = tree.viewport().width()
    observed["column_total"] = sum(
        tree.columnWidth(column)
        for column in range(tree.columnCount())
    )
    observed["horizontal_off"] = (
        tree.horizontalScrollBarPolicy()
        == QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    )
    observed["horizontal_range"] = tree.horizontalScrollBar().maximum()
    dialog.reject()

QtCore.QTimer.singleShot(100, inspect)
show_conflict_review_dialog(
    ConflictReviewRequest(
        kind="emission_duplicate",
        title="选择重复发射谱",
        instruction="请选择保留项。",
        choices=(
            ConflictReviewChoice(
                "book-a",
                "Pho300_10_10",
                (
                    ("来源文件", "20241209_MFL_2DPho.opj"),
                    ("Folder", "PF8_mTHF"),
                    ("扫描范围", "315.00 – 750.00"),
                    ("峰值", "X=595.0，Y=23,095.5"),
                ),
            ),
            ConflictReviewChoice(
                "book-b",
                "Pho300_10_10_F340",
                (
                    ("来源文件", "20250507_PFLDelay.OPJ"),
                    ("Folder", "PFL_10^-4M"),
                    ("扫描范围", "400.00 – 750.00"),
                    ("峰值", "X=597.0，Y=29,776.56"),
                ),
            ),
        ),
        selection_mode="single",
    )
)
print(json.dumps(observed, ensure_ascii=False))
'''
        completed = _run_qt_script(script)

        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        observed = __import__("json").loads(completed.stdout.strip())
        for row in observed["rows"]:
            lines = row.splitlines()
            self.assertGreaterEqual(len(lines), 3, row)
            self.assertIn("来源文件：", row)
            self.assertIn("Folder：", row)
            peak_lines = [line for line in lines if line.startswith("峰值：")]
            self.assertEqual(1, len(peak_lines), row)
            self.assertIn("X=", peak_lines[0])
            self.assertIn("Y=", peak_lines[0])
        self.assertTrue(
            all(
                height >= observed["line_spacing"] * 3 + 12
                for height in observed["heights"]
            ),
            observed,
        )
        self.assertFalse(observed["word_wrap"], observed)
        self.assertLessEqual(
            observed["column_total"],
            observed["viewport_width"],
            observed,
        )
        self.assertTrue(observed["horizontal_off"], observed)
        self.assertEqual(0, observed["horizontal_range"], observed)

    def test_conflict_review_wide_rows_pack_complete_fields_before_wrapping(self):
        script = r'''
import json
from PySide6 import QtCore, QtGui, QtWidgets
from spectrum_organizer.ui.dialog_port import (
    ConflictReviewChoice,
    ConflictReviewRequest,
    show_conflict_review_dialog,
)

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
observed = {}

def inspect():
    dialog = next(
        widget
        for widget in app.topLevelWidgets()
        if isinstance(widget, QtWidgets.QDialog) and widget.isVisible()
    )
    tree = dialog.findChild(QtWidgets.QTreeWidget, "conflict_review_candidates")
    app.processEvents()
    metrics = tree.fontMetrics()
    first = tree.topLevelItem(0)
    complete_fields = (
        "固定激发波长：280.00",
        "峰值：X=749.0，Y=349.54",
    )
    observed["detail_width"] = tree.columnWidth(1)
    observed["packed_width"] = (
        sum(metrics.horizontalAdvance(field) for field in complete_fields)
        + metrics.horizontalAdvance(" · ")
        + 18
    )
    observed["line_spacing"] = metrics.lineSpacing()
    observed["row_height"] = tree.visualItemRect(first).height()
    observed["dialog_width"] = dialog.width()
    observed["preferred_width"] = min(
        900,
        dialog.screen().availableGeometry().width() - 24,
    )
    image = tree.viewport().grab().toImage()
    row = tree.visualItemRect(first)
    detail_left = tree.columnViewportPosition(1)
    detail_right = detail_left + tree.columnWidth(1) - 1
    ink_rows = []
    for y in range(row.top(), row.bottom() + 1):
        if any(
            max(
                image.pixelColor(x, y).red(),
                image.pixelColor(x, y).green(),
                image.pixelColor(x, y).blue(),
            )
            < 120
            for x in range(detail_left, detail_right + 1)
        ):
            ink_rows.append(y)
    bands = []
    for y in ink_rows:
        if not bands or y > bands[-1][-1] + 1:
            bands.append([])
        bands[-1].append(y)
    observed["painted_line_count"] = len(bands)
    observed["ink_top"] = min(ink_rows)
    observed["ink_bottom"] = max(ink_rows)
    observed["row_top"] = row.top()
    observed["row_bottom"] = row.bottom()
    observed["horizontal_off"] = (
        tree.horizontalScrollBarPolicy()
        == QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    )
    observed["horizontal_range"] = tree.horizontalScrollBar().maximum()
    dialog.reject()

QtCore.QTimer.singleShot(100, inspect)
show_conflict_review_dialog(
    ConflictReviewRequest(
        kind="emission_duplicate",
        title="选择重复发射谱",
        instruction="请选择保留项。",
        choices=(
            ConflictReviewChoice(
                "book-a",
                "Pho360_10_10",
                (
                    ("固定激发波长", "280.00"),
                    ("峰值", "X=749.0，Y=349.54"),
                ),
            ),
            ConflictReviewChoice(
                "book-b",
                "Pho360_10_10_F340",
                (
                    ("固定激发波长", "290.00"),
                    ("峰值", "X=511.0，Y=351.55"),
                ),
            ),
        ),
        selection_mode="single",
    )
)
print(json.dumps(observed, ensure_ascii=False))
'''
        completed = _run_qt_script(script)

        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        observed = __import__("json").loads(completed.stdout.strip())
        self.assertGreaterEqual(
            observed["detail_width"],
            observed["packed_width"],
            observed,
        )
        self.assertLess(
            observed["row_height"],
            observed["line_spacing"] * 2 + 12,
            observed,
        )
        self.assertEqual(
            observed["preferred_width"],
            observed["dialog_width"],
            observed,
        )
        self.assertEqual(1, observed["painted_line_count"], observed)
        self.assertGreater(observed["ink_top"], observed["row_top"], observed)
        self.assertLess(observed["ink_bottom"], observed["row_bottom"], observed)
        self.assertTrue(observed["horizontal_off"], observed)
        self.assertEqual(0, observed["horizontal_range"], observed)

    def test_conflict_review_candidates_render_shared_semantic_field_slots(self):
        script = r'''
import json
from PySide6 import QtCore, QtWidgets
from spectrum_organizer.ui.dialog_port import (
    ConflictReviewChoice,
    ConflictReviewRequest,
    show_conflict_review_dialog,
)

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
observed = {}
active_kind = ""

def inspect():
    dialog = next(
        widget
        for widget in app.topLevelWidgets()
        if isinstance(widget, QtWidgets.QDialog) and widget.isVisible()
    )
    tree = dialog.findChild(QtWidgets.QTreeWidget, "conflict_review_candidates")
    app.processEvents()
    tree.clearSelection()
    tree.clearFocus()
    app.processEvents()
    image = tree.viewport().grab().toImage()
    metrics = tree.fontMetrics()
    rows = [
        tree.topLevelItem(index).text(1).splitlines()
        for index in range(tree.topLevelItemCount())
    ]
    field_count = max(len(row) for row in rows)
    slot_widths = [
        max(
            metrics.horizontalAdvance(row[slot] if slot < len(row) else "")
            for row in rows
        )
        for slot in range(field_count)
    ]
    separator_width = metrics.horizontalAdvance(" · ")
    expected_offsets = []
    x = 0
    for width in slot_widths:
        expected_offsets.append(x)
        x += width + separator_width

    starts_by_row = []
    ink_bounds = []
    placeholder_ink = False
    for row_index in range(tree.topLevelItemCount()):
        item = tree.topLevelItem(row_index)
        row_rect = tree.visualItemRect(item)
        option = QtWidgets.QStyleOptionViewItem()
        option.initFrom(tree)
        option.widget = tree
        option.rect = QtCore.QRect(
            tree.columnViewportPosition(1),
            row_rect.top(),
            tree.columnWidth(1),
            row_rect.height(),
        )
        text_rect = tree.style().subElementRect(
            QtWidgets.QStyle.SubElement.SE_ItemViewItemText,
            option,
            tree,
        )
        starts = []
        dark_pixels = []
        for offset in expected_offsets:
            field_pixels = []
            for y in range(row_rect.top(), row_rect.bottom() + 1):
                for pixel_x in range(
                    text_rect.left() + offset,
                    min(text_rect.left() + offset + 20, text_rect.right() + 1),
                ):
                    color = image.pixelColor(pixel_x, y)
                    if max(color.red(), color.green(), color.blue()) < 120:
                        field_pixels.append((pixel_x, y))
            starts.append(
                min(pixel_x for pixel_x, _y in field_pixels)
                if field_pixels
                else None
            )
            dark_pixels.extend(field_pixels)
        starts_by_row.append(starts)
        if row_index == 1:
            placeholder_left = (
                text_rect.left()
                + expected_offsets[1]
                + metrics.horizontalAdvance("B：")
            )
            placeholder_right = (
                text_rect.left()
                + expected_offsets[1]
                + metrics.horizontalAdvance("B：—")
            )
            placeholder_ink = any(
                max(
                    image.pixelColor(pixel_x, y).red(),
                    image.pixelColor(pixel_x, y).green(),
                    image.pixelColor(pixel_x, y).blue(),
                )
                < 120
                for y in range(row_rect.top(), row_rect.bottom() + 1)
                for pixel_x in range(placeholder_left, placeholder_right + 1)
            )
        ink_bounds.append(
            [
                min(y for _x, y in dark_pixels),
                max(y for _x, y in dark_pixels),
                row_rect.top(),
                row_rect.bottom(),
            ]
        )
    observed[active_kind] = {
        "texts": ["\n".join(row) for row in rows],
        "starts": starts_by_row,
        "ink_bounds": ink_bounds,
        "placeholder_ink": placeholder_ink,
        "horizontal_range": tree.horizontalScrollBar().maximum(),
    }
    dialog.reject()

choices = (
    ConflictReviewChoice(
        "book-a",
        "row-a",
        (("A", "x"), ("B", "short"), ("C", "one"), ("D", "tail")),
    ),
    ConflictReviewChoice(
        "book-b",
        "row-b",
        (("A", "much-longer"), ("C", "two"), ("D", "end")),
    ),
)
for active_kind, selection_mode in (
    ("special_group", "none"),
    ("emission_duplicate", "single"),
    ("excitation_selection", "multi"),
):
    QtCore.QTimer.singleShot(100, inspect)
    show_conflict_review_dialog(
        ConflictReviewRequest(
            kind=active_kind,
            title="候选审核",
            instruction="请选择。",
            choices=choices,
            selection_mode=selection_mode,
            single_select_groups=(
                (("book-a", "book-b"),)
                if active_kind == "excitation_selection"
                else ()
            ),
        )
    )
print(json.dumps(observed, ensure_ascii=False))
'''
        completed = _run_qt_script(script)

        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        observed = __import__("json").loads(completed.stdout.strip())
        for kind in (
            "special_group",
            "emission_duplicate",
            "excitation_selection",
        ):
            rendered = observed[kind]
            self.assertIn("B：—", rendered["texts"][1], rendered)
            self.assertTrue(rendered["placeholder_ink"], rendered)
            first, second = rendered["starts"]
            self.assertNotIn(None, first, rendered)
            self.assertEqual(first, second, rendered)
            for ink_top, ink_bottom, row_top, row_bottom in rendered["ink_bounds"]:
                self.assertGreater(ink_top, row_top, rendered)
                self.assertLess(ink_bottom, row_bottom, rendered)
            self.assertEqual(0, rendered["horizontal_range"], rendered)

    def test_conflict_review_overwide_atomic_field_uses_semantic_fallback_without_horizontal_scroll(self):
        script = r'''
import json
from PySide6 import QtCore, QtWidgets
from spectrum_organizer.ui.dialog_port import (
    ConflictReviewChoice,
    ConflictReviewRequest,
    show_conflict_review_dialog,
)

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
observed = {}
long_folder = (
    "Folder："
    "segment_00/segment_01/segment_02/segment_03/segment_04/"
    "segment_05/segment_06/segment_07/segment_08/segment_09/"
    "segment_10/segment_11/segment_12/segment_13/segment_14"
)

def inspect():
    dialog = next(
        widget
        for widget in app.topLevelWidgets()
        if isinstance(widget, QtWidgets.QDialog) and widget.isVisible()
    )
    tree = dialog.findChild(QtWidgets.QTreeWidget, "conflict_review_candidates")
    app.processEvents()
    item = tree.topLevelItem(0)
    metrics = tree.fontMetrics()
    observed["text"] = item.text(1)
    observed["atomic_width"] = metrics.horizontalAdvance(long_folder) + 18
    observed["detail_width"] = tree.columnWidth(1)
    observed["row_height"] = tree.visualItemRect(item).height()
    observed["line_spacing"] = metrics.lineSpacing()
    observed["dialog_width"] = dialog.width()
    observed["screen_limit"] = (
        dialog.screen().availableGeometry().width() - 24
    )
    observed["horizontal_off"] = (
        tree.horizontalScrollBarPolicy()
        == QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    )
    observed["horizontal_range"] = tree.horizontalScrollBar().maximum()
    observed["column_total"] = sum(
        tree.columnWidth(column)
        for column in range(tree.columnCount())
    )
    observed["viewport_width"] = tree.viewport().width()
    image = tree.viewport().grab().toImage()
    row = tree.visualItemRect(item)
    detail_left = tree.columnViewportPosition(1)
    detail_right = detail_left + tree.columnWidth(1) - 1
    ink_rows = []
    for y in range(row.top(), row.bottom() + 1):
        if any(
            max(
                image.pixelColor(x, y).red(),
                image.pixelColor(x, y).green(),
                image.pixelColor(x, y).blue(),
            )
            < 120
            for x in range(detail_left, detail_right + 1)
        ):
            ink_rows.append(y)
    bands = []
    for y in ink_rows:
        if not bands or y > bands[-1][-1] + 1:
            bands.append([])
        bands[-1].append(y)
    observed["painted_line_count"] = len(bands)
    observed["ink_top"] = min(ink_rows)
    observed["ink_bottom"] = max(ink_rows)
    observed["row_top"] = row.top()
    observed["row_bottom"] = row.bottom()
    dialog.reject()

QtCore.QTimer.singleShot(100, inspect)
show_conflict_review_dialog(
    ConflictReviewRequest(
        kind="emission_duplicate",
        title="选择重复发射谱",
        instruction="请选择保留项。",
        choices=(
            ConflictReviewChoice(
                "book-a",
                "Pho300_10_10",
                (
                    ("来源文件", "source-a.opj"),
                    ("Folder", long_folder.removeprefix("Folder：")),
                ),
            ),
            ConflictReviewChoice(
                "book-b",
                "Pho300_10_10_F340",
                (
                    ("来源文件", "source-b.opj"),
                    ("Folder", "short"),
                ),
            ),
        ),
        selection_mode="single",
    )
)
print(json.dumps(observed, ensure_ascii=False))
'''
        completed = _run_qt_script(script)

        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        observed = __import__("json").loads(completed.stdout.strip())
        self.assertEqual(
            (
                "来源文件：source-a.opj\n"
                "Folder：segment_00/segment_01/segment_02/segment_03/segment_04/"
                "segment_05/segment_06/segment_07/segment_08/segment_09/"
                "segment_10/segment_11/segment_12/segment_13/segment_14"
            ),
            observed["text"],
        )
        self.assertEqual(observed["screen_limit"], observed["dialog_width"], observed)
        self.assertGreater(
            observed["atomic_width"],
            observed["detail_width"],
            observed,
        )
        self.assertGreaterEqual(
            observed["row_height"],
            observed["line_spacing"] * 2 + 20,
            observed,
        )
        self.assertLessEqual(
            observed["column_total"],
            observed["viewport_width"],
            observed,
        )
        self.assertGreaterEqual(observed["painted_line_count"], 2, observed)
        self.assertGreater(observed["ink_top"], observed["row_top"], observed)
        self.assertLess(observed["ink_bottom"], observed["row_bottom"], observed)
        self.assertTrue(observed["horizontal_off"], observed)
        self.assertEqual(0, observed["horizontal_range"], observed)

    def test_conflict_review_multiline_rows_have_leading_and_contiguous_state_backgrounds(self):
        script = r'''
import json
from PySide6 import QtCore, QtWidgets
from spectrum_organizer.ui.dialog_port import (
    ConflictReviewChoice,
    ConflictReviewRequest,
    show_conflict_review_dialog,
)

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
observed = {}

def inspect():
    dialog = next(
        widget
        for widget in app.topLevelWidgets()
        if isinstance(widget, QtWidgets.QDialog) and widget.isVisible()
    )
    tree = dialog.findChild(QtWidgets.QTreeWidget, "conflict_review_candidates")
    app.processEvents()
    first = tree.topLevelItem(0)
    row = tree.visualItemRect(first)
    image = tree.viewport().grab().toImage()
    sample_x = tree.columnViewportPosition(1) + 24
    sample_y = row.bottom() - 2
    observed["gap_color"] = image.pixelColor(sample_x, sample_y).name()
    observed["line_spacing"] = tree.fontMetrics().lineSpacing()
    observed["row_height"] = row.height()
    dialog.reject()

long_source = "20241209_MFL_2DPho_with_a_long_distinguishing_source_name.opj"
long_folder = "DiMeFL_EID_mTHF_with_a_long_distinguishing_folder_name"
QtCore.QTimer.singleShot(100, inspect)
show_conflict_review_dialog(
    ConflictReviewRequest(
        kind="excitation_selection",
        title="选择激发谱",
        instruction="请选择候选。",
        choices=(
            ConflictReviewChoice(
                "book-a",
                "PhoEx596_10_10",
                (
                    ("来源文件", long_source),
                    ("Folder", long_folder),
                    ("扫描范围", "200.00 – 581.00"),
                    ("峰值", "X=447.0，Y=29,046.87"),
                ),
            ),
            ConflictReviewChoice(
                "book-b",
                "PhoEx568_10_10",
                (
                    ("来源文件", long_source.replace("09", "10")),
                    ("Folder", long_folder.replace("mTHF", "DCM")),
                    ("扫描范围", "240.00 – 553.00"),
                    ("峰值", "X=350.0，Y=11,800.99"),
                ),
            ),
        ),
        selection_mode="multi",
        initial_selection=("book-a", "book-b"),
    )
)
print(json.dumps(observed, ensure_ascii=False))
'''
        completed = _run_qt_script(script)

        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        observed = __import__("json").loads(completed.stdout.strip())
        self.assertGreaterEqual(
            observed["row_height"],
            observed["line_spacing"] * 3 + 20,
            observed,
        )
        self.assertEqual("#147a6c", observed["gap_color"], observed)

    def test_conflict_review_dialog_emphasizes_subject_without_repeated_facts_and_aligns_actions(self):
        script = r'''
import json
from PySide6 import QtCore, QtWidgets
from spectrum_organizer.ui.dialog_port import (
    ConflictReviewChoice,
    ConflictReviewRequest,
    show_conflict_review_dialog,
)

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
observed = {}

def interact():
    dialog = next(
        widget
        for widget in app.topLevelWidgets()
        if isinstance(widget, QtWidgets.QDialog) and widget.isVisible()
    )
    tree = dialog.findChild(QtWidgets.QTreeWidget, "conflict_review_candidates")
    subject = dialog.findChild(QtWidgets.QLabel, "conflict_review_subject")
    guidance_labels = [
        label.text()
        for label in dialog.findChildren(QtWidgets.QLabel)
        if label.objectName() == "conflict_review_guidance_label"
    ]
    detail_title = dialog.findChild(
        QtWidgets.QLabel,
        "conflict_review_detail_title",
    )
    instruction = dialog.findChild(
        QtWidgets.QLabel,
        "conflict_review_guidance_text",
    )
    detail_values = dialog.findChildren(
        QtWidgets.QLabel,
        "conflict_review_detail_value",
    )
    footer = dialog.findChild(QtWidgets.QFrame, "conflict_review_footer")
    footer_buttons = [
        button
        for button in footer.findChildren(QtWidgets.QPushButton)
        if button.isVisible()
    ]

    tree.setCurrentItem(tree.topLevelItem(0))
    app.processEvents()

    observed["subject"] = subject.text() if subject is not None else ""
    observed["guidance_labels"] = guidance_labels
    observed["detail_title"] = detail_title.text()
    observed["detail_text"] = "\n".join(
        label.text() for label in detail_values
    )
    observed["row_text"] = [
        tree.topLevelItem(index).text(1)
        for index in range(tree.topLevelItemCount())
    ]
    observed["subject_above_instruction"] = (
        subject.mapTo(dialog, QtCore.QPoint(0, 0)).y()
        < instruction.mapTo(dialog, QtCore.QPoint(0, 0)).y()
    )
    observed["subject_larger"] = (
        subject.font().pointSizeF() > instruction.font().pointSizeF()
    )
    observed["row_heights"] = [
        tree.visualItemRect(tree.topLevelItem(index)).height()
        for index in range(tree.topLevelItemCount())
    ]
    observed["button_y"] = [
        button.mapTo(footer, QtCore.QPoint(0, 0)).y()
        for button in footer_buttons
    ]
    observed["button_heights"] = [button.height() for button in footer_buttons]
    next(button for button in footer_buttons if button.text() == "取消并退出").click()

common_fields = (
    ("来源文件", "20241209_MFL_2DPho.opj"),
    ("Folder", "MeFL_mTHF"),
    ("谱图类型", "二维延迟谱"),
    ("扫描范围", "350.00 – 650.00"),
    ("扫描步长", "1.00"),
    ("狭缝", "Ex 10.00/10.00 / Em 10.00/10.00"),
    ("延迟参数", "Flash Delay 10.00"),
    ("Note 时间", "2026-07-27 11:00"),
)
QtCore.QTimer.singleShot(0, interact)
show_conflict_review_dialog(
    ConflictReviewRequest(
        kind="emission_duplicate",
        title="选择特殊组重复点",
        decision_subject="二维延迟谱",
        instruction="同一点存在两个候选，请保留一个。",
        choices=(
            ConflictReviewChoice(
                "a",
                "300",
                common_fields + (
                    ("固定激发波长", "300.00"),
                    ("峰值", "X=458.0，Y=14,652.65"),
                ),
            ),
            ConflictReviewChoice(
                "b",
                "Pho300_Re",
                common_fields + (
                    ("固定激发波长", "300.00"),
                    ("峰值", "X=459.0，Y=15,002.12"),
                ),
            ),
            ConflictReviewChoice(
                "c",
                "305",
                common_fields + (
                    ("固定激发波长", "305.00"),
                    ("峰值", "X=460.0，Y=13,500.00"),
                ),
            ),
        ),
        selection_mode="multi",
        actions=(
            "return_previous",
            "return_to_attribution",
            "confirm_selection",
            "cancel",
        ),
        single_select_groups=(("a", "b"),),
        initial_selection=("a", "c"),
    )
)
print(json.dumps(observed, ensure_ascii=False))
'''
        completed = _run_qt_script(script)

        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        observed = __import__("json").loads(completed.stdout.strip())
        self.assertEqual("二维延迟谱", observed["subject"])
        self.assertEqual([], observed["guidance_labels"])
        self.assertTrue(observed["subject_above_instruction"])
        self.assertTrue(observed["subject_larger"])
        self.assertEqual("共同条件", observed["detail_title"])
        self.assertIn("来源文件：20241209_MFL_2DPho.opj", observed["detail_text"])
        self.assertIn("Folder：MeFL_mTHF", observed["detail_text"])
        self.assertIn("延迟参数：Flash Delay 10.00", observed["detail_text"])
        self.assertNotIn("固定激发波长", observed["detail_text"])
        self.assertNotIn("峰值", observed["detail_text"])
        self.assertTrue(
            all("固定激发波长" in text and "峰值" in text for text in observed["row_text"]),
            observed["row_text"],
        )
        self.assertTrue(
            all(height >= 36 for height in observed["row_heights"]),
            observed["row_heights"],
        )
        self.assertEqual(1, len(set(observed["button_y"])))
        self.assertEqual(1, len(set(observed["button_heights"])))

    def test_conflict_review_normal_desktop_exposes_five_semantic_candidates(self):
        script = r'''
import json
from PySide6 import QtCore, QtWidgets
from spectrum_organizer.ui.dialog_port import (
    ConflictReviewChoice,
    ConflictReviewRequest,
    show_conflict_review_dialog,
)

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
observed = {}

def inspect():
    dialog = next(
        widget
        for widget in app.topLevelWidgets()
        if isinstance(widget, QtWidgets.QDialog) and widget.isVisible()
    )
    tree = dialog.findChild(QtWidgets.QTreeWidget, "conflict_review_candidates")
    viewport = tree.viewport().rect()
    observed["dialog_height"] = dialog.height()
    observed["dialog_width"] = dialog.width()
    observed["candidate_height"] = tree.viewport().height()
    observed["column_widths"] = [
        tree.columnWidth(column)
        for column in range(tree.columnCount())
    ]
    observed["row_heights"] = [
        tree.visualItemRect(tree.topLevelItem(index)).height()
        for index in range(tree.topLevelItemCount())
    ]
    observed["fully_visible"] = sum(
        (
            viewport.top()
            <= tree.visualItemRect(tree.topLevelItem(index)).top()
            and tree.visualItemRect(tree.topLevelItem(index)).bottom()
            <= viewport.bottom()
        )
        for index in range(tree.topLevelItemCount())
    )
    dialog.reject()

QtCore.QTimer.singleShot(100, inspect)
show_conflict_review_dialog(
    ConflictReviewRequest(
        kind="special_group",
        title="确认特殊谱组",
        instruction="这些 Book 共同组成一个候选特殊谱。",
        choices=tuple(
            ConflictReviewChoice(
                f"book-{index}",
                f"PhoEx{596 - index}_10_10",
                (
                    ("来源文件", f"20241209_MFL_2DPho_{index // 2}.opj"),
                    ("Folder", f"PF{8 - index // 2}_mTHF"),
                    ("谱图类型", "延迟激发谱"),
                    ("固定发射波长", f"{596 - index}.00"),
                    ("扫描范围", f"200.00 – {581 - index}.00"),
                    ("扫描步长", "1.00"),
                    ("狭缝", "Ex 10.00/10.00 / Em 10.00/10.00"),
                    ("延迟时间", "1.00"),
                    ("采样窗口", "20.00"),
                    ("单次闪光周期", "46.00"),
                    ("闪光次数", "4"),
                    ("峰值", f"X={447 - index}，Y={29046 + index}.87"),
                ),
            )
            for index in range(7)
        ),
        selection_mode="multi",
        single_select_groups=(
            ("book-0", "book-1"),
            ("book-2", "book-3"),
            ("book-4", "book-5"),
        ),
        actions=("confirm_group", "review_books", "reject_group", "return_to_attribution"),
    )
)
print(json.dumps(observed))
'''
        completed = _run_qt_script(script)

        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        observed = __import__("json").loads(completed.stdout.strip())
        self.assertGreaterEqual(observed["fully_visible"], 5, observed)

    def test_grouped_conflict_editor_updates_one_group_and_active_common_conditions(self):
        script = r'''
import json
from PySide6 import QtCore, QtTest, QtWidgets
from spectrum_organizer.ui.dialog_port import (
    ConflictReviewChoice,
    ConflictReviewGroup,
    ConflictReviewRequest,
    show_conflict_review_dialog,
)

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
observed = {}

def interact():
    dialog = next(
        widget
        for widget in app.topLevelWidgets()
        if isinstance(widget, QtWidgets.QDialog) and widget.isVisible()
    )
    tree = dialog.findChild(QtWidgets.QTreeWidget, "conflict_review_candidates")
    buttons = [
        button
        for button in dialog.findChildren(QtWidgets.QPushButton)
        if button.objectName().startswith("dialog_button_")
    ]
    observed["labels"] = [button.text() for button in buttons]
    observed["initial_selected"] = [
        tree.topLevelItem(index).text(0)
        for index in range(tree.topLevelItemCount())
        if tree.topLevelItem(index).isSelected()
    ]
    observed["top_level_count"] = tree.topLevelItemCount()
    initial_rects = [
        tree.visualItemRect(tree.topLevelItem(index))
        for index in range(tree.topLevelItemCount())
    ]
    observed["internal_gap"] = (
        initial_rects[1].top() - initial_rects[0].bottom() - 1
    )
    observed["between_group_gap"] = (
        initial_rects[3].top() - initial_rects[1].bottom() - 1
    )
    observed["spacer_height"] = initial_rects[2].height()
    detail_values = dialog.findChildren(
        QtWidgets.QLabel,
        "conflict_review_detail_value",
    )
    observed["initial_detail"] = "\n".join(
        label.text() for label in detail_values
    )
    target = tree.topLevelItem(3)
    QtTest.QTest.mouseClick(
        tree.viewport(),
        QtCore.Qt.MouseButton.LeftButton,
        QtCore.Qt.KeyboardModifier.NoModifier,
        tree.visualItemRect(target).center(),
    )
    app.processEvents()
    viewport_image = tree.viewport().grab().toImage()
    active_rect = tree.visualItemRect(tree.topLevelItem(4))
    inactive_rect = tree.visualItemRect(tree.topLevelItem(1))
    observed["active_rail"] = viewport_image.pixelColor(
        active_rect.left() + 1,
        active_rect.center().y(),
    ).name()
    observed["inactive_rail"] = viewport_image.pixelColor(
        inactive_rect.left() + 1,
        inactive_rect.center().y(),
    ).name()
    observed["updated_selected"] = [
        tree.topLevelItem(index).text(0)
        for index in range(tree.topLevelItemCount())
        if tree.topLevelItem(index).isSelected()
    ]
    observed["updated_detail"] = "\n".join(
        label.text() for label in detail_values
    )
    next(
        button for button in buttons
        if button.text() == "确认全部选择"
    ).click()

QtCore.QTimer.singleShot(0, interact)
response = show_conflict_review_dialog(
    ConflictReviewRequest(
        kind="special_conflict_batch",
        title="确认特殊谱相关冲突",
        decision_subject="二维延迟谱",
        instruction="请为每个冲突保留一个候选 Book。",
        choices=(),
        selection_mode="grouped_single",
        actions=("return_to_group", "confirm_all_conflicts", "cancel"),
        choice_groups=(
            ConflictReviewGroup(
                group_key="point-300",
                initial_selection="book-300-a",
                common_fields=(
                    ("来源文件", "source.opj"),
                    ("Folder", "Delayed-300"),
                    ("固定激发波长", "300.00"),
                ),
                choices=(
                    ConflictReviewChoice(
                        "book-300-a",
                        "300",
                        (
                            ("峰值", "X=458.0，Y=144652.65"),
                        ),
                    ),
                    ConflictReviewChoice(
                        "book-300-b",
                        "Pho300_10_10",
                        (
                            ("峰值", "X=459.0，Y=36313.95"),
                        ),
                    ),
                ),
            ),
            ConflictReviewGroup(
                group_key="point-450",
                initial_selection="book-450-b",
                common_fields=(
                    ("来源文件", "source.opj"),
                    ("Folder", "Delayed-450"),
                    ("固定激发波长", "450.00"),
                ),
                choices=(
                    ConflictReviewChoice(
                        "book-450-a",
                        "450",
                        (
                            ("峰值", "X=595.0，Y=11325.44"),
                        ),
                    ),
                    ConflictReviewChoice(
                        "book-450-b",
                        "Pho450_10_10",
                        (
                            ("峰值", "X=595.0，Y=26477.86"),
                        ),
                    ),
                ),
            ),
        ),
        initial_active_group_key="point-300",
    )
)
observed["action"] = response.action
observed["group_selections"] = [list(pair) for pair in response.group_selections]
observed["active_group_key"] = response.active_group_key
print(json.dumps(observed, ensure_ascii=False))
'''
        completed = _run_qt_script(script)

        self.assertEqual(
            0,
            completed.returncode,
            completed.stdout + completed.stderr,
        )
        observed = __import__("json").loads(completed.stdout.strip())
        self.assertEqual(
            ["返回整组确认", "确认全部选择", "取消并退出"],
            observed["labels"],
        )
        self.assertEqual(
            ["300", "Pho450_10_10"],
            observed["initial_selected"],
        )
        self.assertEqual(5, observed["top_level_count"])
        self.assertEqual(0, observed["internal_gap"])
        self.assertGreaterEqual(observed["between_group_gap"], 8)
        self.assertEqual(8, observed["spacer_height"])
        self.assertIn("固定激发波长：300.00", observed["initial_detail"])
        self.assertEqual(
            ["300", "450"],
            observed["updated_selected"],
        )
        self.assertIn("固定激发波长：450.00", observed["updated_detail"])
        self.assertEqual("confirm_all_conflicts", observed["action"])
        self.assertEqual(
            [["point-300", "book-300-a"], ["point-450", "book-450-a"]],
            observed["group_selections"],
        )
        self.assertEqual("point-450", observed["active_group_key"])
        self.assertEqual("#0b8b7c", observed["active_rail"])
        self.assertNotEqual(observed["active_rail"], observed["inactive_rail"])

    def test_conflict_review_actions_use_dense_readable_typography(self):
        script = r'''
import json
from PySide6 import QtCore, QtWidgets
from spectrum_organizer.ui.dialog_port import (
    ConflictReviewChoice,
    ConflictReviewRequest,
    show_conflict_review_dialog,
)

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
observed = {}

def inspect():
    dialog = next(
        widget
        for widget in app.topLevelWidgets()
        if isinstance(widget, QtWidgets.QDialog) and widget.isVisible()
    )
    buttons = [
        button
        for button in dialog.findChildren(QtWidgets.QPushButton)
        if button.objectName().startswith("dialog_button_")
    ]
    observed["buttons"] = [
        {
            "text": button.text(),
            "height": button.height(),
            "font_px": button.font().pixelSize(),
            "weight": button.font().weight(),
        }
        for button in buttons
    ]
    dialog.reject()

QtCore.QTimer.singleShot(100, inspect)
show_conflict_review_dialog(
    ConflictReviewRequest(
        kind="special_group",
        title="确认特殊谱组",
        instruction="检查按钮密度。",
        choices=(
            ConflictReviewChoice("book-1", "Book 1", (("峰值", "X=1，Y=2"),)),
        ),
        selection_mode="none",
        actions=("return_to_attribution", "confirm_group", "review_books", "reject_group"),
    )
)
print(json.dumps(observed, ensure_ascii=False))
'''
        completed = _run_qt_script(script)

        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        observed = __import__("json").loads(completed.stdout.strip())
        self.assertEqual(4, len(observed["buttons"]), observed)
        for button in observed["buttons"]:
            self.assertLessEqual(button["height"], 42, button)
            self.assertGreaterEqual(button["font_px"], 14, button)
            self.assertGreaterEqual(button["weight"], 600, button)

    def test_conflict_review_renders_identical_delay_components_only_as_common(self):
        script = r'''
import json
from PySide6 import QtCore, QtWidgets
from spectrum_organizer.ui.dialog_port import (
    ConflictReviewChoice,
    ConflictReviewRequest,
    show_conflict_review_dialog,
)

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
observed = {}

def inspect():
    dialog = next(
        widget
        for widget in app.topLevelWidgets()
        if isinstance(widget, QtWidgets.QDialog) and widget.isVisible()
    )
    tree = dialog.findChild(QtWidgets.QTreeWidget, "conflict_review_candidates")
    observed["differences"] = [
        tree.topLevelItem(index).text(1)
        for index in range(tree.topLevelItemCount())
    ]
    observed["common"] = "\n".join(
        label.text()
        for label in dialog.findChildren(QtWidgets.QLabel)
        if label.objectName() == "conflict_review_detail_value"
    )
    dialog.reject()

QtCore.QTimer.singleShot(100, inspect)
show_conflict_review_dialog(
    ConflictReviewRequest(
        kind="special_group",
        title="确认特殊谱组",
        instruction="这些 Book 共同组成一个候选特殊谱。",
        choices=(
            ConflictReviewChoice(
                "a",
                "P300-0.05ms",
                (
                    ("Flash Delay", "0.05"),
                    ("Sample Window", "20.00"),
                    ("Time per Flash", "45.05"),
                    ("Flash Count", "4"),
                ),
            ),
            ConflictReviewChoice(
                "b",
                "P300-0.50ms",
                (
                    ("Flash Delay", "0.50"),
                    ("Sample Window", "20.00"),
                    ("Time per Flash", "45.50"),
                    ("Flash Count", "4"),
                ),
            ),
        ),
        selection_mode="none",
        actions=("confirm_group", "cancel"),
    )
)
print(json.dumps(observed, ensure_ascii=False))
'''
        completed = _run_qt_script(script)

        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        observed = __import__("json").loads(completed.stdout.strip())
        for difference in observed["differences"]:
            self.assertIn("Flash Delay", difference)
            self.assertIn("Time per Flash", difference)
            self.assertNotIn("Sample Window", difference)
            self.assertNotIn("Flash Count", difference)
        self.assertIn("Sample Window：20.00", observed["common"])
        self.assertIn("Flash Count：4", observed["common"])

    def test_special_group_popup_keeps_three_decisions_and_return_navigation_reachable(self):
        script = r'''
import json
from PySide6 import QtCore, QtWidgets
from spectrum_organizer.ui import dialog_port as dialog_port_module
from spectrum_organizer.ui.dialog_port import (
    ConflictReviewChoice,
    ConflictReviewRequest,
    show_conflict_review_dialog,
)

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
observed = {}

class DestinationScreen:
    def availableGeometry(self):
        return QtCore.QRect(1000, 0, 360, 260)

class FakeGuiApplication:
    @classmethod
    def screenAt(cls, _point):
        return destination_screen

class FakeQtGui:
    QGuiApplication = FakeGuiApplication

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

destination_screen = DestinationScreen()
original_drag = dialog_port_module._enable_title_bar_drag
dialog_port_module._enable_title_bar_drag = (
    lambda header, window, qt_core: original_drag(
        header,
        window,
        qt_core,
        FakeQtGui,
    )
)

def interact():
    dialog = next(
        widget
        for widget in app.topLevelWidgets()
        if isinstance(widget, QtWidgets.QDialog) and widget.isVisible()
    )
    labels = [
        button.text()
        for button in dialog.findChildren(QtWidgets.QPushButton)
        if button.objectName() != "dialog_close_button"
    ]
    observed["labels"] = labels
    observed["width_before"] = dialog.width()
    header = dialog.findChild(QtWidgets.QFrame, "dialog_header")
    start = dialog.frameGeometry().topLeft() + QtCore.QPoint(30, 20)
    header.mousePressEvent(PointerEvent(start, pressed=True))
    header.mouseMoveEvent(
        PointerEvent(QtCore.QPoint(1180, 80), pressed=True)
    )
    app.processEvents()
    observed["width_after"] = dialog.width()
    observed["minimum_width"] = dialog.minimumWidth()
    observed["maximum_width"] = dialog.maximumWidth()
    observed["frame_contained"] = destination_screen.availableGeometry().contains(
        dialog.frameGeometry()
    )
    observed["actions_reachable"] = all(
        dialog.rect().contains(
            QtCore.QRect(
                button.mapTo(dialog, QtCore.QPoint(0, 0)),
                button.size(),
            )
        )
        for button in dialog.findChildren(QtWidgets.QPushButton)
        if button.objectName() != "dialog_close_button"
    )
    observed["action_widths"] = {
        button.text(): (
            button.width(),
            button.fontMetrics().horizontalAdvance(button.text()) + 10,
        )
        for button in dialog.findChildren(QtWidgets.QPushButton)
        if button.objectName() != "dialog_close_button"
    }
    candidates = dialog.findChild(
        QtWidgets.QTreeWidget,
        "conflict_review_candidates",
    )
    observed["candidates_visible"] = (
        candidates.height() > 0
        and dialog.rect().contains(
            QtCore.QRect(
                candidates.mapTo(dialog, QtCore.QPoint(0, 0)),
                candidates.size(),
            )
        )
    )
    last_candidate = candidates.topLevelItem(candidates.topLevelItemCount() - 1)
    observed["vertical_scroll_range"] = candidates.verticalScrollBar().maximum()
    observed["horizontal_scroll_range"] = candidates.horizontalScrollBar().maximum()
    observed["pixel_scroll"] = (
        candidates.verticalScrollMode()
        == QtWidgets.QAbstractItemView.ScrollMode.ScrollPerPixel
    )
    candidates.scrollToItem(
        last_candidate,
        QtWidgets.QAbstractItemView.ScrollHint.PositionAtBottom,
    )
    candidates.verticalScrollBar().setValue(
        candidates.verticalScrollBar().maximum()
    )
    app.processEvents()
    last_rect = candidates.visualItemRect(last_candidate)
    observed["last_candidate_rect"] = list(last_rect.getRect())
    observed["candidate_viewport_rect"] = list(
        candidates.viewport().rect().getRect()
    )
    observed["vertical_scroll_value"] = candidates.verticalScrollBar().value()
    observed["last_candidate_reachable"] = (
        last_rect.isValid()
        and candidates.viewport().rect().intersects(last_rect)
        and last_rect.bottom() <= candidates.viewport().rect().bottom() + 1
        and candidates.verticalScrollBar().value()
        == candidates.verticalScrollBar().maximum()
    )
    next(button for button in dialog.findChildren(QtWidgets.QPushButton) if button.text() == "确认整个组").click()

QtCore.QTimer.singleShot(0, interact)
response = show_conflict_review_dialog(
    ConflictReviewRequest(
        kind="special_group",
        title="确认特殊谱组",
        instruction="请选择处理方式。",
        choices=tuple(
            ConflictReviewChoice(
                f"book-{index}",
                f"Candidate {index}",
                (
                    ("来源文件", f"source-with-long-name-{index}.opj"),
                    ("Folder", f"folder/segment/{index}/with-long-name"),
                    ("扫描范围", f"{300 + index}.00 – {700 + index}.00"),
                    ("峰值", f"X={500 + index}.0，Y={1000 + index}.25"),
                ),
            )
            for index in range(8)
        ),
        selection_mode="none",
        actions=(
            "return_previous",
            "return_related_conflict",
            "confirm_group",
            "review_books",
            "reject_group",
            "return_to_attribution",
        ),
    )
)
observed["action"] = response.action
print(json.dumps(observed, ensure_ascii=False))
'''
        completed = _run_qt_script(script)

        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        observed = __import__("json").loads(completed.stdout.strip())
        self.assertEqual(
            [
                "返回上一步",
                "修改相关冲突",
                "返回样品归属",
                "确认整个组",
                "逐 Book 审核",
                "拒绝整个组",
            ],
            observed["labels"],
        )
        self.assertEqual("confirm_group", observed["action"])
        self.assertLess(observed["width_after"], observed["width_before"])
        self.assertLess(observed["minimum_width"], observed["width_before"])
        self.assertGreater(observed["maximum_width"], observed["width_before"])
        self.assertTrue(observed["frame_contained"])
        self.assertTrue(observed["actions_reachable"])
        self.assertTrue(observed["candidates_visible"])
        self.assertGreater(observed["vertical_scroll_range"], 0, observed)
        self.assertEqual(0, observed["horizontal_scroll_range"], observed)
        self.assertTrue(observed["pixel_scroll"], observed)
        self.assertTrue(observed["last_candidate_reachable"], observed)
        self.assertTrue(
            all(
                width >= minimum_width
                for width, minimum_width in observed["action_widths"].values()
            ),
            observed["action_widths"],
        )

    @unittest.skipUnless(os.name == "nt", "native Windows window lifecycle only")
    def test_conflict_review_dialog_is_owned_taskbar_window_without_visible_residue(self):
        script = r'''
import ctypes
import os
from ctypes import wintypes
from PySide6 import QtCore, QtWidgets
from spectrum_organizer.ui.dialog_port import (
    ConflictReviewChoice,
    ConflictReviewRequest,
    _windows_user32,
    show_conflict_review_dialog,
)
from spectrum_organizer.ui.qt_main_window import create_production_main_window

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
window, _widgets = create_production_main_window(
    dpi_percent=100,
    size_name="desktop",
    stage="conflict_review",
)
window.show()
app.processEvents()
user32 = _windows_user32()
process_id = os.getpid()
GW_OWNER = 4
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x80
WS_EX_APPWINDOW = 0x40000
callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

def visible_top_levels():
    handles = []
    @callback_type
    def callback(hwnd, _lparam):
        owner_process_id = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner_process_id))
        if owner_process_id.value == process_id and user32.IsWindowVisible(hwnd):
            handles.append(int(hwnd))
        return True
    user32.EnumWindows(callback, 0)
    return handles

def inspect():
    dialog = next(
        widget
        for widget in app.topLevelWidgets()
        if isinstance(widget, QtWidgets.QDialog) and widget.isVisible()
    )
    dialog_hwnd = int(dialog.winId())
    owner = int(user32.GetWindow(dialog_hwnd, GW_OWNER))
    if owner != int(window.winId()):
        raise SystemExit(f"conflict owner={owner}, expected={int(window.winId())}")
    exstyle = int(user32.GetWindowLongPtrW(dialog_hwnd, GWL_EXSTYLE))
    if exstyle & WS_EX_TOOLWINDOW:
        raise SystemExit(f"conflict dialog remains WS_EX_TOOLWINDOW: {exstyle:#x}")
    if not exstyle & WS_EX_APPWINDOW:
        raise SystemExit(f"conflict dialog lacks WS_EX_APPWINDOW: {exstyle:#x}")
    visible = set(visible_top_levels())
    expected = {int(window.winId()), dialog_hwnd}
    if visible != expected:
        raise SystemExit(f"unexpected visible top levels: {visible - expected}")
    dialog.reject()

QtCore.QTimer.singleShot(100, inspect)
response = show_conflict_review_dialog(
    ConflictReviewRequest(
        kind="emission_duplicate",
        title="选择重复发射谱",
        instruction="必须保留一个。",
        choices=(ConflictReviewChoice("a", "A"), ConflictReviewChoice("b", "B")),
        selection_mode="single",
    ),
    parent=window,
)
if response.action != "cancel":
    raise SystemExit(f"unexpected response: {response}")
app.processEvents()
remaining = set(visible_top_levels())
if remaining != {int(window.winId())}:
    raise SystemExit(f"visible residue after conflict dialog: {remaining}")
window.close()
app.processEvents()
'''
        env = os.environ.copy()
        env["QT_QPA_PLATFORM"] = "windows"
        env["PYTHONPATH"] = str(SRC)
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

    def test_dialog_styles_keep_body_opaque_and_scrollbar_track_transparent(self):
        style = dialog_port_module.ORGANIZER_DIALOG_STYLE_SHEET

        self.assertIn("QFrame#dialog_body {\n        background: #f5f7f6;", style)
        self.assertIn("QLabel#dialog_message {\n        background: #f5f7f6;", style)
        self.assertIn(
            "QScrollArea#dialog_message_scroll,\n"
            "    QScrollArea#attribution_body_scroll,\n"
            "    QScrollArea#attribution_picker_scroll {\n"
            "        background: #f5f7f6;",
            style,
        )
        self.assertIn(
            "QScrollArea#dialog_message_scroll > QWidget#qt_scrollarea_viewport,\n"
            "    QScrollArea#attribution_body_scroll > QWidget#qt_scrollarea_viewport,\n"
            "    QScrollArea#attribution_picker_scroll > QWidget#qt_scrollarea_viewport {\n"
            "        background: #f5f7f6;",
            style,
        )
        self.assertNotIn("QScrollArea#dialog_message_scroll QWidget", style)
        self.assertNotIn("QScrollArea#attribution_body_scroll QWidget", style)
        self.assertIn("QScrollBar:vertical {\n        background: transparent;", style)

    def test_validation_error_style_uses_a_structural_warning_container(self):
        style = dialog_port_module.ORGANIZER_DIALOG_STYLE_SHEET
        selector = style.split("QLabel#dialog_error_text {", 1)[1].split("}", 1)[0]

        self.assertIn("background:", selector)
        self.assertIn("border:", selector)
        self.assertIn("padding:", selector)

    def test_generic_dialog_wraps_unbroken_message_without_gray_gutter(self):
        script = r'''
from PySide6 import QtCore, QtGui, QtWidgets
from spectrum_organizer.ui.dialog_port import show_styled_dialog
from spectrum_organizer.ui.dialogs import DialogRequest

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
token = "S" * 500 + ".opju"
expected = "原始文件：" + token

def inspect():
    dialog = app.activeModalWidget()
    scroll = dialog.findChild(QtWidgets.QScrollArea, "dialog_message_scroll")
    message = dialog.findChild(QtWidgets.QLabel, "dialog_message")
    if scroll is None or message is None:
        raise SystemExit("generic dialog message controls are missing")
    app.processEvents()
    if message.text() != expected:
        raise SystemExit("generic dialog message text changed")
    if scroll.horizontalScrollBar().maximum() != 0:
        raise SystemExit(
            f"generic dialog horizontal range={scroll.horizontalScrollBar().maximum()}"
        )
    if message.width() > scroll.viewport().width():
        raise SystemExit(
            f"generic message wider than viewport: {message.width()} > {scroll.viewport().width()}"
        )
    if message.height() < 2 * message.fontMetrics().lineSpacing():
        raise SystemExit(f"generic message did not wrap: height={message.height()}")
    if message.textInteractionFlags() & QtCore.Qt.TextInteractionFlag.TextSelectableByMouse:
        raise SystemExit("custom-painted generic message advertises unsupported text selection")
    for widget, label in ((scroll.viewport(), "viewport"), (message, "message")):
        color = widget.palette().color(QtGui.QPalette.ColorRole.Window).name().lower()
        if color != "#f5f7f6":
            raise SystemExit(f"generic {label} surface={color}, expected #f5f7f6")
    dialog.reject()

QtCore.QTimer.singleShot(100, inspect)
show_styled_dialog(
    DialogRequest(
        kind="inspect",
        title="检查原始文件",
        message=expected,
        actions=("continue",),
    )
)
'''
        completed = _run_qt_script(script, scale_factor="1.5")
        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)

    def test_generic_dialog_keeps_actions_reachable_on_compact_scaled_screen(self):
        script = r'''
from PySide6 import QtCore, QtWidgets
from spectrum_organizer.ui.dialog_port import show_styled_dialog
from spectrum_organizer.ui.dialogs import DialogRequest

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

def inspect():
    dialog = app.activeModalWidget()
    available = dialog.screen().availableGeometry()
    if not available.contains(dialog.frameGeometry()):
        raise SystemExit(
            f"generic dialog outside available geometry: "
            f"{dialog.frameGeometry().getRect()} not in {available.getRect()}"
        )
    action_buttons = [
        button
        for button in dialog.findChildren(QtWidgets.QPushButton)
        if button.objectName().startswith("dialog_button_")
    ]
    if len(action_buttons) != 2:
        raise SystemExit(f"expected two action buttons, found {len(action_buttons)}")
    for button in action_buttons:
        top_left = button.mapToGlobal(QtCore.QPoint(0, 0))
        button_rect = QtCore.QRect(top_left, button.size())
        if not button.isVisible() or not available.contains(button_rect):
            raise SystemExit(
                f"action button is unreachable: {button.text()} {button_rect.getRect()}"
            )
    dialog.reject()

QtCore.QTimer.singleShot(100, inspect)
show_styled_dialog(
    DialogRequest(
        kind="inspect",
        title="检查原始文件",
        message="需要完整显示的说明。" * 100,
        actions=("continue", "cancel"),
    )
)
'''
        completed = _run_qt_script(script, scale_factor="2")
        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)

    def test_final_review_semantic_delegate_preserves_ascii_exponents_and_delimiters(self):
        script = r'''
import json
from PySide6 import QtCore, QtWidgets
from spectrum_organizer.ui.dialog_port import show_styled_dialog
from spectrum_organizer.ui.dialogs import (
    FinalReviewRow,
    final_attribution_summary_dialog,
)

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
observed = {}

def inspect():
    dialog = app.activeModalWidget()
    try:
        table = dialog.findChild(QtWidgets.QTableWidget, "final_review_table")
        delegate = table.itemDelegate()
        metrics = table.fontMetrics()
        exponent_probe = "Sample-Part-1e-4 M-Tail"
        delimiter_probe = "缺少：A；；B"
        observed.update(
            exponent_lines=list(
                delegate._semantic_lines(
                    exponent_probe,
                    metrics,
                    metrics.horizontalAdvance("Part-1e-"),
                )
            ),
            delimiter_lines=list(
                delegate._semantic_lines(
                    delimiter_probe,
                    metrics,
                    metrics.horizontalAdvance("缺少：A；"),
                )
            ),
        )
    finally:
        dialog.reject()

QtCore.QTimer.singleShot(100, inspect)
show_styled_dialog(
    final_attribution_summary_dialog(
        (
            FinalReviewRow(
                "book-1",
                "source.opju",
                "Folder",
                "Book",
                "Sample",
                "将写入输出计划",
                True,
            ),
        ),
        recognized_count=1,
        rejected_count=0,
        excluded_count=0,
        accepted_count=1,
    )
)
print(json.dumps(observed, ensure_ascii=False))
'''
        completed = _run_qt_script(script, scale_factor="1.5")

        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        observed = __import__("json").loads(completed.stdout.strip())
        self.assertEqual(
            "Sample-Part-1e-4 M-Tail",
            "".join(observed["exponent_lines"]),
        )
        self.assertTrue(
            any("1e-4 M" in line for line in observed["exponent_lines"]),
            observed["exponent_lines"],
        )
        self.assertEqual(
            "缺少：A；；B",
            "".join(observed["delimiter_lines"]),
        )

    def test_final_review_semantic_delegate_keeps_extreme_text_complete(self):
        script = r'''
import json
from PySide6 import QtCore, QtWidgets
from spectrum_organizer.ui.dialog_port import show_styled_dialog
from spectrum_organizer.ui.dialogs import (
    FinalReviewRow,
    final_attribution_summary_dialog,
)

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
observed = {}
long_value = "Q" * 200
long_attribution = "温度：" + long_value
long_book = "浓度：" + long_value

def inspect():
    dialog = app.activeModalWidget()
    try:
        table = dialog.findChild(QtWidgets.QTableWidget, "final_review_table")
        delegate = table.itemDelegate()
        metrics = table.fontMetrics()
        attribution_width = max(1, table.columnWidth(2) - 12)
        book_width = max(1, table.columnWidth(1) - 12)
        attribution_lines = delegate._semantic_lines(
            long_attribution,
            metrics,
            attribution_width,
        )
        book_lines = delegate._semantic_lines(
            long_book,
            metrics,
            book_width,
        )
        exponent_probe = "Sample-Part-1e-4 M-Tail"
        exponent_width = metrics.horizontalAdvance("Part-1e-")
        exponent_lines = delegate._semantic_lines(
            exponent_probe,
            metrics,
            exponent_width,
        )
        delimiter_probe = "缺少：A；；B"
        delimiter_lines = delegate._semantic_lines(
            delimiter_probe,
            metrics,
            metrics.horizontalAdvance("缺少：A；"),
        )
        required_line_count = max(
            len(attribution_lines),
            len(book_lines) + 1,
        )
        required_height = (
            metrics.lineSpacing() * required_line_count
            + 4 * max(0, required_line_count - 1)
            + 14
        )
        observed.update(
            attribution_lines=list(attribution_lines),
            attribution_width=attribution_width,
            attribution_line_widths=[
                metrics.horizontalAdvance(line)
                for line in attribution_lines
            ],
            book_lines=list(book_lines),
            book_width=book_width,
            book_line_widths=[
                metrics.horizontalAdvance(line)
                for line in book_lines
            ],
            row_height=table.rowHeight(0),
            required_height=required_height,
            exponent_lines=list(exponent_lines),
            delimiter_lines=list(delimiter_lines),
        )
    finally:
        dialog.reject()

QtCore.QTimer.singleShot(250, inspect)
show_styled_dialog(
    final_attribution_summary_dialog(
        (
            FinalReviewRow(
                "book-long",
                "source.opju",
                "Folder_with_a_long_but_semantically_wrappable_identity",
                long_book,
                long_attribution,
                "将写入输出计划",
                True,
            ),
        ),
        recognized_count=1,
        rejected_count=0,
        excluded_count=0,
        accepted_count=1,
    )
)
print(json.dumps(observed, ensure_ascii=False))
'''
        completed = _run_qt_script(script, scale_factor="1.5")

        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        observed = __import__("json").loads(completed.stdout.strip())
        self.assertEqual(
            "温度：" + "Q" * 200,
            "".join(observed["attribution_lines"]),
        )
        self.assertLessEqual(
            max(observed["attribution_line_widths"]),
            observed["attribution_width"],
        )
        self.assertEqual(
            "浓度：" + "Q" * 200,
            "".join(observed["book_lines"]),
        )
        self.assertLessEqual(
            max(observed["book_line_widths"]),
            observed["book_width"],
        )
        self.assertGreaterEqual(
            observed["row_height"],
            observed["required_height"],
        )
        self.assertEqual(
            "Sample-Part-1e-4 M-Tail",
            "".join(observed["exponent_lines"]),
        )
        self.assertTrue(
            any("1e-4 M" in line for line in observed["exponent_lines"]),
            observed["exponent_lines"],
        )
        self.assertEqual(
            "缺少：A；；B",
            "".join(observed["delimiter_lines"]),
        )

    def test_final_output_plan_dialog_renders_all_review_sections_and_explicit_actions(self):
        long_unbroken = "Q" * 96
        long_attribution = (
            "MFL-Extended-Solution-Sample-" + long_unbroken + "-"
            + "1×10^-4 M-298 K"
        )
        long_output_folder = (
            "F_Ex270_ExSlit2_EmSlit2_" + long_unbroken
        )
        long_output_book = (
            "Series_With_Extended_Book_Identity-" + long_unbroken + "-"
            + "1×10^-4 M-298 K"
        )
        long_missing_item = (
            "PFL_With_A_Deliberately_Long_Sample_Identity-"
            + long_unbroken
            + "-"
            + "1×10^-4 M-298 K"
        )
        script = r'''
import json
from PySide6 import QtCore, QtGui, QtWidgets
from spectrum_organizer.ui.dialog_port import show_styled_dialog
from spectrum_organizer.ui.dialogs import (
    FinalReviewOutputBook,
    FinalReviewOutputFolder,
    FinalReviewRow,
    final_attribution_summary_dialog,
)

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
observed = {}
long_unbroken = "Q" * 96
long_attribution = (
    "MFL-Extended-Solution-Sample-" + long_unbroken + "-"
    + "1×10^-4 M-298 K"
)
long_output_folder = (
    "F_Ex270_ExSlit2_EmSlit2_" + long_unbroken
)
long_output_book = (
    "Series_With_Extended_Book_Identity-" + long_unbroken + "-"
    + "1×10^-4 M-298 K"
)
long_missing_item = (
    "PFL_With_A_Deliberately_Long_Sample_Identity-"
    + long_unbroken
    + "-"
    + "1×10^-4 M-298 K"
)

def inspect():
    dialog = app.activeModalWidget()
    tabs = dialog.findChild(QtWidgets.QTabWidget, "final_review_tabs")
    table = dialog.findChild(QtWidgets.QTableWidget, "final_review_table")
    output_tree = dialog.findChild(QtWidgets.QTreeWidget, "final_review_output_tree")
    search = dialog.findChild(QtWidgets.QLineEdit, "final_review_search")
    count = dialog.findChild(QtWidgets.QLabel, "final_review_search_count")
    up = dialog.findChild(QtWidgets.QToolButton, "final_review_search_up")
    down = dialog.findChild(QtWidgets.QToolButton, "final_review_search_down")
    modify_attribution = dialog.findChild(
        QtWidgets.QPushButton,
        "final_review_modify_attribution",
    )
    modify_conflicts = dialog.findChild(
        QtWidgets.QPushButton,
        "final_review_modify_conflicts",
    )
    confirm = dialog.findChild(QtWidgets.QPushButton, "final_review_confirm")
    cancel = dialog.findChild(QtWidgets.QPushButton, "final_review_cancel")
    root_footer = dialog.findChild(
        QtWidgets.QFrame,
        "conflict_review_footer",
    )
    size_grip = dialog.findChild(
        QtWidgets.QSizeGrip,
        "final_review_size_grip",
    )
    initial = {
        "selected": table.currentRow(),
        "modify_attribution": modify_attribution.isVisible(),
        "modify_conflicts": modify_conflicts.isVisible(),
    }
    table.setCurrentCell(2, 0)
    table.selectRow(2)
    app.processEvents()
    rejected_actions = (
        modify_attribution.isVisible(),
        modify_conflicts.isVisible(),
    )
    search.setText("MFL")
    app.processEvents()
    first_match = (table.currentRow(), count.text(), table.rowCount())
    down.click()
    app.processEvents()
    second_match = (table.currentRow(), count.text(), table.rowCount())
    down.click()
    app.processEvents()
    wrapped_match = (table.currentRow(), count.text(), table.rowCount())
    selected_actions = (
        modify_attribution.isVisible(),
        modify_conflicts.isVisible(),
    )
    search.setText("not-present")
    app.processEvents()
    no_match = (count.text(), up.isEnabled(), down.isEnabled(), table.rowCount())
    tabs.setCurrentIndex(1)
    app.processEvents()
    output_folder = output_tree.topLevelItem(0)
    def render_folder_with_ambient_font(font):
        image = QtGui.QImage(700, 90, QtGui.QImage.Format.Format_ARGB32)
        image.fill(QtGui.QColor("#ffffff"))
        painter = QtGui.QPainter(image)
        painter.setFont(font)
        option = QtWidgets.QStyleOptionViewItem()
        option.initFrom(output_tree)
        option.widget = output_tree
        option.rect = QtCore.QRect(0, 0, 680, 70)
        output_tree.itemDelegate().paint(
            painter,
            option,
            output_tree.indexFromItem(output_folder, 0),
        )
        painter.end()
        return bytes(image.bits())
    styled_folder_font = output_folder.font(0)
    alien_folder_font = QtGui.QFont(styled_folder_font)
    alien_folder_font.setWeight(QtGui.QFont.Weight.Thin)
    alien_folder_font.setPixelSize(31)
    folder_font_paint_is_ambient_invariant = (
        render_folder_with_ambient_font(styled_folder_font)
        == render_folder_with_ambient_font(alien_folder_font)
    )
    output_folder_identity_role = int(QtCore.Qt.ItemDataRole.UserRole) + 2
    output_children = tuple(
        output_folder.child(index)
        for index in range(output_folder.childCount())
    )
    output_audit = next(
        (
            item
            for item in output_children
            if item.isFirstColumnSpanned()
        ),
        output_folder,
    )
    output_item = next(
        (item for item in output_children if item.text(1)),
        output_folder,
    )
    output_text = output_item.text(2)
    output_required_height = (
        output_tree.fontMetrics().boundingRect(
            QtCore.QRect(
                0,
                0,
                max(1, output_tree.columnWidth(2) - 12),
                10000,
            ),
            QtCore.Qt.TextFlag.TextWrapAnywhere,
            output_text,
        ).height()
        + 12
    )
    output_actual_height = output_tree.visualItemRect(output_item).height()
    output_missing_text = output_audit.text(0)
    output_audit_rect = output_tree.visualItemRect(output_audit)
    output_missing_lines = output_tree.itemDelegate()._semantic_lines(
        output_missing_text,
        output_tree.fontMetrics(),
        max(1, output_audit_rect.width() - 12),
    )
    output_missing_required_height = (
        output_tree.fontMetrics().lineSpacing() * len(output_missing_lines)
        + 4 * max(0, len(output_missing_lines) - 1)
        + 14
    )
    output_missing_actual_height = output_audit_rect.height()
    output_book_lines = output_tree.itemDelegate()._semantic_lines(
        output_item.text(1),
        output_tree.fontMetrics(),
        max(1, output_tree.columnWidth(1) - 12),
    )
    output_folder_rect = output_tree.visualItemRect(output_folder)
    output_folder_lines = output_tree.itemDelegate()._semantic_lines(
        output_folder.text(0),
        output_tree.fontMetrics(),
        max(1, output_folder_rect.width() - 12),
    )
    output_folder_required_height = (
        output_tree.fontMetrics().lineSpacing() * len(output_folder_lines)
        + 4 * max(0, len(output_folder_lines) - 1)
        + 14
    )
    semantic_probe = (
        "缺少：PFL-Solid-With-A-Long-Identity-Air-77 K；"
        "2-mTHF-1×10^-4 M-298 K"
    )
    semantic_units = (
        "PFL-Solid-With-A-Long-Identity-Air-77 K；",
        "2-mTHF-1×10^-4 M-298 K",
    )
    semantic_probe_lines = output_tree.itemDelegate()._semantic_lines(
        semantic_probe,
        output_tree.fontMetrics(),
        max(
            output_tree.fontMetrics().horizontalAdvance(unit)
            for unit in semantic_units
        ),
    )
    output_tab_actions = (
        modify_attribution.isVisible(),
        modify_conflicts.isVisible(),
    )
    tabs.setCurrentIndex(0)
    app.processEvents()
    attribution_text = table.item(0, 2).text()
    attribution_lines = table.itemDelegate()._semantic_lines(
        attribution_text,
        table.fontMetrics(),
        max(1, table.columnWidth(2) - 12),
    )
    table_required_height = max(
        table.fontMetrics().boundingRect(
            QtCore.QRect(
                0,
                0,
                max(1, table.columnWidth(column) - 12),
                10000,
            ),
            QtCore.Qt.TextFlag.TextWrapAnywhere,
            table.item(0, column).text(),
        ).height()
        + 12
        for column in range(table.columnCount())
    )
    geometry = dialog.screen().availableGeometry()
    action_y = {
        "modify": modify_attribution.mapTo(dialog, QtCore.QPoint(0, 0)).y(),
        "conflicts": modify_conflicts.mapTo(dialog, QtCore.QPoint(0, 0)).y(),
        "confirm": confirm.mapTo(dialog, QtCore.QPoint(0, 0)).y(),
        "cancel": cancel.mapTo(dialog, QtCore.QPoint(0, 0)).y(),
    }
    observed.update(
        title=dialog.windowTitle(),
        tabs=[tabs.tabText(index) for index in range(tabs.count())],
        headers=[table.horizontalHeaderItem(index).text() for index in range(4)],
        initial=initial,
        rejected_actions=rejected_actions,
        first_match=first_match,
        second_match=second_match,
        wrapped_match=wrapped_match,
        selected_actions=selected_actions,
        no_match=no_match,
        output_tab_actions=output_tab_actions,
        complete_text=table.item(0, 1).text(),
        horizontal_range=table.horizontalScrollBar().maximum(),
        output_top_levels=output_tree.topLevelItemCount(),
        output_children=output_folder.childCount(),
        output_expanded=output_tree.topLevelItem(0).isExpanded(),
        output_columns=output_item.text(2),
        output_folder_status=output_folder.text(3),
        output_folder_display=output_folder.text(0),
        output_folder_identity=output_folder.data(
            0,
            output_folder_identity_role,
        ),
        output_folder_lines=list(output_folder_lines),
        output_folder_line_widths=[
            output_tree.fontMetrics().horizontalAdvance(line)
            for line in output_folder_lines
        ],
        output_folder_available_width=max(1, output_folder_rect.width() - 12),
        output_folder_height=output_tree.visualItemRect(output_folder).height(),
        output_folder_required_height=output_folder_required_height,
        output_folder_bold=output_folder.font(0).bold(),
        folder_font_paint_is_ambient_invariant=(
            folder_font_paint_is_ambient_invariant
        ),
        output_folder_background=output_folder.background(0).style().name,
        output_folder_background_color=output_folder.background(0).color().name(),
        output_folder_foreground_color=output_folder.foreground(0).color().name(),
        output_folder_spanned=output_folder.isFirstColumnSpanned(),
        output_folder_width=output_folder_rect.width(),
        output_audit_first=output_folder.child(0).isFirstColumnSpanned(),
        output_audit_spanned=output_audit.isFirstColumnSpanned(),
        output_audit_width=output_audit_rect.width(),
        output_book_name=output_item.text(1),
        output_book_lines=list(output_book_lines),
        output_book_line_widths=[
            output_tree.fontMetrics().horizontalAdvance(line)
            for line in output_book_lines
        ],
        output_book_available_width=max(1, output_tree.columnWidth(1) - 12),
        output_book_top_aligned=bool(
            output_item.textAlignment(1)
            & QtCore.Qt.AlignmentFlag.AlignTop
        ),
        attribution_text=attribution_text,
        attribution_lines=list(attribution_lines),
        attribution_line_widths=[
            table.fontMetrics().horizontalAdvance(line)
            for line in attribution_lines
        ],
        attribution_available_width=max(1, table.columnWidth(2) - 12),
        complete_geometry={
            "table_actual": table.rowHeight(0),
            "table_required": table_required_height,
            "output_actual": output_actual_height,
            "output_required": output_required_height,
            "missing_actual": output_missing_actual_height,
            "missing_required": output_missing_required_height,
            "output_horizontal": output_tree.horizontalScrollBar().maximum(),
        },
        output_column_widths=[
            output_tree.columnWidth(column)
            for column in range(output_tree.columnCount())
        ],
        output_viewport_width=output_tree.viewport().width(),
        output_missing=output_missing_text,
        output_missing_lines=list(output_missing_lines),
        output_missing_line_widths=[
            output_tree.fontMetrics().horizontalAdvance(line)
            for line in output_missing_lines
        ],
        output_missing_available_width=max(1, output_audit_rect.width() - 12),
        semantic_probe_lines=list(semantic_probe_lines),
        source_divider=bool(
            table.item(2, 0).data(
                int(QtCore.Qt.ItemDataRole.UserRole) + 1
            )
        ),
        output_warning_background=(
            output_audit.background(0).color().name()
        ),
        output_warning_foreground=(
            output_audit.foreground(0).color().name()
        ),
        search_placeholder=search.placeholderText(),
        up_tip=up.toolTip(),
        down_tip=down.toolTip(),
        confirm_visible=confirm.isVisible(),
        cancel_visible=cancel.isVisible(),
        topmost=bool(
            dialog.windowFlags()
            & QtCore.Qt.WindowType.WindowStaysOnTopHint
        ),
        search_control_heights=(
            search.height(),
            count.height(),
            up.height(),
            down.height(),
        ),
        search_font_pixels=(
            search.font().pixelSize(),
            count.font().pixelSize(),
        ),
        search_arrow_types=(up.arrowType().name, down.arrowType().name),
        root_actions_in_footer=all(
            root_footer.isAncestorOf(button)
            for button in (
                modify_attribution,
                modify_conflicts,
                confirm,
                cancel,
            )
        ),
        root_action_y=action_y,
        width_ratio=dialog.width() / geometry.width(),
        height_ratio=dialog.height() / geometry.height(),
        size_grip={
            "present": size_grip is not None,
            "visible": bool(size_grip and size_grip.isVisible()),
            "right_gap": (
                dialog.width() - size_grip.geometry().right() - 1
                if size_grip is not None
                else -1
            ),
            "bottom_gap": (
                dialog.height() - size_grip.geometry().bottom() - 1
                if size_grip is not None
                else -1
            ),
            "tooltip": size_grip.toolTip() if size_grip is not None else "",
        },
    )
    dialog.reject()

QtCore.QTimer.singleShot(100, inspect)
show_styled_dialog(
    final_attribution_summary_dialog(
        (
            FinalReviewRow(
                "book-1",
                "source_with_a_deliberately_long_segment_without_spaces_20260731.opju",
                "Root\\Folder_with_a_deliberately_long_segment_without_spaces\\Subfolder_with_another_long_segment",
                "Book_with_a_deliberately_long_name_for_complete_display",
                long_attribution,
                "将写入输出计划",
                True,
            ),
            FinalReviewRow(
                "book-2",
                "source-a.opju",
                "Folder A",
                "Book B",
                "PFL-Solid-Air-298 K",
                "不输出：用户未选择",
            ),
            FinalReviewRow(
                "book-3",
                "source-b.opju",
                "Folder B",
                "MFL Book C with a complete unelided long name",
                "—",
                "拒绝，不输出：Note 缺少测试条件",
                False,
                False,
            ),
        ),
        recognized_count=3,
        rejected_count=0,
        excluded_count=1,
        accepted_count=2,
        output_folders=(
            FinalReviewOutputFolder(
                long_output_folder,
                (
                    FinalReviewOutputBook(
                        long_output_book,
                        tuple(
                            f"列 {index} [Data] · Comment=Em270 · LongName=Sample_with_complete_column_metadata_{index}"
                            for index in range(1, 13)
                        ),
                    ),
                ),
                (
                    long_missing_item,
                    "PFL-Solid-Air-298 K",
                    "MFL-Solid-Air-77 K",
                    "MFL-Solid-Air-298 K",
                    "1-mTHF-1×10^-4 M-77 K",
                    "2-mTHF-1×10^-4 M-298 K",
                    "4-mTHF-1×10^-7 M-298 K",
                ),
            ),
        ),
    )
)
print(json.dumps(observed, ensure_ascii=False))
'''
        completed = _run_qt_script(script, scale_factor="1.25")

        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        observed = __import__("json").loads(completed.stdout.strip())
        self.assertEqual("最终归属与输出计划", observed["title"])
        self.assertEqual(["最终归属", "输出结构"], observed["tabs"])
        self.assertEqual(
            ["来源文件", "原 Folder / Book", "最终样品归属", "结果 / 原因"],
            observed["headers"],
        )
        self.assertEqual(
            {
                "selected": -1,
                "modify_attribution": False,
                "modify_conflicts": False,
            },
            observed["initial"],
        )
        self.assertEqual([False, False], observed["rejected_actions"])
        self.assertEqual([0, "1 / 2", 3], observed["first_match"])
        self.assertEqual([2, "2 / 2", 3], observed["second_match"])
        self.assertEqual([0, "1 / 2", 3], observed["wrapped_match"])
        self.assertEqual([True, True], observed["selected_actions"])
        self.assertEqual(["0 / 0", False, False, 3], observed["no_match"])
        self.assertEqual([False, False], observed["output_tab_actions"])
        self.assertEqual(
            "Root\\Folder_with_a_deliberately_long_segment_without_spaces\\Subfolder_with_another_long_segment\n"
            "Book_with_a_deliberately_long_name_for_complete_display",
            observed["complete_text"],
        )
        self.assertEqual(0, observed["horizontal_range"])
        self.assertEqual(1, observed["output_top_levels"])
        self.assertEqual(2, observed["output_children"])
        self.assertTrue(observed["output_expanded"])
        self.assertIn("列 12 [Data]", observed["output_columns"])
        self.assertEqual("缺少 7 项", observed["output_folder_status"])
        self.assertEqual(long_output_folder, observed["output_folder_identity"])
        self.assertIn(long_output_folder, observed["output_folder_display"])
        self.assertIn("缺少 7 项", observed["output_folder_display"])
        self.assertEqual(
            "".join(observed["output_folder_display"].split()),
            "".join("".join(observed["output_folder_lines"]).split()),
        )
        self.assertGreater(
            len(observed["output_folder_lines"]),
            1,
        )
        self.assertLessEqual(
            max(observed["output_folder_line_widths"]),
            observed["output_folder_available_width"],
        )
        self.assertGreaterEqual(
            observed["output_folder_height"],
            observed["output_folder_required_height"],
        )
        self.assertTrue(observed["output_folder_bold"])
        self.assertTrue(
            observed["folder_font_paint_is_ambient_invariant"],
            "semantic delegate used the ambient painter font instead of styled.font",
        )
        self.assertNotEqual("NoBrush", observed["output_folder_background"])
        self.assertEqual("#eaf3f0", observed["output_folder_background_color"])
        self.assertEqual("#0f655c", observed["output_folder_foreground_color"])
        self.assertTrue(observed["output_folder_spanned"])
        self.assertGreaterEqual(
            observed["output_folder_width"],
            round(observed["output_viewport_width"] * 0.90),
        )
        self.assertTrue(observed["output_audit_first"])
        self.assertTrue(observed["output_audit_spanned"])
        self.assertGreaterEqual(
            observed["output_audit_width"],
            round(observed["output_viewport_width"] * 0.90),
        )
        self.assertEqual(long_output_book, observed["output_book_name"])
        self.assertEqual(
            long_output_book,
            "".join(observed["output_book_lines"]),
        )
        self.assertGreater(len(observed["output_book_lines"]), 1)
        self.assertLessEqual(
            max(observed["output_book_line_widths"]),
            observed["output_book_available_width"],
        )
        self.assertTrue(
            any("1×10^-4 M" in line for line in observed["output_book_lines"]),
            observed["output_book_lines"],
        )
        self.assertTrue(
            any("298 K" in line for line in observed["output_book_lines"]),
            observed["output_book_lines"],
        )
        self.assertTrue(observed["output_book_top_aligned"])
        self.assertEqual(long_attribution, observed["attribution_text"])
        self.assertEqual(
            long_attribution,
            "".join(observed["attribution_lines"]),
        )
        self.assertLessEqual(
            max(observed["attribution_line_widths"]),
            observed["attribution_available_width"],
        )
        self.assertTrue(
            any("1×10^-4 M" in line for line in observed["attribution_lines"]),
            observed["attribution_lines"],
        )
        self.assertTrue(
            any("298 K" in line for line in observed["attribution_lines"]),
            observed["attribution_lines"],
        )
        self.assertGreaterEqual(
            observed["complete_geometry"]["table_actual"],
            observed["complete_geometry"]["table_required"],
        )
        self.assertGreaterEqual(
            observed["complete_geometry"]["output_actual"],
            observed["complete_geometry"]["output_required"],
        )
        self.assertGreaterEqual(
            observed["complete_geometry"]["missing_actual"],
            observed["complete_geometry"]["missing_required"],
        )
        self.assertEqual(0, observed["complete_geometry"]["output_horizontal"])
        output_widths = observed["output_column_widths"]
        self.assertGreaterEqual(
            output_widths[2],
            round(observed["output_viewport_width"] * 0.38),
        )
        self.assertLessEqual(
            output_widths[0] + output_widths[1] + output_widths[3],
            round(observed["output_viewport_width"] * 0.62),
        )
        self.assertLessEqual(
            output_widths[0],
            round(observed["output_viewport_width"] * 0.20),
        )
        self.assertEqual(
            "缺少：" + long_missing_item + "；PFL-Solid-Air-298 K；"
            "MFL-Solid-Air-77 K；MFL-Solid-Air-298 K；"
            "1-mTHF-1×10^-4 M-77 K；2-mTHF-1×10^-4 M-298 K；"
            "4-mTHF-1×10^-7 M-298 K",
            observed["output_missing"],
        )
        self.assertEqual(
            observed["output_missing"],
            "".join(observed["output_missing_lines"]),
        )
        self.assertLessEqual(
            max(observed["output_missing_line_widths"]),
            observed["output_missing_available_width"],
            observed["output_missing_lines"],
        )
        self.assertTrue(
            any("1×10^-4 M" in line for line in observed["output_missing_lines"]),
            observed["output_missing_lines"],
        )
        self.assertTrue(
            any("298 K" in line for line in observed["output_missing_lines"]),
            observed["output_missing_lines"],
        )
        self.assertEqual(
            [
                "缺少：",
                "PFL-Solid-With-A-Long-Identity-Air-77 K；",
                "2-mTHF-1×10^-4 M-298 K",
            ],
            observed["semantic_probe_lines"],
        )
        self.assertTrue(observed["source_divider"])
        self.assertEqual("#fff3d6", observed["output_warning_background"])
        self.assertEqual("#8a5a00", observed["output_warning_foreground"])
        self.assertEqual(
            "搜索来源、Folder、Book、样品或原因",
            observed["search_placeholder"],
        )
        self.assertEqual("上一个匹配项", observed["up_tip"])
        self.assertEqual("下一个匹配项", observed["down_tip"])
        self.assertTrue(observed["confirm_visible"])
        self.assertTrue(observed["cancel_visible"])
        self.assertFalse(observed["topmost"])
        self.assertLessEqual(
            max(observed["search_control_heights"])
            - min(observed["search_control_heights"]),
            2,
            observed["search_control_heights"],
        )
        self.assertEqual(
            observed["search_font_pixels"][0],
            observed["search_font_pixels"][1],
        )
        self.assertEqual(
            ["NoArrow", "NoArrow"],
            observed["search_arrow_types"],
        )
        self.assertTrue(observed["root_actions_in_footer"])
        self.assertLessEqual(
            len(set(observed["root_action_y"].values())),
            2,
        )
        self.assertGreaterEqual(observed["width_ratio"], 0.85)
        self.assertLessEqual(observed["width_ratio"], 0.98)
        self.assertGreaterEqual(observed["height_ratio"], 0.50)
        self.assertLessEqual(observed["height_ratio"], 0.68)
        self.assertEqual(
            {
                "present": True,
                "visible": True,
                "right_gap": 0,
                "bottom_gap": 0,
                "tooltip": "拖动调整窗口大小",
            },
            observed["size_grip"],
        )

    def test_final_review_low_information_root_uses_content_bounded_height(self):
        script = r'''
import json
from PySide6 import QtCore, QtWidgets
from spectrum_organizer.ui.dialog_port import show_styled_dialog
from spectrum_organizer.ui.dialogs import (
    FinalReviewRow,
    final_attribution_summary_dialog,
)

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
observed = {}

def inspect():
    dialog = app.activeModalWidget()
    table = dialog.findChild(
        QtWidgets.QTableWidget,
        "final_review_table",
    )
    geometry = dialog.screen().availableGeometry()
    row_height = table.visualItemRect(table.item(0, 0)).height()
    observed.update(
        height_ratio=dialog.height() / geometry.height(),
        unused_table_height=max(
            0,
            table.viewport().height() - row_height,
        ),
        row_height=row_height,
        viewport_height=table.viewport().height(),
    )
    dialog.findChild(
        QtWidgets.QPushButton,
        "final_review_cancel",
    ).click()

QtCore.QTimer.singleShot(150, inspect)
show_styled_dialog(
    final_attribution_summary_dialog(
        (
            FinalReviewRow(
                "book-a",
                "source.opju",
                "Folder",
                "Book",
                "Sample-Solid-Air-298 K",
                "将写入输出计划",
                True,
            ),
        ),
        recognized_count=1,
        rejected_count=0,
        excluded_count=0,
        accepted_count=1,
    )
)
print(json.dumps(observed, ensure_ascii=False))
'''
        completed = _run_qt_script(script)

        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        observed = __import__("json").loads(completed.stdout.strip())
        self.assertGreater(observed["row_height"], 0)
        self.assertLessEqual(observed["height_ratio"], 0.60)
        self.assertLessEqual(
            observed["unused_table_height"],
            120,
            observed,
        )

    def test_final_review_table_enter_selects_only_and_never_triggers_footer_action(self):
        script = r'''
import json
from PySide6 import QtCore, QtTest, QtWidgets
from spectrum_organizer.ui.dialog_port import show_styled_dialog
from spectrum_organizer.ui.dialogs import (
    FinalReviewRow,
    final_attribution_summary_dialog,
)

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
observed = {}

def inspect():
    dialog = app.activeModalWidget()
    table = dialog.findChild(QtWidgets.QTableWidget, "final_review_table")
    modify = dialog.findChild(
        QtWidgets.QPushButton,
        "final_review_modify_attribution",
    )
    table.setCurrentCell(0, 0)
    table.selectRow(0)
    table.setFocus()
    app.processEvents()
    QtTest.QTest.keyClick(table, QtCore.Qt.Key.Key_Return)
    app.processEvents()
    observed.update(
        dialog_visible=dialog.isVisible(),
        selected_row=table.currentRow(),
        modify_visible=modify.isVisible(),
    )
    if dialog.isVisible():
        dialog.findChild(
            QtWidgets.QPushButton,
            "final_review_cancel",
        ).click()

QtCore.QTimer.singleShot(100, inspect)
response = show_styled_dialog(
    final_attribution_summary_dialog(
        (
            FinalReviewRow(
                "book-a",
                "source.opju",
                "Folder",
                "Book A",
                "MFL-Solid-Air-298 K",
                "将写入输出计划",
            ),
        ),
        recognized_count=1,
        rejected_count=0,
        excluded_count=0,
        accepted_count=1,
    )
)
observed["response"] = response.action
print(json.dumps(observed, ensure_ascii=False))
'''
        completed = _run_qt_script(script)

        self.assertEqual(
            0,
            completed.returncode,
            completed.stdout + completed.stderr,
        )
        observed = __import__("json").loads(completed.stdout.strip())
        self.assertTrue(observed["dialog_visible"])
        self.assertEqual(0, observed["selected_row"])
        self.assertTrue(observed["modify_visible"])
        self.assertEqual("cancel", observed["response"])

    def test_final_review_root_footer_reflows_without_overlap_on_compact_scaled_screen(self):
        script = r'''
import json
from PySide6 import QtCore, QtWidgets
from spectrum_organizer.ui.dialog_port import show_styled_dialog
from spectrum_organizer.ui.dialogs import (
    FinalReviewRow,
    final_attribution_summary_dialog,
)

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
observed = {}

def inspect():
    dialog = app.activeModalWidget()
    table = dialog.findChild(QtWidgets.QTableWidget, "final_review_table")
    table.setCurrentCell(0, 0)
    table.selectRow(0)
    app.processEvents()
    footer = dialog.findChild(
        QtWidgets.QFrame,
        "conflict_review_footer",
    )
    buttons = [
        dialog.findChild(QtWidgets.QPushButton, object_name)
        for object_name in (
            "final_review_modify_attribution",
            "final_review_modify_conflicts",
            "final_review_confirm",
            "final_review_cancel",
        )
    ]
    rects = [
        QtCore.QRect(
            button.mapTo(footer, QtCore.QPoint(0, 0)),
            button.size(),
        )
        for button in buttons
    ]
    observed.update(
        dialog_width=dialog.width(),
        footer_width=footer.width(),
        visible=[button.isVisible() for button in buttons],
        rows=len({rect.y() for rect in rects}),
        inside=[footer.rect().contains(rect) for rect in rects],
        overlaps=[
            rects[left].intersects(rects[right])
            for left in range(len(rects))
            for right in range(left + 1, len(rects))
        ],
        widths=[button.width() for button in buttons],
        hints=[button.sizeHint().width() for button in buttons],
    )
    dialog.reject()

QtCore.QTimer.singleShot(100, inspect)
show_styled_dialog(
    final_attribution_summary_dialog(
        (
            FinalReviewRow(
                "book-a",
                "source.opju",
                "Folder",
                "Book A",
                "MFL-Solid-Air-298 K",
                "将写入输出计划",
                True,
            ),
        ),
        recognized_count=1,
        rejected_count=0,
        excluded_count=0,
        accepted_count=1,
    )
)
print(json.dumps(observed, ensure_ascii=False))
'''
        completed = _run_qt_script(script, scale_factor="1.5")

        self.assertEqual(
            0,
            completed.returncode,
            completed.stdout + completed.stderr,
        )
        observed = __import__("json").loads(completed.stdout.strip())
        self.assertLess(observed["dialog_width"], 700)
        self.assertEqual([True] * 4, observed["visible"])
        self.assertLessEqual(observed["rows"], 2)
        self.assertEqual([True] * 4, observed["inside"])
        self.assertEqual([False] * 6, observed["overlaps"])
        self.assertEqual(observed["hints"], observed["widths"])

    def test_final_review_window_can_move_partly_offscreen_with_header_recoverable(self):
        script = r'''
import json
from PySide6 import QtCore, QtWidgets
from spectrum_organizer.ui import dialog_port as dialog_port_module
from spectrum_organizer.ui.dialog_port import show_styled_dialog
from spectrum_organizer.ui.dialogs import (
    FinalReviewRow,
    final_attribution_summary_dialog,
)

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
observed = {}

class DestinationScreen:
    def availableGeometry(self):
        return QtCore.QRect(1000, 0, 600, 500)

class FakeGuiApplication:
    @classmethod
    def screenAt(cls, _point):
        return destination_screen

class FakeQtGui:
    QGuiApplication = FakeGuiApplication

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

destination_screen = DestinationScreen()
original_drag = dialog_port_module._enable_title_bar_drag

def inject_fake_screen(header, window, qt_core, **kwargs):
    return original_drag(
        header,
        window,
        qt_core,
        FakeQtGui,
        **kwargs,
    )

dialog_port_module._enable_title_bar_drag = inject_fake_screen

def inspect():
    dialog = app.activeModalWidget()
    header = dialog.findChild(QtWidgets.QFrame, "dialog_header")
    start = dialog.frameGeometry().topLeft() + QtCore.QPoint(30, 20)
    header.mousePressEvent(PointerEvent(start, pressed=True))
    header.mouseMoveEvent(
        PointerEvent(QtCore.QPoint(1570, 80), pressed=True)
    )
    app.processEvents()
    available = destination_screen.availableGeometry()
    frame = dialog.frameGeometry()
    observed.update(
        contained=available.contains(frame),
        frame=frame.getRect(),
        available=available.getRect(),
        visible_header_width=(available.right() - frame.left() + 1),
        right_overflow=(frame.right() - available.right()),
    )
    dialog.reject()

QtCore.QTimer.singleShot(100, inspect)
show_styled_dialog(
    final_attribution_summary_dialog(
        (
            FinalReviewRow(
                "book-a",
                "source.opju",
                "Folder",
                "Book A",
                "Sample-Solid-Air-298 K",
                "将写入输出计划",
            ),
        ),
        recognized_count=1,
        rejected_count=0,
        excluded_count=0,
        accepted_count=1,
    )
)
print(json.dumps(observed, ensure_ascii=False))
'''
        completed = _run_qt_script(script)

        self.assertEqual(
            0,
            completed.returncode,
            completed.stdout + completed.stderr,
        )
        observed = __import__("json").loads(completed.stdout.strip())
        self.assertGreater(observed["right_overflow"], 0)
        self.assertGreaterEqual(observed["visible_header_width"], 64)
        self.assertLessEqual(observed["visible_header_width"], 96)

    def test_final_review_restores_selected_row_and_output_folder_by_identity(self):
        script = r'''
import json
from PySide6 import QtCore, QtWidgets
from spectrum_organizer.ui.dialog_port import show_styled_dialog
from spectrum_organizer.ui.dialogs import (
    FinalReviewOutputBook,
    FinalReviewOutputFolder,
    FinalReviewRow,
    FinalReviewViewState,
    final_attribution_summary_dialog,
)

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
observed = {}
output_folder_identity_role = int(QtCore.Qt.ItemDataRole.UserRole) + 2

def output_folder_identity(item):
    return item.data(0, output_folder_identity_role)

rows = tuple(
    FinalReviewRow(
        f"book-{index}",
        "source.opju",
        f"Folder_{index}",
        f"Book_{index}",
        "Sample-Solid-Air-298 K",
        (
            "完整结果第一行\n完整结果第二行\n完整结果第三行"
            if index < 80
            else "将写入输出计划"
        ),
    )
    for index in range(90)
)
folders = tuple(
    FinalReviewOutputFolder(
        f"Folder {index}",
        (
            FinalReviewOutputBook(
                f"Book {index}",
                tuple(f"列 {column} [Data]" for column in range(8)),
            ),
        ),
    )
    for index in range(12)
)

attribution_checks = {"remaining": 100}

def inspect_attribution():
    dialog = app.activeModalWidget()
    table = dialog.findChild(QtWidgets.QTableWidget, "final_review_table")
    item = table.item(80, 0)
    rect = table.visualItemRect(item)
    if (
        (table.currentRow() != 80 or not rect.intersects(table.viewport().rect()))
        and attribution_checks["remaining"] > 0
    ):
        attribution_checks["remaining"] -= 1
        QtCore.QTimer.singleShot(10, inspect_attribution)
        return
    observed["attribution"] = {
        "row": table.currentRow(),
        "visible": rect.intersects(table.viewport().rect()),
        "rect": rect.getRect(),
        "viewport": table.viewport().rect().getRect(),
    }
    dialog.findChild(QtWidgets.QPushButton, "final_review_cancel").click()

QtCore.QTimer.singleShot(0, inspect_attribution)
show_styled_dialog(
    final_attribution_summary_dialog(
        rows,
        recognized_count=90,
        rejected_count=0,
        excluded_count=0,
        accepted_count=90,
        output_folders=folders,
        initial_view_state=FinalReviewViewState(
            selected_row_id="book-80",
            attribution_scroll_value=0,
        ),
    )
)

output_checks = {"remaining": 100}

def inspect_output():
    dialog = app.activeModalWidget()
    tabs = dialog.findChild(QtWidgets.QTabWidget, "final_review_tabs")
    tree = dialog.findChild(QtWidgets.QTreeWidget, "final_review_output_tree")
    item = next(
        tree.topLevelItem(index)
        for index in range(tree.topLevelItemCount())
        if output_folder_identity(tree.topLevelItem(index)) == "Folder 8"
    )
    rect = tree.visualItemRect(item)
    if (
        (
            tabs.currentIndex() != 1
            or not rect.intersects(tree.viewport().rect())
        )
        and output_checks["remaining"] > 0
    ):
        output_checks["remaining"] -= 1
        QtCore.QTimer.singleShot(10, inspect_output)
        return
    observed["output"] = {
        "folder": output_folder_identity(item),
        "top": rect.top(),
        "visible": rect.intersects(tree.viewport().rect()),
        "viewport": tree.viewport().rect().getRect(),
        "scroll_value": tree.verticalScrollBar().value(),
        "scroll_maximum": tree.verticalScrollBar().maximum(),
        "tab": tabs.currentIndex(),
    }
    latest = next(
        tree.topLevelItem(index)
        for index in range(tree.topLevelItemCount())
        if output_folder_identity(tree.topLevelItem(index)) == "Folder 4"
    )
    tree.scrollToItem(
        latest,
        QtWidgets.QAbstractItemView.ScrollHint.PositionAtTop,
    )
    tree.doItemsLayout()
    tree.verticalScrollBar().setValue(
        tree.verticalScrollBar().value()
        + tree.visualItemRect(latest).top()
        + 6
    )
    app.processEvents()
    observed["latest_output"] = {
        "folder": output_folder_identity(latest),
        "top": tree.visualItemRect(latest).top(),
    }
    tabs.setCurrentIndex(0)
    dialog.findChild(QtWidgets.QPushButton, "final_review_cancel").click()

QtCore.QTimer.singleShot(0, inspect_output)
output_response = show_styled_dialog(
    final_attribution_summary_dialog(
        rows,
        recognized_count=90,
        rejected_count=0,
        excluded_count=0,
        accepted_count=90,
        output_folders=folders,
        initial_view_state=FinalReviewViewState(
            active_tab="output",
            output_scroll_value=0,
            output_anchor_folder="Folder 8",
            output_anchor_offset=-4,
        ),
    )
)
observed["output_state"] = [
    output_response.view_state.output_anchor_folder,
    output_response.view_state.output_anchor_offset,
]

reflow_folders = tuple(
    FinalReviewOutputFolder(
        f"Inserted {index}",
        tuple(
            FinalReviewOutputBook(
                f"Inserted Book {index}-{book_index}",
                ("列 1 [Data]",),
            )
            for book_index in range(6)
        ),
    )
    for index in range(8)
) + folders

hidden_output_checks = {"remaining": 100}
hidden_output_ready = {"value": False}

def inspect_hidden_output_restore():
    dialog = app.activeModalWidget()
    if not hidden_output_ready["value"]:
        hidden_output_ready["value"] = True
        QtCore.QTimer.singleShot(10, inspect_hidden_output_restore)
        return
    tabs = dialog.findChild(QtWidgets.QTabWidget, "final_review_tabs")
    tree = dialog.findChild(QtWidgets.QTreeWidget, "final_review_output_tree")
    observed["hidden_initial_tab"] = tabs.currentIndex()
    tabs.setCurrentIndex(1)

    def measure_visible_output():
        item = next(
            tree.topLevelItem(index)
            for index in range(tree.topLevelItemCount())
            if output_folder_identity(tree.topLevelItem(index)) == "Folder 4"
        )
        rect = tree.visualItemRect(item)
        if (
            not rect.intersects(tree.viewport().rect())
            and hidden_output_checks["remaining"] > 0
        ):
            hidden_output_checks["remaining"] -= 1
            QtCore.QTimer.singleShot(10, measure_visible_output)
            return
        observed["hidden_output_restore"] = {
            "top": rect.top(),
            "visible": rect.intersects(tree.viewport().rect()),
            "viewport": tree.viewport().rect().getRect(),
        }
        dialog.findChild(
            QtWidgets.QPushButton,
            "final_review_cancel",
        ).click()

    QtCore.QTimer.singleShot(0, measure_visible_output)

QtCore.QTimer.singleShot(0, inspect_hidden_output_restore)
show_styled_dialog(
    final_attribution_summary_dialog(
        rows,
        recognized_count=90,
        rejected_count=0,
        excluded_count=0,
        accepted_count=90,
        output_folders=reflow_folders,
        initial_view_state=FinalReviewViewState(
            active_tab="attribution",
            output_anchor_folder="Folder 4",
            output_anchor_offset=-6,
        ),
    )
)
print(json.dumps(observed, ensure_ascii=False))
'''
        completed = _run_qt_script(script)

        self.assertEqual(
            0,
            completed.returncode,
            completed.stdout + completed.stderr,
        )
        observed = __import__("json").loads(completed.stdout.strip())
        self.assertEqual(80, observed["attribution"]["row"])
        self.assertTrue(
            observed["attribution"]["visible"],
            observed["attribution"],
        )
        self.assertEqual("Folder 8", observed["output"]["folder"])
        self.assertTrue(observed["output"]["visible"], observed["output"])
        self.assertLessEqual(abs(observed["output"]["top"] + 4), 3)
        self.assertEqual("Folder 4", observed["latest_output"]["folder"])
        self.assertEqual("Folder 4", observed["output_state"][0])
        self.assertLessEqual(
            abs(
                observed["output_state"][1]
                - observed["latest_output"]["top"]
            ),
            3,
        )
        self.assertEqual(0, observed["hidden_initial_tab"])
        self.assertTrue(
            observed["hidden_output_restore"]["visible"],
            observed["hidden_output_restore"],
        )
        self.assertLessEqual(
            abs(observed["hidden_output_restore"]["top"] + 6),
            3,
        )

    def test_final_review_edits_heterogeneous_conflicts_in_the_same_dialog_body(self):
        script = r'''
import json
from PySide6 import QtCore, QtWidgets
from spectrum_organizer.ui.dialog_port import show_styled_dialog
from spectrum_organizer.ui.dialogs import (
    FinalReviewConflictChoice,
    FinalReviewConflictEditor,
    FinalReviewConflictGroup,
    FinalReviewConflictSelection,
    FinalReviewRow,
    final_attribution_summary_dialog,
)

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
provider_calls = []
observed = {}

def provider(row_id, selections):
    provider_calls.append((row_id, selections))
    selected = {item.group_id: item for item in selections}
    emission = selected.get(
        "emission",
        FinalReviewConflictSelection("emission", ("book-a",)),
    )
    excitation = selected.get(
        "excitation",
        FinalReviewConflictSelection("excitation", ("ex-a", "ex-c")),
    )
    excitation_keys = set(excitation.selected_keys)
    return FinalReviewConflictEditor(
        row_id=row_id,
        groups=(
            FinalReviewConflictGroup(
                group_id="emission",
                title="重复发射谱",
                context="source.opju / Folder / Book A",
                selection_mode="single",
                choices=(
                    FinalReviewConflictChoice(
                        "book-a",
                        "Book A / 极长名称 / 极长名称 / 极长名称 / 极长名称 / 极长名称 / 极长名称",
                        "固定激发波长：300 nm\n扫描范围：500–700 nm\n狭缝：2 / 2 nm",
                    ),
                    FinalReviewConflictChoice("book-b", "Book B", "固定激发波长：310 nm"),
                ),
                common_fields=(("来源文件", "source.opju"), ("Folder", "Folder")),
                selected_keys=emission.selected_keys,
            ),
            FinalReviewConflictGroup(
                group_id="excitation",
                title="激发谱候选",
                context="source.opju / Folder / Book A",
                selection_mode="multi",
                choices=(
                    FinalReviewConflictChoice("ex-a", "Ex A", "固定发射波长：500 nm"),
                    FinalReviewConflictChoice("ex-b", "Ex B", "固定发射波长：510 nm"),
                    FinalReviewConflictChoice("ex-c", "Ex C", "固定发射波长：520 nm"),
                ),
                selected_keys=excitation.selected_keys,
                single_select_groups=(("ex-a", "ex-b"),),
            ),
        ),
        can_confirm=not {"ex-a", "ex-b"}.issubset(excitation_keys),
    )

def inspect_root():
    dialog = app.activeModalWidget()
    table = dialog.findChild(QtWidgets.QTableWidget, "final_review_table")
    search = dialog.findChild(QtWidgets.QLineEdit, "final_review_search")
    table.setCurrentCell(0, 0)
    table.selectRow(0)
    search.setText("Book A")
    app.processEvents()
    dialog.findChild(QtWidgets.QPushButton, "final_review_modify_conflicts").click()
    QtCore.QTimer.singleShot(0, inspect_editor)

def inspect_editor():
    dialog = app.activeModalWidget()
    panel = dialog.findChild(QtWidgets.QWidget, "final_review_conflict_editor")
    trees = dialog.findChildren(QtWidgets.QTreeWidget, "final_review_conflict_choices")
    visible_dialogs = [
        widget
        for widget in app.topLevelWidgets()
        if isinstance(widget, QtWidgets.QDialog) and widget.isVisible()
    ]
    observed["same_dialog"] = panel.isVisible() and len(visible_dialogs) == 1
    observed["group_count"] = len(trees)
    observed["modes"] = [tree.selectionMode().name for tree in trees]
    observed["initial"] = [
        [item.data(0, QtCore.Qt.ItemDataRole.UserRole) for item in tree.selectedItems()]
        for tree in trees
    ]
    observed["headers"] = [tree.headerItem().text(1) for tree in trees]
    observed["common_titles"] = [
        label.text()
        for label in dialog.findChildren(QtWidgets.QLabel, "conflict_review_detail_title")
    ]
    observed["common_values"] = [
        label.text()
        for label in dialog.findChildren(QtWidgets.QLabel, "conflict_review_detail_value")
    ]
    common_frames = dialog.findChildren(QtWidgets.QFrame, "conflict_review_detail")
    observed["common_vertical_policies"] = [
        frame.sizePolicy().verticalPolicy().name for frame in common_frames
    ]
    observed["common_word_wrap"] = [
        label.wordWrap()
        for label in dialog.findChildren(QtWidgets.QLabel, "conflict_review_detail_value")
    ]
    observed["first_choice_detail"] = trees[0].topLevelItem(0).text(1)
    first_tree = trees[0]
    first_item = first_tree.topLevelItem(0)
    delegate = first_tree.itemDelegate()
    semantic_layout = getattr(delegate, "_cell_layout", None)
    observed["has_semantic_layout"] = semantic_layout is not None
    observed["detail_layout_fields"] = (
        [
            field
            for line in semantic_layout(
                first_item.text(1),
                first_tree.fontMetrics(),
                max(1, first_tree.columnWidth(1) - 12),
                1,
            )
            for field, _x in line
            if field
        ]
        if semantic_layout is not None
        else []
    )
    observed["atomic_layout_fields"] = (
        [
            field
            for line in semantic_layout(
                "固定激发波长：123456789e-120 nm",
                first_tree.fontMetrics(),
                54,
                1,
            )
            for field, _x in line
            if field
        ]
        if semantic_layout is not None
        else []
    )
    observed["peak_layout_fields"] = (
        [
            field
            for line in semantic_layout(
                "峰值：X=123456789e-120 nm，Y=987654321e+120",
                first_tree.fontMetrics(),
                64,
                1,
            )
            for field, _x in line
            if field
        ]
        if semantic_layout is not None
        else []
    )
    detail_bounds = first_tree.fontMetrics().boundingRect(
        QtCore.QRect(0, 0, max(1, first_tree.columnWidth(1) - 12), 10000),
        QtCore.Qt.TextFlag.TextWrapAnywhere,
        first_item.text(1),
    )
    observed["choice_geometry"] = {
        "item_height": first_tree.visualItemRect(first_item).height(),
        "required_height": detail_bounds.height() + 12,
        "horizontal_maximum": first_tree.horizontalScrollBar().maximum(),
        "viewport_width": first_tree.viewport().width(),
        "book_width": first_tree.columnWidth(0),
        "columns_width": first_tree.columnWidth(0) + first_tree.columnWidth(1),
    }
    dialog.findChild(QtWidgets.QPushButton, "final_review_conflict_back").click()
    app.processEvents()
    table = dialog.findChild(QtWidgets.QTableWidget, "final_review_table")
    search = dialog.findChild(QtWidgets.QLineEdit, "final_review_search")
    observed["restored"] = (table.currentRow(), search.text())
    dialog.findChild(QtWidgets.QPushButton, "final_review_modify_conflicts").click()
    QtCore.QTimer.singleShot(0, choose_replacement)

def choose_replacement():
    dialog = app.activeModalWidget()
    trees = dialog.findChildren(QtWidgets.QTreeWidget, "final_review_conflict_choices")
    trees[0].setCurrentItem(trees[0].topLevelItem(1))
    trees[0].topLevelItem(1).setSelected(True)
    app.processEvents()
    trees = dialog.findChildren(QtWidgets.QTreeWidget, "final_review_conflict_choices")
    excitation = trees[1]
    excitation.setCurrentItem(excitation.topLevelItem(1))
    excitation.topLevelItem(1).setSelected(True)
    app.processEvents()
    trees = dialog.findChildren(QtWidgets.QTreeWidget, "final_review_conflict_choices")
    observed["excitation_after_replace"] = [
        trees[1].topLevelItem(index).data(
            0,
            QtCore.Qt.ItemDataRole.UserRole,
        )
        for index in range(trees[1].topLevelItemCount())
        if trees[1].topLevelItem(index).isSelected()
    ]
    confirm = dialog.findChild(QtWidgets.QPushButton, "final_review_conflict_confirm")
    observed["confirm_enabled"] = confirm.isEnabled()
    if confirm.isEnabled():
        confirm.click()
    else:
        dialog.done(0)

QtCore.QTimer.singleShot(100, inspect_root)
response = show_styled_dialog(
    final_attribution_summary_dialog(
        (
            FinalReviewRow(
                "book-a",
                "source.opju",
                "Folder",
                "Book A",
                "MFL-Solid-Air-298 K",
                "将写入输出计划",
                True,
            ),
        ),
        recognized_count=1,
        rejected_count=0,
        excluded_count=0,
        accepted_count=1,
        conflict_editor_provider=provider,
    )
)
observed["response"] = {
    "action": response.action,
    "row": response.selected_row_id,
    "selections": [
        [item.group_id, list(item.selected_keys), item.decision]
        for item in response.conflict_selections
    ],
}
observed["provider_calls"] = len(provider_calls)
print(json.dumps(observed, ensure_ascii=False))
'''
        completed = _run_qt_script(script)

        self.assertEqual(
            0,
            completed.returncode,
            completed.stdout + completed.stderr,
        )
        observed = __import__("json").loads(completed.stdout.strip())
        self.assertTrue(observed["same_dialog"])
        self.assertEqual(2, observed["group_count"])
        self.assertEqual(
            [["book-a"], ["ex-a", "ex-c"]],
            observed["initial"],
        )
        self.assertEqual(["关键差异", "关键差异"], observed["headers"])
        self.assertIn("共同条件", observed["common_titles"])
        self.assertTrue(
            any("来源文件：source.opju" in value for value in observed["common_values"])
        )
        self.assertTrue(all(observed["common_word_wrap"]))
        self.assertTrue(
            all(
                policy == "Maximum"
                for policy in observed["common_vertical_policies"]
            )
        )
        self.assertNotIn("来源文件", observed["first_choice_detail"])
        self.assertTrue(observed["has_semantic_layout"])
        self.assertEqual(
            {
                "固定激发波长：300 nm",
                "扫描范围：500–700 nm",
                "狭缝：2 / 2 nm",
            },
            set(observed["detail_layout_fields"]),
        )
        self.assertEqual(
            ["固定激发波长：", "123456789e-120 nm"],
            observed["atomic_layout_fields"],
        )
        self.assertEqual(
            [
                "峰值：",
                "X=123456789e-120 nm",
                "Y=987654321e+120",
            ],
            observed["peak_layout_fields"],
        )
        geometry = observed["choice_geometry"]
        self.assertEqual(0, geometry["horizontal_maximum"])
        self.assertGreaterEqual(
            geometry["item_height"],
            geometry["required_height"],
        )
        self.assertLessEqual(
            geometry["book_width"],
            round(geometry["viewport_width"] * 0.45),
        )
        self.assertLessEqual(
            geometry["columns_width"],
            geometry["viewport_width"] + 1,
        )
        self.assertEqual([0, "Book A"], observed["restored"])
        self.assertEqual(
            ["ex-b", "ex-c"],
            observed["excitation_after_replace"],
        )
        self.assertTrue(observed["confirm_enabled"])
        self.assertEqual("modify_conflicts", observed["response"]["action"])
        self.assertEqual("book-a", observed["response"]["row"])
        self.assertIn(
            ["emission", ["book-b"], ""],
            observed["response"]["selections"],
        )
        self.assertIn(
            ["excitation", ["ex-b", "ex-c"], ""],
            observed["response"]["selections"],
        )
        self.assertGreaterEqual(observed["provider_calls"], 3)

    def test_final_review_small_conflict_is_compact_and_places_common_conditions_after_equal_rows(self):
        script = r'''
import json
from PySide6 import QtCore, QtWidgets
from spectrum_organizer.ui.dialog_port import show_styled_dialog
from spectrum_organizer.ui.dialogs import (
    FinalReviewConflictChoice,
    FinalReviewConflictEditor,
    FinalReviewConflictGroup,
    FinalReviewConflictSelection,
    FinalReviewRow,
    final_attribution_summary_dialog,
)

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
observed = {}

def provider(row_id, selections):
    selected = next(
        (
            selection.selected_keys
            for selection in selections
            if selection.group_id == "emission"
        ),
        ("book-a",),
    )
    return FinalReviewConflictEditor(
        row_id=row_id,
        groups=(
            FinalReviewConflictGroup(
                group_id="emission",
                title="重复发射谱",
                context="source.opju / Folder / Book A\nsource.opju / Folder / Book B",
                selection_mode="single",
                choices=(
                    FinalReviewConflictChoice(
                        "book-a",
                        "source.opju / Folder / Book A",
                        "固定激发波长：540 nm\n"
                        "扫描范围：240–525 nm\n"
                        "峰值：X=363 nm，Y=35,726.74",
                    ),
                    FinalReviewConflictChoice(
                        "book-b",
                        "source.opju / Folder / Book B",
                        "固定激发波长：550 nm\n"
                        "扫描范围：240–535 nm\n"
                        "峰值：X=388 nm，Y=29,344.28",
                    ),
                ),
                common_fields=(
                    ("来源文件", "source.opju"),
                    ("Folder", "Folder"),
                    ("谱图类型", "延迟发射谱"),
                ),
                selected_keys=selected,
            ),
        ),
        can_confirm=True,
    )

def open_editor():
    dialog = app.activeModalWidget()
    table = dialog.findChild(QtWidgets.QTableWidget, "final_review_table")
    table.setCurrentCell(0, 0)
    table.selectRow(0)
    dialog.findChild(
        QtWidgets.QPushButton,
        "final_review_modify_conflicts",
    ).click()
    QtCore.QTimer.singleShot(0, inspect)

def inspect():
    dialog = app.activeModalWidget()
    group = dialog.findChild(QtWidgets.QFrame, "final_review_conflict_group")
    tree = dialog.findChild(QtWidgets.QTreeWidget, "final_review_conflict_choices")
    common = dialog.findChild(QtWidgets.QFrame, "conflict_review_detail")
    row_heights = [
        tree.visualItemRect(tree.topLevelItem(index)).height()
        for index in range(tree.topLevelItemCount())
    ]
    tree_top = tree.mapTo(group, QtCore.QPoint(0, 0)).y()
    common_top = common.mapTo(group, QtCore.QPoint(0, 0)).y()
    geometry = dialog.screen().availableGeometry()
    observed.update(
        width_ratio=dialog.width() / geometry.width(),
        height_ratio=dialog.height() / geometry.height(),
        common_after_choices=(common_top >= tree_top + tree.height()),
        row_heights=row_heights,
        viewport_excess=(
            tree.viewport().height() - sum(row_heights)
        ),
        horizontal_range=tree.horizontalScrollBar().maximum(),
    )
    dialog.reject()

QtCore.QTimer.singleShot(100, open_editor)
show_styled_dialog(
    final_attribution_summary_dialog(
        (
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
        recognized_count=1,
        rejected_count=0,
        excluded_count=0,
        accepted_count=1,
        conflict_editor_provider=provider,
    )
)
print(json.dumps(observed, ensure_ascii=False))
'''
        completed = _run_qt_script(script)

        self.assertEqual(
            0,
            completed.returncode,
            completed.stdout + completed.stderr,
        )
        observed = __import__("json").loads(completed.stdout.strip())
        self.assertLessEqual(observed["width_ratio"], 0.70)
        self.assertLessEqual(
            observed["height_ratio"],
            0.68,
            observed,
        )
        self.assertTrue(observed["common_after_choices"])
        self.assertEqual(1, len(set(observed["row_heights"])))
        self.assertLessEqual(observed["viewport_excess"], 4)
        self.assertEqual(0, observed["horizontal_range"])

    def test_final_review_user_resize_refits_choices_and_survives_recompute(self):
        script = r'''
import json
from PySide6 import QtCore, QtTest, QtWidgets
from spectrum_organizer.ui import dialog_port as dialog_port_module
from spectrum_organizer.ui.dialog_port import show_styled_dialog
from spectrum_organizer.ui.dialogs import (
    FinalReviewConflictChoice,
    FinalReviewConflictEditor,
    FinalReviewConflictGroup,
    FinalReviewConflictSelection,
    FinalReviewRow,
    final_attribution_summary_dialog,
)

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
observed = {}
heartbeat = {"ticks": 0}
heartbeat_timer = QtCore.QTimer()
heartbeat_timer.setInterval(5)
heartbeat_timer.timeout.connect(
    lambda: heartbeat.__setitem__("ticks", heartbeat["ticks"] + 1)
)
heartbeat_timer.start()
dialog_port_module._final_review_conflict_target_width = (
    lambda _available, _preferred: 760
)

def provider(row_id, selections):
    selected = next(
        (
            item.selected_keys
            for item in selections
            if item.group_id == "emission"
        ),
        ("book-a",),
    )
    return FinalReviewConflictEditor(
        row_id,
        (
            FinalReviewConflictGroup(
                "emission",
                "重复发射谱",
                "source.opju / Folder / Book A\nsource.opju / Folder / Book B",
                "single",
                (
                    FinalReviewConflictChoice(
                        "book-a",
                        "source.opju / Folder / Book A",
                        "固定激发波长：540.00 nm\n"
                        "扫描范围：240.00–750.00 nm\n"
                        "延迟时间：1.00 ms\n"
                        "采样窗口：20.00 ms\n"
                        "单次闪光周期：46.00 ms\n"
                        "探测器配置：SCD100 / R1_PD_1200-330.SPC / Counts\n"
                        "光栅配置：Density 1200，Blaze 330，积分时间 0.100000 s\n"
                        "峰值：X=468.00 nm，Y=2,338.71",
                    ),
                    FinalReviewConflictChoice(
                        "book-b",
                        "source.opju / Folder / Book B",
                        "固定激发波长：550.00 nm\n"
                        "扫描范围：240.00–750.00 nm\n"
                        "延迟时间：1.00 ms\n"
                        "采样窗口：20.00 ms\n"
                        "单次闪光周期：46.00 ms\n"
                        "探测器配置：SCD100 / R1_PD_1200-330.SPC / Counts\n"
                        "光栅配置：Density 1200，Blaze 330，积分时间 0.100000 s\n"
                        "峰值：X=471.00 nm，Y=3,407.70",
                    ),
                ),
                selected_keys=selected,
            ),
        ),
        True,
    )

def inspect_initial():
    app.processEvents()
    QtCore.QTimer.singleShot(20, prepare_resize)

def prepare_resize():
    dialog = app.activeModalWidget()
    size_grip = dialog.findChild(
        QtWidgets.QSizeGrip,
        "final_review_size_grip",
    )
    tree = dialog.findChild(
        QtWidgets.QTreeWidget,
        "final_review_conflict_choices",
    )
    original_fit = tree._fit_final_review_height
    observed["resize_fit_calls"] = 0

    def recording_fit():
        observed["resize_fit_calls"] += 1
        original_fit()

    tree._fit_final_review_height = recording_fit
    observed["baseline_ticks"] = heartbeat["ticks"]
    QtTest.QTest.mousePress(
        size_grip,
        QtCore.Qt.MouseButton.LeftButton,
        pos=QtCore.QPoint(size_grip.width() - 2, size_grip.height() - 2),
    )
    sizes = tuple(
        (520 + (index % 3) * 120, dialog.height() - 10 + (index % 2) * 10)
        for index in range(17)
    ) + ((520, dialog.height()),)

    def resize_one(index=0):
        if index == len(sizes):
            QtCore.QTimer.singleShot(180, inspect_narrow)
            return
        dialog.resize(*sizes[index])
        QtCore.QTimer.singleShot(6, lambda: resize_one(index + 1))

    resize_one()

def inspect_narrow():
    dialog = app.activeModalWidget()
    tree = dialog.findChild(
        QtWidgets.QTreeWidget,
        "final_review_conflict_choices",
    )
    tree.doItemsLayout()
    observed["resize_fit_calls_after_resize"] = observed[
        "resize_fit_calls"
    ]
    observed["heartbeat_delta"] = (
        heartbeat["ticks"] - observed["baseline_ticks"]
    )
    observed["narrow_actual"] = tree.height()
    tree._fit_final_review_height()
    observed["narrow_required"] = tree.height()
    dialog.resize(800, dialog.height())
    QtTest.QTest.mouseRelease(
        dialog.findChild(QtWidgets.QSizeGrip, "final_review_size_grip"),
        QtCore.Qt.MouseButton.LeftButton,
    )
    app.processEvents()
    observed["manual_width"] = dialog.width()
    tree.setCurrentItem(
        tree.topLevelItem(1),
        0,
        QtCore.QItemSelectionModel.SelectionFlag.ClearAndSelect,
    )
    QtCore.QTimer.singleShot(50, inspect_after_recompute)

def inspect_after_recompute(attempt=0):
    dialog = app.activeModalWidget()
    tree = dialog.findChild(
        QtWidgets.QTreeWidget,
        "final_review_conflict_choices",
    )
    tree.doItemsLayout()
    required = (
        tree.header().sizeHint().height()
        + 2 * tree.frameWidth()
        + sum(
            max(1, tree.sizeHintForRow(index))
            for index in range(tree.topLevelItemCount())
        )
    )
    if tree.height() < required and attempt < 20:
        QtCore.QTimer.singleShot(
            20,
            lambda: inspect_after_recompute(attempt + 1),
        )
        return
    observed["after_width"] = dialog.width()
    observed["after_actual"] = tree.height()
    observed["after_required"] = required
    dialog.reject()

QtCore.QTimer.singleShot(100, inspect_initial)
show_styled_dialog(
    final_attribution_summary_dialog(
        (
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
        recognized_count=2,
        rejected_count=0,
        excluded_count=0,
        accepted_count=2,
        conflict_editor_provider=provider,
        initial_conflict_row_id="book-a",
    )
)
print(json.dumps(observed, ensure_ascii=False))
'''
        completed = _run_qt_script(script)

        self.assertEqual(
            0,
            completed.returncode,
            completed.stdout + completed.stderr,
        )
        observed = __import__("json").loads(completed.stdout.strip())
        self.assertGreaterEqual(
            observed["resize_fit_calls_after_resize"],
            1,
        )
        self.assertLessEqual(
            observed["resize_fit_calls_after_resize"],
            3,
            observed,
        )
        self.assertGreaterEqual(observed["heartbeat_delta"], 20, observed)
        self.assertGreaterEqual(
            observed["narrow_actual"],
            observed["narrow_required"],
        )
        self.assertEqual(
            observed["manual_width"],
            observed["after_width"],
        )
        self.assertGreaterEqual(
            observed["after_actual"],
            observed["after_required"],
        )

    def test_final_review_remembers_root_and_conflict_sizes_separately_for_session(self):
        script = r'''
import json
from PySide6 import QtCore, QtTest, QtWidgets
from spectrum_organizer.ui.dialog_port import show_styled_dialog
from spectrum_organizer.ui.dialogs import (
    FinalReviewConflictChoice,
    FinalReviewConflictEditor,
    FinalReviewConflictGroup,
    FinalReviewRow,
    final_attribution_summary_dialog,
)

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
main = QtWidgets.QMainWindow()
main.show()
observed = {}

def provider(row_id, _selections):
    return FinalReviewConflictEditor(
        row_id,
        (
            FinalReviewConflictGroup(
                "group-a",
                "重复发射谱",
                "source.opju / Folder / Book A\nsource.opju / Folder / Book B",
                "single",
                (
                    FinalReviewConflictChoice("book-a", "Book A", "峰值：X=1.00 nm，Y=2.00"),
                    FinalReviewConflictChoice("book-b", "Book B", "峰值：X=2.00 nm，Y=1.00"),
                ),
                selected_keys=("book-a",),
            ),
        ),
        True,
    )

request = final_attribution_summary_dialog(
    (
        FinalReviewRow(
            "book-a", "source.opju", "Folder", "Book A",
            "Sample-1×10^-4 M-298 K", "将写入输出计划", True, True,
        ),
    ),
    recognized_count=1,
    rejected_count=0,
    excluded_count=0,
    accepted_count=1,
    conflict_editor_provider=provider,
    initial_conflict_row_id="book-a",
)

def drag_resize(dialog, width, height):
    grip = dialog.findChild(QtWidgets.QSizeGrip, "final_review_size_grip")
    QtTest.QTest.mousePress(
        grip,
        QtCore.Qt.MouseButton.LeftButton,
        pos=QtCore.QPoint(grip.width() - 2, grip.height() - 2),
    )
    dialog.resize(width, height)
    QtTest.QTest.mouseRelease(grip, QtCore.Qt.MouseButton.LeftButton)

def first_conflict():
    dialog = app.activeModalWidget()
    drag_resize(dialog, 650, 600)
    observed["first_conflict"] = [dialog.width(), dialog.height()]
    dialog.findChild(
        QtWidgets.QPushButton,
        "final_review_conflict_back",
    ).click()
    QtCore.QTimer.singleShot(80, first_root)

def first_root():
    dialog = app.activeModalWidget()
    drag_resize(dialog, 780, 700)
    observed["first_root"] = [dialog.width(), dialog.height()]
    dialog.reject()

QtCore.QTimer.singleShot(120, first_conflict)
show_styled_dialog(request, parent=main)

def second_conflict():
    dialog = app.activeModalWidget()
    observed["second_conflict"] = [dialog.width(), dialog.height()]
    dialog.findChild(
        QtWidgets.QPushButton,
        "final_review_conflict_back",
    ).click()
    QtCore.QTimer.singleShot(80, second_root)

def second_root():
    dialog = app.activeModalWidget()
    observed["second_root"] = [dialog.width(), dialog.height()]
    dialog.reject()

QtCore.QTimer.singleShot(120, second_conflict)
show_styled_dialog(request, parent=main)
main.close()
print(json.dumps(observed, ensure_ascii=False))
'''
        completed = _run_qt_script(script)

        self.assertEqual(
            0,
            completed.returncode,
            completed.stdout + completed.stderr,
        )
        observed = __import__("json").loads(completed.stdout.strip())
        self.assertEqual(observed["first_conflict"], observed["second_conflict"])
        self.assertEqual(observed["first_root"], observed["second_root"])
        self.assertNotEqual(observed["first_conflict"], observed["first_root"])

    def test_final_review_special_group_keeps_complete_choices_compact_and_all_actions_in_bottom_dock(self):
        script = r'''
import json
from PySide6 import QtCore, QtWidgets
from spectrum_organizer.ui.dialog_port import show_styled_dialog
from spectrum_organizer.ui.dialogs import (
    FinalReviewConflictChoice,
    FinalReviewConflictEditor,
    FinalReviewConflictGroup,
    FinalReviewConflictSelection,
    FinalReviewRow,
    final_attribution_summary_dialog,
)

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
observed = {}
context_lines = tuple(
    f"source.opju / PF8_mTHF / Book {index:02d}"
    for index in range(12)
)

def provider(row_id, selections):
    selection = next(
        (
            item
            for item in selections
            if item.group_id == "special"
        ),
        FinalReviewConflictSelection(
            "special",
            ("book-0",),
            "confirm_selection",
        ),
    )
    return FinalReviewConflictEditor(
        row_id=row_id,
        groups=(
            FinalReviewConflictGroup(
                group_id="special",
                title="二维延迟谱组确认",
                context="\n".join(context_lines),
                selection_mode="special_group",
                choices=tuple(
                    FinalReviewConflictChoice(
                        f"book-{index}",
                        f"Book {index:02d}",
                        f"固定激发波长：{200 + index * 10} nm",
                    )
                    for index in range(12)
                ),
                common_fields=(
                    ("来源文件", "source.opju"),
                    ("Folder", "PF8_mTHF"),
                    ("谱图类型", "延迟发射谱"),
                ),
                selected_keys=selection.selected_keys,
                decision=selection.decision,
            ),
        ),
        can_confirm=True,
    )

def open_editor():
    dialog = app.activeModalWidget()
    table = dialog.findChild(QtWidgets.QTableWidget, "final_review_table")
    table.setCurrentCell(0, 0)
    table.selectRow(0)
    dialog.findChild(
        QtWidgets.QPushButton,
        "final_review_modify_conflicts",
    ).click()
    QtCore.QTimer.singleShot(0, inspect)

def inspect():
    dialog = app.activeModalWidget()
    group = dialog.findChild(QtWidgets.QFrame, "final_review_conflict_group")
    tree = dialog.findChild(QtWidgets.QTreeWidget, "final_review_conflict_choices")
    context = dialog.findChild(QtWidgets.QWidget, "final_review_conflict_context")
    common_values = dialog.findChildren(
        QtWidgets.QLabel,
        "conflict_review_detail_value",
    )
    mode_buttons = dialog.findChildren(
        QtWidgets.QPushButton,
        "final_review_conflict_decision",
    )
    back = dialog.findChild(
        QtWidgets.QPushButton,
        "final_review_conflict_back",
    )
    confirm = dialog.findChild(
        QtWidgets.QPushButton,
        "final_review_conflict_confirm",
    )
    cancel = dialog.findChild(
        QtWidgets.QPushButton,
        "final_review_conflict_cancel",
    )
    action_buttons = [*mode_buttons, back, confirm, cancel]
    action_y = [
        button.mapTo(dialog, QtCore.QPoint(0, 0)).y()
        for button in action_buttons
    ]
    footer = dialog.findChild(
        QtWidgets.QFrame,
        "final_review_conflict_footer",
    )
    observed.update(
        dialog_width=dialog.width(),
        unused_choice_width=(
            tree.viewport().width()
            - tree.columnWidth(0)
            - max(
                tree.fontMetrics().horizontalAdvance(
                    tree.topLevelItem(index).text(1)
                )
                for index in range(tree.topLevelItemCount())
            )
        ),
        context_visible=bool(context and context.isVisible()),
        choice_labels=[
            tree.topLevelItem(index).text(0)
            for index in range(tree.topLevelItemCount())
        ],
        common_values=[label.text() for label in common_values],
        mode_labels=[button.text() for button in mode_buttons],
        checked=[button.text() for button in mode_buttons if button.isChecked()],
        all_actions_in_footer=all(
            footer.isAncestorOf(button)
            for button in action_buttons
        ),
        action_y=action_y,
        action_left=[
            button.mapTo(dialog, QtCore.QPoint(0, 0)).x()
            for button in action_buttons
        ],
        action_right=[
            button.mapTo(dialog, QtCore.QPoint(0, 0)).x()
            + button.width()
            for button in action_buttons
        ],
        action_bottom=[
            button.mapTo(dialog, QtCore.QPoint(0, 0)).y()
            + button.height()
            for button in action_buttons
        ],
        auto_default=[button.autoDefault() for button in action_buttons],
        default=[button.isDefault() for button in action_buttons],
        footer_top=footer.mapTo(dialog, QtCore.QPoint(0, 0)).y(),
        footer_bottom=(
            footer.mapTo(dialog, QtCore.QPoint(0, 0)).y()
            + footer.height()
        ),
        group_button_count=len(group.findChildren(QtWidgets.QPushButton)),
        active_group=bool(group.property("active_group")),
    )
    dialog.reject()

QtCore.QTimer.singleShot(100, open_editor)
show_styled_dialog(
    final_attribution_summary_dialog(
        (
            FinalReviewRow(
                "book-0",
                "source.opju",
                "PF8_mTHF",
                "Book 00",
                "Sample-Solid-Air-298 K",
                "将写入输出计划",
                True,
            ),
        ),
        recognized_count=12,
        rejected_count=0,
        excluded_count=0,
        accepted_count=12,
        conflict_editor_provider=provider,
    )
)
print(json.dumps(observed, ensure_ascii=False))
'''
        completed = _run_qt_script(script)

        self.assertEqual(
            0,
            completed.returncode,
            completed.stdout + completed.stderr,
        )
        observed = __import__("json").loads(completed.stdout.strip())
        self.assertFalse(observed["context_visible"])
        self.assertEqual(
            [f"Book {index:02d}" for index in range(12)],
            observed["choice_labels"],
        )
        common_text = "\n".join(observed["common_values"])
        self.assertIn("来源文件：source.opju", common_text)
        self.assertIn("Folder：PF8_mTHF", common_text)
        self.assertEqual(
            ["整组确认", "逐 Book 确认", "整组拒绝"],
            observed["mode_labels"],
        )
        self.assertEqual(["逐 Book 确认"], observed["checked"])
        self.assertLess(observed["dialog_width"], 1120)
        self.assertLessEqual(observed["unused_choice_width"], 440)
        self.assertTrue(observed["all_actions_in_footer"])
        self.assertLessEqual(
            len(set(observed["action_y"])),
            2,
        )
        self.assertGreaterEqual(
            min(observed["action_y"]),
            observed["footer_top"],
        )
        self.assertLessEqual(
            max(observed["action_bottom"]),
            observed["footer_bottom"],
        )
        self.assertEqual([False] * 6, observed["auto_default"])
        self.assertEqual([False] * 6, observed["default"])
        self.assertGreaterEqual(min(observed["action_left"]), 0)
        self.assertLessEqual(
            max(observed["action_right"]),
            observed["dialog_width"],
        )
        self.assertEqual(0, observed["group_button_count"])
        self.assertTrue(observed["active_group"])

    def test_final_review_production_scale_enters_event_loop_before_row_fitting_finishes(self):
        script = r'''
import json
import time
from PySide6 import QtCore, QtWidgets
from spectrum_organizer.ui.dialog_port import show_styled_dialog
from spectrum_organizer.ui.dialogs import (
    FinalReviewRow,
    final_attribution_summary_dialog,
)

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
main = QtWidgets.QMainWindow()
heartbeat_label = QtWidgets.QLabel("0", main)
main.setCentralWidget(heartbeat_label)
main.show()
heartbeat = {"ticks": 0}
observed = {}
started = time.perf_counter()
timer = QtCore.QTimer(main)
timer.setInterval(10)

def tick():
    heartbeat["ticks"] += 1
    heartbeat_label.setText(str(heartbeat["ticks"]))

def first_event():
    observed["first_event_elapsed"] = time.perf_counter() - started
    observed["first_event_ticks"] = heartbeat["ticks"]
    QtCore.QTimer.singleShot(700, finish)

def finish():
    dialog = app.activeModalWidget()
    table = dialog.findChild(QtWidgets.QTableWidget, "final_review_table")
    geometry = dialog.screen().availableGeometry()
    row = table.rowCount() - 1
    required = max(
        table.fontMetrics().boundingRect(
            QtCore.QRect(
                0,
                0,
                max(1, table.columnWidth(column) - 12),
                10000,
            ),
            QtCore.Qt.TextFlag.TextWrapAnywhere,
            table.item(row, column).text(),
        ).height()
        + 12
        for column in range(table.columnCount())
    )
    observed.update(
        heartbeat_ticks=heartbeat["ticks"],
        main_label=int(heartbeat_label.text()),
        row_count=table.rowCount(),
        last_row_actual=table.rowHeight(row),
        last_row_required=required,
        horizontal_range=table.horizontalScrollBar().maximum(),
        height_ratio=dialog.height() / geometry.height(),
    )
    dialog.reject()

def watchdog():
    dialog = app.activeModalWidget()
    if dialog is not None:
        observed["watchdog"] = True
        dialog.reject()

timer.timeout.connect(tick)
timer.start()
QtCore.QTimer.singleShot(0, first_event)
QtCore.QTimer.singleShot(5000, watchdog)
rows = tuple(
    FinalReviewRow(
        f"book-{index}",
        "20241209_MFL_2DPho.opj",
        "DiMeFL_EID_mTHF",
        f"PhoEx{220 + index}_10_10_with_complete_identity",
        "2-mTHF-EID-1×10^-4 M-77 K",
        "不输出：特殊谱已确认分类，但不会复制到普通输出",
        True,
    )
    for index in range(237)
)
show_styled_dialog(
    final_attribution_summary_dialog(
        rows,
        recognized_count=237,
        rejected_count=0,
        excluded_count=160,
        accepted_count=77,
    ),
    parent=main,
)
timer.stop()
main.close()
print(json.dumps(observed, ensure_ascii=False))
'''
        completed = _run_qt_script(script)

        self.assertEqual(
            0,
            completed.returncode,
            completed.stdout + completed.stderr,
        )
        observed = __import__("json").loads(completed.stdout.strip())
        self.assertNotIn("watchdog", observed)
        self.assertLess(observed["first_event_elapsed"], 0.8)
        self.assertEqual(237, observed["row_count"])
        self.assertGreaterEqual(observed["height_ratio"], 0.80)
        self.assertLessEqual(observed["height_ratio"], 0.86)
        self.assertGreaterEqual(observed["heartbeat_ticks"], 20)
        self.assertEqual(
            observed["heartbeat_ticks"],
            observed["main_label"],
        )
        self.assertGreaterEqual(
            observed["last_row_actual"],
            observed["last_row_required"],
            observed,
        )
        self.assertEqual(0, observed["horizontal_range"])

    def test_final_review_background_conflict_refresh_keeps_main_window_heartbeat_alive(self):
        script = r'''
import json
import time
from dataclasses import replace
from PySide6 import QtCore, QtWidgets
from spectrum_organizer.ui.dialog_port import show_styled_dialog
from spectrum_organizer.ui.dialogs import (
    FinalReviewConflictChoice,
    FinalReviewConflictEditor,
    FinalReviewConflictGroup,
    FinalReviewConflictSelection,
    FinalReviewRow,
    final_attribution_summary_dialog,
)

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
main = QtWidgets.QMainWindow()
heartbeat_label = QtWidgets.QLabel("0", main)
main.setCentralWidget(heartbeat_label)
main.show()
heartbeat = {"ticks": 0}
timer = QtCore.QTimer(main)
timer.setInterval(5)

def tick():
    heartbeat["ticks"] += 1
    heartbeat_label.setText(str(heartbeat["ticks"]))

def mark_selection_event_loop_alive():
    observed["selection_event_loop_alive"] = True
    heartbeat_label.repaint()

def mark_open_event_loop_alive():
    observed["open_event_loop_alive"] = True
    heartbeat_label.repaint()

timer.timeout.connect(tick)
timer.start()
provider_calls = []
provider_delay = 0.5
observed = {"provider_delay": provider_delay}

def provider(row_id, selections):
    call_number = len(provider_calls) + 1
    provider_calls.append(call_number)
    time.sleep(provider_delay)
    selected = next(
        (
            item.selected_keys
            for item in selections
            if item.group_id == "group"
        ),
        ("a",),
    )
    return FinalReviewConflictEditor(
        row_id=row_id,
        groups=(
            FinalReviewConflictGroup(
                group_id="group",
                title="重复发射谱",
                context="source.opju / Folder / A\nsource.opju / Folder / B",
                selection_mode="single",
                choices=(
                    FinalReviewConflictChoice("a", "A", "峰值：1"),
                    FinalReviewConflictChoice("b", "B", "峰值：2"),
                ),
                selected_keys=selected,
            ),
        ),
        can_confirm=True,
        instruction=f"ready {call_number}",
    )

def open_editor():
    dialog = app.activeModalWidget()
    table = dialog.findChild(QtWidgets.QTableWidget, "final_review_table")
    table.setCurrentCell(0, 0)
    table.selectRow(0)
    baseline = heartbeat["ticks"]
    started = time.perf_counter()
    dialog.findChild(
        QtWidgets.QPushButton,
        "final_review_modify_conflicts",
    ).click()
    observed["open_click_elapsed"] = time.perf_counter() - started
    observed["open_baseline"] = baseline
    observed["open_event_loop_alive"] = False
    QtCore.QTimer.singleShot(0, mark_open_event_loop_alive)
    poll_initial()

def poll_initial():
    dialog = app.activeModalWidget()
    instruction = dialog.findChild(
        QtWidgets.QLabel,
        "final_review_conflict_instruction",
    )
    if instruction is None or instruction.text() != "ready 1":
        QtCore.QTimer.singleShot(10, poll_initial)
        return
    observed["open_heartbeat_delta"] = (
        heartbeat["ticks"] - observed["open_baseline"]
    )
    tree = dialog.findChild(
        QtWidgets.QTreeWidget,
        "final_review_conflict_choices",
    )
    selection_baseline = heartbeat["ticks"]
    started = time.perf_counter()
    tree.setCurrentItem(tree.topLevelItem(1))
    tree.topLevelItem(1).setSelected(True)
    observed["selection_click_elapsed"] = time.perf_counter() - started
    observed["selection_baseline"] = selection_baseline
    observed["selection_event_loop_alive"] = False
    QtCore.QTimer.singleShot(0, mark_selection_event_loop_alive)
    poll_recomputed()

def poll_recomputed():
    dialog = app.activeModalWidget()
    instruction = dialog.findChild(
        QtWidgets.QLabel,
        "final_review_conflict_instruction",
    )
    if (
        instruction is None
        or not instruction.text().startswith("ready ")
        or int(instruction.text().split()[-1]) < 2
    ):
        QtCore.QTimer.singleShot(10, poll_recomputed)
        return
    observed["selection_heartbeat_delta"] = (
        heartbeat["ticks"] - observed["selection_baseline"]
    )
    observed["main_label"] = int(heartbeat_label.text())
    observed["provider_calls"] = len(provider_calls)
    dialog.reject()

def watchdog():
    dialog = app.activeModalWidget()
    if dialog is not None:
        dialog.reject()

QtCore.QTimer.singleShot(100, open_editor)
QtCore.QTimer.singleShot(4000, watchdog)
request = replace(
    final_attribution_summary_dialog(
        (
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
        recognized_count=1,
        rejected_count=0,
        excluded_count=0,
        accepted_count=1,
        conflict_editor_provider=provider,
    ),
    background_conflict_refresh=True,
)
show_styled_dialog(request, parent=main)
timer.stop()
main.close()
print(json.dumps(observed, ensure_ascii=False))
'''
        completed = _run_qt_script(script)

        self.assertEqual(
            0,
            completed.returncode,
            completed.stdout + completed.stderr,
        )
        observed = __import__("json").loads(completed.stdout.strip())
        self.assertLess(
            observed["open_click_elapsed"],
            observed["provider_delay"] / 2,
        )
        self.assertTrue(observed["open_event_loop_alive"])
        self.assertGreaterEqual(observed["open_heartbeat_delta"], 5)
        self.assertLess(
            observed["selection_click_elapsed"],
            observed["provider_delay"] / 2,
        )
        self.assertTrue(observed["selection_event_loop_alive"])
        self.assertGreaterEqual(
            observed["selection_heartbeat_delta"],
            1,
        )
        self.assertGreaterEqual(observed["main_label"], 10)
        self.assertGreaterEqual(observed["provider_calls"], 2)

    def test_final_review_cancel_during_background_refresh_returns_without_thread_abort(self):
        script = r'''
import json
import time
from PySide6 import QtCore, QtWidgets
from spectrum_organizer.ui.dialog_port import show_styled_dialog
from spectrum_organizer.ui.dialogs import (
    FinalReviewConflictChoice,
    FinalReviewConflictEditor,
    FinalReviewConflictGroup,
    FinalReviewRow,
    final_attribution_summary_dialog,
)

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
observed = {}

def provider(row_id, selections):
    time.sleep(1.0)
    return FinalReviewConflictEditor(
        row_id,
        (
            FinalReviewConflictGroup(
                "group",
                "重复发射谱",
                "source.opju / Folder / A\nsource.opju / Folder / B",
                "single",
                (
                    FinalReviewConflictChoice("a", "A", "峰值：1"),
                    FinalReviewConflictChoice("b", "B", "峰值：2"),
                ),
                selected_keys=("a",),
            ),
        ),
        True,
    )

def cancel_while_loading():
    dialog = app.activeModalWidget()
    table = dialog.findChild(QtWidgets.QTableWidget, "final_review_table")
    table.setCurrentCell(0, 0)
    table.selectRow(0)
    started = time.perf_counter()
    dialog.findChild(
        QtWidgets.QPushButton,
        "final_review_modify_conflicts",
    ).click()
    dialog.findChild(
        QtWidgets.QPushButton,
        "final_review_conflict_cancel",
    ).click()
    observed["click_elapsed"] = time.perf_counter() - started

QtCore.QTimer.singleShot(100, cancel_while_loading)
response = show_styled_dialog(
    final_attribution_summary_dialog(
        (
            FinalReviewRow(
                "book-a",
                "source.opju",
                "Folder",
                "A",
                "Sample-Solid-Air-298 K",
                "将写入输出计划",
                True,
            ),
        ),
        recognized_count=1,
        rejected_count=0,
        excluded_count=0,
        accepted_count=1,
        conflict_editor_provider=provider,
        background_conflict_refresh=True,
    )
)
observed["action"] = response.action
print(json.dumps(observed))
'''
        completed = _run_qt_script(script)

        self.assertEqual(
            0,
            completed.returncode,
            completed.stdout + completed.stderr,
        )
        observed = __import__("json").loads(completed.stdout.strip())
        self.assertEqual("cancel_conflicts", observed["action"])
        self.assertLess(observed["click_elapsed"], 0.2)

    def test_final_review_reopens_share_one_background_projection_lane(self):
        script = r'''
import json
import threading
import time
from PySide6 import QtCore, QtWidgets
from spectrum_organizer.ui.dialog_port import show_styled_dialog
from spectrum_organizer.ui.dialogs import (
    FinalReviewConflictChoice,
    FinalReviewConflictEditor,
    FinalReviewConflictGroup,
    FinalReviewRow,
    final_attribution_summary_dialog,
)

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
observed = {"actions": [], "thread_counts": []}

def provider(row_id, _selections):
    time.sleep(1.0)
    return FinalReviewConflictEditor(
        row_id,
        (
            FinalReviewConflictGroup(
                "group",
                "重复发射谱",
                "source.opju / Folder / Book A",
                "single",
                (
                    FinalReviewConflictChoice("a", "A"),
                    FinalReviewConflictChoice("b", "B"),
                ),
                selected_keys=("a",),
            ),
        ),
        True,
    )

request = final_attribution_summary_dialog(
    (
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
    recognized_count=1,
    rejected_count=0,
    excluded_count=0,
    accepted_count=1,
    conflict_editor_provider=provider,
    background_conflict_refresh=True,
    initial_conflict_row_id="book-a",
)

def cancel_conflicts():
    dialog = app.activeModalWidget()
    dialog.findChild(
        QtWidgets.QPushButton,
        "final_review_conflict_cancel",
    ).click()

for _cycle in range(5):
    QtCore.QTimer.singleShot(30, cancel_conflicts)
    response = show_styled_dialog(request)
    observed["actions"].append(response.action)
    observed["thread_counts"].append(
        sum(
            thread.name == "SpectrumOrganizerFinalReview"
            and thread.is_alive()
            for thread in threading.enumerate()
        )
    )

print(json.dumps(observed))
'''
        completed = _run_qt_script(script)

        self.assertEqual(
            0,
            completed.returncode,
            completed.stdout + completed.stderr,
        )
        observed = __import__("json").loads(completed.stdout.strip())
        self.assertEqual(["cancel_conflicts"] * 5, observed["actions"])
        self.assertLessEqual(max(observed["thread_counts"]), 1)

    def test_final_review_conflict_keeps_long_measurements_complete_and_visible(self):
        script = r'''
import json
from PySide6 import QtCore, QtWidgets
from spectrum_organizer.ui.dialog_port import show_styled_dialog
from spectrum_organizer.ui.dialogs import (
    FinalReviewConflictChoice,
    FinalReviewConflictEditor,
    FinalReviewConflictGroup,
    FinalReviewRow,
    final_attribution_summary_dialog,
)

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
observed = {}
number = "9223372036854775807"
slit = f"Ex {number} / {number} nm / Em {number} / {number} nm"
detail_text = f"延迟时间：{number} ms\n狭缝：{slit}"

def provider(row_id, _selections):
    return FinalReviewConflictEditor(
        row_id,
        (
            FinalReviewConflictGroup(
                "group",
                "时间分辨延迟谱重复点",
                "source.opju / Delayed / Book A",
                "single",
                (
                    FinalReviewConflictChoice("a", "Book A", detail_text),
                    FinalReviewConflictChoice(
                        "b",
                        "Book B",
                        "延迟时间：1 ms\n狭缝：Ex 2 / 2 nm / Em 2 / 2 nm",
                    ),
                ),
                common_fields=(
                    ("来源文件", "source.opju"),
                    ("Folder", "Delayed"),
                    ("延迟时间", f"{number} ms"),
                    ("狭缝", slit),
                ),
                selected_keys=("a",),
            ),
        ),
        True,
    )

def layout_observation(owner, text, width, column, metrics):
    layout_method = getattr(owner, "_cell_layout", None)
    if layout_method is None:
        return {
            "semantic": False,
            "fits": False,
            "numeric_atomic": False,
            "parts": [],
            "width": width,
        }
    layout = layout_method(text, metrics, max(1, width), column)
    parts = [part for line in layout for part, _x in line if part]
    return {
        "semantic": True,
        "fits": all(
            metrics.horizontalAdvance(part) <= max(1, width)
            for part in parts
        ),
        "numeric_atomic": all(
            number in part
            for part in parts
            if any(character.isdigit() for character in part)
            and "1 ms" not in part
            and "2 / 2" not in part
        ),
        "parts": parts,
        "width": width,
    }

def inspect_editor():
    dialog = app.activeModalWidget()
    tree = dialog.findChild(
        QtWidgets.QTreeWidget,
        "final_review_conflict_choices",
    )
    delegate = tree.itemDelegate()
    item = tree.topLevelItem(0)
    observed["detail"] = layout_observation(
        delegate,
        item.text(1),
        tree.columnWidth(1) - 12,
        1,
        tree.fontMetrics(),
    )
    labels = dialog.findChildren(
        QtWidgets.QLabel,
        "conflict_review_detail_value",
    )
    observed["common"] = [
        layout_observation(
            label,
            label.text(),
            label.contentsRect().width(),
            0,
            label.fontMetrics(),
        )
        for label in labels
        if label.text()
    ]
    observed["common_policies"] = [
        [
            label.sizePolicy().horizontalPolicy().name,
            label.sizePolicy().verticalPolicy().name,
        ]
        for label in labels
        if label.text()
    ]
    observed["horizontal_maximum"] = tree.horizontalScrollBar().maximum()
    dialog.done(0)

def open_editor():
    dialog = app.activeModalWidget()
    table = dialog.findChild(QtWidgets.QTableWidget, "final_review_table")
    table.setCurrentCell(0, 0)
    table.selectRow(0)
    dialog.findChild(
        QtWidgets.QPushButton,
        "final_review_modify_conflicts",
    ).click()
    QtCore.QTimer.singleShot(80, inspect_editor)

QtCore.QTimer.singleShot(100, open_editor)
show_styled_dialog(
    final_attribution_summary_dialog(
        (
            FinalReviewRow(
                "book-a",
                "source.opju",
                "Delayed",
                "Book A",
                "Sample-Solid-Air-298 K",
                "将写入输出计划",
                True,
            ),
        ),
        recognized_count=1,
        rejected_count=0,
        excluded_count=0,
        accepted_count=1,
        conflict_editor_provider=provider,
    )
)
print(json.dumps(observed, ensure_ascii=False))
'''
        completed = _run_qt_script(script)

        self.assertEqual(
            0,
            completed.returncode,
            completed.stdout + completed.stderr,
        )
        observed = __import__("json").loads(completed.stdout.strip())
        self.assertTrue(observed["detail"]["semantic"])
        self.assertTrue(observed["detail"]["fits"], observed["detail"])
        self.assertTrue(
            observed["detail"]["numeric_atomic"],
            observed["detail"],
        )
        self.assertTrue(observed["common"])
        self.assertTrue(
            all(item["semantic"] for item in observed["common"]),
            observed["common"],
        )
        self.assertTrue(
            all(item["fits"] for item in observed["common"]),
            observed["common"],
        )
        self.assertTrue(
            all(item["numeric_atomic"] for item in observed["common"]),
            observed["common"],
        )
        self.assertTrue(
            all(
                policy == ["Ignored", "Maximum"]
                for policy in observed["common_policies"]
            ),
            observed["common_policies"],
        )
        self.assertEqual(0, observed["horizontal_maximum"])

    def test_final_review_conflict_recompute_preserves_outer_scroll_position(self):
        script = r'''
import json
from PySide6 import QtCore, QtTest, QtWidgets
from spectrum_organizer.ui.dialog_port import show_styled_dialog
from spectrum_organizer.ui.dialogs import (
    FinalReviewConflictChoice,
    FinalReviewConflictEditor,
    FinalReviewConflictGroup,
    FinalReviewConflictSelection,
    FinalReviewRow,
    final_attribution_summary_dialog,
)

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
observed = {"provider_calls": 0}

def provider(row_id, selections):
    observed["provider_calls"] += 1
    selected = {item.group_id: item for item in selections}
    groups = []
    changed = any(
        key.endswith("-b")
        for item in selections
        for key in item.selected_keys
    )
    indexes = range(2, 8) if changed else range(8)
    for index in indexes:
        group_id = f"group-{index}"
        current = selected.get(
            group_id,
            FinalReviewConflictSelection(group_id, (f"{group_id}-a",)),
        )
        groups.append(
            FinalReviewConflictGroup(
                group_id,
                f"冲突组 {index + 1}",
                f"source.opju / Folder_{index} / Book_{index}",
                "single",
                (
                    FinalReviewConflictChoice(
                        f"{group_id}-a",
                        f"Book {index + 1} A",
                        f"固定激发波长：{300 + index} nm",
                    ),
                    FinalReviewConflictChoice(
                        f"{group_id}-b",
                        f"Book {index + 1} B",
                        f"固定激发波长：{400 + index} nm",
                    ),
                ),
                selected_keys=current.selected_keys,
            )
        )
    return FinalReviewConflictEditor(row_id, tuple(groups), True)

def open_editor():
    dialog = app.activeModalWidget()
    table = dialog.findChild(QtWidgets.QTableWidget, "final_review_table")
    table.setCurrentCell(0, 0)
    table.selectRow(0)
    dialog.findChild(
        QtWidgets.QPushButton,
        "final_review_modify_conflicts",
    ).click()
    QtCore.QTimer.singleShot(0, change_after_scroll)

def change_after_scroll():
    dialog = app.activeModalWidget()
    scroll = dialog.findChild(
        QtWidgets.QScrollArea,
        "final_review_conflict_scroll",
    )
    bar = scroll.verticalScrollBar()
    QtTest.QTest.qWait(50)
    maximum = bar.maximum()
    frames = dialog.findChildren(
        QtWidgets.QFrame,
        "final_review_conflict_group",
    )
    anchor = next(
        frame
        for frame in frames
        if frame.findChild(
            QtWidgets.QLabel,
            "final_review_conflict_group_title",
        ).text() == "冲突组 5"
    )
    anchor_y = anchor.mapTo(scroll.widget(), QtCore.QPoint(0, 0)).y()
    bar.setValue(anchor_y)
    app.processEvents()
    before = anchor.mapTo(scroll.viewport(), QtCore.QPoint(0, 0)).y()
    anchor_tree = next(
        tree
        for tree in dialog.findChildren(
            QtWidgets.QTreeWidget,
            "final_review_conflict_choices",
        )
        if tree.property("group_id") == "group-4"
    )
    anchor_tree.setCurrentItem(anchor_tree.topLevelItem(1))
    anchor_tree.topLevelItem(1).setSelected(True)
    app.processEvents()
    QtTest.QTest.qWait(80)
    frames = dialog.findChildren(
        QtWidgets.QFrame,
        "final_review_conflict_group",
    )
    anchor = next(
        frame
        for frame in frames
        if frame.findChild(
            QtWidgets.QLabel,
            "final_review_conflict_group_title",
        ).text() == "冲突组 5"
    )
    after = anchor.mapTo(scroll.viewport(), QtCore.QPoint(0, 0)).y()
    anchor_rect = QtCore.QRect(
        anchor.mapTo(scroll.viewport(), QtCore.QPoint(0, 0)),
        anchor.size(),
    )
    observed.update(
        maximum=maximum,
        before=before,
        after=after,
        anchor_visible=anchor_rect.intersects(scroll.viewport().rect()),
        group_count=len(
            dialog.findChildren(
                QtWidgets.QTreeWidget,
                "final_review_conflict_choices",
            )
        ),
    )
    dialog.done(0)

QtCore.QTimer.singleShot(100, open_editor)
show_styled_dialog(
    final_attribution_summary_dialog(
        (
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
        recognized_count=1,
        rejected_count=0,
        excluded_count=0,
        accepted_count=1,
        conflict_editor_provider=provider,
    )
)
print(json.dumps(observed, ensure_ascii=False))
'''
        completed = _run_qt_script(script)

        self.assertEqual(
            0,
            completed.returncode,
            completed.stdout + completed.stderr,
        )
        observed = __import__("json").loads(completed.stdout.strip())
        self.assertGreater(observed["maximum"], 0)
        self.assertLessEqual(abs(observed["before"]), 3)
        self.assertLessEqual(abs(observed["before"] - observed["after"]), 3)
        self.assertTrue(observed["anchor_visible"])
        self.assertEqual(6, observed["group_count"])
        self.assertGreaterEqual(observed["provider_calls"], 2)

    def test_final_review_initial_conflict_editor_uses_escape_custom_and_native_close_as_back(self):
        script = r'''
import json
from PySide6 import QtCore, QtTest, QtWidgets
from spectrum_organizer.ui.dialog_port import show_styled_dialog
from spectrum_organizer.ui.dialogs import (
    FinalReviewConflictChoice,
    FinalReviewConflictEditor,
    FinalReviewConflictGroup,
    FinalReviewRow,
    final_attribution_summary_dialog,
)

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
observed = {}

def provider(row_id, _selections):
    return FinalReviewConflictEditor(
        row_id,
        (
            FinalReviewConflictGroup(
                "group",
                "重复发射谱",
                "source.opju / Folder / Book A",
                "single",
                (
                    FinalReviewConflictChoice("a", "A"),
                    FinalReviewConflictChoice("b", "B"),
                ),
                selected_keys=("a",),
            ),
        ),
        True,
    )

def inspect_initial():
    dialog = app.activeModalWidget()
    editor = dialog.findChild(QtWidgets.QWidget, "final_review_conflict_editor")
    root = dialog.findChild(QtWidgets.QWidget, "final_review_root")
    table = dialog.findChild(QtWidgets.QTableWidget, "final_review_table")
    observed["initial"] = (editor.isVisible(), root.isVisible(), table.currentRow())
    QtTest.QTest.keyClick(dialog, QtCore.Qt.Key.Key_Escape)
    app.processEvents()
    observed["escape"] = (editor.isVisible(), root.isVisible(), table.currentRow())
    dialog.findChild(QtWidgets.QPushButton, "final_review_modify_conflicts").click()
    app.processEvents()
    dialog.findChild(QtWidgets.QPushButton, "dialog_close_button").click()
    app.processEvents()
    observed["close"] = (editor.isVisible(), root.isVisible(), table.currentRow())
    dialog.findChild(QtWidgets.QPushButton, "final_review_modify_conflicts").click()
    app.processEvents()
    dialog.close()
    app.processEvents()
    observed["native_close"] = (
        editor.isVisible(),
        root.isVisible(),
        table.currentRow(),
        dialog.isVisible(),
    )
    if dialog.isVisible():
        dialog.findChild(QtWidgets.QPushButton, "final_review_cancel").click()

QtCore.QTimer.singleShot(100, inspect_initial)
response = show_styled_dialog(
    final_attribution_summary_dialog(
        (
            FinalReviewRow(
                "book-a",
                "source.opju",
                "Folder",
                "Book A",
                "MFL-Solid-Air-298 K",
                "将写入输出计划",
                True,
            ),
        ),
        recognized_count=1,
        rejected_count=0,
        excluded_count=0,
        accepted_count=1,
        conflict_editor_provider=provider,
        initial_conflict_row_id="book-a",
    )
)
observed["response"] = response.action
print(json.dumps(observed, ensure_ascii=False))
'''
        completed = _run_qt_script(script)

        self.assertEqual(
            0,
            completed.returncode,
            completed.stdout + completed.stderr,
        )
        observed = __import__("json").loads(completed.stdout.strip())
        self.assertEqual([True, False, 0], observed["initial"])
        self.assertEqual([False, True, 0], observed["escape"])
        self.assertEqual([False, True, 0], observed["close"])
        self.assertEqual([False, True, 0, True], observed["native_close"])
        self.assertEqual("cancel", observed["response"])

    def test_final_review_conflict_cancel_returns_and_restores_unconfirmed_draft(self):
        script = r'''
import json
from PySide6 import QtCore, QtWidgets
from spectrum_organizer.ui.dialog_port import show_styled_dialog
from spectrum_organizer.ui.dialogs import (
    FinalReviewConflictChoice,
    FinalReviewConflictEditor,
    FinalReviewConflictGroup,
    FinalReviewConflictSelection,
    FinalReviewRow,
    final_attribution_summary_dialog,
)

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
observed = {"provider_selections": []}

def provider(row_id, selections):
    observed["provider_selections"].append(
        [
            [item.group_id, list(item.selected_keys), item.decision]
            for item in selections
        ]
    )
    selected = {
        item.group_id: item.selected_keys
        for item in selections
    }.get("group", ("a",))
    return FinalReviewConflictEditor(
        row_id,
        (
            FinalReviewConflictGroup(
                "group",
                "重复发射谱",
                "source.opju / Folder / Book A",
                "single",
                (
                    FinalReviewConflictChoice("a", "A"),
                    FinalReviewConflictChoice("b", "B"),
                ),
                selected_keys=selected,
            ),
        ),
        True,
    )

def inspect_editor():
    dialog = app.activeModalWidget()
    tree = dialog.findChild(
        QtWidgets.QTreeWidget,
        "final_review_conflict_choices",
    )
    observed["visible_selection"] = [
        item.data(0, QtCore.Qt.ItemDataRole.UserRole)
        for item in tree.selectedItems()
    ]
    dialog.findChild(
        QtWidgets.QPushButton,
        "final_review_conflict_cancel",
    ).click()

QtCore.QTimer.singleShot(100, inspect_editor)
response = show_styled_dialog(
    final_attribution_summary_dialog(
        (
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
        recognized_count=1,
        rejected_count=0,
        excluded_count=0,
        accepted_count=1,
        conflict_editor_provider=provider,
        initial_conflict_row_id="book-a",
        initial_conflict_selections=(
            FinalReviewConflictSelection("group", ("b",)),
        ),
    )
)
observed["response"] = {
    "action": response.action,
    "row": response.selected_row_id,
    "selections": [
        [item.group_id, list(item.selected_keys), item.decision]
        for item in response.conflict_selections
    ],
}
print(json.dumps(observed, ensure_ascii=False))
'''
        completed = _run_qt_script(script)

        self.assertEqual(
            0,
            completed.returncode,
            completed.stdout + completed.stderr,
        )
        observed = __import__("json").loads(completed.stdout.strip())
        self.assertEqual(["b"], observed["visible_selection"])
        self.assertEqual(
            [["group", ["b"], ""]],
            observed["provider_selections"][0],
        )
        self.assertEqual("cancel_conflicts", observed["response"]["action"])
        self.assertEqual("book-a", observed["response"]["row"])
        self.assertEqual(
            [["group", ["b"], ""]],
            observed["response"]["selections"],
        )

    def test_final_review_conflict_cancel_preserves_special_per_book_pending_mode(self):
        script = r'''
import json
from PySide6 import QtCore, QtTest, QtWidgets
from spectrum_organizer.ui.dialog_port import show_styled_dialog
from spectrum_organizer.ui.dialogs import (
    FinalReviewConflictChoice,
    FinalReviewConflictEditor,
    FinalReviewConflictGroup,
    FinalReviewConflictSelection,
    FinalReviewRow,
    final_attribution_summary_dialog,
)

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
observed = {"provider_calls": []}

def provider(row_id, selections):
    observed["provider_calls"].append(
        [
            [item.group_id, list(item.selected_keys), item.decision]
            for item in selections
        ]
    )
    current = next(item for item in selections if item.group_id == "group")
    return FinalReviewConflictEditor(
        row_id,
        (
            FinalReviewConflictGroup(
                "group",
                "二维延迟谱组确认",
                "",
                "special_group",
                (
                    FinalReviewConflictChoice("220", "220"),
                    FinalReviewConflictChoice("230", "230"),
                ),
                selected_keys=current.selected_keys,
                decision=current.decision,
            ),
        ),
        True,
    )

def inspect_editor():
    dialog = app.activeModalWidget()
    per_book = next(
        button
        for button in dialog.findChildren(
            QtWidgets.QPushButton,
            "final_review_conflict_decision",
        )
        if button.text() == "逐 Book 确认"
    )
    per_book.click()
    app.processEvents()
    tree = dialog.findChildren(
        QtWidgets.QTreeWidget,
        "final_review_conflict_choices",
    )[-1]
    QtTest.QTest.mouseClick(
        tree.viewport(),
        QtCore.Qt.MouseButton.LeftButton,
        pos=tree.visualItemRect(tree.topLevelItem(0)).center(),
    )
    app.processEvents()
    observed["provider_calls_before_cancel"] = len(
        observed["provider_calls"]
    )
    dialog.findChild(
        QtWidgets.QPushButton,
        "final_review_conflict_cancel",
    ).click()

QtCore.QTimer.singleShot(100, inspect_editor)
response = show_styled_dialog(
    final_attribution_summary_dialog(
        (
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
        recognized_count=1,
        rejected_count=0,
        excluded_count=0,
        accepted_count=1,
        conflict_editor_provider=provider,
        initial_conflict_row_id="book-a",
        initial_conflict_selections=(
            FinalReviewConflictSelection(
                "group",
                (),
                "confirm_group",
            ),
        ),
    )
)
observed["response"] = {
    "action": response.action,
    "selections": [
        [item.group_id, list(item.selected_keys), item.decision]
        for item in response.conflict_selections
    ],
    "pending": [
        [item.group_id, list(item.selected_keys), item.decision]
        for item in response.conflict_pending_selections
    ],
    "editing": list(response.conflict_editing_group_ids),
}

def inspect_reopened():
    dialog = app.activeModalWidget()
    tree = dialog.findChildren(
        QtWidgets.QTreeWidget,
        "final_review_conflict_choices",
    )[-1]
    observed["reopened_mode"] = tree.selectionMode().name
    observed["reopened_selection"] = [
        item.text(0) for item in tree.selectedItems()
    ]
    dialog.findChild(
        QtWidgets.QPushButton,
        "final_review_conflict_cancel",
    ).click()

QtCore.QTimer.singleShot(100, inspect_reopened)
show_styled_dialog(
    final_attribution_summary_dialog(
        (
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
        recognized_count=1,
        rejected_count=0,
        excluded_count=0,
        accepted_count=1,
        conflict_editor_provider=provider,
        initial_conflict_row_id=response.selected_row_id,
        initial_conflict_selections=response.conflict_selections,
        initial_conflict_pending_selections=(
            response.conflict_pending_selections
        ),
        initial_conflict_editing_group_ids=(
            response.conflict_editing_group_ids
        ),
    )
)
print(json.dumps(observed, ensure_ascii=False))
'''
        completed = _run_qt_script(script)

        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        observed = __import__("json").loads(completed.stdout.strip())
        self.assertEqual(1, observed["provider_calls_before_cancel"])
        self.assertEqual("cancel_conflicts", observed["response"]["action"])
        self.assertEqual(
            [["group", [], "confirm_group"]],
            observed["response"]["selections"],
        )
        self.assertEqual(
            [["group", ["220"], "confirm_selection"]],
            observed["response"]["pending"],
        )
        self.assertEqual(["group"], observed["response"]["editing"])
        self.assertEqual("MultiSelection", observed["reopened_mode"])
        self.assertEqual(["220"], observed["reopened_selection"])

    def test_final_review_marks_preserved_invalid_downstream_choice_until_reconfirmed(self):
        script = r'''
import json
from PySide6 import QtCore, QtGui, QtTest, QtWidgets
from spectrum_organizer.ui.dialog_port import show_styled_dialog
from spectrum_organizer.ui.dialogs import (
    FinalReviewConflictChoice,
    FinalReviewConflictEditor,
    FinalReviewConflictGroup,
    FinalReviewConflictSelection,
    FinalReviewRow,
    final_attribution_summary_dialog,
)

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
observed = {}
internal_key = '["S1","worksheet","Delayed","300"]'

def provider(row_id, selections):
    selected = {item.group_id: item for item in selections}
    upstream = selected.get(
        "upstream",
        FinalReviewConflictSelection("upstream", ("a",)),
    )
    downstream = selected.get(
        "downstream",
        FinalReviewConflictSelection(
            "downstream",
            (internal_key,),
            "confirm_selection",
        ),
    )
    changed = upstream.selected_keys == ("b",)
    downstream_valid = (
        not changed
        or downstream.decision == "confirm_group"
    )
    return FinalReviewConflictEditor(
        row_id,
        (
            FinalReviewConflictGroup(
                "upstream",
                "上游重复 Book 冲突",
                "source.opju / Folder",
                "single",
                (
                    FinalReviewConflictChoice("a", "A"),
                    FinalReviewConflictChoice("b", "B"),
                ),
                selected_keys=upstream.selected_keys,
            ),
            FinalReviewConflictGroup(
                "downstream",
                "下游特殊谱组",
                "source.opju / Folder",
                "special_group",
                (
                    FinalReviewConflictChoice("new", "New Book"),
                ),
                selected_keys=(
                    downstream.selected_keys
                    if downstream_valid
                    else ()
                ),
                decision=(downstream.decision if downstream_valid else ""),
                stale_selected_keys=(
                    downstream.selected_keys
                    if not downstream_valid
                    else ()
                ),
                stale_decision=(
                    downstream.decision
                    if not downstream_valid
                    else ""
                ),
                warning=(
                    "上游选择已改变，请重新确认本组"
                    if not downstream_valid
                    else ""
                ),
            ),
        ),
        downstream_valid,
    )

def inspect():
    dialog = app.activeModalWidget()
    table = dialog.findChild(QtWidgets.QTableWidget, "final_review_table")
    table.setCurrentCell(0, 0)
    table.selectRow(0)
    dialog.findChild(QtWidgets.QPushButton, "final_review_modify_conflicts").click()
    app.processEvents()
    upstream = dialog.findChildren(QtWidgets.QTreeWidget, "final_review_conflict_choices")[0]
    upstream.setCurrentItem(upstream.topLevelItem(1))
    upstream.topLevelItem(1).setSelected(True)
    app.processEvents()
    warning = dialog.findChild(QtWidgets.QLabel, "final_review_conflict_warning")
    confirm = dialog.findChild(QtWidgets.QPushButton, "final_review_conflict_confirm")
    stale_items = [
        (tree, tree.topLevelItem(index))
        for tree in dialog.findChildren(QtWidgets.QTreeWidget, "final_review_conflict_choices")
        for index in range(tree.topLevelItemCount())
        if tree.topLevelItem(index).text(0).startswith("原选择（已失效）")
    ]
    stale_tree, stale_item = stale_items[0]
    stale_tree.scrollToItem(stale_item)
    app.processEvents()
    stale_rect = stale_tree.visualItemRect(stale_item)
    sample_point = QtCore.QPoint(stale_rect.right() - 6, stale_rect.center().y())
    normal_color = stale_tree.viewport().grab().toImage().pixelColor(sample_point).name()
    QtTest.QTest.mouseMove(stale_tree.viewport(), stale_rect.center())
    QtTest.QTest.qWait(50)
    hover_color = stale_tree.viewport().grab().toImage().pixelColor(sample_point).name()
    forced_option = QtWidgets.QStyleOptionViewItem()
    forced_option.rect = QtCore.QRect(
        0,
        0,
        stale_tree.columnWidth(0),
        max(36, stale_rect.height()),
    )
    forced_option.state = (
        QtWidgets.QStyle.StateFlag.State_Enabled
        | QtWidgets.QStyle.StateFlag.State_MouseOver
    )
    forced_option.palette = stale_tree.palette()
    forced_option.font = stale_tree.font()
    forced_option.widget = stale_tree
    forced_pixmap = QtGui.QPixmap(forced_option.rect.size())
    forced_pixmap.fill(QtCore.Qt.GlobalColor.transparent)
    forced_painter = QtGui.QPainter(forced_pixmap)
    stale_tree.itemDelegate().paint(
        forced_painter,
        forced_option,
        stale_tree.indexFromItem(stale_item, 0),
    )
    forced_painter.end()
    forced_hover_color = forced_pixmap.toImage().pixelColor(
        forced_option.rect.right() - 6,
        forced_option.rect.center().y(),
    ).name()
    observed["stale"] = (
        warning.text(),
        confirm.isEnabled(),
        [item.text(0) for _tree, item in stale_items],
        normal_color,
        hover_color,
        forced_hover_color,
    )
    downstream_heading = next(
        label
        for label in dialog.findChildren(
            QtWidgets.QLabel,
            "final_review_conflict_group_title",
        )
        if label.text() == "下游特殊谱组"
    )
    QtTest.QTest.mouseClick(
        downstream_heading,
        QtCore.Qt.MouseButton.LeftButton,
    )
    app.processEvents()
    decision = next(
        button
        for button in dialog.findChildren(QtWidgets.QPushButton, "final_review_conflict_decision")
        if button.text() == "整组确认"
    )
    decision.click()
    app.processEvents()
    confirm = dialog.findChild(QtWidgets.QPushButton, "final_review_conflict_confirm")
    observed["reconfirmed"] = confirm.isEnabled()
    confirm.click()

QtCore.QTimer.singleShot(100, inspect)
response = show_styled_dialog(
    final_attribution_summary_dialog(
        (
            FinalReviewRow(
                "book-a",
                "source.opju",
                "Folder",
                "Book A",
                "MFL-Solid-Air-298 K",
                "将写入输出计划",
                True,
            ),
        ),
        recognized_count=1,
        rejected_count=0,
        excluded_count=0,
        accepted_count=1,
        conflict_editor_provider=provider,
    )
)
observed["response"] = response.action
print(json.dumps(observed, ensure_ascii=False))
'''
        completed = _run_qt_script(script)

        self.assertEqual(
            0,
            completed.returncode,
            completed.stdout + completed.stderr,
        )
        observed = __import__("json").loads(completed.stdout.strip())
        self.assertEqual(
            "上游选择已改变，请重新确认本组",
            observed["stale"][0],
        )
        self.assertFalse(observed["stale"][1])
        self.assertEqual(
            ["原选择（已失效）：原选择详情不可用"],
            observed["stale"][2],
        )
        self.assertNotIn("[", observed["stale"][2][0])
        self.assertEqual("#fff3d6", observed["stale"][3])
        self.assertEqual("#fff3d6", observed["stale"][4])
        self.assertEqual("#fff3d6", observed["stale"][5])
        self.assertTrue(observed["reconfirmed"])
        self.assertEqual("modify_conflicts", observed["response"])

    def test_attribution_port_passes_structured_request_and_returns_structured_response(self):
        requests = []
        expected = AttributionDialogResponse(
            action="confirm",
            sample_type="solution",
            values={
                "sample": "MFL",
                "solvent": "mTHF",
                "concentration": "1×10^-4",
                "temperature": "298 K",
            },
        )
        port = QtAttributionDialogPort(form_runner=lambda request, parent: requests.append((request, parent)) or expected)
        request = AttributionDialogRequest(
            target_label="MFL_RT",
            source_filename="source.opj",
            book_display_names=("F270", "Ex315"),
            prefill={"temperature": "298 K"},
            allow_split_folder=True,
        )

        response = port.choose(request, parent="window")

        self.assertEqual(expected, response)
        self.assertEqual([(request, "window")], requests)

    def test_attribution_response_defaults_to_explicit_cancel_without_hidden_values(self):
        response = AttributionDialogResponse(action="cancel")

        self.assertEqual("", response.sample_type)
        self.assertEqual({}, response.values)
        self.assertFalse(response.apply_to_remaining_folder)
        self.assertFalse(response.split_folder)

    def test_attribution_port_passes_pending_book_selection_request(self):
        requests = []
        expected = AttributionBookSelectionResponse(action="select_book", book_key="book-2")
        port = QtAttributionDialogPort(
            book_picker_runner=lambda request, parent: requests.append((request, parent)) or expected,
        )
        request = AttributionBookSelectionRequest(
            folder_label="Folder A",
            source_filename="source.opj",
            choices=(("book-1", "F270"), ("book-2", "F300")),
            allow_return_to_folder=True,
        )

        response = port.choose_book(request, parent="window")

        self.assertEqual(expected, response)
        self.assertEqual([(request, "window")], requests)

    def test_qt_dialog_port_maps_clicked_button_to_action_without_real_qt_window(self):
        box = FakeMessageBox(clicked_action="重新检测")
        port = QtManualDialogPort(message_box_factory=lambda: box, qt_flags=FakeQtFlags())

        response = port.choose(DialogRequest(kind="space", title="Space", message="Need room", actions=("retry", "cancel")))

        self.assertEqual(DialogResponse(action="retry"), response)
        self.assertEqual("Space", box.title)
        self.assertEqual("Need room", box.text)
        self.assertEqual(["重新检测", "取消"], [button.text for button in box.buttons])
        self.assertTrue(box.executed)

    def test_database_recovery_uses_complete_confirm_and_exit_labels(self):
        request = database_recovery_dialog("损坏", "C:/data/library.sqlite3", "C:/data/library.backup.sqlite3")

        labels = [dialog_port_module._display_label(action, request.kind) for action in request.actions]

        self.assertEqual(["备份旧库并新建空库", "取消并退出"], labels)

    def test_database_recovery_message_box_renders_labels_and_returns_internal_actions(self):
        request = database_recovery_dialog("损坏", "C:/data/library.sqlite3", "C:/data/library.backup.sqlite3")

        for visible_label, internal_action in (
            ("备份旧库并新建空库", "backup_new_empty"),
            ("取消并退出", "cancel"),
        ):
            with self.subTest(visible_label=visible_label):
                box = FakeMessageBox(clicked_action=visible_label)
                port = QtManualDialogPort(message_box_factory=lambda: box, qt_flags=FakeQtFlags())

                response = port.choose(request)

                self.assertEqual(DialogResponse(action=internal_action), response)
                self.assertEqual(
                    ["备份旧库并新建空库", "取消并退出"],
                    [button.text for button in box.buttons],
                )

    def test_qt_dialog_port_applies_topmost_taskbar_flags_when_requested(self):
        flags = FakeQtFlags()
        box = FakeMessageBox(clicked_action="继续")
        port = QtManualDialogPort(message_box_factory=lambda: box, qt_flags=flags)

        port.choose(
            DialogRequest(
                kind="inspect",
                title="Inspect",
                message="Ready",
                actions=("continue",),
                topmost=True,
                taskbar_visible=True,
            )
        )

        self.assertIn(flags.window_stays_on_top, box.flags)
        self.assertIn(flags.window, box.flags)

    def test_qt_dialog_port_disables_confirm_button_when_request_blocks_confirmation(self):
        box = FakeMessageBox(clicked_action="取消")
        port = QtManualDialogPort(message_box_factory=lambda: box, qt_flags=FakeQtFlags())

        port.choose(
            DialogRequest(
                kind="attribution",
                title="Attribution",
                message="bad field",
                actions=("confirm", "cancel"),
                can_confirm=False,
            )
        )

        confirm = next(button for button in box.buttons if button.text == "确认")
        cancel = next(button for button in box.buttons if button.text == "取消")
        self.assertFalse(confirm.enabled)
        self.assertTrue(cancel.enabled)

    def test_qt_dialog_port_returns_cancel_when_dialog_closes_without_clicked_button(self):
        box = FakeMessageBox(clicked_action=None)
        port = QtManualDialogPort(message_box_factory=lambda: box, qt_flags=FakeQtFlags())

        response = port.choose(DialogRequest(kind="cancel", title="Cancel", message="Stop?", actions=("keep", "cancel")))

        self.assertEqual(DialogResponse(action="cancel"), response)

    def test_default_qt_dialog_port_creates_application_before_message_box(self):
        fake_widgets = FakeQtWidgets()
        original_loader = dialog_port_module._load_qt_modules
        dialog_port_module._load_qt_modules = lambda: (fake_widgets, FakeQtCore)
        try:
            response = QtManualDialogPort().choose(
                DialogRequest(kind="real", title="Real", message="Hello", actions=("continue",))
            )
        finally:
            dialog_port_module._load_qt_modules = original_loader

        self.assertEqual(DialogResponse(action="continue"), response)
        self.assertEqual(["QApplication", "QMessageBox"], fake_widgets.created)

        owner = object()
        with (
            mock.patch.object(
                dialog_port_module,
                "_load_qt_modules",
                return_value=(mock.Mock(QDialog=object), FakeQtCore),
            ),
            mock.patch.object(
                dialog_port_module,
                "show_styled_dialog",
                return_value=DialogResponse(action="continue"),
            ) as show_dialog,
        ):
            response = QtManualDialogPort(parent=owner).choose(
                DialogRequest(
                    kind="real",
                    title="Real",
                    message="Hello",
                    actions=("continue",),
                    taskbar_visible=True,
                )
            )

        self.assertEqual(DialogResponse(action="continue"), response)
        show_dialog.assert_called_once_with(
            mock.ANY,
            parent=owner,
        )

    def test_final_review_conflict_group_owns_shared_context_once(self):
        script = r'''
import json
from PySide6 import QtCore, QtWidgets
from spectrum_organizer.ui.dialog_port import show_styled_dialog
from spectrum_organizer.ui.dialogs import (
    FinalReviewConflictChoice,
    FinalReviewConflictEditor,
    FinalReviewConflictGroup,
    FinalReviewRow,
    final_attribution_summary_dialog,
)

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
observed = {}

def provider(row_id, selections):
    return FinalReviewConflictEditor(
        row_id,
        (
            FinalReviewConflictGroup(
                "group",
                "二维延迟谱组确认",
                "source.opju / Folder / 300\nsource.opju / Folder / 305",
                "special_group",
                (
                    FinalReviewConflictChoice("300", "300", "固定激发波长：300 nm"),
                    FinalReviewConflictChoice("305", "305", "固定激发波长：305 nm"),
                ),
                common_fields=(("来源文件", "source.opju"), ("Folder", "Folder")),
            ),
        ),
        False,
    )

def inspect():
    dialog = app.activeModalWidget()
    context = dialog.findChild(QtWidgets.QWidget, "final_review_conflict_context")
    context_items = dialog.findChildren(
        QtWidgets.QLabel,
        "final_review_conflict_context_item",
    )
    tree = dialog.findChild(
        QtWidgets.QTreeWidget,
        "final_review_conflict_choices",
    )
    common_values = dialog.findChildren(
        QtWidgets.QLabel,
        "conflict_review_detail_value",
    )
    observed.update(
        context_visible=bool(context and context.isVisible()),
        context_items=[label.text() for label in context_items if label.isVisible()],
        choices=[tree.topLevelItem(index).text(0) for index in range(tree.topLevelItemCount())],
        common="\n".join(label.text() for label in common_values),
    )
    dialog.findChild(
        QtWidgets.QPushButton,
        "final_review_conflict_cancel",
    ).click()

QtCore.QTimer.singleShot(150, inspect)
show_styled_dialog(
    final_attribution_summary_dialog(
        (
            FinalReviewRow(
                "book-a",
                "source.opju",
                "Folder",
                "300",
                "Sample-Solid-Air-298 K",
                "将写入输出计划",
                True,
            ),
        ),
        recognized_count=1,
        rejected_count=0,
        excluded_count=0,
        accepted_count=1,
        conflict_editor_provider=provider,
        initial_conflict_row_id="book-a",
    )
)
print(json.dumps(observed, ensure_ascii=False))
'''
        completed = _run_qt_script(script)

        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        observed = __import__("json").loads(completed.stdout.strip())
        self.assertFalse(observed["context_visible"])
        self.assertEqual([], observed["context_items"])
        self.assertEqual(["300", "305"], observed["choices"])
        self.assertIn("来源文件：source.opju", observed["common"])
        self.assertIn("Folder：Folder", observed["common"])

    def test_final_review_special_group_is_read_only_until_per_book_mode_is_explicitly_opened(self):
        script = r'''
import json
from PySide6 import QtCore, QtTest, QtWidgets
from spectrum_organizer.ui.dialog_port import show_styled_dialog
from spectrum_organizer.ui.dialogs import (
    FinalReviewConflictChoice,
    FinalReviewConflictEditor,
    FinalReviewConflictGroup,
    FinalReviewConflictSelection,
    FinalReviewRow,
    final_attribution_summary_dialog,
)

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
observed = {"provider_calls": []}

def provider(row_id, selections):
    observed["provider_calls"].append(
        [
            [selection.group_id, list(selection.selected_keys), selection.decision]
            for selection in selections
        ]
    )
    current = next(
        (
            selection
            for selection in selections
            if selection.group_id == "group"
        ),
        FinalReviewConflictSelection("group", (), "confirm_group"),
    )
    valid = (
        current.decision == "confirm_group" and not current.selected_keys
    ) or (
        current.decision == "confirm_selection" and bool(current.selected_keys)
    )
    return FinalReviewConflictEditor(
        row_id,
        (
            FinalReviewConflictGroup(
                "group",
                "二维延迟谱组确认",
                "source.opju / Folder / 220\nsource.opju / Folder / 230",
                "special_group",
                (
                    FinalReviewConflictChoice("220", "220", "固定激发波长：220 nm"),
                    FinalReviewConflictChoice("230", "230", "固定激发波长：230 nm"),
                ),
                selected_keys=current.selected_keys if valid else (),
                decision=current.decision if valid else "",
                stale_selected_keys=current.selected_keys if not valid else (),
                stale_decision=current.decision if not valid else "",
                warning="上游选择已改变，请重新确认本组" if not valid else "",
            ),
        ),
        valid,
    )

def inspect():
    dialog = app.activeModalWidget()
    tree = dialog.findChild(
        QtWidgets.QTreeWidget,
        "final_review_conflict_choices",
    )
    observed["mode_before"] = tree.selectionMode().name
    first = tree.topLevelItem(0)
    QtTest.QTest.mouseClick(
        tree.viewport(),
        QtCore.Qt.MouseButton.LeftButton,
        pos=tree.visualItemRect(first).center(),
    )
    app.processEvents()
    observed["calls_after_focus"] = len(observed["provider_calls"])
    observed["selected_after_focus"] = [
        item.text(0) for item in tree.selectedItems()
    ]
    observed["warnings_after_focus"] = [
        label.text()
        for label in dialog.findChildren(
            QtWidgets.QLabel,
            "final_review_conflict_warning",
        )
        if label.isVisible()
    ]
    conflict_confirm = dialog.findChild(
        QtWidgets.QPushButton,
        "final_review_conflict_confirm",
    )
    observed["confirm_after_focus"] = conflict_confirm.isEnabled()
    confirm_selection = next(
        button
        for button in dialog.findChildren(
            QtWidgets.QPushButton,
            "final_review_conflict_decision",
        )
        if button.text() == "逐 Book 确认"
    )
    confirm_selection.click()
    app.processEvents()
    tree = dialog.findChildren(
        QtWidgets.QTreeWidget,
        "final_review_conflict_choices",
    )[-1]
    confirm_selection = next(
        button
        for button in dialog.findChildren(
            QtWidgets.QPushButton,
            "final_review_conflict_decision",
        )
        if button.text() == "逐 Book 确认"
    )
    observed["calls_after_mode_open"] = len(observed["provider_calls"])
    observed["mode_after"] = tree.selectionMode().name
    observed["selected_after_mode_open"] = [
        item.text(0) for item in tree.selectedItems()
    ]
    observed["decision_enabled_before_selection"] = (
        confirm_selection.isEnabled()
    )
    first = tree.topLevelItem(0)
    QtTest.QTest.mouseClick(
        tree.viewport(),
        QtCore.Qt.MouseButton.LeftButton,
        pos=tree.visualItemRect(first).center(),
    )
    app.processEvents()
    observed["calls_after_selection"] = len(observed["provider_calls"])
    observed["selected_after_selection"] = [
        item.text(0) for item in tree.selectedItems()
    ]
    observed["decision_enabled_after_selection"] = (
        confirm_selection.isEnabled()
    )
    confirm_selection.click()
    app.processEvents()
    observed["calls_after_decision"] = len(observed["provider_calls"])
    observed["last_call"] = observed["provider_calls"][-1]
    observed["confirm_after_decision"] = conflict_confirm.isEnabled()
    dialog.findChild(
        QtWidgets.QPushButton,
        "final_review_conflict_cancel",
    ).click()

QtCore.QTimer.singleShot(150, inspect)
show_styled_dialog(
    final_attribution_summary_dialog(
        (
            FinalReviewRow(
                "book-a",
                "source.opju",
                "Folder",
                "220",
                "Sample-Solid-Air-298 K",
                "将写入输出计划",
                True,
            ),
        ),
        recognized_count=1,
        rejected_count=0,
        excluded_count=0,
        accepted_count=1,
        conflict_editor_provider=provider,
        initial_conflict_row_id="book-a",
        initial_conflict_selections=(
            FinalReviewConflictSelection("group", (), "confirm_group"),
        ),
    )
)
print(json.dumps(observed, ensure_ascii=False))
'''
        completed = _run_qt_script(script)

        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        observed = __import__("json").loads(completed.stdout.strip())
        self.assertEqual("NoSelection", observed["mode_before"])
        self.assertEqual(1, observed["calls_after_focus"])
        self.assertEqual([], observed["selected_after_focus"])
        self.assertEqual([], observed["warnings_after_focus"])
        self.assertTrue(observed["confirm_after_focus"])
        self.assertEqual(1, observed["calls_after_mode_open"])
        self.assertEqual(
            "MultiSelection",
            observed["mode_after"],
            observed,
        )
        self.assertEqual([], observed["selected_after_mode_open"])
        self.assertFalse(observed["decision_enabled_before_selection"])
        self.assertEqual(1, observed["calls_after_selection"])
        self.assertEqual(["220"], observed["selected_after_selection"])
        self.assertTrue(observed["decision_enabled_after_selection"])
        self.assertEqual(2, observed["calls_after_decision"])
        self.assertTrue(observed["confirm_after_decision"])
        self.assertEqual(
            [["group", ["220"], "confirm_selection"]],
            observed["last_call"],
        )

    def test_final_review_special_group_keeps_committed_choice_read_only_across_other_refreshes(self):
        script = r'''
import json
from PySide6 import QtCore, QtTest, QtWidgets
from spectrum_organizer.ui.dialog_port import show_styled_dialog
from spectrum_organizer.ui.dialogs import (
    FinalReviewConflictChoice,
    FinalReviewConflictEditor,
    FinalReviewConflictGroup,
    FinalReviewConflictSelection,
    FinalReviewRow,
    final_attribution_summary_dialog,
)

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
observed = {"provider_calls": []}

def provider(row_id, selections):
    current = {selection.group_id: selection for selection in selections}
    observed["provider_calls"].append(
        {
            group_id: [list(selection.selected_keys), selection.decision]
            for group_id, selection in current.items()
        }
    )
    special = current.get(
        "special",
        FinalReviewConflictSelection(
            "special",
            ("220",),
            "confirm_selection",
        ),
    )
    ordinary = current.get(
        "ordinary",
        FinalReviewConflictSelection("ordinary", ("a",)),
    )
    return FinalReviewConflictEditor(
        row_id,
        (
            FinalReviewConflictGroup(
                "special",
                "二维延迟谱组确认",
                "",
                "special_group",
                (
                    FinalReviewConflictChoice(
                        "220",
                        "220",
                        "固定激发波长：220 nm",
                    ),
                    FinalReviewConflictChoice(
                        "230",
                        "230",
                        "固定激发波长：230 nm",
                    ),
                ),
                selected_keys=special.selected_keys,
                decision=special.decision,
            ),
            FinalReviewConflictGroup(
                "ordinary",
                "普通冲突",
                "",
                "single",
                (
                    FinalReviewConflictChoice("a", "A", "峰值：A"),
                    FinalReviewConflictChoice("b", "B", "峰值：B"),
                ),
                selected_keys=ordinary.selected_keys,
                decision=ordinary.decision,
            ),
        ),
        True,
    )

def trees_by_group(dialog):
    return {
        str(tree.property("group_id")): tree
        for tree in dialog.findChildren(
            QtWidgets.QTreeWidget,
            "final_review_conflict_choices",
        )
    }

def inspect():
    dialog = app.activeModalWidget()
    trees = trees_by_group(dialog)
    special = trees["special"]
    observed["mode_before"] = special.selectionMode().name
    QtTest.QTest.mouseClick(
        special.viewport(),
        QtCore.Qt.MouseButton.LeftButton,
        pos=special.visualItemRect(special.topLevelItem(1)).center(),
    )
    app.processEvents()
    observed["calls_after_special_focus"] = len(observed["provider_calls"])
    observed["special_after_casual_click"] = [
        item.text(0) for item in special.selectedItems()
    ]
    observed["warnings_after_casual_click"] = [
        label.text()
        for label in dialog.findChildren(
            QtWidgets.QLabel,
            "final_review_conflict_warning",
        )
        if label.isVisible()
    ]

    ordinary = trees["ordinary"]
    QtTest.QTest.mouseClick(
        ordinary.viewport(),
        QtCore.Qt.MouseButton.LeftButton,
        pos=ordinary.visualItemRect(ordinary.topLevelItem(1)).center(),
    )
    app.processEvents()
    observed["calls_after_ordinary_change"] = len(observed["provider_calls"])
    observed["ordinary_refresh_call"] = observed["provider_calls"][-1]

    trees = trees_by_group(dialog)
    observed["special_visible_after_refresh"] = [
        item.text(0) for item in trees["special"].selectedItems()
    ]
    observed["mode_after_refresh"] = trees[
        "special"
    ].selectionMode().name
    confirm = dialog.findChild(
        QtWidgets.QPushButton,
        "final_review_conflict_confirm",
    )
    observed["confirm_after_ordinary_change"] = confirm.isEnabled()

    special_frame = next(
        frame
        for frame in dialog.findChildren(
            QtWidgets.QFrame,
            "final_review_conflict_group",
        )
        if str(frame.property("group_id")) == "special"
    )
    heading = special_frame.findChild(
        QtWidgets.QLabel,
        "final_review_conflict_group_title",
    )
    QtTest.QTest.mouseClick(
        heading,
        QtCore.Qt.MouseButton.LeftButton,
    )
    confirm_selection = next(
        button
        for button in dialog.findChildren(
            QtWidgets.QPushButton,
            "final_review_conflict_decision",
        )
        if button.text() == "逐 Book 确认"
    )
    confirm_selection.click()
    app.processEvents()
    trees = trees_by_group(dialog)
    special = trees["special"]
    observed["calls_after_mode_open"] = len(observed["provider_calls"])
    observed["mode_after_open"] = special.selectionMode().name
    observed["special_after_mode_open"] = [
        item.text(0) for item in special.selectedItems()
    ]
    QtTest.QTest.mouseClick(
        special.viewport(),
        QtCore.Qt.MouseButton.LeftButton,
        pos=special.visualItemRect(special.topLevelItem(0)).center(),
    )
    QtTest.QTest.mouseClick(
        special.viewport(),
        QtCore.Qt.MouseButton.LeftButton,
        pos=special.visualItemRect(special.topLevelItem(1)).center(),
    )
    app.processEvents()
    observed["calls_after_pending_selection"] = len(
        observed["provider_calls"]
    )
    observed["special_pending"] = [
        item.text(0) for item in special.selectedItems()
    ]
    confirm_selection = next(
        button
        for button in dialog.findChildren(
            QtWidgets.QPushButton,
            "final_review_conflict_decision",
        )
        if button.text() == "逐 Book 确认"
    )
    confirm_selection.click()
    app.processEvents()
    observed["explicit_decision_call"] = observed["provider_calls"][-1]
    observed["confirm_after_explicit_decision"] = confirm.isEnabled()
    dialog.findChild(
        QtWidgets.QPushButton,
        "final_review_conflict_cancel",
    ).click()

QtCore.QTimer.singleShot(150, inspect)
show_styled_dialog(
    final_attribution_summary_dialog(
        (
            FinalReviewRow(
                "book-a",
                "source.opju",
                "Folder",
                "220",
                "Sample-Solid-Air-298 K",
                "将写入输出计划",
                True,
            ),
        ),
        recognized_count=1,
        rejected_count=0,
        excluded_count=0,
        accepted_count=1,
        conflict_editor_provider=provider,
        initial_conflict_row_id="book-a",
        initial_conflict_selections=(
            FinalReviewConflictSelection(
                "special",
                ("220",),
                "confirm_selection",
            ),
            FinalReviewConflictSelection("ordinary", ("a",)),
        ),
    )
)
print(json.dumps(observed, ensure_ascii=False))
'''
        completed = _run_qt_script(script)

        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        observed = __import__("json").loads(completed.stdout.strip())
        self.assertEqual("NoSelection", observed["mode_before"])
        self.assertEqual(1, observed["calls_after_special_focus"])
        self.assertEqual(["220"], observed["special_after_casual_click"])
        self.assertEqual([], observed["warnings_after_casual_click"])
        self.assertEqual(2, observed["calls_after_ordinary_change"])
        self.assertEqual(
            [["220"], "confirm_selection"],
            observed["ordinary_refresh_call"]["special"],
        )
        self.assertEqual(
            [["b"], ""],
            observed["ordinary_refresh_call"]["ordinary"],
        )
        self.assertEqual(
            ["220"],
            observed["special_visible_after_refresh"],
        )
        self.assertEqual("NoSelection", observed["mode_after_refresh"])
        self.assertTrue(observed["confirm_after_ordinary_change"])
        self.assertEqual(2, observed["calls_after_mode_open"])
        self.assertEqual("MultiSelection", observed["mode_after_open"])
        self.assertEqual(["220"], observed["special_after_mode_open"])
        self.assertEqual(2, observed["calls_after_pending_selection"])
        self.assertEqual(["230"], observed["special_pending"])
        self.assertEqual(
            [["230"], "confirm_selection"],
            observed["explicit_decision_call"]["special"],
        )
        self.assertTrue(observed["confirm_after_explicit_decision"])

    def test_final_review_special_pending_keys_are_revalidated_after_other_group_refresh(self):
        script = r'''
import json
from PySide6 import QtCore, QtTest, QtWidgets
from spectrum_organizer.ui.dialog_port import show_styled_dialog
from spectrum_organizer.ui.dialogs import (
    FinalReviewConflictChoice,
    FinalReviewConflictEditor,
    FinalReviewConflictGroup,
    FinalReviewConflictSelection,
    FinalReviewRow,
    final_attribution_summary_dialog,
)

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
observed = {"provider_calls": []}

def provider(row_id, selections):
    current = {item.group_id: item for item in selections}
    observed["provider_calls"].append(
        {
            group_id: [list(item.selected_keys), item.decision]
            for group_id, item in current.items()
        }
    )
    ordinary = current.get(
        "ordinary",
        FinalReviewConflictSelection("ordinary", ("keep",)),
    )
    special_choices = (
        (FinalReviewConflictChoice("new", "New"),)
        if ordinary.selected_keys == ("switch",)
        else (FinalReviewConflictChoice("old", "Old"),)
    )
    special = current.get(
        "special",
        FinalReviewConflictSelection("special", (), "confirm_group"),
    )
    return FinalReviewConflictEditor(
        row_id,
        (
            FinalReviewConflictGroup(
                "special",
                "二维延迟谱组确认",
                "",
                "special_group",
                special_choices,
                selected_keys=special.selected_keys,
                decision=special.decision,
            ),
            FinalReviewConflictGroup(
                "ordinary",
                "普通冲突",
                "",
                "single",
                (
                    FinalReviewConflictChoice("keep", "Keep"),
                    FinalReviewConflictChoice("switch", "Switch"),
                ),
                selected_keys=ordinary.selected_keys,
            ),
        ),
        True,
    )

def trees_by_group(dialog):
    return {
        str(tree.property("group_id")): tree
        for tree in dialog.findChildren(
            QtWidgets.QTreeWidget,
            "final_review_conflict_choices",
        )
    }

def activate_group(dialog, group_id):
    frame = next(
        frame
        for frame in dialog.findChildren(
            QtWidgets.QFrame,
            "final_review_conflict_group",
        )
        if str(frame.property("group_id")) == group_id
    )
    QtTest.QTest.mouseClick(
        frame.findChild(
            QtWidgets.QLabel,
            "final_review_conflict_group_title",
        ),
        QtCore.Qt.MouseButton.LeftButton,
    )

def per_book_button(dialog):
    return next(
        button
        for button in dialog.findChildren(
            QtWidgets.QPushButton,
            "final_review_conflict_decision",
        )
        if button.text() == "逐 Book 确认"
    )

def inspect():
    dialog = app.activeModalWidget()
    per_book_button(dialog).click()
    app.processEvents()
    special = trees_by_group(dialog)["special"]
    QtTest.QTest.mouseClick(
        special.viewport(),
        QtCore.Qt.MouseButton.LeftButton,
        pos=special.visualItemRect(special.topLevelItem(0)).center(),
    )
    app.processEvents()
    activate_group(dialog, "ordinary")
    ordinary = trees_by_group(dialog)["ordinary"]
    QtTest.QTest.mouseClick(
        ordinary.viewport(),
        QtCore.Qt.MouseButton.LeftButton,
        pos=ordinary.visualItemRect(ordinary.topLevelItem(1)).center(),
    )
    app.processEvents()
    activate_group(dialog, "special")
    special = trees_by_group(dialog)["special"]
    observed["choices_after_refresh"] = [
        special.topLevelItem(index).text(0)
        for index in range(special.topLevelItemCount())
    ]
    observed["selected_after_refresh"] = [
        item.text(0) for item in special.selectedItems()
    ]
    observed["decision_enabled_after_refresh"] = (
        per_book_button(dialog).isEnabled()
    )
    dialog.findChild(
        QtWidgets.QPushButton,
        "final_review_conflict_cancel",
    ).click()

QtCore.QTimer.singleShot(150, inspect)
response = show_styled_dialog(
    final_attribution_summary_dialog(
        (
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
        recognized_count=1,
        rejected_count=0,
        excluded_count=0,
        accepted_count=1,
        conflict_editor_provider=provider,
        initial_conflict_row_id="book-a",
        initial_conflict_selections=(
            FinalReviewConflictSelection(
                "special",
                (),
                "confirm_group",
            ),
            FinalReviewConflictSelection("ordinary", ("keep",)),
        ),
    )
)
observed["pending_after_refresh"] = [
    [item.group_id, list(item.selected_keys), item.decision]
    for item in response.conflict_pending_selections
]
print(json.dumps(observed, ensure_ascii=False))
'''
        completed = _run_qt_script(script)

        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        observed = __import__("json").loads(completed.stdout.strip())
        self.assertEqual(["New"], observed["choices_after_refresh"])
        self.assertEqual([], observed["selected_after_refresh"])
        self.assertFalse(observed["decision_enabled_after_refresh"])
        self.assertEqual(
            [["special", [], "confirm_selection"]],
            observed["pending_after_refresh"],
        )

    def test_final_review_stale_special_choice_stays_read_only_when_per_book_mode_opens(self):
        script = r'''
import json
from PySide6 import QtCore, QtTest, QtWidgets
from spectrum_organizer.ui.dialog_port import show_styled_dialog
from spectrum_organizer.ui.dialogs import (
    FinalReviewConflictChoice,
    FinalReviewConflictEditor,
    FinalReviewConflictGroup,
    FinalReviewConflictSelection,
    FinalReviewRow,
    final_attribution_summary_dialog,
)

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
observed = {"provider_calls": []}

def provider(row_id, selections):
    observed["provider_calls"].append(
        [
            [selection.group_id, list(selection.selected_keys), selection.decision]
            for selection in selections
        ]
    )
    current = next(
        selection
        for selection in selections
        if selection.group_id == "group"
    )
    valid = (
        current.decision == "confirm_selection"
        and bool(current.selected_keys)
        and set(current.selected_keys) <= {"220", "230"}
    )
    return FinalReviewConflictEditor(
        row_id,
        (
            FinalReviewConflictGroup(
                "group",
                "二维延迟谱组确认",
                "",
                "special_group",
                (
                    FinalReviewConflictChoice(
                        "220",
                        "220",
                        "固定激发波长：220 nm",
                    ),
                    FinalReviewConflictChoice(
                        "230",
                        "230",
                        "固定激发波长：230 nm",
                    ),
                ),
                selected_keys=current.selected_keys if valid else (),
                decision=current.decision if valid else "",
                stale_selected_keys=() if valid else current.selected_keys,
                stale_decision="" if valid else current.decision,
                warning="" if valid else "上游选择已改变，请重新确认本组",
            ),
        ),
        valid,
    )

def current_tree(dialog):
    return dialog.findChildren(
        QtWidgets.QTreeWidget,
        "final_review_conflict_choices",
    )[-1]

def per_book_button(dialog):
    return next(
        button
        for button in dialog.findChildren(
            QtWidgets.QPushButton,
            "final_review_conflict_decision",
        )
        if button.text() == "逐 Book 确认"
    )

def inspect():
    dialog = app.activeModalWidget()
    tree = current_tree(dialog)
    QtTest.QTest.mouseClick(
        tree.viewport(),
        QtCore.Qt.MouseButton.LeftButton,
        pos=tree.visualItemRect(tree.topLevelItem(0)).center(),
    )
    app.processEvents()
    observed["calls_after_read_only_click"] = len(observed["provider_calls"])
    observed["selected_before_mode"] = [
        item.text(0) for item in tree.selectedItems()
    ]
    per_book_button(dialog).click()
    app.processEvents()
    tree = current_tree(dialog)
    button = per_book_button(dialog)
    observed["mode_after_open"] = tree.selectionMode().name
    observed["selected_after_open"] = [
        item.text(0) for item in tree.selectedItems()
    ]
    observed["stale_rows_after_open"] = [
        tree.topLevelItem(index).text(0)
        for index in range(tree.topLevelItemCount())
        if tree.topLevelItem(index).text(0).startswith("原选择（已失效）")
    ]
    observed["decision_enabled_after_open"] = button.isEnabled()
    QtTest.QTest.mouseClick(
        tree.viewport(),
        QtCore.Qt.MouseButton.LeftButton,
        pos=tree.visualItemRect(tree.topLevelItem(0)).center(),
    )
    app.processEvents()
    observed["decision_enabled_after_new_choice"] = button.isEnabled()
    button.click()
    app.processEvents()
    observed["calls_after_confirmation"] = len(observed["provider_calls"])
    observed["last_call"] = observed["provider_calls"][-1]
    dialog.findChild(
        QtWidgets.QPushButton,
        "final_review_conflict_cancel",
    ).click()

QtCore.QTimer.singleShot(150, inspect)
show_styled_dialog(
    final_attribution_summary_dialog(
        (
            FinalReviewRow(
                "book-a",
                "source.opju",
                "Folder",
                "220",
                "Sample-Solid-Air-298 K",
                "将写入输出计划",
                True,
            ),
        ),
        recognized_count=1,
        rejected_count=0,
        excluded_count=0,
        accepted_count=1,
        conflict_editor_provider=provider,
        initial_conflict_row_id="book-a",
        initial_conflict_selections=(
            FinalReviewConflictSelection(
                "group",
                ("380",),
                "confirm_selection",
            ),
        ),
    )
)
print(json.dumps(observed, ensure_ascii=False))
'''
        completed = _run_qt_script(script)

        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        observed = __import__("json").loads(completed.stdout.strip())
        self.assertEqual(1, observed["calls_after_read_only_click"])
        self.assertEqual([], observed["selected_before_mode"])
        self.assertEqual("MultiSelection", observed["mode_after_open"])
        self.assertEqual([], observed["selected_after_open"])
        self.assertEqual(
            ["原选择（已失效）：原选择详情不可用"],
            observed["stale_rows_after_open"],
        )
        self.assertFalse(observed["decision_enabled_after_open"])
        self.assertTrue(observed["decision_enabled_after_new_choice"])
        self.assertEqual(2, observed["calls_after_confirmation"])
        self.assertEqual(
            [["group", ["220"], "confirm_selection"]],
            observed["last_call"],
        )

    def test_final_review_stale_detail_does_not_paint_empty_slots_into_choice_rows(self):
        script = r'''
import json
from PySide6 import QtCore, QtWidgets
from spectrum_organizer.ui.dialog_port import show_styled_dialog
from spectrum_organizer.ui.dialogs import (
    FinalReviewConflictChoice,
    FinalReviewConflictEditor,
    FinalReviewConflictGroup,
    FinalReviewRow,
    final_attribution_summary_dialog,
)

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
observed = {}
normal_detail = "固定激发波长：220.00 nm\n峰值：X=456.0 nm，Y=1,844.12"
stale_detail = "\n".join(
    (
        "来源文件：source.opju",
        "Folder：Folder",
        "谱图类型：延迟发射谱",
        "固定激发波长：380.00 nm",
        "扫描范围：350.00–650.00 nm",
        "扫描步长：1.00 nm",
        "狭缝：Ex 10.00 / 10.00 nm / Em 10.00 / 10.00 nm",
        "延迟时间：10.00 ms",
        "采样窗口：20.00 ms",
        "单次闪光周期：55.00 ms",
        "闪光次数：4",
        "峰值：X=458.0 nm，Y=316.58",
    )
)

def provider(row_id, selections):
    return FinalReviewConflictEditor(
        row_id,
        (
            FinalReviewConflictGroup(
                "group",
                "二维延迟谱组确认",
                "",
                "special_group",
                (
                    FinalReviewConflictChoice("220", "220", normal_detail),
                    FinalReviewConflictChoice(
                        "230",
                        "230",
                        normal_detail.replace("220.00", "230.00"),
                    ),
                ),
                common_fields=(
                    ("来源文件", "source.opju"),
                    ("Folder", "Folder"),
                    ("谱图类型", "延迟发射谱"),
                ),
                stale_selected_keys=("380",),
                stale_choices=(
                    FinalReviewConflictChoice("380", "380", stale_detail),
                ),
                stale_decision="confirm_selection",
                warning="上游选择已改变，请重新确认本组",
            ),
        ),
        False,
    )

def inspect():
    dialog = app.activeModalWidget()
    tree = dialog.findChild(
        QtWidgets.QTreeWidget,
        "final_review_conflict_choices",
    )
    app.processEvents()
    heights = [
        tree.visualItemRect(tree.topLevelItem(index)).height()
        for index in range(tree.topLevelItemCount())
    ]
    observed["heights"] = heights
    observed["texts"] = [
        tree.topLevelItem(index).text(1)
        for index in range(tree.topLevelItemCount())
    ]
    dialog.findChild(
        QtWidgets.QPushButton,
        "final_review_conflict_cancel",
    ).click()

QtCore.QTimer.singleShot(150, inspect)
show_styled_dialog(
    final_attribution_summary_dialog(
        (
            FinalReviewRow(
                "book-a",
                "source.opju",
                "Folder",
                "220",
                "Sample-Solid-Air-298 K",
                "将写入输出计划",
                True,
            ),
        ),
        recognized_count=1,
        rejected_count=0,
        excluded_count=0,
        accepted_count=1,
        conflict_editor_provider=provider,
        initial_conflict_row_id="book-a",
    )
)
print(json.dumps(observed, ensure_ascii=False))
'''
        completed = _run_qt_script(script)

        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        observed = __import__("json").loads(completed.stdout.strip())
        self.assertEqual(observed["heights"][0], observed["heights"][1])
        self.assertLess(observed["heights"][0], observed["heights"][2])
        self.assertEqual(2, len(observed["texts"][0].splitlines()))
        self.assertEqual(9, len(observed["texts"][2].splitlines()))
        self.assertNotIn("来源文件：source.opju", observed["texts"][2])
        self.assertNotIn("Folder：Folder", observed["texts"][2])
        self.assertNotIn("谱图类型：延迟发射谱", observed["texts"][2])
        self.assertIn("固定激发波长：380.00 nm", observed["texts"][2])
        self.assertIn("峰值：X=458.0 nm，Y=316.58", observed["texts"][2])


class QtAttributionDialogInteractionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6 import QtCore, QtTest, QtWidgets

        cls.QtCore = QtCore
        cls.QtTest = QtTest
        cls.QtWidgets = QtWidgets
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def _run_dialog(self, run_dialog, interact, *, timeout_message):
        failures = []
        timed_out = []

        def close_dialogs():
            for widget in self.app.topLevelWidgets():
                if isinstance(widget, self.QtWidgets.QDialog) and widget.isVisible():
                    widget.reject()

        def run_interaction():
            try:
                dialog = self.app.activeModalWidget() or next(
                    (
                        widget
                        for widget in self.app.topLevelWidgets()
                        if isinstance(widget, self.QtWidgets.QDialog)
                        and widget.objectName() == "organizer_dialog"
                        and widget.isVisible()
                    ),
                    None,
                )
                self.assertIsInstance(dialog, self.QtWidgets.QDialog)
                interact(dialog)
            except Exception as exc:
                failures.append(exc)
                close_dialogs()

        def handle_timeout():
            timed_out.append(True)
            close_dialogs()

        watchdog = self.QtCore.QTimer(self.app)
        watchdog.setSingleShot(True)
        watchdog.timeout.connect(handle_timeout)
        watchdog.start(2000)
        self.QtCore.QTimer.singleShot(0, run_interaction)
        response = run_dialog()
        watchdog.stop()

        if failures:
            raise failures[0]
        self.assertFalse(timed_out, timeout_message)
        return response

    def _run_form(self, request, interact, *, parent=None):
        return self._run_dialog(
            lambda: QtAttributionDialogPort().choose(request, parent=parent),
            interact,
            timeout_message="attribution dialog interaction timed out",
        )

    def _run_book_picker(self, request, interact, *, parent=None):
        return self._run_dialog(
            lambda: QtAttributionDialogPort().choose_book(request, parent=parent),
            interact,
            timeout_message="pending-Book picker interaction timed out",
        )

    def _controls(self, dialog):
        form = dialog.findChild(self.QtWidgets.QGridLayout, "attribution_form_layout")
        labels = {}
        fields = {}
        for row in range(form.rowCount()):
            for label_column in (0, 2):
                label_item = form.itemAtPosition(row, label_column)
                field_item = form.itemAtPosition(row, label_column + 1)
                if label_item is None or field_item is None:
                    continue
                label = label_item.widget()
                field_widget = field_item.widget()
                labels[label.text()] = label
                line_edit = (
                    field_widget
                    if isinstance(field_widget, self.QtWidgets.QLineEdit)
                    else field_widget.findChild(self.QtWidgets.QLineEdit)
                )
                if line_edit is not None:
                    fields[label.text()] = line_edit
        combos = dialog.findChildren(self.QtWidgets.QComboBox)
        sample_type = next(combo for combo in combos if combo.findData("solution") >= 0)
        unit = next(combo for combo in combos if combo is not sample_type)
        return labels, fields, sample_type, unit

    def _button(self, dialog, text):
        return next(
            button
            for button in dialog.findChildren(self.QtWidgets.QPushButton)
            if button.text() == text
        )

    def _assert_runtime_nonmodal_and_antialiased(self, dialog, owner):
        self.app.processEvents()
        self.assertIsNone(self.app.activeModalWidget())
        self.assertEqual(
            self.QtCore.Qt.WindowModality.NonModal,
            dialog.windowModality(),
        )
        self.assertTrue(
            dialog.testAttribute(
                self.QtCore.Qt.WidgetAttribute.WA_TranslucentBackground
            )
        )
        self.assertTrue(dialog.mask().isEmpty())
        self.assertTrue(owner.isEnabled())
        body = dialog.findChild(self.QtWidgets.QFrame, "dialog_body")
        self.assertIsNotNone(body)
        image = body.grab().toImage()
        self.assertFalse(image.isNull())
        center = image.pixelColor(image.width() // 2, image.height() // 2)
        self.assertEqual(255, center.alpha())

    def test_attribution_form_is_runtime_nonmodal_topmost_and_antialiased(self):
        owner = self.QtWidgets.QWidget()
        owner.show()

        def interact(dialog):
            self._assert_runtime_nonmodal_and_antialiased(dialog, owner)
            self.assertEqual(
                self.QtCore.Qt.WindowType.Tool,
                dialog.windowType(),
            )
            self.assertTrue(
                bool(dialog.windowFlags() & self.QtCore.Qt.WindowType.WindowStaysOnTopHint)
            )
            dialog.reject()

        try:
            with mock.patch.object(
                dialog_port_module,
                "_make_windows_taskbar_window",
            ) as make_taskbar_window:
                self._run_form(
                    AttributionDialogRequest(
                        "Folder A",
                        "source.opj",
                        ("Book1",),
                    ),
                    interact,
                    parent=owner,
                )
            make_taskbar_window.assert_called_once()
        finally:
            owner.close()

    def test_attribution_form_keeps_owner_scrollable_while_form_refits(self):
        owner = self.QtWidgets.QScrollArea()
        owner_body = self.QtWidgets.QWidget()
        owner_body.setFixedHeight(1600)
        owner.setWidget(owner_body)
        owner.resize(640, 420)
        owner.show()

        def interact(dialog):
            self._assert_runtime_nonmodal_and_antialiased(dialog, owner)
            scrollbar = owner.verticalScrollBar()
            self.assertGreater(scrollbar.maximum(), 0)
            scrollbar.setValue(scrollbar.maximum())
            self.app.processEvents()
            self.assertEqual(scrollbar.maximum(), scrollbar.value())

            initial_geometry = dialog.geometry()
            _labels, _fields, sample_type, _unit = self._controls(dialog)
            sample_type.setCurrentIndex(sample_type.findData("solution"))
            self.app.processEvents()
            sample_type.setCurrentIndex(sample_type.findData("doped"))
            self.QtTest.QTest.qWait(80)
            self.assertEqual(initial_geometry.topLeft(), dialog.geometry().topLeft())
            self.assertEqual(initial_geometry.width(), dialog.width())
            self.assertGreater(dialog.height(), initial_geometry.height())
            form_scroll = dialog.findChild(
                self.QtWidgets.QScrollArea,
                "attribution_body_scroll",
            )
            self.assertEqual(0, form_scroll.verticalScrollBar().maximum())
            self.assertEqual(scrollbar.maximum(), scrollbar.value())
            dialog.reject()

        try:
            self._run_form(
                AttributionDialogRequest("Folder A", "source.opj", ("Book1",)),
                interact,
                parent=owner,
            )
            self.app.processEvents()
            self.assertFalse(
                any(
                    widget.isVisible() and widget.objectName() == "organizer_dialog"
                    for widget in self.app.topLevelWidgets()
                )
            )
        finally:
            owner.close()

    def test_pending_book_picker_is_runtime_nonmodal_topmost_and_antialiased(self):
        owner = self.QtWidgets.QWidget()
        owner.show()

        def interact(dialog):
            self._assert_runtime_nonmodal_and_antialiased(dialog, owner)
            self.assertEqual(
                self.QtCore.Qt.WindowType.Tool,
                dialog.windowType(),
            )
            self.assertTrue(
                bool(dialog.windowFlags() & self.QtCore.Qt.WindowType.WindowStaysOnTopHint)
            )
            dialog.reject()

        try:
            with mock.patch.object(
                dialog_port_module,
                "_make_windows_taskbar_window",
            ) as make_taskbar_window:
                self._run_book_picker(
                    AttributionBookSelectionRequest(
                        folder_label="Folder A",
                        source_filename="source.opj",
                        choices=(("book-1", "Book 1"),),
                        allow_return_to_folder=True,
                    ),
                    interact,
                    parent=owner,
                )
            make_taskbar_window.assert_called_once()
        finally:
            owner.close()

    def test_owner_close_cancels_open_attribution_form_without_orphan_dialog(self):
        owner = self.QtWidgets.QWidget()
        owner.show()

        def interact(dialog):
            self._assert_runtime_nonmodal_and_antialiased(dialog, owner)
            owner.close()

        response = self._run_form(
            AttributionDialogRequest("Folder A", "source.opj", ("Book1",)),
            interact,
            parent=owner,
        )
        self.app.processEvents()
        self.assertEqual("cancel", response.action)
        self.assertFalse(
            any(
                widget.isVisible() and widget.objectName() == "organizer_dialog"
                for widget in self.app.topLevelWidgets()
            )
        )

    def test_active_run_owner_close_keeps_attribution_form_open_for_cancel_confirmation(self):
        from types import SimpleNamespace

        from spectrum_organizer.ui.app import _install_safe_close_filter

        owner = self.QtWidgets.QWidget()
        cancel_after_preferences = mock.Mock()
        controller = SimpleNamespace(
            run_in_progress=True,
            shutdown_pending=False,
            _shutdown_exit_blocked=False,
            approved_pre_extraction_context=None,
            orchestrator=SimpleNamespace(task_cache={}, cancelled=False),
            _startup_health_gate_pending=False,
            cancel_after_preferences=cancel_after_preferences,
        )
        _install_safe_close_filter(owner, controller, self.QtCore)
        owner.show()

        def interact(dialog):
            owner.close()
            self.app.processEvents()
            self.assertTrue(dialog.isVisible())
            cancel_after_preferences.assert_called_once_with()
            dialog.reject()

        response = self._run_form(
            AttributionDialogRequest("Folder A", "source.opj", ("Book1",)),
            interact,
            parent=owner,
        )
        controller.run_in_progress = False
        owner.close()
        self.assertEqual("cancel", response.action)

    def test_owner_minimize_restore_preserves_open_attribution_form_state(self):
        owner = self.QtWidgets.QWidget()
        owner.show()
        observed = {}

        def interact(dialog):
            self._assert_runtime_nonmodal_and_antialiased(dialog, owner)
            _, fields, sample_type, _ = self._controls(dialog)
            sample_type.setCurrentIndex(sample_type.findData("solution"))
            self.app.processEvents()
            _, fields, _, _ = self._controls(dialog)
            fields["样品名称"].setText("NDI")
            owner.showMinimized()
            self.app.processEvents()
            owner.showNormal()
            self.app.processEvents()
            observed["visible_after_restore"] = dialog.isVisible()
            _, fields, _, _ = self._controls(dialog)
            observed["sample_name_after_restore"] = fields["样品名称"].text()
            dialog.reject()

        response = self._run_form(
            AttributionDialogRequest("Folder A", "source.opj", ("Book1",)),
            interact,
            parent=owner,
        )
        owner.close()
        self.assertTrue(observed["visible_after_restore"])
        self.assertEqual("NDI", observed["sample_name_after_restore"])
        self.assertEqual("cancel", response.action)

    def test_owner_close_cancels_open_book_picker_without_orphan_dialog(self):
        owner = self.QtWidgets.QWidget()
        owner.show()

        def interact(dialog):
            self._assert_runtime_nonmodal_and_antialiased(dialog, owner)
            owner.close()

        response = self._run_book_picker(
            AttributionBookSelectionRequest(
                folder_label="Folder A",
                source_filename="source.opj",
                choices=(("book-1", "Book 1"),),
                allow_return_to_folder=True,
            ),
            interact,
            parent=owner,
        )
        self.app.processEvents()
        self.assertEqual("cancel", response.action)
        self.assertFalse(
            any(
                widget.isVisible() and widget.objectName() == "organizer_dialog"
                for widget in self.app.topLevelWidgets()
            )
        )

    def test_production_form_silently_prefills_without_source_banner(self):
        cases = ("folder_heuristic", "task_local_reuse", "")

        for source in cases:
            with self.subTest(source=source):
                def interact(dialog):
                    notice = dialog.findChild(self.QtWidgets.QLabel, "dialog_prefill_notice")
                    self.assertIsNone(notice)
                    fields = dialog.findChildren(self.QtWidgets.QLineEdit)
                    self.assertIn("298 K", {field.text() for field in fields})
                    self._button(dialog, "取消并退出").click()

                response = self._run_form(
                    AttributionDialogRequest(
                        "Folder A",
                        "source.opj",
                        ("Book1",),
                        prefill={"temperature": "298 K"},
                        prefill_source=source,
                    ),
                    interact,
                )
                self.assertEqual(AttributionDialogResponse(action="cancel"), response)

    def test_production_form_wires_visible_fields_for_all_sample_modes(self):
        cases = {
            "solution": {
                "values": {
                    "样品名称": "MFL",
                    "溶剂或状态": "mTHF",
                    "浓度": "2e-4",
                    "温度": "298 K",
                },
                "visible": {"样品名称", "溶剂或状态", "浓度", "温度"},
            },
            "solid": {
                "values": {"样品名称": "MFL", "固体状态": "powder", "温度": "RT"},
                "visible": {"样品名称", "固体状态", "测量环境", "温度"},
            },
            "doped": {
                "values": {
                    "样品名称": "MFL",
                    "主体成分": "PMMA",
                    "浓度": "1.5",
                    "固体状态": "film",
                    "温度": "77K",
                },
                "visible": {"样品名称", "主体成分", "浓度", "固体状态", "测量环境", "温度"},
            },
        }
        response_names = {
            "样品名称": "sample",
            "溶剂或状态": "solvent",
            "主体成分": "host",
            "浓度": "concentration",
            "固体状态": "state",
            "温度": "temperature",
        }

        for mode, case in cases.items():
            with self.subTest(mode=mode):
                apply_remaining = mode == "doped"
                request = AttributionDialogRequest(
                    target_label="Folder A",
                    source_filename="source.opj",
                    book_display_names=("Book1",),
                    allow_apply_to_remaining_folder=apply_remaining,
                )

                def interact(dialog):
                    labels, fields, sample_type, unit = self._controls(dialog)
                    sample_type.setCurrentIndex(sample_type.findData(mode))
                    for name in response_names:
                        self.assertEqual(name in case["visible"], labels[name].isVisible())
                        self.assertEqual(name in case["visible"], fields[name].isVisible())
                    self.assertEqual(mode in {"solution", "doped"}, unit.isVisible())
                    if mode == "solution":
                        self.assertEqual("M", unit.currentText())
                        self.assertFalse(unit.isEnabled())
                    elif mode == "doped":
                        self.assertEqual(-1, unit.currentIndex())
                        self.assertTrue(unit.isEnabled())
                        self.assertEqual(-1, unit.findData("M"))
                        self.assertGreaterEqual(unit.findData("wt%"), 0)
                        self.assertGreaterEqual(unit.findData("mol%"), 0)
                    for name, value in case["values"].items():
                        fields[name].setText(value)
                    if mode in {"solid", "doped"}:
                        dialog.findChild(
                            self.QtWidgets.QPushButton,
                            "oxygen_environment_air",
                        ).click()
                    if mode == "doped":
                        unit.setCurrentText("mol%")
                        checkbox = next(
                            box
                            for box in dialog.findChildren(self.QtWidgets.QCheckBox)
                            if "其余未确认 Book" in box.text()
                        )
                        self.assertTrue(checkbox.isVisible())
                        checkbox.setChecked(True)
                    self._button(dialog, "确认").click()

                response = self._run_form(request, interact)
                expected_values = {
                    response_names[name]: value for name, value in case["values"].items()
                }
                if mode == "solution":
                    expected_values["concentration"] = "2×10^-4"
                if mode == "doped":
                    expected_values["concentration_unit"] = "mol%"
                if mode in {"solid", "doped"}:
                    expected_values["oxygen_environment"] = "Air"
                self.assertEqual(
                    AttributionDialogResponse(
                        action="confirm",
                        sample_type=mode,
                        values=expected_values,
                        apply_to_remaining_folder=apply_remaining,
                    ),
                    response,
                )

    def test_solid_environment_control_is_ordered_required_and_returned_canonically(self):
        def interact(dialog):
            labels, fields, sample_type, _unit = self._controls(dialog)
            sample_type.setCurrentIndex(sample_type.findData("solid"))
            selector = dialog.findChild(self.QtWidgets.QWidget, "oxygen_environment_selector")
            air = dialog.findChild(self.QtWidgets.QPushButton, "oxygen_environment_air")
            deo2 = dialog.findChild(self.QtWidgets.QPushButton, "oxygen_environment_deo2")
            self.assertIsNotNone(selector)
            self.assertTrue(selector.isVisible())
            self.assertEqual("空气中", air.text())
            self.assertEqual("绝氧", deo2.text())
            self.assertEqual((13, 400), (air.font().pixelSize(), air.font().weight()))
            self.assertEqual((13, 400), (deo2.font().pixelSize(), deo2.font().weight()))
            self.assertFalse(air.isChecked())
            self.assertFalse(deo2.isChecked())
            form = dialog.findChild(self.QtWidgets.QGridLayout, "attribution_form_layout")
            ordered_labels = [
                form.itemAtPosition(row, column).widget().text()
                for row in range(form.rowCount())
                for column in (0, 2)
                if form.itemAtPosition(row, column) is not None
            ]
            self.assertLess(ordered_labels.index("固体状态"), ordered_labels.index("测量环境"))
            self.assertLess(ordered_labels.index("测量环境"), ordered_labels.index("温度"))
            fields["样品名称"].setText("NDI")
            fields["固体状态"].setText("Solid")
            fields["温度"].setText("77 K")
            self._button(dialog, "确认").click()
            error = dialog.findChild(self.QtWidgets.QLabel, "dialog_error_text")
            self.assertEqual("请选择测量环境。", error.text())
            self.assertIs(dialog.focusWidget(), air)
            air.click()
            self._button(dialog, "确认").click()

        response = self._run_form(
            AttributionDialogRequest("Folder A", "source.opj", ("Book1",)),
            interact,
        )

        self.assertEqual("Air", response.values["oxygen_environment"])

    def test_environment_prefill_preserves_solid_to_doped_and_solution_clears_hidden_value(self):
        def interact(dialog):
            _labels, _fields, sample_type, _unit = self._controls(dialog)
            air = dialog.findChild(self.QtWidgets.QPushButton, "oxygen_environment_air")
            deo2 = dialog.findChild(self.QtWidgets.QPushButton, "oxygen_environment_deo2")
            sample_type.setCurrentIndex(sample_type.findData("solid"))
            self.assertTrue(deo2.isChecked())
            sample_type.setCurrentIndex(sample_type.findData("doped"))
            self.assertTrue(deo2.isChecked())
            self._button(dialog, "取消并退出").click()

        response = self._run_form(
            AttributionDialogRequest(
                "Folder vacuum",
                "source.opj",
                ("Book1",),
                prefill={"oxygen_environment": "DeO2"},
            ),
            interact,
        )
        self.assertEqual(AttributionDialogResponse(action="cancel"), response)

        def choose_solution(dialog):
            _labels, _fields, sample_type, _unit = self._controls(dialog)
            air = dialog.findChild(self.QtWidgets.QPushButton, "oxygen_environment_air")
            deo2 = dialog.findChild(self.QtWidgets.QPushButton, "oxygen_environment_deo2")
            sample_type.setCurrentIndex(sample_type.findData("solution"))
            self.assertFalse(air.isChecked())
            self.assertFalse(deo2.isChecked())
            self._button(dialog, "取消并退出").click()

        self._run_form(
            AttributionDialogRequest(
                "Folder air",
                "source.opj",
                ("Book1",),
                prefill={"oxygen_environment": "Air"},
            ),
            choose_solution,
        )

    def test_production_form_guides_fields_and_canonicalizes_concentration_on_focus_loss(self):
        def interact(dialog):
            _, fields, sample_type, unit = self._controls(dialog)
            sample_type.setCurrentIndex(sample_type.findData("solution"))
            self.assertTrue(all(field.placeholderText() for field in fields.values()))
            self.assertEqual("例如：DCM", fields["溶剂或状态"].placeholderText())
            fields["浓度"].setText("0.0001000 M")
            fields["浓度"].editingFinished.emit()
            self.assertEqual("1×10^-4", fields["浓度"].text())
            self.assertEqual("M", unit.currentText())
            self._button(dialog, "取消并退出").click()

        response = self._run_form(
            AttributionDialogRequest("Folder A", "source.opj", ("Book1",)),
            interact,
        )

        self.assertEqual(AttributionDialogResponse(action="cancel"), response)

    def test_required_field_validates_on_focus_loss_and_clears_while_correcting(self):
        def interact(dialog):
            _, fields, sample_type, _ = self._controls(dialog)
            error = dialog.findChild(self.QtWidgets.QLabel, "dialog_error_text")
            self.assertTrue(error.isHidden())
            sample_type.setCurrentIndex(sample_type.findData("solid"))

            fields["样品名称"].editingFinished.emit()
            self.assertEqual("请完整填写当前样品类型的所有必填项。", error.text())
            self.assertTrue(error.isVisible())

            fields["样品名称"].setText("MFL")
            self.assertEqual("", error.text())
            self.assertTrue(error.isHidden())
            self._button(dialog, "取消并退出").click()

        response = self._run_form(
            AttributionDialogRequest("Folder A", "source.opj", ("Book1",)),
            interact,
        )

        self.assertEqual(AttributionDialogResponse(action="cancel"), response)

    def test_temperature_validates_on_focus_loss(self):
        def interact(dialog):
            _, fields, sample_type, _ = self._controls(dialog)
            error = dialog.findChild(self.QtWidgets.QLabel, "dialog_error_text")
            sample_type.setCurrentIndex(sample_type.findData("solid"))
            fields["温度"].setText("0")

            fields["温度"].editingFinished.emit()

            self.assertEqual(
                "温度格式无效：请输入 RT、77 K 或大于 0 的 Kelvin 数值。",
                error.text(),
            )
            self._button(dialog, "取消并退出").click()

        response = self._run_form(
            AttributionDialogRequest("Folder A", "source.opj", ("Book1",)),
            interact,
        )

        self.assertEqual(AttributionDialogResponse(action="cancel"), response)

    def test_confirm_focuses_first_invalid_attribution_control(self):
        def interact(dialog):
            _, fields, sample_type, _ = self._controls(dialog)
            sample_type.setCurrentIndex(sample_type.findData("solid"))

            self._button(dialog, "确认").click()

            self.assertIs(fields["样品名称"], dialog.focusWidget())
            self._button(dialog, "取消并退出").click()

        response = self._run_form(
            AttributionDialogRequest("Folder A", "source.opj", ("Book1",)),
            interact,
        )

        self.assertEqual(AttributionDialogResponse(action="cancel"), response)

    def test_confirm_explains_combined_sample_label_overflow_and_focuses_sample_name(self):
        def interact(dialog):
            _, fields, sample_type, _ = self._controls(dialog)
            error = dialog.findChild(self.QtWidgets.QLabel, "dialog_error_text")
            sample_type.setCurrentIndex(sample_type.findData("solution"))
            fields["样品名称"].setText("S" * 125)
            fields["溶剂或状态"].setText("V" * 125)
            fields["浓度"].setText("1×10^-4")
            fields["温度"].setText("298")

            self._button(dialog, "确认").click()

            self.assertEqual(
                "样品信息组合后超过 Origin 名称长度上限，请缩短样品名称。",
                error.text(),
            )
            self.assertIs(fields["样品名称"], dialog.focusWidget())
            self._button(dialog, "取消并退出").click()

        response = self._run_form(
            AttributionDialogRequest("Folder A", "source.opj", ("Book1",)),
            interact,
        )

        self.assertEqual(AttributionDialogResponse(action="cancel"), response)

    def test_confirm_focuses_long_doped_host_when_combined_sample_label_overflows(self):
        def interact(dialog):
            _, fields, sample_type, unit = self._controls(dialog)
            error = dialog.findChild(self.QtWidgets.QLabel, "dialog_error_text")
            air = dialog.findChild(self.QtWidgets.QPushButton, "oxygen_environment_air")
            sample_type.setCurrentIndex(sample_type.findData("doped"))
            fields["样品名称"].setText("N")
            fields["主体成分"].setText("H" * 250)
            fields["浓度"].setText("1")
            fields["固体状态"].setText("S")
            fields["温度"].setText("77")
            unit.setCurrentIndex(unit.findData("wt%"))
            air.click()

            self._button(dialog, "确认").click()

            self.assertEqual(
                "样品信息组合后超过 Origin 名称长度上限，请缩短主体成分。",
                error.text(),
            )
            self.assertIs(fields["主体成分"], dialog.focusWidget())
            self._button(dialog, "取消并退出").click()

        response = self._run_form(
            AttributionDialogRequest("Folder A", "source.opj", ("Book1",)),
            interact,
        )

        self.assertEqual(AttributionDialogResponse(action="cancel"), response)

    def test_completed_attribution_dialog_is_destroyed_instead_of_retained_by_parent(self):
        import shiboken6

        parent = self.QtWidgets.QWidget()
        captured = []

        def interact(dialog):
            captured.append(dialog)
            self._button(dialog, "取消并退出").click()

        self._run_form(
            AttributionDialogRequest("Folder A", "source.opj", ("Book1",)),
            interact,
            parent=parent,
        )

        self.assertFalse(shiboken6.isValid(captured[0]))
        self.assertEqual([], parent.findChildren(self.QtWidgets.QDialog))

    def test_targeted_attribution_switches_scope_in_place_without_rewriting_fields(self):
        observed = {}

        def interact(dialog):
            labels, fields, sample_type, _unit = self._controls(dialog)
            folder_mode = dialog.findChild(
                self.QtWidgets.QPushButton,
                "attribution_folder_mode",
            )
            book_mode = dialog.findChild(
                self.QtWidgets.QPushButton,
                "attribution_book_mode",
            )
            notice = dialog.findChild(
                self.QtWidgets.QLabel,
                "attribution_targeted_scope_notice",
            )
            included = dialog.findChild(
                self.QtWidgets.QLabel,
                "attribution_included_books",
            )
            observed["initial_scope"] = (
                folder_mode.isChecked(),
                book_mode.isChecked(),
            )
            observed["notice_initially_visible"] = notice.isVisible()
            before = fields["样品名称"].text()
            folder_mode.click()
            self.app.processEvents()
            observed["sample_after_scope_switch"] = fields["样品名称"].text()
            observed["notice"] = notice.text()
            observed["included"] = included.text()
            observed["selected_type"] = sample_type.currentData()
            observed["state"] = fields["固体状态"].text()
            self.assertEqual(before, fields["样品名称"].text())
            self._button(dialog, "确认修改").click()

        response = self._run_form(
            AttributionDialogRequest(
                "Folder A / Book1",
                "source.opj",
                ("Book1", "Book2"),
                prefill={
                    "sample_type": "solid",
                    "sample": "MFL",
                    "state": "Solid",
                    "oxygen_environment": "Air",
                    "temperature": "298 K",
                },
                allow_split_folder=True,
                allow_return_previous=True,
                targeted_correction=True,
                initial_scope="book",
                selected_book_display_name="Book1",
                affected_book_count=2,
            ),
            interact,
        )

        self.assertEqual((False, True), observed["initial_scope"])
        self.assertFalse(observed["notice_initially_visible"])
        self.assertEqual("MFL", observed["sample_after_scope_switch"])
        self.assertIn("确认后将更新本 Folder 内 2 个 Book", observed["notice"])
        self.assertIn("Book1", observed["included"])
        self.assertIn("Book2", observed["included"])
        self.assertEqual("solid", observed["selected_type"])
        self.assertEqual("Solid", observed["state"])
        self.assertEqual("confirm", response.action)
        self.assertEqual("folder", response.attribution_scope)

    def test_targeted_attribution_cancel_returns_unconfirmed_fields_and_scope(self):
        def interact(dialog):
            _labels, fields, _sample_type, _unit = self._controls(dialog)
            folder_mode = dialog.findChild(
                self.QtWidgets.QPushButton,
                "attribution_folder_mode",
            )
            folder_mode.click()
            fields["样品名称"].setText("PFL")
            fields["温度"].setText("77")
            self._button(dialog, "取消并退出").click()

        response = self._run_form(
            AttributionDialogRequest(
                "Folder A / Book1",
                "source.opj",
                ("Book1", "Book2"),
                prefill={
                    "sample_type": "solid",
                    "sample": "MFL",
                    "state": "Solid",
                    "oxygen_environment": "Air",
                    "temperature": "298 K",
                },
                allow_split_folder=True,
                allow_return_previous=True,
                targeted_correction=True,
                initial_scope="book",
                selected_book_display_name="Book1",
                affected_book_count=2,
            ),
            interact,
        )

        self.assertEqual("cancel", response.action)
        self.assertEqual("solid", response.sample_type)
        self.assertEqual("PFL", response.values["sample"])
        self.assertEqual("Solid", response.values["state"])
        self.assertEqual("Air", response.values["oxygen_environment"])
        self.assertEqual("77", response.values["temperature"])
        self.assertEqual("folder", response.attribution_scope)

    def test_targeted_unconfirmed_doped_concentration_without_unit_survives_reopen(self):
        observed = {}

        def interact(dialog):
            _labels, fields, sample_type, unit = self._controls(dialog)
            observed["sample_type"] = sample_type.currentData()
            observed["concentration"] = fields["浓度"].text()
            observed["unit_index"] = unit.currentIndex()
            observed["unit"] = str(unit.currentData() or "")
            self._button(dialog, "取消并退出").click()

        response = self._run_form(
            AttributionDialogRequest(
                "Folder A / Book1",
                "source.opj",
                ("Book1", "Book2"),
                prefill={
                    "sample_type": "doped",
                    "sample": "MFL",
                    "host": "PMMA",
                    "concentration": "7.25",
                    "concentration_unit": "",
                    "state": "Film",
                    "oxygen_environment": "Air",
                    "temperature": "77 K",
                },
                prefill_source="unconfirmed_draft",
                allow_split_folder=True,
                allow_return_previous=True,
                targeted_correction=True,
                initial_scope="folder",
                selected_book_display_name="Book1",
                affected_book_count=2,
            ),
            interact,
        )

        self.assertEqual("doped", observed["sample_type"])
        self.assertEqual("7.25", observed["concentration"])
        self.assertEqual(-1, observed["unit_index"])
        self.assertEqual("", observed["unit"])
        self.assertEqual("cancel", response.action)
        self.assertEqual("7.25", response.values["concentration"])
        self.assertEqual("", response.values["concentration_unit"])
        self.assertEqual("folder", response.attribution_scope)

    def test_targeted_attribution_close_escape_and_native_close_return_previous(self):
        request = AttributionDialogRequest(
            "Folder A / Book1",
            "source.opj",
            ("Book1", "Book2"),
            prefill={
                "sample_type": "solid",
                "sample": "MFL",
                "state": "Solid",
                "oxygen_environment": "Air",
                "temperature": "298 K",
            },
            allow_split_folder=True,
            allow_return_previous=True,
            targeted_correction=True,
            initial_scope="folder",
            selected_book_display_name="Book1",
            affected_book_count=2,
        )

        close_response = self._run_form(
            request,
            lambda dialog: dialog.findChild(
                self.QtWidgets.QPushButton,
                "dialog_close_button",
            ).click(),
        )
        escape_response = self._run_form(
            request,
            lambda dialog: self.QtTest.QTest.keyClick(
                dialog,
                self.QtCore.Qt.Key.Key_Escape,
            ),
        )
        native_close_response = self._run_form(
            request,
            lambda dialog: dialog.close(),
        )

        self.assertEqual("return_previous", close_response.action)
        self.assertEqual("return_previous", escape_response.action)
        self.assertEqual("return_previous", native_close_response.action)

    def test_embedded_choices_use_body_typography_while_actions_remain_prominent(self):
        observed = []
        decision_gaps = []

        def capture(dialog):
            self.app.processEvents()
            buttons = [
                button
                for button in dialog.findChildren(self.QtWidgets.QPushButton)
                if button.objectName()
                in {
                    "attribution_folder_mode",
                    "attribution_book_mode",
                    "dialog_button_primary",
                    "dialog_button_secondary",
                    "dialog_button_danger",
                }
                and button.isVisible()
            ]
            observed.extend(
                (
                    button.objectName(),
                    button.text(),
                    button.height(),
                    button.font().pixelSize(),
                    button.font().weight(),
                )
                for button in buttons
            )
            primary = next(
                (
                    button
                    for button in buttons
                    if button.objectName() == "dialog_button_primary"
                ),
                None,
            )
            danger = next(
                (
                    button
                    for button in buttons
                    if button.objectName() == "dialog_button_danger"
                ),
                None,
            )
            if (
                primary is not None
                and danger is not None
                and primary.geometry().top() == danger.geometry().top()
            ):
                left, right = sorted((primary, danger), key=lambda button: button.x())
                decision_gaps.append(right.x() - left.geometry().right() - 1)
            self.assertIsNotNone(danger)
            danger.click()

        self._run_form(
            AttributionDialogRequest(
                "Folder A",
                "source.opj",
                ("Book1", "Book2"),
                allow_split_folder=True,
            ),
            capture,
        )
        self._run_dialog(
            lambda: dialog_port_module.show_conflict_review_dialog(
                ConflictReviewRequest(
                    kind="emission_duplicate",
                    title="选择重复发射谱",
                    instruction="请选择保留项。",
                    choices=(
                        ConflictReviewChoice("a", "A"),
                        ConflictReviewChoice("b", "B"),
                    ),
                    selection_mode="single",
                    actions=("confirm_selection", "cancel"),
                )
            ),
            capture,
            timeout_message="conflict action inspection timed out",
        )
        self._run_dialog(
            lambda: dialog_port_module.show_styled_dialog(
                DialogRequest(
                    kind="confirmation",
                    title="确认",
                    message="请确认。",
                    actions=("confirm", "cancel"),
                )
            ),
            capture,
            timeout_message="generic action inspection timed out",
        )

        self.assertGreaterEqual(len(observed), 8, observed)
        self.assertEqual(
            {42},
            {height for _name, _text, height, _font, _weight in observed},
            observed,
        )
        embedded = [
            item
            for item in observed
            if item[0] in {"attribution_folder_mode", "attribution_book_mode"}
        ]
        actions = [item for item in observed if item not in embedded]
        self.assertEqual(2, len(embedded), observed)
        self.assertTrue(
            all(font_px == 13 for _name, _text, _height, font_px, _weight in embedded),
            embedded,
        )
        self.assertEqual(
            {400},
            {weight for _name, _text, _height, _font, weight in embedded},
            embedded,
        )
        self.assertTrue(
            all(font_px >= 14 for _name, _text, _height, font_px, _weight in actions),
            actions,
        )
        self.assertEqual(
            {600},
            {weight for _name, _text, _height, _font, weight in actions},
            actions,
        )
        self.assertEqual([8, 8, 8], decision_gaps, decision_gaps)

    def test_moved_conflict_dialog_position_is_reused_by_the_next_dialog(self):
        class PointerEvent:
            def __init__(self, point, *, pressed):
                self._point = point
                self._pressed = pressed

            def button(self):
                return self.QtCore.Qt.MouseButton.LeftButton

            def buttons(self):
                if self._pressed:
                    return self.QtCore.Qt.MouseButton.LeftButton
                return self.QtCore.Qt.MouseButton.NoButton

            def globalPosition(self):
                return self.QtCore.QPointF(self._point)

            def accept(self):
                pass

        PointerEvent.QtCore = self.QtCore
        parent = self.QtWidgets.QWidget()
        parent.resize(760, 720)
        parent.show()
        self.app.processEvents()
        positions = []
        request = ConflictReviewRequest(
            kind="emission_duplicate",
            title="选择重复发射谱",
            instruction="请选择保留项。",
            choices=(
                ConflictReviewChoice("a", "A"),
                ConflictReviewChoice("b", "B"),
            ),
            selection_mode="single",
            actions=("confirm_selection", "cancel"),
        )

        def move_and_close(dialog):
            header = dialog.findChild(self.QtWidgets.QFrame, "dialog_header")
            available = dialog.screen().availableGeometry()
            target = available.topLeft() + self.QtCore.QPoint(18, 18)
            grab_offset = self.QtCore.QPoint(30, 20)
            start = dialog.frameGeometry().topLeft() + grab_offset
            pointer = target + grab_offset
            header.mousePressEvent(PointerEvent(start, pressed=True))
            header.mouseMoveEvent(PointerEvent(pointer, pressed=True))
            header.mouseReleaseEvent(PointerEvent(pointer, pressed=False))
            self.app.processEvents()
            positions.append(dialog.frameGeometry().topLeft())
            self._button(dialog, "取消并退出").click()

        def capture_and_close(dialog):
            self.app.processEvents()
            positions.append(dialog.frameGeometry().topLeft())
            self._button(dialog, "取消并退出").click()

        try:
            for interact in (move_and_close, capture_and_close):
                self._run_dialog(
                    lambda: dialog_port_module.show_conflict_review_dialog(
                        request,
                        parent=parent,
                    ),
                    interact,
                    timeout_message="sequential conflict dialog timed out",
                )
        finally:
            parent.close()
            self.app.processEvents()

        self.assertEqual(2, len(positions))
        self.assertEqual(positions[0], positions[1], positions)

    def test_nonmodal_disposal_destroys_native_window_before_returning(self):
        events = []

        class TrackingDialog(self.QtWidgets.QDialog):
            def hide(self):
                events.append("hide")
                super().hide()

            def destroy(self, destroy_window=True, destroy_subwindows=True):
                events.append("destroy")
                super().destroy(destroy_window, destroy_subwindows)

        dialog = TrackingDialog()
        dialog.show()
        self.app.processEvents()

        dialog_port_module._dispose_nonmodal_dialog(dialog, self.QtCore)

        self.assertIn("hide", events)
        self.assertIn("destroy", events)
        self.assertLess(events.index("hide"), events.index("destroy"))

        parent = self.QtWidgets.QWidget()
        request = ConflictReviewRequest(
            kind="emission_duplicate",
            title="选择重复发射谱",
            instruction="必须保留一个。",
            choices=(
                ConflictReviewChoice("a", "A"),
                ConflictReviewChoice("b", "B"),
            ),
            selection_mode="single",
        )
        with mock.patch.object(
            dialog_port_module,
            "_make_windows_taskbar_window",
            side_effect=RuntimeError("INJECTED_TASKBAR_FAILURE"),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "INJECTED_TASKBAR_FAILURE",
            ):
                dialog_port_module.show_conflict_review_dialog(
                    request,
                    parent=parent,
                )
        self.app.processEvents()
        self.assertEqual([], parent.findChildren(self.QtWidgets.QDialog))

        taskbar_failure = RuntimeError("INJECTED_TASKBAR_FAILURE")
        with mock.patch.object(
            dialog_port_module,
            "_make_windows_taskbar_window",
            side_effect=taskbar_failure,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "INJECTED_TASKBAR_FAILURE",
            ):
                dialog_port_module.show_attribution_dialog(
                    AttributionDialogRequest(
                        "Folder A",
                        "source.opj",
                        ("Book1",),
                    ),
                    parent=parent,
                )
            with self.assertRaisesRegex(
                RuntimeError,
                "INJECTED_TASKBAR_FAILURE",
            ):
                dialog_port_module.show_attribution_book_picker(
                    AttributionBookSelectionRequest(
                        folder_label="Folder A",
                        source_filename="source.opj",
                        choices=(("book-1", "Book 1"),),
                        allow_return_to_folder=True,
                    ),
                    parent=parent,
                )
        self.app.processEvents()
        self.assertEqual([], parent.findChildren(self.QtWidgets.QDialog))

    def test_new_doped_attribution_requires_explicit_concentration_unit(self):
        def interact(dialog):
            _, fields, sample_type, unit = self._controls(dialog)
            sample_type.setCurrentIndex(sample_type.findData("doped"))
            self.assertEqual(-1, unit.currentIndex())
            values = {
                "样品名称": "MFL",
                "主体成分": "PMMA",
                "浓度": "1",
                "固体状态": "Film",
                "温度": "298",
            }
            for name, value in values.items():
                fields[name].setText(value)
            self._button(dialog, "确认").click()
            error = dialog.findChild(self.QtWidgets.QLabel, "dialog_error_text")
            self.assertIn("选择 wt% 或 mol%", error.text())
            self.assertTrue(dialog.isVisible())
            self._button(dialog, "取消并退出").click()

        response = self._run_form(
            AttributionDialogRequest("Folder A", "source.opj", ("Book1",)),
            interact,
        )

        self.assertEqual(AttributionDialogResponse(action="cancel"), response)

    def test_production_form_keeps_open_and_reports_required_field_errors(self):
        def interact(dialog):
            _, _, sample_type, _ = self._controls(dialog)
            confirm = self._button(dialog, "确认")
            error = dialog.findChild(self.QtWidgets.QLabel, "dialog_error_text")

            confirm.click()
            self.assertEqual("请先选择样品类型。", error.text())
            self.assertTrue(dialog.isVisible())

            sample_type.setCurrentIndex(sample_type.findData("solid"))
            confirm.click()
            self.assertEqual("请完整填写当前样品类型的所有必填项。", error.text())
            self.assertTrue(dialog.isVisible())
            self._button(dialog, "取消并退出").click()

        response = self._run_form(
            AttributionDialogRequest("Folder A", "source.opj", ("Book1",)),
            interact,
        )

        self.assertEqual(AttributionDialogResponse(action="cancel"), response)

    def test_production_form_opens_per_book_selection_without_intermediate_toggle_state(self):
        def interact(dialog):
            split_button = self._button(dialog, "逐 Book")
            self.assertTrue(split_button.isVisible())
            split_button.click()

        response = self._run_form(
            AttributionDialogRequest(
                "Folder A",
                "source.opj",
                ("Book1", "Book2"),
                allow_split_folder=True,
            ),
            interact,
        )

        self.assertEqual(
            AttributionDialogResponse(action="split_folder", split_folder=True),
            response,
        )

    def test_per_book_form_can_return_to_pending_book_picker_without_confirmation(self):
        def interact(dialog):
            return_button = self._button(dialog, "返回选择 Book")
            self.assertTrue(return_button.isVisible())
            return_button.click()

        response = self._run_form(
            AttributionDialogRequest(
                "Folder A / F300",
                "source.opj",
                ("F300",),
                allow_apply_to_remaining_folder=True,
                allow_return_to_book_picker=True,
            ),
            interact,
        )

        self.assertEqual(AttributionDialogResponse(action="return_to_book_picker"), response)

    def test_attribution_form_can_return_to_previous_confirmed_target(self):
        def interact(dialog):
            return_button = self._button(dialog, "返回上一步")
            self.assertTrue(return_button.isVisible())
            return_button.click()

        response = self._run_form(
            AttributionDialogRequest(
                "Folder B",
                "source.opj",
                ("Book B",),
                allow_return_previous=True,
            ),
            interact,
        )

        self.assertEqual(AttributionDialogResponse(action="return_previous"), response)

    def test_pending_book_picker_requires_explicit_selection_and_returns_chosen_book(self):
        def interact(dialog):
            book_list = dialog.findChild(
                self.QtWidgets.QListWidget,
                "attribution_pending_book_list",
            )
            confirm = self._button(dialog, "确认选择")
            self.assertIsNotNone(book_list)
            self.assertEqual(-1, book_list.currentRow())
            self.assertFalse(confirm.isEnabled())

            book_list.setCurrentRow(1)
            self.assertTrue(confirm.isEnabled())
            confirm.click()

        response = self._run_book_picker(
            AttributionBookSelectionRequest(
                folder_label="Folder A",
                source_filename="source.opj",
                choices=(("book-1", "F270"), ("book-2", "F300")),
                allow_return_to_folder=True,
            ),
            interact,
        )

        self.assertEqual(
            AttributionBookSelectionResponse(action="select_book", book_key="book-2"),
            response,
        )

    def test_pending_book_picker_keeps_duplicate_long_names_visibly_distinct(self):
        def interact(dialog):
            book_list = dialog.findChild(
                self.QtWidgets.QListWidget,
                "attribution_pending_book_list",
            )
            self.assertEqual(
                ["Same Long Name (PE1)", "Same Long Name (PE2)"],
                [book_list.item(row).text() for row in range(book_list.count())],
            )
            book_list.setCurrentRow(1)
            self._button(dialog, "确认选择").click()

        response = self._run_book_picker(
            AttributionBookSelectionRequest(
                folder_label="Mixed",
                source_filename="mixed.opj",
                choices=(
                    ("book-pe1", "Same Long Name (PE1)"),
                    ("book-pe2", "Same Long Name (PE2)"),
                ),
                allow_return_to_folder=True,
            ),
            interact,
        )

        self.assertEqual(
            AttributionBookSelectionResponse(action="select_book", book_key="book-pe2"),
            response,
        )

    def test_pending_book_picker_reserves_enough_text_height_for_underscores(self):
        def interact(dialog):
            book_list = dialog.findChild(
                self.QtWidgets.QListWidget,
                "attribution_pending_book_list",
            )
            self.app.processEvents()
            item = book_list.item(0)
            index = book_list.indexFromItem(item)
            option = self.QtWidgets.QStyleOptionViewItem()
            option.initFrom(book_list)
            option.rect = book_list.visualItemRect(item)
            book_list.itemDelegate().initStyleOption(option, index)
            text_rect = book_list.style().subElementRect(
                self.QtWidgets.QStyle.SubElement.SE_ItemViewItemText,
                option,
                book_list,
            )
            self.assertEqual("285_2_2", item.text())
            self.assertGreaterEqual(text_rect.height(), option.fontMetrics.height())
            self._button(dialog, "取消并退出").click()

        self._run_book_picker(
            AttributionBookSelectionRequest(
                folder_label="DiMeFL_DCM",
                source_filename="20240923_TMeFL_77K.opj",
                choices=(("book-1", "285_2_2"),),
            ),
            interact,
        )

    def test_unbroken_book_names_wrap_to_readable_height(self):
        book_name = "B" * 200

        def inspect_picker(dialog):
            book_list = dialog.findChild(
                self.QtWidgets.QListWidget,
                "attribution_pending_book_list",
            )
            self.app.processEvents()
            item_rect = book_list.visualItemRect(book_list.item(0))
            self.assertLessEqual(item_rect.width(), book_list.viewport().width())
            self.assertGreaterEqual(
                item_rect.height(),
                2 * book_list.fontMetrics().lineSpacing(),
            )
            self._button(dialog, "取消并退出").click()

        self._run_book_picker(
            AttributionBookSelectionRequest(
                folder_label="Folder A",
                source_filename="source.opj",
                choices=(("book-1", book_name),),
            ),
            inspect_picker,
        )

        def inspect_form(dialog):
            included = dialog.findChild(
                self.QtWidgets.QLabel,
                "attribution_included_books",
            )
            self.app.processEvents()
            required = included.fontMetrics().boundingRect(
                self.QtCore.QRect(0, 0, included.width(), 10000),
                self.QtCore.Qt.TextFlag.TextWrapAnywhere,
                included.text(),
            ).height()
            self.assertGreaterEqual(included.height(), required)
            self._button(dialog, "取消并退出").click()

        self._run_form(
            AttributionDialogRequest("Folder A", "source.opj", (book_name,)),
            inspect_form,
        )

    def test_pending_book_picker_fits_compact_screen_at_150_percent_scale(self):
        script = r'''
from PySide6 import QtCore, QtWidgets
from spectrum_organizer.ui.dialog_port import (
    AttributionBookSelectionRequest,
    show_attribution_book_picker,
)

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
observed = {}

def inspect():
    dialog = next(
        widget for widget in app.topLevelWidgets()
        if widget.isVisible() and widget.objectName() == "organizer_dialog"
    )
    available = dialog.screen().availableGeometry()
    observed["dialog"] = (dialog.width(), dialog.height())
    observed["available"] = (available.width(), available.height())
    dialog.reject()

QtCore.QTimer.singleShot(0, inspect)
show_attribution_book_picker(
    AttributionBookSelectionRequest(
        folder_label="Folder A",
        source_filename="source.opj",
        choices=tuple((f"book-{index}", f"Book {index}") for index in range(1, 13)),
        allow_return_to_folder=True,
    )
)
dialog_width, dialog_height = observed["dialog"]
available_width, available_height = observed["available"]
if dialog_width > available_width or dialog_height > available_height:
    raise SystemExit(
        f"picker {dialog_width}x{dialog_height} exceeds available "
        f"{available_width}x{available_height}"
    )
'''
        completed = _run_qt_script(script, scale_factor="1.5")
        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)

    def test_compact_pending_book_picker_keeps_return_actions_readable(self):
        script = r'''
from PySide6 import QtCore, QtWidgets
from spectrum_organizer.ui.dialog_port import (
    AttributionBookSelectionRequest,
    AttributionDialogRequest,
    show_attribution_book_picker,
    show_attribution_dialog,
)

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

def assert_action_widths(dialog, scroll, labels):
    app.processEvents()
    for text in labels:
        button = next(
            button for button in dialog.findChildren(QtWidgets.QPushButton)
            if button.text() == text
        )
        required = button.fontMetrics().horizontalAdvance(text) + 24
        if button.width() < required:
            raise SystemExit(
                f"action {text!r} clipped: width={button.width()} required={required}"
            )
        scroll.ensureWidgetVisible(button)
        app.processEvents()
        left = button.mapTo(scroll.viewport(), QtCore.QPoint(0, 0)).x()
        if left < 0 or left + button.width() > scroll.viewport().width():
            raise SystemExit(
                f"action {text!r} horizontally unreachable: left={left} "
                f"right={left + button.width()} viewport={scroll.viewport().width()}"
            )

def inspect_picker():
    dialog = next(
        widget for widget in app.topLevelWidgets()
        if widget.isVisible() and widget.objectName() == "organizer_dialog"
    )
    dialog.setFixedWidth(396)
    scroll = dialog.findChild(QtWidgets.QScrollArea, "attribution_picker_scroll")
    assert_action_widths(
        dialog,
        scroll,
        ("返回 Folder 统一归属", "确认选择", "取消并退出"),
    )
    dialog.reject()

QtCore.QTimer.singleShot(100, inspect_picker)
show_attribution_book_picker(
    AttributionBookSelectionRequest(
        folder_label="Folder A",
        source_filename="source.opj",
        choices=(("book-1", "Book 1"),),
        allow_return_to_folder=True,
    )
)

def inspect_form():
    dialog = next(
        widget for widget in app.topLevelWidgets()
        if widget.isVisible() and widget.objectName() == "organizer_dialog"
    )
    dialog.setFixedWidth(396)
    scroll = dialog.findChild(QtWidgets.QScrollArea, "attribution_body_scroll")
    assert_action_widths(
        dialog,
        scroll,
        ("返回选择 Book", "确认", "取消并退出"),
    )
    dialog.reject()

QtCore.QTimer.singleShot(100, inspect_form)
show_attribution_dialog(
    AttributionDialogRequest(
        target_label="Folder A / Book 1",
        source_filename="source.opj",
        book_display_names=("Book 1",),
        allow_return_to_book_picker=True,
    )
)
'''
        completed = _run_qt_script(script, scale_factor="1.5")
        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)

    def test_pending_book_picker_viewport_uses_dialog_body_surface(self):
        script = r'''
from PySide6 import QtCore, QtGui, QtWidgets
from spectrum_organizer.ui.dialog_port import (
    AttributionBookSelectionRequest,
    show_attribution_book_picker,
)

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

def inspect():
    dialog = next(
        widget for widget in app.topLevelWidgets()
        if widget.isVisible() and widget.objectName() == "organizer_dialog"
    )
    scroll = dialog.findChild(QtWidgets.QScrollArea, "attribution_picker_scroll")
    if scroll is None:
        raise SystemExit("pending-Book picker scroll area is missing")
    app.processEvents()
    actual = scroll.viewport().palette().color(
        QtGui.QPalette.ColorRole.Window
    ).name().lower()
    if actual != "#f5f7f6":
        raise SystemExit(f"picker viewport surface={actual}, expected #f5f7f6")
    dialog.reject()

QtCore.QTimer.singleShot(0, inspect)
show_attribution_book_picker(
    AttributionBookSelectionRequest(
        folder_label="Folder A",
        source_filename="source.opj",
        choices=(("book-1", "Book 1"),),
        allow_return_to_folder=True,
    )
)
'''
        completed = _run_qt_script(script)
        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)

    def test_pending_book_picker_renders_selected_row_in_approved_teal(self):
        script = r'''
from PySide6 import QtCore, QtGui, QtWidgets
from spectrum_organizer.ui.dialog_port import (
    AttributionBookSelectionRequest,
    show_attribution_book_picker,
)

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

def inspect():
    dialog = next(
        widget for widget in app.topLevelWidgets()
        if widget.isVisible() and widget.objectName() == "organizer_dialog"
    )
    book_list = dialog.findChild(QtWidgets.QListWidget)
    if book_list is None:
        raise SystemExit("pending-Book list is missing")
    class NativeBlueStyle(QtWidgets.QProxyStyle):
        def drawControl(self, element, option, painter, widget=None):
            if element == QtWidgets.QStyle.ControlElement.CE_ItemViewItem:
                painter.fillRect(option.rect, QtGui.QColor("#c7ddf4"))
                return
            super().drawControl(element, option, painter, widget)

        def subElementRect(self, element, option, widget=None):
            if element == QtWidgets.QStyle.SubElement.SE_ItemViewItemText:
                return option.rect.adjusted(8, 0, -8, 0)
            return super().subElementRect(element, option, widget)

    native_style = NativeBlueStyle(book_list.style())
    book_list._test_native_style = native_style
    book_list.setStyle(native_style)
    book_list.setCurrentRow(0)
    book_list.setFocus()
    app.processEvents()
    for state_name, active_flag in (
        ("ACTIVE", QtWidgets.QStyle.StateFlag.State_Active),
        ("INACTIVE", QtWidgets.QStyle.StateFlag.State_None),
    ):
        image = QtGui.QImage(240, 42, QtGui.QImage.Format.Format_ARGB32)
        image.fill(QtCore.Qt.GlobalColor.transparent)
        option = QtWidgets.QStyleOptionViewItem()
        option.initFrom(book_list)
        option.rect = QtCore.QRect(0, 0, 240, 42)
        option.widget = book_list
        if active_flag == QtWidgets.QStyle.StateFlag.State_Active:
            option.state |= QtWidgets.QStyle.StateFlag.State_Active
        else:
            option.state &= ~QtWidgets.QStyle.StateFlag.State_Active
        option.state |= (
            QtWidgets.QStyle.StateFlag.State_Enabled
            | QtWidgets.QStyle.StateFlag.State_Selected
        )
        observed_active = bool(option.state & QtWidgets.QStyle.StateFlag.State_Active)
        expected_active = active_flag == QtWidgets.QStyle.StateFlag.State_Active
        if observed_active != expected_active:
            raise SystemExit(
                f"{state_name} active-state mismatch: "
                f"observed={observed_active}, expected={expected_active}"
            )
        painter = QtGui.QPainter(image)
        book_list.itemDelegate().paint(painter, option, book_list.model().index(0, 0))
        painter.end()
        sample = image.pixelColor(220, 21).name().lower()
        has_white_text = any(
            image.pixelColor(x, y).alpha() > 0
            and image.pixelColor(x, y).red() >= 245
            and image.pixelColor(x, y).green() >= 245
            and image.pixelColor(x, y).blue() >= 245
            for y in range(image.height())
            for x in range(image.width())
        )
        print(f"{state_name}_SELECTED_COLOR={sample}", flush=True)
        print(f"{state_name}_WHITE_TEXT={has_white_text}", flush=True)
    next(
        button
        for button in dialog.findChildren(QtWidgets.QPushButton)
        if button.text() == "取消并退出"
    ).click()

QtCore.QTimer.singleShot(100, inspect)
show_attribution_book_picker(
    AttributionBookSelectionRequest(
        folder_label="Folder_A",
        source_filename="source.opj",
        choices=(("book_1", "Dewar_360"), ("book_2", "Tube_270_77K")),
        allow_return_to_folder=True,
    )
)
'''
        completed = _run_qt_script(script)
        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        self.assertIn("ACTIVE_SELECTED_COLOR=#147a6c", completed.stdout)
        self.assertIn("INACTIVE_SELECTED_COLOR=#147a6c", completed.stdout)
        self.assertIn("ACTIVE_WHITE_TEXT=True", completed.stdout)
        self.assertIn("INACTIVE_WHITE_TEXT=True", completed.stdout)

    def test_pending_book_picker_distinguishes_selected_hovered_and_neutral_rows(self):
        script = r'''
from PySide6 import QtCore, QtTest, QtWidgets
from spectrum_organizer.ui.dialog_port import (
    AttributionBookSelectionRequest,
    show_attribution_book_picker,
)

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

def inspect():
    dialog = next(
        widget for widget in app.topLevelWidgets()
        if widget.isVisible() and widget.objectName() == "organizer_dialog"
    )
    book_list = dialog.findChild(
        QtWidgets.QListWidget,
        "attribution_pending_book_list",
    )
    if book_list is None:
        raise SystemExit("pending-Book list is missing")
    book_list.setCurrentRow(0)
    app.processEvents()
    hovered_rect = book_list.visualItemRect(book_list.item(1))
    QtTest.QTest.mouseMove(book_list.viewport(), hovered_rect.center())
    QtTest.QTest.qWait(50)
    image = book_list.viewport().grab().toImage()

    def row_color(row):
        rect = book_list.visualItemRect(book_list.item(row))
        return image.pixelColor(rect.right() - 12, rect.center().y()).name().lower()

    print(f"SELECTED={row_color(0)}", flush=True)
    print(f"HOVERED={row_color(1)}", flush=True)
    print(f"NEUTRAL={row_color(2)}", flush=True)
    dialog.reject()

QtCore.QTimer.singleShot(100, inspect)
show_attribution_book_picker(
    AttributionBookSelectionRequest(
        folder_label="Folder_A",
        source_filename="source.opj",
        choices=(
            ("book_1", "Dewar_360"),
            ("book_2", "Tube_270_77K"),
            ("book_3", "Blank_320"),
        ),
        allow_return_to_folder=True,
    )
)
'''
        completed = _run_qt_script(script)
        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        self.assertIn("SELECTED=#147a6c", completed.stdout)
        self.assertIn("HOVERED=#dcebe7", completed.stdout)
        self.assertIn("NEUTRAL=#ffffff", completed.stdout)

    def test_pending_book_picker_keeps_long_context_and_actions_reachable(self):
        script = r'''
from PySide6 import QtCore, QtWidgets
from spectrum_organizer.ui.dialog_port import (
    AttributionBookSelectionRequest,
    show_attribution_book_picker,
)

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
observed = {}

def inspect():
    dialog = next(
        widget for widget in app.topLevelWidgets()
        if widget.isVisible() and widget.objectName() == "organizer_dialog"
    )
    scroll = dialog.findChild(QtWidgets.QScrollArea, "attribution_picker_scroll")
    context = dialog.findChild(QtWidgets.QLabel, "attribution_picker_context")
    cancel = next(button for button in dialog.findChildren(QtWidgets.QPushButton) if button.text() == "取消并退出")
    if scroll is None or context is None:
        raise SystemExit("picker does not expose scrollable context")
    app.processEvents()
    required_height = context.heightForWidth(context.width())
    if context.height() < required_height:
        raise SystemExit(f"context clipped: {context.height()} < {required_height}")
    scroll.ensureWidgetVisible(cancel)
    app.processEvents()
    top = cancel.mapTo(scroll.viewport(), QtCore.QPoint(0, 0)).y()
    bottom = top + cancel.height()
    if top < 0 or bottom > scroll.viewport().height():
        raise SystemExit(
            f"cancel action unreachable: top={top} bottom={bottom} "
            f"viewport={scroll.viewport().height()}"
        )
    observed["ok"] = True
    dialog.reject()

QtCore.QTimer.singleShot(100, inspect)
show_attribution_book_picker(
    AttributionBookSelectionRequest(
        folder_label=" / ".join(f"Folder segment {index}" for index in range(10)),
        source_filename="source_" + "very_long_" * 18 + ".opj",
        choices=tuple((f"book-{index}", f"Book {index}") for index in range(1, 13)),
        allow_return_to_folder=True,
    )
)
if not observed.get("ok"):
    raise SystemExit("picker inspection did not complete")
'''
        completed = _run_qt_script(script, scale_factor="1.5")
        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)

    def test_pending_book_picker_contains_unbroken_context_and_actions_horizontally(self):
        script = r'''
from PySide6 import QtCore, QtWidgets
from spectrum_organizer.ui.dialog_port import (
    AttributionBookSelectionRequest,
    show_attribution_book_picker,
)

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
source = "A" * 220 + ".opj"
folder = "F" * 220
book_name = "B" * 200
expected_context = f"{source}  ·  {folder}\n请选择一个尚未确认归属的 Book。"

def assert_visible(scroll, widget):
    scroll.ensureWidgetVisible(widget)
    app.processEvents()
    top_left = widget.mapTo(scroll.viewport(), QtCore.QPoint(0, 0))
    if top_left.x() < 0 or top_left.x() + widget.width() > scroll.viewport().width():
        raise SystemExit(
            f"{widget.text()} horizontally unreachable: x={top_left.x()} "
            f"right={top_left.x() + widget.width()} viewport={scroll.viewport().width()}"
        )
    if top_left.y() < 0 or top_left.y() + widget.height() > scroll.viewport().height():
        raise SystemExit(
            f"{widget.text()} vertically unreachable: y={top_left.y()} "
            f"bottom={top_left.y() + widget.height()} viewport={scroll.viewport().height()}"
        )

def inspect():
    dialog = next(
        widget for widget in app.topLevelWidgets()
        if widget.isVisible() and widget.objectName() == "organizer_dialog"
    )
    scroll = dialog.findChild(QtWidgets.QScrollArea, "attribution_picker_scroll")
    context = dialog.findChild(QtWidgets.QLabel, "attribution_picker_context")
    book_list = dialog.findChild(QtWidgets.QListWidget, "attribution_pending_book_list")
    if scroll is None or context is None or book_list is None:
        raise SystemExit("picker does not expose context scroll ownership")
    app.processEvents()
    if context.text() != expected_context:
        raise SystemExit("picker context text changed")
    required_context_height = context.fontMetrics().boundingRect(
        QtCore.QRect(0, 0, max(1, context.width()), 10000),
        QtCore.Qt.AlignmentFlag.AlignLeft
        | QtCore.Qt.AlignmentFlag.AlignVCenter
        | QtCore.Qt.TextFlag.TextWrapAnywhere,
        context.text(),
    ).height()
    if context.height() < required_context_height:
        raise SystemExit(
            f"picker context clipped: {context.height()} < {required_context_height}"
        )
    if scroll.horizontalScrollBar().maximum() != 0:
        raise SystemExit(f"picker horizontal range={scroll.horizontalScrollBar().maximum()}")
    if scroll.widget().width() > scroll.viewport().width():
        raise SystemExit(
            f"picker body wider than viewport: {scroll.widget().width()} > {scroll.viewport().width()}"
        )
    if not book_list.wordWrap():
        raise SystemExit("pending Book list does not wrap item text")
    if book_list.horizontalScrollBar().maximum() != 0:
        raise SystemExit(
            f"pending Book list horizontal range={book_list.horizontalScrollBar().maximum()}"
        )
    if book_list.item(0).text() != book_name:
        raise SystemExit("pending Book text changed")
    for text in ("返回 Folder 统一归属", "确认选择", "取消并退出"):
        button = next(button for button in dialog.findChildren(QtWidgets.QPushButton) if button.text() == text)
        assert_visible(scroll, button)
    dialog.reject()

QtCore.QTimer.singleShot(100, inspect)
show_attribution_book_picker(
    AttributionBookSelectionRequest(
        folder_label=folder,
        source_filename=source,
        choices=(("book-1", book_name), ("book-2", "Book 2")),
        allow_return_to_folder=True,
    )
)
'''
        completed = _run_qt_script(script, scale_factor="1.5")
        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)

    def test_pending_book_picker_does_not_add_false_scroll_after_context_wraps(self):
        script = r'''
from PySide6 import QtCore, QtWidgets
from spectrum_organizer.ui.dialog_port import AttributionBookSelectionRequest, show_attribution_book_picker

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

def inspect():
    dialog = next(
        widget for widget in app.topLevelWidgets()
        if widget.isVisible() and widget.objectName() == "organizer_dialog"
    )
    scroll = dialog.findChild(QtWidgets.QScrollArea, "attribution_picker_scroll")
    context = dialog.findChild(QtWidgets.QLabel, "attribution_picker_context")
    app.processEvents()
    required_height = context.heightForWidth(context.width())
    if context.height() < required_height:
        raise SystemExit(f"context clipped: {context.height()} < {required_height}")
    if scroll.verticalScrollBar().maximum() != 0:
        raise SystemExit(
            f"wrapped ordinary picker scrolls: maximum={scroll.verticalScrollBar().maximum()}, "
            f"dialog={dialog.width()}x{dialog.height()}"
        )
    dialog.reject()

QtCore.QTimer.singleShot(100, inspect)
show_attribution_book_picker(
    AttributionBookSelectionRequest(
        folder_label=" / ".join(f"Folder segment {index}" for index in range(4)),
        source_filename="source_" + "very_long_" * 10 + ".opj",
        choices=(("book-1", "Book 1"), ("book-2", "Book 2")),
        allow_return_to_folder=True,
    )
)
'''
        for scale in ("1", "1.25"):
            with self.subTest(scale=scale):
                completed = _run_qt_script(script, scale_factor=scale)
                self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)

    def test_pending_book_picker_starts_in_list_and_keyboard_selects(self):
        script = r'''
from PySide6 import QtCore, QtTest, QtWidgets
from spectrum_organizer.ui.dialog_port import (
    AttributionBookSelectionRequest,
    show_attribution_book_picker,
)

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

def choose_with_keyboard():
    dialog = next(
        widget for widget in app.topLevelWidgets()
        if widget.isVisible() and widget.objectName() == "organizer_dialog"
    )
    book_list = dialog.findChild(QtWidgets.QListWidget, "attribution_pending_book_list")
    close_button = dialog.findChild(QtWidgets.QPushButton, "dialog_close_button")
    if dialog.focusWidget() is not book_list:
        raise SystemExit(
            f"initial focus is {getattr(dialog.focusWidget(), 'objectName', lambda: '')()}"
        )
    if close_button.focusPolicy() != QtCore.Qt.FocusPolicy.NoFocus:
        raise SystemExit(f"close button focus policy is {close_button.focusPolicy()}")
    QtTest.QTest.keyClick(book_list, QtCore.Qt.Key.Key_Tab, QtCore.Qt.KeyboardModifier.ShiftModifier)
    if dialog.focusWidget() is close_button:
        raise SystemExit("reverse focus traversal reached close button")
    book_list.setFocus()
    QtTest.QTest.keyClick(book_list, QtCore.Qt.Key.Key_Down)
    QtTest.QTest.keyClick(book_list, QtCore.Qt.Key.Key_Return)

QtCore.QTimer.singleShot(100, choose_with_keyboard)
response = show_attribution_book_picker(
    AttributionBookSelectionRequest(
        folder_label="Folder A",
        source_filename="source.opj",
        choices=(("book-1", "Book 1"), ("book-2", "Book 2")),
        allow_return_to_folder=True,
    )
)
if response.action != "select_book" or response.book_key != "book-1":
    raise SystemExit(f"keyboard selection failed: {response}")
'''
        completed = _run_qt_script(script)
        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)

    def test_compact_pending_book_keyboard_selection_is_visible_before_confirmation(self):
        script = r'''
from PySide6 import QtCore, QtTest, QtWidgets
from spectrum_organizer.ui.dialog_port import (
    AttributionBookSelectionRequest,
    show_attribution_book_picker,
)

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
source = "S" * 300 + ".opj"
folder = "F" * 300

def choose_with_keyboard():
    dialog = next(
        widget for widget in app.topLevelWidgets()
        if widget.isVisible() and widget.objectName() == "organizer_dialog"
    )
    dialog.setFixedSize(396, 296)
    scroll = dialog.findChild(QtWidgets.QScrollArea, "attribution_picker_scroll")
    book_list = dialog.findChild(QtWidgets.QListWidget, "attribution_pending_book_list")
    if dialog.focusWidget() is not book_list:
        raise SystemExit("pending Book list did not receive initial focus")
    QtTest.QTest.keyClick(book_list, QtCore.Qt.Key.Key_Down)
    app.processEvents()
    current = book_list.currentItem()
    if current is None:
        raise SystemExit("keyboard did not select a pending Book")
    item_rect = book_list.visualItemRect(current)
    top_left = book_list.viewport().mapTo(scroll.viewport(), item_rect.topLeft())
    bottom = top_left.y() + item_rect.height()
    if top_left.y() < 0 or bottom > scroll.viewport().height():
        raise SystemExit(
            f"keyboard-selected Book is offscreen: top={top_left.y()} bottom={bottom} "
            f"viewport={scroll.viewport().height()}"
        )
    QtTest.QTest.keyClick(book_list, QtCore.Qt.Key.Key_Return)

QtCore.QTimer.singleShot(100, choose_with_keyboard)
response = show_attribution_book_picker(
    AttributionBookSelectionRequest(
        folder_label=folder,
        source_filename=source,
        choices=(("book-1", "Book 1"), ("book-2", "Book 2")),
        allow_return_to_folder=True,
    )
)
if response.action != "select_book" or response.book_key != "book-1":
    raise SystemExit(f"keyboard selection failed: {response}")
'''
        completed = _run_qt_script(script, scale_factor="1.5")
        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)

    def test_pending_book_picker_does_not_scroll_short_content_on_ordinary_screen(self):
        script = r'''
from PySide6 import QtCore, QtWidgets
from spectrum_organizer.ui.dialog_port import (
    AttributionBookSelectionRequest,
    show_attribution_book_picker,
)

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

def inspect():
    dialog = next(
        widget for widget in app.topLevelWidgets()
        if widget.isVisible() and widget.objectName() == "organizer_dialog"
    )
    scroll = dialog.findChild(QtWidgets.QScrollArea, "attribution_picker_scroll")
    app.processEvents()
    maximum = scroll.verticalScrollBar().maximum()
    if maximum != 0:
        raise SystemExit(
            f"ordinary short picker scrolls: maximum={maximum}, "
            f"dialog={dialog.width()}x{dialog.height()}"
        )
    dialog.reject()

QtCore.QTimer.singleShot(100, inspect)
show_attribution_book_picker(
    AttributionBookSelectionRequest(
        folder_label="Folder A",
        source_filename="source.opj",
        choices=(("book-1", "Book 1"), ("book-2", "Book 2")),
        allow_return_to_folder=True,
    )
)
'''
        completed = _run_qt_script(script)
        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)

    def test_sample_type_popup_hides_placeholder_and_current_type(self):
        def interact(dialog):
            _, _, sample_type, _ = self._controls(dialog)
            sample_type.setCurrentIndex(sample_type.findData("solution"))
            self.assertTrue(sample_type.view().isRowHidden(0))
            self.assertTrue(sample_type.view().isRowHidden(sample_type.findData("solution")))
            self.assertFalse(sample_type.view().isRowHidden(sample_type.findData("solid")))
            self.assertFalse(sample_type.view().isRowHidden(sample_type.findData("doped")))
            folder_mode = self._button(dialog, "整个 Folder")
            mode_button = self._button(dialog, "逐 Book")
            self.assertTrue(folder_mode.isVisible())
            self.assertTrue(mode_button.isVisible())
            self.assertTrue(folder_mode.isChecked())
            self.assertEqual(folder_mode.width(), mode_button.width())
            self._button(dialog, "取消并退出").click()

        response = self._run_form(
            AttributionDialogRequest(
                "Folder A",
                "source.opj",
                ("Book1", "Book2"),
                allow_split_folder=True,
            ),
            interact,
        )
        self.assertEqual(AttributionDialogResponse(action="cancel"), response)

    def test_hidden_sample_type_rows_are_unreachable_from_closed_combo_keyboard(self):
        def interact(dialog):
            _, _, sample_type, _ = self._controls(dialog)
            solution_index = sample_type.findData("solution")
            sample_type.setCurrentIndex(solution_index)
            sample_type.setFocus()

            model = sample_type.model()
            for hidden_index in (0, solution_index):
                flags = model.flags(model.index(hidden_index, 0))
                self.assertFalse(flags & self.QtCore.Qt.ItemFlag.ItemIsEnabled)
                self.assertFalse(flags & self.QtCore.Qt.ItemFlag.ItemIsSelectable)

            self.QtTest.QTest.keyClick(sample_type, self.QtCore.Qt.Key.Key_Up)
            self.app.processEvents()
            self.assertEqual("solution", sample_type.currentData())
            self._button(dialog, "取消并退出").click()

        response = self._run_form(
            AttributionDialogRequest(
                "Folder A",
                "source.opj",
                ("Book1", "Book2"),
                allow_split_folder=True,
            ),
            interact,
        )
        self.assertEqual(AttributionDialogResponse(action="cancel"), response)

    def test_sample_type_choices_and_fields_stay_consistent_across_continuous_switches(self):
        expected_fields = {
            "solution": {"样品名称", "溶剂或状态", "浓度", "温度"},
            "solid": {"样品名称", "固体状态", "测量环境", "温度"},
            "doped": {"样品名称", "主体成分", "浓度", "固体状态", "测量环境", "温度"},
        }

        def interact(dialog):
            labels, fields, sample_type, _ = self._controls(dialog)
            for mode in ("solution", "solid", "doped", "solution"):
                sample_type.setCurrentIndex(sample_type.findData(mode))
                if mode == "solution":
                    fields["浓度"].clear()
                visible = {
                    name
                    for name, label in labels.items()
                    if name not in {"样品类型", "归属方式"} and label.isVisible()
                }
                self.assertEqual(expected_fields[mode], visible)
                self.assertTrue(sample_type.view().isRowHidden(0))
                self.assertTrue(sample_type.view().isRowHidden(sample_type.findData(mode)))
                for other in expected_fields.keys() - {mode}:
                    self.assertFalse(sample_type.view().isRowHidden(sample_type.findData(other)))
            self._button(dialog, "取消并退出").click()

        response = self._run_form(
            AttributionDialogRequest(
                "Folder A",
                "source.opj",
                ("Book1", "Book2"),
                allow_split_folder=True,
            ),
            interact,
        )
        self.assertEqual(AttributionDialogResponse(action="cancel"), response)

    def test_repeated_sample_type_switches_settle_without_scroll_on_normal_screen(self):
        class ScreenProxy:
            def availableGeometry(self):
                return self.QtCore.QRect(0, 0, 1180, 820)

        ScreenProxy.QtCore = self.QtCore

        class SizedDialog(self.QtWidgets.QDialog):
            def screen(self):
                return ScreenProxy()

        class QtWidgetsProxy:
            QDialog = SizedDialog

            def __getattr__(self, name):
                return getattr(self.QtWidgets, name)

        QtWidgetsProxy.QtWidgets = self.QtWidgets

        def interact(dialog):
            _, _, sample_type, _ = self._controls(dialog)
            scroll = dialog.findChild(
                self.QtWidgets.QScrollArea,
                "attribution_body_scroll",
            )
            observed = []
            for _round in range(4):
                for mode in ("solution", "doped", "solid", "doped"):
                    sample_type.setCurrentIndex(sample_type.findData(mode))
                    self.app.processEvents()
                    self.app.processEvents()
                    observed.append(
                        (
                            mode,
                            dialog.height(),
                            scroll.verticalScrollBar().maximum(),
                        )
                    )
            self.assertTrue(
                all(maximum == 0 for _mode, _height, maximum in observed),
                observed,
            )
            self._button(dialog, "取消并退出").click()

        with mock.patch.object(
            dialog_port_module,
            "_load_qt_modules",
            return_value=(QtWidgetsProxy(), self.QtCore),
        ):
            response = self._run_form(
                AttributionDialogRequest(
                    "TMeFL_EID_mTHF",
                    "20241209_MFL_2DPho.opj",
                    tuple(
                        (
                            "Pho390_10_10",
                            "Pho360_10_10",
                            "300",
                            "310",
                            "320",
                            "330",
                            "340",
                            "350",
                            "360",
                            "370",
                            "380",
                            "390",
                            "400",
                            "410",
                            "420",
                            "430",
                            "440",
                            "450",
                            "Pho344_10_10",
                            "Pho372_10_10",
                            "PhoEx540_10_10",
                            "PhoEx550_10_10",
                        )
                    ),
                    allow_split_folder=True,
                ),
                interact,
            )
        self.assertEqual(AttributionDialogResponse(action="cancel"), response)

    def test_solid_and_doped_switch_preserves_shared_state_only(self):
        def interact(dialog):
            _, fields, sample_type, _ = self._controls(dialog)
            sample_type.setCurrentIndex(sample_type.findData("solid"))
            fields["样品名称"].setText("NDI")
            fields["固体状态"].setText("Film")
            fields["温度"].setText("77 K")

            sample_type.setCurrentIndex(sample_type.findData("doped"))
            self.assertEqual("NDI", fields["样品名称"].text())
            self.assertEqual("Film", fields["固体状态"].text())
            self.assertEqual("77 K", fields["温度"].text())
            fields["主体成分"].setText("PMMA")
            fields["浓度"].setText("1")

            sample_type.setCurrentIndex(sample_type.findData("solid"))
            self.assertEqual("NDI", fields["样品名称"].text())
            self.assertEqual("Film", fields["固体状态"].text())
            self.assertEqual("77 K", fields["温度"].text())
            self.assertEqual("", fields["主体成分"].text())
            self.assertEqual("", fields["浓度"].text())
            self._button(dialog, "取消并退出").click()

        response = self._run_form(
            AttributionDialogRequest("Folder A", "source.opj", ("Book1",)),
            interact,
        )
        self.assertEqual(AttributionDialogResponse(action="cancel"), response)

    def test_confirmed_type_switch_does_not_restore_cleared_doped_unit(self):
        def interact(dialog):
            _, fields, sample_type, unit = self._controls(dialog)
            self.assertEqual("doped", sample_type.currentData())
            self.assertEqual("wt%", unit.currentData())

            with mock.patch.object(
                dialog_port_module,
                "show_styled_dialog",
            ) as confirmation:
                sample_type.setCurrentIndex(sample_type.findData("solid"))
                self.assertEqual(-1, unit.currentIndex())
                sample_type.setCurrentIndex(sample_type.findData("doped"))
                confirmation.assert_not_called()

            self.assertEqual(-1, unit.currentIndex())
            self.assertEqual("", fields["浓度"].text())
            self._button(dialog, "取消并退出").click()

        response = self._run_form(
            AttributionDialogRequest(
                "Folder A",
                "source.opj",
                ("Book1",),
                prefill={
                    "sample_type": "doped",
                    "sample": "NDI",
                    "host": "PMMA",
                    "concentration": "1",
                    "concentration_unit": "wt%",
                    "state": "Film",
                    "temperature": "298 K",
                },
            ),
            interact,
        )
        self.assertEqual(AttributionDialogResponse(action="cancel"), response)

    def test_type_switch_recomputes_typed_concentration_without_overwriting_initial_value(self):
        def interact(dialog):
            _, fields, sample_type, unit = self._controls(dialog)
            self.assertEqual("solution", sample_type.currentData())
            self.assertEqual("9×10^-5", fields["浓度"].text())

            sample_type.setCurrentIndex(sample_type.findData("solid"))
            self.assertEqual("", fields["浓度"].text())
            sample_type.setCurrentIndex(sample_type.findData("solution"))
            self.assertEqual("1×10^-5", fields["浓度"].text())

            sample_type.setCurrentIndex(sample_type.findData("doped"))
            self.assertEqual("2", fields["浓度"].text())
            self.assertEqual("mol%", unit.currentData())
            sample_type.setCurrentIndex(sample_type.findData("solid"))
            sample_type.setCurrentIndex(sample_type.findData("doped"))
            self.assertEqual("2", fields["浓度"].text())
            self.assertEqual("mol%", unit.currentData())
            self._button(dialog, "取消并退出").click()

        response = self._run_form(
            AttributionDialogRequest(
                "Folder A",
                "source.opj",
                ("Book1",),
                prefill={
                    "sample_type": "solution",
                    "concentration": "9×10^-5",
                    "solution_concentration": "1×10^-5",
                    "doped_concentration": "2",
                    "doped_concentration_unit": "mol%",
                },
            ),
            interact,
        )
        self.assertEqual(AttributionDialogResponse(action="cancel"), response)

    def test_tall_single_column_blank_form_refits_after_sample_type_selection(self):
        class ScreenProxy:
            def availableGeometry(self):
                return self.QtCore.QRect(0, 0, 1180, 820)

        ScreenProxy.QtCore = self.QtCore

        class SizedDialog(self.QtWidgets.QDialog):
            def screen(self):
                return ScreenProxy()

        class QtWidgetsProxy:
            QDialog = SizedDialog

            def __getattr__(self, name):
                return getattr(self.QtWidgets, name)

        QtWidgetsProxy.QtWidgets = self.QtWidgets

        def interact(dialog):
            _, _, sample_type, _ = self._controls(dialog)
            scroll = dialog.findChild(
                self.QtWidgets.QScrollArea,
                "attribution_body_scroll",
            )
            self.app.processEvents()
            unselected_height = dialog.height()
            sample_type.setCurrentIndex(sample_type.findData("doped"))
            self.app.processEvents()
            self.assertGreater(dialog.height(), unselected_height)
            self.assertEqual(0, scroll.verticalScrollBar().maximum())
            for action_text in ("确认", "取消并退出"):
                action = self._button(dialog, action_text)
                top_left = action.mapTo(
                    scroll.viewport(),
                    self.QtCore.QPoint(0, 0),
                )
                self.assertTrue(
                    scroll.viewport().rect().contains(
                        self.QtCore.QRect(top_left, action.size())
                    ),
                    action_text,
                )
            self._button(dialog, "取消并退出").click()

        with mock.patch.object(
            dialog_port_module,
            "_load_qt_modules",
            return_value=(QtWidgetsProxy(), self.QtCore),
        ):
            response = self._run_form(
                AttributionDialogRequest(
                    "Folder A",
                    "source.opj",
                    ("Book1", "Book2"),
                    allow_split_folder=True,
                ),
                interact,
            )
        self.assertEqual(AttributionDialogResponse(action="cancel"), response)

    def test_tall_single_column_refit_clamps_to_narrower_live_screen(self):
        class ScreenProxy:
            available_width = 1180

            def availableGeometry(self):
                return self.QtCore.QRect(0, 0, self.available_width, 820)

        ScreenProxy.QtCore = self.QtCore
        screen = ScreenProxy()

        class SizedDialog(self.QtWidgets.QDialog):
            def screen(self):
                return screen

        class QtWidgetsProxy:
            QDialog = SizedDialog

            def __getattr__(self, name):
                return getattr(self.QtWidgets, name)

        QtWidgetsProxy.QtWidgets = self.QtWidgets

        def interact(dialog):
            _, _, sample_type, _ = self._controls(dialog)
            screen.available_width = 500
            dialog.move(0, 0)
            sample_type.setCurrentIndex(sample_type.findData("doped"))
            self.app.processEvents()
            available = screen.availableGeometry()
            self.assertLessEqual(dialog.width(), available.width())
            self.assertLessEqual(dialog.height(), available.height())
            self._button(dialog, "取消并退出").click()

        with mock.patch.object(
            dialog_port_module,
            "_load_qt_modules",
            return_value=(QtWidgetsProxy(), self.QtCore),
        ):
            response = self._run_form(
                AttributionDialogRequest(
                    "Folder A",
                    "source.opj",
                    ("Book1", "Book2"),
                    allow_split_folder=True,
                ),
                interact,
            )
        self.assertEqual(AttributionDialogResponse(action="cancel"), response)

    def test_two_column_form_collapses_after_move_to_low_narrow_screen(self):
        class ScreenProxy:
            available_width = 1180
            available_height = 600

            def availableGeometry(self):
                return self.QtCore.QRect(
                    0,
                    0,
                    self.available_width,
                    self.available_height,
                )

        ScreenProxy.QtCore = self.QtCore
        screen = ScreenProxy()

        class SizedDialog(self.QtWidgets.QDialog):
            def screen(self):
                return screen

        class QtWidgetsProxy:
            QDialog = SizedDialog

            def __getattr__(self, name):
                return getattr(self.QtWidgets, name)

        QtWidgetsProxy.QtWidgets = self.QtWidgets

        def interact(dialog):
            _, _, sample_type, _ = self._controls(dialog)
            screen.available_width = 420
            screen.available_height = 320
            dialog.move(0, 0)
            sample_type.setCurrentIndex(sample_type.findData("doped"))
            self.app.processEvents()
            scroll = dialog.findChild(
                self.QtWidgets.QScrollArea,
                "attribution_body_scroll",
            )
            available = screen.availableGeometry()
            self.assertLessEqual(dialog.width(), available.width())
            self.assertLessEqual(dialog.height(), available.height())
            self.assertLessEqual(
                scroll.widget().width(),
                scroll.viewport().width(),
                (scroll.widget().width(), scroll.viewport().width()),
            )
            self.assertEqual(0, scroll.horizontalScrollBar().maximum())
            self._button(dialog, "取消并退出").click()

        with mock.patch.object(
            dialog_port_module,
            "_load_qt_modules",
            return_value=(QtWidgetsProxy(), self.QtCore),
        ):
            response = self._run_form(
                AttributionDialogRequest(
                    "Folder A",
                    "source.opj",
                    ("Book1", "Book2"),
                    allow_split_folder=True,
                    allow_return_to_book_picker=True,
                ),
                interact,
            )
        self.assertEqual(AttributionDialogResponse(action="cancel"), response)

    def test_doped_prefill_rejects_solution_only_molarity_unit(self):
        def interact(dialog):
            _, fields, sample_type, unit = self._controls(dialog)
            self.assertEqual("doped", sample_type.currentData())
            self.assertEqual(-1, unit.currentIndex())
            self.assertEqual("", unit.currentText())
            self.assertEqual("", fields["浓度"].text())
            self.assertEqual(-1, unit.findData("M"))
            self._button(dialog, "取消并退出").click()

        response = self._run_form(
            AttributionDialogRequest(
                "Folder A",
                "source.opj",
                ("Book1",),
                prefill={
                    "sample_type": "doped",
                    "concentration": "1×10^-4",
                    "concentration_unit": "M",
                },
            ),
            interact,
        )
        self.assertEqual(AttributionDialogResponse(action="cancel"), response)

    def test_doped_unit_model_excludes_solution_molarity_from_keyboard_typeahead(self):
        from PySide6 import QtCore, QtTest

        def interact(dialog):
            _, _, sample_type, unit = self._controls(dialog)
            sample_type.setCurrentIndex(sample_type.findData("doped"))
            self.assertEqual(-1, unit.findData("M"))
            unit.setFocus()
            QtTest.QTest.keyClick(unit, QtCore.Qt.Key.Key_M)
            self.app.processEvents()
            self.assertNotEqual("M", unit.currentData())
            self._button(dialog, "取消并退出").click()

        response = self._run_form(
            AttributionDialogRequest("Folder A", "source.opj", ("Book1",)),
            interact,
        )
        self.assertEqual(AttributionDialogResponse(action="cancel"), response)

    def test_custom_title_drag_keeps_modal_dialog_inside_available_screen(self):
        class PointerEvent:
            def __init__(self, point, *, pressed):
                self._point = point
                self._pressed = pressed

            def button(self):
                return self.QtCore.Qt.MouseButton.LeftButton

            def buttons(self):
                return (
                    self.QtCore.Qt.MouseButton.LeftButton
                    if self._pressed
                    else self.QtCore.Qt.MouseButton.NoButton
                )

            def globalPosition(self):
                return self.QtCore.QPointF(self._point)

            def accept(self):
                pass

        PointerEvent.QtCore = self.QtCore
        dialog = self.QtWidgets.QDialog()
        dialog.resize(420, 300)
        header = self.QtWidgets.QFrame(dialog)
        dialog_port_module._enable_title_bar_drag(header, dialog, self.QtCore)
        dialog.show()
        self.app.processEvents()
        start = dialog.frameGeometry().topLeft() + self.QtCore.QPoint(30, 20)
        header.mousePressEvent(PointerEvent(start, pressed=True))
        header.mouseMoveEvent(PointerEvent(self.QtCore.QPoint(-10000, -10000), pressed=True))
        self.app.processEvents()
        available = dialog.screen().availableGeometry()
        self.assertTrue(available.contains(dialog.frameGeometry()))
        dialog.close()

    def test_custom_title_drag_does_not_lock_dynamic_dialog_size(self):
        class PointerEvent:
            def __init__(self, point, *, pressed):
                self._point = point
                self._pressed = pressed

            def button(self):
                return self.QtCore.Qt.MouseButton.LeftButton

            def buttons(self):
                return (
                    self.QtCore.Qt.MouseButton.LeftButton
                    if self._pressed
                    else self.QtCore.Qt.MouseButton.NoButton
                )

            def globalPosition(self):
                return self.QtCore.QPointF(self._point)

            def accept(self):
                pass

        PointerEvent.QtCore = self.QtCore
        dialog = self.QtWidgets.QDialog()
        dialog.resize(420, 300)
        header = self.QtWidgets.QFrame(dialog)
        dialog_port_module._enable_title_bar_drag(header, dialog, self.QtCore)
        dialog.show()
        self.app.processEvents()
        original_minimum = dialog.minimumSize()
        original_maximum = dialog.maximumSize()
        start = dialog.frameGeometry().topLeft() + self.QtCore.QPoint(30, 20)

        header.mousePressEvent(PointerEvent(start, pressed=True))
        header.mouseMoveEvent(
            PointerEvent(start + self.QtCore.QPoint(20, 20), pressed=True)
        )
        header.mouseReleaseEvent(
            PointerEvent(start + self.QtCore.QPoint(20, 20), pressed=False)
        )
        dialog.resize(420, 240)
        self.app.processEvents()

        self.assertEqual(original_minimum, dialog.minimumSize())
        self.assertEqual(original_maximum, dialog.maximumSize())
        self.assertEqual(240, dialog.height())
        dialog.close()

    def test_first_custom_title_drag_preserves_final_layout_size(self):
        class PointerEvent:
            def __init__(self, point, *, pressed):
                self._point = point
                self._pressed = pressed

            def button(self):
                return self.QtCore.Qt.MouseButton.LeftButton

            def buttons(self):
                return (
                    self.QtCore.Qt.MouseButton.LeftButton
                    if self._pressed
                    else self.QtCore.Qt.MouseButton.NoButton
                )

            def globalPosition(self):
                return self.QtCore.QPointF(self._point)

            def accept(self):
                pass

        PointerEvent.QtCore = self.QtCore
        dialog = self.QtWidgets.QDialog()
        dialog.resize(240, 180)
        header = self.QtWidgets.QFrame(dialog)
        dialog_port_module._enable_title_bar_drag(header, dialog, self.QtCore)
        dialog.resize(420, 300)
        dialog.show()
        self.app.processEvents()
        final_size = dialog.size()
        start = dialog.frameGeometry().topLeft() + self.QtCore.QPoint(30, 20)
        header.mousePressEvent(PointerEvent(start, pressed=True))
        header.mouseMoveEvent(PointerEvent(start + self.QtCore.QPoint(1, 1), pressed=True))
        self.app.processEvents()
        self.assertEqual(final_size, dialog.size())
        dialog.close()

    def test_custom_title_drag_uses_screen_under_pointer_when_crossing_monitors(self):
        class PointerEvent:
            def __init__(self, point, *, pressed):
                self._point = point
                self._pressed = pressed

            def button(self):
                return self.QtCore.Qt.MouseButton.LeftButton

            def buttons(self):
                return (
                    self.QtCore.Qt.MouseButton.LeftButton
                    if self._pressed
                    else self.QtCore.Qt.MouseButton.NoButton
                )

            def globalPosition(self):
                return self.QtCore.QPointF(self._point)

            def accept(self):
                pass

        class DestinationScreen:
            def availableGeometry(self):
                return self.QtCore.QRect(1000, 0, 800, 600)

        class FakeGuiApplication:
            seen_point = None

            @classmethod
            def screenAt(cls, point):
                cls.seen_point = point
                return destination_screen

        class FakeQtGui:
            QGuiApplication = FakeGuiApplication

        PointerEvent.QtCore = self.QtCore
        DestinationScreen.QtCore = self.QtCore
        destination_screen = DestinationScreen()
        dialog = self.QtWidgets.QDialog()
        dialog.resize(420, 300)
        header = self.QtWidgets.QFrame(dialog)
        dialog_port_module._enable_title_bar_drag(
            header,
            dialog,
            self.QtCore,
            FakeQtGui,
        )
        dialog.show()
        self.app.processEvents()
        start = dialog.frameGeometry().topLeft() + self.QtCore.QPoint(30, 20)
        pointer = self.QtCore.QPoint(1250, 100)
        header.mousePressEvent(PointerEvent(start, pressed=True))
        header.mouseMoveEvent(PointerEvent(pointer, pressed=True))
        self.app.processEvents()
        self.assertEqual(pointer, FakeGuiApplication.seen_point)
        self.assertTrue(destination_screen.availableGeometry().contains(dialog.frameGeometry()))
        dialog.close()

    def test_custom_title_drag_fits_dialog_to_narrow_destination_screen(self):
        class PointerEvent:
            def __init__(self, point, *, pressed):
                self._point = point
                self._pressed = pressed

            def button(self):
                return self.QtCore.Qt.MouseButton.LeftButton

            def buttons(self):
                return (
                    self.QtCore.Qt.MouseButton.LeftButton
                    if self._pressed
                    else self.QtCore.Qt.MouseButton.NoButton
                )

            def globalPosition(self):
                return self.QtCore.QPointF(self._point)

            def accept(self):
                pass

        class DestinationScreen:
            def availableGeometry(self):
                return self.QtCore.QRect(1000, 0, 360, 260)

        class FakeGuiApplication:
            @classmethod
            def screenAt(cls, _point):
                return destination_screen

        class FakeQtGui:
            QGuiApplication = FakeGuiApplication

        PointerEvent.QtCore = self.QtCore
        DestinationScreen.QtCore = self.QtCore
        destination_screen = DestinationScreen()
        dialog = self.QtWidgets.QDialog()
        dialog.setFixedSize(420, 300)
        header = self.QtWidgets.QFrame(dialog)
        dialog_port_module._enable_title_bar_drag(header, dialog, self.QtCore, FakeQtGui)
        dialog.show()
        self.app.processEvents()
        start = dialog.frameGeometry().topLeft() + self.QtCore.QPoint(30, 20)
        header.mousePressEvent(PointerEvent(start, pressed=True))
        header.mouseMoveEvent(PointerEvent(self.QtCore.QPoint(1180, 80), pressed=True))
        self.app.processEvents()
        self.assertTrue(destination_screen.availableGeometry().contains(dialog.frameGeometry()))
        dialog.close()

    def test_sample_type_switch_keeps_top_level_geometry_stable(self):
        class ScreenProxy:
            def availableGeometry(self):
                return self.QtCore.QRect(0, 0, 420, 320)

        ScreenProxy.QtCore = self.QtCore

        def interact(dialog):
            _labels, _fields, sample_type, _unit = self._controls(dialog)
            initial_size = dialog.size()
            dialog.screen = lambda: ScreenProxy()
            sample_type.setCurrentIndex(sample_type.findData("doped"))
            self.app.processEvents()
            self.assertEqual(initial_size, dialog.size())
            self._button(dialog, "取消并退出").click()

        self._run_form(
            AttributionDialogRequest("Folder A", "source.opj", ("Book1",)),
            interact,
        )

    def test_attribution_body_scrolls_only_when_content_exceeds_the_viewport(self):
        long_books = tuple(
            f"Book {index:02d} - delayed spectrum with a deliberately long display name"
            for index in range(1, 41)
        )

        def interact(dialog):
            scroll = dialog.findChild(self.QtWidgets.QScrollArea, "attribution_body_scroll")
            included = dialog.findChild(self.QtWidgets.QLabel, "attribution_included_books")
            self.assertIsNotNone(scroll)
            self.assertIsNotNone(included)
            self.app.processEvents()
            required_height = included.heightForWidth(included.width())
            self.assertGreater(required_height, 0)
            self.assertGreaterEqual(included.height(), required_height)
            self.assertGreater(scroll.verticalScrollBar().maximum(), 0)
            scroll.ensureWidgetVisible(included)
            self.app.processEvents()
            self._button(dialog, "取消并退出").click()

        self._run_form(
            AttributionDialogRequest("Folder A", "source.opj", long_books),
            interact,
        )

        def inspect_short(dialog):
            scroll = dialog.findChild(self.QtWidgets.QScrollArea, "attribution_body_scroll")
            self.assertIsNotNone(scroll)
            self.app.processEvents()
            self.assertEqual(0, scroll.verticalScrollBar().maximum())
            self._button(dialog, "取消并退出").click()

        self._run_form(
            AttributionDialogRequest("Folder A", "source.opj", ("Book1",)),
            inspect_short,
        )

    def test_attribution_dialog_contains_unbroken_book_text_and_actions_horizontally(self):
        script = r'''
from PySide6 import QtCore, QtWidgets
from spectrum_organizer.ui.dialog_port import AttributionDialogRequest, show_attribution_dialog

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
book_name = "B" * 60
expected_text = f"包含：{book_name}"

def assert_visible(scroll, widget):
    scroll.ensureWidgetVisible(widget)
    app.processEvents()
    top_left = widget.mapTo(scroll.viewport(), QtCore.QPoint(0, 0))
    if top_left.x() < 0 or top_left.x() + widget.width() > scroll.viewport().width():
        raise SystemExit(
            f"{widget.text()} horizontally unreachable: x={top_left.x()} "
            f"right={top_left.x() + widget.width()} viewport={scroll.viewport().width()}"
        )
    if top_left.y() < 0 or top_left.y() + widget.height() > scroll.viewport().height():
        raise SystemExit(
            f"{widget.text()} vertically unreachable: y={top_left.y()} "
            f"bottom={top_left.y() + widget.height()} viewport={scroll.viewport().height()}"
        )

def inspect():
    dialog = next(
        widget for widget in app.topLevelWidgets()
        if widget.isVisible() and widget.objectName() == "organizer_dialog"
    )
    scroll = dialog.findChild(QtWidgets.QScrollArea, "attribution_body_scroll")
    included = dialog.findChild(QtWidgets.QLabel, "attribution_included_books")
    if scroll is None or included is None:
        raise SystemExit("attribution dialog does not expose body scroll ownership")
    app.processEvents()
    if included.text() != expected_text:
        raise SystemExit("included Book text changed")
    if scroll.horizontalScrollBar().maximum() != 0:
        raise SystemExit(f"attribution horizontal range={scroll.horizontalScrollBar().maximum()}")
    if scroll.widget().width() > scroll.viewport().width():
        raise SystemExit(
            f"attribution body wider than viewport: {scroll.widget().width()} > {scroll.viewport().width()}"
        )
    for text in ("确认", "取消并退出"):
        button = next(button for button in dialog.findChildren(QtWidgets.QPushButton) if button.text() == text)
        assert_visible(scroll, button)
    dialog.reject()

QtCore.QTimer.singleShot(100, inspect)
show_attribution_dialog(
    AttributionDialogRequest(
        target_label="Folder",
        source_filename="source.opj",
        book_display_names=(book_name,),
    )
)
'''
        completed = _run_qt_script(script, scale_factor="1.5")
        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)

    def test_validation_error_remains_reachable_inside_attribution_scroll_area(self):
        def interact(dialog):
            _, fields, sample_type, _ = self._controls(dialog)
            scroll = dialog.findChild(self.QtWidgets.QScrollArea, "attribution_body_scroll")
            error = dialog.findChild(self.QtWidgets.QLabel, "dialog_error_text")
            self.assertIsNotNone(scroll)
            sample_type.setCurrentIndex(sample_type.findData("solid"))
            fields["温度"].setText("0")
            scroll.verticalScrollBar().setValue(0)
            fields["温度"].editingFinished.emit()
            self.app.processEvents()
            self.app.processEvents()
            self.assertTrue(error.text())
            top = error.mapTo(scroll.viewport(), self.QtCore.QPoint(0, 0)).y()
            self.assertGreaterEqual(top, 0)
            self.assertLessEqual(top + error.height(), scroll.viewport().height())
            self._button(dialog, "取消并退出").click()

        self._run_form(
            AttributionDialogRequest(
                "Folder A",
                "source.opj",
                tuple(f"Book {index:02d} with long metadata" for index in range(1, 41)),
            ),
            interact,
        )

    def test_attribution_dialog_fits_compact_screen_at_150_percent_scale(self):
        script = r'''
import os
from PySide6 import QtCore, QtWidgets
from spectrum_organizer.ui.dialog_port import AttributionDialogRequest, show_attribution_dialog

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
observed = {}

def inspect():
    dialog = next(
        widget for widget in app.topLevelWidgets()
        if widget.isVisible() and widget.objectName() == "organizer_dialog"
    )
    available = dialog.screen().availableGeometry()
    sample_type = next(
        combo for combo in dialog.findChildren(QtWidgets.QComboBox)
        if combo.findData("solution") >= 0
    )
    sample_type.setCurrentIndex(sample_type.findData("solution"))
    app.processEvents()
    line_edits = dialog.findChildren(QtWidgets.QLineEdit)
    for index, line_edit in enumerate(line_edits):
        line_edit.setText("1" if index else "NDI")
    primary = dialog.findChild(QtWidgets.QPushButton, "dialog_button_primary")
    body_scroll = dialog.findChild(QtWidgets.QScrollArea, "attribution_body_scroll")
    if body_scroll is not None:
        body_scroll.ensureWidgetVisible(primary)
    app.processEvents()
    observed["primary_visible"] = primary.isVisible()
    observed["frame"] = dialog.frameGeometry()
    observed["available"] = available
    primary.click()

QtCore.QTimer.singleShot(0, inspect)
show_attribution_dialog(
    AttributionDialogRequest(
        "Folder A",
        "source.opj",
        ("Book1", "Book2"),
        allow_split_folder=True,
    )
)
frame = observed["frame"]
available = observed["available"]
if not available.contains(frame):
    raise SystemExit(
        f"dialog frame {frame.getRect()} exceeds available {available.getRect()} after resize"
    )
if not observed["primary_visible"]:
    raise SystemExit("primary action is not reachable on a compact scaled screen")
'''
        completed = _run_qt_script(script, scale_factor="1.5")
        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)

    def test_short_attribution_form_does_not_scroll_at_supported_scales(self):
        script = r'''
from PySide6 import QtCore, QtWidgets
from spectrum_organizer.ui.dialog_port import AttributionDialogRequest, show_attribution_dialog

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
observed = {}

def inspect():
    dialog = next(
        widget for widget in app.topLevelWidgets()
        if widget.isVisible() and widget.objectName() == "organizer_dialog"
    )
    scroll = dialog.findChild(QtWidgets.QScrollArea, "attribution_body_scroll")
    available = dialog.screen().availableGeometry()
    observed["dialog"] = (dialog.width(), dialog.height())
    observed["available"] = (available.width(), available.height())
    observed["scroll_maximum"] = scroll.verticalScrollBar().maximum()
    dialog.reject()

QtCore.QTimer.singleShot(0, inspect)
show_attribution_dialog(
    AttributionDialogRequest("Folder A", "source.opj", ("Book1",))
)
dialog_width, dialog_height = observed["dialog"]
available_width, available_height = observed["available"]
if dialog_width > available_width or dialog_height > available_height:
    raise SystemExit(
        f"dialog {dialog_width}x{dialog_height} exceeds available {available_width}x{available_height}"
    )
if observed["scroll_maximum"] != 0:
    raise SystemExit(f"short form scroll maximum is {observed['scroll_maximum']}")
'''
        for scale in ("1", "1.25", "1.5"):
            with self.subTest(scale=scale):
                completed = _run_qt_script(script, scale_factor=scale)
                self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)

    def test_each_real_attribution_form_avoids_unnecessary_scrolling(self):
        request_modes = {
            "folder": {"allow_split_folder": True},
            "book": {"allow_return_to_book_picker": True},
        }

        for request_mode, flags in request_modes.items():
            for sample_mode in ("solution", "solid", "doped"):
                with self.subTest(request_mode=request_mode, sample_mode=sample_mode):
                    def inspect(dialog):
                        scroll = dialog.findChild(
                            self.QtWidgets.QScrollArea,
                            "attribution_body_scroll",
                        )
                        self.assertIsNotNone(scroll)
                        self.app.processEvents()
                        self.assertEqual(0, scroll.verticalScrollBar().maximum())
                        self._button(dialog, "取消并退出").click()

                    response = self._run_form(
                        AttributionDialogRequest(
                            "Folder A",
                            "source.opj",
                            ("Book1",),
                            prefill={"sample_type": sample_mode},
                            **flags,
                        ),
                        inspect,
                    )
                    self.assertEqual(AttributionDialogResponse(action="cancel"), response)

    def test_acceptance_realistic_forms_fit_1180x820_at_125_and_150_percent(self):
        requests = {
            "folder_solution": (
                AttributionDialogRequest(
                    "AH0.1mM+ANH2/AH",
                    "20260113.OPJ",
                    ("365", "320", "465"),
                    prefill={"sample_type": "solution"},
                    allow_split_folder=True,
                ),
                "solution",
            ),
            "book_doped": (
                AttributionDialogRequest(
                    "AH0.1mM+ANH2/AH+ANH2 UV 5min / 365",
                    "20260113.OPJ",
                    ("365",),
                    prefill={"sample_type": "doped"},
                    allow_apply_to_remaining_folder=True,
                    allow_return_to_book_picker=True,
                ),
                "doped",
            ),
        }
        logical_sizes = {
            "125_percent": (944, 656),
            "150_percent": (787, 547),
        }

        for scale_name, (available_width, available_height) in logical_sizes.items():
            class ScreenProxy:
                def availableGeometry(self):
                    return self.QtCore.QRect(
                        0,
                        0,
                        available_width,
                        available_height,
                    )

            ScreenProxy.QtCore = self.QtCore

            class SizedDialog(self.QtWidgets.QDialog):
                def screen(self):
                    return ScreenProxy()

            class QtWidgetsProxy:
                QDialog = SizedDialog

                def __getattr__(self, name):
                    return getattr(self.QtWidgets, name)

            QtWidgetsProxy.QtWidgets = self.QtWidgets

            for request_name, (configured_request, selected_type) in requests.items():
                for initial_state in ("prefilled", "selected_after_open"):
                    request = (
                        configured_request
                        if initial_state == "prefilled"
                        else replace(configured_request, prefill={})
                    )
                    with self.subTest(
                        scale=scale_name,
                        request=request_name,
                        initial_state=initial_state,
                    ):
                        def inspect(dialog):
                            if initial_state == "selected_after_open":
                                sample_type = next(
                                    combo
                                    for combo in dialog.findChildren(self.QtWidgets.QComboBox)
                                    if combo.findData(selected_type) >= 0
                                )
                                sample_type.setCurrentIndex(
                                    sample_type.findData(selected_type)
                                )
                            scroll = dialog.findChild(
                                self.QtWidgets.QScrollArea,
                                "attribution_body_scroll",
                            )
                            self.app.processEvents()
                            self.assertLessEqual(dialog.width(), available_width)
                            self.assertLessEqual(dialog.height(), available_height)
                            self.assertEqual(0, scroll.verticalScrollBar().maximum())
                            visible_controls = [
                                *dialog.findChildren(self.QtWidgets.QLineEdit),
                                *dialog.findChildren(self.QtWidgets.QComboBox),
                                *(
                                    button
                                    for button in dialog.findChildren(self.QtWidgets.QPushButton)
                                    if button.isVisible() and button.text() != "×"
                                ),
                                *(
                                    checkbox
                                    for checkbox in dialog.findChildren(self.QtWidgets.QCheckBox)
                                    if checkbox.isVisible()
                                ),
                            ]
                            for control in visible_controls:
                                top_left = control.mapTo(
                                    scroll.viewport(),
                                    self.QtCore.QPoint(0, 0),
                                )
                                self.assertTrue(
                                    scroll.viewport().rect().contains(
                                        self.QtCore.QRect(top_left, control.size())
                                    ),
                                    (
                                        control.objectName(),
                                        control.text()
                                        if hasattr(control, "text")
                                        else control.currentText(),
                                    ),
                                )
                            self._button(dialog, "取消并退出").click()

                        with mock.patch.object(
                            dialog_port_module,
                            "_load_qt_modules",
                            return_value=(QtWidgetsProxy(), self.QtCore),
                        ):
                            response = self._run_form(request, inspect)
                        self.assertEqual(
                            AttributionDialogResponse(action="cancel"),
                            response,
                        )

    def test_validation_warning_refits_without_scroll_when_work_area_can_fit_it(self):
        class ScreenProxy:
            def availableGeometry(self):
                return self.QtCore.QRect(0, 0, 1180, 820)

        ScreenProxy.QtCore = self.QtCore

        class SizedDialog(self.QtWidgets.QDialog):
            def screen(self):
                return ScreenProxy()

        class QtWidgetsProxy:
            QDialog = SizedDialog

            def __getattr__(self, name):
                return getattr(self.QtWidgets, name)

        QtWidgetsProxy.QtWidgets = self.QtWidgets

        def interact(dialog):
            _labels, fields, sample_type, _unit = self._controls(dialog)
            scroll = dialog.findChild(
                self.QtWidgets.QScrollArea,
                "attribution_body_scroll",
            )
            sample_type.setCurrentIndex(sample_type.findData("solution"))
            fields["浓度"].clear()
            self._button(dialog, "确认").click()
            self.app.processEvents()
            self.app.processEvents()
            error = dialog.findChild(
                self.QtWidgets.QLabel,
                "dialog_error_text",
            )
            self.assertTrue(error.isVisible())
            self.assertEqual(0, scroll.verticalScrollBar().maximum())
            for text in ("确认", "取消并退出"):
                button = self._button(dialog, text)
                top_left = button.mapTo(
                    scroll.viewport(),
                    self.QtCore.QPoint(0, 0),
                )
                self.assertTrue(
                    scroll.viewport().rect().contains(
                        self.QtCore.QRect(top_left, button.size())
                    ),
                    text,
                )
            self._button(dialog, "取消并退出").click()

        with mock.patch.object(
            dialog_port_module,
            "_load_qt_modules",
            return_value=(QtWidgetsProxy(), self.QtCore),
        ):
            response = self._run_form(
                AttributionDialogRequest(
                    "PFL-Xi_77K",
                    "20251104_PFL-DiffBrand.opj",
                    ("Pho300_10_10", "Pho300_10_10_F340", "PhoEx597_10_10_F340"),
                    prefill={"sample_type": "solution"},
                    allow_split_folder=True,
                ),
                interact,
            )
        self.assertEqual(AttributionDialogResponse(action="cancel"), response)

    def test_compact_validation_keeps_the_focused_control_visible(self):
        if os.environ.get("ORIGINAUTO_NATIVE_COMPACT_CHILD") != "1":
            env = os.environ.copy()
            env["PYTHONPATH"] = str(SRC)
            env["QT_QPA_PLATFORM"] = "windows"
            env["ORIGINAUTO_NATIVE_COMPACT_CHILD"] = "1"
            completed = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    "-m",
                    "unittest",
                    (
                        "tests.test_dialog_port."
                        "QtAttributionDialogInteractionTests."
                        "test_compact_validation_keeps_the_focused_control_visible"
                    ),
                    "-v",
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
            return

        class ScreenProxy:
            def availableGeometry(self):
                return self.QtCore.QRect(0, 0, 420, 320)

        ScreenProxy.QtCore = self.QtCore

        def visible_in_scroll(widget, scroll):
            top = widget.mapTo(scroll.viewport(), self.QtCore.QPoint(0, 0)).y()
            return top >= 0 and top + widget.height() <= scroll.viewport().height()

        def scroll_bounds(widget, scroll):
            top = widget.mapTo(scroll.viewport(), self.QtCore.QPoint(0, 0)).y()
            return top, top + widget.height(), scroll.viewport().height()

        def interact(dialog):
            _labels, fields, sample_type, _unit = self._controls(dialog)
            scroll = dialog.findChild(self.QtWidgets.QScrollArea, "attribution_body_scroll")
            dialog.screen = lambda: ScreenProxy()
            sample_type.setCurrentIndex(sample_type.findData("solid"))
            fields["固体状态"].setText("Solid")
            fields["温度"].setText("298")
            dialog.findChild(
                self.QtWidgets.QPushButton,
                "oxygen_environment_air",
            ).click()
            dialog.resize(396, 296)
            self._button(dialog, "确认").click()
            self.app.processEvents()
            self.app.processEvents()
            self.assertIs(dialog.focusWidget(), fields["样品名称"])
            self.assertTrue(
                visible_in_scroll(fields["样品名称"], scroll),
                scroll_bounds(fields["样品名称"], scroll),
            )
            error = dialog.findChild(self.QtWidgets.QLabel, "dialog_error_text")
            self.assertTrue(
                visible_in_scroll(error, scroll),
                (
                    scroll_bounds(fields["样品名称"], scroll),
                    scroll_bounds(error, scroll),
                    scroll.verticalScrollBar().value(),
                    scroll.verticalScrollBar().maximum(),
                ),
            )
            dialog.reject()

        self._run_form(
            AttributionDialogRequest("Folder A", "source.opj", ("Book1",)),
            interact,
        )

    def test_long_form_keyboard_validation_reveals_initial_and_invalid_controls(self):
        script = r'''
from PySide6 import QtCore, QtTest, QtWidgets
from spectrum_organizer.ui.dialog_port import AttributionDialogRequest, show_attribution_dialog

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
observed = {}

def visible_in_scroll(widget, scroll):
    top = widget.mapTo(scroll.viewport(), QtCore.QPoint(0, 0)).y()
    return top >= 0 and top + widget.height() <= scroll.viewport().height()

def inspect():
    dialog = next(
        widget for widget in app.topLevelWidgets()
        if widget.isVisible() and widget.objectName() == "organizer_dialog"
    )
    scroll = dialog.findChild(QtWidgets.QScrollArea, "attribution_body_scroll")
    sample_type = next(
        combo for combo in dialog.findChildren(QtWidgets.QComboBox)
        if combo.findData("solution") >= 0
    )
    confirm = next(
        button for button in dialog.findChildren(QtWidgets.QPushButton)
        if button.text() == "确认"
    )
    error = dialog.findChild(QtWidgets.QLabel, "dialog_error_text")
    observed["initial_focus"] = dialog.focusWidget() is sample_type
    observed["initial_visible"] = visible_in_scroll(sample_type, scroll)
    confirm.setFocus()
    QtTest.QTest.keyClick(confirm, QtCore.Qt.Key.Key_Return)
    app.processEvents()
    observed["validation_focus"] = dialog.focusWidget() is sample_type
    observed["validation_visible"] = visible_in_scroll(sample_type, scroll)
    observed["error_visible"] = visible_in_scroll(error, scroll)
    observed["error"] = error.text()
    dialog.reject()

QtCore.QTimer.singleShot(0, inspect)
show_attribution_dialog(
    AttributionDialogRequest(
        "Folder A",
        "source.opj",
        tuple(
            f"Book {index:02d} - delayed spectrum with a deliberately long display name"
            for index in range(1, 41)
        ),
    )
)
if not observed["initial_focus"] or not observed["initial_visible"]:
    raise SystemExit(f"initial control is not focused and visible: {observed}")
if not observed["validation_focus"] or not observed["validation_visible"]:
    raise SystemExit(f"validation target is not focused and visible: {observed}")
if not observed["error_visible"]:
    raise SystemExit(f"validation error is not visible: {observed}")
if observed["error"] != "请先选择样品类型。":
    raise SystemExit(f"unexpected validation error: {observed}")
'''
        completed = _run_qt_script(script, scale_factor="1.5")
        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)

    def test_long_form_reveals_bottom_validation_after_type_switch(self):
        script = r'''
from PySide6 import QtCore, QtTest, QtWidgets
from spectrum_organizer.ui.dialog_port import AttributionDialogRequest, show_attribution_dialog

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
observed = {}

def visible_in_scroll(widget, scroll):
    top = widget.mapTo(scroll.viewport(), QtCore.QPoint(0, 0)).y()
    return top >= 0 and top + widget.height() <= scroll.viewport().height()

def inspect():
    dialog = next(
        widget for widget in app.topLevelWidgets()
        if widget.isVisible() and widget.objectName() == "organizer_dialog"
    )
    scroll = dialog.findChild(QtWidgets.QScrollArea, "attribution_body_scroll")
    error = dialog.findChild(QtWidgets.QLabel, "dialog_error_text")
    form = dialog.findChild(QtWidgets.QGridLayout, "attribution_form_layout")
    fields = {}
    for row in range(form.rowCount()):
        label_item = form.itemAtPosition(row, 0)
        field_item = form.itemAtPosition(row, 1)
        if label_item is not None and field_item is not None:
            fields[label_item.widget().text()] = field_item.widget()
    sample_type = fields["样品类型"]
    sample_type.setCurrentIndex(sample_type.findData("solid"))
    fields["样品名称"].setText("MFL")
    fields["固体状态"].setText("Solid")
    dialog.findChild(QtWidgets.QPushButton, "oxygen_environment_air").click()
    fields["温度"].setText("0")
    confirm = next(
        button for button in dialog.findChildren(QtWidgets.QPushButton)
        if button.text() == "确认"
    )
    confirm.click()
    QtTest.QTest.qWait(100)
    observed["focus"] = dialog.focusWidget() is fields["温度"]
    observed["focus_visible"] = visible_in_scroll(fields["温度"], scroll)
    observed["error_visible"] = visible_in_scroll(error, scroll)
    observed["error"] = error.text()
    dialog.reject()

QtCore.QTimer.singleShot(0, inspect)
show_attribution_dialog(
    AttributionDialogRequest(
        "Folder A",
        "source.opj",
        tuple(f"Book {index:02d} with long metadata" for index in range(1, 21)),
    )
)
if not observed["focus"] or not observed["focus_visible"]:
    raise SystemExit(f"bottom validation target is not focused and visible: {observed}")
if not observed["error_visible"]:
    raise SystemExit(f"bottom validation error is not visible: {observed}")
if not observed["error"]:
    raise SystemExit(f"bottom validation error is empty: {observed}")
'''
        completed = _run_qt_script(script, scale_factor="1.5")
        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)

    def test_attribution_form_labels_share_right_alignment_and_width(self):
        def interact(dialog):
            labels, _, sample_type, _ = self._controls(dialog)
            sample_type.setCurrentIndex(sample_type.findData("solution"))
            visible = [label for label in labels.values() if label.isVisible()]
            self.assertTrue(visible)
            self.assertEqual(1, len({label.minimumWidth() for label in visible}))
            for label in visible:
                self.assertTrue(label.alignment() & self.QtCore.Qt.AlignmentFlag.AlignRight)
                self.assertEqual(
                    self.QtWidgets.QSizePolicy.Policy.Fixed,
                    label.sizePolicy().horizontalPolicy(),
                )
            self.assertLessEqual(max(label.geometry().right() for label in visible) - min(
                label.geometry().right() for label in visible
            ), 1)
            self._button(dialog, "取消并退出").click()

        self._run_form(
            AttributionDialogRequest("Folder A", "source.opj", ("Book1",)),
            interact,
        )

    def test_attribution_context_emphasizes_source_and_folder_separately(self):
        def interact(dialog):
            source = dialog.findChild(self.QtWidgets.QLabel, "attribution_source_name")
            folder = dialog.findChild(self.QtWidgets.QLabel, "attribution_folder_name")
            self.assertIsNotNone(source)
            self.assertIsNotNone(folder)
            self.assertEqual("source.opj", source.text())
            self.assertEqual("Folder A", folder.text())
            self.assertTrue(source.font().bold())
            self.assertTrue(folder.font().bold())
            self._button(dialog, "取消并退出").click()

        self._run_form(
            AttributionDialogRequest("Folder A", "source.opj", ("Book1",)),
            interact,
        )

    def test_attribution_context_keeps_long_source_and_folder_names_readable(self):
        long_source = ("S" * 220) + ".opju"
        long_folder = "F" * 220

        def interact(dialog):
            scroll = dialog.findChild(self.QtWidgets.QScrollArea, "attribution_body_scroll")
            source = dialog.findChild(self.QtWidgets.QLabel, "attribution_source_name")
            folder = dialog.findChild(self.QtWidgets.QLabel, "attribution_folder_name")
            self.app.processEvents()
            self.assertEqual(0, scroll.horizontalScrollBar().maximum())
            self.assertLessEqual(scroll.widget().width(), scroll.viewport().width())
            for label, expected in ((source, long_source), (folder, long_folder)):
                self.assertIsNotNone(label)
                self.assertEqual(expected, label.toolTip())
                self.assertEqual(expected, label.text())
                self.assertTrue(label.wordWrap())
                self.assertTrue(label.hasHeightForWidth())
                required_height = label.heightForWidth(max(1, label.width()))
                self.assertGreater(required_height, label.fontMetrics().height())
                self.assertGreaterEqual(label.height(), required_height)
            cancel = self._button(dialog, "取消并退出")
            scroll.ensureWidgetVisible(cancel)
            self.app.processEvents()
            top_left = cancel.mapTo(scroll.viewport(), self.QtCore.QPoint(0, 0))
            self.assertGreaterEqual(top_left.x(), 0)
            self.assertLessEqual(
                top_left.x() + cancel.width(),
                scroll.viewport().width(),
                )
            cancel.click()

        self._run_form(
            AttributionDialogRequest(long_folder, long_source, ("Book1",)),
            interact,
        )

    def test_low_information_attribution_dialog_stays_compact(self):
        def interact(dialog):
            self.assertLessEqual(dialog.height(), 400)
            self.assertLessEqual(dialog.width(), 560)
            self._button(dialog, "取消并退出").click()

        self._run_form(
            AttributionDialogRequest(
                "AH0.1mM+ANH2/AH",
                "20260113.OPJ",
                ("365", "320", "465"),
            ),
            interact,
        )

    def test_production_form_window_close_and_cancel_return_cancel(self):
        actions = {
            "window_close": lambda dialog: dialog.findChild(
                self.QtWidgets.QPushButton, "dialog_close_button"
            ).click(),
            "cancel": lambda dialog: self._button(dialog, "取消并退出").click(),
        }

        for name, interact in actions.items():
            with self.subTest(action=name):
                response = self._run_form(
                    AttributionDialogRequest("Folder A", "source.opj", ("Book1",)),
                    interact,
                )
                self.assertEqual(AttributionDialogResponse(action="cancel"), response)

    def test_attribution_form_close_button_is_not_keyboard_focusable(self):
        def interact(dialog):
            close_button = dialog.findChild(
                self.QtWidgets.QPushButton, "dialog_close_button"
            )
            self.assertEqual(
                self.QtCore.Qt.FocusPolicy.NoFocus,
                close_button.focusPolicy(),
            )
            self._button(dialog, "取消并退出").click()

        response = self._run_form(
            AttributionDialogRequest("Folder A", "source.opj", ("Book1",)),
            interact,
        )
        self.assertEqual(AttributionDialogResponse(action="cancel"), response)


class FakeQtFlags:
    window_stays_on_top = "WindowStaysOnTopHint"
    window = "Window"


class FakeButton:
    def __init__(self, text):
        self.text = text
        self.enabled = True

    def setEnabled(self, enabled):
        self.enabled = enabled


class FakeMessageBox:
    def __init__(self, clicked_action):
        self.clicked_action = clicked_action
        self.title = None
        self.text = None
        self.buttons = []
        self.flags = []
        self.executed = False

    def setWindowTitle(self, title):
        self.title = title

    def setText(self, text):
        self.text = text

    def setWindowFlag(self, flag, enabled):
        if enabled:
            self.flags.append(flag)

    def addButton(self, text, role):
        button = FakeButton(text)
        self.buttons.append(button)
        return button

    def exec(self):
        self.executed = True

    def clickedButton(self):
        if self.clicked_action is None:
            return None
        for button in self.buttons:
            if button.text == self.clicked_action:
                return button
        raise AssertionError(f"unknown clicked action {self.clicked_action}")


class FakeQtCore:
    class Qt:
        class WindowType:
            WindowStaysOnTopHint = "WindowStaysOnTopHint"
            Window = "Window"


class FakeQApplication:
    _instance = None

    def __init__(self, args):
        self.args = args
        FakeQApplication._instance = self

    @classmethod
    def instance(cls):
        return cls._instance


class FakeQtWidgets:
    def __init__(self):
        self.created = []
        FakeQApplication._instance = None
        owner = self

        class QMessageBox(FakeMessageBox):
            class ButtonRole:
                ActionRole = "ActionRole"

            def __init__(self):
                owner.created.append("QMessageBox")
                super().__init__(clicked_action="继续")

        class QApplication(FakeQApplication):
            def __init__(self, args):
                owner.created.append("QApplication")
                super().__init__(args)

        self.QMessageBox = QMessageBox
        self.QApplication = QApplication
if __name__ == "__main__":
    unittest.main()
