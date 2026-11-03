"""
Telegram command menu (the blue "Menu" button next to the input field).

Synced at startup and after /admin xp toggles, so the menu always matches
what's actually available: /level appears only while XP is enabled, /admin
only in admins' own chats.
"""

import logging

from telegram import Bot, BotCommand, BotCommandScopeChat

from config import get_settings
from .features import is_xp_enabled

logger = logging.getLogger(__name__)


def _user_commands() -> list[BotCommand]:
    commands = [
        BotCommand("start", "Main menu"),
        BotCommand("exercises", "Choose an exercise"),
        BotCommand("stats", "Your statistics"),
        BotCommand("history", "Last 10 test results"),
        BotCommand("lists", "Saved word lists — review"),
    ]
    if is_xp_enabled():
        commands.append(BotCommand("level", "Your XP and levels"))
    commands += [
        BotCommand("achievements", "Your achievements"),
        BotCommand("leaderboard", "Leaderboard (opt-in)"),
        BotCommand("settings", "Preferences"),
        BotCommand("redeem", "Activate an access code"),
        BotCommand("help", "How the bot works"),
    ]
    return commands


async def sync_command_menu(bot: Bot) -> None:
    commands = _user_commands()
    await bot.set_my_commands(commands)

    admin_commands = commands + [BotCommand("admin", "Admin panel")]
    for admin_id in get_settings().admin_ids:
        try:
            await bot.set_my_commands(
                admin_commands, scope=BotCommandScopeChat(chat_id=admin_id)
            )
        except Exception as e:
            # Fails if the admin has never opened a chat with the bot — harmless.
            logger.warning(f"Could not set admin menu for {admin_id}: {e}")

    logger.info(f"Command menu synced ({len(commands)} user commands)")
