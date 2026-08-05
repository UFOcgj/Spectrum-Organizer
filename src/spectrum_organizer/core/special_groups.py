from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import json
from typing import Callable, Mapping

from spectrum_organizer.domain.models import SpectrumClass


DELAYED_2D_LABEL = "二维延迟谱"
DELAY_TIME_LABEL = "时间分辨延迟谱"
REGULAR_DELAYED_LABEL = "regular"
OVERLAP_CHOICES = (DELAYED_2D_LABEL, DELAY_TIME_LABEL, REGULAR_DELAYED_LABEL)


@dataclass(frozen=True)
class SpectrumBook:
    source_id: str
    folder_path: str
    book_name: str
    spectrum_class: SpectrumClass
    sample_label: str
    page_type: str = "worksheet"
    fixed_excitation_wavelength: str | None = None
    receiving_range: tuple[str, str] | None = None
    excitation_slit: str | None = None
    emission_slit: str | None = None
    flash_delay: str | None = None
    sample_window: str | None = None
    time_per_flash: str | None = None
    flash_count: str | None = None
    confirmed: bool = True

    @property
    def book_key(self) -> str:
        return json.dumps(
            (self.source_id, self.page_type, self.folder_path, self.book_name),
            ensure_ascii=False,
            separators=(",", ":"),
        )


def spectrum_book_point_identity(book: SpectrumBook) -> tuple[object, ...]:
    return (
        book.source_id,
        book.page_type,
        book.folder_path,
        book.sample_label,
        book.spectrum_class.value,
        _numeric_identity(book.fixed_excitation_wavelength),
        _numeric_tuple_identity(book.receiving_range),
        _numeric_identity(book.excitation_slit),
        _numeric_identity(book.emission_slit),
        _numeric_identity(book.flash_delay),
        _numeric_identity(book.sample_window),
        _numeric_identity(book.time_per_flash),
        _numeric_identity(book.flash_count),
    )


@dataclass(frozen=True)
class SpecialGroup:
    kind: str
    book_keys: tuple[str, ...]
    varying_points: tuple[tuple[str, ...], ...]
    confirmed: bool = True
    copy_to_output: bool = False
    adds_completeness_label: bool = False


@dataclass(frozen=True)
class PendingDuplicateReview:
    kind: str
    point_label: str
    book_keys: tuple[str, ...]
    choice_key: str
    context_book_keys: tuple[str, ...]


@dataclass(frozen=True)
class PendingOverlapAssignment:
    book_key: str
    choices: tuple[str, ...] = OVERLAP_CHOICES
    context_book_keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class SpecialGroupResult:
    groups: tuple[SpecialGroup, ...]
    regular_delayed_book_keys: tuple[str, ...]
    pending_duplicate_reviews: tuple[PendingDuplicateReview, ...] = ()
    pending_overlap_assignments: tuple[PendingOverlapAssignment, ...] = ()
    final_validation_runs: int = 0


@dataclass(frozen=True)
class _Candidate:
    kind: str
    books: tuple[SpectrumBook, ...]
    point_by_key: Mapping[str, tuple[str, ...]]
    source_keys: tuple[str, ...]


