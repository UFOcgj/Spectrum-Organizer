from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from spectrum_organizer.core.metadata_numeric import is_finite_real_number
from spectrum_organizer.safety.fingerprints import SourceSnapshot
from spectrum_organizer.safety.source_copies import required_temp_bytes


READER_SIDECAR_AUTH_ENV = "SPECTRUM_ORGANIZER_READER_SIDECAR_AUTH"


class ProductRunnerError(RuntimeError):
    pass


class UnsupportedSourceInputError(ProductRunnerError):
    """One selected project cannot contribute supported raw spectra."""


class ExtractionCleanupBlockedError(ProductRunnerError):
    """The extraction process tree may still own files under the task temp root."""


@dataclass(frozen=True)
class ApprovedPreExtractionRunContext:
    run_id: str
    timestamp: str
    selected_source_paths: tuple[Path, ...]
    output_parent: Path
    settings_snapshot: Mapping[str, object]
    source_fingerprints_before: tuple[SourceSnapshot, ...]
    temp_root: Path
    temp_root_identity: tuple[int, int]
    run_owned_source_copy_paths: tuple[Path, ...]
    protected_fingerprints_before: tuple[SourceSnapshot, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "settings_snapshot",
            MappingProxyType(dict(self.settings_snapshot)),
        )


@dataclass(frozen=True)
class VerifiedSourceCopyIdentity:
    source_id: str
    copy_path: Path
    sha256: str
    size_bytes: int
    device_id: int
    file_id: int


@dataclass(frozen=True)
class ReaderProcessCommand:
    run_id: str
    marker_id: str
    settings_snapshot: Mapping[str, object]
    source_copy: VerifiedSourceCopyIdentity
    snapshot_path: Path
    required_temp_bytes: int
    reader_attempt: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "settings_snapshot",
            MappingProxyType(dict(self.settings_snapshot)),
        )


@dataclass(frozen=True)
class ReaderSourceExtractionSummary:
    snapshot_path: Path
    source_id: str
    inventory_count: int
    result_count: int
    extracted_count: int
    rejected_count: int


def _context_to_payload(
    context: ApprovedPreExtractionRunContext,
) -> dict[str, object]:
    return {
        "run_id": context.run_id,
        "timestamp": context.timestamp,
        "selected_source_paths": [str(path) for path in context.selected_source_paths],
        "output_parent": str(context.output_parent),
        "settings_snapshot": dict(context.settings_snapshot),
        "source_fingerprints_before": [
            _source_snapshot_to_payload(item)
            for item in context.source_fingerprints_before
        ],
        "temp_root": str(context.temp_root),
        "temp_root_identity": list(context.temp_root_identity),
        "run_owned_source_copy_paths": [
            str(path) for path in context.run_owned_source_copy_paths
        ],
        "protected_fingerprints_before": [
            _source_snapshot_to_payload(item)
            for item in context.protected_fingerprints_before
        ],
    }


def _context_from_payload(
    payload: dict[str, object],
) -> ApprovedPreExtractionRunContext:
    expected_fields = {
        "run_id",
        "timestamp",
        "selected_source_paths",
        "output_parent",
        "settings_snapshot",
        "source_fingerprints_before",
        "temp_root",
        "temp_root_identity",
        "run_owned_source_copy_paths",
        "protected_fingerprints_before",
    }
    if not isinstance(payload, dict) or set(payload) != expected_fields:
        raise ProductRunnerError("提取前 context 字段无效")
    string_fields = ("run_id", "timestamp", "output_parent", "temp_root")
    if any(
        not isinstance(payload[field], str) or not payload[field]
        for field in string_fields
    ):
        raise ProductRunnerError("提取前 context 文本字段无效")
    if not isinstance(payload["settings_snapshot"], dict):
        raise ProductRunnerError("提取前 context settings 格式无效")
    if not _valid_identity_payload(payload["temp_root_identity"]):
        raise ProductRunnerError("提取前 context temp root 身份无效")
    for field in ("selected_source_paths", "run_owned_source_copy_paths"):
        values = payload[field]
        if not isinstance(values, list) or any(
            not isinstance(value, str) or not value for value in values
        ):
            raise ProductRunnerError("提取前 context 路径列表无效")
    for field in ("source_fingerprints_before", "protected_fingerprints_before"):
        if not isinstance(payload[field], list):
            raise ProductRunnerError("提取前 context fingerprint 列表无效")
    identity = payload["temp_root_identity"]
    return ApprovedPreExtractionRunContext(
        run_id=payload["run_id"],
        timestamp=payload["timestamp"],
        selected_source_paths=tuple(
            Path(path) for path in payload["selected_source_paths"]
        ),
        output_parent=Path(payload["output_parent"]),
        settings_snapshot=dict(payload["settings_snapshot"]),
        source_fingerprints_before=tuple(
            _source_snapshot_from_payload(item)
            for item in payload["source_fingerprints_before"]
        ),
        temp_root=Path(payload["temp_root"]),
        temp_root_identity=(identity[0], identity[1]),
        run_owned_source_copy_paths=tuple(
            Path(path) for path in payload["run_owned_source_copy_paths"]
        ),
        protected_fingerprints_before=tuple(
            _source_snapshot_from_payload(item)
            for item in payload["protected_fingerprints_before"]
        ),
    )


