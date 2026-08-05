from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from spectrum_organizer.core.selection import SelectionSpectrum, build_candidate_display
from spectrum_organizer.safety.name_policy import NamePolicyError, validate_user_origin_name_text
from spectrum_organizer.workflow.interaction import DialogRequest


@dataclass(frozen=True)
class FinalReviewRow:
    row_id: str
    source_filename: str
    folder_path: str
    book_name: str
    attribution: str
    result: str
    has_related_conflicts: bool = False
    can_modify_attribution: bool = True


@dataclass(frozen=True)
class FinalReviewOutputBook:
    book_name: str
    column_order: tuple[str, ...]


@dataclass(frozen=True)
class FinalReviewOutputFolder:
    folder_name: str
    books: tuple[FinalReviewOutputBook, ...]
    missing_items: tuple[str, ...] = ()


@dataclass(frozen=True)
class FinalReviewViewState:
    active_tab: str = "attribution"
    search_text: str = ""
    selected_row_id: str = ""
    attribution_scroll_value: int = 0
    output_scroll_value: int = 0
    output_anchor_folder: str = ""
    output_anchor_offset: int = 0
    collapsed_output_folders: tuple[str, ...] = ()


@dataclass(frozen=True)
class FinalReviewConflictChoice:
    choice_key: str
    display_name: str
    detail: str = ""


@dataclass(frozen=True)
class FinalReviewConflictSelection:
    group_id: str
    selected_keys: tuple[str, ...] = ()
    decision: str = ""


@dataclass(frozen=True)
class FinalReviewConflictGroup:
    group_id: str
    title: str
    context: str
    selection_mode: str
    choices: tuple[FinalReviewConflictChoice, ...]
    common_fields: tuple[tuple[str, str], ...] = ()
    selected_keys: tuple[str, ...] = ()
    decision: str = ""
    stale_selected_keys: tuple[str, ...] = ()
    stale_decision: str = ""
    single_select_groups: tuple[tuple[str, ...], ...] = ()
    warning: str = ""
    stale_choices: tuple[FinalReviewConflictChoice, ...] = ()


@dataclass(frozen=True)
class FinalReviewConflictEditor:
    row_id: str
    groups: tuple[FinalReviewConflictGroup, ...]
    can_confirm: bool
    instruction: str = "请完成所有相关冲突选择。"


FinalReviewConflictEditorProvider = Callable[
    [str, tuple[FinalReviewConflictSelection, ...]],
    FinalReviewConflictEditor,
]


@dataclass(frozen=True)
class FinalReviewDialogRequest(DialogRequest):
    rows: tuple[FinalReviewRow, ...] = ()
    recognized_count: int = 0
    rejected_count: int = 0
    excluded_count: int = 0
    accepted_count: int = 0
    output_folders: tuple[FinalReviewOutputFolder, ...] = ()
    initial_view_state: FinalReviewViewState = field(
        default_factory=FinalReviewViewState
    )
    conflict_editor_provider: FinalReviewConflictEditorProvider | None = field(
        default=None,
        compare=False,
        repr=False,
    )
    background_conflict_refresh: bool = False
    initial_conflict_row_id: str = ""
    initial_conflict_selections: tuple[FinalReviewConflictSelection, ...] = ()
    initial_conflict_pending_selections: tuple[
        FinalReviewConflictSelection,
        ...,
    ] = ()
    initial_conflict_editing_group_ids: tuple[str, ...] = ()
    conflict_back_action: str = "local"

    @property
    def counts(self) -> tuple[int, int, int, int]:
        return (
            self.recognized_count,
            self.rejected_count,
            self.excluded_count,
            self.accepted_count,
        )


