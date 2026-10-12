# Reddit warmup — before vs now

Updated 6 October 2026 (continuous warmup, same-day retries, report day folders, unbroken upvote streak, capacity planner, checker accounts, join fixes; earlier: News articles, following, feed sorts, Ask pool). PDF copy: `reports/warmup-redesign/warmup-before-vs-now-2026-10-06.pdf`.

In late September almost every account of the 23 Sep creation batch was shadowbanned. The analysis ([reddit-ban-analysis-2026-10.md](reddit-ban-analysis-2026-10.md)) showed Reddit treated them as one coordinated network: many accounts on the same threads, upvoting each other, commenting from day one, sharing IPs and acting on the same schedule. The warmup was rebuilt so each account behaves like a separate person and our accounts never meet on the same content.

- **96%** of the 23 Sep creation batch invisible from outside (399 of 417)
- **308 of 422** banned accounts had never posted a single comment
- **36 threads** had comments from 2+ of our accounts (up to 22 on one thread)
- **64 exit IPs** were shared by 2–4 of our accounts at the same time
- **769** comments posted by brand-new accounts in their first two days

## Running the profiles

**How profiles are opened**

- Before: Batches of 10 in list order. The next batch waited until all 10 had closed, so one slow profile held 9 slots idle.
- Now: 10 rolling slots: when one profile closes, the next opens. Random order every cycle. 20–90 s random gap between opens; a profile only opens when the server has free RAM and CPU. "Profile in use" errors are retried twice.
- Why: Faster cycles, no burst of 10 identical starts, no overloaded server.

**How often an account runs**

- Before: The scheduler restarts every 1–2 h and every account ran in every cycle, so with a small pool an account could get many sessions a day, at any hour.
- Now: Daily gate: one session a day, soon after the account's own active window opens; about 1 account in 10 gets a second, shorter session 3–7 h later; never more than 2. The daemon wakes when the next account is due (5–60 min), not on a fixed 1–2 h timer. A failed attempt is retried the same day (30 min, 1 h, 2 h, then every 3 h, up to 8 tries).
- Why: Real people open Reddit once or twice a day, not every 90 minutes; and no account loses its day to one bad proxy moment.

**Stop and resume**

- Before: Ctrl+C lost the run; no way to continue. No test mode.
- Now: Ctrl+C lets running sessions finish; --resume continues the same cycle. --dry-run does everything except vote, join, comment or post. A selftest checks Reddit selectors on one profile.
- Why: Safe testing before touching real accounts.

**Several VPSes**

- Before: Each VPS worked alone and knew nothing about the others.
- Now: One shared history (git branch warmup-history): which threads were opened, voted, commented, which subs joined. Each profile is owned by one VPS.
- Why: Two VPSes can never act on the same thread or open the same account.


## Account rhythm and identity

**Session length**

- Before: 4–6 minutes for everyone: about 1 minute of feed, then the comment search. Same 20-minute force-close for all.
- Now: Each account has its own average (6–16 min), shorter while new (×0.5–0.7 on days 1–3) and growing with age. No two sessions of an account within ±15 s; never on a whole minute. Force-close at planned + 5 min (max 25).
- Why: Identical session shapes across hundreds of accounts are an easy pattern to spot.

**Time of day**

- Before: Whenever the batch reached the account, around the clock, regardless of the IP's country.
- Now: 1–2 personal active windows in the account's local time (timezone from its proxy location), leaning towards Reddit's busy hours there: about 28% of sessions between 19:00 and 22:00 local (21% if uniform), fewer early mornings, the rest spread through the day. The window moves every day (±90 min, length ±20 %, plus 0–40 min delay), so no account has a fixed daily time. No rest days (6 Oct). A new account starts 0–3 days after creation.
- Why: Accounts are active when real users in their area are, without all piling into the same hour; more fresh posts to comment on.

**Persona**

- Before: One persona for every account: "SASSY", "always wants to take spotlight".
- Now: Each account has its own persona: region and spelling (US/UK), age range, 2–3 interests, home subs, voice. Each community has its own tone profile.
- Why: The old voice drew downvotes and reports; one voice for all linked the accounts.


## What a session does

**Activity mix**

