"""
Saved lists — every word-memo test list, kept by date, reviewable from
memory at any time. Replaced the Oct 2026 delayed-recall flow.

Storage: no table of its own. A saved list IS the fresh test's session row
(mode "test"): quiz_engine stores the word list in parameters["items"] on
every fresh word-memo test, plus parameters["saved"] from the user's
auto-save setting (preferences["autosave_lists"], default on). Date/time,
difficulty, format, size and the original score are the row's own columns.
"saved" missing = saved (rows from the recall build carry over).

After each test the results keyboard gets one toggle — the opposite of the
user's default: "🗑 Don't save this list" when auto-save is on, "💾 Save
this list" when it's off. It flips in place (lists:save / lists:unsave).

Panel (/lists, main menu "📚 Saved lists"): newest first, LISTS_PER_PAGE
per page, each list opens a detail card → 🎯 Review from memory / 🗑 Remove.
Times show in the user's local hour when the reminder setup gave us their
UTC offset, else UTC.

Review: the whole list, quiz straight away (no study phase), run by the
shared quiz engine as round_mode "review" in the list's own format.
Bookkeeping (quiz_engine._show_test_results): unscored (_IS_SCORED_TEST +
dashboard UNSCORED_MODES — no stats/leaderboard/PB/achievements), daily
streak counts, hard streak untouched, XP = REVIEW_XP_MULT (¼) of what the
round would pay as a fresh test, only at REVIEW_MIN_PCT (80%)+ and only on
the first review of that list per UTC day (review_xp_date on the source
row; also last_review_at / last_review_pct / reviews).

REMOVAL: delete this module plus: "lists" route (bot/handlers.py), /lists
command (bot/__init__.py, bot/menu.py), main-menu button + autosave
settings rows (bot/commands.py), the saved-list / review branches in
bot/quiz_engine.py::_show_test_results (is_fresh_list, is_review), the
saved-list queries (database/repositories.py), SAVED_LISTS_ENABLED_KEY
(bot/features.py), "lists" admin subcommand (bot/admin.py). Keep "review"
in _IS_SCORED_TEST so old rows stay unscored. Or just /admin lists off.
"""

import asyncio
import logging
from datetime import datetime, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from database import get_session, UserRepository, ExerciseSessionRepository
from database.models import utcnow
from exercises import Difficulty
from exercises.word_memorization import (
    QUESTION_TIME_LIMIT, DIFFICULTY_NAMES, DIFF_EMOJI, FORMAT_UNITS,
)
from gamification.xp import REVIEW_MIN_PCT
from .analytics import mark_round_start
from .features import is_flag_enabled, SAVED_LISTS_ENABLED_KEY
from .quiz_engine import cancel_question_timer, cancel_study_timer, send_next_question
from .state import (
    get_user_state, set_user_state, clear_user_state,
    track_bot_message, cleanup_bot_messages,
)
from .word_memo import _build_quiz_items, _build_list_quiz_items

logger = logging.getLogger(__name__)

LISTS_PER_PAGE = 8
AUTOSAVE_PREF = "autosave_lists"

# Users whose review is being set up — double-tap guard across the DB await
# (test_active isn't set until the round is built).
_starting: set[int] = set()