def final_attribution_summary_dialog(
    rows: tuple[FinalReviewRow, ...],
    *,
    recognized_count: int,
    rejected_count: int,
    excluded_count: int,
    accepted_count: int,
    output_folders: tuple[FinalReviewOutputFolder, ...] = (),
    initial_view_state: FinalReviewViewState | None = None,
    conflict_editor_provider: FinalReviewConflictEditorProvider | None = None,
    background_conflict_refresh: bool = False,
    initial_conflict_row_id: str = "",
    initial_conflict_selections: tuple[FinalReviewConflictSelection, ...] = (),
    initial_conflict_pending_selections: tuple[
        FinalReviewConflictSelection,
        ...,
    ] = (),
    initial_conflict_editing_group_ids: tuple[str, ...] = (),
    conflict_back_action: str = "local",
) -> FinalReviewDialogRequest:
    return FinalReviewDialogRequest(
        kind="final_attribution_summary",
        title="最终归属与输出计划",
        message="",
        actions=(
            "confirm",
            "modify_attribution",
            "modify_conflicts",
            "cancel",
        ),
        topmost=False,
        rows=tuple(rows),
        recognized_count=recognized_count,
        rejected_count=rejected_count,
        excluded_count=excluded_count,
        accepted_count=accepted_count,
        output_folders=tuple(output_folders),
        initial_view_state=initial_view_state or FinalReviewViewState(),
        conflict_editor_provider=conflict_editor_provider,
        background_conflict_refresh=background_conflict_refresh,
        initial_conflict_row_id=initial_conflict_row_id,
        initial_conflict_selections=initial_conflict_selections,
        initial_conflict_pending_selections=(
            initial_conflict_pending_selections
        ),
        initial_conflict_editing_group_ids=(
            initial_conflict_editing_group_ids
        ),
        conflict_back_action=conflict_back_action,
    )


def batch_write_failure_dialog(message: str) -> DialogRequest:
    return DialogRequest(
        kind="sample_record_commit_failed",
        title="样品记录写入失败",
        message=message,
        actions=("retry", "cancel"),
    )


def special_duplicate_point_review_dialog(kind: str, point_label: str, book_keys: tuple[str, ...]) -> DialogRequest:
    return DialogRequest(
        kind="special_duplicate_point_review",
        title="特殊谱图重复点审核",
        message="\n".join((kind, point_label, *book_keys)),
        actions=("select_one",),
    )


def special_overlap_assignment_dialog(book_key: str) -> DialogRequest:
    return DialogRequest(
        kind="special_overlap_assignment",
        title="特殊谱图归类",
        message=book_key,
        actions=("二维延迟谱", "时间分辨延迟谱", "regular"),
    )


def emission_duplicate_review_dialog(stage: str, candidates: tuple[SelectionSpectrum, ...]) -> DialogRequest:
    blocks = []
    for candidate in candidates:
        blocks.append("\n".join(f"{name}: {value}" for name, value in build_candidate_display(candidate)))
    return DialogRequest(
        kind="emission_duplicate_review",
        title="重复发射谱审核",
        message=f"{stage}\n\n" + "\n\n".join(blocks),
        actions=("select_one",),
    )


def cross_source_emission_conflict_dialog(book_keys: tuple[str, ...]) -> DialogRequest:
    return DialogRequest(
        kind="cross_source_emission_conflict",
        title="跨文件发射谱冲突",
        message="\n".join(book_keys),
        actions=("select_one", "返回样品归属步骤"),
    )


def save_and_close_origin_dialog() -> DialogRequest:
    return DialogRequest(
        kind="save_and_close_origin",
        title="请关闭 Origin 后继续",
        message=(
            "请先保存并关闭正在使用的 Origin，然后点击下方“重新检测”。"
            "任务会停在这里，直到你完成操作。"
        ),
        actions=("retry", "cancel"),
    )


def output_can_be_inspected_dialog() -> DialogRequest:
    return DialogRequest(
        kind="output_can_be_inspected",
        title="可以检查输出文件",
        message="Origin 控制已结束，现在可以打开输出文件检查结果。",
        actions=("continue", "cancel"),
    )


def special_group_confirmation_dialog(book_key: str) -> DialogRequest:
    return DialogRequest(
        kind="special_group_confirmation",
        title="特殊谱图确认",
        message=book_key,
        actions=("confirm", "return_to_attribution", "exclude"),
    )


