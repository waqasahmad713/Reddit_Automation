#!/usr/bin/env python3
"""Pick communities that usually accept newer / lower-karma accounts."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

# Real communities that often allow self posts from newer accounts.
# Do not use karma-farm subs.
NEWBIE_SUBS: List[str] = [
    "CasualConversation",
    "NoStupidQuestions",
    "self",
    "Advice",
    "internetparents",
    "hobbies",
    "DecidingToBeBetter",
    "socialskills",
    "findapath",
    "TooAfraidToAsk",
    "NewToReddit",
    "DoesAnybodyElse",
    "OutOfTheLoop",
    "ExplainTheJoke",
    "MadeMeSmile",
    "wholesome",
]

GROWING_SUBS: List[str] = [
    "learnpython",
    "learnprogramming",
    "productivity",
    "simpleliving",
    "careerguidance",
    "unpopularopinion",
    "selfimprovement",
    "getdisciplined",
    "IWantToLearn",
    "GetStudying",
    "languagelearning",
    "jobs",
    "resumes",
    "YouShouldKnow",
]

# Extra browse/join targets so each run can pick a fresh set.
ACTIVITY_SUBS: List[str] = [
    "books",
    "suggestmeabook",
    "movies",
    "television",
    "music",
    "cooking",
    "baking",
    "coffee",
    "tea",
    "gardening",
    "hiking",
    "boardgames",
    "journaling",
    "bulletjournal",
    "college",
    "CozyPlaces",
    "aww",
    "cats",
    "dogs",
    "mildlyinteresting",
    "oddlysatisfying",
    "tipofmytongue",
    "whatisthisthing",
    "internetparents",
    "hobbies",
]

ESTABLISHED_SUBS: List[str] = [
    "python",
    "technology",
    "automation",
    "webdev",
    "programming",
]

_KARMA_KINDS = r"(?:comment|post|link|combined|total|sub(?:reddit)?)?\s*"
_KARMA_NEED_PATTERNS = (
    re.compile(
        r"(?:at least|minimum|min(?:imum)?|need(?:s)?|require[sd]?|over|more than)\s*"
        rf"(\d{{1,5}})\+?\s*{_KARMA_KINDS}karma",
        re.I,
    ),
    re.compile(
        rf"(\d{{1,5}})\+?\s*{_KARMA_KINDS}karma\s*"
        r"(?:is\s*|are\s*)?(?:required|requirement|minimum|needed|or (?:more|higher|above))",
        re.I,
    ),
    # "Karma requirement: 50", "karma minimum of 50", "karma threshold is 50"
    re.compile(
        rf"{_KARMA_KINDS}karma\s*(?:requirement|minimum|threshold|limit)\s*"
        r"(?:of|is|:|=)?\s*(\d{1,5})",
        re.I,
    ),
    # "...must be 14 days old and have 20 karma": the qualifier sits too far from
    # the number for the patterns above to reach it.
    re.compile(rf"(?:have|has|having|with)\s*(\d{{1,5}})\+?\s*{_KARMA_KINDS}karma", re.I),
)
_AGE_UNITS = r"(day|days|week|weeks|month|months)"
_AGE_NEED_PATTERNS = (
    re.compile(
        rf"(?:at least|minimum|min(?:imum)?|older than|over)\s*(\d{{1,3}})\s*{_AGE_UNITS}",
        re.I,
    ),
    # "Account must be 30 days old", "accounts need to be 2 weeks old"
    re.compile(
        rf"accounts?\s*(?:must|need(?:s)? to|should|has to|have to)\s*be\s*(\d{{1,3}})\s*{_AGE_UNITS}",
        re.I,
    ),
    # "30 day account age requirement", "7 days old minimum"
    re.compile(
        rf"(\d{{1,3}})\s*{_AGE_UNITS}\s*(?:old\s*)?(?:account\s*)?"
        r"(?:age\s*)?(?:requirement|required|minimum|min)",
        re.I,
    ),
    # "accounts younger than 7 days cannot post"
    re.compile(rf"younger than\s*(\d{{1,3}})\s*{_AGE_UNITS}", re.I),
    # "account age: 30 days"
    re.compile(rf"account age\s*(?:of|is|:|=)?\s*(\d{{1,3}})\s*{_AGE_UNITS}", re.I),
)


# Communities that gate on karma or account age via automod. A fresh account
# posting here usually gets removed, which looks like a failed action and wastes
# the account's small 48h budget, so they are withheld until it has standing.
NEEDS_STANDING = {
    "unpopularopinion",
    "youshouldknow",
    "jobs",
    "resumes",
    "careerguidance",
}


def account_tier(karma: int, age_days: float) -> str:
    # Deliberately OR, not AND: most subreddit gates check age *or* karma, so an
    # account that fails either test is treated as new.
    if karma < 80 or age_days < 14:
        return "new"
    if karma < 400 or age_days < 45:
        return "growing"
    return "established"


def candidate_subreddits(
    karma: int,
    age_days: float,
    extra: Optional[List[str]] = None,
) -> List[str]:
    """Ordered list of subs to try, matching how established the account looks."""
    tier = account_tier(karma, age_days)
    extra_clean = [s.strip().lstrip("r/") for s in (extra or []) if s and str(s).strip()]
    if tier == "new":
        ordered = (
            list(NEWBIE_SUBS)
            + [s for s in GROWING_SUBS if s.lower() not in NEEDS_STANDING]
            + list(ACTIVITY_SUBS)
        )
    elif tier == "growing":
        ordered = (
            list(GROWING_SUBS)
            + list(NEWBIE_SUBS)
            + list(ACTIVITY_SUBS)
            + extra_clean
            + list(ESTABLISHED_SUBS)
        )
    else:
        ordered = (
            extra_clean
            + list(ESTABLISHED_SUBS)
            + list(GROWING_SUBS)
            + list(ACTIVITY_SUBS)
            + list(NEWBIE_SUBS)
        )
    seen = set()
    out: List[str] = []
    for name in ordered:
        key = name.lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(name)
    return out


def comment_subreddits(karma: int, age_days: float, extra: Optional[List[str]] = None) -> List[str]:
    """Subs where comments from this account are more likely to stay up."""
    names = candidate_subreddits(karma, age_days, extra=extra)
    # Prefer a mix: one newbie-friendly, then the account's usual browse list.
    extra_clean = [s.strip().lstrip("r/") for s in (extra or []) if s and str(s).strip()]
    mixed: List[str] = []
    seen = set()
    for name in names[:4] + extra_clean + names[4:]:
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        mixed.append(name)
    return mixed


def _rules_text(rules_payload: Any) -> str:
    chunks: List[str] = []
    rows = []
    if isinstance(rules_payload, dict):
        rows = rules_payload.get("rules") or []
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            chunks.append(str(row.get("short_name") or ""))
            chunks.append(str(row.get("description") or ""))
    return "\n".join(chunks)


def summarize_subreddit(about: Dict[str, Any], rules_payload: Any) -> Dict[str, Any]:
    data = about.get("data") if isinstance(about.get("data"), dict) else about
    if not isinstance(data, dict):
        data = {}
    description = str(data.get("public_description") or data.get("description") or "")
    submission_type = str(data.get("submission_type") or "any").lower()
    restrict = bool(data.get("restrict_posting"))
    over18 = bool(data.get("over18"))
    subscribers = int(data.get("subscribers") or 0)
    rules = _rules_text(rules_payload)
    return {
        "name": str(data.get("display_name") or data.get("display_name_prefixed") or "").lstrip("r/"),
        "description": description.strip(),
        "rules": rules.strip(),
        "submission_type": submission_type,
        "restrict_posting": restrict,
        "over18": over18,
        "subscribers": subscribers,
        "allows_text": submission_type in {"any", "self", ""},
    }


def required_karma_and_age(text: str) -> Tuple[Optional[int], Optional[float]]:
    blob = text or ""
    # Every pattern is scanned and the strictest requirement wins. Stopping at
    # the first match let a lenient sentence hide a stricter rule further down.
    karma_need: Optional[int] = None
    for pattern in _KARMA_NEED_PATTERNS:
        for match in pattern.finditer(blob):
            value = int(match.group(1))
            if karma_need is None or value > karma_need:
                karma_need = value
    age_need: Optional[float] = None
    for pattern in _AGE_NEED_PATTERNS:
        for match in pattern.finditer(blob):
            amount = float(match.group(1))
            unit = match.group(2).lower()
            if unit.startswith("week"):
                amount *= 7
            elif unit.startswith("month"):
                amount *= 30
            if age_need is None or amount > age_need:
                age_need = amount
    return karma_need, age_need


def subreddit_allows_account(
    summary: Dict[str, Any],
    karma: int,
    age_days: float,
) -> Tuple[bool, str]:
    if summary.get("over18"):
        return False, "nsfw"
    if summary.get("restrict_posting"):
        return False, "posting restricted"
    if not summary.get("allows_text"):
        return False, "text posts not allowed"
    blob = f"{summary.get('description') or ''}\n{summary.get('rules') or ''}"
    need_karma, need_age = required_karma_and_age(blob)
    if need_karma is not None and karma < need_karma:
        return False, f"needs {need_karma} karma"
    if need_age is not None and age_days < need_age:
        return False, f"needs {need_age:.0f} day old account"
    lowered = blob.lower()
    if "no self posts" in lowered or "self posts are not allowed" in lowered:
        return False, "no self posts"
    return True, "ok"
