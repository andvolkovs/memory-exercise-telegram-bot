"""
Runtime feature flags — DB-persisted, cached in memory.

The cache means zero per-message DB cost: flags load once at startup and are
updated in place when an admin toggles them. Safe because the bot is a single
process; revisit if it ever runs multiple workers.
"""

import logging

from database import get_session, BotSettingsRepository

logger = logging.getLogger(__name__)

XP_ENABLED_KEY = "xp_enabled"
AUDIO_VIZ_ENABLED_KEY = "audio_viz_enabled"
AUDIO_VIZ_QUIZ_ENABLED_KEY = "audio_viz_quiz_enabled"
AUDIO_XP_ENABLED_KEY = "audio_xp_enabled"
REMINDERS_ENABLED_KEY = "reminders_enabled"
SPRINT_ENABLED_KEY = "sprint_enabled"
ANALYTICS_ENABLED_KEY = "analytics_enabled"
SAVED_LISTS_ENABLED_KEY = "saved_lists_enabled"

# Defaults apply when the key has never been set in the DB.
# Audio visualization ships dark: admin flips it on when content is ready.
# Reminders default on — the feature is opt-in per user anyway; the flag is
# the kill switch (/admin reminders off hides setup and stops all pings).
# Sprint default on — cosmetic results line, kill switch /admin sprint off.
# Audio XP default on — moot until the audio exercise itself is switched on;
# kill switch /admin audioxp off (see gamification/audio_xp.py).
# Analytics default on — gates only the raw interaction log (bot/analytics.py);
# per-round training time is stored either way.
# Saved lists default on — kill switch /admin lists off hides the panel,
# the save buttons and review starts (bot/saved_lists.py). Lists are still
# recorded while off, so switching back on loses nothing.
_flags: dict[str, bool] = {
    XP_ENABLED_KEY: True,
    AUDIO_VIZ_ENABLED_KEY: False,
    AUDIO_VIZ_QUIZ_ENABLED_KEY: False,
    AUDIO_XP_ENABLED_KEY: True,
    REMINDERS_ENABLED_KEY: True,
    SPRINT_ENABLED_KEY: True,
    ANALYTICS_ENABLED_KEY: True,
    SAVED_LISTS_ENABLED_KEY: True,
}


async def load_feature_flags() -> None:
    """Call once at startup."""
    async with get_session() as session:
        repo = BotSettingsRepository(session)
        for key in _flags:
            value = await repo.get(key)
            if value is not None:
                _flags[key] = value == "1"
    logger.info(f"Feature flags loaded: {_flags}")


def is_flag_enabled(key: str) -> bool:
    return _flags.get(key, False)


async def set_flag(key: str, enabled: bool) -> None:
    async with get_session() as session:
        await BotSettingsRepository(session).set(key, "1" if enabled else "0")
    _flags[key] = enabled


def is_exercise_enabled(feature_flag: str | None) -> bool:
    """Exercises with no feature_flag are always on; flagged ones follow the flag."""
    if feature_flag is None:
        return True
    return is_flag_enabled(feature_flag)


def is_xp_enabled() -> bool:
    return _flags[XP_ENABLED_KEY]


async def set_xp_enabled(enabled: bool) -> None:
    await set_flag(XP_ENABLED_KEY, enabled)
