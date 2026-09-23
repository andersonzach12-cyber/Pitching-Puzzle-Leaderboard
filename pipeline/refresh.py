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
        "hfGT": "R|PO|S|",  # regular season, postseason, spring training
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
    # Spring training usually starts mid-to-late February; starting the pull
    # there instead of Jan 1 skips several weeks of guaranteed-empty windows
    # (and requests) with no games at all.
    season_start = datetime(SEASON, 2, 1)
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
    df["horizontal_in"] = pd.to_numeric(df[resolved["horizontal_in_raw"]], errors="coerce") * 12
    df["extension_ft"] = pd.to_numeric(df[resolved["extension_ft"]], errors="coerce")

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


def compute_pitch_quotients(pitch_metrics: pd.DataFrame, active_spin_fallback: pd.DataFrame) -> pd.DataFrame:
    df = pitch_metrics.copy()

    if not active_spin_fallback.empty:
        df = df.merge(
            active_spin_fallback, on=["player_id", "pitch_type"], how="left", suffixes=("", "_fallback")
        )
        if "active_spin_pct_fallback" in df.columns:
            df["active_spin_pct"] = df["active_spin_pct"].fillna(df["active_spin_pct_fallback"])
            df = df.drop(columns=["active_spin_pct_fallback"])

    out_frames = []
    for pt, group in df.groupby("pitch_type"):
        group = group.copy()
        group["active_spin_quotient"] = active_spin_quotient(
            group["active_spin_pct"], ACTIVE_SPIN_SHAPE.get(pt)
        )
        velo_z = zscore(group["velo"])
        ivb_z = zscore(group["ivb_in"])
        horiz_z = zscore(group["horizontal_in"])
        spin_z = zscore(group["spin_rpm"])
        as_weight = ACTIVE_SPIN_WEIGHT.get(pt, 0.0)

        ceiling = (
            velo_z
            + IVB_WEIGHT * ivb_z
            + HORIZ_WEIGHT * horiz_z
            + SPIN_WEIGHT * spin_z
            + as_weight * group["active_spin_quotient"]
        )
        group["quotient"] = ceiling * group["usage_rate"]
        out_frames.append(group)

    result = pd.concat(out_frames, ignore_index=True)
    return result[[
        "player_id", "pitch_type", "velo", "ivb_in", "horizontal_in", "spin_rpm",
        "active_spin_pct", "usage_rate", "active_spin_quotient", "quotient",
    ]]


def compute_pitchers(pitch_metrics: pd.DataFrame, delivery: pd.DataFrame,
                      pitcher_names: pd.DataFrame) -> pd.DataFrame:
    # `delivery` (the arm-angle leaderboard export) only covers a subset of
    # pitchers -- a pitcher can clear MIN_PITCHES in the full-season pitch
    # aggregation and appear in `pitch_metrics` without showing up on that
    # leaderboard. The `pitchers` table has to contain every player_id that
    # `pitch_metrics` references (pitch_metrics.player_id is a foreign key
    # into pitchers), so build the full player universe as the union of
    # both sources first, then left-merge delivery's fields onto it --
    # pitchers missing from the arm-angle export just get blank delivery
    # metrics (handled below via fillna(0) on the z-scores) rather than
    # being dropped from the database entirely.
    all_player_ids = pd.Index(
        pd.unique(pd.concat([delivery["player_id"], pitch_metrics["player_id"]], ignore_index=True)),
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

def upsert_in_batches(table, rows: list[dict], batch_size: int = 500):
    for i in range(0, len(rows), batch_size):
        table.upsert(rows[i:i + batch_size]).execute()


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

        pitch_metrics = compute_pitch_quotients(pitch_metrics_raw, active_spin)
        pitch_metrics["season"] = SEASON  # required by the pitch_metrics table's NOT NULL constraint
        pitchers = compute_pitchers(pitch_metrics, delivery, pitcher_names)

        pitchers = pitchers.dropna(subset=["player_id"])
        pitch_metrics = pitch_metrics.dropna(subset=["player_id"])

        # Force player_id to plain Python ints (pandas' nullable Int64 dtype
        # otherwise leaves numpy.int64 values, which some JSON encoders in
        # the Supabase write path below don't know how to serialize).
        pitchers["player_id"] = pitchers["player_id"].map(int)
        pitch_metrics["player_id"] = pitch_metrics["player_id"].map(int)

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
        upsert_in_batches(supabase.table("pitch_metrics"), pitch_rows)

        supabase.table("refresh_log").update({
            "status": "success",
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "pitchers_written": len(pitchers_rows),
        }).eq("id", log_id).execute()

        print(f"Refresh complete: {len(pitchers_rows)} pitchers, {len(pitch_rows)} pitch-type rows.")

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
