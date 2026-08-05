import io
import json
import os
import pathlib
import sqlite3
import tempfile
import sys
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import spectrum_organizer.product_runner as product_runner
from spectrum_organizer.product_runner import (
    ApprovedAttribution,
    ApprovedAuditItem,
    ApprovedBookIdentity,
    ApprovedReviewChoice,
    ApprovedReviewRequirement,
    CountReconciliation,
    FinalProcessCountHook,
    ManualDialogSmokeInputs,
    ProductManualDialogFlow,
    ProductRunnerDependencies,
    ProductRunnerError,
    ProductWorkflowRunner,
    ProtectedPathAuditHook,
    ProtectedPathAuditPlan,
    SourceInputIssue,
    approve_output_plan,
    assert_ready_for_task15,
    build_default_product_dependencies,
    check_task15_readiness,
)
from spectrum_organizer.__main__ import readiness_main
from spectrum_organizer.ui.dialog_port import DialogResponse
from spectrum_organizer.ui.orchestrator import BookOnlyOrchestrator
from spectrum_organizer.ui.state_machine import TaskStateMachine


def _book_key(
    short_name,
    *,
    source_id="S0001",
    page_type="worksheet",
    folder_path="/",
):
    return json.dumps(
        [source_id, page_type, folder_path, short_name],
        ensure_ascii=False,
        separators=(",", ":"),
    )


BOOK_A = _book_key("book-a")
BOOK_B = _book_key("book-b")
BAD = _book_key("bad")
DUPLICATE = _book_key("duplicate")
GHOST = _book_key("ghost")
OTHER = _book_key("other")
OTHER_1 = _book_key("other-1")
OTHER_2 = _book_key("other-2")
BOOK_REAL_REJECTED = _book_key("book-real-rejected")
BOOK_INVENTED = _book_key("book-invented")
BOOK_EXTRA = _book_key("book-extra")
BOOK_MISSING = _book_key("book-missing")
_APPROVAL_NOTE = (
    "[EXP_FD_FILE]\n"
    "Acquisition Type = Spectral Acquisition[Emission]\n"
    "[EX1]\n"
    "Park = 270\n"
    "Front Entrance Slit = 2\n"
    "Front Exit Slit = 2\n"
    "[EM1]\n"
    "Start = 350\n"
    "End = 650\n"
    "Increment = 1\n"
    "Front Entrance Slit = 2\n"
    "Front Exit Slit = 2\n"
)


def _book_identity_parts(book_key):
    source_id, page_type, folder_path, short_name = json.loads(book_key)
    return source_id, page_type, folder_path, short_name


def _approved_audit_item(
    book_key,
    detail,
    *,
    reason_code="candidate_rejection",
    decision_source="automatic",
    evidence=(),
):
    source_id, page_type, folder_path, short_name = _book_identity_parts(
        book_key
    )
    return ApprovedAuditItem(
        book_key=book_key,
        detail=detail,
        source_id=source_id,
        source_filename="a.opju",
        page_type=page_type,
        folder_path=folder_path,
        short_name=short_name,
        display_name=short_name,
        reason_code=reason_code,
        evidence=evidence,
        decision_source=decision_source,
    )


def _approved_attribution(
    book_key,
    canonical_label,
    system_label,
    temperature,
    *,
    sample_system_identity=None,
):
    source_id, page_type, folder_path, short_name = _book_identity_parts(
        book_key
    )
    return ApprovedAttribution(
        book_key=book_key,
        canonical_sample_label=canonical_label,
        sample_system_label=system_label,
        temperature=temperature,
        sample_system_identity=(
            system_label
            if sample_system_identity is None
            else sample_system_identity
        ),
        source_id=source_id,
        source_filename="a.opju",
        page_type=page_type,
        folder_path=folder_path,
        short_name=short_name,
        display_name=short_name,
        payload_checksum="c" * 64,
    )


def _approved_book_identity(book_key):
    source_id, page_type, folder_path, short_name = _book_identity_parts(
        book_key
    )
    return ApprovedBookIdentity(
        book_key=book_key,
        source_id=source_id,
        source_filename="a.opju",
        page_type=page_type,
        folder_path=folder_path,
        short_name=short_name,
        display_name=short_name,
        payload_checksum="c" * 64,
        raw_display_name=short_name,
        spectrum_class="steady_emission",
        selected_y_column="S1c",
        paired_x_column="X",
    )


_APPROVAL_TEMP_ROOT = tempfile.TemporaryDirectory()
_APPROVAL_SOURCE_PATH = (
    pathlib.Path(_APPROVAL_TEMP_ROOT.name) / "a.opju"
)
_APPROVAL_SOURCE_PATH.write_bytes(b"approved-source")


def _approval_source_snapshot():
    from spectrum_organizer.safety.fingerprints import snapshot_sources

    return snapshot_sources([_APPROVAL_SOURCE_PATH], [])[0]


def _approval_snapshot(
    *book_keys,
    display_names=None,
    rejected_book_keys=(),
):
    path = (
        pathlib.Path(_APPROVAL_TEMP_ROOT.name)
        / f"snapshot-{len(tuple(pathlib.Path(_APPROVAL_TEMP_ROOT.name).iterdir()))}.sqlite3"
    )
    connection = sqlite3.connect(path)
    try:
        rejected = set(rejected_book_keys)
        source = _approval_source_snapshot()
        connection.execute(
            "create table source_files ("
            "source_id text primary key, copy_path text not null, "
            "sha256 text not null, original_path text, "
            "original_size_bytes integer, original_mtime_ns integer)"
        )
        connection.execute(
            "insert into source_files values (?, ?, ?, ?, ?, ?)",
            (
                "S0001",
                str(
                    pathlib.Path(_APPROVAL_TEMP_ROOT.name)
                    / "owned-copy"
                    / "a.opju"
                ),
                source.sha256,
                os.path.normcase(str(source.path.resolve())),
                source.size_bytes,
                source.mtime_ns,
            ),
        )
        connection.execute(
            "create table book_results ("
            "source_id text, page_type text, folder_path text, "
            "short_name text, display_name text, payload_checksum text, "
            "status text, rejection_reason text, "
            "selected_x_values_json text, selected_y_values_json text, "
            "note_text text, spectrum_class text, "
            "selected_y_column text, paired_x_column text, "
            "s1_max_for_limit_json text, "
            "s1_max_for_limit_x_json text, "
            "max_planned_y_json text, "
            "max_planned_y_x_json text)"
        )
        connection.executemany(
            "insert into book_results values "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            tuple(
                (
                    *_book_identity_parts(book_key),
                    (
                        _book_identity_parts(book_key)[-1]
                        if display_names is None
                        else display_names[index]
                    ),
                    "c" * 64,
                    (
                        "rejected"
                        if book_key in rejected
                        else "extracted"
                    ),
                    (
                        "invalid data"
                        if book_key in rejected
                        else None
                    ),
                    json.dumps([500]),
                    json.dumps([10]),
                    _APPROVAL_NOTE,
                    "steady_emission",
                    "S1c",
                    "X",
                    (
                        None
                        if book_key in rejected
                        else json.dumps(10)
                    ),
                    (
                        None
                        if book_key in rejected
                        else json.dumps(500)
                    ),
                    (
                        None
                        if book_key in rejected
                        else json.dumps(10)
                    ),
                    (
                        None
                        if book_key in rejected
                        else json.dumps(500)
                    ),
                )
                for index, book_key in enumerate(book_keys)
            ),
        )
        connection.commit()
    finally:
        connection.close()
    return path


def _valid_approved_payload():
    from spectrum_organizer.core.output_model import (
        OutputSpectrum,
        build_output_plan,
    )
    from spectrum_organizer.domain.models import SpectrumClass
    from spectrum_organizer.safety.fingerprints import SourceSnapshot
    from spectrum_organizer.store.run_snapshot import (
        snapshot_approval_sha256,
    )

    spectrum = OutputSpectrum(
        BOOK_A,
        SpectrumClass.STEADY_EMISSION,
        "A-298 K",
        "A",
        "298 K",
        "270",
        (("500", "10"),),
        excitation_slit=("2", "2"),
        emission_slit=("2", "2"),
        scan_start="350",
        scan_stop="650",
        scan_step="1",
        sample_system_identity='{"sample":"A"}',
    )
    source = _approval_source_snapshot()
    snapshot_path = _approval_snapshot(BOOK_A)
    return {
        "task_snapshot_sha256": snapshot_approval_sha256(snapshot_path),
        "recognized_book_keys": (BOOK_A,),
        "accepted_spectra": (spectrum,),
        "rejections": (),
        "exclusions": (),
        "attributions": (
            _approved_attribution(
                BOOK_A,
                "A-298 K",
                "A",
                "298 K",
                sample_system_identity='{"sample":"A"}',
            ),
        ),
        "review_requirements": (),
        "review_choices": (),
        "output_plan": build_output_plan((spectrum,)),
        "source_fingerprints_before": (source,),
        "source_fingerprints_after": (source,),
        "count_reconciliation": CountReconciliation(
            1,
            0,
            0,
            1,
            1,
            3,
        ),
        "recognized_books": (_approved_book_identity(BOOK_A),),
        "source_ids": ("S0001",),
        "task_snapshot_path": snapshot_path,
        "task_temp_root_identity": (101, 202),
    }


