from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
from typing import Mapping

from spectrum_organizer.domain.models import (
    DopedSample,
    LiquidSample,
    NeatSample,
    canonicalize_oxygen_environment,
)
from spectrum_organizer.domain.normalization import (
    extract_concentration_evidence,
    normalize_concentration_input,
    normalize_temperature,
)
from spectrum_organizer.safety.name_policy import validate_user_origin_name_text


@dataclass(frozen=True)
class AttributionBook:
    source_id: str
    folder_path: str
    book_name: str
    page_type: str = "worksheet"
    valid: bool = True
    mixed_folder: bool = False

    @property
    def book_key(self) -> str:
        return json.dumps(
            [self.source_id, self.page_type, self.folder_path, self.book_name],
            ensure_ascii=False,
            separators=(",", ":"),
        )

    @property
    def is_root(self) -> bool:
        return self.folder_path in {"", "/"}


@dataclass(frozen=True)
class AttributionFields:
    sample: object


@dataclass(frozen=True)
class AttributionTarget:
    scope: str
    source_id: str
    folder_path: str | None
    book_keys: tuple[str, ...]
    cache_key: str | None = None
    prefill: Mapping[str, str] = field(default_factory=dict)
    confirmed: bool = False


class AttributionCache:
    def __init__(self) -> None:
        self._latest_by_key: dict[str, AttributionFields] = {}

    def remember(self, folder_path: str, attribution: AttributionFields) -> None:
        self._latest_by_key[_folder_cache_key(folder_path)] = attribution

    def lookup(self, folder_path: str) -> AttributionFields | None:
        return self._latest_by_key.get(_folder_cache_key(folder_path))

    def restore(self, folder_path: str, attribution: AttributionFields | None) -> None:
        key = _folder_cache_key(folder_path)
        if attribution is None:
            self._latest_by_key.pop(key, None)
        else:
            self._latest_by_key[key] = attribution


class AttributionSession:
    def __init__(self, targets: list[AttributionTarget]) -> None:
        self.targets = list(targets)
        self._assignments: dict[str, AttributionFields] = {}
        self._confirmed_scopes: dict[str, str] = {}

    def confirm(
        self,
        book_key: str,
        attribution: AttributionFields,
        *,
        apply_to_remaining_folder: bool = False,
    ) -> None:
        target = self._target_for_book(book_key)
        keys = target.book_keys
        if apply_to_remaining_folder:
            keys = tuple(
                key
                for candidate in self.targets
                if candidate.source_id == target.source_id and candidate.folder_path == target.folder_path
                for key in candidate.book_keys
                if key not in self._assignments
            )
        for key in keys:
            self._assignments[key] = attribution
            self._confirmed_scopes[key] = target.scope

    def assignment_for(self, book_key: str) -> AttributionFields | None:
        return self._assignments.get(book_key)

    @property
    def assignments(self) -> Mapping[str, AttributionFields]:
        return dict(self._assignments)

    def replace_assignments(
        self,
        book_keys: tuple[str, ...],
        attribution: AttributionFields,
        *,
        scope: str | None = None,
    ) -> None:
        if not book_keys or len(book_keys) != len(set(book_keys)):
            raise ValueError("Replacement attribution scope is invalid")
        if scope not in {None, "book", "folder"}:
            raise ValueError("Replacement attribution scope is invalid")
        for book_key in book_keys:
            self._target_for_book(book_key)
            if book_key not in self._assignments:
                raise KeyError(book_key)
        for book_key in book_keys:
            self._assignments[book_key] = attribution
            if scope is not None:
                self._confirmed_scopes[book_key] = scope

    def confirmed_scope_for(self, book_key: str) -> str:
        self._target_for_book(book_key)
        return self._confirmed_scopes.get(book_key, "")

    def reopen(
        self,
        book_keys: tuple[str, ...],
    ) -> tuple[tuple[str, ...], Mapping[str, AttributionFields]]:
        reopened: list[str] = []
        for book_key in book_keys:
            target = self._target_for_book(book_key)
            for target_key in target.book_keys:
                if target_key not in reopened:
                    reopened.append(target_key)
        previous = {
            book_key: self._assignments.pop(book_key)
            for book_key in reopened
            if book_key in self._assignments
        }
        for book_key in reopened:
            self._confirmed_scopes.pop(book_key, None)
        return tuple(reopened), previous

    def split_folder(self, target: AttributionTarget) -> list[AttributionTarget]:
        split = split_folder_target(target)
        index = self.targets.index(target)
        self.targets[index : index + 1] = split
        return split

    def restore_folder(self, target: AttributionTarget) -> None:
        split = [
            candidate
            for candidate in self.targets
            if candidate.scope == "book"
            and candidate.source_id == target.source_id
            and candidate.folder_path == target.folder_path
            and candidate.book_keys[0] in target.book_keys
        ]
        if len(split) != len(target.book_keys):
            raise ValueError("Folder attribution target is not fully split")
        index = min(self.targets.index(candidate) for candidate in split)
        self.targets[index : index + len(split)] = [target]
        for book_key in target.book_keys:
            self._assignments.pop(book_key, None)
            self._confirmed_scopes.pop(book_key, None)

    def _target_for_book(self, book_key: str) -> AttributionTarget:
        for target in self.targets:
            if book_key in target.book_keys:
                return target
        raise KeyError(book_key)


