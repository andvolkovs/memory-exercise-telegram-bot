# Feature Roadmap

Backlog of agreed-on ideas, roughly ordered by (training value × low effort).
Working agreement: build one at a time, flag-gate anything user-facing, keep
each feature removable like audio-viz. Details get worked out at build time —
what's written here is direction, not final spec.

Suggested build order: #1's audio half once audio is switched on; #3 as
the next big exercise. #2 partially superseded by
the post-listen focus check — revisit whether vividness still adds signal.
Shipped: #7 (redemption codes), #9 (daily reminder + fresh-mind bonus),
#4 (visualization XP bar + audio achievements), #10 L1+L2 (usage logging +
analytics dashboard), #1 word-memo half (as saved lists + review).

---

## 1. Delayed recall (spaced repetition) — flagship feature — word memo ✅ SHIPPED as saved lists

First shipped (Oct 2026) as an automatic once-a-day recall of a 5–10
question sample, pushed via /start and the reminder ping. Owner didn't like
it; replaced (Nov 2026) by user-driven **saved lists**: every test list is
kept by date (auto-save on by default, a button under results flips it),
reviewable from a panel at any time — whole list, from memory, ¼ XP at 80%+
on the first review of a list each day. Spec: docs/FEATURES.md › Saved
lists. Still open: the audio half, SM-2-style "due for review" hints in
the panel or the reminder ping, "Iron Memory" achievement. Original plan
below.


Re-test users on material from previous days. Retrieval after sleep is where
consolidation actually happens — immediate quizzes test attention, delayed
ones test whether it stuck. Applies to BOTH exercises:

**Audio visualization**: next day the bot messages "Yesterday you heard *The
Night Train* — what color was the conductor's uniform?" Sessions already
store story id + timestamp. Sidecar questions beyond the 3 asked immediately
(`MAX_QUIZ_QUESTIONS`) can be reserved for the delayed round, or the same 3
reused with reshuffled options.

**Word memorization**: after a scored test, save the actual word pairs (add
`pairs` to session `parameters` JSON at save time — they currently live only
in handler state and are lost). A daily job then re-quizzes a sample (e.g.
5–10 pairs) from yesterday's test. Saved as `mode: "recall"`. Later: proper
SM-2-style intervals (1 → 3 → 7 days) per list.

**Shared plumbing** (build once, both exercises use it):
- Daily `job_queue` job scanning for due re-tests; per-user opt-in via
  `/settings` + a quiet-hours window; unanswered prompts expire silently.
- XP hook: delayed recall feeds the SAME skill bar as the source exercise
  (mnemonics / visualization) with a retention multiplier (e.g. 1.5×) —
  spaced retrieval is harder than immediate, should pay more.
- Streak counts (it's active work). Achievements later ("Iron Memory:
  90%+ on a 3-day-old list").
- Excluded from leaderboard averages initially (same `_IS_SCORED_TEST`
  pattern as retry/placement/audio_quiz).

## 2. Vividness self-rating after passive listens — build first

One tap after each listen: "How vivid was your movie? 1–5". Stored in session
`parameters` (`vividness`). Gives the passive exercise a visible progress
curve — `/stats` line like "avg vividness this month: 4.1 (was 2.8)". Very
low effort, big retention payoff.

## 3. "Story method" bridge exercise — the technique teacher

Third exercise/mode that closes the loop between the two existing ones:
show 10 words, explicitly instruct "link them into one absurd story in your
head", then quiz. Then show the user their story-method score vs their raw
word-memo baseline — the moment they *feel* the technique working. Reuses the
whole existing quiz engine (timers, retry, results); mostly new copy + intro
flow, not new machinery. This is the product's core promise made tangible.

## 4. Visualization XP bar + audio achievements ✅ SHIPPED

Built as self-contained `gamification/audio_xp.py` (registers the
"visualization" `SkillDef` + 3 audio achievements at import time). Anti-farm
layers: separate bar, first-listen-only XP (replays = 0), small fixed XP for
passive listens per length bucket, quiz XP scaled accuracy² with 50% floor,
80 XP/day cap. Kill switch `/admin audioxp on|off` (default ON).

## 5. Daily community story — Skool engagement

Everyone gets the same story on the same day; quiz scores feed a daily
mini-leaderboard. Shared experience → discussion in the community → the bot
markets itself inside the group. Needs: a "story of the day" picker (seeded
by date), a daily board query, maybe an announcement message.

## 6. Detail-density difficulty for audio

Same length buckets, but "advanced" stories pack ~2× the concrete details and
quizzes pull harder questions. Gives audio a progression ladder like
word-memo's difficulty tiers. Mostly content work (story writing guidelines
per tier in ADMIN_GUIDE), light code (difficulty field in sidecar + keyboard).

## 7. Redemption codes + founding-member offer — Skool access gate ✅ SHIPPED

One-time codes gate the bot to paid Skool members, no Skool API needed:

- New `redemption_codes` table: `code`, `tier`, `duration_days`
  (NULL = lifetime), `redeemed_by`, `redeemed_at`.
