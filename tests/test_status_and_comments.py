#!/usr/bin/env python3
"""Status skip and comment-count checks. Uses a temp DB/CSV, not data/activity.db."""

from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

LOOKUP = ROOT / "brand-lookup-2026-10-08_09-46-04-waqas.txt"


class StoreStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        import reddit_joiner.store as store

        self.store = store
        self._old_db = store.DB_PATH
        store.DB_PATH = str(Path(self._tmp.name) / "activity.db")
        store.init_db()

    def tearDown(self) -> None:
        self.store.DB_PATH = self._old_db
        self._tmp.cleanup()

    def test_parse_brand_lookup_sections(self) -> None:
        text = LOOKUP.read_text(encoding="utf-8")
        mapping = self.store.parse_brand_lookup_report(text)
        counts = Counter(mapping.values())
        self.assertEqual(counts["LIVE"], 102)
        self.assertEqual(counts["BANNED"], 138)
        self.assertEqual(counts["SUSPENDED"], 7)
        self.assertEqual(len(mapping), 247)
        self.assertEqual(mapping["k1hfml2d"], "LIVE")
        self.assertEqual(mapping["k1hfmkv6"], "BANNED")
        self.assertEqual(mapping["k1hfmksr"], "SUSPENDED")
        self.assertNotEqual(mapping.get("k1hfml2d"), "BANNED")

    def test_live_does_not_replace_banned(self) -> None:
        self.store.record_account_outside_status("k1hfmkv6", "BANNED", source="outside")
        kept = self.store.record_account_outside_status(
            "k1hfmkv6", "LIVE", source="inside"
        )
        self.assertEqual(kept, "BANNED")
        self.assertEqual(self.store.get_account_outside_status("k1hfmkv6"), "BANNED")

    def test_outside_source_does_not_skip_before_open(self) -> None:
        self.store.record_account_outside_status("k1out1", "BANNED", source="outside")
        self.assertTrue(self.store.account_outside_status_is_dead("k1out1"))
        self.assertFalse(self.store.account_inside_status_is_dead("k1out1"))
        from reddit_joiner.joiner import account_is_dead, drop_dead_targets

        self.assertFalse(account_is_dead("k1out1"))
        kept = drop_dead_targets(
            [{"user_id": "k1out1", "name": "out1"}],
            why="test",
        )
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["user_id"], "k1out1")

    def test_inside_source_skips_before_open(self) -> None:
        self.store.record_account_outside_status("k1in1", "BANNED", source="inside")
        self.assertTrue(self.store.account_inside_status_is_dead("k1in1"))
        from reddit_joiner.joiner import account_is_dead, drop_dead_targets

        self.assertTrue(account_is_dead("k1in1"))
        kept = drop_dead_targets(
            [{"user_id": "k1in1", "name": "in1"}],
            why="test",
        )
        self.assertEqual(kept, [])


class CommentVisibleTests(unittest.TestCase):
    def test_unconfirmed_comments_are_unknown(self) -> None:
        from reddit_joiner.joiner import AccountSummary, session_tally_lines

        stats = AccountSummary(
            name="x",
            user_id="u1",
            comments=2,
            comments_thread_visible=0,
        )
        self.assertEqual(stats.comments_placed(), 2)
        self.assertEqual(stats.comments_visible(), 0)
        line = stats.one_line()
        self.assertIn("unknown", line)
        self.assertNotIn("comment 2 visible", line)
        tally = "\n".join(session_tally_lines([stats]))
        self.assertIn("not checked", tally)
        self.assertNotIn("2 still visible", tally)

    def test_thread_check_counts_visible(self) -> None:
        from reddit_joiner.joiner import AccountSummary

        stats = AccountSummary(
            name="y",
            user_id="u2",
            comments=1,
            comments_thread_visible=1,
        )
        self.assertEqual(stats.comments_visible(), 1)
        self.assertIn("comment 1 visible", stats.one_line())


class ServerErrorStatusTests(unittest.TestCase):
    def test_reason_is_ban_for_server_error(self) -> None:
        from reddit_joiner.joiner import _popup_dead_reason, _reason_is_ban, _reason_is_server_error

        self.assertTrue(_reason_is_server_error("we had a server error"))
        self.assertTrue(_reason_is_server_error("server error"))
        self.assertTrue(_reason_is_ban("we had a server error"))
        self.assertFalse(_reason_is_server_error("the account is banned"))
        self.assertEqual(
            _popup_dead_reason("We had a server error. Try again later."),
            "we had a server error",
        )

    def test_one_line_and_tally_show_server_error(self) -> None:
        from reddit_joiner.joiner import AccountSummary, _analytics_status, session_tally_lines

        stats = AccountSummary(
            name="bob",
            user_id="u1",
            sitting_ok=True,
            account_status="we had a server error",
        )
        self.assertTrue(stats.is_banned())
        line = stats.one_line()
        self.assertIn("we had a server error", line)
        self.assertNotIn("| banned", line)
        tally = "\n".join(session_tally_lines([stats]))
        self.assertIn("Banned                    : 1", tally)
        self.assertEqual(_analytics_status(stats), "server error")


class SheetEnabledTests(unittest.TestCase):
    def test_enabled_no_rows_are_skipped(self) -> None:
        from reddit_joiner import sheets

        tmp = tempfile.TemporaryDirectory()
        path = Path(tmp.name) / "accounts.csv"
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle, fieldnames=["user_id", "enabled", "name", "notes"]
            )
            writer.writeheader()
            writer.writerow({"user_id": "live1", "enabled": "yes", "name": "", "notes": ""})
            writer.writerow({"user_id": "dead1", "enabled": "no", "name": "", "notes": "banned"})
            writer.writerow({"user_id": "dead2", "enabled": "skip", "name": "", "notes": ""})
        old = sheets.ACCOUNTS_SHEET
        sheets.ACCOUNTS_SHEET = str(path)
        try:
            found = sheets.load_account_ids_from_sheet()
        finally:
            sheets.ACCOUNTS_SHEET = old
            tmp.cleanup()
        self.assertEqual(found, ["live1"])


if __name__ == "__main__":
    unittest.main()
