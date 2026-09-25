"""
uScore correlation analysis: how well does the pitch-SHAPE uScore model line
up with actual pitch OUTCOMES?

uScore (quotient/display_score) is built entirely from velocity, movement,
spin, active spin, usage, and delivery -- it never looks at what actually
happened when the pitch was thrown. This script checks that assumption
against reality, using outcome columns Baseball Savant's Statcast Search CSV
already includes on every pitch (and which the main refresh pipeline pulls
down but never uses, since scoring doesn't need them).

What it does, in order:
  1. Reuses fetch_pitch_events() and normalize_player_id() from refresh.py
     -- the exact same Statcast Search fetch/retry/chunking logic the daily
     refresh already uses -- to pull every pitch thrown this season.
  2. Aggregates the OUTCOME columns on those same rows (which refresh.py's
     own aggregation step ignores) to one row per (player_id, pitch_type):
       - whiff%            = swinging strikes / swings
       - chase%             = swings outside the zone / pitches outside the zone
       - xwOBA against      = mean Statcast xwOBA on batted balls only
       - run value prevented / 100 pitches, from delta_run_exp
  3. Pulls this season's quotient/display_score straight from Supabase --
     the same numbers already live on the site -- for every
     (player_id, pitch_type).
  4. Joins the two on (player_id, pitch_type) and reports each pitch type's
     Spearman rank correlation between quotient and each outcome metric,
     computed WITHIN pitch type (matching how uScore itself only ever
     compares a pitch to others of the same type), plus a pooled
     cross-pitch-type number for reference.

This needs live internet access to Baseball Savant and to your Supabase
project, which the sandbox that wrote this script doesn't have. Run it
locally with the same environment variables refresh.py uses, or as a
one-off GitHub Actions job, and send back the printed table (or the
correlation_report.md file it writes) for an honest read of what it shows.

Usage:
    cd pipeline
    SUPABASE_URL=... SUPABASE_SERVICE_ROLE_KEY=... python3 correlation_analysis.py

Optional: USCORE_SEASON (defaults to the current year, same as refresh.py)
and USCORE_MIN_PITCHES (defaults to 25, same sample-size floor the
leaderboard itself uses) to change which season/threshold this checks.

Needs scipy for p-values (`pip install scipy`); it falls back to pandas'
Spearman correlation (same rho, no p-value) if scipy isn't installed.
"""
from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd
from supabase import create_client

# Reuse the exact same fetch/normalize machinery the daily refresh already
# uses, rather than duplicating Savant-endpoint/retry/chunking logic here --
# if Savant ever changes shape, both scripts get the fix from one place.
# Run this script from the pipeline/ directory (or with pipeline/ on
# PYTHONPATH) so this import resolves.
from refresh import (
    fetch_pitch_events,
    normalize_player_id,
    SEASON,
    MIN_PITCHES,
    SUPABASE_URL,
    SUPABASE_SERVICE_KEY,
)

try:
    from scipy.stats import spearmanr
    HAVE_SCIPY = True
except ImportError:
    HAVE_SCIPY = False


def spearman(x: pd.Series, y: pd.Series) -> tuple[float, int]:
    """Spearman correlation + the n it was computed on, dropping any row
    where either side is missing (e.g. a pitch type with zero batted balls
    this season has no xwOBA to correlate against)."""
    paired = pd.DataFrame({"x": x, "y": y}).dropna()
    n = len(paired)
    if n < 5:
        return float("nan"), n
    if HAVE_SCIPY:
        rho, _ = spearmanr(paired["x"], paired["y"])
        return float(rho), n
    # Fallback if scipy isn't installed: pandas' own rank-based correlation
    # gives the same rho as scipy.stats.spearmanr for a clean pairwise column.
    return float(paired["x"].corr(paired["y"], method="spearman")), n


