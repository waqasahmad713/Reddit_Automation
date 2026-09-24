# Reddit AdsPower automation — project document

This is the full description of the project: what it is, how a run works, what you put in the sheets, and what you should expect.

The program opens **AdsPower Chrome profiles** that are already logged into Reddit. It browses like a person, leaves comments, optionally edits a comments.csv comment after a few minutes, and may submit one text post if karma is high enough. A Deep Q-Network (RL) remembers what worked so later runs can pick better comment tones and post communities.

Main entry: `./run.sh` or `python3 -m reddit_joiner`. Sitting-by-sitting path: [WORKFLOW.md](WORKFLOW.md).

---

## 1. What this project is for

You have several Reddit accounts, each in its own AdsPower browser profile (own cookies, proxy, fingerprint). The script:

1. Starts those profiles through the AdsPower Local API.
2. Attaches Selenium to that Chrome (not a new empty Chrome).
3. Spends time on Reddit so the account looks active.
4. Each sitting starts with **3–4 minutes** on Reddit Home, then hops **Home → random sheet sub → Home → another sub**. Each sitting uses a **new activity pattern** that account has not used before.
5. Leaves **at most 2 comments per 48 hours** (sheet + general combined). If `comments.csv` has unused links, those go first. If the sheet is empty, two **general** comments (top + new, or new + top).
6. Optionally **edits** that comments.csv comment after 3–4 minutes (if you filled `edit`).
7. Submits **at most 1 post per 48 hours** if karma ≥ 1: `posts.csv` when you filled a row, otherwise one **general post** written after lurking in a sheet community (rules + recent posts). A 1-karma account can try; communities that require more karma are skipped.
8. Closes the profile and saves logs + the RL model.

Comments and posts are typed in the **logged-in AdsPower window**. The script does not log into Reddit with a password. Each profile must already be logged in.

---

## 2. What must be ready before you run

| Requirement | Detail |
| --- | --- |
| AdsPower open | Local API enabled at `http://local.adspower.net:50325` |
| Profiles logged in | Each AdsPower profile already signed into Reddit |
| Python venv | `source ~/myenv/bin/activate` |
| Dependencies | `pip install -r requirements.txt` |
| `.env` | At least one AI key (Gemini / DeepSeek / OpenRouter / Groq / OpenAI) |
| `sheets/accounts.csv` | AdsPower user ids (`enabled=yes` to run that account) |
| `sheets/subreddits.csv` | Extra communities to join/browse (`enabled=yes`) |

Optional in `.env`:

- `HF_TOKEN` — free Hugging Face Inference (huggingface.co/settings/tokens, enable Inference Providers)
- `DEEPSEEK_API_KEY`, `OPENROUTER_API_KEY`, `GROQ_API_KEY`, `GEMINI_API_KEY`, `OPENAI_API_KEY`
- `REDDIT_CLIENT_ID` + `REDDIT_CLIENT_SECRET` — read-only PRAW checks (LIVE score)
- `ADSPOWER_API` — defaults to `http://local.adspower.net:50325`

Do not commit `.env`.

---

## 3. How to run

```bash
source ~/myenv/bin/activate
cd ~/Desktop/reddit
./run.sh
```

Same thing: `python3 -m reddit_joiner`.

**Expected:** AdsPower stays open. Up to **4 Chromes** start (the rest wait). Each account runs about **40 minutes** of Reddit time: Home first, then community hops with Home in between. The 48h post (if karma ≥ 1) happens after lurking, not at the end. Comments are written by local **Ollama `mistral:7b`**.

Ctrl+C still saves `data/rl_model.pkl` and `data/activity.db`.

---

## 4. What one run does

Uses `accounts.csv`. Each enabled account does the full session:

- ~40 minutes browse (unique Home → sub → Home pattern each sitting)
- join **2–3 new** communities (not the same ones as the last few runs)
- **2** comments / **48 hours** — `comments.csv` first, otherwise general (top + new)
- **1** post / **48 hours** if karma ≥ 1 — `posts.csv` first, otherwise a general community post (a community can still refuse a 1-karma account)

---

## 5. What one regular account does (expected timeline)

