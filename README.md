# Mental Training Bot 🧠

Telegram bot for brain training. Two exercises — Word Memorization (live) and
Audio Visualization (feature-flagged) — wrapped in a gamification layer of XP,
achievements, streaks and an opt-in leaderboard. Built as a training product
for a mental-training community.

## Features

### Word Memorization
- **Formats**: 🔗 Pairs (recall the partner word) | 📜 Word List (sequential chain — each word prompts the next)
- **Modes**: Training (study only) | Test (study → timed disappear → quiz)
- **Difficulty**: Beginner (everyday concrete nouns) | Intermediate (all nouns + verbs) | Advanced (+adjectives)
- **Counts**: 5, 10, 15, 20, 30, 50, 75, 100
- Per-question timer with auto-skip, plus a 2s grace window for answers typed just after a timeout
- Fuzzy matching (typo allowance scales with word length; swapped letters = 1 edit; a different real word is never accepted)
- Retry Mistakes, Reverse quiz (columns flipped), one-tap level-up to the next size
- Speed mode — normal (5s/pair) or fast (2.5s/pair) study time
- Placement test for new users — one 2-minute calibration round recommends a starting level
- Personal bests (per format), progressive difficulty suggestions at ≥90%
- Anti-repeat word selection (rolling 200-word window)
- Daily streak tracking, plus a daily sprint challenge (5 tests at 90%+ inside an hour)
- Saved lists — every test list kept by date (auto-save toggle); review any of them from memory, ¼ XP at 80%+ once a day per list

### Audio Visualization
- Narrated stories (1/3/5 min) the user listens to and visualizes; passive by design
- Optional multiple-choice detail quiz afterwards as a proxy score
- Post-listen focus check ("how many times did your mind wander?")
- Drop an `.mp3` (+ optional `.json` sidecar) into `data/audio/` and it's live — no restart
- Own XP bar and achievements; off by default behind a runtime feature flag

### Gamification
- XP and levels per skill bar, with challenge-rating scaling and anti-farm accuracy gates
- 21 achievements, including tiered sets
- Opt-in leaderboard ranked by average score (min 3 tests)
- One-time redemption codes for gating access to paid community members

### Analytics
- Engaged training time recorded per round (study + quiz + listening)
- Raw interaction stream for usage patterns, with an admin kill switch
- `streamlit run dashboard.py` — private dashboard over a database snapshot:
  time on task, commitment vs improvement, where members stall, engagement, per-member drilldown

### Architecture
- Modular exercise system — adding an exercise is a new module plus a registry entry
- Async throughout (python-telegram-bot + SQLAlchemy async), concurrent updates enabled
- SQLite via aiosqlite in WAL mode (PostgreSQL migration planned)
- Features ship behind DB-persisted runtime flags, toggleable without a redeploy

## Bot Commands

| Command | Description |
|---------|-------------|
| `/start` | Main menu |
| `/help` | Help message |
| `/stats` | Training stats + current streak |
| `/history` | Last 10 tests |
| `/level` | XP bars (hidden when XP is off) |
| `/achievements` | Unlocked achievements |
| `/leaderboard` | Opt-in leaderboard |
| `/exercises` | List available exercises |
| `/settings` | Preferences, daily reminder, placement retake |
| `/redeem` | Activate an access code |
| `/admin` | Admin-only: stats, users, training time, CSV export, feature flags |

## Project Structure

```
mental_training_bot/
├── main.py                        # Entry point
├── dashboard.py                   # Streamlit analytics dashboard (reads a snapshot)
├── config/settings.py             # Settings, env vars, lru_cache singleton
├── bot/
│   ├── __init__.py               # Bot setup, handler registration
│   ├── handlers.py               # Callback + text-message routing
│   ├── commands.py               # Slash commands + menu callbacks
│   ├── quiz_engine.py            # Shared quiz engine (timers, scoring, results)
│   ├── word_memo.py              # Word memorization flow
│   ├── audio_viz.py              # Audio visualization flow
│   ├── admin.py                  # Admin commands
│   ├── analytics.py              # Training-time + interaction logging
│   ├── features.py               # Runtime feature flags
│   ├── reminders.py              # Daily reminder + fresh-mind bonus
│   ├── sprint.py                 # Daily sprint challenge
│   ├── saved_lists.py            # Saved lists panel + review
│   └── redeem.py                 # Access codes
├── gamification/                  # XP, levels, achievements
├── exercises/                     # BaseExercise ABC, registry, exercise logic
├── database/                      # Models, repositories, connection + migrations
├── scripts/                       # Story generator, DB backup
├── docs/                          # Admin guide, roadmap, dashboard, scaling
└── data/                          # Word lists, audio stories
```

## Setup

### 1. Create a Telegram Bot

1. Open Telegram → `@BotFather` → `/newbot`
2. Copy the token

### 2. Configure Environment

```bash
cp .env.example .env
# Edit .env — add TELEGRAM_BOT_TOKEN
```

### 3. Install Dependencies

Requires **Python 3.12** (3.14 breaks SQLAlchemy + python-telegram-bot).

```bash
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 4. Run

```bash
python main.py
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `TELEGRAM_BOT_TOKEN` | required | Token from BotFather |
| `DATABASE_URL` | `sqlite+aiosqlite:///./mental_training.db` | DB connection string |
| `BOT_NAME` | `MentalTrainingBot` | Bot display name |
| `MAX_WORD_PAIRS` | `100` | Max pairs per session |
| `DEFAULT_WORD_PAIRS` | `10` | Default pair count |
| `ADMIN_TELEGRAM_IDS` | none | Comma-separated IDs allowed to use `/admin` |

## Adding a New Exercise

1. Create `exercises/your_exercise.py` extending `BaseExercise`
2. Implement: `get_difficulty_keyboard()`, `get_parameter_keyboard()`, `generate()`, `get_intro_message()`
3. Set class attrs: `name`, `description`, `exercise_type`
4. Register in `exercises/registry.py`
5. Add an `ExerciseType` enum value in `database/models.py`
6. Add a callback route in `bot/handlers.py`, with the flow in its own `bot/` module
7. To reuse the shared quiz engine, follow the checklist in `CLAUDE.md`

## Roadmap

Full backlog with build order in [`docs/ROADMAP.md`](docs/ROADMAP.md).

- [ ] Delayed recall / spaced repetition across both exercises
- [ ] "Story method" bridge exercise — teaches the linking technique explicitly
- [ ] Daily personal recommendations + shareable progress cards
- [ ] More exercise types (number sequences, pattern recognition, mental math)
- [ ] PostgreSQL + Redis migration
- [ ] Subscription enforcement and payment flow
- [x] Leaderboard, achievements, XP levels
- [x] Audio visualization exercise
- [x] Usage analytics + admin dashboard

## License

MIT
