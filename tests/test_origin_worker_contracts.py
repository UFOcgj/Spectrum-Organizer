from decimal import Decimal
import hashlib
import json
import os
import pathlib
import shutil
import sys
from types import SimpleNamespace
import unittest
from unittest import mock
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from spectrum_organizer.core.output_model import OutputSpectrum, build_output_plan
from spectrum_organizer.domain.models import SpectrumClass
from spectrum_organizer.origin.contracts import (
    OriginStructureMismatchError,
    ProjectArtifactEvidence,
)
from spectrum_organizer.origin.output_worker import (
    DeterministicOutputError,
    InfrastructureOutputError,
    OutputInfrastructureFailure,
    OutputWorkerCommand,
    OutputWorkerPreflightError,
    build_project_write_contract,
    classify_output_error,
    run_output_with_infrastructure_retry,
    run_output_worker,
)
from spectrum_organizer.origin.verify_worker import (
    DeterministicVerificationError,
    InfrastructureVerificationError,
    VerificationMismatchError,
    VerifierInfrastructureFailure,
    VerifierWorkerCommand,
    VerifierWorkerPreflightError,
    classify_verifier_error,
    compare_project_contract,
    prove_live_dependency_on_mutation_copy,
    run_verifier_with_infrastructure_retry,
    run_verifier_worker,
    validate_verifier_command,
)
from spectrum_organizer.origin.output_process import _verifier_command_from_request
from spectrum_organizer.safety.identity_paths import path_identity


class WorkspaceTempDir:
    def __init__(self):
        self.root = ROOT / ".test-tmp" / "task11"
        self.path = self.root / f"case-{uuid.uuid4().hex}"

    def __enter__(self):
        self.path.mkdir(parents=True)
        return self.path

    def __exit__(self, exc_type, exc, tb):
        shutil.rmtree(self.path, ignore_errors=True)
        if self.root.exists() and not any(self.root.iterdir()):
            self.root.rmdir()


class OriginProcessIdentityAuditTests(unittest.TestCase):
    def test_records_exact_origin_session_identity_from_origin_process(self):
        from spectrum_organizer.origin import process_identity
        from spectrum_organizer.safety.process_boundary import ProcessInfo

        class FakeOrigin:
            def __init__(self):
                self.commands = []

            def lt_int(self, command):
                self.commands.append(command)
                return 0 if command.startswith("run.LoadOC") else 4321

        process = ProcessInfo(
            pid=4321,
            start_time_ns=987654321,
            visible=True,
            taskbar_visible=True,
            program_owned=False,
        )
        recorded = []
        with WorkspaceTempDir() as root:
            helper = root / "origin-current-pid.c"
            helper.write_text("int helper;\n", encoding="ascii")
            with (
                mock.patch.object(
                    process_identity,
                    "ORIGIN_PID_HELPER_PATH",
                    helper,
                ),
                mock.patch.object(
                    process_identity,
                    "runtime_audit_enabled",
                    return_value=True,
                ),
                mock.patch.object(
                    process_identity,
                    "default_origin_process_probe",
                    return_value=(process,),
                ),
                mock.patch.object(
                    process_identity,
                    "record_runtime_audit_event",
                    side_effect=lambda event_type, payload: recorded.append(
                        (event_type, payload)
                    ),
                ),
            ):
                identity = process_identity.record_origin_session_identity(
                    FakeOrigin(),
                    role="output",
                    attempt_binding={
                        "approved_snapshot_id": "approved-1",
                        "run_staging_root": str(root),
                        "attempt": 1,
                    },
                )

        self.assertEqual(process.identity, identity)
        self.assertEqual(
            [
                (
                    "origin_process_identity",
                    {
                        "role": "output",
                        "pid": 4321,
                        "start_time_ns": 987654321,
                        "attempt_binding": {
                            "approved_snapshot_id": "approved-1",
                            "run_staging_root": str(root),
                            "attempt": 1,
                        },
                    },
                )
            ],
            recorded,
        )

    def test_runtime_audit_is_durable_before_parent_handoff(self):
        from spectrum_organizer.origin import process_identity
        from spectrum_organizer.safety.process_boundary import ProcessInfo

        class FakeOrigin:
            def lt_int(self, command):
                return 0 if command.startswith("run.LoadOC") else 4321

        process = ProcessInfo(
            pid=4321,
            start_time_ns=987654321,
            visible=False,
            taskbar_visible=False,
            program_owned=True,
        )
        events = []
        publisher = process_identity._publish_identity_handoff
        with WorkspaceTempDir() as root:
            helper = root / "origin-current-pid.c"
            helper.write_text("int helper;\n", encoding="ascii")
            handoff = root / "origin-identity.json"
            handoff.write_text("", encoding="utf-8")
            environment = {
                process_identity.ORIGIN_IDENTITY_HANDOFF_PATH_ENV: str(
                    handoff
                ),
                process_identity.ORIGIN_IDENTITY_HANDOFF_TOKEN_ENV: "token-1",
            }
            with (
                mock.patch.dict(os.environ, environment, clear=False),
                mock.patch.object(
                    process_identity,
                    "ORIGIN_PID_HELPER_PATH",
                    helper,
                ),
                mock.patch.object(
                    process_identity,
                    "runtime_audit_enabled",
                    return_value=True,
                ),
                mock.patch.object(
                    process_identity,
                    "default_origin_process_probe",
                    return_value=(process,),
                ),
                mock.patch.object(
                    process_identity,
                    "record_runtime_audit_event",
                    side_effect=lambda *_args: events.append("audit"),
                ),
                mock.patch.object(
                    process_identity,
                    "_publish_identity_handoff",
                    side_effect=lambda path, payload: (
                        events.append("handoff"),
                        publisher(path, payload),
                    )[-1],
                ),
            ):
                process_identity.record_origin_session_identity(
                    FakeOrigin(),
                    role="output",
                    attempt_binding={
                        "approved_snapshot_id": "approved-1",
                        "run_staging_root": str(root),
                        "attempt": 1,
                    },
                )

        self.assertEqual(["audit", "handoff"], events)

    def test_publishes_exact_identity_handoff_without_runtime_audit(self):
        from spectrum_organizer.origin import process_identity
        from spectrum_organizer.safety.process_boundary import ProcessInfo

        class FakeOrigin:
            def lt_int(self, command):
                return 0 if command.startswith("run.LoadOC") else 4321

        process = ProcessInfo(
            pid=4321,
            start_time_ns=987654321,
            visible=False,
            taskbar_visible=False,
            program_owned=True,
        )
        with WorkspaceTempDir() as root:
            helper = root / "origin-current-pid.c"
            helper.write_text("int helper;\n", encoding="ascii")
            handoff = root / "origin-identity.json"
            handoff.write_text("", encoding="utf-8")
            binding = {
                "approved_snapshot_id": "approved-1",
                "run_staging_root": str(root),
                "attempt": 1,
            }
            environment = {
                process_identity.ORIGIN_IDENTITY_HANDOFF_PATH_ENV: str(handoff),
                process_identity.ORIGIN_IDENTITY_HANDOFF_TOKEN_ENV: "token-1",
            }
            with (
                mock.patch.dict(os.environ, environment, clear=False),
                mock.patch.object(
                    process_identity,
                    "ORIGIN_PID_HELPER_PATH",
                    helper,
                ),
                mock.patch.object(
                    process_identity,
                    "runtime_audit_enabled",
                    return_value=False,
                ),
                mock.patch.object(
                    process_identity,
                    "default_origin_process_probe",
                    return_value=(process,),
                ),
            ):
                identity = process_identity.record_origin_session_identity(
                    FakeOrigin(),
                    role="verifier",
                    attempt_binding=binding,
                )

            payload = json.loads(handoff.read_text(encoding="utf-8"))

        self.assertEqual(process.identity, identity)
        self.assertEqual(
            {
                "schema_version": 1,
                "token": "token-1",
                "role": "verifier",
                "attempt_binding": binding,
                "pid": 4321,
                "start_time_ns": 987654321,
            },
            payload,
        )


class FakeOutputSession:
    def __init__(self, events, root_path="/"):
        self.events = events
        self.root_path = root_path
        self.saved_path = None

    def new(self):
        self.events.append("op.new")

    def delete_default_template_book(self):
        self.events.append("delete_default_template_book")

    def root_folder_path(self):
        self.events.append("root_folder_path")
        return self.root_path

    def add_folder(self, root_path, folder_name):
        self.events.append(("RootFolder.Folders.Add", root_path))
        self.events.append(("Folder.SetName", folder_name))
        return folder_name

    def add_book(self, folder, display_long_name):
        self.events.append(("add_book", folder, display_long_name))
        return display_long_name

    def write_column(self, book, column):
        self.events.append(("write_column", book, column.short_name, column.designation, column.comment, column.values))
        if column.formula is not None:
            self.events.append(("set_formula", column.short_name, column.formula))
        if column.method is not None:
            self.events.append(("set_method", column.short_name, column.method))

    def method_row(self, column_short_name):
        for event in reversed(self.events):
            if event[:2] == ("set_method", column_short_name):
                return event[2]
        return None

    def save(self, path):
        self.events.append(("save", path))
        self.saved_path = path

    def close(self):
        self.events.append("close")