_READER_COMMAND_FIELDS = {
    "run_id",
    "marker_id",
    "source_id",
    "copy_path",
    "copy_sha256",
    "copy_size_bytes",
    "copy_device_id",
    "copy_file_id",
    "settings_snapshot",
    "snapshot_path",
    "required_temp_bytes",
    "reader_attempt",
}


def _reader_command_to_payload(
    command: ReaderProcessCommand,
) -> dict[str, object]:
    return {
        "run_id": command.run_id,
        "marker_id": command.marker_id,
        "source_id": command.source_copy.source_id,
        "copy_path": str(command.source_copy.copy_path),
        "copy_sha256": command.source_copy.sha256,
        "copy_size_bytes": command.source_copy.size_bytes,
        "copy_device_id": command.source_copy.device_id,
        "copy_file_id": command.source_copy.file_id,
        "settings_snapshot": dict(command.settings_snapshot),
        "snapshot_path": str(command.snapshot_path),
        "required_temp_bytes": command.required_temp_bytes,
        "reader_attempt": command.reader_attempt,
    }


def _reader_command_from_payload(
    payload: dict[str, object],
) -> ReaderProcessCommand:
    if not isinstance(payload, dict) or set(payload) != _READER_COMMAND_FIELDS:
        raise ProductRunnerError("谱图提取 reader manifest 字段无效")
    settings = payload["settings_snapshot"]
    copy_size = payload["copy_size_bytes"]
    copy_device_id = payload["copy_device_id"]
    copy_file_id = payload["copy_file_id"]
    required_bytes = payload["required_temp_bytes"]
    reader_attempt = payload["reader_attempt"]
    run_id = payload["run_id"]
    marker_id = payload["marker_id"]
    source_id = payload["source_id"]
    copy_path = payload["copy_path"]
    copy_sha256 = payload["copy_sha256"]
    snapshot_path = payload["snapshot_path"]
    if (
        not isinstance(settings, dict)
        or set(settings)
        not in (
            {"s1Limit", "steadyEmissionY"},
            {"s1Limit", "steadyEmissionY", "allowMissingS1"},
        )
        or isinstance(copy_size, bool)
        or not isinstance(copy_size, int)
        or copy_size < 0
        or isinstance(copy_device_id, bool)
        or not isinstance(copy_device_id, int)
        or copy_device_id < 0
        or isinstance(copy_file_id, bool)
        or not isinstance(copy_file_id, int)
        or copy_file_id <= 0
        or isinstance(required_bytes, bool)
        or not isinstance(required_bytes, int)
        or required_bytes < required_temp_bytes(copy_size)
        or isinstance(reader_attempt, bool)
        or not isinstance(reader_attempt, int)
        or reader_attempt not in {1, 2}
        or not isinstance(run_id, str)
        or not run_id
        or not isinstance(marker_id, str)
        or not marker_id
        or not isinstance(source_id, str)
        or len(source_id) != 5
        or not source_id.startswith("S")
        or not source_id[1:].isdigit()
        or int(source_id[1:]) < 1
        or not isinstance(copy_path, str)
        or not copy_path
        or not isinstance(snapshot_path, str)
        or not snapshot_path
        or not isinstance(copy_sha256, str)
        or len(copy_sha256) != 64
        or any(
            character not in "0123456789abcdefABCDEF"
            for character in copy_sha256
        )
    ):
        raise ProductRunnerError("谱图提取 reader manifest 类型无效")
    _confirmed_s1_limit(settings)
    _confirmed_allow_missing_s1(settings)
    _confirmed_steady_emission_y(settings)
    return ReaderProcessCommand(
        run_id=run_id,
        marker_id=marker_id,
        settings_snapshot=dict(settings),
        source_copy=VerifiedSourceCopyIdentity(
            source_id=source_id,
            copy_path=Path(copy_path),
            sha256=copy_sha256.lower(),
            size_bytes=copy_size,
            device_id=copy_device_id,
            file_id=copy_file_id,
        ),
        snapshot_path=Path(snapshot_path),
        required_temp_bytes=required_bytes,
        reader_attempt=reader_attempt,
    )


_READER_SUMMARY_FIELDS = {
    "snapshot_path",
    "source_id",
    "inventory_count",
    "result_count",
    "extracted_count",
    "rejected_count",
}


def _reader_summary_to_payload(
    summary: ReaderSourceExtractionSummary,
) -> dict[str, object]:
    return {
        "snapshot_path": str(summary.snapshot_path),
        "source_id": summary.source_id,
        "inventory_count": summary.inventory_count,
        "result_count": summary.result_count,
        "extracted_count": summary.extracted_count,
        "rejected_count": summary.rejected_count,
    }


