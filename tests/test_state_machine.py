import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from spectrum_organizer.ui.state_machine import CancellationDecision, Stage, TaskStateMachine


class StateMachineTests(unittest.TestCase):
    def test_stages_include_task13_workflow_order(self):
        self.assertEqual(
            [
                "source_selection",
                "preflight",
                "save_and_close_origin",
                "extraction",
                "attribution",
                "special_review",
                "duplicate_review",
                "excitation_selection",
                "final_attribution_summary",
                "sample_record_commit",
                "approved_snapshot",
                "output_staging",
                "verification",
                "publication",
                "output_inspection",
                "completion",
                "failed",
                "cancelled",
            ],
            [stage.value for stage in Stage],
        )

    def test_valid_forward_transitions_include_manual_intervention_stages(self):
        machine = TaskStateMachine()

        for stage in (
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
            Stage.OUTPUT_INSPECTION,
            Stage.COMPLETION,
        ):
            machine.advance_to(stage)
            self.assertEqual(stage, machine.stage)

    def test_cancellation_confirmation_continue_and_window_close_keep_running(self):
        for decision in (CancellationDecision.CONTINUE, CancellationDecision.CLOSED):
            with self.subTest(decision=decision):
                machine = TaskStateMachine(stage=Stage.EXTRACTION)

                final_popup = machine.resolve_cancel_confirmation(decision)

                self.assertEqual(Stage.EXTRACTION, machine.stage)
                self.assertFalse(machine.output_published)
                self.assertIsNone(final_popup)

    def test_cancellation_confirmation_cancel_exits_without_publication_and_shows_final_popup(self):
        machine = TaskStateMachine(stage=Stage.EXTRACTION)

        final_popup = machine.resolve_cancel_confirmation(CancellationDecision.CANCEL_AND_EXIT)

        self.assertEqual(Stage.CANCELLED, machine.stage)
        self.assertEqual("user cancelled and exited", machine.cancel_reason)
        self.assertFalse(machine.output_published)
        self.assertIsNotNone(final_popup)
        self.assertEqual("cancelled_and_exited", final_popup.kind)

    def test_cancellation_moves_to_cancelled_and_records_reason(self):
        machine = TaskStateMachine()

        machine.cancel("user cancelled")

        self.assertEqual(Stage.CANCELLED, machine.stage)
        self.assertEqual("user cancelled", machine.cancel_reason)

    def test_cancellation_rejects_terminal_workflow_stages(self):
        for stage in (Stage.COMPLETION, Stage.FAILED, Stage.CANCELLED):
            with self.subTest(stage=stage):
                machine = TaskStateMachine(stage=stage)

                with self.assertRaises(ValueError):
                    machine.cancel("late cancel")

                self.assertEqual(stage, machine.stage)

    def test_state_helpers_reject_invalid_source_stages(self):
        machine = TaskStateMachine(stage=Stage.SOURCE_SELECTION)
        with self.assertRaises(ValueError):
            machine.return_to_attribution(("book",))
        with self.assertRaises(ValueError):
            machine.record_sample_commit_success({"book": 1})
        machine.stage = Stage.COMPLETION
        with self.assertRaises(ValueError):
            machine.record_sample_commit_failure("database locked")

    def test_return_to_attribution_reopens_scope_and_clears_dependent_results(self):
        machine = TaskStateMachine()
        machine.stage = Stage.SPECIAL_REVIEW
        machine.special_groups = {"group": object()}
        machine.duplicate_choices = {"duplicate": object()}
        machine.excitation_pairing = {"pair": object()}
        machine.completeness = {"complete": object()}
        machine.approved_snapshot = object()
        machine.output_model = object()
        machine.sample_record_ids = {"book-a": 1}

        machine.return_to_attribution(("S1|Folder|PE1", "S1|Folder|PE2"))

        self.assertEqual(Stage.ATTRIBUTION, machine.stage)
        self.assertEqual(("S1|Folder|PE1", "S1|Folder|PE2"), machine.reopened_attribution_keys)
        self.assertEqual({}, machine.special_groups)
        self.assertEqual({}, machine.duplicate_choices)
        self.assertEqual({}, machine.excitation_pairing)
        self.assertEqual({}, machine.completeness)
        self.assertIsNone(machine.approved_snapshot)
        self.assertIsNone(machine.output_model)
        self.assertEqual({}, machine.sample_record_ids)

    def test_batch_write_failure_stays_at_final_summary_for_retry_or_cancel(self):
        machine = TaskStateMachine()
        machine.stage = Stage.FINAL_ATTRIBUTION_SUMMARY

        machine.record_sample_commit_failure("database locked")

        self.assertEqual(Stage.FINAL_ATTRIBUTION_SUMMARY, machine.stage)
        self.assertEqual("database locked", machine.last_error)

    def test_successful_final_commit_advances_to_approved_snapshot(self):
        machine = TaskStateMachine()
        machine.stage = Stage.SAMPLE_RECORD_COMMIT

        machine.record_sample_commit_success({"book-a": 1})

        self.assertEqual(Stage.APPROVED_SNAPSHOT, machine.stage)
        self.assertEqual({"book-a": 1}, machine.sample_record_ids)


if __name__ == "__main__":
    unittest.main()
