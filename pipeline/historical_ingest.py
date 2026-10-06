"""
Historical (multi-season) data ingestion for uScore+ model validation.

THIS IS NOT PART OF THE LIVE DAILY PIPELINE. refresh.py's live run (fetch ->
compute_pitch_quotients -> write to Supabase) is completely untouched by
this file, and nothing here ever writes to Supabase or touches the live
site. This exists purely to build an offline, season-tagged dataset from
Baseball Savant's "pitch arsenal" leaderboard exports (one row per pitcher
per pitch type per season -- a different, already-aggregated CSV shape than
the raw per-pitch Statcast Search export refresh.py pulls live each day),
so past seasons' uScore+ scores can be computed and used to validate/
re-tune model weights against more data than one season alone provides.

Per the user's explicit instruction (2026-10-02): every season's z-scores
stay fully IN-SEASON -- no pooling across years, no multi-season baselines.
This is enforced structurally, not by special-casing: compute_pitch_quotients
(imported unmodified from refresh.py) already groups only by pitch_type
within whatever rows it's handed, with no concept of "season" at all. Feeding
it one season's slice at a time is sufficient on its own to guarantee
within-season z-scores -- see run_historical_quotients() below.

Input file format (Savant's pitch-arsenal leaderboard CSV export, NOT the
same as the per-pitch Statcast Search export refresh.py uses live):
  velocity, spin_rate, api_break_z_induced, api_break_x_arm, pitch_percent,
  pitches, total_pitches, release_extension, player_id, player_name,
  pitch_type, ... (many outcome/quality columns not used here)

A few things confirmed by inspecting the real uploaded files before writing
this (2026-10-02), not assumed:
  - api_break_z_induced / api_break_x_arm are in FEET (values top out
    around +/-2), same as refresh.py's live pfx_z/pfx_x -- need the same
    *12 conversion to inches.
  - api_break_x_arm is ALREADY handedness-normalized (positive = arm-side
    for both lefties and righties) -- confirmed by checking real rows:
    CH/SI are ~100% positive, SL is ~97% negative, matching the arm-side/
    glove-side convention the live model expects. No p_throws flip needed
    here, unlike the live per-pitch path.
  - pitch_percent is 0-100 (needs /100 for usage_rate, which the model
    expects as a 0-1 fraction).
  - arm_angle, release_pos_z, release_pos_x, and release_extension are ALL
    present in this file too (one value per pitcher per pitch type, not one
    per pitcher like the live arm-angle leaderboard refresh.py pulls daily)
    -- confirmed 2026-10-02 after initially (incorrectly) telling the user
    this file had no delivery data at all. arm_angle specifically is only
    populated from 2020 onward (Statcast didn't track it before then, which
    matches real history); release_pos_z/release_pos_x/release_extension go
    back through 2017. See build_delivery_from_arsenal() below -- it
    collapses each pitcher's per-pitch-type rows into the one-row-per-
    pitcher shape build_delivery_quotients() (imported from refresh.py,
    unmodified) expects, usage-weighted so a show-me curveball thrown 2% of
    the time doesn't move the needle as much as the pitcher's primary pitch.
"""
from __future__ import annotations

import pandas as pd

# Reuses refresh.py's actual constants/thresholds and scoring function
# UNCHANGED, so historical rows are held to the exact same quality bar and
# scored with the exact same math as live data -- nothing here reimplements
# or approximates the model.
from refresh import (
    MIN_PITCHES,
    MIN_SEASON_PITCHES_TO_QUALIFY,
    PITCH_TYPES,
    SL_TO_ST_RECLASSIFY,
    ST_TO_SL_RECLASSIFY,
    build_delivery_quotients,
    compute_pitch_quotients,
)

# Same mapping as refresh.py's fetch_active_spin() (col_to_type), copied
# here rather than imported since it's defined inline inside that function.
# Keep these in sync if the live mapping ever changes.
ACTIVE_SPIN_COL_TO_TYPE = {
    "fourseam": "FF", "4seam": "FF", "ff": "FF",
    "sinker": "SI", "si": "SI",
    "cutter": "FC", "fc": "FC",
    "changeup": "CH", "ch": "CH",
    "splitter": "FS", "split": "FS", "fs": "FS",
    "curve": "CU", "curveball": "CU", "cu": "CU",
    "slider": "SL", "sl": "SL",
    "sweeper": "ST", "st": "ST",
    "slurve": "SV", "sv": "SV",
}