def classify_special_groups(
    books: list[SpectrumBook],
    *,
    duplicate_choices: Mapping[str, str] | None = None,
    overlap_choices: Mapping[str, str] | None = None,
    final_validation_passes: bool = True,
) -> SpecialGroupResult:
    duplicate_choices = duplicate_choices or {}
    overlap_choices = overlap_choices or {}
    delayed_books = [book for book in books if book.spectrum_class == SpectrumClass.DELAYED_EMISSION]
    delayed_keys = tuple(book.book_key for book in delayed_books)
    steady_groups = tuple(
        SpecialGroup("steady_2d", (book.book_key,), ())
        for book in books
        if book.spectrum_class == SpectrumClass.STEADY_2D and book.confirmed
    )

    pending_duplicates: list[PendingDuplicateReview] = []
    excluded_duplicate_keys: set[str] = set()
    candidates = _delayed_2d_candidates(
        delayed_books,
        duplicate_choices,
        pending_duplicates,
        excluded_duplicate_keys,
    )
    candidates.extend(
        _delay_time_candidates(
            delayed_books,
            duplicate_choices,
            pending_duplicates,
            excluded_duplicate_keys,
        )
    )
    if pending_duplicates:
        return SpecialGroupResult(steady_groups, (), pending_duplicate_reviews=tuple(pending_duplicates))

    pending_overlaps = _pending_overlaps(candidates, overlap_choices)
    if pending_overlaps:
        return SpecialGroupResult(steady_groups, (), pending_overlap_assignments=pending_overlaps)

    resolved = _trim_overlaps(candidates, overlap_choices)
    final_validation_runs = 1 if resolved else 0
    valid_candidates = []
    if final_validation_passes:
        for candidate in resolved:
            if _candidate_still_valid(candidate):
                valid_candidates.append(candidate)

    special_keys = {key for candidate in valid_candidates for key in candidate.point_by_key}
    groups = list(steady_groups)
    for candidate in valid_candidates:
        groups.append(SpecialGroup(candidate.kind, tuple(book.book_key for book in candidate.books), _ordered_points(candidate)))
    regular_keys = tuple(key for key in delayed_keys if key not in special_keys)
    return SpecialGroupResult(tuple(groups), regular_keys, final_validation_runs=final_validation_runs)


def resolve_special_group_selection(
    group: SpecialGroup,
    selected_book_keys: tuple[str, ...],
) -> tuple[SpecialGroup | None, tuple[str, ...]]:
    if len(selected_book_keys) != len(set(selected_book_keys)) or any(
        book_key not in group.book_keys for book_key in selected_book_keys
    ):
        raise ValueError("Special-group selection contains an invalid Book")
    selected = tuple(
        book_key
        for book_key in group.book_keys
        if book_key in selected_book_keys
    )
    if group.kind == "steady_2d":
        valid = bool(selected)
    elif group.kind in {"delayed_2d", "delay_time_series"}:
        point_by_key = dict(zip(group.book_keys, group.varying_points, strict=True))
        points = [point_by_key[book_key] for book_key in selected]
        valid = (
            _is_valid_delayed_2d_points(points)
            if group.kind == "delayed_2d"
            else _is_valid_delay_time_points(points)
        )
    else:
        raise ValueError(f"Unsupported special-group kind: {group.kind}")
    if not valid:
        return None, group.book_keys
    excluded = tuple(
        book_key for book_key in group.book_keys if book_key not in selected
    )
    points = ()
    if group.varying_points:
        point_by_key = dict(zip(group.book_keys, group.varying_points, strict=True))
        points = tuple(point_by_key[book_key] for book_key in selected)
    return (
        SpecialGroup(
            kind=group.kind,
            book_keys=selected,
            varying_points=points,
            confirmed=True,
            copy_to_output=False,
            adds_completeness_label=False,
        ),
        excluded,
    )


def _delayed_2d_candidates(
    books: list[SpectrumBook],
    duplicate_choices: Mapping[str, str],
    pending_duplicates: list[PendingDuplicateReview],
    excluded_duplicate_keys: set[str],
) -> list[_Candidate]:
    by_signature: dict[tuple[object, ...], list[SpectrumBook]] = {}
    for book in books:
        if book.fixed_excitation_wavelength is None:
            continue
        signature = (
            book.source_id,
            book.folder_path,
            book.sample_label,
            _numeric_tuple_identity(book.receiving_range),
            _numeric_identity(book.excitation_slit),
            _numeric_identity(book.emission_slit),
            _numeric_identity(book.flash_delay),
            _numeric_identity(book.sample_window),
            _numeric_identity(book.time_per_flash),
            _numeric_identity(book.flash_count),
        )
        by_signature.setdefault(signature, []).append(book)
    candidates = []
    for group in by_signature.values():
        candidate = _candidate_with_duplicate_review(
            "delayed_2d",
            group,
            lambda book: (book.fixed_excitation_wavelength or "",),
            duplicate_choices,
            pending_duplicates,
            excluded_duplicate_keys,
            min_points=5,
        )
        if candidate is not None:
            candidates.append(candidate)
    return candidates


