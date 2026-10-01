-- uScore database schema
-- Run this once in the Supabase SQL Editor (Supabase dashboard -> SQL Editor -> New query)
-- to create the tables the refresh pipeline writes to and the website reads from.

-- One row per pitcher: identity, delivery metrics, and the season-level scores.
create table if not exists pitchers (
    player_id       bigint primary key,       -- Statcast/MLBAM player id
    pitcher_name    text not null,             -- "Last, First"
    season          int  not null,

    -- delivery raw inputs
    extension_ft            numeric,
    arm_angle_deg            numeric,
    release_height_ft        numeric,
    horizontal_release_ft    numeric,

    -- delivery quotients
    delivery_quotient        numeric,          -- undamped (ABS z-scores)
    adj_delivery_quotient    numeric,          -- dampened (SQRT(ABS z)) -- the "funky delivery" fix

    -- arsenal diversity
    n_pitches_thrown  int,
    arsenal_entropy   numeric,
    diversity_multiplier numeric,

    -- season-level scores
    uscore            numeric,
    adjusted_uscore    numeric,

    updated_at        timestamptz not null default now(),
    unique (player_id, season)
);

-- One row per (pitcher, pitch type): every pitch-level metric, so the site can
-- filter/sort/chart by pitch type without touching the pitchers table.
create table if not exists pitch_metrics (
    id               bigint generated always as identity primary key,
    player_id        bigint not null references pitchers(player_id) on delete cascade,
    season           int not null,
    pitch_type       text not null,             -- 'FF','SI','FC','CH','FS','FO','CU','KC','CS','SL','ST','SV'

    velo             numeric,
    ivb_in           numeric,
    horizontal_in    numeric,
    spin_rpm         numeric,
    active_spin_pct  numeric,                   -- null when Savant has no reading
    usage_rate       numeric,                   -- 0-1

    active_spin_quotient numeric,               -- 0 when active_spin_pct is null
    delivery_modifier numeric,                  -- multiplier from how unusual this pitcher's
                                                 -- release point is league-wide (1.0 = average delivery)
    quotient          numeric,                   -- this pitch type's contribution to uScore

    prev_quotient     numeric,                   -- this pitch type's quotient as of the PREVIOUS
                                                   -- refresh, captured just before it's overwritten --
                                                   -- powers the home page's day-over-day movers boxes.
                                                   -- Null until a pitcher/pitch-type has been through
                                                   -- at least two refreshes.
    prev_captured_at  timestamptz,                -- when prev_quotient was captured (i.e. the previous
                                                   -- refresh's finish time)

    updated_at        timestamptz not null default now(),
    unique (player_id, season, pitch_type)
);

-- One row per refresh run, so you (or the site) can see when data last updated
-- and whether a run failed.
create table if not exists refresh_log (
    id            bigint generated always as identity primary key,
    started_at    timestamptz not null default now(),
    finished_at   timestamptz,
    status        text not null default 'running',   -- 'running' | 'success' | 'failed'
    pitchers_written int,
    error_message text
);

-- Today's probable starting pitchers, refreshed daily from MLB's own Stats
-- API (a separate source from Baseball Savant) -- powers the home page's
-- "pitchers to watch today" box. player_id references pitchers(player_id)
-- because the refresh script only ever writes starters who already have
-- qualifying uScore data this season (see fetch_probable_starters /
-- run() in refresh.py) -- a starter with no tracked pitch data has nothing
-- to rank by anyway, so there's no case where this FK should reject a row.
create table if not exists probable_starters (
    id            bigint generated always as identity primary key,
    game_date     date not null,
    player_id     bigint not null references pitchers(player_id) on delete cascade,
    team          text,
    opponent      text,
    game_time     timestamptz,
    fetched_at    timestamptz not null default now(),
    unique (game_date, player_id)
);

-- Day-over-day score snapshots, used by the home page's "Biggest Movers" box
-- to compare today's display_score against a ~week-old one. (Predates this
-- schema file being kept fully in sync -- documented here now so a fresh
-- database setup doesn't miss it.)
create table if not exists score_snapshots (
    id              bigint generated always as identity primary key,
    player_id       bigint not null references pitchers(player_id) on delete cascade,
    pitch_type      text not null,
    display_score   numeric,
    snapshot_date   date not null,
    unique (player_id, pitch_type, snapshot_date)
);

-- One row per (pitcher, pitch type, calendar day) that pitcher threw at
-- least DAILY_MIN_PITCHES (see refresh.py) pitches of that type -- a true
-- single-day uScore+, not a season-to-date average, so it can surface a
-- real-time trend (e.g. a changeup looking sharper in one start) that a
-- season-cumulative number is too diluted to show. velo/ivb_in/
-- horizontal_in/spin_rpm are genuine single-day averages; usage_rate,
-- delivery_modifier, and (for changeups) the fastball-gap baseline are NOT
-- recomputed daily -- the pipeline reuses that pitcher's current SEASON
-- values for all three (day-specific usage is mostly game-plan noise,
-- release characteristics don't shift day to day, and a day-only fastball
-- baseline risks "no fastball thrown that day"), and active spin% is
-- excluded entirely (no daily reading exists from Savant; confirmed via a
-- real A/B comparison that excluding it changes the season formula's
-- output negligibly). Every z-score behind daily_quotient/
-- daily_display_score is taken against the SEASON population's
-- distribution for that pitch type, so "100" keeps meaning the same thing
-- here as it does in pitch_metrics.display_score. Powers the home page's
-- "Yesterday's Best Pitches" box.
create table if not exists daily_pitch_scores (
    id              bigint generated always as identity primary key,
    player_id       bigint not null references pitchers(player_id) on delete cascade,
    pitch_type      text not null,
    game_date       date not null,

    velo            numeric,
    ivb_in          numeric,
    horizontal_in   numeric,
    spin_rpm        numeric,
    n_pitches       int not null,              -- how many of this type were thrown this day

    daily_quotient       numeric,              -- same math as pitch_metrics.quotient, just for
                                                 -- this one day's readings scored against the
                                                 -- season's distribution
    daily_display_score  numeric,              -- same 100-average scale as pitch_metrics.
                                                 -- display_score, rescaled against the SEASON's
                                                 -- quotient distribution for that pitch type

    created_at      timestamptz not null default now(),
    unique (player_id, pitch_type, game_date)
);

create index if not exists idx_pitch_metrics_player on pitch_metrics(player_id, season);
create index if not exists idx_pitch_metrics_type on pitch_metrics(pitch_type, season);
create index if not exists idx_pitchers_season on pitchers(season);
create index if not exists idx_probable_starters_date on probable_starters(game_date);
create index if not exists idx_score_snapshots_date on score_snapshots(snapshot_date);
create index if not exists idx_daily_pitch_scores_date on daily_pitch_scores(game_date);

-- Row Level Security: the public website uses Supabase's "anon" key, which
-- must only ever be able to READ. All writes go through the refresh script,
-- which uses the separate, secret "service_role" key that bypasses RLS.
alter table pitchers enable row level security;
alter table pitch_metrics enable row level security;
alter table refresh_log enable row level security;
alter table probable_starters enable row level security;
alter table score_snapshots enable row level security;
alter table daily_pitch_scores enable row level security;

create policy "public read pitchers" on pitchers
    for select using (true);
create policy "public read pitch_metrics" on pitch_metrics
    for select using (true);
create policy "public read refresh_log" on refresh_log
    for select using (true);
create policy "public read probable_starters" on probable_starters
    for select using (true);
create policy "public read score_snapshots" on score_snapshots
    for select using (true);
create policy "public read daily_pitch_scores" on daily_pitch_scores
    for select using (true);

-- No insert/update/delete policies are created for the anon role, so the
-- public site can only ever read -- writes require the service_role key.
