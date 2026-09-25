// uScore home page: a top-N grid across every pitch type with enough
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

async function fetchTopN(pitchType) {
  const { data, error } = await client
    .from("pitch_metrics")
    .select("display_score, pitchers(pitcher_name)")
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
    pitcher_name: r.pitchers ? r.pitchers.pitcher_name : "(unknown)",
    score: r.display_score,
  }));
}

function formatDelta(v) {
  if (v === null || v === undefined || Number.isNaN(v)) return "-";
  const rounded = Math.round(Number(v));
  const sign = rounded > 0 ? "+" : "";
  return sign + String(rounded);
}

// Every pitch-type row that has a prev_display_score (i.e. has been through
// at least two refreshes), with the day-over-day delta computed client-side
// in display_score's 100-average units -- simplest way to sort/slice into
// gainers vs. decliners without needing a generated column or a second
// round-trip per pitch type.
async function fetchMovers() {
  const { data, error } = await client
    .from("pitch_metrics")
    .select("pitch_type, display_score, prev_display_score, pitchers(pitcher_name)")
    .not("prev_display_score", "is", null)
    .limit(5000);
  if (error) throw error;
  const withDelta = data
    .map((r) => ({
      pitcher_name: r.pitchers ? r.pitchers.pitcher_name : "(unknown)",
      pitch_type: r.pitch_type,
      delta: r.display_score - r.prev_display_score,
    }))
    .filter((r) => !Number.isNaN(r.delta));
  const gainers = [...withDelta].sort((a, b) => b.delta - a.delta).slice(0, 5);
  const decliners = [...withDelta].sort((a, b) => a.delta - b.delta).slice(0, 5);
  return { gainers, decliners };
}

function renderMoverBox(title, rows, deltaClass) {
  const items = rows.map((r) => `
    <li>
      <span class="mover-name">${escapeHtml(r.pitcher_name || "")}</span>
      <span class="mover-pitch">${escapeHtml(PITCH_LABELS[r.pitch_type] || r.pitch_type)}</span>
      <span class="mover-delta ${deltaClass}">${formatDelta(r.delta)}</span>
    </li>
  `).join("");

  return `
    <div class="mover-box">
      <h3>${escapeHtml(title)}</h3>
      <ol class="mover-list">${items || "<li class=\"home-empty\">No data yet &mdash; check back after the next refresh</li>"}</ol>
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
        pitcher_name: s.pitchers ? s.pitchers.pitcher_name : "(unknown)",
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
          <span class="watch-name">${escapeHtml(r.pitcher_name || "")}</span>
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
  return data;
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
  const items = rows.map((r, i) => `
    <li>
      <span class="home-rank">${i + 1}</span>
      <span class="home-name">${escapeHtml(r.pitcher_name || "")}</span>
      <span class="home-value">${formatScore(r.score)}</span>
    </li>
  `).join("");

  return `
    <a class="pitch-card" href="leaderboard.html?pitch=${encodeURIComponent(pitchType)}">
      <h3>${escapeHtml(PITCH_LABELS[pitchType] || pitchType)}</h3>
      <ol class="home-list">${items || "<li class=\"home-empty\">No qualifying pitchers yet</li>"}</ol>
      <span class="pitch-card-link">See full leaderboard &rarr;</span>
    </a>
  `;
}

async function loadHome() {
  const grid = document.getElementById("pitch-grid");
  const status = document.getElementById("status");
  const moversRow = document.getElementById("movers-row");
  status.textContent = "Loading...";
  try {
    const [pitchResults, movers, watchList] = await Promise.all([
      Promise.all(HOME_PITCH_TYPES.map((pt) => fetchTopN(pt).then((rows) => ({ pt, rows })))),
      fetchMovers().catch((err) => {
        console.error(err);
        return { gainers: [], decliners: [] };
      }),
      fetchWatchList().catch((err) => {
        console.error(err);
        return [];
      }),
    ]);
    grid.innerHTML = pitchResults.map(({ pt, rows }) => renderCard(pt, rows)).join("");
    moversRow.innerHTML =
      renderMoverBox("Yesterday's Biggest Gainers", movers.gainers, "mover-up") +
      renderMoverBox("Yesterday's Biggest Decliners", movers.decliners, "mover-down") +
      renderWatchBox(watchList);
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