_MAIN_MENU_ROW = [InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")]


def lists_menu_enabled() -> bool:
    return is_flag_enabled(SAVED_LISTS_ENABLED_KEY)


def autosave_on(preferences: dict) -> bool:
    return (preferences or {}).get(AUTOSAVE_PREF, True)


# ============================================================================
# Formatting helpers
# ============================================================================

def tz_offset_hours(preferences: dict) -> int | None:
    """Whole-hour UTC offset from the reminder setup, or None if unknown."""
    rem = (preferences or {}).get("reminder", {})
    if "hour_local" not in rem or "utc_hour" not in rem:
        return None
    off = (rem["hour_local"] - rem["utc_hour"]) % 24
    return off - 24 if off > 12 else off


def format_when(dt: datetime | None, offset: int | None, with_year: bool = False) -> str:
    if dt is None:
        return "unknown date"
    local = dt + timedelta(hours=offset or 0)
    text = f"{local:%b} {local.day}"
    if with_year:
        text += f", {local.year}"
    text += f", {local:%H:%M}"
    if offset is None:
        text += " UTC"
    return text


def _unit(fmt: str) -> str:
    return FORMAT_UNITS.get(fmt, "pairs")


def _list_button_label(row, offset: int | None) -> str:
    params = row.parameters or {}
    fmt = params.get("format", "pairs")
    emoji = DIFF_EMOJI.get(row.difficulty, "⚪")
    count = len(params.get("items") or [])
    pct = f" · {row.score * 100 // row.max_score}%" if row.max_score else ""
    return f"{format_when(row.completed_at, offset)} · {emoji} {count} {_unit(fmt)}{pct}"


# ============================================================================
# Results-screen toggle (called from bot/quiz_engine.py)
# ============================================================================

def save_toggle_button(row_id: int, saved: bool) -> InlineKeyboardButton:
    if saved:
        return InlineKeyboardButton("🗑 Don't save this list", callback_data=f"lists:unsave:{row_id}")
    return InlineKeyboardButton("💾 Save this list", callback_data=f"lists:save:{row_id}")


def with_save_toggle(keyboard: InlineKeyboardMarkup, row_id: int, saved: bool) -> InlineKeyboardMarkup:
    """Results keyboard + the toggle row, just above the last (menu) row."""
    if not lists_menu_enabled():
        return keyboard
    rows = [list(r) for r in keyboard.inline_keyboard]
    rows.insert(max(len(rows) - 1, 0), [save_toggle_button(row_id, saved)])
    return InlineKeyboardMarkup(rows)


def _swap_toggle(markup: InlineKeyboardMarkup | None, row_id: int, saved: bool) -> InlineKeyboardMarkup | None:
    if markup is None:
        return None
    ids = (f"lists:save:{row_id}", f"lists:unsave:{row_id}")
    rows = [
        [save_toggle_button(row_id, saved) if b.callback_data in ids else b for b in r]
        for r in markup.inline_keyboard
    ]
    return InlineKeyboardMarkup(rows)


def review_results_keyboard(source_id: int | None) -> InlineKeyboardMarkup:
    rows = []
    if source_id is not None:
        rows.append([InlineKeyboardButton("🔁 Review again", callback_data=f"lists:review:{source_id}")])
    rows.append([InlineKeyboardButton("📚 Saved lists", callback_data="lists:page:0")])
    rows.append(_MAIN_MENU_ROW)
    return InlineKeyboardMarkup(rows)


# ============================================================================
# Settings integration (called from bot/commands.py)
# ============================================================================

def autosave_settings_text(preferences: dict) -> str:
    if not lists_menu_enabled():
        return ""
    on = autosave_on(preferences)
    return (
        "\n\n💾 *Auto-save lists*\n"
        "_On: every test list goes to 📚 Saved lists (a button under the "
        "results lets you skip one). Off: only lists you save by hand._\n"
        f"Currently: *{'On ✅' if on else 'Off'}*"
    )


def autosave_settings_rows(preferences: dict) -> list[list[InlineKeyboardButton]]:
    if not lists_menu_enabled():
        return []
    on = autosave_on(preferences)
    return [[InlineKeyboardButton(
        f"💾 Auto-save lists: {'✅ On' if on else '⬜ Off'}",
        callback_data="lists:autosave",
    )]]


# ============================================================================
# Panel
# ============================================================================

async def _panel(telegram_id: int, page: int) -> tuple[str, InlineKeyboardMarkup]:
    async with get_session() as session:
        db_user = await UserRepository(session).get_by_telegram_id(telegram_id)
        if not db_user:
            return "Use /start first.", InlineKeyboardMarkup([_MAIN_MENU_ROW])
        repo = ExerciseSessionRepository(session)
        total = await repo.count_saved_lists(db_user.id)
        pages = max((total + LISTS_PER_PAGE - 1) // LISTS_PER_PAGE, 1)
        page = min(max(page, 0), pages - 1)
        rows_db = await repo.get_saved_lists(
            db_user.id, limit=LISTS_PER_PAGE, offset=page * LISTS_PER_PAGE,
        )
        prefs = db_user.preferences or {}

    offset = tz_offset_hours(prefs)
    auto = "On" if autosave_on(prefs) else "Off"
    if total == 0:
        how = (
            "Every test list is saved here automatically."
            if autosave_on(prefs)
            else "Auto-save is off — tap 💾 Save this list under a test's results."
        )
        text = f"📚 *Saved lists*\n\nNo saved lists yet. {how}"
        return text, InlineKeyboardMarkup([_MAIN_MENU_ROW])

    text = (
        f"📚 *Saved lists* — {total}\n\n"
        "Tap a list to review it from memory: the whole list, no study "
        f"phase. The first review of a list each day at {REVIEW_MIN_PCT:.0f}%+ "
        "earns a little XP.\n\n"
        f"Auto-save: *{auto}* (change in /settings)"
    )
    if pages > 1:
        text += f"\nPage {page + 1} of {pages}"
    kb = [
        [InlineKeyboardButton(
            _list_button_label(r, offset), callback_data=f"lists:open:{r.id}:{page}",
        )]
        for r in rows_db
    ]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀ Newer", callback_data=f"lists:page:{page - 1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("Older ▶", callback_data=f"lists:page:{page + 1}"))
    if nav:
        kb.append(nav)
    kb.append(_MAIN_MENU_ROW)
    return text, InlineKeyboardMarkup(kb)


async def _detail(telegram_id: int, row_id: int, page: int) -> tuple[str, InlineKeyboardMarkup] | None:
    async with get_session() as session:
        db_user = await UserRepository(session).get_by_telegram_id(telegram_id)
        if not db_user:
            return None
        row = await ExerciseSessionRepository(session).get_saved_list(db_user.id, row_id)
        if row is None:
            return None
        prefs = db_user.preferences or {}
        params = dict(row.parameters or {})
        when, difficulty = row.completed_at, row.difficulty
        score, max_score = row.score, row.max_score

    offset = tz_offset_hours(prefs)
    fmt = params.get("format", "pairs")
    count = len(params.get("items") or [])
    diff_name = DIFFICULTY_NAMES.get(Difficulty(difficulty), difficulty) if difficulty else "?"
    fmt_icon = "📜" if fmt == "list" else "🔗"
    lines = [
        "📚 *Saved list*\n",
        f"📅 {format_when(when, offset, with_year=True)}",
        f"{DIFF_EMOJI.get(difficulty, '⚪')} {diff_name} · {fmt_icon} {count} {_unit(fmt)}"
        + (" · ⚡ speed" if params.get("speed") else ""),
    ]
    if max_score:
        lines.append(f"🎯 Test score: {score}/{max_score} ({score * 100 // max_score}%)")
    reviews = params.get("reviews", 0)
    if reviews:
        last_at = params.get("last_review_at")
        last_dt = datetime.fromisoformat(last_at) if last_at else None
        lines.append(
            f"🔁 Reviewed {reviews}× · last {params.get('last_review_pct', 0)}% "
            f"({format_when(last_dt, offset)})"
        )
    if params.get("review_xp_date") == utcnow().date().isoformat():
        lines.append("⭐ _Review XP already earned for this list today._")
    else:
        lines.append(f"⭐ _Review at {REVIEW_MIN_PCT:.0f}%+ today for a little XP._")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🎯 Review from memory", callback_data=f"lists:review:{row_id}")],
        [InlineKeyboardButton("🗑 Remove from saved", callback_data=f"lists:remove:{row_id}:{page}")],
        [InlineKeyboardButton("◀ Back", callback_data=f"lists:page:{page}")],
    ])
    return "\n".join(lines), kb


