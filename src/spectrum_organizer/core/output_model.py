from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import re
from typing import Any

from spectrum_organizer.core.audit_details import identity_discriminator
from spectrum_organizer.core.metadata_numeric import (
    format_metadata_decimal,
    parse_metadata_decimal,
)
from spectrum_organizer.domain.models import SpectrumClass
from spectrum_organizer.safety.name_policy import (
    preflight_generated_names,
)


@dataclass(frozen=True)
class OutputSpectrum:
    spectrum_id: str
    spectrum_class: SpectrumClass
    canonical_sample_label: str
    sample_system_label: str
    temperature: str
    key_wavelength: str
    x_y: tuple[tuple[Any, Any], ...]
    excitation_slit: str | tuple[str, ...] | None = None
    emission_slit: str | tuple[str, ...] | None = None
    flash_delay: str | None = None
    sample_window: str | None = None
    time_per_flash: str | None = None
    flash_count: str | None = None
    scan_start: str | None = None
    scan_stop: str | None = None
    scan_step: str | None = None
    selection_order: int = 0
    sample_system_identity: str | None = None

    def __post_init__(self) -> None:
        converted_x_y = tuple(
            (_decimal(x), _decimal(y))
            for x, y in self.x_y
        )
        x_values = tuple(x for x, _y in converted_x_y)
        if len(x_values) != len(set(x_values)):
            raise ValueError(
                f"duplicate X values in spectrum {self.spectrum_id}"
            )
        object.__setattr__(self, "x_y", converted_x_y)
        _validate_single_slit_value(
            self.excitation_slit,
            "excitation_slit",
        )
        _validate_single_slit_value(
            self.emission_slit,
            "emission_slit",
        )
        if self.sample_system_identity is None:
            object.__setattr__(
                self,
                "sample_system_identity",
                self.sample_system_label,
            )

    @property
    def family(self) -> str:
        if self.spectrum_class in {SpectrumClass.STEADY_EMISSION, SpectrumClass.STEADY_EXCITATION}:
            return "F"
        if self.spectrum_class in {SpectrumClass.DELAYED_EMISSION, SpectrumClass.DELAYED_EXCITATION}:
            return "P"
        raise ValueError(f"unsupported output spectrum class: {self.spectrum_class.value}")

    @property
    def is_emission(self) -> bool:
        return self.spectrum_class in {SpectrumClass.STEADY_EMISSION, SpectrumClass.DELAYED_EMISSION}

    @property
    def is_excitation(self) -> bool:
        return self.spectrum_class in {SpectrumClass.STEADY_EXCITATION, SpectrumClass.DELAYED_EXCITATION}


@dataclass(frozen=True)
class OutputSpectrumRef:
    family: str
    side: str
    canonical_sample_label: str
    sample_system_identity: str
    temperature: str


@dataclass(frozen=True)
class OutputColumn:
    kind: str
    comment: str
    values: tuple[Decimal | None, ...]
    method: str | None = None
    formula: str | None = None
    short_name: str | None = None
    source: OutputSpectrumRef | None = None


@dataclass(frozen=True)
class OutputBook:
    display_name: str
    columns: tuple[OutputColumn, ...]

    @property
    def raw_y_columns(self) -> tuple[OutputColumn, ...]:
        return tuple(column for column in self.columns if column.kind == "raw_y")


@dataclass(frozen=True)
class OutputFolder:
    name: str
    family: str
    is_fallback: bool
    books: tuple[OutputBook, ...]


@dataclass(frozen=True)
class IncompleteFolder:
    folder_name: str
    represented_labels: tuple[str, ...]
    missing_labels: tuple[str, ...]


@dataclass(frozen=True)
class OutputPlan:
    folders: tuple[OutputFolder, ...]
    incomplete_folders: tuple[IncompleteFolder, ...]

    def folder(self, name: str) -> OutputFolder:
        for folder in self.folders:
            if folder.name == name:
                return folder
        raise KeyError(name)


