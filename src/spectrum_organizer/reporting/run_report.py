from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

from spectrum_organizer.core.audit_details import identity_discriminator
from spectrum_organizer.core.output_model import OutputPlan


@dataclass(frozen=True)
class ReportItem:
    subject: str
    detail: str


@dataclass(frozen=True)
class SpecialGroupSummary:
    kind: str
    book_keys: tuple[str, ...]


@dataclass(frozen=True)
class SampleAttribution:
    canonical_sample_label: str
    source_path: Path
    status: str
    book_key: str = ""
    source_filename: str = ""
    folder_path: str = ""
    book_name: str = ""


@dataclass(frozen=True)
class ReportData:
    output_path: Path
    ignored_duplicate_input_paths: tuple[Path, ...]
    rejections: tuple[ReportItem, ...]
    exclusions: tuple[ReportItem, ...]
    warnings: tuple[str, ...]
    special_groups: tuple[SpecialGroupSummary, ...]
    final_attributions: tuple[SampleAttribution, ...]
    output_plan: OutputPlan
    input_paths: tuple[Path, ...] = ()
    settings: tuple[ReportItem, ...] = ()
    manual_selections: tuple[ReportItem, ...] = ()
    source_fingerprints: tuple[ReportItem, ...] = ()
    count_reconciliation: tuple[ReportItem, ...] = ()
    errors: tuple[ReportItem, ...] = ()


@dataclass(frozen=True)
class CompletionPopupSummary:
    message: str
    output_path: Path
    report_path: Path
    complete_folder_count: int
    incomplete_folder_count: int
    ignored_duplicate_count: int
    rejection_count: int
    exclusion_count: int
    error_count: int
    warning_count: int
    special_group_count: int
    attribution_count: int


@dataclass(frozen=True)
class FinalOutputPlanSummary:
    message: str
    counts_closed: bool
    folder_count: int
    book_count: int
    column_count: int
    complete_folder_count: int
    incomplete_folder_count: int


def build_approved_output_report(
    approved_snapshot,
    *,
    output_path: Path,
    source_fingerprints_after: tuple[object, ...],
    verifier_readback_spectrum_count: int,
    verifier_readback_column_count: int,
) -> str:
    source_fingerprints_before = tuple(
        getattr(
            approved_snapshot,
            "selected_source_fingerprints_before",
            (),
        )
        or approved_snapshot.source_fingerprints_before
    )
    source_fingerprints_after = tuple(source_fingerprints_after)
    if len(source_fingerprints_before) != len(
        source_fingerprints_after
    ):
        raise ValueError(
            "source fingerprint count changed before report construction"
        )
    source_paths_by_id = {
        item.source_id: Path(item.snapshot.path)
        for item in approved_snapshot.approved_sources
    }
    rejected_keys = {
        item.book_key
        for item in approved_snapshot.rejections
    }
    excluded_keys = {
        item.book_key
        for item in approved_snapshot.exclusions
    }
    count = approved_snapshot.count_reconciliation
    report_data = ReportData(
        output_path=Path(output_path),
        ignored_duplicate_input_paths=tuple(
            approved_snapshot.ignored_duplicate_input_paths
        ),
        rejections=tuple(
            ReportItem(
                _audit_subject(item, source_paths_by_id),
                item.detail,
            )
            for item in approved_snapshot.rejections
        ),
        exclusions=tuple(
            ReportItem(
                _audit_subject(item, source_paths_by_id),
                item.detail,
            )
            for item in approved_snapshot.exclusions
        ),
        warnings=(),
        special_groups=tuple(
            SpecialGroupSummary(
                choice.subject,
                tuple(choice.selected_book_keys),
            )
            for choice in approved_snapshot.review_choices
            if choice.kind == "special_group"
            and choice.selected_book_keys
            and choice.decision != "reject_group"
        ),
        final_attributions=tuple(
            SampleAttribution(
                attribution.canonical_sample_label,
                source_paths_by_id[attribution.source_id],
                _attribution_status(
                    attribution.book_key,
                    rejected_keys,
                    excluded_keys,
                ),
                book_key=attribution.book_key,
                source_filename=(
                    attribution.source_filename
                    or source_paths_by_id[attribution.source_id].name
                ),
                folder_path=attribution.folder_path,
                book_name=(
                    attribution.display_name
                    or attribution.short_name
                ),
            )
            for attribution in approved_snapshot.attributions
        ),
        output_plan=approved_snapshot.output_plan,
        input_paths=tuple(
            Path(item.path)
            for item in source_fingerprints_before
        ),
        settings=_report_settings(
            approved_snapshot.settings_snapshot
        ),
        manual_selections=tuple(
            ReportItem(
                _review_kind_label(choice.kind),
                _review_choice_detail(choice),
            )
            for choice in approved_snapshot.review_choices
        ),
        source_fingerprints=tuple(
            ReportItem(
                str(before.path),
                _fingerprint_detail(before, after),
            )
            for before, after in zip(
                source_fingerprints_before,
                source_fingerprints_after,
                strict=True,
            )
        ),
        count_reconciliation=(
            ReportItem(
                "识别 Book",
                str(count.recognizable_book_count),
            ),
            ReportItem("拒绝 Book", str(count.rejected_book_count)),
            ReportItem("排除 Book", str(count.excluded_book_count)),
            ReportItem(
                "接受普通谱",
                str(count.accepted_ordinary_spectrum_count),
            ),
            ReportItem(
                "输出计划谱图",
                str(count.output_plan_spectrum_count),
            ),
            ReportItem(
                "输出计划列",
                str(count.output_plan_column_count),
            ),
            ReportItem(
                "验证回读谱图",
                str(verifier_readback_spectrum_count),
            ),
            ReportItem(
                "验证回读列",
                str(verifier_readback_column_count),
            ),
        ),
        errors=tuple(
            ReportItem(
                str(issue.original_path),
                f"{issue.reason}；处理建议：{issue.recommendation}",
            )
            for issue in approved_snapshot.source_input_issues
        ),
    )
    return build_success_report(report_data)


