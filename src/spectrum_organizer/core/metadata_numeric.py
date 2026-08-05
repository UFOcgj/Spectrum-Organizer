from __future__ import annotations

from decimal import Decimal, InvalidOperation
import math
import re


_DECIMAL_TEXT = re.compile(
    r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?"
)
_MAX_SIGNIFICANT_DIGITS = 64
_MAX_ABS_EXPONENT = 64


def is_finite_real_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, ValueError):
        return False


def parse_metadata_decimal(
    value: object,
    *,
    nonnegative: bool = False,
) -> Decimal:
    text = str(value).strip()
    if _DECIMAL_TEXT.fullmatch(text) is None:
        raise ValueError("not an ASCII decimal")
    try:
        number = Decimal(text)
    except (InvalidOperation, ValueError):
        raise ValueError("not a decimal") from None
    if not number.is_finite():
        raise ValueError("not finite")
    decimal_tuple = number.as_tuple()
    if (
        len(decimal_tuple.digits) > _MAX_SIGNIFICANT_DIGITS
        or abs(decimal_tuple.exponent) > _MAX_ABS_EXPONENT
        or abs(number.adjusted()) > _MAX_ABS_EXPONENT
    ):
        raise ValueError("outside supported metadata numeric domain")
    if nonnegative and number < 0:
        raise ValueError("must not be negative")
    return number


def format_metadata_decimal(number: Decimal) -> str:
    formatted = format(number, "f")
    if "." in formatted:
        formatted = formatted.rstrip("0").rstrip(".")
    return "0" if formatted == "-0" else formatted


def format_raw_slit_fields(
    value: tuple[object, ...] | None,
) -> str:
    if not value:
        return ""
    return "/".join(str(item) for item in value)
