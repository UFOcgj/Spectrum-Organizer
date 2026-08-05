from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import shutil
import sqlite3
import sys
import threading
import time
from types import MappingProxyType
from typing import Callable, Mapping
import uuid

from spectrum_organizer.core.audit_details import (
    canonical_audit_detail,
    measurement_text as _approved_measurement_text,
)
from spectrum_organizer.core.metadata_numeric import (
    format_raw_slit_fields,
    is_finite_real_number,
)
from spectrum_organizer.core.output_model import (
    OutputPlan,
    OutputSpectrum,
    build_output_plan,
)
from spectrum_organizer.domain.models import SpectrumClass
from spectrum_organizer.origin.ipc_auth import (
    sidecar_content_hmac,
    validate_sidecar_auth_key,
)
from spectrum_organizer.safety.fingerprints import (
    SourceSnapshot,
    disambiguated_source_labels,
    file_identity,
    hash_file,
    snapshot_sources,
    verify_sources_unchanged,
)
from spectrum_organizer.safety.identity_paths import (
    hold_file_identity,
    IdentityPathError,
    lexical_path_exists,
    path_identity,
    unlink_owned_path,
)
from spectrum_organizer.safety.owned_paths import (
    ACTIVE_LEASE_FILE,
    OWNERSHIP_FILE,
    OwnershipError,
    add_allowed_child,
    bind_allowed_child_identity,
    cleanup_owned_temp_root,
    create_run_ownership,
    read_ownership,
    write_ownership,
)
from spectrum_organizer.safety.process_boundary import (
    ProcessIdentity,
    WindowsOriginProcessController,
    classify_process,
    default_origin_process_probe,
    preflight_origin_boundary,
)
from spectrum_organizer.safety.process_job import (
    PARENT_START_GATE_ENV,
    PARENT_START_GATE_TOKEN_ENV,
    bind_process_to_job,
    close_bound_process_job,
    terminate_bound_process,
)
from spectrum_organizer.safety.source_copies import (
    required_temp_bytes,
)
from spectrum_organizer.runtime_audit import (
    record_runtime_audit_event,
    runtime_audit_enabled,
    runtime_audit_file_identity,
)
from spectrum_organizer.store.sqlite_digest import sqlite_content_sha256
from spectrum_organizer.workflow.extraction_contracts import (
    ApprovedPreExtractionRunContext,
    ExtractionCleanupBlockedError,
    ProductRunnerError,
    READER_SIDECAR_AUTH_ENV,
    ReaderProcessCommand,
    ReaderSourceExtractionSummary,
    UnsupportedSourceInputError,
    VerifiedSourceCopyIdentity,
    _canonical_source_snapshot_path,
    _confirmed_allow_missing_s1,
    _confirmed_s1_limit,
    _confirmed_steady_emission_y,
    _context_from_payload,
    _context_to_payload,
    _reader_command_from_payload,
    _reader_command_to_payload,
    _reader_summary_from_payload,
    _valid_identity_payload,
)
from spectrum_organizer.workflow.extraction_ipc import (
    _write_json_atomic_exclusive,
    _write_json_atomic_exclusive_evidence,
    _write_json_exclusive,
    _write_json_exclusive_evidence,
)
from spectrum_organizer.workflow.pre_extraction_service import (
    prepare_extraction_context,
)
from spectrum_organizer.ui.dialogs import (
    FinalReviewRow,
    attribution_dialog,
    final_attribution_summary_dialog,
    output_can_be_inspected_dialog,
    preflight_settings_dialog,
    save_and_close_origin_dialog,
)
from spectrum_organizer.ui.state_machine import Stage


PUBLICATION_READY = "publication_ready"


@dataclass(frozen=True)
class ProtectedPathAuditPlan:
    source_snapshots: bool = True
    worker_open_targets: bool = True
    worker_text_logs: bool = True
    final_origin_process_count: int = 0


@dataclass(frozen=True)
class Task15ReadinessReport:
    ready: bool
    missing: tuple[str, ...]
    next_action: str


@dataclass(frozen=True)
class ProductRunnerDependencies:
    manual_dialog_port: object | None = None
    extraction_worker_factory: object | None = None
    output_worker: Callable | None = None
    verifier_worker: Callable | None = None
    create_staging: Callable | None = None
    publish_run: Callable | None = None
    report_builder: Callable | None = None
    protected_path_audit_hook: Callable | None = None
    final_process_count_hook: Callable | None = None
    state_machine_factory: Callable | None = None
    mode: str = "book_only"


@dataclass(frozen=True)
class SourceExtractionSummary:
    source_id: str
    original_path: str
    copy_path: str
    inventory_count: int
    result_count: int
    extracted_count: int
    rejected_count: int


@dataclass(frozen=True)
class SourceInputIssue:
    source_id: str
    original_path: str
    reason: str
    recommendation: str


@dataclass(frozen=True)
class ExtractionPhaseSummary:
    snapshot_path: Path
    source_summaries: tuple[SourceExtractionSummary, ...]
    total_inventory_count: int
    total_result_count: int
    total_extracted_count: int
    total_rejected_count: int
    snapshot_sha256: str | None = None
    worker_open_targets: tuple[str, ...] = ()
    source_input_issues: tuple[SourceInputIssue, ...] = ()


@dataclass(frozen=True)
class ApprovedAuditItem:
    book_key: str
    detail: str
    source_id: str = ""
    source_filename: str = ""
    page_type: str = ""
    folder_path: str = ""
    short_name: str = ""
    display_name: str = ""
    reason_code: str = ""
    evidence: tuple[tuple[str, str], ...] = ()
    decision_source: str = ""


@dataclass(frozen=True)
class ApprovedAttribution:
    book_key: str
    canonical_sample_label: str
    sample_system_label: str
    temperature: str
    sample_system_identity: str = ""
    source_id: str = ""
    source_filename: str = ""
    page_type: str = ""
    folder_path: str = ""
    short_name: str = ""
    display_name: str = ""
    payload_checksum: str = ""


@dataclass(frozen=True)
class ApprovedBookIdentity:
    book_key: str
    source_id: str
    source_filename: str
    page_type: str
    folder_path: str
    short_name: str
    display_name: str
    payload_checksum: str
    raw_display_name: str = ""
    spectrum_class: str = ""
    selected_y_column: str = ""
    paired_x_column: str = ""


@dataclass(frozen=True)
class ApprovedSourceFingerprint:
    source_id: str
    snapshot: SourceSnapshot


@dataclass(frozen=True)
class ApprovedReviewChoice:
    kind: str
    review_key: str
    selected_book_keys: tuple[str, ...]
    candidate_book_keys: tuple[str, ...] = ()
    decision: str = ""
    subject: str = ""
    decision_source: str = "manual"


@dataclass(frozen=True)
class ApprovedReviewRequirement:
    kind: str
    review_key: str
    candidate_book_keys: tuple[str, ...]
    decision_source: str = "manual"


@dataclass(frozen=True)
class CountReconciliation:
    recognizable_book_count: int
    rejected_book_count: int
    excluded_book_count: int
    accepted_ordinary_spectrum_count: int
    output_plan_spectrum_count: int
    output_plan_column_count: int
    verifier_readback_spectrum_count: int | None = None
    verifier_readback_column_count: int | None = None

    @property
    def is_closed(self) -> bool:
        return self.recognizable_book_count == (
            self.rejected_book_count
            + self.excluded_book_count
            + self.accepted_ordinary_spectrum_count
        )


@dataclass(frozen=True)
class ApprovedOutputSnapshot:
    snapshot_id: str
    task_snapshot_sha256: str
    recognized_book_keys: tuple[str, ...]
    accepted_spectra: tuple[OutputSpectrum, ...]
    rejections: tuple[ApprovedAuditItem, ...]
    exclusions: tuple[ApprovedAuditItem, ...]
    attributions: tuple[ApprovedAttribution, ...]
    review_requirements: tuple[ApprovedReviewRequirement, ...]
    review_choices: tuple[ApprovedReviewChoice, ...]
    output_plan: OutputPlan
    source_fingerprints_before: tuple[SourceSnapshot, ...]
    source_fingerprints_after: tuple[SourceSnapshot, ...]
    count_reconciliation: CountReconciliation
    recognized_books: tuple[ApprovedBookIdentity, ...]
    approved_sources: tuple[ApprovedSourceFingerprint, ...]
    source_ids: tuple[str, ...]
    task_snapshot_path: Path
    task_temp_root_identity: tuple[int, int]
    settings_snapshot: Mapping[str, object] = field(
        default_factory=lambda: MappingProxyType({})
    )
    ignored_duplicate_input_paths: tuple[Path, ...] = ()
    source_input_issues: tuple[SourceInputIssue, ...] = ()
    selected_source_fingerprints_before: tuple[SourceSnapshot, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "settings_snapshot",
            MappingProxyType(dict(self.settings_snapshot)),
        )
        object.__setattr__(
            self,
            "ignored_duplicate_input_paths",
            tuple(
                Path(path)
                for path in self.ignored_duplicate_input_paths
            ),
        )
        object.__setattr__(
            self,
            "source_input_issues",
            tuple(self.source_input_issues),
        )
        object.__setattr__(
            self,
            "selected_source_fingerprints_before",
            tuple(self.selected_source_fingerprints_before)
            or tuple(self.source_fingerprints_before),
        )


class AllSelectedSourcesInvalidError(ProductRunnerError):
    def __init__(self, source_input_issues: tuple[SourceInputIssue, ...]):
        self.source_input_issues = tuple(source_input_issues)
        super().__init__(
            f"所选 {len(self.source_input_issues)} 个输入文件均未进入后续流程。"
        )


class _ReaderProcessInfrastructureError(ProductRunnerError):
    """The reader process failed before returning a trustworthy domain result."""


def approve_output_plan(
    *,
    task_snapshot_sha256: str,
    recognized_book_keys: tuple[str, ...],
    accepted_spectra: tuple[OutputSpectrum, ...],
    rejections: tuple[ApprovedAuditItem, ...],
    exclusions: tuple[ApprovedAuditItem, ...],
    attributions: tuple[ApprovedAttribution, ...],
    review_choices: tuple[ApprovedReviewChoice, ...],
    output_plan: OutputPlan,
    source_fingerprints_before: tuple[SourceSnapshot, ...],
    source_fingerprints_after: tuple[SourceSnapshot, ...],
    count_reconciliation: CountReconciliation,
    recognized_books: tuple[ApprovedBookIdentity, ...] = (),
    review_requirements: tuple[ApprovedReviewRequirement, ...] = (),
    source_ids: tuple[str, ...] = (),
    task_snapshot_path: Path | None = None,
    task_temp_root_identity: tuple[int, int] | None = None,
    cancel_check: Callable[[], None] | None = None,
    settings_snapshot: Mapping[str, object] | None = None,
    ignored_duplicate_input_paths: tuple[Path, ...] = (),
    source_input_issues: tuple[SourceInputIssue, ...] = (),
    selected_source_fingerprints_before: tuple[SourceSnapshot, ...] | None = None,
) -> ApprovedOutputSnapshot:
    if cancel_check is not None:
        cancel_check()
    recognized_book_keys = tuple(recognized_book_keys)
    accepted_spectra = tuple(
        _freeze_output_spectrum(spectrum)
        for spectrum in accepted_spectra
    )
    rejections = tuple(
        replace(
            item,
            evidence=tuple(
                (name, value)
                for name, value in item.evidence
            ),
        )
        for item in rejections
    )
    exclusions = tuple(
        replace(
            item,
            evidence=tuple(
                (name, value)
                for name, value in item.evidence
            ),
        )
        for item in exclusions
    )
    attributions = tuple(attributions)
    review_requirements = tuple(
        replace(
            requirement,
            candidate_book_keys=tuple(
                requirement.candidate_book_keys
            ),
        )
        for requirement in review_requirements
    )
    review_choices = tuple(
        replace(
            choice,
            selected_book_keys=tuple(choice.selected_book_keys),
            candidate_book_keys=tuple(choice.candidate_book_keys),
        )
        for choice in review_choices
    )
    source_fingerprints_before = tuple(source_fingerprints_before)
    source_fingerprints_after = tuple(source_fingerprints_after)
    recognized_books = tuple(recognized_books)
    source_ids = tuple(source_ids)
    approved_settings_snapshot = MappingProxyType(
        dict(settings_snapshot or {})
    )
    ignored_duplicate_input_paths = tuple(
        Path(path)
        for path in ignored_duplicate_input_paths
    )
    source_input_issues = tuple(source_input_issues)
    selected_source_fingerprints_before = tuple(
        selected_source_fingerprints_before
        if selected_source_fingerprints_before is not None
        else source_fingerprints_before
    )
    if any(
        not issue.source_id.strip()
        or not issue.original_path.strip()
        or not issue.reason.strip()
        or not issue.recommendation.strip()
        for issue in source_input_issues
    ):
        raise ProductRunnerError("source input issue is incomplete")
    if cancel_check is not None:
        cancel_check()
    if (
        len(task_snapshot_sha256) != 64
        or any(character not in "0123456789abcdef" for character in task_snapshot_sha256.casefold())
    ):
        raise ProductRunnerError("approved output requires a valid task snapshot SHA-256")
    if source_fingerprints_after != source_fingerprints_before:
        raise ProductRunnerError("source fingerprints changed before approved snapshot")
    counts = (
        count_reconciliation.recognizable_book_count,
        count_reconciliation.rejected_book_count,
        count_reconciliation.excluded_book_count,
        count_reconciliation.accepted_ordinary_spectrum_count,
        count_reconciliation.output_plan_spectrum_count,
        count_reconciliation.output_plan_column_count,
    )
    if any(type(count) is not int for count in counts):
        raise ProductRunnerError("approved output counts must be integers")
    if any(count < 0 for count in counts):
        raise ProductRunnerError("approved output counts cannot be negative")
    if (
        count_reconciliation.verifier_readback_spectrum_count is not None
        or count_reconciliation.verifier_readback_column_count is not None
    ):
        raise ProductRunnerError(
            "approved output cannot contain verifier readback counts"
        )
    if not count_reconciliation.is_closed:
        raise ProductRunnerError("approved output counts do not reconcile")
    if len(rejections) != count_reconciliation.rejected_book_count:
        raise ProductRunnerError(
            "rejection audit count does not reconcile"
        )
    if len(exclusions) != count_reconciliation.excluded_book_count:
        raise ProductRunnerError(
            "exclusion audit count does not reconcile"
        )
    if count_reconciliation.accepted_ordinary_spectrum_count != len(accepted_spectra):
        raise ProductRunnerError("accepted spectrum count does not match approved spectra")
    if not accepted_spectra or not output_plan.folders:
        raise ProductRunnerError(
            "approved output requires at least one output spectrum"
        )
    if len(recognized_book_keys) != count_reconciliation.recognizable_book_count:
        raise ProductRunnerError(
            "recognized book key count does not reconcile"
        )
    if len(recognized_book_keys) != len(set(recognized_book_keys)):
        raise ProductRunnerError("recognized book keys must be unique")
    accepted_keys = tuple(spectrum.spectrum_id for spectrum in accepted_spectra)
    rejected_keys = tuple(item.book_key for item in rejections)
    excluded_keys = tuple(item.book_key for item in exclusions)
    if len(accepted_keys) != len(set(accepted_keys)):
        raise ProductRunnerError("accepted spectrum book keys must be unique")
    if len(rejected_keys) != len(set(rejected_keys)):
        raise ProductRunnerError("rejection audit book keys must be unique")
    if len(excluded_keys) != len(set(excluded_keys)):
        raise ProductRunnerError("exclusion audit book keys must be unique")
    accepted_key_set = set(accepted_keys)
    rejected_key_set = set(rejected_keys)
    excluded_key_set = set(excluded_keys)
    if (
        accepted_key_set.intersection(rejected_key_set)
        or accepted_key_set.intersection(excluded_key_set)
        or rejected_key_set.intersection(excluded_key_set)
    ):
        raise ProductRunnerError("approved book dispositions overlap")
    if (
        accepted_key_set | rejected_key_set | excluded_key_set
        != set(recognized_book_keys)
    ):
        raise ProductRunnerError(
            "approved dispositions do not exactly cover recognized books"
        )
    rebuilt_output_plan = build_output_plan(accepted_spectra)
    if rebuilt_output_plan != output_plan:
        raise ProductRunnerError(
            "OutputPlan does not match accepted spectra"
        )
    output_plan = rebuilt_output_plan
    plan_spectrum_count = sum(
        len(book.raw_y_columns)
        for folder in output_plan.folders
        for book in folder.books
    )
    plan_column_count = sum(
        len(book.columns)
        for folder in output_plan.folders
        for book in folder.books
    )
    if plan_spectrum_count != count_reconciliation.output_plan_spectrum_count:
        raise ProductRunnerError("OutputPlan spectrum count does not reconcile")
    if plan_column_count != count_reconciliation.output_plan_column_count:
        raise ProductRunnerError("OutputPlan column count does not reconcile")
    attribution_keys = tuple(
        attribution.book_key
        for attribution in attributions
    )
    if len(attribution_keys) != len(set(attribution_keys)):
        raise ProductRunnerError("attribution book keys must be unique")
    attribution_key_set = set(attribution_keys)
    required_attribution_keys = accepted_key_set | excluded_key_set
    if not required_attribution_keys.issubset(attribution_key_set):
        raise ProductRunnerError(
            "attributions do not cover approved candidate books"
        )
    if not attribution_key_set.issubset(set(recognized_book_keys)):
        raise ProductRunnerError(
            "attribution books are not recognized"
        )
    attribution_by_key = {
        attribution.book_key: attribution
        for attribution in attributions
    }
    for spectrum in accepted_spectra:
        attribution = attribution_by_key[spectrum.spectrum_id]
        if (
            attribution.canonical_sample_label,
            attribution.sample_system_label,
            attribution.temperature,
            attribution.sample_system_identity,
        ) != (
            spectrum.canonical_sample_label,
            spectrum.sample_system_label,
            spectrum.temperature,
            spectrum.sample_system_identity,
        ):
            raise ProductRunnerError(
                f"attribution does not match accepted spectrum: "
                f"{spectrum.spectrum_id}"
            )
    review_identities = tuple(
        (choice.kind, choice.review_key)
        for choice in review_choices
    )
    if len(review_identities) != len(set(review_identities)):
        raise ProductRunnerError("review choices must be unique")
    for choice in review_choices:
        if choice.decision_source not in {"automatic", "manual"}:
            raise ProductRunnerError(
                "review choice decision source is invalid"
            )
        candidate_keys = tuple(choice.candidate_book_keys)
        selected_keys = tuple(choice.selected_book_keys)
        if not candidate_keys:
            raise ProductRunnerError(
                "review choices require candidate books"
            )
        if len(candidate_keys) != len(set(candidate_keys)):
            raise ProductRunnerError(
                "review candidate book keys must be unique"
            )
        if len(selected_keys) != len(set(selected_keys)):
            raise ProductRunnerError(
                "review selected book keys must be unique"
            )
        candidate_key_set = set(candidate_keys)
        if not candidate_key_set.issubset(attribution_key_set):
            raise ProductRunnerError(
                "review candidate books are not attributed"
            )
        if not set(selected_keys).issubset(candidate_key_set):
            raise ProductRunnerError(
                "review selected books are not candidates"
            )
        if choice.kind in {"emission", "special_duplicate"}:
            if len(candidate_keys) < 2 or len(selected_keys) != 1:
                raise ProductRunnerError(
                    f"{choice.kind} review must select exactly one candidate"
                )
        elif choice.kind == "excitation":
            if not selected_keys:
                raise ProductRunnerError(
                    "excitation review must select one or more candidates"
                )
        elif choice.kind == "special_overlap":
            if (
                len(candidate_keys) != 1
                or selected_keys != candidate_keys
                or choice.decision not in {
                    "delayed_2d",
                    "delay_time_series",
                    "regular",
                }
            ):
                raise ProductRunnerError(
                    "special_overlap review decision is invalid"
                )
        elif choice.kind == "special_group":
            if choice.decision == "confirm_group":
                valid = selected_keys == candidate_keys
            elif choice.decision == "confirm_selection":
                valid = bool(selected_keys)
            elif choice.decision == "reject_group":
                valid = not selected_keys
            else:
                valid = False
            if not valid:
                raise ProductRunnerError(
                    "special_group review decision is invalid"
                )
        else:
            raise ProductRunnerError(
                f"unsupported approved review kind: {choice.kind}"
            )
    _validate_review_requirements(
        review_requirements,
        review_choices,
        attribution_key_set,
        cancel_check=cancel_check,
    )
    exclusions_by_key = {
        item.book_key: item
        for item in exclusions
    }
    for item in (*rejections, *exclusions):
        required = (
            item.book_key,
            item.detail,
            item.reason_code,
        )
        if any(
            not isinstance(value, str) or not value.strip()
            for value in required
        ):
            raise ProductRunnerError(
                f"approved audit item is incomplete: {item.book_key}"
            )
    review_kind_by_reason = {
        "emission_duplicate_unselected": "emission",
        "exact_excitation_duplicate_unselected": "excitation",
        "excitation_candidate_unselected": "excitation",
        "special_group_rejected": "special_group",
        "special_group_not_copied_to_ordinary_output": "special_group",
        "special_duplicate_unselected": "special_duplicate",
    }
    for book_key, exclusion in exclusions_by_key.items():
        expected_kind = review_kind_by_reason.get(
            exclusion.reason_code
        )
        if expected_kind is None:
            raise ProductRunnerError(
                f"unsupported exclusion reason code: "
                f"{exclusion.reason_code}"
            )
        matching_choices = tuple(
            choice
            for choice in review_choices
            if choice.kind == expected_kind
            and book_key in choice.candidate_book_keys
            and _choice_caused_exclusion(choice, exclusion.reason_code, book_key)
        )
        if not matching_choices:
            raise ProductRunnerError(
                f"approved exclusion has no matching review choice: "
                f"{book_key}"
            )
        evidence = dict(exclusion.evidence)
        bound_choices = tuple(
            choice
            for choice in matching_choices
            if (
                evidence.get("review_kind"),
                evidence.get("review_key"),
            )
            == (choice.kind, choice.review_key)
        )
        if len(bound_choices) != 1:
            raise ProductRunnerError(
                f"approved exclusion is not bound to its review choice: "
                f"{book_key}"
            )
        bound_choice = bound_choices[0]
        expected_evidence = tuple(
            (name, value)
            for name, value in (
                ("review_kind", bound_choice.kind),
                ("review_key", bound_choice.review_key),
                ("decision", bound_choice.decision),
                ("subject", bound_choice.subject),
            )
            if value
        )
        if (
            exclusion.evidence != expected_evidence
            or exclusion.decision_source
            != bound_choice.decision_source
        ):
            raise ProductRunnerError(
                f"approved exclusion audit does not match its review "
                f"choice: {book_key}"
            )
    _validate_review_dispositions(
        review_choices,
        exclusions_by_key,
        accepted_spectra,
        rejected_key_set,
        cancel_check=cancel_check,
    )
    if recognized_book_keys and not source_fingerprints_before:
        raise ProductRunnerError("source fingerprints are required")
    source_path_keys = tuple(
        _canonical_source_snapshot_path(snapshot)
        for snapshot in source_fingerprints_before
    )
    if len(source_path_keys) != len(set(source_path_keys)):
        raise ProductRunnerError(
            "source fingerprint paths must be unique"
        )
    for snapshot in source_fingerprints_before:
        if (
            len(snapshot.sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in snapshot.sha256.casefold()
            )
            or snapshot.size_bytes < 0
            or snapshot.mtime_ns < 0
            or snapshot.device_id is None
            or snapshot.file_id is None
        ):
            raise ProductRunnerError("source fingerprint is invalid")
    selected_source_path_keys = tuple(
        _canonical_source_snapshot_path(snapshot)
        for snapshot in selected_source_fingerprints_before
    )
    if (
        not selected_source_path_keys
        or len(selected_source_path_keys) != len(set(selected_source_path_keys))
        or not set(source_path_keys).issubset(selected_source_path_keys)
    ):
        raise ProductRunnerError(
            "selected source fingerprints must uniquely cover approved sources"
        )
    issue_path_keys = {
        os.path.normcase(str(Path(issue.original_path).resolve()))
        for issue in source_input_issues
    }
    if (
        len(issue_path_keys) != len(source_input_issues)
        or issue_path_keys.intersection(source_path_keys)
        or set(selected_source_path_keys)
        != set(source_path_keys).union(issue_path_keys)
    ):
        raise ProductRunnerError(
            "every selected source must have exactly one processing disposition"
        )
    for snapshot in selected_source_fingerprints_before:
        if (
            len(snapshot.sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in snapshot.sha256.casefold()
            )
            or snapshot.size_bytes < 0
            or snapshot.mtime_ns < 0
            or snapshot.device_id is None
            or snapshot.file_id is None
        ):
            raise ProductRunnerError(
                "selected source fingerprint is invalid"
            )
    if (
        not source_ids
        or len(source_ids) != len(source_fingerprints_before)
        or len(source_ids) != len(set(source_ids))
        or any(not source_id for source_id in source_ids)
    ):
        raise ProductRunnerError(
            "source ids must map exactly to source fingerprints"
        )
    issue_source_ids = tuple(
        issue.source_id
        for issue in source_input_issues
    )
    if (
        len(issue_source_ids) != len(set(issue_source_ids))
        or set(issue_source_ids).intersection(source_ids)
    ):
        raise ProductRunnerError(
            "approved and input-issue source ids must be unique and disjoint"
        )
    expected_selected_pairs = {
        (f"S{index:04d}", path_key)
        for index, path_key in enumerate(
            selected_source_path_keys,
            start=1,
        )
    }
    approved_pairs = set(zip(source_ids, source_path_keys, strict=True))
    issue_pairs = {
        (
            issue.source_id,
            os.path.normcase(str(Path(issue.original_path).resolve())),
        )
        for issue in source_input_issues
    }
    if (
        approved_pairs.intersection(issue_pairs)
        or approved_pairs.union(issue_pairs) != expected_selected_pairs
    ):
        raise ProductRunnerError(
            "selected source ids and paths must have exactly one processing disposition"
        )
    approved_sources = tuple(
        ApprovedSourceFingerprint(source_id, snapshot)
        for source_id, snapshot in zip(
            source_ids,
            source_fingerprints_before,
            strict=True,
        )
    )
    try:
        verify_sources_unchanged(
            list(source_fingerprints_after),
            cancel_check=cancel_check,
        )
    except Exception as exc:
        raise ProductRunnerError(
            f"approved source fingerprint does not match current file: "
            f"{exc}"
        ) from exc
    approved_source_ids = set(source_ids)
    for item in (*rejections, *exclusions):
        required_text = (
            item.book_key,
            item.detail,
            item.source_id,
            item.source_filename,
            item.page_type,
            item.short_name,
            item.display_name,
            item.reason_code,
            item.decision_source,
        )
        if any(not str(value).strip() for value in required_text):
            raise ProductRunnerError(
                f"approved audit item is incomplete: {item.book_key}"
            )
        if item.decision_source not in {"automatic", "manual"}:
            raise ProductRunnerError(
                f"approved audit decision source is invalid: "
                f"{item.book_key}"
            )
        evidence_names = tuple(name for name, _value in item.evidence)
        if (
            len(evidence_names) != len(set(evidence_names))
            or any(
                type(name) is not str
                or type(value) is not str
                or not name
                or not value
                for name, value in item.evidence
            )
        ):
            raise ProductRunnerError(
                f"approved audit evidence is invalid: {item.book_key}"
            )
        if item.source_id not in approved_source_ids:
            raise ProductRunnerError(
                f"approved audit source is not fingerprinted: "
                f"{item.book_key}"
            )
    for attribution in attributions:
        required_text = (
            attribution.book_key,
            attribution.canonical_sample_label,
            attribution.sample_system_label,
            attribution.temperature,
            attribution.sample_system_identity,
            attribution.source_id,
            attribution.source_filename,
            attribution.page_type,
            attribution.short_name,
            attribution.display_name,
        )
        if any(not str(value).strip() for value in required_text):
            raise ProductRunnerError(
                f"approved attribution is incomplete: "
                f"{attribution.book_key}"
            )
        if attribution.source_id not in approved_source_ids:
            raise ProductRunnerError(
                f"approved attribution source is not fingerprinted: "
                f"{attribution.book_key}"
            )
    approved_task_snapshot_path = Path(
        task_snapshot_path
        if task_snapshot_path is not None
        else ""
    )
    if not str(approved_task_snapshot_path).strip() or str(
        approved_task_snapshot_path
    ) == ".":
        raise ProductRunnerError(
            "approved output requires the task snapshot path"
        )
    if (
        not isinstance(task_temp_root_identity, tuple)
        or len(task_temp_root_identity) != 2
        or not all(
            isinstance(part, int)
            and not isinstance(part, bool)
            and part >= 0
            for part in task_temp_root_identity
        )
    ):
        raise ProductRunnerError(
            "approved output requires the caller-held task temp root identity"
        )
    approved_task_temp_root_identity = tuple(task_temp_root_identity)
    _validate_approved_book_ledger(
        recognized_book_keys=recognized_book_keys,
        recognized_books=recognized_books,
        rejections=rejections,
        exclusions=exclusions,
        attributions=attributions,
        approved_sources=approved_sources,
        cancel_check=cancel_check,
    )
    _verify_task_snapshot_book_ledger(
        approved_task_snapshot_path,
        task_snapshot_sha256.casefold(),
        recognized_books,
        approved_sources,
        accepted_spectra,
        rejections,
        attributions,
        review_requirements,
        review_choices,
        exclusions,
        cancel_check=cancel_check,
    )
    if cancel_check is not None:
        cancel_check()
    approval_payload = (
        task_snapshot_sha256.casefold(),
        recognized_book_keys,
        accepted_spectra,
        rejections,
        exclusions,
        attributions,
        review_requirements,
        review_choices,
        output_plan,
        source_fingerprints_before,
        source_fingerprints_after,
        count_reconciliation,
        recognized_books,
        approved_sources,
        approved_task_snapshot_path,
        approved_task_temp_root_identity,
        tuple(sorted(approved_settings_snapshot.items())),
        ignored_duplicate_input_paths,
        source_input_issues,
        selected_source_fingerprints_before,
    )
    snapshot_id = hashlib.sha256(
        repr(approval_payload).encode("utf-8")
    ).hexdigest()
    return ApprovedOutputSnapshot(
        snapshot_id=snapshot_id,
        task_snapshot_sha256=task_snapshot_sha256.casefold(),
        recognized_book_keys=recognized_book_keys,
        accepted_spectra=accepted_spectra,
        rejections=rejections,
        exclusions=exclusions,
        attributions=attributions,
        review_requirements=review_requirements,
        review_choices=review_choices,
        output_plan=output_plan,
        source_fingerprints_before=source_fingerprints_before,
        source_fingerprints_after=source_fingerprints_after,
        count_reconciliation=count_reconciliation,
        recognized_books=recognized_books,
        approved_sources=approved_sources,
        source_ids=source_ids,
        task_snapshot_path=approved_task_snapshot_path,
        task_temp_root_identity=approved_task_temp_root_identity,
        settings_snapshot=approved_settings_snapshot,
        ignored_duplicate_input_paths=ignored_duplicate_input_paths,
        source_input_issues=source_input_issues,
        selected_source_fingerprints_before=(
            selected_source_fingerprints_before
        ),
    )


