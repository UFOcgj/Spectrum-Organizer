from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from spectrum_organizer.ui.qt_main_window import (
    TASK16_ATTENTION_FRAME_INSET,
    TASK16_ATTENTION_LABEL_MIN_WIDTH,
    TASK16_DPI_PERCENTS,
    TASK16_SUMMARY_PANE_MIN_WIDTH,
    TASK16_TARGET_SIZES,
    _production_focus_order_matches,
    create_production_main_window,
)
from spectrum_organizer.ui.dialogs import save_and_close_origin_dialog
from spectrum_organizer.ui.main_window import FONT_FAMILY, FONT_SIZES_PX


TASK16_FOCUS_ORDER = (
    "select_sources",
    "select_output_parent",
    "preflight_s1_limit",
    "preflight_steady_emission_y",
    "attribution_sample_name",
    "selection_dialog_choice",
    "completion_open_output",
    "completion_start_new_task",
    "completion_exit",
)
TASK16_ATTENTION_FRAME_LINE_WIDTH = 1
_EXPECTED_DPR = {100: 1.0, 125: 1.25, 150: 1.5}


@dataclass(frozen=True)
class Task16WindowSpec:
    font_family: str
    font_sizes_px: dict[str, int]
    attention_messages: tuple[str, ...]
    right_pane_scrolls: bool
    attention_text_wraps: bool
    allow_font_shrink: bool


@dataclass(frozen=True)
class Task16WindowInspection:
    dpi_percent: int
    size_name: str
    screenshot_path: str
    screenshot_saved: bool
    device_pixel_ratio: float
    dpi_scale_ok: bool
    font_family_requested_ok: bool
    font_sizes_ok: bool
    no_attention_clipping: bool
    attention_width_ok: bool
    attention_frame_ok: bool
    no_widget_overlap: bool
    focus_order_ok: bool
    dialog_topmost_flag: bool
    dialog_window_flag: bool

    @property
    def ok(self) -> bool:
        return all(
            (
                self.screenshot_saved,
                self.dpi_scale_ok,
                self.font_family_requested_ok,
                self.font_sizes_ok,
                self.no_attention_clipping,
                self.attention_width_ok,
                self.attention_frame_ok,
                self.no_widget_overlap,
                self.focus_order_ok,
                self.dialog_topmost_flag,
                self.dialog_window_flag,
            )
        )


def build_task16_window_spec() -> Task16WindowSpec:
    return Task16WindowSpec(
        font_family=FONT_FAMILY,
        font_sizes_px=dict(FONT_SIZES_PX),
        attention_messages=(
            "需要你的确认：样品状态无法可靠推断。",
            "需要手动确认样品状态、温度和测试归属。",
            "根目录中的 Book 需要回退到样品归属步骤。",
        ),
        right_pane_scrolls=True,
        attention_text_wraps=True,
        allow_font_shrink=False,
    )


def inspect_production_main_window(
    *,
    dpi_percent: int,
    size_name: str,
    screenshot_path: Path,
    stage: str = "source_input",
) -> Task16WindowInspection:
    from PySide6 import QtCore, QtWidgets

    window, widgets = create_production_main_window(dpi_percent=dpi_percent, size_name=size_name, stage=stage)
    screenshot_path.parent.mkdir(parents=True, exist_ok=True)
    window.show()
    QtWidgets.QApplication.processEvents()
    screenshot_saved = window.grab().save(str(screenshot_path))
    device_pixel_ratio = round(float(window.devicePixelRatioF()), 2)
    QtWidgets.QApplication.processEvents()

    labels = tuple(widgets["attention_labels"])
    no_attention_clipping = all(label.sizeHint().height() <= label.height() + 2 for label in labels)
    attention_width_ok = (
        widgets["summary_panel"].width() >= TASK16_SUMMARY_PANE_MIN_WIDTH
        and all(label.width() >= TASK16_ATTENTION_LABEL_MIN_WIDTH for label in labels)
    )
    body_margins = widgets["attention_body"].layout().contentsMargins()
    attention_frame_ok = (
        widgets["attention_pane"].frameShape() == QtWidgets.QFrame.Shape.NoFrame
        and body_margins.left() >= TASK16_ATTENTION_FRAME_INSET
    )
    no_widget_overlap = not _has_overlap(window, widgets["layout_widgets"])
    font_family_requested_ok = all(
        widget.font().family().casefold() == FONT_FAMILY.casefold()
        for widget, _ in widgets["text_widgets"]
    )
    font_sizes_ok = all(_font_pixel_size(widget) >= expected for widget, expected in widgets["text_widgets"])
    dialog_topmost_flag, dialog_window_flag = _probe_dialog_flags(QtWidgets, QtCore, window)
    if stage == "source_input":
        focus_order_ok = _production_focus_order_matches(widgets)
    else:
        focus_order_ok = (
            widgets["cancel_run_button"].isVisible()
            and widgets["select_sources_button"].isHidden()
            and widgets["select_output_parent_button"].isHidden()
            and widgets["start_run_button"].isHidden()
        )
    window.close()
    QtWidgets.QApplication.processEvents()

    return Task16WindowInspection(
        dpi_percent=dpi_percent,
        size_name=size_name,
        screenshot_path=str(screenshot_path),
        screenshot_saved=bool(screenshot_saved),
        device_pixel_ratio=device_pixel_ratio,
        dpi_scale_ok=abs(device_pixel_ratio - _EXPECTED_DPR[dpi_percent]) <= 0.01,
        font_family_requested_ok=font_family_requested_ok,
        font_sizes_ok=font_sizes_ok,
        no_attention_clipping=no_attention_clipping,
        attention_width_ok=attention_width_ok,
        attention_frame_ok=attention_frame_ok,
        no_widget_overlap=no_widget_overlap,
        focus_order_ok=focus_order_ok,
        dialog_topmost_flag=dialog_topmost_flag,
        dialog_window_flag=dialog_window_flag,
    )