def compute_outcome_metrics(events: pd.DataFrame) -> pd.DataFrame:
    """Aggregate raw pitch-level Statcast rows to one row per
    (player_id, pitch_type) with whiff%, chase%, xwOBA against, and run
    value -- the outcome side of the ledger the scoring model never looks
    at.

    Column meanings, straight from Savant's documented Statcast Search CSV
    (https://baseballsavant.mlb.com/csv-docs):
      description  -- per-pitch result: 'swinging_strike', 'foul',
                       'hit_into_play', 'ball', 'called_strike', etc.
      zone         -- 1-9 = in the strike zone, 11-14 = outside it.
      estimated_woba_using_speedangle -- Savant's xwOBA for BATTED BALLS
                       only (an exit-velo + launch-angle model); NaN on
                       every pitch that wasn't put in play.
      delta_run_exp -- change in run expectancy this pitch caused, credited
                       to the OFFENSE. A swinging strike lowers the
                       offense's run expectancy, so it's negative; negating
                       it gives "runs saved by the pitcher on this pitch" --
                       higher = better for the pitcher, matching quotient's
                       own "higher = better" direction.
    """
    df = normalize_player_id(events, "statcast_search (outcomes)")
    if "pitch_type" not in df.columns:
        raise RuntimeError(
            f"statcast_search (outcomes): no 'pitch_type' column. Columns were: {list(df.columns)}"
        )
    if "description" not in df.columns:
        raise RuntimeError(
            f"statcast_search (outcomes): no 'description' column -- can't compute whiff/chase "
            f"without it. Columns were: {list(df.columns)}"
        )

    # Guard columns that might be absent so the rest of the function doesn't
    # need separate branches -- an all-NaN column just makes that metric
    # come out NaN for everyone, which the report already handles.
    for col in ("zone", "delta_run_exp", "estimated_woba_using_speedangle"):
        if col not in df.columns:
            df[col] = np.nan

    swing_descriptions = {
        "foul", "foul_tip", "hit_into_play", "swinging_strike",
        "swinging_strike_blocked", "missed_bunt", "foul_bunt",
    }
    whiff_descriptions = {"swinging_strike", "swinging_strike_blocked", "missed_bunt"}

    zone = pd.to_numeric(df["zone"], errors="coerce")
    in_zone = zone.between(1, 9)
    out_of_zone = zone.between(11, 14)  # explicit membership, not just "~in_zone", so a missing/
                                        # unrecognized zone code doesn't get miscounted either way

    # De-fragment before adding several new columns one at a time -- df has
    # 700,000+ rows and 119 columns by this point (a season's worth of raw
    # Statcast pitches), and inserting columns individually into a frame
    # that size is what pandas' "highly fragmented" warning was flagging.
    # Harmless, just noisy and slightly wasteful; a plain .copy() re-lays it
    # out contiguously in one shot.
    df = df.copy()

    df["is_swing"] = df["description"].isin(swing_descriptions)
    df["is_whiff"] = df["description"].isin(whiff_descriptions)
    df["is_out_of_zone"] = out_of_zone
    df["is_chase"] = df["is_swing"] & out_of_zone
    df["run_value"] = -pd.to_numeric(df["delta_run_exp"], errors="coerce")
    df["xwoba_value"] = pd.to_numeric(df["estimated_woba_using_speedangle"], errors="coerce")

    grouped = df.groupby(["player_id", "pitch_type"])
    out = grouped.agg(
        n_pitches=("description", "size"),
        n_swings=("is_swing", "sum"),
        n_whiffs=("is_whiff", "sum"),
        n_out_of_zone=("is_out_of_zone", "sum"),
        n_chases=("is_chase", "sum"),
        run_value_per_pitch=("run_value", "mean"),
        xwoba_against=("xwoba_value", "mean"),  # mean over batted balls only -- non-batted-ball
                                                  # rows are NaN and pandas' mean() skips them
    ).reset_index()

    out["whiff_pct"] = np.where(out["n_swings"] > 0, out["n_whiffs"] / out["n_swings"], np.nan)
    out["chase_pct"] = np.where(out["n_out_of_zone"] > 0, out["n_chases"] / out["n_out_of_zone"], np.nan)
    out["run_value_per_100"] = out["run_value_per_pitch"] * 100

    return out[[
        "player_id", "pitch_type", "n_pitches",
        "whiff_pct", "chase_pct", "xwoba_against", "run_value_per_100",
    ]]


def fetch_quotients() -> pd.DataFrame:
    """This season's quotient/display_score for every qualifying
    (player_id, pitch_type), straight from Supabase -- the exact same
    numbers currently live on the site, so this correlates against what
    people are actually seeing rather than a fresh recomputation.

    Paginated explicitly with .range() rather than one unbounded .select():
    PostgREST (what Supabase's API runs on) caps a single response at a
    default row limit regardless of how many rows actually match the query,
    silently truncating rather than erroring. A single-page fetch happened
    to land mid-table and cut off almost every Splitter/Knuckle-Curve/
    Slider/Sweeper/Slurve row (whatever pitch types' rows simply weren't
    written yet by the time the cutoff hit) while leaving earlier pitch
    types looking mostly fine -- exactly the kind of silent, uneven data
    loss that's easy to miss without comparing against an independent row
    count."""
    supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    page_size = 1000
    all_rows: list[dict] = []
    start = 0
    while True:
        page = (
            supabase.table("pitch_metrics")
            .select("player_id, pitch_type, quotient, display_score")
            .eq("season", SEASON)
            .range(start, start + page_size - 1)
            .execute()
            .data
        )
        if not page:
            break
        all_rows.extend(page)
        if len(page) < page_size:
            break  # a partial page means this was the last one
        start += page_size
    if not all_rows:
        raise RuntimeError(
            f"No pitch_metrics rows found for season {SEASON} -- has the daily refresh run yet?"
        )
    return pd.DataFrame(all_rows)