def _freeze_output_spectrum(spectrum: OutputSpectrum) -> OutputSpectrum:
    return replace(
        spectrum,
        x_y=tuple(tuple(pair) for pair in spectrum.x_y),
        excitation_slit=_freeze_optional_sequence(
            spectrum.excitation_slit
        ),
        emission_slit=_freeze_optional_sequence(
            spectrum.emission_slit
        ),
    )


def _freeze_optional_sequence(value):
    if value is None or isinstance(value, str):
        return value
    return tuple(value)


def _validate_review_requirements(
    requirements: tuple[ApprovedReviewRequirement, ...],
    choices: tuple[ApprovedReviewChoice, ...],
    attribution_key_set: set[str],
    *,
    cancel_check=None,
) -> None:
    requirement_ids = tuple(
        (requirement.kind, requirement.review_key)
        for requirement in requirements
    )
    choice_ids = tuple(
        (choice.kind, choice.review_key)
        for choice in choices
    )
    if len(requirement_ids) != len(set(requirement_ids)):
        raise ProductRunnerError("review requirements must be unique")
    if requirement_ids != choice_ids:
        raise ProductRunnerError(
            "review choices do not exactly cover required reviews"
        )
    for requirement, choice in zip(
        requirements,
        choices,
        strict=True,
    ):
        if cancel_check is not None:
            cancel_check()
        candidate_keys = tuple(requirement.candidate_book_keys)
        if (
            not candidate_keys
            or len(candidate_keys) != len(set(candidate_keys))
            or not set(candidate_keys).issubset(attribution_key_set)
        ):
            raise ProductRunnerError(
                "review requirement candidate books are invalid"
            )
        if (
            candidate_keys != choice.candidate_book_keys
            or requirement.decision_source != choice.decision_source
        ):
            raise ProductRunnerError(
                "review choice does not match its requirement"
            )


def _choice_caused_exclusion(
    choice: ApprovedReviewChoice,
    reason_code: str,
    book_key: str,
) -> bool:
    if reason_code == "special_group_not_copied_to_ordinary_output":
        return book_key in choice.selected_book_keys
    return book_key not in choice.selected_book_keys


def _validate_review_dispositions(
    choices: tuple[ApprovedReviewChoice, ...],
    exclusions_by_key: dict[str, ApprovedAuditItem],
    accepted_spectra: tuple[OutputSpectrum, ...],
    rejected_key_set: set[str],
    *,
    cancel_check=None,
) -> None:
    expected_reason_by_kind = {
        "emission": {"emission_duplicate_unselected"},
        "excitation": {
            "exact_excitation_duplicate_unselected",
            "excitation_candidate_unselected",
        },
    }
    for choice in choices:
        if cancel_check is not None:
            cancel_check()
        if choice.kind in expected_reason_by_kind:
            unselected = (
                set(choice.candidate_book_keys)
                - set(choice.selected_book_keys)
            )
            for book_key in unselected:
                exclusion = exclusions_by_key.get(book_key)
                if (
                    exclusion is None
                    or exclusion.reason_code
                    not in expected_reason_by_kind[choice.kind]
                    or dict(exclusion.evidence).get("review_key")
                    != choice.review_key
                ):
                    raise ProductRunnerError(
                        "review choice does not match final dispositions"
                    )
            if choice.kind == "excitation":
                surviving_selection = tuple(
                    book_key
                    for book_key in choice.selected_book_keys
                    if book_key not in rejected_key_set
                )
                accepted_order = tuple(
                    spectrum.spectrum_id
                    for spectrum in sorted(
                        accepted_spectra,
                        key=lambda item: item.selection_order,
                    )
                    if spectrum.spectrum_id
                    in choice.candidate_book_keys
                )
                if surviving_selection != accepted_order:
                    raise ProductRunnerError(
                        "excitation review order does not match approved "
                        "spectrum order"
                    )
        elif choice.kind == "special_group":
            for book_key in choice.selected_book_keys:
                exclusion = exclusions_by_key.get(book_key)
                if (
                    exclusion is None
                    or exclusion.reason_code
                    != "special_group_not_copied_to_ordinary_output"
                    or dict(exclusion.evidence).get("review_key")
                    != choice.review_key
                ):
                    raise ProductRunnerError(
                        "special-group review does not match final "
                        "dispositions"
                    )


def _approved_source_filename_by_id(
    approved_sources: tuple[ApprovedSourceFingerprint, ...],
) -> dict[str, str]:
    labels = disambiguated_source_labels(
        tuple(
            source.snapshot.path
            for source in approved_sources
        )
    )
    return {
        source.source_id: label
        for source, label in zip(
            approved_sources,
            labels,
            strict=True,
        )
    }


