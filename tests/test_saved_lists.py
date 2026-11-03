"""Saved lists (bot/saved_lists.py): every fresh test stores its list, the
auto-save default + the opposite-of-default toggle, the panel queries, and
the review round's bookkeeping and once-a-day XP."""

from datetime import datetime, timedelta

from database import (
    ExerciseSessionRepository, ExerciseType, UserRepository,
    UserSkillRepository, get_session,
)
from database.models import utcnow
from exercises import Difficulty
from bot import features, quiz_engine as qe, saved_lists as sl
from tests.conftest import fresh_telegram_id
from tests.fakes import FakeContext, FakeQuery


async def _new_user(prefs=None):
    uid = fresh_telegram_id()
    async with get_session() as s:
        user, _ = await UserRepository(s).get_or_create(telegram_id=uid, first_name="t")
        if prefs:
            user.preferences = prefs
    return uid


def _finished_state(round_mode, pairs, answers, fmt="pairs"):
    items = [{"pair_index": i, "shown_word": a, "expected": b}
             for i, (a, b) in enumerate(pairs)]
    results = [{"pair_index": i, "shown_word": a, "expected": b, "answer": ans,
                "correct": ans == b, "fuzzy": False, "direction": None}
               for i, ((a, b), ans) in enumerate(zip(pairs, answers))]
    return {
        "test_active": True, "test_exercise_type": "word_memo",
        "test_pairs": pairs, "test_format": fmt, "test_quiz_items": items,
        "test_current_index": len(items), "test_results": results,
        "test_difficulty": Difficulty.INTERMEDIATE, "baseline_results": [],
        "test_round_mode": round_mode,
    }


PAIRS = [("apple", "river"), ("candle", "stone")]


async def _rows(uid):
    async with get_session() as s:
        u = await UserRepository(s).get_by_telegram_id(uid)
        return u, await ExerciseSessionRepository(s).get_user_sessions(u.id)


