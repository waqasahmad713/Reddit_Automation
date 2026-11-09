#!/usr/bin/env python3
"""Unique sitting fingerprints and rare session explores."""

from __future__ import annotations

import random
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class SittingPatternTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        import reddit_joiner.joiner as joiner

        self.joiner = joiner
        self._old = joiner._SESSION_PATTERNS_PATH
        joiner._SESSION_PATTERNS_PATH = Path(self._tmp.name) / "session_patterns.json"
        joiner._batch_style_fps.clear()
        joiner._explore_account_ids = set()

    def tearDown(self) -> None:
        self.joiner._SESSION_PATTERNS_PATH = self._old
        self.joiner._batch_style_fps.clear()
        self.joiner._explore_account_ids = set()
        self._tmp.cleanup()

    def _targets(self, count: int) -> list:
        return [{"user_id": f"acct-{i}"} for i in range(count)]

    def test_session_explore_picks_few_accounts(self) -> None:
        self.joiner.assign_batch_explore_accounts(self._targets(20))
        picked = set(self.joiner._explore_account_ids)
        self.assertEqual(len(picked), 1)
        self.assertTrue(picked.issubset({f"acct-{i}" for i in range(20)}))
        chosen = next(iter(picked))
        self.assertTrue(self.joiner.account_may_explore(chosen))
        others = [f"acct-{i}" for i in range(20) if f"acct-{i}" != chosen]
        self.assertFalse(any(self.joiner.account_may_explore(uid) for uid in others))

    def test_session_explore_share_on_larger_run(self) -> None:
        sizes = []
        for _ in range(12):
            self.joiner.assign_batch_explore_accounts(self._targets(80))
            sizes.append(len(self.joiner._explore_account_ids))
        self.assertTrue(all(2 <= n <= 4 for n in sizes), sizes)

    def test_pick_explore_n_only_for_chosen_accounts(self) -> None:
        self.joiner._explore_account_ids = {"yes"}
        wins = {self.joiner.pick_explore_n(random.Random(i), "yes") for i in range(20)}
        self.assertTrue(wins <= {1, 2, 3})
        self.assertTrue(wins)
        self.assertEqual(self.joiner.pick_explore_n(random.Random(1), "no"), 0)
        self.assertEqual(self.joiner.pick_explore_n(random.Random(1), ""), 0)

    def test_keeps_more_than_eighty_fingerprints(self) -> None:
        for i in range(90):
            self.joiner.remember_session_pattern("acct", f"fp-{i}")
        data = self.joiner._load_session_patterns()
        self.assertEqual(len(data["acct"]), 90)
        self.assertTrue(self.joiner.pattern_already_used("acct", "fp-0"))
        self.assertTrue(self.joiner.pattern_already_used("acct", "fp-89"))

    def test_unique_session_plan_never_repeats(self) -> None:
        with patch.object(self.joiner, "pick_session_subreddits", return_value=["AskUK"]), \
            patch.object(self.joiner, "reserve_subreddits", return_value=True), \
            patch.object(self.joiner, "release_live_subreddits"):
            first = self.joiner.unique_session_plan("acct-unique", "lab")
            second = self.joiner.unique_session_plan("acct-unique", "lab")
        self.assertNotEqual(first[2], second[2])
        self.assertTrue(self.joiner.pattern_already_used("acct-unique", first[2]))
        self.assertTrue(self.joiner.pattern_already_used("acct-unique", second[2]))
        with patch.object(self.joiner, "pick_session_subreddits", return_value=["AskUK"]), \
            patch.object(self.joiner, "reserve_subreddits", return_value=True), \
            patch.object(self.joiner, "release_live_subreddits"):
            later = {self.joiner.unique_session_plan("acct-unique", "lab")[2] for _ in range(3)}
        self.assertNotIn(first[2], later)
        self.assertNotIn(second[2], later)


if __name__ == "__main__":
    unittest.main()
