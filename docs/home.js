// uScore+ home page: a top-N grid across every pitch type with enough 
// pitchers to make a real leaderboard, each card linking into the full
// leaderboard for that pitch type.

const { createClient } = supabase;
const client = createClient(window.USCORE_CONFIG.SUPABASE_URL, window.USCORE_CONFIG.SUPABASE_ANON_KEY);

const PITCH_LABELS = {
  FF: "4-Seam Fastball", SI: "Sinker", FC: "Cutter",
  CH: "Changeup", FS: "Splitter", FO: "Forkball",
  CU: "Curveball", KC: "Knuckle Curve", CS: "Slow Curve",
  SL: "Slider", ST: "Sweeper", SV: "Slurve",
};

// Slow curve (CS) and forkball (FO) are excluded here -- too few qualifying
// pitchers league-wide for a top-10 card to mean much (2 total as of this
// writing). They're still fully available on the main leaderboard.
const HOME_PITCH_TYPES = ["FF", "SI", "FC", "CH", "FS", "CU", "KC", "SL", "ST", "SV"];
const TOP_N = 10;

// display_score is already rounded to a whole number server-side (100 =
// league average for that pitch type, like Stuff+) -- this just guards
// against it coming back as e.g. "103.0" and strips the decimal.
function formatScore(v) {
  if (v === null || v === undefined || Number.isNaN(v)) return "-";
  return String(Math.round(Number(v)));
}

function escapeHtml(s) {
  const div = document.createElement("div");
  div.textContent = s;
  return div.innerHTML;
}

function formatStat(v, decimals, suffix) {
  if (v === null || v === undefined || Number.isNaN(v)) return "-";
  return Number(v).toFixed(decimals) + (suffix || "");
}

// The DB stores names straight from Baseball Savant as "Last, First" (and
// "Last Jr., First" etc.) -- this flips that to the "First Last" form
// people actually expect to read. Names with no comma (or a malformed one)
// are returned unchanged rather than mangled.
function formatPitcherName(name) {
  if (!name) return name;
  const idx = name.indexOf(",");
  if (idx === -1) return name;
  const last = name.slice(0, idx).trim();
  const first = name.slice(idx + 1).trim();
  if (!last || !first) return name;
  return `${first} ${last}`;
}

// Every pitcher name on the home page (top-10 cards, movers boxes, watch
// list) links here rather than just being plain text -- same destination
// the top-right search box sends you to.
function playerProfileUrl(playerId, pitcherName) {
  const params = new URLSearchParams({ player_id: playerId, name: pitcherName || "" });
  return `leaderboard.html?${params.toString()}`;
}

async function fetchTopN(pitchType) {
  const { data, error } = await client
    .from("pitch_metrics")
    .select("player_id, display_score, pitchers(pitcher_name)")
    .eq("pitch_type", pitchType)
    .not("display_score", "is", null)
    // Postgres sorts NULLs FIRST by default on a descending order, so
    // without this any row still missing a display_score (e.g. mid-migration,
    // or a future data hiccup for one pitcher) would bubble to the TOP of
    // the list as a blank "-" and bury real, ranked scores below it. The
    // .not() filter above already excludes nulls entirely, but nullsFirst:
    // false is kept as a second, independent guard against the same failure
    // mode -- belt and suspenders.
    .order("display_score", { ascending: false, nullsFirst: false })
    .limit(TOP_N);
  if (error) throw error;
  return data.map((r) => ({
    player_id: r.player_id,
    pitcher_name: formatPitcherName(r.pitchers ? r.pitchers.pitcher_name : "(unknown)"),
    score: r.display_score,
  }));
}

// Local calendar date N days back, as YYYY-MM-DD -- same convention as
// todayDateString() below, just parameterized. Still used by
// fetchYesterdaysBest() below (daysAgoDateString(1)) even though the
// biggest-movers box that used to need the general N-days-back case has
// been removed.
function daysAgoDateString(days) {
  const d = new Date();
  d.setDate(d.getDate() - days);
  const yyyy = d.getFullYear();
  const mm = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  return `${yyyy}-${mm}-${dd}`;
}

