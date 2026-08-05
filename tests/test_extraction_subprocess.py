from dataclasses import replace
import hashlib
import inspect
import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import unittest.mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from spectrum_organizer.origin.extract_worker import ExtractionSource, InventoryBook, TerminalBookResult
from spectrum_organizer.origin import extract_worker as extract_worker_module
import spectrum_organizer.product_runner as product_runner
from spectrum_organizer.store import run_snapshot as run_snapshot_module
from spectrum_organizer.safety.fingerprints import snapshot_sources
from spectrum_organizer.safety.owned_paths import (
    CleanupRefusedError,
    RunOwnership,
    _write_initial_ownership_under_created_root,
    _write_ownership_anchor,
    add_allowed_child,
    bind_allowed_child_identity,
    bind_held_allowed_child_identity,
    create_run_ownership,
    read_ownership,
    write_ownership,
)
from spectrum_organizer.safety.identity_paths import file_sha256, path_identity
from spectrum_organizer.safety.process_job import ProcessJobError
from spectrum_organizer.safety.process_boundary import ProcessIdentity, ProcessInfo
from spectrum_organizer.store.run_snapshot import RunSnapshot
from spectrum_organizer.workflow import extraction_ipc


def _worker_args(manifest, result):
    return [file_sha256(manifest), str(manifest), str(result)]


def _test_origin_ownership(
    root: pathlib.Path,
    payload: dict[str, object],
) -> RunOwnership:
    try:
        return read_ownership(root)
    except CleanupRefusedError:
        root.mkdir(parents=True, exist_ok=True)
        ownership = _write_initial_ownership_under_created_root(
            RunOwnership(
                run_id=str(payload["run_id"]),
                marker_id=str(payload["marker_id"]),
                temp_root=root,
                temp_root_identity=path_identity(root),
                metadata_identity=None,
                allowed_children=(),
                allowed_child_identities=(),
                protected_paths=(),
            )
        )
        _write_ownership_anchor(ownership)
        return ownership


def _write_origin_sidecar(
    path: pathlib.Path,
    payload: dict[str, object],
) -> tuple[int, int]:
    sidecar = pathlib.Path(path)
    ownership = _test_origin_ownership(sidecar.parent, payload)
    if sidecar not in ownership.allowed_children:
        ownership = add_allowed_child(ownership, sidecar)
    if sidecar.exists():
        deadline = time.monotonic() + 1.0
        while True:
            try:
                sidecar.unlink()
                break
            except PermissionError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)
    with sidecar.open("x", encoding="utf-8") as stream:
        status = os.fstat(stream.fileno())
        identity = (status.st_dev, status.st_ino)
        bind_held_allowed_child_identity(ownership, sidecar, identity)
        document = dict(payload)
        document["creation_identity"] = list(identity)
        json.dump(document, stream)
        stream.flush()
        os.fsync(stream.fileno())
    return identity


def _make_context(base: pathlib.Path, source_count: int = 1):
    ownership = create_run_ownership(base / "localapp", "run-1", "marker-1", [])
    originals = []
    copies = []
    for index in range(1, source_count + 1):
        original = base / f"raw-{index}.opju"
        original.write_bytes(f"raw-{index}".encode("ascii"))
        source_dir = ownership.temp_root / f"source-{index:04d}"
        source_dir.mkdir()
        ownership = add_allowed_child(ownership, source_dir)
        source_copy = source_dir / original.name
        source_copy.write_bytes(original.read_bytes())
        ownership = add_allowed_child(ownership, source_copy)
        originals.append(original)
        copies.append(source_copy)
    fingerprints = tuple(snapshot_sources(originals, protected_paths=[]))
    return product_runner.ApprovedPreExtractionRunContext(
        run_id="run-1",
        timestamp="2026-07-12T00:00:00+00:00",
        selected_source_paths=tuple(originals),
        output_parent=base / "out",
        settings_snapshot={"s1Limit": 1000000, "steadyEmissionY": "S1c"},
        source_fingerprints_before=fingerprints,
        temp_root=ownership.temp_root,
        temp_root_identity=ownership.temp_root_identity,
        run_owned_source_copy_paths=tuple(copies),
    )


def _record_valid_source(
    snapshot_path: pathlib.Path,
    context,
    source_id: str,
    *,
    copy_path: pathlib.Path | None = None,
    reader_attempt: int | None = 1,
    include_original_provenance: bool = False,
) -> dict[str, object]:
    index = int(source_id[1:]) - 1
    source_copy = copy_path or context.run_owned_source_copy_paths[index]
    fingerprint = context.source_fingerprints_before[index]
    snapshot = RunSnapshot(snapshot_path)
    provenance = (
        {
            "original_path": fingerprint.canonical_path,
            "original_size_bytes": fingerprint.size_bytes,
            "original_mtime_ns": fingerprint.mtime_ns,
        }
        if include_original_provenance
        else {}
    )
    snapshot.add_source(
        source_id,
        source_copy,
        fingerprint.sha256,
        **provenance,
    )
    book = InventoryBook(source_id, "Root", "Book1", "Display", 1, ("Note", "Data"), True, True)
    snapshot.record_inventory_book(source_id, book)
    snapshot.record_book_result(
        TerminalBookResult(
            source_id=source_id,
            folder_path="Root",
            short_name="Book1",
            display_name="Display",
            page_order=1,
            spectrum_class="steady_emission",
            status="extracted",
            note_text="[EXP_FD_FILE]\nAcquisition Type = Spectral Acquisition[Emission]",
            data_sheet_name="Data",
            available_columns=("X", "S1c", "S1 X", "S1"),
            column_metadata=(
                ("A", "X", "X"),
                ("B", "S1c", "Y"),
                ("C", "S1 X", "X"),
                ("D", "S1", "Y"),
            ),
            selected_y_column="S1c",
            paired_x_column="X",
            selected_x_values=(300,),
            selected_y_values=(1,),
            s1_x_values=(300,),
            s1_values=(1,),
            selected_x_row_count=1,
            selected_y_row_count=1,
            max_planned_y=1,
            max_planned_y_x=300,
            s1_max_for_limit=1,
            s1_max_for_limit_x=300,
            s1_limit_status="ok",
            data_checksum="checksum",
        ),
        book,
    )
    snapshot.reconcile_source(source_id)
    if reader_attempt is not None:
        _record_open_target(context, source_id, reader_attempt, source_copy)
    return {
        "source_id": source_id,
        "original_path": str(fingerprint.path),
        "copy_path": str(source_copy),
        "inventory_count": 1,
        "result_count": 1,
        "extracted_count": 1,
        "rejected_count": 0,
    }


def _summary_payload(snapshot_path: pathlib.Path, source_summary: dict[str, object]) -> dict[str, object]:
    return {
        "snapshot_path": str(snapshot_path),
        "source_id": source_summary["source_id"],
        "inventory_count": source_summary["inventory_count"],
        "result_count": source_summary["result_count"],
        "extracted_count": source_summary["extracted_count"],
        "rejected_count": source_summary["rejected_count"],
    }


def _record_open_target(context, source_id: str, reader_attempt: int, copy_path: pathlib.Path) -> None:
    ownership = read_ownership(context.temp_root)
    marker_path = pathlib.Path(context.temp_root) / (
        f"origin_open_target.{source_id}.attempt{reader_attempt}.json"
    )
    pending_path = marker_path.with_name(f"{marker_path.name}.pending")
    for path in (marker_path, pending_path):
        if path not in ownership.allowed_children:
            ownership = add_allowed_child(ownership, path)
    def bind_created(path, identity):
        nonlocal ownership
        ownership = bind_allowed_child_identity(
            ownership,
            path,
            expected_identity=identity,
        )

    extract_worker_module._write_owned_json_atomic(
        marker_path,
        {
            "schema_version": 1,
            "run_id": context.run_id,
            "marker_id": ownership.marker_id,
            "source_id": source_id,
            "reader_attempt": reader_attempt,
            "open_target": str(copy_path.resolve()),
        },
        cleanup_identity_callback=bind_created,
    )


def _child_creation_stdout(result_path: pathlib.Path) -> str:
    result = pathlib.Path(result_path)
    ownership = read_ownership(result.parent)
    created = []
    for path in ownership.allowed_children:
        if (
            path.parent == result.parent
            and (path.name.startswith("origin_") or path.suffix == ".c")
            and path.exists()
        ):
            status = path.stat()
            created.append(
                {
                    "path": str(path),
                    "identity": [status.st_dev, status.st_ino],
                }
            )
    status = result.stat()
    return json.dumps(
        {
            "result_identity": [status.st_dev, status.st_ino],
            "result_sha256": hashlib.sha256(result.read_bytes()).hexdigest(),
            "created_temp_identities": created,
        }
    )


class _CompletedChild:
    def __init__(self, result_path: pathlib.Path, payload: dict[str, object], activity: dict[str, int]):
        self.returncode = 0
        self._activity = activity
        self._activity["active"] += 1
        self._activity["max_active"] = max(self._activity["max_active"], self._activity["active"])
        result_path.write_text(json.dumps({"ok": True, "summary": payload}), encoding="utf-8")
        self._stdout = _child_creation_stdout(result_path)

    def communicate(self, timeout=None):
        del timeout
        self._activity["active"] -= 1
        return self._stdout, ""

    def poll(self):
        return self.returncode


class _RejectedChild:
    returncode = 1

    def __init__(self, result_path: pathlib.Path, *, error: str, error_type: str):
        result_path.write_text(
            json.dumps(
                {
                    "ok": False,
                    "error": error,
                    "error_type": error_type,
                    "error_notes": [],
                }
            ),
            encoding="utf-8",
        )
        self._stdout = _child_creation_stdout(result_path)

    def communicate(self, timeout=None):
        del timeout
        return self._stdout, ""

    def poll(self):
        return self.returncode


class _ExitedJobReader:
    returncode = 1

    def __init__(self):
        self._spectrum_organizer_job = object()

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        del timeout
        return self.returncode


