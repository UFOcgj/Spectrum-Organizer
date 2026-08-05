import pathlib
import json
import os
import subprocess
import sys
import unittest
import unittest.mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from spectrum_organizer.safety.process_boundary import (
    ORIGIN_CONFIRM_HIDDEN,
    ORIGIN_VISIBLE_USER_BLOCKED,
    ProcessBoundaryError,
    ProcessInfo,
    WindowsOriginProcessController,
    WindowsOriginProcessProbe,
    classify_process,
    preflight_origin_boundary,
)
import spectrum_organizer.safety.process_boundary as process_boundary


class FakeController:
    def __init__(self, current=None, graceful=True, force=True, survivors=None):
        self.current = dict(current or {})
        self.graceful = graceful
        self.force = force
        self.survivors = set(survivors or [])
        self.calls = []

    def close_program_owned(self, identity):
        self.calls.append(("close_program_owned", identity.pid))
        self.current.pop(identity.pid, None)

    def current_process(self, pid):
        self.calls.append(("current_process", pid))
        return self.current.get(pid)

    def graceful_close(self, identity):
        self.calls.append(("graceful_close", identity.pid))
        if self.graceful:
            self.current.pop(identity.pid, None)
            self.survivors.discard(identity.pid)
        return self.graceful

    def force_close(self, identity):
        self.calls.append(("force_close", identity.pid))
        if self.force:
            self.current.pop(identity.pid, None)
            self.survivors.discard(identity.pid)
        return self.force

    def is_running(self, identity):
        self.calls.append(("is_running", identity.pid))
        return identity.pid in self.survivors or identity.pid in self.current

