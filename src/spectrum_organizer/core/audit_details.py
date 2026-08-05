from __future__ import annotations

from decimal import Decimal
import json
import math
import re


_EXACT_REJECTION_REASON_TEXT = {
    "missing Note": "缺少 Note",
    "missing Data sheet": "缺少 Data 工作表",
    "multiple Note sheets are ambiguous": "存在多个 Note 工作表，无法确定唯一来源",
    "multiple Data sheets are ambiguous": "存在多个 Data 工作表，无法确定唯一来源",
    "Book-local Note must start with [EXP_FD_FILE]": "Book 对应的 Note 未以 [EXP_FD_FILE] 开头",
    "Unsupported acquisition type": "Note 中的采集类型不受支持",
    "Conflicting acquisition types": "Note 中存在冲突的采集类型",
    "Wavelength section requires both Start and End": "波长段必须同时包含 Start 和 End",
    "Wavelength section requires both Front Entrance Slit and Front Exit Slit": (
        "波长段必须同时包含 Front Entrance Slit 和 Front Exit Slit"
    ),
    "missing selected X/Y": "缺少拟提取的 X/Y 数据",
    "selected Y max <= 0": "拟提取 Y 列的最大值小于或等于 0，无法归一化",
    "Note is missing excitation or emission scan range": "Note 缺少激发或发射扫描范围",
    "Note is missing excitation or emission scan increment": "Note 缺少激发或发射扫描步长",
    "Note is missing fixed emission wavelength": "Note 缺少固定发射波长",
    "Note is missing excitation scan range": "Note 缺少激发扫描范围",
    "Note is missing excitation scan increment": "Note 缺少激发扫描步长",
    "Note is missing fixed excitation wavelength": "Note 缺少固定激发波长",
    "Note is missing emission scan range": "Note 缺少发射扫描范围",
    "Note is missing emission scan increment": "Note 缺少发射扫描步长",
    "Note is missing delayed acquisition parameters": "Note 缺少延迟谱采集参数",
    "Note is missing excitation slits": "Note 缺少激发侧入口或出口狭缝",
    "Note is missing emission slits": "Note 缺少发射侧入口或出口狭缝",
    "stored spectrum class does not match Note": "快照中的谱图类型与 Note 不一致",
    "S1 max exceeds limit": "S1 最大值超过设定上限",
    "invalid data": "数据无效",
}

_SELECTION_EXCLUSION_DETAILS = {
    "emission_duplicate_unselected": "重复发射谱审核未选择",
    "exact_excitation_duplicate_unselected": "精确重复激发谱审核未选择",
    "excitation_candidate_unselected": "激发谱审核未选择",
    "special_group_rejected": "特殊谱组审核已拒绝",
    "special_group_not_copied_to_ordinary_output": (
        "特殊谱已确认分类，但不会复制到普通输出"
    ),
    "special_duplicate_unselected": "特殊谱相关重复审核未选择",
}


def canonical_audit_detail(
    reason_code: str,
    evidence: tuple[tuple[str, str], ...] = (),
) -> str:
    exclusion_detail = _SELECTION_EXCLUSION_DETAILS.get(reason_code)
    if exclusion_detail is not None:
        return exclusion_detail

    display_reason = (
        "selected Y max <= 0"
        if reason_code == "normalization_nonpositive_max"
        else reason_code
    )
    localized = localized_rejection_reason(display_reason)
    evidence_by_name = dict(evidence)
    if reason_code == "S1 max exceeds limit":
        value_name = "s1_max"
        x_name = "x_at_s1_max"
    elif reason_code in {
        "selected Y max <= 0",
        "normalization_nonpositive_max",
    }:
        value_name = "max_y"
        x_name = "x_at_max_y"
    else:
        return localized

    details = []
    maximum = evidence_by_name.get(value_name)
    maximum_x = evidence_by_name.get(x_name)
    if maximum is not None:
        details.append(f"最大值：{maximum}")
    if maximum_x is not None:
        details.append(f"对应 X：{maximum_x}")
    suffix = f"（{'；'.join(details)}）" if details else ""
    return f"{localized}{suffix}"


def selection_exclusion_detail(reason_code: str) -> str:
    try:
        return _SELECTION_EXCLUSION_DETAILS[reason_code]
    except KeyError as exc:
        raise ValueError(
            f"unsupported selection exclusion reason: {reason_code}"
        ) from exc


