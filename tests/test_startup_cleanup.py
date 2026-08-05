import json
import os
import pathlib
import shutil
import subprocess
import sys
import time
import uuid
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

class WorkspaceTempDir:
    def __init__(self):
        self.root = ROOT / ".test-tmp" / "task1"
        self.path = self.root / f"case-{uuid.uuid4().hex}"

    def __enter__(self):
        self.path.mkdir(parents=True, exist_ok=False)
        return str(self.path)

    def __exit__(self, exc_type, exc, tb):
        shutil.rmtree(self.path, ignore_errors=True)
        for path in (self.root, self.root.parent):
            try:
                path.rmdir()
            except OSError:
                pass
        return False

def workspace_tempdir():
    return WorkspaceTempDir()

class StartupCleanupTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "win32", "Windows junction semantics are required")
    def test_cleanup_retains_dangling_junction_temp_root(self):
        import _winapi

        from spectrum_organizer.safety.startup_cleanup import cleanup_temp_runs

        with workspace_tempdir() as tmp:
            target = pathlib.Path(tmp) / "junction-target"
            temp_root = pathlib.Path(tmp) / "dangling-temp-root"
            target.mkdir()
            _winapi.CreateJunction(str(target), str(temp_root))
            target.rmdir()
            try:
                result = cleanup_temp_runs(temp_root)

                self.assertEqual([], result.deleted)
                self.assertEqual([temp_root], result.retained)
                self.assertIn(str(temp_root), result.warning_message)
            finally:
                temp_root.rmdir()

    def test_cleanup_refuses_self_consistent_replacement_without_external_anchor_match(self):
        import json

        from spectrum_organizer.safety.identity_paths import path_identity
        from spectrum_organizer.safety.owned_paths import (
            CleanupRefusedError,
            _anchor_auth_hmac,
            _ownership_anchor_key_path,
            cleanup_owned_temp_root,
            create_run_ownership,
        )

        with workspace_tempdir() as tmp:
            ownership = create_run_ownership(tmp, "run-owned", "marker-1", [])
            parked = ownership.temp_root.with_name("parked-original-run")
            ownership.temp_root.rename(parked)
            ownership.temp_root.mkdir()
            foreign = ownership.temp_root / "foreign.txt"
            foreign.write_text("FOREIGN USER CONTENT", encoding="utf-8")
            root_identity = path_identity(ownership.temp_root)
            foreign_identity = path_identity(foreign)
            (ownership.temp_root / "ownership.json").write_text(
                json.dumps(
                    {
                        "run_id": "run-owned",
                        "marker_id": "marker-1",
                        "temp_root": str(ownership.temp_root),
                        "temp_root_identity": list(root_identity),
                        "allowed_children": [str(foreign)],
                        "allowed_child_identities": [
                            {
                                "path": str(foreign),
                                "identity": list(foreign_identity),
                            }
                        ],
                        "protected_paths": [],
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            anchor = ownership.temp_root.parent / (
                f".{ownership.temp_root.name}.ownership-anchor.json"
            )
            anchor.unlink()
            anchor_payload = {
                "run_id": "run-owned",
                "marker_id": "marker-1",
                "temp_root": str(ownership.temp_root),
                "temp_root_identity": list(root_identity),
            }
            anchor.write_text(
                json.dumps(
                    {
                        **anchor_payload,
                        "auth_hmac": _anchor_auth_hmac(
                            anchor_payload,
                            _ownership_anchor_key_path(
                                ownership.temp_root.parent
                            ).read_bytes(),
                        ),
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )

            with self.assertRaises(CleanupRefusedError):
                cleanup_owned_temp_root(
                    ownership.temp_root,
                    expected_root_identity=ownership.temp_root_identity,
                )

            self.assertEqual(
                "FOREIGN USER CONTENT",
                foreign.read_text(encoding="utf-8"),
            )

    def test_startup_retains_self_consistent_replacement_with_unauthenticated_anchor(self):
        import json

        from spectrum_organizer.safety.identity_paths import path_identity
        from spectrum_organizer.safety.owned_paths import create_run_ownership
        from spectrum_organizer.safety.startup_cleanup import cleanup_temp_runs

        with workspace_tempdir() as tmp:
            ownership = create_run_ownership(tmp, "run-owned", "marker-1", [])
            ownership.temp_root.rename(
                ownership.temp_root.with_name("parked-original-run")
            )
            ownership.temp_root.mkdir()
            foreign = ownership.temp_root / "foreign.txt"
            foreign.write_text("FOREIGN USER CONTENT", encoding="utf-8")
            root_identity = path_identity(ownership.temp_root)
            foreign_identity = path_identity(foreign)
            (ownership.temp_root / "ownership.json").write_text(
                json.dumps(
                    {
                        "run_id": "run-owned",
                        "marker_id": "marker-1",
                        "temp_root": str(ownership.temp_root),
                        "temp_root_identity": list(root_identity),
                        "allowed_children": [str(foreign)],
                        "allowed_child_identities": [
                            {
                                "path": str(foreign),
                                "identity": list(foreign_identity),
                            }
                        ],
                        "protected_paths": [],
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            anchor = ownership.temp_root.parent / (
                f".{ownership.temp_root.name}.ownership-anchor.json"
            )
            anchor.write_text(
                json.dumps(
                    {
                        "run_id": "run-owned",
                        "marker_id": "marker-1",
                        "temp_root": str(ownership.temp_root),
                        "temp_root_identity": list(root_identity),
                        "auth_hmac": "0" * 64,
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )

            result = cleanup_temp_runs(ownership.temp_root.parent)

            self.assertEqual([], result.deleted)
            self.assertIn(ownership.temp_root, result.retained)
            self.assertEqual(
                "FOREIGN USER CONTENT",
                foreign.read_text(encoding="utf-8"),
            )

    @unittest.skipUnless(sys.platform == "win32", "Windows lock semantics are required")
    def test_active_child_lease_retains_the_entire_owned_run_until_release(self):
        from spectrum_organizer.safety.owned_paths import (
            ACTIVE_LEASE_FILE,
            add_allowed_child,
            bind_allowed_child_identity,
            create_run_ownership,
            read_ownership,
        )
        from spectrum_organizer.safety.identity_paths import path_identity
        from spectrum_organizer.safety.startup_cleanup import cleanup_temp_runs

        with workspace_tempdir() as tmp:
            ownership = create_run_ownership(tmp, "run-active", "marker-active", [])
            lease_path = ownership.temp_root / ACTIVE_LEASE_FILE
            payload_path = ownership.temp_root / "payload.json"
            ready_path = ownership.temp_root / "ready.txt"
            ownership = add_allowed_child(ownership, lease_path)
            payload_path.write_text("owned", encoding="utf-8")
            ownership = add_allowed_child(ownership, payload_path)
            ownership = add_allowed_child(ownership, ready_path)
            command = [
                sys.executable,
                "-c",
                (
                    "from pathlib import Path; import sys, time; "
                    "from spectrum_organizer.safety.owned_paths import acquire_run_lease; "
                    "lease=acquire_run_lease(Path(sys.argv[1])); "
                    "Path(sys.argv[2]).write_text('ready', encoding='utf-8'); "
                    "time.sleep(30)"
                ),
                str(ownership.temp_root),
                str(ready_path),
            ]
            environment = dict(os.environ)
            environment["PYTHONPATH"] = str(SRC)
            process = subprocess.Popen(
                command,
                env=environment,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            try:
                deadline = time.monotonic() + 5
                while not ready_path.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(ready_path.exists())
                bind_allowed_child_identity(
                    read_ownership(ownership.temp_root),
                    ready_path,
                    expected_identity=path_identity(ready_path),
                )

                temp_root = pathlib.Path(tmp) / "Spectrum Organizer" / "temp"
                result = cleanup_temp_runs(temp_root)

                self.assertEqual([], result.deleted)
                self.assertEqual([ownership.temp_root], result.retained)
                self.assertTrue(payload_path.exists())
                self.assertTrue((ownership.temp_root / "ownership.json").exists())
            finally:
                process.terminate()
                process.wait(timeout=5)

            result = cleanup_temp_runs(temp_root)
            self.assertEqual([ownership.temp_root], result.deleted)
            self.assertFalse(ownership.temp_root.exists())

    def test_deletes_only_ownership_json_proven_temp_run_directories(self):
        from spectrum_organizer.safety.owned_paths import create_run_ownership
        from spectrum_organizer.safety.startup_cleanup import cleanup_temp_runs

        with workspace_tempdir() as tmp:
            ownership = create_run_ownership(tmp, "run-owned", "marker-1", [])
            allowed_child = ownership.temp_root / "owned-child"
            allowed_child.mkdir()
            from spectrum_organizer.safety.owned_paths import add_allowed_child
            add_allowed_child(ownership, allowed_child)
            temp_root = pathlib.Path(tmp) / "Spectrum Organizer" / "temp"
            unknown = temp_root / "unknown"
            unknown.mkdir()

            result = cleanup_temp_runs(temp_root)

            self.assertFalse(ownership.temp_root.exists())
            self.assertTrue(unknown.exists())
            self.assertEqual(result.deleted, [ownership.temp_root])
            self.assertEqual(result.retained, [unknown])
            self.assertIsNone(result.warning_message)

    def test_leaves_marker_only_malformed_or_unrecognized_directories_untouched(self):
        from spectrum_organizer.safety.startup_cleanup import (
            TEMP_RUN_MARKER,
            cleanup_temp_runs,
        )

        with workspace_tempdir() as tmp:
            temp_root = pathlib.Path(tmp)
            marker_only = temp_root / "marker-only"
            marker_only.mkdir()
            (marker_only / TEMP_RUN_MARKER).write_text(
                json.dumps({"app": "Spectrum Organizer", "kind": "temp-run"}),
                encoding="utf-8",
            )
            malformed = temp_root / "malformed"
            malformed.mkdir()
            (malformed / "ownership.json").write_text("{bad", encoding="utf-8")
            malformed_schema = temp_root / "malformed-schema"
            malformed_schema.mkdir()
            (malformed_schema / "ownership.json").write_text(
                json.dumps({
                    "run_id": 123,
                    "marker_id": "marker-1",
                    "temp_root": str(malformed_schema),
                    "allowed_children": None,
                    "protected_paths": [],
                }),
                encoding="utf-8",
            )
            malformed_top_level = temp_root / "malformed-top-level"
            malformed_top_level.mkdir()
            (malformed_top_level / "ownership.json").write_text("null", encoding="utf-8")
            unrecognized = temp_root / "unrecognized"
            unrecognized.mkdir()

            result = cleanup_temp_runs(temp_root)

            self.assertTrue(marker_only.exists())
            self.assertTrue(malformed.exists())
            self.assertTrue(malformed_schema.exists())
            self.assertTrue(malformed_top_level.exists())
            self.assertTrue(unrecognized.exists())
            self.assertEqual(result.deleted, [])
            self.assertEqual(result.retained, [malformed, malformed_schema, malformed_top_level, marker_only, unrecognized])
            self.assertIsNone(result.warning_message)

    def test_files_are_retained_without_user_warning(self):
        from spectrum_organizer.safety.startup_cleanup import cleanup_temp_runs

        with workspace_tempdir() as tmp:
            temp_root = pathlib.Path(tmp)
            retained_file = temp_root / "note.txt"
            retained_file.write_text("keep", encoding="utf-8")

            result = cleanup_temp_runs(temp_root)

            self.assertTrue(retained_file.exists())
            self.assertEqual(result.retained, [retained_file])
            self.assertIsNone(result.warning_message)

    def test_owned_temp_run_delete_failure_is_retained_with_actionable_warning(self):
        from spectrum_organizer.safety.owned_paths import add_allowed_child, create_run_ownership
        from spectrum_organizer.safety.startup_cleanup import cleanup_temp_runs

        with workspace_tempdir() as tmp:
            ownership = create_run_ownership(tmp, "run-owned", "marker-1", [])
            allowed_child = ownership.temp_root / "owned-child"
            allowed_child.mkdir()
            add_allowed_child(ownership, allowed_child)
            temp_root = pathlib.Path(tmp) / "Spectrum Organizer" / "temp"

            with mock.patch(
                "spectrum_organizer.safety.owned_paths._remove_owned_tree",
                side_effect=OSError("locked"),
            ):
                result = cleanup_temp_runs(temp_root)

            self.assertTrue(ownership.temp_root.exists())
            self.assertEqual(result.deleted, [])
            self.assertEqual(result.retained, [ownership.temp_root])
            self.assertIsNotNone(result.warning_message)
            self.assertIn(str(ownership.temp_root), result.warning_message)
            self.assertIn("locked", result.warning_message)

    def test_startup_adds_one_notice_for_owned_temp_delete_failure(self):
        from spectrum_organizer.safety.owned_paths import add_allowed_child, create_run_ownership
        from spectrum_organizer.safety.startup_cleanup import startup
        from spectrum_organizer.single_instance import FakeInstanceBackend

        with workspace_tempdir() as tmp:
            ownership = create_run_ownership(tmp, "run-owned", "marker-1", [])
            allowed_child = ownership.temp_root / "owned-child"
            allowed_child.mkdir()
            add_allowed_child(ownership, allowed_child)

            with mock.patch(
                "spectrum_organizer.safety.owned_paths._remove_owned_tree",
                side_effect=OSError("locked"),
            ):
                result = startup(FakeInstanceBackend(), local_appdata=tmp)

            self.assertEqual(1, len(result.notices))
            self.assertEqual("warning", result.notices[0].severity)
            self.assertIn(str(ownership.temp_root), result.notices[0].message)
            self.assertIn("locked", result.notices[0].message)

    def test_startup_orchestration_retains_unknown_temp_without_user_warning(self):
        from spectrum_organizer.app_paths import ensure_app_paths
        from spectrum_organizer.single_instance import FakeInstanceBackend
        from spectrum_organizer.safety.startup_cleanup import startup

        with workspace_tempdir() as tmp:
            paths = ensure_app_paths(local_appdata=tmp)
            unknown = paths.temp / "unknown-temp"
            unknown.mkdir()

            result = startup(FakeInstanceBackend(), local_appdata=tmp)

            self.assertFalse(result.instance.should_exit)
            self.assertEqual("", result.warnings)

    def test_startup_retains_the_settings_store_that_detected_damage(self):
        from spectrum_organizer.app_paths import ensure_app_paths
        from spectrum_organizer.single_instance import FakeInstanceBackend
        from spectrum_organizer.safety.startup_cleanup import startup

        with workspace_tempdir() as tmp:
            paths = ensure_app_paths(local_appdata=tmp)
            paths.settings_file.write_text("{bad", encoding="utf-8")

            result = startup(FakeInstanceBackend(), local_appdata=tmp)

            self.assertIsNotNone(result.settings_store)
            self.assertTrue(paths.settings_file.exists())
            self.assertEqual(1, len(result.notices))
            self.assertEqual([], result.settings_store.discard_damaged_file())
            self.assertFalse(paths.settings_file.exists())

    def test_startup_orchestration_second_launch_skips_app_state(self):
        from spectrum_organizer.single_instance import FakeInstanceBackend
        from spectrum_organizer.safety.startup_cleanup import startup

        with workspace_tempdir() as tmp:
            result = startup(FakeInstanceBackend(already_running=True), local_appdata=tmp)

            self.assertTrue(result.instance.should_exit)
            self.assertFalse((pathlib.Path(tmp) / "Spectrum Organizer").exists())

if __name__ == "__main__":
    unittest.main()
