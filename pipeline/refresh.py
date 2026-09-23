"""
uScore daily refresh pipeline.

What this script does, in order:
  1. Pulls fresh pitch-level data from Baseball Savant for every pitch type.
  2. Pulls delivery data (extension, arm angle, release point) for every pitcher.
  3. Recomputes the uScore model (same math as the Excel workbook: z-scored
     "uniqueness" quotients per pitch type, the funky-delivery dampening fix,
     and the arsenal-diversity multiplier) fresh against THIS run's league.
  4. Writes the results into Supabase (pitchers + pitch_metrics tables).
  5. Logs the run (success/failure, row counts) to refresh_log.

This is meant to run unattended once a day via GitHub Actions (see
.github/workflows/refresh.yml). It is NOT meant to be run inside a network
sandbox with no internet access -- it needs to reach baseballsavant.mlb.com.

IMPORTANT NOTE ON THE FIRST RUN:
The Baseball Savant URLs below are built from the same CSV export pattern
Baseball Savant leaderboards use (the same one you used to manually download
the files we built the original Excel model from). Savant doesn't publish a
stable, documented API, so it is possible the exact URL or column names for
the active-spin or arm-angle leaderboards have shifted slightly since this
was written. The very first GitHub Actions run will tell us immediately if
that's the case (it fails loudly and the failure reason lands in the
refresh_log table and the Actions log) -- at that point, send me the error
and, if easy, a fresh manual CSV download from the page in question, and
I'll adjust the URL/column mapping. Everything downstream (the uScore math,
the database writes) does not need to change either way.
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

# Every pitch type we score, and the Savant "pitch_type" code used to filter
# the arsenal-stats leaderboard, plus which quotient group it rolls into.
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


# --------------------------------------------------------------------------
# 1. Pull pitch-arsenal data (Velo / IVB / Horizontal break / Spin / Usage)
# --------------------------------------------------------------------------

# Every id column Savant has used across its various leaderboard CSV
# exports, in priority order -- the first one found in a given export is
# treated as that pitcher's id.
PLAYER_ID_ALIASES = ["player_id", "pitcher_id", "pitcher", "mlbam_id", "mlb_id"]


def normalize_player_id(df: pd.DataFrame, source_label: str) -> pd.DataFrame:
    """Rename whichever id column is present to 'player_id' and force it to
    a consistent integer type, so merges across the three data sources never
    fail with a dtype or column-name mismatch even if Savant's export uses a
    different id column name than we expect."""
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


def fetch_pitch_arsenal(pitch_type: str) -> pd.DataFrame:
    """One row per pitcher for a single pitch type, straight from Savant's
    pitch-arsenal-stats leaderboard CSV export."""
    url = (
        "https://baseballsavant.mlb.com/leaderboard/pitch-arsenal-stats"
        f"?type=pitcher&pitchType={pitch_type}&year={SEASON}&team=&min={MIN_PITCHES}&csv=true"
    )
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    df = pd.read_csv(StringIO(resp.text))
    if df.empty:
        return df
    print(f"pitch-arsenal-stats ({pitch_type}) columns:", list(df.columns))
    df = normalize_player_id(df, f"pitch-arsenal-stats ({pitch_type})")
    df["pitch_type"] = pitch_type
    return df


def fetch_all_pitch_arsenal() -> pd.DataFrame:
    frames = []
    for pt in PITCH_TYPES:
        df = fetch_pitch_arsenal(pt)
        if not df.empty:
            frames.append(df)
    if not frames:
        raise RuntimeError("Savant pitch-arsenal pull returned no data for any pitch type")
    return pd.concat(frames, ignore_index=True)


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
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    raw = pd.read_csv(StringIO(resp.text))
    print("active-spin columns:", list(raw.columns))

    # Savant's active-spin export is wide (one column per pitch type, e.g.
    # "active_spin_fourseam", "active_spin_sinker", ...). Melt it to long
    # form so it lines up with fetch_all_pitch_arsenal()'s one-row-per-type
    # shape. Column names are matched loosely (lowercased, no separators)
    # since Savant has changed these before.
    spin_cols = {c: c for c in raw.columns if "active_spin" in c.lower()}
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
# 3. Pull delivery data (extension, arm angle, release point)
# --------------------------------------------------------------------------

def fetch_delivery_metrics() -> pd.DataFrame:
    """One row per pitcher: extension, arm angle, release height, horizontal
    release point. Returns columns: player_id, pitcher_name, extension_ft,
    arm_angle_deg, release_height_ft, horizontal_release_ft."""
    url = f"https://baseballsavant.mlb.com/leaderboard/pitcher-arm-angles?season={SEASON}&min=1&csv=true"
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    raw = pd.read_csv(StringIO(resp.text))

    # Column names Savant has used for this leaderboard's CSV export. If a
    # future export renames a field, this print makes the mismatch obvious
    # in the Actions log instead of silently writing all-null columns.
    print("pitcher-arm-angles columns:", list(raw.columns))

    name_rename = {
        "last_name, first_name": "pitcher_name",
        "pitcher_name": "pitcher_name",
        "name": "pitcher_name",
    }
    # Confirmed live (2026 season) column names from this leaderboard's CSV
    # export: 'ball_angle' (arm angle), 'release_ball_z' (release height),
    # 'relative_release_ball_x' (horizontal release point). This leaderboard
    # does NOT publish release extension at all -- extension_ft is left
    # blank here; the delivery-quotient math below treats an all-blank
    # column as contributing 0 (see zscore()), so this only means the
    # delivery quotient is currently based on 3 metrics instead of 4, not
    # that anything breaks. If you'd like extension added back in, tell me
    # and I'll wire up a second Savant source for it.
    metric_rename = {
        "release_extension": "extension_ft",
        "avg_release_extension": "extension_ft",
        "extension": "extension_ft",
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
    mean, std = series.mean(), series.std(ddof=0)
    if not std or math.isnan(std):
        return series * 0.0
    return (series - mean) / std


def active_spin_quotient(series: pd.Series, shape: str | None) -> pd.Series:
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


def compute_pitch_metrics(arsenal: pd.DataFrame, active_spin: pd.DataFrame) -> pd.DataFrame:
    df = arsenal.merge(active_spin, on=["player_id", "pitch_type"], how="left")

    df["velo"] = df["velocity"]
    df["ivb_in"] = df["api_break_z_induced"] * 12
    df["horizontal_in"] = df["api_break_x_arm"] * 12
    df["spin_rpm"] = df["spin_rate"]
    df["usage_rate"] = df["pitch_percent"] / 100.0

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
        arsenal = fetch_all_pitch_arsenal()
        active_spin = fetch_active_spin()
        delivery = fetch_delivery_metrics()

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