@dataclass(frozen=True)
class _FolderKey:
    family: str
    is_fallback: bool
    key_wavelength: str
    excitation_slit: str
    emission_slit: str
    flash_delay: str | None = None
    sample_window: str | None = None
    time_per_flash: str | None = None
    flash_count: str | None = None


def build_output_plan(spectra: tuple[OutputSpectrum, ...]) -> OutputPlan:
    indexed = tuple(_with_order(spectrum, index) for index, spectrum in enumerate(spectra))
    book_long_names = _book_long_names_by_identity(indexed)
    emissions = tuple(spectrum for spectrum in indexed if spectrum.is_emission)
    excitations = tuple(spectrum for spectrum in indexed if spectrum.is_excitation)

    baseline = _family_baselines(emissions, excitations)
    folders: list[OutputFolder] = []
    incomplete: list[IncompleteFolder] = []

    for key, folder_emissions in _groups(emissions, _emission_folder_key).items():
        books = _books_for_folder(
            folder_emissions,
            tuple(
                excitation
                for excitation in excitations
                if any(
                    _compatible(excitation, emission)
                    for emission in folder_emissions
                )
            ),
            book_long_names,
        )
        if not books:
            continue
        family_baseline = baseline[key.family]
        represented_keys = {
            _sample_state_key(spectrum)
            for spectrum in folder_emissions
        }
        label_counts = _label_counts(family_baseline.values())
        represented = _sorted_labels(
            _state_display_label(
                state_key,
                family_baseline[state_key],
                label_counts,
            )
            for state_key in represented_keys
        )
        missing = _sorted_labels(
            _state_display_label(
                state_key,
                label,
                label_counts,
            )
            for state_key, label in family_baseline.items()
            if state_key not in represented_keys
        )
        name = _folder_name(key)
        if family_baseline and not missing:
            name += "_ALL_SAMPLES"
        folder = OutputFolder(name, key.family, False, books)
        folders.append(folder)
        if missing:
            incomplete.append(IncompleteFolder(name, represented, missing))

    orphan_excitations = tuple(ex for ex in excitations if not any(_compatible(ex, em) for em in emissions))
    for key, folder_excitations in _groups(orphan_excitations, _fallback_folder_key).items():
        books = _books_for_folder(
            (),
            folder_excitations,
            book_long_names,
        )
        if books:
            folders.append(OutputFolder(_folder_name(key), key.family, True, books))

    folders.sort(key=_folder_sort_key)
    folder_order = {
        folder.name: index
        for index, folder in enumerate(folders)
    }
    incomplete.sort(
        key=lambda entry: folder_order[entry.folder_name]
    )
    preflight_generated_names(
        folder_names=tuple(folder.name for folder in folders),
        book_display_names=tuple(book.display_name for folder in folders for book in folder.books),
    )
    return OutputPlan(tuple(folders), tuple(incomplete))


def _with_order(spectrum: OutputSpectrum, index: int) -> OutputSpectrum:
    if spectrum.selection_order != 0:
        return spectrum
    return OutputSpectrum(
        spectrum_id=spectrum.spectrum_id,
        spectrum_class=spectrum.spectrum_class,
        canonical_sample_label=spectrum.canonical_sample_label,
        sample_system_label=spectrum.sample_system_label,
        temperature=spectrum.temperature,
        key_wavelength=spectrum.key_wavelength,
        x_y=spectrum.x_y,
        excitation_slit=spectrum.excitation_slit,
        emission_slit=spectrum.emission_slit,
        flash_delay=spectrum.flash_delay,
        sample_window=spectrum.sample_window,
        time_per_flash=spectrum.time_per_flash,
        flash_count=spectrum.flash_count,
        scan_start=spectrum.scan_start,
        scan_stop=spectrum.scan_stop,
        scan_step=spectrum.scan_step,
        selection_order=index,
        sample_system_identity=spectrum.sample_system_identity,
    )


