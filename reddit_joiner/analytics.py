#!/usr/bin/env python3
"""Charts from activity.db (pandas + matplotlib) and one HTML page to view them.

Run on its own:  python3 -m reddit_joiner.analytics
"""

from __future__ import annotations

import html
import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import pandas as pd

from reddit_joiner.paths import (
    DB_PATH as _DB_PATH,
    LOGS_DIR,
    REPORTS_DIR,
    SUBREDDIT_LOG_PATH,
)

STATUS_ORDER = ("live", "removed", "filtered", "unchecked")
STATUS_COLORS = {
    "live": "#2e9e5b",
    "removed": "#d64545",
    "filtered": "#e0a030",
    "unchecked": "#9aa4b2",
}
AGE_BANDS = ((0, 4, "days 1-3"), (4, 8, "days 4-7"), (8, 14, "days 8-13"), (14, 10_000, "day 14+"))


def _plt():
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    return plt


def _read(conn: sqlite3.Connection, sql: str) -> pd.DataFrame:
    try:
        return pd.read_sql_query(sql, conn)
    except Exception:
        return pd.DataFrame()


def _comment_status(value: object) -> str:
    text = str(value or "").strip().lower()
    if text == "live":
        return "live"
    if text in {"removed", "deleted"}:
        return "removed"
    if text in {"filtered", "collapsed"}:
        return "filtered"
    return "unchecked"


def _age_band(age: float) -> str:
    for lo, hi, name in AGE_BANDS:
        if lo <= float(age or 0) < hi:
            return name
    return "unknown"


def load_frames(db_path: Optional[str] = None) -> Dict[str, pd.DataFrame]:
    """Every table the charts use, as DataFrames. Missing tables are empty."""
    path = str(db_path or _DB_PATH)
    frames: Dict[str, pd.DataFrame] = {}
    if not Path(path).exists():
        return {name: pd.DataFrame() for name in ("sittings", "comments", "posts", "status", "ips", "join_fail")}
    conn = sqlite3.connect(path)
    try:
        frames["sittings"] = _read(conn, "SELECT * FROM sittings")
        frames["comments"] = _read(
            conn,
            "SELECT created_at, account, user_id, subreddit, tone, status FROM ai_comments",
        )
        frames["posts"] = _read(conn, "SELECT created_at, account, subreddit, status FROM posts")
        frames["status"] = _read(conn, "SELECT user_id, status FROM account_outside_status")
        frames["ips"] = _read(conn, "SELECT day, user_id, ip FROM account_ips")
        frames["join_fail"] = _read(conn, "SELECT subreddit, user_id FROM join_failures")
    finally:
        conn.close()
    comments = frames["comments"]
    if not comments.empty:
        comments["day"] = comments["created_at"].astype(str).str[:10]
        comments["outcome"] = comments["status"].map(_comment_status)
        comments["tone"] = comments["tone"].fillna("").replace("", "unknown")
        comments["subreddit"] = comments["subreddit"].fillna("").replace("", "unknown")
    return frames


# ---------------------------------------------------------------------------
# Charts. Each returns True when it wrote a file, False when there was no data.
# ---------------------------------------------------------------------------


def _save(fig, out: Path) -> None:
    fig.tight_layout()
    fig.savefig(out, dpi=110)
    _plt().close(fig)


def chart_run_targets(frames: Dict[str, pd.DataFrame], run_id: str, out: Path) -> bool:
    sit = frames["sittings"]
    if sit.empty or not run_id:
        return False
    run = sit[(sit["run_id"] == run_id) & (sit["sitting_ok"] == 1)]
    if run.empty:
        return False
    plt = _plt()
    names = run["account"].astype(str).tolist()
    x = range(len(names))
    width = 0.2
    fig, ax = plt.subplots(figsize=(max(6, len(names) * 0.9), 4))
    ax.bar([i - 1.5 * width for i in x], run["upvote_target"], width, label="upvote target", color="#b8c7e0")
    ax.bar([i - 0.5 * width for i in x], run["upvotes"], width, label="upvotes", color="#3b6fb6")
    ax.bar([i + 0.5 * width for i in x], run["comment_target"], width, label="comment target", color="#f0c9a0")
    ax.bar([i + 1.5 * width for i in x], run["comments"], width, label="comments", color="#d9822b")
    ax.set_xticks(list(x))
    ax.set_xticklabels(names, rotation=35, ha="right", fontsize=8)
    ax.set_title("This run — done vs target per account")
    ax.legend(fontsize=8)
    _save(fig, out)
    return True