def _delay_time_candidates(
    books: list[SpectrumBook],
    duplicate_choices: Mapping[str, str],
    pending_duplicates: list[PendingDuplicateReview],
    excluded_duplicate_keys: set[str],
) -> list[_Candidate]:
    by_signature: dict[tuple[object, ...], list[SpectrumBook]] = {}
    for book in books:
        if book.fixed_excitation_wavelength is None or book.flash_delay is None or book.time_per_flash is None:
            continue
        signature = (
            book.source_id,
            book.folder_path,
            book.sample_label,
            _numeric_identity(book.fixed_excitation_wavelength),
            _numeric_tuple_identity(book.receiving_range),
            _numeric_identity(book.excitation_slit),
            _numeric_identity(book.emission_slit),
            _numeric_identity(book.sample_window),
            _numeric_identity(book.flash_count),
        )
        by_signature.setdefault(signature, []).append(book)
    candidates = []
    for group in by_signature.values():
        candidate = _candidate_with_duplicate_review(
            "delay_time_series",
            group,
            lambda book: (book.flash_delay or "", book.time_per_flash or ""),
            duplicate_choices,
            pending_duplicates,
            excluded_duplicate_keys,
            min_points=3,
        )
        if candidate is not None:
            candidates.append(candidate)
    return candidates


def _candidate_with_duplicate_review(
    kind: str,
    books: list[SpectrumBook],
    point_for: Callable[[SpectrumBook], tuple[str, ...]],
    duplicate_choices: Mapping[str, str],
    pending_duplicates: list[PendingDuplicateReview],
    excluded_duplicate_keys: set[str],
    *,
    min_points: int,
) -> _Candidate | None:
    books = [
        book
        for book in books
        if book.book_key not in excluded_duplicate_keys
    ]
    context_book_keys = tuple(book.book_key for book in books)
    by_point: dict[tuple[object, ...], list[SpectrumBook]] = {}
    point_text: dict[tuple[object, ...], tuple[str, ...]] = {}
    for book in books:
        point = point_for(book)
        identity = tuple(_numeric_identity(value) for value in point)
        by_point.setdefault(identity, []).append(book)
        point_text.setdefault(identity, point)
    if len(by_point) < min_points:
        return None

    selected: list[SpectrumBook] = []
    point_by_key: dict[str, tuple[str, ...]] = {}
    unresolved = False
    for point_identity, point_books in by_point.items():
        point_label = "/".join(point_text[point_identity])
        if len(point_books) == 1:
            book = point_books[0]
        else:
            book_keys = tuple(book.book_key for book in point_books)
            choice_key = _duplicate_choice_key(kind, point_label, book_keys)
            choice = duplicate_choices.get(choice_key)
            matches = [candidate for candidate in point_books if candidate.book_key == choice]
            if not matches:
                pending_duplicates.append(
                    PendingDuplicateReview(
                        kind,
                        point_label,
                        book_keys,
                        choice_key,
                        context_book_keys,
                    )
                )
                unresolved = True
                continue
            book = matches[0]
            excluded_duplicate_keys.update(
                candidate.book_key
                for candidate in point_books
                if candidate.book_key != book.book_key
            )
        selected.append(book)
        point_by_key[book.book_key] = point_for(book)
    if unresolved:
        return None
    selected.sort(key=lambda book: _point_sort_key(point_by_key[book.book_key]))
    return _Candidate(kind, tuple(selected), point_by_key, tuple(book.book_key for book in books))


def _pending_overlaps(candidates: list[_Candidate], overlap_choices: Mapping[str, str]) -> tuple[PendingOverlapAssignment, ...]:
    membership: dict[str, set[str]] = {}
    context_book_keys: dict[str, list[str]] = {}
    for candidate in candidates:
        for key in candidate.point_by_key:
            membership.setdefault(key, set()).add(candidate.kind)
            context = context_book_keys.setdefault(key, [])
            for source_key in candidate.source_keys:
                if source_key not in context:
                    context.append(source_key)
    return tuple(
        PendingOverlapAssignment(
            key,
            context_book_keys=tuple(context_book_keys[key]),
        )
        for key in sorted(membership)
        if len(membership[key]) > 1 and key not in overlap_choices
    )