def _callbacks(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


async def _finish_test(uid, round_mode="test", answers=("river", "stone"), **extra):
    ctx = FakeContext(uid)
    ctx.state = _finished_state(round_mode, PAIRS, list(answers))
    ctx.state.update(extra)
    await qe._show_test_results(ctx, uid, ctx.state)
    return ctx


# ---- storage + toggle ------------------------------------------------------

async def test_fresh_test_is_saved_by_default_with_dont_save_button():
    uid = await _new_user()
    ctx = await _finish_test(uid)
    _, rows = await _rows(uid)
    params = rows[0].parameters
    assert params["items"] == [["apple", "river"], ["candle", "stone"]]
    assert params["saved"] is True
    assert f"lists:unsave:{rows[0].id}" in _callbacks(ctx.bot.markups[-1])


async def test_autosave_off_stores_unsaved_with_save_button():
    uid = await _new_user({"autosave_lists": False})
    ctx = await _finish_test(uid)
    _, rows = await _rows(uid)
    assert rows[0].parameters["saved"] is False
    assert f"lists:save:{rows[0].id}" in _callbacks(ctx.bot.markups[-1])
    async with get_session() as s:
        u = await UserRepository(s).get_by_telegram_id(uid)
        assert await ExerciseSessionRepository(s).count_saved_lists(u.id) == 0


async def test_toggle_flips_saved_and_swaps_button_in_place():
    uid = await _new_user()
    ctx = await _finish_test(uid)
    _, rows = await _rows(uid)
    row_id = rows[0].id
    q = FakeQuery(uid)
    q.message.reply_markup = ctx.bot.markups[-1]
    await sl.handle_lists_callback(q, ctx, f"lists:unsave:{row_id}")
    assert f"lists:save:{row_id}" in _callbacks(q.message.reply_markup)
    assert f"lists:unsave:{row_id}" not in _callbacks(q.message.reply_markup)
    _, rows = await _rows(uid)
    assert rows[0].parameters["saved"] is False
    await sl.handle_lists_callback(q, ctx, f"lists:save:{row_id}")
    _, rows = await _rows(uid)
    assert rows[0].parameters["saved"] is True


async def test_retry_round_stores_no_list():
    uid = await _new_user()
    ctx = await _finish_test(uid, round_mode="retry", test_retry_rounds=1)
    _, rows = await _rows(uid)
    assert "items" not in rows[0].parameters
    assert not any(d.startswith("lists:") for d in _callbacks(ctx.bot.markups[-1]))


async def test_flag_off_hides_toggle(monkeypatch):
    monkeypatch.setitem(features._flags, features.SAVED_LISTS_ENABLED_KEY, False)
    uid = await _new_user()
    ctx = await _finish_test(uid)
    assert not any(d.startswith("lists:") for d in _callbacks(ctx.bot.markups[-1]))


# ---- panel queries ---------------------------------------------------------

async def _stored(s, user, *, mode="test", items=PAIRS, saved=None, hours_ago=1):
    params = {"count": len(items), "mode": mode, "format": "pairs",
              "items": [list(p) for p in items]}
    if saved is not None:
        params["saved"] = saved
    row = await ExerciseSessionRepository(s).create(
        user_id=user.id, exercise_type=ExerciseType.WORD_MEMORIZATION,
        difficulty="intermediate", parameters=params, score=1, max_score=2,
        completed=True,
    )
    row.completed_at = utcnow() - timedelta(hours=hours_ago)
    await s.flush()
    return row


async def test_saved_list_query_newest_first_and_legacy_rows_count():
    uid = await _new_user()
    async with get_session() as s:
        u = await UserRepository(s).get_by_telegram_id(uid)
        repo = ExerciseSessionRepository(s)
        legacy = await _stored(s, u, hours_ago=50)            # no "saved" key
        newest = await _stored(s, u, saved=True, hours_ago=1)
        await _stored(s, u, saved=False, hours_ago=2)          # unsaved
        await _stored(s, u, mode="retry", hours_ago=3)         # not a fresh test
        await repo.create(                                     # pre-items row
            user_id=u.id, exercise_type=ExerciseType.WORD_MEMORIZATION,
            difficulty="beginner", parameters={"count": 5, "mode": "test"},
            score=5, max_score=5, completed=True,
        )
        assert await repo.count_saved_lists(u.id) == 2
        assert [r.id for r in await repo.get_saved_lists(u.id)] == [newest.id, legacy.id]


async def test_panel_lists_and_pages():
    uid = await _new_user()
    async with get_session() as s:
        u = await UserRepository(s).get_by_telegram_id(uid)
        for h in range(sl.LISTS_PER_PAGE + 2):
            await _stored(s, u, saved=True, hours_ago=h + 1)
    text, kb = await sl._panel(uid, 0)
    data = _callbacks(kb)
    assert f"Saved lists* — {sl.LISTS_PER_PAGE + 2}" in text
    assert sum(d.startswith("lists:open:") for d in data) == sl.LISTS_PER_PAGE
    assert "lists:page:1" in data
    _, kb2 = await sl._panel(uid, 1)
    assert sum(d.startswith("lists:open:") for d in _callbacks(kb2)) == 2


def test_times_use_reminder_offset_when_known():
    dt = datetime(2026, 11, 3, 12, 5)
    prefs = {"reminder": {"hour_local": 9, "utc_hour": 7}}
    assert sl.tz_offset_hours(prefs) == 2
    assert sl.format_when(dt, 2) == "Nov 3, 14:05"
    assert sl.tz_offset_hours({"reminder": {"hour_local": 1, "utc_hour": 23}}) == 2
    assert sl.tz_offset_hours({"reminder": {"hour_local": 22, "utc_hour": 3}}) == -5
    assert sl.format_when(dt, None) == "Nov 3, 12:05 UTC"


# ---- review ----------------------------------------------------------------

async def test_review_runs_whole_list_from_memory():
    uid = await _new_user()
    async with get_session() as s:
        u = await UserRepository(s).get_by_telegram_id(uid)
        src = await _stored(s, u, saved=True)
        src_id = src.id
    ctx = FakeContext(uid)
    await sl.handle_lists_callback(FakeQuery(uid), ctx, f"lists:review:{src_id}")
    st = ctx.state
    assert st["test_active"] and st["test_round_mode"] == "review"
    assert len(st["test_quiz_items"]) == 2 and st["review_source_id"] == src_id
    assert not ctx.job_queue.live("quiz_timer_")      # no study phase
    assert "Question 1/2" in ctx.bot.sent[-1]


async def test_unsaved_list_cannot_be_reviewed():
    uid = await _new_user()
    async with get_session() as s:
        u = await UserRepository(s).get_by_telegram_id(uid)
        src = await _stored(s, u, saved=False)
    ctx, q = FakeContext(uid), FakeQuery(uid)
    await sl.handle_lists_callback(q, ctx, f"lists:review:{src.id}")
    assert "isn't in your saved lists" in q.edits[-1]
    assert "test_active" not in ctx.state


async def _review(uid, src_id, answers):
    ctx = FakeContext(uid)
    ctx.state = _finished_state("review", PAIRS, list(answers))
    ctx.state.update(review_source_id=src_id, review_label="Nov 1, 10:00 UTC")
    await qe._show_test_results(ctx, uid, ctx.state)
    return ctx


async def test_review_xp_quarter_once_per_list_per_day_and_unscored():
    uid = await _new_user()
    async with get_session() as s:
        u = await UserRepository(s).get_by_telegram_id(uid)
        skill = await UserSkillRepository(s).get_or_create(u.id, "mnemonics")
        skill.hard_streak = 3
        src = await _stored(s, u, saved=True)
        src_id, user_db_id = src.id, u.id

    ctx = await _review(uid, src_id, ["river", "stone"])
    text = ctx.bot.sent[-1]
    assert text.startswith("📚 *List review* — saved Nov 1, 10:00 UTC")
    assert "List review — ¼ XP" in text and "personal best" not in text.lower()
    assert "last_test_pairs" not in ctx.state
    assert f"lists:review:{src_id}" in _callbacks(ctx.bot.markups[-1])

    async with get_session() as s:
        repo = ExerciseSessionRepository(s)
        skill = await UserSkillRepository(s).get_or_create(user_db_id, "mnemonics")
        first_xp = skill.xp
        assert first_xp > 0 and skill.hard_streak == 3
        # Only the source test counts — the review round is unscored.
        assert await repo.count_completed_tests(user_db_id) == 1
        source = await repo.get_saved_list(user_db_id, src_id)
        assert source.parameters["reviews"] == 1
        assert source.parameters["review_xp_date"] == utcnow().date().isoformat()

    # Same list, same day: no more XP.
    ctx = await _review(uid, src_id, ["river", "stone"])
    assert "first review of a list each day" in ctx.bot.sent[-1]
    async with get_session() as s:
        skill = await UserSkillRepository(s).get_or_create(user_db_id, "mnemonics")
        assert skill.xp == first_xp
        source = await ExerciseSessionRepository(s).get_saved_list(user_db_id, src_id)
        assert source.parameters["reviews"] == 2


async def test_failed_first_review_closes_xp_for_the_day():
    # The failed review's results show the whole list — "Review again" must
    # not pay out on answers copied off that screen.
    uid = await _new_user()
    async with get_session() as s:
        u = await UserRepository(s).get_by_telegram_id(uid)
        src = await _stored(s, u, saved=True)
        src_id, user_db_id = src.id, u.id
    ctx = await _review(uid, src_id, ["(skipped)", "(skipped)"])    # 0%
    assert "Review XP needs 80%+" in ctx.bot.sent[-1]
    ctx = await _review(uid, src_id, ["river", "stone"])            # copied
    assert "first review of a list each day" in ctx.bot.sent[-1]
    async with get_session() as s:
        skill = await UserSkillRepository(s).get_or_create(user_db_id, "mnemonics")
        assert skill.xp == 0
        source = await ExerciseSessionRepository(s).get_saved_list(user_db_id, src_id)
        assert source.parameters["reviews"] == 2
        assert "review_xp_date" not in source.parameters


async def test_rolled_back_save_drops_the_toggle(monkeypatch):
    # The row id would point at nothing — or at a reused rowid.
    async def boom(*a, **kw):
        raise RuntimeError("late failure in the results transaction")
    monkeypatch.setattr(qe, "record_sprint_progress", boom)
    uid = await _new_user()
    ctx = await _finish_test(uid)
    assert not any(d.startswith("lists:") for d in _callbacks(ctx.bot.markups[-1]))
    _, rows = await _rows(uid)
    assert rows == []


async def test_old_recall_buttons_point_to_saved_lists():
    uid = await _new_user()
    q = FakeQuery(uid)
    await sl.handle_legacy_recall_callback(q, FakeContext(uid), "recall:go:5")
    assert "replaced by 📚 *Saved lists*" in q.edits[-1]
