import io
import json
import os
import pathlib
import shutil
import sys
import tempfile
import threading
import types
import unittest
import uuid
import weakref
from dataclasses import replace
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
VALIDATION = ROOT / "validation"
if str(VALIDATION) not in sys.path:
    sys.path.insert(0, str(VALIDATION))

import manual_full_run_acceptance as acceptance_module
from manual_full_run_acceptance import ManualAcceptanceError, run_extraction_only
from spectrum_organizer.product_runner import (
    ApprovedPreExtractionRunContext,
    ExtractionCleanupBlockedError,
    ExtractionPhaseSummary,
    SourceExtractionSummary,
)
from spectrum_organizer.safety.fingerprints import (
    SnapshotMismatchError,
    hash_file,
    snapshot_sources,
)
from spectrum_organizer.safety.identity_paths import path_identity
from spectrum_organizer.safety.owned_paths import create_run_ownership_at_root
from spectrum_organizer.ui.dialog_port import DialogResponse


CONFIRMED_SETTINGS = {
    "s1Limit": 2_000_000,
    "steadyEmissionY": "S1c",
    "allowMissingS1": False,
}


class WorkspaceTempDir:
    def __init__(self):
        self.root = ROOT / ".test-tmp" / "manual-acceptance"
        self.path = self.root / f"case-{uuid.uuid4().hex}"

    def __enter__(self):
        self.path.mkdir(parents=True)
        return self.path

    def __exit__(self, exc_type, exc, tb):
        shutil.rmtree(self.path, ignore_errors=True)
        if self.root.exists() and not any(self.root.iterdir()):
            self.root.rmdir()


class ExternalTempDir:
    def __enter__(self):
        self._directory = tempfile.TemporaryDirectory(prefix="spectrum-organizer-manual-")
        return pathlib.Path(self._directory.name)

    def __exit__(self, exc_type, exc, tb):
        self._directory.cleanup()


class FakeDialogPort:
    def choose(self, request):
        if request.kind == "save_and_close_origin":
            return DialogResponse("retry")
        raise AssertionError(f"Unexpected dialog {request.kind}")


class FakeExtractionRunner:
    def __init__(self, summary):
        self.summary = summary
        self.contexts = []
        self.cancelled = False

    def __call__(self, context):
        self.contexts.append(context)
        return self.summary

    def cancel(self):
        self.cancelled = True


class InterruptingExtractionRunner(FakeExtractionRunner):
    def __init__(self, summary, *, cancel_side_effect=None, cancel_error=None):
        super().__init__(summary)
        self.cancel_side_effect = cancel_side_effect
        self.cancel_error = cancel_error
        self.calling_thread = None

    def __call__(self, context):
        self.contexts.append(context)
        self.calling_thread = threading.current_thread()
        raise KeyboardInterrupt

    def cancel(self):
        super().cancel()
        if self.cancel_side_effect is not None:
            self.cancel_side_effect()
        if self.cancel_error is not None:
            raise self.cancel_error


class SuccessfulBlockingRunner(FakeExtractionRunner):
    def __init__(self, summary):
        super().__init__(summary)
        self.entered = threading.Event()
        self.release = threading.Event()

    def __call__(self, context):
        self.contexts.append(context)
        self.entered.set()
        self.release.wait(2.0)
        return self.summary


