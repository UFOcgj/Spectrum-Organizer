from __future__ import annotations

from contextlib import contextmanager
import ctypes
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
from uuid import uuid4


class IdentityPathError(RuntimeError):
    def __init__(self, retained_path: Path, reason: str):
        self.retained_path = Path(retained_path)
        super().__init__(reason)


@dataclass(frozen=True)
class ProjectArtifactEvidence:
    identity: tuple[int, int]
    sha256: str
    size: int


def path_identity(path: Path) -> tuple[int, int]:
    target = Path(path)
    if target.is_symlink():
        raise IdentityPathError(
            target,
            f"Filesystem identity target is a symbolic link: {target}",
        )
    try:
        status = target.stat()
    except OSError as exc:
        raise IdentityPathError(
            target,
            f"Filesystem identity could not be read: {target}",
        ) from exc
    if status.st_ino == 0:
        raise IdentityPathError(
            target,
            f"Filesystem identity is unavailable: {target}",
        )
    return status.st_dev, status.st_ino


def lexical_path_exists(path: Path) -> bool:
    return os.path.lexists(Path(path))


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


@contextmanager
def create_exclusive_held_file(
    path: Path,
    *,
    share_write: bool = True,
):
    target = Path(path)
    stream = None
    raw_handle = None
    close_handle = None
    try:
        if os.name == "nt":
            import msvcrt

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            create_file = kernel32.CreateFileW
            create_file.argtypes = (
                ctypes.c_wchar_p,
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.c_void_p,
            )
            create_file.restype = ctypes.c_void_p
            close_handle = kernel32.CloseHandle
            close_handle.argtypes = (ctypes.c_void_p,)
            close_handle.restype = ctypes.c_int
            raw_handle = create_file(
                str(target),
                0x80000000 | 0x40000000,
                0x00000001 | (0x00000002 if share_write else 0),
                None,
                1,
                0x00000080 | 0x00200000,
                None,
            )
            if raw_handle == ctypes.c_void_p(-1).value:
                error = ctypes.get_last_error()
                if error in {80, 183}:
                    raise FileExistsError(target)
                raise IdentityPathError(
                    target,
                    f"Filesystem object could not be created exclusively: "
                    f"{target} (WinError {error})",
                )
            descriptor = msvcrt.open_osfhandle(
                int(raw_handle),
                os.O_RDWR | os.O_BINARY,
            )
            raw_handle = None
            stream = os.fdopen(descriptor, "r+b", buffering=0)
        else:
            stream = target.open("x+b", buffering=0)
        status = os.fstat(stream.fileno())
        identity = (status.st_dev, status.st_ino)
        if path_identity(target) != identity:
            raise IdentityPathError(
                target,
                f"Exclusive filesystem object identity changed: {target}",
            )
        yield stream, identity
        if path_identity(target) != identity:
            raise IdentityPathError(
                target,
                f"Exclusive filesystem object identity changed while held: {target}",
            )
    finally:
        if stream is not None:
            stream.close()
        if close_handle is not None and raw_handle not in (
            None,
            ctypes.c_void_p(-1).value,
        ):
            close_handle(ctypes.c_void_p(raw_handle))


