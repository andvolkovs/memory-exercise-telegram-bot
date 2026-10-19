"""Delayed recall (bot/recall.py): word lists stored on fresh tests, round
building, the due window, and the engine's bookkeeping for recall rounds."""

import random
from datetime import timedelta

from database import (
    ExerciseSessionRepository, ExerciseType, UserRepository,
    UserSkillRepository, get_session,
)
from database.models import utcnow
from exercises import Difficulty
from bot import features, quiz_engine as qe
from bot import recall
from bot.reminders import _build_ping
from tests.conftest import fresh_telegram_id
from tests.fakes import FakeContext, FakeQuery

PAIRS = [[f"a{i}", f"b{i}"] for i in range(10)]
WORDS = [f"w{i}" for i in range(20)]


# ---- pure helpers ----------------------------------------------------------

def test_question_count_clamps_to_half_between_5_and_10():
    assert recall.recall_question_count(5, "pairs") == 5
    assert recall.recall_question_count(10, "pairs") == 5
    assert recall.recall_question_count(20, "pairs") == 10
    assert recall.recall_question_count(100, "pairs") == 10
    # Lists: never more than n//2 (non-neighbouring positions 1..n-1).
    assert recall.recall_question_count(5, "list") == 2
    assert recall.recall_question_count(20, "list") == 10


def test_pairs_round_samples_in_study_order():
    pairs, items = recall.build_recall_round(PAIRS, "pairs", random.Random(1))
    assert len(pairs) == len(items) == 5
    order = [PAIRS.index(list(p)) for p in pairs]
    assert order == sorted(order)
    for i, (item, (w1, w2)) in enumerate(zip(items, pairs)):
        assert item["pair_index"] == i
        assert {item["shown_word"], item["expected"]} == {w1, w2}


def test_list_round_never_asks_neighbours():
    # Asking positions i and i+1 would show answer i as the next prompt.
    for seed in range(200):
        pairs, items = recall.build_recall_round(WORDS, "list", random.Random(seed))
        positions = [WORDS.index(it["expected"]) for it in items]
        assert len(positions) == 10
        assert positions == sorted(positions) and positions[0] >= 1
        assert all(b - a >= 2 for a, b in zip(positions, positions[1:]))
        for it in items:
            p = WORDS.index(it["expected"])
            assert it["shown_word"] == WORDS[p - 1] and it["direction"] == "next"


def test_age_label():
    assert recall.age_label(10) == "your test 10 hours ago"
    assert recall.age_label(30) == "yesterday's test"
    assert recall.age_label(80) == "your test 3 days ago"


# ---- due window ------------------------------------------------------------

async def _source(session, user, hours_ago, items=PAIRS, mode="test", fmt="pairs"):
    params = {"count": len(items or PAIRS), "mode": mode, "format": fmt}
    if items is not None:
        params["items"] = items
    row = await ExerciseSessionRepository(session).create(
        user_id=user.id, exercise_type=ExerciseType.WORD_MEMORIZATION,
        difficulty="intermediate", parameters=params,
        score=8, max_score=len(items or PAIRS), completed=True,
    )
    row.completed_at = utcnow() - timedelta(hours=hours_ago)
    await session.flush()
    return row


async def _user(session):
    user, _ = await UserRepository(session).get_or_create(
        telegram_id=fresh_telegram_id(), first_name="t",
    )
    return user


async def test_due_picks_newest_test_inside_window():
    async with get_session() as s:
        u = await _user(s)
        repo = ExerciseSessionRepository(s)
        await _source(s, u, hours_ago=2)                   # too fresh
        await _source(s, u, hours_ago=200)                 # too old
        await _source(s, u, hours_ago=20, mode="retry")    # not a fresh test
        await _source(s, u, hours_ago=12, items=None)      # pre-feature row
        older = await _source(s, u, hours_ago=40)
        newest = await _source(s, u, hours_ago=30)
        due = await recall.get_due_recall(repo, u)
        assert due["source_id"] == newest.id != older.id
        assert due["questions"] == 5 and due["age_label"] == "yesterday's test"


async def test_not_due_after_claim_today_or_same_source():
    async with get_session() as s:
        u = await _user(s)
        repo = ExerciseSessionRepository(s)
        src = await _source(s, u, hours_ago=20)
        today = utcnow().date().isoformat()
        u.preferences = {"recall": {"source_id": 0, "date": today}}
        assert await recall.get_due_recall(repo, u) is None
        u.preferences = {"recall": {"source_id": src.id, "date": "2000-01-01"}}
        assert await recall.get_due_recall(repo, u) is None


async def test_flag_off_hides_recall(monkeypatch):
    monkeypatch.setitem(features._flags, features.RECALL_ENABLED_KEY, False)
    async with get_session() as s:
        u = await _user(s)
        await _source(s, u, hours_ago=20)
        assert await recall.get_due_recall(ExerciseSessionRepository(s), u) is None


# ---- engine bookkeeping ----------------------------------------------------