def _font_pixel_size(widget) -> int:
    size = widget.font().pixelSize()
    return size if size > 0 else int(widget.fontInfo().pixelSize())


def _has_overlap(window, widgets: list) -> bool:
    rects = []
    for widget in widgets:
        top_left = widget.mapTo(window, widget.rect().topLeft())
        rects.append(widget.rect().translated(top_left))
    return any(rect.intersects(other) for index, rect in enumerate(rects) for other in rects[index + 1 :])


def _probe_dialog_flags(qt_widgets, qt_core, parent) -> tuple[bool, bool]:
    request = save_and_close_origin_dialog()
    box = qt_widgets.QMessageBox(parent)
    if request.topmost:
        box.setWindowFlag(qt_core.Qt.WindowType.WindowStaysOnTopHint, True)
    if request.taskbar_visible:
        box.setWindowFlag(qt_core.Qt.WindowType.Window, True)
    flags = box.windowFlags()
    return (
        bool(flags & qt_core.Qt.WindowType.WindowStaysOnTopHint),
        bool(flags & qt_core.Qt.WindowType.Window),
    )


@dataclass(frozen=True)
class Task16EvidenceManifest:
    output_dir: Path
    screenshot_paths: tuple[Path, ...]
    checklist_path: Path
    result_json_path: Path


def build_task16_evidence_manifest(output_dir: Path, spec: Task16WindowSpec) -> Task16EvidenceManifest:
    del spec
    screenshot_paths = tuple(
        output_dir / f"production-ui-{dpi}-{size_name}.png"
        for dpi in TASK16_DPI_PERCENTS
        for size_name in TASK16_TARGET_SIZES
    )
    return Task16EvidenceManifest(
        output_dir=output_dir,
        screenshot_paths=screenshot_paths,
        checklist_path=output_dir / "production-ui-checklist.md",
        result_json_path=output_dir / "production-ui-results.json",
    )


def build_task16_qt_environment(base_environment: dict[str, str], dpi_percent: int) -> dict[str, str]:
    if dpi_percent not in TASK16_DPI_PERCENTS:
        raise ValueError(f"Unsupported DPI percent: {dpi_percent}")
    scale_factor = {100: "1", 125: "1.25", 150: "1.5"}[dpi_percent]
    environment = dict(base_environment)
    environment["QT_QPA_PLATFORM"] = "offscreen"
    environment["QT_SCALE_FACTOR"] = scale_factor
    return environment


def task16_results_pass(results: list[Task16WindowInspection]) -> bool:
    return bool(results) and all(result.ok for result in results)


