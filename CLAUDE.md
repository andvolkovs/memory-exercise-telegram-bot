# Mental Training Bot

Python Telegram bot for brain training exercises. Two exercises: Word Memorization (live) and Audio Visualization (feature-flagged, default off), architecture designed for easy expansion. Gamified: achievements, streaks, opt-in leaderboard. Offered as a training product for a Skool mental-training community.

**Full feature behaviour lives in `docs/FEATURES.md`** — read the relevant
section there before changing how a feature behaves. This file is the map,
the schema and the rules.

## Tech Stack

- **Python 3.12** (required — 3.14 breaks SQLAlchemy + python-telegram-bot)
- **python-telegram-bot 21.x** with async polling, `concurrent_updates(True)` (required — sequential mode blocks all users on any handler await)
- **SQLAlchemy 2.0** + aiosqlite (SQLite, async, WAL mode)
- **python-dotenv** for config

## Invariants

Rules that are deliberate and easy to undo by accident. Don't "fix" these
without asking.

- **New model column → also add it to `connection.py::_run_sqlite_migrations`** (`{table: {column: ddl}}`). `create_all` never alters existing tables. The streak columns once shipped without it and every pre-existing database crashed on every user query for months.
- **JSON columns: assign a new dict** (`user.preferences = {**old, **new}`). In-place mutation is silently not persisted (identity-based change detection).
- **`_IS_SCORED_TEST` (repositories) is duplicated in pandas in `dashboard.py`** — change one and the other silently disagrees.
- **`RANK_MIN_TESTS` in `bot/quiz_engine.py` must match the leaderboard query's `min_tests`.**
- **Never rename an achievement `code`** — unlocks are stored by code.
- **Anti-farm rules are intentional**: retry rounds = subset XP then 0 on 2nd+; first reverse ×0.5, `reverse_extra` = 0 XP; retry/reverse_extra/placement/review excluded from stats/leaderboard/PB/achievements; retry/reverse/review rounds never touch the hard streak; review XP = ¼ at 80%+, first review of a list per day only; replayed audio stories earn 0 XP; 80 audio XP/day cap.
- **Results screens carry no praise line and no progression-suggestion text** — removed on purpose; the ⬆️ Level up / ⚡ Speed run buttons do that job.
- **Users only ever see *training* time.** Reconstructed "time in bot" is admin-side only; the leaderboard stays on accuracy — time is context, never rank.
- **Passive audio listens don't count a streak** (audio quiz does).
- **Never back up SQLite with `cp`** — WAL mode makes a live copy inconsistent. Use `scripts/backup_db.sh`.
- **Never edit files on the VPS.** `scripts/deploy.sh` refuses to pull over a dirty tree, and `/admin version` flags one. Hand edits once left the server silently on old code for weeks.
- Scores live in the real `score`/`max_score` columns, never in `parameters` JSON — stats/leaderboard aggregate in SQL, not Python.
- Answer recording is serialized per user with an asyncio lock — required under `concurrent_updates`.
- Quiz flow assumes private chats (`chat_id == telegram user id` in results/streak paths).

## Project Structure

```
main.py                        # Entry point, logging, run_polling
config/settings.py             # Settings class, env vars, lru_cache singleton
bot/__init__.py                # create_application(), handler registration, lifecycle hooks
bot/handlers.py                # Router: callback_handler → CALLBACK_ROUTES, text_message_handler
bot/commands.py                # Slash commands + menu/lb/settings callbacks, main-menu keyboard
bot/quiz_engine.py             # Shared quiz engine: timers, grace window, results pipeline, retry/reverse
bot/word_memo.py               # Word Memorization flow + placement test
bot/state.py                   # Per-user context state, answer locks, bot-message cleanup
bot/recent_words.py            # Anti-repeat window of recent words
bot/admin.py                   # /admin commands
bot/redeem.py                  # Redemption codes: /redeem + /admin codes
bot/reminders.py               # Daily reminder + fresh-mind XP bonus
bot/sprint.py                  # Daily sprint challenge
bot/saved_lists.py             # Saved lists panel (/lists) + review from memory
bot/analytics.py               # Engaged training time + raw interaction log
bot/access.py                  # is_admin, admin_only, subscription tier gating
bot/features.py                # Runtime feature flags (DB-persisted, cached)
bot/audio_viz.py               # Audio Visualization flow
bot/menu.py                    # Telegram command menu sync
bot/version.py                 # Running-build info (git, cached at startup) for /admin version|changes
gamification/achievements.py   # Achievement defs + evaluation; register_achievements()
gamification/xp.py             # XP/level system: skills, curve, diminishing returns
gamification/audio_xp.py       # Audio XP + audio achievements
exercises/base.py              # BaseExercise ABC
exercises/registry.py          # Exercise registry
exercises/word_memorization.py # Word memorization logic
exercises/audio_visualization.py # Story library scan, file_id cache, keyboards
database/models.py             # Models + enums
database/repositories.py       # DB query layer — aggregates in SQL
database/connection.py         # Engine, session factory, SQLite migrations + backfill
data/                          # concrete_nouns.json (beginner), nouns/verbs/adjectives.json
data/audio/{1min,3min,5min}/   # Stories: .mp3 + optional .json sidecar; rescanned live
scripts/make_story.py          # Story text → mp3 (edge-tts) + sidecar; --batch auto-buckets
scripts/STORY_GUIDE.md         # Story + quiz writing rules — follow when writing stories
scripts/backup_db.sh           # SQLite online backup, cron'd daily on the VPS
scripts/deploy.sh              # VPS deploy: dirty-tree check → pull → restart → verify
tests/                         # pytest suite (pure logic, SQL rules, engine races); fakes.py stubs PTB
dashboard.py                   # Streamlit dashboard — reads a SNAPSHOT, never the live DB
requirements-dashboard.txt     # Dashboard-only deps, kept out of requirements.txt
docs/FEATURES.md               # Full behavioural spec for every feature
docs/ADMIN_GUIDE.md            # Admin cheat sheet: commands, XP tuning, VPS ops
docs/DASHBOARD.md              # Dashboard build/deploy guide
docs/ROADMAP.md                # Agreed feature backlog + build order
docs/SCALING.md                # SQLite → Postgres/Redis plan with triggers
```

