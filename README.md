# Reddit Automation

Opens AdsPower Chrome profiles that are already logged into Reddit, browses for a short sitting, and may leave a comment or a text post. It does not type a Reddit password. Each profile must already be signed in.

One run handles every `enabled=yes` account in `sheets/accounts.csv`. Up to **4** profiles run at once. The rest wait. Two accounts in the same batch do not sit in the same community at the same time.

## What one account does

Each sitting is **5–10 minutes**, mostly on Home, with short visits to communities.

| Action | Limit |
| --- | --- |
| Comments | 2 per account per 48 hours |
| Text post | 1 per account per 48 hours, and only when karma is **10 or more** |
| Communities | 1–3 from `subreddits.csv`, plus a few random explores |

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
