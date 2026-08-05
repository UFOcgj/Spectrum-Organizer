import json
import pathlib
import shutil
import sys
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

class SettingsTests(unittest.TestCase):
    def test_acknowledgement_never_deletes_file_replaced_after_identity_check(self):
        from spectrum_organizer.settings import SettingsStore

        with workspace_tempdir() as tmp:
            path = pathlib.Path(tmp) / "settings.json"
            path.write_text("{bad json", encoding="utf-8")
            store = SettingsStore(path)
            store.load()
            replacement = json.dumps({
                "lastOutputParent": "C:/new",
                "s1Limit": 1000000,
                "steadyEmissionY": "S1c",
            })
            original_replace = pathlib.Path.replace

            def replace_after_check(candidate, target):
                if candidate == path:
                    path.write_text(replacement, encoding="utf-8")
                return original_replace(candidate, target)

            with mock.patch.object(pathlib.Path, "replace", replace_after_check):
                notices = store.discard_damaged_file()

            self.assertTrue(path.exists())
            self.assertEqual(replacement, path.read_text(encoding="utf-8"))
            self.assertTrue(notices)

    def test_defaults_when_settings_file_missing(self):
        from spectrum_organizer.settings import SettingsStore

        with workspace_tempdir() as tmp:
            store = SettingsStore(pathlib.Path(tmp) / "settings.json")
            settings, notices = store.load()

            self.assertEqual(settings.lastOutputParent, "")
            self.assertEqual(settings.s1Limit, 2000000)
            self.assertEqual(settings.steadyEmissionY, "S1c")
            self.assertFalse(settings.allowMissingS1)
            self.assertEqual(notices, [])

    def test_legacy_settings_without_allow_missing_s1_load_with_safe_default(self):
        from spectrum_organizer.settings import SettingsStore

        with workspace_tempdir() as tmp:
            path = pathlib.Path(tmp) / "settings.json"
            path.write_text(json.dumps({
                "lastOutputParent": "C:/Output",
                "s1Limit": 1000000,
                "steadyEmissionY": "S1c/R1c",
            }), encoding="utf-8")

            settings, notices = SettingsStore(path).load()

            self.assertEqual([], notices)
            self.assertFalse(settings.allowMissingS1)

    def test_existing_saved_s1_limit_is_not_replaced_by_new_default(self):
        from spectrum_organizer.settings import SettingsStore

        with workspace_tempdir() as tmp:
            path = pathlib.Path(tmp) / "settings.json"
            path.write_text(
                json.dumps(
                    {
                        "lastOutputParent": "C:/Output",
                        "s1Limit": 1000000,
                        "steadyEmissionY": "S1c/R1c",
                    }
                ),
                encoding="utf-8",
            )

            settings, notices = SettingsStore(path).load()

            self.assertEqual(1000000, settings.s1Limit)
            self.assertEqual("S1c/R1c", settings.steadyEmissionY)
            self.assertEqual([], notices)

    def test_corrupt_settings_are_deleted_only_after_notice_is_acknowledged(self):
        from spectrum_organizer.settings import SettingsStore

        with workspace_tempdir() as tmp:
            path = pathlib.Path(tmp) / "settings.json"
            path.write_text("{bad json", encoding="utf-8")
            store = SettingsStore(path)

            settings, notices = store.load()

            self.assertTrue(path.exists())
            self.assertEqual(settings.lastOutputParent, "")
            self.assertEqual(settings.s1Limit, 2000000)
            self.assertEqual(settings.steadyEmissionY, "S1c")
            self.assertEqual(len(notices), 1)
            self.assertEqual(notices[0].severity, "conspicuous")
            self.assertIn("损坏", notices[0].message)

            self.assertEqual([], store.discard_damaged_file())
            self.assertFalse(path.exists())

    def test_non_object_json_settings_wait_for_notice_before_deletion(self):
        from spectrum_organizer.settings import SettingsStore

        for content in ("[]", '"bad"'):
            with self.subTest(content=content):
                with workspace_tempdir() as tmp:
                    path = pathlib.Path(tmp) / "settings.json"
                    path.write_text(content, encoding="utf-8")
                    store = SettingsStore(path)

                    settings, notices = store.load()

                    self.assertTrue(path.exists())
                    self.assertEqual(settings.lastOutputParent, "")
                    self.assertEqual(settings.s1Limit, 2000000)
                    self.assertEqual(settings.steadyEmissionY, "S1c")
                    self.assertEqual(len(notices), 1)
                    self.assertEqual(notices[0].severity, "conspicuous")
                    store.discard_damaged_file()
                    self.assertFalse(path.exists())

    def test_incompatible_settings_object_waits_for_notice_before_deletion(self):
        from spectrum_organizer.settings import SettingsStore

        cases = [
            {"lastOutputParent": "C:/Out", "s1Limit": 1000000},
            {"lastOutputParent": "C:/Out", "s1Limit": 1000000, "steadyEmissionY": "S1c", "extra": True},
            {"lastOutputParent": "C:/Out", "s1Limit": "1000000", "steadyEmissionY": "S1c"},
            {"lastOutputParent": "C:/Out", "s1Limit": 0, "steadyEmissionY": "S1c"},
            {"lastOutputParent": "C:/Out", "s1Limit": -1, "steadyEmissionY": "S1c"},
            {"lastOutputParent": "C:/Out", "s1Limit": 1000000, "steadyEmissionY": "S1"},
            {"lastOutputParent": 42, "s1Limit": 1000000, "steadyEmissionY": "S1c"},
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                with workspace_tempdir() as tmp:
                    path = pathlib.Path(tmp) / "settings.json"
                    path.write_text(json.dumps(payload), encoding="utf-8")
                    store = SettingsStore(path)

                    settings, notices = store.load()

                    self.assertTrue(path.exists())
                    self.assertEqual(settings.lastOutputParent, "")
                    self.assertEqual(settings.s1Limit, 2000000)
                    self.assertEqual(settings.steadyEmissionY, "S1c")
                    self.assertEqual(len(notices), 1)
                    self.assertEqual(notices[0].severity, "conspicuous")
                    store.discard_damaged_file()
                    self.assertFalse(path.exists())

    def test_output_parent_persists_immediately(self):
        from spectrum_organizer.settings import SettingsStore

        with workspace_tempdir() as tmp:
            path = pathlib.Path(tmp) / "settings.json"
            store = SettingsStore(path)

            warnings = store.set_last_output_parent("C:/Output")

            self.assertEqual(warnings, [])
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["lastOutputParent"], "C:/Output")
            self.assertEqual(data["s1Limit"], 2000000)
            self.assertEqual(data["steadyEmissionY"], "S1c")

    def test_runtime_setter_does_not_overwrite_damage_before_user_acknowledges(self):
        from spectrum_organizer.settings import SettingsStore

        with workspace_tempdir() as tmp:
            path = pathlib.Path(tmp) / "settings.json"
            damaged = b"{broken"
            path.write_bytes(damaged)
            store = SettingsStore(path)

            notices = store.set_last_output_parent("C:/Output")

            self.assertEqual(1, len(notices))
            self.assertEqual("conspicuous", notices[0].severity)
            self.assertEqual(damaged, path.read_bytes())

    def test_acknowledgement_does_not_delete_a_replaced_settings_file(self):
        from spectrum_organizer.settings import SettingsStore

        with workspace_tempdir() as tmp:
            path = pathlib.Path(tmp) / "settings.json"
            path.write_text("{broken", encoding="utf-8")
            store = SettingsStore(path)
            _settings, notices = store.load()
            self.assertEqual("conspicuous", notices[0].severity)
            replacement = {
                "lastOutputParent": "C:/Replacement",
                "s1Limit": 42,
                "steadyEmissionY": "S1c/R1c",
            }
            path.write_text(json.dumps(replacement), encoding="utf-8")

            warnings = store.discard_damaged_file()

            self.assertTrue(path.exists())
            self.assertEqual(replacement, json.loads(path.read_text(encoding="utf-8")))
            self.assertEqual(1, len(warnings))
            self.assertEqual("warning", warnings[0].severity)

    def test_preflight_settings_persist_immediately(self):
        from spectrum_organizer.settings import SettingsStore

        with workspace_tempdir() as tmp:
            path = pathlib.Path(tmp) / "settings.json"
            store = SettingsStore(path)

            warnings = store.set_preflight_settings(
                s1_limit=42,
                steady_emission_y="S1c/R1c",
                allow_missing_s1=True,
            )

            self.assertEqual(warnings, [])
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["s1Limit"], 42)
            self.assertEqual(data["steadyEmissionY"], "S1c/R1c")
            self.assertTrue(data["allowMissingS1"])

    def test_preflight_settings_reject_invalid_values_before_persisting(self):
        from spectrum_organizer.settings import SettingsStore

        cases = [
            {"s1_limit": 0, "steady_emission_y": "S1c"},
            {"s1_limit": -1, "steady_emission_y": "S1c"},
            {"s1_limit": 1000000, "steady_emission_y": "S1"},
        ]
        for kwargs in cases:
            with self.subTest(kwargs=kwargs):
                with workspace_tempdir() as tmp:
                    path = pathlib.Path(tmp) / "settings.json"
                    store = SettingsStore(path)

                    with self.assertRaises(ValueError):
                        store.set_preflight_settings(**kwargs)

                    self.assertFalse(path.exists())

    def test_save_failure_returns_non_blocking_warning(self):
        from spectrum_organizer.settings import SettingsStore

        with workspace_tempdir() as tmp:
            path = pathlib.Path(tmp) / "settings.json"
            store = SettingsStore(path)

            with mock.patch("os.replace", side_effect=OSError("locked")):
                warnings = store.set_last_output_parent("C:/Output")

            self.assertEqual(len(warnings), 1)
            self.assertEqual(warnings[0].severity, "warning")
            self.assertIn("保存", warnings[0].message)

    def test_atomic_save_failure_preserves_existing_settings_file(self):
        from spectrum_organizer.settings import SettingsStore

        with workspace_tempdir() as tmp:
            path = pathlib.Path(tmp) / "settings.json"
            original = {
                "lastOutputParent": "C:/Original",
                "s1Limit": 2000000,
                "steadyEmissionY": "S1c",
                "allowMissingS1": False,
            }
            path.write_text(json.dumps(original), encoding="utf-8")
            store = SettingsStore(path)

            with mock.patch("os.replace", side_effect=OSError("replace failed")):
                warnings = store.set_last_output_parent("C:/Replacement")

            self.assertEqual(1, len(warnings))
            self.assertEqual(original, json.loads(path.read_text(encoding="utf-8")))

    def test_read_failure_waits_for_conspicuous_notice_before_delete(self):
        from spectrum_organizer.settings import SettingsStore

        with workspace_tempdir() as tmp:
            path = pathlib.Path(tmp) / "settings.json"
            path.write_text(json.dumps({
                "lastOutputParent": "C:/Out",
                "s1Limit": 1000000,
                "steadyEmissionY": "S1c",
            }), encoding="utf-8")
            store = SettingsStore(path)

            with mock.patch("pathlib.Path.read_bytes", side_effect=OSError("locked")):
                settings, notices = store.load()

            self.assertTrue(path.exists())
            self.assertEqual(settings.lastOutputParent, "")
            self.assertEqual(settings.s1Limit, 2000000)
            self.assertEqual(settings.steadyEmissionY, "S1c")
            self.assertEqual(len(notices), 1)
            self.assertEqual(notices[0].severity, "conspicuous")
            self.assertIn("默认", notices[0].message)

            self.assertEqual([], store.discard_damaged_file())
            self.assertFalse(path.exists())

if __name__ == "__main__":
    unittest.main()
