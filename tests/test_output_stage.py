from decimal import Decimal
import io
import json
import os
import pathlib
import subprocess
import tempfile
from types import SimpleNamespace
import sys
import threading
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def _project_artifact_evidence():
    from spectrum_organizer.origin.contracts import ProjectArtifactEvidence

    return ProjectArtifactEvidence((1, 2), "0" * 64, 0)


def _project_artifact_payload():
    artifact = _project_artifact_evidence()
    return {
        "identity": list(artifact.identity),
        "sha256": artifact.sha256,
        "size": artifact.size,
    }


class OutputProcessContractTests(unittest.TestCase):
    @unittest.skipUnless(
        os.name == "nt",
        "production Origin cancellation budget is Windows-specific",
    )
    def test_production_cancellation_budget_covers_cold_identity_startup(self):
        from spectrum_organizer.origin.output_process import (
            JsonOriginChildProcessRunner,
        )

        runner = JsonOriginChildProcessRunner()

        self.assertGreaterEqual(runner.termination_timeout, 20.0)

    def test_child_audit_failure_preserves_known_output_artifact_identity(self):
        from spectrum_organizer.origin import output_process
        from spectrum_organizer.origin.output_process import (
            output_process_main,
            project_contract_to_payload,
        )
        from spectrum_organizer.origin.output_worker import build_project_write_contract
        from tests.test_origin_worker_contracts import _output_model

        contract = build_project_write_contract(_output_model())
        payload = {
            "approved_snapshot_id": "approved-1",
            "contract": project_contract_to_payload(contract),
            "staging_project_path": "staging/output.opju",
            "staging_project_identity": [1, 2],
            "run_staging_root": "staging",
            "run_staging_identity": [1, 2],
            "allowed_output_targets": ["staging/output.opju"],
            "attempt": 1,
        }
        stdout = io.StringIO()

        with mock.patch.object(
            output_process,
            "_record_successful_worker_targets",
            side_effect=OSError("audit unavailable"),
        ):
            return_code = output_process_main(
                ["output"],
                stdin=io.StringIO(json.dumps(payload)),
                stdout=stdout,
                output_runner=lambda _command: _project_artifact_evidence(),
            )

        result = json.loads(stdout.getvalue())
        self.assertEqual(1, return_code)
        self.assertEqual([1, 2], result["owned_artifact_identity"])

    @unittest.skipUnless(os.name == "nt", "production start-gate ownership is Windows-specific")
    def test_child_start_gate_never_overwrites_preexisting_foreign_file(self):
        from spectrum_organizer.origin.output_process import (
            JsonOriginChildProcessRunner,
        )

        class FinishedProcess:
            returncode = 0

            def poll(self):
                return 0

            def communicate(self, **_kwargs):
                return '{"ok": true}', ""

        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            protected = root / "protected.txt"
            protected.write_text("immutable", encoding="ascii")
            gate = root / "SpectrumOrganizer_gate.gate"
            os.link(protected, gate)
            runner = JsonOriginChildProcessRunner(
                process_factory=lambda *args, **kwargs: FinishedProcess(),
            )
            runner._require_process_job = True
            ids = iter((SimpleNamespace(hex="gate"), SimpleNamespace(hex="token")))

            with mock.patch.dict(os.environ, {"TEMP": str(root)}), mock.patch(
                "spectrum_organizer.origin.output_process.uuid.uuid4",
                side_effect=lambda: next(ids),
            ), mock.patch(
                "spectrum_organizer.origin.output_process.bind_process_to_job"
            ), mock.patch(
                "spectrum_organizer.origin.output_process.close_bound_process_job"
            ), self.assertRaises(Exception):
                runner("output", {})

            self.assertEqual("immutable", protected.read_text(encoding="ascii"))
            self.assertTrue(gate.exists())

    @unittest.skipUnless(os.name == "nt", "production start-gate ownership is Windows-specific")
    def test_child_start_gate_cleanup_refuses_replacement_after_handle_close(self):
        from spectrum_organizer.origin.output_process import (
            JsonOriginChildProcessRunner,
            OriginChildProcessError,
        )
        from spectrum_organizer.safety.identity_paths import (
            unlink_owned_path as unlink_owned_path_real,
        )

        class FinishedProcess:
            returncode = 0

            def poll(self):
                return 0

            def communicate(self, **_kwargs):
                return '{"ok": true}', ""

        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            protected = root / "protected.txt"
            protected.write_text("immutable", encoding="ascii")
            gate = root / "SpectrumOrganizer_gate.gate"
            parked = root / "owned.gate"

            def replace_then_unlink(path, expected_identity):
                pathlib.Path(path).rename(parked)
                os.link(protected, path)
                return unlink_owned_path_real(path, expected_identity)

            runner = JsonOriginChildProcessRunner(
                process_factory=lambda *args, **kwargs: FinishedProcess(),
            )
            runner._require_process_job = True
            ids = iter((SimpleNamespace(hex="gate"), SimpleNamespace(hex="token")))

            with mock.patch.dict(os.environ, {"TEMP": str(root)}), mock.patch(
                "spectrum_organizer.origin.output_process.uuid.uuid4",
                side_effect=lambda: next(ids),
            ), mock.patch(
                "spectrum_organizer.origin.output_process.bind_process_to_job"
            ), mock.patch(
                "spectrum_organizer.origin.output_process.close_bound_process_job"
            ), mock.patch(
                "spectrum_organizer.origin.output_process.unlink_owned_path",
                side_effect=replace_then_unlink,
            ), self.assertRaises(OriginChildProcessError):
                runner("output", {})

            self.assertEqual("immutable", protected.read_text(encoding="ascii"))
            self.assertTrue(gate.exists())
            self.assertTrue(parked.exists())

    @unittest.skipUnless(os.name == "nt", "production start-gate ownership is Windows-specific")
    def test_child_start_gate_cleanup_failure_is_retryable(self):
        from spectrum_organizer.origin.output_process import (
            JsonOriginChildProcessRunner,
            OriginChildProcessError,
        )
        from spectrum_organizer.safety.identity_paths import (
            IdentityPathError,
            unlink_owned_path as unlink_owned_path_real,
        )

        class FinishedProcess:
            returncode = 0

            def poll(self):
                return 0

            def communicate(self, **_kwargs):
                return '{"ok": true}', ""

        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            gate = root / "SpectrumOrganizer_gate.gate"
            runner = JsonOriginChildProcessRunner(
                process_factory=lambda *args, **kwargs: FinishedProcess(),
            )
            runner._require_process_job = True
            ids = iter((SimpleNamespace(hex="gate"), SimpleNamespace(hex="token")))
            gate_calls = 0

            def fail_once_then_unlink(path, expected_identity):
                nonlocal gate_calls
                if pathlib.Path(path) == gate:
                    gate_calls += 1
                if pathlib.Path(path) == gate and gate_calls == 1:
                    raise IdentityPathError(pathlib.Path(path), "temporarily locked")
                return unlink_owned_path_real(path, expected_identity)

            with mock.patch.dict(os.environ, {"TEMP": str(root)}), mock.patch(
                "spectrum_organizer.origin.output_process.uuid.uuid4",
                side_effect=lambda: next(ids),
            ), mock.patch(
                "spectrum_organizer.origin.output_process.bind_process_to_job"
            ), mock.patch(
                "spectrum_organizer.origin.output_process.close_bound_process_job"
            ), mock.patch(
                "spectrum_organizer.origin.output_process.unlink_owned_path",
                side_effect=fail_once_then_unlink,
            ):
                with self.assertRaises(OriginChildProcessError):
                    runner("output", {})
                self.assertTrue(gate.exists())
                runner.retry_cleanup()

            self.assertEqual(2, gate_calls)
            self.assertFalse(gate.exists())

    def test_child_json_transports_secondary_error_notes(self):
        from spectrum_organizer.origin.output_process import (
            _raise_child_failure,
            output_process_main,
            project_contract_to_payload,
        )
        from spectrum_organizer.origin.output_worker import (
            build_project_write_contract,
        )
        from tests.test_origin_worker_contracts import _output_model

        failure = RuntimeError("primary failure")
        failure.add_note("Origin session close also failed: close blocked")
        stdout = io.StringIO()

        output_process_main(
            ["output"],
            stdin=io.StringIO(
                json.dumps(
                    {
                        "approved_snapshot_id": "approved-1",
                        "contract": project_contract_to_payload(
                            build_project_write_contract(_output_model())
                        ),
                        "staging_project_path": "staging/output.opju",
                        "staging_project_identity": [1, 2],
                        "run_staging_root": "staging",
                        "run_staging_identity": [0, 0],
                        "allowed_output_targets": ["staging/output.opju"],
                        "attempt": 1,
                    }
                )
            ),
            stdout=stdout,
            output_runner=lambda _command: (_ for _ in ()).throw(failure),
        )
        result = json.loads(stdout.getvalue())

        self.assertEqual(
            ["Origin session close also failed: close blocked"],
            result["error_notes"],
        )
        with self.assertRaises(Exception) as raised:
            _raise_child_failure(result, "output")
        self.assertIn(
            "Origin session close also failed: close blocked",
            getattr(raised.exception, "__notes__", ()),
        )

    def test_verifier_mismatch_round_trip_preserves_exact_structural_report(self):
        from spectrum_organizer.origin.output_process import (
            _raise_child_failure,
            output_process_main,
            project_contract_to_payload,
        )
        from spectrum_organizer.origin.output_worker import (
            build_project_write_contract,
        )
        from spectrum_organizer.origin.verify_worker import (
            MismatchReport,
            VerificationMismatchError,
        )
        from tests.test_origin_worker_contracts import _output_model

        contract = build_project_write_contract(_output_model())
        report = MismatchReport(
            "/Folder/Book",
            "B",
            7,
            Decimal("1.25"),
            Decimal("1.5"),
            "numeric",
        )
        payload = {
            "approved_snapshot_id": "approved-1",
            "staged_project_path": "staging/output.opju",
            "mutation_copy_path": "staging/mutation.opju",
            "mutation_copy_identity": [3, 4],
            "run_staging_root": "staging",
            "run_staging_identity": [1, 2],
            "allowed_open_targets": [
                "staging/output.opju",
                "staging/mutation.opju",
            ],
            "protected_paths": [],
            "expected_project_artifact": _project_artifact_payload(),
            "contract": project_contract_to_payload(contract),
            "attempt": 1,
        }
        stdout = io.StringIO()

        result_code = output_process_main(
            ["verifier"],
            stdin=io.StringIO(json.dumps(payload)),
            stdout=stdout,
            verifier_runner=lambda command: (_ for _ in ()).throw(
                VerificationMismatchError(
                    "Origin output verification mismatch",
                    report,
                )
            ),
        )
        result = json.loads(stdout.getvalue())

        self.assertEqual(1, result_code)
        self.assertEqual(
            {
                "structural_path": "/Folder/Book",
                "column": "B",
                "row": 7,
                "expected": "1.25",
                "actual": "1.5",
                "mismatch_class": "numeric",
            },
            result["mismatch_report"],
        )
        with self.assertRaises(VerificationMismatchError) as raised:
            _raise_child_failure(result, "verifier")
        self.assertEqual(report, raised.exception.report)
        self.assertIn("/Folder/Book", str(raised.exception))
        self.assertIn("column=B", str(raised.exception))
        self.assertIn("row=7", str(raised.exception))

    def test_metadata_mismatch_round_trip_restores_tuple_values_and_error_notes(self):
        from spectrum_organizer.origin.output_process import (
            _raise_child_failure,
            output_process_main,
        )
        from spectrum_organizer.origin.verify_worker import (
            MismatchReport,
            VerificationMismatchError,
        )

        report = MismatchReport(
            "/Folder/Book",
            "B",
            None,
            ("B", "Y", "expected"),
            ("B", "Y", "actual"),
            "metadata",
        )
        stdout = io.StringIO()

        output_process_main(
            ["verifier"],
            stdin=io.StringIO("{}"),
            stdout=stdout,
            verifier_runner=lambda _command: None,
        )
        result = {
            "ok": False,
            "classification": "non_retryable",
            "error": "metadata mismatch",
            "error_type": "VerificationMismatchError",
            "mismatch_report": {
                "structural_path": report.structural_path,
                "column": report.column,
                "row": report.row,
                "expected": list(report.expected),
                "actual": list(report.actual),
                "mismatch_class": report.mismatch_class,
            },
            "error_notes": ["Origin session close also failed: close blocked"],
        }

        with self.assertRaises(VerificationMismatchError) as raised:
            _raise_child_failure(result, "verifier")

        self.assertEqual(report, raised.exception.report)
        self.assertIn(
            "Origin session close also failed: close blocked",
            getattr(raised.exception, "__notes__", ()),
        )

    def test_child_job_close_failure_does_not_mask_primary_protocol_failure(self):
        from spectrum_organizer.origin.output_process import (
            JsonOriginChildProcessRunner,
            OriginChildProcessError,
        )

        process = SimpleNamespace(returncode=0)
        process.poll = lambda: 0
        process.communicate = lambda **kwargs: ("not-json", "")
        runner = JsonOriginChildProcessRunner(
            process_factory=lambda *args, **kwargs: process,
        )

        with mock.patch(
            "spectrum_organizer.origin.output_process.close_bound_process_job",
            side_effect=RuntimeError("job close failed"),
        ), self.assertRaises(OriginChildProcessError) as raised:
            runner("output", {})

        self.assertIn("invalid JSON", str(raised.exception))
        self.assertIn(
            "child process job close also failed: job close failed",
            getattr(raised.exception, "__notes__", ()),
        )

    def test_child_rejects_success_json_with_failure_exit_code(self):
        from spectrum_organizer.origin.output_process import (
            JsonOriginChildProcessRunner,
            OriginChildProcessError,
        )

        process = SimpleNamespace(returncode=1)
        process.poll = lambda: 1
        process.communicate = lambda **kwargs: (
            json.dumps(
                {
                    "ok": True,
                    "project_artifact": _project_artifact_payload(),
                }
            ),
            "",
        )
        runner = JsonOriginChildProcessRunner(
            process_factory=lambda *args, **kwargs: process,
        )

        with self.assertRaisesRegex(
            OriginChildProcessError,
            "exit code|result",
        ):
            runner("output", {})

    def test_cancel_during_process_spawn_cannot_release_an_unregistered_child(self):
        from spectrum_organizer.origin.output_process import (
            JsonOriginChildProcessRunner,
            OriginChildProcessError,
        )

        factory_entered = threading.Event()
        release_factory = threading.Event()
        terminated = threading.Event()

        class Process:
            returncode = None

            def poll(self):
                return 0 if terminated.is_set() else None

            def wait(self):
                self.returncode = 0
                return 0

            def communicate(self, **kwargs):
                raise AssertionError("cancelled child must not communicate")

        process = Process()

        def process_factory(*args, **kwargs):
            del args, kwargs
            factory_entered.set()
            release_factory.wait(2)
            return process

        runner = JsonOriginChildProcessRunner(
            process_factory=process_factory,
        )
        errors = []
        run_thread = threading.Thread(
            target=lambda: self._capture_child_error(
                errors,
                lambda: runner("output", {}),
            )
        )
        run_thread.start()
        self.assertTrue(factory_entered.wait(2))
        with mock.patch(
            "spectrum_organizer.origin.output_process.terminate_bound_process",
            side_effect=lambda _process: terminated.set(),
        ):
            cancel_finished = threading.Event()
            cancel_thread = threading.Thread(
                target=lambda: (runner.cancel(), cancel_finished.set())
            )
            cancel_thread.start()
            self.assertTrue(cancel_finished.wait(0.2))
            release_factory.set()
            run_thread.join(2)
            cancel_thread.join(2)

        self.assertFalse(run_thread.is_alive())
        self.assertFalse(cancel_thread.is_alive())
        self.assertTrue(terminated.is_set())
        self.assertIsInstance(errors[0], OriginChildProcessError)
        self.assertIn("cancelled", str(errors[0]).lower())

    @unittest.skipUnless(os.name == "nt", "production identity handoff is Windows-specific")
    def test_production_cancel_waits_for_exact_origin_identity_before_cleanup(self):
        from spectrum_organizer.origin import process_identity
        from spectrum_organizer.origin.output_process import (
            JsonOriginChildProcessRunner,
            OriginChildProcessError,
        )
        from spectrum_organizer.safety.process_boundary import ProcessIdentity

        runner = None
        environment = None
        terminated = False

        class Process:
            returncode = None

            def poll(self):
                return 0 if terminated else None

            def communicate(self, **_kwargs):
                nonlocal terminated
                if terminated:
                    self.returncode = -1
                    return "", ""
                runner.cancel()
                self.assert_not_terminated_before_handoff()
                handoff = pathlib.Path(
                    environment[
                        process_identity.ORIGIN_IDENTITY_HANDOFF_PATH_ENV
                    ]
                )
                handoff.write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "token": environment[
                                process_identity.ORIGIN_IDENTITY_HANDOFF_TOKEN_ENV
                            ],
                            "role": "verifier",
                            "attempt_binding": {
                                "approved_snapshot_id": "approved-1",
                                "run_staging_root": "staging",
                                "attempt": 1,
                            },
                            "pid": 4321,
                            "start_time_ns": 987654321,
                        }
                    ),
                    encoding="utf-8",
                )
                raise subprocess.TimeoutExpired("worker", 0)

            def assert_not_terminated_before_handoff(self):
                if terminated:
                    raise AssertionError(
                        "child terminated before exact Origin identity handoff"
                    )

            def wait(self, timeout=None):
                del timeout
                self.returncode = -1
                return self.returncode

        process = Process()

        def process_factory(*_args, **kwargs):
            nonlocal environment
            environment = kwargs["env"]
            return process

        controller = mock.Mock()
        runner = JsonOriginChildProcessRunner(
            process_factory=process_factory,
            origin_process_controller=controller,
            poll_interval=0.001,
            termination_timeout=1.0,
        )
        runner._require_process_job = True

        def terminate(_process):
            nonlocal terminated
            terminated = True

        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(
            os.environ,
            {"TEMP": temp},
        ), mock.patch(
            "spectrum_organizer.origin.output_process.bind_process_to_job"
        ), mock.patch(
            "spectrum_organizer.origin.output_process.terminate_bound_process",
            side_effect=terminate,
        ), mock.patch(
            "spectrum_organizer.origin.output_process.close_bound_process_job"
        ), self.assertRaisesRegex(OriginChildProcessError, "cancelled"):
            runner(
                "verifier",
                {
                    "approved_snapshot_id": "approved-1",
                    "run_staging_root": "staging",
                    "attempt": 1,
                },
            )

        controller.close_program_owned.assert_called_once()
        identity_arg = controller.close_program_owned.call_args.args[0]
        timeout_arg = controller.close_program_owned.call_args.kwargs[
            "timeout"
        ]
        self.assertEqual(
            ProcessIdentity(pid=4321, start_time_ns=987654321),
            identity_arg,
        )
        self.assertGreater(timeout_arg, 0)
        self.assertLessEqual(timeout_arg, 1.0)

    def test_cancelled_child_has_a_total_termination_deadline_and_retains_cleanup_ownership(self):
        from spectrum_organizer.origin.output_process import (
            JsonOriginChildProcessRunner,
            OriginChildProcessError,
        )

        runner = None

        class Process:
            returncode = None
            exited = False

            def poll(self):
                return 0 if self.exited else None

            def communicate(self, **_kwargs):
                runner._cancelled.set()
                raise subprocess.TimeoutExpired("worker", 0)

            def wait(self, timeout=None):
                raise subprocess.TimeoutExpired("worker", timeout)

        process = Process()
        runner = JsonOriginChildProcessRunner(
            process_factory=lambda *args, **kwargs: process,
            poll_interval=0.001,
            termination_timeout=0.0,
        )

        with mock.patch(
            "spectrum_organizer.origin.output_process.terminate_bound_process"
        ), self.assertRaisesRegex(OriginChildProcessError, "did not exit"):
            runner("output", {})

        self.assertIs(process, runner._current_process)
        process.exited = True
        with mock.patch(
            "spectrum_organizer.origin.output_process.close_bound_process_job"
        ) as close_job:
            runner.reset()
        close_job.assert_called_once_with(process)
        self.assertIsNone(runner._current_process)

    def test_cancel_deadline_failure_uses_pipeline_cancellation_type_and_one_total_deadline(self):
        from spectrum_organizer.origin.output_process import (
            JsonOriginChildProcessRunner,
        )
        from spectrum_organizer.workflow.output_pipeline import (
            OutputPipelineCancelled,
        )

        times = iter((10.0, 10.6, 11.1, 11.1))
        wait_timeouts = []
        runner = None

        class Process:
            returncode = None

            def poll(self):
                return None

            def communicate(self, **_kwargs):
                runner.cancel()
                raise subprocess.TimeoutExpired("worker", 0)

            def wait(self, timeout=None):
                wait_timeouts.append(timeout)
                raise subprocess.TimeoutExpired("worker", timeout)

        process = Process()
        runner = JsonOriginChildProcessRunner(
            process_factory=lambda *args, **kwargs: process,
            cancellation_error_factory=OutputPipelineCancelled,
            poll_interval=0.001,
            termination_timeout=1.0,
            monotonic=lambda: next(times),
        )

        with mock.patch(
            "spectrum_organizer.origin.output_process.terminate_bound_process"
        ), self.assertRaises(OutputPipelineCancelled):
            runner("output", {})

        self.assertTrue(all(timeout <= 0.4 for timeout in wait_timeouts))

    def test_retry_cleanup_reclaims_the_retained_child_and_job(self):
        from spectrum_organizer.origin.output_process import (
            JsonOriginChildProcessRunner,
        )

        class Process:
            exited = False

            def poll(self):
                return 0 if self.exited else None

            def wait(self, timeout=None):
                del timeout
                self.exited = True
                return 0

        process = Process()
        runner = JsonOriginChildProcessRunner(
            process_factory=lambda *args, **kwargs: process,
            termination_timeout=1.0,
        )
        runner._current_process = process
        runner._cancelled.set()

        with mock.patch(
            "spectrum_organizer.origin.output_process.terminate_bound_process"
        ) as terminate, mock.patch(
            "spectrum_organizer.origin.output_process.close_bound_process_job"
        ) as close_job:
            runner.retry_cleanup()

        terminate.assert_called_once_with(process)
        close_job.assert_called_once_with(process)
        self.assertIsNone(runner._current_process)
        self.assertIsNone(runner._termination_deadline)

    def test_retained_live_child_blocks_replacement_and_automatic_retry(self):
        from spectrum_organizer.origin.output_process import (
            JsonOriginChildProcessRunner,
            OriginWorkerProcessPort,
        )
        from spectrum_organizer.origin.output_worker import (
            OutputInfrastructureFailure,
        )
        from spectrum_organizer.workflow.output_pipeline import (
            OutputRunRequest,
        )
        from tests.test_origin_worker_contracts import _output_model

        launches = []

        class Process:
            returncode = None

            def poll(self):
                return None

            def communicate(self, **_kwargs):
                raise OSError("pipe disconnected")

            def wait(self, timeout=None):
                raise subprocess.TimeoutExpired("worker", timeout)

        process = Process()

        def process_factory(*_args, **_kwargs):
            launches.append(process)
            return process

        runner = JsonOriginChildProcessRunner(
            process_factory=process_factory,
            termination_timeout=0.0,
        )
        targets = SimpleNamespace(
            staging_dir=pathlib.Path("staging"),
            staging_identity=(0, 0),
            staging_project_path=pathlib.Path(
                "staging/Organized_Spectra_20260802_120000.opju"
            ),
            verifier_mutation_path=pathlib.Path(
                "staging/Verifier_Mutation_20260802_120000.opju"
            ),
        )
        port = OriginWorkerProcessPort(
            child_runner=runner,
            prepare_output=lambda _targets: (1, 2),
            prepare_verifier=lambda _targets: (3, 4),
            cleanup_output=lambda _targets, _identity: None,
            cleanup_verifier=lambda _targets, _identity: None,
        )

        with mock.patch(
            "spectrum_organizer.origin.output_process.terminate_bound_process"
        ), self.assertRaises(OutputInfrastructureFailure):
            port.run_output(
                OutputRunRequest(
                    SimpleNamespace(
                        snapshot_id="approved-1",
                        output_plan=_output_model(),
                    ),
                    targets,
                )
            )

        self.assertEqual([process], launches)
        self.assertIs(process, runner._current_process)

    @staticmethod
    def _capture_child_error(errors, operation):
        try:
            operation()
        except BaseException as exc:
            errors.append(exc)

    def test_project_contract_json_round_trip_preserves_exact_decimal_and_blank_values(self):
        from spectrum_organizer.origin.output_process import (
            project_contract_from_payload,
            project_contract_to_payload,
        )
        from spectrum_organizer.origin.output_worker import (
            BookWriteContract,
            ColumnWriteContract,
            FolderWriteContract,
            ProjectWriteContract,
        )

        contract = ProjectWriteContract(
            "/",
            (
                FolderWriteContract(
                    "F_Ex270_ExSlit2_EmSlit2",
                    (
                        BookWriteContract(
                            "Long sample name",
                            None,
                            (
                                ColumnWriteContract(
                                    "A",
                                    "X",
                                    "Em",
                                    (Decimal("1.0000000000000000001"), None),
                                ),
                                ColumnWriteContract(
                                    "B",
                                    "Y",
                                    "sample",
                                    (Decimal("2.5"), Decimal("3")),
                                    "col(A)/max(col(A))",
                                    "Divided by Max of A",
                                ),
                            ),
                        ),
                    ),
                ),
            ),
        )

        payload = project_contract_to_payload(contract)

        self.assertEqual(contract, project_contract_from_payload(payload))
        self.assertEqual("1.0000000000000000001", payload["folders"][0]["books"][0]["columns"][0]["values"][0])

    def test_project_contract_payload_uses_readable_ascii_multiplication_for_origin_visible_text(self):
        from spectrum_organizer.origin.output_process import (
            project_contract_to_payload,
        )
        from spectrum_organizer.origin.output_worker import (
            BookWriteContract,
            ColumnWriteContract,
            FolderWriteContract,
            ProjectWriteContract,
        )

        contract = ProjectWriteContract(
            "/",
            (
                FolderWriteContract(
                    "F_Ex270_ExSlit2_EmSlit2",
                    (
                        BookWriteContract(
                            "MFL-mTHF-1×10^-4 M",
                            None,
                            (
                                ColumnWriteContract(
                                    "A",
                                    "X",
                                    "MFL-mTHF-1×10^-4 M-298 K_F270",
                                    (Decimal("1"),),
                                ),
                            ),
                        ),
                    ),
                ),
            ),
        )

        payload = project_contract_to_payload(contract)
        book_payload = payload["folders"][0]["books"][0]

        self.assertEqual("MFL-mTHF-1x10^-4 M", book_payload["display_long_name"])
        self.assertEqual(
            "MFL-mTHF-1x10^-4 M-298 K_F270",
            book_payload["columns"][0]["comment"],
        )
        self.assertEqual("MFL-mTHF-1×10^-4 M", contract.folders[0].books[0].display_long_name)

    def test_project_contract_payload_rejects_same_folder_book_names_that_collapse_after_origin_text_conversion(self):
        from spectrum_organizer.origin.output_process import (
            project_contract_to_payload,
        )
        from spectrum_organizer.origin.output_worker import (
            BookWriteContract,
            FolderWriteContract,
            ProjectWriteContract,
        )

        contract = ProjectWriteContract(
            "/",
            (
                FolderWriteContract(
                    "F_Ex270_ExSlit2_EmSlit2",
                    (
                        BookWriteContract("Sample-A×B", None, ()),
                        BookWriteContract("Sample-AxB", None, ()),
                    ),
                ),
            ),
        )

        with self.assertRaisesRegex(
            ValueError,
            "Origin-visible Book Long Name collision",
        ):
            project_contract_to_payload(contract)

    def test_child_entrypoint_uses_validated_json_contract_and_emits_one_json_result(self):
        from spectrum_organizer.origin.output_process import (
            output_process_main,
            project_contract_to_payload,
        )
        from spectrum_organizer.origin.output_worker import build_project_write_contract
        from tests.test_origin_worker_contracts import _output_model

        contract = build_project_write_contract(_output_model())
        payload = {
            "approved_snapshot_id": "approved-1",
            "contract": project_contract_to_payload(contract),
            "staging_project_path": "staging/Organized_Spectra_20260802_120000.opju",
            "staging_project_identity": [1, 2],
            "run_staging_root": "staging",
            "run_staging_identity": [0, 0],
            "allowed_output_targets": ["staging/Organized_Spectra_20260802_120000.opju"],
            "attempt": 1,
        }
        observed = []
        stdout = io.StringIO()

        result = output_process_main(
            ["output"],
            stdin=io.StringIO(json.dumps(payload)),
            stdout=stdout,
            output_runner=lambda command: (
                observed.append(command)
                or _project_artifact_evidence()
            ),
        )

        self.assertEqual(0, result)
        self.assertEqual(1, len(observed))
        self.assertEqual(contract, observed[0].approved_contract)
        self.assertEqual(
            {
                "ok": True,
                "project_artifact": _project_artifact_payload(),
                "owned_artifact_identity": [1, 2],
            },
            json.loads(stdout.getvalue()),
        )

    def test_verifier_child_success_uses_worker_creation_identity_for_cleanup(self):
        from spectrum_organizer.origin.output_process import (
            output_process_main,
            project_contract_to_payload,
        )
        from spectrum_organizer.origin.output_worker import (
            build_project_write_contract,
        )
        from spectrum_organizer.safety.identity_paths import path_identity
        from tests.test_origin_worker_contracts import _output_model

        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            staged = root / "output.opju"
            staged.write_bytes(b"project")
            mutation = root / "mutation.opju"
            mutation.write_bytes(b"")
            reserved_mutation_identity = path_identity(mutation)
            parked = root / "parked-owned-mutation.opju"
            contract = build_project_write_contract(_output_model())
            payload = {
                "approved_snapshot_id": "approved-1",
                "staged_project_path": str(staged),
                "mutation_copy_path": str(mutation),
                "mutation_copy_identity": list(reserved_mutation_identity),
                "run_staging_root": str(root),
                "run_staging_identity": list(path_identity(root)),
                "allowed_open_targets": [str(staged), str(mutation)],
                "protected_paths": [],
                "expected_project_artifact": _project_artifact_payload(),
                "contract": project_contract_to_payload(contract),
                "attempt": 1,
            }
            owned_identity = None

            def verifier_runner(_command):
                nonlocal owned_identity
                mutation.write_bytes(b"owned")
                owned_identity = path_identity(mutation)
                mutation.rename(parked)
                mutation.write_bytes(b"FOREIGN")
                return _project_artifact_evidence(), owned_identity

            stdout = io.StringIO()
            result_code = output_process_main(
                ["verifier"],
                stdin=io.StringIO(json.dumps(payload)),
                stdout=stdout,
                verifier_runner=verifier_runner,
            )
            result = json.loads(stdout.getvalue())

            self.assertEqual(0, result_code)
            self.assertEqual(list(owned_identity), result["owned_artifact_identity"])
            self.assertEqual(b"FOREIGN", mutation.read_bytes())
            self.assertEqual(b"owned", parked.read_bytes())


