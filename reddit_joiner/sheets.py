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
    TARGETED_ACCOUNTS_SHEET as _TARGETED_ACCOUNTS_SHEET,
    TARGETED_RELATED_SHEET as _TARGETED_RELATED_SHEET,
    TARGETED_SUBREDDITS_SHEET as _TARGETED_SUBREDDITS_SHEET,
)

ACCOUNTS_SHEET = str(_ACCOUNTS_SHEET)
SUBREDDITS_SHEET = str(_SUBREDDITS_SHEET)
GENERAL_ACCOUNTS_SHEET = str(_ACCOUNTS_SHEET)
GENERAL_SUBREDDITS_SHEET = str(_SUBREDDITS_SHEET)
TARGETED_ACCOUNTS_SHEET = str(_TARGETED_ACCOUNTS_SHEET)
TARGETED_SUBREDDITS_SHEET = str(_TARGETED_SUBREDDITS_SHEET)
TARGETED_RELATED_SHEET = str(_TARGETED_RELATED_SHEET)
ACCOUNTS_SHEET_FIELDS = ["user_id", "enabled", "name", "notes"]
SUBREDDITS_SHEET_FIELDS = ["subreddit", "enabled", "notes"]
TARGETED_ACCOUNTS_FIELDS = ["user_id", "enabled", "name", "notes", "group"]
TARGETED_SUBREDDITS_FIELDS = ["group", "subreddit", "enabled", "notes"]
TARGETED_RELATED_FIELDS = ["group", "subreddit", "found_from", "enabled"]
TARGETED_RELATED_CAP = 30  # per group, so the related list stays reviewable
_DEFAULT_PROFILE_IDS: List[str] = [
    "k1g5mjmi",
    "k1g5mjmh",
]
_SHEET_LOCK = threading.RLock()
_SHEET_MODE = "general"


def clean_subreddit_name(name: str) -> str:
    """Drop a leading r/ or /r/ only. str.lstrip("r/") also eats the r of "running"."""
    value = str(name or "").strip()
    if value.startswith("/"):
        value = value[1:]
    if value.lower().startswith("r/"):
        value = value[2:]
    return value.strip("/").split("/")[0]


def set_sheet_mode(mode: str) -> str:
    """Point accounts/subreddits sheets at the general or targeted files for this run."""
    global ACCOUNTS_SHEET, SUBREDDITS_SHEET, _SHEET_MODE
    if str(mode or "").strip().lower() == "targeted":
        _SHEET_MODE = "targeted"
        ACCOUNTS_SHEET = TARGETED_ACCOUNTS_SHEET
        SUBREDDITS_SHEET = TARGETED_SUBREDDITS_SHEET
    else:
        _SHEET_MODE = "general"
        ACCOUNTS_SHEET = GENERAL_ACCOUNTS_SHEET
        SUBREDDITS_SHEET = GENERAL_SUBREDDITS_SHEET
    return _SHEET_MODE


def sheet_mode() -> str:
    return _SHEET_MODE


def is_targeted_mode() -> bool:
    return _SHEET_MODE == "targeted"


def accounts_sheet_path() -> str:
    return ACCOUNTS_SHEET


def subreddits_sheet_path() -> str:
    return SUBREDDITS_SHEET


def _group_key(value: str) -> str:
    return str(value or "").strip().lower()


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


def _accounts_fields() -> List[str]:
    return list(TARGETED_ACCOUNTS_FIELDS if is_targeted_mode() else ACCOUNTS_SHEET_FIELDS)


def _subreddits_fields() -> List[str]:
    return list(TARGETED_SUBREDDITS_FIELDS if is_targeted_mode() else SUBREDDITS_SHEET_FIELDS)


def ensure_accounts_sheet() -> None:
    if os.path.isfile(ACCOUNTS_SHEET):
        return
    if is_targeted_mode():
        _write_simple_csv(ACCOUNTS_SHEET, TARGETED_ACCOUNTS_FIELDS, [])
        return
    rows = [{"user_id": item, "enabled": "yes", "name": "", "notes": ""} for item in _DEFAULT_PROFILE_IDS]
    _write_simple_csv(ACCOUNTS_SHEET, ACCOUNTS_SHEET_FIELDS, rows)


def ensure_targeted_related_sheet() -> None:
    if not os.path.isfile(TARGETED_RELATED_SHEET):
        _write_simple_csv(TARGETED_RELATED_SHEET, TARGETED_RELATED_FIELDS, [])


def ensure_subreddits_sheet() -> None:
    if os.path.isfile(SUBREDDITS_SHEET):
        return
    if is_targeted_mode():
        _write_simple_csv(SUBREDDITS_SHEET, TARGETED_SUBREDDITS_FIELDS, [])
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
    _, rows = _read_simple_csv(ACCOUNTS_SHEET, _accounts_fields())
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


