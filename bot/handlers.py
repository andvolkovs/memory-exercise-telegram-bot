"""
Callback and text-message routing.

Thin layer: every callback_data "<prefix>:..." is dispatched through
CALLBACK_ROUTES; typed text goes to the quiz engine during tests. The actual
feature logic lives in commands.py (slash commands + menu/lb/settings),
word_memo.py, audio_viz.py and quiz_engine.py.
"""

import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, Forbidden, NetworkError
from telegram.ext import ContextTypes
from telegram.constants import ParseMode

from exercises import ExerciseRegistry
from .audio_viz import handle_audio_viz_callback
from .commands import (
    get_main_menu_keyboard,
    handle_leaderboard_callback,
    handle_menu_callback,
    handle_settings_callback,
)
from .features import is_exercise_enabled
from .quiz_engine import cancel_question_timer, cancel_study_timer, record_answer
from .state import (
    get_user_state, set_user_state, clear_user_state,
    is_in_test_mode, cleanup_bot_messages,
)
from .saved_lists import handle_legacy_recall_callback, handle_lists_callback
from .reminders import handle_reminder_callback
from .word_memo import handle_word_memo_callback, handle_placement_callback

logger = logging.getLogger(__name__)


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data
    logger.debug(f"Callback received: {data}")

    if data == "main_menu":
        await show_main_menu(query, context)
    elif data.startswith("exercise:"):
        await start_exercise(query, context, data.split(":")[1])
    else:
        # Route "<prefix>:..." to the registered handler for that prefix.
        # New exercises: add one entry to CALLBACK_ROUTES (see bottom of file).
        route = CALLBACK_ROUTES.get(data.split(":")[0])
        if route:
            await route(query, context, data)
        else:
            logger.warning(f"Unknown callback: {data}")


async def show_main_menu(query, context) -> None:
    cancel_question_timer(context, query.from_user.id)
    state = get_user_state(context)
    await cleanup_bot_messages(context.bot, query.message.chat_id, state)
    clear_user_state(context)
    await query.edit_message_text(
        "🧠 *Mental Training Bot*\n\nChoose an exercise to begin:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=get_main_menu_keyboard(),
    )


async def start_exercise(query, context, exercise_type: str) -> None:
    exercise = ExerciseRegistry.get(exercise_type)
    if not exercise:
        await query.edit_message_text("Exercise not found.")
        return
    # Stale button on an old message can still reach a paused exercise.
    if not is_exercise_enabled(exercise.feature_flag):
        await query.edit_message_text(
            "This exercise is paused right now. Check back soon!",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")]]
            ),
        )
        return
    # Full reset, same as the main menu: this is reachable mid-test from any
    # old menu message (/start, /exercises). Keeping the state left the quiz
    # half-alive — test_active with no timer, so later typing was eaten as
    # answers — and a pending study countdown still launched the quiz.
    cancel_question_timer(context, query.from_user.id)
    cancel_study_timer(context, query.from_user.id)
    state = get_user_state(context)
    await cleanup_bot_messages(context.bot, query.message.chat_id, state)
    clear_user_state(context)
    set_user_state(context, "current_exercise", exercise_type)
    kb = exercise.get_mode_keyboard()
    await query.edit_message_text(
        exercise.get_intro_message(),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb,
    )


async def text_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    if is_in_test_mode(context):
        try:
            await update.message.delete()
        except Exception:
            pass
        await record_answer(
            context, update.message.chat_id, update.message.text,
            user_id=update.effective_user.id,
            answer_message_id=None,
        )
    else:
        await update.message.reply_text(
            "Use /start to see the main menu, or /help for instructions."
        )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Global PTB error handler: full traceback in the log, one short line to
    the user so a crashed handler doesn't look like a dead button.

    Expected noise is downgraded: users blocking the bot (Forbidden), the
    double-tap "message is not modified" BadRequest, and transient network
    errors. Note BadRequest subclasses NetworkError in PTB, so it is checked
    first.
    """
    err = context.error
    if isinstance(err, Forbidden):
        logger.warning(f"Forbidden (user blocked the bot?): {err}")
        return
    if isinstance(err, BadRequest) and "not modified" in str(err).lower():
        return
    if isinstance(err, NetworkError) and not isinstance(err, BadRequest):
        logger.warning(f"Telegram network error: {err}")
        return

    user_id = "?"
    chat_id = None
    if isinstance(update, Update):
        if update.effective_user:
            user_id = update.effective_user.id
        if update.effective_chat:
            chat_id = update.effective_chat.id
    logger.error(f"Unhandled error (user {user_id}): {err!r}", exc_info=err)

    if chat_id is not None:
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text="⚠️ Something went wrong on my side. Use /start to continue.",
            )
        except Exception:
            pass


# Callback routing table: prefix of callback_data -> handler(query, context, data).
# To add a new exercise flow, register its prefix here (usually the
# exercise_type string used in its keyboards).
CALLBACK_ROUTES = {
    "word_memo": handle_word_memo_callback,
    "audio_viz": handle_audio_viz_callback,
    "lb": handle_leaderboard_callback,
    "menu": handle_menu_callback,
    "settings": handle_settings_callback,
    "placement": handle_placement_callback,
    "rem": handle_reminder_callback,
    "lists": handle_lists_callback,
    # Retired Oct 2026 build — old buttons still in chats. Remove ~2027.
    "recall": handle_legacy_recall_callback,
}