APPROVED_OUTPUT_LEDGER_SECTION_TITLES = (
    "本次设置",
    "忽略的重复输入路径",
    "拒绝",
    "排除",
    "错误",
    "警告",
    "人工选择",
    "特殊谱组",
    "样品归属",
    "输出 Folder/Book 映射",
    "齐全 Folder",
    "不齐全 Folder",
    "仅激发谱 Folder",
)


def approved_output_report_ledger(
    approved_snapshot,
) -> dict[str, tuple[str, ...]]:
    before = tuple(
        getattr(
            approved_snapshot,
            "selected_source_fingerprints_before",
            (),
        )
        or approved_snapshot.source_fingerprints_before
    )
    count = approved_snapshot.count_reconciliation
    report = build_approved_output_report(
        approved_snapshot,
        output_path=Path("__runtime_audit_output__"),
        source_fingerprints_after=before,
        verifier_readback_spectrum_count=count.output_plan_spectrum_count,
        verifier_readback_column_count=count.output_plan_column_count,
    )
    sections = _rendered_report_sections(report)
    return {
        title: sections[title]
        for title in APPROVED_OUTPUT_LEDGER_SECTION_TITLES
    }


def _rendered_report_sections(text: str) -> dict[str, tuple[str, ...]]:
    sections: dict[str, list[str]] = {}
    current = None
    for line in text.splitlines():
        if not line:
            current = None
        elif line.startswith("- "):
            if current is None:
                raise ValueError("report entry has no section")
            sections[current].append(line[2:])
        elif line[:1].isspace():
            if current is None or not sections[current]:
                raise ValueError("report continuation has no entry")
            sections[current][-1] += "\n" + line
        else:
            if line in sections:
                raise ValueError(f"report section is duplicated: {line}")
            current = line
            sections[current] = []
    return {
        title: tuple(entries)
        for title, entries in sections.items()
    }


def _audit_subject(item, source_paths_by_id: dict[str, Path]) -> str:
    source_id = str(getattr(item, "source_id", ""))
    source_path = source_paths_by_id.get(source_id)
    source = str(source_path) if source_path is not None else str(
        getattr(item, "source_filename", "")
    )
    display_name = str(getattr(item, "display_name", ""))
    short_name = str(getattr(item, "short_name", ""))
    location = " / ".join(
        part
        for part in (
            str(getattr(item, "folder_path", "")),
            display_name or short_name,
        )
        if part
    )
    source_identity = (
        f"{source} [source_id={source_id}]" if source_id else source
    )
    book_identity = []
    if short_name and short_name != display_name:
        book_identity.append(f"short={short_name}")
    book_key = str(getattr(item, "book_key", ""))
    if book_key:
        book_identity.append(f"BookKey={book_key}")
    subject = " · ".join(part for part in (source_identity, location) if part)
    if book_identity:
        subject += f" [{'; '.join(book_identity)}]"
    return subject or book_key