class OriginWorkerProcessPortTests(unittest.TestCase):

    def test_parent_process_secondary_notes_survive_port_wrapping(self):
        from spectrum_organizer.origin.output_process import (
            OriginChildProcessError,
            OriginWorkerProcessPort,
        )
        from spectrum_organizer.origin.output_worker import (
            InfrastructureOutputError,
        )

        child_error = OriginChildProcessError("child protocol failed")
        child_error.add_note("child process job close also failed: close blocked")
        port = OriginWorkerProcessPort(
            child_runner=lambda role, payload: (_ for _ in ()).throw(
                child_error
            ),
            prepare_output=lambda _targets: (1, 2),
            prepare_verifier=lambda _targets: (3, 4),
            cleanup_output=lambda targets, identity: None,
            cleanup_verifier=lambda targets, identity: None,
        )

        with self.assertRaises(InfrastructureOutputError) as raised:
            port._run_child("output", {})

        self.assertIn(
            "close blocked",
            str(raised.exception),
        )
        self.assertIn(
            "close blocked",
            "\n".join(getattr(raised.exception, "__notes__", ())),
        )
    def test_output_infrastructure_failure_retries_once_with_exact_artifact_cleanup(self):
        from spectrum_organizer.origin.output_process import OriginWorkerProcessPort
        from spectrum_organizer.workflow.output_pipeline import OutputRunRequest
        from tests.test_origin_worker_contracts import _output_model

        calls = []
        cleanups = []

        def child_runner(role, payload):
            calls.append((role, payload))
            if len(calls) == 1:
                return {
                    "ok": False,
                    "classification": "retry_once_later",
                    "error": "COM launch failed",
                }
            return {
                "ok": True,
                "project_artifact": _project_artifact_payload(),
            }

        targets = SimpleNamespace(
            staging_dir=pathlib.Path("staging"),
            staging_identity=(0, 0),
            staging_project_path=pathlib.Path("staging/Organized_Spectra_20260802_120000.opju"),
            verifier_mutation_path=pathlib.Path("staging/Verifier_Mutation_20260802_120000.opju"),
        )
        port = OriginWorkerProcessPort(
            child_runner=child_runner,
            prepare_output=lambda _targets: (1, 2),
            prepare_verifier=lambda _targets: (3, 4),
            cleanup_output=lambda targets, identity: cleanups.append("output"),
            cleanup_verifier=lambda targets, identity: cleanups.append("verifier"),
        )

        result = port.run_output(
            OutputRunRequest(
                SimpleNamespace(snapshot_id="approved-1", output_plan=_output_model()),
                targets,
            )
        )

        self.assertEqual(("infrastructure_failed", "succeeded"), tuple(item.status for item in result.attempts))
        self.assertEqual(["output"], cleanups)
        self.assertEqual(["output", "output"], [role for role, _payload in calls])

    def test_output_retry_audits_each_target_attempt_and_failed_attempt_cleanup(self):
        from spectrum_organizer.origin.output_process import OriginWorkerProcessPort
        from spectrum_organizer.runtime_audit import RUNTIME_AUDIT_DIR_ENV
        from spectrum_organizer.safety.identity_paths import (
            path_identity,
            unlink_owned_path,
        )
        from spectrum_organizer.workflow.output_pipeline import OutputRunRequest
        from tests.test_origin_worker_contracts import _output_model

        calls = 0

        def child_runner(_role, _payload):
            nonlocal calls
            calls += 1
            if calls == 1:
                return {
                    "ok": False,
                    "classification": "retry_once_later",
                    "error": "COM launch failed",
                }
            return {
                "ok": True,
                "project_artifact": _project_artifact_payload(),
            }

        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            audit_dir = root / "audit"
            audit_dir.mkdir()
            targets = SimpleNamespace(
                staging_dir=root / "staging",
                staging_identity=(0, 0),
                staging_project_path=(
                    root / "staging" / "Organized_Spectra_20260802_120000.opju"
                ),
                verifier_mutation_path=(
                    root / "staging" / "Verifier_Mutation_20260802_120000.opju"
                ),
            )
            targets.staging_dir.mkdir()
            reserved_identities = []

            def prepare_output(_targets):
                with targets.staging_project_path.open("xb") as stream:
                    stream.write(b"reserved")
                identity = path_identity(targets.staging_project_path)
                reserved_identities.append(identity)
                return identity

            def cleanup_output(_targets, identity):
                unlink_owned_path(targets.staging_project_path, identity)

            port = OriginWorkerProcessPort(
                child_runner=child_runner,
                prepare_output=prepare_output,
                prepare_verifier=lambda _targets: (3, 4),
                cleanup_output=cleanup_output,
                cleanup_verifier=lambda _targets, _identity: None,
            )

            with mock.patch.dict(
                os.environ,
                {RUNTIME_AUDIT_DIR_ENV: str(audit_dir)},
            ), mock.patch(
                "spectrum_organizer.origin.output_process._record_approved_counts"
            ):
                port.run_output(
                    OutputRunRequest(
                        SimpleNamespace(
                            snapshot_id="approved-1",
                            output_plan=_output_model(),
                        ),
                        targets,
                    )
                )

            events = [
                json.loads(path.read_text(encoding="utf-8"))
                for path in audit_dir.glob("*.json")
            ]
            attempts = [
                event["payload"]
                for event in events
                if event["event_type"] == "origin_worker_target_attempt"
            ]
            cleanups = [
                event["payload"]
                for event in events
                if event["event_type"] == "origin_worker_retry_cleanup"
            ]

            self.assertEqual([1, 2], [item["attempt"] for item in attempts])
            self.assertEqual([1], [item["attempt"] for item in cleanups])
            self.assertTrue(cleanups[0]["completed"])
            self.assertEqual(2, len(reserved_identities))
            self.assertNotEqual(*reserved_identities)
            self.assertEqual(
                list(reserved_identities[0]),
                [
                    cleanups[0]["artifact_identity"]["device_id"],
                    cleanups[0]["artifact_identity"]["file_id"],
                ],
            )
            self.assertNotEqual(
                attempts[0]["target_states"][0]["identity"],
                attempts[1]["target_states"][0]["identity"],
            )

    def test_output_post_delete_audit_failure_does_not_restore_retired_identity(self):
        from spectrum_organizer.origin import output_process
        from spectrum_organizer.origin.output_process import OriginWorkerProcessPort
        from spectrum_organizer.origin.output_worker import OutputInfrastructureFailure
        from spectrum_organizer.workflow.output_pipeline import OutputRunRequest
        from tests.test_origin_worker_contracts import _output_model

        targets = SimpleNamespace(
            staging_dir=pathlib.Path("staging"),
            staging_identity=(0, 0),
            staging_project_path=pathlib.Path("staging/output.opju"),
            verifier_mutation_path=pathlib.Path("staging/mutation.opju"),
        )
        port = OriginWorkerProcessPort(
            child_runner=lambda _role, _payload: {
                "ok": False,
                "classification": "retry_once_later",
                "error": "worker failed",
                "owned_artifact_identity": [17, 2],
            },
            prepare_output=lambda _targets: (17, 2),
            prepare_verifier=lambda _targets: (31, 2),
            cleanup_output=lambda _targets, _identity: None,
            cleanup_verifier=lambda _targets, _identity: None,
        )

        with mock.patch.object(
            output_process,
            "_record_worker_retry_cleanup",
            side_effect=OSError("audit write failed"),
        ), self.assertRaises(OutputInfrastructureFailure) as raised:
            port.run_output(
                OutputRunRequest(
                    SimpleNamespace(
                        snapshot_id="approved-1",
                        output_plan=_output_model(),
                    ),
                    targets,
                )
            )

        self.assertFalse(
            hasattr(raised.exception, "owned_artifact_identity")
        )

    def test_verifier_post_delete_audit_failure_does_not_restore_retired_identity(self):
        from spectrum_organizer.origin import output_process
        from spectrum_organizer.origin.output_process import OriginWorkerProcessPort
        from spectrum_organizer.origin.output_worker import build_project_write_contract
        from spectrum_organizer.origin.verify_worker import VerifierInfrastructureFailure
        from spectrum_organizer.workflow.output_pipeline import VerificationRunRequest
        from tests.test_origin_worker_contracts import _output_model

        targets = SimpleNamespace(
            staging_dir=pathlib.Path("staging"),
            staging_identity=(0, 0),
            staging_project_path=pathlib.Path("staging/output.opju"),
            verifier_mutation_path=pathlib.Path("staging/mutation.opju"),
        )
        port = OriginWorkerProcessPort(
            child_runner=lambda _role, _payload: {
                "ok": False,
                "classification": "retry_once_later",
                "error": "worker failed",
                "owned_artifact_identity": [31, 2],
            },
            prepare_output=lambda _targets: (17, 2),
            prepare_verifier=lambda _targets: (31, 2),
            cleanup_output=lambda _targets, _identity: None,
            cleanup_verifier=lambda _targets, _identity: None,
        )

        with mock.patch.object(
            output_process,
            "_record_worker_retry_cleanup",
            side_effect=OSError("audit write failed"),
        ), self.assertRaises(VerifierInfrastructureFailure) as raised:
            port.run_verifier(
                VerificationRunRequest(
                    SimpleNamespace(
                        snapshot_id="approved-1",
                        source_fingerprints_before=(),
                    ),
                    targets,
                    build_project_write_contract(_output_model()),
                    _project_artifact_evidence(),
                )
            )

        self.assertFalse(
            hasattr(raised.exception, "owned_artifact_identity")
        )

    def test_cancel_delegates_to_the_owned_child_process_runner(self):
        from spectrum_organizer.origin.output_process import OriginWorkerProcessPort

        child = SimpleNamespace(cancel_calls=0)
        child.cancel = lambda: setattr(child, "cancel_calls", child.cancel_calls + 1)
        port = OriginWorkerProcessPort(
            child_runner=child,
            prepare_output=lambda _targets: (1, 2),
            prepare_verifier=lambda _targets: (3, 4),
            cleanup_output=lambda targets, identity: None,
            cleanup_verifier=lambda targets, identity: None,
        )

        port.cancel()

        self.assertEqual(1, child.cancel_calls)

    def test_verifier_success_removes_its_mutation_copy_before_publication(self):
        from spectrum_organizer.origin.output_process import OriginWorkerProcessPort
        from spectrum_organizer.origin.output_worker import build_project_write_contract
        from spectrum_organizer.workflow.output_pipeline import VerificationRunRequest
        from tests.test_origin_worker_contracts import _output_model

        cleanups = []
        contract = build_project_write_contract(_output_model())
        snapshot = SimpleNamespace(
            snapshot_id="approved-1",
            source_fingerprints_before=(
                SimpleNamespace(path=pathlib.Path("source.opju")),
            ),
        )
        targets = SimpleNamespace(
            staging_dir=pathlib.Path("staging"),
            staging_identity=(0, 0),
            staging_project_path=pathlib.Path("staging/Organized_Spectra_20260802_120000.opju"),
            verifier_mutation_path=pathlib.Path("staging/Verifier_Mutation_20260802_120000.opju"),
        )
        port = OriginWorkerProcessPort(
            child_runner=lambda role, payload: {
                "ok": True,
                "project_artifact": _project_artifact_payload(),
            },
            prepare_output=lambda _targets: (1, 2),
            prepare_verifier=lambda _targets: (3, 4),
            cleanup_output=lambda targets, identity: None,
            cleanup_verifier=lambda targets, identity: cleanups.append("verifier"),
        )

        result = port.run_verifier(
            VerificationRunRequest(
                snapshot,
                targets,
                contract,
                _project_artifact_evidence(),
            )
        )

        self.assertEqual(["verifier"], cleanups)
        self.assertEqual(2, result.readback_spectrum_count)

    def test_child_process_crash_is_treated_as_retryable_infrastructure(self):
        from spectrum_organizer.origin.output_process import OriginWorkerProcessPort
        from spectrum_organizer.workflow.output_pipeline import OutputRunRequest
        from tests.test_origin_worker_contracts import _output_model

        calls = 0
        cleanups = []

        def child_runner(role, payload):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("pipe closed")
            return {
                "ok": True,
                "project_artifact": _project_artifact_payload(),
            }

        targets = SimpleNamespace(
            staging_dir=pathlib.Path("staging"),
            staging_identity=(0, 0),
            staging_project_path=pathlib.Path("staging/Organized_Spectra_20260802_120000.opju"),
            verifier_mutation_path=pathlib.Path("staging/Verifier_Mutation_20260802_120000.opju"),
        )
        port = OriginWorkerProcessPort(
            child_runner=child_runner,
            prepare_output=lambda _targets: (1, 2),
            prepare_verifier=lambda _targets: (3, 4),
            cleanup_output=lambda targets, identity: cleanups.append("output"),
            cleanup_verifier=lambda targets, identity: None,
        )

        result = port.run_output(
            OutputRunRequest(
                SimpleNamespace(snapshot_id="approved-1", output_plan=_output_model()),
                targets,
            )
        )

        self.assertEqual(2, calls)
        self.assertEqual(["output"], cleanups)
        self.assertEqual("succeeded", result.attempts[-1].status)