def chart_run_minutes(frames: Dict[str, pd.DataFrame], run_id: str, out: Path) -> bool:
    sit = frames["sittings"]
    if sit.empty or not run_id:
        return False
    run = sit[(sit["run_id"] == run_id) & (sit["minutes"] > 0)]
    if run.empty:
        return False
    plt = _plt()
    fig, ax = plt.subplots(figsize=(max(6, len(run) * 0.7), 3.6))
    ax.bar(run["account"].astype(str), run["minutes"], color="#5a8f7b")
    ax.axhline(6, color="#888", linestyle="--", linewidth=1)
    ax.axhline(15, color="#888", linestyle="--", linewidth=1)
    ax.set_ylabel("minutes")
    ax.set_title("This run — sitting length (6-15 min band)")
    ax.tick_params(axis="x", rotation=35, labelsize=8)
    _save(fig, out)
    return True


def chart_history(frames: Dict[str, pd.DataFrame], out: Path) -> bool:
    sit = frames["sittings"]
    if sit.empty:
        return False
    daily = sit.groupby("day").agg(
        sittings=("id", "count"),
        upvotes=("upvotes", "sum"),
        comments=("comments", "sum"),
        joins=("joins", "sum"),
        posts=("posts", "sum"),
    )
    if daily.empty:
        return False
    plt = _plt()
    fig, ax = plt.subplots(figsize=(8, 4))
    for col in daily.columns:
        ax.plot(daily.index, daily[col], marker="o", label=col)
    ax.set_title("Activity per day")
    ax.legend(fontsize=8)
    ax.tick_params(axis="x", rotation=35, labelsize=8)
    _save(fig, out)
    return True


def chart_comment_outcomes(frames: Dict[str, pd.DataFrame], out: Path) -> bool:
    com = frames["comments"]
    if com.empty:
        return False
    table = com.pivot_table(index="day", columns="outcome", values="created_at", aggfunc="count", fill_value=0)
    table = table.reindex(columns=[c for c in STATUS_ORDER if c in table.columns])
    if table.empty:
        return False
    plt = _plt()
    fig, ax = plt.subplots(figsize=(max(7, len(table) * 0.45), 4))
    bottom = None
    for col in table.columns:
        ax.bar(table.index, table[col], bottom=bottom, label=col, color=STATUS_COLORS.get(col))
        bottom = table[col] if bottom is None else bottom + table[col]
    ax.set_title("Comment outcomes per day")
    ax.legend(fontsize=8)
    ax.tick_params(axis="x", rotation=45, labelsize=7)
    _save(fig, out)
    return True


def _removal_rate(df: pd.DataFrame, key: str) -> pd.DataFrame:
    checked = df[df["outcome"] != "unchecked"]
    if checked.empty:
        return pd.DataFrame()
    grouped = checked.groupby(key).agg(
        checked=("outcome", "count"),
        removed=("outcome", lambda s: int((s == "removed").sum())),
    )
    grouped["rate"] = grouped["removed"] / grouped["checked"]
    return grouped


def chart_removal_by_tone(frames: Dict[str, pd.DataFrame], out: Path) -> bool:
    com = frames["comments"]
    if com.empty:
        return False
    rates = _removal_rate(com, "tone")
    if rates.empty:
        return False
    rates = rates.sort_values("rate", ascending=False)
    plt = _plt()
    fig, ax = plt.subplots(figsize=(6, 3.6))
    bars = ax.bar(rates.index, rates["rate"] * 100, color="#d64545")
    for bar, (_, row) in zip(bars, rates.iterrows()):
        ax.annotate(
            f"{int(row['removed'])}/{int(row['checked'])}",
            (bar.get_x() + bar.get_width() / 2, bar.get_height()),
            ha="center", va="bottom", fontsize=8,
        )
    ax.set_ylim(0, 105)
    ax.set_ylabel("% removed (checked comments)")
    ax.set_title("Removal rate by tone")
    _save(fig, out)
    return True


def chart_top_subreddits(frames: Dict[str, pd.DataFrame], out: Path) -> bool:
    com = frames["comments"]
    if com.empty:
        return False
    counts = com.groupby("subreddit").size().sort_values(ascending=False).head(10)
    if counts.empty:
        return False
    rates = _removal_rate(com[com["subreddit"].isin(counts.index)], "subreddit")
    plt = _plt()
    fig, ax = plt.subplots(figsize=(8, 4))
    ax.barh(counts.index[::-1], counts.values[::-1], color="#3b6fb6")
    for name, value in zip(counts.index[::-1], counts.values[::-1]):
        rate = rates["rate"].get(name) if not rates.empty else None
        label = f" {rate:.0%} removed" if rate is not None and rate == rate else " unchecked"
        ax.annotate(label, (value, name), va="center", fontsize=8)
    ax.set_xlabel("comments")
    ax.set_title("Top 10 subreddits by comments")
    _save(fig, out)
    return True


