from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from spectrum_organizer.ui.dialogs import DialogRequest, cancelled_and_exited_dialog


class Stage(Enum):
    SOURCE_SELECTION = "source_selection"
    PREFLIGHT = "preflight"
    SAVE_AND_CLOSE_ORIGIN = "save_and_close_origin"
    EXTRACTION = "extraction"
    ATTRIBUTION = "attribution"
    SPECIAL_REVIEW = "special_review"
    DUPLICATE_REVIEW = "duplicate_review"
    EXCITATION_SELECTION = "excitation_selection"
    FINAL_ATTRIBUTION_SUMMARY = "final_attribution_summary"
    SAMPLE_RECORD_COMMIT = "sample_record_commit"
    APPROVED_SNAPSHOT = "approved_snapshot"
    OUTPUT_STAGING = "output_staging"
    VERIFICATION = "verification"
    PUBLICATION = "publication"
    OUTPUT_INSPECTION = "output_inspection"
    COMPLETION = "completion"
    FAILED = "failed"
    CANCELLED = "cancelled"


class CancellationDecision(Enum):
    CONTINUE = "continue"
    CLOSED = "closed"
    CANCEL_AND_EXIT = "cancel_and_exit"


_ALLOWED_FORWARD = {
    Stage.SOURCE_SELECTION: {Stage.PREFLIGHT, Stage.CANCELLED, Stage.FAILED},
    Stage.PREFLIGHT: {Stage.SAVE_AND_CLOSE_ORIGIN, Stage.CANCELLED, Stage.FAILED},
    Stage.SAVE_AND_CLOSE_ORIGIN: {Stage.EXTRACTION, Stage.CANCELLED, Stage.FAILED},
    Stage.EXTRACTION: {Stage.ATTRIBUTION, Stage.CANCELLED, Stage.FAILED},
    Stage.ATTRIBUTION: {Stage.SPECIAL_REVIEW, Stage.CANCELLED, Stage.FAILED},
    Stage.SPECIAL_REVIEW: {Stage.DUPLICATE_REVIEW, Stage.ATTRIBUTION, Stage.CANCELLED, Stage.FAILED},
    Stage.DUPLICATE_REVIEW: {Stage.EXCITATION_SELECTION, Stage.ATTRIBUTION, Stage.CANCELLED, Stage.FAILED},
    Stage.EXCITATION_SELECTION: {Stage.FINAL_ATTRIBUTION_SUMMARY, Stage.ATTRIBUTION, Stage.CANCELLED, Stage.FAILED},
    Stage.FINAL_ATTRIBUTION_SUMMARY: {Stage.SAMPLE_RECORD_COMMIT, Stage.ATTRIBUTION, Stage.CANCELLED, Stage.FAILED},
    Stage.SAMPLE_RECORD_COMMIT: {Stage.APPROVED_SNAPSHOT, Stage.FINAL_ATTRIBUTION_SUMMARY, Stage.CANCELLED, Stage.FAILED},
    Stage.APPROVED_SNAPSHOT: {Stage.OUTPUT_STAGING, Stage.CANCELLED, Stage.FAILED},
    Stage.OUTPUT_STAGING: {Stage.VERIFICATION, Stage.CANCELLED, Stage.FAILED},
    Stage.VERIFICATION: {Stage.PUBLICATION, Stage.CANCELLED, Stage.FAILED},
    Stage.OUTPUT_INSPECTION: {Stage.COMPLETION, Stage.CANCELLED, Stage.FAILED},
    Stage.PUBLICATION: {Stage.OUTPUT_INSPECTION, Stage.CANCELLED, Stage.FAILED},
}


@dataclass
class TaskStateMachine:
    stage: Stage = Stage.SOURCE_SELECTION
    cancel_reason: str | None = None
    last_error: str | None = None
    reopened_attribution_keys: tuple[str, ...] = ()
    special_groups: dict = field(default_factory=dict)
    duplicate_choices: dict = field(default_factory=dict)
    excitation_pairing: dict = field(default_factory=dict)
    completeness: dict = field(default_factory=dict)
    approved_snapshot: object | None = None
    output_model: object | None = None
    sample_record_ids: dict[str, int] = field(default_factory=dict)
    output_published: bool = False

    def advance_to(self, stage: Stage) -> None:
        allowed = _ALLOWED_FORWARD.get(self.stage, set())
        if stage not in allowed:
            raise ValueError(f"Cannot advance from {self.stage.value} to {stage.value}")
        self.stage = stage

    def cancel(self, reason: str) -> None:
        self.advance_to(Stage.CANCELLED)
        self.cancel_reason = reason
        self.output_published = False

    def resolve_cancel_confirmation(self, decision: CancellationDecision) -> DialogRequest | None:
        if decision in {CancellationDecision.CONTINUE, CancellationDecision.CLOSED}:
            return None
        if decision is CancellationDecision.CANCEL_AND_EXIT:
            self.cancel("user cancelled and exited")
            return cancelled_and_exited_dialog()
        raise ValueError(f"Unsupported cancellation decision: {decision}")

    def return_to_attribution(self, book_keys: tuple[str, ...]) -> None:
        self.advance_to(Stage.ATTRIBUTION)
        self.reopened_attribution_keys = tuple(book_keys)
        self.special_groups.clear()
        self.duplicate_choices.clear()
        self.excitation_pairing.clear()
        self.completeness.clear()
        self.approved_snapshot = None
        self.output_model = None
        self.sample_record_ids.clear()

    def record_sample_commit_failure(self, message: str) -> None:
        if self.stage is not Stage.FINAL_ATTRIBUTION_SUMMARY:
            self.advance_to(Stage.FINAL_ATTRIBUTION_SUMMARY)
        self.last_error = message

    def record_sample_commit_success(self, sample_record_ids: dict[str, int]) -> None:
        self.advance_to(Stage.APPROVED_SNAPSHOT)
        self.sample_record_ids = dict(sample_record_ids)
        self.last_error = None
