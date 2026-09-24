"""Project folders: sheets you edit, data the bot writes, code in this package."""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SHEETS_DIR = PROJECT_ROOT / "sheets"
DATA_DIR = PROJECT_ROOT / "data"
OMNIROUTE_DIR = PROJECT_ROOT / "omniroute"
ENV_FILE = PROJECT_ROOT / ".env"

ACCOUNTS_SHEET = SHEETS_DIR / "accounts.csv"
SUBREDDITS_SHEET = SHEETS_DIR / "subreddits.csv"
COMMENTS_SHEET = SHEETS_DIR / "comments.csv"
COMMENTED_LINKS_SHEET = SHEETS_DIR / "commented_links.csv"
POSTS_SHEET = SHEETS_DIR / "posts.csv"
POST_TEMPLATES_DIR = SHEETS_DIR / "post_templates"
PROFILES_FILE = SHEETS_DIR / "profiles.txt"

DB_PATH = DATA_DIR / "activity.db"
LIVE_POSTS_XLSX = DATA_DIR / "live_posts.xlsx"
RL_MODEL_FILE = DATA_DIR / "rl_model.pkl"
PROFILE_CACHE_PATH = DATA_DIR / "adspower_profile_cache.json"
POST_LOG_PATH = DATA_DIR / "adspower_post_log.json"
COMMENT_LOG_PATH = DATA_DIR / "adspower_comment_log.json"
SUBREDDIT_LOG_PATH = DATA_DIR / "adspower_subreddit_log.json"
RULES_CACHE_PATH = DATA_DIR / "subreddit_rules.json"
JOINER_LOG = DATA_DIR / "joiner_run.log"
SESSION_PATTERNS_PATH = DATA_DIR / "session_patterns.json"
SUMMARIES_DIR = DATA_DIR / "summaries"


def ensure_runtime_dirs() -> None:
    SHEETS_DIR.mkdir(parents=True, exist_ok=True)
    POST_TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SUMMARIES_DIR.mkdir(parents=True, exist_ok=True)
    OMNIROUTE_DIR.mkdir(parents=True, exist_ok=True)
