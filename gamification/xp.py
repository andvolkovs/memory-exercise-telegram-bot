"""
Experience / leveling system.

One skill bar live ("mnemonics" — fed by Word Memorization tests). Future
exercises declare their own skill via EXERCISE_SKILLS; each skill gets its own
bar automatically.

Balance model
-------------
Every test has a *challenge rating* (CR):

    CR = pairs × difficulty_mult × speed_mult

Each level has an *expected challenge*. Tests below it earn proportionally
less XP (diminishing returns — the "outgrown" effect); tests at/above it earn
full XP and count toward the hard-streak bonus. So to keep leveling you raise
pairs, difficulty, or speed — exactly the intended progression pressure.

All tuning knobs are the module constants below; see docs/ADMIN_GUIDE.md for
what each one does.
"""

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Skill definitions — add future bars here
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SkillDef:
    code: str
    emoji: str
    name: str
    description: str


SKILLS: dict[str, SkillDef] = {
    "mnemonics": SkillDef(
        "mnemonics", "🧠", "Mnemonics",
        "Grows with completed Word Memorization tests",
    ),
    # Future: "focus", "mental_math", "pattern_recognition", ...
}

# exercise_type value (database.ExerciseType.value) -> skill code
EXERCISE_SKILLS: dict[str, str] = {
    "word_memorization": "mnemonics",
}

# ---------------------------------------------------------------------------
# Tuning constants
# ---------------------------------------------------------------------------

BASE_XP_PER_PAIR = 4.0

DIFFICULTY_MULT = {
    "beginner": 1.0,
    "intermediate": 1.25,
    "advanced": 1.5,
}

SPEED_MULT_FAST = 1.3      # speed-mode study time earns 30% more
ACCURACY_EXPONENT = 2.0    # rewards accuracy super-linearly (80% -> x0.64)
PERFECT_BONUS = 1.2        # extra 20% for a flawless test

# Anti-farm accuracy gates: a failed test (below MIN_XP_SCORE_PCT) earns
# nothing — otherwise spamming huge lists at low accuracy out-earns honest
# small tests (100-word advanced at 30% used to beat a perfect 10-pair run).
# The hard streak additionally requires HARD_STREAK_MIN_PCT: attempting a
# hard test isn't enough, you have to perform on it.
MIN_XP_SCORE_PCT = 50.0
HARD_STREAK_MIN_PCT = 70.0

# Diminishing returns: an exercise below your level's expected challenge earns
# CR/expected of the XP, never less than the floor.
EFFICIENCY_FLOOR = 0.15

# Hard-streak bonus: +10% per consecutive hard test after the first, cap +50%.
HARD_STREAK_BONUS_PER = 0.10
HARD_STREAK_BONUS_CAP = 0.50

# Delayed recall (bot/recall.py): retrieval after sleep is harder than an
# immediate quiz, so it pays more. Applied on top of an XP calc that skips
# diminishing returns — the bot, not the user, picks the round size, and it
# runs at most once a day, so there is nothing to farm.
RECALL_XP_MULT = 1.5


def expected_challenge(level: int) -> float:
    """Challenge rating a player of this level is 'supposed' to attempt.

    Level 1 = 5 (a 5-pair beginner test). Grows 2.5/level; the max exercise
    (100 pairs advanced fast, CR 195) stops giving full XP around level ~77.
    """
    return 5.0 + 2.5 * (level - 1)


def xp_for_next_level(level: int) -> int:
    """XP needed to go from `level` to `level + 1`."""
    return int(80 + 45 * (level - 1) ** 1.3)


def level_from_xp(total_xp: int) -> tuple[int, int, int]:
    """Return (level, xp_into_level, xp_needed_for_next) for a total XP amount."""
    level = 1
    threshold = 0
    need = xp_for_next_level(level)
    while total_xp >= threshold + need:
        threshold += need
        level += 1
        need = xp_for_next_level(level)
    return level, total_xp - threshold, need


def challenge_rating(pairs: int, difficulty: str, speed_mode: bool) -> float:
    return (
        pairs
        * DIFFICULTY_MULT.get(difficulty, 1.0)
        * (SPEED_MULT_FAST if speed_mode else 1.0)
    )


@dataclass(frozen=True)
class XpResult:
    xp: int                  # XP awarded (after all multipliers)
    is_hard: bool            # counted toward the hard streak
    new_hard_streak: int     # streak value after this test
    streak_multiplier: float # 1.0 if no bonus applied
    efficiency: float        # 1.0 = full value, <1 = outgrown exercise


def compute_test_xp(
    *, pairs: int, difficulty: str, speed_mode: bool,
    score_pct: float, level: int, hard_streak: int,
) -> XpResult:
    """XP for one completed test. Pure function — all state passed in."""
    if pairs <= 0:
        return XpResult(0, False, 0, 1.0, 0.0)

    cr = challenge_rating(pairs, difficulty, speed_mode)
    expected = expected_challenge(level)
    efficiency = max(min(cr / expected, 1.0), EFFICIENCY_FLOOR)

    if score_pct < MIN_XP_SCORE_PCT:
        # Failed test: no XP, hard streak broken.
        return XpResult(0, False, 0, 1.0, efficiency)

    accuracy = max(score_pct, 0.0) / 100.0
    xp = BASE_XP_PER_PAIR * cr * (accuracy ** ACCURACY_EXPONENT) * efficiency
    if score_pct >= 100:
        xp *= PERFECT_BONUS

    is_hard = cr >= expected and score_pct >= HARD_STREAK_MIN_PCT
    new_streak = hard_streak + 1 if is_hard else 0
    streak_mult = 1.0
    if is_hard and new_streak > 1:
        streak_mult = 1.0 + min(
            (new_streak - 1) * HARD_STREAK_BONUS_PER, HARD_STREAK_BONUS_CAP
        )
        xp *= streak_mult

    return XpResult(
        xp=round(xp),
        is_hard=is_hard,
        new_hard_streak=new_streak,
        streak_multiplier=streak_mult,
        efficiency=efficiency,
    )


def render_progress_bar(xp_into_level: int, xp_needed: int, width: int = 10) -> str:
    filled = int(width * xp_into_level / xp_needed) if xp_needed else width
    filled = min(filled, width)
    return "▰" * filled + "▱" * (width - filled)
