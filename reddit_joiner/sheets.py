#!/usr/bin/env python3
"""accounts.csv / subreddits.csv helpers and outside-lookup skip list."""

from __future__ import annotations

import csv
import os
import threading
from pathlib import Path
from typing import Dict, List, Tuple

from reddit_joiner.paths import (
    ACCOUNTS_SHEET as _ACCOUNTS_SHEET,
    DATA_DIR as _DATA_DIR,
    PROJECT_ROOT as _PROJECT_ROOT,
    SUBREDDITS_SHEET as _SUBREDDITS_SHEET,
)

ACCOUNTS_SHEET = str(_ACCOUNTS_SHEET)
SUBREDDITS_SHEET = str(_SUBREDDITS_SHEET)
ACCOUNTS_SHEET_FIELDS = ["user_id", "enabled", "name", "notes"]
SUBREDDITS_SHEET_FIELDS = ["subreddit", "enabled", "notes"]
_DEFAULT_PROFILE_IDS: List[str] = [
    "k1g5mjmi",
    "k1g5mjmh",
]
_SHEET_LOCK = threading.RLock()


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


def disable_account_in_sheet(user_id: str, reason: str = "") -> bool:
    key = str(user_id or "").strip()
    if not key:
        return False
    with _SHEET_LOCK:
        headers, rows = _read_simple_csv(ACCOUNTS_SHEET, ACCOUNTS_SHEET_FIELDS)
        changed = False
        for row in rows:
            found = _csv_lookup(row, "user_id", "account", "profile", "ads_power", "id")
            if found.lower() != key.lower():
                continue
            if _row_is_enabled(row):
                row["enabled"] = "no"
                changed = True
            if reason and not str(row.get("notes") or "").strip():
                row["notes"] = reason[:160]
                changed = True
        if changed:
            fields = list(headers) if headers else list(ACCOUNTS_SHEET_FIELDS)
            for name in ACCOUNTS_SHEET_FIELDS:
                if name not in fields:
                    fields.append(name)
            _write_simple_csv(ACCOUNTS_SHEET, fields, rows)
        return changed


def apply_dead_status_to_accounts_sheet() -> int:
    """Turn enabled=no for every sheet row whose outside status is BANNED or SUSPENDED."""
    try:
        from reddit_joiner.store import DEAD_ACCOUNT_STATUSES, get_account_outside_status
    except Exception:
        return 0
    with _SHEET_LOCK:
        headers, rows = _read_simple_csv(ACCOUNTS_SHEET, ACCOUNTS_SHEET_FIELDS)
        changed = 0
        for row in rows:
            user_id = _csv_lookup(row, "user_id", "account", "profile", "ads_power", "id")
            if not user_id:
                continue
            status = get_account_outside_status(user_id)
            if status not in DEAD_ACCOUNT_STATUSES:
                continue
            if _row_is_enabled(row):
                row["enabled"] = "no"
                changed += 1
            if not str(row.get("notes") or "").strip():
                row["notes"] = f"outside lookup {status}"
        if changed:
            fields = list(headers) if headers else list(ACCOUNTS_SHEET_FIELDS)
            for name in ACCOUNTS_SHEET_FIELDS:
                if name not in fields:
                    fields.append(name)
            _write_simple_csv(ACCOUNTS_SHEET, fields, rows)
        return changed


def load_outside_lookup_reports() -> int:
    """Read checker reports from the project root into activity.db."""
    try:
        from reddit_joiner.store import import_brand_lookup_file
    except Exception:
        return 0
    loaded = 0
    root = Path(_PROJECT_ROOT)
    paths = sorted(root.glob("brand-lookup-*.txt")) + sorted(Path(_DATA_DIR).glob("brand-lookup-*.txt"))
    seen = set()
    for path in paths:
        key = str(path.resolve())
        if key in seen:
            continue
        seen.add(key)
        try:
            loaded += int(import_brand_lookup_file(str(path)) or 0)
        except Exception:
            continue
    return loaded


def persist_inside_ban(user_id: str, reason: str = "") -> None:
    """Inside check found a ban: store it and disable the sheet row. Never writes LIVE."""
    text = str(reason or "").lower()
    status = "SUSPENDED" if "suspend" in text else "BANNED"
    try:
        from reddit_joiner.store import record_account_outside_status

        record_account_outside_status(user_id, status, source="inside")
    except Exception:
        pass
    try:
        disable_account_in_sheet(user_id, reason=f"inside check {status}")
    except Exception:
        pass
