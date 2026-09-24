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

function formatValue(v) {
  if (v === null || v === undefined || Number.isNaN(v)) return "-";
  return Number(v).toFixed(3);
}

function escapeHtml(s) {
  const div = document.createElement("div");
  div.textContent = s;
  return div.innerHTML;
}

async function fetchTopN(pitchType) {
  const { data, error } = await client
    .from("pitch_metrics")
    .select("quotient, pitchers(pitcher_name)")
    .eq("pitch_type", pitchType)
    .order("quotient", { ascending: false })
    .limit(TOP_N);
  if (error) throw error;
  return data.map((r) => ({
    pitcher_name: r.pitchers ? r.pitchers.pitcher_name : "(unknown)",
    quotient: r.quotient,
  }));
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
      <span class="home-value">${formatValue(r.quotient)}</span>
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
  status.textContent = "Loading...";
  try {
    const results = await Promise.all(
      HOME_PITCH_TYPES.map((pt) => fetchTopN(pt).then((rows) => ({ pt, rows })))
    );
    grid.innerHTML = results.map(({ pt, rows }) => renderCard(pt, rows)).join("");
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
  loadHome();
})();
