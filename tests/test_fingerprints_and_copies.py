import json
import os
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
        self.root = ROOT / ".test-tmp" / "task2"
        self.path = self.root / f"case-{uuid.uuid4().hex}"

    def __enter__(self):
        self.path.mkdir(parents=True, exist_ok=False)
        return self.path

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


class FingerprintAndCopyTests(unittest.TestCase):
    def test_owned_root_creation_does_not_adopt_same_path_replacement(self):
        from spectrum_organizer.safety.owned_paths import (
            create_run_ownership,
        )

        with workspace_tempdir() as tmp:
            target = tmp / "localapp" / "Spectrum Organizer" / "temp" / "run-race"
            parked = tmp / "parked-created-root"
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
                ownership = create_run_ownership(
                    tmp / "localapp",
                    "run-race",
                    "marker-race",
                    [],
                )

            self.assertFalse(attacked)
            self.assertEqual(target, ownership.temp_root)
            self.assertFalse(parked.exists())

    def test_source_directory_creation_does_not_adopt_same_path_replacement(self):
        from spectrum_organizer.safety.fingerprints import snapshot_sources
        from spectrum_organizer.safety.owned_paths import create_run_ownership
        from spectrum_organizer.safety.source_copies import (
            copy_sources,
        )

        with workspace_tempdir() as tmp:
            source = tmp / "raw.opju"
            source.write_bytes(b"raw")
            snapshot = snapshot_sources([source], protected_paths=[])[0]
            ownership = create_run_ownership(
                tmp / "localapp",
                "run-race",
                "marker-race",
                [],
            )
            target = ownership.temp_root / f"source-0001-{snapshot.sha256[:12]}"
            parked = ownership.temp_root / "parked-created-source"
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
                result = copy_sources([snapshot], ownership)

            self.assertFalse(attacked)
            self.assertEqual(target, result.copies[0].path.parent)
            self.assertFalse(parked.exists())

    @unittest.skipUnless(sys.platform == "win32", "Windows directory handle contract")
    def test_exclusive_directory_creation_blocks_namespace_replacement_while_held(self):
        from spectrum_organizer.safety.identity_paths import (
            create_exclusive_held_directory,
        )

        with workspace_tempdir() as tmp:
            target = tmp / "held-directory"
            with create_exclusive_held_directory(target):
                with self.assertRaises(PermissionError):
                    target.rename(tmp / "replacement-target")

    @unittest.skipUnless(sys.platform == "win32", "Windows share-mode contract")
    def test_locked_verified_copy_blocks_write_and_replacement_until_release(self):
        from spectrum_organizer.safety.fingerprints import file_identity, hash_file
        from spectrum_organizer.safety.source_copies import locked_verified_source_copy

        with workspace_tempdir() as tmp:
            copy_path = tmp / "copy.opju"
            copy_path.write_bytes(b"approved-copy")
            identity = file_identity(copy_path)

            with locked_verified_source_copy(
                copy_path,
                expected_identity=identity,
                expected_size_bytes=copy_path.stat().st_size,
                expected_sha256=hash_file(copy_path),
            ):
                with self.assertRaises(PermissionError):
                    copy_path.write_bytes(b"changed")
                with self.assertRaises(PermissionError):
                    copy_path.unlink()

            copy_path.unlink()

    def test_same_basename_sources_copy_into_unique_ordered_directories(self):
        from spectrum_organizer.safety.fingerprints import snapshot_sources
        from spectrum_organizer.safety.owned_paths import create_run_ownership
        from spectrum_organizer.safety.source_copies import copy_sources

        with workspace_tempdir() as tmp:
            left = tmp / "left"
            right = tmp / "right"
            left.mkdir()
            right.mkdir()
            source_a = left / "sample.opj"
            source_b = right / "sample.opj"
            source_a.write_bytes(b"alpha")
            source_b.write_bytes(b"bravo")
            protected_reference = tmp / "protected-reference.opju"
            protected_reference.write_bytes(b"reference")
            snapshots = snapshot_sources([source_a, source_b], protected_paths=[protected_reference])
            ownership = create_run_ownership(tmp / "localapp", "run-1", "marker-1", [protected_reference])

            result = copy_sources(snapshots, ownership, free_bytes_provider=lambda path: 2 * 1024**3)

            copied_names = [copy.path.name for copy in result.copies]
            self.assertEqual(copied_names, ["sample.opj", "sample.opj"])
            parent_names = [copy.path.parent.name for copy in result.copies]
            self.assertEqual(parent_names[0], f"source-0001-{snapshots[0].sha256[:12]}")
            self.assertEqual(parent_names[1], f"source-0002-{snapshots[1].sha256[:12]}")
            self.assertNotEqual(result.copies[0].path.parent, result.copies[1].path.parent)
            self.assertEqual(result.copies[0].path.read_bytes(), b"alpha")
            self.assertEqual(result.copies[1].path.read_bytes(), b"bravo")

    def test_source_directory_is_registered_before_it_is_created(self):
        from spectrum_organizer.safety.fingerprints import snapshot_sources
        from spectrum_organizer.safety.owned_paths import create_run_ownership, read_ownership
        from spectrum_organizer.safety.source_copies import copy_sources

        with workspace_tempdir() as tmp:
            source = tmp / "sample.opj"
            source.write_bytes(b"source")
            snapshots = snapshot_sources([source], protected_paths=[])
            ownership = create_run_ownership(tmp / "localapp", "run-1", "marker-1", [])
            real_mkdir = pathlib.Path.mkdir

            def checked_mkdir(path, *args, **kwargs):
                persisted = read_ownership(ownership.temp_root)
                self.assertIn(path, persisted.allowed_children)
                return real_mkdir(path, *args, **kwargs)

            with mock.patch.object(pathlib.Path, "mkdir", autospec=True, side_effect=checked_mkdir):
                result = copy_sources(
                    snapshots,
                    ownership,
                    free_bytes_provider=lambda path: 2 * 1024**3,
                )

            self.assertTrue(result.copies[0].path.is_file())

    def test_missing_local_appdata_has_no_fallback_temp_root(self):
        from spectrum_organizer.safety.owned_paths import OwnershipError, create_run_ownership

        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(OwnershipError, "LOCALAPPDATA"):
                create_run_ownership(None, "run-1", "marker-1", [])

    def test_initial_ownership_write_failure_retains_unowned_pending_collision(self):
        from spectrum_organizer.safety import owned_paths

        with workspace_tempdir() as tmp:
            base = tmp / "localapp"
            run_root = base / "Spectrum Organizer" / "temp" / "run-1"

            def fail_initial_write(ownership):
                (ownership.temp_root / owned_paths.OWNERSHIP_TEMP_FILE).write_text(
                    "FOREIGN USER CONTENT",
                    encoding="utf-8",
                )
                raise owned_paths.OwnershipError("metadata failed")

            with (
                mock.patch.object(
                    owned_paths,
                    "_write_initial_ownership_under_created_root",
                    side_effect=fail_initial_write,
                ),
                self.assertRaisesRegex(owned_paths.OwnershipError, "metadata failed"),
            ):
                owned_paths.create_run_ownership(base, "run-1", "marker-1", [])

            pending = run_root / owned_paths.OWNERSHIP_TEMP_FILE
            self.assertTrue(run_root.is_dir())
            self.assertEqual(
                "FOREIGN USER CONTENT",
                pending.read_text(encoding="utf-8"),
            )

    def test_insufficient_space_error_payload_lists_required_fields(self):
        from spectrum_organizer.safety.fingerprints import snapshot_sources
        from spectrum_organizer.safety.owned_paths import create_run_ownership
        from spectrum_organizer.safety.source_copies import InsufficientSpaceError, copy_sources

        with workspace_tempdir() as tmp:
            source = tmp / "sample.opj"
            source.write_bytes(b"1234567890")
            snapshots = snapshot_sources([source], protected_paths=[])
            ownership = create_run_ownership(tmp / "localapp", "run-1", "marker-1", [])

            with self.assertRaises(InsufficientSpaceError) as caught:
                copy_sources(snapshots, ownership, free_bytes_provider=lambda path: 1)

            payload = caught.exception.payload
            self.assertEqual(payload.temp_root, ownership.temp_root)
            self.assertEqual(payload.input_total_bytes, 10)
            self.assertEqual(payload.required_bytes, 1024**3)
            self.assertEqual(payload.available_bytes, 1)
            self.assertEqual(payload.actions, ("retry", "cancel"))

    def test_required_temp_space_uses_two_point_five_x_plus_sixty_four_mib_ceiling(self):
        from spectrum_organizer.safety.source_copies import MIB, required_temp_bytes

        self.assertEqual(1024**3, required_temp_bytes(0))
        self.assertEqual(1024**3, required_temp_bytes(1))
        self.assertEqual((5 * 500_000_001 + 1) // 2 + 64 * MIB, required_temp_bytes(500_000_001))

    def test_insufficient_space_aborts_before_copy_callback(self):
        from spectrum_organizer.safety.fingerprints import snapshot_sources
        from spectrum_organizer.safety.owned_paths import create_run_ownership
        from spectrum_organizer.safety.source_copies import InsufficientSpaceError, copy_sources

        with workspace_tempdir() as tmp:
            source = tmp / "sample.opj"
            source.write_bytes(b"1234567890")
            snapshots = snapshot_sources([source], protected_paths=[])
            ownership = create_run_ownership(tmp / "localapp", "run-1", "marker-1", [])
            copy_calls = []

            with self.assertRaises(InsufficientSpaceError):
                copy_sources(
                    snapshots,
                    ownership,
                    free_bytes_provider=lambda path: 1,
                    copy_file=lambda source_path, target_path: copy_calls.append((source_path, target_path)),
                )

            self.assertEqual([], copy_calls)

    def test_default_space_check_uses_actual_temp_root_volume(self):
        from spectrum_organizer.safety.source_copies import InsufficientSpaceError, ensure_sufficient_space

        class DiskUsage:
            free = 1

        with workspace_tempdir() as tmp:
            with mock.patch("spectrum_organizer.safety.source_copies.shutil.disk_usage", return_value=DiskUsage()) as disk_usage:
                with self.assertRaises(InsufficientSpaceError) as caught:
                    ensure_sufficient_space(tmp, 10)

            disk_usage.assert_called_once_with(tmp)
            self.assertEqual(caught.exception.payload.temp_root, tmp)
            self.assertEqual(caught.exception.payload.available_bytes, 1)

    def test_copy_sources_defaults_to_actual_temp_root_volume(self):
        from spectrum_organizer.safety.fingerprints import snapshot_sources
        from spectrum_organizer.safety.owned_paths import create_run_ownership
        from spectrum_organizer.safety.source_copies import InsufficientSpaceError, copy_sources

        class DiskUsage:
            free = 1

        with workspace_tempdir() as tmp:
            source = tmp / "sample.opj"
            source.write_bytes(b"1234567890")
            snapshots = snapshot_sources([source], protected_paths=[])
            ownership = create_run_ownership(tmp / "localapp", "run-1", "marker-1", [])

            with mock.patch("spectrum_organizer.safety.source_copies.shutil.disk_usage", return_value=DiskUsage()) as disk_usage:
                with self.assertRaises(InsufficientSpaceError):
                    copy_sources(snapshots, ownership)

            disk_usage.assert_called_once_with(ownership.temp_root)

    def test_space_formula_rejects_overflow_and_negative_input(self):
        from spectrum_organizer.safety.source_copies import SpaceRequirementError, required_temp_bytes

        with self.assertRaisesRegex(SpaceRequirementError, "negative"):
            required_temp_bytes(-1)
        with self.assertRaisesRegex(SpaceRequirementError, "signed 64-bit"):
            required_temp_bytes(2**63)

    def test_copy_hash_mismatch_aborts(self):
        from spectrum_organizer.safety.fingerprints import snapshot_sources
        from spectrum_organizer.safety.owned_paths import create_run_ownership
        from spectrum_organizer.safety.source_copies import CopyVerificationError, copy_sources

        with workspace_tempdir() as tmp:
            source = tmp / "sample.opj"
            source.write_bytes(b"original")
            snapshots = snapshot_sources([source], protected_paths=[])
            ownership = create_run_ownership(tmp / "localapp", "run-1", "marker-1", [])

            def corrupt_copy(source_path, target_path):
                shutil.copy2(source_path, target_path)
                target_path.write_bytes(b"changed")

            with self.assertRaisesRegex(CopyVerificationError, "copy mismatch"):
                copy_sources(
                    snapshots,
                    ownership,
                    free_bytes_provider=lambda path: 2 * 1024**3,
                    copy_file=corrupt_copy,
                )

    def test_original_mutation_after_worker_aborts(self):
        from spectrum_organizer.safety.fingerprints import SnapshotMismatchError, snapshot_sources, verify_sources_unchanged

        with workspace_tempdir() as tmp:
            source = tmp / "sample.opj"
            source.write_bytes(b"before")
            snapshots = snapshot_sources([source], protected_paths=[])
            source.write_bytes(b"after")

            with self.assertRaisesRegex(SnapshotMismatchError, "changed"):
                verify_sources_unchanged(snapshots)

    def test_source_path_cannot_be_rebound_to_byte_identical_file(self):
        from spectrum_organizer.safety.fingerprints import SnapshotMismatchError, snapshot_sources, verify_sources_unchanged

        with workspace_tempdir() as tmp:
            original = tmp / "original.opju"
            substitute = tmp / "substitute.opju"
            selected_path = tmp / "selected.opju"
            original.write_bytes(b"same bytes")
            substitute.write_bytes(b"same bytes")
            original_stat = original.stat()
            os.utime(
                substitute,
                ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
            )
            try:
                selected_path.hardlink_to(original)
            except OSError as exc:
                self.skipTest(f"hard links are unavailable: {exc}")

            snapshots = snapshot_sources([selected_path], protected_paths=[])
            selected_path.unlink()
            selected_path.hardlink_to(substitute)
            original.write_bytes(b"changed original")

            with self.assertRaisesRegex(SnapshotMismatchError, "changed"):
                verify_sources_unchanged(snapshots)

    def test_hash_file_checks_cancellation_between_chunks(self):
        from spectrum_organizer.safety.fingerprints import hash_file

        with workspace_tempdir() as tmp:
            source = tmp / "large.opju"
            source.write_bytes(b"x" * (2 * 1024 * 1024 + 1))
            checks = []

            def cancel_check():
                checks.append(None)
                if len(checks) == 2:
                    raise RuntimeError("cancelled")

            with self.assertRaisesRegex(RuntimeError, "cancelled"):
                hash_file(source, cancel_check=cancel_check)

            self.assertEqual(2, len(checks))

    def test_source_path_must_not_overlap_protected_paths(self):
        from spectrum_organizer.safety.fingerprints import SnapshotError, snapshot_sources

        with workspace_tempdir() as tmp:
            protected_reference = tmp / "protected-reference.opju"
            protected_reference.write_bytes(b"reference")

            with self.assertRaisesRegex(SnapshotError, "protected"):
                snapshot_sources([protected_reference], protected_paths=[protected_reference])

    def test_source_hard_link_must_not_alias_protected_path(self):
        from spectrum_organizer.safety.fingerprints import SnapshotError, snapshot_sources

        with workspace_tempdir() as tmp:
            protected_reference = tmp / "protected-reference.opju"
            protected_reference.write_bytes(b"reference")
            alias = tmp / "renamed-input.opju"
            try:
                alias.hardlink_to(protected_reference)
            except OSError as exc:
                self.skipTest(f"hard links are unavailable: {exc}")

            with self.assertRaisesRegex(SnapshotError, "protected"):
                snapshot_sources([alias], protected_paths=[protected_reference])

    def test_selected_sources_must_remain_distinct_physical_files_at_snapshot_time(self):
        from spectrum_organizer.safety.fingerprints import SnapshotError, snapshot_sources

        with workspace_tempdir() as tmp:
            source_a = tmp / "a.opju"
            source_b = tmp / "b.opju"
            source_a.write_bytes(b"a")
            source_b.write_bytes(b"b")

            source_b.unlink()
            try:
                source_b.hardlink_to(source_a)
            except OSError as exc:
                self.skipTest(f"hard links are unavailable: {exc}")

            with self.assertRaisesRegex(SnapshotError, "same physical|duplicate"):
                snapshot_sources([source_a, source_b], protected_paths=[])

    def test_cleanup_refuses_unknown_paths(self):
        from spectrum_organizer.safety.owned_paths import CleanupRefusedError, cleanup_owned_temp_root, create_run_ownership

        with workspace_tempdir() as tmp:
            ownership = create_run_ownership(tmp / "localapp", "run-1", "marker-1", [])
            unknown = ownership.temp_root / "unknown"
            unknown.mkdir()

            with self.assertRaisesRegex(CleanupRefusedError, "unknown"):
                cleanup_owned_temp_root(ownership.temp_root)

            self.assertTrue(unknown.exists())
            self.assertTrue(ownership.temp_root.exists())

    def test_cleanup_isolates_owned_temp_root_before_recursive_delete(self):
        from spectrum_organizer.safety import owned_paths

        with workspace_tempdir() as tmp:
            ownership = owned_paths.create_run_ownership(
                tmp / "localapp",
                "run-1",
                "marker-1",
                [],
            )
            child = ownership.temp_root / "source-0001"
            child.mkdir()
            owned_file = child / "owned.opju"
            owned_file.write_bytes(b"owned")
            ownership = owned_paths.add_allowed_child(ownership, child)
            ownership = owned_paths.add_allowed_child(ownership, owned_file)
            original_remove = owned_paths._remove_owned_tree
            removal_roots = []

            def record_isolated_delete(path, expected_identity, **kwargs):
                path = pathlib.Path(path)
                removal_roots.append(path)
                return original_remove(path, expected_identity, **kwargs)

            with mock.patch.object(
                owned_paths,
                "_remove_owned_tree",
                side_effect=record_isolated_delete,
            ):
                deleted = owned_paths.cleanup_owned_temp_root(
                    ownership.temp_root
                )

            self.assertTrue(removal_roots)
            self.assertNotIn(child, removal_roots)
            self.assertNotEqual(ownership.temp_root, removal_roots[0])
            self.assertCountEqual([child, owned_file], deleted)
            self.assertFalse(ownership.temp_root.exists())

    def test_cleanup_refuses_replaced_source_copy_descendant(self):
        from spectrum_organizer.safety.fingerprints import snapshot_sources
        from spectrum_organizer.safety.owned_paths import (
            CleanupRefusedError,
            cleanup_owned_temp_root,
            create_run_ownership,
        )
        from spectrum_organizer.safety.source_copies import copy_sources

        with workspace_tempdir() as tmp:
            source = tmp / "source.opju"
            source.write_bytes(b"owned source bytes")
            ownership = create_run_ownership(
                tmp / "localapp",
                "run-1",
                "marker-1",
                [],
            )
            result = copy_sources(
                snapshot_sources([source], protected_paths=[]),
                ownership,
                free_bytes_provider=lambda _path: 2 * 1024**3,
            )
            copy_path = result.copies[0].path
            parked_owned_copy = tmp / "parked-owned-source.opju"
            copy_path.rename(parked_owned_copy)
            copy_path.write_bytes(source.read_bytes())

            with self.assertRaises(CleanupRefusedError):
                cleanup_owned_temp_root(result.ownership.temp_root)

            self.assertEqual(b"owned source bytes", copy_path.read_bytes())
            self.assertEqual(b"owned source bytes", parked_owned_copy.read_bytes())

    def test_cleanup_refuses_replacement_at_quarantined_temp_root(self):
        from spectrum_organizer.safety import owned_paths

        with workspace_tempdir() as tmp:
            ownership = owned_paths.create_run_ownership(
                tmp / "localapp",
                "run-1",
                "marker-1",
                [],
            )
            original_quarantine = owned_paths.quarantine_owned_path
            parked = tmp / "parked-owned-root"
            replacement = None

            def quarantine_then_replace(path, expected_identity):
                nonlocal replacement
                isolated = original_quarantine(path, expected_identity)
                if pathlib.Path(path) == ownership.temp_root:
                    isolated.rename(parked)
                    isolated.mkdir()
                    replacement = isolated
                return isolated

            with mock.patch.object(
                owned_paths,
                "quarantine_owned_path",
                side_effect=quarantine_then_replace,
            ), self.assertRaises(owned_paths.CleanupRefusedError):
                owned_paths.cleanup_owned_temp_root(ownership.temp_root)

            self.assertIsNotNone(replacement)
            self.assertTrue(replacement.exists())
            self.assertTrue(parked.exists())

    def test_cleanup_refuses_same_path_replacement_with_copied_marker(self):
        from spectrum_organizer.safety import owned_paths

        with workspace_tempdir() as tmp:
            ownership = owned_paths.create_run_ownership(
                tmp / "localapp",
                "run-1",
                "marker-1",
                [],
            )
            child = ownership.temp_root / "source-0001"
            child.mkdir()
            (child / "owned.opju").write_bytes(b"owned")
            ownership = owned_paths.add_allowed_child(ownership, child)
            parked = ownership.temp_root.with_name("parked-approved-root")
            ownership.temp_root.rename(parked)
            ownership.temp_root.mkdir()
            shutil.copyfile(
                parked / owned_paths.OWNERSHIP_FILE,
                ownership.temp_root / owned_paths.OWNERSHIP_FILE,
            )
            replacement_child = ownership.temp_root / "source-0001"
            replacement_child.mkdir()
            foreign = replacement_child / "foreign.opju"
            foreign.write_bytes(b"FOREIGN USER CONTENT")

            with self.assertRaisesRegex(
                owned_paths.CleanupRefusedError,
                "identity|身份|ownership",
            ):
                owned_paths.cleanup_owned_temp_root(ownership.temp_root)

            self.assertEqual(b"FOREIGN USER CONTENT", foreign.read_bytes())
            self.assertEqual(b"owned", (parked / "source-0001" / "owned.opju").read_bytes())

    def test_cleanup_refuses_self_consistent_replacement_ownership_ledger(self):
        from spectrum_organizer.safety import owned_paths

        with workspace_tempdir() as tmp:
            ownership = owned_paths.create_run_ownership(
                tmp / "localapp",
                "run-1",
                "marker-1",
                [],
            )
            foreign = ownership.temp_root / "foreign.txt"
            foreign.write_text("FOREIGN USER CONTENT", encoding="utf-8")
            metadata = ownership.temp_root / owned_paths.OWNERSHIP_FILE
            metadata.unlink()
            metadata.write_text(
                json.dumps(
                    {
                        "run_id": ownership.run_id,
                        "marker_id": ownership.marker_id,
                        "temp_root": str(ownership.temp_root),
                        "temp_root_identity": list(ownership.temp_root_identity),
                        "allowed_children": [str(foreign)],
                        "allowed_child_identities": [
                            {
                                "path": str(foreign),
                                "identity": list(owned_paths.path_identity(foreign)),
                            }
                        ],
                        "protected_paths": [],
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                owned_paths.CleanupRefusedError,
                "metadata|anchor|authentication|ownership",
            ):
                owned_paths.cleanup_owned_temp_root(
                    ownership.temp_root,
                    expected_root_identity=ownership.temp_root_identity,
                )

            self.assertEqual(
                "FOREIGN USER CONTENT",
                foreign.read_text(encoding="utf-8"),
            )

    def test_cleanup_authenticates_the_same_ledger_bytes_it_parses(self):
        from spectrum_organizer.safety import owned_paths

        with workspace_tempdir() as tmp:
            ownership = owned_paths.create_run_ownership(
                tmp / "localapp",
                "run-1",
                "marker-1",
                [],
            )
            metadata = ownership.temp_root / owned_paths.OWNERSHIP_FILE
            authenticated_bytes = metadata.read_bytes()
            metadata_identity = owned_paths.path_identity(metadata)
            foreign = ownership.temp_root / "foreign.txt"
            foreign.write_text("FOREIGN USER CONTENT", encoding="utf-8")
            attacker_payload = json.loads(authenticated_bytes)
            attacker_payload["allowed_children"] = [str(foreign)]
            attacker_payload["allowed_child_identities"] = [
                {
                    "path": str(foreign),
                    "identity": list(owned_paths.path_identity(foreign)),
                }
            ]
            attacker_bytes = json.dumps(attacker_payload, sort_keys=True).encode(
                "utf-8"
            )
            metadata.write_bytes(attacker_bytes)
            real_sha256 = owned_paths.hashlib.sha256
            restored = False

            def restore_authenticated_bytes(content=b""):
                nonlocal restored
                if content == attacker_bytes and not restored:
                    metadata.write_bytes(authenticated_bytes)
                    self.assertEqual(
                        metadata_identity,
                        owned_paths.path_identity(metadata),
                    )
                    restored = True
                return real_sha256(content)

            with mock.patch.object(
                owned_paths.hashlib,
                "sha256",
                side_effect=restore_authenticated_bytes,
            ), self.assertRaises(owned_paths.CleanupRefusedError):
                owned_paths.cleanup_owned_temp_root(
                    ownership.temp_root,
                    expected_root_identity=ownership.temp_root_identity,
                )

            self.assertTrue(restored)
            self.assertEqual(
                "FOREIGN USER CONTENT",
                foreign.read_text(encoding="utf-8"),
            )

    def test_cleanup_retains_shared_anchor_key(self):
        from spectrum_organizer.safety import owned_paths

        with workspace_tempdir() as tmp:
            ownership = owned_paths.create_run_ownership(
                tmp / "localapp",
                "run-1",
                "marker-1",
                [],
            )
            key_path = owned_paths._ownership_anchor_key_path(
                ownership.temp_root.parent
            )

            owned_paths.cleanup_owned_temp_root(
                ownership.temp_root,
                expected_root_identity=ownership.temp_root_identity,
            )

            self.assertTrue(key_path.is_file())

    @unittest.skipUnless(os.name == "nt", "Windows junction behavior")
    def test_cleanup_refuses_dangling_junction_replacement_after_root_delete(self):
        import _winapi

        from spectrum_organizer.safety import owned_paths

        with workspace_tempdir() as tmp:
            ownership = owned_paths.create_run_ownership(
                tmp / "localapp",
                "run-1",
                "marker-1",
                [],
            )
            real_remove = owned_paths._remove_owned_tree

            def replace_with_dangling_junction(*args, **kwargs):
                real_remove(*args, **kwargs)
                target = tmp / "junction-target"
                target.mkdir()
                _winapi.CreateJunction(str(target), str(ownership.temp_root))
                target.rmdir()

            try:
                with mock.patch.object(
                    owned_paths,
                    "_remove_owned_tree",
                    side_effect=replace_with_dangling_junction,
                ), self.assertRaises(owned_paths.CleanupRefusedError):
                    owned_paths.cleanup_owned_temp_root(
                        ownership.temp_root,
                        expected_root_identity=ownership.temp_root_identity,
                    )
            finally:
                if os.path.lexists(ownership.temp_root):
                    os.rmdir(ownership.temp_root)

    def test_ownership_json_records_run_marker_allowed_children_and_protected_paths(self):
        from spectrum_organizer.safety.fingerprints import snapshot_sources
        from spectrum_organizer.safety.owned_paths import create_run_ownership
        from spectrum_organizer.safety.source_copies import copy_sources

        with workspace_tempdir() as tmp:
            source = tmp / "sample.opj"
            source.write_bytes(b"data")
            protected_reference = tmp / "protected-reference.opju"
            protected_reference.write_bytes(b"reference")
            ownership = create_run_ownership(tmp / "localapp", "run-1", "marker-1", [protected_reference])
            snapshots = snapshot_sources([source], protected_paths=[protected_reference])

            copy_sources(snapshots, ownership, free_bytes_provider=lambda path: 2 * 1024**3)

            payload = json.loads((ownership.temp_root / "ownership.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["run_id"], "run-1")
            self.assertEqual(payload["marker_id"], "marker-1")
            self.assertEqual(payload["temp_root"], str(ownership.temp_root))
            self.assertEqual(payload["protected_paths"], [str(protected_reference)])
            allowed_children = tuple(
                pathlib.Path(item) for item in payload["allowed_children"]
            )
            self.assertEqual(2, len(allowed_children))
            self.assertTrue(any(path.is_dir() for path in allowed_children))
            self.assertTrue(any(path.is_file() for path in allowed_children))
            self.assertEqual(2, len(payload["allowed_child_identities"]))

    def test_ownership_update_is_atomic_and_preserves_previous_metadata_on_replace_failure(self):
        from spectrum_organizer.safety.owned_paths import OwnershipError, add_allowed_child, create_run_ownership, read_ownership

        with workspace_tempdir() as tmp:
            ownership = create_run_ownership(tmp / "localapp", "run-1", "marker-1", [])
            child = ownership.temp_root / "child"
            child.mkdir()
            with mock.patch(
                "spectrum_organizer.safety.owned_paths.os.link",
                side_effect=OSError("publish failed"),
            ):
                with self.assertRaisesRegex(OwnershipError, "publish failed"):
                    add_allowed_child(ownership, child)

            persisted = read_ownership(ownership.temp_root)
            self.assertEqual((), persisted.allowed_children)
            self.assertFalse((ownership.temp_root / "ownership.json.tmp").exists())

    def test_ownership_update_preserves_previous_marker_when_pending_cleanup_fails(self):
        from spectrum_organizer.safety import owned_paths

        with workspace_tempdir() as tmp:
            ownership = owned_paths.create_run_ownership(
                tmp / "localapp",
                "run-1",
                "marker-1",
                [],
            )
            child = ownership.temp_root / "child"
            child.mkdir()
            original_unlink = owned_paths.unlink_owned_path

            def fail_pending_cleanup(path, expected_identity):
                if pathlib.Path(path).name == owned_paths.OWNERSHIP_TEMP_FILE:
                    raise owned_paths.IdentityPathError(
                        pathlib.Path(path),
                        "pending cleanup failed",
                    )
                return original_unlink(path, expected_identity)

            with mock.patch.object(
                owned_paths,
                "unlink_owned_path",
                side_effect=fail_pending_cleanup,
            ), self.assertRaises(owned_paths.OwnershipError):
                owned_paths.add_allowed_child(ownership, child)

            self.assertTrue(
                (ownership.temp_root / owned_paths.OWNERSHIP_FILE).exists()
            )
            persisted = owned_paths.read_ownership(ownership.temp_root)
            self.assertEqual((), persisted.allowed_children)

    def test_ownership_update_refuses_replaced_destination_marker(self):
        from spectrum_organizer.safety.owned_paths import (
            OwnershipError,
            add_allowed_child,
            create_run_ownership,
        )

        with workspace_tempdir() as tmp:
            ownership = create_run_ownership(
                tmp / "localapp",
                "run-1",
                "marker-1",
                [],
            )
            metadata = ownership.temp_root / "ownership.json"
            parked = ownership.temp_root / "parked-owned-metadata.json"
            metadata.rename(parked)
            foreign = tmp / "foreign-marker.json"
            foreign.write_text("FOREIGN", encoding="utf-8")
            metadata.hardlink_to(foreign)
            child = ownership.temp_root / "child"
            child.mkdir()

            with self.assertRaises(OwnershipError):
                add_allowed_child(ownership, child)

            self.assertTrue(os.path.samefile(metadata, foreign))
            self.assertEqual("FOREIGN", foreign.read_text(encoding="utf-8"))
            self.assertTrue(parked.exists())

    def test_ownership_update_never_overwrites_preexisting_pending_hard_link(self):
        from spectrum_organizer.safety.owned_paths import (
            OwnershipError,
            add_allowed_child,
            create_run_ownership,
        )

        with workspace_tempdir() as tmp:
            ownership = create_run_ownership(
                tmp / "localapp",
                "run-1",
                "marker-1",
                [],
            )
            protected = tmp / "protected-user-file.txt"
            protected.write_text("IMMUTABLE USER CONTENT", encoding="utf-8")
            pending = ownership.temp_root / "ownership.json.tmp"
            pending.hardlink_to(protected)
            child = ownership.temp_root / "child"
            child.mkdir()

            with self.assertRaises(OwnershipError):
                add_allowed_child(ownership, child)

            self.assertEqual(
                "IMMUTABLE USER CONTENT",
                protected.read_text(encoding="utf-8"),
            )
            self.assertTrue(pending.exists())
            self.assertTrue(os.path.samefile(pending, protected))

if __name__ == "__main__":
    unittest.main()
