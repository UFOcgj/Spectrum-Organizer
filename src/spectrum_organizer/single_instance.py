from __future__ import annotations

from dataclasses import dataclass
import ctypes
import os
import re


@dataclass(frozen=True)
class InstanceResult:
    is_primary: bool
    should_exit: bool


class FakeInstanceBackend:
    def __init__(self, already_running: bool = False):
        self.already_running = already_running
        self.activation_requests = 0

    def acquire(self) -> bool:
        if self.already_running:
            return False
        self.already_running = True
        return True

    def request_activation(self) -> None:
        self.activation_requests += 1

    def wait_for_activation_request(self, timeout_ms: int = 0) -> bool:
        del timeout_ms
        if not self.activation_requests:
            return False
        self.activation_requests -= 1
        return True


class SingleInstance:
    def __init__(self, backend):
        self.backend = backend

    def enter(self) -> InstanceResult:
        if self.backend.acquire():
            return InstanceResult(is_primary=True, should_exit=False)
        self.backend.request_activation()
        return InstanceResult(is_primary=False, should_exit=True)


def guarded_startup(backend, state_start):
    result = SingleInstance(backend).enter()
    if result.should_exit:
        return result
    state_start()
    return result


class WindowsMutexBackend:
    def __init__(self, name: str | None = None, name_prefix: str = "SpectrumOrganizer"):
        base_name = name or f"Local\\{name_prefix}-{_current_session_key()}"
        self.name = _windows_object_name(base_name)
        self.activation_event_name = f"{self.name}-Activate"
        self._mutex = None
        self._activation_event = None

    def acquire(self) -> bool:
        import win32api
        import win32event
        import winerror

        self._activation_event_handle()
        self._mutex = win32event.CreateMutex(None, False, self.name)
        return win32api.GetLastError() != winerror.ERROR_ALREADY_EXISTS

    def request_activation(self) -> None:
        import win32event

        _allow_any_process_to_set_foreground()
        event = self._activation_event_handle()
        win32event.SetEvent(event)

    def wait_for_activation_request(self, timeout_ms: int = 0) -> bool:
        import win32event

        event = self._activation_event_handle()
        return win32event.WaitForSingleObject(event, timeout_ms) == win32event.WAIT_OBJECT_0

    def _activation_event_handle(self):
        if self._activation_event is None:
            import win32event

            self._activation_event = win32event.CreateEvent(None, False, False, self.activation_event_name)
        return self._activation_event


def _allow_any_process_to_set_foreground() -> bool:
    if os.name != "nt":
        return False
    try:
        allow = ctypes.windll.user32.AllowSetForegroundWindow
        allow.argtypes = [ctypes.c_uint]
        allow.restype = ctypes.c_int
        return bool(allow(0xFFFFFFFF))
    except (AttributeError, OSError):
        return False


def _current_session_key() -> str:
    session_id = _windows_session_id()
    user = os.environ.get("USERNAME") or os.environ.get("USER") or "user"
    return _clean_name_part(f"{user}-{session_id}")


def _windows_session_id() -> str:
    try:
        session_id = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(session_id))
        if ok:
            return str(session_id.value)
    except (AttributeError, OSError):
        pass
    return os.environ.get("SESSIONNAME", "session")


def _windows_object_name(name: str) -> str:
    if name.startswith("Local\\") or name.startswith("Global\\"):
        namespace, rest = name.split("\\", 1)
        return namespace + "\\" + _clean_name_part(rest)
    return _clean_name_part(name)


def _clean_name_part(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-") or "default"