class FakeVerifierSession:
    def __init__(self, contract, events):
        self.contract = contract
        self.events = events

    def open(self, path, readonly):
        self.events.append(("open", path, readonly))

    def read_project_contract(self):
        self.events.append("read_project_contract")
        return self.contract

    def close(self):
        self.events.append("close")


class OriginWorkerContractTests(unittest.TestCase):
    def test_session_adapters_depend_on_shared_contracts_not_output_worker(self):
        session_source = (
            ROOT
            / "src"
            / "spectrum_organizer"
            / "origin"
            / "session_adapters.py"
        ).read_text(encoding="utf-8")
        verifier_source = (
            ROOT
            / "src"
            / "spectrum_organizer"
            / "origin"
            / "verify_worker.py"
        ).read_text(encoding="utf-8")

        self.assertIn(
            "from spectrum_organizer.origin.contracts import",
            session_source,
        )
        self.assertNotIn(
            "from spectrum_organizer.origin.output_worker import",
            session_source,
        )
        self.assertIn(
            "from spectrum_organizer.origin.contracts import",
            verifier_source,
        )
        self.assertNotIn(
            "from spectrum_organizer.origin.output_worker import",
            verifier_source,
        )

    def test_origin_session_launch_failures_are_retryable_infrastructure(self):
        with WorkspaceTempDir() as root:
            staging_root = root / "staging"
            staging_root.mkdir()
            target = staging_root / "Organized_Spectra_20260628_120000.opju"

            with self.assertRaises(InfrastructureOutputError):
                run_output_worker(
                    _output_command(target, staging_root),
                    process_preflight=lambda: None,
                    origin_loader=lambda: (_ for _ in ()).throw(
                        OSError("Origin launch failed")
                    ),
                )

            target.write_text("staged", encoding="utf-8")
            with self.assertRaises(InfrastructureVerificationError):
                run_verifier_worker(
                    _verifier_command(
                        target,
                        staging_root / "verify-mutation.opju",
                        staging_root,
                    ),
                    process_preflight=lambda: None,
                    origin_loader=lambda: (_ for _ in ()).throw(
                        OSError("Origin launch failed")
                    ),
                    dependency_proof=FakeDependencyProof(),
                )

    def test_verifier_structure_mismatch_is_deterministic_and_not_retryable(self):
        class StructurallyInvalidSession:
            origin = object()

            def open(self, path, readonly):
                del path, readonly

            def read_project_contract(self):
                raise OriginStructureMismatchError(
                    "Verifier encountered empty output folder"
                )

            def close(self):
                pass

        with WorkspaceTempDir() as root:
            staging_root = root / "staging"
            staging_root.mkdir()
            target = staging_root / "Organized_Spectra_20260628_120000.opju"
            target.write_text("staged", encoding="utf-8")

            with self.assertRaises(DeterministicVerificationError) as raised:
                run_verifier_worker(
                    _verifier_command(
                        target,
                        staging_root / "verify-mutation.opju",
                        staging_root,
                    ),
                    process_preflight=lambda: None,
                    origin_loader=StructurallyInvalidSession,
                    dependency_proof=FakeDependencyProof(),
                )

        self.assertIn("empty output folder", str(raised.exception))
        self.assertEqual("non_retryable", classify_verifier_error(raised.exception))

    def test_session_close_failure_does_not_mask_primary_output_failure(self):
        class FailingSession(FakeOutputSession):
            def new(self):
                raise OSError("Origin communication failed")

            def close(self):
                raise RuntimeError("Origin exit failed")

        with WorkspaceTempDir() as root:
            staging_root = root / "staging"
            staging_root.mkdir()
            target = staging_root / "Organized_Spectra_20260628_120000.opju"

            with self.assertRaises(InfrastructureOutputError) as raised:
                run_output_worker(
                    _output_command(target, staging_root),
                    process_preflight=lambda: None,
                    origin_loader=lambda: FailingSession([]),
                )

        self.assertIsInstance(raised.exception.__cause__, OSError)
        self.assertIn(
            "Origin session close also failed: Origin exit failed",
            getattr(raised.exception, "__notes__", ()),
        )

    def test_late_session_close_failures_preserve_created_cleanup_identity(self):
        class CloseFailingOutputSession(FakeOutputSession):
            def close(self):
                raise RuntimeError("output close failed")

        class CloseFailingVerifierSession(FakeVerifierSession):
            origin = object()

            def close(self):
                raise RuntimeError("verifier close failed")

        with WorkspaceTempDir() as root:
            staging_root = root / "staging"
            staging_root.mkdir()
            target = staging_root / "Organized_Spectra_20260628_120000.opju"
            with self.assertRaises(InfrastructureOutputError) as output_error:
                run_output_worker(
                    _output_command(target, staging_root),
                    process_preflight=lambda: None,
                    origin_loader=lambda: CloseFailingOutputSession([]),
                )
            self.assertEqual(
                path_identity(target),
                getattr(output_error.exception, "owned_artifact_identity", None),
            )

            staged = staging_root / "verified.opju"
            staged.write_bytes(b"project")
            mutation = staging_root / "mutation.opju"
            command = _verifier_command(staged, mutation, staging_root)
            with self.assertRaises(
                InfrastructureVerificationError
            ) as verifier_error:
                run_verifier_worker(
                    command,
                    process_preflight=lambda: None,
                    origin_loader=lambda: CloseFailingVerifierSession(
                        _origin_readback_contract(command.expected_contract),
                        [],
                    ),
                    dependency_proof=FakeDependencyProof(),
                )
            self.assertEqual(
                path_identity(mutation),
                getattr(
                    verifier_error.exception,
                    "owned_artifact_identity",
                    None,
                ),
            )

    def test_output_command_rejects_invalid_existing_missing_snapshot_and_outside_staging_before_import(self):
        with WorkspaceTempDir() as root:
            staging_root = root / "staging"
            staging_root.mkdir()
            allowed_target = staging_root / "Organized_Spectra_20260628_120000.opju"
            unallowlisted_target = staging_root / "Organized_Spectra_20260628_120003.opju"
            existing_target = staging_root / "Organized_Spectra_20260628_120001.opju"
            existing_target.write_text("do not overwrite", encoding="utf-8")
            original = root / "original.opju"
            original.write_text("original", encoding="utf-8")
            outside = root / "elsewhere" / "Organized_Spectra_20260628_120002.opju"

            cases = (
                _output_command(allowed_target, staging_root, approved_snapshot_id=""),
                _output_command(existing_target, staging_root),
                _output_command(original, staging_root),
                _output_command(outside, staging_root),
                _output_command(unallowlisted_target, staging_root, allowed_output_targets=(allowed_target,)),
            )
            for command in cases:
                imported = []
                with self.subTest(path=command.staging_project_path):
                    with self.assertRaises(OutputWorkerPreflightError):
                        run_output_worker(command, process_preflight=lambda: None, origin_loader=lambda: imported.append("originpro"))
                    self.assertEqual([], imported)

    def test_output_command_surface_contains_only_approved_model_and_staging_target(self):
        fields = set(OutputWorkerCommand.__dataclass_fields__)
        self.assertIn("approved_output_model", fields)
        self.assertIn("staging_project_path", fields)
        self.assertIn("allowed_output_targets", fields)
        self.assertNotIn("original_source_paths", fields)
        self.assertNotIn("source_paths", fields)
        self.assertNotIn("allowed_open_targets", fields)
        self.assertNotIn("protected_paths", fields)

    def test_output_worker_imports_after_preflight_then_writes_brand_new_book_contract(self):
        with WorkspaceTempDir() as root:
            staging_root = root / "staging"
            staging_root.mkdir()
            target = staging_root / "Organized_Spectra_20260628_120000.opju"
            events = []
            session = FakeOutputSession(events)
            command = _output_command(target, staging_root)

            run_output_worker(
                command,
                process_preflight=lambda: events.append("process_preflight"),
                origin_loader=lambda: events.append("originpro_imported") or session,
            )

            self.assertEqual("process_preflight", events[0])
            self.assertEqual("originpro_imported", events[1])
            self.assertIn("op.new", events)
            self.assertIn("delete_default_template_book", events)
            self.assertIn("root_folder_path", events)
            self.assertIn(("Folder.SetName", "F_Ex270_ExSlit2_EmSlit2_ALL_SAMPLES"), events)
            self.assertIn(("add_book", "F_Ex270_ExSlit2_EmSlit2_ALL_SAMPLES", "Sample-mTHF"), events)
            self.assertIn(("save", target), events)
            self.assertEqual("close", events[-1])

    def test_output_worker_records_the_exact_origin_session_identity(self):
        with WorkspaceTempDir() as root:
            staging_root = root / "staging"
            staging_root.mkdir()
            target = staging_root / "Organized_Spectra_20260628_120000.opju"
            session = FakeOutputSession([])
            session.origin = object()
            command = _output_command(target, staging_root)

            with mock.patch(
                "spectrum_organizer.origin.output_worker.record_origin_session_identity"
            ) as record_identity:
                run_output_worker(
                    command,
                    process_preflight=lambda: None,
                    origin_loader=lambda: session,
                )

            record_identity.assert_called_once_with(
                session.origin,
                role="output",
                attempt_binding={
                    "approved_snapshot_id": command.approved_snapshot_id,
                    "run_staging_root": str(command.run_staging_root),
                    "attempt": command.attempt,
                },
            )

    def test_identity_handoff_failure_still_closes_loaded_origin_sessions(self):
        with WorkspaceTempDir() as root:
            staging_root = root / "staging"
            staging_root.mkdir()
            output_target = (
                staging_root / "Organized_Spectra_20260628_120000.opju"
            )
            output_events = []
            output_session = FakeOutputSession(output_events)
            with mock.patch(
                "spectrum_organizer.origin.output_worker.record_origin_session_identity",
                side_effect=RuntimeError("handoff failed"),
            ), self.assertRaises(InfrastructureOutputError):
                run_output_worker(
                    _output_command(output_target, staging_root),
                    process_preflight=lambda: None,
                    origin_loader=lambda: output_session,
                )

            staged = staging_root / "verified.opju"
            staged.write_bytes(b"project")
            mutation = staging_root / "mutation.opju"
            verifier_command = _verifier_command(
                staged,
                mutation,
                staging_root,
            )
            verifier_events = []
            verifier_session = FakeVerifierSession(
                _origin_readback_contract(
                    verifier_command.expected_contract
                ),
                verifier_events,
            )
            with mock.patch(
                "spectrum_organizer.origin.verify_worker.record_origin_session_identity",
                side_effect=RuntimeError("handoff failed"),
            ), self.assertRaises(InfrastructureVerificationError):
                run_verifier_worker(
                    verifier_command,
                    process_preflight=lambda: None,
                    origin_loader=lambda: verifier_session,
                    dependency_proof=FakeDependencyProof(),
                )

        self.assertEqual(["close"], output_events)
        self.assertEqual(["close"], verifier_events)

    def test_output_worker_refuses_same_path_staging_root_replacement(self):
        with WorkspaceTempDir() as root:
            staging_root = root / "staging"
            staging_root.mkdir()
            target = staging_root / "Organized_Spectra_20260628_120000.opju"
            command = _output_command(target, staging_root)
            parked = root / "parked-approved-staging"
            staging_root.rename(parked)
            staging_root.mkdir()
            events = []

            with self.assertRaisesRegex(
                OutputWorkerPreflightError,
                "identity|staging",
            ):
                run_output_worker(
                    command,
                    process_preflight=lambda: None,
                    origin_loader=lambda: events.append("originpro")
                    or FakeOutputSession(events),
                )

            self.assertEqual([], events)
            self.assertFalse(target.exists())

    @unittest.skipUnless(os.name == "nt", "production no-replacement reservation is Windows-specific")
    def test_output_worker_reserves_target_identity_through_origin_save(self):
        with WorkspaceTempDir() as root:
            staging_root = root / "staging"
            staging_root.mkdir()
            target = staging_root / "Organized_Spectra_20260628_120000.opju"
            protected = root / "selected-original.opju"
            protected.write_bytes(b"immutable original")

            class ReplacingSaveSession(FakeOutputSession):
                def save(self, path):
                    os.link(protected, path)
                    pathlib.Path(path).write_bytes(b"overwritten")

            with self.assertRaises(InfrastructureOutputError):
                run_output_worker(
                    _output_command(target, staging_root),
                    process_preflight=lambda: None,
                    origin_loader=lambda: ReplacingSaveSession([]),
                )

            self.assertEqual(b"immutable original", protected.read_bytes())

    def test_output_worker_uses_origin_true_root_as_write_handle_not_verifier_contract(self):
        with WorkspaceTempDir() as root:
            staging_root = root / "staging"
            staging_root.mkdir()
            target = staging_root / "Organized_Spectra_20260628_120000.opju"
            events = []
            session = FakeOutputSession(events, root_path="/UNTITLED/")

            contract = run_output_worker(
                _output_command(target, staging_root),
                process_preflight=lambda: None,
                origin_loader=lambda: session,
            )

            self.assertEqual("/", contract.root_path)
            self.assertIn(("RootFolder.Folders.Add", "/UNTITLED/"), events)

    def test_write_contract_maps_norm_to_raw_with_emission_and_excitation_sections(self):
        contract = build_project_write_contract(_output_model_with_excitation())

        columns = contract.folders[0].books[0].columns
        self.assertEqual(
            (
                ("A", "X", "Em", None),
                ("B", "Y", "Sample-mTHF-77 K_F270", None),
                ("C", "Y", "Sample-mTHF-77 K_F270_Norm", "col(B)/max(col(B))"),
                ("D", "X", "Ex", None),
                ("E", "Y", "Sample-mTHF-77 K_FEx315", None),
                ("F", "Y", "Sample-mTHF-77 K_FEx315_Norm", "col(E)/max(col(E))"),
            ),
            tuple((column.short_name, column.designation, column.comment, column.formula) for column in columns),
        )
    def test_write_contract_uses_dense_sorted_x_raw_blanks_norm_formula_and_method_rows(self):
        contract = build_project_write_contract(_output_model())

        book = contract.folders[0].books[0]
        self.assertEqual("Sample-mTHF", book.display_long_name)
        self.assertIsNone(book.internal_short_name)
        self.assertEqual(
            (
                ("A", "X", "Em", (Decimal("300"), Decimal("300.5"), Decimal("301")), None, None),
                ("B", "Y", "Sample-mTHF-77 K_F270", (None, Decimal("5"), Decimal("15")), None, None),
                ("C", "Y", "Sample-mTHF-298 K_F270", (Decimal("10"), None, Decimal("20")), None, None),
                ("D", "Y", "Sample-mTHF-77 K_F270_Norm", (None, Decimal("0.3333333333333333"), Decimal("1")), "col(B)/max(col(B))", "Divided by Max of B"),
                ("E", "Y", "Sample-mTHF-298 K_F270_Norm", (Decimal("0.5"), None, Decimal("1")), "col(C)/max(col(C))", "Divided by Max of C"),
            ),
            tuple((column.short_name, column.designation, column.comment, column.values, column.formula, column.method) for column in book.columns),
        )

    def test_verifier_rejects_protected_missing_snapshot_outside_allowlist_and_existing_mutation_target_before_import(self):
        with WorkspaceTempDir() as root:
            staging_root = root / "staging"
            staging_root.mkdir()
            staged = staging_root / "Organized_Spectra_20260628_120000.opju"
            staged.write_text("staged", encoding="utf-8")
            mutation = staging_root / "verify-mutation.opju"
            existing_mutation = staging_root / "existing-mutation.opju"
            existing_mutation.write_text("existing", encoding="utf-8")
            original = root / "original.opju"
            protected_reference = root / "protected-reference.opju"
            original.write_text("original", encoding="utf-8")
            protected_reference.write_text("reference", encoding="utf-8")

            cases = (
                _verifier_command(staged, mutation, staging_root, approved_snapshot_id=""),
                _verifier_command(original, mutation, staging_root, protected_paths=(original, protected_reference)),
                _verifier_command(protected_reference, mutation, staging_root, protected_paths=(original, protected_reference)),
                _verifier_command(root / "outside.opju", mutation, staging_root, allowed_open_targets=(staged,)),
                _verifier_command(staged, existing_mutation, staging_root),
            )
            for command in cases:
                imported = []
                with self.subTest(path=command.staged_project_path):
                    with self.assertRaises(VerifierWorkerPreflightError):
                        run_verifier_worker(command, process_preflight=lambda: None, origin_loader=lambda: imported.append("originpro"), dependency_proof=FakeDependencyProof())
                    self.assertEqual([], imported)

    def test_verifier_rejects_staged_hardlink_to_any_protected_original(self):
        with WorkspaceTempDir() as root:
            original = root / "original.opju"
            original.write_text("original", encoding="utf-8")
            staging_root = root / "staging"
            staging_root.mkdir()
            staged = staging_root / "Organized_Spectra.opju"
            staged.hardlink_to(original)
            mutation = staging_root / "Verifier_Mutation.opju"

            with self.assertRaisesRegex(
                VerifierWorkerPreflightError,
                "protected|physical",
            ):
                validate_verifier_command(
                    _verifier_command(
                        staged,
                        mutation,
                        staging_root,
                        protected_paths=(original,),
                    )
                )

    @unittest.skipUnless(os.name == "nt", "production no-replacement lock is Windows-specific")
    def test_verifier_holds_staged_project_identity_through_readonly_open(self):
        with WorkspaceTempDir() as root:
            original = root / "selected-original.opju"
            original.write_bytes(b"immutable original")
            staging_root = root / "staging"
            staging_root.mkdir()
            staged = staging_root / "Organized_Spectra.opju"
            staged.write_bytes(b"owned staged project")
            parked = staging_root / "parked-staged.opju"
            command = _verifier_command(
                staged,
                staging_root / "Verifier_Mutation.opju",
                staging_root,
                protected_paths=(original,),
            )
            events = []

            class ReplacingVerifierSession(FakeVerifierSession):
                origin = object()

                def open(self, path, readonly):
                    pathlib.Path(path).rename(parked)
                    os.link(original, path)
                    events.append(("opened_original", os.path.samefile(path, original)))

            with self.assertRaises(
                (
                    InfrastructureVerificationError,
                    DeterministicVerificationError,
                )
            ):
                run_verifier_worker(
                    command,
                    process_preflight=lambda: None,
                    origin_loader=lambda: ReplacingVerifierSession(
                        _origin_readback_contract(command.expected_contract),
                        events,
                    ),
                    dependency_proof=FakeDependencyProof(),
                )

            self.assertNotIn(("opened_original", True), events)
            self.assertEqual(b"immutable original", original.read_bytes())

    def test_verifier_command_protects_every_selected_source_not_only_recognized_sources(self):
        recognized = pathlib.Path("C:/raw/recognized.opju")
        rejected = pathlib.Path("C:/raw/rejected.opju")
        targets = SimpleNamespace(
            staging_project_path=pathlib.Path("C:/out/staging/project.opju"),
            verifier_mutation_path=pathlib.Path("C:/out/staging/mutation.opju"),
            staging_dir=pathlib.Path("C:/out/staging"),
            staging_identity=(1, 2),
        )
        snapshot = SimpleNamespace(
            snapshot_id="approved-1",
            source_fingerprints_before=(SimpleNamespace(path=recognized),),
            selected_source_fingerprints_before=(
                SimpleNamespace(path=recognized),
                SimpleNamespace(path=rejected),
            ),
        )

        command = _verifier_command_from_request(
            SimpleNamespace(
                approved_snapshot=snapshot,
                targets=targets,
                expected_contract=build_project_write_contract(
                    _output_model()
                ),
            )
        )

        self.assertEqual((recognized, rejected), command.protected_paths)

    def test_verifier_opens_staged_project_readonly_in_fresh_worker_and_reports_exact_mismatch(self):
        with WorkspaceTempDir() as root:
            staging_root = root / "staging"
            staging_root.mkdir()
            staged = staging_root / "Organized_Spectra_20260628_120000.opju"
            staged.write_text("staged", encoding="utf-8")
            mutation = staging_root / "verify-mutation.opju"
            expected = build_project_write_contract(_output_model())
            actual = _origin_readback_contract(expected).with_replaced_value(folder_index=0, book_index=0, column_index=1, row_index=0, value=Decimal("99"))
            events = []
            command = _verifier_command(staged, mutation, staging_root, expected_contract=expected)

            with self.assertRaises(VerificationMismatchError) as raised:
                run_verifier_worker(
                    command,
                    process_preflight=lambda: events.append("process_preflight"),
                    origin_loader=lambda: events.append("originpro_imported") or FakeVerifierSession(actual, events),
                    dependency_proof=FakeDependencyProof(),
                )

            self.assertEqual(("open", staged, True), events[2])
            report = raised.exception.report
            self.assertEqual("F_Ex270_ExSlit2_EmSlit2_ALL_SAMPLES/Sample-mTHF", report.structural_path)
            self.assertEqual("B", report.column)
            self.assertEqual(1, report.row)
            self.assertIsNone(report.expected)
            self.assertEqual(Decimal("99"), report.actual)
            self.assertEqual("blank_mask", report.mismatch_class)
            self.assertEqual("close", events[-1])

    def test_verifier_success_proves_live_dependency_on_mutation_copy_for_each_raw_norm_pair(self):
        with WorkspaceTempDir() as root:
            staging_root = root / "staging"
            staging_root.mkdir()
            staged = staging_root / "Organized_Spectra_20260628_120000.opju"
            staged.write_text("staged", encoding="utf-8")
            mutation = staging_root / "verify-mutation.opju"
            expected = build_project_write_contract(_output_model_with_excitation())
            proof = FakeDependencyProof()
            events = []
            command = _verifier_command(staged, mutation, staging_root, expected_contract=expected)

            run_verifier_worker(
                command,
                process_preflight=lambda: events.append("process_preflight"),
                origin_loader=lambda: events.append("originpro_imported") or FakeVerifierSession(_origin_readback_contract(expected), events),
                dependency_proof=proof,
            )

            self.assertEqual(("open", staged, True), events[2])
            self.assertEqual(
                [
                    ("open", mutation, False),
                    ("assert_raw_to_norm_live", "F_Ex270_ExSlit2_EmSlit2_ALL_SAMPLES", "Sample-mTHF", "B", "C"),
                    ("assert_raw_to_norm_live", "F_Ex270_ExSlit2_EmSlit2_ALL_SAMPLES", "Sample-mTHF", "E", "F"),
                ],
                proof.events,
            )
            self.assertEqual("close", events[-1])

    def test_verifier_worker_records_the_exact_origin_session_identity(self):
        with WorkspaceTempDir() as root:
            staging_root = root / "staging"
            staging_root.mkdir()
            staged = staging_root / "Organized_Spectra_20260628_120000.opju"
            staged.write_text("staged", encoding="utf-8")
            mutation = staging_root / "verify-mutation.opju"
            expected = build_project_write_contract(_output_model())
            session = FakeVerifierSession(_origin_readback_contract(expected), [])
            session.origin = object()
            command = _verifier_command(
                staged,
                mutation,
                staging_root,
                expected_contract=expected,
            )

            with mock.patch(
                "spectrum_organizer.origin.verify_worker.record_origin_session_identity"
            ) as record_identity:
                run_verifier_worker(
                    command,
                    process_preflight=lambda: None,
                    origin_loader=lambda: session,
                    dependency_proof=FakeDependencyProof(),
                )

            record_identity.assert_called_once_with(
                session.origin,
                role="verifier",
                attempt_binding={
                    "approved_snapshot_id": command.approved_snapshot_id,
                    "run_staging_root": str(command.run_staging_root),
                    "attempt": command.attempt,
                },
            )

    def test_verifier_compares_project_root_path(self):
        expected = build_project_write_contract(_output_model())
        actual = _origin_readback_contract(expected).__class__("/wrong", _origin_readback_contract(expected).folders)

        report = compare_project_contract(expected, actual)

        self.assertIsNotNone(report)
        self.assertEqual("structure", report.mismatch_class)
        self.assertEqual("root", report.structural_path)
        self.assertEqual("/", report.expected)
        self.assertEqual("/wrong", report.actual)

    def test_verifier_ignores_origin_sibling_folder_enumeration_order(self):
        model = build_output_plan(
            (
                OutputSpectrum(
                    "em-270",
                    SpectrumClass.STEADY_EMISSION,
                    "Sample-A-298 K",
                    "Sample-A",
                    "298 K",
                    key_wavelength="270",
                    excitation_slit="2",
                    emission_slit="2",
                    x_y=(("300", "10"), ("301", "20")),
                ),
                OutputSpectrum(
                    "em-280",
                    SpectrumClass.STEADY_EMISSION,
                    "Sample-B-298 K",
                    "Sample-B",
                    "298 K",
                    key_wavelength="280",
                    excitation_slit="2",
                    emission_slit="2",
                    x_y=(("300", "5"), ("301", "15")),
                ),
            )
        )
        expected = build_project_write_contract(model)
        actual = _origin_readback_contract(expected)
        reordered = actual.__class__(
            actual.root_path,
            tuple(reversed(actual.folders)),
        )

        self.assertIsNone(compare_project_contract(expected, reordered))

    def test_verifier_ignores_origin_sibling_book_enumeration_order(self):
        expected = build_project_write_contract(_output_model_two_books())
        actual = _origin_readback_contract(expected)
        folder = actual.folders[0]
        reordered = actual.__class__(
            actual.root_path,
            (
                folder.__class__(
                    folder.path,
                    tuple(reversed(folder.books)),
                ),
            ),
        )

        self.assertIsNone(compare_project_contract(expected, reordered))

    def test_verifier_reports_folder_book_and_row_count_structure_mismatches(self):
        expected = build_project_write_contract(_output_model())
        actual_base = _origin_readback_contract(expected)
        folder = actual_base.folders[0]
        book = folder.books[0]
        first_column = book.columns[0]
        cases = (
            (actual_base.__class__(actual_base.root_path, actual_base.folders + (folder.__class__(folder.path + "_copy", folder.books),)), 1, 2),
            (actual_base.__class__(actual_base.root_path, (folder.__class__("WrongFolder", folder.books),)), "F_Ex270_ExSlit2_EmSlit2_ALL_SAMPLES", "WrongFolder"),
            (actual_base.__class__(actual_base.root_path, (folder.__class__(folder.path, folder.books + (book.__class__(book.display_long_name + " copy", "BookExtra", book.columns),)),)), 1, 2),
            (actual_base.__class__(actual_base.root_path, (folder.__class__(folder.path, (book.__class__("WrongBook", book.internal_short_name, book.columns),)),)), "Sample-mTHF", "WrongBook"),
            (actual_base.__class__(actual_base.root_path, (folder.__class__(folder.path, (book.with_replaced_column(0, first_column.__class__(first_column.short_name, first_column.designation, first_column.comment, first_column.values[:-1], first_column.formula, first_column.method)),)),)), 3, 2),
        )
        for actual, expected_detail, actual_detail in cases:
            with self.subTest(expected=expected_detail, actual=actual_detail):
                report = compare_project_contract(expected, actual)
                self.assertIsNotNone(report)
                self.assertEqual("structure", report.mismatch_class)
                self.assertEqual(expected_detail, report.expected)
                self.assertEqual(actual_detail, report.actual)
    def test_verifier_requires_nonempty_unique_internal_short_names_from_origin_readback(self):
        expected = build_project_write_contract(_output_model())
        actual_missing = _origin_readback_contract(expected).with_replaced_book_short_name(0, 0, "")
        report = compare_project_contract(expected, actual_missing)
        self.assertIsNotNone(report)
        self.assertEqual("metadata", report.mismatch_class)
        self.assertEqual("internal_short_name", report.column)

        actual_invalid = _origin_readback_contract(expected).with_replaced_book_short_name(
            0,
            0,
            "bad name",
        )
        report = compare_project_contract(expected, actual_invalid)
        self.assertIsNotNone(report)
        self.assertEqual("metadata", report.mismatch_class)
        self.assertEqual("internal_short_name", report.column)

        expected_two_books = build_project_write_contract(_output_model_two_books())
        duplicate_actual = _origin_readback_contract(expected_two_books).with_replaced_book_short_name(0, 1, "Book1")
        report = compare_project_contract(expected_two_books, duplicate_actual)
        self.assertIsNotNone(report)
        self.assertEqual("metadata", report.mismatch_class)
        self.assertEqual("internal_short_name", report.column)

        case_collision = _origin_readback_contract(
            expected_two_books
        ).with_replaced_book_short_name(0, 1, "book1")
        report = compare_project_contract(expected_two_books, case_collision)
        self.assertIsNotNone(report)
        self.assertEqual("metadata", report.mismatch_class)
        self.assertEqual("internal_short_name", report.column)
    def test_verifier_compares_blank_masks_before_numeric_values(self):
        expected = build_project_write_contract(_output_model())
        actual = _origin_readback_contract(expected).with_replaced_value(0, 0, 2, 0, Decimal("11")).with_replaced_value(0, 0, 2, 2, None)

        report = compare_project_contract(expected, actual)

        self.assertIsNotNone(report)
        self.assertEqual("blank_mask", report.mismatch_class)
        self.assertEqual(3, report.row)
        self.assertEqual(Decimal("20"), report.expected)
        self.assertIsNone(report.actual)

    def test_verifier_accepts_origin_ieee_double_roundtrip_values(self):
        expected = build_project_write_contract(_output_model())
        high_precision_value = expected.folders[0].books[0].columns[3].values[1]
        actual_value = Decimal(str(float(high_precision_value)))
        actual = _origin_readback_contract(expected).with_replaced_value(0, 0, 3, 1, actual_value)

        report = compare_project_contract(expected, actual)

        self.assertIsNone(report)

    def test_verifier_rejects_unbound_formula_engine_rounding(self):
        expected = build_project_write_contract(_output_model()).with_replaced_value(
            0,
            0,
            3,
            1,
            Decimal("0.07175401019425014134249948656"),
        )
        actual = _origin_readback_contract(expected).with_replaced_value(
            0,
            0,
            3,
            1,
            Decimal("0.07175401019425015"),
        )

        report = compare_project_contract(expected, actual)

        self.assertIsNotNone(report)
        self.assertEqual("numeric", report.mismatch_class)

    def test_verifier_rejects_every_distinct_finite_numeric_value(self):
        base = build_project_write_contract(_output_model())
        cases = (
            ("large integer step", "1000000000000", "1000000000001"),
            ("small decimal step", "1.0000000000000", "1.0000000000005"),
            ("float collapse", "9007199254740992", "9007199254740993"),
        )

        for label, expected_value, actual_value in cases:
            with self.subTest(label=label):
                expected = base.with_replaced_value(
                    0,
                    0,
                    0,
                    0,
                    Decimal(expected_value),
                )
                actual = _origin_readback_contract(
                    expected
                ).with_replaced_value(
                    0,
                    0,
                    0,
                    0,
                    Decimal(actual_value),
                )

                report = compare_project_contract(expected, actual)

                self.assertIsNotNone(report)
                self.assertEqual("numeric", report.mismatch_class)

    def test_verifier_dependency_proof_infrastructure_failure_retries_once(self):
        with WorkspaceTempDir() as root:
            staging_root = root / "staging"
            staging_root.mkdir()
            staged = staging_root / "Organized_Spectra_20260628_120000.opju"
            staged.write_text("staged", encoding="utf-8")
            mutation = staging_root / "verify-mutation.opju"
            expected = build_project_write_contract(_output_model())
            events = []
            command = _verifier_command(staged, mutation, staging_root, expected_contract=expected)

            def verifier_factory(attempt):
                events.append(("factory", attempt))
                def verifier(_command):
                    run_verifier_worker(
                        _command,
                        process_preflight=lambda: events.append("process_preflight"),
                        origin_loader=lambda: events.append("originpro_imported") or FakeVerifierSession(_origin_readback_contract(expected), events),
                        dependency_proof=FailingDependencyProof(),
                    )
                return verifier

            with self.assertRaises(VerifierInfrastructureFailure):
                run_verifier_with_infrastructure_retry(
                    command,
                    verifier_factory,
                    cleanup_attempt=lambda _attempt: mutation.write_bytes(b""),
                )

            self.assertEqual(
                [("factory", 1), ("factory", 2)],
                [
                    event
                    for event in events
                    if isinstance(event, tuple) and event[0] == "factory"
                ],
            )
            self.assertIn("read_project_contract", events)

    def test_verifier_mutation_copy_setup_failure_retries_once(self):
        with WorkspaceTempDir() as root:
            staging_root = root / "staging"
            staging_root.mkdir()
            staged = staging_root / "Organized_Spectra_20260628_120000.opju"
            staged.write_text("staged", encoding="utf-8")
            expected = build_project_write_contract(_output_model())
            command = _verifier_command(
                staged,
                staging_root / "verify-mutation.opju",
                staging_root,
                expected_contract=expected,
            )
            attempts = []

            def verifier_factory(attempt):
                attempts.append(attempt)
                return lambda worker_command: run_verifier_worker(
                    worker_command,
                    process_preflight=lambda: None,
                    origin_loader=lambda: FakeVerifierSession(
                        _origin_readback_contract(expected),
                        [],
                    ),
                    dependency_proof=FakeDependencyProof(),
                )

            with (
                mock.patch(
                    "spectrum_organizer.origin.verify_worker._copy_mutation_exclusive",
                    side_effect=InfrastructureVerificationError(
                        "mutation copy write failed"
                    ),
                ),
                self.assertRaises(VerifierInfrastructureFailure),
            ):
                run_verifier_with_infrastructure_retry(command, verifier_factory)

        self.assertEqual([1, 2], attempts)

    def test_generic_dependency_proof_mismatch_is_deterministic_and_not_retried(self):
        class RuntimeMismatchProof(FakeDependencyProof):
            def assert_raw_to_norm_live(
                self,
                folder_path,
                book_display_name,
                raw_column_short_name,
                norm_column_short_name,
            ):
                raise RuntimeError("Raw-to-Norm dependency did not update")

        with WorkspaceTempDir() as root:
            staging_root = root / "staging"
            staging_root.mkdir()
            staged = staging_root / "Organized_Spectra_20260628_120000.opju"
            staged.write_text("staged", encoding="utf-8")
            expected = build_project_write_contract(_output_model())
            command = _verifier_command(
                staged,
                staging_root / "verify-mutation.opju",
                staging_root,
                expected_contract=expected,
            )
            attempts = []

            def verifier_factory(attempt):
                attempts.append(attempt)
                return lambda worker_command: run_verifier_worker(
                    worker_command,
                    process_preflight=lambda: None,
                    origin_loader=lambda: FakeVerifierSession(
                        _origin_readback_contract(expected),
                        [],
                    ),
                    dependency_proof=RuntimeMismatchProof(),
                )

            with self.assertRaises(VerificationMismatchError) as raised:
                run_verifier_with_infrastructure_retry(
                    command,
                    verifier_factory,
                )

        self.assertEqual([1], attempts)
        self.assertIn("F_Ex270_ExSlit2_EmSlit2_ALL_SAMPLES", str(raised.exception))
        self.assertIn("Sample-mTHF", str(raised.exception))
        self.assertIn("column=B", str(raised.exception))
        self.assertIn("class=dependency", str(raised.exception))

    def test_dependency_proof_rejects_missing_explicit_automatic_formula_lock_state(self):
        from spectrum_organizer.origin.verify_worker import (
            prove_live_dependency_on_mutation_copy,
        )

        expected = build_project_write_contract(_output_model())

        class MissingCalculationStateProof(FakeDependencyProof):
            def assert_raw_to_norm_live(self, *args):
                super().assert_raw_to_norm_live(*args)
                return None

        with WorkspaceTempDir() as staging_root:
            staged = staging_root / "staged.opju"
            staged.write_text("staged", encoding="utf-8")
            command = _verifier_command(
                staged,
                staging_root / "mutation.opju",
                staging_root,
                expected_contract=expected,
            )

            with self.assertRaisesRegex(
                VerificationMismatchError,
                "calculation state",
            ):
                prove_live_dependency_on_mutation_copy(
                    command,
                    MissingCalculationStateProof(),
                )

    def test_verifier_compares_formula_method_structure_rows_and_finite_values(self):
        expected = build_project_write_contract(_output_model())
        actual_base = _origin_readback_contract(expected)
        cases = (
            (actual_base.with_replaced_formula(0, 0, 3, "bad"), "formula", None, None),
            (actual_base.with_replaced_method(0, 0, 3, "bad"), "method", None, None),
            (actual_base.with_replaced_comment(0, 0, 1, "bad"), "metadata", None, None),
            (actual_base.with_replaced_value(0, 0, 1, 2, Decimal("11")), "numeric", None, None),
            (actual_base.with_replaced_value(0, 0, 1, 2, Decimal("NaN")), "finite", None, None),
            (actual_base.with_removed_column(0, 0), "structure", 5, 4),
        )
        for actual, mismatch_class, expected_detail, actual_detail in cases:
            with self.subTest(mismatch_class=mismatch_class):
                report = compare_project_contract(expected, actual)
                self.assertIsNotNone(report)
                self.assertEqual(mismatch_class, report.mismatch_class)
                if expected_detail is not None:
                    self.assertEqual(expected_detail, report.expected)
                    self.assertEqual(actual_detail, report.actual)

    def test_live_dependency_proof_uses_only_verifier_owned_mutation_copy(self):
        with WorkspaceTempDir() as root:
            staging_root = root / "staging"
            staging_root.mkdir()
            staged = staging_root / "Organized_Spectra_20260628_120000.opju"
            staged.write_text("staged", encoding="utf-8")
            mutation = staging_root / "verify-mutation.opju"
            command = _verifier_command(staged, mutation, staging_root)
            proof = FakeDependencyProof()

            prove_live_dependency_on_mutation_copy(command, proof)

            self.assertEqual(
                [
                    ("open", mutation, False),
                    ("assert_raw_to_norm_live", "F_Ex270_ExSlit2_EmSlit2_ALL_SAMPLES", "Sample-mTHF", "B", "D"),
                    ("assert_raw_to_norm_live", "F_Ex270_ExSlit2_EmSlit2_ALL_SAMPLES", "Sample-mTHF", "C", "E"),
                ],
                proof.events,
            )

    def test_live_dependency_copy_is_exclusive_and_never_delegates_destination_creation(self):
        with WorkspaceTempDir() as root:
            staging_root = root / "staging"
            staging_root.mkdir()
            staged = staging_root / "Organized_Spectra.opju"
            staged.write_bytes(b"owned staged project")
            protected = root / "selected-original.opju"
            protected.write_bytes(b"immutable original")
            mutation = staging_root / "Verifier_Mutation.opju"
            command = _verifier_command(
                staged,
                mutation,
                staging_root,
                protected_paths=(protected,),
            )

            class UnsafeCopyProof(FakeDependencyProof):
                def __init__(self):
                    super().__init__()
                    self.copy_called = False

                def copy_for_mutation(self, staged_path, mutation_path):
                    self.copy_called = True
                    os.link(protected, mutation_path)
                    shutil.copy2(staged_path, mutation_path)

            proof = UnsafeCopyProof()

            prove_live_dependency_on_mutation_copy(command, proof)

            self.assertFalse(proof.copy_called)
            self.assertEqual(b"immutable original", protected.read_bytes())
            self.assertEqual(b"owned staged project", mutation.read_bytes())

    @unittest.skipUnless(os.name == "nt", "production no-replacement lock is Windows-specific")
    def test_live_dependency_copy_does_not_adopt_replacement_before_identity_lock(self):
        from spectrum_organizer.origin import verify_worker

        with WorkspaceTempDir() as root:
            staging_root = root / "staging"
            staging_root.mkdir()
            staged = staging_root / "Organized_Spectra.opju"
            staged.write_bytes(b"owned staged project")
            foreign = root / "foreign.opju"
            foreign.write_bytes(b"foreign")
            mutation = staging_root / "Verifier_Mutation.opju"
            parked = staging_root / "parked-owned-mutation.opju"
            command = _verifier_command(staged, mutation, staging_root)
            proof = FakeDependencyProof()
            real_identity = verify_worker.path_identity
            replaced = False

            def replace_before_adoption(path):
                nonlocal replaced
                path = pathlib.Path(path)
                if path == mutation and path.exists() and not replaced:
                    replaced = True
                    path.rename(parked)
                    os.link(foreign, path)
                return real_identity(path)

            with mock.patch.object(
                verify_worker,
                "path_identity",
                side_effect=replace_before_adoption,
            ), self.assertRaises(VerifierWorkerPreflightError):
                prove_live_dependency_on_mutation_copy(command, proof)

            self.assertNotIn(("open", mutation, False), proof.events)
            self.assertEqual(b"foreign", foreign.read_bytes())

    @unittest.skipUnless(os.name == "nt", "production no-replacement lock is Windows-specific")
    def test_live_dependency_open_holds_mutation_identity_against_path_replacement(self):
        with WorkspaceTempDir() as root:
            staging_root = root / "staging"
            staging_root.mkdir()
            staged = staging_root / "Organized_Spectra.opju"
            staged.write_bytes(b"owned staged project")
            protected = root / "selected-original.opju"
            protected.write_bytes(b"immutable original")
            mutation = staging_root / "Verifier_Mutation.opju"
            parked = staging_root / "parked-owned-mutation.opju"
            command = _verifier_command(
                staged,
                mutation,
                staging_root,
                protected_paths=(protected,),
            )

            class ReplacingOpenProof(FakeDependencyProof):
                def open(self, path, readonly):
                    pathlib.Path(path).rename(parked)
                    os.link(protected, path)
                    self.events.append(("opened_protected_alias", os.path.samefile(path, protected)))

            proof = ReplacingOpenProof()

            with self.assertRaises(InfrastructureVerificationError):
                prove_live_dependency_on_mutation_copy(command, proof)

            self.assertNotIn(("opened_protected_alias", True), proof.events)
            self.assertEqual(b"immutable original", protected.read_bytes())

    def test_output_infrastructure_retry_uses_fresh_worker_once_cleans_attempt_and_reports_both_failures(self):
        command = _output_command(pathlib.Path("staging") / "Organized_Spectra_20260628_120000.opju", pathlib.Path("staging"))
        created = []
        cleaned = []

        def worker_factory(attempt):
            created.append(attempt)
            def worker(_command):
                if attempt == 1:
                    raise InfrastructureOutputError("launch failed")
                return build_project_write_contract(_output_model())
            return worker

        result = run_output_with_infrastructure_retry(command, worker_factory, lambda attempt: cleaned.append(attempt))

        self.assertEqual([1, 2], created)
        self.assertEqual([1], cleaned)
        self.assertEqual(("infrastructure_failed", "succeeded"), tuple(attempt.status for attempt in result.attempts))

        created.clear()
        cleaned.clear()
        def failing_factory(attempt):
            created.append(attempt)
            raise InfrastructureOutputError(f"failed {attempt}")

        with self.assertRaises(OutputInfrastructureFailure) as raised:
            run_output_with_infrastructure_retry(command, failing_factory, lambda attempt: cleaned.append(attempt))

        self.assertEqual([1, 2], created)
        self.assertEqual([1, 2], cleaned)
        self.assertEqual((1, 2), tuple(attempt.attempt for attempt in raised.exception.attempts))

    def test_output_retry_cleanup_failure_preserves_attempt_evidence(self):
        command = _output_command(
            pathlib.Path("staging") / "Organized_Spectra_20260628_120000.opju",
            pathlib.Path("staging"),
        )

        owned_identity = (17, 29)

        def fail_worker(attempt):
            error = InfrastructureOutputError(f"failed {attempt}")
            error.owned_artifact_identity = owned_identity
            raise error

        with self.assertRaises(OutputInfrastructureFailure) as raised:
            run_output_with_infrastructure_retry(
                command,
                lambda attempt: lambda _command: fail_worker(attempt),
                lambda attempt: (_ for _ in ()).throw(
                    OSError(f"cleanup failed {attempt}")
                ),
            )

        self.assertEqual((1,), tuple(item.attempt for item in raised.exception.attempts))
        self.assertIn(
            "cleanup attempt 1 also failed: cleanup failed 1",
            getattr(raised.exception, "__notes__", ()),
        )
        self.assertEqual(
            owned_identity,
            getattr(raised.exception, "owned_artifact_identity", None),
        )

    def test_output_final_retry_does_not_propagate_retired_identity(self):
        command = _output_command(
            pathlib.Path("staging") / "Organized_Spectra_20260628_120000.opju",
            pathlib.Path("staging"),
        )
        cleaned = []

        def worker_factory(attempt):
            def worker(_command):
                error = InfrastructureOutputError(f"failed {attempt}")
                error.owned_artifact_identity = (17, attempt)
                raise error

            return worker

        with self.assertRaises(OutputInfrastructureFailure) as raised:
            run_output_with_infrastructure_retry(
                command,
                worker_factory,
                lambda attempt: cleaned.append(attempt),
            )

        self.assertEqual([1, 2], cleaned)
        self.assertFalse(
            hasattr(raised.exception, "owned_artifact_identity")
        )

    def test_deterministic_output_error_is_not_retried_or_cleaned(self):
        command = _output_command(pathlib.Path("staging") / "Organized_Spectra_20260628_120000.opju", pathlib.Path("staging"))
        created = []
        cleaned = []

        def worker_factory(attempt):
            created.append(attempt)
            def worker(_command):
                raise DeterministicOutputError("rule failure")
            return worker

        with self.assertRaises(DeterministicOutputError):
            run_output_with_infrastructure_retry(command, worker_factory, lambda attempt: cleaned.append(attempt))

        self.assertEqual([1], created)
        self.assertEqual([], cleaned)

    def test_verifier_infrastructure_retry_uses_fresh_worker_once_and_reports_both_failures(self):
        command = _verifier_command(pathlib.Path("staging") / "Organized_Spectra_20260628_120000.opju", pathlib.Path("staging") / "mutation.opju", pathlib.Path("staging"))
        created = []
        cleaned = []

        def verifier_factory(attempt):
            created.append(attempt)
            def verifier(_command):
                if attempt == 1:
                    raise InfrastructureVerificationError("communication lost")
            return verifier

        result = run_verifier_with_infrastructure_retry(
            command,
            verifier_factory,
            cleanup_attempt=lambda attempt: cleaned.append(attempt),
        )

        self.assertEqual([1, 2], created)
        self.assertEqual([1], cleaned)
        self.assertEqual(("infrastructure_failed", "succeeded"), tuple(attempt.status for attempt in result.attempts))
        self.assertEqual(2, result.readback_spectrum_count)
        self.assertEqual(5, result.readback_column_count)

        created.clear()
        cleaned.clear()
        def failing_factory(attempt):
            created.append(attempt)
            raise InfrastructureVerificationError(f"failed {attempt}")

        with self.assertRaises(VerifierInfrastructureFailure) as raised:
            run_verifier_with_infrastructure_retry(
                command,
                failing_factory,
                cleanup_attempt=lambda attempt: cleaned.append(attempt),
            )

        self.assertEqual([1, 2], created)
        self.assertEqual([1, 2], cleaned)
        self.assertEqual((1, 2), tuple(attempt.attempt for attempt in raised.exception.attempts))

    def test_verifier_retry_cleanup_failure_preserves_attempt_evidence(self):
        command = _verifier_command(
            pathlib.Path("staging") / "Organized_Spectra_20260628_120000.opju",
            pathlib.Path("staging") / "mutation.opju",
            pathlib.Path("staging"),
        )

        owned_identity = (31, 43)

        def fail_verifier(attempt):
            error = InfrastructureVerificationError(f"failed {attempt}")
            error.owned_artifact_identity = owned_identity
            raise error

        with self.assertRaises(VerifierInfrastructureFailure) as raised:
            run_verifier_with_infrastructure_retry(
                command,
                lambda attempt: lambda _command: fail_verifier(attempt),
                cleanup_attempt=lambda attempt: (_ for _ in ()).throw(
                    OSError(f"cleanup failed {attempt}")
                ),
            )

        self.assertEqual((1,), tuple(item.attempt for item in raised.exception.attempts))
        self.assertIn(
            "cleanup attempt 1 also failed: cleanup failed 1",
            getattr(raised.exception, "__notes__", ()),
        )
        self.assertEqual(
            owned_identity,
            getattr(raised.exception, "owned_artifact_identity", None),
        )

    def test_verifier_final_retry_does_not_propagate_retired_identity(self):
        command = _verifier_command(
            pathlib.Path("staging") / "Organized_Spectra_20260628_120000.opju",
            pathlib.Path("staging") / "mutation.opju",
            pathlib.Path("staging"),
        )
        cleaned = []

        def verifier_factory(attempt):
            def verifier(_command):
                error = InfrastructureVerificationError(f"failed {attempt}")
                error.owned_artifact_identity = (31, attempt)
                raise error

            return verifier

        with self.assertRaises(VerifierInfrastructureFailure) as raised:
            run_verifier_with_infrastructure_retry(
                command,
                verifier_factory,
                cleanup_attempt=lambda attempt: cleaned.append(attempt),
            )

        self.assertEqual([1, 2], cleaned)
        self.assertFalse(
            hasattr(raised.exception, "owned_artifact_identity")
        )

    def test_deterministic_verifier_error_is_not_retried(self):
        command = _verifier_command(pathlib.Path("staging") / "Organized_Spectra_20260628_120000.opju", pathlib.Path("staging") / "mutation.opju", pathlib.Path("staging"))
        created = []

        def verifier_factory(attempt):
            created.append(attempt)
            def verifier(_command):
                raise DeterministicVerificationError("mismatch")
            return verifier

        with self.assertRaises(DeterministicVerificationError):
            run_verifier_with_infrastructure_retry(command, verifier_factory)

        self.assertEqual([1], created)
    def test_live_dependency_proof_targets_each_book_when_column_short_names_repeat(self):
        with WorkspaceTempDir() as root:
            staging_root = root / "staging"
            staging_root.mkdir()
            staged = staging_root / "Organized_Spectra_20260628_120000.opju"
            staged.write_text("staged", encoding="utf-8")
            mutation = staging_root / "verify-mutation.opju"
            expected = build_project_write_contract(_output_model_two_books())
            proof = FakeDependencyProof()
            command = _verifier_command(staged, mutation, staging_root, expected_contract=expected)

            prove_live_dependency_on_mutation_copy(command, proof)

            self.assertEqual(
                [
                    ("open", mutation, False),
                    ("assert_raw_to_norm_live", "F_Ex270_ExSlit2_EmSlit2_ALL_SAMPLES", "Sample-A", "B", "C"),
                    ("assert_raw_to_norm_live", "F_Ex270_ExSlit2_EmSlit2_ALL_SAMPLES", "Sample-B", "B", "C"),
                ],
                proof.events,
            )

    def test_live_dependency_proof_rejects_unparseable_norm_formula(self):
        with WorkspaceTempDir() as root:
            staging_root = root / "staging"
            staging_root.mkdir()
            staged = staging_root / "Organized_Spectra_20260628_120000.opju"
            staged.write_text("staged", encoding="utf-8")
            mutation = staging_root / "verify-mutation.opju"
            expected = build_project_write_contract(_output_model()).with_replaced_formula(0, 0, 3, "B/max(B)")
            command = _verifier_command(staged, mutation, staging_root, expected_contract=expected)

            with self.assertRaisesRegex(DeterministicVerificationError, "Unparseable Raw-to-Norm formula"):
                prove_live_dependency_on_mutation_copy(command, FakeDependencyProof())
    def test_retry_classification_contracts_split_infrastructure_from_deterministic_errors(self):
        self.assertEqual("retry_once_later", classify_output_error(InfrastructureOutputError("launch failed")))
        self.assertEqual("non_retryable", classify_output_error(DeterministicOutputError("naming failed")))
        self.assertEqual("retry_once_later", classify_verifier_error(InfrastructureVerificationError("communication lost")))
        self.assertEqual("non_retryable", classify_verifier_error(DeterministicVerificationError("mismatch")))
        self.assertEqual("non_retryable", classify_verifier_error(VerificationMismatchError("mismatch", report=None)))


    def test_verifier_refuses_replaced_staging_root_even_with_same_project_identity(self):
        with WorkspaceTempDir() as root:
            staging_root = root / "staging"
            staging_root.mkdir()
            staged = staging_root / "Organized_Spectra_20260628_120000.opju"
            staged.write_bytes(b"project")
            mutation = staging_root / "mutation.opju"
            command = _verifier_command(staged, mutation, staging_root)
            object.__setattr__(
                command,
                "run_staging_identity",
                path_identity(staging_root),
            )
            parked = root / "parked-staging"
            staging_root.rename(parked)
            staging_root.mkdir()
            os.link(parked / staged.name, staged)
            session = FakeVerifierSession(
                _origin_readback_contract(command.expected_contract),
                [],
            )

            with self.assertRaises(
                (VerifierWorkerPreflightError, DeterministicVerificationError)
            ):
                run_verifier_worker(
                    command,
                    process_preflight=lambda: None,
                    origin_loader=lambda: session,
                    dependency_proof=FakeDependencyProof(),
                )