```
AdsPower Start API
    → wait for proxy IP
    → attach Selenium to that Chrome
    → open reddit.com
    → read username, karma, account age
            │
            ▼
~40 minute clock (fresh pattern each sitting)
    3–4 min random activity on Reddit Home
    for 2–3 NEW communities (from subreddits.csv, random order):
        Open /r/community/about/rules and read them
        Join r/community (or already joined)
        RL chooses comment vs lurk from those rules
        random activity inside that sub (scroll, read posts, sometimes upvote)
        sometimes leave 1 general comment on a top or new post if comments.csv is empty
        stay on that thread ~20–70s
        back to Home for a random stretch, then the next community
    after lurking: posts.csv or a general community post if karma ≥ 1 and the 48h post slot is free
    leftover time stays on Home
            │
            ▼
comments.csv
    open the assigned post URL
    type the `text` (or write from the post if `text` is empty)
    count it only if the comment is visible on the thread
    if `edit` is filled: stay on the post 3–4 min, then append that text
    move the link to commented_links.csv and clear the row
            │
            ▼
AdsPower Stop + summary
```

**What you should see in the log**

- `This run: N new communities ... | 2 general comments on this account's comments.csv links`
- `Comment is on the post: ...` — it really landed
- `Comment not visible ... — not counting it` — Reddit did not keep it
- `Saved commentN to commented_links.csv and cleared it from comments.csv`

**What you should not expect**

- The same three subs (technology / python / automation) every run
- Every account commenting on the same URL
- 6–8 comments dumped in one sitting
- A post from a 1-karma account
- A counted comment that never appears on the thread

---

## 6. comments.csv — your link queue

Paste Reddit post URLs here. This is the **working queue**. Finished links leave this file.

| Column | What to put |
| --- | --- |
| `comment` | Label (`comment1`, `comment2`, …) — leave as-is |
| `link` | Full Reddit post URL |
| `account` | AdsPower user id, **or leave empty** so the script assigns |
| `text` | Exact first comment. Empty = write from the post |
| `edit` | Extra text added **3–4 minutes later** (optional) |
| `status` | Leave empty. Script uses `in progress`, `retry`, `skip` |
| `posted_by` / `posted_at` | Filled by the script, then the row is cleared |

### Rules (this is how it actually behaves)

1. **One comment per unique URL** across all accounts. A second row with the same link is marked `skip`.
2. **Up to 2 comments per 48 hours** (sheet + general together). Unused `comments.csv` links go first.
3. Empty `text` → the bot writes a **general comment** from that post.
4. Empty `account` → next run assigns ids that still have 48h room (fewest sheet comments first).
5. After the comment **lands**, the link is **moved** to `commented_links.csv` and the working row is cleared (`comment1` stays, link/text gone).
6. If submit fails, status becomes `retry` and the same account can try later.
7. If you paste a URL that is already in `commented_links.csv`, it is skipped (no double comment).
8. If `text` has commas, keep it in quotes (Excel/CSV). Otherwise the text can land in the wrong column.

### Optional edit

If `edit` has text:

1. First comment is posted.
2. The tab stays on that post for **3–4 minutes** (light scrolling).
3. The script opens **Edit** on that comment and **appends** your edit text.
4. If edit fails, the original comment stays; the link is still archived.

If `edit` is empty, there is no wait.

### Status values you may see

| Status | Meaning |
| --- | --- |
| empty / ready | Waiting |
| `in progress` | An account claimed this row this run |
| `retry` | Submit failed; try again |
| `skip` | Duplicate URL or already used |

---

## 7. commented_links.csv — history

Every successful comments.csv comment is appended here:

`commented_at`, `comment`, `link`, `account`, `posted_by`, `text`, `edit`

This file is the record of links that already got a comment. Do not delete rows if you want the bot to refuse those URLs later.

---

## 8. posts.csv — text posts (1 per 48 hours)

| Column | What to put |
| --- | --- |
| `post` | Label (`post1`, …) |
| `title` | Exact title (not rewritten) |
| `body` | Exact body (not rewritten) |
| `subreddit` | Community name without `r/` |
| `flair` | Optional. Exact tag if you want one. Empty = pick from the post text |
| `account` | AdsPower id, or empty = any account with room |
| `status` | empty / `draft` / `retry` = ready |

**Caps**

- 1 LIVE post per account per 48 hours
- karma must be **≥ 1**
- 1 community attempt per run
- If it fails, status stays `retry` for another account (max 2 accounts)

LIVE posts are also written to `live_posts.xlsx`.

---

## 9. Accounts and profiles

### `accounts.csv`

AdsPower user ids. Set `enabled` to `yes` or `no`. You can use user id, profile name, or serial.

### `subreddits.csv`

Extra communities to join and browse. Add a row per subreddit, `enabled=yes`. The script still mixes in karma-friendly pools so it does not only use this list.