// --------------------------------------------------------------------------
// "Yesterday's Best Pitches" -- a true single-day uScore+ (see
// daily_pitch_scores / compute_daily_display_scores in refresh.py), so a
// pitch that looked unusually sharp in one start can show up here even when
// a pitcher's season-cumulative number is too diluted by a big sample to
// move much. Reuses daysAgoDateString(1) -- same local-date convention as
// the movers box above -- rather than a separate "yesterday" helper.
// --------------------------------------------------------------------------

const YESTERDAY_TOP_N = 5;

async function fetchYesterdaysBest() {
  const { data, error } = await client
    .from("daily_pitch_scores")
    // velo/ivb_in/horizontal_in/spin_rpm weren't previously selected here --
    // added so a clicked pitch can show its actual readings and plot them
    // against the season's movement chart (2026-10 "why is this rated X"
    // feature).
    .select("player_id, pitch_type, daily_display_score, n_pitches, velo, ivb_in, horizontal_in, spin_rpm, pitchers(pitcher_name)")
    .eq("game_date", daysAgoDateString(1))
    .not("daily_display_score", "is", null)
    .order("daily_display_score", { ascending: false, nullsFirst: false })
    .limit(YESTERDAY_TOP_N);
  if (error) throw error;
  return data.map((r) => ({
    player_id: r.player_id,
    pitcher_name: formatPitcherName(r.pitchers ? r.pitchers.pitcher_name : "(unknown)"),
    pitch_type: r.pitch_type,
    score: r.daily_display_score,
    n_pitches: r.n_pitches,
    velo: r.velo,
    ivb_in: r.ivb_in,
    horizontal_in: r.horizontal_in,
    spin_rpm: r.spin_rpm,
  }));
}

// --------------------------------------------------------------------------
// "Why is this rated X?" -- click a pitch in the Yesterday's Best Pitches box
// to expand an inline panel with that start's raw characteristics and a
// movement-profile chart, same inline-SVG approach as the full leaderboard's
// detail panel (docs/app.js). The one difference: the highlighted dot here
// is that SPECIFIC START's actual velo/movement (from daily_pitch_scores),
// not a season average -- because that's what the daily score is actually
// z-scored against (see compute_daily_display_scores in pipeline/refresh.py:
// every day's reading is measured against the season population, not the
// day's own tiny cross-section). The season cloud it's plotted on is the
// same per-pitch-type leaderboard data the full leaderboard page charts.
// --------------------------------------------------------------------------

let yesterdayRows = [];
const seasonCloudCache = {};

async function fetchSeasonCloud(pitchType) {
  if (seasonCloudCache[pitchType]) return seasonCloudCache[pitchType];
  const { data, error } = await client
    .from("pitch_metrics")
    .select("player_id, display_score, horizontal_in, ivb_in, pitchers(pitcher_name)")
    .eq("pitch_type", pitchType)
    .not("horizontal_in", "is", null)
    .not("ivb_in", "is", null);
  if (error) throw error;
  const cloud = data.map((r) => ({
    player_id: r.player_id,
    pitcher_name: formatPitcherName(r.pitchers ? r.pitchers.pitcher_name : "(unknown)"),
    display_score: r.display_score,
    horizontal_in: r.horizontal_in,
    ivb_in: r.ivb_in,
  }));
  seasonCloudCache[pitchType] = cloud;
  return cloud;
}