@contextmanager
def create_exclusive_held_directory(path: Path):
    target = Path(path)
    raw_handle = None
    descriptor = None
    close_handle = None
    try:
        if os.name == "nt":
            ntdll = ctypes.WinDLL("ntdll")
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

            class UnicodeString(ctypes.Structure):
                _fields_ = (
                    ("length", ctypes.c_ushort),
                    ("maximum_length", ctypes.c_ushort),
                    ("buffer", ctypes.c_wchar_p),
                )

            class ObjectAttributes(ctypes.Structure):
                _fields_ = (
                    ("length", ctypes.c_ulong),
                    ("root_directory", ctypes.c_void_p),
                    ("object_name", ctypes.POINTER(UnicodeString)),
                    ("attributes", ctypes.c_ulong),
                    ("security_descriptor", ctypes.c_void_p),
                    ("security_quality_of_service", ctypes.c_void_p),
                )

            class IoStatusBlock(ctypes.Structure):
                _fields_ = (
                    ("status", ctypes.c_void_p),
                    ("information", ctypes.c_size_t),
                )

            rtl_dos_path_to_nt_path = ntdll.RtlDosPathNameToNtPathName_U
            rtl_dos_path_to_nt_path.argtypes = (
                ctypes.c_wchar_p,
                ctypes.POINTER(UnicodeString),
                ctypes.c_void_p,
                ctypes.c_void_p,
            )
            rtl_dos_path_to_nt_path.restype = ctypes.c_ubyte
            rtl_free_unicode_string = ntdll.RtlFreeUnicodeString
            rtl_free_unicode_string.argtypes = (
                ctypes.POINTER(UnicodeString),
            )
            nt_create_file = ntdll.NtCreateFile
            nt_create_file.argtypes = (
                ctypes.POINTER(ctypes.c_void_p),
                ctypes.c_ulong,
                ctypes.POINTER(ObjectAttributes),
                ctypes.POINTER(IoStatusBlock),
                ctypes.c_void_p,
                ctypes.c_ulong,
                ctypes.c_ulong,
                ctypes.c_ulong,
                ctypes.c_ulong,
                ctypes.c_void_p,
                ctypes.c_ulong,
            )
            nt_create_file.restype = ctypes.c_long
            close_handle = kernel32.CloseHandle
            close_handle.argtypes = (ctypes.c_void_p,)
            close_handle.restype = ctypes.c_int

            unicode_path = UnicodeString()
            if not rtl_dos_path_to_nt_path(
                str(target.absolute()),
                ctypes.byref(unicode_path),
                None,
                None,
            ):
                raise IdentityPathError(
                    target,
                    f"Filesystem path could not be converted for creation: {target}",
                )
            attributes = ObjectAttributes(
                ctypes.sizeof(ObjectAttributes),
                None,
                ctypes.pointer(unicode_path),
                0x00000040,
                None,
                None,
            )
            io_status = IoStatusBlock()
            handle = ctypes.c_void_p()
            try:
                status = nt_create_file(
                    ctypes.byref(handle),
                    0x00100000 | 0x00010000 | 0x00000080,
                    ctypes.byref(attributes),
                    ctypes.byref(io_status),
                    None,
                    0x00000080,
                    0,
                    2,
                    0x00000001 | 0x00000020,
                    None,
                    0,
                )
            finally:
                rtl_free_unicode_string(ctypes.byref(unicode_path))
            status_code = status & 0xFFFFFFFF
            if status < 0:
                if status_code in {0xC0000035, 0xC000003A}:
                    raise FileExistsError(target)
                raise IdentityPathError(
                    target,
                    "Filesystem directory could not be created exclusively: "
                    f"{target} (NTSTATUS 0x{status_code:08X})",
                )
            raw_handle = handle.value

            class FileId128(ctypes.Structure):
                _fields_ = (("identifier", ctypes.c_ubyte * 16),)

            class FileIdInfo(ctypes.Structure):
                _fields_ = (
                    ("volume_serial_number", ctypes.c_ulonglong),
                    ("file_id", FileId128),
                )

            get_file_information = kernel32.GetFileInformationByHandleEx
            get_file_information.argtypes = (
                ctypes.c_void_p,
                ctypes.c_int,
                ctypes.c_void_p,
                ctypes.c_uint32,
            )
            get_file_information.restype = ctypes.c_int
            file_id_info = FileIdInfo()
            if not get_file_information(
                ctypes.c_void_p(raw_handle),
                18,
                ctypes.byref(file_id_info),
                ctypes.sizeof(file_id_info),
            ):
                raise IdentityPathError(
                    target,
                    f"Created directory identity is unavailable: {target}",
                )
            identity = (
                file_id_info.volume_serial_number,
                int.from_bytes(
                    bytes(file_id_info.file_id.identifier)[:8],
                    "little",
                ),
            )
        else:
            os.mkdir(target)
            descriptor = os.open(
                target,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )

        if descriptor is not None:
            status = os.fstat(descriptor)
            identity = (status.st_dev, status.st_ino)
        if descriptor is not None and path_identity(target) != identity:
            raise IdentityPathError(
                target,
                f"Exclusive directory identity changed after creation: {target}",
            )
        yield target, identity
        if descriptor is not None and path_identity(target) != identity:
            raise IdentityPathError(
                target,
                f"Exclusive directory identity changed while held: {target}",
            )
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if close_handle is not None and raw_handle not in (
            None,
            ctypes.c_void_p(-1).value,
        ):
            close_handle(ctypes.c_void_p(raw_handle))


