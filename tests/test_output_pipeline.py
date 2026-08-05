from datetime import datetime
import json
import os
import pathlib
import tempfile
import threading
from types import SimpleNamespace
import sys
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def output_result(*, contract="contract", attempts=()):
    return SimpleNamespace(
        contract=contract,
        attempts=attempts,
        project_artifact=object(),
    )


class OutputPipelineTests(unittest.TestCase):
    def test_runtime_audit_binds_attempt_staging_and_publication(self):
        from spectrum_organizer.runtime_audit import RUNTIME_AUDIT_DIR_ENV
        from spectrum_organizer.workflow.output_pipeline import (
            OutputPipelinePorts,
            run_output_pipeline,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            audit_dir = root / "audit"
            audit_dir.mkdir()
            output_parent = root / "output"
            output_parent.mkdir()
            temp_root = root / "task-temp"
            temp_root.mkdir()
            staging = output_parent / ".staging-run-1"
            final_dir = output_parent / "Organized_Origin_Data_20260802_120000"
            project_name = "Organized_Spectra_20260802_120000.opju"
            report_name = "Run_Report_20260802_120000.txt"
            targets = SimpleNamespace(
                run_id="run-1",
                output_parent=output_parent,
                staging_dir=staging,
                staging_project_path=staging / project_name,
                verifier_mutation_path=staging / "Verifier_Mutation.opju",
                final_run_dir=final_dir,
                final_project_path=final_dir / project_name,
                final_report_path=final_dir / report_name,
            )
            snapshot = SimpleNamespace(
                snapshot_id="approved-1",
                output_plan=object(),
                source_fingerprints_before=(),
                selected_source_fingerprints_before=(),
                task_snapshot_path=temp_root / "run.sqlite3",
            )

            def create_staging(*_args, **_kwargs):
                staging.mkdir()
                return targets

            def run_output(_request):
                targets.staging_project_path.write_bytes(b"project")
                return output_result()

            def publish(_targets, report_text, _verifier_result):
                (staging / report_name).write_text(
                    report_text,
                    encoding="utf-8",
                )
                staging.rename(final_dir)
                return SimpleNamespace(
                    output_path=final_dir,
                    project_path=targets.final_project_path,
                    report_path=targets.final_report_path,
                    post_commit_error=None,
                )

            ports = OutputPipelinePorts(
                process_gate=lambda: None,
                create_staging=create_staging,
                run_output=run_output,
                run_verifier=lambda _request: SimpleNamespace(attempts=()),
                verify_sources=lambda _expected, _cancel: (),
                build_report=lambda _request: "report",
                publish=publish,
                cleanup=lambda *_args, **_kwargs: self.fail(
                    "successful publication must not clean staging"
                ),
                write_failure=lambda _request: self.fail(
                    "successful publication must not write a failure log"
                ),
            )
            with mock.patch.dict(
                os.environ,
                {RUNTIME_AUDIT_DIR_ENV: str(audit_dir)},
            ):
                run_output_pipeline(
                    snapshot,
                    output_parent,
                    run_id="run-1",
                    ports=ports,
                    clock=lambda: datetime(2026, 8, 2, 12, 0, 0),
                    cancel_check=lambda: None,
                    progress=lambda _stage: None,
                )

            events = [
                json.loads(path.read_text(encoding="utf-8"))
                for path in sorted(audit_dir.glob("*.json"))
            ]
            by_type = {}
            for event in events:
                by_type.setdefault(event["event_type"], []).append(
                    event["payload"]
                )
            self.assertEqual([], by_type["output_stage_attempt"][0]["output_parent_entries_before"])
            self.assertEqual(
                str(staging),
                by_type["output_staging_created"][0]["staging_dir"],
            )
            self.assertEqual(
                str(targets.verifier_mutation_path),
                by_type["output_staging_created"][0][
                    "verifier_mutation_path"
                ],
            )
            self.assertIn(
                "verify_output",
                [item["stage"] for item in by_type["output_stage_progress"]],
            )
            publication = by_type["publication_committed"][0]
            self.assertEqual("approved-1", publication["approved_snapshot_id"])
            self.assertEqual("run-1", publication["run_id"])
            self.assertEqual(str(final_dir), publication["final_run_dir"])
            self.assertEqual(2, len(publication["artifacts"]))

    def test_cancel_during_precommit_publication_returns_immediately_and_prevents_publish(self):
        from spectrum_organizer.workflow.output_pipeline import (
            OutputPipelineJob,
            OutputPipelinePorts,
            OutputStageRequest,
        )

        publish_started = threading.Event()
        release_publish = threading.Event()
        cancel_finished = threading.Event()
        cancelled_workers = []
        cleanup_calls = []

        def publish(targets, report, _verifier_result, *, commit):
            publish_started.set()
            release_publish.wait(2)
            return commit(lambda: self.fail("cancelled precommit must not publish"))

        job = OutputPipelineJob(
            ports=OutputPipelinePorts(
                process_gate=lambda: None,
                create_staging=lambda *args, **kwargs: SimpleNamespace(
                    staging_dir=pathlib.Path("owned-staging")
                ),
                run_output=lambda request: output_result(),
                run_verifier=lambda request: SimpleNamespace(attempts=()),
                verify_sources=lambda expected, cancel_check: (),
                build_report=lambda request: "report",
                publish=publish,
                cleanup=lambda paths, *, run_id: cleanup_calls.append((paths, run_id)),
                write_failure=lambda request: None,
                cancel_workers=lambda: cancelled_workers.append(True),
            ),
            clock=lambda: datetime(2026, 8, 2, 12, 0, 0),
            run_id_factory=lambda: "run-1",
        )
        errors = []
        run_thread = threading.Thread(
            target=lambda: self._capture_error(
                errors,
                lambda: job(
                    OutputStageRequest(
                        SimpleNamespace(
                            snapshot_id="approved-1",
                            output_plan=object(),
                            source_fingerprints_before=(),
                        ),
                        pathlib.Path("output-parent"),
                    )
                ),
            )
        )
        run_thread.start()
        self.assertTrue(publish_started.wait(2))
        cancel_thread = threading.Thread(
            target=lambda: (job.cancel(), cancel_finished.set())
        )
        cancel_thread.start()
        self.assertTrue(cancel_finished.wait(0.2))

        release_publish.set()
        run_thread.join(2)
        cancel_thread.join(2)

        self.assertTrue(cancel_finished.is_set())
        self.assertEqual([True], cancelled_workers)
        self.assertEqual("OutputPipelineCancelled", type(errors[0]).__name__)
        self.assertEqual(1, len(cleanup_calls))

    def test_cancel_during_atomic_commit_returns_immediately_without_relabeling_success(self):
        from spectrum_organizer.workflow.output_pipeline import (
            OutputPipelineJob,
            OutputPipelinePorts,
            OutputStageRequest,
        )

        commit_started = threading.Event()
        release_commit = threading.Event()
        cancel_result = []
        completion = object()

        def publish(_targets, _report, _verifier_result, *, commit):
            def atomic_rename():
                commit_started.set()
                release_commit.wait(2)
                return completion

            return commit(atomic_rename)

        job = OutputPipelineJob(
            ports=OutputPipelinePorts(
                process_gate=lambda: None,
                create_staging=lambda *args, **kwargs: SimpleNamespace(
                    staging_dir=pathlib.Path("owned-staging")
                ),
                run_output=lambda request: output_result(),
                run_verifier=lambda request: SimpleNamespace(attempts=()),
                verify_sources=lambda expected, cancel_check: (),
                build_report=lambda request: "report",
                publish=publish,
                cleanup=lambda paths, *, run_id: self.fail("committed output must not be cleaned"),
                write_failure=lambda request: self.fail("committed output must not fail"),
            ),
            clock=lambda: datetime(2026, 8, 2, 12, 0, 0),
            run_id_factory=lambda: "run-1",
        )
        results = []
        thread = threading.Thread(
            target=lambda: results.append(
                job(
                    OutputStageRequest(
                        SimpleNamespace(
                            snapshot_id="approved-1",
                            output_plan=object(),
                            source_fingerprints_before=(),
                        ),
                        pathlib.Path("output-parent"),
                    )
                )
            )
        )
        thread.start()
        self.assertTrue(commit_started.wait(2))

        cancel_thread = threading.Thread(
            target=lambda: cancel_result.append(job.cancel())
        )
        cancel_thread.start()
        cancel_thread.join(0.2)

        self.assertFalse(cancel_thread.is_alive())
        self.assertEqual([False], cancel_result)
        self.assertTrue(job.committed)
        release_commit.set()
        thread.join(2)
        self.assertIs(completion, results[0].completion)

    def test_commit_ownership_is_visible_before_the_commit_gate_is_entered(self):
        from spectrum_organizer.workflow.output_pipeline import (
            OutputPipelineJob,
            OutputPipelinePorts,
        )

        job = OutputPipelineJob(
            ports=OutputPipelinePorts(
                process_gate=lambda: None,
                create_staging=lambda *args, **kwargs: None,
                run_output=lambda request: None,
                run_verifier=lambda request: None,
                verify_sources=lambda expected, cancel_check: (),
                build_report=lambda request: "report",
                publish=lambda *args, **kwargs: None,
                cleanup=lambda *args, **kwargs: None,
                write_failure=lambda request: None,
            ),
            clock=lambda: None,
        )
        gate_requested = threading.Event()
        release_gate = threading.Event()

        class DelayedGate:
            def __enter__(self):
                gate_requested.set()
                release_gate.wait(2)

            def acquire(self, blocking=True):
                del blocking
                return False

            def release(self):
                raise AssertionError("unacquired gate must not be released")

            def __exit__(self, *_args):
                return False

        job._commit_lock = DelayedGate()
        results = []
        thread = threading.Thread(
            target=lambda: results.append(
                job._commit_publication(lambda: "done")
            )
        )
        thread.start()
        self.assertTrue(gate_requested.wait(2))

        self.assertTrue(job.committed)
        self.assertFalse(job.cancel())

        release_gate.set()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(["done"], results)

    def test_cancelled_pipeline_retains_cleanup_evidence_on_the_cancellation_error(self):
        from spectrum_organizer.workflow.output_pipeline import (
            OutputPipelineCancelled,
            OutputPipelinePorts,
            run_output_pipeline,
        )

        targets = SimpleNamespace(staging_dir=pathlib.Path("owned-staging"))
        calls = 0

        def cancel_check():
            nonlocal calls
            calls += 1
            if calls == 3:
                raise OutputPipelineCancelled("cancelled")

        retained = SimpleNamespace(
            retained_unknown=(pathlib.Path("owned-staging/unknown.bin"),)
        )
        with self.assertRaises(OutputPipelineCancelled) as raised:
            run_output_pipeline(
                SimpleNamespace(
                    snapshot_id="approved-1",
                    output_plan=object(),
                    source_fingerprints_before=(),
                ),
                pathlib.Path("output-parent"),
                run_id="run-1",
                ports=OutputPipelinePorts(
                    process_gate=lambda: None,
                    create_staging=lambda *args, **kwargs: targets,
                    run_output=lambda request: self.fail("cancel first"),
                    run_verifier=lambda request: None,
                    verify_sources=lambda expected, cancel_check: (),
                    build_report=lambda request: "report",
                    publish=lambda targets, report, verifier: None,
                    cleanup=lambda paths, *, run_id: retained,
                    write_failure=lambda request: None,
                ),
                clock=lambda: datetime(2026, 8, 2, 12, 0, 0),
                cancel_check=cancel_check,
                progress=lambda stage: None,
            )

        self.assertIs(retained, raised.exception.cleanup_result)
        self.assertIsNone(raised.exception.cleanup_error)

    def test_job_cleanup_retry_reclaims_workers_before_owned_staging(self):
        from spectrum_organizer.workflow.output_pipeline import (
            OutputPipelineFailure,
            OutputPipelineJob,
            OutputPipelinePorts,
            OutputStageRequest,
        )

        targets = SimpleNamespace(staging_dir=pathlib.Path("owned-staging"))
        retained = SimpleNamespace(
            retained_unknown=(pathlib.Path("owned-staging/unknown.bin"),)
        )
        clean = SimpleNamespace(retained_unknown=())
        cleanup_results = iter((retained, clean))
        events = []
        job = OutputPipelineJob(
            ports=OutputPipelinePorts(
                process_gate=lambda: None,
                create_staging=lambda *args, **kwargs: targets,
                run_output=lambda request: (_ for _ in ()).throw(
                    RuntimeError("writer failed")
                ),
                run_verifier=lambda request: None,
                verify_sources=lambda expected, cancel_check: (),
                build_report=lambda request: "report",
                publish=lambda targets, report, verifier: None,
                cleanup=lambda paths, *, run_id: (
                    events.append("staging"),
                    next(cleanup_results),
                )[1],
                write_failure=lambda request: None,
                retry_workers=lambda: events.append("workers"),
            ),
            clock=lambda: datetime(2026, 8, 2, 12, 0, 0),
            run_id_factory=lambda: "run-1",
        )

        with self.assertRaises(OutputPipelineFailure):
            job(
                OutputStageRequest(
                    SimpleNamespace(
                        snapshot_id="approved-1",
                        output_plan=object(),
                        source_fingerprints_before=(),
                    ),
                    pathlib.Path("output-parent"),
                )
            )

        result = job.retry_cleanup()

        self.assertIs(clean, result)
        self.assertEqual(["staging", "workers", "staging"], events)

    @staticmethod
    def _capture_error(errors, operation):
        try:
            operation()
        except BaseException as exc:
            errors.append(exc)

    def test_output_pipeline_job_delegates_progress_and_owned_worker_cancellation(self):
        from spectrum_organizer.workflow.output_pipeline import (
            OutputPipelineCancelled,
            OutputPipelineJob,
            OutputPipelinePorts,
            OutputStageRequest,
        )

        events = []
        ports = OutputPipelinePorts(
            process_gate=lambda: events.append("process_gate"),
            create_staging=lambda *args, **kwargs: self.fail("cancelled job must not stage"),
            run_output=lambda request: None,
            run_verifier=lambda request: None,
            verify_sources=lambda expected, cancel_check: (),
            build_report=lambda request: "report",
            publish=lambda targets, report, verifier: None,
            cleanup=lambda paths, *, run_id: None,
            write_failure=lambda request: None,
            reset_workers=lambda: events.append("reset_workers"),
            cancel_workers=lambda: events.append("cancel_workers"),
        )
        job = OutputPipelineJob(
            ports=ports,
            clock=lambda: datetime(2026, 8, 2, 12, 0, 0),
            run_id_factory=lambda: "run-1",
        )
        job.set_progress_callback(lambda stage: events.append(("progress", stage)))

        job.prepare()
        job.cancel()

        with self.assertRaises(OutputPipelineCancelled):
            job(
                OutputStageRequest(
                    SimpleNamespace(
                        snapshot_id="approved-1",
                        output_plan=object(),
                        source_fingerprints_before=(),
                    ),
                    pathlib.Path("output-parent"),
                )
            )

        self.assertEqual(
            ["reset_workers", "cancel_workers"],
            events,
        )

    def test_cancel_worker_termination_failure_is_reported_by_the_terminal_cancellation(self):
        from spectrum_organizer.workflow.output_pipeline import (
            OutputPipelineCancelled,
            OutputPipelineJob,
            OutputPipelinePorts,
            OutputStageRequest,
        )

        job = OutputPipelineJob(
            ports=OutputPipelinePorts(
                process_gate=lambda: None,
                create_staging=lambda *args, **kwargs: self.fail(
                    "cancelled job must not stage"
                ),
                run_output=lambda request: None,
                run_verifier=lambda request: None,
                verify_sources=lambda expected, cancel_check: (),
                build_report=lambda request: "report",
                publish=lambda *args, **kwargs: None,
                cleanup=lambda *args, **kwargs: None,
                write_failure=lambda request: None,
                cancel_workers=lambda: (_ for _ in ()).throw(
                    OSError("Job termination failed")
                ),
            ),
            clock=lambda: None,
        )

        self.assertTrue(job.cancel())
        with self.assertRaises(OutputPipelineCancelled) as raised:
            job(
                OutputStageRequest(
                    SimpleNamespace(
                        snapshot_id="approved-1",
                        output_plan=object(),
                        source_fingerprints_before=(),
                    ),
                    pathlib.Path("output-parent"),
                )
            )

        self.assertIn(
            "Job termination failed",
            "\n".join(getattr(raised.exception, "__notes__", ())),
        )

    def test_approved_snapshot_protocol_accepts_fake_and_task8_concrete_snapshot_without_importing_it_in_workflow(self):
        from spectrum_organizer.product_runner import ApprovedOutputSnapshot
        from spectrum_organizer.workflow.output_pipeline import ApprovedSnapshotView

        fake = SimpleNamespace(
            snapshot_id="fake",
            output_plan=object(),
            source_fingerprints_before=(),
            selected_source_fingerprints_before=(),
            task_snapshot_path=pathlib.Path("fake-task.json"),
            task_temp_root_identity=(101, 202),
        )
        missing_temp_identity = SimpleNamespace(
            snapshot_id="missing-temp-identity",
            output_plan=object(),
            source_fingerprints_before=(),
            selected_source_fingerprints_before=(),
            task_snapshot_path=pathlib.Path("fake-task.json"),
        )
        incomplete = SimpleNamespace(
            snapshot_id="incomplete",
            output_plan=object(),
            source_fingerprints_before=(),
            selected_source_fingerprints_before=(),
        )
        concrete = ApprovedOutputSnapshot(
            snapshot_id="approved",
            task_snapshot_sha256="0" * 64,
            recognized_book_keys=(),
            accepted_spectra=(),
            rejections=(),
            exclusions=(),
            attributions=(),
            review_requirements=(),
            review_choices=(),
            output_plan=object(),
            source_fingerprints_before=(),
            source_fingerprints_after=(),
            count_reconciliation=object(),
            recognized_books=(),
            approved_sources=(),
            source_ids=(),
            task_snapshot_path=pathlib.Path("task.json"),
            task_temp_root_identity=(101, 202),
        )

        self.assertIsInstance(fake, ApprovedSnapshotView)
        self.assertIsInstance(concrete, ApprovedSnapshotView)
        self.assertNotIsInstance(incomplete, ApprovedSnapshotView)
        self.assertNotIsInstance(
            missing_temp_identity,
            ApprovedSnapshotView,
        )

    def test_success_runs_safety_write_verify_source_check_report_and_atomic_commit_in_order(self):
        from spectrum_organizer.workflow.output_pipeline import (
            OutputPipelinePorts,
            run_output_pipeline,
        )

        events = []
        recognized_before = (
            SimpleNamespace(path=pathlib.Path("source-a.opju")),
        )
        selected_before = (
            *recognized_before,
            SimpleNamespace(path=pathlib.Path("skipped-source.opju")),
        )
        snapshot = SimpleNamespace(
            snapshot_id="approved-1",
            output_plan=object(),
            source_fingerprints_before=recognized_before,
            selected_source_fingerprints_before=selected_before,
        )
        targets = SimpleNamespace(
            staging_dir=pathlib.Path("staging"),
            staging_project_path=pathlib.Path("staging/Organized_Spectra_20260802_120000.opju"),
            final_run_dir=pathlib.Path("Organized_Origin_Data_20260802_120000"),
        )
        write_result = output_result(
            contract="write-contract",
            attempts=("write-1",),
        )
        verify_result = SimpleNamespace(attempts=("verify-1",))
        completion = SimpleNamespace(output_path=targets.final_run_dir)

        def create_staging(output_parent, timestamp, *, run_id):
            events.append(("create_staging", output_parent, timestamp, run_id))
            return targets

        def run_output(request):
            events.append(("run_output", request.approved_snapshot, request.targets))
            return write_result

        def run_verifier(request):
            events.append(("run_verifier", request.expected_contract, request.targets))
            return verify_result

        def verify_sources(expected, cancel_check):
            events.append(("verify_sources", expected))
            cancel_check()
            return selected_before

        def build_report(request):
            events.append(("build_report", request.source_fingerprints_after))
            return "完整报告"

        def publish(run_targets, report_text, _verifier_result):
            events.append(("publish", run_targets, report_text))
            return completion

        ports = OutputPipelinePorts(
            process_gate=lambda: events.append("process_gate"),
            create_staging=create_staging,
            run_output=run_output,
            run_verifier=run_verifier,
            verify_sources=verify_sources,
            build_report=build_report,
            publish=publish,
            cleanup=lambda paths, *, run_id: events.append(("cleanup", paths, run_id)),
            write_failure=lambda request: events.append(("write_failure", request)),
        )

        result = run_output_pipeline(
            snapshot,
            pathlib.Path("output-parent"),
            run_id="run-1",
            ports=ports,
            clock=lambda: datetime(2026, 8, 2, 12, 0, 0),
            cancel_check=lambda: None,
            progress=lambda stage: events.append(("progress", stage)),
        )

        self.assertIs(completion, result.completion)
        self.assertEqual(selected_before, result.source_fingerprints_after)
        self.assertEqual(("write-1",), result.output_attempts)
        self.assertEqual(("verify-1",), result.verifier_attempts)
        self.assertEqual(
            [
                ("progress", "process_gate"),
                "process_gate",
                ("progress", "create_staging"),
                (
                    "create_staging",
                    pathlib.Path("output-parent"),
                    "20260802_120000",
                    "run-1",
                ),
                ("progress", "write_output"),
                ("run_output", snapshot, targets),
                ("progress", "verify_output"),
                ("run_verifier", "write-contract", targets),
                ("progress", "verify_sources"),
                ("verify_sources", selected_before),
                ("progress", "build_report"),
                ("build_report", selected_before),
                ("progress", "publish"),
                ("publish", targets, "完整报告"),
                ("progress", "committed"),
                ("progress", "complete"),
            ],
            events,
        )

    def test_changed_source_after_output_verification_cleans_owned_staging_and_never_builds_or_publishes(self):
        from spectrum_organizer.workflow.output_pipeline import (
            OutputPipelineFailure,
            OutputPipelinePorts,
            run_output_pipeline,
        )

        events = []
        before = (SimpleNamespace(path=pathlib.Path("source-a.opju"), sha256="before"),)
        after = (SimpleNamespace(path=pathlib.Path("source-a.opju"), sha256="after"),)
        snapshot = SimpleNamespace(
            snapshot_id="approved-1",
            output_plan=object(),
            source_fingerprints_before=before,
        )
        targets = SimpleNamespace(staging_dir=pathlib.Path("owned-staging"))
        write_result = output_result(
            contract="write-contract",
            attempts=("write-1",),
        )
        verify_result = SimpleNamespace(attempts=("verify-1",))

        ports = OutputPipelinePorts(
            process_gate=lambda: events.append("process_gate"),
            create_staging=lambda *args, **kwargs: events.append("create_staging") or targets,
            run_output=lambda request: events.append("run_output") or write_result,
            run_verifier=lambda request: events.append("run_verifier") or verify_result,
            verify_sources=lambda expected, cancel_check: events.append("verify_sources") or after,
            build_report=lambda request: events.append("build_report") or "report",
            publish=lambda run_targets, report, verifier: events.append("publish"),
            cleanup=lambda paths, *, run_id: events.append(("cleanup", paths, run_id)) or "cleanup-result",
            write_failure=lambda request: events.append(("write_failure", request)) or pathlib.Path("Failed_Run.txt"),
        )

        with self.assertRaises(OutputPipelineFailure) as raised:
            run_output_pipeline(
                snapshot,
                pathlib.Path("output-parent"),
                run_id="run-1",
                ports=ports,
                clock=lambda: datetime(2026, 8, 2, 12, 0, 0),
                cancel_check=lambda: None,
                progress=lambda stage: None,
            )

        self.assertEqual("verify_sources", raised.exception.stage)
        self.assertIn("source-a.opju", str(raised.exception.cause))
        self.assertIn("sha256", str(raised.exception.cause))
        self.assertIn("before", str(raised.exception.cause))
        self.assertIn("after", str(raised.exception.cause))
        self.assertEqual(pathlib.Path("Failed_Run.txt"), raised.exception.failure_log_path)
        self.assertEqual("cleanup-result", raised.exception.cleanup_result)
        self.assertEqual(
            (
                "process_gate",
                "create_staging",
                "run_output",
                "run_verifier",
                "verify_sources",
                ("cleanup", (targets,), "run-1"),
                "write_failure",
            ),
            tuple(item[0] if isinstance(item, tuple) and item[0] == "write_failure" else item for item in events),
        )
        self.assertNotIn("build_report", events)
        self.assertNotIn("publish", events)
        failure_request = next(item[1] for item in events if isinstance(item, tuple) and item[0] == "write_failure")
        self.assertEqual("verify_sources", failure_request.stage)
        self.assertEqual(("write-1",), failure_request.output_attempts)
        self.assertEqual(("verify-1",), failure_request.verifier_attempts)

    def test_cancellation_before_verification_cleans_staging_without_failure_log_or_publication(self):
        from spectrum_organizer.workflow.output_pipeline import (
            OutputPipelineCancelled,
            OutputPipelinePorts,
            run_output_pipeline,
        )

        events = []
        snapshot = SimpleNamespace(
            snapshot_id="approved-1",
            output_plan=object(),
            source_fingerprints_before=(),
        )
        targets = SimpleNamespace(staging_dir=pathlib.Path("owned-staging"))
        cancel_calls = 0

        def cancel_check():
            nonlocal cancel_calls
            cancel_calls += 1
            if cancel_calls == 4:
                raise OutputPipelineCancelled("cancelled by user")

        ports = OutputPipelinePorts(
            process_gate=lambda: events.append("process_gate"),
            create_staging=lambda *args, **kwargs: events.append("create_staging") or targets,
            run_output=lambda request: events.append("run_output") or output_result(
                contract="write-contract",
            ),
            run_verifier=lambda request: events.append("run_verifier"),
            verify_sources=lambda expected, cancel_check: events.append("verify_sources") or (),
            build_report=lambda request: events.append("build_report") or "report",
            publish=lambda run_targets, report, verifier: events.append("publish"),
            cleanup=lambda paths, *, run_id: events.append(("cleanup", paths, run_id)),
            write_failure=lambda request: events.append("write_failure"),
        )

        with self.assertRaises(OutputPipelineCancelled):
            run_output_pipeline(
                snapshot,
                pathlib.Path("output-parent"),
                run_id="run-1",
                ports=ports,
                clock=lambda: datetime(2026, 8, 2, 12, 0, 0),
                cancel_check=cancel_check,
                progress=lambda stage: None,
            )

        self.assertEqual(
            [
                "process_gate",
                "create_staging",
                "run_output",
                ("cleanup", (targets,), "run-1"),
            ],
            events,
        )

    def test_cleanup_failure_does_not_mask_pipeline_cancellation(self):
        from spectrum_organizer.workflow.output_pipeline import (
            OutputPipelineCancelled,
            OutputPipelinePorts,
            run_output_pipeline,
        )

        targets = SimpleNamespace(
            staging_dir=pathlib.Path("owned-staging")
        )
        cancel_calls = 0

        def cancel_check():
            nonlocal cancel_calls
            cancel_calls += 1
            if cancel_calls == 3:
                raise OutputPipelineCancelled("cancelled by user")

        with self.assertRaises(OutputPipelineCancelled) as raised:
            run_output_pipeline(
                SimpleNamespace(
                    snapshot_id="approved-1",
                    output_plan=object(),
                    source_fingerprints_before=(),
                ),
                pathlib.Path("output-parent"),
                run_id="run-1",
                ports=OutputPipelinePorts(
                    process_gate=lambda: None,
                    create_staging=lambda *args, **kwargs: targets,
                    run_output=lambda request: self.fail(
                        "cancel must stop before output"
                    ),
                    run_verifier=lambda request: None,
                    verify_sources=lambda expected, cancel_check: (),
                    build_report=lambda request: "report",
                    publish=lambda run_targets, report, verifier: None,
                    cleanup=lambda paths, *, run_id: (_ for _ in ()).throw(
                        OSError("cleanup blocked")
                    ),
                    write_failure=lambda request: self.fail(
                        "cancellation must not write a failure log"
                    ),
                ),
                clock=lambda: datetime(2026, 8, 2, 12, 0, 0),
                cancel_check=cancel_check,
                progress=lambda stage: None,
            )

        self.assertEqual("cancelled by user", str(raised.exception))
        self.assertIn(
            "staging cleanup also failed: cleanup blocked",
            getattr(raised.exception, "__notes__", ()),
        )

    def test_committed_success_is_returned_when_completion_notification_fails(self):
        from spectrum_organizer.workflow.output_pipeline import (
            OutputPipelinePorts,
            run_output_pipeline,
        )

        snapshot = SimpleNamespace(
            snapshot_id="approved-1",
            output_plan=object(),
            source_fingerprints_before=(),
        )
        targets = SimpleNamespace(staging_dir=pathlib.Path("owned-staging"))
        completion = object()

        def progress(stage):
            if stage == "complete":
                raise RuntimeError("UI callback failed after commit")

        result = run_output_pipeline(
            snapshot,
            pathlib.Path("output-parent"),
            run_id="run-1",
            ports=OutputPipelinePorts(
                process_gate=lambda: None,
                create_staging=lambda *args, **kwargs: targets,
                run_output=lambda request: output_result(),
                run_verifier=lambda request: SimpleNamespace(attempts=()),
                verify_sources=lambda expected, cancel_check: (),
                build_report=lambda request: "report",
                publish=lambda run_targets, report, verifier: completion,
                cleanup=lambda paths, *, run_id: self.fail("committed output must not be cleaned"),
                write_failure=lambda request: self.fail("committed output must not become a failed run"),
            ),
            clock=lambda: datetime(2026, 8, 2, 12, 0, 0),
            cancel_check=lambda: None,
            progress=progress,
        )

        self.assertIs(completion, result.completion)
        self.assertIsInstance(result.post_commit_error, RuntimeError)
        self.assertIn("UI callback failed", str(result.post_commit_error))

    def test_publication_cleanup_warning_keeps_committed_success(self):
        from spectrum_organizer.workflow.output_pipeline import (
            OutputPipelinePorts,
            run_output_pipeline,
        )

        warning = OSError("ownership sidecar cleanup blocked")
        completion = SimpleNamespace(post_commit_error=warning)
        snapshot = SimpleNamespace(
            snapshot_id="approved-1",
            output_plan=object(),
            source_fingerprints_before=(),
        )

        result = run_output_pipeline(
            snapshot,
            pathlib.Path("output-parent"),
            run_id="run-1",
            ports=OutputPipelinePorts(
                process_gate=lambda: None,
                create_staging=lambda *args, **kwargs: SimpleNamespace(
                    staging_dir=pathlib.Path("owned-staging")
                ),
                run_output=lambda request: output_result(),
                run_verifier=lambda request: SimpleNamespace(attempts=()),
                verify_sources=lambda expected, cancel_check: (),
                build_report=lambda request: "report",
                publish=lambda targets, report, verifier: completion,
                cleanup=lambda paths, *, run_id: self.fail(
                    "committed output must not be cleaned"
                ),
                write_failure=lambda request: self.fail(
                    "committed output must not become a failed run"
                ),
            ),
            clock=lambda: datetime(2026, 8, 2, 12, 0, 0),
            cancel_check=lambda: None,
            progress=lambda stage: None,
        )

        self.assertIs(completion, result.completion)
        self.assertIs(warning, result.post_commit_error)

    def test_later_post_commit_failures_do_not_replace_first_cleanup_error(self):
        from spectrum_organizer.workflow.output_pipeline import (
            OutputPipelinePorts,
            run_output_pipeline,
        )

        first_error = OSError("ownership sidecar cleanup blocked")
        completion = SimpleNamespace(post_commit_error=first_error)
        targets = SimpleNamespace(
            staging_dir=pathlib.Path("owned-staging")
        )

        def progress(stage):
            if stage == "committed":
                raise RuntimeError("committed notification failed")
            if stage == "complete":
                raise RuntimeError("completion notification failed")

        result = run_output_pipeline(
            SimpleNamespace(
                snapshot_id="approved-1",
                output_plan=object(),
                source_fingerprints_before=(),
            ),
            pathlib.Path("output-parent"),
            run_id="run-1",
            ports=OutputPipelinePorts(
                process_gate=lambda: None,
                create_staging=lambda *args, **kwargs: targets,
                run_output=lambda request: output_result(),
                run_verifier=lambda request: SimpleNamespace(attempts=()),
                verify_sources=lambda expected, cancel_check: (),
                build_report=lambda request: "report",
                publish=lambda run_targets, report, verifier: completion,
                cleanup=lambda paths, *, run_id: self.fail(
                    "committed output must not be cleaned"
                ),
                write_failure=lambda request: self.fail(
                    "committed output must not become a failed run"
                ),
                post_commit=lambda snapshot, published: (_ for _ in ()).throw(
                    OSError("task temp cleanup failed")
                ),
            ),
            clock=lambda: datetime(2026, 8, 2, 12, 0, 0),
            cancel_check=lambda: None,
            progress=progress,
        )

        self.assertIs(first_error, result.post_commit_error)
        notes = "\n".join(getattr(first_error, "__notes__", ()))
        self.assertIn("committed notification failed", notes)
        self.assertIn("task temp cleanup failed", notes)
        self.assertIn("completion notification failed", notes)

    def test_job_retains_committed_cleanup_retry_until_it_succeeds(self):
        from spectrum_organizer.workflow.output_pipeline import (
            OutputPipelineJob,
            OutputPipelinePorts,
            OutputStageRequest,
        )

        cleanup_calls = []
        targets = SimpleNamespace(
            staging_dir=pathlib.Path("owned-staging")
        )
        completion = object()

        def post_commit(_snapshot, _completion):
            raise OSError("task temp cleanup failed")

        def retry_post_commit(snapshot, published):
            cleanup_calls.append((snapshot, published))

        job = OutputPipelineJob(
            ports=OutputPipelinePorts(
                process_gate=lambda: None,
                create_staging=lambda *args, **kwargs: targets,
                run_output=lambda request: output_result(),
                run_verifier=lambda request: SimpleNamespace(attempts=()),
                verify_sources=lambda expected, cancel_check: (),
                build_report=lambda request: "report",
                publish=lambda run_targets, report, verifier, **kwargs: completion,
                cleanup=lambda paths, *, run_id: self.fail(
                    "committed output must not be cleaned as staging"
                ),
                write_failure=lambda request: self.fail(
                    "committed output must not become failed"
                ),
                post_commit=post_commit,
                retry_post_commit=retry_post_commit,
            ),
            clock=lambda: datetime(2026, 8, 2, 12, 0, 0),
            run_id_factory=lambda: "run-1",
        )
        snapshot = SimpleNamespace(
            snapshot_id="approved-1",
            output_plan=object(),
            source_fingerprints_before=(),
        )
        request = OutputStageRequest(
            snapshot,
            pathlib.Path("output-parent"),
        )

        result = job(request)

        self.assertTrue(result.post_commit_cleanup_pending)
        with self.assertRaisesRegex(RuntimeError, "cleanup|清理"):
            job.prepare()
        job.retry_cleanup()
        self.assertEqual([(snapshot, completion)], cleanup_calls)
        job.prepare()

    def test_job_does_not_retain_cleanup_retry_when_staging_was_never_created(self):
        from spectrum_organizer.reporting.publication import (
            ParentUnavailableError,
        )
        from spectrum_organizer.workflow.output_pipeline import (
            OutputPipelineFailure,
            OutputPipelineJob,
            OutputPipelinePorts,
            OutputStageRequest,
        )

        job = OutputPipelineJob(
            ports=OutputPipelinePorts(
                process_gate=lambda: None,
                create_staging=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    ParentUnavailableError(
                        pathlib.Path("unavailable"),
                        "offline",
                    )
                ),
                run_output=lambda _request: None,
                run_verifier=lambda _request: None,
                verify_sources=lambda _expected, _cancel: (),
                build_report=lambda _request: "report",
                publish=lambda _targets, _report, _verifier: None,
                cleanup=lambda *_args, **_kwargs: self.fail(
                    "no staging exists to clean"
                ),
                write_failure=lambda _request: pathlib.Path("failure.txt"),
            ),
            clock=lambda: datetime(2026, 8, 2, 12, 0, 0),
            run_id_factory=lambda: "run-1",
        )
        request = OutputStageRequest(
            SimpleNamespace(
                snapshot_id="approved-1",
                output_plan=object(),
                source_fingerprints_before=(),
            ),
            pathlib.Path("output-parent"),
        )

        with self.assertRaises(OutputPipelineFailure):
            job(request)

        job.prepare()

    def test_job_retains_create_staging_cleanup_retry_until_it_succeeds(self):
        from spectrum_organizer.reporting.publication import (
            ParentUnavailableError,
        )
        from spectrum_organizer.workflow.output_pipeline import (
            OutputPipelineFailure,
            OutputPipelineJob,
            OutputPipelinePorts,
            OutputStageRequest,
        )

        cleanup_calls = []
        failure = ParentUnavailableError(
            pathlib.Path("output-parent"),
            "marker write failed",
            cleanup_retry=lambda: cleanup_calls.append("cleanup"),
        )
        job = OutputPipelineJob(
            ports=OutputPipelinePorts(
                process_gate=lambda: None,
                create_staging=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    failure
                ),
                run_output=lambda _request: None,
                run_verifier=lambda _request: None,
                verify_sources=lambda _expected, _cancel: (),
                build_report=lambda _request: "report",
                publish=lambda _targets, _report, _verifier: None,
                cleanup=lambda *_args, **_kwargs: self.fail(
                    "pipeline has no registered staging target"
                ),
                write_failure=lambda _request: pathlib.Path("failure.txt"),
            ),
            clock=lambda: datetime(2026, 8, 2, 12, 0, 0),
            run_id_factory=lambda: "run-1",
        )
        request = OutputStageRequest(
            SimpleNamespace(
                snapshot_id="approved-1",
                output_plan=object(),
                source_fingerprints_before=(),
            ),
            pathlib.Path("output-parent"),
        )

        with self.assertRaises(OutputPipelineFailure):
            job(request)
        with self.assertRaisesRegex(RuntimeError, "cleanup"):
            job.prepare()

        job.retry_cleanup()
        self.assertEqual(["cleanup"], cleanup_calls)
        job.prepare()

    def test_job_does_not_retain_cleanup_retry_after_successful_failure_cleanup(self):
        from spectrum_organizer.workflow.output_pipeline import (
            OutputPipelineFailure,
            OutputPipelineJob,
            OutputPipelinePorts,
            OutputStageRequest,
        )

        targets = SimpleNamespace(
            staging_dir=pathlib.Path("owned-staging")
        )
        job = OutputPipelineJob(
            ports=OutputPipelinePorts(
                process_gate=lambda: None,
                create_staging=lambda *_args, **_kwargs: targets,
                run_output=lambda _request: (_ for _ in ()).throw(
                    RuntimeError("writer failed")
                ),
                run_verifier=lambda _request: None,
                verify_sources=lambda _expected, _cancel: (),
                build_report=lambda _request: "report",
                publish=lambda _targets, _report, _verifier: None,
                cleanup=lambda *_args, **_kwargs: SimpleNamespace(
                    retained_unknown=()
                ),
                write_failure=lambda _request: pathlib.Path("failure.txt"),
            ),
            clock=lambda: datetime(2026, 8, 2, 12, 0, 0),
            run_id_factory=lambda: "run-1",
        )
        request = OutputStageRequest(
            SimpleNamespace(
                snapshot_id="approved-1",
                output_plan=object(),
                source_fingerprints_before=(),
            ),
            pathlib.Path("output-parent"),
        )

        with self.assertRaises(OutputPipelineFailure):
            job(request)

        job.prepare()

    def test_cleanup_failure_does_not_mask_primary_pipeline_failure(self):
        from spectrum_organizer.workflow.output_pipeline import (
            OutputPipelineFailure,
            OutputPipelinePorts,
            SourceFingerprintMismatchError,
            run_output_pipeline,
        )

        snapshot = SimpleNamespace(
            snapshot_id="approved-1",
            output_plan=object(),
            source_fingerprints_before=("before",),
        )
        targets = SimpleNamespace(staging_dir=pathlib.Path("owned-staging"))
        failure_requests = []

        def cleanup(paths, *, run_id):
            raise OSError("cleanup blocked")

        with self.assertRaises(OutputPipelineFailure) as raised:
            run_output_pipeline(
                snapshot,
                pathlib.Path("output-parent"),
                run_id="run-1",
                ports=OutputPipelinePorts(
                    process_gate=lambda: None,
                    create_staging=lambda *args, **kwargs: targets,
                    run_output=lambda request: output_result(),
                    run_verifier=lambda request: SimpleNamespace(attempts=()),
                    verify_sources=lambda expected, cancel_check: ("after",),
                    build_report=lambda request: self.fail("report must not be built"),
                    publish=lambda run_targets, report, verifier: self.fail("output must not be published"),
                    cleanup=cleanup,
                    write_failure=lambda request: failure_requests.append(request) or pathlib.Path("Failed_Run.txt"),
                ),
                clock=lambda: datetime(2026, 8, 2, 12, 0, 0),
                cancel_check=lambda: None,
                progress=lambda stage: None,
            )

        self.assertIsInstance(raised.exception.cause, SourceFingerprintMismatchError)
        self.assertIsInstance(raised.exception.cleanup_error, OSError)
        self.assertEqual("cleanup blocked", str(raised.exception.cleanup_error))
        self.assertEqual(1, len(failure_requests))

    def test_failed_worker_identity_is_registered_before_staging_cleanup(self):
        from spectrum_organizer.workflow.output_pipeline import (
            OutputPipelineFailure,
            OutputPipelinePorts,
            run_output_pipeline,
        )

        snapshot = SimpleNamespace(
            snapshot_id="approved-1",
            output_plan=object(),
            source_fingerprints_before=(),
        )
        targets = SimpleNamespace(staging_dir=pathlib.Path("owned-staging"))
        identity = (41, 42)
        calls = []

        def fail_output(_request):
            error = RuntimeError("writer failed after creation")
            error.owned_artifact_identity = identity
            raise error

        def register_failed(targets_arg, stage, identity_arg):
            calls.append(("register", targets_arg, stage, identity_arg))

        def cleanup(paths, *, run_id):
            calls.append(("cleanup", tuple(paths), run_id))
            return SimpleNamespace(retained_unknown=())

        with self.assertRaises(OutputPipelineFailure):
            run_output_pipeline(
                snapshot,
                pathlib.Path("output-parent"),
                run_id="run-1",
                ports=OutputPipelinePorts(
                    process_gate=lambda: None,
                    create_staging=lambda *_args, **_kwargs: targets,
                    run_output=fail_output,
                    run_verifier=lambda _request: None,
                    verify_sources=lambda _expected, _cancel: (),
                    build_report=lambda _request: "report",
                    publish=lambda *_args: None,
                    cleanup=cleanup,
                    write_failure=lambda _request: pathlib.Path("failure.txt"),
                    register_failed_artifact=register_failed,
                ),
                clock=lambda: datetime(2026, 8, 3, 12, 0, 0),
                cancel_check=lambda: None,
                progress=lambda _stage: None,
            )

        self.assertEqual(
            ("register", targets, "write_output", identity),
            calls[0],
        )
        self.assertEqual("cleanup", calls[1][0])

    def test_failed_artifact_registration_cannot_skip_staging_cleanup(self):
        from spectrum_organizer.workflow.output_pipeline import (
            OutputPipelineFailure,
            OutputPipelinePorts,
            run_output_pipeline,
        )

        snapshot = SimpleNamespace(
            snapshot_id="approved-1",
            output_plan=object(),
            source_fingerprints_before=(),
        )
        targets = SimpleNamespace(staging_dir=pathlib.Path("owned-staging"))
        cleanup_result = SimpleNamespace(retained_unknown=())
        calls = []

        def fail_output(_request):
            error = RuntimeError("writer failed after creation")
            error.owned_artifact_identity = (41, 42)
            raise error

        def fail_registration(*_args):
            calls.append("register")
            raise RuntimeError("identity was already retired")

        def cleanup(*_args, **_kwargs):
            calls.append("cleanup")
            return cleanup_result

        with self.assertRaises(OutputPipelineFailure) as raised:
            run_output_pipeline(
                snapshot,
                pathlib.Path("output-parent"),
                run_id="run-1",
                ports=OutputPipelinePorts(
                    process_gate=lambda: None,
                    create_staging=lambda *_args, **_kwargs: targets,
                    run_output=fail_output,
                    run_verifier=lambda _request: None,
                    verify_sources=lambda _expected, _cancel: (),
                    build_report=lambda _request: "report",
                    publish=lambda *_args: None,
                    cleanup=cleanup,
                    write_failure=lambda _request: pathlib.Path("failure.txt"),
                    register_failed_artifact=fail_registration,
                ),
                clock=lambda: datetime(2026, 8, 3, 12, 0, 0),
                cancel_check=lambda: None,
                progress=lambda _stage: None,
            )

        self.assertEqual(["register", "cleanup"], calls)
        self.assertIs(cleanup_result, raised.exception.cleanup_result)
        self.assertIsNone(raised.exception.cleanup_error)
        self.assertTrue(
            any(
                "identity was already retired" in note
                for note in getattr(raised.exception.cause, "__notes__", ())
            )
        )

    def test_source_reverification_receives_live_cancel_check(self):
        from spectrum_organizer.workflow.output_pipeline import (
            OutputPipelineCancelled,
            OutputPipelinePorts,
            run_output_pipeline,
        )

        snapshot = SimpleNamespace(
            snapshot_id="approved-1",
            output_plan=object(),
            source_fingerprints_before=("before",),
        )
        targets = SimpleNamespace(staging_dir=pathlib.Path("owned-staging"))
        checks = []
        cancel_calls = 0

        def verify_sources(expected, cancel_check):
            checks.append(expected)
            cancel_check()
            raise AssertionError("cancel_check must stop source hashing")

        def cancel_check():
            nonlocal cancel_calls
            cancel_calls += 1
            if cancel_calls == 6:
                raise OutputPipelineCancelled("cancelled while hashing")

        with self.assertRaises(OutputPipelineCancelled):
            run_output_pipeline(
                snapshot,
                pathlib.Path("output-parent"),
                run_id="run-1",
                ports=OutputPipelinePorts(
                    process_gate=lambda: None,
                    create_staging=lambda *args, **kwargs: targets,
                    run_output=lambda request: output_result(),
                    run_verifier=lambda request: SimpleNamespace(attempts=()),
                    verify_sources=verify_sources,
                    build_report=lambda request: self.fail("report must not be built"),
                    publish=lambda run_targets, report, verifier: self.fail("must not publish"),
                    cleanup=lambda paths, *, run_id: None,
                    write_failure=lambda request: self.fail("cancel must not log failure"),
                ),
                clock=lambda: datetime(2026, 8, 2, 12, 0, 0),
                cancel_check=cancel_check,
                progress=lambda stage: None,
            )

        self.assertEqual([("before",)], checks)

    def test_terminal_infrastructure_exception_attempts_are_kept_in_failure_log(self):
        from spectrum_organizer.workflow.output_pipeline import (
            OutputPipelineFailure,
            OutputPipelinePorts,
            run_output_pipeline,
        )

        attempts = (SimpleNamespace(attempt=1), SimpleNamespace(attempt=2))
        terminal = RuntimeError("output infrastructure exhausted")
        terminal.attempts = attempts
        requests = []

        with self.assertRaises(OutputPipelineFailure):
            run_output_pipeline(
                SimpleNamespace(
                    snapshot_id="approved-1",
                    output_plan=object(),
                    source_fingerprints_before=(),
                ),
                pathlib.Path("output-parent"),
                run_id="run-1",
                ports=OutputPipelinePorts(
                    process_gate=lambda: None,
                    create_staging=lambda *args, **kwargs: SimpleNamespace(
                        staging_dir=pathlib.Path("owned-staging")
                    ),
                    run_output=lambda request: (_ for _ in ()).throw(terminal),
                    run_verifier=lambda request: None,
                    verify_sources=lambda expected, cancel_check: (),
                    build_report=lambda request: "report",
                    publish=lambda targets, report, verifier: None,
                    cleanup=lambda paths, *, run_id: None,
                    write_failure=lambda request: requests.append(request),
                ),
                clock=lambda: datetime(2026, 8, 2, 12, 0, 0),
                cancel_check=lambda: None,
                progress=lambda stage: None,
            )

        self.assertEqual(attempts, requests[0].output_attempts)

    def test_failure_log_write_error_is_exposed_with_primary_failure(self):
        from spectrum_organizer.workflow.output_pipeline import (
            OutputPipelineFailure,
            OutputPipelinePorts,
            run_output_pipeline,
        )

        with self.assertRaises(OutputPipelineFailure) as raised:
            run_output_pipeline(
                SimpleNamespace(
                    snapshot_id="approved-1",
                    output_plan=object(),
                    source_fingerprints_before=(),
                ),
                pathlib.Path("output-parent"),
                run_id="run-1",
                ports=OutputPipelinePorts(
                    process_gate=lambda: (_ for _ in ()).throw(RuntimeError("blocked")),
                    create_staging=lambda *args, **kwargs: None,
                    run_output=lambda request: None,
                    run_verifier=lambda request: None,
                    verify_sources=lambda expected, cancel_check: (),
                    build_report=lambda request: "report",
                    publish=lambda targets, report, verifier: None,
                    cleanup=lambda paths, *, run_id: None,
                    write_failure=lambda request: (_ for _ in ()).throw(
                        OSError("log directory read-only")
                    ),
                ),
                clock=lambda: datetime(2026, 8, 2, 12, 0, 0),
                cancel_check=lambda: None,
                progress=lambda stage: None,
            )

        self.assertIsInstance(raised.exception.failure_log_error, OSError)
        self.assertIn("read-only", str(raised.exception.failure_log_error))


if __name__ == "__main__":
    unittest.main()
