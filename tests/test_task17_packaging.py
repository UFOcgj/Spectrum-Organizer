import importlib.util
import io
import json
import os
import pathlib
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def _load_packaging_module(name):
    path = ROOT / "packaging" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"task17_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Task17PackagingTests(unittest.TestCase):
    def test_runtime_audit_is_disabled_by_default_and_atomic_when_enabled(self):
        from spectrum_organizer.runtime_audit import (
            RUNTIME_AUDIT_DIR_ENV,
            record_runtime_audit_event,
        )

        with tempfile.TemporaryDirectory() as directory:
            audit_dir = pathlib.Path(directory)
            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertIsNone(
                    record_runtime_audit_event("probe", {"value": 1})
                )
            self.assertEqual([], list(audit_dir.iterdir()))

            with mock.patch.dict(
                os.environ,
                {RUNTIME_AUDIT_DIR_ENV: str(audit_dir)},
                clear=True,
            ):
                event_path = record_runtime_audit_event(
                    "probe",
                    {"value": 1},
                )
                second_event_path = record_runtime_audit_event(
                    "second_probe",
                    {"value": 2},
                )

            self.assertEqual(audit_dir.resolve(strict=True), event_path.parent)
            self.assertEqual([], list(audit_dir.glob("*.pending")))
            event = json.loads(event_path.read_text(encoding="utf-8"))
            self.assertEqual(1, event["schema_version"])
            self.assertEqual("probe", event["event_type"])
            self.assertEqual({"value": 1}, event["payload"])
            self.assertRegex(event["process_instance_id"], r"^[0-9a-f]{32}$")
            second_event = json.loads(
                second_event_path.read_text(encoding="utf-8")
            )
            self.assertEqual(
                event["process_instance_id"],
                second_event["process_instance_id"],
            )

    def test_runtime_audit_never_publishes_replaced_pending_file(self):
        from spectrum_organizer.runtime_audit import (
            RUNTIME_AUDIT_DIR_ENV,
            record_runtime_audit_event,
        )

        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            os.environ,
            {RUNTIME_AUDIT_DIR_ENV: directory},
        ):
            audit_dir = pathlib.Path(directory)
            original_link = os.link
            injected = False

            def replace_pending_with_foreign(source, destination):
                nonlocal injected
                injected = True
                source = pathlib.Path(source)
                parked = source.with_name("parked-owned.pending")
                source.rename(parked)
                source.write_text(
                    '{"event_type":"FOREIGN"}',
                    encoding="utf-8",
                )
                return original_link(source, destination)

            with mock.patch(
                "spectrum_organizer.runtime_audit.os.link",
                side_effect=replace_pending_with_foreign,
            ):
                with self.assertRaises((RuntimeError, OSError)):
                    record_runtime_audit_event(
                        "owned-event",
                        {"value": 1},
                    )

            self.assertTrue(injected)
            self.assertEqual([], list(audit_dir.glob("*.json")))

    def test_runtime_audit_holds_configured_directory_identity(self):
        from contextlib import contextmanager
        from spectrum_organizer.runtime_audit import (
            RUNTIME_AUDIT_DIR_ENV,
            record_runtime_audit_event,
        )
        from spectrum_organizer import runtime_audit

        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            os.environ,
            {RUNTIME_AUDIT_DIR_ENV: str(pathlib.Path(directory) / "audit")},
        ):
            root = pathlib.Path(directory)
            audit_dir = root / "audit"
            audit_dir.mkdir()
            parked = root / "parked-audit"
            real_create = runtime_audit.create_exclusive_held_file
            injected = False

            @contextmanager
            def replace_directory_before_pending(path, **kwargs):
                nonlocal injected
                injected = True
                audit_dir.rename(parked)
                audit_dir.mkdir()
                with real_create(path, **kwargs) as held:
                    yield held

            with mock.patch.object(
                runtime_audit,
                "create_exclusive_held_file",
                replace_directory_before_pending,
            ):
                with self.assertRaises((RuntimeError, OSError)):
                    record_runtime_audit_event("owned-event", {"value": 1})

            self.assertTrue(injected)
            self.assertEqual([], list(audit_dir.glob("*.json")))
            self.assertEqual([], list(parked.glob("*.json")))

    def test_output_worker_records_validated_targets_only_after_success(self):
        from spectrum_organizer.origin.contracts import ProjectArtifactEvidence
        from spectrum_organizer.origin.output_process import output_process_main
        from spectrum_organizer.runtime_audit import RUNTIME_AUDIT_DIR_ENV
        from spectrum_organizer.safety.identity_paths import file_sha256, path_identity

        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            os.environ,
            {RUNTIME_AUDIT_DIR_ENV: directory},
        ):
            staging = pathlib.Path(directory) / "staging"
            staging.mkdir()
            output_path = staging / "output.opju"
            mutation_path = staging / "mutation.opju"
            output_path.touch()
            mutation_path.touch()
            payloads = {
                "output": {
                    "approved_snapshot_id": "approved-1",
                    "contract": {"root_path": "/", "folders": []},
                    "staging_project_path": str(output_path),
                    "staging_project_identity": list(path_identity(output_path)),
                    "run_staging_root": str(staging),
                    "run_staging_identity": list(path_identity(staging)),
                    "allowed_output_targets": [str(output_path)],
                    "attempt": 1,
                },
                "verifier": {
                    "approved_snapshot_id": "approved-1",
                    "contract": {"root_path": "/", "folders": []},
                    "staged_project_path": str(output_path),
                    "mutation_copy_path": str(mutation_path),
                    "mutation_copy_identity": list(path_identity(mutation_path)),
                    "run_staging_root": str(staging),
                    "run_staging_identity": list(path_identity(staging)),
                    "allowed_open_targets": [
                        str(output_path),
                        str(mutation_path),
                    ],
                    "protected_paths": [],
                    "attempt": 1,
                },
            }

            def output_runner(_command):
                output_path.write_bytes(b"output")
                return ProjectArtifactEvidence(
                    identity=path_identity(output_path),
                    sha256=file_sha256(output_path),
                    size=output_path.stat().st_size,
                )

            def verifier_runner(_command):
                mutation_path.write_bytes(b"mutation")
                return (
                    ProjectArtifactEvidence(
                        identity=path_identity(output_path),
                        sha256=file_sha256(output_path),
                        size=output_path.stat().st_size,
                    ),
                    path_identity(mutation_path),
                )

            for role in ("output", "verifier"):
                with self.subTest(role=role):
                    if role == "verifier":
                        artifact = output_runner(None)
                        payloads[role]["expected_project_artifact"] = {
                            "identity": list(artifact.identity),
                            "sha256": artifact.sha256,
                            "size": artifact.size,
                        }
                    self.assertEqual(
                        0,
                        output_process_main(
                            [role],
                            stdin=io.StringIO(json.dumps(payloads[role])),
                            stdout=io.StringIO(),
                            output_runner=output_runner,
                            verifier_runner=verifier_runner,
                        ),
                    )

            events = [
                json.loads(path.read_text(encoding="utf-8"))
                for path in pathlib.Path(directory).glob("*.json")
            ]

        targets = {
            event["payload"]["role"]: event["payload"]["open_targets"]
            for event in events
        }
        completed_attempts = {
            event["payload"]["role"]: event["payload"]["attempt"]
            for event in events
            if event["event_type"] == "origin_worker_targets"
        }
        self.assertEqual(
            [str(output_path)],
            targets["output"],
        )
        self.assertEqual(
            [str(output_path), str(mutation_path)],
            targets["verifier"],
        )
        self.assertEqual({"output": 1, "verifier": 1}, completed_attempts)

    def test_extraction_target_and_approved_counts_share_runtime_audit(self):
        from spectrum_organizer.core.output_model import OutputPlan
        from spectrum_organizer.origin.extract_worker import (
            _record_origin_open_target,
        )
        from spectrum_organizer.origin.output_process import (
            _record_approved_counts,
        )
        from spectrum_organizer.runtime_audit import RUNTIME_AUDIT_DIR_ENV

        counts = SimpleNamespace(
            recognizable_book_count=10,
            rejected_book_count=2,
            excluded_book_count=3,
            accepted_ordinary_spectrum_count=5,
            output_plan_spectrum_count=5,
            output_plan_column_count=12,
        )
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            os.environ,
            {RUNTIME_AUDIT_DIR_ENV: directory},
        ):
            root = pathlib.Path(directory)
            copy_path = root / "owned" / "copy.opju"
            copy_path.parent.mkdir()
            copy_path.write_bytes(b"copy")
            _record_origin_open_target(
                root / "origin-open.json",
                copy_path,
                run_id="run-1",
                marker_id="marker-1",
                source_id="S0001",
                reader_attempt=1,
            )
            _record_approved_counts(
                SimpleNamespace(
                    snapshot_id="approved-1",
                    count_reconciliation=counts,
                    selected_source_fingerprints_before=(),
                    source_fingerprints_before=(),
                    approved_sources=(),
                    rejections=(),
                    exclusions=(),
                    review_choices=(),
                    attributions=(),
                    output_plan=OutputPlan((), ()),
                    settings_snapshot={},
                    ignored_duplicate_input_paths=(),
                    source_input_issues=(),
                )
            )
            events = [
                json.loads(path.read_text(encoding="utf-8"))
                for path in root.glob("*.json")
                if path.name != "origin-open.json"
            ]

        by_type = {event["event_type"]: event["payload"] for event in events}
        self.assertEqual(
            [str(copy_path.resolve())],
            by_type["origin_worker_targets"]["open_targets"],
        )
        self.assertEqual("extraction", by_type["origin_worker_targets"]["role"])
        self.assertEqual(10, by_type["approved_count_reconciliation"]["recognizable_book_count"])
        self.assertEqual(12, by_type["approved_count_reconciliation"]["output_plan_column_count"])
        self.assertEqual(
            "approved-1",
            by_type["approved_report_ledger"]["approved_snapshot_id"],
        )
        self.assertIn(
            "输出 Folder/Book 映射",
            by_type["approved_report_ledger"]["sections"],
        )

    def test_packaged_startup_failures_use_unique_chinese_logs(self):
        entry = _load_packaging_module("pyinstaller_entry")

        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.dict(
                os.environ,
                {"LOCALAPPDATA": directory},
            ):
                try:
                    raise RuntimeError("first startup failure")
                except RuntimeError as exc:
                    first = entry._write_startup_failure_log(
                        exc,
                        timestamp="20260730_120000",
                    )
                try:
                    raise RuntimeError("second startup failure")
                except RuntimeError as exc:
                    second = entry._write_startup_failure_log(
                        exc,
                        timestamp="20260730_120000",
                    )

            self.assertIsNotNone(first)
            self.assertIsNotNone(second)
            self.assertNotEqual(first, second)
            self.assertEqual(
                "Packaged_Startup_Exception_20260730_120000.txt",
                first.name,
            )
            self.assertEqual(
                "Packaged_Startup_Exception_20260730_120000_001.txt",
                second.name,
            )
            first_text = first.read_text(encoding="utf-8")
            second_text = second.read_text(encoding="utf-8")
            self.assertIn("程序启动失败", first_text)
            self.assertIn("first startup failure", first_text)
            self.assertIn("Traceback", first_text)
            self.assertIn("second startup failure", second_text)
            self.assertNotIn("second startup failure", first_text)

    def test_packaged_startup_failure_shows_chinese_native_error(self):
        entry = _load_packaging_module("pyinstaller_entry")
        log_path = pathlib.Path(
            "C:/LocalAppData/Spectrum Organizer/logs/"
            "Packaged_Startup_Exception_20260730_120000.txt"
        )

        with mock.patch.object(
            entry,
            "_native_message_box",
        ) as message_box:
            entry._show_startup_failure_dialog(
                RuntimeError("broken import"),
                log_path,
            )

        title, message = message_box.call_args.args
        self.assertEqual("程序启动失败", title)
        self.assertIn("程序无法启动", message)
        self.assertIn("broken import", message)
        self.assertIn(str(log_path), message)

    def test_pyinstaller_entry_imports_product_main_inside_startup_guard(self):
        entry_path = ROOT / "packaging" / "pyinstaller_entry.py"
        text = entry_path.read_text(encoding="utf-8")
        main_guard_index = text.index('if __name__ == "__main__":')
        guarded = text[main_guard_index:]

        self.assertNotIn("from spectrum_organizer.__main__ import main", text[:main_guard_index])
        self.assertIn("from spectrum_organizer.__main__ import main", guarded)
        self.assertNotIn("validation", text)
        self.assertNotIn("originpro", text)
        self.assertNotIn('LOCALAPPDATA", "."', text)
        self.assertNotIn("LOCALAPPDATA', '.'", text)
        self.assertIn("if not local_appdata:", text)
        self.assertIn("raise SystemExit(main())", text)
        self.assertIn("raise SystemExit(1)", text)
    def test_pyinstaller_spec_is_one_folder_windowed_app(self):
        spec_path = ROOT / "packaging" / "spectrum_organizer.spec"
        text = spec_path.read_text(encoding="utf-8")

        self.assertIn("Analysis(", text)
        self.assertIn("PYZ(", text)
        self.assertIn("EXE(", text)
        self.assertIn("COLLECT(", text)
        self.assertIn("Path(SPEC)", text)
        self.assertNotIn("__file__", text)
        self.assertIn("pyinstaller_entry.py", text)
        self.assertIn("pathex=[str(ROOT / \"src\")]", text)
        self.assertIn("exclude_binaries=True", text)
        self.assertIn("console=False", text)
        self.assertIn("name=\"Spectrum Organizer\"", text)
        self.assertIn("originpro", text)
        self.assertIn("win32timezone", text)
        self.assertIn("spectrum_organizer.origin.extraction_process", text)
        self.assertIn("spectrum_organizer.origin.output_process", text)
        self.assertIn("spectrum-organizer.png", text)
        self.assertIn("spectrum-organizer.ico", text)
        self.assertIn("icon=", text)
        self.assertNotIn("onefile", text.lower())

    def test_distribution_readme_is_copied_next_to_executable(self):
        readme_path = ROOT / "packaging" / "README.txt"
        self.assertTrue(readme_path.is_file())

        readme = readme_path.read_text(encoding="utf-8")
        self.assertTrue(readme.startswith("Spectrum Organizer 使用说明"))
        self.assertIn("针对HORIBA FluoroMax-4 Spectrofluorometer开发", readme)
        self.assertIn("一、下载并打开程序", readme)
        self.assertIn("双击 Spectrum Organizer.exe", readme)
        self.assertIn("不要在 ZIP 压缩包里直接运行", readme)
        self.assertIn("支持 .opj 和 .opju", readme)
        self.assertIn("一次任务可以只选一个文件，也可以同时选择多个", readme)
        self.assertIn("按住 Ctrl 或 Shift", readme)
        self.assertIn("二、可以选择哪些原始文件", readme)
        self.assertIn("三、程序会生成什么", readme)
        source_safety = "程序使用任务校验副本读取和整理，不会修改原始项目，也不会覆盖已有输出"
        self.assertEqual(1, readme.count(source_safety))
        self.assertLess(
            readme.index("二、可以选择哪些原始文件"),
            readme.index(source_safety),
        )
        self.assertLess(
            readme.index(source_safety),
            readme.index("三、程序会生成什么"),
        )
        self.assertIn("四、程序怎样整理输出项目", readme)
        self.assertNotIn("五、为什么要保留运行报告", readme)
        self.assertIn("五、遇到问题怎么办", readme)
        self.assertNotIn("六、遇到问题怎么办", readme)
        self.assertLess(
            readme.index("三、程序会生成什么"),
            readme.index("运行报告记录本次输入"),
        )
        self.assertLess(
            readme.index("运行报告记录本次输入"),
            readme.index("四、程序怎样整理输出项目"),
        )
        self.assertNotIn("不是对原始文件的要求", readme)
        self.assertNotIn("几个容易误解的整理规则", readme)
        self.assertNotIn("你不需要提前按这些规则改名、移动数据或重做", readme)
        self.assertNotIn("七、当前分发状态", readme)
        self.assertNotIn("测试分发包", readme)
        self.assertNotIn("清洁机验证", readme)
        self.assertIn("不要只复制 EXE", readme)
        self.assertIn("当前版本不会输出特殊谱", readme)
        self.assertLess(
            readme.index("二、可以选择哪些原始文件"),
            readme.index("三、程序会生成什么"),
        )
        self.assertLess(
            readme.index("三、程序会生成什么"),
            readme.index("四、程序怎样整理输出项目"),
        )

        spec = (ROOT / "packaging" / "spectrum_organizer.spec").read_text(
            encoding="utf-8"
        )
        self.assertIn("copyfile(", spec)
        self.assertIn(
            'Path(DISTPATH) / "Spectrum Organizer" / "README.txt"',
            spec,
        )

    def test_frozen_output_roles_use_the_packaged_executable(self):
        from spectrum_organizer.origin import output_process

        with mock.patch.object(
            output_process.sys,
            "frozen",
            True,
            create=True,
        ), mock.patch.object(
            output_process.sys,
            "executable",
            r"C:\Package\Spectrum Organizer.exe",
        ):
            self.assertEqual(
                [r"C:\Package\Spectrum Organizer.exe", "--origin-output-worker"],
                output_process._origin_process_command("output"),
            )
            self.assertEqual(
                [r"C:\Package\Spectrum Organizer.exe", "--origin-verifier-worker"],
                output_process._origin_process_command("verifier"),
            )

    def test_product_main_dispatches_packaged_output_roles_before_startup(self):
        from spectrum_organizer.__main__ import main
        from spectrum_organizer.origin import output_process

        for flag, role in (
            ("--origin-output-worker", "output"),
            ("--origin-verifier-worker", "verifier"),
        ):
            with self.subTest(flag=flag), mock.patch.object(
                output_process,
                "output_process_main",
                return_value=17,
            ) as child:
                self.assertEqual(17, main(argv=[flag]))
                child.assert_called_once_with([role])

    def test_clean_environment_gate_detects_runtime_workspace_and_validation_paths(self):
        gate = _load_packaging_module("clean_environment_gate")

        failures = gate.evaluate_clean_environment(
            gate.CleanEnvironmentEvidence(
                runtime_text=f"loaded from {ROOT / 'src'} and validation.task16.py",
                worker_open_targets=(),
                created_paths=(),
                preexisting_user_paths=(),
                final_origin_process_count=0,
            ),
            workspace_root=ROOT,
            original_source_paths=(),
        )

        self.assertIn("runtime references workspace path", failures)
        self.assertIn("runtime references validation path", failures)

    def test_clean_environment_gate_does_not_match_workspace_prefix_siblings(self):
        gate = _load_packaging_module("clean_environment_gate")

        failures = gate.evaluate_clean_environment(
            gate.CleanEnvironmentEvidence(
                runtime_text=str(ROOT.parent / f"{ROOT.name}-backup" / "output.txt"),
                worker_open_targets=(),
                created_paths=(),
                preexisting_user_paths=(),
                final_origin_process_count=0,
            ),
            workspace_root=ROOT,
            original_source_paths=(),
        )

        self.assertNotIn("runtime references workspace path", failures)
    def test_clean_environment_gate_blocks_original_targets_overwrite_and_origin_survivors(self):
        gate = _load_packaging_module("clean_environment_gate")
        original = ROOT / "20250412_MFL-mTHF_RT.opj"

        failures = gate.evaluate_clean_environment(
            gate.CleanEnvironmentEvidence(
                runtime_text="ok",
                worker_open_targets=(str(original),),
                created_paths=(r"C:\Users\tester\Desktop\existing.txt",),
                preexisting_user_paths=(r"C:\Users\tester\Desktop\existing.txt",),
                final_origin_process_count=1,
            ),
            workspace_root=ROOT,
            original_source_paths=(original,),
        )

        self.assertIn("worker open target is an original source path", failures)
        self.assertIn("created path collides with preexisting user file", failures)
        self.assertIn("final Origin process count is not zero", failures)

    def test_clean_environment_gate_normalizes_equivalent_paths_and_validation_forms(self):
        gate = _load_packaging_module("clean_environment_gate")
        original = ROOT / "20250412_MFL-mTHF_RT.opj"
        existing = ROOT / "docs" / "already-there.txt"

        failures = gate.evaluate_clean_environment(
            gate.CleanEnvironmentEvidence(
                runtime_text="import validation.task15_controlled_smoke from C:/x/validation",
                worker_open_targets=(str(original.parent / "subdir" / ".." / original.name),),
                created_paths=(str(existing.parent / "subdir" / ".." / existing.name),),
                preexisting_user_paths=(str(existing),),
                final_origin_process_count=0,
                process_returncode_after_shutdown=0,
                shutdown="normal",
            ),
            workspace_root=ROOT,
            original_source_paths=(original,),
        )

        self.assertIn("runtime references validation path", failures)
        self.assertIn("worker open target is an original source path", failures)
        self.assertIn("created path collides with preexisting user file", failures)

    def test_clean_environment_gate_validation_detector_ignores_benign_words(self):
        gate = _load_packaging_module("clean_environment_gate")

        failures = gate.evaluate_clean_environment(
            gate.CleanEnvironmentEvidence(
                runtime_text="validation failed in a settings form; form.validation.enabled=false; validation.enabled=false",
                worker_open_targets=(),
                created_paths=(),
                preexisting_user_paths=(),
                final_origin_process_count=0,
            ),
            workspace_root=ROOT,
            original_source_paths=(),
        )

        self.assertNotIn("runtime references validation path", failures)

    def test_clean_environment_gate_validation_detector_keeps_path_and_module_forms(self):
        gate = _load_packaging_module("clean_environment_gate")

        for runtime_text in ("import validation.task17_package_smoke", "importlib.import_module('validation.task17_package_smoke')", "loaded C:/x/validation", "loaded C:/x/validation/task17.py", "loaded C:/x/validation)", "loaded validation)", "loaded validation,", "loaded validation;", "loaded validation:"):
            with self.subTest(runtime_text=runtime_text):
                failures = gate.evaluate_clean_environment(
                    gate.CleanEnvironmentEvidence(
                        runtime_text=runtime_text,
                        worker_open_targets=(),
                        created_paths=(),
                        preexisting_user_paths=(),
                        final_origin_process_count=0,
                    ),
                    workspace_root=ROOT,
                    original_source_paths=(),
                )

                self.assertIn("runtime references validation path", failures)

    def test_clean_environment_gate_blocks_bad_packaged_shutdown(self):
        gate = _load_packaging_module("clean_environment_gate")

        failures = gate.evaluate_clean_environment(
            gate.CleanEnvironmentEvidence(
                runtime_text="ok",
                worker_open_targets=(),
                created_paths=(),
                preexisting_user_paths=(),
                final_origin_process_count=0,
                process_returncode_after_shutdown=1,
                shutdown="exited_after_startup_dirs",
            ),
            workspace_root=ROOT,
            original_source_paths=(),
        )

        self.assertIn("packaged app did not shut down cleanly", failures)

    def test_clean_environment_gate_blocks_early_exit_before_stable_startup(self):
        gate = _load_packaging_module("clean_environment_gate")

        failures = gate.evaluate_clean_environment(
            gate.CleanEnvironmentEvidence(
                runtime_text="ok",
                worker_open_targets=(),
                created_paths=(),
                preexisting_user_paths=(),
                final_origin_process_count=0,
                process_returncode_after_shutdown=1,
                shutdown="exited_early",
            ),
            workspace_root=ROOT,
            original_source_paths=(),
        )

        self.assertIn("packaged app did not shut down cleanly", failures)

    def test_clean_environment_gate_blocks_forced_kill_timeout(self):
        gate = _load_packaging_module("clean_environment_gate")

        failures = gate.evaluate_clean_environment(
            gate.CleanEnvironmentEvidence(
                runtime_text="ok",
                worker_open_targets=(),
                created_paths=(),
                preexisting_user_paths=(),
                final_origin_process_count=0,
                process_returncode_after_shutdown=1,
                shutdown="kill_after_timeout",
            ),
            workspace_root=ROOT,
            original_source_paths=(),
        )

        self.assertIn("packaged app did not shut down cleanly", failures)

    def test_product_main_can_launch_window_after_primary_startup_with_injected_launcher(self):
        from spectrum_organizer.__main__ import main
        from spectrum_organizer.single_instance import FakeInstanceBackend

        calls = []

        with tempfile.TemporaryDirectory() as temp:
            result = main(
                instance_backend=FakeInstanceBackend(),
                local_appdata=temp,
                window_launcher=lambda startup_result: calls.append(startup_result.paths.root) or 0,
            )

        self.assertEqual(0, result)
        self.assertEqual(1, len(calls))
        self.assertTrue(str(calls[0]).endswith("Spectrum Organizer"))


if __name__ == "__main__":
    unittest.main()