HEALTH_ORDER = ("Live", "Warm-up", "Banned", "Suspended", "No browser")
HEALTH_COLORS = {
    "Live": "#2e9e5b",
    "Warm-up": "#3b6fb6",
    "Banned": "#d64545",
    "Suspended": "#e0a030",
    "No browser": "#9aa4b2",
}


def _health_label(status: object) -> str:
    text = str(status or "").strip().lower()
    if text.startswith("warmup"):
        return "Warm-up"
    if "suspend" in text:
        return "Suspended"
    if "ban" in text:
        return "Banned"
    if text == "no browser":
        return "No browser"
    return "Live"


def run_health(frames: Dict[str, pd.DataFrame], run_id: str) -> pd.DataFrame:
    """One row per account in this run: user_id, age_days, health."""
    sit = frames["sittings"]
    if sit.empty or not run_id:
        return pd.DataFrame(columns=["user_id", "age_days", "health"])
    run = sit[sit["run_id"] == run_id].sort_values("id").groupby("user_id").tail(1)
    out = run[["user_id", "age_days"]].copy()
    out["health"] = run["status"].map(_health_label).values
    status = frames["status"]
    if not status.empty:
        outside = status.assign(flag=status["status"].astype(str).str.upper())
        outside = outside[outside["flag"].isin({"BANNED", "SUSPENDED"})]
        flags = dict(zip(outside["user_id"].astype(str), outside["flag"].str.title()))
        out["health"] = [flags.get(str(uid), health) for uid, health in zip(out["user_id"], out["health"])]
    return out


def chart_account_health(frames: Dict[str, pd.DataFrame], run_id: str, out: Path) -> bool:
    health = run_health(frames, run_id)
    if health.empty:
        return False
    counts = health["health"].value_counts()
    counts = counts.reindex([name for name in HEALTH_ORDER if name in counts.index])
    plt = _plt()
    fig, ax = plt.subplots(figsize=(4.8, 4.8))
    ax.pie(
        counts.values,
        labels=[f"{k} ({v})" for k, v in counts.items()],
        colors=[HEALTH_COLORS[k] for k in counts.index],
        autopct="%1.0f%%",
        startangle=90,
    )
    ax.set_title(f"Account health — this run ({len(health)} accounts)")
    _save(fig, out)
    return True


def chart_bans_by_age(frames: Dict[str, pd.DataFrame], run_id: str, out: Path) -> bool:
    health = run_health(frames, run_id)
    health = health[health["age_days"] > 0]
    if health.empty:
        return False
    health = health.assign(
        band=health["age_days"].map(_age_band),
        dead=health["health"].isin({"Banned", "Suspended"}),
    )
    table = health.groupby("band")["dead"].agg(["sum", "count"])
    table = table.reindex([name for _, _, name in AGE_BANDS if name in table.index])
    plt = _plt()
    fig, ax = plt.subplots(figsize=(6, 3.6))
    ax.bar(table.index, table["count"], color="#c9d3e0", label="accounts")
    ax.bar(table.index, table["sum"], color="#d64545", label="banned/suspended")
    ax.set_title("Bans by account age — this run")
    ax.legend(fontsize=8)
    _save(fig, out)
    return True


def chart_shared_ips(frames: Dict[str, pd.DataFrame], out: Path) -> bool:
    ips = frames["ips"]
    if ips.empty:
        return False
    per = ips.groupby(["day", "ip"])["user_id"].nunique()
    shared = per[per >= 2].groupby(level=0).size()
    if shared.empty:
        return False
    plt = _plt()
    fig, ax = plt.subplots(figsize=(7, 3.4))
    ax.bar(shared.index, shared.values, color="#e0a030")
    ax.set_ylabel("IPs on 2+ accounts")
    ax.set_title("Shared proxy IPs per day")
    ax.tick_params(axis="x", rotation=35, labelsize=8)
    _save(fig, out)
    return True


def chart_join_failures(frames: Dict[str, pd.DataFrame], out: Path) -> bool:
    fails = frames["join_fail"]
    if fails.empty:
        return False
    per = fails.groupby("subreddit")["user_id"].nunique().sort_values(ascending=False).head(12)
    if per.empty:
        return False
    plt = _plt()
    fig, ax = plt.subplots(figsize=(7, 3.6))
    colors = ["#d64545" if value >= 3 else "#e0a030" for value in per.values[::-1]]
    ax.barh(per.index[::-1], per.values[::-1], color=colors)
    ax.axvline(3, color="#888", linestyle="--", linewidth=1)
    ax.set_xlabel("accounts that could not Join (3 = blocked)")
    ax.set_title("Join failures by subreddit")
    _save(fig, out)
    return True