- Before: Fixed shape: scroll feed, upvote, open text posts only, rare join, then comment. Media posts were skipped.
- Now: A random plan per session: scroll Home/Popular (Popular clicked in the left sidebar), open and read threads, watch videos and step through galleries, open user profiles, browse communities, search topics, change sort (Best / Hot / New / Top / Rising, weighted), read the inbox, read News articles in a new tab, follow a top commenter and revisit followed users. Each action has a 180 s watchdog; a timed-out action is cancelled.
- Why: Looks like browsing, not a script; many more varied clicks per account; stuck pages no longer waste the session.

**Upvotes**

- Before: 3–10 post upvotes and 3–6 comment upvotes every session from day 1. The main post was always upvoted, including threads where our own accounts had commented.
- Now: None on days 1–3; 1–3 a day on days 4–7; 2–6 post + 0–2 comment upvotes from day 8. Only after reading. Never on a thread any of our accounts already touched (checked in the shared history). From day 4 the streak never breaks: at least 1 upvote every day (read-only sessions keep 1, an end-of-session streak upvote, or one more session later that day).
- Why: New accounts upvoting each other is vote manipulation, the main reason for network-wide shadowbans.

**Commenting**

- Before: Every account from day 1. Subs picked from the default feed, so everyone went to the same few (AskUK, AskReddit, CasualUK), onto the newest post, often the same thread.
- Now: From day 8 only, at most 1 a day, in subs the account has joined that accept new accounts. Post 10–90 min old with 1–2 comments, never a thread our accounts touched. AI writes with the persona, community rules and the image; a judge must approve. A removal monitor checks it 2–72 h later and demotes subs that remove us. A comment on a News article's thread follows the same rules (and counts as the day's one comment); the AI gets an excerpt of the article and the judge rejects facts not in it.
- Why: Pile-ons on one thread were the strongest ban signal (22 accounts on one AskUK post).

**Posting**

- Before: None.
- Now: From day 14, one post every 5–9 days, only in subs verified for posting, where the account already has a surviving comment. Strict moderator-style AI judge; duplicate-title check.
- Why: Builds post karma the normal way once the account is established.

**Joining communities**

- Before: Rare random join from the feed. A config bug made "subscribe" fire every time it was picked.
- Now: Each account gets its own join calendar over its first 3–6 weeks: persona niche subs early, one local city/state sub, big karma subs spread out. Four join methods (search, direct, feed link, thread link). At most 3 of our accounts join the same sub per cycle; no two accounts share the same first 3 subs. Each account also gets its own 2–4 "Ask" communities (AskBrits for UK, AskEurope, AskCulinary, AskMen …) joined on a random day from day 3; strict Ask subs are browse-only, heavily moderated ones blocked.
- Why: Mass joins of the same subs on the same day linked accounts together.


## More random clicks (added 5 Oct)

**News articles**

- Before: Never left Reddit; no outside links were ever opened.
- Now: From day 2, up to 2 per session: click News in Reddit's left sidebar, scroll, pick a link post to an outside site (no politics/war/crime, no political subs, no shortener or adult domains, thread untouched by our accounts), click it, the article opens in a new browser tab, read it for a weighted time (25–60 s 45%, 60–120 s 40%, 120–160 s 15%, scaled by length) with scrolls and pauses, close the tab, back to Reddit. 75% then open the post's thread, read, maybe upvote (60%) or comment under the normal rules.
- Why: Real users click out to articles; time spent off-site and coming back is a strong human signal.

**Following people**

- Before: Never followed anyone.
- Now: From day 5, after reading a thread, a small personal chance (4–10%) to open the top commenter's profile, read it and follow: max 1 a day, a personal total of 12–20, never our own accounts, never a user already followed by 2 of our accounts (shared history).
- Why: A normal account slowly builds a list of people it likes; capped so our accounts never cluster on the same users.

**Follower window**

- Before: Did not exist.
- Now: A separate session slot (personal 30–45% of sessions, only once the account follows someone, never the first slot): open a followed user's profile (least recently visited first, via search or URL), read posts and comments, sometimes open one of their threads, rarely unfollow.
- Why: Gives sessions two different shapes: the general feed and "checking on people I follow".

**Feed sorts**

- Before: Default feed only; the sort button was rarely and often unsuccessfully used.
- Now: 60% of feed slots start by switching sort, weighted Best 35 / Hot 30 / New 15 / Top 12 / Rising 8; sorts also switch at random on community and News pages.
- Why: Different accounts see and click different posts.


## Checks and safety

**Account checks**