### Parallel

- `PARALLEL_PROFILES = True`
- `MAX_PARALLEL_PROFILES = 4` — four Chromes at a time; others wait
- A few seconds stagger between launches so AdsPower is not flooded

---

## 10. Communities (not the same every run)

The old fixed list (technology / python / automation) is only a fallback.

Each regular run:

1. Uses **only** enabled rows in `subreddits.csv` (no extra karma-pool communities).
2. Uses **only** enabled AdsPower ids in `accounts.csv` (not `profiles.txt`).
3. Drops communities this account used in the last **4** runs (`.adspower_subreddit_log.json`).
4. Picks **2–3** from that sheet and browses them.

New / low-karma accounts get newbie-friendly subs first. Older accounts can get growing / established ones too.

---

## 11. Comments — three kinds

| Kind | When | How many |
| --- | --- | --- |
| **General** | During the session, on **top** and **new** posts | 2 per session, up to 16/week |
| **Sheet** | Same 2 comments, on `comments.csv` links you assigned | 2 per run if that account has links |
| **Karma fill** | Only if that account has **no** comments.csv links | Fills up to the 2 target on top/new posts in the new subs |

If `comments.csv` has `link` + `account`, the bot does **not** also leave random general comments that sitting. Empty `text` means it writes a general comment from the post.

A comment is counted **only if it is visible on the post** (or the composer cleared after a real submit). “Posted” in one second without that check is not used anymore.

Most of the sitting is **lurking**: scroll, pause, open a post, read comments, sometimes upvote, often leave without commenting. Each account rolls a fresh sitting (about 34–46 minutes, post not always at the exact middle, listing order and scroll mix change). Comments are spaced a few minutes apart. That is normal Reddit use (read more than you post). It is not a stealth or ban-evasion layer.

New accounts (1 day old, 1 karma) often have comments **filtered**. Check the post while logged in as that user, not only Overview.

---

## 12. Reinforcement learning (RL)

File: `reddit_joiner/rl.py`. Model: `data/rl_model.pkl`. History: `data/activity.db`.

This is a **Deep Q-Network (DQN)**: a small neural net scores each action, a replay buffer stores past outcomes, and a target network keeps training stable. The old tabular Q-table is not reused.

RL does **not** choose which Chrome to open or how long to scroll. It only learns:

| Decision | When | Choices |
| --- | --- | --- |
| Comment vs lurk | After reading that community's rules | `join:comment` or `join:lurk` |
| Comment tone | Every general / sheet comment | friendly, expert, funny, neutral |
| Skip or comment | Optional browse comments only | skip, or a tone |
| Which subreddit | `posts.csv` submit | Row subreddit + fallbacks |

**State the net sees** (20 numbers + hashed subreddit / account / rules): karma, account age, post mood, post length, hour, weekday, hour/day sin-cos, sheet vs browse vs post, comments this session, recent success rate, weekend, comment length, rule count, rule strictness, rule flags, and whether rules were read.

On join, the bot opens `/r/sub/about/rules`, reads the public rules (and `rules.json`), then joins. Comments are written with those rules in the prompt. If the rules discourage jokes and RL picked a funny tone, the comment is rewritten in a plain tone and RL gets a small penalty. Later live / removed / score checks still train the network.

**When a comment is written**, the bot stores the comment permalink and waits. On the next run (and again at the end of a long sitting) it checks Reddit:

1. Is the comment still **live**, **filtered**, or **removed**?
2. What is the **score**?
3. That outcome becomes a delayed reward and the DQN is trained.

Comments younger than 20 minutes are left for later. Unknown after 3 days is closed with reward 0.

**Rewards**

| Outcome | Reward |
| --- | --- |
| Comment visible | +2 |
| Comment failed | −1 |
| Comment still live, score 0–1 | +1 |
| Comment score ≥ 2 / ≥ 5 / ≥ 20 | +2 / +5 / +10 |
| Comment score &lt; 0 / ≤ −3 | −2 / −5 |
| Comment filtered / collapsed | −3 |
| Comment removed or deleted | −8 |
| Post LIVE | +10 |
| Post removed (spam-like) | −10 |
| Post removed by mods | −15 |
| All post attempts failed | −20 |
| Rules page read | +0.5 |
| Join succeeded / failed | +1 / −1 |
| Lurk on a strict-rule sub | +0.4 |
| Comment allowed on a loose-rule sub | +0.2 |
| Tone clashes with rules / fits | −0.4 / +0.2 |