class QtOutputStageRunnerTests(unittest.TestCase):
    def test_real_qt_thread_delivers_progress_and_result_on_gui_thread(self):
        import os

        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6 import QtCore
        from spectrum_organizer.ui.output_stage import QtOutputStageRunner

        app = QtCore.QCoreApplication.instance() or QtCore.QCoreApplication([])
        del app
        main_thread = threading.get_ident()
        worker_threads = []
        callback_threads = []
        completed = threading.Event()

        class RunFunc:
            def set_progress_callback(self, callback):
                self.progress = callback

            def __call__(self, request):
                worker_threads.append(threading.get_ident())
                self.progress("write_output")
                return request

        runner = QtOutputStageRunner(QtCore, RunFunc())
        runner.start(
            "approved",
            lambda result: (
                callback_threads.append(("success", threading.get_ident(), result)),
                completed.set(),
            ),
            lambda error: (
                callback_threads.append(("error", threading.get_ident(), error)),
                completed.set(),
            ),
            lambda stage: callback_threads.append(
                ("progress", threading.get_ident(), stage)
            ),
        )

        loop = QtCore.QEventLoop()
        poll = QtCore.QTimer()
        poll.setInterval(1)
        poll.timeout.connect(lambda: completed.is_set() and loop.quit())
        timeout = QtCore.QTimer()
        timeout.setSingleShot(True)
        timeout.timeout.connect(loop.quit)
        poll.start()
        timeout.start(2_000)
        loop.exec()
        poll.stop()

        self.assertTrue(completed.is_set())
        self.assertNotEqual(main_thread, worker_threads[0])
        self.assertEqual(
            [main_thread, main_thread],
            [item[1] for item in callback_threads],
        )
        self.assertEqual(["progress", "success"], [item[0] for item in callback_threads])

    def test_retry_cleanup_runs_off_the_gui_thread_and_reports_completion(self):
        import os

        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6 import QtCore
        from spectrum_organizer.ui.output_stage import QtOutputStageRunner

        app = QtCore.QCoreApplication.instance() or QtCore.QCoreApplication([])
        del app
        main_thread = threading.get_ident()
        worker_threads = []
        callbacks = []
        completed = threading.Event()

        class RunFunc:
            def retry_cleanup(self):
                worker_threads.append(threading.get_ident())

        runner = QtOutputStageRunner(QtCore, RunFunc())
        self.assertTrue(
            runner.retry_cleanup(
                lambda error: (
                    callbacks.append((threading.get_ident(), error)),
                    completed.set(),
                )
            )
        )

        loop = QtCore.QEventLoop()
        poll = QtCore.QTimer()
        poll.setInterval(1)
        poll.timeout.connect(lambda: completed.is_set() and loop.quit())
        timeout = QtCore.QTimer()
        timeout.setSingleShot(True)
        timeout.timeout.connect(loop.quit)
        poll.start()
        timeout.start(2_000)
        loop.exec()
        poll.stop()

        self.assertTrue(completed.is_set())
        self.assertNotEqual(main_thread, worker_threads[0])
        self.assertEqual([(main_thread, None)], callbacks)
        self.assertEqual([], runner._threads)

    def test_runner_queues_progress_and_terminal_result_off_the_calling_stack(self):
        from spectrum_organizer.ui.output_stage import QtOutputStageRunner

        threads = []
        release = threading.Event()
        finished = threading.Event()
        calling_thread = threading.get_ident()
        run_threads = []

        class Signal:
            def __init__(self, *_args):
                self.callbacks = []

            def connect(self, callback):
                self.callbacks.append(callback)

            def emit(self, value=None):
                for callback in tuple(self.callbacks):
                    callback(value)

        class QThread:
            def __init__(self):
                self.finished = Signal()
                threads.append(self)

            def start(self):
                def execute():
                    release.wait()
                    self.run()
                    self.finished.emit()
                    finished.set()

                self.worker = threading.Thread(target=execute)
                self.worker.start()

            def requestInterruption(self):
                pass

            def deleteLater(self):
                pass

        class RunFunc:
            def __init__(self):
                self.progress = None

            def prepare(self):
                pass

            def set_progress_callback(self, callback):
                self.progress = callback

            def __call__(self, request):
                run_threads.append(threading.get_ident())
                self.progress("write_output")
                return ("done", request)

        events = []
        runner = QtOutputStageRunner(
            SimpleNamespace(QThread=QThread, Signal=Signal),
            RunFunc(),
        )

        runner.start(
            "approved",
            lambda result: events.append(("success", result)),
            lambda error: events.append(("error", error)),
            lambda stage: events.append(("progress", stage)),
        )

        self.assertEqual([], events)
        self.assertEqual(1, len(runner._threads))
        release.set()
        self.assertTrue(finished.wait(2))
        threads[0].worker.join()

        self.assertEqual(
            [
                ("progress", "write_output"),
                ("success", ("done", "approved")),
            ],
            events,
        )
        self.assertEqual(1, len(run_threads))
        self.assertNotEqual(calling_thread, run_threads[0])
        self.assertEqual([], runner._threads)

    def test_cancel_requests_worker_termination_and_notifies_after_thread_stops(self):
        from spectrum_organizer.ui.output_stage import QtOutputStageRunner

        class Signal:
            def __init__(self, *_args):
                self.callbacks = []

            def connect(self, callback):
                self.callbacks.append(callback)

            def emit(self, value=None):
                for callback in tuple(self.callbacks):
                    callback(value)

        class QThread:
            def __init__(self):
                self.finished = Signal()
                self.interrupted = False

            def start(self):
                pass

            def requestInterruption(self):
                self.interrupted = True

            def deleteLater(self):
                pass

        run_func = SimpleNamespace(cancel_calls=0)
        run_func.cancel = lambda: setattr(
            run_func,
            "cancel_calls",
            run_func.cancel_calls + 1,
        )
        runner = QtOutputStageRunner(
            SimpleNamespace(QThread=QThread, Signal=Signal),
            run_func,
        )
        stopped = []
        runner.start("approved", lambda result: None, lambda error: None)
        thread = runner._threads[0]

        self.assertTrue(runner.cancel(lambda: stopped.append(True)))
        self.assertEqual(1, run_func.cancel_calls)
        self.assertTrue(thread.interrupted)
        self.assertEqual([], stopped)

        thread.finished.emit()
        self.assertEqual([True], stopped)

    def test_terminal_callback_precedes_stopped_callback(self):
        from spectrum_organizer.ui.output_stage import QtOutputStageRunner

        class Signal:
            def __init__(self, *_args):
                self.callbacks = []

            def connect(self, callback):
                self.callbacks.append(callback)

            def emit(self, value=None):
                for callback in tuple(self.callbacks):
                    callback(value)

        class QThread:
            def __init__(self):
                self.finished = Signal()

            def start(self):
                pass

            def requestInterruption(self):
                pass

        run_func = SimpleNamespace(cancel=lambda: True)
        runner = QtOutputStageRunner(
            SimpleNamespace(QThread=QThread, Signal=Signal),
            run_func,
        )
        events = []
        runner.start(
            "approved",
            lambda result: events.append("success"),
            lambda error: events.append("error"),
        )
        runner.cancel(lambda: events.append("stopped"))
        runner._threads[0].result = "done"

        runner._threads[0].finished.emit()

        self.assertEqual(["success", "stopped"], events)

    def test_committed_job_refuses_cancel_without_registering_stopped_callback(self):
        from spectrum_organizer.ui.output_stage import QtOutputStageRunner

        class Signal:
            def __init__(self, *_args):
                self.callbacks = []

            def connect(self, callback):
                self.callbacks.append(callback)

        class QThread:
            def __init__(self):
                self.finished = Signal()
                self.interrupted = False

            def start(self):
                pass

            def requestInterruption(self):
                self.interrupted = True

        runner = QtOutputStageRunner(
            SimpleNamespace(QThread=QThread, Signal=Signal),
            SimpleNamespace(cancel=lambda: False, committed=True),
        )
        stopped = []
        runner.start("approved", lambda result: None, lambda error: None)

        self.assertFalse(runner.cancel(lambda: stopped.append(True)))
        self.assertEqual([], stopped)
        self.assertFalse(runner._threads[0].interrupted)


class OutputCompletionUiTests(unittest.TestCase):
    def test_complete_stage_shows_three_compact_actions_and_hides_cancel_task(self):
        import os

        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6 import QtWidgets
        from spectrum_organizer.ui.qt_main_window import (
            create_production_main_window,
        )

        window, widgets = create_production_main_window(
            dpi_percent=100,
            size_name="desktop",
            stage="complete",
        )
        try:
            window.show()
            QtWidgets.QApplication.processEvents()
            self.assertTrue(widgets["open_output_folder_button"].isVisible())
            self.assertTrue(widgets["start_new_task_button"].isVisible())
            self.assertTrue(widgets["exit_application_button"].isVisible())
            self.assertFalse(widgets["cancel_run_button"].isVisible())
            self.assertEqual(
                ("打开输出文件夹", "开始新任务", "退出"),
                tuple(
                    widgets[name].text()
                    for name in (
                        "open_output_folder_button",
                        "start_new_task_button",
                        "exit_application_button",
                    )
                ),
            )
        finally:
            window.close()
            QtWidgets.QApplication.processEvents()


if __name__ == "__main__":
    unittest.main()
