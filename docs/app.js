// uScore leaderboard front end.
//
// Two views, both driven straight off Supabase (no build step, no pipeline
// changes needed for anything in this file):
//   - Leaderboard view: every qualifying pitcher for one Statcast pitch
//     type, ranked by uScore quotient. Click a row to expand a detail panel
//     with that pitch's raw characteristics and a movement-profile chart
//     (horizontal break vs. induced vertical break) plotted against every
//     other pitcher's dot for that same pitch type.
//   - Player view: search a pitcher's name to see every pitch type THEY
//     throw, each with its own quotient and rank (e.g. "#12 of 187"),
//     computed live against the full table -- not just whatever happened
//     to be loaded on screen already.
//
// The composite "overall pitcher" uScore is still computed and stored by
// the pipeline every day; it's just not surfaced in this UI for now.

const { createClient } = supabase;
const client = createClient(window.USCORE_CONFIG.SUPABASE_URL, window.USCORE_CONFIG.SUPABASE_ANON_KEY);

const PITCH_LABELS = {
  FF: "4-Seam Fastball", SI: "Sinker", FC: "Cutter",
  CH: "Changeup", FS: "Splitter", FO: "Forkball",
  CU: "Curveball", KC: "Knuckle Curve", CS: "Slow Curve",
  SL: "Slider", ST: "Sweeper", SV: "Slurve",
};

const LEADERBOARD_LIMIT = 250;

const state = {
  view: "leaderboard", // "leaderboard" | "player"
  pitchType: "FF",
  rows: [],            // current leaderboard rows (extended with raw metrics)
  sortField: "value",   // "value" | "usage_rate"
  expandedKey: null,    // player_id (leaderboard) or pitch_type (player view) currently expanded
  playerId: null,
  playerName: "",
  playerRows: [],       // player view: one row per pitch type they throw
  suggestions: [],
  chartCache: {},       // pitch_type -> cloud rows, cached per session to avoid refetching
  totalCount: null,     // how many pitchers qualify for the current pitch type, total (not just what's loaded)
  showingAll: false,    // false = capped at LEADERBOARD_LIMIT (the default view); true = every qualifying pitcher
};

let searchDebounceTimer = null;

// --------------------------------------------------------------------------
// Data fetching
// --------------------------------------------------------------------------

async function fetchPitchTypeLeaderboard(pitchType, { limit = LEADERBOARD_LIMIT } = {}) {
  let query = client
    .from("pitch_metrics")
    .select("player_id, display_score, usage_rate, velo, ivb_in, horizontal_in, spin_rpm, active_spin_pct, delivery_modifier, pitchers(pitcher_name)")
    .eq("pitch_type", pitchType)
    .not("display_score", "is", null)
    // Postgres sorts NULLs FIRST by default on a descending order, so
    // without this any row still missing a display_score would bubble to
    // the TOP of the leaderboard as a blank "-" and bury real, ranked
    // scores below it. The .not() filter above already excludes nulls
    // entirely; nullsFirst: false is a second, independent guard against
    // the same failure mode.
    .order("display_score", { ascending: false, nullsFirst: false });
  // limit: null means "every qualifying pitcher" (the "show all" expansion)
  // -- omit .limit() entirely rather than passing a huge number, so this
  // stays correct even if a pitch type someday has more qualifiers than
  // whatever number we might have guessed as "big enough".
  if (limit != null) query = query.limit(limit);
  const { data, error } = await query;
  if (error) throw error;
  return data.map((r) => ({
    player_id: r.player_id,
    pitcher_name: r.pitchers ? r.pitchers.pitcher_name : "(unknown)",
    value: r.display_score,
    usage_rate: r.usage_rate,
    velo: r.velo,
    ivb_in: r.ivb_in,
    horizontal_in: r.horizontal_in,
    spin_rpm: r.spin_rpm,
    active_spin_pct: r.active_spin_pct,
    delivery_modifier: r.delivery_modifier,
  }));
}

// A cheap count-only query (no rows fetched) so the "showing top 250 of N"
// footer can report the true total even before anyone clicks "show all".
async function fetchQualifyingCount(pitchType) {
  const { count, error } = await client
    .from("pitch_metrics")
    .select("player_id", { count: "exact", head: true })
    .eq("pitch_type", pitchType)
    .not("display_score", "is", null);
  if (error) throw error;
  return count || 0;
}

