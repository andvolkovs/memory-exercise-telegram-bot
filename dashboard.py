"""
Private analytics dashboard for the Mental Training Bot.

Reads a SQLite snapshot **read-only** — it never writes to the bot's database
and never needs the bot to be running.

    pip install streamlit plotly pandas numpy
    streamlit run dashboard.py

Point it at a snapshot taken with scripts/backup_db.sh (see ADMIN_GUIDE
"Backups"). Never open the live production file: an analysis tool has no
business holding a lock on the database members are training against.

Every chart has a camera icon in its top-right corner that downloads a PNG,
which is the "shareable graphic" story until proper share cards exist.

Scoring rules mirror the bot exactly (see database/repositories.py
_IS_SCORED_TEST): retry, placement, audio_quiz, reverse_extra, review and recall rounds are
excluded from accuracy stats so the numbers here match /stats and the
leaderboard. They still count as training *time* — practice is practice.
"""

import json
import os
import sqlite3

import numpy as np
import pandas as pd
import plotly.express as px
import streamlit as st

# Rounds excluded from accuracy aggregates (but not from training time).
UNSCORED_MODES = {"retry", "placement", "audio_quiz", "reverse_extra", "review", "recall"}

# A gap longer than this starts a new "visit" when sessionizing raw events.
# Telegram gives no app-open/close signal, so every time-in-bot number derived
# from this is an approximation — never quote it to a member as training time.
IDLE_GAP_MIN = 5

st.set_page_config(page_title="Mental Training Dashboard", layout="wide")


# ---------------------------------------------------------------- data load

