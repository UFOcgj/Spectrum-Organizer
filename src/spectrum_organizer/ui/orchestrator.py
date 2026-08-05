from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import inspect
from pathlib import Path, PureWindowsPath
import posixpath
from types import MappingProxyType
from typing import Mapping, Protocol


class WorkflowMode(Enum):
    BOOK_ONLY = "book_only"


@dataclass(frozen=True)
class StartupWorkflows:
    enabled_workflow: WorkflowMode
    book_only_enabled: bool
    graph_generation_enabled: bool
    graph_generation_runnable: bool
    prompt_for_otpu: bool


@dataclass(frozen=True)
class SourceSelectionResult:
    ok: bool
    source_paths: tuple[str, ...] = ()
    duplicate_paths: tuple[str, ...] = ()
    reason: str | None = None


@dataclass(frozen=True)
class ApprovedPreExtractionInputs:
    selected_source_paths: tuple[str, ...]
    output_parent: str
    settings_snapshot: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "settings_snapshot",
            MappingProxyType(dict(self.settings_snapshot)),
        )


class SettingsWriter(Protocol):
    def set_last_output_parent(self, value: str): ...
    def set_preflight_settings(
        self,
        s1_limit: int,
        steady_emission_y: str,
        allow_missing_s1: bool = False,
    ): ...


@dataclass
class BookOnlyOrchestrator:
    settings_store: SettingsWriter
    task_cache: dict = field(default_factory=dict)
    last_failure: str | None = None
    cancelled: bool = False

    def select_sources(self, paths: list[str]) -> SourceSelectionResult:
        accepted: list[str] = []
        seen: set[str] = set()
        seen_file_ids: set[tuple[int, int]] = set()
        duplicates: list[str] = []
        for path in paths:
            if PureWindowsPath(path).suffix.lower() not in {".opj", ".opju"}:
                return SourceSelectionResult(ok=False, reason="unrecognized_source_file")
            key = _source_path_key(path)
            file_id = _source_file_id(path)
            if key in seen or (file_id is not None and file_id in seen_file_ids):
                duplicates.append(path)
                continue
            seen.add(key)
            if file_id is not None:
                seen_file_ids.add(file_id)
            accepted.append(path)
        if not accepted:
            return SourceSelectionResult(ok=False, reason="no_source_files")
        result = SourceSelectionResult(ok=True, source_paths=tuple(accepted), duplicate_paths=tuple(duplicates))
        self.task_cache["selected_source_paths"] = result.source_paths
        self.task_cache["ignored_duplicate_input_paths"] = result.duplicate_paths
        return result

    def select_output_parent(self, path: str):
        notices = self.settings_store.set_last_output_parent(path)
        self.task_cache["output_parent"] = path
        return notices

    def confirm_preflight_settings(
        self,
        *,
        s1_limit: int,
        steady_emission_y: str,
        allow_missing_s1: bool = False,
    ):
        notices = _write_preflight_settings(
            self.settings_store,
            s1_limit,
            steady_emission_y,
            allow_missing_s1,
        )
        self.task_cache["settings_snapshot"] = {
            "s1Limit": s1_limit,
            "steadyEmissionY": steady_emission_y,
            "allowMissingS1": allow_missing_s1,
        }
        return notices

    def approved_pre_extraction_inputs(self) -> ApprovedPreExtractionInputs:
        if "selected_source_paths" not in self.task_cache:
            raise RuntimeError("approved pre-extraction inputs require selected source paths")
        if "output_parent" not in self.task_cache:
            raise RuntimeError("approved pre-extraction inputs require output parent")
        if "settings_snapshot" not in self.task_cache:
            raise RuntimeError("approved pre-extraction inputs require confirmed preflight settings")
        return ApprovedPreExtractionInputs(
            selected_source_paths=tuple(self.task_cache["selected_source_paths"]),
            output_parent=str(self.task_cache["output_parent"]),
            settings_snapshot=dict(self.task_cache["settings_snapshot"]),
        )

    def cancel_after_preferences(self) -> None:
        self.cancelled = True

    def fail_after_preferences(self, message: str) -> None:
        self.last_failure = message

    def start_new_task(self) -> None:
        self.task_cache.clear()
        self.cancelled = False
        self.last_failure = None


def _source_path_key(path: str) -> str:
    as_windows = str(PureWindowsPath(path)).replace("\\", "/")
    return posixpath.normpath(as_windows).casefold()


def _write_preflight_settings(
    settings_store: SettingsWriter,
    s1_limit: int,
    steady_emission_y: str,
    allow_missing_s1: bool,
):
    writer = settings_store.set_preflight_settings
    try:
        signature = inspect.signature(writer)
    except (TypeError, ValueError):
        return writer(s1_limit, steady_emission_y, allow_missing_s1)

    try:
        signature.bind(s1_limit, steady_emission_y, allow_missing_s1)
    except TypeError:
        if allow_missing_s1:
            raise TypeError("settings writer cannot persist the approved missing-S1 option")
        signature.bind(s1_limit, steady_emission_y)
        return writer(s1_limit, steady_emission_y)
    return writer(s1_limit, steady_emission_y, allow_missing_s1)


def _source_file_id(path: str) -> tuple[int, int] | None:
    try:
        stat = Path(path).stat()
    except OSError:
        return None
    if stat.st_ino == 0:
        return None
    return (stat.st_dev, stat.st_ino)


def build_startup_workflows() -> StartupWorkflows:
    return StartupWorkflows(
        enabled_workflow=WorkflowMode.BOOK_ONLY,
        book_only_enabled=True,
        graph_generation_enabled=False,
        graph_generation_runnable=False,
        prompt_for_otpu=False,
    )