CHARTS: List[Tuple[str, str, Callable[..., bool], bool]] = [
    ("run_targets.png", "This run: done vs target", chart_run_targets, True),
    ("run_minutes.png", "This run: sitting length", chart_run_minutes, True),
    ("history.png", "Activity per day", chart_history, False),
    ("comment_outcomes.png", "Comment outcomes per day", chart_comment_outcomes, False),
    ("removal_by_tone.png", "Removal rate by tone", chart_removal_by_tone, False),
    ("top_subreddits.png", "Top subreddits", chart_top_subreddits, False),
    ("account_health.png", "Account health — this run", chart_account_health, True),
    ("bans_by_age.png", "Bans by account age — this run", chart_bans_by_age, True),
    ("shared_ips.png", "Shared proxy IPs", chart_shared_ips, False),
    ("join_failures.png", "Join failures", chart_join_failures, False),
]


# ---------------------------------------------------------------------------
# Run analysis: plain-text sections shared by the terminal and the page
# ---------------------------------------------------------------------------

_PROFILE_RE = re.compile(r"\[Profile ([^\]]+)\]")
_LOG_STAMP_RE = re.compile(r"run-(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})\.log$")
_JOIN_CLICK_RE = re.compile(r"Clicked Join on r/([A-Za-z0-9_]+)")

# (label, pattern, count distinct accounts instead of lines, hint)
PROBLEM_PATTERNS: List[Tuple[str, re.Pattern, bool, str]] = [
    ("Browser did not start", re.compile(r"Browser did not start"), False,
     "those accounts are retried later in the run"),
    ("AdsPower API unreachable", re.compile(r"NameResolutionError|Max retries exceeded"), False,
     "check AdsPower is open; 127.0.0.1:50325 avoids the local.adspower.net DNS lookup"),
    ("Browser window closed mid-sitting", re.compile(r"no such window|target window already closed"), True,
     "Chrome/AdsPower closed or crashed — often low RAM"),
    ("Sittings ended early (FAILED)", re.compile(r"\bFAILED — "), False,
     "slow pages or a frozen tab; the account still closes cleanly"),
    ("Page loads timed out", re.compile(r"Page load timed out"), False,
     "slow proxy or Reddit"),
    ("Join clicks failed", re.compile(r"Join button not found|click did not fire"), False,
     "layout change or gated community; 3 accounts failing blocks the sub"),
    ("Comment drafts rejected", re.compile(r"Comment draft rejected"), False,
     "the on-topic gate threw the draft away"),
]

ANALYSIS_ORDER = (
    "Totals",
    "Compared with previous run",
    "Problems found",
    "Per community",
    "Coming next",
)


def latest_run_log() -> Optional[Path]:
    try:
        logs = sorted(Path(LOGS_DIR).glob("run-*.log"))
    except Exception:
        return None
    return logs[-1] if logs else None


def _log_started_at(path: Optional[Path]) -> Optional[float]:
    if not path:
        return None
    match = _LOG_STAMP_RE.search(str(path))
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y-%m-%d_%H-%M-%S").timestamp()
    except ValueError:
        return None


def _plain(name: object) -> str:
    return str(name or "").strip().removeprefix("u/").lower()


def _examples(names: List[str], limit: int = 3) -> str:
    unique = list(dict.fromkeys(name for name in names if name))
    if not unique:
        return ""
    more = f" +{len(unique) - limit}" if len(unique) > limit else ""
    return " (" + ", ".join(unique[:limit]) + more + ")"


def _run_rows(sit: pd.DataFrame, run_id: str) -> pd.DataFrame:
    if sit.empty or not run_id:
        return pd.DataFrame()
    return sit[sit["run_id"] == run_id]


def _run_metrics(run: pd.DataFrame) -> Dict[str, float]:
    if run.empty:
        return {}
    ok = run[run["sitting_ok"] == 1]
    dead = run["status"].astype(str).str.lower().isin({"banned", "suspended"})
    return {
        "accounts": len(run),
        "ok": len(ok),
        "no browser": int((run["sitting_ok"] != 1).sum()),
        "banned/suspended": int(dead.sum()),
        "avg minutes": round(float(ok["minutes"].mean()), 1) if not ok.empty else 0.0,
        "upvotes": int(run["upvotes"].sum()),
        "upvote target": int(run["upvote_target"].sum()),
        "comments": int(run["comments"].sum()),
        "comment target": int(run["comment_target"].sum()),
        "joins": int(run["joins"].sum()),
        "posts": int(run["posts"].sum()),
    }