- Before: Login check, then one health/karma check per session (from inside the logged-in browser). A shadowban still looks live.
- Now: This Python joiner loads `brand-lookup-*.txt` into `account_outside_status` and skips BANNED / SUSPENDED IDs before AdsPower opens. An inside ban is stored the same way and cannot be overwritten by LIVE. The 19 checker browsers that produce that file are not in this package.
- Why: The inside check missed 73 shadowbanned accounts; the outside view is the reliable one.

**Stopping a ban wave**

- Before: Nothing: banned accounts kept running.
- Now: This repo has no circuit breaker. Dead IDs stay off via `enabled=no` and the outside-status skip. A cycle-wide halt on a ban spike is not implemented here.
- Why: Document what this joiner actually does so a missing breaker is not assumed.

**Moving accounts between lists**

- Before: Manual.
- Now: Automatic graduation to warmed-up at day 14+, karma 50+ and live from outside.
- Why: Less manual work, fewer mistakes.


## Network (proxies)

**Proxy setup**

- Before: Almost all accounts on UK Oxylabs through one gateway; IP changing every 10 minutes (sesstime-10), so it could change in the middle of a session. 64 exit IPs shared by 2–4 accounts. Browser timezone not matched to the IP.
- Now: One proxy per profile, never shared. 70% US (states weighted by population) / 30% UK (cities weighted by population), created in a fixed order (reports/proxy-plan/proxy-creation-sequence.txt). Same IP all day (sesstime-1440). Timezone, language and location from the IP. IP audit on the first session of each day. npm run proxy:plan checks every profile.
- Why: Shared and jumping IPs tie accounts together; a stable home-like IP per account does not.


## Reports

**Report content**

- Before: One loose text file per day; minutes only ("365m 50s"); upvotes, joins and scroll not recorded. "Completed" meant "finished without error".
- Now: One folder per report day (09:00–09:00 Pakistan time) holding a folder per cycle (report.txt + report.pdf), a day-so-far report after every cycle, a mini report every 4 h and the main report at 09:00. Day totals at the top (warmed, failed all day, new accounts, upvote streak, retries, hung browsers closed), a TOTAL row, capacity. The PDF carries everything in the text: per session time vs plan, scroll, clicks by type, upvotes, joins, comments, karma trend, check order; one table per outcome with the reason; IP and fingerprint flags; outside status; removals; events; AI cost. Hours and minutes. Older reports moved to reports/legacy-reports.
- Why: Clear daily picture of what each account really did, at any hour; the PDF costs no AI tokens.

## Code fixes in the old flow

- Hover setting never fired; subscribe always fired when picked; thread filters never applied (settings compared as the wrong type).
- Unused code paths removed or wired in; the list-order setting (cyclingStrategy) was ignored.
- Session statistics were thrown away after each session; now saved per account and per cycle.

## Fixes after the first test cycles (5 Oct)

- A timed-out action is now cancelled, so two actions never drive the page at once.
- Mouse paths are no longer curved twice (our Bézier path + the humanize layer), hovers only pick posts on screen, and a stalled hover / mouse drift is capped (30 s / 12 s).
- Search only types into the visible header search box.
- Join confirmation re-reads the button and reloads once, so a late join is recorded and never clicked again (a second click leaves). Replaced on 6 Oct by the about.json check below.
- AdsPower socket hang-up / reset errors re-queue the profile; "Reddit did not load" (proxy) retries once and is recorded as failed, not "not logged in".
- Sort menu: the real selectors (Sort by button, items are links) — the old ones never matched and the step hung until the watchdog; the feed-sort step now has a 60 s limit.

## Continuous warmup (added 6 Oct)

**Waiting between cycles**

- Before: After each cycle the daemon slept a random 1–2 h, so an account whose window opened during the sleep waited, and peak hours could be missed.
- Now: After each cycle every account's next due time is worked out and the daemon wakes for the earliest one (at least 5, at most 60 min). After a stop or crash, overdue accounts go first, the ones with the least local day left at the front. A check where nobody is due leaves no report folder.
- Why: More accounts warmed per day, at their own time, without a fixed rhythm.

**Failed accounts**

- Before: A proxy error, page that did not load or "not logged in" ended the account's day; "not logged in" blocked it for 12 h.
- Now: Retried later the same day: +30 min, +1 h, +2 h, then every 3 h, up to 8 tries. Each attempt and its cause is in the report; an account that never succeeds shows as "failed all day".
- Why: Most proxy and loading problems are temporary.

