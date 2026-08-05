from __future__ import annotations

import re
from collections.abc import Iterable
from enum import Enum


FORBIDDEN_ORIGIN_NAME_CHARACTERS = frozenset(("\n", "\r"))
MAX_GENERATED_NAME_LENGTH = 255
_SAFE_GENERATED_TOKEN = re.compile(r"^[A-Za-z0-9._-]+$")


class NamePolicyError(ValueError):
    pass


class GeneratedBookNamePolicyError(NamePolicyError):
    pass


class NamePreflightIssue(Enum):
    OK = "ok"
    RETURN_TO_ATTRIBUTION = "return_to_attribution"
    INTERNAL_NAMING_FAILURE = "internal_naming_failure"


def forbidden_character_display() -> str:
    labels = []
    if "\n" in FORBIDDEN_ORIGIN_NAME_CHARACTERS:
        labels.append("LF")
    if "\r" in FORBIDDEN_ORIGIN_NAME_CHARACTERS:
        labels.append("CR")
    return ", ".join(labels)


def validate_user_origin_name_text(text: str, *, field_name: str) -> str:
    value = str(text)
    _validate_common(value, field_name=field_name)
    return value


def require_safe_generated_token(token: str, *, field_name: str) -> str:
    value = str(token)
    _validate_common(value, field_name=field_name)
    if not _SAFE_GENERATED_TOKEN.fullmatch(value):
        raise NamePolicyError(f"{field_name} contains characters outside the generated-name token policy")
    return value


def preflight_generated_names(*, folder_names: Iterable[str], book_display_names: Iterable[str]) -> None:
    for folder_name in folder_names:
        require_safe_generated_token(folder_name, field_name="generated folder name")
    for book_display_name in book_display_names:
        try:
            validate_user_origin_name_text(
                book_display_name,
                field_name="generated book display name",
            )
        except NamePolicyError as exc:
            raise GeneratedBookNamePolicyError(str(exc)) from exc


def classify_generated_name_preflight(*, folder_names: Iterable[str], book_display_names: Iterable[str]) -> NamePreflightIssue:
    for folder_name in folder_names:
        try:
            require_safe_generated_token(folder_name, field_name="generated folder name")
        except NamePolicyError:
            return NamePreflightIssue.INTERNAL_NAMING_FAILURE
    for book_display_name in book_display_names:
        try:
            validate_user_origin_name_text(book_display_name, field_name="generated book display name")
        except NamePolicyError:
            return NamePreflightIssue.RETURN_TO_ATTRIBUTION
    return NamePreflightIssue.OK


def _validate_common(value: str, *, field_name: str) -> None:
    if value == "":
        raise NamePolicyError(f"{field_name} must not be empty")
    for character in FORBIDDEN_ORIGIN_NAME_CHARACTERS:
        if character in value:
            raise NamePolicyError(f"{field_name} contains a forbidden control character")
    if len(value) > MAX_GENERATED_NAME_LENGTH:
        raise NamePolicyError(f"{field_name} exceeds the conservative generated-name length limit")
