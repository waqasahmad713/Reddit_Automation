# Reddit Automation

Opens AdsPower Chrome profiles that are already logged into Reddit, browses for a short sitting, and may leave a comment or a text post. It does not type a Reddit password. Each profile must already be signed in.

One run handles every `enabled=yes` account in `sheets/accounts.csv`. Up to **4** profiles run at once. The rest wait. Two accounts in the same batch do not sit in the same community at the same time.

## What one account does

Each sitting is **5–10 minutes**, mostly on Home, with short visits to communities. Unlock days are per account, not a shared day-4 / day-8 / day-14 ladder.

| Action | Limit |
| --- | --- |
| Comments | 2 per account per 48 hours |
| Text post | 1 per account per 48 hours, and only when karma is **10 or more** |
| Communities | 1–10 from `subreddits.csv`, plus a few random explores |

Comments go only on posts that read as **help, question, suggestion, or review**. Image, video, gallery, and GIF posts are skipped. If `comments.csv` has an unused link, that link is used first. If `posts.csv` has an unused row and karma is at least 10, that row is posted as written (title and body are not rewritten). Empty sheets fall back to a general comment or, when karma allows, one community post.

A small reinforcement-learning model (`reddit_joiner/rl.py`) picks comment tone and whether to comment or lurk. About 30% of those choices stay random. It does not choose which browser to open.

## Layout

```
sheets/           accounts, communities, comment queue, posts
reddit_joiner/    joiner, comment writer, RL, rules, storage
omniroute/        Docker compose for the local model router
data/             activity.db, RL model, logs (created at runtime, not in git)
docs/             longer notes (some timings there are older than this README)
run.sh            Linux start
run.ps1 run.bat   Windows start
```

## Before you run

1. Install [AdsPower](https://www.adspower.com/) and leave it open with the Local API on: `http://local.adspower.net:50325`.
2. Each profile is already logged into Reddit.
3. Python 3.10+.
4. Optional: Docker (OmniRoute) and [Ollama](https://ollama.com/) with `ollama pull mistral:7b`. Comments do not need either if `OPENAI_API_KEY` is set.

```bash
pip install -r requirements.txt
cp .env.example .env
```

Put keys only in `.env`. That file is gitignored.

| Key | Role |
| --- | --- |
| `OPENAI_API_KEY` | Comments use OpenAI first (`gpt-4o-mini` unless `OPENAI_MODEL` is set) |
| `OLLAMA_MODEL` | Local fallback, default `mistral:7b` |
| `GEMINI_API_KEY`, `HF_TOKEN`, `DEEPSEEK_API_KEY`, `OPENROUTER_API_KEY`, `GROQ_API_KEY` | Used only if OpenAI and the local model both fail |
| `REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET` | Optional read-only check of whether a comment is still live |
| `ADSPOWER_API` | Local API URL |

## Sheets

| File | What to put |
| --- | --- |
| `sheets/accounts.csv` | AdsPower user id. `enabled=yes` to run that profile |
| `sheets/subreddits.csv` | Communities to join and browse. `enabled=yes` |
| `sheets/comments.csv` | Optional post URLs. Empty `text` means the model writes the comment |
| `sheets/posts.csv` | Optional exact title and body. Not rewritten |
| `sheets/commented_links.csv` | Filled automatically after a comment lands |

`sheets/profiles.txt` is not used. Accounts come from `accounts.csv` only.

## Run

Linux:

```bash
./run.sh
```

Windows PowerShell, from this folder:

```powershell
py -3 -m reddit_joiner
```

Or, after `run.ps1` is in the folder: `.\run.ps1`. Command Prompt: `run.bat`.

Ctrl+C still saves `data/rl_model.pkl` and `data/activity.db`.

## Targeted warm-up

A separate run for accounts that should warm up inside chosen subreddits. It uses the same sitting as the general warm-up (Home stretches, 4 at a time, per-account unlock days, comment and post limits, comments.csv / posts.csv). The difference is where communities come from: only your targets and subreddits related to them.

| Warm-up | Linux | Windows | Reads |
| --- | --- | --- | --- |
| General | `./run.sh` | `run.bat` / `.\run.ps1` | `accounts.csv`, `subreddits.csv` |
| Targeted | `./run_targeted.sh` | `run_targeted.bat` / `.\run_targeted.ps1` | `targeted_accounts.csv`, `targeted_subreddits.csv` |

`./run.sh --targeted` and `py -3 -m reddit_joiner --targeted` are the same as the targeted scripts.

`sheets/targeted_accounts.csv`: one AdsPower id per row. `group` is optional; empty means the account uses every target.

```csv
user_id,enabled,name,notes,group
k1g5mjai,yes,,,fitness
k1g5mjc5,yes,,,crypto
```

`sheets/targeted_subreddits.csv`: the target subreddits, by group.

```csv
group,subreddit,enabled,notes
fitness,bodyweightfitness,yes,
fitness,running,yes,
crypto,CryptoCurrency,yes,
```

`sheets/targeted_related.csv` is filled by the bot. Explores read the related list on a target page, search communities for the group or target name, and reuse related subreddits found in earlier sittings. A new one is added only when it is not NSFW, quarantined, private, or under 1,000 members, up to 30 per group. Set `enabled=no` on a row to stop using it.

Targeted summaries are saved as `data/summaries/targeted_session_N.txt`. Comment and post limits are per account across both runs. Keep an account in one sheet only, and do not run both warm-ups at the same time: they would open the same AdsPower profile. The run logs a warning when an account is in both sheets.

## Comment order

1. OpenAI, when `OPENAI_API_KEY` is set
2. OmniRoute at `http://127.0.0.1:20128`, if Docker started it
3. Local Ollama `mistral:7b`
4. Hugging Face, DeepSeek, OpenRouter, Groq, Gemini

If every provider fails, a short local sentence from the post title is used. A comment is counted only when it is visible on the thread.

## Notes

- Keep at least a few hundred MB free. AdsPower copies the profile on start and fails with “No space left on device” when the disk is full.
- On Linux, do not use `xdotool --sync` (it can hang on Wayland). The script does not.
- Do not commit `.env`. It holds API keys.