function buildDailyMovementChartSvg(cloud, dayPoint, pitchType) {
  const points = cloud.filter((r) => r.horizontal_in != null && r.ivb_in != null);
  if (!points.length || dayPoint.horizontal_in == null || dayPoint.ivb_in == null) {
    return "<p class=\"chart-caption\">No movement data available for this pitch type yet.</p>";
  }

  const width = 320, height = 220, pad = 30;
  // Axis range includes the day's own point -- an exceptional start can sit
  // outside the season cloud's normal range entirely, and that's worth
  // showing rather than clipping.
  const xs = points.map((p) => p.horizontal_in).concat([dayPoint.horizontal_in]);
  const ys = points.map((p) => p.ivb_in).concat([dayPoint.ivb_in]);
  const xMin = Math.min(...xs), xMax = Math.max(...xs);
  const yMin = Math.min(...ys), yMax = Math.max(...ys);
  const xSpan = xMax - xMin || 1;
  const ySpan = yMax - yMin || 1;

  const sx = (x) => pad + ((x - xMin) / xSpan) * (width - 2 * pad);
  // SVG y grows downward, so flip: higher break drawn higher on screen.
  const sy = (y) => height - pad - ((y - yMin) / ySpan) * (height - 2 * pad);

  const dots = points.map((p) => {
    const title = escapeHtml(`${p.pitcher_name || ""} (season avg): ${formatStat(p.horizontal_in, 1, "\" horiz")}, ${formatStat(p.ivb_in, 1, "\" IVB")}`);
    return `<circle cx="${sx(p.horizontal_in).toFixed(1)}" cy="${sy(p.ivb_in).toFixed(1)}" r="2.6" fill="#b7c9bb" fill-opacity="0.55"><title>${title}</title></circle>`;
  }).join("");

  const dayTitle = escapeHtml(`This start: ${formatStat(dayPoint.horizontal_in, 1, "\" horiz")}, ${formatStat(dayPoint.ivb_in, 1, "\" IVB")}`);
  const dayDot = `<circle cx="${sx(dayPoint.horizontal_in).toFixed(1)}" cy="${sy(dayPoint.ivb_in).toFixed(1)}" r="6" fill="#14532d" stroke="#fcfcfb" stroke-width="1.5"><title>${dayTitle}</title></circle>`;

  return `
    <svg viewBox="0 0 ${width} ${height}" role="img" aria-label="Movement profile scatter plot">
      <line x1="${pad}" y1="${height - pad}" x2="${width - pad}" y2="${height - pad}" stroke="#e3e3e3" />
      <line x1="${pad}" y1="${pad}" x2="${pad}" y2="${height - pad}" stroke="#e3e3e3" />
      <text x="${width / 2}" y="${height - 6}" text-anchor="middle" font-size="9" fill="#6b6b6b">Horizontal break (in, arm-side +)</text>
      <text x="10" y="${height / 2}" text-anchor="middle" font-size="9" fill="#6b6b6b" transform="rotate(-90 10 ${height / 2})">Induced vertical break (in)</text>
      ${dots}
      ${dayDot}
    </svg>
    <p class="chart-caption">Every qualifying ${escapeHtml(PITCH_LABELS[pitchType] || pitchType)} this season (${points.length} pitchers, season averages, light dots); the large dot is this pitcher's actual readings from this one start.</p>
  `;
}

function buildDailyDetailPanel(row, cloud) {
  const chart = buildDailyMovementChartSvg(cloud, row, row.pitch_type);
  const seasonEntry = cloud.find((c) => c.player_id === row.player_id);
  const seasonComparison = seasonEntry && seasonEntry.display_score != null
    ? `<dt title="This pitcher's full-season uScore+ for this pitch type, for comparison">Season uScore+</dt><dd>${formatScore(seasonEntry.display_score)}</dd>`
    : "";
  return `
    <div class="detail-panel">
      <div class="detail-stats">
        <dl>
          <dt>Velocity</dt><dd>${formatStat(row.velo, 1, " mph")}</dd>
          <dt>Induced vertical break</dt><dd>${formatStat(row.ivb_in, 1, " in")}</dd>
          <dt>Horizontal break</dt><dd title="Positive = breaks toward the pitcher's arm side; negative = glove side.">${formatStat(row.horizontal_in, 1, " in")}</dd>
          <dt>Spin rate</dt><dd>${formatStat(row.spin_rpm, 0, " rpm")}</dd>
          <dt>Pitches thrown</dt><dd>${row.n_pitches}</dd>
          <dt title="This start's uScore+, z-scored against the full-season league distribution for this pitch type">uScore+ (this start)</dt><dd>${formatScore(row.score)}</dd>
          ${seasonComparison}
        </dl>
      </div>
      <div class="detail-chart">${chart}</div>
    </div>
  `;
}