def build_delivery_from_arsenal(path: str, season: int) -> pd.DataFrame:
    """Collapse one season's per-(pitcher, pitch_type) arsenal export into
    the one-row-per-pitcher shape build_delivery_quotients() expects:
    player_id, extension_ft, arm_angle_deg, release_height_ft,
    horizontal_release_ft. Each metric is averaged across a pitcher's pitch
    types, weighted by how many pitches of that type he threw (`pitches`),
    so arm slot/release point reflects his overall delivery, not an
    unweighted average across a show-me pitch and his primary fastball
    alike. arm_angle is entirely missing before 2020 (see module docstring)
    -- for those seasons this returns NaN arm_angle_deg for everyone, which
    build_delivery_quotients already treats as "no reading" (z-scored as 0,
    neutral) rather than a penalty, same as a pitcher missing from the live
    arm-angle leaderboard."""
    raw = pd.read_csv(path)
    raw["player_id"] = pd.to_numeric(raw["player_id"], errors="coerce").astype("Int64")
    raw["weight"] = pd.to_numeric(raw["pitches"], errors="coerce")

    def weighted_mean(col: str) -> pd.Series:
        vals = pd.to_numeric(raw[col], errors="coerce")
        w = raw["weight"].where(vals.notna())
        contrib = (vals * w).groupby(raw["player_id"]).sum()
        wsum = w.groupby(raw["player_id"]).sum()
        return (contrib / wsum)

    out = pd.DataFrame({
        "arm_angle_deg": weighted_mean("arm_angle"),
        "release_height_ft": weighted_mean("release_pos_z"),
        "horizontal_release_ft": weighted_mean("release_pos_x"),
        "extension_ft": weighted_mean("release_extension"),
    }).reset_index().rename(columns={"index": "player_id"})
    n_with_arm_angle = out["arm_angle_deg"].notna().sum()
    print(f"[{season}] delivery: {len(out)} pitchers, {n_with_arm_angle} with an arm_angle reading.")
    return out


def _apply_sl_st_reclassification(df: pd.DataFrame, season: int) -> pd.DataFrame:
    """Mirror refresh.py's live SL<->ST hand reclassification (see
    SL_TO_ST_RECLASSIFY/ST_TO_SL_RECLASSIFY in refresh.py -- specific
    pitchers whose Savant-tagged pitch type doesn't match its actual
    depth-vs-no-depth shape, e.g. Tyler Rogers' "slider" that's really a
    sweeper). The live pipeline applies this at the raw per-pitch-event
    level, before _aggregate_pitch_events groups by (player_id, pitch_type),
    so a reclassified pitch that collides with a pitcher's pre-existing
    pitch of the destination type gets folded into that same arsenal slot
    via the ordinary groupby/mean rather than producing an impossible
    duplicate (player_id, season, pitch_type) row.

    historical_ingest has no raw per-pitch-event stream to apply this to --
    Savant's arsenal export arrives already aggregated to one row per
    (pitcher, pitch_type, season). This relabels the affected rows and then
    merges any resulting duplicates the same pitches-weighted way: a
    weighted mean of two already pitches-weighted means, re-weighted by
    their own pitch counts, equals the single weighted mean over the
    combined raw events, so this is mathematically identical to applying
    the live reclassification before aggregation, not an approximation of
    it. Only the specific player_ids in those two sets are touched --
    everyone else's rows pass through unchanged. Added 2026-10 after the ST
    review surfaced that this historical ingest had been missing the
    reclassification entirely (it was built, and the SL/ST passes were
    both run, before this gap was noticed)."""
    df = df.copy()
    df.loc[
        df["player_id"].isin(SL_TO_ST_RECLASSIFY) & (df["pitch_type"] == "SL"), "pitch_type"
    ] = "ST"
    df.loc[
        df["player_id"].isin(ST_TO_SL_RECLASSIFY) & (df["pitch_type"] == "ST"), "pitch_type"
    ] = "SL"

    dup_mask = df.duplicated(subset=["player_id", "pitch_type"], keep=False)
    if not dup_mask.any():
        return df

    dups = df[dup_mask]
    singles = df[~dup_mask]
    merged_rows = []
    for (pid, pt), group in dups.groupby(["player_id", "pitch_type"]):
        w = group["n_pitches"]
        wsum = w.sum()
        total_pitches = group["total_pitches"].iloc[0]
        merged_rows.append({
            "player_id": pid,
            "player_name": group["player_name"].iloc[0],
            "season": season,
            "pitch_type": pt,
            "velo": (group["velo"] * w).sum() / wsum,
            "spin_rpm": (group["spin_rpm"] * w).sum() / wsum,
            "ivb_in": (group["ivb_in"] * w).sum() / wsum,
            "horizontal_in": (group["horizontal_in"] * w).sum() / wsum,
            "n_pitches": wsum,
            "total_pitches": total_pitches,
            "usage_rate": wsum / total_pitches,
        })
        print(f"[{season}] SL/ST reclass: merged player {pid} ({group['player_name'].iloc[0]}) "
              f"{pt}: {len(group)} rows -> 1 ({wsum:.0f} combined pitches).")
    merged = pd.DataFrame(merged_rows, columns=singles.columns)
    return pd.concat([singles, merged], ignore_index=True)


