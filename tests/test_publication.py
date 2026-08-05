import hashlib
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from spectrum_organizer.reporting.publication import (
    ParentUnavailableError,
    PublicationError,
    PublicationCollisionError,
    cleanup_owned_staging,
    create_run_staging,
    publish_completed_run as _publish_completed_run,
    register_staging_artifact_identity,
    remove_run_owned_artifact,
    reserve_staging_artifact_identity,
    retry_post_commit_cleanup,
    write_failure_log,
)
from spectrum_organizer.safety import identity_paths
from spectrum_organizer.safety.identity_paths import (
    IdentityPathError,
    path_identity,
    unlink_owned_path,
)


def publish_completed_run(targets, report_text, *args, **kwargs):
    if (
        not args
        and "verifier_result" not in kwargs
        and "verified_project_identity" not in kwargs
    ):
        status = targets.staging_project_path.stat()
        kwargs["verified_project_identity"] = (
            status.st_dev,
            status.st_ino,
        )
        kwargs["verified_project_sha256"] = hashlib.sha256(
            targets.staging_project_path.read_bytes()
        ).hexdigest()
    return _publish_completed_run(targets, report_text, *args, **kwargs)


class PublicationTests(unittest.TestCase):
    def test_real_artifact_reservation_can_retry_after_owned_cleanup(self):
        with tempfile.TemporaryDirectory() as temp:
            targets = create_run_staging(
                pathlib.Path(temp),
                "20260629_123456",
                run_id="run-a",
            )

            for artifact in (
                targets.staging_project_path,
                targets.verifier_mutation_path,
            ):
                with self.subTest(artifact=artifact.name):
                    first = reserve_staging_artifact_identity(
                        targets,
                        artifact,
                        run_id="run-a",
                    )
                    self.assertTrue(
                        remove_run_owned_artifact(
                            targets,
                            artifact,
                            run_id="run-a",
                            expected_identity=first,
                        )
                    )
                    second = reserve_staging_artifact_identity(
                        targets,
                        artifact,
                        run_id="run-a",
                    )

                    self.assertNotEqual(first, second)
                    self.assertEqual(second, path_identity(artifact))
                    self.assertTrue(
                        remove_run_owned_artifact(
                            targets,
                            artifact,
                            run_id="run-a",
                            expected_identity=second,
                        )
                    )

    def test_cleanup_refuses_self_consistent_replacement_staging_and_marker(self):
        with tempfile.TemporaryDirectory() as temp:
            targets = create_run_staging(
                pathlib.Path(temp),
                "20260629_123456",
                run_id="run-a",
            )
            marker = targets.staging_dir.with_name(
                f"{targets.staging_dir.name}.ownership.json"
            )
            targets.staging_dir.rename(targets.staging_dir.with_name("parked-staging"))
            marker.rename(marker.with_name("parked-marker.json"))
            targets.staging_dir.mkdir()
            targets.staging_project_path.write_text(
                "FOREIGN USER CONTENT",
                encoding="utf-8",
            )
            staging_identity = path_identity(targets.staging_dir)
            artifact_identity = path_identity(targets.staging_project_path)
            with marker.open("x", encoding="utf-8") as stream:
                marker_status = os.fstat(stream.fileno())
                marker_identity = (marker_status.st_dev, marker_status.st_ino)
                stream.write(
                    json.dumps(
                        {
                            "run_id": targets.run_id,
                            "timestamp": targets.timestamp,
                            "final_run_dir": str(targets.final_run_dir),
                            "project_name": targets.staging_project_path.name,
                            "verifier_mutation_name": targets.verifier_mutation_path.name,
                            "report_name": targets.staging_report_path.name,
                            "staging_identity": list(staging_identity),
                            "marker_identity": list(marker_identity),
                        },
                        sort_keys=True,
                    )
                    + "\n"
                )
                stream.write(
                    json.dumps(
                        {
                            "artifact_name": targets.staging_project_path.name,
                            "identity": list(artifact_identity),
                        },
                        sort_keys=True,
                    )
                    + "\n"
                )

            result = cleanup_owned_staging((targets,), run_id="run-a")

            self.assertEqual((), result.deleted)
            self.assertIn(targets.staging_dir, result.retained_unknown)
            self.assertEqual(
                "FOREIGN USER CONTENT",
                targets.staging_project_path.read_text(encoding="utf-8"),
            )

    def test_cleanup_refuses_artifact_identity_added_only_to_mutable_marker(self):
        with tempfile.TemporaryDirectory() as temp:
            targets = create_run_staging(
                pathlib.Path(temp),
                "20260629_123456",
                run_id="run-a",
            )
            owned_identity = reserve_staging_artifact_identity(
                targets,
                targets.staging_project_path,
                run_id="run-a",
            )
            self.assertTrue(
                remove_run_owned_artifact(
                    targets,
                    targets.staging_project_path,
                    run_id="run-a",
                    expected_identity=owned_identity,
                )
            )
            targets.staging_project_path.write_text(
                "FOREIGN USER CONTENT",
                encoding="utf-8",
            )
            foreign_identity = path_identity(targets.staging_project_path)
            marker = targets.staging_dir.with_name(
                f"{targets.staging_dir.name}.ownership.json"
            )
            with marker.open("a", encoding="utf-8") as stream:
                stream.write(
                    json.dumps(
                        {
                            "artifact_name": targets.staging_project_path.name,
                            "identity": list(foreign_identity),
                        },
                        sort_keys=True,
                    )
                    + "\n"
                )

            result = cleanup_owned_staging((targets,), run_id="run-a")

            self.assertEqual((), result.deleted)
            self.assertIn(targets.staging_project_path, result.retained_unknown)
            self.assertEqual(
                "FOREIGN USER CONTENT",
                targets.staging_project_path.read_text(encoding="utf-8"),
            )

    def test_staging_creation_does_not_adopt_same_path_replacement(self):
        with tempfile.TemporaryDirectory() as temp:
            parent = pathlib.Path(temp)
            target = parent / ".SpectrumOrganizer_staging_20260629_123456_run-a"
            parked = parent / "parked-created-staging"
            real_stat = pathlib.Path.stat
            attacked = False

            def replace_before_first_identity_read(path, *args, **kwargs):
                nonlocal attacked
                path = pathlib.Path(path)
                if path == target and os.path.lexists(path) and not attacked:
                    attacked = True
                    path.rename(parked)
                    path.mkdir()
                return real_stat(path, *args, **kwargs)

            with mock.patch.object(
                pathlib.Path,
                "stat",
                autospec=True,
                side_effect=replace_before_first_identity_read,
            ):
                targets = create_run_staging(
                    parent,
                    "20260629_123456",
                    run_id="run-a",
                )

            self.assertFalse(attacked)
            self.assertEqual(target, targets.staging_dir)
            self.assertFalse(parked.exists())

    def test_staging_uses_exact_project_filename_and_does_not_create_final_run_dir(self):
        with tempfile.TemporaryDirectory() as temp:
            parent = pathlib.Path(temp)

            targets = create_run_staging(parent, "20260629_123456", run_id="run-a")

            self.assertTrue(targets.staging_dir.is_dir())
            self.assertEqual(parent / "Organized_Origin_Data_20260629_123456", targets.final_run_dir)
            self.assertEqual(targets.staging_dir / "Organized_Spectra_20260629_123456.opju", targets.staging_project_path)
            self.assertEqual(
                targets.staging_dir / "Verifier_Mutation_20260629_123456.opju",
                targets.verifier_mutation_path,
            )
            self.assertFalse(targets.final_run_dir.exists())

    def test_cleanup_accepts_only_the_registered_verifier_mutation_copy(self):
        with tempfile.TemporaryDirectory() as temp:
            targets = create_run_staging(
                pathlib.Path(temp),
                "20260629_123456",
                run_id="run-a",
            )
            targets.verifier_mutation_path.write_text("mutation", encoding="utf-8")
            register_staging_artifact_identity(
                targets,
                targets.verifier_mutation_path,
                run_id="run-a",
                expected_identity=path_identity(targets.verifier_mutation_path),
            )

            result = cleanup_owned_staging((targets,), run_id="run-a")

            self.assertEqual((targets.staging_dir,), result.deleted)
            self.assertEqual((), result.retained_unknown)

    def test_cleanup_retry_reclaims_owned_orphan_marker_after_unlink_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            targets = create_run_staging(
                pathlib.Path(temp),
                "20260629_123456",
                run_id="run-a",
            )
            marker = targets.staging_dir.with_name(
                f"{targets.staging_dir.name}.ownership.json"
            )
            original_unlink = identity_paths._unlink_held_file

            def fail_marker_once(path, expected_identity):
                if (
                    path.name.startswith(".SpectrumOrganizer_cleanup_")
                    and path.name.endswith(marker.name)
                ):
                    raise PermissionError("marker temporarily locked")
                return original_unlink(path, expected_identity)

            with mock.patch.object(
                identity_paths,
                "_unlink_held_file",
                side_effect=fail_marker_once,
            ):
                with self.assertRaises(IdentityPathError):
                    cleanup_owned_staging(
                        (targets,),
                        run_id="run-a",
                    )

            self.assertFalse(targets.staging_dir.exists())
            self.assertTrue(marker.exists())

            result = cleanup_owned_staging(
                (targets,),
                run_id="run-a",
            )

            self.assertEqual((marker,), result.deleted)
            self.assertEqual((), result.retained_unknown)
            self.assertFalse(marker.exists())

    def test_cleanup_does_not_remove_orphan_marker_owned_by_another_run(self):
        with tempfile.TemporaryDirectory() as temp:
            targets = create_run_staging(
                pathlib.Path(temp),
                "20260629_123456",
                run_id="run-a",
            )
            marker = targets.staging_dir.with_name(
                f"{targets.staging_dir.name}.ownership.json"
            )
            targets.staging_dir.rmdir()

            result = cleanup_owned_staging(
                (targets.staging_dir,),
                run_id="run-b",
            )

            self.assertEqual((), result.deleted)
            self.assertEqual((marker,), result.retained_unknown)
            self.assertTrue(marker.exists())

    def test_retry_removes_only_a_registered_artifact_from_owned_staging(self):
        with tempfile.TemporaryDirectory() as temp:
            targets = create_run_staging(
                pathlib.Path(temp),
                "20260629_123456",
                run_id="run-a",
            )
            targets.staging_project_path.write_text("partial", encoding="utf-8")
            from spectrum_organizer.safety.identity_paths import path_identity
            project_identity = path_identity(targets.staging_project_path)
            outside = pathlib.Path(temp) / "outside.opju"
            outside.write_text("user", encoding="utf-8")

            self.assertTrue(
                remove_run_owned_artifact(
                    targets,
                    targets.staging_project_path,
                    run_id="run-a",
                    expected_identity=project_identity,
                )
            )
            with self.assertRaises(PublicationError):
                remove_run_owned_artifact(
                    targets,
                    outside,
                    run_id="run-a",
                    expected_identity=None,
                )

            self.assertFalse(targets.staging_project_path.exists())
            self.assertEqual("user", outside.read_text(encoding="utf-8"))

    def test_retry_cleanup_refuses_registered_artifact_replacement(self):
        from spectrum_organizer.safety.identity_paths import path_identity

        with tempfile.TemporaryDirectory() as temp:
            targets = create_run_staging(
                pathlib.Path(temp),
                "20260629_123456",
                run_id="run-a",
            )
            targets.staging_project_path.write_text("owned", encoding="utf-8")
            owned_identity = path_identity(targets.staging_project_path)
            parked = targets.staging_dir / "parked-owned.opju"
            targets.staging_project_path.rename(parked)
            targets.staging_project_path.write_text("FOREIGN", encoding="utf-8")

            with self.assertRaises(PublicationError):
                remove_run_owned_artifact(
                    targets,
                    targets.staging_project_path,
                    run_id="run-a",
                    expected_identity=owned_identity,
                )

            self.assertEqual(
                "FOREIGN",
                targets.staging_project_path.read_text(encoding="utf-8"),
            )
            self.assertEqual("owned", parked.read_text(encoding="utf-8"))

    def test_publish_writes_report_then_atomically_renames_staging_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            targets = create_run_staging(pathlib.Path(temp), "20260629_123456", run_id="run-a")
            targets.staging_project_path.write_text("project", encoding="utf-8")

            summary = publish_completed_run(targets, "success report")

            self.assertFalse(targets.staging_dir.exists())
            self.assertTrue(targets.final_run_dir.is_dir())
            self.assertEqual("project", summary.project_path.read_text(encoding="utf-8"))
            self.assertEqual("success report", summary.report_path.read_text(encoding="utf-8"))
            self.assertEqual(1, summary.project_count)
            self.assertEqual(summary.report_path, targets.final_report_path)
            self.assertFalse(
                targets.staging_dir.with_name(
                    f"{targets.staging_dir.name}.ownership.json"
                ).exists()
            )

    def test_publish_refuses_report_replacement_after_creation_handle_closes(self):
        from spectrum_organizer.reporting import publication

        with tempfile.TemporaryDirectory() as temp:
            targets = create_run_staging(
                pathlib.Path(temp),
                "20260629_123456",
                run_id="run-a",
            )
            targets.staging_project_path.write_text(
                "project",
                encoding="utf-8",
            )
            parked = pathlib.Path(temp) / "parked-owned-report.txt"
            original_write = publication._write_text_exclusive

            def write_then_replace(path, text):
                artifact = original_write(path, text)
                pathlib.Path(path).rename(parked)
                pathlib.Path(path).write_text(
                    "FOREIGN REPORT",
                    encoding="utf-8",
                )
                return artifact

            with mock.patch.object(
                publication,
                "_write_text_exclusive",
                side_effect=write_then_replace,
            ), self.assertRaises(PublicationError):
                publish_completed_run(targets, "owned report")

            self.assertEqual("owned report", parked.read_text(encoding="utf-8"))
            self.assertEqual(
                "FOREIGN REPORT",
                targets.staging_report_path.read_text(encoding="utf-8"),
            )
            self.assertFalse(targets.final_run_dir.exists())

    def test_publish_rejects_staging_replaced_inside_commit_boundary(self):
        with tempfile.TemporaryDirectory() as temp:
            parent = pathlib.Path(temp)
            targets = create_run_staging(
                parent,
                "20260629_123456",
                run_id="run-a",
            )
            targets.staging_project_path.write_text("owned project", encoding="utf-8")
            parked = parent / "parked-owned-staging"

            def replace_then_commit(action):
                targets.staging_dir.rename(parked)
                targets.staging_dir.mkdir()
                targets.staging_project_path.write_text(
                    "foreign project",
                    encoding="utf-8",
                )
                targets.staging_report_path.write_text(
                    "foreign report",
                    encoding="utf-8",
                )
                return action()

            with self.assertRaisesRegex(PublicationError, "identity|committed"):
                publish_completed_run(
                    targets,
                    "owned report",
                    commit=replace_then_commit,
                )

            self.assertFalse(targets.final_run_dir.exists())
            self.assertEqual(
                "foreign project",
                targets.staging_project_path.read_text(encoding="utf-8"),
            )
            self.assertEqual(
                "foreign report",
                targets.staging_report_path.read_text(encoding="utf-8"),
            )
            self.assertEqual(
                "owned project",
                (parked / targets.staging_project_path.name).read_text(
                    encoding="utf-8"
                ),
            )

    def test_publish_rolls_back_when_project_changes_during_commit_rename(self):
        with tempfile.TemporaryDirectory() as temp:
            targets = create_run_staging(
                pathlib.Path(temp),
                "20260629_123456",
                run_id="run-a",
            )
            targets.staging_project_path.write_text(
                "verified project",
                encoding="utf-8",
            )

            def rename_then_tamper(staging_dir, final_run_dir):
                staging_dir.rename(final_run_dir)
                (final_run_dir / targets.staging_project_path.name).write_text(
                    "tampered project",
                    encoding="utf-8",
                )

            with mock.patch(
                "spectrum_organizer.reporting.publication._rename_staging_directory",
                side_effect=rename_then_tamper,
            ), self.assertRaisesRegex(PublicationError, "digest|identity"):
                publish_completed_run(targets, "success report")

            self.assertFalse(targets.final_run_dir.exists())
            self.assertTrue(targets.staging_dir.is_dir())
            self.assertEqual(
                "tampered project",
                targets.staging_project_path.read_text(encoding="utf-8"),
            )

    def test_publish_rejects_in_place_report_change_inside_commit(self):
        with tempfile.TemporaryDirectory() as temp:
            targets = create_run_staging(
                pathlib.Path(temp),
                "20260629_123456",
                run_id="run-a",
            )
            targets.staging_project_path.write_text(
                "verified project",
                encoding="utf-8",
            )

            def tamper_report_then_commit(action):
                targets.staging_report_path.write_text(
                    "FOREIGN REPORT",
                    encoding="utf-8",
                )
                return action()

            with self.assertRaisesRegex(
                PublicationError,
                "report|digest|identity",
            ):
                publish_completed_run(
                    targets,
                    "owned report",
                    commit=tamper_report_then_commit,
                )

            self.assertFalse(targets.final_run_dir.exists())

    def test_publish_requires_the_exact_verified_project_identity_and_digest(self):
        with tempfile.TemporaryDirectory() as temp:
            targets = create_run_staging(
                pathlib.Path(temp),
                "20260629_123456",
                run_id="run-a",
            )
            targets.staging_project_path.write_bytes(b"verified project")
            status = targets.staging_project_path.stat()
            verified_identity = (status.st_dev, status.st_ino)
            verified_digest = hashlib.sha256(b"verified project").hexdigest()
            targets.staging_project_path.write_bytes(b"tampered project")

            with self.assertRaisesRegex(PublicationError, "verified|digest|identity"):
                publish_completed_run(
                    targets,
                    "success report",
                    verified_project_identity=verified_identity,
                    verified_project_sha256=verified_digest,
                )

            self.assertFalse(targets.final_run_dir.exists())

    def test_publish_rechecks_allowed_staging_children_inside_commit(self):
        with tempfile.TemporaryDirectory() as temp:
            targets = create_run_staging(
                pathlib.Path(temp),
                "20260629_123456",
                run_id="run-a",
            )
            targets.staging_project_path.write_bytes(b"verified project")
            status = targets.staging_project_path.stat()
            verified_identity = (status.st_dev, status.st_ino)
            verified_digest = hashlib.sha256(b"verified project").hexdigest()
            foreign = targets.staging_dir / "foreign.bin"

            def add_foreign_child_then_commit(action):
                foreign.write_bytes(b"foreign")
                return action()

            with self.assertRaisesRegex(PublicationError, "Unexpected staging artifact"):
                publish_completed_run(
                    targets,
                    "success report",
                    commit=add_foreign_child_then_commit,
                    verified_project_identity=verified_identity,
                    verified_project_sha256=verified_digest,
                )

            self.assertFalse(targets.final_run_dir.exists())
            self.assertEqual(b"foreign", foreign.read_bytes())

    def test_publish_never_derives_allowlist_from_mutable_marker_names(self):
        from spectrum_organizer.reporting import publication

        with tempfile.TemporaryDirectory() as temp:
            targets = create_run_staging(
                pathlib.Path(temp),
                "20260629_123456",
                run_id="run-a",
            )
            targets.staging_project_path.write_bytes(b"verified project")
            verified_identity = path_identity(targets.staging_project_path)
            verified_digest = hashlib.sha256(b"verified project").hexdigest()
            foreign = targets.staging_dir / "foreign-user-file.txt"
            real_registered_names = publication._registered_staging_names

            def marker_supplied_names(path):
                names = real_registered_names(path)
                if targets.staging_report_path.exists():
                    foreign.write_bytes(b"FOREIGN USER CONTENT")
                    return {
                        targets.staging_project_path.name,
                        targets.staging_report_path.name,
                        foreign.name,
                    }
                return names

            with mock.patch.object(
                publication,
                "_registered_staging_names",
                side_effect=marker_supplied_names,
            ), self.assertRaisesRegex(
                PublicationError,
                "Unexpected staging artifact|marker artifact names changed",
            ):
                publish_completed_run(
                    targets,
                    "success report",
                    verified_project_identity=verified_identity,
                    verified_project_sha256=verified_digest,
                )

            self.assertFalse(targets.final_run_dir.exists())
            self.assertEqual(b"FOREIGN USER CONTENT", foreign.read_bytes())

    def test_publish_rejects_late_registered_verifier_mutation_artifact(self):
        with tempfile.TemporaryDirectory() as temp:
            targets = create_run_staging(
                pathlib.Path(temp),
                "20260629_123456",
                run_id="run-a",
            )
            targets.staging_project_path.write_bytes(b"verified project")
            status = targets.staging_project_path.stat()
            verified_identity = (status.st_dev, status.st_ino)
            verified_digest = hashlib.sha256(b"verified project").hexdigest()

            def add_mutation_then_commit(action):
                targets.verifier_mutation_path.write_bytes(b"late mutation")
                return action()

            with self.assertRaisesRegex(
                PublicationError,
                "Unexpected staging artifact|mutation",
            ):
                publish_completed_run(
                    targets,
                    "success report",
                    commit=add_mutation_then_commit,
                    verified_project_identity=verified_identity,
                    verified_project_sha256=verified_digest,
                )

            self.assertFalse(targets.final_run_dir.exists())
            self.assertEqual(
                b"late mutation",
                targets.verifier_mutation_path.read_bytes(),
            )

    def test_publish_never_overwrites_a_preexisting_registered_report(self):
        with tempfile.TemporaryDirectory() as temp:
            targets = create_run_staging(
                pathlib.Path(temp),
                "20260629_123456",
                run_id="run-a",
            )
            targets.staging_project_path.write_text(
                "project",
                encoding="utf-8",
            )
            targets.staging_report_path.write_text(
                "user-owned report",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                PublicationCollisionError,
                "Run_Report_20260629_123456.txt",
            ):
                publish_completed_run(targets, "new report")

            self.assertEqual(
                "user-owned report",
                targets.staging_report_path.read_text(encoding="utf-8"),
            )
            self.assertFalse(targets.final_run_dir.exists())

    def test_staging_marker_is_created_exclusively(self):
        from spectrum_organizer.reporting import publication

        with tempfile.TemporaryDirectory() as temp:
            parent = pathlib.Path(temp)
            marker_path = parent / "marker.json"
            marker_path.write_text("user marker", encoding="utf-8")

            with self.assertRaises(FileExistsError):
                publication._write_text_exclusive(
                    marker_path,
                    "replacement",
                )

            self.assertEqual(
                "user marker",
                marker_path.read_text(encoding="utf-8"),
            )

    def test_publish_refuses_while_verifier_mutation_or_unknown_staging_artifact_remains(self):
        with tempfile.TemporaryDirectory() as temp:
            targets = create_run_staging(
                pathlib.Path(temp),
                "20260629_123456",
                run_id="run-a",
            )
            targets.staging_project_path.write_text("project", encoding="utf-8")
            targets.verifier_mutation_path.write_text("mutation", encoding="utf-8")

            with self.assertRaisesRegex(PublicationError, "Verifier mutation"):
                publish_completed_run(targets, "report")

            targets.verifier_mutation_path.unlink()
            unknown = targets.staging_dir / "unexpected.bin"
            unknown.write_text("unknown", encoding="utf-8")
            with self.assertRaisesRegex(PublicationError, "unexpected.bin"):
                publish_completed_run(targets, "report")

            self.assertFalse(targets.staging_report_path.exists())
            self.assertTrue(targets.staging_project_path.exists())

    def test_target_collision_refuses_without_overwriting_reusing_or_deleting(self):
        with tempfile.TemporaryDirectory() as temp:
            parent = pathlib.Path(temp)
            existing = parent / "Organized_Origin_Data_20260629_123456"
            existing.mkdir()
            sentinel = existing / "keep.txt"
            sentinel.write_text("user output", encoding="utf-8")
            targets = create_run_staging(parent, "20260629_123456", run_id="run-a")
            targets.staging_project_path.write_text("project", encoding="utf-8")

            with self.assertRaisesRegex(PublicationCollisionError, "Organized_Origin_Data_20260629_123456"):
                publish_completed_run(targets, "success report")

            self.assertEqual("user output", sentinel.read_text(encoding="utf-8"))
            self.assertTrue(targets.staging_dir.exists())
            self.assertFalse(targets.staging_report_path.exists())
            self.assertFalse(targets.final_report_path.exists())

    def test_publish_rename_failure_removes_only_success_report_from_staging(self):
        with tempfile.TemporaryDirectory() as temp:
            targets = create_run_staging(pathlib.Path(temp), "20260629_123456", run_id="run-a")
            targets.staging_project_path.write_text("project", encoding="utf-8")

            with (
                mock.patch(
                    "spectrum_organizer.reporting.publication._rename_staging_directory",
                    side_effect=OSError("rename failed"),
                ),
                self.assertRaisesRegex(ParentUnavailableError, "rename failed") as raised,
            ):
                publish_completed_run(targets, "success report")

            self.assertEqual(targets.output_parent, raised.exception.path)
            self.assertTrue(targets.staging_dir.exists())
            self.assertEqual("project", targets.staging_project_path.read_text(encoding="utf-8"))
            self.assertFalse(targets.staging_report_path.exists())
            self.assertFalse(targets.final_run_dir.exists())
            self.assertTrue(
                targets.staging_dir.with_name(
                    f"{targets.staging_dir.name}.ownership.json"
                ).is_file()
            )

    def test_report_write_failure_is_output_parent_unavailable_and_keeps_owned_staging(self):
        with tempfile.TemporaryDirectory() as temp:
            targets = create_run_staging(
                pathlib.Path(temp),
                "20260629_123456",
                run_id="run-a",
            )
            targets.staging_project_path.write_text("project", encoding="utf-8")

            with mock.patch(
                "spectrum_organizer.reporting.publication._write_text_exclusive",
                side_effect=PermissionError("report write blocked"),
            ), self.assertRaises(ParentUnavailableError) as raised:
                publish_completed_run(targets, "success report")

            self.assertEqual(targets.output_parent, raised.exception.path)
            self.assertTrue(targets.staging_dir.is_dir())
            self.assertTrue(
                targets.staging_dir.with_name(
                    f"{targets.staging_dir.name}.ownership.json"
                ).is_file()
            )

    def test_marker_cleanup_failure_keeps_committed_success_with_warning(self):
        with tempfile.TemporaryDirectory() as temp:
            targets = create_run_staging(
                pathlib.Path(temp),
                "20260629_123456",
                run_id="run-a",
            )
            targets.staging_project_path.write_text("project", encoding="utf-8")

            marker = targets.staging_dir.with_name(
                f"{targets.staging_dir.name}.ownership.json"
            )
            with mock.patch.object(
                identity_paths,
                "_unlink_held_file",
                side_effect=OSError("marker cleanup failed"),
            ):
                summary = publish_completed_run(targets, "success report")

            self.assertFalse(targets.staging_dir.exists())
            self.assertEqual(
                {targets.final_project_path, targets.final_report_path},
                set(targets.final_run_dir.iterdir()),
            )
            self.assertTrue(marker.is_file())
            self.assertIsNotNone(summary.post_commit_error)
            self.assertIn(str(marker), str(summary.post_commit_error))
            self.assertIn(str(targets.final_run_dir), str(summary.post_commit_error))

            retry_post_commit_cleanup(summary)
            self.assertFalse(marker.exists())

    def test_post_commit_identity_probe_failure_keeps_committed_success(self):
        from spectrum_organizer.reporting import publication

        with tempfile.TemporaryDirectory() as temp:
            targets = create_run_staging(
                pathlib.Path(temp),
                "20260629_123456",
                run_id="run-a",
            )
            targets.staging_project_path.write_text(
                "project",
                encoding="utf-8",
            )

            real_identity = publication._path_identity
            marker = targets.staging_dir.with_name(
                f"{targets.staging_dir.name}.ownership.json"
            )

            def fail_after_commit(path):
                if targets.final_run_dir.exists() and pathlib.Path(path) == marker:
                    raise PublicationError("identity temporarily unreadable")
                return real_identity(path)

            with mock.patch(
                "spectrum_organizer.reporting.publication._path_identity",
                side_effect=fail_after_commit,
            ):
                summary = publish_completed_run(targets, "success report")

            self.assertTrue(targets.final_project_path.is_file())
            self.assertTrue(targets.final_report_path.is_file())
            self.assertIsNotNone(summary.post_commit_error)
            self.assertIn(
                "identity temporarily unreadable",
                str(summary.post_commit_error),
            )

    def test_publish_refuses_replaced_marker_even_when_payload_is_copied(self):
        with tempfile.TemporaryDirectory() as temp:
            targets = create_run_staging(
                pathlib.Path(temp),
                "20260629_123456",
                run_id="run-a",
            )
            targets.staging_project_path.write_text(
                "project",
                encoding="utf-8",
            )
            marker = targets.staging_dir.with_name(
                f"{targets.staging_dir.name}.ownership.json"
            )
            payload = marker.read_text(encoding="utf-8")
            marker.unlink()
            marker.write_text(payload, encoding="utf-8")

            with self.assertRaisesRegex(
                PublicationError,
                "identity|ownership|marker",
            ):
                publish_completed_run(targets, "success report")

            self.assertTrue(marker.is_file())
            self.assertTrue(targets.staging_dir.is_dir())
            self.assertFalse(targets.final_run_dir.exists())

    def test_cleanup_refuses_replaced_staging_even_when_marker_is_copied(self):
        with tempfile.TemporaryDirectory() as temp:
            parent = pathlib.Path(temp)
            targets = create_run_staging(
                parent,
                "20260629_123456",
                run_id="run-a",
            )
            parked = parent / "parked-original-staging"
            targets.staging_dir.rename(parked)
            targets.staging_dir.mkdir()
            foreign = targets.staging_dir / targets.staging_project_path.name
            foreign.write_text("foreign", encoding="utf-8")

            result = cleanup_owned_staging(
                (targets,),
                run_id="run-a",
            )

            self.assertTrue(targets.staging_dir.is_dir())
            self.assertEqual("foreign", foreign.read_text(encoding="utf-8"))
            self.assertEqual((), result.deleted)
            self.assertIn(targets.staging_dir, result.retained_unknown)

    def test_post_commit_retry_refuses_replaced_foreign_marker(self):
        with tempfile.TemporaryDirectory() as temp:
            targets = create_run_staging(
                pathlib.Path(temp),
                "20260629_123456",
                run_id="run-a",
            )
            targets.staging_project_path.write_text(
                "project",
                encoding="utf-8",
            )
            marker = targets.staging_dir.with_name(
                f"{targets.staging_dir.name}.ownership.json"
            )
            with mock.patch.object(
                identity_paths,
                "_unlink_held_file",
                side_effect=OSError("marker cleanup failed"),
            ):
                summary = publish_completed_run(targets, "success report")

            marker.unlink()
            marker.write_text("FOREIGN USER CONTENT", encoding="utf-8")

            with self.assertRaisesRegex(
                PublicationError,
                "ownership|identity|marker",
            ):
                retry_post_commit_cleanup(summary)

            self.assertEqual(
                "FOREIGN USER CONTENT",
                marker.read_text(encoding="utf-8"),
            )

    def test_post_commit_retry_isolates_marker_before_unlink(self):
        with tempfile.TemporaryDirectory() as temp:
            parent = pathlib.Path(temp)
            targets = create_run_staging(
                parent,
                "20260629_123456",
                run_id="run-a",
            )
            targets.staging_project_path.write_text("project", encoding="utf-8")
            marker = targets.staging_dir.with_name(
                f"{targets.staging_dir.name}.ownership.json"
            )
            with mock.patch.object(
                identity_paths,
                "_unlink_held_file",
                side_effect=OSError("marker cleanup failed"),
            ):
                summary = publish_completed_run(targets, "success report")

            original_unlink = pathlib.Path.unlink
            injected = False
            parked = parent / "parked-owned-marker.json"

            def replace_if_direct_unlink(path, *args, **kwargs):
                nonlocal injected
                if path == marker:
                    injected = True
                    marker.rename(parked)
                    marker.write_text("FOREIGN USER CONTENT", encoding="utf-8")
                return original_unlink(path, *args, **kwargs)

            with mock.patch.object(
                pathlib.Path,
                "unlink",
                autospec=True,
                side_effect=replace_if_direct_unlink,
            ):
                retry_post_commit_cleanup(summary)

            self.assertFalse(injected)
            self.assertFalse(marker.exists())

    def test_target_collision_after_report_write_refuses_and_removes_success_report(self):
        with tempfile.TemporaryDirectory() as temp:
            parent = pathlib.Path(temp)
            targets = create_run_staging(parent, "20260629_123456", run_id="run-a")
            targets.staging_project_path.write_text("project", encoding="utf-8")
            original_require_absent = __import__(
                "spectrum_organizer.reporting.publication",
                fromlist=("_require_absent",),
            )._require_absent
            sentinel = targets.final_run_dir / "keep.txt"

            def collide_after_report_write(path):
                if pathlib.Path(path) == targets.final_run_dir and targets.staging_report_path.exists():
                    targets.final_run_dir.mkdir()
                    sentinel.write_text("user output", encoding="utf-8")
                return original_require_absent(path)

            with mock.patch(
                "spectrum_organizer.reporting.publication._require_absent",
                side_effect=collide_after_report_write,
            ), self.assertRaisesRegex(
                PublicationCollisionError,
                "Organized_Origin_Data_20260629_123456",
            ):
                publish_completed_run(targets, "success report")

            self.assertEqual("user output", sentinel.read_text(encoding="utf-8"))
            self.assertTrue(targets.staging_dir.exists())
            self.assertFalse(targets.staging_report_path.exists())
            self.assertFalse(targets.final_report_path.exists())

    def test_report_cleanup_failure_does_not_mask_original_publish_error(self):
        with tempfile.TemporaryDirectory() as temp:
            targets = create_run_staging(pathlib.Path(temp), "20260629_123456", run_id="run-a")
            targets.staging_project_path.write_text("project", encoding="utf-8")

            with (
                mock.patch(
                    "spectrum_organizer.reporting.publication._rename_staging_directory",
                    side_effect=OSError("rename failed"),
                ),
                mock.patch.object(
                    identity_paths,
                    "_unlink_held_file",
                    side_effect=OSError("unlink failed"),
                ),
                self.assertRaisesRegex(PublicationError, "rename failed") as raised,
            ):
                publish_completed_run(targets, "success report")

            self.assertIsInstance(raised.exception.__cause__, OSError)
            self.assertIn("rename failed", str(raised.exception.__cause__))
            self.assertTrue(targets.staging_report_path.exists())
            self.assertFalse(targets.final_run_dir.exists())

    def test_marker_write_failure_removes_unowned_staging_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            parent = pathlib.Path(temp)

            with (
                mock.patch(
                    "spectrum_organizer.reporting.publication._write_staging_marker",
                    side_effect=OSError("marker failed"),
                ),
                self.assertRaisesRegex(ParentUnavailableError, "marker failed"),
            ):
                create_run_staging(parent, "20260629_123456", run_id="run-a")

            self.assertEqual((), tuple(parent.glob(".SpectrumOrganizer_staging_*")))

    def test_marker_write_failure_isolates_new_staging_before_rmdir(self):
        with tempfile.TemporaryDirectory() as temp:
            parent = pathlib.Path(temp)
            original_rmdir = pathlib.Path.rmdir
            direct_delete_injected = False
            parked = parent / "parked-owned-empty-staging"

            def replace_if_direct_rmdir(path):
                nonlocal direct_delete_injected
                path = pathlib.Path(path)
                if path.name.startswith(".SpectrumOrganizer_staging_"):
                    direct_delete_injected = True
                    path.rename(parked)
                    path.mkdir()
                return original_rmdir(path)

            with (
                mock.patch(
                    "spectrum_organizer.reporting.publication._write_staging_marker",
                    side_effect=OSError("marker failed"),
                ),
                mock.patch.object(
                    pathlib.Path,
                    "rmdir",
                    autospec=True,
                    side_effect=replace_if_direct_rmdir,
                ),
                self.assertRaisesRegex(ParentUnavailableError, "marker failed"),
            ):
                create_run_staging(parent, "20260629_123456", run_id="run-a")

            self.assertFalse(direct_delete_injected)
            self.assertFalse(parked.exists())

    def test_marker_failure_cleanup_does_not_delete_replaced_quarantine(self):
        with tempfile.TemporaryDirectory() as temp:
            parent = pathlib.Path(temp)
            parked = parent / "parked-owned-empty-staging"
            foreign = None
            real_iterdir = pathlib.Path.iterdir

            def replace_after_empty_check(path):
                nonlocal foreign
                path = pathlib.Path(path)
                if path.name.startswith(".SpectrumOrganizer_cleanup_"):
                    def replaced_empty_iterator():
                        nonlocal foreign
                        path.rename(parked)
                        path.mkdir()
                        foreign = path
                        if False:
                            yield path

                    return replaced_empty_iterator()
                return real_iterdir(path)

            with (
                mock.patch(
                    "spectrum_organizer.reporting.publication._write_staging_marker",
                    side_effect=OSError("marker failed"),
                ),
                mock.patch.object(
                    pathlib.Path,
                    "iterdir",
                    autospec=True,
                    side_effect=replace_after_empty_check,
                ),
                self.assertRaisesRegex(ParentUnavailableError, "marker failed"),
            ):
                create_run_staging(
                    parent,
                    "20260629_123456",
                    run_id="run-a",
                )

            self.assertIsNotNone(foreign)
            self.assertTrue(foreign.is_dir())
            self.assertTrue(parked.is_dir())

    def test_marker_write_failure_retains_verified_cleanup_retry_when_rmdir_is_blocked(self):
        with tempfile.TemporaryDirectory() as temp:
            parent = pathlib.Path(temp)
            with (
                mock.patch(
                    "spectrum_organizer.reporting.publication._write_staging_marker",
                    side_effect=OSError("marker failed"),
                ),
                mock.patch(
                    "spectrum_organizer.reporting.publication.remove_empty_owned_directory",
                    side_effect=OSError("directory locked"),
                ),
                self.assertRaises(ParentUnavailableError) as raised,
            ):
                create_run_staging(
                    parent,
                    "20260629_123456",
                    run_id="run-a",
                )

            staging = next(parent.glob(".SpectrumOrganizer_staging_*"))
            self.assertTrue(callable(raised.exception.cleanup_retry))
            raised.exception.cleanup_retry()
            self.assertFalse(staging.exists())

    def test_missing_output_parent_is_recreated_at_staging_time(self):
        with tempfile.TemporaryDirectory() as temp:
            parent = pathlib.Path(temp) / "missing" / "chosen"

            targets = create_run_staging(parent, "20260629_123456", run_id="run-a")

            self.assertTrue(parent.is_dir())
            self.assertTrue(targets.staging_dir.is_dir())

    def test_occupied_output_parent_reports_exact_path_and_allows_retry_elsewhere(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            occupied = root / "chosen"
            occupied.write_text("not a folder", encoding="utf-8")

            with self.assertRaises(ParentUnavailableError) as raised:
                create_run_staging(occupied, "20260629_123456", run_id="run-a")

            self.assertEqual(occupied, raised.exception.path)
            self.assertIn(str(occupied), str(raised.exception))
            retry = create_run_staging(root / "other", "20260629_123456", run_id="same-approved-snapshot")
            self.assertTrue(retry.staging_dir.is_dir())

    def test_failure_log_records_output_and_verifier_retry_attempts_under_local_appdata(self):
        with tempfile.TemporaryDirectory() as temp:
            log_path = write_failure_log(
                "20260629_123456",
                "writer failed",
                local_appdata=pathlib.Path(temp),
                output_attempts=(_attempt(1, "infrastructure_failed", "COM busy"), _attempt(2, "infrastructure_failed", "COM gone")),
                verifier_attempts=(_attempt(1, "infrastructure_failed", "Verifier busy"),),
            )

            self.assertEqual(pathlib.Path(temp) / "Spectrum Organizer" / "logs" / "Failed_Run_20260629_123456.txt", log_path)
            text = log_path.read_text(encoding="utf-8")
            self.assertIn("writer failed", text)
            self.assertIn("失败运行 20260629_123456", text)
            self.assertIn("输出尝试 1: infrastructure_failed - COM busy", text)
            self.assertIn("输出尝试 2: infrastructure_failed - COM gone", text)
            self.assertIn("验证尝试 1: infrastructure_failed - Verifier busy", text)

    def test_failure_log_suffixes_collisions_and_uses_utf8(self):
        with tempfile.TemporaryDirectory() as temp:
            first = write_failure_log("20260629_123456", "first", local_appdata=pathlib.Path(temp))
            second = write_failure_log("20260629_123456", "second cafe \u00b5", local_appdata=pathlib.Path(temp))

            self.assertEqual("Failed_Run_20260629_123456.txt", first.name)
            self.assertEqual("Failed_Run_20260629_123456_001.txt", second.name)
            self.assertEqual("second cafe \u00b5", second.read_text(encoding="utf-8").splitlines()[1])

    def test_failure_log_exclusive_create_does_not_overwrite_a_racing_writer(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            log_dir = root / "Spectrum Organizer" / "logs"
            base = log_dir / "Failed_Run_20260629_123456.txt"
            original_open = pathlib.Path.open
            raced = False

            def racing_open(path, mode="r", *args, **kwargs):
                nonlocal raced
                if path == base and mode == "x" and not raced:
                    raced = True
                    with original_open(path, "x", encoding="utf-8") as stream:
                        stream.write("other writer\n")
                    raise FileExistsError(path)
                return original_open(path, mode, *args, **kwargs)

            with mock.patch.object(pathlib.Path, "open", racing_open):
                result = write_failure_log(
                    "20260629_123456",
                    "this writer",
                    local_appdata=root,
                )

            self.assertEqual("other writer\n", base.read_text(encoding="utf-8"))
            self.assertEqual(
                "Failed_Run_20260629_123456_001.txt",
                result.name,
            )
            self.assertIn(
                "this writer",
                result.read_text(encoding="utf-8"),
            )

    def test_no_success_report_is_created_when_failure_log_is_written(self):
        with tempfile.TemporaryDirectory() as temp:
            targets = create_run_staging(pathlib.Path(temp) / "out", "20260629_123456", run_id="run-a")

            write_failure_log("20260629_123456", "failed before publication", local_appdata=pathlib.Path(temp))

            self.assertFalse(targets.staging_report_path.exists())
            self.assertFalse(targets.final_report_path.exists())

    def test_cleanup_does_not_delete_published_final_output(self):
        with tempfile.TemporaryDirectory() as temp:
            targets = create_run_staging(pathlib.Path(temp), "20260629_123456", run_id="run-a")
            targets.staging_project_path.write_text("project", encoding="utf-8")
            publish_completed_run(targets, "success report")

            result = cleanup_owned_staging((targets.final_run_dir,), run_id="run-a")

            self.assertEqual((), result.deleted)
            self.assertEqual((targets.final_run_dir,), result.retained_unknown)
            self.assertTrue(targets.final_run_dir.exists())
            self.assertTrue(targets.final_project_path.exists())
            self.assertTrue(targets.final_report_path.exists())

    def test_cleanup_removes_only_owned_staging_and_retains_unknown_unowned_objects(self):
        with tempfile.TemporaryDirectory() as temp:
            parent = pathlib.Path(temp)
            owned = create_run_staging(parent, "20260629_123456", run_id="run-a")
            unowned = parent / ".SpectrumOrganizer_staging_20260629_123456_unowned"
            unowned.mkdir()

            result = cleanup_owned_staging((owned, unowned), run_id="run-a")

            self.assertEqual((owned.staging_dir,), result.deleted)
            self.assertEqual((unowned,), result.retained_unknown)
            self.assertFalse(owned.staging_dir.exists())
            self.assertTrue(unowned.exists())

    def test_cleanup_removes_registered_artifacts_without_recursive_delete(self):
        with tempfile.TemporaryDirectory() as temp:
            parent = pathlib.Path(temp)
            targets = create_run_staging(
                parent,
                "20260629_123456",
                run_id="run-a",
            )
            targets.staging_project_path.write_text("owned", encoding="utf-8")
            register_staging_artifact_identity(
                targets,
                targets.staging_project_path,
                run_id="run-a",
                expected_identity=path_identity(targets.staging_project_path),
            )

            result = cleanup_owned_staging(
                (targets,),
                run_id="run-a",
            )

            self.assertEqual((targets.staging_dir,), result.deleted)

    def test_cleanup_never_recursively_deletes_a_replacement_at_the_quarantine_name(self):
        from spectrum_organizer.reporting import publication

        with tempfile.TemporaryDirectory() as temp:
            parent = pathlib.Path(temp)
            targets = create_run_staging(
                parent,
                "20260629_123456",
                run_id="run-a",
            )
            targets.staging_project_path.write_text("owned", encoding="utf-8")
            register_staging_artifact_identity(
                targets,
                targets.staging_project_path,
                run_id="run-a",
                expected_identity=path_identity(targets.staging_project_path),
            )
            original_remove = publication.remove_empty_owned_directory
            parked = parent / "parked-owned-staging"
            replacement = None

            def replace_quarantine_then_delete(path, expected_identity):
                nonlocal replacement
                path = pathlib.Path(path)
                if path.name.startswith(".SpectrumOrganizer_cleanup_"):
                    path.rename(parked)
                    path.mkdir()
                    replacement = path / "foreign.txt"
                    replacement.write_text(
                        "FOREIGN USER CONTENT",
                        encoding="utf-8",
                    )
                return original_remove(path, expected_identity)

            with mock.patch.object(
                publication,
                "remove_empty_owned_directory",
                side_effect=replace_quarantine_then_delete,
            ), self.assertRaises(IdentityPathError):
                cleanup_owned_staging(
                    (targets,),
                    run_id="run-a",
                )

            self.assertIsNotNone(replacement)
            self.assertEqual(
                "FOREIGN USER CONTENT",
                replacement.read_text(encoding="utf-8"),
            )
            self.assertTrue(parked.is_dir())

    def test_cleanup_reports_replacement_at_original_staging_namespace(self):
        from spectrum_organizer.reporting import publication

        with tempfile.TemporaryDirectory() as temp:
            targets = create_run_staging(
                pathlib.Path(temp),
                "20260629_123456",
                run_id="run-a",
            )
            targets.staging_project_path.write_text("owned", encoding="utf-8")
            register_staging_artifact_identity(
                targets,
                targets.staging_project_path,
                run_id="run-a",
                expected_identity=path_identity(targets.staging_project_path),
            )
            original_isolate = publication._isolate_for_cleanup

            def isolate_then_replace(path, expected_identity):
                isolated = original_isolate(path, expected_identity)
                if pathlib.Path(path) == targets.staging_dir:
                    targets.staging_dir.mkdir()
                    (targets.staging_dir / "foreign.txt").write_text(
                        "FOREIGN USER CONTENT",
                        encoding="utf-8",
                    )
                return isolated

            with mock.patch.object(
                publication,
                "_isolate_for_cleanup",
                side_effect=isolate_then_replace,
            ):
                result = cleanup_owned_staging(
                    (targets,),
                    run_id="run-a",
                )

            self.assertEqual((), result.deleted)
            self.assertEqual((targets.staging_dir,), result.retained_unknown)
            self.assertEqual(
                "FOREIGN USER CONTENT",
                (targets.staging_dir / "foreign.txt").read_text(encoding="utf-8"),
            )

    def test_cleanup_retains_dangling_symlink_at_staging_namespace(self):
        from spectrum_organizer.reporting import publication

        with tempfile.TemporaryDirectory() as temp:
            parent = pathlib.Path(temp)
            dangling = parent / ".SpectrumOrganizer_staging_dangling_run-a"
            with mock.patch.object(
                publication,
                "lexical_path_exists",
                return_value=True,
            ):
                result = cleanup_owned_staging((dangling,), run_id="run-a")

            self.assertEqual((), result.deleted)
            self.assertEqual((dangling,), result.retained_unknown)

    @unittest.skipUnless(os.name == "nt", "Windows junction behavior")
    def test_cleanup_retains_dangling_junction_and_its_marker(self):
        import _winapi

        with tempfile.TemporaryDirectory() as temp:
            parent = pathlib.Path(temp)
            targets = create_run_staging(
                parent,
                "20260629_123456",
                run_id="run-a",
            )
            marker = targets.staging_dir.with_name(
                f"{targets.staging_dir.name}.ownership.json"
            )
            parked = parent / "parked-owned-staging"
            targets.staging_dir.rename(parked)
            target = parent / "junction-target"
            target.mkdir()
            _winapi.CreateJunction(str(target), str(targets.staging_dir))
            target.rmdir()
            try:
                result = cleanup_owned_staging((targets,), run_id="run-a")

                self.assertEqual((), result.deleted)
                self.assertIn(targets.staging_dir, result.retained_unknown)
                self.assertTrue(marker.exists())
            finally:
                if os.path.lexists(targets.staging_dir):
                    os.rmdir(targets.staging_dir)

    @unittest.skipUnless(os.name == "nt", "Windows junction behavior")
    def test_cleanup_retains_dangling_final_junction_and_its_marker(self):
        import _winapi

        with tempfile.TemporaryDirectory() as temp:
            parent = pathlib.Path(temp)
            targets = create_run_staging(
                parent,
                "20260629_123456",
                run_id="run-a",
            )
            marker = targets.staging_dir.with_name(
                f"{targets.staging_dir.name}.ownership.json"
            )
            targets.staging_dir.rename(parent / "parked-owned-staging")
            target = parent / "final-junction-target"
            target.mkdir()
            _winapi.CreateJunction(str(target), str(targets.final_run_dir))
            target.rmdir()
            try:
                result = cleanup_owned_staging((targets,), run_id="run-a")

                self.assertEqual((), result.deleted)
                self.assertIn(marker, result.retained_unknown)
                self.assertIn(targets.final_run_dir, result.retained_unknown)
                self.assertTrue(marker.exists())
                self.assertTrue(os.path.lexists(targets.final_run_dir))
            finally:
                if os.path.lexists(targets.final_run_dir):
                    os.rmdir(targets.final_run_dir)

    def test_identity_unlink_never_deletes_replacement_at_quarantine_name(self):
        with tempfile.TemporaryDirectory() as temp:
            target = pathlib.Path(temp) / "owned.tmp"
            target.write_text("owned", encoding="utf-8")
            expected_identity = path_identity(target)
            parked = pathlib.Path(temp) / "parked-owned.tmp"
            original_unlink = pathlib.Path.unlink
            injected = False

            def replace_before_path_unlink(path, *args, **kwargs):
                nonlocal injected
                path = pathlib.Path(path)
                if path.name.startswith(".SpectrumOrganizer_cleanup_"):
                    injected = True
                    path.rename(parked)
                    path.write_text("FOREIGN", encoding="utf-8")
                return original_unlink(path, *args, **kwargs)

            with mock.patch.object(
                pathlib.Path,
                "unlink",
                autospec=True,
                side_effect=replace_before_path_unlink,
            ):
                unlink_owned_path(target, expected_identity)

            self.assertFalse(injected)
            self.assertFalse(parked.exists())


def _attempt(attempt, status, message):
    return type("Attempt", (), {"attempt": attempt, "status": status, "message": message})()


if __name__ == "__main__":
    unittest.main()