def _family_baselines(
    emissions: tuple[OutputSpectrum, ...],
    excitations: tuple[OutputSpectrum, ...],
) -> dict[str, dict[tuple[str, str, str], str]]:
    labels: dict[str, dict[tuple[str, str, str], str]] = {
        "F": {},
        "P": {},
    }
    for emission in emissions:
        labels[emission.family][_sample_state_key(emission)] = (
            emission.canonical_sample_label
        )
    for excitation in excitations:
        if not any(_compatible(excitation, emission) for emission in emissions):
            labels[excitation.family][_sample_state_key(excitation)] = (
                excitation.canonical_sample_label
            )
    return labels


def _books_for_folder(
    emissions: tuple[OutputSpectrum, ...],
    excitations: tuple[OutputSpectrum, ...],
    book_long_names: dict[str, str],
) -> tuple[OutputBook, ...]:
    identities = {
        str(spectrum.sample_system_identity)
        for spectrum in (*emissions, *excitations)
    }
    books = []
    display_identities = tuple(
        (book_long_names[identity], identity)
        for identity in identities
    )
    for display_name, identity in sorted(
        display_identities,
        key=lambda item: (_natural_key(item[0]), item[1]),
    ):
        book_emissions = tuple(
            spectrum
            for spectrum in emissions
            if spectrum.sample_system_identity == identity
        )
        book_excitations = tuple(
            spectrum
            for spectrum in excitations
            if spectrum.sample_system_identity == identity
        )
        columns = _columns_for_book(book_emissions, book_excitations)
        if columns:
            books.append(
                OutputBook(
                    display_name,
                    columns,
                )
            )
    return tuple(books)


def _columns_for_book(emissions: tuple[OutputSpectrum, ...], excitations: tuple[OutputSpectrum, ...]) -> tuple[OutputColumn, ...]:
    columns: list[OutputColumn] = []
    if emissions:
        _append_side_columns(columns, _sort_emissions(emissions), x_comment="Em", comment_for=_emission_comment)
    if excitations:
        _append_side_columns(columns, _sort_excitations(excitations), x_comment="Ex", comment_for=_excitation_comment)
    return tuple(columns)


def _append_side_columns(columns: list[OutputColumn], spectra: tuple[OutputSpectrum, ...], *, x_comment: str, comment_for) -> None:
    x_values = tuple(sorted({x for spectrum in spectra for x, _y in spectrum.x_y}))
    columns.append(OutputColumn("x", x_comment, x_values))

    raw_columns: list[tuple[int, OutputSpectrum, OutputColumn]] = []
    comments = _column_comments(spectra, comment_for, distinguish_collisions=x_comment == "Ex")
    for spectrum, comment in zip(spectra, comments, strict=True):
        y_by_x = dict(spectrum.x_y)
        values = tuple(y_by_x.get(x) for x in x_values)
        source = OutputSpectrumRef(
            family=spectrum.family,
            side="emission" if spectrum.is_emission else "excitation",
            canonical_sample_label=spectrum.canonical_sample_label,
            sample_system_identity=str(spectrum.sample_system_identity),
            temperature=spectrum.temperature,
        )
        raw = OutputColumn("raw_y", comment, values, source=source)
        raw_columns.append((len(columns) + 1, spectrum, raw))
        columns.append(raw)

    for raw_position, spectrum, raw in raw_columns:
        raw_letter = _column_letter(raw_position)
        max_y = max(value for value in raw.values if value is not None)
        if max_y <= 0:
            raise ValueError(
                f"{spectrum.spectrum_id} selected raw Y maximum {max_y}; normalization is invalid"
            )
        norm_values = tuple(None if value is None else value / max_y for value in raw.values)
        columns.append(
            OutputColumn(
                "norm_y",
                f"{raw.comment}_Norm",
                norm_values,
                method=f"Divided by Max of {raw_letter}",
                formula=f"col({raw_letter})/max(col({raw_letter}))",
                source=raw.source,
            )
        )