def _pct(done: float, target: float) -> str:
    return f"{done / target:.0%}" if target else "n/a"


def _section_totals(run: pd.DataFrame) -> List[str]:
    m = _run_metrics(run)
    if not m:
        return ["no sittings saved for this run"]
    ok = run[run["sitting_ok"] == 1]
    ips = run[run["ip"].astype(str).str.len() > 0].groupby("ip")["user_id"].nunique()
    shared = ips[ips >= 2]
    return [
        f"Accounts ran       : {m['accounts']}  (ok {m['ok']}, no browser {m['no browser']}, "
        f"banned/suspended {m['banned/suspended']})",
        f"Sitting minutes    : avg {m['avg minutes']}, total {ok['minutes'].sum():.0f}",
        f"Upvotes            : {m['upvotes']} of {m['upvote target']} target ({_pct(m['upvotes'], m['upvote target'])})",
        f"Comments           : {m['comments']} of {m['comment target']} target ({_pct(m['comments'], m['comment target'])})",
        f"Joins              : {m['joins']}",
        f"Posts              : {m['posts']}",
        f"Shared proxy IPs   : {len(shared)} IPs used by 2+ accounts"
        + (f" ({int(shared.sum())} accounts)" if len(shared) else ""),
    ]


def _section_compare(sit: pd.DataFrame, run_id: str, run: pd.DataFrame) -> List[str]:
    if sit.empty or run.empty:
        return ["no previous run to compare"]
    older = sit[(sit["run_id"] != run_id) & (sit["id"] < int(run["id"].min()))]
    if older.empty:
        return ["no previous run to compare"]
    prev_id = str(older.sort_values("id")["run_id"].iloc[-1])
    now, prev = _run_metrics(run), _run_metrics(_run_rows(sit, prev_id))
    lines = [f"previous run: {prev_id}"]
    for key, value in now.items():
        before = prev.get(key, 0)
        diff = value - before
        sign = "+" if diff > 0 else ""
        diff_text = f"{sign}{diff:.1f}" if isinstance(diff, float) and not float(diff).is_integer() else f"{sign}{int(diff)}"
        lines.append(f"{key:<18} : {value} (prev {before}, {diff_text})")
    return lines


def _read_log(path: Optional[Path]) -> Tuple[List[str], Dict[str, List[str]]]:
    if not path or not Path(path).exists():
        return [], {}
    lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    by_account: Dict[str, List[str]] = defaultdict(list)
    for line in lines:
        match = _PROFILE_RE.search(line)
        if match:
            by_account[_plain(match.group(1))].append(line)
    return lines, by_account


def _comment_miss_reason(lines: List[str]) -> str:
    text = "\n".join(lines)
    if re.search(r"no such window|target window already closed", text):
        return "browser window closed"
    drafted = re.findall(r"Comment draft rejected — (.+)", text)
    if drafted:
        return f"draft rejected ({drafted[-1].strip()[:50]})"
    if "No help, question, suggestion, or review post left" in text:
        return "no question/help post left"
    if "RL chose lurk" in text:
        return "RL chose lurk"
    if re.search(r"FAILED — ", text):
        return "sitting ended early"
    return "comment step not reached"


def _section_problems(run: pd.DataFrame, log_path: Optional[Path]) -> List[str]:
    lines, by_account = _read_log(log_path)
    out: List[str] = []
    if not lines:
        out.append("run log not found — only sitting numbers checked")
    for label, pattern, per_account, hint in PROBLEM_PATTERNS:
        hits = [line for line in lines if pattern.search(line)]
        if not hits:
            continue
        names = [_PROFILE_RE.search(line).group(1) for line in hits if _PROFILE_RE.search(line)]
        count = len(set(names)) if per_account else len(hits)
        amount = f"{count} account{'s' if count != 1 else ''}" if per_account else f"{count}x"
        extra = ""
        if label == "Browser did not start" and not run.empty:
            ok_names = {_plain(name) for name in run[run["sitting_ok"] == 1]["account"]}
            retried = len({_plain(name) for name in names} & ok_names)
            extra = f", {retried} finished ok after a retry"
        out.append(f"{label}: {amount}{extra}{_examples(names)} — {hint}")
    if not run.empty:
        missed = (run["upvote_target"] - run["upvotes"]).clip(lower=0)
        total_missed = int(missed.sum())
        if total_missed:
            skips = Counter(
                m.group(1).strip()
                for line in lines
                for m in [re.search(r"Upvote skipped — (.+)$", line)]
                if m
            )
            short = run[missed > 0]["account"].astype(str).str.removeprefix("u/").tolist()
            out.append(
                f"Upvotes missed: {total_missed} of {int(run['upvote_target'].sum())} on "
                f"{len(short)} accounts{_examples(short)}"
            )
            for reason, count in skips.most_common(3):
                out.append(f"  - skipped {count}x: {reason}")
            unseen = total_missed - sum(skips.values())
            if unseen > 0:
                out.append(f"  - {unseen} never reached (no upvote button hit before time ran out)")
        wants = run[(run["age_days"] >= 8) & (run["comment_target"] > run["comments"])]
        if not wants.empty:
            out.append(f"Comments missed on day 8+ accounts: {len(wants)}")
            reasons = Counter()
            examples: Dict[str, List[str]] = defaultdict(list)
            for name in wants["account"].astype(str).str.removeprefix("u/"):
                why = _comment_miss_reason(by_account.get(_plain(name), []))
                reasons[why] += 1
                examples[why].append(name)
            for why, count in reasons.most_common():
                out.append(f"  - {count}x {why}{_examples(examples[why])}")
    if not out:
        out.append("no problems found")
    return out


