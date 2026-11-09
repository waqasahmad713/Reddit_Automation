#!/usr/bin/env python3
"""Per-account unlock days for join, upvote, comment, and post."""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any, Optional, Tuple

# Range highs (and fallbacks when user_id is missing). Each account rolls
# inside these ranges once and keeps the result.
COMMENT_MIN_AGE_DAYS = 8
UPVOTE_MIN_AGE_DAYS = 2
POST_MIN_AGE_DAYS = 16
NEWS_MIN_AGE_DAYS = 4
FOLLOW_MIN_AGE_DAYS = 8
ASK_JOIN_MIN_AGE_DAYS = 3
JOIN_MIN_AGE_DAYS = 5
COMMENTS_PER_DAY = 1
POST_GAP_DAYS = (5, 9)
SITTING_NEW_SCALE = (1.0, 1.0)
UPVOTE_PER_SITTING = (1, 5)
UPVOTE_DAYS_4_7 = UPVOTE_PER_SITTING
UPVOTE_DAYS_8_POST = UPVOTE_PER_SITTING
UPVOTE_COMMENT_DAYS_8 = (0, 2)
FOLLOW_CHANCE_RANGE = (0.04, 0.10)
FOLLOW_MAX_PER_DAY = 1
FOLLOW_TOTAL_RANGE = (12, 20)
FOLLOW_SHARED_CAP = 2
ACTION_WATCHDOG_SECONDS = 180
HUNG_START_SECONDS = 180
COMMENT_POST_AGE_MIN = 5 * 60
COMMENT_POST_AGE_MAX = 6 * 60 * 60
COMMENT_THREAD_REPLIES = (0, 8)
FEED_SORT_WEIGHTS = (
    ("best", 35),
    ("hot", 30),
    ("new", 15),
    ("top", 12),
    ("rising", 8),
)
ASK_JOIN_NAMES = frozenset(
    {
        "askuk",
        "askreddit",
        "tooafraidtoask",
        "nostupidquestions",
        "askmen",
        "askwomen",
        "askeurope",
        "askculinary",
        "askbrits",
        "askscience",
    }
)


@dataclass(frozen=True)
class UnlockSchedule:
    join_day: int
    upvote_day: int
    comment_day: int
    post_day: int
    news_day: int
    follow_day: int

    def describe(self) -> str:
        return (
            f"join d{self.join_day}, upvote d{self.upvote_day}, "
            f"comment d{self.comment_day}, post d{self.post_day}"
        )


def _user_id_of(stats: Any, user_id: str = "") -> str:
    if user_id:
        return str(user_id).strip()
    if stats is not None:
        return str(getattr(stats, "user_id", "") or "").strip()
    return ""


def unlocks_for(user_id: str = "", stats: Any = None) -> UnlockSchedule:
    key = _user_id_of(stats, user_id)
    try:
        from reddit_joiner.store import unlocks_for as _stored

        data = _stored(key)
    except Exception:
        data = {
            "join_day": JOIN_MIN_AGE_DAYS,
            "upvote_day": UPVOTE_MIN_AGE_DAYS,
            "comment_day": COMMENT_MIN_AGE_DAYS,
            "post_day": POST_MIN_AGE_DAYS,
            "news_day": NEWS_MIN_AGE_DAYS,
            "follow_day": FOLLOW_MIN_AGE_DAYS,
        }
    return UnlockSchedule(
        join_day=max(2, int(data.get("join_day") or 2)),
        upvote_day=UPVOTE_MIN_AGE_DAYS,
        comment_day=max(2, int(data.get("comment_day") or COMMENT_MIN_AGE_DAYS)),
        post_day=int(data.get("post_day") or POST_MIN_AGE_DAYS),
        news_day=int(data.get("news_day") or NEWS_MIN_AGE_DAYS),
        follow_day=int(data.get("follow_day") or FOLLOW_MIN_AGE_DAYS),
    )


def age_days(stats: Any) -> float:
    try:
        return max(0.0, float(getattr(stats, "account_age_days", 0) or 0))
    except (TypeError, ValueError):
        return 0.0


def quiet_sitting(age: float, user_id: str = "", stats: Any = None) -> bool:
    """True while age is unread or under 2 days. Upvote/join/comment start on day 2."""
    return float(age or 0) < 2.0


def day_band(age: float, user_id: str = "", stats: Any = None) -> str:
    value = float(age or 0)
    if value < 1.0:
        return "unknown"
    if quiet_sitting(value, user_id, stats):
        return "quiet"
    sched = unlocks_for(user_id, stats)
    if value < sched.comment_day:
        return "early"
    if value < sched.post_day:
        return "comments"
    return "posts"


def day_band_label(age: float, user_id: str = "", stats: Any = None) -> str:
    band = day_band(age, user_id, stats)
    sched = unlocks_for(user_id, stats)
    return {
        "unknown": "age unread",
        "quiet": "quiet browse — join and upvote still locked",
        "early": f"early sitting — comments from day {sched.comment_day}",
        "comments": f"comments unlocked — posts from day {sched.post_day}",
        "posts": "posts unlocked",
    }.get(band, "warmup")


