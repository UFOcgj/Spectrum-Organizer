from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, DecimalException, InvalidOperation, localcontext
import re


class ConcentrationError(ValueError):
    pass


@dataclass(frozen=True)
class ConcentrationEntry:
    value_text: str
    unit: str
    full_text: str


_SUPERSCRIPT_TRANSLATION = str.maketrans(
    {
        "⁰": "0",
        "¹": "1",
        "²": "2",
        "³": "3",
        "⁴": "4",
        "⁵": "5",
        "⁶": "6",
        "⁷": "7",
        "⁸": "8",
        "⁹": "9",
        "⁺": "+",
        "⁻": "-",
    }
)

_MAX_CANONICAL_CONCENTRATION_NUMBER_LENGTH = 255
_MOLARITY_UNIT_PATTERN = r"(?:[mM][oO][lL]\s*/\s*[lL]|mM|[µμu]M|nM|pM|M)"
_MOLARITY_UNIT_EXPONENTS = {
    "M": 0,
    "mM": -3,
    "µM": -6,
    "μM": -6,
    "uM": -6,
    "nM": -9,
    "pM": -12,
}
_PERCENTAGE_UNIT_PATTERN = r"(?P<percentage_unit>wt|weight|mol|mole)\s*\.?\s*%"
_PLAIN_CONCENTRATION_NUMBER = r"[+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
_CARET_CONCENTRATION_NUMBER = (
    r"(?:(?:\d+(?:\.\d*)?|\.\d+)\s*[×xX*]\s*)?"
    r"10\s*(?:\^|\*\*)\s*[+-]?\d+"
)
_SUPERSCRIPT_CONCENTRATION_NUMBER = (
    r"(?:(?:\d+(?:\.\d*)?|\.\d+)\s*[×xX*]\s*)?"
    r"10[⁺⁻⁰¹²³⁴⁵⁶⁷⁸⁹]+"
)
_CONCENTRATION_NUMBER_PATTERN = (
    rf"(?:{_CARET_CONCENTRATION_NUMBER}"
    rf"|{_SUPERSCRIPT_CONCENTRATION_NUMBER}"
    rf"|{_PLAIN_CONCENTRATION_NUMBER})"
)
_CONCENTRATION_TOKEN_START = r"(?<![0-9.+^*×−⁺⁻⁰¹²³⁴⁵⁶⁷⁸⁹])"
_CONCENTRATION_TOKEN_END = r"(?=$|[\s_./\\+()\[\],;:-])"
_MOLARITY_TOKEN_PATTERN = re.compile(
    rf"{_CONCENTRATION_TOKEN_START}"
    rf"(?P<token>{_CONCENTRATION_NUMBER_PATTERN}\s*{_MOLARITY_UNIT_PATTERN})"
    rf"{_CONCENTRATION_TOKEN_END}"
)
_PERCENTAGE_TOKEN_PATTERN = re.compile(
    rf"{_CONCENTRATION_TOKEN_START}"
    rf"(?P<token>{_PLAIN_CONCENTRATION_NUMBER}\s*{_PERCENTAGE_UNIT_PATTERN})"
    rf"{_CONCENTRATION_TOKEN_END}",
    flags=re.IGNORECASE,
)
_MOLARITY_UNIT_TOKEN_PATTERN = re.compile(
    rf"{_MOLARITY_UNIT_PATTERN}{_CONCENTRATION_TOKEN_END}"
)
_PERCENTAGE_UNIT_TOKEN_PATTERN = re.compile(
    rf"{_PERCENTAGE_UNIT_PATTERN}{_CONCENTRATION_TOKEN_END}",
    flags=re.IGNORECASE,
)


def normalize_temperature(text: str) -> str:
    value = str(text).strip()
    folded = value.casefold()
    room_alias = re.sub(r"[\s_-]+", "", folded)
    if folded == "rt" or room_alias in {"roomtemperature", "roomtemp"} or value == "室温":
        return "298 K"
    if folded.endswith("k"):
        numeric = value[:-1].strip()
    else:
        numeric = value
    if not re.fullmatch(r"[+]?(?:\d+(?:\.\d*)?|\.\d+)", numeric):
        raise ValueError(f"Invalid Kelvin temperature: {text}")
    number = _decimal(numeric, "temperature")
    if number <= 0:
        raise ValueError(f"Invalid Kelvin temperature: {text}")
    return f"{_format_decimal_plain(number)} K"


def normalize_molarity(text: str) -> str:
    value = str(text).strip()
    number_text, unit_exponent = _strip_exact_molarity_unit(value)
    number = _parse_molarity_number(number_text)
    if unit_exponent:
        try:
            with localcontext() as context:
                context.prec = max(context.prec, len(number.as_tuple().digits))
                number = number.scaleb(unit_exponent)
        except DecimalException as exc:
            raise ConcentrationError("Molarity exponent is outside the supported range") from exc
    if number <= 0:
        raise ConcentrationError("Molarity must be positive")
    return f"{_format_molarity_number(number)} M"


