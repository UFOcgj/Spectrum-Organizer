import pathlib
import tempfile
import threading
import unittest
from unittest import mock


from validation import evidence_lock


class EvidenceLockTests(unittest.TestCase):
    def test_preexisting_prepared_directory_is_never_removed_by_failed_acquire(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            lock_path = root / ".workflow.lock"
            token = "fixed-token"
            prepared = root / f"..workflow.lock.{evidence_lock.os.getpid()}.{token}.acquire"
            prepared.mkdir()
            owner = prepared / "owner-token"
            owner.write_text("foreign-owner", encoding="ascii")
            fixed_uuid = mock.Mock(hex=token)

            with (
                mock.patch.object(evidence_lock, "uuid4", return_value=fixed_uuid),
                self.assertRaises(evidence_lock.OwnedDirectoryLockError),
            ):
                evidence_lock.acquire_owned_directory_lock(
                    lock_path,
                    owner_filename="owner-token",
                    label="Test workflow",
                )

            self.assertEqual("foreign-owner", owner.read_text(encoding="ascii"))
            self.assertTrue(prepared.is_dir())

    def test_successor_cannot_acquire_after_canonical_lock_is_captured_for_release(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            lock_path = root / ".workflow.lock"
            first = evidence_lock.acquire_owned_directory_lock(
                lock_path,
                owner_filename="owner-token",
                label="Test workflow",
            )
            captured = threading.Event()
            allow_release = threading.Event()
            release_errors: list[BaseException] = []
            original_unlink = pathlib.Path.unlink

            def pause_captured_owner_unlink(path, *args, **kwargs):
                if (
                    path.name == "owner-token"
                    and path.parent.name.endswith(f".{first.owner_token}.release")
                ):
                    captured.set()
                    self.assertTrue(allow_release.wait(timeout=10))
                return original_unlink(path, *args, **kwargs)

            def release_first():
                try:
                    evidence_lock.release_owned_directory_lock(first)
                except BaseException as exc:
                    release_errors.append(exc)

            successor = None
            with mock.patch.object(pathlib.Path, "unlink", pause_captured_owner_unlink):
                release_thread = threading.Thread(target=release_first)
                release_thread.start()
                self.assertTrue(captured.wait(timeout=10))
                try:
                    with self.assertRaisesRegex(
                        evidence_lock.OwnedDirectoryLockError,
                        "already running",
                    ):
                        successor = evidence_lock.acquire_owned_directory_lock(
                            lock_path,
                            owner_filename="owner-token",
                            label="Test workflow",
                        )
                finally:
                    allow_release.set()
                    release_thread.join(timeout=10)

            self.assertFalse(release_thread.is_alive())
            self.assertEqual([], release_errors)
            if successor is not None:
                evidence_lock.release_owned_directory_lock(successor)


if __name__ == "__main__":
    unittest.main()
