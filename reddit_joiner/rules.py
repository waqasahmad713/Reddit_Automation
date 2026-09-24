"""Read and classify public subreddit rules for comments and RL."""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from reddit_joiner.paths import RULES_CACHE_PATH

CACHE_TTL = 7 * 86400

_FLAG_PATTERNS = {
    "no_humor": (
        r"\bno (?:jokes?|memes?|humou?r|sarcasm|shitposts?)\b",
        r"\b(?:jokes?|memes?|shitposts?|low[- ]effort joke)s? (?:are )?(?:not allowed|banned|forbidden)\b",
    ),
    "no_promo": (
        r"\bself[- ]promot",
        r"\bno (?:ads?|advertis|spam|soliciting)\b",
        r"\b(?:advertis|spam|promo)(?:ing|ement)? (?:is )?(?:not allowed|banned)\b",
    ),
    "account_gate": (
        r"\b(?:account age|karma)\b.{0,40}\b(?:required|minimum|need|must)\b",
        r"\b(?:required|minimum|need|must)\b.{0,40}\b(?:account age|karma)\b",
        r"\bnew accounts?\b",
        r"\blurk (?:first|before)\b",
    ),
    "civil": (
        r"\bbe (?:civil|respectful|kind|nice)\b",
        r"\bno (?:harass|insult|personal attack|hate|bigotry|abuse)\b",
    ),
    "low_effort": (
        r"\bno low[- ]effort\b",
        r"\b(?:one[- ]word|emoji[- ]only|\"this\")\b",
        r"\blow[- ]effort comments?\b",
    ),
    "questions_only": (
        r"\bquestions? only\b",
        r"\bmust be a question\b",
        r"\btitle must\b",
    ),
}


@dataclass
class SubredditRules:
    subreddit: str
    titles: List[str] = field(default_factory=list)
    summary: str = ""
    raw: str = ""
    rule_count: int = 0
    source: str = ""
    flags: Dict[str, bool] = field(default_factory=dict)
    strictness: float = 0.0

    def prompt_text(self) -> str:
        return (self.summary or self.raw or "").strip()[:1200]

    def fingerprint(self) -> str:
        blob = " | ".join(self.titles) + "\n" + (self.raw or "")
        return re.sub(r"\s+", " ", blob).strip().lower()[:400]

    def features(self) -> Dict[str, Any]:
        flags = self.flags or {}
        packed = (
            0.25 * float(bool(flags.get("no_humor")))
            + 0.25 * float(bool(flags.get("no_promo")))
            + 0.25 * float(bool(flags.get("account_gate")))
            + 0.25 * float(bool(flags.get("low_effort")))
        )
        return {
            "rules_count": min(max(self.rule_count / 12.0, 0.0), 1.0),
            "rules_strict": min(max(self.strictness, 0.0), 1.0),
            "rules_flags": packed,
            "rules_read": 1.0 if self.rule_count or self.summary else 0.0,
            "rules_fp": self.fingerprint(),
        }


def empty_rules(subreddit: str, source: str = "") -> SubredditRules:
    return SubredditRules(
        subreddit=_clean_name(subreddit),
        source=source,
        flags={key: False for key in _FLAG_PATTERNS},
    )


def _clean_name(name: str) -> str:
    return str(name or "").strip().lstrip("r/").split("/")[0]


def _classify(text: str) -> Dict[str, bool]:
    blob = (text or "").lower()
    flags = {}
    for key, patterns in _FLAG_PATTERNS.items():
        flags[key] = any(re.search(pattern, blob) for pattern in patterns)
    return flags


def _strictness(count: int, flags: Dict[str, bool]) -> float:
    restrictive = sum(
        1
        for key in ("no_humor", "no_promo", "account_gate", "low_effort", "questions_only")
        if flags.get(key)
    )
    return min(1.0, 0.07 * max(0, count) + 0.18 * restrictive)


def parse_rules_payload(
    subreddit: str,
    rules_payload: Any,
    *,
    source: str = "json",
) -> SubredditRules:
    name = _clean_name(subreddit)
    titles: List[str] = []
    lines: List[str] = []
    rows = []
    if isinstance(rules_payload, dict):
        rows = rules_payload.get("rules") or []
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            title = re.sub(r"\s+", " ", str(row.get("short_name") or "")).strip()
            body = re.sub(r"\s+", " ", str(row.get("description") or "")).strip()
            if title:
                titles.append(title[:160])
            chunk = ": ".join(part for part in (title, body) if part)
            if chunk:
                lines.append(chunk[:400])
    return _finish(name, titles, lines, source)