async function toggleDailyDetail(idx, btn, container) {
  const isOpen = !container.hidden;
  if (isOpen) {
    container.hidden = true;
    container.innerHTML = "";
    btn.setAttribute("aria-expanded", "false");
    return;
  }
  btn.setAttribute("aria-expanded", "true");
  container.hidden = false;
  container.innerHTML = "<div class=\"detail-loading\">Loading chart...</div>";
  const row = yesterdayRows[idx];
  try {
    const cloud = await fetchSeasonCloud(row.pitch_type);
    // The toggle could have been collapsed again while the fetch was in
    // flight -- don't reopen it out from under someone who already closed it.
    if (container.hidden) return;
    container.innerHTML = buildDailyDetailPanel(row, cloud);
  } catch (err) {
    console.error(err);
    container.innerHTML = "<p class=\"chart-caption\">Couldn't load the chart for this pitch.</p>";
  }
}

function wireYesterdayDetailToggles() {
  document.querySelectorAll(".watch-pitch-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const idx = Number(btn.dataset.idx);
      const container = document.querySelector(`.daily-detail[data-idx="${idx}"]`);
      if (!container) return;
      toggleDailyDetail(idx, btn, container);
    });
  });
}

function renderYesterdayBox(rows) {
  yesterdayRows = rows;
  const items = rows.map((r, i) => `
    <li>
      <div class="watch-main">
        <a class="watch-name" href="${playerProfileUrl(r.player_id, r.pitcher_name)}">${escapeHtml(r.pitcher_name || "")}</a>
        <button type="button" class="watch-pitch-btn" data-idx="${i}" aria-expanded="false" title="Click to see why this pitch was rated ${formatScore(r.score)}">${escapeHtml(PITCH_LABELS[r.pitch_type] || r.pitch_type)} (${formatScore(r.score)})</button>
      </div>
      <div class="watch-meta">${r.n_pitches} thrown yesterday</div>
      <div class="daily-detail" data-idx="${i}" hidden></div>
    </li>
  `).join("");

  return `
    <div class="mover-box watch-box">
      <h3>Yesterday's Best Pitches</h3>
      <ol class="watch-list">${items || "<li class=\"home-empty\">No qualifying pitches from yesterday yet</li>"}</ol>
    </div>
  `;
}

// --------------------------------------------------------------------------
// "Pitchers to watch today" -- today's probable starters, ranked by each
// starter's single best pitch (highest quotient in their arsenal). Two
// queries rather than an embedded join: probable_starters -> pitchers for
// names, then a separate pitch_metrics lookup for the starters' full
// arsenals, reduced client-side to each pitcher's best pitch. Simpler than
// a single embedded query and keeps each query's shape obvious.
// --------------------------------------------------------------------------

const WATCH_TOP_N = 5;

function todayDateString() {
  // Local calendar date (not UTC) -- "today" should match the games the
  // person actually sees on their own clock, not a date that could roll
  // over hours early/late depending on server-vs-viewer timezone.
  const d = new Date();
  const yyyy = d.getFullYear();
  const mm = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  return `${yyyy}-${mm}-${dd}`;
}

function formatGameTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

