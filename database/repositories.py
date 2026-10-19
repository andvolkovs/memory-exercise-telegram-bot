"""
Repository pattern for database operations.
Provides clean interface for CRUD operations on models.

Stats/leaderboard queries aggregate in SQL (not Python loops) so they stay
fast as the session table grows.
"""

import secrets
from datetime import datetime, timedelta
from typing import Iterable, Optional
from sqlalchemy import select, update, func, and_, or_, case
from sqlalchemy.ext.asyncio import AsyncSession

from .models import (
    User, ExerciseSession, ExerciseType, SubscriptionTier, UserAchievement,
    UserSkill, BotSetting, RedemptionCode, ActivityEvent, utcnow,
)

# Score percentage expression reused across queries.
_PCT = ExerciseSession.score * 100.0 / ExerciseSession.max_score

# Quiz round type stored in parameters JSON:
# "test" | "reverse" | "reverse_extra" | "retry" | "placement" | "recall" |
# "audio_listen" | "audio_quiz".
_ROUND_MODE = ExerciseSession.parameters["mode"].as_string()

# Any session that produced a score (includes retry-mistakes rounds).
_HAS_SCORE = and_(
    ExerciseSession.max_score.isnot(None),
    ExerciseSession.max_score > 0,
)

# Filter for real tests: scored, and neither a retry-mistakes round (practice
# on a subset of pairs — would inflate test counts and average scores) nor a
# placement round (one-off calibration for new users) nor an audio detail
# quiz (different exercise, tiny question counts — would skew word-memo
# averages) nor an extra reverse round (2nd+ reverse on one memorized set —
# repeat practice that would let a single set farm the leaderboard average)
# nor a delayed-recall round (a 5–10 question sample of an older test —
# different task, kept off the leaderboard until it has its own board).
# The FIRST reverse quiz DOES count — it tests the full set, column-flipped.
# Mirrored in pandas as dashboard.py UNSCORED_MODES — change both.
_IS_SCORED_TEST = and_(
    _HAS_SCORE,
    or_(
        _ROUND_MODE.is_(None),
        _ROUND_MODE.notin_(
            ("retry", "placement", "audio_quiz", "reverse_extra", "recall")
        ),
    ),
)