def quarantine_owned_path(
    path: Path,
    expected_identity: tuple[int, int],
) -> Path:
    target = Path(path)
    if path_identity(target) != expected_identity:
        raise IdentityPathError(
            target,
            f"Filesystem object identity changed before quarantine: {target}",
        )
    quarantine = target.with_name(
        f".SpectrumOrganizer_cleanup_{uuid4().hex}_{target.name}"
    )
    try:
        target.rename(quarantine)
    except OSError as exc:
        raise IdentityPathError(
            target,
            f"Filesystem object could not be quarantined: {target}",
        ) from exc
    try:
        actual_identity = path_identity(quarantine)
    except IdentityPathError as exc:
        raise IdentityPathError(
            exc.retained_path,
            f"Cleanup quarantine identity could not be verified: {quarantine}",
        ) from exc
    if actual_identity != expected_identity:
        retained = quarantine
        if restore_quarantined_path(
            quarantine,
            target,
            actual_identity,
        ):
            retained = target
        raise IdentityPathError(
            retained,
            f"Cleanup quarantine captured a replaced filesystem object: {retained}",
        )
    return quarantine


def restore_quarantined_path(
    quarantined_path: Path,
    original_path: Path,
    expected_identity: tuple[int, int],
) -> bool:
    quarantined = Path(quarantined_path)
    original = Path(original_path)
    if lexical_path_exists(original):
        return False
    try:
        if path_identity(quarantined) != expected_identity:
            return False
        quarantined.rename(original)
        return path_identity(original) == expected_identity
    except (OSError, IdentityPathError):
        return False


def unlink_owned_path(
    path: Path,
    expected_identity: tuple[int, int],
) -> None:
    target = Path(path)
    isolated = quarantine_owned_path(target, expected_identity)
    try:
        _unlink_held_file(isolated, expected_identity)
    except OSError as exc:
        retained = isolated
        if restore_quarantined_path(isolated, target, expected_identity):
            retained = target
        raise IdentityPathError(
            retained,
            f"Owned file could not be removed; retained at {retained}: {exc}",
        ) from exc