class ManualAcceptanceContractTests(unittest.TestCase):
    def test_guided_origin_settling_ignores_visible_manual_inspection_window(self):
        from spectrum_organizer.safety.process_boundary import ProcessInfo

        visible = ProcessInfo(
            pid=101,
            start_time_ns=1001,
            visible=True,
            taskbar_visible=True,
            program_owned=False,
        )

        residual_count, visible_count = (
            acceptance_module._settled_origin_process_counts(
                process_probe=lambda: (visible,),
                timeout=0,
                poll_interval=0,
            )
        )

        self.assertEqual(0, residual_count)
        self.assertEqual(1, visible_count)

    def test_guided_origin_settling_counts_visible_program_owned_origin_as_residual(self):
        from spectrum_organizer.safety.process_boundary import ProcessInfo

        owned = ProcessInfo(
            pid=104,
            start_time_ns=1004,
            visible=True,
            taskbar_visible=True,
            program_owned=True,
        )

        residual_count, visible_count = (
            acceptance_module._settled_origin_process_counts(
                process_probe=lambda: (owned,),
                timeout=0,
                poll_interval=0,
            )
        )

        self.assertEqual(1, residual_count)
        self.assertEqual(0, visible_count)

    def test_guided_origin_settling_uses_audited_identity_for_visible_owned_origin(self):
        from spectrum_organizer.safety.process_boundary import (
            ProcessIdentity,
            ProcessInfo,
        )

        owned = ProcessInfo(
            pid=105,
            start_time_ns=1005,
            visible=True,
            taskbar_visible=True,
            program_owned=False,
        )

        residual_count, visible_count = (
            acceptance_module._settled_origin_process_counts(
                process_probe=lambda: (owned,),
                owned_identities=frozenset(
                    {ProcessIdentity(pid=105, start_time_ns=1005)}
                ),
                timeout=0,
                poll_interval=0,
            )
        )

        self.assertEqual(1, residual_count)
        self.assertEqual(0, visible_count)

    def test_guided_acceptance_passes_audited_origin_identities_to_final_settling(self):
        from spectrum_organizer.safety.process_boundary import ProcessIdentity

        with ExternalTempDir() as root:
            package_dir = root / "package"
            package_dir.mkdir()
            (package_dir / "Spectrum Organizer.exe").touch()

            class FakeProcess:
                def __init__(self, audit_dir):
                    self.audit_dir = audit_dir

                def wait(self):
                    for index, role in enumerate(
                        ("extraction", "output", "verifier"),
                        start=1,
                    ):
                        if role == "extraction":
                            binding = {
                                "run_id": "run-1",
                                "source_id": "S0001",
                                "reader_attempt": 1,
                            }
                            attempt_type = "origin_extraction_target_attempt"
                            attempt_payload = {
                                **binding,
                            }
                        else:
                            binding = {
                                "approved_snapshot_id": "snapshot-1",
                                "run_staging_root": str(root / "run-staging"),
                                "attempt": 1,
                            }
                            attempt_type = "origin_worker_target_attempt"
                            attempt_payload = {"role": role, **binding}
                        (self.audit_dir / f"origin-attempt-{role}.json").write_text(
                            json.dumps(
                                {
                                    "schema_version": 1,
                                    "event_type": attempt_type,
                                    "recorded_time_ns": index,
                                    "process_id": 100,
                                    "process_instance_id": "1" * 32,
                                    "payload": attempt_payload,
                                }
                            ),
                            encoding="utf-8",
                        )
                        (self.audit_dir / f"origin-identity-{role}.json").write_text(
                            json.dumps(
                                {
                                    "schema_version": 1,
                                    "event_type": "origin_process_identity",
                                    "recorded_time_ns": index,
                                    "process_id": 100 + index,
                                    "process_instance_id": f"{index}" * 32,
                                    "payload": {
                                        "role": role,
                                        "pid": 106 + index,
                                        "start_time_ns": 1006 + index,
                                        "attempt_binding": binding,
                                    },
                                }
                            ),
                            encoding="utf-8",
                        )
                    return 0

            def launch(_command, **kwargs):
                audit_dir = pathlib.Path(
                    kwargs["env"][acceptance_module.RUNTIME_AUDIT_DIR_ENV]
                )
                return FakeProcess(audit_dir)

            with (
                mock.patch.object(
                    acceptance_module,
                    "_settled_origin_process_counts",
                    return_value=(0, 0),
                ) as settled,
                self.assertRaises(ManualAcceptanceError),
            ):
                acceptance_module.run_guided_full_acceptance(
                    package_dir=package_dir,
                    evidence_root=root / "evidence",
                    process_launcher=launch,
                    final_product_process_count=lambda _executable: 0,
                    timestamp_factory=lambda: "20260804_140000",
                )

            settled.assert_called_once_with(
                owned_identities=frozenset(
                    {
                        ProcessIdentity(pid=107, start_time_ns=1007),
                        ProcessIdentity(pid=108, start_time_ns=1008),
                        ProcessIdentity(pid=109, start_time_ns=1009),
                    }
                )
            )

    def test_guided_identity_audit_rejects_missing_required_roles(self):
        with self.assertRaisesRegex(
            ManualAcceptanceError,
            "identity.*coverage",
        ):
            acceptance_module._audited_origin_process_identities(
                (),
                required_roles=frozenset(
                    {"extraction", "output", "verifier"}
                ),
            )

    def test_guided_identity_audit_rejects_worker_attempt_without_identity(self):
        events = (
            {
                "schema_version": 1,
                "event_type": "origin_extraction_target_attempt",
                "recorded_time_ns": 1,
                "process_id": 99,
                "payload": {
                    "run_id": "run-1",
                    "source_id": "S0001",
                    "reader_attempt": 1,
                },
            },
            {
                "schema_version": 1,
                "event_type": "origin_process_identity",
                "recorded_time_ns": 1,
                "process_id": 100,
                "process_instance_id": "a" * 32,
                "payload": {
                    "role": "extraction",
                    "pid": 110,
                    "start_time_ns": 1010,
                    "attempt_binding": {
                        "run_id": "run-1",
                        "source_id": "S0001",
                        "reader_attempt": 1,
                    },
                },
            },
            {
                "schema_version": 1,
                "event_type": "origin_worker_target_attempt",
                "recorded_time_ns": 2,
                "process_id": 200,
                "payload": {
                    "role": "output",
                    "approved_snapshot_id": "snapshot-1",
                    "run_staging_root": r"C:\output\.staging",
                    "attempt": 1,
                },
            },
        )

        with self.assertRaisesRegex(
            ManualAcceptanceError,
            "identity.*coverage",
        ):
            acceptance_module._audited_origin_process_identities(
                events,
                required_roles=frozenset({"extraction", "output"}),
            )

    def test_guided_identity_audit_binds_completed_worker_process(self):
        events = (
            {
                "schema_version": 1,
                "event_type": "origin_extraction_target_attempt",
                "recorded_time_ns": 1,
                "process_id": 99,
                "payload": {
                    "run_id": "run-1",
                    "source_id": "S0001",
                    "reader_attempt": 1,
                },
            },
            {
                "schema_version": 1,
                "event_type": "origin_process_identity",
                "recorded_time_ns": 1,
                "process_id": 100,
                "process_instance_id": "a" * 32,
                "payload": {
                    "role": "extraction",
                    "pid": 110,
                    "start_time_ns": 1010,
                    "attempt_binding": {
                        "run_id": "run-1",
                        "source_id": "S0001",
                        "reader_attempt": 1,
                    },
                },
            },
            {
                "schema_version": 1,
                "event_type": "origin_worker_targets",
                "recorded_time_ns": 2,
                "process_id": 101,
                "process_instance_id": "b" * 32,
                "payload": {
                    "role": "extraction",
                    "run_id": "run-1",
                    "source_id": "S0001",
                    "reader_attempt": 1,
                },
            },
        )

        with self.assertRaisesRegex(
            ManualAcceptanceError,
            "identity.*coverage",
        ):
            acceptance_module._audited_origin_process_identities(
                events,
                required_roles=frozenset({"extraction"}),
            )

    def test_guided_identity_audit_rejects_reused_pid_from_different_process_instance(self):
        root = r"C:\output\.staging"
        events = (
            {
                "schema_version": 1,
                "event_type": "origin_worker_target_attempt",
                "recorded_time_ns": 1,
                "process_id": 200,
                "process_instance_id": "1" * 32,
                "payload": {
                    "role": "output",
                    "attempt": 1,
                    "approved_snapshot_id": "snapshot-1",
                    "run_staging_root": root,
                },
            },
            {
                "schema_version": 1,
                "event_type": "origin_process_identity",
                "recorded_time_ns": 2,
                "process_id": 300,
                "process_instance_id": "a" * 32,
                "payload": {
                    "role": "output",
                    "pid": 400,
                    "start_time_ns": 500,
                    "attempt_binding": {
                        "approved_snapshot_id": "snapshot-1",
                        "run_staging_root": root,
                        "attempt": 1,
                    },
                },
            },
            {
                "schema_version": 1,
                "event_type": "origin_worker_targets",
                "recorded_time_ns": 3,
                "process_id": 300,
                "process_instance_id": "b" * 32,
                "payload": {
                    "role": "output",
                    "approved_snapshot_id": "snapshot-1",
                    "run_staging_root": root,
                    "attempt": 1,
                },
            },
        )

        with self.assertRaisesRegex(
            ManualAcceptanceError,
            "identity.*coverage",
        ):
            acceptance_module._audited_origin_process_identities(
                events,
                required_roles=frozenset({"output"}),
            )

    def test_guided_identity_audit_rejects_same_role_attempt_substitution(self):
        root = r"C:\output\.staging"
        events = (
            *(
                {
                    "schema_version": 1,
                    "event_type": "origin_worker_target_attempt",
                    "recorded_time_ns": attempt,
                    "process_id": 200,
                    "payload": {
                        "role": "output",
                        "attempt": attempt,
                        "approved_snapshot_id": "snapshot-1",
                        "run_staging_root": root,
                    },
                }
                for attempt in (1, 2)
            ),
            *(
                {
                    "schema_version": 1,
                    "event_type": "origin_process_identity",
                    "recorded_time_ns": 10 + attempt,
                    "process_id": 300 + attempt,
                    "process_instance_id": f"{attempt}" * 32,
                    "payload": {
                        "role": "output",
                        "pid": 400 + attempt,
                        "start_time_ns": 500 + attempt,
                        "attempt_binding": {
                            "approved_snapshot_id": "snapshot-1",
                            "run_staging_root": root,
                            "attempt": attempt,
                        },
                    },
                }
                for attempt in (1, 3)
            ),
        )

        with self.assertRaisesRegex(
            ManualAcceptanceError,
            "identity.*coverage",
        ):
            acceptance_module._audited_origin_process_identities(
                events,
                required_roles=frozenset({"output"}),
            )

    def test_guided_identity_audit_rejects_reused_origin_identity(self):
        root = r"C:\output\.staging"
        events = tuple(
            item
            for attempt in (1, 2)
            for item in (
                {
                    "schema_version": 1,
                    "event_type": "origin_worker_target_attempt",
                    "recorded_time_ns": attempt,
                    "process_id": 200,
                    "payload": {
                        "role": "output",
                        "attempt": attempt,
                        "approved_snapshot_id": "snapshot-1",
                        "run_staging_root": root,
                    },
                },
                {
                    "schema_version": 1,
                    "event_type": "origin_process_identity",
                    "recorded_time_ns": 10 + attempt,
                    "process_id": 300 + attempt,
                    "process_instance_id": f"{attempt}" * 32,
                    "payload": {
                        "role": "output",
                        "pid": 400,
                        "start_time_ns": 500,
                        "attempt_binding": {
                            "approved_snapshot_id": "snapshot-1",
                            "run_staging_root": root,
                            "attempt": attempt,
                        },
                    },
                },
            )
        )

        with self.assertRaisesRegex(
            ManualAcceptanceError,
            "identity.*coverage",
        ):
            acceptance_module._audited_origin_process_identities(
                events,
                required_roles=frozenset({"output"}),
            )

    def test_guided_origin_settling_waits_for_hidden_worker_cleanup(self):
        from spectrum_organizer.safety.process_boundary import ProcessInfo

        hidden = ProcessInfo(
            pid=102,
            start_time_ns=1002,
            visible=False,
            taskbar_visible=False,
            program_owned=False,
        )
        observations = iter(((hidden,), ()))
        sleep = mock.Mock()

        residual_count, visible_count = (
            acceptance_module._settled_origin_process_counts(
                process_probe=lambda: next(observations),
                timeout=1,
                poll_interval=0.01,
                monotonic=iter((0.0, 0.1)).__next__,
                sleep=sleep,
            )
        )

        self.assertEqual((0, 0), (residual_count, visible_count))
        sleep.assert_called_once_with(0.01)

    def test_guided_origin_settling_retains_persistent_hidden_worker_failure(self):
        from spectrum_organizer.safety.process_boundary import ProcessInfo

        hidden = ProcessInfo(
            pid=103,
            start_time_ns=1003,
            visible=False,
            taskbar_visible=False,
            program_owned=False,
        )

        residual_count, visible_count = (
            acceptance_module._settled_origin_process_counts(
                process_probe=lambda: (hidden,),
                timeout=0,
                poll_interval=0,
            )
        )

        self.assertEqual(1, residual_count)
        self.assertEqual(0, visible_count)

    def test_context_discard_refuses_newer_owned_root_at_same_path(self):
        with ExternalTempDir() as root:
            temp_root = root / "active-run"
            old = create_run_ownership_at_root(
                temp_root,
                "run-old",
                "marker-old",
                [],
            )
            old_anchor = temp_root.parent / (
                f".{temp_root.name}.ownership-anchor.json"
            )
            old.temp_root.rename(root / "parked-old-root")
            old_anchor.rename(root / "parked-old-anchor.json")
            newer = create_run_ownership_at_root(
                temp_root,
                "run-new",
                "marker-new",
                [],
            )
            stale_context = types.SimpleNamespace(
                temp_root=temp_root,
                temp_root_identity=old.temp_root_identity,
            )

            with self.assertRaisesRegex(
                ManualAcceptanceError,
                "Could not discard the changed-source temporary context",
            ):
                acceptance_module._discard_context_temp_root(stale_context)

            self.assertTrue(newer.temp_root.is_dir())
            self.assertEqual(newer.temp_root_identity, path_identity(newer.temp_root))

    def test_cli_preserves_extraction_only_and_accepts_guided_package_mode(self):
        extraction = acceptance_module._parse_args(
            ["--phase", "extraction-only"]
        )
        guided = acceptance_module._parse_args(
            ["--package-dir", "dist/Spectrum Organizer"]
        )
        cancellation = acceptance_module._parse_args(
            [
                "--package-dir",
                "dist/Spectrum Organizer",
                "--cycle",
                "cancellation",
            ]
        )

        self.assertEqual("extraction-only", extraction.phase)
        self.assertIsNone(extraction.package_dir)
        self.assertIsNone(guided.phase)
        self.assertEqual(
            pathlib.Path("dist/Spectrum Organizer"),
            guided.package_dir,
        )
        self.assertEqual("success", guided.cycle)
        self.assertEqual("cancellation", cancellation.cycle)
        with self.assertRaises(SystemExit):
            acceptance_module._parse_args(
                [
                    "--phase",
                    "extraction-only",
                    "--package-dir",
                    "dist/Spectrum Organizer",
                ]
            )

    def test_guided_main_uses_package_mode_without_preselecting_sources(self):
        package_dir = pathlib.Path("dist/Spectrum Organizer")
        evidence_dir = pathlib.Path("evidence/full-run-manual-1")
        with mock.patch.object(
            acceptance_module,
            "run_guided_full_acceptance",
            return_value=evidence_dir,
            create=True,
        ) as guided, mock.patch.object(
            acceptance_module,
            "_confirm_settings_with_qt",
        ) as confirm, mock.patch.object(
            acceptance_module,
            "run_extraction_only",
        ) as extraction:
            self.assertEqual(
                0,
                acceptance_module.main(
                    ["--package-dir", str(package_dir)]
                ),
            )

        guided.assert_called_once_with(
            package_dir=package_dir,
            evidence_root=acceptance_module.DEFAULT_EVIDENCE_ROOT,
            cycle="success",
        )
        confirm.assert_not_called()
        extraction.assert_not_called()

    def test_guided_package_launch_collects_and_reconciles_actual_runtime_evidence(self):
        with ExternalTempDir() as root:
            package_dir = root / "package"
            package_dir.mkdir()
            executable = package_dir / "Spectrum Organizer.exe"
            executable.write_bytes(b"package")
            source = root / "fresh-raw.opju"
            source.write_bytes(b"immutable raw source")
            output_parent = root / "output"
            output_parent.mkdir()
            calls = []

            class FakeProcess:
                returncode = 0

                def wait(self):
                    audit_dir = pathlib.Path(
                        calls[0][1]["env"][
                            "SPECTRUM_ORGANIZER_RUNTIME_AUDIT_DIR"
                        ]
                    )
                    before = snapshot_sources([source], [])[0]
                    staging = output_parent / ".spectrum-organizer-staging-run-1"
                    staging_project = staging / "Organized_Spectra_20260802_230000.opju"
                    mutation = staging / "Verifier_Mutation_20260802_230000.opju"
                    temp_root = root / "runtime-temp"
                    copy_path = temp_root / "copy.opju"
                    final_dir = output_parent / "Organized_Origin_Data_20260802_230000"
                    final_dir.mkdir()
                    project = final_dir / "Organized_Spectra_20260802_230000.opju"
                    report = final_dir / "Run_Report_20260802_230000.txt"
                    project.write_bytes(b"verified project")
                    fingerprint_detail = (
                        f"提交前 SHA-256={before.sha256}；"
                        f"输出后 SHA-256={before.sha256}；"
                        f"大小={before.size_bytes}；"
                        f"UTC mtime_ns={before.mtime_ns}；未改变"
                    )
                    approved_sections = {
                        title: ["无"]
                        for title in acceptance_module.APPROVED_OUTPUT_LEDGER_SECTION_TITLES
                    }
                    report_lines = [
                                "输入路径",
                                f"- {source}",
                                "",
                                "输出路径",
                                f"- {final_dir}",
                                "",
                    ]
                    for title, entries in approved_sections.items():
                        report_lines.extend(
                            [title, *(f"- {entry}" for entry in entries), ""]
                        )
                    report_lines.extend(
                        [
                                "源文件指纹",
                                f"- {source}：{fingerprint_detail}",
                                "",
                                "数量核对",
                                "- 识别 Book：1",
                                "- 拒绝 Book：0",
                                "- 排除 Book：0",
                                "- 接受普通谱：1",
                                "- 输出计划谱图：1",
                                "- 输出计划列：3",
                                "- 验证回读谱图：1",
                                "- 验证回读列：3",
                                "",
                        ]
                    )
                    report.write_text(
                        "\n".join(report_lines),
                        encoding="utf-8",
                    )

                    def write_event(name, event_type, payload):
                        (audit_dir / f"{name}.json").write_text(
                            json.dumps(
                                {
                                    "schema_version": 1,
                                    "event_type": event_type,
                                    "recorded_time_ns": 1,
                                    "process_id": 100,
                                    "process_instance_id": "1" * 32,
                                    "payload": payload,
                                }
                            ),
                            encoding="utf-8",
                        )

                    snapshot_payload = {
                        "path": str(before.path),
                        "canonical_path": str(before.canonical_path),
                        "sha256": before.sha256,
                        "size_bytes": before.size_bytes,
                        "mtime_ns": before.mtime_ns,
                        "device_id": before.device_id,
                        "file_id": before.file_id,
                    }
                    def target_identity(path, file_id, *, device_id=None):
                        return {
                            "path": str(path.resolve()),
                            "device_id": (
                                before.device_id
                                if device_id is None
                                else device_id
                            ),
                            "file_id": file_id,
                        }

                    publication_artifacts = [
                        {
                            "path": str(item.path),
                            "canonical_path": str(item.canonical_path),
                            "sha256": item.sha256,
                            "size_bytes": item.size_bytes,
                            "mtime_ns": item.mtime_ns,
                            "device_id": item.device_id,
                            "file_id": item.file_id,
                        }
                        for item in snapshot_sources([project, report], [])
                    ]
                    published_project = publication_artifacts[0]
                    write_event(
                        "01-context",
                        "pre_extraction_context",
                        {
                            "run_id": "run-1",
                            "timestamp": "20260802_230000",
                            "selected_source_paths": [str(source)],
                            "output_parent": str(output_parent),
                            "settings_snapshot": {"s1Limit": 2_000_000},
                            "source_fingerprints_before": [snapshot_payload],
                            "temp_root": str(temp_root),
                            "temp_root_identity": [1, 2],
                            "run_owned_source_copy_paths": [str(copy_path)],
                            "protected_fingerprints_before": [],
                        },
                    )
                    write_event(
                        "02-extraction-attempt",
                        "origin_extraction_target_attempt",
                        {
                            "run_id": "run-1",
                            "source_id": "S0001",
                            "reader_attempt": 1,
                            "copy_path": str(copy_path),
                            "copy_identity": target_identity(
                                copy_path,
                                before.file_id + 10,
                            ),
                        },
                    )
                    write_event(
                        "03-extraction",
                        "origin_worker_targets",
                        {
                            "role": "extraction",
                            "run_id": "run-1",
                            "source_id": "S0001",
                            "reader_attempt": 1,
                            "open_targets": [str(copy_path)],
                            "open_target_identities": [
                                target_identity(copy_path, before.file_id + 10)
                            ],
                        },
                    )
                    write_event(
                        "03-attempt",
                        "output_stage_attempt",
                        {
                            "approved_snapshot_id": "approved-1",
                            "run_id": "attempt-1",
                            "output_parent": str(output_parent),
                            "output_parent_existed_before": True,
                            "output_parent_entries_before": [],
                            "task_temp_root": str(temp_root),
                        },
                    )
                    for index, stage in enumerate(
                        ("create_staging", "write_output", "verify_output", "publish"),
                        start=4,
                    ):
                        write_event(
                            f"{index:02d}-progress",
                            "output_stage_progress",
                            {
                                "approved_snapshot_id": "approved-1",
                                "run_id": "attempt-1",
                                "stage": stage,
                            },
                        )
                    write_event(
                        "08-staging",
                        "output_staging_created",
                        {
                            "approved_snapshot_id": "approved-1",
                            "run_id": "attempt-1",
                            "output_parent": str(output_parent),
                            "staging_dir": str(staging),
                            "staging_project_path": str(staging_project),
                            "verifier_mutation_path": str(mutation),
                        },
                    )
                    write_event(
                        "09-counts",
                        "approved_count_reconciliation",
                        {
                            "recognizable_book_count": 1,
                            "rejected_book_count": 0,
                            "excluded_book_count": 0,
                            "accepted_ordinary_spectrum_count": 1,
                            "output_plan_spectrum_count": 1,
                            "output_plan_column_count": 3,
                        },
                    )
                    write_event(
                        "10-ledger",
                        "approved_report_ledger",
                        {
                            "approved_snapshot_id": "approved-1",
                            "recognized_source_paths": [str(source)],
                            "sections": approved_sections,
                        },
                    )
                    write_event(
                        "10-output-attempt",
                        "origin_worker_target_attempt",
                        {
                            "role": "output",
                            "attempt": 1,
                            "approved_snapshot_id": "approved-1",
                            "run_staging_root": str(staging),
                            "target_states": [
                                {
                                    "path": str(staging_project),
                                    "existed_before": True,
                                    "identity": target_identity(
                                        staging_project,
                                        published_project["file_id"],
                                        device_id=published_project[
                                            "device_id"
                                        ],
                                    ),
                                }
                            ],
                        },
                    )
                    write_event(
                        "11-output",
                        "origin_worker_targets",
                        {
                            "role": "output",
                            "approved_snapshot_id": "approved-1",
                            "run_staging_root": str(staging),
                            "open_targets": [str(staging_project)],
                            "open_target_identities": [
                                target_identity(
                                    staging_project,
                                    published_project["file_id"],
                                    device_id=published_project["device_id"],
                                )
                            ],
                            "spectrum_count": 1,
                            "column_count": 3,
                        },
                    )
                    write_event(
                        "11-verifier-attempt",
                        "origin_worker_target_attempt",
                        {
                            "role": "verifier",
                            "attempt": 1,
                            "approved_snapshot_id": "approved-1",
                            "run_staging_root": str(staging),
                            "target_states": [
                                {
                                    "path": str(staging_project),
                                    "existed_before": True,
                                    "identity": target_identity(
                                        staging_project,
                                        published_project["file_id"],
                                        device_id=published_project[
                                            "device_id"
                                        ],
                                    ),
                                },
                                {
                                    "path": str(mutation),
                                    "existed_before": True,
                                    "identity": target_identity(
                                        mutation,
                                        published_project["file_id"] + 1,
                                        device_id=published_project[
                                            "device_id"
                                        ],
                                    ),
                                },
                            ],
                        },
                    )
                    write_event(
                        "12-verifier",
                        "origin_worker_targets",
                        {
                            "role": "verifier",
                            "approved_snapshot_id": "approved-1",
                            "run_staging_root": str(staging),
                            "open_targets": [str(staging_project), str(mutation)],
                            "open_target_identities": [
                                target_identity(
                                    staging_project,
                                    published_project["file_id"],
                                    device_id=published_project["device_id"],
                                ),
                                target_identity(
                                    mutation,
                                    published_project["file_id"] + 1,
                                    device_id=published_project["device_id"],
                                ),
                            ],
                            "spectrum_count": 1,
                            "column_count": 3,
                        },
                    )
                    write_event(
                        "13-publication",
                        "publication_committed",
                        {
                            "approved_snapshot_id": "approved-1",
                            "run_id": "attempt-1",
                            "output_parent": str(output_parent),
                            "final_run_dir": str(final_dir),
                            "final_project_path": str(project),
                            "final_report_path": str(report),
                            "artifacts": publication_artifacts,
                        },
                    )
                    return self.returncode

            def launch(command, **kwargs):
                calls.append((command, kwargs))
                return FakeProcess()

            evidence_dir = acceptance_module.run_guided_full_acceptance(
                package_dir=package_dir,
                evidence_root=root / "evidence",
                process_launcher=launch,
                final_origin_process_count=lambda: 0,
                final_product_process_count=lambda _executable: 0,
                freshness_selector=lambda paths: paths[0],
                timestamp_factory=lambda: "20260802_230000",
            )

            self.assertEqual([str(executable)], calls[0][0])
            self.assertEqual(str(package_dir), calls[0][1]["cwd"])
            self.assertNotIn("--source", calls[0][0])
            self.assertNotIn("--output-parent", calls[0][0])
            self.assertIn("LOCALAPPDATA", calls[0][1]["env"])
            summary = json.loads(
                (evidence_dir / "manual-full-run-summary.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                "automated_evidence_ready_manual_checks_pending",
                summary["status"],
            )
            self.assertEqual([str(source)], summary["selected_source_paths"])
            self.assertEqual(0, summary["final_origin_process_count"])
            self.assertEqual(0, summary["final_product_process_count"])
            self.assertEqual(
                str(source),
                summary["operator_freshness_attestation"]["source_path"],
            )
            self.assertNotIn("reference_audit", summary)
            for filename in summary["required_evidence"]:
                self.assertTrue((evidence_dir / filename).is_file(), filename)
            count_summary = json.loads(
                (evidence_dir / "count-reconciliation-summary.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertTrue(count_summary["counts_closed"])
            self.assertTrue(count_summary["report_cross_check_passed"])
            checklist = (
                evidence_dir / "manual-acceptance-checklist.md"
            ).read_text(encoding="utf-8")
            for required in (
                "由用户在生产 UI 中选择原始文件",
                "SHA-256、字节大小和 UTC mtime_ns",
                "worker-open-targets.json",
                "count-reconciliation-summary.json",
                "approved-snapshot ledger",
            ):
                self.assertIn(required, checklist)

    def test_guided_package_launch_fails_closed_without_runtime_audit(self):
        with ExternalTempDir() as root:
            package_dir = root / "package"
            package_dir.mkdir()
            (package_dir / "Spectrum Organizer.exe").write_bytes(b"package")
            process = mock.Mock()
            process.wait.return_value = 0

            with self.assertRaisesRegex(
                ManualAcceptanceError,
                "pre-extraction runtime audit",
            ):
                acceptance_module.run_guided_full_acceptance(
                    package_dir=package_dir,
                    evidence_root=root / "evidence",
                    process_launcher=lambda *_args, **_kwargs: process,
                    final_origin_process_count=lambda: 0,
                    final_product_process_count=lambda _executable: 0,
                    freshness_selector=lambda paths: paths[0],
                    timestamp_factory=lambda: "20260802_230001",
                )

    def test_runtime_audit_reader_rejects_missing_process_instance(self):
        with ExternalTempDir() as root:
            audit_dir = root / "runtime-audit"
            audit_dir.mkdir()
            (audit_dir / "event.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "event_type": "probe",
                        "recorded_time_ns": 1,
                        "process_id": 100,
                        "payload": {},
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ManualAcceptanceError,
                "process instance",
            ):
                acceptance_module._read_runtime_audit_events(audit_dir)

    def test_guided_cancellation_cycle_requires_no_publication_or_staging(self):
        with ExternalTempDir() as root:
            package_dir = root / "package"
            package_dir.mkdir()
            executable = package_dir / "Spectrum Organizer.exe"
            executable.write_bytes(b"package")
            source = root / "selected.opju"
            source.write_bytes(b"immutable")
            output_parent = root / "empty-output"
            output_parent.mkdir()
            calls = []

            class FakeProcess:
                def wait(self):
                    audit_dir = pathlib.Path(
                        calls[0][1]["env"][
                            "SPECTRUM_ORGANIZER_RUNTIME_AUDIT_DIR"
                        ]
                    )
                    before = snapshot_sources([source], [])[0]
                    context = {
                        "run_id": "cancel-run",
                        "timestamp": "20260802_230002",
                        "selected_source_paths": [str(source)],
                        "output_parent": str(output_parent),
                        "settings_snapshot": {"s1Limit": 2_000_000},
                        "source_fingerprints_before": [
                            {
                                "path": str(before.path),
                                "canonical_path": str(before.canonical_path),
                                "sha256": before.sha256,
                                "size_bytes": before.size_bytes,
                                "mtime_ns": before.mtime_ns,
                                "device_id": before.device_id,
                                "file_id": before.file_id,
                            }
                        ],
                        "temp_root": str(root / "runtime-temp"),
                        "temp_root_identity": [1, 2],
                        "run_owned_source_copy_paths": [
                            str(root / "runtime-temp" / "copy.opju")
                        ],
                        "protected_fingerprints_before": [],
                    }
                    for name, event_type, payload in (
                        ("01", "pre_extraction_context", context),
                        (
                            "02-attempt",
                            "origin_extraction_target_attempt",
                            {
                                "run_id": "cancel-run",
                                "source_id": "S0001",
                                "reader_attempt": 1,
                                "copy_path": str(
                                    root / "runtime-temp" / "copy.opju"
                                ),
                                "copy_identity": {
                                    "path": str(
                                        (root / "runtime-temp" / "copy.opju").resolve()
                                    ),
                                    "device_id": before.device_id,
                                    "file_id": before.file_id + 10,
                                },
                            },
                        ),
                        (
                            "03",
                            "origin_worker_targets",
                            {
                                "role": "extraction",
                                "run_id": "cancel-run",
                                "source_id": "S0001",
                                "reader_attempt": 1,
                                "open_targets": [
                                    str(root / "runtime-temp" / "copy.opju")
                                ],
                                "open_target_identities": [
                                    {
                                        "path": str(
                                            (root / "runtime-temp" / "copy.opju").resolve()
                                        ),
                                        "device_id": before.device_id,
                                        "file_id": before.file_id + 10,
                                    }
                                ],
                            },
                        ),
                        (
                            "04-output-attempt",
                            "output_stage_attempt",
                            {
                                "approved_snapshot_id": "approved-cancel",
                                "run_id": "cancel-attempt-1",
                                "output_parent": str(output_parent),
                                "output_parent_existed_before": True,
                                "output_parent_entries_before": [],
                                "task_temp_root": str(root / "runtime-temp"),
                            },
                        ),
                        (
                            "05-progress",
                            "output_stage_progress",
                            {
                                "approved_snapshot_id": "approved-cancel",
                                "run_id": "cancel-attempt-1",
                                "stage": "write_output",
                            },
                        ),
                        (
                            "06-staging",
                            "output_staging_created",
                            {
                                "approved_snapshot_id": "approved-cancel",
                                "run_id": "cancel-attempt-1",
                                "output_parent": str(output_parent),
                                "staging_dir": str(
                                    output_parent / ".cancelled-staging"
                                ),
                                "staging_project_path": str(
                                    output_parent
                                    / ".cancelled-staging"
                                    / "Organized_Spectra_cancel.opju"
                                ),
                                "verifier_mutation_path": str(
                                    output_parent
                                    / ".cancelled-staging"
                                    / "Verifier_Mutation_cancel.opju"
                                ),
                            },
                        ),
                        (
                            "07-worker-attempt",
                            "origin_worker_target_attempt",
                            {
                                "role": "output",
                                "attempt": 1,
                                "approved_snapshot_id": "approved-cancel",
                                "run_staging_root": str(
                                    output_parent / ".cancelled-staging"
                                ),
                                "target_states": [
                                    {
                                        "path": str(
                                            output_parent
                                            / ".cancelled-staging"
                                            / "Organized_Spectra_cancel.opju"
                                        ),
                                        "existed_before": True,
                                        "identity": {
                                            "path": str(
                                                output_parent
                                                / ".cancelled-staging"
                                                / "Organized_Spectra_cancel.opju"
                                            ),
                                            "device_id": before.device_id,
                                            "file_id": before.file_id + 20,
                                        },
                                    }
                                ],
                            },
                        ),
                    ):
                        (audit_dir / f"{name}.json").write_text(
                            json.dumps(
                                {
                                    "schema_version": 1,
                                    "event_type": event_type,
                                    "recorded_time_ns": 1,
                                    "process_id": 100,
                                    "process_instance_id": "1" * 32,
                                    "payload": payload,
                                }
                            ),
                            encoding="utf-8",
                        )
                    return 0

            def launch(command, **kwargs):
                calls.append((command, kwargs))
                return FakeProcess()

            evidence_dir = acceptance_module.run_guided_full_acceptance(
                package_dir=package_dir,
                evidence_root=root / "evidence",
                cycle="cancellation",
                process_launcher=launch,
                final_origin_process_count=lambda: 0,
                final_product_process_count=lambda _executable: 0,
                timestamp_factory=lambda: "20260802_230002",
            )

            summary = json.loads(
                (evidence_dir / "manual-cancellation-summary.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                "cancellation_evidence_ready_manual_checks_pending",
                summary["status"],
            )
            self.assertTrue(summary["no_publication_or_staging"])
            self.assertEqual([], list(output_parent.iterdir()))

    def test_worker_target_reconciliation_rejects_original_source_hit(self):
        source = pathlib.Path(r"C:\raw\selected.opju")
        events = (
            _extraction_attempt_event(source, file_id=1),
            {
                "event_type": "origin_worker_targets",
                "payload": {
                    "role": "extraction",
                    "run_id": "run-1",
                    "source_id": "S0001",
                    "reader_attempt": 1,
                    "open_targets": [str(source)],
                    "open_target_identities": [
                        {
                            "path": str(source.resolve()),
                            "device_id": 1,
                            "file_id": 1,
                        }
                    ],
                },
            },
        )

        with self.assertRaisesRegex(ManualAcceptanceError, "original source"):
            acceptance_module._reconcile_worker_targets(
                events,
                selected_paths=(source,),
                context=_worker_audit_context(source),
                require_completed_output=False,
            )

    def test_extraction_worker_must_match_run_and_owned_source_copy(self):
        source = pathlib.Path(r"C:\raw\selected.opju")
        owned_copy = pathlib.Path(r"C:\temp\source-0001\selected.opju")
        context = types.SimpleNamespace(
            run_id="run-1",
            run_owned_source_copy_paths=(owned_copy,),
        )
        event = {
            "event_type": "origin_worker_targets",
            "payload": {
                "role": "extraction",
                "run_id": "wrong-run",
                "source_id": "S0001",
                "reader_attempt": 1,
                "open_targets": [r"C:\unrelated\selected.opju"],
                "open_target_identities": [
                    {
                        "path": r"C:\unrelated\selected.opju",
                        "device_id": 1,
                        "file_id": 10,
                    }
                ],
            },
        }

        with self.assertRaisesRegex(
            ManualAcceptanceError,
            "(?i)extraction worker",
        ):
            acceptance_module._reconcile_worker_targets(
                (event,),
                selected_paths=(source,),
                context=context,
                require_completed_output=False,
            )

    def test_extraction_worker_rejects_duplicate_reader_attempt(self):
        source = pathlib.Path(r"C:\raw\selected.opju")
        owned_copy = pathlib.Path(r"C:\temp\source-0001\selected.opju")
        context = types.SimpleNamespace(
            run_id="run-1",
            run_owned_source_copy_paths=(owned_copy,),
        )
        extraction = {
            "event_type": "origin_worker_targets",
            "payload": {
                "role": "extraction",
                "run_id": "run-1",
                "source_id": "S0001",
                "reader_attempt": 1,
                "open_targets": [str(owned_copy)],
                "open_target_identities": [
                    {
                        "path": str(owned_copy),
                        "device_id": 1,
                        "file_id": 10,
                    }
                ],
            },
        }
        with self.assertRaisesRegex(
            ManualAcceptanceError,
            "duplicate",
        ):
            acceptance_module._reconcile_worker_targets(
                (extraction, extraction),
                selected_paths=(source,),
                context=context,
                require_completed_output=False,
            )

    def test_extraction_worker_requires_parent_attempt_ledger(self):
        source = pathlib.Path(r"C:\raw\selected.opju")
        owned_copy = pathlib.Path(r"C:\temp\source-0001\selected.opju")
        context = types.SimpleNamespace(
            run_id="run-1",
            run_owned_source_copy_paths=(owned_copy,),
        )
        completed = {
            "event_type": "origin_worker_targets",
            "payload": {
                "role": "extraction",
                "run_id": "run-1",
                "source_id": "S0001",
                "reader_attempt": 1,
                "open_targets": [str(owned_copy)],
                "open_target_identities": [
                    {
                        "path": str(owned_copy),
                        "device_id": 1,
                        "file_id": 10,
                    }
                ],
            },
        }

        with self.assertRaisesRegex(
            ManualAcceptanceError,
            "attempt audit is missing",
        ):
            acceptance_module._reconcile_worker_targets(
                (completed,),
                selected_paths=(source,),
                context=context,
                require_completed_output=False,
            )

    def test_extraction_retry_chain_accepts_parent_attempt_cleanup_and_replacement(self):
        source = pathlib.Path(r"C:\raw\selected.opju")
        initial_copy = pathlib.Path(r"C:\temp\source-0001\selected.opju")
        replacement_copy = pathlib.Path(
            r"C:\temp\source-0001\selected.retry.opju"
        )
        context = types.SimpleNamespace(
            run_id="run-1",
            run_owned_source_copy_paths=(initial_copy,),
        )

        def identity(path, file_id):
            return {
                "path": str(path),
                "device_id": 1,
                "file_id": file_id,
            }

        def attempt(number, path, file_id):
            return {
                "event_type": "origin_extraction_target_attempt",
                "payload": {
                    "run_id": "run-1",
                    "source_id": "S0001",
                    "reader_attempt": number,
                    "copy_path": str(path),
                    "copy_identity": identity(path, file_id),
                },
            }

        cleanup = {
            "event_type": "origin_extraction_retry_cleanup",
            "payload": {
                "run_id": "run-1",
                "source_id": "S0001",
                "reader_attempt": 1,
                "failed_copy_path": str(initial_copy),
                "replacement_copy_path": str(replacement_copy),
                "replacement_copy_identity": identity(
                    replacement_copy,
                    11,
                ),
                "completed": True,
            },
        }
        completed = {
            "event_type": "origin_worker_targets",
            "payload": {
                "role": "extraction",
                "run_id": "run-1",
                "source_id": "S0001",
                "reader_attempt": 2,
                "open_targets": [str(replacement_copy)],
                "open_target_identities": [
                    identity(replacement_copy, 11)
                ],
            },
        }

        result = acceptance_module._reconcile_worker_targets(
            (
                attempt(1, initial_copy, 10),
                cleanup,
                attempt(2, replacement_copy, 11),
                completed,
            ),
            selected_paths=(source,),
            context=context,
            require_completed_output=False,
        )

        self.assertEqual(2, len(result["extraction_attempts"]))
        self.assertEqual(1, len(result["extraction_retry_cleanups"]))

    def test_extraction_retry_chain_rejects_reused_path_or_physical_identity(self):
        source = pathlib.Path(r"C:\raw\selected.opju")
        initial_copy = pathlib.Path(r"C:\temp\source-0001\selected.opju")
        replacement_copy = pathlib.Path(
            r"C:\temp\source-0001\selected.retry.opju"
        )
        context = types.SimpleNamespace(
            run_id="run-1",
            run_owned_source_copy_paths=(initial_copy,),
        )

        def identity(path, file_id):
            return {
                "path": str(path),
                "device_id": 1,
                "file_id": file_id,
            }

        for replacement_path, replacement_file_id in (
            (initial_copy, 11),
            (replacement_copy, 10),
        ):
            with self.subTest(
                path=str(replacement_path),
                file_id=replacement_file_id,
            ):
                events = (
                    {
                        "event_type": "origin_extraction_target_attempt",
                        "payload": {
                            "run_id": "run-1",
                            "source_id": "S0001",
                            "reader_attempt": 1,
                            "copy_path": str(initial_copy),
                            "copy_identity": identity(initial_copy, 10),
                        },
                    },
                    {
                        "event_type": "origin_extraction_retry_cleanup",
                        "payload": {
                            "run_id": "run-1",
                            "source_id": "S0001",
                            "reader_attempt": 1,
                            "failed_copy_path": str(initial_copy),
                            "replacement_copy_path": str(replacement_path),
                            "replacement_copy_identity": identity(
                                replacement_path,
                                replacement_file_id,
                            ),
                            "completed": True,
                        },
                    },
                    {
                        "event_type": "origin_extraction_target_attempt",
                        "payload": {
                            "run_id": "run-1",
                            "source_id": "S0001",
                            "reader_attempt": 2,
                            "copy_path": str(replacement_path),
                            "copy_identity": identity(
                                replacement_path,
                                replacement_file_id,
                            ),
                        },
                    },
                    {
                        "event_type": "origin_worker_targets",
                        "payload": {
                            "role": "extraction",
                            "run_id": "run-1",
                            "source_id": "S0001",
                            "reader_attempt": 2,
                            "open_targets": [str(replacement_path)],
                            "open_target_identities": [
                                identity(
                                    replacement_path,
                                    replacement_file_id,
                                )
                            ],
                        },
                    },
                )

                with self.assertRaisesRegex(
                    ManualAcceptanceError,
                    "distinct|generation|identity",
                ):
                    acceptance_module._reconcile_worker_targets(
                        events,
                        selected_paths=(source,),
                        context=context,
                        require_completed_output=False,
                    )

    def test_extraction_retry_chain_requires_cleanup_before_replacement_attempt(self):
        source = pathlib.Path(r"C:\raw\selected.opju")
        initial_copy = pathlib.Path(r"C:\temp\source-0001\selected.opju")
        replacement_copy = pathlib.Path(
            r"C:\temp\source-0001\selected.retry.opju"
        )

        def attempt(number, path, file_id):
            return {
                "event_type": "origin_extraction_target_attempt",
                "payload": {
                    "run_id": "run-1",
                    "source_id": "S0001",
                    "reader_attempt": number,
                    "copy_path": str(path),
                    "copy_identity": {
                        "path": str(path),
                        "device_id": 1,
                        "file_id": file_id,
                    },
                },
            }

        completed = {
            "event_type": "origin_worker_targets",
            "payload": {
                "role": "extraction",
                "run_id": "run-1",
                "source_id": "S0001",
                "reader_attempt": 2,
                "open_targets": [str(replacement_copy)],
                "open_target_identities": [
                    {
                        "path": str(replacement_copy),
                        "device_id": 1,
                        "file_id": 11,
                    }
                ],
            },
        }

        with self.assertRaisesRegex(ManualAcceptanceError, "cleanup"):
            acceptance_module._reconcile_worker_targets(
                (
                    attempt(1, initial_copy, 10),
                    attempt(2, replacement_copy, 11),
                    completed,
                ),
                selected_paths=(source,),
                context=types.SimpleNamespace(
                    run_id="run-1",
                    run_owned_source_copy_paths=(initial_copy,),
                ),
                require_completed_output=False,
            )

    def test_worker_target_reconciliation_rejects_unknown_role(self):
        source = pathlib.Path(r"C:\raw\selected.opju")
        owned_copy = pathlib.Path(r"C:\temp\source-0001\selected.opju")
        context = types.SimpleNamespace(
            run_id="run-1",
            run_owned_source_copy_paths=(owned_copy,),
        )
        events = (
            _extraction_worker_event(owned_copy),
            {
                "event_type": "origin_worker_targets",
                "payload": {
                    "role": "unknown",
                    "open_targets": [r"C:\temp\unknown.opju"],
                    "open_target_identities": [
                        {
                            "path": r"C:\temp\unknown.opju",
                            "device_id": 1,
                            "file_id": 11,
                        }
                    ],
                },
            },
        )

        with self.assertRaisesRegex(
            ManualAcceptanceError,
            "unknown role",
        ):
            acceptance_module._reconcile_worker_targets(
                events,
                selected_paths=(source,),
                context=context,
                require_completed_output=False,
            )

    def test_worker_target_reconciliation_rejects_same_physical_original_alias(self):
        with ExternalTempDir() as root:
            source = root / "selected.opju"
            source.write_bytes(b"immutable")
            alias = root / "selected-hardlink.opju"
            os.link(source, alias)
            snapshot = snapshot_sources([source], [])[0]
            events = (
                _extraction_attempt_event(
                    alias,
                    device_id=snapshot.device_id,
                    file_id=snapshot.file_id,
                ),
                {
                    "event_type": "origin_worker_targets",
                    "payload": {
                        "role": "extraction",
                        "run_id": "run-1",
                        "source_id": "S0001",
                        "reader_attempt": 1,
                        "open_targets": [str(alias)],
                        "open_target_identities": [
                            {
                                "path": str(alias.resolve()),
                                "device_id": snapshot.device_id,
                                "file_id": snapshot.file_id,
                            }
                        ],
                    },
                },
            )

            with self.assertRaisesRegex(
                ManualAcceptanceError,
                "original source",
            ):
                acceptance_module._reconcile_worker_targets(
                    events,
                    selected_paths=(source,),
                    context=_worker_audit_context(alias),
                    selected_snapshots=(snapshot,),
                    require_completed_output=False,
                )

    def test_cancellation_attempt_reconciliation_requires_output_or_verifier_entry(self):
        context = types.SimpleNamespace(
            output_parent=pathlib.Path(r"C:\output"),
            temp_root=pathlib.Path(r"C:\temp\run"),
        )
        events = (
            _extraction_attempt_event(pathlib.Path(r"C:\temp\copy.opju")),
            {
                "event_type": "output_stage_attempt",
                "payload": {
                    "approved_snapshot_id": "approved-1",
                    "run_id": "attempt-1",
                    "output_parent": r"C:\output",
                    "output_parent_existed_before": True,
                    "output_parent_entries_before": [],
                    "task_temp_root": r"C:\temp\run",
                },
            },
            {
                "event_type": "output_stage_progress",
                "payload": {
                    "approved_snapshot_id": "approved-1",
                    "run_id": "attempt-1",
                    "stage": "create_staging",
                },
            },
        )

        with self.assertRaisesRegex(ManualAcceptanceError, "output or verifier"):
            acceptance_module._reconcile_output_attempts(
                events,
                context=context,
                require_output_phase=True,
            )

    def test_cancellation_entered_stage_requires_corresponding_worker_attempt(self):
        with self.assertRaisesRegex(
            ManualAcceptanceError,
            "target-attempt",
        ):
            acceptance_module._require_cancellation_worker_attempts(
                worker_evidence={"attempts": []},
                attempt_summary={
                    "approved_snapshot_id": "approved-1",
                    "entered_stages": ["write_output"],
                    "staging_paths": [r"C:\output\.staging"],
                    "staging_targets": [],
                },
            )

    def test_cancellation_attempt_must_match_output_stage_snapshot_and_staging(self):
        worker_evidence = {
            "attempts": [
                {
                    "role": "output",
                    "attempt": 1,
                    "approved_snapshot_id": "wrong-approved-snapshot",
                    "run_staging_root": r"C:\other\.staging",
                    "target_states": [],
                }
            ]
        }
        attempt_summary = {
            "approved_snapshot_id": "approved-1",
            "entered_stages": ["write_output"],
            "staging_paths": [r"C:\output\.staging"],
            "staging_targets": [
                {
                    "staging_dir": r"C:\output\.staging",
                    "staging_project_path": (
                        r"C:\output\.staging\Organized_Spectra.opju"
                    ),
                    "verifier_mutation_path": (
                        r"C:\output\.staging\Verifier_Mutation.opju"
                    ),
                    "entered_stages": ["write_output"],
                }
            ],
        }

        with self.assertRaisesRegex(
            ManualAcceptanceError,
            "output-stage attempt",
        ):
            acceptance_module._require_cancellation_worker_attempts(
                worker_evidence=worker_evidence,
                attempt_summary=attempt_summary,
            )

    def test_reserved_worker_targets_are_valid_prelaunch_state(self):
        root = r"C:\output\.staging"
        project = rf"{root}\Organized_Spectra.opju"
        mutation = rf"{root}\Verifier_Mutation.opju"

        def state(path, file_id):
            return {
                "path": path,
                "existed_before": True,
                "identity": {
                    "path": path,
                    "device_id": 1,
                    "file_id": file_id,
                },
            }

        acceptance_module._bind_worker_attempts_to_output_stage(
            worker_evidence={
                "attempts": [
                    {
                        "role": "output",
                        "approved_snapshot_id": "approved-1",
                        "run_staging_root": root,
                        "target_states": [state(project, 20)],
                    },
                    {
                        "role": "verifier",
                        "approved_snapshot_id": "approved-1",
                        "run_staging_root": root,
                        "target_states": [
                            state(project, 20),
                            state(mutation, 21),
                        ],
                    },
                ]
            },
            attempt_summary={
                "approved_snapshot_id": "approved-1",
                "staging_targets": [
                    {
                        "staging_dir": root,
                        "staging_project_path": project,
                        "verifier_mutation_path": mutation,
                        "entered_stages": [
                            "write_output",
                            "verify_output",
                        ],
                    }
                ],
            },
        )

    def test_reserved_worker_target_without_exact_identity_is_invalid(self):
        root = r"C:\output\.staging"
        project = rf"{root}\Organized_Spectra.opju"

        with self.assertRaisesRegex(
            ManualAcceptanceError,
            "prelaunch state",
        ):
            acceptance_module._bind_worker_attempts_to_output_stage(
                worker_evidence={
                    "attempts": [
                        {
                            "role": "output",
                            "approved_snapshot_id": "approved-1",
                            "run_staging_root": root,
                            "target_states": [
                                {
                                    "path": project,
                                    "existed_before": True,
                                    "identity": None,
                                }
                            ],
                        }
                    ]
                },
                attempt_summary={
                    "approved_snapshot_id": "approved-1",
                    "staging_targets": [
                        {
                            "staging_dir": root,
                            "staging_project_path": project,
                            "verifier_mutation_path": (
                                rf"{root}\Verifier_Mutation.opju"
                            ),
                            "entered_stages": ["write_output"],
                        }
                    ],
                },
            )

    def test_cancelled_second_attempt_requires_first_retry_cleanup(self):
        selected = pathlib.Path(r"C:\raw\selected.opju")

        def attempt(number):
            return {
                "event_type": "origin_worker_target_attempt",
                "payload": {
                    "role": "output",
                    "attempt": number,
                    "approved_snapshot_id": "approved-1",
                    "run_staging_root": r"C:\output\.staging",
                    "target_states": [
                        {
                            "path": r"C:\output\.staging\output.opju",
                            "existed_before": True,
                            "identity": {
                                "path": r"C:\output\.staging\output.opju",
                                "device_id": 1,
                                "file_id": number + 10,
                            },
                        }
                    ],
                },
            }

        events = (
            _extraction_attempt_event(pathlib.Path(r"C:\temp\copy.opju")),
            {
                "event_type": "origin_worker_targets",
                "payload": {
                    "role": "extraction",
                    "run_id": "run-1",
                    "source_id": "S0001",
                    "reader_attempt": 1,
                    "open_targets": [r"C:\temp\copy.opju"],
                    "open_target_identities": [
                        {
                            "path": r"C:\temp\copy.opju",
                            "device_id": 1,
                            "file_id": 10,
                        }
                    ],
                },
            },
            attempt(1),
            attempt(2),
        )

        with self.assertRaisesRegex(
            ManualAcceptanceError,
            "cleanup",
        ):
            acceptance_module._reconcile_worker_targets(
                events,
                selected_paths=(selected,),
                context=_worker_audit_context(r"C:\temp\copy.opju"),
                require_completed_output=False,
            )

    def test_retry_cleanup_must_match_attempt_snapshot(self):
        selected = pathlib.Path(r"C:\raw\selected.opju")

        def attempt(number):
            return {
                "event_type": "origin_worker_target_attempt",
                "payload": {
                    "role": "output",
                    "attempt": number,
                    "approved_snapshot_id": "approved-1",
                    "run_staging_root": r"C:\output\.staging",
                    "target_states": [
                        {
                            "path": r"C:\output\.staging\output.opju",
                            "existed_before": True,
                            "identity": {
                                "path": r"C:\output\.staging\output.opju",
                                "device_id": 1,
                                "file_id": number + 10,
                            },
                        }
                    ],
                },
            }

        events = (
            _extraction_attempt_event(pathlib.Path(r"C:\temp\copy.opju")),
            {
                "event_type": "origin_worker_targets",
                "payload": {
                    "role": "extraction",
                    "run_id": "run-1",
                    "source_id": "S0001",
                    "reader_attempt": 1,
                    "open_targets": [r"C:\temp\copy.opju"],
                    "open_target_identities": [
                        {
                            "path": r"C:\temp\copy.opju",
                            "device_id": 1,
                            "file_id": 10,
                        }
                    ],
                },
            },
            attempt(1),
            attempt(2),
            {
                "event_type": "origin_worker_retry_cleanup",
                "payload": {
                    "role": "output",
                    "attempt": 1,
                    "approved_snapshot_id": "wrong-approved-snapshot",
                    "run_staging_root": r"C:\output\.staging",
                    "artifact_path": r"C:\output\.staging\output.opju",
                    "artifact_identity": {
                        "path": r"C:\output\.staging\output.opju",
                        "device_id": 1,
                        "file_id": 11,
                    },
                    "completed": True,
                    "error": None,
                },
            },
        )

        with self.assertRaisesRegex(
            ManualAcceptanceError,
            "snapshot",
        ):
            acceptance_module._reconcile_worker_targets(
                events,
                selected_paths=(selected,),
                context=_worker_audit_context(r"C:\temp\copy.opju"),
                require_completed_output=False,
            )

    def test_retry_cleanup_rejects_reused_artifact_generation(self):
        root = r"C:\output\.staging"
        artifact = rf"{root}\output.opju"
        identity = {
            "path": artifact,
            "device_id": 1,
            "file_id": 10,
        }
        attempts = tuple(
            {
                "role": "output",
                "attempt": number,
                "approved_snapshot_id": "approved-1",
                "run_staging_root": root,
                "target_states": [
                    {
                        "path": artifact,
                        "existed_before": True,
                        "identity": dict(identity),
                    }
                ],
            }
            for number in (1, 2)
        )
        cleanups = (
            {
                "role": "output",
                "attempt": 1,
                "approved_snapshot_id": "approved-1",
                "run_staging_root": root,
                "artifact_path": artifact,
                "artifact_identity": dict(identity),
                "completed": True,
                "error": None,
            },
        )

        with self.assertRaisesRegex(
            ManualAcceptanceError,
            "distinct|generation|identity",
        ):
            acceptance_module._reconcile_worker_attempts(
                attempts,
                cleanups,
                completed_events=(),
                all_targets=[],
                target_identities=[],
            )

    def test_retry_cleanup_must_bind_to_an_existing_attempt(self):
        selected = pathlib.Path(r"C:\raw\selected.opju")
        events = (
            _extraction_attempt_event(pathlib.Path(r"C:\temp\copy.opju")),
            {
                "event_type": "origin_worker_targets",
                "payload": {
                    "role": "extraction",
                    "run_id": "run-1",
                    "source_id": "S0001",
                    "reader_attempt": 1,
                    "open_targets": [r"C:\temp\copy.opju"],
                    "open_target_identities": [
                        {
                            "path": r"C:\temp\copy.opju",
                            "device_id": 1,
                            "file_id": 10,
                        }
                    ],
                },
            },
            {
                "event_type": "origin_worker_target_attempt",
                "payload": {
                    "role": "output",
                    "attempt": 1,
                    "approved_snapshot_id": "approved-1",
                    "run_staging_root": r"C:\output\.staging",
                    "target_states": [
                        {
                            "path": r"C:\output\.staging\output.opju",
                            "existed_before": False,
                            "identity": None,
                        }
                    ],
                },
            },
            {
                "event_type": "origin_worker_retry_cleanup",
                "payload": {
                    "role": "output",
                    "attempt": 1,
                    "approved_snapshot_id": "approved-1",
                    "run_staging_root": r"C:\other\.staging",
                    "artifact_path": r"C:\other\.staging\output.opju",
                    "artifact_identity": {
                        "path": r"C:\other\.staging\output.opju",
                        "device_id": 1,
                        "file_id": 11,
                    },
                    "completed": True,
                    "error": None,
                },
            },
        )

        with self.assertRaisesRegex(
            ManualAcceptanceError,
            "existing attempt",
        ):
            acceptance_module._reconcile_worker_targets(
                events,
                selected_paths=(selected,),
                context=_worker_audit_context(r"C:\temp\copy.opju"),
                require_completed_output=False,
            )

    def test_completed_worker_attempt_must_match_snapshot_and_targets(self):
        selected = pathlib.Path(r"C:\raw\selected.opju")
        root = r"C:\output\.staging"
        project = rf"{root}\real.opju"
        mutation = rf"{root}\mutation.opju"

        def attempt(role, paths):
            return {
                "event_type": "origin_worker_target_attempt",
                "payload": {
                    "role": role,
                    "attempt": 1,
                    "approved_snapshot_id": "wrong-approved-snapshot",
                    "run_staging_root": root,
                    "target_states": [
                        {
                            "path": path,
                            "existed_before": False,
                            "identity": None,
                        }
                        for path in paths
                    ],
                },
            }

        def completed(role, paths, first_file_id):
            return {
                "event_type": "origin_worker_targets",
                "payload": {
                    "role": role,
                    "approved_snapshot_id": "approved-1",
                    "run_staging_root": root,
                    "open_targets": list(paths),
                    "open_target_identities": [
                        {
                            "path": path,
                            "device_id": 1,
                            "file_id": first_file_id + index,
                        }
                        for index, path in enumerate(paths)
                    ],
                },
            }

        events = (
            _extraction_attempt_event(pathlib.Path(r"C:\temp\copy.opju")),
            {
                "event_type": "origin_worker_targets",
                "payload": {
                    "role": "extraction",
                    "run_id": "run-1",
                    "source_id": "S0001",
                    "reader_attempt": 1,
                    "open_targets": [r"C:\temp\copy.opju"],
                    "open_target_identities": [
                        {
                            "path": r"C:\temp\copy.opju",
                            "device_id": 1,
                            "file_id": 10,
                        }
                    ],
                },
            },
            attempt("output", (project,)),
            completed("output", (project,), 20),
            attempt("verifier", (project, mutation)),
            completed("verifier", (project, mutation), 20),
        )

        with self.assertRaisesRegex(
            ManualAcceptanceError,
            "snapshot",
        ):
            acceptance_module._reconcile_worker_targets(
                events,
                selected_paths=(selected,),
                context=_worker_audit_context(r"C:\temp\copy.opju"),
            )

    def test_completed_worker_attempt_must_match_exact_target_set(self):
        selected = pathlib.Path(r"C:\raw\selected.opju")
        root = r"C:\output\.staging"
        real_project = rf"{root}\real.opju"
        fake_project = rf"{root}\fake.opju"

        events = (
            _extraction_attempt_event(pathlib.Path(r"C:\temp\copy.opju")),
            {
                "event_type": "origin_worker_targets",
                "payload": {
                    "role": "extraction",
                    "run_id": "run-1",
                    "source_id": "S0001",
                    "reader_attempt": 1,
                    "open_targets": [r"C:\temp\copy.opju"],
                    "open_target_identities": [
                        {
                            "path": r"C:\temp\copy.opju",
                            "device_id": 1,
                            "file_id": 10,
                        }
                    ],
                },
            },
            {
                "event_type": "origin_worker_target_attempt",
                "payload": {
                    "role": "output",
                    "attempt": 1,
                    "approved_snapshot_id": "approved-1",
                    "run_staging_root": root,
                    "target_states": [
                        {
                            "path": fake_project,
                            "existed_before": False,
                            "identity": None,
                        }
                    ],
                },
            },
            {
                "event_type": "origin_worker_targets",
                "payload": {
                    "role": "output",
                    "approved_snapshot_id": "approved-1",
                    "run_staging_root": root,
                    "open_targets": [real_project],
                    "open_target_identities": [
                        {
                            "path": real_project,
                            "device_id": 1,
                            "file_id": 20,
                        }
                    ],
                },
            },
        )

        with self.assertRaisesRegex(
            ManualAcceptanceError,
            "target set",
        ):
            acceptance_module._reconcile_worker_targets(
                events,
                selected_paths=(selected,),
                context=_worker_audit_context(r"C:\temp\copy.opju"),
                require_completed_output=False,
            )

    def test_worker_identity_must_remain_stable_through_verification(self):
        events = _successful_worker_audit_events(
            verifier_attempt_project_file_id=999,
            verifier_completed_project_file_id=888,
        )

        with self.assertRaisesRegex(
            ManualAcceptanceError,
            "identity continuity",
        ):
            acceptance_module._reconcile_worker_targets(
                events,
                selected_paths=(pathlib.Path(r"C:\raw\selected.opju"),),
                context=_worker_audit_context(r"C:\temp\copy.opju"),
            )

    def test_successful_final_attempt_must_not_have_retry_cleanup(self):
        cleanup = {
            "event_type": "origin_worker_retry_cleanup",
            "payload": {
                "role": "output",
                "attempt": 1,
                "approved_snapshot_id": "approved-1",
                "run_staging_root": r"C:\output\.staging",
                "artifact_path": r"C:\output\.staging\Organized_Spectra.opju",
                "artifact_identity": None,
                "completed": False,
                "error": "unexpected cleanup",
            },
        }

        with self.assertRaisesRegex(
            ManualAcceptanceError,
            "successful final attempt",
        ):
            acceptance_module._reconcile_worker_targets(
                (*_successful_worker_audit_events(), cleanup),
                selected_paths=(pathlib.Path(r"C:\raw\selected.opju"),),
                context=_worker_audit_context(r"C:\temp\copy.opju"),
            )

    def test_worker_attempt_target_must_match_staging_event_target(self):
        worker_evidence = {
            "attempts": [
                {
                    "role": "output",
                    "attempt": 1,
                    "approved_snapshot_id": "approved-1",
                    "run_staging_root": r"C:\output\.staging",
                    "target_states": [
                        {
                            "path": r"C:\output\.staging\fake.opju",
                            "existed_before": False,
                            "identity": None,
                        }
                    ],
                }
            ]
        }
        attempt_summary = {
            "approved_snapshot_id": "approved-1",
            "staging_paths": [r"C:\output\.staging"],
            "staging_targets": [
                {
                    "run_id": "run-1",
                    "approved_snapshot_id": "approved-1",
                    "staging_dir": r"C:\output\.staging",
                    "staging_project_path": (
                        r"C:\output\.staging\Organized_Spectra.opju"
                    ),
                    "verifier_mutation_path": (
                        r"C:\output\.staging\Verifier_Mutation.opju"
                    ),
                    "entered_stages": ["write_output"],
                }
            ],
        }

        with self.assertRaisesRegex(
            ManualAcceptanceError,
            "staging target",
        ):
            acceptance_module._bind_worker_attempts_to_output_stage(
                worker_evidence=worker_evidence,
                attempt_summary=attempt_summary,
            )

    def test_worker_attempt_must_match_the_run_that_entered_its_stage(self):
        root = r"C:\output\.staging-b"
        project = rf"{root}\Organized_Spectra.opju"
        worker_evidence = {
            "attempts": [
                {
                    "role": "output",
                    "attempt": 1,
                    "approved_snapshot_id": "approved-1",
                    "run_staging_root": root,
                    "target_states": [
                        {
                            "path": project,
                            "existed_before": False,
                            "identity": None,
                        }
                    ],
                }
            ]
        }
        attempt_summary = {
            "approved_snapshot_id": "approved-1",
            "staging_targets": [
                {
                    "staging_dir": root,
                    "staging_project_path": project,
                    "verifier_mutation_path": (
                        rf"{root}\Verifier_Mutation.opju"
                    ),
                    "entered_stages": ["create_staging"],
                }
            ],
        }

        with self.assertRaisesRegex(
            ManualAcceptanceError,
            "output-stage run",
        ):
            acceptance_module._bind_worker_attempts_to_output_stage(
                worker_evidence=worker_evidence,
                attempt_summary=attempt_summary,
            )

    def test_report_ledger_reconciliation_requires_every_frozen_section(self):
        report_text = "本次设置\n- S1 强度上限：2000000\n\n样品归属\n- A：Book\n"
        events = (
            {
                "event_type": "approved_report_ledger",
                "payload": {
                    "approved_snapshot_id": "approved-1",
                    "recognized_source_paths": [r"C:\raw\source.opju"],
                    "sections": {
                        "本次设置": ["S1 强度上限：2000000"],
                        "样品归属": ["B：Book"],
                    },
                },
            },
        )

        with self.assertRaisesRegex(
            ManualAcceptanceError,
            "(?i)approved snapshot ledger",
        ):
            acceptance_module._reconcile_approved_report_ledger(
                events,
                approved_snapshot_id="approved-1",
                report_text=report_text,
            )

    def test_report_ledger_reconciliation_preserves_multiline_missing_labels(self):
        sections = {
            title: ["无"]
            for title in acceptance_module.APPROVED_OUTPUT_LEDGER_SECTION_TITLES
        }
        sections["不齐全 Folder"] = [
            "F_Ex270\n  缺少样品状态：Sample-A-77 K"
        ]
        report_lines = []
        for title, entries in sections.items():
            report_lines.extend(
                [title, *(f"- {entry}" for entry in entries), ""]
            )
        events = (
            {
                "event_type": "approved_report_ledger",
                "payload": {
                    "approved_snapshot_id": "approved-1",
                    "recognized_source_paths": [r"C:\raw\source.opju"],
                    "sections": sections,
                },
            },
        )

        result = acceptance_module._reconcile_approved_report_ledger(
            events,
            approved_snapshot_id="approved-1",
            report_text="\n".join(report_lines),
        )

        self.assertTrue(result["report_cross_check_passed"])

    def test_success_cleanup_rejects_retained_task_temp_or_staging(self):
        with ExternalTempDir() as root:
            task_temp = root / "task-temp"
            staging = root / "staging"
            task_temp.mkdir()
            staging.mkdir()

            with self.assertRaisesRegex(
                ManualAcceptanceError,
                "success.*temp|staging",
            ):
                acceptance_module._reconcile_owned_output_cleanup(
                    context=types.SimpleNamespace(temp_root=task_temp),
                    attempt_summary={"staging_paths": [str(staging)]},
                    phase="success",
                )

    @unittest.skipUnless(os.name == "nt", "Windows junction behavior")
    def test_success_cleanup_rejects_dangling_task_junction(self):
        import _winapi

        with ExternalTempDir() as root:
            target = root / "junction-target"
            junction = root / "task-temp"
            target.mkdir()
            _winapi.CreateJunction(str(target), str(junction))
            target.rmdir()
            try:
                with self.assertRaisesRegex(
                    ManualAcceptanceError,
                    "temp|staging",
                ):
                    acceptance_module._reconcile_owned_output_cleanup(
                        context=types.SimpleNamespace(temp_root=junction),
                        attempt_summary={"staging_paths": []},
                        phase="success",
                    )
            finally:
                if os.path.lexists(junction):
                    os.rmdir(junction)

    def test_success_cleanup_rejects_orphaned_staging_ownership_sidecar(self):
        with ExternalTempDir() as root:
            staging = root / ".SpectrumOrganizer_staging_run-1"
            marker = staging.with_name(
                f"{staging.name}.ownership.json"
            )
            marker.write_text("{}", encoding="utf-8")

            with self.assertRaisesRegex(
                ManualAcceptanceError,
                "sidecar|staging",
            ):
                acceptance_module._reconcile_owned_output_cleanup(
                    context=types.SimpleNamespace(
                        temp_root=root / "absent-task-temp"
                    ),
                    attempt_summary={
                        "staging_paths": [str(staging)]
                    },
                    phase="success",
                )

    def test_freshness_attestation_requires_recognized_unused_source(self):
        with ExternalTempDir() as root:
            accepted = root / "accepted.opju"
            skipped = root / "skipped.opju"
            accepted.write_bytes(b"accepted raw")
            skipped.write_bytes(b"unsupported project")
            snapshots = tuple(snapshot_sources([accepted, skipped], []))
            evidence_root = root / "evidence"
            evidence_root.mkdir()

            with self.assertRaisesRegex(
                ManualAcceptanceError,
                "recognized|accepted|valid raw",
            ):
                acceptance_module._validated_freshness_attestation(
                    selected_path=skipped,
                    selected_paths=(accepted, skipped),
                    selected_snapshots=snapshots,
                    recognized_source_paths=(accepted,),
                    evidence_root=evidence_root,
                )

            prior = evidence_root / "full-run-extraction-prior"
            prior.mkdir()
            (prior / "extraction-only-summary.json").write_text(
                json.dumps(
                    {
                        "source_fingerprints_before": [
                            acceptance_module._snapshot_to_evidence(
                                snapshots[0]
                            )
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ManualAcceptanceError,
                "previously used",
            ):
                acceptance_module._validated_freshness_attestation(
                    selected_path=accepted,
                    selected_paths=(accepted, skipped),
                    selected_snapshots=snapshots,
                    recognized_source_paths=(accepted,),
                    evidence_root=evidence_root,
                )

    def test_prior_history_includes_guided_selected_source_fingerprints(self):
        with ExternalTempDir() as root:
            source = root / "accepted.opju"
            source.write_bytes(b"accepted raw")
            snapshot = snapshot_sources([source], [])[0]
            prior = root / "full-run-manual-prior"
            prior.mkdir()
            (prior / "selected-source-fingerprints-before.json").write_text(
                json.dumps(
                    {
                        "snapshots": [
                            acceptance_module._snapshot_to_evidence(snapshot)
                        ]
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                {snapshot.sha256},
                acceptance_module._prior_acceptance_source_hashes(root),
            )

    def test_prior_history_includes_interrupted_guided_runtime_context(self):
        with ExternalTempDir() as root:
            source = root / "interrupted.opju"
            source.write_bytes(b"interrupted accepted raw")
            snapshot = snapshot_sources([source], [])[0]
            prior = root / "full-run-manual-interrupted"
            audit_dir = prior / "runtime-audit"
            audit_dir.mkdir(parents=True)
            (audit_dir / "pre-context.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "event_type": "pre_extraction_context",
                        "recorded_time_ns": 1,
                        "process_id": 100,
                        "process_instance_id": "1" * 32,
                        "payload": {
                            "source_fingerprints_before": [
                                acceptance_module._snapshot_to_evidence(
                                    snapshot
                                )
                            ]
                        },
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                {snapshot.sha256},
                acceptance_module._prior_acceptance_source_hashes(root),
            )

    def test_prior_history_accepts_legacy_runtime_context_without_process_instance(self):
        with ExternalTempDir() as root:
            source = root / "legacy.opju"
            source.write_bytes(b"legacy accepted raw")
            snapshot = snapshot_sources([source], [])[0]
            audit_dir = root / "full-run-manual-legacy" / "runtime-audit"
            audit_dir.mkdir(parents=True)
            (audit_dir / "pre-context.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "event_type": "pre_extraction_context",
                        "recorded_time_ns": 1,
                        "process_id": 100,
                        "payload": {
                            "source_fingerprints_before": [
                                acceptance_module._snapshot_to_evidence(
                                    snapshot
                                )
                            ]
                        },
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                {snapshot.sha256},
                acceptance_module._prior_acceptance_source_hashes(root),
            )

    def test_publication_reconciliation_rejects_preexisting_pair_without_commit_event(self):
        with ExternalTempDir() as root:
            final_dir = root / "Organized_Origin_Data_20260802_230000"
            final_dir.mkdir()
            (final_dir / "Organized_Spectra_20260802_230000.opju").write_bytes(
                b"old project"
            )
            (final_dir / "Run_Report_20260802_230000.txt").write_text(
                "old report",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ManualAcceptanceError, "publication"):
                acceptance_module._reconcile_publication_event(
                    (),
                    approved_snapshot_id="approved-1",
                    run_staging_root=(root / ".staging"),
                    expected_project_identity=(1, 1),
                )

    def test_publication_project_identity_must_match_staged_project(self):
        with ExternalTempDir() as root:
            staging = root / ".staging"
            final_dir = root / "Organized_Origin_Data_20260802_230000"
            final_dir.mkdir()
            project = final_dir / "Organized_Spectra_20260802_230000.opju"
            report = final_dir / "Run_Report_20260802_230000.txt"
            project.write_bytes(b"project")
            report.write_text("report", encoding="utf-8")
            artifacts = [
                acceptance_module._snapshot_to_evidence(snapshot)
                for snapshot in snapshot_sources([project, report], [])
            ]
            events = (
                {
                    "event_type": "output_staging_created",
                    "payload": {
                        "approved_snapshot_id": "approved-1",
                        "run_id": "run-1",
                        "output_parent": str(root),
                        "staging_dir": str(staging),
                        "staging_project_path": str(
                            staging / project.name
                        ),
                        "verifier_mutation_path": str(
                            staging / "Verifier_Mutation.opju"
                        ),
                    },
                },
                {
                    "event_type": "publication_committed",
                    "payload": {
                        "approved_snapshot_id": "approved-1",
                        "run_id": "run-1",
                        "output_parent": str(root),
                        "final_run_dir": str(final_dir),
                        "final_project_path": str(project),
                        "final_report_path": str(report),
                        "artifacts": artifacts,
                    },
                },
            )

            with self.assertRaisesRegex(
                ManualAcceptanceError,
                "staged project identity",
            ):
                acceptance_module._reconcile_publication_event(
                    events,
                    approved_snapshot_id="approved-1",
                    run_staging_root=staging,
                    expected_project_identity=(999, 999),
                )

    def test_guided_checklists_are_cycle_specific(self):
        success = acceptance_module._guided_acceptance_checklist("success")
        cancellation = acceptance_module._guided_acceptance_checklist(
            "cancellation"
        )

        self.assertIn("count-reconciliation-summary.json", success)
        self.assertIn("freshness attestation", success)
        self.assertNotIn("count-reconciliation-summary.json", cancellation)
        self.assertNotIn("freshness attestation", cancellation)
        self.assertIn("实际进入输出创建或独立验证", cancellation)
        self.assertIn("任务 temp root 和 staging 均不存在", cancellation)
        self.assertIn("sample_library.sqlite3", success)
        self.assertIn("零写入", success)

    def test_guided_success_rejects_any_isolated_sample_library_write(self):
        with ExternalTempDir() as root:
            runtime_appdata = root / "runtime-localappdata"
            database = (
                runtime_appdata
                / "Spectrum Organizer"
                / "data"
                / "sample_library.sqlite3"
            )
            database.parent.mkdir(parents=True)
            database.write_bytes(b"unexpected sample record store")

            with self.assertRaisesRegex(
                ManualAcceptanceError,
                "sample library|样品库",
            ):
                acceptance_module._reconcile_zero_sample_library_write(
                    runtime_appdata
                )

    def test_guided_success_rejects_transient_sample_library_write_event(self):
        with ExternalTempDir() as root:
            runtime_appdata = root / "runtime-localappdata"
            runtime_audit_dir = root / "runtime-audit"
            runtime_audit_dir.mkdir()
            (runtime_audit_dir / "sample-write.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "event_type": "sample_library_write_attempt",
                        "process_instance_id": "1" * 32,
                        "payload": {
                            "database_path": str(
                                runtime_appdata
                                / "Spectrum Organizer"
                                / "data"
                                / "sample_library.sqlite3"
                            ),
                            "record_count": 1,
                        },
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ManualAcceptanceError,
                "sample library|样品库",
            ):
                acceptance_module._reconcile_zero_sample_library_write(
                    runtime_appdata,
                    runtime_audit_dir=runtime_audit_dir,
                )

    def test_reroute_accepts_paired_workers_and_identical_snapshot_ledgers(self):
        selected = pathlib.Path(r"C:\raw\selected.opju")

        def worker(role, root, targets, file_id):
            return {
                "event_type": "origin_worker_targets",
                "payload": {
                    "role": role,
                    "approved_snapshot_id": "approved-1",
                    "run_staging_root": root,
                    "open_targets": list(targets),
                    "open_target_identities": [
                        {
                            "path": target,
                            "device_id": 1,
                            "file_id": file_id + index,
                        }
                        for index, target in enumerate(targets)
                    ],
                    "spectrum_count": 1,
                    "column_count": 3,
                },
            }

        def attempt(role, root, targets, file_id):
            return {
                "event_type": "origin_worker_target_attempt",
                "payload": {
                    "role": role,
                    "attempt": 1,
                    "approved_snapshot_id": "approved-1",
                    "run_staging_root": root,
                    "target_states": [
                        {
                            "path": target,
                            "existed_before": (
                                role == "verifier" and index == 0
                            ),
                            "identity": (
                                {
                                    "path": target,
                                    "device_id": 1,
                                    "file_id": file_id,
                                }
                                if role == "verifier" and index == 0
                                else None
                            ),
                        }
                        for index, target in enumerate(targets)
                    ],
                },
            }

        extraction = {
            "event_type": "origin_worker_targets",
            "payload": {
                "role": "extraction",
                "run_id": "run-1",
                "source_id": "S0001",
                "reader_attempt": 1,
                "open_targets": [r"C:\temp\copy.opju"],
                "open_target_identities": [
                    {
                        "path": r"C:\temp\copy.opju",
                        "device_id": 1,
                        "file_id": 10,
                    }
                ],
            },
        }
        root_a = r"C:\out-a\.staging-a"
        project_a = rf"{root_a}\a.opju"
        mutation_a = rf"{root_a}\mutation.opju"
        root_b = r"C:\out-b\.staging-b"
        project_b = rf"{root_b}\b.opju"
        mutation_b = rf"{root_b}\mutation.opju"
        events = (
            _extraction_attempt_event(pathlib.Path(r"C:\temp\copy.opju")),
            extraction,
            attempt("output", root_a, (project_a,), 20),
            worker("output", root_a, (project_a,), 20),
            attempt("verifier", root_a, (project_a, mutation_a), 20),
            worker("verifier", root_a, (project_a, mutation_a), 20),
            attempt("output", root_b, (project_b,), 30),
            worker("output", root_b, (project_b,), 30),
            attempt("verifier", root_b, (project_b, mutation_b), 30),
            worker("verifier", root_b, (project_b, mutation_b), 30),
        )

        reconciled = acceptance_module._reconcile_worker_targets(
            events,
            selected_paths=(selected,),
            context=_worker_audit_context(r"C:\temp\copy.opju"),
        )

        self.assertEqual(5, len(reconciled["events"]))

        sections = {
            title: ["无"]
            for title in acceptance_module.APPROVED_OUTPUT_LEDGER_SECTION_TITLES
        }
        report = "\n".join(
            line
            for title, entries in sections.items()
            for line in (title, *(f"- {entry}" for entry in entries), "")
        )
        ledger_event = {
            "event_type": "approved_report_ledger",
            "payload": {
                "approved_snapshot_id": "approved-1",
                "recognized_source_paths": [r"C:\raw\source.opju"],
                "sections": sections,
            },
        }
        ledger = acceptance_module._reconcile_approved_report_ledger(
            (ledger_event, ledger_event),
            approved_snapshot_id="approved-1",
            report_text=report,
        )
        self.assertTrue(ledger["report_cross_check_passed"])

    def test_package_mode_failure_uses_neutral_acceptance_label(self):
        with mock.patch.object(
            acceptance_module,
            "run_guided_full_acceptance",
            side_effect=ManualAcceptanceError("broken package guide"),
        ), mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
            result = acceptance_module.main(
                ["--package-dir", "dist/Spectrum Organizer"]
            )

        self.assertEqual(1, result)
        self.assertIn("manual acceptance failed", stderr.getvalue())
        self.assertNotIn("extraction-only acceptance failed", stderr.getvalue())

    def test_task10_runtime_scan_uses_no_development_reference_identity(self):
        forbidden_tokens = ("paper" + ".opju", "paper" + "_opju")
        offenders = []
        for code_root in (SRC, ROOT / "validation"):
            for path in code_root.rglob("*.py"):
                text = path.read_text(encoding="utf-8").casefold()
                if any(token in text for token in forbidden_tokens):
                    offenders.append(str(path.relative_to(ROOT)))

        self.assertEqual([], offenders)

    def test_context_creation_keyboard_interrupt_becomes_bounded_manual_cancellation(self):
        with ExternalTempDir() as root:
            source, output_parent, _context_builder, runner = _acceptance_case(root)

            def interrupted_builder(**_kwargs):
                raise KeyboardInterrupt

            with self.assertRaisesRegex(ManualAcceptanceError, "取消"):
                run_extraction_only(
                    evidence_root=root / "evidence",
                    source_selector=lambda: (source,),
                    output_selector=lambda: output_parent,
                    dialog_port=FakeDialogPort(),
                    context_builder=interrupted_builder,
                    extraction_runner=runner,
                    final_process_count_hook=lambda: 0,
                    settings_snapshot=CONFIRMED_SETTINGS,
                    timestamp_factory=lambda: "20260709_000000",
                )

            self.assertFalse(any((root / "evidence").rglob("extraction-only-summary.json")))
            self.assertFalse(
                (root / "evidence" / "full-run-extraction-20260709_000000").exists()
            )

    def test_extraction_failure_cleanup_preserves_same_name_replacement_directory(self):
        with ExternalTempDir() as root:
            evidence_root = root / "evidence"
            parked = evidence_root / "parked-owned-evidence"
            foreign_file = (
                evidence_root
                / "full-run-extraction-20260709_000000"
                / "foreign.txt"
            )

            def replace_evidence_then_fail(*, evidence_dir, **_kwargs):
                evidence_dir.rename(parked)
                evidence_dir.mkdir()
                foreign_file.write_text("FOREIGN USER CONTENT", encoding="utf-8")
                raise RuntimeError("injected extraction failure")

            with mock.patch.object(
                acceptance_module,
                "_run_reserved_extraction_in_evidence_dir",
                side_effect=replace_evidence_then_fail,
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "injected extraction failure",
                ):
                    acceptance_module._run_reserved_extraction(
                        evidence_root=evidence_root,
                        sources=(),
                        output_parent=root / "out",
                        settings={},
                        dialog_port=None,
                        context_builder=None,
                        extraction_runner=None,
                        final_process_count_hook=None,
                        origin_process_probe=None,
                        process_controller=None,
                        timestamp_factory=lambda: "20260709_000000",
                        authoritative_snapshots=(),
                    )

            self.assertEqual(
                "FOREIGN USER CONTENT",
                foreign_file.read_text(encoding="utf-8"),
            )
            self.assertTrue(parked.is_dir())

    def test_cli_rejects_alternate_evidence_history_root(self):
        with self.assertRaises(SystemExit):
            acceptance_module._parse_args(
                ["--phase", "extraction-only", "--evidence-root", "alternate-history"]
            )

    def test_main_confirms_preflight_and_passes_snapshot(self):
        confirmed = {
            "s1_limit": 1234,
            "steady_emission_y": "S1c/R1c",
            "allow_missing_s1": True,
        }
        with mock.patch.object(
            acceptance_module,
            "_confirm_settings_with_qt",
            return_value=confirmed,
            create=True,
        ) as confirm, mock.patch.object(
            acceptance_module,
            "run_extraction_only",
            return_value=acceptance_module.DEFAULT_EVIDENCE_ROOT / "evidence",
        ) as run:
            result = acceptance_module.main(["--phase", "extraction-only"])

        self.assertEqual(0, result)
        confirm.assert_called_once_with()
        run.assert_called_once_with(
            evidence_root=acceptance_module.DEFAULT_EVIDENCE_ROOT,
            settings_snapshot={
                "s1Limit": 1234,
                "steadyEmissionY": "S1c/R1c",
                "allowMissingS1": True,
            },
        )

    def test_main_does_not_start_extraction_when_preflight_is_cancelled(self):
        with mock.patch.object(
            acceptance_module,
            "_confirm_settings_with_qt",
            return_value=None,
            create=True,
        ) as confirm, mock.patch.object(
            acceptance_module,
            "run_extraction_only",
        ) as run:
            result = acceptance_module.main(["--phase", "extraction-only"])

        self.assertEqual(1, result)
        confirm.assert_called_once_with()
        run.assert_not_called()

    def test_extraction_only_rejects_missing_confirmed_settings_before_selection(self):
        source_selector = mock.Mock(side_effect=AssertionError("selector must not run"))

        with self.assertRaisesRegex(ManualAcceptanceError, "Confirmed preflight settings"):
            run_extraction_only(
                source_selector=source_selector,
                settings_snapshot=None,
            )

        source_selector.assert_not_called()

    def test_extraction_only_rejects_non_origin_selection(self):
        with WorkspaceTempDir() as root:
            bad = root / "not-origin.txt"
            bad.write_text("x", encoding="utf-8")
            with self.assertRaises(ManualAcceptanceError):
                run_extraction_only(
                    evidence_root=root / "evidence",
                    source_selector=lambda: (bad,),
                    output_selector=lambda: root / "out",
                    dialog_port=FakeDialogPort(),
                    context_builder=_unexpected_context_builder,
                    settings_snapshot=CONFIRMED_SETTINGS,
                    timestamp_factory=lambda: "20260709_000000",
                )

    def test_extraction_only_rejects_workspace_only_sources_before_output_selection(self):
        with WorkspaceTempDir() as root:
            source = root / "raw.opju"
            source.write_bytes(b"origin bytes")
            output_selector = mock.Mock(side_effect=AssertionError("output selector must not run"))

            with self.assertRaisesRegex(ManualAcceptanceError, "outside the workspace"):
                run_extraction_only(
                    evidence_root=root / "evidence",
                    source_selector=lambda: (source,),
                    output_selector=output_selector,
                    settings_snapshot=CONFIRMED_SETTINGS,
                )

            output_selector.assert_not_called()

    def test_extraction_only_does_not_scan_workspace_origin_projects(self):
        with ExternalTempDir() as root:
            source, output_parent, context_builder, runner = _acceptance_case(root)
            original_rglob = pathlib.Path.rglob

            def guarded_rglob(path, pattern):
                if path.resolve() == ROOT.resolve():
                    raise AssertionError("workspace Origin projects must not be scanned")
                return original_rglob(path, pattern)

            with mock.patch.object(pathlib.Path, "rglob", guarded_rglob):
                evidence_dir = run_extraction_only(
                    evidence_root=root / "evidence",
                    source_selector=lambda: (source,),
                    output_selector=lambda: output_parent,
                    context_builder=context_builder,
                    extraction_runner=runner,
                    final_process_count_hook=lambda: 0,
                    settings_snapshot=CONFIRMED_SETTINGS,
                    timestamp_factory=lambda: "20260709_000000",
                )

            self.assertTrue((evidence_dir / "extraction-only-summary.json").is_file())

    def test_extraction_only_rejects_source_fingerprint_from_prior_acceptance(self):
        with ExternalTempDir() as root:
            source, output_parent, context_builder, runner = _acceptance_case(root)
            context_builder = mock.Mock(wraps=context_builder)
            evidence_root = root / "evidence"
            prior = evidence_root / "full-run-extraction-20260708_000000"
            prior.mkdir(parents=True)
            (prior / "extraction-only-summary.json").write_text(
                json.dumps(
                    {
                        "source_fingerprints_before": [
                            {"path": str(source), "sha256": hash_file(source)}
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ManualAcceptanceError, "previously used"):
                run_extraction_only(
                    evidence_root=evidence_root,
                    source_selector=lambda: (source,),
                    output_selector=lambda: output_parent,
                    context_builder=context_builder,
                    extraction_runner=runner,
                    final_process_count_hook=lambda: 0,
                    settings_snapshot=CONFIRMED_SETTINGS,
                )

            self.assertEqual([], runner.contexts)
            context_builder.assert_not_called()

    def test_extraction_only_rejects_malformed_prior_acceptance_summary(self):
        with ExternalTempDir() as root:
            source, output_parent, context_builder, runner = _acceptance_case(root)
            context_builder = mock.Mock(wraps=context_builder)
            prior = root / "evidence" / "full-run-extraction-20260708_000000"
            prior.mkdir(parents=True)
            (prior / "extraction-only-summary.json").write_text("{}", encoding="utf-8")

            with self.assertRaisesRegex(ManualAcceptanceError, "previous manual-acceptance evidence"):
                run_extraction_only(
                    evidence_root=root / "evidence",
                    source_selector=lambda: (source,),
                    output_selector=lambda: output_parent,
                    context_builder=context_builder,
                    extraction_runner=runner,
                    final_process_count_hook=lambda: 0,
                    settings_snapshot=CONFIRMED_SETTINGS,
                )

            self.assertEqual([], runner.contexts)
            context_builder.assert_not_called()

    def test_extraction_only_uses_authoritative_snapshot_after_output_selection(self):
        with ExternalTempDir() as root:
            source, output_parent, _context_builder, runner = _acceptance_case(root)
            shutil.rmtree(root / "copy-root")
            used_bytes = b"previously accepted bytes"
            used_source = root / "used.opju"
            used_source.write_bytes(used_bytes)
            evidence_root = root / "evidence"
            prior = evidence_root / "full-run-extraction-20260708_000000"
            prior.mkdir(parents=True)
            (prior / "extraction-only-summary.json").write_text(
                json.dumps(
                    {
                        "source_fingerprints_before": [
                            {"path": str(used_source), "sha256": hash_file(used_source)}
                        ]
                    }
                ),
                encoding="utf-8",
            )
            copy_path = root / "copy-root" / "source-0001" / source.name

            def output_selector():
                source.write_bytes(used_bytes)
                return output_parent

            def context_builder(**kwargs):
                copy_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, copy_path)
                snapshot = snapshot_sources([source], [])[0]
                return ApprovedPreExtractionRunContext(
                    run_id="run-1",
                    timestamp="2026-07-09T00:00:00Z",
                    selected_source_paths=(source,),
                    output_parent=output_parent,
                    settings_snapshot=dict(kwargs["settings_snapshot"]),
                    source_fingerprints_before=(snapshot,),
                    temp_root=copy_path.parents[1],
                    temp_root_identity=path_identity(copy_path.parents[1]),
                    run_owned_source_copy_paths=(copy_path,),
                )

            context_builder = mock.Mock(wraps=context_builder)

            with self.assertRaisesRegex(ManualAcceptanceError, "previously used"):
                run_extraction_only(
                    evidence_root=evidence_root,
                    source_selector=lambda: (source,),
                    output_selector=output_selector,
                    context_builder=context_builder,
                    extraction_runner=runner,
                    final_process_count_hook=lambda: 0,
                    settings_snapshot=CONFIRMED_SETTINGS,
                )

            self.assertEqual([], runner.contexts)
            context_builder.assert_not_called()
            self.assertFalse(copy_path.exists())

    def test_extraction_only_reserves_fresh_source_until_evidence_is_published(self):
        with ExternalTempDir() as root:
            source, output_parent, context_builder, base_runner = _acceptance_case(root)
            first_runner = SuccessfulBlockingRunner(base_runner.summary)
            first_result = []

            def run_first():
                try:
                    first_result.append(
                        run_extraction_only(
                            evidence_root=root / "evidence",
                            source_selector=lambda: (source,),
                            output_selector=lambda: output_parent,
                            context_builder=context_builder,
                            extraction_runner=first_runner,
                            final_process_count_hook=lambda: 0,
                            settings_snapshot=CONFIRMED_SETTINGS,
                            timestamp_factory=lambda: "20260709_000000",
                        )
                    )
                except BaseException as exc:
                    first_result.append(exc)

            worker = threading.Thread(target=run_first, daemon=True)
            worker.start()
            self.assertTrue(first_runner.entered.wait(2.0))
            try:
                with self.assertRaisesRegex(ManualAcceptanceError, "already|active|reservation"):
                    run_extraction_only(
                        evidence_root=root / "evidence",
                        source_selector=lambda: (source,),
                        output_selector=lambda: output_parent,
                        context_builder=context_builder,
                        extraction_runner=base_runner,
                        final_process_count_hook=lambda: 0,
                        settings_snapshot=CONFIRMED_SETTINGS,
                        timestamp_factory=lambda: "20260709_000001",
                    )
            finally:
                first_runner.release.set()
                worker.join(2.0)

            self.assertFalse(worker.is_alive())
            self.assertEqual(1, len(first_result))
            self.assertIsInstance(first_result[0], pathlib.Path)

    def test_extraction_only_treats_cancelled_output_selection_as_cancellation(self):
        with ExternalTempDir() as root:
            source = root / "raw.opju"
            source.write_bytes(b"origin bytes")

            with self.assertRaisesRegex(ManualAcceptanceError, "No output parent folder was selected"):
                run_extraction_only(
                    evidence_root=root / "evidence",
                    source_selector=lambda: (source,),
                    output_selector=lambda: None,
                    dialog_port=FakeDialogPort(),
                    context_builder=_unexpected_context_builder,
                    settings_snapshot=CONFIRMED_SETTINGS,
                    timestamp_factory=lambda: "20260709_000000",
                )

    def test_extraction_only_uses_production_summary_and_preserves_confirmed_settings(self):
        with ExternalTempDir() as root:
            source = root / "raw.opju"
            source.write_bytes(b"origin bytes")
            output_parent = root / "output"
            output_parent.mkdir()
            copy_path = root / "copy-root" / "source-0001" / source.name
            copy_path.parent.mkdir(parents=True)
            shutil.copy2(source, copy_path)
            snapshot = snapshot_sources([source], [])[0]
            settings = {"s1Limit": 1234, "steadyEmissionY": "S1c/R1c", "allowMissingS1": True}
            first_attempt_copy = root / "copy-root" / "source-0001-attempt1" / source.name
            phase_summary = replace(
                _phase_summary(root, source, copy_path),
                worker_open_targets=(str(first_attempt_copy), str(copy_path)),
            )
            runner = FakeExtractionRunner(phase_summary)

            def context_builder(**kwargs):
                self.assertEqual((source,), tuple(kwargs["selected_source_paths"]))
                self.assertEqual(output_parent, pathlib.Path(kwargs["output_parent"]))
                self.assertEqual(settings, kwargs["settings_snapshot"])
                return ApprovedPreExtractionRunContext(
                    run_id="run-1",
                    timestamp="2026-07-09T00:00:00Z",
                    selected_source_paths=(source,),
                    output_parent=output_parent,
                    settings_snapshot=dict(kwargs["settings_snapshot"]),
                    source_fingerprints_before=(snapshot,),
                    temp_root=copy_path.parents[1],
                    temp_root_identity=path_identity(copy_path.parents[1]),
                    run_owned_source_copy_paths=(copy_path,),
                )

            evidence_dir = run_extraction_only(
                evidence_root=root / "evidence",
                source_selector=lambda: (source,),
                output_selector=lambda: output_parent,
                dialog_port=FakeDialogPort(),
                context_builder=context_builder,
                extraction_runner=runner,
                final_process_count_hook=lambda: 0,
                settings_snapshot=settings,
                timestamp_factory=lambda: "20260709_000000",
            )
            payload = json.loads((evidence_dir / "extraction-only-summary.json").read_text(encoding="utf-8"))

        self.assertEqual("extraction-only", payload["phase"])
        self.assertEqual([str(source)], payload["selected_source_paths"])
        self.assertEqual(
            [str(first_attempt_copy), str(copy_path)],
            payload["worker_open_targets"],
        )
        self.assertEqual([], payload["protected_source_open_target_hits"])
        self.assertEqual(1, payload["source_summaries"][0]["inventory_count"])
        self.assertEqual(1, payload["source_summaries"][0]["extracted_count"])
        self.assertEqual(settings, payload["settings_snapshot"])
        self.assertEqual(1, len(runner.contexts))

    def test_extraction_only_refuses_publication_when_worker_opened_selected_source(self):
        with ExternalTempDir() as root:
            source, output_parent, context_builder, runner = _acceptance_case(root)
            runner.summary = replace(runner.summary, worker_open_targets=(str(source),))

            with self.assertRaisesRegex(ManualAcceptanceError, "selected original source"):
                run_extraction_only(
                    evidence_root=root / "evidence",
                    source_selector=lambda: (source,),
                    output_selector=lambda: output_parent,
                    dialog_port=FakeDialogPort(),
                    context_builder=context_builder,
                    extraction_runner=runner,
                    final_process_count_hook=lambda: 1,
                    settings_snapshot=CONFIRMED_SETTINGS,
                    timestamp_factory=lambda: "20260709_000000",
                )

            self.assertFalse(any((root / "evidence").rglob("extraction-only-summary.json")))

    def test_json_publication_failure_leaves_no_partial_final_file(self):
        with ExternalTempDir() as root:
            destination = root / "summary.json"

            def fail_after_partial_write(payload, handle, **kwargs):
                handle.write('{"partial":')
                raise RuntimeError("serialization failed")

            with mock.patch.object(acceptance_module.json, "dump", side_effect=fail_after_partial_write):
                with self.assertRaisesRegex(RuntimeError, "serialization failed"):
                    acceptance_module._write_json_exclusive(destination, {"ok": True})

            self.assertFalse(destination.exists())
            self.assertEqual([], list(root.iterdir()))

    def test_json_publication_does_not_fail_after_final_file_is_committed(self):
        with ExternalTempDir() as root:
            destination = root / "summary.json"

            with mock.patch.object(
                pathlib.Path,
                "unlink",
                side_effect=PermissionError("temporary cleanup failed"),
            ):
                acceptance_module._write_json_exclusive(destination, {"ok": True})

            self.assertEqual({"ok": True}, json.loads(destination.read_text(encoding="utf-8")))

    def test_committed_extraction_evidence_survives_reservation_release_failure(self):
        with ExternalTempDir() as root:
            source, output_parent, context_builder, runner = _acceptance_case(root)

            with mock.patch.object(
                acceptance_module,
                "release_owned_directory_lock",
                side_effect=PermissionError("reservation cleanup failed"),
            ):
                evidence_dir = run_extraction_only(
                    evidence_root=root / "evidence",
                    source_selector=lambda: (source,),
                    output_selector=lambda: output_parent,
                    dialog_port=FakeDialogPort(),
                    context_builder=context_builder,
                    extraction_runner=runner,
                    final_process_count_hook=lambda: 0,
                    settings_snapshot=CONFIRMED_SETTINGS,
                    timestamp_factory=lambda: "20260709_000000",
                )

            self.assertTrue((evidence_dir / "extraction-only-summary.json").exists())

    def test_extraction_only_refuses_physical_alias_of_selected_source(self):
        with ExternalTempDir() as root:
            source, output_parent, context_builder, runner = _acceptance_case(root)
            alias = root / "source-alias.opju"
            os.link(source, alias)
            runner.summary = replace(runner.summary, worker_open_targets=(str(alias),))

            with self.assertRaisesRegex(ManualAcceptanceError, "selected original source"):
                run_extraction_only(
                    evidence_root=root / "evidence",
                    source_selector=lambda: (source,),
                    output_selector=lambda: output_parent,
                    dialog_port=FakeDialogPort(),
                    context_builder=context_builder,
                    extraction_runner=runner,
                    final_process_count_hook=lambda: 0,
                    settings_snapshot=CONFIRMED_SETTINGS,
                    timestamp_factory=lambda: "20260709_000000",
                )

            self.assertFalse(any((root / "evidence").rglob("extraction-only-summary.json")))

    def test_extraction_only_refuses_publication_when_origin_process_remains(self):
        with ExternalTempDir() as root:
            source, output_parent, context_builder, runner = _acceptance_case(root)

            with self.assertRaisesRegex(ManualAcceptanceError, "Origin"):
                run_extraction_only(
                    evidence_root=root / "evidence",
                    source_selector=lambda: (source,),
                    output_selector=lambda: output_parent,
                    dialog_port=FakeDialogPort(),
                    context_builder=context_builder,
                    extraction_runner=runner,
                    final_process_count_hook=lambda: 1,
                    settings_snapshot=CONFIRMED_SETTINGS,
                    timestamp_factory=lambda: "20260709_000000",
                )

            self.assertFalse(any((root / "evidence").rglob("extraction-only-summary.json")))

    def test_extraction_only_default_runner_uses_production_process_dependencies(self):
        with ExternalTempDir() as root:
            source, output_parent, context_builder, runner = _acceptance_case(root)
            process_probe = mock.Mock(return_value=())
            process_controller = object()
            process_count_hook = mock.Mock(return_value=0)

            with mock.patch.object(
                acceptance_module,
                "ExtractionSubprocessRunner",
                return_value=runner,
            ) as runner_constructor, mock.patch.object(
                acceptance_module,
                "FinalProcessCountHook",
                return_value=process_count_hook,
            ) as count_hook_constructor:
                run_extraction_only(
                    evidence_root=root / "evidence",
                    source_selector=lambda: (source,),
                    output_selector=lambda: output_parent,
                    dialog_port=FakeDialogPort(),
                    context_builder=context_builder,
                    origin_process_probe=process_probe,
                    process_controller=process_controller,
                    settings_snapshot=CONFIRMED_SETTINGS,
                    timestamp_factory=lambda: "20260709_000000",
                )

            runner_constructor.assert_called_once_with(
                origin_process_probe=process_probe,
                origin_process_controller=process_controller,
            )
            count_hook_constructor.assert_called_once_with(process_probe)
            process_count_hook.assert_called_once_with()

    def test_extraction_only_keyboard_interrupt_cancels_runner_before_returning(self):
        with ExternalTempDir() as root:
            source, output_parent, context_builder, base_runner = _acceptance_case(root)
            runner = InterruptingExtractionRunner(base_runner.summary)
            process_count_hook = mock.Mock(return_value=0)

            with self.assertRaisesRegex(ManualAcceptanceError, "取消"):
                run_extraction_only(
                    evidence_root=root / "evidence",
                    source_selector=lambda: (source,),
                    output_selector=lambda: output_parent,
                    dialog_port=FakeDialogPort(),
                    context_builder=context_builder,
                    extraction_runner=runner,
                    final_process_count_hook=process_count_hook,
                    settings_snapshot=CONFIRMED_SETTINGS,
                    timestamp_factory=lambda: "20260709_000000",
                )

            self.assertTrue(runner.cancelled)
            process_count_hook.assert_called_once_with()
            self.assertFalse(any((root / "evidence").rglob("extraction-only-summary.json")))
            self.assertFalse(
                (root / "evidence" / "full-run-extraction-20260709_000000").exists()
            )

    def test_extraction_only_cancellation_never_returns_with_live_worker_thread(self):
        with ExternalTempDir() as root:
            source, output_parent, context_builder, base_runner = _acceptance_case(root)
            runner = InterruptingExtractionRunner(base_runner.summary)

            with self.assertRaisesRegex(ManualAcceptanceError, "取消"):
                run_extraction_only(
                    evidence_root=root / "evidence",
                    source_selector=lambda: (source,),
                    output_selector=lambda: output_parent,
                    dialog_port=FakeDialogPort(),
                    context_builder=context_builder,
                    extraction_runner=runner,
                    final_process_count_hook=lambda: 0,
                    settings_snapshot=CONFIRMED_SETTINGS,
                    timestamp_factory=lambda: "20260709_000000",
                )

            self.assertIs(threading.current_thread(), runner.calling_thread)
            self.assertFalse(
                any(
                    thread.name == "manual-acceptance-extraction" and thread.is_alive()
                    for thread in threading.enumerate()
                )
            )

    def test_extraction_only_keeps_cancellation_bounded_when_cancel_hook_raises(self):
        with ExternalTempDir() as root:
            source, output_parent, context_builder, base_runner = _acceptance_case(root)
            runner = InterruptingExtractionRunner(
                base_runner.summary,
                cancel_error=RuntimeError("cancel hook failed"),
            )

            with self.assertRaisesRegex(ManualAcceptanceError, "取消") as raised:
                run_extraction_only(
                    evidence_root=root / "evidence",
                    source_selector=lambda: (source,),
                    output_selector=lambda: output_parent,
                    context_builder=context_builder,
                    extraction_runner=runner,
                    final_process_count_hook=lambda: 0,
                    settings_snapshot=CONFIRMED_SETTINGS,
                    timestamp_factory=lambda: "20260709_000000",
                )

            self.assertTrue(any("cancel hook failed" in note for note in raised.exception.__notes__))

    def test_extraction_only_preserves_cleanup_blocked_error_during_cancellation(self):
        with ExternalTempDir() as root:
            source, output_parent, context_builder, base_runner = _acceptance_case(root)
            cleanup_error = ExtractionCleanupBlockedError("cleanup remains blocked")
            runner = InterruptingExtractionRunner(base_runner.summary, cancel_error=cleanup_error)

            with self.assertRaises(ExtractionCleanupBlockedError) as raised:
                run_extraction_only(
                    evidence_root=root / "evidence",
                    source_selector=lambda: (source,),
                    output_selector=lambda: output_parent,
                    dialog_port=FakeDialogPort(),
                    context_builder=context_builder,
                    extraction_runner=runner,
                    final_process_count_hook=lambda: 0,
                    settings_snapshot=CONFIRMED_SETTINGS,
                    timestamp_factory=lambda: "20260709_000000",
                )

            self.assertIs(cleanup_error, raised.exception)

    def test_extraction_only_source_mismatch_supersedes_generic_cancellation(self):
        with ExternalTempDir() as root:
            source, output_parent, context_builder, base_runner = _acceptance_case(root)
            runner = InterruptingExtractionRunner(
                base_runner.summary,
                cancel_side_effect=lambda: source.write_bytes(b"changed"),
            )
            process_count_hook = mock.Mock(return_value=0)

            with self.assertRaises(SnapshotMismatchError) as raised:
                run_extraction_only(
                    evidence_root=root / "evidence",
                    source_selector=lambda: (source,),
                    output_selector=lambda: output_parent,
                    dialog_port=FakeDialogPort(),
                    context_builder=context_builder,
                    extraction_runner=runner,
                    final_process_count_hook=process_count_hook,
                    settings_snapshot=CONFIRMED_SETTINGS,
                    timestamp_factory=lambda: "20260709_000000",
                )

            process_count_hook.assert_called_once_with()
            self.assertTrue(any("取消" in note for note in raised.exception.__notes__))

    def test_extraction_only_residual_origin_supersedes_generic_cancellation(self):
        with ExternalTempDir() as root:
            source, output_parent, context_builder, base_runner = _acceptance_case(root)
            runner = InterruptingExtractionRunner(base_runner.summary)

            with self.assertRaisesRegex(ManualAcceptanceError, "Origin process count is 1"):
                run_extraction_only(
                    evidence_root=root / "evidence",
                    source_selector=lambda: (source,),
                    output_selector=lambda: output_parent,
                    dialog_port=FakeDialogPort(),
                    context_builder=context_builder,
                    extraction_runner=runner,
                    final_process_count_hook=lambda: 1,
                    settings_snapshot=CONFIRMED_SETTINGS,
                    timestamp_factory=lambda: "20260709_000000",
                )

    def test_extraction_only_preserves_run_error_when_final_process_probe_fails(self):
        with ExternalTempDir() as root:
            source, output_parent, context_builder, base_runner = _acceptance_case(root)
            runner = mock.Mock(side_effect=ValueError("run failed"))

            with self.assertRaisesRegex(ValueError, "run failed") as raised:
                run_extraction_only(
                    evidence_root=root / "evidence",
                    source_selector=lambda: (source,),
                    output_selector=lambda: output_parent,
                    context_builder=context_builder,
                    extraction_runner=runner,
                    final_process_count_hook=mock.Mock(side_effect=RuntimeError("probe failed")),
                    settings_snapshot=CONFIRMED_SETTINGS,
                    timestamp_factory=lambda: "20260709_000000",
                )

            self.assertTrue(any("probe failed" in note for note in raised.exception.__notes__))

    def test_source_selector_keeps_created_qapplication_alive(self):
        module, dialog_saw_application = _fake_pyside6_selector_module()
        with mock.patch.dict(sys.modules, {"PySide6": module}):
            selected = acceptance_module._select_sources_with_qt()

        self.assertEqual((pathlib.Path("selected.opju"),), selected)
        self.assertTrue(dialog_saw_application())

    def test_output_selector_keeps_created_qapplication_alive(self):
        module, dialog_saw_application = _fake_pyside6_selector_module()
        with mock.patch.dict(sys.modules, {"PySide6": module}):
            selected = acceptance_module._select_output_parent_with_qt()

        self.assertEqual(pathlib.Path("selected-output"), selected)
        self.assertTrue(dialog_saw_application())

    def test_main_retains_one_qapplication_for_the_complete_cli_workflow(self):
        module, application_ref = _fake_pyside6_application_module()

        def confirm_settings():
            self.assertIsNotNone(application_ref())
            return {
                "s1_limit": CONFIRMED_SETTINGS["s1Limit"],
                "steady_emission_y": CONFIRMED_SETTINGS["steadyEmissionY"],
                "allow_missing_s1": CONFIRMED_SETTINGS["allowMissingS1"],
            }

        def run_acceptance(**_kwargs):
            self.assertIsNotNone(application_ref())
            return pathlib.Path("evidence")

        with mock.patch.dict(sys.modules, {"PySide6": module}), mock.patch.object(
            acceptance_module,
            "_confirm_settings_with_qt",
            side_effect=confirm_settings,
        ), mock.patch.object(
            acceptance_module,
            "run_extraction_only",
            side_effect=run_acceptance,
        ):
            self.assertEqual(0, acceptance_module.main(["--phase", "extraction-only"]))

        self.assertIsNone(application_ref())

    def test_main_prints_secondary_exception_notes(self):
        error = ManualAcceptanceError("primary failure")
        error.add_note("secondary cleanup failure")
        stderr = io.StringIO()

        with mock.patch.object(
            acceptance_module,
            "_ensure_qapplication",
            return_value=object(),
        ), mock.patch.object(
            acceptance_module,
            "_confirm_settings_with_qt",
            return_value={
                "s1_limit": CONFIRMED_SETTINGS["s1Limit"],
                "steady_emission_y": CONFIRMED_SETTINGS["steadyEmissionY"],
                "allow_missing_s1": CONFIRMED_SETTINGS["allowMissingS1"],
            },
        ), mock.patch.object(
            acceptance_module,
            "run_extraction_only",
            side_effect=error,
        ), mock.patch.object(
            acceptance_module.sys,
            "stderr",
            stderr,
        ):
            self.assertEqual(1, acceptance_module.main(["--phase", "extraction-only"]))

        output = stderr.getvalue()
        self.assertIn("primary failure", output)
        self.assertIn("secondary cleanup failure", output)

    def test_secondary_error_aggregation_preserves_secondary_notes(self):
        primary = ManualAcceptanceError("primary")
        secondary = ManualAcceptanceError("secondary")
        secondary.add_note("nested cleanup detail")

        acceptance_module._add_secondary_error(primary, secondary)

        self.assertEqual(
            ["secondary", "nested cleanup detail"],
            getattr(primary, "__notes__", []),
        )


def _successful_worker_audit_events(
    *,
    verifier_attempt_project_file_id=20,
    verifier_completed_project_file_id=20,
):
    root = r"C:\output\.staging"
    project = rf"{root}\Organized_Spectra.opju"
    mutation = rf"{root}\Verifier_Mutation.opju"

    def identity(path, file_id):
        return {
            "path": path,
            "device_id": 1,
            "file_id": file_id,
        }

    def completed(role, paths, file_ids):
        return {
            "event_type": "origin_worker_targets",
            "payload": {
                "role": role,
                "approved_snapshot_id": "approved-1",
                "run_staging_root": root,
                "open_targets": list(paths),
                "open_target_identities": [
                    identity(path, file_id)
                    for path, file_id in zip(paths, file_ids, strict=True)
                ],
            },
        }

    return (
        _extraction_attempt_event(
            pathlib.Path(r"C:\temp\copy.opju")
        ),
        _extraction_worker_event(pathlib.Path(r"C:\temp\copy.opju")),
        {
            "event_type": "origin_worker_target_attempt",
            "payload": {
                "role": "output",
                "attempt": 1,
                "approved_snapshot_id": "approved-1",
                "run_staging_root": root,
                "target_states": [
                    {
                        "path": project,
                        "existed_before": False,
                        "identity": None,
                    }
                ],
            },
        },
        completed("output", (project,), (20,)),
        {
            "event_type": "origin_worker_target_attempt",
            "payload": {
                "role": "verifier",
                "attempt": 1,
                "approved_snapshot_id": "approved-1",
                "run_staging_root": root,
                "target_states": [
                    {
                        "path": project,
                        "existed_before": True,
                        "identity": identity(
                            project,
                            verifier_attempt_project_file_id,
                        ),
                    },
                    {
                        "path": mutation,
                        "existed_before": False,
                        "identity": None,
                    },
                ],
            },
        },
        completed(
            "verifier",
            (project, mutation),
            (verifier_completed_project_file_id, 21),
        ),
    )


def _extraction_worker_event(copy_path):
    path = str(copy_path)
    return {
        "event_type": "origin_worker_targets",
        "payload": {
            "role": "extraction",
            "run_id": "run-1",
            "source_id": "S0001",
            "reader_attempt": 1,
            "open_targets": [path],
            "open_target_identities": [
                {
                    "path": path,
                    "device_id": 1,
                    "file_id": 10,
                }
            ],
        },
    }


def _extraction_attempt_event(
    copy_path,
    *,
    device_id=1,
    file_id=10,
    run_id="run-1",
):
    path = str(copy_path)
    return {
        "event_type": "origin_extraction_target_attempt",
        "payload": {
            "run_id": run_id,
            "source_id": "S0001",
            "reader_attempt": 1,
            "copy_path": path,
            "copy_identity": {
                "path": path,
                "device_id": device_id,
                "file_id": file_id,
            },
        },
    }


def _worker_audit_context(copy_path):
    return types.SimpleNamespace(
        run_id="run-1",
        run_owned_source_copy_paths=(pathlib.Path(copy_path),),
    )


def _unexpected_context_builder(**kwargs):
    raise AssertionError("context builder should not run")


def _fake_pyside6_selector_module():
    state = {}

    class FakeApplication:
        @staticmethod
        def instance():
            return None

        def __init__(self, _args):
            state["application_ref"] = weakref.ref(self)

    class FakeFileDialog:
        @staticmethod
        def getOpenFileNames(*_args):
            if state["application_ref"]() is None:
                raise AssertionError("QApplication was destroyed before the source dialog")
            state["dialog_saw_application"] = True
            return ["selected.opju"], ""

        @staticmethod
        def getExistingDirectory(*_args):
            if state["application_ref"]() is None:
                raise AssertionError("QApplication was destroyed before the output dialog")
            state["dialog_saw_application"] = True
            return "selected-output"

    module = types.ModuleType("PySide6")
    module.QtWidgets = types.SimpleNamespace(
        QApplication=FakeApplication,
        QFileDialog=FakeFileDialog,
    )
    return module, lambda: state.get("dialog_saw_application", False)


def _fake_pyside6_application_module():
    state = {}

    class FakeApplication:
        _instance_ref = None

        @classmethod
        def instance(cls):
            return cls._instance_ref() if cls._instance_ref is not None else None

        def __init__(self, _args):
            type(self)._instance_ref = weakref.ref(self)
            state["application_ref"] = weakref.ref(self)

    module = types.ModuleType("PySide6")
    module.QtWidgets = types.SimpleNamespace(QApplication=FakeApplication)
    return module, lambda: state.get("application_ref", lambda: None)()


def _phase_summary(root, source, copy_path):
    source_summary = SourceExtractionSummary(
        source_id="S0001",
        original_path=str(source),
        copy_path=str(copy_path),
        inventory_count=1,
        result_count=1,
        extracted_count=1,
        rejected_count=0,
    )
    return ExtractionPhaseSummary(
        snapshot_path=root / "run_snapshot.sqlite3",
        source_summaries=(source_summary,),
        total_inventory_count=1,
        total_result_count=1,
        total_extracted_count=1,
        total_rejected_count=0,
        snapshot_sha256="approval-sha",
        worker_open_targets=(str(copy_path),),
    )


def _acceptance_case(root):
    source = root / "raw.opju"
    source.write_bytes(b"origin bytes")
    output_parent = root / "output"
    output_parent.mkdir()
    copy_path = root / "copy-root" / "source-0001" / source.name
    copy_path.parent.mkdir(parents=True)
    shutil.copy2(source, copy_path)
    snapshot = snapshot_sources([source], [])[0]

    def context_builder(**kwargs):
        return ApprovedPreExtractionRunContext(
            run_id="run-1",
            timestamp="2026-07-09T00:00:00Z",
            selected_source_paths=(source,),
            output_parent=output_parent,
            settings_snapshot=dict(kwargs["settings_snapshot"]),
            source_fingerprints_before=(snapshot,),
            temp_root=copy_path.parents[1],
            temp_root_identity=path_identity(copy_path.parents[1]),
            run_owned_source_copy_paths=(copy_path,),
        )

    return source, output_parent, context_builder, FakeExtractionRunner(_phase_summary(root, source, copy_path))


if __name__ == "__main__":
    unittest.main()