class UserRepository:
    """Repository for User operations."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_by_telegram_id(self, telegram_id: int) -> Optional[User]:
        result = await self.session.execute(
            select(User).where(User.telegram_id == telegram_id)
        )
        return result.scalar_one_or_none()

    async def create(
        self, telegram_id: int, username=None, first_name=None,
        last_name=None, language_code: str = "en",
    ) -> User:
        user = User(
            telegram_id=telegram_id, username=username,
            first_name=first_name, last_name=last_name,
            language_code=language_code,
        )
        self.session.add(user)
        await self.session.flush()
        return user

    async def get_or_create(
        self, telegram_id: int, username=None, first_name=None,
        last_name=None, language_code: str = "en",
    ) -> tuple[User, bool]:
        user = await self.get_by_telegram_id(telegram_id)
        if user:
            user.last_active_at = utcnow()
            return user, False
        user = await self.create(
            telegram_id=telegram_id, username=username,
            first_name=first_name, last_name=last_name,
            language_code=language_code,
        )
        return user, True

    async def update_streak(self, telegram_id: int) -> dict:
        """
        Update daily training streak for user.
        Returns {"streak": int, "longest": int, "is_first_today": bool}.
        Call once per completed test session.
        """
        user = await self.get_by_telegram_id(telegram_id)
        if not user:
            return {"streak": 0, "longest": 0, "is_first_today": False}

        today = utcnow().date()
        last = user.last_trained_date

        if last == today:
            # Already counted today
            return {
                "streak": user.current_streak or 0,
                "longest": user.longest_streak or 0,
                "is_first_today": False,
            }

        yesterday = today - timedelta(days=1)
        if last == yesterday:
            user.current_streak = (user.current_streak or 0) + 1
        else:
            user.current_streak = 1

        user.last_trained_date = today
        if user.current_streak > (user.longest_streak or 0):
            user.longest_streak = user.current_streak

        await self.session.flush()
        return {
            "streak": user.current_streak,
            "longest": user.longest_streak,
            "is_first_today": True,
        }

    async def touch_last_active(self, telegram_id: int) -> None:
        """Bump last_active_at with a single UPDATE — no row load. Called from
        the activity tracker on every interaction (throttled there), so the
        admin "active today/week" counts reflect button-only users too."""
        await self.session.execute(
            update(User)
            .where(User.telegram_id == telegram_id)
            .values(last_active_at=utcnow())
        )

    async def update_preferences(self, telegram_id: int, preferences: dict) -> Optional[User]:
        user = await self.get_by_telegram_id(telegram_id)
        if user:
            # Assign a NEW dict — SQLAlchemy JSON change detection compares
            # object identity, so mutating the loaded dict in place is
            # silently ignored at flush time.
            user.preferences = {**(user.preferences or {}), **preferences}
            await self.session.flush()
        return user

    async def set_leaderboard_opt_in(self, telegram_id: int, opt_in: bool) -> Optional[User]:
        user = await self.get_by_telegram_id(telegram_id)
        if user:
            user.leaderboard_opt_in = opt_in
            await self.session.flush()
        return user

    async def get_users_with_reminder_at(self, utc_hour: int) -> list[User]:
        """Users whose daily reminder fires at this UTC hour. The caller
        decides per user what (if anything) to send: no training nudge after
        a done session, but a due delayed recall still goes out. Part of the
        reminder feature (bot/reminders.py) — remove together with it."""
        rem = User.preferences["reminder"]
        result = await self.session.execute(
            select(User).where(
                rem["enabled"].as_boolean().is_(True),
                rem["utc_hour"].as_integer() == utc_hour,
            )
        )
        return list(result.scalars().all())

    async def set_subscription(
        self, telegram_id: int, tier: SubscriptionTier,
        expires_at: Optional[datetime] = None,
    ) -> Optional[User]:
        user = await self.get_by_telegram_id(telegram_id)
        if user:
            user.subscription_tier = tier
            user.subscription_expires_at = expires_at
            await self.session.flush()
        return user


class ExerciseSessionRepository:
    """Repository for ExerciseSession operations."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(
        self, user_id: int, exercise_type: ExerciseType,
        difficulty: str, parameters: dict,
        score: Optional[int] = None, max_score: Optional[int] = None,
        completed: bool = False, duration_s: Optional[int] = None,
    ) -> ExerciseSession:
        exercise_session = ExerciseSession(
            user_id=user_id, exercise_type=exercise_type,
            difficulty=difficulty, parameters=parameters,
            score=score, max_score=max_score, completed=completed,
            duration_s=duration_s,
            completed_at=utcnow() if completed else None,
        )
        self.session.add(exercise_session)
        await self.session.flush()
        return exercise_session

    async def get_user_sessions(
        self, user_id: int, exercise_type: Optional[ExerciseType] = None,
        limit: int = 10,
    ) -> list[ExerciseSession]:
        query = select(ExerciseSession).where(ExerciseSession.user_id == user_id)
        if exercise_type:
            query = query.where(ExerciseSession.exercise_type == exercise_type)
        query = query.order_by(ExerciseSession.started_at.desc()).limit(limit)
        result = await self.session.execute(query)
        return list(result.scalars().all())

    async def get_user_stats(self, user_id: int) -> dict:
        """
        Test statistics broken down per difficulty — computed in SQL.
        Retry-mistakes rounds are excluded (see _IS_SCORED_TEST).

        "mastered" = biggest pair count with a 90%+ score at that difficulty
        (a raw best-% would hide the huge gap between acing 5 and 50 pairs).

        Returns {
            "tests_total": int,
            "by_difficulty": {difficulty: {"tests", "mastered"}},  # mastered: int | None
            "latest_pct": float, "latest_pairs": int | None,
        }
        """
        stats = {
            "tests_total": 0, "by_difficulty": {},
            "latest_pct": 0.0, "latest_pairs": None,
        }
        pair_count = ExerciseSession.parameters["count"].as_integer()

        rows = await self.session.execute(
            select(
                ExerciseSession.difficulty,
                func.count(),
                func.max(case((_PCT >= 90, pair_count))),
            )
            .where(ExerciseSession.user_id == user_id, _IS_SCORED_TEST)
            .group_by(ExerciseSession.difficulty)
        )
        for difficulty, count, mastered in rows:
            stats["by_difficulty"][difficulty] = {
                "tests": count,
                "mastered": mastered,
            }
            stats["tests_total"] += count

        if stats["tests_total"]:
            latest = (await self.session.execute(
                select(_PCT, pair_count)
                .where(ExerciseSession.user_id == user_id, _IS_SCORED_TEST)
                .order_by(ExerciseSession.started_at.desc())
                .limit(1)
            )).one_or_none()
            if latest:
                stats["latest_pct"] = latest[0] or 0.0
                stats["latest_pairs"] = latest[1]

        return stats

    async def count_completed_tests(self, user_id: int) -> int:
        """Total scored test sessions for a user (used by achievements)."""
        return (await self.session.execute(
            select(func.count())
            .where(ExerciseSession.user_id == user_id, _IS_SCORED_TEST)
        )).scalar_one()

    async def get_audio_achievement_stats(self, user_id: int) -> dict:
        """Lifetime audio counters for audio achievements — removable with
        the audio XP feature (see gamification/audio_xp.py)."""
        base = and_(
            ExerciseSession.user_id == user_id,
            ExerciseSession.exercise_type == ExerciseType.AUDIO_VISUALIZATION,
            ExerciseSession.completed.is_(True),
        )
        story = ExerciseSession.parameters["story"].as_string()
        total = (await self.session.execute(
            select(func.count()).where(base)
        )).scalar_one()
        distinct_stories = (await self.session.execute(
            select(func.count(func.distinct(story))).where(base)
        )).scalar_one()
        perfect_quizzes = (await self.session.execute(
            select(func.count()).where(
                base, _HAS_SCORE, ExerciseSession.score == ExerciseSession.max_score,
            )
        )).scalar_one()
        return {
            "total_sessions": total,
            "distinct_stories": distinct_stories,
            "perfect_quizzes": perfect_quizzes,
        }

    async def get_personal_best(
        self, user_id: int, difficulty: str, count: int, fmt: str = "pairs",
    ) -> float | None:
        """
        Return the best score percentage for a given difficulty + count +
        format ("pairs" or "list"), or None if no previous test exists for
        that combo. Rows without a format (pre-list-feature) count as pairs.
        """
        fmt_col = ExerciseSession.parameters["format"].as_string()
        fmt_filter = (
            fmt_col == "list" if fmt == "list"
            else or_(fmt_col.is_(None), fmt_col == "pairs")
        )
        return (await self.session.execute(
            select(func.max(_PCT)).where(
                ExerciseSession.user_id == user_id,
                ExerciseSession.difficulty == difficulty,
                ExerciseSession.parameters["count"].as_integer() == count,
                fmt_filter,
                _IS_SCORED_TEST,
            )
        )).scalar_one_or_none()

    async def get_recent_test_history(
        self, user_id: int, limit: int = 10,
    ) -> list[dict]:
        """
        Return the last *limit* test-mode sessions as a list of dicts:
        [{date, difficulty, count, score, max_score, pct}, ...]
        Newest first.
        """
        result = await self.session.execute(
            select(ExerciseSession)
            .where(ExerciseSession.user_id == user_id, _IS_SCORED_TEST)
            .order_by(ExerciseSession.started_at.desc())
            .limit(limit)
        )
        history = []
        for sess in result.scalars():
            params = sess.parameters or {}
            history.append({
                "date": sess.started_at,
                "difficulty": sess.difficulty,
                "count": params.get("count", "?"),
                "mode": params.get("mode", "test"),
                "format": params.get("format", "pairs"),
                "speed": params.get("speed", False),
                "score": sess.score or 0,
                "max_score": sess.max_score,
                "pct": (sess.score or 0) / sess.max_score * 100,
            })
        return history

    async def get_recall_candidates(
        self, user_id: int, completed_after: datetime,
        completed_before: datetime, limit: int = 5,
    ) -> list[ExerciseSession]:
        """Fresh word-memo tests completed inside the window, newest first —
        delayed-recall sources (bot/recall.py). The caller picks the first
        one that stored its word list (rows before that change didn't)."""
        result = await self.session.execute(
            select(ExerciseSession)
            .where(
                ExerciseSession.user_id == user_id,
                ExerciseSession.exercise_type == ExerciseType.WORD_MEMORIZATION,
                _ROUND_MODE == "test",
                ExerciseSession.completed_at > completed_after,
                ExerciseSession.completed_at <= completed_before,
            )
            .order_by(ExerciseSession.completed_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    # ------------------------------------------------------------------
    # Leaderboard & admin analytics
    # ------------------------------------------------------------------

    async def get_leaderboard(
        self,
        limit: int = 10,
        min_tests: int = 3,
        exclude_telegram_ids: Iterable[int] = (),
    ) -> list[dict]:
        """Opted-in users ranked by average test score (min_tests required).

        exclude_telegram_ids: never list these users even if opted in —
        callers pass the admin IDs so admins can't compete with members.
        """
        stmt = (
            select(
                User.telegram_id, User.first_name, User.username,
                User.current_streak,
                func.count(ExerciseSession.id).label("tests"),
                func.avg(_PCT).label("avg_pct"),
                func.max(_PCT).label("best_pct"),
            )
            .join(ExerciseSession, ExerciseSession.user_id == User.id)
            .where(User.leaderboard_opt_in.is_(True), _IS_SCORED_TEST)
            .group_by(User.id)
            .having(func.count(ExerciseSession.id) >= min_tests)
            .order_by(func.avg(_PCT).desc())
            .limit(limit)
        )
        excluded = list(exclude_telegram_ids)
        if excluded:
            stmt = stmt.where(User.telegram_id.not_in(excluded))
        result = await self.session.execute(stmt)
        return [
            {
                "telegram_id": r.telegram_id,
                "name": r.first_name or r.username or "Anonymous",
                "streak": r.current_streak or 0,
                "tests": r.tests,
                "avg_pct": r.avg_pct or 0.0,
                "best_pct": r.best_pct or 0.0,
            }
            for r in result
        ]

    async def get_rank_for_user(
        self,
        telegram_id: int,
        min_tests: int = 3,
        exclude_telegram_ids: Iterable[int] = (),
    ) -> Optional[dict]:
        """Where *telegram_id* would place on the leaderboard right now.

        Returns {rank, total, avg_pct, tests, opted_in} — rank/total are None
        when the user has fewer than *min_tests* scored tests (not rankable
        yet; *tests* then tells the caller how far off they are). Ranks the
        user against the opted-in board even when they haven't opted in, so
        results can nudge them with a provisional placing. Returns None when
        the user has no scored tests at all, or is excluded (admins).
        """
        if telegram_id in set(exclude_telegram_ids):
            return None

        board = (
            select(
                User.telegram_id.label("telegram_id"),
                func.avg(_PCT).label("avg_pct"),
                func.count(ExerciseSession.id).label("tests"),
                User.leaderboard_opt_in.label("opted_in"),
            )
            .join(ExerciseSession, ExerciseSession.user_id == User.id)
            .where(_IS_SCORED_TEST)
            .group_by(User.id)
        )
        excluded = [t for t in exclude_telegram_ids]
        if excluded:
            board = board.where(User.telegram_id.not_in(excluded))
        board = board.subquery()

        rows = (await self.session.execute(
            select(board.c.telegram_id, board.c.avg_pct, board.c.tests, board.c.opted_in)
        )).all()

        me = next((r for r in rows if r.telegram_id == telegram_id), None)
        if me is None:
            return None

        opted_in = bool(me.opted_in)
        if (me.tests or 0) < min_tests:
            return {
                "rank": None, "total": None, "tests": me.tests or 0,
                "avg_pct": me.avg_pct or 0.0, "opted_in": opted_in,
            }

        # The board itself is opted-in users only; an opted-out viewer is
        # ranked *into* it (total counts them) so the nudge shows a real place.
        listed = [
            r for r in rows
            if r.tests >= min_tests and (r.opted_in or r.telegram_id == telegram_id)
        ]
        my_avg = me.avg_pct or 0.0
        rank = 1 + sum(1 for r in listed if (r.avg_pct or 0.0) > my_avg)
        return {
            "rank": rank, "total": len(listed), "tests": me.tests,
            "avg_pct": my_avg, "opted_in": opted_in,
        }

    async def get_global_stats(self) -> dict:
        """Bot-wide overview for the admin dashboard."""
        now = utcnow()
        day_ago = now - timedelta(days=1)
        week_ago = now - timedelta(days=7)

        total_users = (await self.session.execute(
            select(func.count()).select_from(User)
        )).scalar_one()
        new_week = (await self.session.execute(
            select(func.count()).select_from(User).where(User.created_at >= week_ago)
        )).scalar_one()
        active_day = (await self.session.execute(
            select(func.count()).select_from(User).where(User.last_active_at >= day_ago)
        )).scalar_one()
        active_week = (await self.session.execute(
            select(func.count()).select_from(User).where(User.last_active_at >= week_ago)
        )).scalar_one()
        tests_total = (await self.session.execute(
            select(func.count()).select_from(ExerciseSession).where(_IS_SCORED_TEST)
        )).scalar_one()
        tests_week = (await self.session.execute(
            select(func.count()).select_from(ExerciseSession)
            .where(_IS_SCORED_TEST, ExerciseSession.started_at >= week_ago)
        )).scalar_one()
        avg_score = (await self.session.execute(
            select(func.avg(_PCT)).where(_IS_SCORED_TEST)
        )).scalar_one()

        return {
            "total_users": total_users,
            "new_users_week": new_week,
            "active_day": active_day,
            "active_week": active_week,
            "tests_total": tests_total,
            "tests_week": tests_week,
            "avg_score": avg_score or 0.0,
        }

    async def get_users_progress(self, limit: int = 30) -> list[dict]:
        """Per-user progress overview for admin, most recently active first."""
        result = await self.session.execute(
            select(
                User.telegram_id, User.first_name, User.username,
                User.current_streak, User.longest_streak, User.last_active_at,
                func.count(ExerciseSession.id).label("tests"),
                func.avg(_PCT).label("avg_pct"),
                func.max(_PCT).label("best_pct"),
            )
            .outerjoin(
                ExerciseSession,
                and_(ExerciseSession.user_id == User.id, _IS_SCORED_TEST),
            )
            .group_by(User.id)
            .order_by(User.last_active_at.desc())
            .limit(limit)
        )
        return [
            {
                "telegram_id": r.telegram_id,
                "name": r.first_name or r.username or str(r.telegram_id),
                "streak": r.current_streak or 0,
                "longest_streak": r.longest_streak or 0,
                "last_active": r.last_active_at,
                "tests": r.tests or 0,
                "avg_pct": r.avg_pct,
                "best_pct": r.best_pct,
            }
            for r in result
        ]

    async def get_sessions_for_export(self) -> list[dict]:
        """
        All scored or completed sessions (retry rounds, audio listens, etc. —
        tagged in "mode") joined with user info, for CSV export — filter by
        mode in the spreadsheet. Unscored completed sessions (e.g. passive
        audio listens) export with empty score columns.
        """
        result = await self.session.execute(
            select(
                User.telegram_id, User.first_name, User.username,
                ExerciseSession.exercise_type, ExerciseSession.difficulty,
                ExerciseSession.parameters, ExerciseSession.score,
                ExerciseSession.max_score, ExerciseSession.started_at,
                ExerciseSession.duration_s,
            )
            .join(User, ExerciseSession.user_id == User.id)
            .where(or_(_HAS_SCORE, ExerciseSession.completed.is_(True)))
            .order_by(ExerciseSession.started_at)
        )
        rows = []
        for r in result:
            params = r.parameters or {}
            rows.append({
                "telegram_id": r.telegram_id,
                "name": r.first_name or r.username or "",
                "exercise": r.exercise_type.value,
                "difficulty": r.difficulty,
                "mode": params.get("mode", "test"),
                "format": params.get("format", ""),
                "pairs": params.get("count", ""),
                "score": r.score,
                "max_score": r.max_score,
                "pct": round(r.score / r.max_score * 100, 1) if r.max_score else "",
                "duration_s": r.duration_s if r.duration_s is not None else "",
                "date": r.started_at.isoformat() if r.started_at else "",
            })
        return rows

    async def get_training_time(
        self, since: Optional[datetime] = None, limit: int = 30,
    ) -> list[dict]:
        """Engaged training seconds per user, biggest first.

        Sums duration_s over every session that recorded one — training-mode
        rows and pre-feature rows are NULL and simply don't contribute, so
        totals are a floor, never an overstatement.
        """
        stmt = (
            select(
                User.telegram_id, User.first_name, User.username,
                func.sum(ExerciseSession.duration_s).label("seconds"),
                func.count(ExerciseSession.id).label("rounds"),
                func.avg(_PCT).label("avg_pct"),
            )
            .join(User, ExerciseSession.user_id == User.id)
            .where(ExerciseSession.duration_s.isnot(None))
            .group_by(User.id)
            .order_by(func.sum(ExerciseSession.duration_s).desc())
            .limit(limit)
        )
        if since is not None:
            stmt = stmt.where(ExerciseSession.started_at >= since)
        result = await self.session.execute(stmt)
        return [
            {
                "telegram_id": r.telegram_id,
                "name": r.first_name or r.username or str(r.telegram_id),
                "seconds": r.seconds or 0,
                "rounds": r.rounds,
                "avg_pct": r.avg_pct,
            }
            for r in result
        ]


class UserSkillRepository:
    """Repository for per-user skill XP bars."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_or_create(self, user_id: int, skill: str) -> UserSkill:
        result = await self.session.execute(
            select(UserSkill).where(
                UserSkill.user_id == user_id, UserSkill.skill == skill,
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            row = UserSkill(user_id=user_id, skill=skill, xp=0, level=1, hard_streak=0)
            self.session.add(row)
            await self.session.flush()
        return row

    async def get_all_for_user(self, user_id: int) -> list[UserSkill]:
        result = await self.session.execute(
            select(UserSkill).where(UserSkill.user_id == user_id)
            .order_by(UserSkill.skill)
        )
        return list(result.scalars().all())

    async def add_xp(
        self, user_id: int, skill: str, amount: int,
        new_level: int, hard_streak: int,
    ) -> UserSkill:
        """Apply an XP gain (level precomputed via gamification.xp.level_from_xp)."""
        row = await self.get_or_create(user_id, skill)
        row.xp += amount
        row.level = new_level
        row.hard_streak = hard_streak
        await self.session.flush()
        return row


class BotSettingsRepository:
    """Key-value runtime settings (feature flags)."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get(self, key: str) -> Optional[str]:
        result = await self.session.execute(
            select(BotSetting.value).where(BotSetting.key == key)
        )
        return result.scalar_one_or_none()

    async def set(self, key: str, value: str) -> None:
        result = await self.session.execute(
            select(BotSetting).where(BotSetting.key == key)
        )
        row = result.scalar_one_or_none()
        if row is None:
            self.session.add(BotSetting(key=key, value=value))
        else:
            row.value = value
        await self.session.flush()


class RedemptionCodeRepository:
    """One-time access codes (bot/redeem.py). Part of the redemption-code
    feature — remove together with it."""

    # No 0/O/1/I — codes get read off a screen and typed on a phone.
    _ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

    def __init__(self, session: AsyncSession):
        self.session = session

    def _generate_code(self) -> str:
        chunk = lambda: "".join(secrets.choice(self._ALPHABET) for _ in range(4))
        return f"MTB-{chunk()}-{chunk()}"

    async def create_batch(
        self, n: int, tier: SubscriptionTier, duration_days: Optional[int],
    ) -> list[str]:
        """Generate n unique unredeemed codes. duration_days None = lifetime."""
        codes = []
        while len(codes) < n:
            code = self._generate_code()
            exists = (await self.session.execute(
                select(func.count()).select_from(RedemptionCode)
                .where(RedemptionCode.code == code)
            )).scalar_one()
            if exists or code in codes:
                continue
            self.session.add(RedemptionCode(
                code=code, tier=tier, duration_days=duration_days,
            ))
            codes.append(code)
        await self.session.flush()
        return codes

    async def redeem(self, code: str, telegram_id: int) -> Optional[RedemptionCode]:
        """Atomically claim a code for a user. Returns the code row on
        success, None if the code doesn't exist or is already redeemed.
        The UPDATE ... WHERE redeemed_by IS NULL guard makes two concurrent
        redeems of the same code impossible."""
        result = await self.session.execute(
            update(RedemptionCode)
            .where(
                RedemptionCode.code == code,
                RedemptionCode.redeemed_by.is_(None),
            )
            .values(redeemed_by=telegram_id, redeemed_at=utcnow())
        )
        if result.rowcount == 0:
            return None
        await self.session.flush()
        return (await self.session.execute(
            select(RedemptionCode).where(RedemptionCode.code == code)
        )).scalar_one()

    async def get_stats(self) -> dict:
        """Counts + unredeemed codes for /admin codes list."""
        redeemed = (await self.session.execute(
            select(func.count()).select_from(RedemptionCode)
            .where(RedemptionCode.redeemed_by.isnot(None))
        )).scalar_one()
        unredeemed_rows = (await self.session.execute(
            select(RedemptionCode)
            .where(RedemptionCode.redeemed_by.is_(None))
            .order_by(RedemptionCode.created_at)
        )).scalars().all()
        return {"redeemed": redeemed, "unredeemed": list(unredeemed_rows)}


class AchievementRepository:
    """Repository for unlocked user achievements."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_unlocked_codes(self, user_id: int) -> set[str]:
        result = await self.session.execute(
            select(UserAchievement.code).where(UserAchievement.user_id == user_id)
        )
        return {row[0] for row in result}

    async def get_unlocked(self, user_id: int) -> dict[str, datetime]:
        """Map of code -> unlocked_at for a user."""
        result = await self.session.execute(
            select(UserAchievement.code, UserAchievement.unlocked_at)
            .where(UserAchievement.user_id == user_id)
        )
        return {code: ts for code, ts in result}

    async def unlock(self, user_id: int, codes: list[str]) -> None:
        for code in codes:
            self.session.add(UserAchievement(user_id=user_id, code=code))
        await self.session.flush()


class ActivityEventRepository:
    """Raw interaction stream (bot/analytics.py) — remove together with it."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def log(
        self, telegram_id: int, kind: str, detail: Optional[str] = None,
    ) -> None:
        self.session.add(ActivityEvent(
            telegram_id=telegram_id, kind=kind,
            detail=detail[:64] if detail else None,
        ))
        await self.session.flush()

    async def get_active_days(self, telegram_id: int, since: datetime) -> int:
        """Distinct UTC days with at least one interaction."""
        return (await self.session.execute(
            select(func.count(func.distinct(func.date(ActivityEvent.ts))))
            .where(
                ActivityEvent.telegram_id == telegram_id,
                ActivityEvent.ts >= since,
            )
        )).scalar_one()

    async def get_event_counts(self, since: datetime) -> list[tuple[str, int]]:
        """(kind, count) since a timestamp — coarse funnel view."""
        result = await self.session.execute(
            select(ActivityEvent.kind, func.count())
            .where(ActivityEvent.ts >= since)
            .group_by(ActivityEvent.kind)
            .order_by(func.count().desc())
        )
        return [(kind, count) for kind, count in result]

    async def purge_older_than(self, days: int) -> int:
        """Delete raw events older than *days*. Called daily by the retention
        job in bot/analytics.py (ACTIVITY_RETENTION_DAYS)."""
        cutoff = utcnow() - timedelta(days=days)
        result = await self.session.execute(
            ActivityEvent.__table__.delete().where(ActivityEvent.ts < cutoff)
        )
        return result.rowcount
