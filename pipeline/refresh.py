"""
uScore daily refresh pipeline.

What this script does, in order:
  1. Pulls fresh pitch-level data (velocity, movement, spin, usage, extension)
     from Baseball Savant's "Pitch Arsenals" leaderboard.
  2. Pulls active-spin% from Savant's active-spin leaderboard.
  3. Pulls arm angle / release point from Savant's arm-angle leaderboard.
  4. Recomputes the uScore model (same math as the Excel workbook: z-scored
     "uniqueness" quotients per pitch type, the funky-delivery dampening fix,
     and the arsenal-diversity multiplier) fresh against THIS run's league.
  5. Writes the results into Supabase (pitchers + pitch_metrics tables).
  6. Logs the run (success/failure, row counts) to refresh_log.

This is meant to run unattended once a day via GitHub Actions (see
.github/workflows/refresh.yml). It is NOT meant to be run inside a network
sandbox with no internet access -- it needs to reach baseballsavant.mlb.com.

A NOTE ON HOW THIS WAS BUILT:
Savant doesn't publish a documented, stable API -- every URL and column name
below was confirmed against real responses while getting the very first
automated runs working (several rounds: a wrong leaderboard, a wrong id
column, wrong parameter names, etc., each one caught by print statements in
the code below and fixed from the actual GitHub Actions log). If a future
run ever fails after Savant changes something again, the same pattern
applies: the log will show the real column names Savant returned, and that's
enough to fix it -- you don't need to debug anything yourself, just send me
the log.
"""
from __future__ import annotations

import os
import sys
import math
import traceback
from datetime import datetime, timezone
from io import StringIO

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