class FailingDependencyProof:
    def open(self, path, readonly):
        pass

    def assert_raw_to_norm_live(self, folder_path, book_display_name, raw_column_short_name, norm_column_short_name):
        raise InfrastructureVerificationError("mutation copy communication failed")

class FakeDependencyProof:
    def __init__(self):
        self.events = []

    def open(self, path, readonly):
        self.events.append(("open", path, readonly))

    def assert_raw_to_norm_live(self, folder_path, book_display_name, raw_column_short_name, norm_column_short_name):
        from spectrum_organizer.origin.contracts import (
            AUTOMATIC_FORMULA_LOCK_STATE,
        )

        self.events.append(("assert_raw_to_norm_live", folder_path, book_display_name, raw_column_short_name, norm_column_short_name))
        return AUTOMATIC_FORMULA_LOCK_STATE


def _origin_readback_contract(contract):
    folders = []
    next_book_number = 1
    for folder in contract.folders:
        books = []
        for book in folder.books:
            books.append(book.__class__(book.display_long_name, f"Book{next_book_number}", book.columns))
            next_book_number += 1
        folders.append(folder.__class__(folder.path, tuple(books)))
    return contract.__class__(contract.root_path, tuple(folders))

def _output_model():
    return build_output_plan(
        (
            OutputSpectrum(
                "em-298",
                SpectrumClass.STEADY_EMISSION,
                "Sample-mTHF-298 K",
                "Sample-mTHF",
                "298 K",
                key_wavelength="270",
                excitation_slit="2",
                emission_slit="2",
                x_y=(("300", "10"), ("301", "20")),
            ),
            OutputSpectrum(
                "em-77",
                SpectrumClass.STEADY_EMISSION,
                "Sample-mTHF-77 K",
                "Sample-mTHF",
                "77 K",
                key_wavelength="270",
                excitation_slit="2",
                emission_slit="2",
                x_y=(("300.5", "5"), ("301", "15")),
            ),
        )
    )