def _reader_summary_from_payload(
    payload: dict[str, object],
) -> ReaderSourceExtractionSummary:
    if not isinstance(payload, dict) or set(payload) != _READER_SUMMARY_FIELDS:
        raise ProductRunnerError("谱图提取 reader summary 字段无效")
    snapshot_path = payload["snapshot_path"]
    source_id = payload["source_id"]
    counts = tuple(
        payload[name]
        for name in (
            "inventory_count",
            "result_count",
            "extracted_count",
            "rejected_count",
        )
    )
    if (
        not isinstance(snapshot_path, str)
        or not snapshot_path
        or not isinstance(source_id, str)
        or len(source_id) != 5
        or not source_id.startswith("S")
        or not source_id[1:].isdigit()
        or int(source_id[1:]) < 1
        or any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in counts
        )
    ):
        raise ProductRunnerError("谱图提取 reader summary 计数无效")
    summary = ReaderSourceExtractionSummary(
        snapshot_path=Path(snapshot_path),
        source_id=source_id,
        inventory_count=counts[0],
        result_count=counts[1],
        extracted_count=counts[2],
        rejected_count=counts[3],
    )
    if summary.inventory_count != summary.result_count or summary.result_count != (
        summary.extracted_count + summary.rejected_count
    ):
        raise ProductRunnerError("谱图提取 reader summary 计数不闭合")
    return summary


def _source_snapshot_to_payload(snapshot: SourceSnapshot) -> dict[str, object]:
    canonical_path = _canonical_source_snapshot_path(snapshot)
    if snapshot.device_id is None or snapshot.file_id is None:
        raise ProductRunnerError("提取前 fingerprint 文件身份无效")
    return {
        "path": str(snapshot.path),
        "sha256": snapshot.sha256,
        "size_bytes": snapshot.size_bytes,
        "mtime_ns": snapshot.mtime_ns,
        "canonical_path": canonical_path,
        "device_id": snapshot.device_id,
        "file_id": snapshot.file_id,
    }


def _source_snapshot_from_payload(payload: dict[str, object]) -> SourceSnapshot:
    if not isinstance(payload, dict) or set(payload) != {
        "path",
        "sha256",
        "size_bytes",
        "mtime_ns",
        "canonical_path",
        "device_id",
        "file_id",
    }:
        raise ProductRunnerError("提取前 fingerprint 字段无效")
    path = payload["path"]
    sha256 = payload["sha256"]
    size_bytes = payload["size_bytes"]
    mtime_ns = payload["mtime_ns"]
    canonical_path = payload["canonical_path"]
    device_id = payload["device_id"]
    file_id = payload["file_id"]
    if not isinstance(path, str) or not path:
        raise ProductRunnerError("提取前 fingerprint 路径无效")
    if not isinstance(canonical_path, str) or not canonical_path:
        raise ProductRunnerError("提取前 fingerprint canonical 路径无效")
    if (
        not isinstance(sha256, str)
        or len(sha256) != 64
        or any(
            character not in "0123456789abcdefABCDEF" for character in sha256
        )
    ):
        raise ProductRunnerError("提取前 fingerprint SHA-256 无效")
    if (
        isinstance(size_bytes, bool)
        or not isinstance(size_bytes, int)
        or size_bytes < 0
        or isinstance(mtime_ns, bool)
        or not isinstance(mtime_ns, int)
        or mtime_ns < 0
        or isinstance(device_id, bool)
        or not isinstance(device_id, int)
        or device_id < 0
        or isinstance(file_id, bool)
        or not isinstance(file_id, int)
        or file_id <= 0
    ):
        raise ProductRunnerError("提取前 fingerprint 数值字段无效")
    return SourceSnapshot(
        path=Path(path),
        sha256=sha256,
        size_bytes=size_bytes,
        mtime_ns=mtime_ns,
        canonical_path=Path(canonical_path),
        device_id=device_id,
        file_id=file_id,
    )


def _canonical_source_snapshot_path(snapshot: SourceSnapshot) -> str:
    path = snapshot.canonical_path
    if path is None or not Path(path).is_absolute():
        raise ProductRunnerError("source fingerprint canonical path is invalid")
    return os.path.normcase(str(Path(path)))


def _valid_identity_payload(value) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 2
        and all(
            isinstance(part, int) and not isinstance(part, bool)
            for part in value
        )
        and value[1] != 0
    )


def _confirmed_s1_limit(settings_snapshot: Mapping[str, object]) -> int | float:
    value = settings_snapshot.get("s1Limit", settings_snapshot.get("s1_limit"))
    if not is_finite_real_number(value) or value <= 0:
        raise ProductRunnerError("已确认的 S1 limit 无效")
    return value


def _confirmed_steady_emission_y(settings_snapshot: Mapping[str, object]) -> str:
    value = settings_snapshot.get(
        "steadyEmissionY",
        settings_snapshot.get("steady_emission_y"),
    )
    if value not in {"S1c", "S1c/R1c"}:
        raise ProductRunnerError("已确认的稳态发射 Y 无效")
    return str(value)


def _confirmed_allow_missing_s1(settings_snapshot: Mapping[str, object]) -> bool:
    value = settings_snapshot.get(
        "allowMissingS1",
        settings_snapshot.get("allow_missing_s1", False),
    )
    if not isinstance(value, bool):
        raise ProductRunnerError("已确认的缺少 S1 处理选项无效")
    return value