def _load_extra(db_path: str, since_iso: str) -> Dict[str, pd.DataFrame]:
    frames = {"joins": pd.DataFrame(), "touches": pd.DataFrame()}
    if not Path(db_path).exists():
        return frames
    conn = sqlite3.connect(db_path)
    try:
        frames["joins"] = _read(
            conn,
            f"SELECT subreddit, user_id FROM account_joins WHERE created_at >= '{since_iso}'",
        )
        frames["touches"] = _read(
            conn,
            f"SELECT subreddit, url, kind, user_id FROM thread_touches WHERE created_at >= '{since_iso}'",
        )
    finally:
        conn.close()
    touches = frames["touches"]
    if not touches.empty:
        from_url = touches["url"].astype(str).str.extract(r"/r/([A-Za-z0-9_]+)", expand=False).fillna("")
        touches["subreddit"] = touches["subreddit"].fillna("").astype(str).where(
            touches["subreddit"].fillna("").astype(str) != "", from_url
        )
    return frames


def _visits_since(since: float) -> Counter:
    visits: Counter = Counter()
    try:
        data = json.loads(Path(SUBREDDIT_LOG_PATH).read_text(encoding="utf-8"))
    except Exception:
        return visits
    for entry in data.values() if isinstance(data, dict) else []:
        if not isinstance(entry, dict):
            continue
        for key in ("runs", "explored_runs"):
            for item in entry.get(key) or []:
                if isinstance(item, dict) and float(item.get("at") or 0) >= since:
                    visits.update(_plain(name).removeprefix("r/") for name in item.get("subs") or [] if name)
    return visits


def _section_communities(
    frames: Dict[str, pd.DataFrame], db_path: str, since: float, log_path: Optional[Path]
) -> List[str]:
    since_iso = datetime.fromtimestamp(since).isoformat(timespec="seconds")
    extra = _load_extra(db_path, since_iso)
    visits = _visits_since(since)
    log_lines, _ = _read_log(log_path)
    log_joins = Counter(
        _plain(m.group(1)) for line in log_lines for m in [_JOIN_CLICK_RE.search(line)] if m
    )

    def _count(df: pd.DataFrame, where=None) -> Counter:
        if df.empty or "subreddit" not in df:
            return Counter()
        if where is not None:
            df = df[where(df)]
        return Counter(df["subreddit"].astype(str).map(_plain))

    joins = log_joins or _count(extra["joins"])
    votes = _count(extra["touches"], lambda d: d["kind"].astype(str) == "vote")
    touches = _count(extra["touches"])
    com = frames["comments"]
    run_com = com[com["created_at"].astype(str) >= since_iso] if not com.empty else com
    comments = _count(run_com)
    week_iso = (datetime.now() - timedelta(days=7)).isoformat(timespec="seconds")
    week = com[com["created_at"].astype(str) >= week_iso] if not com.empty else com
    rates = _removal_rate(week.assign(subreddit=week["subreddit"].map(_plain)), "subreddit") if not week.empty else pd.DataFrame()
    names = set(visits) | set(joins) | set(comments) | set(touches)
    names.discard("")
    if not names:
        return ["no community activity recorded this run"]
    ranked = sorted(names, key=lambda n: (-visits[n], -joins[n], -comments[n], n))[:15]
    out = [f"{'community':<24} {'visits':>6} {'joins':>5} {'upvotes':>7} {'touches':>7} {'comments':>8}  removed (7d)"]
    for name in ranked:
        rate = ""
        if not rates.empty and name in rates.index:
            row = rates.loc[name]
            rate = f"{row['rate']:.0%} of {int(row['checked'])}"
        out.append(
            f"r/{name:<22} {visits[name]:>6} {joins[name]:>5} {votes[name]:>7} "
            f"{touches[name]:>7} {comments[name]:>8}  {rate or '-'}"
        )
    if len(names) > len(ranked):
        out.append(f"... {len(names) - len(ranked)} more communities")
    return out