def build_attribution_targets(books: list[AttributionBook]) -> list[AttributionTarget]:
    surviving = [book for book in books if book.valid]
    folder_groups: dict[tuple[str, str], list[AttributionBook]] = {}
    targets: list[AttributionTarget] = []
    for book in surviving:
        if book.is_root or book.mixed_folder:
            targets.append(_book_target(book))
        else:
            folder_groups.setdefault((book.source_id, book.folder_path), []).append(book)
    for (source_id, folder_path), group in folder_groups.items():
        targets.append(
            AttributionTarget(
                scope="folder",
                source_id=source_id,
                folder_path=folder_path,
                book_keys=tuple(book.book_key for book in group),
                cache_key=_folder_cache_key(folder_path),
                prefill=_folder_prefill(folder_path),
            )
        )
    return sorted(targets, key=lambda target: target.book_keys[0])


def split_folder_target(target: AttributionTarget) -> list[AttributionTarget]:
    if target.scope != "folder" or target.folder_path is None:
        raise ValueError("Only a Folder attribution target can be split")
    return [
        AttributionTarget(
            scope="book",
            source_id=target.source_id,
            folder_path=target.folder_path,
            book_keys=(book_key,),
            prefill=dict(target.prefill),
        )
        for book_key in target.book_keys
    ]


def build_attribution_fields(sample_type: str, values: Mapping[str, str]) -> AttributionFields:
    normalized_type = str(sample_type).strip().casefold()
    cleaned = {
        name: validate_user_origin_name_text(str(value).strip(), field_name=name)
        for name, value in values.items()
        if name != "concentration_unit"
    }
    if normalized_type == "solution":
        concentration = normalize_concentration_input(
            cleaned.get("concentration", ""),
            "M",
            ("M",),
        ).full_text
        sample = LiquidSample(
            cleaned.get("sample", ""),
            cleaned.get("solvent", ""),
            concentration,
            normalize_temperature(cleaned.get("temperature", "")),
        )
    elif normalized_type == "solid":
        sample = NeatSample(
            cleaned.get("sample", ""),
            cleaned.get("state", ""),
            normalize_temperature(cleaned.get("temperature", "")),
            oxygen_environment=canonicalize_oxygen_environment(
                cleaned.get("oxygen_environment", "")
            ),
        )
    elif normalized_type == "doped":
        unit = str(values.get("concentration_unit", "")).strip()
        concentration = normalize_concentration_input(
            cleaned.get("concentration", ""),
            unit or None,
            ("wt%", "mol%"),
        ).full_text
        sample = DopedSample(
            cleaned.get("sample", ""),
            cleaned.get("host", ""),
            concentration,
            cleaned.get("state", ""),
            normalize_temperature(cleaned.get("temperature", "")),
            oxygen_environment=canonicalize_oxygen_environment(
                cleaned.get("oxygen_environment", "")
            ),
        )
    else:
        raise ValueError(f"Unsupported sample type: {sample_type}")
    validate_user_origin_name_text(sample.canonical_label, field_name="canonical sample label")
    return AttributionFields(sample=sample)


