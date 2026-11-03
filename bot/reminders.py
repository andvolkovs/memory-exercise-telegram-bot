"""
Daily training reminder + fresh-mind XP bonus.

Opt-in per user via /settings: pick a local hour, the bot pings once a day
with a one-tap button that starts a test with the user's last-used settings.
Starting within FRESH_MIND_WINDOW_MIN of the ping pays a ×FRESH_MIND_MULT
XP bonus, once per day. Users who already trained today are never pinged.

Timezone handling: Telegram exposes no user timezone, so setup asks "what
time is it for you right now?" and stores the derived UTC hour. Half-hour
timezones (e.g. India) land up to 30 min off — accepted until the Postgres
migration brings proper tz storage.

Prefs schema (user.preferences["reminder"]):
    {"enabled": bool, "hour_local": int, "utc_hour": int,
     "last_ping": iso-datetime, "bonus_date": iso-date}

REMOVAL: self-contained module. Delete it plus these touch points:
schedule_reminder_job call + import (bot/__init__.py), "rem" route
(bot/handlers.py), reminder_settings_rows/text calls (bot/commands.py),
the fresh-mind block in bot/quiz_engine.py (marked with "Fresh-mind bonus"),
get_users_due_reminder (database/repositories.py), REMINDERS_ENABLED_KEY
(bot/features.py), "reminders" admin subcommand (bot/admin.py). Or just
/admin reminders off to pause everything without touching code.
"""

import logging
from datetime import datetime, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import Application, ContextTypes

from database import get_session, UserRepository, ExerciseSessionRepository
from database.models import utcnow
from exercises.base import Difficulty
from exercises.word_memorization import DIFF_EMOJI, FORMAT_UNITS
from .features import is_flag_enabled, is_xp_enabled, REMINDERS_ENABLED_KEY
from .state import (
    get_user_state, set_user_state, clear_user_state, cleanup_bot_messages,
)
from .word_memo import generate_word_memo_test

logger = logging.getLogger(__name__)

FRESH_MIND_WINDOW_MIN = 15
FRESH_MIND_MULT = 1.25

_DEFAULT_PRESET = {
    "difficulty": "intermediate", "count": 10, "format": "pairs", "speed": False,
}


# ============================================================================
# Hourly job
# ============================================================================

def schedule_reminder_job(application: Application) -> None:
    """Register the hourly sweep, aligned to the top of the hour."""
    now = utcnow()
    first = (59 - now.minute) * 60 + (60 - now.second) + 5
    application.job_queue.run_repeating(
        send_due_reminders, interval=3600, first=first, name="daily_reminders",
    )
    logger.info(f"Reminder job scheduled (first run in {first}s)")