async def lists_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not lists_menu_enabled():
        await update.message.reply_text("Saved lists are paused right now.")
        return
    text, kb = await _panel(update.effective_user.id, 0)
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)


# ============================================================================
# Callbacks (lists: prefix)
# ============================================================================

async def _set_saved(telegram_id: int, row_id: int, saved: bool) -> bool:
    async with get_session() as session:
        db_user = await UserRepository(session).get_by_telegram_id(telegram_id)
        if not db_user:
            return False
        repo = ExerciseSessionRepository(session)
        row = await repo.get_own_test_row(db_user.id, row_id)
        if row is None:
            return False
        await repo.set_parameters(row, saved=saved)
        return True


async def handle_lists_callback(query, context, data: str) -> None:
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else None
    user_id = query.from_user.id

    if not lists_menu_enabled():
        await query.edit_message_text(
            "Saved lists are paused right now.",
            reply_markup=InlineKeyboardMarkup([_MAIN_MENU_ROW]),
        )
        return

    if action == "page":
        page = int(parts[2]) if len(parts) > 2 else 0
        text, kb = await _panel(user_id, page)
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)

    elif action == "open":
        row_id, page = int(parts[2]), int(parts[3]) if len(parts) > 3 else 0
        detail = await _detail(user_id, row_id, page)
        if detail is None:
            text, kb = await _panel(user_id, page)
        else:
            text, kb = detail
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)

    elif action == "remove":
        row_id, page = int(parts[2]), int(parts[3]) if len(parts) > 3 else 0
        await query.edit_message_text(
            "🗑 Remove this list from your saved lists?\n\n"
            "Your test score stays in your stats either way.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Yes, remove", callback_data=f"lists:rmyes:{row_id}:{page}")],
                [InlineKeyboardButton("Cancel", callback_data=f"lists:open:{row_id}:{page}")],
            ]),
        )

    elif action == "rmyes":
        row_id, page = int(parts[2]), int(parts[3]) if len(parts) > 3 else 0
        await _set_saved(user_id, row_id, False)
        text, kb = await _panel(user_id, page)
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)

    elif action in ("save", "unsave"):
        # Results-screen toggle: flip and swap the button in place, leaving
        # the rest of the results message untouched.
        row_id = int(parts[2])
        saved = action == "save"
        if await _set_saved(user_id, row_id, saved):
            markup = _swap_toggle(getattr(query.message, "reply_markup", None), row_id, saved)
            if markup is not None:
                await query.edit_message_reply_markup(reply_markup=markup)

    elif action == "autosave":
        async with get_session() as session:
            user_repo = UserRepository(session)
            db_user = await user_repo.get_by_telegram_id(user_id)
            if not db_user:
                await query.edit_message_text("Use /start first.")
                return
            current = autosave_on(db_user.preferences)
            db_user = await user_repo.update_preferences(user_id, {AUTOSAVE_PREF: not current})
            prefs = db_user.preferences or {}
        from .commands import _settings_text_and_keyboard   # lazy: cycle
        text, kb = _settings_text_and_keyboard(prefs)
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)

    elif action == "review":
        await start_review(query, context, int(parts[2]))


