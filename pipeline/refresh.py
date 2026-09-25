"""
uScore daily refresh pipeline.

What this script does, in order:
  1. Pulls every individual pitch thrown this season from Baseball Savant's
     Statcast Search CSV export (the same underlying endpoint the popular
     `pybaseball` library uses -- documented and stable, unlike the
     "leaderboard" pages, which turned out to have several dead ends: one
     had only outcome stats with no velocity/movement, another packed each
     pitch type into a single unparseable column meant for Savant's own
     chart, not for external use).
  2. Averages that pitch-level data into one row per pitcher per pitch type
     (velocity, spin, movement, usage, extension).
  3. Pulls active-spin% from Savant's active-spin leaderboard.
  4. Pulls arm angle / release point from Savant's arm-angle leaderboard.
  5. Recomputes the uScore model (same math as the Excel workbook: z-scored
     "uniqueness" quotients per pitch type, the funky-delivery dampening fix,
     and the arsenal-diversity multiplier) fresh against THIS run's league.
  6. Writes the results into Supabase (pitchers + pitch_metrics tables).
  7. Logs the run (success/failure, row counts) to refresh_log.

This is meant to run unattended once a day via GitHub Actions (see
.github/workflows/refresh.yml). It is NOT meant to be run inside a network
sandbox with no internet access -- it needs to reach baseballsavant.mlb.com.

A NOTE ON HOW THIS WAS BUILT:
Savant's leaderboard pages don't publish a documented, stable API, so
getting this pipeline right took several rounds against real GitHub Actions
runs: a wrong leaderboard, a wrong id column, wrong parameter names, a
leaderboard whose CSV wasn't actually tabular data. Each was caught by print
statements in the code below and fixed from the actual Actions log. This
version pulls from Savant's documented Statcast Search CSV endpoint instead
of a leaderboard page for the core pitch data, which should be far more
stable going forward. If a future run still fails, the same pattern
applies: the log shows the real column names Savant returned, which is
enough to fix it without any coding on your end -- just send me the log.
"""
from __future__ import annotations

import os
import sys
import math
import time
import traceback
from datetime import datetime, timezone
from io import StringIO

import numpy as np
import requests
import pandas as pd
from supabase import create_client

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

SEASON = int(os.environ.get("USCORE_SEASON", datetime.now().year))
MIN_PITCHES = int(os.environ.get("USCORE_MIN_PITCHES", 25))  # per pitch type, to filter out tiny samples

# Savant's "player_type": "pitcher" filter means "whoever was on the mound
# for this pitch" -- it has no concept of a player's primary position, so a
# position player taking the mound for an inning in a blowout is included
# exactly like a real pitcher. Their pitches are wildly unlike an actual
# pitcher's (much slower, unusual movement, often from an unpracticed arm
# slot), and even 25-30 of them can clear the per-pitch-type MIN_PITCHES bar
# above for a single pitch type, producing a leaderboard entry that's a
# statistical freak next to real pitchers rather than a meaningful one (this
# is exactly what a -64 Slider score turned out to be -- a position player,
# not a data bug). A real MLB pitcher, even a September call-up with a
# handful of appearances, throws several hundred pitches across a season at
# minimum; an incidental mound appearance is almost always under a few dozen
# total. Filtering on TOTAL pitches across every type this season (not just
# one type) is a simple, reliable way to separate the two without needing
# roster/position data from a different source.
MIN_SEASON_PITCHES_TO_QUALIFY = int(os.environ.get("USCORE_MIN_SEASON_PITCHES", 100))

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; uScoreBot/1.0)"}

# Every pitch type we score, its display label, and which quotient group it
# rolls into. Keys match Savant's own 2-letter pitch_type codes exactly, so
# no renaming is needed against the raw Statcast data.
PITCH_TYPES = {
    "FF": {"label": "4-Seam Fastball", "group": "4-Seam"},
    "SI": {"label": "Sinker",          "group": "Sinker"},
    "FC": {"label": "Cutter",          "group": "Cutter"},
    "CH": {"label": "Changeup",        "group": "Changeup"},
    "FS": {"label": "Splitter",        "group": "Changeup"},
    "FO": {"label": "Forkball",        "group": "Changeup"},
    "CU": {"label": "Curveball",       "group": "Curve"},
    "KC": {"label": "Knuckle Curve",   "group": "Curve"},
    "CS": {"label": "Slow Curve",      "group": "Curve"},
    "SL": {"label": "Slider",          "group": "Slider"},
    "ST": {"label": "Sweeper",         "group": "Slider"},
    "SV": {"label": "Slurve",          "group": "Slider"},
}

# Active-spin quotient shape per pitch type, matching the Excel model:
#   "signed"     -> higher active spin is better (4-Seam, Cutter)
#   "signed_neg" -> lower active spin is better (Sinker -- more seam-shifted
#                   wake / less "true" spin reads as more unique/effective)
#   "abs"        -> distance from the league mean either direction is what's
#                   rewarded (offspeed/breaking pitches)
#   None         -> Savant doesn't publish active-spin for this pitch type
ACTIVE_SPIN_SHAPE = {
    "FF": "signed", "SI": "signed_neg", "FC": "signed",
    "CH": "abs", "FS": "abs", "FO": None,
    "CU": "abs", "KC": None, "CS": None,
    "SL": "abs", "ST": "abs", "SV": "abs",
}

# Weight each pitch type's active-spin quotient carries in that pitch's
# Ceiling formula (0 where Savant has no active-spin reading for the type).
ACTIVE_SPIN_WEIGHT = {
    "FF": 0.08, "SI": 0.08, "FC": 0.05,
    "CH": 0.10, "FS": 0.10, "FO": 0.0,
    "CU": 0.10, "KC": 0.0, "CS": 0.0,
    "SL": 0.10, "ST": 0.10, "SV": 0.10,
}

IVB_WEIGHT = 0.95
HORIZ_WEIGHT = 0.25
SPIN_WEIGHT = 0.10
DELIVERY_WEIGHT_IN_USCORE = 0.3

# usage_rate is dampened with an exponent < 1 rather than applied linearly.
# Plain linear usage let raw usage share swing a pitch's score by as much as
# (or more than) real differences in shape -- e.g. two cutters with a
# similar underlying quality but an 8x usage gap ended up with a >50x
# quotient gap, almost all of it from usage alone. usage_rate is still meant
# to separate "a freakish pitch rarely used" from "a real, trusted weapon"
# (that's the whole reason it's here), just without letting it swamp the
# pitch's own velocity/movement/spin. 0.75 was chosen as a middle ground
# after comparing it against full sqrt (0.5) on the real leaderboard: sqrt
# cut usage's influence so far that even elite, heavily-used pitches (e.g. a
# slider thrown half the time) lost meaningful ground to rarely-thrown ones,
# which undersells usage's intended signal; 0.75 fixes the worst of the
# linear-scaling distortion while keeping a high-usage pitch clearly ahead
# of an identically-shaped low-usage one. Can be dialed up/down later if the
# live results call for it.
USAGE_RATE_EXPONENT = 0.75