async def send_due_reminders(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_flag_enabled(REMINDERS_ENABLED_KEY):
        return
    utc_hour = utcnow().hour
    now_iso = utcnow().isoformat()

    pings = []  # (telegram_id, text, keyboard) built inside the DB session
    async with get_session() as session:
        user_repo = UserRepository(session)
        session_repo = ExerciseSessionRepository(session)
        due = await user_repo.get_users_due_reminder(utc_hour)
        for u in due:
            preset = await _get_preset(session_repo, u)
            pings.append((u.telegram_id, *_build_ping(preset)))
            rem = dict((u.preferences or {}).get("reminder", {}))
            rem["last_ping"] = now_iso
            await user_repo.update_preferences(u.telegram_id, {"reminder": rem})

    for telegram_id, text, keyboard in pings:
        try:
            await context.bot.send_message(
                chat_id=telegram_id, text=text,
                parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard,
            )
        except Exception as e:
            # Blocked bot / deleted account — skip silently, ping tomorrow.
            logger.warning(f"Reminder to {telegram_id} failed: {e}")
    if pings:
        logger.info(f"Sent {len(pings)} daily reminders (utc hour {utc_hour})")


async def _get_preset(session_repo: ExerciseSessionRepository, db_user) -> dict:
    """Last-used test settings; falls back to placement rec, then defaults."""
    history = await session_repo.get_recent_test_history(db_user.id, limit=1)
    if history:
        h = history[0]
        if isinstance(h["count"], int):
            return {
                "difficulty": h["difficulty"], "count": h["count"],
                "format": h["format"], "speed": h.get("speed", False),
            }
    placement = (db_user.preferences or {}).get("placement")
    if placement:
        return {**_DEFAULT_PRESET, "difficulty": placement["level"],
                "count": placement["count"]}
    return dict(_DEFAULT_PRESET)


def _build_ping(preset: dict) -> tuple[str, InlineKeyboardMarkup]:
    unit = FORMAT_UNITS.get(preset["format"], "pairs")
    emoji = DIFF_EMOJI.get(preset["difficulty"], "⚪")
    speed_tag = " ⚡" if preset["speed"] else ""
    text = "🔔 *Daily training time!*\n"
    if is_xp_enabled():
        text += (
            f"Start within {FRESH_MIND_WINDOW_MIN} minutes for the "
            f"⚡ *fresh-mind bonus* (×{FRESH_MIND_MULT} XP)."
        )
    else:
        text += "A few minutes now keeps your streak alive."
    go_data = (
        f"rem:go:{preset['difficulty']}:{preset['count']}"
        f":{preset['format']}:{1 if preset['speed'] else 0}"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"🚀 Start: {emoji} {preset['count']} {unit}{speed_tag}",
            callback_data=go_data,
        )],
        [InlineKeyboardButton("😴 Not today", callback_data="rem:dismiss")],
    ])
    return text, keyboard


# ============================================================================
# Fresh-mind bonus (consumed by bot/quiz_engine.py)
# ============================================================================

def _within_window(last_ping_iso: str | None) -> bool:
    if not last_ping_iso:
        return False
    try:
        ping = datetime.fromisoformat(last_ping_iso)
    except ValueError:
        return False
    return utcnow() - ping <= timedelta(minutes=FRESH_MIND_WINDOW_MIN)


async def claim_fresh_mind_bonus(user_repo, db_user, base_xp: int) -> int:
    """Award the bonus XP for a reminder-started test. Persists bonus_date
    so it pays at most once per day even across restarts. Returns bonus XP."""
    prefs = db_user.preferences or {}
    rem = dict(prefs.get("reminder", {}))
    today = utcnow().date().isoformat()
    if rem.get("bonus_date") == today:
        return 0
    rem["bonus_date"] = today
    await user_repo.update_preferences(db_user.telegram_id, {"reminder": rem})
    return round(base_xp * (FRESH_MIND_MULT - 1.0))


# ============================================================================
# Settings integration (called from bot/commands.py)
# ============================================================================

def reminder_settings_text(preferences: dict) -> str:
    if not is_flag_enabled(REMINDERS_ENABLED_KEY):
        return ""
    rem = preferences.get("reminder", {})
    if rem.get("enabled"):
        status = f"*{rem.get('hour_local', 8):02d}:00 daily ✅*"
    else:
        status = "*Off*"
    return (
        "\n\n🔔 *Daily reminder*\n"
        "_A daily ping with a one-tap test at your level. Start within "
        f"{FRESH_MIND_WINDOW_MIN} min → ×{FRESH_MIND_MULT} XP._\n"
        f"Currently: {status}"
    )


def reminder_settings_rows(preferences: dict) -> list[list[InlineKeyboardButton]]:
    if not is_flag_enabled(REMINDERS_ENABLED_KEY):
        return []
    rem = preferences.get("reminder", {})
    if rem.get("enabled"):
        return [[
            InlineKeyboardButton(
                f"🔔 Change time ({rem.get('hour_local', 8):02d}:00)",
                callback_data="rem:setup",
            ),
            InlineKeyboardButton("🔕 Turn off", callback_data="rem:off"),
        ]]
    return [[InlineKeyboardButton(
        "🔔 Daily reminder: ⬜ Off", callback_data="rem:setup",
    )]]


# ============================================================================
# Callbacks (rem: prefix)
# ============================================================================