async function fetchWatchList() {
  const { data: starters, error: startersError } = await client
    .from("probable_starters")
    .select("player_id, team, opponent, game_time, pitchers(pitcher_name)")
    .eq("game_date", todayDateString());
  if (startersError) throw startersError;
  if (!starters || !starters.length) return [];

  const ids = starters.map((s) => s.player_id);
  const { data: pitches, error: pitchesError } = await client
    .from("pitch_metrics")
    .select("player_id, pitch_type, display_score")
    .in("player_id", ids);
  if (pitchesError) throw pitchesError;

  const bestByPlayer = {};
  (pitches || []).forEach((p) => {
    const cur = bestByPlayer[p.player_id];
    if (p.display_score != null && (!cur || p.display_score > cur.display_score)) bestByPlayer[p.player_id] = p;
  });

  const combined = starters
    .map((s) => {
      const best = bestByPlayer[s.player_id];
      if (!best) return null; // no qualifying pitch data to rank this starter by
      return {
        player_id: s.player_id,
        pitcher_name: formatPitcherName(s.pitchers ? s.pitchers.pitcher_name : "(unknown)"),
        team: s.team,
        opponent: s.opponent,
        game_time: s.game_time,
        pitch_type: best.pitch_type,
        score: best.display_score,
      };
    })
    .filter(Boolean);

  return combined.sort((a, b) => b.score - a.score).slice(0, WATCH_TOP_N);
}

function renderWatchBox(rows) {
  const items = rows.map((r) => {
    const matchup = r.team && r.opponent ? `${escapeHtml(r.team)} vs ${escapeHtml(r.opponent)}` : "";
    const time = formatGameTime(r.game_time);
    const meta = [matchup, time].filter(Boolean).join(" &middot; ");
    return `
      <li>
        <div class="watch-main">
          <a class="watch-name" href="${playerProfileUrl(r.player_id, r.pitcher_name)}">${escapeHtml(r.pitcher_name || "")}</a>
          <span class="watch-pitch">${escapeHtml(PITCH_LABELS[r.pitch_type] || r.pitch_type)} (${formatScore(r.score)})</span>
        </div>
        ${meta ? `<div class="watch-meta">${meta}</div>` : ""}
      </li>
    `;
  }).join("");

  return `
    <div class="mover-box watch-box">
      <h3>Pitchers to Watch Today</h3>
      <ol class="watch-list">${items || "<li class=\"home-empty\">No probable starters found for today yet</li>"}</ol>
    </div>
  `;
}

// --------------------------------------------------------------------------
// Top-right pitcher search -- a lightweight autocomplete that hands off to
// the full leaderboard page's player view rather than duplicating it here.
// --------------------------------------------------------------------------

let searchDebounceTimer = null;

async function searchPitchers(query) {
  const { data, error } = await client
    .from("pitchers")
    .select("player_id, pitcher_name")
    .ilike("pitcher_name", `%${query}%`)
    .order("pitcher_name")
    .limit(8);
  if (error) throw error;
  // The DB stores "Last, First" (straight from Baseball Savant); the search
  // itself and the alphabetical .order() above stay on that raw form (so
  // it's still sorted by last name), but everything shown to a person uses
  // the "First Last" display form.
  return data.map((p) => ({ ...p, pitcher_name: formatPitcherName(p.pitcher_name) }));
}

function goToPlayer(playerId, pitcherName) {
  const params = new URLSearchParams({ player_id: playerId, name: pitcherName });
  window.location.href = `leaderboard.html?${params.toString()}`;
}

function renderSearchSuggestions(list) {
  const box = document.getElementById("home-suggestions");
  if (!list.length) {
    box.hidden = true;
    box.innerHTML = "";
    return;
  }
  box.innerHTML = list.map((p) =>
    `<button type="button" data-player-id="${p.player_id}">${escapeHtml(p.pitcher_name)}</button>`
  ).join("");
  box.hidden = false;
  Array.from(box.querySelectorAll("button")).forEach((btn, i) => {
    btn.addEventListener("click", () => goToPlayer(list[i].player_id, list[i].pitcher_name));
  });
}

