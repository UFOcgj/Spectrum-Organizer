import json
from concurrent.futures import ThreadPoolExecutor
import inspect
import pathlib
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from validation import task17_validation_workflow_smoke as workflow_smoke
from validation.packaged_workflow import PackagedWorkflowSummary


class Task17ValidationWorkflowSmokeTests(unittest.TestCase):
    def test_validation_workflow_cli_bootstraps_direct_and_module_launches(self):
        commands = (
            [sys.executable, "-B", str(ROOT / "validation" / "task17_validation_workflow_smoke.py"), "--help"],
            [sys.executable, "-B", "-m", "validation.task17_validation_workflow_smoke", "--help"],
        )

        for command in commands:
            with self.subTest(command=command):
                completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=30)
                self.assertEqual(0, completed.returncode, completed.stderr)
                self.assertIn("--evidence-dir", completed.stdout)

    def test_validation_workflow_cli_executes_direct_and_module_workflows(self):
        commands = (
            [sys.executable, "-B", str(ROOT / "validation" / "task17_validation_workflow_smoke.py")],
            [sys.executable, "-B", "-m", "validation.task17_validation_workflow_smoke"],
        )

        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            for index, command in enumerate(commands):
                with self.subTest(command=command):
                    evidence_dir = root / f"evidence-{index}"
                    completed = subprocess.run(
                        [*command, "--evidence-dir", str(evidence_dir)],
                        cwd=ROOT,
                        capture_output=True,
                        text=True,
                        timeout=60,
                    )
                    self.assertEqual(0, completed.returncode, completed.stderr)
                    summary = json.loads(
                        (evidence_dir / "validation-workflow-summary.json").read_text(encoding="utf-8")
                    )
                    self.assertEqual("completion", summary["app_summary"]["final_stage"])
                    self.assertFalse(pathlib.Path(summary["task_root"]).exists())

    def test_validation_workflow_cli_propagates_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            evidence_file = pathlib.Path(temp) / "not-a-directory"
            evidence_file.write_text("occupied", encoding="utf-8")
            completed = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(ROOT / "validation" / "task17_validation_workflow_smoke.py"),
                    "--evidence-dir",
                    str(evidence_file),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=30,
            )

        self.assertNotEqual(0, completed.returncode)
        self.assertFalse((evidence_file / "validation-workflow-summary.json").exists())

    def test_production_entrypoint_and_package_exclude_validation_dry_run(self):
        main_source = (SRC / "spectrum_organizer" / "__main__.py").read_text(encoding="utf-8")
        spec_source = (ROOT / "packaging" / "spectrum_organizer.spec").read_text(encoding="utf-8")

        self.assertNotIn("--non-origin-dry-run", main_source)
        self.assertNotIn("spectrum_organizer.packaged_workflow", spec_source)
        self.assertNotIn("spectrum_organizer.dry_run", spec_source)
        self.assertFalse((SRC / "spectrum_organizer" / "packaged_workflow.py").exists())

    def test_validation_workflow_smoke_uses_c_tmp_for_owned_sources_and_output(self):
        self.assertEqual(pathlib.Path(r"C:\tmp"), workflow_smoke.TMP_ROOT)
        source = "\n".join(
            (
                inspect.getsource(workflow_smoke.run_validation_workflow_smoke),
                inspect.getsource(workflow_smoke._run_validation_workflow_smoke_locked),
            )
        )

        self.assertNotIn("--non-origin-dry-run", source)
        self.assertIn("selected-sources", source)
        self.assertIn("chosen-output", source)
        self.assertIn("runtime-localappdata", source)
        self.assertIn("sources_unchanged", source)
        self.assertIn("validation_failures", source)

    def test_validation_workflow_smoke_does_not_claim_unobserved_package_process_evidence(self):
        source = "\n".join(
            (
                inspect.getsource(workflow_smoke.run_validation_workflow_smoke),
                inspect.getsource(workflow_smoke._run_validation_workflow_smoke_locked),
            )
        )

        self.assertNotIn("timeout_seconds", source)
        self.assertNotIn('"returncode"', source)
        self.assertNotIn('"shutdown"', source)
        self.assertNotIn('"stdout"', source)
        self.assertNotIn('"stderr"', source)
        self.assertNotIn('"worker_open_targets"', source)
        self.assertNotIn('"final_origin_process_count"', source)
        self.assertIn('"evidence_scope": "validation_only_non_origin"', source)

    def test_validation_workflow_summary_contains_only_observed_scope(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            evidence_dir = root / "evidence"
            evidence_dir.mkdir()
            with mock.patch.object(workflow_smoke, "TMP_ROOT", root / "tasks"):
                summary = workflow_smoke.run_validation_workflow_smoke(evidence_dir=evidence_dir)

        self.assertEqual("validation_only_non_origin", summary["evidence_scope"])
        self.assertTrue(summary["sources_unchanged"])
        self.assertTrue(summary["preexisting_sentinel_unchanged"])
        self.assertGreater(summary["evidence_text_bytes_checked"], 0)
        self.assertEqual([], list(summary["validation_failures"]))
        self.assertEqual("completion", summary["app_summary"]["final_stage"])
        for unobserved in (
            "returncode",
            "shutdown",
            "stdout",
            "stderr",
            "worker_open_targets",
            "final_origin_process_count",
        ):
            self.assertNotIn(unobserved, summary)

    def test_task_root_is_unique_under_c_tmp_without_workspace_dependency(self):
        with tempfile.TemporaryDirectory() as temp:
            fake_root = pathlib.Path(temp)
            with mock.patch.object(workflow_smoke, "TMP_ROOT", fake_root):
                root = workflow_smoke._new_task_root("20260705_121000")

            self.assertTrue(root.is_dir())
            self.assertEqual(fake_root, root.parent)
            self.assertIn("SpectrumOrganizerTask17Workflow-20260705_121000", root.name)

    def test_validation_gate_rejects_runtime_workspace_references(self):
        with tempfile.TemporaryDirectory() as temp:
            source = pathlib.Path(temp) / "raw.opju"
            source.write_bytes(b"raw")
            failures = workflow_smoke._validation_failures(
                runtime_text=str(ROOT),
                preexisting_sentinel_unchanged=True,
                original_source_paths=(source,),
            )

        self.assertIn("runtime references workspace path", failures)

    def test_validation_gate_normalizes_workspace_path_forms_and_boundaries(self):
        with tempfile.TemporaryDirectory() as temp:
            source = pathlib.Path(temp) / "raw.opju"
            source.write_bytes(b"raw")
            workspace_forms = (
                json.dumps({"path": str(ROOT)}),
                ROOT.as_posix(),
                str(ROOT / "nested" / "report.txt"),
            )
            for runtime_text in workspace_forms:
                with self.subTest(runtime_text=runtime_text):
                    failures = workflow_smoke._validation_failures(
                        runtime_text=runtime_text,
                        preexisting_sentinel_unchanged=True,
                        original_source_paths=(source,),
                    )
                    self.assertIn("runtime references workspace path", failures)

            sibling_failures = workflow_smoke._validation_failures(
                runtime_text=f"{ROOT}-backup",
                preexisting_sentinel_unchanged=True,
                original_source_paths=(source,),
            )
            prefixed_failures = workflow_smoke._validation_failures(
                runtime_text=f"prefix{ROOT}",
                preexisting_sentinel_unchanged=True,
                original_source_paths=(source,),
            )

        self.assertNotIn("runtime references workspace path", sibling_failures)
        self.assertNotIn("runtime references workspace path", prefixed_failures)

    def test_validation_gate_rejects_changed_preexisting_sentinel(self):
        with tempfile.TemporaryDirectory() as temp:
            source = pathlib.Path(temp) / "raw.opju"
            source.write_bytes(b"raw")
            failures = workflow_smoke._validation_failures(
                runtime_text="clean",
                preexisting_sentinel_unchanged=False,
                original_source_paths=(source,),
            )

        self.assertIn("preexisting validation sentinel changed", failures)

    def test_generated_report_text_is_included_in_workspace_reference_scan(self):
        with tempfile.TemporaryDirectory() as temp:
            report = pathlib.Path(temp) / "Run_Report.txt"
            report.write_text(f"unexpected path: {ROOT}", encoding="utf-8")
            evidence_text = workflow_smoke._collect_text((report,))

        self.assertIn(str(ROOT), evidence_text)

    def test_required_evidence_must_exist_and_remain_inside_task_root(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            task_root = root / "task"
            task_root.mkdir()
            missing = task_root / "missing.json"
            with self.assertRaises(workflow_smoke.EvidenceCollectionError):
                workflow_smoke._collect_text(
                    (missing,),
                    required_files=(missing,),
                    allowed_root=task_root,
                )

            outside = root / "outside.txt"
            outside.write_text("outside", encoding="utf-8")
            with self.assertRaises(workflow_smoke.EvidenceCollectionError):
                workflow_smoke._collect_text(
                    (outside,),
                    required_files=(outside,),
                    allowed_root=task_root,
                )

            with self.assertRaises(workflow_smoke.EvidenceCollectionError):
                workflow_smoke._collect_text((outside,), allowed_root=task_root)

            oversized = task_root / "oversized.txt"
            oversized.write_bytes(b"x" * 1_000_001)
            with self.assertRaises(workflow_smoke.EvidenceCollectionError):
                workflow_smoke._collect_text(
                    (oversized,),
                    required_files=(oversized,),
                    allowed_root=task_root,
                )

    def test_required_evidence_read_failure_is_not_silently_ignored(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            required = root / "required.txt"
            required.write_text("evidence", encoding="utf-8")
            original_read_bytes = pathlib.Path.read_bytes

            def fail_required_read(path, *args, **kwargs):
                if path == required:
                    raise OSError("cannot read required evidence")
                return original_read_bytes(path, *args, **kwargs)

            with (
                mock.patch.object(pathlib.Path, "read_bytes", fail_required_read),
                self.assertRaises(workflow_smoke.EvidenceCollectionError),
            ):
                workflow_smoke._collect_text(
                    (required,),
                    required_files=(required,),
                    allowed_root=root,
                )

    def test_linked_directory_target_outside_task_root_is_rejected_before_type_filtering(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            task_root = root / "task"
            linked_directory = task_root / "linked-runtime"
            outside_directory = root / "outside-runtime"
            linked_directory.mkdir(parents=True)
            outside_directory.mkdir()
            (outside_directory / "hidden.log").write_text("outside evidence", encoding="utf-8")
            original_resolve = pathlib.Path.resolve

            def resolve_link_target(path, *args, **kwargs):
                if path == linked_directory:
                    return outside_directory
                return original_resolve(path, *args, **kwargs)

            with (
                mock.patch.object(
                    pathlib.Path,
                    "resolve",
                    autospec=True,
                    side_effect=resolve_link_target,
                ),
                self.assertRaisesRegex(
                    workflow_smoke.EvidenceCollectionError,
                    "outside the task root",
                ),
            ):
                workflow_smoke._collect_text((task_root,), allowed_root=task_root)

    def test_optional_runtime_evidence_failures_are_not_silently_ignored(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            oversized = root / "oversized.log"
            oversized.write_bytes(b"x" * (workflow_smoke.MAX_EVIDENCE_TEXT_BYTES + 1))
            with self.assertRaises(workflow_smoke.EvidenceCollectionError):
                workflow_smoke._collect_text((oversized,))

            unreadable = root / "unreadable.log"
            unreadable.write_text("evidence", encoding="utf-8")
            original_read_bytes = pathlib.Path.read_bytes

            def fail_optional_read(path, *args, **kwargs):
                if path == unreadable:
                    raise PermissionError("cannot read optional evidence")
                return original_read_bytes(path, *args, **kwargs)

            with (
                mock.patch.object(pathlib.Path, "read_bytes", fail_optional_read),
                self.assertRaises(workflow_smoke.EvidenceCollectionError),
            ):
                workflow_smoke._collect_text((unreadable,))

    def test_optional_runtime_evidence_metadata_failure_is_not_treated_as_absent(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = pathlib.Path(temp) / "runtime.json"
            runtime.write_text("evidence", encoding="utf-8")
            original_stat = pathlib.Path.stat

            def fail_optional_stat(path, *args, **kwargs):
                if path == runtime:
                    raise PermissionError("cannot inspect optional evidence")
                return original_stat(path, *args, **kwargs)

            with (
                mock.patch.object(pathlib.Path, "stat", fail_optional_stat),
                self.assertRaisesRegex(
                    workflow_smoke.EvidenceCollectionError,
                    "Runtime evidence cannot be inspected",
                ),
            ):
                workflow_smoke._collect_text((runtime,))

    def test_existing_optional_text_evidence_cannot_be_skipped_by_false_predicates(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = pathlib.Path(temp) / "runtime.json"
            runtime.write_text("evidence", encoding="utf-8")
            original_is_file = pathlib.Path.is_file
            original_is_dir = pathlib.Path.is_dir

            def false_for_runtime_file(path):
                if path == runtime:
                    return False
                return original_is_file(path)

            def false_for_runtime_dir(path):
                if path == runtime:
                    return False
                return original_is_dir(path)

            with (
                mock.patch.object(pathlib.Path, "is_file", false_for_runtime_file),
                mock.patch.object(pathlib.Path, "is_dir", false_for_runtime_dir),
            ):
                evidence = workflow_smoke._collect_text((runtime,))

            self.assertIn("evidence", evidence)

    def test_known_binary_runtime_artifacts_are_excluded_from_text_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            database = root / "sample_library.sqlite3"
            database.write_bytes(b"\x00\xffbinary")

            self.assertEqual("", workflow_smoke._collect_text((root,)))

    def test_validation_summary_reports_utf8_evidence_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            evidence_dir = root / "evidence"
            evidence_dir.mkdir()
            with (
                mock.patch.object(workflow_smoke, "TMP_ROOT", root / "tasks"),
                mock.patch.object(workflow_smoke, "_collect_text_with_byte_count", return_value=("测", 3)),
                mock.patch.object(workflow_smoke, "_validation_failures", return_value=()),
            ):
                summary = workflow_smoke.run_validation_workflow_smoke(evidence_dir=evidence_dir)

        self.assertEqual(3, summary["evidence_text_bytes_checked"])

    def test_text_evidence_byte_count_uses_raw_file_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            (root / "a.txt").write_bytes(b"a\r\nb")
            (root / "b.log").write_bytes("测".encode("utf-8"))

            text, byte_count = workflow_smoke._collect_text_with_byte_count((root,))

        self.assertIn("a\r\nb", text)
        self.assertIn("测", text)
        self.assertEqual(7, byte_count)

    def test_validation_workflow_scans_optional_output_text(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            evidence_dir = root / "evidence"
            evidence_dir.mkdir()

            def fake_workflow(source_paths, output_parent, *, local_appdata, timestamp):
                logs = pathlib.Path(local_appdata) / "Spectrum Organizer" / "logs"
                logs.mkdir(parents=True)
                summary_file = logs / "summary.json"
                summary_file.write_text("{}", encoding="utf-8")
                run_dir = output_parent / "run"
                run_dir.mkdir(parents=True)
                project_path = run_dir / "project.opju"
                project_path.write_text("clean", encoding="utf-8")
                report_path = run_dir / "Run_Report.txt"
                report_path.write_text("clean", encoding="utf-8")
                (output_parent / "extra.log").write_text(str(ROOT), encoding="utf-8")
                return PackagedWorkflowSummary(
                    selected_source_paths=tuple(str(path) for path in source_paths),
                    duplicate_source_paths=(),
                    output_parent=str(output_parent),
                    settings_file=str(pathlib.Path(local_appdata) / "settings.json"),
                    summary_file=str(summary_file),
                    final_run_dir=str(run_dir),
                    project_path=str(project_path),
                    report_path=str(report_path),
                    final_stage="completion",
                    copied_spectrum_ids=(),
                )

            with (
                mock.patch.object(workflow_smoke, "TMP_ROOT", root / "tasks"),
                mock.patch.object(workflow_smoke, "run_packaged_non_origin_workflow", side_effect=fake_workflow),
                self.assertRaises(SystemExit),
            ):
                workflow_smoke.run_validation_workflow_smoke(evidence_dir=evidence_dir)

    def test_validation_workflow_scans_generated_text_project(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            evidence_dir = root / "evidence"
            evidence_dir.mkdir()

            def fake_workflow(source_paths, output_parent, *, local_appdata, timestamp):
                logs = pathlib.Path(local_appdata) / "Spectrum Organizer" / "logs"
                logs.mkdir(parents=True)
                summary_file = logs / "summary.json"
                summary_file.write_text("{}", encoding="utf-8")
                run_dir = output_parent / "run"
                run_dir.mkdir(parents=True)
                project_path = run_dir / "project.opju"
                project_path.write_text(str(ROOT), encoding="utf-8")
                report_path = run_dir / "Run_Report.txt"
                report_path.write_text("clean", encoding="utf-8")
                return PackagedWorkflowSummary(
                    selected_source_paths=tuple(str(path) for path in source_paths),
                    duplicate_source_paths=(),
                    output_parent=str(output_parent),
                    settings_file=str(pathlib.Path(local_appdata) / "settings.json"),
                    summary_file=str(summary_file),
                    final_run_dir=str(run_dir),
                    project_path=str(project_path),
                    report_path=str(report_path),
                    final_stage="completion",
                    copied_spectrum_ids=(),
                )

            with (
                mock.patch.object(workflow_smoke, "TMP_ROOT", root / "tasks"),
                mock.patch.object(workflow_smoke, "run_packaged_non_origin_workflow", side_effect=fake_workflow),
                self.assertRaises(SystemExit),
            ):
                workflow_smoke.run_validation_workflow_smoke(evidence_dir=evidence_dir)

    def test_failed_rerun_invalidates_previous_success_summary(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            evidence_dir = root / "evidence"
            evidence_dir.mkdir()
            summary_path = evidence_dir / "validation-workflow-summary.json"
            summary_path.write_text('{"validation_failures": []}', encoding="utf-8")

            with (
                mock.patch.object(workflow_smoke, "TMP_ROOT", root / "tasks"),
                mock.patch.object(
                    workflow_smoke,
                    "run_packaged_non_origin_workflow",
                    side_effect=RuntimeError("workflow failed"),
                ),
                self.assertRaisesRegex(RuntimeError, "workflow failed"),
            ):
                workflow_smoke.run_validation_workflow_smoke(evidence_dir=evidence_dir)

            self.assertFalse(summary_path.exists())

    def test_failed_previous_summary_invalidation_preserves_the_lock(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            evidence_dir = root / "evidence"
            evidence_dir.mkdir()
            summary_path = evidence_dir / "validation-workflow-summary.json"
            lock_path = evidence_dir / ".validation-workflow.lock"
            summary_path.write_text('{"validation_failures": []}', encoding="utf-8")
            original_unlink = pathlib.Path.unlink

            original_replace = workflow_smoke.os.replace

            def fail_public_summary_unlink(path, *args, **kwargs):
                if path == summary_path:
                    raise OSError("summary invalidation failed")
                return original_unlink(path, *args, **kwargs)

            def fail_public_summary_move(source, destination):
                if pathlib.Path(source) == summary_path:
                    raise OSError("summary invalidation move failed")
                return original_replace(source, destination)

            with (
                mock.patch.object(workflow_smoke, "TMP_ROOT", root / "tasks"),
                mock.patch.object(workflow_smoke.os, "replace", side_effect=fail_public_summary_move),
                mock.patch.object(pathlib.Path, "unlink", fail_public_summary_unlink),
                self.assertRaisesRegex(
                    workflow_smoke.EvidenceCollectionError,
                    "invalidate previous validation workflow success summary",
                ),
            ):
                workflow_smoke.run_validation_workflow_smoke(evidence_dir=evidence_dir)

            self.assertTrue(summary_path.exists())
            self.assertTrue(lock_path.exists())

    def test_overlapping_run_is_rejected_before_shared_summary_mutation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            evidence_dir = root / "evidence"
            evidence_dir.mkdir()
            first_entered = threading.Event()
            release_first = threading.Event()
            calls = 0

            def fake_workflow(source_paths, output_parent, *, local_appdata, timestamp):
                nonlocal calls
                calls += 1
                if calls != 1:
                    raise RuntimeError("overlapping workflow entered")
                logs = pathlib.Path(local_appdata) / "Spectrum Organizer" / "logs"
                logs.mkdir(parents=True)
                run_dir = output_parent / "run"
                run_dir.mkdir(parents=True)
                summary_file = logs / "summary.json"
                project_path = run_dir / "project.opju"
                report_path = run_dir / "Run_Report.txt"
                summary = PackagedWorkflowSummary(
                    selected_source_paths=tuple(str(path) for path in source_paths),
                    duplicate_source_paths=(),
                    output_parent=str(output_parent),
                    settings_file=str(pathlib.Path(local_appdata) / "settings.json"),
                    summary_file=str(summary_file),
                    final_run_dir=str(run_dir),
                    project_path=str(project_path),
                    report_path=str(report_path),
                    final_stage="completion",
                    copied_spectrum_ids=(),
                )
                summary_file.write_text(json.dumps(summary.__dict__), encoding="utf-8")
                project_path.write_text("clean", encoding="utf-8")
                report_path.write_text("clean", encoding="utf-8")
                first_entered.set()
                self.assertTrue(release_first.wait(timeout=10))
                return summary

            with (
                mock.patch.object(workflow_smoke, "TMP_ROOT", root / "tasks"),
                mock.patch.object(workflow_smoke, "run_packaged_non_origin_workflow", side_effect=fake_workflow),
                ThreadPoolExecutor(max_workers=2) as pool,
            ):
                first = pool.submit(workflow_smoke.run_validation_workflow_smoke, evidence_dir=evidence_dir)
                self.assertTrue(first_entered.wait(timeout=10))
                try:
                    with self.assertRaisesRegex(workflow_smoke.EvidenceCollectionError, "already running"):
                        workflow_smoke.run_validation_workflow_smoke(evidence_dir=evidence_dir)
                finally:
                    release_first.set()
                first_summary = first.result(timeout=10)

            published = json.loads(
                (evidence_dir / "validation-workflow-summary.json").read_text(encoding="utf-8")
            )
            normalized_first = json.loads(json.dumps(first_summary, ensure_ascii=False))
            self.assertEqual(normalized_first, published)
            self.assertEqual(1, calls)
            self.assertFalse((evidence_dir / ".validation-workflow.lock").exists())

    def test_success_summary_publication_remains_inside_exclusive_run(self):
        with tempfile.TemporaryDirectory() as temp:
            evidence_dir = pathlib.Path(temp) / "evidence"
            evidence_dir.mkdir()
            first_publication_entered = threading.Event()
            release_first_publication = threading.Event()
            calls = 0
            real_write_json_atomic = workflow_smoke._write_json_atomic

            def fake_locked_workflow(_evidence_dir):
                nonlocal calls
                calls += 1
                return {"run": calls}

            def blocking_write(path, payload):
                if payload == {"run": 1}:
                    first_publication_entered.set()
                    self.assertTrue(release_first_publication.wait(timeout=10))
                real_write_json_atomic(path, payload)

            with (
                mock.patch.object(
                    workflow_smoke,
                    "_run_validation_workflow_smoke_locked",
                    side_effect=fake_locked_workflow,
                ),
                mock.patch.object(
                    workflow_smoke,
                    "_write_json_atomic",
                    side_effect=blocking_write,
                ),
                ThreadPoolExecutor(max_workers=2) as pool,
            ):
                first = pool.submit(
                    workflow_smoke.run_validation_workflow_smoke,
                    evidence_dir=evidence_dir,
                )
                self.assertTrue(first_publication_entered.wait(timeout=10))
                try:
                    self.assertTrue((evidence_dir / ".validation-workflow.lock").exists())
                    with self.assertRaisesRegex(
                        workflow_smoke.EvidenceCollectionError,
                        "already running",
                    ):
                        workflow_smoke.run_validation_workflow_smoke(
                            evidence_dir=evidence_dir,
                        )
                finally:
                    release_first_publication.set()
                self.assertEqual({"run": 1}, first.result(timeout=10))

            published = json.loads(
                (evidence_dir / "validation-workflow-summary.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual({"run": 1}, published)
            self.assertEqual(1, calls)
            self.assertFalse((evidence_dir / ".validation-workflow.lock").exists())

    def test_corrupt_persisted_app_summary_is_rejected(self):
        self._assert_persisted_app_summary_rejected("not-json", "not valid JSON")

    def test_mismatched_persisted_app_summary_is_rejected(self):
        self._assert_persisted_app_summary_rejected("{}", "does not match")

    def _assert_persisted_app_summary_rejected(self, persisted_text, message):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            evidence_dir = root / "evidence"
            evidence_dir.mkdir()

            def fake_workflow(source_paths, output_parent, *, local_appdata, timestamp):
                logs = pathlib.Path(local_appdata) / "Spectrum Organizer" / "logs"
                logs.mkdir(parents=True)
                run_dir = output_parent / "run"
                run_dir.mkdir(parents=True)
                summary_file = logs / "summary.json"
                project_path = run_dir / "project.opju"
                report_path = run_dir / "Run_Report.txt"
                summary_file.write_text(persisted_text, encoding="utf-8")
                project_path.write_text("clean", encoding="utf-8")
                report_path.write_text("clean", encoding="utf-8")
                return PackagedWorkflowSummary(
                    selected_source_paths=tuple(str(path) for path in source_paths),
                    duplicate_source_paths=(),
                    output_parent=str(output_parent),
                    settings_file=str(pathlib.Path(local_appdata) / "settings.json"),
                    summary_file=str(summary_file),
                    final_run_dir=str(run_dir),
                    project_path=str(project_path),
                    report_path=str(report_path),
                    final_stage="completion",
                    copied_spectrum_ids=(),
                )

            with (
                mock.patch.object(workflow_smoke, "TMP_ROOT", root / "tasks"),
                mock.patch.object(workflow_smoke, "run_packaged_non_origin_workflow", side_effect=fake_workflow),
                self.assertRaisesRegex(workflow_smoke.EvidenceCollectionError, message),
            ):
                workflow_smoke.run_validation_workflow_smoke(evidence_dir=evidence_dir)

    def test_cleanup_failure_does_not_publish_success_summary(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            evidence_dir = root / "evidence"
            evidence_dir.mkdir()
            task_parent = root / "tasks"
            original_rmtree = workflow_smoke.shutil.rmtree

            def fail_task_cleanup(path, *args, **kwargs):
                if pathlib.Path(path).parent == task_parent:
                    raise OSError("cleanup failed")
                return original_rmtree(path, *args, **kwargs)

            with (
                mock.patch.object(workflow_smoke, "TMP_ROOT", task_parent),
                mock.patch.object(workflow_smoke.shutil, "rmtree", side_effect=fail_task_cleanup),
                self.assertRaisesRegex(OSError, "cleanup failed"),
            ):
                workflow_smoke.run_validation_workflow_smoke(evidence_dir=evidence_dir)

            self.assertFalse((evidence_dir / "validation-workflow-summary.json").exists())

    def test_lock_release_failure_prevents_success_summary_publication(self):
        with tempfile.TemporaryDirectory() as temp:
            evidence_dir = pathlib.Path(temp) / "evidence"
            summary_path = evidence_dir / "validation-workflow-summary.json"

            def fake_locked_run(_evidence_dir):
                return {"validation_failures": []}

            with (
                mock.patch.object(
                    workflow_smoke,
                    "_run_validation_workflow_smoke_locked",
                    side_effect=fake_locked_run,
                ),
                mock.patch.object(
                    workflow_smoke,
                    "release_owned_directory_lock",
                    side_effect=workflow_smoke.OwnedDirectoryLockError(
                        "Could not release validation workflow lock"
                    ),
                ),
                self.assertRaisesRegex(
                    workflow_smoke.EvidenceCollectionError,
                    "Could not release validation workflow lock",
                ),
            ):
                workflow_smoke.run_validation_workflow_smoke(evidence_dir=evidence_dir)

            self.assertFalse(summary_path.exists())

    def test_lock_release_and_invalid_tombstone_cleanup_never_leave_success(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            evidence_dir = root / "evidence"
            evidence_dir.mkdir()
            summary_path = evidence_dir / "validation-workflow-summary.json"
            original_unlink = pathlib.Path.unlink

            def fail_invalid_tombstone_unlink(path, *args, **kwargs):
                if path.name.startswith(f".{summary_path.name}.") and path.name.endswith(".invalid"):
                    raise OSError("invalid tombstone cleanup failed")
                return original_unlink(path, *args, **kwargs)

            with (
                mock.patch.object(workflow_smoke, "TMP_ROOT", root / "tasks"),
                mock.patch.object(
                    workflow_smoke,
                    "release_owned_directory_lock",
                    side_effect=workflow_smoke.OwnedDirectoryLockError(
                        "Could not release validation workflow lock"
                    ),
                ),
                mock.patch.object(pathlib.Path, "unlink", fail_invalid_tombstone_unlink),
                self.assertRaisesRegex(
                    workflow_smoke.EvidenceCollectionError,
                    "Could not release validation workflow lock",
                ),
            ):
                workflow_smoke.run_validation_workflow_smoke(evidence_dir=evidence_dir)

            self.assertFalse(summary_path.exists())

    def test_missing_lock_during_success_invalidates_the_run(self):
        with tempfile.TemporaryDirectory() as temp:
            evidence_dir = pathlib.Path(temp) / "evidence"
            lock_path = evidence_dir / ".validation-workflow.lock"

            def remove_lock_and_return_summary(_evidence_dir):
                workflow_smoke.shutil.rmtree(lock_path)
                return {"validation_failures": []}

            with (
                mock.patch.object(
                    workflow_smoke,
                    "_run_validation_workflow_smoke_locked",
                    side_effect=remove_lock_and_return_summary,
                ),
                self.assertRaisesRegex(
                    workflow_smoke.EvidenceCollectionError,
                    "Validation workflow lock disappeared",
                ),
            ):
                workflow_smoke.run_validation_workflow_smoke(evidence_dir=evidence_dir)

            self.assertFalse((evidence_dir / "validation-workflow-summary.json").exists())

    def test_replaced_lock_is_not_released_by_the_previous_owner(self):
        with tempfile.TemporaryDirectory() as temp:
            evidence_dir = pathlib.Path(temp) / "evidence"
            lock_path = evidence_dir / ".validation-workflow.lock"

            def replace_lock_and_return_summary(_evidence_dir):
                workflow_smoke.shutil.rmtree(lock_path)
                lock_path.mkdir()
                return {"validation_failures": []}

            with (
                mock.patch.object(
                    workflow_smoke,
                    "_run_validation_workflow_smoke_locked",
                    side_effect=replace_lock_and_return_summary,
                ),
                self.assertRaisesRegex(
                    workflow_smoke.EvidenceCollectionError,
                    "lock ownership changed",
                ),
            ):
                workflow_smoke.run_validation_workflow_smoke(evidence_dir=evidence_dir)

            self.assertTrue(lock_path.is_dir())
            self.assertFalse((evidence_dir / "validation-workflow-summary.json").exists())

    def test_lock_replaced_after_owner_verification_is_not_deleted_by_previous_owner(self):
        with tempfile.TemporaryDirectory() as temp:
            evidence_dir = pathlib.Path(temp) / "evidence"
            lock_path = evidence_dir / ".validation-workflow.lock"
            foreign_token = "replacement-owner"
            original_ownership_check = workflow_smoke._lock_ownership_error

            def replace_after_verification(owner_path, expected_token):
                ownership_error = original_ownership_check(owner_path, expected_token)
                if ownership_error is None:
                    if lock_path.exists():
                        workflow_smoke.shutil.rmtree(lock_path)
                    lock_path.mkdir()
                    (lock_path / workflow_smoke.LOCK_OWNER_FILENAME).write_text(
                        foreign_token,
                        encoding="ascii",
                    )
                return ownership_error

            with (
                mock.patch.object(
                    workflow_smoke,
                    "_run_validation_workflow_smoke_locked",
                    return_value={"validation_failures": []},
                ),
                mock.patch.object(
                    workflow_smoke,
                    "_lock_ownership_error",
                    side_effect=replace_after_verification,
                ),
            ):
                workflow_smoke.run_validation_workflow_smoke(evidence_dir=evidence_dir)

            self.assertEqual(
                foreign_token,
                (lock_path / workflow_smoke.LOCK_OWNER_FILENAME).read_text(encoding="ascii"),
            )
            self.assertTrue((evidence_dir / "validation-workflow-summary.json").is_file())

    def test_failed_summary_move_falls_back_to_removing_public_success(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            evidence_dir = root / "evidence"
            summary_path = evidence_dir / "validation-workflow-summary.json"
            original_replace = workflow_smoke.os.replace

            def fail_summary_move(source, destination):
                if pathlib.Path(source) == summary_path:
                    raise OSError("summary move failed")
                return original_replace(source, destination)

            with (
                mock.patch.object(workflow_smoke, "TMP_ROOT", root / "tasks"),
                mock.patch.object(workflow_smoke.os, "replace", side_effect=fail_summary_move),
                mock.patch.object(
                    workflow_smoke,
                    "release_owned_directory_lock",
                    side_effect=workflow_smoke.OwnedDirectoryLockError(
                        "Could not release validation workflow lock"
                    ),
                ),
                self.assertRaises(workflow_smoke.EvidenceCollectionError),
            ):
                workflow_smoke.run_validation_workflow_smoke(evidence_dir=evidence_dir)

            self.assertFalse(summary_path.exists())

    def test_validation_workflow_cleans_owned_task_root_after_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            evidence_dir = root / "evidence"
            evidence_dir.mkdir()
            task_parent = root / "tasks"
            with (
                mock.patch.object(workflow_smoke, "TMP_ROOT", task_parent),
                mock.patch.object(
                    workflow_smoke,
                    "run_packaged_non_origin_workflow",
                    side_effect=RuntimeError("workflow failed"),
                ),
                self.assertRaisesRegex(RuntimeError, "workflow failed"),
            ):
                workflow_smoke.run_validation_workflow_smoke(evidence_dir=evidence_dir)

            self.assertEqual([], list(task_parent.iterdir()))

    def test_validation_workflow_rejects_workspace_reference_in_generated_report(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            evidence_dir = root / "evidence"
            evidence_dir.mkdir()

            def fake_workflow(source_paths, output_parent, *, local_appdata, timestamp):
                logs = pathlib.Path(local_appdata) / "Spectrum Organizer" / "logs"
                logs.mkdir(parents=True)
                summary_file = logs / "summary.json"
                summary_file.write_text("{}", encoding="utf-8")
                run_dir = output_parent / "run"
                run_dir.mkdir(parents=True)
                project_path = run_dir / "project.opju"
                project_path.write_text("clean", encoding="utf-8")
                report_path = run_dir / "Run_Report.txt"
                report_path.write_text(f"unexpected path: {ROOT}", encoding="utf-8")
                return PackagedWorkflowSummary(
                    selected_source_paths=tuple(str(path) for path in source_paths),
                    duplicate_source_paths=(),
                    output_parent=str(output_parent),
                    settings_file=str(pathlib.Path(local_appdata) / "settings.json"),
                    summary_file=str(summary_file),
                    final_run_dir=str(run_dir),
                    project_path=str(project_path),
                    report_path=str(report_path),
                    final_stage="completion",
                    copied_spectrum_ids=(),
                )

            with (
                mock.patch.object(workflow_smoke, "TMP_ROOT", root / "tasks"),
                mock.patch.object(workflow_smoke, "run_packaged_non_origin_workflow", side_effect=fake_workflow),
                self.assertRaises(SystemExit),
            ):
                workflow_smoke.run_validation_workflow_smoke(evidence_dir=evidence_dir)


if __name__ == "__main__":
    unittest.main()