class FakeCompleted:
    def __init__(self, *, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode

class ProcessBoundaryTests(unittest.TestCase):
    def test_windows_origin_process_probe_maps_origin_json_to_process_info(self):
        def run_command(command, *, timeout):
            self.assertIn("Origin", command)
            self.assertIn("Get-Process", command)
            self.assertNotIn("Get-CimInstance", command)
            self.assertEqual(5, timeout)
            return FakeCompleted(
                stdout=(
                    '[{"name":"Origin64.exe","pid":321,"start_time_ns":1700000000000000000,'
                    '"main_window_handle":200},'
                    '{"name":"NotOrigin.exe","pid":999,"start_time_ns":1700000000000000999,'
                    '"main_window_handle":0}]'
                )
            )

        probe = WindowsOriginProcessProbe(
            command_runner=run_command,
            window_visibility=lambda handle: (handle == 200, False),
        )

        self.assertEqual(
            (ProcessInfo(321, 1700000000000000000, visible=True, taskbar_visible=False, program_owned=False),),
            probe(),
        )

    def test_windows_origin_process_probe_treats_zero_window_handle_as_hidden(self):
        visibility_handles = []

        def classify_window(handle):
            visibility_handles.append(handle)
            return False, False

        probe = WindowsOriginProcessProbe(
            command_runner=lambda command, *, timeout: FakeCompleted(
                stdout=(
                    '{"name":"Origin64.exe","pid":322,'
                    '"start_time_ns":1700000000000000001,'
                    '"main_window_handle":0}'
                )
            ),
            window_visibility=classify_window,
        )

        self.assertEqual(
            (
                ProcessInfo(
                    322,
                    1700000000000000001,
                    visible=False,
                    taskbar_visible=False,
                    program_owned=False,
                ),
            ),
            probe(),
        )
        self.assertEqual([0], visibility_handles)

    def test_windows_origin_process_probe_keeps_native_visibility_failure_blocking(self):
        user32 = unittest.mock.Mock()
        user32.IsWindowVisible.side_effect = OSError("native visibility unavailable")
        probe = WindowsOriginProcessProbe(
            command_runner=lambda command, *, timeout: FakeCompleted(
                stdout=(
                    '{"name":"Origin64.exe","pid":323,'
                    '"start_time_ns":1700000000000000002,'
                    '"main_window_handle":200}'
                )
            ),
        )

        with unittest.mock.patch.object(
            process_boundary.ctypes,
            "windll",
            unittest.mock.Mock(user32=user32),
            create=True,
        ):
            self.assertEqual(
                (
                    ProcessInfo(
                        323,
                        1700000000000000002,
                        visible=True,
                        taskbar_visible=True,
                        program_owned=False,
                    ),
                ),
                probe(),
            )

    def test_windows_process_identity_uses_full_datetime_tick_precision(self):
        expression = ".Ticks - 621355968000000000) * 100)"
        self.assertIn(expression, process_boundary._ORIGIN_PROCESS_QUERY)
        self.assertNotIn("ToUnixTimeMilliseconds", process_boundary._ORIGIN_PROCESS_QUERY)

        process = ProcessInfo(323, 700_000_100, visible=False, taskbar_visible=False, program_owned=False)
        commands = []
        controller = WindowsOriginProcessController(
            command_runner=lambda command, *, timeout: commands.append(command)
            or FakeCompleted(stdout="closed\n"),
            process_probe=lambda *, timeout=5.0: (process,),
        )

        controller.graceful_close(process.identity)
        controller.force_close(process.identity)

        self.assertTrue(commands)
        for command in commands:
            self.assertIn(expression, command)
            self.assertNotIn("ToUnixTimeMilliseconds", command)

    @unittest.skipUnless(sys.platform == "win32", "requires Windows PowerShell")
    def test_process_identity_arithmetic_executes_in_windows_powershell_51(self):
        query = process_boundary._ORIGIN_PROCESS_QUERY.replace(
            "$_.ProcessName -like 'Origin*'",
            "$_.Id -eq $PID",
        )

        completed = process_boundary._run_powershell(query)
        version = process_boundary._run_powershell("$PSVersionTable.PSVersion.Major")

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual(0, version.returncode, version.stderr)
        self.assertEqual("5", version.stdout.strip())
        records = json.loads(completed.stdout)
        records = records if isinstance(records, list) else [records]
        self.assertTrue(records)
        self.assertTrue(all(int(record["start_time_ns"]) > 0 for record in records))

    def test_windows_origin_process_probe_failure_is_blocking_not_empty(self):
        probe = WindowsOriginProcessProbe(
            command_runner=lambda command, *, timeout: FakeCompleted(stderr="WMI denied", returncode=1)
        )

        with self.assertRaisesRegex(ProcessBoundaryError, "Origin process probe failed: WMI denied"):
            probe()

    def test_windows_origin_process_probe_timeout_is_blocking_not_empty(self):
        def timeout(_command, *, timeout):
            raise subprocess.TimeoutExpired("powershell", timeout)

        probe = WindowsOriginProcessProbe(command_runner=timeout)

        with self.assertRaisesRegex(ProcessBoundaryError, "timed out"):
            probe()

    def test_default_powershell_probe_has_bounded_subprocess_timeout(self):
        completed = FakeCompleted(stdout="[]")
        with unittest.mock.patch.object(process_boundary.subprocess, "run", return_value=completed) as run:
            process_boundary._run_powershell("query")

        self.assertEqual(5, run.call_args.kwargs["timeout"])
        self.assertEqual(
            getattr(subprocess, "CREATE_NO_WINDOW", 0),
            run.call_args.kwargs["creationflags"],
        )
        executable = pathlib.Path(run.call_args.args[0][0])
        self.assertTrue(executable.is_absolute())
        self.assertEqual("powershell.exe", executable.name.casefold())
        self.assertIn("system32", tuple(part.casefold() for part in executable.parts))

    @unittest.skipUnless(sys.platform == "win32", "requires Windows system directory API")
    def test_powershell_path_cannot_be_redirected_by_systemroot_environment(self):
        with unittest.mock.patch.dict(os.environ, {"SystemRoot": r"C:\redirected-root"}):
            executable = process_boundary._windows_powershell_executable()

        self.assertTrue(executable.is_absolute())
        self.assertNotIn("redirected-root", str(executable).casefold())
        self.assertTrue(executable.is_file())

    def test_default_origin_probe_forwards_remaining_timeout_budget(self):
        completed = FakeCompleted(stdout="[]")
        with unittest.mock.patch.object(process_boundary.subprocess, "run", return_value=completed) as run:
            process_boundary.default_origin_process_probe(timeout=0.25)

        self.assertEqual(0.25, run.call_args.kwargs["timeout"])

    def test_public_windows_probe_accepts_runner_timeout_budget(self):
        completed = FakeCompleted(stdout="[]")
        probe = WindowsOriginProcessProbe()
        with unittest.mock.patch.object(process_boundary.subprocess, "run", return_value=completed) as run:
            probe(timeout=0.25)

        self.assertEqual(0.25, run.call_args.kwargs["timeout"])

    def test_public_windows_probe_forwards_budget_to_injected_command_runner(self):
        command_runner = unittest.mock.Mock(return_value=FakeCompleted(stdout="[]"))
        probe = WindowsOriginProcessProbe(command_runner=command_runner)

        probe(timeout=0.25)

        self.assertEqual(0.25, command_runner.call_args.kwargs["timeout"])

    def test_controller_internal_deadline_timeout_is_a_process_boundary_error(self):
        process = ProcessInfo(322, 600, visible=False, taskbar_visible=False, program_owned=False)
        command_runner = unittest.mock.Mock(return_value=FakeCompleted(stdout="closed\n"))
        controller = WindowsOriginProcessController(
            command_runner=command_runner,
            process_probe=lambda *, timeout: (process,),
        )

        with unittest.mock.patch.object(
            process_boundary.time,
            "monotonic",
            side_effect=(0.0, 0.0, 0.02),
        ):
            with self.assertRaisesRegex(ProcessBoundaryError, "timed out") as raised:
                controller.force_close(process.identity, timeout=0.01)

        self.assertIsInstance(raised.exception.__cause__, subprocess.TimeoutExpired)
        command_runner.assert_not_called()

    def test_controller_command_timeout_is_a_process_boundary_error(self):
        timeout_error = subprocess.TimeoutExpired("powershell", 0.25)
        controller = WindowsOriginProcessController(
            command_runner=unittest.mock.Mock(side_effect=timeout_error),
            process_probe=lambda *, timeout: (),
        )
        identity = ProcessInfo(322, 600, False, False, False).identity

        with self.assertRaisesRegex(ProcessBoundaryError, "timed out") as raised:
            controller.force_close(identity, timeout=0.25)

        self.assertIs(raised.exception.__cause__, timeout_error)

    def test_controller_probe_timeout_is_a_process_boundary_error(self):
        timeout_error = subprocess.TimeoutExpired("probe", 0.25)
        controller = WindowsOriginProcessController(
            process_probe=unittest.mock.Mock(side_effect=timeout_error),
        )

        with self.assertRaisesRegex(ProcessBoundaryError, "probe timed out") as raised:
            controller.current_process(322, timeout=0.25)

        self.assertIs(raised.exception.__cause__, timeout_error)

    def test_windows_origin_process_controller_uses_identity_for_current_and_running_checks(self):
        original = ProcessInfo(322, 600, visible=False, taskbar_visible=False, program_owned=False)
        controller = WindowsOriginProcessController(
            process_probe=lambda *, timeout=5.0: (original,)
        )

        self.assertEqual(original, controller.current_process(322))
        self.assertTrue(controller.is_running(original.identity))
        self.assertFalse(controller.is_running(ProcessInfo(322, 601, False, False, False).identity))

    def test_windows_origin_process_controller_closes_without_launching_origin(self):
        calls = []
        identity = ProcessInfo(323, 700, visible=False, taskbar_visible=False, program_owned=False).identity

        def run_command(command, *, timeout):
            del timeout
            calls.append(command)
            return FakeCompleted(stdout="closed\n")

        controller = WindowsOriginProcessController(
            command_runner=run_command,
            process_probe=lambda *, timeout=5.0: (),
        )

        self.assertTrue(controller.graceful_close(identity))
        self.assertTrue(controller.force_close(identity))
        controller.close_program_owned(identity)
        self.assertEqual(3, len(calls))
        self.assertTrue(all("Start-Process" not in command for command in calls))

    def test_close_program_owned_rejects_unconfirmed_force_close(self):
        process = ProcessInfo(
            323,
            700,
            visible=False,
            taskbar_visible=False,
            program_owned=True,
        )
        controller = WindowsOriginProcessController(
            command_runner=lambda command, *, timeout: FakeCompleted(
                stdout="running\n"
            ),
            process_probe=lambda *, timeout=5.0: (process,),
        )

        with self.assertRaisesRegex(
            ProcessBoundaryError,
            "survived cleanup",
        ):
            controller.close_program_owned(process.identity)

    def test_force_close_rechecks_same_hidden_identity_and_embeds_start_time_guard(self):
        process = ProcessInfo(323, 700_000_000, visible=False, taskbar_visible=False, program_owned=False)
        calls = []

        def run_command(command, *, timeout):
            del timeout
            calls.append(command)
            return FakeCompleted(stdout="closed\n")

        controller = WindowsOriginProcessController(
            command_runner=run_command,
            process_probe=lambda *, timeout=5.0: (process,),
        )

        self.assertTrue(controller.force_close(process.identity))
        self.assertIn("700000000", calls[0])
        self.assertIn("IsWindowVisible", calls[0])
        self.assertNotIn("MainWindowHandle -ne 0", calls[0])
        self.assertIn("Stop-Process -InputObject $p -Force", calls[0])
        self.assertNotIn("Stop-Process -Id 323 -Force", calls[0])
        self.assertEqual(1, calls[0].count("Get-Process -Id 323"))
        self.assertIn("$p.HasExited", calls[0])

    def test_graceful_close_rechecks_hidden_window_inside_same_command(self):
        process = ProcessInfo(324, 701_000_000, visible=False, taskbar_visible=False, program_owned=False)
        calls = []

        def run_command(command, *, timeout):
            del timeout
            calls.append(command)
            return FakeCompleted(stdout="visible\n")

        controller = WindowsOriginProcessController(
            command_runner=run_command,
            process_probe=lambda *, timeout=5.0: (process,),
        )

        with self.assertRaisesRegex(ProcessBoundaryError, "visible"):
            controller.graceful_close(process.identity)
        command = calls[0]
        self.assertIn("IsWindowVisible", command)
        self.assertNotIn("MainWindowHandle -ne 0", command)
        self.assertLess(command.index("IsWindowVisible"), command.index("CloseMainWindow"))
        self.assertEqual(1, command.count("Get-Process -Id 324"))
        self.assertIn("$p.HasExited", command)

    def test_force_close_refuses_identity_that_became_visible(self):
        process = ProcessInfo(323, 700, visible=True, taskbar_visible=False, program_owned=False)
        controller = WindowsOriginProcessController(
            command_runner=lambda command, *, timeout: self.fail("force command must not run"),
            process_probe=lambda *, timeout=5.0: (process,),
        )

        with self.assertRaisesRegex(ProcessBoundaryError, "visible"):
            controller.force_close(process.identity)

    def test_classifies_by_visibility_and_program_owned_registry_not_name_only(self):
        self.assertEqual("program_owned", classify_process(ProcessInfo(1, 10, True, True, True)))
        self.assertEqual("visible_user", classify_process(ProcessInfo(2, 10, True, False, False)))
        self.assertEqual("visible_user", classify_process(ProcessInfo(3, 10, False, True, False)))
        self.assertEqual("preexisting_hidden", classify_process(ProcessInfo(4, 10, False, False, False)))

    def test_visible_user_origin_blocks_with_retry_cancel_and_never_closes(self):
        process = ProcessInfo(100, 500, visible=True, taskbar_visible=False, program_owned=False)
        controller = FakeController(current={100: process})
        outcome = preflight_origin_boundary([process], controller)
        self.assertFalse(outcome.can_continue)
        self.assertEqual(ORIGIN_VISIBLE_USER_BLOCKED, outcome.dialog.kind)
        self.assertEqual("请关闭 Origin 后继续", outcome.dialog.title)
        self.assertIn("检测到可见的 Origin 进程 100", outcome.dialog.message)
        self.assertIn("点击下方“重新检测”", outcome.dialog.message)
        self.assertIn("任务会停在这里", outcome.dialog.message)
        self.assertNotIn("Visible Origin", outcome.dialog.message)
        self.assertEqual(("retry", "cancel"), outcome.dialog.actions)
        self.assertTrue(outcome.dialog.topmost)
        self.assertTrue(outcome.dialog.taskbar_visible)
        self.assertEqual([], controller.calls)

    def test_hidden_preexisting_origin_requires_one_confirmation_before_close(self):
        process = ProcessInfo(101, 501, visible=False, taskbar_visible=False, program_owned=False)
        controller = FakeController(current={101: process})
        outcome = preflight_origin_boundary([process], controller, hidden_confirmation=False)
        self.assertFalse(outcome.can_continue)
        self.assertEqual(ORIGIN_CONFIRM_HIDDEN, outcome.dialog.kind)
        self.assertEqual("检测到隐藏 Origin 进程", outcome.dialog.title)
        self.assertIn("检测到隐藏 Origin 进程：101", outcome.dialog.message)
        self.assertNotIn("Hidden Origin", outcome.dialog.message)
        self.assertEqual(("confirm", "cancel"), outcome.dialog.actions)
        self.assertTrue(outcome.dialog.topmost)
        self.assertTrue(outcome.dialog.taskbar_visible)
        self.assertEqual([], controller.calls)

    def test_hidden_confirmation_is_scoped_to_the_exact_process_identity(self):
        previously_confirmed = ProcessInfo(101, 501, visible=False, taskbar_visible=False, program_owned=False)
        newly_detected = ProcessInfo(102, 502, visible=False, taskbar_visible=False, program_owned=False)
        controller = FakeController(current={102: newly_detected})

        outcome = preflight_origin_boundary(
            [newly_detected],
            controller,
            hidden_confirmation={previously_confirmed.identity},
        )

        self.assertFalse(outcome.can_continue)
        self.assertEqual(ORIGIN_CONFIRM_HIDDEN, outcome.dialog.kind)
        self.assertEqual([], controller.calls)

    def test_confirmed_hidden_origin_rechecks_identity_then_gracefully_closes(self):
        process = ProcessInfo(102, 502, visible=False, taskbar_visible=False, program_owned=False)
        controller = FakeController(current={102: process})
        outcome = preflight_origin_boundary([process], controller, hidden_confirmation=True)
        self.assertTrue(outcome.can_continue)
        self.assertEqual((102,), outcome.closed_pids)
        self.assertEqual((), outcome.forced_pids)
        self.assertEqual(
            [("current_process", 102), ("graceful_close", 102), ("is_running", 102)],
            controller.calls,
        )

    def test_hidden_origin_pid_start_time_drift_is_refused(self):
        original = ProcessInfo(103, 503, visible=False, taskbar_visible=False, program_owned=False)
        drifted = ProcessInfo(103, 999, visible=False, taskbar_visible=False, program_owned=False)
        controller = FakeController(current={103: drifted})
        with self.assertRaises(ProcessBoundaryError):
            preflight_origin_boundary([original], controller, hidden_confirmation=True)
        self.assertEqual([("current_process", 103)], controller.calls)

    def test_hidden_origin_becoming_visible_on_recheck_returns_visible_block(self):
        original = ProcessInfo(104, 504, visible=False, taskbar_visible=False, program_owned=False)
        visible = ProcessInfo(104, 504, visible=True, taskbar_visible=False, program_owned=False)
        controller = FakeController(current={104: visible})
        outcome = preflight_origin_boundary([original], controller, hidden_confirmation=True)
        self.assertFalse(outcome.can_continue)
        self.assertEqual(ORIGIN_VISIBLE_USER_BLOCKED, outcome.dialog.kind)
        self.assertEqual([("current_process", 104)], controller.calls)

    def test_hidden_origin_that_exits_after_failed_graceful_close_is_not_forced(self):
        process = ProcessInfo(105, 505, visible=False, taskbar_visible=False, program_owned=False)
        controller = FakeController(current={105: process}, graceful=False, force=True)

        def remove_then_report_missing(identity):
            controller.calls.append(("graceful_close", identity.pid))
            controller.current.pop(identity.pid, None)
            return False

        controller.graceful_close = remove_then_report_missing

        outcome = preflight_origin_boundary([process], controller, hidden_confirmation=True)

        self.assertTrue(outcome.can_continue)
        self.assertEqual((105,), outcome.closed_pids)
        self.assertEqual((), outcome.forced_pids)
        self.assertEqual(
            [("current_process", 105), ("graceful_close", 105), ("current_process", 105), ("is_running", 105)],
            controller.calls,
        )

    def test_hidden_origin_force_closes_after_graceful_failure_without_second_prompt(self):
        process = ProcessInfo(105, 505, visible=False, taskbar_visible=False, program_owned=False)
        controller = FakeController(current={105: process}, graceful=False, force=True)
        outcome = preflight_origin_boundary([process], controller, hidden_confirmation=True)
        self.assertTrue(outcome.can_continue)
        self.assertEqual((105,), outcome.closed_pids)
        self.assertEqual((105,), outcome.forced_pids)
        self.assertEqual(("已强制关闭隐藏 Origin 进程 105。",), outcome.warnings)
        self.assertEqual(
            [
                ("current_process", 105),
                ("graceful_close", 105),
                ("current_process", 105),
                ("force_close", 105),
                ("is_running", 105),
            ],
            controller.calls,
        )

    def test_hidden_origin_force_failure_is_reported(self):
        process = ProcessInfo(106, 506, visible=False, taskbar_visible=False, program_owned=False)
        controller = FakeController(current={106: process}, graceful=False, force=False)
        with self.assertRaises(ProcessBoundaryError):
            preflight_origin_boundary([process], controller, hidden_confirmation=True)

    def test_program_owned_workers_close_silently_and_survivors_fail(self):
        process = ProcessInfo(107, 507, visible=False, taskbar_visible=False, program_owned=True)
        controller = FakeController(current={107: process}, survivors={107})
        with self.assertRaises(ProcessBoundaryError):
            preflight_origin_boundary([process], controller)
        self.assertEqual(("close_program_owned", 107), controller.calls[0])
        self.assertEqual(("is_running", 107), controller.calls[1])

    def test_empty_process_list_can_continue(self):
        outcome = preflight_origin_boundary([], FakeController())
        self.assertTrue(outcome.can_continue)
        self.assertIsNone(outcome.dialog)


if __name__ == "__main__":
    unittest.main()