def localized_rejection_reason(reason: str) -> str:
    exact = _EXACT_REJECTION_REASON_TEXT.get(reason)
    if exact is not None:
        return exact
    if reason.startswith("missing S1:"):
        return "缺少 S1 列"

    prefixes = (
        ("unsupported Origin page type: ", "不支持的 Origin 页面类型："),
        ("Missing delayed Note fields: ", "延迟谱 Note 缺少字段："),
        ("Invalid wavelength range: ", "波长范围无效："),
        ("ambiguous S1: ", "存在多个 S1 列："),
        ("Missing selected Y column: ", "缺少拟提取的 Y 列："),
        ("Ambiguous selected Y column: ", "拟提取的 Y 列不唯一："),
        ("Selected Y has no preceding X column: ", "拟提取的 Y 列前没有配套 X 列："),
        ("Selected Y column is not Y-designated: ", "拟提取的列未指定为 Y："),
        (
            "Selected Y has no preceding X-designated column: ",
            "拟提取的 Y 列前没有指定为 X 的配套列：",
        ),
        ("ambiguous selected Y: ", "拟提取的 Y 列不唯一："),
        ("missing selected Y: ", "缺少拟提取的 Y 列："),
    )
    for prefix, translated_prefix in prefixes:
        if reason.startswith(prefix):
            return f"{translated_prefix}{reason.removeprefix(prefix)}"

    if reason.startswith("Note read failed:"):
        return (
            "读取 Note 失败（原始诊断："
            f"{reason.removeprefix('Note read failed:').strip()}）"
        )
    if reason.startswith("Data read failed:"):
        return (
            "读取 Data 失败（原始诊断："
            f"{reason.removeprefix('Data read failed:').strip()}）"
        )

    invalid_numeric = re.fullmatch(
        r"Note has invalid numeric (.+?): (.+)",
        reason,
    )
    if invalid_numeric:
        field, value = invalid_numeric.groups()
        return f"Note 中的数值字段 {field} 无效：{value}"

    conflicting_note_field = re.fullmatch(
        r"Conflicting Note field (.+?) in (.+)",
        reason,
    )
    if conflicting_note_field:
        field, scope = conflicting_note_field.groups()
        return f"Note 字段 {field} 在 {scope} 中存在冲突"

    slit_conflict = re.fullmatch(
        r"(EX1|EM1) entrance and exit slit values conflict",
        reason,
    )
    if slit_conflict:
        return f"{slit_conflict.group(1)} 入口与出口狭缝数值不一致"

    physical_role_alias = re.fullmatch(
        r"selected Y and S1 resolve to the same physical column: (.+)",
        reason,
    )
    if physical_role_alias:
        return (
            "拟提取的 Y 列与 S1 指向同一物理列："
            f"{physical_role_alias.group(1)}"
        )

    blank = re.fullmatch(
        r"blank in column (.+?) at row (\d+)(?:: .+)?",
        reason,
    )
    if blank:
        column, row = blank.groups()
        return f"列 {column} 第 {row} 行为空"

    non_finite = re.fullmatch(
        r"non-finite column (.+) at row (\d+)",
        reason,
    )
    if non_finite:
        column, row = non_finite.groups()
        return f"列 {column} 第 {row} 行不是有限数值"

    duplicate = re.fullmatch(
        r"duplicate value in column (.+) at row (\d+)",
        reason,
    )
    if duplicate:
        column, row = duplicate.groups()
        return f"列 {column} 第 {row} 行的 X 数值重复"

    row_count = re.fullmatch(
        r"column (.+) has (\d+) rows but column (.+) has (\d+) rows",
        reason,
    )
    if row_count:
        first_column, first_count, second_column, second_count = (
            row_count.groups()
        )
        return (
            f"列 {first_column} 有 {first_count} 行，"
            f"列 {second_column} 有 {second_count} 行，行数不一致"
        )

    if any("\u4e00" <= character <= "\u9fff" for character in reason):
        return reason
    return f"数据或元数据不符合可用谱图要求（原始诊断：{reason}）"


def identity_discriminator(identity: str) -> str:
    try:
        payload = json.loads(identity)
    except (TypeError, ValueError):
        return identity
    if not isinstance(payload, dict):
        return identity
    return "; ".join(
        f"{key}={value}"
        for key, value in sorted(payload.items())
    )


def measurement_text(value: object) -> str:
    if isinstance(value, (list, tuple)):
        return f"({', '.join(measurement_text(item) for item in value)})"
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            return str(value)
        return str(int(value)) if value.is_integer() else repr(value)
    if isinstance(value, Decimal):
        return str(value)
    return str(value)
