"""
Delayed recall — re-test a sample of an earlier word-memo test, hours or
days later (ROADMAP #1, word-memo half). Immediate quizzes test attention;
retrieval after sleep tests whether it stuck.

Source: the user's newest fresh word-memo test (mode "test") completed
between RECALL_MIN_AGE_H and RECALL_MAX_AGE_H ago that stored its word list
(parameters["items"], written by quiz_engine since this feature shipped).
At most one recall per UTC day, and each source is recalled once.

The round: RECALL_MIN_QUESTIONS–RECALL_MAX_QUESTIONS questions, no study
phase, run by the shared quiz engine as round_mode "recall".
- Pairs source: sampled pairs in study order, random side shown.
- List source: sampled positions shown as "which word came right after X",
  never two neighbours — the prompt for position i+1 would reveal answer i.
  Runs in pairs format so results list "X — Y" lines for the sample only.

Bookkeeping (quiz_engine._show_test_results): saved with mode "recall" +
source_id; excluded from stats/leaderboard/PB/achievements via
_IS_SCORED_TEST (and dashboard.py UNSCORED_MODES); streak counts (active
work); never touches the hard streak; XP on the mnemonics bar at
×RECALL_XP_MULT (gamification/xp.py) without diminishing returns.

Delivery: a button inside the daily reminder ping (bot/reminders.py — same
opt-in, same hour, sent even if the user already trained today) and on
/start. No new notification channel, no extra opt-in.

Prefs schema (user.preferences["recall"]):
    {"source_id": int, "date": iso-date}   # claimed when the round starts

REMOVAL: delete this module plus: "recall" route (bot/handlers.py), the
recall hooks in bot/reminders.py and bot/commands.py (start_command), the
recall branches in bot/quiz_engine.py::_show_test_results (is_recall),
get_recall_candidates (database/repositories.py), RECALL_ENABLED_KEY
(bot/features.py), "recall" admin subcommand (bot/admin.py). Keep
"recall" in _IS_SCORED_TEST so old rows stay unscored. Or just
/admin recall off.
"""

import asyncio
import logging
import random
from datetime import timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode

from database import get_session, UserRepository, ExerciseSessionRepository
from database.models import utcnow
from exercises import Difficulty
from exercises.word_memorization import QUESTION_TIME_LIMIT
from .analytics import mark_round_start
from .features import is_flag_enabled, RECALL_ENABLED_KEY
from .quiz_engine import cancel_question_timer, cancel_study_timer, send_next_question
from .state import (
    get_user_state, set_user_state, clear_user_state,
    track_bot_message, cleanup_bot_messages,
)

logger = logging.getLogger(__name__)

# A night's sleep is the point; 8h still catches "trained at 23:00, reminder
# at 08:00". After 4 days the list is stale — let the next test replace it.
RECALL_MIN_AGE_H = 8
RECALL_MAX_AGE_H = 96
RECALL_MIN_QUESTIONS = 5
RECALL_MAX_QUESTIONS = 10

PREF_KEY = "recall"

# Users whose recall round is being set up — double-tap guard across the
# DB await (test_active isn't set until the round is built).
_starting: set[int] = set()


# ============================================================================
# Pure helpers
# ============================================================================