It explores more on new communities (ε around 0.22, floor 0.10). DQN step size is `RL_DQN_LR` (0.001). If the RL file breaks, browse and comments still run.

Set `RL_ENABLED = False` in `reddit_joiner/joiner.py` to turn learning off.

---

## 13. How comments are written (AI)

Order: **[OmniRoute](https://github.com/diegosouzapw/OmniRoute)** (`http://localhost:20128/v1`) → local **Ollama `mistral:7b`** → Hugging Face → DeepSeek → OpenRouter → Groq → Gemini → OpenAI.

The joiner starts OmniRoute’s Docker image for you and connects local Ollama. You do not need the dashboard. If OmniRoute is down, comments go straight to Ollama `mistral:7b`.

1. VADER scores the post mood.
2. Mistral writes 1–2 sentences about **that** post (or uses your `text` column as-is).
3. If Ollama is down and every API fails, a local sentence from the post topic is used.
4. Typing is real keypresses (uneven delays, rare backspace), then Comment / Reply — not the Post tab.

`posts.csv` title and body are **never** rewritten by AI. The bot does pick a community **flair/tag** from the post text before it clicks **Post**.

---

## 14. Project files

| File | Role |
| --- | --- |
| `run.sh` | Starts the joiner (`python3 -m reddit_joiner`) |
| `reddit_joiner/joiner.py` | Main bot: AdsPower, browse, comments, posts |
| `reddit_joiner/ai.py` | VADER + LLM comments |
| `reddit_joiner/omniroute.py` | Starts OmniRoute Docker and connects Ollama |
| `reddit_joiner/karma.py` | Newbie / growing community lists |
| `reddit_joiner/rl.py` | Deep Q-Network (DQN) |
| `reddit_joiner/store.py` | `data/activity.db`, live posts, retry helper |
| `reddit_joiner/paths.py` | Sheet and data folder locations |
| `reddit_joiner/rules.py` | Parse / cache public subreddit rules |
| `data/subreddit_rules.json` | Cached community rules (generated) |
| `sheets/accounts.csv` | AdsPower ids to open |
| `sheets/subreddits.csv` | Extra communities to browse |
| `sheets/comments.csv` | Link queue you paste |
| `sheets/commented_links.csv` | Finished comments.csv links |
| `sheets/posts.csv` | Text posts (1 per 48 hours) |
| `.env` | API keys (do not commit) |
| `.env.example` | Key names only |
| `requirements.txt` | Python packages |
| `data/rl_model.pkl` | Saved DQN weights |
| `data/activity.db` | Comments, post attempts, RL experience |
| `data/live_posts.xlsx` | Posts that checked LIVE |
| `data/adspower_comment_log.json` | Local comment log (not Reddit) |
| `data/adspower_post_log.json` | Local post log |
| `data/adspower_profile_cache.json` | AdsPower id → name cache |
| `data/adspower_subreddit_log.json` | Communities used in recent runs |
| `data/session_patterns.json` | Activity fingerprints already used per account |

---

## 15. Settings you can change

All of these live at the top of `reddit_joiner/joiner.py`.

| Setting | Current | Meaning |
| --- | --- | --- |
| `ACCOUNT_SESSION_SECONDS` | 40 × 60 | Default browse length (each sitting rolls 34–46 min) |
| `SESSION_SECONDS_RANGE` | 34–46 min | Per-account sitting length this run |
| `POST_IN_SESSION_RANGE` | 38–62% | When a post may be attempted in that sitting |
| `SUBREDDITS_PER_SESSION` | 2–3 | How many new communities to join |
| `ACTIVITY_ON_SUBREDDIT` | 180–260 s | General activity inside each new sub |
| `HOME_ACTIVITY_BEFORE_JOIN` | 180–260 s | First 3–4 min on Reddit Home |
| `HOME_BETWEEN_SUBS` | 90–240 s | Random Home time between communities |
| `ACTION_WINDOW_HOURS` | 48 | Comment + post window per account |
| `COMMENTS_PER_WINDOW` | 2 | Sheet + general comments in that window |
| `POSTS_PER_WINDOW` | 1 | LIVE posts in that window |
| `SHEET_COMMENTS_PER_RUN` | 2 | comments.csv comments per session (same 48h budget) |
| `COMMENT_EDIT_WAIT` | 180–240 s | Wait before appending `edit` text |
| `GENERAL_POSTS_PER_WEEK` | 1 | Same as `POSTS_PER_WINDOW` |
| `MIN_KARMA_TO_POST` | 1 | Below this: comments only |
| `MAX_PARALLEL_PROFILES` | 4 | Chromes open at once |
| `PARALLEL_PROFILES` | True | Run accounts together |
| `BETWEEN_COMMENTS` | 90–210 s | Gap between general comments |
| `THREAD_READ` | 14–32 s | Read a post before commenting or leaving |
| `AFTER_COMMENT_LINGER` | 22–70 s | Stay on the thread after a comment |
| `UPVOTE_CHANCE` | 0.10 | Occasional upvote while reading |
| `RL_ENABLED` | True | Learn from outcomes |
| `RL_DQN_LR` | 0.001 | Neural-net step size |
| `RL_REPLAY_SIZE` | 3000 | Past outcomes kept for training |
| `RL_BATCH_SIZE` | 16 | Transitions per train step |
| `RL_TARGET_SYNC` | 20 | Copy online net → target net |
| `RL_STATUS_MIN_AGE` | 20 min | Wait before scoring a comment |
| `KARMA_GROWTH_ENABLED` | True | Use newbie-friendly subs |

---

## 16. Caps at a glance (per account)

| Action | Per 48 hours | Notes |
| --- | --- | --- |
| Comments (sheet + general) | 2 | Sheets first if `comments.csv` has unused links |
| Same comments.csv URL | 1 across **all** accounts | Archived in `commented_links.csv` |
| Text post | 1 LIVE, karma ≥ 1 | `posts.csv` first; generated post if empty |
| Communities joined / browsed | 2–3 new per sitting | rotates; last 4 runs skipped |

---

## 17. What “success” looks like

For each account:

- Joined or already-in several **new** `r/...` names
- 2 lines like `Comment is on the post`
- If it had a comments.csv row: that link is gone from `comments.csv` and present in `commented_links.csv`
- Post skipped with `karma N < 1` until karma grows, or if the 48h post was already used
- RL line with actions / success % / epsilon

---

## 18. If something does not work

| Symptom | Likely cause | What to do |
| --- | --- | --- |
| AdsPower API not reachable | AdsPower closed or Local API off | Leave AdsPower open, enable Local API |
| `no unused comments.csv links` | Empty `link`, already assigned to someone else, or weekly cap | Paste unique URLs; leave `account` empty or assign the right id |
| Comment submit failed / not visible | Composer, filter, or rate limit | Read the log; check the post while logged in as that user |
| New account comments missing | Reddit filters new / low-karma | Age the account; keep browsing; check the thread, not Overview |
| Same first link grabbed by many Chromes | Old bug | Current build claims the row (`in progress`) under a lock |
| Browse comments blocked comments.csv | Old weekly cap mixed both | Sheet and general caps are separate now |
| Always the same 3 subs | Old fixed list | Current build rotates 2–3 new karma-friendly subs |
| Wayland hang | `xdotool --sync` | Do not add `--sync`; the script avoids it |
| Text in the `account` column | Unquoted commas in `text` | Quote the comment text in the CSV |
| Duplicate URL skipped | Same post pasted twice | One comment per unique link is intended |

---

## 19. Linux / AdsPower notes (this machine)

- Do **not** use `xdotool --sync` (it can hang on Wayland).
- Do not click AdsPower’s manager window during Reddit activity (it steals scroll).
- Prefer AdsPower’s chromedriver via `debuggerAddress`.
- After Open, wait for the debugger; do not double-start (AggregateError).
- Do not click the Reddit **Post** tab when commenting — only Comment / Reply.
- Do not rewrite `posts.csv` title/body in code.

---

## 20. Typical weekly plan

1. Keep accounts in `sheets/accounts.csv` (`enabled=yes`). Add extra communities in `sheets/subreddits.csv`.
2. Paste **unique** post URLs in `sheets/comments.csv` (`link` + `account`). Leave `text` empty for a general comment, or write your own. Optional `edit`.
3. Run when you want. Each account: Home 3–4 min then community hops, at most 2 comments and 1 post per 48 hours, 4 Chromes at a time.
4. Check `sheets/commented_links.csv` and the account summaries.
5. Fill `sheets/posts.csv` if you want a specific post. If it is empty and the 48h slot is free, a general community post may be written. Karma 1 is enough to try; Reddit may still filter it.

That is the whole system: AdsPower sessions, unique Home → sub patterns, a 48h comment/post budget, a sheet queue that archives itself, optional late edits, and RL that learns from what actually stayed on Reddit.