**Warmup day**

- Before: Calendar days since the first session, so a day that failed or never ran still moved the account up the ladder.
- Now: This joiner still uses cake-day age from Reddit `me.json` `created_utc` (`read_logged_in_account`). It is not completed sittings + 1.
- Why: The markdown used to describe a Node warmup service. The Python joiner has not implemented that day counter.

**Hung browsers**

- Before: A profile stuck on the AdsPower start page stayed open for hours, holding a slot.
- Now: 3 min without progress from start to "logged in" closes it and schedules a retry. A sweep every 2 min closes browsers the bot opened that nothing drives any more. Report line: hung browsers closed.
- Why: A slot is never lost to a frozen browser.

**Upvote streak**

- Before: Read-only sessions and rest days meant some day-4+ accounts had 0 upvotes on some days.
- Now: No rest days. From day 4, at least 1 upvote every day: read-only sessions keep 1, an end-of-session upvote if none happened, or one more short session later that day. The report shows the streak and lists any account that broke it.
- Why: Steady daily activity looks like a real user and builds karma.

**More accounts than the VPS can run**

- Before: Not planned: a busy hour just ran late, with no warning.
- Now: At every cycle start, accounts due today are compared with what the slots can run per hour. Overflow moves to a later quieter hour (never the same account two days running). If the whole day is short, second sessions go first, then extras, then shorter sessions, then comments and posts move to another day; one completed session with the streak upvote is never dropped. The report warns how many slots are missing.
- Why: Scale to 200+ accounts per VPS knowingly instead of silently falling behind.

**Checker accounts**

- Before: 10 checkers used in list order, all on one shared SOCKS5 proxy, and they only ever opened profile links.
- Now: This package does not run checker browsers. It consumes a `brand-lookup-*.txt` report (LIVE / BANNED / SUSPENDED) written elsewhere.
- Why: Skip decisions stay in the joiner; producing the lookup is a separate process.

**Joining communities**

- Before: The button was guessed from the page; 9 failed joins on 5 Oct (not confirmed, button not found, could not open).
- Now: Reddit's own about.json says whether the account is already a member and whether the sub can be joined. Private, quarantined or removed subs are blocked for every account and swapped for another. The page scrolls back to the header, the real inner button is clicked, and the join is confirmed by Reddit's data — never clicked twice. A failed join saves a screenshot and the header HTML.
- Why: Fewer wasted join attempts, and evidence if Reddit changes the page.

**Before every restart**

- Before: Code errors only showed after the service had restarted and crashed on real accounts.
- Now: `npm run warmup:check` checks everything offline (no browser, no account): undefined names, module loading, the scheduling rules and a full profile run against a fake AdsPower, including a hang. The log also warns when the code on disk is newer than the running service.
- Why: A mistake shows before it touches an account.

## What warmup cannot fix (account creation)

- About 455 accounts were created on one day (23 Sep), all UK, all on the same proxy provider, with one username style (injected misspellings) and bulk avatar changes. Warmup cannot change a creation date.
- Recommended: create 10–30 accounts a day spread over the hours, mix username styles and email domains, never two new accounts on the same location within an hour, and never create checker accounts together with a production batch.

## Test plan

| Step | Detail |
|---|---|
| First test: 100 profiles | New profiles with proxies at positions 1–100 of the creation sequence (70 US states / 30 UK cities, the first 10 on the pilot locations). Dry run on 3 profiles first, then live. New accounts start over 0–3 days, so the load builds up gradually. |
| Watch each day (PDF) | The 09:00 PKT main report: day totals (warmed vs warmable, failed all day, upvote streak, retries, hung browsers), capacity warning, outside status (banned/suspended), not logged in, incomplete sessions, IP/timezone flags, proxy data per session (Oxylabs dashboard). Mini reports every 4 h for the day so far. |
| Expected | Days 1–3 browse and join only (News articles from day 2); days 4–7 a few upvotes, first follows from day 5, Ask subs joined from day 3; first comment from day 8; first post from day 14; first graduations after day 14. |
| Check in the report | Clicks line shows news and followed-visit; sessions show "news read: domain Ns" and "followed: u/…"; the PDF has News and Followed in the extras row. |
| Go / no-go | Suggested: no account banned from outside in the first 7 days, at least 90% of sessions completed, no IP/timezone mismatches. |
| Scale | Then continue with positions 101–500 of the sequence, a batch at a time. |