def _section_next(run: pd.DataFrame) -> List[str]:
    if run.empty:
        return ["no account ages this run"]
    ages = run[run["age_days"] > 0]["age_days"].astype(float)
    if ages.empty:
        return ["no account ages were read this run"]
    out = []
    for day, what in ((4, "upvotes + joins"), (8, "comments"), (14, "posts")):
        already = int((ages >= day).sum())
        soon = int(((ages < day) & (ages + 1 >= day)).sum())
        out.append(f"Day {day:<2} ({what:<15}): {already} already there, {soon} more by tomorrow")
    return out


def analyze_run(
    run_id: Optional[str] = None,
    run_log_path: Optional[str] = None,
    run_started_at: Optional[float] = None,
    db_path: Optional[str] = None,
    frames: Optional[Dict[str, pd.DataFrame]] = None,
) -> Dict[str, List[str]]:
    """Sections of plain-text lines describing one run."""
    path = str(db_path or _DB_PATH)
    frames = frames if frames is not None else load_frames(path)
    run_id = run_id or _latest_run_id(frames)
    log_path = Path(run_log_path) if run_log_path else latest_run_log()
    run = _run_rows(frames["sittings"], run_id)
    since = run_started_at or _log_started_at(log_path)
    if not since:
        since = (datetime.now() - timedelta(days=1)).timestamp()
    builders: Dict[str, Callable[[], List[str]]] = {
        "Totals": lambda: _section_totals(run),
        "Compared with previous run": lambda: _section_compare(frames["sittings"], run_id, run),
        "Problems found": lambda: _section_problems(run, log_path),
        "Per community": lambda: _section_communities(frames, path, float(since), log_path),
        "Coming next": lambda: _section_next(run),
    }
    sections: Dict[str, List[str]] = {}
    for name in ANALYSIS_ORDER:
        try:
            sections[name] = builders[name]()
        except Exception as exc:
            sections[name] = [f"could not build this section ({type(exc).__name__}: {exc})"]
    return sections


def format_analysis(sections: Dict[str, List[str]], width: int = 78) -> List[str]:
    """Framed text block for the terminal and analysis.txt."""
    bar = "=" * width
    out = [bar, "RUN ANALYSIS".center(width), bar]
    for name in ANALYSIS_ORDER:
        if name not in sections:
            continue
        out.append("")
        out.append(f"-- {name} " + "-" * max(0, width - len(name) - 4))
        out.extend(sections[name])
    out.append(bar)
    return out


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------


def _latest_run_id(frames: Dict[str, pd.DataFrame]) -> str:
    sit = frames["sittings"]
    if sit.empty:
        return ""
    return str(sit.sort_values("id")["run_id"].iloc[-1])


def _headline(frames: Dict[str, pd.DataFrame], run_id: str) -> List[Tuple[str, str]]:
    sit, com = frames["sittings"], frames["comments"]
    run = sit[sit["run_id"] == run_id] if (not sit.empty and run_id) else pd.DataFrame()
    items: List[Tuple[str, str]] = []
    if not run.empty:
        items.append(("Accounts this run", str(len(run))))
        items.append(("Comments this run", str(int(run["comments"].sum()))))
        items.append(("Upvotes this run", str(int(run["upvotes"].sum()))))
    if not com.empty:
        checked = com[com["outcome"] != "unchecked"]
        rate = (checked["outcome"] == "removed").mean() if not checked.empty else None
        items.append(("Comments total", str(len(com))))
        items.append(("Removed (checked)", f"{rate:.0%}" if rate is not None else "n/a"))
    health = run_health(frames, run_id)
    if not health.empty:
        dead = health["health"].isin({"Banned", "Suspended"}).mean()
        items.append(("Banned/suspended this run", f"{dead:.0%}"))
    return items


def _analysis_html(sections: Optional[Dict[str, List[str]]]) -> str:
    if not sections:
        return ""
    blocks = "".join(
        f"<h3>{html.escape(name)}</h3><pre>{html.escape(chr(10).join(sections[name]))}</pre>"
        for name in ANALYSIS_ORDER
        if name in sections
    )
    return f'<section class="analysis"><h2>Run analysis</h2>{blocks}</section>'