def attribution_dialog(field_values: dict[str, str]) -> DialogRequest:
    can_confirm = True
    messages = ["样品信息不可输入换行。"]
    messages.extend(f"{field_name}: {value}" for field_name, value in field_values.items())
    for field_name, value in field_values.items():
        try:
            validate_user_origin_name_text(value, field_name=field_name)
        except NamePolicyError as exc:
            can_confirm = False
            messages.append(f"{field_name}: 确认被阻止 - {exc}")
    return DialogRequest(
        kind="attribution",
        title="样品归属",
        message="\n".join(messages),
        actions=("confirm", "cancel"),
        can_confirm=can_confirm,
        field_values=dict(field_values),
    )


def duplicate_emission_dialog(book_keys: tuple[str, ...]) -> DialogRequest:
    return DialogRequest(
        kind="duplicate_emission_single_select",
        title="重复发射谱审核",
        message="\n".join(book_keys),
        actions=("select_one", "return_to_attribution"),
    )


def preflight_settings_dialog(
    *,
    default_s1_limit: int,
    steady_emission_y: str,
    allow_missing_s1: bool = False,
) -> DialogRequest:
    return DialogRequest(
        kind="preflight_settings",
        title="预检设置",
        message=(
            f"S1 强度上限：{default_s1_limit}\n"
            "适用于稳态谱和延迟谱；二维稳态谱不检查。\n"
            f"发射谱 Y 列：{steady_emission_y}\n"
            "仅影响稳态发射谱。稳态激发谱固定使用 S1c/R1c，延迟谱固定使用 S1c。\n"
            f"缺少 S1 时继续：{'是' if allow_missing_s1 else '否'}"
        ),
        actions=("confirm", "cancel"),
        field_values={
            "s1Limit": str(default_s1_limit),
            "steadyEmissionY": steady_emission_y,
            "allowMissingS1": allow_missing_s1,
        },
    )


def excitation_selection_dialog(book_keys: tuple[str, ...], *, duplicate_mode: str) -> DialogRequest:
    if duplicate_mode == "multi":
        action = "select_many"
    elif duplicate_mode == "single":
        action = "select_one"
    else:
        raise ValueError(f"不支持的激发谱重复模式：{duplicate_mode}")
    return DialogRequest(
        kind="excitation_selection",
        title="激发谱选择",
        message="\n".join(book_keys),
        actions=(action, "return_to_attribution"),
    )


def database_recovery_dialog(reason: str, database_path: str, backup_path: str) -> DialogRequest:
    return DialogRequest(
        kind="database_recovery",
        title="样品数据库恢复",
        message=(
            f"{reason}\n\n"
            f"原数据库：{database_path}\n"
            f"拟创建备份：{backup_path}"
        ),
        actions=("backup_new_empty", "cancel"),
    )


def hidden_origin_confirmation_dialog(pids: tuple[int, ...]) -> DialogRequest:
    return DialogRequest(
        kind="hidden_origin_confirmation",
        title="确认关闭隐藏 Origin",
        message="\n".join(str(pid) for pid in pids),
        actions=("confirm_close_hidden_origin", "cancel"),
    )


def space_retry_cancel_dialog(temp_root: str, *, required_bytes: int, available_bytes: int) -> DialogRequest:
    return DialogRequest(
        kind="space_retry_cancel",
        title="临时空间不足",
        message=f"{temp_root}\n需要空间：{required_bytes}\n可用空间：{available_bytes}",
        actions=("retry", "cancel"),
    )


def output_parent_recovery_dialog(path: str, reason: str) -> DialogRequest:
    return DialogRequest(
        kind="output_parent_recovery",
        title="输出目录恢复",
        message=f"{path}\n{reason}",
        actions=("choose_another_parent", "cancel"),
    )


def completion_actions_dialog(output_folder: str) -> DialogRequest:
    return DialogRequest(
        kind="completion_actions",
        title="任务完成",
        message=output_folder,
        actions=("open_output_folder", "start_new_task", "exit"),
    )


def cancel_and_exit_confirmation_dialog() -> DialogRequest:
    return DialogRequest(
        kind="cancel_and_exit_confirmation",
        title="取消任务？",
        message="关闭这个确认窗口会继续当前任务。",
        actions=("继续运行", "取消并退出"),
    )


def cancelled_and_exited_dialog() -> DialogRequest:
    return DialogRequest(
        kind="cancelled_and_exited",
        title="任务已取消并退出",
        message="任务已取消并退出",
        actions=("acknowledge",),
    )
