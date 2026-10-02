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
from datetime import datetime, timedelta, timezone
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

# How many days of score_snapshots history to keep around. Only needs to be
# a bit more than the movers-box comparison window (currently 7 days, see
# home.js's fetchMovers) so a missed-refresh gap day doesn't leave the home
# page with nothing to compare against -- the rest is just bloat.
SNAPSHOT_RETENTION_DAYS = int(os.environ.get("USCORE_SNAPSHOT_RETENTION_DAYS", 35))

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

# Minimum pitches of a given type in a SINGLE DAY to qualify for the home
# page's "Yesterday's Best Pitches" box (2026-10 model review). Deliberately
# far below MIN_PITCHES (25, a season-long bar) -- a single start or relief
# outing rarely clears 25 of one secondary pitch, let alone 25 in one day.
# The point of this floor is narrower: keep a single extreme reading (one
# wild pitch out of a 3-pitch sample) from dominating the box. Bump this up
# if live numbers still look too noisy with 6.
DAILY_MIN_PITCHES = int(os.environ.get("USCORE_DAILY_MIN_PITCHES", 6))

# How many days of daily_pitch_scores history to keep. The home page only
# ever shows yesterday's, but a little headroom (same idea as
# SNAPSHOT_RETENTION_DAYS) means a missed-refresh day doesn't leave nothing
# to fall back on, and leaves room for a "yesterday" box to become a
# short trailing-window box later without a schema change.
DAILY_SCORE_RETENTION_DAYS = int(os.environ.get("USCORE_DAILY_SCORE_RETENTION_DAYS", 10))

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

# Hand reclassification of specific pitchers' Savant-tagged "slider" to
# "sweeper" (2026-10 model review, SL horizontal-weight pass). Savant's own
# SL/ST tagging turned out to have a real gap for these pitchers: a
# nearest-centroid check (horizontal_in + ivb_in, against the real
# 2026-season SL/ST population centroids -- note IVB barely differs between
# the two pitch types league-wide, mean 1.7 for SL vs 1.1 for ST, so
# horizontal break is really the only thing separating them) flagged 28 of
# 443 SL rows as sitting closer to ST's centroid than SL's own. Of those 28,
# this subset of 11 was selected by real-example review, not distance alone
# -- the guiding distinction (stated explicitly, not a derived rule): a
# slider has real depth to its break, a sweeper doesn't, so the deciding
# question for each candidate was whether the pitch visibly broke along a
# single, roughly straight-line axis out of the release point (sweeper) or
# curved/dove through its path (slider, even a sweepy one). This is
# explicitly acknowledged as an imperfect, subjective call, not a clean
# reproducible formula -- the remaining 17 (e.g. Chris Sale's, Mason
# Miller's) were judged to retain genuine depth alongside the sweep, or were
# simply less clear-cut, and were deliberately left as SL. Full pitcher-by-
# pitcher review in /areas/pitch-uniqueness-model.md.
#
# Applied here, at the raw per-pitch-event level before _aggregate_pitch_events
# groups by (player_id, pitch_type) -- NOT as a post-hoc relabel of an
# already-aggregated row -- so a pitcher who already throws a separate,
# distinct sweeper would have this pitch correctly MERGED into that same
# arsenal slot via the ordinary groupby/mean, rather than producing an
# impossible duplicate (player_id, season, "ST") row that the
# (player_id, season, pitch_type) uniqueness constraint can't hold. (Nolan
# McLean, 690997, was one of the original 12 candidates and did hit this
# exact case -- he already throws a separate, distinct ST pitch -- but was
# deliberately left OUT of this set: his slider and sweeper are kept as two
# separate, distinct pitches rather than merged into one.)
SL_TO_ST_RECLASSIFY = {
    680704,  # Sandlin, Nick
    676254,  # Walker, Ryan
    657044,  # Thompson, Ryan
    682825,  # Mey, Luis
    643511,  # Rogers, Tyler
    683363,  # Wilkinson, Matt
    805427,  # Sommers, Drew
    571927,  # Matz, Steven
    806960,  # Morales, Luis
    665622,  # Medina, Luis
    676879,  # Ashby, Aaron
}