def parse_page_rules(
    subreddit: str,
    titles: List[str],
    bodies: Optional[List[str]] = None,
    *,
    source: str = "page",
) -> SubredditRules:
    name = _clean_name(subreddit)
    clean_titles = [
        re.sub(r"\s+", " ", str(title)).strip()[:160]
        for title in titles
        if str(title or "").strip()
    ]
    lines: List[str] = []
    extra = bodies or []
    if clean_titles and extra and len(extra) == len(clean_titles):
        for title, body in zip(clean_titles, extra):
            body_text = re.sub(r"\s+", " ", str(body or "")).strip()
            lines.append(": ".join(part for part in (title, body_text) if part)[:400])
    else:
        lines = list(clean_titles)
        lines.extend(
            re.sub(r"\s+", " ", str(body)).strip()[:400]
            for body in extra
            if str(body or "").strip()
        )
    return _finish(name, clean_titles, lines, source)


def merge_rules(primary: SubredditRules, secondary: SubredditRules) -> SubredditRules:
    if primary.rule_count >= secondary.rule_count and primary.summary:
        if not primary.titles and secondary.titles:
            primary.titles = list(secondary.titles)
        return primary
    if secondary.rule_count:
        return secondary
    return primary


def _finish(name: str, titles: List[str], lines: List[str], source: str) -> SubredditRules:
    seen = set()
    uniq_titles: List[str] = []
    for title in titles:
        key = title.lower()
        if not key or key in seen:
            continue
        seen.add(key)
        uniq_titles.append(title)
    summary = "\n".join(line for line in lines if line)[:2000]
    raw = summary
    flags = _classify(f"{' '.join(uniq_titles)}\n{raw}")
    return SubredditRules(
        subreddit=name,
        titles=uniq_titles[:20],
        summary=summary,
        raw=raw,
        rule_count=len(uniq_titles) or len(lines),
        source=source,
        flags=flags,
        strictness=_strictness(len(uniq_titles) or len(lines), flags),
    )


def rules_were_read(rules: Optional[SubredditRules]) -> bool:
    """False when the rules object is a placeholder from a failed fetch."""
    if rules is None:
        return False
    return bool(rules.rule_count or rules.summary or rules.raw)


def tone_fits(rules: Optional[SubredditRules], tone: str) -> bool:
    style = (tone or "").strip().lower()
    if not rules_were_read(rules):
        # A failed rules read produces an object with every flag False, which
        # used to read as "this community bans nothing". Humour is the tone that
        # gets comments removed, so withhold it until the rules are actually known.
        return style != "funny"
    if style == "funny" and rules.flags.get("no_humor"):
        return False
    return True


def safer_tone(rules: Optional[SubredditRules], tone: str) -> str:
    style = (tone or "friendly").strip().lower() or "friendly"
    if not tone_fits(rules, style):
        return "neutral"
    return style


def load_cached(subreddit: str, *, max_age: float = CACHE_TTL) -> Optional[SubredditRules]:
    name = _clean_name(subreddit).lower()
    data = _read_cache()
    row = data.get(name)
    if not isinstance(row, dict):
        return None
    try:
        age = time.time() - float(row.get("saved_at") or 0)
    except (TypeError, ValueError):
        return None
    if age > max_age:
        return None
    try:
        return SubredditRules(
            subreddit=str(row.get("subreddit") or name),
            titles=list(row.get("titles") or []),
            summary=str(row.get("summary") or ""),
            raw=str(row.get("raw") or ""),
            rule_count=int(row.get("rule_count") or 0),
            source=str(row.get("source") or "cache"),
            flags=dict(row.get("flags") or {}),
            strictness=float(row.get("strictness") or 0),
        )
    except Exception:
        return None


_CACHE_LOCK = threading.Lock()


def save_cached(rules: SubredditRules) -> None:
    if not rules.subreddit:
        return
    # Profiles run in parallel threads, so read-modify-write has to be atomic or
    # concurrent saves silently drop each other's entries.
    with _CACHE_LOCK:
        data, readable = _read_cache_state()
        if not readable:
            # The file exists but could not be parsed. Overwriting now would
            # replace every cached community with just this one, so leave it
            # alone and let this subreddit be re-fetched next time.
            return
        payload = asdict(rules)
        payload["saved_at"] = time.time()
        data[_clean_name(rules.subreddit).lower()] = payload
        path = Path(RULES_CACHE_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, path)


def _read_cache_state() -> Tuple[Dict[str, Any], bool]:
    """Returns (entries, readable). readable=False means do not overwrite."""
    path = Path(RULES_CACHE_PATH)
    if not path.is_file():
        return {}, True
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}, False
    if not isinstance(data, dict):
        return {}, False
    return data, True


def _read_cache() -> Dict[str, Any]:
    return _read_cache_state()[0]
