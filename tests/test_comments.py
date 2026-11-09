#!/usr/bin/env python3
"""Humanized comment cleaning: keep contractions, no TL;DR template."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class CommentVoiceTests(unittest.TestCase):
    def test_keeps_contractions(self) -> None:
        from reddit_joiner.ai import _clean_comment

        text = "I've tried that stove and don't think it's worth the extra cash."
        out = _clean_comment(text, expand=True, strategy="validating")
        self.assertIn("I've", out)
        self.assertIn("don't", out)
        self.assertNotIn("I have", out)
        self.assertNotIn("do not", out)

    def test_formatting_accepts_two_lines(self) -> None:
        from reddit_joiner.ai import _clean_comment, _shape_formatting_comment

        two = "Start with a cheap used pan.\nSeason it once and see if you like cooking more."
        shaped = _shape_formatting_comment(two)
        self.assertEqual(shaped, two)
        cleaned = _clean_comment(two, strategy="formatting")
        self.assertIn("used pan", cleaned)
        self.assertNotIn("TL;DR", cleaned)
        self.assertNotIn("**", cleaned)

    def test_formatting_does_not_require_tldr(self) -> None:
        from reddit_joiner.ai import _shape_formatting_comment

        blob = (
            "**TL;DR:** Buy the cheaper one.\n\n"
            "- **Cost:** It is half the price.\n"
            "- **Use:** You can return it."
        )
        out = _shape_formatting_comment(blob)
        self.assertTrue(out)
        self.assertNotIn("TL;DR", out)
        self.assertNotIn("**", out)

    def test_rejects_as_an_ai(self) -> None:
        from reddit_joiner.ai import _clean_comment, looks_like_automation

        self.assertTrue(looks_like_automation("As an AI I cannot cook steak."))
        self.assertEqual(_clean_comment("As an AI I cannot cook steak tonight."), "")

    def test_rewrites_dash_and_keeps_comment(self) -> None:
        from reddit_joiner.ai import _clean_comment

        text = "I've tried that stove — don't think it's worth the extra cash."
        out = _clean_comment(text, strategy="validating")
        self.assertTrue(out)
        self.assertNotIn("—", out)
        self.assertNotIn("–", out)
        self.assertNotIn("--", out)
        self.assertIn("I've", out)
        self.assertIn("don't", out)
        self.assertTrue("," in out or "." in out)

    def test_rejects_slop_phrases(self) -> None:
        from reddit_joiner.ai import _clean_comment

        self.assertEqual(
            _clean_comment(
                "It's not just cheaper, it's better overall.",
                strategy="validating",
            ),
            "",
        )
        self.assertEqual(
            _clean_comment("Let me know if you want more.", strategy="validating"),
            "",
        )

    def test_keeps_ordinary_contraction_comment(self) -> None:
        from reddit_joiner.ai import _clean_comment

        text = "I've tried that and don't think it's worth it."
        out = _clean_comment(text, strategy="validating")
        self.assertEqual(out, text)
        self.assertIn("I've", out)
        self.assertIn("don't", out)

    def test_rejects_new_slop_phrases(self) -> None:
        from reddit_joiner.ai import _clean_comment

        self.assertEqual(
            _clean_comment("It turns out this stove is fine for weeknights.", strategy="validating"),
            "",
        )
        self.assertEqual(
            _clean_comment("Let that sink in after you try the cheaper pan.", strategy="validating"),
            "",
        )
        self.assertEqual(
            _clean_comment("This cheap pan is a game-changer for weeknights.", strategy="validating"),
            "",
        )

    def test_keeps_ordinary_adverbs(self) -> None:
        from reddit_joiner.ai import _clean_comment

        text = "I just really like that pan for weeknight cooking."
        out = _clean_comment(text, strategy="validating")
        self.assertEqual(out, text)
        self.assertIn("just", out)
        self.assertIn("really", out)

    def test_voice_file_loads(self) -> None:
        from reddit_joiner.ai import comment_voice_blob

        blob = comment_voice_blob()
        self.assertTrue(blob)
        self.assertIn("em dash", blob.lower())
        self.assertIn("contraction", blob.lower())

    def test_post_field_rewrites_dash_and_keeps_contractions(self) -> None:
        from reddit_joiner.ai import _clean_post_field

        out = _clean_post_field(
            "I've tried that stove — don't think it's worth the extra cash."
        )
        self.assertTrue(out)
        self.assertNotIn("—", out)
        self.assertNotIn("–", out)
        self.assertNotIn("--", out)
        self.assertIn("I've", out)
        self.assertIn("don't", out)

    def test_post_field_drops_slop(self) -> None:
        from reddit_joiner.ai import _clean_post_field

        self.assertEqual(
            _clean_post_field("It turns out this stove is fine for weeknight cooking."),
            "",
        )
        self.assertEqual(
            _clean_post_field("Here's the thing about this cheap used pan."),
            "",
        )


if __name__ == "__main__":
    unittest.main()
