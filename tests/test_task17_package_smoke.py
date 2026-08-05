import pathlib
import json
import sys
import tempfile
import threading
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import validation.task17_package_smoke as package_smoke
from validation.task17_package_smoke import ROOT as SMOKE_ROOT
from validation.task17_package_smoke import _clean_gate_evidence_scope, _clean_gate_failures, _new_runtime_appdata


class Task17PackageSmokeScriptTests(unittest.TestCase):
    def test_packaged_worker_probe_uses_invalid_contract_without_source_paths(self):
        calls = []
        process_counts = iter((0, 0, 0, 0))

        def run(command, **kwargs):
            calls.append((command, kwargs))
            role = "output" if "--origin-output-worker" in command else "verifier"
            return mock.Mock(
                returncode=1,
                stdout=json.dumps(
                    {
                        "ok": False,
                        "classification": "non_retryable",
                        "error": f"{role} payload fields are invalid",
                        "error_type": "ValueError",
                        "error_notes": [],
                    }
                ),
                stderr="",
            )

        result = package_smoke._probe_packaged_worker_entrypoints(
            pathlib.Path(r"C:\Package\Spectrum Organizer.exe"),
            {"LOCALAPPDATA": r"C:\isolated"},
            process_runner=run,
            origin_process_count=lambda: next(process_counts),
        )

        self.assertEqual({"output", "verifier"}, set(result))
        for role, evidence in result.items():
            self.assertEqual(1, evidence["returncode"])
            self.assertEqual(0, evidence["origin_process_count_before"])
            self.assertEqual(0, evidence["origin_process_count_after"])
            self.assertEqual("ValueError", evidence["result"]["error_type"])
            self.assertEqual("non_retryable", evidence["result"]["classification"])
            self.assertEqual(
                f"{role} payload fields are invalid",
                evidence["result"]["error"],
            )
        self.assertEqual(
            [
                [
                    r"C:\Package\Spectrum Organizer.exe",
                    "--origin-output-worker",
                ],
                [
                    r"C:\Package\Spectrum Organizer.exe",
                    "--origin-verifier-worker",
                ],
            ],
            [call[0] for call in calls],
        )
        for _command, kwargs in calls:
            self.assertEqual("{}", kwargs["input"])
            self.assertEqual(20, kwargs["timeout"])
            self.assertEqual({"LOCALAPPDATA": r"C:\isolated"}, kwargs["env"])

    def test_packaged_worker_probe_rejects_exit_one_without_exact_structured_contract(self):
        def run(_command, **_kwargs):
            return mock.Mock(returncode=1, stdout="", stderr="import failed")

        with self.assertRaisesRegex(RuntimeError, "structured invalid-contract"):
            package_smoke._probe_packaged_worker_entrypoints(
                pathlib.Path(r"C:\Package\Spectrum Organizer.exe"),
                {"LOCALAPPDATA": r"C:\isolated"},
                process_runner=run,
                origin_process_count=lambda: 0,
            )

    def test_smoke_script_targets_packaged_exe_and_is_not_product_imported(self):
        script = ROOT / "validation" / "task17_package_smoke.py"
        text = script.read_text(encoding="utf-8")

        self.assertEqual(ROOT, SMOKE_ROOT)
        self.assertIn('"Spectrum Organizer.exe"', text)
        self.assertIn('"runtime-localappdata"', text)
        self.assertIn("clean_environment_gate.py", text)
        self.assertIn("final_origin_process_count", text)
        self.assertIn("clean_gate_evidence_scope", text)
        self.assertNotIn("originpro", text)
        self.assertNotRegex(text, r'(?i)ROOT\s*/\s*"[^"]+\.opju?"')

    def test_smoke_uses_fresh_runtime_appdata_to_avoid_stale_startup_proof(self):
        with tempfile.TemporaryDirectory() as temp:
            evidence_dir = pathlib.Path(temp)
            stale_app_root = evidence_dir / "runtime-localappdata" / "Spectrum Organizer"
            (stale_app_root / "data").mkdir(parents=True)
            (stale_app_root / "temp").mkdir()
            (stale_app_root / "logs").mkdir()

            fresh = _new_runtime_appdata(evidence_dir)

        self.assertNotEqual(stale_app_root.parent, fresh)
        self.assertTrue(fresh.name.startswith("run-"))
        self.assertEqual([], list(fresh.rglob("*")))

    def test_clean_gate_evidence_scope_is_emitted_as_explicit_startup_scope(self):
        scope = _clean_gate_evidence_scope()

        self.assertEqual(True, scope["runtime_text"])
        self.assertEqual("fresh LOCALAPPDATA subtree only", scope["created_paths"])
        self.assertEqual("fresh LOCALAPPDATA subtree only", scope["preexisting_user_paths"])
        self.assertEqual(True, scope["final_origin_process_count"])
        self.assertEqual(True, scope["final_product_process_count"])
        self.assertEqual(
            "controlled startup termination; natural shutdown is not proven by startup-only smoke",
            scope["shutdown"],
        )
        self.assertEqual(
            "not collected; Task 10 real packaged acceptance must collect this evidence",
            scope["worker_open_targets"],
        )

    def test_clean_gate_failure_helper_uses_runtime_text_and_preexisting_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            existing = pathlib.Path(temp) / "already-there.txt"
            failures = _clean_gate_failures(
                runtime_text="validation.task17_package_smoke",
                created_paths=(str(existing),),
                preexisting_user_paths=(str(existing),),
                final_origin_count=0,
                returncode=0,
                shutdown="normal",
            )

        self.assertIn("runtime references validation path", failures)
        self.assertIn("created path collides with preexisting user file", failures)

    def test_text_evidence_is_not_silently_skipped(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime_appdata = pathlib.Path(temp)
            oversized = runtime_appdata / "oversized.log"
            oversized.write_bytes(b"x" * (1024 * 1024 + 1))

            with self.assertRaisesRegex(RuntimeError, "exceeds the size limit"):
                package_smoke._collect_runtime_text(runtime_appdata)

            oversized.unlink()
            invalid = runtime_appdata / "invalid.json"
            invalid.write_bytes(b"\xff\xfe\xfd")
            with self.assertRaisesRegex(RuntimeError, "UTF-8"):
                package_smoke._collect_runtime_text(runtime_appdata)

    def test_runtime_text_byte_count_uses_encoded_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime_appdata = pathlib.Path(temp)
            evidence = runtime_appdata / "multibyte.json"
            evidence.write_text("光谱", encoding="utf-8")

            text, bytes_checked = package_smoke._collect_runtime_text_with_byte_count(runtime_appdata)

        self.assertEqual("光谱", text)
        self.assertEqual(len("光谱".encode("utf-8")), bytes_checked)

    def test_known_text_metadata_failure_is_not_treated_as_absence(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime_appdata = pathlib.Path(temp)
            evidence = runtime_appdata / "runtime.log"
            evidence.write_text("runtime evidence", encoding="utf-8")
            original_stat = pathlib.Path.stat

            def stat_with_failure(path, *args, **kwargs):
                if path == evidence:
                    raise PermissionError("metadata denied")
                return original_stat(path, *args, **kwargs)

            with (
                mock.patch.object(pathlib.Path, "stat", autospec=True, side_effect=stat_with_failure),
                self.assertRaisesRegex(RuntimeError, "cannot be inspected"),
            ):
                package_smoke._collect_runtime_text(runtime_appdata)

    def test_linked_runtime_target_outside_fresh_appdata_is_rejected_before_filtering(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            runtime_appdata = root / "runtime-localappdata"
            linked_directory = runtime_appdata / "linked-runtime"
            outside_directory = root / "outside-runtime"
            linked_directory.mkdir(parents=True)
            outside_directory.mkdir()
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
                self.assertRaisesRegex(RuntimeError, "outside the fresh runtime appdata"),
            ):
                package_smoke._collect_runtime_text(runtime_appdata)

    def test_failed_rerun_removes_previous_packaged_smoke_summary(self):
        with tempfile.TemporaryDirectory() as temp:
            evidence_dir = pathlib.Path(temp)
            summary_path = evidence_dir / "packaged-smoke-summary.json"
            summary_path.write_text('{"clean_gate_failures": []}', encoding="utf-8")

            with (
                mock.patch.object(package_smoke, "run_packaged_smoke", side_effect=RuntimeError("new run failed")),
                self.assertRaisesRegex(RuntimeError, "new run failed"),
            ):
                package_smoke.main(["--evidence-dir", str(evidence_dir)])

            self.assertFalse(summary_path.exists())

    def test_failed_previous_summary_invalidation_preserves_the_lock(self):
        with tempfile.TemporaryDirectory() as temp:
            evidence_dir = pathlib.Path(temp)
            summary_path = evidence_dir / "packaged-smoke-summary.json"
            lock_path = evidence_dir / ".packaged-smoke.lock"
            summary_path.write_text('{"clean_gate_failures": []}', encoding="utf-8")
            original_replace = package_smoke.os.replace
            original_unlink = pathlib.Path.unlink

            def fail_summary_move(source, destination):
                if pathlib.Path(source) == summary_path:
                    raise OSError("summary move denied")
                return original_replace(source, destination)

            def fail_summary_unlink(path, *args, **kwargs):
                if path == summary_path:
                    raise OSError("summary unlink denied")
                return original_unlink(path, *args, **kwargs)

            with (
                mock.patch.object(package_smoke.os, "replace", side_effect=fail_summary_move),
                mock.patch.object(pathlib.Path, "unlink", fail_summary_unlink),
                mock.patch.object(package_smoke, "run_packaged_smoke") as run_smoke,
                self.assertRaisesRegex(RuntimeError, "invalidate previous packaged smoke summary"),
            ):
                package_smoke.main(["--evidence-dir", str(evidence_dir)])

            run_smoke.assert_not_called()
            self.assertTrue(summary_path.is_file())
            self.assertTrue(lock_path.is_dir())

    def test_overlapping_failed_rerun_cannot_leave_earlier_success_summary(self):
        with tempfile.TemporaryDirectory() as temp:
            evidence_dir = pathlib.Path(temp)
            summary_path = evidence_dir / "packaged-smoke-summary.json"
            first_started = threading.Event()
            allow_first_to_finish = threading.Event()
            first_finished = threading.Event()
            second_invalidated = threading.Event()
            errors: list[BaseException] = []
            original_unlink = pathlib.Path.unlink

            def fake_smoke(*, evidence_dir, timeout_seconds=180):
                if threading.current_thread().name == "package-smoke-first":
                    first_started.set()
                    self.assertTrue(allow_first_to_finish.wait(timeout=10))
                    return {"clean_gate_failures": []}
                self.assertTrue(first_finished.wait(timeout=10))
                raise RuntimeError("later run failed")

            def observe_summary_invalidation(path, *args, **kwargs):
                if (
                    path == summary_path
                    and threading.current_thread().name == "package-smoke-second"
                ):
                    second_invalidated.set()
                return original_unlink(path, *args, **kwargs)

            def invoke_main():
                try:
                    package_smoke.main(["--evidence-dir", str(evidence_dir)])
                except BaseException as exc:
                    errors.append(exc)

            with (
                mock.patch.object(package_smoke, "run_packaged_smoke", side_effect=fake_smoke),
                mock.patch.object(pathlib.Path, "unlink", observe_summary_invalidation),
            ):
                first = threading.Thread(target=invoke_main, name="package-smoke-first")
                second = threading.Thread(target=invoke_main, name="package-smoke-second")
                first.start()
                self.assertTrue(first_started.wait(timeout=10))
                second.start()
                second_invalidated.wait(timeout=2)
                allow_first_to_finish.set()
                first.join(timeout=10)
                self.assertFalse(first.is_alive())
                first_finished.set()
                second.join(timeout=10)
                self.assertFalse(second.is_alive())

            self.assertEqual(1, len(errors))
            self.assertRegex(str(errors[0]), "already running")
            self.assertFalse(second_invalidated.is_set())
            self.assertTrue(summary_path.is_file())

    def test_lock_release_failure_with_unlink_failure_does_not_leave_public_summary(self):
        with tempfile.TemporaryDirectory() as temp:
            evidence_dir = pathlib.Path(temp)
            summary_path = evidence_dir / "packaged-smoke-summary.json"
            original_unlink = pathlib.Path.unlink

            def fail_public_summary_unlink(path, *args, **kwargs):
                if path == summary_path and path.exists():
                    raise OSError("public summary unlink failed")
                return original_unlink(path, *args, **kwargs)

            with (
                mock.patch.object(
                    package_smoke,
                    "run_packaged_smoke",
                    return_value={"clean_gate_failures": []},
                ),
                mock.patch.object(
                    package_smoke,
                    "release_owned_directory_lock",
                    side_effect=package_smoke.OwnedDirectoryLockError(
                        "Could not release packaged smoke lock"
                    ),
                ),
                mock.patch.object(pathlib.Path, "unlink", fail_public_summary_unlink),
                self.assertRaisesRegex(RuntimeError, "Could not release packaged smoke lock"),
            ):
                package_smoke.main(["--evidence-dir", str(evidence_dir)])

            self.assertFalse(summary_path.exists())


if __name__ == "__main__":
    unittest.main()