Modules marked self-contained in their docstring (`redeem`, `reminders`,
`sprint`, `saved_lists`, `analytics`, `audio_xp`, audio viz) carry their own removal notes.

## Environment Variables

```
TELEGRAM_BOT_TOKEN=    # required
DATABASE_URL=          # default: sqlite+aiosqlite:///./mental_training.db
BOT_NAME=              # default: MentalTrainingBot
MAX_WORD_PAIRS=        # default: 100
DEFAULT_WORD_PAIRS=    # default: 10
ADMIN_TELEGRAM_IDS=    # comma-separated Telegram IDs allowed to use /admin
```

Loaded from `.env` via `config/settings.py`; singleton via `get_settings()`.

## Commands

`/start` `/help` `/stats` `/history` (last 10) `/level` (hidden when XP off)
`/achievements` `/leaderboard` `/exercises` `/settings` `/redeem` `/lists` — each maps
to `<name>_command` in `bot/commands.py`.

`/admin` (admin-only): overview | `users` | `time [days]` | `export` CSV |
`grant <id> <tier> [days]` | `codes <n> <tier> <days|lifetime>` | `codes list` |
`version` | `changes [n]` (running commit / recent commits, `bot/version.py`) |
toggles `xp|audio|audioquiz|audioxp|reminders|sprint|lists|analytics on|off`.

Callbacks route through `callback_handler` → `CALLBACK_ROUTES`, keyed by
prefix (`word_memo`, `audio_viz`, `lb`, `menu`, `settings`, `placement`,
`rem`, `lists`, plus legacy `recall` → "replaced" notice). In-test text goes through `text_message_handler`.

## Features (summary — detail in `docs/FEATURES.md`)

- **Word Memorization** — formats Pairs / Word List; Training or Test mode; 3 difficulties; 5–100 items; per-question timer + grace window; fuzzy matching (length-scaled typo allowance, real-word answers rejected); results at end only (+ compact toggle, leaderboard standing); Retry Mistakes, Reverse quiz, Placement test, Level-up / Speed-run buttons, personal bests, anti-repeat, daily streak, daily sprint challenge, saved lists (every test list kept by date, auto-save toggle, review from memory in `/lists`).
- **Audio Visualization** — narrated story .mp3 the user visualizes; passive by design, optional detail quiz as proxy score. Two flags, both default OFF (`/admin audio`, `/admin audioquiz`). Library rescanned live from `data/audio/`. Fully self-contained → clean removal.
- **Access codes** (`bot/redeem.py`) — one-time `MTB-XXXX-XXXX` codes set subscription tier; the Skool↔Telegram data link. Tiers not enforced anywhere yet.
- **Daily reminders** (`bot/reminders.py`) — per-user opt-in, hourly sweep, one-tap ping button with last-used settings, ×1.25 fresh-mind XP within 15 min.
- **Usage analytics** (`bot/analytics.py`) — engaged training seconds on the session row + a raw interaction stream (`activity_events`, never message text, purged after `ACTIVITY_RETENTION_DAYS` = 90 by a daily job). Also bumps `last_active_at` (hourly throttle). Feeds `/admin time` and the dashboard.
- **Gamification** — XP bars per skill (challenge rating, diminishing returns, accuracy gates), 21 achievements, opt-in leaderboard by average score, subscription helpers.