def _hour_grid(prefix: str) -> InlineKeyboardMarkup:
    rows = []
    for start in range(0, 24, 6):
        rows.append([
            InlineKeyboardButton(f"{h:02d}", callback_data=f"{prefix}:{h}")
            for h in range(start, start + 6)
        ])
    return InlineKeyboardMarkup(rows)


async def _rerender_settings(query, telegram_id: int) -> None:
    # Lazy import — commands.py imports this module, top-level would cycle.
    from .commands import _settings_text_and_keyboard
    async with get_session() as session:
        db_user = await UserRepository(session).get_by_telegram_id(telegram_id)
        prefs = (db_user.preferences or {}) if db_user else {}
    text, kb = _settings_text_and_keyboard(prefs)
    await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)


async def handle_reminder_callback(query, context, data: str) -> None:
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else None
    user = query.from_user

    if not is_flag_enabled(REMINDERS_ENABLED_KEY) and action != "dismiss":
        await query.edit_message_text("Reminders are paused right now.")
        return

    if action == "setup":
        await query.edit_message_text(
            "🕐 *Step 1 of 2*\n\nWhat time is it for you *right now*?\n"
            "_(Just the hour — this tells me your timezone.)_",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_hour_grid("rem:now"),
        )

    elif action == "now":
        local_now = int(parts[2])
        offset = (local_now - utcnow().hour) % 24
        await query.edit_message_text(
            "🔔 *Step 2 of 2*\n\nWhen should I remind you every day?",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_hour_grid(f"rem:set:{offset}"),
        )

    elif action == "set":
        offset = int(parts[2])
        hour_local = int(parts[3])
        utc_hour = (hour_local - offset) % 24
        async with get_session() as session:
            user_repo = UserRepository(session)
            db_user, _ = await user_repo.get_or_create(
                telegram_id=user.id, username=user.username,
                first_name=user.first_name, last_name=user.last_name,
            )
            rem = dict((db_user.preferences or {}).get("reminder", {}))
            rem.update(enabled=True, hour_local=hour_local, utc_hour=utc_hour)
            await user_repo.update_preferences(user.id, {"reminder": rem})
        await query.edit_message_text(
            f"✅ Daily reminder set for *{hour_local:02d}:00*.\n\n"
            "You'll get a ping with a one-tap test — no ping on days you "
            "already trained. Change or turn off anytime in /settings.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu"),
            ]]),
        )

    elif action == "off":
        async with get_session() as session:
            user_repo = UserRepository(session)
            db_user = await user_repo.get_by_telegram_id(user.id)
            if db_user:
                rem = dict((db_user.preferences or {}).get("reminder", {}))
                rem["enabled"] = False
                await user_repo.update_preferences(user.id, {"reminder": rem})
        await _rerender_settings(query, user.id)

    elif action == "go":
        # rem:go:<difficulty>:<count>:<format>:<speed> — one-tap preset test.
        difficulty = Difficulty(parts[2])
        count = int(parts[3])
        fmt = parts[4]
        speed = parts[5] == "1"

        async with get_session() as session:
            db_user = await UserRepository(session).get_by_telegram_id(user.id)
            prefs = (db_user.preferences or {}) if db_user else {}
        rem = prefs.get("reminder", {})
        eligible = (
            _within_window(rem.get("last_ping"))
            and rem.get("bonus_date") != utcnow().date().isoformat()
        )

        state = get_user_state(context)
        await cleanup_bot_messages(context.bot, query.message.chat_id, state)
        clear_user_state(context)
        set_user_state(context, "current_exercise", "word_memo")
        set_user_state(context, "mode", "test")
        set_user_state(context, "format", fmt)
        set_user_state(context, "speed_mode", speed)
        set_user_state(context, "difficulty", difficulty)
        set_user_state(context, "count", count)
        if eligible:
            set_user_state(context, "fresh_mind_pending", True)
        await generate_word_memo_test(query, context, difficulty, count)

    elif action == "dismiss":
        await query.edit_message_text("👍 No worries — see you tomorrow!")
