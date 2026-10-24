#!/usr/bin/env python3
"""Targeted warm-up: sheet switch, groups, related list, and target picking. Temp CSVs only."""

from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import reddit_joiner.sheets as sheets  # noqa: E402


def _write(path: Path, fields, rows) -> None:
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


class _TempSheets(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        folder = Path(self._tmp.name)
        self._saved = {
            key: getattr(sheets, key)
            for key in (
                "GENERAL_ACCOUNTS_SHEET",
                "GENERAL_SUBREDDITS_SHEET",
                "TARGETED_ACCOUNTS_SHEET",
                "TARGETED_SUBREDDITS_SHEET",
                "TARGETED_RELATED_SHEET",
            )
        }
        self.general_accounts = folder / "accounts.csv"
        self.general_subs = folder / "subreddits.csv"
        self.accounts = folder / "targeted_accounts.csv"
        self.subs = folder / "targeted_subreddits.csv"
        self.related = folder / "targeted_related.csv"
        sheets.GENERAL_ACCOUNTS_SHEET = str(self.general_accounts)
        sheets.GENERAL_SUBREDDITS_SHEET = str(self.general_subs)
        sheets.TARGETED_ACCOUNTS_SHEET = str(self.accounts)
        sheets.TARGETED_SUBREDDITS_SHEET = str(self.subs)
        sheets.TARGETED_RELATED_SHEET = str(self.related)
        _write(self.general_accounts, sheets.ACCOUNTS_SHEET_FIELDS, [
            {"user_id": "gen1", "enabled": "yes", "name": "", "notes": ""},
        ])
        _write(self.general_subs, sheets.SUBREDDITS_SHEET_FIELDS, [
            {"subreddit": "CasualConversation", "enabled": "yes", "notes": ""},
        ])
        _write(self.accounts, sheets.TARGETED_ACCOUNTS_FIELDS, [
            {"user_id": "acc1", "enabled": "yes", "name": "", "notes": "", "group": "fitness"},
            {"user_id": "acc2", "enabled": "yes", "name": "", "notes": "", "group": ""},
            {"user_id": "acc3", "enabled": "no", "name": "", "notes": "", "group": "fitness"},
        ])
        _write(self.subs, sheets.TARGETED_SUBREDDITS_FIELDS, [
            {"group": "fitness", "subreddit": "running", "enabled": "yes", "notes": ""},
            {"group": "fitness", "subreddit": "r/bodyweightfitness", "enabled": "yes", "notes": ""},
            {"group": "crypto", "subreddit": "CryptoCurrency", "enabled": "yes", "notes": ""},
            {"group": "crypto", "subreddit": "Bitcoin", "enabled": "no", "notes": ""},
        ])
        sheets.set_sheet_mode("targeted")

    def tearDown(self) -> None:
        sheets.set_sheet_mode("general")
        for key, value in self._saved.items():
            setattr(sheets, key, value)
        sheets.set_sheet_mode("general")
        self._tmp.cleanup()


class SheetModeTests(_TempSheets):
    def test_clean_name_keeps_leading_r(self) -> None:
        self.assertEqual(sheets.clean_subreddit_name("running"), "running")
        self.assertEqual(sheets.clean_subreddit_name("r/running"), "running")
        self.assertEqual(sheets.clean_subreddit_name("/r/relationships/"), "relationships")

    def test_switch_points_at_targeted_files(self) -> None:
        self.assertTrue(sheets.is_targeted_mode())
        self.assertEqual(sheets.load_profile_ids(), ["acc1", "acc2"])
        sheets.set_sheet_mode("general")
        self.assertEqual(sheets.load_profile_ids(), ["gen1"])
        self.assertEqual(sheets.load_browse_subreddits(), ["CasualConversation"])

    def test_groups(self) -> None:
        self.assertEqual(sheets.account_group("ACC1"), "fitness")
        self.assertEqual(sheets.account_group("acc2"), "")
        self.assertEqual(sheets.load_browse_subreddits("fitness"), ["running", "bodyweightfitness"])
        self.assertEqual(sheets.load_browse_subreddits("crypto"), ["CryptoCurrency"])
        self.assertEqual(
            sheets.load_browse_subreddits(), ["running", "bodyweightfitness", "CryptoCurrency"]
        )

    def test_ban_disables_targeted_row(self) -> None:
        self.assertTrue(sheets.disable_account_in_sheet("acc1", reason="inside check BANNED"))
        self.assertEqual(sheets.load_profile_ids(), ["acc2"])
        with open(self.accounts, encoding="utf-8-sig") as handle:
            self.assertIn("group", next(csv.reader(handle)))

    def test_empty_sheets_created_with_headers_only(self) -> None:
        self.accounts.unlink()
        self.subs.unlink()
        self.assertEqual(sheets.load_profile_ids(), [])
        self.assertEqual(sheets.load_browse_subreddits(), [])
        with open(self.subs, encoding="utf-8-sig") as handle:
            self.assertEqual(next(csv.reader(handle)), sheets.TARGETED_SUBREDDITS_FIELDS)


class RelatedSheetTests(_TempSheets):
    def test_append_dedupe_and_skip_targets(self) -> None:
        self.assertTrue(sheets.append_target_related("fitness", "Fitness", "related on r/running"))
        self.assertFalse(sheets.append_target_related("fitness", "fitness", "search"))
        self.assertFalse(sheets.append_target_related("fitness", "running", "search"))
        self.assertTrue(sheets.append_target_related("crypto", "Fitness", "search"))
        self.assertEqual(sheets.load_target_related("fitness"), ["Fitness"])

    def test_disabled_related_is_not_used(self) -> None:
        sheets.append_target_related("fitness", "Fitness", "x")
        sheets.append_target_related("fitness", "xxfitness", "x")
        with open(self.related, encoding="utf-8-sig") as handle:
            rows = list(csv.DictReader(handle))
        rows[0]["enabled"] = "no"
        _write(self.related, sheets.TARGETED_RELATED_FIELDS, rows)
        self.assertEqual(sheets.load_target_related("fitness"), ["xxfitness"])
        self.assertIn("fitness", sheets.target_related_known("fitness"))
        self.assertFalse(sheets.append_target_related("fitness", "Fitness", "again"))

    def test_cap_per_group(self) -> None:
        for index in range(sheets.TARGETED_RELATED_CAP):
            self.assertTrue(sheets.append_target_related("fitness", f"sub{index:03d}", "x"))
        self.assertFalse(sheets.append_target_related("fitness", "onemore", "x"))
        self.assertTrue(sheets.append_target_related("crypto", "onemore", "x"))


try:
    import reddit_joiner.joiner as joiner  # noqa: E402
except ImportError:  # selenium missing outside the project venv
    joiner = None


@unittest.skipIf(joiner is None, "joiner needs selenium (run with the project venv)")
class TargetPickTests(_TempSheets):
    def tearDown(self) -> None:
        joiner.clear_session_target()
        super().tearDown()

    def test_allowed_and_pick_use_group(self) -> None:
        sheets.append_target_related("fitness", "Fitness", "x")
        target = joiner.set_session_target("acc1", "t")
        self.assertEqual(target["group"], "fitness")
        self.assertEqual(
            [name.lower() for name in joiner.allowed_subreddits()],
            ["running", "bodyweightfitness", "fitness"],
        )
        picked = joiner.pick_session_subreddits("acc1")
        self.assertTrue(picked)
        self.assertTrue({name.lower() for name in picked} <= {"running", "bodyweightfitness"})

    def test_no_group_uses_every_target(self) -> None:
        target = joiner.set_session_target("acc2", "t")
        self.assertEqual(target["group"], "")
        self.assertIn("CryptoCurrency", joiner.allowed_subreddits())

    def test_general_mode_has_no_target(self) -> None:
        sheets.set_sheet_mode("general")
        self.assertIsNone(joiner.set_session_target("gen1", "t"))
        self.assertEqual(joiner.allowed_subreddits(), ["CasualConversation"])

    def test_run_args(self) -> None:
        self.assertTrue(joiner.parse_run_args(["--targeted"]).targeted)
        self.assertFalse(joiner.parse_run_args([]).targeted)


if __name__ == "__main__":
    unittest.main()