def may_join(
    age: float,
    subreddit: str = "",
    user_id: str = "",
    stats: Any = None,
) -> Tuple[bool, str]:
    value = float(age or 0)
    if value < 1.0:
        return False, "account age was not read — not joining"
    sched = unlocks_for(user_id, stats)
    join_from = max(2, int(sched.join_day))
    if value < float(join_from):
        return (
            False,
            f"no joins until day {join_from} for this account (age is {value:.0f}d)",
        )
    from reddit_joiner.sheets import clean_subreddit_name

    name = clean_subreddit_name(subreddit).lower()
    ask_from = max(int(ASK_JOIN_MIN_AGE_DAYS), join_from)
    if name in ASK_JOIN_NAMES or name.startswith("ask"):
        if value < float(ask_from):
            return (
                False,
                f"Ask communities from day {ask_from} for this account (age is {value:.0f}d)",
            )
    return True, ""


def may_upvote(age: float, user_id: str = "", stats: Any = None) -> Tuple[bool, str]:
    value = float(age or 0)
    if value < 1.0:
        return False, "account age was not read — no upvotes"
    if value < float(UPVOTE_MIN_AGE_DAYS):
        return (
            False,
            f"no upvotes until day {UPVOTE_MIN_AGE_DAYS} for this account (age is {value:.0f}d)",
        )
    return True, ""


def may_comment(age: float, user_id: str = "", stats: Any = None) -> Tuple[bool, str]:
    value = float(age or 0)
    if value < 1.0:
        return False, "account age was not read — no comment"
    sched = unlocks_for(user_id, stats)
    comment_from = max(2, int(sched.comment_day))
    if value < float(comment_from):
        return (
            False,
            f"comments from day {comment_from} for this account (age {value:.0f}d)",
        )
    return True, ""


def may_post(age: float, user_id: str = "", stats: Any = None) -> Tuple[bool, str]:
    value = float(age or 0)
    if value < 1.0:
        return False, "account age was not read — no post"
    sched = unlocks_for(user_id, stats)
    if value < float(sched.post_day):
        return (
            False,
            f"posts from day {sched.post_day} for this account (age {value:.0f}d)",
        )
    return True, ""


def may_news(age: float, user_id: str = "", stats: Any = None) -> bool:
    value = float(age or 0)
    if value < 1.0:
        return False
    return value >= float(unlocks_for(user_id, stats).news_day)


def may_follow(age: float, user_id: str = "", stats: Any = None) -> bool:
    value = float(age or 0)
    if value < 1.0:
        return False
    return value >= float(unlocks_for(user_id, stats).follow_day)


def sitting_scale(age: float, user_id: str = "", stats: Any = None) -> float:
    lo, hi = SITTING_NEW_SCALE
    return random.uniform(float(lo), float(hi))


def scale_sitting_seconds(
    seconds: float, age: float, user_id: str = "", stats: Any = None
) -> float:
    scaled = max(90.0, float(seconds) * sitting_scale(age, user_id, stats))
    minute = round(scaled / 60.0) * 60.0
    if abs(scaled - minute) < 1.0:
        scaled += random.uniform(7.0, 23.0)
    return scaled


def upvote_post_budget(
    age: float, user_id: str = "", stats: Any = None
) -> Tuple[int, int]:
    value = float(age or 0)
    allowed, _why = may_upvote(value, user_id, stats)
    if not allowed:
        return 0, 0
    return UPVOTE_PER_SITTING


def upvote_comment_budget(
    age: float, user_id: str = "", stats: Any = None
) -> Tuple[int, int]:
    allowed, _why = may_comment(age, user_id, stats)
    if not allowed:
        return 0, 0
    return UPVOTE_COMMENT_DAYS_8


def pick_upvote_target(age: float, user_id: str = "", stats: Any = None) -> int:
    lo, hi = upvote_post_budget(age, user_id, stats)
    if hi <= 0:
        return 0
    return random.randint(int(lo), int(hi))


def pick_feed_sort() -> str:
    names = [name for name, _w in FEED_SORT_WEIGHTS]
    weights = [int(weight) for _name, weight in FEED_SORT_WEIGHTS]
    return random.choices(names, weights=weights, k=1)[0]


def comment_thread_ok(num_comments: int, created_age_seconds: Optional[float]) -> Tuple[bool, str]:
    lo, hi = COMMENT_THREAD_REPLIES
    if int(num_comments) < int(lo) or int(num_comments) > int(hi):
        return False, f"thread has {num_comments} comments, want {lo}-{hi}"
    if created_age_seconds is None:
        return True, ""
    age = float(created_age_seconds)
    if age < float(COMMENT_POST_AGE_MIN):
        return False, f"post is newer than {_age_label(COMMENT_POST_AGE_MIN)}"
    if age > float(COMMENT_POST_AGE_MAX):
        return False, f"post is older than {_age_label(COMMENT_POST_AGE_MAX)}"
    return True, ""


def _age_label(seconds: float) -> str:
    minutes = float(seconds) / 60.0
    if minutes >= 120 and minutes % 60 == 0:
        return f"{minutes / 60:.0f} h"
    return f"{minutes:.0f} min"
