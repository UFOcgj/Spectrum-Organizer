from __future__ import annotations

from dataclasses import dataclass


FONT_FAMILY = "Microsoft YaHei UI"
FONT_SIZES_PX = {
    "supporting": 12,
    "body": 13,
    "table": 13,
    "control": 13,
    "current_task_title": 20,
    "key_summary_number": 26,
}
PRODUCTION_DESIGN_TOKENS = {
    "instrument_green": "#17332F",
    "deep_panel": "#0F2522",
    "mint_signal": "#66D6BF",
    "work_surface": "#F3F7F5",
    "rule_line": "#A8BBB4",
    "warning_amber": "#B86B10",
    "danger_red": "#A33A32",
}
PRODUCTION_REQUIRED_OBJECT_NAMES = (
    "select_sources_button",
    "select_output_parent_button",
    "preflight_settings_summary_label",
    "start_run_button",
    "cancel_run_button",
    "phase_rail",
    "attention_pane",
    "run_log",
    "output_path_label",
)


@dataclass(frozen=True)
class MainWindowContract:
    regions: tuple[str, ...]
    style: str
    decorative_gradients: bool
    decorative_orbs: bool
    main_window_visible_throughout: bool
    font_family: str
    font_sizes_px: dict[str, int]
    available_actions: tuple[str, ...]
    required_object_names: tuple[str, ...]


@dataclass(frozen=True)
class DpiFontPolicy:
    dpi_percent: int
    font_sizes_px: dict[str, int]
    reflow_or_scroll: bool
    clip_text: bool


def build_production_design_tokens() -> dict[str, str]:
    return dict(PRODUCTION_DESIGN_TOKENS)


def build_main_window_contract() -> MainWindowContract:
    return MainWindowContract(
        regions=(
            "left_phase_rail",
            "central_task_area",
            "right_summary_attention_pane",
            "bottom_log_progress_area",
        ),
        style="dark_green_lab_instrument",
        decorative_gradients=False,
        decorative_orbs=False,
        main_window_visible_throughout=True,
        font_family=FONT_FAMILY,
        font_sizes_px=dict(FONT_SIZES_PX),
        available_actions=("select_sources", "select_output_parent", "confirm_preflight_settings", "start_run", "cancel", "close"),
        required_object_names=PRODUCTION_REQUIRED_OBJECT_NAMES,
    )


def scaled_font_policy(dpi_percent: int) -> DpiFontPolicy:
    if dpi_percent not in (100, 125, 150):
        raise ValueError("Task 13 only defines the 100/125/150 percent DPI contract")
    return DpiFontPolicy(
        dpi_percent=dpi_percent,
        font_sizes_px=dict(FONT_SIZES_PX),
        reflow_or_scroll=True,
        clip_text=False,
    )
