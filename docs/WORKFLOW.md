# Workflow

How a sitting actually runs. Caps, sheets, and RL details: [DOCUMENT.md](DOCUMENT.md).

## Start

AdsPower open, Local API at `http://local.adspower.net:50325`. Each profile already logged into Reddit.

```bash
source ~/myenv/bin/activate
cd ~/Desktop/reddit
./run.sh
```

Same thing: `python3 -m reddit_joiner`.

`./run.sh` activates `~/myenv`, sets `PYTHONPATH`, and starts the joiner. The joiner loads `.env`, opens `data/activity.db`, loads the DQN (`data/rl_model.pkl`), scores comments older than 20 minutes, starts OmniRoute in Docker if needed, then opens enabled AdsPower profiles (up to 4 at a time).

## You prepare

| File | Purpose |
| --- | --- |
| `sheets/accounts.csv` | AdsPower ids with `enabled=yes` |
| `sheets/subreddits.csv` | Communities to join and browse |
| `sheets/comments.csv` | Optional post URLs (`link` + `account`) |
| `sheets/posts.csv` | Optional weekly text post (title/body never rewritten) |

If `comments.csv` / `posts.csv` have unused rows, those go first. If the sheets are empty, the sitting uses general comments and (if karma ≥ 1) one generated community post. Each account: **2 comments** and **1 post** per **48 hours**.

## Each account (about 34–46 minutes, unique pattern)

1. AdsPower Start → wait for proxy → attach Selenium to that Chrome.
2. Read username, karma, age from Reddit.
3. Roll a **new activity pattern** this account has not used before (persona, timings, listing order, community mix). Saved in `data/session_patterns.json`.
4. **3–4 minutes** of random activity on Reddit Home.
5. Open a **random** sheet community, browse randomly, then **back to Home** for a random stretch. Repeat: Home → sub → Home → sub.
6. In a community:
   - Open `/r/sub/about/rules` and `rules.json`.
   - Join (or already joined).
   - RL chooses `join:comment` or `join:lurk`.
   - Random scroll / read / occasional upvote.
   - General comment only if `comments.csv` has no unused link for the remaining 48h slots (top then new, or new then top).
7. After lurking: if `posts.csv` is for that community, submit that row. If the sheet is empty and the 48h post slot is free (karma ≥ 1), write one **general post** that fits that community. Flair, then **Post**.
8. Leftover sitting time stays on **Home**.
9. If `comments.csv` still has unused links and 48h comment room: open those URLs, type `text` or AI, count only if visible. Optional `edit` after 3–4 minutes.
10. AdsPower Stop. Print the summary. Save RL + DB.

## RL in this workflow

The DQN does **not** pick Chromes or scroll length. It only chooses:

- comment vs lurk after reading rules
- comment tone (friendly / expert / funny / neutral)
- skip on optional extra browse comments
- fallback community after a `posts.csv` subreddit already failed

**Immediate rewards:** rules read (+0.5), join ok/fail (+1/−1), comment visible/failed (+2/−1), tone vs rules (±0.2 / −0.4).

**Delayed rewards (next run):** live score, filtered (−3), removed (−8). Comments younger than 20 minutes wait. Unknown after 3 days = 0.

State includes karma, age, post mood, time, and rule features (count, strictness, flags). Model: `data/rl_model.pkl`. History: `activity.db`.

## Caps (per account)

| Action | Per 48 hours | Notes |
| --- | --- | --- |
| Comments (sheet + general together) | 2 | Sheets first; general only if the sheet is empty or a slot remains |
| Same URL | 1 across all accounts | `comments.csv` / `commented_links.csv` |
| Text post (sheet + general together) | 1 LIVE, karma ≥ 1 | `posts.csv` first; generated post if the sheet is empty |
| Communities | 2–3 new per sitting | last 4 runs skipped |

## After a run

Read the account summary in the log. Check `sheets/commented_links.csv` if you used the queue. Ctrl+C still saves the RL model and `activity.db`.
