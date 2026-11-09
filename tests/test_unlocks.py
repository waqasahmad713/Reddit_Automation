#!/usr/bin/env python3
"""Per-account unlock days: stable schedules, may_*, quiet sitting, hops."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class UnlockScheduleTests(unittest.TestCase):
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

    def _set_days(self, user_id: str, **days: int) -> None:
        self.store.unlocks_for(user_id)
        cols = ", ".join(f"{name} = ?" for name in days)
        values = list(days.values()) + [user_id]
        with self.store._connect() as conn:
            conn.execute(
                f"UPDATE account_unlocks SET {cols} WHERE user_id = ?",
                values,
            )

    def test_two_ids_get_different_schedules(self) -> None:
        tuples = []
        for i in range(30):
            data = self.store.unlocks_for(f"unlock-id-{i}")
            tuples.append(
                (
                    data["join_day"],
                    data["upvote_day"],
                    data["comment_day"],
                    data["post_day"],
                )
            )
        self.assertGreater(len(set(tuples)), 1)

    def test_same_id_is_stable(self) -> None:
        first = self.store.unlocks_for("stable-account")
        second = self.store.unlocks_for("stable-account")
        self.assertEqual(first, second)
        from reddit_joiner import warmup

        a = warmup.unlocks_for("stable-account")
        b = warmup.unlocks_for("stable-account")
        self.assertEqual(a, b)
        self.assertEqual(a.join_day, first["join_day"])
        self.assertEqual(a.comment_day, first["comment_day"])

    def test_may_comment_uses_personal_day(self) -> None:
        from reddit_joiner import warmup

        self._set_days("comment-acct", comment_day=5)
        before, why = warmup.may_comment(4, user_id="comment-acct")
        self.assertFalse(before)
        self.assertIn("day 5", why)
        after, after_why = warmup.may_comment(5, user_id="comment-acct")
        self.assertTrue(after)
        self.assertEqual(after_why, "")

    def test_may_upvote_from_day_two(self) -> None:
        from reddit_joiner import warmup

        self._set_days("late-upvote", upvote_day=4)
        unread, why = warmup.may_upvote(0.5, user_id="late-upvote")
        self.assertFalse(unread)
        self.assertIn("age was not read", why)
        day_one, day_one_why = warmup.may_upvote(1, user_id="late-upvote")
        self.assertFalse(day_one)
        self.assertIn("day 2", day_one_why)
        allowed, allowed_why = warmup.may_upvote(2, user_id="late-upvote")
        self.assertTrue(allowed)
        self.assertEqual(allowed_why, "")

    def test_may_join_never_before_day_two(self) -> None:
        from reddit_joiner import warmup

        self._set_days("early-join", join_day=1)
        before, why = warmup.may_join(1, "CasualConversation", user_id="early-join")
        self.assertFalse(before)
        self.assertIn("day 2", why)
        after, after_why = warmup.may_join(2, "CasualConversation", user_id="early-join")
        self.assertTrue(after)
        self.assertEqual(after_why, "")

    def test_pick_upvote_target_is_one_to_five(self) -> None:
        from reddit_joiner import warmup

        self.assertEqual(warmup.pick_upvote_target(1, user_id="vote-acct"), 0)
        seen = {warmup.pick_upvote_target(2, user_id="vote-acct") for _ in range(40)}
        self.assertTrue(seen)
        self.assertTrue(all(1 <= n <= 5 for n in seen))

    def test_quiet_sitting_under_day_two(self) -> None:
        from reddit_joiner import warmup

        self._set_days("quiet-acct", join_day=3, upvote_day=4)
        self.assertTrue(warmup.quiet_sitting(0.5, user_id="quiet-acct"))
        self.assertTrue(warmup.quiet_sitting(1, user_id="quiet-acct"))
        self.assertFalse(warmup.quiet_sitting(2, user_id="quiet-acct"))

    def test_unique_session_hops_keeps_first(self) -> None:
        from reddit_joiner.joiner import unique_session_hops

        self.assertEqual(
            unique_session_hops(["AskUK", "python", "askuk", "Python", "news"]),
            ["AskUK", "python", "news"],
        )

    def test_quiet_sitting_keeps_sheet_pick(self) -> None:
        from reddit_joiner.joiner import keep_quiet_sheet_pick

        rng = __import__("random").Random(7)
        self.assertEqual(keep_quiet_sheet_pick([], rng=rng), [])
        kept = keep_quiet_sheet_pick(
            ["NoStupidQuestions", "CasualConversation", "hobbies"], rng=rng
        )
        self.assertEqual(len(kept), 1)
        self.assertIn(kept[0], ["NoStupidQuestions", "CasualConversation", "hobbies"])
        seen = {
            keep_quiet_sheet_pick(
                ["NoStupidQuestions", "CasualConversation"], rng=__import__("random").Random(i)
            )[0]
            for i in range(20)
        }
        self.assertTrue(seen <= {"NoStupidQuestions", "CasualConversation"})
        self.assertTrue(seen)

    def test_sheet_hops_stay_ahead_of_explore(self) -> None:
        from reddit_joiner.joiner import order_sheet_then_explore

        rng = __import__("random").Random(3)
        hops = order_sheet_then_explore(
            ["hobbies", "learnpython"],
            ["worldnews", "pics"],
            rng=rng,
        )
        self.assertEqual(len(hops), 4)
        self.assertEqual(set(hops[:2]), {"hobbies", "learnpython"})
        self.assertEqual(set(hops[2:]), {"worldnews", "pics"})
        mixed = order_sheet_then_explore(
            ["hobbies"],
            ["hobbies", "pics"],
            rng=__import__("random").Random(1),
        )
        self.assertEqual(mixed[0], "hobbies")
        self.assertEqual(mixed[1:], ["pics"])

    def test_leftover_menu_does_not_revisit_subs(self) -> None:
        from reddit_joiner.joiner import roll_activity_menu

        rng = __import__("random").Random(1)
        for _ in range(40):
            menu = roll_activity_menu(rng)
            self.assertEqual(menu.leftover_dest, "home")
            self.assertNotEqual(menu.leftover_dest, "visited_subs")

    def test_subreddits_per_session_is_one_to_ten(self) -> None:
        from reddit_joiner.joiner import SUBREDDITS_PER_SESSION

        self.assertEqual(SUBREDDITS_PER_SESSION, (1, 10))

    def test_max_joins_per_session_is_four(self) -> None:
        from reddit_joiner.joiner import MAX_JOINS_PER_SESSION

        self.assertEqual(MAX_JOINS_PER_SESSION, 4)

    def test_sitting_scale_keeps_assigned_minutes(self) -> None:
        from reddit_joiner import warmup
        from reddit_joiner.joiner import SESSION_SECONDS_RANGE

        self.assertEqual(SESSION_SECONDS_RANGE, (7 * 60, 20 * 60))
        assigned = 13 * 60.0
        for age in (0.5, 1.0, 8.0):
            scaled = warmup.scale_sitting_seconds(assigned, age, user_id="sit-acct")
            self.assertGreaterEqual(scaled, assigned)
            self.assertLess(scaled, assigned + 30.0)

    def test_proxy_success_message(self) -> None:
        from reddit_joiner.joiner import _proxy_refresh_succeeded

        self.assertTrue(_proxy_refresh_succeeded("Refresh proxy IP Successful request"))
        self.assertFalse(_proxy_refresh_succeeded("Refresh proxy IP"))

    def test_hung_start_resets_after_home(self) -> None:
        from reddit_joiner.joiner import hung_start_overrun
        from reddit_joiner.warmup import HUNG_START_SECONDS

        ads_started = 0.0
        after_proxy = 90.0
        self.assertFalse(hung_start_overrun(after_proxy, after_proxy + 1.0))
        self.assertTrue(
            hung_start_overrun(ads_started, ads_started + HUNG_START_SECONDS + 1.0)
        )

    def test_activity_deadline_excludes_proxy_wait(self) -> None:
        from reddit_joiner.joiner import activity_deadline

        session_start = 1_000.0
        sitting = 12 * 60.0
        wind_down = 10.0
        self.assertEqual(
            activity_deadline(session_start, sitting, wind_down),
            session_start + sitting - wind_down,
        )


if __name__ == "__main__":
    unittest.main()
