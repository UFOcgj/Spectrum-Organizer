import pathlib
import sys
from types import SimpleNamespace
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from spectrum_organizer.core.output_model import (
    IncompleteFolder,
    OutputBook,
    OutputColumn,
    OutputFolder,
    OutputPlan,
    OutputSpectrum,
    build_output_plan,
)
from spectrum_organizer.domain.models import SpectrumClass
from spectrum_organizer.reporting.run_report import (
    ReportData,
    ReportItem,
    SampleAttribution,
    SpecialGroupSummary,
    build_completion_summary,
    build_approved_output_report,
    build_final_output_plan_summary,
    build_success_report,
    paired_spectrum_warnings,
)
from spectrum_organizer.product_runner import CountReconciliation


class RunReportTests(unittest.TestCase):
    def test_approved_output_report_renders_every_frozen_audit_surface_and_verified_readback_count(self):
        source = SimpleNamespace(
            path=pathlib.Path(r"C:\raw\a.opju"),
            sha256="abc",
            size_bytes=123,
            mtime_ns=456,
        )
        snapshot = SimpleNamespace(
            source_fingerprints_before=(source,),
            ignored_duplicate_input_paths=(pathlib.Path(r"C:\raw\dup.opju"),),
            settings_snapshot={
                "s1Limit": 2_000_000,
                "steadyEmissionY": "S1c",
                "allowMissingS1": False,
            },
            source_input_issues=(
                SimpleNamespace(
                    original_path=pathlib.Path(
                        r"C:\raw\unsupported.opju"
                    ),
                    reason="未检测到受支持的 Origin 原始谱图",
                    recommendation=(
                        "请重新选择包含原始光谱 Book 的 Origin 项目文件。"
                    ),
                ),
            ),
            rejections=(
                SimpleNamespace(
                    book_key="rejected-key",
                    detail="Note 字段冲突",
                    source_id="S0001",
                    source_filename="a.opju",
                    folder_path="Folder A",
                    display_name="Rejected Book",
                ),
            ),
            exclusions=(
                SimpleNamespace(
                    book_key="excluded-key",
                    detail="精确重复激发谱未选择",
                    source_id="S0001",
                    source_filename="a.opju",
                    folder_path="Folder A",
                    display_name="Excluded Book",
                ),
            ),
            review_choices=(
                SimpleNamespace(
                    kind="excitation",
                    review_key="review-1",
                    selected_book_keys=("selected-key",),
                    candidate_book_keys=("selected-key", "excluded-key"),
                    decision="",
                    subject="",
                    decision_source="manual",
                ),
                SimpleNamespace(
                    kind="special_group",
                    review_key="review-2",
                    selected_book_keys=("map-key",),
                    candidate_book_keys=("map-key",),
                    decision="confirm_group",
                    subject="steady_2d",
                    decision_source="automatic",
                ),
            ),
            attributions=(
                SimpleNamespace(
                    book_key="selected-key",
                    canonical_sample_label="MFL-Solid-Air-298 K",
                    source_id="S0001",
                    source_filename="a.opju",
                    folder_path="Folder A",
                    short_name="F300",
                    display_name="Emission 300",
                ),
            ),
            approved_sources=(
                SimpleNamespace(source_id="S0001", snapshot=source),
            ),
            count_reconciliation=CountReconciliation(3, 1, 1, 1, 1, 3),
            output_plan=_plan_for_ordering(),
        )

        report = build_approved_output_report(
            snapshot,
            output_path=pathlib.Path(r"C:\out\Organized_Origin_Data_20260802_120000"),
            source_fingerprints_after=(source,),
            verifier_readback_spectrum_count=1,
            verifier_readback_column_count=3,
        )

        for text in (
            r"C:\raw\a.opju",
            r"C:\raw\dup.opju",
            "S1 强度上限：2000000",
            "稳态发射强度列：S1c",
            "缺少 S1 时继续：否",
            "C:\\raw\\unsupported.opju：未检测到受支持的 Origin 原始谱图；"
            "处理建议：请重新选择包含原始光谱 Book 的 Origin 项目文件。",
            "C:\\raw\\a.opju [source_id=S0001] · Folder A / Rejected Book [BookKey=rejected-key]：Note 字段冲突",
            "C:\\raw\\a.opju [source_id=S0001] · Folder A / Excluded Book [BookKey=excluded-key]：精确重复激发谱未选择",
            "激发谱选择：已选 selected-key；候选 selected-key、excluded-key；来源 manual",
            "二维稳态谱：map-key",
            "MFL-Solid-Air-298 K：C:\\raw\\a.opju / Folder A / Emission 300；"
            "BookKey=selected-key（已接受）",
            "提交前 SHA-256=abc；输出后 SHA-256=abc；大小=123；UTC mtime_ns=456；未改变",
            "识别 Book：3",
            "验证回读谱图：1",
            "验证回读列：3",
        ):
            self.assertIn(text, report)

    def test_rejection_subjects_disambiguate_same_basename_folder_and_book(self):
        sources = (
            SimpleNamespace(
                path=pathlib.Path(r"C:\first\same.opju"),
                sha256="a",
                size_bytes=1,
                mtime_ns=1,
            ),
            SimpleNamespace(
                path=pathlib.Path(r"D:\second\same.opju"),
                sha256="b",
                size_bytes=2,
                mtime_ns=2,
            ),
        )
        snapshot = SimpleNamespace(
            source_fingerprints_before=sources,
            ignored_duplicate_input_paths=(),
            settings_snapshot={},
            source_input_issues=(),
            rejections=tuple(
                SimpleNamespace(
                    book_key=f"key-{index}",
                    detail="rejected",
                    source_id=f"S000{index}",
                    source_filename="same.opju",
                    folder_path="Folder",
                    short_name="B1",
                    display_name="Book",
                )
                for index in (1, 2)
            ),
            exclusions=(),
            review_choices=(),
            attributions=(),
            approved_sources=tuple(
                SimpleNamespace(source_id=f"S000{index}", snapshot=source)
                for index, source in enumerate(sources, start=1)
            ),
            count_reconciliation=CountReconciliation(2, 2, 0, 0, 0, 0),
            output_plan=OutputPlan(folders=(), incomplete_folders=()),
        )

        report = build_approved_output_report(
            snapshot,
            output_path=pathlib.Path(r"C:\out\run"),
            source_fingerprints_after=sources,
            verifier_readback_spectrum_count=0,
            verifier_readback_column_count=0,
        )

        self.assertIn(
            r"C:\first\same.opju [source_id=S0001] · Folder / Book [short=B1; BookKey=key-1]",
            report,
        )
        self.assertIn(
            r"D:\second\same.opju [source_id=S0002] · Folder / Book [short=B1; BookKey=key-2]",
            report,
        )

    def test_attribution_report_keeps_each_book_identity_when_labels_repeat(self):
        plan = _plan_for_ordering()
        report = build_success_report(
            ReportData(
                output_path=pathlib.Path("out"),
                ignored_duplicate_input_paths=(),
                rejections=(),
                exclusions=(),
                warnings=(),
                special_groups=(),
                final_attributions=(
                    SampleAttribution(
                        "Same sample",
                        pathlib.Path("source.opju"),
                        "accepted",
                        book_key="key-a",
                        source_filename="source.opju",
                        folder_path="Folder A",
                        book_name="Book A",
                    ),
                    SampleAttribution(
                        "Same sample",
                        pathlib.Path("source.opju"),
                        "accepted",
                        book_key="key-b",
                        source_filename="source.opju",
                        folder_path="Folder A",
                        book_name="Book B",
                    ),
                ),
                output_plan=plan,
            )
        )

        self.assertIn(
            "Same sample：source.opju / Folder A / Book A；BookKey=key-a（已接受）",
            report,
        )
        self.assertIn(
            "Same sample：source.opju / Folder A / Book B；BookKey=key-b（已接受）",
            report,
        )

    def test_attribution_report_uses_full_source_path_when_basenames_repeat(self):
        report = build_success_report(
            ReportData(
                output_path=pathlib.Path("out"),
                ignored_duplicate_input_paths=(),
                rejections=(),
                exclusions=(),
                warnings=(),
                special_groups=(),
                final_attributions=(
                    SampleAttribution(
                        "Sample A",
                        pathlib.Path("C:/first/same.opju"),
                        "accepted",
                        book_key="key-a",
                        source_filename="same.opju",
                        folder_path="Folder",
                        book_name="Book",
                    ),
                    SampleAttribution(
                        "Sample B",
                        pathlib.Path("D:/second/same.opju"),
                        "accepted",
                        book_key="key-b",
                        source_filename="same.opju",
                        folder_path="Folder",
                        book_name="Book",
                    ),
                ),
                output_plan=_plan_for_ordering(),
            )
        )

        self.assertIn("C:\\first\\same.opju / Folder / Book", report)
        self.assertIn("D:\\second\\same.opju / Folder / Book", report)

    def test_final_output_plan_summary_exposes_closed_counts_structure_and_completeness(self):
        plan = _plan_for_ordering()
        reconciliation = CountReconciliation(
            recognizable_book_count=4,
            rejected_book_count=1,
            excluded_book_count=1,
            accepted_ordinary_spectrum_count=2,
            output_plan_spectrum_count=2,
            output_plan_column_count=6,
        )

        summary = build_final_output_plan_summary(
            plan,
            reconciliation,
            review_decisions=(
                "重复发射谱选择：source.opju · Folder A / Emission A",
                "激发谱选择：source.opju · Folder A / Excitation A",
            ),
        )

        self.assertTrue(summary.counts_closed)
        self.assertEqual(2, summary.folder_count)
        self.assertEqual(2, summary.book_count)
        self.assertEqual(6, summary.column_count)
        self.assertIn("识别 4 · 拒绝 1 · 排除 1 · 接受 2", summary.message)
        self.assertIn("F_complete_ALL_SAMPLES", summary.message)
        self.assertIn("F_incomplete", summary.message)
        self.assertIn("缺少：A-77 K", summary.message)
        self.assertIn("缺少：B-77 K", summary.message)
        self.assertIn("Book：A", summary.message)
        self.assertIn("审核决定", summary.message)
        self.assertIn(
            "重复发射谱选择：source.opju · Folder A / Emission A",
            summary.message,
        )

    def test_final_output_plan_summary_lists_each_book_and_column_in_order(self):
        plan = OutputPlan(
            folders=(
                OutputFolder(
                    "F_Ex270_ALL_SAMPLES",
                    "F",
                    False,
                    (
                        OutputBook(
                            "A",
                            (
                                OutputColumn("x", "Em", ()),
                                OutputColumn(
                                    "raw_y",
                                    "A-298 K_F270",
                                    (),
                                ),
                                OutputColumn(
                                    "norm_y",
                                    "A-298 K_F270_Norm",
                                    (),
                                    method="Divided by Max of B",
                                    formula="col(B)/max(col(B))",
                                ),
                            ),
                        ),
                    ),
                ),
            ),
            incomplete_folders=(),
        )
        reconciliation = CountReconciliation(1, 0, 0, 1, 1, 3)

        summary = build_final_output_plan_summary(plan, reconciliation)

        self.assertIn("Book：A", summary.message)
        self.assertIn("列 1 [X] · Comment=Em", summary.message)
        self.assertIn(
            "列 2 [原始 Y] · Comment=A-298 K_F270",
            summary.message,
        )
        self.assertIn(
            "列 3 [归一化 Y] · Comment=A-298 K_F270_Norm"
            " · Method=Divided by Max of B"
            " · F(x)=col(B)/max(col(B))",
            summary.message,
        )

    def test_final_output_plan_summary_keeps_excitation_fallback_neutral(self):
        plan = OutputPlan(
            folders=(
                OutputFolder(
                    "F_Em315_ExSlit2_EmSlit2",
                    "F",
                    True,
                    (OutputBook("A", ()),),
                ),
            ),
            incomplete_folders=(),
        )
        reconciliation = CountReconciliation(
            recognizable_book_count=1,
            rejected_book_count=0,
            excluded_book_count=0,
            accepted_ordinary_spectrum_count=1,
            output_plan_spectrum_count=1,
            output_plan_column_count=3,
        )

        summary = build_final_output_plan_summary(plan, reconciliation)

        self.assertEqual(0, summary.complete_folder_count)
        self.assertEqual(0, summary.incomplete_folder_count)
        self.assertIn(
            "不参与完整性：F_Em315_ExSlit2_EmSlit2",
            summary.message,
        )

    def test_final_output_plan_summary_preserves_each_missing_label_verbatim(self):
        plan = OutputPlan(
            folders=(
                OutputFolder(
                    "F_incomplete",
                    "F",
                    False,
                    (OutputBook("A", ()),),
                ),
            ),
            incomplete_folders=(
                IncompleteFolder(
                    "F_incomplete",
                    ("Present-298 K",),
                    (
                        "Sample, Alpha-298 K",
                        "Name: missing literal-77 K",
                    ),
                ),
            ),
        )

        summary = build_final_output_plan_summary(
            plan,
            CountReconciliation(1, 0, 0, 1, 1, 0),
        )

        self.assertIn("缺少：Sample, Alpha-298 K", summary.message)
        self.assertIn("缺少：Name: missing literal-77 K", summary.message)
        self.assertNotIn("Sample、Alpha-298 K", summary.message)
        self.assertNotIn("Name，缺少 literal-77 K", summary.message)

    def test_success_report_lists_fallback_neutrally_without_counting_it_as_complete(self):
        fallback_name = "F_Em315_ExSlit2_EmSlit2"
        data = ReportData(
            output_path=pathlib.Path("out"),
            ignored_duplicate_input_paths=(),
            rejections=(),
            exclusions=(),
            warnings=(),
            special_groups=(),
            final_attributions=(),
            output_plan=OutputPlan(
                folders=(
                    OutputFolder(
                        fallback_name,
                        "F",
                        True,
                        (OutputBook("A", ()),),
                    ),
                ),
                incomplete_folders=(),
            ),
        )

        report = build_success_report(data)
        summary = build_completion_summary(
            data,
            report_path=pathlib.Path("report.txt"),
        )

        self.assertIn("仅激发谱 Folder", report)
        self.assertIn(f"- {fallback_name}", report)
        self.assertIn(f"Folder：{fallback_name}；Book：A", report)
        self.assertEqual(0, summary.complete_folder_count)
        self.assertEqual(0, summary.incomplete_folder_count)

    def test_success_report_is_chinese_and_carries_the_complete_audit_schema(self):
        report = build_success_report(
            ReportData(
                output_path=pathlib.Path(r"C:\out\Organized_Origin_Data_20260629_123456"),
                ignored_duplicate_input_paths=(pathlib.Path(r"C:\raw\dup.opj"),),
                rejections=(ReportItem("bad.opj", "invalid objective"),),
                exclusions=(ReportItem("skip.opj", "user excluded"),),
                warnings=("missing excitation for F sample A-298 K",),
                special_groups=(SpecialGroupSummary("steady_2d", ("S1|2D|Book",)),),
                final_attributions=(SampleAttribution("A-298 K", pathlib.Path(r"C:\raw\a.opj"), "accepted"),),
                output_plan=_plan_for_ordering(),
                input_paths=(pathlib.Path(r"C:\raw\a.opj"),),
                settings=(ReportItem("S1 强度上限", "2000000"),),
                manual_selections=(ReportItem("激发谱选择", "保留 Book A"),),
                source_fingerprints=(ReportItem("a.opj", "SHA-256=abc；副本一致"),),
                count_reconciliation=(ReportItem("识别", "4；接受 2"),),
                errors=(ReportItem("非致命错误", "无"),),
            )
        )

        section_order = (
            "输入路径",
            "输出路径",
            "本次设置",
            "忽略的重复输入路径",
            "拒绝",
            "排除",
            "错误",
            "警告",
            "人工选择",
            "特殊谱组",
            "样品归属",
            "源文件指纹",
            "数量核对",
            "输出 Folder/Book 映射",
            "齐全 Folder",
            "不齐全 Folder",
            "仅激发谱 Folder",
        )
        positions = [report.index(title) for title in section_order]
        self.assertEqual(sorted(positions), positions)
        self.assertIn(r"C:\raw\a.opj", report)
        self.assertIn("S1 强度上限：2000000", report)
        self.assertIn("激发谱选择：保留 Book A", report)
        self.assertIn("a.opj：SHA-256=abc；副本一致", report)
        self.assertIn("识别：4；接受 2", report)
        self.assertIn("非致命错误：无", report)
        self.assertIn("二维稳态谱：S1|2D|Book", report)
        self.assertIn(
            r"A-298 K：C:\raw\a.opj（已接受）",
            report,
        )
        self.assertNotIn("steady_2d", report)
        self.assertNotIn("accepted", report)
        self.assertIn("F_complete_ALL_SAMPLES", report)
        self.assertIn("缺少样品状态：A-77 K", report)
        self.assertIn("缺少样品状态：B-77 K", report)
        complete_section = report.index("齐全 Folder")
        incomplete_section = report.index("不齐全 Folder")
        self.assertGreater(
            report.index("F_complete_ALL_SAMPLES", complete_section),
            complete_section,
        )
        self.assertGreater(
            report.index("F_incomplete", incomplete_section),
            incomplete_section,
        )
        for english_title in (
            "Output Path",
            "Rejections",
            "Exclusions",
            "Warnings",
            "Complete Sample Folders",
        ):
            self.assertNotIn(english_title, report)

    def test_success_report_is_utf8_text_without_missing_reason_trace(self):
        report = build_success_report(
            ReportData(
                output_path=pathlib.Path("out"),
                ignored_duplicate_input_paths=(),
                rejections=(ReportItem("sample-\u00b5.opj", "bad objective"),),
                exclusions=(),
                warnings=("missing emission for P sample P-\u00b5-77 K",),
                special_groups=(),
                final_attributions=(),
                output_plan=OutputPlan((), ()),
            )
        )

        encoded = report.encode("utf-8")
        self.assertIn("sample-\u00b5.opj".encode("utf-8"), encoded)
        self.assertNotIn("missing reason", report.casefold())
        self.assertNotIn("reason trace", report.casefold())

    def test_paired_spectrum_warnings_are_aggregated_by_family_and_canonical_label(self):
        plan = OutputPlan(
            folders=(
                OutputFolder(
                    "F_Ex270_ExSlit2_EmSlit2",
                    "F",
                    False,
                    (
                        OutputBook(
                            "A",
                            (
                                OutputColumn("x", "Em", ()),
                                OutputColumn("raw_y", "A-298 K_F270", ()),
                                OutputColumn("raw_y", "A-298 K_F275", ()),
                            ),
                        ),
                    ),
                ),
                OutputFolder(
                    "F_Ex280_ExSlit2_EmSlit2",
                    "F",
                    False,
                    (
                        OutputBook(
                            "A",
                            (
                                OutputColumn("x", "Em", ()),
                                OutputColumn("raw_y", "A-298 K_F280", ()),
                            ),
                        ),
                    ),
                ),
                OutputFolder(
                    "P_Ex270_ExSlit2_EmSlit2_FD0.1_SW1_TPF0.1_FC100",
                    "P",
                    False,
                    (
                        OutputBook(
                            "P",
                            (
                                OutputColumn("x", "Em", ()),
                                OutputColumn("raw_y", "P-77 K_P270", ()),
                                OutputColumn("x", "Ex", ()),
                                OutputColumn("raw_y", "P-77 K_PEx315", ()),
                            ),
                        ),
                    ),
                ),
                OutputFolder(
                    "P_Em330_ExSlit2_EmSlit2_FD0.1_SW1_TPF0.1_FC100",
                    "P",
                    True,
                    (
                        OutputBook(
                            "P",
                            (
                                OutputColumn("x", "Ex", ()),
                                OutputColumn("raw_y", "P-77 K_PEx330", ()),
                                OutputColumn("raw_y", "P-77 K_PEx340", ()),
                            ),
                        ),
                    ),
                ),
            ),
            incomplete_folders=(),
        )

        warnings = paired_spectrum_warnings(plan)

        self.assertEqual(
            (
                "F 样品 A-298 K 缺少配套激发谱。",
                "P 样品 P-77 K 缺少配套发射谱。",
            ),
            warnings,
        )

    def test_report_never_receives_distinct_identities_with_one_book_long_name(self):
        shared = {
            "canonical_sample_label": "A-B-C-1 M-298 K",
            "sample_system_label": "A-B-C-1 M",
            "temperature": "298 K",
            "excitation_slit": ("2", "2"),
            "emission_slit": ("2", "2"),
        }
        with self.assertRaisesRegex(
            ValueError,
            "Book Long Name.*multiple sample system identities",
        ):
            build_output_plan(
                (
                    OutputSpectrum(
                        "first-emission",
                        SpectrumClass.STEADY_EMISSION,
                        key_wavelength="270",
                        x_y=((500, 1),),
                        sample_system_identity='{"sample":"A-B","solvent":"C"}',
                        **shared,
                    ),
                    OutputSpectrum(
                        "second-emission",
                        SpectrumClass.STEADY_EMISSION,
                        key_wavelength="270",
                        x_y=((500, 1),),
                        sample_system_identity='{"sample":"A","solvent":"B-C"}',
                        **shared,
                    ),
                    OutputSpectrum(
                        "second-excitation",
                        SpectrumClass.STEADY_EXCITATION,
                        key_wavelength="315",
                        x_y=((300, 1),),
                        sample_system_identity='{"sample":"A","solvent":"B-C"}',
                        **shared,
                    ),
                )
            )

    def test_success_report_includes_aggregated_paired_spectrum_warnings(self):
        plan = OutputPlan(
            folders=(
                OutputFolder(
                    "F_Ex270_ExSlit2_EmSlit2",
                    "F",
                    False,
                    (
                        OutputBook(
                            "A",
                            (
                                OutputColumn("x", "Em", ()),
                                OutputColumn("raw_y", "A-298 K_F270", ()),
                            ),
                        ),
                    ),
                ),
            ),
            incomplete_folders=(),
        )

        report = build_success_report(
            ReportData(
                output_path=pathlib.Path("out"),
                ignored_duplicate_input_paths=(),
                rejections=(),
                exclusions=(),
                warnings=(),
                special_groups=(),
                final_attributions=(),
                output_plan=plan,
            )
        )

        self.assertIn("F 样品 A-298 K 缺少配套激发谱。", report)

    def test_completion_summary_has_counts_and_report_pointer_without_detailed_lists(self):
        summary = build_completion_summary(
            ReportData(
                output_path=pathlib.Path(r"C:\out\Organized_Origin_Data_20260629_123456"),
                ignored_duplicate_input_paths=(pathlib.Path("dup1.opj"), pathlib.Path("dup2.opj")),
                rejections=(ReportItem("bad.opj", "invalid"),),
                exclusions=(ReportItem("skip.opj", "manual"),),
                warnings=("w1", "w2"),
                special_groups=(SpecialGroupSummary("steady_2d", ("a", "b")),),
                final_attributions=(SampleAttribution("A", pathlib.Path("a.opj"), "accepted"),),
                output_plan=_plan_for_ordering(),
            ),
            report_path=pathlib.Path(r"C:\out\Organized_Origin_Data_20260629_123456\Run_Report_20260629_123456.txt"),
        )

        self.assertEqual(1, summary.complete_folder_count)
        self.assertEqual(1, summary.incomplete_folder_count)
        self.assertEqual(2, summary.warning_count)
        self.assertEqual(2, summary.ignored_duplicate_count)
        self.assertIn("输出已创建", summary.message)
        self.assertIn("拒绝：1；排除：1；错误：0；警告：2", summary.message)
        self.assertIn("运行报告", summary.message)
        self.assertIn("Run_Report_20260629_123456.txt", summary.message)
        self.assertNotIn("F_complete_ALL_SAMPLES", summary.message)
        self.assertNotIn("F_incomplete", summary.message)


def _plan_for_ordering():
    return OutputPlan(
        folders=(
            OutputFolder("F_incomplete", "F", False, (OutputBook("A", ()),)),
            OutputFolder("F_complete_ALL_SAMPLES", "F", False, (OutputBook("B", ()),)),
        ),
        incomplete_folders=(IncompleteFolder("F_incomplete", ("C-77 K",), ("A-77 K", "B-77 K")),),
    )


if __name__ == "__main__":
    unittest.main()