def _exact_excitation_exclusion_payload(
    *,
    detail="精确重复激发谱审核未选择",
    reason_code="exact_excitation_duplicate_unselected",
):
    from spectrum_organizer.core.output_model import (
        OutputSpectrum,
        build_output_plan,
    )
    from spectrum_organizer.domain.models import SpectrumClass
    from spectrum_organizer.store.run_snapshot import (
        snapshot_approval_sha256,
    )

    note = (
        "[EXP_FD_FILE]\n"
        "Acquisition Type = Spectral Acquisition[Excitation]\n"
        "[EX1]\n"
        "Start = 300\n"
        "End = 600\n"
        "Increment = 1\n"
        "Front Entrance Slit = 2\n"
        "Front Exit Slit = 2\n"
        "[EM1]\n"
        "Park = 500\n"
        "Front Entrance Slit = 2\n"
        "Front Exit Slit = 2\n"
    )
    snapshot_path = _approval_snapshot(BOOK_A, BOOK_B)
    connection = sqlite3.connect(snapshot_path)
    try:
        connection.execute(
            "update book_results set note_text = ?, "
            "spectrum_class = 'steady_excitation'",
            (note,),
        )
        connection.commit()
    finally:
        connection.close()
    spectrum = OutputSpectrum(
        BOOK_A,
        SpectrumClass.STEADY_EXCITATION,
        "A-298 K",
        "A",
        "298 K",
        "500",
        (("500", "10"),),
        excitation_slit=("2", "2"),
        emission_slit=("2", "2"),
        scan_start="300",
        scan_stop="600",
        scan_step="1",
        sample_system_identity='{"sample":"A"}',
        selection_order=1,
    )
    raw_review_key = json.dumps(
        ["excitation_selection", None, [BOOK_A, BOOK_B]],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    review_key = repr(raw_review_key)
    base = _valid_approved_payload()
    return {
        **base,
        "task_snapshot_sha256": snapshot_approval_sha256(snapshot_path),
        "task_snapshot_path": snapshot_path,
        "recognized_book_keys": (BOOK_A, BOOK_B),
        "accepted_spectra": (spectrum,),
        "exclusions": (
            _approved_audit_item(
                BOOK_B,
                detail,
                reason_code=reason_code,
                decision_source="manual",
                evidence=(
                    ("review_kind", "excitation"),
                    ("review_key", review_key),
                ),
            ),
        ),
        "attributions": tuple(
            _approved_attribution(
                book_key,
                "A-298 K",
                "A",
                "298 K",
                sample_system_identity='{"sample":"A"}',
            )
            for book_key in (BOOK_A, BOOK_B)
        ),
        "review_requirements": (
            ApprovedReviewRequirement(
                "excitation",
                review_key,
                (BOOK_A, BOOK_B),
            ),
        ),
        "review_choices": (
            ApprovedReviewChoice(
                "excitation",
                review_key,
                (BOOK_A,),
                (BOOK_A, BOOK_B),
            ),
        ),
        "output_plan": build_output_plan((spectrum,)),
        "count_reconciliation": CountReconciliation(
            2,
            0,
            1,
            1,
            1,
            3,
        ),
        "recognized_books": tuple(
            replace(
                _approved_book_identity(book_key),
                spectrum_class="steady_excitation",
            )
            for book_key in (BOOK_A, BOOK_B)
        ),
    }


class ApprovedOutputSnapshotTests(unittest.TestCase):
    def test_approval_requires_every_selected_source_to_have_one_disposition(self):
        from spectrum_organizer.safety.fingerprints import snapshot_sources

        unsupported_path = (
            pathlib.Path(_APPROVAL_TEMP_ROOT.name) / "unsupported.opju"
        )
        unsupported_path.write_bytes(b"unsupported immutable source")
        issue = SourceInputIssue(
            "S0002",
            str(unsupported_path),
            "未检测到受支持的 Origin 原始谱图",
            "请重新选择包含原始光谱 Book 的 Origin 项目文件。",
        )
        payload = _valid_approved_payload()
        selected_sources = (
            *payload["source_fingerprints_before"],
            snapshot_sources([unsupported_path], [])[0],
        )

        with self.assertRaisesRegex(
            ProductRunnerError,
            "selected source|处理结论|disposition",
        ):
            approve_output_plan(
                **payload,
                selected_source_fingerprints_before=selected_sources,
            )
        with_issue = approve_output_plan(
            **payload,
            source_input_issues=(issue,),
            selected_source_fingerprints_before=selected_sources,
        )

        self.assertEqual((issue,), with_issue.source_input_issues)

    def test_approval_binds_each_selected_source_id_to_its_selected_path(self):
        from spectrum_organizer.safety.fingerprints import snapshot_sources

        unsupported_path = (
            pathlib.Path(_APPROVAL_TEMP_ROOT.name) / "unsupported-id.opju"
        )
        unsupported_path.write_bytes(b"unsupported immutable source")
        payload = _valid_approved_payload()
        selected_sources = (
            *payload["source_fingerprints_before"],
            snapshot_sources([unsupported_path], [])[0],
        )
        wrong_id_issue = SourceInputIssue(
            "S9999",
            str(unsupported_path),
            "未检测到受支持的 Origin 原始谱图",
            "请重新选择包含原始光谱 Book 的 Origin 项目文件。",
        )

        with self.assertRaisesRegex(
            ProductRunnerError,
            "source id|源文件|处理结论|disposition",
        ):
            approve_output_plan(
                **payload,
                source_input_issues=(wrong_id_issue,),
                selected_source_fingerprints_before=selected_sources,
            )

    def test_approval_honors_cancellation_before_expensive_verification(self):
        class ApprovalCancelled(Exception):
            pass

        checks = 0

        def cancel_check():
            nonlocal checks
            checks += 1
            raise ApprovalCancelled

        with self.assertRaises(ApprovalCancelled):
            approve_output_plan(
                **_valid_approved_payload(),
                cancel_check=cancel_check,
            )
        self.assertEqual(1, checks)

    def test_approval_rejects_swapped_exact_excitation_exclusion_reason(self):
        with self.assertRaisesRegex(
            ProductRunnerError,
            "exclusion|recomputed|reason",
        ):
            approve_output_plan(
                **_exact_excitation_exclusion_payload(
                    reason_code="excitation_candidate_unselected",
                )
            )

    def test_approval_rejects_forged_human_detail_for_bound_rejection(self):
        from spectrum_organizer.store.run_snapshot import (
            snapshot_approval_sha256,
        )

        base = _valid_approved_payload()
        snapshot_path = _approval_snapshot(
            BOOK_A,
            BAD,
            rejected_book_keys=(BAD,),
        )

        with self.assertRaisesRegex(ProductRunnerError, "audit.*detail"):
            approve_output_plan(
                **{
                    **base,
                    "task_snapshot_sha256": snapshot_approval_sha256(
                        snapshot_path
                    ),
                    "task_snapshot_path": snapshot_path,
                    "recognized_book_keys": (BOOK_A, BAD),
                    "rejections": (
                        _approved_audit_item(
                            BAD,
                            "FORGED HUMAN DETAIL",
                            reason_code="invalid data",
                        ),
                    ),
                    "count_reconciliation": CountReconciliation(
                        2,
                        1,
                        0,
                        1,
                        1,
                        3,
                    ),
                    "recognized_books": (
                        _approved_book_identity(BOOK_A),
                        _approved_book_identity(BAD),
                    ),
                }
            )

    def test_approval_rejects_forged_human_detail_for_bound_exclusion(self):
        with self.assertRaisesRegex(ProductRunnerError, "audit.*detail"):
            approve_output_plan(
                **_exact_excitation_exclusion_payload(
                    detail="FORGED HUMAN DETAIL",
                )
            )

    def test_approval_accepts_canonical_tied_s1_rejection_evidence(self):
        from spectrum_organizer.store.run_snapshot import (
            snapshot_approval_sha256,
        )

        base = _valid_approved_payload()
        snapshot_path = _approval_snapshot(
            BOOK_A,
            BAD,
            rejected_book_keys=(BAD,),
        )
        connection = sqlite3.connect(snapshot_path)
        try:
            connection.execute(
                "update book_results set rejection_reason = ?, "
                "s1_max_for_limit_json = ?, "
                "s1_max_for_limit_x_json = ? "
                "where short_name = 'bad'",
                (
                    "S1 max exceeds limit",
                    json.dumps(101),
                    json.dumps([300, 302]),
                ),
            )
            connection.commit()
        finally:
            connection.close()

        approved = approve_output_plan(
            **{
                **base,
                "task_snapshot_sha256": snapshot_approval_sha256(
                    snapshot_path
                ),
                "task_snapshot_path": snapshot_path,
                "recognized_book_keys": (BOOK_A, BAD),
                "rejections": (
                    _approved_audit_item(
                        BAD,
                        "S1 最大值超过设定上限"
                        "（最大值：101；对应 X：(300, 302)）",
                        reason_code="S1 max exceeds limit",
                        evidence=(
                            ("s1_max", "101"),
                            ("x_at_s1_max", "(300, 302)"),
                        ),
                    ),
                ),
                "count_reconciliation": CountReconciliation(
                    2,
                    1,
                    0,
                    1,
                    1,
                    3,
                ),
                "recognized_books": (
                    _approved_book_identity(BOOK_A),
                    _approved_book_identity(BAD),
                ),
            }
        )

        self.assertEqual(
            "(300, 302)",
            dict(approved.rejections[0].evidence)["x_at_s1_max"],
        )

    def test_approval_accepts_disambiguated_labels_for_duplicate_source_basenames(self):
        from spectrum_organizer.core.output_model import (
            OutputSpectrum,
            build_output_plan,
        )
        from spectrum_organizer.domain.models import SpectrumClass
        from spectrum_organizer.safety.fingerprints import (
            snapshot_sources,
        )
        from spectrum_organizer.store.run_snapshot import (
            snapshot_approval_sha256,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            first = root / "first" / "same.opju"
            second = root / "second" / "same.opju"
            first.parent.mkdir()
            second.parent.mkdir()
            first.write_bytes(b"first immutable source")
            second.write_bytes(b"second immutable source")
            source_snapshots = tuple(
                snapshot_sources([first, second], [])
            )
            second_book = _book_key(
                "book-b",
                source_id="S0002",
            )
            snapshot_path = _approval_snapshot(
                BOOK_A,
                second_book,
            )
            connection = sqlite3.connect(snapshot_path)
            try:
                connection.execute("delete from source_files")
                connection.executemany(
                    "insert into source_files values (?, ?, ?, ?, ?, ?)",
                    (
                        (
                            source_id,
                            str(
                                root
                                / "owned-copy"
                                / source_id
                                / "same.opju"
                            ),
                            snapshot.sha256,
                            os.path.normcase(
                                str(snapshot.path.resolve())
                            ),
                            snapshot.size_bytes,
                            snapshot.mtime_ns,
                        )
                        for source_id, snapshot in zip(
                            ("S0001", "S0002"),
                            source_snapshots,
                            strict=True,
                        )
                    ),
                )
                connection.commit()
            finally:
                connection.close()
            labels = tuple(
                str(path).replace("\\", "/")
                for path in (first, second)
            )
            spectra = tuple(
                OutputSpectrum(
                    book_key,
                    SpectrumClass.STEADY_EMISSION,
                    f"{sample}-298 K",
                    sample,
                    "298 K",
                    "270",
                    (("500", "10"),),
                    excitation_slit=("2", "2"),
                    emission_slit=("2", "2"),
                    scan_start="350",
                    scan_stop="650",
                    scan_step="1",
                    sample_system_identity=json.dumps(
                        {"sample": sample},
                        separators=(",", ":"),
                    ),
                    selection_order=index,
                )
                for index, (book_key, sample) in enumerate(
                    (
                        (BOOK_A, "A"),
                        (second_book, "B"),
                    ),
                    start=1,
                )
            )
            plan = build_output_plan(spectra)

            approved = approve_output_plan(
                task_snapshot_sha256=snapshot_approval_sha256(
                    snapshot_path
                ),
                recognized_book_keys=(BOOK_A, second_book),
                accepted_spectra=spectra,
                rejections=(),
                exclusions=(),
                attributions=tuple(
                    replace(
                        _approved_attribution(
                            book_key,
                            f"{sample}-298 K",
                            sample,
                            "298 K",
                            sample_system_identity=json.dumps(
                                {"sample": sample},
                                separators=(",", ":"),
                            ),
                        ),
                        source_filename=label,
                    )
                    for book_key, sample, label in zip(
                        (BOOK_A, second_book),
                        ("A", "B"),
                        labels,
                        strict=True,
                    )
                ),
                review_requirements=(),
                review_choices=(),
                output_plan=plan,
                source_fingerprints_before=source_snapshots,
                source_fingerprints_after=source_snapshots,
                count_reconciliation=CountReconciliation(
                    2,
                    0,
                    0,
                    2,
                    2,
                    sum(
                        len(book.columns)
                        for folder in plan.folders
                        for book in folder.books
                    ),
                ),
                recognized_books=tuple(
                    replace(
                        _approved_book_identity(book_key),
                        source_filename=label,
                    )
                    for book_key, label in zip(
                        (BOOK_A, second_book),
                        labels,
                        strict=True,
                    )
                ),
                source_ids=("S0001", "S0002"),
                task_snapshot_path=snapshot_path,
                task_temp_root_identity=(101, 202),
            )

        self.assertEqual(
            labels,
            tuple(
                item.source_filename
                for item in approved.recognized_books
            ),
        )

    def test_approval_rejects_forged_visible_book_name(self):
        payload = _valid_approved_payload()

        with self.assertRaisesRegex(
            ProductRunnerError,
            "display|visible|name",
        ):
            approve_output_plan(
                **{
                    **payload,
                    "recognized_books": (
                        replace(
                            payload["recognized_books"][0],
                            display_name="FORGED VISIBLE NAME",
                        ),
                    ),
                    "attributions": (
                        replace(
                            payload["attributions"][0],
                            display_name="FORGED VISIBLE NAME",
                        ),
                    ),
                }
            )

    def test_approval_rejects_self_consistent_forged_source_fingerprints(self):
        from spectrum_organizer.safety.fingerprints import SourceSnapshot

        payload = _valid_approved_payload()
        real = payload["source_fingerprints_before"][0]
        forged = SourceSnapshot(
            real.path,
            "f" * 64,
            real.size_bytes + 1,
            real.mtime_ns + 1,
        )

        with self.assertRaisesRegex(
            ProductRunnerError,
            "source fingerprint|source changed",
        ):
            approve_output_plan(
                **{
                    **payload,
                    "source_fingerprints_before": (forged,),
                    "source_fingerprints_after": (forged,),
                }
            )

    def test_approval_rejects_byte_identical_unselected_original_path(self):
        from spectrum_organizer.safety.fingerprints import snapshot_sources

        payload = _valid_approved_payload()
        with tempfile.TemporaryDirectory() as directory:
            alternate = (
                pathlib.Path(directory)
                / "not-the-selected-source.opju"
            )
            alternate.write_bytes(_APPROVAL_SOURCE_PATH.read_bytes())
            substituted = snapshot_sources([alternate], [])[0]

            with self.assertRaisesRegex(
                ProductRunnerError,
                "source fingerprints.*task snapshot|original source",
            ):
                approve_output_plan(
                    **{
                        **payload,
                        "source_fingerprints_before": (substituted,),
                        "source_fingerprints_after": (substituted,),
                        "recognized_books": (
                            replace(
                                payload["recognized_books"][0],
                                source_filename=alternate.name,
                            ),
                        ),
                        "attributions": (
                            replace(
                                payload["attributions"][0],
                                source_filename=alternate.name,
                            ),
                        ),
                    }
                )

    def test_approval_rejects_non_string_audit_evidence(self):
        payload = _valid_approved_payload()
        snapshot_path = _approval_snapshot(BOOK_A, BOOK_B)
        from spectrum_organizer.store.run_snapshot import (
            snapshot_approval_sha256,
        )

        with self.assertRaisesRegex(
            ProductRunnerError,
            "audit evidence is invalid",
        ):
            approve_output_plan(
                **{
                    **payload,
                    "task_snapshot_sha256": snapshot_approval_sha256(
                        snapshot_path
                    ),
                    "task_snapshot_path": snapshot_path,
                    "recognized_book_keys": (BOOK_A, BOOK_B),
                    "rejections": (
                        _approved_audit_item(
                            BOOK_B,
                            "rejected",
                            evidence=(("mutable", ["x"]),),
                        ),
                    ),
                    "count_reconciliation": CountReconciliation(
                        2,
                        1,
                        0,
                        1,
                        1,
                        3,
                    ),
                    "recognized_books": (
                        _approved_book_identity(BOOK_A),
                        _approved_book_identity(BOOK_B),
                    ),
                }
            )

    def test_approval_rejects_fabricated_rejection_for_extracted_book(self):
        payload = _valid_approved_payload()
        snapshot_path = _approval_snapshot(BOOK_A, BOOK_B)
        from spectrum_organizer.store.run_snapshot import (
            snapshot_approval_sha256,
        )

        with self.assertRaisesRegex(
            ProductRunnerError,
            "rejection reason|extracted Book",
        ):
            approve_output_plan(
                **{
                    **payload,
                    "task_snapshot_sha256": snapshot_approval_sha256(
                        snapshot_path
                    ),
                    "task_snapshot_path": snapshot_path,
                    "recognized_book_keys": (BOOK_A, BOOK_B),
                    "rejections": (
                        _approved_audit_item(
                            BOOK_B,
                            "fabricated",
                            reason_code="fabricated_reason",
                        ),
                    ),
                    "count_reconciliation": CountReconciliation(
                        2,
                        1,
                        0,
                        1,
                        1,
                        3,
                    ),
                    "recognized_books": (
                        _approved_book_identity(BOOK_A),
                        _approved_book_identity(BOOK_B),
                    ),
                }
            )

    def test_approval_rejects_fabricated_review_not_required_by_candidate_physics(self):
        payload = _valid_approved_payload()
        snapshot_path = _approval_snapshot(BOOK_A, BOOK_B)
        from spectrum_organizer.store.run_snapshot import (
            snapshot_approval_sha256,
        )
        fabricated_key = "fabricated-group"

        with self.assertRaisesRegex(
            ProductRunnerError,
            "recomputed|required review|review ledger",
        ):
            approve_output_plan(
                **{
                    **payload,
                    "task_snapshot_sha256": snapshot_approval_sha256(
                        snapshot_path
                    ),
                    "task_snapshot_path": snapshot_path,
                    "recognized_book_keys": (BOOK_A, BOOK_B),
                    "exclusions": (
                        _approved_audit_item(
                            BOOK_B,
                            "not selected",
                            reason_code=(
                                "emission_duplicate_unselected"
                            ),
                            decision_source="manual",
                            evidence=(
                                ("review_kind", "emission"),
                                ("review_key", fabricated_key),
                            ),
                        ),
                    ),
                    "attributions": (
                        payload["attributions"][0],
                        _approved_attribution(
                            BOOK_B,
                            "B-298 K",
                            "B",
                            "298 K",
                            sample_system_identity='{"sample":"B"}',
                        ),
                    ),
                    "review_requirements": (
                        ApprovedReviewRequirement(
                            "emission",
                            fabricated_key,
                            (BOOK_A, BOOK_B),
                        ),
                    ),
                    "review_choices": (
                        ApprovedReviewChoice(
                            "emission",
                            fabricated_key,
                            (BOOK_A,),
                            (BOOK_A, BOOK_B),
                        ),
                    ),
                    "count_reconciliation": CountReconciliation(
                        2,
                        0,
                        1,
                        1,
                        1,
                        3,
                    ),
                    "recognized_books": (
                        _approved_book_identity(BOOK_A),
                        _approved_book_identity(BOOK_B),
                    ),
                }
            )

    def test_approval_freezes_mutable_sequence_inputs_before_hashing(self):
        payload = _valid_approved_payload()
        accepted = list(payload["accepted_spectra"])

        approved = approve_output_plan(
            **{
                **payload,
                "accepted_spectra": accepted,
            }
        )
        accepted.clear()

        self.assertIsInstance(approved.accepted_spectra, tuple)
        self.assertEqual(1, len(approved.accepted_spectra))
        self.assertEqual(
            approved.accepted_spectra[0].spectrum_id,
            BOOK_A,
        )

    def test_approval_freezes_report_settings_and_ignored_duplicate_paths_into_snapshot_identity(self):
        payload = _valid_approved_payload()
        settings = {
            "s1Limit": 1_000_000,
            "steadyEmissionY": "S1c",
            "allowMissingS1": False,
        }
        duplicates = [pathlib.Path("duplicate.opju")]

        approved = approve_output_plan(
            **payload,
            settings_snapshot=settings,
            ignored_duplicate_input_paths=duplicates,
        )
        settings["s1Limit"] = 99
        duplicates.clear()
        changed_settings = approve_output_plan(
            **payload,
            settings_snapshot={
                "s1Limit": 2_000_000,
                "steadyEmissionY": "S1c",
                "allowMissingS1": False,
            },
            ignored_duplicate_input_paths=(pathlib.Path("duplicate.opju"),),
        )

        self.assertEqual(1_000_000, approved.settings_snapshot["s1Limit"])
        self.assertEqual(
            (pathlib.Path("duplicate.opju"),),
            approved.ignored_duplicate_input_paths,
        )
        with self.assertRaises(TypeError):
            approved.settings_snapshot["s1Limit"] = 5
        self.assertNotEqual(approved.snapshot_id, changed_settings.snapshot_id)

    def test_approval_rejects_pre_verifier_readback_counts(self):
        payload = _valid_approved_payload()

        with self.assertRaisesRegex(
            ProductRunnerError,
            "verifier readback counts",
        ):
            approve_output_plan(
                **{
                    **payload,
                    "count_reconciliation": CountReconciliation(
                        1,
                        0,
                        0,
                        1,
                        1,
                        3,
                        "forged",
                        -9,
                    ),
                }
            )

    def test_approval_rejects_accepted_xy_forged_away_from_snapshot_payload(self):
        from spectrum_organizer.core.output_model import build_output_plan

        payload = _valid_approved_payload()
        forged = replace(
            payload["accepted_spectra"][0],
            x_y=(("999", "123456"),),
        )

        with self.assertRaisesRegex(
            ProductRunnerError,
            "spectrum payload does not match task snapshot",
        ):
            approve_output_plan(
                **{
                    **payload,
                    "accepted_spectra": (forged,),
                    "output_plan": build_output_plan((forged,)),
                }
            )

    def test_approval_rejects_accepted_metadata_forged_away_from_snapshot_note(self):
        from spectrum_organizer.core.output_model import build_output_plan

        payload = _valid_approved_payload()
        forged = replace(
            payload["accepted_spectra"][0],
            key_wavelength="999",
            excitation_slit=("88", "88"),
        )

        with self.assertRaisesRegex(
            ProductRunnerError,
            "spectrum metadata does not match task snapshot",
        ):
            approve_output_plan(
                **{
                    **payload,
                    "accepted_spectra": (forged,),
                    "output_plan": build_output_plan((forged,)),
                }
            )

    def test_approval_preserves_empty_raw_long_name_with_short_name_display_fallback(self):
        from spectrum_organizer.store.run_snapshot import (
            snapshot_approval_sha256,
        )

        payload = _valid_approved_payload()
        snapshot_path = _approval_snapshot(
            BOOK_A,
            display_names=("",),
        )
        identity = replace(
            payload["recognized_books"][0],
            raw_display_name="",
        )

        approved = approve_output_plan(
            **{
                **payload,
                "task_snapshot_path": snapshot_path,
                "task_snapshot_sha256": snapshot_approval_sha256(
                    snapshot_path
                ),
                "recognized_books": (identity,),
            }
        )

        self.assertEqual("", approved.recognized_books[0].raw_display_name)
        self.assertEqual(
            "book-a",
            approved.recognized_books[0].display_name,
        )

    def test_approval_rejects_review_selection_inverted_from_final_disposition(self):
        payload = _valid_approved_payload()
        snapshot_path = _approval_snapshot(BOOK_A, DUPLICATE)
        from spectrum_organizer.store.run_snapshot import (
            snapshot_approval_sha256,
        )

        exclusion = _approved_audit_item(
            DUPLICATE,
            "not selected",
            reason_code="emission_duplicate_unselected",
            decision_source="manual",
            evidence=(
                ("review_kind", "emission"),
                ("review_key", "duplicate-1"),
            ),
        )
        with self.assertRaisesRegex(
            ProductRunnerError,
            "no matching review choice|does not match final dispositions",
        ):
            approve_output_plan(
                **{
                    **payload,
                    "task_snapshot_sha256": snapshot_approval_sha256(
                        snapshot_path
                    ),
                    "task_snapshot_path": snapshot_path,
                    "recognized_book_keys": (BOOK_A, DUPLICATE),
                    "exclusions": (exclusion,),
                    "attributions": (
                        *payload["attributions"],
                        _approved_attribution(
                            DUPLICATE,
                            "A-298 K",
                            "A",
                            "298 K",
                            sample_system_identity='{"sample":"A"}',
                        ),
                    ),
                    "review_requirements": (
                        ApprovedReviewRequirement(
                            "emission",
                            "duplicate-1",
                            (BOOK_A, DUPLICATE),
                        ),
                    ),
                    "review_choices": (
                        ApprovedReviewChoice(
                            "emission",
                            "duplicate-1",
                            (DUPLICATE,),
                            (BOOK_A, DUPLICATE),
                        ),
                    ),
                    "count_reconciliation": CountReconciliation(
                        2,
                        0,
                        1,
                        1,
                        1,
                        3,
                    ),
                    "recognized_books": (
                        _approved_book_identity(BOOK_A),
                        _approved_book_identity(DUPLICATE),
                    ),
                }
            )

    def test_approval_rejects_omitted_select_all_excitation_review(self):
        from spectrum_organizer.core.output_model import (
            OutputSpectrum,
            build_output_plan,
        )
        from spectrum_organizer.domain.models import SpectrumClass
        from spectrum_organizer.store.run_snapshot import (
            snapshot_approval_sha256,
        )

        spectra = tuple(
            OutputSpectrum(
                book_key,
                SpectrumClass.STEADY_EXCITATION,
                "A-298 K",
                "A",
                "298 K",
                wavelength,
                (("300", "10"),),
                excitation_slit="2",
                emission_slit="2",
                sample_system_identity='{"sample":"A"}',
            )
            for book_key, wavelength in (
                (BOOK_A, "500"),
                (BOOK_B, "600"),
            )
        )
        plan = build_output_plan(spectra)
        snapshot_path = _approval_snapshot(BOOK_A, BOOK_B)
        source = _valid_approved_payload()[
            "source_fingerprints_before"
        ]

        with self.assertRaisesRegex(
            ProductRunnerError,
            "do not exactly cover required reviews",
        ):
            approve_output_plan(
                task_snapshot_sha256=snapshot_approval_sha256(
                    snapshot_path
                ),
                recognized_book_keys=(BOOK_A, BOOK_B),
                accepted_spectra=spectra,
                rejections=(),
                exclusions=(),
                attributions=tuple(
                    _approved_attribution(
                        book_key,
                        "A-298 K",
                        "A",
                        "298 K",
                        sample_system_identity='{"sample":"A"}',
                    )
                    for book_key in (BOOK_A, BOOK_B)
                ),
                review_requirements=(
                    ApprovedReviewRequirement(
                        "excitation",
                        "excitation-A",
                        (BOOK_A, BOOK_B),
                    ),
                ),
                review_choices=(),
                output_plan=plan,
                source_fingerprints_before=source,
                source_fingerprints_after=source,
                count_reconciliation=CountReconciliation(
                    2,
                    0,
                    0,
                    2,
                    2,
                    sum(
                        len(book.columns)
                        for folder in plan.folders
                        for book in folder.books
                    ),
                ),
                recognized_books=(
                    _approved_book_identity(BOOK_A),
                    _approved_book_identity(BOOK_B),
                ),
                source_ids=("S0001",),
                task_snapshot_path=snapshot_path,
            )

    def test_approval_rejects_excitation_choice_order_different_from_selection_order(self):
        from spectrum_organizer.core.output_model import (
            OutputSpectrum,
            build_output_plan,
        )
        from spectrum_organizer.domain.models import SpectrumClass
        from spectrum_organizer.store.run_snapshot import (
            snapshot_approval_sha256,
        )

        spectra = tuple(
            OutputSpectrum(
                book_key,
                SpectrumClass.STEADY_EXCITATION,
                "A-298 K",
                "A",
                "298 K",
                wavelength,
                (("500", "10"),),
                excitation_slit="2",
                emission_slit="2",
                selection_order=selection_order,
                sample_system_identity='{"sample":"A"}',
            )
            for book_key, wavelength, selection_order in (
                (BOOK_A, "500", 1),
                (BOOK_B, "600", 2),
            )
        )
        plan = build_output_plan(spectra)
        snapshot_path = _approval_snapshot(BOOK_A, BOOK_B)
        source = _valid_approved_payload()[
            "source_fingerprints_before"
        ]

        with self.assertRaisesRegex(
            ProductRunnerError,
            "excitation review order",
        ):
            approve_output_plan(
                task_snapshot_sha256=snapshot_approval_sha256(
                    snapshot_path
                ),
                recognized_book_keys=(BOOK_A, BOOK_B),
                accepted_spectra=spectra,
                rejections=(),
                exclusions=(),
                attributions=tuple(
                    _approved_attribution(
                        book_key,
                        "A-298 K",
                        "A",
                        "298 K",
                        sample_system_identity='{"sample":"A"}',
                    )
                    for book_key in (BOOK_A, BOOK_B)
                ),
                review_requirements=(
                    ApprovedReviewRequirement(
                        "excitation",
                        "excitation-A",
                        (BOOK_A, BOOK_B),
                    ),
                ),
                review_choices=(
                    ApprovedReviewChoice(
                        "excitation",
                        "excitation-A",
                        (BOOK_B, BOOK_A),
                        (BOOK_A, BOOK_B),
                    ),
                ),
                output_plan=plan,
                source_fingerprints_before=source,
                source_fingerprints_after=source,
                count_reconciliation=CountReconciliation(
                    2,
                    0,
                    0,
                    2,
                    2,
                    sum(
                        len(book.columns)
                        for folder in plan.folders
                        for book in folder.books
                    ),
                ),
                recognized_books=(
                    _approved_book_identity(BOOK_A),
                    _approved_book_identity(BOOK_B),
                ),
                source_ids=("S0001",),
                task_snapshot_path=snapshot_path,
            )

    def test_approval_rejects_forged_book_provenance_payload_and_snapshot_path(self):
        payload = _valid_approved_payload()
        identity = payload["recognized_books"][0]
        attribution = payload["attributions"][0]

        with self.assertRaisesRegex(
            ProductRunnerError,
            "identity key is invalid",
        ):
            approve_output_plan(
                **{
                    **payload,
                    "recognized_books": (
                        replace(identity, folder_path="/wrong"),
                    ),
                }
            )
        with self.assertRaisesRegex(
            ProductRunnerError,
            "payload checksum is invalid",
        ):
            approve_output_plan(
                **{
                    **payload,
                    "attributions": (
                        replace(attribution, payload_checksum="not-a-sha"),
                    ),
                }
            )
        with self.assertRaisesRegex(
            ProductRunnerError,
            "task snapshot could not be verified",
        ):
            approve_output_plan(
                **{
                    **payload,
                    "task_snapshot_path": pathlib.Path(
                        _APPROVAL_TEMP_ROOT.name
                    )
                    / "missing.sqlite3",
                }
            )

    def test_approval_rejects_task_snapshot_changed_after_extraction(self):
        payload = _valid_approved_payload()
        connection = sqlite3.connect(payload["task_snapshot_path"])
        try:
            connection.execute(
                "update book_results set display_name = 'changed'"
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaisesRegex(
            ProductRunnerError,
            "task snapshot changed before approval",
        ):
            approve_output_plan(**payload)

    def test_approval_rejects_empty_output_non_integer_counts_and_sample_identity_drift(self):
        payload = _valid_approved_payload()
        invalid_counts = (
            CountReconciliation(True, 0, 0, 1, 1, 3),
            CountReconciliation(1.0, 0, 0, 1, 1, 3),
        )
        for counts in invalid_counts:
            with self.subTest(counts=counts), self.assertRaisesRegex(
                ProductRunnerError,
                "counts must be integers",
            ):
                approve_output_plan(
                    **{
                        **payload,
                        "count_reconciliation": counts,
                    }
                )

        wrong_identity = _approved_attribution(
            BOOK_A,
            "A-298 K",
            "A",
            "298 K",
            sample_system_identity='{"sample":"different"}',
        )
        with self.assertRaisesRegex(
            ProductRunnerError,
            "attribution does not match accepted spectrum",
        ):
            approve_output_plan(
                **{
                    **payload,
                    "attributions": (wrong_identity,),
                }
            )

        with self.assertRaisesRegex(
            ProductRunnerError,
            "requires at least one output spectrum",
        ):
            approve_output_plan(
                **{
                    **payload,
                    "recognized_book_keys": (BAD,),
                    "accepted_spectra": (),
                    "rejections": (
                        _approved_audit_item(
                            BAD,
                            "invalid",
                        ),
                    ),
                    "attributions": (),
                    "output_plan": product_runner.OutputPlan((), ()),
                    "count_reconciliation": CountReconciliation(
                        1,
                        1,
                        0,
                        0,
                        0,
                        0,
                    ),
                }
            )

    def test_approval_rejects_incomplete_audit_and_missing_or_invalid_review_coverage(self):
        payload = _valid_approved_payload()
        duplicate_attribution = _approved_attribution(
            DUPLICATE,
            "A-298 K",
            "A",
            "298 K",
            sample_system_identity='{"sample":"A"}',
        )
        exclusion = _approved_audit_item(
            DUPLICATE,
            "not selected",
            reason_code="emission_duplicate_unselected",
            decision_source="manual",
        )
        duplicate_payload = {
            **payload,
            "recognized_book_keys": (BOOK_A, DUPLICATE),
            "exclusions": (exclusion,),
            "attributions": (
                *payload["attributions"],
                duplicate_attribution,
            ),
            "count_reconciliation": CountReconciliation(
                2,
                0,
                1,
                1,
                1,
                3,
            ),
        }

        with self.assertRaisesRegex(
            ProductRunnerError,
            "no matching review choice",
        ):
            approve_output_plan(**duplicate_payload)
        with self.assertRaisesRegex(
            ProductRunnerError,
            "must select exactly one candidate",
        ):
            approve_output_plan(
                **{
                    **duplicate_payload,
                    "review_choices": (
                        ApprovedReviewChoice(
                            "emission",
                            "duplicate-1",
                            (),
                            (BOOK_A, DUPLICATE),
                        ),
                    ),
                }
            )
        with self.assertRaisesRegex(
            ProductRunnerError,
            "audit item is incomplete",
        ):
            approve_output_plan(
                **{
                    **duplicate_payload,
                    "exclusions": (
                        ApprovedAuditItem(
                            DUPLICATE,
                            "",
                        ),
                    ),
                    "review_choices": (
                        ApprovedReviewChoice(
                            "emission",
                            "duplicate-1",
                            (BOOK_A,),
                            (BOOK_A, DUPLICATE),
                        ),
                    ),
                    "review_requirements": (
                        ApprovedReviewRequirement(
                            "emission",
                            "duplicate-1",
                            (BOOK_A, DUPLICATE),
                        ),
                    ),
                }
            )

    def test_approval_binds_reviewed_output_and_requires_closed_counts_and_unchanged_sources(self):
        from spectrum_organizer.core.output_model import (
            OutputSpectrum,
            build_output_plan,
        )
        from spectrum_organizer.domain.models import SpectrumClass
        from spectrum_organizer.safety.fingerprints import SourceSnapshot

        source = _APPROVAL_SOURCE_PATH
        before = (_approval_source_snapshot(),)
        spectrum = OutputSpectrum(
            spectrum_id=BOOK_A,
            spectrum_class=SpectrumClass.STEADY_EMISSION,
            canonical_sample_label="MFL-Solid-Air-298 K",
            sample_system_label="MFL-Solid-Air",
            temperature="298 K",
            key_wavelength="270",
            x_y=(("500", "10"),),
            excitation_slit=("2", "2"),
            emission_slit=("2", "2"),
            scan_start="350",
            scan_stop="650",
            scan_step="1",
        )
        plan = build_output_plan((spectrum,))
        snapshot_path = _approval_snapshot(
            BOOK_A,
            BAD,
            DUPLICATE,
            rejected_book_keys=(BAD,),
        )
        from spectrum_organizer.store.run_snapshot import (
            snapshot_approval_sha256,
        )

        reconciliation = CountReconciliation(
            recognizable_book_count=3,
            rejected_book_count=1,
            excluded_book_count=1,
            accepted_ordinary_spectrum_count=1,
            output_plan_spectrum_count=1,
            output_plan_column_count=3,
        )
        duplicate_review_key = repr(
            json.dumps(
                [
                    "emission_duplicate",
                    "stage1",
                    [BOOK_A, DUPLICATE],
                ],
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )

        approved = approve_output_plan(
            task_snapshot_sha256=snapshot_approval_sha256(snapshot_path),
            recognized_book_keys=(BOOK_A, BAD, DUPLICATE),
            accepted_spectra=(spectrum,),
            rejections=(
                _approved_audit_item(
                    BAD,
                    "数据无效",
                    reason_code="invalid data",
                ),
            ),
            exclusions=(
                _approved_audit_item(
                    DUPLICATE,
                    "重复发射谱审核未选择",
                    reason_code="emission_duplicate_unselected",
                    decision_source="manual",
                    evidence=(
                        ("review_kind", "emission"),
                        ("review_key", duplicate_review_key),
                    ),
                ),
            ),
            attributions=(
                _approved_attribution(
                    BOOK_A,
                    "MFL-Solid-Air-298 K",
                    "MFL-Solid-Air",
                    "298 K",
                ),
                _approved_attribution(
                    DUPLICATE,
                    "MFL-Solid-Air-298 K",
                    "MFL-Solid-Air",
                    "298 K",
                ),
            ),
            review_choices=(
                ApprovedReviewChoice(
                    "emission",
                    duplicate_review_key,
                    (BOOK_A,),
                    (BOOK_A, DUPLICATE),
                ),
            ),
            review_requirements=(
                ApprovedReviewRequirement(
                    "emission",
                    duplicate_review_key,
                    (BOOK_A, DUPLICATE),
                ),
            ),
            output_plan=plan,
            source_fingerprints_before=before,
            source_fingerprints_after=before,
            count_reconciliation=reconciliation,
            recognized_books=tuple(
                _approved_book_identity(book_key)
                for book_key in (BOOK_A, BAD, DUPLICATE)
            ),
            source_ids=("S0001",),
            task_snapshot_path=snapshot_path,
            task_temp_root_identity=(101, 202),
        )

        self.assertEqual(64, len(approved.snapshot_id))
        self.assertEqual(plan, approved.output_plan)
        self.assertTrue(approved.count_reconciliation.is_closed)
        changed = (SourceSnapshot(source, "c" * 64, 123, 456),)
        with self.assertRaisesRegex(
            ProductRunnerError,
            "source fingerprints changed before approved snapshot",
        ):
            approve_output_plan(
                task_snapshot_sha256="b" * 64,
                recognized_book_keys=(BOOK_A, BAD, DUPLICATE),
                accepted_spectra=(spectrum,),
                rejections=(ApprovedAuditItem(BAD, "invalid Note"),),
                exclusions=(ApprovedAuditItem(DUPLICATE, "not selected"),),
                attributions=(),
                review_choices=(),
                output_plan=plan,
                source_fingerprints_before=before,
                source_fingerprints_after=changed,
                count_reconciliation=reconciliation,
            )

    def test_approval_rejects_output_plan_built_from_different_spectra(self):
        from spectrum_organizer.core.output_model import (
            OutputSpectrum,
            build_output_plan,
        )
        from spectrum_organizer.domain.models import SpectrumClass

        accepted = OutputSpectrum(
            BOOK_A,
            SpectrumClass.STEADY_EMISSION,
            "A-298 K",
            "A",
            "298 K",
            "270",
            (("500", "10"),),
            excitation_slit=("2", "2"),
            emission_slit=("2", "2"),
            scan_start="350",
            scan_stop="650",
            scan_step="1",
        )
        different = OutputSpectrum(
            BOOK_B,
            SpectrumClass.STEADY_EMISSION,
            "B-298 K",
            "B",
            "298 K",
            "270",
            (("500", "20"),),
            excitation_slit="2",
            emission_slit="2",
        )
        reconciliation = CountReconciliation(1, 0, 0, 1, 1, 3)

        with self.assertRaisesRegex(
            ProductRunnerError,
            "OutputPlan does not match accepted spectra",
        ):
            approve_output_plan(
                task_snapshot_sha256="b" * 64,
                recognized_book_keys=(BOOK_A,),
                accepted_spectra=(accepted,),
                rejections=(),
                exclusions=(),
                attributions=(),
                review_choices=(),
                output_plan=build_output_plan((different,)),
                source_fingerprints_before=(),
                source_fingerprints_after=(),
                count_reconciliation=reconciliation,
            )

    def test_approval_rejects_audit_items_that_do_not_match_counts(self):
        from spectrum_organizer.core.output_model import (
            OutputSpectrum,
            build_output_plan,
        )
        from spectrum_organizer.domain.models import SpectrumClass

        spectrum = OutputSpectrum(
            BOOK_A,
            SpectrumClass.STEADY_EMISSION,
            "A-298 K",
            "A",
            "298 K",
            "270",
            (("500", "10"),),
            excitation_slit="2",
            emission_slit="2",
        )

        with self.assertRaisesRegex(
            ProductRunnerError,
            "rejection audit count does not reconcile",
        ):
            approve_output_plan(
                task_snapshot_sha256="b" * 64,
                recognized_book_keys=(BOOK_A, BAD),
                accepted_spectra=(spectrum,),
                rejections=(),
                exclusions=(),
                attributions=(),
                review_choices=(),
                output_plan=build_output_plan((spectrum,)),
                source_fingerprints_before=(),
                source_fingerprints_after=(),
                count_reconciliation=CountReconciliation(
                    2,
                    1,
                    0,
                    1,
                    1,
                    3,
                ),
            )

    def test_approval_rejects_book_keys_shared_across_dispositions(self):
        from spectrum_organizer.core.output_model import (
            OutputSpectrum,
            build_output_plan,
        )
        from spectrum_organizer.domain.models import SpectrumClass

        spectrum = OutputSpectrum(
            BOOK_A,
            SpectrumClass.STEADY_EMISSION,
            "A-298 K",
            "A",
            "298 K",
            "270",
            (("500", "10"),),
            excitation_slit="2",
            emission_slit="2",
        )

        with self.assertRaisesRegex(
            ProductRunnerError,
            "approved book dispositions overlap",
        ):
            approve_output_plan(
                task_snapshot_sha256="b" * 64,
                recognized_book_keys=(BOOK_A, OTHER_1, OTHER_2),
                accepted_spectra=(spectrum,),
                rejections=(ApprovedAuditItem(BOOK_A, "rejected"),),
                exclusions=(ApprovedAuditItem(BOOK_A, "excluded"),),
                attributions=(),
                review_choices=(),
                output_plan=build_output_plan((spectrum,)),
                source_fingerprints_before=(),
                source_fingerprints_after=(),
                count_reconciliation=CountReconciliation(
                    3,
                    1,
                    1,
                    1,
                    1,
                    3,
                ),
            )

    def test_approval_rejects_duplicate_book_keys_inside_one_audit(self):
        from spectrum_organizer.core.output_model import (
            OutputSpectrum,
            build_output_plan,
        )
        from spectrum_organizer.domain.models import SpectrumClass

        spectrum = OutputSpectrum(
            BOOK_A,
            SpectrumClass.STEADY_EMISSION,
            "A-298 K",
            "A",
            "298 K",
            "270",
            (("500", "10"),),
            excitation_slit="2",
            emission_slit="2",
        )

        with self.assertRaisesRegex(
            ProductRunnerError,
            "rejection audit book keys must be unique",
        ):
            approve_output_plan(
                task_snapshot_sha256="b" * 64,
                recognized_book_keys=(BOOK_A, BAD, OTHER),
                accepted_spectra=(spectrum,),
                rejections=(
                    ApprovedAuditItem(BAD, "first"),
                    ApprovedAuditItem(BAD, "second"),
                ),
                exclusions=(),
                attributions=(),
                review_choices=(),
                output_plan=build_output_plan((spectrum,)),
                source_fingerprints_before=(),
                source_fingerprints_after=(),
                count_reconciliation=CountReconciliation(
                    3,
                    2,
                    0,
                    1,
                    1,
                    3,
                ),
            )

    def test_approval_rejects_dispositions_that_replace_a_recognized_book(self):
        from spectrum_organizer.core.output_model import (
            OutputSpectrum,
            build_output_plan,
        )
        from spectrum_organizer.domain.models import SpectrumClass

        spectrum = OutputSpectrum(
            BOOK_A,
            SpectrumClass.STEADY_EMISSION,
            "A-298 K",
            "A",
            "298 K",
            "270",
            (("500", "10"),),
            excitation_slit="2",
            emission_slit="2",
        )

        with self.assertRaisesRegex(
            ProductRunnerError,
            "approved dispositions do not exactly cover recognized books",
        ):
            approve_output_plan(
                task_snapshot_sha256="b" * 64,
                recognized_book_keys=(BOOK_A, BOOK_REAL_REJECTED),
                accepted_spectra=(spectrum,),
                rejections=(
                    ApprovedAuditItem(BOOK_INVENTED, "rejected"),
                ),
                exclusions=(),
                attributions=(),
                review_choices=(),
                output_plan=build_output_plan((spectrum,)),
                source_fingerprints_before=(),
                source_fingerprints_after=(),
                count_reconciliation=CountReconciliation(
                    2,
                    1,
                    0,
                    1,
                    1,
                    3,
                ),
            )

    def test_approval_rejects_recognized_book_ledger_count_mismatch(self):
        from spectrum_organizer.core.output_model import (
            OutputSpectrum,
            build_output_plan,
        )
        from spectrum_organizer.domain.models import SpectrumClass

        spectrum = OutputSpectrum(
            BOOK_A,
            SpectrumClass.STEADY_EMISSION,
            "A-298 K",
            "A",
            "298 K",
            "270",
            (("500", "10"),),
            excitation_slit="2",
            emission_slit="2",
        )
        cases = (
            ("missing", (BOOK_A, BOOK_MISSING), (), 1),
            (
                "extra",
                (BOOK_A,),
                (ApprovedAuditItem(BOOK_EXTRA, "rejected"),),
                2,
            ),
        )

        for label, recognized, rejections, recognizable_count in cases:
            with self.subTest(label=label), self.assertRaisesRegex(
                ProductRunnerError,
                "recognized book key count does not reconcile",
            ):
                approve_output_plan(
                    task_snapshot_sha256="b" * 64,
                    recognized_book_keys=recognized,
                    accepted_spectra=(spectrum,),
                    rejections=rejections,
                    exclusions=(),
                    attributions=(),
                    review_choices=(),
                    output_plan=build_output_plan((spectrum,)),
                    source_fingerprints_before=(),
                    source_fingerprints_after=(),
                    count_reconciliation=CountReconciliation(
                        recognizable_count,
                        len(rejections),
                        0,
                        1,
                        1,
                        3,
                    ),
                )

    def test_approval_rejects_unbound_attribution_review_and_source_ledgers(self):
        from spectrum_organizer.core.output_model import (
            OutputSpectrum,
            build_output_plan,
        )
        from spectrum_organizer.domain.models import SpectrumClass
        from spectrum_organizer.safety.fingerprints import SourceSnapshot

        spectrum = OutputSpectrum(
            BOOK_A,
            SpectrumClass.STEADY_EMISSION,
            "A-298 K",
            "A",
            "298 K",
            "270",
            (("500", "10"),),
            excitation_slit="2",
            emission_slit="2",
        )
        attribution = _approved_attribution(
            BOOK_A,
            "A-298 K",
            "A",
            "298 K",
        )
        source = SourceSnapshot(
            pathlib.Path("C:/raw/a.opju"),
            "a" * 64,
            123,
            456,
            canonical_path=pathlib.Path("C:/raw/a.opju"),
            device_id=1,
            file_id=1,
        )
        snapshot_path = _approval_snapshot(BOOK_A)
        from spectrum_organizer.store.run_snapshot import (
            snapshot_approval_sha256,
        )

        valid = {
            "task_snapshot_sha256": snapshot_approval_sha256(
                snapshot_path
            ),
            "recognized_book_keys": (BOOK_A,),
            "accepted_spectra": (spectrum,),
            "rejections": (),
            "exclusions": (),
            "attributions": (attribution,),
            "review_choices": (),
            "output_plan": build_output_plan((spectrum,)),
            "source_fingerprints_before": (source,),
            "source_fingerprints_after": (source,),
            "count_reconciliation": CountReconciliation(1, 0, 0, 1, 1, 3),
            "recognized_books": (_approved_book_identity(BOOK_A),),
            "source_ids": ("S0001",),
            "task_snapshot_path": snapshot_path,
        }
        ghost = _approved_attribution(
            GHOST,
            "G-298 K",
            "G",
            "298 K",
        )
        wrong = _approved_attribution(
            BOOK_A,
            "Wrong",
            "A",
            "298 K",
        )
        cases = (
            (
                "missing attribution",
                {"attributions": ()},
                "attributions do not cover approved candidate books",
            ),
            (
                "ghost attribution",
                {"attributions": (attribution, ghost)},
                "attribution books are not recognized",
            ),
            (
                "duplicate attribution",
                {"attributions": (attribution, attribution)},
                "attribution book keys must be unique",
            ),
            (
                "mismatched attribution",
                {"attributions": (wrong,)},
                "attribution does not match accepted spectrum",
            ),
            (
                "ghost review candidate",
                {
                    "review_choices": (
                        ApprovedReviewChoice(
                            "emission",
                            "duplicate-1",
                            (BOOK_A,),
                            (BOOK_A, GHOST),
                        ),
                    )
                },
                "review candidate books are not attributed",
            ),
            (
                "selected outside candidates",
                {
                    "review_choices": (
                        ApprovedReviewChoice(
                            "emission",
                            "duplicate-1",
                            (GHOST,),
                            (BOOK_A,),
                        ),
                    )
                },
                "review selected books are not candidates",
            ),
            (
                "duplicate review",
                {
                    "review_choices": (
                        ApprovedReviewChoice(
                            "emission",
                            "duplicate-1",
                            (BOOK_A,),
                            (BOOK_A,),
                        ),
                        ApprovedReviewChoice(
                            "emission",
                            "duplicate-1",
                            (BOOK_A,),
                            (BOOK_A,),
                        ),
                    )
                },
                "review choices must be unique",
            ),
            (
                "missing source fingerprints",
                {
                    "source_fingerprints_before": (),
                    "source_fingerprints_after": (),
                },
                "source fingerprints are required",
            ),
            (
                "duplicate source fingerprints",
                {
                    "source_fingerprints_before": (source, source),
                    "source_fingerprints_after": (source, source),
                },
                "source fingerprint paths must be unique",
            ),
        )

        for label, changes, error in cases:
            payload = dict(valid)
            payload.update(changes)
            with self.subTest(label=label), self.assertRaisesRegex(
                ProductRunnerError,
                error,
            ):
                approve_output_plan(**payload)



class ProductRunnerReadinessTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows junction behavior")
    def test_active_cleanup_does_not_treat_dangling_junction_as_absent(self):
        import _winapi

        from spectrum_organizer import product_runner
        from spectrum_organizer.safety.owned_paths import CleanupRefusedError

        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            target = root / "junction-target"
            junction = root / "dangling-run"
            target.mkdir()
            _winapi.CreateJunction(str(target), str(junction))
            target.rmdir()
            try:
                with mock.patch.object(
                    product_runner,
                    "cleanup_owned_temp_root",
                    side_effect=CleanupRefusedError("dangling junction refused"),
                ) as cleanup:
                    result = product_runner._cleanup_temp_root_error(
                        junction,
                        expected_root_identity=(1, 2),
                    )

                self.assertEqual("dangling junction refused", result)
                cleanup.assert_called_once_with(
                    junction,
                    expected_root_identity=(1, 2),
                )
            finally:
                if os.path.lexists(junction):
                    os.rmdir(junction)

    def test_active_cleanup_does_not_treat_dangling_root_symlink_as_absent(self):
        from spectrum_organizer import product_runner
        from spectrum_organizer.safety.owned_paths import CleanupRefusedError

        target = pathlib.Path(r"C:\temp\dangling-run")
        with mock.patch.object(
            product_runner,
            "lexical_path_exists",
            return_value=True,
        ), mock.patch.object(
            product_runner,
            "cleanup_owned_temp_root",
            side_effect=CleanupRefusedError("dangling root refused"),
        ) as cleanup:
            result = product_runner._cleanup_temp_root_error(
                target,
                expected_root_identity=(1, 2),
            )

        self.assertEqual("dangling root refused", result)
        cleanup.assert_called_once_with(
            target,
            expected_root_identity=(1, 2),
        )

    def test_approved_context_and_reader_command_freeze_settings_snapshots(self):
        from spectrum_organizer.product_runner import (
            ApprovedPreExtractionRunContext,
            ReaderProcessCommand,
            VerifiedSourceCopyIdentity,
        )

        settings = {"s1Limit": 42}
        context = ApprovedPreExtractionRunContext(
            run_id="run",
            timestamp="20260730_000000",
            selected_source_paths=(),
            output_parent=pathlib.Path("D:/out"),
            settings_snapshot=settings,
            source_fingerprints_before=(),
            temp_root=pathlib.Path("C:/temp/run"),
            temp_root_identity=(1, 2),
            run_owned_source_copy_paths=(),
        )
        command = ReaderProcessCommand(
            run_id="run",
            marker_id="marker",
            settings_snapshot=settings,
            source_copy=VerifiedSourceCopyIdentity(
                "S0001",
                pathlib.Path("C:/temp/run/raw.opju"),
                "a" * 64,
                1,
                2,
                3,
            ),
            snapshot_path=pathlib.Path(
                "C:/temp/run/run_snapshot.sqlite3"
            ),
            required_temp_bytes=1,
        )
        settings["s1Limit"] = 999

        self.assertEqual(42, context.settings_snapshot["s1Limit"])
        self.assertEqual(42, command.settings_snapshot["s1Limit"])
        with self.assertRaises(TypeError):
            context.settings_snapshot["s1Limit"] = 999
        with self.assertRaises(TypeError):
            command.settings_snapshot["s1Limit"] = 999

    def test_extraction_copy_is_verified_before_worker_creation(self):
        from spectrum_organizer.origin.extract_worker import ExtractionOrchestrator, ExtractionSource

        with tempfile.TemporaryDirectory() as temp_name:
            root = pathlib.Path(temp_name)
            original = root / "raw.opju"
            owned = root / "owned"
            owned.mkdir()
            copy_path = owned / "raw.opju"
            original.write_bytes(b"raw")
            copy_path.write_bytes(b"changed")
            source = ExtractionSource(
                source_id="S0001",
                copy_path=copy_path,
                sha256="expected",
                original_path=original,
                allowed_children=(owned,),
                protected_paths=(original,),
            )
            snapshot = mock.Mock()
            worker_factory = mock.Mock()
            source_manager = mock.Mock()
            source_manager.verify_copy.side_effect = ProductRunnerError("copy mismatch")

            with self.assertRaisesRegex(ProductRunnerError, "copy mismatch"):
                ExtractionOrchestrator(
                    snapshot,
                    worker_factory,
                    source_manager,
                    max_attempts=1,
                    runtime_space_guard=lambda operation: None,
                ).run((source,))

            worker_factory.create.assert_not_called()

    def test_hard_link_to_protected_original_is_not_an_owned_copy(self):
        from spectrum_organizer.origin.extract_worker import WorkerPreflightError, validate_worker_open_target

        with tempfile.TemporaryDirectory() as temp_name:
            root = pathlib.Path(temp_name)
            original = root / "raw.opju"
            original.write_bytes(b"raw")
            owned = root / "owned"
            owned.mkdir()
            alias = owned / "copy.opju"
            alias.hardlink_to(original)

            with self.assertRaisesRegex(WorkerPreflightError, "protected"):
                validate_worker_open_target(
                    alias,
                    {alias},
                    role="extraction",
                    protected_paths=(original,),
                    allowed_children=(owned,),
                )

    def test_runtime_space_shortage_stops_before_next_origin_book_read(self):
        from spectrum_organizer.origin.extract_worker import (
            ExtractionOrchestrator,
            ExtractionSource,
            InventoryBook,
            RuntimeSpaceError,
        )

        with tempfile.TemporaryDirectory() as temp_name:
            root = pathlib.Path(temp_name)
            original = root / "raw.opju"
            owned = root / "owned"
            owned.mkdir()
            copy_path = owned / "raw.opju"
            original.write_bytes(b"raw")
            copy_path.write_bytes(b"raw")
            source = ExtractionSource(
                source_id="S0001",
                copy_path=copy_path,
                sha256="hash",
                original_path=original,
                allowed_children=(owned,),
                protected_paths=(original,),
            )
            book = InventoryBook("S0001", "Root", "Book1", "Book1", 1, ("Note", "Data"), True, True)

            class Worker:
                result_reads = 0

                def iter_inventory(self, _copy_path, _allowlist):
                    yield book

                def iter_book_results(self):
                    self.result_reads += 1
                    yield book, object()

                def close(self):
                    pass

            worker = Worker()
            worker_factory = mock.Mock()
            worker_factory.create.return_value = worker
            source_manager = mock.Mock()
            snapshot = mock.Mock(path=root / "run.sqlite3")

            def runtime_space_guard(operation):
                if operation == "result_read":
                    raise RuntimeSpaceError("runtime space exhausted")

            with self.assertRaisesRegex(RuntimeSpaceError, "runtime space exhausted"):
                ExtractionOrchestrator(
                    snapshot,
                    worker_factory,
                    source_manager,
                    max_attempts=1,
                    runtime_space_guard=runtime_space_guard,
                ).run((source,))

            self.assertEqual(0, worker.result_reads)

    def test_runtime_space_guard_uses_remaining_approved_requirement(self):
        from spectrum_organizer.origin.extract_worker import RuntimeSpaceError, build_runtime_space_guard

        with tempfile.TemporaryDirectory() as temp_name:
            root = pathlib.Path(temp_name)
            (root / "materialized.bin").write_bytes(b"x" * 400)
            free_bytes = [599]
            guard = build_runtime_space_guard(
                root,
                required_total_bytes=1000,
                free_bytes_provider=lambda _path: free_bytes[0],
            )

            with self.assertRaisesRegex(RuntimeSpaceError, "remaining 600"):
                guard("snapshot_write")

            free_bytes[0] = 600
            guard("snapshot_write")

    def test_reader_phase_checks_approved_remaining_space_before_worker_factory(self):
        from spectrum_organizer.origin.extract_worker import RuntimeSpaceError
        from spectrum_organizer.safety.fingerprints import file_identity, hash_file
        from spectrum_organizer.safety.owned_paths import add_allowed_child, create_run_ownership

        with tempfile.TemporaryDirectory() as temp_name:
            root = pathlib.Path(temp_name)
            ownership = create_run_ownership(root, "run-1", "marker-1", [])
            source_dir = ownership.temp_root / "source-0001"
            ownership = add_allowed_child(ownership, source_dir)
            source_dir.mkdir()
            copy_path = source_dir / "raw.opju"
            copy_path.write_bytes(b"raw-copy")
            snapshot_path = ownership.temp_root / "run_snapshot.sqlite3"
            add_allowed_child(ownership, snapshot_path)
            command = product_runner.ReaderProcessCommand(
                run_id="run-1",
                marker_id="marker-1",
                settings_snapshot={"s1Limit": 1_000_000, "steadyEmissionY": "S1c"},
                source_copy=product_runner.VerifiedSourceCopyIdentity(
                    source_id="S0001",
                    copy_path=copy_path,
                    sha256=hash_file(copy_path),
                    size_bytes=copy_path.stat().st_size,
                    device_id=file_identity(copy_path)[0],
                    file_id=file_identity(copy_path)[1],
                ),
                snapshot_path=snapshot_path,
                required_temp_bytes=1024**3,
            )
            worker_factory_builder = mock.Mock()

            with self.assertRaisesRegex(RuntimeSpaceError, "required remaining"):
                product_runner.run_reader_source_extraction_phase(
                    command,
                    worker_factory_builder=worker_factory_builder,
                    free_bytes_provider=lambda _path: 0,
                )

            worker_factory_builder.assert_not_called()

    def test_reader_phase_returns_counts_after_successful_extraction(self):
        from spectrum_organizer.origin.extract_worker import InventoryBook, TerminalBookResult
        from spectrum_organizer.safety.fingerprints import file_identity, hash_file
        from spectrum_organizer.safety.owned_paths import add_allowed_child, create_run_ownership

        with tempfile.TemporaryDirectory() as temp_name:
            root = pathlib.Path(temp_name)
            ownership = create_run_ownership(root, "run-1", "marker-1", [])
            source_dir = ownership.temp_root / "source-0001"
            ownership = add_allowed_child(ownership, source_dir)
            source_dir.mkdir()
            copy_path = source_dir / "raw.opju"
            copy_path.write_bytes(b"raw-copy")
            snapshot_path = ownership.temp_root / "run_snapshot.sqlite3"
            add_allowed_child(ownership, snapshot_path)
            command = product_runner.ReaderProcessCommand(
                run_id="run-1",
                marker_id="marker-1",
                settings_snapshot={"s1Limit": 1_000_000, "steadyEmissionY": "S1c"},
                source_copy=product_runner.VerifiedSourceCopyIdentity(
                    source_id="S0001",
                    copy_path=copy_path,
                    sha256=hash_file(copy_path),
                    size_bytes=copy_path.stat().st_size,
                    device_id=file_identity(copy_path)[0],
                    file_id=file_identity(copy_path)[1],
                ),
                snapshot_path=snapshot_path,
                required_temp_bytes=1,
            )
            book = InventoryBook(
                "S0001", "Root", "Book1", "Display", 1, ("Note", "Data"), True, True
            )
            result = TerminalBookResult(
                source_id="S0001",
                folder_path="Root",
                short_name="Book1",
                display_name="Display",
                page_order=1,
                spectrum_class="steady_emission",
                status="rejected",
                note_text="[EXP_FD_FILE]\nAcquisition Type = Spectral Acquisition[Emission]",
                rejection_reason="Data read failed: simulated",
            )

            class Worker:
                def iter_inventory(self, _copy_path, _allowlist):
                    yield book

                def iter_book_results(self):
                    yield book, result

                def close(self):
                    pass

            class Factory:
                def create(self, _source_id, _attempt):
                    return Worker()

            summary = product_runner.run_reader_source_extraction_phase(
                command,
                worker_factory_builder=lambda **_kwargs: Factory(),
                free_bytes_provider=lambda _path: 1024**3,
            )

            self.assertEqual((1, 1, 0, 1), (
                summary.inventory_count,
                summary.result_count,
                summary.extracted_count,
                summary.rejected_count,
            ))

    def test_context_creation_failure_after_temp_root_creation_cleans_owned_root(self):
        with tempfile.TemporaryDirectory() as temp_name:
            root = pathlib.Path(temp_name)
            original = root / "raw.opju"
            original.write_bytes(b"origin")
            temp_root = root / "localapp" / "Spectrum Organizer" / "temp" / "failed-run"

            with self.assertRaisesRegex(RuntimeError, "copy failed"):
                product_runner.prepare_approved_pre_extraction_context(
                    selected_source_paths=(original,),
                    output_parent=root / "out",
                    settings_snapshot={"s1Limit": 1_000_000, "steadyEmissionY": "S1c"},
                    local_appdata=root / "localapp",
                    dialog_port=None,
                    origin_process_probe=lambda *, timeout=5.0: (),
                    process_controller=None,
                    copy_file=lambda source, target: (_ for _ in ()).throw(OSError("copy failed")),
                    run_id_factory=lambda: "failed-run",
                    marker_id_factory=lambda: "failed-marker",
                    run_origin_process_preflight=False,
                )

            self.assertFalse(temp_root.exists())

    def test_context_creation_interrupt_after_copy_cleans_owned_root(self):
        with tempfile.TemporaryDirectory() as temp_name:
            root = pathlib.Path(temp_name)
            original = root / "raw.opju"
            original.write_bytes(b"origin")
            temp_root = root / "localapp" / "Spectrum Organizer" / "temp" / "interrupted-run"

            def interrupt_after_copy(source, target):
                target.write_bytes(source.read_bytes())
                raise KeyboardInterrupt

            with self.assertRaises(KeyboardInterrupt) as raised:
                product_runner.prepare_approved_pre_extraction_context(
                    selected_source_paths=(original,),
                    output_parent=root / "out",
                    settings_snapshot={"s1Limit": 1_000_000, "steadyEmissionY": "S1c"},
                    local_appdata=root / "localapp",
                    dialog_port=None,
                    origin_process_probe=lambda *, timeout=5.0: (),
                    process_controller=None,
                    copy_file=interrupt_after_copy,
                    run_id_factory=lambda: "interrupted-run",
                    marker_id_factory=lambda: "interrupted-marker",
                    run_origin_process_preflight=False,
                )

            self.assertTrue(temp_root.exists())
            self.assertTrue(tuple(temp_root.rglob("raw.opju")))
            self.assertIn(
                "creation identity",
                "\n".join(getattr(raised.exception, "__notes__", ())),
            )

    def test_context_creation_cleanup_refusal_reports_failure_without_deleting_unknown_path(self):
        with tempfile.TemporaryDirectory() as temp_name:
            root = pathlib.Path(temp_name)
            original = root / "raw.opju"
            original.write_bytes(b"origin")
            temp_root = root / "localapp" / "Spectrum Organizer" / "temp" / "refused-run"
            unknown_path = temp_root / "unknown.bin"

            def fail_with_unknown_path(source, target):
                del source
                unknown_path.write_bytes(b"not-owned")
                raise OSError("copy failed")

            with self.assertRaisesRegex(RuntimeError, "临时文件清理失败"):
                product_runner.prepare_approved_pre_extraction_context(
                    selected_source_paths=(original,),
                    output_parent=root / "out",
                    settings_snapshot={"s1Limit": 1_000_000, "steadyEmissionY": "S1c"},
                    local_appdata=root / "localapp",
                    dialog_port=None,
                    origin_process_probe=lambda *, timeout=5.0: (),
                    process_controller=None,
                    copy_file=fail_with_unknown_path,
                    run_id_factory=lambda: "refused-run",
                    marker_id_factory=lambda: "refused-marker",
                    run_origin_process_preflight=False,
                )

            self.assertTrue(unknown_path.exists())
            self.assertTrue(temp_root.exists())

    def test_extraction_source_copy_must_be_under_context_temp_root(self):
        with tempfile.TemporaryDirectory() as temp_name:
            root = pathlib.Path(temp_name)
            temp_root = root / "owned"
            temp_root.mkdir()
            outside_copy = root / "outside.opju"
            outside_copy.write_bytes(b"copy")
            context = SimpleNamespace(
                temp_root=temp_root,
                source_fingerprints_before=(SimpleNamespace(path=root / "original.opju", sha256="abc"),),
                run_owned_source_copy_paths=(outside_copy,),
            )

            with self.assertRaisesRegex(ProductRunnerError, "outside the task temp root"):
                product_runner._build_extraction_sources(context, lambda **kwargs: kwargs)

    def test_snapshot_is_owned_immediate_child_and_cleanup_removes_it(self):
        from spectrum_organizer.origin.extract_worker import InventoryBook, TerminalBookResult
        from spectrum_organizer.safety.owned_paths import cleanup_owned_temp_root, read_ownership

        with tempfile.TemporaryDirectory() as temp_name:
            root = pathlib.Path(temp_name)
            original = root / "raw.opju"
            original.write_bytes(b"origin")
            context = product_runner.prepare_approved_pre_extraction_context(
                selected_source_paths=(original,),
                output_parent=root / "out",
                settings_snapshot={"s1Limit": 1_000_000, "steadyEmissionY": "S1c"},
                local_appdata=root / "localapp",
                dialog_port=None,
                origin_process_probe=lambda *, timeout=5.0: (),
                process_controller=None,
                run_origin_process_preflight=False,
            )
            snapshot_path = context.temp_root / "extraction.sqlite3"

            class Worker:
                def __init__(self, source_id):
                    self.source_id = source_id
                    self.book = InventoryBook(source_id, "Root", "Book1", "Display", 1, ("Note", "Data"), True, True)

                def iter_inventory(self, copy_path, allowlist):
                    yield self.book

                def iter_book_results(self):
                    yield self.book, TerminalBookResult(
                        source_id=self.source_id,
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
                    )

                def close(self):
                    pass

            class Factory:
                def create(self, source_id, attempt):
                    return Worker(source_id)

            product_runner.run_approved_extraction_phase(
                context,
                snapshot_path=snapshot_path,
                worker_factory_builder=lambda **kwargs: Factory(),
            )

            fingerprint = context.source_fingerprints_before[0]
            connection = sqlite3.connect(snapshot_path)
            try:
                source_row = connection.execute(
                    "select original_path, original_size_bytes, "
                    "original_mtime_ns from source_files "
                    "where source_id = 'S0001'"
                ).fetchone()
            finally:
                connection.close()
            self.assertEqual(
                (
                    os.path.normcase(str(original.resolve())),
                    fingerprint.size_bytes,
                    fingerprint.mtime_ns,
                ),
                source_row,
            )
            self.assertIn(snapshot_path, read_ownership(context.temp_root).allowed_children)
            cleanup_owned_temp_root(context.temp_root)
            self.assertFalse(context.temp_root.exists())

    def test_snapshot_cannot_replace_reserved_ownership_marker(self):
        with tempfile.TemporaryDirectory() as temp_name:
            root = pathlib.Path(temp_name)
            original = root / "raw.opju"
            original.write_bytes(b"origin")
            context = product_runner.prepare_approved_pre_extraction_context(
                selected_source_paths=(original,),
                output_parent=root / "out",
                settings_snapshot={"s1Limit": 1_000_000, "steadyEmissionY": "S1c"},
                local_appdata=root / "localapp",
                dialog_port=None,
                origin_process_probe=lambda *, timeout=5.0: (),
                process_controller=None,
                run_origin_process_preflight=False,
            )

            with self.assertRaisesRegex(ProductRunnerError, "reserved ownership metadata"):
                product_runner.run_approved_extraction_phase(
                    context,
                    snapshot_path=context.temp_root / "ownership.json",
                    worker_factory_builder=lambda **_kwargs: self.fail("worker must not be built"),
                )

    def test_extraction_summary_uses_sql_status_counts_without_loading_payloads(self):
        class AggregateOnlySnapshot:
            path = pathlib.Path("run.sqlite3")

            def source_copy_path(self, _source_id):
                return pathlib.Path("copy.opju")

            def inventory_count(self, _source_id):
                return 3

            def result_count(self, _source_id):
                return 3

            def status_count(self, _source_id, status):
                return {"extracted": 2, "rejected": 1}[status]

            def book_results(self, _source_id):
                raise AssertionError("summary must not materialize Book payloads")

        source = type("Source", (), {"source_id": "S1", "original_path": pathlib.Path("raw.opju")})()
        summary = product_runner._summarize_extraction(AggregateOnlySnapshot(), (source,))

        self.assertEqual(2, summary.total_extracted_count)
        self.assertEqual(1, summary.total_rejected_count)

    def test_final_extraction_verification_includes_generic_protected_fingerprint(self):
        class VerificationSentinel(RuntimeError):
            pass

        with tempfile.TemporaryDirectory() as temp_name:
            root = pathlib.Path(temp_name)
            original = root / "raw.opju"
            protected_reference = root / "protected-reference.opju"
            original.write_bytes(b"origin")
            protected_reference.write_bytes(b"reference")
            context = product_runner.prepare_approved_pre_extraction_context(
                selected_source_paths=(original,),
                output_parent=root / "out",
                settings_snapshot={"s1Limit": 1_000_000, "steadyEmissionY": "S1c"},
                local_appdata=root / "localapp",
                protected_paths=(protected_reference,),
                dialog_port=None,
                origin_process_probe=lambda *, timeout=5.0: (),
                process_controller=None,
                run_origin_process_preflight=False,
            )

            with mock.patch(
                "spectrum_organizer.origin.extract_worker.ExtractionOrchestrator.run"
            ), mock.patch(
                "spectrum_organizer.product_runner.verify_sources_unchanged",
                side_effect=VerificationSentinel("verified"),
            ) as verify:
                with self.assertRaises(VerificationSentinel):
                    product_runner.run_approved_extraction_phase(
                        context,
                        worker_factory_builder=lambda **_kwargs: object(),
                    )

            verified_paths = tuple(snapshot.path for snapshot in verify.call_args.args[0])
            self.assertEqual((original, protected_reference), verified_paths)

    def test_approved_extraction_passes_confirmed_validation_settings_to_orchestrator(self):
        class StopAfterConstruction(RuntimeError):
            pass

        with tempfile.TemporaryDirectory() as temp_name:
            root = pathlib.Path(temp_name)
            original = root / "raw.opju"
            original.write_bytes(b"origin")
            context = product_runner.prepare_approved_pre_extraction_context(
                selected_source_paths=(original,),
                output_parent=root / "out",
                settings_snapshot={
                    "s1Limit": 42,
                    "steadyEmissionY": "S1c/R1c",
                    "allowMissingS1": True,
                },
                local_appdata=root / "localapp",
                dialog_port=None,
                origin_process_probe=lambda *, timeout=5.0: (),
                process_controller=None,
                run_origin_process_preflight=False,
            )

            with mock.patch(
                "spectrum_organizer.origin.extract_worker.ExtractionOrchestrator"
            ) as orchestrator:
                orchestrator.return_value.run.side_effect = StopAfterConstruction
                with self.assertRaises(StopAfterConstruction):
                    product_runner.run_approved_extraction_phase(
                        context,
                        worker_factory_builder=lambda **_kwargs: object(),
                    )

            self.assertEqual(42, orchestrator.call_args.kwargs["s1_limit"])
            self.assertEqual("S1c/R1c", orchestrator.call_args.kwargs["steady_emission_y"])
            self.assertTrue(orchestrator.call_args.kwargs["allow_missing_s1"])

    def test_per_source_child_rechecks_only_selected_original_and_protected_files(self):
        with tempfile.TemporaryDirectory() as temp_name:
            root = pathlib.Path(temp_name)
            originals = tuple(root / f"raw-{index}.opju" for index in range(3))
            for index, path in enumerate(originals):
                path.write_bytes(f"raw-{index}".encode("ascii"))
            protected = root / "protected-reference.opju"
            protected.write_bytes(b"reference")
            context = product_runner.prepare_approved_pre_extraction_context(
                selected_source_paths=originals,
                output_parent=root / "out",
                settings_snapshot={"s1Limit": 1_000_000, "steadyEmissionY": "S1c"},
                local_appdata=root / "localapp",
                protected_paths=(protected,),
                dialog_port=None,
                origin_process_probe=lambda *, timeout=5.0: (),
                process_controller=None,
                run_origin_process_preflight=False,
            )
            class VerificationSentinel(RuntimeError):
                pass

            with mock.patch(
                "spectrum_organizer.origin.extract_worker.ExtractionOrchestrator.run"
            ), mock.patch(
                "spectrum_organizer.product_runner.verify_sources_unchanged",
                side_effect=VerificationSentinel,
            ) as verify:
                with self.assertRaises(VerificationSentinel):
                    product_runner.run_approved_source_extraction_phase(
                        context,
                        source_id="S0002",
                        snapshot_path=context.temp_root / "run.sqlite3",
                        worker_factory_builder=lambda **_kwargs: object(),
                    )

            verified_paths = tuple(snapshot.path for snapshot in verify.call_args.args[0])
            self.assertEqual((originals[1], protected), verified_paths)

    def test_refresh_copy_uses_new_retry_path_when_original_copy_cannot_be_deleted(self):
        from spectrum_organizer.origin.extract_worker import ExtractionSource
        from spectrum_organizer.safety.fingerprints import snapshot_sources
        from spectrum_organizer.safety.owned_paths import (
            add_allowed_child,
            cleanup_owned_temp_root,
            create_run_ownership,
            read_ownership,
        )

        with tempfile.TemporaryDirectory() as temp_name:
            root = pathlib.Path(temp_name)
            original = root / "original.opju"
            original.write_bytes(b"original")
            ownership = create_run_ownership(root / "localapp", "retry-run", "retry-marker", [])
            source_dir = ownership.temp_root / "source-0001"
            source_dir.mkdir()
            ownership = add_allowed_child(ownership, source_dir)
            initial_copy = source_dir / "source.opju"
            initial_copy.write_bytes(b"original")
            ownership = add_allowed_child(ownership, initial_copy)
            snapshot = snapshot_sources([original], protected_paths=[])[0]
            source = ExtractionSource(
                source_id="S0001",
                copy_path=initial_copy,
                sha256=snapshot.sha256,
                original_path=original,
                allowed_children=(initial_copy.parent,),
                protected_paths=(original,),
            )
            manager = product_runner.ExtractionSourceManager(
                (source,),
                (snapshot,),
                temp_root=ownership.temp_root,
            )

            retry_copy = manager.refresh_copy("S0001")

            self.assertNotEqual(initial_copy, retry_copy)
            self.assertTrue(initial_copy.exists())
            self.assertEqual(b"original", retry_copy.read_bytes())
            self.assertIn(retry_copy, read_ownership(ownership.temp_root).allowed_children)
            manager.verify_copy("S0001")
            cleanup_owned_temp_root(ownership.temp_root)
            self.assertFalse(ownership.temp_root.exists())

    def test_refresh_copy_refuses_equal_content_replacement_after_writer_closes(self):
        from spectrum_organizer.origin.extract_worker import ExtractionSource
        from spectrum_organizer.safety.fingerprints import snapshot_sources
        from spectrum_organizer.safety.owned_paths import (
            add_allowed_child,
            create_run_ownership,
        )

        with tempfile.TemporaryDirectory() as temp_name:
            root = pathlib.Path(temp_name)
            original = root / "original.opju"
            original.write_bytes(b"original")
            ownership = create_run_ownership(
                root / "localapp",
                "retry-run",
                "retry-marker",
                [],
            )
            source_dir = ownership.temp_root / "source-0001"
            source_dir.mkdir()
            add_allowed_child(ownership, source_dir)
            initial_copy = source_dir / "source.opju"
            initial_copy.write_bytes(b"original")
            snapshot = snapshot_sources([original], protected_paths=[])[0]
            source = ExtractionSource(
                source_id="S0001",
                copy_path=initial_copy,
                sha256=snapshot.sha256,
                original_path=original,
                allowed_children=(source_dir,),
                protected_paths=(original,),
            )
            manager = product_runner.ExtractionSourceManager(
                (source,),
                (snapshot,),
                temp_root=ownership.temp_root,
            )
            original_copy = product_runner._copy_file_cancellable
            parked = source_dir / "parked-owned-retry.opju"

            def copy_then_replace(
                source_path,
                target_path,
                *,
                cancel_check=None,
                creation_callback=None,
            ):
                identity = original_copy(
                    source_path,
                    target_path,
                    cancel_check=cancel_check,
                    creation_callback=creation_callback,
                )
                pathlib.Path(target_path).rename(parked)
                pathlib.Path(target_path).write_bytes(b"original")
                return identity

            with mock.patch.object(
                product_runner,
                "_copy_file_cancellable",
                side_effect=copy_then_replace,
            ), self.assertRaises(product_runner.ProductRunnerError):
                manager.refresh_copy("S0001")

            self.assertEqual(b"original", parked.read_bytes())
            self.assertEqual(
                b"original",
                (source_dir / "source.retry.opju").read_bytes(),
            )

    def test_refresh_copy_binds_creation_identity_before_copy_helper_returns(self):
        from spectrum_organizer.origin.extract_worker import ExtractionSource
        from spectrum_organizer.safety.fingerprints import snapshot_sources
        from spectrum_organizer.safety.owned_paths import (
            add_allowed_child,
            create_run_ownership,
            read_ownership,
        )

        with tempfile.TemporaryDirectory() as temp_name:
            root = pathlib.Path(temp_name)
            original = root / "original.opju"
            original.write_bytes(b"original")
            ownership = create_run_ownership(
                root / "localapp",
                "retry-run",
                "retry-marker",
                [],
            )
            source_dir = ownership.temp_root / "source-0001"
            source_dir.mkdir()
            add_allowed_child(ownership, source_dir)
            initial_copy = source_dir / "source.opju"
            initial_copy.write_bytes(b"original")
            snapshot = snapshot_sources([original], protected_paths=[])[0]
            manager = product_runner.ExtractionSourceManager(
                (
                    ExtractionSource(
                        source_id="S0001",
                        copy_path=initial_copy,
                        sha256=snapshot.sha256,
                        original_path=original,
                        allowed_children=(source_dir,),
                        protected_paths=(original,),
                    ),
                ),
                (snapshot,),
                temp_root=ownership.temp_root,
            )
            original_copy = product_runner._copy_file_cancellable
            observed = {}

            def copy_and_observe(
                source_path,
                target_path,
                *,
                cancel_check=None,
                creation_callback,
            ):
                def bind_and_observe(path, identity):
                    creation_callback(path, identity)
                    persisted = read_ownership(ownership.temp_root)
                    observed["bound_before_return"] = (
                        dict(persisted.allowed_child_identities).get(path)
                        == identity
                    )

                return original_copy(
                    source_path,
                    target_path,
                    cancel_check=cancel_check,
                    creation_callback=bind_and_observe,
                )

            with mock.patch.object(
                product_runner,
                "_copy_file_cancellable",
                side_effect=copy_and_observe,
            ):
                manager.refresh_copy("S0001")

            self.assertTrue(observed.get("bound_before_return"))

    def test_snapshot_reservation_binds_identity_before_creation_handle_closes(self):
        from spectrum_organizer.safety.owned_paths import create_run_ownership

        with tempfile.TemporaryDirectory() as temp_name:
            root = pathlib.Path(temp_name)
            ownership = create_run_ownership(
                root / "localapp",
                "snapshot-run",
                "snapshot-marker",
                [],
            )
            context = SimpleNamespace(temp_root=ownership.temp_root)
            snapshot_path = ownership.temp_root / "run_snapshot.sqlite3"
            real_open = pathlib.Path.open
            real_bind = product_runner.bind_allowed_child_identity
            state = {"closed": False, "bound_while_open": False}

            class TrackedCreation:
                def __enter__(self):
                    self.stream = real_open(snapshot_path, "xb")
                    return self.stream

                def __exit__(self, exc_type, exc, traceback):
                    state["closed"] = True
                    self.stream.close()

            def tracked_open(path, mode="r", *args, **kwargs):
                if pathlib.Path(path) == snapshot_path and mode == "xb":
                    return TrackedCreation()
                return real_open(path, mode, *args, **kwargs)

            def observe_bind(current, child, expected_identity=None):
                if pathlib.Path(child) == snapshot_path:
                    state["bound_while_open"] = not state["closed"]
                return real_bind(
                    current,
                    child,
                    expected_identity=expected_identity,
                )

            with (
                mock.patch.object(pathlib.Path, "open", tracked_open),
                mock.patch.object(
                    product_runner,
                    "bind_allowed_child_identity",
                    side_effect=observe_bind,
                ),
            ):
                product_runner._register_snapshot_path(context, snapshot_path)

            self.assertTrue(state["bound_while_open"])

    def test_locked_failed_copy_blocks_retry_instead_of_leaving_two_owned_copies(self):
        from spectrum_organizer.origin.extract_worker import ExtractionSource
        from spectrum_organizer.safety.fingerprints import snapshot_sources
        from spectrum_organizer.safety.owned_paths import add_allowed_child, create_run_ownership

        with tempfile.TemporaryDirectory() as temp_name:
            root = pathlib.Path(temp_name)
            original = root / "original.opju"
            original.write_bytes(b"original")
            ownership = create_run_ownership(root / "localapp", "retry-run", "retry-marker", [])
            source_dir = ownership.temp_root / "source-0001"
            source_dir.mkdir()
            add_allowed_child(ownership, source_dir)
            initial_copy = source_dir / "source.opju"
            initial_copy.write_bytes(b"original")
            snapshot = snapshot_sources([original], protected_paths=[])[0]
            source = ExtractionSource(
                source_id="S0001",
                copy_path=initial_copy,
                sha256=snapshot.sha256,
                original_path=original,
                allowed_children=(source_dir,),
                protected_paths=(original,),
            )
            manager = product_runner.ExtractionSourceManager(
                (source,),
                (snapshot,),
                temp_root=ownership.temp_root,
            )

            with unittest.mock.patch(
                "spectrum_organizer.safety.identity_paths._unlink_held_file",
                side_effect=PermissionError("locked"),
            ), self.assertRaisesRegex(
                product_runner.ExtractionCleanupBlockedError,
                "locked|清理",
            ):
                manager.discard_failed_copy("S0001")

            self.assertTrue(initial_copy.exists())
            self.assertEqual(
                [initial_copy],
                list(source_dir.glob("*.opju")),
            )

    def test_failed_copy_cleanup_isolates_identity_before_unlink(self):
        from spectrum_organizer.origin.extract_worker import ExtractionSource
        from spectrum_organizer.safety.fingerprints import snapshot_sources
        from spectrum_organizer.safety.owned_paths import (
            add_allowed_child,
            create_run_ownership,
        )

        with tempfile.TemporaryDirectory() as temp_name:
            root = pathlib.Path(temp_name)
            original = root / "original.opju"
            original.write_bytes(b"original")
            ownership = create_run_ownership(
                root / "localapp",
                "retry-run",
                "retry-marker",
                [],
            )
            source_dir = ownership.temp_root / "source-0001"
            source_dir.mkdir()
            add_allowed_child(ownership, source_dir)
            initial_copy = source_dir / "source.opju"
            initial_copy.write_bytes(b"original")
            snapshot = snapshot_sources([original], protected_paths=[])[0]
            source = ExtractionSource(
                source_id="S0001",
                copy_path=initial_copy,
                sha256=snapshot.sha256,
                original_path=original,
                allowed_children=(source_dir,),
                protected_paths=(original,),
            )
            manager = product_runner.ExtractionSourceManager(
                (source,),
                (snapshot,),
                temp_root=ownership.temp_root,
            )
            parked = source_dir / "parked-owned-copy.opju"
            original_unlink = pathlib.Path.unlink
            direct_delete_injected = False

            def replace_if_direct_unlink(path, *args, **kwargs):
                nonlocal direct_delete_injected
                path = pathlib.Path(path)
                if path == initial_copy:
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
                manager.discard_failed_copy("S0001")

            self.assertFalse(direct_delete_injected)
            self.assertFalse(initial_copy.exists())
            self.assertFalse(parked.exists())

    def test_retry_copy_checks_cancellation_while_streaming(self):
        from spectrum_organizer.origin.extract_worker import ExtractionSource
        from spectrum_organizer.safety.fingerprints import snapshot_sources
        from spectrum_organizer.safety.owned_paths import add_allowed_child, create_run_ownership

        with tempfile.TemporaryDirectory() as temp_name:
            root = pathlib.Path(temp_name)
            original = root / "large.opju"
            original.write_bytes(b"x" * (2 * 1024 * 1024))
            ownership = create_run_ownership(root / "localapp", "cancel-run", "cancel-marker", [])
            source_dir = ownership.temp_root / "source-0001"
            source_dir.mkdir()
            add_allowed_child(ownership, source_dir)
            initial_copy = source_dir / "large.opju"
            initial_copy.write_bytes(original.read_bytes())
            snapshot = snapshot_sources([original], protected_paths=[])[0]
            source = ExtractionSource(
                source_id="S0001",
                copy_path=initial_copy,
                sha256=snapshot.sha256,
                original_path=original,
                allowed_children=(source_dir,),
                protected_paths=(original,),
            )
            checks = []

            def cancel_check():
                checks.append(None)
                if len(checks) == 2:
                    raise product_runner.ProductRunnerError("cancelled")

            manager = product_runner.ExtractionSourceManager(
                (source,),
                (snapshot,),
                temp_root=ownership.temp_root,
                cancel_check=cancel_check,
            )

            with self.assertRaisesRegex(product_runner.ProductRunnerError, "cancelled"):
                manager.refresh_copy("S0001")
            self.assertEqual(2, len(checks))

    def test_readiness_reports_every_missing_task15_surface(self):
        report = check_task15_readiness(ProductRunnerDependencies())

        self.assertFalse(report.ready)
        self.assertEqual(
            (
                "manual_dialog_port",
                "extraction_worker_factory",
                "output_worker",
                "verifier_worker",
                "create_staging",
                "publish_run",
                "report_builder",
                "protected_path_audit_hook",
                "final_process_count_hook",
                "state_machine_factory",
            ),
            report.missing,
        )
        self.assertEqual("Provide missing production dependencies before Task 15.", report.next_action)

    def test_readiness_rejects_callable_only_dialog_port(self):
        deps = _ready_deps(manual_dialog_port=lambda request: DialogResponse("confirm"))

        report = check_task15_readiness(deps)

        self.assertFalse(report.ready)
        self.assertEqual(("manual_dialog_port",), report.missing)

    def test_readiness_requires_book_only_mode(self):
        deps = _ready_deps(mode="copy_originals")

        report = check_task15_readiness(deps)

        self.assertFalse(report.ready)
        self.assertEqual(("book_only mode",), report.missing)

    def test_readiness_rejects_unmarked_noop_audit_hooks(self):
        deps = _ready_deps(
            protected_path_audit_hook=lambda: None,
            final_process_count_hook=lambda: None,
        )

        report = check_task15_readiness(deps)

        self.assertFalse(report.ready)
        self.assertEqual(("protected_path_audit_hook", "final_process_count_hook"), report.missing)

    def test_readiness_rejects_marker_only_audit_hooks_and_missing_process_probe(self):
        marker_only = MarkerOnlyHook("protected_path_audit")
        missing_probe = FinalProcessCountHook()
        deps = _ready_deps(
            protected_path_audit_hook=marker_only,
            final_process_count_hook=missing_probe,
        )

        report = check_task15_readiness(deps)

        self.assertFalse(report.ready)
        self.assertEqual(("protected_path_audit_hook", "final_process_count_hook"), report.missing)
        with self.assertRaisesRegex(ProductRunnerError, "requires a real process probe"):
            missing_probe()

    def test_ready_dependencies_report_task15_pre_smoke_stop(self):
        report = check_task15_readiness(_ready_deps())

        self.assertTrue(report.ready)
        self.assertEqual((), report.missing)
        self.assertEqual("Stop before Task 15 smoke execution.", report.next_action)

    def test_readiness_fails_when_origin_process_probe_finds_processes(self):
        report = check_task15_readiness(
            _ready_deps(
                final_process_count_hook=FinalProcessCountHook(
                    lambda *, timeout=5.0: ("Origin:123",)
                )
            )
        )

        self.assertFalse(report.ready)
        self.assertEqual(("final_process_count_hook",), report.missing)

    def test_prepare_for_task15_uses_state_machine_without_invoking_workers(self):
        events = []
        deps = _ready_deps(events)
        runner = ProductWorkflowRunner(deps)

        plan = runner.prepare_for_task15()

        self.assertEqual(
            (
                "source_selection",
                "preflight",
                "save_and_close_origin",
                "extraction",
                "attribution",
                "special_review",
                "duplicate_review",
                "excitation_selection",
                "final_attribution_summary",
                "sample_record_commit",
                "approved_snapshot",
                "output_staging",
                "verification",
                "publication",
                "publication_ready",
            ),
            plan,
        )
        self.assertNotIn("output_inspection", plan)
        self.assertNotIn("completion", plan)
        self.assertEqual(["state_machine_factory"], events)

    def test_prepare_for_task15_rejects_incomplete_dependencies(self):
        runner = ProductWorkflowRunner(ProductRunnerDependencies())

        with self.assertRaisesRegex(ProductRunnerError, "Task 15 readiness failed"):
            runner.prepare_for_task15()

    def test_readiness_main_reports_readiness_without_startup_or_origin_launch(self):
        stream = io.StringIO()

        exit_code = readiness_main(deps=_ready_deps(), output=stream)

        self.assertEqual(0, exit_code)
        self.assertEqual("ready: Stop before Task 15 smoke execution.\n", stream.getvalue())

    def test_default_product_dependencies_have_real_surfaces_without_executing_task15(self):
        deps = build_default_product_dependencies()

        self.assertTrue(callable(getattr(deps.manual_dialog_port, "choose", None)))
        self.assertIsInstance(deps.protected_path_audit_hook(), ProtectedPathAuditPlan)
        self.assertEqual(0, deps.final_process_count_hook.expected_origin_process_count)

    def test_default_final_process_probe_stays_hidden_in_windowed_package(self):
        completed = SimpleNamespace(returncode=0, stdout="", stderr="")
        with mock.patch.object(
            product_runner.subprocess,
            "run",
            return_value=completed,
        ) as run:
            count = build_default_product_dependencies().final_process_count_hook()

        self.assertEqual(0, count)
        self.assertEqual(
            getattr(product_runner.subprocess, "CREATE_NO_WINDOW", 0),
            run.call_args.kwargs["creationflags"],
        )

    def test_pre_extraction_context_snapshots_copies_and_runs_origin_gate_after_space_check(self):
        self.assertTrue(hasattr(product_runner, "prepare_approved_pre_extraction_context"))
        with tempfile.TemporaryDirectory() as temp_name:
            root = pathlib.Path(temp_name)
            source = root / "sample.opj"
            source.write_bytes(b"source data")
            protected_reference = root / "protected-reference.opju"
            protected_reference.write_bytes(b"reference only")
            output_parent = root / "out"
            output_parent.mkdir()
            dialog = RecordingDialogPort({"save_and_close_origin": "retry"})
            controller = NoOriginController()

            context = product_runner.prepare_approved_pre_extraction_context(
                selected_source_paths=(source,),
                output_parent=output_parent,
                settings_snapshot={"s1Limit": 42, "steadyEmissionY": "S1c/R1c"},
                local_appdata=root / "localapp",
                protected_paths=(protected_reference,),
                dialog_port=dialog,
                origin_process_probe=lambda *, timeout=5.0: (),
                process_controller=controller,
                free_bytes_provider=lambda path: 2 * 1024**3,
                run_id_factory=lambda: "run-abc",
                marker_id_factory=lambda: "marker-abc",
                timestamp_factory=lambda: "2026-07-08T00:00:00Z",
            )

            self.assertEqual("run-abc", context.run_id)
            self.assertEqual("2026-07-08T00:00:00Z", context.timestamp)
            self.assertEqual((source,), context.selected_source_paths)
            self.assertEqual(output_parent, context.output_parent)
            self.assertEqual({"s1Limit": 42, "steadyEmissionY": "S1c/R1c"}, context.settings_snapshot)
            self.assertEqual(1, len(context.source_fingerprints_before))
            self.assertEqual(source, context.source_fingerprints_before[0].path)
            self.assertEqual(len(b"source data"), context.source_fingerprints_before[0].size_bytes)
            self.assertEqual(1, len(context.run_owned_source_copy_paths))
            self.assertEqual(b"source data", context.run_owned_source_copy_paths[0].read_bytes())
            self.assertTrue(str(context.run_owned_source_copy_paths[0]).startswith(str(context.temp_root)))
            self.assertEqual(("save_and_close_origin",), tuple(request.kind for request in dialog.requests))
            self.assertEqual([], controller.calls)

    def test_default_pre_extraction_timestamp_matches_publication_name_contract(self):
        with tempfile.TemporaryDirectory() as temp_name:
            root = pathlib.Path(temp_name)
            source = root / "sample.opj"
            source.write_bytes(b"source data")

            context = product_runner.prepare_approved_pre_extraction_context(
                selected_source_paths=(source,),
                output_parent=root / "out",
                settings_snapshot={
                    "s1Limit": 42,
                    "steadyEmissionY": "S1c",
                },
                local_appdata=root / "localapp",
                dialog_port=NoDialogPort(),
                origin_process_probe=lambda *, timeout=5.0: (),
                process_controller=NoOriginController(),
                free_bytes_provider=lambda path: 2 * 1024**3,
                run_id_factory=lambda: "run-default-time",
                marker_id_factory=lambda: "marker-default-time",
                run_origin_process_preflight=False,
            )

            self.assertRegex(context.timestamp, r"^\d{8}_\d{6}$")

    def test_pre_extraction_context_insufficient_temp_space_stops_before_copy_or_origin_prompt(self):
        from spectrum_organizer.safety.source_copies import InsufficientSpaceError

        self.assertTrue(hasattr(product_runner, "prepare_approved_pre_extraction_context"))
        with tempfile.TemporaryDirectory() as temp_name:
            root = pathlib.Path(temp_name)
            source = root / "sample.opj"
            source.write_bytes(b"source data")
            copy_calls = []

            with self.assertRaises(InsufficientSpaceError):
                product_runner.prepare_approved_pre_extraction_context(
                    selected_source_paths=(source,),
                    output_parent=root / "out",
                    settings_snapshot={"s1Limit": 42, "steadyEmissionY": "S1c"},
                    local_appdata=root / "localapp",
                    protected_paths=(),
                    dialog_port=NoDialogPort(),
                    origin_process_probe=lambda *, timeout=5.0: (_ for _ in ()).throw(
                        AssertionError("Origin probe must not run")
                    ),
                    process_controller=NoOriginController(),
                    free_bytes_provider=lambda path: 1,
                    copy_file=lambda source_path, target_path: copy_calls.append((source_path, target_path)),
                    run_id_factory=lambda: "run-low-space",
                    marker_id_factory=lambda: "marker-low-space",
                    timestamp_factory=lambda: "2026-07-08T00:00:00Z",
                )

            self.assertEqual([], copy_calls)

    def test_orchestrator_exposes_approved_inputs_only_after_preflight_confirmation(self):
        store = RecordingSettingsStore()
        orchestrator = BookOnlyOrchestrator(store)
        self.assertTrue(hasattr(orchestrator, "approved_pre_extraction_inputs"))

        orchestrator.select_sources(["C:/raw/a.opj"])
        orchestrator.select_output_parent("D:/Organized")

        with self.assertRaisesRegex(RuntimeError, "preflight settings"):
            orchestrator.approved_pre_extraction_inputs()

        orchestrator.confirm_preflight_settings(s1_limit=42, steady_emission_y="S1c/R1c")

        approved = orchestrator.approved_pre_extraction_inputs()
        self.assertEqual(("C:/raw/a.opj",), approved.selected_source_paths)
        self.assertEqual("D:/Organized", approved.output_parent)
        self.assertEqual(
            {"s1Limit": 42, "steadyEmissionY": "S1c/R1c", "allowMissingS1": False},
            approved.settings_snapshot,
        )
        self.assertEqual([(42, "S1c/R1c", False)], store.preflight_writes)
        with self.assertRaises(TypeError):
            approved.settings_snapshot["s1Limit"] = 999
        self.assertEqual(
            42,
            orchestrator.task_cache["settings_snapshot"]["s1Limit"],
        )

    def test_orchestrator_does_not_approve_preflight_settings_if_persist_fails(self):
        store = RecordingSettingsStore(fail_preflight=True)
        orchestrator = BookOnlyOrchestrator(store)
        orchestrator.select_sources(["C:/raw/a.opj"])
        orchestrator.select_output_parent("D:/Organized")

        with self.assertRaisesRegex(ValueError, "invalid test settings"):
            orchestrator.confirm_preflight_settings(s1_limit=42, steady_emission_y="S1c/R1c")
        with self.assertRaisesRegex(RuntimeError, "preflight settings"):
            orchestrator.approved_pre_extraction_inputs()

    def test_default_product_dependency_readiness_can_be_verified_with_deterministic_zero_probe(self):
        deps = build_default_product_dependencies()
        deterministic = ProductRunnerDependencies(
            manual_dialog_port=deps.manual_dialog_port,
            extraction_worker_factory=deps.extraction_worker_factory,
            output_worker=deps.output_worker,
            verifier_worker=deps.verifier_worker,
            create_staging=deps.create_staging,
            publish_run=deps.publish_run,
            report_builder=deps.report_builder,
            protected_path_audit_hook=deps.protected_path_audit_hook,
            final_process_count_hook=FinalProcessCountHook(lambda *, timeout=5.0: ()),
            state_machine_factory=deps.state_machine_factory,
            mode=deps.mode,
        )

        report = check_task15_readiness(deterministic)
        plan = assert_ready_for_task15(deterministic)

        self.assertTrue(report.ready)
        self.assertEqual("publication_ready", plan[-1])
        self.assertNotIn("output_inspection", plan)
        self.assertNotIn("completion", plan)

    def test_default_extraction_factory_builder_uses_confirmed_preflight_settings(self):
        deps = build_default_product_dependencies()

        factory = deps.extraction_worker_factory(
            settings_snapshot={
                "s1Limit": 42,
                "steadyEmissionY": "S1c/R1c",
                "allowMissingS1": True,
            }
        )

        self.assertEqual(42, factory._s1_limit)
        self.assertEqual("S1c/R1c", factory._steady_emission_y)
        self.assertTrue(factory._allow_missing_s1)

    def test_readiness_main_uses_default_product_dependencies_when_not_injected(self):
        stream = io.StringIO()

        exit_code = readiness_main(output=stream)

        self.assertIn(exit_code, (0, 1))
        self.assertRegex(stream.getvalue(), r"^(ready|not ready):")

    def test_manual_dialog_flow_uses_real_dialog_catalog_and_state_machine(self):
        port = RecordingDialogPort(
            {
                "preflight_settings": "confirm",
                "save_and_close_origin": "retry",
                "attribution": "confirm",
                "final_attribution_summary": "confirm",
                "output_can_be_inspected": "continue",
            }
        )
        flow = ProductManualDialogFlow(port, TaskStateMachine())

        observed = flow.run(
            ManualDialogSmokeInputs(
                s1_limit=1000000,
                steady_emission_y="S1c",
                attribution_fields={"sample": "MFL", "solvent": "mTHF", "concentration": "1×10^-4 M", "temperature": "298 K"},
                canonical_labels=("MFL-mTHF-1×10^-4 M-298 K",),
                sample_record_ids={"20250412|/MFL_RT/|DfltEm1": 1},
            )
        )

        self.assertEqual(
            (
                "source_selection",
                "preflight",
                "save_and_close_origin",
                "extraction",
                "attribution",
                "special_review_skipped:0",
                "duplicate_review_skipped:0",
                "excitation_selection_skipped:0",
                "final_attribution_summary",
                "sample_record_commit",
                "approved_snapshot",
                "output_staging",
                "verification",
                "publication",
                "output_inspection",
                "completion",
            ),
            observed,
        )
        self.assertEqual(
            (
                "preflight_settings",
                "save_and_close_origin",
                "attribution",
                "final_attribution_summary",
                "output_can_be_inspected",
            ),
            tuple(request.kind for request in port.requests),
        )
        final_request = port.requests[3]
        self.assertEqual((1, 0, 0, 1), final_request.counts)
        self.assertEqual(
            ("MFL-mTHF-1×10^-4 M-298 K",),
            tuple(row.book_name for row in final_request.rows),
        )

    def test_manual_dialog_flow_rejects_pending_review_candidates_without_dialogs(self):
        port = RecordingDialogPort(
            {
                "preflight_settings": "confirm",
                "save_and_close_origin": "retry",
                "attribution": "confirm",
            }
        )
        flow = ProductManualDialogFlow(port, TaskStateMachine())

        with self.assertRaisesRegex(ProductRunnerError, "cannot skip special_review"):
            flow.run(
                ManualDialogSmokeInputs(
                    s1_limit=1000000,
                    steady_emission_y="S1c",
                    attribution_fields={"sample": "MFL"},
                    canonical_labels=("MFL",),
                    sample_record_ids={},
                    special_review_candidates=1,
                )
            )

    def test_manual_dialog_flow_rejects_invalid_confirm_even_from_non_qt_port(self):
        port = RecordingDialogPort(
            {
                "preflight_settings": "confirm",
                "save_and_close_origin": "retry",
                "attribution": "confirm",
            }
        )
        flow = ProductManualDialogFlow(port, TaskStateMachine())

        with self.assertRaisesRegex(ProductRunnerError, "cannot confirm invalid input"):
            flow.run(
                ManualDialogSmokeInputs(
                    s1_limit=1000000,
                    steady_emission_y="S1c",
                    attribution_fields={"sample": "MFL\nSolid"},
                    canonical_labels=("MFL",),
                    sample_record_ids={},
                )
            )

    def test_manual_dialog_flow_rejects_closed_output_inspection(self):
        port = RecordingDialogPort(
            {
                "preflight_settings": "confirm",
                "save_and_close_origin": "retry",
                "attribution": "confirm",
                "final_attribution_summary": "confirm",
                "output_can_be_inspected": "cancel",
            }
        )
        flow = ProductManualDialogFlow(port, TaskStateMachine())

        with self.assertRaisesRegex(ProductRunnerError, "output_can_be_inspected returned cancel"):
            flow.run(
                ManualDialogSmokeInputs(
                    s1_limit=1000000,
                    steady_emission_y="S1c",
                    attribution_fields={"sample": "MFL"},
                    canonical_labels=("MFL",),
                    sample_record_ids={},
                )
            )

    def test_manual_dialog_flow_rejects_cancelled_attribution(self):
        port = RecordingDialogPort(
            {
                "preflight_settings": "confirm",
                "save_and_close_origin": "retry",
                "attribution": "cancel",
            }
        )
        flow = ProductManualDialogFlow(port, TaskStateMachine())

        with self.assertRaisesRegex(ProductRunnerError, "attribution returned cancel"):
            flow.run(
                ManualDialogSmokeInputs(
                    s1_limit=1000000,
                    steady_emission_y="S1c",
                    attribution_fields={"sample": "MFL"},
                    canonical_labels=("MFL",),
                    sample_record_ids={},
                )
            )


class FakeDialogPort:
    def choose(self, request):
        raise AssertionError("manual dialog must not be invoked during readiness")


class FakeExtractionWorkerFactory:
    def create(self, source_id, attempt):
        raise AssertionError("extraction factory must not be invoked during readiness")


class MarkerOnlyHook:
    def __init__(self, readiness_kind):
        self.readiness_kind = readiness_kind

    def __call__(self):
        return None


class RecordingDialogPort:
    def __init__(self, responses):
        self.responses = dict(responses)
        self.requests = []

    def choose(self, request):
        self.requests.append(request)
        return DialogResponse(self.responses[request.kind])


class NoDialogPort:
    def choose(self, request):
        raise AssertionError(f"Dialog must not be shown: {request.kind}")


class RecordingSettingsStore:
    def __init__(self, *, fail_preflight=False):
        self.fail_preflight = fail_preflight
        self.output_parent_writes = []
        self.preflight_writes = []

    def set_last_output_parent(self, value):
        self.output_parent_writes.append(value)
        return []

    def set_preflight_settings(self, s1_limit, steady_emission_y, allow_missing_s1=False):
        if self.fail_preflight:
            raise ValueError("invalid test settings")
        self.preflight_writes.append((s1_limit, steady_emission_y, allow_missing_s1))
        return []


class NoOriginController:
    def __init__(self):
        self.calls = []

    def close_program_owned(self, identity):
        self.calls.append(("close_program_owned", identity.pid))

    def current_process(self, pid):
        self.calls.append(("current_process", pid))
        return None

    def graceful_close(self, identity):
        self.calls.append(("graceful_close", identity.pid))
        return True

    def force_close(self, identity):
        self.calls.append(("force_close", identity.pid))
        return True

    def is_running(self, identity):
        self.calls.append(("is_running", identity.pid))
        return False


def _ready_deps(events=None, *, mode="book_only", manual_dialog_port=None, protected_path_audit_hook=None, final_process_count_hook=None):
    events = events if events is not None else []

    def forbidden(name):
        def inner(*args, **kwargs):
            events.append(name)
            raise AssertionError(f"{name} must not be invoked during readiness")

        return inner

    def state_machine_factory():
        events.append("state_machine_factory")
        return TaskStateMachine()

    return ProductRunnerDependencies(
        manual_dialog_port=manual_dialog_port or FakeDialogPort(),
        extraction_worker_factory=FakeExtractionWorkerFactory(),
        output_worker=forbidden("output_worker"),
        verifier_worker=forbidden("verifier_worker"),
        create_staging=forbidden("create_staging"),
        publish_run=forbidden("publish_run"),
        report_builder=forbidden("report_builder"),
        protected_path_audit_hook=protected_path_audit_hook or ProtectedPathAuditHook(),
        final_process_count_hook=final_process_count_hook
        or FinalProcessCountHook(lambda *, timeout=5.0: ()),
        state_machine_factory=state_machine_factory,
        mode=mode,
    )


if __name__ == "__main__":
    unittest.main()