def _finished_state(round_mode, pairs, answers):
    items = [{"pair_index": i, "shown_word": a, "expected": b}
             for i, (a, b) in enumerate(pairs)]
    results = [{"pair_index": i, "shown_word": a, "expected": b, "answer": ans,
                "correct": ans == b, "fuzzy": False, "direction": None}
               for i, ((a, b), ans) in enumerate(zip(pairs, answers))]
    return {
        "test_active": True, "test_exercise_type": "word_memo",
        "test_pairs": pairs, "test_format": "pairs", "test_quiz_items": items,
        "test_current_index": len(items), "test_results": results,
        "test_difficulty": Difficulty.INTERMEDIATE, "baseline_results": [],
        "test_round_mode": round_mode,
    }


async def _sessions(telegram_id):
    async with get_session() as s:
        u = await UserRepository(s).get_by_telegram_id(telegram_id)
        rows = await ExerciseSessionRepository(s).get_user_sessions(u.id)
        return u, rows


async def test_fresh_test_stores_its_word_list():
    uid = fresh_telegram_id()
    async with get_session() as s:
        await UserRepository(s).get_or_create(telegram_id=uid, first_name="t")
    ctx = FakeContext(uid)
    pairs = [("apple", "river"), ("candle", "stone")]
    ctx.state = _finished_state("test", pairs, ["river", "stone"])
    await qe._show_test_results(ctx, uid, ctx.state)
    _, rows = await _sessions(uid)
    assert rows[0].parameters["items"] == [["apple", "river"], ["candle", "stone"]]


async def test_retry_round_does_not_store_word_list():
    uid = fresh_telegram_id()
    async with get_session() as s:
        await UserRepository(s).get_or_create(telegram_id=uid, first_name="t")
    ctx = FakeContext(uid)
    ctx.state = _finished_state("retry", [("a", "b")], ["b"])
    ctx.state["test_retry_rounds"] = 1
    await qe._show_test_results(ctx, uid, ctx.state)
    _, rows = await _sessions(uid)
    assert "items" not in rows[0].parameters


async def test_recall_round_is_unscored_streaks_and_pays_bonus_xp():
    uid = fresh_telegram_id()
    async with get_session() as s:
        user, _ = await UserRepository(s).get_or_create(telegram_id=uid, first_name="t")
        skill = await UserSkillRepository(s).get_or_create(user.id, "mnemonics")
        skill.level, skill.hard_streak = 20, 3
    ctx = FakeContext(uid)
    pairs = [(f"x{i}", f"y{i}") for i in range(5)]
    ctx.state = _finished_state("recall", pairs, [f"y{i}" for i in range(5)])
    ctx.state.update(recall_source_id=42, recall_source_format="pairs",
                     recall_age_label="yesterday's test")
    await qe._show_test_results(ctx, uid, ctx.state)

    text = ctx.bot.sent[-1]
    assert text.startswith("🧠 *Delayed recall* — yesterday's test")
    assert "Delayed recall — ×1.5 XP" in text
    assert "personal best" not in text.lower()
    assert "last_test_pairs" not in ctx.state   # stale Retry can't reuse it

    u, rows = await _sessions(uid)
    assert rows[0].parameters["mode"] == "recall"
    assert rows[0].parameters["source_id"] == 42 and "items" not in rows[0].parameters
    assert u.current_streak == 1
    async with get_session() as s:
        assert await ExerciseSessionRepository(s).count_completed_tests(u.id) == 0
        skill = await UserSkillRepository(s).get_or_create(u.id, "mnemonics")
        assert skill.hard_streak == 3
        # 5 intermediate pairs, 100%, no diminishing returns, ×1.5.
        assert skill.xp > 0


async def test_recall_callback_starts_round_and_claims_source():
    uid = fresh_telegram_id()
    async with get_session() as s:
        u, _ = await UserRepository(s).get_or_create(telegram_id=uid, first_name="t")
        src = await _source(s, u, hours_ago=20, items=WORDS, fmt="list")
        src_id = src.id
    ctx = FakeContext(uid)
    await recall.handle_recall_callback(FakeQuery(uid), ctx, f"recall:go:{src_id}")
    st = ctx.state
    assert st["test_active"] and st["test_round_mode"] == "recall"
    assert st["test_format"] == "pairs" and len(st["test_quiz_items"]) == 10
    assert "Question 1/10" in ctx.bot.sent[-1]

    # Claimed: the same button again (or tomorrow's stale one) is refused.
    ctx2 = FakeContext(uid)
    q = FakeQuery(uid)
    await recall.handle_recall_callback(q, ctx2, f"recall:go:{src_id}")
    assert "isn't available" in q.edits[-1]
    assert "test_active" not in ctx2.state


# ---- reminder ping ---------------------------------------------------------

def _due():
    return {"source_id": 7, "questions": 5, "age_label": "yesterday's test"}


def test_ping_with_and_without_training_nudge():
    preset = {"difficulty": "beginner", "count": 10, "format": "pairs", "speed": False}
    text, kb = _build_ping(preset, _due())
    data = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert data[0].startswith("rem:go:") and "recall:go:7" in data
    assert "Daily training time" in text and "Delayed recall ready" in text

    text, kb = _build_ping(None, _due())
    data = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert not any(d.startswith("rem:go:") for d in data) and "recall:go:7" in data
    assert "Daily training time" not in text