# Whether induced vertical break should reward a specific direction
# ("signed" -- more "ride"/less drop rewarded, the default), the opposite
# direction ("signed_neg" -- more drop rewarded), or distance from
# league-average IVB in EITHER direction ("abs" -- for pitches where tilt
# itself is the weapon and there's no single "better" direction: a
# slider-family pitch that dives more than average can be just as much of a
# threat as one that sweeps/rises more, and a cutter's value can come from
# exceptional depth (e.g. Drew Rasmussen's, which drops far more than a
# typical cutter and grades as his best pitch by results) just as easily as
# from exceptional ride).
#   - Changeups, curveballs, knuckle curves, and splitters are valued
#     specifically for dropping MORE than average (real deception/tunneling
#     off the fastball) -- "signed_neg", so more drop is rewarded, not
#     penalized. (Confirmed on real splitter examples: Gausman's -- one of
#     the most respected splitters in the game -- and Sasaki's were both
#     landing at/below league average under the old signed treatment, which
#     rewarded LESS drop on a pitch whose whole purpose is heavy, late
#     plunge.)
#   - Sliders, sweepers, slurves, cutters, and sinkers get real value from
#     either kind of unusual tilt -- "abs". A sinker's defining trait is
#     heavy sink (confirmed on Logan Webb, whose near-zero IVB -- elite,
#     maximal sink -- was ranking him near dead last under the old signed
#     treatment), but an unusually high-riding sinker/two-seam hybrid can
#     also be a real, distinct weapon, so both extremes are rewarded rather
#     than only one.
# Defaults to "signed" for any pitch type not listed here.
IVB_SHAPE = {
    "SL": "abs", "ST": "abs", "SV": "abs", "FC": "abs", "SI": "abs",
    "CH": "signed_neg", "CU": "signed_neg", "KC": "signed_neg", "FS": "signed_neg",
}

# Whether velocity should reward being faster ("signed", the default),
# reward being SLOWER than average ("signed_neg"), or reward distance from
# league-average velocity in EITHER direction ("abs"). Curveballs and
# knuckle curves don't have one "better" speed -- a firm, hard curve (more
# like Glasnow's) and a slow, loopy one (more like Valdez's, with a huge gap
# off his fastball) can both be elite for different reasons, so "abs"
# rewards either extreme. Splitters work the same way -- confirmed on real
# examples: Duran's splitter is a weapon largely BECAUSE it's thrown at
# near-fastball velocity (97+ mph), while Gausman's and Imanaga's are
# weapons despite (or because of) being notably slow -- so, like a
# slider/curve, there's no single "better" speed, just distance from
# average in either direction. Defaults to "signed" for any pitch type not
# listed here.
#
# Changeups are the one exception, handled separately from this dict (see
# CH_VELO_GAP_WEIGHT / CH_RAW_VELO_WEIGHT below) rather than through a
# simple shape override -- real changeup analysis was empirically confirmed
# to be different: what makes a changeup deceptive is largely its velocity
# SEPARATION from the pitcher's OWN fastball, not just being fast or slow in
# some absolute, cross-pitcher sense the way curves/sliders/splitters are.
VELO_SHAPE = {
    "CU": "abs", "KC": "abs", "FS": "abs",
}

# Changeup velocity is scored as a blend of two things, rather than a single
# shape flag like every other pitch type:
#   1. Velocity SEPARATION from the pitcher's own fastball (the harder of
#      their four-seam or sinker, whichever they throw) -- the main driver
#      of a changeup's deception, and the majority of the weight.
#   2. Raw changeup velocity itself, signed so faster is still rewarded --
#      a smaller, secondary term, since there's still real value in a firm
#      94 mph changeup over a loopy 78 mph one even at an identical gap off
#      the fastball (reaction time is governed by the actual pitch speed
#      too, not just the gap).
# The two weights sum to 1.0, the same total weight every other pitch type's
# single velocity term carries, so CH's velocity dimension stays on the same
# overall scale as the rest of the model -- just split between two signals
# instead of one.
CH_VELO_GAP_WEIGHT = 0.7
CH_RAW_VELO_WEIGHT = 0.3

# Per-pitch-type override for how much horizontal break counts toward the
# Ceiling formula, in place of the global HORIZ_WEIGHT. Sliders, sweepers,
# and slurves get real, distinct value from horizontal movement
# specifically -- a pitcher's slider can be a weapon because of exceptional
# sweep even with unremarkable depth, which the default fastball-tuned
# weighting (where vertical movement dominates) badly undersells.
HORIZ_WEIGHT_OVERRIDE = {
    "SL": 0.65, "ST": 0.65, "SV": 0.5,
}

# Whether horizontal break should reward a specific direction ("signed" --
# arm-side movement rewarded, the default), the opposite direction
# ("signed_neg" -- glove-side movement rewarded), or distance from league
# average in EITHER direction ("abs"). This has to match each pitch type's
# own defining/characteristic movement under the handedness-normalized
# convention above (positive = arm-side, negative = glove-side for
# everyone, regardless of throwing hand):
#   - Sinkers, changeups, splitters, and forkballs are defined by arm-side
#     run/fade -- "signed" (the default) is correct as-is.
#   - Cutters, curveballs (all three variants), sliders, sweepers, and
#     slurves are defined by glove-side break -- "signed_neg", so more
#     glove-side movement is rewarded rather than penalized. (Sliders and
#     sweepers were the first ones caught and fixed; curves, cutters, and
#     slurves had the exact same backwards-direction bug at the smaller
#     default weight, just less visibly.)
#   - Four-seam fastballs have no single "better" direction -- a classic
#     arm-side-running four-seamer and a cut-riding four-seamer (like
#     Justin Steele's) can both be plus pitches -- so "abs" rewards
#     distance from average in either direction rather than picking a side.
# Defaults to "signed" for any pitch type not listed here.
HORIZ_SHAPE = {
    "FF": "abs",
    "FC": "signed_neg",
    "CU": "signed_neg", "KC": "signed_neg", "CS": "signed_neg",
    "SL": "signed_neg", "ST": "signed_neg", "SV": "signed_neg",
}

# Every id column Savant has used across its various CSV exports, in
# priority order -- the first one found in a given export is treated as
# that pitcher's id.
PLAYER_ID_ALIASES = ["pitcher", "player_id", "pitcher_id", "entity_id", "mlbam_id", "mlb_id"]
PLAYER_NAME_ALIASES = ["player_name", "last_name, first_name", "pitcher_name", "entity_name", "name"]


def normalize_player_id(df: pd.DataFrame, source_label: str) -> pd.DataFrame:
    """Rename whichever id column is present to 'player_id' and force it to
    a consistent integer type, so merges across data sources never fail with
    a dtype or column-name mismatch even if Savant's export uses a different
    id column name than we expect."""
    if "player_id" not in df.columns:
        found = next((c for c in PLAYER_ID_ALIASES if c in df.columns), None)
        if found is None:
            raise RuntimeError(
                f"{source_label}: couldn't find a player-id column among "
                f"{PLAYER_ID_ALIASES} -- actual columns were {list(df.columns)}"
            )
        df = df.rename(columns={found: "player_id"})
    df["player_id"] = pd.to_numeric(df["player_id"], errors="coerce").astype("Int64")
    return df


# --------------------------------------------------------------------------
# 1. Pull every pitch thrown this season, then aggregate to pitcher x
#    pitch-type averages. This is Savant's documented Statcast Search CSV
#    export (https://baseballsavant.mlb.com/csv-docs) -- the same endpoint
#    the `pybaseball` library's statcast() function uses, so it's a stable,
#    well-tested source rather than a guess at an undocumented leaderboard.
# --------------------------------------------------------------------------

