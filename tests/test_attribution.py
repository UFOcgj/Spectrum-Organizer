from contextlib import closing
import gc
import pathlib
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from spectrum_organizer.core.attribution import (
    AttributionBook,
    AttributionCache,
    AttributionFields,
    AttributionSession,
    build_attribution_targets,
    build_attribution_fields,
    commit_final_attributions,
    infer_oxygen_environment,
    reconcile_concentration_prefill,
    reconcile_oxygen_environment_prefill,
    reconcile_temperature_prefill,
    split_folder_target,
)
from spectrum_organizer.domain.models import DopedSample, LiquidSample, NeatSample
from spectrum_organizer.domain.normalization import ConcentrationError
from spectrum_organizer.safety.name_policy import NamePolicyError
from spectrum_organizer.store.sample_library import SampleLibrary


class WorkspaceTempDir:
    def __init__(self):
        self.path = pathlib.Path(tempfile.mkdtemp(prefix="spectrum-organizer-attribution-"))

    def __enter__(self):
        return self.path

    def __exit__(self, exc_type, exc, tb):
        for attempt in range(4):
            try:
                shutil.rmtree(self.path)
                break
            except FileNotFoundError:
                break
            except OSError:
                if attempt == 3:
                    raise
                gc.collect()
                time.sleep(0.05)
        if self.path.exists():
            raise AssertionError(f"test cleanup retained case directory: {self.path}")


