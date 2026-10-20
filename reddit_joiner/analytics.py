#!/usr/bin/env python3
"""Charts from activity.db (pandas + matplotlib) and one HTML page to view them.

Run on its own:  python3 -m reddit_joiner.analytics
"""

from __future__ import annotations

import html
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import pandas as pd

from reddit_joiner.paths import DB_PATH as _DB_PATH, REPORTS_DIR

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


def chart_account_health(frames: Dict[str, pd.DataFrame], out: Path) -> bool:
    status = frames["status"]
    if status.empty:
        return False
    counts = status["status"].astype(str).str.upper().value_counts()
    if counts.empty:
        return False
    colors = {"LIVE": "#2e9e5b", "BANNED": "#d64545", "SUSPENDED": "#e0a030"}
    plt = _plt()
    fig, ax = plt.subplots(figsize=(4.6, 4.6))
    ax.pie(
        counts.values,
        labels=[f"{k} ({v})" for k, v in counts.items()],
        colors=[colors.get(k, "#9aa4b2") for k in counts.index],
        autopct="%1.0f%%",
        startangle=90,
    )
    ax.set_title("Account health (outside + inside checks)")
    _save(fig, out)
    return True


def chart_bans_by_age(frames: Dict[str, pd.DataFrame], out: Path) -> bool:
    sit, status = frames["sittings"], frames["status"]
    if sit.empty or status.empty:
        return False
    known = sit[sit["age_days"] > 0].sort_values("id").groupby("user_id").tail(1)[["user_id", "age_days"]]
    merged = known.merge(status, on="user_id", how="inner")
    if merged.empty:
        return False
    merged["band"] = merged["age_days"].map(_age_band)
    merged["dead"] = merged["status"].astype(str).str.upper().isin({"BANNED", "SUSPENDED"})
    table = merged.groupby("band")["dead"].agg(["sum", "count"])
    table = table.reindex([name for _, _, name in AGE_BANDS if name in table.index])
    plt = _plt()
    fig, ax = plt.subplots(figsize=(6, 3.6))
    ax.bar(table.index, table["count"], color="#c9d3e0", label="accounts")
    ax.bar(table.index, table["sum"], color="#d64545", label="banned/suspended")
    ax.set_title("Bans by account age (last age seen)")
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
    ("account_health.png", "Account health", chart_account_health, False),
    ("bans_by_age.png", "Bans by account age", chart_bans_by_age, False),
    ("shared_ips.png", "Shared proxy IPs", chart_shared_ips, False),
    ("join_failures.png", "Join failures", chart_join_failures, False),
]


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------


def _latest_run_id(frames: Dict[str, pd.DataFrame]) -> str:
    sit = frames["sittings"]
    if sit.empty:
        return ""
    return str(sit.sort_values("id")["run_id"].iloc[-1])


def _headline(frames: Dict[str, pd.DataFrame], run_id: str) -> List[Tuple[str, str]]:
    sit, com, status = frames["sittings"], frames["comments"], frames["status"]
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
    if not status.empty:
        dead = status["status"].astype(str).str.upper().isin({"BANNED", "SUSPENDED"}).mean()
        items.append(("Banned/suspended", f"{dead:.0%}"))
    return items


def _page(title: str, stamp: str, cards: List[Tuple[str, str]], charts: List[Tuple[str, str]], prefix: str) -> str:
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
</style></head><body>
<h1>{html.escape(title)}</h1>
<div class="sub">Built {html.escape(stamp)} from data/activity.db</div>
<div class="cards">{card_html}</div>
<div class="grid">{chart_html}</div>
</body></html>
"""


def build_report(run_id: Optional[str] = None, db_path: Optional[str] = None) -> str:
    """Write the charts and index.html; returns the path of reports/latest.html."""
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
    built = now.strftime("%Y-%m-%d %H:%M")
    title = "Reddit joiner analytics"
    (folder / "index.html").write_text(_page(title, built, cards, written, ""), encoding="utf-8")
    latest = Path(REPORTS_DIR) / "latest.html"
    latest.write_text(_page(title, built, cards, written, f"{stamp}/"), encoding="utf-8")
    return str(latest)


def main() -> int:
    try:
        path = build_report()
    except ImportError:
        print("Analytics skipped — pip install matplotlib")
        return 1
    print(f"Analytics: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
