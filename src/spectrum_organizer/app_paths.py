from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path


APP_NAME = "Spectrum Organizer"
APP_MARKER = ".spectrum_organizer_owner.json"
APP_MARKER_PAYLOAD = {"app": APP_NAME, "kind": "app-state"}


class AppPathError(RuntimeError):
    pass


@dataclass(frozen=True)
class AppPaths:
    root: Path
    data: Path
    backups: Path
    temp: Path
    logs: Path
    settings_file: Path


def ensure_app_paths(local_appdata: str | os.PathLike[str] | None = None) -> AppPaths:
    base = Path(local_appdata) if local_appdata is not None else _local_appdata_from_env()
    root = base / APP_NAME
    data = root / "data"
    paths = AppPaths(
        root=root,
        data=data,
        backups=data / "backups",
        temp=root / "temp",
        logs=root / "logs",
        settings_file=root / "settings.json",
    )
    for directory in (paths.root, paths.data, paths.backups, paths.temp, paths.logs):
        _ensure_owned_dir(directory)
    return paths


def _local_appdata_from_env() -> Path:
    value = os.environ.get("LOCALAPPDATA")
    if not value:
        raise AppPathError("LOCALAPPDATA is required; no fallback app-state root is allowed")
    return Path(value)


def _ensure_owned_dir(path: Path) -> None:
    _ensure_dir(path)
    _ensure_marker(path)


def _ensure_dir(path: Path) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise AppPathError(f"Could not create app-state directory {path}: {exc}") from exc
    if not path.is_dir():
        raise AppPathError(f"App-state path is not a directory: {path}")


def _ensure_marker(path: Path) -> None:
    marker = path / APP_MARKER
    if marker.exists():
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AppPathError(f"Invalid ownership marker at {marker}: {exc}") from exc
        if payload != APP_MARKER_PAYLOAD:
            raise AppPathError(f"App-state ownership marker mismatch at {marker}")
        return
    try:
        marker.write_text(json.dumps(APP_MARKER_PAYLOAD, sort_keys=True), encoding="utf-8")
    except OSError as exc:
        raise AppPathError(f"Could not write ownership marker at {marker}: {exc}") from exc