class ExtractionSubprocessTests(unittest.TestCase):
    def test_process_evidence_requires_result_content_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            result = root / "result.json"
            result.write_text('{"ok": true, "context": {}}', encoding="utf-8")
            identity = path_identity(result)
            stdout = json.dumps(
                {
                    "result_identity": list(identity),
                    "created_temp_identities": [],
                }
            )

            with self.assertRaises(product_runner.ProductRunnerError):
                product_runner._process_result_evidence(stdout, temp_root=root)

    def test_authenticated_result_read_rejects_same_inode_content_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            result = pathlib.Path(directory) / "result.json"
            evidence = product_runner._write_json_atomic_exclusive_evidence(
                result,
                {"ok": True, "context": {}},
            )
            original_identity = path_identity(result)
            result.write_text('{"ok":false,"error":"x"}', encoding="utf-8")
            self.assertEqual(original_identity, path_identity(result))

            with self.assertRaises(product_runner.ProductRunnerError):
                product_runner._read_authenticated_process_payload(
                    result,
                    "提取前复制子进程",
                    expected_identity=evidence.identity,
                    expected_sha256=evidence.sha256,
                )

    def test_manifest_digest_is_creation_evidence_not_a_post_close_hash(self):
        pre_source = inspect.getsource(
            product_runner.PreExtractionSubprocessRunner.__call__
        )
        reader_source = inspect.getsource(
            product_runner.ExtractionSubprocessRunner._run_reader_process_attempt
        )

        self.assertNotIn("hash_file(manifest_path)", pre_source)
        self.assertNotIn("hash_file(manifest_path)", reader_source)

    def test_keyboard_interrupt_requests_termination_before_reader_reference_is_cleared(self):
        with tempfile.TemporaryDirectory() as directory:
            context = _make_context(pathlib.Path(directory))
            runner = product_runner.ExtractionSubprocessRunner()
            process = object()
            observed = []

            def interrupt(_context):
                runner._current_process = process
                raise KeyboardInterrupt

            runner._run = interrupt
            runner._request_termination_locked = observed.append

            with self.assertRaises(KeyboardInterrupt):
                runner(context)

            self.assertEqual([process], observed)
            self.assertTrue(runner._cancelled.is_set())

    def test_keyboard_interrupt_during_reader_phase_cleans_owned_temp_root(self):
        with tempfile.TemporaryDirectory() as directory:
            context = _make_context(pathlib.Path(directory))
            runner = product_runner.ExtractionSubprocessRunner()
            runner._run = unittest.mock.Mock(side_effect=KeyboardInterrupt)

            with self.assertRaises(KeyboardInterrupt):
                runner(context)

            self.assertFalse(context.temp_root.exists())

    def test_keyboard_interrupt_keeps_primary_error_when_cancel_cleanup_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            context = _make_context(pathlib.Path(directory))
            runner = product_runner.ExtractionSubprocessRunner()

            def interrupt(_context):
                runner._current_process = object()
                raise KeyboardInterrupt

            def fail_cleanup(_process):
                raise RuntimeError("cleanup failed")

            runner._run = interrupt
            runner._request_termination_locked = fail_cleanup

            with self.assertRaises(KeyboardInterrupt) as captured:
                runner(context)

            self.assertIn("cleanup failed", "\n".join(captured.exception.__notes__))

    @classmethod
    def setUpClass(cls):
        cls._default_origin_process_probe = product_runner.default_origin_process_probe
        product_runner.default_origin_process_probe = lambda **_kwargs: ()

    @classmethod
    def tearDownClass(cls):
        product_runner.default_origin_process_probe = cls._default_origin_process_probe

    def test_runner_rejects_nonpositive_wait_configuration(self):
        for field in (
            "cancellation_timeout",
            "cancellation_poll_interval",
            "origin_identity_timeout",
            "origin_shutdown_timeout",
            "origin_shutdown_poll_interval",
        ):
            for value in (0, float("nan"), float("inf"), 10**400):
                with self.subTest(field=field, value=value), self.assertRaisesRegex(ValueError, field):
                    product_runner.ExtractionSubprocessRunner(**{field: value})

    def test_reader_start_gate_does_not_overwrite_existing_entity(self):
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            sentinel = base / "sentinel.txt"
            sentinel.write_text("unchanged", encoding="utf-8")
            gate = base / "start.gate"
            gate.hardlink_to(sentinel)
            runner = product_runner.ExtractionSubprocessRunner()

            with self.assertRaises(FileExistsError):
                runner._release_start_gate(unittest.mock.Mock(), gate, "task-secret")

            self.assertEqual("unchanged", sentinel.read_text(encoding="utf-8"))
            self.assertFalse(runner._origin_start_gate_released)

    def test_initial_snapshot_registration_rejects_preexisting_hard_link(self):
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            context = _make_context(base)
            sentinel = base / "sentinel.sqlite3"
            connection = sqlite3.connect(sentinel)
            try:
                connection.execute("create table sentinel(value text)")
                connection.execute("insert into sentinel values ('unchanged')")
                connection.commit()
            finally:
                connection.close()
            snapshot_path = context.temp_root / "run_snapshot.sqlite3"
            snapshot_path.hardlink_to(sentinel)

            with self.assertRaises(product_runner.ProductRunnerError):
                product_runner._register_snapshot_path(context, snapshot_path)

            connection = sqlite3.connect(sentinel)
            try:
                tables = {
                    row[0]
                    for row in connection.execute(
                        "select name from sqlite_master where type = 'table'"
                    )
                }
            finally:
                connection.close()
            self.assertEqual({"sentinel"}, tables)

    def test_reader_rejects_copy_replaced_by_original_hard_link_after_parent_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            context = _make_context(base)
            source = product_runner._build_extraction_sources(context, ExtractionSource)[0]
            snapshot_path = product_runner._register_snapshot_path(context, None)
            product_runner._prepare_reader_temp_root(context)
            command = product_runner._build_reader_process_command(context, source, snapshot_path)
            copy_path = context.run_owned_source_copy_paths[0]
            copy_path.unlink()
            copy_path.hardlink_to(context.selected_source_paths[0])
            worker_factory_builder = unittest.mock.Mock()

            with self.assertRaisesRegex(product_runner.ProductRunnerError, "身份|identity|原始文件"):
                product_runner.run_reader_source_extraction_phase(
                    command,
                    worker_factory_builder=worker_factory_builder,
                    free_bytes_provider=lambda _path: 2**63 - 1,
                )

            worker_factory_builder.assert_not_called()

    def test_reader_temp_preparation_isolates_registered_file_before_unlink(self):
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            context = _make_context(base)
            sensitive = context.temp_root / "pre_extraction_context.json"
            sensitive.write_text("owned manifest", encoding="utf-8")
            ownership = add_allowed_child(
                read_ownership(context.temp_root),
                sensitive,
            )
            parked = context.temp_root / "parked-owned-manifest.json"
            original_unlink = pathlib.Path.unlink
            direct_delete_injected = False

            def replace_if_direct_unlink(path, *args, **kwargs):
                nonlocal direct_delete_injected
                path = pathlib.Path(path)
                if path == sensitive:
                    direct_delete_injected = True
                    path.rename(parked)
                    path.write_text("FOREIGN USER CONTENT", encoding="utf-8")
                return original_unlink(path, *args, **kwargs)

            with unittest.mock.patch.object(
                pathlib.Path,
                "unlink",
                autospec=True,
                side_effect=replace_if_direct_unlink,
            ):
                product_runner._prepare_reader_temp_root(context)

            self.assertEqual("run-1", ownership.run_id)
            self.assertFalse(direct_delete_injected)
            self.assertFalse(sensitive.exists())
            self.assertFalse(parked.exists())

    def test_reader_temp_preparation_refuses_registered_file_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            context = _make_context(base)
            sensitive = context.temp_root / "pre_extraction_context.json"
            sensitive.write_text("owned manifest", encoding="utf-8")
            add_allowed_child(read_ownership(context.temp_root), sensitive)
            parked = context.temp_root / "parked-owned-manifest.json"
            sensitive.rename(parked)
            sensitive.write_text("FOREIGN", encoding="utf-8")

            with self.assertRaises(product_runner.ProductRunnerError):
                product_runner._prepare_reader_temp_root(context)

            self.assertEqual("FOREIGN", sensitive.read_text(encoding="utf-8"))
            self.assertEqual("owned manifest", parked.read_text(encoding="utf-8"))

    def test_reader_passes_confirmed_validation_settings_to_orchestrator(self):
        class StopAfterConstruction(RuntimeError):
            pass

        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            context = replace(
                _make_context(base),
                settings_snapshot={
                    "s1Limit": 42,
                    "steadyEmissionY": "S1c/R1c",
                    "allowMissingS1": True,
                },
            )
            source = product_runner._build_extraction_sources(context, ExtractionSource)[0]
            snapshot_path = product_runner._register_snapshot_path(context, None)
            product_runner._prepare_reader_temp_root(context)
            command = product_runner._build_reader_process_command(context, source, snapshot_path)

            with unittest.mock.patch.object(
                extract_worker_module,
                "ExtractionOrchestrator",
            ) as orchestrator:
                orchestrator.return_value.run.side_effect = StopAfterConstruction
                with self.assertRaises(StopAfterConstruction):
                    product_runner.run_reader_source_extraction_phase(
                        command,
                        worker_factory_builder=lambda **_kwargs: object(),
                        free_bytes_provider=lambda _path: 2**63 - 1,
                    )

            self.assertEqual(42, orchestrator.call_args.kwargs["s1_limit"])
            self.assertEqual("S1c/R1c", orchestrator.call_args.kwargs["steady_emission_y"])
            self.assertTrue(orchestrator.call_args.kwargs["allow_missing_s1"])

    def test_reader_localizes_unsupported_source_without_leaking_internal_source_id(self):
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            context = _make_context(base)
            source = product_runner._build_extraction_sources(context, ExtractionSource)[0]
            snapshot_path = product_runner._register_snapshot_path(context, None)
            product_runner._prepare_reader_temp_root(context)
            command = product_runner._build_reader_process_command(context, source, snapshot_path)

            with unittest.mock.patch.object(
                extract_worker_module,
                "ExtractionOrchestrator",
            ) as orchestrator:
                orchestrator.return_value.run.side_effect = (
                    run_snapshot_module.UnsupportedSourceReconciliationError(
                        "Source has zero recognizable supported raw-spectrum Books: S0001"
                    )
                )
                with self.assertRaises(product_runner.UnsupportedSourceInputError) as captured:
                    product_runner.run_reader_source_extraction_phase(
                        command,
                        worker_factory_builder=lambda **_kwargs: object(),
                        free_bytes_provider=lambda _path: 2**63 - 1,
                    )

            self.assertIn("未检测到受支持的 Origin 原始谱图", str(captured.exception))
            self.assertNotIn("S0001", str(captured.exception))

    def test_extraction_sources_reject_shared_copy_before_reader_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            context = _make_context(base, source_count=2)
            shared = context.run_owned_source_copy_paths[0]
            forged = replace(context, run_owned_source_copy_paths=(shared, shared))

            with self.assertRaisesRegex(product_runner.ProductRunnerError, "unique|distinct"):
                product_runner._build_extraction_sources(forged, ExtractionSource)

    def test_runner_uses_default_origin_probe_when_none_is_injected(self):
        probe = unittest.mock.Mock(return_value=())
        with unittest.mock.patch.object(product_runner, "default_origin_process_probe", probe):
            runner = product_runner.ExtractionSubprocessRunner(origin_shutdown_poll_interval=0.001)

        runner._wait_for_origin_shutdown()

        self.assertEqual(2, probe.call_count)
        self.assertTrue(all(call.kwargs["timeout"] <= 5 for call in probe.call_args_list))

    def test_default_origin_probe_receives_only_remaining_shutdown_budget(self):
        probe = unittest.mock.Mock(return_value=())
        with unittest.mock.patch.object(product_runner, "default_origin_process_probe", probe):
            runner = product_runner.ExtractionSubprocessRunner(
                origin_shutdown_timeout=12.0,
                origin_shutdown_poll_interval=0.1,
            )

        with unittest.mock.patch.object(
            product_runner.time,
            "monotonic",
            side_effect=(0.0, 0.1, 1.0, 11.8, 12.1),
        ), unittest.mock.patch.object(product_runner.time, "sleep"):
            with self.assertRaisesRegex(product_runner.ExtractionCleanupBlockedError, "超时"):
                runner._wait_for_origin_shutdown()

        self.assertEqual(2, probe.call_count)
        self.assertEqual(5.0, probe.call_args_list[0].kwargs["timeout"])
        self.assertAlmostEqual(0.2, probe.call_args_list[1].kwargs["timeout"])

    def test_injected_origin_probe_receives_only_remaining_shutdown_budget(self):
        probe = unittest.mock.Mock(return_value=())
        runner = product_runner.ExtractionSubprocessRunner(
            origin_process_probe=probe,
            origin_shutdown_timeout=12.0,
            origin_shutdown_poll_interval=0.1,
        )

        with unittest.mock.patch.object(
            product_runner.time,
            "monotonic",
            side_effect=(0.0, 0.1, 1.0, 11.8, 12.1),
        ), unittest.mock.patch.object(product_runner.time, "sleep"):
            with self.assertRaisesRegex(product_runner.ExtractionCleanupBlockedError, "超时"):
                runner._wait_for_origin_shutdown()

        self.assertEqual(2, probe.call_count)
        self.assertEqual(5.0, probe.call_args_list[0].kwargs["timeout"])
        self.assertAlmostEqual(0.2, probe.call_args_list[1].kwargs["timeout"])

    def test_parent_rejects_later_child_mutating_accepted_source_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            context = _make_context(base, source_count=2)

            def process_factory(command, **kwargs):
                del kwargs
                manifest = json.loads(pathlib.Path(command[-2]).read_text(encoding="utf-8"))
                result_path = pathlib.Path(command[-1])
                snapshot_path = pathlib.Path(manifest["snapshot_path"])
                source_id = manifest["source_id"]
                source_summary = _record_valid_source(snapshot_path, context, source_id)
                if source_id == "S0002":
                    connection = sqlite3.connect(snapshot_path)
                    try:
                        connection.execute(
                            "update book_results set note_text = 'mutated by S0002' where source_id = 'S0001'"
                        )
                        connection.commit()
                    finally:
                        connection.close()
                return _CompletedChild(
                    result_path,
                    _summary_payload(snapshot_path, source_summary),
                    {"active": 0, "max_active": 0},
                )

            runner = product_runner.ExtractionSubprocessRunner(process_factory=process_factory)

            with self.assertRaisesRegex(product_runner.ProductRunnerError, "S0001|reconcil|checksum|payload"):
                runner(context)

    def test_parent_rejects_later_child_rewriting_prior_payload_with_valid_checksum(self):
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            context = _make_context(base, source_count=2)

            def process_factory(command, **kwargs):
                del kwargs
                manifest = json.loads(pathlib.Path(command[-2]).read_text(encoding="utf-8"))
                result_path = pathlib.Path(command[-1])
                snapshot_path = pathlib.Path(manifest["snapshot_path"])
                source_id = manifest["source_id"]
                source_summary = _record_valid_source(snapshot_path, context, source_id)
                if source_id == "S0002":
                    connection = sqlite3.connect(snapshot_path)
                    try:
                        row = connection.execute(
                            """
                            select status, rejection_reason, data_checksum, note_text, data_sheet_name,
                                   spectrum_class, available_columns_json, selected_y_column, paired_x_column,
                                   selected_x_values_json, selected_y_values_json, selected_x_row_count,
                                   selected_y_row_count, max_planned_y_json, max_planned_y_x_json,
                                   s1_max_for_limit_json, s1_limit_status
                            from book_results where source_id = 'S0001'
                            """
                        ).fetchone()
                        changed = (*row[:3], "valid but replaced", *row[4:])
                        checksum = run_snapshot_module._payload_checksum(*changed)
                        connection.execute(
                            "update book_results set note_text = ?, payload_checksum = ? where source_id = 'S0001'",
                            (changed[3], checksum),
                        )
                        connection.commit()
                    finally:
                        connection.close()
                return _CompletedChild(
                    result_path,
                    _summary_payload(snapshot_path, source_summary),
                    {"active": 0, "max_active": 0},
                )

            runner = product_runner.ExtractionSubprocessRunner(process_factory=process_factory)

            with self.assertRaisesRegex(product_runner.ProductRunnerError, "S0001|changed|partition|分区"):
                runner(context)

    def test_parent_rejects_later_child_rewriting_prior_worker_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            context = _make_context(base, source_count=2)

            def process_factory(command, **kwargs):
                del kwargs
                manifest = json.loads(pathlib.Path(command[-2]).read_text(encoding="utf-8"))
                result_path = pathlib.Path(command[-1])
                snapshot_path = pathlib.Path(manifest["snapshot_path"])
                source_id = manifest["source_id"]
                source_summary = _record_valid_source(snapshot_path, context, source_id)
                if source_id == "S0002":
                    connection = sqlite3.connect(snapshot_path)
                    try:
                        connection.execute(
                            "insert into worker_attempts (source_id, attempt, status, message) values ('S0001', 99, 'failed', 'rewritten')"
                        )
                        connection.commit()
                    finally:
                        connection.close()
                return _CompletedChild(
                    result_path,
                    _summary_payload(snapshot_path, source_summary),
                    {"active": 0, "max_active": 0},
                )

            runner = product_runner.ExtractionSubprocessRunner(process_factory=process_factory)

            with self.assertRaisesRegex(product_runner.ProductRunnerError, "S0001|changed|partition|分区"):
                runner(context)

    def test_parent_uses_one_fresh_serial_child_per_source_and_recomputes_final_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            context = _make_context(base, source_count=2)
            seen_source_ids = []
            activity = {"active": 0, "max_active": 0}

            def process_factory(command, **kwargs):
                del kwargs
                manifest_path = pathlib.Path(command[-2])
                result_path = pathlib.Path(command[-1])
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                source_id = manifest["source_id"]
                seen_source_ids.append(source_id)
                snapshot_path = pathlib.Path(manifest["snapshot_path"])
                source_summary = _record_valid_source(snapshot_path, context, source_id)
                return _CompletedChild(result_path, _summary_payload(snapshot_path, source_summary), activity)

            runner = product_runner.ExtractionSubprocessRunner(process_factory=process_factory)
            summary = runner(context)

            self.assertEqual(["S0001", "S0002"], seen_source_ids)
            self.assertEqual(1, activity["max_active"])
            self.assertEqual(2, summary.total_inventory_count)
            self.assertEqual(2, summary.total_result_count)
            self.assertEqual(2, summary.total_extracted_count)
            self.assertEqual(0, summary.total_rejected_count)
            self.assertEqual(("S0001", "S0002"), tuple(item.source_id for item in summary.source_summaries))
            allowed_names = {path.name for path in read_ownership(context.temp_root).allowed_children}
            self.assertIn("run_snapshot.sqlite3", allowed_names)

    def test_parent_rejects_successful_source_without_its_own_open_target_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            context = _make_context(base, source_count=2)

            def process_factory(command, **kwargs):
                del kwargs
                manifest = json.loads(pathlib.Path(command[-2]).read_text(encoding="utf-8"))
                result_path = pathlib.Path(command[-1])
                snapshot_path = pathlib.Path(manifest["snapshot_path"])
                source_id = manifest["source_id"]
                source_summary = _record_valid_source(
                    snapshot_path,
                    context,
                    source_id,
                    reader_attempt=1 if source_id == "S0001" else None,
                )
                return _CompletedChild(
                    result_path,
                    _summary_payload(snapshot_path, source_summary),
                    {"active": 0, "max_active": 0},
                )

            runner = product_runner.ExtractionSubprocessRunner(process_factory=process_factory)

            with self.assertRaises(product_runner.ProductRunnerError) as captured:
                runner(context)

            self.assertIn("raw-2.opju", str(captured.exception))
            self.assertIn("Origin", str(captured.exception))
            self.assertNotIn("S0002", str(captured.exception))

    def test_parent_retries_reader_process_failure_once_with_fresh_verified_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            context = _make_context(base)
            audit_dir = base / "runtime-audit"
            audit_dir.mkdir()
            attempts = []
            opened_copies = []

            class FailedChild:
                returncode = 7

                def communicate(self, timeout=None):
                    del timeout
                    return "", "reader crashed"

                def poll(self):
                    return self.returncode

            def process_factory(command, **kwargs):
                del kwargs
                manifest = json.loads(pathlib.Path(command[-2]).read_text(encoding="utf-8"))
                attempts.append(command)
                opened_copy = pathlib.Path(manifest["copy_path"])
                opened_copies.append(opened_copy)
                if len(attempts) == 1:
                    return FailedChild()
                _record_open_target(context, manifest["source_id"], 2, opened_copy)
                result_path = pathlib.Path(command[-1])
                snapshot_path = pathlib.Path(manifest["snapshot_path"])
                summary = _record_valid_source(
                    snapshot_path,
                    context,
                    manifest["source_id"],
                    copy_path=opened_copy,
                    reader_attempt=None,
                )
                return _CompletedChild(
                    result_path,
                    _summary_payload(snapshot_path, summary),
                    {"active": 0, "max_active": 0},
                )

            with unittest.mock.patch.dict(
                os.environ,
                {"SPECTRUM_ORGANIZER_RUNTIME_AUDIT_DIR": str(audit_dir)},
            ):
                summary = product_runner.ExtractionSubprocessRunner(
                    process_factory=process_factory
                )(context)

            self.assertEqual(2, len(attempts))
            self.assertNotEqual(opened_copies[0], opened_copies[1])
            self.assertFalse(opened_copies[0].exists())
            self.assertTrue(opened_copies[1].exists())
            self.assertIn(opened_copies[1], read_ownership(context.temp_root).allowed_children)
            self.assertEqual(
                (str(opened_copies[1].resolve()),),
                summary.worker_open_targets,
            )
            self.assertEqual(1, summary.total_extracted_count)
            audit_events = [
                json.loads(path.read_text(encoding="utf-8"))
                for path in audit_dir.glob("*.json")
            ]
            target_attempts = sorted(
                (
                    event["payload"]
                    for event in audit_events
                    if event["event_type"]
                    == "origin_extraction_target_attempt"
                ),
                key=lambda payload: payload["reader_attempt"],
            )
            self.assertEqual([1, 2], [item["reader_attempt"] for item in target_attempts])
            self.assertEqual(
                [str(path.resolve()) for path in opened_copies],
                [item["copy_path"] for item in target_attempts],
            )
            self.assertEqual(
                {
                    "run_id",
                    "source_id",
                    "reader_attempt",
                    "copy_path",
                    "copy_identity",
                },
                set(target_attempts[0]),
            )
            cleanup_events = [
                event["payload"]
                for event in audit_events
                if event["event_type"]
                == "origin_extraction_retry_cleanup"
            ]
            self.assertEqual(1, len(cleanup_events))
            self.assertEqual(1, cleanup_events[0]["reader_attempt"])
            self.assertEqual(str(opened_copies[0].resolve()), cleanup_events[0]["failed_copy_path"])
            self.assertEqual(str(opened_copies[1].resolve()), cleanup_events[0]["replacement_copy_path"])
            self.assertTrue(cleanup_events[0]["completed"])

    def test_parent_keeps_open_target_from_failed_post_open_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            context = _make_context(base)
            opened_copies = []

            class FailedChild:
                returncode = 7

                def communicate(self, timeout=None):
                    del timeout
                    return "", "reader crashed after open"

                def poll(self):
                    return self.returncode

            def process_factory(command, **kwargs):
                del kwargs
                manifest = json.loads(pathlib.Path(command[-2]).read_text(encoding="utf-8"))
                opened_copy = pathlib.Path(manifest["copy_path"])
                opened_copies.append(opened_copy)
                attempt = len(opened_copies)
                _record_open_target(context, manifest["source_id"], attempt, opened_copy)
                if attempt == 1:
                    return FailedChild()
                result_path = pathlib.Path(command[-1])
                snapshot_path = pathlib.Path(manifest["snapshot_path"])
                source_summary = _record_valid_source(
                    snapshot_path,
                    context,
                    manifest["source_id"],
                    copy_path=opened_copy,
                    reader_attempt=None,
                )
                return _CompletedChild(
                    result_path,
                    _summary_payload(snapshot_path, source_summary),
                    {"active": 0, "max_active": 0},
                )

            summary = product_runner.ExtractionSubprocessRunner(process_factory=process_factory)(context)

            self.assertEqual(
                tuple(str(path.resolve()) for path in opened_copies),
                summary.worker_open_targets,
            )

    def test_cancellation_during_final_snapshot_hash_remains_cancellation(self):
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            context = _make_context(base)
            activity = {"active": 0, "max_active": 0}

            def process_factory(command, **kwargs):
                del kwargs
                manifest = json.loads(pathlib.Path(command[-2]).read_text(encoding="utf-8"))
                result_path = pathlib.Path(command[-1])
                snapshot_path = pathlib.Path(manifest["snapshot_path"])
                source_summary = _record_valid_source(snapshot_path, context, manifest["source_id"])
                return _CompletedChild(
                    result_path,
                    _summary_payload(snapshot_path, source_summary),
                    activity,
                )

            runner = product_runner.ExtractionSubprocessRunner(process_factory=process_factory)

            def cancel_during_hash(_path, *, cancel_check=None):
                runner.cancel()
                cancel_check()
                self.fail("cancellation callback did not raise")

            with (
                unittest.mock.patch.object(
                    run_snapshot_module,
                    "snapshot_approval_sha256",
                    side_effect=cancel_during_hash,
                ),
                self.assertRaisesRegex(product_runner.ProductRunnerError, r"^谱图数据提取已取消$"),
            ):
                runner(context)

    def test_parent_retry_uses_next_absent_copy_name_when_child_left_retry_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            context = _make_context(base)
            opened_copies = []

            class FailedChild:
                returncode = 7

                def communicate(self, timeout=None):
                    del timeout
                    return "", "reader crashed after internal retry"

                def poll(self):
                    return self.returncode

            def process_factory(command, **kwargs):
                del kwargs
                manifest = json.loads(pathlib.Path(command[-2]).read_text(encoding="utf-8"))
                opened_copy = pathlib.Path(manifest["copy_path"])
                opened_copies.append(opened_copy)
                if len(opened_copies) == 1:
                    leftover = opened_copy.with_name(f"{opened_copy.stem}.retry{opened_copy.suffix}")
                    leftover.write_bytes(context.selected_source_paths[0].read_bytes())
                    return FailedChild()
                result_path = pathlib.Path(command[-1])
                snapshot_path = pathlib.Path(manifest["snapshot_path"])
                source_summary = _record_valid_source(
                    snapshot_path,
                    context,
                    manifest["source_id"],
                    copy_path=opened_copy,
                    reader_attempt=2,
                )
                return _CompletedChild(
                    result_path,
                    _summary_payload(snapshot_path, source_summary),
                    {"active": 0, "max_active": 0},
                )

            summary = product_runner.ExtractionSubprocessRunner(process_factory=process_factory)(context)

            self.assertEqual(2, len(opened_copies))
            self.assertEqual("raw-1.retry2.opju", opened_copies[1].name)
            self.assertEqual(1, summary.total_extracted_count)

    def test_parent_does_not_retry_structured_child_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            context = _make_context(pathlib.Path(directory))
            calls = []

            class RejectedChild:
                returncode = 1

                def __init__(self, result_path):
                    result_path.write_text(
                        json.dumps(
                            {
                                "ok": False,
                                "error": "zero recognizable supported Books",
                                "error_type": "ProductRunnerError",
                                "error_notes": ["secondary reader diagnostic"],
                            }
                        ),
                        encoding="utf-8",
                    )
                    self._stdout = _child_creation_stdout(result_path)

                def communicate(self, timeout=None):
                    del timeout
                    return self._stdout, ""

                def poll(self):
                    return self.returncode

            def process_factory(command, **kwargs):
                del kwargs
                calls.append(command)
                return RejectedChild(pathlib.Path(command[-1]))

            with self.assertRaisesRegex(
                product_runner.ProductRunnerError,
                "zero recognizable",
            ) as captured:
                product_runner.ExtractionSubprocessRunner(process_factory=process_factory)(context)

            self.assertEqual(1, len(calls))
            self.assertIn("secondary reader diagnostic", captured.exception.__notes__)

    def test_parent_skips_unsupported_source_and_preserves_valid_source_results(self):
        with tempfile.TemporaryDirectory() as directory:
            context = _make_context(pathlib.Path(directory), source_count=2)
            events = []

            def process_factory(command, **kwargs):
                del kwargs
                manifest = json.loads(pathlib.Path(command[-2]).read_text(encoding="utf-8"))
                result_path = pathlib.Path(command[-1])
                if manifest["source_id"] == "S0001":
                    snapshot_path = pathlib.Path(manifest["snapshot_path"])
                    source_summary = _record_valid_source(
                        snapshot_path,
                        context,
                        "S0001",
                    )
                    return _CompletedChild(
                        result_path,
                        _summary_payload(snapshot_path, source_summary),
                        {"active": 0, "max_active": 0},
                    )
                _record_open_target(
                    context,
                    "S0002",
                    1,
                    context.run_owned_source_copy_paths[1],
                )
                return _RejectedChild(
                    result_path,
                    error="内部 source id 不应展示给用户：S0002",
                    error_type="UnsupportedSourceInputError",
                )

            runner = product_runner.ExtractionSubprocessRunner(process_factory=process_factory)
            runner.set_progress_callback(events.append)

            summary = runner(context)

            self.assertEqual(1, summary.total_inventory_count)
            self.assertEqual(("S0001",), tuple(item.source_id for item in summary.source_summaries))
            self.assertEqual(1, len(summary.source_input_issues))
            issue = summary.source_input_issues[0]
            self.assertEqual(str(context.selected_source_paths[1]), issue.original_path)
            self.assertNotIn("S0002", issue.reason)
            self.assertIn("未检测到受支持的 Origin 原始谱图", issue.reason)
            self.assertIn("重新选择", issue.recommendation)
            self.assertEqual(
                ["source_started", "source_completed", "source_started", "source_skipped", "batch_completed"],
                [event["kind"] for event in events],
            )
            self.assertEqual(1, events[3]["total_inventory_count"])
            self.assertEqual(1, events[3]["total_extracted_count"])

    def test_parent_treats_missing_open_target_for_unsupported_source_as_fatal(self):
        with tempfile.TemporaryDirectory() as directory:
            context = _make_context(pathlib.Path(directory))

            def process_factory(command, **kwargs):
                del kwargs
                return _RejectedChild(
                    pathlib.Path(command[-1]),
                    error="zero recognizable supported Books",
                    error_type="UnsupportedSourceInputError",
                )

            with self.assertRaisesRegex(
                product_runner.ProductRunnerError,
                "Origin.*打开",
            ) as captured:
                product_runner.ExtractionSubprocessRunner(
                    process_factory=process_factory
                )(context)

            self.assertNotIsInstance(
                captured.exception,
                product_runner.AllSelectedSourcesInvalidError,
            )
            message = str(captured.exception)
            self.assertIn("raw-1.opju", message)
            self.assertIn("为保护原始数据", message)
            self.assertIn("关闭残留 Origin 进程后重试", message)
            self.assertNotIn("S0001", message)

    def test_parent_reports_all_unsupported_sources_in_one_chinese_error(self):
        with tempfile.TemporaryDirectory() as directory:
            context = _make_context(pathlib.Path(directory), source_count=2)

            def process_factory(command, **kwargs):
                del kwargs
                manifest = json.loads(pathlib.Path(command[-2]).read_text(encoding="utf-8"))
                source_id = manifest["source_id"]
                source_index = int(source_id[1:]) - 1
                _record_open_target(
                    context,
                    source_id,
                    1,
                    context.run_owned_source_copy_paths[source_index],
                )
                return _RejectedChild(
                    pathlib.Path(command[-1]),
                    error=f"Source has zero recognizable supported raw-spectrum Books: {source_id}",
                    error_type="UnsupportedSourceInputError",
                )

            with self.assertRaises(product_runner.AllSelectedSourcesInvalidError) as captured:
                product_runner.ExtractionSubprocessRunner(process_factory=process_factory)(context)

            error = captured.exception
            self.assertEqual(2, len(error.source_input_issues))
            self.assertEqual(
                "所选 2 个输入文件均未进入后续流程。",
                str(error),
            )
            self.assertNotIn("S0001", str(error))
            self.assertNotIn("S0002", str(error))
            self.assertNotIn("raw-1.opju", str(error))
            self.assertNotIn("建议", str(error))

    def test_parent_retries_structured_infrastructure_failure_in_fresh_process(self):
        with tempfile.TemporaryDirectory() as directory:
            context = _make_context(pathlib.Path(directory))
            calls = []

            class Child:
                returncode = 1

                def __init__(self, result_path, attempt):
                    if attempt == 1:
                        payload = {
                            "ok": False,
                            "error": "Origin data session failed: RPC_E_DISCONNECTED",
                            "error_type": "InfrastructureExtractionError",
                            "error_notes": [],
                        }
                    else:
                        snapshot_path = pathlib.Path(context.temp_root) / "run_snapshot.sqlite3"
                        source_summary = _record_valid_source(
                            snapshot_path,
                            context,
                            "S0001",
                            copy_path=pathlib.Path(context.run_owned_source_copy_paths[0]).with_name("raw-1.retry.opju"),
                            reader_attempt=2,
                        )
                        payload = {"ok": True, "summary": _summary_payload(snapshot_path, source_summary)}
                        self.returncode = 0
                    result_path.write_text(json.dumps(payload), encoding="utf-8")
                    self._stdout = _child_creation_stdout(result_path)

                def communicate(self, timeout=None):
                    del timeout
                    return self._stdout, ""

                def poll(self):
                    return self.returncode

            def process_factory(command, **kwargs):
                del kwargs
                calls.append(command)
                return Child(pathlib.Path(command[-1]), len(calls))

            summary = product_runner.ExtractionSubprocessRunner(process_factory=process_factory)(context)

            self.assertEqual(2, len(calls))
            self.assertEqual(1, summary.total_extracted_count)

    def test_reader_command_carries_parent_attempt_number(self):
        with tempfile.TemporaryDirectory() as directory:
            context = _make_context(pathlib.Path(directory))
            snapshot_path = pathlib.Path(context.temp_root) / "run_snapshot.sqlite3"
            ownership = read_ownership(context.temp_root)
            if snapshot_path not in ownership.allowed_children:
                add_allowed_child(ownership, snapshot_path)
            source = product_runner._build_extraction_sources(context, ExtractionSource)[0]

            command = product_runner._build_reader_process_command(
                context,
                source,
                snapshot_path,
                reader_attempt=2,
            )
            payload = product_runner._reader_command_to_payload(command)

            self.assertEqual(2, command.reader_attempt)
            self.assertEqual(2, payload["reader_attempt"])
            self.assertEqual(2, product_runner._reader_command_from_payload(payload).reader_attempt)

    def test_pre_extraction_result_envelope_is_strict(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "result.json"
            malformed = (
                {"ok": "yes", "context": {}},
                {"ok": True, "context": {}, "unexpected": True},
                {"ok": False, "error": "failed"},
            )
            for payload in malformed:
                with self.subTest(payload=payload):
                    path.write_text(json.dumps(payload), encoding="utf-8")
                    with self.assertRaises(product_runner.ProductRunnerError):
                        product_runner._read_process_payload(path, "提取前复制子进程")

    def test_pre_extraction_refuses_manifest_replacement_before_identity_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = root / "raw.opju"
            source.write_bytes(b"source")
            parked = root / "parked-owned-manifest.json"
            foreign = {}

            class Child:
                returncode = 0

                def __init__(self, manifest_path, result_path):
                    self.manifest_path = pathlib.Path(manifest_path)
                    self.result_path = pathlib.Path(result_path)

                def communicate(self, timeout=None):
                    del timeout
                    self.manifest_path.rename(parked)
                    self.manifest_path.write_text("FOREIGN", encoding="utf-8")
                    foreign["path"] = self.manifest_path
                    self.result_path.write_text("{}", encoding="utf-8")
                    identity = product_runner.path_identity(self.result_path)
                    return json.dumps(
                        {
                            "result_identity": list(identity),
                            "result_sha256": hashlib.sha256(
                                self.result_path.read_bytes()
                            ).hexdigest(),
                            "created_temp_identities": [],
                        }
                    ), ""

                def poll(self):
                    return self.returncode

            def process_factory(command, **_kwargs):
                return Child(command[-2], command[-1])

            runner = product_runner.PreExtractionSubprocessRunner(
                local_appdata=root / "localapp",
                process_factory=process_factory,
            )
            context = object()

            with unittest.mock.patch.object(
                runner,
                "_wait_for_termination_process",
            ), unittest.mock.patch.object(
                runner,
                "_validate_context",
            ), unittest.mock.patch.object(
                product_runner,
                "_read_process_payload",
                return_value={"ok": True, "context": {}},
            ), unittest.mock.patch.object(
                product_runner,
                "_context_from_payload",
                return_value=context,
            ), self.assertRaises(product_runner.ProductRunnerError):
                runner(
                    selected_source_paths=(source,),
                    output_parent=root / "out",
                    settings_snapshot={
                        "s1Limit": 1_000_000,
                        "steadyEmissionY": "S1c",
                    },
                )

            self.assertEqual("FOREIGN", foreign["path"].read_text(encoding="utf-8"))
            self.assertTrue(parked.exists())

    def test_pre_extraction_failure_routes_dangling_root_to_cleanup_refusal(self):
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            source = base / "raw.opju"
            source.write_bytes(b"source")
            captured = {}
            original_exists = pathlib.Path.exists
            original_is_symlink = pathlib.Path.is_symlink

            def fail_launch(command, **_kwargs):
                captured["root"] = pathlib.Path(command[-2]).parent
                raise product_runner.ProductRunnerError("child launch failed")

            def exists(path):
                current = pathlib.Path(path)
                if current == captured.get("root"):
                    return False
                return original_exists(current)

            def is_symlink(path):
                current = pathlib.Path(path)
                if current == captured.get("root"):
                    return True
                return original_is_symlink(current)

            runner = product_runner.PreExtractionSubprocessRunner(
                local_appdata=base / "localapp",
                process_factory=fail_launch,
            )
            with unittest.mock.patch.object(
                pathlib.Path,
                "exists",
                autospec=True,
                side_effect=exists,
            ), unittest.mock.patch.object(
                pathlib.Path,
                "is_symlink",
                autospec=True,
                side_effect=is_symlink,
            ), unittest.mock.patch.object(
                product_runner,
                "_cleanup_temp_root_error",
                return_value="dangling root refused",
            ) as cleanup, self.assertRaisesRegex(
                product_runner.ExtractionCleanupBlockedError,
                "dangling root refused",
            ):
                runner(
                    selected_source_paths=(source,),
                    output_parent=base / "out",
                    settings_snapshot={
                        "s1Limit": 1_000_000,
                        "steadyEmissionY": "S1c",
                    },
                )

            cleanup.assert_called_once()
            self.assertEqual(captured["root"], cleanup.call_args.args[0])

    def test_pre_extraction_context_payload_requires_exact_native_types(self):
        with tempfile.TemporaryDirectory() as directory:
            context = _make_context(pathlib.Path(directory))
            valid = product_runner._context_to_payload(context)
            malformed = []
            with_extra = dict(valid)
            with_extra["unexpected"] = True
            malformed.append(with_extra)
            with_string_size = json.loads(json.dumps(valid))
            with_string_size["source_fingerprints_before"][0]["size_bytes"] = str(
                with_string_size["source_fingerprints_before"][0]["size_bytes"]
            )
            malformed.append(with_string_size)
            with_string_mtime = json.loads(json.dumps(valid))
            with_string_mtime["source_fingerprints_before"][0]["mtime_ns"] = str(
                with_string_mtime["source_fingerprints_before"][0]["mtime_ns"]
            )
            malformed.append(with_string_mtime)

            for payload in malformed:
                with self.subTest(payload=payload):
                    with self.assertRaises(product_runner.ProductRunnerError):
                        product_runner._context_from_payload(payload)

    def test_parent_retries_structured_worker_shutdown_failure_in_fresh_process(self):
        with tempfile.TemporaryDirectory() as directory:
            context = _make_context(pathlib.Path(directory))
            calls = []
            events = []

            class Child:
                returncode = 1

                def __init__(self, result_path, attempt):
                    if attempt == 1:
                        payload = {
                            "ok": False,
                            "error": "Origin worker close could not be confirmed",
                            "error_type": "WorkerShutdownUnconfirmedError",
                            "error_notes": [],
                        }
                    else:
                        snapshot_path = pathlib.Path(context.temp_root) / "run_snapshot.sqlite3"
                        source_summary = _record_valid_source(
                            snapshot_path,
                            context,
                            "S0001",
                            copy_path=pathlib.Path(context.run_owned_source_copy_paths[0]).with_name(
                                "raw-1.retry.opju"
                            ),
                            reader_attempt=2,
                        )
                        payload = {"ok": True, "summary": _summary_payload(snapshot_path, source_summary)}
                        self.returncode = 0
                    result_path.write_text(json.dumps(payload), encoding="utf-8")
                    self._stdout = _child_creation_stdout(result_path)

                def communicate(self, timeout=None):
                    del timeout
                    return self._stdout, ""

                def poll(self):
                    return self.returncode

            def process_factory(command, **kwargs):
                del kwargs
                calls.append(command)
                if len(calls) == 2:
                    self.assertEqual(
                        ["first_child_launch", "origin_empty", "origin_empty", "job_close"],
                        events,
                    )
                else:
                    events.append("first_child_launch")
                return Child(pathlib.Path(command[-1]), len(calls))

            def origin_probe(*, timeout):
                del timeout
                events.append("origin_empty")
                return ()

            with unittest.mock.patch.object(
                product_runner,
                "close_bound_process_job",
                side_effect=lambda _process: events.append("job_close"),
            ):
                summary = product_runner.ExtractionSubprocessRunner(
                    process_factory=process_factory,
                    origin_process_probe=origin_probe,
                )(context)

            self.assertEqual(2, len(calls))
            self.assertEqual(1, summary.total_extracted_count)

    def test_parent_revalidates_original_before_second_reader_process(self):
        with tempfile.TemporaryDirectory() as directory:
            context = _make_context(pathlib.Path(directory))
            calls = []

            class FailedChild:
                returncode = 8

                def communicate(self, timeout=None):
                    del timeout
                    return "", "reader crashed"

                def poll(self):
                    return self.returncode

            def process_factory(command, **kwargs):
                del command, kwargs
                calls.append(1)
                context.selected_source_paths[0].write_bytes(b"changed")
                return FailedChild()

            with self.assertRaisesRegex(Exception, "changed"):
                product_runner.ExtractionSubprocessRunner(process_factory=process_factory)(context)

            self.assertEqual([1], calls)

    def test_parent_reports_both_reader_process_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            context = _make_context(pathlib.Path(directory))
            calls = []

            class FailedChild:
                returncode = 1

                def __init__(self, result_path, message, note):
                    result_path.write_text(
                        json.dumps(
                            {
                                "ok": False,
                                "error": message,
                                "error_type": "InfrastructureExtractionError",
                                "error_notes": [note],
                            }
                        ),
                        encoding="utf-8",
                    )
                    self._stdout = _child_creation_stdout(result_path)

                def communicate(self, timeout=None):
                    del timeout
                    return self._stdout, ""

                def poll(self):
                    return self.returncode

            def process_factory(command, **kwargs):
                del kwargs
                calls.append(len(calls) + 1)
                return FailedChild(
                    pathlib.Path(command[-1]),
                    f"reader failure {calls[-1]}",
                    f"secondary reader failure {calls[-1]}",
                )

            with self.assertRaisesRegex(
                product_runner.ProductRunnerError,
                r"attempt 1.*reader failure 1.*attempt 2.*reader failure 2",
            ) as captured:
                product_runner.ExtractionSubprocessRunner(process_factory=process_factory)(context)

            self.assertEqual([1, 2], calls)
            self.assertEqual(
                [
                    "attempt 1: secondary reader failure 1",
                    "attempt 2: secondary reader failure 2",
                ],
                getattr(captured.exception, "__notes__", []),
            )

    def test_first_infrastructure_reader_note_survives_second_product_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            context = _make_context(pathlib.Path(directory))
            calls = []

            class FailedChild:
                returncode = 1

                def __init__(self, result_path, attempt):
                    error_type = (
                        "InfrastructureExtractionError"
                        if attempt == 1
                        else "ProductRunnerError"
                    )
                    result_path.write_text(
                        json.dumps(
                            {
                                "ok": False,
                                "error": f"reader failure {attempt}",
                                "error_type": error_type,
                                "error_notes": [f"reader note {attempt}"],
                            }
                        ),
                        encoding="utf-8",
                    )
                    self._stdout = _child_creation_stdout(result_path)

                def communicate(self, timeout=None):
                    del timeout
                    return self._stdout, ""

                def poll(self):
                    return self.returncode

            def process_factory(command, **kwargs):
                del kwargs
                calls.append(len(calls) + 1)
                return FailedChild(pathlib.Path(command[-1]), calls[-1])

            with self.assertRaisesRegex(
                product_runner.ProductRunnerError,
                "reader failure 2",
            ) as captured:
                product_runner.ExtractionSubprocessRunner(
                    process_factory=process_factory
                )(context)

            self.assertEqual([1, 2], calls)
            self.assertEqual(
                ["attempt 1: reader note 1", "reader note 2"],
                getattr(captured.exception, "__notes__", []),
            )

    def test_runner_emits_per_source_and_overall_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            context = _make_context(base, source_count=2)
            events = []

            def process_factory(command, **kwargs):
                del kwargs
                manifest = json.loads(pathlib.Path(command[-2]).read_text(encoding="utf-8"))
                result_path = pathlib.Path(command[-1])
                snapshot_path = pathlib.Path(manifest["snapshot_path"])
                summary = _record_valid_source(snapshot_path, context, manifest["source_id"])
                return _CompletedChild(
                    result_path,
                    _summary_payload(snapshot_path, summary),
                    {"active": 0, "max_active": 0},
                )

            runner = product_runner.ExtractionSubprocessRunner(process_factory=process_factory)
            runner.set_progress_callback(events.append)
            runner(context)

            self.assertEqual(
                ["source_started", "source_completed", "source_started", "source_completed", "batch_completed"],
                [event["kind"] for event in events],
            )
            self.assertEqual((1, 2), (events[0]["source_index"], events[0]["source_total"]))
            self.assertEqual(
                (1, 1, 0),
                (
                    events[2]["total_inventory_count"],
                    events[2]["total_extracted_count"],
                    events[2]["total_rejected_count"],
                ),
            )
            self.assertEqual((2, 2), (events[3]["completed_sources"], events[3]["source_total"]))
            self.assertEqual(2, events[-1]["total_inventory_count"])

    def test_reader_manifest_and_temp_root_expose_no_parent_only_source_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            context = _make_context(base)
            original_text = str(context.selected_source_paths[0])
            ownership = read_ownership(context.temp_root)
            sensitive_paths = (
                context.temp_root / "pre_extraction_context.json",
                context.temp_root / "pre_extraction_result.json",
            )
            for path in sensitive_paths:
                path.write_text(original_text, encoding="utf-8")
                ownership = add_allowed_child(ownership, path)
            ownership = write_ownership(
                replace(
                    ownership,
                    protected_paths=(context.selected_source_paths[0],),
                )
            )

            def process_factory(command, **kwargs):
                del kwargs
                manifest_path = pathlib.Path(command[-2])
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                self.assertEqual(
                    {
                        "run_id",
                        "marker_id",
                        "source_id",
                        "copy_path",
                        "copy_sha256",
                        "copy_size_bytes",
                        "copy_device_id",
                        "copy_file_id",
                        "settings_snapshot",
                        "snapshot_path",
                        "required_temp_bytes",
                        "reader_attempt",
                    },
                    set(manifest),
                )
                self.assertNotIn(original_text, manifest_path.read_text(encoding="utf-8"))
                self.assertTrue(all(not path.exists() for path in sensitive_paths))
                reader_ownership = read_ownership(context.temp_root)
                self.assertEqual((), reader_ownership.protected_paths)
                allowed_names = {path.name for path in reader_ownership.allowed_children}
                self.assertIn("origin_launch.S0001.attempt1.json.pending", allowed_names)
                self.assertIn("origin_identity.S0001.attempt1.json.pending", allowed_names)
                self.assertIn("origin_identity.S0001.attempt1.c", allowed_names)
                self.assertIn("origin_open_target.S0001.attempt1.json", allowed_names)
                self.assertIn("origin_open_target.S0001.attempt1.json.pending", allowed_names)
                self.assertIn("extraction_result.S0001.attempt1.json.pending", allowed_names)
                snapshot_path = pathlib.Path(manifest["snapshot_path"])
                summary = _record_valid_source(snapshot_path, context, manifest["source_id"])
                child_summary = {
                    "snapshot_path": str(snapshot_path),
                    "source_id": manifest["source_id"],
                    "inventory_count": summary["inventory_count"],
                    "result_count": summary["result_count"],
                    "extracted_count": summary["extracted_count"],
                    "rejected_count": summary["rejected_count"],
                }
                return _CompletedChild(
                    pathlib.Path(command[-1]),
                    child_summary,
                    {"active": 0, "max_active": 0},
                )

            summary = product_runner.ExtractionSubprocessRunner(process_factory=process_factory)(context)

            self.assertEqual(1, summary.total_inventory_count)

    def test_reader_command_copy_hash_uses_parent_cancellation_callback(self):
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            context = _make_context(base)
            product_runner._prepare_reader_temp_root(context)
            snapshot_path = product_runner._register_snapshot_path(
                context,
                context.temp_root / "run_snapshot.sqlite3",
            )
            source = product_runner._build_extraction_sources(context, ExtractionSource)[0]
            cancel_check = unittest.mock.Mock()
            real_hash_file = product_runner.hash_file

            with unittest.mock.patch.object(product_runner, "hash_file") as hash_file:
                hash_file.side_effect = lambda path, *, cancel_check=None: real_hash_file(
                    path,
                    cancel_check=cancel_check,
                )
                product_runner._build_reader_process_command(
                    context,
                    source,
                    snapshot_path,
                    cancel_check=cancel_check,
                )

            self.assertIs(hash_file.call_args.kwargs["cancel_check"], cancel_check)
            self.assertGreater(cancel_check.call_count, 0)

    def test_multi_source_validation_is_incremental_then_runs_one_final_full_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            context = _make_context(base, source_count=2)

            def process_factory(command, **kwargs):
                del kwargs
                manifest = json.loads(pathlib.Path(command[-2]).read_text(encoding="utf-8"))
                result_path = pathlib.Path(command[-1])
                snapshot_path = pathlib.Path(manifest["snapshot_path"])
                summary = _record_valid_source(snapshot_path, context, manifest["source_id"])
                return _CompletedChild(
                    result_path,
                    _summary_payload(snapshot_path, summary),
                    {"active": 0, "max_active": 0},
                )

            record_pass_sizes = []
            summary_pass_sizes = []
            real_validate_records = product_runner._validate_snapshot_source_records
            real_summarize = product_runner._summarize_extraction

            def validate_records(snapshot_path, approved_context, sources, **kwargs):
                record_pass_sizes.append(len(sources))
                return real_validate_records(snapshot_path, approved_context, sources, **kwargs)

            def summarize(snapshot, sources, **kwargs):
                summary_pass_sizes.append(len(sources))
                return real_summarize(snapshot, sources, **kwargs)

            with unittest.mock.patch.object(
                product_runner,
                "_validate_snapshot_source_records",
                side_effect=validate_records,
            ), unittest.mock.patch.object(
                product_runner,
                "_summarize_extraction",
                side_effect=summarize,
            ):
                product_runner.ExtractionSubprocessRunner(process_factory=process_factory)(context)

            self.assertEqual([1, 1, 2], record_pass_sizes)
            self.assertEqual([1, 1, 2], summary_pass_sizes)

    def test_parent_confirms_origin_shutdown_before_starting_next_source(self):
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            context = _make_context(base, source_count=2)
            seen_source_ids = []
            probe_calls = []

            def probe(*, timeout):
                del timeout
                probe_calls.append(len(seen_source_ids))
                return ()

            def process_factory(command, **kwargs):
                del kwargs
                manifest = json.loads(pathlib.Path(command[-2]).read_text(encoding="utf-8"))
                source_id = manifest["source_id"]
                if source_id == "S0002":
                    self.assertGreaterEqual(len(probe_calls), 2)
                seen_source_ids.append(source_id)
                result_path = pathlib.Path(command[-1])
                snapshot_path = pathlib.Path(manifest["snapshot_path"])
                summary = _record_valid_source(snapshot_path, context, source_id)
                return _CompletedChild(result_path, _summary_payload(snapshot_path, summary), {"active": 0, "max_active": 0})

            runner = product_runner.ExtractionSubprocessRunner(
                process_factory=process_factory,
                origin_process_probe=probe,
                origin_shutdown_poll_interval=0.001,
            )
            runner(context)

            self.assertEqual(["S0001", "S0002"], seen_source_ids)
            self.assertEqual(4, len(probe_calls))

    def test_surviving_origin_after_first_source_blocks_second_and_preserves_temp_root(self):
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            context = _make_context(base, source_count=2)
            seen_source_ids = []

            def process_factory(command, **kwargs):
                del kwargs
                manifest = json.loads(pathlib.Path(command[-2]).read_text(encoding="utf-8"))
                source_id = manifest["source_id"]
                seen_source_ids.append(source_id)
                result_path = pathlib.Path(command[-1])
                snapshot_path = pathlib.Path(manifest["snapshot_path"])
                summary = _record_valid_source(snapshot_path, context, source_id)
                return _CompletedChild(result_path, _summary_payload(snapshot_path, summary), {"active": 0, "max_active": 0})

            runner = product_runner.ExtractionSubprocessRunner(
                process_factory=process_factory,
                origin_process_probe=lambda *, timeout: (types.SimpleNamespace(pid=7654),),
                origin_shutdown_timeout=0.03,
                origin_shutdown_poll_interval=0.005,
            )

            with self.assertRaisesRegex(product_runner.ExtractionCleanupBlockedError, "7654"):
                runner(context)

            self.assertEqual(["S0001"], seen_source_ids)
            self.assertTrue(context.temp_root.exists())
            self.assertIn("7654", runner._cleanup_blocked_reason)
            with self.assertRaisesRegex(product_runner.ProductRunnerError, "清理状态不可确认"):
                runner.reset()
            with self.assertRaisesRegex(product_runner.ProductRunnerError, "清理状态不可确认"):
                runner(context)

    def test_cancel_publishes_termination_helper_before_cancel_state(self):
        runner = product_runner.ExtractionSubprocessRunner()
        runner._current_process = object()
        terminate_entered = threading.Event()
        release_terminate = threading.Event()
        helper = object()

        def terminate(_process):
            terminate_entered.set()
            release_terminate.wait(1)
            return helper

        with unittest.mock.patch.object(product_runner, "_terminate_process_nonblocking", side_effect=terminate):
            thread = threading.Thread(target=runner.cancel)
            thread.start()
            self.assertTrue(terminate_entered.wait(1))
            self.assertFalse(runner._cancelled.is_set())
            release_terminate.set()
            thread.join(1)

        self.assertFalse(thread.is_alive())
        self.assertTrue(runner._cancelled.is_set())
        self.assertIs(helper, runner._termination_process)

    def test_repeated_cancel_does_not_replace_running_termination_helper(self):
        class RunningHelper:
            def is_alive(self):
                return True

        runner = product_runner.ExtractionSubprocessRunner()
        runner._current_process = object()
        helper = RunningHelper()

        with unittest.mock.patch.object(
            product_runner,
            "_terminate_process_nonblocking",
            return_value=helper,
        ) as terminate:
            runner.cancel()
            runner.cancel()

        terminate.assert_called_once()
        self.assertIs(helper, runner._termination_process)

    def test_post_launch_cancel_check_reuses_helper_registered_by_cancel(self):
        runner = product_runner.ExtractionSubprocessRunner()
        process = object()
        helper = object()
        runner._current_process = process

        with unittest.mock.patch.object(
            product_runner,
            "_terminate_process_nonblocking",
            return_value=helper,
        ) as terminate:
            runner.cancel()
            with runner._state_lock:
                runner._request_termination_locked(process)

        terminate.assert_called_once_with(process)
        self.assertIs(helper, runner._termination_process)

    def test_reset_is_blocked_while_termination_helper_is_still_running(self):
        class RunningHelper:
            def is_alive(self):
                return True

        runner = product_runner.ExtractionSubprocessRunner()
        runner._termination_process = RunningHelper()

        with self.assertRaisesRegex(product_runner.ProductRunnerError, "终止线程仍在运行"):
            runner.reset()

        self.assertIsNotNone(runner._termination_process)

    def test_new_run_is_blocked_while_termination_helper_is_still_running(self):
        class RunningHelper:
            def is_alive(self):
                return True

        runner = product_runner.ExtractionSubprocessRunner()
        runner._termination_process = RunningHelper()

        with self.assertRaisesRegex(product_runner.ProductRunnerError, "终止线程仍在运行"):
            runner(object())

        self.assertFalse(runner._active)

    def test_parent_rejects_untrusted_child_summary_against_context_and_sqlite(self):
        def external_snapshot(payload, base):
            payload["snapshot_path"] = str(base / "forged.sqlite3")

        def missing_source(payload, _base):
            del payload["source_id"]

        def duplicate_source(payload, _base):
            payload["source_summaries"] = [{"source_id": payload["source_id"]}]

        def extra_source(payload, _base):
            payload["source_id"] = "S9999"

        def forged_original(payload, base):
            payload["original_path"] = str(base / "other.opju")

        def forged_copy(payload, base):
            payload["copy_path"] = str(base / "outside.opju")

        def wrong_source_count(payload, _base):
            payload["inventory_count"] = 2

        def wrong_total(payload, _base):
            payload["extracted_count"] = 2

        def forged_sqlite_sha(payload, _base):
            connection = sqlite3.connect(payload["snapshot_path"])
            try:
                connection.execute("update source_files set sha256 = 'forged'")
                connection.commit()
            finally:
                connection.close()

        def unregister_snapshot(payload, _base):
            snapshot_path = pathlib.Path(payload["snapshot_path"])
            ownership_path = snapshot_path.parent / "ownership.json"
            ownership_payload = json.loads(ownership_path.read_text(encoding="utf-8"))
            ownership_payload["allowed_children"].remove(str(snapshot_path))
            ownership_path.write_text(json.dumps(ownership_payload), encoding="utf-8")

        def tampered_copy(payload, _base):
            context.run_owned_source_copy_paths[0].write_bytes(b"tampered")

        cases = {
            "external snapshot": external_snapshot,
            "missing source": missing_source,
            "duplicate source": duplicate_source,
            "extra source": extra_source,
            "forged original": forged_original,
            "forged copy": forged_copy,
            "wrong source count": wrong_source_count,
            "wrong total": wrong_total,
            "forged sqlite sha": forged_sqlite_sha,
            "unregistered snapshot": unregister_snapshot,
            "tampered copy": tampered_copy,
        }
        for label, mutate in cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                base = pathlib.Path(directory)
                context = _make_context(base)

                def process_factory(command, **kwargs):
                    del kwargs
                    manifest = json.loads(pathlib.Path(command[-2]).read_text(encoding="utf-8"))
                    result_path = pathlib.Path(command[-1])
                    snapshot_path = pathlib.Path(manifest["snapshot_path"])
                    source_summary = _record_valid_source(snapshot_path, context, manifest["source_id"])
                    payload = _summary_payload(snapshot_path, source_summary)
                    mutate(payload, base)
                    return _CompletedChild(result_path, payload, {"active": 0, "max_active": 0})

                runner = product_runner.ExtractionSubprocessRunner(process_factory=process_factory)
                with self.assertRaises(product_runner.ProductRunnerError):
                    runner(context)

    def test_cancel_terminates_active_child_and_cleans_owned_temp_root(self):
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            context = _make_context(base)
            started = threading.Event()
            stopped = threading.Event()

            class BlockingChild:
                returncode = None

                def communicate(self, timeout=None):
                    started.set()
                    if not stopped.wait(timeout):
                        raise product_runner.subprocess.TimeoutExpired("fake", timeout)
                    return "", ""

                def poll(self):
                    return self.returncode

                def terminate(self):
                    self.returncode = -1
                    stopped.set()

            child = BlockingChild()
            runner = product_runner.ExtractionSubprocessRunner(process_factory=lambda command, **kwargs: child)
            errors = []
            thread = threading.Thread(target=lambda: self._capture_error(errors, runner, context))
            thread.start()
            self.assertTrue(started.wait(2))

            runner.cancel()
            thread.join(5)

            self.assertFalse(thread.is_alive())
            self.assertEqual(-1, child.returncode)
            self.assertFalse(context.temp_root.exists())
            self.assertRegex(str(errors[0]), "取消")

    def test_cancel_returns_bounded_error_when_pipe_and_termination_never_finish(self):
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            context = _make_context(base)
            started = threading.Event()

            class StuckChild:
                returncode = None
                pid = 12345

                def communicate(self, timeout=None):
                    started.set()
                    if timeout is None:
                        raise AssertionError("cancelled communicate must be bounded")
                    time.sleep(min(timeout, 0.01))
                    raise product_runner.subprocess.TimeoutExpired("fake", timeout)

                def poll(self):
                    return None

                def terminate(self):
                    return None

                def kill(self):
                    return None

            runner = product_runner.ExtractionSubprocessRunner(
                process_factory=lambda command, **kwargs: StuckChild(),
                cancellation_timeout=0.08,
                cancellation_poll_interval=0.01,
            )
            errors = []
            thread = threading.Thread(target=lambda: self._capture_error(errors, runner, context))
            thread.start()
            self.assertTrue(started.wait(5))

            runner.cancel()
            thread.join(1)

            self.assertFalse(thread.is_alive())
            self.assertRegex(str(errors[0]), "取消.*超时|无法.*取消")

    def test_cancel_rejects_termination_thread_that_outlives_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            context = _make_context(pathlib.Path(directory))
            started = threading.Event()

            class StuckChild:
                returncode = -1

                def __init__(self):
                    self.calls = 0

                def communicate(self, timeout=None):
                    started.set()
                    self.calls += 1
                    if self.calls == 1:
                        time.sleep(min(timeout, 0.01))
                        raise product_runner.subprocess.TimeoutExpired("fake", timeout)
                    return "", ""

                def poll(self):
                    return self.returncode

            class StuckTerminationThread:
                def join(self, timeout=None):
                    del timeout

                def is_alive(self):
                    return True

            runner = product_runner.ExtractionSubprocessRunner(
                process_factory=lambda command, **kwargs: StuckChild(),
                cancellation_timeout=0.04,
                cancellation_poll_interval=0.01,
            )
            errors = []
            with unittest.mock.patch.object(
                product_runner,
                "_terminate_process_nonblocking",
                return_value=StuckTerminationThread(),
            ):
                thread = threading.Thread(target=lambda: self._capture_error(errors, runner, context))
                thread.start()
                self.assertTrue(started.wait(5))
                runner.cancel()
                thread.join(1)

            self.assertFalse(thread.is_alive())
            self.assertRegex(str(errors[0]), "终止.*超时|取消.*超时")

    def test_cancel_rejects_completed_termination_thread_when_child_is_still_alive(self):
        with tempfile.TemporaryDirectory() as directory:
            context = _make_context(pathlib.Path(directory))
            started = threading.Event()
            allow_return = threading.Event()

            class StillAliveChild:
                def __init__(self):
                    self.calls = 0

                def communicate(self, timeout=None):
                    started.set()
                    self.calls += 1
                    if self.calls == 1:
                        raise product_runner.subprocess.TimeoutExpired("fake", timeout)
                    allow_return.wait(1)
                    return "", ""

                def poll(self):
                    return None

            class CompletedTerminationThread:
                def join(self, timeout=None):
                    del timeout

                def is_alive(self):
                    return False

            runner = product_runner.ExtractionSubprocessRunner(
                process_factory=lambda command, **kwargs: StillAliveChild(),
                cancellation_timeout=0.04,
                cancellation_poll_interval=0.01,
            )
            errors = []
            with unittest.mock.patch.object(
                product_runner,
                "_terminate_process_nonblocking",
                return_value=CompletedTerminationThread(),
            ):
                thread = threading.Thread(target=lambda: self._capture_error(errors, runner, context))
                thread.start()
                self.assertTrue(started.wait(5))
                runner.cancel()
                allow_return.set()
                thread.join(1)

            self.assertFalse(thread.is_alive())
            self.assertRegex(str(errors[0]), "仍在运行|终止.*失败|取消.*超时")

    def test_cancel_rejects_still_alive_child_when_termination_returns_no_helper(self):
        with tempfile.TemporaryDirectory() as directory:
            context = _make_context(pathlib.Path(directory))
            started = threading.Event()
            release = threading.Event()

            class StillAliveChild:
                def __init__(self):
                    self.calls = 0

                def communicate(self, timeout=None):
                    started.set()
                    self.calls += 1
                    if self.calls == 1:
                        raise product_runner.subprocess.TimeoutExpired("fake", timeout)
                    release.wait(1)
                    return "", ""

                def poll(self):
                    return None

            runner = product_runner.ExtractionSubprocessRunner(
                process_factory=lambda command, **kwargs: StillAliveChild(),
                cancellation_timeout=0.04,
                cancellation_poll_interval=0.01,
            )
            errors = []
            with unittest.mock.patch.object(
                product_runner,
                "_terminate_process_nonblocking",
                return_value=None,
            ):
                thread = threading.Thread(target=lambda: self._capture_error(errors, runner, context))
                thread.start()
                self.assertTrue(started.wait(5))
                runner.cancel()
                release.set()
                thread.join(1)

            self.assertFalse(thread.is_alive())
            self.assertRegex(str(errors[0]), "仍在运行|终止.*失败")

    def test_cancel_ignores_taskkill_race_when_reader_already_exited(self):
        class ExitedReader:
            pid = 4321

            def __init__(self):
                self.poll_calls = 0

            def poll(self):
                self.poll_calls += 1
                return None if self.poll_calls == 1 else -1

        process = ExitedReader()
        runner = product_runner.ExtractionSubprocessRunner(cancellation_timeout=0.2)
        runner._current_process = process
        with unittest.mock.patch.object(product_runner.sys, "platform", "win32"), unittest.mock.patch.object(
            product_runner.subprocess,
            "run",
            return_value=product_runner.subprocess.CompletedProcess([], 1),
        ) as taskkill:
            runner._termination_process = product_runner._terminate_process_nonblocking(process)

            runner._wait_for_termination_process()

        taskkill.assert_not_called()

    def test_cancel_terminates_bound_job_even_after_reader_has_exited(self):
        terminated = threading.Event()

        class Job:
            def terminate(self):
                terminated.set()

        class ExitedReader:
            _spectrum_organizer_job = Job()

            def poll(self):
                return -1

        with unittest.mock.patch.object(product_runner.sys, "platform", "win32"):
            helper = product_runner._terminate_process_nonblocking(ExitedReader())
            self.assertIsNotNone(helper)
            helper.join(1)

        self.assertTrue(terminated.is_set())

    def test_bind_failure_with_unconfirmed_reader_preserves_owned_temp_root(self):
        with tempfile.TemporaryDirectory() as directory:
            context = _make_context(pathlib.Path(directory))

            class UnstoppableChild:
                returncode = None

                def poll(self):
                    return None

                def kill(self):
                    return None

                def wait(self, timeout=None):
                    raise product_runner.subprocess.TimeoutExpired("reader", timeout)

            runner = product_runner.ExtractionSubprocessRunner(
                process_factory=lambda command, **kwargs: UnstoppableChild(),
                cancellation_timeout=0.01,
                origin_shutdown_poll_interval=0.001,
            )
            with unittest.mock.patch.object(
                product_runner,
                "bind_process_to_job",
                side_effect=ProcessJobError("bind failed"),
            ):
                with self.assertRaisesRegex(
                    product_runner.ExtractionCleanupBlockedError,
                    "清理|终止|运行",
                ):
                    runner(context)

            self.assertTrue(context.temp_root.exists())

    def test_cancel_observed_during_job_bind_does_not_release_reader_start_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            context = _make_context(pathlib.Path(directory))
            gate_writes = []
            real_write_text = pathlib.Path.write_text

            class ExitedChild:
                returncode = -1

                def communicate(self, timeout=None):
                    del timeout
                    return "", ""

                def poll(self):
                    return -1

            runner = product_runner.ExtractionSubprocessRunner(
                process_factory=lambda command, **kwargs: ExitedChild(),
                origin_process_probe=lambda *, timeout: (),
                origin_shutdown_poll_interval=0.001,
            )
            runner._require_process_job = True

            def cancel_during_bind(process, *, required):
                del process, required
                runner.cancel()
                return None

            def record_gate_write(path, text, *args, **kwargs):
                if path.name.endswith(".gate"):
                    gate_writes.append(path)
                return real_write_text(path, text, *args, **kwargs)

            with unittest.mock.patch.object(
                product_runner,
                "bind_process_to_job",
                side_effect=cancel_during_bind,
            ), unittest.mock.patch.object(
                product_runner,
                "_terminate_process_nonblocking",
                return_value=None,
            ), unittest.mock.patch.object(
                pathlib.Path,
                "write_text",
                new=record_gate_write,
            ):
                with self.assertRaisesRegex(product_runner.ProductRunnerError, "取消"):
                    runner(context)

            self.assertEqual([], gate_writes)

    def test_bind_failure_confirms_origin_shutdown_before_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            context = _make_context(pathlib.Path(directory))
            probe = unittest.mock.Mock(return_value=())
            launch_count = 0

            class FirstChild:
                returncode = None

                def poll(self):
                    return self.returncode

                def kill(self):
                    self.returncode = -1

            def process_factory(command, **kwargs):
                del kwargs
                nonlocal launch_count
                launch_count += 1
                if launch_count == 1:
                    return FirstChild()
                self.assertGreaterEqual(probe.call_count, 2)
                manifest = json.loads(pathlib.Path(command[-2]).read_text(encoding="utf-8"))
                result_path = pathlib.Path(command[-1])
                snapshot_path = pathlib.Path(manifest["snapshot_path"])
                source_summary = _record_valid_source(
                    snapshot_path,
                    context,
                    manifest["source_id"],
                    copy_path=pathlib.Path(manifest["copy_path"]),
                    reader_attempt=2,
                )
                return _CompletedChild(
                    result_path,
                    _summary_payload(snapshot_path, source_summary),
                    {"active": 0, "max_active": 0},
                )

            runner = product_runner.ExtractionSubprocessRunner(
                process_factory=process_factory,
                origin_process_probe=probe,
                origin_shutdown_poll_interval=0.001,
            )
            with unittest.mock.patch.object(
                product_runner,
                "bind_process_to_job",
                side_effect=(ProcessJobError("bind failed"), None),
            ):
                summary = runner(context)

            self.assertEqual(2, launch_count)
            self.assertEqual(1, summary.total_extracted_count)

    def test_reader_remains_cancellable_until_origin_shutdown_is_confirmed(self):
        with tempfile.TemporaryDirectory() as directory:
            context = _make_context(pathlib.Path(directory))

            def process_factory(command, **kwargs):
                del kwargs
                manifest = json.loads(pathlib.Path(command[-2]).read_text(encoding="utf-8"))
                result_path = pathlib.Path(command[-1])
                snapshot_path = pathlib.Path(manifest["snapshot_path"])
                source_summary = _record_valid_source(snapshot_path, context, manifest["source_id"])
                return _CompletedChild(
                    result_path,
                    _summary_payload(snapshot_path, source_summary),
                    {"active": 0, "max_active": 0},
                )

            runner = product_runner.ExtractionSubprocessRunner(process_factory=process_factory)
            original_wait = runner._wait_for_termination_process

            def assert_still_tracked(process=None):
                self.assertIs(process, runner._current_process)
                return original_wait(process)

            with unittest.mock.patch.object(
                runner,
                "_wait_for_termination_process",
                side_effect=assert_still_tracked,
            ):
                summary = runner(context)

            self.assertEqual(1, summary.total_extracted_count)

    def test_final_reader_failure_still_rechecks_original_source(self):
        with tempfile.TemporaryDirectory() as directory:
            context = _make_context(pathlib.Path(directory))
            runner = product_runner.ExtractionSubprocessRunner(process_factory=lambda *args, **kwargs: None)

            with unittest.mock.patch.object(
                runner,
                "_run_reader_process_attempt",
                side_effect=(
                    product_runner._ReaderProcessInfrastructureError("first"),
                    product_runner._ReaderProcessInfrastructureError("second"),
                ),
            ), unittest.mock.patch.object(
                product_runner.ExtractionSourceManager,
                "verify_after_worker",
            ) as verify_after_worker:
                with self.assertRaisesRegex(product_runner.ProductRunnerError, "attempt 2"):
                    runner(context)

            self.assertEqual(2, verify_after_worker.call_count)

    def test_invalid_open_target_marker_still_rechecks_original_source(self):
        with tempfile.TemporaryDirectory() as directory:
            context = _make_context(pathlib.Path(directory))
            runner = product_runner.ExtractionSubprocessRunner(process_factory=lambda *args, **kwargs: None)

            with unittest.mock.patch.object(
                runner,
                "_run_reader_process_attempt",
                side_effect=product_runner._ReaderProcessInfrastructureError("reader failed"),
            ), unittest.mock.patch.object(
                product_runner,
                "_read_observed_origin_open_target",
                side_effect=product_runner.ProductRunnerError("invalid open-target marker"),
            ), unittest.mock.patch.object(
                product_runner.ExtractionSourceManager,
                "verify_after_worker",
            ) as verify_after_worker:
                with self.assertRaisesRegex(product_runner.ProductRunnerError, "invalid open-target marker"):
                    runner(context)

            verify_after_worker.assert_called_once_with("S0001")

    def test_cancelled_reader_still_detects_original_source_change(self):
        with tempfile.TemporaryDirectory() as directory:
            context = _make_context(pathlib.Path(directory))
            runner = product_runner.ExtractionSubprocessRunner(process_factory=lambda *args, **kwargs: None)

            def cancel_after_changing_original(*args):
                del args
                context.source_fingerprints_before[0].path.write_bytes(b"changed-original")
                runner.cancel()
                raise product_runner.ProductRunnerError("谱图数据提取已取消")

            with unittest.mock.patch.object(
                runner,
                "_run_reader_process_attempt",
                side_effect=cancel_after_changing_original,
            ):
                with self.assertRaisesRegex(RuntimeError, "Source changed after snapshot"):
                    runner(context)

    def test_cancelled_reader_still_detects_source_copy_change(self):
        with tempfile.TemporaryDirectory() as directory:
            context = _make_context(pathlib.Path(directory))
            runner = product_runner.ExtractionSubprocessRunner(process_factory=lambda *args, **kwargs: None)

            def cancel_after_changing_copy(*args):
                del args
                context.run_owned_source_copy_paths[0].write_bytes(b"changed-copy")
                runner.cancel()
                raise product_runner.ProductRunnerError("谱图数据提取已取消")

            with unittest.mock.patch.object(
                runner,
                "_run_reader_process_attempt",
                side_effect=cancel_after_changing_copy,
            ):
                with self.assertRaisesRegex(RuntimeError, "Source copy changed or mismatched"):
                    runner(context)

    def test_temp_root_cleanup_failure_is_latched_as_cleanup_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            context = _make_context(pathlib.Path(directory))
            runner = product_runner.ExtractionSubprocessRunner(process_factory=lambda *args, **kwargs: None)

            with unittest.mock.patch.object(
                runner,
                "_run",
                side_effect=product_runner.ProductRunnerError("谱图数据提取已取消"),
            ), unittest.mock.patch.object(
                product_runner,
                "_cleanup_temp_root_error",
                return_value="存在未知路径",
            ):
                with self.assertRaisesRegex(
                    product_runner.ExtractionCleanupBlockedError,
                    "临时文件清理失败",
                ):
                    runner(context)

            self.assertIsNotNone(runner._cleanup_blocked_reason)

    def test_job_close_failure_latches_cleanup_blocked_after_origin_shutdown_confirmation(self):
        class ExitedReader:
            def poll(self):
                return -1

        events = []

        def probe(*, timeout):
            del timeout
            events.append("probe")
            return ()

        def close_job(_process):
            events.append("close")
            raise OSError("CloseHandle failed")

        runner = product_runner.ExtractionSubprocessRunner(
            origin_process_probe=probe,
            origin_shutdown_poll_interval=0.001,
        )
        process = ExitedReader()

        with unittest.mock.patch.object(
            product_runner,
            "close_bound_process_job",
            side_effect=close_job,
        ):
            with self.assertRaisesRegex(product_runner.ExtractionCleanupBlockedError, "Job|CloseHandle"):
                runner._wait_for_termination_process(process)

        self.assertIsNotNone(runner._cleanup_blocked_reason)
        self.assertEqual(["probe", "probe", "close"], events)

    def test_cancel_cleanup_force_closes_only_the_recorded_origin_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory) / "owned"
            launch_path = root / "origin_launch.S0001.attempt1.json"
            identity_path = root / "origin_identity.S0001.attempt1.json"
            binding = {
                "schema_version": 1,
                "run_id": "run-1",
                "marker_id": "marker-1",
                "source_id": "S0001",
                "reader_attempt": 1,
            }
            _write_origin_sidecar(
                launch_path,
                {**binding, "launch_state": "launch_allowed", "processes": []},
            )
            _write_origin_sidecar(
                identity_path,
                {**binding, "pid": 4321, "start_time_ns": 987654321},
            )
            controller = unittest.mock.Mock()
            controller.is_running.return_value = False
            runner = product_runner.ExtractionSubprocessRunner(
                origin_process_probe=lambda *, timeout: (),
                origin_process_controller=controller,
            )
            runner._active_origin_launch_path = launch_path
            runner._active_origin_identity_path = identity_path
            runner._active_origin_binding = ("run-1", "marker-1", "S0001", 1)

            runner._close_run_owned_origin()

            identity = ProcessIdentity(pid=4321, start_time_ns=987654321)
            controller.force_close.assert_called_once()
            controller.is_running.assert_called_once()
            self.assertEqual((identity,), controller.force_close.call_args.args)
            self.assertEqual((identity,), controller.is_running.call_args.args)
            self.assertGreater(controller.force_close.call_args.kwargs["timeout"], 0)
            self.assertGreater(controller.is_running.call_args.kwargs["timeout"], 0)

    def test_cancel_cleanup_retries_transient_force_close_timeout_within_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory) / "owned"
            launch_path = root / "origin_launch.S0001.attempt1.json"
            identity_path = root / "origin_identity.S0001.attempt1.json"
            binding = {
                "schema_version": 1,
                "run_id": "run-1",
                "marker_id": "marker-1",
                "source_id": "S0001",
                "reader_attempt": 1,
            }
            _write_origin_sidecar(
                launch_path,
                {**binding, "launch_state": "launch_allowed", "processes": []},
            )
            _write_origin_sidecar(
                identity_path,
                {**binding, "pid": 4321, "start_time_ns": 987654321},
            )
            controller = unittest.mock.Mock()
            controller.force_close.side_effect = (
                subprocess.TimeoutExpired("powershell", 5),
                True,
            )
            controller.is_running.side_effect = (True, False)
            runner = product_runner.ExtractionSubprocessRunner(
                origin_process_probe=lambda *, timeout: (),
                origin_process_controller=controller,
                origin_shutdown_timeout=0.2,
                origin_shutdown_poll_interval=0.001,
            )
            runner._active_origin_launch_path = launch_path
            runner._active_origin_identity_path = identity_path
            runner._active_origin_binding = ("run-1", "marker-1", "S0001", 1)

            runner._close_run_owned_origin()

            identity = ProcessIdentity(pid=4321, start_time_ns=987654321)
            self.assertEqual(
                [identity, identity],
                [call.args[0] for call in controller.force_close.call_args_list],
            )
            self.assertEqual(
                [identity, identity],
                [call.args[0] for call in controller.is_running.call_args_list],
            )
            self.assertTrue(
                all(call.kwargs["timeout"] > 0 for call in controller.force_close.call_args_list)
            )
            self.assertTrue(
                all(call.kwargs["timeout"] > 0 for call in controller.is_running.call_args_list)
            )

    def test_cancel_cleanup_persistent_force_close_timeout_stops_at_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory) / "owned"
            launch_path = root / "origin_launch.S0001.attempt1.json"
            identity_path = root / "origin_identity.S0001.attempt1.json"
            binding = {
                "schema_version": 1,
                "run_id": "run-1",
                "marker_id": "marker-1",
                "source_id": "S0001",
                "reader_attempt": 1,
            }
            _write_origin_sidecar(
                launch_path,
                {**binding, "launch_state": "launch_allowed", "processes": []},
            )
            _write_origin_sidecar(
                identity_path,
                {**binding, "pid": 4321, "start_time_ns": 987654321},
            )
            controller = unittest.mock.Mock()
            controller.force_close.side_effect = subprocess.TimeoutExpired("powershell", 5)
            controller.is_running.return_value = True
            runner = product_runner.ExtractionSubprocessRunner(
                origin_process_probe=lambda *, timeout: (),
                origin_process_controller=controller,
                origin_shutdown_timeout=0.01,
                origin_shutdown_poll_interval=0.001,
            )
            runner._active_origin_launch_path = launch_path
            runner._active_origin_identity_path = identity_path
            runner._active_origin_binding = ("run-1", "marker-1", "S0001", 1)

            with self.assertRaisesRegex(
                product_runner.ExtractionCleanupBlockedError,
                "期限内.*Origin.*退出|Origin.*退出.*超时",
            ):
                runner._close_run_owned_origin()

            self.assertGreaterEqual(controller.force_close.call_count, 2)

    def test_cancel_cleanup_deadline_bounds_blocking_controller_call(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory) / "owned"
            launch_path = root / "origin_launch.S0001.attempt1.json"
            identity_path = root / "origin_identity.S0001.attempt1.json"
            binding = {
                "schema_version": 1,
                "run_id": "run-1",
                "marker_id": "marker-1",
                "source_id": "S0001",
                "reader_attempt": 1,
            }
            _write_origin_sidecar(
                launch_path,
                {**binding, "launch_state": "launch_allowed", "processes": []},
            )
            _write_origin_sidecar(
                identity_path,
                {**binding, "pid": 4321, "start_time_ns": 987654321},
            )
            release = threading.Event()
            observed_timeouts = []

            def bounded_force_close(_identity, *, timeout=None):
                observed_timeouts.append(timeout)
                wait_timeout = 1.0 if timeout is None else timeout
                if not release.wait(wait_timeout):
                    raise subprocess.TimeoutExpired("force close", wait_timeout)
                return True

            controller = unittest.mock.Mock()
            controller.force_close.side_effect = bounded_force_close
            controller.is_running.return_value = True
            runner = product_runner.ExtractionSubprocessRunner(
                origin_process_probe=lambda *, timeout: (),
                origin_process_controller=controller,
                origin_shutdown_timeout=0.05,
                origin_shutdown_poll_interval=0.001,
            )
            runner._active_origin_launch_path = launch_path
            runner._active_origin_identity_path = identity_path
            runner._active_origin_binding = ("run-1", "marker-1", "S0001", 1)

            threads_before = {id(thread) for thread in threading.enumerate()}
            started = time.monotonic()
            try:
                with self.assertRaisesRegex(
                    product_runner.ExtractionCleanupBlockedError,
                    "期限内.*Origin.*退出|Origin.*退出.*超时",
                ):
                    runner._close_run_owned_origin()
                leaked_threads = [
                    thread
                    for thread in threading.enumerate()
                    if id(thread) not in threads_before and thread.is_alive()
                ]
                self.assertEqual([], leaked_threads)
                self.assertTrue(observed_timeouts)
                self.assertIsNotNone(observed_timeouts[0])
            finally:
                release.set()
                for thread in threading.enumerate():
                    if id(thread) not in threads_before:
                        thread.join(0.2)

            self.assertLess(time.monotonic() - started, 0.2)

    def test_cancel_cleanup_refuses_unrecorded_origin_even_when_it_is_hidden(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory) / "owned"
            launch_path = root / "origin_launch.S0001.attempt1.json"
            _write_origin_sidecar(
                launch_path,
                {
                    "schema_version": 1,
                    "run_id": "run-1",
                    "marker_id": "marker-1",
                    "source_id": "S0001",
                    "reader_attempt": 1,
                    "launch_state": "launch_allowed",
                    "processes": [],
                },
            )
            hidden = ProcessInfo(
                pid=4321,
                start_time_ns=987654321,
                visible=False,
                taskbar_visible=False,
                program_owned=False,
            )
            controller = unittest.mock.Mock()
            process_probe = unittest.mock.Mock(return_value=(hidden,))
            runner = product_runner.ExtractionSubprocessRunner(
                origin_process_probe=process_probe,
                origin_process_controller=controller,
                cancellation_timeout=0.02,
                cancellation_poll_interval=0.001,
                origin_identity_timeout=0.01,
                origin_shutdown_timeout=0.02,
                origin_shutdown_poll_interval=0.001,
            )
            reader = _ExitedJobReader()
            runner._require_process_job = True
            runner._current_process = reader
            runner._cleanup_blocked_reason = "精确身份记录尚未发布"
            runner._active_origin_launch_path = launch_path
            runner._active_origin_identity_path = root / "missing.json"
            runner._active_origin_binding = ("run-1", "marker-1", "S0001", 1)
            runner._origin_start_gate_released = True

            with (
                unittest.mock.patch.object(product_runner.sys, "platform", "win32"),
                unittest.mock.patch.object(
                    product_runner,
                    "close_bound_process_job",
                ) as close_job,
                self.assertRaisesRegex(
                    product_runner.ExtractionCleanupBlockedError,
                    "等待 Origin 退出超时",
                ),
            ):
                runner.retry_cancel_cleanup()

            controller.force_close.assert_not_called()
            close_job.assert_not_called()
            self.assertGreater(process_probe.call_count, 0)
            self.assertIs(reader, runner._current_process)
            self.assertIsNotNone(runner._cleanup_blocked_reason)

    def test_cancel_cleanup_accepts_missing_identity_after_reader_exit_and_two_empty_probes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory) / "owned"
            launch_path = root / "origin_launch.S0001.attempt1.json"
            _write_origin_sidecar(
                launch_path,
                {
                    "schema_version": 1,
                    "run_id": "run-1",
                    "marker_id": "marker-1",
                    "source_id": "S0001",
                    "reader_attempt": 1,
                    "launch_state": "launch_allowed",
                    "processes": [],
                },
            )
            reader = _ExitedJobReader()
            controller = unittest.mock.Mock()
            process_probe = unittest.mock.Mock(return_value=())
            runner = product_runner.ExtractionSubprocessRunner(
                origin_process_probe=process_probe,
                origin_process_controller=controller,
                cancellation_timeout=0.02,
                cancellation_poll_interval=0.001,
                origin_identity_timeout=0.01,
                origin_shutdown_timeout=0.05,
                origin_shutdown_poll_interval=0.001,
            )
            runner._require_process_job = True
            runner._current_process = reader
            runner._cleanup_blocked_reason = "精确身份记录尚未发布"
            runner._active_origin_launch_path = launch_path
            runner._active_origin_identity_path = root / "missing.json"
            runner._active_origin_binding = ("run-1", "marker-1", "S0001", 1)
            runner._origin_start_gate_released = True

            with (
                unittest.mock.patch.object(product_runner.sys, "platform", "win32"),
                unittest.mock.patch.object(
                    product_runner,
                    "close_bound_process_job",
                ) as close_job,
            ):
                runner.retry_cancel_cleanup()

            controller.force_close.assert_not_called()
            self.assertGreaterEqual(process_probe.call_count, 2)
            close_job.assert_called_once_with(reader)
            self.assertIsNone(runner._current_process)
            self.assertIsNone(runner._cleanup_blocked_reason)
            self.assertIsNone(runner._active_origin_launch_path)
            self.assertIsNone(runner._active_origin_identity_path)
            self.assertIsNone(runner._active_origin_binding)
            self.assertFalse(runner._origin_start_gate_released)

    def test_cancel_cleanup_accepts_bound_prelaunch_rejection_without_identity(self):
        class ExitedReader:
            def poll(self):
                return -1

        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory) / "owned"
            launch_path = root / "origin_launch.S0001.attempt1.json"
            identity_path = root / "origin_identity.S0001.attempt1.json"
            helper_path = root / "origin_identity.S0001.attempt1.c"
            existing = ProcessInfo(
                pid=4321,
                start_time_ns=987654321,
                visible=False,
                taskbar_visible=False,
                program_owned=False,
            )
            ownership = _test_origin_ownership(
                root,
                {"run_id": "run-1", "marker_id": "marker-1"},
            )
            for path in (
                launch_path,
                launch_path.with_name(f"{launch_path.name}.pending"),
                helper_path,
            ):
                ownership = add_allowed_child(ownership, path)

            def bind_created(path, identity):
                nonlocal ownership
                ownership = bind_allowed_child_identity(
                    ownership,
                    path,
                    expected_identity=identity,
                )

            with unittest.mock.patch(
                "spectrum_organizer.safety.process_boundary.default_origin_process_probe",
                return_value=(existing,),
            ):
                with self.assertRaises(extract_worker_module.WorkerPreflightError):
                    extract_worker_module._record_origin_launch_baseline(
                        launch_path,
                        helper_path=helper_path,
                        run_id="run-1",
                        marker_id="marker-1",
                        source_id="S0001",
                        reader_attempt=1,
                        cleanup_identity_callback=bind_created,
                    )

            payload = json.loads(launch_path.read_text(encoding="utf-8"))
            self.assertEqual("prelaunch_rejected", payload["launch_state"])
            self.assertFalse(identity_path.exists())
            self.assertFalse(helper_path.exists())

            controller = unittest.mock.Mock()
            runner = product_runner.ExtractionSubprocessRunner(
                origin_process_probe=lambda *, timeout: (existing,),
                origin_process_controller=controller,
                origin_shutdown_timeout=0.02,
                origin_shutdown_poll_interval=0.001,
            )
            runner._active_origin_launch_path = launch_path
            runner._active_origin_identity_path = identity_path
            runner._active_origin_binding = ("run-1", "marker-1", "S0001", 1)

            with unittest.mock.patch.object(product_runner, "close_bound_process_job") as close_job:
                runner._wait_for_termination_process(ExitedReader())

            controller.force_close.assert_not_called()
            controller.is_running.assert_not_called()
            close_job.assert_called_once()
            self.assertIsNone(runner._active_origin_launch_path)
            self.assertIsNone(runner._active_origin_identity_path)
            self.assertIsNone(runner._active_origin_binding)

    def test_cancel_cleanup_rejects_inconsistent_launch_state(self):
        existing_process = {"pid": 4321, "start_time_ns": 987654321}
        cases = (
            ("launch_allowed", [existing_process]),
            ("prelaunch_rejected", []),
            ("unknown", []),
        )
        for index, (launch_state, processes) in enumerate(cases):
            with self.subTest(launch_state=launch_state, processes=processes):
                with tempfile.TemporaryDirectory() as directory:
                    root = pathlib.Path(directory)
                    launch_path = root / f"origin_launch.S0001.attempt{index}.json"
                    launch_path.write_text(
                        json.dumps(
                            {
                                "schema_version": 1,
                                "run_id": "run-1",
                                "marker_id": "marker-1",
                                "source_id": "S0001",
                                "reader_attempt": index + 1,
                                "launch_state": launch_state,
                                "processes": processes,
                            }
                        ),
                        encoding="utf-8",
                    )
                    controller = unittest.mock.Mock()
                    runner = product_runner.ExtractionSubprocessRunner(
                        origin_process_probe=lambda *, timeout: (),
                        origin_process_controller=controller,
                    )
                    runner._active_origin_launch_path = launch_path
                    runner._active_origin_identity_path = root / "missing.json"
                    runner._active_origin_binding = (
                        "run-1",
                        "marker-1",
                        "S0001",
                        index + 1,
                    )

                    with self.assertRaises(product_runner.ExtractionCleanupBlockedError):
                        runner._close_run_owned_origin()

                    controller.force_close.assert_not_called()
                    controller.is_running.assert_not_called()

    def test_cancel_cleanup_rejects_identity_that_was_present_in_launch_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory) / "owned"
            launch_path = root / "origin_launch.S0001.attempt1.json"
            identity_path = root / "origin_identity.S0001.attempt1.json"
            binding = {
                "schema_version": 1,
                "run_id": "run-1",
                "marker_id": "marker-1",
                "source_id": "S0001",
                "reader_attempt": 1,
            }
            _write_origin_sidecar(
                launch_path,
                {
                    **binding,
                    "launch_state": "prelaunch_rejected",
                    "processes": [{"pid": 4321, "start_time_ns": 987654321}],
                },
            )
            _write_origin_sidecar(
                identity_path,
                {**binding, "pid": 4321, "start_time_ns": 987654321},
            )
            controller = unittest.mock.Mock()
            runner = product_runner.ExtractionSubprocessRunner(
                origin_process_probe=lambda *, timeout: (),
                origin_process_controller=controller,
            )
            runner._active_origin_launch_path = launch_path
            runner._active_origin_identity_path = identity_path
            runner._active_origin_binding = ("run-1", "marker-1", "S0001", 1)

            with self.assertRaisesRegex(
                product_runner.ExtractionCleanupBlockedError,
                "启动前基线",
            ):
                runner._close_run_owned_origin()

            controller.force_close.assert_not_called()

    def test_cancel_cleanup_rejects_identity_with_wrong_binding_or_extra_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            launch_path = root / "origin_launch.S0001.attempt1.json"
            identity_path = root / "origin_identity.S0001.attempt1.json"
            binding = {
                "schema_version": 1,
                "run_id": "run-1",
                "marker_id": "marker-1",
                "source_id": "S0001",
                "reader_attempt": 1,
            }
            launch_path.write_text(
                json.dumps({**binding, "launch_state": "launch_allowed", "processes": []}),
                encoding="utf-8",
            )
            identity_path.write_text(
                json.dumps(
                    {
                        **binding,
                        "source_id": "S0002",
                        "pid": 4321,
                        "start_time_ns": 987654321,
                        "unexpected": True,
                    }
                ),
                encoding="utf-8",
            )
            controller = unittest.mock.Mock()
            runner = product_runner.ExtractionSubprocessRunner(
                origin_process_probe=lambda *, timeout: (),
                origin_process_controller=controller,
            )
            runner._active_origin_launch_path = launch_path
            runner._active_origin_identity_path = identity_path
            runner._active_origin_binding = ("run-1", "marker-1", "S0001", 1)

            with self.assertRaisesRegex(
                product_runner.ExtractionCleanupBlockedError,
                "身份记录",
            ):
                runner._close_run_owned_origin()

            controller.force_close.assert_not_called()

    def test_origin_identity_sidecar_is_published_only_after_complete_json_is_flushed(self):
        with tempfile.TemporaryDirectory() as directory:
            final_path = pathlib.Path(directory) / "origin_identity.S0001.attempt1.json"
            pending_path = final_path.with_name(f"{final_path.name}.pending")
            real_link = extract_worker_module.os.link
            observed = {}

            def inspect_then_link(source, destination):
                observed["source"] = pathlib.Path(source)
                observed["destination"] = pathlib.Path(destination)
                observed["payload"] = json.loads(pathlib.Path(source).read_text(encoding="utf-8"))
                self.assertFalse(final_path.exists())
                real_link(source, destination)

            payload = {
                "schema_version": 1,
                "run_id": "run-1",
                "marker_id": "marker-1",
                "source_id": "S0001",
                "reader_attempt": 1,
                "pid": 4321,
                "start_time_ns": 987654321,
            }
            with unittest.mock.patch.object(
                extract_worker_module.os,
                "link",
                side_effect=inspect_then_link,
            ):
                extract_worker_module._write_owned_json_atomic(final_path, payload)

            self.assertEqual(pending_path, observed["source"])
            self.assertEqual(final_path, observed["destination"])
            observed_payload = dict(observed["payload"])
            creation_identity = observed_payload.pop("creation_identity")
            self.assertEqual(payload, observed_payload)
            self.assertEqual(2, len(creation_identity))
            final_payload = json.loads(final_path.read_text(encoding="utf-8"))
            final_payload.pop("creation_identity")
            self.assertEqual(payload, final_payload)
            self.assertFalse(pending_path.exists())

    def test_origin_sidecar_reader_rejects_self_consistent_same_path_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            ownership = create_run_ownership(
                pathlib.Path(directory) / "localapp",
                "run-1",
                "marker-1",
                [],
            )
            identity_path = ownership.temp_root / "origin_identity.S0001.attempt1.json"
            ownership = add_allowed_child(ownership, identity_path)
            binding = {
                "schema_version": 1,
                "run_id": "run-1",
                "marker_id": "marker-1",
                "source_id": "S0001",
                "reader_attempt": 1,
                "pid": 4321,
                "start_time_ns": 987654321,
            }
            extract_worker_module._write_owned_json_atomic(
                identity_path,
                binding,
            )
            ownership = bind_allowed_child_identity(ownership, identity_path)
            parked = ownership.temp_root / "parked-owned-origin-identity.json"
            identity_path.rename(parked)
            with identity_path.open("x", encoding="utf-8") as stream:
                status = os.fstat(stream.fileno())
                json.dump(
                    {
                        **binding,
                        "creation_identity": [status.st_dev, status.st_ino],
                    },
                    stream,
                )
                stream.flush()
                os.fsync(stream.fileno())

            with self.assertRaisesRegex(ValueError, "identity|身份"):
                product_runner._read_owned_origin_identity(
                    identity_path,
                    ("run-1", "marker-1", "S0001", 1),
                )

            self.assertEqual(4321, json.loads(parked.read_text(encoding="utf-8"))["pid"])

    def test_origin_sidecar_reader_rejects_same_inode_content_rewrite(self):
        with tempfile.TemporaryDirectory() as directory:
            ownership = create_run_ownership(
                pathlib.Path(directory) / "localapp",
                "run-1",
                "marker-1",
                [],
            )
            identity_path = ownership.temp_root / "origin_identity.S0001.attempt1.json"
            ownership = add_allowed_child(ownership, identity_path)
            auth_key = "a" * 64
            binding = {
                "schema_version": 1,
                "run_id": "run-1",
                "marker_id": "marker-1",
                "source_id": "S0001",
                "reader_attempt": 1,
                "pid": 4321,
                "start_time_ns": 987654321,
            }
            extract_worker_module._write_owned_json_atomic(
                identity_path,
                binding,
                auth_key=auth_key,
            )
            bind_allowed_child_identity(ownership, identity_path)
            payload = json.loads(identity_path.read_text(encoding="utf-8"))
            payload["pid"] = 9999
            replacement = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
            with identity_path.open("r+b", buffering=0) as stream:
                stream.seek(0)
                stream.write(replacement)
                stream.truncate()
                stream.flush()
                os.fsync(stream.fileno())

            with self.assertRaisesRegex(ValueError, "内容|认证|auth"):
                product_runner._read_owned_origin_identity(
                    identity_path,
                    ("run-1", "marker-1", "S0001", 1),
                    auth_key=auth_key,
                )

    def test_reader_child_refuses_same_inode_manifest_rewrite(self):
        from spectrum_organizer.origin.extraction_process import extraction_process_main
        from spectrum_organizer.safety.identity_paths import file_sha256

        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            context = _make_context(base)
            product_runner._prepare_reader_temp_root(context)
            manifest_path = context.temp_root / "manifest.json"
            result_path = context.temp_root / "result.json"
            snapshot_path = product_runner._register_snapshot_path(
                context,
                context.temp_root / "run_snapshot.sqlite3",
            )
            ownership = read_ownership(context.temp_root)
            for path in (
                manifest_path,
                result_path,
                result_path.with_name(f"{result_path.name}.pending"),
                context.temp_root / product_runner.ACTIVE_LEASE_FILE,
            ):
                if path not in ownership.allowed_children:
                    ownership = add_allowed_child(ownership, path)
            source = product_runner._build_extraction_sources(
                context,
                ExtractionSource,
            )[0]
            command = product_runner._build_reader_process_command(
                context,
                source,
                snapshot_path,
            )
            manifest_path.write_text(
                json.dumps(product_runner._reader_command_to_payload(command)),
                encoding="utf-8",
            )
            bind_allowed_child_identity(ownership, manifest_path)
            expected_sha256 = file_sha256(manifest_path)
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            payload["copy_path"] = str(base / "FORGED-ORIGINAL.opju")
            with manifest_path.open("r+", encoding="utf-8") as stream:
                stream.seek(0)
                json.dump(payload, stream)
                stream.truncate()
                stream.flush()
                os.fsync(stream.fileno())
            seen = []

            return_code = extraction_process_main(
                [expected_sha256, str(manifest_path), str(result_path)],
                extraction_runner=lambda received: seen.append(received),
            )

            self.assertEqual(1, return_code)
            self.assertEqual([], seen)

    def test_reader_child_refuses_registered_manifest_replaced_before_read(self):
        from spectrum_organizer.origin.extraction_process import extraction_process_main

        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            context = _make_context(base)
            product_runner._prepare_reader_temp_root(context)
            manifest_path = context.temp_root / "manifest.json"
            result_path = context.temp_root / "result.json"
            snapshot_path = product_runner._register_snapshot_path(
                context,
                context.temp_root / "run_snapshot.sqlite3",
            )
            ownership = read_ownership(context.temp_root)
            for path in (
                manifest_path,
                result_path,
                result_path.with_name(f"{result_path.name}.pending"),
                context.temp_root / product_runner.ACTIVE_LEASE_FILE,
            ):
                if path not in ownership.allowed_children:
                    ownership = add_allowed_child(ownership, path)
            source = product_runner._build_extraction_sources(
                context,
                ExtractionSource,
            )[0]
            command = product_runner._build_reader_process_command(
                context,
                source,
                snapshot_path,
            )
            manifest_path.write_text(
                json.dumps(product_runner._reader_command_to_payload(command)),
                encoding="utf-8",
            )
            ownership = bind_allowed_child_identity(ownership, manifest_path)
            manifest_path.rename(context.temp_root / "parked-owned-manifest.json")
            manifest_path.write_text(
                json.dumps(product_runner._reader_command_to_payload(command)),
                encoding="utf-8",
            )
            seen = []

            return_code = extraction_process_main(
                _worker_args(manifest_path, result_path),
                extraction_runner=lambda received: seen.append(received),
            )

            self.assertEqual(1, return_code)
            self.assertEqual([], seen)

    def test_owned_atomic_writer_preserves_preexisting_final_and_pending_collisions(self):
        for collision in ("final", "pending"):
            with self.subTest(collision=collision), tempfile.TemporaryDirectory() as directory:
                base = pathlib.Path(directory)
                final_path = base / "origin_identity.S0001.attempt1.json"
                pending_path = final_path.with_name(f"{final_path.name}.pending")
                sentinel_path = base / "sentinel.txt"
                sentinel_path.write_text("preexisting", encoding="utf-8")
                collision_path = final_path if collision == "final" else pending_path
                collision_path.hardlink_to(sentinel_path)

                with self.assertRaises(FileExistsError):
                    extract_worker_module._write_owned_json_atomic(
                        final_path,
                        {"ok": True},
                    )

                self.assertTrue(collision_path.exists())
                self.assertEqual(
                    "preexisting",
                    collision_path.read_text(encoding="utf-8"),
                )
                self.assertEqual(
                    "preexisting",
                    sentinel_path.read_text(encoding="utf-8"),
                )

    def test_owned_atomic_writer_rejects_late_final_collision(self):
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            final_path = base / "origin_identity.S0001.attempt1.json"
            pending_path = final_path.with_name(f"{final_path.name}.pending")
            sentinel_path = base / "sentinel.txt"
            sentinel_path.write_text("late collision", encoding="utf-8")
            real_link = extract_worker_module.os.link

            def inject_collision_then_link(source, destination):
                real_link(sentinel_path, final_path)
                return real_link(source, destination)

            with (
                unittest.mock.patch.object(
                    extract_worker_module.os,
                    "link",
                    side_effect=inject_collision_then_link,
                ),
                self.assertRaises(FileExistsError),
            ):
                extract_worker_module._write_owned_json_atomic(
                    final_path,
                    {"ok": True},
                )

            self.assertEqual("late collision", final_path.read_text(encoding="utf-8"))
            self.assertEqual("late collision", sentinel_path.read_text(encoding="utf-8"))
            self.assertFalse(pending_path.exists())

    def test_origin_launch_baseline_creates_owned_pid_helper_before_origin_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            launch_path = root / "origin_launch.S0001.attempt1.json"
            helper_path = root / "origin_identity.S0001.attempt1.c"

            with unittest.mock.patch(
                "spectrum_organizer.safety.process_boundary.default_origin_process_probe",
                return_value=(),
            ):
                extract_worker_module._record_origin_launch_baseline(
                    launch_path,
                    helper_path=helper_path,
                    run_id="run-1",
                    marker_id="marker-1",
                    source_id="S0001",
                    reader_attempt=1,
                )

            self.assertTrue(launch_path.is_file())
            payload = json.loads(launch_path.read_text(encoding="utf-8"))
            self.assertEqual("launch_allowed", payload["launch_state"])
            helper_source = helper_path.read_text(encoding="ascii")
            self.assertIn("GetCurrentProcessId", helper_source)
            self.assertIn("spectrum_organizer_current_pid", helper_source)

    def test_origin_launch_baseline_helper_failure_leaves_no_launch_permission(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            launch_path = root / "origin_launch.S0001.attempt1.json"
            helper_path = root / "origin_identity.S0001.attempt1.c"
            helper_path.mkdir()

            with unittest.mock.patch(
                "spectrum_organizer.safety.process_boundary.default_origin_process_probe",
                return_value=(),
            ):
                with self.assertRaises(OSError):
                    extract_worker_module._record_origin_launch_baseline(
                        launch_path,
                        helper_path=helper_path,
                        run_id="run-1",
                        marker_id="marker-1",
                        source_id="S0001",
                        reader_attempt=1,
                    )

            self.assertFalse(launch_path.exists())

    def test_origin_identity_is_taken_from_exact_session_not_process_delta(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory) / "owned"
            launch_path = root / "origin_launch.S0001.attempt1.json"
            identity_path = root / "origin_identity.S0001.attempt1.json"
            helper_path = root / "origin_identity.S0001.attempt1.c"
            binding = {
                "schema_version": 1,
                "run_id": "run-1",
                "marker_id": "marker-1",
                "source_id": "S0001",
                "reader_attempt": 1,
            }
            launch_identity = _write_origin_sidecar(
                launch_path,
                {**binding, "launch_state": "launch_allowed", "processes": []},
            )
            helper_path.write_text("int helper();", encoding="utf-8")
            helper_identity = product_runner.path_identity(helper_path)

            class ExactOriginSession:
                def __init__(self):
                    self.commands = []

                def lt_exec(self, command):
                    self.commands.append(command)
                    return True

                def lt_int(self, expression):
                    self.commands.append(expression)
                    return 0 if "LoadOC" in expression else 4321

            origin = ExactOriginSession()
            exact = ProcessInfo(4321, 987654321, False, False, False)
            unrelated = ProcessInfo(9876, 123456789, False, False, False)
            recorded = []

            with (
                unittest.mock.patch(
                    "spectrum_organizer.safety.process_boundary.default_origin_process_probe",
                    return_value=(unrelated, exact),
                ),
                unittest.mock.patch.object(
                    extract_worker_module,
                    "runtime_audit_enabled",
                    return_value=True,
                ),
                unittest.mock.patch.object(
                    extract_worker_module,
                    "record_runtime_audit_event",
                    side_effect=lambda event_type, payload: recorded.append(
                        (event_type, payload)
                    ),
                ),
            ):
                extract_worker_module._record_owned_origin_identity(
                    launch_path,
                    identity_path,
                    origin,
                    launch_identity=launch_identity,
                    helper_path=helper_path,
                    helper_identity=helper_identity,
                    run_id="run-1",
                    marker_id="marker-1",
                    source_id="S0001",
                    reader_attempt=1,
                )

            payload = json.loads(identity_path.read_text(encoding="utf-8"))
            self.assertEqual((4321, 987654321), (payload["pid"], payload["start_time_ns"]))
            load_command = next(command for command in origin.commands if "LoadOC" in command)
            expected_helper_path = str(helper_path.resolve()).replace("\\", "\\\\")
            self.assertIn(expected_helper_path, load_command)
            self.assertNotIn(helper_path.resolve().as_posix(), load_command)
            self.assertTrue(load_command.endswith('", 0)'))
            self.assertTrue(any("spectrum_organizer_current_pid" in command for command in origin.commands))
            self.assertEqual(
                [
                    (
                        "origin_process_identity",
                        {
                            "role": "extraction",
                            "pid": 4321,
                            "start_time_ns": 987654321,
                            "attempt_binding": {
                                "run_id": "run-1",
                                "source_id": "S0001",
                                "reader_attempt": 1,
                            },
                        },
                    )
                ],
                recorded,
            )

    def test_origin_pid_helper_load_requires_creation_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            launch_path = root / "origin_launch.S0001.attempt1.json"
            identity_path = root / "origin_identity.S0001.attempt1.json"
            helper_path = root / "origin_identity.S0001.attempt1.c"
            launch_identity = extract_worker_module._write_owned_json_atomic(
                launch_path,
                {
                    "schema_version": 1,
                    "run_id": "run-1",
                    "marker_id": "marker-1",
                    "source_id": "S0001",
                    "reader_attempt": 1,
                    "launch_state": "launch_allowed",
                    "processes": [],
                },
            )
            helper_path.write_text("int owned_helper();", encoding="ascii")
            helper_identity = product_runner.path_identity(helper_path)
            parked = root / "parked-owned-helper.c"
            helper_path.rename(parked)
            helper_path.write_text("int foreign_helper();", encoding="ascii")

            with self.assertRaises(product_runner.IdentityPathError):
                extract_worker_module._record_owned_origin_identity(
                    launch_path,
                    identity_path,
                    unittest.mock.Mock(),
                    launch_identity=launch_identity,
                    helper_path=helper_path,
                    helper_identity=helper_identity,
                    run_id="run-1",
                    marker_id="marker-1",
                    source_id="S0001",
                    reader_attempt=1,
                )

            self.assertEqual("int foreign_helper();", helper_path.read_text(encoding="ascii"))

    def test_production_cancel_waits_for_identity_publication_before_terminating_reader(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory) / "owned"
            root.mkdir()
            identity_path = root / "origin_identity.S0001.attempt1.json"
            terminated = threading.Event()

            class Process:
                def poll(self):
                    return None

            with unittest.mock.patch.object(
                product_runner,
                "terminate_bound_process",
                side_effect=lambda process: terminated.set(),
            ):
                helper = product_runner._terminate_extraction_process_nonblocking(
                    Process(),
                    identity_path=identity_path,
                    expected_binding=("run-1", "marker-1", "S0001", 1),
                    identity_wait_timeout=1.0,
                )
                self.assertFalse(terminated.wait(0.05))
                identity_path.write_text("{}", encoding="utf-8")
                self.assertFalse(terminated.wait(0.05))
                _write_origin_sidecar(
                    identity_path,
                    {
                        "schema_version": 1,
                        "run_id": "run-1",
                        "marker_id": "marker-1",
                        "source_id": "S0001",
                        "reader_attempt": 1,
                        "pid": 4321,
                        "start_time_ns": 987654321,
                    },
                )
                helper.join(1.0)

            self.assertFalse(helper.is_alive())
            self.assertTrue(terminated.is_set())

    def test_production_cancel_accepts_bound_prelaunch_rejection_without_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory) / "owned"
            launch_path = root / "origin_launch.S0001.attempt1.json"
            identity_path = root / "origin_identity.S0001.attempt1.json"
            _write_origin_sidecar(
                launch_path,
                {
                    "schema_version": 1,
                    "run_id": "run-1",
                    "marker_id": "marker-1",
                    "source_id": "S0001",
                    "reader_attempt": 1,
                    "launch_state": "prelaunch_rejected",
                    "processes": [{"pid": 4321, "start_time_ns": 987654321}],
                },
            )
            terminated = threading.Event()

            class Process:
                def poll(self):
                    return None

            with unittest.mock.patch.object(product_runner.sys, "platform", "win32"):
                with unittest.mock.patch.object(
                    product_runner,
                    "terminate_bound_process",
                    side_effect=lambda process: terminated.set(),
                ):
                    helper = product_runner._terminate_extraction_process_nonblocking(
                        Process(),
                        launch_path=launch_path,
                        identity_path=identity_path,
                        expected_binding=("run-1", "marker-1", "S0001", 1),
                        identity_wait_timeout=0.2,
                    )
                    helper.join(1.0)

            self.assertFalse(helper.is_alive())
            self.assertTrue(terminated.is_set())
            self.assertIsNone(helper._spectrum_organizer_termination_state["error"])

    def test_production_cancel_never_terminates_reader_when_identity_publication_times_out(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            launch_path = root / "origin_launch.S0001.attempt1.json"
            identity_path = root / "origin_identity.S0001.attempt1.json"
            terminated = threading.Event()

            class Process:
                def poll(self):
                    return None

            with unittest.mock.patch.object(
                product_runner,
                "terminate_bound_process",
                side_effect=lambda process: terminated.set(),
            ):
                helper = product_runner._terminate_extraction_process_nonblocking(
                    Process(),
                    launch_path=launch_path,
                    identity_path=identity_path,
                    expected_binding=("run-1", "marker-1", "S0001", 1),
                    identity_wait_timeout=0.05,
                )
                helper.join(1.0)

            self.assertFalse(helper.is_alive())
            self.assertFalse(terminated.is_set())
            self.assertIn(
                "精确身份记录",
                helper._spectrum_organizer_termination_state["error"],
            )

    def test_post_gate_cancel_stops_waiting_for_identity_after_reader_process_exits(self):
        with tempfile.TemporaryDirectory() as directory:
            identity_path = pathlib.Path(directory) / "origin_identity.S0001.attempt1.json"
            terminated = threading.Event()

            class Process:
                _spectrum_organizer_job = object()

                def poll(self):
                    return 0

            with unittest.mock.patch.object(
                product_runner,
                "terminate_bound_process",
                side_effect=lambda process: terminated.set(),
            ):
                helper = product_runner._terminate_extraction_process_nonblocking(
                    Process(),
                    identity_path=identity_path,
                    expected_binding=("run-1", "marker-1", "S0001", 1),
                    identity_wait_timeout=0.05,
                )
                helper.join(1.0)

            self.assertFalse(terminated.is_set())
            self.assertIsNone(helper._spectrum_organizer_termination_state["error"])

    def test_post_gate_cancel_leaves_exited_reader_job_for_parent_when_launch_was_never_published(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            launch_path = root / "origin_launch.S0001.attempt1.json"
            identity_path = root / "origin_identity.S0001.attempt1.json"
            terminated = threading.Event()

            class Process:
                _spectrum_organizer_job = object()

                def poll(self):
                    return 0

            with unittest.mock.patch.object(product_runner.sys, "platform", "win32"):
                with unittest.mock.patch.object(
                    product_runner,
                    "terminate_bound_process",
                    side_effect=lambda process: terminated.set(),
                ):
                    helper = product_runner._terminate_extraction_process_nonblocking(
                        Process(),
                        launch_path=launch_path,
                        identity_path=identity_path,
                        expected_binding=("run-1", "marker-1", "S0001", 1),
                        identity_wait_timeout=0.05,
                    )
                    helper.join(1.0)

            self.assertFalse(helper.is_alive())
            self.assertFalse(terminated.is_set())
            self.assertIsNone(helper._spectrum_organizer_termination_state["error"])

    def test_post_gate_cancel_leaves_exited_reader_job_for_parent_even_with_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory) / "owned"
            identity_path = root / "origin_identity.S0001.attempt1.json"
            _write_origin_sidecar(
                identity_path,
                {
                    "schema_version": 1,
                    "run_id": "run-1",
                    "marker_id": "marker-1",
                    "source_id": "S0001",
                    "reader_attempt": 1,
                    "pid": 4321,
                    "start_time_ns": 987654321,
                },
            )
            terminated = threading.Event()

            class Process:
                _spectrum_organizer_job = object()

                def poll(self):
                    return 0

            with unittest.mock.patch.object(
                product_runner,
                "terminate_bound_process",
                side_effect=lambda process: terminated.set(),
            ):
                helper = product_runner._terminate_extraction_process_nonblocking(
                    Process(),
                    identity_path=identity_path,
                    expected_binding=("run-1", "marker-1", "S0001", 1),
                    identity_wait_timeout=1.0,
                )
                helper.join(1.0)

            self.assertFalse(terminated.is_set())
            self.assertIsNone(helper._spectrum_organizer_termination_state["error"])

    def test_production_cancel_rejects_identity_published_after_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory) / "owned"
            identity_path = root / "origin_identity.S0001.attempt1.json"
            terminated = threading.Event()
            clock = iter((0.0, 0.0, 2.0, 2.0))

            class Process:
                def poll(self):
                    return None

            def publish_after_deadline(_seconds):
                _write_origin_sidecar(
                    identity_path,
                    {
                        "schema_version": 1,
                        "run_id": "run-1",
                        "marker_id": "marker-1",
                        "source_id": "S0001",
                        "reader_attempt": 1,
                        "pid": 4321,
                        "start_time_ns": 987654321,
                    },
                )

            with (
                unittest.mock.patch.object(product_runner.time, "monotonic", side_effect=lambda: next(clock)),
                unittest.mock.patch.object(product_runner.time, "sleep", side_effect=publish_after_deadline),
                unittest.mock.patch.object(
                    product_runner,
                    "terminate_bound_process",
                    side_effect=lambda process: terminated.set(),
                ),
            ):
                helper = product_runner._terminate_extraction_process_nonblocking(
                    Process(),
                    identity_path=identity_path,
                    expected_binding=("run-1", "marker-1", "S0001", 1),
                    identity_wait_timeout=1.0,
                )
                helper.join(1.0)

            self.assertFalse(helper.is_alive())
            self.assertFalse(terminated.is_set())
            self.assertIn(
                "精确身份记录",
                helper._spectrum_organizer_termination_state["error"],
            )

    def test_cancel_before_origin_start_gate_uses_reader_only_termination(self):
        runner = product_runner.ExtractionSubprocessRunner(process_factory=lambda *args, **kwargs: None)
        runner._require_process_job = True
        runner._origin_start_gate_released = False
        process = unittest.mock.Mock()
        helper = object()

        with unittest.mock.patch.object(
            product_runner,
            "_terminate_process_nonblocking",
            return_value=helper,
        ) as terminate_reader, unittest.mock.patch.object(
            product_runner,
            "_terminate_extraction_process_nonblocking",
        ) as terminate_started_reader:
            runner._request_termination_locked(process)

        terminate_reader.assert_called_once_with(process)
        terminate_started_reader.assert_not_called()
        self.assertIs(helper, runner._termination_process)

    def test_cancel_before_origin_start_gate_skips_origin_shutdown(self):
        runner = product_runner.ExtractionSubprocessRunner(
            process_factory=lambda *args, **kwargs: None
        )
        runner._require_process_job = True
        runner._origin_start_gate_released = False
        process = unittest.mock.Mock()
        process.poll.return_value = 0

        with (
            unittest.mock.patch.object(
                product_runner,
                "_terminate_process_nonblocking",
                return_value=None,
            ),
            unittest.mock.patch.object(
                product_runner,
                "_wait_for_process_exit",
                return_value=True,
            ),
            unittest.mock.patch.object(
                runner,
                "_close_run_owned_origin",
            ) as close_origin,
            unittest.mock.patch.object(
                runner,
                "_wait_for_origin_shutdown",
            ) as wait_origin,
            unittest.mock.patch.object(
                product_runner,
                "close_bound_process_job",
            ) as close_job,
        ):
            runner._request_termination_locked(process)
            runner._wait_for_termination_process_unchecked(process)

        close_origin.assert_not_called()
        wait_origin.assert_not_called()
        close_job.assert_called_once_with(process)

    def test_retry_cancel_cleanup_retries_same_process_and_clears_it_only_after_success(self):
        runner = product_runner.ExtractionSubprocessRunner(process_factory=lambda *args, **kwargs: None)
        process = unittest.mock.Mock()
        failed_helper = unittest.mock.Mock()
        failed_helper.is_alive.return_value = False
        replacement_helper = object()
        runner._current_process = process
        runner._termination_process = failed_helper
        runner._termination_finalized = True
        runner._cleanup_blocked_reason = "精确身份记录尚未发布"

        with unittest.mock.patch.object(
            runner,
            "_request_termination_locked",
            side_effect=lambda current: setattr(runner, "_termination_process", replacement_helper),
        ) as request_termination, unittest.mock.patch.object(
            runner,
            "_wait_for_termination_process",
            side_effect=lambda current: setattr(runner, "_cleanup_blocked_reason", None),
        ) as wait_for_termination:
            runner.retry_cancel_cleanup()

        request_termination.assert_called_once_with(process)
        wait_for_termination.assert_called_once_with(process)
        self.assertIsNone(runner._current_process)
        self.assertTrue(runner._cancelled.is_set())

    def test_retry_cancel_cleanup_keeps_process_when_retry_still_cannot_confirm_cleanup(self):
        runner = product_runner.ExtractionSubprocessRunner(process_factory=lambda *args, **kwargs: None)
        process = unittest.mock.Mock()
        failed_helper = unittest.mock.Mock()
        failed_helper.is_alive.return_value = False
        runner._current_process = process
        runner._termination_process = failed_helper
        runner._cleanup_blocked_reason = "精确身份记录尚未发布"

        with unittest.mock.patch.object(
            runner,
            "_request_termination_locked",
        ), unittest.mock.patch.object(
            runner,
            "_wait_for_termination_process",
            side_effect=product_runner.ExtractionCleanupBlockedError("仍无法确认"),
        ):
            with self.assertRaisesRegex(product_runner.ExtractionCleanupBlockedError, "仍无法确认"):
                runner.retry_cancel_cleanup()

        self.assertIs(runner._current_process, process)

    def test_identity_helper_error_blocks_job_close_after_reader_has_exited(self):
        runner = product_runner.ExtractionSubprocessRunner(process_factory=lambda *args, **kwargs: None)
        process = unittest.mock.Mock()
        process.poll.return_value = 0
        process._spectrum_organizer_job = object()
        helper = unittest.mock.Mock()
        helper.is_alive.return_value = False
        helper._spectrum_organizer_termination_state = {"error": "缺少精确身份记录"}
        helper._spectrum_organizer_require_error_propagation = True
        runner._termination_process = helper

        with unittest.mock.patch.object(
            product_runner,
            "close_bound_process_job",
        ) as close_job:
            with self.assertRaisesRegex(
                product_runner.ExtractionCleanupBlockedError,
                "缺少精确身份记录",
            ):
                runner._wait_for_termination_process(process)

        close_job.assert_not_called()
        self.assertIn("缺少精确身份记录", runner._cleanup_blocked_reason)

    def test_post_gate_missing_identity_closes_job_after_two_empty_probes_without_termination_helper(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory) / "owned"
            launch_path = root / "origin_launch.S0001.attempt1.json"
            _write_origin_sidecar(
                launch_path,
                {
                    "schema_version": 1,
                    "run_id": "run-1",
                    "marker_id": "marker-1",
                    "source_id": "S0001",
                    "reader_attempt": 1,
                    "launch_state": "launch_allowed",
                    "processes": [],
                },
            )
            process_probe = unittest.mock.Mock(return_value=())
            runner = product_runner.ExtractionSubprocessRunner(
                process_factory=lambda *args, **kwargs: None,
                origin_process_probe=process_probe,
                origin_shutdown_timeout=0.05,
                origin_shutdown_poll_interval=0.001,
            )
            process = unittest.mock.Mock()
            process.poll.return_value = 0
            process._spectrum_organizer_job = object()
            runner._origin_start_gate_released = True
            runner._active_origin_launch_path = launch_path
            runner._active_origin_identity_path = root / "missing.json"
            runner._active_origin_binding = ("run-1", "marker-1", "S0001", 1)

            with unittest.mock.patch.object(
                product_runner,
                "close_bound_process_job",
            ) as close_job:
                runner._wait_for_termination_process(process)

        close_job.assert_called_once_with(process)
        self.assertGreaterEqual(process_probe.call_count, 2)
        self.assertIsNone(runner._cleanup_blocked_reason)

    def test_post_gate_missing_launch_baseline_blocks_reader_job_close_after_child_exit(self):
        runner = product_runner.ExtractionSubprocessRunner(
            process_factory=lambda *args, **kwargs: None,
            origin_process_probe=lambda *, timeout: (),
        )
        process = unittest.mock.Mock()
        process.poll.return_value = 0
        process._spectrum_organizer_job = object()
        runner._origin_start_gate_released = True
        runner._active_origin_binding = ("run-1", "marker-1", "S0001", 1)

        with unittest.mock.patch.object(
            product_runner,
            "close_bound_process_job",
        ) as close_job:
            with self.assertRaisesRegex(
                product_runner.ExtractionCleanupBlockedError,
                "缺少.*启动基线",
            ):
                runner._wait_for_termination_process(process)

        close_job.assert_not_called()
        self.assertIn("启动基线", runner._cleanup_blocked_reason)
        self.assertTrue(runner._origin_start_gate_released)

    def test_pre_extraction_retry_clears_retained_process_and_temp_root(self):
        with tempfile.TemporaryDirectory() as directory:
            ownership = product_runner.create_run_ownership(
                pathlib.Path(directory),
                "retry-run",
                "retry-marker",
                [],
            )
            root = ownership.temp_root
            runner = product_runner.PreExtractionSubprocessRunner(
                local_appdata=pathlib.Path(directory),
                process_factory=lambda *args, **kwargs: None,
            )
            process = unittest.mock.Mock()
            process.poll.return_value = 0
            process._spectrum_organizer_job = object()
            runner._current_process = process
            runner._cleanup_temp_root = root
            runner._cleanup_temp_root_identity = (
                ownership.temp_root_identity
            )
            runner._cleanup_blocked_reason = "先前清理失败"

            with unittest.mock.patch.object(
                product_runner,
                "close_bound_process_job",
            ):
                runner.retry_cancel_cleanup()

            self.assertIsNone(runner._current_process)
            self.assertIsNone(runner._cleanup_blocked_reason)
            self.assertIsNone(runner._cleanup_temp_root)
            self.assertFalse(root.exists())

    def test_active_temp_cleanup_refuses_an_existing_root_without_caller_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            ownership = product_runner.create_run_ownership(
                pathlib.Path(directory),
                "missing-caller-identity",
                "marker",
                [],
            )

            error = product_runner._cleanup_temp_root_error(
                ownership.temp_root
            )

            self.assertIn("caller-held", error)
            self.assertTrue(ownership.temp_root.exists())

    def test_child_summary_copy_hash_is_cancellable(self):
        with tempfile.TemporaryDirectory() as directory:
            context = _make_context(pathlib.Path(directory))
            snapshot_path = context.temp_root / "run_snapshot.sqlite3"
            source_summary = _record_valid_source(snapshot_path, context, "S0001")
            source = product_runner._build_extraction_sources(context, ExtractionSource)[0]

            with self.assertRaisesRegex(RuntimeError, "cancelled"):
                product_runner._validate_child_summary(
                    _summary_payload(snapshot_path, source_summary),
                    context=context,
                    expected_snapshot_path=snapshot_path,
                    expected_source=source,
                    cancel_check=lambda: (_ for _ in ()).throw(RuntimeError("cancelled")),
                )

    def test_child_summary_hashes_only_the_just_completed_source_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            context = _make_context(pathlib.Path(directory), source_count=2)
            snapshot_path = product_runner._register_snapshot_path(
                context,
                context.temp_root / "run_snapshot.sqlite3",
            )
            _record_valid_source(snapshot_path, context, "S0001")
            source_summary = _record_valid_source(snapshot_path, context, "S0002")
            sources = product_runner._build_extraction_sources(context, ExtractionSource)
            real_hash_file = product_runner.hash_file

            with unittest.mock.patch.object(
                product_runner,
                "hash_file",
                wraps=real_hash_file,
            ) as hash_file:
                product_runner._validate_child_summary(
                    _summary_payload(snapshot_path, source_summary),
                    context=context,
                    expected_snapshot_path=snapshot_path,
                    expected_source=sources[1],
                )

            self.assertEqual(1, hash_file.call_count)
            self.assertEqual(context.run_owned_source_copy_paths[1], hash_file.call_args.args[0])

    def test_final_source_id_scan_is_cancellable(self):
        with tempfile.TemporaryDirectory() as directory:
            context = _make_context(pathlib.Path(directory), source_count=2)
            snapshot_path = product_runner._register_snapshot_path(
                context,
                context.temp_root / "run_snapshot.sqlite3",
            )
            _record_valid_source(snapshot_path, context, "S0001")
            _record_valid_source(snapshot_path, context, "S0002")
            checks = []

            def cancel_check():
                checks.append(None)
                if len(checks) == 2:
                    raise RuntimeError("cancelled")

            with self.assertRaisesRegex(RuntimeError, "cancelled"):
                product_runner._validate_snapshot_source_ids(
                    snapshot_path,
                    ("S0001", "S0002"),
                    cancel_check=cancel_check,
                )

    def test_final_source_id_scan_rejects_orphan_partition_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            context = _make_context(pathlib.Path(directory))
            snapshot_path = product_runner._register_snapshot_path(
                context,
                context.temp_root / "run_snapshot.sqlite3",
            )
            _record_valid_source(snapshot_path, context, "S0001")
            connection = sqlite3.connect(snapshot_path)
            try:
                connection.execute(
                    """
                    insert into inventory_rows (
                        source_id, page_type, folder_path, short_name, display_name,
                        page_order, sheet_names_json, has_note, has_data
                    ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    ("S9999", "worksheet", "Root", "Extra", "Extra", 99, "[]", 1, 1),
                )
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(product_runner.ProductRunnerError, "source 集合"):
                product_runner._validate_snapshot_source_ids(snapshot_path, ("S0001",))

    def test_current_source_record_validation_uses_source_filtered_query(self):
        with tempfile.TemporaryDirectory() as directory:
            context = _make_context(pathlib.Path(directory), source_count=2)
            snapshot_path = product_runner._register_snapshot_path(
                context,
                context.temp_root / "run_snapshot.sqlite3",
            )
            _record_valid_source(
                snapshot_path,
                context,
                "S0001",
                include_original_provenance=True,
            )
            _record_valid_source(
                snapshot_path,
                context,
                "S0002",
                include_original_provenance=True,
            )
            sources = product_runner._build_extraction_sources(context, ExtractionSource)
            statements = []
            real_connect = product_runner.sqlite3.connect

            def recording_connect(*args, **kwargs):
                connection = real_connect(*args, **kwargs)
                connection.set_trace_callback(statements.append)
                return connection

            with unittest.mock.patch.object(
                product_runner.sqlite3,
                "connect",
                side_effect=recording_connect,
            ):
                product_runner._validate_snapshot_source_records(
                    snapshot_path,
                    context,
                    (sources[1],),
                )

            source_queries = [
                statement.lower()
                for statement in statements
                if "from source_files" in statement.lower()
            ]
            self.assertEqual(1, len(source_queries))
            self.assertIn("where source_id in ('s0002')", source_queries[0])

    def test_final_source_record_validation_reads_ownership_once(self):
        with tempfile.TemporaryDirectory() as directory:
            context = _make_context(pathlib.Path(directory), source_count=2)
            snapshot_path = product_runner._register_snapshot_path(
                context,
                context.temp_root / "run_snapshot.sqlite3",
            )
            _record_valid_source(
                snapshot_path,
                context,
                "S0001",
                include_original_provenance=True,
            )
            _record_valid_source(
                snapshot_path,
                context,
                "S0002",
                include_original_provenance=True,
            )
            sources = product_runner._build_extraction_sources(context, ExtractionSource)
            real_read_ownership = product_runner.read_ownership

            with unittest.mock.patch.object(
                product_runner,
                "read_ownership",
                wraps=real_read_ownership,
            ) as ownership_reads:
                product_runner._validate_snapshot_source_records(
                    snapshot_path,
                    context,
                    tuple(sources),
                )

            self.assertEqual(1, ownership_reads.call_count)

    def test_partition_digest_streams_rows_without_fetchall(self):
        rows = [("S0001", "value")]

        class Cursor:
            def __iter__(self):
                return iter(rows)

            def fetchall(self):
                raise AssertionError("partition digest must stream rows")

        class Connection:
            def execute(self, query, parameters):
                del query, parameters
                return Cursor()

            def close(self):
                return None

        with unittest.mock.patch.object(product_runner.sqlite3, "connect", return_value=Connection()):
            digest = product_runner._snapshot_partition_digest(pathlib.Path("snapshot.sqlite3"), "S0001")

        self.assertEqual(64, len(digest))

    def test_partition_digest_checks_cancellation_while_streaming_rows(self):
        class Cursor:
            def __iter__(self):
                return iter((("S0001", "one"), ("S0001", "two")))

        class Connection:
            def execute(self, query, parameters):
                del query, parameters
                return Cursor()

            def close(self):
                return None

        checks = []

        def cancel_check():
            checks.append(None)
            if len(checks) == 3:
                raise RuntimeError("cancelled")

        with unittest.mock.patch.object(product_runner.sqlite3, "connect", return_value=Connection()):
            with self.assertRaisesRegex(RuntimeError, "cancelled"):
                product_runner._snapshot_partition_digest(
                    pathlib.Path("snapshot.sqlite3"),
                    "S0001",
                    cancel_check=cancel_check,
                )

        self.assertEqual(3, len(checks))

    def test_termination_timeout_keeps_helper_tracked_and_blocks_reuse(self):
        class RunningHelper:
            def join(self, timeout):
                self.timeout = timeout

            def is_alive(self):
                return True

        helper = RunningHelper()
        runner = product_runner.ExtractionSubprocessRunner(cancellation_timeout=0.01)
        runner._termination_process = helper

        with self.assertRaisesRegex(product_runner.ExtractionCleanupBlockedError, "终止线程"):
            runner._wait_for_termination_process()

        self.assertIs(helper, runner._termination_process)
        with self.assertRaisesRegex(product_runner.ProductRunnerError, "清理状态不可确认"):
            runner.reset()

    def test_exited_helper_with_surviving_reader_permanently_blocks_runner_reuse(self):
        class FinishedHelper:
            def join(self, timeout):
                self.timeout = timeout

            def is_alive(self):
                return False

        class SurvivingReader:
            def poll(self):
                return None

        runner = product_runner.ExtractionSubprocessRunner()
        runner._termination_process = FinishedHelper()

        with self.assertRaisesRegex(product_runner.ExtractionCleanupBlockedError, "子进程仍在运行"):
            runner._wait_for_termination_process(SurvivingReader())

        self.assertIsNotNone(runner._cleanup_blocked_reason)
        with self.assertRaisesRegex(product_runner.ProductRunnerError, "清理状态不可确认"):
            runner.reset()
        with self.assertRaisesRegex(product_runner.ProductRunnerError, "清理状态不可确认"):
            runner(object())

    def test_reader_exit_with_surviving_origin_blocks_cleanup(self):
        class ExitedReader:
            def poll(self):
                return -1

        probe = unittest.mock.Mock(return_value=(types.SimpleNamespace(pid=7654),))
        runner = product_runner.ExtractionSubprocessRunner(
            origin_process_probe=probe,
            origin_shutdown_timeout=1.0,
            origin_shutdown_poll_interval=0.1,
        )
        runner._current_process = ExitedReader()

        with unittest.mock.patch.object(
            product_runner.time,
            "monotonic",
            side_effect=(0.0, 0.1, 0.2, 0.3, 1.1),
        ), unittest.mock.patch.object(product_runner.time, "sleep"):
            with self.assertRaisesRegex(product_runner.ExtractionCleanupBlockedError, "Origin|7654"):
                runner._wait_for_termination_process()
        self.assertEqual(2, probe.call_count)

    def test_reader_exit_waits_for_transient_origin_shutdown_before_cleanup(self):
        class ExitedReader:
            def poll(self):
                return -1

        probe = unittest.mock.Mock(
            side_effect=(
                (types.SimpleNamespace(pid=7654),),
                (types.SimpleNamespace(pid=7654),),
                (),
                (),
            )
        )
        runner = product_runner.ExtractionSubprocessRunner(
            origin_process_probe=probe,
            origin_shutdown_timeout=0.2,
            origin_shutdown_poll_interval=0.001,
        )
        runner._current_process = ExitedReader()

        runner._wait_for_termination_process()

        self.assertEqual(4, probe.call_count)

    def test_reader_exit_requires_two_consecutive_empty_origin_probes(self):
        class ExitedReader:
            def poll(self):
                return -1

        probe = unittest.mock.Mock(
            side_effect=(
                (),
                (types.SimpleNamespace(pid=7654),),
                (),
                (),
            )
        )
        runner = product_runner.ExtractionSubprocessRunner(
            origin_process_probe=probe,
            origin_shutdown_timeout=0.2,
            origin_shutdown_poll_interval=0.001,
        )
        runner._current_process = ExitedReader()

        runner._wait_for_termination_process()

        self.assertEqual(4, probe.call_count)

    def test_one_concrete_probe_contract_serves_preflight_and_shutdown_wait(self):
        timeouts = []

        def probe(*, timeout=5.0):
            timeouts.append(timeout)
            return ()

        product_runner._complete_origin_process_preflight(None, probe, None)
        runner = product_runner.ExtractionSubprocessRunner(
            origin_process_probe=probe,
            origin_shutdown_timeout=0.2,
            origin_shutdown_poll_interval=0.001,
        )
        runner._wait_for_origin_shutdown()

        self.assertEqual(3, len(timeouts))
        self.assertEqual(5.0, timeouts[0])
        self.assertTrue(all(0 < timeout <= 0.2 for timeout in timeouts[1:]))

    def test_origin_shutdown_rejects_second_empty_probe_that_finishes_after_deadline(self):
        call_count = 0

        def probe(*, timeout):
            del timeout
            nonlocal call_count
            call_count += 1
            return ()

        runner = product_runner.ExtractionSubprocessRunner(
            origin_process_probe=probe,
            origin_shutdown_timeout=1.0,
            origin_shutdown_poll_interval=0.1,
        )

        with unittest.mock.patch.object(
            product_runner.time,
            "monotonic",
            side_effect=(0.0, 0.1, 0.2, 0.9, 1.1),
        ), unittest.mock.patch.object(product_runner.time, "sleep"):
            with self.assertRaisesRegex(product_runner.ExtractionCleanupBlockedError, "超时"):
                runner._wait_for_origin_shutdown()

        self.assertEqual(2, call_count)

    def test_origin_shutdown_timeout_retains_last_nonempty_pid_after_empty_probe(self):
        probe = unittest.mock.Mock(
            side_effect=((types.SimpleNamespace(pid=7654),), ()),
        )
        runner = product_runner.ExtractionSubprocessRunner(
            origin_process_probe=probe,
            origin_shutdown_timeout=1.0,
            origin_shutdown_poll_interval=0.1,
        )

        with unittest.mock.patch.object(
            product_runner.time,
            "monotonic",
            side_effect=(0.0, 0.1, 0.2, 0.9, 1.1),
        ), unittest.mock.patch.object(product_runner.time, "sleep"):
            with self.assertRaisesRegex(product_runner.ExtractionCleanupBlockedError, "7654"):
                runner._wait_for_origin_shutdown()

    def test_transient_origin_probe_failure_is_retried_until_two_empty_probes(self):
        probe = unittest.mock.Mock(
            side_effect=(
                (types.SimpleNamespace(pid=7654),),
                RuntimeError("probe failed"),
                (),
                (),
            ),
        )
        runner = product_runner.ExtractionSubprocessRunner(
            origin_process_probe=probe,
            origin_shutdown_timeout=1.0,
            origin_shutdown_poll_interval=0.001,
        )

        runner._wait_for_origin_shutdown()

        self.assertEqual(4, probe.call_count)

    def test_origin_probe_failure_breaks_consecutive_empty_confirmation(self):
        probe = unittest.mock.Mock(
            side_effect=(
                (),
                RuntimeError("probe failed"),
                (),
                (),
            ),
        )
        runner = product_runner.ExtractionSubprocessRunner(
            origin_process_probe=probe,
            origin_shutdown_timeout=1.0,
            origin_shutdown_poll_interval=0.001,
        )

        runner._wait_for_origin_shutdown()

        self.assertEqual(4, probe.call_count)

    def test_persistent_origin_probe_failure_stops_at_overall_deadline(self):
        probe = unittest.mock.Mock(side_effect=RuntimeError("probe failed"))
        runner = product_runner.ExtractionSubprocessRunner(
            origin_process_probe=probe,
            origin_shutdown_timeout=1.0,
            origin_shutdown_poll_interval=0.1,
        )

        with unittest.mock.patch.object(
            product_runner.time,
            "monotonic",
            side_effect=(0.0, 0.1, 0.2, 1.1),
        ), unittest.mock.patch.object(product_runner.time, "sleep"):
            with self.assertRaisesRegex(product_runner.ExtractionCleanupBlockedError, "probe failed"):
                runner._wait_for_origin_shutdown()

        self.assertEqual(1, probe.call_count)

    def test_runner_preserves_temp_root_when_process_tree_state_is_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            context = _make_context(pathlib.Path(directory))
            runner = product_runner.ExtractionSubprocessRunner()
            with unittest.mock.patch.object(
                runner,
                "_run",
                side_effect=product_runner.ExtractionCleanupBlockedError("进程树状态无法确认"),
            ):
                with self.assertRaisesRegex(product_runner.ExtractionCleanupBlockedError, "进程树状态无法确认"):
                    runner(context)

            self.assertTrue(context.temp_root.exists())
            self.assertEqual("进程树状态无法确认", runner._cleanup_blocked_reason)
            with self.assertRaisesRegex(product_runner.ProductRunnerError, "清理状态不可确认"):
                runner.reset()
            with self.assertRaisesRegex(product_runner.ProductRunnerError, "清理状态不可确认"):
                runner(context)

    def test_snapshot_registers_possible_sqlite_sidecars_as_owned_children(self):
        with tempfile.TemporaryDirectory() as directory:
            context = _make_context(pathlib.Path(directory))
            snapshot_path = product_runner._register_snapshot_path(context, context.temp_root / "run_snapshot.sqlite3")
            allowed = {path.name for path in read_ownership(context.temp_root).allowed_children}

            self.assertEqual(context.temp_root / "run_snapshot.sqlite3", snapshot_path)
            self.assertTrue({"run_snapshot.sqlite3", "run_snapshot.sqlite3-journal", "run_snapshot.sqlite3-wal", "run_snapshot.sqlite3-shm"}.issubset(allowed))

    def test_cancel_registers_termination_before_worker_can_finish_shutdown(self):
        with tempfile.TemporaryDirectory() as directory:
            context = _make_context(pathlib.Path(directory))
            child_started = threading.Event()
            release_child = threading.Event()
            terminate_entered = threading.Event()
            release_terminate = threading.Event()
            termination_joined = threading.Event()

            class Child:
                returncode = -1

                def communicate(self, timeout=None):
                    child_started.set()
                    if not release_child.wait(timeout):
                        raise product_runner.subprocess.TimeoutExpired("fake", timeout)
                    return "", ""

                def poll(self):
                    return self.returncode

            class TerminationThread:
                def join(self, timeout=None):
                    del timeout
                    termination_joined.set()

                def is_alive(self):
                    return False

            def terminate(_process):
                terminate_entered.set()
                release_terminate.wait(1)
                return TerminationThread()

            runner = product_runner.ExtractionSubprocessRunner(
                process_factory=lambda command, **kwargs: Child(),
                cancellation_poll_interval=0.01,
            )
            errors = []
            run_thread = threading.Thread(target=lambda: self._capture_error(errors, runner, context))
            with unittest.mock.patch.object(product_runner, "_terminate_process_nonblocking", side_effect=terminate):
                run_thread.start()
                self.assertTrue(child_started.wait(5))
                cancel_thread = threading.Thread(target=runner.cancel)
                cancel_thread.start()
                self.assertTrue(terminate_entered.wait(1))
                release_child.set()
                time.sleep(0.03)
                self.assertTrue(run_thread.is_alive())
                release_terminate.set()
                cancel_thread.join(1)
                run_thread.join(1)

            self.assertFalse(run_thread.is_alive())
            self.assertTrue(termination_joined.is_set())
            self.assertRegex(str(errors[0]), "取消")

    def test_job_close_blocks_late_termination_helper_registration(self):
        runner = product_runner.ExtractionSubprocessRunner(
            cancellation_timeout=0.2,
            origin_process_probe=lambda *, timeout: (),
        )

        class Process:
            def poll(self):
                return 0

        class Helper:
            def __init__(self):
                self.joined = False

            def join(self, timeout=None):
                del timeout
                self.joined = True

            def is_alive(self):
                return False

        process = Process()
        helper = Helper()
        runner._current_process = process

        def close_job(_process):
            runner.cancel()

        with (
            unittest.mock.patch.object(product_runner, "close_bound_process_job", side_effect=close_job),
            unittest.mock.patch.object(
                product_runner,
                "_terminate_process_nonblocking",
                return_value=helper,
            ) as terminate,
        ):
            runner._wait_for_termination_process(process)

        terminate.assert_not_called()
        self.assertFalse(helper.joined)
        self.assertIsNone(runner._termination_process)
        self.assertTrue(runner._termination_finalized)

    def test_termination_helper_completion_waits_for_child_exit(self):
        runner = product_runner.ExtractionSubprocessRunner(
            cancellation_timeout=0.2,
            origin_process_probe=lambda *, timeout: (),
        )

        class Process:
            def __init__(self):
                self.returncode = None
                self.wait_calls = []

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                self.wait_calls.append(timeout)
                self.returncode = 0
                return self.returncode

        class Helper:
            _spectrum_organizer_termination_state = {"error": None}

            def join(self, timeout=None):
                del timeout

            def is_alive(self):
                return False

        process = Process()
        runner._termination_process = Helper()

        with (
            unittest.mock.patch.object(product_runner, "close_bound_process_job"),
            unittest.mock.patch.object(runner, "_close_run_owned_origin"),
            unittest.mock.patch.object(runner, "_wait_for_origin_shutdown"),
        ):
            runner._wait_for_termination_process(process)

        self.assertEqual(process.wait_calls, [0.2])
        self.assertTrue(runner._termination_finalized)

    def test_normal_extraction_has_no_fixed_communicate_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            context = _make_context(base)
            communicate_calls = []

            def process_factory(command, **kwargs):
                del kwargs
                manifest = json.loads(pathlib.Path(command[-2]).read_text(encoding="utf-8"))
                result_path = pathlib.Path(command[-1])
                snapshot_path = pathlib.Path(manifest["snapshot_path"])
                summary = _record_valid_source(snapshot_path, context, manifest["source_id"])
                child = _CompletedChild(result_path, _summary_payload(snapshot_path, summary), {"active": 0, "max_active": 0})
                def communicate(timeout=None):
                    communicate_calls.append(timeout)
                    if len(communicate_calls) < 3:
                        raise product_runner.subprocess.TimeoutExpired("fake", timeout)
                    child._activity["active"] -= 1
                    return child._stdout, ""

                child.communicate = communicate
                return child

            product_runner.ExtractionSubprocessRunner(process_factory=process_factory)(context)

            self.assertEqual(3, len(communicate_calls))

    def test_reader_cannot_observe_original_paths_in_shared_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            context = _make_context(base)
            observed_original_paths = []

            def process_factory(command, **kwargs):
                del kwargs
                manifest = json.loads(pathlib.Path(command[-2]).read_text(encoding="utf-8"))
                result_path = pathlib.Path(command[-1])
                snapshot_path = pathlib.Path(manifest["snapshot_path"])
                connection = sqlite3.connect(snapshot_path)
                try:
                    has_source_table = connection.execute(
                        "select 1 from sqlite_master "
                        "where type = 'table' and name = 'source_files'"
                    ).fetchone()
                    observed_original_paths.append(
                        ()
                        if has_source_table is None
                        else tuple(
                            row[0]
                            for row in connection.execute(
                                "select original_path from source_files "
                                "where original_path is not null"
                            )
                        )
                    )
                finally:
                    connection.close()
                summary = _record_valid_source(
                    snapshot_path,
                    context,
                    manifest["source_id"],
                    reader_attempt=manifest["reader_attempt"],
                )
                return _CompletedChild(
                    result_path,
                    _summary_payload(snapshot_path, summary),
                    {"active": 0, "max_active": 0},
                )

            product_runner.ExtractionSubprocessRunner(
                process_factory=process_factory
            )(context)

            self.assertEqual([()], observed_original_paths)

    def test_child_failure_cleans_owned_temp_root(self):
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            context = _make_context(base)

            class FailedChild:
                returncode = 1

                def communicate(self, timeout=None):
                    del timeout
                    return "", "child failed"

                def poll(self):
                    return self.returncode

            runner = product_runner.ExtractionSubprocessRunner(
                process_factory=lambda command, **kwargs: FailedChild()
            )

            with self.assertRaisesRegex(product_runner.ProductRunnerError, "child failed"):
                runner(context)

            self.assertFalse(context.temp_root.exists())

    def test_reader_attempt_rejects_preexisting_result_file(self):
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            context = _make_context(base)
            product_runner._prepare_reader_temp_root(context)
            snapshot_path = product_runner._register_snapshot_path(
                context,
                context.temp_root / "run_snapshot.sqlite3",
            )
            source = product_runner._build_extraction_sources(context, ExtractionSource)[0]
            stale_result = context.temp_root / "extraction_result.S0001.attempt1.json"
            stale_result.write_text(
                json.dumps({"ok": True, "summary": {}}),
                encoding="utf-8",
            )

            class SilentChild:
                returncode = 0

                def communicate(self, timeout=None):
                    del timeout
                    return "", ""

                def poll(self):
                    return self.returncode

            runner = product_runner.ExtractionSubprocessRunner(
                process_factory=lambda command, **kwargs: SilentChild()
            )

            with self.assertRaisesRegex(product_runner.ProductRunnerError, "result|结果|已存在"):
                runner._run_reader_process_attempt(context, source, snapshot_path, 1)

    def test_reader_attempt_rejects_preexisting_final_process_sidecars_before_launch(self):
        for sidecar_name in (
            "origin_launch.S0001.attempt1.json",
            "origin_identity.S0001.attempt1.json",
            "origin_open_target.S0001.attempt1.json",
        ):
            with self.subTest(sidecar_name=sidecar_name), tempfile.TemporaryDirectory() as directory:
                base = pathlib.Path(directory)
                context = _make_context(base)
                product_runner._prepare_reader_temp_root(context)
                snapshot_path = product_runner._register_snapshot_path(
                    context,
                    context.temp_root / "run_snapshot.sqlite3",
                )
                source = product_runner._build_extraction_sources(context, ExtractionSource)[0]
                sidecar_path = context.temp_root / sidecar_name
                sidecar_path.write_text("preexisting", encoding="utf-8")
                process_factory = unittest.mock.Mock(
                    side_effect=AssertionError("child was launched")
                )
                runner = product_runner.ExtractionSubprocessRunner(
                    process_factory=process_factory
                )

                with self.assertRaisesRegex(product_runner.ProductRunnerError, "已存在"):
                    runner._run_reader_process_attempt(context, source, snapshot_path, 1)

                process_factory.assert_not_called()
                self.assertEqual("preexisting", sidecar_path.read_text(encoding="utf-8"))

    def test_reader_attempt_interrupt_requests_termination_before_waiting(self):
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            context = _make_context(base)
            product_runner._prepare_reader_temp_root(context)
            snapshot_path = product_runner._register_snapshot_path(
                context,
                context.temp_root / "run_snapshot.sqlite3",
            )
            source = product_runner._build_extraction_sources(context, ExtractionSource)[0]
            events = []

            class InterruptingChild:
                returncode = None

                def communicate(self, timeout=None):
                    del timeout
                    raise KeyboardInterrupt

                def poll(self):
                    return self.returncode

            child = InterruptingChild()
            runner = product_runner.ExtractionSubprocessRunner(
                process_factory=lambda command, **kwargs: child
            )

            def request_termination(process):
                events.append(("terminate", process))

            def wait_for_termination(process):
                events.append(("wait", process))
                process.returncode = 0

            runner._request_termination_locked = request_termination
            runner._wait_for_termination_process = wait_for_termination

            with self.assertRaises(KeyboardInterrupt):
                runner._run_reader_process_attempt(context, source, snapshot_path, 1)

            self.assertEqual([("terminate", child), ("wait", child)], events)

    def test_reader_attempt_interrupt_preserves_primary_when_wait_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            context = _make_context(base)
            product_runner._prepare_reader_temp_root(context)
            snapshot_path = product_runner._register_snapshot_path(
                context,
                context.temp_root / "run_snapshot.sqlite3",
            )
            source = product_runner._build_extraction_sources(context, ExtractionSource)[0]

            class InterruptingChild:
                returncode = None

                def communicate(self, timeout=None):
                    del timeout
                    raise KeyboardInterrupt

                def poll(self):
                    return self.returncode

            child = InterruptingChild()
            runner = product_runner.ExtractionSubprocessRunner(
                process_factory=lambda command, **kwargs: child
            )
            runner._request_termination_locked = lambda process: None
            runner._wait_for_termination_process = lambda process: (_ for _ in ()).throw(
                product_runner.ExtractionCleanupBlockedError("cleanup blocked")
            )

            with self.assertRaises(KeyboardInterrupt) as captured:
                runner._run_reader_process_attempt(context, source, snapshot_path, 1)

            self.assertIn("cleanup blocked", "\n".join(captured.exception.__notes__))
            self.assertIs(child, runner._current_process)

    def test_reader_attempt_does_not_overwrite_preexisting_manifest_hard_link(self):
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            context = _make_context(base)
            product_runner._prepare_reader_temp_root(context)
            snapshot_path = product_runner._register_snapshot_path(
                context,
                context.temp_root / "run_snapshot.sqlite3",
            )
            source = product_runner._build_extraction_sources(context, ExtractionSource)[0]
            manifest_path = context.temp_root / "extraction_context.S0001.attempt1.json"
            sentinel_path = base / "external-sentinel.txt"
            sentinel_bytes = b"must remain unchanged"
            sentinel_path.write_bytes(sentinel_bytes)
            manifest_path.hardlink_to(sentinel_path)
            process_factory = unittest.mock.Mock()
            runner = product_runner.ExtractionSubprocessRunner(process_factory=process_factory)

            with self.assertRaisesRegex(product_runner.ProductRunnerError, "manifest|清单|已存在"):
                runner._run_reader_process_attempt(context, source, snapshot_path, 1)

            self.assertEqual(sentinel_bytes, sentinel_path.read_bytes())
            process_factory.assert_not_called()

    def test_reader_result_envelope_requires_exact_fields_and_boolean_ok(self):
        invalid_payloads = (
            {"ok": "false", "summary": {}},
            {"ok": 1, "summary": {}},
            {"ok": True, "summary": {}, "unexpected": True},
            {"ok": False, "error": "failed"},
            {
                "ok": False,
                "error": "failed",
                "error_type": "ProductRunnerError",
                "error_notes": [1],
            },
        )
        with tempfile.TemporaryDirectory() as directory:
            result_path = pathlib.Path(directory) / "result.json"
            for payload in invalid_payloads:
                with self.subTest(payload=payload):
                    result_path.write_text(json.dumps(payload), encoding="utf-8")
                    with self.assertRaises(product_runner.ProductRunnerError):
                        product_runner._read_extraction_process_result(result_path)

    def test_child_entry_requires_exactly_one_source_id_and_shared_snapshot_path(self):
        from spectrum_organizer.origin.extraction_process import extraction_process_main

        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            context = _make_context(base, source_count=2)
            product_runner._prepare_reader_temp_root(context)
            manifest_path = context.temp_root / "manifest.json"
            result_path = context.temp_root / "result.json"
            snapshot_path = product_runner._register_snapshot_path(
                context,
                context.temp_root / "run_snapshot.sqlite3",
            )
            ownership = read_ownership(context.temp_root)
            for path in (
                manifest_path,
                result_path,
                result_path.with_name(f"{result_path.name}.pending"),
                context.temp_root / product_runner.ACTIVE_LEASE_FILE,
            ):
                if path not in ownership.allowed_children:
                    ownership = add_allowed_child(ownership, path)
            source = product_runner._build_extraction_sources(context, ExtractionSource)[1]
            command = product_runner._build_reader_process_command(context, source, snapshot_path)
            manifest_path.write_text(
                json.dumps(product_runner._reader_command_to_payload(command)),
                encoding="utf-8",
            )
            ownership = bind_allowed_child_identity(ownership, manifest_path)
            seen = []

            def extraction_runner(received_command):
                seen.append(
                    (
                        received_command.run_id,
                        received_command.source_copy.source_id,
                        received_command.snapshot_path,
                    )
                )
                return product_runner.ReaderSourceExtractionSummary(
                    received_command.snapshot_path,
                    received_command.source_copy.source_id,
                    0,
                    0,
                    0,
                    0,
                )

            return_code = extraction_process_main(
                _worker_args(manifest_path, result_path),
                extraction_runner=extraction_runner,
            )

            self.assertEqual(0, return_code)
            self.assertEqual([("run-1", "S0002", snapshot_path)], seen)
            self.assertTrue(json.loads(result_path.read_text(encoding="utf-8"))["ok"])

    def test_child_entry_does_not_publish_partial_reader_result(self):
        from spectrum_organizer.origin.extraction_process import extraction_process_main

        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            context = _make_context(base)
            product_runner._prepare_reader_temp_root(context)
            manifest_path = context.temp_root / "manifest.json"
            result_path = context.temp_root / "result.json"
            pending_path = result_path.with_name(f"{result_path.name}.pending")
            snapshot_path = product_runner._register_snapshot_path(
                context,
                context.temp_root / "run_snapshot.sqlite3",
            )
            ownership = read_ownership(context.temp_root)
            for path in (
                manifest_path,
                result_path,
                pending_path,
                context.temp_root / product_runner.ACTIVE_LEASE_FILE,
            ):
                if path not in ownership.allowed_children:
                    ownership = add_allowed_child(ownership, path)
            source = product_runner._build_extraction_sources(context, ExtractionSource)[0]
            command = product_runner._build_reader_process_command(context, source, snapshot_path)
            manifest_path.write_text(
                json.dumps(product_runner._reader_command_to_payload(command)),
                encoding="utf-8",
            )
            ownership = bind_allowed_child_identity(ownership, manifest_path)

            def fail_after_partial_write(_payload, stream, **_kwargs):
                stream.write('{"ok":')
                raise OSError("disk full")

            with unittest.mock.patch.object(
                product_runner.json,
                "dump",
                side_effect=fail_after_partial_write,
            ):
                return_code = extraction_process_main(
                    _worker_args(manifest_path, result_path),
                    extraction_runner=lambda received: product_runner.ReaderSourceExtractionSummary(
                        received.snapshot_path,
                        received.source_copy.source_id,
                        0,
                        0,
                        0,
                        0,
                    ),
                )

            self.assertEqual(1, return_code)
            self.assertFalse(result_path.exists())
            self.assertFalse(pending_path.exists())

    def test_atomic_result_writer_preserves_preexisting_pending_collision(self):
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            result_path = base / "result.json"
            pending_path = result_path.with_name(f"{result_path.name}.pending")
            sentinel_path = base / "sentinel.txt"
            sentinel_path.write_text("preexisting", encoding="utf-8")
            pending_path.hardlink_to(sentinel_path)

            with self.assertRaises(FileExistsError):
                product_runner._write_json_atomic_exclusive(
                    result_path,
                    {"ok": True},
                )

            self.assertFalse(result_path.exists())
            self.assertTrue(pending_path.exists())
            self.assertEqual("preexisting", pending_path.read_text(encoding="utf-8"))
            self.assertEqual("preexisting", sentinel_path.read_text(encoding="utf-8"))

    def test_atomic_result_writer_preserves_failed_pending_cleanup_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            result_path = base / "result.json"
            pending_path = result_path.with_name(f"{result_path.name}.pending")

            def fail_after_partial_write(_payload, stream, **_kwargs):
                stream.write('{"ok":')
                raise OSError("disk full")

            def fail_pending_cleanup(path, identity):
                raise product_runner.IdentityPathError(
                    pathlib.Path(path),
                    "pending cleanup failed",
                )

            with (
                unittest.mock.patch.object(
                    product_runner.json,
                    "dump",
                    side_effect=fail_after_partial_write,
                ),
                unittest.mock.patch.object(
                    extraction_ipc,
                    "unlink_owned_path",
                    side_effect=fail_pending_cleanup,
                ),
                self.assertRaises(OSError) as raised,
            ):
                product_runner._write_json_atomic_exclusive(
                    result_path,
                    {"ok": True},
                )

            self.assertTrue(pending_path.exists())
            self.assertEqual(
                ((pending_path, product_runner.path_identity(pending_path)),),
                getattr(raised.exception, "retained_owned_identities", ()),
            )

    def test_atomic_result_writer_preserves_final_and_pending_after_post_link_cleanup_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            result_path = base / "result.json"
            pending_path = result_path.with_name(f"{result_path.name}.pending")

            def fail_pending_cleanup(path, identity):
                raise product_runner.IdentityPathError(
                    pathlib.Path(path),
                    "pending cleanup failed after link",
                )

            with (
                unittest.mock.patch.object(
                    extraction_ipc,
                    "unlink_owned_path",
                    side_effect=fail_pending_cleanup,
                ),
                self.assertRaises(product_runner.IdentityPathError) as raised,
            ):
                product_runner._write_json_atomic_exclusive(
                    result_path,
                    {"ok": True},
                )

            identity = product_runner.path_identity(result_path)
            self.assertEqual(identity, product_runner.path_identity(pending_path))
            self.assertEqual(
                {
                    (result_path, identity),
                    (pending_path, identity),
                },
                set(getattr(raised.exception, "retained_owned_identities", ())),
            )

    def test_exclusive_result_writer_preserves_identity_when_cleanup_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            result_path = pathlib.Path(directory) / "result.json"

            def fail_write(_payload, stream, **_kwargs):
                stream.write('{"ok":')
                raise OSError("disk full")

            def fail_cleanup(path, identity):
                raise product_runner.IdentityPathError(
                    pathlib.Path(path),
                    "exclusive cleanup failed",
                )

            with (
                unittest.mock.patch.object(
                    product_runner.json,
                    "dump",
                    side_effect=fail_write,
                ),
                unittest.mock.patch.object(
                    extraction_ipc,
                    "unlink_owned_path",
                    side_effect=fail_cleanup,
                ),
                self.assertRaises(OSError) as raised,
            ):
                product_runner._write_json_exclusive(
                    result_path,
                    {"ok": True},
                )

            self.assertEqual(
                ((result_path, product_runner.path_identity(result_path)),),
                getattr(raised.exception, "retained_owned_identities", ()),
            )

    def test_atomic_result_writer_rejects_late_final_collision(self):
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            result_path = base / "result.json"
            pending_path = result_path.with_name(f"{result_path.name}.pending")
            sentinel_path = base / "sentinel.txt"
            sentinel_path.write_text("late collision", encoding="utf-8")
            real_link = product_runner.os.link

            def inject_collision_then_link(source, destination):
                real_link(sentinel_path, result_path)
                return real_link(source, destination)

            with (
                unittest.mock.patch.object(
                    product_runner.os,
                    "link",
                    side_effect=inject_collision_then_link,
                ),
                self.assertRaises(FileExistsError),
            ):
                product_runner._write_json_atomic_exclusive(
                    result_path,
                    {"ok": True},
                )

            self.assertEqual("late collision", result_path.read_text(encoding="utf-8"))
            self.assertEqual("late collision", sentinel_path.read_text(encoding="utf-8"))
            self.assertFalse(pending_path.exists())

    def test_child_entry_preserves_exception_notes_in_reader_result(self):
        from spectrum_organizer.origin.extraction_process import extraction_process_main

        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            context = _make_context(base)
            product_runner._prepare_reader_temp_root(context)
            manifest_path = context.temp_root / "manifest.json"
            result_path = context.temp_root / "result.json"
            pending_path = result_path.with_name(f"{result_path.name}.pending")
            snapshot_path = product_runner._register_snapshot_path(
                context,
                context.temp_root / "run_snapshot.sqlite3",
            )
            ownership = read_ownership(context.temp_root)
            for path in (
                manifest_path,
                result_path,
                pending_path,
                context.temp_root / product_runner.ACTIVE_LEASE_FILE,
            ):
                if path not in ownership.allowed_children:
                    ownership = add_allowed_child(ownership, path)
            source = product_runner._build_extraction_sources(context, ExtractionSource)[0]
            command = product_runner._build_reader_process_command(context, source, snapshot_path)
            manifest_path.write_text(
                json.dumps(product_runner._reader_command_to_payload(command)),
                encoding="utf-8",
            )
            ownership = bind_allowed_child_identity(ownership, manifest_path)

            def fail_with_note(_received):
                error = RuntimeError("primary reader failure")
                error.add_note("secondary reader failure")
                raise error

            return_code = extraction_process_main(
                _worker_args(manifest_path, result_path),
                extraction_runner=fail_with_note,
            )
            payload = json.loads(result_path.read_text(encoding="utf-8"))

            self.assertEqual(1, return_code)
            self.assertEqual(["secondary reader failure"], payload["error_notes"])

    def test_child_entry_does_not_overwrite_result_hard_link_created_during_extraction(self):
        from spectrum_organizer.origin.extraction_process import extraction_process_main

        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            context = _make_context(base)
            product_runner._prepare_reader_temp_root(context)
            manifest_path = context.temp_root / "manifest.json"
            result_path = context.temp_root / "result.json"
            snapshot_path = product_runner._register_snapshot_path(
                context,
                context.temp_root / "run_snapshot.sqlite3",
            )
            ownership = read_ownership(context.temp_root)
            for path in (
                manifest_path,
                result_path,
                result_path.with_name(f"{result_path.name}.pending"),
                context.temp_root / product_runner.ACTIVE_LEASE_FILE,
            ):
                if path not in ownership.allowed_children:
                    ownership = add_allowed_child(ownership, path)
            source = product_runner._build_extraction_sources(context, ExtractionSource)[0]
            command = product_runner._build_reader_process_command(context, source, snapshot_path)
            manifest_path.write_text(
                json.dumps(product_runner._reader_command_to_payload(command)),
                encoding="utf-8",
            )
            ownership = bind_allowed_child_identity(ownership, manifest_path)
            sentinel_path = base / "external-sentinel.txt"
            sentinel_bytes = b"must remain unchanged"
            sentinel_path.write_bytes(sentinel_bytes)

            def extraction_runner(received_command):
                result_path.hardlink_to(sentinel_path)
                return product_runner.ReaderSourceExtractionSummary(
                    received_command.snapshot_path,
                    received_command.source_copy.source_id,
                    0,
                    0,
                    0,
                    0,
                )

            return_code = extraction_process_main(
                _worker_args(manifest_path, result_path),
                extraction_runner=extraction_runner,
            )

            self.assertEqual(1, return_code)
            self.assertEqual(sentinel_bytes, sentinel_path.read_bytes())

    def test_reader_command_rejects_legacy_fields_mistyped_identity_and_lowered_space_budget(self):
        base = pathlib.Path("C:/owned-temp")
        valid = {
            "run_id": "run-1",
            "marker_id": "marker-1",
            "source_id": "S0001",
            "copy_path": str(base / "source-0001" / "raw.opju"),
            "copy_sha256": "a" * 64,
            "copy_size_bytes": 100,
            "settings_snapshot": {"s1Limit": 1_000_000, "steadyEmissionY": "S1c"},
            "snapshot_path": str(base / "run_snapshot.sqlite3"),
            "required_temp_bytes": 1024**3,
        }
        mutations = {
            "legacy field": lambda payload: payload.update({"selected_source_paths": ["C:/raw.opju"]}),
            "run id type": lambda payload: payload.update({"run_id": 1}),
            "empty marker": lambda payload: payload.update({"marker_id": ""}),
            "source id format": lambda payload: payload.update({"source_id": "raw"}),
            "copy path type": lambda payload: payload.update({"copy_path": 1}),
            "copy hash format": lambda payload: payload.update({"copy_sha256": "bad"}),
            "snapshot path type": lambda payload: payload.update({"snapshot_path": []}),
            "settings keys": lambda payload: payload["settings_snapshot"].update({"unexpected": True}),
            "space budget": lambda payload: payload.update({"required_temp_bytes": 0}),
        }

        for label, mutate in mutations.items():
            with self.subTest(label=label):
                payload = dict(valid)
                payload["settings_snapshot"] = dict(valid["settings_snapshot"])
                mutate(payload)
                with self.assertRaises(product_runner.ProductRunnerError):
                    product_runner._reader_command_from_payload(payload)

    def test_reader_summary_rejects_unknown_fields_mistyped_identity_and_open_counts(self):
        valid = {
            "snapshot_path": "C:/owned-temp/run_snapshot.sqlite3",
            "source_id": "S0001",
            "inventory_count": 1,
            "result_count": 1,
            "extracted_count": 1,
            "rejected_count": 0,
        }
        mutations = {
            "unknown field": lambda payload: payload.update({"original_path": "C:/raw.opju"}),
            "snapshot path type": lambda payload: payload.update({"snapshot_path": []}),
            "source id type": lambda payload: payload.update({"source_id": 1}),
            "negative count": lambda payload: payload.update({"inventory_count": -1}),
            "open counts": lambda payload: payload.update({"result_count": 2}),
        }

        for label, mutate in mutations.items():
            with self.subTest(label=label):
                payload = dict(valid)
                mutate(payload)
                with self.assertRaises(product_runner.ProductRunnerError):
                    product_runner._reader_summary_from_payload(payload)

    @staticmethod
    def _capture_error(errors, runner, context):
        try:
            runner(context)
        except Exception as exc:
            errors.append(exc)


if __name__ == "__main__":
    unittest.main()
