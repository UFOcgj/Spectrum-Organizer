from __future__ import annotations

import re
from pathlib import Path
from typing import NamedTuple


class CleanEnvironmentEvidence(NamedTuple):
    runtime_text: str
    worker_open_targets: tuple[str, ...]
    created_paths: tuple[str, ...]
    preexisting_user_paths: tuple[str, ...]
    final_origin_process_count: int
    process_returncode_after_shutdown: int | None = 0
    shutdown: str = "normal"


def evaluate_clean_environment(
    evidence: CleanEnvironmentEvidence,
    *,
    workspace_root: Path,
    original_source_paths: tuple[str | Path, ...],
) -> tuple[str, ...]:
    failures: list[str] = []
    runtime = _normalize_text(evidence.runtime_text)
    workspace = _normalize_text(_resolved_path_text(workspace_root))
    if workspace and _references_normalized_path(runtime, workspace):
        failures.append("runtime references workspace path")
    if _references_validation(runtime):
        failures.append("runtime references validation path")
    if _any_intersection(evidence.worker_open_targets, original_source_paths):
        failures.append("worker open target is an original source path")
    if _any_intersection(evidence.created_paths, evidence.preexisting_user_paths):
        failures.append("created path collides with preexisting user file")
    if evidence.final_origin_process_count != 0:
        failures.append("final Origin process count is not zero")
    bad_shutdown = evidence.shutdown in {"exited_early", "exited_after_startup_dirs", "kill_after_timeout"}
    bad_normal_returncode = evidence.shutdown == "normal" and evidence.process_returncode_after_shutdown not in (0, None)
    if bad_shutdown or bad_normal_returncode:
        failures.append("packaged app did not shut down cleanly")
    return tuple(failures)


def _references_validation(runtime: str) -> bool:
    path_reference = re.search(r"(^|[/\\'\"\s])validation(?=[/\\'\"),;:]|$)", runtime)
    module_reference = re.search(r"(^|[\s'\"(=])validation\.(?:task|test|[a-z]+_[a-z0-9_]*)", runtime)
    return bool(path_reference or module_reference)


def _references_normalized_path(runtime: str, normalized_path: str) -> bool:
    start = 0
    while True:
        index = runtime.find(normalized_path, start)
        if index == -1:
            return False
        before_ok = index == 0 or runtime[index - 1] in " \t\r\n'\"([{<=;,"
        after_index = index + len(normalized_path)
        after_ok = after_index == len(runtime) or runtime[after_index] in "/\\ \t\r\n'\")]}>=;,:"
        if before_ok and after_ok:
            return True
        start = index + 1


def _any_intersection(left: tuple[str | Path, ...], right: tuple[str | Path, ...]) -> bool:
    return bool({_path_key(item) for item in left} & {_path_key(item) for item in right})


def _path_key(value: str | Path) -> str:
    return _normalize_text(_resolved_path_text(value)).rstrip("/")


def _resolved_path_text(value: str | Path) -> str:
    return str(Path(value).expanduser().resolve(strict=False))


def _normalize_text(value: str) -> str:
    return value.replace("\\", "/").casefold()