def _validate_approved_book_ledger(
    *,
    recognized_book_keys: tuple[str, ...],
    recognized_books: tuple[ApprovedBookIdentity, ...],
    rejections: tuple[ApprovedAuditItem, ...],
    exclusions: tuple[ApprovedAuditItem, ...],
    attributions: tuple[ApprovedAttribution, ...],
    approved_sources: tuple[ApprovedSourceFingerprint, ...],
    cancel_check=None,
) -> None:
    ledger_keys = tuple(item.book_key for item in recognized_books)
    if ledger_keys != recognized_book_keys:
        raise ProductRunnerError(
            "recognized Book identities do not match recognized book keys"
        )
    if len(ledger_keys) != len(set(ledger_keys)):
        raise ProductRunnerError("recognized Book identities must be unique")
    source_filename_by_id = _approved_source_filename_by_id(
        approved_sources
    )
    ledger_by_key = {}
    for item in recognized_books:
        if cancel_check is not None:
            cancel_check()
        expected_key = json.dumps(
            [
                item.source_id,
                item.page_type,
                item.folder_path,
                item.short_name,
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        if item.book_key != expected_key:
            raise ProductRunnerError(
                f"recognized Book identity key is invalid: {item.book_key}"
            )
        if (
            item.source_id not in source_filename_by_id
            or item.source_filename
            != source_filename_by_id[item.source_id]
        ):
            raise ProductRunnerError(
                f"recognized Book source identity is invalid: "
                f"{item.book_key}"
            )
        if any(
            not str(value).strip()
            for value in (
                item.source_id,
                item.source_filename,
                item.page_type,
                item.short_name,
                item.display_name,
            )
        ):
            raise ProductRunnerError(
                f"recognized Book identity is incomplete: {item.book_key}"
            )
        raw_display_name = str(item.raw_display_name or "")
        expected_display_name = (
            raw_display_name
            if raw_display_name.strip()
            else item.short_name
        )
        if item.display_name != expected_display_name:
            raise ProductRunnerError(
                f"recognized Book display name is invalid: "
                f"{item.book_key}"
            )
        if not _is_sha256(item.payload_checksum):
            raise ProductRunnerError(
                f"recognized Book payload checksum is invalid: "
                f"{item.book_key}"
            )
        ledger_by_key[item.book_key] = item
    for audit in (*rejections, *exclusions):
        if cancel_check is not None:
            cancel_check()
        identity = ledger_by_key[audit.book_key]
        if (
            audit.source_id,
            audit.source_filename,
            audit.page_type,
            audit.folder_path,
            audit.short_name,
            audit.display_name,
        ) != (
            identity.source_id,
            identity.source_filename,
            identity.page_type,
            identity.folder_path,
            identity.short_name,
            identity.display_name,
        ):
            raise ProductRunnerError(
                f"approved audit identity does not match recognized Book: "
                f"{audit.book_key}"
            )
    for attribution in attributions:
        if cancel_check is not None:
            cancel_check()
        identity = ledger_by_key[attribution.book_key]
        if not _is_sha256(attribution.payload_checksum):
            raise ProductRunnerError(
                f"approved attribution payload checksum is invalid: "
                f"{attribution.book_key}"
            )
        if (
            attribution.source_id,
            attribution.source_filename,
            attribution.page_type,
            attribution.folder_path,
            attribution.short_name,
            attribution.display_name,
            attribution.payload_checksum.casefold(),
        ) != (
            identity.source_id,
            identity.source_filename,
            identity.page_type,
            identity.folder_path,
            identity.short_name,
            identity.display_name,
            identity.payload_checksum.casefold(),
        ):
            raise ProductRunnerError(
                f"approved attribution identity does not match recognized "
                f"Book: {attribution.book_key}"
            )


def _verify_task_snapshot_book_ledger(
    snapshot_path: Path,
    expected_snapshot_sha256: str,
    recognized_books: tuple[ApprovedBookIdentity, ...],
    approved_sources: tuple[ApprovedSourceFingerprint, ...],
    accepted_spectra: tuple[OutputSpectrum, ...],
    rejections: tuple[ApprovedAuditItem, ...],
    attributions: tuple[ApprovedAttribution, ...],
    review_requirements: tuple[ApprovedReviewRequirement, ...],
    review_choices: tuple[ApprovedReviewChoice, ...],
    exclusions: tuple[ApprovedAuditItem, ...],
    *,
    cancel_check=None,
) -> None:
    if cancel_check is not None:
        cancel_check()
    source_filename_by_id = _approved_source_filename_by_id(
        approved_sources
    )
    try:
        connection = sqlite3.connect(
            f"{snapshot_path.resolve().as_uri()}?mode=ro",
            uri=True,
        )
        try:
            connection.execute("begin")
            transaction_sha256 = sqlite_content_sha256(
                connection,
                cancel_check=cancel_check,
            )
            if (
                transaction_sha256.casefold()
                != expected_snapshot_sha256
            ):
                raise ProductRunnerError(
                    "approved task snapshot changed before approval"
                )
            source_rows = tuple(
                connection.execute(
                    "select source_id, copy_path, sha256, "
                    "original_path, original_size_bytes, "
                    "original_mtime_ns "
                    "from source_files order by source_id"
                )
            )
            rows = tuple(
                connection.execute(
                    "select source_id, page_type, folder_path, short_name, "
                    "display_name, payload_checksum, status, "
                    "rejection_reason, selected_x_values_json, "
                    "selected_y_values_json, note_text, spectrum_class, "
                    "selected_y_column, paired_x_column, "
                    "s1_max_for_limit_json, "
                    "s1_max_for_limit_x_json, "
                    "max_planned_y_json, "
                    "max_planned_y_x_json from book_results "
                    "order by source_id, folder_path, short_name, page_type"
                )
            )
        finally:
            connection.close()
        from spectrum_organizer.store.run_snapshot import (
            snapshot_approval_sha256,
        )

        actual_sha256 = snapshot_approval_sha256(
            snapshot_path,
            cancel_check=cancel_check,
        )
    except Exception as exc:
        raise ProductRunnerError(
            f"approved task snapshot could not be verified: {exc}"
        ) from exc
    if actual_sha256.casefold() != expected_snapshot_sha256:
        raise ProductRunnerError(
            "approved task snapshot changed before approval"
        )
    expected_source_rows = tuple(
        sorted(
            (
                source.source_id,
                _canonical_source_snapshot_path(source.snapshot),
                source.snapshot.sha256.casefold(),
                source.snapshot.size_bytes,
                source.snapshot.mtime_ns,
            )
            for source in approved_sources
        )
    )
    actual_source_rows = tuple(
        (
            str(source_id),
            original_path,
            str(sha256).casefold(),
            original_size_bytes,
            original_mtime_ns,
        )
        for (
            source_id,
            copy_path,
            sha256,
            original_path,
            original_size_bytes,
            original_mtime_ns,
        ) in source_rows
        if (
            type(copy_path) is str
            and copy_path.strip()
            and type(original_path) is str
            and original_path.strip()
            and type(original_size_bytes) is int
            and type(original_mtime_ns) is int
        )
    )
    if (
        len(actual_source_rows) != len(source_rows)
        or actual_source_rows != expected_source_rows
    ):
        raise ProductRunnerError(
            "approved source fingerprints do not match task snapshot"
        )
    actual = tuple(
        sorted(
            (
                json.dumps(
                    [source_id, page_type, folder_path, short_name],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                source_id,
                source_filename_by_id.get(source_id, ""),
                page_type,
                folder_path,
                short_name,
                display_name,
                payload_checksum,
                str(spectrum_class or ""),
                str(selected_y_column or ""),
                str(paired_x_column or ""),
            )
            for (
                source_id,
                page_type,
                folder_path,
                short_name,
                display_name,
                payload_checksum,
                _status,
                _rejection_reason,
                _selected_x_values_json,
                _selected_y_values_json,
                _note_text,
                spectrum_class,
                selected_y_column,
                paired_x_column,
                _s1_max_for_limit_json,
                _s1_max_for_limit_x_json,
                _max_planned_y_json,
                _max_planned_y_x_json,
            ) in rows
        )
    )
    expected = tuple(
        sorted(
            (
                item.book_key,
                item.source_id,
                item.source_filename,
                item.page_type,
                item.folder_path,
                item.short_name,
                item.raw_display_name,
                item.payload_checksum,
                item.spectrum_class,
                item.selected_y_column,
                item.paired_x_column,
            )
            for item in recognized_books
        )
    )
    if actual != expected:
        raise ProductRunnerError(
            "approved Book identities do not match task snapshot"
        )
    row_by_key = {
        json.dumps(
            [row[0], row[1], row[2], row[3]],
            ensure_ascii=False,
            separators=(",", ":"),
        ): row
        for row in rows
    }
    for attribution in attributions:
        if cancel_check is not None:
            cancel_check()
        if row_by_key[attribution.book_key][6] != "extracted":
            raise ProductRunnerError(
                "approved candidate was not extracted in task snapshot"
            )
    rejection_by_key = {
        item.book_key: item
        for item in rejections
    }
    for book_key, item in rejection_by_key.items():
        if cancel_check is not None:
            cancel_check()
        row = row_by_key[book_key]
        _validate_approved_rejection(item, row)
    for spectrum in accepted_spectra:
        if cancel_check is not None:
            cancel_check()
        row = row_by_key[spectrum.spectrum_id]
        try:
            source_x = tuple(json.loads(row[8] or "[]"))
            source_y = tuple(json.loads(row[9] or "[]"))
            source_xy = tuple(
                (
                    Decimal(str(x_value)),
                    Decimal(str(y_value)),
                )
                for x_value, y_value in zip(
                    source_x,
                    source_y,
                    strict=True,
                )
            )
            approved_xy = tuple(
                (
                    Decimal(str(x_value)),
                    Decimal(str(y_value)),
                )
                for x_value, y_value in spectrum.x_y
            )
        except (
            InvalidOperation,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            raise ProductRunnerError(
                "approved spectrum payload is invalid"
            ) from exc
        if source_xy != approved_xy:
            raise ProductRunnerError(
                "approved spectrum payload does not match task snapshot"
            )
        _validate_approved_spectrum_metadata(spectrum, row)
    _validate_recomputed_task7_ledger(
        rows,
        attributions,
        review_requirements,
        review_choices,
        accepted_spectra,
        rejections,
        exclusions,
        cancel_check=cancel_check,
    )
    for item in (*rejections, *exclusions):
        if item.detail != canonical_audit_detail(
            item.reason_code,
            item.evidence,
        ):
            raise ProductRunnerError(
                f"approved audit detail is invalid: {item.book_key}"
            )


def _validate_approved_rejection(
    item: ApprovedAuditItem,
    snapshot_row: tuple[object, ...],
) -> None:
    if item.decision_source != "automatic":
        raise ProductRunnerError(
            "approved rejection decision source must be automatic"
        )
    _note, expected_reason = _snapshot_candidate_note_and_reason(
        snapshot_row
    )
    expected_evidence = _snapshot_rejection_evidence(snapshot_row)
    if expected_reason is not None:
        if (
            item.reason_code != expected_reason
            or item.evidence != expected_evidence
        ):
            raise ProductRunnerError(
                "approved rejection reason does not match task snapshot"
            )
        return
    if item.reason_code != "normalization_nonpositive_max":
        raise ProductRunnerError(
            "approved extracted Book has no proven rejection reason"
        )
    try:
        maximum = Decimal(str(json.loads(snapshot_row[16])))
    except (
        InvalidOperation,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        raise ProductRunnerError(
            "approved normalization rejection maximum is invalid"
        ) from exc
    if not maximum.is_finite() or maximum > 0:
        raise ProductRunnerError(
            "approved normalization rejection predicate is false"
        )
    normalization_evidence = tuple(
        (name, value)
        for name, value in expected_evidence
        if name in {"max_y", "x_at_max_y"}
    )
    if item.evidence != normalization_evidence:
        raise ProductRunnerError(
            "approved normalization rejection evidence is invalid"
        )


def _snapshot_rejection_evidence(
    snapshot_row: tuple[object, ...],
) -> tuple[tuple[str, str], ...]:
    values = tuple(
        _json_snapshot_value(snapshot_row[index])
        for index in range(14, 18)
    )
    return tuple(
        (name, _approved_measurement_text(value))
        for name, value in zip(
            (
                "s1_max",
                "x_at_s1_max",
                "max_y",
                "x_at_max_y",
            ),
            values,
            strict=True,
        )
        if value is not None
    )


def _snapshot_candidate_note_and_reason(
    snapshot_row: tuple[object, ...],
):
    from spectrum_organizer.core.selection import (
        required_note_metadata_error,
    )
    from spectrum_organizer.core.note_parser import (
        NoteParseError,
        parse_book_note,
    )

    status = str(snapshot_row[6])
    stored_reason = str(snapshot_row[7] or status)
    try:
        note = parse_book_note(str(snapshot_row[10] or ""))
    except NoteParseError as exc:
        return None, str(snapshot_row[7] or exc)
    stored_class = str(snapshot_row[11] or "")
    if stored_class != note.spectrum_class.value:
        return None, "stored spectrum class does not match Note"
    if status != "extracted":
        return None, stored_reason
    return note, required_note_metadata_error(note)


def _validate_recomputed_task7_ledger(
    snapshot_rows: tuple[tuple[object, ...], ...],
    attributions: tuple[ApprovedAttribution, ...],
    review_requirements: tuple[ApprovedReviewRequirement, ...],
    review_choices: tuple[ApprovedReviewChoice, ...],
    accepted_spectra: tuple[OutputSpectrum, ...],
    rejections: tuple[ApprovedAuditItem, ...],
    exclusions: tuple[ApprovedAuditItem, ...],
    *,
    cancel_check=None,
) -> None:
    from spectrum_organizer.core.selection import (
        SelectionSpectrum,
        filter_copyable_emissions_after_special,
        review_emission_duplicates,
        select_excitation_candidates,
    )
    from spectrum_organizer.core.special_groups import (
        SpectrumBook,
        classify_special_groups,
        resolve_special_group_selection,
    )

    attribution_by_key = {
        attribution.book_key: attribution
        for attribution in attributions
    }
    candidates = []
    special_books = []
    valid_candidate_keys = []
    for row in snapshot_rows:
        if cancel_check is not None:
            cancel_check()
        book_key = _snapshot_row_book_key(row)
        note, rejection_reason = _snapshot_candidate_note_and_reason(row)
        if rejection_reason is not None:
            continue
        if note is None:
            raise ProductRunnerError(
                "approved candidate Note could not be reconstructed"
            )
        attribution = attribution_by_key.get(book_key)
        if attribution is None:
            raise ProductRunnerError(
                "attributions do not exactly cover extracted candidates"
            )
        sample_identity = json.dumps(
            [
                attribution.sample_system_identity,
                attribution.temperature,
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        selection, special = _recomputed_candidate_models(
            row,
            note,
            attribution,
            sample_identity,
            SelectionSpectrum,
            SpectrumBook,
        )
        candidates.append(selection)
        special_books.append(special)
        valid_candidate_keys.append(book_key)
    if set(attribution_by_key) != set(valid_candidate_keys):
        raise ProductRunnerError(
            "attributions do not exactly cover extracted candidates"
        )

    expected_requirements = []
    choice_index = 0

    def consume(
        kind: str,
        review_key: str,
        candidate_book_keys: tuple[str, ...],
        *,
        subject: str = "",
    ) -> ApprovedReviewChoice:
        nonlocal choice_index
        expected_requirements.append(
            ApprovedReviewRequirement(
                kind,
                review_key,
                candidate_book_keys,
            )
        )
        if choice_index >= len(review_choices):
            raise ProductRunnerError(
                "recomputed required review has no approved choice"
            )
        choice = review_choices[choice_index]
        choice_index += 1
        if (
            choice.kind,
            choice.review_key,
            choice.candidate_book_keys,
            choice.decision_source,
            choice.subject,
        ) != (
            kind,
            review_key,
            candidate_book_keys,
            "manual",
            subject,
        ):
            raise ProductRunnerError(
                "approved review ledger does not match recomputed "
                "required reviews"
            )
        return choice

    duplicate_choices = {}
    overlap_choices = {}
    while True:
        if cancel_check is not None:
            cancel_check()
        special_result = classify_special_groups(
            special_books,
            duplicate_choices=duplicate_choices,
            overlap_choices=overlap_choices,
        )
        if special_result.pending_duplicate_reviews:
            context = (
                special_result.pending_duplicate_reviews[0]
                .context_book_keys
            )
            pending_batch = tuple(
                pending
                for pending in special_result.pending_duplicate_reviews
                if pending.context_book_keys == context
            )
            for pending in pending_batch:
                choice = consume(
                    "special_duplicate",
                    repr(pending.choice_key),
                    pending.book_keys,
                    subject=pending.kind,
                )
                if choice.decision or len(choice.selected_book_keys) != 1:
                    raise ProductRunnerError(
                        "approved special-duplicate choice is invalid"
                    )
                duplicate_choices[pending.choice_key] = (
                    choice.selected_book_keys[0]
                )
            continue
        if special_result.pending_overlap_assignments:
            context = (
                special_result.pending_overlap_assignments[0]
                .context_book_keys
            )
            pending_batch = tuple(
                pending
                for pending
                in special_result.pending_overlap_assignments
                if pending.context_book_keys == context
            )
            for pending in pending_batch:
                choice = consume(
                    "special_overlap",
                    repr(pending.book_key),
                    (pending.book_key,),
                )
                if (
                    choice.selected_book_keys != (pending.book_key,)
                    or choice.decision
                    not in {
                        "delayed_2d",
                        "delay_time_series",
                        "regular",
                    }
                ):
                    raise ProductRunnerError(
                        "approved special-overlap choice is invalid"
                    )
                overlap_choices[pending.book_key] = choice.decision
            continue
        break

    accepted_special_keys = []
    rejected_special_keys = []
    regular_delayed_keys = list(
        special_result.regular_delayed_book_keys
    )
    delayed_emission_keys = {
        book.book_key
        for book in special_books
        if book.spectrum_class is SpectrumClass.DELAYED_EMISSION
    }
    for group in special_result.groups:
        if cancel_check is not None:
            cancel_check()
        if group.kind == "steady_2d":
            accepted_special_keys.extend(group.book_keys)
            continue
        choice = consume(
            "special_group",
            repr((group.kind, group.book_keys)),
            group.book_keys,
            subject=group.kind,
        )
        if choice.decision == "confirm_group":
            accepted_special_keys.extend(group.book_keys)
        elif choice.decision == "reject_group":
            rejected_special_keys.extend(group.book_keys)
            regular_delayed_keys.extend(
                book_key
                for book_key in group.book_keys
                if book_key in delayed_emission_keys
            )
        elif choice.decision == "confirm_selection":
            accepted, ordinary_keys = resolve_special_group_selection(
                group,
                choice.selected_book_keys,
            )
            if accepted is not None:
                accepted_special_keys.extend(accepted.book_keys)
            else:
                rejected_special_keys.extend(group.book_keys)
            regular_delayed_keys.extend(
                book_key
                for book_key in ordinary_keys
                if book_key in delayed_emission_keys
            )
        else:
            raise ProductRunnerError(
                "approved special-group choice is invalid"
            )

    copyable_emissions = filter_copyable_emissions_after_special(
        candidates,
        regular_delayed_book_keys=tuple(
            dict.fromkeys(regular_delayed_keys)
        ),
        special_group_book_keys=tuple(accepted_special_keys),
    )
    emission_choices = {}
    while True:
        if cancel_check is not None:
            cancel_check()
        emission_result = review_emission_duplicates(
            list(copyable_emissions),
            choices=emission_choices,
        )
        if not emission_result.pending_reviews:
            break
        pending = emission_result.pending_reviews[0]
        choice = consume(
            "emission",
            repr(pending.review_key),
            pending.book_keys,
        )
        if choice.decision or len(choice.selected_book_keys) != 1:
            raise ProductRunnerError(
                "approved emission choice is invalid"
            )
        emission_choices[pending.review_key] = (
            choice.selected_book_keys[0]
        )

    excitation_choices = {}
    while True:
        if cancel_check is not None:
            cancel_check()
        excitation_result = select_excitation_candidates(
            candidates,
            choices=excitation_choices,
        )
        if not excitation_result.pending_reviews:
            break
        pending = excitation_result.pending_reviews[0]
        choice = consume(
            "excitation",
            repr(pending.review_key),
            pending.book_keys,
        )
        if choice.decision:
            raise ProductRunnerError(
                "approved excitation choice is invalid"
            )
        excitation_choices[pending.review_key] = (
            choice.selected_book_keys
        )

    for group in special_result.groups:
        if cancel_check is not None:
            cancel_check()
        if group.kind != "steady_2d":
            continue
        book_keys = tuple(group.book_keys)
        review_key = f"automatic:{book_keys!r}"
        expected_requirements.append(
            ApprovedReviewRequirement(
                "special_group",
                review_key,
                book_keys,
                "automatic",
            )
        )
        if choice_index >= len(review_choices):
            raise ProductRunnerError(
                "automatic special-group review is missing"
            )
        choice = review_choices[choice_index]
        choice_index += 1
        if choice != ApprovedReviewChoice(
            "special_group",
            review_key,
            book_keys,
            book_keys,
            "confirm_group",
            "steady_2d",
            "automatic",
        ):
            raise ProductRunnerError(
                "automatic special-group review is invalid"
            )
    if (
        choice_index != len(review_choices)
        or tuple(expected_requirements) != review_requirements
    ):
        raise ProductRunnerError(
            "approved review ledger does not match recomputed "
            "required reviews"
        )

    selected_book_keys = (
        *emission_result.selected_book_keys,
        *excitation_result.selected_book_keys,
    )
    normalization_rejection_keys = {
        item.book_key
        for item in rejections
        if item.reason_code == "normalization_nonpositive_max"
    }
    if not normalization_rejection_keys.issubset(
        set(selected_book_keys)
    ):
        raise ProductRunnerError(
            "normalization rejection was not selected for output"
        )
    expected_accepted_keys = tuple(
        book_key
        for book_key in selected_book_keys
        if book_key not in normalization_rejection_keys
    )
    if tuple(
        spectrum.spectrum_id for spectrum in accepted_spectra
    ) != expected_accepted_keys:
        raise ProductRunnerError(
            "approved spectra do not match recomputed Task 7 selection"
        )
    expected_exclusion_keys = (
        set(valid_candidate_keys) - set(selected_book_keys)
    )
    expected_exclusion_reasons = {
        book_key: "special_group_not_copied_to_ordinary_output"
        for book_key in accepted_special_keys
    }
    expected_exclusion_reasons.update(
        {
            book_key: "special_group_rejected"
            for book_key in rejected_special_keys
            if book_key not in set(regular_delayed_keys)
        }
    )
    for choice in review_choices:
        if choice.kind != "special_duplicate":
            continue
        expected_exclusion_reasons.update(
            {
                book_key: "special_duplicate_unselected"
                for book_key in (
                    set(choice.candidate_book_keys)
                    - set(choice.selected_book_keys)
                )
                if book_key in expected_exclusion_keys
            }
        )
    expected_exclusion_reasons.update(
        {
            exclusion.book_key: exclusion.reason
            for exclusion in (
                *emission_result.exclusions,
                *excitation_result.exclusions,
            )
        }
    )
    actual_exclusion_reasons = {
        item.book_key: item.reason_code
        for item in exclusions
    }
    if (
        set(actual_exclusion_reasons) != expected_exclusion_keys
        or actual_exclusion_reasons != expected_exclusion_reasons
    ):
        raise ProductRunnerError(
            "approved exclusions do not match recomputed Task 7 "
            "selection"
        )
    expected_original_rejections = (
        {
            _snapshot_row_book_key(row)
            for row in snapshot_rows
        }
        - set(valid_candidate_keys)
    )
    actual_original_rejections = {
        item.book_key for item in rejections
    } - normalization_rejection_keys
    if actual_original_rejections != expected_original_rejections:
        raise ProductRunnerError(
            "approved rejections do not match recomputed candidates"
        )


def _snapshot_row_book_key(row: tuple[object, ...]) -> str:
    return json.dumps(
        [row[0], row[1], row[2], row[3]],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _recomputed_candidate_models(
    row,
    note,
    attribution,
    sample_identity,
    selection_type,
    special_type,
):
    spectrum_class = note.spectrum_class
    is_excitation = spectrum_class in {
        SpectrumClass.STEADY_EXCITATION,
        SpectrumClass.DELAYED_EXCITATION,
    }
    wavelength_range = (
        note.excitation_range
        if is_excitation
        else note.emission_range
    )
    scan_increment = (
        note.excitation_increment
        if is_excitation
        else note.emission_increment
    )
    fixed_wavelength = (
        note.fixed_emission_wavelength
        if is_excitation
        else note.fixed_excitation_wavelength
    )
    delayed = note.delay
    excitation_slit = format_raw_slit_fields(note.excitation_slits)
    emission_slit = format_raw_slit_fields(note.emission_slits)
    selection = selection_type(
        source_id=str(row[0]),
        source_filename=attribution.source_filename,
        folder_path=str(row[2]),
        book_name=str(row[3]),
        display_name=str(row[4] or row[3]),
        default_name=str(row[3]),
        spectrum_class=spectrum_class,
        sample_system=sample_identity,
        temperature=attribution.temperature,
        page_type=str(row[1]),
        fixed_excitation_wavelength=(
            None if is_excitation else fixed_wavelength
        ),
        fixed_receiving_wavelength=(
            fixed_wavelength if is_excitation else None
        ),
        excitation_slit=excitation_slit,
        emission_slit=emission_slit,
        flash_delay=(
            None if delayed is None else delayed.flash_delay
        ),
        sample_window=(
            None if delayed is None else delayed.sample_window
        ),
        time_per_flash=(
            None if delayed is None else delayed.time_per_flash
        ),
        flash_count=(
            None if delayed is None else delayed.flash_count
        ),
        scan_start=(
            None if wavelength_range is None else wavelength_range[0]
        ),
        scan_stop=(
            None if wavelength_range is None else wavelength_range[1]
        ),
        scan_step=scan_increment,
        note_datetime=note.note_datetime,
    )
    special = special_type(
        source_id=str(row[0]),
        folder_path=str(row[2]),
        book_name=str(row[3]),
        spectrum_class=spectrum_class,
        sample_label=sample_identity,
        page_type=str(row[1]),
        fixed_excitation_wavelength=(
            fixed_wavelength
            if spectrum_class == SpectrumClass.DELAYED_EMISSION
            else None
        ),
        receiving_range=wavelength_range,
        excitation_slit=excitation_slit,
        emission_slit=emission_slit,
        flash_delay=(
            None if delayed is None else delayed.flash_delay
        ),
        sample_window=(
            None if delayed is None else delayed.sample_window
        ),
        time_per_flash=(
            None if delayed is None else delayed.time_per_flash
        ),
        flash_count=(
            None if delayed is None else delayed.flash_count
        ),
    )
    return selection, special


def _json_snapshot_value(value):
    if value is None:
        return None
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ProductRunnerError(
            "approved task snapshot measurement is invalid"
        ) from exc


def _validate_approved_spectrum_metadata(
    spectrum: OutputSpectrum,
    snapshot_row: tuple[object, ...],
) -> None:
    from spectrum_organizer.core.note_parser import (
        NoteParseError,
        parse_book_note,
    )

    try:
        note = parse_book_note(str(snapshot_row[10] or ""))
    except NoteParseError as exc:
        raise ProductRunnerError(
            "approved spectrum Note is invalid"
        ) from exc
    stored_class = str(snapshot_row[11] or "")
    if (
        note.spectrum_class != spectrum.spectrum_class
        or stored_class != spectrum.spectrum_class.value
    ):
        raise ProductRunnerError(
            "approved spectrum class does not match task snapshot"
        )
    if spectrum.is_excitation:
        key_wavelength = note.fixed_emission_wavelength
        scan_range = note.excitation_range
        scan_step = note.excitation_increment
    else:
        key_wavelength = note.fixed_excitation_wavelength
        scan_range = note.emission_range
        scan_step = note.emission_increment
    expected_range = scan_range or (None, None)
    delay = note.delay
    expected = (
        str(key_wavelength or ""),
        note.excitation_slits,
        note.emission_slits,
        None if delay is None else delay.flash_delay,
        None if delay is None else delay.sample_window,
        None if delay is None else delay.time_per_flash,
        None if delay is None else delay.flash_count,
        expected_range[0],
        expected_range[1],
        scan_step,
    )
    actual = (
        spectrum.key_wavelength,
        spectrum.excitation_slit,
        spectrum.emission_slit,
        spectrum.flash_delay,
        spectrum.sample_window,
        spectrum.time_per_flash,
        spectrum.flash_count,
        spectrum.scan_start,
        spectrum.scan_stop,
        spectrum.scan_step,
    )
    if actual != expected:
        raise ProductRunnerError(
            "approved spectrum metadata does not match task snapshot"
        )


def _is_sha256(value: object) -> bool:
    text = str(value)
    return (
        len(text) == 64
        and all(
            character in "0123456789abcdef"
            for character in text.casefold()
        )
    )


class ExtractionSourceManager:
    def __init__(
        self,
        sources: tuple[object, ...],
        snapshots: tuple[SourceSnapshot, ...],
        *,
        temp_root: Path | None = None,
        cancel_check: Callable[[], None] | None = None,
    ):
        self._sources = {source.source_id: source for source in sources}
        self._snapshots = {f"S{index:04d}": snapshot for index, snapshot in enumerate(snapshots, start=1)}
        self._copy_paths = {source.source_id: Path(source.copy_path) for source in sources}
        self._copy_identities = {
            source_id: path_identity(path)
            for source_id, path in self._copy_paths.items()
        }
        self._temp_root = Path(temp_root).resolve() if temp_root is not None else None
        self._cancel_check = cancel_check

    def verify_original(self, source_id: str) -> None:
        verify_sources_unchanged([self._snapshots[source_id]], cancel_check=self._cancel_check)

    def verify_copy(self, source_id: str) -> None:
        snapshot = self._snapshots[source_id]
        copy_path = self._copy_paths[source_id]
        if (
            copy_path.stat().st_size != snapshot.size_bytes
            or hash_file(copy_path, cancel_check=self._cancel_check) != snapshot.sha256
        ):
            raise ProductRunnerError(f"Source copy changed or mismatched: {copy_path}")

    def verify_after_worker(self, source_id: str) -> None:
        errors: list[str] = []
        snapshot = self._snapshots[source_id]
        copy_path = self._copy_paths[source_id]
        try:
            verify_sources_unchanged([snapshot])
        except Exception as exc:
            errors.append(str(exc))
        try:
            if copy_path.stat().st_size != snapshot.size_bytes or hash_file(copy_path) != snapshot.sha256:
                errors.append(f"Source copy changed or mismatched: {copy_path}")
        except Exception as exc:
            errors.append(str(exc))
        if errors:
            raise ProductRunnerError("; ".join(errors))

    def discard_failed_copy(self, source_id: str) -> None:
        copy_path = self._copy_paths[source_id]
        if not lexical_path_exists(copy_path):
            self._copy_identities.pop(source_id, None)
            return
        expected_identity = self._copy_identities.get(source_id)
        if expected_identity is None:
            raise ExtractionCleanupBlockedError(
                f"提取重试前缺少失败临时副本身份：{copy_path}"
            )
        try:
            unlink_owned_path(copy_path, expected_identity)
        except IdentityPathError as exc:
            retained = Path(getattr(exc, "retained_path", copy_path))
            raise ExtractionCleanupBlockedError(
                f"提取重试前无法清理失败的临时副本：{retained}；{exc}"
            ) from exc
        self._copy_identities.pop(source_id, None)
        if lexical_path_exists(copy_path):
            raise ExtractionCleanupBlockedError(
                f"提取重试前失败的临时副本仍然存在：{copy_path}"
            )

    def refresh_copy(self, source_id: str) -> None:
        snapshot = self._snapshots[source_id]
        current_copy = self._copy_paths[source_id]
        retry_index = 1
        while True:
            retry_suffix = ".retry" if retry_index == 1 else f".retry{retry_index}"
            retry_copy = current_copy.with_name(f"{current_copy.stem}{retry_suffix}{current_copy.suffix}")
            if not retry_copy.exists():
                break
            retry_index += 1
        retry_copy.parent.mkdir(parents=True, exist_ok=True)
        ownership = None
        if self._temp_root is not None:
            ownership = read_ownership(self._temp_root)
            if retry_copy not in ownership.allowed_children:
                ownership = add_allowed_child(ownership, retry_copy)

        def bind_retry_copy(path, identity):
            nonlocal ownership
            if ownership is None:
                return
            ownership = bind_allowed_child_identity(
                ownership,
                path,
                expected_identity=identity,
            )

        retry_identity = _copy_file_cancellable(
            snapshot.path,
            retry_copy,
            cancel_check=self._cancel_check,
            creation_callback=bind_retry_copy,
        )
        try:
            if path_identity(retry_copy) != retry_identity:
                raise ProductRunnerError(
                    f"Retry copy identity changed after creation: {retry_copy}"
                )
        except IdentityPathError as exc:
            raise ProductRunnerError(str(exc)) from exc
        self._copy_paths[source_id] = retry_copy
        self._copy_identities[source_id] = retry_identity
        self.verify_copy(source_id)
        return retry_copy


def _record_extraction_target_attempt(context, source, reader_attempt: int) -> None:
    if not runtime_audit_enabled():
        return
    copy_path = Path(source.copy_path).resolve(strict=True)
    record_runtime_audit_event(
        "origin_extraction_target_attempt",
        {
            "run_id": context.run_id,
            "source_id": source.source_id,
            "reader_attempt": reader_attempt,
            "copy_path": str(copy_path),
            "copy_identity": runtime_audit_file_identity(copy_path),
        },
    )


def _record_extraction_retry_cleanup(
    context,
    *,
    source_id: str,
    reader_attempt: int,
    failed_copy: Path,
    replacement_copy: Path,
) -> None:
    if not runtime_audit_enabled():
        return
    replacement = Path(replacement_copy).resolve(strict=True)
    record_runtime_audit_event(
        "origin_extraction_retry_cleanup",
        {
            "run_id": context.run_id,
            "source_id": source_id,
            "reader_attempt": reader_attempt,
            "failed_copy_path": str(Path(failed_copy).resolve()),
            "replacement_copy_path": str(replacement),
            "replacement_copy_identity": runtime_audit_file_identity(
                replacement
            ),
            "completed": True,
        },
    )


def _copy_file_cancellable(
    source: Path,
    target: Path,
    *,
    cancel_check=None,
    creation_callback=None,
) -> tuple[int, int]:
    with Path(source).open("rb") as reader, Path(target).open("xb") as writer:
        status = os.fstat(writer.fileno())
        identity = (status.st_dev, status.st_ino)
        while True:
            if cancel_check is not None:
                cancel_check()
            chunk = reader.read(1024 * 1024)
            if not chunk:
                break
            writer.write(chunk)
        writer.flush()
        os.fsync(writer.fileno())
        shutil.copystat(source, target)
        if path_identity(target) != identity:
            raise ProductRunnerError(
                f"Retry copy identity changed during creation: {target}"
            )
        if creation_callback is not None:
            creation_callback(Path(target), identity)
    if cancel_check is not None:
        cancel_check()
    return identity


class ProtectedPathAuditHook:
    readiness_kind = "protected_path_audit"

    def __call__(self) -> ProtectedPathAuditPlan:
        return ProtectedPathAuditPlan()


class FinalProcessCountHook:
    readiness_kind = "final_process_count"
    expected_origin_process_count = 0

    def __init__(self, process_probe: Callable | None = None):
        self.process_probe = process_probe

    def __call__(self) -> int:
        if self.process_probe is None:
            raise ProductRunnerError("final process-count hook requires a real process probe")
        return len(tuple(self.process_probe(timeout=5.0)))


def prepare_approved_pre_extraction_context(
    *,
    selected_source_paths,
    output_parent,
    settings_snapshot: dict[str, object],
    local_appdata,
    protected_paths=(),
    dialog_port,
    origin_process_probe,
    process_controller,
    free_bytes_provider: Callable[[Path], int] | None = None,
    copy_file: Callable[[Path, Path], None] | None = None,
    run_id_factory: Callable[[], str] | None = None,
    marker_id_factory: Callable[[], str] | None = None,
    timestamp_factory: Callable[[], str] | None = None,
    run_origin_process_preflight: bool = True,
    precreated_ownership=None,
) -> ApprovedPreExtractionRunContext:
    if precreated_ownership is not None:
        run_id = precreated_ownership.run_id
        marker_id = precreated_ownership.marker_id
    else:
        run_id = run_id_factory() if run_id_factory is not None else uuid.uuid4().hex
        marker_id = marker_id_factory() if marker_id_factory is not None else uuid.uuid4().hex
    timestamp = (
        timestamp_factory()
        if timestamp_factory is not None
        else datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    )
    ownership = precreated_ownership
    try:
        if ownership is None:
            ownership = create_run_ownership(
                local_appdata,
                run_id,
                marker_id,
                [Path(path) for path in protected_paths],
            )
        context = prepare_extraction_context(
            selected_source_paths=selected_source_paths,
            output_parent=output_parent,
            settings_snapshot=settings_snapshot,
            protected_paths=protected_paths,
            ownership=ownership,
            timestamp=timestamp,
            free_bytes_provider=free_bytes_provider,
            copy_file=copy_file,
        )

        if run_origin_process_preflight:
            complete_pre_extraction_origin_process_gate(dialog_port, origin_process_probe, process_controller)
    except (Exception, KeyboardInterrupt) as exc:
        if precreated_ownership is None:
            cleanup_error = _cleanup_temp_root_error(
                ownership.temp_root if ownership is not None else None,
                expected_root_identity=(
                    ownership.temp_root_identity
                    if ownership is not None
                    else None
                ),
            )
            if cleanup_error is not None:
                if isinstance(exc, KeyboardInterrupt):
                    exc.add_note(f"临时文件清理失败：{cleanup_error}")
                    raise
                raise ExtractionCleanupBlockedError(
                    f"{exc}; 临时文件清理失败：{cleanup_error}"
                ) from exc
        raise

    return context


class PreExtractionSubprocessRunner:
    def __init__(
        self,
        *,
        local_appdata=None,
        protected_paths=(),
        process_factory=None,
        cancellation_timeout: float = 12.0,
        cancellation_poll_interval: float = 0.1,
    ):
        if not is_finite_real_number(cancellation_timeout) or cancellation_timeout <= 0:
            raise ValueError("cancellation_timeout must be a finite value greater than zero")
        if not is_finite_real_number(cancellation_poll_interval) or cancellation_poll_interval <= 0:
            raise ValueError("cancellation_poll_interval must be a finite value greater than zero")
        self.local_appdata = local_appdata
        self.protected_paths = tuple(Path(path) for path in protected_paths)
        self.process_factory = process_factory or subprocess.Popen
        self._require_process_job = process_factory is None and sys.platform == "win32"
        self.cancellation_timeout = cancellation_timeout
        self.cancellation_poll_interval = cancellation_poll_interval
        self._cancelled = threading.Event()
        self._state_lock = threading.Lock()
        self._current_process = None
        self._termination_process = None
        self._termination_finalized = False
        self._cleanup_blocked_reason = None
        self._cleanup_temp_root = None
        self._cleanup_temp_root_identity = None
        self._active = False
        self._start_gate_identity = None

    def reset(self) -> None:
        with self._state_lock:
            if self._active:
                raise ProductRunnerError("提取前复制子进程仍在运行")
            if self._cleanup_blocked_reason is not None:
                raise ProductRunnerError(f"提取前复制清理状态不可确认：{self._cleanup_blocked_reason}")
            if self._termination_process is not None and self._termination_process.is_alive():
                raise ProductRunnerError("提取前复制子进程终止线程仍在运行")
            self._termination_process = None
            self._termination_finalized = False
            self._cleanup_temp_root = None
            self._cleanup_temp_root_identity = None
            self._start_gate_identity = None
            self._cancelled.clear()

    def cancel(self) -> None:
        with self._state_lock:
            if (
                self._current_process is not None
                and self._termination_process is None
                and not self._termination_finalized
            ):
                self._termination_process = _terminate_process_nonblocking(self._current_process)
            self._cancelled.set()

    def retry_cancel_cleanup(self) -> None:
        with self._state_lock:
            if self._cleanup_blocked_reason is None:
                return
            process = self._current_process
            temp_root = self._cleanup_temp_root
            temp_root_identity = self._cleanup_temp_root_identity
            termination_process = self._termination_process
            is_alive = getattr(termination_process, "is_alive", None)
            if termination_process is not None and callable(is_alive) and is_alive():
                raise ExtractionCleanupBlockedError("提取前复制子进程终止线程仍在运行")
            if process is None and temp_root is None:
                raise ExtractionCleanupBlockedError("提取前复制清理状态不可确认")
            self._termination_process = None
            self._termination_finalized = False
            self._cleanup_blocked_reason = None
            if process is not None and process.poll() is None:
                self._termination_process = _terminate_process_nonblocking(process)
            self._cancelled.set()
        try:
            if process is not None:
                self._wait_for_termination_process(process)
            cleanup_error = _cleanup_temp_root_error(
                temp_root,
                expected_root_identity=temp_root_identity,
            )
            if cleanup_error is not None:
                raise ExtractionCleanupBlockedError(f"临时文件清理失败：{cleanup_error}")
        except ExtractionCleanupBlockedError as exc:
            with self._state_lock:
                self._cleanup_blocked_reason = str(exc)
            raise
        with self._state_lock:
            self._current_process = None
            self._cleanup_temp_root = None
            self._cleanup_temp_root_identity = None
            self._cleanup_blocked_reason = None

    def _release_start_gate(
        self,
        process,
        start_gate_path: Path | None,
        start_gate_token: str = "ready",
    ) -> bool:
        with self._state_lock:
            if self._cancelled.is_set():
                if self._termination_process is None and not self._termination_finalized:
                    self._termination_process = _terminate_process_nonblocking(process)
                return False
            if start_gate_path is not None:
                with start_gate_path.open("x", encoding="ascii") as stream:
                    stream.write(start_gate_token)
                    stream.flush()
                    os.fsync(stream.fileno())
                    status = os.fstat(stream.fileno())
                    self._start_gate_identity = (
                        status.st_dev,
                        status.st_ino,
                    )
                    if (
                        path_identity(start_gate_path)
                        != self._start_gate_identity
                    ):
                        raise ProductRunnerError(
                            "提取前复制启动门身份在创建时发生变化"
                        )
            return True

    def __call__(self, *, selected_source_paths, output_parent, settings_snapshot):
        with self._state_lock:
            if self._active:
                raise ProductRunnerError("提取前复制子进程仍在运行")
            if self._cleanup_blocked_reason is not None:
                raise ProductRunnerError(f"提取前复制清理状态不可确认：{self._cleanup_blocked_reason}")
            self._active = True
            self._start_gate_identity = None
        ownership = None
        try:
            run_id = uuid.uuid4().hex
            marker_id = uuid.uuid4().hex
            timestamp = datetime.now(timezone.utc).isoformat()
            ownership = create_run_ownership(
                self.local_appdata,
                run_id,
                marker_id,
                list(self.protected_paths),
            )
            with self._state_lock:
                self._cleanup_temp_root = ownership.temp_root
                self._cleanup_temp_root_identity = ownership.temp_root_identity
            manifest_path = ownership.temp_root / "pre_extraction_context.json"
            result_path = ownership.temp_root / "pre_extraction_result.json"
            result_pending_path = result_path.with_name(f"{result_path.name}.pending")
            start_gate_path = (
                ownership.temp_root / "pre_extraction.start.gate"
                if self._require_process_job
                else None
            )
            start_gate_token = uuid.uuid4().hex if start_gate_path is not None else None
            ownership = add_allowed_child(ownership, ownership.temp_root / ACTIVE_LEASE_FILE)
            ownership = add_allowed_child(ownership, manifest_path)
            ownership = add_allowed_child(ownership, result_path)
            ownership = add_allowed_child(ownership, result_pending_path)
            if start_gate_path is not None:
                ownership = add_allowed_child(ownership, start_gate_path)
            try:
                manifest_evidence = _write_json_exclusive_evidence(
                    manifest_path,
                    {
                        "run_id": run_id,
                        "marker_id": marker_id,
                        "timestamp": timestamp,
                        "temp_root": str(ownership.temp_root),
                        "selected_source_paths": [str(path) for path in selected_source_paths],
                        "output_parent": str(output_parent),
                        "settings_snapshot": dict(settings_snapshot),
                        "protected_paths": [str(path) for path in self.protected_paths],
                    },
                )
                manifest_identity = manifest_evidence.identity
                ownership = bind_allowed_child_identity(
                    ownership,
                    manifest_path,
                    expected_identity=manifest_identity,
                )
            except FileExistsError as exc:
                raise ProductRunnerError(
                    f"提取前复制 manifest 已存在：{manifest_path}"
                ) from exc
            except OSError as exc:
                raise ProductRunnerError(
                    f"无法创建提取前复制 manifest：{exc}"
                ) from exc
            process = self.process_factory(
                _pre_extraction_process_command(
                    manifest_path,
                    result_path,
                    manifest_evidence.sha256,
                ),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=_subprocess_environment(
                    start_gate_path=start_gate_path,
                    start_gate_token=start_gate_token,
                ),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            with self._state_lock:
                self._current_process = process
                self._termination_finalized = False
                if self._cancelled.is_set() and self._termination_process is None:
                    self._termination_process = _terminate_process_nonblocking(process)
            try:
                bind_process_to_job(process, required=self._require_process_job)
                if not self._release_start_gate(process, start_gate_path, start_gate_token or "ready"):
                    raise ProductRunnerError("谱图数据提取已取消")
                if start_gate_path is not None:
                    ownership = bind_allowed_child_identity(
                        ownership,
                        start_gate_path,
                        expected_identity=self._start_gate_identity,
                    )
            except Exception:
                with self._state_lock:
                    if self._termination_process is None:
                        self._termination_process = _terminate_process_nonblocking(process)
                self._wait_for_termination_process(process)
                raise
            try:
                stdout, stderr = self._communicate(process)
            finally:
                self._wait_for_termination_process(process)
            if self._cancelled.is_set():
                raise ProductRunnerError("谱图数据提取已取消")
            if not str(stdout).strip() and not result_path.is_file():
                raise ProductRunnerError(
                    str(stderr).strip()
                    or f"提取前复制子进程退出代码 {process.returncode}"
                )
            (
                result_identity,
                result_sha256,
                retained_identities,
            ) = _process_result_evidence(
                stdout,
                temp_root=ownership.temp_root,
            )
            ownership = read_ownership(ownership.temp_root)
            try:
                for retained_path, retained_identity in retained_identities:
                    ownership = bind_allowed_child_identity(
                        ownership,
                        retained_path,
                        expected_identity=retained_identity,
                    )
                if result_identity is not None:
                    ownership = bind_allowed_child_identity(
                        ownership,
                        result_path,
                        expected_identity=result_identity,
                    )
            except OwnershipError as exc:
                raise ProductRunnerError(str(exc)) from exc
            if result_identity is None or not result_path.is_file():
                raise ProductRunnerError(
                    str(stderr).strip()
                    or f"提取前复制子进程退出代码 {process.returncode}"
                )
            payload = _read_authenticated_process_payload(
                result_path,
                "提取前复制子进程",
                expected_identity=result_identity,
                expected_sha256=result_sha256,
            )
            if process.returncode != 0 or not payload.get("ok"):
                message = str(payload.get("error") or str(stderr).strip() or "提取前复制子进程失败")
                raise ProductRunnerError(message)
            context = _context_from_payload(payload["context"])
            self._validate_context(context, ownership, selected_source_paths, output_parent, settings_snapshot)
            expected_sensitive_identities = {
                manifest_path: manifest_identity,
            }
            try:
                for sensitive, expected_identity in (
                    expected_sensitive_identities.items()
                ):
                    ownership = bind_allowed_child_identity(
                        ownership,
                        sensitive,
                        expected_identity=expected_identity,
                    )
            except OwnershipError as exc:
                raise ProductRunnerError(str(exc)) from exc
            if lexical_path_exists(result_pending_path):
                raise ProductRunnerError(
                    f"提取前复制结果临时文件仍然存在：{result_pending_path}"
                )
            with self._state_lock:
                self._cleanup_temp_root = None
                self._cleanup_temp_root_identity = None
            return context
        except KeyboardInterrupt as exc:
            try:
                self.cancel()
                with self._state_lock:
                    process = self._current_process
                    termination_finalized = self._termination_finalized
                if process is not None and not termination_finalized:
                    self._wait_for_termination_process(process)
                cleanup_error = _cleanup_temp_root_error(
                    ownership.temp_root if ownership is not None else None,
                    expected_root_identity=(
                        ownership.temp_root_identity
                        if ownership is not None
                        else None
                    ),
                )
                if cleanup_error is not None:
                    raise ExtractionCleanupBlockedError(
                        f"临时文件清理失败：{cleanup_error}"
                    )
                with self._state_lock:
                    self._cleanup_temp_root = None
                    self._cleanup_temp_root_identity = None
            except BaseException as cleanup_exc:
                with self._state_lock:
                    self._cleanup_blocked_reason = str(cleanup_exc)
                exc.add_note(f"提取前复制取消清理失败：{cleanup_exc}")
            raise
        except ExtractionCleanupBlockedError as exc:
            self._cleanup_blocked_reason = str(exc)
            raise
        except Exception as exc:
            if ownership is not None and lexical_path_exists(
                ownership.temp_root
            ):
                cleanup_error = _cleanup_temp_root_error(
                    ownership.temp_root,
                    expected_root_identity=ownership.temp_root_identity,
                )
                if cleanup_error is not None:
                    blocked = ExtractionCleanupBlockedError(
                        f"{exc}; 临时文件清理失败：{cleanup_error}"
                    )
                    self._cleanup_blocked_reason = str(blocked)
                    raise blocked from exc
                with self._state_lock:
                    self._cleanup_temp_root = None
                    self._cleanup_temp_root_identity = None
            raise
        finally:
            with self._state_lock:
                if self._cleanup_blocked_reason is None:
                    self._current_process = None
                self._active = False

    def _communicate(self, process):
        cancellation_deadline = None
        while True:
            try:
                return process.communicate(timeout=self.cancellation_poll_interval)
            except subprocess.TimeoutExpired:
                if not self._cancelled.is_set():
                    continue
                if cancellation_deadline is None:
                    cancellation_deadline = time.monotonic() + self.cancellation_timeout
                if process.poll() is not None:
                    continue
                if time.monotonic() >= cancellation_deadline:
                    raise ExtractionCleanupBlockedError("提取前复制子进程终止超时")

    def _wait_for_termination_process(self, process=None) -> None:
        while True:
            self._drain_termination_process(process)
            if not _wait_for_process_exit(process, self.cancellation_timeout):
                raise ExtractionCleanupBlockedError("提取前复制子进程仍在运行，清理状态无法确认")
            with self._state_lock:
                if self._termination_process is None:
                    if process is not None and process.poll() is None:
                        raise ExtractionCleanupBlockedError("提取前复制子进程仍在运行，清理状态无法确认")
                    self._termination_finalized = True
                    break
        if process is not None:
            try:
                close_bound_process_job(process)
            except Exception as exc:
                raise ExtractionCleanupBlockedError(
                    f"提取前复制 Windows Job 关闭失败，清理状态无法确认：{exc}"
                ) from exc

    def _drain_termination_process(self, process=None) -> None:
        while True:
            with self._state_lock:
                helper = self._termination_process
            if helper is None:
                return
            helper.join(self.cancellation_timeout)
            if helper.is_alive():
                raise ExtractionCleanupBlockedError("提取前复制子进程终止线程仍在运行")
            error = getattr(helper, "_spectrum_organizer_termination_state", {}).get("error")
            with self._state_lock:
                if self._termination_process is helper:
                    self._termination_process = None
            if error and (process is None or process.poll() is None):
                raise ExtractionCleanupBlockedError(f"提取前复制子进程终止失败：{error}")

    def _validate_context(self, context, ownership, selected_source_paths, output_parent, settings_snapshot) -> None:
        expected_sources = tuple(Path(path) for path in selected_source_paths)
        if (
            context.run_id != ownership.run_id
            or context.temp_root.resolve() != ownership.temp_root.resolve()
            or context.temp_root_identity != ownership.temp_root_identity
        ):
            raise ProductRunnerError("提取前复制子进程返回了错误的任务所有权")
        if context.selected_source_paths != expected_sources:
            raise ProductRunnerError("提取前复制子进程返回了错误的原始文件列表")
        if context.output_parent != Path(output_parent) or context.settings_snapshot != dict(settings_snapshot):
            raise ProductRunnerError("提取前复制子进程返回了错误的已确认设置")
        if tuple(snapshot.path for snapshot in context.source_fingerprints_before) != expected_sources:
            raise ProductRunnerError("提取前复制子进程返回了错误的原始文件 fingerprint")
        if tuple(snapshot.path for snapshot in context.protected_fingerprints_before) != self.protected_paths:
            raise ProductRunnerError("提取前复制子进程返回了错误的受保护文件 fingerprint")
        verified_ownership = read_ownership(ownership.temp_root)
        if (
            verified_ownership.run_id != ownership.run_id
            or verified_ownership.marker_id != ownership.marker_id
            or tuple(verified_ownership.protected_paths) != self.protected_paths
        ):
            raise ProductRunnerError("提取前复制子进程修改了任务所有权身份")
        verify_sources_unchanged(
            [
                *context.source_fingerprints_before,
                *context.protected_fingerprints_before,
            ],
            cancel_check=self._raise_if_cancelled,
        )
        if len(context.run_owned_source_copy_paths) != len(context.source_fingerprints_before):
            raise ProductRunnerError("提取前复制子进程返回的副本数量不一致")
        allowed = {Path(path).resolve() for path in verified_ownership.allowed_children}
        resolved_copies = tuple(Path(path).resolve() for path in context.run_owned_source_copy_paths)
        if len(set(resolved_copies)) != len(resolved_copies):
            raise ProductRunnerError("提取前复制子进程返回了重复的源文件副本")
        if len({path.parent for path in resolved_copies}) != len(resolved_copies):
            raise ProductRunnerError("提取前复制子进程未隔离每个源文件副本目录")
        for index, (copy_path, snapshot) in enumerate(
            zip(resolved_copies, context.source_fingerprints_before),
            start=1,
        ):
            resolved_copy = Path(copy_path).resolve()
            expected_dir = context.temp_root.resolve() / f"source-{index:04d}-{snapshot.sha256[:12]}"
            if resolved_copy.parent != expected_dir or resolved_copy.name != snapshot.path.name:
                raise ProductRunnerError("提取前复制子进程返回的源文件副本布局无效")
            if not any(resolved_copy == path or path in resolved_copy.parents for path in allowed):
                raise ProductRunnerError("提取前复制子进程返回了未登记的副本路径")
            self._raise_if_cancelled()
            if (
                resolved_copy.stat().st_size != snapshot.size_bytes
                or hash_file(resolved_copy, cancel_check=self._raise_if_cancelled) != snapshot.sha256
            ):
                raise ProductRunnerError("提取前复制子进程返回的副本校验失败")

    def _raise_if_cancelled(self) -> None:
        if self._cancelled.is_set():
            raise ProductRunnerError("谱图数据提取已取消")


def run_approved_extraction_phase(
    context: ApprovedPreExtractionRunContext,
    *,
    snapshot_path: Path | None = None,
    worker_factory_builder=None,
) -> ExtractionPhaseSummary:
    from spectrum_organizer.domain.extracted import ExtractionSource
    from spectrum_organizer.origin.extract_worker import ExtractionOrchestrator, build_origin_extraction_worker_factory
    from spectrum_organizer.store.run_snapshot import RunSnapshot

    production_worker_factory = worker_factory_builder is None
    if production_worker_factory:
        worker_factory_builder = build_origin_extraction_worker_factory
    sources = _build_extraction_sources(context, ExtractionSource)
    snapshot = RunSnapshot(_register_snapshot_path(context, snapshot_path))
    worker_factory = worker_factory_builder(settings_snapshot=context.settings_snapshot)
    source_manager = ExtractionSourceManager(
        sources,
        tuple(context.source_fingerprints_before),
        temp_root=context.temp_root,
    )
    ExtractionOrchestrator(
        snapshot,
        worker_factory,
        source_manager,
        worker_shutdown_waiter=(
            _production_worker_shutdown_waiter if production_worker_factory else None
        ),
        s1_limit=_confirmed_s1_limit(context.settings_snapshot),
        steady_emission_y=_confirmed_steady_emission_y(context.settings_snapshot),
        allow_missing_s1=_confirmed_allow_missing_s1(context.settings_snapshot),
    ).run(sources)
    verify_sources_unchanged([
        *context.source_fingerprints_before,
        *context.protected_fingerprints_before,
    ])
    return _summarize_extraction(snapshot, sources)


def run_approved_source_extraction_phase(
    context: ApprovedPreExtractionRunContext,
    *,
    source_id: str,
    snapshot_path: Path,
    worker_factory_builder=None,
) -> ExtractionPhaseSummary:
    from spectrum_organizer.domain.extracted import ExtractionSource
    from spectrum_organizer.origin.extract_worker import ExtractionOrchestrator, build_origin_extraction_worker_factory
    from spectrum_organizer.store.run_snapshot import RunSnapshot

    production_worker_factory = worker_factory_builder is None
    if production_worker_factory:
        worker_factory_builder = build_origin_extraction_worker_factory
    sources = _build_extraction_sources(context, ExtractionSource)
    selected = tuple(source for source in sources if source.source_id == source_id)
    if len(selected) != 1:
        raise ProductRunnerError(f"Invalid extraction source id: {source_id}")
    snapshot = RunSnapshot(
        _register_snapshot_path(context, snapshot_path, allow_existing=True)
    )
    worker_factory = worker_factory_builder(settings_snapshot=context.settings_snapshot)
    source_manager = ExtractionSourceManager(
        sources,
        tuple(context.source_fingerprints_before),
        temp_root=context.temp_root,
    )
    ExtractionOrchestrator(
        snapshot,
        worker_factory,
        source_manager,
        max_attempts=1,
        worker_shutdown_waiter=(
            _production_worker_shutdown_waiter if production_worker_factory else None
        ),
        s1_limit=_confirmed_s1_limit(context.settings_snapshot),
        steady_emission_y=_confirmed_steady_emission_y(context.settings_snapshot),
        allow_missing_s1=_confirmed_allow_missing_s1(context.settings_snapshot),
    ).run(selected)
    selected_index = next(index for index, source in enumerate(sources) if source.source_id == source_id)
    verify_sources_unchanged([
        context.source_fingerprints_before[selected_index],
        *context.protected_fingerprints_before,
    ])
    return _summarize_extraction(snapshot, selected)


def run_reader_source_extraction_phase(
    command: ReaderProcessCommand,
    *,
    worker_factory_builder=None,
    free_bytes_provider=None,
    cleanup_identity_callback=None,
    sidecar_auth_key: str | None = None,
) -> ReaderSourceExtractionSummary:
    from spectrum_organizer.origin.reader_service import (
        run_reader_source_extraction_phase as run_reader_service,
    )

    return run_reader_service(
        command,
        worker_factory_builder=worker_factory_builder,
        free_bytes_provider=free_bytes_provider,
        cleanup_identity_callback=cleanup_identity_callback,
        sidecar_auth_key=sidecar_auth_key,
    )


def _production_worker_shutdown_waiter(source_id: str, attempt: int) -> None:
    del source_id, attempt
    ExtractionSubprocessRunner()._wait_for_origin_shutdown()


def run_approved_extraction_phase_in_subprocess(
    context: ApprovedPreExtractionRunContext,
    *,
    process_factory=None,
) -> ExtractionPhaseSummary:
    return ExtractionSubprocessRunner(process_factory=process_factory)(context)


class ExtractionSubprocessRunner:
    def __init__(
        self,
        *,
        process_factory=None,
        origin_process_probe=None,
        origin_process_controller=None,
        cancellation_timeout: float = 12.0,
        cancellation_poll_interval: float = 0.1,
        origin_identity_timeout: float = 60.0,
        origin_shutdown_timeout: float = 12.0,
        origin_shutdown_poll_interval: float = 0.1,
    ):
        for name, value in (
            ("cancellation_timeout", cancellation_timeout),
            ("cancellation_poll_interval", cancellation_poll_interval),
            ("origin_identity_timeout", origin_identity_timeout),
            ("origin_shutdown_timeout", origin_shutdown_timeout),
            ("origin_shutdown_poll_interval", origin_shutdown_poll_interval),
        ):
            if not is_finite_real_number(value) or value <= 0:
                raise ValueError(f"{name} must be a finite value greater than zero")
        self.process_factory = process_factory or subprocess.Popen
        self._require_process_job = process_factory is None and sys.platform == "win32"
        self.origin_process_probe = origin_process_probe or default_origin_process_probe
        self.origin_process_controller = origin_process_controller or WindowsOriginProcessController(
            process_probe=self.origin_process_probe
        )
        self.cancellation_timeout = cancellation_timeout
        self.cancellation_poll_interval = cancellation_poll_interval
        self.origin_identity_timeout = origin_identity_timeout
        self.origin_shutdown_timeout = origin_shutdown_timeout
        self.origin_shutdown_poll_interval = origin_shutdown_poll_interval
        self._cancelled = threading.Event()
        self._state_lock = threading.Lock()
        self._current_process = None
        self._termination_process = None
        self._termination_finalized = False
        self._cleanup_blocked_reason = None
        self._active = False
        self._progress_callback = None
        self._active_origin_launch_path = None
        self._active_origin_identity_path = None
        self._active_origin_binding = None
        self._active_origin_auth_key = None
        self._last_origin_auth_key = None
        self._origin_start_gate_released = False
        self._reader_only_termination_requested = False
        self._reader_start_gate_identity = None

    def set_progress_callback(self, callback) -> None:
        self._progress_callback = callback

    def _emit_progress(self, kind: str, **payload) -> None:
        if self._progress_callback is not None:
            self._progress_callback({"kind": kind, **payload})

    def reset(self) -> None:
        with self._state_lock:
            if self._active:
                raise ProductRunnerError("谱图数据提取子进程仍在运行")
            if self._cleanup_blocked_reason is not None:
                raise ProductRunnerError(f"谱图数据提取清理状态不可确认：{self._cleanup_blocked_reason}")
            if self._termination_process is not None:
                is_alive = getattr(self._termination_process, "is_alive", None)
                if callable(is_alive) and is_alive():
                    raise ProductRunnerError("谱图数据提取进程树终止线程仍在运行")
                self._termination_process = None
            self._termination_finalized = False
            self._cancelled.clear()
            self._reader_only_termination_requested = False
            self._reader_start_gate_identity = None

    def cancel(self) -> None:
        with self._state_lock:
            process = self._current_process
            if process is not None:
                self._request_termination_locked(process)
            self._cancelled.set()

    def retry_cancel_cleanup(self) -> None:
        with self._state_lock:
            process = self._current_process
            if self._cleanup_blocked_reason is None:
                return
            if process is None:
                self._cleanup_blocked_reason = None
                return
            termination_process = self._termination_process
            is_alive = getattr(termination_process, "is_alive", None)
            if termination_process is not None and callable(is_alive) and is_alive():
                raise ExtractionCleanupBlockedError("谱图数据提取进程树终止线程仍在运行")
            self._termination_process = None
            self._termination_finalized = False
            self._cleanup_blocked_reason = None
            self._request_termination_locked(process)
            self._cancelled.set()
        self._wait_for_termination_process(process)
        with self._state_lock:
            if self._cleanup_blocked_reason is None and self._current_process is process:
                self._current_process = None

    def _release_start_gate(
        self,
        process,
        start_gate_path: Path | None,
        start_gate_token: str = "ready",
    ) -> bool:
        with self._state_lock:
            if self._cancelled.is_set():
                self._request_termination_locked(process)
                return False
            if start_gate_path is not None:
                self._origin_start_gate_released = True
                try:
                    with start_gate_path.open("x", encoding="ascii") as stream:
                        stream.write(start_gate_token)
                        stream.flush()
                        os.fsync(stream.fileno())
                        status = os.fstat(stream.fileno())
                        self._reader_start_gate_identity = (
                            status.st_dev,
                            status.st_ino,
                        )
                        if (
                            path_identity(start_gate_path)
                            != self._reader_start_gate_identity
                        ):
                            raise ProductRunnerError(
                                "谱图提取启动门身份在创建时发生变化"
                            )
                except Exception:
                    self._origin_start_gate_released = False
                    self._reader_start_gate_identity = None
                    raise
            return True

    def _request_termination_locked(self, process) -> None:
        if self._termination_process is not None or self._termination_finalized:
            return
        if self._require_process_job and not self._origin_start_gate_released:
            self._reader_only_termination_requested = True
            termination_process = _terminate_process_nonblocking(process)
        else:
            termination_process = _terminate_extraction_process_nonblocking(
                process,
                identity_path=(self._active_origin_identity_path if self._require_process_job else None),
                launch_path=(self._active_origin_launch_path if self._require_process_job else None),
                expected_binding=(self._active_origin_binding if self._require_process_job else None),
                sidecar_auth_key=(self._active_origin_auth_key if self._require_process_job else None),
                identity_wait_timeout=self.origin_identity_timeout,
            )
        if termination_process is not None:
            self._termination_process = termination_process

    def __call__(self, context: ApprovedPreExtractionRunContext) -> ExtractionPhaseSummary:
        with self._state_lock:
            if self._active:
                raise ProductRunnerError("谱图数据提取子进程仍在运行")
            if self._cleanup_blocked_reason is not None:
                raise ProductRunnerError(f"谱图数据提取清理状态不可确认：{self._cleanup_blocked_reason}")
            if self._termination_process is not None:
                is_alive = getattr(self._termination_process, "is_alive", None)
                if callable(is_alive) and is_alive():
                    raise ProductRunnerError("谱图数据提取进程树终止线程仍在运行")
                self._termination_process = None
            self._active = True
        try:
            return self._run(context)
        except KeyboardInterrupt as exc:
            try:
                self.cancel()
                with self._state_lock:
                    process = self._current_process
                    termination_finalized = self._termination_finalized
                if process is not None and not termination_finalized:
                    self._wait_for_termination_process(process)
                cleanup_error = _cleanup_temp_root_error(
                    context.temp_root,
                    expected_root_identity=context.temp_root_identity,
                )
                if cleanup_error is not None:
                    raise ExtractionCleanupBlockedError(
                        f"临时文件清理失败：{cleanup_error}"
                    )
            except BaseException as cleanup_exc:
                with self._state_lock:
                    self._cleanup_blocked_reason = str(cleanup_exc)
                exc.add_note(f"谱图数据提取取消清理失败：{cleanup_exc}")
            raise
        except Exception as exc:
            if isinstance(exc, ExtractionCleanupBlockedError):
                with self._state_lock:
                    self._cleanup_blocked_reason = str(exc)
                raise
            cleanup_error = _cleanup_temp_root_error(
                context.temp_root,
                expected_root_identity=context.temp_root_identity,
            )
            if cleanup_error is not None:
                blocked = ExtractionCleanupBlockedError(
                    f"{exc}; 临时文件清理失败：{cleanup_error}"
                )
                with self._state_lock:
                    self._cleanup_blocked_reason = str(blocked)
                raise blocked from exc
            raise
        finally:
            with self._state_lock:
                if self._cleanup_blocked_reason is None:
                    self._current_process = None
                self._active = False

    def _run(self, context: ApprovedPreExtractionRunContext) -> ExtractionPhaseSummary:
        from spectrum_organizer.domain.extracted import ExtractionSource
        from spectrum_organizer.store.run_snapshot import RunSnapshot

        temp_root = Path(context.temp_root).resolve()
        _prepare_reader_temp_root(context)
        snapshot_path = _register_snapshot_path(context, temp_root / "run_snapshot.sqlite3")
        active_context = context
        sources = list(_build_extraction_sources(active_context, ExtractionSource))
        source_manager = ExtractionSourceManager(
            tuple(sources),
            tuple(context.source_fingerprints_before),
            temp_root=context.temp_root,
            cancel_check=self._raise_if_cancelled,
        )
        total_inventory_count = 0
        total_extracted_count = 0
        total_rejected_count = 0
        accepted_partition_digests: dict[str, str] = {}
        accepted_sources: list[object] = []
        source_input_issues: list[SourceInputIssue] = []
        worker_open_targets: list[str] = []
        source_total = len(sources)
        for source_index, initial_source in enumerate(tuple(sources), start=1):
            source = initial_source
            unsupported_error = None
            self._raise_if_cancelled()
            self._emit_progress(
                "source_started",
                source_id=source.source_id,
                source_path=str(source.original_path),
                source_index=source_index,
                source_total=source_total,
                completed_sources=source_index - 1,
                total_inventory_count=total_inventory_count,
                total_extracted_count=total_extracted_count,
                total_rejected_count=total_rejected_count,
            )
            reader_failures: list[str] = []
            reader_failure_notes: list[str] = []
            for reader_attempt in (1, 2):
                infrastructure_error = None
                cleanup_blocked = False
                reader_succeeded = False
                try:
                    _record_extraction_target_attempt(
                        active_context,
                        source,
                        reader_attempt,
                    )
                    payload = self._run_reader_process_attempt(
                        active_context,
                        source,
                        snapshot_path,
                        reader_attempt,
                    )
                    reader_succeeded = True
                except _ReaderProcessInfrastructureError as exc:
                    infrastructure_error = exc
                except ExtractionCleanupBlockedError:
                    cleanup_blocked = True
                    raise
                except UnsupportedSourceInputError as exc:
                    unsupported_error = exc
                except ProductRunnerError as exc:
                    if reader_failure_notes:
                        exc.__notes__ = [
                            *reader_failure_notes,
                            *getattr(exc, "__notes__", ()),
                        ]
                    raise
                finally:
                    if not cleanup_blocked:
                        source_manager.verify_after_worker(source.source_id)
                        observed_target = _read_observed_origin_open_target(
                            active_context,
                            source,
                            reader_attempt,
                            auth_key=self._last_origin_auth_key,
                        )
                        if observed_target is not None:
                            worker_open_targets.append(observed_target)
                        elif reader_succeeded or unsupported_error is not None:
                            raise ProductRunnerError(
                                "Origin 未确认打开输入文件 "
                                f"{Path(source.original_path).name} 的临时副本；"
                                "为保护原始数据，本次任务已停止。"
                                "建议关闭残留 Origin 进程后重试。"
                            )
                if infrastructure_error is not None:
                    reader_failures.append(f"attempt {reader_attempt}: {infrastructure_error}")
                    reader_failure_notes.extend(
                        f"attempt {reader_attempt}: {note}"
                        for note in getattr(infrastructure_error, "__notes__", ())
                    )
                    if reader_attempt >= 2:
                        error = ProductRunnerError("; ".join(reader_failures))
                        for note in reader_failure_notes:
                            error.add_note(note)
                        raise error from infrastructure_error
                    if snapshot_path.exists():
                        RunSnapshot(snapshot_path).discard_source_partition(source.source_id)
                    failed_copy = Path(source.copy_path).resolve()
                    source_manager.discard_failed_copy(source.source_id)
                    retry_copy = Path(source_manager.refresh_copy(source.source_id))
                    _record_extraction_retry_cleanup(
                        active_context,
                        source_id=source.source_id,
                        reader_attempt=reader_attempt,
                        failed_copy=failed_copy,
                        replacement_copy=retry_copy,
                    )
                    copy_paths = list(active_context.run_owned_source_copy_paths)
                    copy_paths[source_index - 1] = retry_copy
                    active_context = replace(
                        active_context,
                        run_owned_source_copy_paths=tuple(copy_paths),
                    )
                    ownership = read_ownership(temp_root)
                    source = replace(
                        source,
                        copy_path=retry_copy,
                        allowed_children=ownership.allowed_children,
                    )
                    sources[source_index - 1] = source
                    continue
                if unsupported_error is not None:
                    RunSnapshot(snapshot_path).discard_source(source.source_id)
                    issue = SourceInputIssue(
                        source_id=source.source_id,
                        original_path=str(source.original_path),
                        reason="未检测到受支持的 Origin 原始谱图。",
                        recommendation="请重新选择包含原始光谱 Book 的 Origin 项目文件。",
                    )
                    source_input_issues.append(issue)
                    self._emit_progress(
                        "source_skipped",
                        source_id=source.source_id,
                        source_path=str(source.original_path),
                        source_index=source_index,
                        source_total=source_total,
                        completed_sources=source_index,
                        reason=issue.reason,
                        recommendation=issue.recommendation,
                        total_inventory_count=total_inventory_count,
                        total_extracted_count=total_extracted_count,
                        total_rejected_count=total_rejected_count,
                    )
                break
            if unsupported_error is not None:
                continue
            source_summary = _validate_child_summary(
                payload.get("summary"),
                context=active_context,
                expected_snapshot_path=snapshot_path,
                expected_source=source,
                cancel_check=self._raise_if_cancelled,
                validate_snapshot_registration=False,
            )
            total_inventory_count += source_summary.inventory_count
            total_extracted_count += source_summary.extracted_count
            total_rejected_count += source_summary.rejected_count
            accepted_partition_digests[source.source_id] = _snapshot_partition_digest(
                snapshot_path,
                source.source_id,
                cancel_check=self._raise_if_cancelled,
            )
            accepted_sources.append(source)
            self._emit_progress(
                "source_completed",
                source_id=source.source_id,
                source_path=str(source.original_path),
                source_index=source_index,
                source_total=source_total,
                completed_sources=source_index,
                inventory_count=source_summary.inventory_count,
                extracted_count=source_summary.extracted_count,
                rejected_count=source_summary.rejected_count,
                total_inventory_count=total_inventory_count,
                total_extracted_count=total_extracted_count,
                total_rejected_count=total_rejected_count,
            )
        self._raise_if_cancelled()
        if not accepted_sources:
            raise AllSelectedSourcesInvalidError(tuple(source_input_issues))
        for source_id, expected_digest in accepted_partition_digests.items():
            self._raise_if_cancelled()
            if _snapshot_partition_digest(
                snapshot_path,
                source_id,
                cancel_check=self._raise_if_cancelled,
            ) != expected_digest:
                raise ProductRunnerError(
                    f"谱图提取 snapshot 已接受分区在 reader 阶段发生变化：{source_id}"
                )
        _validate_snapshot_source_ids(
            snapshot_path,
            tuple(source.source_id for source in accepted_sources),
            cancel_check=self._raise_if_cancelled,
        )
        verify_sources_unchanged(
            [
                *context.source_fingerprints_before,
                *context.protected_fingerprints_before,
            ],
            cancel_check=self._raise_if_cancelled,
        )
        trusted_snapshot = RunSnapshot(snapshot_path)
        for source in accepted_sources:
            source_index = int(source.source_id[1:]) - 1
            fingerprint = context.source_fingerprints_before[source_index]
            trusted_snapshot.bind_original_provenance(
                source.source_id,
                source.copy_path,
                fingerprint.sha256,
                original_path=Path(
                    _canonical_source_snapshot_path(fingerprint)
                ),
                original_size_bytes=fingerprint.size_bytes,
                original_mtime_ns=fingerprint.mtime_ns,
            )
        accepted_partition_digests = {
            source.source_id: _snapshot_partition_digest(
                snapshot_path,
                source.source_id,
                cancel_check=self._raise_if_cancelled,
            )
            for source in accepted_sources
        }
        self._raise_if_cancelled()
        snapshot = _ReadOnlySnapshotView(snapshot_path)
        _validate_snapshot_registration(active_context, snapshot_path)
        _validate_reconciled_snapshot_partitions(
            snapshot_path,
            tuple(source.source_id for source in accepted_sources),
            cancel_check=self._raise_if_cancelled,
            s1_limit=_confirmed_s1_limit(active_context.settings_snapshot),
            steady_emission_y=_confirmed_steady_emission_y(active_context.settings_snapshot),
            allow_missing_s1=_confirmed_allow_missing_s1(active_context.settings_snapshot),
        )
        _validate_snapshot_source_records(
            snapshot_path,
            active_context,
            tuple(accepted_sources),
            cancel_check=self._raise_if_cancelled,
        )
        for source_id, expected_digest in accepted_partition_digests.items():
            self._raise_if_cancelled()
            if _snapshot_partition_digest(
                snapshot_path,
                source_id,
                cancel_check=self._raise_if_cancelled,
            ) != expected_digest:
                raise ProductRunnerError(f"谱图提取 snapshot 已接受分区在最终复核时发生变化：{source_id}")
        summary = _summarize_extraction(
            snapshot,
            tuple(accepted_sources),
            cancel_check=self._raise_if_cancelled,
        )
        summary = replace(
            summary,
            worker_open_targets=tuple(worker_open_targets),
            source_input_issues=tuple(source_input_issues),
        )
        _validate_summary_closure(summary)
        try:
            from spectrum_organizer.store.run_snapshot import snapshot_approval_sha256

            summary = replace(
                summary,
                snapshot_sha256=snapshot_approval_sha256(
                    snapshot_path,
                    cancel_check=self._raise_if_cancelled,
                ),
            )
        except Exception as exc:
            self._raise_if_cancelled()
            raise ProductRunnerError(f"无法批准本次谱图提取 snapshot：{exc}") from exc
        self._emit_progress(
            "batch_completed",
            source_total=source_total,
            completed_sources=source_total,
            total_inventory_count=summary.total_inventory_count,
            total_extracted_count=summary.total_extracted_count,
            total_rejected_count=summary.total_rejected_count,
        )
        return summary

    def _run_reader_process_attempt(self, context, source, snapshot_path: Path, reader_attempt: int):
        temp_root = Path(context.temp_root).resolve()
        suffix = f"{source.source_id}.attempt{reader_attempt}"
        manifest_path = temp_root / f"extraction_context.{suffix}.json"
        result_path = temp_root / f"extraction_result.{suffix}.json"
        result_pending_path = result_path.with_name(f"{result_path.name}.pending")
        origin_launch_path = temp_root / f"origin_launch.{suffix}.json"
        origin_identity_path = temp_root / f"origin_identity.{suffix}.json"
        origin_open_target_path = temp_root / f"origin_open_target.{suffix}.json"
        origin_pid_helper_path = origin_identity_path.with_suffix(".c")
        origin_launch_pending_path = origin_launch_path.with_name(f"{origin_launch_path.name}.pending")
        origin_identity_pending_path = origin_identity_path.with_name(
            f"{origin_identity_path.name}.pending"
        )
        origin_open_target_pending_path = origin_open_target_path.with_name(
            f"{origin_open_target_path.name}.pending"
        )
        ipc_paths = (
            result_path,
            result_pending_path,
            origin_launch_path,
            origin_identity_path,
            origin_open_target_path,
            origin_launch_pending_path,
            origin_identity_pending_path,
            origin_open_target_pending_path,
        )
        for path in ipc_paths:
            if lexical_path_exists(path):
                raise ProductRunnerError(f"谱图提取 reader IPC 文件已存在：{path}")
        ownership = read_ownership(temp_root)
        for path in (
            temp_root / ACTIVE_LEASE_FILE,
            manifest_path,
            *ipc_paths,
            origin_pid_helper_path,
        ):
            if path not in ownership.allowed_children:
                ownership = add_allowed_child(ownership, path)
        with self._state_lock:
            self._active_origin_launch_path = origin_launch_path
            self._active_origin_identity_path = origin_identity_path
            self._active_origin_binding = (
                context.run_id,
                ownership.marker_id,
                source.source_id,
                reader_attempt,
            )
            self._origin_start_gate_released = False
        manifest = _reader_command_to_payload(
            _build_reader_process_command(
                context,
                source,
                snapshot_path,
                reader_attempt=reader_attempt,
                cancel_check=self._raise_if_cancelled,
            )
        )
        try:
            manifest_evidence = _write_json_exclusive_evidence(
                manifest_path,
                manifest,
            )
            manifest_identity = manifest_evidence.identity
            ownership = bind_allowed_child_identity(
                ownership,
                manifest_path,
                expected_identity=manifest_identity,
            )
        except FileExistsError as exc:
            raise ProductRunnerError(f"谱图提取 reader manifest 已存在：{manifest_path}") from exc
        except OSError as exc:
            raise ProductRunnerError(f"无法创建谱图提取 reader manifest：{exc}") from exc
        manifest_sha256 = manifest_evidence.sha256
        sidecar_auth_key = (
            secrets.token_hex(32)
            if self._require_process_job
            else None
        )
        command = _extraction_process_command(
            manifest_path,
            result_path,
            manifest_sha256,
        )
        start_gate_path = (
            temp_root / f"extraction_start.{suffix}.gate"
            if self._require_process_job
            else None
        )
        start_gate_token = uuid.uuid4().hex if start_gate_path is not None else None
        if start_gate_path is not None and start_gate_path not in ownership.allowed_children:
            ownership = add_allowed_child(ownership, start_gate_path)
        try:
            process = self.process_factory(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=_subprocess_environment(
                    start_gate_path=start_gate_path,
                    start_gate_token=start_gate_token,
                    sidecar_auth_key=sidecar_auth_key,
                ),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            with self._state_lock:
                self._current_process = process
                self._active_origin_auth_key = sidecar_auth_key
                self._last_origin_auth_key = sidecar_auth_key
                self._termination_finalized = False
                if self._cancelled.is_set():
                    self._request_termination_locked(process)
            bind_process_to_job(process, required=self._require_process_job)
            if not self._release_start_gate(process, start_gate_path, start_gate_token or "ready"):
                raise ProductRunnerError("谱图数据提取已取消")
            if start_gate_path is not None:
                ownership = bind_allowed_child_identity(
                    ownership,
                    start_gate_path,
                    expected_identity=self._reader_start_gate_identity,
                )
        except Exception as exc:
            if "process" in locals():
                with self._state_lock:
                    self._request_termination_locked(process)
                self._wait_for_termination_process(process)
                with self._state_lock:
                    if self._current_process is process:
                        self._current_process = None
            raise _ReaderProcessInfrastructureError(f"reader process launch failed: {exc}") from exc
        termination_wait_attempted = False
        termination_wait_completed = False
        try:
            try:
                stdout, stderr = self._communicate(process)
            except KeyboardInterrupt as exc:
                with self._state_lock:
                    self._request_termination_locked(process)
                termination_wait_attempted = True
                try:
                    self._wait_for_termination_process(process)
                except BaseException as cleanup_exc:
                    exc.add_note(f"读取进程退出确认失败：{cleanup_exc}")
                else:
                    termination_wait_completed = True
                raise
            except Exception as exc:
                if self._cancelled.is_set():
                    raise
                raise _ReaderProcessInfrastructureError(
                    f"reader process communication failed: {exc}"
                ) from exc
        finally:
            try:
                if not termination_wait_attempted:
                    self._wait_for_termination_process(process)
                    termination_wait_completed = True
            finally:
                with self._state_lock:
                    if (
                        termination_wait_completed
                        and self._cleanup_blocked_reason is None
                        and self._current_process is process
                    ):
                        self._current_process = None
        self._raise_if_cancelled()
        if not str(stdout).strip() and not result_path.is_file():
            message = str(stderr).strip() or "reader process produced no result"
            raise _ReaderProcessInfrastructureError(message)
        (
            result_identity,
            result_sha256,
            created_temp_identities,
        ) = _reader_process_evidence(
            stdout,
            temp_root=temp_root,
        )
        ownership = read_ownership(temp_root)
        try:
            for created_path, created_identity in created_temp_identities:
                ownership = bind_allowed_child_identity(
                    ownership,
                    created_path,
                    expected_identity=created_identity,
                )
            if result_identity is not None:
                ownership = bind_allowed_child_identity(
                    ownership,
                    result_path,
                    expected_identity=result_identity,
                )
        except OwnershipError as exc:
            raise _ReaderProcessInfrastructureError(str(exc)) from exc
        if result_identity is None or not result_path.is_file():
            message = str(stderr).strip() or "reader process produced no result"
            raise _ReaderProcessInfrastructureError(message)
        try:
            payload = _read_authenticated_extraction_process_result(
                result_path,
                expected_identity=result_identity,
                expected_sha256=result_sha256,
            )
        except ProductRunnerError as exc:
            raise _ReaderProcessInfrastructureError(str(exc)) from exc
        if not payload.get("ok"):
            message = str(payload.get("error") or "谱图数据提取子进程失败")
            error_type = payload.get("error_type")
            if error_type == "UnsupportedSourceInputError":
                error_class = UnsupportedSourceInputError
            elif error_type in {
                    "InfrastructureExtractionError",
                    "WorkerShutdownUnconfirmedError",
            }:
                error_class = _ReaderProcessInfrastructureError
            else:
                error_class = ProductRunnerError
            error = error_class(message)
            for note in payload["error_notes"]:
                error.add_note(note)
            raise error
        if process.returncode != 0:
            message = str(stderr).strip() or f"reader process exited with code {process.returncode}"
            raise _ReaderProcessInfrastructureError(message)
        return payload

    def _raise_if_cancelled(self) -> None:
        if self._cancelled.is_set():
            raise ProductRunnerError("谱图数据提取已取消")

    def _communicate(self, process) -> tuple[str, str]:
        cancellation_deadline = None
        while True:
            try:
                return process.communicate(timeout=self.cancellation_poll_interval)
            except subprocess.TimeoutExpired:
                if not self._cancelled.is_set():
                    continue
                if cancellation_deadline is None:
                    wait_budget = self.cancellation_timeout
                    if self._require_process_job and self._origin_start_gate_released:
                        wait_budget += self.origin_identity_timeout
                    cancellation_deadline = time.monotonic() + wait_budget
                if time.monotonic() >= cancellation_deadline:
                    raise ProductRunnerError("谱图数据提取取消超时，子进程或输出管道未能在期限内结束")

    def _wait_for_termination_process(self, process=None) -> None:
        try:
            self._wait_for_termination_process_unchecked(process)
        except ExtractionCleanupBlockedError as exc:
            with self._state_lock:
                self._cleanup_blocked_reason = str(exc)
            raise

    def _wait_for_termination_process_unchecked(self, process=None) -> None:
        with self._state_lock:
            if process is None:
                process = self._current_process
        while True:
            self._drain_termination_process(process)
            if not _wait_for_process_exit(process, self.cancellation_timeout):
                raise ExtractionCleanupBlockedError("谱图数据提取取消超时或失败，子进程仍在运行")
            with self._state_lock:
                if self._termination_process is None:
                    if process is not None and process.poll() is None:
                        raise ExtractionCleanupBlockedError("谱图数据提取取消超时或失败，子进程仍在运行")
                    self._termination_finalized = True
                    break
        with self._state_lock:
            skip_origin_cleanup = self._reader_only_termination_requested
        if not skip_origin_cleanup:
            prelaunch_rejected = self._close_run_owned_origin()
            if not prelaunch_rejected:
                self._wait_for_origin_shutdown()
        if process is not None:
            try:
                close_bound_process_job(process)
            except Exception as exc:
                raise ExtractionCleanupBlockedError(
                    f"谱图数据提取 Windows Job 关闭失败，清理状态无法确认：{exc}"
                ) from exc
        with self._state_lock:
            self._active_origin_launch_path = None
            self._active_origin_identity_path = None
            self._active_origin_binding = None
            self._active_origin_auth_key = None
            self._origin_start_gate_released = False
            self._reader_only_termination_requested = False
            self._reader_start_gate_identity = None

    def _close_run_owned_origin(self) -> bool:
        with self._state_lock:
            launch_path = self._active_origin_launch_path
            identity_path = self._active_origin_identity_path
            binding = self._active_origin_binding
            auth_key = self._active_origin_auth_key
        if launch_path is None or not Path(launch_path).is_file():
            with self._state_lock:
                gate_released = self._origin_start_gate_released
            if gate_released:
                raise ExtractionCleanupBlockedError(
                    "取消任务时缺少本次任务 Origin 的启动基线，拒绝关闭 reader Windows Job"
                )
            return False
        try:
            if binding is None:
                raise ExtractionCleanupBlockedError("缺少本次 reader 的 Origin 身份绑定")
            baseline, launch_state = _read_origin_launch_baseline(
                Path(launch_path),
                binding,
                auth_key=auth_key,
            )
            identity_exists = identity_path is not None and Path(identity_path).is_file()
            if launch_state == "prelaunch_rejected":
                if identity_exists:
                    raise ExtractionCleanupBlockedError(
                        "Origin 启动前基线标记为拒绝，但出现了精确身份记录，拒绝继续清理"
                    )
                return True
            if not identity_exists:
                return False
            identity = _read_owned_origin_identity(
                Path(identity_path),
                binding,
                auth_key=auth_key,
            )
            if (identity.pid, identity.start_time_ns) in baseline:
                raise ExtractionCleanupBlockedError(
                    "Origin 身份记录指向启动前基线中的既有进程，拒绝结束该进程"
                )
            deadline = time.monotonic() + self.origin_shutdown_timeout
            last_timeout = None
            while True:
                try:
                    self.origin_process_controller.force_close(
                        identity,
                        timeout=_remaining_before_deadline(
                            deadline,
                            "Origin force close",
                        ),
                    )
                except Exception as exc:
                    if not _caused_by_subprocess_timeout(exc):
                        raise
                    last_timeout = exc
                try:
                    still_running = self.origin_process_controller.is_running(
                        identity,
                        timeout=_remaining_before_deadline(
                            deadline,
                            "Origin exit check",
                        ),
                    )
                except Exception as exc:
                    if not _caused_by_subprocess_timeout(exc):
                        raise
                    last_timeout = exc
                    still_running = True
                if not still_running:
                    return False
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    detail = ""
                    if last_timeout is not None:
                        detail = "；关闭命令或退出检测曾超时"
                    raise ExtractionCleanupBlockedError(
                        f"未能在期限内确认本次任务创建的 Origin 进程退出：{identity.pid}{detail}"
                    )
                time.sleep(min(self.origin_shutdown_poll_interval, remaining))
        except ExtractionCleanupBlockedError:
            raise
        except Exception as exc:
            raise ExtractionCleanupBlockedError(
                f"无法安全回收本次任务创建的 Origin 进程：{exc}"
            ) from exc

    def _drain_termination_process(self, process=None) -> None:
        while True:
            with self._state_lock:
                termination_process = self._termination_process
            if termination_process is None:
                return
            join = getattr(termination_process, "join", None)
            if callable(join):
                wait_budget = self.cancellation_timeout
                if self._require_process_job and self._origin_start_gate_released:
                    wait_budget += self.origin_identity_timeout
                join(timeout=wait_budget)
                is_alive = getattr(termination_process, "is_alive", None)
                if callable(is_alive) and is_alive():
                    raise ExtractionCleanupBlockedError("谱图数据提取取消超时，进程树终止线程未能在期限内结束")
                termination_state = getattr(
                    termination_process,
                    "_spectrum_organizer_termination_state",
                    None,
                )
                require_error_propagation = bool(
                    getattr(
                        termination_process,
                        "_spectrum_organizer_require_error_propagation",
                        False,
                    )
                )
                if termination_state and termination_state.get("error") and (
                    require_error_propagation or process is None or process.poll() is None
                ):
                    raise ExtractionCleanupBlockedError(
                        f"谱图数据提取取消失败，进程树终止命令失败：{termination_state['error']}"
                    )
            else:
                try:
                    termination_process.wait(timeout=self.cancellation_timeout)
                except subprocess.TimeoutExpired as exc:
                    raise ExtractionCleanupBlockedError("谱图数据提取取消超时，进程树终止命令未能在期限内结束") from exc
            with self._state_lock:
                if self._termination_process is termination_process:
                    self._termination_process = None

    def _wait_for_origin_shutdown(self) -> None:
        deadline = time.monotonic() + self.origin_shutdown_timeout
        consecutive_empty_probes = 0
        last_nonempty_origin_processes = ()
        last_probe_error = None

        def last_pid_suffix() -> str:
            if not last_nonempty_origin_processes:
                return ""
            pids = ", ".join(
                str(getattr(item, "pid", "unknown")) for item in last_nonempty_origin_processes
            )
            return f"；最后观测到的 Origin PID：{pids}"

        def timeout_error() -> ExtractionCleanupBlockedError:
            if last_nonempty_origin_processes:
                detail = last_pid_suffix()
                if last_probe_error is not None:
                    detail += f"；最后一次进程检测失败：{last_probe_error}"
                return ExtractionCleanupBlockedError(
                    f"等待 Origin 退出超时，禁止清理临时文件{detail}"
                )
            if last_probe_error is not None:
                return ExtractionCleanupBlockedError(
                    f"无法确认 Origin 进程已全部退出：{last_probe_error}"
                )
            return ExtractionCleanupBlockedError("等待 Origin 退出超时，未能连续确认进程已全部退出")

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise timeout_error()
            try:
                origin_processes = tuple(self.origin_process_probe(timeout=min(5.0, remaining)))
            except Exception as exc:
                consecutive_empty_probes = 0
                last_probe_error = exc
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise timeout_error() from exc
                time.sleep(min(self.origin_shutdown_poll_interval, remaining))
                continue
            last_probe_error = None
            if origin_processes:
                last_nonempty_origin_processes = origin_processes
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if origin_processes:
                    pids = ", ".join(str(getattr(item, "pid", "unknown")) for item in origin_processes)
                    raise ExtractionCleanupBlockedError(
                        f"等待 Origin 退出超时，进程仍在运行，禁止清理临时文件：{pids}"
                    )
                if last_nonempty_origin_processes:
                    raise ExtractionCleanupBlockedError(
                        f"等待 Origin 退出超时，未能连续确认进程已全部退出{last_pid_suffix()}"
                    )
                raise ExtractionCleanupBlockedError("等待 Origin 退出超时，未能连续确认进程已全部退出")
            if origin_processes:
                consecutive_empty_probes = 0
            else:
                consecutive_empty_probes += 1
                if consecutive_empty_probes >= 2:
                    return
            time.sleep(min(self.origin_shutdown_poll_interval, remaining))


def _extraction_process_command(
    manifest_path: Path,
    result_path: Path,
    manifest_sha256: str,
) -> list[str]:
    if getattr(sys, "frozen", False):
        return [
            sys.executable,
            "--origin-extraction-worker",
            manifest_sha256,
            str(manifest_path),
            str(result_path),
        ]
    return [
        sys.executable,
        "-m",
        "spectrum_organizer.origin.extraction_process",
        manifest_sha256,
        str(manifest_path),
        str(result_path),
    ]


def _pre_extraction_process_command(
    manifest_path: Path,
    result_path: Path,
    manifest_sha256: str,
) -> list[str]:
    if getattr(sys, "frozen", False):
        return [
            sys.executable,
            "--pre-extraction-worker",
            manifest_sha256,
            str(manifest_path),
            str(result_path),
        ]
    return [
        sys.executable,
        "-m",
        "spectrum_organizer.pre_extraction_process",
        manifest_sha256,
        str(manifest_path),
        str(result_path),
    ]


def _prepare_reader_temp_root(context: ApprovedPreExtractionRunContext) -> None:
    temp_root = Path(context.temp_root).resolve()
    ownership = read_ownership(temp_root)
    if ownership.run_id != context.run_id:
        raise ProductRunnerError("谱图提取 reader ownership 与 approved run 不一致")
    if ownership.temp_root_identity != context.temp_root_identity:
        raise ProductRunnerError("谱图提取 reader temp root 身份与 approved run 不一致")

    sensitive_paths = {
        temp_root / "pre_extraction_context.json",
        temp_root / "pre_extraction_result.json",
        temp_root / "pre_extraction_result.json.pending",
        temp_root / "pre_extraction.start.gate",
    }
    allowed = set(ownership.allowed_children)
    allowed_identities = dict(ownership.allowed_child_identities)
    for path in sensitive_paths:
        if lexical_path_exists(path) and path not in allowed:
            raise ProductRunnerError(f"未登记的提取前敏感文件不能交给 reader：{path}")
        if lexical_path_exists(path):
            if path.is_dir() and not path.is_symlink():
                raise ProductRunnerError(f"提取前敏感路径不是文件：{path}")
            expected_identity = allowed_identities.get(path)
            if expected_identity is None:
                raise ProductRunnerError(
                    f"提取前敏感文件缺少创建身份记录：{path}"
                )
            try:
                unlink_owned_path(path, expected_identity)
            except IdentityPathError as exc:
                raise ProductRunnerError(f"无法清除提取前敏感文件：{path}: {exc}") from exc

    write_ownership(
        replace(
            ownership,
            allowed_children=tuple(
                path for path in ownership.allowed_children if path not in sensitive_paths
            ),
            allowed_child_identities=tuple(
                (path, identity)
                for path, identity in ownership.allowed_child_identities
                if path not in sensitive_paths
            ),
            protected_paths=(),
        )
    )


def _build_reader_process_command(
    context: ApprovedPreExtractionRunContext,
    source: object,
    snapshot_path: Path,
    *,
    reader_attempt: int = 1,
    cancel_check=None,
) -> ReaderProcessCommand:
    if cancel_check is not None:
        cancel_check()
    temp_root = Path(context.temp_root).resolve()
    ownership = read_ownership(temp_root)
    if ownership.run_id != context.run_id or ownership.protected_paths:
        raise ProductRunnerError("谱图提取 reader ownership 未完成父进程信息清理")

    expected_source_ids = tuple(
        f"S{index:04d}" for index in range(1, len(context.source_fingerprints_before) + 1)
    )
    try:
        source_index = expected_source_ids.index(source.source_id)
    except ValueError as exc:
        raise ProductRunnerError(f"Invalid extraction source id: {source.source_id}") from exc
    if len(context.source_fingerprints_before) != len(context.run_owned_source_copy_paths):
        raise ProductRunnerError("Source fingerprint/copy count mismatch")

    fingerprint = context.source_fingerprints_before[source_index]
    copy_path = Path(context.run_owned_source_copy_paths[source_index]).resolve()
    if Path(source.copy_path).resolve() != copy_path:
        raise ProductRunnerError("谱图提取 reader source copy 与 approved context 不一致")
    if not _path_is_registered(copy_path, ownership.allowed_children):
        raise ProductRunnerError("谱图提取 reader source copy 未登记")
    try:
        if os.path.samefile(copy_path, fingerprint.path):
            raise ProductRunnerError("谱图提取 reader source copy 与原始文件为同一实体")
        copy_identity = file_identity(copy_path)
        if copy_identity == file_identity(fingerprint.path):
            raise ProductRunnerError("谱图提取 reader source copy 与原始文件身份相同")
        copy_size = copy_path.stat().st_size
    except OSError as exc:
        raise ProductRunnerError(f"无法验证谱图提取 reader source copy：{exc}") from exc
    if copy_size != fingerprint.size_bytes or hash_file(
        copy_path,
        cancel_check=cancel_check,
    ) != fingerprint.sha256:
        raise ProductRunnerError("谱图提取 reader source copy 校验失败")
    if cancel_check is not None:
        cancel_check()

    registered_snapshot = Path(snapshot_path).resolve()
    if (
        registered_snapshot.parent != temp_root
        or registered_snapshot not in ownership.allowed_children
    ):
        raise ProductRunnerError("谱图提取 reader snapshot 未登记")
    return ReaderProcessCommand(
        run_id=context.run_id,
        marker_id=ownership.marker_id,
        settings_snapshot=dict(context.settings_snapshot),
        source_copy=VerifiedSourceCopyIdentity(
            source_id=source.source_id,
            copy_path=copy_path,
            sha256=fingerprint.sha256,
            size_bytes=fingerprint.size_bytes,
            device_id=copy_identity[0],
            file_id=copy_identity[1],
        ),
        snapshot_path=registered_snapshot,
        required_temp_bytes=required_temp_bytes(
            sum(item.size_bytes for item in context.source_fingerprints_before)
        ),
        reader_attempt=reader_attempt,
    )


def _path_is_registered(path: Path, allowed_children: tuple[Path, ...]) -> bool:
    resolved = Path(path).resolve()
    for child in allowed_children:
        registered = Path(child).resolve()
        if resolved == registered or registered in resolved.parents:
            return True
    return False


def _process_result_evidence(
    stdout: str,
    *,
    temp_root: Path,
) -> tuple[
    tuple[int, int] | None,
    str | None,
    tuple[tuple[Path, tuple[int, int]], ...],
]:
    return _process_output_evidence(
        stdout,
        temp_root=temp_root,
        error_message="子进程返回的临时文件创建身份无效",
    )


def _reader_process_evidence(
    stdout: str,
    *,
    temp_root: Path,
) -> tuple[
    tuple[int, int] | None,
    str | None,
    tuple[tuple[Path, tuple[int, int]], ...],
]:
    return _process_output_evidence(
        stdout,
        temp_root=temp_root,
        error_message="reader 子进程临时文件创建身份无效",
    )


def _process_output_evidence(
    stdout: str,
    *,
    temp_root: Path,
    error_message: str,
) -> tuple[
    tuple[int, int] | None,
    str | None,
    tuple[tuple[Path, tuple[int, int]], ...],
]:
    try:
        payload = json.loads(stdout)
        result_identity = payload["result_identity"]
        result_sha256 = payload["result_sha256"]
        created = payload["created_temp_identities"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ProductRunnerError(error_message) from exc
    if (
        not isinstance(payload, dict)
        or set(payload)
        != {"result_identity", "result_sha256", "created_temp_identities"}
        or (
            result_identity is not None
            and not _valid_identity_payload(result_identity)
        )
        or not isinstance(created, list)
        or (result_identity is None) != (result_sha256 is None)
        or (
            result_sha256 is not None
            and not _valid_sha256_payload(result_sha256)
        )
    ):
        raise ProductRunnerError(error_message)
    root = Path(temp_root)
    evidence = []
    seen = set()
    for item in created:
        if (
            not isinstance(item, dict)
            or set(item) != {"path", "identity"}
            or not isinstance(item["path"], str)
            or not item["path"]
            or not _valid_identity_payload(item["identity"])
        ):
            raise ProductRunnerError(error_message)
        path = Path(item["path"])
        if not path.is_absolute() or path.parent != root or path in seen:
            raise ProductRunnerError(error_message)
        seen.add(path)
        identity = item["identity"]
        evidence.append((path, (identity[0], identity[1])))
    return (
        None
        if result_identity is None
        else (result_identity[0], result_identity[1]),
        result_sha256,
        tuple(evidence),
    )


def _valid_sha256_payload(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _read_extraction_process_result(result_path: Path) -> dict[str, object]:
    try:
        data = result_path.read_bytes()
    except OSError as exc:
        raise ProductRunnerError(f"无法读取谱图提取子进程结果：{exc}") from exc
    return _decode_extraction_process_result(data)


def _read_authenticated_extraction_process_result(
    result_path: Path,
    *,
    expected_identity: tuple[int, int] | None,
    expected_sha256: str | None,
) -> dict[str, object]:
    data = _read_authenticated_result_bytes(
        result_path,
        "谱图提取子进程",
        expected_identity=expected_identity,
        expected_sha256=expected_sha256,
    )
    return _decode_extraction_process_result(data)


def _decode_extraction_process_result(data: bytes) -> dict[str, object]:
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProductRunnerError(f"无法读取谱图提取子进程结果：{exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("ok"), bool):
        raise ProductRunnerError("谱图提取子进程结果格式无效")
    expected_fields = (
        {"ok", "summary"}
        if payload["ok"]
        else {"ok", "error", "error_type", "error_notes"}
    )
    if set(payload) != expected_fields:
        raise ProductRunnerError("谱图提取子进程结果字段无效")
    if payload["ok"] and not isinstance(payload["summary"], dict):
        raise ProductRunnerError("谱图提取子进程 summary 格式无效")
    if not payload["ok"] and (
        not isinstance(payload["error"], str)
        or not payload["error"]
        or not isinstance(payload["error_type"], str)
        or not payload["error_type"]
        or not isinstance(payload["error_notes"], list)
        or any(not isinstance(note, str) for note in payload["error_notes"])
    ):
        raise ProductRunnerError("谱图提取子进程错误结果格式无效")
    return payload


def _read_process_payload(result_path: Path, label: str) -> dict[str, object]:
    try:
        data = result_path.read_bytes()
    except OSError as exc:
        raise ProductRunnerError(f"无法读取{label}结果：{exc}") from exc
    return _decode_process_payload(data, label)


def _read_authenticated_process_payload(
    result_path: Path,
    label: str,
    *,
    expected_identity: tuple[int, int] | None,
    expected_sha256: str | None,
) -> dict[str, object]:
    data = _read_authenticated_result_bytes(
        result_path,
        label,
        expected_identity=expected_identity,
        expected_sha256=expected_sha256,
    )
    return _decode_process_payload(data, label)


def _read_authenticated_result_bytes(
    result_path: Path,
    label: str,
    *,
    expected_identity: tuple[int, int] | None,
    expected_sha256: str | None,
) -> bytes:
    path = Path(result_path)
    if expected_identity is None or not _valid_sha256_payload(expected_sha256):
        raise ProductRunnerError(f"{label}结果创建证据无效")
    try:
        with hold_file_identity(path, expected_identity):
            with path.open("rb", buffering=0) as stream:
                status = os.fstat(stream.fileno())
                if (status.st_dev, status.st_ino) != expected_identity:
                    raise ProductRunnerError(f"{label}结果身份在读取前发生变化")
                data = stream.read()
                if path_identity(path) != expected_identity:
                    raise ProductRunnerError(f"{label}结果路径在读取时发生变化")
    except (OSError, IdentityPathError) as exc:
        raise ProductRunnerError(f"无法读取{label}结果：{exc}") from exc
    if hashlib.sha256(data).hexdigest() != expected_sha256:
        raise ProductRunnerError(f"{label}结果内容摘要不匹配")
    return data


def _decode_process_payload(data: bytes, label: str) -> dict[str, object]:
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProductRunnerError(f"无法读取{label}结果：{exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("ok"), bool):
        raise ProductRunnerError(f"{label}结果格式无效")
    expected_fields = {"ok", "context"} if payload["ok"] else {"ok", "error", "error_type"}
    if set(payload) != expected_fields:
        raise ProductRunnerError(f"{label}结果字段无效")
    if payload["ok"] and not isinstance(payload["context"], dict):
        raise ProductRunnerError(f"{label} context 格式无效")
    if not payload["ok"] and (
        not isinstance(payload["error"], str)
        or not payload["error"]
        or not isinstance(payload["error_type"], str)
        or not payload["error_type"]
    ):
        raise ProductRunnerError(f"{label}错误结果格式无效")
    return payload


def _validate_child_summary(
    payload,
    *,
    context: ApprovedPreExtractionRunContext,
    expected_snapshot_path: Path,
    expected_source,
    cancel_check=None,
    validate_snapshot_registration: bool = True,
) -> SourceExtractionSummary:
    if cancel_check is not None:
        cancel_check()
    try:
        if not isinstance(payload, dict):
            raise TypeError("summary is not an object")
        child_summary = _reader_summary_from_payload(payload)
    except Exception as exc:
        raise ProductRunnerError(f"谱图提取子进程结果格式无效：{exc}") from exc
    if child_summary.snapshot_path.resolve() != expected_snapshot_path.resolve():
        raise ProductRunnerError("谱图提取子进程结果 snapshot_path 与本次 snapshot 不一致")
    if child_summary.source_id != expected_source.source_id:
        raise ProductRunnerError("谱图提取子进程结果包含额外或错误 source")
    if validate_snapshot_registration:
        _validate_snapshot_registration(context, expected_snapshot_path)
    if not expected_snapshot_path.is_file():
        raise ProductRunnerError("谱图提取子进程结果未创建本次 snapshot")
    _validate_snapshot_source_presence(
        expected_snapshot_path,
        expected_source.source_id,
        cancel_check=cancel_check,
    )
    snapshot = _ReadOnlySnapshotView(expected_snapshot_path)
    _validate_reconciled_snapshot_partitions(
        expected_snapshot_path,
        (expected_source.source_id,),
        cancel_check=cancel_check,
        s1_limit=_confirmed_s1_limit(context.settings_snapshot),
        steady_emission_y=_confirmed_steady_emission_y(context.settings_snapshot),
        allow_missing_s1=_confirmed_allow_missing_s1(context.settings_snapshot),
    )
    expected_summary = _summarize_extraction(snapshot, (expected_source,), cancel_check=cancel_check)
    _validate_snapshot_source_records(
        expected_snapshot_path,
        context,
        (expected_source,),
        cancel_check=cancel_check,
        verify_copy_source_id=expected_source.source_id,
        verify_registration=False,
        require_original_provenance=False,
    )
    expected_source_summary = expected_summary.source_summaries[0]
    if (
        child_summary.inventory_count,
        child_summary.result_count,
        child_summary.extracted_count,
        child_summary.rejected_count,
    ) != (
        expected_source_summary.inventory_count,
        expected_source_summary.result_count,
        expected_source_summary.extracted_count,
        expected_source_summary.rejected_count,
    ):
        raise ProductRunnerError("谱图提取子进程结果与 approved context 或 SQLite snapshot 不一致")
    return expected_source_summary


def _validate_reconciled_snapshot_partitions(
    snapshot_path: Path,
    source_ids: tuple[str, ...],
    *,
    cancel_check=None,
    s1_limit: int | float | None = None,
    steady_emission_y: str | None = None,
    allow_missing_s1: bool = False,
) -> None:
    from spectrum_organizer.store.run_snapshot import ReconciliationError, validate_reconciled_sources

    try:
        validate_reconciled_sources(
            snapshot_path,
            source_ids,
            cancel_check=cancel_check,
            s1_limit=s1_limit,
            steady_emission_y=steady_emission_y,
            allow_missing_s1=allow_missing_s1,
        )
    except (OSError, sqlite3.Error, ReconciliationError) as exc:
        raise ProductRunnerError(f"谱图提取 snapshot reconciliation 失败：{exc}") from exc


def _snapshot_partition_digest(snapshot_path: Path, source_id: str, *, cancel_check=None) -> str:
    tables = (
        ("source_files", "source_id"),
        ("inventory_rows", "page_type, folder_path, short_name"),
        ("book_results", "page_type, folder_path, short_name"),
        ("worker_attempts", "attempt, id"),
        ("reconciliation_results", "source_id"),
    )
    digest = hashlib.sha256()
    try:
        connection = sqlite3.connect(f"{Path(snapshot_path).resolve().as_uri()}?mode=ro", uri=True)
        try:
            for table, ordering in tables:
                if cancel_check is not None:
                    cancel_check()
                digest.update(json.dumps(("table", table), separators=(",", ":")).encode("utf-8"))
                cursor = connection.execute(
                    f"select * from {table} where source_id = ? order by {ordering}",
                    (source_id,),
                )
                for row in cursor:
                    if cancel_check is not None:
                        cancel_check()
                    digest.update(b"\n")
                    digest.update(
                        json.dumps(row, ensure_ascii=False, default=str, separators=(",", ":")).encode("utf-8")
                    )
        finally:
            connection.close()
    except (OSError, sqlite3.Error) as exc:
        raise ProductRunnerError(f"谱图提取 snapshot 分区摘要复核失败：{exc}") from exc
    return digest.hexdigest()


def _validate_snapshot_source_ids(
    snapshot_path: Path,
    expected_source_ids: tuple[str, ...],
    *,
    cancel_check=None,
) -> None:
    try:
        connection = sqlite3.connect(f"{snapshot_path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            actual_source_ids = set()
            for table in (
                "source_files",
                "inventory_rows",
                "book_results",
                "worker_attempts",
                "reconciliation_results",
            ):
                for row in connection.execute(f"select distinct source_id from {table}"):
                    if cancel_check is not None:
                        cancel_check()
                    actual_source_ids.add(str(row[0]))
        finally:
            connection.close()
    except (OSError, sqlite3.Error) as exc:
        raise ProductRunnerError(f"谱图提取子进程结果无法独立读取 SQLite snapshot：{exc}") from exc
    if actual_source_ids != set(expected_source_ids):
        raise ProductRunnerError("谱图提取子进程结果的 SQLite source 集合不一致")


def _validate_snapshot_source_presence(
    snapshot_path: Path,
    expected_source_id: str,
    *,
    cancel_check=None,
) -> None:
    if cancel_check is not None:
        cancel_check()
    try:
        connection = sqlite3.connect(f"{snapshot_path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            row = connection.execute(
                "select 1 from source_files where source_id = ?",
                (expected_source_id,),
            ).fetchone()
        finally:
            connection.close()
    except (OSError, sqlite3.Error) as exc:
        raise ProductRunnerError(f"谱图提取子进程结果无法独立读取 SQLite snapshot：{exc}") from exc
    if cancel_check is not None:
        cancel_check()
    if row is None:
        raise ProductRunnerError("谱图提取子进程结果的 SQLite source 记录缺失")


def _validate_snapshot_registration(context, snapshot_path: Path) -> None:
    temp_root = Path(context.temp_root).resolve()
    snapshot = Path(snapshot_path).resolve()
    ownership = read_ownership(temp_root)
    if snapshot.parent != temp_root or snapshot not in {path.resolve() for path in ownership.allowed_children}:
        raise ProductRunnerError("谱图提取子进程结果的 snapshot 未登记为 approved temp root 直接子文件")


def _validate_snapshot_source_records(
    snapshot_path: Path,
    context,
    processed_sources: tuple[object, ...],
    *,
    cancel_check=None,
    verify_copy_source_id: str | None = None,
    verify_registration: bool = True,
    require_original_provenance: bool = True,
) -> None:
    source_ids = tuple(source.source_id for source in processed_sources)
    if not source_ids:
        return
    placeholders = ", ".join("?" for _source_id in source_ids)
    try:
        connection = sqlite3.connect(f"{snapshot_path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            rows = []
            for row in connection.execute(
                f"select source_id, copy_path, sha256, original_path, "
                f"original_size_bytes, original_mtime_ns "
                f"from source_files "
                f"where source_id in ({placeholders}) order by source_id",
                source_ids,
            ):
                if cancel_check is not None:
                    cancel_check()
                rows.append(row)
        finally:
            connection.close()
    except (OSError, sqlite3.Error) as exc:
        raise ProductRunnerError(f"谱图提取子进程结果无法独立读取 SQLite source 记录：{exc}") from exc
    records = {
        str(row[0]): (
            str(row[1]),
            str(row[2]),
            row[3],
            row[4],
            row[5],
        )
        for row in rows
    }
    approved_allowed_children = None
    if verify_registration:
        ownership = read_ownership(Path(context.temp_root).resolve())
        approved_allowed_children = {path.resolve() for path in ownership.allowed_children}
    for source in processed_sources:
        source_index = int(source.source_id[1:]) - 1
        expected_sha256 = context.source_fingerprints_before[source_index].sha256
        fingerprint = context.source_fingerprints_before[source_index]
        try:
            (
                copy_path,
                sha256,
                original_path,
                original_size_bytes,
                original_mtime_ns,
            ) = records[source.source_id]
        except KeyError as exc:
            raise ProductRunnerError("谱图提取子进程结果的 SQLite source 记录缺失") from exc
        if sha256 != expected_sha256:
            raise ProductRunnerError("谱图提取子进程结果的 SQLite source sha256 不一致")
        provenance = (
            original_path,
            original_size_bytes,
            original_mtime_ns,
        )
        expected_provenance = (
            _canonical_source_snapshot_path(fingerprint),
            fingerprint.size_bytes,
            fingerprint.mtime_ns,
        )
        if (
            require_original_provenance
            and provenance != expected_provenance
        ):
            raise ProductRunnerError(
                "谱图提取子进程结果的 SQLite 原始文件身份不一致"
            )
        if (
            not require_original_provenance
            and provenance != (None, None, None)
        ):
            raise ProductRunnerError(
                "谱图提取 reader 不得写入原始文件身份"
            )
        _validate_snapshot_copy_ownership(
            context,
            source,
            copy_path,
            cancel_check=cancel_check,
            verify_content=source.source_id == verify_copy_source_id,
            approved_allowed_children=approved_allowed_children,
        )


def _validate_snapshot_copy_ownership(
    context,
    source,
    copy_path: str,
    *,
    cancel_check=None,
    verify_content: bool = True,
    approved_allowed_children: set[Path] | None = None,
) -> None:
    temp_root = Path(context.temp_root).resolve()
    copy = Path(copy_path).resolve()
    if copy == temp_root or temp_root not in copy.parents:
        raise ProductRunnerError("谱图提取子进程结果 copy_path 位于 approved temp root 之外")
    source_index = int(source.source_id[1:]) - 1
    fingerprint = context.source_fingerprints_before[source_index]
    approved_initial_copy = Path(context.run_owned_source_copy_paths[source_index]).resolve()
    if (
        approved_allowed_children is not None
        and copy != approved_initial_copy
        and copy not in approved_allowed_children
    ):
        raise ProductRunnerError("谱图提取子进程结果 copy_path 未登记到 ownership")
    if not verify_content:
        return
    try:
        if (
            not copy.is_file()
            or copy.stat().st_size != fingerprint.size_bytes
            or hash_file(copy, cancel_check=cancel_check) != fingerprint.sha256
        ):
            raise ProductRunnerError("谱图提取子进程结果 copy_path 内容与 approved source fingerprint 不一致")
    except OSError as exc:
        raise ProductRunnerError(f"谱图提取子进程结果无法复核 copy_path：{exc}") from exc


def _validate_summary_closure(summary: ExtractionPhaseSummary) -> None:
    for source in summary.source_summaries:
        if source.inventory_count != source.result_count:
            raise ProductRunnerError("谱图提取子进程结果的 source 计数未闭环")
        if source.result_count != source.extracted_count + source.rejected_count:
            raise ProductRunnerError("谱图提取子进程结果的 source 状态计数未闭环")
    expected_totals = (
        sum(source.inventory_count for source in summary.source_summaries),
        sum(source.result_count for source in summary.source_summaries),
        sum(source.extracted_count for source in summary.source_summaries),
        sum(source.rejected_count for source in summary.source_summaries),
    )
    actual_totals = (
        summary.total_inventory_count,
        summary.total_result_count,
        summary.total_extracted_count,
        summary.total_rejected_count,
    )
    if actual_totals != expected_totals:
        raise ProductRunnerError("谱图提取子进程结果的总计未闭环")


class _ReadOnlySnapshotView:
    def __init__(self, path: Path):
        self.path = Path(path).resolve()

    def source_copy_path(self, source_id: str) -> Path:
        row = self._query_one("select copy_path from source_files where source_id = ?", (source_id,))
        if row is None:
            raise ProductRunnerError(f"谱图提取子进程结果缺失 SQLite source：{source_id}")
        return Path(row[0])

    def inventory_count(self, source_id: str) -> int:
        return self._count("inventory_rows", source_id)

    def result_count(self, source_id: str) -> int:
        return self._count("book_results", source_id)

    def status_count(self, source_id: str, status: str) -> int:
        row = self._query_one(
            "select count(*) from book_results where source_id = ? and status = ?",
            (source_id, status),
        )
        return int(row[0])

    def source_counts(self, source_id: str, *, cancel_check=None) -> tuple[int, int, int, int]:
        inventory_count = 0
        result_count = 0
        extracted_count = 0
        rejected_count = 0
        try:
            connection = sqlite3.connect(f"{self.path.as_uri()}?mode=ro", uri=True)
            try:
                for _row in connection.execute(
                    "select 1 from inventory_rows where source_id = ?",
                    (source_id,),
                ):
                    if cancel_check is not None:
                        cancel_check()
                    inventory_count += 1
                for row in connection.execute(
                    "select status from book_results where source_id = ?",
                    (source_id,),
                ):
                    if cancel_check is not None:
                        cancel_check()
                    result_count += 1
                    if row[0] == "extracted":
                        extracted_count += 1
                    elif row[0] == "rejected":
                        rejected_count += 1
            finally:
                connection.close()
        except (OSError, sqlite3.Error) as exc:
            raise ProductRunnerError(f"谱图提取子进程结果无法只读复核 SQLite snapshot：{exc}") from exc
        return inventory_count, result_count, extracted_count, rejected_count

    def _count(self, table: str, source_id: str) -> int:
        row = self._query_one(f"select count(*) from {table} where source_id = ?", (source_id,))
        return int(row[0])

    def _query_one(self, query: str, parameters: tuple[object, ...]):
        try:
            connection = sqlite3.connect(f"{self.path.as_uri()}?mode=ro", uri=True)
            try:
                return connection.execute(query, parameters).fetchone()
            finally:
                connection.close()
        except (OSError, sqlite3.Error) as exc:
            raise ProductRunnerError(f"谱图提取子进程结果无法只读复核 SQLite snapshot：{exc}") from exc


def _terminate_process_nonblocking(process):
    try:
        bound_job = getattr(process, "_spectrum_organizer_job", None)
        if process.poll() is not None and bound_job is None:
            return None
        if sys.platform == "win32":
            termination_state: dict[str, str | None] = {"error": None}

            def terminate_process_tree():
                try:
                    if process.poll() is not None and getattr(
                        process,
                        "_spectrum_organizer_job",
                        None,
                    ) is None:
                        return
                    terminate_bound_process(process)
                except Exception as exc:
                    termination_state["error"] = str(exc)

            termination_thread = threading.Thread(target=terminate_process_tree, daemon=True)
            termination_thread._spectrum_organizer_termination_state = termination_state
            termination_thread.start()
            return termination_thread
        process.terminate()
    except Exception:
        kill = getattr(process, "kill", None)
        if kill is not None:
            try:
                kill()
            except Exception:
                pass
    return None


def _wait_for_process_exit(process, timeout: float) -> bool:
    if process is None or process.poll() is not None:
        return True
    wait = getattr(process, "wait", None)
    if not callable(wait):
        return False
    try:
        wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        return False
    return process.poll() is not None


def _remaining_before_deadline(deadline: float, operation: str) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise subprocess.TimeoutExpired(operation, 0)
    return remaining


def _caused_by_subprocess_timeout(error: BaseException) -> bool:
    current = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        if isinstance(current, subprocess.TimeoutExpired):
            return True
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return False


def _terminate_extraction_process_nonblocking(
    process,
    *,
    identity_path: Path | None,
    launch_path: Path | None = None,
    expected_binding: tuple[str, str, str, int] | None,
    identity_wait_timeout: float,
    sidecar_auth_key: str | None = None,
):
    if identity_path is None or expected_binding is None or sys.platform != "win32":
        return _terminate_process_nonblocking(process)
    termination_state: dict[str, str | None] = {"error": None}

    def wait_for_identity_then_terminate():
        try:
            deadline = time.monotonic() + identity_wait_timeout
            identity_verified = False
            prelaunch_rejected = False
            while True:
                if process.poll() is not None:
                    return
                if time.monotonic() >= deadline:
                    break
                if launch_path is not None:
                    try:
                        _, launch_state = _read_origin_launch_baseline(
                            Path(launch_path),
                            expected_binding,
                            auth_key=sidecar_auth_key,
                        )
                        if launch_state == "prelaunch_rejected":
                            prelaunch_rejected = True
                            break
                    except (OSError, ValueError, json.JSONDecodeError):
                        pass
                try:
                    _read_owned_origin_identity(
                        Path(identity_path),
                        expected_binding,
                        auth_key=sidecar_auth_key,
                    )
                    if time.monotonic() >= deadline:
                        break
                    identity_verified = True
                    break
                except (OSError, ValueError, json.JSONDecodeError):
                    pass
                time.sleep(0.05)
            if not identity_verified and not prelaunch_rejected:
                termination_state["error"] = (
                    "取消任务时未在期限内取得本次任务 Origin 的精确身份记录，"
                    "已拒绝结束 reader 或任何 Origin 进程"
                )
                return
            terminate_bound_process(process)
        except Exception as exc:
            termination_state["error"] = str(exc)

    termination_thread = threading.Thread(target=wait_for_identity_then_terminate, daemon=True)
    termination_thread._spectrum_organizer_termination_state = termination_state
    termination_thread._spectrum_organizer_require_error_propagation = True
    termination_thread.start()
    return termination_thread


_ORIGIN_SIDECAR_BINDING_FIELDS = {
    "schema_version",
    "creation_identity",
    "run_id",
    "marker_id",
    "source_id",
    "reader_attempt",
}


def _read_origin_launch_baseline(
    path: Path,
    expected_binding: tuple[str, str, str, int],
    *,
    auth_key: str | None = None,
) -> tuple[set[tuple[int, int]], str]:
    payload = _read_strict_origin_sidecar(
        path,
        expected_binding,
        extra_fields={"launch_state", "processes"},
        auth_key=auth_key,
    )
    launch_state = payload["launch_state"]
    if launch_state not in {"launch_allowed", "prelaunch_rejected"}:
        raise ValueError("Origin 启动基线 launch_state 字段无效")
    processes = payload["processes"]
    if not isinstance(processes, list):
        raise ValueError("Origin 启动基线 processes 字段无效")
    identities: set[tuple[int, int]] = set()
    for record in processes:
        if not isinstance(record, dict) or set(record) != {"pid", "start_time_ns"}:
            raise ValueError("Origin 启动基线进程记录字段无效")
        identity = (
            _required_positive_sidecar_int(record, "pid"),
            _required_positive_sidecar_int(record, "start_time_ns"),
        )
        if identity in identities:
            raise ValueError("Origin 启动基线包含重复进程身份")
        identities.add(identity)
    if launch_state == "launch_allowed" and identities:
        raise ValueError("Origin 启动基线允许启动时不应包含既有进程")
    if launch_state == "prelaunch_rejected" and not identities:
        raise ValueError("Origin 启动前拒绝状态缺少既有进程身份")
    return identities, launch_state


def _read_owned_origin_identity(
    path: Path,
    expected_binding: tuple[str, str, str, int],
    *,
    auth_key: str | None = None,
) -> ProcessIdentity:
    payload = _read_strict_origin_sidecar(
        path,
        expected_binding,
        extra_fields={"pid", "start_time_ns"},
        auth_key=auth_key,
    )
    return ProcessIdentity(
        pid=_required_positive_sidecar_int(payload, "pid"),
        start_time_ns=_required_positive_sidecar_int(payload, "start_time_ns"),
    )


def _read_strict_origin_sidecar(
    path: Path,
    expected_binding: tuple[str, str, str, int],
    *,
    extra_fields: set[str],
    auth_key: str | None = None,
) -> dict[str, object]:
    sidecar = Path(path)
    try:
        ownership = read_ownership(sidecar.resolve().parent)
        expected_identity = dict(ownership.allowed_child_identities).get(
            sidecar.resolve()
        )
        if expected_identity is None:
            raise ValueError("Origin 进程身份记录缺少可信创建身份")
        with hold_file_identity(
            sidecar,
            expected_identity,
            allow_write=False,
        ):
            payload = json.loads(sidecar.read_text(encoding="utf-8"))
    except (IdentityPathError, OwnershipError) as exc:
        raise ValueError("Origin 进程身份记录创建身份无效") from exc
    expected_fields = _ORIGIN_SIDECAR_BINDING_FIELDS | extra_fields
    if auth_key is not None:
        expected_fields = expected_fields | {"content_hmac"}
    if not isinstance(payload, dict) or set(payload) != expected_fields:
        raise ValueError("Origin 进程身份记录字段无效")
    if auth_key is not None:
        content_hmac = payload.get("content_hmac")
        if (
            not isinstance(content_hmac, str)
            or not secrets.compare_digest(
                content_hmac,
                sidecar_content_hmac(payload, auth_key),
            )
        ):
            raise ValueError("Origin 进程身份记录内容认证失败")
    run_id, marker_id, source_id, reader_attempt = expected_binding
    creation_identity = payload.get("creation_identity")
    if (
        isinstance(payload["schema_version"], bool)
        or not isinstance(payload["schema_version"], int)
        or payload["schema_version"] != 1
        or not isinstance(payload["run_id"], str)
        or payload["run_id"] != run_id
        or not isinstance(payload["marker_id"], str)
        or payload["marker_id"] != marker_id
        or not isinstance(payload["source_id"], str)
        or payload["source_id"] != source_id
        or isinstance(payload["reader_attempt"], bool)
        or not isinstance(payload["reader_attempt"], int)
        or payload["reader_attempt"] != reader_attempt
        or not _valid_identity_payload(creation_identity)
        or tuple(creation_identity) != expected_identity
    ):
        raise ValueError("Origin 进程身份记录与当前 reader 任务不匹配")
    return payload


def _read_observed_origin_open_target(
    context,
    source,
    reader_attempt: int,
    *,
    auth_key: str | None = None,
) -> str | None:
    temp_root = Path(context.temp_root).resolve()
    marker_path = temp_root / f"origin_open_target.{source.source_id}.attempt{reader_attempt}.json"
    pending_path = marker_path.with_name(f"{marker_path.name}.pending")
    if not marker_path.exists():
        if lexical_path_exists(pending_path):
            raise ProductRunnerError("Origin 项目打开记录未完整写入")
        return None
    ownership = read_ownership(temp_root)
    binding = (context.run_id, ownership.marker_id, source.source_id, reader_attempt)
    try:
        payload = _read_strict_origin_sidecar(
            marker_path,
            binding,
            extra_fields={"open_target"},
            auth_key=auth_key,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ProductRunnerError(f"Origin 项目打开记录无效：{exc}") from exc
    open_target = payload["open_target"]
    if not isinstance(open_target, str) or not open_target:
        raise ProductRunnerError("Origin 项目打开记录路径无效")
    expected_target = Path(source.copy_path).resolve()
    if Path(open_target).resolve() != expected_target:
        raise ProductRunnerError("Origin 项目打开记录与当前只读副本不匹配")
    return str(expected_target)


def _required_positive_sidecar_int(payload: dict[str, object], field: str) -> int:
    value = payload[field]
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"Origin 进程身份记录 {field} 字段无效")
    return value


def _subprocess_environment(
    *,
    start_gate_path: Path | None = None,
    start_gate_token: str | None = None,
    sidecar_auth_key: str | None = None,
) -> dict[str, str]:
    environment = os.environ.copy()
    if not getattr(sys, "frozen", False):
        source_root = str(Path(__file__).resolve().parents[1])
        entries = [entry for entry in environment.get("PYTHONPATH", "").split(os.pathsep) if entry]
        if source_root not in entries:
            environment["PYTHONPATH"] = os.pathsep.join((source_root, *entries))
    if start_gate_path is not None:
        if not start_gate_token:
            raise ValueError("Parent start gate token is required")
        environment[PARENT_START_GATE_ENV] = str(Path(start_gate_path).resolve())
        environment[PARENT_START_GATE_TOKEN_ENV] = start_gate_token
    else:
        environment.pop(PARENT_START_GATE_ENV, None)
        environment.pop(PARENT_START_GATE_TOKEN_ENV, None)
    if sidecar_auth_key is None:
        environment.pop(READER_SIDECAR_AUTH_ENV, None)
    else:
        validate_sidecar_auth_key(sidecar_auth_key)
        environment[READER_SIDECAR_AUTH_ENV] = sidecar_auth_key
    return environment


def _cleanup_temp_root_error(
    temp_root: Path | None,
    *,
    expected_root_identity: tuple[int, int] | None = None,
) -> str | None:
    if temp_root is None:
        return None
    target = Path(temp_root)
    if not lexical_path_exists(target):
        return None
    if expected_root_identity is None:
        return "active temp cleanup requires caller-held root identity"
    try:
        cleanup_owned_temp_root(
            target,
            expected_root_identity=expected_root_identity,
        )
    except Exception as exc:
        return str(exc)
    return None


def _build_extraction_sources(context: ApprovedPreExtractionRunContext, extraction_source_type) -> tuple[object, ...]:
    if len(context.source_fingerprints_before) != len(context.run_owned_source_copy_paths):
        raise ProductRunnerError("Source fingerprint/copy count mismatch")
    temp_root = Path(context.temp_root).resolve()
    resolved_copies = tuple(Path(path).resolve() for path in context.run_owned_source_copy_paths)
    if len(set(resolved_copies)) != len(resolved_copies):
        raise ProductRunnerError("Run-owned source copies must be unique per selected source")
    if len({path.parent for path in resolved_copies}) != len(resolved_copies):
        raise ProductRunnerError("Run-owned source copies must use distinct per-source directories")
    sources = []
    for index, (fingerprint, copy_path) in enumerate(
        zip(context.source_fingerprints_before, resolved_copies),
        start=1,
    ):
        copy = Path(copy_path).resolve()
        if copy == temp_root or temp_root not in copy.parents:
            raise ProductRunnerError(f"Run-owned source copy is outside the task temp root: {copy}")
        if not copy.parent.name.startswith(f"source-{index:04d}"):
            raise ProductRunnerError(f"Run-owned source copy is in the wrong per-source directory: {copy}")
        original = fingerprint.path
        retry_prefix = f"{original.stem}.retry"
        retry_number = copy.stem[len(retry_prefix):] if copy.stem.startswith(retry_prefix) else None
        if copy.name != original.name and not (
            copy.suffix == original.suffix
            and retry_number is not None
            and (not retry_number or retry_number.isdigit())
        ):
            raise ProductRunnerError(f"Run-owned source copy does not preserve the source filename: {copy}")
        canonical_original = Path(
            _canonical_source_snapshot_path(fingerprint)
        )
        sources.append(
            extraction_source_type(
                source_id=f"S{index:04d}",
                copy_path=copy,
                sha256=fingerprint.sha256,
                original_path=fingerprint.path,
                original_canonical_path=canonical_original,
                allowed_children=(temp_root,),
                protected_paths=(
                    fingerprint.path,
                    canonical_original,
                ),
                size_bytes=fingerprint.size_bytes,
                original_mtime_ns=fingerprint.mtime_ns,
            )
        )
    return tuple(sources)


def _register_snapshot_path(
    context: ApprovedPreExtractionRunContext,
    snapshot_path: Path | None,
    *,
    allow_existing: bool = False,
) -> Path:
    temp_root = Path(context.temp_root).resolve()
    path = Path(snapshot_path or (temp_root / "run_snapshot.sqlite3")).resolve()
    if path.parent != temp_root:
        raise ProductRunnerError(f"Snapshot path must be an owned immediate child of the task temp root: {path}")
    if path.name == OWNERSHIP_FILE:
        raise ProductRunnerError(f"Snapshot path cannot replace reserved ownership metadata: {path}")
    ownership = read_ownership(temp_root)
    for owned_path in (
        path,
        path.with_name(path.name + "-journal"),
        path.with_name(path.name + "-wal"),
        path.with_name(path.name + "-shm"),
    ):
        if owned_path not in ownership.allowed_children:
            ownership = add_allowed_child(ownership, owned_path)
    created_identity = None
    try:
        with path.open("xb") as stream:
            status = os.fstat(stream.fileno())
            created_identity = (status.st_dev, status.st_ino)
            if path_identity(path) != created_identity:
                raise ProductRunnerError(
                    f"Snapshot identity changed during reservation: {path}"
                )
            try:
                ownership = bind_allowed_child_identity(
                    ownership,
                    path,
                    expected_identity=created_identity,
                )
            except OwnershipError as exc:
                raise ProductRunnerError(str(exc)) from exc
    except FileExistsError as exc:
        if not allow_existing or path.is_symlink() or not path.is_file():
            raise ProductRunnerError(f"Snapshot path already exists: {path}") from exc
    except OSError as exc:
        raise ProductRunnerError(f"Could not reserve snapshot path {path}: {exc}") from exc
    if created_identity is None:
        expected_identity = dict(ownership.allowed_child_identities).get(path)
        if expected_identity is None or path_identity(path) != expected_identity:
            raise ProductRunnerError(
                f"Existing snapshot path lacks its creation identity: {path}"
            )
    return path


def _summarize_extraction(snapshot, sources: tuple[object, ...], *, cancel_check=None) -> ExtractionPhaseSummary:
    source_summaries = []
    for source in sources:
        if cancel_check is not None:
            cancel_check()
        source_counts = getattr(snapshot, "source_counts", None)
        if callable(source_counts):
            inventory_count, result_count, extracted_count, rejected_count = source_counts(
                source.source_id,
                cancel_check=cancel_check,
            )
        else:
            inventory_count = snapshot.inventory_count(source.source_id)
            result_count = snapshot.result_count(source.source_id)
            extracted_count = snapshot.status_count(source.source_id, "extracted")
            rejected_count = snapshot.status_count(source.source_id, "rejected")
        source_summaries.append(
            SourceExtractionSummary(
                source_id=source.source_id,
                original_path=str(source.original_path),
                copy_path=str(snapshot.source_copy_path(source.source_id)),
                inventory_count=inventory_count,
                result_count=result_count,
                extracted_count=extracted_count,
                rejected_count=rejected_count,
            )
        )
    return _phase_summary(Path(snapshot.path), tuple(source_summaries))


def _phase_summary(snapshot_path: Path, source_summaries: tuple[SourceExtractionSummary, ...]) -> ExtractionPhaseSummary:
    return ExtractionPhaseSummary(
        snapshot_path=Path(snapshot_path),
        source_summaries=source_summaries,
        total_inventory_count=sum(item.inventory_count for item in source_summaries),
        total_result_count=sum(item.result_count for item in source_summaries),
        total_extracted_count=sum(item.extracted_count for item in source_summaries),
        total_rejected_count=sum(item.rejected_count for item in source_summaries),
    )


def complete_pre_extraction_origin_process_gate(dialog_port, origin_process_probe, process_controller) -> None:
    _require_dialog_action(dialog_port, save_and_close_origin_dialog(), "retry")
    _complete_origin_process_preflight(dialog_port, origin_process_probe, process_controller)


def _complete_origin_process_preflight(dialog_port, origin_process_probe, process_controller) -> None:
    confirmed_hidden: frozenset[ProcessIdentity] = frozenset()
    while True:
        processes = list(origin_process_probe(timeout=5.0))
        outcome = preflight_origin_boundary(
            processes,
            process_controller,
            hidden_confirmation=confirmed_hidden,
        )
        if outcome.can_continue:
            return
        if outcome.dialog is None:
            raise ProductRunnerError("Origin process preflight blocked without a dialog")
        response = dialog_port.choose(outcome.dialog)
        if response.action == "retry":
            confirmed_hidden = frozenset()
            continue
        if response.action in {"confirm", "confirm_close_hidden_origin"}:
            confirmed_hidden = frozenset(
                process.identity
                for process in processes
                if classify_process(process) == "preexisting_hidden"
            )
            continue
        raise ProductRunnerError(f"Origin process preflight returned {response.action}")


def _require_dialog_action(dialog_port, request, expected_action: str) -> None:
    response = dialog_port.choose(request)
    if response.action != expected_action:
        raise ProductRunnerError(f"Manual dialog {request.kind} returned {response.action}; expected {expected_action}")


def check_task15_readiness(deps: ProductRunnerDependencies) -> Task15ReadinessReport:
    missing: list[str] = []
    if not _dialog_port_ready(deps.manual_dialog_port):
        missing.append("manual_dialog_port")
    if not _worker_factory_ready(deps.extraction_worker_factory):
        missing.append("extraction_worker_factory")
    for name in (
        "output_worker",
        "verifier_worker",
        "create_staging",
        "publish_run",
        "report_builder",
    ):
        if not callable(getattr(deps, name)):
            missing.append(name)
    if not _audit_hook_ready(deps.protected_path_audit_hook, "protected_path_audit"):
        missing.append("protected_path_audit_hook")
    if not _audit_hook_ready(deps.final_process_count_hook, "final_process_count"):
        missing.append("final_process_count_hook")
    if not callable(deps.state_machine_factory):
        missing.append("state_machine_factory")
    if deps.mode != "book_only":
        missing.append("book_only mode")
    ready = not missing
    next_action = "Stop before Task 15 smoke execution." if ready else "Provide missing production dependencies before Task 15."
    return Task15ReadinessReport(ready=ready, missing=tuple(missing), next_action=next_action)


class ProductWorkflowRunner:
    def __init__(self, deps: ProductRunnerDependencies):
        self.deps = deps

    def prepare_for_task15(self) -> tuple[str, ...]:
        report = check_task15_readiness(self.deps)
        if not report.ready:
            missing = ", ".join(report.missing)
            raise ProductRunnerError(f"Task 15 readiness failed: {missing}")
        state_machine = self.deps.state_machine_factory()
        observed = [state_machine.stage.value]
        for stage in _task15_pre_smoke_transitions():
            state_machine.advance_to(stage)
            observed.append(state_machine.stage.value)
        observed.append(PUBLICATION_READY)
        return tuple(observed)


@dataclass(frozen=True)
class ManualDialogSmokeInputs:
    s1_limit: int
    steady_emission_y: str
    attribution_fields: dict[str, str]
    canonical_labels: tuple[str, ...]
    sample_record_ids: dict[str, int]
    special_review_candidates: int = 0
    duplicate_review_candidates: int = 0
    excitation_review_candidates: int = 0


class ProductManualDialogFlow:
    def __init__(self, dialog_port, state_machine):
        self.dialog_port = dialog_port
        self.state_machine = state_machine

    def run(self, inputs: ManualDialogSmokeInputs) -> tuple[str, ...]:
        observed = [self.state_machine.stage.value]
        observed.extend(self.confirm_pre_extraction(inputs.s1_limit, inputs.steady_emission_y))
        observed.extend(self.confirm_post_extraction_reviews(inputs))
        observed.append(self.mark_output_staging_started())
        observed.append(self.mark_verification_started())
        observed.append(self.mark_publication_started())
        observed.extend(self.confirm_output_inspection())
        return tuple(observed)

    def confirm_pre_extraction(self, s1_limit: int, steady_emission_y: str) -> tuple[str, ...]:
        observed: list[str] = []
        self._advance(Stage.PREFLIGHT, observed)
        self._require("confirm", preflight_settings_dialog(default_s1_limit=s1_limit, steady_emission_y=steady_emission_y))
        self._advance(Stage.SAVE_AND_CLOSE_ORIGIN, observed)
        self._require("retry", save_and_close_origin_dialog())
        self._advance(Stage.EXTRACTION, observed)
        return tuple(observed)

    def confirm_post_extraction_reviews(self, inputs: ManualDialogSmokeInputs) -> tuple[str, ...]:
        observed: list[str] = []
        self._advance(Stage.ATTRIBUTION, observed)
        self._require("confirm", attribution_dialog(inputs.attribution_fields))
        self._advance_skipped_review(Stage.SPECIAL_REVIEW, inputs.special_review_candidates, observed)
        self._advance_skipped_review(Stage.DUPLICATE_REVIEW, inputs.duplicate_review_candidates, observed)
        self._advance_skipped_review(Stage.EXCITATION_SELECTION, inputs.excitation_review_candidates, observed)
        self._advance(Stage.FINAL_ATTRIBUTION_SUMMARY, observed)
        final_rows = tuple(
            FinalReviewRow(
                row_id=f"manual-smoke-{index}",
                source_filename="手工流程检查",
                folder_path="",
                book_name=label,
                attribution=label,
                result="将写入输出",
            )
            for index, label in enumerate(inputs.canonical_labels)
        )
        self._require(
            "confirm",
            final_attribution_summary_dialog(
                final_rows,
                recognized_count=len(final_rows),
                rejected_count=0,
                excluded_count=0,
                accepted_count=len(final_rows),
            ),
        )
        self._advance(Stage.SAMPLE_RECORD_COMMIT, observed)
        self.state_machine.record_sample_commit_success(inputs.sample_record_ids)
        observed.append(self.state_machine.stage.value)
        return tuple(observed)

    def mark_output_staging_started(self) -> str:
        self.state_machine.advance_to(Stage.OUTPUT_STAGING)
        return self.state_machine.stage.value

    def mark_verification_started(self) -> str:
        self.state_machine.advance_to(Stage.VERIFICATION)
        return self.state_machine.stage.value

    def mark_publication_started(self) -> str:
        self.state_machine.advance_to(Stage.PUBLICATION)
        return self.state_machine.stage.value

    def confirm_output_inspection(self) -> tuple[str, ...]:
        observed: list[str] = []
        self._advance(Stage.OUTPUT_INSPECTION, observed)
        self._require("continue", output_can_be_inspected_dialog())
        self._advance(Stage.COMPLETION, observed)
        return tuple(observed)

    def _advance(self, stage: Stage, observed: list[str]) -> None:
        self.state_machine.advance_to(stage)
        observed.append(self.state_machine.stage.value)

    def _advance_skipped_review(self, stage: Stage, candidate_count: int, observed: list[str]) -> None:
        if candidate_count != 0:
            raise ProductRunnerError(f"Manual dialog smoke cannot skip {stage.value} with {candidate_count} pending candidates")
        self.state_machine.advance_to(stage)
        observed.append(f"{self.state_machine.stage.value}_skipped:0")

    def _require(self, expected_action: str, request) -> None:
        if expected_action == "confirm" and not request.can_confirm:
            raise ProductRunnerError(f"Manual dialog {request.kind} cannot confirm invalid input")
        response = self.dialog_port.choose(request)
        if response.action != expected_action:
            raise ProductRunnerError(f"Manual dialog {request.kind} returned {response.action}; expected {expected_action}")


def assert_ready_for_task15(deps: ProductRunnerDependencies | None = None) -> tuple[str, ...]:
    return ProductWorkflowRunner(deps or build_default_product_dependencies()).prepare_for_task15()


def build_default_product_dependencies() -> ProductRunnerDependencies:
    from spectrum_organizer.origin.extract_worker import build_origin_extraction_worker_factory
    from spectrum_organizer.origin.output_worker import run_output_worker
    from spectrum_organizer.origin.verify_worker import run_verifier_worker
    from spectrum_organizer.reporting.publication import create_run_staging, publish_completed_run
    from spectrum_organizer.reporting.run_report import build_success_report
    from spectrum_organizer.ui.dialog_port import QtManualDialogPort
    from spectrum_organizer.ui.state_machine import TaskStateMachine

    return ProductRunnerDependencies(
        manual_dialog_port=QtManualDialogPort(),
        extraction_worker_factory=build_origin_extraction_worker_factory,
        output_worker=run_output_worker,
        verifier_worker=run_verifier_worker,
        create_staging=create_run_staging,
        publish_run=publish_completed_run,
        report_builder=build_success_report,
        protected_path_audit_hook=ProtectedPathAuditHook(),
        final_process_count_hook=FinalProcessCountHook(_default_origin_process_probe),
        state_machine_factory=TaskStateMachine,
        mode="book_only",
    )


def _task15_pre_smoke_transitions() -> tuple[Stage, ...]:
    return (
        Stage.PREFLIGHT,
        Stage.SAVE_AND_CLOSE_ORIGIN,
        Stage.EXTRACTION,
        Stage.ATTRIBUTION,
        Stage.SPECIAL_REVIEW,
        Stage.DUPLICATE_REVIEW,
        Stage.EXCITATION_SELECTION,
        Stage.FINAL_ATTRIBUTION_SUMMARY,
        Stage.SAMPLE_RECORD_COMMIT,
        Stage.APPROVED_SNAPSHOT,
        Stage.OUTPUT_STAGING,
        Stage.VERIFICATION,
        Stage.PUBLICATION,
    )


def _dialog_port_ready(port: object | None) -> bool:
    return port is not None and callable(getattr(port, "choose", None))


def _worker_factory_ready(factory: object | None) -> bool:
    return factory is not None and (callable(factory) or callable(getattr(factory, "create", None)))


def _audit_hook_ready(hook: object | None, readiness_kind: str) -> bool:
    if readiness_kind == "protected_path_audit":
        return isinstance(hook, ProtectedPathAuditHook) and isinstance(hook(), ProtectedPathAuditPlan)
    if readiness_kind == "final_process_count":
        if not isinstance(hook, FinalProcessCountHook) or not callable(hook.process_probe):
            return False
        try:
            return hook() == hook.expected_origin_process_count
        except ProductRunnerError:
            return False
    return False


def _default_origin_process_probe(*, timeout: float = 5.0) -> tuple[str, ...]:
    command = (
        "$items=Get-Process | "
        "Where-Object { $_.ProcessName -like 'Origin*' -or $_.ProcessName -like 'Procmon*' } | "
        "ForEach-Object { '{0}:{1}' -f $_.ProcessName,$_.Id }; "
        "$items"
    )
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as exc:
        raise ProductRunnerError("final process-count probe timed out") from exc
    if completed.returncode != 0:
        message = completed.stderr.strip() or "process query failed"
        raise ProductRunnerError(message)
    return tuple(line.strip() for line in completed.stdout.splitlines() if line.strip())