def _trim_overlaps(candidates: list[_Candidate], overlap_choices: Mapping[str, str]) -> list[_Candidate]:
    trimmed = []
    for candidate in candidates:
        kept = []
        for book in candidate.books:
            choice = overlap_choices.get(book.book_key)
            if choice is None or choice == candidate.kind or choice == _display_label(candidate.kind):
                kept.append(book)
        point_by_key = {book.book_key: candidate.point_by_key[book.book_key] for book in kept}
        trimmed.append(_Candidate(candidate.kind, tuple(kept), point_by_key, candidate.source_keys))
    return trimmed


def _candidate_still_valid(candidate: _Candidate) -> bool:
    if candidate.kind == "delayed_2d":
        return _is_valid_delayed_2d_points(list(candidate.point_by_key.values()))
    return _is_valid_delay_time_points(list(candidate.point_by_key.values()))


def _ordered_points(candidate: _Candidate) -> tuple[tuple[str, ...], ...]:
    return tuple(candidate.point_by_key[book.book_key] for book in candidate.books)


def _is_valid_delayed_2d_points(points: list[tuple[str, ...]]) -> bool:
    wavelengths = [_decimal(point[0]) for point in points]
    return len(set(wavelengths)) >= 5 and _is_clustered_sequence(wavelengths)


def _is_valid_delay_time_points(points: list[tuple[str, ...]]) -> bool:
    pairs = [(_decimal(point[0]), _decimal(point[1])) for point in points]
    return _is_valid_delay_time_series(pairs)


def _is_clustered_sequence(values: list[Decimal]) -> bool:
    ordered = sorted(set(values))
    if len(ordered) < 5:
        return False
    diffs = [ordered[index + 1] - ordered[index] for index in range(len(ordered) - 1)]
    if any(diff <= 0 for diff in diffs):
        return False
    step_sizes = sorted(set(diffs))
    if len(step_sizes) == 1:
        return True
    return len(step_sizes) == 2 and step_sizes[1] == step_sizes[0] * 2


def _duplicate_choice_key(
    kind: str,
    point_label: str,
    book_keys: tuple[str, ...],
) -> str:
    return json.dumps(
        [kind, point_label, list(book_keys)],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _is_valid_delay_time_series(pairs: list[tuple[Decimal, Decimal]]) -> bool:
    if len(set(pairs)) < 3:
        return False
    ordered = sorted(set(pairs), key=lambda pair: pair[0])
    delays = [pair[0] for pair in ordered]
    times = [pair[1] for pair in ordered]
    if len(set(delays)) != len(delays) or len(set(times)) == 1:
        return False
    if any(delays[index + 1] <= delays[index] for index in range(len(delays) - 1)):
        return False
    if any(times[index + 1] <= times[index] for index in range(len(times) - 1)):
        return False
    differences = {time - delay for delay, time in ordered}
    return len(differences) == 1


def _point_sort_key(point: tuple[str, ...]) -> tuple[Decimal, ...]:
    return tuple(_decimal(value) for value in point)


def _decimal(value: str) -> Decimal:
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"Invalid numeric special-group value: {value}") from exc


def _numeric_identity(value: object | None) -> object | None:
    if value is None:
        return None
    text = str(value).strip()
    if "/" in text:
        return tuple(_numeric_identity(part) for part in text.split("/"))
    try:
        number = Decimal(text)
    except InvalidOperation:
        return text
    return number if number.is_finite() else text


def _numeric_tuple_identity(
    values: tuple[str, ...] | None,
) -> tuple[object | None, ...] | None:
    if values is None:
        return None
    return tuple(_numeric_identity(value) for value in values)


def _display_label(kind: str) -> str:
    if kind == "delayed_2d":
        return DELAYED_2D_LABEL
    if kind == "delay_time_series":
        return DELAY_TIME_LABEL
    return kind