def recall_question_count(n_items: int, fmt: str) -> int:
    """Half the list, clamped to 5–10, never more than the list holds. List
    sources cap at n//2: non-neighbouring positions among 1..n-1."""
    k = min(n_items, max(RECALL_MIN_QUESTIONS, min(RECALL_MAX_QUESTIONS, n_items // 2)))
    if fmt == "list":
        k = min(k, n_items // 2)
    return k


def build_recall_round(items: list, fmt: str, rng=random) -> tuple[list, list]:
    """(test_pairs, quiz_items) for the engine, pairs-shaped either way.

    pair_index is the position in the returned test_pairs (0..k-1), so the
    engine's results/total see only the sample, not the whole old test.
    """
    k = recall_question_count(len(items), fmt)
    if fmt == "list":
        # k non-adjacent positions from 1..n-1: choose k from a range shrunk
        # by k-1, then spread them out by their rank (gap >= 2 guaranteed).
        m = len(items) - 1
        picks = sorted(rng.sample(range(m - k + 1), k))
        positions = [1 + c + j for j, c in enumerate(picks)]
        pairs = [(items[p - 1], items[p]) for p in positions]
        quiz_items = [
            {"pair_index": i, "direction": "next", "shown_word": a, "expected": b}
            for i, (a, b) in enumerate(pairs)
        ]
        return pairs, quiz_items

    idxs = sorted(rng.sample(range(len(items)), k))
    pairs = [tuple(items[i]) for i in idxs]
    quiz_items = []
    for i, (w1, w2) in enumerate(pairs):
        shown, expected = (w1, w2) if rng.random() < 0.5 else (w2, w1)
        quiz_items.append({"pair_index": i, "shown_word": shown, "expected": expected})
    return pairs, quiz_items


def age_label(hours: float) -> str:
    if hours < 24:
        return f"your test {int(hours)} hours ago"
    if hours < 48:
        return "yesterday's test"
    return f"your test {int(hours // 24)} days ago"


# ============================================================================
# Due check (used by /start and the reminder sweep)
# ============================================================================

async def get_due_recall(session_repo: ExerciseSessionRepository, db_user) -> dict | None:
    """The recall this user can take right now, or None."""
    if not is_flag_enabled(RECALL_ENABLED_KEY):
        return None
    now = utcnow()
    rec = (db_user.preferences or {}).get(PREF_KEY, {})
    if rec.get("date") == now.date().isoformat():
        return None
    candidates = await session_repo.get_recall_candidates(
        db_user.id,
        completed_after=now - timedelta(hours=RECALL_MAX_AGE_H),
        completed_before=now - timedelta(hours=RECALL_MIN_AGE_H),
    )
    for sess in candidates:
        params = sess.parameters or {}
        items = params.get("items")
        if not items:
            continue  # pre-feature row — no word list stored
        # Newest source already recalled → nothing new until the next test.
        if sess.id == rec.get("source_id"):
            return None
        fmt = params.get("format", "pairs")
        questions = recall_question_count(len(items), fmt)
        if questions < 1:
            return None
        hours = (now - sess.completed_at).total_seconds() / 3600
        return {
            "source_id": sess.id, "difficulty": sess.difficulty,
            "format": fmt, "items": items, "questions": questions,
            "age_label": age_label(hours),
        }
    return None


def recall_button(due: dict) -> InlineKeyboardButton:
    n = due["questions"]
    return InlineKeyboardButton(
        f"🧠 Delayed recall: {n} question{'s' if n != 1 else ''}",
        callback_data=f"recall:go:{due['source_id']}",
    )


def recall_ping_text(due: dict) -> str:
    return (
        f"🧠 *Delayed recall ready* — {due['questions']} questions from "
        f"{due['age_label']}. No study phase: what stuck?"
    )


# ============================================================================
# Callback (recall: prefix)
# ============================================================================

async def handle_recall_callback(query, context, data: str) -> None:
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else None
    if action != "go" or len(parts) < 3:
        return
    source_id = int(parts[2])
    user_id = query.from_user.id
    chat_id = query.message.chat_id

    state = get_user_state(context)
    if state.get("test_active") or user_id in _starting:
        return
    if not is_flag_enabled(RECALL_ENABLED_KEY):
        await query.edit_message_text("Delayed recall is paused right now.")
        return

    _starting.add(user_id)
    try:
        async with get_session() as session:
            user_repo = UserRepository(session)
            db_user = await user_repo.get_by_telegram_id(user_id)
            due = (
                await get_due_recall(ExerciseSessionRepository(session), db_user)
                if db_user else None
            )
            if not due or due["source_id"] != source_id:
                await query.edit_message_text(
                    "⌛ This recall isn't available anymore — one per day, "
                    "from a test 8 hours to 4 days old.",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu"),
                    ]]),
                )
                return
            # Claimed at start, not at results: abandoning and restarting
            # would otherwise give a second look at the same sample.
            await user_repo.update_preferences(user_id, {PREF_KEY: {
                "source_id": source_id, "date": utcnow().date().isoformat(),
            }})

        pairs, quiz_items = build_recall_round(due["items"], due["format"])

        cancel_question_timer(context, user_id)
        cancel_study_timer(context, user_id)
        await cleanup_bot_messages(context.bot, chat_id, state)
        clear_user_state(context)
        set_user_state(context, "current_exercise", "word_memo")
        set_user_state(context, "test_active", True)
        set_user_state(context, "test_exercise_type", "word_memo")
        set_user_state(context, "test_pairs", pairs)
        set_user_state(context, "test_format", "pairs")
        set_user_state(context, "test_quiz_items", quiz_items)
        set_user_state(context, "test_current_index", 0)
        set_user_state(context, "test_results", [])
        set_user_state(context, "test_difficulty", Difficulty(due["difficulty"]))
        set_user_state(context, "test_chat_id", chat_id)
        set_user_state(context, "baseline_results", [])
        set_user_state(context, "test_round_mode", "recall")
        set_user_state(context, "speed_mode", False)
        set_user_state(context, "recall_source_id", source_id)
        set_user_state(context, "recall_source_format", due["format"])
        set_user_state(context, "recall_age_label", due["age_label"])
        state = get_user_state(context)
        mark_round_start(state)
    finally:
        _starting.discard(user_id)

    how = (
        "I show a word from the list — you recall the one that came *right after* it."
        if due["format"] == "list"
        else "I show one word of a pair — you recall its partner."
    )
    await query.edit_message_text(
        f"🧠 *Delayed recall — {len(quiz_items)} questions*\n\n"
        f"Words from {due['age_label']}. No study phase this time.\n"
        f"{how}\n"
        f"Each question has *{QUESTION_TIME_LIMIT}s*.",
        parse_mode=ParseMode.MARKDOWN,
    )
    track_bot_message(state, query.message.message_id)
    await asyncio.sleep(1.0)
    await send_next_question(context, chat_id, get_user_state(context), user_id)