def _attribution_status(
    book_key: str,
    rejected_keys: set[str],
    excluded_keys: set[str],
) -> str:
    if book_key in rejected_keys:
        return "rejected"
    if book_key in excluded_keys:
        return "excluded"
    return "accepted"


def _report_settings(settings) -> tuple[ReportItem, ...]:
    labels = {
        "s1Limit": "S1 强度上限",
        "steadyEmissionY": "稳态发射强度列",
        "allowMissingS1": "缺少 S1 时继续",
    }
    items = []
    for key, value in settings.items():
        if isinstance(value, bool):
            value = "是" if value else "否"
        items.append(ReportItem(labels.get(key, str(key)), str(value)))
    return tuple(items)


def _review_kind_label(kind: str) -> str:
    return {
        "special_group": "特殊谱组选择",
        "special_duplicate": "特殊谱重复选择",
        "special_overlap": "特殊谱类型选择",
        "emission": "重复发射谱选择",
        "excitation": "激发谱选择",
    }.get(kind, kind)


def _review_choice_detail(choice) -> str:
    parts = [
        "已选 " + "、".join(choice.selected_book_keys)
    ]
    if choice.candidate_book_keys:
        parts.append(
            "候选 " + "、".join(choice.candidate_book_keys)
        )
    if choice.decision:
        parts.append(f"决定 {choice.decision}")
    parts.append(f"来源 {choice.decision_source}")
    return "；".join(parts)


def _fingerprint_detail(before, after) -> str:
    status = "未改变" if before == after else "已改变"
    return (
        f"提交前 SHA-256={before.sha256}；"
        f"输出后 SHA-256={after.sha256}；"
        f"大小={after.size_bytes}；"
        f"UTC mtime_ns={after.mtime_ns}；{status}"
    )


def build_final_output_plan_summary(
    plan: OutputPlan,
    count_reconciliation,
    *,
    review_decisions: tuple[str, ...] = (),
) -> FinalOutputPlanSummary:
    incomplete_names = {
        folder.folder_name
        for folder in plan.incomplete_folders
    }
    complete = tuple(
        folder.name
        for folder in plan.folders
        if not folder.is_fallback
        and folder.name not in incomplete_names
    )
    neutral = tuple(
        folder.name
        for folder in plan.folders
        if folder.is_fallback
    )
    book_count = sum(len(folder.books) for folder in plan.folders)
    folder_lines: list[str] = []
    for folder in plan.folders:
        folder_lines.append(
            f"Folder：{folder.name}（{len(folder.books)} 个 Book，"
            f"{sum(len(book.columns) for book in folder.books)} 列）"
        )
        for book in folder.books:
            folder_lines.append(f"  Book：{book.display_name}")
            for index, column in enumerate(book.columns, start=1):
                details = [f"Comment={column.comment}"]
                if column.method:
                    details.append(f"Method={column.method}")
                if column.formula:
                    details.append(f"F(x)={column.formula}")
                folder_lines.append(
                    f"    列 {index} [{_output_column_kind(column.kind)}] · "
                    + " · ".join(details)
                )
    completeness_lines = [
        f"完整：{folder_name}"
        for folder_name in complete
    ]
    for folder in plan.incomplete_folders:
        completeness_lines.append(f"不完整：{folder.folder_name}")
        completeness_lines.extend(
            f"  缺少：{label}"
            for label in folder.missing_labels
        )
    completeness_lines.extend(
        f"不参与完整性：{folder_name}"
        for folder_name in neutral
    )
    lines = [
        "数量核对",
        (
            f"识别 {count_reconciliation.recognizable_book_count} · "
            f"拒绝 {count_reconciliation.rejected_book_count} · "
            f"排除 {count_reconciliation.excluded_book_count} · "
            f"接受 {count_reconciliation.accepted_ordinary_spectrum_count}"
        ),
        (
            f"计划输出谱图 {count_reconciliation.output_plan_spectrum_count} · "
            f"Folder {len(plan.folders)} · Book {book_count} · "
            f"列 {count_reconciliation.output_plan_column_count}"
        ),
        "",
        "输出结构",
        *(folder_lines or ("无可输出 Folder",)),
        "",
        "审核决定",
        *(review_decisions or ("无人工审核决定",)),
        "",
        "完整性",
        *(completeness_lines or ("无完整性条目",)),
    ]
    return FinalOutputPlanSummary(
        message="\n".join(lines),
        counts_closed=bool(count_reconciliation.is_closed),
        folder_count=len(plan.folders),
        book_count=book_count,
        column_count=count_reconciliation.output_plan_column_count,
        complete_folder_count=len(complete),
        incomplete_folder_count=len(plan.incomplete_folders),
    )