async function fetchCloud(pitchType) {
  if (state.chartCache[pitchType]) return state.chartCache[pitchType];
  const cloud = await fetchPitchTypeLeaderboard(pitchType);
  state.chartCache[pitchType] = cloud;
  return cloud;
}

async function searchPitchers(query) {
  const { data, error } = await client
    .from("pitchers")
    .select("player_id, pitcher_name")
    .ilike("pitcher_name", `%${query}%`)
    .order("pitcher_name")
    .limit(8);
  if (error) throw error;
  return data;
}

async function getRank(pitchType, quotient) {
  const [higher, total] = await Promise.all([
    client.from("pitch_metrics").select("player_id", { count: "exact", head: true })
      .eq("pitch_type", pitchType).gt("quotient", quotient),
    client.from("pitch_metrics").select("player_id", { count: "exact", head: true })
      .eq("pitch_type", pitchType),
  ]);
  if (higher.error) throw higher.error;
  if (total.error) throw total.error;
  return { rank: (higher.count || 0) + 1, total: total.count || 0 };
}

async function fetchArsenal(playerId) {
  const { data, error } = await client
    .from("pitch_metrics")
    .select("pitch_type, velo, ivb_in, horizontal_in, spin_rpm, active_spin_pct, delivery_modifier, usage_rate, quotient, display_score")
    .eq("player_id", playerId)
    .order("quotient", { ascending: false });
  if (error) throw error;

  const ranked = await Promise.all(data.map(async (row) => {
    const { rank, total } = await getRank(row.pitch_type, row.quotient);
    return { ...row, rank, total };
  }));
  return ranked;
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

// --------------------------------------------------------------------------
// Small formatters
// --------------------------------------------------------------------------

// display_score is already rounded to a whole number server-side (100 =
// league average for that pitch type, like Stuff+) -- this just guards
// against it coming back as e.g. "103.0" and strips the decimal.
function formatScore(v) {
  if (v === null || v === undefined || Number.isNaN(v)) return "-";
  return String(Math.round(Number(v)));
}

function formatPercent(v) {
  if (v === null || v === undefined || Number.isNaN(v)) return "-";
  return (Number(v) * 100).toFixed(1) + "%";
}

function formatStat(v, decimals, suffix) {
  if (v === null || v === undefined || Number.isNaN(v)) return "-";
  return Number(v).toFixed(decimals) + (suffix || "");
}

function escapeHtml(s) {
  const div = document.createElement("div");
  div.textContent = s;
  return div.innerHTML;
}

function mean(values) {
  const nums = values.filter((v) => v !== null && v !== undefined && !Number.isNaN(v));
  if (!nums.length) return null;
  return nums.reduce((sum, v) => sum + v, 0) / nums.length;
}

// Quick-scan color tier for a quotient value, relative to the rest of the
// currently loaded set (so it adapts per pitch type rather than using one
// fixed global scale).
function tierClass(value, allValues) {
  if (value === null || value === undefined || Number.isNaN(value) || !allValues.length) return "";
  const sorted = [...allValues].sort((a, b) => b - a);
  const rankIdx = sorted.findIndex((v) => v <= value);
  const percentile = 1 - rankIdx / sorted.length;
  if (percentile >= 0.9) return "tier-elite";
  if (percentile >= 0.7) return "tier-strong";
  if (percentile >= 0.4) return "tier-good";
  return "";
}

// --------------------------------------------------------------------------
// Movement chart -- plain inline SVG scatter, no charting library needed.
// Horizontal break (in) on the x-axis, induced vertical break (in) on the y.
// --------------------------------------------------------------------------

function buildMovementChartSvg(cloud, highlightPlayerId, pitchType) {
  const points = cloud.filter((r) => r.horizontal_in != null && r.ivb_in != null);
  if (!points.length) {
    return "<p class=\"chart-caption\">No movement data available for this pitch type yet.</p>";
  }

  const width = 320, height = 220, pad = 30;
  const xs = points.map((p) => p.horizontal_in);
  const ys = points.map((p) => p.ivb_in);
  const xMin = Math.min(...xs), xMax = Math.max(...xs);
  const yMin = Math.min(...ys), yMax = Math.max(...ys);
  const xSpan = xMax - xMin || 1;
  const ySpan = yMax - yMin || 1;

  const sx = (x) => pad + ((x - xMin) / xSpan) * (width - 2 * pad);
  // SVG y grows downward, so flip: higher break drawn higher on screen.
  const sy = (y) => height - pad - ((y - yMin) / ySpan) * (height - 2 * pad);

  const dots = points.map((p) => {
    const isHighlight = p.player_id === highlightPlayerId;
    const r = isHighlight ? 5.5 : 2.6;
    const fill = isHighlight ? "#14532d" : "#b7c9bb";
    const opacity = isHighlight ? 1 : 0.55;
    const title = escapeHtml(`${p.pitcher_name || ""}: ${formatStat(p.horizontal_in, 1, "\" horiz")}, ${formatStat(p.ivb_in, 1, "\" IVB")}`);
    return `<circle cx="${sx(p.horizontal_in).toFixed(1)}" cy="${sy(p.ivb_in).toFixed(1)}" r="${r}" fill="${fill}" fill-opacity="${opacity}"><title>${title}</title></circle>`;
  }).join("");

  return `
    <svg viewBox="0 0 ${width} ${height}" role="img" aria-label="Movement profile scatter plot">
      <line x1="${pad}" y1="${height - pad}" x2="${width - pad}" y2="${height - pad}" stroke="#e3e3e3" />
      <line x1="${pad}" y1="${pad}" x2="${pad}" y2="${height - pad}" stroke="#e3e3e3" />
      <text x="${width / 2}" y="${height - 6}" text-anchor="middle" font-size="9" fill="#6b6b6b">Horizontal break (in, arm-side +)</text>
      <text x="10" y="${height / 2}" text-anchor="middle" font-size="9" fill="#6b6b6b" transform="rotate(-90 10 ${height / 2})">Induced vertical break (in)</text>
      ${dots}
    </svg>
    <p class="chart-caption">Every qualifying ${escapeHtml(PITCH_LABELS[pitchType] || pitchType)} this season (${points.length} pitchers); the highlighted dot is this pitcher's.</p>
  `;
}

function buildDetailPanel(row, cloud, pitchType) {
  const chart = buildMovementChartSvg(cloud, row.player_id, pitchType);
  return `
    <div class="detail-panel">
      <div class="detail-stats">
        <dl>
          <dt>Velocity</dt><dd>${formatStat(row.velo, 1, " mph")}</dd>
          <dt>Induced vertical break</dt><dd>${formatStat(row.ivb_in, 1, " in")}</dd>
          <dt>Horizontal break</dt><dd title="Positive = breaks toward the pitcher's arm side; negative = glove side. Normalized so lefties and righties are directly comparable.">${formatStat(row.horizontal_in, 1, " in")}</dd>
          <dt>Spin rate</dt><dd>${formatStat(row.spin_rpm, 0, " rpm")}</dd>
          <dt>Active spin</dt><dd>${row.active_spin_pct == null ? "n/a" : formatStat(row.active_spin_pct, 1, "%")}</dd>
          <dt>Usage</dt><dd>${formatPercent(row.usage_rate)}</dd>
          <dt>Delivery modifier</dt><dd title="How unusual this pitcher's release point is league-wide -- 1.00 is a perfectly average delivery">${formatStat(row.delivery_modifier, 2, "&times;")}</dd>
          <dt title="100 = league average for this pitch type; higher = more unique">uScore</dt><dd>${formatScore(row.value != null ? row.value : row.display_score)}</dd>
        </dl>
      </div>
      <div class="detail-chart">${chart}</div>
    </div>
  `;
}

// --------------------------------------------------------------------------
// Leaderboard view
// --------------------------------------------------------------------------

function sortedRows() {
  const field = state.sortField;
  return [...state.rows].sort((a, b) => {
    const av = a[field] == null ? -Infinity : a[field];
    const bv = b[field] == null ? -Infinity : b[field];
    return bv - av;
  });
}

function renderLeaderboard() {
  const head = document.getElementById("leaderboard-head");
  const body = document.getElementById("leaderboard-body");
  const rows = sortedRows();
  const allValues = state.rows.map((r) => r.value);

  const arrow = (field) => (state.sortField === field ? " <span class=\"sort-arrow\">&#9660;</span>" : "");
  head.innerHTML = `
    <tr>
      <th>#</th>
      <th>Pitcher</th>
      <th class="sortable" id="sort-quotient" title="100 = league average for this pitch type. Reflects how far this pitch's velocity, movement, spin, and active spin deviate from average, weighted by how often it's thrown -- higher means more unique.">uScore${arrow("value")}</th>
      <th class="sortable" id="sort-usage" title="Share of this pitcher's tracked pitches this season that were this pitch type">Usage${arrow("usage_rate")}</th>
    </tr>
  `;
  document.getElementById("sort-quotient").addEventListener("click", () => { state.sortField = "value"; renderLeaderboard(); });
  document.getElementById("sort-usage").addEventListener("click", () => { state.sortField = "usage_rate"; renderLeaderboard(); });

  body.innerHTML = "";
  rows.forEach((r, i) => {
    const tr = document.createElement("tr");
    tr.className = "data-row" + (state.expandedKey === r.player_id ? " expanded" : "");
    tr.innerHTML = `
      <td class="rank">${i + 1}</td>
      <td>${escapeHtml(r.pitcher_name || "")}</td>
      <td class="value ${tierClass(r.value, allValues)}">${formatScore(r.value)}</td>
      <td class="value">${formatPercent(r.usage_rate)}</td>
    `;
    tr.addEventListener("click", () => toggleLeaderboardDetail(r.player_id));
    body.appendChild(tr);

    if (state.expandedKey === r.player_id) {
      const detailTr = document.createElement("tr");
      detailTr.className = "detail-row";
      const td = document.createElement("td");
      td.colSpan = 4;
      td.innerHTML = buildDetailPanel(r, state.rows, state.pitchType);
      detailTr.appendChild(td);
      body.appendChild(detailTr);
    }
  });

  renderLeaderboardFooter();
}

// Below the table: by default the leaderboard caps at LEADERBOARD_LIMIT so
// the page stays fast and the table stays a manageable length -- but capping
// at, say, 250 out of a pitch type with 700+ qualifying pitchers means only
// the top slice (all comfortably above the 100 average) is ever visible,
// which reads as if uScore skews high when it's really just showing the
// best of the best. This footer makes the cap visible and gives a way past
// it, so the full, honest distribution -- including everything below 100 --
// is always one click away.
function renderLeaderboardFooter() {
  const el = document.getElementById("leaderboard-footer");
  if (!el) return;
  if (state.totalCount == null || state.totalCount <= state.rows.length) {
    el.innerHTML = "";
    return;
  }
  if (state.showingAll) {
    el.innerHTML = `
      <span class="leaderboard-footer-note">Showing all ${state.totalCount} qualifying pitchers</span>
      <button type="button" id="show-top-btn">Show top ${LEADERBOARD_LIMIT} only</button>
    `;
    document.getElementById("show-top-btn").addEventListener("click", () => {
      state.showingAll = false;
      loadLeaderboard();
    });
  } else {
    el.innerHTML = `
      <span class="leaderboard-footer-note">Showing top ${state.rows.length} of ${state.totalCount} qualifying pitchers</span>
      <button type="button" id="show-all-btn">Show all ${state.totalCount} &darr;</button>
    `;
    document.getElementById("show-all-btn").addEventListener("click", expandLeaderboard);
  }
}

async function expandLeaderboard() {
  document.getElementById("status").textContent = "Loading full leaderboard...";
  try {
    const rows = await fetchPitchTypeLeaderboard(state.pitchType, { limit: null });
    state.rows = rows;
    state.chartCache[state.pitchType] = rows;
    state.showingAll = true;
    state.expandedKey = null;
    renderLeaderboard();
    renderLeagueStrip();
    document.getElementById("status").textContent = "";
  } catch (err) {
    console.error(err);
    document.getElementById("status").textContent = "Couldn't load the full leaderboard.";
  }
}

function toggleLeaderboardDetail(playerId) {
  state.expandedKey = state.expandedKey === playerId ? null : playerId;
  renderLeaderboard();
}

// A quick reference strip so a raw number like "88.7 mph" has something to
// be compared against without clicking into any individual row -- built
// from whatever's already loaded for the current pitch type, no extra query.
function renderLeagueStrip() {
  const el = document.getElementById("league-strip");
  if (!state.rows.length) {
    el.hidden = true;
    return;
  }
  const avgVelo = mean(state.rows.map((r) => r.velo));
  const avgIvb = mean(state.rows.map((r) => r.ivb_in));
  const avgHoriz = mean(state.rows.map((r) => r.horizontal_in));
  const avgSpin = mean(state.rows.map((r) => r.spin_rpm));

  el.innerHTML = `
    <span class="league-strip-title">League avg ${escapeHtml(PITCH_LABELS[state.pitchType] || state.pitchType)}:</span>
    <span class="stat"><span class="stat-label">Velo</span><span class="stat-value">${formatStat(avgVelo, 1, " mph")}</span></span>
    <span class="stat"><span class="stat-label">IVB</span><span class="stat-value">${formatStat(avgIvb, 1, " in")}</span></span>
    <span class="stat"><span class="stat-label" title="Positive = arm-side, negative = glove-side">Horiz</span><span class="stat-value">${formatStat(avgHoriz, 1, " in")}</span></span>
    <span class="stat"><span class="stat-label">Spin</span><span class="stat-value">${formatStat(avgSpin, 0, " rpm")}</span></span>
    <span class="stat"><span class="stat-label">Pitchers</span><span class="stat-value">${state.rows.length}</span></span>
  `;
  el.hidden = false;
}

async function loadLeaderboard() {
  document.getElementById("player-header").hidden = true;
  document.getElementById("status").textContent = "Loading...";
  state.showingAll = false;
  try {
    const [rows, totalCount] = await Promise.all([
      fetchPitchTypeLeaderboard(state.pitchType, { limit: LEADERBOARD_LIMIT }),
      fetchQualifyingCount(state.pitchType),
    ]);
    state.rows = rows;
    state.chartCache[state.pitchType] = rows;
    state.totalCount = totalCount;
    state.expandedKey = null;
    renderLeaderboard();
    renderLeagueStrip();
    document.getElementById("status").textContent = "";
  } catch (err) {
    console.error(err);
    document.getElementById("status").textContent =
      "Couldn't load data. Check that config.js has your Supabase project's URL and anon key, and that the daily refresh has run at least once.";
  }
}

// --------------------------------------------------------------------------
// Player (arsenal) view
// --------------------------------------------------------------------------

function renderPlayerView() {
  document.getElementById("player-header").hidden = false;
  document.getElementById("player-name").textContent = state.playerName;
  document.getElementById("league-strip").hidden = true;

  const head = document.getElementById("leaderboard-head");
  const body = document.getElementById("leaderboard-body");
  head.innerHTML = `
    <tr>
      <th>Pitch</th>
      <th title="100 = league average for this pitch type; higher = more unique">uScore</th>
      <th>Rank</th>
      <th title="Share of this pitcher's tracked pitches this season that were this pitch type">Usage</th>
    </tr>
  `;

  body.innerHTML = "";
  state.playerRows.forEach((r) => {
    const tr = document.createElement("tr");
    tr.className = "data-row" + (state.expandedKey === r.pitch_type ? " expanded" : "");
    tr.innerHTML = `
      <td>${PITCH_LABELS[r.pitch_type] || r.pitch_type}</td>
      <td class="value">${formatScore(r.display_score)}</td>
      <td class="rank">#${r.rank} of ${r.total}</td>
      <td class="value">${formatPercent(r.usage_rate)}</td>
    `;
    tr.addEventListener("click", () => togglePlayerDetail(r.pitch_type));
    body.appendChild(tr);

    if (state.expandedKey === r.pitch_type) {
      const detailTr = document.createElement("tr");
      detailTr.className = "detail-row";
      const td = document.createElement("td");
      td.colSpan = 4;
      const cloud = state.chartCache[r.pitch_type];
      if (cloud) {
        td.innerHTML = buildDetailPanel(
          { ...r, player_id: state.playerId, value: r.display_score },
          cloud,
          r.pitch_type
        );
      } else {
        td.innerHTML = "<div class=\"detail-loading\">Loading chart...</div>";
      }
      detailTr.appendChild(td);
      body.appendChild(detailTr);
    }
  });
}

async function togglePlayerDetail(pitchType) {
  if (state.expandedKey === pitchType) {
    state.expandedKey = null;
    renderPlayerView();
    return;
  }
  state.expandedKey = pitchType;
  renderPlayerView();
  if (!state.chartCache[pitchType]) {
    try {
      await fetchCloud(pitchType);
    } catch (err) {
      console.error(err);
    }
    if (state.expandedKey === pitchType) renderPlayerView();
  }
}

async function selectPlayer(playerId, pitcherName) {
  document.getElementById("suggestions").hidden = true;
  document.getElementById("status").textContent = "Loading...";
  try {
    state.playerRows = await fetchArsenal(playerId);
    state.playerId = playerId;
    state.playerName = pitcherName;
    state.view = "player";
    state.expandedKey = null;
    renderPlayerView();
    document.getElementById("status").textContent = "";
  } catch (err) {
    console.error(err);
    document.getElementById("status").textContent = "Couldn't load that pitcher's data.";
  }
}

function backToLeaderboard() {
  state.view = "leaderboard";
  state.expandedKey = null;
  state.playerId = null;
  state.playerName = "";
  state.playerRows = [];
  document.getElementById("search").value = "";
  document.getElementById("player-header").hidden = true;
  renderLeaderboard();
  renderLeagueStrip();
}

// --------------------------------------------------------------------------
// Search / autocomplete
// --------------------------------------------------------------------------

function renderSuggestions(list) {
  const box = document.getElementById("suggestions");
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
    btn.addEventListener("click", () => selectPlayer(list[i].player_id, list[i].pitcher_name));
  });
}