@st.cache_data(show_spinner=False)
def load(db_path: str) -> dict[str, pd.DataFrame]:
    """Read every table we need in one go. Cached until the path changes."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        users = pd.read_sql_query(
            "SELECT id, telegram_id, first_name, username, current_streak,"
            " longest_streak, created_at, last_active_at FROM users", conn,
        )
        sessions = pd.read_sql_query(
            "SELECT s.id, s.user_id, u.first_name AS name, u.username,"
            " s.exercise_type, s.difficulty, s.score, s.max_score,"
            " s.duration_s, s.started_at, s.parameters"
            " FROM exercise_sessions s JOIN users u ON u.id = s.user_id", conn,
        )
        # activity_events only exists on databases migrated past the analytics
        # release — an older snapshot should still open, just with less in it.
        try:
            events = pd.read_sql_query(
                "SELECT telegram_id, ts, kind, detail FROM activity_events", conn,
            )
        except pd.errors.DatabaseError:
            events = pd.DataFrame(columns=["telegram_id", "ts", "kind", "detail"])
    finally:
        conn.close()

    if not sessions.empty:
        params = sessions.parameters.map(
            lambda p: json.loads(p) if isinstance(p, str) else (p or {})
        )
        sessions["mode"] = params.map(lambda d: d.get("mode", "test"))
        sessions["count"] = params.map(lambda d: d.get("count"))
        sessions["format"] = params.map(lambda d: d.get("format", "pairs"))
        sessions["speed"] = params.map(lambda d: bool(d.get("speed", False)))
        sessions["started_at"] = pd.to_datetime(sessions.started_at)
        sessions["date"] = sessions.started_at.dt.date
        sessions["pct"] = np.where(
            sessions.max_score.fillna(0) > 0,
            sessions.score / sessions.max_score * 100,
            np.nan,
        )
        sessions["minutes"] = sessions.duration_s / 60
        sessions["display_name"] = (
            sessions.name.fillna(sessions.username).fillna("user")
        )
    if not events.empty:
        events["ts"] = pd.to_datetime(events.ts)

    return {"users": users, "sessions": sessions, "events": events}


def scored(sessions: pd.DataFrame) -> pd.DataFrame:
    """Rounds that count toward accuracy — same rule as the bot."""
    if sessions.empty:
        return sessions
    return sessions[~sessions["mode"].isin(UNSCORED_MODES) & sessions.pct.notna()]


def accuracy_slope(group: pd.DataFrame) -> float:
    """Percentage points gained per test, by least-squares fit.

    Needs at least three tests — a line through two points is not a trend,
    and reporting one as progress would be inventing signal.
    """
    g = group.sort_values("started_at")
    if len(g) < 3:
        return np.nan
    return float(np.polyfit(np.arange(len(g)), g.pct.values, 1)[0])


def sessionize(events: pd.DataFrame, gap_min: int = IDLE_GAP_MIN) -> pd.DataFrame:
    """Group the raw event stream into visits, splitting on idle gaps."""
    if events.empty:
        return pd.DataFrame(columns=["telegram_id", "visit", "start", "end", "minutes", "events"])
    ev = events.sort_values(["telegram_id", "ts"]).copy()
    gap = ev.groupby("telegram_id").ts.diff() > pd.Timedelta(minutes=gap_min)
    ev["visit"] = gap.groupby(ev.telegram_id).cumsum()
    visits = ev.groupby(["telegram_id", "visit"]).agg(
        start=("ts", "min"), end=("ts", "max"), events=("ts", "size"),
    ).reset_index()
    visits["minutes"] = (visits.end - visits.start).dt.total_seconds() / 60
    return visits


def fmt_duration(seconds: float) -> str:
    if pd.isna(seconds) or seconds <= 0:
        return "—"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60}m"


# ---------------------------------------------------------------- sidebar

st.sidebar.header("Data source")
default_db = os.environ.get("MTB_DB", "snapshot.db")
db_path = st.sidebar.text_input("Snapshot path", value=default_db)

if not os.path.exists(db_path):
    st.error(f"No database at `{db_path}`.")
    st.caption(
        "Take a snapshot on the VPS with `bash scripts/backup_db.sh`, then "
        "`scp` the file from /root/backups down to this folder."
    )
    st.stop()

data = load(db_path)
users, sessions, events = data["users"], data["sessions"], data["events"]

if sessions.empty:
    st.warning("No exercise sessions in this snapshot yet.")
    st.stop()

days = st.sidebar.slider("Window (days)", 7, 365, 30)
cutoff = pd.Timestamp.now() - pd.Timedelta(days=days)
recent = sessions[sessions.started_at >= cutoff]
recent_events = events[events.ts >= cutoff] if not events.empty else events

st.sidebar.caption(
    f"{len(sessions)} sessions total · {len(recent)} in window\n\n"
    f"{'Timing data present' if sessions.duration_s.notna().any() else 'No timing data yet'}"
)

# ---------------------------------------------------------------- KPIs

st.title("🧠 Mental Training — Analytics")

scored_recent = scored(recent)
trained_seconds = recent.duration_s.sum(skipna=True)

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Users", len(users))
c2.metric("Tests (window)", len(scored_recent))
c3.metric(
    "Avg score",
    f"{scored_recent.pct.mean():.0f}%" if not scored_recent.empty else "—",
)
c4.metric("Training time", fmt_duration(trained_seconds))
c5.metric(
    "Active 7d",
    int((pd.to_datetime(users.last_active_at)
         > pd.Timestamp.now() - pd.Timedelta(days=7)).sum()),
)

if recent.duration_s.notna().sum() == 0:
    st.info(
        "No round in this window recorded a duration. Sessions finished before "
        "the analytics release have none — time charts fill in from here on."
    )

st.caption(
    "Training time = study + quiz + listening, measured per round. Rounds with "
    "no recorded duration (training mode, interrupted rounds) are excluded, so "
    "this is a floor, never an overestimate."
)

# ---------------------------------------------------------------- time

st.header("Time on task")

timed = recent[recent.duration_s.notna()]
if timed.empty:
    st.caption("Nothing to show until rounds with durations accumulate.")
else:
    left, right = st.columns(2)

    daily = timed.groupby("date").duration_s.sum().div(60).reset_index(name="minutes")
    left.plotly_chart(
        px.bar(daily, x="date", y="minutes", title="Minutes trained per day"),
        use_container_width=True,
    )

    per_ex = timed.groupby("exercise_type").duration_s.sum().div(60).reset_index(name="minutes")
    right.plotly_chart(
        px.pie(per_ex, names="exercise_type", values="minutes",
               title="Time split by exercise", hole=0.45),
        use_container_width=True,
    )

    per_user = timed.groupby("display_name").agg(
        minutes=("duration_s", lambda s: s.sum() / 60),
        rounds=("id", "size"),
    ).reset_index().sort_values("minutes", ascending=False)
    st.plotly_chart(
        px.bar(per_user, x="display_name", y="minutes", hover_data=["rounds"],
               title="Minutes trained per user"),
        use_container_width=True,
    )

    st.subheader("Commitment vs progress")
    st.caption(
        "Each dot is one member: minutes invested against how fast their score "
        "is climbing (percentage points per test). Above the line = the time is "
        "paying off. Members with fewer than 3 scored tests are omitted — a "
        "trend needs more than two points."
    )
    slopes = (
        scored(sessions).groupby("display_name")
        .apply(accuracy_slope, include_groups=False)
        .rename("slope").reset_index()
    )
    effort = (
        sessions[sessions.duration_s.notna()]
        .groupby("display_name").duration_s.sum().div(60)
        .rename("minutes").reset_index()
    )
    combo = slopes.merge(effort, on="display_name", how="inner").dropna()
    if combo.empty:
        st.caption("Not enough history yet — needs 3+ scored tests per member.")
    else:
        st.plotly_chart(
            px.scatter(
                combo, x="minutes", y="slope", text="display_name",
                labels={"minutes": "Minutes trained", "slope": "Score gain per test (pp)"},
                title="Time invested vs improvement rate",
            ).update_traces(textposition="top center"),
            use_container_width=True,
        )

# ---------------------------------------------------------------- scores

st.header("Performance")

if scored_recent.empty:
    st.caption("No scored tests in this window.")
else:
    left, right = st.columns(2)
    trend = scored_recent.groupby("date").pct.mean().reset_index()
    left.plotly_chart(
        px.line(trend, x="date", y="pct", markers=True,
                title="Average score over time"),
        use_container_width=True,
    )
    right.plotly_chart(
        px.box(scored_recent, x="difficulty", y="pct", title="Scores by difficulty"),
        use_container_width=True,
    )

    progress = scored_recent.groupby("display_name").agg(
        tests=("pct", "size"), avg=("pct", "mean"), best=("pct", "max"),
    ).reset_index().sort_values("avg", ascending=False)
    st.plotly_chart(
        px.bar(progress, x="display_name", y="avg", hover_data=["tests", "best"],
               title="Average score per user"),
        use_container_width=True,
    )

    st.subheader("Where people stop")
    st.caption(
        "Volume and accuracy by list size — a size with many attempts and a low "
        "average is where members hit the wall."
    )
    by_count = scored_recent.dropna(subset=["count"]).groupby("count").agg(
        attempts=("pct", "size"), avg=("pct", "mean"),
    ).reset_index()
    if not by_count.empty:
        st.plotly_chart(
            px.bar(by_count, x="count", y="attempts", color="avg",
                   color_continuous_scale="RdYlGn", range_color=[0, 100],
                   labels={"count": "Words / pairs", "attempts": "Attempts",
                           "avg": "Avg %"},
                   title="Attempts by list size, coloured by average score"),
            use_container_width=True,
        )

# ---------------------------------------------------------------- activity

st.header("Engagement")
st.caption(
    f"Reconstructed from the raw interaction stream by splitting on gaps longer "
    f"than {IDLE_GAP_MIN} minutes. Approximate by construction — Telegram sends "
    "no app-open or idle signal. Use it for patterns, never as a member's "
    "training time."
)

if recent_events.empty:
    st.caption("No interaction events in this window yet.")
else:
    left, right = st.columns(2)
    per_kind = recent_events.groupby(
        [recent_events.ts.dt.date, "kind"]
    ).size().reset_index(name="events")
    per_kind.columns = ["date", "kind", "events"]
    left.plotly_chart(
        px.bar(per_kind, x="date", y="events", color="kind",
               title="Interactions per day"),
        use_container_width=True,
    )

    top_screens = (
        recent_events[recent_events.kind == "callback"]
        .detail.value_counts().head(15).reset_index()
    )
    top_screens.columns = ["screen", "taps"]
    right.plotly_chart(
        px.bar(top_screens, x="taps", y="screen", orientation="h",
               title="Most-used screens"),
        use_container_width=True,
    )

    visits = sessionize(recent_events)
    v1, v2, v3 = st.columns(3)
    v1.metric("Visits", len(visits))
    v2.metric("Median visit", fmt_duration(visits.minutes.median() * 60))
    v3.metric("Events per visit", f"{visits.events.mean():.1f}")

# ---------------------------------------------------------------- drilldown

st.header("Member detail")

names = sorted(sessions.display_name.unique())
who = st.selectbox("Member", names)
mine = sessions[sessions.display_name == who]
mine_scored = scored(mine)

d1, d2, d3, d4 = st.columns(4)
d1.metric("Scored tests", len(mine_scored))
d2.metric("Avg score", f"{mine_scored.pct.mean():.0f}%" if not mine_scored.empty else "—")
d3.metric("Total trained", fmt_duration(mine.duration_s.sum(skipna=True)))
slope = accuracy_slope(mine_scored) if not mine_scored.empty else np.nan
d4.metric("Trend", f"{slope:+.2f} pp/test" if not pd.isna(slope) else "—")

if not mine_scored.empty:
    st.plotly_chart(
        px.scatter(
            mine_scored, x="started_at", y="pct", color="difficulty",
            size=mine_scored["count"].fillna(5), hover_data=["mode", "format"],
            title=f"{who} — every scored test",
        ),
        use_container_width=True,
    )

st.dataframe(
    mine[["started_at", "exercise_type", "mode", "difficulty", "count",
          "score", "max_score", "pct", "duration_s"]]
    .sort_values("started_at", ascending=False),
    use_container_width=True, hide_index=True,
)

st.download_button(
    "Download this member's sessions (CSV)",
    mine.to_csv(index=False).encode("utf-8-sig"),
    file_name=f"{who}_sessions.csv",
    mime="text/csv",
)