def _output_column_kind(kind: str) -> str:
    return {
        "x": "X",
        "raw_y": "原始 Y",
        "norm_y": "归一化 Y",
    }.get(kind, kind)


def build_success_report(data: ReportData) -> str:
    lines: list[str] = []
    _append_section(lines, "输入路径", tuple(str(path) for path in data.input_paths))
    _append_section(lines, "输出路径", (str(data.output_path),))
    _append_items(lines, "本次设置", data.settings)
    _append_section(
        lines,
        "忽略的重复输入路径",
        tuple(str(path) for path in data.ignored_duplicate_input_paths),
    )
    _append_items(lines, "拒绝", data.rejections)
    _append_items(lines, "排除", data.exclusions)
    _append_items(lines, "错误", data.errors)
    _append_section(lines, "警告", _report_warnings(data))
    _append_items(lines, "人工选择", data.manual_selections)
    _append_section(
        lines,
        "特殊谱组",
        tuple(_format_special_group(group) for group in data.special_groups),
    )
    _append_section(
        lines,
        "样品归属",
        tuple(_format_attribution(item) for item in data.final_attributions),
    )
    _append_items(lines, "源文件指纹", data.source_fingerprints)
    _append_items(lines, "数量核对", data.count_reconciliation)
    _append_section(
        lines,
        "输出 Folder/Book 映射",
        _folder_book_mapping_lines(data.output_plan),
    )
    complete, incomplete, fallback = _folder_report_lines(data.output_plan)
    _append_section(lines, "齐全 Folder", complete)
    _append_section(lines, "不齐全 Folder", incomplete)
    _append_section(lines, "仅激发谱 Folder", fallback)
    return "\n".join(lines) + "\n"


def paired_spectrum_warnings(plan: OutputPlan) -> tuple[str, ...]:
    all_labels = tuple(
        label
        for folder in plan.folders
        for label in _folder_raw_labels(folder)
    )
    states_by_label: dict[tuple[str, str], set[tuple[str, str]]] = {}
    for family, label, identity, temperature, _side in all_labels:
        states_by_label.setdefault((family, label), set()).add(
            (identity, temperature)
        )
    missing_excitation: set[tuple[str, str, str, str]] = set()
    missing_emission: set[tuple[str, str, str, str]] = set()
    for folder in plan.folders:
        labels = _folder_raw_labels(folder)
        sided_states = {
            ((family, label, identity, temperature), side)
            for family, label, identity, temperature, side in labels
        }
        for family, label, identity, temperature, side in labels:
            state = (family, label, identity, temperature)
            if folder.is_fallback and side == "excitation":
                missing_emission.add(state)
            elif (
                not folder.is_fallback
                and side == "emission"
                and (state, "excitation") not in sided_states
            ):
                missing_excitation.add(state)
    warnings = [
        f"{state[0]} 样品 {_warning_state_label(state, states_by_label)}"
        " 缺少配套激发谱。"
        for state in sorted(missing_excitation)
    ]
    warnings.extend(
        f"{state[0]} 样品 {_warning_state_label(state, states_by_label)}"
        " 缺少配套发射谱。"
        for state in sorted(missing_emission)
    )
    return tuple(warnings)


def _report_warnings(data: ReportData) -> tuple[str, ...]:
    return tuple(dict.fromkeys(data.warnings + paired_spectrum_warnings(data.output_plan)))