def _output_model_with_excitation():
    return build_output_plan(
        (
            OutputSpectrum(
                "em-77",
                SpectrumClass.STEADY_EMISSION,
                "Sample-mTHF-77 K",
                "Sample-mTHF",
                "77 K",
                key_wavelength="270",
                excitation_slit="2",
                emission_slit="2",
                x_y=(("300", "10"), ("301", "20")),
            ),
            OutputSpectrum(
                "ex-77",
                SpectrumClass.STEADY_EXCITATION,
                "Sample-mTHF-77 K",
                "Sample-mTHF",
                "77 K",
                key_wavelength="315",
                excitation_slit="2",
                emission_slit="2",
                x_y=(("250", "4"), ("251", "8")),
            ),
        )
    )

def _output_model_two_books():
    return build_output_plan(
        (
            OutputSpectrum(
                "em-a",
                SpectrumClass.STEADY_EMISSION,
                "Sample-A-77 K",
                "Sample-A",
                "77 K",
                key_wavelength="270",
                excitation_slit="2",
                emission_slit="2",
                x_y=(("300", "10"), ("301", "20")),
            ),
            OutputSpectrum(
                "em-b",
                SpectrumClass.STEADY_EMISSION,
                "Sample-B-77 K",
                "Sample-B",
                "77 K",
                key_wavelength="270",
                excitation_slit="2",
                emission_slit="2",
                x_y=(("300", "5"), ("301", "15")),
            ),
        )
    )