_ASCII_TOKEN = r"A-Za-z0-9"
_ROOM_TEMPERATURE_PATTERN = re.compile(
    rf"(?:"
    rf"(?<![{_ASCII_TOKEN}])RT(?![{_ASCII_TOKEN}])"
    rf"|(?<![{_ASCII_TOKEN}])room[\s_.-]*(?:temperature|temp)(?![{_ASCII_TOKEN}])"
    rf"|室温"
    rf")",
    re.IGNORECASE,
)
_KELVIN_TEMPERATURE_PATTERN = re.compile(
    r"(?<![0-9.])(?P<value>[+]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+))\s*K(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_EXPLICIT_TEMPERATURE_UNIT_PATTERN = re.compile(
    r"(?P<unit>[KCF])(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_OXYGEN_ENVIRONMENT_PATTERNS = {
    "Air": (
        re.compile(rf"(?<![{_ASCII_TOKEN}])air(?![{_ASCII_TOKEN}])", re.IGNORECASE),
        re.compile("空气"),
    ),
    "DeO2": (
        re.compile(rf"(?<![{_ASCII_TOKEN}])vacuum(?![{_ASCII_TOKEN}])", re.IGNORECASE),
        re.compile(rf"(?<![{_ASCII_TOKEN}])vac(?![{_ASCII_TOKEN}])", re.IGNORECASE),
        re.compile(rf"(?<![{_ASCII_TOKEN}])de[\s_.-]*o2(?![{_ASCII_TOKEN}])", re.IGNORECASE),
        re.compile(rf"(?<![{_ASCII_TOKEN}])de[\s_.-]*oxygenated(?![{_ASCII_TOKEN}])", re.IGNORECASE),
        re.compile(rf"(?<![{_ASCII_TOKEN}])de[\s_.-]*gassed(?![{_ASCII_TOKEN}])", re.IGNORECASE),
        re.compile(rf"(?<![{_ASCII_TOKEN}])de[\s_.-]*aerated(?![{_ASCII_TOKEN}])", re.IGNORECASE),
        re.compile(rf"(?<![{_ASCII_TOKEN}])oxygen[\s_.-]*free(?![{_ASCII_TOKEN}])", re.IGNORECASE),
        re.compile(rf"(?<![{_ASCII_TOKEN}])o2[\s_.-]*free(?![{_ASCII_TOKEN}])", re.IGNORECASE),
        re.compile("绝氧"),
        re.compile("真空"),
    ),
}


def infer_oxygen_environment(*labels: str) -> str:
    matches = _matched_oxygen_environments(labels)
    return next(iter(matches)) if len(matches) == 1 else ""


def reconcile_oxygen_environment_prefill(
    prefill: Mapping[str, str],
    *labels: str,
) -> dict[str, str]:
    merged = dict(prefill)
    existing = str(merged.get("oxygen_environment", "")).strip()
    if existing:
        try:
            existing = canonicalize_oxygen_environment(existing)
        except ValueError:
            existing = ""
            merged.pop("oxygen_environment", None)
        else:
            merged["oxygen_environment"] = existing
    matches = _matched_oxygen_environments(labels)
    if len(matches) > 1:
        merged.pop("oxygen_environment", None)
    elif len(matches) == 1:
        inferred = next(iter(matches))
        if existing and existing != inferred:
            merged.pop("oxygen_environment", None)
        else:
            merged["oxygen_environment"] = inferred
    return merged


