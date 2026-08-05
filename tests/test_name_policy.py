import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from spectrum_organizer.safety.name_policy import (
    FORBIDDEN_ORIGIN_NAME_CHARACTERS,
    MAX_GENERATED_NAME_LENGTH,
    NamePolicyError,
    NamePreflightIssue,
    classify_generated_name_preflight,
    forbidden_character_display,
    preflight_generated_names,
    require_safe_generated_token,
    validate_user_origin_name_text,
)


class NamePolicyTests(unittest.TestCase):
    def test_forbidden_character_display_includes_lf_and_cr_for_ui(self):
        self.assertIn("\n", FORBIDDEN_ORIGIN_NAME_CHARACTERS)
        self.assertIn("\r", FORBIDDEN_ORIGIN_NAME_CHARACTERS)
        self.assertIn("LF", forbidden_character_display())
        self.assertIn("CR", forbidden_character_display())

    def test_user_text_is_validated_not_silently_rewritten(self):
        text = "MFL-in-mCP-1×10^-4 M"

        self.assertEqual(text, validate_user_origin_name_text(text, field_name="book display name"))

        for value in ("MFL\nSolid", "MFL\rSolid"):
            with self.subTest(value=repr(value)):
                with self.assertRaises(NamePolicyError):
                    validate_user_origin_name_text(value, field_name="book display name")

    def test_user_text_exceeding_conservative_length_is_blocked(self):
        with self.assertRaises(NamePolicyError):
            validate_user_origin_name_text("X" * (MAX_GENERATED_NAME_LENGTH + 1), field_name="sample")

    def test_program_generated_tokens_are_ascii_and_separator_safe(self):
        self.assertEqual("FD0.1", require_safe_generated_token("FD0.1", field_name="folder token"))

        for value in ("Ex 270", "MFL×mCP", "FD/0.1", ""):
            with self.subTest(value=value):
                with self.assertRaises(NamePolicyError):
                    require_safe_generated_token(value, field_name="folder token")

    def test_generated_name_preflight_separates_folder_tokens_from_book_text(self):
        preflight_generated_names(
            folder_names=("F_Ex270_ExSlit2_EmSlit2",),
            book_display_names=("MFL-mTHF-1×10^-4 M", "MFL-in-mCP-10 wt%-Film"),
        )

        with self.assertRaises(NamePolicyError):
            preflight_generated_names(
                folder_names=("F_Ex270_Bad Token",),
                book_display_names=("MFL-mTHF-1×10^-4 M",),
            )
        with self.assertRaises(NamePolicyError):
            preflight_generated_names(
                folder_names=("F_Ex270_ExSlit2_EmSlit2",),
                book_display_names=("MFL\nSolid",),
            )
        with self.assertRaises(NamePolicyError):
            preflight_generated_names(
                folder_names=("F_Ex270_ExSlit2_EmSlit2",),
                book_display_names=("X" * 256,),
            )

    def test_generated_name_preflight_classifies_user_correction_vs_internal_failure(self):
        user_issue = classify_generated_name_preflight(
            folder_names=("F_Ex270",),
            book_display_names=("MFL\nSolid",),
        )
        internal_issue = classify_generated_name_preflight(
            folder_names=("F Bad",),
            book_display_names=("MFL Solid",),
        )

        self.assertEqual(NamePreflightIssue.RETURN_TO_ATTRIBUTION, user_issue)
        self.assertEqual(NamePreflightIssue.INTERNAL_NAMING_FAILURE, internal_issue)


if __name__ == "__main__":
    unittest.main()