def load_browse_subreddits(group: str = "") -> List[str]:
    """Enabled communities from the active sheet. In targeted mode, `group` narrows to one group."""
    ensure_subreddits_sheet()
    _, rows = _read_simple_csv(SUBREDDITS_SHEET, _subreddits_fields())
    want = _group_key(group)
    found: List[str] = []
    seen = set()
    for row in rows:
        if not _row_is_enabled(row):
            continue
        if want and _group_key(_csv_lookup(row, "group")) != want:
            continue
        name = clean_subreddit_name(_csv_lookup(row, "subreddit", "community", "sub"))
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        found.append(name)
    return found


def load_account_groups() -> Dict[str, str]:
    """user_id (lower) -> group from the active accounts sheet. Empty group means every target."""
    ensure_accounts_sheet()
    _, rows = _read_simple_csv(ACCOUNTS_SHEET, _accounts_fields())
    groups: Dict[str, str] = {}
    for row in rows:
        user_id = _csv_lookup(row, "user_id", "account", "profile", "ads_power", "id")
        if user_id:
            groups[user_id.lower()] = _csv_lookup(row, "group").strip()
    return groups


def account_group(user_id: str) -> str:
    return load_account_groups().get(str(user_id or "").strip().lower(), "")


def load_target_groups() -> Dict[str, List[str]]:
    """group (as written) -> enabled target subreddits."""
    ensure_subreddits_sheet()
    _, rows = _read_simple_csv(SUBREDDITS_SHEET, _subreddits_fields())
    groups: Dict[str, List[str]] = {}
    for row in rows:
        if not _row_is_enabled(row):
            continue
        name = clean_subreddit_name(_csv_lookup(row, "subreddit", "community", "sub"))
        if not name:
            continue
        bucket = groups.setdefault(_csv_lookup(row, "group").strip(), [])
        if name.lower() not in {item.lower() for item in bucket}:
            bucket.append(name)
    return groups


def _related_rows() -> Tuple[List[str], List[Dict[str, str]]]:
    ensure_targeted_related_sheet()
    return _read_simple_csv(TARGETED_RELATED_SHEET, TARGETED_RELATED_FIELDS)


def load_target_related(group: str = "") -> List[str]:
    """Enabled related subreddits for a group ("" = every group)."""
    want = _group_key(group)
    found: List[str] = []
    seen = set()
    with _SHEET_LOCK:
        _, rows = _related_rows()
    for row in rows:
        if want and _group_key(row.get("group")) != want:
            continue
        if not _row_is_enabled(row):
            continue
        name = clean_subreddit_name(row.get("subreddit") or "")
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        found.append(name)
    return found


def target_related_known(group: str = "") -> set:
    """Every related name already in the sheet for a group, enabled or not (lowercase)."""
    want = _group_key(group)
    with _SHEET_LOCK:
        _, rows = _related_rows()
    return {
        clean_subreddit_name(row.get("subreddit") or "").lower()
        for row in rows
        if (not want or _group_key(row.get("group")) == want) and row.get("subreddit")
    }


def append_target_related(group: str, subreddit: str, found_from: str = "") -> bool:
    """Add one related subreddit for a group. False when already listed, a target, or the group is full."""
    name = clean_subreddit_name(subreddit)
    if not name:
        return False
    want = _group_key(group)
    with _SHEET_LOCK:
        headers, rows = _related_rows()
        in_group = [row for row in rows if _group_key(row.get("group")) == want]
        if any(clean_subreddit_name(row.get("subreddit") or "").lower() == name.lower() for row in in_group):
            return False
        if len(in_group) >= TARGETED_RELATED_CAP:
            return False
        targets = load_browse_subreddits(group) if is_targeted_mode() else []
        if name.lower() in {item.lower() for item in targets}:
            return False
        rows.append(
            {
                "group": str(group or "").strip(),
                "subreddit": name,
                "found_from": str(found_from or "").strip()[:80],
                "enabled": "yes",
            }
        )
        _write_simple_csv(TARGETED_RELATED_SHEET, headers or list(TARGETED_RELATED_FIELDS), rows)
    return True


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
        headers, rows = _read_simple_csv(ACCOUNTS_SHEET, _accounts_fields())
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
            fields = list(headers) if headers else _accounts_fields()
            for name in _accounts_fields():
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
        headers, rows = _read_simple_csv(ACCOUNTS_SHEET, _accounts_fields())
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
            fields = list(headers) if headers else _accounts_fields()
            for name in _accounts_fields():
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
