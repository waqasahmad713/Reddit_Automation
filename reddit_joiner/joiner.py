#!/usr/bin/env python3
"""
Automate human-like Reddit browsing, AI comments, and spreadsheet posts
across AdsPower profiles.

Step 1 — Launch AdsPower profile (Start API + Selenium debuggerAddress)
Step 2 — 5–10 min human activity: Home first, then sub → Home hops.
         1 post + 2 comments / 48h. Bouncy scroll; skip image/video.
Step 3 — 2 general comments on new posts or comments.csv links.
Step 4 — Account summary (AI comments, upvotes, live posts)
Step 5 — driver.quit() + AdsPower Stop API

Set DEEPSEEK_API_KEY (free tokens at platform.deepseek.com) or OPENROUTER_API_KEY
for comments and AI posts. Gemini/Groq/OpenAI still work as fallbacks.
Optional: REDDIT_CLIENT_ID + REDDIT_CLIENT_SECRET for PRAW LIVE checks.
"""

from __future__ import annotations

import csv
import json
import math
import os
import random
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote_plus, urlparse

from reddit_joiner import human
from reddit_joiner.paths import (
    ACCOUNTS_SHEET as _ACCOUNTS_SHEET,
    COMMENT_LOG_PATH as _COMMENT_LOG_PATH,
    COMMENTED_LINKS_SHEET as _COMMENTED_LINKS_SHEET,
    COMMENTS_SHEET as _COMMENTS_SHEET,
    DATA_DIR as _DATA_DIR,
    ENV_FILE,
    POST_LOG_PATH as _POST_LOG_PATH,
    POSTS_SHEET as _POSTS_SHEET,
    POST_TEMPLATES_DIR as _POST_TEMPLATES_DIR,
    PROFILE_CACHE_PATH as _PROFILE_CACHE_PATH,
    PROFILES_FILE as _PROFILES_FILE,
    RL_MODEL_FILE as _RL_MODEL_FILE,
    SESSION_PATTERNS_PATH as _SESSION_PATTERNS_PATH,
    SUBREDDIT_LOG_PATH as _SUBREDDIT_LOG_PATH,
    SUBREDDITS_SHEET as _SUBREDDITS_SHEET,
    SUMMARIES_DIR as _SUMMARIES_DIR,
)

try:
    from dotenv import load_dotenv

    load_dotenv(ENV_FILE)
except ImportError:
    pass

try:
    from PIL import Image
except ImportError:
    Image = None  # type: ignore[assignment]

import requests
from selenium import webdriver
from selenium.common.exceptions import (
    JavascriptException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver import ActionChains
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

try:
    import undetected_chromedriver as uc
except ImportError:
    uc = None  # type: ignore[assignment]


# =============================================================================
# CONFIGURATION — edit these
# =============================================================================

# AdsPower profiles to open (user_id, profile name, or serial).
# Prefer profiles.txt (one id per line). All listed accounts run in parallel.
_DEFAULT_PROFILE_IDS: List[str] = [
    "k1g5mjmi",
    "k1g5mjmh",
]
PROFILES_FILE = str(_PROFILES_FILE)
ACCOUNTS_SHEET = str(_ACCOUNTS_SHEET)
SUBREDDITS_SHEET = str(_SUBREDDITS_SHEET)
ACCOUNTS_SHEET_FIELDS = ["user_id", "enabled", "name", "notes"]
SUBREDDITS_SHEET_FIELDS = ["subreddit", "enabled", "notes"]


def _cell_enabled(value: str) -> bool:
    return str(value or "yes").strip().lower() not in {
        "",
        "0",
        "no",
        "n",
        "false",
        "off",
        "skip",
        "disabled",
    }


def _read_simple_csv(path: str, fieldnames: List[str]) -> Tuple[List[str], List[Dict[str, str]]]:
    if not os.path.isfile(path):
        return list(fieldnames), []
    with open(path, encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        headers = list(reader.fieldnames or fieldnames)
        rows = [{key: (row.get(key) or "").strip() for key in headers} for row in reader]
    for name in fieldnames:
        if name not in headers:
            headers.append(name)
            for row in rows:
                row.setdefault(name, "")
    return headers, rows


def _write_simple_csv(path: str, fieldnames: List[str], rows: List[Dict[str, str]]) -> None:
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fieldnames})
    os.replace(tmp_path, path)


def _csv_lookup(row: Dict[str, str], *names: str) -> str:
    lower = {str(key).strip().lower(): key for key in row}
    for name in names:
        key = lower.get(name.lower())
        if key:
            value = str(row.get(key) or "").strip()
            if value:
                return value
    return ""


def ensure_accounts_sheet() -> None:
    if os.path.isfile(ACCOUNTS_SHEET):
        return
    rows = [{"user_id": item, "enabled": "yes", "name": "", "notes": ""} for item in _DEFAULT_PROFILE_IDS]
    _write_simple_csv(ACCOUNTS_SHEET, ACCOUNTS_SHEET_FIELDS, rows)


def ensure_subreddits_sheet() -> None:
    if os.path.isfile(SUBREDDITS_SHEET):
        return
    rows = [
        {"subreddit": "CasualConversation", "enabled": "yes", "notes": ""},
        {"subreddit": "NoStupidQuestions", "enabled": "yes", "notes": ""},
        {"subreddit": "Advice", "enabled": "yes", "notes": ""},
    ]
    _write_simple_csv(SUBREDDITS_SHEET, SUBREDDITS_SHEET_FIELDS, rows)


def _row_is_enabled(row: Dict[str, str]) -> bool:
    lower = {str(key).strip().lower(): key for key in row}
    col = lower.get("enabled") or lower.get("active") or lower.get("on")
    if not col:
        return True
    flag = str(row.get(col) or "").strip()
    return (not flag) or _cell_enabled(flag)


def load_account_ids_from_sheet() -> List[str]:
    ensure_accounts_sheet()
    _, rows = _read_simple_csv(ACCOUNTS_SHEET, ACCOUNTS_SHEET_FIELDS)
    found: List[str] = []
    seen = set()
    for row in rows:
        if not _row_is_enabled(row):
            continue
        user_id = _csv_lookup(row, "user_id", "account", "profile", "ads_power", "id")
        if not user_id:
            continue
        key = user_id.lower()
        if key in seen:
            continue
        seen.add(key)
        found.append(user_id)
    return found


def load_browse_subreddits() -> List[str]:
    ensure_subreddits_sheet()
    _, rows = _read_simple_csv(SUBREDDITS_SHEET, SUBREDDITS_SHEET_FIELDS)
    found: List[str] = []
    seen = set()
    for row in rows:
        if not _row_is_enabled(row):
            continue
        name = _csv_lookup(row, "subreddit", "community", "sub")
        name = name.lstrip("r/").lstrip("/")
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        found.append(name)
    return found


def _read_profile_id_file(path: str) -> List[str]:
    found: List[str] = []
    seen = set()
    if not os.path.isfile(path):
        return found
    try:
        with open(path, encoding="utf-8") as handle:
            for raw in handle:
                line = raw.split("#", 1)[0].strip()
                if not line:
                    continue
                key = line.lower()
                if key in seen:
                    continue
                seen.add(key)
                found.append(line)
    except Exception:
        return []
    return found


def load_profile_ids() -> List[str]:
    """Only AdsPower ids from accounts.csv (enabled=yes)."""
    return load_account_ids_from_sheet()


PROFILE_IDS: List[str] = load_profile_ids()

# Fallback names only. Each run picks a fresh set from karma-friendly pools
# so accounts do not keep browsing the same three communities.
SUBREDDITS: List[str] = [
    "NoStupidQuestions",
    "Advice",
    "AskUK",
]
SUBREDDITS_TO_BROWSE = SUBREDDITS
SUBREDDITS_PER_SESSION = (1, 3)  # rolled again each sitting from subreddits.csv
SUBREDDIT_ROTATION_RUNS = 1  # prefer sheet communities not used last sitting
RANDOM_EXPLORE_PER_SESSION = (1, 4)  # extra random communities, count changes every sitting
EXPLORE_RANDOM_SUBS = True
EXPLORE_SHARE = 0.30  # about 30% of communities this sitting are exploration
# A random explore is skipped for this many later sittings on the same account,
# then it can be explored again. Sheet communities (AskUK / NoStupidQuestions /
# Advice) are never put on this cooldown.
EXPLORE_SKIP_SESSIONS = 1
EXPLORE_MEMORY = 12  # how many explore sittings to remember per account
EXPLORE_RANDOM_TRIES = 6  # unused; communities come from search or the live feed

ACTIVITY_ON_HOMEPAGE = 12  # unused; home time comes from HOME_ACTIVITY_SHARE
ACTIVITY_ON_SUBREDDIT = (12, 28)  # quick look inside a community, then back to Home
ACCOUNT_SESSION_SECONDS = 7 * 60  # fallback only; live sittings roll 5–10 min
POST_IN_SESSION_FRACTION = 0.45  # attempt post mid-sitting when there is room
HOME_ACTIVITY_SHARE = 0.65  # default; each sitting rolls its own Home share
HOME_ACTIVITY_SHARE_RANGE = (0.52, 0.78)
HOME_ACTIVITY_BEFORE_JOIN = (70, 130)  # first Home stretch, then shifted per sitting
HOME_BETWEEN_SUBS = (35, 70)  # Home hop between communities
MIN_SUBREDDIT_DWELL = 10.0
MAX_SUBREDDIT_DWELL = 30.0  # hard cap so no community eats the sitting
# Ceiling on ALL time inside communities per sitting — feed browsing, rules,
# thread reads and the comment flow together. Measured on the clock, so extras
# inside a community cannot quietly stretch the visit.
MAX_COMMUNITY_SHARE = 0.28  # default; each sitting rolls 0.16–0.34
MAX_COMMUNITY_SHARE_RANGE = (0.16, 0.34)
# Held back from the browse dwell when a comment is planned here, so reading the
# thread and writing the reply does not land on top of a full-length browse.
COMMENT_FLOW_RESERVE = 45.0
# Never browse, join, comment or post in these, whatever the sheets say.
BLOCKED_SUBREDDITS = {
    "askreddit",
}
COMMENTS_PER_SUBREDDIT = (1, 1)  # one general comment per community when budget allows
COMMENT_LISTING_SORTS = ("new",)
# A general comment is only written when the analysed post is one of these.
# Anything else ("other") is read and left alone.
COMMENTABLE_POST_INTENTS = {"help", "suggestion", "review", "question"}

# Searching from Home, then reading the results on the "New" tab. Counts as Home
# time, since the search bar and the results page both live outside a community.
SEARCH_ON_NEW_CHANCE = 0.7
SEARCHES_PER_SESSION = (0, 3)
SEARCH_RESULT_DWELL = (20.0, 45.0)
SEARCH_OPEN_RESULT_CHANCE = 0.5
SEARCH_MIN_REMAINING = 45.0  # general comments only on new posts
COMMUNITY_POST_CHANCE = 0.18  # open fewer posts in a short sitting

# Smooth wheel scrolling: small ticks, uneven gaps, light rebound.
SCROLL_TICK_PX = (10, 36)
SCROLL_TICK_DELAY = (0.03, 0.18)  # redrawn every tick, not one steady beat
SCROLL_DOWN_PX = (70, 420)
SCROLL_UP_PX = (24, 160)
SCROLL_EASE_SETTLE = (0.08, 0.55)  # pause once the gesture stops
SCROLL_BOUNCE_CHANCE = 0.40  # light rebound on some gestures
SCROLL_BOUNCE_FRAC = (0.04, 0.10)  # rebound this fraction of the travel
SCROLL_BOUNCE_TICKS = (1, 2)  # tiny opposite ticks as it settles
SKIP_IMAGE_VIDEO_POSTS = True  # do not open / comment on image, video, or gallery posts
READ_PAUSE = (0.5, 4.8)  # seconds spent reading after a scroll
LONG_READ_PAUSE = (3.2, 11.0)  # occasional longer look at the feed
HUMAN_BREAK_CHANCE = 0.07  # sit still like someone checked their phone
HUMAN_BREAK = (6.0, 16.0)
MOUSE_WANDER_CHANCE = 0.16
THREAD_READ = (4.0, 7.0)  # open replies, glance briefly, leave
AFTER_COMMENT_LINGER = (4.0, 9.0)  # short stay after commenting
COMMENT_THINK = (1.5, 4.0)  # pause before opening the composer
BETWEEN_COMMENTS = (20.0, 40.0)  # gap between general comments in a short sitting
PROFILE_VISIT_CHANCE = 0.04
POPULAR_VISIT_CHANCE = 0.03
EXPAND_COMMENTS_CHANCE = 0.25
UPVOTE_CHANCE = 0.10  # occasional upvote while reading — not every post
MIN_UPVOTE_GAP = (20.0, 40.0)  # seconds between upvotes
UPVOTE_ON_OPENED_POST = True  # also upvote after opening a post to read
HOVER_CHANCE = 0.20
CLICK_POST_CHANCE = 0.12  # open posts to read; most are lurk-only
AI_COMMENT_CHANCE = 0.08  # most opened posts stay read-only
POST_LOAD_WAIT = (2.0, 4.0)
POST_READ_WAIT = (1.5, 3.0)  # brief pause before typing — thread glance is THREAD_READ
AI_COMMENT_WAIT = (4.0, 8.0)
COMMENT_CHANCE = 0.50  # often open a post and leave without commenting
COMMENT_MAX_PER_ACTIVITY = 1  # at most one browse comment per page so the run stays short
REQUIRE_POST_REPLIES = True  # browse peeks still prefer posts with replies
MIN_POST_REPLIES = 1
# Exception: new posts with 0 comments are OK when intent is help/suggestion/question/review
ALLOW_ZERO_COMMENT_INTENTS = True
LURK_BEFORE_COMMENT = (5.0, 12.0)  # short feed read before a reply
PEEK_THREAD_CHANCE = 0.18  # less peeking — community visits stay short
MIN_SESSION_LEFT_FOR_SUB = 40.0  # skip more communities when time is almost up
MIN_HOME_HOP = 15.0
MIN_LEFTOVER_HOME = 12.0

# Short generic comments. One is picked at random so they are not identical.
GENERAL_COMMENTS: List[str] = [
    "Interesting, thanks for sharing.",
    "This is a solid write-up.",
    "Appreciate the info.",
    "Good point, I had not thought about that.",
    "Nice post, learned something new.",
    "Makes sense. Thanks for posting this.",
    "Helpful, saving this for later.",
    "Yeah this is a good take.",
]

# After activity: submit text posts from posts.csv (title/body/subreddit).
# status draft/empty/retry = ready. LIVE posts are saved to live_posts.xlsx.
POSTS_SHEET = str(_POSTS_SHEET)
POSTS_FILE = POSTS_SHEET
POST_TEMPLATES_DIR = str(_POST_TEMPLATES_DIR)
POSTS_SHEET_ROWS = 12
ACTION_WINDOW_HOURS = 48
ACTION_WINDOW_DAYS = ACTION_WINDOW_HOURS / 24.0
COMMENTS_PER_WINDOW = 2  # sheet + general comments combined
POSTS_PER_WINDOW = 1
POST_COOLDOWN_DAYS = ACTION_WINDOW_DAYS
POST_STATUS_WAIT = 10  # seconds to wait before checking LIVE vs REMOVED
MAX_POST_ATTEMPTS = 1  # one community per run — do not spray fallbacks in one sitting
FALLBACK_SUBREDDITS: List[str] = [
    "python",
    "technology",
    "CasualConversation",
    "NoStupidQuestions",
    "self",
]

# After activity: comment on post links from comments.csv.
# One comment per unique link. Sheet + general comments share the 48h budget.
# After a comment lands, the link is moved to commented_links.csv.
COMMENTS_SHEET = str(_COMMENTS_SHEET)
COMMENTED_LINKS_SHEET = str(_COMMENTED_LINKS_SHEET)
COMMENTS_SHEET_ROWS = 40
COMMENT_COOLDOWN_DAYS = ACTION_WINDOW_DAYS
GENERAL_COMMENT_DAYS = ACTION_WINDOW_DAYS
GENERAL_COMMENTS_PER_WEEK_MIN = 0
GENERAL_COMMENTS_PER_WEEK_MAX = COMMENTS_PER_WINDOW
COMMENT_MAX_PER_WEEK = COMMENTS_PER_WINDOW
SHEET_COMMENTS_PER_RUN = 2  # comments.csv uses the same 48h comment budget
COMMENT_EDIT_WAIT = (180, 240)  # after a comments.csv comment, wait 3–4 min then edit
COMMENTS_PER_RUN = 2
SESSION_GENERAL_COMMENTS = (1, 1)  # one general comment fits a 5–10 min sitting
SESSION_COMMENT_CAP = 2
GENERAL_COMMENT_SUBS_PER_RUN = 4
GENERAL_COMMENT_OPENS_PER_SUB = 3

# Karma growth: read karma + account age, then post/comment in newbie-friendly subs.
KARMA_GROWTH_ENABLED = True
COMMUNITY_GENERAL_POST = True  # when posts.csv has no row, write one post that fits the community
GENERAL_POSTS_PER_WEEK = POSTS_PER_WINDOW
MIN_KARMA_TO_POST = 10  # no post until karma is 10; a community can still require more
GENERAL_COMMENT_MIN_AGE_DAYS = 3  # karma 0 and younger than this: no general comment
GENERAL_POST_DAYS = ACTION_WINDOW_DAYS
KARMA_POST_DAYS = GENERAL_POST_DAYS
KARMA_COMMENT_DAYS = GENERAL_COMMENT_DAYS
KARMA_COMMENT_MIN = GENERAL_COMMENTS_PER_WEEK_MIN
KARMA_COMMENT_MAX = GENERAL_COMMENTS_PER_WEEK_MAX
KARMA_POST_MAX_SUBS = 1  # one community per run
KARMA_POST_RESTRICTED_STOP = 2  # stop hunting after this many restricted skips
# A general post is only written once the community itself has been analysed:
# enough recent posts to see what belongs there, and evidence that text posts
# are normal. Writing blind is how a post lands in an image-only community.
POST_MIN_COMMUNITY_TOPICS = 3  # recent titles needed before writing
POST_MIN_TEXT_SHARE = 0.25  # at least this share of recent posts are text posts
POST_COMMUNITY_SAMPLE = 25  # recent posts sampled per community

# Organic AI comments during browse. Weekly cap above is the real limit.
MAX_AI_COMMENTS_PER_ACCOUNT_DAY = 8
MIN_DELAY_BETWEEN_AI_COMMENTS = 90  # seconds between general comments

# Cross-account spreadsheet retry.
MAX_RETRY_ACCOUNTS = 1

# Deep Q-Network for subreddit choice and comment/skip. Never crashes the bot.
RL_ENABLED = True
RL_MODEL_FILE = str(_RL_MODEL_FILE)
# A steady 30% of actions stay exploratory. The floor equals the start, so a
# model loaded from disk is pulled to 30% rather than keeping an older rate.
RL_EPSILON_INIT = 0.30
RL_EPSILON_MIN = 0.30
RL_EPSILON_DECAY = 1.0
RL_LEARNING_RATE = 0.1
RL_DQN_LR = 0.001
RL_DISCOUNT_FACTOR = 0.9
RL_SAVE_INTERVAL = 10
RL_REPLAY_SIZE = 3000
RL_BATCH_SIZE = 16
RL_TARGET_SYNC = 20
RL_STATUS_MIN_AGE = 20 * 60  # wait before scoring a comment
RL_STATUS_STALE = 3 * 86400
REWARD_POST_LIVE = 10
REWARD_POST_APPROVED = 15
REWARD_COMMENT_POSTED = 2
REWARD_COMMENT_FAILED = -1
REWARD_COMMENT_STILL_LIVE = 1
REWARD_COMMENT_SCORE_2 = 2
REWARD_COMMENT_UPVOTED_5 = 5
REWARD_COMMENT_UPVOTED_20 = 10
REWARD_COMMENT_SCORE_NEG = -2
REWARD_COMMENT_DOWNVOTED = -5
REWARD_COMMENT_REMOVED = -8
REWARD_COMMENT_FILTERED = -3
REWARD_POST_REMOVED_SPAM = -10
REWARD_POST_REMOVED_MOD = -15
REWARD_POST_ALL_FAILED = -20
REWARD_RULES_READ = 0.5
REWARD_JOIN_OK = 1.0
REWARD_JOIN_FAIL = -1.0
REWARD_JOIN_LURK_STRICT = 0.4
REWARD_JOIN_COMMENT_OPEN = 0.2
REWARD_RULES_TONE_CLASH = -0.4
REWARD_RULES_TONE_FIT = 0.2

ADSPOWER_API = os.environ.get("ADSPOWER_API", "http://local.adspower.net:50325").rstrip("/")
ADSPOWER_API_KEY = os.environ.get("ADSPOWER_API_KEY", "").strip()

REQUEST_TIMEOUT = 60
PAGE_LOAD_TIMEOUT = 45
ELEMENT_WAIT = 18
API_MIN_INTERVAL = 1.2  # AdsPower Local API is ~1 request/second
DELAY_BETWEEN_PROFILES = (10.0, 42.0)

# Open every account and run browse/comment/post at the same time.
PARALLEL_PROFILES = True
MAX_PARALLEL_PROFILES = 4  # how many Chrome profiles run at once (rest wait in queue)
PARALLEL_START_STAGGER = (7.0, 28.0)  # seconds between launching each Chrome

# Each account gets its own sitting length and start time this run.
SESSION_SECONDS_RANGE = (5 * 60, 10 * 60)
# AdsPower copies profile files on start. A full /home disk is why browsers
# fail with "Failed to start browser" and logs die with "No space left on device".
MIN_FREE_DISK_MB = 400.0
PROFILE_START_DELAY = (8.0, 90.0)  # wait before this Chrome opens
POST_IN_SESSION_RANGE = (0.35, 0.65)
_tls = threading.local()


def _rng() -> random.Random:
    """This sitting's random stream. Parallel accounts do not share one sequence."""
    return human.rng()


def begin_session_rng(user_id: str = "", label: str = "") -> random.Random:
    return human.begin_session_rng(user_id, label)


def end_session_rng() -> None:
    human.end_session_rng()


def _shift_pair(
    rng: random.Random,
    pair: Tuple[float, float],
    lo_scale: float = 0.72,
    hi_scale: float = 1.38,
    min_lo: Optional[float] = None,
    as_int: bool = False,
) -> Tuple[float, float]:
    a, b = float(pair[0]), float(pair[1])
    mid = ((a + b) / 2.0) * rng.uniform(lo_scale, hi_scale)
    half = ((b - a) / 2.0) * rng.uniform(0.7, 1.35)
    lo, hi = mid - half, mid + half
    if min_lo is not None:
        lo = max(float(min_lo), lo)
    if hi <= lo:
        hi = lo + max(0.2, (b - a) * 0.25)
    if as_int:
        left = max(1, int(round(lo)))
        return (left, max(left + 1, int(round(hi))))
    return (lo, hi)


def _clamp_chance(value: float, low: float = 0.02, high: float = 0.55) -> float:
    return max(low, min(high, float(value)))


@dataclass
class SessionStyle:
    seed: int
    persona: str
    session_seconds: float
    post_fraction: float
    listing_sorts: Tuple[str, ...]
    listing_top_bias: float
    start_with_profile: bool
    home_first_seconds: float
    hop_home_between: bool
    leftover_mode: str
    scroll_down: float
    scroll_up: float
    wander: float
    pulse_every: float
    profile_gap: float
    between_break_chance: float
    subreddit_dwell: Tuple[float, float]
    home_before: Tuple[float, float]
    home_between: Tuple[float, float]
    scroll_tick_px: Tuple[int, int]
    scroll_tick_delay: Tuple[float, float]
    scroll_down_px: Tuple[int, int]
    scroll_up_px: Tuple[int, int]
    read_pause: Tuple[float, float]
    long_read_pause: Tuple[float, float]
    human_break: Tuple[float, float]
    thread_read: Tuple[float, float]
    after_comment: Tuple[float, float]
    comment_think: Tuple[float, float]
    between_comments: Tuple[float, float]
    post_load_wait: Tuple[float, float]
    upvote_chance: float
    click_post_chance: float
    community_post_chance: float
    hover_chance: float
    break_chance: float
    profile_visit_chance: float
    comment_chance: float
    opened_upvote_chance: float
    # Lean toward one scroll engine this sitting, while still mixing both — a
    # single scroll signature on every run is itself a tell.
    smooth_scroll_bias: float = 0.6
    bail_chance: float = 0.12
    home_share: float = HOME_ACTIVITY_SHARE
    community_share: float = MAX_COMMUNITY_SHARE
    home_skip_chance: float = 0.12
    explore_n: int = 2
    search_n: int = 1
    type_char: Tuple[float, float] = (0.03, 0.11)
    type_space: Tuple[float, float] = (0.05, 0.18)
    type_punct: Tuple[float, float] = (0.16, 0.48)
    type_nl: Tuple[float, float] = (0.18, 0.45)

    def summary(self) -> str:
        sorts = "→".join(self.listing_sorts)
        return (
            f"{self.session_seconds / 60:.0f} min, Home {self.home_first_seconds / 60:.1f} min first, "
            f"{self.persona}, listings {sorts}"
        )

    def fingerprint(self, subs: List[str]) -> str:
        names = ",".join(normalize_subreddit(name).lower() for name in subs if name)
        return "|".join(
            (
                self.persona,
                f"t{int(self.session_seconds) // 60}",
                f"hf{int(self.home_first_seconds) // 10}",
                f"hb{int(self.home_between[0]) // 10}-{int(self.home_between[1]) // 10}",
                f"sd{int(self.subreddit_dwell[0]) // 5}",
                f"m{int(self.scroll_down * 10)}{int(self.scroll_up * 10)}",
                f"ss{int(self.smooth_scroll_bias * 20)}",
                f"hs{int(self.home_share * 100)}",
                f"ex{self.explore_n}q{self.search_n}",
                f"skip{int(self.home_skip_chance * 20)}",
                "homehop" if self.hop_home_between else "subchain",
                "→".join(self.listing_sorts),
                self.leftover_mode,
                f"u{int(self.upvote_chance * 100)}c{int(self.click_post_chance * 100)}",
                names,
            )
        )


def roll_session_style(user_id: str = "", label: str = "") -> SessionStyle:
    """Fresh timing and browse mix for this account sitting."""
    rng = _rng()
    seed = human.session_seed() or secrets.randbits(64)
    persona = rng.choice(("slow lurker", "curious", "restless", "steady"))
    session_seconds = rng.uniform(*SESSION_SECONDS_RANGE)
    post_fraction = rng.uniform(*POST_IN_SESSION_RANGE)
    listing_sorts = ("new",)
    upvote = UPVOTE_CHANCE
    click = CLICK_POST_CHANCE
    community = COMMUNITY_POST_CHANCE
    hover = HOVER_CHANCE
    brk = HUMAN_BREAK_CHANCE
    profile = PROFILE_VISIT_CHANCE
    comment = COMMENT_CHANCE
    opened_up = 0.45
    dwell = ACTIVITY_ON_SUBREDDIT
    read = READ_PAUSE
    long_read = LONG_READ_PAUSE
    thread = THREAD_READ
    if persona == "slow lurker":
        span = SESSION_SECONDS_RANGE[1] - SESSION_SECONDS_RANGE[0]
        session_seconds = rng.uniform(
            SESSION_SECONDS_RANGE[0] + span * 0.4, SESSION_SECONDS_RANGE[1]
        )
        upvote *= 0.55
        click *= 0.5
        community *= 0.55
        brk *= 1.4
        read = _shift_pair(rng, READ_PAUSE, 1.1, 1.5, min_lo=1.2)
        long_read = _shift_pair(rng, LONG_READ_PAUSE, 1.05, 1.45, min_lo=3.0)
        thread = _shift_pair(rng, THREAD_READ, 0.95, 1.05, min_lo=4.0)
        dwell = _shift_pair(
            rng, ACTIVITY_ON_SUBREDDIT, 1.0, 1.2, min_lo=MIN_SUBREDDIT_DWELL
        )
    elif persona == "curious":
        upvote *= 1.6
        click *= 1.7
        community *= 1.55
        hover *= 1.4
        comment *= 1.15
        opened_up = 0.62
        dwell = _shift_pair(
            rng, ACTIVITY_ON_SUBREDDIT, 0.85, 1.15, min_lo=MIN_SUBREDDIT_DWELL
        )
    elif persona == "restless":
        span = SESSION_SECONDS_RANGE[1] - SESSION_SECONDS_RANGE[0]
        session_seconds = rng.uniform(
            SESSION_SECONDS_RANGE[0], SESSION_SECONDS_RANGE[0] + span * 0.4
        )
        brk *= 1.7
        click *= 1.15
        read = _shift_pair(rng, READ_PAUSE, 0.65, 0.95, min_lo=0.9)
        long_read = _shift_pair(rng, LONG_READ_PAUSE, 0.6, 0.95, min_lo=2.0)
        dwell = _shift_pair(
            rng, ACTIVITY_ON_SUBREDDIT, 0.7, 1.0, min_lo=MIN_SUBREDDIT_DWELL
        )
    else:
        dwell = _shift_pair(
            rng, ACTIVITY_ON_SUBREDDIT, 0.85, 1.15, min_lo=MIN_SUBREDDIT_DWELL
        )
        read = _shift_pair(rng, READ_PAUSE, 0.85, 1.2, min_lo=1.0)
        long_read = _shift_pair(rng, LONG_READ_PAUSE, 0.85, 1.2, min_lo=2.5)
        thread = _shift_pair(rng, THREAD_READ, 0.95, 1.05, min_lo=4.0)
    down, up, wander = 0.58, 0.16, 0.08
    mix = rng.random()
    if mix < 0.34:
        down, up, wander = 0.70, 0.10, 0.06
    elif mix < 0.62:
        down, up, wander = 0.46, 0.22, 0.14
    elif mix < 0.80:
        down, up, wander = 0.52, 0.12, 0.18
    leftover_mode = rng.choice(("even", "weighted", "one_long"))
    home_share = rng.uniform(*HOME_ACTIVITY_SHARE_RANGE)
    community_share = rng.uniform(*MAX_COMMUNITY_SHARE_RANGE)
    community_share = min(community_share, max(0.12, 0.92 - home_share))
    return SessionStyle(
        seed=seed,
        persona=persona,
        session_seconds=session_seconds,
        post_fraction=post_fraction,
        listing_sorts=listing_sorts,
        listing_top_bias=rng.uniform(0.22, 0.92),
        start_with_profile=rng.random() < 0.18,
        home_first_seconds=rng.uniform(*HOME_ACTIVITY_BEFORE_JOIN),
        hop_home_between=rng.random() < 0.82,
        leftover_mode=leftover_mode,
        scroll_down=down,
        scroll_up=up,
        wander=wander,
        pulse_every=rng.uniform(12.0, 28.0),
        profile_gap=rng.uniform(45.0, 120.0),
        between_break_chance=rng.uniform(0.18, 0.45),
        subreddit_dwell=dwell,
        home_before=_shift_pair(rng, HOME_ACTIVITY_BEFORE_JOIN, 0.92, 1.08, min_lo=60.0),
        home_between=_shift_pair(rng, HOME_BETWEEN_SUBS, 0.85, 1.2, min_lo=MIN_HOME_HOP),
        # Keep scroll ticks small and the cadence tight so motion stays smooth
        scroll_tick_px=_shift_pair(rng, SCROLL_TICK_PX, 0.9, 1.1, min_lo=12, as_int=True),
        scroll_tick_delay=_shift_pair(rng, SCROLL_TICK_DELAY, 0.9, 1.15, min_lo=0.05),
        scroll_down_px=_shift_pair(rng, SCROLL_DOWN_PX, 0.8, 1.2, min_lo=90, as_int=True),
        scroll_up_px=_shift_pair(rng, SCROLL_UP_PX, 0.8, 1.2, min_lo=35, as_int=True),
        read_pause=read,
        long_read_pause=long_read,
        human_break=_shift_pair(rng, HUMAN_BREAK, 0.7, 1.4, min_lo=5.0),
        thread_read=_shift_pair(rng, THREAD_READ, 0.95, 1.05, min_lo=4.0),
        after_comment=_shift_pair(rng, AFTER_COMMENT_LINGER, 0.9, 1.15, min_lo=4.0),
        comment_think=_shift_pair(rng, COMMENT_THINK, 0.7, 1.4, min_lo=1.2),
        between_comments=_shift_pair(rng, BETWEEN_COMMENTS, 0.75, 1.45, min_lo=18.0),
        post_load_wait=_shift_pair(rng, POST_LOAD_WAIT, 0.8, 1.2, min_lo=2.0),
        upvote_chance=_clamp_chance(upvote * rng.uniform(0.7, 1.35), 0.03, 0.28),
        click_post_chance=_clamp_chance(click * rng.uniform(0.65, 1.4), 0.08, 0.42),
        community_post_chance=_clamp_chance(community * rng.uniform(0.65, 1.4), 0.12, 0.55),
        hover_chance=_clamp_chance(hover * rng.uniform(0.6, 1.45), 0.08, 0.5),
        break_chance=_clamp_chance(brk * rng.uniform(0.7, 1.5), 0.04, 0.28),
        profile_visit_chance=_clamp_chance(profile * rng.uniform(0.5, 1.8), 0.03, 0.22),
        comment_chance=_clamp_chance(comment * rng.uniform(0.75, 1.2), 0.25, 0.75),
        opened_upvote_chance=_clamp_chance(opened_up * rng.uniform(0.6, 1.3), 0.15, 0.75),
        smooth_scroll_bias=0.45 + rng.random() * 0.35,
        bail_chance=rng.uniform(0.06, 0.18),
        home_share=home_share,
        community_share=community_share,
        home_skip_chance=rng.uniform(0.04, 0.28),
        explore_n=rng.randint(*RANDOM_EXPLORE_PER_SESSION),
        search_n=rng.randint(*SEARCHES_PER_SESSION),
        type_char=_shift_pair(rng, (0.03, 0.11), 0.65, 1.45, min_lo=0.018),
        type_space=_shift_pair(rng, (0.05, 0.18), 0.7, 1.4, min_lo=0.03),
        type_punct=_shift_pair(rng, (0.16, 0.48), 0.7, 1.4, min_lo=0.1),
        type_nl=_shift_pair(rng, (0.18, 0.45), 0.7, 1.4, min_lo=0.12),
    )


def current_style() -> Optional[SessionStyle]:
    return getattr(_tls, "style", None)


def set_current_style(style: Optional[SessionStyle]) -> None:
    _tls.style = style


def _style() -> Optional[SessionStyle]:
    return current_style()


def _load_session_patterns() -> Dict[str, Any]:
    try:
        with open(str(_SESSION_PATTERNS_PATH), encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_session_patterns(data: Dict[str, Any]) -> None:
    path = str(_SESSION_PATTERNS_PATH)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
    os.replace(tmp, path)


def pattern_already_used(user_id: str, fingerprint: str) -> bool:
    data = _load_session_patterns()
    used = data.get(user_id) if isinstance(data.get(user_id), list) else []
    return fingerprint in used


def remember_session_pattern(user_id: str, fingerprint: str) -> None:
    if not user_id or not fingerprint:
        return
    data = _load_session_patterns()
    used = [str(item) for item in (data.get(user_id) or []) if item]
    if fingerprint not in used:
        used.append(fingerprint)
    data[user_id] = used[-80:]
    try:
        _save_session_patterns(data)
    except Exception:
        pass


def unique_session_plan(
    user_id: str,
    label: str,
    karma: int = 0,
    age_days: float = 0.0,
    assigned_seconds: Optional[float] = None,
) -> Tuple[SessionStyle, List[str], str]:
    """Roll a sitting that this account has not used before."""
    last_style = roll_session_style(user_id, label)
    last_subs = pick_session_subreddits(user_id, karma, age_days)
    last_fp = last_style.fingerprint(last_subs)

    def _apply(style: SessionStyle) -> SessionStyle:
        if assigned_seconds:
            lo, hi = SESSION_SECONDS_RANGE
            jitter = _rng().uniform(-22.0, 22.0)
            style.session_seconds = min(
                float(hi), max(float(lo), float(assigned_seconds) + jitter)
            )
        style.home_first_seconds = _rng().uniform(*style.home_before)
        return style

    for _ in range(28):
        style = _apply(roll_session_style(user_id, label))
        subs = pick_session_subreddits(user_id, karma, age_days)
        fingerprint = style.fingerprint(subs)
        if not pattern_already_used(user_id, fingerprint):
            remember_session_pattern(user_id, fingerprint)
            return style, subs, fingerprint
        last_style, last_subs, last_fp = style, subs, fingerprint
    last_style = _apply(last_style)
    last_style.leftover_mode = f"{last_style.leftover_mode}-{secrets.token_hex(2)}"
    last_fp = last_style.fingerprint(last_subs)
    remember_session_pattern(user_id, last_fp)
    return last_style, last_subs, last_fp


def assign_profile_schedules(
    targets: List[Dict[str, str]],
) -> Dict[str, Dict[str, float]]:
    """Give every profile a distinct sitting length and start time."""
    if not targets:
        return {}
    lo_min = SESSION_SECONDS_RANGE[0] / 60.0
    hi_min = SESSION_SECONDS_RANGE[1] / 60.0
    # Keep sittings distinct inside the 5–10 min band (~20s apart).
    min_gap_min = 0.35
    minutes: List[float] = []
    for _ in targets:
        picked = None
        for _try in range(36):
            cand = _rng().uniform(lo_min, hi_min)
            if all(abs(cand - other) >= min_gap_min for other in minutes):
                picked = cand
                break
        minutes.append(picked if picked is not None else _rng().uniform(lo_min, hi_min))
    n = len(targets)
    delay_hi = 40.0 if n == 1 else min(float(PROFILE_START_DELAY[1]), 25.0 + n * 35.0)
    delays: List[float] = []
    for _ in targets:
        picked = None
        for _try in range(36):
            cand = _rng().uniform(0.0, delay_hi)
            if all(abs(cand - other) >= 8.0 for other in delays):
                picked = cand
                break
        delays.append(picked if picked is not None else _rng().uniform(0.0, delay_hi))
    out: Dict[str, Dict[str, float]] = {}
    for profile, mins, delay in zip(targets, minutes, delays):
        out[profile["user_id"]] = {
            "session_seconds": float(mins) * 60.0,
            "start_delay": float(delay),
        }
    return out


def plan_home_budget(
    session_seconds: float,
    hop_count: int,
    first_seconds: Optional[float] = None,
    home_share: Optional[float] = None,
) -> Tuple[float, Tuple[float, float]]:
    """
    Split a sitting so this session's Home share happens on Reddit Home.
    Returns (first Home stretch, per-hop Home range). Whatever is left over
    at the end of the sitting also runs on Home, so the share holds even when
    a community visit is cut short. Pass first_seconds to re-split the rest
    once the real hop count is known.
    """
    total = max(60.0, float(session_seconds))
    share = HOME_ACTIVITY_SHARE if home_share is None else float(home_share)
    share = max(HOME_ACTIVITY_SHARE_RANGE[0], min(HOME_ACTIVITY_SHARE_RANGE[1], share))
    home_total = total * share
    hops = max(0, int(hop_count))
    # Most Home time goes up front; the rest is split across the hops back
    if first_seconds is None:
        first = home_total * _rng().uniform(0.42, 0.72)
    else:
        first = min(float(first_seconds), home_total)
    between_total = max(0.0, home_total - first)
    if hops <= 0:
        return first, (MIN_HOME_HOP, MIN_HOME_HOP * 1.5)
    per_hop = max(MIN_HOME_HOP, between_total / float(hops))
    return first, (per_hop * 0.7, per_hop * 1.4)


def community_dwell_seconds(style: Optional[SessionStyle], remaining: float) -> float:
    """Short community visit — capped so Home keeps its share of the sitting."""
    window = style.subreddit_dwell if style else ACTIVITY_ON_SUBREDDIT
    dwell = _rng().uniform(*window)
    dwell = min(dwell, MAX_SUBREDDIT_DWELL)
    # Always leave room for the Home hop that follows
    dwell = min(dwell, max(MIN_SUBREDDIT_DWELL, remaining - MIN_HOME_HOP))
    return max(MIN_SUBREDDIT_DWELL, dwell)


def hop_reddit_home(
    driver: WebDriver,
    label: str,
    user_id: str,
    stats: AccountSummary,
    seconds: float,
    reason: str,
) -> float:
    """Random Home activity — always between communities. Returns seconds used."""
    dwell = max(MIN_HOME_HOP, float(seconds))
    if dwell >= 90:
        log(f"[Profile {label}] {dwell / 60:.1f} min random activity on Home ({reason})")
    else:
        log(f"[Profile {label}] {dwell:.0f}s random activity on Home ({reason})")
    return_to_reddit_home(driver, label, reason=reason)
    perform_browse_activity(
        driver,
        dwell,
        label,
        user_id=user_id,
        stats=stats,
        stay_url=REDDIT_HOME_URL,
    )
    return dwell


def community_entry_url(subreddit: str) -> str:
    """Open a community through a different listing each time before Join."""
    name = normalize_subreddit(subreddit)
    route = _rng().choice(("front", "new", "hot", "rising", "top"))
    if route == "front":
        return f"https://www.reddit.com/r/{name}/"
    return f"https://www.reddit.com/r/{name}/{route}/"


def visit_sheet_post_community(
    driver: WebDriver,
    label: str,
    user_id: str,
    stats: AccountSummary,
    subreddit: str,
    seconds: float,
    *,
    join: bool,
    reason: str,
) -> float:
    """Open the posts.csv community, optionally Join, then the usual browse. No post."""
    name = normalize_subreddit(subreddit)
    if not name:
        return 0.0
    started = time.time()
    url = community_entry_url(name)
    log(f"[Profile {label}] {reason} — r/{name} via {url}")
    raise_profile_browser(label)
    if not open_exclusive_community(driver, label, user_id, name, url):
        return 0.0
    time.sleep(_rng().uniform(1.1, 2.8))
    dismiss_popups(driver)
    try:
        WebDriverWait(driver, ELEMENT_WAIT).until(
            EC.presence_of_element_located((By.TAG_NAME, "body"))
        )
    except TimeoutException:
        log(f"[Profile {label}] r/{name} body did not appear — continuing")
    try:
        read_subreddit_rules(driver, label, name, stats, open_page=join)
    except Exception:
        pass
    if join:
        try:
            _record_join(stats, name, join_subreddit(driver, label, name))
        except Exception as exc:
            log(
                f"[Profile {label}] Join on r/{name} skipped "
                f"({brief_error(exc)}) — still browsing"
            )
    dwell = max(MIN_SUBREDDIT_DWELL, min(float(seconds), MAX_SUBREDDIT_DWELL))
    log(f"[Profile {label}] {dwell:.0f}s in r/{name} ({reason})")
    perform_browse_activity(
        driver,
        dwell,
        label,
        user_id=user_id,
        stats=stats,
        stay_url=url,
    )
    return time.time() - started


def plan_sheet_post_return_hops(hops: List[str], target: str) -> List[str]:
    """Keep other hops as they are; visit the post community later, not first."""
    name = normalize_subreddit(target)
    if not name:
        return list(hops)
    others = [item for item in hops if item.lower() != name.lower()]
    if not others:
        return [name]
    # 0 = after the Home gap only; len(others) = after every other community.
    insert_at = _rng().randint(0, len(others))
    return others[:insert_at] + [name] + others[insert_at:]


_SKIP_EXPLORE_SUBS = {
    "all",
    "popular",
    "random",
    "randnsfw",
    "home",
    "friends",
    "mod",
    "reddit.com",
    "announcements",
    "blog",
    "help",
} | BLOCKED_SUBREDDITS

# One community open per batch. Parallel accounts wait or skip instead of
# sitting in the same subreddit at the same time.
_SUBREDDIT_BUSY: Dict[str, str] = {}
_SUBREDDIT_BUSY_COND = threading.Condition(threading.Lock())
SUBREDDIT_BUSY_WAIT = 90.0


def _community_key(name: str) -> str:
    return normalize_subreddit(name).lower()


def _community_is_shared_listing(name: str) -> bool:
    key = _community_key(name)
    return not key or key in _SKIP_EXPLORE_SUBS


def subreddit_holder(name: str) -> str:
    """AdsPower id of the account currently inside this community, if any."""
    key = _community_key(name)
    if _community_is_shared_listing(name):
        return ""
    with _SUBREDDIT_BUSY_COND:
        return str(_SUBREDDIT_BUSY.get(key) or "")


def claim_subreddit(name: str, user_id: str, *, wait: float = 0.0) -> bool:
    """Hold this community for one account. Same account may claim it again."""
    key = _community_key(name)
    owner = str(user_id or "")
    if _community_is_shared_listing(name) or not owner:
        return True
    deadline = time.time() + max(0.0, float(wait))
    with _SUBREDDIT_BUSY_COND:
        while True:
            holder = _SUBREDDIT_BUSY.get(key)
            if holder is None or holder == owner:
                _SUBREDDIT_BUSY[key] = owner
                return True
            left = deadline - time.time()
            if left <= 0:
                return False
            _SUBREDDIT_BUSY_COND.wait(timeout=left)


def release_all_held_subreddits(user_id: str = "", *, keep: str = "") -> None:
    """Free communities this account is holding. `keep` stays claimed."""
    owner = str(user_id or getattr(_tls, "batch_user_id", "") or "")
    if not owner:
        return
    stay = _community_key(keep)
    with _SUBREDDIT_BUSY_COND:
        released = False
        for key, holder in list(_SUBREDDIT_BUSY.items()):
            if holder == owner and key != stay:
                _SUBREDDIT_BUSY.pop(key, None)
                released = True
        if released:
            _SUBREDDIT_BUSY_COND.notify_all()


def held_subreddits(user_id: str) -> List[str]:
    owner = str(user_id or "")
    if not owner:
        return []
    with _SUBREDDIT_BUSY_COND:
        return [key for key, holder in _SUBREDDIT_BUSY.items() if holder == owner]


def open_exclusive_community(
    driver: WebDriver,
    label: str,
    user_id: str,
    subreddit: str,
    url: str,
    *,
    wait: float = SUBREDDIT_BUSY_WAIT,
) -> bool:
    """Open a community only when no other account in this batch is inside it."""
    name = normalize_subreddit(subreddit)
    if not name:
        return False
    # Leave the previous community before waiting, so two accounts cannot
    # each hold the subreddit the other one wants.
    if any(key != _community_key(name) for key in held_subreddits(user_id)):
        return_to_reddit_home(
            driver,
            label,
            reason="freeing the previous community for the other accounts",
        )
    if not claim_subreddit(name, user_id, wait=wait):
        holder = subreddit_holder(name)
        who = f" ({holder})" if holder else ""
        log(
            f"[Profile {label}] r/{name} is already open on another account{who} "
            "— not opening it at the same time"
        )
        return False
    navigate(driver, url, label)
    release_all_held_subreddits(user_id, keep=name)
    return True


_FEED_SUB_JS = r"""
const names = [];
const seen = new Set();
function add(href) {
  const m = String(href || '').match(/\/r\/([A-Za-z0-9_]+)/i);
  if (!m) return;
  const n = m[1];
  const low = n.toLowerCase();
  if (seen.has(low)) return;
  seen.add(low);
  names.push(n);
}
function walk(root) {
  try {
    root.querySelectorAll('a[href*="/r/"]').forEach(a => add(a.getAttribute('href')));
  } catch (e) {}
  try {
    root.querySelectorAll('*').forEach(el => { if (el.shadowRoot) walk(el.shadowRoot); });
  } catch (e) {}
}
walk(document);
return names.slice(0, 30);
"""

_RELATED_SUB_JS = r"""
const names = [];
const seen = new Set();
function add(href) {
  const m = String(href || '').match(/\/r\/([A-Za-z0-9_]+)/i);
  if (!m) return;
  const n = m[1];
  const low = n.toLowerCase();
  if (seen.has(low)) return;
  seen.add(low);
  names.push(n);
}
function walk(root) {
  let nodes = [];
  try { nodes = root.querySelectorAll('a[href*="/r/"]'); } catch (e) { nodes = []; }
  nodes.forEach(a => {
    let blob = '';
    try {
      const box = (a.closest && a.closest('aside, section, nav, li')) || a.parentElement;
      blob = ((box && (box.innerText || box.textContent)) || '').slice(0, 120).toLowerCase();
    } catch (e) {}
    const label = ((a.getAttribute('aria-label') || '') + ' ' + (a.innerText || a.textContent || '')).toLowerCase();
    if (blob.includes('related') || blob.includes('similar') || label.includes('related') || label.includes('similar')) {
      add(a.getAttribute('href'));
    }
  });
  try {
    root.querySelectorAll('*').forEach(el => { if (el.shadowRoot) walk(el.shadowRoot); });
  } catch (e) {}
}
walk(document);
if (!names.length) {
  function walkAside(root) {
    try {
      root.querySelectorAll('aside a[href*="/r/"], [role="complementary"] a[href*="/r/"]').forEach(a => add(a.getAttribute('href')));
    } catch (e) {}
    try {
      root.querySelectorAll('*').forEach(el => { if (el.shadowRoot) walkAside(el.shadowRoot); });
    } catch (e) {}
  }
  walkAside(document);
}
return names.slice(0, 20);
"""


def _explore_pool(karma: int = 0, age_days: float = 0.0) -> List[str]:
    try:
        from reddit_joiner.karma import candidate_subreddits

        return list(candidate_subreddits(karma, age_days))
    except Exception:
        return [
            "CasualConversation",
            "NoStupidQuestions",
            "mildlyinteresting",
            "books",
            "movies",
            "hobbies",
            "OutOfTheLoop",
            "DoesAnybodyElse",
        ]


def _usable_explore_name(name: str, avoid: set) -> str:
    clean = normalize_subreddit(name)
    key = clean.lower()
    if not clean or key in avoid or key in _SKIP_EXPLORE_SUBS:
        return ""
    if not re.match(r"^[A-Za-z0-9_]{3,21}$", clean):
        return ""
    return clean


def collect_feed_subreddits(driver: WebDriver) -> List[str]:
    try:
        names = driver.execute_script(_FEED_SUB_JS) or []
    except Exception:
        return []
    found: List[str] = []
    seen = set()
    for raw in names:
        name = normalize_subreddit(str(raw or ""))
        key = name.lower()
        if not name or key in seen or key in _SKIP_EXPLORE_SUBS:
            continue
        seen.add(key)
        found.append(name)
    return found


def discover_explore_subreddits(
    driver: WebDriver,
    label: str,
    avoid: Optional[Iterable[str]] = None,
    want: int = 2,
    karma: int = 0,
    age_days: float = 0.0,
    seen: Optional[Iterable[str]] = None,
    user_id: str = "",
) -> List[str]:
    """Find communities the way a person would, never from r/random or a fixed list.

    Each sitting uses a few of these: a community already on screen, a keyword
    search, the search-box suggestions, related communities on the page, or
    another feed (Popular, All, or Rising).

    `avoid` is hard: this run's sheet communities and anything already joined
    never come back. `seen` is the one-sitting cooldown — last session's
    explores are held back, then allowed again the sitting after that.
    """
    need = max(0, int(want))
    if need <= 0:
        return []
    hard = {normalize_subreddit(str(item or "")).lower() for item in (avoid or [])}
    hard.update(_SKIP_EXPLORE_SUBS)
    hard.discard("")
    soft = {normalize_subreddit(str(item or "")).lower() for item in (seen or [])}
    soft.discard("")
    skip = hard | soft
    found: List[str] = []
    seen_during_activity = collect_feed_subreddits(driver)

    def _take(name: str) -> bool:
        clean = _usable_explore_name(name, skip)
        if not clean:
            return False
        found.append(clean)
        skip.add(clean.lower())
        return True

    # Each sitting uses a few of these. Never r/random and never a fixed name list.
    catalog = ["activity", "search", "suggestions", "related", "listing"]
    _rng().shuffle(catalog)
    sources = catalog[: _rng().randint(2, 4)]
    quotas: Dict[str, int] = {}
    remaining = need
    for index, source in enumerate(sources):
        if remaining <= 0:
            quotas[source] = 0
            continue
        if index == len(sources) - 1:
            quotas[source] = remaining
            remaining = 0
            continue
        if remaining == 1:
            take_n = 1 if _rng().random() < 0.5 else 0
        else:
            take_n = _rng().randint(1, remaining - 1)
        quotas[source] = take_n
        remaining -= take_n
    log(
        f"[Profile {label}] Finding communities this sitting by "
        + " and ".join(f"{source}×{quotas[source]}" for source in sources if quotas[source])
    )

    def _room(source: str) -> int:
        return quotas.get(source, 0)

    def _from_activity() -> None:
        added = 0
        names = list(seen_during_activity)
        _rng().shuffle(names)
        for name in names:
            if added >= _room("activity") or len(found) >= need:
                return
            if _take(name):
                added += 1
                log(f"[Profile {label}] Saw r/{name} during activity — will join it")

    def _search_key() -> str:
        try:
            pool = _search_query_pool(None)
        except Exception:
            pool = []
        phrase = pool[0] if pool else "beginner advice"
        words = [word for word in phrase.split() if word]
        if len(words) > 2:
            phrase = " ".join(words[: _rng().randint(1, 2)])
        return phrase

    def _from_search() -> None:
        added = 0
        if _room("search") <= 0:
            return
        key = _search_key()
        urls = (
            "https://www.reddit.com/search/?q=" + quote_plus(key) + "&type=communities",
            "https://www.reddit.com/search/?q=" + quote_plus(key) + "&type=link&sort=new",
        )
        names: List[str] = []
        for url in urls:
            try:
                navigate(driver, url, label)
                time.sleep(_rng().uniform(1.8, 3.4))
                dismiss_popups(driver)
            except Exception as exc:
                log(f"[Profile {label}] Community search for \"{key}\" skipped ({brief_error(exc)})")
                continue
            for name in collect_feed_subreddits(driver):
                if name.lower() not in {item.lower() for item in names}:
                    names.append(name)
            if len(names) >= _room("search"):
                break
        _rng().shuffle(names)
        log(f"[Profile {label}] Searched communities for \"{key}\"")
        for name in names:
            if added >= _room("search") or len(found) >= need:
                return
            if _take(name):
                added += 1
                log(f"[Profile {label}] Search for \"{key}\" found r/{name} — will join it")

    def _from_suggestions() -> None:
        added = 0
        if _room("suggestions") <= 0:
            return
        key = _search_key()
        try:
            if "/search" in (driver.current_url or ""):
                navigate(driver, REDDIT_HOME_URL, label)
                time.sleep(_rng().uniform(1.0, 2.0))
            focused = driver.execute_script(_SEARCH_FOCUS_JS) == "focused"
        except Exception as exc:
            log(f"[Profile {label}] Search suggestions skipped ({brief_error(exc)})")
            return
        if not focused:
            log(f"[Profile {label}] Search box was not open — suggestions skipped")
            return
        try:
            time.sleep(_rng().uniform(0.3, 0.8))
            _type_into_focused(driver, key)
            time.sleep(_rng().uniform(1.0, 1.8))
        except Exception as exc:
            log(f"[Profile {label}] Could not type \"{key}\" for suggestions ({brief_error(exc)})")
            return
        names = collect_feed_subreddits(driver)
        try:
            ActionChains(driver).send_keys(Keys.ESCAPE).perform()
        except Exception:
            pass
        _rng().shuffle(names)
        log(f"[Profile {label}] Typed \"{key}\" and read the search suggestions")
        for name in names:
            if added >= _room("suggestions") or len(found) >= need:
                return
            if _take(name):
                added += 1
                log(f"[Profile {label}] Suggestion for \"{key}\" showed r/{name} — will join it")

    def _from_related() -> None:
        added = 0
        if _room("related") <= 0:
            return
        try:
            raw = driver.execute_script(_RELATED_SUB_JS) or []
        except Exception:
            raw = []
        names = [str(item) for item in raw]
        _rng().shuffle(names)
        for name in names:
            if added >= _room("related") or len(found) >= need:
                return
            if _take(str(name)):
                added += 1
                log(f"[Profile {label}] Related list showed r/{name} — will join it")

    def _from_listing() -> None:
        added = 0
        if _room("listing") <= 0:
            return
        title, url = _rng().choice(
            (
                ("Popular", "https://www.reddit.com/r/popular/"),
                ("All", "https://www.reddit.com/r/all/"),
                ("Rising", "https://www.reddit.com/r/all/rising/"),
            )
        )
        try:
            navigate(driver, url, label)
            time.sleep(_rng().uniform(1.8, 3.2))
            dismiss_popups(driver)
        except Exception as exc:
            log(f"[Profile {label}] {title} feed skipped ({brief_error(exc)})")
            return
        names = collect_feed_subreddits(driver)
        _rng().shuffle(names)
        log(f"[Profile {label}] Looking at the {title} feed for a community")
        for name in names:
            if added >= _room("listing") or len(found) >= need:
                return
            if _take(name):
                added += 1
                log(f"[Profile {label}] Saw r/{name} on {title} — will join it")

    starters = {
        "activity": _from_activity,
        "search": _from_search,
        "suggestions": _from_suggestions,
        "related": _from_related,
        "listing": _from_listing,
    }
    for source in sources:
        if len(found) >= need:
            break
        before = len(found)
        starters[source]()
        short = _room(source) - (len(found) - before)
        if short > 0:
            for later in sources[sources.index(source) + 1 :]:
                quotas[later] = quotas.get(later, 0) + short
                break

    if len(found) < need and soft:
        relaxed = set(hard)
        relaxed.update(item.lower() for item in found)
        names = collect_feed_subreddits(driver)
        _rng().shuffle(names)
        for name in names:
            if len(found) >= need:
                break
            clean = _usable_explore_name(name, relaxed)
            if not clean:
                continue
            found.append(clean)
            relaxed.add(clean.lower())
            log(f"[Profile {label}] Saw r/{clean} again during activity — will join it")

    return found[:need]


# After clicking Open, AdsPower assigns then refreshes the proxy IP.
# Do not attach Selenium or open Reddit until that finishes.
PROXY_ASSIGN_WAIT = (8.0, 12.0)
PROXY_REFRESH_WAIT = (6.0, 10.0)
PROXY_READY_TIMEOUT = 90.0
PROXY_SETTLE_DELAY = (3.0, 6.0)

REDDIT_HOME_URL = "https://www.reddit.com/"
PROFILE_CACHE_PATH = str(_PROFILE_CACHE_PATH)
PROFILE_CACHE_TTL = 6 * 60 * 60
POST_LOG_PATH = str(_POST_LOG_PATH)
COMMENT_LOG_PATH = str(_COMMENT_LOG_PATH)
SUBREDDIT_LOG_PATH = str(_SUBREDDIT_LOG_PATH)
SESSION_PATTERNS_PATH = str(_SESSION_PATTERNS_PATH)
SUMMARIES_DIR = str(_SUMMARIES_DIR)
POSTS_SHEET_FIELDS = [
    "post",
    "title",
    "body",
    "body_file",
    "subreddit",
    "flair",
    "account",
    "status",
    "posted_by",
    "posted_at",
    "posted_subreddit",
]
_POSTS_SHEET_ALIASES = {
    "title": ("title", "post_title", "post title"),
    "body": ("body", "post_body", "post body", "text", "content"),
    "body_file": ("body_file", "body file", "template", "template_file", "body_path"),
    "subreddit": ("subreddit", "subreddits", "sub", "community", "communities"),
    "flair": ("flair", "tag", "post_flair", "post flair", "link_flair"),
    "account": ("account", "profile", "user", "user_id"),
    "status": ("status", "state"),
    "posted_by": ("posted_by", "posted by"),
    "posted_at": ("posted_at", "posted at"),
    "posted_subreddit": ("posted_subreddit", "posted subreddit"),
}
_SUBREDDIT_COL_RE = re.compile(
    r"^post_?(\d+)(?:_?(?:subreddit|sub))?$|"
    r"^(?:subreddit|sub|community)_?(\d+)$",
    re.I,
)
_TRACKING_COLS = {
    "post",
    "title",
    "body",
    "body_file",
    "template",
    "template_file",
    "body_path",
    "account",
    "status",
    "posted_by",
    "posted_at",
    "posted_subreddit",
    "flair",
    "tag",
    "post_flair",
    "link_flair",
    "post_title",
    "post_body",
    "text",
    "content",
}
_POST_DONE_STATUS = {"posted", "done", "used", "live"}
_POST_SKIP_STATUS = {"skip", "skipped"}
_POST_FAILED_STATUS = {"failed", "retry"}
COMMENTS_SHEET_FIELDS = [
    "comment",
    "link",
    "account",
    "text",
    "edit",
    "status",
    "posted_by",
    "posted_at",
]
COMMENTED_LINKS_FIELDS = [
    "commented_at",
    "comment",
    "link",
    "comment_url",
    "account",
    "posted_by",
    "text",
    "edit",
]
_COMMENTS_SHEET_ALIASES = {
    "link": ("link", "url", "post_link", "post url", "permalink"),
    "account": ("account", "profile", "user", "user_id", "ads_power"),
    "text": ("text", "comment_text", "comment text", "body"),
    "edit": ("edit", "edit_text", "edit text", "add", "followup", "follow_up"),
    "status": ("status", "state"),
    "posted_by": ("posted_by", "posted by"),
    "posted_at": ("posted_at", "posted at"),
}

# Remember the AdsPower manager window so we never scroll/raise it during Reddit activity.
_ADSPOWER_MANAGER_WID = ""


@dataclass
class AccountSummary:
    name: str
    user_id: str
    upvotes: int = 0
    comments: int = 0
    sheet_comments: int = 0
    comment_note: str = ""
    posted: str = ""
    post_note: str = ""
    post_status: str = "none"
    post_title: str = ""
    post_url: str = ""
    ai_comments: int = 0
    live_posts: List[str] = field(default_factory=list)
    posts_removed: int = 0
    upvotes_received: int = 0
    joined: List[str] = field(default_factory=list)
    already_member: List[str] = field(default_factory=list)
    join_failed: List[str] = field(default_factory=list)
    reddit_username: str = ""
    account_status: str = ""
    account_karma: int = 0
    account_age_days: float = 0.0
    karma_tier: str = ""
    karma_comments: int = 0
    session_comment_target: int = 0
    karma_comment_note: str = ""
    karma_post_status: str = "none"
    karma_post_note: str = ""
    karma_post_sub: str = ""
    rl_actions: int = 0
    rl_reward: float = 0.0
    rl_epsilon: float = 0.0
    rl_note: str = ""
    last_comment_text: str = ""
    last_comment_url: str = ""
    rules_by_sub: Dict[str, Any] = field(default_factory=dict)
    rules_read: List[str] = field(default_factory=list)
    lurk_subs: List[str] = field(default_factory=list)
    comment_sorts: List[str] = field(default_factory=list)
    session_style_note: str = ""
    subreddit_topics: Dict[str, List[str]] = field(default_factory=dict)
    searches: int = 0
    search_queries: List[str] = field(default_factory=list)
    scrolled_px: float = 0.0
    scroll_engine: str = ""
    explored: List[str] = field(default_factory=list)

    def report_lines(self) -> List[str]:
        lines = [
            f"ACCOUNT SUMMARY — {self.name}",
            f"  Profile ID : {self.user_id}",
        ]
        if self.account_status:
            lines.append(f"  Account status: {self.account_status}")
            return lines
        if self.reddit_username:
            age = f"{self.account_age_days:.0f}d" if self.account_age_days else "?"
            lines.append(
                f"  Reddit     : u/{self.reddit_username} | "
                f"karma {self.account_karma} | age {age} | tier {self.karma_tier or '-'}"
            )
        if self.session_style_note:
            lines.append(f"  Sitting    : {self.session_style_note}")
        lines.append(f"  Upvotes    : {self.upvotes} given")
        lines.append(f"  Upvotes recv: {self.upvotes_received}")
        if self.scrolled_px or self.scroll_engine:
            engine = self.scroll_engine or "unknown"
            lines.append(
                f"  Scrolling  : {self.scrolled_px / 1000.0:.1f}k px via {engine}"
            )
        if self.explored:
            lines.append(
                f"  Explored   : {', '.join('r/' + name for name in self.explored)}"
            )
        if self.searches:
            queries = ", ".join(f'"{item}"' for item in self.search_queries)
            lines.append(f"  Searches   : {self.searches} on the New tab ({queries})")
        target = self.session_comment_target or SESSION_COMMENT_CAP
        sorts = ", ".join(self.comment_sorts) if self.comment_sorts else "none"
        lines.append(
            f"  Comments   : {self.comments} general this run "
            f"(target {target}, AI: {self.ai_comments}, listings: {sorts})"
        )
        if self.sheet_comments:
            lines.append(f"  Link comments: {self.sheet_comments}/{COMMENT_MAX_PER_WEEK} in 48h")
            if self.last_comment_url:
                lines.append(f"  Comment URL : {self.last_comment_url}")
        elif self.comment_note:
            lines.append(f"  Link comments: skipped ({self.comment_note})")
        else:
            lines.append("  Link comments: none")
        joined = ", ".join("r/" + s for s in self.joined) or "none"
        already = ", ".join("r/" + s for s in self.already_member) or "none"
        failed = ", ".join("r/" + s for s in self.join_failed) or "none"
        lines.append(f"  Joined     : {len(self.joined)} ({joined})")
        lines.append(f"  Already in : {len(self.already_member)} ({already})")
        if self.join_failed:
            lines.append(f"  Join failed: {len(self.join_failed)} ({failed})")
        if self.rules_read:
            lines.append(
                f"  Rules read : {len(self.rules_read)} "
                f"({', '.join('r/' + name for name in self.rules_read)})"
            )
        if self.lurk_subs:
            lines.append(f"  RL lurk    : {', '.join('r/' + name for name in self.lurk_subs)}")
        status = (self.post_status or "none").strip() or "none"
        lines.append(f"  Post status: {status}")
        if self.post_title:
            title = self.post_title if len(self.post_title) <= 80 else self.post_title[:77] + "..."
            lines.append(f"  Post title : {title}")
        if self.posted:
            lines.append(f"  Posted to  : r/{self.posted}")
        if self.post_url:
            lines.append(f"  Post URL   : {self.post_url}")
        if self.live_posts:
            lines.append(f"  Live posts : {len(self.live_posts)}")
        if self.posts_removed:
            lines.append(f"  Removed    : {self.posts_removed}")
        if self.post_note:
            lines.append(f"  Post note  : {self.post_note}")
        if self.last_comment_text:
            snippet = self.last_comment_text.replace("\n", " ").strip()
            if len(snippet) > 160:
                snippet = snippet[:157] + "..."
            lines.append(f"  Last comment: {snippet}")
        if self.karma_comments or self.karma_comment_note:
            lines.append(
                f"  Karma comments: {self.karma_comments} this run"
                + (f" ({self.karma_comment_note})" if self.karma_comment_note else "")
            )
        if self.karma_post_status and self.karma_post_status != "none":
            extra = f" r/{self.karma_post_sub}" if self.karma_post_sub else ""
            lines.append(f"  Karma post : {self.karma_post_status}{extra}")
            if self.karma_post_note:
                lines.append(f"  Karma note : {self.karma_post_note}")
        if RL_ENABLED and (self.rl_actions or self.rl_note):
            lines.append(
                f"  RL         : {self.rl_actions} actions this run | "
                f"reward {self.rl_reward:.1f} | epsilon {self.rl_epsilon:.3f}"
            )
            if self.rl_note:
                lines.append(f"  RL note    : {self.rl_note}")
        return lines

    def print_report(self) -> None:
        log("=" * 56)
        for line in self.report_lines():
            log(line)
        log("=" * 56)


def next_session_number() -> int:
    folder = _SUMMARIES_DIR
    folder.mkdir(parents=True, exist_ok=True)
    highest = 0
    for path in folder.glob("session_*.txt"):
        match = re.match(r"session_(\d+)\.txt$", path.name, re.I)
        if match:
            highest = max(highest, int(match.group(1)))
    return highest + 1


def session_tally_lines(reports: List[AccountSummary]) -> List[str]:
    """How many accounts did activity, and how many were banned."""
    banned = [
        item
        for item in reports
        if "banned" in (item.account_status or "").lower()
    ]
    active = []
    for item in reports:
        if item in banned or (item.account_status or "").strip():
            continue
        if (
            item.session_style_note
            or item.comments
            or item.sheet_comments
            or item.upvotes
            or item.joined
            or item.scrolled_px
            or item.searches
            or item.posted
            or item.explored
        ):
            active.append(item)
    banned_names = [
        f"u/{item.reddit_username}" if item.reddit_username else (item.name or item.user_id)
        for item in banned
    ]
    comments = sum(int(item.comments or 0) + int(item.sheet_comments or 0) for item in active)
    joins = sum(len(item.joined) for item in active)
    upvotes = sum(int(item.upvotes or 0) for item in active)
    posts = sum(1 for item in active if (item.posted or "").strip())
    return [
        f"Accounts that did activity : {len(active)}",
        f"Accounts banned            : {len(banned)}",
        f"Banned accounts            : {', '.join(banned_names) if banned_names else 'none'}",
        (
            f"Activity                   : {comments} comments, "
            f"{joins} joins, {upvotes} upvotes, {posts} posts"
        ),
    ]


def write_session_summary(reports: List[AccountSummary]) -> str:
    """Write session N + each account summary to data/summaries/session_N.txt."""
    number = next_session_number()
    folder = _SUMMARIES_DIR
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"session_{number}.txt"
    when = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    accounts = [item.name or item.user_id for item in reports]
    lines = [
        f"SESSION {number}",
        f"Date     : {when}",
        f"Accounts : {len(reports)}",
        *session_tally_lines(reports),
        f"Names    : {', '.join(accounts) if accounts else 'none'}",
        "",
        "ACCOUNTS",
        "-" * 56,
    ]
    if not reports:
        lines.append("No accounts ran this session.")
    else:
        for index, item in enumerate(reports, start=1):
            lines.append(f"Account {index}/{len(reports)}")
            lines.extend(item.report_lines())
            lines.append("-" * 56)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    latest = folder / "latest.txt"
    latest.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    return str(path)


# =============================================================================
# LOGGING
# =============================================================================

_LOG_LOCK = threading.Lock()
_API_LOCK = threading.Lock()
_SHEET_LOCK = threading.RLock()
_FILE_LOCK = threading.RLock()


def log(message: str) -> None:
    stamp = datetime.now().strftime("%H:%M:%S")
    line = f"[{stamp}] {message}"
    with _LOG_LOCK:
        print(line, flush=True)


def disk_free_mb(path: str = "") -> float:
    """Free megabytes on the filesystem that holds `path` (the data folder)."""
    target = path or str(_DATA_DIR)
    try:
        return shutil.disk_usage(target).free / (1024.0 * 1024.0)
    except Exception:
        return -1.0


def require_disk_space() -> Optional[str]:
    """
    AdsPower and the activity logs both write under /home. When that disk is
    full, browsers fail to start and comment/post budgets cannot be recorded.
    """
    free = disk_free_mb()
    if free < 0:
        return None
    log(f"Disk: {free:.0f} MB free for data + AdsPower profiles")
    if free >= MIN_FREE_DISK_MB:
        return None
    return (
        f"STOPPED: only {free:.0f} MB free (need {MIN_FREE_DISK_MB:.0f} MB). "
        "AdsPower cannot start a browser and logs cannot be saved. "
        "Free space on /home — this is why the last run failed with "
        "'Failed to start browser' / 'No space left on device'."
    )


def brief_error(exc: BaseException) -> str:
    text = getattr(exc, "msg", None) or str(exc)
    text = re.sub(r"([?&]key=)[^&\s]+", r"\1***", str(text), flags=re.I)
    return text.splitlines()[0][:240]


def _rl_agent():
    try:
        from reddit_joiner.rl import get_agent

        return get_agent()
    except Exception:
        return None


def _rl_state(
    stats: AccountSummary,
    subreddit: str = "",
    title: str = "",
    body: str = "",
    sentiment: Optional[float] = None,
    kind: str = "",
    comment_length: int = 0,
    rules: Any = None,
) -> Dict[str, Any]:
    try:
        from reddit_joiner.ai import sentiment_score
        from reddit_joiner.rl import build_state
    except Exception:
        return {}
    if sentiment is None:
        try:
            sentiment = sentiment_score(f"{title}\n{body}")
        except Exception:
            sentiment = 0.0
    success_rate = 0.0
    try:
        agent = _rl_agent()
        if agent is not None:
            success_rate = float(agent.get_performance_report().get("success_rate") or 0)
    except Exception:
        success_rate = 0.0
    extra: Dict[str, Any] = {}
    if rules is None and subreddit:
        rules = (stats.rules_by_sub or {}).get(normalize_subreddit(subreddit).lower())
    if rules is not None:
        try:
            extra = rules.features() if hasattr(rules, "features") else {}
        except Exception:
            extra = {}
    return build_state(
        account_id=stats.user_id or stats.name,
        account_karma=stats.account_karma,
        account_age_days=stats.account_age_days,
        target_subreddit=subreddit,
        post_sentiment=float(sentiment or 0),
        post_length=len(body or "") + len(title or ""),
        comment_length=int(comment_length or 0),
        kind=str(kind or ""),
        session_comments=int(stats.comments or 0) + int(stats.sheet_comments or 0),
        success_rate=success_rate,
        **extra,
    )


POST_ACTIONS = ("post:sheet", "post:explore")


def _post_target_action(subreddit: str, is_sheet: bool = False) -> str:
    """
    Map a post target to a small, fixed action label.

    Subreddit names must never be actions: the set is unbounded, a mis-parsed
    sheet row can invent one out of a post title, and the net cannot learn
    anything from a label it sees once. Which community it was belongs in the
    state, where it is already encoded.
    """
    return "post:sheet" if is_sheet else "post:explore"


def _rank_post_targets(
    stats: AccountSummary,
    title: str,
    body: str,
    candidates: List[str],
    sheet_names: Optional[set] = None,
) -> List[str]:
    """
    Order fallback communities best-first by what the model expects from each.

    Each candidate becomes part of the STATE and is scored with a fixed action
    label, so this ranks over a bounded action space instead of treating every
    subreddit name as its own action.
    """
    agent = _rl_agent()
    names = [name for name in candidates if name]
    if agent is None or not RL_ENABLED or len(names) < 2:
        return names
    sheet = {str(item).lower() for item in (sheet_names or set())}
    scored: List[Tuple[float, str]] = []
    for name in names:
        try:
            state = _rl_state(stats, name, title, body, kind="post")
            action = _post_target_action(name, name.lower() in sheet)
            score = float(agent.get_q_value(state, action))
        except Exception:
            score = 0.0
        # Break ties randomly so equal-value communities rotate.
        scored.append((score + _rng().uniform(-1e-3, 1e-3), name))
    # Explore sometimes instead of always taking the model's favourite.
    if _rng().random() < float(getattr(agent, "epsilon", 0.1) or 0.1):
        _rng().shuffle(names)
        return names
    scored.sort(key=lambda row: row[0], reverse=True)
    return [name for _, name in scored]


def _rl_learn(
    stats: AccountSummary,
    state: Any,
    action: str,
    reward: float,
    next_state: Any = None,
    next_actions: Optional[List[str]] = None,
    graded: bool = False,
) -> None:
    agent = _rl_agent()
    if agent is None or not action:
        return
    try:
        agent.update_q_value(state, action, reward, next_state, next_actions, graded=graded)
        stats.rl_actions += 1
        stats.rl_reward += float(reward)
        stats.rl_epsilon = float(agent.epsilon)
    except Exception as exc:
        stats.rl_note = brief_error(exc)


def _removal_reward(reason: str) -> float:
    text = (reason or "").lower()
    if any(word in text for word in ("mod", "banned", "approved", "moderator")):
        return float(REWARD_POST_REMOVED_MOD)
    return float(REWARD_POST_REMOVED_SPAM)


# =============================================================================
# ADSPOWER LOCAL API
# =============================================================================

_last_api_call_at = 0.0


def _api_headers() -> Dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if ADSPOWER_API_KEY:
        headers["Authorization"] = f"Bearer {ADSPOWER_API_KEY}"
    return headers


def _throttle_api() -> None:
    global _last_api_call_at
    with _API_LOCK:
        elapsed = time.time() - _last_api_call_at
        if elapsed < API_MIN_INTERVAL:
            time.sleep(API_MIN_INTERVAL - elapsed)
        _last_api_call_at = time.time()


def adspower_get(path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    _throttle_api()
    url = f"{ADSPOWER_API}{path}"
    response = requests.get(
        url, params=params, headers=_api_headers(), timeout=REQUEST_TIMEOUT
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("code") not in (0, None) and str(payload.get("code")) != "0":
        raise RuntimeError(
            f"AdsPower API error on {path}: code={payload.get('code')} "
            f"msg={payload.get('msg')}"
        )
    return payload


def adspower_api_ready() -> bool:
    try:
        payload = requests.get(
            f"{ADSPOWER_API}/status", headers=_api_headers(), timeout=8
        ).json()
        return payload.get("code") in (0, None) or str(payload.get("code")) == "0"
    except Exception:
        try:
            requests.get(f"{ADSPOWER_API}/api/v1/user/list", params={"page": 1, "page_size": 1}, timeout=8)
            return True
        except Exception:
            return False


def _load_profile_cache() -> List[Dict[str, str]]:
    try:
        with open(PROFILE_CACHE_PATH, encoding="utf-8") as handle:
            data = json.load(handle)
        if time.time() - float(data.get("saved_at") or 0) > PROFILE_CACHE_TTL:
            return []
        return list(data.get("profiles") or [])
    except Exception:
        return []


def _save_profile_cache(profiles: List[Dict[str, str]]) -> None:
    try:
        with open(PROFILE_CACHE_PATH, "w", encoding="utf-8") as handle:
            json.dump({"saved_at": time.time(), "profiles": profiles}, handle, indent=2)
    except Exception as exc:
        log(f"Could not write profile cache: {exc}")


def _load_post_log() -> Dict[str, Any]:
    try:
        with _FILE_LOCK:
            with open(POST_LOG_PATH, encoding="utf-8") as handle:
                data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_json_atomic(path: str, data: Any) -> None:
    """Write via a temp file so a crash cannot leave a truncated log behind.

    These logs hold the 48h comment and post budgets. A half-written file parses
    as empty, which would silently reset every account's budget to zero used.
    """
    with _FILE_LOCK:
        directory = os.path.dirname(path) or "."
        os.makedirs(directory, exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)


def _save_post_log(data: Dict[str, Any]) -> None:
    try:
        _write_json_atomic(POST_LOG_PATH, data)
    except Exception as exc:
        log(f"Could not write post log: {exc}")


def _load_comment_log() -> Dict[str, Any]:
    try:
        with _FILE_LOCK:
            with open(COMMENT_LOG_PATH, encoding="utf-8") as handle:
                data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_comment_log(data: Dict[str, Any]) -> None:
    try:
        _write_json_atomic(COMMENT_LOG_PATH, data)
    except Exception as exc:
        log(f"Could not write comment log: {exc}")


def _account_comment_items(user_id: str) -> List[Dict[str, Any]]:
    entry = _load_comment_log().get(user_id) or {}
    items = entry.get("items") if isinstance(entry, dict) else None
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def comments_in_window(
    user_id: str,
    days: float = COMMENT_COOLDOWN_DAYS,
    kind: Optional[str] = None,
) -> List[Dict[str, Any]]:
    cutoff = time.time() - float(days) * 86400.0
    found: List[Dict[str, Any]] = []
    for item in _account_comment_items(user_id):
        try:
            when = float(item.get("at") or 0)
        except (TypeError, ValueError):
            continue
        if when < cutoff:
            continue
        item_kind = str(item.get("kind") or "sheet")
        if kind and item_kind != kind:
            continue
        found.append(item)
    return found


def general_comments_this_week(user_id: str) -> List[Dict[str, Any]]:
    """All comments from this account in the 48-hour window."""
    found: List[Dict[str, Any]] = []
    seen = set()
    for item in comments_in_window(user_id, ACTION_WINDOW_DAYS):
        key = _comment_link_key(str(item.get("link") or "")) or f"{item.get('at')}-{item.get('kind')}"
        if key in seen:
            continue
        seen.add(key)
        found.append(item)
    return found


def comments_remaining(user_id: str) -> int:
    return max(0, COMMENTS_PER_WINDOW - len(general_comments_this_week(user_id)))


def hours_until_comment_room(user_id: str) -> float:
    """Hours until the oldest 48h comment ages out and a new slot opens."""
    items = general_comments_this_week(user_id)
    if len(items) < COMMENTS_PER_WINDOW:
        return 0.0
    oldest = min(float(item.get("at") or 0) for item in items if item.get("at"))
    if not oldest:
        return 0.0
    unlock = oldest + float(ACTION_WINDOW_HOURS) * 3600.0
    return max(0.0, (unlock - time.time()) / 3600.0)


def general_comment_need(
    user_id: str,
    session_want: Optional[int] = None,
) -> Tuple[int, int]:
    """Return (already in 48h, how many comments this run may still leave)."""
    have = len(general_comments_this_week(user_id))
    room = comments_remaining(user_id)
    want = session_want if session_want is not None else SESSION_COMMENT_CAP
    return have, min(room, max(0, int(want)), SESSION_COMMENT_CAP)


def account_may_post(stats: AccountSummary) -> Tuple[bool, str]:
    karma = int(stats.account_karma or 0)
    if karma < MIN_KARMA_TO_POST:
        return (
            False,
            f"karma {karma} < {MIN_KARMA_TO_POST} — comments only until karma reaches {MIN_KARMA_TO_POST}",
        )
    if karma <= 2:
        return True, "new account"
    return True, ""


def record_account_comment(user_id: str, link: str, text: str, kind: str = "sheet") -> None:
    with _FILE_LOCK:
        data = _load_comment_log()
        entry = data.get(user_id) if isinstance(data.get(user_id), dict) else {}
        items = entry.get("items") if isinstance(entry.get("items"), list) else []
        items.append({"at": time.time(), "link": link, "text": text, "kind": kind or "sheet"})
        entry["items"] = items[-80:]
        data[user_id] = entry
        _save_comment_log(data)


def _load_subreddit_log() -> Dict[str, Any]:
    try:
        with _FILE_LOCK:
            if not os.path.isfile(SUBREDDIT_LOG_PATH):
                return {}
            with open(SUBREDDIT_LOG_PATH, encoding="utf-8") as handle:
                data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_subreddit_log(data: Dict[str, Any]) -> None:
    try:
        _write_json_atomic(SUBREDDIT_LOG_PATH, data)
    except Exception as exc:
        log(f"Could not write subreddit log: {exc}")


def _subreddit_run_names(user_id: str, last_n: int) -> List[List[str]]:
    entry = _load_subreddit_log().get(user_id) or {}
    runs = entry.get("runs") if isinstance(entry, dict) else None
    if not isinstance(runs, list):
        return []
    cleaned: List[List[str]] = []
    for item in runs[-max(0, int(last_n)) :]:
        names = item.get("subs") if isinstance(item, dict) else None
        if not isinstance(names, list):
            continue
        cleaned.append([str(name).strip() for name in names if str(name).strip()])
    return cleaned


def recent_session_subreddits(user_id: str, last_n: int = SUBREDDIT_ROTATION_RUNS) -> set:
    found = set()
    for names in _subreddit_run_names(user_id, last_n):
        found.update(name.lower() for name in names)
    return found


def _explored_run_names(user_id: str, last_n: int) -> List[List[str]]:
    """Recent random-explore batches for this account, newest last."""
    entry = _load_subreddit_log().get(user_id) or {}
    runs = entry.get("explored_runs") if isinstance(entry, dict) else None
    if not isinstance(runs, list):
        return []
    cleaned: List[List[str]] = []
    for item in runs[-max(0, int(last_n)) :]:
        names = item.get("subs") if isinstance(item, dict) else None
        if not isinstance(names, list):
            continue
        batch = [
            normalize_subreddit(str(name))
            for name in names
            if str(name).strip()
        ]
        batch = [name for name in batch if name]
        if batch:
            cleaned.append(batch)
    return cleaned


def explored_subreddits(user_id: str, last_n: int = EXPLORE_SKIP_SESSIONS) -> set:
    """Random explores still on cooldown for this account (default: last sitting)."""
    found = set()
    for names in _explored_run_names(user_id, last_n):
        found.update(name.lower() for name in names)
    return found


def remember_explored_subreddits(user_id: str, subs: List[str]) -> None:
    """Record this sitting's random explores so the next sitting skips them."""
    names = [normalize_subreddit(str(name)) for name in subs if str(name).strip()]
    names = [name for name in names if name]
    if not user_id or not names:
        return
    with _FILE_LOCK:
        data = _load_subreddit_log()
        entry = data.get(user_id) if isinstance(data.get(user_id), dict) else {}
        runs = entry.get("explored_runs") if isinstance(entry.get("explored_runs"), list) else []
        runs.append({"at": time.time(), "subs": names})
        entry["explored_runs"] = runs[-EXPLORE_MEMORY:]
        data[user_id] = entry
        _save_subreddit_log(data)


def remember_session_subreddits(user_id: str, subs: List[str]) -> None:
    names = [str(name).strip().lstrip("r/") for name in subs if str(name).strip()]
    if not user_id or not names:
        return
    with _FILE_LOCK:
        data = _load_subreddit_log()
        entry = data.get(user_id) if isinstance(data.get(user_id), dict) else {}
        runs = entry.get("runs") if isinstance(entry.get("runs"), list) else []
        runs.append({"at": time.time(), "subs": names})
        entry["runs"] = runs[-12:]
        data[user_id] = entry
        _save_subreddit_log(data)


def is_blocked_subreddit(name: str) -> bool:
    """True for communities we never touch, even if a sheet still lists them."""
    return normalize_subreddit(name).lower() in BLOCKED_SUBREDDITS


def allowed_subreddits() -> List[str]:
    """Communities from subreddits.csv only (enabled=yes), minus the blocklist."""
    return [
        name for name in load_browse_subreddits() if not is_blocked_subreddit(name)
    ]


def pick_session_subreddits(
    user_id: str,
    karma: int = 0,
    age_days: float = 0.0,
) -> List[str]:
    """Pick this sitting's general-activity communities from subreddits.csv."""
    sheet: List[str] = []
    seen = set()
    for raw in allowed_subreddits():
        name = str(raw or "").strip().lstrip("r/")
        key = name.lower()
        if not name or key in seen or is_blocked_subreddit(name):
            continue
        seen.add(key)
        sheet.append(name)
    if not sheet:
        return []
    recent = recent_session_subreddits(user_id, SUBREDDIT_ROTATION_RUNS)
    count = min(_rng().randint(*SUBREDDITS_PER_SESSION), len(sheet))
    fresh = [name for name in sheet if name.lower() not in recent]
    if len(fresh) < count:
        last_run = set()
        runs = _subreddit_run_names(user_id, 1)
        if runs:
            last_run = {name.lower() for name in runs[-1]}
        fresh = [name for name in sheet if name.lower() not in last_run] or list(sheet)
    _rng().shuffle(fresh)
    picked = fresh[:count]
    if len(picked) < count:
        extras = [name for name in sheet if name not in picked]
        _rng().shuffle(extras)
        picked.extend(extras[: count - len(picked)])
    return picked


def _comment_link_key(url: str) -> str:
    return (url or "").split("?")[0].rstrip("/").lower()


def already_commented_on(user_id: str, url: str) -> bool:
    key = _comment_link_key(url)
    if not key:
        return False
    if any(_comment_link_key(str(item.get("link") or "")) == key for item in _account_comment_items(user_id)):
        return True
    try:
        from reddit_joiner.store import already_commented_url

        return already_commented_url(user_id, url)
    except Exception:
        return False


def link_commented_by_any_account(url: str) -> bool:
    """One comments.csv link = one comment, not one per AdsPower profile."""
    key = _comment_link_key(url)
    if not key:
        return False
    data = _load_comment_log()
    for entry in data.values():
        items = entry.get("items") if isinstance(entry, dict) else None
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict) and _comment_link_key(str(item.get("link") or "")) == key:
                return True
    if link_in_commented_archive(url):
        return True
    try:
        from reddit_joiner.store import already_commented_url_any

        return already_commented_url_any(url)
    except Exception:
        return False


def _account_post_items(user_id: str) -> List[Dict[str, Any]]:
    entry = _load_post_log().get(user_id) or {}
    items = entry.get("items") if isinstance(entry, dict) else None
    found = [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []
    if found:
        return found
    migrated: List[Dict[str, Any]] = []
    for key, kind in (("karma_posted_at", "karma"), ("posted_at", "sheet")):
        try:
            when = float(entry.get(key) or 0)
        except (TypeError, ValueError):
            when = 0.0
        if when:
            migrated.append({"at": when, "kind": kind})
    return migrated


def live_posts_this_week(user_id: str) -> List[Dict[str, Any]]:
    cutoff = time.time() - float(GENERAL_POST_DAYS) * 86400.0
    found: List[Dict[str, Any]] = []
    for item in _account_post_items(user_id):
        try:
            when = float(item.get("at") or 0)
        except (TypeError, ValueError):
            continue
        if when >= cutoff:
            found.append(item)
    return found


def general_posts_remaining(user_id: str) -> int:
    return max(0, GENERAL_POSTS_PER_WEEK - len(live_posts_this_week(user_id)))


def _append_live_post_item(user_id: str, kind: str, subreddit: str, title: str) -> None:
    # Held across the read and the write: profiles run in parallel threads and
    # share one post log, so an interleaved save would drop another account's
    # record and make its 48h post budget look unused.
    with _FILE_LOCK:
        data = _load_post_log()
        entry = data.get(user_id) if isinstance(data.get(user_id), dict) else {}
        items = entry.get("items") if isinstance(entry.get("items"), list) else []
        items.append({"at": time.time(), "kind": kind, "subreddit": subreddit, "title": title})
        entry["items"] = items[-40:]
        data[user_id] = entry
        _save_post_log(data)


def days_since_karma_post(user_id: str) -> Optional[float]:
    entry = _load_post_log().get(user_id) or {}
    posted_at = entry.get("karma_posted_at")
    if not posted_at:
        return None
    return (time.time() - float(posted_at)) / 86400.0


def record_karma_post(user_id: str, subreddit: str, title: str) -> None:
    with _FILE_LOCK:
        data = _load_post_log()
        entry = data.get(user_id) if isinstance(data.get(user_id), dict) else {}
        entry["karma_posted_at"] = time.time()
        entry["karma_subreddit"] = subreddit
        entry["karma_title"] = title
        items = entry.get("items") if isinstance(entry.get("items"), list) else []
        items.append({"at": time.time(), "kind": "karma", "subreddit": subreddit, "title": title})
        entry["items"] = items[-40:]
        data[user_id] = entry
        _save_post_log(data)


def days_since_last_post(user_id: str) -> Optional[float]:
    entry = (_load_post_log().get(user_id) or {})
    posted_at = entry.get("posted_at")
    if not posted_at:
        return None
    return (time.time() - float(posted_at)) / 86400.0


def record_account_post(
    user_id: str,
    subreddit: str,
    title: str,
    sheet_row: Optional[int] = None,
) -> None:
    with _FILE_LOCK:
        data = _load_post_log()
        entry = data.get(user_id) if isinstance(data.get(user_id), dict) else {}
        entry["posted_at"] = time.time()
        entry["subreddit"] = subreddit
        entry["title"] = title
        entry["sheet_row"] = sheet_row
        items = entry.get("items") if isinstance(entry.get("items"), list) else []
        items.append({"at": time.time(), "kind": "sheet", "subreddit": subreddit, "title": title})
        entry["items"] = items[-40:]
        data[user_id] = entry
        _save_post_log(data)


def next_post_subreddit(user_id: str, names: List[str]) -> str:
    clean = [normalize_subreddit(s) for s in names if normalize_subreddit(s)]
    if not clean:
        return ""
    last = str((_load_post_log().get(user_id) or {}).get("subreddit") or "")
    if last in clean:
        return clean[(clean.index(last) + 1) % len(clean)]
    return clean[0]


def _sheet_header_map(fieldnames: List[str]) -> Dict[str, str]:
    lowered = {str(name).strip().lower(): str(name) for name in fieldnames if name}
    mapping: Dict[str, str] = {}
    for canonical, aliases in _POSTS_SHEET_ALIASES.items():
        for alias in aliases:
            if alias in lowered:
                mapping[canonical] = lowered[alias]
                break
    return mapping


def _sheet_cell(
    row: Dict[str, str],
    header_map: Dict[str, str],
    key: str,
    keep_spacing: bool = False,
) -> str:
    column = header_map.get(key)
    if not column:
        return ""
    value = str(row.get(column) or "")
    return value if keep_spacing else value.strip()


def _post_template_path(name: str) -> Optional[str]:
    """Resolve a template filename to a file under sheets/, or None."""
    raw = str(name or "").strip().strip('"').strip("'")
    if raw.startswith("@"):
        raw = raw[1:].strip()
    if not raw:
        return None
    raw = raw.replace("\\", "/")
    candidate = Path(raw)
    if not candidate.is_absolute():
        under_templates = Path(POST_TEMPLATES_DIR) / raw
        under_sheets = Path(POSTS_SHEET).resolve().parent / raw
        if under_templates.is_file():
            candidate = under_templates
        elif under_sheets.is_file():
            candidate = under_sheets
        else:
            candidate = under_templates
    try:
        resolved = candidate.resolve()
        allowed = (
            Path(POST_TEMPLATES_DIR).resolve(),
            Path(POSTS_SHEET).resolve().parent,
        )
        if not any(resolved == root or root in resolved.parents for root in allowed):
            return None
        if resolved.is_file():
            return str(resolved)
    except Exception:
        return None
    return None


def _load_post_template_file(name: str) -> Optional[str]:
    path = _post_template_path(name)
    if not path:
        return None
    try:
        return Path(path).read_text(encoding="utf-8")
    except Exception as exc:
        log(f"Could not read post template {path}: {brief_error(exc)}")
        return None


def format_post_template(text: str, *, as_title: bool = False) -> str:
    """Keep the spacing from a pasted posts.csv body or a .txt template.

    Real line breaks in a quoted CSV cell are kept. In a one-line cell, type
    \\n for a new line and \\n\\n for a blank line between paragraphs. A cell
    that is @post1.txt loads sheets/post_templates/post1.txt as-is.
    """
    value = str(text or "")
    loaded = _load_post_template_file(value) if value.strip().startswith("@") else None
    if loaded is not None:
        value = loaded
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    if "\\" in value:
        value = (
            value.replace("\\r\\n", "\n")
            .replace("\\n", "\n")
            .replace("\\t", "\t")
        )
    value = re.sub(r"(?i)<br\s*/?>", "\n", value)
    if as_title:
        return re.sub(r"[ \t]*\n+[ \t]*", " ", value).strip()
    lines = value.split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    out: List[str] = []
    blanks = 0
    for line in lines:
        if not line.strip():
            blanks += 1
            if blanks <= 2:
                out.append("")
            continue
        blanks = 0
        out.append(line.rstrip())
    return "\n".join(out)


def resolve_sheet_post_body(
    row: Dict[str, str],
    header_map: Dict[str, str],
) -> Tuple[str, str]:
    """Body from body_file / @template / postN.txt / the body cell, with spacing kept."""
    file_cell = _sheet_cell(row, header_map, "body_file")
    post_id = str(row.get("post") or "").strip()
    body_cell = _sheet_cell(row, header_map, "body", keep_spacing=True)
    source = "posts.csv"
    loaded: Optional[str] = None
    if file_cell:
        loaded = _load_post_template_file(file_cell)
        if loaded is not None:
            source = file_cell
    if loaded is None and body_cell.strip().startswith("@"):
        loaded = _load_post_template_file(body_cell)
        if loaded is not None:
            source = body_cell.strip()
    if loaded is None and post_id:
        loaded = _load_post_template_file(f"{post_id}.txt")
        if loaded is not None:
            source = f"{post_id}.txt"
    if loaded is not None:
        return format_post_template(loaded), source
    return format_post_template(body_cell), source


def _parse_sheet_subreddits(raw: str) -> List[str]:
    parts = re.split(r"[,;|\n]+", raw or "")
    names: List[str] = []
    for part in parts:
        name = normalize_subreddit(part)
        if name and name not in names:
            names.append(name)
    return names


def _subreddit_columns(fieldnames: List[str]) -> List[str]:
    numbered: List[Tuple[int, str]] = []
    generic: List[str] = []
    for name in fieldnames:
        if not name:
            continue
        raw = str(name).strip()
        low = re.sub(r"[\s\-]+", "_", raw.lower()).strip("_")
        if low in _TRACKING_COLS:
            continue
        match = _SUBREDDIT_COL_RE.fullmatch(low)
        if match:
            numbered.append((int(match.group(1) or match.group(2)), raw))
            continue
        if low in {"subreddits", "subreddit", "subs", "communities"}:
            generic.append(raw)
    numbered.sort(key=lambda item: item[0])
    return [name for _, name in numbered] + generic


def _row_subreddits(row: Dict[str, str], fieldnames: List[str]) -> List[str]:
    names: List[str] = []
    for column in _subreddit_columns(fieldnames):
        for name in _parse_sheet_subreddits(str(row.get(column) or "")):
            if name and name not in names:
                names.append(name)
    if names:
        return names
    # Recover a misplaced subreddit (old rows put AskReddit in the body column).
    body = str(row.get("body") or "").strip().lstrip("r/")
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{2,30}", body or ""):
        names.append(body)
    return names


def _already_posted_subs(row: Dict[str, str], header_map: Dict[str, str]) -> List[str]:
    return _parse_sheet_subreddits(_sheet_cell(row, header_map, "posted_subreddit"))


def _remaining_subs(row: Dict[str, str], fieldnames: List[str], header_map: Dict[str, str]) -> List[str]:
    already = {name.lower() for name in _already_posted_subs(row, header_map)}
    return [name for name in _row_subreddits(row, fieldnames) if name.lower() not in already]


def ensure_posts_sheet() -> None:
    if os.path.isfile(POSTS_SHEET):
        return
    try:
        with open(POSTS_SHEET, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=POSTS_SHEET_FIELDS)
            writer.writeheader()
            for index in range(1, POSTS_SHEET_ROWS + 1):
                writer.writerow({"post": f"post{index}"})
        log(f"Created posts sheet with {POSTS_SHEET_ROWS} rows: {POSTS_SHEET}")
        log("Fill title, body, and subreddit for post1, post2, post3, ...")
    except Exception as exc:
        log(f"Could not create posts sheet: {exc}")


def _posts_row_looks_split(row: Dict[str, str], extras: Any) -> bool:
    """True when commas in title/body split the row across the wrong columns."""
    if extras:
        return True
    account = str(row.get("account") or "").strip()
    subreddit = str(row.get("subreddit") or "").strip()
    if account and (len(account) > 40 or " " in account):
        return True
    if subreddit and len(subreddit) > 40:
        return True
    return False


def _read_posts_sheet() -> Tuple[List[str], List[Dict[str, str]]]:
    ensure_posts_sheet()
    with open(POSTS_SHEET, encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, restkey="_extra")
        fieldnames = [name for name in (reader.fieldnames or POSTS_SHEET_FIELDS) if name]
        raw_rows = list(reader)
    rows: List[Dict[str, str]] = []
    split_labels: List[str] = []
    for raw in raw_rows:
        extras = raw.get("_extra")
        row = {key: str(raw.get(key) or "") for key in fieldnames}
        if _posts_row_looks_split(row, extras):
            split_labels.append(str(row.get("post") or "row").strip() or "row")
        rows.append(row)
    if split_labels:
        names = ", ".join(split_labels[:6])
        log(
            f"posts.csv {names} split on commas — wrap title and body in quotes "
            "or the account/subreddit land in the wrong columns"
        )
    for name in POSTS_SHEET_FIELDS:
        if name not in fieldnames:
            fieldnames.append(name)
            for row in rows:
                row.setdefault(name, "")
    return fieldnames, rows


def _write_posts_sheet(fieldnames: List[str], rows: List[Dict[str, str]]) -> None:
    tmp_path = POSTS_SHEET + ".tmp"
    with _SHEET_LOCK:
        with open(tmp_path, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({key: row.get(key, "") for key in fieldnames})
        os.replace(tmp_path, POSTS_SHEET)


def _sheet_row_is_skipped(row: Dict[str, str], header_map: Dict[str, str]) -> bool:
    status = _sheet_cell(row, header_map, "status").lower()
    return status in _POST_SKIP_STATUS


def _sheet_row_is_done(row: Dict[str, str], header_map: Dict[str, str]) -> bool:
    status = _sheet_cell(row, header_map, "status").lower()
    return status in _POST_DONE_STATUS


def _sheet_row_is_used(
    row: Dict[str, str],
    fieldnames: List[str],
    header_map: Dict[str, str],
) -> bool:
    if _sheet_row_is_skipped(row, header_map) or _sheet_row_is_done(row, header_map):
        return True
    status = _sheet_cell(row, header_map, "status").lower()
    if status == "in progress":
        return not _remaining_subs(row, fieldnames, header_map)
    return False


def count_pending_posts() -> int:
    try:
        fieldnames, rows = _read_posts_sheet()
    except Exception:
        return 0
    header_map = _sheet_header_map(fieldnames)
    pending = 0
    for row in rows:
        if not _sheet_cell(row, header_map, "title"):
            continue
        if _sheet_row_is_used(row, fieldnames, header_map):
            continue
        pending += 1
    return pending


def print_posts_sheet_status() -> None:
    try:
        fieldnames, rows = _read_posts_sheet()
    except Exception as exc:
        log(f"Could not read posts.csv status ({exc})")
        return
    header_map = _sheet_header_map(fieldnames)
    log("POSTS SHEET STATUS")
    shown = 0
    for row in rows:
        label = str(row.get("post") or "").strip() or "row"
        title = _sheet_cell(row, header_map, "title")
        if not title and not _sheet_cell(row, header_map, "subreddit"):
            continue
        shown += 1
        status = _sheet_cell(row, header_map, "status").lower() or "ready"
        if not title:
            status = "empty"
        elif _sheet_row_is_skipped(row, header_map):
            status = "skip"
        elif _sheet_row_is_done(row, header_map):
            status = "posted"
        elif status in {"draft", "pending", "ready", "todo", "retry", ""}:
            status = "ready" if status != "retry" else "retry"
        subs = _row_subreddits(row, fieldnames)
        subreddit = subs[0] if subs else _sheet_cell(row, header_map, "posted_subreddit")
        account = _sheet_cell(row, header_map, "account") or _sheet_cell(row, header_map, "posted_by") or "-"
        when = _sheet_cell(row, header_map, "posted_at")
        short = title if len(title) <= 60 else title[:57] + "..."
        extra = f"  {when}" if when else ""
        log(
            f"  {label:<8} {status:<8} "
            f"{('r/' + subreddit) if subreddit else '-':<16} "
            f"{account:<22} {short}{extra}"
        )
    if not shown:
        log("  (no posts filled in posts.csv)")


def take_next_sheet_post(
    user_id: str = "",
    name: str = "",
    serial: str = "",
) -> Optional[Dict[str, Any]]:
    """Return one unused posts.csv row that belongs to this account only.

    An empty account cell is claimed by the first profile that reaches it.
    Failed rows stay with that same account; they are not passed around.
    """
    with _SHEET_LOCK:
        fieldnames, rows = _read_posts_sheet()
        header_map = _sheet_header_map(fieldnames)
        if "title" not in header_map:
            raise RuntimeError("posts.csv needs a title column")
        owner = (user_id or name or serial or "").strip()
        for index, row in enumerate(rows):
            title = _sheet_cell(row, header_map, "title")
            if not title:
                continue
            if _sheet_row_is_skipped(row, header_map) or _sheet_row_is_done(row, header_map):
                continue
            assigned = _sheet_cell(row, header_map, "account")
            mine = bool(assigned) and _comment_assigned_to(assigned, user_id, name, serial)
            status = _sheet_cell(row, header_map, "status").lower()
            if assigned and not mine:
                continue
            if status in _POST_FAILED_STATUS and not mine:
                # Unassigned failures used to be retried by every profile.
                continue
            if status == "in progress" and assigned and not mine:
                continue
            remaining = _row_subreddits(row, fieldnames)
            if status == "in progress":
                remaining = _remaining_subs(row, fieldnames, header_map)
            if not remaining:
                continue
            title = format_post_template(title, as_title=True)
            body, body_source = resolve_sheet_post_body(row, header_map)
            if not title:
                continue
            acc_key = header_map.get("account") or "account"
            st_key = header_map.get("status") or "status"
            claimed = False
            if not assigned and owner:
                row[acc_key] = owner
                assigned = owner
                claimed = True
            if status not in {"in progress"} | _POST_DONE_STATUS:
                row[st_key] = "in progress"
                claimed = True
            if claimed:
                _write_posts_sheet(fieldnames, rows)
            return {
                "row_index": index,
                "title": title,
                "body": body,
                "body_source": body_source,
                "flair": _sheet_cell(row, header_map, "flair"),
                "subreddit": remaining[0],
                "subreddits": _row_subreddits(row, fieldnames),
                "remaining": remaining,
                "account": assigned,
            }
        return None


def _sheet_post_skip_reason(user_id: str, name: str = "", serial: str = "") -> str:
    """Why take_next_sheet_post returned nothing, in one short line."""
    try:
        fieldnames, rows = _read_posts_sheet()
    except Exception as exc:
        return brief_error(exc)
    header_map = _sheet_header_map(fieldnames)
    other: List[str] = []
    split = 0
    titled = 0
    for row in rows:
        title = _sheet_cell(row, header_map, "title")
        if not title:
            continue
        titled += 1
        if _posts_row_looks_split(row, None):
            split += 1
            continue
        assigned = _sheet_cell(row, header_map, "account")
        if assigned and not _comment_assigned_to(assigned, user_id, name, serial):
            other.append(assigned)
            continue
        status = _sheet_cell(row, header_map, "status").lower()
        if status in _POST_FAILED_STATUS and not (
            assigned and _comment_assigned_to(assigned, user_id, name, serial)
        ):
            continue
    if split:
        return (
            "posts.csv title/body commas split the row — wrap those cells in quotes"
        )
    if other:
        return f"posts.csv is assigned to {other[0]}, not this account"
    if titled:
        return "posts.csv has no unused row for this account"
    return "no unused posts in posts.csv for this account"


def mark_sheet_post_used(row_index: int, account: str, subreddit: str) -> None:
    with _SHEET_LOCK:
        fieldnames, rows = _read_posts_sheet()
        if row_index < 0 or row_index >= len(rows):
            return
        header_map = _sheet_header_map(fieldnames)
        row = rows[row_index]
        posted = _already_posted_subs(row, header_map)
        if subreddit not in posted:
            posted.append(subreddit)
        row[header_map.get("posted_subreddit") or "posted_subreddit"] = ", ".join(posted)
        row[header_map.get("posted_by") or "posted_by"] = account
        row[header_map.get("posted_at") or "posted_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        remaining = [name for name in _row_subreddits(row, fieldnames) if name.lower() not in {s.lower() for s in posted}]
        row[header_map.get("status") or "status"] = "live" if not remaining else "in progress"
        _write_posts_sheet(fieldnames, rows)


def release_sheet_post(row_index: int, reason: str = "failed") -> None:
    """Mark this row failed. Keep the account so another profile cannot take it."""
    with _SHEET_LOCK:
        fieldnames, rows = _read_posts_sheet()
        if row_index < 0 or row_index >= len(rows):
            return
        header_map = _sheet_header_map(fieldnames)
        row = rows[row_index]
        st_key = header_map.get("status") or "status"
        row[st_key] = "failed" if reason in {"retry", "failed", ""} else (reason or "failed")
        _write_posts_sheet(fieldnames, rows)


def normalize_post_link(url: str) -> str:
    raw = (url or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    path = (parsed.path or "").rstrip("/").lower()
    if path.endswith(".json"):
        path = path[:-5]
    return path or raw.lower()


def _comments_header_map(fieldnames: List[str]) -> Dict[str, str]:
    lowered = {str(name).strip().lower(): str(name) for name in fieldnames if name}
    mapping: Dict[str, str] = {}
    for canonical, aliases in _COMMENTS_SHEET_ALIASES.items():
        for alias in aliases:
            if alias in lowered:
                mapping[canonical] = lowered[alias]
                break
    return mapping


def ensure_comments_sheet() -> None:
    try:
        if not os.path.isfile(COMMENTS_SHEET):
            with open(COMMENTS_SHEET, "w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=COMMENTS_SHEET_FIELDS)
                writer.writeheader()
                for index in range(1, COMMENTS_SHEET_ROWS + 1):
                    writer.writerow({"comment": f"comment{index}"})
            log(f"Created comments sheet with {COMMENTS_SHEET_ROWS} rows: {COMMENTS_SHEET}")
            log("Paste Reddit post URLs in the link column. Leave account empty.")
            log("Optional: put extra text in the edit column — it is added 3–4 min after the comment.")
            log(f"After a comment lands, that link is saved in {os.path.basename(COMMENTED_LINKS_SHEET)}")
            return
        fieldnames, rows = _read_comments_sheet_raw()
        changed = False
        for name in COMMENTS_SHEET_FIELDS:
            if name not in fieldnames:
                fieldnames.append(name)
                for row in rows:
                    row.setdefault(name, "")
                changed = True
        while len(rows) < COMMENTS_SHEET_ROWS:
            rows.append({"comment": f"comment{len(rows) + 1}"})
            changed = True
        if changed:
            _write_comments_sheet(fieldnames, rows)
    except Exception as exc:
        log(f"Could not create comments sheet: {exc}")


def _read_comments_sheet_raw() -> Tuple[List[str], List[Dict[str, str]]]:
    with open(COMMENTS_SHEET, encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or COMMENTS_SHEET_FIELDS)
        rows = [{key: (row.get(key) or "") for key in fieldnames} for row in reader]
    return fieldnames, rows


def _read_comments_sheet() -> Tuple[List[str], List[Dict[str, str]]]:
    ensure_comments_sheet()
    with open(COMMENTS_SHEET, encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or COMMENTS_SHEET_FIELDS)
        rows = [{key: (row.get(key) or "") for key in fieldnames} for row in reader]
    for name in COMMENTS_SHEET_FIELDS:
        if name not in fieldnames:
            fieldnames.append(name)
            for row in rows:
                row.setdefault(name, "")
    return fieldnames, rows


def _write_comments_sheet(fieldnames: List[str], rows: List[Dict[str, str]]) -> None:
    tmp_path = COMMENTS_SHEET + ".tmp"
    with _SHEET_LOCK:
        with open(tmp_path, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({key: row.get(key, "") for key in fieldnames})
        os.replace(tmp_path, COMMENTS_SHEET)


def ensure_commented_links_sheet() -> None:
    if os.path.isfile(COMMENTED_LINKS_SHEET):
        return
    with open(COMMENTED_LINKS_SHEET, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COMMENTED_LINKS_FIELDS)
        writer.writeheader()


def _read_commented_links() -> Tuple[List[str], List[Dict[str, str]]]:
    ensure_commented_links_sheet()
    with open(COMMENTED_LINKS_SHEET, encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or COMMENTED_LINKS_FIELDS)
        rows = [{key: (row.get(key) or "") for key in fieldnames} for row in reader]
    for name in COMMENTED_LINKS_FIELDS:
        if name not in fieldnames:
            fieldnames.append(name)
            for row in rows:
                row.setdefault(name, "")
    return fieldnames, rows


def _write_commented_links(fieldnames: List[str], rows: List[Dict[str, str]]) -> None:
    tmp_path = COMMENTED_LINKS_SHEET + ".tmp"
    with open(tmp_path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fieldnames})
    os.replace(tmp_path, COMMENTED_LINKS_SHEET)


def link_in_commented_archive(url: str) -> bool:
    path = normalize_post_link(url)
    if not path or not os.path.isfile(COMMENTED_LINKS_SHEET):
        return False
    try:
        _, rows = _read_commented_links()
    except Exception:
        return False
    return any(normalize_post_link(str(row.get("link") or "")) == path for row in rows)


def _append_commented_link(item: Dict[str, str]) -> None:
    fieldnames, rows = _read_commented_links()
    path = normalize_post_link(item.get("link") or "")
    if path:
        for row in rows:
            if normalize_post_link(str(row.get("link") or "")) == path:
                for key in fieldnames:
                    val = str(item.get(key) or "")
                    if val:
                        row[key] = val
                _write_commented_links(fieldnames, rows)
                return
    rows.append({key: str(item.get(key) or "") for key in fieldnames})
    _write_commented_links(fieldnames, rows)


def _clear_comment_row(row: Dict[str, str], header_map: Dict[str, str]) -> None:
    label = str(row.get("comment") or "").strip()
    for key in COMMENTS_SHEET_FIELDS:
        if key == "comment":
            continue
        column = header_map.get(key) or key
        if column in row:
            row[column] = ""
    if label:
        row["comment"] = label


def _comment_cell(row: Dict[str, str], header_map: Dict[str, str], key: str) -> str:
    column = header_map.get(key)
    if not column:
        return ""
    return str(row.get(column) or "").strip()


def _comment_row_done(row: Dict[str, str], header_map: Dict[str, str]) -> bool:
    status = _comment_cell(row, header_map, "status").lower()
    return status in {"skip", "skipped", "commented", "done", "posted", "used"}


def _comment_assigned_to(
    assigned: str,
    user_id: str,
    name: str = "",
    serial: str = "",
) -> bool:
    return _profile_matches(
        {"user_id": user_id, "name": name, "serial_number": serial},
        assigned,
    )


def count_pending_comment_links() -> int:
    try:
        fieldnames, rows = _read_comments_sheet()
    except Exception:
        return 0
    header_map = _comments_header_map(fieldnames)
    count = 0
    for row in rows:
        if _comment_row_done(row, header_map):
            continue
        link = _comment_cell(row, header_map, "link")
        if not normalize_post_link(link):
            continue
        if link_commented_by_any_account(link):
            continue
        count += 1
    return count


def print_comments_sheet_status() -> None:
    try:
        fieldnames, rows = _read_comments_sheet()
    except Exception as exc:
        log(f"Could not read comments.csv ({exc})")
        return
    header_map = _comments_header_map(fieldnames)
    log("COMMENTS SHEET STATUS")
    shown = 0
    for row in rows:
        link = _comment_cell(row, header_map, "link")
        if not link:
            continue
        shown += 1
        label = str(row.get("comment") or "").strip() or "row"
        status = _comment_cell(row, header_map, "status").lower() or "ready"
        if link_commented_by_any_account(link) or status in {
            "commented",
            "done",
            "posted",
            "used",
        }:
            status = "commented"
        elif status in {"skip", "skipped"}:
            status = "skip"
        account = (
            _comment_cell(row, header_map, "posted_by")
            or _comment_cell(row, header_map, "account")
            or "-"
        )
        short = link if len(link) <= 70 else link[:67] + "..."
        log(f"  {label:<10} {status:<10} {account:<22} {short}")
    if not shown:
        log("  (no post links in comments.csv — paste URLs in the link column)")
    try:
        _, archived = _read_commented_links()
        kept = [row for row in archived if str(row.get("link") or "").strip()]
        log(
            f"COMMENTED LINKS ({os.path.basename(COMMENTED_LINKS_SHEET)}): "
            f"{len(kept)} saved"
        )
        for row in kept[-8:]:
            link = str(row.get("link") or "").strip()
            short = link if len(link) <= 70 else link[:67] + "..."
            when = str(row.get("commented_at") or "-")
            who = str(row.get("posted_by") or row.get("account") or "-")
            log(f"  {when:<20} {who:<22} {short}")
    except Exception:
        pass


def _comment_path_held_by_other(
    rows: List[Dict[str, str]],
    header_map: Dict[str, str],
    user_id: str,
    name: str,
    serial: str,
) -> set:
    """Paths another account already claimed or finished — do not double-comment."""
    held: set = set()
    for row in rows:
        path = normalize_post_link(_comment_cell(row, header_map, "link"))
        if not path:
            continue
        status = _comment_cell(row, header_map, "status").lower()
        assigned = _comment_cell(row, header_map, "account")
        if status in {"commented", "done", "posted", "used"}:
            held.add(path)
            continue
        if status in {"skip", "skipped"}:
            continue
        mine = bool(assigned) and _comment_assigned_to(assigned, user_id, name, serial)
        if status == "in progress" and not mine:
            held.add(path)
            continue
        if assigned and not mine:
            held.add(path)
    return held


def _iter_comment_candidates(
    rows: List[Dict[str, str]],
    header_map: Dict[str, str],
    user_id: str,
    name: str,
    serial: str,
    seen: set,
) -> List[Dict[str, Any]]:
    held = _comment_path_held_by_other(rows, header_map, user_id, name, serial)
    own: List[Dict[str, Any]] = []
    free: List[Dict[str, Any]] = []
    seen_own = set(seen)
    seen_free = set(seen)

    def _item(index: int, row: Dict[str, str], link: str, path: str) -> Dict[str, Any]:
        return {
            "row_index": index,
            "link": link,
            "path": path,
            "text": _comment_cell(row, header_map, "text"),
            "edit": _comment_cell(row, header_map, "edit"),
            "label": str(row.get("comment") or f"comment{index + 1}").strip(),
            "account": _comment_cell(row, header_map, "account"),
        }

    for index, row in enumerate(rows):
        if _comment_row_done(row, header_map):
            continue
        assigned = _comment_cell(row, header_map, "account")
        link = _comment_cell(row, header_map, "link")
        path = normalize_post_link(link)
        if not path:
            continue
        if link_commented_by_any_account(link):
            continue
        if assigned and _comment_assigned_to(assigned, user_id, name, serial):
            if path in seen_own:
                continue
            seen_own.add(path)
            own.append(_item(index, row, link, path))
            continue
        if assigned:
            continue
        if path in held or path in seen_free:
            continue
        seen_free.add(path)
        free.append(_item(index, row, link, path))
    return own + free


def take_comment_targets(
    user_id: str,
    limit: int,
    name: str = "",
    serial: str = "",
    *,
    claim: bool = True,
) -> List[Dict[str, Any]]:
    if limit <= 0:
        return []
    with _SHEET_LOCK:
        fieldnames, rows = _read_comments_sheet()
        header_map = _comments_header_map(fieldnames)
        if "link" not in header_map:
            raise RuntimeError("comments.csv needs a link column")

        week_links = {
            normalize_post_link(str(item.get("link") or ""))
            for item in comments_in_window(user_id)
        }
        week_links.discard("")
        candidates = _iter_comment_candidates(
            rows, header_map, user_id, name, serial, set(week_links)
        )
        picked = candidates[:limit]
        if claim and picked:
            for item in picked:
                row = rows[item["row_index"]]
                row[header_map.get("account") or "account"] = (
                    _comment_cell(row, header_map, "account") or user_id or name
                )
                row[header_map.get("status") or "status"] = "in progress"
                item["account"] = row[header_map.get("account") or "account"]
            _write_comments_sheet(fieldnames, rows)
        return picked


def unused_sheet_comment_slots(
    user_id: str, name: str = "", serial: str = ""
) -> int:
    """How many unused comments.csv links this account can still take in 48h."""
    room = comments_remaining(user_id)
    if room <= 0:
        return 0
    try:
        targets = take_comment_targets(
            user_id,
            min(room, SHEET_COMMENTS_PER_RUN),
            name=name,
            serial=serial,
            claim=False,
        )
    except Exception:
        return 0
    return min(len(targets), room, SHEET_COMMENTS_PER_RUN)


def account_has_sheet_comment(user_id: str, name: str = "", serial: str = "") -> bool:
    """True if this account can take one unused comments.csv link this run."""
    return unused_sheet_comment_slots(user_id, name=name, serial=serial) > 0


def mark_comment_row_used(
    row_index: int,
    account: str,
    *,
    comment_url: str = "",
    posted_text: str = "",
) -> None:
    with _SHEET_LOCK:
        fieldnames, rows = _read_comments_sheet()
        if row_index < 0 or row_index >= len(rows):
            return
        header_map = _comments_header_map(fieldnames)
        row = rows[row_index]
        link = _comment_cell(row, header_map, "link")
        path = normalize_post_link(link)
        posted_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        label = str(row.get("comment") or f"comment{row_index + 1}").strip()
        _append_commented_link(
            {
                "commented_at": posted_at,
                "comment": label,
                "link": link,
                "comment_url": comment_url,
                "account": _comment_cell(row, header_map, "account") or account,
                "posted_by": account,
                "text": posted_text or _comment_cell(row, header_map, "text"),
                "edit": _comment_cell(row, header_map, "edit"),
            }
        )
        for item in rows:
            item_path = normalize_post_link(_comment_cell(item, header_map, "link"))
            if item is row or (path and item_path == path):
                _clear_comment_row(item, header_map)
        _write_comments_sheet(fieldnames, rows)
        log(
            f"Saved {label} to {os.path.basename(COMMENTED_LINKS_SHEET)} "
            "and cleared it from comments.csv"
        )


def release_comment_row(row_index: int, reason: str = "retry") -> None:
    """Leave the row for a later retry after a failed comment."""
    with _SHEET_LOCK:
        fieldnames, rows = _read_comments_sheet()
        if row_index < 0 or row_index >= len(rows):
            return
        header_map = _comments_header_map(fieldnames)
        row = rows[row_index]
        if _comment_row_done(row, header_map):
            return
        row[header_map.get("status") or "status"] = reason or "retry"
        _write_comments_sheet(fieldnames, rows)


def sync_comment_sheet_assignments(targets: List[Dict[str, str]]) -> int:
    """Give each new unique comments.csv link to one account that still has room."""
    if not targets:
        return 0
    with _SHEET_LOCK:
        fieldnames, rows = _read_comments_sheet()
        header_map = _comments_header_map(fieldnames)
        if "link" not in header_map:
            return 0
        account_col = header_map.get("account") or "account"
        status_col = header_map.get("status") or "status"
        changed = 0
        for row in rows:
            link = _comment_cell(row, header_map, "link")
            if link and link_in_commented_archive(link):
                _clear_comment_row(row, header_map)
                changed += 1
        seen_paths: set = set()
        for row in rows:
            path = normalize_post_link(_comment_cell(row, header_map, "link"))
            if not path:
                continue
            if path in seen_paths:
                status = _comment_cell(row, header_map, "status").lower()
                if status not in {"skip", "skipped", "commented", "done", "posted", "used"}:
                    row[status_col] = "skip"
                    changed += 1
                    log(f"comments.csv: skipped duplicate link ({path})")
                continue
            seen_paths.add(path)

        def _row_for_target(row: Dict[str, str], target: Dict[str, str]) -> bool:
            assigned = _comment_cell(row, header_map, "account")
            if not assigned:
                return False
            return _comment_assigned_to(
                assigned,
                str(target.get("user_id") or ""),
                str(target.get("name") or ""),
                str(target.get("serial_number") or target.get("serial") or ""),
            )

        pending_count: Dict[str, int] = {}
        sheet_count: Dict[str, int] = {}
        for target in targets:
            uid = str(target.get("user_id") or "")
            if not uid:
                continue
            pending_count[uid] = 0
            sheet_count[uid] = len(
                comments_in_window(uid, GENERAL_COMMENT_DAYS, kind="sheet")
            )
            for row in rows:
                if _comment_row_done(row, header_map):
                    continue
                if not normalize_post_link(_comment_cell(row, header_map, "link")):
                    continue
                if _row_for_target(row, target):
                    pending_count[uid] += 1

        def _sheet_room(target: Dict[str, str]) -> int:
            uid = str(target.get("user_id") or "")
            have = sheet_count.get(uid, 0)
            pending = pending_count.get(uid, 0)
            return max(0, COMMENT_MAX_PER_WEEK - have - pending)

        ranked = sorted(
            enumerate(targets),
            key=lambda pair: (
                sheet_count.get(str(pair[1].get("user_id") or ""), 0)
                + pending_count.get(str(pair[1].get("user_id") or ""), 0),
                pair[0],
            ),
        )
        queue: List[Dict[str, str]] = []
        for _, target in ranked:
            uid = str(target.get("user_id") or "")
            if not uid or _sheet_room(target) <= 0:
                continue
            queue.append(target)

        for row in rows:
            if _comment_row_done(row, header_map):
                continue
            if _comment_cell(row, header_map, "account"):
                continue
            link = _comment_cell(row, header_map, "link")
            path = normalize_post_link(link)
            if not path or link_commented_by_any_account(link):
                continue
            while queue and _sheet_room(queue[0]) <= 0:
                queue.pop(0)
            if not queue:
                break
            target = queue[0]
            uid = str(target.get("user_id") or "")
            row[account_col] = uid
            pending_count[uid] = pending_count.get(uid, 0) + 1
            changed += 1
            have = sheet_count.get(uid, 0)
            held = have + pending_count.get(uid, 0)
            label = str(row.get("comment") or "row").strip() or "row"
            log(
                f"comments.csv: {label} -> {uid} "
                f"({held}/{COMMENT_MAX_PER_WEEK} comments.csv links in 48h)"
            )
            if _sheet_room(target) <= 0:
                queue.pop(0)
            elif len(queue) > 1:
                queue.append(queue.pop(0))

        if changed:
            _write_comments_sheet(fieldnames, rows)
        return changed


def _profile_matches(item: Dict[str, str], needle: str) -> bool:
    target = needle.strip().lower()
    if not target:
        return False
    fields = (
        item.get("user_id") or "",
        item.get("name") or "",
        item.get("serial_number") or "",
        item.get("username") or "",
    )
    return any(target == str(value).strip().lower() for value in fields)


def resolve_profile_id(raw_id: str) -> Dict[str, str]:
    """Accept a user_id, profile name, or serial number and return AdsPower fields."""
    raw = (raw_id or "").strip()
    if not raw:
        raise RuntimeError("Empty profile id")

    cached = _load_profile_cache()
    for item in cached:
        if _profile_matches(item, raw):
            log(f"Resolved {raw!r} from cache -> {item.get('name') or item['user_id']} [{item['user_id']}]")
            return item

    page = 1
    found: Optional[Dict[str, str]] = None
    collected: List[Dict[str, str]] = []
    while page <= 50:
        payload = adspower_get("/api/v1/user/list", params={"page": page, "page_size": 100})
        rows = ((payload.get("data") or {}).get("list") or [])
        if not rows:
            break
        for row in rows:
            item = {
                "user_id": str(row.get("user_id") or ""),
                "name": str(row.get("name") or ""),
                "username": str(row.get("username") or ""),
                "serial_number": str(row.get("serial_number") or ""),
            }
            if item["user_id"]:
                collected.append(item)
            if _profile_matches(item, raw) and found is None:
                found = item
        if len(rows) < 100:
            break
        page += 1

    if collected:
        _save_profile_cache(collected)
    if found:
        log(f"Resolved {raw!r} via API -> {found.get('name') or found['user_id']} [{found['user_id']}]")
        return found

    # Treat the value as a raw user_id if AdsPower will accept it.
    log(f"Using {raw!r} as AdsPower user_id (no list match)")
    return {"user_id": raw, "name": raw, "username": "", "serial_number": ""}


def _xdotool(*args: str, timeout: float = 2.0) -> None:
    """Run xdotool without --sync (that hangs on Wayland) and never raise."""
    try:
        subprocess.run(
            ["xdotool", *args],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except Exception:
        pass


def _adspower_manager_window() -> Tuple[str, int, int, int, int]:
    """Return (window_id, x, y, width, height) for the AdsPower manager app."""
    try:
        out = subprocess.check_output(
            ["xdotool", "search", "--name", "AdsPower Browser"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except Exception as exc:
        raise RuntimeError(f"Could not find the AdsPower window: {exc}") from exc

    best = ""
    best_area = 0
    best_geom = (0, 0, 0, 0)
    for wid in [line.strip() for line in out.splitlines() if line.strip()]:
        try:
            geo = subprocess.check_output(
                ["xdotool", "getwindowgeometry", wid],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
        except Exception:
            continue
        pos_m = re.search(r"Position:\s*(-?\d+),(-?\d+)", geo)
        size_m = re.search(r"Geometry:\s*(\d+)x(\d+)", geo)
        if not pos_m or not size_m:
            continue
        x, y = int(pos_m.group(1)), int(pos_m.group(2))
        w, h = int(size_m.group(1)), int(size_m.group(2))
        area = w * h
        if area > best_area:
            best_area = area
            best = wid
            best_geom = (x, y, w, h)
    if not best or best_area < 200_000:
        raise RuntimeError("AdsPower manager window was not found on screen")
    return best, best_geom[0], best_geom[1], best_geom[2], best_geom[3]


def _click_screen(x: int, y: int) -> None:
    _xdotool("mousemove", str(int(x)), str(int(y)))
    time.sleep(0.05)
    _xdotool("click", "1")


def _screenshot_window(wid: str) -> Optional[Any]:
    if Image is None:
        return None
    path = os.path.join(tempfile.gettempdir(), "adspower_ui_open.png")
    try:
        subprocess.run(
            ["import", "-window", wid, path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=8,
            check=True,
        )
        return Image.open(path).convert("RGB")
    except Exception:
        return None


def _find_open_button(img: Any) -> Optional[Tuple[int, int]]:
    """Locate the top-most blue Open button on the right side of the profile table."""
    width, height = img.size
    hits: List[Tuple[int, int]] = []
    x0 = int(width * 0.72)
    y0 = int(height * 0.28)
    for y in range(y0, height - 8, 2):
        for x in range(x0, width - 8, 2):
            r, g, b = img.getpixel((x, y))
            if b > 170 and r < 90 and 70 < g < 190:
                hits.append((x, y))
    if not hits:
        return None
    min_y = min(y for _, y in hits)
    cluster = [(x, y) for x, y in hits if y <= min_y + 18]
    cx = sum(x for x, _ in cluster) // len(cluster)
    cy = sum(y for _, y in cluster) // len(cluster)
    return cx, cy


def focus_adspower_window() -> Tuple[str, int, int, int, int]:
    global _ADSPOWER_MANAGER_WID
    wid, ox, oy, ww, wh = _adspower_manager_window()
    _ADSPOWER_MANAGER_WID = wid
    _xdotool("windowactivate", wid)
    _xdotool("windowraise", wid)
    log("Brought the AdsPower window to the front")
    time.sleep(0.4)
    return wid, ox, oy, ww, wh


def raise_profile_browser(label: str) -> None:
    """
    Put the profile Chrome in front. Never raise the AdsPower manager —
    that is the window that was being scrolled by mistake.
    In parallel mode Selenium talks to each Chrome over its own debugger,
    so we do not steal focus from the other accounts.
    """
    if PARALLEL_PROFILES:
        return
    try:
        out = subprocess.check_output(
            ["xdotool", "search", "--name", "AdsPower Browser"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except Exception:
        out = ""

    for wid in [line.strip() for line in out.splitlines() if line.strip()]:
        if _ADSPOWER_MANAGER_WID and wid == _ADSPOWER_MANAGER_WID:
            continue
        try:
            geo = subprocess.check_output(
                ["xdotool", "getwindowgeometry", wid],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=3,
            )
        except Exception:
            continue
        size_m = re.search(r"Geometry:\s*(\d+)x(\d+)", geo)
        if not size_m:
            continue
        if int(size_m.group(1)) * int(size_m.group(2)) < 200_000:
            continue
        _xdotool("windowactivate", wid)
        _xdotool("windowraise", wid)
        log(f"[Profile {label}] Profile browser is in front — Reddit activity stays in this window")
        return

    for title in ("Reddit", "reddit.com", "start.adspower"):
        try:
            found = subprocess.check_output(
                ["xdotool", "search", "--name", title],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=3,
            )
        except Exception:
            continue
        wids = [line.strip() for line in found.splitlines() if line.strip()]
        if not wids:
            continue
        _xdotool("windowactivate", wids[-1])
        _xdotool("windowraise", wids[-1])
        log(f"[Profile {label}] Raised the profile window ({title})")
        return
    log(f"[Profile {label}] Look at the new Chrome window beside AdsPower — do not scroll AdsPower")


def opened_profile_ids() -> List[str]:
    """Profiles that AdsPower lists under Opened (Open button became Close)."""
    try:
        payload = adspower_get("/api/v1/browser/local-active")
        rows = ((payload.get("data") or {}).get("list") or [])
        return [str(item.get("user_id") or "") for item in rows if item.get("user_id")]
    except Exception:
        return []


_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_PROXY_BUSY_MARKERS = (
    "assigning",
    "refreshing",
    "checking proxy",
    "checking ip",
    "detecting ip",
    "detecting proxy",
    "getting ip",
    "connecting proxy",
    "please wait",
    "ip checking",
    "proxy checking",
    "waiting for proxy",
    "正在分配",
    "正在刷新",
    "正在检测",
    "检测中",
    "刷新中",
    "分配中",
)


def _is_public_ipv4(ip: str) -> bool:
    parts = (ip or "").split(".")
    if len(parts) != 4:
        return False
    try:
        nums = [int(part) for part in parts]
    except ValueError:
        return False
    if any(num < 0 or num > 255 for num in nums):
        return False
    first, second = nums[0], nums[1]
    if first in {0, 10, 127}:
        return False
    if first == 192 and second == 168:
        return False
    if first == 172 and 16 <= second <= 31:
        return False
    return True


def _profile_listed_ip(user_id: str) -> str:
    try:
        payload = adspower_get(
            "/api/v1/user/list",
            params={"user_id": user_id, "page": 1, "page_size": 1},
        )
        rows = (payload.get("data") or {}).get("list") or []
        if not rows:
            return ""
        ip = str(rows[0].get("ip") or "").strip()
        return ip if _is_public_ipv4(ip) else ""
    except Exception:
        return ""


def wait_for_adspower_proxy_phase(label: str) -> None:
    """Pause after Open so AdsPower can assign, then refresh, the proxy IP."""
    assign_for = _rng().uniform(*PROXY_ASSIGN_WAIT)
    log(
        f"[Profile {label}] Waiting {assign_for:.0f}s for AdsPower to assign the proxy IP "
        "— not opening Reddit yet"
    )
    time.sleep(assign_for)
    refresh_for = _rng().uniform(*PROXY_REFRESH_WAIT)
    log(
        f"[Profile {label}] Waiting {refresh_for:.0f}s for AdsPower to refresh the proxy IP"
    )
    time.sleep(refresh_for)


def _browser_page_text(driver: WebDriver) -> str:
    try:
        return str(
            driver.execute_script(
                "return (document.body && (document.body.innerText || document.body.textContent) || '').slice(0, 8000);"
            )
            or ""
        )
    except Exception:
        return ""


def _proxy_page_busy(text: str) -> bool:
    lowered = (text or "").lower()
    return any(marker in lowered for marker in _PROXY_BUSY_MARKERS)


def _public_ips_in_text(text: str) -> List[str]:
    found: List[str] = []
    seen = set()
    for ip in _IPV4_RE.findall(text or ""):
        if ip in seen or not _is_public_ipv4(ip):
            continue
        seen.add(ip)
        found.append(ip)
    return found


def wait_for_proxy_ip(driver: WebDriver, label: str, user_id: str) -> str:
    """Stay on the AdsPower IP tab until a public IP is assigned and refresh is done."""
    prefix = f"[Profile {label}] "
    log(f"{prefix}Checking the proxy IP page — waiting until assign + refresh finish")
    deadline = time.time() + PROXY_READY_TIMEOUT
    started = time.time()
    last_log = 0.0
    last_list_check = 0.0
    stable_ip = ""
    stable_hits = 0
    listed_ip = ""

    while time.time() < deadline:
        page_ip = ""
        busy = False
        try:
            handles = list(driver.window_handles or [])
        except Exception:
            handles = []
        current = ""
        try:
            current = driver.current_window_handle
        except Exception:
            current = ""
        for handle in handles or [current]:
            if not handle:
                continue
            try:
                if handle != current:
                    driver.switch_to.window(handle)
            except Exception:
                continue
            text = _browser_page_text(driver)
            url = ""
            try:
                url = (driver.current_url or "").lower()
            except Exception:
                url = ""
            if _proxy_page_busy(text):
                busy = True
            ips = _public_ips_in_text(text)
            on_ip_page = any(
                token in url
                for token in ("adspower", "ip-check", "ipcheck", "whatismyip", "ipify")
            )
            if ips and (on_ip_page or "ip" in text.lower() or not busy):
                page_ip = ips[0]
                if on_ip_page or not busy:
                    break
        now = time.time()
        if now - last_list_check >= 6:
            listed = _profile_listed_ip(user_id)
            last_list_check = now
            if listed:
                listed_ip = listed

        ip = page_ip
        if not ip and listed_ip and not busy and (now - started) >= 20:
            ip = listed_ip

        if ip and not busy:
            if ip == stable_ip:
                stable_hits += 1
            else:
                stable_ip = ip
                stable_hits = 1
            if stable_hits >= 2:
                settle = _rng().uniform(*PROXY_SETTLE_DELAY)
                log(f"{prefix}Proxy IP ready: {ip} — waiting {settle:.0f}s to settle")
                time.sleep(settle)
                return ip
            if now - last_log >= 6:
                log(f"{prefix}Proxy IP {ip} seen — confirming refresh finished")
                last_log = now
        else:
            stable_ip = ""
            stable_hits = 0
            if now - last_log >= 8:
                if busy:
                    log(f"{prefix}AdsPower is still assigning or refreshing the proxy IP…")
                else:
                    left = int(max(0, deadline - now))
                    log(f"{prefix}Waiting for proxy IP… {left}s left")
                last_log = now
        time.sleep(2.0)

    if listed_ip:
        log(f"{prefix}Proxy wait timed out — using last listed IP {listed_ip}")
        return listed_ip
    log(f"{prefix}Proxy IP page did not show an address — continuing after the wait")
    return ""


def wait_until_profile_open(user_id: str, label: str, timeout: float = 90.0) -> Dict[str, Any]:
    """Wait until this account's Chrome debugger is ready. Do not require the Opened list."""
    deadline = time.time() + timeout
    data: Dict[str, Any] = {}
    last_log = 0.0
    while time.time() < deadline:
        try:
            payload = adspower_get("/api/v1/browser/active", params={"user_id": user_id})
            info = payload.get("data") or {}
            selenium = ((info.get("ws") or {}).get("selenium") or "").strip()
            status = str(info.get("status") or "").lower()
            if selenium:
                return info
            if status == "active":
                data = info
        except Exception:
            pass
        now = time.time()
        if now - last_log >= 8:
            log(f"[Profile {label}] Waiting for the account browser… {int(max(0, deadline - now))}s left")
            last_log = now
        time.sleep(1.2)
    return data


def click_open_in_adspower(name: str, serial: str, user_id: str, label: str) -> bool:
    """
    Click only a detected blue Open button. Do not click/scroll the AdsPower
    profile list — that was scrolling the AdsPower app instead of Reddit.
    """
    query = (name or serial or user_id or "").strip()
    if not query:
        return False
    try:
        wid, ox, oy, ww, _wh = focus_adspower_window()
    except Exception as exc:
        log(f"[Profile {label}] Cannot focus AdsPower ({brief_error(exc)})")
        return False

    log(f"[Profile {label}] Looking for the Open button ({query!r})")
    try:
        search_x = ox + int(ww * 0.58)
        search_y = oy + 66
        _click_screen(search_x, search_y)
        time.sleep(0.2)
        _xdotool("key", "--clearmodifiers", "ctrl+a")
        _xdotool("type", "--clearmodifiers", "--delay", "18", "--", query, timeout=20)
        _xdotool("key", "--clearmodifiers", "Return")
        time.sleep(1.4)

        img = _screenshot_window(wid)
        click_pos = _find_open_button(img) if img is not None else None
        if click_pos is None:
            log(f"[Profile {label}] Open button not visible — opening the account via API instead")
            return False
        log(f"[Profile {label}] Clicking Open in AdsPower")
        _click_screen(ox + click_pos[0], oy + click_pos[1])
        time.sleep(2.8)
        return True
    except Exception as exc:
        log(f"[Profile {label}] AdsPower Open click skipped ({brief_error(exc)})")
        return False


def _poll_profile_debugger(user_id: str, seconds: float = 16.0) -> Dict[str, Any]:
    deadline = time.time() + seconds
    data: Dict[str, Any] = {}
    while time.time() < deadline:
        try:
            payload = adspower_get("/api/v1/browser/active", params={"user_id": user_id})
            info = payload.get("data") or {}
            if ((info.get("ws") or {}).get("selenium") or "").strip():
                return info
            data = info or data
        except Exception:
            pass
        time.sleep(1.4)
    return data


def _start_profile_via_api(user_id: str, label: str) -> Dict[str, Any]:
    last_error: Optional[BaseException] = None
    for attempt in range(1, 4):
        existing = _poll_profile_debugger(user_id, seconds=2.0)
        if ((existing.get("ws") or {}).get("selenium") or "").strip():
            return existing
        log(f"[Profile {label}] Opening the Reddit account browser (AdsPower Start API, try {attempt}/3)")
        try:
            payload = adspower_get(
                "/api/v1/browser/start",
                params={
                    "user_id": user_id,
                    "open_tabs": 0,
                    "ip_tab": 1,
                    "new_first_tab": 1,
                    "headless": 0,
                    "cdp_mask": 1,
                },
            )
            return payload.get("data") or {}
        except RuntimeError as exc:
            last_error = exc
            message = str(exc).lower()
            log(f"[Profile {label}] Start API failed ({brief_error(exc)}) — waiting for the browser")
            waited = _poll_profile_debugger(user_id, seconds=12.0)
            if ((waited.get("ws") or {}).get("selenium") or "").strip():
                return waited
            if "already" in message or "is open" in message or "being used" in message:
                payload = adspower_get("/api/v1/browser/active", params={"user_id": user_id})
                return payload.get("data") or {}
            time.sleep(2.0 * attempt)
    if last_error:
        raise last_error
    return {}


def start_profile(
    user_id: str,
    label: str,
    name: str = "",
    serial: str = "",
) -> Dict[str, Any]:
    """Open the profile browser. Activity never starts in the AdsPower manager."""
    already = opened_profile_ids()
    if user_id in already:
        log(f"[Profile {label}] Account already open — attaching")
        payload = adspower_get("/api/v1/browser/active", params={"user_id": user_id})
        data = payload.get("data") or {}
        if ((data.get("ws") or {}).get("selenium") or "").strip():
            return data

    if not PARALLEL_PROFILES:
        try:
            focus_adspower_window()
        except Exception:
            pass

    clicked = False
    if not PARALLEL_PROFILES:
        try:
            clicked = click_open_in_adspower(name, serial, user_id, label)
        except Exception as exc:
            log(f"[Profile {label}] Open click skipped ({brief_error(exc)})")
    else:
        log(f"[Profile {label}] Opening via AdsPower Start API (parallel)")

    data: Dict[str, Any] = {}
    if clicked:
        wait_for_adspower_proxy_phase(label)
        log(f"[Profile {label}] Waiting for the account browser after proxy assign/refresh")
        data = wait_until_profile_open(user_id, label, timeout=90.0)
        if ((data.get("ws") or {}).get("selenium") or "").strip():
            log(f"[Profile {label}] Account browser is open")
            return data
        if user_id in opened_profile_ids():
            log(f"[Profile {label}] AdsPower already shows this account as Opened — waiting on debugger")
            data = wait_until_profile_open(user_id, label, timeout=40.0)
            if ((data.get("ws") or {}).get("selenium") or "").strip():
                log(f"[Profile {label}] Account browser is open")
                return data
        log(f"[Profile {label}] Open click did not expose debugger — trying Start API")

    data = _start_profile_via_api(user_id, label)
    selenium_addr = ((data.get("ws") or {}).get("selenium") or "").strip()
    if not selenium_addr:
        time.sleep(3.0)
        payload = adspower_get("/api/v1/browser/active", params={"user_id": user_id})
        data = payload.get("data") or {}
        selenium_addr = ((data.get("ws") or {}).get("selenium") or "").strip()
    if not selenium_addr:
        raise RuntimeError("AdsPower did not open the account browser (no debuggerAddress)")
    log(f"[Profile {label}] Account browser is open — debugger {selenium_addr}")
    return data


def stop_profile(user_id: str, label: str) -> None:
    """Step E — close the profile with /api/v1/browser/stop."""
    try:
        adspower_get("/api/v1/browser/stop", params={"user_id": user_id})
        log(f"[Profile {label}] AdsPower Stop API called")
    except Exception as exc:
        log(f"[Profile {label}] Stop API warning: {brief_error(exc)}")


# =============================================================================
# SELENIUM ATTACH
# =============================================================================

def _normalize_debugger_address(raw: str) -> str:
    address = (raw or "").strip()
    for prefix in ("ws://", "wss://"):
        if address.startswith(prefix):
            address = address[len(prefix) :]
    if "/" in address:
        address = address.split("/", 1)[0]
    return address


def _copy_webdriver(src: str) -> str:
    suffix = ".exe" if os.name == "nt" else ""
    tmp = tempfile.NamedTemporaryFile(prefix="adspower_chromedriver_", suffix=suffix, delete=False)
    tmp.close()
    shutil.copy2(src, tmp.name)
    try:
        os.chmod(tmp.name, 0o755)
    except OSError:
        pass
    return tmp.name


def connect_to_browser(data: Dict[str, Any], label: str) -> WebDriver:
    """Step B — attach to the AdsPower Chrome instance."""
    debugger = _normalize_debugger_address((data.get("ws") or {}).get("selenium") or "")
    webdriver_path = str(data.get("webdriver") or "")
    log(f"[Profile {label}] Connecting Selenium to {debugger}")

    driver: Optional[WebDriver] = None
    if uc is not None:
        copied = _copy_webdriver(webdriver_path) if webdriver_path and os.path.isfile(webdriver_path) else ""
        try:
            options = uc.ChromeOptions()
            try:
                options.debugger_address = debugger
            except Exception:
                options.add_experimental_option("debuggerAddress", debugger)
            kwargs: Dict[str, Any] = {"options": options, "use_subprocess": True}
            if copied:
                kwargs["driver_executable_path"] = copied
            driver = uc.Chrome(**kwargs)
            _ = driver.current_url
            log(f"[Profile {label}] Connected via undetected-chromedriver")
        except Exception as exc:
            log(f"[Profile {label}] undetected-chromedriver attach failed ({brief_error(exc)}); using Selenium")
            driver = None

    if driver is None:
        if webdriver_path and os.path.isfile(webdriver_path):
            driver_path = webdriver_path
        else:
            log(f"[Profile {label}] AdsPower webdriver path missing — using webdriver-manager")
            driver_path = ChromeDriverManager().install()
        options = ChromeOptions()
        options.add_experimental_option("debuggerAddress", debugger)
        driver = webdriver.Chrome(service=Service(executable_path=driver_path), options=options)
        _ = driver.current_url
        log(f"[Profile {label}] Connected via Selenium debuggerAddress")

    driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
    driver.implicitly_wait(0)
    return driver


def close_browser(driver: Optional[WebDriver], user_id: str, label: str) -> None:
    if driver is not None:
        try:
            driver.quit()
        except Exception:
            pass
    stop_profile(user_id, label)


# =============================================================================
# PAGE HELPERS
# =============================================================================

def wait_for_page_ready(driver: WebDriver, timeout: int = ELEMENT_WAIT) -> None:
    WebDriverWait(driver, timeout).until(
        lambda d: d.execute_script("return document.readyState") == "complete"
    )


def navigate(driver: WebDriver, url: str, label: str) -> None:
    """
    Open a URL. AdsPower Chrome sometimes drops the debugger on driver.get(),
    so JS assign is the fallback.
    """
    try:
        driver.get(url)
    except TimeoutException:
        log(f"[Profile {label}] Page load timed out for {url} — continuing")
        return
    except WebDriverException:
        try:
            driver.execute_script("window.location.assign(arguments[0]);", url)
        except WebDriverException:
            raise
    try:
        wait_for_page_ready(driver)
    except TimeoutException:
        log(f"[Profile {label}] Document not complete yet for {url} — continuing")


def current_is_reddit(driver: WebDriver) -> bool:
    try:
        url = (driver.current_url or "").lower()
        title = (driver.title or "").lower()
    except WebDriverException:
        return False
    if "adspower" in url or "start.adspower" in url:
        return False
    return "reddit.com" in url or "reddit" in title


def open_reddit_home_ready(driver: WebDriver, label: str) -> None:
    """Open reddit.com in the profile browser. Do not scroll until this succeeds."""
    raise_profile_browser(label)
    log(f"[Profile {label}] Opening Reddit homepage — no scrolling until it loads")
    navigate(driver, REDDIT_HOME_URL, label)
    deadline = time.time() + PAGE_LOAD_TIMEOUT
    while time.time() < deadline:
        if current_is_reddit(driver):
            dismiss_popups(driver)
            log(f"[Profile {label}] Reddit home is loaded — starting activity here")
            release_all_held_subreddits()
            return
        time.sleep(0.5)
    raise RuntimeError(
        "Reddit homepage did not load in the profile browser. "
        "Will not scroll the AdsPower app."
    )


_HOME_CLICK_JS = r"""
function walk(root, fn) {
  fn(root);
  let nodes;
  try { nodes = root.querySelectorAll('*'); } catch (e) { return; }
  nodes.forEach(el => { if (el.shadowRoot) walk(el.shadowRoot, fn); });
}
function visible(el) {
  try {
    const r = el.getBoundingClientRect();
    const st = window.getComputedStyle(el);
    return r.width > 8 && r.height > 8 && st.visibility !== 'hidden' && st.display !== 'none';
  } catch (e) { return false; }
}
function txt(el) {
  try { return (el.innerText || el.textContent || el.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim().toLowerCase(); } catch (e) { return ''; }
}
const mode = String(arguments[0] || 'any');
const hits = [];
walk(document, root => {
  try {
    root.querySelectorAll('a[href], button, [role="link"], [role="button"]').forEach(el => hits.push(el));
  } catch (e) {}
});
function score(el) {
  const href = ((el.getAttribute && el.getAttribute('href')) || '').toLowerCase();
  const label = txt(el);
  const id = ((el.id || '') + ' ' + (el.className || '')).toLowerCase();
  let s = 0;
  if (href === '/' || href === 'https://www.reddit.com/' || href === 'https://reddit.com/') s += 8;
  if (href === '/home' || href.endsWith('/home/') || href.includes('/?feed=home')) s += 7;
  if (label === 'home' || label === 'reddit home' || label.startsWith('home ')) s += 6;
  if (id.includes('logo') || label.includes('reddit logo') || label === 'reddit') s += 5;
  if (el.getAttribute && (el.getAttribute('aria-label') || '').toLowerCase().includes('home')) s += 5;
  if (mode === 'logo' && (id.includes('logo') || label.includes('logo') || label === 'reddit')) s += 4;
  if (mode === 'nav' && (label === 'home' || href.includes('/home'))) s += 4;
  return s;
}
hits.sort((a, b) => score(b) - score(a));
for (const el of hits) {
  if (!visible(el) || score(el) < 5) continue;
  try {
    el.scrollIntoView({block: 'center', inline: 'nearest'});
    el.click();
    return 'clicked:' + (txt(el) || el.getAttribute('href') || 'home');
  } catch (e) {}
}
return 'miss';
"""


def _on_reddit_home(driver: WebDriver) -> bool:
    try:
        url = (driver.current_url or "").lower().split("?")[0].rstrip("/")
    except Exception:
        return False
    if not current_is_reddit(driver):
        return False
    return url in {
        "https://www.reddit.com",
        "http://www.reddit.com",
        "https://reddit.com",
        "http://reddit.com",
        "https://www.reddit.com/",
        "https://new.reddit.com",
        "https://old.reddit.com",
    } or url.endswith("reddit.com") or "/home" in url


def _home_via_logo(driver: WebDriver) -> bool:
    try:
        result = driver.execute_script(_HOME_CLICK_JS, "logo") or ""
    except Exception:
        return False
    return str(result).startswith("clicked")


def _home_via_nav(driver: WebDriver) -> bool:
    try:
        result = driver.execute_script(_HOME_CLICK_JS, "nav") or ""
    except Exception:
        return False
    return str(result).startswith("clicked")


def _home_via_back(driver: WebDriver, steps: int = 3) -> bool:
    for _ in range(max(1, steps)):
        if _on_reddit_home(driver):
            return True
        try:
            driver.back()
            time.sleep(_rng().uniform(0.7, 1.4))
            wait_for_page_ready(driver, timeout=8)
        except Exception:
            return False
        if _on_reddit_home(driver):
            return True
        try:
            here = (driver.current_url or "").lower()
        except Exception:
            here = ""
        if not current_is_reddit(driver):
            return False
        # Stop if we landed on a non-home feed that is not a thread we came from
        if "/comments/" not in here and "/r/" in here:
            break
    return _on_reddit_home(driver)


def _home_via_js_assign(driver: WebDriver) -> bool:
    try:
        driver.execute_script("window.location.assign(arguments[0]);", REDDIT_HOME_URL)
        time.sleep(_rng().uniform(1.0, 2.0))
        wait_for_page_ready(driver, timeout=12)
        return current_is_reddit(driver)
    except Exception:
        return False


def return_to_reddit_home(
    driver: WebDriver,
    label: str = "",
    *,
    reason: str = "",
    methods: Optional[List[str]] = None,
) -> str:
    """
    Get back to Reddit Home using a random path so sittings do not always
    look like a direct navigate.
    """
    prefix = f"[Profile {label}] " if label else ""
    raise_profile_browser(label or "browser")
    if _on_reddit_home(driver):
        dismiss_popups(driver)
        release_all_held_subreddits()
        return "already-home"

    order = list(
        methods
        or _rng().sample(
            ["logo", "nav", "back", "js", "navigate"],
            k=5,
        )
    )
    # Always keep navigate as last-resort if not already listed
    if "navigate" not in order:
        order.append("navigate")

    for method in order:
        ok = False
        try:
            if method == "logo":
                log(f"{prefix}Back to Home via Reddit logo ({reason or 'hop'})")
                ok = _home_via_logo(driver)
            elif method == "nav":
                log(f"{prefix}Back to Home via Home nav ({reason or 'hop'})")
                ok = _home_via_nav(driver)
            elif method == "back":
                log(f"{prefix}Back to Home via browser Back ({reason or 'hop'})")
                ok = _home_via_back(driver, steps=_rng().randint(1, 4))
            elif method == "js":
                log(f"{prefix}Back to Home via page jump ({reason or 'hop'})")
                ok = _home_via_js_assign(driver)
            else:
                log(f"{prefix}Back to Home via open homepage ({reason or 'hop'})")
                open_reddit_home_ready(driver, label or "browser")
                return "navigate"
        except Exception as exc:
            log(f"{prefix}Home via {method} failed ({brief_error(exc)})")
            ok = False
        if ok:
            time.sleep(_rng().uniform(0.8, 1.8))
            dismiss_popups(driver)
            if current_is_reddit(driver):
                if not _on_reddit_home(driver):
                    # Click landed somewhere on Reddit but not Home — finish with navigate
                    try:
                        navigate(driver, REDDIT_HOME_URL, label or "browser")
                    except Exception:
                        pass
                log(f"{prefix}Reddit home ready ({method})")
                release_all_held_subreddits()
                return method
    open_reddit_home_ready(driver, label or "browser")
    release_all_held_subreddits()
    return "navigate"


def normalize_subreddit(name: str) -> str:
    value = (name or "").strip()
    if value.lower().startswith("r/"):
        value = value[2:]
    return value.strip("/")


def subreddit_from_url(url: str) -> str:
    match = re.search(r"/r/([^/?#]+)", url or "", re.I)
    return match.group(1) if match else ""


_FETCH_JSON_JS = r"""
const url = arguments[0];
const done = arguments[arguments.length - 1];
fetch(url, {credentials: 'include', headers: {Accept: 'application/json'}})
  .then(async (r) => {
    const text = await r.text();
    try { done(JSON.parse(text)); }
    catch (e) { done({error: 'not-json', status: r.status}); }
  })
  .catch((e) => done({error: String(e)}));
"""


def reddit_session_json(driver: WebDriver, url: str, label: str = "") -> Dict[str, Any]:
    """Read a reddit.com JSON endpoint using the logged-in AdsPower session."""
    prefix = f"[Profile {label}] " if label else ""
    try:
        driver.set_script_timeout(20)
        data = driver.execute_async_script(_FETCH_JSON_JS, url)
        if isinstance(data, dict) and not data.get("error"):
            return data
    except Exception as exc:
        log(f"{prefix}JSON fetch failed for {url.split('?')[0][-60:]} ({brief_error(exc)})")
    return {}


_HOME_STATUS_JS = r"""
function walk(root, fn) {
  fn(root);
  let nodes;
  try { nodes = root.querySelectorAll('*'); } catch (e) { return; }
  nodes.forEach(el => { if (el.shadowRoot) walk(el.shadowRoot, fn); });
}
const headings = [];
let login = false;
let avatar = false;
walk(document, root => {
  try {
    root.querySelectorAll('h1, h2, [role="heading"]').forEach(el => {
      const t = ((el.innerText || el.textContent || '') + '').replace(/\s+/g, ' ').trim();
      if (t && t.length < 140) headings.push(t);
    });
    root.querySelectorAll('a, button, [role="button"]').forEach(el => {
      const t = ((el.innerText || el.textContent || el.getAttribute('aria-label') || '') + '')
        .replace(/\s+/g, ' ').trim().toLowerCase();
      if (t === 'log in' || t === 'login' || t === 'sign up' || t === 'sign in') login = true;
      if (t.includes('profile menu') || t.includes('user menu') || t.includes('avatar')) avatar = true;
    });
  } catch (e) {}
});
let body = '';
try { body = ((document.body && document.body.innerText) || '').slice(0, 1200); } catch (e) {}
let posts = 0;
try { posts = document.querySelectorAll('shreddit-post, article shreddit-post').length; } catch (e) {}
return {
  href: location.href || '',
  title: document.title || '',
  body: body,
  headings: headings.slice(0, 12),
  login: login,
  avatar: avatar,
  posts: posts
};
"""

_BAN_PHRASES = (
    "this account has been suspended",
    "this account has been banned",
    "account has been permanently banned",
    "your account has been banned",
    "your account is suspended",
    "you've been banned",
    "you have been banned",
    "permanently suspended",
)

_SERVER_PHRASES = (
    "server error",
    "internal server error",
    "bad gateway",
    "service unavailable",
    "gateway timeout",
    "we had a server error",
    "we had some trouble",
)


def home_account_block_reason(driver: WebDriver, logged_in: bool) -> str:
    """What the open Home page is showing, or '' when this account can continue."""
    try:
        page = driver.execute_script(_HOME_STATUS_JS) or {}
    except Exception:
        page = {}
    if not isinstance(page, dict):
        page = {}
    title = str(page.get("title") or "").lower()
    href = str(page.get("href") or "").lower()
    body = str(page.get("body") or "").lower()
    headings = " ".join(str(item) for item in (page.get("headings") or [])).lower()
    shown = f"{title}\n{headings}\n{body[:700]}"
    if any(phrase in shown for phrase in _BAN_PHRASES):
        return "account banned"
    posts = int(page.get("posts") or 0)
    server_page = posts == 0 and (
        any(phrase in f"{title} {headings}" for phrase in _SERVER_PHRASES)
        or bool(re.search(r"\b50[234]\b", title))
    )
    if server_page:
        return "server error"
    if logged_in:
        return ""
    login_page = "/login" in href or "/register" in href or bool(page.get("login"))
    if login_page or not page.get("avatar"):
        return "not logged in"
    return ""


_PROFILE_ICON_JS = r"""
function walk(root, fn) {
  fn(root);
  let nodes;
  try { nodes = root.querySelectorAll('*'); } catch (e) { return; }
  nodes.forEach(el => { if (el.shadowRoot) walk(el.shadowRoot, fn); });
}
function visible(el) {
  try {
    const r = el.getBoundingClientRect();
    const st = window.getComputedStyle(el);
    return r.width > 10 && r.height > 10 && r.top >= 0 && r.top < 160
      && st.visibility !== 'hidden' && st.display !== 'none' && Number(st.opacity || 1) > 0.1;
  } catch (e) { return false; }
}
let best = null;
let bestScore = 0;
walk(document, root => {
  let nodes = [];
  try { nodes = root.querySelectorAll('button, a, [role="button"], img'); } catch (e) { return; }
  nodes.forEach(el => {
    if (!visible(el)) return;
    const r = el.getBoundingClientRect();
    if (r.left < window.innerWidth * 0.5) return;
    const blob = (
      (el.id || '') + ' ' +
      (el.getAttribute('aria-label') || '') + ' ' +
      (el.getAttribute('alt') || '') + ' ' +
      (el.getAttribute('noun') || '') + ' ' +
      (el.getAttribute('title') || '')
    ).toLowerCase();
    let score = r.right / window.innerWidth;
    if (blob.includes('expand-user-drawer')) score += 12;
    if (blob.includes('user_avatar') || blob.includes('user-avatar') || blob.includes('avatar')) score += 8;
    if (blob.includes('profile menu') || blob.includes('user menu') || blob.includes('open profile')) score += 8;
    if (blob.includes('profile')) score += 3;
    if (el.tagName === 'IMG' && r.width <= 72 && r.height <= 72) score += 4;
    if (score > bestScore) { bestScore = score; best = el; }
  });
});
if (best && best.tagName === 'IMG' && best.closest) {
  best = best.closest('button, a, [role="button"]') || best;
}
return bestScore >= 3 ? best : null;
"""

_HOVER_ERROR_JS = r"""
function walk(root, fn) {
  fn(root);
  let nodes;
  try { nodes = root.querySelectorAll('*'); } catch (e) { return; }
  nodes.forEach(el => { if (el.shadowRoot) walk(el.shadowRoot, fn); });
}
function visible(el) {
  try {
    const r = el.getBoundingClientRect();
    const st = window.getComputedStyle(el);
    return r.width > 4 && r.height > 4
      && st.visibility !== 'hidden' && st.display !== 'none' && Number(st.opacity || 1) > 0.05;
  } catch (e) { return false; }
}
const bits = [];
walk(document, root => {
  let nodes = [];
  try { nodes = root.querySelectorAll('*'); } catch (e) { return; }
  nodes.forEach(el => {
    if (!visible(el)) return;
    try {
      if (el.closest && el.closest('shreddit-post, article, shreddit-feed')) return;
    } catch (e) {}
    const t = ((el.innerText || el.textContent || '') + ' ' + (el.getAttribute('title') || '') + ' ' + (el.getAttribute('aria-label') || ''))
      .replace(/\s+/g, ' ').trim().toLowerCase();
    if (!t || t.length > 320) return;
    if (t.includes('we had a server') || t.includes('we had server')) bits.push(t);
  });
});
return bits.join('\n');
"""


def _profile_icon(driver: WebDriver):
    try:
        return driver.execute_script(_PROFILE_ICON_JS)
    except Exception:
        return None


def _hover_popup_text(driver: WebDriver) -> str:
    try:
        return str(driver.execute_script(_HOVER_ERROR_JS) or "").lower()
    except Exception:
        return ""


def _popup_says_server_error(text: str) -> bool:
    folded = re.sub(r"\s+", " ", text or "")
    return "we had a server" in folded or "we had server" in folded


def profile_icon_shows_server_error(driver: WebDriver, label: str) -> bool:
    """Hold the cursor on the profile icon. That popup means the account is banned."""
    icon = None
    deadline = time.time() + 8.0
    while time.time() < deadline and icon is None:
        icon = _profile_icon(driver)
        if icon is None:
            time.sleep(0.4)
    if icon is None:
        log(f"[Profile {label}] No profile icon on Home — ban hover skipped")
        return False
    log(f"[Profile {label}] Holding the cursor on the profile icon")
    try:
        driver.execute_script(
            "arguments[0].scrollIntoView({block:'center', inline:'nearest'});", icon
        )
    except Exception:
        pass
    try:
        ActionChains(driver).move_to_element(icon).pause(1.6).perform()
    except Exception as exc:
        log(f"[Profile {label}] Could not move onto the profile icon ({brief_error(exc)})")
        return False
    try:
        driver.execute_script(
            """
            const el = arguments[0];
            for (const type of ['mouseenter', 'mouseover', 'mousemove']) {
              el.dispatchEvent(new MouseEvent(type, {bubbles: true, cancelable: true, view: window}));
            }
            """,
            icon,
        )
    except Exception:
        pass
    time.sleep(1.4)
    if _popup_says_server_error(_hover_popup_text(driver)):
        log(f"[Profile {label}] Profile icon showed we had a server error")
        return True
    try:
        ActionChains(driver).move_to_element(icon).pause(0.3).click().perform()
    except Exception:
        pass
    time.sleep(1.2)
    if _popup_says_server_error(_hover_popup_text(driver)):
        log(f"[Profile {label}] Profile icon showed we had a server error")
        return True
    try:
        ActionChains(driver).send_keys(Keys.ESCAPE).perform()
    except Exception:
        pass
    return False


def _account_payload_suspended(data: Dict[str, Any]) -> bool:
    """True when Reddit's own account object says this user is suspended."""
    if not isinstance(data, dict):
        return False
    if data.get("is_suspended") is True:
        return True
    inner = data.get("data")
    if isinstance(inner, dict) and inner.get("is_suspended") is True:
        return True
    return False


def read_logged_in_account(driver: WebDriver, label: str) -> Dict[str, Any]:
    payload = reddit_session_json(driver, "https://www.reddit.com/api/v1/me.json", label)
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    if not isinstance(data, dict):
        data = {}
    name = str(data.get("name") or "").strip()
    if not name:
        payload = reddit_session_json(driver, "https://www.reddit.com/api/me.json", label)
        wrapped = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        if isinstance(wrapped, dict):
            data = wrapped
            name = str(data.get("name") or "").strip()
    created = 0.0
    try:
        created = float(data.get("created_utc") or data.get("created") or 0)
    except (TypeError, ValueError):
        created = 0.0
    age_days = ((time.time() - created) / 86400.0) if created else 0.0
    try:
        total = int(data.get("total_karma") or 0)
    except (TypeError, ValueError):
        total = 0
    try:
        link_karma = int(data.get("link_karma") or 0)
        comment_karma = int(data.get("comment_karma") or 0)
    except (TypeError, ValueError):
        link_karma = 0
        comment_karma = 0
    if total <= 0:
        total = link_karma + comment_karma
    suspended = _account_payload_suspended(data)
    if name and not suspended:
        about = reddit_session_json(
            driver, f"https://www.reddit.com/user/{name}/about.json", label
        )
        about_data = about.get("data") if isinstance(about.get("data"), dict) else about
        if _account_payload_suspended(about_data if isinstance(about_data, dict) else {}):
            suspended = True
    return {
        "username": name,
        "karma": max(0, total),
        "link_karma": max(0, link_karma),
        "comment_karma": max(0, comment_karma),
        "age_days": max(0.0, age_days),
        "created_utc": created,
        "suspended": suspended,
    }


def _visible_elements(driver: WebDriver, by: By, selector: str, limit: int = 20) -> List[Any]:
    found = []
    try:
        for element in driver.find_elements(by, selector)[: limit * 2]:
            try:
                if element.is_displayed():
                    found.append(element)
            except StaleElementReferenceException:
                continue
            if len(found) >= limit:
                break
    except WebDriverException:
        pass
    return found


def human_click(driver: WebDriver, element: Any) -> bool:
    try:
        driver.execute_script(
            "arguments[0].scrollIntoView({block:'center', inline:'nearest'});", element
        )
        time.sleep(_rng().uniform(0.2, 0.5))
    except Exception:
        pass
    try:
        ActionChains(driver).move_to_element(element).pause(_rng().uniform(0.1, 0.35)).click().perform()
        return True
    except Exception:
        pass
    try:
        element.click()
        return True
    except Exception:
        pass
    try:
        driver.execute_script("arguments[0].click();", element)
        return True
    except Exception:
        return False


# Welcome / cookie / NSFW dialogs. Never click a bare "Close" on the page —
# that can close the AdsPower tab.
_DISMISS_JS = r"""
const LABELS = ['done', 'next', 'got it', 'continue', 'accept all', 'accept', 'not now', 'no thanks', 'skip'];
function collect(root, out) {
  try { root.querySelectorAll('button, a, [role="button"]').forEach(el => out.push(el)); } catch (e) {}
  try {
    root.querySelectorAll('*').forEach(el => { if (el.shadowRoot) collect(el.shadowRoot, out); });
  } catch (e) {}
}
const nodes = [];
collect(document, nodes);
let clicked = 0;
for (const el of nodes) {
  let t = '';
  try { t = ((el.innerText || el.textContent || '') + ' ' + (el.getAttribute('aria-label') || '')).replace(/\s+/g, ' ').trim().toLowerCase(); } catch (e) {}
  if (!t) continue;
  const match = LABELS.some(l => t === l || t.startsWith(l + ' '));
  let inDialog = false;
  try {
    inDialog = !!(el.closest && el.closest('dialog, [role="dialog"], [aria-modal="true"]'));
  } catch (e) {}
  if (match && inDialog) {
    try { el.click(); clicked += 1; } catch (e) {}
    if (clicked >= 2) break;
  }
}
return clicked;
"""


def dismiss_popups(driver: WebDriver) -> None:
    try:
        clicked = driver.execute_script(_DISMISS_JS)
        if clicked:
            time.sleep(_rng().uniform(0.3, 0.8))
    except JavascriptException:
        pass

    xpaths = [
        "//*[@role='dialog']//button[normalize-space()='Done']",
        "//*[@role='dialog']//button[normalize-space()='Next']",
        "//*[@role='dialog']//button[normalize-space()='Got it']",
        "//*[@role='dialog']//button[normalize-space()='Continue']",
        "//button[normalize-space()='Accept all']",
        "//button[normalize-space()='Accept All']",
        "//button[contains(., 'over 18')]",
    ]
    for xpath in xpaths:
        for element in _visible_elements(driver, By.XPATH, xpath, limit=2):
            human_click(driver, element)
            time.sleep(_rng().uniform(0.25, 0.6))


# =============================================================================
# JOIN
# =============================================================================

JOIN_XPATHS = [
    "//button[normalize-space()='Join']",
    "//button[.//span[normalize-space()='Join']]",
    "//*[@role='button' and normalize-space()='Join']",
    "//button[contains(@aria-label, 'Join') and not(contains(@aria-label, 'Joined'))]",
    "//span[normalize-space()='Join']/ancestor::button[1]",
    "//shreddit-join-button//button",
    "//faceplate-tracker[@noun='join']//button",
    "//button[contains(@class, 'join') and not(contains(., 'Joined'))]",
]

JOIN_CSS = [
    "shreddit-join-button",
    "button[aria-label*='Join' i]",
    "button[slot='join-button']",
    "[noun='join'] button",
]

ALREADY_JOINED_XPATHS = [
    "//button[normalize-space()='Joined']",
    "//button[normalize-space()='Leave']",
    "//button[contains(@aria-label, 'Joined')]",
    "//button[contains(@aria-label, 'Leave')]",
    "//span[normalize-space()='Joined']/ancestor::button[1]",
    "//*[@role='button' and (normalize-space()='Joined' or normalize-space()='Leave')]",
]

_JOIN_JS = r"""
const ACTION = arguments[0];
function collect(root, out) {
  try { root.querySelectorAll('button, a, [role="button"]').forEach(el => out.push(el)); } catch (e) {}
  try { root.querySelectorAll('*').forEach(el => { if (el.shadowRoot) collect(el.shadowRoot, out); }); } catch (e) {}
}
function visible(el) {
  try {
    const r = el.getBoundingClientRect();
    const st = window.getComputedStyle(el);
    return r.width > 2 && r.height > 2 && st.visibility !== 'hidden' && st.display !== 'none';
  } catch (e) { return false; }
}
function textOf(el) {
  return ((el.innerText || el.textContent || '') + ' ' + (el.getAttribute('aria-label') || ''))
    .replace(/\s+/g, ' ').trim().toLowerCase();
}
const nodes = [];
collect(document, nodes);
let already = null;
let join = null;
for (const el of nodes) {
  if (!visible(el)) continue;
  const t = textOf(el);
  if (!t) continue;
  if (t === 'joined' || t.startsWith('joined ') || t === 'leave' || t === 'unsubscribe') {
    already = el;
    break;
  }
  if ((t === 'join' || t === 'join community' || t === 'subscribe') && !join) {
    join = el;
  }
}
if (already) return {status: 'already_joined', text: textOf(already)};
if (join && ACTION === 'click_join') {
  try { join.click(); return {status: 'clicked', text: textOf(join)}; } catch (e) {}
}
if (join) return {status: 'found_join', text: textOf(join)};
return {status: 'not_found', text: ''};
"""


def _already_joined(driver: WebDriver) -> bool:
    js_state = {}
    try:
        js_state = driver.execute_script(_JOIN_JS, "inspect") or {}
    except Exception:
        js_state = {}
    if js_state.get("status") == "already_joined":
        return True
    for xpath in ALREADY_JOINED_XPATHS:
        if _visible_elements(driver, By.XPATH, xpath, limit=1):
            return True
    return False


def _find_join_button(driver: WebDriver) -> Optional[Any]:
    for xpath in JOIN_XPATHS:
        elements = _visible_elements(driver, By.XPATH, xpath, limit=3)
        if elements:
            return elements[0]
    for css in JOIN_CSS:
        elements = _visible_elements(driver, By.CSS_SELECTOR, css, limit=3)
        if elements:
            return elements[0]
    return None


def join_subreddit(driver: WebDriver, label: str, subreddit: str) -> str:
    """
    Click Join. Returns joined | already_joined | skipped | failed.
    """
    if _already_joined(driver):
        log(f"[Profile {label}] Already a member of r/{subreddit} — skipping Join")
        return "already_joined"

    try:
        js_state = driver.execute_script(_JOIN_JS, "click_join") or {}
    except Exception:
        js_state = {}
    clicked = js_state.get("status") == "clicked"

    if not clicked:
        button = _find_join_button(driver)
        if button is None:
            try:
                WebDriverWait(driver, 6).until(
                    lambda d: _find_join_button(d) is not None or _already_joined(d)
                )
            except TimeoutException:
                pass
            if _already_joined(driver):
                log(f"[Profile {label}] Already a member of r/{subreddit} — skipping Join")
                return "already_joined"
            button = _find_join_button(driver)
        if button is None:
            log(f"[Profile {label}] Join button not found on r/{subreddit}")
            return "failed"
        if not human_click(driver, button):
            log(f"[Profile {label}] Join click did not fire on r/{subreddit}")
            return "failed"

    time.sleep(_rng().uniform(0.8, 1.6))
    dismiss_popups(driver)
    time.sleep(_rng().uniform(0.3, 0.8))
    dismiss_popups(driver)
    log(f"[Profile {label}] Clicked Join on r/{subreddit}")
    return "joined"


_RULES_PAGE_JS = r"""
function collect(root, out) {
  try { root.querySelectorAll('h1,h2,h3,h4,li,p,[data-testid],[slot]').forEach(el => out.push(el)); } catch (e) {}
  try { root.querySelectorAll('*').forEach(el => { if (el.shadowRoot) collect(el.shadowRoot, out); }); } catch (e) {}
}
function visible(el) {
  try {
    const r = el.getBoundingClientRect();
    const st = window.getComputedStyle(el);
    return r.width > 8 && r.height > 8 && st.visibility !== 'hidden' && st.display !== 'none';
  } catch (e) { return false; }
}
function textOf(el) {
  return ((el.innerText || el.textContent || '')).replace(/\s+/g, ' ').trim();
}
const nodes = [];
collect(document, nodes);
const titles = [];
const seen = new Set();
for (const el of nodes) {
  if (!visible(el)) continue;
  const tag = (el.tagName || '').toLowerCase();
  const t = textOf(el);
  if (!t || t.length < 3 || t.length > 180) continue;
  const low = t.toLowerCase();
  if (low === 'rules' || low.startsWith('r/') || low.includes('cookie') || low.includes('reddit')) continue;
  if (tag === 'h1' || tag === 'h2' || tag === 'h3' || tag === 'h4' || tag === 'li') {
    if (seen.has(low)) continue;
    seen.add(low);
    titles.push(t);
  }
  if (titles.length >= 16) break;
}
return {titles: titles, href: location.href || ''};
"""


def _store_rules(stats: AccountSummary, rules: Any) -> Any:
    name = normalize_subreddit(getattr(rules, "subreddit", "") or "")
    if not name:
        return rules
    stats.rules_by_sub[name.lower()] = rules
    if name not in stats.rules_read and (getattr(rules, "rule_count", 0) or getattr(rules, "summary", "")):
        stats.rules_read.append(name)
    return rules


def _rules_for(stats: AccountSummary, subreddit: str) -> Any:
    name = normalize_subreddit(subreddit)
    if not name:
        return None
    return (stats.rules_by_sub or {}).get(name.lower())


def fetch_subreddit_rules_json(
    driver: WebDriver,
    label: str,
    subreddit: str,
) -> Any:
    from reddit_joiner.rules import empty_rules, parse_rules_payload

    name = normalize_subreddit(subreddit)
    payload = reddit_session_json(
        driver, f"https://www.reddit.com/r/{name}/about/rules.json", label
    )
    if payload:
        parsed = parse_rules_payload(name, payload, source="json")
        if parsed.rule_count:
            return parsed
    return empty_rules(name, source="json")


def read_subreddit_rules(
    driver: WebDriver,
    label: str,
    subreddit: str,
    stats: AccountSummary,
    *,
    open_page: bool = True,
) -> Any:
    """
    Read public community rules. On join, opens /r/sub/about/rules and
    also pulls rules.json. Cached for a week so later comments can reuse them.
    """
    from reddit_joiner.rules import (
        empty_rules,
        load_cached,
        merge_rules,
        parse_page_rules,
        save_cached,
    )

    name = normalize_subreddit(subreddit)
    prefix = f"[Profile {label}] "
    existing = _rules_for(stats, name)
    if existing is not None and getattr(existing, "rule_count", 0):
        return existing

    cached = load_cached(name)
    page_rules = empty_rules(name, source="page")
    json_rules = empty_rules(name, source="json")
    if open_page:
        rules_url = f"https://www.reddit.com/r/{name}/about/rules"
        log(f"{prefix}Checking r/{name} rules")
        try:
            navigate(driver, rules_url, label)
            dismiss_popups(driver)
            try:
                WebDriverWait(driver, ELEMENT_WAIT).until(
                    EC.presence_of_element_located((By.TAG_NAME, "body"))
                )
            except TimeoutException:
                log(f"{prefix}r/{name} rules page was slow — still trying to read them")
            try:
                human_scroll(driver, 1, remaining=6.0)
            except Exception:
                pass
            time.sleep(_rng().uniform(8.0, 16.0))
            try:
                scraped = driver.execute_script(_RULES_PAGE_JS) or {}
            except Exception:
                scraped = {}
            titles = list((scraped or {}).get("titles") or [])
            if titles:
                page_rules = parse_page_rules(name, titles, source="page")
        except Exception as exc:
            log(f"{prefix}Could not open r/{name} rules page ({brief_error(exc)})")
        try:
            json_rules = fetch_subreddit_rules_json(driver, label, name)
        except Exception:
            json_rules = empty_rules(name, source="json")
        try:
            navigate(driver, community_entry_url(name), label)
            dismiss_popups(driver)
        except Exception:
            pass
    else:
        try:
            json_rules = fetch_subreddit_rules_json(driver, label, name)
        except Exception:
            json_rules = empty_rules(name, source="json")

    rules = merge_rules(json_rules, page_rules)
    if not rules.rule_count and cached is not None:
        rules = cached
        rules.source = f"cache:{rules.source}" if rules.source else "cache"
    if rules.rule_count:
        try:
            save_cached(rules)
        except Exception:
            pass
        shown = ", ".join(rules.titles[:6]) or rules.summary[:80]
        log(
            f"{prefix}r/{name} rules ({rules.rule_count}, {rules.source}): {shown}"
        )
    else:
        log(f"{prefix}No public rules listed for r/{name}")
    return _store_rules(stats, rules)


def decide_join_policy(
    stats: AccountSummary,
    subreddit: str,
    join_status: str,
    rules: Any,
) -> str:
    """
    After reading rules, RL chooses comment vs lurk for this community.
    Later comment live/removed outcomes train that choice.
    """
    name = normalize_subreddit(subreddit)
    state = _rl_state(stats, name, kind="join", rules=rules)
    if join_status in {"joined", "already_joined"}:
        _rl_learn(stats, state, f"join:{join_status}", REWARD_JOIN_OK)
    elif join_status == "failed":
        _rl_learn(stats, state, "join:failed", REWARD_JOIN_FAIL)

    policy = "comment"
    join_actions = ("join:comment", "join:lurk")
    chosen = "join:comment"
    agent = _rl_agent()
    if agent is not None and RL_ENABLED:
        try:
            from reddit_joiner.rl import parse_join_action

            chosen = agent.choose_action(state, join_actions) or "join:comment"
            policy = parse_join_action(chosen)
            log(
                f"[Profile {stats.name}] RL join action: {chosen} "
                f"on r/{name} (ε={agent.epsilon:.3f})"
            )
        except Exception as exc:
            log(f"[Profile {stats.name}] RL join choice skipped ({brief_error(exc)})")
            chosen = "join:comment"

    need_comment = (
        int(stats.session_comment_target or 0) > 0
        and int(stats.comments or 0) < int(stats.session_comment_target or 0)
        and name.lower() in {item.lower() for item in allowed_subreddits()}
    )
    flags = getattr(rules, "flags", None) or {}
    strict = float(getattr(rules, "strictness", 0) or 0)
    rules_limit = bool(flags.get("account_gate")) or strict >= 0.45
    if need_comment and policy == "lurk" and not rules_limit:
        log(
            f"[Profile {stats.name}] RL chose lurk on r/{name} — still commenting "
            "because this sitting still needs a general comment"
        )
        policy = "comment"
        chosen = "join:comment"
    elif policy == "lurk" and rules_limit:
        log(
            f"[Profile {stats.name}] r/{name} rules say be careful "
            "(karma, account age, or a strict community) — browsing only"
        )

    if policy == "lurk":
        reward = REWARD_JOIN_LURK_STRICT if strict >= 0.45 else -0.1
        if name and name not in stats.lurk_subs:
            stats.lurk_subs.append(name)
    else:
        reward = REWARD_JOIN_COMMENT_OPEN if strict < 0.45 else -0.1
    _rl_learn(stats, state, chosen, reward, next_actions=list(join_actions))
    return policy


# =============================================================================
# HUMAN ACTIVITY
# =============================================================================

POST_SELECTORS = [
    (By.CSS_SELECTOR, "shreddit-post a[slot='title']"),
    (By.CSS_SELECTOR, "shreddit-post a[id^='post-title']"),
    (By.CSS_SELECTOR, "a[data-click-id='body']"),
    (By.CSS_SELECTOR, "article a[href*='/comments/']"),
    (By.XPATH, "//shreddit-post//a[contains(@href, '/comments/')]"),
    (By.XPATH, "//a[contains(@href, '/r/') and contains(@href, '/comments/')]"),
]


_POST_COMMENT_COUNT_JS = r"""
const el = arguments[0];
function walkUp(node) {
  let cur = node;
  for (let i = 0; i < 12 && cur; i++) {
    const tag = (cur.tagName || '').toLowerCase();
    if (tag === 'shreddit-post' || tag === 'article' || (cur.getAttribute && cur.getAttribute('data-testid') === 'post-container')) {
      return cur;
    }
    cur = cur.parentElement || (cur.getRootNode && cur.getRootNode().host) || null;
  }
  return null;
}
const post = walkUp(el) || el;
const attrs = ['comment-count', 'commentcount', 'num-comments', 'score-comment'];
for (const name of attrs) {
  try {
    const raw = post.getAttribute && post.getAttribute(name);
    if (raw != null && String(raw).trim() !== '') {
      const n = parseInt(String(raw).replace(/[^0-9]/g, ''), 10);
      if (!isNaN(n)) return n;
    }
  } catch (e) {}
}
try {
  const label = (post.getAttribute('aria-label') || '') + ' ' + (post.innerText || '');
  const m = label.match(/(\d[\d,]*)\s*comments?/i);
  if (m) return parseInt(m[1].replace(/,/g, ''), 10);
} catch (e) {}
return -1;
"""


def _element_comment_count(driver: WebDriver, element: Any) -> int:
    if element is None:
        return -1
    try:
        value = driver.execute_script(_POST_COMMENT_COUNT_JS, element)
        return int(value)
    except Exception:
        return -1


def _element_has_replies(driver: WebDriver, element: Any) -> bool:
    if not REQUIRE_POST_REPLIES:
        return True
    count = _element_comment_count(driver, element)
    if count < 0:
        # Unknown — allow (feed cards sometimes hide the count)
        return True
    return count >= MIN_POST_REPLIES


def _element_interest(driver: WebDriver, element: Any) -> float:
    """Cheap interest score for a feed card: a meatier title draws the eye."""
    try:
        text = driver.execute_script(
            "return ((arguments[0].innerText || '').trim()).length;", element
        )
        return float(max(1, int(text or 1)))
    except Exception:
        return 1.0


def _random_post_title(driver: WebDriver) -> Optional[Any]:
    pool: List[Any] = []
    for by, selector in POST_SELECTORS:
        for element in _visible_elements(driver, by, selector, limit=12):
            if _element_is_media_post(driver, element):
                continue
            if not _element_has_replies(driver, element):
                continue
            pool.append(element)
        if len(pool) >= 8:
            break
        if len(pool) >= 8:
            break
    if not pool:
        return None
    # Lean toward the more interesting card, but with enough jitter that it is
    # not always the same "best" pick — people do not scan a feed optimally.
    ranked = human.order_by_human_interest(
        pool, lambda el: _element_interest(driver, el)
    )
    return ranked[0]


def _listing_post_elements(driver: WebDriver, limit: int = 10) -> List[Any]:
    pool: List[Any] = []
    seen = set()
    for by, selector in POST_SELECTORS:
        for element in _visible_elements(driver, by, selector, limit=16):
            try:
                href = (element.get_attribute("href") or "").split("?")[0].rstrip("/").lower()
            except StaleElementReferenceException:
                continue
            if "/comments/" not in href or href in seen:
                continue
            if _element_is_media_post(driver, element):
                continue
            if not _element_has_replies(driver, element):
                continue
            seen.add(href)
            pool.append(element)
            if len(pool) >= limit:
                return pool
    return pool


def _listing_sort_label(sort: str) -> str:
    raw = str(sort or "top").strip().lower()
    if raw == "new":
        return "new"
    if raw in {"hot", "top_week"}:
        return raw
    return "top"


def _listing_page_url(subreddit: str, sort: str) -> str:
    name = normalize_subreddit(subreddit)
    kind = _listing_sort_label(sort)
    if kind == "new":
        return f"https://www.reddit.com/r/{name}/new/"
    if kind == "hot":
        return f"https://www.reddit.com/r/{name}/hot/"
    if kind == "top_week":
        return f"https://www.reddit.com/r/{name}/top/?t=week"
    return f"https://www.reddit.com/r/{name}/top/?t=day"


def _listing_json_url(subreddit: str, sort: str) -> str:
    name = normalize_subreddit(subreddit)
    kind = _listing_sort_label(sort)
    if kind == "new":
        return f"https://www.reddit.com/r/{name}/new.json?limit=25&raw_json=1"
    if kind == "hot":
        return f"https://www.reddit.com/r/{name}/hot.json?limit=25&raw_json=1"
    if kind == "top_week":
        return f"https://www.reddit.com/r/{name}/top.json?t=week&limit=25&raw_json=1"
    return f"https://www.reddit.com/r/{name}/top.json?t=day&limit=25&raw_json=1"


def _normalize_post_url(url: str) -> str:
    value = str(url or "").split("?")[0].strip()
    if value.startswith("/r/") and "/comments/" in value:
        value = "https://www.reddit.com" + value
    return value.rstrip("/")


_MEDIA_DOMAINS = (
    "i.redd.it",
    "v.redd.it",
    "preview.redd.it",
    "i.imgur.com",
    "imgur.com",
    "gfycat.com",
    "redgifs.com",
    "youtube.com",
    "youtu.be",
    "streamable.com",
    "vimeo.com",
    "tiktok.com",
    "giphy.com",
)


def _url_is_direct_media(url: str) -> bool:
    lower = str(url or "").lower().split("?")[0].split("#")[0]
    if not lower:
        return False
    if any(
        part in lower
        for part in (
            "/gallery/",
            "i.redd.it",
            "v.redd.it",
            "i.imgur.com",
            "imgur.com",
            "gfycat.com",
            "redgifs.com",
            "giphy.com",
            "youtube.com",
            "youtu.be",
            "streamable.com",
            "vimeo.com",
            "tiktok.com",
        )
    ):
        return True
    return lower.endswith(
        (".jpg", ".jpeg", ".png", ".gif", ".gifv", ".webp", ".mp4", ".webm", ".mov")
    )


def _item_has_reddit_video(item: Dict[str, Any]) -> bool:
    for key in ("media", "secure_media"):
        media = item.get(key)
        if isinstance(media, dict) and media.get("reddit_video"):
            return True
    return False


def _is_media_listing_item(item: Dict[str, Any], *, depth: int = 0) -> bool:
    """True for image / video / gallery posts — skip these for open + comment."""
    if not SKIP_IMAGE_VIDEO_POSTS or not isinstance(item, dict):
        return False
    if item.get("is_video") or item.get("is_gallery") or item.get("media_only"):
        return True
    if item.get("gallery_data") or item.get("media_metadata") and item.get("is_gallery"):
        return True
    if _item_has_reddit_video(item):
        return True
    hint = str(item.get("post_hint") or "").lower()
    if hint in {"image", "hosted:video", "rich:video", "video", "gallery"}:
        return True
    # A text self-post can mention a picture. That is still a text post.
    if item.get("is_self") or item.get("is_self") is True:
        return False
    domain = str(item.get("domain") or "").lower()
    if any(domain == host or domain.endswith("." + host) for host in _MEDIA_DOMAINS):
        return True
    url = str(item.get("url") or item.get("url_overridden_by_dest") or "")
    if _url_is_direct_media(url):
        return True
    if depth < 1:
        parents = item.get("crosspost_parent_list") or []
        if isinstance(parents, list) and parents and isinstance(parents[0], dict):
            if _is_media_listing_item(parents[0], depth=depth + 1):
                return True
    return False


_POST_IS_MEDIA_JS = r"""
const el = arguments[0];
function walkUp(node) {
  let cur = node;
  for (let i = 0; i < 12 && cur; i++) {
    const tag = (cur.tagName || '').toLowerCase();
    if (tag === 'shreddit-post' || tag === 'article' || (cur.getAttribute && cur.getAttribute('data-testid') === 'post-container')) {
      return cur;
    }
    cur = cur.parentElement || (cur.getRootNode && cur.getRootNode().host) || null;
  }
  return null;
}
function attr(node, name) {
  try { return String(node.getAttribute(name) || '').toLowerCase(); } catch (e) { return ''; }
}
const post = walkUp(el) || el;
const type = attr(post, 'post-type') || attr(post, 'posttype') || attr(post, 'content-type') || attr(post, 'view-type');
if (['image', 'video', 'gallery', 'gif', 'media'].includes(type)) return true;
if (attr(post, 'is-video') === 'true' || attr(post, 'is-gallery') === 'true') return true;
const domain = attr(post, 'domain');
const mediaHosts = ['i.redd.it','v.redd.it','i.imgur.com','imgur.com','gfycat.com','redgifs.com','giphy.com','youtube.com','youtu.be','streamable.com','vimeo.com','tiktok.com'];
if (mediaHosts.some(host => domain === host || domain.endsWith('.' + host))) return true;
const content = (attr(post, 'content-href') || '').toLowerCase();
if (content && (
  content.includes('i.redd.it') || content.includes('v.redd.it') || content.includes('/gallery/') ||
  content.includes('imgur.com') || content.includes('youtube.com') || content.includes('youtu.be') ||
  /\.(jpe?g|png|gif|gifv|webp|mp4|webm|mov)(\?|$)/.test(content)
)) return true;
try {
  if (post.querySelector('shreddit-player, gallery-carousel, video')) return true;
} catch (e) {}
const href = ((el.getAttribute && el.getAttribute('href')) || '').toLowerCase();
if (href.includes('/gallery/') || href.includes('v.redd.it') || href.includes('i.redd.it')) return true;
return false;
"""


def _page_is_media_post(driver: WebDriver) -> bool:
    """True when the open thread itself is an image, video, or gallery."""
    if not SKIP_IMAGE_VIDEO_POSTS:
        return False
    try:
        post = driver.execute_script("return document.querySelector('shreddit-post');")
    except Exception:
        post = None
    if post is None:
        return False
    return _element_is_media_post(driver, post)


def _element_is_media_post(driver: WebDriver, element: Any) -> bool:
    if not SKIP_IMAGE_VIDEO_POSTS or element is None:
        return False
    try:
        return bool(driver.execute_script(_POST_IS_MEDIA_JS, element))
    except Exception:
        return False


def _listing_posts_from_json(payload: Any) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    data = payload.get("data") if isinstance(payload, dict) else None
    children = data.get("children") if isinstance(data, dict) else None
    if not isinstance(children, list):
        return rows
    for child in children:
        item = child.get("data") if isinstance(child, dict) else None
        if not isinstance(item, dict):
            continue
        if item.get("stickied") or item.get("pinned"):
            continue
        if item.get("locked") or item.get("archived"):
            continue
        if _is_media_listing_item(item):
            continue
        permalink = str(item.get("permalink") or "")
        url = _normalize_post_url(permalink or str(item.get("url") or ""))
        if "/comments/" not in url:
            continue
        rows.append(
            {
                "url": url,
                "title": str(item.get("title") or "").strip(),
                "body": re.sub(r"\s+", " ", str(item.get("selftext") or "")).strip()[:400],
                "score": int(item.get("score") or 0),
                "num_comments": int(item.get("num_comments") or 0),
                "is_self": bool(item.get("is_self")),
                "post_hint": str(item.get("post_hint") or ""),
            }
        )
    return rows


def fetch_listing_posts(
    driver: WebDriver,
    label: str,
    subreddit: str,
    sort: str,
    *,
    limit: int = 12,
) -> Tuple[List[Dict[str, Any]], str]:
    """Return (posts, sort_used). Prefers Reddit listing JSON, then visible links."""
    name = normalize_subreddit(subreddit)
    wanted = _listing_sort_label(sort)
    attempts = [wanted]
    if wanted == "top":
        attempts = ["top", "top_week", "hot"]
    elif wanted == "new":
        attempts = ["new"]
    posts: List[Dict[str, Any]] = []
    used = wanted
    for attempt in attempts:
        payload = reddit_session_json(driver, _listing_json_url(name, attempt), label)
        posts = _listing_posts_from_json(payload)
        if posts:
            used = attempt
            break
    if not posts:
        used = wanted
        for element in _listing_post_elements(driver, limit=limit):
            try:
                href = _normalize_post_url(element.get_attribute("href") or "")
            except StaleElementReferenceException:
                continue
            if "/comments/" not in href:
                continue
            count = _element_comment_count(driver, element)
            # Keep 0-comment posts — pick_listing_post decides via intent
            posts.append(
                {
                    "url": href,
                    "title": _element_post_title(driver, element),
                    "body": "",
                    "score": 0,
                    "num_comments": max(0, count),
                }
            )
    seen = set()
    clean: List[Dict[str, Any]] = []
    for row in posts:
        key = str(row.get("url") or "").lower()
        if not key or key in seen:
            continue
        seen.add(key)
        clean.append(row)
        if len(clean) >= limit:
            break
    return clean, used


def next_comment_sorts(stats: AccountSummary, want: int) -> List[str]:
    """General comments use the new listing only."""
    want = max(0, int(want))
    return ["new"] * want


def _element_post_title(driver: WebDriver, element: Any) -> str:
    if element is None:
        return ""
    try:
        value = driver.execute_script(
            r"""
            const el = arguments[0];
            let cur = el;
            for (let i = 0; i < 12 && cur; i++) {
              const tag = (cur.tagName || '').toLowerCase();
              if (tag === 'shreddit-post' || tag === 'article') {
                return cur.getAttribute('post-title') || cur.getAttribute('posttitle') || '';
              }
              cur = cur.parentElement || (cur.getRootNode && cur.getRootNode().host) || null;
            }
            return '';
            """,
            element,
        )
        return re.sub(r"\s+", " ", str(value or "")).strip()
    except Exception:
        return ""


def _listing_post_intent(row: Dict[str, Any]) -> str:
    try:
        from reddit_joiner.ai import classify_post_intent
    except Exception:
        return "other"
    try:
        return classify_post_intent(
            str(row.get("title") or ""),
            str(row.get("body") or ""),
        )
    except Exception:
        return "other"


def _listing_replies(row: Dict[str, Any]) -> int:
    try:
        return int(row.get("num_comments") or 0)
    except (TypeError, ValueError):
        return 0


def commentable_intent(intent: str) -> bool:
    """True only for the four post types we comment on.

    Anything classified as "other" is left alone: the post was analysed and it
    is not a question, suggestion, review or help request.
    """
    label = str(intent or "").strip().lower()
    try:
        from reddit_joiner.ai import is_commentable_intent
    except Exception:
        return label in COMMENTABLE_POST_INTENTS
    return is_commentable_intent(label)


def _zero_comment_intent_ok(row: Dict[str, Any], intent: str = "") -> bool:
    """New empty threads are fair game only for help / suggestion / question / review."""
    if not ALLOW_ZERO_COMMENT_INTENTS:
        return False
    return commentable_intent(intent or _listing_post_intent(row))


def pick_listing_post(
    posts: List[Dict[str, Any]],
    user_id: str,
) -> Optional[Dict[str, Any]]:
    """
    Prefer a help, question, suggestion, or review post on /new.
    Image, video, and gallery posts are not candidates.
    Any other kind of post is left alone.
    """
    eligible = []
    for row in posts:
        url = str(row.get("url") or "")
        if already_commented_on(user_id, url):
            continue
        if _is_media_listing_item(row):
            continue
        intent = _listing_post_intent(row)
        if not commentable_intent(intent):
            continue
        replies = _listing_replies(row)
        if REQUIRE_POST_REPLIES and replies < MIN_POST_REPLIES:
            if not _zero_comment_intent_ok(row, intent):
                continue
        eligible.append(row)
    if not eligible:
        return None
    # Prefer empty intent threads first so we actually reply to new asks
    zero_intent = [
        row
        for row in eligible
        if _listing_replies(row) == 0 and _zero_comment_intent_ok(row)
    ]
    pool = (zero_intent or eligible)[:10]
    style = _style()
    bias = style.listing_top_bias if style else 0.7
    weights: List[float] = []
    for index, row in enumerate(pool):
        base = max(0.25, (len(pool) - index) ** (0.45 + bias * 1.1))
        if bias < 0.38:
            base = 0.8 + _rng().random()
        intent = _listing_post_intent(row)
        replies = _listing_replies(row)
        if intent in {"help", "suggestion", "review", "question"}:
            base *= _rng().uniform(1.8, 3.2)
        else:
            base *= _rng().uniform(0.55, 1.05)
        if replies == 0 and _zero_comment_intent_ok(row, intent):
            base *= _rng().uniform(2.0, 3.5)
        if row.get("is_self"):
            base *= 1.15
        if replies >= 3:
            base *= 1.1
        weights.append(base)
    return _rng().choices(pool, weights=weights, k=1)[0]


def _lurk_before_comment(
    driver: WebDriver,
    label: str,
    subreddit: str,
    seconds: float,
) -> None:
    """Browse the community a bit so commenting does not look like hunt → reply."""
    prefix = f"[Profile {label}] "
    dwell = max(6.0, float(seconds))
    url = f"https://www.reddit.com/r/{normalize_subreddit(subreddit)}/"
    try:
        here = (driver.current_url or "").lower()
    except Exception:
        here = ""
    if f"/r/{normalize_subreddit(subreddit).lower()}/" not in here:
        navigate(driver, url, label)
        dismiss_popups(driver)
    log(f"{prefix}Reading the r/{normalize_subreddit(subreddit)} feed for {dwell:.0f}s before any reply")
    end_at = time.time() + dwell
    while time.time() < end_at:
        remaining = end_at - time.time()
        if remaining <= 0.4:
            break
        roll = _rng().random()
        if roll < 0.55:
            human_scroll(driver, direction=1, remaining=min(3.2, remaining))
        elif roll < 0.68:
            human_scroll(driver, direction=-1, remaining=min(1.4, remaining))
        elif roll < 0.82:
            _reading_pause(remaining)
        else:
            _mouse_wander(driver)
            time.sleep(min(_rng().uniform(0.5, 1.4), remaining))


def _peek_random_thread(
    driver: WebDriver,
    label: str,
    subreddit: str,
    recover_url: str,
) -> None:
    """Open a post just to read — no comment — breaks the scroll→target→comment shape."""
    prefix = f"[Profile {label}] "
    post = _random_post_title(driver)
    if post is None:
        return
    log(f"{prefix}Opening a post in r/{normalize_subreddit(subreddit)} just to read")
    if not human_click(driver, post):
        return
    style = _style()
    time.sleep(_rng().uniform(*(style.post_load_wait if style else POST_LOAD_WAIT)))
    dismiss_popups(driver)
    read_opened_thread(
        driver,
        _rng().uniform(*THREAD_READ),
        label,
    )
    leave_opened_page(driver, label, recover_url=recover_url)
    time.sleep(_rng().uniform(1.0, 2.4))


def leave_subreddit_comments(
    driver: WebDriver,
    label: str,
    user_id: str,
    stats: AccountSummary,
    subreddit: str,
    want: int,
    sorts: Optional[List[str]] = None,
) -> int:
    """
    Leave general comments after natural browse time.
    Soft-prefers help / suggestion / review posts; comment text matches that post.
    """
    prefix = f"[Profile {label}] "
    want = max(0, int(want))
    if want <= 0:
        return 0
    name = normalize_subreddit(subreddit)
    if is_blocked_subreddit(name):
        log(f"{prefix}No comments in r/{name} — on the blocked list")
        return 0
    sheet = {item.lower() for item in allowed_subreddits()}
    if name.lower() not in sheet:
        log(f"{prefix}No general comments in r/{name} — general activity comments stay on subreddits.csv")
        return 0
    planned = list(sorts or next_comment_sorts(stats, want))[:want]
    posted = 0
    feed = f"https://www.reddit.com/r/{name}/"
    try:
        from reddit_joiner.ai import classify_post_intent
    except Exception:
        classify_post_intent = None  # type: ignore[assignment]

    try:
        raise_profile_browser(label)
        # Arrive like a person: short lurk first, sometimes peek a thread with no reply
        _lurk_before_comment(driver, label, name, _rng().uniform(*LURK_BEFORE_COMMENT))
        if _rng().random() < PEEK_THREAD_CHANCE:
            _peek_random_thread(driver, label, name, feed)
        if _rng().random() < 0.18:
            _lurk_before_comment(driver, label, name, _rng().uniform(5.0, 12.0))
            if _rng().random() < 0.35:
                _peek_random_thread(driver, label, name, feed)

        for _ in planned:
            if posted >= want:
                break
            # Mix how we reach candidates — not always /new first
            listing = _rng().choice(
                (
                    f"https://www.reddit.com/r/{name}/new/",
                    f"https://www.reddit.com/r/{name}/",
                    f"https://www.reddit.com/r/{name}/new/",
                )
            )
            navigate(driver, listing, label)
            dismiss_popups(driver)
            human_wait(driver, _rng().uniform(3.0, 8.0), label, listing)
            try:
                human_scroll(driver, 1, remaining=_rng().uniform(2.0, 5.0))
            except Exception:
                pass
            posts, used = fetch_listing_posts(driver, label, name, "new")
            if _listing_sort_label(used) != "new" and "/new" in listing:
                log(f"{prefix}r/{name} new listing was empty — skipping this try")
                continue
            if not posts:
                posts, used = fetch_listing_posts(driver, label, name, "new")
            picked = pick_listing_post(posts, user_id)
            if picked is None:
                log(
                    f"{prefix}No help, question, suggestion, or review post left in r/{name} "
                    "(image and video posts are skipped)"
                )
                continue
            title = str(picked.get("title") or "").strip()
            body_snip = str(picked.get("body") or "")
            replies = _listing_replies(picked)
            intent = "other"
            if classify_post_intent is not None:
                try:
                    intent = classify_post_intent(title, body_snip)
                except Exception:
                    intent = "other"
            # 0-comment new posts: only help / suggestion / question / review
            if replies == 0:
                ok_zero = _zero_comment_intent_ok(picked, intent)
                if not ok_zero:
                    log(
                        f"{prefix}Skipped 0-comment post in r/{name} — "
                        "not help/suggestion/question/review"
                    )
                    continue
                log(
                    f"{prefix}New 0-comment {intent} post in r/{name} — will reply"
                )
            elif not commentable_intent(intent):
                log(
                    f"{prefix}Read a title in r/{name} and moved on — "
                    f"{intent} post, not help/suggestion/question/review"
                )
                continue
            if not wait_before_next_comment(driver, label, user_id, listing):
                continue
            log(
                f"{prefix}Opening a post in r/{name}"
                + (f": {title[:80]}" if title else "")
                + f" ({posted + 1}/{want})"
                + (f" [{intent}, {replies} replies]" if title else "")
            )
            navigate(driver, str(picked["url"]), label)
            style = _style()
            time.sleep(_rng().uniform(*(style.post_load_wait if style else POST_LOAD_WAIT)))
            dismiss_popups(driver)
            # Read first — comment only after looking like a person
            read_opened_thread(
                driver,
                _rng().uniform(*(style.thread_read if style else THREAD_READ)),
                label,
            )
            if maybe_ai_comment_on_opened_post(
                driver, label, user_id, stats, kind="subreddit", force=True
            ):
                posted += 1
                stats.comment_sorts.append("new")
                linger = _rng().uniform(*(style.after_comment if style else AFTER_COMMENT_LINGER))
                log(f"{prefix}Staying on the thread {linger:.0f}s after commenting")
                human_wait(driver, linger, label)
                leave_opened_page(driver, label, recover_url=feed)
                if posted < want:
                    pause = _rng().uniform(*(style.between_comments if style else BETWEEN_COMMENTS))
                    log(f"{prefix}Waiting {pause:.0f}s before more browsing in r/{name}")
                    _lurk_before_comment(driver, label, name, min(pause, 28.0))
            else:
                leave_opened_page(driver, label, recover_url=feed)
            time.sleep(_rng().uniform(1.2, 2.8))
    except Exception as exc:
        log(f"{prefix}Comments in r/{name} failed ({brief_error(exc)})")
    log(f"{prefix}Left {posted} comment(s) in r/{name}")
    return posted


def _random_link(driver: WebDriver) -> Optional[Any]:
    links = _visible_elements(driver, By.TAG_NAME, "a", limit=25)
    usable = []
    for link in links:
        try:
            href = (link.get_attribute("href") or "").lower()
            if href and "reddit.com" in href and "login" not in href:
                usable.append(link)
        except StaleElementReferenceException:
            continue
    return _rng().choice(usable) if usable else None


def _element_point(driver: WebDriver, element: Any) -> Optional[Tuple[float, float]]:
    """A random spot inside the element's box — people do not click dead centre."""
    try:
        box = driver.execute_script(
            """
            const r = arguments[0].getBoundingClientRect();
            if (!r || r.width < 4 || r.height < 4) return null;
            return [r.left, r.top, r.width, r.height];
            """,
            element,
        )
    except Exception:
        return None
    if not box:
        return None
    left, top, width, height = (float(v) for v in box)
    return (
        left + width * _rng().uniform(0.25, 0.75),
        top + height * _rng().uniform(0.25, 0.75),
    )


def _hover(driver: WebDriver, element: Any) -> None:
    """Glide the cursor onto the element and dwell like someone considering it."""
    point = _element_point(driver, element)
    if point is not None:
        move_cursor_to(driver, point[0], point[1], remaining=1.5)
        human_sleep(human.gaussian_between(0.6, 2.2))
        return
    try:
        ActionChains(driver).move_to_element(element).pause(_rng().uniform(0.25, 0.8)).perform()
        return
    except Exception:
        pass
    try:
        driver.execute_script(
            """
            const el = arguments[0];
            el.dispatchEvent(new MouseEvent('mouseover', {bubbles: true}));
            el.dispatchEvent(new MouseEvent('mousemove', {bubbles: true}));
            """,
            element,
        )
    except Exception:
        pass


_last_upvote_at: Dict[str, float] = {}


def set_session_deadline(when: Optional[float]) -> None:
    """Hard stop for this account's sitting. Pauses never sleep past it."""
    _tls.deadline = when


def deadline_remaining() -> Optional[float]:
    when = getattr(_tls, "deadline", None)
    if when is None:
        return None
    return float(when) - time.time()


def past_deadline() -> bool:
    left = deadline_remaining()
    return left is not None and left <= 0.0


def human_sleep(seconds: float, remaining: Optional[float] = None) -> float:
    """
    Sleep, clamped by both the caller's own budget and the sitting deadline, so
    one long pause can never push a 5–10 min sitting over its time.
    """
    nap = max(0.0, float(seconds))
    if remaining is not None:
        nap = min(nap, max(0.0, float(remaining)))
    left = deadline_remaining()
    if left is not None:
        nap = min(nap, max(0.0, left))
    if nap > 0:
        time.sleep(nap)
    return nap


def _viewport(driver: WebDriver) -> Tuple[int, int]:
    cached = getattr(_tls, "viewport", None)
    if cached:
        return cached
    size = (1280, 800)
    try:
        got = driver.execute_script(
            "return [window.innerWidth || 0, window.innerHeight || 0];"
        )
        if got and int(got[0]) > 200 and int(got[1]) > 200:
            size = (int(got[0]), int(got[1]))
    except Exception:
        pass
    _tls.viewport = size
    return size


def cursor_pos(driver: WebDriver) -> Tuple[int, int]:
    """Where we last left the pointer — scrolling happens under it."""
    pos = getattr(_tls, "cursor", None)
    if pos:
        return pos
    width, height = _viewport(driver)
    pos = (width // 2, height // 2)
    _tls.cursor = pos
    return pos


def _dispatch_mouse_move(driver: WebDriver, x: float, y: float) -> bool:
    try:
        driver.execute_cdp_cmd(
            "Input.dispatchMouseEvent",
            {"type": "mouseMoved", "x": int(x), "y": int(y), "button": "none"},
        )
        return True
    except Exception:
        return False


def move_cursor_to(driver: WebDriver, x: float, y: float, remaining: float = 3.0) -> None:
    """
    Glide the pointer along a jittered bezier curve instead of teleporting.
    Keeps the tracked position in sync so wheel gestures land under the cursor.
    """
    width, height = _viewport(driver)
    target_x = max(8, min(width - 8, float(x)))
    target_y = max(8, min(height - 8, float(y)))
    start = cursor_pos(driver)
    steps = _rng().randint(14, 28)
    for point in human.bezier_path(start, (target_x, target_y), steps):
        if remaining <= 0.05:
            break
        if not _dispatch_mouse_move(driver, point[0], point[1]):
            break
        remaining -= human_sleep(human.gaussian_between(0.008, 0.025), remaining)
    _tls.cursor = (int(target_x), int(target_y))


_SCROLL_OFFSET_JS = (
    "return (window.scrollY || window.pageYOffset || "
    "(document.scrollingElement && document.scrollingElement.scrollTop) || "
    "(document.body && document.body.scrollTop) || 0);"
)


def scroll_offset(driver: WebDriver) -> float:
    """Current scroll position, or -1 when it cannot be read."""
    try:
        return float(driver.execute_script(_SCROLL_OFFSET_JS) or 0.0)
    except Exception:
        return -1.0


def _js_scroll(driver: WebDriver, amount: int) -> None:
    try:
        driver.execute_script("window.scrollBy(0, arguments[0]);", int(amount))
    except Exception:
        pass


_SMOOTH_SCROLL_JS = r"""
const distance = arguments[0];
const duration = Math.max(140, arguments[1]);
const done = arguments[arguments.length - 1];
const start = window.scrollY || document.documentElement.scrollTop || 0;
const t0 = performance.now();
function ease(p) {
  return 0.5 - 0.5 * Math.cos(Math.PI * Math.max(0, Math.min(1, p)));
}
let prev = 0;
function frame(now) {
  const p = Math.min(1, (now - t0) / duration);
  const next = distance * ease(p);
  const delta = next - prev;
  prev = next;
  if (Math.abs(delta) >= 0.4) window.scrollBy(0, delta);
  if (p < 1) requestAnimationFrame(frame);
  else done((window.scrollY || 0) - start);
}
requestAnimationFrame(frame);
"""


def _smooth_js_scroll(driver: WebDriver, amount: int) -> None:
    """Ease one gesture with scrollBy so a dead wheel still moves smoothly."""
    distance = int(amount)
    if distance == 0:
        return
    pixels = abs(distance)
    speed = _rng().uniform(480.0, 1500.0)
    duration = int(max(180, min(980, pixels / speed * 1000.0)))
    moved = 0.0
    try:
        driver.set_script_timeout(3)
        moved = float(driver.execute_async_script(_SMOOTH_SCROLL_JS, distance, duration) or 0)
    except Exception:
        _js_scroll(driver, distance)
        moved = float(distance)
    _note_scrolled(moved if abs(moved) >= 1 else distance)


def _note_scrolled(pixels: float) -> None:
    try:
        _tls.scrolled_px = float(getattr(_tls, "scrolled_px", 0.0)) + abs(float(pixels))
    except Exception:
        return


def session_scrolled_px() -> float:
    return float(getattr(_tls, "scrolled_px", 0.0) or 0.0)


def _wheel_is_live() -> Optional[bool]:
    return getattr(_tls, "wheel_ok", None)


def _scroll_tick(driver: WebDriver, delta: int) -> None:
    """
    One small wheel tick at the cursor.

    A synthesized wheel event is only delivered if the browser accepts it. An
    unfocused window can drop it, and execute_cdp_cmd still returns success, so
    the old version could scroll nothing at all and report no error. The page
    position is measured until the wheel has proven it works; after a few ticks
    that moved nothing, this switches to JS scrolling for the rest of the run.
    """
    amount = int(delta)
    if amount == 0:
        return
    if _wheel_is_live() is False:
        before = scroll_offset(driver)
        _js_scroll(driver, amount)
        after = scroll_offset(driver)
        _note_scrolled(after - before if before >= 0 and after >= 0 else amount)
        return
    x, y = cursor_pos(driver)
    unproven = _wheel_is_live() is None
    before = scroll_offset(driver) if unproven else -1.0
    try:
        driver.execute_cdp_cmd(
            "Input.dispatchMouseEvent",
            {
                "type": "mouseWheel",
                "x": x,
                "y": y,
                "deltaX": 0,
                "deltaY": amount,
            },
        )
    except Exception:
        _tls.wheel_ok = False
        log("Wheel events are not accepted by this browser — scrolling with JS instead")
        _js_scroll(driver, amount)
        _note_scrolled(amount)
        return
    if not unproven or before < 0:
        _note_scrolled(amount)
        return
    moved = scroll_offset(driver) - before
    if abs(moved) >= 1.0:
        _tls.wheel_ok = True
        _note_scrolled(moved)
        return
    # Nothing moved. Could be the end of the feed, so only give up after a few.
    misses = int(getattr(_tls, "wheel_misses", 0)) + 1
    _tls.wheel_misses = misses
    if misses >= 3:
        _tls.wheel_ok = False
        log("Wheel events moved the page 0px three times — scrolling with JS instead")
    _js_scroll(driver, amount)
    after = scroll_offset(driver)
    _note_scrolled(after - before if after >= 0 else 0.0)


def _momentum_scroll(
    driver: WebDriver,
    distance: int,
    remaining: float,
) -> Optional[float]:
    """
    Chromium's own animated scroll gesture (Input.synthesizeScrollGesture).
    The compositor runs the whole fling, so it is genuinely smooth instead of
    a train of wheel events sent one at a time over the wire.

    Returns the seconds it consumed, or None when the gesture is unavailable.
    """
    dist = int(distance)
    if dist == 0:
        return None
    speed = human.scroll_speed_px_per_sec()
    expected = abs(dist) / float(max(200, speed))
    if expected > max(0.2, remaining):
        return None
    if getattr(_tls, "gesture_ok", None) is False:
        return None
    x, y = cursor_pos(driver)
    # A minority of gestures are an inertial trackpad/touch fling
    touch = _rng().random() < 0.15
    started = time.time()
    before = scroll_offset(driver)
    try:
        driver.execute_cdp_cmd(
            "Input.synthesizeScrollGesture",
            {
                "x": int(x),
                "y": int(y),
                "xDistance": 0,
                # Negative yDistance scrolls the page down
                "yDistance": -dist,
                "speed": speed,
                "gestureSourceType": "touch" if touch else "mouse",
            },
        )
    except Exception:
        _tls.gesture_ok = False
        log("Momentum scroll gesture unavailable — using stepped wheel scrolling")
        return None
    # The command can succeed while the compositor scrolls nothing. Treat a
    # gesture that moved the page 0px as unavailable so the caller falls back.
    after = scroll_offset(driver)
    if before >= 0 and after >= 0:
        moved = abs(after - before)
        if moved < 1.0:
            misses = int(getattr(_tls, "gesture_misses", 0)) + 1
            _tls.gesture_misses = misses
            if misses >= 3:
                _tls.gesture_ok = False
                log(
                    "Momentum gestures moved the page 0px three times — "
                    "using stepped wheel scrolling"
                )
            return None
        _tls.gesture_ok = True
        _note_scrolled(moved)
    else:
        _note_scrolled(abs(dist))
    return time.time() - started


def _stepped_scroll(
    driver: WebDriver,
    direction: int,
    target_px: int,
    remaining: float,
) -> Tuple[float, float]:
    """
    Stepped wheel scroll: many small ticks ramped accelerate → cruise →
    decelerate, at one steady cadence so it does not read as jerky.
    Returns (pixels travelled, seconds left).
    """
    style = _style()
    tick_px = style.scroll_tick_px if style else SCROLL_TICK_PX
    tick_delay = style.scroll_tick_delay if style else SCROLL_TICK_DELAY
    traveled = 0
    for step in human.scroll_step_sizes(target_px, tick_px):
        if remaining <= 0.12:
            break
        _scroll_tick(driver, direction * step)
        traveled += step
        gap = human.gaussian_between(*tick_delay) * _rng().uniform(0.55, 1.6)
        remaining -= human_sleep(gap, remaining)
    return float(traveled), remaining


def _scroll_settle(
    driver: WebDriver,
    direction: int,
    traveled: float,
    remaining: float,
) -> float:
    """Light rebound after a gesture, like a trackpad easing to a stop."""
    if remaining < 0.2 or traveled < 60 or _rng().random() > SCROLL_BOUNCE_CHANCE:
        return remaining
    bounce_px = max(10, int(traveled * _rng().uniform(*SCROLL_BOUNCE_FRAC)))
    ticks = max(1, _rng().randint(*SCROLL_BOUNCE_TICKS))
    remaining -= human_sleep(_rng().uniform(0.05, 0.12), remaining)
    per = max(6, bounce_px // ticks)
    for _ in range(ticks):
        if remaining < 0.12 or bounce_px <= 0:
            break
        tick = min(per, bounce_px)
        _scroll_tick(driver, -direction * tick)
        bounce_px -= tick
        remaining -= human_sleep(_rng().uniform(0.07, 0.13), remaining)
    return remaining


def _scroll_gesture(
    driver: WebDriver,
    direction: int,
    target_px: int,
    remaining: float,
) -> None:
    """
    One scroll gesture. Two engines, picked per call with a per-session bias,
    so the scroll signature is not identical on every run:

      momentum - Chromium animates the fling itself (smoothest)
      stepped  - small wheel ticks ramped accel → cruise → decel
    """
    if remaining < 0.2 or target_px <= 0:
        return
    # Wheel input is ignored in this browser. One eased gesture, not a train
    # of instant jumps at a fixed gap.
    if _wheel_is_live() is False:
        jitter = _rng().uniform(0.62, 1.55)
        distance = int(direction * max(12, abs(int(target_px)) * jitter))
        _smooth_js_scroll(driver, distance)
        if abs(distance) > 90 and _rng().random() < SCROLL_BOUNCE_CHANCE * 0.7:
            human_sleep(_rng().uniform(0.06, 0.22), remaining)
            bounce = max(8, int(abs(distance) * _rng().uniform(0.03, 0.09)))
            _smooth_js_scroll(driver, -direction * bounce)
        human_sleep(_rng().uniform(*SCROLL_EASE_SETTLE), remaining)
        return
    style = _style()
    bias = style.smooth_scroll_bias if style else 0.6
    used: Optional[float] = None
    if _rng().random() < bias:
        used = _momentum_scroll(driver, direction * target_px, remaining)
    if used is None:
        traveled, remaining = _stepped_scroll(driver, direction, target_px, remaining)
    else:
        traveled = float(target_px)
        remaining -= used
    remaining = _scroll_settle(driver, direction, traveled, remaining)
    human_sleep(_rng().uniform(*SCROLL_EASE_SETTLE), remaining)


def scroll_by_range(
    driver: WebDriver,
    pixel_range: Tuple[int, int],
    direction: int = 1,
    remaining: float = 8.0,
) -> None:
    """Scroll about pixel_range pixels as one smooth gesture."""
    target = human.gaussian_int(int(pixel_range[0]), int(pixel_range[1]))
    _scroll_gesture(driver, direction, max(1, target), remaining)


def human_scroll(driver: WebDriver, direction: int = 1, remaining: float = 8.0) -> None:
    """One smooth human scroll gesture in the given direction."""
    if remaining < 0.25:
        return
    style = _style()
    span = (
        (style.scroll_down_px if style else SCROLL_DOWN_PX)
        if direction >= 0
        else (style.scroll_up_px if style else SCROLL_UP_PX)
    )
    target = human.gaussian_int(int(span[0]), int(span[1]))
    _scroll_gesture(driver, direction, max(1, target), remaining)


def _scroll_flicks(
    driver: WebDriver,
    pixel_range: Tuple[int, int],
    direction: int,
    remaining: float,
) -> None:
    """One scroll, or two or three flicks with a short gap, then a read."""
    flicks = 1 if _rng().random() < 0.58 else _rng().choice((2, 3))
    for index in range(flicks):
        if remaining < 0.3:
            return
        scale = _rng().uniform(0.55, 1.7)
        lo = max(16, int(pixel_range[0] * scale))
        hi = max(lo + 8, int(pixel_range[1] * scale))
        scroll_by_range(driver, (lo, hi), direction=direction, remaining=remaining)
        if index + 1 < flicks:
            human_sleep(_rng().uniform(0.05, 0.32), remaining)


def _reading_pause(remaining: float) -> None:
    """
    Sit on the feed like someone reading. Heavy tailed on purpose: mostly quick
    skims between flicks, some real reading, and the odd long dwell — a single
    narrow range for every gap is a mechanical tell.
    """
    if remaining <= 0.3:
        return
    style = _style()
    human_sleep(
        human.heavy_tailed_pause(
            style.read_pause if style else READ_PAUSE,
            style.long_read_pause if style else LONG_READ_PAUSE,
        ),
        remaining,
    )


_last_human_extra: Dict[str, float] = {}


def _mouse_wander(driver: WebDriver) -> None:
    """Idle cursor drift along short bezier arcs — never a frozen mouse."""
    width, height = _viewport(driver)
    x, y = cursor_pos(driver)
    for _ in range(_rng().randint(1, 3)):
        target_x = max(10, min(width - 10, x + _rng().randint(-60, 60)))
        target_y = max(10, min(height - 10, y + _rng().randint(-45, 45)))
        move_cursor_to(driver, target_x, target_y, remaining=1.2)
        x, y = target_x, target_y
        human_sleep(human.gaussian_between(0.3, 1.2))


def human_wait(
    driver: WebDriver,
    seconds: float,
    label: str = "",
    stay_url: str = "",
) -> None:
    """Pass time on the current page: mostly still, some light scrolling."""
    end_at = time.time() + max(0.0, float(seconds))
    while time.time() < end_at:
        remaining = end_at - time.time()
        if remaining <= 0:
            break
        if stay_url and not current_is_reddit(driver):
            try:
                navigate(driver, stay_url, label or "browser")
            except Exception:
                return
        roll = _rng().random()
        if roll < 0.38:
            human_scroll(driver, direction=1, remaining=min(2.4, remaining))
        elif roll < 0.52:
            human_scroll(driver, direction=-1, remaining=min(1.6, remaining))
        elif roll < 0.62:
            _mouse_wander(driver)
        else:
            time.sleep(min(_rng().uniform(5.0, 14.0), remaining))


_EXPAND_COMMENTS_JS = r"""
function walk(root, fn) {
  fn(root);
  let nodes;
  try { nodes = root.querySelectorAll('*'); } catch (e) { return; }
  nodes.forEach(el => { if (el.shadowRoot) walk(el.shadowRoot, fn); });
}
function visible(el) {
  try {
    const r = el.getBoundingClientRect();
    const st = window.getComputedStyle(el);
    return r.width > 8 && r.height > 8 && st.visibility !== 'hidden' && st.display !== 'none';
  } catch (e) { return false; }
}
const keys = ['more repl', 'more comment', 'continue this thread', 'view more', 'see more', 'view all comments', 'comments'];
const hits = [];
walk(document, root => {
  try { root.querySelectorAll('button, a, [role="button"]').forEach(el => hits.push(el)); } catch (e) {}
});
for (const el of hits) {
  if (!visible(el)) continue;
  const t = ((el.innerText || el.textContent || '') + ' ' + (el.getAttribute('aria-label') || '')).toLowerCase();
  if (t.includes('search') || t.includes('share') || t.includes('award') || t.includes('sort')) continue;
  if (keys.some(k => t.includes(k))) {
    try { el.scrollIntoView({block:'center'}); el.click(); return 'expanded'; } catch (e) {}
  }
}
return 'none';
"""


_SCROLL_TO_COMMENTS_JS = r"""
function walk(root, fn) {
  fn(root);
  let nodes;
  try { nodes = root.querySelectorAll('*'); } catch (e) { return; }
  nodes.forEach(el => { if (el.shadowRoot) walk(el.shadowRoot, fn); });
}
const hits = [];
walk(document, root => {
  try {
    root.querySelectorAll(
      'shreddit-comment, shreddit-comment-tree, [id*="comment"], [data-testid*="comment"], .Comment, .commentarea'
    ).forEach(el => hits.push(el));
  } catch (e) {}
});
for (const el of hits) {
  try {
    el.scrollIntoView({block: 'center', inline: 'nearest'});
    return 'scrolled';
  } catch (e) {}
}
try {
  window.scrollBy(0, Math.min(900, Math.floor(window.innerHeight * 0.7)));
  return 'paged';
} catch (e) {}
return 'none';
"""


def _visible_text_length(driver: WebDriver) -> int:
    """Roughly how much text is on screen, to size the reading pause."""
    try:
        value = driver.execute_script(
            """
            const post = document.querySelector('shreddit-post, [data-test-id="post-content"]');
            const src = post || document.body;
            return ((src.innerText || '').trim()).length;
            """
        )
        return int(value or 0)
    except Exception:
        return 0


def read_opened_thread(
    driver: WebDriver,
    seconds: float,
    label: str = "",
) -> None:
    """
    Open the replies area and glance. A wall of text gets a longer look than a
    one-liner, but the glance is still capped so we never lurk the whole tree.
    """
    prefix = f"[Profile {label}] " if label else ""
    window = _style().thread_read if _style() else THREAD_READ
    asked = float(seconds) if seconds else human.gaussian_between(*window)
    scaled = human.reading_seconds(
        _visible_text_length(driver),
        base_range=(window[0], window[1]),
        per_char=0.004,
        extra_cap=4.0,
    )
    dwell = max(window[0], min(max(asked, scaled), window[1] + 4.0))
    left = deadline_remaining()
    if left is not None:
        dwell = min(dwell, max(1.0, left))
    log(f"{prefix}Opening replies — {dwell:.0f}s glance")
    try:
        driver.execute_script(_SCROLL_TO_COMMENTS_JS)
    except Exception:
        pass
    human_sleep(_rng().uniform(0.35, 0.7))
    try:
        driver.execute_script(_EXPAND_COMMENTS_JS)
    except Exception:
        pass
    end_at = time.time() + dwell
    remaining = end_at - time.time()
    if remaining > 1.2:
        human_scroll(driver, direction=1, remaining=min(2.2, remaining - 0.8))
    while time.time() < end_at:
        leftover = end_at - time.time()
        if leftover <= 0.15:
            break
        if leftover > 1.5 and _rng().random() < 0.35:
            human_scroll(driver, direction=1, remaining=min(1.2, leftover))
        else:
            human_sleep(human.heavy_tailed_pause((0.4, 1.4)), leftover)
    log(f"{prefix}Done with replies")


def maybe_human_side_trip(
    driver: WebDriver,
    label: str,
    stats: AccountSummary,
    remaining: float,
    stay_url: str = "",
) -> None:
    """Sometimes check your own profile. Never leave the sheet communities for Popular/All."""
    if remaining < 25:
        return
    key = label or "browser"
    style = _style()
    gap = style.profile_gap if style else 80.0
    visit_chance = style.profile_visit_chance if style else PROFILE_VISIT_CHANCE
    if time.time() - _last_human_extra.get(key, 0.0) < gap:
        return
    if _rng().random() >= visit_chance or not stats.reddit_username:
        return
    dest = f"https://www.reddit.com/user/{stats.reddit_username}/"
    log(f"[Profile {label}] Checking own profile (normal account check)")
    _last_human_extra[key] = time.time()
    try:
        navigate(driver, dest, label)
        dismiss_popups(driver)
        human_wait(driver, min(_rng().uniform(18.0, 40.0), remaining - 20.0), label, dest)
        back_to = stay_url or REDDIT_HOME_URL
        if back_to.rstrip("/").lower() in {
            REDDIT_HOME_URL.rstrip("/").lower(),
            "https://www.reddit.com",
            "https://reddit.com",
        }:
            return_to_reddit_home(driver, label, reason="after profile check")
        else:
            navigate(driver, back_to, label)
        time.sleep(_rng().uniform(1.2, 2.4))
    except Exception:
        try:
            if stay_url:
                navigate(driver, stay_url, label)
            else:
                return_to_reddit_home(driver, label, reason="profile check recover")
        except Exception:
            pass


_SEARCH_FOCUS_JS = r"""
function walk(root, fn) {
  fn(root);
  let nodes;
  try { nodes = root.querySelectorAll('*'); } catch (e) { return; }
  nodes.forEach(el => { if (el.shadowRoot) walk(el.shadowRoot, fn); });
}
function visible(el) {
  try {
    const r = el.getBoundingClientRect();
    const st = window.getComputedStyle(el);
    return r.width > 8 && r.height > 8 && st.visibility !== 'hidden' && st.display !== 'none';
  } catch (e) { return false; }
}
function hint(el) {
  return ((el.getAttribute('placeholder') || '') + ' ' + (el.getAttribute('aria-label') || '') + ' ' +
          (el.getAttribute('name') || '') + ' ' + (el.id || '')).toLowerCase();
}
const boxes = [];
walk(document, root => {
  try {
    root.querySelectorAll('input[type="search"], input[name="q"], input#search-input, input').forEach(el => boxes.push(el));
  } catch (e) {}
});
for (const el of boxes) {
  if (!visible(el)) continue;
  if (!hint(el).includes('search')) continue;
  try {
    el.scrollIntoView({block:'center'});
    el.click();
    el.focus();
    return 'focused';
  } catch (e) {}
}
return 'no-box';
"""

_SEARCH_SORT_NEW_JS = r"""
function walk(root, fn) {
  fn(root);
  let nodes;
  try { nodes = root.querySelectorAll('*'); } catch (e) { return; }
  nodes.forEach(el => { if (el.shadowRoot) walk(el.shadowRoot, fn); });
}
function visible(el) {
  try {
    const r = el.getBoundingClientRect();
    const st = window.getComputedStyle(el);
    return r.width > 6 && r.height > 6 && st.visibility !== 'hidden' && st.display !== 'none';
  } catch (e) { return false; }
}
function txt(el) {
  return ((el.innerText || el.textContent || '') + ' ' + (el.getAttribute('aria-label') || ''))
    .replace(/\s+/g, ' ').trim().toLowerCase();
}
const items = [];
walk(document, root => {
  try {
    root.querySelectorAll('button, a, [role="tab"], [role="menuitem"], [role="radio"], faceplate-chip, li')
      .forEach(el => items.push(el));
  } catch (e) {}
});
for (const el of items) {
  if (!visible(el)) continue;
  const t = txt(el);
  if (t === 'new' || t === 'sort by: new' || t === 'newest') {
    try { el.scrollIntoView({block:'center'}); el.click(); return 'clicked'; } catch (e) {}
  }
}
return 'no-tab';
"""

_SEARCH_FALLBACK_QUERIES = (
    "best budget headphones",
    "how to fix a slow laptop",
    "weekend trip ideas",
    "cheap meal prep ideas",
    "is it worth upgrading",
    "beginner running tips",
    "how do you stay motivated",
    "what should i watch next",
    "small flat storage ideas",
    "first car advice",
)


def _search_query_pool(stats: Optional[AccountSummary]) -> List[str]:
    """Queries that look like this account's own interests, not random noise."""
    queries: List[str] = []
    topics: Dict[str, Any] = getattr(stats, "subreddit_topics", {}) or {}
    for titles in topics.values():
        for title in titles or []:
            words = re.findall(r"[A-Za-z][A-Za-z'-]{2,}", str(title or ""))
            if len(words) < 2:
                continue
            take = words[: _rng().randint(2, 4)]
            phrase = " ".join(word.lower() for word in take)
            if 6 <= len(phrase) <= 60:
                queries.append(phrase)
    _rng().shuffle(queries)
    queries.extend(_rng().sample(_SEARCH_FALLBACK_QUERIES, 4))
    seen = set()
    out: List[str] = []
    for phrase in queries:
        if phrase in seen:
            continue
        seen.add(phrase)
        out.append(phrase)
    return out


def _search_results_url(query: str) -> str:
    return "https://www.reddit.com/search/?q=" + quote_plus(query) + "&sort=new&t=all"


def search_posts_on_new(
    driver: WebDriver,
    label: str,
    stats: Optional[AccountSummary],
    query: str,
    remaining: float,
) -> float:
    """
    Search from the Home search bar, then read the results on the New tab.
    Returns the seconds spent, so the caller can bill it to Home time.
    """
    prefix = f"[Profile {label}] "
    started = time.time()
    typed = False
    try:
        if driver.execute_script(_SEARCH_FOCUS_JS) == "focused":
            time.sleep(_rng().uniform(0.4, 1.1))
            _type_into_focused(driver, query)
            time.sleep(_rng().uniform(0.5, 1.4))
            try:
                ActionChains(driver).send_keys(Keys.ENTER).perform()
                typed = True
            except Exception:
                typed = False
    except Exception as exc:
        log(f"{prefix}Search box not usable ({brief_error(exc)}) — opening results directly")

    if typed:
        log(f'{prefix}Searched "{query}" from the Home search bar')
        time.sleep(_rng().uniform(1.6, 3.0))
        dismiss_popups(driver)
        sorted_new = False
        try:
            sorted_new = driver.execute_script(_SEARCH_SORT_NEW_JS) == "clicked"
        except Exception:
            sorted_new = False
        if sorted_new:
            log(f"{prefix}Switched the search results to the New tab")
            time.sleep(_rng().uniform(1.4, 2.6))
        else:
            navigate(driver, _search_results_url(query), label)
            log(f"{prefix}Opened the New tab for these search results")
    else:
        navigate(driver, _search_results_url(query), label)
        log(f'{prefix}Searched "{query}" and opened the New tab')

    dismiss_popups(driver)
    time.sleep(_rng().uniform(1.2, 2.4))
    results_url = driver.current_url or _search_results_url(query)
    spent = time.time() - started
    dwell = min(_rng().uniform(*SEARCH_RESULT_DWELL), max(8.0, remaining - spent - 10.0))
    log(f"{prefix}Reading {dwell:.0f}s of newest results for \"{query}\"")
    perform_browse_activity(
        driver,
        dwell,
        label,
        user_id=getattr(stats, "user_id", "") or "",
        stats=stats,
        stay_url=results_url,
    )
    return time.time() - started


def maybe_search_posts_on_new(
    driver: WebDriver,
    label: str,
    stats: Optional[AccountSummary],
    remaining: float,
    how_many: int = 1,
) -> float:
    """Search-and-read-New stretch. `how_many` is this sitting's roll. Returns Home seconds spent."""
    if int(how_many) <= 0:
        return 0.0
    pool = _search_query_pool(stats)
    if not pool:
        return 0.0
    spent = 0.0
    for query in pool[: max(0, int(how_many))]:
        left = remaining - spent
        if left < SEARCH_MIN_REMAINING:
            break
        try:
            spent += search_posts_on_new(driver, label, stats, query, left)
        except Exception as exc:
            log(f"[Profile {label}] Search for \"{query}\" failed ({brief_error(exc)})")
            try:
                return_to_reddit_home(driver, label, reason="search recover")
            except Exception:
                pass
            break
        if stats is not None:
            stats.searches += 1
            stats.search_queries.append(query)
        time.sleep(_rng().uniform(1.0, 2.5))
    if spent > 0:
        try:
            return_to_reddit_home(driver, label, reason="after searching")
        except Exception:
            pass
    return spent


def wait_before_next_comment(
    driver: WebDriver,
    label: str,
    user_id: str,
    stay_url: str = "",
) -> bool:
    """Lurk until enough time has passed since the last comment."""
    try:
        from reddit_joiner.store import seconds_since_last_ai_comment
    except ImportError:
        return True
    elapsed = seconds_since_last_ai_comment(user_id or label)
    style = _style()
    gap = style.between_comments if style else BETWEEN_COMMENTS
    need = max(float(MIN_DELAY_BETWEEN_AI_COMMENTS), _rng().uniform(*gap) * 0.45)
    if elapsed is None or elapsed >= need:
        return True
    wait = need - elapsed
    if wait > 240:
        log(f"[Profile {label}] Too soon for another comment — skipping this post")
        return False
    log(f"[Profile {label}] Waiting {wait:.0f}s (human gap) before another comment")
    human_wait(driver, wait, label, stay_url)
    return True


def _still_on_reddit(driver: WebDriver) -> bool:
    try:
        host = urlparse(driver.current_url or "").netloc.lower()
        return "reddit.com" in host
    except WebDriverException:
        return False


_UPVOTE_JS = r"""
function collect(root, out) {
  const sel = 'button, [role="button"], [data-click-id="upvote"], [name="upvote"], [icon-name="upvote"]';
  try { root.querySelectorAll(sel).forEach(el => out.push(el)); } catch (e) {}
  try { root.querySelectorAll('*').forEach(el => { if (el.shadowRoot) collect(el.shadowRoot, out); }); } catch (e) {}
}
function visible(el) {
  try {
    const r = el.getBoundingClientRect();
    const st = window.getComputedStyle(el);
    return r.width > 2 && r.height > 2 && st.visibility !== 'hidden' && st.display !== 'none' && st.opacity !== '0';
  } catch (e) { return false; }
}
function labelOf(el) {
  const bits = [
    el.getAttribute('aria-label'), el.getAttribute('name'), el.getAttribute('icon-name'),
    el.getAttribute('data-click-id'), el.getAttribute('id'), el.innerText
  ];
  return bits.filter(Boolean).join(' ').toLowerCase();
}
function alreadyOn(el) {
  const pressed = (el.getAttribute('aria-pressed') || el.getAttribute('aria-checked') || '').toLowerCase();
  if (pressed === 'true') return true;
  const t = labelOf(el);
  return t.includes('remove upvote') || t.includes('undo upvote') || t.includes('upvoted');
}
const nodes = [];
collect(document, nodes);
const votes = [];
for (const el of nodes) {
  if (!visible(el) || alreadyOn(el)) continue;
  const t = labelOf(el);
  if (t.includes('downvote')) continue;
  const isVote = t.includes('upvote') || t === 'up' || el.getAttribute('data-click-id') === 'upvote'
    || el.getAttribute('name') === 'upvote' || el.getAttribute('icon-name') === 'upvote';
  if (!isVote) continue;
  votes.push(el);
}
if (!votes.length) return false;
const pick = votes[Math.floor(Math.random() * votes.length)];
try { pick.scrollIntoView({block: 'center', inline: 'nearest'}); pick.click(); return true; } catch (e) { return false; }
"""

UPVOTE_XPATHS = [
    "//button[@aria-label='upvote' or contains(translate(@aria-label,'UPVOTE','upvote'),'upvote')]",
    "//button[contains(@aria-label, 'Upvote') and not(contains(@aria-pressed, 'true'))]",
    "//button[@name='upvote' or @data-click-id='upvote']",
    "//shreddit-post//button[contains(@aria-label, 'upvote') or contains(@aria-label, 'Upvote')]",
    "//*[@data-click-id='upvote']",
]

UPVOTE_CSS = [
    "button[aria-label='upvote' i]",
    "button[aria-label*='Upvote']",
    "[data-click-id='upvote']",
    "shreddit-post button[name='upvote']",
]


def upvote_random_post(
    driver: WebDriver,
    label: str = "",
    stats: Optional[AccountSummary] = None,
) -> bool:
    """Click a random un-upvoted upvote button. Never clicks downvote."""
    prefix = f"[Profile {label}] " if label else ""
    key = label or "browser"
    gap = _rng().uniform(*MIN_UPVOTE_GAP)
    if time.time() - _last_upvote_at.get(key, 0.0) < gap:
        return False
    ok = False
    try:
        if driver.execute_script(_UPVOTE_JS):
            ok = True
    except Exception:
        pass
    if not ok:
        pool: List[Any] = []
        for xpath in UPVOTE_XPATHS:
            pool.extend(_visible_elements(driver, By.XPATH, xpath, limit=8))
        for css in UPVOTE_CSS:
            pool.extend(_visible_elements(driver, By.CSS_SELECTOR, css, limit=8))
        unused = []
        seen = set()
        for button in pool:
            try:
                ident = id(button)
                if ident in seen:
                    continue
                seen.add(ident)
                pressed = (button.get_attribute("aria-pressed") or "").lower()
                label_text = (button.get_attribute("aria-label") or "").lower()
                if pressed == "true" or "downvote" in label_text or "upvoted" in label_text:
                    continue
                unused.append(button)
            except StaleElementReferenceException:
                continue
        if unused and human_click(driver, _rng().choice(unused)):
            ok = True
    if ok:
        _last_upvote_at[key] = time.time()
        log(f"{prefix}Upvoted a random post")
        if stats is not None:
            stats.upvotes += 1
        time.sleep(_rng().uniform(0.6, 1.8))
        return True
    return False


def _type_like_human(element: Any, text: str) -> None:
    for char in text:
        element.send_keys(char)
        if char in ".,!? ":
            time.sleep(_rng().uniform(0.12, 0.42))
        else:
            time.sleep(_rng().uniform(0.04, 0.16))


def _type_into_focused(driver: WebDriver, text: str) -> None:
    """Type into whatever is focused (works inside Reddit shadow DOM)."""
    for index, char in enumerate(text):
        typed = False
        try:
            ActionChains(driver).send_keys(char).perform()
            typed = True
        except Exception:
            pass
        if not typed:
            try:
                driver.switch_to.active_element.send_keys(char)
                typed = True
            except Exception:
                pass
        if not typed:
            try:
                driver.execute_cdp_cmd("Input.insertText", {"text": char})
            except Exception:
                continue
        if char in ".,!?":
            time.sleep(_rng().uniform(0.22, 0.70))
        elif char == " ":
            time.sleep(_rng().uniform(0.08, 0.32))
        else:
            time.sleep(_rng().uniform(0.04, 0.16))
        if index > 0 and index % _rng().randint(18, 34) == 0:
            time.sleep(_rng().uniform(0.45, 1.4))
        if char.isalpha() and _rng().random() < 0.028:
            try:
                ActionChains(driver).send_keys(_rng().choice("aeiou")).perform()
                time.sleep(_rng().uniform(0.08, 0.22))
                ActionChains(driver).send_keys(Keys.BACKSPACE).perform()
                time.sleep(_rng().uniform(0.06, 0.16))
            except Exception:
                pass


def _replace_focused_text(driver: WebDriver, text: str) -> None:
    try:
        ActionChains(driver).key_down(Keys.CONTROL).send_keys("a").key_up(Keys.CONTROL).perform()
        time.sleep(_rng().uniform(0.15, 0.35))
        ActionChains(driver).send_keys(Keys.BACKSPACE).perform()
        time.sleep(_rng().uniform(0.12, 0.28))
    except Exception:
        pass
    _type_into_focused(driver, text)


_COMMENT_OPEN_JS = r"""
function walk(root, fn) {
  fn(root);
  let nodes;
  try { nodes = root.querySelectorAll('*'); } catch (e) { return; }
  nodes.forEach(el => { if (el.shadowRoot) walk(el.shadowRoot, fn); });
}
function visible(el) {
  try {
    const r = el.getBoundingClientRect();
    const st = window.getComputedStyle(el);
    return r.width > 6 && r.height > 6 && st.visibility !== 'hidden' && st.display !== 'none';
  } catch (e) { return false; }
}
function txt(el) {
  return ((el.innerText || el.textContent || '') + ' ' + (el.getAttribute('placeholder') || '') + ' ' +
          (el.getAttribute('aria-label') || '')).replace(/\s+/g, ' ').trim().toLowerCase();
}
const prompts = [];
walk(document, root => {
  try { root.querySelectorAll('button, a, [role="button"], [role="textbox"], textarea, div, span, p, shreddit-composer, faceplate-textarea').forEach(el => prompts.push(el)); } catch (e) {}
});
const keys = ['add a comment', 'join the conversation', 'what are your thoughts', 'add your comment', 'leave a comment'];
for (const el of prompts) {
  if (!visible(el)) continue;
  const t = txt(el);
  if (t.includes('search')) continue;
  if (keys.some(k => t.includes(k))) {
    try { el.scrollIntoView({block:'center'}); el.click(); return 'clicked:' + t.slice(0, 40); } catch (e) {}
  }
}
return 'no-prompt';
"""

_COMMENT_FOCUS_JS = r"""
function walk(root, fn) {
  fn(root);
  let nodes;
  try { nodes = root.querySelectorAll('*'); } catch (e) { return; }
  nodes.forEach(el => { if (el.shadowRoot) walk(el.shadowRoot, fn); });
}
function visible(el) {
  try {
    const r = el.getBoundingClientRect();
    const st = window.getComputedStyle(el);
    return r.width > 8 && r.height > 8 && st.visibility !== 'hidden' && st.display !== 'none';
  } catch (e) { return false; }
}
function hint(el) {
  return ((el.getAttribute('placeholder') || '') + ' ' + (el.getAttribute('aria-label') || '') + ' ' +
          (el.getAttribute('name') || '') + ' ' + (el.innerText || '')).toLowerCase();
}
const boxes = [];
walk(document, root => {
  try { root.querySelectorAll('textarea, [contenteditable="true"], [role="textbox"]').forEach(el => boxes.push(el)); } catch (e) {}
});
let box = null;
for (const el of boxes) {
  if (!visible(el)) continue;
  const t = hint(el);
  if (t.includes('search') || t.includes('filter') || t.includes('title')) continue;
  box = el;
  if (t.includes('comment') || t.includes('thought') || t.includes('reply') || t.includes('conversation')) break;
}
if (!box) return 'no-box';
try {
  box.scrollIntoView({block:'center'});
  box.click();
  box.focus();
  return 'focused';
} catch (e) { return 'focus-failed'; }
"""

_COMPOSER_TEXT_JS = r"""
function walk(root, fn) {
  fn(root);
  let nodes;
  try { nodes = root.querySelectorAll('*'); } catch (e) { return; }
  nodes.forEach(el => { if (el.shadowRoot) walk(el.shadowRoot, fn); });
}
function visible(el) {
  try {
    const r = el.getBoundingClientRect();
    const st = window.getComputedStyle(el);
    return r.width > 8 && r.height > 8 && st.visibility !== 'hidden' && st.display !== 'none';
  } catch (e) { return false; }
}
function hint(el) {
  return ((el.getAttribute('placeholder') || '') + ' ' + (el.getAttribute('aria-label') || '')).toLowerCase();
}
const boxes = [];
walk(document, root => {
  try { root.querySelectorAll('textarea, [contenteditable="true"], [role="textbox"]').forEach(el => boxes.push(el)); } catch (e) {}
});
for (const el of boxes) {
  if (!visible(el)) continue;
  const t = hint(el);
  if (t.includes('search') || t.includes('filter') || t.includes('title')) continue;
  const val = (el.value || el.innerText || el.textContent || '');
  if (val && val.trim()) return val.slice(0, 500);
}
return '';
"""

_COMMENT_SUBMIT_JS = r"""
function walk(root, fn) {
  fn(root);
  let nodes;
  try { nodes = root.querySelectorAll('*'); } catch (e) { return; }
  nodes.forEach(el => { if (el.shadowRoot) walk(el.shadowRoot, fn); });
}
function visible(el) {
  try {
    const r = el.getBoundingClientRect();
    const st = window.getComputedStyle(el);
    return r.width > 8 && r.height > 8 && st.visibility !== 'hidden' && st.display !== 'none';
  } catch (e) { return false; }
}
const buttons = [];
walk(document, root => {
  try { root.querySelectorAll('button, [role="button"]').forEach(el => buttons.push(el)); } catch (e) {}
});
const preferred = [];
const rest = [];
for (const el of buttons) {
  if (!visible(el) || el.disabled) continue;
  const t = ((el.innerText || el.textContent || '') + ' ' + (el.getAttribute('aria-label') || ''))
    .replace(/\s+/g, ' ').trim().toLowerCase();
  if (!t || t.includes('search') || t.includes('join') || t.includes('share')) continue;
  if (/\d/.test(t) && t.includes('comment')) continue;
  if (t === 'comments' || t.startsWith('comments')) continue;
  if (!(t === 'comment' || t === 'reply' || t === 'add comment' || t.includes('comment as'))) continue;
  let n = el;
  let inComposer = false;
  for (let i = 0; i < 12 && n; i++) {
    const name = (n.tagName || '').toLowerCase();
    if (name.includes('composer') || name === 'form') { inComposer = true; break; }
    const rootNode = n.getRootNode && n.getRootNode();
    n = n.parentElement || (rootNode && rootNode.host) || null;
  }
  (inComposer ? preferred : rest).push([el, t]);
}
for (const [el, t] of preferred.concat(rest)) {
  try { el.click(); return 'clicked:' + t.slice(0, 40); } catch (e) {}
}
return 'no-submit';
"""

_COMMENT_VERIFY_JS = r"""
const snippet = (arguments[0] || '').toLowerCase();
if (!snippet || snippet.length < 10) return false;
function walk(root, fn) {
  fn(root);
  let nodes;
  try { nodes = root.querySelectorAll('*'); } catch (e) { return; }
  nodes.forEach(el => { if (el.shadowRoot) walk(el.shadowRoot, fn); });
}
function txt(el) {
  try { return (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim(); } catch (e) { return ''; }
}
const blocked = (document.body && document.body.innerText || '').toLowerCase();
if (blocked.includes('you are doing that too much') || blocked.includes("you're doing that too much")
    || blocked.includes('try again later') || blocked.includes('not allowed to comment')) {
  return 'blocked';
}
const hits = [];
walk(document, root => {
  try {
    root.querySelectorAll(
      'shreddit-comment, [data-testid="comment"], .comment, [id^="t1_"]'
    ).forEach(el => hits.push(el));
  } catch (e) {}
});
function insideComposer(el) {
  let n = el;
  for (let i = 0; i < 14 && n; i++) {
    const name = (n.tagName || '').toLowerCase();
    if (name.includes('composer') || name === 'textarea' || name === 'faceplate-textarea') return true;
    const rootNode = n.getRootNode && n.getRootNode();
    n = n.parentElement || (rootNode && rootNode.host) || null;
  }
  return false;
}
for (const el of hits) {
  if (insideComposer(el)) continue;
  const t = txt(el).toLowerCase();
  if (t.length < 10 || t.length > 4000) continue;
  if (t.includes(snippet)) return 'visible';
}
return 'missing';
"""


def _comment_blocked_on_page(driver: WebDriver) -> str:
    try:
        text = (
            driver.execute_script(
                "return (document.body && document.body.innerText || '').slice(0, 5000).toLowerCase();"
            )
            or ""
        )
    except Exception:
        return ""
    for marker in (
        "you're doing that too much",
        "you are doing that too much",
        "try again later",
        "not allowed to comment",
        "unable to comment",
    ):
        if marker in text:
            return marker
    return ""


def _composer_contains(driver: WebDriver, text: str) -> bool:
    snippet = re.sub(r"\s+", " ", (text or "")).strip()[:18].lower()
    if len(snippet) < 8:
        return False
    try:
        blob = str(driver.execute_script(_COMPOSER_TEXT_JS) or "").lower()
    except Exception:
        return False
    return snippet in blob


def _comment_landed(driver: WebDriver, text: str) -> str:
    snippet = re.sub(r"\s+", " ", (text or "")).strip()[:28]
    try:
        result = driver.execute_script(_COMMENT_VERIFY_JS, snippet)
    except Exception:
        result = "missing"
    return str(result or "missing")


_COMMENT_PERMALINK_JS = r"""
const snippet = (arguments[0] || '').toLowerCase();
const authorWanted = (arguments[1] || '').toLowerCase().replace(/^u\//, '');
function walk(root, fn) {
  fn(root);
  let nodes;
  try { nodes = root.querySelectorAll('*'); } catch (e) { return; }
  nodes.forEach(el => { if (el.shadowRoot) walk(el.shadowRoot, fn); });
}
function txt(el) {
  try { return (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim(); } catch (e) { return ''; }
}
function absUrl(href) {
  if (!href) return '';
  try { return new URL(href, location.origin).href; } catch (e) { return href; }
}
function permalinkFrom(el) {
  const attrs = ['permalink', 'data-permalink'];
  for (const name of attrs) {
    const raw = el.getAttribute && el.getAttribute(name);
    if (raw && String(raw).includes('/comments/')) return absUrl(raw);
  }
  const thing = (el.getAttribute && (el.getAttribute('thingid') || el.getAttribute('comment-id') || el.id || '')) || '';
  const id = String(thing).replace(/^t1_/, '');
  if (id && /^[a-z0-9]+$/i.test(id) && location.pathname.includes('/comments/')) {
    const base = location.href.split('?')[0].replace(/\/+$/, '');
    if (/\/comment\//.test(base)) return base;
    return base.replace(/\/$/, '') + '/' + id + '/';
  }
  try {
    const links = el.querySelectorAll ? el.querySelectorAll('a[href*="/comment/"], a[href*="/comments/"]') : [];
    for (const a of links) {
      const href = a.getAttribute('href') || '';
      if (href.includes('/comment/') || /\/comments\/[^/]+\/[^/]+\/[a-z0-9]+/i.test(href)) {
        return absUrl(href);
      }
    }
  } catch (e) {}
  return '';
}
function authorOf(el) {
  try {
    return String(el.getAttribute('author') || el.getAttribute('data-author') || '')
      .toLowerCase().replace(/^u\//, '');
  } catch (e) { return ''; }
}
const hits = [];
walk(document, root => {
  try {
    root.querySelectorAll(
      'shreddit-comment, [data-testid="comment"], .comment, [id^="t1_"]'
    ).forEach(el => hits.push(el));
  } catch (e) {}
});
for (const el of hits) {
  const t = txt(el).toLowerCase();
  if (!snippet || t.length < 8 || !t.includes(snippet)) continue;
  const author = authorOf(el);
  if (authorWanted && author && author !== authorWanted) continue;
  const url = permalinkFrom(el);
  if (url) return url;
}
return '';
"""


def extract_comment_permalink(driver: WebDriver, text: str, author: str = "") -> str:
    """Return the Reddit permalink for a comment that is already visible on the post."""
    snippet = re.sub(r"\s+", " ", (text or "")).strip()[:28]
    if len(snippet) < 8:
        return ""
    try:
        url = driver.execute_script(_COMMENT_PERMALINK_JS, snippet, author or "")
    except Exception:
        url = ""
    url = str(url or "").strip()
    if not url:
        return ""
    if url.startswith("/"):
        url = "https://www.reddit.com" + url
    return url.split("?")[0].split("#")[0]


COMMENT_BOX_XPATHS = [
    "//textarea[contains(@placeholder, 'comment') or contains(@placeholder, 'Comment')]",
    "//textarea[contains(@placeholder, 'thought')]",
    "//div[@contenteditable='true' and (contains(@aria-label, 'comment') or contains(@aria-label, 'Comment'))]",
    "//*[@role='textbox' and (contains(@placeholder, 'comment') or contains(@aria-label, 'comment'))]",
    "//shreddit-composer//textarea",
    "//faceplate-textarea//textarea",
]


def comment_on_current_post(
    driver: WebDriver,
    label: str = "",
    stats: Optional[AccountSummary] = None,
    text: Optional[str] = None,
    count_session: bool = True,
) -> bool:
    """Open the composer, type like a person, submit, and confirm the comment is on the page."""
    prefix = f"[Profile {label}] " if label else ""
    body = (text or "").strip()
    if len(re.findall(r"[A-Za-z]", body)) < 18:
        log(f"{prefix}Comment skipped — text too short to look real")
        return False

    for _ in range(_rng().randint(2, 4)):
        human_scroll(driver, direction=1, remaining=1.8)
        time.sleep(_rng().uniform(0.6, 1.6))
    style = _style()
    think = _rng().uniform(*(style.comment_think if style else COMMENT_THINK))
    log(f"{prefix}Thinking for {think:.0f}s before typing")
    time.sleep(think)

    try:
        opened = driver.execute_script(_COMMENT_OPEN_JS)
        log(f"{prefix}Comment composer: {opened}")
    except Exception as exc:
        log(f"{prefix}Could not open comment box ({brief_error(exc)})")
        opened = "error"
    time.sleep(_rng().uniform(1.0, 2.0))

    focused = ""
    try:
        focused = driver.execute_script(_COMMENT_FOCUS_JS) or ""
    except Exception as exc:
        log(f"{prefix}Could not focus comment box ({brief_error(exc)})")
        focused = "error"
    if focused != "focused":
        box = None
        for xpath in COMMENT_BOX_XPATHS:
            found = _visible_elements(driver, By.XPATH, xpath, limit=3)
            if found:
                box = found[0]
                break
        if box is None:
            log(f"{prefix}Comment box not found ({focused or opened})")
            return False
        human_click(driver, box)
        time.sleep(_rng().uniform(0.4, 0.9))

    log(f"{prefix}Typing comment ({len(body)} chars)")
    _type_into_focused(driver, body)
    time.sleep(_rng().uniform(1.6, 3.4))

    clicked = ""
    try:
        clicked = driver.execute_script(_COMMENT_SUBMIT_JS) or ""
    except Exception as exc:
        clicked = brief_error(exc)
    if not str(clicked).startswith("clicked"):
        try:
            driver.switch_to.active_element.send_keys(Keys.CONTROL, Keys.ENTER)
            clicked = "ctrl-enter"
        except Exception:
            pass
    log(f"{prefix}Comment submit: {clicked}")
    time.sleep(_rng().uniform(2.5, 4.5))

    blocked = _comment_blocked_on_page(driver)
    if blocked:
        log(f"{prefix}Comment did not land on Reddit ({blocked})")
        return False

    submitted = str(clicked).startswith("clicked") or clicked == "ctrl-enter"
    status = "missing"
    deadline = time.time() + 14.0
    while time.time() < deadline:
        status = _comment_landed(driver, body)
        if status == "visible":
            break
        if status == "blocked":
            log(f"{prefix}Comment did not land on Reddit (rate limited or blocked)")
            return False
        on_thread = "/comments/" in ((driver.current_url or "").lower())
        if submitted and on_thread and not _composer_contains(driver, body):
            status = "visible"
            break
        time.sleep(1.2)

    if status != "visible":
        log(
            f"{prefix}Comment not visible on the post after submit "
            f"({status}, submit={clicked}) — not counting it"
        )
        return False

    log(f"{prefix}Comment is on the post: {body[:90]}")
    if stats is not None:
        if count_session:
            stats.comments += 1
        stats.last_comment_text = body
        stats.last_comment_url = extract_comment_permalink(
            driver, body, author=getattr(stats, "reddit_username", "")
        )
        if stats.last_comment_url:
            log(f"{prefix}Comment URL: {stats.last_comment_url}")
    time.sleep(_rng().uniform(3.5, 7.0))
    return True


_COMMENT_EDIT_OPEN_JS = r"""
const snippet = (arguments[0] || '').toLowerCase();
const authorWanted = (arguments[1] || '').toLowerCase().replace(/^u\//, '');
if (!snippet || snippet.length < 8) return 'no-snippet';
function walk(root, fn) {
  fn(root);
  let nodes;
  try { nodes = root.querySelectorAll('*'); } catch (e) { return; }
  nodes.forEach(el => { if (el.shadowRoot) walk(el.shadowRoot, fn); });
}
function visible(el) {
  try {
    const r = el.getBoundingClientRect();
    const st = window.getComputedStyle(el);
    return r.width > 6 && r.height > 6 && st.visibility !== 'hidden' && st.display !== 'none';
  } catch (e) { return false; }
}
function txt(el) {
  try { return (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim(); } catch (e) { return ''; }
}
const comments = [];
walk(document, root => {
  try {
    root.querySelectorAll('shreddit-comment, [data-testid="comment"], .comment, [id^="t1_"]').forEach(el => comments.push(el));
  } catch (e) {}
});
let target = null;
for (const el of comments) {
  const t = txt(el).toLowerCase();
  if (!t.includes(snippet.slice(0, 22))) continue;
  let author = '';
  try { author = (el.getAttribute('author') || el.getAttribute('data-author') || '').toLowerCase(); } catch (e) {}
  if (authorWanted && author && author !== authorWanted) continue;
  target = el;
  break;
}
if (!target) return 'no-comment';
try { target.scrollIntoView({block:'center'}); } catch (e) {}
const buttons = [];
walk(target, root => {
  try { root.querySelectorAll('button, [role="button"], a').forEach(el => buttons.push(el)); } catch (e) {}
});
for (const el of buttons) {
  if (!visible(el)) continue;
  const t = (txt(el) + ' ' + (el.getAttribute('aria-label') || '')).toLowerCase();
  if (t.includes('edit') && !t.includes('editor')) {
    try { el.click(); return 'clicked-edit'; } catch (e) {}
  }
}
for (const el of buttons) {
  if (!visible(el)) continue;
  const t = (txt(el) + ' ' + (el.getAttribute('aria-label') || '')).toLowerCase();
  if (t.includes('overflow') || t.includes('more options') || t.includes('more actions') || t.includes('comment actions')) {
    try { el.click(); return 'opened-menu'; } catch (e) {}
  }
}
return 'no-edit';
"""

_COMMENT_MENU_EDIT_JS = r"""
function walk(root, fn) {
  fn(root);
  let nodes;
  try { nodes = root.querySelectorAll('*'); } catch (e) { return; }
  nodes.forEach(el => { if (el.shadowRoot) walk(el.shadowRoot, fn); });
}
function visible(el) {
  try {
    const r = el.getBoundingClientRect();
    const st = window.getComputedStyle(el);
    return r.width > 4 && r.height > 4 && st.visibility !== 'hidden' && st.display !== 'none';
  } catch (e) { return false; }
}
const items = [];
walk(document, root => {
  try { root.querySelectorAll('button, [role="menuitem"], [role="button"], a, li').forEach(el => items.push(el)); } catch (e) {}
});
for (const el of items) {
  if (!visible(el)) continue;
  const t = ((el.innerText || el.textContent || '') + ' ' + (el.getAttribute('aria-label') || ''))
    .replace(/\s+/g, ' ').trim().toLowerCase();
  if (t === 'edit' || t === 'edit comment' || t.startsWith('edit comment')) {
    try { el.click(); return 'clicked-edit'; } catch (e) {}
  }
}
return 'no-menu-edit';
"""

_COMMENT_SAVE_EDIT_JS = r"""
function walk(root, fn) {
  fn(root);
  let nodes;
  try { nodes = root.querySelectorAll('*'); } catch (e) { return; }
  nodes.forEach(el => { if (el.shadowRoot) walk(el.shadowRoot, fn); });
}
function visible(el) {
  try {
    const r = el.getBoundingClientRect();
    const st = window.getComputedStyle(el);
    return r.width > 8 && r.height > 8 && st.visibility !== 'hidden' && st.display !== 'none';
  } catch (e) { return false; }
}
const buttons = [];
walk(document, root => {
  try { root.querySelectorAll('button, [role="button"]').forEach(el => buttons.push(el)); } catch (e) {}
});
for (const el of buttons) {
  if (!visible(el) || el.disabled) continue;
  const t = ((el.innerText || el.textContent || '') + ' ' + (el.getAttribute('aria-label') || ''))
    .replace(/\s+/g, ' ').trim().toLowerCase();
  if (t === 'save' || t === 'save edits' || t === 'save comment' || t === 'save edit') {
    try { el.click(); return 'clicked:' + t; } catch (e) {}
  }
}
return 'no-save';
"""


def linger_on_current_thread(
    driver: WebDriver,
    seconds: float,
    label: str = "",
    stay_url: str = "",
) -> None:
    """Stay on the same post for a few minutes so the later edit looks natural."""
    prefix = f"[Profile {label}] " if label else ""
    recover = stay_url or ""
    try:
        recover = recover or (driver.current_url or "")
    except Exception:
        recover = stay_url or REDDIT_HOME_URL
    end_at = time.time() + max(20.0, float(seconds))
    log(f"{prefix}Waiting {seconds / 60:.1f} min on this post, then editing the comment")
    while time.time() < end_at:
        try:
            here = (driver.current_url or "").lower()
            if recover and "/comments/" in recover.lower() and "/comments/" not in here:
                navigate(driver, recover, label)
        except Exception:
            pass
        remaining = end_at - time.time()
        if remaining <= 0:
            break
        if _rng().random() < 0.6:
            human_scroll(driver, direction=1 if _rng().random() < 0.7 else -1, remaining=min(2.0, remaining))
        time.sleep(min(_rng().uniform(7.0, 16.0), max(0.4, end_at - time.time())))


def edit_own_comment_on_current_post(
    driver: WebDriver,
    label: str,
    original: str,
    new_text: str,
    author: str = "",
) -> bool:
    """Open this account's comment, replace the body, and save."""
    prefix = f"[Profile {label}] " if label else ""
    snippet = re.sub(r"\s+", " ", (original or "")).strip()
    body = (new_text or "").strip()
    if len(snippet) < 12 or len(re.findall(r"[A-Za-z]", body)) < 18:
        log(f"{prefix}Comment edit skipped — missing original or new text")
        return False
    try:
        opened = driver.execute_script(_COMMENT_EDIT_OPEN_JS, snippet[:40], author or "") or ""
    except Exception as exc:
        opened = brief_error(exc)
    log(f"{prefix}Comment edit menu: {opened}")
    if opened == "opened-menu":
        time.sleep(_rng().uniform(0.6, 1.2))
        try:
            opened = driver.execute_script(_COMMENT_MENU_EDIT_JS) or opened
        except Exception as exc:
            opened = brief_error(exc)
        log(f"{prefix}Comment edit item: {opened}")
    if "edit" not in str(opened).lower():
        log(f"{prefix}Could not open the comment editor")
        return False
    time.sleep(_rng().uniform(1.0, 2.0))
    focused = ""
    try:
        focused = driver.execute_script(_COMMENT_FOCUS_JS) or ""
    except Exception as exc:
        focused = brief_error(exc)
    if focused != "focused":
        log(f"{prefix}Comment edit box not focused ({focused})")
        return False
    log(f"{prefix}Typing edited comment ({len(body)} chars)")
    _replace_focused_text(driver, body)
    time.sleep(_rng().uniform(1.2, 2.4))
    saved = ""
    try:
        saved = driver.execute_script(_COMMENT_SAVE_EDIT_JS) or ""
    except Exception as exc:
        saved = brief_error(exc)
    if not str(saved).startswith("clicked"):
        try:
            driver.switch_to.active_element.send_keys(Keys.CONTROL, Keys.ENTER)
            saved = "ctrl-enter"
        except Exception:
            pass
    log(f"{prefix}Comment edit save: {saved}")
    time.sleep(_rng().uniform(2.5, 4.5))
    status = _comment_landed(driver, body)
    if status != "visible":
        add_snip = re.sub(r"\s+", " ", body[len(original) :] if body.startswith(original) else body).strip()
        if add_snip:
            status = _comment_landed(driver, add_snip)
    if status != "visible":
        log(f"{prefix}Edited comment not visible ({status}) — leaving the original up")
        return False
    log(f"{prefix}Comment edited: {body[:90]}")
    return True


_EXTRACT_POST_JS = r"""
function walk(root, fn) {
  fn(root);
  let nodes;
  try { nodes = root.querySelectorAll('*'); } catch (e) { return; }
  nodes.forEach(el => { if (el.shadowRoot) walk(el.shadowRoot, fn); });
}
function txt(el) {
  try { return (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim(); } catch (e) { return ''; }
}
let title = '';
const titleNodes = [];
walk(document, root => {
  try {
    root.querySelectorAll('h1, [slot="title"], [id^="post-title"], shreddit-post a[slot="title"]').forEach(el => titleNodes.push(el));
  } catch (e) {}
});
for (const el of titleNodes) {
  const t = txt(el);
  if (t && t.length > 8 && t.length < 400) { title = t; break; }
}
let body = '';
const bodyNodes = [];
walk(document, root => {
  try {
    root.querySelectorAll('[slot="text-body"], [id$="-post-rtjson-content"], .md, shreddit-post [data-click-id="text"]').forEach(el => bodyNodes.push(el));
  } catch (e) {}
});
for (const el of bodyNodes) {
  const t = txt(el);
  if (t && t.length > 20) { body = t.slice(0, 2500); break; }
}
return { title: title, body: body, url: location.href };
"""


def extract_opened_post(driver: WebDriver) -> Dict[str, str]:
    try:
        data = driver.execute_script(_EXTRACT_POST_JS) or {}
    except Exception:
        data = {}
    title = str(data.get("title") or "").strip()
    body = str(data.get("body") or "").strip()
    url = str(data.get("url") or "")
    try:
        url = url or (driver.current_url or "")
    except Exception:
        pass
    return {"title": title, "body": body, "url": url}


def _too_new_for_general_comment(stats: AccountSummary) -> bool:
    """Karma 0 and younger than 3 days does not get a general comment."""
    try:
        karma = int(stats.account_karma or 0)
    except (TypeError, ValueError):
        karma = 0
    try:
        age = float(stats.account_age_days or 0.0)
    except (TypeError, ValueError):
        age = 0.0
    return karma == 0 and age < float(GENERAL_COMMENT_MIN_AGE_DAYS)


def maybe_ai_comment_on_opened_post(
    driver: WebDriver,
    label: str,
    user_id: str,
    stats: AccountSummary,
    *,
    kind: str = "karma",
    force: bool = False,
) -> bool:
    """Open a post, analyze it, then leave one comment that matches that post."""
    prefix = f"[Profile {label}] "
    have_window = len(general_comments_this_week(user_id))
    if comments_remaining(user_id) <= 0:
        log(
            f"{prefix}Comment skipped — already used "
            f"{have_window}/{COMMENTS_PER_WINDOW} comments in {ACTION_WINDOW_HOURS:.0f}h"
        )
        return False
    if kind != "sheet" and _too_new_for_general_comment(stats):
        log(
            f"{prefix}Comment skipped — karma is 0 and the account is under "
            f"{GENERAL_COMMENT_MIN_AGE_DAYS} days old"
        )
        return False
    if kind != "sheet":
        target = stats.session_comment_target or SESSION_COMMENT_CAP
        have_week, room = general_comment_need(user_id, session_want=target)
        if room <= 0:
            log(
                f"{prefix}Comment skipped — "
                f"{have_week}/{COMMENTS_PER_WINDOW} comments in {ACTION_WINDOW_HOURS:.0f}h"
            )
            return False
        if stats.comments >= target or stats.ai_comments >= target:
            log(f"{prefix}Comment skipped — already left {target} general comments this run")
            return False
    try:
        from reddit_joiner.store import already_commented_url, can_ai_comment
    except ImportError:
        already_commented_url = None  # type: ignore[assignment]
        can_ai_comment = None  # type: ignore[assignment]
    if can_ai_comment is not None and kind != "sheet":
        delay = MIN_DELAY_BETWEEN_AI_COMMENTS
        allowed, why = can_ai_comment(
            user_id or label,
            max_per_day=MAX_AI_COMMENTS_PER_ACCOUNT_DAY,
            min_delay_seconds=delay,
        )
        if not allowed:
            if "wait" in why and wait_before_next_comment(
                driver, label, user_id, stay_url=""
            ):
                allowed, why = can_ai_comment(
                    user_id or label,
                    max_per_day=MAX_AI_COMMENTS_PER_ACCOUNT_DAY,
                    min_delay_seconds=delay,
                )
            if not allowed:
                log(f"{prefix}Comment skipped — {why}")
                return False
    info = extract_opened_post(driver)
    title = info.get("title") or ""
    body = info.get("body") or ""
    url = info.get("url") or ""
    subreddit = subreddit_from_url(url)
    if kind != "sheet" and normalize_subreddit(subreddit).lower() in {
        name.lower() for name in (stats.lurk_subs or [])
    }:
        sheet = {item.lower() for item in allowed_subreddits()}
        still_need = int(stats.comments or 0) < int(stats.session_comment_target or 0)
        if still_need and normalize_subreddit(subreddit).lower() in sheet:
            log(
                f"{prefix}RL marked r/{subreddit} lurk — still commenting "
                "because this sitting still needs a general comment"
            )
        else:
            log(f"{prefix}Comment skipped — RL chose lurk in r/{subreddit} after the rules")
            return False
    if not title:
        log(f"{prefix}Comment skipped — could not read the post title")
        return False
    if _page_is_media_post(driver):
        log(f"{prefix}Comment skipped — image, video, or gallery post")
        return False
    if already_commented_on(user_id, url) or (
        already_commented_url is not None and already_commented_url(user_id or label, url)
    ):
        log(f"{prefix}Comment skipped — already commented on this post")
        return False
    if kind == "sheet" and link_commented_by_any_account(url):
        log(f"{prefix}Comment skipped — another account already commented on this link")
        return False
    try:
        from reddit_joiner.ai import analyze_post, generate_ai_comment, local_comment_for_post
    except ImportError as exc:
        log(f"{prefix}Comment module missing ({exc})")
        return False

    analysis = analyze_post(title, body, subreddit)
    score = float(analysis.get("sentiment") or 0.0)
    mood = str(analysis.get("mood") or "neutral")
    intent = str(analysis.get("intent") or "other")
    topic = str(analysis.get("topic") or title)[:80]
    where = f"r/{subreddit} " if subreddit else ""
    log(f"{prefix}Reading {where}post: {title[:90]}")
    log(
        f"{prefix}Post analysis — mood {mood}, intent {intent}, "
        f"sentiment {score:.2f}, topic: {topic}"
    )
    # The post has now been analysed, so this is the decision point: a general
    # comment is written only for a question, suggestion, review or help request.
    # Sheet links are exempt because the sheet names the exact post to reply to.
    # This deliberately ignores `force`, which only selects the tone from here on.
    if kind != "sheet" and not commentable_intent(intent):
        log(
            f"{prefix}Moved on without commenting — {intent} post, "
            "not help/suggestion/question/review"
        )
        return False

    page_replies = -1
    try:
        page_replies = int(
            driver.execute_script(
                """
                try {
                  const n = document.querySelector('shreddit-post');
                  if (n && n.getAttribute('comment-count') != null)
                    return parseInt(n.getAttribute('comment-count'), 10) || 0;
                } catch (e) {}
                return -1;
                """
            )
        )
    except Exception:
        page_replies = -1
    if kind != "sheet" and page_replies == 0:
        log(f"{prefix}Empty thread + {intent} post — commenting anyway")
        force = True
    rules = _rules_for(stats, subreddit)
    if rules is None and subreddit:
        try:
            rules = read_subreddit_rules(
                driver, label, subreddit, stats, open_page=False
            )
        except Exception:
            rules = None
    if rules is not None and getattr(rules, "titles", None):
        log(f"{prefix}Using r/{subreddit} rules ({rules.rule_count}): {', '.join(rules.titles[:4])}")
    rule_flags = (getattr(rules, "flags", None) or {}) if rules is not None else {}
    if kind != "sheet" and rule_flags.get("questions_only") and intent != "question":
        log(
            f"{prefix}Comment skipped — r/{subreddit} rules are questions only"
        )
        return False
    if kind != "sheet" and rule_flags.get("account_gate") and _too_new_for_general_comment(stats):
        log(
            f"{prefix}Comment skipped — r/{subreddit} rules want karma or account age"
        )
        return False
    time.sleep(human.gaussian_between(*POST_READ_WAIT))
    # Read scaled to how long the post actually is, then think it over
    read_opened_thread(driver, 0.0, label)
    style = _style()
    think = human.gaussian_between(*(style.comment_think if style else COMMENT_THINK))
    time.sleep(think)
    if _rng().random() < 0.5:
        _mouse_wander(driver)
    # Last-second change of mind — a person sometimes closes the tab instead
    if kind != "sheet" and human.changed_mind(style.bail_chance if style else 0.12):
        log(f"{prefix}Read the thread and changed mind — leaving without commenting")
        return False

    tone = "neutral" if mood == "negative" else "friendly"
    action = f"comment:{tone}"
    state = _rl_state(stats, subreddit, title, body, sentiment=score, kind=kind, rules=rules)
    agent = _rl_agent()
    if agent is not None and RL_ENABLED:
        try:
            from reddit_joiner.rl import COMMENT_ACTIONS, COMMENT_TONE_ACTIONS, parse_comment_action

            choices = COMMENT_TONE_ACTIONS if force else COMMENT_ACTIONS
            chosen = agent.choose_action(state, choices)
            action = chosen or action
            should, tone = parse_comment_action(action)
            log(f"{prefix}RL comment action: {action} (ε={agent.epsilon:.3f})")
            if not should:
                _rl_learn(stats, state, action, 0.0)
                return False
        except Exception as exc:
            log(f"{prefix}RL comment choice skipped ({brief_error(exc)})")

    try:
        from reddit_joiner.rules import safer_tone

        fitted = safer_tone(rules, tone)
        if fitted != tone:
            log(f"{prefix}r/{subreddit} rules discourage {tone} — using {fitted}")
            _rl_learn(stats, state, action, REWARD_RULES_TONE_CLASH)
            tone = fitted
            action = f"comment:{tone}"
        elif rules is not None:
            _rl_learn(stats, state, action, REWARD_RULES_TONE_FIT)
    except Exception:
        pass

    text = ""
    provider = "local"
    try:
        text, provider = generate_ai_comment(
            title,
            body,
            subreddit=subreddit,
            tone=tone,
            analysis=analysis,
            rules=getattr(rules, "prompt_text", lambda: "")(),
        )
    except Exception as exc:
        log(
            f"{prefix}AI generate failed ({brief_error(exc)}) — "
            "writing a comment from this post's topic"
        )
        text, provider = local_comment_for_post(analysis), "local"
    if len(re.findall(r"[A-Za-z]", text or "")) < 18:
        log(f"{prefix}Comment skipped — generated text was too short")
        return False
    log(
        f"{prefix}Commenting on this post ({provider}, {mood}): {text[:90]}"
    )
    ok = comment_on_current_post(
        driver, label, stats, text=text, count_session=(kind != "sheet")
    )
    if ok:
        if kind != "sheet":
            stats.ai_comments += 1
        stats.last_comment_text = text
        record_account_comment(user_id, url, text, kind=kind)
        if kind == "karma":
            stats.karma_comments += 1
        posted_state = _rl_state(
            stats,
            subreddit,
            title,
            body,
            sentiment=score,
            kind=kind,
            comment_length=len(text),
            rules=rules,
        )
        # Submitting successfully says nothing about whether the comment was any
        # good — it fires every time the composer worked. Training on it used to
        # add a +2 sample that the later outcome check then contradicted with a
        # negative one for the same action. Count it, do not learn from it; the
        # delayed live/removed/score check below is the real label.
        stats.rl_reward += float(REWARD_COMMENT_POSTED)
        if kind != "sheet":
            try:
                agent = _rl_agent()
                if agent is not None:
                    agent.queue_delayed(
                        posted_state,
                        action,
                        stats.last_comment_url or url,
                        "comment",
                    )
            except Exception:
                pass
        try:
            from reddit_joiner.store import log_ai_comment

            comment_id = _ids_from_reddit_url(stats.last_comment_url or "")[1]
            log_ai_comment(
                account=label,
                user_id=user_id,
                post_url=url,
                post_title=title,
                sentiment=score,
                comment=text,
                method=provider,
                subreddit=subreddit,
                post_body=body[:500],
                comment_id=comment_id,
                tone=tone,
            )
        except Exception as exc:
            log(f"{prefix}Could not log AI comment ({brief_error(exc)})")
    else:
        # A failed submit is our automation missing the composer, not Reddit
        # judging the tone, so keep it out of tone training.
        stats.rl_reward += float(REWARD_COMMENT_FAILED)
    return ok


def leave_opened_page(
    driver: WebDriver,
    label: str = "",
    *,
    recover_url: str = "",
) -> str:
    """
    Leave a thread using a random path: browser Back, Home logo/nav,
    or go straight to the recover URL / Home.
    """
    prefix = f"[Profile {label}] " if label else ""
    want_home = (not recover_url) or recover_url.rstrip("/").lower() in {
        REDDIT_HOME_URL.rstrip("/").lower(),
        "https://www.reddit.com",
        "https://reddit.com",
    }
    methods = ["back", "logo", "nav", "recover"] if not want_home else ["back", "logo", "nav", "home"]
    _rng().shuffle(methods)
    for method in methods:
        try:
            if method == "back":
                driver.back()
                wait_for_page_ready(driver, timeout=10)
                time.sleep(_rng().uniform(0.6, 1.4))
                if current_is_reddit(driver):
                    log(f"{prefix}Left thread via Back")
                    return "back"
            elif method == "logo":
                if _home_via_logo(driver):
                    time.sleep(_rng().uniform(0.8, 1.6))
                    dismiss_popups(driver)
                    log(f"{prefix}Left thread via Reddit logo → Home")
                    return "logo"
            elif method == "nav":
                if _home_via_nav(driver):
                    time.sleep(_rng().uniform(0.8, 1.6))
                    dismiss_popups(driver)
                    log(f"{prefix}Left thread via Home nav")
                    return "nav"
            elif method == "recover" and recover_url:
                navigate(driver, recover_url, label or "browser")
                log(f"{prefix}Left thread → feed")
                return "recover"
            elif method == "home":
                return_to_reddit_home(driver, label, reason="after reading a post")
                return "home"
        except Exception:
            continue
    if recover_url:
        try:
            navigate(driver, recover_url, label or "browser")
            return "recover"
        except Exception:
            pass
    try:
        return_to_reddit_home(driver, label, reason="fallback after post")
        return "home"
    except Exception:
        return "fail"


def perform_browse_activity(
    driver: WebDriver,
    duration: float,
    label: str = "",
    user_id: str = "",
    stats: Optional[AccountSummary] = None,
    stay_url: str = "",
) -> None:
    """Step 2 browse loop: scroll ranges, hover, open posts, optional AI comments."""
    prefix = f"[Profile {label}] " if label else ""
    recover_url = stay_url or REDDIT_HOME_URL
    in_community = "/r/" in recover_url.lower() and not any(
        part in recover_url.lower() for part in ("/r/popular", "/r/all")
    )
    style = _style()
    open_chance = (
        (style.community_post_chance if style else COMMUNITY_POST_CHANCE)
        if in_community
        else (style.click_post_chance if style else CLICK_POST_CHANCE)
    )
    break_chance = style.break_chance if style else HUMAN_BREAK_CHANCE
    human_break = style.human_break if style else HUMAN_BREAK
    upvote_chance = style.upvote_chance if style else UPVOTE_CHANCE
    hover_chance = style.hover_chance if style else HOVER_CHANCE
    scroll_down_px = style.scroll_down_px if style else SCROLL_DOWN_PX
    scroll_up_px = style.scroll_up_px if style else SCROLL_UP_PX
    down_share = style.scroll_down if style else 0.58
    up_share = style.scroll_up if style else 0.16
    wander_share = style.wander if style else 0.08
    thread_read = style.thread_read if style else THREAD_READ
    post_load = style.post_load_wait if style else POST_LOAD_WAIT
    after_comment = style.after_comment if style else AFTER_COMMENT_LINGER
    opened_up = style.opened_upvote_chance if style else 0.45
    if stats is None:
        stats = AccountSummary(name=label or "browser", user_id=user_id)
    if not current_is_reddit(driver):
        if stay_url:
            navigate(driver, stay_url, label or "browser")
        else:
            open_reddit_home_ready(driver, label or "browser")
    end_at = time.time() + max(0.0, float(duration))
    comments_left = 0
    last_pulse = 0.0
    pulse_every = (style.pulse_every if style else 30.0) if duration >= 60 else 8.0
    minutes = max(0.0, float(duration)) / 60.0
    if minutes >= 1.5:
        log(f"{prefix}browsing — {minutes:.0f} min")
    else:
        log(f"{prefix}browsing — {int(max(0, duration))}s")
    while time.time() < end_at:
        remaining = end_at - time.time()
        if remaining <= 0:
            break
        if not current_is_reddit(driver):
            try:
                if stay_url:
                    navigate(driver, stay_url, label or "browser")
                else:
                    open_reddit_home_ready(driver, label or "browser")
            except Exception as exc:
                log(f"{prefix}Stopped activity ({brief_error(exc)})")
                return
        now = time.time()
        if now - last_pulse >= pulse_every:
            left_m = remaining / 60.0
            if left_m >= 1.5:
                log(f"{prefix}browsing — {left_m:.1f} min left")
            else:
                log(f"{prefix}browsing — {int(remaining)}s left")
            last_pulse = now

        if _rng().random() < break_chance and remaining > 35:
            pause = min(_rng().uniform(*human_break), remaining - 8.0)
            log(f"{prefix}Sitting still for {pause:.0f}s")
            time.sleep(max(4.0, pause))
            remaining = end_at - time.time()

        maybe_human_side_trip(driver, label, stats, remaining, recover_url)
        remaining = end_at - time.time()

        roll = _rng().random()
        if roll < down_share:
            _scroll_flicks(driver, scroll_down_px, 1, remaining)
        elif roll < down_share + up_share:
            _scroll_flicks(driver, scroll_up_px, -1, min(remaining, 3.0))
        elif roll < down_share + up_share + wander_share:
            _mouse_wander(driver)
        elif _rng().random() < 0.35 and remaining > 8:
            human_sleep(_rng().uniform(2.5, 8.0), remaining)
        remaining = end_at - time.time()
        _reading_pause(remaining)

        remaining = end_at - time.time()
        if remaining <= 0:
            break
        if _rng().random() < upvote_chance:
            upvote_random_post(driver, label, stats)

        if _rng().random() < hover_chance:
            link = _random_link(driver)
            if link is not None:
                _hover(driver, link)
                time.sleep(min(_rng().uniform(0.5, 1.6), max(0.1, end_at - time.time())))

        remaining = end_at - time.time()
        if _rng().random() < open_chance and remaining > 28:
            post = _random_post_title(driver)
            if post is None:
                continue
            log(f"{prefix}Opening a random post to read")
            if not human_click(driver, post):
                continue
            time.sleep(min(_rng().uniform(*post_load), remaining - 12.0))
            dismiss_popups(driver)
            read_opened_thread(
                driver,
                min(_rng().uniform(*thread_read), max(8.0, end_at - time.time() - 10.0)),
                label,
            )
            if UPVOTE_ON_OPENED_POST and _rng().random() < opened_up:
                upvote_random_post(driver, label, stats)
            if comments_left > 0 and remaining > 40 and _rng().random() < AI_COMMENT_CHANCE:
                try:
                    if maybe_ai_comment_on_opened_post(driver, label, user_id, stats, kind="browse"):
                        comments_left -= 1
                        human_wait(
                            driver,
                            min(_rng().uniform(*after_comment), max(8.0, end_at - time.time())),
                            label,
                            recover_url,
                        )
                except Exception as exc:
                    log(f"{prefix}AI comment skipped ({brief_error(exc)})")
            leave_opened_page(driver, label, recover_url=recover_url)
            time.sleep(min(_rng().uniform(1.2, 3.0), max(0.1, end_at - time.time())))


def perform_human_activity(
    driver: WebDriver,
    duration: float,
    label: str = "",
    *,
    allow_upvote: bool = True,
    allow_open_post: bool = True,
    allow_comment: bool = True,
    stats: Optional[AccountSummary] = None,
) -> None:
    """
    Browse until `duration` seconds have elapsed.

    Scrolls in short wheel bursts, then pauses to read. Does not jump
    hundreds of pixels in one shot or scroll up after every down-scroll.
    """
    prefix = f"[Profile {label}] " if label else ""
    style = _style()
    break_chance = style.break_chance if style else HUMAN_BREAK_CHANCE
    human_break = style.human_break if style else HUMAN_BREAK
    upvote_chance = style.upvote_chance if style else UPVOTE_CHANCE
    hover_chance = style.hover_chance if style else HOVER_CHANCE
    down_share = style.scroll_down if style else 0.58
    up_share = style.scroll_up if style else 0.18
    wander_share = style.wander if style else 0.08
    thread_read = style.thread_read if style else THREAD_READ
    post_load = style.post_load_wait if style else POST_LOAD_WAIT
    after_comment = style.after_comment if style else AFTER_COMMENT_LINGER
    opened_up = style.opened_upvote_chance if style else 0.45
    comment_chance = style.comment_chance if style else COMMENT_CHANCE
    pulse_every = style.pulse_every if style else 30.0
    if not current_is_reddit(driver):
        log(f"{prefix}Not on Reddit yet — opening home before any scrolling")
        open_reddit_home_ready(driver, label or "browser")
    end_at = time.time() + max(0.0, float(duration))
    comments_left = 0  # comments happen in the dedicated analyze-then-comment pass
    last_pulse = 0.0
    while time.time() < end_at:
        remaining = end_at - time.time()
        if remaining <= 0:
            break
        if not current_is_reddit(driver):
            log(f"{prefix}Left Reddit — going back to home before more activity")
            try:
                open_reddit_home_ready(driver, label or "browser")
            except Exception as exc:
                log(f"{prefix}Stopped activity ({brief_error(exc)})")
                return
        now = time.time()
        if now - last_pulse >= pulse_every:
            log(f"{prefix}browsing — {int(remaining)}s left")
            last_pulse = now

        if _rng().random() < break_chance and remaining > 35:
            pause = min(_rng().uniform(*human_break), remaining - 8.0)
            log(f"{prefix}Sitting still for {pause:.0f}s")
            time.sleep(max(4.0, pause))
            remaining = end_at - time.time()

        if stats is not None:
            maybe_human_side_trip(driver, label, stats, remaining, REDDIT_HOME_URL)

        roll = _rng().random()
        if roll < down_share:
            _scroll_flicks(driver, style.scroll_down_px if style else SCROLL_DOWN_PX, 1, remaining)
        elif roll < down_share + up_share:
            _scroll_flicks(driver, style.scroll_up_px if style else SCROLL_UP_PX, -1, min(remaining, 2.5))
        elif roll < down_share + up_share + wander_share:
            _mouse_wander(driver)
        elif _rng().random() < 0.35 and remaining > 8:
            human_sleep(_rng().uniform(2.5, 8.0), remaining)

        remaining = end_at - time.time()
        _reading_pause(remaining)
        remaining = end_at - time.time()
        if remaining <= 0:
            break

        if allow_upvote and _rng().random() < upvote_chance:
            if upvote_random_post(driver, label, stats):
                time.sleep(min(_rng().uniform(1.0, 2.8), max(0.1, remaining)))
            else:
                time.sleep(min(_rng().uniform(0.4, 1.0), max(0.1, remaining)))

        if _rng().random() < hover_chance:
            link = _random_link(driver)
            if link is not None:
                _hover(driver, link)
                time.sleep(min(_rng().uniform(0.6, 1.8), max(0.1, end_at - time.time())))

        remaining = end_at - time.time()
        if allow_open_post and _rng().random() < (style.click_post_chance if style else 0.26) and remaining > 22:
            post = _random_post_title(driver)
            if post is not None:
                log(f"{prefix}Opening a random post to read")
                if human_click(driver, post):
                    time.sleep(min(_rng().uniform(*post_load), remaining - 8.0))
                    dismiss_popups(driver)
                    read_opened_thread(
                        driver,
                        min(_rng().uniform(*thread_read), max(8.0, end_at - time.time() - 8.0)),
                        label,
                    )
                    if allow_upvote and UPVOTE_ON_OPENED_POST and _rng().random() < opened_up:
                        upvote_random_post(driver, label, stats)
                        time.sleep(min(_rng().uniform(0.8, 2.0), max(0.1, end_at - time.time())))
                    if (
                        allow_comment
                        and comments_left > 0
                        and _rng().random() < comment_chance
                    ):
                        try:
                            if comment_on_current_post(driver, label, stats):
                                comments_left -= 1
                                human_wait(
                                    driver,
                                    min(_rng().uniform(*after_comment), max(8.0, end_at - time.time())),
                                    label,
                                )
                        except Exception as exc:
                            log(f"{prefix}Comment skipped ({brief_error(exc)})")
                    leave_opened_page(driver, label, recover_url=REDDIT_HOME_URL)
                    time.sleep(min(_rng().uniform(1.2, 3.0), max(0.1, end_at - time.time())))


# =============================================================================
# PER-PROFILE FLOW
# =============================================================================

_SUBMIT_POST_JS = r"""
const title = arguments[0];
const body = arguments[1] || '';
function walk(root, fn) {
  fn(root);
  let nodes;
  try { nodes = root.querySelectorAll('*'); } catch (e) { return; }
  nodes.forEach(el => { if (el.shadowRoot) walk(el.shadowRoot, fn); });
}
function visible(el) {
  try {
    const r = el.getBoundingClientRect();
    const st = window.getComputedStyle(el);
    return r.width > 8 && r.height > 8 && st.visibility !== 'hidden' && st.display !== 'none';
  } catch (e) { return false; }
}
function setValue(el, value) {
  const tag = (el.tagName || '').toLowerCase();
  if (tag === 'faceplate-textarea' || tag.includes('composer') || tag.includes('textarea')) {
    try {
      const inner = (el.shadowRoot && el.shadowRoot.querySelector('textarea, input, [contenteditable="true"]'))
        || el.querySelector('textarea, input, [contenteditable="true"]');
      if (inner) el = inner;
    } catch (e) {}
  }
  try { el.scrollIntoView({block:'center'}); } catch (e) {}
  el.focus();
  try { el.click(); } catch (e) {}
  const isEdit = el.isContentEditable || el.getAttribute('contenteditable') === 'true';
  const keepBreaks = String(value).indexOf('\n') >= 0;
  if (isEdit) {
    if (keepBreaks) {
      el.innerText = value;
    } else {
      try { document.execCommand('selectAll', false, null); } catch (e) {}
      try {
        if (!document.execCommand('insertText', false, value)) {
          el.innerText = value;
        }
      } catch (e) {
        el.innerText = value;
      }
    }
  } else if ('value' in el) {
    const proto = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value')
      || Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value');
    if (proto && proto.set) proto.set.call(el, value);
    else el.value = value;
  } else {
    el.innerText = value;
  }
  el.dispatchEvent(new Event('input', {bubbles: true, composed: true}));
  el.dispatchEvent(new Event('change', {bubbles: true, composed: true}));
  el.dispatchEvent(new KeyboardEvent('keyup', {bubbles: true, composed: true, key: 'a'}));
  try {
    el.dispatchEvent(new InputEvent('input', {bubbles: true, composed: true, data: value, inputType: 'insertText'}));
  } catch (e) {}
}
function hint(el) {
  return ((el.getAttribute('placeholder') || '') + ' ' + (el.getAttribute('aria-label') || '') + ' ' +
          (el.getAttribute('name') || '') + ' ' + (el.getAttribute('id') || '') + ' ' +
          (el.getAttribute('data-testid') || '')).toLowerCase();
}
function inChrome(el) {
  try {
    return !!(el.closest('nav, header, aside, [slot="tabbar"]') ||
      (el.getAttribute('aria-label') || '').toLowerCase().includes('create'));
  } catch (e) { return false; }
}

const tabs = [];
walk(document, root => {
  try { root.querySelectorAll('button, [role="tab"], [role="button"], faceplate-tabitem').forEach(el => tabs.push(el)); } catch (e) {}
});
for (const el of tabs) {
  if (!visible(el)) continue;
  const t = ((el.innerText || el.textContent || '') + ' ' + (el.getAttribute('aria-label') || '')).toLowerCase().replace(/\s+/g, ' ').trim();
  if (t === 'text' || t.includes('text post') || t.includes('body text')) {
    if (inChrome(el) && t === 'post') continue;
    try { el.click(); break; } catch (e) {}
  }
}

const fields = [];
walk(document, root => {
  try { root.querySelectorAll('textarea, input[type="text"], [contenteditable="true"], faceplate-textarea').forEach(el => fields.push(el)); } catch (e) {}
});
let titleEl = null;
let bodyEl = null;
for (const el of fields) {
  if (!visible(el)) continue;
  const t = hint(el);
  if (t.includes('search') || t.includes('community')) continue;
  if (!titleEl && (t.includes('title') || t === 'title' || t.includes('post title'))) titleEl = el;
  else if (!bodyEl && (t.includes('body') || t.includes('text') || t.includes('markdown') || el.tagName === 'TEXTAREA')) bodyEl = el;
}
if (!titleEl) {
  for (const el of fields) { if (visible(el) && !hint(el).includes('search')) { titleEl = el; break; } }
}
if (!bodyEl) {
  for (const el of fields) {
    if (el === titleEl || !visible(el)) continue;
    const t = hint(el);
    if (t.includes('search') || t.includes('community') || t.includes('title')) continue;
    bodyEl = el; break;
  }
}
if (!titleEl) return 'no-title';
try { titleEl.focus(); titleEl.click(); } catch (e) {}
return 'ready';
"""

_SUBMIT_CLICK_JS = r"""
function walk(root, fn) {
  fn(root);
  let nodes;
  try { nodes = root.querySelectorAll('*'); } catch (e) { return; }
  nodes.forEach(el => { if (el.shadowRoot) walk(el.shadowRoot, fn); });
}
function visible(el, allowDisabled) {
  try {
    const r = el.getBoundingClientRect();
    const st = window.getComputedStyle(el);
    const disabled = el.disabled || el.getAttribute('aria-disabled') === 'true' || el.hasAttribute('disabled');
    return r.width > 4 && r.height > 4 && st.visibility !== 'hidden' && st.display !== 'none' && (allowDisabled || !disabled);
  } catch (e) { return false; }
}
function labelOf(el) {
  const parts = [
    el.innerText, el.textContent,
    el.getAttribute('aria-label'), el.getAttribute('name'),
    el.getAttribute('id'), el.getAttribute('data-testid'),
    el.getAttribute('slot'), el.getAttribute('type'),
    el.tagName
  ];
  try { if (el.shadowRoot) parts.push(el.shadowRoot.textContent); } catch (e) {}
  return parts.filter(Boolean).join(' ').replace(/\s+/g, ' ').trim().toLowerCase();
}
function isTab(el) {
  try {
    const role = (el.getAttribute('role') || '').toLowerCase();
    if (role === 'tab') return true;
    if (el.closest('[role="tablist"], [slot="tabbar"], faceplate-tabgroup, r-post-type-select')) return true;
  } catch (e) {}
  return false;
}
function inComposer(el) {
  try {
    return !!(el.closest(
      'form, shreddit-composer, shreddit-post-creation-form, r-post-form, r-post-form-submit-button, ' +
      '[data-testid*="submit"], [data-testid*="composer"]'
    ));
  } catch (e) { return false; }
}
function inChrome(el) {
  try { return !!(el.closest('nav, header, aside')); } catch (e) { return false; }
}
function innerClickable(el) {
  try {
    if (el.shadowRoot) {
      const inner = el.shadowRoot.querySelector('button, [role="button"], input[type="submit"]');
      if (inner) return inner;
    }
  } catch (e) {}
  return el;
}
function looksSubmit(el, t) {
  const id = (el.id || el.getAttribute('id') || '').toLowerCase();
  const testid = (el.getAttribute('data-testid') || '').toLowerCase();
  const tag = (el.tagName || '').toLowerCase();
  if (t.includes('create a post') || t.includes('crosspost') || t.includes('save draft')) return false;
  if (id === 'submit-post-button' || tag === 'r-post-form-submit-button') return true;
  if (testid.includes('submit-post') || testid === 'post') return true;
  if (t === 'post' || t === 'post now' || t === 'submit post') return true;
  if (t.startsWith('post to') || t.startsWith('post ')) return true;
  if (/\bpost\b/.test(t) && t.length < 40 && !t.includes('popular') && !t.includes('search')) return true;
  return false;
}
function clickEl(el) {
  const target = innerClickable(el);
  try { target.scrollIntoView({block:'center', inline:'nearest'}); } catch (e) {}
  try { target.click(); return true; } catch (e) {}
  try { el.click(); return true; } catch (e) {}
  return false;
}

const hosts = [];
walk(document, root => {
  try {
    root.querySelectorAll(
      '#submit-post-button, r-post-form-submit-button, [id="submit-post-button"]'
    ).forEach(el => hosts.push(el));
  } catch (e) {}
});
for (const host of hosts) {
  if (clickEl(host)) {
    return 'submitted:' + (labelOf(host) || 'submit-post-button').slice(0, 40);
  }
}

const buttons = [];
walk(document, root => {
  try {
    root.querySelectorAll(
      'button, [role="button"], input[type="submit"], faceplate-button, r-post-form-submit-button'
    ).forEach(el => buttons.push(el));
  } catch (e) {}
});
const preferred = [];
const fallback = [];
const disabledHits = [];
const nextHits = [];
for (const el of buttons) {
  const t = labelOf(el);
  if (isTab(el) || inChrome(el)) continue;
  if (t.includes('create a post') || t.includes('popular') || t.includes('search') || t.includes('crosspost')) continue;
  const typeSubmit = ((el.getAttribute('type') || el.type || '') + '').toLowerCase() === 'submit';
  const match = looksSubmit(el, t);
  const isNext = t === 'next' || t === 'continue';
  if (!match && !typeSubmit && !isNext) continue;
  if (!visible(el, true) && (el.tagName || '').toLowerCase() !== 'r-post-form-submit-button') continue;
  const enabled = visible(el, false) || (el.tagName || '').toLowerCase() === 'r-post-form-submit-button';
  if (isNext && !match) {
    if (enabled) nextHits.push(el);
    continue;
  }
  if (match || typeSubmit) {
    (enabled ? preferred : disabledHits).push(el);
  } else if (enabled) {
    fallback.push(el);
  }
}
for (const el of preferred.concat(fallback)) {
  if (clickEl(el)) {
    return 'submitted:' + (labelOf(el) || 'post').slice(0, 40);
  }
}
if (disabledHits.length) return 'post-disabled:' + disabledHits.length;
for (const el of nextHits) {
  if (clickEl(el)) return 'clicked-next';
}
const sample = buttons.slice(0, 12).map(el => labelOf(el).slice(0, 24)).filter(Boolean).slice(0, 6).join('|');
return 'no-submit:' + buttons.length + (sample ? ':' + sample : '');
"""

_FIND_SUBMIT_TITLE_JS = r"""
function walk(root, fn) {
  fn(root);
  let nodes;
  try { nodes = root.querySelectorAll('*'); } catch (e) { return; }
  nodes.forEach(el => { if (el.shadowRoot) walk(el.shadowRoot, fn); });
}
function visible(el) {
  try {
    const r = el.getBoundingClientRect();
    const st = window.getComputedStyle(el);
    return r.width > 8 && r.height > 8 && st.visibility !== 'hidden' && st.display !== 'none';
  } catch (e) { return false; }
}
function hint(el) {
  return ((el.getAttribute('placeholder') || '') + ' ' + (el.getAttribute('aria-label') || '') + ' ' +
          (el.getAttribute('name') || '') + ' ' + (el.getAttribute('id') || '')).toLowerCase();
}
const fields = [];
walk(document, root => {
  try { root.querySelectorAll('textarea, input[type="text"], [contenteditable="true"], faceplate-textarea').forEach(el => fields.push(el)); } catch (e) {}
});
for (const el of fields) {
  if (!visible(el)) continue;
  const t = hint(el);
  if (t.includes('search') || t.includes('community')) continue;
  if (t.includes('title')) {
    const tag = (el.tagName || '').toLowerCase();
    if (tag === 'faceplate-textarea' && el.shadowRoot) {
      const inner = el.shadowRoot.querySelector('textarea, input, [contenteditable="true"]');
      if (inner) return inner;
    }
    return el;
  }
}
for (const el of fields) {
  if (visible(el) && !hint(el).includes('search')) return el;
}
return null;
"""


_READ_SUBMIT_TITLE_JS = r"""
function walk(root, fn) {
  fn(root);
  let nodes;
  try { nodes = root.querySelectorAll('*'); } catch (e) { return; }
  nodes.forEach(el => { if (el.shadowRoot) walk(el.shadowRoot, fn); });
}
function visible(el) {
  try {
    const r = el.getBoundingClientRect();
    const st = window.getComputedStyle(el);
    return r.width > 8 && r.height > 8 && st.visibility !== 'hidden' && st.display !== 'none';
  } catch (e) { return false; }
}
function hint(el) {
  return ((el.getAttribute('placeholder') || '') + ' ' + (el.getAttribute('aria-label') || '') + ' ' +
          (el.getAttribute('name') || '') + ' ' + (el.getAttribute('id') || '')).toLowerCase();
}
function textOf(el) {
  try {
    if ('value' in el && el.value) return String(el.value);
  } catch (e) {}
  try { return String(el.innerText || el.textContent || ''); } catch (e) { return ''; }
}
const fields = [];
walk(document, root => {
  try { root.querySelectorAll('textarea, input[type="text"], [contenteditable="true"], faceplate-textarea').forEach(el => fields.push(el)); } catch (e) {}
});
for (const el of fields) {
  if (!visible(el)) continue;
  const t = hint(el);
  if (t.includes('search') || t.includes('community') || t.includes('body')) continue;
  if (t.includes('title')) return textOf(el);
}
return '';
"""


_REPLACE_SUBMIT_TITLE_JS = r"""
const title = arguments[0] || '';
function walk(root, fn) {
  fn(root);
  let nodes;
  try { nodes = root.querySelectorAll('*'); } catch (e) { return; }
  nodes.forEach(el => { if (el.shadowRoot) walk(el.shadowRoot, fn); });
}
function visible(el) {
  try {
    const r = el.getBoundingClientRect();
    const st = window.getComputedStyle(el);
    return r.width > 8 && r.height > 8 && st.visibility !== 'hidden' && st.display !== 'none';
  } catch (e) { return false; }
}
function hint(el) {
  return ((el.getAttribute('placeholder') || '') + ' ' + (el.getAttribute('aria-label') || '') + ' ' +
          (el.getAttribute('name') || '') + ' ' + (el.getAttribute('id') || '')).toLowerCase();
}
function inner(el) {
  const tag = (el.tagName || '').toLowerCase();
  if (tag === 'faceplate-textarea' || tag.includes('composer')) {
    try {
      const found = (el.shadowRoot && el.shadowRoot.querySelector('textarea, input, [contenteditable="true"]'))
        || el.querySelector('textarea, input, [contenteditable="true"]');
      if (found) return found;
    } catch (e) {}
  }
  return el;
}
function setOnce(el, value) {
  el = inner(el);
  try { el.scrollIntoView({block:'center'}); } catch (e) {}
  try { el.focus(); el.click(); } catch (e) {}
  try { document.execCommand('selectAll', false, null); } catch (e) {}
  try { document.execCommand('delete', false, null); } catch (e) {}
  const isEdit = el.isContentEditable || el.getAttribute('contenteditable') === 'true';
  if (isEdit) {
    el.innerText = value;
  } else if ('value' in el) {
    const proto = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value')
      || Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value');
    if (proto && proto.set) proto.set.call(el, value);
    else el.value = value;
  } else {
    el.innerText = value;
  }
  el.dispatchEvent(new Event('input', {bubbles: true, composed: true}));
  el.dispatchEvent(new Event('change', {bubbles: true, composed: true}));
}
const fields = [];
walk(document, root => {
  try { root.querySelectorAll('textarea, input[type="text"], [contenteditable="true"], faceplate-textarea').forEach(el => fields.push(el)); } catch (e) {}
});
for (const el of fields) {
  if (!visible(el)) continue;
  const t = hint(el);
  if (t.includes('search') || t.includes('community') || t.includes('body')) continue;
  if (t.includes('title')) {
    setOnce(el, title);
    return 'set';
  }
}
return 'miss';
"""


_FOCUS_SUBMIT_BODY_JS = r"""
function walk(root, fn) {
  fn(root);
  let nodes;
  try { nodes = root.querySelectorAll('*'); } catch (e) { return; }
  nodes.forEach(el => { if (el.shadowRoot) walk(el.shadowRoot, fn); });
}
function visible(el) {
  try {
    const r = el.getBoundingClientRect();
    const st = window.getComputedStyle(el);
    return r.width > 8 && r.height > 8 && st.visibility !== 'hidden' && st.display !== 'none';
  } catch (e) { return false; }
}
function hint(el) {
  return ((el.getAttribute('placeholder') || '') + ' ' + (el.getAttribute('aria-label') || '') + ' ' +
          (el.getAttribute('name') || '') + ' ' + (el.getAttribute('id') || '') + ' ' +
          (el.getAttribute('data-testid') || '')).toLowerCase();
}
function inner(el) {
  const tag = (el.tagName || '').toLowerCase();
  if (tag === 'faceplate-textarea' || tag.includes('composer') || tag.includes('textarea')) {
    try {
      const found = (el.shadowRoot && el.shadowRoot.querySelector('textarea, [contenteditable="true"]'))
        || el.querySelector('textarea, [contenteditable="true"]');
      if (found) return found;
    } catch (e) {}
  }
  return el;
}
const fields = [];
walk(document, root => {
  try { root.querySelectorAll('textarea, [contenteditable="true"], faceplate-textarea').forEach(el => fields.push(el)); } catch (e) {}
});
let titleEl = null;
let bodyEl = null;
for (const el of fields) {
  if (!visible(el)) continue;
  const t = hint(el);
  if (t.includes('search') || t.includes('community')) continue;
  if (!titleEl && t.includes('title')) titleEl = el;
  else if (!bodyEl && (t.includes('body') || t.includes('text') || t.includes('markdown') || (el.tagName || '').toLowerCase() === 'textarea')) bodyEl = el;
}
if (!bodyEl) {
  for (const el of fields) {
    if (el === titleEl || !visible(el)) continue;
    const t = hint(el);
    if (t.includes('search') || t.includes('community') || t.includes('title')) continue;
    bodyEl = el; break;
  }
}
if (!bodyEl) return 'no-body';
const target = inner(bodyEl);
try { target.scrollIntoView({block:'center'}); } catch (e) {}
try { target.focus(); } catch (e) {}
try { target.click(); } catch (e) {}
return 'focused';
"""


_FETCH_FLAIRS_API_JS = r"""
const sub = arguments[0];
const done = arguments[arguments.length - 1];
const paths = [
  '/r/' + sub + '/api/link_flair_v2.json',
  '/r/' + sub + '/api/link_flair.json'
];
(async () => {
  for (const path of paths) {
    try {
      const res = await fetch(path, {credentials: 'include'});
      if (!res.ok) continue;
      const data = await res.json();
      const rows = Array.isArray(data) ? data : (data.choices || data.templates || []);
      const names = [];
      for (const row of rows || []) {
        const t = String((row && (row.text || row.flair_text || row.name)) || '').replace(/\s+/g, ' ').trim();
        if (t && !names.includes(t)) names.push(t);
      }
      if (names.length) { done(names); return; }
    } catch (e) {}
  }
  done([]);
})();
"""

_FLAIR_UI_JS = r"""
const action = arguments[0];
const wanted = String(arguments[1] || '').replace(/\s+/g, ' ').trim().toLowerCase();
function walk(root, fn) {
  fn(root);
  let nodes;
  try { nodes = root.querySelectorAll('*'); } catch (e) { return; }
  nodes.forEach(el => { if (el.shadowRoot) walk(el.shadowRoot, fn); });
}
function visible(el) {
  try {
    const r = el.getBoundingClientRect();
    const st = window.getComputedStyle(el);
    return r.width > 4 && r.height > 4 && st.visibility !== 'hidden' && st.display !== 'none';
  } catch (e) { return false; }
}
function labelOf(el) {
  const parts = [
    el.innerText, el.textContent,
    el.getAttribute('aria-label'), el.getAttribute('name'),
    el.getAttribute('id'), el.getAttribute('data-testid'),
    el.getAttribute('slot'), el.tagName
  ];
  try { if (el.shadowRoot) parts.push(el.shadowRoot.textContent); } catch (e) {}
  return parts.filter(Boolean).join(' ').replace(/\s+/g, ' ').trim();
}
function innerClickable(el) {
  try {
    if (el.shadowRoot) {
      const inner = el.shadowRoot.querySelector('button, [role="button"], [role="radio"], [role="option"]');
      if (inner) return inner;
    }
  } catch (e) {}
  return el;
}
function clickEl(el) {
  const target = innerClickable(el);
  try { target.scrollIntoView({block:'center', inline:'nearest'}); } catch (e) {}
  try { target.click(); return true; } catch (e) {}
  try { el.click(); return true; } catch (e) {}
  return false;
}
function allClickables() {
  const out = [];
  walk(document, root => {
    try {
      root.querySelectorAll(
        'button, [role="button"], [role="radio"], [role="option"], [role="menuitem"], ' +
        'li, faceplate-radio-input, faceplate-tracker, faceplate-button, shreddit-button, ' +
        'r-post-form-flair, input[type="submit"], input[type="button"], [type="submit"], ' +
        '[data-testid*="flair"], [id*="flair"], .flairselect, .flairselector .flair, ' +
        '.flairselector li, .flair-choice'
      ).forEach(el => out.push(el));
    } catch (e) {}
  });
  return out;
}
function inChrome(el) {
  try { return !!(el.closest('nav, header, aside')); } catch (e) { return false; }
}
function compactText(el) {
  let t = '';
  try { t = String(el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim(); } catch (e) {}
  if (!t) {
    t = String(el.getAttribute('aria-label') || el.getAttribute('value') || el.getAttribute('name') || '')
      .replace(/\s+/g, ' ').trim();
  }
  try {
    if (!t && el.shadowRoot) {
      t = String(el.shadowRoot.textContent || '').replace(/\s+/g, ' ').trim();
    }
  } catch (e) {}
  return t;
}
function isOpenTrigger(el) {
  const low = labelOf(el).toLowerCase();
  const cls = String(el.className || '').toLowerCase();
  const id = (el.id || el.getAttribute('id') || '').toLowerCase();
  if (low.includes('user flair')) return false;
  if (low.includes('create a post') || low.includes('crosspost')) return false;
  if (cls.includes('flairselect') || id.includes('flair-select') || id === 'flair_select') return true;
  if (!low) return false;
  return (
    low.includes('add tags and flair') || low.includes('add tags') ||
    low.includes('add flair') || low.includes('select flair') ||
    low.includes('choose flair') || low.includes('add a flair') ||
    low.includes('post flair') || low === 'flair' || low === 'tags' ||
    low === 'add tag' || low.startsWith('flair')
  );
}
function isAddButton(el) {
  const t = compactText(el).toLowerCase();
  const aria = String(el.getAttribute('aria-label') || '').toLowerCase().replace(/\s+/g, ' ').trim();
  const testid = String(el.getAttribute('data-testid') || el.getAttribute('name') || el.id || '').toLowerCase();
  if (t.includes('add tags and flair')) return false;
  if (t === 'cancel' || t === 'close' || t === 'post' || aria === 'cancel') return false;
  if (t === 'add' || t === 'apply' || t === 'save' || t === 'done' ||
      t === 'select' || t === 'confirm' || t === 'ok') return true;
  if (t === 'add flair' || t === 'add tag' || t === 'add tags' || t === 'add selected') return true;
  if (aria === 'add' || aria === 'apply' || aria === 'save' || aria === 'done') return true;
  if (testid.includes('add-flair') || testid.includes('flair-add') ||
      testid.includes('apply-flair') || testid.includes('confirm-flair')) return true;
  if (t.startsWith('add ') && t.length < 16 && !t.includes('and flair')) return true;
  return false;
}
function isApply(t) {
  const low = String(t || '').replace(/\s+/g, ' ').trim().toLowerCase();
  if (!low || low.includes('add tags and flair')) return false;
  return low === 'apply' || low === 'save' || low === 'done' ||
    low === 'select' || low === 'confirm' || low === 'add' || low === 'ok' ||
    low === 'add flair' || low === 'add tag' || low === 'add tags';
}
function inPicker(el) {
  try {
    return !!(el.closest(
      '[role="dialog"], [role="menu"], [role="listbox"], [role="radiogroup"], ' +
      'faceplate-dropdown-menu, faceplate-menu, r-post-form-flair, .flairselector, ' +
      '[data-testid*="flair"]'
    ));
  } catch (e) { return false; }
}
function collectOptions() {
  const names = [];
  const seen = new Set();
  for (const el of allClickables()) {
    if (!visible(el) && (el.tagName || '').toLowerCase().indexOf('flair') < 0) continue;
    if (inChrome(el)) continue;
    const t = labelOf(el).replace(/\s+/g, ' ').trim();
    if (!t || t.length > 80) continue;
    const low = t.toLowerCase();
    if (isOpenTrigger(el) || isApply(low) || isAddButton(el) || low === 'post' || low === 'cancel' || low === 'close') continue;
    if (!inPicker(el) && !low.includes('flair')) {
      const role = (el.getAttribute('role') || '').toLowerCase();
      if (!['radio', 'option', 'menuitem'].includes(role)) continue;
    }
    if (seen.has(low)) continue;
    seen.add(low);
    names.push(t);
  }
  return names;
}
function currentFlair() {
  const bits = [];
  walk(document, root => {
    try {
      root.querySelectorAll(
        '[data-testid*="flair"], r-post-form-flair, [aria-label*="flair" i], .flair'
      ).forEach(el => bits.push(labelOf(el)));
    } catch (e) {}
  });
  const text = bits.join(' ').replace(/\s+/g, ' ').trim();
  return text.slice(0, 80);
}

if (action === 'open') {
  let opened = false;
  for (const el of allClickables()) {
    if (!visible(el) && (el.tagName || '').toLowerCase().indexOf('flair') < 0) continue;
    if (inChrome(el)) continue;
    if (isOpenTrigger(el)) {
      if (clickEl(el)) opened = true;
    }
  }
  return opened ? 'opened' : 'no-picker';
}

if (action === 'list') {
  return collectOptions();
}

if (action === 'current') {
  return currentFlair();
}

if (action === 'pick') {
  if (!wanted) return 'no-wanted';
  let clicked = false;
  for (const el of allClickables()) {
    if (inChrome(el)) continue;
    if (isAddButton(el)) continue;
    const t = labelOf(el).replace(/\s+/g, ' ').trim().toLowerCase();
    if (!t) continue;
    if (isOpenTrigger(el) || isApply(t) || t === 'post' || t === 'cancel') continue;
    if (t === wanted || t.includes(wanted) || wanted.includes(t)) {
      if (clickEl(el)) { clicked = true; break; }
    }
  }
  if (!clicked) return 'no-match';
  return 'picked:' + wanted;
}

if (action === 'add') {
  const hits = [];
  for (const el of allClickables()) {
    if (!visible(el) || inChrome(el)) continue;
    if (!isAddButton(el)) continue;
    hits.push(el);
  }
  hits.sort((a, b) => Number(inPicker(b)) - Number(inPicker(a)));
  for (const el of hits) {
    if (clickEl(el)) return 'added:' + compactText(el).slice(0, 40);
  }
  return 'no-add';
}

return 'unknown';
"""

_FLAIR_INTENT = (
    (re.compile(r"\bserious\b"), ("serious",)),
    (
        re.compile(
            r"[?]|\b(what|what's|whats|why|how|who|when|where|which|does|did|is it|"
            r"anyone|anybody|can you|would you|am i)\b",
            re.I,
        ),
        ("question", "ask"),
    ),
    (
        re.compile(r"\b(discuss|discussion|thoughts|opinion|what do you think|unpopular)\b", re.I),
        ("discuss",),
    ),
    (
        re.compile(r"\b(advice|should i|help me|what should|need help|recommend)\b", re.I),
        ("advice", "help"),
    ),
    (re.compile(r"\b(rant|vent|angry|fed up)\b", re.I), ("rant", "vent")),
    (re.compile(r"\b(news|breaking|update)\b", re.I), ("news",)),
    (re.compile(r"\b(humour|humor|funny|joke|meme)\b", re.I), ("humour", "humor", "funny", "joke")),
    (re.compile(r"\b(politic|government|election|labour|tory)\b", re.I), ("politic",)),
    (re.compile(r"\b(meta|mod|rule)\b", re.I), ("meta",)),
)
_SKIP_FLAIRS = ("mod only", "moderator", "removed", "approved", "locked")
_NSFW_FLAIRS = ("nsfw", "18+", "nsfw+")


def choose_post_flair(
    title: str,
    body: str,
    options: List[str],
    preferred: str = "",
) -> str:
    """Pick the community flair that best matches the post text."""
    names = []
    seen = set()
    for raw in options:
        name = re.sub(r"\s+", " ", str(raw or "")).strip()
        key = name.lower()
        if not name or key in seen:
            continue
        seen.add(key)
        names.append(name)
    if not names:
        return ""
    want = re.sub(r"\s+", " ", (preferred or "")).strip().lower()
    if want:
        for name in names:
            if name.lower() == want:
                return name
        for name in names:
            if want in name.lower() or name.lower() in want:
                return name
    text = f"{title or ''} {body or ''}"
    nsfw = bool(re.search(r"\b(nsfw|18\+)\b", text, re.I))
    intents: List[str] = []
    for pattern, keys in _FLAIR_INTENT:
        if pattern.search(text):
            intents.extend(keys)
    scored: List[Tuple[float, str]] = []
    for name in names:
        low = name.lower()
        if any(skip in low for skip in _SKIP_FLAIRS):
            continue
        if any(tag in low for tag in _NSFW_FLAIRS) and not nsfw:
            continue
        score = 0.0
        for key in intents:
            if key in low:
                score += 40.0
        tokens = [part for part in re.split(r"[^a-z0-9]+", low) if len(part) > 2]
        blob = text.lower()
        score += sum(2.0 for part in tokens if part in blob)
        if "question" in low:
            score += 6.0 if "?" in text else 2.0
        if "discuss" in low:
            score += 3.0
        if low in {"other", "general", "misc", "none"}:
            score += 0.5
        scored.append((score, name))
    if not scored:
        return names[0]
    scored.sort(key=lambda item: (-item[0], item[1].lower()))
    return scored[0][1]


def _flair_name_list(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def apply_post_flair(
    driver: WebDriver,
    label: str,
    subreddit: str,
    title: str,
    body: str,
    preferred: str = "",
) -> str:
    """Open the composer flair/tag picker and choose one that fits the post."""
    api_names: List[str] = []
    try:
        driver.set_script_timeout(12)
        api_names = _flair_name_list(
            driver.execute_async_script(_FETCH_FLAIRS_API_JS, subreddit)
        )
    except Exception:
        api_names = []

    opened = ""
    try:
        opened = str(driver.execute_script(_FLAIR_UI_JS, "open", "") or "")
    except Exception as exc:
        opened = f"open-failed:{brief_error(exc)}"
    time.sleep(_rng().uniform(0.6, 1.1))

    ui_names: List[str] = []
    try:
        ui_names = _flair_name_list(driver.execute_script(_FLAIR_UI_JS, "list", ""))
    except Exception:
        ui_names = []

    names = ui_names or api_names
    if not names:
        if opened == "no-picker" and not api_names:
            log(f"[Profile {label}] No flair/tag picker on r/{subreddit}")
            return ""
        log(f"[Profile {label}] Flair picker opened but no tags were listed")
        return ""

    picked = choose_post_flair(title, body, names, preferred=preferred)
    if not picked:
        return ""
    log(
        f"[Profile {label}] Adding flair/tag for this post: {picked} "
        f"(from {len(names)} on r/{subreddit})"
    )
    result = ""
    try:
        result = str(driver.execute_script(_FLAIR_UI_JS, "pick", picked) or "")
    except Exception as exc:
        result = f"pick-failed:{brief_error(exc)}"
    if not str(result).startswith("picked"):
        result = _click_old_reddit_flair(driver, picked) or result
    time.sleep(_rng().uniform(0.35, 0.7))
    if not str(result).startswith("picked"):
        log(f"[Profile {label}] Could not click flair {picked} ({result})")
        return ""
    added = _click_flair_add_button(driver, label)
    if not added:
        time.sleep(_rng().uniform(0.35, 0.7))
        added = _click_flair_add_button(driver, label)
    time.sleep(_rng().uniform(0.4, 0.8))
    return picked


def _click_flair_add_button(driver: WebDriver, label: str) -> bool:
    """Click Add / Apply on the flair-tag picker after a tag is selected."""
    result = ""
    try:
        result = str(driver.execute_script(_FLAIR_UI_JS, "add", "") or "")
    except Exception as exc:
        result = f"add-failed:{brief_error(exc)}"
    if str(result).startswith("added"):
        name = str(result).split(":", 1)[-1].strip() or "Add"
        log(f"[Profile {label}] Clicked {name} on the flair/tag picker")
        return True
    try:
        for btn in driver.find_elements(
            By.CSS_SELECTOR,
            "button, [role='button'], faceplate-button, input[type='submit'], "
            "input[type='button'], .flairselector button, .flairselector .select",
        ):
            text = re.sub(
                r"\s+",
                " ",
                (btn.text or btn.get_attribute("aria-label") or btn.get_attribute("value") or ""),
            ).strip().lower()
            if text in {
                "add",
                "apply",
                "save",
                "done",
                "select",
                "confirm",
                "ok",
                "add flair",
                "add tag",
                "add tags",
            }:
                try:
                    btn.click()
                except Exception:
                    driver.execute_script("arguments[0].click();", btn)
                log(f"[Profile {label}] Clicked {text} on the flair/tag picker")
                return True
    except Exception:
        pass
    if result:
        log(f"[Profile {label}] Flair Add button not clicked ({result})")
    return False


def _click_old_reddit_flair(driver: WebDriver, wanted: str) -> str:
    want = (wanted or "").strip().lower()
    if not want:
        return ""
    href = _current_href(driver).lower()
    if "old.reddit.com" not in href:
        return ""
    try:
        openers = driver.find_elements(
            By.CSS_SELECTOR, ".flairselect, button.flairselect, #flair-selection"
        )
        for opener in openers:
            try:
                driver.execute_script(
                    "arguments[0].scrollIntoView({block:'center'}); arguments[0].click();",
                    opener,
                )
                time.sleep(0.4)
                break
            except Exception:
                continue
        choices = driver.find_elements(
            By.CSS_SELECTOR, ".flairselector .flair, .flairselector li, .flair-choice"
        )
        for choice in choices:
            text = re.sub(r"\s+", " ", (choice.text or "")).strip().lower()
            if text == want or want in text or text in want:
                driver.execute_script(
                    "arguments[0].scrollIntoView({block:'center'}); arguments[0].click();",
                    choice,
                )
                time.sleep(0.3)
                for apply_btn in driver.find_elements(
                    By.CSS_SELECTOR,
                    ".flairselector .select, .flairselector button, .flairselector input[type='button']",
                ):
                    label = (apply_btn.get_attribute("value") or apply_btn.text or "").lower()
                    if label in {"select", "save", "apply", "ok", "add", "done", "add flair"}:
                        try:
                            apply_btn.click()
                        except Exception:
                            pass
                        break
                return "picked:" + wanted
    except Exception:
        return ""
    return ""


def _submit_page_error(driver: WebDriver) -> str:
    try:
        text = (
            driver.execute_script(
                "return (document.body && document.body.innerText || '').slice(0, 5000).toLowerCase();"
            )
            or ""
        )
    except Exception:
        return ""
    checks = (
        ("you are doing that too much", "rate limited"),
        ("something went wrong", "reddit error"),
        ("this community doesn't allow", "community does not allow this post type"),
        ("not allowed to post", "not allowed to post here"),
        ("must be a member", "must join the community first"),
        ("verify your email", "email verification required"),
        ("complete a captcha", "captcha required"),
        ("not enough karma", "account does not have enough karma"),
        ("account is too new", "account is too new to post here"),
        ("you can't post", "this account cannot post here"),
        ("assign a flair", "post flair required"),
        ("select a flair", "post flair required"),
        ("choose a flair", "post flair required"),
        ("add a flair", "post flair required"),
        ("flair is required", "post flair required"),
        ("must select a flair", "post flair required"),
    )
    for needle, message in checks:
        if needle in text:
            return message
    return ""


def _submit_looks_successful(driver: WebDriver, subreddit: str) -> bool:
    try:
        href = (driver.current_url or "").lower()
    except Exception:
        return False
    if "submit" in href:
        return False
    if "/comments/" in href or "/r/" + subreddit.lower() + "/" in href:
        return True
    return False


def _wait_for_submit_result(driver: WebDriver, subreddit: str, seconds: float = 12.0) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if _submit_looks_successful(driver, subreddit):
            return True
        time.sleep(0.8)
    return False


def _current_href(driver: WebDriver) -> str:
    try:
        return driver.current_url or ""
    except Exception:
        return ""


def _submit_via_old_reddit(
    driver: WebDriver,
    label: str,
    subreddit: str,
    title: str,
    body: str,
    preferred_flair: str = "",
) -> bool:
    url = f"https://old.reddit.com/r/{subreddit}/submit"
    log(f"[Profile {label}] Opening old Reddit submit for r/{subreddit}")
    navigate(driver, url, label)
    time.sleep(_rng().uniform(2.0, 3.2))
    dismiss_popups(driver)
    href = _current_href(driver).lower()
    if "old.reddit.com" not in href:
        log(f"[Profile {label}] Old Reddit redirected to new submit ({href[:90]})")
        return False
    try:
        kind = driver.find_elements(By.CSS_SELECTOR, "input[name='kind'][value='self'], a[href='#self']")
        if kind:
            human_click(driver, kind[0])
            time.sleep(0.4)
        title_el = driver.find_elements(By.CSS_SELECTOR, "textarea[name='title'], input[name='title'], #title-field")
        if not title_el:
            return False
        human_click(driver, title_el[0])
        time.sleep(0.2)
        try:
            title_el[0].clear()
        except Exception:
            pass
        _type_composer_human(driver, str(title or "")[:300])
        body_el = driver.find_elements(By.CSS_SELECTOR, "textarea[name='text'], #text-field")
        if body and body_el:
            human_click(driver, body_el[0])
            time.sleep(0.2)
            try:
                body_el[0].clear()
            except Exception:
                pass
            _type_composer_human(driver, body)
        time.sleep(_rng().uniform(0.6, 1.2))
        apply_post_flair(driver, label, subreddit, title, body, preferred=preferred_flair)
        time.sleep(_rng().uniform(0.4, 0.8))
        submit_btns = driver.find_elements(
            By.CSS_SELECTOR,
            "button[name='submit'], button[type='submit'], .submit-btn, #submit-btn",
        )
        clicked = False
        for btn in submit_btns:
            try:
                driver.execute_script(
                    "arguments[0].scrollIntoView({block:'center'}); arguments[0].click();",
                    btn,
                )
                clicked = True
                break
            except Exception:
                if human_click(driver, btn):
                    clicked = True
                    break
        if not clicked:
            return False
        if _wait_for_submit_result(driver, subreddit, 10):
            return True
        err = _submit_page_error(driver)
        if err:
            log(f"[Profile {label}] Old Reddit submit blocked: {err}")
        return False
    except Exception as exc:
        log(f"[Profile {label}] Old Reddit submit failed ({brief_error(exc)})")
        return False


def submitted_post_url(driver: WebDriver) -> str:
    try:
        href = (driver.current_url or "").strip()
    except Exception:
        return ""
    if not href or "submit" in href.lower():
        return ""
    return href.split("?")[0]


def _wait_for_submit_composer(driver: WebDriver, seconds: float = 16.0) -> bool:
    """New Reddit's submit form is shadow DOM and often missing after a load timeout."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            field = driver.execute_script(_FIND_SUBMIT_TITLE_JS)
        except Exception:
            field = None
        if field is not None:
            return True
        if _submit_page_error(driver):
            return False
        time.sleep(0.8)
    return False


def _normalize_submit_title(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _read_submit_title(driver: WebDriver) -> str:
    try:
        return _normalize_submit_title(driver.execute_script(_READ_SUBMIT_TITLE_JS) or "")
    except Exception:
        return ""


def _title_is_doubled(current: str, title: str) -> bool:
    """True when the composer already contains the title pasted twice."""
    have = _normalize_submit_title(current)
    want = _normalize_submit_title(title)
    if not have or not want or have == want:
        return False
    if have == want + want or have == f"{want} {want}":
        return True
    if have.startswith(want):
        rest = _normalize_submit_title(have[len(want) :])
        if rest == want:
            return True
    compact_have = re.sub(r"\s+", "", have).lower()
    compact_want = re.sub(r"\s+", "", want).lower()
    return compact_want and compact_have == compact_want * 2


def _replace_submit_title(driver: WebDriver, title: str) -> bool:
    want = str(title or "")[:300]
    try:
        result = str(driver.execute_script(_REPLACE_SUBMIT_TITLE_JS, want) or "")
    except Exception:
        result = ""
    return _read_submit_title(driver) == _normalize_submit_title(want) or result == "set"


def _clear_composer_field(driver: WebDriver) -> None:
    """Empty the focused title/body box so we never append on top of existing text."""
    try:
        ActionChains(driver).key_down(Keys.CONTROL).send_keys("a").key_up(Keys.CONTROL).perform()
        time.sleep(0.08)
        ActionChains(driver).send_keys(Keys.BACKSPACE).perform()
        time.sleep(0.08)
    except Exception:
        pass
    try:
        driver.execute_script(
            """
            const el = document.activeElement;
            if (!el) return;
            try { document.execCommand('selectAll', false, null); } catch (e) {}
            try { document.execCommand('delete', false, null); } catch (e) {}
            if (el.isContentEditable || el.getAttribute('contenteditable') === 'true') {
              el.innerText = '';
            } else if ('value' in el) {
              el.value = '';
            }
            el.dispatchEvent(new Event('input', {bubbles: true, composed: true}));
            """
        )
    except Exception:
        pass


def _type_composer_human(driver: WebDriver, text: str) -> None:
    """Type the post one key at a time. Line breaks are Enter, never a paste."""
    value = str(text or "")[:4000]
    style = _style()
    char_gap = style.type_char if style else (0.03, 0.11)
    space_gap = style.type_space if style else (0.05, 0.18)
    punct_gap = style.type_punct if style else (0.16, 0.48)
    nl_gap = style.type_nl if style else (0.18, 0.45)
    for index, char in enumerate(value):
        key: Any = Keys.ENTER if char in "\n\r" else char
        typed = False
        try:
            ActionChains(driver).send_keys(key).perform()
            typed = True
        except Exception:
            pass
        if not typed:
            try:
                driver.switch_to.active_element.send_keys(key)
                typed = True
            except Exception:
                pass
        if not typed and char not in "\n\r":
            try:
                driver.execute_cdp_cmd("Input.insertText", {"text": char})
            except Exception:
                continue
        if char in "\n\r":
            time.sleep(_rng().uniform(*nl_gap))
        elif char in ".,!?":
            time.sleep(_rng().uniform(*punct_gap))
        elif char == " ":
            time.sleep(_rng().uniform(*space_gap))
        else:
            time.sleep(_rng().uniform(*char_gap))
        if index > 0 and index % _rng().randint(18, 48) == 0:
            time.sleep(_rng().uniform(0.22, 0.95))
        if char.isalpha() and _rng().random() < _rng().uniform(0.008, 0.028):
            try:
                ActionChains(driver).send_keys(_rng().choice("aeiou")).perform()
                time.sleep(_rng().uniform(0.06, 0.16))
                ActionChains(driver).send_keys(Keys.BACKSPACE).perform()
                time.sleep(_rng().uniform(0.05, 0.12))
            except Exception:
                pass


def _focus_submit_title(driver: WebDriver) -> bool:
    field = None
    try:
        field = driver.execute_script(_FIND_SUBMIT_TITLE_JS)
    except Exception:
        field = None
    if field is None:
        return False
    try:
        human_click(driver, field)
        return True
    except Exception:
        try:
            driver.execute_script("arguments[0].focus(); arguments[0].click();", field)
            return True
        except Exception:
            return False


def _type_submit_title(driver: WebDriver, title: str) -> bool:
    """Clear the title box, then type it once like a person."""
    want = str(title or "")[:300]
    if not want:
        return False
    if not _focus_submit_title(driver):
        return False
    time.sleep(_rng().uniform(0.2, 0.45))
    _clear_composer_field(driver)
    _type_composer_human(driver, want)
    if _title_is_doubled(_read_submit_title(driver), want):
        _clear_composer_field(driver)
        _type_composer_human(driver, want)
    return bool(_normalize_submit_title(_read_submit_title(driver)))


def _fill_submit_body(driver: WebDriver, body: str) -> None:
    """Clear the body box, then type the template once like a person."""
    if not str(body or "").strip():
        return
    focused = ""
    try:
        focused = str(driver.execute_script(_FOCUS_SUBMIT_BODY_JS) or "")
    except Exception:
        focused = ""
    if focused != "focused":
        try:
            driver.switch_to.active_element.send_keys(Keys.TAB)
        except Exception:
            pass
    time.sleep(_rng().uniform(0.25, 0.55))
    _clear_composer_field(driver)
    _type_composer_human(driver, body)


def _is_composer_failure(reason: str) -> bool:
    """True when we never reached Reddit's Post button — do not burn retry accounts."""
    low = (reason or "").lower()
    tokens = (
        "no-title",
        "js-failed",
        "title-failed",
        "submit page did not load",
        "no-submit",
        "post-disabled",
        "click-failed",
        "composer missing",
        "post flair required",
    )
    return any(token in low for token in tokens)


def submit_text_post(
    driver: WebDriver,
    label: str,
    subreddit: str,
    title: str,
    body: str,
    preferred_flair: str = "",
) -> Tuple[str, str]:
    """Create one text post in r/{subreddit}. Returns (url, reason). url is empty on failure."""
    raise_profile_browser(label)
    page = f"https://www.reddit.com/r/{subreddit}/submit?type=TEXT"
    log(f"[Profile {label}] Opening submit page for r/{subreddit}")
    navigate(driver, page, label)
    time.sleep(_rng().uniform(2.2, 3.4))
    dismiss_popups(driver)
    try:
        WebDriverWait(driver, ELEMENT_WAIT).until(
            EC.presence_of_element_located((By.TAG_NAME, "body"))
        )
    except TimeoutException:
        log(f"[Profile {label}] Submit page did not load")
        return "", "submit page did not load"
    if not _wait_for_submit_composer(driver, 18):
        log(f"[Profile {label}] Title field not ready yet — waiting a bit longer")
        time.sleep(_rng().uniform(2.0, 3.5))
        dismiss_popups(driver)

    try:
        result = str(driver.execute_script(_SUBMIT_POST_JS, "", "") or "")
        log(f"[Profile {label}] Submit form: {result}")
    except Exception as exc:
        log(f"[Profile {label}] Submit script failed ({brief_error(exc)})")
        result = "js-failed"

    title_ok = _type_submit_title(driver, title)
    current_title = _read_submit_title(driver)
    if _title_is_doubled(current_title, title):
        log(f"[Profile {label}] Title was typed twice — clearing and typing it once")
        _focus_submit_title(driver)
        _clear_composer_field(driver)
        _type_composer_human(driver, str(title or "")[:300])
        current_title = _read_submit_title(driver)
    if title_ok or current_title:
        result = "filled"
        log(f"[Profile {label}] Typed the title once ({len(str(title or ''))} chars)")
    else:
        log(f"[Profile {label}] Could not type the title")

    if result == "filled":
        if body:
            log(
                f"[Profile {label}] Typing the post body once "
                f"({len(body)} chars, {body.count(chr(10)) + 1} lines)"
            )
            _fill_submit_body(driver, body)
        time.sleep(_rng().uniform(0.8, 1.3))
        apply_post_flair(
            driver, label, subreddit, title, body, preferred=preferred_flair
        )
    time.sleep(_rng().uniform(1.0, 1.6))

    click_result = ""
    if result == "filled":
        try:
            driver.switch_to.active_element.send_keys(Keys.CONTROL, Keys.ENTER)
        except Exception:
            pass
        for _attempt in range(10):
            try:
                click_result = str(driver.execute_script(_SUBMIT_CLICK_JS) or "")
            except Exception as exc:
                click_result = f"click-failed:{brief_error(exc)}"
            log(f"[Profile {label}] Submit click: {click_result}")
            if str(click_result).startswith("submitted"):
                break
            if str(click_result) == "clicked-next":
                log(f"[Profile {label}] Submit composer asked for Next — continuing")
                time.sleep(1.1)
                continue
            if _submit_looks_successful(driver, subreddit):
                click_result = "submitted:url"
                break
            time.sleep(1.0)
        result = click_result or result

    if _wait_for_submit_result(driver, subreddit, 14):
        url = submitted_post_url(driver)
        log(f"[Profile {label}] Posted to r/{subreddit}")
        return url or f"https://www.reddit.com/r/{subreddit}/", ""

    err = _submit_page_error(driver)
    href = _current_href(driver)
    if err:
        log(f"[Profile {label}] Submit blocked: {err} ({href[:90]})")
        return "", err
    if result != "filled":
        log(f"[Profile {label}] New Reddit composer missing — trying old.reddit.com")
        if _submit_via_old_reddit(
            driver, label, subreddit, title, body, preferred_flair=preferred_flair
        ):
            url = submitted_post_url(driver)
            log(f"[Profile {label}] Posted to r/{subreddit} via old Reddit")
            return url or f"https://www.reddit.com/r/{subreddit}/", ""
        old_err = _submit_page_error(driver)
        if old_err:
            log(f"[Profile {label}] Submit blocked: {old_err}")
            return "", old_err
    log(f"[Profile {label}] Could not confirm the post was submitted ({result}) ({href[:90]})")
    return "", str(result or "submit failed")


def _praw_read_only():
    client_id = os.environ.get("REDDIT_CLIENT_ID", "").strip()
    client_secret = os.environ.get("REDDIT_CLIENT_SECRET", "").strip()
    if not client_id:
        return None
    try:
        import praw
    except ImportError:
        return None
    return praw.Reddit(
        client_id=client_id,
        client_secret=client_secret or None,
        user_agent=os.environ.get("REDDIT_USER_AGENT", "reddit-joiner/1.0"),
        check_for_async=False,
    )


def _ids_from_reddit_url(url: str) -> Tuple[str, str]:
    path = urlparse((url or "").split("?")[0]).path
    parts = [part for part in path.split("/") if part]
    post_id = ""
    comment_id = ""
    if "comments" in parts:
        index = parts.index("comments")
        if len(parts) > index + 1:
            post_id = parts[index + 1]
        tail = [part for part in parts[index + 2 :] if part not in {"comment", "_"}]
        if tail:
            last = tail[-1].replace("t1_", "")
            if 5 <= len(last) <= 12 and re.fullmatch(r"[A-Za-z0-9]+", last):
                comment_id = last
    if not comment_id and "comment" in parts:
        index = parts.index("comment")
        if len(parts) > index + 1:
            last = parts[index + 1].replace("t1_", "")
            if 5 <= len(last) <= 12 and re.fullmatch(r"[A-Za-z0-9]+", last):
                comment_id = last
    return post_id, comment_id


_rl_check_blocked: Dict[str, int] = {}


def _note_rl_check_blocked(code: int, url: str) -> None:
    """Count outcome checks Reddit refused, so a blind pipeline is visible."""
    key = str(int(code))
    _rl_check_blocked[key] = _rl_check_blocked.get(key, 0) + 1
    total = sum(_rl_check_blocked.values())
    if total in {1, 5, 25, 100} or total % 250 == 0:
        spread = ", ".join(f"HTTP {k} x{v}" for k, v in sorted(_rl_check_blocked.items()))
        log(
            f"Deep RL outcome check blocked {total}x ({spread}) — these stay "
            f"unscored instead of counting as removals. Set REDDIT_CLIENT_ID / "
            f"REDDIT_CLIENT_SECRET for PRAW reads if this keeps happening."
        )


def rl_check_blocked_total() -> int:
    return sum(_rl_check_blocked.values())


def inspect_reddit_url_status(url: str) -> Optional[Dict[str, Any]]:
    """Return live/removed/filtered + score for a comment or post permalink."""
    href = (url or "").split("?")[0].strip()
    if not href or "reddit.com" not in href.lower():
        return None
    href = href.replace("old.reddit.com", "www.reddit.com")
    post_id, comment_id = _ids_from_reddit_url(href)
    reddit = _praw_read_only()
    if reddit is not None and comment_id:
        try:
            item = reddit.comment(id=comment_id)
            item.refresh()
            body = str(getattr(item, "body", "") or "")
            score = int(getattr(item, "score", 0) or 0)
            if body.strip() in {"[removed]", "[deleted]"} or getattr(item, "banned_by", None):
                return {"status": "removed", "score": score, "kind": "comment"}
            collapsed_reason = str(
                getattr(item, "collapsed_reason_code", None)
                or getattr(item, "collapsed_reason", None)
                or ""
            ).lower()
            if "filter" in collapsed_reason or (
                bool(getattr(item, "collapsed", False)) and score <= 1
            ):
                return {"status": "filtered", "score": score, "kind": "comment"}
            return {"status": "live", "score": score, "kind": "comment"}
        except Exception:
            # A PRAW 403/404 means "we could not read it" — blocked IP, rate
            # limit, bad auth or a malformed permalink. It is NOT evidence the
            # comment was removed. A real removal comes back 200 with the body
            # replaced by [removed], which is handled above. Fall through to the
            # JSON path and, failing that, report inconclusive.
            pass
    if reddit is not None and post_id and not comment_id:
        try:
            submission = reddit.submission(id=post_id)
            submission._fetch()
            score = int(getattr(submission, "score", 0) or 0)
            selftext = str(getattr(submission, "selftext", "") or "")
            if (
                getattr(submission, "removed_by_category", None)
                or getattr(submission, "banned_by", None)
                or selftext.strip() in {"[removed]", "[deleted]"}
            ):
                return {"status": "removed", "score": score, "kind": "post"}
            return {"status": "live", "score": score, "kind": "post"}
        except Exception:
            pass

    json_url = href
    if comment_id and post_id:
        json_url = f"https://www.reddit.com/comments/{post_id}/_/{comment_id}.json"
    elif href and not href.endswith(".json"):
        json_url = href + ".json"
    try:
        response = requests.get(
            json_url,
            headers={"User-Agent": os.environ.get("REDDIT_USER_AGENT", "reddit-joiner/1.0")},
            timeout=20,
        )
        if response.status_code != 200:
            # 403 = Reddit blocking this IP, 429 = rate limited, 404 = usually a
            # permalink we built wrong, 5xx = Reddit. None of these prove removal,
            # so stay inconclusive and let the row be retried later.
            _note_rl_check_blocked(response.status_code, json_url)
            return None
        payload = response.json()
        data: Dict[str, Any] = {}
        if isinstance(payload, list) and len(payload) >= 2 and comment_id:
            children = (((payload[1] or {}).get("data") or {}).get("children") or [])
            if children:
                data = children[0].get("data") or {}
        elif isinstance(payload, list) and payload:
            data = ((((payload[0] or {}).get("data") or {}).get("children") or [{}])[0].get("data") or {})
        elif isinstance(payload, dict):
            data = payload.get("data") or payload
        if not data:
            return None
        score = int(data.get("score") or 0)
        body = str(data.get("body") or data.get("selftext") or "")
        if (
            data.get("removed_by_category")
            or data.get("banned_by")
            or body.strip() in {"[removed]", "[deleted]"}
        ):
            return {"status": "removed", "score": score, "kind": "comment" if comment_id else "post"}
        collapsed = str(data.get("collapsed_reason_code") or data.get("collapsed_reason") or "").lower()
        if "filter" in collapsed or data.get("collapsed") and score <= 1:
            return {"status": "filtered", "score": score, "kind": "comment" if comment_id else "post"}
        if data.get("id") or body or data.get("title"):
            return {"status": "live", "score": score, "kind": "comment" if comment_id else "post"}
    except Exception:
        return None
    return None


def _comment_inspect_url(post_url: str, comment_id: str = "") -> str:
    cid = (comment_id or "").strip()
    post_id, existing = _ids_from_reddit_url(post_url or "")
    cid = cid or existing
    if cid and post_id:
        return f"https://www.reddit.com/comments/{post_id}/_/{cid}"
    if cid:
        return f"https://www.reddit.com/comment/{cid}"
    return (post_url or "").split("?")[0]


def enqueue_unscored_comments_for_rl() -> int:
    """Queue older comments that never got a delayed RL status check."""
    agent = _rl_agent()
    if agent is None:
        return 0
    try:
        from reddit_joiner.store import unscored_ai_comments
    except Exception:
        return 0
    try:
        already = agent.pending_urls()
        queued = 0
        for row in unscored_ai_comments(40):
            url = _comment_inspect_url(
                str(row.get("post_url") or ""),
                str(row.get("comment_id") or ""),
            )
            key = url.split("?")[0].rstrip("/").lower()
            if not key or "reddit.com" not in key or key in already:
                continue
            created = str(row.get("created_at") or "")
            age = 0.0
            if created:
                try:
                    age = (datetime.now() - datetime.fromisoformat(created)).total_seconds()
                except Exception:
                    age = float(RL_STATUS_MIN_AGE)
            if age < float(RL_STATUS_MIN_AGE):
                continue
            tone = str(row.get("tone") or "neutral").strip().lower() or "neutral"
            method = str(row.get("method") or "").strip().lower()
            kind = "sheet" if method == "sheet" else "browse"
            state = build_state_for_logged_comment(row, kind=kind)
            agent.queue_delayed(state, f"comment:{tone}", url, "comment")
            already.add(key)
            queued += 1
        return queued
    except Exception:
        return 0


def build_state_for_logged_comment(row: Dict[str, Any], kind: str = "browse") -> Dict[str, Any]:
    try:
        from reddit_joiner.rl import build_state
    except Exception:
        return {}
    return build_state(
        account_id=str(row.get("user_id") or row.get("account") or ""),
        target_subreddit=str(row.get("subreddit") or ""),
        post_sentiment=float(row.get("sentiment") or 0),
        post_length=len(str(row.get("post_body") or "")) + len(str(row.get("post_title") or "")),
        comment_length=len(str(row.get("comment") or "")),
        kind=kind,
    )


def apply_rl_status_rewards() -> int:
    agent = _rl_agent()
    if agent is None:
        return 0
    queued = enqueue_unscored_comments_for_rl()
    if queued:
        log(f"Deep RL queued {queued} earlier comment(s) for a live/removed/score check")
    applied = agent.apply_delayed_rewards(
        inspect_reddit_url_status,
        upvote_5=REWARD_COMMENT_UPVOTED_5,
        upvote_20=REWARD_COMMENT_UPVOTED_20,
        downvote=REWARD_COMMENT_DOWNVOTED,
        still_live=REWARD_COMMENT_STILL_LIVE,
        score_2=REWARD_COMMENT_SCORE_2,
        score_neg=REWARD_COMMENT_SCORE_NEG,
        removed=REWARD_COMMENT_REMOVED,
        filtered=REWARD_COMMENT_FILTERED,
        min_age_seconds=RL_STATUS_MIN_AGE,
        stale_seconds=RL_STATUS_STALE,
    )
    if not applied:
        return 0
    try:
        from reddit_joiner.store import update_ai_comment_outcome
    except Exception:
        update_ai_comment_outcome = None  # type: ignore[assignment]
    for item in getattr(agent, "last_status_updates", []) or []:
        status = str(item.get("status") or "unknown")
        score = int(item.get("score") or 0)
        reward = float(item.get("reward") or 0)
        url = str(item.get("url") or "")
        log(
            f"Deep RL status {status} score={score} reward={reward:+.1f} "
            f"{url[:90]}"
        )
        if update_ai_comment_outcome is not None:
            try:
                update_ai_comment_outcome(url, score=score, status=status)
            except Exception:
                pass
    return applied


def _praw_url_score(url: str) -> Optional[int]:
    reddit = _praw_read_only()
    href = (url or "").split("?")[0].strip()
    if reddit is None or not href or "reddit.com" not in href.lower():
        return None
    try:
        submission = reddit.submission(url=href.replace("old.reddit.com", "www.reddit.com"))
        submission._fetch()
        return int(getattr(submission, "score", 0) or 0)
    except Exception:
        return None


def check_post_live(driver: WebDriver, url: str, label: str = "") -> Tuple[str, str, int]:
    """
    Return (LIVE|REMOVED|UNKNOWN, reason, score).
    Prefers PRAW, then public JSON, then the open browser page.
    """
    prefix = f"[Profile {label}] " if label else ""
    href = (url or "").split("?")[0].rstrip("/")
    reddit = _praw_read_only()
    if reddit is not None and href:
        try:
            submission = reddit.submission(url=href.replace("old.reddit.com", "www.reddit.com"))
            submission._fetch()
            score = int(getattr(submission, "score", 0) or 0)
            removed = bool(getattr(submission, "removed_by_category", None))
            banned = getattr(submission, "banned_by", None)
            selftext = str(getattr(submission, "selftext", "") or "")
            approved = getattr(submission, "approved_by", None)
            if removed or banned or selftext.strip() in {"[removed]", "[deleted]"}:
                reason = str(getattr(submission, "removed_by_category", None) or banned or "removed")
                return "REMOVED", reason, score
            if approved:
                return "LIVE", "approved", score
            return "LIVE", "visible", score
        except Exception as exc:
            log(f"{prefix}PRAW status check failed ({brief_error(exc)})")

    json_url = href + ".json" if href and not href.endswith(".json") else href
    if json_url:
        try:
            response = requests.get(
                json_url.replace("old.reddit.com", "www.reddit.com"),
                headers={"User-Agent": os.environ.get("REDDIT_USER_AGENT", "reddit-joiner/1.0")},
                timeout=20,
            )
            if response.status_code != 200:
                # Reddit blocks or throttles anonymous JSON from server IPs.
                # That is not a removal verdict — fall through to the DOM check
                # below, which reads the page in the logged-in browser.
                raise RuntimeError(f"http {response.status_code}")
            payload = response.json()
            if not isinstance(payload, list) or not payload:
                raise RuntimeError("unexpected json")
            child = (((payload[0] or {}).get("data") or {}).get("children") or [{}])[0]
            data = child.get("data") or {}
            score = int(data.get("score") or 0)
            if data.get("removed_by_category") or data.get("banned_by") or str(data.get("selftext") or "") in {"[removed]", "[deleted]"}:
                return "REMOVED", str(data.get("removed_by_category") or "removed"), score
            if data.get("id"):
                return "LIVE", "visible", score
        except Exception as exc:
            log(f"{prefix}JSON status check failed ({brief_error(exc)})")

    if href:
        try:
            navigate(driver, href, label or "browser")
            time.sleep(2.0)
            text = (
                driver.execute_script(
                    "return (document.body && document.body.innerText || '').slice(0, 4000).toLowerCase();"
                )
                or ""
            )
            if "this post was removed" in text or "sorry, this post" in text or "[removed]" in text:
                return "REMOVED", "page says removed", 0
            if "/comments/" in (driver.current_url or "").lower():
                return "LIVE", "page visible", 0
        except Exception as exc:
            log(f"{prefix}Page status check failed ({brief_error(exc)})")
    return "UNKNOWN", "could not verify", 0


def post_candidate_pool(primary: str, extras: Optional[List[str]] = None) -> List[str]:
    """Only the subreddit(s) written on the posts.csv row — never other communities."""
    names: List[str] = []
    seen = set()
    for raw in [primary] + list(extras or []):
        name = normalize_subreddit(str(raw or ""))
        key = name.lower()
        if not name or key in seen or is_blocked_subreddit(name):
            continue
        seen.add(key)
        names.append(name)
    return names


def post_targets_for_row(primary: str, extras: Optional[List[str]] = None) -> List[str]:
    return post_candidate_pool(primary, extras)[:MAX_POST_ATTEMPTS]


def apply_account_analysis(stats: AccountSummary, info: Dict[str, Any]) -> None:
    stats.reddit_username = str(info.get("username") or "")
    stats.account_karma = int(info.get("karma") or 0)
    stats.account_age_days = float(info.get("age_days") or 0.0)
    try:
        from reddit_joiner.karma import account_tier

        stats.karma_tier = account_tier(stats.account_karma, stats.account_age_days)
    except Exception:
        stats.karma_tier = ""


def collect_community_topics(
    driver: WebDriver,
    label: str,
    subreddit: str,
    limit: int = 12,
) -> List[str]:
    """Read recent post titles so a general post can match this community.

    Only text posts count. These titles are the sample a general post is modelled
    on, and they also feed the post-fit gate, so counting link posts here would
    let a link or image community look like it welcomes text discussion.
    """
    titles: List[str] = []
    seen = set()
    for sort in ("hot", "new", "top"):
        try:
            posts, _used = fetch_listing_posts(driver, label, subreddit, sort, limit=8)
        except Exception:
            posts = []
        for row in posts:
            if not row.get("is_self") or row.get("post_hint"):
                continue
            title = re.sub(r"\s+", " ", str(row.get("title") or "")).strip()
            key = title.lower()
            if len(title) < 8 or key in seen:
                continue
            seen.add(key)
            titles.append(title)
            if len(titles) >= limit:
                return titles
    return titles


def collect_recent_discussion(
    driver: WebDriver,
    label: str,
    subreddit: str,
    limit: int = 8,
) -> List[str]:
    """Titles plus a scrap of body from New/Hot — the live conversation, not just names."""
    notes: List[str] = []
    seen = set()
    for sort in ("new", "hot"):
        try:
            posts, _used = fetch_listing_posts(driver, label, subreddit, sort, limit=12)
        except Exception:
            posts = []
        for row in posts:
            if not row.get("is_self") or row.get("post_hint"):
                continue
            title = re.sub(r"\s+", " ", str(row.get("title") or "")).strip()
            key = title.lower()
            if len(title) < 8 or key in seen:
                continue
            seen.add(key)
            body = re.sub(r"\s+", " ", str(row.get("body") or "")).strip()[:160]
            replies = int(row.get("num_comments") or 0)
            if body:
                notes.append(f"{title} — {body}")
            elif replies:
                notes.append(f"{title} ({replies} comments)")
            else:
                notes.append(title)
            if len(notes) >= limit:
                return notes
    return notes


def explore_subreddit_before_post(
    driver: WebDriver,
    label: str,
    subreddit: str,
    stats: AccountSummary,
) -> List[str]:
    """Scroll New, read a thread, then pull recent discussion. Posting comes after this."""
    name = normalize_subreddit(subreddit)
    if not name:
        return []
    feed = f"https://www.reddit.com/r/{name}/new/"
    log(
        f"[Profile {label}] Exploring r/{name} first — reading New, then a post "
        "from that discussion"
    )
    try:
        raise_profile_browser(label)
        navigate(driver, feed, label)
        dismiss_popups(driver)
    except Exception as exc:
        log(f"[Profile {label}] Could not open r/{name}/new ({brief_error(exc)})")
    leftover = deadline_remaining()
    dwell = _rng().uniform(16.0, 32.0)
    if leftover is not None:
        dwell = min(dwell, max(8.0, leftover * 0.22))
    try:
        _lurk_before_comment(driver, label, name, dwell)
    except Exception:
        time.sleep(min(8.0, dwell))
    if _rng().random() < 0.72:
        try:
            _peek_random_thread(driver, label, name, feed)
        except Exception:
            pass
        try:
            navigate(driver, feed, label)
            dismiss_popups(driver)
        except Exception:
            pass
    discussion = collect_recent_discussion(driver, label, name)
    titles: List[str] = []
    seen = set()
    for note in discussion:
        title = str(note).split(" — ", 1)[0].split(" (", 1)[0].strip()
        key = title.lower()
        if len(title) < 8 or key in seen:
            continue
        seen.add(key)
        titles.append(title)
    if titles:
        stats.subreddit_topics[name] = titles
    if discussion:
        log(
            f"[Profile {label}] Recent r/{name} discussion: "
            + "; ".join(note[:70] for note in discussion[:4])
        )
    else:
        log(f"[Profile {label}] No recent text discussion found on r/{name} New")
    return discussion


def _sample_community_posts(
    driver: WebDriver,
    label: str,
    subreddit: str,
) -> Tuple[List[str], int, int]:
    """Read the community's recent posts.

    Returns (topics, text_posts, total_posts). Unlike fetch_listing_posts this
    keeps media posts in the totals, because the whole point is to find out how
    much of the community is text and how much is images and links.
    """
    topics: List[str] = []
    seen = set()
    text_posts = 0
    total = 0
    for sort in ("new", "hot"):
        payload = reddit_session_json(driver, _listing_json_url(subreddit, sort), label)
        data = payload.get("data") if isinstance(payload, dict) else None
        children = data.get("children") if isinstance(data, dict) else None
        if not isinstance(children, list):
            continue
        for child in children:
            item = child.get("data") if isinstance(child, dict) else None
            if not isinstance(item, dict):
                continue
            if item.get("stickied") or item.get("pinned"):
                continue
            total += 1
            is_text = bool(item.get("is_self")) and not item.get("post_hint")
            if is_text:
                text_posts += 1
            title = re.sub(r"\s+", " ", str(item.get("title") or "")).strip()
            key = title.lower()
            if is_text and len(title) >= 8 and key not in seen:
                seen.add(key)
                topics.append(title)
            if total >= POST_COMMUNITY_SAMPLE:
                return topics, text_posts, total
    return topics, text_posts, total


def analyze_community_for_post(
    driver: WebDriver,
    label: str,
    subreddit: str,
    stats: AccountSummary,
) -> Tuple[List[str], bool, str]:
    """Analyse the community, then say whether a general post belongs here.

    Mirrors how a general comment is only written after the post is analysed:
    the community is read first, and a post is only written when there is enough
    signal to make it fit.
    """
    name = normalize_subreddit(subreddit)
    topics = [t for t in (stats.subreddit_topics.get(name) or []) if str(t).strip()]
    text_posts = 0
    total = 0
    if len(topics) < POST_MIN_COMMUNITY_TOPICS:
        fresh, text_posts, total = _sample_community_posts(driver, label, name)
        for title in fresh:
            if title not in topics:
                topics.append(title)
        if topics:
            stats.subreddit_topics[name] = topics
        if total and not topics:
            return topics, False, f"no text posts in the last {total} submissions"
        if total:
            share = text_posts / float(total)
            if share < POST_MIN_TEXT_SHARE:
                return (
                    topics,
                    False,
                    f"only {text_posts}/{total} recent posts are text ("
                    f"{share * 100:.0f}%) — images and links community",
                )
    if len(topics) < POST_MIN_COMMUNITY_TOPICS:
        return (
            topics,
            False,
            f"only {len(topics)} recent text post(s) to learn from",
        )
    return topics, True, f"{len(topics)} recent text posts read"


def _sheet_post_communities(user_id: str, name: str, serial: str) -> List[str]:
    try:
        post = take_next_sheet_post(user_id, name=name, serial=serial)
    except Exception:
        return []
    if not post:
        return []
    names = [normalize_subreddit(item) for item in (post.get("subreddits") or [])]
    primary = normalize_subreddit(str(post.get("subreddit") or ""))
    if primary and primary not in names:
        names.insert(0, primary)
    return [item for item in names if item and not is_blocked_subreddit(item)]


def _primary_sheet_post_subreddit(user_id: str, name: str, serial: str) -> str:
    names = _sheet_post_communities(user_id, name, serial)
    return names[0] if names else ""


def maybe_general_community_post(
    driver: WebDriver,
    label: str,
    user_id: str,
    stats: AccountSummary,
    subreddit: str,
    done: Optional[List[bool]] = None,
    serial: str = "",
) -> bool:
    """
    After lurking here: read rules + recent posts, write a fitting general post,
    add flair, click Post. One LIVE post per 48 hours still applies.
    Only used when posts.csv has no row for this account.
    """
    if not COMMUNITY_GENERAL_POST:
        return False
    if done and done[0]:
        return False
    name = normalize_subreddit(subreddit)
    if not name:
        return False
    if is_blocked_subreddit(name):
        log(f"[Profile {label}] No post in r/{name} — on the blocked list")
        return False
    sheet_names = {item.lower() for item in allowed_subreddits()}
    if name.lower() not in sheet_names:
        log(
            f"[Profile {label}] No general post in r/{name} — "
            "general posts stay on subreddits.csv"
        )
        return False
    if _sheet_post_communities(user_id, label, serial):
        log(
            f"[Profile {label}] posts.csv has a row for this account — "
            "not writing a general post"
        )
        return False
    allowed_post, why = account_may_post(stats)
    if not allowed_post:
        log(f"[Profile {label}] Skipping general post in r/{name} — {why}")
        return False
    if int(stats.account_karma or 0) <= 2:
        log(
            f"[Profile {label}] Karma is {stats.account_karma} — still trying a "
            f"simple general post in r/{name}. Communities that require more karma are skipped."
        )
    if general_posts_remaining(user_id) <= 0:
        log(
            f"[Profile {label}] Skipping general post in r/{name} — "
            f"already made {POSTS_PER_WINDOW} LIVE post in {ACTION_WINDOW_HOURS:.0f}h"
        )
        if done:
            done[0] = True
        return True

    summary, allowed, reason = analyze_subreddit_for_account(
        driver, label, name, stats.account_karma, stats.account_age_days
    )
    if not allowed:
        log(f"[Profile {label}] r/{name} is not open for a general post ({reason})")
        return False

    discussion = explore_subreddit_before_post(driver, label, name, stats)
    topics, fits, fit_reason = analyze_community_for_post(driver, label, name, stats)
    if not fits:
        log(f"[Profile {label}] No general post in r/{name} — {fit_reason}")
        return False
    if discussion:
        log(
            f"[Profile {label}] r/{name} explored — {fit_reason}; "
            f"post will follow {len(discussion)} recent thread(s)"
        )
    else:
        log(f"[Profile {label}] r/{name} analysed for posting — {fit_reason}")

    rules_obj = _rules_for(stats, name)
    if rules_obj is None or not getattr(rules_obj, "rule_count", 0):
        try:
            rules_obj = read_subreddit_rules(
                driver, label, name, stats, open_page=True
            )
        except Exception as exc:
            log(f"[Profile {label}] Could not read r/{name} rules ({brief_error(exc)})")
            rules_obj = _rules_for(stats, name)
    flags = (getattr(rules_obj, "flags", None) or {}) if rules_obj else {}
    questions_only = bool(flags.get("questions_only")) or name.lower() in {
        "nostupidquestions",
        "askuk",
        "tooafraidtoask",
    }
    no_humor = bool(flags.get("no_humor"))
    no_promo = bool(flags.get("no_promo"))
    rules_text = ""
    if rules_obj is not None:
        prompt_fn = getattr(rules_obj, "prompt_text", None)
        if callable(prompt_fn):
            rules_text = str(prompt_fn() or "")
        if not rules_text:
            rules_text = str(getattr(rules_obj, "summary", "") or "")
        titles = list(getattr(rules_obj, "titles", None) or [])
        if titles:
            rules_text = (rules_text + "\n" + " | ".join(titles[:10])).strip()
        raw = str(getattr(rules_obj, "raw", "") or "")
        if raw and raw not in rules_text:
            rules_text = (rules_text + "\n" + raw[:900]).strip()
    rules_text = rules_text or str(summary.get("rules") or "")
    if rules_text:
        log(f"[Profile {label}] Using r/{name} rules for the general post")

    log(
        f"[Profile {label}] Writing a human-tone post for r/{name} from recent discussion "
        f"({len(discussion or topics)} threads, {summary.get('subscribers') or 0} members)"
    )
    try:
        from reddit_joiner.ai import generate_ai_post
        from reddit_joiner.store import append_live_post, log_post_attempt
    except ImportError:
        append_live_post = None  # type: ignore[assignment]
        log_post_attempt = None  # type: ignore[assignment]
        from reddit_joiner.ai import generate_ai_post

    try:
        title, body, provider = generate_ai_post(
            name,
            description=str(summary.get("description") or ""),
            rules=rules_text,
            topics=discussion or topics,
            questions_only=questions_only,
            no_humor=no_humor,
            no_promo=no_promo,
            karma=stats.account_karma,
            age_days=stats.account_age_days,
        )
    except Exception as exc:
        log(f"[Profile {label}] Could not write a general r/{name} post ({brief_error(exc)})")
        return False

    log(f"[Profile {label}] General r/{name} post ({provider}): {title[:80]}")
    linger = _rng().uniform(4.0, 10.0)
    remaining = deadline_remaining()
    if remaining is not None:
        linger = min(linger, max(0.0, remaining - 8.0))
    if linger >= 2.0:
        log(f"[Profile {label}] {linger:.0f}s more in r/{name} before posting")
        try:
            human_wait(driver, linger, label, f"https://www.reddit.com/r/{name}/")
        except Exception:
            time.sleep(linger)

    if done:
        done[0] = True
    posted_url, submit_reason = submit_text_post(driver, label, name, title, body)
    if not posted_url:
        stats.post_status = "failed"
        stats.post_title = title
        stats.posted = name
        stats.post_note = submit_reason or "general post submit failed"
        if log_post_attempt:
            log_post_attempt(
                account=label,
                user_id=user_id,
                subreddit=name,
                title=title,
                url="",
                status="FAILED",
                reason=stats.post_note,
                body=body,
            )
        log(f"[Profile {label}] General post in r/{name} failed — {stats.post_note}")
        return True

    log(f"[Profile {label}] Waiting {POST_STATUS_WAIT}s then checking if the general post is LIVE")
    time.sleep(POST_STATUS_WAIT)
    status, why, score = check_post_live(driver, posted_url, label)
    log(f"[Profile {label}] General post on r/{name}: {status} ({why}) score={score}")
    if log_post_attempt:
        log_post_attempt(
            account=label,
            user_id=user_id,
            subreddit=name,
            title=title,
            url=posted_url,
            status=status,
            reason=why,
            score=score,
            body=body,
        )
    stats.post_title = title
    stats.posted = name
    stats.post_url = posted_url
    if status == "LIVE":
        record_karma_post(user_id, name, title)
        stats.post_status = "live"
        stats.post_note = f"general LIVE on r/{name} ({provider})"
        stats.karma_post_status = "live"
        stats.karma_post_sub = name
        stats.karma_post_note = stats.post_note
        stats.live_posts.append(posted_url)
        stats.upvotes_received += max(0, score)
        if append_live_post:
            append_live_post(
                account=label,
                subreddit=name,
                title=title,
                url=posted_url,
                score=score,
                status="LIVE",
            )
    elif status == "REMOVED":
        stats.posts_removed += 1
        stats.post_status = "removed"
        stats.post_note = why or "removed"
    else:
        stats.post_status = (status or "unknown").lower()
        stats.post_note = why or status
    return True


def analyze_subreddit_for_account(
    driver: WebDriver,
    label: str,
    subreddit: str,
    karma: int,
    age_days: float,
) -> Tuple[Dict[str, Any], bool, str]:
    about = reddit_session_json(driver, f"https://www.reddit.com/r/{subreddit}/about.json", label)
    rules = reddit_session_json(driver, f"https://www.reddit.com/r/{subreddit}/about/rules.json", label)
    if not about or about.get("error") or about.get("reason"):
        return {}, False, "could not read community"
    try:
        from reddit_joiner.karma import subreddit_allows_account, summarize_subreddit
    except ImportError as exc:
        return {}, False, str(exc)
    summary = summarize_subreddit(about, rules)
    allowed, reason = subreddit_allows_account(summary, karma, age_days)
    return summary, allowed, reason


def _record_join(stats: AccountSummary, subreddit: str, join_status: str) -> None:
    if join_status == "joined" and subreddit not in stats.joined:
        stats.joined.append(subreddit)
    elif (
        join_status == "already_joined"
        and subreddit not in stats.already_member
        and subreddit not in stats.joined
    ):
        stats.already_member.append(subreddit)


def maybe_karma_growth_comments(
    driver: WebDriver,
    label: str,
    user_id: str,
    stats: AccountSummary,
    session_subs: Optional[List[str]] = None,
    time_budget: Optional[float] = None,
) -> None:
    if _too_new_for_general_comment(stats):
        stats.karma_comment_note = (
            f"karma 0 and under {GENERAL_COMMENT_MIN_AGE_DAYS} days — no general comment"
        )
        log(f"[Profile {label}] Skipping extra general comments — {stats.karma_comment_note}")
        return
    if not KARMA_GROWTH_ENABLED:
        return
    target = stats.session_comment_target or _rng().randint(*SESSION_GENERAL_COMMENTS)
    have, room = general_comment_need(user_id, session_want=target)
    need = min(room, max(0, target - stats.comments))
    if need <= 0:
        stats.karma_comment_note = (
            f"already left {stats.comments}/{target} this run "
            f"({have}/{COMMENTS_PER_WINDOW} comments in {ACTION_WINDOW_HOURS:.0f}h)"
        )
        log(f"[Profile {label}] Skipping extra general comments — {stats.karma_comment_note}")
        return

    allowed = {name.lower() for name in allowed_subreddits()}
    session_names = [
        normalize_subreddit(name)
        for name in (session_subs or stats.joined + stats.already_member or allowed_subreddits())
        if normalize_subreddit(name) and normalize_subreddit(name).lower() in allowed
    ]
    seen = set()
    prefer: List[str] = []
    for name in session_names:
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        prefer.append(name)
    extras = [name for name in allowed_subreddits() if name.lower() not in seen]
    _rng().shuffle(prefer)
    _rng().shuffle(extras)
    subs = (prefer + extras)[:GENERAL_COMMENT_SUBS_PER_RUN]
    if not subs:
        stats.karma_comment_note = "no communities in subreddits.csv"
        log(f"[Profile {label}] Skipping extra general comments — {stats.karma_comment_note}")
        return
    log(
        f"[Profile {label}] Extra general comments — {need} new post(s) in this run's "
        f"new communities (have {have}/{COMMENTS_PER_WINDOW} in {ACTION_WINDOW_HOURS:.0f}h, "
        f"{stats.comments}/{target} this run)"
    )
    left = need
    started = time.time()
    for subreddit in subs:
        if left <= 0:
            break
        if time_budget is not None and time.time() - started >= time_budget:
            log(
                f"[Profile {label}] Stopping extra general comments — "
                f"{time_budget:.0f}s of community time already used"
            )
            break
        subreddit = normalize_subreddit(subreddit)
        if not subreddit or is_blocked_subreddit(subreddit):
            continue
        try:
            raise_profile_browser(label)
            if not open_exclusive_community(
                driver,
                label,
                user_id,
                subreddit,
                f"https://www.reddit.com/r/{subreddit}/",
                wait=20.0,
            ):
                continue
            time.sleep(_rng().uniform(1.2, 2.0))
            dismiss_popups(driver)
            _record_join(stats, subreddit, join_subreddit(driver, label, subreddit))
            time.sleep(_rng().uniform(1.2, 2.4))
            took = leave_subreddit_comments(
                driver, label, user_id, stats, subreddit, min(left, 1)
            )
            left -= took
        except Exception as exc:
            log(f"[Profile {label}] General comments on r/{subreddit} failed ({brief_error(exc)})")
            continue

    done = need - left
    have_after = have + done
    stats.karma_comment_note = (
        f"{done} this run, {have_after}/{COMMENTS_PER_WINDOW} in {ACTION_WINDOW_HOURS:.0f}h"
    )
    log(f"[Profile {label}] General comments done — {stats.karma_comment_note}")


def maybe_karma_growth_post(
    driver: WebDriver,
    label: str,
    user_id: str,
    stats: AccountSummary,
) -> None:
    if not KARMA_GROWTH_ENABLED:
        return
    allowed, why = account_may_post(stats)
    if not allowed:
        stats.karma_post_status = "skipped"
        stats.karma_post_note = why
        log(f"[Profile {label}] Skipping karma post — {why}")
        return
    remaining_posts = general_posts_remaining(user_id)
    if remaining_posts <= 0:
        stats.karma_post_status = "skipped"
        stats.karma_post_note = (
            f"already made {POSTS_PER_WINDOW} posts in {ACTION_WINDOW_HOURS:.0f}h"
        )
        log(f"[Profile {label}] Skipping karma post — {stats.karma_post_note}")
        return

    try:
        from reddit_joiner.ai import generate_ai_post
        from reddit_joiner.karma import candidate_subreddits
        from reddit_joiner.store import append_live_post, log_post_attempt
    except ImportError:
        append_live_post = None  # type: ignore[assignment]
        log_post_attempt = None  # type: ignore[assignment]
        try:
            from reddit_joiner.ai import generate_ai_post
            from reddit_joiner.karma import candidate_subreddits
        except ImportError as exc:
            stats.karma_post_status = "skipped"
            stats.karma_post_note = brief_error(exc)
            log(f"[Profile {label}] Karma post unavailable ({stats.karma_post_note})")
            return

    session_names = []
    seen = set()
    for raw in list(stats.subreddit_topics.keys()) + stats.joined + stats.already_member + allowed_subreddits():
        name = normalize_subreddit(raw)
        key = name.lower()
        if not name or key in seen:
            continue
        seen.add(key)
        session_names.append(name)
    candidates = session_names[:4]
    log(
        f"[Profile {label}] General community post — account u/{stats.reddit_username or label} "
        f"karma={stats.account_karma} age={stats.account_age_days:.0f}d tier={stats.karma_tier} "
        f"| trying communities already browsed: {', '.join('r/' + s for s in candidates[:KARMA_POST_MAX_SUBS])}"
    )

    tried = 0
    restricted = 0
    last_reason = ""
    for subreddit in candidates:
        if tried >= KARMA_POST_MAX_SUBS:
            break
        subreddit = normalize_subreddit(subreddit)
        if not subreddit:
            continue
        summary, allowed, reason = analyze_subreddit_for_account(
            driver, label, subreddit, stats.account_karma, stats.account_age_days
        )
        if not allowed:
            log(f"[Profile {label}] Skip r/{subreddit} for karma post ({reason})")
            last_reason = f"r/{subreddit}: {reason}"
            restricted += 1
            if restricted >= KARMA_POST_RESTRICTED_STOP:
                log(
                    f"[Profile {label}] Stopping karma post hunt after "
                    f"{restricted} restricted communities"
                )
                break
            continue
        # Analyse the community before writing. This path used to pass whatever
        # topics happened to be cached, so a community that was not browsed this
        # run got a post written with no idea what belongs in it.
        topics, fits, fit_reason = analyze_community_for_post(
            driver, label, subreddit, stats
        )
        if not fits:
            log(f"[Profile {label}] Skip r/{subreddit} for karma post — {fit_reason}")
            last_reason = f"r/{subreddit}: {fit_reason}"
            continue
        tried += 1
        log(
            f"[Profile {label}] r/{subreddit} looks open to this account "
            f"({summary.get('subscribers') or 0} members, {reason}; {fit_reason})"
        )
        try:
            rules_obj = _rules_for(stats, subreddit)
            flags = (getattr(rules_obj, "flags", None) or {}) if rules_obj else {}
            title, body, provider = generate_ai_post(
                subreddit,
                description=str(summary.get("description") or ""),
                rules=str(summary.get("rules") or ""),
                topics=topics,
                questions_only=bool(flags.get("questions_only")),
                no_humor=bool(flags.get("no_humor")),
                no_promo=bool(flags.get("no_promo")),
                karma=stats.account_karma,
                age_days=stats.account_age_days,
            )
        except Exception as exc:
            last_reason = brief_error(exc)
            log(f"[Profile {label}] Could not write a r/{subreddit} post ({last_reason})")
            continue
        log(f"[Profile {label}] Growth post ({provider}) for r/{subreddit}: {title[:80]}")
        try:
            raise_profile_browser(label)
            if not open_exclusive_community(
                driver,
                label,
                user_id,
                subreddit,
                f"https://www.reddit.com/r/{subreddit}/",
                wait=20.0,
            ):
                last_reason = f"r/{subreddit} is open on another account"
                continue
            time.sleep(_rng().uniform(1.4, 2.4))
            dismiss_popups(driver)
            _record_join(stats, subreddit, join_subreddit(driver, label, subreddit))
            time.sleep(_rng().uniform(1.0, 1.8))
            posted_url, submit_reason = submit_text_post(driver, label, subreddit, title, body)
            if not posted_url:
                last_reason = submit_reason or f"submit failed on r/{subreddit}"
                if log_post_attempt:
                    log_post_attempt(
                        account=label,
                        user_id=user_id,
                        subreddit=subreddit,
                        title=title,
                        url="",
                        status="FAILED",
                        reason=last_reason,
                        body=body,
                    )
                continue
            log(f"[Profile {label}] Waiting {POST_STATUS_WAIT}s then checking if the growth post is LIVE")
            time.sleep(POST_STATUS_WAIT)
            status, why, score = check_post_live(driver, posted_url, label)
            last_reason = why
            log(f"[Profile {label}] Karma post on r/{subreddit}: {status} ({why}) score={score}")
            if log_post_attempt:
                log_post_attempt(
                    account=label,
                    user_id=user_id,
                    subreddit=subreddit,
                    title=title,
                    url=posted_url,
                    status=status,
                    reason=why,
                    score=score,
                    body=body,
                )
            if status == "LIVE":
                record_karma_post(user_id, subreddit, title)
                stats.karma_post_status = "live"
                stats.karma_post_sub = subreddit
                stats.karma_post_note = f"LIVE on r/{subreddit} ({provider})"
                stats.live_posts.append(posted_url)
                stats.upvotes_received += max(0, score)
                if not stats.post_url:
                    stats.post_url = posted_url
                    stats.post_title = title
                    stats.posted = subreddit
                    stats.post_status = "live"
                    stats.post_note = stats.karma_post_note
                if append_live_post:
                    append_live_post(
                        account=label,
                        subreddit=subreddit,
                        title=title,
                        url=posted_url,
                        score=score,
                        status="LIVE",
                    )
                return
            if status == "REMOVED":
                stats.posts_removed += 1
                last_reason = f"removed on r/{subreddit}"
                continue
        except Exception as exc:
            last_reason = brief_error(exc)
            log(f"[Profile {label}] Karma post attempt failed ({last_reason})")

    stats.karma_post_status = "failed"
    stats.karma_post_note = last_reason or "no newbie-friendly sub accepted a post"
    log(f"[Profile {label}] Karma post failed — {stats.karma_post_note}")


def maybe_leave_weekly_comments(
    driver: WebDriver,
    label: str,
    user_id: str,
    stats: AccountSummary,
    serial: str = "",
) -> None:
    have = len(general_comments_this_week(user_id))
    room = comments_remaining(user_id)
    if room <= 0:
        stats.comment_note = (
            f"already left {have}/{COMMENTS_PER_WINDOW} comments in the last "
            f"{ACTION_WINDOW_HOURS:.0f}h"
        )
        log(f"[Profile {label}] Skipping comments.csv — {stats.comment_note}")
        return
    remaining = min(
        SHEET_COMMENTS_PER_RUN - int(stats.sheet_comments or 0),
        room,
    )
    if remaining <= 0:
        stats.comment_note = "already used this account's comments.csv slots this run"
        log(f"[Profile {label}] Skipping comments.csv — {stats.comment_note}")
        return

    try:
        targets = take_comment_targets(user_id, remaining, name=label, serial=serial)
    except Exception as exc:
        stats.comment_note = brief_error(exc)
        log(f"[Profile {label}] Could not read comments.csv ({stats.comment_note})")
        return

    if not targets:
        stats.comment_note = "no unused comments.csv links for this account"
        log(
            f"[Profile {label}] Skipping comments.csv — no unused post link for this account"
        )
        return

    for item in targets:
        link_sub = re.search(r"/r/([A-Za-z0-9_]+)", str(item.get("link") or ""))
        if link_sub and is_blocked_subreddit(link_sub.group(1)):
            log(
                f"[Profile {label}] Skipping the comments.csv link for "
                f"r/{link_sub.group(1)} — on the blocked list"
            )
            continue
        comment_text = (item.get("text") or "").strip()
        if not comment_text:
            log(
                f"[Profile {label}] Opening {item['label']} to write a general comment "
                f"on the comments.csv link"
            )
        else:
            log(
                f"[Profile {label}] Commenting on {item['label']} "
                f"({item['link'][:90]}): {comment_text[:80]}"
            )
        try:
            raise_profile_browser(label)
            navigate(driver, item["link"], label)
            style = _style()
            time.sleep(_rng().uniform(*(style.post_load_wait if style else POST_LOAD_WAIT)))
            dismiss_popups(driver)
            read_opened_thread(
                driver, _rng().uniform(*(style.thread_read if style else THREAD_READ)), label
            )
            if not current_is_reddit(driver):
                log(f"[Profile {label}] Not on Reddit after opening {item['label']} — skip")
                release_comment_row(item["row_index"])
                continue
            time.sleep(_rng().uniform(1.5, 3.0))
            posted = False
            stats.last_comment_url = ""
            if comment_text:
                posted = comment_on_current_post(
                    driver, label, stats, text=comment_text, count_session=False
                )
                if posted:
                    record_account_comment(user_id, item["link"], comment_text, kind="sheet")
                    try:
                        from reddit_joiner.store import log_ai_comment

                        info = extract_opened_post(driver)
                        comment_id = _ids_from_reddit_url(stats.last_comment_url or "")[1]
                        log_ai_comment(
                            account=label,
                            user_id=user_id,
                            post_url=item["link"],
                            post_title=str(info.get("title") or item["label"]),
                            sentiment=0.0,
                            comment=comment_text,
                            method="sheet",
                            subreddit=subreddit_from_url(item["link"]),
                            post_body=str(info.get("body") or "")[:500],
                            comment_id=comment_id,
                            tone="neutral",
                        )
                    except Exception:
                        pass
                    state = _rl_state(
                        stats,
                        subreddit_from_url(item["link"]),
                        item.get("label") or "",
                        comment_text,
                        kind="sheet",
                        comment_length=len(comment_text),
                    )
                    # Sheet comments use text the user supplied, so their
                    # outcome says nothing about the model's tone choice.
                    stats.rl_reward += float(REWARD_COMMENT_POSTED)
            else:
                posted = maybe_ai_comment_on_opened_post(
                    driver, label, user_id, stats, kind="sheet", force=True
                )
            if posted:
                posted_body = (stats.last_comment_text or comment_text).strip()
                extra = (item.get("edit") or "").strip()
                if extra:
                    wait_for = _rng().uniform(*COMMENT_EDIT_WAIT)
                    linger_on_current_thread(
                        driver,
                        wait_for,
                        label,
                        stay_url=item["link"],
                    )
                    edited = posted_body.rstrip() + "\n\n" + extra
                    if extra.lower() not in posted_body.lower():
                        ok_edit = edit_own_comment_on_current_post(
                            driver,
                            label,
                            posted_body,
                            edited,
                            author=stats.reddit_username,
                        )
                        if ok_edit:
                            stats.last_comment_text = edited
                            log(f"[Profile {label}] Added the edit text to {item['label']}")
                        else:
                            log(f"[Profile {label}] Could not edit {item['label']} — original comment stays")
                    else:
                        log(f"[Profile {label}] Edit text already in {item['label']} — skip")
                comment_url = (stats.last_comment_url or "").strip()
                if not comment_url:
                    comment_url = extract_comment_permalink(
                        driver,
                        posted_body,
                        author=stats.reddit_username,
                    )
                mark_comment_row_used(
                    item["row_index"],
                    label,
                    comment_url=comment_url,
                    posted_text=stats.last_comment_text or posted_body,
                )
                if comment_url:
                    log(f"[Profile {label}] Kept comment URL: {comment_url}")
                    stats.last_comment_url = comment_url
                try:
                    from reddit_joiner.store import attach_ai_comment_permalink

                    attach_ai_comment_permalink(
                        item["link"],
                        comment_url,
                        _ids_from_reddit_url(comment_url)[1],
                    )
                except Exception:
                    pass
                try:
                    agent = _rl_agent()
                    if agent is not None:
                        sheet_state = _rl_state(
                            stats,
                            subreddit_from_url(item["link"]),
                            item.get("label") or "",
                            posted_body,
                            kind="sheet",
                            comment_length=len(posted_body),
                        )
                        agent.queue_delayed(
                            sheet_state,
                            "comment:neutral",
                            comment_url or item["link"],
                            "comment",
                        )
                except Exception:
                    pass
                stats.sheet_comments += 1
                if stats.sheet_comments >= remaining:
                    break
                if not wait_before_next_comment(driver, label, user_id, item["link"]):
                    break
                continue
            log(f"[Profile {label}] Could not comment on {item['label']}")
            release_comment_row(item["row_index"])
        except Exception as exc:
            log(f"[Profile {label}] Comment on {item['label']} failed ({brief_error(exc)})")
            try:
                release_comment_row(item["row_index"])
            except Exception:
                pass
        time.sleep(_rng().uniform(2.0, 4.5))

    if not stats.sheet_comments and not stats.comment_note:
        stats.comment_note = "comment submit failed"


def maybe_submit_weekly_post(
    driver: WebDriver,
    label: str,
    user_id: str,
    stats: AccountSummary,
    serial: str = "",
) -> None:
    remaining_posts = general_posts_remaining(user_id)
    if remaining_posts <= 0:
        stats.post_status = "skipped"
        stats.post_note = (
            f"already made {POSTS_PER_WINDOW} posts in {ACTION_WINDOW_HOURS:.0f}h"
        )
        log(f"[Profile {label}] Skipping sheet post — {stats.post_note}")
        return
    # Sheet rows and general posts both wait until karma reaches MIN_KARMA_TO_POST.

    try:
        post = take_next_sheet_post(user_id, name=label, serial=serial)
    except Exception as exc:
        stats.post_status = "skipped"
        stats.post_note = brief_error(exc)
        log(f"[Profile {label}] Could not read posts.csv ({stats.post_note})")
        return

    if not post:
        stats.post_status = "skipped"
        stats.post_note = _sheet_post_skip_reason(user_id, label, serial)
        log(f"[Profile {label}] Skipping post — {stats.post_note}")
        return

    title = post["title"]
    body = post["body"]
    preferred_flair = str(post.get("flair") or "").strip()
    subreddit = post.get("subreddit") or next_post_subreddit(user_id, post.get("subreddits") or [])
    stats.post_title = title
    body_source = str(post.get("body_source") or "posts.csv")
    paras = body.count("\n\n") + 1 if str(body).strip() else 0
    log(
        f"[Profile {label}] Sheet post template from {body_source} "
        f"({len(body)} chars, {paras} paragraph(s), spacing kept)"
    )
    if not subreddit:
        stats.post_status = "skipped"
        stats.post_note = "sheet row has no subreddit"
        log(f"[Profile {label}] Skipping post — put a subreddit on that row")
        return

    post_label = f"row {post['row_index'] + 2}"
    try:
        from reddit_joiner.store import PostRetryManager, append_live_post, log_post_attempt
    except ImportError:
        PostRetryManager = None  # type: ignore[assignment]
        append_live_post = None  # type: ignore[assignment]
        log_post_attempt = None  # type: ignore[assignment]

    retry = PostRetryManager() if PostRetryManager else None
    if retry is not None:
        if retry.is_live(title, body):
            stats.post_status = "skipped"
            stats.post_note = "already LIVE for this title/body"
            log(f"[Profile {label}] Skipping post — already LIVE")
            return
        if not retry.should_retry_on_another_account(title, body, MAX_RETRY_ACCOUNTS):
            tried = retry.accounts_tried(title, body)
            if user_id not in tried and label not in tried:
                stats.post_status = "skipped"
                stats.post_note = "max accounts already tried this post"
                log(f"[Profile {label}] Skipping post — {stats.post_note}")
                return

    extras = list(post.get("remaining") or []) + list(post.get("subreddits") or [])
    targets = post_targets_for_row(subreddit, extras)
    if not targets:
        stats.post_status = "skipped"
        stats.post_note = "sheet row has no subreddit"
        log(f"[Profile {label}] Skipping post — put a subreddit on that row")
        return
    agent = _rl_agent()
    sheet_names = {name.lower() for name in targets}
    log(
        f"[Profile {label}] Posting {post_label}: {title[:80]} | "
        f"only r/{targets[0]} (from posts.csv)"
        + (f" (RL ε={agent.epsilon:.3f})" if agent is not None else "")
    )

    last_reason = ""
    for attempt, target in enumerate(targets, start=1):
        log(f"[Profile {label}] Attempt {attempt}/{len(targets)} on r/{target}")
        # Score this attempt against the community actually being tried, using a
        # fixed action label rather than the subreddit name.
        target_state = _rl_state(stats, target, title, body, kind="post")
        target_action = _post_target_action(target, target.lower() in sheet_names)
        try:
            raise_profile_browser(label)
            if not open_exclusive_community(
                driver,
                label,
                user_id,
                target,
                f"https://www.reddit.com/r/{target}/",
            ):
                last_reason = f"r/{target} is open on another account"
                continue
            time.sleep(_rng().uniform(1.8, 3.0))
            dismiss_popups(driver)
            try:
                read_subreddit_rules(driver, label, target, stats, open_page=False)
            except Exception:
                pass
            join_status = join_subreddit(driver, label, target)
            if join_status == "joined":
                stats.joined.append(target)
            elif join_status == "already_joined" and target not in stats.already_member and target not in stats.joined:
                stats.already_member.append(target)
            elif join_status == "failed":
                log(f"[Profile {label}] Could not confirm Join on r/{target} — still trying to submit")
            time.sleep(_rng().uniform(1.5, 3.0))
            posted_url, submit_reason = submit_text_post(
                driver, label, target, title, body, preferred_flair=preferred_flair
            )
            if not posted_url:
                last_reason = submit_reason or f"submit failed on r/{target}"
                composer_miss = _is_composer_failure(last_reason)
                if log_post_attempt:
                    log_post_attempt(
                        account=label,
                        user_id=user_id,
                        subreddit=target,
                        title=title,
                        url="",
                        status="FAILED",
                        reason=last_reason,
                        body=body,
                    )
                if retry is not None and not composer_miss:
                    retry.log_attempt(
                        post_title=title,
                        post_body=body,
                        subreddit=target,
                        account_id=label,
                        attempt_num=attempt,
                        status="failed",
                        reason=last_reason,
                        original_subreddit=subreddit,
                    )
                _rl_learn(
                    stats,
                    target_state,
                    target_action,
                    REWARD_POST_REMOVED_SPAM,
                    next_actions=list(POST_ACTIONS),
                    graded=True,
                )
                continue
            log(f"[Profile {label}] Waiting {POST_STATUS_WAIT}s then checking if the post is LIVE")
            time.sleep(POST_STATUS_WAIT)
            status, reason, score = check_post_live(driver, posted_url, label)
            last_reason = reason
            log(f"[Profile {label}] Post status on r/{target}: {status} ({reason}) score={score}")
            if log_post_attempt:
                log_post_attempt(
                    account=label,
                    user_id=user_id,
                    subreddit=target,
                    title=title,
                    url=posted_url,
                    status=status,
                    reason=reason,
                    score=score,
                    body=body,
                )
            if retry is not None:
                retry.log_attempt(
                    post_title=title,
                    post_body=body,
                    subreddit=target,
                    account_id=label,
                    attempt_num=attempt,
                    status=status.lower(),
                    url=posted_url,
                    reason=reason,
                    original_subreddit=subreddit,
                )
            if status == "LIVE":
                reward = REWARD_POST_APPROVED if reason == "approved" else REWARD_POST_LIVE
                _rl_learn(
                    stats,
                    target_state,
                    target_action,
                    reward,
                    next_actions=list(POST_ACTIONS),
                    graded=True,
                )
                try:
                    agent = _rl_agent()
                    if agent is not None:
                        agent.queue_delayed(target_state, target_action, posted_url, "post")
                except Exception:
                    pass
                record_account_post(user_id, target, title, sheet_row=post["row_index"])
                mark_sheet_post_used(post["row_index"], label, target)
                stats.posted = target
                stats.post_status = "live"
                stats.post_url = posted_url
                stats.post_note = f"LIVE on r/{target}"
                stats.live_posts.append(posted_url)
                stats.upvotes_received += max(0, score)
                if retry is not None:
                    retry.save_live_post(
                        post_title=title,
                        post_body=body,
                        subreddit=target,
                        account_id=label,
                        url=posted_url,
                        score=score,
                    )
                elif append_live_post:
                    append_live_post(
                        account=label,
                        subreddit=target,
                        title=title,
                        url=posted_url,
                        score=score,
                        status="LIVE",
                    )
                return
            if status == "REMOVED":
                stats.posts_removed += 1
                _rl_learn(
                    stats,
                    target_state,
                    target_action,
                    _removal_reward(reason),
                    next_actions=list(POST_ACTIONS),
                    graded=True,
                )
                log(
                    f"[Profile {label}] Post was REMOVED on r/{target} — "
                    "not posting to another community"
                )
                continue
            last_reason = reason or "unknown"
            _rl_learn(
                stats,
                target_state,
                target_action,
                REWARD_POST_REMOVED_SPAM,
                next_actions=list(POST_ACTIONS),
                graded=True,
            )
        except Exception as exc:
            last_reason = brief_error(exc)
            log(f"[Profile {label}] Post attempt failed ({last_reason})")
            # A crash in our own automation is not Reddit's verdict on the post,
            # so this trains nothing.

    stats.posted = subreddit
    stats.post_status = "failed"
    stats.post_note = last_reason or "all post attempts failed"
    if targets:
        final = targets[-1]
        _rl_learn(
            stats,
            _rl_state(stats, final, title, body, kind="post"),
            _post_target_action(final, final.lower() in sheet_names),
            REWARD_POST_ALL_FAILED,
            next_actions=list(POST_ACTIONS),
            graded=True,
        )
    log(f"[Profile {label}] All post attempts failed — this row stays with this account")
    release_sheet_post(post["row_index"], reason="failed")


def maybe_do_session_post(
    driver: WebDriver,
    label: str,
    user_id: str,
    stats: AccountSummary,
    serial: str,
    done: List[bool],
    preferred_sub: str = "",
) -> None:
    """One posts.csv post, or a rules-fitted general post when the sheet is empty."""
    if done and done[0]:
        return
    allowed_post, post_why = account_may_post(stats)
    has_sheet = bool(_sheet_post_communities(user_id, label, serial))
    log(
        f"[Profile {label}] Mid-session post "
        f"(1 attempt this run, max {POSTS_PER_WINDOW} LIVE/{ACTION_WINDOW_HOURS:.0f}h, "
        f"karma {stats.account_karma}/{MIN_KARMA_TO_POST}"
        + (", sheet row assigned" if has_sheet else ", no posts.csv row")
        + ")"
    )
    if has_sheet:
        if not allowed_post:
            if done:
                done[0] = True
            stats.post_status = "skipped"
            stats.post_note = post_why
            log(f"[Profile {label}] Skipping posts.csv — {post_why}")
            return
        if done:
            done[0] = True
        if general_posts_remaining(user_id) <= 0:
            log(
                f"[Profile {label}] Weekly post cap reached "
                f"({POSTS_PER_WINDOW}/{ACTION_WINDOW_HOURS:.0f}h) — skipping posts.csv"
            )
            return
        log(
            f"[Profile {label}] Checking posts.csv "
            f"({general_posts_remaining(user_id)} post(s) left in {ACTION_WINDOW_HOURS:.0f}h)"
        )
        maybe_submit_weekly_post(
            driver,
            label,
            user_id,
            stats,
            serial=serial,
        )
        return
    if not COMMUNITY_GENERAL_POST:
        if done:
            done[0] = True
        stats.post_status = "skipped"
        stats.post_note = "no posts.csv row — general posts are off"
        log(f"[Profile {label}] No posts.csv row for this account — not posting")
        return
    if not allowed_post:
        if done:
            done[0] = True
        stats.post_status = "skipped"
        stats.post_note = post_why
        log(f"[Profile {label}] Skipping general post — {post_why}")
        return
    if general_posts_remaining(user_id) <= 0:
        if done:
            done[0] = True
        stats.post_status = "skipped"
        stats.post_note = (
            f"already made {POSTS_PER_WINDOW} posts in {ACTION_WINDOW_HOURS:.0f}h"
        )
        log(f"[Profile {label}] Skipping general post — {stats.post_note}")
        return
    allowed = {name.lower() for name in allowed_subreddits()}
    ordered: List[str] = []
    seen = set()

    def _add(raw: str) -> None:
        name = normalize_subreddit(raw)
        key = name.lower()
        if not name or key in seen or key not in allowed:
            return
        seen.add(key)
        ordered.append(name)

    # Newbie-friendly sheet communities first; AskUK is often posting-restricted.
    for raw in ("Advice", "NoStupidQuestions", "AskUK"):
        _add(raw)
    _add(preferred_sub)
    for raw in list(stats.subreddit_topics.keys()) + stats.joined + stats.already_member:
        _add(raw)
    for raw in allowed_subreddits():
        _add(raw)
    if not ordered:
        stats.post_status = "skipped"
        stats.post_note = "no subreddits.csv community for a general post"
        log(f"[Profile {label}] {stats.post_note}")
        return
    last_why = ""
    for target in ordered:
        if done and done[0]:
            return
        log(
            f"[Profile {label}] posts.csv is empty — trying a general post in r/{target} "
            "from that community's rules and recent posts"
        )
        before = bool(done and done[0])
        maybe_general_community_post(
            driver, label, user_id, stats, target, done=done, serial=serial
        )
        if done and done[0] and not before:
            return
        if stats.post_status in {"live", "removed"} and stats.posted:
            return
        if stats.post_note:
            last_why = stats.post_note
    if not (done and done[0]):
        stats.post_status = stats.post_status if stats.post_status != "none" else "skipped"
        stats.post_note = last_why or "no subreddits.csv community accepted a general post"
        log(f"[Profile {label}] General post skipped — {stats.post_note}")


def continue_general_activity_in_subs(
    driver: WebDriver,
    label: str,
    user_id: str,
    stats: AccountSummary,
    subs: List[str],
    seconds: float,
) -> None:
    """Spend leftover session time doing general browse activity in the new subs."""
    names = []
    seen = set()
    for raw in subs:
        name = normalize_subreddit(raw)
        key = name.lower()
        if not name or key in seen:
            continue
        seen.add(key)
        names.append(name)
    if not names or seconds < MIN_LEFTOVER_HOME:
        return
    _rng().shuffle(names)
    style = _style()
    mode = style.leftover_mode if style else "even"
    use_n = len(names)
    if mode == "one_long":
        use_n = 1
    elif mode == "weighted":
        use_n = max(1, min(len(names), _rng().randint(1, min(2, len(names)))))
    else:
        use_n = max(1, min(len(names), _rng().randint(1, min(2, len(names)))))
    use = names[:use_n]
    leftover = float(seconds)
    if mode == "one_long":
        shares = [1.0]
    elif mode == "weighted":
        raw = [_rng().uniform(0.35, 1.7) for _ in use]
        total = sum(raw) or 1.0
        shares = [item / total for item in raw]
    else:
        shares = [1.0 / len(use)] * len(use)
    for name, share in zip(use, shares):
        if leftover < MIN_LEFTOVER_HOME:
            break
        dwell = min(max(MIN_LEFTOVER_HOME, float(seconds) * share), leftover)
        url = f"https://www.reddit.com/r/{name}/"
        log(
            f"[Profile {label}] {dwell / 60:.1f} min more general activity in r/{name}"
        )
        try:
            raise_profile_browser(label)
            if not open_exclusive_community(
                driver,
                label,
                user_id,
                name,
                url,
                wait=30.0,
            ):
                continue
            dismiss_popups(driver)
            perform_browse_activity(
                driver,
                dwell,
                label,
                user_id=user_id,
                stats=stats,
                stay_url=url,
            )
        except Exception as exc:
            log(f"[Profile {label}] Extra activity in r/{name} stopped ({brief_error(exc)})")
        leftover -= dwell


def process_profile(
    profile: Dict[str, str],
    subreddits: List[str],
    assigned_seconds: Optional[float] = None,
) -> AccountSummary:
    user_id = profile["user_id"]
    label = profile.get("name") or user_id
    stats = AccountSummary(name=label, user_id=user_id)
    driver: Optional[WebDriver] = None
    style: Optional[SessionStyle] = None
    hops: List[str] = []

    try:
        begin_session_rng(user_id, label)
        _tls.batch_user_id = user_id
        data = start_profile(
            user_id,
            label,
            name=profile.get("name") or "",
            serial=profile.get("serial_number") or "",
        )
        time.sleep(_rng().uniform(1.2, 2.2))
        driver = connect_to_browser(data, label)
        raise_profile_browser(label)
        wait_for_proxy_ip(driver, label, user_id)

        log(f"[Profile {label}] Step 1 done — proxy IP ready. Opening Reddit Home.")
        try:
            open_reddit_home_ready(driver, label)
        except RuntimeError:
            blocked = home_account_block_reason(driver, False)
            if not blocked:
                raise
            stats.account_status = blocked
            log(f"[Profile {label}] Closing this account — {blocked}")
            stats.print_report()
            return stats
        logged_in = False
        try:
            info = read_logged_in_account(driver, label)
            apply_account_analysis(stats, info)
            logged_in = bool(stats.reddit_username)
        except Exception as exc:
            log(f"[Profile {label}] Account analysis failed ({brief_error(exc)})")
            info = {}
        if info.get("suspended"):
            stats.account_status = "the account is banned"
            who = f"u/{stats.reddit_username} " if stats.reddit_username else ""
            log(f"[Profile {label}] Account check: {who}account banned — closing")
            stats.print_report()
            return stats
        blocked = home_account_block_reason(driver, logged_in)
        if blocked:
            stats.account_status = blocked
            log(f"[Profile {label}] Closing this account — {blocked}")
            stats.print_report()
            return stats
        if profile_icon_shows_server_error(driver, label):
            stats.account_status = "the account is banned"
            log(f"[Profile {label}] Closing this account — the account is banned")
            stats.print_report()
            return stats
        if logged_in:
            log(f"[Profile {label}] Account check: u/{stats.reddit_username} is not banned")
        if stats.reddit_username:
            log(
                f"[Profile {label}] Account u/{stats.reddit_username} | "
                f"karma {stats.account_karma} "
                f"(link {info.get('link_karma', 0)}, comment {info.get('comment_karma', 0)}) | "
                f"age {stats.account_age_days:.0f} days | tier {stats.karma_tier}"
            )
        else:
            log(f"[Profile {label}] Could not read karma/age — treating this account as new")

        style, browse_list, fingerprint = unique_session_plan(
            user_id,
            label,
            stats.account_karma,
            stats.account_age_days,
            assigned_seconds=assigned_seconds,
        )
        set_current_style(style)
        session_start = time.time()
        session_end = session_start + float(style.session_seconds)
        # Hard stop: every pause from here on is clamped to this sitting
        set_session_deadline(session_end)
        _tls.cursor = None
        _tls.viewport = None
        # Re-probe the scroll engines for every profile: whether synthesized
        # input is accepted depends on the browser window, not on the code.
        _tls.wheel_ok = None
        _tls.gesture_ok = None
        _tls.wheel_misses = 0
        _tls.gesture_misses = 0
        _tls.scrolled_px = 0.0
        post_mid_at = session_start + float(style.session_seconds) * float(
            style.post_fraction
        )
        log(
            f"[Profile {label}] Assigned sitting {style.session_seconds / 60:.0f} min — "
            f"{style.summary()} | Home {style.home_share:.0%} | "
            f"{style.explore_n} explore, {style.search_n} search "
            f"({fingerprint[:55]}…)"
        )

        serial = profile.get("serial_number") or ""
        session_post_done = [False]
        sheet_post_joined = [False]
        sheet_target = ""
        have_comments, comment_room = general_comment_need(user_id)
        post_room = general_posts_remaining(user_id)
        allowed_set = {name.lower() for name in allowed_subreddits()}
        browse_list = [
            normalize_subreddit(name)
            for name in browse_list
            if name and normalize_subreddit(name).lower() in allowed_set
        ]
        hops = list(browse_list)
        sheet_slots = unused_sheet_comment_slots(user_id, name=label, serial=serial)
        has_sheet_links = sheet_slots > 0
        general_room = max(0, comment_room - sheet_slots)
        stats.session_comment_target = min(
            _rng().randint(*SESSION_GENERAL_COMMENTS), general_room
        )
        if _too_new_for_general_comment(stats):
            stats.session_comment_target = 0
            general_room = 0
            stats.karma_comment_note = (
                f"karma 0 and under {GENERAL_COMMENT_MIN_AGE_DAYS} days — no general comment"
            )
        has_sheet_post = bool(_sheet_post_communities(user_id, label, serial))
        sheet_target = (
            _primary_sheet_post_subreddit(user_id, label, serial)
            if has_sheet_post
            else ""
        )
        comment_slots: set = set()
        wait_h = hours_until_comment_room(user_id)
        log(
            f"[Profile {label}] 48h budget: {comment_room}/{COMMENTS_PER_WINDOW} comments, "
            f"{post_room}/{POSTS_PER_WINDOW} posts left"
        )
        if comment_room <= 0:
            log(
                f"[Profile {label}] No general comment on a random post this sitting — "
                f"{COMMENTS_PER_WINDOW}/{COMMENTS_PER_WINDOW} comments already used in "
                f"{ACTION_WINDOW_HOURS:.0f}h. Next slot in {wait_h:.1f}h"
            )
        elif _too_new_for_general_comment(stats):
            sheet_note = (
                f" comments.csv still has {sheet_slots} link(s)."
                if has_sheet_links
                else ""
            )
            log(
                f"[Profile {label}] No general comment — karma is 0 and the account "
                f"is under {GENERAL_COMMENT_MIN_AGE_DAYS} days old.{sheet_note}"
            )
        elif has_sheet_links:
            log(
                f"[Profile {label}] comments.csv has {sheet_slots} unused link(s) — "
                f"those comments use the 48h budget first"
                + (
                    f"; {general_room} general comment(s) on random posts if room remains"
                    if general_room
                    else ""
                )
            )
        else:
            log(
                f"[Profile {label}] comments.csv is empty — "
                f"{stats.session_comment_target} general comment(s) on random posts this sitting"
            )
        if has_sheet_post:
            where = f" r/{sheet_target}" if sheet_target else ""
            log(
                f"[Profile {label}] posts.csv has a draft{where} — "
                "Home first, join that community, Home again, then post later"
            )
        elif post_room > 0:
            log(
                f"[Profile {label}] posts.csv is empty — after rules and recent posts, "
                "one general post in a subreddits.csv community"
            )
        else:
            log(
                f"[Profile {label}] Post already used in the last {ACTION_WINDOW_HOURS:.0f}h — browse only"
            )
        sheet_n = max(1, len(browse_list))
        explore_target = (EXPLORE_SHARE * sheet_n) / (1.0 - EXPLORE_SHARE)
        want_explore = int(explore_target)
        if _rng().random() < (explore_target - want_explore):
            want_explore += 1
        expected_hops = max(1, len(browse_list) + want_explore)
        log(
            f"[Profile {label}] Exploration {EXPLORE_SHARE:.0%} — "
            f"{want_explore} extra communit{'y' if want_explore == 1 else 'ies'}, "
            f"{len(browse_list)} from subreddits.csv"
        )
        home_first, home_between = plan_home_budget(
            style.session_seconds,
            expected_hops,
            home_share=style.home_share,
        )
        style.home_first_seconds = home_first
        style.home_between = home_between
        home_seconds = 0.0
        community_seconds = 0.0
        community_budget = float(style.session_seconds) * float(style.community_share)
        log(
            f"[Profile {label}] Home gets {style.home_share:.0%} of this "
            f"{style.session_seconds / 60:.1f} min sitting — {home_first:.0f}s on Home first, "
            f"~{home_between[0]:.0f}–{home_between[1]:.0f}s back on Home per community, "
            f"max {MAX_SUBREDDIT_DWELL:.0f}s browsing each community and "
            f"{community_budget:.0f}s total inside communities"
        )
        home_seconds += hop_reddit_home(
            driver,
            label,
            user_id,
            stats,
            style.home_first_seconds,
            "first Home stretch",
        )
        home_seconds += maybe_search_posts_on_new(
            driver,
            label,
            stats,
            session_end - time.time(),
            max(0, int(style.search_n)),
        )
        explore: List[str] = []
        if EXPLORE_RANDOM_SUBS:
            # Sheet communities stay on the general-activity list. Random
            # explores skip whatever this account joined last sitting, then
            # those names are eligible again the sitting after that.
            cooldown = explored_subreddits(user_id, EXPLORE_SKIP_SESSIONS)
            if cooldown:
                log(
                    f"[Profile {label}] Skipping last sitting's random explores "
                    "this run: "
                    + ", ".join("r/" + name for name in sorted(cooldown))
                )
            avoid_explore = set(browse_list)
            avoid_explore.update(allowed_subreddits())
            avoid_explore.update(stats.joined)
            avoid_explore.update(stats.already_member)
            explore = discover_explore_subreddits(
                driver,
                label,
                avoid=avoid_explore,
                want=want_explore,
                karma=stats.account_karma,
                age_days=stats.account_age_days,
                seen=cooldown,
                user_id=user_id,
            )
            if explore:
                remember_explored_subreddits(user_id, explore)
                stats.explored = list(explore)
                log(
                    f"[Profile {label}] Will join {len(explore)} "
                    f"communit{'y' if len(explore) == 1 else 'ies'} "
                    f"found by search, suggestions, related communities, or a feed "
                    f"(skip next sitting): "
                    + ", ".join(f"r/{name}" for name in explore)
                )
        hops = [name for name in browse_list if not is_blocked_subreddit(name)]
        seen_hops = {name.lower() for name in hops}
        for name in explore:
            key = name.lower()
            if key in seen_hops or is_blocked_subreddit(name):
                continue
            hops.append(name)
            seen_hops.add(key)
        sheet_hops = [name for name in hops if name.lower() in allowed_set]
        other_hops = [name for name in hops if name.lower() not in allowed_set]
        need_sheet_first = bool(
            stats.session_comment_target
            or (COMMUNITY_GENERAL_POST and not has_sheet_post)
        )
        hops = sheet_hops + other_hops
        hops = [
            name
            for name in hops
            if name.lower() in allowed_set
            or name.lower() in {item.lower() for item in explore}
            or (sheet_target and name.lower() == sheet_target.lower())
        ]
        _rng().shuffle(hops)
        # A comment or general post still needs a sheet community early enough
        # to fit. Its slot is random inside the first half, not always first.
        if need_sheet_first and sheet_hops and len(hops) > 1:
            sheet_keys = {name.lower() for name in sheet_hops}
            first_sheet = next(
                (i for i, name in enumerate(hops) if name.lower() in sheet_keys),
                None,
            )
            window = max(1, (len(hops) + 1) // 2)
            if first_sheet is not None and first_sheet >= window:
                slot = _rng().randint(0, window - 1)
                moved = hops.pop(first_sheet)
                hops.insert(slot, moved)
        if sheet_target:
            hops = plan_sheet_post_return_hops(hops, sheet_target)
        if hops:
            _, style.home_between = plan_home_budget(
                style.session_seconds,
                len(hops),
                first_seconds=home_first,
                home_share=style.home_share,
            )
        remember_session_subreddits(user_id, hops)
        fingerprint = style.fingerprint(hops)
        remember_session_pattern(user_id, fingerprint)
        stats.session_style_note = (
            f"{style.summary()} | Home→"
            + "→Home→".join("r/" + name for name in hops)
        )
        comment_indexes = [
            i for i, name in enumerate(hops) if name.lower() in allowed_set
        ]
        if comment_indexes and stats.session_comment_target:
            slot_count = min(stats.session_comment_target, len(comment_indexes))
            comment_slots = set(_rng().sample(comment_indexes, slot_count))
        explore_keys = {item.lower() for item in explore}
        log(
            f"[Profile {label}] Random order ({EXPLORE_SHARE:.0%} exploration): "
            + " → ".join(
                "r/" + name + (" (explore)" if name.lower() in explore_keys else "")
                for name in hops
            )
            or "Home only"
        )
        if sheet_target:
            remaining = session_end - time.time()
            if remaining >= MIN_SESSION_LEFT_FOR_SUB + MIN_HOME_HOP:
                if _rng().random() < 0.55 and remaining > 90:
                    extra = _rng().uniform(
                        MIN_HOME_HOP,
                        min(42.0, remaining * _rng().uniform(0.08, 0.16)),
                    )
                    home_seconds += hop_reddit_home(
                        driver,
                        label,
                        user_id,
                        stats,
                        extra,
                        "before joining the post community",
                    )
                dwell = community_dwell_seconds(style, session_end - time.time())
                log(
                    f"[Profile {label}] Joining r/{sheet_target} after Home activity — "
                    "will go back to Home, then return later to post"
                )
                try:
                    used = visit_sheet_post_community(
                        driver,
                        label,
                        user_id,
                        stats,
                        sheet_target,
                        dwell,
                        join=True,
                        reason="join the community to post in",
                    )
                    community_seconds += used
                    sheet_post_joined[0] = True
                except Exception as exc:
                    log(
                        f"[Profile {label}] Could not join r/{sheet_target} yet "
                        f"({brief_error(exc)}) — will try again later"
                    )
                next_is_post = bool(hops) and hops[0].lower() == sheet_target.lower()
                lo, hi = style.home_between
                if next_is_post:
                    hi = max(hi, lo + _rng().uniform(10, 32))
                home_gap = min(
                    _rng().uniform(lo, hi),
                    max(MIN_HOME_HOP, session_end - time.time() - MIN_HOME_HOP),
                )
                if home_gap >= MIN_HOME_HOP and session_end - time.time() > MIN_HOME_HOP:
                    home_seconds += hop_reddit_home(
                        driver,
                        label,
                        user_id,
                        stats,
                        home_gap,
                        "after joining the post community",
                    )
            else:
                log(
                    f"[Profile {label}] Not enough sitting left to join r/{sheet_target} "
                    "before the hops — will still post later if there is time"
                )
        deferred_hops: set = set()
        for index, subreddit in enumerate(hops, start=1):
            remaining = session_end - time.time()
            if remaining < MIN_SESSION_LEFT_FOR_SUB:
                log(f"[Profile {label}] Session time is almost up — not joining more subreddits")
                break
            if is_blocked_subreddit(subreddit):
                log(f"[Profile {label}] Skipping r/{subreddit} — on the blocked list")
                continue
            if community_budget - community_seconds < MIN_SUBREDDIT_DWELL:
                log(
                    f"[Profile {label}] Community time used up "
                    f"({community_seconds:.0f}s/{community_budget:.0f}s) — "
                    "staying on Home for the rest of the sitting"
                )
                break
            is_sheet = subreddit.lower() in allowed_set
            comment_planned = (
                is_sheet
                and (index - 1) in comment_slots
                and (stats.comments < stats.session_comment_target)
            )
            # Share what community time is left across the hops still to come, so
            # early communities cannot use up the budget and starve the explores.
            # A hop that will carry a comment needs the reserve; the quick explore
            # tastes split whatever is left over.
            budget_now = community_budget - community_seconds
            rest = range(index, len(hops))
            comment_hops_left = sum(1 for i in rest if i in comment_slots)
            taste_hops_left = max(0, len(hops) - index - comment_hops_left)
            if comment_planned:
                hop_allowance = max(
                    COMMENT_FLOW_RESERVE + MIN_SUBREDDIT_DWELL,
                    budget_now / float(max(1, len(hops) - index + 1)),
                )
            else:
                spare = budget_now - comment_hops_left * (
                    COMMENT_FLOW_RESERVE + MIN_SUBREDDIT_DWELL
                )
                hop_allowance = max(
                    MIN_SUBREDDIT_DWELL, spare / float(taste_hops_left + 1)
                )
            origin = "from subreddits.csv" if is_sheet else "random explore"
            log(
                f"[Profile {label}] {'Sheet' if is_sheet else 'Exploring'} r/{subreddit} "
                f"({index}/{len(hops)}) {origin}, ~{hop_allowance:.0f}s allowance"
            )
            raise_profile_browser(label)
            # Clock the whole visit — feed, rules, threads, comment flow — so
            # extras inside a community cannot stretch the time spent there.
            sub_started = time.time()
            try:
                url = community_entry_url(subreddit)
                wait_for_free = min(
                    SUBREDDIT_BUSY_WAIT,
                    max(0.0, (session_end - time.time()) - MIN_SESSION_LEFT_FOR_SUB),
                )
                if not open_exclusive_community(
                    driver,
                    label,
                    user_id,
                    subreddit,
                    url,
                    wait=wait_for_free,
                ):
                    key = subreddit.lower()
                    if key not in deferred_hops:
                        deferred_hops.add(key)
                        hops.append(subreddit)
                        log(
                            f"[Profile {label}] Will try r/{subreddit} again later this sitting"
                        )
                    else:
                        log(
                            f"[Profile {label}] Skipping r/{subreddit} — another account "
                            "still has it open"
                        )
                    continue
                log(
                    f"[Profile {label}] Joining r/{subreddit} for "
                    f"{'general activity' if is_sheet else 'random explore'} via {url}"
                )
                dismiss_popups(driver)
                try:
                    WebDriverWait(driver, ELEMENT_WAIT).until(
                        EC.presence_of_element_located((By.TAG_NAME, "body"))
                    )
                except TimeoutException:
                    log(f"[Profile {label}] r/{subreddit} body did not appear — continuing")

                # The rules page costs real seconds. Open it where it matters —
                # sheet communities and anywhere a comment is planned — and read
                # cached/JSON rules only for a quick explore taste.
                rules = read_subreddit_rules(
                    driver,
                    label,
                    subreddit,
                    stats,
                    open_page=is_sheet or comment_planned,
                )
                try:
                    read_state = _rl_state(stats, subreddit, kind="rules", rules=rules)
                    if getattr(rules, "rule_count", 0):
                        _rl_learn(stats, read_state, "rules:read", REWARD_RULES_READ)
                    else:
                        _rl_learn(stats, read_state, "rules:missing", 0.0)
                except Exception as exc:
                    log(
                        f"[Profile {label}] RL after r/{subreddit} rules skipped "
                        f"({brief_error(exc)})"
                    )

                try:
                    join_status = join_subreddit(driver, label, subreddit)
                    if join_status == "joined":
                        stats.joined.append(subreddit)
                    elif join_status == "already_joined":
                        stats.already_member.append(subreddit)
                    else:
                        stats.join_failed.append(subreddit)
                    policy = decide_join_policy(stats, subreddit, join_status, rules)
                except Exception as exc:
                    join_status = "failed"
                    policy = "comment"
                    log(
                        f"[Profile {label}] Join on r/{subreddit} skipped "
                        f"({brief_error(exc)}) — still browsing"
                    )

                dwell = community_dwell_seconds(style, session_end - time.time())
                # Do not let the feed browse alone eat this hop's allowance
                budget_left = (
                    hop_allowance
                    - (time.time() - sub_started)
                    - (COMMENT_FLOW_RESERVE if comment_planned else 0.0)
                )
                dwell = min(dwell, max(MIN_SUBREDDIT_DWELL, budget_left))
                log(
                    f"[Profile {label}] {dwell:.0f}s quick look inside r/{subreddit}, "
                    "then back to Home"
                )
                perform_browse_activity(
                    driver,
                    dwell,
                    label,
                    user_id=user_id,
                    stats=stats,
                    stay_url=url,
                )
                have_week, room = general_comment_need(
                    user_id, session_want=stats.session_comment_target
                )
                still_need = min(
                    room, max(0, stats.session_comment_target - stats.comments)
                )
                remaining_subs = sum(
                    1
                    for name in hops[index - 1 :]
                    if name.lower() in allowed_set
                )
                should_comment = (
                    is_sheet
                    and still_need > 0
                    and ((index - 1) in comment_slots or still_need >= remaining_subs)
                )
                if policy == "lurk" and still_need > 0 and not should_comment:
                    log(
                        f"[Profile {label}] RL chose lurk in r/{subreddit} after reading rules "
                        f"— browse only this community"
                    )
                elif policy == "lurk" and should_comment:
                    log(
                        f"[Profile {label}] RL chose lurk in r/{subreddit} — still commenting "
                        "because this sitting still needs a general comment"
                    )
                if should_comment:
                    want = 1
                    if still_need >= 2 and remaining_subs <= 1:
                        want = min(2, still_need)
                    sorts = next_comment_sorts(stats, want)
                    log(
                        f"[Profile {label}] General comment on a new r/{subreddit} post "
                        f"({stats.comments + 1}/{stats.session_comment_target} this run, "
                        f"{have_week}/{COMMENTS_PER_WINDOW} in {ACTION_WINDOW_HOURS:.0f}h)"
                    )
                    try:
                        leave_subreddit_comments(
                            driver, label, user_id, stats, subreddit, want, sorts=sorts
                        )
                    except Exception as exc:
                        log(
                            f"[Profile {label}] Comment on r/{subreddit} failed "
                            f"({brief_error(exc)}) — staying in the sitting"
                        )
                else:
                    if still_need <= 0:
                        wait_h = hours_until_comment_room(user_id)
                        extra = (
                            f" — next random-post comment in {wait_h:.1f}h"
                            if wait_h > 0
                            else ""
                        )
                        log(
                            f"[Profile {label}] No general comment on a random post in r/{subreddit} "
                            f"({have_week}/{COMMENTS_PER_WINDOW} in {ACTION_WINDOW_HOURS:.0f}h{extra})"
                        )
                    else:
                        log(
                            f"[Profile {label}] Browse only in r/{subreddit} "
                            f"({stats.comments}/{stats.session_comment_target} "
                            "general comments this run)"
                        )
                spent_here = time.time() - sub_started
                if spent_here < hop_allowance:
                    try:
                        topics = collect_community_topics(driver, label, subreddit)
                        if topics:
                            stats.subreddit_topics[subreddit] = topics
                            log(
                                f"[Profile {label}] Read {len(topics)} recent r/{subreddit} "
                                "posts to learn what this community talks about"
                            )
                    except Exception as exc:
                        log(
                            f"[Profile {label}] Could not read r/{subreddit} feed "
                            f"({brief_error(exc)})"
                        )
                else:
                    log(
                        f"[Profile {label}] Skipping the r/{subreddit} topic read — "
                        "community time budget is spent"
                    )
                if not session_post_done[0]:
                    try:
                        sheet_subs = {
                            item.lower()
                            for item in _sheet_post_communities(user_id, label, serial)
                        }
                        if sheet_subs and subreddit.lower() in sheet_subs:
                            if sheet_post_joined[0]:
                                log(
                                    f"[Profile {label}] Back on r/{subreddit} "
                                    "(posts.csv) — activity done, adding the post"
                                )
                            else:
                                log(
                                    f"[Profile {label}] posts.csv is for r/{subreddit} — "
                                    "posting after activity here"
                                )
                            maybe_do_session_post(
                                driver, label, user_id, stats, serial, session_post_done,
                                preferred_sub=subreddit,
                            )
                        elif (
                            not sheet_subs
                            and is_sheet
                            and COMMUNITY_GENERAL_POST
                            and (session_end - time.time()) > 40
                        ):
                            log(
                                f"[Profile {label}] posts.csv empty — drafting a general "
                                f"post for r/{subreddit} from its rules and recent posts"
                            )
                            maybe_general_community_post(
                                driver,
                                label,
                                user_id,
                                stats,
                                subreddit,
                                done=session_post_done,
                                serial=serial,
                            )
                    except Exception as exc:
                        log(
                            f"[Profile {label}] Post in r/{subreddit} skipped "
                            f"({brief_error(exc)}) — sitting continues"
                        )
                community_seconds += time.time() - sub_started
                log(
                    f"[Profile {label}] Left r/{subreddit} after "
                    f"{time.time() - sub_started:.0f}s "
                    f"({community_seconds:.0f}s/{community_budget:.0f}s community time used)"
                )
                more_communities = index < len(hops)
                home_gap = min(
                    _rng().uniform(*style.home_between),
                    max(MIN_HOME_HOP, session_end - time.time() - MIN_HOME_HOP),
                )
                skip_home = (not style.hop_home_between) or (
                    _rng().random() < float(style.home_skip_chance or 0)
                )
                if more_communities and home_gap >= MIN_HOME_HOP and not skip_home:
                    home_seconds += hop_reddit_home(
                        driver,
                        label,
                        user_id,
                        stats,
                        home_gap,
                        f"between communities ({index}/{len(hops)})",
                    )
                elif more_communities and skip_home:
                    log(
                        f"[Profile {label}] Skipping Home between r/{subreddit} and "
                        "the next community this sitting"
                    )
                if not session_post_done[0] and time.time() >= post_mid_at:
                    if sheet_target:
                        log(
                            f"[Profile {label}] {style.post_fraction:.0%} through the "
                            f"{style.session_seconds / 60:.0f} min sitting — still waiting "
                            f"to return to r/{sheet_target} before posting"
                        )
                    else:
                        log(
                            f"[Profile {label}] {style.post_fraction:.0%} through the "
                            f"{style.session_seconds / 60:.0f} min sitting — general post "
                            "waits for a subreddits.csv community"
                        )
            except Exception as exc:
                community_seconds += time.time() - sub_started
                log(f"[Profile {label}] Error on r/{subreddit}: {brief_error(exc)} — back to Home")
                try:
                    open_reddit_home_ready(driver, label)
                except Exception:
                    pass
                continue

        leftover = session_end - time.time()
        if leftover > MIN_LEFTOVER_HOME:
            home_seconds += hop_reddit_home(
                driver,
                label,
                user_id,
                stats,
                leftover,
                "leftover sitting on Home",
            )
        else:
            log(
                f"[Profile {label}] Finished ~{(style.session_seconds if style else ACCOUNT_SESSION_SECONDS) / 60:.0f} min "
                f"Home → community hops"
            )
        tracked = home_seconds + community_seconds
        if tracked > 0:
            log(
                f"[Profile {label}] Time split: {home_seconds:.0f}s on Home "
                f"({home_seconds / tracked:.0%}), {community_seconds:.0f}s inside "
                f"communities ({community_seconds / tracked:.0%})"
            )
        stats.scrolled_px = session_scrolled_px()
        if _wheel_is_live() is False or getattr(_tls, "gesture_ok", None) is False:
            stats.scroll_engine = "smooth js (wheel input was ignored)"
        elif getattr(_tls, "gesture_ok", None):
            stats.scroll_engine = "momentum gesture"
        elif _wheel_is_live():
            stats.scroll_engine = "wheel ticks"
        log(
            f"[Profile {label}] Scrolled {stats.scrolled_px / 1000.0:.1f}k px this "
            f"sitting via {stats.scroll_engine or 'unknown engine'}"
        )
        # Browsing is done — release the clamp so the post / comment flows below
        # keep their natural typing and review pacing.
        set_session_deadline(None)
        if not session_post_done[0] and sheet_target:
            try:
                extra = community_dwell_seconds(style, _rng().uniform(18.0, 36.0))
                log(
                    f"[Profile {label}] Back to r/{sheet_target} after sitting activity — "
                    "browse, then add the posts.csv post"
                )
                visit_sheet_post_community(
                    driver,
                    label,
                    user_id,
                    stats,
                    sheet_target,
                    extra,
                    join=not sheet_post_joined[0],
                    reason="back to add the post",
                )
            except Exception as exc:
                log(
                    f"[Profile {label}] Return to r/{sheet_target} skipped "
                    f"({brief_error(exc)}) — still trying to post"
                )
            maybe_do_session_post(
                driver, label, user_id, stats, serial, session_post_done,
                preferred_sub=sheet_target,
            )
        elif not session_post_done[0]:
            maybe_do_session_post(
                driver, label, user_id, stats, serial, session_post_done,
                preferred_sub=(browse_list[0] if browse_list else ""),
            )

        maybe_leave_weekly_comments(
                    driver,
                    label,
                    user_id,
                    stats,
                    serial=serial,
                )
        if stats.comments < stats.session_comment_target:
            maybe_karma_growth_comments(
                driver,
                label,
                user_id,
                stats,
                session_subs=browse_list or allowed_subreddits(),
                time_budget=max(45.0, community_budget - community_seconds),
            )
    except Exception as exc:
        log(f"[Profile {label}] FAILED — {brief_error(exc)}")
    finally:
        release_all_held_subreddits(user_id)
        set_current_style(None)
        set_session_deadline(None)
        end_session_rng()
        close_browser(driver, user_id, label)
        agent = _rl_agent()
        if agent is not None:
            report = agent.get_performance_report()
            stats.rl_epsilon = float(report.get("epsilon") or stats.rl_epsilon)
            graded = int(report.get("graded_actions") or 0)
            if graded:
                quality = (
                    f"graded {graded} | "
                    f"avg graded reward {report.get('graded_average_reward', 0):+.2f}"
                )
            else:
                quality = "no graded outcomes yet — nothing real to learn from"
            stats.rl_note = (
                f"lifetime actions {report.get('actions', 0)} | {quality} | "
                f"trained {report.get('train_steps', 0)} steps | "
                f"replay {report.get('replay', 0)} | "
                f"buckets {report.get('prior_buckets', 0)}"
            )
            if rl_check_blocked_total():
                stats.rl_note += f" | {rl_check_blocked_total()} outcome checks blocked"
        stats.print_report()

    return stats


def run_profiles(
    targets: List[Dict[str, str]],
    subreddits: List[str],
) -> List[AccountSummary]:
    """Run every AdsPower account. Parallel mode opens them together (multitask)."""
    schedules = assign_profile_schedules(targets)
    log("Assigned a random sitting time and start time for each profile:")
    for profile in targets:
        sch = schedules.get(profile["user_id"]) or {}
        label = profile.get("name") or profile["user_id"]
        log(
            f"  {label}: {sch.get('session_seconds', ACCOUNT_SESSION_SECONDS) / 60:.0f} min sitting, "
            f"opens in {sch.get('start_delay', 0) / 60:.1f} min"
        )

    if not PARALLEL_PROFILES or len(targets) <= 1:
        reports: List[AccountSummary] = []
        for index, profile in enumerate(targets):
            sch = schedules.get(profile["user_id"]) or {}
            label = profile.get("name") or profile["user_id"]
            delay = float(sch.get("start_delay") or 0)
            log("-" * 56)
            log(f"Profile {label}: {index + 1}/{len(targets)}")
            if delay > 1:
                log(f"[Profile {label}] Waiting {delay / 60:.1f} min before opening")
                time.sleep(delay)
            reports.append(
                process_profile(
                    profile,
                    subreddits,
                    assigned_seconds=sch.get("session_seconds"),
                )
            )
            if index < len(targets) - 1:
                gap = _rng().uniform(*DELAY_BETWEEN_PROFILES)
                log(f"Waiting {gap:.1f}s before the next profile")
                time.sleep(gap)
        return reports

    workers = MAX_PARALLEL_PROFILES or len(targets)
    workers = max(1, min(int(workers), len(targets)))
    log(
        f"Multitask: {len(targets)} accounts, {workers} open at a time "
        "(each has its own start time)"
    )
    slots: List[Optional[AccountSummary]] = [None] * len(targets)

    def worker(index: int, profile: Dict[str, str]) -> None:
        label = profile.get("name") or profile["user_id"]
        sch = schedules.get(profile["user_id"]) or {}
        delay = float(sch.get("start_delay") or 0)
        log("-" * 56)
        log(f"Profile {label}: {index + 1}/{len(targets)} queued")
        if delay > 1:
            log(f"[Profile {label}] Assigned start in {delay / 60:.1f} min — waiting")
            time.sleep(delay)
        log(f"Profile {label}: starting now")
        try:
            slots[index] = process_profile(
                profile,
                subreddits,
                assigned_seconds=sch.get("session_seconds"),
            )
        except Exception as exc:
            log(f"[Profile {label}] FAILED — {brief_error(exc)}")
            failed = AccountSummary(name=label, user_id=profile["user_id"])
            failed.post_note = brief_error(exc)
            slots[index] = failed

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = []
        for index, profile in enumerate(targets):
            futures.append(pool.submit(worker, index, profile))
            if index < len(targets) - 1:
                time.sleep(_rng().uniform(1.6, 4.2))
        for future in as_completed(futures):
            future.result()
    return [item for item in slots if item is not None]


# =============================================================================
# MAIN
# =============================================================================

def main() -> int:
    global PROFILE_IDS, SUBREDDITS, SUBREDDITS_TO_BROWSE
    disk_error = require_disk_space()
    if disk_error:
        log(disk_error)
        return 1
    ensure_accounts_sheet()
    ensure_subreddits_sheet()
    PROFILE_IDS = load_profile_ids()
    sheet_subs = load_browse_subreddits()
    SUBREDDITS = sheet_subs
    SUBREDDITS_TO_BROWSE = sheet_subs
    if not PROFILE_IDS:
        log("No accounts — add AdsPower user_ids in accounts.csv (enabled=yes)")
        return 1
    if not SUBREDDITS:
        log("No subreddits — add community names in subreddits.csv (enabled=yes)")
        return 1

    log(f"AdsPower API: {ADSPOWER_API}")
    log(
        f"Only these accounts ({os.path.basename(ACCOUNTS_SHEET)}): {', '.join(PROFILE_IDS)}"
    )
    log(
        f"Only these communities ({os.path.basename(SUBREDDITS_SHEET)}): "
        + ", ".join("r/" + name for name in SUBREDDITS)
    )
    log(
        f"Profiles: {len(PROFILE_IDS)} from {os.path.basename(ACCOUNTS_SHEET)} "
        f"({'all open together' if PARALLEL_PROFILES else 'one by one'}) | "
        f"Browse extras: {len(SUBREDDITS)} from {os.path.basename(SUBREDDITS_SHEET)} | "
        f"{SUBREDDITS_PER_SESSION[0]}–{SUBREDDITS_PER_SESSION[1]} general "
        f"(AskUK / NoStupidQuestions / Advice) + "
        f"{RANDOM_EXPLORE_PER_SESSION[0]}–{RANDOM_EXPLORE_PER_SESSION[1]} random explore "
        f"(skip {EXPLORE_SKIP_SESSIONS} sitting per account)"
    )
    log(
        f"Each account: own random sitting ({SESSION_SECONDS_RANGE[0] // 60}–"
        f"{SESSION_SECONDS_RANGE[1] // 60} min, Home "
        f"{int(HOME_ACTIVITY_SHARE_RANGE[0]*100)}–{int(HOME_ACTIVITY_SHARE_RANGE[1]*100)}%) | "
        f"short community visits (max {MAX_SUBREDDIT_DWELL:.0f}s each) | "
        f"{COMMENTS_PER_WINDOW} comments / {ACTION_WINDOW_HOURS:.0f}h | "
        f"{POSTS_PER_WINDOW} post / {ACTION_WINDOW_HOURS:.0f}h"
    )
    try:
        from reddit_joiner.store import init_db

        init_db()
        log("Activity database: activity.db | Live posts: live_posts.xlsx")
    except Exception as exc:
        log(f"Could not init activity.db ({exc})")
    if RL_ENABLED:
        try:
            from reddit_joiner.rl import RLAgent, set_agent

            agent = RLAgent(
                model_file=RL_MODEL_FILE,
                epsilon=RL_EPSILON_INIT,
                epsilon_min=RL_EPSILON_MIN,
                epsilon_decay=RL_EPSILON_DECAY,
                alpha=RL_LEARNING_RATE,
                gamma=RL_DISCOUNT_FACTOR,
                save_interval=RL_SAVE_INTERVAL,
                replay_size=RL_REPLAY_SIZE,
                batch_size=RL_BATCH_SIZE,
                target_sync=RL_TARGET_SYNC,
                dqn_lr=RL_DQN_LR,
            )
            set_agent(agent)
            agent.install_handlers()
            # Epsilon used to be forced back to 0.22 here on every run, which
            # threw away the decay schedule and kept a fifth of all actions
            # random forever. Exploration is now handled by the UCB bonus, which
            # targets the actions that are actually short of data.
            report = agent.get_performance_report()
            kind = str(report.get("kind") or "dqn")
            if getattr(agent, "arch_reset", False):
                log(
                    f"Deep RL ({kind}) starting a new network "
                    f"(ε={agent.epsilon:.3f}) — state now includes subreddit rules"
                )
            elif os.path.isfile(RL_MODEL_FILE) and int(report.get("actions") or 0) > 0:
                log(
                    f"Deep RL ({kind}) loaded — {report.get('actions', 0)} actions, "
                    f"{report.get('graded_actions', 0)} graded, "
                    f"replay {report.get('replay', 0)}, "
                    f"{report.get('train_steps', 0)} train steps, ε={agent.epsilon:.3f} "
                    "— learning comment tone, join/lurk, and subreddit rules"
                )
                top = agent.action_breakdown(5)
                if top:
                    summary = ", ".join(
                        f"{row['action']} {row['mean']:+.1f} (n={row['n']})" for row in top
                    )
                    log(f"Deep RL measured so far — {summary}")
            else:
                log(
                    f"Deep RL ({kind}) starting a new network "
                    f"(ε={agent.epsilon:.3f}) — learning comment tone and subreddit rules"
                )
            try:
                applied = apply_rl_status_rewards()
                if applied:
                    log(
                        f"Deep RL scored {applied} earlier comment/post statuses "
                        "(live / removed / score) and updated the network"
                    )
            except Exception as exc:
                log(f"RL delayed rewards skipped ({brief_error(exc)})")
        except Exception as exc:
            log(f"RL unavailable this run ({brief_error(exc)}) — posting continues without it")
            try:
                from reddit_joiner.rl import set_agent

                set_agent(None)
            except Exception:
                pass
    try:
        from reddit_joiner.omniroute import ensure_omniroute
        from reddit_joiner.ai import omniroute_base, omniroute_ready, ollama_model_name, ollama_ready

        ensure_omniroute(log)
        if os.environ.get("OPENAI_API_KEY", "").strip():
            model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini").strip() or "gpt-4o-mini"
            log(f"Comments: OpenAI {model} (local models only if that request fails)")
        elif omniroute_ready():
            log(f"Comments: OmniRoute at {omniroute_base()}/v1 (then Ollama {ollama_model_name()})")
        elif ollama_ready():
            log(f"Comments: Ollama {ollama_model_name()}")
        else:
            log("OmniRoute and Ollama are down. Cloud keys are only a fallback.")
    except Exception:
        if not (
            os.environ.get("HF_TOKEN")
            or os.environ.get("HUGGINGFACE_HUB_TOKEN")
            or os.environ.get("DEEPSEEK_API_KEY")
            or os.environ.get("OPENROUTER_API_KEY")
            or os.environ.get("GROQ_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
            or os.environ.get("GEMINI_API_KEY")
            or os.environ.get("GOOGLE_API_KEY")
        ):
            log(
                "Ollama is not available — start Ollama (`ollama pull mistral:7b`) "
                "or add a cloud API key. Comments will otherwise use a local fallback."
            )
    ensure_posts_sheet()
    pending = count_pending_posts()
    log(f"Posts sheet: {POSTS_SHEET} ({pending} unused post{'s' if pending != 1 else ''})")
    ensure_comments_sheet()
    ensure_commented_links_sheet()
    pending_comments = count_pending_comment_links()
    log(
        f"Comments sheet: {COMMENTS_SHEET} "
        f"({pending_comments} unused post link{'s' if pending_comments != 1 else ''})"
    )
    log(
        f"After each comment, that link is saved in {COMMENTED_LINKS_SHEET}"
    )
    if KARMA_GROWTH_ENABLED:
        log(
            f"Per account: {SESSION_SECONDS_RANGE[0] // 60}–{SESSION_SECONDS_RANGE[1] // 60} min sitting, "
            f"{COMMENTS_PER_WINDOW} comments/{ACTION_WINDOW_HOURS:.0f}h, "
            f"{POSTS_PER_WINDOW} post/{ACTION_WINDOW_HOURS:.0f}h "
            f"(sheets first, then general; karma >= {MIN_KARMA_TO_POST} to post)"
        )
    log(
        f"Each sitting is a new random mix: {SESSION_SECONDS_RANGE[0] // 60}–"
        f"{SESSION_SECONDS_RANGE[1] // 60} min, Home "
        f"{int(HOME_ACTIVITY_SHARE_RANGE[0]*100)}–{int(HOME_ACTIVITY_SHARE_RANGE[1]*100)}%, "
        f"community visits under {MAX_SUBREDDIT_DWELL:.0f}s. "
        f"Each account: {COMMENTS_PER_WINDOW} comments and {POSTS_PER_WINDOW} post "
        f"per {ACTION_WINDOW_HOURS:.0f}h. "
        "If comments.csv / posts.csv have unused rows, those go first; "
        "if posts.csv is empty, one general post is written in a subreddits.csv "
        "community after its rules and recent posts are read. "
        "General comments stay on subreddits.csv. "
        "Random explores are skipped for one sitting, then allowed again. "
        "RL picks comment tone and learns from the result."
    )

    if not adspower_api_ready():
        log(
            "AdsPower Local API is not reachable. Leave AdsPower open and enable "
            f"Local API at {ADSPOWER_API}"
        )
        return 1

    if not PARALLEL_PROFILES:
        try:
            focus_adspower_window()
        except Exception as exc:
            log(f"Could not bring AdsPower to the front ({brief_error(exc)})")

    targets: List[Dict[str, str]] = []
    for raw in PROFILE_IDS:
        try:
            targets.append(resolve_profile_id(raw))
        except Exception as exc:
            log(f"Could not resolve {raw!r}: {brief_error(exc)} — skipping")

    if not targets:
        log("No AdsPower profiles could be resolved")
        return 1

    assigned = sync_comment_sheet_assignments(targets)
    if assigned:
        log(f"Assigned {assigned} comments.csv link(s) to accounts")
    print_comments_sheet_status()

    reports = run_profiles(targets, SUBREDDITS)

    if RL_ENABLED and _rl_agent() is not None:
        try:
            later = apply_rl_status_rewards()
            if later:
                log(
                    f"Deep RL scored {later} comment/post statuses after the sitting "
                    "and updated the network"
                )
        except Exception as exc:
            log(f"RL status rewards after run skipped ({brief_error(exc)})")

    log("")
    log("FINAL SUMMARY BY ACCOUNT")
    for line in session_tally_lines(reports):
        log(line)
    for item in reports:
        item.print_report()
    try:
        summary_path = write_session_summary(reports)
        log(f"Session summary saved: {summary_path}")
    except Exception as exc:
        log(f"Could not write session summary txt ({brief_error(exc)})")
    log("")
    print_posts_sheet_status()
    print_comments_sheet_status()
    agent = _rl_agent()
    if agent is not None:
        try:
            agent.save_model()
            agent.save_experience_to_db()
            agent.log_performance()
            report = agent.get_performance_report()
            log(
                f"Deep RL saved to {os.path.basename(RL_MODEL_FILE)} — "
                f"{report['actions']} actions | success {report['success_rate']:.0%} | "
                f"avg reward {report['average_reward']:.2f} | ε={report['epsilon']:.3f} | "
                f"replay {report.get('replay', 0)}"
            )
        except Exception as exc:
            log(f"Could not save RL model ({brief_error(exc)})")
    log("All profiles processed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