document.getElementById("search").addEventListener("input", (e) => {
  const q = e.target.value.trim();
  clearTimeout(searchDebounceTimer);
  if (q.length < 2) {
    renderSuggestions([]);
    return;
  }
  searchDebounceTimer = setTimeout(async () => {
    try {
      const results = await searchPitchers(q);
      renderSuggestions(results);
    } catch (err) {
      console.error(err);
    }
  }, 250);
});

document.addEventListener("click", (e) => {
  const box = document.getElementById("suggestions");
  const search = document.getElementById("search");
  if (!box.contains(e.target) && e.target !== search) box.hidden = true;
});

document.getElementById("back-to-leaderboard").addEventListener("click", backToLeaderboard);

document.getElementById("pitch-select").addEventListener("change", (e) => {
  state.pitchType = e.target.value;
  if (state.view === "player") backToLeaderboard();
  loadLeaderboard();
});

// --------------------------------------------------------------------------
// Init
// --------------------------------------------------------------------------

(async function init() {
  const el = document.getElementById("updated-at");
  fetchLastUpdated().then((iso) => {
    if (!iso) { el.textContent = ""; return; }
    el.textContent = "Data last refreshed " + new Date(iso).toLocaleString();
  });

  // Support linking straight into a specific pitch type's leaderboard (e.g.
  // from the home page's top-pitchers cards): leaderboard.html?pitch=SL
  const params = new URLSearchParams(window.location.search);
  const requestedPitch = params.get("pitch");
  const pitchSelect = document.getElementById("pitch-select");
  if (requestedPitch && PITCH_LABELS[requestedPitch]) {
    state.pitchType = requestedPitch;
    pitchSelect.value = requestedPitch;
  }

  // Support linking straight into a specific pitcher's arsenal view (e.g.
  // from the home page's top-right search box):
  // leaderboard.html?player_id=123&name=Jane+Doe
  const requestedPlayerId = params.get("player_id");
  const requestedName = params.get("name");
  if (requestedPlayerId) {
    selectPlayer(Number(requestedPlayerId), requestedName || "");
  } else {
    loadLeaderboard();
  }
})();