def reconcile_temperature_prefill(
    prefill: Mapping[str, str],
    source_filename: str,
    folder_path: str = "",
    *,
    book_name: str = "",
) -> dict[str, str]:
    merged = dict(prefill)
    existing = str(merged.get("temperature", "")).strip()
    if existing:
        try:
            existing = normalize_temperature(existing)
        except ValueError:
            merged.pop("temperature", None)
        else:
            merged["temperature"] = existing

    labels = [_final_folder_name(source_filename), _final_folder_name(folder_path)]
    if book_name:
        labels.append(book_name)
    for label in labels:
        current, invalid = _temperature_evidence(label)
        if invalid or len(current) > 1:
            merged.pop("temperature", None)
        elif len(current) == 1:
            merged["temperature"] = next(iter(current))
    return merged


def reconcile_concentration_prefill(
    prefill: Mapping[str, str],
    source_filename: str,
    folder_path: str = "",
    *,
    book_name: str = "",
) -> dict[str, str]:
    merged = dict(prefill)
    solution_value = str(merged.get("solution_concentration", "")).strip()
    doped_value = str(merged.get("doped_concentration", "")).strip()
    doped_unit = str(merged.get("doped_concentration_unit", "")).strip()

    sample_type = str(merged.get("sample_type", "")).strip().casefold()
    if sample_type == "solution" and not solution_value:
        solution_value = str(merged.get("concentration", "")).strip()
    elif sample_type == "doped" and not doped_value:
        doped_value = str(merged.get("concentration", "")).strip()
        doped_unit = str(merged.get("concentration_unit", "")).strip()

    labels = [_final_folder_name(source_filename), _final_folder_name(folder_path)]
    if book_name:
        labels.append(book_name)
    for label in labels:
        entries, invalid_units = extract_concentration_evidence(label)
        solution_values = {entry.value_text for entry in entries if entry.unit == "M"}
        doped_values = {
            (entry.value_text, entry.unit)
            for entry in entries
            if entry.unit in {"wt%", "mol%"}
        }
        if "M" in invalid_units or len(solution_values) > 1:
            solution_value = "1×10^-4"
        elif solution_values:
            solution_value = next(iter(solution_values))
        if "percentage" in invalid_units or len(doped_values) > 1:
            doped_value = ""
            doped_unit = ""
        elif doped_values:
            doped_value, doped_unit = next(iter(doped_values))

    merged["solution_concentration"] = solution_value or "1×10^-4"
    if doped_value and doped_unit:
        merged["doped_concentration"] = doped_value
        merged["doped_concentration_unit"] = doped_unit
    else:
        merged.pop("doped_concentration", None)
        merged.pop("doped_concentration_unit", None)

    if sample_type == "solution":
        merged["concentration"] = merged["solution_concentration"]
        merged.pop("concentration_unit", None)
    elif sample_type == "doped":
        if doped_value and doped_unit:
            merged["concentration"] = doped_value
            merged["concentration_unit"] = doped_unit
        else:
            merged.pop("concentration", None)
            merged.pop("concentration_unit", None)
    return merged


def _matched_oxygen_environments(labels) -> set[str]:
    matches: set[str] = set()
    for label in labels:
        text = str(label or "")
        for environment, patterns in _OXYGEN_ENVIRONMENT_PATTERNS.items():
            if any(pattern.search(text) for pattern in patterns):
                matches.add(environment)
    return matches