def load_arsenal_csv(path: str, season: int) -> pd.DataFrame:
    """Normalize one season's Savant pitch-arsenal export into the same
    column shape refresh.py's live _aggregate_pitch_events() produces:
    player_id, pitch_type, velo, spin_rpm, ivb_in, horizontal_in,
    usage_rate, n_pitches, total_pitches -- plus season and player_name
    for traceability. Applies the exact same MIN_PITCHES/
    MIN_SEASON_PITCHES_TO_QUALIFY filters the live season path uses, so a
    position player's incidental mound appearance or a cup-of-coffee
    pitch type doesn't sneak into the historical z-score population either.
    """
    raw = pd.read_csv(path)

    missing = [c for c in (
        "velocity", "spin_rate", "api_break_z_induced", "api_break_x_arm",
        "pitch_percent", "pitches", "total_pitches", "player_id", "pitch_type",
    ) if c not in raw.columns]
    if missing:
        raise RuntimeError(f"{path}: missing expected columns {missing}. Actual columns: {list(raw.columns)}")

    df = raw[raw["pitch_type"].isin(PITCH_TYPES.keys())].copy()

    df["velo"] = pd.to_numeric(df["velocity"], errors="coerce")
    df["spin_rpm"] = pd.to_numeric(df["spin_rate"], errors="coerce")
    df["ivb_in"] = pd.to_numeric(df["api_break_z_induced"], errors="coerce") * 12
    df["horizontal_in"] = pd.to_numeric(df["api_break_x_arm"], errors="coerce") * 12
    df["usage_rate"] = pd.to_numeric(df["pitch_percent"], errors="coerce") / 100.0
    df["n_pitches"] = pd.to_numeric(df["pitches"], errors="coerce")
    df["total_pitches"] = pd.to_numeric(df["total_pitches"], errors="coerce")
    df["player_id"] = pd.to_numeric(df["player_id"], errors="coerce").astype("Int64")
    df["season"] = season

    cols_pre_filter = ["player_id", "player_name", "season", "pitch_type", "velo", "spin_rpm",
                        "ivb_in", "horizontal_in", "usage_rate", "n_pitches", "total_pitches"]
    # Reclassify/merge before the quality filters below, same ordering as
    # the live pipeline (reclassification happens before aggregation-level
    # filtering there too) -- see _apply_sl_st_reclassification's docstring.
    df = _apply_sl_st_reclassification(df[cols_pre_filter], season)

    before = df["player_id"].nunique()
    df = df[df["total_pitches"] >= MIN_SEASON_PITCHES_TO_QUALIFY].copy()
    df = df[df["n_pitches"] >= MIN_PITCHES].copy()
    after = df["player_id"].nunique()
    print(f"[{season}] arsenal: {len(raw)} raw rows -> {len(df)} rows after pitch-type/quality "
          f"filters ({before} -> {after} distinct pitchers).")

    cols = ["player_id", "player_name", "season", "pitch_type", "velo", "spin_rpm",
            "ivb_in", "horizontal_in", "usage_rate", "n_pitches", "total_pitches"]
    return df[cols].reset_index(drop=True)


