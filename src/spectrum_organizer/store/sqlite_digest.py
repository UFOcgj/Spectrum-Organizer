from __future__ import annotations

import hashlib
import sqlite3
import struct


_SORT_KEY_FUNCTION = "_spectrum_organizer_digest_value"
_PROGRESS_HANDLER_STEPS = 1000


def sqlite_content_sha256(connection, *, cancel_check=None) -> str:
    """Hash user schema and table contents without materializing whole tables."""
    digest = hashlib.sha256()
    _update_frame(digest, b"spectrum-organizer-sqlite-content-v1")

    pending_cancel_error: BaseException | None = None

    def check_progress() -> int:
        nonlocal pending_cancel_error
        try:
            _check_cancel(cancel_check)
        except BaseException as exc:
            pending_cancel_error = exc
            return 1
        return 0

    connection.create_function(
        _SORT_KEY_FUNCTION,
        1,
        _value_frame,
        deterministic=True,
    )
    if cancel_check is not None:
        connection.set_progress_handler(check_progress, _PROGRESS_HANDLER_STEPS)

    try:
        schema_query = (
            "select type, name, tbl_name, sql from sqlite_schema "
            "where lower(name) not glob 'sqlite_*' order by type, name"
        )
        for row in connection.execute(schema_query):
            _check_cancel(cancel_check)
            _update_frame(digest, b"schema")
            _update_row(digest, row)

        table_query = (
            "select name from sqlite_schema "
            "where type = 'table' and lower(name) not glob 'sqlite_*' order by name"
        )
        for (table_name,) in connection.execute(table_query):
            _check_cancel(cancel_check)
            name = str(table_name)
            quoted_table = _quote_identifier(name)
            _update_frame(digest, b"table")
            _update_value(digest, name)

            columns: list[str] = []
            for column in connection.execute(f"pragma table_info({quoted_table})"):
                _check_cancel(cancel_check)
                column_name = str(column[1])
                columns.append(column_name)
                _update_frame(digest, b"column")
                _update_value(digest, column_name)

            statement = f"select * from {quoted_table}"
            if columns:
                statement += " order by " + ", ".join(
                    f"{_SORT_KEY_FUNCTION}({_quote_identifier(column_name)})"
                    for column_name in columns
                )

            for row in connection.execute(statement):
                _check_cancel(cancel_check)
                _update_frame(digest, b"row")
                _update_row(digest, row)
    except sqlite3.OperationalError:
        if pending_cancel_error is not None:
            raise pending_cancel_error
        raise
    finally:
        if cancel_check is not None:
            connection.set_progress_handler(None, 0)

    return digest.hexdigest()


def _check_cancel(cancel_check) -> None:
    if cancel_check is not None:
        cancel_check()


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _update_row(digest, row) -> None:
    digest.update(len(row).to_bytes(4, "big"))
    for value in row:
        _update_value(digest, value)


def _update_value(digest, value) -> None:
    digest.update(_value_frame(value))


def _value_frame(value) -> bytes:
    if value is None:
        tag, payload = b"N", b""
    elif isinstance(value, int):
        tag, payload = b"I", str(value).encode("ascii")
    elif isinstance(value, float):
        tag, payload = b"F", struct.pack(">d", value)
    elif isinstance(value, str):
        tag, payload = b"T", value.encode("utf-8")
    elif isinstance(value, (bytes, bytearray, memoryview)):
        tag, payload = b"B", bytes(value)
    else:
        raise TypeError(f"Unsupported SQLite value type: {type(value).__name__}")
    return tag + len(payload).to_bytes(8, "big") + payload


def _update_frame(digest, payload: bytes) -> None:
    digest.update(len(payload).to_bytes(8, "big"))
    digest.update(payload)