def _column_comments(
    spectra: tuple[OutputSpectrum, ...],
    comment_for,
    *,
    distinguish_collisions: bool,
) -> tuple[str, ...]:
    comments = [comment_for(spectrum) for spectrum in spectra]
    if not distinguish_collisions:
        return tuple(comments)
    groups: dict[str, list[int]] = {}
    for index, comment in enumerate(comments):
        groups.setdefault(comment, []).append(index)
    for indices in groups.values():
        if len(indices) < 2:
            continue
        suffixes = ["" for _index in indices]
        for label, field_name in (
            ("ExSlit", "excitation_slit"),
            ("EmSlit", "emission_slit"),
            ("FC", "flash_count"),
            ("ExStart", "scan_start"),
            ("ExStop", "scan_stop"),
            ("ExStep", "scan_step"),
        ):
            values = [
                _comment_discriminator_value(
                    spectra[index],
                    field_name,
                )
                for index in indices
            ]
            if len(set(values)) == 1:
                continue
            suffixes = [
                f"{suffix}_{label}{value}"
                for suffix, value in zip(suffixes, values, strict=True)
            ]
            if len(set(suffixes)) == len(suffixes):
                break
        if len(set(suffixes)) != len(suffixes):
            collision = comments[indices[0]]
            raise ValueError(f"selected excitation comments remain ambiguous: {collision}")
        for index, suffix in zip(indices, suffixes, strict=True):
            comments[index] += suffix
    return tuple(comments)


def _comment_discriminator_value(
    spectrum: OutputSpectrum,
    field_name: str,
) -> str:
    value = getattr(spectrum, field_name)
    if field_name in {"excitation_slit", "emission_slit"}:
        return _slit_text(value, field_name=field_name)
    return _number_text(value, field_name=field_name)


def _emission_comment(spectrum: OutputSpectrum) -> str:
    return f"{spectrum.canonical_sample_label}_{spectrum.family}{_number_text(spectrum.key_wavelength, field_name='key_wavelength')}"


def _excitation_comment(spectrum: OutputSpectrum) -> str:
    return f"{spectrum.canonical_sample_label}_{spectrum.family}Ex{_number_text(spectrum.key_wavelength, field_name='key_wavelength')}"


def _compatible(excitation: OutputSpectrum, emission: OutputSpectrum) -> bool:
    if not excitation.is_excitation or not emission.is_emission:
        return False
    if excitation.family != emission.family:
        return False
    if excitation.canonical_sample_label != emission.canonical_sample_label:
        return False
    if (
        excitation.sample_system_identity
        != emission.sample_system_identity
        or excitation.temperature != emission.temperature
    ):
        return False
    if excitation.family == "F":
        return True
    return (
        _number_text(excitation.flash_delay, field_name="flash_delay") == _number_text(emission.flash_delay, field_name="flash_delay")
        and _number_text(excitation.sample_window, field_name="sample_window") == _number_text(emission.sample_window, field_name="sample_window")
        and _number_text(excitation.time_per_flash, field_name="time_per_flash") == _number_text(emission.time_per_flash, field_name="time_per_flash")
    )


def _emission_folder_key(spectrum: OutputSpectrum) -> _FolderKey:
    return _FolderKey(
        spectrum.family,
        False,
        _number_text(spectrum.key_wavelength, field_name="key_wavelength"),
        _slit_text(spectrum.excitation_slit, field_name="excitation_slit"),
        _slit_text(spectrum.emission_slit, field_name="emission_slit"),
        _number_text(spectrum.flash_delay, field_name="flash_delay") if spectrum.family == "P" else None,
        _number_text(spectrum.sample_window, field_name="sample_window") if spectrum.family == "P" else None,
        _number_text(spectrum.time_per_flash, field_name="time_per_flash") if spectrum.family == "P" else None,
        _number_text(spectrum.flash_count, field_name="flash_count") if spectrum.family == "P" else None,
    )


def _fallback_folder_key(spectrum: OutputSpectrum) -> _FolderKey:
    return _FolderKey(
        spectrum.family,
        True,
        _number_text(spectrum.key_wavelength, field_name="key_wavelength"),
        _slit_text(spectrum.excitation_slit, field_name="excitation_slit"),
        _slit_text(spectrum.emission_slit, field_name="emission_slit"),
        _number_text(spectrum.flash_delay, field_name="flash_delay") if spectrum.family == "P" else None,
        _number_text(spectrum.sample_window, field_name="sample_window") if spectrum.family == "P" else None,
        _number_text(spectrum.time_per_flash, field_name="time_per_flash") if spectrum.family == "P" else None,
        _number_text(spectrum.flash_count, field_name="flash_count") if spectrum.family == "P" else None,
    )


