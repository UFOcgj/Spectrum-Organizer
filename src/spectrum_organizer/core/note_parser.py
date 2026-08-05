from __future__ import annotations

from dataclasses import dataclass
import re

from spectrum_organizer.core.metadata_numeric import parse_metadata_decimal
from spectrum_organizer.domain.models import SpectrumClass


class NoteParseError(ValueError):
    pass


_TYPE_MAP = {
    "Spectral Acquisition[Emission]": SpectrumClass.STEADY_EMISSION,
    "Spectral Acquisition[Excitation]": SpectrumClass.STEADY_EXCITATION,
    "Phos Acquisition[Emission]": SpectrumClass.DELAYED_EMISSION,
    "Phos Acquisition[Excitation]": SpectrumClass.DELAYED_EXCITATION,
    "3D Acquisition[Excitation vs Emission vs Intensity]": SpectrumClass.STEADY_2D,
}


@dataclass(frozen=True)
class DelayParameters:
    flash_delay: str
    sample_window: str
    time_per_flash: str
    flash_count: str


@dataclass(frozen=True)
class ParsedNote:
    acquisition_type: str
    spectrum_class: SpectrumClass
    fixed_excitation_wavelength: str | None = None
    fixed_emission_wavelength: str | None = None
    excitation_range: tuple[str, str] | None = None
    emission_range: tuple[str, str] | None = None
    excitation_increment: str | None = None
    emission_increment: str | None = None
    excitation_slits: tuple[str, str] | None = None
    emission_slits: tuple[str, str] | None = None
    delay: DelayParameters | None = None
    note_datetime: str | None = None


def parse_book_note(text: str) -> ParsedNote:
    if not str(text).startswith("[EXP_FD_FILE]"):
        raise NoteParseError("Book-local Note must start with [EXP_FD_FILE]")
    values = _parse_key_values(text)
    acquisition_type = _find_acquisition_type(text)
    sections = _parse_sections(text)
    spectrum_class = _TYPE_MAP[acquisition_type]
    delay = _parse_delay(values) if spectrum_class in {SpectrumClass.DELAYED_EMISSION, SpectrumClass.DELAYED_EXCITATION} else None
    excitation = sections.get("ex1", {})
    emission = sections.get("em1", {})
    fixed_excitation = _measurement_value(excitation, "Park", "nm")
    fixed_emission = _measurement_value(emission, "Park", "nm")
    excitation_range = _section_range(excitation)
    emission_range = _section_range(emission)
    return ParsedNote(
        acquisition_type=acquisition_type,
        spectrum_class=spectrum_class,
        fixed_excitation_wavelength=fixed_excitation
        or _measurement_value(values, "Excitation Wavelength", "nm"),
        fixed_emission_wavelength=fixed_emission
        or _measurement_value(values, "Emission Wavelength", "nm"),
        excitation_range=excitation_range or _parse_range(_first(values, "Excitation Range")),
        emission_range=emission_range or _parse_range(_first(values, "Emission Range")),
        excitation_increment=_measurement_value(excitation, "Increment", "nm")
        or _measurement_value(values, "Excitation Increment", "nm"),
        emission_increment=_measurement_value(emission, "Increment", "nm")
        or _measurement_value(values, "Emission Increment", "nm"),
        excitation_slits=_section_slits(excitation, "EX1"),
        emission_slits=_section_slits(emission, "EM1"),
        delay=delay,
        note_datetime=_parse_note_datetime(values),
    )


def ui_delay_units() -> dict[str, str]:
    return {"Flash Delay": "ms", "Sample window": "ms", "Time per Flash": "ms"}


def _find_acquisition_type(text: str) -> str:
    declared = []
    type_keys = {"acquisition type", "experiment type"}
    for line in text.splitlines():
        key_value = _split_key_value(line)
        if key_value is None:
            continue
        key, value = key_value
        if key.strip().casefold() in type_keys and value.strip():
            declared.append(value.strip())
    if not declared or any(value not in _TYPE_MAP for value in declared):
        raise NoteParseError("Unsupported acquisition type")
    if len(set(declared)) != 1:
        raise NoteParseError("Conflicting acquisition types")
    return declared[0]


def _parse_note_datetime(values: dict[str, str]) -> str | None:
    combined = _first(values, "Date/Time") or _first(values, "Test Date/Time")
    if combined:
        return combined
    date = _first(values, "Test Date") or _first(values, "Date")
    time = _first(values, "Test Time") or _first(values, "Time")
    return " ".join(value for value in (date, time) if value) or None


