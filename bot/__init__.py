"""
Bot initialization and setup.
"""

import logging
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    TypeHandler,
    filters,
)

from config import get_settings
from database import init_db, close_db, get_session, UserRepository
from .commands import (
    start_command,
    help_command,
    stats_command,
    history_command,
    exercises_command,
    achievements_command,
    leaderboard_command,
    level_command,
    settings_command,
)
from .handlers import callback_handler, error_handler, text_message_handler
from .admin import admin_command
from .analytics import activity_tracker, schedule_retention_job
from .features import load_feature_flags
from .menu import sync_command_menu
from .redeem import redeem_command
from .saved_lists import lists_command
from .reminders import schedule_reminder_job
from .version import get_build_info

logger = logging.getLogger(__name__)


def setup_handlers(application: Application) -> None:
    """Register all handlers with the application."""
    # Group -1 runs before every feature handler and never blocks: the tracker
    # queues its insert and returns. Removing analytics = delete this line.
    application.add_handler(TypeHandler(Update, activity_tracker), group=-1)
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("history", history_command))
    application.add_handler(CommandHandler("exercises", exercises_command))
    application.add_handler(CommandHandler("achievements", achievements_command))
    application.add_handler(CommandHandler("leaderboard", leaderboard_command))
    application.add_handler(CommandHandler("level", level_command))
    application.add_handler(CommandHandler("settings", settings_command))
    application.add_handler(CommandHandler("redeem", redeem_command))
    application.add_handler(CommandHandler("lists", lists_command))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CallbackQueryHandler(callback_handler))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, text_message_handler)
    )
    application.add_error_handler(error_handler)
    logger.info("Handlers registered successfully")


async def on_startup(application: Application) -> None:
    build = get_build_info()
    logger.info(
        f"Starting Mental Training Bot — build {build.short} "
        f"({build.date}: {build.subject}){' [DIRTY TREE]' if build.dirty else ''}"
    )
    await init_db()
    logger.info("Database initialized")
    await load_feature_flags()
    await _remove_admins_from_leaderboard()
    await sync_command_menu(application.bot)
    schedule_reminder_job(application)
    schedule_retention_job(application)
    bot_info = await application.bot.get_me()
    logger.info(f"Bot started: @{bot_info.username}")


async def _remove_admins_from_leaderboard() -> None:
    """Admins never compete with members: the leaderboard query excludes their
    IDs, and this startup sweep clears any opt-in flag set before that rule
    (or before an ID was added to ADMIN_TELEGRAM_IDS)."""
    async with get_session() as session:
        user_repo = UserRepository(session)
        for admin_id in get_settings().admin_ids:
            user = await user_repo.get_by_telegram_id(admin_id)
            if user and user.leaderboard_opt_in:
                await user_repo.set_leaderboard_opt_in(admin_id, False)
                logger.info(f"Removed admin {admin_id} from the leaderboard")


async def on_shutdown(application: Application) -> None:
    logger.info("Shutting down Mental Training Bot...")
    await close_db()
    logger.info("Database connections closed")


def create_application() -> Application:
    settings = get_settings()
    application = (
        Application.builder()
        .token(settings.telegram_bot_token)
        # Process updates concurrently — without this, PTB handles updates
        # one at a time and any await (e.g. the 2s answer-grace sleep) blocks
        # EVERY user. Required for multi-user scale.
        .concurrent_updates(True)
        .post_init(on_startup)
        .post_shutdown(on_shutdown)
        .build()
    )
    setup_handlers(application)
    return application
