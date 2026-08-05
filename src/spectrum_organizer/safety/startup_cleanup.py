from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from spectrum_organizer.app_paths import AppPaths, ensure_app_paths
from spectrum_organizer.safety.identity_paths import lexical_path_exists
from spectrum_organizer.safety.owned_paths import (
    CleanupFailedError,
    CleanupRefusedError,
    OWNERSHIP_ANCHOR_KEY,
    OWNERSHIP_ANCHOR_SUFFIX,
    cleanup_owned_temp_root,
    run_lease_is_held,
)
from spectrum_organizer.settings import Notice, Settings, SettingsStore
from spectrum_organizer.single_instance import InstanceResult, SingleInstance


TEMP_RUN_MARKER = ".spectrum_organizer_temp_run.json"


@dataclass(frozen=True)
class CleanupResult:
    deleted: list[Path]
    retained: list[Path]
    warning_message: str | None


@dataclass(frozen=True)
class StartupResult:
    instance: InstanceResult
    paths: AppPaths | None
    settings: Settings | None
    settings_store: SettingsStore | None
    notices: list[Notice]
    warnings: str
    activation_request_probe: Callable[..., bool] | None = None


def startup(instance_backend, local_appdata=None) -> StartupResult:
    instance = SingleInstance(instance_backend).enter()
    if instance.should_exit:
        return StartupResult(
            instance=instance,
            paths=None,
            settings=None,
            settings_store=None,
            notices=[],
            warnings="",
            activation_request_probe=None,
        )

    paths = ensure_app_paths(local_appdata=local_appdata)
    cleanup_result = cleanup_temp_runs(paths.temp)
    settings_store = SettingsStore(paths.settings_file)
    settings, notices = settings_store.load()
    if cleanup_result.warning_message:
        notices.append(Notice(severity="warning", message=cleanup_result.warning_message))
    return StartupResult(
        instance=instance,
        paths=paths,
        settings=settings,
        settings_store=settings_store,
        notices=notices,
        warnings="",
        activation_request_probe=getattr(
            instance_backend,
            "wait_for_activation_request",
            None,
        ),
    )


def cleanup_temp_runs(temp_root: Path) -> CleanupResult:
    temp_root = Path(temp_root)
    deleted: list[Path] = []
    retained: list[Path] = []
    delete_failures: list[tuple[Path, Exception]] = []
    if not lexical_path_exists(temp_root):
        return CleanupResult(deleted=deleted, retained=retained, warning_message=None)
    if temp_root.is_symlink() or not temp_root.is_dir():
        return CleanupResult(
            deleted=deleted,
            retained=[temp_root],
            warning_message=f"临时目录不是可安全检查的真实目录，已保留：{temp_root}",
        )

    for child in sorted(temp_root.iterdir(), key=lambda path: path.name):
        if (
            child.name == ".spectrum_organizer_owner.json"
            or child.name.endswith(OWNERSHIP_ANCHOR_SUFFIX)
            or child.name == OWNERSHIP_ANCHOR_KEY
        ):
            continue
        if child.is_dir():
            if run_lease_is_held(child):
                retained.append(child)
                continue
            try:
                cleanup_owned_temp_root(child)
            except CleanupFailedError as exc:
                retained.append(child)
                delete_failures.append((child, exc))
            except CleanupRefusedError:
                retained.append(child)
            except OSError as exc:
                retained.append(child)
                delete_failures.append((child, exc))
            else:
                deleted.append(child)
        else:
            retained.append(child)

    warning_message = None
    if delete_failures:
        details = "\n".join(f"- {path}：{error}" for path, error in delete_failures)
        warning_message = f"无法清理本程序遗留的临时目录，已保留并将在下次启动时重试：\n{details}"
    return CleanupResult(deleted=deleted, retained=retained, warning_message=warning_message)
