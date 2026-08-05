import pathlib
import os
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


class ProcessJobCloseTests(unittest.TestCase):
    def test_parent_start_gate_requires_matching_secret_not_path_existence(self):
        from spectrum_organizer.safety import process_job

        with tempfile.TemporaryDirectory() as directory:
            gate = pathlib.Path(directory) / "parent-start.gate"
            gate.write_text("stale-value", encoding="ascii")
            environment = {
                process_job.PARENT_START_GATE_ENV: str(gate),
                process_job.PARENT_START_GATE_TOKEN_ENV: "task-secret",
            }
            with (
                unittest.mock.patch.dict(os.environ, environment, clear=False),
                unittest.mock.patch.object(process_job.time, "sleep"),
                unittest.mock.patch.object(process_job.time, "monotonic", side_effect=(0.0, 1.0)),
            ):
                with self.assertRaisesRegex(process_job.ProcessJobError, "release"):
                    process_job.wait_for_parent_start_gate(timeout=0.5)

    def test_failed_close_keeps_job_handle_and_process_reference(self):
        from spectrum_organizer.safety import process_job

        job = process_job.WindowsProcessJob(123)
        process = type("Process", (), {"_spectrum_organizer_job": job})()
        kernel32 = unittest.mock.Mock()
        kernel32.CloseHandle.return_value = False

        with unittest.mock.patch.object(process_job, "_kernel32", kernel32):
            with self.assertRaises(OSError):
                process_job.close_bound_process_job(process)

        self.assertEqual(123, job._handle)
        self.assertIs(job, process._spectrum_organizer_job)

    def test_bind_failure_keeps_job_reference_when_initial_close_fails(self):
        from spectrum_organizer.safety import process_job

        for failure_stage in ("setup", "assignment"):
            with self.subTest(stage=failure_stage):
                process = type("Process", (), {"_handle": 456})()
                kernel32 = unittest.mock.Mock()
                kernel32.CreateJobObjectW.return_value = 123
                kernel32.SetInformationJobObject.return_value = failure_stage != "setup"
                kernel32.AssignProcessToJobObject.return_value = False
                kernel32.CloseHandle.return_value = False

                with unittest.mock.patch.object(process_job, "_kernel32", kernel32):
                    with unittest.mock.patch.object(process_job.sys, "platform", "win32"):
                        with self.assertRaisesRegex(process_job.ProcessJobError, "close|Close|Job"):
                            process_job.bind_process_to_job(process, required=True)

                self.assertIsInstance(process._spectrum_organizer_job, process_job.WindowsProcessJob)
                self.assertEqual(123, process._spectrum_organizer_job._handle)

    def test_unassigned_retained_job_also_terminates_exact_process(self):
        from spectrum_organizer.safety import process_job

        job = process_job.WindowsProcessJob(123)
        job.assigned = False
        process = unittest.mock.Mock()
        process._spectrum_organizer_job = job
        kernel32 = unittest.mock.Mock()
        kernel32.TerminateJobObject.return_value = True

        with unittest.mock.patch.object(process_job, "_kernel32", kernel32):
            process_job.terminate_bound_process(process)

        kernel32.TerminateJobObject.assert_called_once_with(123, 1)
        process.kill.assert_called_once_with()


@unittest.skipUnless(sys.platform == "win32", "Windows Job Objects are required")
class WindowsProcessJobTests(unittest.TestCase):
    def test_parent_start_gate_blocks_child_work_until_parent_releases_it(self):
        from spectrum_organizer.safety.process_job import (
            PARENT_START_GATE_ENV,
            PARENT_START_GATE_TOKEN_ENV,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            gate = root / "parent-start.gate"
            marker = root / "child-started"
            environment = os.environ.copy()
            environment[PARENT_START_GATE_ENV] = str(gate)
            environment[PARENT_START_GATE_TOKEN_ENV] = "task-secret"
            environment["PYTHONPATH"] = os.pathsep.join(
                (str(SRC), environment.get("PYTHONPATH", ""))
            ).rstrip(os.pathsep)
            child_code = (
                "from pathlib import Path; import sys; "
                "from spectrum_organizer.safety.process_job import wait_for_parent_start_gate; "
                "wait_for_parent_start_gate(); "
                "Path(sys.argv[1]).write_text('started', encoding='utf-8')"
            )
            process = subprocess.Popen(
                [sys.executable, "-c", child_code, str(marker)],
                env=environment,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            try:
                time.sleep(0.2)
                self.assertFalse(marker.exists())

                bind_process_to_job = __import__(
                    "spectrum_organizer.safety.process_job",
                    fromlist=["bind_process_to_job"],
                ).bind_process_to_job
                bind_process_to_job(process, required=True)
                gate.write_text("task-secret", encoding="ascii")
                process.wait(timeout=5)

                self.assertTrue(marker.exists())
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                from spectrum_organizer.safety.process_job import close_bound_process_job

                close_bound_process_job(process)

    def test_bound_job_terminates_the_exact_running_child_without_pid_lookup(self):
        from spectrum_organizer.safety.process_job import (
            bind_process_to_job,
            close_bound_process_job,
            terminate_bound_process,
        )

        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            job = bind_process_to_job(process, required=True)
            self.assertIsNotNone(job)

            terminate_bound_process(process)
            process.wait(timeout=5)

            self.assertIsNotNone(process.returncode)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            close_bound_process_job(process)

    def test_bound_job_terminates_descendant_processes(self):
        from spectrum_organizer.safety.process_job import (
            bind_process_to_job,
            close_bound_process_job,
            terminate_bound_process,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            start_child = root / "start-child"
            child_started = root / "child-started"
            forbidden_marker = root / "descendant-survived"
            child_code = (
                "from pathlib import Path; import sys, time; "
                "time.sleep(1); Path(sys.argv[1]).write_text('survived', encoding='utf-8')"
            )
            parent_code = (
                "from pathlib import Path; import subprocess, sys, time; "
                "start=Path(sys.argv[1]); ready=Path(sys.argv[2]); marker=sys.argv[3]; "
                "\nwhile not start.exists(): time.sleep(0.01)\n"
                "subprocess.Popen([sys.executable, '-c', sys.argv[4], marker]); "
                "ready.write_text('ready', encoding='utf-8'); time.sleep(30)"
            )
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    parent_code,
                    str(start_child),
                    str(child_started),
                    str(forbidden_marker),
                    child_code,
                ],
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            try:
                bind_process_to_job(process, required=True)
                start_child.write_text("go", encoding="utf-8")
                deadline = time.monotonic() + 5
                while not child_started.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(child_started.exists())

                terminate_bound_process(process)
                process.wait(timeout=5)
                time.sleep(1.2)

                self.assertFalse(forbidden_marker.exists())
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                close_bound_process_job(process)


if __name__ == "__main__":
    unittest.main()