# The mirror image of SL_TO_ST_RECLASSIFY above: specific pitchers' Savant-
# tagged "sweeper" that, by the same depth-vs-no-depth criterion, is
# functionally a slider (ST review, 2026-10). A nearest-centroid check run
# the other direction (horizontal_in + ivb_in, against the real 2026-season
# SL/ST centroids, post-SL_TO_ST_RECLASSIFY) flagged 20 of 309 ST rows as
# sitting closer to SL's centroid than ST's own -- all "shallow break"
# sweepers. Of those 20, this subset of 8 was selected by the same real-
# example review (does the pitch visibly curve/dive through its path, not
# just break along a straight-line axis out of the release point) as the
# clearest slider-shaped cases; the remaining 12 were kept as ST. Full
# pitcher-by-pitcher review in /areas/pitch-uniqueness-model.md.
#
# Applied the same way as SL_TO_ST_RECLASSIFY -- at the raw per-pitch-event
# level before the groupby below -- so a pitcher here who already throws a
# separate, distinct slider (Soriano, Singer, Pérez, Bachar) has this pitch
# correctly MERGED into that same arsenal slot via the ordinary groupby/mean.
ST_TO_SL_RECLASSIFY = {
    666277,  # Soriano, George -- merges into his existing SL
    663903,  # Singer, Brady -- merges into his existing SL
    700712,  # Ureña, Walbert
    661395,  # Duran, Jhoan
    691587,  # Pérez, Eury -- merges into his existing SL
    669199,  # Bachar, Lake -- merges into his existing SL
    678394,  # Bello, Brayan
    666200,  # Luzardo, Jesús
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

# Per-pitch-type override for how much vertical break (IVB) counts toward
# the Ceiling formula, in place of the global IVB_WEIGHT -- the IVB
# counterpart to HORIZ_WEIGHT_OVERRIDE below. Flat/symmetric (applies the
# same on both sides of league-average IVB), unlike
# IVB_WEIGHT_BELOW_AVG_OVERRIDE/IVB_WEIGHT_ABOVE_AVG_OVERRIDE's
# direction-conditional discount above.
#
# SL 0.60 (2026-10 model review, SL IVB pass): sliders already carry a
# boosted HORIZ_WEIGHT_OVERRIDE (0.65, vs. the 0.25 global default) on the
# premise that horizontal sweep is a slider's primary defining trait; this
# is the other half of that same statement -- vertical depth is real and
# meaningfully valuable, but secondary, so it's discounted from the
# fastball-tuned 0.95 rather than left at full weight. Chosen from a sweep
# of candidates (0.95/0.70/0.60/0.54/0.40/0.0) against the real 2026-season
# SL population (443 rows, on top of the already-shipped SL velocity
# change): population effect is mild at 0.60 (mean |delta| 1.12, max
# |delta| 7, 0/443 pitchers moving >=10 points) -- much gentler than the
# velocity change, since IVB's existing "abs" shape (see IVB_SHAPE below)
# was already symmetric, so this only rescales an existing term rather than
# flipping its direction. The PCA-derived candidate was 0.54; 0.60 was
# chosen instead as a deliberate middle ground, giving genuine depth
# outliers (e.g. player_id 681544 at -10.3in IVB, one of the largest in the
# league) slightly more residual credit than the raw PCA ratio would, while
# still meaningfully shifting weight toward horizontal/velocity for the
# average slider. Named spot-check: player_id 690925 (-5.9in IVB, 50.5%
# usage, otherwise average velocity/horizontal) drops 110->105 (-5) --a
# real but modest cost, not the kind of "punished for a defining trait"
# result that sank CU/KC's movement-weight bump (that case involved losses
# of -6/-9 on pitchers whose ENTIRE value proposition was the bumped
# dimension; here, no single pitcher crosses -10, and the pitchers who lose
# the most (681544 -6, 641793 -5) are still left with real, substantial IVB
# credit at 0.60, not a near-zeroing-out).
#
# SV 0.5 (2026-10 model review, SV horizontal pass, done after the SV
# velocity pass): this value had the exact same unvalidated provenance as
# SL/ST's old 0.65 -- introduced in the same commit, with no sweep or
# spot-check behind it. Unlike SL/ST, re-deriving it from scratch did NOT
# produce a different answer: swept 0.25 through 1.56 against the real
# 15-pitcher SV population and found the dimension genuinely low-leverage
# at this sample size -- even tripling the weight to 1.0 moves the single
# biggest mover (Sean Newcomb, -17.8in) by only 6 points, and it takes the
# full 1.56 PCA candidate before even one pitcher crosses a 10-point move
# (Newcomb +10). Same conclusion as SL's spin review: checked carefully,
# found flat, left as-is -- 0.5 is now a confirmed value, not an inherited
# guess.
#
# ST 1.46 (2026-10 model review, ST IVB pass, done after the ST velocity
# pass above and after both SL<->ST reclassifications): tested against the
# final, post-reclassification ST population (301 rows, IVB mean 1.18in/std
# 3.5in). This is a magnitude-only change -- IVB_SHAPE["ST"] is already
# "abs" (symmetric both directions), so bumping the weight from the
# fastball-tuned 0.95 default to 1.46 just gives that existing symmetric
# term more say, without changing its shape. Full sweep (0.95/1.10/1.22/
# 1.46) showed the gentlest population effect of any weight change in this
# review: even at full candidate strength, mean |delta| 1.52, max |delta| 9,
# 0/301 pitchers moving >=10 points -- low risk by construction. Named
# extremes move sensibly: Tyler Rogers (643511, 73.5mph submarine, +13.6in,
# the single most extreme IVB in the ST population) 115->120; Elieser
# Hernández (622694, +10.1in) 101->108; Scott Barlow (605130, +7.8in)
# 108->115.
#
# Explicitly tested and rejected: an asymmetric above/below-average IVB
# discount (mirroring IVB_WEIGHT_BELOW_AVG_OVERRIDE/
# IVB_WEIGHT_ABOVE_AVG_OVERRIDE's pattern elsewhere in this file). The
# above-average (rise) and below-average (drop) IVB groups are
# indistinguishable on every other trait that would justify treating them
# differently: mean |horizontal_in| 13.43in (above-avg group) vs 13.84in
# (below-avg group), corr(ivb_in, horizontal_in) = 0.07 (~none), mean velo
# 82.25 vs 82.60mph (~none). Critically, Max Meyer (676974, the 89mph power
# sweeper explicitly protected by the VELO_SHAPE/discount decision above)
# sits on the negative-IVB/drop side (-6.3in) alongside Dylan Cease and Drew
# Rom -- discounting below-average IVB to address any one of these would
# have clawed back credit from the same pitcher that decision was built to
# protect, directly undermining it. No asymmetric split applied; flat 1.46
# on both sides.
IVB_WEIGHT_OVERRIDE = {
    "SL": 0.60,
    "ST": 1.46,
}

# Per-pitch-type override: when a pitch type's IVB shape is "abs" (reward
# distance from league-average IVB in EITHER direction -- see IVB_SHAPE
# below) AND that pitch type is listed here, IVB_WEIGHT applies ONLY to
# rows at or above the league-average IVB; rows BELOW average use this
# discounted weight instead. Added for FF (2026-10 model review) as a
# deliberate middle ground between the old "signed" treatment (more carry
# always rewarded, less carry always penalized) and a straight "abs" flip
# (equal credit either direction): real four-seamers with genuinely
# below-average IVB -- Chris Sale's, Sean Manaea's, Logan Webb's -- are
# known, deliberate weapons specifically BECAUSE of their flatter shape,
# so flipping FF's IVB to "abs" is right in spirit, but a flat/heavy
# four-seamer is still generally a harder pitch to get right than a
# carrying one, so it's discounted relative to above-average carry rather
# than credited equally. 0.60 (vs. IVB_WEIGHT's 0.95) was chosen by
# sweeping 0.95/0.80/0.65/0.50/0.35/0.0 against the real 2026 FF population
# and spot-checking named pitchers at each step -- 0.60 reshuffles the
# leaderboard meaningfully (Spearman 0.80 vs. the old signed treatment) and
# gives Sale et al. real, substantial credit, without drifting all the way
# to pure ABS's 0.706 Spearman / up to +49-point swings. Only FF is listed
# here for now -- this pattern (an asymmetric, direction-conditional
# weight) is new to the model as of this change, and hasn't been
# considered for any other pitch type yet.
IVB_WEIGHT_BELOW_AVG_OVERRIDE = {
    "FF": 0.60,
}

# The mirror image of IVB_WEIGHT_BELOW_AVG_OVERRIDE above: IVB_WEIGHT
# applies ONLY to rows below league-average IVB; rows AT OR ABOVE average
# use this discounted weight instead. Added for SI (2026-10 model review,
# right after FF) for the opposite reason FF needed a below-average
# discount: a sinker's defining, sought-after trait runs the other way --
# heavy sink (BELOW-average IVB, a "true sinker" like Logan Webb's) is the
# archetype this model should lean toward recognizing, while an
# above-average-IVB sinker is closer to carrying/riding, a less
# characteristic (if still perfectly good) shape for the pitch type. SI's
# IVB shape was already "abs" (shipped in an earlier pass, unlike FF which
# started from "signed"), so this is a pure weight-magnitude tweak on top
# of an unchanged shape, not a shape flip -- which is also why this lever
# tested meaningfully gentler than FF's equivalent: even discounting
# above-average SI IVB all the way to 0.0 only produced a Spearman of
# 0.944 vs. the pre-existing abs treatment (compare FF's 0.60 alone
# producing 0.805 vs. its pre-existing signed treatment). 0.60 was chosen
# to match FF's value for consistency, and spot-checked against real
# elite high-carry sinkerballers (Hader, Chapman, Dodd, Whitlock,
# Montgomery, Jansen, Hunter Greene) -- all stayed clearly above-average
# even at far more aggressive discount levels than 0.60, confirming this
# compresses the high-carry group without punishing it into irrelevance.
IVB_WEIGHT_ABOVE_AVG_OVERRIDE = {
    "SI": 0.60,
    "FC": 0.50,
}

# The velocity-side counterpart to IVB_WEIGHT_BELOW_AVG_OVERRIDE/
# IVB_WEIGHT_ABOVE_AVG_OVERRIDE above, for pitch types where VELO_SHAPE is
# "abs" (see VELO_SHAPE below) AND the pitch type is listed here: velocity
# gets full (1.0) weight on rows AT OR ABOVE league-average velocity, and
# this discounted weight on rows BELOW average. Added for FC (2026-10 model
# review, right after FC's IVB override above) for the same reason FF
# needed a below-average IVB discount: real cutters get genuine value from
# being unusually hard (velocity itself is part of the weapon, tunneling
# off a mid-90s fastball), so a below-average-velocity cutter -- even a
# real, distinct depth-archetype one (see VELO_SHAPE's docstring) -- is
# still generally a lesser weapon than an equally-unusual hard one, and
# shouldn't be credited equally. Settled on jointly with IVB's 0.50 above
# (not independently) via repeated spot-checks on named pitchers at both
# extremes: an elite, near-exclusively-used high-carry cutter (Kenley
# Jansen, 82.8% usage -- the single highest usage rate of any cutter in the
# 2026 sample) and a low-usage, no-other-metric-corroborated depth cutter
# (Kolby Allard) were the two real-world cases driving the final choice --
# both remain clearly above-average at 0.50/0.50 without either dominating
# the leaderboard the way the uncompressed (1.0/0.95) version let them.
# Also confirmed via component-level breakdown that neither pitcher's score
# is actually being driven primarily by this lever: Jansen's is driven at
# least as much by his extreme usage rate (a separate, model-wide lever --
# USAGE_RATE_EXPONENT -- not anything FC-specific), and Allard's by several
# moderately-unusual components (velocity, IVB, horizontal) compounding at
# once rather than any single extreme reading. 0.50 was chosen from a
# sweep of 0.95/0.80/0.65/0.60/0.55/0.50/0.45/0.35/0.20/0.0, both on its own
# and jointly with IVB's discount, balancing "nobody moves >=10 vs. the
# uncompressed version" against still giving the depth archetype (and
# Jansen's high-carry counterpart) a real, visible compression.
#
# Added for CU/KC (2026-10 model review) for a related but distinct reason
# than FC's: CU/KC's VELO_SHAPE is already "abs" (a firm, hard curve and a
# slow, loopy one can both be elite -- see VELO_SHAPE's docstring), which is
# directionally right, but it was crediting extreme slowness exactly as
# generously as extreme hardness with no discount either way. Considered
# alongside a candidate horizontal-weight cut (0.25 -> 0.14, from an earlier
# PCA pass) and a movement-weight bump (IVB 0.95 -> 1.22), but testing both
# together showed the horizontal cut actively working against real,
# legitimate standouts: Matthew Boyd (-11.5in horizontal break, one of the
# larger sweeps in the league) and Quinn Mathews (-18.6in, a genuine
# outlier) both lost most of their movement credit under the combined
# version (-6 and -9 display-score points respectively) specifically
# because their defining trait is horizontal break, not velocity -- the
# same "don't punish a pitcher for one standout dimension" problem flagged
# earlier in this project for sinkers. Velocity alone, isolated from that
# horizontal cut, was confirmed gentler on exactly those two cases (-5 and
# -4 instead) while still correcting the thing that mattered: Chris
# Bassitt's CU (71.3 mph, >2.5 standard deviations below the 80.3 league
# mean) and Aaron Nola's KC (78.4 vs. an 82.5 league mean) were getting full
# credit for extreme slowness alone. Movement weights (IVB/horizontal) are
# left untouched for CU/KC -- the movement-only variant tested separately
# moved nobody by double digits (max |delta| 7 across 303 rows), confirming
# there's no real case for changing them right now. 0.60 was chosen to
# match FC's value for consistency, not independently re-swept, since the
# full-population effect was already modest at that setting (mean |delta|
# 1.38, only 1 pitcher of 303 CU/KC rows moving >=10 points) and named
# archetype checks (Glasnow's firm curve, Valdez's slow/loopy one) moved by
# 0-1 point, confirming the two intended extremes stay intact.
VELO_WEIGHT_BELOW_AVG_OVERRIDE = {
    "FC": 0.50,
    "CU": 0.60,
    "KC": 0.60,
    "SL": 0.35,
    "ST": 0.25,
}

# SL's 0.35 (see VELO_SHAPE's SL note above for the full "abs" shape
# rationale) was chosen from a full sweep of candidate discounts (0.95 down
# to 0.0) against the real 2026-season SL population (443 qualifying rows):
#   discount  mean|delta|  max|delta|  moved>=10   Sale(FF 519242)  Corbin(571578)
#     0.95       6.88          87          56         113->169          68->121
#     0.65       6.07          78          47         113->166          68->115
#     0.50       5.61          73          41         113->164          68->111
#     0.35       5.10          67          40         113->161          68->108
#     0.20       4.57          61          38         113->158          68->103
#     0.0        3.79          51          27         113->152          68->97
# Unlike FC/CU/KC, the 0.0 floor itself already carries a large, legitimate
# correction (undoing "signed"'s old penalty on real slow-slider outliers),
# so there's no discount that shrinks the population-wide swing to single
# digits the way FC/CU/KC's did. Given that, 0.35 was chosen to keep the
# ADDITIONAL discretionary credit above that 0.0 floor conservative,
# consistent with FC/CU/KC's discount-not-full-credit philosophy: it keeps
# the moved->=10 count (40) close to the 0.50 setting's (41) rather than
# climbing toward 56 at near-full credit (0.95), while still giving Sale and
# Corbin real, visible recognition (161/108) short of full symmetric credit.
# Named spot-check at 0.35 against the 10 pitchers from the arsenal-usage
# review (Anderson, Taylor, Morillo, Martinez, Headrick, Ginn, Zeferjahn,
# Sabrowski, Meyer, Tidwell) -- all above-average-velocity sliders -- showed
# small, uniform -2 to -4 point nudges (the expected cost of no longer
# getting full "signed" credit for throwing hard), confirming the change
# tracks as intended rather than disturbing an unrelated archetype.
#
# ST's 0.25 (see VELO_SHAPE's ST note above for the "abs" shape rationale
# and the double-counting counter-argument) was chosen from a full sweep of
# candidate discounts (0.35 down to 0.0, plus negative values tested and
# rejected -- see below) against the final, post-reclassification ST
# population (301 qualifying rows):
#   discount  mean|delta|  max|delta|  moved>=10   Manaea(640455)  Yarbrough(642232)  Cosgrove(676680)  Palmquist(687223)
#     0.35       5.39          57          27         58->97          75->124           72->128            42->99
#     0.30       5.21          55          27         58->95          75->122           72->126            42->97
#     0.25       5.02          52          27         58->93          75->121           72->123            42->94
#     0.20       4.77          49          26         58->92          75->119           72->121            42->91
#     0.10       4.40          45          24         58->88          75->115           72->117            42->86
#     0.0        4.03          40          21         58->84          75->111           72->112            42->81
# Negative discounts were also tested (pushing below-average ST velocity
# into active penalty territory again, rather than just zero credit) but
# explicitly rejected: at discount=-1.0 the below-average side is
# mathematically identical to the old "signed" shape (mean|delta| drops to
# 0.00 -- a full round trip back to the pre-change baseline), confirming
# that going negative at all just partially re-introduces the exact
# behavior the "abs" switch was meant to fix. Unlike FC/CU/KC, 0.0 itself
# still carries a large legitimate correction (removing "signed"'s old
# penalty on real slow-sweeper outliers, same mechanical pattern as SL), so
# 0.25 was chosen -- slightly below SL's 0.35 -- as a deliberately more
# conservative discretionary credit above that 0.0 floor, directly
# reflecting the stated skepticism that most slow sweepers hold up as a
# broad archetype the way slow sliders/curves do: it keeps the named
# deceptive-lefty group's gains in the 35-52 point range rather than the
# 39-57 point range at 0.35, while Meyer stays pinned at ~130-132 across the
# entire sweep regardless of setting, confirming the change is isolated to
# the below-average side as intended.

# ---------------------------------------------------------------------------
# OPEN ITEM (2026-10 model review): per-pitch-type re-weighting, paused.
#
# IVB_WEIGHT/HORIZ_WEIGHT/ACTIVE_SPIN_WEIGHT above are still blanket/
# hand-picked values for the pitch types below (FF, SI, FC, and now CU/KC's
# weights are resolved -- see IVB_WEIGHT_BELOW_AVG_OVERRIDE/
# IVB_WEIGHT_ABOVE_AVG_OVERRIDE/VELO_WEIGHT_BELOW_AVG_OVERRIDE above) -- a
# PCA pass against the real 2026-season `pitch_metrics` table (PC1 loadings
# normalized to velocity, which has no configurable weight of its own)
# produced candidate per-pitch-type replacements, but we deliberately
# deferred applying them pending further validation. Only the SHAPE changes
# from that same review went in at the time (see IVB_SHAPE/HORIZ_SHAPE/
# VELO_SHAPE above and ACTIVE_SPIN_SHAPE below); FF, SI, and FC's weights
# were each resolved separately and directly above, going pitch-by-pitch
# rather than from this batch of PCA candidates -- none of the three had a
# single dominant PCA archetype to derive a trustworthy ratio from (FF's
# PC1/2/3 were 38%/28%/20%; SI's were 37%/31%/17%/16%; FC's were
# 33%/31%/23%/14%, with FC's PC3 specifically flagging the Rasmussen-style
# depth archetype discussed below), so a straight PCA-ratio reweight was
# skipped for all three in favor of real-example-driven conditional
# weights instead; see each pitch type's dedicated review for the full
# writeup.
#
# Candidate weights from the PCA pass, for the remaining untouched pitch
# types (ST/SV next, given FF/SI/FC/CU/KC/SL are now done):
#   IVB_WEIGHT_OVERRIDE:   SL RESOLVED (2026-10, see IVB_WEIGHT_OVERRIDE's
#                          own definition above -- shipped at 0.60, not the
#                          raw PCA candidate of 0.54), ST RESOLVED (2026-10,
#                          see IVB_WEIGHT_OVERRIDE's own definition above --
#                          shipped at 1.46, the raw PCA candidate; asymmetric
#                          above/below discount explicitly tested and
#                          rejected, see same definition) (CU/KC's 1.22
#                          candidate TESTED and REJECTED below, not applied)
#   HORIZ_WEIGHT_OVERRIDE: SL RESOLVED (2026-10, see HORIZ_WEIGHT_OVERRIDE's
#                          own definition above -- the previous 0.65 had NO
#                          fact-based derivation at all, discovered during
#                          this review; re-swept from scratch and shipped at
#                          0.49, matching the PCA candidate), ST RESOLVED
#                          (2026-10, see HORIZ_WEIGHT_OVERRIDE's own
#                          definition above -- ST's previous 0.65 had the
#                          SAME unvalidated provenance as SL's; re-swept
#                          from scratch, shape confirmed to stay signed_neg
#                          (not flipped to abs -- no "wrong-way breaker"
#                          population exists for ST, unlike SL), and the
#                          PCA candidate 1.56 TESTED and REJECTED for
#                          punishing Max Meyer -23 points; shipped at 1.05
#                          instead, a deliberate middle ground), SV RESOLVED
#                          (2026-10, see HORIZ_WEIGHT_OVERRIDE's own
#                          definition above -- same unvalidated provenance
#                          as SL/ST's old 0.65, but re-deriving it did NOT
#                          change the answer: swept 0.25-1.56 against the
#                          15-pitcher SV population and found the dimension
#                          genuinely flat at this sample size, same
#                          conclusion as SL's spin review; left at 0.5,
#                          now confirmed rather than inherited) (CU/KC's
#                          0.14 candidate TESTED and REJECTED below)
#   ACTIVE_SPIN_WEIGHT:    SL 0.06, ST 0.23, SV 0.10 (unchanged) -- SL's
#                          spin (both active-spin and raw spin_rpm) reviewed
#                          2026-10 and found genuinely flat: even a full
#                          sweep down to 0 produced max |delta| 2-3 across
#                          443 rows, far below every other SL dimension's
#                          effect size; deliberately left as-is pending a
#                          larger, dedicated spin-across-all-pitch-types
#                          conversation, not re-applied piecemeal here. (CU/KC's
#                          0.11 candidate was never applied -- a ~no-op vs.
#                          the existing 0.10, not worth a separate change)
# (CH/FS/FO/CS left untouched in all three -- CH/FS because raw velocity
# barely loads on their dominant axis of variation, making a PCA-derived
# ratio-to-velocity unstable there; FO/CS because the season sample is only
# 2 pitchers league-wide.)
#
# Known issues to resolve before applying these, not just rubber-stamping
# the numbers above:
#   - PC1-only captures each pitch type's DOMINANT shared axis of variation,
#     not every real archetype -- the exact issue that sank a PCA-ratio
#     approach for FF/SI/FC above. ST showed a similar pattern to FC's (a
#     real, non-trivial PC3). Worth deciding whether to live with that
#     (PC1 = "the common case") or address it directly (e.g. a
#     two-component blend, or an asymmetric conditional weight like
#     FF/SI/FC's above) before shipping any weight change derived this way.
#   - ST's candidate change is the most aggressive of the batch and the
#     least certain: its horizontal weight would more than double, on top of
#     a shape flip, on top of a velocity shape flip whose own PCA signal was
#     close to a coin flip (32.1% vs 30.6% variance explained) -- three
#     compounding changes at once produced the single largest swing of any
#     pitch type (+110 display-score points for one pitcher in testing).
#     Sanity-check this against real sweeper names before trusting it.
#   - CU/KC RESOLVED (2026-10): the candidate IVB/horizontal weight bump
#     (1.22/0.14) was isolated from the earlier shape flip and tested on its
#     own -- it was a near-no-op alone (max |delta| 7 across 303 CU/KC rows)
#     but, combined with a velocity discount, actively punished real
#     standout-horizontal-break pitchers (Matthew Boyd, Quinn Mathews) for
#     their defining trait -- so the IVB/horizontal bump was REJECTED, and
#     only a velocity discount shipped (VELO_WEIGHT_BELOW_AVG_OVERRIDE, 0.60
#     -- see its definition above for the full writeup, including the
#     Boyd/Mathews component breakdown that drove this decision).
#   - CS (slow curve) hasn't been touched at all, shape or weight -- only 2
#     pitchers league-wide throw a tracked slow curve this season, so any
#     shape or weight call there (velocity/IVB/horizontal -> abs was floated
#     earlier) is pure analogy to CU/SL, never PCA-validated.
#   - Active spin % and IVB are highly correlated for FC specifically
#     (Pearson 0.86 in the real 2026 data) -- flagged during the FC pass but
#     deliberately left unresolved pending a broader, cross-pitch-type
#     conversation about active spin's role generally, not just for cutters.
# ---------------------------------------------------------------------------

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

# Option C (2026-10 usage-rate review): usage_rate itself conflates "how
# much a pitcher trusts/leans on this pitch" with "how many other pitch
# types compete for a share of their mix" -- across the real 2026 dataset
# (720 qualifying pitchers), usage_rate correlates -0.463 with arsenal_size
# (the pitcher's own count of qualifying pitch types). A 2-3 pitch reliever
# is structurally pushed toward extreme single-pitch usage shares that a
# 5-6 pitch starter can never produce, independent of real "trust" in the
# pitch -- raising or lowering USAGE_RATE_EXPONENT can't fix this, since it
# barely touches the extreme tail of the usage distribution where this
# confound matters most (tested at 0.65/0.55/0.45: even an aggressive 0.45
# only moved a usage=0.828 case from 179->161, still rank 1/284). A hard
# cap on raw usage_rate before the exponent was also tested and rejected as
# arbitrary -- no principled reason for one cap value over another.
# effective_usage instead rescales usage_rate by the ratio of a pitcher's
# own arsenal_size to MEDIAN_ARSENAL_SIZE: narrow-arsenal pitchers get
# scaled down (correcting the inflation), broad-arsenal pitchers get scaled
# up (crediting real usage spread across more pitches), and a pitcher at
# exactly the median arsenal size is unaffected. Explicitly decided LINEAR
# (the ratio is applied directly, not dampened through sqrt or another
# sub-linear exponent) and MEDIAN_ARSENAL_SIZE=4 as a single GLOBAL
# reference point (the dataset's actual median arsenal_size) rather than a
# role-specific one. A role-bucketed alternative -- grouping pitchers into
# narrow/typical/broad arsenal buckets and normalizing each against their
# OWN bucket's median instead of one global median -- was tested and
# rejected: it nearly cancels the correction for exactly the narrow-arsenal
# cases that motivated this work (e.g. a 3-pitch reliever's own bucket
# median is 3, producing a ~1.0 no-op ratio), because it uses arsenal_size
# both to define the grouping and as the thing being corrected -- a
# genuine role-aware version would need an external role signal (e.g.
# starter/reliever), which doesn't exist anywhere in the pipeline's data.
# Spot-tested by name against the full ~700-pitcher 2026 population before
# shipping: narrow-arsenal, high-usage relievers (2-3 pitch arsenals,
# 65-83% single-pitch usage -- e.g. Ben Joyce, Tyler Rogers, Kenley Jansen)
# lost 11-17 display-score points on their primary pitch, while
# broad-arsenal starters (5-6 pitches -- e.g. Drew Rasmussen, Yoshinobu
# Yamamoto, Aaron Ashby) gained 3-7 points on pitches where they still show
# real usage -- the intended direction in both cases, and proportional to
# how extreme the usage reading actually is (not a blanket penalty on
# every narrow arsenal regardless of usage level). effective_usage replaces
# usage_rate everywhere usage_rate previously fed the quotient formula;
# usage_rate itself is left unchanged in the output and still reported for
# transparency/debugging.
MEDIAN_ARSENAL_SIZE = 4

# MIN_PITCHES (25, season-long per pitch type) was designed purely as a
# DISPLAY filter -- "is there enough sample to trust showing this pitch its
# own quotient at all." Before Option C, that's all it affected. Now that
# arsenal_size feeds effective_usage for EVERY pitch a pitcher throws, that
# same 25-pitch bar was also silently deciding how much credit a pitcher's
# OTHER pitches get -- and 25 pitches across a full season is a low bar for
# "this is a real weapon broadening the arsenal" vs. a show-me pitch that
# happens to clear the display floor. Confirmed on real 2026 data: Drew
# Rasmussen's 6-pitch arsenal counted a 3.5%-usage CU and a 2.6%-usage ST
# that are barely thrown at all; his real, meaningfully-used arsenal is 4
# pitches, and his FC credit was inflated from 109 to 116 by the two
# nominal ones. Aaron Ashby's "5-pitch" arsenal similarly counted a
# 1.8%-usage show-me FF.
#
# MIN_ARSENAL_USAGE_RATE adds a SEPARATE, higher bar that a pitch type must
# clear to count toward a pitcher's arsenal_size specifically -- it still
# gets its own displayed quotient at MIN_PITCHES, it just doesn't inflate
# credit on the pitcher's OTHER pitches. Deliberately usage_rate-based
# rather than a second raw-count threshold: usage_rate already normalizes
# for how many total pitches that pitcher threw all season (a raw count
# doesn't -- 25 pitches means something different for a 300-pitch reliever
# than a 900-pitch workhorse starter), and n_pitches/total_pitches aren't
# retained anywhere downstream of the initial aggregation step, so a
# usage-rate floor was also the only one testable against the real
# pipeline without threading raw counts further through it.
#
# 0.035 (vs. 0.05 and 0.08, also tested) was chosen as the more surgical of
# the candidates: it still catches the two motivating cases above
# (Rasmussen's CU/ST, Ashby's FF all fall below it) while leaving more
# borderline-but-real arsenals untouched than a higher bar would -- e.g.
# Javier Assad's genuine 7-pitch mix (thinnest pitch at 3.7% usage) stays
# intact at 0.035 but would have been clipped to 6 at 0.05. Population-wide
# effect at 0.035: only 70 of 720 pitchers (vs. 140 at 0.05) see their
# arsenal_size change at all, mean |display-score delta| across every row
# is 0.21, and only one pitcher (Roki Sasaki's SL, arsenal 5->3) moves by
# more than single digits (129->116).
MIN_ARSENAL_USAGE_RATE = 0.035

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
#   - Splitters are valued specifically for dropping MORE than average (real
#     deception/tunneling off the fastball) -- "signed_neg", so more drop is
#     rewarded, not penalized. (Confirmed on real splitter examples:
#     Gausman's -- one of the most respected splitters in the game -- and
#     Sasaki's were both landing at/below league average under the old
#     signed treatment, which rewarded LESS drop on a pitch whose whole
#     purpose is heavy, late plunge.)
#   - Sliders, sweepers, slurves, cutters, and sinkers get real value from
#     either kind of unusual tilt -- "abs". A sinker's defining trait is
#     heavy sink (confirmed on Logan Webb, whose near-zero IVB -- elite,
#     maximal sink -- was ranking him near dead last under the old signed
#     treatment), but an unusually high-riding sinker/two-seam hybrid can
#     also be a real, distinct weapon, so both extremes are rewarded rather
#     than only one.
#   - Curveballs and knuckle curves switched signed_neg -> abs (2026-10
#     model review): the old treatment only rewarded exceptional drop, but a
#     tight, flatter power curve (less depth than average) is just as much a
#     distinct weapon as a huge sweeping 12-6 curve -- same "reward either
#     extreme" logic already applied to sliders/cutters/sinkers above. CU and
#     KC are kept configured identically everywhere in this file (the two
#     pitch types are too similar, and too inconsistently distinguished in
#     the underlying Statcast tagging, to justify separate hand-tuning) --
#     this was already true for VELO_SHAPE below, now true here too.
#   - Changeups are handled separately from this dict entirely (see
#     CH_IVB_GAP_WEIGHT / CH_IVB_RAW_WEIGHT below), the same way changeup
#     velocity already is -- a brief signed_neg -> abs flip lived here for
#     about an hour on 2026-10 before being replaced by the gap-blend
#     treatment once it became clear league-relative IVB alone can't
#     distinguish "mirrors the fastball's shape" from "diverges hard from
#     it," which is the actual question for a changeup.
#   - Four-seamers switched signed -> abs (2026-10 model review, done
#     pitch-by-pitch rather than as part of the batch above): real four-seam
#     fastballs get genuine value from an unusually LOW-carry, flat/heavy
#     shape too (Chris Sale's, Sean Manaea's, Logan Webb's are known,
#     deliberate weapons built around exactly that), not just from
#     exceptional ride -- same "reward either extreme" logic as every other
#     entry in this dict. Unlike those, though, FF's below-average side is
#     ALSO discounted relative to its above-average side rather than
#     credited equally -- see IVB_WEIGHT_BELOW_AVG_OVERRIDE above for why
#     and how.
# Defaults to "signed" for any pitch type not listed here.
IVB_SHAPE = {
    "SL": "abs", "ST": "abs", "SV": "abs", "FC": "abs", "SI": "abs",
    "CU": "abs", "KC": "abs", "FS": "signed_neg", "FF": "abs",
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
#
# Cutters switched signed -> abs (2026-10 model review, FC pass): a fresh
# PCA pass found FC's variance splits ~evenly across 3-4 components (no
# dominant archetype, same issue as FF/SI), but flagged a real, substantial
# axis (PC3, ~23% of variance) that's almost purely IVB-driven and nearly
# independent of velocity -- the "depth is the whole pitch" archetype this
# file's IVB_SHAPE docstring already names (a Rasmussen-style cutter).
# Checking real examples confirmed IVB's existing "abs" shape already
# credits that archetype correctly and symmetrically (extreme-depth
# cutters' IVB z-score is just as large as extreme-carry cutters'), but
# the signed velocity term was silently canceling most of that credit out
# -- real extreme-depth cutters in the data are mostly thrown notably
# SLOWER than average, so the old "faster always rewarded" shape was
# penalizing the exact pitchers PC3 said should be recognized. Same "both
# extremes can be elite" logic as CU/KC/FS above: a firm, hard cutter and
# a slow, loopy, slider-like one can both be distinct weapons.
#
# Sliders switched signed -> abs (2026-10 model review, SL pass): same "both
# extremes can be elite" logic as CU/KC/FC above, but the swing from making
# this change is the largest of any pitch type reviewed so far (mean |delta|
# 5.10, max |delta| 67, 40/443 pitchers moving >=10 points even WITH the
# 0.35 discount below) -- driven by a handful of legitimately extreme
# slow-slider archetypes (Chris Sale 79.7mph, Patrick Corbin 79.0mph, and
# others in the 76-78mph range) sitting many standard deviations below a
# tight league SL velocity distribution (mean 86.3, std 2.6). Important
# mechanical note confirmed during testing: even at a FULLY NEUTRAL discount
# (0.0, zero velocity credit for below-average rows), the swing floor is
# still large (max |delta| 51, 27 movers >=10) -- most of the swing comes
# from REMOVING the old "signed" shape's active penalty on these legitimate
# outliers, not from adding new credit. So unlike FC/CU/KC, there's no
# setting that reduces Sale/Corbin-magnitude moves to single digits; the
# discount below (0.35) is a deliberate, conservative choice above that 0.0
# floor, not an attempt to minimize the swing further.
#
# Sweepers switched signed -> abs (2026-10 model review, ST pass, done after
# the SL pass above and after both the SL<->ST hand reclassifications --
# see SL_TO_ST_RECLASSIFY/ST_TO_SL_RECLASSIFY): tested against the final,
# post-reclassification ST population (301 rows, velo mean 82.4/std 2.8).
# "abs" was chosen over "signed_neg" (full reversal, rewarding slow and
# penalizing fast) -- signed_neg was conclusively rejected: it scrambled the
# leaderboard (Spearman -0.10 vs the old signed baseline, mean |delta| 10.61,
# 121/301 movers >=10) and specifically punished known elite power-sweeper
# archetypes (Max Meyer, 89.0mph, 676974: 130 -> 84, a 46-point drop), with
# no corresponding upside -- there's no real-world case that a hard sweeper
# is WORSE for being hard. "abs" protects Meyer (stays ~130-133 across every
# discount tested) while still recognizing deceptive, known soft-sweeper
# archetypes the old "signed" shape was penalizing (Sean Manaea 640455,
# Ryan Yarbrough 642232, Tom Cosgrove 676680, Carson Palmquist 687223 --
# all pitchers with a broader reputation for success via deception/funk
# rather than velocity).
#
# Unlike SL, this one came with a real, explicit counter-argument that was
# evaluated before shipping: ST's velo and horizontal_in are correlated at
# r=0.30 in this population, a moderate "double-counting" risk (a slow
# sweeper may already be credited for the same velocity/movement tradeoff
# via the boosted horizontal weight). r=0.30 was judged suggestive but not
# dominant, and the named-archetype evidence (Meyer protected, known
# deceptive-lefty group correctly recognized) was judged to outweigh it --
# but see the discount choice below for how that skepticism was still
# incorporated.
#
# Follow-up (2026-10, revisited the day after shipping): tested whether a
# NEGATIVE below-average discount (e.g. -0.20 -- still abs-shaped, but
# clawing back part of the credit rather than just zeroing it out) would be
# a better answer to the double-counting concern above. Two things came out
# of testing this properly:
#   1. The original r=0.30 was computed on raw velo vs. raw horizontal_in
#      across the WHOLE population -- not a measure of actual overlap in
#      the scoring formula. Directly checking correlation between the
#      actual CREDIT terms (velo_weight*velo_z vs. horiz_weight*horiz_z)
#      for the population the discount actually touches (below-average-
#      velocity rows, n=149) found no meaningful overlap at all: r=0.06
#      with horizontal credit (p=0.47), r=0.12 with IVB credit (p=0.15).
#      Full-population velo-credit-vs-horiz-credit correlation was even
#      slightly NEGATIVE (-0.26).
#   2. Named check confirms this directly: Manaea (640455) and Palmquist
#      (687223), the two pitchers a negative discount would hit hardest,
#      rank in the bottom ~10th percentile of the ST population on IVB
#      credit and only ~35th percentile on horizontal credit -- their
#      velocity credit is not icing on an already-strong movement profile,
#      it's close to the ONLY thing distinguishing them. The double-
#      counting story specifically does not hold for the pitchers it would
#      most affect.
# Conclusion: the one substantive reason to move off 0.25 failed a direct
# test, so 0.25 stands. (Also worth being honest about: 0.25 itself sits on
# a smooth, inflection-free sweep curve just like -0.20 would -- it is not
# "data-derived" in the sense of a real breakpoint, it was a deliberate,
# conservative judgment call. That's an accurate description of this
# number, not a flaw unique to it.)
#
# Slurves switched signed -> abs (2026-10 model review, SV pass, done after
# the full ST pass above): SV is the thinnest population reviewed this
# round (15 qualifying pitchers, velo range 77.2-87.3mph, mean 82.0/std
# 2.8) -- too small to trust a precisely-calibrated below-average discount
# the way SL (0.35) and ST (0.25) got, so this was deliberately kept to a
# directional/shape question only, no weight override. The shape case
# itself is the same analogical one as every other breaking ball in this
# dict: SV is a slider/curveball hybrid, and both a hard/firm slurve and a
# slow/loopy one are plausible distinct weapons, matching CU/KC/FS/FC/SL/
# ST's "both extremes can be elite" logic -- SV was simply the one pitch
# type this hadn't been applied to yet. Tested against the real population:
# Spearman 0.70 vs the old signed baseline, mean |delta| 5.80, max |delta|
# 20, 2/15 movers >=10 -- a real reshuffle, but expected and accepted at
# this sample size: with only 15 pitchers, one or two individuals (Mitch
# Bratt, 79.6mph, +20; Michael Soroka, 80.6mph, +11) necessarily carry a
# large share of any population-wide change. No below-average discount
# override was added on top of this -- full symmetric credit, consistent
# with not over-fitting a magnitude decision to this few data points.
VELO_SHAPE = {
    "CU": "abs", "KC": "abs", "FS": "abs", "FC": "abs", "SL": "abs", "ST": "abs",
    "SV": "abs",
}

# Changeup velocity is scored as a blend of two things, rather than a single
# shape flag like every other pitch type:
#   1. Velocity SEPARATION from the pitcher's own fastball (the harder of
#      their four-seam or sinker, whichever they throw) -- the main driver
#      of a changeup's deception, and the majority of the weight.
#   2. The RESIDUAL of raw changeup velocity after removing the part
#      mechanically explained by that same gap -- i.e. "is this changeup
#      faster or slower than you'd expect GIVEN how much separation this
#      pitcher gets," not raw velocity in a flat, context-free sense. A
#      smaller, secondary term: there's still real value in a changeup that
#      beats the population's own gap/velocity tradeoff (genuinely hard AND
#      genuinely well-separated at once), independent of how large the gap
#      itself is.
#
# 2026-10 model review, CH velocity pass: originally this second term was
# just raw velocity, signed, blended directly with the gap term. Checking
# this against the real 2026-season CH population (400 pitchers) found a
# real design problem: gap (fastball velo minus CH velo) and raw CH velocity
# are correlated at r=-0.70 (R^2=0.49) -- not surprising in hindsight, since
# a harder changeup almost mechanically means a smaller gap for a given
# fastball. The two terms were not complementary signals, they were mostly
# the same signal pointed in opposite directions: 74% of CH pitchers
# (294/398 with a valid fastball baseline) had gap_z and raw_z on OPPOSITE
# sides of average, meaning the supposedly-independent 30% raw term was
# mostly just partially canceling the dominant 70% gap term rather than
# adding real information. Concretely, hard/small-gap pitchers (Camilo
# Doval, 94.5mph CH off a 98.2mph FB, 3.7in-mph gap) were getting a strongly
# negative gap_z (-1.93) only partially offset by a positive raw_z (+2.60) --
# net negative overall, despite a legitimately hard changeup -- while
# big-gap/soft-CH pitchers (Craig Yoho, 15.5mph gap) were getting penalized
# on raw_z (-2.26) for a softness that was already fully explained by (and
# arguably the whole point of) their unusually large gap.
#
# Fix: residualize. Fit raw_z = intercept + slope*gap_z by OLS across the
# population with a valid fastball baseline (refit every run, same as every
# other z-score in this model -- not a fixed historical coefficient), then
# use the LEFTOVER (raw_z minus that fitted prediction) as the second blend
# term instead of raw_z itself. The residual is uncorrelated with gap_z by
# construction, so it adds genuinely new information: a pitcher whose
# changeup is unusually hard AND well-separated (Jhoan Duran, Andres Munoz,
# Adrian Morejon, Jacob Misiorowski) now shows a real positive residual
# (+1.5 to +2.0) that the old raw_z blend couldn't distinguish from "just
# has a small gap, nothing special." A pitcher who's slow AND doesn't even
# have the separation to show for it (Alek Manoah) shows a real negative
# residual, distinct from "slow because huge gap" (which the residual
# correctly stops penalizing). Pitchers whose only distinguishing trait was
# "hard changeup, small gap" (Doval, Edward Cabrera) see a modest net
# decline -- their apparent hardness turns out to be mostly what their small
# gap already predicts, not an independent plus. Named/population testing
# against the real 2026-season CH population (swapping in the residual at
# the then-current 70/30 split first): Spearman 0.98 vs. the old raw-blend
# baseline, mean |delta| 1.05, max |delta| 6, 0/400 pitchers moving >=10 --
# a real, sensible reshuffle, gentle in magnitude since other dimensions
# (horizontal, IVB, usage) dilute any one sub-component's swing.
#
# Weight re-derivation: swept the gap/residual split from 1.0/0.0 (gap-only)
# down through 0.5/0.5 against the old raw-blend baseline; no inflection
# point, smooth curve like every other sweep in this file (0.5/0.5: mean
# |delta| 1.07/max 6; 0.7/0.3: 1.05/6; 0.8/0.2: 1.57/8; 0.9/0.1: 2.06/11,
# 2 movers >=10; 1.0/0.0: 2.44/13, 6 movers >=10). Shipped at 0.6/0.4
# (closest net stability to the old baseline, 0.73/5, though not uniquely
# "correct" any more than any other point on that curve) -- gives the
# genuinely-independent residual signal slightly more say than a straight
# carryover of the old 70/30 ratio would, since unlike the old raw_z, it's
# no longer mostly redundant with the gap term.
#
# The two weights sum to 1.0, the same total weight every other pitch type's
# single velocity term carries, so CH's velocity dimension stays on the same
# overall scale as the rest of the model -- just split between two signals
# instead of one.
CH_VELO_GAP_WEIGHT = 0.6
CH_RAW_VELO_WEIGHT = 0.4

# Changeup IVB is scored as the same kind of two-term blend as changeup
# velocity above, added 2026-10 once it became clear a league-relative shape
# flag (signed/signed_neg/abs) can't tell "this changeup mirrors its own
# pitcher's fastball" apart from "this changeup diverges hard from it" --
# both could have identical league-relative IVB, but they're very different
# pitches from a deception standpoint:
#   1. IVB SEPARATION from the pitcher's own fastball baseline (the same
#      harder-of-FF/SI reference pitch used for the velocity gap above) --
#      the main signal, and the majority of the weight.
#   2. Raw changeup IVB itself, league-relative -- a smaller, secondary
#      term, since a changeup that moves a lot in absolute terms is still
#      harder to square up on its own merits, independent of what it's
#      tunneling off of.
# Unlike the velocity blend, BOTH terms here use abs() before blending,
# not after: real pitcher data shows changeup IVB has no single "better"
# direction either for the raw metric (an unusually flat/rising changeup
# can be as distinct a weapon as an unusually diving one) or for the gap
# itself (true fastball-mirroring and extreme late divergence are both
# plausible distinct deception profiles, and there's no outcome data in
# this model to say one beats the other -- see CH_IVB_GAP_WEIGHT's
# discussion). Folding each term to a distance BEFORE blending avoids an
# extreme-positive gap and an extreme-negative raw IVB silently canceling
# each other out in the blend, the way they could if abs() were applied to
# the combined signed composite instead.
# The weights match the velocity blend's split for consistency, with the
# same caveat the velocity split carries: this is a judgment call, not
# something empirically fit (there's no outcome data in this model to
# derive it from).
CH_IVB_GAP_WEIGHT = 0.7
CH_IVB_RAW_WEIGHT = 0.3

# Per-pitch-type override for how much horizontal break counts toward the
# Ceiling formula, in place of the global HORIZ_WEIGHT. Sliders, sweepers,
# and slurves get real, distinct value from horizontal movement
# specifically -- a pitcher's slider can be a weapon because of exceptional
# sweep even with unremarkable depth, which the default fastball-tuned
# weighting (where vertical movement dominates) badly undersells.
#
# SL 0.49 (2026-10 model review, SL horizontal-weight pass): the previous
# value here, 0.65, turned out to have NO fact-based derivation behind it --
# unlike every other weight in this file, it was never swept or spot-checked
# against real data; it was a plausible-sounding number picked by feel when
# IVB_SHAPE["SL"] was set to "abs" in an earlier commit. Re-derived from
# scratch: swept 0.25 (the global default, i.e. "no SL-specific override at
# all") up through 1.10 against the real 2026-season SL population, both
# before and after the SL_TO_ST_RECLASSIFY move above (horizontal weight
# turned out to matter a LOT for SL -- unlike SI's analogous check, which
# found sweeping 0.25->0.65 was a near no-op -- so this is a real, not
# cosmetic, decision). 0.49 was chosen as the point where genuine sweep/
# break outliers (both directions -- extreme sweep and Sasaki-style
# opposite-direction break) still get real, substantial recognition, while
# population churn stays low (0 pitchers moved >=10 points vs. the 0.25
# baseline, pre-reclassification; 2 moved >=10 post-reclassification, still
# far gentler than 0.65's 3-5). 0.65 was the first weight on the sweep where
# that stability broke down for marginal extra credit to the same handful of
# outliers -- diminishing returns for real cost.
#
# ST 1.05 (2026-10 model review, ST horizontal pass, done after the ST
# velocity and IVB passes above): the previous value here, 0.65, turned out
# to have the exact same unvalidated provenance as SL's old 0.65 -- it was
# introduced in the same commit, with no sweep or spot-check behind it
# either. Re-derived from scratch: swept 0.25 (no override) through 1.56
# (the PCA candidate) against the final, post-reclassification ST
# population (301 rows). Two things came out of this:
#   1. Shape: HORIZ_SHAPE["ST"] stays "signed_neg" (NOT flipped to "abs").
#      Tested abs anyway for rigor -- at the (then-current) 0.65 weight it
#      reshuffled the leaderboard significantly (Spearman 0.83, mean|delta|
#      4.15, max|delta| 29, 19/301 movers >=10), but the WRONG direction:
#      unlike SL (which has real "wrong-way breaker" outliers, e.g.
#      Sasaki-style, that abs correctly credits), zero of the 301 ST
#      pitches break arm-side at all -- every single one breaks glove-side,
#      consistent with that being the pitch's defining trait. abs would
#      instead reward mediocre sweep (e.g. Poulin -7.7in, well below the
#      -13.6in population average) just for being numerically far from the
#      mean on the "wrong" side, treating weak sweep as if it were an
#      equally valid archetype to extreme sweep -- no real-world story
#      supports that, so abs was rejected and signed_neg kept.
#   2. Weight: the sweep showed 0.49-0.85 is a genuinely stable zone (0
#      pitchers moving >=10 points anywhere in that range), meaning the old
#      0.65, despite being unvalidated, happened to land somewhere
#      defensible. 1.56 (the raw PCA candidate) was tested and explicitly
#      REJECTED: it hits Max Meyer (676974, the 89mph power sweeper
#      protected by the VELO_SHAPE/VELO_WEIGHT_BELOW_AVG_OVERRIDE and
#      IVB_WEIGHT_OVERRIDE decisions above, whose sweep is only -8.9in,
#      well below average) for -23 points (135->112), since he earns his
#      value from velocity/IVB rather than extreme horizontal -- the same
#      "punished for not leaning on the bumped dimension" failure mode that
#      sank CU/KC's rejected movement-weight bump elsewhere in this file.
#      1.05 was chosen as a deliberate middle ground past the fully-stable
#      zone: it gives real, visible extra credit to genuine sweep outliers
#      (Gibson, Cade at -21.5in: 122->131) while accepting a modest,
#      bounded cost to pitchers whose value comes from elsewhere (Meyer
#      135->123, Poulin 99->89) rather than chasing the full PCA ratio's
#      much larger swing.
HORIZ_WEIGHT_OVERRIDE = {
    "SL": 0.49, "ST": 1.05, "SV": 0.5,
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
#
# Sinkers, curveballs/knuckle curves, and sliders switched signed/signed_neg
# -> abs (2026-10 model review): each had the same underlying issue as
# four-seamers above -- real pitchers get value from unusually large break
# in EITHER direction (a sinker with almost no arm-side run but elite
# sink/cut character, a tight short curve, a slider that breaks the "wrong"
# way but still misses bats), not just from more of whichever single
# direction happened to be flagged as "correct." Sweepers and slurves are
# left as signed_neg for now pending further review (see the open
# re-weighting note near HORIZ_WEIGHT_OVERRIDE below -- ST's shape and
# weight are tangled up with a larger pending change, not a quick swap like
# these).
# Defaults to "signed" for any pitch type not listed here.
HORIZ_SHAPE = {
    "FF": "abs",
    "FC": "signed_neg",
    "SI": "abs", "CU": "abs", "KC": "abs", "CS": "signed_neg",
    "SL": "abs", "ST": "signed_neg", "SV": "signed_neg",
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


def fetch_yesterdays_pitch_events() -> pd.DataFrame:
    """Every individual pitch thrown league-wide on a single day (yesterday,
    server-local date) for the home page's "Yesterday's Best Pitches" box.
    Reuses the same chunk-fetch helper as the season pull, since one day's
    worth of pitches league-wide is always well under Savant's ~25,000-row
    export cap -- no chunking needed.

    Pulls a 3-day-wide window (the day before yesterday through today) and
    then filters down to exactly yesterday's rows by the event-level
    game_date column, rather than trusting game_date_gt/game_date_lt to be
    exclusive or inclusive at the boundary -- the season fetch above gets
    away with window math that assumes a particular boundary behavior
    because small overlaps or gaps between MULTI-day chunks don't matter
    (everything gets concatenated into one season-long dataframe either
    way), but a single-day box is exactly the case where getting one extra
    or missing day would be a visible, wrong answer, so this confirms the
    actual date explicitly instead."""
    yesterday = datetime.now().date() - timedelta(days=1)
    window_start = yesterday - timedelta(days=1)
    window_end = yesterday + timedelta(days=1)
    df = _fetch_pitch_events_chunk(window_start.strftime("%Y-%m-%d"), window_end.strftime("%Y-%m-%d"))
    if df.empty:
        print(f"statcast_search (daily): no pitch data returned for {yesterday.isoformat()}")
        return df
    date_col = next((c for c in ("game_date", "game_date_utc") if c in df.columns), None)
    if date_col is None:
        print(f"WARNING: statcast_search (daily) export has no game_date column -- "
              f"can't confirm the {len(df)} rows pulled are exactly {yesterday.isoformat()}; "
              f"using them all as-is. Columns were: {list(df.columns)}")
        return df
    yesterday_str = yesterday.isoformat()
    out = df[df[date_col].astype(str).str.startswith(yesterday_str)].copy()
    print(f"statcast_search (daily): pulled {len(out)} pitch rows for {yesterday_str} "
          f"(of {len(df)} in the wider fetch window).")
    return out


def _aggregate_pitch_events(events: pd.DataFrame, source_label: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Shared core behind both the season aggregation
    (compute_pitch_metrics_from_events) and the single-day aggregation
    (compute_daily_pitch_aggregates): Savant column resolution, handedness-
    normalized horizontal break, and the groupby(player_id, pitch_type)
    itself. Returns (grouped, extension); grouped has one row per
    (player_id, pitch_type) with mean velo/spin_rpm/ivb_in/horizontal_in,
    n_pitches, total_pitches, and usage_rate -- deliberately UNFILTERED by
    any sample-size floor, since the season and daily callers each apply a
    different one (MIN_PITCHES/MIN_SEASON_PITCHES_TO_QUALIFY vs.
    DAILY_MIN_PITCHES)."""
    df = normalize_player_id(events, source_label)

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

    # SL->ST hand reclassification (see SL_TO_ST_RECLASSIFY's definition
    # above for the full rationale) -- applied here, at the raw per-pitch-
    # event level, BEFORE the groupby(player_id, pitch_type) below, so a
    # pitcher who already throws a separate real sweeper (Nolan McLean) gets
    # this pitch correctly merged into that same group via the ordinary
    # aggregation rather than needing special-case merge logic of its own.
    reclass_mask = df["player_id"].isin(SL_TO_ST_RECLASSIFY) & (df["pitch_type"] == "SL")
    if reclass_mask.any():
        print(f"SL->ST hand reclassification ({source_label}): remapping "
              f"{reclass_mask.sum()} raw pitch events for "
              f"{df.loc[reclass_mask, 'player_id'].nunique()} pitcher(s).")
        df.loc[reclass_mask, "pitch_type"] = "ST"

    # ST->SL hand reclassification -- the mirror image of the block just
    # above (see ST_TO_SL_RECLASSIFY's definition above for the full
    # rationale), applied the same way and for the same reason: a pitcher
    # here who already throws a separate real slider gets this pitch
    # correctly merged into that group via the ordinary aggregation below.
    reclass_mask_st = df["player_id"].isin(ST_TO_SL_RECLASSIFY) & (df["pitch_type"] == "ST")
    if reclass_mask_st.any():
        print(f"ST->SL hand reclassification ({source_label}): remapping "
              f"{reclass_mask_st.sum()} raw pitch events for "
              f"{df.loc[reclass_mask_st, 'player_id'].nunique()} pitcher(s).")
        df.loc[reclass_mask_st, "pitch_type"] = "SL"

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

    ext = df.dropna(subset=["extension_ft"])
    if ext.empty:
        extension = pd.DataFrame(columns=["player_id", "extension_ft"])
    else:
        extension = ext.groupby("player_id")["extension_ft"].mean().reset_index()

    return grouped, extension


def compute_pitch_metrics_from_events(events: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Aggregate raw pitch-level rows into (a) one row per pitcher per pitch
    type with average velo/spin/movement/usage, and (b) one row per pitcher
    with a usage-weighted average extension. Season-long: filters out
    position players (too few total pitches) and individual pitch types
    thrown too rarely to trust (MIN_PITCHES)."""
    grouped, extension = _aggregate_pitch_events(events, "statcast_search")

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
    return pitch_metrics, extension


def compute_daily_pitch_aggregates(events: pd.DataFrame) -> pd.DataFrame:
    """One row per (player_id, pitch_type) for a single day's pitch events,
    for the home page's "Yesterday's Best Pitches" box. Filtered only by
    DAILY_MIN_PITCHES -- a single day's events obviously can't clear a
    season-long total-pitches qualifier, and daily scoring deliberately
    reuses the season's own usage_rate/delivery_modifier/fastball_baseline
    rather than computing day-specific versions of those (see
    compute_daily_display_scores), so none of that filtering applies here."""
    grouped, _ = _aggregate_pitch_events(events, "statcast_search (daily)")
    grouped = grouped[grouped["n_pitches"] >= DAILY_MIN_PITCHES].copy()
    return grouped[["player_id", "pitch_type", "velo", "spin_rpm", "ivb_in", "horizontal_in", "n_pitches"]]


# --------------------------------------------------------------------------
# 2. Pull active-spin data
# --------------------------------------------------------------------------

def fetch_active_spin() -> pd.DataFrame:
    """Active-spin% by pitcher and pitch type, from Savant's active-spin
    leaderboard. Returns columns: player_id, pitch_type, active_spin_pct.
    Savant publishes this for FF/SI/FC/CH/FS/CU/SL/ST/SV -- FO/KC/CS simply
    won't have a row here, which is expected (handled below by treating
    missing = None, not 0). (Splitter was previously believed unavailable --
    confirmed via a real Savant export on 2026-09-30 that active_spin_splitter
    is a real column with a usable sample size, ~115 of ~700 pitchers that
    season.)"""
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
        "splitter": "FS", "split": "FS", "fs": "FS",
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


def build_fastball_baseline(pitch_metrics: pd.DataFrame) -> pd.DataFrame:
    """Each pitcher's hardest fastball-family pitch (four-seam or sinker --
    whichever is harder for that pitcher, since either can be the "primary"
    heater a changeup is meant to look like out of the hand) this season.
    Returns a DataFrame indexed by player_id with that one pitch's velocity
    AND induced vertical break, both pulled from the SAME fastball row --
    so a changeup's velocity gap and IVB gap are always measured against
    the one specific heater it's actually meant to tunnel off of, never an
    average of two different pitches. A pitcher who throws neither simply
    has no entry (handled as a neutral/no-gap-signal case downstream, same
    philosophy as a pitcher missing from the arm-angle leaderboard getting
    a neutral delivery modifier rather than a penalty)."""
    fastball_rows = pitch_metrics[pitch_metrics["pitch_type"].isin(["FF", "SI"])]
    hardest_idx = fastball_rows.groupby("player_id")["velo"].idxmax()
    return fastball_rows.loc[hardest_idx].set_index("player_id")[["velo", "ivb_in"]]


def compute_pitch_quotients(pitch_metrics: pd.DataFrame, active_spin_fallback: pd.DataFrame,
                             delivery_modifiers: pd.DataFrame) -> pd.DataFrame:
    df = pitch_metrics.copy()

    # arsenal_size/effective_usage -- see MEDIAN_ARSENAL_SIZE's definition
    # above (Option C, 2026-10 usage-rate review) for the full rationale.
    # Computed from this same pitch_metrics frame (one row per qualifying
    # player_id/pitch_type) before any merge below, so it reflects each
    # pitcher's real qualifying arsenal regardless of what active-spin or
    # delivery data is or isn't available for them.
    #
    # Only pitch types clearing MIN_ARSENAL_USAGE_RATE count toward
    # arsenal_size (see its definition above) -- a pitch below that bar
    # still gets its own row/quotient/display_score below, it just doesn't
    # inflate credit on the pitcher's OTHER pitches by counting as part of
    # a "broad arsenal." fillna(1) covers the (practically unreachable,
    # since usage_rate sums to 1.0 across a real pitcher's arsenal) edge
    # case of a player with zero pitches clearing the bar at all.
    arsenal_counts = (
        df[df["usage_rate"] >= MIN_ARSENAL_USAGE_RATE]
        .groupby("player_id")["pitch_type"].nunique()
    )
    df["arsenal_size"] = df["player_id"].map(arsenal_counts).fillna(1).astype(int)
    df["effective_usage"] = df["usage_rate"] * (df["arsenal_size"] / MEDIAN_ARSENAL_SIZE)

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
        velo_z_raw = zscore(group["velo"])  # kept pre-abs/pre-sign-flip: VELO_WEIGHT_BELOW_AVG_OVERRIDE
                                             # needs to know which rows were below league average
                                             # BEFORE any shape transform collapses that information.
        velo_z = velo_z_raw
        if pt == "CH":
            # Blend velocity-separation-from-own-fastball with the RESIDUAL
            # of raw velocity after removing the part explained by that same
            # gap (see CH_VELO_GAP_WEIGHT/CH_RAW_VELO_WEIGHT's definition
            # above for the full rationale and the r=-0.70 problem this
            # fixes) instead of a single shape flag. A pitcher with no
            # qualifying FF/SI this season has no baseline to compare
            # against -- treat both the gap and residual terms as neutral
            # (0) for just those rows rather than penalizing or rewarding on
            # an undefined basis.
            baseline_velo = group["player_id"].map(fastball_baseline["velo"])
            velo_gap = baseline_velo - group["velo"]
            gap_z_raw = zscore(velo_gap)
            has_baseline = gap_z_raw.notna()
            gap_z = gap_z_raw.fillna(0.0)
            if has_baseline.sum() >= 2 and gap_z[has_baseline].std() > 0:
                slope, intercept = np.polyfit(gap_z[has_baseline], velo_z[has_baseline], 1)
                residual = (velo_z - (intercept + slope * gap_z)).where(has_baseline, 0.0)
            else:
                residual = pd.Series(0.0, index=group.index)
            velo_z = CH_VELO_GAP_WEIGHT * gap_z + CH_RAW_VELO_WEIGHT * residual
        elif VELO_SHAPE.get(pt) == "abs":
            velo_z = velo_z.abs()
        elif VELO_SHAPE.get(pt) == "signed_neg":
            velo_z = -velo_z
        ivb_z_raw = zscore(group["ivb_in"])  # kept pre-abs/pre-sign-flip: IVB_WEIGHT_BELOW_AVG_OVERRIDE
                                              # needs to know which rows were below league average
                                              # BEFORE any shape transform collapses that information.
        ivb_z = ivb_z_raw
        if pt == "CH":
            # Blend IVB-separation-from-own-fastball with raw league-
            # relative IVB (see CH_IVB_GAP_WEIGHT/CH_IVB_RAW_WEIGHT above),
            # the same two-signal idea as changeup velocity just above --
            # except both terms are folded to a distance (abs) before
            # blending, since neither has a single "better" direction the
            # way velocity's gap and raw terms do. Same neutral (0) handling
            # for a pitcher with no qualifying FF/SI baseline.
            baseline_ivb = group["player_id"].map(fastball_baseline["ivb_in"])
            ivb_gap = baseline_ivb - group["ivb_in"]
            ivb_gap_z = zscore(ivb_gap).abs().fillna(0.0)
            ivb_raw_z = ivb_z.abs()
            ivb_z = CH_IVB_GAP_WEIGHT * ivb_gap_z + CH_IVB_RAW_WEIGHT * ivb_raw_z
        elif IVB_SHAPE.get(pt) == "abs":
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

        # Most pitch types use a single flat IVB_WEIGHT for every row. A
        # pitch type listed in IVB_WEIGHT_BELOW_AVG_OVERRIDE (FF) or
        # IVB_WEIGHT_ABOVE_AVG_OVERRIDE (SI -- see both definitions above)
        # instead gets a per-row weight: the discounted override weight on
        # whichever side of league-average IVB that pitch type's dict
        # targets, full IVB_WEIGHT on the other side. The two dicts are
        # mutually exclusive per pitch type (nothing is listed in both).
        # pt == "CH" is excluded here on purpose -- its IVB term is already
        # its own gap-blend composite (see above), not a plain
        # shape-flagged z-score, so this conditional-discount concept
        # doesn't apply to it.
        # Flat/symmetric per-pitch-type override (IVB_WEIGHT_OVERRIDE, SL's
        # 0.60 so far) in place of the global IVB_WEIGHT -- resolved first so
        # the direction-conditional dicts below layer on top of the right
        # base weight rather than always falling back to the fastball-tuned
        # global default. Mutually exclusive in practice with the two dicts
        # below (no pitch type listed in both as of this change).
        base_ivb_weight = IVB_WEIGHT_OVERRIDE.get(pt, IVB_WEIGHT)
        below_avg_ivb_weight = IVB_WEIGHT_BELOW_AVG_OVERRIDE.get(pt)
        above_avg_ivb_weight = IVB_WEIGHT_ABOVE_AVG_OVERRIDE.get(pt)
        if pt != "CH" and below_avg_ivb_weight is not None:
            ivb_weight = pd.Series(base_ivb_weight, index=group.index)
            ivb_weight[ivb_z_raw < 0] = below_avg_ivb_weight
        elif pt != "CH" and above_avg_ivb_weight is not None:
            ivb_weight = pd.Series(base_ivb_weight, index=group.index)
            ivb_weight[ivb_z_raw >= 0] = above_avg_ivb_weight
        else:
            ivb_weight = base_ivb_weight

        # Velocity's own conditional weight, the mirror of the IVB one just
        # above -- see VELO_WEIGHT_BELOW_AVG_OVERRIDE's definition for the
        # reasoning. Only a below-average-side override dict exists for
        # velocity so far (FC); unlike IVB there's no pitch type yet needing
        # the opposite (above-average velocity discounted), so there's no
        # analogous "above_avg_velo_weight" branch here.
        below_avg_velo_weight = VELO_WEIGHT_BELOW_AVG_OVERRIDE.get(pt)
        if pt != "CH" and below_avg_velo_weight is not None:
            velo_weight = pd.Series(1.0, index=group.index)
            velo_weight[velo_z_raw < 0] = below_avg_velo_weight
        else:
            velo_weight = 1.0

        ceiling = (
            velo_weight * velo_z
            + ivb_weight * ivb_z
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
            ceiling * (group["effective_usage"] ** USAGE_RATE_EXPONENT) * group["delivery_modifier"]
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
        "active_spin_pct", "usage_rate", "arsenal_size", "effective_usage",
        "active_spin_quotient", "delivery_modifier", "quotient", "display_score",
    ]]


def _season_pop_zscore(day_values: pd.Series, season_values: pd.Series) -> pd.Series:
    """z-score a single day's raw values against the SEASON population's own
    mean/std for that same metric and pitch type, rather than the day's own
    (much smaller, noisier) cross-section -- so a daily score of "100"
    keeps meaning the same thing as a season "100": average by the
    season-established yardstick, not average-for-one-random-Tuesday."""
    day_values = pd.to_numeric(day_values, errors="coerce")
    season_values = pd.to_numeric(season_values, errors="coerce")
    mean, std = season_values.mean(), season_values.std(ddof=0)
    if not std or math.isnan(std):
        return day_values.fillna(0) * 0.0
    return (day_values - mean) / std


def compute_daily_display_scores(daily_agg: pd.DataFrame, pitch_metrics: pd.DataFrame,
                                   fastball_baseline: pd.DataFrame, game_date: str) -> pd.DataFrame:
    """Rescore a single day's pitches (`daily_agg`, from
    compute_daily_pitch_aggregates) onto the exact same uScore+ yardstick as
    the season leaderboard, for the home page's "Yesterday's Best Pitches"
    box (2026-10 design discussion). Mirrors compute_pitch_quotients'
    shape/weight logic pitch-type by pitch-type, but:

    - every z-score is taken against the SEASON population's mean/std for
      that pitch type (via _season_pop_zscore), never the day's own tiny
      cross-section -- a day might have only a handful of pitchers throwing
      a given pitch type at all, nowhere near enough to self-calibrate a
      meaningful distribution.
    - effective_usage (Option C's arsenal-corrected usage_rate -- see
      MEDIAN_ARSENAL_SIZE above) and delivery_modifier are borrowed from the
      SEASON pitch_metrics row for that (player_id, pitch_type) rather than
      computed fresh -- a single day's game plan or pitch count says little
      about a pitcher's real mix or release, and there's no daily delivery
      reading (or daily arsenal_size) to compute from in the first place. A
      pitcher who threw this pitch type yesterday but has no qualifying
      SEASON row for it (hasn't cleared MIN_PITCHES/MIN_SEASON_PITCHES_TO_QUALIFY
      yet) is dropped -- there's no season yardstick to place them on.
    - the changeup's fastball-gap terms (velocity and IVB) use the SEASON
      fastball_baseline and are z-scored against the SEASON gap
      distribution, for the same reason -- and for the same reason CH is
      skipped here if `fastball_baseline` has no row for a given pitcher.
    - active spin is excluded entirely -- Savant doesn't publish a daily
      active-spin reading, and an empirical A/B run against this same
      season formula with active spin zeroed out showed negligible impact
      (Spearman rank-correlation 0.994-1.000 across pitch types, max
      display-score shift 1-4 points, no pitcher moved 10+ points) -- so
      this omission doesn't change what the resulting box represents in
      any meaningful way.
    - the final display score is rescaled against the SEASON quotient
      distribution for that pitch type (mean/std of pitch_metrics'
      `quotient` column), not the day's own quotients -- same reasoning as
      every other z-score here.
    """
    out_frames = []
    for pt, day_group in daily_agg.groupby("pitch_type"):
        season_group = pitch_metrics[pitch_metrics["pitch_type"] == pt]
        if season_group.empty:
            # No season leaderboard exists yet for this pitch type at all --
            # nothing to place a daily reading against.
            continue
        day_group = day_group.copy()

        velo_z_raw = _season_pop_zscore(day_group["velo"], season_group["velo"])  # kept pre-shape,
                                              # same reason as compute_pitch_quotients: needed to know
                                              # which rows are below the SEASON average velocity before
                                              # any abs()/sign-flip collapses that information.
        velo_z = velo_z_raw
        if pt == "CH":
            # Residualized gap/velocity blend -- see CH_VELO_GAP_WEIGHT's
            # definition in compute_pitch_quotients for the full rationale.
            # The gap->raw-velocity relationship is fit against the SEASON
            # population (never the day's own tiny cross-section), for the
            # same stability reason _season_pop_zscore itself exists -- a
            # day's handful of CH pitchers is nowhere near enough to fit a
            # trustworthy regression line of its own.
            baseline_velo = day_group["player_id"].map(fastball_baseline["velo"])
            day_velo_gap = baseline_velo - day_group["velo"]
            season_baseline_velo = season_group["player_id"].map(fastball_baseline["velo"])
            season_velo_gap = season_baseline_velo - season_group["velo"]
            gap_z_raw = _season_pop_zscore(day_velo_gap, season_velo_gap)
            has_baseline = gap_z_raw.notna()
            gap_z = gap_z_raw.fillna(0.0)
            season_gap_z_raw = _season_pop_zscore(season_velo_gap, season_velo_gap)
            season_has_baseline = season_gap_z_raw.notna()
            season_gap_z = season_gap_z_raw.fillna(0.0)
            season_raw_z = _season_pop_zscore(season_group["velo"], season_group["velo"])
            if season_has_baseline.sum() >= 2 and season_gap_z[season_has_baseline].std() > 0:
                slope, intercept = np.polyfit(
                    season_gap_z[season_has_baseline], season_raw_z[season_has_baseline], 1
                )
            else:
                slope, intercept = 0.0, 0.0
            residual = (velo_z - (intercept + slope * gap_z)).where(has_baseline, 0.0)
            velo_z = CH_VELO_GAP_WEIGHT * gap_z + CH_RAW_VELO_WEIGHT * residual
        elif VELO_SHAPE.get(pt) == "abs":
            velo_z = velo_z.abs()
        elif VELO_SHAPE.get(pt) == "signed_neg":
            velo_z = -velo_z

        ivb_z_raw = _season_pop_zscore(day_group["ivb_in"], season_group["ivb_in"])  # kept pre-shape,
                                              # same reason as compute_pitch_quotients: needed to know
                                              # which rows are below the SEASON average IVB before any
                                              # abs()/sign-flip collapses that information.
        ivb_z = ivb_z_raw
        if pt == "CH":
            baseline_ivb = day_group["player_id"].map(fastball_baseline["ivb_in"])
            day_ivb_gap = baseline_ivb - day_group["ivb_in"]
            season_baseline_ivb = season_group["player_id"].map(fastball_baseline["ivb_in"])
            season_ivb_gap = season_baseline_ivb - season_group["ivb_in"]
            ivb_gap_z = _season_pop_zscore(day_ivb_gap, season_ivb_gap).abs().fillna(0.0)
            ivb_z = CH_IVB_GAP_WEIGHT * ivb_gap_z + CH_IVB_RAW_WEIGHT * ivb_z.abs()
        elif IVB_SHAPE.get(pt) == "abs":
            ivb_z = ivb_z.abs()
        elif IVB_SHAPE.get(pt) == "signed_neg":
            ivb_z = -ivb_z

        horiz_z = _season_pop_zscore(day_group["horizontal_in"], season_group["horizontal_in"])
        if HORIZ_SHAPE.get(pt) == "abs":
            horiz_z = horiz_z.abs()
        elif HORIZ_SHAPE.get(pt) == "signed_neg":
            horiz_z = -horiz_z

        spin_z = _season_pop_zscore(day_group["spin_rpm"], season_group["spin_rpm"])
        horiz_weight = HORIZ_WEIGHT_OVERRIDE.get(pt, HORIZ_WEIGHT)

        # Same per-row conditional IVB weight as compute_pitch_quotients --
        # see IVB_WEIGHT_BELOW_AVG_OVERRIDE's and
        # IVB_WEIGHT_ABOVE_AVG_OVERRIDE's definitions for the reasoning.
        # "Below"/"above" average here means relative to the SEASON
        # population's mean, consistent with every other z-score in this
        # daily function.
        base_ivb_weight = IVB_WEIGHT_OVERRIDE.get(pt, IVB_WEIGHT)
        below_avg_ivb_weight = IVB_WEIGHT_BELOW_AVG_OVERRIDE.get(pt)
        above_avg_ivb_weight = IVB_WEIGHT_ABOVE_AVG_OVERRIDE.get(pt)
        if pt != "CH" and below_avg_ivb_weight is not None:
            ivb_weight = pd.Series(base_ivb_weight, index=day_group.index)
            ivb_weight[ivb_z_raw < 0] = below_avg_ivb_weight
        elif pt != "CH" and above_avg_ivb_weight is not None:
            ivb_weight = pd.Series(base_ivb_weight, index=day_group.index)
            ivb_weight[ivb_z_raw >= 0] = above_avg_ivb_weight
        else:
            ivb_weight = base_ivb_weight

        # Same per-row conditional velocity weight as compute_pitch_quotients
        # -- see VELO_WEIGHT_BELOW_AVG_OVERRIDE's definition for the
        # reasoning. "Below average" here means relative to the SEASON
        # population's mean, consistent with every other z-score in this
        # daily function.
        below_avg_velo_weight = VELO_WEIGHT_BELOW_AVG_OVERRIDE.get(pt)
        if pt != "CH" and below_avg_velo_weight is not None:
            velo_weight = pd.Series(1.0, index=day_group.index)
            velo_weight[velo_z_raw < 0] = below_avg_velo_weight
        else:
            velo_weight = 1.0

        day_group["ceiling"] = (
            velo_weight * velo_z + ivb_weight * ivb_z + horiz_weight * horiz_z + SPIN_WEIGHT * spin_z
        )

        day_group = day_group.merge(
            season_group[["player_id", "effective_usage", "delivery_modifier"]],
            on="player_id", how="left",
        )
        day_group = day_group.dropna(subset=["effective_usage", "delivery_modifier"])
        if day_group.empty:
            continue

        day_group["daily_quotient"] = (
            day_group["ceiling"] * (day_group["effective_usage"] ** USAGE_RATE_EXPONENT) * day_group["delivery_modifier"]
        )

        season_quotient_mean = season_group["quotient"].mean()
        season_quotient_std = season_group["quotient"].std(ddof=0)
        if not season_quotient_std or math.isnan(season_quotient_std):
            day_group["daily_display_score"] = 100.0
        else:
            day_group["daily_display_score"] = (
                100 + 10 * (day_group["daily_quotient"] - season_quotient_mean) / season_quotient_std
            ).round()

        day_group["game_date"] = game_date
        out_frames.append(day_group)

    if not out_frames:
        return pd.DataFrame(columns=[
            "player_id", "pitch_type", "game_date", "velo", "ivb_in", "horizontal_in",
            "spin_rpm", "n_pitches", "daily_quotient", "daily_display_score",
        ])

    result = pd.concat(out_frames, ignore_index=True)
    return result[[
        "player_id", "pitch_type", "game_date", "velo", "ivb_in", "horizontal_in",
        "spin_rpm", "n_pitches", "daily_quotient", "daily_display_score",
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
        # arsenal_size/effective_usage (Option C, 2026-10 usage-rate review)
        # exist on this DataFrame for transparency/debugging and because
        # compute_daily_display_scores still needs effective_usage from it --
        # but the live Supabase `pitch_metrics` table's schema was never
        # migrated to include either column, so uploading them as-is fails
        # the upsert (PGRST204, "column not found in schema cache"). Drop
        # them from the upload payload only; the in-memory `pitch_metrics`
        # passed to compute_daily_display_scores above is untouched.
        pitch_metrics_for_upload = pitch_metrics.drop(
            columns=["arsenal_size", "effective_usage"], errors="ignore"
        )
        pitch_rows = sanitize_records(
            pitch_metrics_for_upload.where(pd.notnull(pitch_metrics_for_upload), None).to_dict(orient="records"),
            "pitch_metrics",
        )

        upsert_in_batches(supabase.table("pitchers"), pitchers_rows)
        upsert_in_batches(supabase.table("pitch_metrics"), pitch_rows, on_conflict="player_id,season,pitch_type")

        # One row per (pitcher, pitch type) for TODAY, into score_snapshots --
        # this is what lets a future refresh compare "now" against "~a week
        # ago" instead of only ever knowing the immediately-previous run's
        # value (which is what prev_quotient/prev_display_score above still
        # capture, and which the home page's movers boxes used to rely on).
        # Late in a season, day-over-day barely moves (one more start is a
        # tiny fraction of a whole season's accumulated pitches), so the home
        # page switched to comparing against this table's ~7-day-old rows
        # instead -- see home.js's fetchMovers. Upserting on
        # (player_id, season, pitch_type, snapshot_date) means re-running the
        # pipeline more than once on the same day just updates today's row
        # rather than piling up duplicates.
        snapshot_date = datetime.now(timezone.utc).date().isoformat()
        snapshot_rows = [
            {
                "player_id": r["player_id"],
                "season": r["season"],
                "pitch_type": r["pitch_type"],
                "display_score": r["display_score"],
                "snapshot_date": snapshot_date,
            }
            for r in pitch_rows
        ]
        upsert_in_batches(
            supabase.table("score_snapshots"), snapshot_rows,
            on_conflict="player_id,season,pitch_type,snapshot_date",
        )

        # Trim old snapshots so this table doesn't grow forever -- only a
        # bit more history than the movers boxes actually compare against is
        # kept (see SNAPSHOT_RETENTION_DAYS above).
        snapshot_cutoff = (datetime.now(timezone.utc).date() - timedelta(days=SNAPSHOT_RETENTION_DAYS)).isoformat()
        supabase.table("score_snapshots").delete().lt("snapshot_date", snapshot_cutoff).execute()

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

        # "Yesterday's Best Pitches" for the home page. Same isolation
        # pattern as probable starters just above, and for the same reason:
        # this hits Savant again for a single extra day's data, entirely
        # separate from the season pull that already succeeded and was
        # logged above, so a problem here (no data yet for yesterday, a
        # Savant hiccup, an off day) only skips this one box.
        try:
            yesterday_str = (datetime.now().date() - timedelta(days=1)).isoformat()
            daily_events = fetch_yesterdays_pitch_events()
            if daily_events.empty:
                print(f"Yesterday's best pitches: no pitch data for {yesterday_str} -- "
                      f"leaving existing daily_pitch_scores rows for this date as-is.")
            else:
                daily_agg = compute_daily_pitch_aggregates(daily_events)
                if daily_agg.empty:
                    print(f"Yesterday's best pitches: no (pitcher, pitch type) cleared "
                          f"DAILY_MIN_PITCHES ({DAILY_MIN_PITCHES}) for {yesterday_str}.")
                else:
                    fastball_baseline = build_fastball_baseline(pitch_metrics_raw)
                    daily_scores = compute_daily_display_scores(
                        daily_agg, pitch_metrics, fastball_baseline, yesterday_str
                    )
                    if daily_scores.empty:
                        print(f"Yesterday's best pitches: {len(daily_agg)} daily aggregate row(s) for "
                              f"{yesterday_str}, but none matched a qualifying season pitch_metrics row "
                              f"to score against.")
                    else:
                        daily_scores["player_id"] = daily_scores["player_id"].map(int)
                        daily_rows = sanitize_records(
                            daily_scores.where(pd.notnull(daily_scores), None).to_dict(orient="records"),
                            "daily_pitch_scores",
                        )
                        supabase.table("daily_pitch_scores").delete().eq("game_date", yesterday_str).execute()
                        upsert_in_batches(
                            supabase.table("daily_pitch_scores"), daily_rows,
                            on_conflict="player_id,pitch_type,game_date",
                        )
                        print(f"Yesterday's best pitches: wrote {len(daily_rows)} (pitcher, pitch type) "
                              f"rows for {yesterday_str}.")

            daily_cutoff = (datetime.now(timezone.utc).date() - timedelta(days=DAILY_SCORE_RETENTION_DAYS)).isoformat()
            supabase.table("daily_pitch_scores").delete().lt("game_date", daily_cutoff).execute()
        except Exception as e:
            print(f"WARNING: yesterday's-best-pitches daily scoring failed, skipping this run's "
                  f"update -- leaderboard refresh above is unaffected. Error: {e}", file=sys.stderr)

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
