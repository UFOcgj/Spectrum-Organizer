import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from spectrum_organizer.core.special_groups import (
    SpecialGroup,
    SpectrumBook,
    classify_special_groups,
    resolve_special_group_selection,
)
from spectrum_organizer.domain.models import SpectrumClass
from spectrum_organizer.ui.dialogs import (
    special_duplicate_point_review_dialog,
    special_overlap_assignment_dialog,
)


class SpecialGroupsTests(unittest.TestCase):
    def test_final_selection_rejects_unknown_special_group_kind(self):
        group = SpecialGroup("unknown", ("a",), ())

        with self.assertRaisesRegex(
            ValueError,
            "Unsupported special-group kind: unknown",
        ):
            resolve_special_group_selection(group, ("a",))

    def test_special_group_identity_includes_page_type(self):
        worksheet = _book("S1", "Delayed", "Book1", SpectrumClass.DELAYED_EMISSION)
        matrix = SpectrumBook(
            source_id=worksheet.source_id,
            folder_path=worksheet.folder_path,
            book_name=worksheet.book_name,
            spectrum_class=worksheet.spectrum_class,
            sample_label=worksheet.sample_label,
            page_type="matrix",
        )

        self.assertNotEqual(worksheet.book_key, matrix.book_key)

    def test_book_key_cannot_collide_when_identity_parts_contain_separator(self):
        left = _book("S", "F|A", "B", SpectrumClass.STEADY_2D)
        right = _book("S", "F", "A|B", SpectrumClass.STEADY_2D)

        self.assertNotEqual(left.book_key, right.book_key)

    def test_steady_2d_is_confirmed_reported_and_not_copied(self):
        result = classify_special_groups([_book("S1", "2D", "SS1", SpectrumClass.STEADY_2D)])

        self.assertEqual(1, len(result.groups))
        self.assertEqual("steady_2d", result.groups[0].kind)
        self.assertEqual(
            ('["S1","worksheet","2D","SS1"]',),
            result.groups[0].book_keys,
        )
        self.assertTrue(result.groups[0].confirmed)
        self.assertFalse(result.groups[0].copy_to_output)
        self.assertFalse(result.groups[0].adds_completeness_label)
        self.assertEqual((), result.regular_delayed_book_keys)

    def test_delayed_2d_requires_one_scope_sample_label_and_receiving_range(self):
        good = [_delayed_emission(f"B{i}", excitation=300 + i * 5) for i in range(5)]
        wrong_scope = _delayed_emission("OtherFolder", excitation=330, folder="Other")
        wrong_sample = _delayed_emission("OtherSample", excitation=335, sample="PFL-film-298 K")
        wrong_range = _delayed_emission("OtherRange", excitation=340, receiving_range=("400", "700"))

        result = classify_special_groups(good + [wrong_scope, wrong_sample, wrong_range])

        self.assertEqual(1, len(result.groups))
        self.assertEqual("delayed_2d", result.groups[0].kind)
        self.assertEqual(tuple(book.book_key for book in good), result.groups[0].book_keys)
        self.assertEqual(
            (wrong_scope.book_key, wrong_sample.book_key, wrong_range.book_key),
            result.regular_delayed_book_keys,
        )

    def test_delayed_2d_accepts_clustered_sequences_and_rejects_irregular_break(self):
        accepted = classify_special_groups([
            _delayed_emission(f"A{w}", excitation=w)
            for w in (300, 305, 310, 320, 325, 330)
        ])
        second_accepted = classify_special_groups([
            _delayed_emission(f"C{w}", excitation=w)
            for w in (400, 410, 420, 430, 435, 440)
        ])
        rejected = classify_special_groups([
            _delayed_emission(f"R{w}", excitation=w)
            for w in (300, 310, 360, 370, 380)
        ])

        self.assertEqual("delayed_2d", accepted.groups[0].kind)
        self.assertEqual("delayed_2d", second_accepted.groups[0].kind)
        self.assertEqual((), rejected.groups)
        self.assertEqual(5, len(rejected.regular_delayed_book_keys))
        self.assertEqual(1, rejected.final_validation_runs)

    def test_delayed_2d_rejects_more_than_two_step_sizes_even_when_each_is_within_double(self):
        result = classify_special_groups([
            _delayed_emission(f"B{w}", excitation=w)
            for w in (300, 310, 325, 342, 362)
        ])

        self.assertEqual((), result.groups)
        self.assertEqual(5, len(result.regular_delayed_book_keys))
        self.assertEqual(1, result.final_validation_runs)

    def test_delayed_2d_requires_five_distinct_excitation_wavelengths(self):
        result = classify_special_groups([
            _delayed_emission("A", excitation=300),
            _delayed_emission("B", excitation=305),
            _delayed_emission("C", excitation=310),
            _delayed_emission("D", excitation=315),
        ])

        self.assertEqual((), result.groups)
        self.assertEqual(4, len(result.regular_delayed_book_keys))
        self.assertEqual(0, result.final_validation_runs)

    def test_delay_time_requires_monotonic_same_direction_and_fixed_difference(self):
        accepted = classify_special_groups([
            _delayed_emission("D1", excitation=300, flash_delay=0.1, time_per_flash=1.1),
            _delayed_emission("D2", excitation=300, flash_delay=0.2, time_per_flash=1.2),
            _delayed_emission("D3", excitation=300, flash_delay=0.3, time_per_flash=1.3),
        ])
        opposite_direction = classify_special_groups([
            _delayed_emission("O1", excitation=300, flash_delay=0.1, time_per_flash=1.3),
            _delayed_emission("O2", excitation=300, flash_delay=0.2, time_per_flash=1.2),
            _delayed_emission("O3", excitation=300, flash_delay=0.3, time_per_flash=1.1),
        ])
        changed_difference = classify_special_groups([
            _delayed_emission("X1", excitation=300, flash_delay=0.1, time_per_flash=1.1),
            _delayed_emission("X2", excitation=300, flash_delay=0.2, time_per_flash=1.3),
            _delayed_emission("X3", excitation=300, flash_delay=0.3, time_per_flash=1.3),
        ])
        constant_value = classify_special_groups([
            _delayed_emission("K1", excitation=300, flash_delay=0.1, time_per_flash=1.0),
            _delayed_emission("K2", excitation=300, flash_delay=0.2, time_per_flash=1.0),
            _delayed_emission("K3", excitation=300, flash_delay=0.3, time_per_flash=1.0),
        ])

        self.assertEqual("delay_time_series", accepted.groups[0].kind)
        self.assertEqual(["0.1", "0.2", "0.3"], [point[0] for point in accepted.groups[0].varying_points])
        self.assertEqual((), opposite_direction.groups)
        self.assertEqual((), changed_difference.groups)
        self.assertEqual((), constant_value.groups)

    def test_delay_time_requires_one_scope_sample_fixed_excitation_and_receiving_range(self):
        good = [
            _delayed_emission("D1", excitation=300, flash_delay=0.1, time_per_flash=1.1),
            _delayed_emission("D2", excitation=300, flash_delay=0.2, time_per_flash=1.2),
            _delayed_emission("D3", excitation=300, flash_delay=0.3, time_per_flash=1.3),
        ]
        wrong_excitation = _delayed_emission("WrongEx", excitation=310, flash_delay=0.4, time_per_flash=1.4)
        wrong_range = _delayed_emission("WrongRange", excitation=300, receiving_range=("400", "700"), flash_delay=0.4, time_per_flash=1.4)

        result = classify_special_groups(good + [wrong_excitation, wrong_range])

        self.assertEqual(1, len(result.groups))
        self.assertEqual(tuple(book.book_key for book in good), result.groups[0].book_keys)
        self.assertEqual((wrong_excitation.book_key, wrong_range.book_key), result.regular_delayed_book_keys)

    def test_duplicate_varying_axis_point_requires_review_and_keeps_one_selected_book(self):
        books = [
            _delayed_emission("B300a", excitation=300, excitation_slit="2/2", emission_slit="3/3"),
            _delayed_emission(
                "B300b",
                excitation="300.00",
                receiving_range=("3.50e2", "650.0"),
                excitation_slit="2.0/2.00",
                emission_slit="3.00/3.0",
                flash_delay="1e-1",
                sample_window="1.0",
                time_per_flash="1.10",
                flash_count="1e2",
            ),
            _delayed_emission("B305", excitation=305, excitation_slit="2/2", emission_slit="3/3"),
            _delayed_emission("B310", excitation=310, excitation_slit="2/2", emission_slit="3/3"),
            _delayed_emission("B315", excitation=315, excitation_slit="2/2", emission_slit="3/3"),
            _delayed_emission("B320", excitation=320, excitation_slit="2/2", emission_slit="3/3"),
        ]
        request = special_duplicate_point_review_dialog("delayed_2d", "300", (books[0].book_key, books[1].book_key))

        pending = classify_special_groups(books).pending_duplicate_reviews[0]
        result = classify_special_groups(
            books,
            duplicate_choices={pending.choice_key: books[1].book_key},
        )

        self.assertEqual("special_duplicate_point_review", request.kind)
        self.assertEqual(("select_one",), request.actions)
        self.assertEqual(
            tuple(book.book_key for book in books[1:]),
            result.groups[0].book_keys,
        )
        self.assertEqual((books[0].book_key,), result.regular_delayed_book_keys)
        self.assertEqual(1, result.final_validation_runs)
        time_books = [
            _delayed_emission("T1a", excitation="3e2", flash_delay="0.1", time_per_flash="1.1"),
            _delayed_emission(
                "T1b",
                excitation="300.00",
                receiving_range=("3.50e2", "650.0"),
                excitation_slit="2.0",
                emission_slit="3.00",
                flash_delay="0.10",
                sample_window="1.0",
                time_per_flash="1.10",
                flash_count="1e2",
            ),
            _delayed_emission("T2", excitation=300, flash_delay="0.2", time_per_flash="1.2"),
            _delayed_emission("T3", excitation=300, flash_delay="0.3", time_per_flash="1.3"),
        ]
        time_pending = classify_special_groups(time_books).pending_duplicate_reviews
        self.assertEqual(1, len(time_pending))
        self.assertEqual("delay_time_series", time_pending[0].kind)

    def test_delayed_2d_under_five_distinct_points_with_duplicate_stays_regular(self):
        books = [
            _delayed_emission("B300a", excitation=300),
            _delayed_emission("B300b", excitation=300),
            _delayed_emission("B305", excitation=305),
            _delayed_emission("B310", excitation=310),
            _delayed_emission("B315", excitation=315),
        ]

        result = classify_special_groups(books)

        self.assertEqual((), result.pending_duplicate_reviews)
        self.assertEqual((), result.groups)
        self.assertEqual(tuple(book.book_key for book in books), result.regular_delayed_book_keys)

    def test_unresolved_duplicate_point_is_pending_and_does_not_mask_as_regular(self):
        books = [
            _delayed_emission("B300a", excitation=300),
            _delayed_emission("B300b", excitation=300),
            _delayed_emission("B305", excitation=305),
            _delayed_emission("B310", excitation=310),
            _delayed_emission("B315", excitation=315),
            _delayed_emission("B320", excitation=320),
        ]

        result = classify_special_groups(books)

        self.assertEqual((), result.groups)
        self.assertEqual((), result.regular_delayed_book_keys)
        self.assertEqual(0, result.final_validation_runs)
        self.assertEqual(1, len(result.pending_duplicate_reviews))
        review = result.pending_duplicate_reviews[0]
        self.assertEqual("delayed_2d", review.kind)
        self.assertEqual("300", review.point_label)
        self.assertEqual(tuple(book.book_key for book in books[:2]), review.book_keys)

    def test_duplicate_reviews_with_same_kind_and_point_have_distinct_choice_keys(self):
        first_group = [
            _delayed_emission(f"A{w}{suffix}", excitation=w, source_id="S1", folder="F1")
            for w, suffix in ((300, "a"), (300, "b"), (305, ""), (310, ""), (315, ""), (320, ""))
        ]
        second_group = [
            _delayed_emission(f"B{w}{suffix}", excitation=w, source_id="S2", folder="F2")
            for w, suffix in ((300, "a"), (300, "b"), (305, ""), (310, ""), (315, ""), (320, ""))
        ]

        pending = classify_special_groups(first_group + second_group).pending_duplicate_reviews

        self.assertEqual(2, len(pending))
        self.assertEqual(2, len({review.choice_key for review in pending}))
        self.assertEqual(2, len({review.context_book_keys for review in pending}))
        resolved = classify_special_groups(
            first_group + second_group,
            duplicate_choices={
                review.choice_key: review.book_keys[0]
                for review in pending
            },
        )
        self.assertEqual((), resolved.pending_duplicate_reviews)
        self.assertEqual(2, len(resolved.groups))

    def test_duplicate_reviews_in_one_candidate_share_context_identity(self):
        books = [
            _delayed_emission(f"B{w}{suffix}", excitation=w)
            for w, suffix in (
                (300, "a"),
                (300, "b"),
                (350, ""),
                (400, ""),
                (450, "a"),
                (450, "b"),
                (500, ""),
            )
        ]

        pending = classify_special_groups(books).pending_duplicate_reviews

        self.assertEqual(["300", "450"], [review.point_label for review in pending])
        self.assertEqual(
            [tuple(book.book_key for book in books)] * 2,
            [review.context_book_keys for review in pending],
        )

    def test_overlap_assignment_is_exclusive_and_regular_choice_returns_to_regular_flow(self):
        books = [
            _delayed_emission("B300", excitation=300, flash_delay=0.1, time_per_flash=1.1),
            _delayed_emission("B305", excitation=305, flash_delay=0.1, time_per_flash=1.1),
            _delayed_emission("B310", excitation=310, flash_delay=0.1, time_per_flash=1.1),
            _delayed_emission("B315", excitation=315, flash_delay=0.1, time_per_flash=1.1),
            _delayed_emission("B320", excitation=320, flash_delay=0.1, time_per_flash=1.1),
            _delayed_emission("B300_D2", excitation=300, flash_delay=0.2, time_per_flash=1.2),
            _delayed_emission("B300_D3", excitation=300, flash_delay=0.3, time_per_flash=1.3),
        ]
        request = special_overlap_assignment_dialog(books[0].book_key)

        result = classify_special_groups(books, overlap_choices={books[0].book_key: "regular"})

        self.assertEqual("special_overlap_assignment", request.kind)
        self.assertEqual(("二维延迟谱", "时间分辨延迟谱", "regular"), request.actions)
        self.assertEqual((), result.groups)
        self.assertEqual(tuple(book.book_key for book in books), result.regular_delayed_book_keys)
        self.assertEqual(1, result.final_validation_runs)

    def test_unresolved_overlap_assignment_is_pending_and_does_not_mask_as_regular(self):
        books = [
            _delayed_emission("B300", excitation=300, flash_delay=0.1, time_per_flash=1.1),
            _delayed_emission("B305", excitation=305, flash_delay=0.1, time_per_flash=1.1),
            _delayed_emission("B310", excitation=310, flash_delay=0.1, time_per_flash=1.1),
            _delayed_emission("B315", excitation=315, flash_delay=0.1, time_per_flash=1.1),
            _delayed_emission("B320", excitation=320, flash_delay=0.1, time_per_flash=1.1),
            _delayed_emission("B300_D2", excitation=300, flash_delay=0.2, time_per_flash=1.2),
            _delayed_emission("B300_D3", excitation=300, flash_delay=0.3, time_per_flash=1.3),
        ]

        result = classify_special_groups(books)

        self.assertEqual((), result.groups)
        self.assertEqual((), result.regular_delayed_book_keys)
        self.assertEqual(0, result.final_validation_runs)
        self.assertEqual(1, len(result.pending_overlap_assignments))
        assignment = result.pending_overlap_assignments[0]
        self.assertEqual(books[0].book_key, assignment.book_key)
        self.assertEqual(("二维延迟谱", "时间分辨延迟谱", "regular"), assignment.choices)
        self.assertEqual(
            tuple(book.book_key for book in books),
            assignment.context_book_keys,
        )

    def test_cross_category_duplicate_point_cannot_split_between_two_special_groups(self):
        books = [
            _delayed_emission("A", excitation=300, flash_delay=0.1, time_per_flash=1.1),
            _delayed_emission("B", excitation=300, flash_delay=0.1, time_per_flash=1.1),
            _delayed_emission("B305", excitation=305, flash_delay=0.1, time_per_flash=1.1),
            _delayed_emission("B310", excitation=310, flash_delay=0.1, time_per_flash=1.1),
            _delayed_emission("B315", excitation=315, flash_delay=0.1, time_per_flash=1.1),
            _delayed_emission("B320", excitation=320, flash_delay=0.1, time_per_flash=1.1),
            _delayed_emission("D2", excitation=300, flash_delay=0.2, time_per_flash=1.2),
            _delayed_emission("D3", excitation=300, flash_delay=0.3, time_per_flash=1.3),
        ]
        pending = classify_special_groups(books).pending_duplicate_reviews
        delayed_2d = next(review for review in pending if review.kind == "delayed_2d")
        delay_time = next(review for review in pending if review.kind == "delay_time_series")
        duplicate_choices = {
            delayed_2d.choice_key: books[0].book_key,
            delay_time.choice_key: books[1].book_key,
        }

        overlap = classify_special_groups(
            books,
            duplicate_choices=duplicate_choices,
        )

        self.assertEqual(1, len(overlap.pending_overlap_assignments))
        self.assertEqual(
            books[0].book_key,
            overlap.pending_overlap_assignments[0].book_key,
        )
        resolved = classify_special_groups(
            books,
            duplicate_choices=duplicate_choices,
            overlap_choices={books[0].book_key: "二维延迟谱"},
        )
        special_keys = {
            book_key
            for group in resolved.groups
            for book_key in group.book_keys
        }
        self.assertIn(books[0].book_key, special_keys)
        self.assertNotIn(books[1].book_key, special_keys)
        self.assertIn(books[1].book_key, resolved.regular_delayed_book_keys)

    def test_irregular_candidate_still_exposes_overlap_before_final_validation(self):
        books = [
            _delayed_emission("B300", excitation=300, flash_delay=0.1, time_per_flash=1.1),
            _delayed_emission("B310", excitation=310, flash_delay=0.1, time_per_flash=1.1),
            _delayed_emission("B360", excitation=360, flash_delay=0.1, time_per_flash=1.1),
            _delayed_emission("B370", excitation=370, flash_delay=0.1, time_per_flash=1.1),
            _delayed_emission("B380", excitation=380, flash_delay=0.1, time_per_flash=1.1),
            _delayed_emission("B300_D2", excitation=300, flash_delay=0.2, time_per_flash=1.2),
            _delayed_emission("B300_D3", excitation=300, flash_delay=0.3, time_per_flash=1.3),
        ]

        result = classify_special_groups(books)

        self.assertEqual((), result.groups)
        self.assertEqual((), result.regular_delayed_book_keys)
        self.assertEqual(0, result.final_validation_runs)
        self.assertEqual(1, len(result.pending_overlap_assignments))
        self.assertEqual(books[0].book_key, result.pending_overlap_assignments[0].book_key)

    def test_final_failure_after_review_returns_all_books_to_regular_delayed_flow(self):
        books = [
            _delayed_emission("B300a", excitation=300),
            _delayed_emission("B300b", excitation=300),
            _delayed_emission("B305", excitation=305),
            _delayed_emission("B310", excitation=310),
            _delayed_emission("B315", excitation=315),
            _delayed_emission("B320", excitation=320),
        ]

        pending = classify_special_groups(books).pending_duplicate_reviews[0]
        result = classify_special_groups(
            books,
            duplicate_choices={pending.choice_key: books[0].book_key},
            final_validation_passes=False,
        )

        self.assertEqual((), result.groups)
        self.assertEqual(tuple(book.book_key for book in books), result.regular_delayed_book_keys)
        self.assertEqual(1, result.final_validation_runs)

    def test_per_book_special_review_revalidates_final_points_and_returns_whole_failed_group(self):
        books = [
            _delayed_emission(f"B{w}", excitation=w)
            for w in (300, 305, 310, 315, 320)
        ]
        proposed = classify_special_groups(books).groups[0]

        accepted, ordinary_keys = resolve_special_group_selection(
            proposed,
            proposed.book_keys[:-1],
        )

        self.assertIsNone(accepted)
        self.assertEqual(proposed.book_keys, ordinary_keys)

    def test_per_book_special_review_keeps_valid_subset_and_returns_only_excluded_books(self):
        books = [
            _delayed_emission(f"B{w}", excitation=w)
            for w in (300, 305, 310, 315, 320, 325)
        ]
        proposed = classify_special_groups(books).groups[0]

        accepted, ordinary_keys = resolve_special_group_selection(
            proposed,
            proposed.book_keys[1:],
        )

        self.assertIsNotNone(accepted)
        self.assertEqual(proposed.book_keys[1:], accepted.book_keys)
        self.assertEqual((proposed.book_keys[0],), ordinary_keys)


def _book(source_id, folder, name, spectrum_class):
    return SpectrumBook(
        source_id=source_id,
        folder_path=folder,
        book_name=name,
        spectrum_class=spectrum_class,
        sample_label="MFL-film-298 K",
    )


def _delayed_emission(
    name,
    *,
    excitation,
    source_id="S1",
    folder="Delayed",
    sample="MFL-film-298 K",
    receiving_range=("350", "650"),
    excitation_slit="2",
    emission_slit="3",
    flash_delay=0.1,
    sample_window=1.0,
    time_per_flash=1.1,
    flash_count=100,
):
    return SpectrumBook(
        source_id=source_id,
        folder_path=folder,
        book_name=name,
        spectrum_class=SpectrumClass.DELAYED_EMISSION,
        sample_label=sample,
        fixed_excitation_wavelength=str(excitation),
        receiving_range=tuple(str(value) for value in receiving_range),
        excitation_slit=str(excitation_slit),
        emission_slit=str(emission_slit),
        flash_delay=str(flash_delay),
        sample_window=str(sample_window),
        time_per_flash=str(time_per_flash),
        flash_count=str(flash_count),
    )


if __name__ == "__main__":
    unittest.main()