def remove_empty_owned_directory(
    path: Path,
    expected_identity: tuple[int, int],
) -> None:
    target = Path(path)
    if os.name != "nt":
        if path_identity(target) != expected_identity:
            raise IdentityPathError(
                target,
                f"Owned directory identity changed before deletion: {target}",
            )
        target.rmdir()
        return

    import msvcrt

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    )
    create_file.restype = ctypes.c_void_p
    set_file_information = kernel32.SetFileInformationByHandle
    set_file_information.argtypes = (
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint32,
    )
    set_file_information.restype = ctypes.c_int
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (ctypes.c_void_p,)
    close_handle.restype = ctypes.c_int
    raw_handle = create_file(
        str(target),
        0x00010000 | 0x00000080,
        0x00000001 | 0x00000002 | 0x00000004,
        None,
        3,
        0x02000000 | 0x00200000,
        None,
    )
    if raw_handle == ctypes.c_void_p(-1).value:
        raise IdentityPathError(
            target,
            f"Owned directory could not be opened for deletion: {target} "
            f"(WinError {ctypes.get_last_error()})",
        )
    descriptor = None
    try:
        descriptor = msvcrt.open_osfhandle(int(raw_handle), os.O_RDONLY)
        raw_handle = None
        status = os.fstat(descriptor)
        if (status.st_dev, status.st_ino) != expected_identity:
            raise IdentityPathError(
                target,
                f"Owned directory identity changed before deletion: {target}",
            )

        class FileDispositionInfo(ctypes.Structure):
            _fields_ = (("delete_file", ctypes.c_int),)

        disposition = FileDispositionInfo(1)
        operating_handle = msvcrt.get_osfhandle(descriptor)
        if not set_file_information(
            ctypes.c_void_p(operating_handle),
            4,
            ctypes.byref(disposition),
            ctypes.sizeof(disposition),
        ):
            raise OSError(
                ctypes.get_last_error(),
                f"Owned directory could not be marked for deletion: {target}",
            )
    finally:
        if descriptor is not None:
            os.close(descriptor)
        elif raw_handle not in (None, ctypes.c_void_p(-1).value):
            close_handle(ctypes.c_void_p(raw_handle))
    if lexical_path_exists(target):
        raise IdentityPathError(
            target,
            f"Filesystem object remains at owned directory deletion path: {target}",
        )


def _unlink_held_file(
    path: Path,
    expected_identity: tuple[int, int],
) -> None:
    target = Path(path)
    if os.name != "nt":
        if path_identity(target) != expected_identity:
            raise IdentityPathError(
                target,
                f"Owned file identity changed before deletion: {target}",
            )
        target.unlink()
        return

    import msvcrt

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    )
    create_file.restype = ctypes.c_void_p
    set_file_information = kernel32.SetFileInformationByHandle
    set_file_information.argtypes = (
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint32,
    )
    set_file_information.restype = ctypes.c_int
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (ctypes.c_void_p,)
    close_handle.restype = ctypes.c_int
    raw_handle = create_file(
        str(target),
        0x00010000 | 0x00000080,
        0x00000001 | 0x00000002 | 0x00000004,
        None,
        3,
        0x00000080 | 0x00200000,
        None,
    )
    if raw_handle == ctypes.c_void_p(-1).value:
        raise IdentityPathError(
            target,
            f"Owned file could not be opened for deletion: {target} "
            f"(WinError {ctypes.get_last_error()})",
        )
    descriptor = None
    try:
        descriptor = msvcrt.open_osfhandle(
            int(raw_handle),
            os.O_RDONLY | os.O_BINARY,
        )
        raw_handle = None
        status = os.fstat(descriptor)
        if (status.st_dev, status.st_ino) != expected_identity:
            raise IdentityPathError(
                target,
                f"Owned file identity changed before deletion: {target}",
            )

        class FileDispositionInfo(ctypes.Structure):
            _fields_ = (("delete_file", ctypes.c_int),)

        disposition = FileDispositionInfo(1)
        operating_handle = msvcrt.get_osfhandle(descriptor)
        if not set_file_information(
            ctypes.c_void_p(operating_handle),
            4,
            ctypes.byref(disposition),
            ctypes.sizeof(disposition),
        ):
            raise OSError(
                ctypes.get_last_error(),
                f"Owned file could not be marked for deletion: {target}",
            )
    finally:
        if descriptor is not None:
            os.close(descriptor)
        elif raw_handle not in (None, ctypes.c_void_p(-1).value):
            close_handle(ctypes.c_void_p(raw_handle))
    if lexical_path_exists(target):
        raise IdentityPathError(
            target,
            f"Filesystem object remains at owned deletion path: {target}",
        )


