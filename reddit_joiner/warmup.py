#!/usr/bin/env python3
"""Staged warmup ladder: join, upvote, comment, and post unlock by account age."""

from __future__ import annotations

import random
from typing import Any, Optional, Tuple

COMMENT_MIN_AGE_DAYS = 8
UPVOTE_MIN_AGE_DAYS = 4
POST_MIN_AGE_DAYS = 14
NEWS_MIN_AGE_DAYS = 2
FOLLOW_MIN_AGE_DAYS = 5
ASK_JOIN_MIN_AGE_DAYS = 3
JOIN_MIN_AGE_DAYS = 4
COMMENTS_PER_DAY = 1
POST_GAP_DAYS = (5, 9)
SITTING_NEW_SCALE = (0.5, 0.7)
UPVOTE_DAYS_4_7 = (0, 3)
UPVOTE_DAYS_8_POST = (0, 5)
UPVOTE_COMMENT_DAYS_8 = (0, 2)
FOLLOW_CHANCE_RANGE = (0.04, 0.10)
FOLLOW_MAX_PER_DAY = 1
FOLLOW_TOTAL_RANGE = (12, 20)
FOLLOW_SHARED_CAP = 2
ACTION_WATCHDOG_SECONDS = 180
HUNG_START_SECONDS = 180
COMMENT_POST_AGE_MIN = 10 * 60
COMMENT_POST_AGE_MAX = 90 * 60
COMMENT_THREAD_REPLIES = (1, 2)
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


def age_days(stats: Any) -> float:
    try:
        return max(0.0, float(getattr(stats, "account_age_days", 0) or 0))
    except (TypeError, ValueError):
        return 0.0


def day_band(age: float) -> str:
    value = float(age or 0)
    if value < 1.0:
        return "unknown"
    if value < 4.0:
        return "days_1_3"
    if value < 8.0:
        return "days_4_7"
    if value < 14.0:
        return "days_8_13"
    return "day_14_plus"


def day_band_label(age: float) -> str:
    return {
        "unknown": "age unread",
        "days_1_3": "days 1-3 browse only",
        "days_4_7": "days 4-7 upvotes",
        "days_8_13": "day 8+ comments",
        "day_14_plus": "day 14+ posts",
    }.get(day_band(age), "warmup")


def may_join(age: float, subreddit: str = "") -> Tuple[bool, str]:
    value = float(age or 0)
    if value < 1.0:
        return False, "account age was not read — not joining"
    if value < float(JOIN_MIN_AGE_DAYS):
        return (
            False,
            f"no joins until day {JOIN_MIN_AGE_DAYS} (account is {value:.0f}d)",
        )
    name = (subreddit or "").strip().lstrip("r/").lower()
    if name in ASK_JOIN_NAMES or name.startswith("ask"):
        if value < float(ASK_JOIN_MIN_AGE_DAYS):
            return (
                False,
                f"Ask communities from day {ASK_JOIN_MIN_AGE_DAYS} (account is {value:.0f}d)",
            )
    return True, ""


def may_upvote(age: float) -> Tuple[bool, str]:
    value = float(age or 0)
    if value < float(UPVOTE_MIN_AGE_DAYS):
        return False, f"no upvotes until day {UPVOTE_MIN_AGE_DAYS} (account is {value:.0f}d)"
    return True, ""


def may_comment(age: float) -> Tuple[bool, str]:
    value = float(age or 0)
    if value < 1.0:
        return False, "account age was not read — no comment"
    if value < float(COMMENT_MIN_AGE_DAYS):
        return (
            False,
            f"account is {value:.0f} days old — comments from day {COMMENT_MIN_AGE_DAYS}",
        )
    return True, ""


def may_post(age: float) -> Tuple[bool, str]:
    value = float(age or 0)
    if value < 1.0:
        return False, "account age was not read — no post"
    if value < float(POST_MIN_AGE_DAYS):
        return (
            False,
            f"account is {value:.0f} days old — posts from day {POST_MIN_AGE_DAYS}",
        )
    return True, ""


def may_news(age: float) -> bool:
    return float(age or 0) >= float(NEWS_MIN_AGE_DAYS)


def may_follow(age: float) -> bool:
    return float(age or 0) >= float(FOLLOW_MIN_AGE_DAYS)


def sitting_scale(age: float) -> float:
    if float(age or 0) < 4.0:
        lo, hi = SITTING_NEW_SCALE
        return random.uniform(float(lo), float(hi))
    return 1.0


def scale_sitting_seconds(seconds: float, age: float) -> float:
    scaled = max(90.0, float(seconds) * sitting_scale(age))
    minute = round(scaled / 60.0) * 60.0
    if abs(scaled - minute) < 1.0:
        scaled += random.uniform(7.0, 23.0)
    return scaled


def upvote_post_budget(age: float) -> Tuple[int, int]:
    value = float(age or 0)
    if value < float(UPVOTE_MIN_AGE_DAYS):
        return 0, 0
    if value < float(COMMENT_MIN_AGE_DAYS):
        return UPVOTE_DAYS_4_7
    return UPVOTE_DAYS_8_POST


def upvote_comment_budget(age: float) -> Tuple[int, int]:
    value = float(age or 0)
    if value < float(COMMENT_MIN_AGE_DAYS):
        return 0, 0
    return UPVOTE_COMMENT_DAYS_8


def pick_upvote_target(age: float) -> int:
    lo, hi = upvote_post_budget(age)
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
        return False, "post is newer than 10 min"
    if age > float(COMMENT_POST_AGE_MAX):
        return False, "post is older than 90 min"
    return True, ""
