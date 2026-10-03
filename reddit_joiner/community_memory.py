#!/usr/bin/env python3
"""Persisted per-community comment memory, used on the next run.

Stores live / removed / filtered counts, tones, and upvoted example text.
A filter on a young account does not count against a tone. Only a real
removal on an account that was allowed to speak does.
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from reddit_joiner.paths import COMMUNITY_MEMORY_PATH as _MEMORY_PATH

MEMORY_PATH = str(_MEMORY_PATH)
_LOCK = threading.RLock()

TONE_SKIP_AFTER = 2
TONE_SKIP_SECONDS = 7 * 86400
COMMUNITY_SKIP_AFTER = 4
COMMUNITY_SKIP_SECONDS = 3 * 86400
COMMUNITY_SKIP_REPEAT = 7 * 86400
MAX_EXAMPLES = 8
EXAMPLE_MIN_SCORE = 1


def _empty() -> Dict[str, Any]:
    return {"communities": {}, "first_seen": {}}


def _load() -> Dict[str, Any]:
    try:
        if not os.path.isfile(MEMORY_PATH):
            return _empty()
        with open(MEMORY_PATH, encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            return _empty()
        if not isinstance(data.get("communities"), dict):
            data["communities"] = {}
        if not isinstance(data.get("first_seen"), dict):
            data["first_seen"] = {}
        return data
    except Exception:
        return _empty()


def _save(data: Dict[str, Any]) -> None:
    directory = os.path.dirname(MEMORY_PATH) or "."
    os.makedirs(directory, exist_ok=True)
    tmp = f"{MEMORY_PATH}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, MEMORY_PATH)


def _norm(name: str) -> str:
    return str(name or "").strip().lstrip("r/").lower()


def _bucket(data: Dict[str, Any], name: str) -> Dict[str, Any]:
    key = _norm(name)
    communities = data.setdefault("communities", {})
    bucket = communities.get(key)
    if not isinstance(bucket, dict):
        bucket = {
            "name": key,
            "live": 0,
            "removed": 0,
            "filtered": 0,
            "tones": {},
            "examples": [],
            "skip_until": 0.0,
            "skip_tones": {},
        }
        communities[key] = bucket
    bucket.setdefault("tones", {})
    bucket.setdefault("examples", [])
    bucket.setdefault("skip_tones", {})
    bucket.setdefault("skip_until", 0.0)
    return bucket


def _tone_bucket(community: Dict[str, Any], tone: str) -> Dict[str, Any]:
    key = str(tone or "neutral").strip().lower() or "neutral"
    tones = community.setdefault("tones", {})
    row = tones.get(key)
    if not isinstance(row, dict):
        row = {"live": 0, "removed": 0, "filtered": 0}
        tones[key] = row
    return row


def seed_seen_from_history(runs_by_account: Dict[str, List[str]]) -> int:
    """Mark communities from earlier sittings as already seen."""
    added = 0
    with _LOCK:
        data = _load()
        seen = data.setdefault("first_seen", {})
        for user_id, names in (runs_by_account or {}).items():
            uid = str(user_id or "")
            if not uid:
                continue
            entry = seen.get(uid)
            if not isinstance(entry, dict):
                entry = {}
                seen[uid] = entry
            for raw in names or []:
                key = _norm(raw)
                if key and key not in entry:
                    entry[key] = 1.0
                    added += 1
        if added:
            _save(data)
    return added


def account_has_seen_community(
    user_id: str, name: str, *, before: float = 0.0
) -> bool:
    key = _norm(name)
    uid = str(user_id or "")
    if not uid or not key:
        return False
    with _LOCK:
        data = _load()
        entry = data.get("first_seen", {}).get(uid)
        if not isinstance(entry, dict) or key not in entry:
            return False
        if before:
            try:
                return float(entry.get(key) or 0) < float(before)
            except (TypeError, ValueError):
                return False
        return True


def mark_community_seen(user_id: str, name: str) -> None:
    key = _norm(name)
    uid = str(user_id or "")
    if not uid or not key:
        return
    with _LOCK:
        data = _load()
        seen = data.setdefault("first_seen", {})
        entry = seen.get(uid)
        if not isinstance(entry, dict):
            entry = {}
            seen[uid] = entry
        if key not in entry:
            entry[key] = time.time()
            _save(data)


def community_skip_reason(name: str) -> str:
    key = _norm(name)
    if not key:
        return ""
    now = time.time()
    with _LOCK:
        bucket = _load().get("communities", {}).get(key)
    if not isinstance(bucket, dict):
        return ""
    until = float(bucket.get("skip_until") or 0)
    if until > now:
        hours = max(1.0, (until - now) / 3600.0)
        return f"cooling off after removals ({hours:.0f}h left)"
    return ""


def community_is_skipped(name: str) -> bool:
    return bool(community_skip_reason(name))


def tone_is_skipped(name: str, tone: str) -> bool:
    key = _norm(name)
    wanted = str(tone or "").strip().lower()
    if not key or not wanted:
        return False
    now = time.time()
    with _LOCK:
        bucket = _load().get("communities", {}).get(key)
    if not isinstance(bucket, dict):
        return False
    until = (bucket.get("skip_tones") or {}).get(wanted)
    try:
        return float(until or 0) > now
    except (TypeError, ValueError):
        return False


def skipped_tones(name: str) -> List[str]:
    key = _norm(name)
    if not key:
        return []
    now = time.time()
    with _LOCK:
        bucket = _load().get("communities", {}).get(key)
    if not isinstance(bucket, dict):
        return []
    out: List[str] = []
    for tone, until in (bucket.get("skip_tones") or {}).items():
        try:
            if float(until or 0) > now:
                out.append(str(tone))
        except (TypeError, ValueError):
            continue
    return out


def pick_live_example(name: str) -> str:
    key = _norm(name)
    if not key:
        return ""
    with _LOCK:
        bucket = _load().get("communities", {}).get(key)
    if not isinstance(bucket, dict):
        return ""
    rows = [
        row
        for row in (bucket.get("examples") or [])
        if isinstance(row, dict) and str(row.get("text") or "").strip()
    ]
    if not rows:
        return ""
    rows.sort(key=lambda row: int(row.get("score") or 0), reverse=True)
    return str(rows[0].get("text") or "").strip()


def record_comment_outcome(
    name: str,
    *,
    tone: str = "",
    status: str = "",
    score: int = 0,
    text: str = "",
    punish_tone: bool = False,
) -> Tuple[str, List[str]]:
    """Update counts. Returns (community_note, newly_skipped_tones)."""
    key = _norm(name)
    kind = str(status or "").strip().lower()
    if not key or kind not in {"live", "removed", "deleted", "filtered", "collapsed"}:
        return "", []
    if kind == "deleted":
        kind = "removed"
    if kind == "collapsed":
        kind = "filtered"
    notes = ""
    skipped: List[str] = []
    with _LOCK:
        data = _load()
        community = _bucket(data, key)
        community[kind] = int(community.get(kind) or 0) + 1
        row = _tone_bucket(community, tone)
        row[kind] = int(row.get(kind) or 0) + 1
        if kind == "live" and int(score or 0) >= EXAMPLE_MIN_SCORE and text.strip():
            examples = [
                item
                for item in (community.get("examples") or [])
                if isinstance(item, dict)
                and str(item.get("text") or "").strip() != text.strip()
            ]
            examples.append(
                {"text": text.strip()[:280], "score": int(score or 0), "at": time.time()}
            )
            examples.sort(key=lambda item: int(item.get("score") or 0), reverse=True)
            community["examples"] = examples[:MAX_EXAMPLES]
        if punish_tone and kind == "removed":
            removed = int(row.get("removed") or 0)
            if removed >= TONE_SKIP_AFTER:
                community.setdefault("skip_tones", {})
                community["skip_tones"][str(tone or "neutral").strip().lower()] = (
                    time.time() + TONE_SKIP_SECONDS
                )
                skipped.append(str(tone or "neutral").strip().lower())
            total_removed = int(community.get("removed") or 0)
            if total_removed >= COMMUNITY_SKIP_AFTER:
                extra = COMMUNITY_SKIP_REPEAT if total_removed >= COMMUNITY_SKIP_AFTER + 2 else COMMUNITY_SKIP_SECONDS
                community["skip_until"] = time.time() + extra
                notes = f"r/{key} cooling off after {total_removed} removals"
        _save(data)
    return notes, skipped