@contextmanager
def hold_directory_identity(
    path: Path,
    expected_identity: tuple[int, int],
):
    target = Path(path)
    handle = None
    descriptor = None
    close_handle = None
    try:
        if target.is_symlink() or not target.is_dir():
            raise IdentityPathError(
                target,
                f"Filesystem identity target is not a directory: {target}",
            )
        if os.name == "nt":
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            create_file = kernel32.CreateFileW
            create_file.argtypes = (
                ctypes.c_wchar_p,
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.c_void_p,
            )
            create_file.restype = ctypes.c_void_p
            close_handle = kernel32.CloseHandle
            close_handle.argtypes = (ctypes.c_void_p,)
            close_handle.restype = ctypes.c_int
            handle = create_file(
                str(target),
                0x00000080,
                0x00000001 | 0x00000002,
                None,
                3,
                0x02000000 | 0x00200000,
                None,
            )
            if handle == ctypes.c_void_p(-1).value:
                raise IdentityPathError(
                    target,
                    f"Filesystem directory could not be locked: {target} "
                    f"(WinError {ctypes.get_last_error()})",
                )
        else:
            descriptor = os.open(
                target,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            status = os.fstat(descriptor)
            if (status.st_dev, status.st_ino) != expected_identity:
                raise IdentityPathError(
                    target,
                    f"Filesystem directory identity changed before lock: {target}",
                )
        if path_identity(target) != expected_identity:
            raise IdentityPathError(
                target,
                f"Filesystem directory identity changed before lock: {target}",
            )
        yield target
        if path_identity(target) != expected_identity:
            raise IdentityPathError(
                target,
                f"Filesystem directory identity changed while locked: {target}",
            )
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if (
            close_handle is not None
            and handle not in (None, ctypes.c_void_p(-1).value)
        ):
            close_handle(ctypes.c_void_p(handle))


@contextmanager
def hold_file_identity(
    path: Path,
    expected_identity: tuple[int, int],
    *,
    allow_write: bool = True,
    allow_delete: bool = False,
):
    target = Path(path)
    handle = None
    stream = None
    close_handle = None
    try:
        if os.name == "nt":
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            create_file = kernel32.CreateFileW
            create_file.argtypes = (
                ctypes.c_wchar_p,
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.c_void_p,
            )
            create_file.restype = ctypes.c_void_p
            close_handle = kernel32.CloseHandle
            close_handle.argtypes = (ctypes.c_void_p,)
            close_handle.restype = ctypes.c_int
            handle = create_file(
                str(target),
                0x80000000,
                0x00000001
                | (0x00000002 if allow_write else 0)
                | (0x00000004 if allow_delete else 0),
                None,
                3,
                0x00000080 | 0x00200000,
                None,
            )
            if handle == ctypes.c_void_p(-1).value:
                raise IdentityPathError(
                    target,
                    f"Filesystem object could not be locked: {target} "
                    f"(WinError {ctypes.get_last_error()})",
                )
        else:
            stream = target.open("rb")
            status = os.fstat(stream.fileno())
            if (status.st_dev, status.st_ino) != expected_identity:
                raise IdentityPathError(
                    target,
                    f"Filesystem object identity changed before lock: {target}",
                )
        if path_identity(target) != expected_identity:
            raise IdentityPathError(
                target,
                f"Filesystem object identity changed before lock: {target}",
            )
        yield target
        if path_identity(target) != expected_identity:
            raise IdentityPathError(
                target,
                f"Filesystem object identity changed while locked: {target}",
            )
    finally:
        if stream is not None:
            stream.close()
        if (
            close_handle is not None
            and handle not in (None, ctypes.c_void_p(-1).value)
        ):
            close_handle(ctypes.c_void_p(handle))


def read_held_file_bytes(
    path: Path,
    expected_identity: tuple[int, int],
) -> bytes:
    target = Path(path)
    with hold_file_identity(
        target,
        expected_identity,
        allow_write=False,
    ):
        with target.open("rb", buffering=0) as stream:
            status = os.fstat(stream.fileno())
            identity = (status.st_dev, status.st_ino)
            content = stream.read()
            if (
                identity != expected_identity
                or path_identity(target) != identity
            ):
                raise IdentityPathError(
                    target,
                    f"Filesystem object identity changed while reading: {target}",
                )
    return content
