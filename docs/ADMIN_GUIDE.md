# Admin Cheat Sheet

Personal reference for running the Mental Training Bot. Everything you can do
as admin, in one place.

---

## Your admin commands (in Telegram)

Your Telegram ID must be in `ADMIN_TELEGRAM_IDS` in the `.env` on the VPS.
Non-admins get silence — the commands don't exist for them.

| Command | What it does |
|---|---|
| `/admin` | Overview: users, actives, tests, avg score, XP system status |
| `/admin users` | Per-user progress: tests, avg %, best %, streak, last active (top 30 by activity) |
| `/admin time [days]` | Engaged training minutes per user (default last 7 days) |
| `/admin export` | Sends you a CSV of every scored test — opens in Excel/Google Sheets |
| `/admin version` | Which commit is actually running, uptime, and a ⚠️ if files were edited on the server (a pull will refuse until they're stashed) |
| `/admin changes [n]` | Last n commit subjects of the running build (default 8, max 20) — a quick "what changed lately" |
| `/admin grant <telegram_id> <free\|basic\|premium> [days]` | Set a user's subscription tier; omit days = no expiry |
| `/admin xp on` / `/admin xp off` | Turn the whole XP/level system on/off (see below) |
| `/admin audio on` / `/admin audio off` | Show/hide the Audio Visualization exercise for everyone (default: off) |
| `/admin audioquiz on` / `/admin audioquiz off` | Offer the optional detail quiz after audio stories (default: off) |
| `/admin recall on` / `/admin recall off` | Delayed recall button on /start and in reminder pings (default: on) |
| `/admin analytics on` / `/admin analytics off` | Raw interaction logging (default: on) — training minutes are recorded either way |

Get someone's Telegram ID: it's in `/admin users` next to their name, or they
can message @userinfobot.

## XP system kill switch

`/admin xp off`:
- No XP calculated or stored for any test
- XP lines vanish from test results, `/stats`, `/help`
- `/level` replies "not available right now"
- **Existing XP is kept** — nothing is deleted

`/admin xp on` brings it all back instantly. No restart needed, survives
restarts (stored in DB `bot_settings` table).

## How XP works (for tuning)

All knobs in `gamification/xp.py`, top of file:

| Constant | Current | Meaning |
|---|---|---|
| `BASE_XP_PER_PAIR` | 4.0 | Raw XP per word pair |
| `DIFFICULTY_MULT` | 1.0 / 1.25 / 1.5 | beginner / intermediate / advanced |
| `SPEED_MULT_FAST` | 1.3 | Speed mode bonus |
| `ACCURACY_EXPONENT` | 2.0 | Higher = accuracy matters more |
| `PERFECT_BONUS` | 1.2 | Extra 20% for a 100% test |
| `MIN_XP_SCORE_PCT` | 50 | Below this score a test earns 0 XP (anti-farm) |
| `HARD_STREAK_MIN_PCT` | 70 | Hard streak needs this score, not just a hard attempt |
| `EFFICIENCY_FLOOR` | 0.15 | Outgrown exercises never drop below 15% XP |
| `HARD_STREAK_BONUS_PER` | 0.10 | +10% per consecutive hard test |
| `HARD_STREAK_BONUS_CAP` | 0.50 | Streak bonus max +50% |

**Diminishing returns**: each level has an "expected challenge"
(`expected_challenge()` = 5 + 2.5/level). Tests below it earn proportionally
less. Tests at/above it are "hard" — full XP + they build the streak bonus.
So players must raise pairs / difficulty / speed to keep leveling.
Only fresh tests move the hard streak: retry and reverse rounds leave it
untouched (and get no streak bonus) — a small retry subset would otherwise
always reset it, and a reverse would count one set twice.

**Level curve**: `xp_for_next_level()` = 80 + 45·(level−1)^1.3.
Reference (90% score): level 2 after ~2 easy tests; a 30-pair advanced test
gives 154 XP; a 100-pair advanced speed test gives 666 XP.

**Repeat-round throttle** (anti-farm — one memorized set pays out once):
- Fresh test: full XP.
- First reverse quiz after a study phase: ×0.5 XP, still counts as a real test.
- Every further reverse on the same set: 0 XP, saved as `reverse_extra`,
  excluded from stats/leaderboard/PB/achievements. Results show a nudge to
  start a fresh test instead.
- First retry-mistakes round: XP for the retried questions only. Second and
  later retries: 0 XP.
The counters reset whenever a new study phase starts (fresh test, level-up
button, etc.), so normal play is unaffected.

**Round types**: every quiz round is saved with a `mode` — `test`, `reverse`,
`reverse_extra` or `retry`. Fresh tests and first reverses count as real tests
everywhere. Retry and `reverse_extra` rounds are **excluded** from stats,
leaderboard, personal bests and achievements (they're repeat practice), but
still appear in `/admin export` with their mode so you can see them in the CSV.

After editing constants: `systemctl restart mental_training_bot`. Existing
XP totals stay; only future gains change.

**Adding a future skill bar**: add a `SkillDef` to `SKILLS` and map the new
exercise in `EXERCISE_SKILLS` (both in `gamification/xp.py`). Each user gets
the new bar automatically on first XP gain.

## Audio XP (visualization bar)

Audio sessions feed a separate **👁 Visualization** bar — never the mnemonics
bar, so audio can't shortcut word-memo levels. Deliberately stricter economy
(audio is the easier exercise). All knobs in `gamification/audio_xp.py`:

| Constant | Default | Meaning |
|----------|---------|---------|
| `LISTEN_XP` | 5 / 12 / 20 | Fixed XP per passive listen (1min / 3min / 5min) |
| `QUIZ_BASE_XP` | 15 / 30 / 45 | Detail-quiz XP before accuracy scaling |
| `QUIZ_MIN_ACCURACY` | 0.5 | Below 50% the quiz pays 0 |
| `QUIZ_ACCURACY_EXPONENT` | 2.0 | Accuracy² scaling, like word memo |
| `QUIZ_PERFECT_BONUS` | 1.2 | ×1.2 on 100% |
| `DAILY_XP_CAP` | 80 | Hard cap per UTC day (~2–3 honest sessions) |

Anti-farm rules (not tunable, by design):
- **Replays pay nothing** — a story already in the user's heard list earns
  0 XP forever. The library is finite; this kills the main farm vector.
- Daily cap is a backstop on top, tracked in `preferences["audio_xp_day"]`.
- No hard-streak or challenge-rating mechanics — audio has no difficulty axis.

Audio achievements (First Listen, Story Collector, Perfect Recall) live in
the same file and are checked after every audio session, even with XP off.

Kill switch: `/admin audioxp on|off` (default ON — moot while the audio
exercise itself is off). The global `/admin xp off` also stops audio XP.
Removing the whole feature: removal notes in the `gamification/audio_xp.py`
docstring.

## Audio Visualization exercise

User listens to a narrated story and visualizes it. Passive by design; an
optional multiple-choice detail quiz gives a proxy score.

**Both flags default OFF** — turn on with `/admin audio on` when content is
ready, `/admin audioquiz on` to experiment with the quiz. No restart needed,
survives restarts (DB `bot_settings`). Turning audio off hides the exercise
everywhere; old buttons answer "paused". All data is kept.

**Adding stories — the batch/queue workflow** (from the laptop):

1. Ask an AI (Claude etc.) for story texts + quiz questions, and **give it
   `scripts/STORY_GUIDE.md`** — it keeps stories densely visual (concrete
   objects, textures, colors — the point of the exercise) and sets the
   variety rules (POV, settings, fantastical stories welcome, quiz question
   templates) that keep the library from getting repetitive and the quiz
   from being gameable.
   Word targets at the default voice/pace (**~175 spoken words per minute**,
   measured): `1min` ≈ 175 words · `3min` ≈ 525 · `5min` ≈ 875.
   **Max 3 quiz questions per story** — the bot asks the first 3 even if a
   sidecar ships more (`MAX_QUIZ_QUESTIONS` in
   `exercises/audio_visualization.py`).
2. Save each story as `scripts/queue/<name>.txt`, its quiz (optional) as
   `scripts/queue/<name>.questions.json`
   (`{"questions": [{"q": "...", "options": ["A","B","C","D"], "answer": 0}]}`).
3. Render the whole queue in one command:

```bash
pip install edge-tts          # once
python scripts/make_story.py --batch scripts/queue
```

Each story lands in the right `data/audio/<bucket>/` folder automatically
(bucket picked from word count), processed sources move to
`scripts/queue/done/`. Single story: `python scripts/make_story.py story.txt`
(optional `--bucket`, `--title`, `--questions`, `--voice`,
`--rate` — speech pace, default `-10%` of edge-tts default: measured
comfortable for visualization).

Then commit + push + `git pull` on the VPS — or `scp` the new files straight
into `/root/mental_training_bot/data/audio/<bucket>/`. **No restart needed**:
the bot rescans the folders on every session. Sample story + questions:
`scripts/samples/`.

Any hand-made .mp3 works too — just drop it in a bucket folder. Without a
sidecar .json the title comes from the filename and no quiz is offered.

**Bookkeeping**: passive listens save as mode `audio_listen` (no score, **no
streak** — passive work), quiz runs as `audio_quiz` (scored, streak counts).
Both excluded from word-memo stats/leaderboard/PB/achievements; both visible
in `/admin export`. Per-user anti-repeat: last 100 heard stories in
preferences. Telegram file_ids cached in `data/audio/file_ids.json` (bot
writes it; gitignored) so each file uploads only once.

**Removing the exercise entirely**: delete `exercises/audio_visualization.py`,
`bot/audio_viz.py`, `data/audio/`; remove the one registry line, the one
`CALLBACK_ROUTES` entry + import in `bot/handlers.py`, the `AUDIO_VISUALIZATION`
enum value, the two flag keys in `bot/features.py`, the two `/admin` subcommands,
and `"audio_quiz"` from `_IS_SCORED_TEST` in `database/repositories.py`.

## Achievements

Definitions: `gamification/achievements.py`. Add one = append to the
`ACHIEVEMENTS` list (code, emoji, name, description, check lambda) + restart.
No DB change needed. Never reuse/rename a `code` — unlocks are stored by code
(display names/descriptions can change freely).

Tiered achievements (I/II/III by pair count, 10/30/50+): Flawless, Speedster,
Advanced Ace. 18 total. Achievements are never checked on retry rounds.

## Usage analytics (time on task)

Two numbers, and they are not the same thing:

**Engaged training time** — `duration_s` on every session row: study phase +
quiz for word memo, listen (+ quiz) for audio. Measured on a monotonic clock,
so it survives clock changes and can't be inflated by leaving the chat open.
This is the only number to quote to a member ("47 min trained this week").

- `/admin time` — last 7 days per user; `/admin time 30` for a month.
- Also a `duration_s` column in `/admin export`.
- **Blind spots, by design:** training-mode rounds (no completion event to
  close them), rounds interrupted by a bot restart, and anything longer than
  its cap (1h for word memo, ~2× story length + 5 min for audio) record NULL.
  Totals are always a floor, never inflated.

**Raw interaction stream** — one `activity_events` row per tap or message
(`kind` + callback prefix; message text is never stored). Telegram gives no
app-open/close signal, so "time in the bot" can only be *reconstructed* from
this by grouping events and starting a new visit after a 5-minute gap. Fuzzy
by nature — use it for patterns (which screens people bounce off, how often
they come back), never as a member's training time. `/admin analytics off`
stops the logging; nothing else changes. Rows older than 90 days are deleted
by a daily job (`ACTIVITY_RETENTION_DAYS` in `bot/analytics.py`) — the
numbers you care about long-term (scores, `duration_s`) live on the sessions
table and are never purged.

Analysing it: the table is plain SQLite, so `docs/DASHBOARD.md`'s Streamlit
setup reads it read-only alongside the session table. Retention is unlimited
for now — `ActivityEventRepository.purge_older_than(days)` exists but nothing
calls it; wire it to a job once you decide how far back you care.

## Users & data

- User progress: `/admin users`, deeper analysis via `/admin export` CSV
- Time on task: `/admin time` (see above)
- Web dashboard with shareable PNG charts: build guide in `docs/DASHBOARD.md`
- Leaderboard is opt-in — users join via button under `/leaderboard`

## Backups

Never back up the database with `cp`. The bot runs SQLite in WAL mode, so the
newest writes sit in a separate `-wal` file — a plain copy taken while the bot
is running can be inconsistent or missing rows. `scripts/backup_db.sh` uses
SQLite's online backup API instead, which is safe on a live database, then
integrity-checks the copy and prunes anything older than 14 days.

```bash
bash /root/mental_training_bot/scripts/backup_db.sh
```

**Run it before every deploy that changes the schema** — migrations write to
production data and there is no undo. Daily otherwise; install once with:

```bash
echo '0 3 * * * root bash /root/mental_training_bot/scripts/backup_db.sh >> /var/log/mtb_backup.log 2>&1' > /etc/cron.d/mtb-backup
```

Backups land in `/root/backups/mtb_<date>_<time>.db`. Each one is a complete,
standalone database — restoring is just stopping the bot, copying the file
over `mental_training.db`, and starting again.

Every run also repoints `/root/backups/latest.db` at the newest snapshot
(only after its integrity check passes), so pulling one down is a fixed
command with no filename to look up. To analyse data locally — e.g. the
dashboard — copy *that*, never the live database:

```bash
scp root@<VPS_IP>:/root/backups/latest.db snapshot.db
```

Run it from the project folder: `dashboard.py` looks for `snapshot.db`
beside it.

## VPS operations

**Deploying a new version** — one command from your machine:

```
ssh root@<VPS_IP> bash /root/mental_training_bot/scripts/deploy.sh
```

It refuses if files were edited on the server (fix: `git stash` there, or
discard), fast-forward pulls, restarts the service, and prints the build
line from the log. Then `/admin version` in the bot should show the new
commit. Never edit files on the VPS directly — a dirty tree blocks every
future deploy and the bot keeps running old code without telling you.

```bash
ssh root@<VPS_IP>

systemctl status mental_training_bot          # is it running?
systemctl restart mental_training_bot         # after any code/config change
journalctl -u mental_training_bot -n 50 --no-pager   # recent logs

# deploy latest code (normal path — commit + push locally first)
bash /root/mental_training_bot/scripts/deploy.sh

# watch logs live while testing a command in Telegram
journalctl -u mental_training_bot -n 0 -f

# backup the database (do this before risky changes!) — never cp a WAL-mode
# SQLite file; this uses the online backup API and verifies the copy
bash /root/mental_training_bot/scripts/backup_db.sh
```

Normal deploy flow is always: edit on laptop → commit → push to GitHub →
`deploy.sh` on the VPS. Never edit files directly on the VPS — those edits sit
uncommitted, `deploy.sh` refuses to run, and `/admin version` shows ⚠️. If a
stray edit did happen and you're sure GitHub's version supersedes it:
`git stash push -m server-edits` (keeps a copy) or `git checkout -- <file>`
(discards), then deploy again.

One-off single-file push without a full deploy cycle (e.g. testing a tiny
change before committing) — from a local Git Bash / PowerShell terminal:

```bash
scp bot/handlers.py root@<VPS_IP>:/root/mental_training_bot/bot/handlers.py
ssh root@<VPS_IP> "systemctl restart mental_training_bot"
```

Still commit + push the same change on the laptop afterward, or the VPS
diverges from GitHub again.

`.env` on the VPS needs:

```
TELEGRAM_BOT_TOKEN=...
ADMIN_TELEGRAM_IDS=<your telegram id>    # comma-separated if several admins
```

Add/edit a value without opening an editor:

```bash
echo 'ADMIN_TELEGRAM_IDS=123456789' >> /root/mental_training_bot/.env
systemctl restart mental_training_bot   # required to pick up .env changes
```

DB schema changes apply automatically at startup (migrations + backfills run
in `database/connection.py::init_db`).

## Quick diagnosis

| Symptom | Check |
|---|---|
| Bot silent | `systemctl status mental_training_bot`, then journalctl logs |
| `/admin` does nothing | Your ID missing from `ADMIN_TELEGRAM_IDS`, or no restart after adding it |
| Bot slow for everyone | Should not happen (concurrent updates on) — check VPS CPU/RAM: `htop` |
| "Conflict: terminated by other getUpdates" in logs | Two bot instances running — make sure it runs only on the VPS |
