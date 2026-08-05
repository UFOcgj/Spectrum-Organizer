from __future__ import annotations

import ctypes
from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
import time

from spectrum_organizer.workflow.interaction import DialogRequest


ORIGIN_VISIBLE_USER_BLOCKED = "origin_visible_user_blocked"
ORIGIN_CONFIRM_HIDDEN = "origin_confirm_hidden"

def _start_time_ns_expression(process_ref: str) -> str:
    return (
        f"[int64](({process_ref}.StartTime.ToUniversalTime().Ticks "
        "- 621355968000000000) * 100)"
    )


_PIPELINE_START_TIME_NS = _start_time_ns_expression("$_")
_PROCESS_START_TIME_NS = _start_time_ns_expression("$p")

_WINDOW_VISIBILITY_PREAMBLE = (
    "Add-Type -MemberDefinition "
    "'[DllImport(\"user32.dll\")] public static extern bool IsWindowVisible(IntPtr hWnd);' "
    "-Name NativeWindow -Namespace SpectrumOrganizer; "
)
_PROCESS_IS_VISIBLE = (
    "[SpectrumOrganizer.NativeWindow]::IsWindowVisible([IntPtr]$p.MainWindowHandle)"
)


class ProcessBoundaryError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    start_time_ns: int


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    start_time_ns: int
    visible: bool
    taskbar_visible: bool
    program_owned: bool

    @property
    def identity(self) -> ProcessIdentity:
        return ProcessIdentity(pid=self.pid, start_time_ns=self.start_time_ns)


@dataclass(frozen=True)
class BoundaryOutcome:
    can_continue: bool
    dialog: DialogRequest | None = None
    closed_pids: tuple[int, ...] = ()
    forced_pids: tuple[int, ...] = ()
    warnings: tuple[str, ...] = ()


_ORIGIN_PROCESS_QUERY = f"""
$ErrorActionPreference = 'Stop'
$items = Get-Process | Where-Object {{ $_.ProcessName -like 'Origin*' }} | ForEach-Object {{
    [pscustomobject]@{{
        name = $_.ProcessName
        pid = [int]$_.Id
        start_time_ns = {_PIPELINE_START_TIME_NS}
        main_window_handle = [int64]$_.MainWindowHandle
    }}
}}
@($items) | ConvertTo-Json -Compress
"""


class WindowsOriginProcessProbe:
    def __init__(self, *, command_runner=None, window_visibility=None, timeout: float = 5):
        self.command_runner = command_runner
        self.window_visibility = window_visibility or _best_effort_window_visibility
        self.timeout = timeout

    def __call__(self, *, timeout: float | None = None) -> tuple[ProcessInfo, ...]:
        effective_timeout = self.timeout if timeout is None else timeout
        try:
            if self.command_runner is None:
                completed = _run_powershell(_ORIGIN_PROCESS_QUERY, timeout=effective_timeout)
            else:
                completed = self.command_runner(_ORIGIN_PROCESS_QUERY, timeout=effective_timeout)
        except subprocess.TimeoutExpired as exc:
            raise ProcessBoundaryError("Origin process probe timed out") from exc
        if completed.returncode != 0:
            message = completed.stderr.strip() or "process query failed"
            raise ProcessBoundaryError(f"Origin process probe failed: {message}")
        return _parse_origin_process_json(completed.stdout, self.window_visibility)