STATCAST_SEARCH_URL = "https://baseballsavant.mlb.com/statcast_search/csv"

# Column names Savant's per-pitch CSV export is documented to use. A few
# plausible fallbacks are listed too in case Savant renames something --
# the print statement below shows the real columns on every run either way.
PITCH_COLUMN_CANDIDATES = {
    "velo": ["release_speed"],
    "spin_rpm": ["release_spin_rate", "release_spin"],
    "ivb_in_raw": ["pfx_z"],           # feet; multiply by 12 for inches
    "horizontal_in_raw": ["pfx_x", "api_break_x_arm"],  # feet; multiply by 12
    "extension_ft": ["release_extension"],
}


STATCAST_SEARCH_ROW_CAP = 25000  # Savant silently caps a single request at this many rows
CHUNK_DAYS = 3  # small enough that even the busiest 3-day stretch of a full slate stays under the cap
MAX_RETRIES = 4
RETRY_BACKOFF_SECONDS = 5  # doubles each retry: 5s, 10s, 20s, 40s


def get_with_retries(url: str, params: dict | None = None, timeout: int = 60) -> requests.Response:
    """A plain requests.get, but retried with exponential backoff on
    transient failures (connection errors, timeouts, and 5xx server
    errors). Savant's endpoints occasionally return a one-off 502/503 under
    load -- across ~50-90 requests in a full pipeline run, hitting that at
    least once is expected, and a retry almost always clears it. A 4xx
    error (a real, permanent problem like a bad URL) is NOT retried -- it
    fails immediately, same as before, so a genuine bug still surfaces
    right away instead of being masked by retries."""
    last_exc = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
            if resp.status_code >= 500:
                raise requests.exceptions.HTTPError(
                    f"{resp.status_code} Server Error for url: {resp.url}", response=resp
                )
            resp.raise_for_status()
            return resp
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout,
                requests.exceptions.HTTPError) as e:
            is_server_or_network_error = (
                not isinstance(e, requests.exceptions.HTTPError)
                or (e.response is not None and e.response.status_code >= 500)
            )
            last_exc = e
            if not is_server_or_network_error or attempt == MAX_RETRIES:
                raise
            wait = RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1))
            print(f"WARNING: request to {url} failed ({e}) -- retrying in {wait}s "
                  f"(attempt {attempt}/{MAX_RETRIES})")
            time.sleep(wait)
    raise last_exc  # pragma: no cover -- loop always returns or raises above


def _fetch_pitch_events_chunk(start: str, end: str) -> pd.DataFrame:
    params = {
        "all": "true",
        "hfGT": "R|PO|",  # regular season + postseason only -- spring training rosters are full of
                          # non-roster minor-league invitees facing MLB hitters, which was mixing
                          # minor leaguers into the leaderboard alongside real MLB pitchers
        "hfSea": f"{SEASON}|",
        "player_type": "pitcher",
        "game_date_gt": start,
        "game_date_lt": end,
        "min_pitches": "0",
        "min_results": "0",
        "group_by": "name",
        "sort_col": "pitches",
        "player_event_sort": "h_launch_speed",
        "sort_order": "desc",
        "min_abs": "0",
        "type": "details",
    }
    resp = get_with_retries(STATCAST_SEARCH_URL, params=params, timeout=180)
    df = pd.read_csv(StringIO(resp.text), low_memory=False)
    if len(df) >= STATCAST_SEARCH_ROW_CAP:
        print(f"WARNING: chunk {start}..{end} returned {len(df)} rows -- likely hit Savant's "
              f"per-request cap ({STATCAST_SEARCH_ROW_CAP}); some pitches from this window may be missing. "
              "Consider lowering CHUNK_DAYS if this keeps happening.")
    return df


def fetch_pitch_events() -> pd.DataFrame:
    """Every individual pitch thrown by any pitcher this season, as one row
    per pitch, straight from Savant's Statcast Search CSV export.

    Savant caps a single request's CSV export at ~25,000 rows -- a full
    season is more like 600,000+ pitches league-wide, so one request would
    silently return only a small, order-biased slice. This instead pulls
    the season in small date windows and concatenates them, so the league
    z-scores downstream are calculated against the real, full-season data.
    """
    # hfGT above excludes spring training, and the regular season doesn't
    # start until late March, so starting the pull at Mar 15 skips several
    # weeks of guaranteed-empty windows (and requests) with no qualifying
    # games at all.
    season_start = datetime(SEASON, 3, 15)
    season_end = datetime.now()

    chunks = []
    window_start = season_start
    while window_start <= season_end:
        window_end = min(window_start + pd.Timedelta(days=CHUNK_DAYS), season_end)
        start_str = window_start.strftime("%Y-%m-%d")
        end_str = window_end.strftime("%Y-%m-%d")
        chunk = _fetch_pitch_events_chunk(start_str, end_str)
        if not chunk.empty:
            chunks.append(chunk)
        window_start = window_end + pd.Timedelta(days=1)

    if not chunks:
        raise RuntimeError(f"statcast_search: no pitch data returned for any window in {SEASON}")

    df = pd.concat(chunks, ignore_index=True)
    print(f"statcast_search: pulled {len(chunks)} date windows, {len(df)} total pitch rows, "
          f"{len(df.columns)} columns")
    print("statcast_search columns (first 40):", list(df.columns)[:40])
    return df