def _page(
    title: str,
    stamp: str,
    cards: List[Tuple[str, str]],
    charts: List[Tuple[str, str]],
    prefix: str,
    analysis: Optional[Dict[str, List[str]]] = None,
) -> str:
    card_html = "".join(
        f'<div class="card"><div class="v">{html.escape(v)}</div><div class="k">{html.escape(k)}</div></div>'
        for k, v in cards
    )
    chart_html = "".join(
        f'<figure><img src="{html.escape(prefix + name)}" alt="{html.escape(label)}">'
        f"<figcaption>{html.escape(label)}</figcaption></figure>"
        for name, label in charts
    ) or "<p>No data yet — run the joiner first.</p>"
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{html.escape(title)}</title>
<style>
body {{ font-family: system-ui, sans-serif; margin: 24px; background: #f6f7f9; color: #222; }}
h1 {{ margin: 0 0 4px; font-size: 22px; }}
.sub {{ color: #666; margin-bottom: 18px; font-size: 13px; }}
.cards {{ display: flex; flex-wrap: wrap; gap: 12px; margin-bottom: 20px; }}
.card {{ background: #fff; border-radius: 8px; padding: 12px 16px; min-width: 130px; box-shadow: 0 1px 2px #0001; }}
.card .v {{ font-size: 22px; font-weight: 600; }}
.card .k {{ font-size: 12px; color: #666; }}
.grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(460px, 1fr)); gap: 16px; }}
figure {{ background: #fff; margin: 0; padding: 10px; border-radius: 8px; box-shadow: 0 1px 2px #0001; }}
img {{ width: 100%; height: auto; }}
figcaption {{ font-size: 12px; color: #666; margin-top: 4px; }}
.analysis {{ background: #fff; border-radius: 8px; padding: 12px 18px; margin-bottom: 20px; box-shadow: 0 1px 2px #0001; }}
.analysis h2 {{ margin: 4px 0 8px; font-size: 18px; }}
.analysis h3 {{ margin: 14px 0 4px; font-size: 14px; color: #3b6fb6; }}
.analysis pre {{ margin: 0; font-size: 12px; white-space: pre-wrap; }}
</style></head><body>
<h1>{html.escape(title)}</h1>
<div class="sub">Built {html.escape(stamp)} from data/activity.db</div>
<div class="cards">{card_html}</div>
{_analysis_html(analysis)}
<div class="grid">{chart_html}</div>
</body></html>
"""


def build_report(
    run_id: Optional[str] = None,
    db_path: Optional[str] = None,
    run_log: Optional[str] = None,
    run_started_at: Optional[float] = None,
) -> str:
    """Write the charts and index.html; returns the path of reports/latest.html."""
    return build_report_with_analysis(run_id, db_path, run_log, run_started_at)[0]


def build_report_with_analysis(
    run_id: Optional[str] = None,
    db_path: Optional[str] = None,
    run_log: Optional[str] = None,
    run_started_at: Optional[float] = None,
) -> Tuple[str, Dict[str, List[str]]]:
    """Like build_report, also returning the run analysis sections."""
    _plt()
    frames = load_frames(db_path)
    run = run_id or _latest_run_id(frames)
    now = datetime.now()
    stamp = now.strftime("%Y-%m-%d_%H-%M")
    folder = Path(REPORTS_DIR) / stamp
    folder.mkdir(parents=True, exist_ok=True)
    written: List[Tuple[str, str]] = []
    for name, label, fn, needs_run in CHARTS:
        try:
            ok = fn(frames, run, folder / name) if needs_run else fn(frames, folder / name)
        except Exception:
            ok = False
        if ok:
            written.append((name, label))
    cards = _headline(frames, run)
    sections = analyze_run(run, run_log, run_started_at, db_path, frames=frames)
    try:
        (folder / "analysis.txt").write_text("\n".join(format_analysis(sections)) + "\n", encoding="utf-8")
    except Exception:
        pass
    built = now.strftime("%Y-%m-%d %H:%M")
    title = "Reddit joiner analytics"
    (folder / "index.html").write_text(
        _page(title, built, cards, written, "", sections), encoding="utf-8"
    )
    latest = Path(REPORTS_DIR) / "latest.html"
    latest.write_text(_page(title, built, cards, written, f"{stamp}/", sections), encoding="utf-8")
    return str(latest), sections


def main() -> int:
    try:
        path, sections = build_report_with_analysis()
    except ImportError:
        print("Analytics skipped — pip install matplotlib")
        return 1
    for line in format_analysis(sections):
        print(line)
    print(f"Charts: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