def _folder_name(key: _FolderKey) -> str:
    wavelength_prefix = "Em" if key.is_fallback else "Ex"
    name = f"{key.family}_{wavelength_prefix}{key.key_wavelength}_ExSlit{key.excitation_slit}_EmSlit{key.emission_slit}"
    if key.family == "P":
        name += f"_FD{key.flash_delay}_SW{key.sample_window}_TPF{key.time_per_flash}_FC{key.flash_count}"
    return name


def _folder_sort_key(folder: OutputFolder) -> tuple[object, ...]:
    name = folder.name.removesuffix("_ALL_SAMPLES")
    parts = name.split("_")
    delayed = folder.family == "P"
    values = [
        0 if folder.family == "F" else 1,
        1 if folder.is_fallback else 0,
        _sort_number(parts[1][2:]),
        _sort_slit_number(parts[2].removeprefix("ExSlit")),
        _sort_slit_number(parts[3].removeprefix("EmSlit")),
    ]
    if delayed:
        values.extend(
            _sort_number(part[3:] if part.startswith("TPF") else part[2:])
            for part in parts[4:8]
        )
    return tuple(values)


def _sort_emissions(spectra: tuple[OutputSpectrum, ...]) -> tuple[OutputSpectrum, ...]:
    return tuple(sorted(spectra, key=lambda spectrum: (_temperature_key(spectrum.temperature), spectrum.selection_order)))


def _sort_excitations(spectra: tuple[OutputSpectrum, ...]) -> tuple[OutputSpectrum, ...]:
    return tuple(
        sorted(
            spectra,
            key=lambda spectrum: (
                _temperature_key(spectrum.temperature),
                _required_sort_number(spectrum.key_wavelength, "key_wavelength"),
                _required_slit_sort(spectrum.excitation_slit, "excitation_slit"),
                _required_slit_sort(spectrum.emission_slit, "emission_slit"),
                _required_sort_number(spectrum.flash_count, "flash_count") if spectrum.family == "P" else Decimal(0),
                spectrum.selection_order,
            ),
        )
    )


def _groups(spectra: tuple[OutputSpectrum, ...], key_for) -> dict[_FolderKey, tuple[OutputSpectrum, ...]]:
    groups: dict[_FolderKey, list[OutputSpectrum]] = {}
    for spectrum in spectra:
        groups.setdefault(key_for(spectrum), []).append(spectrum)
    return {key: tuple(group) for key, group in groups.items()}


def _sorted_labels(labels) -> tuple[str, ...]:
    return tuple(sorted(labels, key=_natural_key))


def _sample_state_key(
    spectrum: OutputSpectrum,
) -> tuple[str, str, str]:
    return (
        str(spectrum.sample_system_identity),
        spectrum.temperature,
        spectrum.canonical_sample_label,
    )


def _label_counts(labels) -> dict[str, int]:
    counts: dict[str, int] = {}
    for label in labels:
        counts[label] = counts.get(label, 0) + 1
    return counts


def _book_long_names_by_identity(
    spectra: tuple[OutputSpectrum, ...],
) -> dict[str, str]:
    long_name_by_identity: dict[str, str] = {}
    identity_by_long_name: dict[str, str] = {}
    for spectrum in spectra:
        identity = str(spectrum.sample_system_identity)
        long_name = spectrum.sample_system_label
        previous_long_name = long_name_by_identity.setdefault(
            identity,
            long_name,
        )
        if previous_long_name != long_name:
            raise ValueError(
                "sample system identity has conflicting Book Long Names: "
                f"{identity!r}"
            )
        previous_identity = identity_by_long_name.setdefault(
            long_name,
            identity,
        )
        if previous_identity != identity:
            raise ValueError(
                "Book Long Name maps to multiple sample system identities: "
                f"{long_name!r}"
            )
    return long_name_by_identity