def _output_command(
    target,
    staging_root,
    *,
    approved_snapshot_id="approved-1",
    allowed_output_targets=None,
):
    staging_root = pathlib.Path(staging_root)
    target_path = pathlib.Path(target)
    if (
        staging_root.exists()
        and target_path.parent.resolve() == staging_root.resolve()
        and not target_path.exists()
    ):
        target_path.touch()
    if staging_root.exists():
        status = staging_root.stat()
        staging_identity = (status.st_dev, status.st_ino)
    else:
        staging_identity = (0, 0)
    return OutputWorkerCommand(
        approved_snapshot_id=approved_snapshot_id,
        approved_output_model=_output_model(),
        staging_project_path=target_path,
        run_staging_root=staging_root,
        run_staging_identity=staging_identity,
        allowed_output_targets=tuple(pathlib.Path(item) for item in (allowed_output_targets or (target,))),
        worker_role="output",
        staging_project_identity=(
            path_identity(target_path)
            if target_path.exists()
            else None
        ),
    )


def _verifier_command(
    staged,
    mutation,
    staging_root,
    *,
    approved_snapshot_id="approved-1",
    expected_contract=None,
    allowed_open_targets=None,
    protected_paths=(),
):
    staged_path = pathlib.Path(staged)
    mutation_path = pathlib.Path(mutation)
    staging_root_path = pathlib.Path(staging_root)
    staging_identity = (
        path_identity(staging_root_path)
        if staging_root_path.exists()
        else (0, 0)
    )
    if staged_path.exists():
        status = staged_path.stat()
        expected_project_artifact = ProjectArtifactEvidence(
            identity=(status.st_dev, status.st_ino),
            sha256=hashlib.sha256(staged_path.read_bytes()).hexdigest(),
            size=status.st_size,
        )
    else:
        expected_project_artifact = ProjectArtifactEvidence(
            identity=(0, 0),
            sha256="0" * 64,
            size=0,
        )
    if (
        staging_root_path.exists()
        and mutation_path.parent.resolve() == staging_root_path.resolve()
        and not mutation_path.exists()
    ):
        mutation_path.touch()
    return VerifierWorkerCommand(
        approved_snapshot_id=approved_snapshot_id,
        staged_project_path=pathlib.Path(staged),
        mutation_copy_path=mutation_path,
        run_staging_root=staging_root_path,
        run_staging_identity=staging_identity,
        allowed_open_targets=tuple(pathlib.Path(path) for path in (allowed_open_targets or (staged, mutation))),
        protected_paths=tuple(pathlib.Path(path) for path in protected_paths),
        expected_contract=expected_contract or build_project_write_contract(_output_model()),
        expected_project_artifact=expected_project_artifact,
        worker_role="verifier",
        mutation_copy_identity=(
            path_identity(mutation_path)
            if mutation_path.exists()
            else None
        ),
    )


if __name__ == "__main__":
    unittest.main()
