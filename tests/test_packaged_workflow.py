import json
import pathlib
import sys
import tempfile
import unittest
from io import StringIO
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import validation.packaged_workflow as packaged_workflow_module
from validation.packaged_workflow import packaged_workflow_main, run_packaged_non_origin_workflow


class PackagedWorkflowTests(unittest.TestCase):
    def test_validation_workflow_requires_explicit_isolated_appdata(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            source = root / "raw.opju"
            source.write_bytes(b"raw")

            with (
                mock.patch.object(
                    packaged_workflow_module,
                    "ensure_app_paths",
                    side_effect=AssertionError("real appdata resolution attempted"),
                ) as ensure_app_paths,
                self.assertRaisesRegex(ValueError, "local_appdata must be explicit"),
            ):
                run_packaged_non_origin_workflow(
                    (source,),
                    root / "out",
                    timestamp="20260718_120000",
                )

            ensure_app_paths.assert_not_called()

    def test_packaged_non_origin_workflow_uses_selected_sources_and_output_parent(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            appdata = root / "localappdata"
            source_a = root / "inputs" / "raw-a.opju"
            source_b = root / "inputs" / "raw-b.OPJ"
            source_a.parent.mkdir()
            source_a.write_bytes(b"source-a")
            source_b.write_bytes(b"source-b")
            output_parent = root / "chosen-output"

            summary = run_packaged_non_origin_workflow(
                (source_a, source_b),
                output_parent,
                local_appdata=appdata,
                timestamp="20260705_120000",
            )

            self.assertEqual((str(source_a), str(source_b)), summary.selected_source_paths)
            self.assertEqual(str(output_parent), summary.output_parent)
            self.assertEqual("completion", summary.final_stage)
            self.assertTrue(pathlib.Path(summary.project_path).is_file())
            self.assertTrue(pathlib.Path(summary.report_path).is_file())
            self.assertTrue(pathlib.Path(summary.summary_file).is_file())
            self.assertEqual(b"source-a", source_a.read_bytes())
            self.assertEqual(b"source-b", source_b.read_bytes())
            settings = json.loads((appdata / "Spectrum Organizer" / "settings.json").read_text(encoding="utf-8"))
            self.assertEqual(str(output_parent), settings["lastOutputParent"])
            self.assertEqual(2000000, settings["s1Limit"])
            self.assertEqual("S1c", settings["steadyEmissionY"])

    def test_packaged_workflow_library_uses_owned_application_temp_root(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            appdata = root / "localappdata"
            source = root / "raw.opju"
            source.write_bytes(b"raw")
            real_library = packaged_workflow_module.SampleLibrary
            created = []

            def make_library(*args, **kwargs):
                library = real_library(*args, **kwargs)
                created.append(library)
                return library

            with mock.patch.object(
                packaged_workflow_module,
                "SampleLibrary",
                side_effect=make_library,
            ):
                run_packaged_non_origin_workflow(
                    (source,),
                    root / "out",
                    local_appdata=appdata,
                    timestamp="20260716_230000",
                )

            self.assertEqual(
                appdata / "Spectrum Organizer" / "temp",
                created[0].health_temp_root,
            )

    def test_packaged_workflow_rejects_unrecognized_source_before_output(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            bad_source = root / "input.txt"
            bad_source.write_text("not origin", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Source selection failed"):
                run_packaged_non_origin_workflow(
                    (bad_source,),
                    root / "chosen-output",
                    local_appdata=root / "localappdata",
                    timestamp="20260705_120001",
                )
            self.assertFalse((root / "chosen-output").exists())

    def test_packaged_workflow_cli_writes_summary_to_stream(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            source = root / "raw.opju"
            source.write_bytes(b"raw")
            stream = StringIO()

            code = packaged_workflow_main(
                ["--source", str(source), "--output-parent", str(root / "out"), "--timestamp", "20260705_120002"],
                local_appdata=root / "localappdata",
                output=stream,
            )

            self.assertEqual(0, code)
            payload = json.loads(stream.getvalue())
            self.assertEqual([str(source)], payload["selected_source_paths"])
            self.assertEqual("completion", payload["final_stage"])


    def test_product_dry_run_module_is_not_test_helper(self):
        text = (ROOT / "src" / "spectrum_organizer" / "dry_run.py").read_text(encoding="utf-8")

        self.assertNotIn("tests.", text)
        self.assertNotIn("TEST-ONLY", text)


if __name__ == "__main__":
    unittest.main()