def normalize_doped_concentration(text: str) -> str:
    number_text, unit = _strip_percentage_unit(str(text).strip())
    number = _decimal(number_text, "percentage concentration")
    if number < 0 or number > 100:
        raise ConcentrationError("Percentage concentration must be between 0 and 100")
    return f"{_format_concentration_decimal_plain(number)} {unit}"


def normalize_concentration_input(
    text: str,
    selected_unit: str | None,
    allowed_units: tuple[str, ...],
) -> ConcentrationEntry:
    allowed = tuple(_canonical_unit(unit) for unit in allowed_units)
    value = str(text).strip()
    explicit_unit = _detect_explicit_unit(value)
    if explicit_unit:
        unit = _canonical_unit(explicit_unit)
        if unit not in allowed:
            raise ConcentrationError(f"Unit {explicit_unit} is not allowed here")
        if unit == "M":
            full = normalize_molarity(value)
        else:
            full = normalize_doped_concentration(value)
    else:
        if selected_unit is None:
            raise ConcentrationError("A concentration unit must be selected")
        unit = _canonical_unit(selected_unit)
        if unit not in allowed:
            raise ConcentrationError(f"Unit {selected_unit} is not allowed here")
        full = normalize_molarity(f"{value} M") if unit == "M" else normalize_doped_concentration(f"{value} {unit}")
    value_text, full_unit = full.rsplit(" ", 1)
    return ConcentrationEntry(value_text=value_text, unit=full_unit, full_text=full)


def extract_concentration_entries(text: str) -> tuple[ConcentrationEntry, ...]:
    entries, _ = extract_concentration_evidence(text)
    return entries


def extract_concentration_evidence(
    text: str,
) -> tuple[tuple[ConcentrationEntry, ...], frozenset[str]]:
    source = str(text or "")
    entries: list[ConcentrationEntry] = []
    invalid_units: set[str] = set()
    valid_ends: dict[str, set[int]] = {"M": set(), "percentage": set()}
    for match in _MOLARITY_TOKEN_PATTERN.finditer(source):
        if _has_malformed_concentration_number_prefix(source, match.start()):
            invalid_units.add("M")
            continue
        try:
            entry = normalize_concentration_input(match.group("token"), None, ("M",))
        except ConcentrationError:
            invalid_units.add("M")
        else:
            entries.append(entry)
            valid_ends["M"].add(match.end("token"))
    for match in _PERCENTAGE_TOKEN_PATTERN.finditer(source):
        if _has_malformed_concentration_number_prefix(source, match.start()):
            invalid_units.add("percentage")
            continue
        try:
            entry = normalize_concentration_input(
                match.group("token"),
                None,
                ("wt%", "mol%"),
            )
        except ConcentrationError:
            invalid_units.add("percentage")
        else:
            entries.append(entry)
            valid_ends["percentage"].add(match.end("token"))

    for family, pattern in (
        ("M", _MOLARITY_UNIT_TOKEN_PATTERN),
        ("percentage", _PERCENTAGE_UNIT_TOKEN_PATTERN),
    ):
        for match in pattern.finditer(source):
            if (
                match.end() not in valid_ends[family]
                and _has_numeric_concentration_prefix(source, match.start())
            ):
                invalid_units.add(family)
    return tuple(entries), frozenset(invalid_units)


def _has_malformed_concentration_number_prefix(text: str, token_start: int) -> bool:
    prefix = text[:token_start]
    if prefix.endswith("−"):
        return True
    if prefix.endswith("-") and (len(prefix) < 2 or not prefix[-2].isalnum()):
        return True
    return bool(
        re.search(
            r"[0-9⁰¹²³⁴⁵⁶⁷⁸⁹]"
            r"[0-9.\s+\-^*×xXeE⁺⁻⁰¹²³⁴⁵⁶⁷⁸⁹]*"
            r"[eExX^*×][+\-−⁺⁻]*\s*$",
            prefix,
        )
    )


def _has_numeric_concentration_prefix(text: str, unit_start: int) -> bool:
    prefix = text[:unit_start].rstrip()
    return bool(
        re.search(
            r"[0-9⁰¹²³⁴⁵⁶⁷⁸⁹][0-9.\s+\-^*×xXeE⁺⁻⁰¹²³⁴⁵⁶⁷⁸⁹]*$",
            prefix,
        )
    )


