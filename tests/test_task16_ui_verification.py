import pathlib
import tempfile
import sys
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from spectrum_organizer.ui.main_window import FONT_FAMILY, FONT_SIZES_PX
from spectrum_organizer.ui.qt_main_window import (
    APP_ICON_PATH,
    TASK16_ATTENTION_FRAME_INSET,
    TASK16_ATTENTION_LABEL_MIN_WIDTH,
    TASK16_DPI_PERCENTS,
    TASK16_SUMMARY_PANE_MIN_WIDTH,
    TASK16_TARGET_SIZES,
)
from validation import task16_ui_dpi_check as task16_validation
from validation.task16_ui_dpi_check import (
    TASK16_FOCUS_ORDER,
    Task16WindowInspection,
    build_task16_evidence_manifest,
    build_task16_qt_environment,
    build_task16_window_spec,
    task16_results_pass,
)


class Task16UiVerificationTests(unittest.TestCase):
    def test_task16_window_spec_covers_dpi_sizes_focus_and_attention_text(self):
        spec = build_task16_window_spec()

        self.assertEqual((100, 125, 150), TASK16_DPI_PERCENTS)
        self.assertEqual(("desktop", "compact"), tuple(TASK16_TARGET_SIZES))
        self.assertEqual((1180, 820), TASK16_TARGET_SIZES["desktop"])
        self.assertEqual((980, 700), TASK16_TARGET_SIZES["compact"])
        self.assertEqual(
            (
                "select_sources",
                "select_output_parent",
                "preflight_s1_limit",
                "preflight_steady_emission_y",
                "attribution_sample_name",
                "selection_dialog_choice",
                "completion_open_output",
                "completion_start_new_task",
                "completion_exit",
            ),
            TASK16_FOCUS_ORDER,
        )
        self.assertEqual(FONT_FAMILY, spec.font_family)
        self.assertEqual(FONT_SIZES_PX, spec.font_sizes_px)
        self.assertIn("需要你的确认：样品状态无法可靠推断。", spec.attention_messages)

    def test_task16_window_spec_requires_reflow_scroll_and_no_text_shrink(self):
        spec = build_task16_window_spec()

        self.assertTrue(spec.right_pane_scrolls)
        self.assertTrue(spec.attention_text_wraps)
        self.assertFalse(spec.allow_font_shrink)
        self.assertEqual(12, min(spec.font_sizes_px.values()))
        self.assertEqual(250, TASK16_SUMMARY_PANE_MIN_WIDTH)
        self.assertEqual(180, TASK16_ATTENTION_LABEL_MIN_WIDTH)
        self.assertGreaterEqual(TASK16_ATTENTION_FRAME_INSET, 2)

    def test_production_icon_asset_exists(self):
        self.assertTrue(APP_ICON_PATH.is_file())
        self.assertEqual("spectrum-organizer.png", APP_ICON_PATH.name)

    def test_task16_evidence_manifest_lists_every_dpi_and_size_screenshot(self):
        spec = build_task16_window_spec()
        manifest = build_task16_evidence_manifest(pathlib.Path("evidence"), spec)

        expected = {
            f"evidence/production-ui-{dpi}-{size_name}.png"
            for dpi in TASK16_DPI_PERCENTS
            for size_name in TASK16_TARGET_SIZES
        }
        self.assertEqual(expected, {path.as_posix() for path in manifest.screenshot_paths})
        self.assertEqual("evidence/production-ui-checklist.md", manifest.checklist_path.as_posix())

    def test_task16_qt_environment_sets_scale_factor_per_dpi_before_child_qt_start(self):
        env_100 = build_task16_qt_environment({}, 100)
        env_125 = build_task16_qt_environment({}, 125)
        env_150 = build_task16_qt_environment({}, 150)

        self.assertEqual("offscreen", env_100["QT_QPA_PLATFORM"])
        self.assertEqual("1", env_100["QT_SCALE_FACTOR"])
        self.assertEqual("1.25", env_125["QT_SCALE_FACTOR"])
        self.assertEqual("1.5", env_150["QT_SCALE_FACTOR"])
        with self.assertRaises(ValueError):
            build_task16_qt_environment({}, 110)

    def test_task16_validation_runs_production_window_inspector(self):
        calls = []

        def fake_inspector(*, dpi_percent, size_name, screenshot_path):
            calls.append((dpi_percent, size_name, screenshot_path.name))
            return _inspection(screenshot_path=str(screenshot_path))

        with mock.patch.object(
            task16_validation,
            "inspect_production_main_window",
            new=fake_inspector,
        ):
            with tempfile.TemporaryDirectory() as temp_dir:
                temp_path = pathlib.Path(temp_dir)
                task16_validation._run_child_once(
                    dpi=100,
                    size_name="desktop",
                    screenshot_path=temp_path / "probe.png",
                    result_path=temp_path / "probe.json",
                )

        self.assertEqual([(100, "desktop", "probe.png")], calls)

    def test_task16_result_fails_if_any_verification_boolean_fails(self):
        good = _inspection()
        missing_screenshot = _inspection(screenshot_saved=False)
        wrong_dpi = _inspection(dpi_scale_ok=False)

        self.assertTrue(good.ok)
        self.assertFalse(missing_screenshot.ok)
        self.assertFalse(wrong_dpi.ok)
        self.assertFalse(_inspection(attention_width_ok=False).ok)
        self.assertFalse(_inspection(attention_frame_ok=False).ok)
        self.assertFalse(task16_results_pass([good, missing_screenshot]))


def _inspection(**overrides):
    values = dict(
        dpi_percent=100,
        size_name="desktop",
        screenshot_path="x.png",
        screenshot_saved=True,
        device_pixel_ratio=1.0,
        dpi_scale_ok=True,
        font_family_requested_ok=True,
        font_sizes_ok=True,
        no_attention_clipping=True,
        attention_width_ok=True,
        attention_frame_ok=True,
        no_widget_overlap=True,
        focus_order_ok=True,
        dialog_topmost_flag=True,
        dialog_window_flag=True,
    )
    values.update(overrides)
    return Task16WindowInspection(**values)


if __name__ == "__main__":
    unittest.main()