class AttributionTests(unittest.TestCase):
    def test_ordinary_folder_has_one_target_for_all_surviving_books_and_skips_invalid_only_folder(self):
        books = [
            _book("S1", "MFL-298K", "PE1"),
            _book("S1", "MFL-298K", "PE2"),
            _book("S1", "bad-folder", "Bad", valid=False),
        ]

        targets = build_attribution_targets(books)

        self.assertEqual(1, len(targets))
        self.assertEqual("folder", targets[0].scope)
        self.assertEqual(
            ('["S1","worksheet","MFL-298K","PE1"]', '["S1","worksheet","MFL-298K","PE2"]'),
            targets[0].book_keys,
        )
        self.assertEqual("MFL-298K", targets[0].folder_path)
        self.assertEqual({"temperature": "298 K"}, targets[0].prefill)
        self.assertFalse(targets[0].confirmed)

    def test_mixed_folder_and_root_books_are_attributed_per_book(self):
        targets = build_attribution_targets([
            _book("S1", "Mixed", "PE1", mixed=True),
            _book("S1", "Mixed", "PE2", mixed=True),
            _book("S1", "", "RootPE"),
            _book("S1", "/", "CanonicalRootPE"),
        ])

        self.assertEqual(["book", "book", "book", "book"], [target.scope for target in targets])
        self.assertEqual(
            [
                ('["S1","worksheet","","RootPE"]',),
                ('["S1","worksheet","/","CanonicalRootPE"]',),
                ('["S1","worksheet","Mixed","PE1"]',),
                ('["S1","worksheet","Mixed","PE2"]',),
            ],
            [target.book_keys for target in targets],
        )
        self.assertEqual([None, None, None, None], [target.cache_key for target in targets])

    def test_task_local_same_folder_reuse_is_case_sensitive_and_keeps_most_recent_confirmation(self):
        cache = AttributionCache()
        first = AttributionFields(sample=NeatSample("MFL", "film", "298 K"))
        latest = AttributionFields(sample=NeatSample("PFL", "film", "298 K"))

        cache.remember("M F-L_298K", first)
        self.assertEqual(first, cache.lookup("MFL298K"))
        self.assertIsNone(cache.lookup("mfl298K"))

        cache.remember("MFL298K", latest)
        self.assertEqual(latest, cache.lookup("M_F-L 298K"))
        self.assertIsNone(AttributionCache().lookup("MFL298K"))

        cache.remember("Parent/Folder_RT", first)
        self.assertEqual(first, cache.lookup("Other Parent/Folder-RT"))

    def test_origin_style_trailing_separator_uses_final_folder_name_for_cache_and_prefill(self):
        cache = AttributionCache()
        mfl = AttributionFields(sample=NeatSample("MFL", "film", "298 K"))

        cache.remember("/RawProject/MFL_RT/", mfl)

        self.assertEqual(mfl, cache.lookup("/OtherProject/MFL-RT/"))
        self.assertIsNone(cache.lookup("/RawProject/PFL_77K/"))
        targets = build_attribution_targets([
            _book("S1", "/RawProject/MFL_RT/", "PE1"),
            _book("S1", "/RawProject/PFL_77K/", "PE2"),
        ])
        self.assertEqual(
            [{"temperature": "298 K"}, {"temperature": "77 K"}],
            [dict(target.prefill) for target in targets],
        )

    def test_mixed_folder_can_apply_current_attribution_to_remaining_unconfirmed_books(self):
        session = AttributionSession(build_attribution_targets([
            _book("S1", "Mixed", "PE1", mixed=True),
            _book("S1", "Mixed", "PE2", mixed=True),
        ]))
        attribution = AttributionFields(sample=NeatSample("MFL", "film", "298 K"))

        first_key, second_key = (target.book_keys[0] for target in session.targets)
        session.confirm(first_key, attribution, apply_to_remaining_folder=True)

        self.assertEqual(attribution, session.assignment_for(first_key))
        self.assertEqual(attribution, session.assignment_for(second_key))

    def test_mixed_folder_apply_to_remaining_stays_within_same_source(self):
        session = AttributionSession(build_attribution_targets([
            _book("S1", "Mixed", "PE1", mixed=True),
            _book("S1", "Mixed", "PE2", mixed=True),
            _book("S2", "Mixed", "PE1", mixed=True),
        ]))
        attribution = AttributionFields(sample=NeatSample("MFL", "film", "298 K"))

        first_source_keys = [target.book_keys[0] for target in session.targets if target.source_id == "S1"]
        other_source_key = next(target.book_keys[0] for target in session.targets if target.source_id == "S2")
        session.confirm(first_source_keys[0], attribution, apply_to_remaining_folder=True)

        self.assertEqual(attribution, session.assignment_for(first_source_keys[0]))
        self.assertEqual(attribution, session.assignment_for(first_source_keys[1]))
        self.assertIsNone(session.assignment_for(other_source_key))

    def test_root_apply_to_remaining_preserves_existing_assignments_and_source_boundary(self):
        session = AttributionSession(build_attribution_targets([
            _book("S1", "", "Root1"),
            _book("S1", "/", "Root2"),
            _book("S1", "", "Root3"),
            _book("S2", "", "OtherRoot"),
        ]))
        first, second, third = [
            target.book_keys[0]
            for target in session.targets
            if target.source_id == "S1"
        ]
        other = next(target.book_keys[0] for target in session.targets if target.source_id == "S2")
        existing = AttributionFields(sample=NeatSample("PFL", "film", "77 K"))
        replacement = AttributionFields(sample=NeatSample("MFL", "film", "298 K"))

        session.confirm(second, existing)
        session.confirm(first, replacement, apply_to_remaining_folder=True)

        self.assertEqual(replacement, session.assignment_for(first))
        self.assertEqual(existing, session.assignment_for(second))
        self.assertEqual(replacement, session.assignment_for(third))
        self.assertIsNone(session.assignment_for(other))

    def test_rt_prefill_and_folder_split_keep_each_book_available_for_confirmation(self):
        target = build_attribution_targets([
            _book("S1", "Parent/MFL_RT", "PE1"),
            _book("S1", "Parent/MFL_RT", "PE2"),
        ])[0]

        split = split_folder_target(target)

        self.assertEqual({"temperature": "298 K"}, target.prefill)
        self.assertEqual(["book", "book"], [item.scope for item in split])
        self.assertEqual(target.book_keys, tuple(item.book_keys[0] for item in split))

    def test_temperature_prefill_uses_only_the_final_folder_name(self):
        target = build_attribution_targets([
            _book("S1", "Archive_RT/Sample_77K", "PE1"),
        ])[0]

        self.assertEqual({"temperature": "77 K"}, target.prefill)

    def test_folder_temperature_prefill_accepts_explicit_room_temperature_aliases(self):
        for folder_name in (
            "Sample_room_temp",
            "Sample-room-temperature",
            "Sample_RoomTemp",
            "样品_室温",
        ):
            with self.subTest(folder_name=folder_name):
                target = build_attribution_targets([
                    _book("S1", folder_name, "PE1"),
                ])[0]
                self.assertEqual({"temperature": "298 K"}, target.prefill)

    def test_folder_prefill_recognizes_scientific_concentration_with_molar_unit(self):
        target = build_attribution_targets([
            _book("S1", "PFL_10^-5M", "PE1"),
        ])[0]

        self.assertEqual({"solution_concentration": "1×10^-5"}, target.prefill)

    def test_folder_prefill_recognizes_general_typed_concentration_syntax(self):
        cases = (
            ("AH0.1mM+ANH2", {"solution_concentration": "1×10^-4"}),
            ("PFL-10^-7M_RT", {"temperature": "298 K", "solution_concentration": "1×10^-7"}),
            ("PFL1e-5M", {"solution_concentration": "1×10^-5"}),
            ("PFL10**-5M", {"solution_concentration": "1×10^-5"}),
            ("NDI10µM-film", {"solution_concentration": "1×10^-5"}),
            ("Dye10µM-film", {"solution_concentration": "1×10^-5"}),
            ("Complex10µM", {"solution_concentration": "1×10^-5"}),
            ("Base1e-5M", {"solution_concentration": "1×10^-5"}),
            ("Sample10nM", {"solution_concentration": "1×10^-8"}),
            ("PFL_10⁻⁵M", {"solution_concentration": "1×10^-5"}),
            (
                "Guest_5wt.%",
                {
                    "doped_concentration": "5",
                    "doped_concentration_unit": "wt%",
                },
            ),
            (
                "Guest_2mole%",
                {
                    "doped_concentration": "2",
                    "doped_concentration_unit": "mol%",
                },
            ),
        )

        for folder_name, expected in cases:
            with self.subTest(folder_name=folder_name):
                target = build_attribution_targets([
                    _book("S1", folder_name, "PE1"),
                ])[0]
                self.assertEqual(expected, target.prefill)

    def test_folder_prefill_uses_solution_default_for_conflicting_molar_concentrations(self):
        target = build_attribution_targets([
            _book("S1", "PFL_10^-5M_10^-4M", "PE1"),
        ])[0]

        self.assertEqual(
            {"solution_concentration": "1×10^-4"},
            target.prefill,
        )

    def test_folder_prefill_uses_solution_default_for_malformed_molar_evidence(self):
        for folder_name in (
            "PFL_0M",
            "PFL2 x 10M",
            "PFL2e 10^-5M",
            "sample_1ee5M",
            "sample_1xx5M",
        ):
            with self.subTest(folder_name=folder_name):
                target = build_attribution_targets([
                    _book("S1", folder_name, "PE1"),
                ])[0]
                self.assertEqual(
                    {"solution_concentration": "1×10^-4"},
                    target.prefill,
                )

    def test_concentration_prefill_handles_malformed_current_evidence_by_sample_type(self):
        solution = reconcile_concentration_prefill(
            {
                "sample_type": "solution",
                "concentration": "1×10^-6",
            },
            "PFL_0M.opj",
        )
        doped = reconcile_concentration_prefill(
            {
                "sample_type": "doped",
                "concentration": "5",
                "concentration_unit": "wt%",
            },
            "Guest_101wt%.opj",
        )

        self.assertEqual("1×10^-4", solution["solution_concentration"])
        self.assertEqual("1×10^-4", solution["concentration"])
        self.assertNotIn("doped_concentration", doped)
        self.assertNotIn("doped_concentration_unit", doped)
        self.assertNotIn("concentration", doped)
        self.assertNotIn("concentration_unit", doped)

    def test_concentration_prefill_uses_specificity_then_task_local_reuse(self):
        per_book = reconcile_concentration_prefill(
            {
                "sample_type": "solution",
                "concentration": "1×10^-6",
            },
            "batch_1e-5M.opj",
            "Sample_0.1mM",
            book_name="Book_10uM",
        )
        folder = reconcile_concentration_prefill(
            {},
            "batch_1e-5M.opj",
            "Sample_0.1mM",
        )
        reused = reconcile_concentration_prefill(
            {
                "sample_type": "solution",
                "concentration": "1×10^-6",
            },
            "batch.opj",
            "Sample",
        )

        self.assertEqual("1×10^-5", per_book["solution_concentration"])
        self.assertEqual("1×10^-5", per_book["concentration"])
        self.assertEqual("1×10^-4", folder["solution_concentration"])
        self.assertEqual("1×10^-6", reused["solution_concentration"])
        self.assertEqual("1×10^-6", reused["concentration"])

    def test_concentration_prefill_defaults_only_solution_when_no_evidence_exists(self):
        prefill = reconcile_concentration_prefill(
            {},
            "batch.opj",
            "Sample",
        )

        self.assertEqual("1×10^-4", prefill["solution_concentration"])
        self.assertNotIn("doped_concentration", prefill)
        self.assertNotIn("doped_concentration_unit", prefill)

    def test_temperature_prefill_uses_source_filename_when_folder_has_no_temperature(self):
        self.assertEqual(
            {"temperature": "77 K"},
            reconcile_temperature_prefill(
                {},
                "20240923_TMeFL_77K.opj",
                "DiMeFL_DCM",
            ),
        )

    def test_temperature_prefill_prefers_specific_folder_over_source_filename(self):
        for folder_path in ("DFL_77K", "MFL_77K"):
            with self.subTest(folder_path=folder_path):
                self.assertEqual(
                    {"temperature": "77 K"},
                    reconcile_temperature_prefill(
                        {},
                        "20250412_MFL-mTHF_RT.opj",
                        folder_path,
                    ),
                )

    def test_temperature_prefill_prefers_specific_folder_over_source_and_reuse(self):
        self.assertEqual(
            {"temperature": "298 K"},
            reconcile_temperature_prefill(
                {"temperature": "298 K"},
                "20240923_TMeFL_77K.opj",
                "DiMeFL_RT",
            ),
        )

    def test_temperature_prefill_recognizes_explicit_supported_forms(self):
        cases = (
            ("sample_RT.opj", "298 K"),
            ("sample_4K.opj", "4 K"),
            ("sample10 K.opj", "10 K"),
            ("sample77.5K.opj", "77.5 K"),
        )

        for source_filename, expected in cases:
            with self.subTest(source_filename=source_filename):
                self.assertEqual(
                    {"temperature": expected},
                    reconcile_temperature_prefill({}, source_filename),
                )

    def test_temperature_prefill_rejects_embedded_rt_and_bare_numbers(self):
        for source_filename in ("START.opj", "sample_77.opj"):
            with self.subTest(source_filename=source_filename):
                self.assertEqual(
                    {},
                    reconcile_temperature_prefill({}, source_filename),
                )

    def test_temperature_prefill_clears_reuse_for_malformed_explicit_temperature(self):
        for source_filename in (
            "sample_0K.opj",
            "-77K.opj",
            "sample_1e3K.opj",
            "sample_10^3K.opj",
            "sample_10**3K.opj",
            "sample_2x10^3K.opj",
            "sample_2×10^3K.opj",
            "sample_10³K.opj",
            "sample_1e⁻3K.opj",
            "sample_1ee3K.opj",
            "sample_10***3K.opj",
            "sample_10^^3K.opj",
            "sample_1e--3K.opj",
            "sample_1e+-3K.opj",
            "sample_10^--3K.opj",
            "sample_10^+-3K.opj",
            "sample_10⁻⁻3K.opj",
            "sample_10⁺⁺3K.opj",
            "sample_10⁻⁺3K.opj",
            "sample_10⁺⁻3K.opj",
            "sample_-77K.opj",
            "sample--77K.opj",
            "sample(-77K.opj",
            "sample_25C.opj",
            "sample_77F.opj",
            "sample_25°C.opj",
            "sample_77°F.opj",
            "sample_−77K.opj",
        ):
            with self.subTest(source_filename=source_filename):
                self.assertEqual(
                    {},
                    reconcile_temperature_prefill(
                        {"temperature": "RT"},
                        source_filename,
                    ),
                )

    def test_temperature_prefill_accepts_hyphen_separated_positive_kelvin(self):
        for source_filename in ("MFL-77K.opj", "sample2-77K.opj", "样品-77K.opj"):
            with self.subTest(source_filename=source_filename):
                self.assertEqual(
                    {"temperature": "77 K"},
                    reconcile_temperature_prefill({}, source_filename),
                )

    def test_temperature_prefill_ignores_parent_directories_and_parent_folders(self):
        self.assertEqual(
            {},
            reconcile_temperature_prefill(
                {},
                r"C:\archive_4K\sample.opj",
                "Archive_RT/Sample",
            ),
        )

    def test_temperature_prefill_current_evidence_outranks_task_local_reuse(self):
        self.assertEqual(
            {"temperature": "77 K"},
            reconcile_temperature_prefill(
                {"temperature": "298 K"},
                "sample_77K.opj",
            ),
        )

    def test_temperature_prefill_keeps_reuse_only_when_current_evidence_is_absent(self):
        self.assertEqual(
            {"temperature": "298 K"},
            reconcile_temperature_prefill(
                {"temperature": "RT"},
                "sample.opj",
                "Folder",
            ),
        )

    def test_temperature_prefill_uses_book_only_when_book_name_is_supplied(self):
        self.assertEqual(
            {},
            reconcile_temperature_prefill({}, "sample.opj", "Folder"),
        )
        self.assertEqual(
            {"temperature": "77 K"},
            reconcile_temperature_prefill(
                {},
                "sample.opj",
                "Folder",
                book_name="Book_77K",
            ),
        )

    def test_temperature_prefill_clears_conflict_inside_one_label(self):
        self.assertEqual(
            {},
            reconcile_temperature_prefill(
                {"temperature": "298 K"},
                "sample_77K_RT.opj",
            ),
        )

    def test_session_folder_split_replaces_original_target_before_confirmation(self):
        target = build_attribution_targets([
            _book("S1", "Mixed", "PE1"),
            _book("S1", "Mixed", "PE2"),
        ])[0]
        session = AttributionSession([target])

        split = session.split_folder(target)
        first = AttributionFields(sample=NeatSample("MFL", "Solid", "298 K"))
        second = AttributionFields(sample=NeatSample("PFL", "Solid", "298 K"))
        session.confirm(split[0].book_keys[0], first)
        session.confirm(split[1].book_keys[0], second)

        self.assertEqual(split, session.targets)
        self.assertEqual(first, session.assignment_for(split[0].book_keys[0]))
        self.assertEqual(second, session.assignment_for(split[1].book_keys[0]))

    def test_reopen_expands_folder_scope_but_keeps_split_book_scope_local(self):
        folder_target = build_attribution_targets([
            _book("S1", "Folder", "PE1"),
            _book("S1", "Folder", "PE2"),
        ])[0]
        folder_session = AttributionSession([folder_target])
        attribution = AttributionFields(sample=NeatSample("MFL", "Solid", "298 K"))
        folder_session.confirm(folder_target.book_keys[0], attribution)

        reopened, previous = folder_session.reopen((folder_target.book_keys[0],))

        self.assertEqual(folder_target.book_keys, reopened)
        self.assertEqual(set(folder_target.book_keys), set(previous))
        self.assertEqual({}, folder_session.assignments)

        split_session = AttributionSession([folder_target])
        split_targets = split_session.split_folder(folder_target)
        for target in split_targets:
            split_session.confirm(target.book_keys[0], attribution)

        reopened, previous = split_session.reopen((split_targets[0].book_keys[0],))

        self.assertEqual(split_targets[0].book_keys, reopened)
        self.assertEqual(split_targets[0].book_keys, tuple(previous))
        self.assertIsNone(split_session.assignment_for(split_targets[0].book_keys[0]))
        self.assertEqual(
            attribution,
            split_session.assignment_for(split_targets[1].book_keys[0]),
        )

    def test_replace_assignments_updates_only_the_explicit_confirmed_scope(self):
        folder_target = build_attribution_targets([
            _book("S1", "Folder", "PE1"),
            _book("S1", "Folder", "PE2"),
        ])[0]
        session = AttributionSession([folder_target])
        original = AttributionFields(
            sample=NeatSample("MFL", "Solid", "298 K")
        )
        book_update = AttributionFields(
            sample=NeatSample("PFL", "Solid", "298 K")
        )
        folder_update = AttributionFields(
            sample=NeatSample("TFL", "Solid", "77 K")
        )
        session.confirm(folder_target.book_keys[0], original)

        session.replace_assignments(
            (folder_target.book_keys[0],),
            book_update,
            scope="book",
        )

        self.assertEqual(
            book_update,
            session.assignment_for(folder_target.book_keys[0]),
        )
        self.assertEqual(
            original,
            session.assignment_for(folder_target.book_keys[1]),
        )
        self.assertEqual(
            "book",
            session.confirmed_scope_for(folder_target.book_keys[0]),
        )
        self.assertEqual(
            "folder",
            session.confirmed_scope_for(folder_target.book_keys[1]),
        )

        session.replace_assignments(
            folder_target.book_keys,
            folder_update,
            scope="folder",
        )

        self.assertEqual(
            {key: folder_update for key in folder_target.book_keys},
            session.assignments,
        )
        self.assertEqual(
            {"folder"},
            {
                session.confirmed_scope_for(key)
                for key in folder_target.book_keys
            },
        )

    def test_sample_form_normalizes_all_three_supported_types(self):
        solution = build_attribution_fields(
            "solution",
            {"sample": "MFL", "solvent": "mTHF", "concentration": "10⁻⁴", "temperature": "RT"},
        )
        solid = build_attribution_fields(
            "solid",
            {
                "sample": "MFL",
                "state": "Crystal",
                "oxygen_environment": "DeO2",
                "temperature": "77K",
            },
        )
        doped = build_attribution_fields(
            "doped",
            {
                "sample": "MFL",
                "host": "PVA",
                "concentration": "0",
                "concentration_unit": "wt%",
                "state": "Film",
                "oxygen_environment": "Air",
                "temperature": "298",
            },
        )

        self.assertEqual(LiquidSample("MFL", "mTHF", "1×10^-4 M", "298 K"), solution.sample)
        self.assertEqual(NeatSample("MFL", "Crystal", "77 K", "DeO2"), solid.sample)
        self.assertEqual(DopedSample("MFL", "PVA", "0 wt%", "Film", "298 K", "Air"), doped.sample)

    def test_solid_and_doped_environment_is_required_and_part_of_exact_identity(self):
        solid = build_attribution_fields(
            "solid",
            {
                "sample": "NDI",
                "state": "Solid",
                "oxygen_environment": "DeO2",
                "temperature": "77 K",
            },
        )
        doped = build_attribution_fields(
            "doped",
            {
                "sample": "NDI",
                "host": "mCP",
                "concentration": "10",
                "concentration_unit": "wt%",
                "state": "Film",
                "oxygen_environment": "Air",
                "temperature": "298 K",
            },
        )

        self.assertEqual("NDI-Solid-DeO2-77 K", solid.sample.canonical_label)
        self.assertEqual("NDI-Solid-DeO2", solid.sample.system_label)
        self.assertIn('"oxygen_environment":"DeO2"', solid.sample.identity_json())
        self.assertEqual("NDI-in-mCP-10 wt%-Film-Air-298 K", doped.sample.canonical_label)
        self.assertEqual("NDI-in-mCP-10 wt%-Film-Air", doped.sample.system_label)
        self.assertIn('"oxygen_environment":"Air"', doped.sample.identity_json())
        with self.assertRaisesRegex(ValueError, "oxygen_environment"):
            build_attribution_fields(
                "solid",
                {"sample": "NDI", "state": "Solid", "temperature": "77 K"},
            )

    def test_oxygen_environment_inference_is_boundary_aware_and_conflict_safe(self):
        air_cases = ("sample_air_77K", "AIR-film", "空气中")
        deo2_cases = (
            "sample-vacuum-77K",
            "sample_vac_77K",
            "sample-DeO2-77K",
            "sample_de-o2_77K",
            "sample-deoxygenated",
            "sample_degassed",
            "sample_deaerated",
            "sample-oxygen-free",
            "sample_O2_free",
            "绝氧",
            "真空",
        )
        for text in air_cases:
            with self.subTest(text=text):
                self.assertEqual("Air", infer_oxygen_environment(text))
        for text in deo2_cases:
            with self.subTest(text=text):
                self.assertEqual("DeO2", infer_oxygen_environment(text))
        for text in ("chair", "N2", "Ar", "ordinary"):
            with self.subTest(text=text):
                self.assertEqual("", infer_oxygen_environment(text))
        self.assertEqual("", infer_oxygen_environment("sample_air", "folder_vacuum"))

    def test_oxygen_environment_prefill_keeps_agreement_and_clears_conflict(self):
        self.assertEqual(
            {"sample_type": "solid", "oxygen_environment": "Air"},
            reconcile_oxygen_environment_prefill(
                {"sample_type": "solid"},
                "source_air.opj",
                "Folder_RT",
            ),
        )
        self.assertEqual(
            {"sample_type": "solid", "oxygen_environment": "DeO2"},
            reconcile_oxygen_environment_prefill(
                {"sample_type": "solid", "oxygen_environment": "DeO2"},
                "source_vacuum.opj",
            ),
        )
        self.assertEqual(
            {"sample_type": "solid"},
            reconcile_oxygen_environment_prefill(
                {"sample_type": "solid", "oxygen_environment": "Air"},
                "source_vacuum.opj",
            ),
        )

    def test_sample_form_rejects_forbidden_origin_text(self):
        with self.assertRaises(NamePolicyError):
            build_attribution_fields(
                "solid",
                {"sample": "MFL\nPFL", "state": "Solid", "temperature": "298 K"},
            )

    def test_sample_form_rejects_doped_concentration_that_cannot_be_canonicalized(self):
        with self.assertRaises(ConcentrationError):
            build_attribution_fields(
                "doped",
                {
                    "sample": "NDI",
                    "host": "mCP",
                    "concentration": "1e-1000000",
                    "concentration_unit": "wt%",
                    "state": "Film",
                    "oxygen_environment": "Air",
                    "temperature": "298 K",
                },
            )

    def test_commit_final_attributions_deduplicates_records_and_uses_one_batch_call(self):
        library = RecordingLibrary()
        first = AttributionFields(sample=LiquidSample("MFL", "mTHF", "1x10^-4 M", "298 K"))
        second = AttributionFields(sample=LiquidSample("MFL", "mTHF", "1x10^-4 M", "298 K"))

        mapping = commit_final_attributions(
            library,
            {"book-a": first, "book-b": second},
        )

        self.assertEqual(1, library.calls)
        self.assertEqual([first.sample], library.records)
        self.assertEqual({"book-a": 101, "book-b": 101}, mapping)

    def test_commit_final_attributions_rolls_back_to_final_confirmation_on_batch_failure(self):
        library = RecordingLibrary(error=RuntimeError("locked"))

        with self.assertRaises(RuntimeError):
            commit_final_attributions(library, {"book-a": AttributionFields(sample=NeatSample("MFL", "film", "298 K"))})

        self.assertEqual(1, library.calls)

    def test_final_sqlite_transaction_rolls_back_and_then_succeeds_as_one_batch(self):
        with WorkspaceTempDir() as root:
            db = root / "sample_library.sqlite3"
            library = FailingSampleLibrary(db, root / "backups", clock=lambda: "20260627_120000")
            assignments = {
                "book-a": AttributionFields(sample=NeatSample("MFL", "film", "298 K")),
                "book-b": AttributionFields(sample=NeatSample("PFL", "film", "298 K")),
            }

            with self.assertRaises(sqlite3.DatabaseError):
                commit_final_attributions(library, assignments)
            self.assertEqual(0, _count_records(db))

            ids = commit_final_attributions(SampleLibrary(db, root / "backups", clock=lambda: "20260627_120100"), assignments)

            self.assertEqual({"book-a": 1, "book-b": 2}, ids)
            self.assertEqual(2, _count_records(db))


class RecordingLibrary:
    def __init__(self, error=None):
        self.error = error
        self.calls = 0
        self.records = None

    def save_final_records(self, records):
        self.calls += 1
        self.records = list(records)
        if self.error is not None:
            raise self.error
        return [101 + index for index, _record in enumerate(self.records)]


class FailingSampleLibrary(SampleLibrary):
    def save_final_records(self, records):
        return super().save_final_records(records, fail_after=1)


def _count_records(path):
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute("select count(*) from sample_records").fetchone()[0]


def _book(source_id, folder, short_name, valid=True, mixed=False):
    return AttributionBook(
        source_id=source_id,
        page_type="worksheet",
        folder_path=folder,
        book_name=short_name,
        valid=valid,
        mixed_folder=mixed,
    )


if __name__ == "__main__":
    unittest.main()