- `/admin codes <n> <tier> <days|lifetime>` generates a batch;
  `/redeem CODE` in the bot sets `subscription_tier` +
  `subscription_expires_at` (NULL = never expires). One-time codes
  (not multi-use) so a leaked code burns one spot, not the campaign.
- **Founding-member campaign**: first 100 signups get full access forever —
  generate the first batch as 100 lifetime codes, hand out per member in the
  paid Skool section. After those, switch to 30-day batches rotated monthly.
- Later: replace monthly rotation with Zapier "member joined/left" webhook →
  endpoint on the VPS → auto grant/revoke.

## 8. Milestone level trials + community rewards

XP levels stay automatic except every 5th level, which is gated by a
**trial**: a fixed challenge test (count/difficulty/speed defined per
milestone), pass at ≥90% to cross. Rare enough to feel like a boss fight.

- Pass → badge/title shown on leaderboard ("🏅 Adept", "🏆 Master").
- Skool rewards are manual (no Skool API for points), which keeps them
  personal: shoutout post on milestone, classroom module unlocks, higher
  milestones = free month / 1-on-1 call.
- Pairs well with #5 (daily community story) — trials as community events.

## 9. Daily training reminder + fresh-mind XP bonus — #1's plumbing ✅ SHIPPED

Opt-in daily notification with a one-tap preset test start; answering fast
pays bonus XP. Builds the daily `job_queue` sweep + opt-in + quiet-hours
plumbing that delayed recall (#1) needs — build this first, #1 rides on it.

- `/settings`: pick reminder hour + preferred test (difficulty/count/speed/
  format, default = last used). Off by default, one tap to disable, never
  ping twice a day. Unanswered prompts expire silently.
- Notification carries a one-tap button ("Start: Intermediate, 15 pairs")
  with the preset — the button IS the feature, no menu friction.
- **Fresh-mind bonus**: start within 15 min of the ping → ×1.25 XP on that
  test, once per day. Multiplier not flat XP (flat = farmable with tiny
  tests; multiplier scales with challenge rating like the rest of xp.py).
- **Streak-saver variant** (phase 2, maybe the better default): only ping
  when today's streak is about to die (e.g. 21:00 user time, no test yet).
  Loss aversion > reward.
- Once #1 exists, the same daily slot delivers recall prompts ("Quick one:
  5 pairs from yesterday") — one notification channel, two features.
- **Timezone catch**: Telegram exposes no user timezone. Setup asks "what
  time is it for you right now?" (hour buttons) → derive + store offset in
  prefs. Good enough until the Postgres era.
- Achievement later: "Early bird — 7 on-time responses in a row."

## 10. Usage analytics → recommendations → shareable progress

Layered feature. **L1 (logging) is SHIPPED**: `ExerciseSession.duration_s`
(engaged training seconds) + the `activity_events` interaction stream, read
via `/admin time`, kill switch `/admin analytics on|off`. See `bot/analytics.py`
and the ADMIN_GUIDE section. Decided up front: users only ever see *training*
time (study + quiz + listening), never a reconstructed "time in bot" — the
latter is admin-side pattern analysis only.

**L2 (cohort dashboard) is SHIPPED**: `dashboard.py` — minutes per
day/user/exercise, commitment-vs-improvement scatter, attempts by list size,
engagement sessionized from the event stream, per-member drilldown. Runs
locally against a snapshot; no bot code, nothing exposed on the VPS.

Remaining layers, in build order:

**L3 — daily personal recommendation.** Build against real distributions from
the L2 dashboard, not guesses — the thresholds decide what every member gets
told. One extra line inside the existing opt-in reminder ping
(`bot/reminders.py` already has the hourly sweep, the timezone offset and the
quiet-hours logic — no new notification channel, no second opt-in). Rules
engine, not an LLM: ~12 if-clauses over minutes-last-7d,
exercise mix, accuracy trend, difficulty ceiling, streak risk. Examples:
three sessions all 10 pairs all ≥95% → push 15; audio-only for 5 days → nudge
word memo; median session 90s → suggest one longer block. Debuggable and free.

**L4 — shareable progress cards (PNG).** Both variants wanted:
- *Personal*: "Share my progress" button → level, streak, minutes trained,
  accuracy curve. The member posts it in Skool; the bot markets itself.
- *Community*: `/admin card` → aggregate stats (members, total hours trained,
  average improvement) for announcement posts.
Needs Pillow (currently commented out in `requirements.txt`). Mostly design
work; render server-side, send as a photo.

**Guardrail carried through all layers**: time is context, never rank. Ten
focused minutes beat sixty idle ones, so the leaderboard stays on accuracy and
no screen ever implies "more minutes = better". Usage disclosure is live in
`/help` ("🔒 Your data") — update it if L3/L4 start recording anything new.

## Parked ideas

- **Study-phase metronome audio**: Telegram bots can't trigger vibration or
  sounds, so the workaround is a pre-generated tick mp3 (tick every
  5s/2.5s, duration = study time) sent with the study message; `file_id`
  cached like audio stories, opt-in via `/settings`. Parked — revisit if
  users ask for pacing help.