function initHomeSearch() {
  const input = document.getElementById("home-search");
  if (!input) return;
  input.addEventListener("input", (e) => {
    const q = e.target.value.trim();
    clearTimeout(searchDebounceTimer);
    if (q.length < 2) {
      renderSearchSuggestions([]);
      return;
    }
    searchDebounceTimer = setTimeout(async () => {
      try {
        const results = await searchPitchers(q);
        renderSearchSuggestions(results);
      } catch (err) {
        console.error(err);
      }
    }, 250);
  });
  document.addEventListener("click", (e) => {
    const box = document.getElementById("home-suggestions");
    if (!box.contains(e.target) && e.target !== input) box.hidden = true;
  });
}

async function fetchLastUpdated() {
  const { data, error } = await client
    .from("refresh_log")
    .select("finished_at, status")
    .eq("status", "success")
    .order("finished_at", { ascending: false })
    .limit(1);
  if (error || !data || !data.length) return null;
  return data[0].finished_at;
}

function renderCard(pitchType, rows) {
  // Each row's name links straight to that pitcher's own profile, so the
  // card as a whole can no longer be a single <a> wrapping everything
  // (nested links aren't valid HTML/reliable across browsers) -- it's a
  // plain container now, with its own title link and footer link to the
  // full pitch-type leaderboard alongside the per-row profile links.
  const items = rows.map((r, i) => `
    <li>
      <span class="home-rank">${i + 1}</span>
      <a class="home-name" href="${playerProfileUrl(r.player_id, r.pitcher_name)}">${escapeHtml(r.pitcher_name || "")}</a>
      <span class="home-value">${formatScore(r.score)}</span>
    </li>
  `).join("");
  const leaderboardHref = `leaderboard.html?pitch=${encodeURIComponent(pitchType)}`;

  return `
    <div class="pitch-card">
      <a class="pitch-card-title" href="${leaderboardHref}"><h3>${escapeHtml(PITCH_LABELS[pitchType] || pitchType)}</h3></a>
      <ol class="home-list">${items || "<li class=\"home-empty\">No qualifying pitchers yet</li>"}</ol>
      <a class="pitch-card-link" href="${leaderboardHref}">See full leaderboard &rarr;</a>
    </div>
  `;
}

async function loadHome() {
  const grid = document.getElementById("pitch-grid");
  const status = document.getElementById("status");
  const moversRow = document.getElementById("movers-row");
  status.textContent = "Loading...";
  try {
    const [pitchResults, yesterdaysBest, watchList] = await Promise.all([
      Promise.all(HOME_PITCH_TYPES.map((pt) => fetchTopN(pt).then((rows) => ({ pt, rows })))),
      fetchYesterdaysBest().catch((err) => {
        console.error(err);
        return [];
      }),
      fetchWatchList().catch((err) => {
        console.error(err);
        return [];
      }),
    ]);
    grid.innerHTML = pitchResults.map(({ pt, rows }) => renderCard(pt, rows)).join("");
    // Two centered boxes, watch-list first -- the biggest-movers box has
    // been removed for now (see git history to restore it).
    moversRow.innerHTML = renderWatchBox(watchList) + renderYesterdayBox(yesterdaysBest);
    wireYesterdayDetailToggles();
    status.textContent = "";
  } catch (err) {
    console.error(err);
    status.textContent =
      "Couldn't load data. Check that config.js has your Supabase project's URL and anon key, and that the daily refresh has run at least once.";
  }
}

(function init() {
  const el = document.getElementById("updated-at");
  fetchLastUpdated().then((iso) => {
    if (!iso) { el.textContent = ""; return; }
    el.textContent = "Data last refreshed " + new Date(iso).toLocaleString();
  });
  initHomeSearch();
  loadHome();
})();
