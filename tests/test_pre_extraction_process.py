import pathlib
import sys
import tempfile
import threading
import unittest
import unittest.mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from spectrum_organizer import product_runner
from spectrum_organizer import pre_extraction_process
from spectrum_organizer.safety.owned_paths import (
    ACTIVE_LEASE_FILE,
    add_allowed_child,
    bind_allowed_child_identity,
    cleanup_owned_temp_root,
    create_run_ownership,
)
from spectrum_organizer.safety.process_job import ProcessJobError
from spectrum_organizer.safety.identity_paths import file_sha256


def _worker_args(manifest, result):
    return [file_sha256(manifest), str(manifest), str(result)]


class PreExtractionProcessTests(unittest.TestCase):
    def test_child_refuses_registered_manifest_replaced_before_read(self):
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            ownership = create_run_ownership(
                base / "localapp", "run-1", "marker-1", []
            )
            manifest = ownership.temp_root / "pre_extraction_context.json"
            result = ownership.temp_root / "pre_extraction_result.json"
            pending = result.with_name(f"{result.name}.pending")
            for path in (
                manifest,
                result,
                pending,
                ownership.temp_root / ACTIVE_LEASE_FILE,
            ):
                ownership = add_allowed_child(ownership, path)
            payload = {
                "temp_root": str(ownership.temp_root),
                "run_id": ownership.run_id,
                "marker_id": ownership.marker_id,
                "selected_source_paths": [str(base / "owned.opju")],
                "output_parent": str(base / "output"),
                "settings_snapshot": {"s1Limit": 1},
                "protected_paths": [],
                "timestamp": "2026-08-03T00:00:00+00:00",
            }
            manifest.write_text(product_runner.json.dumps(payload), encoding="utf-8")
            ownership = bind_allowed_child_identity(ownership, manifest)
            manifest.rename(ownership.temp_root / "parked-owned-manifest.json")
            payload["selected_source_paths"] = [str(base / "FORGED.opju")]
            manifest.write_text(product_runner.json.dumps(payload), encoding="utf-8")

            with (
                unittest.mock.patch.object(
                    pre_extraction_process,
                    "wait_for_parent_start_gate",
                    return_value=None,
                ),
                unittest.mock.patch.object(
                    pre_extraction_process,
                    "prepare_extraction_context",
                ) as prepare,
            ):
                return_code = pre_extraction_process.pre_extraction_process_main(
                    _worker_args(manifest, result)
                )

            self.assertEqual(1, return_code)
            prepare.assert_not_called()

    def test_successful_child_records_actual_pre_extraction_context_for_runtime_audit(self):
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            ownership = create_run_ownership(
                base / "localapp",
                "run-1",
                "marker-1",
                [],
            )
            manifest = ownership.temp_root / "pre_extraction_context.json"
            result = ownership.temp_root / "pre_extraction_result.json"
            pending = result.with_name(f"{result.name}.pending")
            ownership = add_allowed_child(ownership, manifest)
            ownership = add_allowed_child(ownership, result)
            ownership = add_allowed_child(ownership, pending)
            ownership = add_allowed_child(
                ownership,
                ownership.temp_root / ACTIVE_LEASE_FILE,
            )
            manifest.write_text(
                product_runner.json.dumps(
                    {
                        "temp_root": str(ownership.temp_root),
                        "run_id": ownership.run_id,
                        "marker_id": ownership.marker_id,
                        "selected_source_paths": [str(base / "raw.opju")],
                        "output_parent": str(base / "output"),
                        "settings_snapshot": {"s1Limit": 1},
                        "protected_paths": [],
                        "timestamp": "2026-08-02T00:00:00+00:00",
                    }
                ),
                encoding="utf-8",
            )
            bind_allowed_child_identity(ownership, manifest)
            context = unittest.mock.Mock()
            context_payload = {
                "run_id": "run-1",
                "selected_source_paths": [str(base / "raw.opju")],
            }

            with (
                unittest.mock.patch.object(
                    pre_extraction_process,
                    "wait_for_parent_start_gate",
                    return_value=None,
                ),
                unittest.mock.patch.object(
                    pre_extraction_process,
                    "prepare_extraction_context",
                    return_value=context,
                ),
                unittest.mock.patch.object(
                    pre_extraction_process,
                    "_context_to_payload",
                    return_value=context_payload,
                ),
                unittest.mock.patch.object(
                    pre_extraction_process,
                    "record_runtime_audit_event",
                    create=True,
                ) as audit,
            ):
                return_code = pre_extraction_process.pre_extraction_process_main(
                    _worker_args(manifest, result)
                )

            self.assertEqual(0, return_code)
            audit.assert_called_once_with(
                "pre_extraction_context",
                context_payload,
            )

    def test_runner_rejects_nonpositive_or_unrepresentable_wait_configuration(self):
        for field in ("cancellation_timeout", "cancellation_poll_interval"):
            for value in (0, float("nan"), float("inf"), 10**400):
                with (
                    self.subTest(field=field, value=value),
                    self.assertRaisesRegex(ValueError, field),
                ):
                    product_runner.PreExtractionSubprocessRunner(
                        **{field: value}
                    )

    def test_parent_does_not_overwrite_preexisting_manifest_hard_link(self):
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            source = base / "raw.opju"
            source.write_bytes(b"raw")
            output = base / "output"
            output.mkdir()
            sentinel = base / "sentinel.txt"
            sentinel.write_text("unchanged", encoding="utf-8")
            process_factory = unittest.mock.Mock(
                side_effect=product_runner.ProductRunnerError("child was launched")
            )
            real_add_allowed_child = product_runner.add_allowed_child

            def inject_manifest_link(ownership, child):
                updated = real_add_allowed_child(ownership, child)
                if pathlib.Path(child).name == "pre_extraction_context.json":
                    pathlib.Path(child).hardlink_to(sentinel)
                return updated

            runner = product_runner.PreExtractionSubprocessRunner(
                local_appdata=base / "localapp",
                process_factory=process_factory,
            )
            with unittest.mock.patch.object(
                product_runner,
                "add_allowed_child",
                side_effect=inject_manifest_link,
            ), self.assertRaises(product_runner.ProductRunnerError):
                runner(
                    selected_source_paths=(source,),
                    output_parent=output,
                    settings_snapshot={"s1Limit": 1_000_000, "steadyEmissionY": "S1c"},
                )

            process_factory.assert_not_called()
            self.assertEqual("unchanged", sentinel.read_text(encoding="utf-8"))

    def test_child_does_not_overwrite_result_hard_link_created_during_work(self):
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            ownership = create_run_ownership(base / "localapp", "run-1", "marker-1", [])
            manifest = ownership.temp_root / "pre_extraction_context.json"
            result = ownership.temp_root / "pre_extraction_result.json"
            pending = result.with_name(f"{result.name}.pending")
            ownership = add_allowed_child(ownership, manifest)
            ownership = add_allowed_child(ownership, result)
            ownership = add_allowed_child(ownership, pending)
            ownership = add_allowed_child(
                ownership,
                ownership.temp_root / ACTIVE_LEASE_FILE,
            )
            manifest.write_text(
                product_runner.json.dumps(
                    {
                        "temp_root": str(ownership.temp_root),
                        "run_id": ownership.run_id,
                        "marker_id": ownership.marker_id,
                    }
                ),
                encoding="utf-8",
            )
            sentinel = base / "sentinel.txt"
            sentinel.write_text("unchanged", encoding="utf-8")

            def create_result_link(**_kwargs):
                result.hardlink_to(sentinel)
                return unittest.mock.Mock()

            with unittest.mock.patch.object(
                pre_extraction_process,
                "wait_for_parent_start_gate",
                return_value=None,
            ), unittest.mock.patch.object(
                pre_extraction_process,
                "prepare_extraction_context",
                side_effect=create_result_link,
            ):
                return_code = pre_extraction_process.pre_extraction_process_main(
                    _worker_args(manifest, result)
                )

            self.assertEqual(1, return_code)
            self.assertEqual("unchanged", sentinel.read_text(encoding="utf-8"))

    def test_child_does_not_publish_partial_pre_extraction_result(self):
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            ownership = create_run_ownership(base / "localapp", "run-1", "marker-1", [])
            manifest = ownership.temp_root / "pre_extraction_context.json"
            result = ownership.temp_root / "pre_extraction_result.json"
            pending = result.with_name(f"{result.name}.pending")
            ownership = add_allowed_child(ownership, manifest)
            ownership = add_allowed_child(ownership, result)
            ownership = add_allowed_child(ownership, pending)
            ownership = add_allowed_child(
                ownership,
                ownership.temp_root / ACTIVE_LEASE_FILE,
            )
            manifest.write_text(
                product_runner.json.dumps(
                    {
                        "temp_root": str(ownership.temp_root),
                        "run_id": ownership.run_id,
                        "marker_id": ownership.marker_id,
                        "selected_source_paths": [],
                        "output_parent": str(base / "output"),
                        "settings_snapshot": {},
                        "protected_paths": [],
                        "timestamp": "2026-07-24T00:00:00+00:00",
                    }
                ),
                encoding="utf-8",
            )
            context = unittest.mock.Mock(
                run_id=ownership.run_id,
                timestamp="2026-07-24T00:00:00+00:00",
                selected_source_paths=(),
                output_parent=base / "output",
                settings_snapshot={},
                source_fingerprints_before=(),
                temp_root=ownership.temp_root,
                run_owned_source_copy_paths=(),
                protected_fingerprints_before=(),
            )

            def fail_after_partial_write(_payload, stream, **_kwargs):
                stream.write('{"ok":')
                raise OSError("disk full")

            with (
                unittest.mock.patch.object(
                    pre_extraction_process,
                    "wait_for_parent_start_gate",
                    return_value=None,
                ),
                unittest.mock.patch.object(
                    pre_extraction_process,
                    "prepare_extraction_context",
                    return_value=context,
                ),
                unittest.mock.patch.object(
                    product_runner.json,
                    "dump",
                    side_effect=fail_after_partial_write,
                ),
            ):
                return_code = pre_extraction_process.pre_extraction_process_main(
                    _worker_args(manifest, result)
                )

            self.assertEqual(1, return_code)
            self.assertFalse(result.exists())
            self.assertFalse(pending.exists())

    def test_pre_extraction_start_gate_does_not_overwrite_existing_entity(self):
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            sentinel = base / "sentinel.txt"
            sentinel.write_text("unchanged", encoding="utf-8")
            gate = base / "start.gate"
            gate.hardlink_to(sentinel)
            runner = product_runner.PreExtractionSubprocessRunner()

            with self.assertRaises(FileExistsError):
                runner._release_start_gate(unittest.mock.Mock(), gate)

            self.assertEqual("unchanged", sentinel.read_text(encoding="utf-8"))

    def test_worker_refuses_to_write_unregistered_external_result_path(self):
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            ownership = create_run_ownership(base / "localapp", "run-1", "marker-1", [])
            manifest = ownership.temp_root / "pre-extraction-manifest.json"
            add_allowed_child(ownership, manifest)
            manifest.write_text(
                product_runner.json.dumps(
                    {
                        "temp_root": str(ownership.temp_root),
                        "run_id": ownership.run_id,
                        "marker_id": ownership.marker_id,
                    }
                ),
                encoding="utf-8",
            )
            protected = base / "protected.opju"
            protected.write_bytes(b"must remain unchanged")

            with unittest.mock.patch.object(
                pre_extraction_process,
                "wait_for_parent_start_gate",
                return_value=None,
            ):
                return_code = pre_extraction_process.pre_extraction_process_main(
                    _worker_args(manifest, protected)
                )

            self.assertEqual(1, return_code)
            self.assertEqual(b"must remain unchanged", protected.read_bytes())

    def test_cancel_observed_during_job_bind_does_not_release_start_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            source = base / "raw.opju"
            source.write_bytes(b"raw")
            output = base / "output"
            output.mkdir()
            gate_writes = []
            real_write_text = pathlib.Path.write_text

            class ExitedChild:
                returncode = -1

                def communicate(self, timeout=None):
                    del timeout
                    return "", ""

                def poll(self):
                    return -1

            runner = product_runner.PreExtractionSubprocessRunner(
                local_appdata=base / "localapp",
                process_factory=lambda command, **kwargs: ExitedChild(),
            )
            runner._require_process_job = True

            def cancel_during_bind(process, *, required):
                del process, required
                runner.cancel()
                return None

            def record_gate_write(path, text, *args, **kwargs):
                if path.name.endswith(".gate"):
                    gate_writes.append(path)
                return real_write_text(path, text, *args, **kwargs)

            with unittest.mock.patch.object(
                product_runner,
                "bind_process_to_job",
                side_effect=cancel_during_bind,
            ), unittest.mock.patch.object(
                product_runner,
                "_terminate_process_nonblocking",
                return_value=None,
            ), unittest.mock.patch.object(
                pathlib.Path,
                "write_text",
                new=record_gate_write,
            ):
                with self.assertRaisesRegex(product_runner.ProductRunnerError, "取消"):
                    runner(
                        selected_source_paths=(source,),
                        output_parent=output,
                        settings_snapshot={"s1Limit": 1_000_000, "steadyEmissionY": "S1c"},
                    )

            self.assertEqual([], gate_writes)

    def test_job_close_failure_blocks_pre_extraction_cleanup(self):
        runner = product_runner.PreExtractionSubprocessRunner(process_factory=lambda *args, **kwargs: None)

        class ExitedChild:
            def poll(self):
                return -1

        with unittest.mock.patch.object(
            product_runner,
            "close_bound_process_job",
            side_effect=OSError("CloseHandle failed"),
        ):
            with self.assertRaisesRegex(product_runner.ExtractionCleanupBlockedError, "Job|CloseHandle"):
                runner._wait_for_termination_process(ExitedChild())

    def test_bind_failure_with_unconfirmed_child_preserves_owned_temp_root(self):
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            source = base / "raw.opju"
            source.write_bytes(b"raw")
            output = base / "output"
            output.mkdir()

            class UnstoppableChild:
                returncode = None

                def poll(self):
                    return None

                def kill(self):
                    return None

                def wait(self, timeout=None):
                    raise product_runner.subprocess.TimeoutExpired("preflight", timeout)

            runner = product_runner.PreExtractionSubprocessRunner(
                local_appdata=base / "localapp",
                process_factory=lambda command, **kwargs: UnstoppableChild(),
                cancellation_timeout=0.01,
            )
            with unittest.mock.patch.object(
                product_runner,
                "bind_process_to_job",
                side_effect=ProcessJobError("bind failed"),
            ):
                with self.assertRaisesRegex(
                    product_runner.ExtractionCleanupBlockedError,
                    "清理|终止|运行",
                ):
                    runner(
                        selected_source_paths=(source,),
                        output_parent=output,
                        settings_snapshot={"s1Limit": 1000000, "steadyEmissionY": "S1c"},
                    )

            temp_base = base / "localapp" / "Spectrum Organizer" / "temp"
            self.assertTrue(temp_base.exists() and any(temp_base.iterdir()))

    def test_real_child_process_builds_verified_context_without_origin(self):
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            source = base / "raw.opju"
            source.write_bytes(b"origin-project-copy-test")
            output = base / "output"
            output.mkdir()
            runner = product_runner.PreExtractionSubprocessRunner(local_appdata=base / "localapp")

            context = runner(
                selected_source_paths=(source,),
                output_parent=output,
                settings_snapshot={"s1Limit": 1000000, "steadyEmissionY": "S1c"},
            )

            self.assertEqual((source,), context.selected_source_paths)
            self.assertEqual(output, context.output_parent)
            self.assertEqual(source.read_bytes(), context.run_owned_source_copy_paths[0].read_bytes())
            self.assertNotEqual(source, context.run_owned_source_copy_paths[0])
            cleanup_owned_temp_root(context.temp_root)

    def test_parent_context_validation_stops_when_cancelled(self):
        from spectrum_organizer.safety.owned_paths import read_ownership

        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            source = base / "raw.opju"
            source.write_bytes(b"origin-project-copy-test")
            output = base / "output"
            output.mkdir()
            runner = product_runner.PreExtractionSubprocessRunner(local_appdata=base / "localapp")
            context = runner(
                selected_source_paths=(source,),
                output_parent=output,
                settings_snapshot={"s1Limit": 1000000, "steadyEmissionY": "S1c"},
            )
            runner._cancelled.set()

            with self.assertRaisesRegex(product_runner.ProductRunnerError, "取消"):
                runner._validate_context(
                    context,
                    read_ownership(context.temp_root),
                    (source,),
                    output,
                    {"s1Limit": 1000000, "steadyEmissionY": "S1c"},
                )

            cleanup_owned_temp_root(context.temp_root)

    def test_parent_context_rejects_two_sources_sharing_one_owned_copy(self):
        from dataclasses import replace
        from spectrum_organizer.safety.owned_paths import read_ownership

        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            sources = (base / "raw-a.opju", base / "raw-b.opju")
            for source in sources:
                source.write_bytes(b"identical-origin-project")
            output = base / "output"
            output.mkdir()
            runner = product_runner.PreExtractionSubprocessRunner(local_appdata=base / "localapp")
            context = runner(
                selected_source_paths=sources,
                output_parent=output,
                settings_snapshot={"s1Limit": 1000000, "steadyEmissionY": "S1c"},
            )
            shared_copy = context.run_owned_source_copy_paths[0]
            forged = replace(context, run_owned_source_copy_paths=(shared_copy, shared_copy))

            with self.assertRaisesRegex(product_runner.ProductRunnerError, "副本|copy"):
                runner._validate_context(
                    forged,
                    read_ownership(context.temp_root),
                    sources,
                    output,
                    {"s1Limit": 1000000, "steadyEmissionY": "S1c"},
                )

            cleanup_owned_temp_root(context.temp_root)

    def test_cancel_terminates_blocked_context_child_and_cleans_owned_root(self):
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            source = base / "raw.opju"
            source.write_bytes(b"raw")
            output = base / "output"
            output.mkdir()
            started = threading.Event()
            released = threading.Event()

            class BlockingChild:
                pid = None
                returncode = None

                def communicate(self, timeout=None):
                    started.set()
                    if not released.wait(timeout or 0):
                        raise product_runner.subprocess.TimeoutExpired("preflight", timeout)
                    self.returncode = -15
                    return "", ""

                def poll(self):
                    return self.returncode

                def terminate(self):
                    self.returncode = -15
                    released.set()

            runner = product_runner.PreExtractionSubprocessRunner(
                local_appdata=base / "localapp",
                process_factory=lambda command, **kwargs: BlockingChild(),
                cancellation_timeout=1,
                cancellation_poll_interval=0.01,
            )
            errors = []

            def run():
                try:
                    runner(
                        selected_source_paths=(source,),
                        output_parent=output,
                        settings_snapshot={"s1Limit": 1000000, "steadyEmissionY": "S1c"},
                    )
                except Exception as exc:
                    errors.append(exc)

            thread = threading.Thread(target=run)
            thread.start()
            self.assertTrue(started.wait(2))

            runner.cancel()
            thread.join(3)

            self.assertFalse(thread.is_alive())
            self.assertRegex(str(errors[0]), "取消")
            temp_base = base / "localapp" / "Spectrum Organizer" / "temp"
            self.assertFalse(temp_base.exists() and any(temp_base.iterdir()))

    def test_keyboard_interrupt_during_pre_extraction_cleans_owned_root(self):
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            source = base / "raw.opju"
            source.write_bytes(b"raw")
            output = base / "output"
            output.mkdir()

            class InterruptingChild:
                returncode = 0

                def communicate(self, timeout=None):
                    del timeout
                    raise KeyboardInterrupt

                def poll(self):
                    return self.returncode

            runner = product_runner.PreExtractionSubprocessRunner(
                local_appdata=base / "localapp",
                process_factory=lambda command, **kwargs: InterruptingChild(),
            )

            with self.assertRaises(KeyboardInterrupt):
                runner(
                    selected_source_paths=(source,),
                    output_parent=output,
                    settings_snapshot={"s1Limit": 1_000_000, "steadyEmissionY": "S1c"},
                )

            temp_base = base / "localapp" / "Spectrum Organizer" / "temp"
            self.assertFalse(temp_base.exists() and any(temp_base.iterdir()))

    def test_child_failure_is_reported_before_parent_cleans_owned_root(self):
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            missing = base / "missing.opju"
            output = base / "output"
            output.mkdir()
            runner = product_runner.PreExtractionSubprocessRunner(local_appdata=base / "localapp")

            with self.assertRaisesRegex(product_runner.ProductRunnerError, "missing.opju|stat source"):
                runner(
                    selected_source_paths=(missing,),
                    output_parent=output,
                    settings_snapshot={"s1Limit": 1000000, "steadyEmissionY": "S1c"},
                )

            temp_base = base / "localapp" / "Spectrum Organizer" / "temp"
            self.assertFalse(temp_base.exists() and any(temp_base.iterdir()))

    def test_cancel_publishes_termination_helper_before_cancel_state(self):
        runner = product_runner.PreExtractionSubprocessRunner()
        runner._current_process = object()
        terminate_entered = threading.Event()
        release_terminate = threading.Event()
        helper = object()

        def terminate(_process):
            terminate_entered.set()
            release_terminate.wait(1)
            return helper

        with unittest.mock.patch.object(
            product_runner,
            "_terminate_process_nonblocking",
            side_effect=terminate,
        ):
            cancel_thread = threading.Thread(target=runner.cancel)
            cancel_thread.start()
            self.assertTrue(terminate_entered.wait(1))
            try:
                self.assertFalse(runner._cancelled.is_set())
                self.assertIsNone(runner._termination_process)
            finally:
                release_terminate.set()
                cancel_thread.join(1)

        self.assertFalse(cancel_thread.is_alive())
        self.assertTrue(runner._cancelled.is_set())
        self.assertIs(helper, runner._termination_process)

    def test_completed_child_is_not_cleanup_blocked_by_taskkill_race(self):
        runner = product_runner.PreExtractionSubprocessRunner()

        class Process:
            def poll(self):
                return 0

        class Helper:
            _spectrum_organizer_termination_state = {"error": "taskkill 返回代码 128"}

            def join(self, timeout=None):
                del timeout

            def is_alive(self):
                return False

        runner._termination_process = Helper()

        runner._wait_for_termination_process(Process())

        self.assertIsNone(runner._termination_process)

    def test_termination_helper_completion_waits_for_child_exit(self):
        runner = product_runner.PreExtractionSubprocessRunner(cancellation_timeout=0.2)

        class Process:
            def __init__(self):
                self.returncode = None
                self.wait_calls = []

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                self.wait_calls.append(timeout)
                self.returncode = 0
                return self.returncode

        class Helper:
            _spectrum_organizer_termination_state = {"error": None}

            def join(self, timeout=None):
                del timeout

            def is_alive(self):
                return False

        process = Process()
        runner._termination_process = Helper()

        with unittest.mock.patch.object(product_runner, "close_bound_process_job"):
            runner._wait_for_termination_process(process)

        self.assertEqual(process.wait_calls, [0.2])
        self.assertTrue(runner._termination_finalized)

    def test_job_close_blocks_late_termination_helper_registration(self):
        runner = product_runner.PreExtractionSubprocessRunner(cancellation_timeout=0.2)

        class Process:
            def poll(self):
                return 0

        class Helper:
            def __init__(self):
                self.joined = False

            def join(self, timeout=None):
                del timeout
                self.joined = True

            def is_alive(self):
                return False

        process = Process()
        helper = Helper()
        runner._current_process = process

        def close_job(_process):
            runner.cancel()

        with (
            unittest.mock.patch.object(product_runner, "close_bound_process_job", side_effect=close_job),
            unittest.mock.patch.object(
                product_runner,
                "_terminate_process_nonblocking",
                return_value=helper,
            ) as terminate,
        ):
            runner._wait_for_termination_process(process)

        terminate.assert_not_called()
        self.assertFalse(helper.joined)
        self.assertIsNone(runner._termination_process)
        self.assertTrue(runner._termination_finalized)


if __name__ == "__main__":
    unittest.main()