# quotient and display_score are a monotonic transform of each other
# (display_score = 100 + 10 * zscore(quotient), computed within the exact
# same per-pitch-type groups), so they correlate IDENTICALLY against
# anything else -- only quotient needs to be tested.
METRICS = [
    ("whiff_pct", "Whiff%", 1),
    ("chase_pct", "Chase%", 1),
    ("xwoba_against", "xwOBA against (batted balls)", -1),  # lower xwOBA is GOOD for the
                                                              # pitcher, so this gets flipped
                                                              # before reporting -- a positive
                                                              # number always means "quotient
                                                              # and quality agree", across
                                                              # every column in the table
    ("run_value_per_100", "Run value prevented / 100 pitches", 1),
]


def correlation_row(label: str, n: str, group: pd.DataFrame) -> str:
    cells = [label, n]
    for col, _, sign in METRICS:
        rho, n_pairs = spearman(group["quotient"], sign * group[col])
        cells.append(f"{rho:+.2f} (n={n_pairs})" if not np.isnan(rho) else f"n/a (n={n_pairs})")
    return "| " + " | ".join(cells) + " |"


def build_report(merged: pd.DataFrame) -> str:
    lines = [
        "# uScore correlation report",
        "",
        f"Generated {datetime.now().strftime('%Y-%m-%d %H:%M')} against season {SEASON} data "
        f"(pitchers/pitch-types with at least {MIN_PITCHES} qualifying pitches, same floor the "
        "leaderboard itself uses).",
        "",
        "Spearman rank correlation between `quotient` (uScore's underlying number -- "
        "`display_score` is a monotonic rescaling of the same thing, so it correlates "
        "identically) and four outcome metrics uScore never sees: whiff%, chase%, xwOBA "
        "allowed on batted balls, and run value prevented per 100 pitches. Positive means "
        "\"more unique pitches tend to perform better\" on that metric; near zero means no "
        "relationship; negative means the opposite of what you'd hope for a quality metric.",
        "",
        "| Pitch type | n pitchers | " + " | ".join(label for _, label, _ in METRICS) + " |",
        "|---|---|" + "---|" * len(METRICS),
    ]
    for pitch_type, group in merged.groupby("pitch_type"):
        lines.append(correlation_row(pitch_type, str(len(group)), group))

    lines += [
        "",
        "## Overall (all pitch types pooled)",
        "",
        "The table above is computed **within** each pitch type, matching how uScore itself "
        "only ever compares a pitch to others of the same type. The row below pools every "
        "pitch type together for reference, but it's a weaker test -- it can pick up "
        "differences **between** pitch types (e.g. sliders whiff more than sinkers "
        "league-wide) that have nothing to do with whether uScore ranks pitchers well "
        "**within** a type.",
        "",
        "| | n pitchers | " + " | ".join(label for _, label, _ in METRICS) + " |",
        "|---|---|" + "---|" * len(METRICS),
        correlation_row("All types (pooled)", str(len(merged)), merged),
        "",
        "**Rough reading guide** (typical for a single-season, pitch-shape-only metric like "
        "this -- not a hard rule): |rho| < 0.15 is essentially no relationship, 0.15-0.3 is "
        "weak but real, 0.3-0.5 is moderate and worth taking seriously, and > 0.5 would be "
        "unusually strong for a metric that ignores results entirely. Even a true Stuff+-style "
        "model, trained explicitly to predict outcomes, typically lands in the 0.3-0.5 range "
        "against a single season of results -- uScore doesn't need to beat that to be doing "
        "something real, since it was never fit to outcomes in the first place.",
    ]
    return "\n".join(lines)


def main():
    print(f"Fetching this season's ({SEASON}) pitch-level Statcast data for outcome metrics...")
    events = fetch_pitch_events()

    outcomes = compute_outcome_metrics(events)
    outcomes = outcomes[outcomes["n_pitches"] >= MIN_PITCHES]
    print(f"Outcome metrics computed for {len(outcomes)} (pitcher, pitch type) pairs "
          f"with >= {MIN_PITCHES} pitches.")

    print("Fetching this season's quotient/display_score from Supabase...")
    quotients = fetch_quotients()
    print(f"Loaded {len(quotients)} (pitcher, pitch type) rows from pitch_metrics.")

    merged = outcomes.merge(quotients, on=["player_id", "pitch_type"], how="inner")
    print(f"Matched {len(merged)} (pitcher, pitch type) pairs across both sources.")
    if merged.empty:
        raise RuntimeError(
            "No rows matched between Statcast outcomes and Supabase quotients -- check that "
            "SEASON/USCORE_SEASON matches between this run and your last refresh."
        )

    if not HAVE_SCIPY:
        print("NOTE: scipy isn't installed -- falling back to pandas' Spearman correlation "
              "(same rho, just without scipy's p-values). `pip install scipy` for p-values too.")

    report = build_report(merged)
    print("\n" + report)

    with open("correlation_report.md", "w") as f:
        f.write(report)
    print("\nWrote correlation_report.md -- send this file (or paste its contents) back for an honest read.")


if __name__ == "__main__":
    main()
