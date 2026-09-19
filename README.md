# Betix Payment Verifier

AI-assisted payment verification pipeline:

```
You (Telegram) ──▶ input bot ──▶ case + evidence ──▶ GPT-5.6 Terra extraction
      ▲                                                     │
      │ notifications                                       ▼
      │                                     Playwright: illunise.in/admin/orders
      │                                     search registration → score candidates
      │                                                     │  Betex Pay Order ID
      │                                                     ▼
      └──── verified / escalated ◀── monitor ◀── Betix Pay support group ◀── post evidence
                                     (bot replies, reviewers)         follow-up #1, #2
```

Nothing is ever marked "payment successful" from a screenshot, statement, video or AI confidence alone. Only
authoritative Betix signals (the `betixpay_cs_bot` status messages and/or a clear confirmation from a member of the
Betix support group) can verify a case, according to `CONFIRMATION_MODE` (`either` = system **or** a group member,
the default; `strict` = both).

## What was built from the real data

The Betix group export (`ChatExport_B3126-Support`) and the earlier support export were used to model the protocol:

| Observed in export | Implemented as |
|---|---|
| Screenshot posted with the merchant order number `ILLUN-…` as caption | `BETIX_SCREENSHOT_CAPTION_TEMPLATE={betex_order_id}` |
| Then video, then statement PDF (sometimes `Password:- xxx` caption) | evidence order + password forwarding |
| `✅ STATUS: Successful / Payment confirmed` reply from the bot | `SYSTEM_SUCCESS` (regex, confidence 0.99) |
| `📌 STATUS: Still Pending`, `🛑 STATUS: Failed`, `Order not found`, `Already matched`, `OrderStatus: Paid \| CallbackStatus: Success` | PENDING / FAILED / NOT_FOUND / SUCCESS classifiers |
| Reviewers reply `checking`, `Success`, `Done ✔️` | human classifier, counted for any member of the configured Betix group |
| Reviewers query `/pi ILLUN-…` for status | `BETIX_STATUS_QUERY_TEMPLATE` sent with each follow-up |
| The login form at `https://illunise.in/admin/login` (`#username`, `#password`, `button.login-btn`) | defaults in `config/admin_selectors.yaml` |

## How Illunise identifies an order (verified live, 2026-09-10)

* `/admin/orders?search=<query>` is a GET search over **Order ID, name, email, mobile, UTR, amount**. A search by
  the customer's 10-digit mobile returns every order of that customer (verified exact: all rows share the mobile),
  100 rows per page with pagination. So the "registration number" of a case **is the customer's mobile**.