def _state_display_label(
    state_key: tuple[str, str, str],
    label: str,
    label_counts: dict[str, int],
) -> str:
    if label_counts[label] == 1:
        return label
    identity, temperature, _canonical_label = state_key
    return (
        f"{label} ["
        f"{identity_discriminator(identity)}; "
        f"temperature={temperature}]"
    )


def _natural_key(
    value: str,
) -> tuple[tuple[tuple[int, int | str], ...], str]:
    tokens = tuple(
        (0, int(part))
        if part.isascii() and part.isdigit()
        else (1, part.casefold())
        for part in re.split(r"([0-9]+)", value)
    )
    return tokens, value


def _temperature_key(value: str) -> tuple[Decimal, str]:
    match = re.search(r"[-+]?\d+(?:\.\d+)?", value)
    if not match:
        return (Decimal("Infinity"), value)
    return (_decimal(match.group(0)), value)


def _sort_number(value: str | None) -> Decimal | str:
    if value is None:
        return ""
    try:
        return _decimal(value)
    except Exception:
        return str(value)


def _sort_slit_number(
    value: str | tuple[str, ...] | None,
) -> tuple[Decimal, ...]:
    return _required_slit_sort(value, "slit")


def _required_slit_sort(
    value: str | tuple[str, ...] | None,
    field_name: str,
) -> tuple[Decimal, ...]:
    text = _slit_text(value, field_name=field_name)
    return tuple(
        _metadata_decimal(part, field_name)
        for part in text.split("-")
    )


def _required_sort_number(value: str | None, field_name: str) -> Decimal:
    return _metadata_decimal(value, field_name)


def _number_text(value: str | None, *, field_name: str | None = None) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if field_name is not None:
        decimal = _metadata_decimal(text, field_name)
    else:
        try:
            decimal = _decimal(text)
        except Exception:
            return text
    return format_metadata_decimal(decimal)


def _slit_text(
    value: str | tuple[str, ...] | None,
    *,
    field_name: str,
) -> str:
    if isinstance(value, tuple):
        parts = tuple(str(part).strip() for part in value)
        text = "-".join(parts)
    else:
        text = "" if value is None else str(value).strip()
        try:
            _metadata_decimal(text, field_name)
        except ValueError:
            parts = tuple(text.split("-"))
        else:
            parts = (text,)
    if not text or len(parts) > 2 or any(not part for part in parts):
        raise ValueError(
            f"{field_name} must be numeric for generated output metadata: {text!r}"
        )
    normalized = tuple(
        format_metadata_decimal(
            _metadata_decimal(
                part,
                field_name,
                nonnegative=True,
            )
        )
        for part in parts
    )
    if len(set(normalized)) == 1:
        return normalized[0]
    return "-".join(normalized)


def _validate_single_slit_value(
    value: str | tuple[str, ...] | None,
    field_name: str,
) -> None:
    if value is None:
        return
    parts = value if isinstance(value, tuple) else (value,)
    try:
        normalized = {
            format_metadata_decimal(
                _metadata_decimal(
                    str(part).strip(),
                    field_name,
                    nonnegative=True,
                )
            )
            for part in parts
        }
    except ValueError as exc:
        raise ValueError(
            f"{field_name} must contain one semantic value: {value!r}"
        ) from exc
    if len(parts) > 2 or len(normalized) != 1:
        raise ValueError(
            f"{field_name} must contain one semantic value: {value!r}"
        )


def _metadata_decimal(
    value: str | None,
    field_name: str,
    *,
    nonnegative: bool = False,
) -> Decimal:
    text = "" if value is None else str(value).strip()
    try:
        return parse_metadata_decimal(
            text,
            nonnegative=nonnegative,
        )
    except ValueError as exc:
        raise ValueError(
            f"{field_name} is outside supported metadata numeric domain: "
            f"{text!r} ({exc})"
        ) from None


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value).strip())


def _column_letter(position: int) -> str:
    letters = ""
    while position:
        position, remainder = divmod(position - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters
