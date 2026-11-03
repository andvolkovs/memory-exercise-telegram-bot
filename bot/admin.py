"""
Admin commands — usable only by Telegram IDs in ADMIN_TELEGRAM_IDS.

/admin           — bot-wide overview
/admin users     — per-user progress table
/admin time [d]  — engaged training minutes per user (bot/analytics.py)
/admin export    — CSV of all test sessions (for spreadsheets / dashboard)
/admin grant <telegram_id> <free|basic|premium> [days]
/admin version   — running commit, uptime, dirty-tree warning (bot/version.py)
/admin changes [n] — last n commit subjects of the running build
"""

import csv
import io
import logging
from datetime import timedelta

from telegram import Update
from telegram.ext import ContextTypes

from database import (
    get_session, UserRepository, ExerciseSessionRepository, SubscriptionTier,
    ActivityEventRepository,
)
from database.models import utcnow
from .access import admin_only
from .features import (
    is_xp_enabled, set_xp_enabled, is_flag_enabled, set_flag,
    AUDIO_VIZ_ENABLED_KEY, AUDIO_VIZ_QUIZ_ENABLED_KEY, AUDIO_XP_ENABLED_KEY,
    REMINDERS_ENABLED_KEY, SPRINT_ENABLED_KEY, ANALYTICS_ENABLED_KEY,
    SAVED_LISTS_ENABLED_KEY,
)
from .menu import sync_command_menu
from .redeem import admin_codes
from .version import format_version, format_changes, get_build_info, DEFAULT_CHANGES

logger = logging.getLogger(__name__)


@admin_only
async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    sub = args[0].lower() if args else ""

    if sub == "users":
        await _admin_users(update)
    elif sub == "export":
        await _admin_export(update)
    elif sub == "grant":
        await _admin_grant(update, args[1:])
    elif sub == "xp":
        await _admin_xp(update, args[1:])
    elif sub == "audio":
        await _admin_flag(update, args[1:], AUDIO_VIZ_ENABLED_KEY,
                          "🎧 Audio Visualization exercise", "/admin audio on|off")
    elif sub == "audioquiz":
        await _admin_flag(update, args[1:], AUDIO_VIZ_QUIZ_ENABLED_KEY,
                          "🧠 Audio detail quiz", "/admin audioquiz on|off")
    elif sub == "audioxp":
        await _admin_flag(update, args[1:], AUDIO_XP_ENABLED_KEY,
                          "👁 Audio XP (visualization bar)", "/admin audioxp on|off")
    elif sub == "codes":
        await admin_codes(update, args[1:])
    elif sub == "reminders":
        await _admin_flag(update, args[1:], REMINDERS_ENABLED_KEY,
                          "🔔 Daily reminders", "/admin reminders on|off")
    elif sub == "sprint":
        await _admin_flag(update, args[1:], SPRINT_ENABLED_KEY,
                          "🏁 Daily sprint challenge", "/admin sprint on|off")
    elif sub == "lists":
        await _admin_flag(update, args[1:], SAVED_LISTS_ENABLED_KEY,
                          "📚 Saved lists", "/admin lists on|off")
    elif sub == "analytics":
        await _admin_flag(update, args[1:], ANALYTICS_ENABLED_KEY,
                          "📈 Usage analytics", "/admin analytics on|off")
    elif sub == "time":
        await _admin_time(update, args[1:])
    elif sub == "version":
        await update.message.reply_text(format_version())
    elif sub == "changes":
        try:
            n = int(args[1]) if len(args) > 1 else DEFAULT_CHANGES
        except ValueError:
            await update.message.reply_text("Usage: /admin changes [n]")
            return
        await update.message.reply_text(format_changes(n))
    else:
        await _admin_overview(update)


async def _admin_overview(update: Update) -> None:
    async with get_session() as session:
        stats = await ExerciseSessionRepository(session).get_global_stats()

    xp_status = "ON ✅" if is_xp_enabled() else "OFF ⛔"
    audio_status = "ON ✅" if is_flag_enabled(AUDIO_VIZ_ENABLED_KEY) else "OFF ⛔"
    audio_quiz_status = "ON ✅" if is_flag_enabled(AUDIO_VIZ_QUIZ_ENABLED_KEY) else "OFF ⛔"
    audio_xp_status = "ON ✅" if is_flag_enabled(AUDIO_XP_ENABLED_KEY) else "OFF ⛔"
    reminders_status = "ON ✅" if is_flag_enabled(REMINDERS_ENABLED_KEY) else "OFF ⛔"
    sprint_status = "ON ✅" if is_flag_enabled(SPRINT_ENABLED_KEY) else "OFF ⛔"
    analytics_status = "ON ✅" if is_flag_enabled(ANALYTICS_ENABLED_KEY) else "OFF ⛔"
    lists_status = "ON ✅" if is_flag_enabled(SAVED_LISTS_ENABLED_KEY) else "OFF ⛔"
    build = get_build_info()
    build_line = f"🏷 Build: {build.short} ({build.date}){' ⚠️ local edits' if build.dirty else ''}"
    text = (
        "🛠 Admin — Bot Overview\n\n"
        f"{build_line}\n"
        f"👥 Users: {stats['total_users']} total, +{stats['new_users_week']} this week\n"
        f"🟢 Active: {stats['active_day']} today, {stats['active_week']} this week\n"
        f"🎯 Tests: {stats['tests_total']} total, {stats['tests_week']} this week\n"
        f"📊 Avg score (all users): {stats['avg_score']:.0f}%\n"
        f"⭐ XP system: {xp_status}\n"
        f"🎧 Audio visualization: {audio_status} (quiz: {audio_quiz_status}, XP: {audio_xp_status})\n"
        f"🔔 Daily reminders: {reminders_status}\n"
        f"🏁 Daily sprint: {sprint_status}\n"
        f"📚 Saved lists: {lists_status}\n"
        f"📈 Usage analytics: {analytics_status}\n\n"
        "Commands:\n"
        "/admin users — per-user progress\n"
        "/admin time [days] — training minutes per user\n"
        "/admin export — CSV of all sessions\n"
        "/admin grant <id> <tier> [days] — set subscription\n"
        "/admin codes <n> <tier> <days|lifetime> — generate access codes\n"
        "/admin codes list — unredeemed codes\n"
        "/admin xp on|off — toggle the XP/level system\n"
        "/admin audio on|off — toggle the audio exercise\n"
        "/admin audioquiz on|off — offer the detail quiz after audio\n"
        "/admin audioxp on|off — audio XP (visualization bar)\n"
        "/admin reminders on|off — toggle daily reminders\n"
        "/admin sprint on|off — toggle the daily sprint challenge\n"
        "/admin lists on|off — toggle saved lists + review\n"
        "/admin analytics on|off — toggle the raw interaction log\n"
        "/admin version — running commit + uptime\n"
        "/admin changes [n] — recent commits of the running build"
    )
    await update.message.reply_text(text)


async def _admin_xp(update: Update, args: list[str]) -> None:
    if not args or args[0].lower() not in ("on", "off"):
        status = "ON ✅" if is_xp_enabled() else "OFF ⛔"
        await update.message.reply_text(
            f"XP system is {status}\nUsage: /admin xp on|off"
        )
        return
    enabled = args[0].lower() == "on"
    await set_xp_enabled(enabled)
    # Keep the command menu in sync (/level shown only while XP is on)
    await sync_command_menu(update.get_bot())
    if enabled:
        await update.message.reply_text(
            "⭐ XP system ON — users see XP gains, levels and /level again."
        )
    else:
        await update.message.reply_text(
            "⛔ XP system OFF — no XP is calculated or shown anywhere. "
            "Existing XP is kept and comes back when you turn it on."
        )


async def _admin_flag(update: Update, args: list[str], key: str, label: str, usage: str) -> None:
    """Generic on/off toggle for a runtime feature flag."""
    if not args or args[0].lower() not in ("on", "off"):
        status = "ON ✅" if is_flag_enabled(key) else "OFF ⛔"
        await update.message.reply_text(f"{label} is {status}\nUsage: {usage}")
        return
    enabled = args[0].lower() == "on"
    await set_flag(key, enabled)
    await update.message.reply_text(
        f"{label} is now {'ON ✅' if enabled else 'OFF ⛔'}"
    )


async def _admin_users(update: Update) -> None:
    async with get_session() as session:
        users = await ExerciseSessionRepository(session).get_users_progress(limit=30)

    if not users:
        await update.message.reply_text("No users yet.")
        return

    lines = ["👥 Users (last active first, top 30)\n"]
    for u in users:
        last = u["last_active"].strftime("%b %d") if u["last_active"] else "?"
        if u["tests"]:
            scores = f"{u['tests']} tests, avg {u['avg_pct']:.0f}%, best {u['best_pct']:.0f}%"
        else:
            scores = "no tests"
        streak = f", 🔥{u['streak']}" if u["streak"] else ""
        lines.append(f"• {u['name']} ({u['telegram_id']}) — {scores}{streak} — {last}")

    # Telegram message limit is 4096 chars
    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "\n…"
    await update.message.reply_text(text)


def _fmt_duration(seconds: float) -> str:
    """Seconds under an hour stay readable as m/s — rounding a 45s round and a
    90s round both to "1 min" hides the differences worth looking at."""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        m, s = divmod(seconds, 60)
        return f"{m}m {s}s" if s else f"{m}m"
    h, rem = divmod(seconds, 3600)
    return f"{h}h {rem // 60}m"


async def _admin_time(update: Update, args: list[str]) -> None:
    """Engaged training minutes per user. `/admin time [days]` (default 7)."""
    try:
        days = int(args[0]) if args else 7
    except ValueError:
        await update.message.reply_text("Usage: /admin time [days]")
        return
    days = max(1, min(days, 365))
    since = utcnow() - timedelta(days=days)

    async with get_session() as session:
        rows = await ExerciseSessionRepository(session).get_training_time(
            since=since, limit=30,
        )
        events = await ActivityEventRepository(session).get_event_counts(since)

    if not rows:
        await update.message.reply_text(
            f"No training time recorded in the last {days} days.\n"
            "(Rounds finished before this feature shipped have no duration.)"
        )
        return

    total = sum(r["seconds"] for r in rows)
    lines = [
        f"⏱ Training time — last {days} days\n",
        f"Total: {_fmt_duration(total)} across {len(rows)} "
        f"user{'s' if len(rows) != 1 else ''}\n",
    ]
    for r in rows:
        avg = f", avg {r['avg_pct']:.0f}%" if r["avg_pct"] is not None else ""
        rounds = f"{r['rounds']} round{'s' if r['rounds'] != 1 else ''}"
        lines.append(
            f"• {r['name']} — {_fmt_duration(r['seconds'])}, {rounds}{avg}"
        )
    if events:
        summary = ", ".join(f"{kind} {count}" for kind, count in events)
        lines.append(f"\nInteractions: {summary}")
    lines.append(
        "\nTraining time only — study + quiz + listening. "
        "Menu browsing is not counted."
    )

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "\n…"
    await update.message.reply_text(text)


async def _admin_export(update: Update) -> None:
    async with get_session() as session:
        rows = await ExerciseSessionRepository(session).get_sessions_for_export()

    if not rows:
        await update.message.reply_text("No test sessions to export yet.")
        return

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)

    filename = f"sessions_{utcnow().strftime('%Y%m%d_%H%M')}.csv"
    await update.message.reply_document(
        document=io.BytesIO(buf.getvalue().encode("utf-8-sig")),
        filename=filename,
        caption=f"📄 {len(rows)} test sessions",
    )


async def _admin_grant(update: Update, args: list[str]) -> None:
    usage = "Usage: /admin grant <telegram_id> <free|basic|premium> [days]"
    if len(args) < 2:
        await update.message.reply_text(usage)
        return
    try:
        target_id = int(args[0])
        tier = SubscriptionTier(args[1].lower())
        days = int(args[2]) if len(args) > 2 else None
    except (ValueError, KeyError):
        await update.message.reply_text(usage)
        return

    expires_at = utcnow() + timedelta(days=days) if days else None
    async with get_session() as session:
        user = await UserRepository(session).set_subscription(target_id, tier, expires_at)

    if not user:
        await update.message.reply_text(f"User {target_id} not found.")
        return
    until = f" until {expires_at.strftime('%Y-%m-%d')}" if expires_at else " (no expiry)"
    await update.message.reply_text(
        f"✅ {user.first_name or target_id} → {tier.value}{until}"
    )