def build_completion_summary(data: ReportData, *, report_path: Path) -> CompletionPopupSummary:
    complete, incomplete, _fallback = _folder_report_lines(data.output_plan)
    message = (
        f"输出已创建：{data.output_path}。"
        f"拒绝：{len(data.rejections)}；排除：{len(data.exclusions)}；"
        f"错误：{len(data.errors)}；警告：{len(_report_warnings(data))}。"
        f"齐全 Folder：{len(complete)}；不齐全 Folder：{len(incomplete)}。"
        f"运行报告：{report_path}。"
    )
    return CompletionPopupSummary(
        message=message,
        output_path=data.output_path,
        report_path=report_path,
        complete_folder_count=len(complete),
        incomplete_folder_count=len(incomplete),
        ignored_duplicate_count=len(data.ignored_duplicate_input_paths),
        rejection_count=len(data.rejections),
        exclusion_count=len(data.exclusions),
        error_count=len(data.errors),
        warning_count=len(_report_warnings(data)),
        special_group_count=len(data.special_groups),
        attribution_count=len(data.final_attributions),
    )


def _append_items(lines: list[str], title: str, items: tuple[ReportItem, ...]) -> None:
    _append_section(lines, title, tuple(f"{item.subject}：{item.detail}" for item in items))


def _append_section(lines: list[str], title: str, entries: tuple[str, ...]) -> None:
    if lines:
        lines.append("")
    lines.append(title)
    if not entries:
        lines.append("- 无")
        return
    lines.extend(f"- {entry}" for entry in entries)


def _format_special_group(group: SpecialGroupSummary) -> str:
    kind = {
        "steady_2d": "二维稳态谱",
        "delayed_2d": "二维延迟谱",
        "delay_time_series": "时间分辨延迟谱",
    }.get(group.kind, group.kind)
    return f"{kind}：{', '.join(group.book_keys)}"


def _format_attribution(attribution: SampleAttribution) -> str:
    status = {
        "accepted": "已接受",
        "rejected": "已拒绝",
        "excluded": "已排除",
    }.get(attribution.status, attribution.status)
    if attribution.book_key:
        location = " / ".join(
            part
            for part in (
                str(attribution.source_path),
                attribution.folder_path,
                attribution.book_name,
            )
            if part
        )
        return (
            f"{attribution.canonical_sample_label}：{location}；"
            f"BookKey={attribution.book_key}（{status}）"
        )
    return (
        f"{attribution.canonical_sample_label}："
        f"{attribution.source_path}（{status}）"
    )


def _folder_book_mapping_lines(plan: OutputPlan) -> tuple[str, ...]:
    return tuple(
        f"Folder：{folder.name}；Book：{book.display_name}"
        for folder in plan.folders
        for book in folder.books
    )


def _folder_report_lines(
    plan: OutputPlan,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    incomplete_by_name = {folder.folder_name: folder for folder in plan.incomplete_folders}
    complete = tuple(
        folder.name
        for folder in plan.folders
        if not folder.is_fallback
        and folder.name not in incomplete_by_name
    )
    incomplete = tuple(
        "\n".join(
            (
                folder.folder_name,
                *(
                    f"  缺少样品状态：{label}"
                    for label in folder.missing_labels
                ),
            )
        )
        for folder in plan.incomplete_folders
    )
    fallback = tuple(
        folder.name
        for folder in plan.folders
        if folder.is_fallback
    )
    return complete, incomplete, fallback


def _folder_raw_labels(folder) -> set[tuple[str, str, str, str, str]]:
    labels: set[tuple[str, str, str, str, str]] = set()
    for book in folder.books:
        for column in book.raw_y_columns:
            if column.source is not None:
                labels.add(
                    (
                        column.source.family,
                        column.source.canonical_sample_label,
                        column.source.sample_system_identity,
                        column.source.temperature,
                        column.source.side,
                    )
                )
                continue
            parsed = _parse_raw_comment(column.comment)
            if parsed is not None:
                family, label, side = parsed
                labels.add((family, label, label, "", side))
    return labels


def _warning_state_label(
    state: tuple[str, str, str, str],
    states_by_label: dict[tuple[str, str], set[tuple[str, str]]],
) -> str:
    family, label, identity, temperature = state
    if len(states_by_label[(family, label)]) == 1:
        return label
    detail = identity_discriminator(identity)
    if temperature:
        detail = f"{detail}; 温度={temperature}"
    return f"{label} [{detail}]"


def _parse_raw_comment(comment: str) -> tuple[str, str, str] | None:
    match = re.match(r"^(?P<label>.+)_(?P<family>[FP])(?P<side>Ex)?[-+]?\d", comment)
    if match is None:
        return None
    side = "excitation" if match.group("side") else "emission"
    return (match.group("family"), match.group("label"), side)