def compute_pitch_metrics_from_events(events: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Aggregate raw pitch-level rows into (a) one row per pitcher per pitch
    type with average velo/spin/movement/usage, and (b) one row per pitcher
    with a usage-weighted average extension."""
    df = normalize_player_id(events, "statcast_search")

    def first_present(candidates: list[str]) -> str | None:
        return next((c for c in candidates if c in df.columns), None)

    resolved = {metric: first_present(cands) for metric, cands in PITCH_COLUMN_CANDIDATES.items()}
    print("statcast_search resolved columns:", resolved)
    missing = [m for m, c in resolved.items() if c is None]
    if missing:
        raise RuntimeError(
            f"statcast_search: couldn't find columns for {missing} among "
            f"{PITCH_COLUMN_CANDIDATES}. Actual columns were: {list(df.columns)}"
        )

    if "pitch_type" not in df.columns:
        raise RuntimeError(f"statcast_search: no 'pitch_type' column. Columns were: {list(df.columns)}")

    df = df[df["pitch_type"].isin(PITCH_TYPES.keys())].copy()
    df["velo"] = pd.to_numeric(df[resolved["velo"]], errors="coerce")
    df["spin_rpm"] = pd.to_numeric(df[resolved["spin_rpm"]], errors="coerce")
    df["ivb_in"] = pd.to_numeric(df[resolved["ivb_in_raw"]], errors="coerce") * 12
    df["extension_ft"] = pd.to_numeric(df[resolved["extension_ft"]], errors="coerce")

    # Statcast's raw horizontal-break column isn't handedness-normalized: the
    # exact same physical movement (e.g. a changeup's arm-side fade) comes
    # back as a NEGATIVE number for a right-handed pitcher and a POSITIVE
    # number for a left-handed one, because it's measured from the catcher's
    # perspective, not relative to which arm threw it. Z-scoring that raw
    # value across a league that mixes both hands doesn't measure "how much
    # did this pitch move" -- it mostly measures "is this pitcher left-handed",
    # since every lefty's break lands on one side of the distribution and
    # every righty's lands on the other. Flip the sign for right-handers so
    # positive consistently means "arm-side" movement for everyone, and
    # negative consistently means "glove-side" -- the standard convention in
    # pitching analytics, and the only way this number is comparable across
    # a mixed-handed league.
    if "p_throws" not in df.columns:
        raise RuntimeError(
            f"statcast_search: no 'p_throws' (pitcher handedness) column -- needed to make "
            f"horizontal break comparable across left- and right-handed pitchers. "
            f"Actual columns were: {list(df.columns)}"
        )
    raw_horizontal_in = pd.to_numeric(df[resolved["horizontal_in_raw"]], errors="coerce") * 12
    p_throws_counts = df["p_throws"].astype(str).str.upper().value_counts().to_dict()
    print("p_throws raw value counts (sanity check -- expecting only 'R' and 'L'):", p_throws_counts)
    is_rhp = df["p_throws"].astype(str).str.upper().eq("R")
    df["horizontal_in"] = raw_horizontal_in.where(~is_rhp, -raw_horizontal_in)

    per_pitcher_totals = df.groupby("player_id").size().rename("total_pitches")

    grouped = df.groupby(["player_id", "pitch_type"]).agg(
        velo=("velo", "mean"),
        spin_rpm=("spin_rpm", "mean"),
        ivb_in=("ivb_in", "mean"),
        horizontal_in=("horizontal_in", "mean"),
        n_pitches=("velo", "size"),
    ).reset_index()

    grouped = grouped.merge(per_pitcher_totals, on="player_id", how="left")
    grouped["usage_rate"] = grouped["n_pitches"] / grouped["total_pitches"]
    n_before_position_player_filter = grouped["player_id"].nunique()
    grouped = grouped[grouped["total_pitches"] >= MIN_SEASON_PITCHES_TO_QUALIFY].copy()
    n_removed = n_before_position_player_filter - grouped["player_id"].nunique()
    if n_removed:
        print(f"Excluded {n_removed} player(s) with fewer than {MIN_SEASON_PITCHES_TO_QUALIFY} "
              f"total pitches this season (likely position players who took the mound briefly, "
              f"not real pitchers).")
    grouped = grouped[grouped["n_pitches"] >= MIN_PITCHES].copy()
    grouped["active_spin_pct"] = None

    pitch_metrics = grouped[[
        "player_id", "pitch_type", "velo", "spin_rpm", "ivb_in", "horizontal_in",
        "usage_rate", "active_spin_pct",
    ]]

    ext = df.dropna(subset=["extension_ft"])
    if ext.empty:
        extension = pd.DataFrame(columns=["player_id", "extension_ft"])
    else:
        extension = ext.groupby("player_id")["extension_ft"].mean().reset_index()

    return pitch_metrics, extension


# --------------------------------------------------------------------------
# 2. Pull active-spin data
# --------------------------------------------------------------------------

def fetch_active_spin() -> pd.DataFrame:
    """Active-spin% by pitcher and pitch type, from Savant's active-spin
    leaderboard. Returns columns: player_id, pitch_type, active_spin_pct.
    Savant only publishes this for FF/SI/FC/CH/CU/SL/ST/SV -- other pitch
    types simply won't have a row here, which is expected (handled below by
    treating missing = None, not 0)."""
    url = f"https://baseballsavant.mlb.com/leaderboard/active-spin?year={SEASON}&csv=true"
    resp = get_with_retries(url, timeout=30)
    raw = pd.read_csv(StringIO(resp.text))
    print("active-spin columns:", list(raw.columns))

    # Savant's active-spin export is wide (one column per pitch type, e.g.
    # "active_spin_fourseam", "active_spin_sinker", ...). Melt it to long
    # form so it lines up with pitch_metrics' one-row-per-type shape.
    spin_cols = [c for c in raw.columns if "active_spin" in c.lower()]
    col_to_type = {
        "fourseam": "FF", "4seam": "FF", "ff": "FF",
        "sinker": "SI", "si": "SI",
        "cutter": "FC", "fc": "FC",
        "changeup": "CH", "ch": "CH",
        "curve": "CU", "curveball": "CU", "cu": "CU",
        "slider": "SL", "sl": "SL",
        "sweeper": "ST", "st": "ST",
        "slurve": "SV", "sv": "SV",
    }
    id_col = next((c for c in PLAYER_ID_ALIASES if c in raw.columns), None)
    if id_col is None:
        raise RuntimeError(
            f"active-spin: couldn't find a player-id column among {PLAYER_ID_ALIASES} "
            f"-- actual columns were {list(raw.columns)}"
        )
    long_rows = []
    for col in spin_cols:
        key = col.lower().replace("active_spin", "").replace("_", "")
        pt = col_to_type.get(key)
        if pt is None:
            continue
        for _, row in raw.iterrows():
            val = row[col]
            if pd.notna(val):
                long_rows.append({"player_id": row[id_col], "pitch_type": pt, "active_spin_pct": val})
    if not long_rows:
        print("WARNING: active-spin export matched none of the expected pitch-type "
              "column names -- active-spin quotients will be 0 for everyone this run. "
              f"Raw columns were: {list(raw.columns)}")
        return pd.DataFrame(columns=["player_id", "pitch_type", "active_spin_pct"])
    result = pd.DataFrame(long_rows)
    return normalize_player_id(result, "active-spin")


# --------------------------------------------------------------------------
# 3. Pull delivery data (arm angle, release point; extension comes from the
#    pitch-event aggregation above)
# --------------------------------------------------------------------------

def fetch_delivery_metrics() -> pd.DataFrame:
    """One row per pitcher: arm angle, release height, horizontal release
    point (extension is filled in separately, from the pitch-event
    aggregation, since this leaderboard doesn't publish it). Returns
    columns: player_id, pitcher_name, extension_ft (blank placeholder),
    arm_angle_deg, release_height_ft, horizontal_release_ft."""
    url = f"https://baseballsavant.mlb.com/leaderboard/pitcher-arm-angles?season={SEASON}&min=1&csv=true"
    resp = get_with_retries(url, timeout=30)
    raw = pd.read_csv(StringIO(resp.text))
    print("pitcher-arm-angles columns:", list(raw.columns))

    name_rename = {alias: "pitcher_name" for alias in PLAYER_NAME_ALIASES}
    # Confirmed live column names from this leaderboard's CSV export:
    # 'ball_angle' (arm angle), 'release_ball_z' (release height),
    # 'relative_release_ball_x' (horizontal release point).
    metric_rename = {
        "ball_angle": "arm_angle_deg",
        "arm_angle": "arm_angle_deg",
        "release_ball_z": "release_height_ft",
        "release_pos_z": "release_height_ft",
        "relative_release_ball_x": "horizontal_release_ft",
        "release_pos_x": "horizontal_release_ft",
    }
    rename = {**name_rename, **metric_rename}
    df = raw.rename(columns={c: rename[c] for c in raw.columns if c in rename})
    df = normalize_player_id(df, "pitcher-arm-angles")
    df["extension_ft"] = None

    keep = ["player_id", "pitcher_name", "extension_ft", "arm_angle_deg",
            "release_height_ft", "horizontal_release_ft"]
    missing = [c for c in keep if c not in df.columns]
    if missing:
        print(f"NOTE: pitcher-arm-angles export doesn't include {missing} "
              f"-- those fields will be blank this run. Raw columns were: {list(raw.columns)}")
    for col in keep:
        if col not in df.columns:
            df[col] = None
    return df[keep]


# --------------------------------------------------------------------------
# 3b. Today's probable starters, for the site's home-page "pitchers to watch
#     today" box. This is entirely separate from the uScore pitch-data
#     pipeline above and from MLB's own Statcast/Savant data -- it hits a
#     different source (MLB's own Stats API) and is wired up in run() to be
#     fully isolated: if this fails for any reason (MLB's feed down, a
#     response-shape change, no games today), it's caught and logged as a
#     warning there, and the main leaderboard refresh completes normally
#     either way.
# --------------------------------------------------------------------------

MLB_SCHEDULE_URL = "https://statsapi.mlb.com/api/v1/schedule"


def fetch_probable_starters(target_date: str) -> list[dict]:
    """Every probable starting pitcher for MLB games on `target_date`
    (YYYY-MM-DD), one row per starter with their team, opponent, and game
    time. Returns an empty list on an off day or before starters have been
    announced -- both normal, not errors."""
    params = {"sportId": 1, "date": target_date, "hydrate": "probablePitcher,team"}
    resp = get_with_retries(MLB_SCHEDULE_URL, params=params, timeout=30)
    payload = resp.json()

    rows = []
    for date_entry in payload.get("dates", []):
        for game in date_entry.get("games", []):
            game_time = game.get("gameDate")
            teams = game.get("teams", {})
            for side, other_side in (("home", "away"), ("away", "home")):
                team_info = teams.get(side, {})
                pitcher = team_info.get("probablePitcher")
                if not pitcher or "id" not in pitcher:
                    continue
                rows.append({
                    "game_date": target_date,
                    "player_id": pitcher["id"],
                    "team": (team_info.get("team") or {}).get("name"),
                    "opponent": (teams.get(other_side, {}).get("team") or {}).get("name"),
                    "game_time": game_time,
                })
    return rows


# --------------------------------------------------------------------------
# 4. uScore math -- mirrors the Excel workbook, but z-scores are computed
#    fresh against this run's league each time (self-calibrating), rather
#    than the fixed historical constants baked into the one-off workbook.
# --------------------------------------------------------------------------

def zscore(series: pd.Series) -> pd.Series:
    series = pd.to_numeric(series, errors="coerce")
    mean, std = series.mean(), series.std(ddof=0)
    if not std or math.isnan(std):
        return series.fillna(0) * 0.0
    return (series - mean) / std


def active_spin_quotient(series: pd.Series, shape: str | None) -> pd.Series:
    series = pd.to_numeric(series, errors="coerce")
    if shape is None or series.isna().all():
        return pd.Series(0.0, index=series.index)
    z = zscore(series.fillna(series.mean()))
    out = z.copy()
    if shape == "signed_neg":
        out = -z
    elif shape == "abs":
        out = z.abs()
    # mask back to 0 wherever the pitcher had no active-spin reading at all
    out[series.isna()] = 0.0
    return out


DELIVERY_MODIFIER_WEIGHT = 0.15  # how much a league-relative "how unusual is this
                                  # delivery" z-score shifts every pitch's quotient
DELIVERY_MODIFIER_MIN = 0.85
DELIVERY_MODIFIER_MAX = 1.20


def build_delivery_quotients(pitch_metrics_player_ids: pd.Series, delivery: pd.DataFrame) -> pd.DataFrame:
    """One row per pitcher with delivery z-scores, delivery_quotient/
    adj_delivery_quotient, and a bounded delivery_modifier -- shared by both
    the per-pitch quotient (as a multiplier, since an unusual release point
    plausibly makes every pitch a pitcher throws harder to pick up, not just
    one) and the pitchers table's own delivery columns, so the two can never
    drift out of sync with each other.

    `delivery` (the arm-angle leaderboard export) only covers a subset of
    pitchers -- a pitcher can clear MIN_PITCHES in the full-season pitch
    aggregation and appear in `pitch_metrics` without showing up on that
    leaderboard. Every player_id that pitch_metrics references needs a row
    here regardless (pitch_metrics.player_id is a foreign key into
    pitchers), so build the full player universe as the union of both
    sources first, then left-merge delivery's fields onto it -- pitchers
    missing from the arm-angle export just get a neutral (1.0) modifier and
    blank delivery metrics rather than being dropped entirely.
    """
    known_ids = set(delivery["player_id"])  # players actually present on the arm-angle leaderboard

    all_player_ids = pd.Index(
        pd.unique(pd.concat([delivery["player_id"], pitch_metrics_player_ids], ignore_index=True)),
        name="player_id",
    )
    base = pd.DataFrame({"player_id": all_player_ids})
    delivery = base.merge(delivery, on="player_id", how="left")

    for col in ["extension_ft", "arm_angle_deg", "release_height_ft", "horizontal_release_ft"]:
        # fillna(0) on the z-score itself (not the raw metric) treats a
        # missing delivery reading as "no deviation from league average" --
        # neutral, rather than letting a single NaN metric poison the whole
        # row's delivery_quotient/adj_delivery_quotient sum with NaN.
        delivery[f"{col}_z"] = zscore(delivery[col]).fillna(0.0)

    delivery["delivery_quotient"] = sum(
        delivery[f"{c}_z"].abs()
        for c in ["extension_ft", "arm_angle_deg", "release_height_ft", "horizontal_release_ft"]
    )
    delivery["adj_delivery_quotient"] = sum(
        delivery[f"{c}_z"].abs().pow(0.5)
        for c in ["extension_ft", "arm_angle_deg", "release_height_ft", "horizontal_release_ft"]
    )

    # A second, "meta" z-score: not how unusual the delivery is in absolute
    # terms, but how unusual it is relative to *other pitchers'* deliveries
    # this season. That keeps the modifier well-behaved and centered on 1.0
    # for a dead-average delivery, regardless of what scale adj_delivery_quotient
    # happens to land on in a given season, then bounded so no single pitcher's
    # delivery can swing every one of his pitches too far in either direction.
    delivery_uniqueness_z = zscore(delivery["adj_delivery_quotient"]).fillna(0.0)
    delivery["delivery_modifier"] = (
        1.0 + DELIVERY_MODIFIER_WEIGHT * delivery_uniqueness_z
    ).clip(DELIVERY_MODIFIER_MIN, DELIVERY_MODIFIER_MAX)

    # A pitcher missing from the arm-angle leaderboard has no real delivery
    # reading at all -- their z-scores were filled with 0 above just so the
    # rest of the math doesn't break, but 0 isn't "an average delivery," it's
    # "we don't know." Zscoring that placeholder against real deliveries
    # tends to land below the pack (most real deliveries pull the mean above
    # zero), which would incorrectly *penalize* pitchers for missing data
    # instead of staying neutral. Force those rows back to 1.0 explicitly.
    delivery.loc[~delivery["player_id"].isin(known_ids), "delivery_modifier"] = 1.0

    return delivery


def build_fastball_baseline(pitch_metrics: pd.DataFrame) -> pd.Series:
    """Each pitcher's hardest fastball-family pitch (four-seam or sinker --
    whichever is harder for that pitcher, since either can be the "primary"
    heater a changeup is meant to look like out of the hand) this season.
    Returns a Series of velocity indexed by player_id; a pitcher who throws
    neither simply has no entry (handled as a neutral/no-gap-signal case
    downstream, same philosophy as a pitcher missing from the arm-angle
    leaderboard getting a neutral delivery modifier rather than a penalty)."""
    fastball_rows = pitch_metrics[pitch_metrics["pitch_type"].isin(["FF", "SI"])]
    return fastball_rows.groupby("player_id")["velo"].max()


def compute_pitch_quotients(pitch_metrics: pd.DataFrame, active_spin_fallback: pd.DataFrame,
                             delivery_modifiers: pd.DataFrame) -> pd.DataFrame:
    df = pitch_metrics.copy()

    if not active_spin_fallback.empty:
        df = df.merge(
            active_spin_fallback, on=["player_id", "pitch_type"], how="left", suffixes=("", "_fallback")
        )
        if "active_spin_pct_fallback" in df.columns:
            df["active_spin_pct"] = df["active_spin_pct"].fillna(df["active_spin_pct_fallback"])
            df = df.drop(columns=["active_spin_pct_fallback"])

    df = df.merge(delivery_modifiers[["player_id", "delivery_modifier"]], on="player_id", how="left")
    df["delivery_modifier"] = df["delivery_modifier"].fillna(1.0)

    fastball_baseline = build_fastball_baseline(pitch_metrics)

    out_frames = []
    for pt, group in df.groupby("pitch_type"):
        group = group.copy()
        group["active_spin_quotient"] = active_spin_quotient(
            group["active_spin_pct"], ACTIVE_SPIN_SHAPE.get(pt)
        )
        velo_z = zscore(group["velo"])
        if pt == "CH":
            # Blend velocity-separation-from-own-fastball with raw velocity
            # (see CH_VELO_GAP_WEIGHT/CH_RAW_VELO_WEIGHT above) instead of a
            # single shape flag. A pitcher with no qualifying FF/SI this
            # season has no baseline to compare against -- treat the gap
            # term as neutral (0) for just those rows rather than penalizing
            # or rewarding on an undefined basis.
            baseline = group["player_id"].map(fastball_baseline)
            velo_gap = baseline - group["velo"]
            gap_z = zscore(velo_gap).fillna(0.0)
            velo_z = CH_VELO_GAP_WEIGHT * gap_z + CH_RAW_VELO_WEIGHT * velo_z
        elif VELO_SHAPE.get(pt) == "abs":
            velo_z = velo_z.abs()
        elif VELO_SHAPE.get(pt) == "signed_neg":
            velo_z = -velo_z
        ivb_z = zscore(group["ivb_in"])
        if IVB_SHAPE.get(pt) == "abs":
            ivb_z = ivb_z.abs()
        elif IVB_SHAPE.get(pt) == "signed_neg":
            ivb_z = -ivb_z
        horiz_z = zscore(group["horizontal_in"])
        if HORIZ_SHAPE.get(pt) == "abs":
            horiz_z = horiz_z.abs()
        elif HORIZ_SHAPE.get(pt) == "signed_neg":
            horiz_z = -horiz_z
        spin_z = zscore(group["spin_rpm"])
        as_weight = ACTIVE_SPIN_WEIGHT.get(pt, 0.0)
        horiz_weight = HORIZ_WEIGHT_OVERRIDE.get(pt, HORIZ_WEIGHT)

        ceiling = (
            velo_z
            + IVB_WEIGHT * ivb_z
            + horiz_weight * horiz_z
            + SPIN_WEIGHT * spin_z
            + as_weight * group["active_spin_quotient"]
        )
        # Release characteristics (extension, arm angle, release point) make
        # every pitch a pitcher throws harder to pick up, not just the pitch
        # itself in isolation -- so the delivery modifier applies here, to
        # every pitch type, rather than only to a composite pitcher score.
        #
        group["quotient"] = (
            ceiling * (group["usage_rate"] ** USAGE_RATE_EXPONENT) * group["delivery_modifier"]
        )
        # display_score: the same information as quotient, just rescaled onto
        # a "100 = league average for this pitch type" scale (like Stuff+/
        # PitchingBot), so it reads intuitively instead of as a raw
        # z-score-weighted composite whose range differs oddly by pitch type
        # (cutters routinely topping 4+ while curveballs top out under 2,
        # purely as an artifact of the weighting/usage math, not because
        # cutters are "better"). This is a display-only transform -- a
        # second z-score taken of quotient itself, within the same pitch-type
        # group, mapped onto 100 +/- 10 per standard deviation and rounded to
        # a whole number. It's monotonic with quotient, so ranking/sorting by
        # either produces identical order; quotient itself is unchanged and
        # still drives every actual computation (including this one).
        group["display_score"] = (100 + 10 * zscore(group["quotient"])).round()
        out_frames.append(group)

    result = pd.concat(out_frames, ignore_index=True)
    return result[[
        "player_id", "pitch_type", "velo", "ivb_in", "horizontal_in", "spin_rpm",
        "active_spin_pct", "usage_rate", "active_spin_quotient", "delivery_modifier",
        "quotient", "display_score",
    ]]


def compute_pitchers(pitch_metrics: pd.DataFrame, delivery: pd.DataFrame,
                      pitcher_names: pd.DataFrame) -> pd.DataFrame:
    # `delivery` here is already the full, unioned, z-scored frame built by
    # build_delivery_quotients() -- covers every player_id in pitch_metrics,
    # not just the arm-angle leaderboard's own subset.
    group_of = {pt: info["group"] for pt, info in PITCH_TYPES.items()}
    pitch_metrics = pitch_metrics.copy()
    pitch_metrics["group"] = pitch_metrics["pitch_type"].map(group_of)
    group_quotients = pitch_metrics.groupby(["player_id", "group"])["quotient"].sum().unstack(fill_value=0.0)
    for g in ["4-Seam", "Sinker", "Cutter", "Changeup", "Curve", "Slider"]:
        if g not in group_quotients.columns:
            group_quotients[g] = 0.0
    group_quotients["pitch_sum"] = group_quotients[
        ["4-Seam", "Sinker", "Cutter", "Changeup", "Curve", "Slider"]
    ].sum(axis=1)

    usage_by_player = pitch_metrics.groupby("player_id")["usage_rate"].apply(list)
    n_thrown = pitch_metrics.groupby("player_id")["pitch_type"].nunique()

    def entropy(rates):
        return -sum(p * math.log(p) for p in rates if p and p > 0)

    arsenal_entropy = usage_by_player.apply(entropy)
    diversity_multiplier = 0.9 + 0.15 * arsenal_entropy

    # delivery already carries its own (possibly all-blank) 'pitcher_name'
    # column; drop it before merging in the authoritative one from
    # pitcher_names, so pandas doesn't rename both to pitcher_name_x/_y.
    pitchers = delivery.drop(columns=["pitcher_name"]).merge(
        group_quotients, left_on="player_id", right_index=True, how="left"
    )
    pitchers = pitchers.merge(pitcher_names, on="player_id", how="left")
    pitchers["pitch_sum"] = pitchers["pitch_sum"].fillna(0.0)
    pitchers["n_pitches_thrown"] = pitchers["player_id"].map(n_thrown).fillna(0).astype(int)
    pitchers["arsenal_entropy"] = pitchers["player_id"].map(arsenal_entropy).fillna(0.0)
    pitchers["diversity_multiplier"] = pitchers["player_id"].map(diversity_multiplier).fillna(0.9)

    pitchers["uscore"] = (
        DELIVERY_WEIGHT_IN_USCORE * pitchers["delivery_quotient"] + pitchers["pitch_sum"]
    )
    pitchers["adjusted_uscore"] = (
        (pitchers["uscore"]
         - DELIVERY_WEIGHT_IN_USCORE * pitchers["delivery_quotient"]
         + DELIVERY_WEIGHT_IN_USCORE * pitchers["adj_delivery_quotient"])
        * pitchers["diversity_multiplier"]
    )
    pitchers["season"] = SEASON

    return pitchers[[
        "player_id", "pitcher_name", "season",
        "extension_ft", "arm_angle_deg", "release_height_ft", "horizontal_release_ft",
        "delivery_quotient", "adj_delivery_quotient",
        "n_pitches_thrown", "arsenal_entropy", "diversity_multiplier",
        "uscore", "adjusted_uscore",
    ]]


# --------------------------------------------------------------------------
# 5. Write to Supabase
# --------------------------------------------------------------------------

def upsert_in_batches(table, rows: list[dict], batch_size: int = 500, on_conflict: str | None = None):
    """Upsert wraps Postgres' `INSERT ... ON CONFLICT`, but Supabase's
    client only targets the table's PRIMARY KEY by default -- it has no way
    to know a different column combination should count as "the same row"
    unless told explicitly via on_conflict. `pitchers` is fine without it
    (player_id IS the primary key), but `pitch_metrics`'s primary key is an
    unrelated auto-incrementing `id` column, while the real uniqueness rule
    is the separate `unique(player_id, season, pitch_type)` constraint --
    without on_conflict, every run tries to INSERT a brand new row and
    collides with that constraint instead of updating the existing row."""
    kwargs = {"on_conflict": on_conflict} if on_conflict else {}
    for i in range(0, len(rows), batch_size):
        table.upsert(rows[i:i + batch_size], **kwargs).execute()


def fetch_all_rows(build_query, page_size: int = 1000) -> list[dict]:
    """Runs a Supabase select repeatedly with .range() pagination and
    concatenates every page into one list.

    A plain, unpaginated `.select(...).execute()` doesn't error when a
    table has more rows than PostgREST's default per-request cap (around
    1000) -- it just silently returns a partial result. That's harmless for
    a query whose result gets displayed, but dangerous for one that DECIDES
    something: this exact bug let stale pitch_metrics rows survive
    undetected (the cleanup below could only ever "see" the first slice of
    the table, so anything past the cap was never even considered stale or
    fresh) and separately truncated the correlation-analysis script's own
    read of this same table. Anywhere this pipeline reads back its own
    already-written data to compare against or build on, it needs to see
    the WHOLE table, not just however much fit in one page.

    `build_query` is a zero-arg callable of (start, end) -> a fresh, not-
    yet-executed Supabase query with `.range(start, end)` applied -- fresh
    each call, since a query builder is spent after one `.execute()`."""
    all_rows: list[dict] = []
    start = 0
    while True:
        page = build_query(start, start + page_size - 1).execute().data
        if not page:
            break
        all_rows.extend(page)
        if len(page) < page_size:
            break  # a partial page means this was the last one
        start += page_size
    return all_rows


def run():
    supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    log_row = supabase.table("refresh_log").insert({"status": "running"}).execute().data[0]
    log_id = log_row["id"]

    try:
        events = fetch_pitch_events()
        pitch_metrics_raw, extension_from_events = compute_pitch_metrics_from_events(events)
        active_spin = fetch_active_spin()
        delivery = fetch_delivery_metrics()

        if not extension_from_events.empty:
            delivery = delivery.drop(columns=["extension_ft"]).merge(
                extension_from_events, on="player_id", how="left"
            )

        # Build a name lookup that covers every pitcher, not just the ones
        # on the (smaller) arm-angle leaderboard -- a pitcher who only shows
        # up in pitch_metrics (see compute_pitchers' union-of-player-ids fix
        # below) still needs a name. Start from the raw pitch events, which
        # include every pitcher who threw a pitch this season, then let the
        # arm-angle leaderboard's name win where both have one, since it's
        # already curated to real MLB pitcher names.
        name_col = next((c for c in PLAYER_NAME_ALIASES if c in events.columns), None)
        if name_col:
            events_named = normalize_player_id(events.copy(), "statcast_search (names)")
            names_from_events = (
                events_named[["player_id", name_col]]
                .rename(columns={name_col: "pitcher_name"})
                .dropna(subset=["pitcher_name"])
                .drop_duplicates(subset=["player_id"])
            )
        else:
            names_from_events = pd.DataFrame(columns=["player_id", "pitcher_name"])

        names_from_delivery = (
            delivery[["player_id", "pitcher_name"]]
            .dropna(subset=["pitcher_name"])
            .drop_duplicates(subset=["player_id"])
        )
        pitcher_names = pd.concat([names_from_delivery, names_from_events], ignore_index=True).drop_duplicates(
            subset=["player_id"], keep="first"
        )

        # Built once, ahead of the per-pitch quotients, so the same delivery
        # z-scores and modifier feed both the per-pitch quotient (as a
        # multiplier) and the pitchers table's own delivery columns without
        # computing them twice or letting the two drift apart.
        delivery_full = build_delivery_quotients(pitch_metrics_raw["player_id"], delivery)

        pitch_metrics = compute_pitch_quotients(pitch_metrics_raw, active_spin, delivery_full)
        pitch_metrics["season"] = SEASON  # required by the pitch_metrics table's NOT NULL constraint
        pitchers = compute_pitchers(pitch_metrics, delivery_full, pitcher_names)

        pitchers = pitchers.dropna(subset=["player_id"])
        pitch_metrics = pitch_metrics.dropna(subset=["player_id"])

        # Force player_id to plain Python ints (pandas' nullable Int64 dtype
        # otherwise leaves numpy.int64 values, which some JSON encoders in
        # the Supabase write path below don't know how to serialize).
        pitchers["player_id"] = pitchers["player_id"].map(int)
        pitch_metrics["player_id"] = pitch_metrics["player_id"].map(int)

        # Capture each (player_id, pitch_type)'s quotient as it stands RIGHT
        # NOW, before this run's upsert overwrites it -- this becomes
        # "yesterday's" value for the home page's day-over-day movers boxes.
        # Has to happen after the upserts above would be too late (the old
        # value would already be gone), so this reads the table one last
        # time before writing anything.
        existing_quotients = fetch_all_rows(
            lambda start, end: supabase.table("pitch_metrics").select(
                "player_id, pitch_type, quotient, display_score"
            ).eq("season", SEASON).range(start, end)
        )
        prev_map = {
            (row["player_id"], row["pitch_type"]): row["quotient"]
            for row in existing_quotients if row["quotient"] is not None
        }
        prev_display_map = {
            (row["player_id"], row["pitch_type"]): row["display_score"]
            for row in existing_quotients if row["display_score"] is not None
        }
        prev_captured_at = datetime.now(timezone.utc).isoformat()
        pitch_metrics["prev_quotient"] = [
            prev_map.get((pid, pt)) for pid, pt in zip(pitch_metrics["player_id"], pitch_metrics["pitch_type"])
        ]
        pitch_metrics["prev_display_score"] = [
            prev_display_map.get((pid, pt)) for pid, pt in zip(pitch_metrics["player_id"], pitch_metrics["pitch_type"])
        ]
        pitch_metrics["prev_captured_at"] = [
            prev_captured_at if (pid, pt) in prev_map else None
            for pid, pt in zip(pitch_metrics["player_id"], pitch_metrics["pitch_type"])
        ]

        # Strict JSON (which the Supabase write below requires) can't
        # represent NaN or +/-Infinity. A pandas-level replace() was tried
        # here first and didn't catch everything (still failed against a
        # real run), so this instead walks the actual list-of-dicts that
        # will be serialized and scrubs any bad float at the raw Python
        # level -- this can't miss anything regardless of which pandas
        # dtype or code path produced the value.
        def sanitize_records(records: list[dict], label: str) -> list[dict]:
            bad_counts: dict[str, int] = {}
            for record in records:
                for key, value in record.items():
                    if isinstance(value, float) and math.isnan(value):
                        record[key] = None
                    elif isinstance(value, float) and math.isinf(value):
                        bad_counts[key] = bad_counts.get(key, 0) + 1
                        record[key] = None
            if bad_counts:
                print(f"NOTE: {label} had infinite values, replaced with blank: {bad_counts}")
            return records

        pitchers_rows = sanitize_records(
            pitchers.where(pd.notnull(pitchers), None).to_dict(orient="records"), "pitchers"
        )
        pitch_rows = sanitize_records(
            pitch_metrics.where(pd.notnull(pitch_metrics), None).to_dict(orient="records"), "pitch_metrics"
        )

        upsert_in_batches(supabase.table("pitchers"), pitchers_rows)
        upsert_in_batches(supabase.table("pitch_metrics"), pitch_rows, on_conflict="player_id,season,pitch_type")

        # upsert only adds/updates rows -- it never removes ones that
        # shouldn't be there anymore (e.g. a pitcher who qualified in a
        # past run, under looser filtering, but doesn't this time). Without
        # this cleanup, players like that would sit in the database
        # forever. Compare who's actually in this run's result against who
        # the database already has for this season, and delete anyone no
        # longer present -- pitch_metrics rows are removed automatically
        # via the "on delete cascade" foreign key set up in schema.sql.
        current_ids = {r["player_id"] for r in pitchers_rows}
        existing = fetch_all_rows(
            lambda start, end: supabase.table("pitchers").select("player_id").eq("season", SEASON).range(start, end)
        )
        stale_ids = [row["player_id"] for row in existing if row["player_id"] not in current_ids]
        for i in range(0, len(stale_ids), 500):
            chunk = stale_ids[i:i + 500]
            supabase.table("pitchers").delete().eq("season", SEASON).in_("player_id", chunk).execute()
        if stale_ids:
            print(f"Removed {len(stale_ids)} pitchers no longer in this run's data "
                  f"(e.g. previously-included spring-training-only players).")

        # The cleanup above only catches a pitcher who no longer qualifies at
        # ALL this season (their whole "pitchers" row disappears, which is
        # what actually triggers the "on delete cascade" and clears their
        # pitch_metrics rows too). It does NOT catch the more common case: a
        # pitcher who still qualifies overall (their pitchers row survives,
        # so no cascade ever fires) but who no longer meets MIN_PITCHES for
        # ONE SPECIFIC pitch type they used to throw enough of -- they've
        # simply thrown fewer of it lately, changed their mix, etc. That
        # (player_id, pitch_type) row would otherwise sit in pitch_metrics
        # forever with whatever it last computed, including columns added
        # AFTER that row's last real update (e.g. display_score coming back
        # null on an old row that predates the column, sorting to the top of
        # every leaderboard as a mystery blank -- exactly what this fixes).
        # Same idea as the cleanup above, just scoped to the finer
        # (player_id, pitch_type) grain pitch_metrics actually keys on.
        current_pitch_keys = {(r["player_id"], r["pitch_type"]) for r in pitch_rows}
        existing_pitch_keys = fetch_all_rows(
            lambda start, end: supabase.table("pitch_metrics").select(
                "player_id, pitch_type"
            ).eq("season", SEASON).range(start, end)
        )
        stale_by_player: dict[int, list[str]] = {}
        for row in existing_pitch_keys:
            key = (row["player_id"], row["pitch_type"])
            if key not in current_pitch_keys:
                stale_by_player.setdefault(row["player_id"], []).append(row["pitch_type"])
        for player_id, pitch_types in stale_by_player.items():
            supabase.table("pitch_metrics").delete().eq("season", SEASON).eq(
                "player_id", player_id
            ).in_("pitch_type", pitch_types).execute()
        if stale_by_player:
            stale_pitch_row_count = sum(len(v) for v in stale_by_player.values())
            print(f"Removed {stale_pitch_row_count} stale pitch_metrics rows across "
                  f"{len(stale_by_player)} pitchers (individual pitch types they no "
                  f"longer qualify for this run, even though the pitcher overall still does).")

        supabase.table("refresh_log").update({
            "status": "success",
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "pitchers_written": len(pitchers_rows),
        }).eq("id", log_id).execute()

        print(f"Refresh complete: {len(pitchers_rows)} pitchers, {len(pitch_rows)} pitch-type rows "
              f"({len(stale_ids)} stale pitchers removed).")

        # Probable starters for the home page's "pitchers to watch today" box.
        # Deliberately its own try/except, AFTER the main refresh has already
        # succeeded and been logged -- a problem here (MLB's feed down, a
        # response-shape change, no games scheduled) only skips this one box
        # and never touches the leaderboard data above, which is already
        # safely written at this point regardless of what happens next.
        try:
            today_str = datetime.now().strftime("%Y-%m-%d")
            probable_rows = fetch_probable_starters(today_str)
            # Only keep starters who are actually in this run's pitchers
            # table -- anyone else has no quotient data to rank by anyway,
            # and pre-filtering here (rather than relying on the database to
            # reject bad rows) means this insert can never fail with a
            # foreign-key error.
            probable_rows = [r for r in probable_rows if r["player_id"] in current_ids]
            if probable_rows:
                supabase.table("probable_starters").delete().eq("game_date", today_str).execute()
                upsert_in_batches(
                    supabase.table("probable_starters"), probable_rows,
                    on_conflict="game_date,player_id",
                )
                print(f"Probable starters: wrote {len(probable_rows)} qualifying starters for {today_str}.")
            else:
                print(f"Probable starters: none found for {today_str} with qualifying uScore data "
                      f"(off day, starters not yet announced, or none met MIN_PITCHES) -- "
                      f"leaving existing data for this date as-is.")
        except Exception as e:
            print(f"WARNING: probable-starters fetch failed, skipping this run's update -- "
                  f"leaderboard refresh above is unaffected. Error: {e}", file=sys.stderr)

    except Exception as e:
        supabase.table("refresh_log").update({
            "status": "failed",
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "error_message": f"{e}\n\n{traceback.format_exc()}"[:4000],
        }).eq("id", log_id).execute()
        print("Refresh FAILED:", e, file=sys.stderr)
        raise


if __name__ == "__main__":
    run()