async def start_review(query, context, row_id: int) -> None:
    user_id = query.from_user.id
    chat_id = query.message.chat_id
    state = get_user_state(context)
    if state.get("test_active") or user_id in _starting:
        return

    _starting.add(user_id)
    try:
        async with get_session() as session:
            db_user = await UserRepository(session).get_by_telegram_id(user_id)
            row = (
                await ExerciseSessionRepository(session).get_saved_list(db_user.id, row_id)
                if db_user else None
            )
            if row is None:
                await query.edit_message_text(
                    "This list isn't in your saved lists anymore.",
                    reply_markup=review_results_keyboard(None),
                )
                return
            params = row.parameters or {}
            fmt = params.get("format", "pairs")
            items = params["items"]
            difficulty = Difficulty(row.difficulty)
            label = format_when(row.completed_at, tz_offset_hours(db_user.preferences))

        if fmt == "list":
            pairs = list(items)
            quiz_items = _build_list_quiz_items(pairs)
        else:
            pairs = [tuple(p) for p in items]
            quiz_items = _build_quiz_items(pairs)

        cancel_question_timer(context, user_id)
        cancel_study_timer(context, user_id)
        await cleanup_bot_messages(context.bot, chat_id, state)
        clear_user_state(context)
        set_user_state(context, "current_exercise", "word_memo")
        set_user_state(context, "test_active", True)
        set_user_state(context, "test_exercise_type", "word_memo")
        set_user_state(context, "test_pairs", pairs)
        set_user_state(context, "test_format", fmt)
        set_user_state(context, "test_quiz_items", quiz_items)
        set_user_state(context, "test_current_index", 0)
        set_user_state(context, "test_results", [])
        set_user_state(context, "test_difficulty", difficulty)
        set_user_state(context, "test_chat_id", chat_id)
        set_user_state(context, "baseline_results", [])
        set_user_state(context, "test_round_mode", "review")
        set_user_state(context, "speed_mode", False)
        set_user_state(context, "review_source_id", row_id)
        set_user_state(context, "review_label", label)
        state = get_user_state(context)
        mark_round_start(state)
    finally:
        _starting.discard(user_id)

    how = (
        "Recall the list in order, starting with the 1st word."
        if fmt == "list"
        else "I show one word of each pair — you recall its partner."
    )
    await query.edit_message_text(
        f"📚 *Review — {len(pairs)} {_unit(fmt)}*\n\n"
        f"Saved {label}. No study phase: answer from memory.\n"
        f"{how}\n"
        f"Each question has *{QUESTION_TIME_LIMIT}s*.",
        parse_mode=ParseMode.MARKDOWN,
    )
    track_bot_message(state, query.message.message_id)
    await asyncio.sleep(1.0)
    await send_next_question(context, chat_id, get_user_state(context), user_id)