def _strip_exact_molarity_unit(text: str) -> tuple[str, int]:
    if re.search(r"(?<![A-Za-z])m\s*$", text):
        raise ConcentrationError("Molarity unit must be uppercase M")
    match = re.fullmatch(
        rf"(?P<number>.+?)\s*(?P<unit>{_MOLARITY_UNIT_PATTERN})",
        text,
    )
    if not match:
        raise ConcentrationError("Molarity must use an explicit supported unit")
    unit = re.sub(r"\s+", "", match.group("unit"))
    exponent = 0 if "/" in unit else _MOLARITY_UNIT_EXPONENTS[unit]
    return match.group("number").strip(), exponent


def _strip_percentage_unit(text: str) -> tuple[str, str]:
    match = re.fullmatch(
        rf"(?P<number>.+?)\s*{_PERCENTAGE_UNIT_PATTERN}",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        raise ConcentrationError("Doped concentration must use wt% or mol%")
    unit = "wt%" if match.group("percentage_unit").casefold().startswith("w") else "mol%"
    return match.group("number").strip(), unit


def _detect_explicit_unit(text: str) -> str | None:
    if re.search(r"(?<![A-Za-z])m\s*$", text):
        return "m"
    if re.search(rf"{_MOLARITY_UNIT_PATTERN}\s*$", text):
        return "M"
    match = re.search(
        rf"{_PERCENTAGE_UNIT_PATTERN}\s*$",
        text,
        flags=re.IGNORECASE,
    )
    if match:
        return "wt%" if match.group("percentage_unit").casefold().startswith("w") else "mol%"
    return None


def _canonical_unit(unit: str) -> str:
    value = str(unit).strip()
    if value == "M":
        return "M"
    match = re.fullmatch(r"(wt|mol)\s*%", value, flags=re.IGNORECASE)
    if match:
        return f"{match.group(1).casefold()}%"
    raise ConcentrationError(f"Unsupported concentration unit: {unit}")


def _parse_molarity_number(text: str) -> Decimal:
    value = _mark_superscript_exponents(text)
    value = value.translate(_SUPERSCRIPT_TRANSLATION).replace("**", "^")
    value = value.replace("×", "x").replace("X", "x").replace("*", "x")
    compact = re.sub(r"\s+", "", value)
    if "e" in compact.lower():
        return _decimal(compact, "molarity")
    sci = re.fullmatch(r"(?:(?P<coeff>[+-]?(?:\d+(?:\.\d*)?|\.\d+))x)?10\^(?P<exp>[+-]?\d+)", compact)
    if sci:
        coeff = _decimal(sci.group("coeff") or "1", "molarity coefficient")
        exponent = int(sci.group("exp"))
        return _decimal(f"{coeff}e{exponent}", "molarity")
    return _decimal(compact, "molarity")


def _mark_superscript_exponents(text: str) -> str:
    superscripts = "⁺⁻⁰¹²³⁴⁵⁶⁷⁸⁹"
    return re.sub(rf"10([{superscripts}]+)", r"10^\1", text)


def _decimal(text: str, field: str) -> Decimal:
    try:
        value = Decimal(text)
    except (InvalidOperation, ValueError) as exc:
        raise ConcentrationError(f"Invalid {field}: {text}") from exc
    if not value.is_finite():
        raise ConcentrationError(f"Invalid {field}: {text}")
    return value


def _format_molarity_number(number: Decimal) -> str:
    if Decimal("0.1") <= number <= Decimal("100"):
        return _format_decimal_plain(number)
    exponent = number.adjusted()
    try:
        with localcontext() as context:
            context.prec = max(context.prec, len(number.as_tuple().digits))
            coefficient = number.scaleb(-exponent)
    except DecimalException as exc:
        raise ConcentrationError("Molarity exponent is outside the supported range") from exc
    if not coefficient.is_finite() or not Decimal("1") <= coefficient < Decimal("10"):
        raise ConcentrationError("Molarity exponent is outside the supported range")
    return f"{_format_decimal_plain(coefficient)}×10^{exponent}"


def _format_decimal_plain(number: Decimal) -> str:
    text = format(number, "f")
    if "." in text:
        return text.rstrip("0").rstrip(".")
    return text


def _format_concentration_decimal_plain(number: Decimal) -> str:
    if number.is_zero():
        return "0"
    if _canonical_plain_length(number) > _MAX_CANONICAL_CONCENTRATION_NUMBER_LENGTH:
        raise ConcentrationError("Concentration value is too long to use safely")
    return _format_decimal_plain(number)


def _canonical_plain_length(number: Decimal) -> int:
    sign, raw_digits, exponent = number.as_tuple()
    digits = list(raw_digits)
    while len(digits) > 1 and digits[-1] == 0:
        digits.pop()
        exponent += 1
    if not any(digits):
        return 1
    digit_count = len(digits)
    decimal_point = digit_count + exponent
    if exponent >= 0:
        length = digit_count + exponent
    elif decimal_point > 0:
        length = digit_count + 1
    else:
        length = 2 - decimal_point + digit_count
    return int(sign) + length
