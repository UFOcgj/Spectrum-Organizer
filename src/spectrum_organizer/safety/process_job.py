from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import sys
import time


PARENT_START_GATE_ENV = "SPECTRUM_ORGANIZER_PARENT_START_GATE"
PARENT_START_GATE_TOKEN_ENV = "SPECTRUM_ORGANIZER_PARENT_START_GATE_TOKEN"


class ProcessJobError(RuntimeError):
    pass


class WindowsProcessJob:
    def __init__(self, handle: int):
        self._handle = handle
        self.assigned = False

    def terminate(self) -> None:
        if self._handle is None:
            return
        if not _kernel32.TerminateJobObject(self._handle, 1):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self) -> None:
        if self._handle is None:
            return
        handle = self._handle
        if not _kernel32.CloseHandle(handle):
            raise ctypes.WinError(ctypes.get_last_error())
        self._handle = None


def wait_for_parent_start_gate(*, timeout: float = 30.0, poll_interval: float = 0.01) -> None:
    gate_value = os.environ.get(PARENT_START_GATE_ENV)
    if not gate_value:
        return
    expected_token = os.environ.get(PARENT_START_GATE_TOKEN_ENV)
    if not expected_token:
        raise ProcessJobError("Parent start gate token is missing")
    gate_path = Path(gate_value)
    deadline = time.monotonic() + timeout
    while True:
        try:
            if gate_path.is_file() and gate_path.read_text(encoding="ascii") == expected_token:
                return
        except (OSError, UnicodeError):
            pass
        if time.monotonic() >= deadline:
            raise ProcessJobError("Parent did not release the child process start gate")
        time.sleep(poll_interval)


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _BasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _ExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


if sys.platform == "win32":
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.SetInformationJobObject.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    )
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL
    _kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    _kernel32.TerminateJobObject.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _kernel32.CloseHandle.restype = wintypes.BOOL
else:
    _kernel32 = None


def bind_process_to_job(process, *, required: bool) -> WindowsProcessJob | None:
    if sys.platform != "win32":
        return None
    process_handle = getattr(process, "_handle", None)
    if process_handle is None:
        if required:
            raise ProcessJobError("Windows child process handle is unavailable")
        return None

    job_handle = _kernel32.CreateJobObjectW(None, None)
    if not job_handle:
        raise ProcessJobError(str(ctypes.WinError(ctypes.get_last_error())))
    job = WindowsProcessJob(job_handle)
    try:
        limits = _ExtendedLimitInformation()
        limits.BasicLimitInformation.LimitFlags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not _kernel32.SetInformationJobObject(
            job_handle,
            9,  # JobObjectExtendedLimitInformation
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        if not _kernel32.AssignProcessToJobObject(job_handle, int(process_handle)):
            raise ctypes.WinError(ctypes.get_last_error())
        job.assigned = True
    except Exception as exc:
        try:
            job.close()
        except Exception as close_exc:
            process._spectrum_organizer_job = job
            raise ProcessJobError(
                f"Could not bind child process to Windows Job Object: {exc}; "
                f"Job close failed: {close_exc}"
            ) from close_exc
        raise ProcessJobError(f"Could not bind child process to Windows Job Object: {exc}") from exc
    process._spectrum_organizer_job = job
    return job


def terminate_bound_process(process) -> None:
    job = getattr(process, "_spectrum_organizer_job", None)
    if job is not None:
        assigned = getattr(job, "assigned", True)
        job_error = None
        try:
            job.terminate()
        except Exception as exc:
            if assigned:
                raise
            job_error = exc
        if assigned:
            return
    kill = getattr(process, "kill", None)
    if callable(kill):
        kill()
    else:
        process.terminate()
    if job is not None and job_error is not None:
        raise job_error


def close_bound_process_job(process) -> None:
    job = getattr(process, "_spectrum_organizer_job", None)
    if job is None:
        return
    job.close()
    process._spectrum_organizer_job = None