## Database Models

**User**: `telegram_id` (BigInteger), `username`, `first_name`, `last_name`, `language_code`, `subscription_tier` (FREE/BASIC/PREMIUM) + `subscription_expires_at`, `preferences` (JSON), `current_streak`, `longest_streak`, `last_trained_date`, `leaderboard_opt_in`, timestamps

**ExerciseSession**: `user_id`, `exercise_type`, `difficulty`, `parameters` (JSON: count, mode, format, speed; `items` + `saved` = the word list on fresh word-memo tests, plus review counters; `source_id` on review rounds), `score`, `max_score`, `completed`, `duration_s` (nullable), timestamps. Legacy JSON-score rows are backfilled at startup. Rows written only at completion, so `started_at` ≈ `completed_at` — duration comes from `duration_s`, not their difference. Composite index `(user_id, started_at)`.

**ActivityEvent**: `telegram_id`, `ts`, `kind`, `detail`; index `(telegram_id, ts)`.

**UserAchievement**: `user_id`, `code`, `unlocked_at`; unique `(user_id, code)`.

**UserSkill**: `user_id`, `skill`, `xp`, `level`, `hard_streak`; unique `(user_id, skill)`. One row per XP bar — future bars need no schema change.

**BotSetting**: key-value runtime flags (`xp_enabled`, `audio_viz_enabled`, `audio_viz_quiz_enabled`, `audio_xp_enabled`, `reminders_enabled`, `sprint_enabled`, `saved_lists_enabled`, `analytics_enabled`).

**RedemptionCode**: `code` (unique), `tier`, `duration_days` (NULL = lifetime), `redeemed_by` (Telegram ID, NULL = unredeemed), `redeemed_at`, `created_at`.

## Adding a New Exercise

1. Create `exercises/your_exercise.py` extending `BaseExercise`
2. Implement: `get_difficulty_keyboard()`, `get_parameter_keyboard()`, `generate()`, `get_intro_message()`
3. Set class attrs: `name`, `description`, `exercise_type`
4. Register in `exercises/registry.py`
5. Add `ExerciseType` enum value in `database/models.py`
6. Add a callback route: `CALLBACK_ROUTES["your_type"] = your_callback_handler` in `bot/handlers.py` (flow handler in its own `bot/your_exercise.py`, like `bot/word_memo.py` / `bot/audio_viz.py`)
7. To reuse the quiz engine (`bot/quiz_engine.py`: per-question timers, grace window, retry/reverse), set the same `test_*` state keys plus `test_exercise_type`, provide `format_test_prompt`, `get_skip_keyboard`, `format_test_results`, `get_results_keyboard` on the exercise, and add the registry key → enum mapping to `ENGINE_EXERCISE_ENUM`
8. Map it to an XP bar: add the ExerciseType value to `EXERCISE_SKILLS` in `gamification/xp.py` (new bar = new `SkillDef` in `SKILLS`)

## Admin & Ops

- `/admin` totals · `users` per-user progress · `export` CSV of scored sessions · `time [days]` engaged minutes
- Dashboard: `streamlit run dashboard.py` against a snapshot (`scp root@<VPS>:/root/backups/latest.db snapshot.db`). Charts export PNG. See `docs/DASHBOARD.md`
- Backups: `scripts/backup_db.sh`, cron'd 03:00 UTC daily, 14-day retention
- Tests: `pip install -r requirements-dev.txt` then `python -m pytest` (temp SQLite, no network, ~10 s)

## Deployment (VPS)

Ubuntu VPS (`root@<VPS_IP>` — real address kept out of this public repo; it's
in your `.env`/SSH config and your local deploy notes). App at
`/root/mental_training_bot/`; use `venv/bin/python3`, NOT system python3.

Deploy: `ssh root@<VPS_IP> bash /root/mental_training_bot/scripts/deploy.sh`
(refuses on a dirty tree, fast-forward pulls, restarts, prints the build line).

Systemd unit `/etc/systemd/system/mental_training_bot.service` —
`ExecStart=/root/mental_training_bot/venv/bin/python3 main.py`,
`Restart=always`, `RestartSec=5`.

```bash
systemctl status mental_training_bot
systemctl restart mental_training_bot
journalctl -u mental_training_bot -n 50 --no-pager
```

## Known Issues / Future Work

- **Feature backlog: `docs/ROADMAP.md`** — check it before proposing new features
- **SQLite → PostgreSQL + Redis** planned; switch to Alembic then. Plan: `docs/SCALING.md`
- Subscription tiers + gating helpers exist, but nothing is gated and there's no payment flow
- `ExerciseType` has placeholder entries (NUMBER_SEQUENCE, PATTERN_RECOGNITION, MENTAL_MATH) — not implemented
