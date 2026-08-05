from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import uuid


@dataclass(frozen=True)
class Notice:
    severity: str
    message: str


@dataclass
class Settings:
    lastOutputParent: str = ""
    s1Limit: int = 2000000
    steadyEmissionY: str = "S1c"
    allowMissingS1: bool = False


_ALLOWED_STEADY_EMISSION_Y = {"S1c", "S1c/R1c"}


def _is_valid_settings_payload(data) -> bool:
    return (
        isinstance(data, dict)
        and set(data) in (
            {"lastOutputParent", "s1Limit", "steadyEmissionY"},
            {"lastOutputParent", "s1Limit", "steadyEmissionY", "allowMissingS1"},
        )
        and isinstance(data["lastOutputParent"], str)
        and isinstance(data["s1Limit"], int)
        and not isinstance(data["s1Limit"], bool)
        and data["s1Limit"] > 0
        and isinstance(data.get("allowMissingS1", False), bool)
        and _is_valid_preflight_values(
            data["s1Limit"],
            data["steadyEmissionY"],
            data.get("allowMissingS1", False),
        )
    )


class SettingsStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.settings = Settings()
        self._damaged_file_pending = False
        self._damaged_file_identity = None

    def load(self) -> tuple[Settings, list[Notice]]:
        if not self.path.exists():
            self._clear_damage_state()
            self.settings = Settings()
            return self.settings, []
        before = _settings_file_identity(self.path)
        try:
            raw_bytes = self.path.read_bytes()
            raw = raw_bytes.decode("utf-8")
        except (OSError, UnicodeError):
            self._mark_damaged(before)
            return self.settings, [_reset_notice()]
        after = _settings_file_identity(self.path, content=raw_bytes)
        damaged_identity = before if before != after else after
        try:
            data = json.loads(raw)
            if not _is_valid_settings_payload(data):
                raise ValueError("settings JSON does not match the supported schema")
            self.settings = Settings(
                lastOutputParent=data["lastOutputParent"],
                s1Limit=data["s1Limit"],
                steadyEmissionY=data["steadyEmissionY"],
                allowMissingS1=data.get("allowMissingS1", False),
            )
            self._clear_damage_state()
            return self.settings, []
        except (ValueError, TypeError):
            self._mark_damaged(damaged_identity)
            return self.settings, [_reset_notice()]

    def set_last_output_parent(self, value: str) -> list[Notice]:
        _, notices = self.load()
        if notices:
            return notices
        self.settings.lastOutputParent = value
        return self.save()

    def set_preflight_settings(
        self,
        s1_limit: int,
        steady_emission_y: str,
        allow_missing_s1: bool = False,
    ) -> list[Notice]:
        if not _is_valid_preflight_values(s1_limit, steady_emission_y, allow_missing_s1):
            raise ValueError("Invalid preflight settings")
        _, notices = self.load()
        if notices:
            return notices
        self.settings.s1Limit = s1_limit
        self.settings.steadyEmissionY = steady_emission_y
        self.settings.allowMissingS1 = allow_missing_s1
        return self.save()

    def save(self) -> list[Notice]:
        pending = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with pending.open("w", encoding="utf-8") as file:
                json.dump(asdict(self.settings), file, indent=2, sort_keys=True)
                file.flush()
                os.fsync(file.fileno())
            os.replace(pending, self.path)
            return []
        except OSError as exc:
            try:
                pending.unlink(missing_ok=True)
            except OSError:
                pass
            return [Notice(severity="warning", message=f"无法保存设置：{exc}")]

    def discard_damaged_file(self) -> list[Notice]:
        if not self._damaged_file_pending:
            return []
        if not self.path.exists():
            self._clear_damage_state()
            return []
        current_identity = _settings_file_identity(self.path)
        if not _same_settings_file(self._damaged_file_identity, current_identity):
            self._clear_damage_state()
            return [Notice(severity="warning", message="设置文件在确认期间已变化，程序已保留当前文件。")]
        quarantine = self.path.with_name(f".{self.path.name}.discard-{uuid.uuid4().hex}")
        try:
            self.path.replace(quarantine)
        except OSError as exc:
            return [Notice(severity="warning", message=f"无法删除损坏的设置文件：{exc}")]
        moved_identity = _settings_file_identity(quarantine)
        if not _same_settings_file(self._damaged_file_identity, moved_identity):
            try:
                if self.path.exists():
                    raise FileExistsError(str(self.path))
                quarantine.replace(self.path)
                message = "设置文件在确认期间已变化，程序已保留当前文件。"
            except OSError:
                message = f"设置文件在确认期间已变化，程序已保留替换文件：{quarantine}"
            self._clear_damage_state()
            return [Notice(severity="warning", message=message)]
        try:
            quarantine.unlink(missing_ok=True)
        except OSError as exc:
            return [Notice(severity="warning", message=f"无法删除损坏的设置文件：{exc}")]
        self._clear_damage_state()
        return []

    @property
    def damaged_file_pending(self) -> bool:
        return self._damaged_file_pending

    def _mark_damaged(self, identity) -> None:
        self._damaged_file_pending = True
        self._damaged_file_identity = identity
        self.settings = Settings()

    def _clear_damage_state(self) -> None:
        self._damaged_file_pending = False
        self._damaged_file_identity = None


def _is_valid_preflight_values(
    s1_limit: int,
    steady_emission_y: str,
    allow_missing_s1: bool = False,
) -> bool:
    return (
        isinstance(s1_limit, int)
        and not isinstance(s1_limit, bool)
        and s1_limit > 0
        and steady_emission_y in _ALLOWED_STEADY_EMISSION_Y
        and isinstance(allow_missing_s1, bool)
    )


def _reset_notice() -> Notice:
    return Notice(
        severity="conspicuous",
        message="设置文件损坏、不可读或版本不兼容。程序将恢复默认设置，并在你确认此提示后删除损坏文件。",
    )


def _settings_file_identity(path: Path, *, content: bytes | None = None):
    try:
        stat = Path(path).stat()
    except OSError:
        return None
    digest = None
    try:
        payload = Path(path).read_bytes() if content is None else content
        digest = hashlib.sha256(payload).hexdigest()
    except OSError:
        pass
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, digest)


def _same_settings_file(expected, current) -> bool:
    if expected is None or current is None:
        return expected == current
    if expected[-1] is None:
        return expected[:-1] == current[:-1]
    return expected == current