# Every pitch type we score: the Savant column-prefix used on the wide
# "Pitch Arsenals" export, the internal pitch-type code, and which quotient
# group it rolls into.
PITCH_TYPES = {
    "ff": {"code": "FF", "label": "4-Seam Fastball", "group": "4-Seam"},
    "si": {"code": "SI", "label": "Sinker",          "group": "Sinker"},
    "fc": {"code": "FC", "label": "Cutter",          "group": "Cutter"},
    "ch": {"code": "CH", "label": "Changeup",        "group": "Changeup"},
    "fs": {"code": "FS", "label": "Splitter",        "group": "Changeup"},
    "fo": {"code": "FO", "label": "Forkball",        "group": "Changeup"},
    "cu": {"code": "CU", "label": "Curveball",       "group": "Curve"},
    "kc": {"code": "KC", "label": "Knuckle Curve",   "group": "Curve"},
    "cs": {"code": "CS", "label": "Slow Curve",      "group": "Curve"},
    "sl": {"code": "SL", "label": "Slider",          "group": "Slider"},
    "st": {"code": "ST", "label": "Sweeper",         "group": "Slider"},
    "sv": {"code": "SV", "label": "Slurve",          "group": "Slider"},
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

# Every id column Savant has used across its various leaderboard CSV
# exports, in priority order -- the first one found in a given export is
# treated as that pitcher's id.
PLAYER_ID_ALIASES = ["player_id", "pitcher_id", "pitcher", "entity_id", "mlbam_id", "mlb_id"]
PLAYER_NAME_ALIASES = ["last_name, first_name", "pitcher_name", "entity_name", "name"]


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
# 1. Pull pitch-level data (Velo / IVB / Horizontal break / Spin / Usage /
#    Extension) from Savant's "Pitch Arsenals" leaderboard -- a single wide
#    export with one row per pitcher and one group of columns per pitch
#    type (e.g. ff_avg_speed, ff_avg_spin, si_avg_speed, si_avg_spin, ...).
# --------------------------------------------------------------------------

# Candidate suffixes for each metric, tried in order, per pitch-type prefix
# (e.g. prefix "ff" + suffix "avg_speed" -> looks for column "ff_avg_speed").
# Savant's exact naming wasn't testable from this environment, so this list
# covers the plausible variants; the print statements below show the real
# column names on the first run so any mismatch is a one-line fix.
METRIC_SUFFIXES = {
    "velo": ["avg_speed", "velo", "avg_velocity", "speed"],
    "spin_rpm": ["avg_spin", "spin", "avg_spin_rate", "spin_rate"],
    "ivb_in": ["avg_break_z", "break_z", "ivb", "avg_break_z_induced", "induced_break_z"],
    "horizontal_in": ["avg_break_x", "break_x", "avg_horz_break", "horz_break"],
    "usage_rate": ["pitch_usage", "usage", "percent", "pct"],
    "extension_ft": ["avg_extension", "extension"],
    "active_spin_pct": ["active_spin", "avg_active_spin"],
}
CORE_METRICS = ["velo", "spin_rpm", "ivb_in", "horizontal_in", "usage_rate"]


def fetch_pitch_arsenals_wide() -> pd.DataFrame:
    url = f"https://baseballsavant.mlb.com/leaderboard/pitch-arsenals?year={SEASON}&min={MIN_PITCHES}&type=pitching&csv=true"
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    raw = pd.read_csv(StringIO(resp.text))
    print("pitch-arsenals (wide) columns:", list(raw.columns))
    return raw


def melt_pitch_arsenals(raw: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame | None]:
    """Reshape the wide (one row per pitcher) Pitch Arsenals export into a
    long table (one row per pitcher per pitch type), matching the shape the
    rest of the pipeline expects. Also pulls out a usage-weighted average
    extension per pitcher, if extension is present in this export, to feed
    the delivery quotient."""
    id_col = next((c for c in PLAYER_ID_ALIASES if c in raw.columns), None)
    if id_col is None:
        raise RuntimeError(
            f"pitch-arsenals: no player-id column found among {PLAYER_ID_ALIASES} "
            f"-- columns were {list(raw.columns)}"
        )

    rows = []
    ext_rows = []
    matched_summary = {}
    for prefix, info in PITCH_TYPES.items():
        found = {}
        for metric, suffixes in METRIC_SUFFIXES.items():
            found[metric] = next(
                (f"{prefix}_{suf}" for suf in suffixes if f"{prefix}_{suf}" in raw.columns), None
            )
        matched_summary[prefix] = found

        if not all(found[m] for m in CORE_METRICS):
            continue  # can't build usable rows for this pitch type -- skip it

        for _, row in raw.iterrows():
            usage = row[found["usage_rate"]]
            if pd.isna(usage) or usage == 0:
                continue
            usage_rate = usage / 100.0 if usage > 1 else usage
            rec = {
                "player_id": row[id_col],
                "pitch_type": info["code"],
                "velo": row[found["velo"]],
                "spin_rpm": row[found["spin_rpm"]],
                "ivb_in": row[found["ivb_in"]],
                "horizontal_in": row[found["horizontal_in"]],
                "usage_rate": usage_rate,
            }
            if found["active_spin_pct"]:
                rec["active_spin_pct"] = row[found["active_spin_pct"]]
            rows.append(rec)

            if found["extension_ft"]:
                ext_val = row[found["extension_ft"]]
                if pd.notna(ext_val):
                    ext_rows.append({"player_id": row[id_col], "extension_ft": ext_val, "weight": usage_rate})

    print("pitch-arsenals matched columns per pitch type:", matched_summary)

    if not rows:
        raise RuntimeError(
            "pitch-arsenals: couldn't find velocity/spin/movement/usage columns for ANY "
            f"pitch type. Matched-column summary: {matched_summary}. "
            f"Full column list was: {list(raw.columns)}"
        )

    arsenal = pd.DataFrame(rows)
    arsenal = normalize_player_id(arsenal, "pitch-arsenals")
    if "active_spin_pct" not in arsenal.columns:
        arsenal["active_spin_pct"] = None

    extension = None
    if ext_rows:
        ext_df = pd.DataFrame(ext_rows)
        ext_df = normalize_player_id(ext_df, "pitch-arsenals (extension)")

        def weighted_avg(g: pd.DataFrame) -> float:
            w = g["weight"]
            return (g["extension_ft"] * w).sum() / w.sum() if w.sum() else g["extension_ft"].mean()

        extension = (
            ext_df.groupby("player_id")
            .apply(weighted_avg, include_groups=False)
            .reset_index(name="extension_ft")
        )
    else:
        print("NOTE: pitch-arsenals export doesn't include an extension column -- "
              "extension_ft will stay blank this run.")

    return arsenal, extension


def fetch_all_pitch_arsenal() -> tuple[pd.DataFrame, pd.DataFrame | None]:
    raw = fetch_pitch_arsenals_wide()
    return melt_pitch_arsenals(raw)


# --------------------------------------------------------------------------
# 2. Pull active-spin data (only used as a fallback -- the Pitch Arsenals
#    export above may already include it per pitch type; this fills in
#    anything it's missing from Savant's dedicated active-spin leaderboard).
# --------------------------------------------------------------------------

def fetch_active_spin() -> pd.DataFrame:
    """Active-spin% by pitcher and pitch type, from Savant's active-spin
    leaderboard. Returns columns: player_id, pitch_type, active_spin_pct.
    Savant only publishes this for FF/SI/FC/CH/CU/SL/ST/SV -- other pitch
    types simply won't have a row here, which is expected (handled below by
    treating missing = None, not 0)."""
    url = f"https://baseballsavant.mlb.com/leaderboard/active-spin?year={SEASON}&csv=true"
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    raw = pd.read_csv(StringIO(resp.text))
    print("active-spin columns:", list(raw.columns))

    # Savant's active-spin export is wide (one column per pitch type, e.g.
    # "active_spin_fourseam", "active_spin_sinker", ...). Melt it to long
    # form so it lines up with fetch_all_pitch_arsenal()'s one-row-per-type
    # shape. Column names are matched loosely (lowercased, no separators)
    # since Savant has changed these before.
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
              "column names -- falling back to whatever the Pitch Arsenals export had. "
              f"Raw columns were: {list(raw.columns)}")
        return pd.DataFrame(columns=["player_id", "pitch_type", "active_spin_pct"])
    result = pd.DataFrame(long_rows)
    return normalize_player_id(result, "active-spin")


