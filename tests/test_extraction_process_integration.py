import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from spectrum_organizer.safety.owned_paths import add_allowed_child, create_run_ownership


class ExtractionProcessIntegrationTests(unittest.TestCase):
    def test_real_python_module_process_reports_invalid_manifest_without_origin(self):
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            ownership = create_run_ownership(base, "run-1", "marker-1", [])
            missing_manifest = ownership.temp_root / "missing.json"
            result_path = ownership.temp_root / "result.json"
            ownership = add_allowed_child(ownership, missing_manifest)
            ownership = add_allowed_child(ownership, result_path)
            add_allowed_child(
                ownership,
                result_path.with_name(f"{result_path.name}.pending"),
            )
            environment = dict(os.environ)
            environment["PYTHONPATH"] = str(SRC)

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "spectrum_organizer.origin.extraction_process",
                    "0" * 64,
                    str(missing_manifest),
                    str(result_path),
                ],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
                check=False,
            )

            self.assertEqual(1, completed.returncode)
            payload = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertFalse(payload["ok"])
            self.assertIn("error_type", payload)


if __name__ == "__main__":
    unittest.main()
