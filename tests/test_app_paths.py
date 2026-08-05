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

class AppPathsTests(unittest.TestCase):
    def test_creates_and_validates_owned_app_directories(self):
        from spectrum_organizer.app_paths import ensure_app_paths

        with workspace_tempdir() as tmp:
            paths = ensure_app_paths(local_appdata=tmp)

            self.assertEqual(paths.root, pathlib.Path(tmp) / "Spectrum Organizer")
            self.assertTrue(paths.data.is_dir())
            self.assertTrue(paths.backups.is_dir())
            self.assertTrue(paths.temp.is_dir())
            self.assertTrue(paths.logs.is_dir())
            self.assertEqual(paths.settings_file, paths.root / "settings.json")
            marker = json.loads((paths.root / ".spectrum_organizer_owner.json").read_text())
            self.assertEqual(marker["app"], "Spectrum Organizer")
            self.assertEqual(marker["kind"], "app-state")

    def test_missing_local_appdata_raises_without_fallback(self):
        from spectrum_organizer.app_paths import AppPathError, ensure_app_paths

        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(AppPathError, "LOCALAPPDATA"):
                ensure_app_paths()

    def test_mismatched_ownership_marker_blocks_use(self):
        from spectrum_organizer.app_paths import AppPathError, ensure_app_paths

        with workspace_tempdir() as tmp:
            root = pathlib.Path(tmp) / "Spectrum Organizer"
            root.mkdir()
            (root / ".spectrum_organizer_owner.json").write_text(
                json.dumps({"app": "Other", "kind": "app-state"}), encoding="utf-8"
            )

            with self.assertRaisesRegex(AppPathError, "ownership"):
                ensure_app_paths(local_appdata=tmp)

    def test_directory_creation_failure_is_reported(self):
        from spectrum_organizer.app_paths import AppPathError, ensure_app_paths

        with workspace_tempdir() as tmp:
            root = pathlib.Path(tmp) / "Spectrum Organizer"
            root.write_text("not a directory", encoding="utf-8")

            with self.assertRaisesRegex(AppPathError, "directory"):
                ensure_app_paths(local_appdata=tmp)

    def test_marker_write_failure_is_reported_as_app_path_error(self):
        from spectrum_organizer.app_paths import AppPathError, ensure_app_paths

        with workspace_tempdir() as tmp:
            with mock.patch("pathlib.Path.write_text", side_effect=OSError("denied")):
                with self.assertRaisesRegex(AppPathError, "ownership marker"):
                    ensure_app_paths(local_appdata=tmp)

    def test_required_directories_have_ownership_markers(self):
        from spectrum_organizer.app_paths import APP_MARKER, ensure_app_paths

        with workspace_tempdir() as tmp:
            paths = ensure_app_paths(local_appdata=tmp)

            for directory in (paths.root, paths.data, paths.backups, paths.temp, paths.logs):
                marker = directory / APP_MARKER
                self.assertTrue(marker.is_file(), f"missing marker for {directory}")
                payload = json.loads(marker.read_text(encoding="utf-8"))
                self.assertEqual(payload["app"], "Spectrum Organizer")
                self.assertEqual(payload["kind"], "app-state")

    def test_mismatched_required_directory_marker_blocks_use(self):
        from spectrum_organizer.app_paths import APP_MARKER, AppPathError, ensure_app_paths

        with workspace_tempdir() as tmp:
            paths = ensure_app_paths(local_appdata=tmp)
            (paths.data / APP_MARKER).write_text(
                json.dumps({"app": "Other", "kind": "app-state"}), encoding="utf-8"
            )

            with self.assertRaisesRegex(AppPathError, "ownership"):
                ensure_app_paths(local_appdata=tmp)

if __name__ == "__main__":
    unittest.main()