# --------------------------------------------------------------------------
# 3. Pull delivery data (arm angle, release point; extension comes from the
#    Pitch Arsenals export above when available)
# --------------------------------------------------------------------------

def fetch_delivery_metrics() -> pd.DataFrame:
    """One row per pitcher: arm angle, release height, horizontal release
    point (extension is filled in separately, from the Pitch Arsenals pull,
    when that export includes it). Returns columns: player_id, pitcher_name,
    extension_ft (blank placeholder), arm_angle_deg, release_height_ft,
    horizontal_release_ft."""
    url = f"https://baseballsavant.mlb.com/leaderboard/pitcher-arm-angles?season={SEASON}&min=1&csv=true"
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    raw = pd.read_csv(StringIO(resp.text))
    print("pitcher-arm-angles columns:", list(raw.columns))

    name_rename = {alias: "pitcher_name" for alias in PLAYER_NAME_ALIASES}
    # Confirmed live (2026 season) column names from this leaderboard's CSV
    # export: 'ball_angle' (arm angle), 'release_ball_z' (release height),
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

    if "extension_ft" not in df.columns:
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


def compute_pitch_metrics(arsenal: pd.DataFrame, active_spin_fallback: pd.DataFrame) -> pd.DataFrame:
    df = arsenal.copy()

    # Fill in active-spin from the dedicated active-spin leaderboard only
    # where the Pitch Arsenals export didn't already have it.
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
    delivery = delivery.copy()
    for col in ["extension_ft", "arm_angle_deg", "release_height_ft", "horizontal_release_ft"]:
        delivery[f"{col}_z"] = zscore(delivery[col])

    delivery["delivery_quotient"] = sum(
        delivery[f"{c}_z"].abs()
        for c in ["extension_ft", "arm_angle_deg", "release_height_ft", "horizontal_release_ft"]
    )
    delivery["adj_delivery_quotient"] = sum(
        delivery[f"{c}_z"].abs().pow(0.5)
        for c in ["extension_ft", "arm_angle_deg", "release_height_ft", "horizontal_release_ft"]
    )

    group_of = {info["code"]: info["group"] for info in PITCH_TYPES.values()}
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

    pitchers = delivery.merge(group_quotients, left_on="player_id", right_index=True, how="left")
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
        arsenal, extension_from_arsenal = fetch_all_pitch_arsenal()
        active_spin = fetch_active_spin()
        delivery = fetch_delivery_metrics()

        if extension_from_arsenal is not None:
            delivery = delivery.drop(columns=["extension_ft"]).merge(
                extension_from_arsenal, on="player_id", how="left"
            )

        pitcher_names = delivery[["player_id", "pitcher_name"]].drop_duplicates()
        pitch_metrics = compute_pitch_metrics(arsenal, active_spin)
        pitchers = compute_pitchers(pitch_metrics, delivery, pitcher_names)

        pitchers = pitchers.dropna(subset=["player_id"])
        pitch_metrics = pitch_metrics.dropna(subset=["player_id"])

        # Force player_id to plain Python ints (pandas' nullable Int64 dtype
        # otherwise leaves numpy.int64 values, which some JSON encoders in
        # the Supabase write path below don't know how to serialize).
        pitchers["player_id"] = pitchers["player_id"].map(int)
        pitch_metrics["player_id"] = pitch_metrics["player_id"].map(int)

        pitchers_rows = pitchers.where(pd.notnull(pitchers), None).to_dict(orient="records")
        pitch_rows = pitch_metrics.where(pd.notnull(pitch_metrics), None).to_dict(orient="records")

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