def load_active_spin_csv(path: str, season: int) -> pd.DataFrame:
    """Normalize one season's Savant active-spin leaderboard export into
    long form: player_id, pitch_type, active_spin_pct, season. Same melt
    logic as refresh.py's live fetch_active_spin(), just reading from a
    local file instead of the leaderboard URL."""
    raw = pd.read_csv(path)
    id_col = "entity_id" if "entity_id" in raw.columns else None
    if id_col is None:
        raise RuntimeError(f"{path}: no entity_id column. Actual columns: {list(raw.columns)}")

    spin_cols = [c for c in raw.columns if "active_spin" in c.lower()]
    long_rows = []
    for col in spin_cols:
        key = col.lower().replace("active_spin", "").replace("_", "")
        pt = ACTIVE_SPIN_COL_TO_TYPE.get(key)
        if pt is None:
            continue
        for _, row in raw.iterrows():
            val = row[col]
            if pd.notna(val):
                long_rows.append({"player_id": row[id_col], "pitch_type": pt,
                                   "active_spin_pct": val, "season": season})
    out = pd.DataFrame(long_rows, columns=["player_id", "pitch_type", "active_spin_pct", "season"])
    out["player_id"] = pd.to_numeric(out["player_id"], errors="coerce").astype("Int64")
    print(f"[{season}] active-spin: {len(raw)} pitchers in export -> {len(out)} (player_id, pitch_type) rows.")
    return out


def run_historical_quotients(arsenal_files: dict[int, str],
                              active_spin_files: dict[int, str] | None = None) -> pd.DataFrame:
    """Build the season-tagged uScore+ dataset across every season given.

    arsenal_files: {season: path to that season's pitch-arsenal CSV}
    active_spin_files: {season: path to that season's active-spin CSV},
        optional per season -- a season with no entry here just runs with
        active_spin_pct entirely missing (None), exactly like how the live
        pipeline already treats a pitch type active-spin doesn't cover
        (FO/KC/CS) or a season where the file wasn't available (2017-2019).

    Runs compute_pitch_quotients -- imported from refresh.py, completely
    unmodified -- ONCE PER SEASON on that season's slice alone, so z-scores
    are computed within that season's own population only. No cross-season
    baseline is ever built.

    delivery_modifier is built fresh per season from that same arsenal file
    (via build_delivery_from_arsenal + build_delivery_quotients, both
    unmodified/lightly-wrapped versions of the live logic) rather than left
    at a flat 1.0 -- release_height/horizontal_release/extension are
    available all the way back to 2017, and arm_angle from 2020 on (see
    module docstring). A season missing arm_angle just has that one
    component treated as neutral for everyone, same as the live pipeline
    already does for any pitcher absent from the arm-angle leaderboard.
    """
    active_spin_files = active_spin_files or {}

    all_quotients = []
    for season, arsenal_path in sorted(arsenal_files.items()):
        pitch_metrics = load_arsenal_csv(arsenal_path, season)
        pitch_metrics["active_spin_pct"] = None

        if season in active_spin_files:
            active_spin = load_active_spin_csv(active_spin_files[season], season)
            active_spin = active_spin.drop(columns=["season"])
        else:
            active_spin = pd.DataFrame(columns=["player_id", "pitch_type", "active_spin_pct"])
            print(f"[{season}] no active-spin file provided -- active_spin_pct stays missing for this season.")

        delivery_raw = build_delivery_from_arsenal(arsenal_path, season)
        delivery = build_delivery_quotients(pitch_metrics["player_id"], delivery_raw)

        # compute_pitch_quotients itself only ever groups by pitch_type --
        # feeding it this one season's rows in isolation is what makes the
        # z-scores "in-season." Nothing inside it is season-aware, by design.
        player_lookup = pitch_metrics[["player_id", "player_name"]].drop_duplicates()
        quotients = compute_pitch_quotients(
            pitch_metrics.drop(columns=["player_name", "n_pitches", "total_pitches"]),
            active_spin,
            delivery,
        )
        quotients["season"] = season
        quotients = quotients.merge(player_lookup, on="player_id", how="left")
        all_quotients.append(quotients)
        print(f"[{season}] scored {len(quotients)} (player, pitch_type) rows.")

    result = pd.concat(all_quotients, ignore_index=True)
    cols = ["player_id", "player_name", "season", "pitch_type", "velo", "ivb_in",
            "horizontal_in", "spin_rpm", "active_spin_pct", "usage_rate",
            "arsenal_size", "effective_usage", "active_spin_quotient",
            "delivery_modifier", "quotient", "display_score"]
    return result[cols]


if __name__ == "__main__":
    import sys
    print("This module is meant to be imported and called with explicit file paths "
          "(run_historical_quotients(arsenal_files={...}, active_spin_files={...})), "
          "not run standalone with no arguments.", file=sys.stderr)
    sys.exit(1)