class WindowsOriginProcessController:
    def __init__(self, *, command_runner=None, process_probe=None):
        self.command_runner = command_runner or _run_powershell
        self.process_probe = process_probe or default_origin_process_probe

    def current_process(self, pid: int, *, timeout: float = 5.0) -> ProcessInfo | None:
        try:
            processes = self.process_probe(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise ProcessBoundaryError("Origin process controller probe timed out") from exc
        for process in processes:
            if process.pid == pid:
                return process
        return None

    def graceful_close(self, identity: ProcessIdentity, *, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        self._require_same_hidden_process(
            identity,
            action="gracefully close",
            timeout=_remaining_controller_timeout(deadline, "gracefully close"),
        )
        pid = int(identity.pid)
        start_time_ns = int(identity.start_time_ns)
        command = (
            "$ErrorActionPreference = 'Stop'; "
            f"{_WINDOW_VISIBILITY_PREAMBLE}"
            f"$p = Get-Process -Id {pid} -ErrorAction SilentlyContinue; "
            "if ($null -eq $p) { 'missing'; exit 0 }; "
            f"$startNs = {_PROCESS_START_TIME_NS}; "
            f"if ($startNs -ne {start_time_ns}) {{ 'identity_changed'; exit 0 }}; "
            f"if ({_PROCESS_IS_VISIBLE}) {{ 'visible'; exit 0 }}; "
            "if ($p.CloseMainWindow()) { "
            "Start-Sleep -Milliseconds 500; "
            "if ($p.HasExited) { 'closed' } else { 'running' } "
            "} else { 'no_window' }"
        )
        return _controller_command_succeeded(
            _run_controller_command(
                self.command_runner,
                command,
                timeout=_remaining_controller_timeout(deadline, "gracefully close"),
                operation="gracefully close",
            ),
            "gracefully close",
            {"closed", "missing"},
        )

    def force_close(self, identity: ProcessIdentity, *, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        self._require_same_hidden_process(
            identity,
            action="force close",
            timeout=_remaining_controller_timeout(deadline, "force close"),
        )
        pid = int(identity.pid)
        start_time_ns = int(identity.start_time_ns)
        command = (
            "$ErrorActionPreference = 'Stop'; "
            f"{_WINDOW_VISIBILITY_PREAMBLE}"
            f"$p = Get-Process -Id {pid} -ErrorAction SilentlyContinue; "
            "if ($null -eq $p) { 'missing'; exit 0 }; "
            f"$startNs = {_PROCESS_START_TIME_NS}; "
            f"if ($startNs -ne {start_time_ns}) {{ 'identity_changed'; exit 0 }}; "
            f"if ({_PROCESS_IS_VISIBLE}) {{ 'visible'; exit 0 }}; "
            "Stop-Process -InputObject $p -Force; "
            "Start-Sleep -Milliseconds 300; "
            "if ($p.HasExited) { 'closed' } else { 'running' }"
        )
        return _controller_command_succeeded(
            _run_controller_command(
                self.command_runner,
                command,
                timeout=_remaining_controller_timeout(deadline, "force close"),
                operation="force close",
            ),
            "force close",
            {"closed", "missing"},
        )

    def _require_same_hidden_process(
        self,
        identity: ProcessIdentity,
        *,
        action: str,
        timeout: float,
    ) -> None:
        current = self.current_process(identity.pid, timeout=timeout)
        if current is None:
            return
        if current.start_time_ns != identity.start_time_ns:
            raise ProcessBoundaryError(f"Origin process identity changed before {action}: {identity.pid}")
        if current.visible or current.taskbar_visible:
            raise ProcessBoundaryError(f"Origin process became visible before {action}: {identity.pid}")

    def is_running(self, identity: ProcessIdentity, *, timeout: float = 5.0) -> bool:
        current = self.current_process(identity.pid, timeout=timeout)
        return current is not None and current.start_time_ns == identity.start_time_ns

    def close_program_owned(self, identity: ProcessIdentity, *, timeout: float = 5.0) -> None:
        if not self.force_close(identity, timeout=timeout):
            raise ProcessBoundaryError(
                f"Program-owned Origin process survived cleanup: {identity.pid}"
            )


def default_origin_process_probe(*, timeout: float = 5) -> tuple[ProcessInfo, ...]:
    return WindowsOriginProcessProbe(timeout=timeout)()


def classify_process(process: ProcessInfo) -> str:
    if process.program_owned:
        return "program_owned"
    if process.visible or process.taskbar_visible:
        return "visible_user"
    return "preexisting_hidden"


def preflight_origin_boundary(
    processes: list[ProcessInfo],
    controller,
    hidden_confirmation: bool | set[ProcessIdentity] | frozenset[ProcessIdentity] = False,
) -> BoundaryOutcome:
    closed: list[int] = []
    forced: list[int] = []
    warnings: list[str] = []
    hidden: list[ProcessInfo] = []

    for process in processes:
        classification = classify_process(process)
        if classification == "program_owned":
            controller.close_program_owned(process.identity)
            if controller.is_running(process.identity):
                raise ProcessBoundaryError(f"Program-owned Origin process survived cleanup: {process.pid}")
            closed.append(process.pid)
        elif classification == "visible_user":
            return _visible_user_blocked(process)
        else:
            hidden.append(process)

    confirmed_identities = (
        frozenset(process.identity for process in hidden)
        if hidden_confirmation is True
        else frozenset(hidden_confirmation or ())
    )
    if hidden and any(process.identity not in confirmed_identities for process in hidden):
        return BoundaryOutcome(can_continue=False, dialog=_confirm_hidden_dialog(hidden))

    for process in hidden:
        current = controller.current_process(process.pid)
        if current is None:
            continue
        if current.start_time_ns != process.start_time_ns:
            raise ProcessBoundaryError(f"Origin process identity changed before cleanup: {process.pid}")
        if current.visible or current.taskbar_visible:
            return _visible_user_blocked(current)
        if not controller.graceful_close(process.identity):
            after_graceful = controller.current_process(process.pid)
            if after_graceful is not None:
                if after_graceful.start_time_ns != process.start_time_ns:
                    raise ProcessBoundaryError(f"Origin process identity changed before force cleanup: {process.pid}")
                if after_graceful.visible or after_graceful.taskbar_visible:
                    return _visible_user_blocked(after_graceful)
                if not controller.force_close(process.identity):
                    raise ProcessBoundaryError(f"Could not close hidden Origin process: {process.pid}")
                forced.append(process.pid)
                warnings.append(f"已强制关闭隐藏 Origin 进程 {process.pid}。")
        if controller.is_running(process.identity):
            raise ProcessBoundaryError(f"Origin process survived cleanup: {process.pid}")
        closed.append(process.pid)

    return BoundaryOutcome(
        can_continue=True,
        closed_pids=tuple(closed),
        forced_pids=tuple(forced),
        warnings=tuple(warnings),
    )


def _windows_powershell_executable() -> Path:
    if not hasattr(ctypes, "WinDLL"):
        raise ProcessBoundaryError("Windows system directory API is unavailable")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_system_directory = kernel32.GetSystemDirectoryW
    get_system_directory.argtypes = [ctypes.c_wchar_p, ctypes.c_uint]
    get_system_directory.restype = ctypes.c_uint
    buffer = ctypes.create_unicode_buffer(32768)
    length = get_system_directory(buffer, len(buffer))
    if length == 0 or length >= len(buffer):
        raise ProcessBoundaryError("Could not resolve the trusted Windows system directory")
    executable = Path(buffer.value) / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if not executable.is_file():
        raise ProcessBoundaryError(f"Windows PowerShell executable is unavailable: {executable}")
    return executable


def _run_powershell(command: str, *, timeout: float = 5):
    return subprocess.run(
        [str(_windows_powershell_executable()), "-NoProfile", "-Command", command],
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _parse_origin_process_json(stdout: str, window_visibility) -> tuple[ProcessInfo, ...]:
    raw = stdout.strip()
    if not raw or raw == "null":
        return ()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProcessBoundaryError(f"Origin process probe returned invalid JSON: {exc}") from exc
    records = parsed if isinstance(parsed, list) else [parsed]
    processes: list[ProcessInfo] = []
    for record in records:
        if not isinstance(record, dict):
            raise ProcessBoundaryError("Origin process probe returned invalid process record")
        name = str(record.get("name", ""))
        if not name.casefold().startswith("origin"):
            continue
        pid = _required_int(record, "pid")
        start_time_ns = _required_int(record, "start_time_ns")
        handle = int(record.get("main_window_handle") or 0)
        visible, taskbar_visible = window_visibility(handle)
        processes.append(
            ProcessInfo(
                pid=pid,
                start_time_ns=start_time_ns,
                visible=bool(visible),
                taskbar_visible=bool(taskbar_visible),
                program_owned=False,
            )
        )
    return tuple(processes)


def _required_int(record: dict, field: str) -> int:
    try:
        return int(record[field])
    except (KeyError, TypeError, ValueError) as exc:
        raise ProcessBoundaryError(f"Origin process probe missing invalid {field}") from exc


def _best_effort_window_visibility(handle: int) -> tuple[bool, bool]:
    if not handle:
        return False, False
    try:
        user32 = ctypes.windll.user32
        hwnd = ctypes.c_void_p(int(handle))
        visible = bool(user32.IsWindowVisible(hwnd))
        owner = user32.GetWindow(hwnd, 4)
        ex_style = user32.GetWindowLongW(hwnd, -20)
        taskbar_visible = visible and owner == 0 and not (ex_style & 0x80)
        return visible, taskbar_visible
    except Exception:
        return True, True


def _controller_command_succeeded(completed, action: str, success_statuses: set[str]) -> bool:
    if completed.returncode != 0:
        message = completed.stderr.strip() or f"{action} failed"
        raise ProcessBoundaryError(f"Origin process controller failed to {action}: {message}")
    status = completed.stdout.strip().casefold()
    if status in success_statuses:
        return True
    if status in {"running", "no_window"}:
        return False
    raise ProcessBoundaryError(f"Origin process controller returned unexpected status for {action}: {status}")


def _remaining_controller_timeout(deadline: float, operation: str) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        timeout = subprocess.TimeoutExpired(operation, 0)
        raise ProcessBoundaryError(f"Origin process controller timed out during {operation}") from timeout
    return remaining


def _run_controller_command(command_runner, command: str, *, timeout: float, operation: str):
    try:
        return command_runner(command, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise ProcessBoundaryError(f"Origin process controller timed out during {operation}") from exc


def _visible_user_blocked(process: ProcessInfo) -> BoundaryOutcome:
    return BoundaryOutcome(
        can_continue=False,
        dialog=DialogRequest(
            kind=ORIGIN_VISIBLE_USER_BLOCKED,
            title="请关闭 Origin 后继续",
            message=(
                f"检测到可见的 Origin 进程 {process.pid}。"
                "请保存并关闭 Origin，然后点击下方“重新检测”。"
                "任务会停在这里，直到你完成操作。"
            ),
            actions=("retry", "cancel"),
        ),
    )


def _confirm_hidden_dialog(processes: list[ProcessInfo]) -> DialogRequest:
    pids = ", ".join(str(process.pid) for process in processes)
    return DialogRequest(
        kind=ORIGIN_CONFIRM_HIDDEN,
        title="检测到隐藏 Origin 进程",
        message=f"检测到隐藏 Origin 进程：{pids}。确认后程序会尝试自动关闭。",
        actions=("confirm", "cancel"),
    )