def _temperature_evidence(label: str) -> tuple[set[str], bool]:
    matches: set[str] = set()
    invalid = False
    valid_unit_ends: set[int] = set()
    text = str(label or "")
    if _ROOM_TEMPERATURE_PATTERN.search(text):
        matches.add("298 K")
    for match in _KELVIN_TEMPERATURE_PATTERN.finditer(text):
        prefix = text[: match.start()]
        if _has_malformed_temperature_number_prefix(prefix):
            invalid = True
            continue
        try:
            temperature = normalize_temperature(match.group("value"))
        except ValueError:
            invalid = True
            continue
        matches.add(temperature)
        valid_unit_ends.add(match.end())

    for match in _EXPLICIT_TEMPERATURE_UNIT_PATTERN.finditer(text):
        if not _has_numeric_temperature_prefix(text, match.start()):
            continue
        if match.group("unit").casefold() != "k" or match.end() not in valid_unit_ends:
            invalid = True
    return matches, invalid


def _has_malformed_temperature_number_prefix(prefix: str) -> bool:
    if prefix.endswith("−"):
        return True
    if prefix.endswith("-") and (len(prefix) < 2 or not prefix[-2].isalnum()):
        return True
    return bool(
        re.search(
            r"(?:[0-9.]\s*[eE^*×xX]+\s*[+\-−⁺⁻]*|[0-9][⁺⁻]+)$",
            prefix,
        )
    )


def _has_numeric_temperature_prefix(text: str, unit_start: int) -> bool:
    prefix = text[:unit_start].rstrip()
    if prefix.endswith("°"):
        prefix = prefix[:-1].rstrip()
    return bool(
        re.search(
            r"[0-9⁰¹²³⁴⁵⁶⁷⁸⁹][0-9.\s+\-−eE^*×xX⁺⁻⁰¹²³⁴⁵⁶⁷⁸⁹]*$",
            prefix,
        )
    )


def commit_final_attributions(library, assignments: Mapping[str, AttributionFields]) -> dict[str, int]:
    unique_records: list[object] = []
    index_by_identity: dict[str, int] = {}
    book_identity: dict[str, str] = {}
    for book_key, attribution in assignments.items():
        record = attribution.sample
        identity = record.identity_json()
        book_identity[book_key] = identity
        if identity not in index_by_identity:
            index_by_identity[identity] = len(unique_records)
            unique_records.append(record)
    ids = library.save_final_records(unique_records)
    return {book_key: ids[index_by_identity[identity]] for book_key, identity in book_identity.items()}


def _book_target(book: AttributionBook) -> AttributionTarget:
    return AttributionTarget(
        scope="book",
        source_id=book.source_id,
        folder_path=None if book.is_root else book.folder_path,
        book_keys=(book.book_key,),
        cache_key=None,
        prefill={} if book.is_root else _folder_prefill(book.folder_path),
    )


def _folder_cache_key(folder_path: str) -> str:
    final_name = _final_folder_name(folder_path)
    return re.sub(r"[\s_-]+", "", final_name)


def _folder_prefill(folder_path: str) -> dict[str, str]:
    prefill = reconcile_temperature_prefill({}, "", folder_path)
    final_name = _final_folder_name(folder_path)
    entries, invalid_units = extract_concentration_evidence(final_name)
    solution_values = {entry.value_text for entry in entries if entry.unit == "M"}
    doped_values = {
        (entry.value_text, entry.unit)
        for entry in entries
        if entry.unit in {"wt%", "mol%"}
    }
    if "M" in invalid_units or len(solution_values) > 1:
        prefill["solution_concentration"] = "1×10^-4"
    elif len(solution_values) == 1:
        prefill["solution_concentration"] = next(iter(solution_values))
    if "percentage" not in invalid_units and len(doped_values) == 1:
        value, unit = next(iter(doped_values))
        prefill["doped_concentration"] = value
        prefill["doped_concentration_unit"] = unit
    return prefill


def _final_folder_name(folder_path: str) -> str:
    parts = tuple(part for part in re.split(r"[\\/]", folder_path) if part)
    return parts[-1] if parts else ""