def _parse_key_values(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    in_section = False
    for line in text.splitlines():
        section = re.fullmatch(r"\s*\[([^\]]+)\]\s*", line)
        if section:
            in_section = section.group(1).strip().casefold() != "exp_fd_file"
            continue
        if re.fullmatch(r"\s*((?:EX|EM)\d+)\s*:\s*.*", line, re.IGNORECASE):
            in_section = True
            continue
        colon_header = re.fullmatch(r"\s*([^:=]+?)\s*:\s*(.*)", line)
        if colon_header:
            header_name = colon_header.group(1).strip()
            if not colon_header.group(2).strip() or header_name.isupper():
                in_section = False
                continue
        if in_section:
            continue
        key_value = _split_key_value(line)
        if key_value is None:
            continue
        key, value = key_value
        key = key.strip()
        if key:
            _store_unique_value(values, key, value.strip(), "global")
    return values


def _parse_sections(text: str) -> dict[str, dict[str, str]]:
    sections: dict[str, dict[str, str]] = {}
    current: dict[str, str] | None = None
    current_name: str | None = None
    for line in text.splitlines():
        section = re.fullmatch(r"\s*\[([^\]]+)\]\s*", line)
        if section:
            current_name = section.group(1).strip().casefold()
            current = sections.setdefault(current_name, {})
            continue
        optical_section = re.fullmatch(r"\s*((?:EX|EM)\d+)\s*:\s*.*", line, re.IGNORECASE)
        if optical_section:
            current_name = optical_section.group(1).casefold()
            current = sections.setdefault(current_name, {})
            continue
        colon_header = re.fullmatch(r"\s*([^:=]+?)\s*:\s*(.*)", line)
        if colon_header:
            header_name = colon_header.group(1).strip()
            if not colon_header.group(2).strip() or header_name.isupper():
                current = None
                current_name = None
                continue
        if current is None:
            continue
        key_value = _split_key_value(line)
        if key_value is None:
            continue
        key, value = key_value
        key = key.strip()
        if key.casefold() in {"corrected", "detector", "units"}:
            continue
        if key:
            _store_unique_value(current, key, value.strip(), current_name or "section")
    return sections


def _store_unique_value(
    values: dict[str, str],
    key: str,
    value: str,
    scope: str,
) -> None:
    existing_key = next(
        (candidate for candidate in values if candidate.casefold() == key.casefold()),
        None,
    )
    if existing_key is None:
        values[key] = value
        return
    existing_value = values[existing_key]
    if existing_value and value and existing_value != value:
        raise NoteParseError(f"Conflicting Note field {existing_key!r} in {scope}")
    if value and not existing_value:
        values[existing_key] = value


def _split_key_value(line: str) -> tuple[str, str] | None:
    if "=" in line:
        key, value = line.split("=", 1)
    elif ":" in line:
        key, value = line.split(":", 1)
    else:
        return None
    return key, value


def _parse_delay(values: dict[str, str]) -> DelayParameters:
    required = ("Flash Delay", "Sample Window", "Time per Flash", "Flash Count")
    missing = [key for key in required if not _case_insensitive_value(values, key)]
    if missing:
        raise NoteParseError(f"Missing delayed Note fields: {missing}")
    return DelayParameters(
        flash_delay=_case_insensitive_value(values, "Flash Delay"),
        sample_window=_case_insensitive_value(values, "Sample Window"),
        time_per_flash=_case_insensitive_value(values, "Time per Flash"),
        flash_count=_case_insensitive_value(values, "Flash Count"),
    )


def _first(values: dict[str, str], key: str) -> str | None:
    value = _case_insensitive_value(values, key)
    return value if value else None


def _case_insensitive_value(values: dict[str, str], key: str) -> str | None:
    value = values.get(key)
    if value:
        return value
    wanted = key.casefold()
    for candidate, candidate_value in values.items():
        if candidate.casefold() == wanted and candidate_value:
            return candidate_value
    return None


def _parse_range(value: str | None) -> tuple[str, str] | None:
    if value is None:
        return None
    match = re.fullmatch(r"\s*(.+?)\s*(?<![eE])-\s*(.+?)\s*", value)
    if not match:
        raise NoteParseError(f"Invalid wavelength range: {value}")
    return (
        _strip_expected_unit(match.group(1), "nm"),
        _strip_expected_unit(match.group(2), "nm"),
    )


def _section_range(values: dict[str, str]) -> tuple[str, str] | None:
    start = _measurement_value(values, "Start", "nm")
    end = _measurement_value(values, "End", "nm")
    if start is None and end is None:
        return None
    if start is None or end is None:
        raise NoteParseError("Wavelength section requires both Start and End")
    return (start, end)


def _section_slits(
    values: dict[str, str],
    section_name: str,
) -> tuple[str, str] | None:
    entrance = _measurement_value(values, "Front Entrance Slit", "nmBandpass")
    exit_slit = _measurement_value(values, "Front Exit Slit", "nmBandpass")
    if entrance is None and exit_slit is None:
        return None
    if entrance is None or exit_slit is None:
        raise NoteParseError("Wavelength section requires both Front Entrance Slit and Front Exit Slit")
    try:
        entrance_number = parse_metadata_decimal(entrance)
        exit_number = parse_metadata_decimal(exit_slit)
    except ValueError:
        pass
    else:
        if entrance_number != exit_number:
            raise NoteParseError(
                f"{section_name} entrance and exit slit values conflict"
            )
    return (entrance, exit_slit)


def _measurement_value(values: dict[str, str], key: str, unit: str) -> str | None:
    value = _first(values, key)
    return None if value is None else _strip_expected_unit(value, unit)


def _strip_expected_unit(value: str, unit: str) -> str:
    match = re.fullmatch(rf"\s*(.*?)\s*{re.escape(unit)}\s*", value, re.IGNORECASE)
    if match and match.group(1).strip():
        return match.group(1).strip()
    return value.strip()