* The list has `ORDER ID | AMOUNT | STATUS | GATEWAY | LOCATION | DATE & TIME | UTR` — no mobile column. The
  **View page** (`/admin/orders/ILLUN-…`) adds `Name, Mobile, Email, Amount, UTR, Status, SMS Gateway, Created,
  Updated` and two collapsed JSON blocks from the gateway ("raw BetixPay response", "creation response") with
  `platOrderNo` (Betix's own id, `PI…`), `merchantOrderNo` (= the ILLUN id), `refNo` (= UTR), `status`.
* **The id posted to Betix is the plain Illunise order id `ILLUN-…`** (Betix knows it as MerchantOrderNo). The
  Betix `PI…` id is read from the View page and kept internally only, to correlate Betix bot replies.
* Customers pay a **padded amount** (₹13,999.35 for a ₹14,000.00 order); the padded value is not exposed by the
  panel, so `ORDER_AMOUNT_TOLERANCE=1.00` is required.
* The order is created 0–1 min **before** the payment in every historical case checked; the matcher scores
  `payment_time − order_created` against `ORDER_TIME_MIN/MAX_BEFORE_PAYMENT` (default 0–30 min, ±3 min skew).

Read-only replay: `PYTHONPATH=. python scripts/replay_historical.py --mobile … --extraction-json … --expected ILLUN-…`
(3/3 historical cases from the Betix export selected the correct order: scores 0.95, 0.95, 0.95).

## Who the case belongs to, and how Betix is threaded

* **Forwarded evidence keeps the original sender.** When you forward a customer's screenshot to the bot, the case
  is created for the *forwarded-from* user (`original_user_id`, `original_username`, first/last name, original
  chat/message ids, `evidence_forwarded=true`), never for the person forwarding. A plain message you type
  afterwards (the mobile number) joins that customer's open case; a forward from a different customer starts a
  new case; the same file forwarded twice is ignored (`file_unique_id`). Privacy-restricted forwards keep the
  display name only. `ALLOW_CUSTOMER_DIRECT_SUBMISSIONS=true` lets customers message the bot themselves.
* **What Betix sees.** The payment screenshot with the plain `ILLUN-…` id as its caption is the first message and
  the anchor: no mobile, amount, time, UTR, URL or Betix-side id ever appear. The bank statement (with
  `Password:- …` if protected) and the payment video follow **as replies to that screenshot**, governed by
  `BETIX_EXTRA_EVIDENCE_POLICY` = `always` (default) | `on_request` (only once Betix asks for more evidence) |
  `never`. Files are never sent standalone or twice. **Follow-up #1 and #2 are also replies to the screenshot**
  (`followup_1_message_id`, `followup_2_message_id`).
* **Intake requires four things** (`REQUIRE_ALL_EVIDENCE=true`, default): the payment screenshot, the customer's
  mobile number (any format: `+91 77339 31348`, `773-393-1348`… normalised to 10 digits), the bank statement PDF
  and the payment video. They may arrive in any order as separate messages and join the same case. Until all four
  are in, the bot lists what is pending and does **not** search Illunise. A password-protected statement is still
  a statement: the bot asks for the password (send it as `Password:- 1234` or just `1234`). A registration number
  is stored if one is sent but is never required.
* **File type is decided by extension + Telegram MIME only** (no AI): `.pdf`/`application/pdf` → bank statement,
  `.mp4 .mov .mkv .avi .webm .m4v .3gp`/`video/*` → payment video, `.jpg .jpeg .png .webp`/`image/*` → screenshot.
* **`/search` — order-id lookup without a case.** Send `/search` (or `/search 7733931348`), then the payment
  screenshot and the customer's mobile number (a registration number or UTR is accepted as the search text).
  The bot extracts amount/time/UTR from the screenshot, searches Illunise, runs the same deterministic matcher
  and replies with the `ILLUN-…` order id (or the ambiguous / closest candidates). Read-only: no case, nothing
  posted to Betix, no follow-ups. `/cancel` aborts; a search session expires after 30 minutes.
* **A batch of forwards is safe.** Messages from one chat are handled one at a time and a case-id collision is
  retried, so forwarding four messages at once never loses one.
* **Confirmation attribution.** On VERIFIED the case stores `confirmation_type` (`system_bot` | `group_member` |
  `manual`), `confirmation_message_id`, `confirmation_user_id`, `confirmation_username`, `confirmation_at`, and all
  pending follow-ups are cancelled.
* **Solved notification** (HTML):
  ```
  ✅ PAYMENT SOLVED
  👤 User: @customer_username          ← clickable; without a username: tg://user?id=… link + "User ID: … (no username)"
  🧾 Order: ILLUN-178903327221195
  📱 Mobile: 98…
  💰 Amount: ₹1,485.00
  🕒 Payment: 15:10 (10 Sep)
  ✅ Betix: Confirmed at 15:31
  👨‍💻 Confirmed by: @betix_staff / Betix System
  ```

## Layout

```
app/
  main.py                 FastAPI (health + /cases/{id} timeline) + Telegram polling
  config.py               all settings from .env (pydantic-settings)
  telegram/  input_bot.py   evidence intake, case commands (/status /cases /summary /search /cancel)
             betix_poster.py  posts screenshot → details → statement → video, follow-ups (bot or user mode)
             betix_monitor.py group monitoring (aiogram or Telethon), correlation + classification
             confirmation.py  regex classifiers built from the export, group-membership authority, strict/monitor rule
             notifications.py idempotent admin notifications
             user_client.py   optional Telethon user session
  ai/        analyzer.py     GPT-5.6 Terra via OpenAI Responses API, JSON-schema structured output
             prompts.py      extraction / classification prompts and schemas
             extractor.py    deterministic regex extraction, Extraction(value, confidence, source)
  admin/     browser.py      Playwright + persisted storage state + selector config
             login.py        auto login; OTP/captcha → ManualAuthRequired (never bypassed)
             orders.py       /admin/orders search, generic table → Candidate
             matcher.py      weighted deterministic scoring, threshold + ambiguity gap
  cases/     correlation.py  message → case grouping; Betix message → case linking
             manager.py      orchestration: analyze → search → match → post → verify / escalate
             state_machine.py validated transitions, full status history
  evidence/  manager.py files.py ocr.py   download, classify, ffmpeg keyframes, retention
  followups/ service.py scheduler.py       DB-persisted follow-ups (#1, #2, escalation)
  workers/   tasks.py queue.py             arq worker, cron sweeper, startup recovery
  db/        models.py repository.py session.py migrations/
tests/                    39 tests (SQLite + fakes), incl. end-to-end flow and confirmation authority
config/admin_selectors.yaml
scripts/create_telegram_session.py
```

## Setup

### 1. Prerequisites

* Python 3.11+ (developed on 3.14), PostgreSQL 14+, Redis 6+
* `ffmpeg` — required for payment-video keyframes (`VIDEO_KEYFRAMES_ENABLED=true`). The Docker image installs it
  with apt. On a machine without a system ffmpeg, `pip install -r requirements.txt` brings `imageio-ffmpeg`, a
  static build the app falls back to automatically (`python -c "from app.evidence.ocr import ffmpeg_path; print(ffmpeg_path())"`).
* A Telegram bot from @BotFather
* An OpenAI API key with access to `gpt-5.6-terra`

### 2. Install

```bash
python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt && playwright install chromium
```

### 3. Configure

```bash
cp .env.example .env
```

Fill in `.env`. The important ones:

| Variable | Notes |
|---|---|
| `OPENAI_API_KEY`, `OPENAI_MODEL=gpt-5.6-terra` | |
| `DATABASE_URL`, `REDIS_URL` | Postgres + Redis. `INLINE_JOBS=true` runs jobs in-process without Redis (dev only). |
| `TELEGRAM_BOT_TOKEN` | the input/notification bot |
| `ADMIN_TELEGRAM_USER_IDS` | who may submit cases (your numeric Telegram user id; get it from @userinfobot) |
| `ADMIN_NOTIFY_CHAT_ID` | where confirmations/alerts go — one or more chat ids, comma separated |
| `BETIX_GROUP_CHAT_ID` | numeric id of the Betix support group (`-100…`). Add the bot to the group. |
| `BETIX_POST_MODE` / `BETIX_MONITOR_MODE` | `bot` (default) or `user` (Telethon). See below. |
| `AUTHORIZED_BETIX_REVIEWER_IDS` / `_USERNAMES` | leave EMPTY: any member of the Betix group may confirm. Non-empty only if you want to restrict it. |
| `BETIX_VERIFY_MEMBERSHIP` | `true`: confirm the sender's group membership via the Bot API before counting a human confirmation |
| `OUR_TELEGRAM_IDS` / `_USERNAMES` | your own extra accounts (the input bot is detected automatically); never counted as a confirmer |
| `BETIX_SYSTEM_BOT_USERNAMES` | `betixpay_cs_bot` |
| `CONFIRMATION_MODE` | `either` (default: system bot OR any group member) or `strict` (both) |
| `ILLUNISE_ADMIN_USERNAME` / `PASSWORD` | admin panel credentials, never logged |
| `FOLLOWUP_SCHEDULE_MINUTES`, `ESCALATION_DELAY_MINUTES` | 120,480,1440,2880 / 480 |
| `ORDER_MATCH_THRESHOLD`, `ORDER_MATCH_AMBIGUITY_GAP`, `PAYMENT_TIME_WINDOW_MINUTES` | 0.90 / 0.10 / 15 |

**Two Telegram credential sets.** `TELEGRAM_BOT_TOKEN` is the **Bot API** credential and is always required.
`TELEGRAM_API_ID`, `TELEGRAM_API_HASH` and `TELEGRAM_SESSION` are the **user/client API** (Telethon) credentials
for a Telegram user account; they are read only when `BETIX_POST_MODE=user` or `BETIX_MONITOR_MODE=user`, and
are otherwise ignored. Keep them configured so the switch is a one-line change if the Bot API turns out to be
limited in the Betix group.

**Bot in the Betix group.** For the bot to *read* replies in the group it must either be a group admin or have
privacy mode disabled in @BotFather (`/setprivacy` → Disable). If the group does not allow bots or you need to read
history, switch `BETIX_MONITOR_MODE=user` (and optionally `BETIX_POST_MODE=user`): create API credentials at
my.telegram.org, then

```bash
python scripts/create_telegram_session.py
```

and put the printed string into `TELEGRAM_SESSION` together with `TELEGRAM_API_ID` / `TELEGRAM_API_HASH`.

### 4. Database

```bash
alembic upgrade head
```

### 5. Admin panel selectors (one-time check)

The login selectors are taken from the live login page. The orders table selectors and column names in
`config/admin_selectors.yaml` are sensible defaults that **must be verified once** against your `/admin/orders` page:

```bash
python -m app.admin.login --check
```

```bash
python -m app.admin.orders REG123456
```

The second command prints every candidate row it parsed (including `raw` cells with the real column headers). Edit the
`columns:` aliases in the YAML until `betex_order_id`, `registration_number`, `amount` and `created_at`/`paid_at`
come out populated. Failed parses save a screenshot + HTML under `data/browser/debug/`.

If the panel ever asks for OTP / captcha, the system will not try to bypass it; it escalates and asks you to run

```bash
python -m app.admin.login --manual
```

which opens a visible browser, lets you log in, and saves the session for the workers.

### 6. Run

Terminal 1 (API + Telegram bot):

```bash
python -m app.main
```

Terminal 2 (worker: processing, posting, monitoring jobs, follow-up sweeper, recovery on start):

```bash
arq app.workers.tasks.WorkerSettings
```

Or with Docker:

```bash
docker compose up --build
```

### 7. Use

Send the bot, in any order and as separate messages:

```
[payment screenshot]
7733931348             (the customer's mobile number, any format)
[bank statement PDF]   (caption "Password:- 1234" if it is protected, or send the password afterwards)
[payment video]
```

The bot replies with the case id and what is still pending. Once all four are present it waits
`COLLECTION_SECONDS` (5 s) from the first message before creating the case at all, then creates ONE case from everything received and checks that all four items are in (otherwise it says what is missing and waits), then: extracts → logs in → searches → scores every candidate →
posts to Betix → monitors → follows up → notifies you. Anything uncertain (ambiguous match, no match, login problem,
layout change, Betix "Failed", no answer after 2 follow-ups) produces a `⚠️ MANUAL REVIEW REQUIRED` message and stops.

Admin commands: `/status`, `/cases`, `/retry CASE-…`, `/select CASE-… ILLUN-…` (pick a candidate for an ambiguous
match), `/verify CASE-…`, `/fail CASE-… reason`, `/new`, `/cancel`.

Full timeline of any case: `GET http://localhost:8080/cases/CASE-20260910-000001` (status history, messages,
evidence, candidates with per-signal scores, Betix messages with classification/correlation, verification events,
follow-ups, notifications, audit log).

## Tests

```bash
python -m pytest -q
```

## Design notes / guarantees

* **Who can confirm.** The Betix *group* is the authority, not a list of names. A clear confirmation
  ("success", "confirmed", "done"…) counts as a human confirmation when it comes from a member of the
  configured Betix group **and** is tied to the case (reply to our post, the Betex order id, registration,
  UTR, or a single recent post). The Betix system bot (`betixpay_cs_bot`) is tracked separately; our own
  bot/account and anyone outside the group are ignored. `either` (default) = system bot OR a group member
  verifies the case; `strict` = both are required.
* **No guessing.** Every extracted field is `{value, confidence, source}`; missing = `null`. Amount, registration,
  UTR, UPI are compared deterministically; the LLM never chooses the order. The Betex Pay order id comes only from the
  matched admin row.
* **Scoring, not first result.** Every candidate gets a weighted score over the signals that could be compared; a
  disagreeing registration/amount/UTR caps the score; one signal alone can never pass the threshold; close scores →
  `ORDER_MATCH_AMBIGUOUS` and a `/select` prompt.
* **Idempotency.** Unique constraints on Telegram (chat, message) for input messages, evidence and Betix messages;
  one row per (case, follow-up number); one notification per (case, kind); posting checks what was already sent.
* **Restart safety.** Follow-ups live in the DB (`followups.due_at`), not in memory; the worker's startup
  `recover()` re-queues interrupted cases, re-schedules missing follow-ups and (user mode) back-fills group history.
* **Never follow up after confirmation.** `verify_case` cancels all scheduled rows first; the sweeper re-checks the
  case status before every send.
* **Secrets** only from the environment; the structured logger redacts token/password/session-like values.

## Withdrawals

A withdrawal case needs only two things from the operator, in any order: the **withdrawal id** as Betix shows
it (`WD-84425-67115`) and the **bank statement** (PDF, `.uu` accepted; a password can follow). No screenshot,
mobile number or video is asked for, nothing is read by the model and Illunise is not searched.

The Betix group receives `BXWD-84425-67115` as a plain message (the case's anchor) and the statement as a reply
to it. Confirmation, reminders and `/status` / `/add BXWD-84425-67115` work exactly as for a payment; one
withdrawal id is posted once.

## Running on the Mac (launchd)

Everything lives in this folder. The bot and its own PostgreSQL run as user launch agents (start at login,
restarted if they stop) from the plists in `launchd/`; `~/Library/LaunchAgents/` only holds symlinks to them.

| Agent (`launchd/`) | What it runs | Log |
|---|---|---|
| `com.betix.verifier.postgres` | `pg/bin/postgres -D data/pg -p 5433 -k data/pg` (PostgreSQL 18, bundled in `pg/`) | `logs/postgres.log` |
| `com.betix.verifier.bot` | `.venv/bin/python -m app.main` | `logs/bot-launchd.log` |

```bash
./scripts/run.sh    # link/load/nudge both agents and report status (safe any time)
./scripts/stop.sh   # stop both until the next run.sh or login
```

`DATABASE_URL` in `.env` points at port 5433. Backups / moves: `backups/*.json` dumps can be loaded into an
empty, migrated database with `PYTHONPATH=. .venv/bin/python scripts/restore_backup.py backups/<file>.json`.

Gotcha: macOS privacy protection stops launchd from running a *shell script* inside `~/Documents`
(`/bin/zsh: can't open input file`), so the agents call the binaries directly.

## Scaling (how many cases at once)

The workflow is unchanged; the runtime is built so many users, cases, files and groups can be in flight together
and one slow case never holds up another:

| Concern | What happens now | Setting |
|---|---|---|
| A slow case blocking others | `process_case` runs as short transactions: the case row lock and the DB connection are released while files are read and while Illunise is searched; a newer message makes the stale run stop (`_relock`) | `DB_POOL_SIZE` 20 / `DB_MAX_OVERFLOW` 20 |
| Illunise browser | one Chromium per process, `ADMIN_BROWSER_PAGES` logged-in tabs shared by all searches; extra searches wait their turn; a broken tab is replaced | `ADMIN_BROWSER_PAGES` 3 |
| Customers with hundreds of orders | the Illunise search passes the payment DAY to the panel (`from_date`/`to_date`), keeps only orders created within `ORDER_SEARCH_WINDOW_MINUTES` before the payment, reads their View pages closest-first and stops at a clear match (same mobile, same amount, created just before); rows outside the requested dates are dropped even if the panel returns them | `ORDER_SEARCH_WINDOW_MINUTES` 30, `ORDER_SEARCH_KEEP_CLOSEST` 10 |
| Telegram limits | every send/edit/delete goes through `app/telegram/throttle.py`: spaced per chat, flood waits slept through and retried | `TG_GROUP_SEND_INTERVAL_SECONDS` 3, `TG_PRIVATE_SEND_INTERVAL_SECONDS` 0.4, `TG_SENDS_PER_SECOND` 25 |
| AI reads | at most `AI_MAX_CONCURRENCY` model calls in flight; the SDK backs off on 429/5xx; a file already read is never sent again (retries, restarts, late files) | `AI_MAX_CONCURRENCY` 6, `OPENAI_MAX_RETRIES` 4 |
| Background jobs | `app/workers/runner.py`: `INLINE_JOB_WORKERS` jobs at once, ordered per case (two jobs for one case never overlap), deferred jobs on timers, duplicate job ids skipped | `INLINE_JOB_WORKERS` 8 |
| Several Betix groups | `BETIX_GROUP_CHAT_ID` may list groups; all are monitored; a case is answered in the group it was posted to; new cases go to the first group (`BetixPoster.choose_group` is the routing hook) | `BETIX_GROUP_CHAT_ID` |
| Seeing the load | `GET /health` returns job queue depth, browser tab use, AI reads in flight, Telegram flood waits and DB pool use | |

Redis + arq workers (`INLINE_JOBS=false`) remain available for a multi-process setup.
