from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import traceback


def _write_startup_failure_log(
    exc: BaseException,
    *,
    timestamp: str | None = None,
) -> Path | None:
    local_appdata = os.environ.get("LOCALAPPDATA")
    if not local_appdata:
        return None
    base = Path(local_appdata) / "Spectrum Organizer" / "logs"
    timestamp = timestamp or datetime.now().strftime(
        "%Y%m%d_%H%M%S"
    )
    traceback_text = "".join(
        traceback.format_exception(
            type(exc),
            exc,
            exc.__traceback__,
        )
    )
    text = (
        "程序启动失败\n\n"
        "原始异常：\n"
        f"{traceback_text}"
    )
    try:
        base.mkdir(parents=True, exist_ok=True)
        suffix = 0
        while True:
            suffix_text = (
                ""
                if suffix == 0
                else f"_{suffix:03d}"
            )
            path = base / (
                "Packaged_Startup_Exception_"
                f"{timestamp}{suffix_text}.txt"
            )
            try:
                with path.open(
                    "x",
                    encoding="utf-8",
                ) as stream:
                    stream.write(text)
                return path
            except FileExistsError:
                suffix += 1
    except OSError:
        return None


def _show_startup_failure_dialog(
    exc: BaseException,
    log_path: Path | None,
) -> None:
    exception_text = (
        f"{type(exc).__name__}: {exc}"
    )
    if log_path is None:
        log_text = "失败日志未能写入。"
    else:
        log_text = f"失败日志：{log_path}"
    _native_message_box(
        "程序启动失败",
        "程序无法启动。\n\n"
        f"{exception_text}\n\n"
        f"{log_text}",
    )


def _native_message_box(title: str, message: str) -> None:
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(
            None,
            message,
            title,
            0x00000010 | 0x00010000 | 0x00040000,
        )
    except (AttributeError, OSError):
        pass


if __name__ == "__main__":
    try:
        from spectrum_organizer.__main__ import main

        raise SystemExit(main())
    except Exception as exc:
        log_path = _write_startup_failure_log(exc)
        _show_startup_failure_dialog(exc, log_path)
        raise SystemExit(1)
