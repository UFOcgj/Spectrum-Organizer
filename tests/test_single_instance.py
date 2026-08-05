import pathlib
import uuid
import sys
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


class SingleInstanceTests(unittest.TestCase):
    def test_first_launch_acquires_lock_without_activation(self):
        from spectrum_organizer.single_instance import FakeInstanceBackend, SingleInstance

        backend = FakeInstanceBackend()
        gate = SingleInstance(backend)

        result = gate.enter()

        self.assertTrue(result.is_primary)
        self.assertFalse(result.should_exit)
        self.assertEqual(backend.activation_requests, 0)

    def test_second_launch_requests_activation_and_exits(self):
        from spectrum_organizer.single_instance import FakeInstanceBackend, SingleInstance

        backend = FakeInstanceBackend(already_running=True)
        gate = SingleInstance(backend)

        result = gate.enter()

        self.assertFalse(result.is_primary)
        self.assertTrue(result.should_exit)
        self.assertEqual(backend.activation_requests, 1)

    def test_second_launch_exits_before_state_access(self):
        from spectrum_organizer.single_instance import FakeInstanceBackend, guarded_startup

        backend = FakeInstanceBackend(already_running=True)
        state_accessed = []

        result = guarded_startup(backend, state_start=lambda: state_accessed.append(True))

        self.assertTrue(result.should_exit)
        self.assertEqual(state_accessed, [])

    def test_guarded_startup_runs_instance_gate_before_state_access(self):
        from spectrum_organizer.single_instance import FakeInstanceBackend, guarded_startup

        backend = FakeInstanceBackend(already_running=True)
        calls = []

        result = guarded_startup(backend, state_start=lambda: calls.append("state"))

        self.assertTrue(result.should_exit)
        self.assertEqual(calls, [])
        self.assertEqual(backend.activation_requests, 1)

    def test_windows_backend_constructor_works_without_os_getsid(self):
        from spectrum_organizer.single_instance import WindowsMutexBackend

        backend = WindowsMutexBackend(name_prefix=f"SpectrumOrganizerTest-{uuid.uuid4().hex}")

        self.assertIn("SpectrumOrganizerTest-", backend.name)
        self.assertTrue(backend.activation_event_name.endswith("-Activate"))

    def test_windows_backend_activation_signal_contract(self):
        from spectrum_organizer.single_instance import WindowsMutexBackend

        backend = WindowsMutexBackend(name_prefix=f"SpectrumOrganizerTest-{uuid.uuid4().hex}")
        self.addCleanup(_close_backend_handles, backend)

        self.assertFalse(backend.wait_for_activation_request(timeout_ms=0))
        backend.request_activation()

        self.assertTrue(backend.wait_for_activation_request(timeout_ms=1000))
        self.assertFalse(backend.wait_for_activation_request(timeout_ms=0))

    def test_windows_activation_request_grants_primary_foreground_authority(self):
        from spectrum_organizer.single_instance import (
            WindowsMutexBackend,
        )

        backend = WindowsMutexBackend(
            name_prefix=f"SpectrumOrganizerTest-{uuid.uuid4().hex}"
        )
        self.addCleanup(_close_backend_handles, backend)

        with mock.patch(
            "spectrum_organizer.single_instance."
            "_allow_any_process_to_set_foreground",
            return_value=True,
        ) as grant:
            backend.request_activation()

        grant.assert_called_once_with()
        self.assertTrue(
            backend.wait_for_activation_request(timeout_ms=1000)
        )

    def test_primary_retains_activation_event_before_secondary_exits(self):
        from spectrum_organizer.single_instance import WindowsMutexBackend

        prefix = f"SpectrumOrganizerTest-{uuid.uuid4().hex}"
        primary = WindowsMutexBackend(name_prefix=prefix)
        secondary = WindowsMutexBackend(name_prefix=prefix)
        self.addCleanup(_close_backend_handles, primary)
        self.addCleanup(_close_backend_handles, secondary)

        self.assertTrue(primary.acquire())
        self.assertFalse(secondary.acquire())
        secondary.request_activation()
        secondary._activation_event.Close()
        secondary._activation_event = None

        self.assertTrue(primary.wait_for_activation_request(timeout_ms=0))

    def test_main_window_launch_has_no_project_specific_protected_paths(self):
        from unittest.mock import patch

        from spectrum_organizer.__main__ import _launch_main_window

        with patch("spectrum_organizer.ui.app.run_main_window", return_value=0) as run_main_window:
            self.assertEqual(0, _launch_main_window(None))

        self.assertEqual((), run_main_window.call_args.kwargs["protected_paths"])
        self.assertIsNone(run_main_window.call_args.kwargs["startup_result"])

    def test_main_uses_single_instance_gate_before_app_state(self):
        from spectrum_organizer.__main__ import main
        from spectrum_organizer.single_instance import FakeInstanceBackend

        class ExplodingPath:
            def __fspath__(self):
                raise AssertionError("app state was accessed")

        result = main(
            instance_backend=FakeInstanceBackend(already_running=True),
            local_appdata=ExplodingPath(),
        )

        self.assertEqual(result, 0)

    def test_main_exposes_primary_activation_requests_to_window(self):
        from spectrum_organizer.__main__ import main
        from spectrum_organizer.single_instance import FakeInstanceBackend

        backend = FakeInstanceBackend()
        launched = []

        result = main(
            instance_backend=backend,
            local_appdata=self._local_appdata(),
            window_launcher=lambda startup_result: (
                launched.append(startup_result),
                0,
            )[1],
        )

        self.assertEqual(0, result)
        backend.request_activation()
        probe = launched[0].activation_request_probe
        self.assertTrue(probe(timeout_ms=0))
        self.assertFalse(probe(timeout_ms=0))

    def _local_appdata(self):
        import tempfile

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return directory.name

def _close_backend_handles(backend):
    for attribute in ("_activation_event", "_mutex"):
        handle = getattr(backend, attribute)
        if handle is not None:
            handle.Close()
            setattr(backend, attribute, None)


if __name__ == "__main__":
    unittest.main()