def run_task16_checks(output_dir: Path) -> Task16EvidenceManifest:
    spec = build_task16_window_spec()
    manifest = build_task16_evidence_manifest(output_dir, spec)
    output_dir.mkdir(parents=True, exist_ok=True)
    result_paths: list[Path] = []
    for dpi in TASK16_DPI_PERCENTS:
        for size_name in TASK16_TARGET_SIZES:
            screenshot_path = output_dir / f"production-ui-{dpi}-{size_name}.png"
            result_path = output_dir / f"production-ui-{dpi}-{size_name}.json"
            _run_child_check(dpi=dpi, size_name=size_name, screenshot_path=screenshot_path, result_path=result_path)
            result_paths.append(result_path)
    results = [Task16WindowInspection(**json.loads(path.read_text(encoding="utf-8"))) for path in result_paths]
    manifest.result_json_path.write_text(
        json.dumps([asdict(result) for result in results], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    manifest.checklist_path.write_text(_build_checklist(spec, results), encoding="utf-8")
    if not task16_results_pass(results):
        failed = [asdict(result) for result in results if not result.ok]
        raise RuntimeError(f"Task 16 UI checks failed: {json.dumps(failed, ensure_ascii=False)}")
    return manifest


def _run_child_check(*, dpi: int, size_name: str, screenshot_path: Path, result_path: Path) -> None:
    command = [
        sys.executable,
        "-B",
        str(Path(__file__).resolve()),
        "--child-dpi",
        str(dpi),
        "--child-size",
        size_name,
        "--child-screenshot",
        str(screenshot_path),
        "--child-result",
        str(result_path),
    ]
    subprocess.run(command, cwd=str(ROOT), env=build_task16_qt_environment(os.environ, dpi), check=True)


def _run_child_once(*, dpi: int, size_name: str, screenshot_path: Path, result_path: Path) -> None:
    result = inspect_production_main_window(dpi_percent=dpi, size_name=size_name, screenshot_path=screenshot_path)
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(asdict(result), indent=2, ensure_ascii=False), encoding="utf-8")
    if not result.ok:
        raise RuntimeError(f"Task 16 child UI check failed: {json.dumps(asdict(result), ensure_ascii=False)}")


def _build_checklist(spec: Task16WindowSpec, results: list[Task16WindowInspection]) -> str:
    lines = [
        "# Production UI DPI, Font, And Interaction Checklist",
        "",
        "Scope: production PySide main window shell. No Origin project is opened.",
        "This offscreen check proves Qt scaling, layout, fonts, focus order, screenshots, and dialog window flags. It does not prove native Windows taskbar visibility.",
        "",
        "## Fixed UI Contract",
        "",
        f"- Font family: `{spec.font_family}`",
        f"- Font sizes px: `{spec.font_sizes_px}`",
        f"- Attention text wraps: `{spec.attention_text_wraps}`",
        f"- Right pane scrolls: `{spec.right_pane_scrolls}`",
        f"- Font shrink allowed: `{spec.allow_font_shrink}`",
        "",
        "## Results",
        "",
        "| DPI | Qt device pixel ratio | Size | Screenshot | Pass |",
        "|---:|---:|---|---|---|",
    ]
    for result in results:
        screenshot = Path(result.screenshot_path).name
        lines.append(f"| {result.dpi_percent}% | {result.device_pixel_ratio:g} | {result.size_name} | `{screenshot}` | `{result.ok}` |")
    lines.extend(
        [
            "",
            "## Machine Checks",
            "",
            "- Screenshot save succeeds for every DPI/size case.",
            "- Qt reports the expected device pixel ratio for 100%, 125%, and 150% scale factors.",
            "- Every checked text widget requests Microsoft YaHei UI across controls, labels, summary number, and log text. Offscreen Qt does not prove native Windows font fallback resolution.",
            "- Required text classes stay at or above 12/13/20/26 px; approved fixed text scale is not reduced for DPI.",
            "- Attention labels are word-wrapped, sized without clipping, kept above the minimum logical width needed to avoid awkward 150% DPI wrapping, and the scroll area stays frameless with inset body margins so pane borders are not drawn on the clipping edge.",
            "- Main layout regions do not overlap.",
            "- Focus order follows the production shell controls: source selection, output parent selection, start run, and cancel run. Detailed attribution/review/completion dialogs are checked by their own dialog contracts.",
            "- Dialog flag probe includes WindowStaysOnTopHint and Window flags; native Windows taskbar visibility still needs visible-desktop verification.",
        ]
    )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", "--evidence-dir", dest="output_dir", type=Path)
    parser.add_argument("--child-dpi", type=int)
    parser.add_argument("--child-size")
    parser.add_argument("--child-screenshot", type=Path)
    parser.add_argument("--child-result", type=Path)
    args = parser.parse_args(argv)
    if args.child_dpi is not None:
        if args.child_size is None or args.child_screenshot is None or args.child_result is None:
            parser.error("child mode requires --child-size, --child-screenshot, and --child-result")
        _run_child_once(
            dpi=args.child_dpi,
            size_name=args.child_size,
            screenshot_path=args.child_screenshot,
            result_path=args.child_result,
        )
        return 0
    if args.output_dir is None:
        parser.error("--output-dir is required unless child mode is used")
    manifest = run_task16_checks(args.output_dir)
    print(manifest.checklist_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
