// uScore leaderboard front end.
//
// Deliberately simple today (one table, sortable, filterable) but built on
// top of a normalized query layer (fetchLeaderboard) so a richer dashboard
// later -- charts, pitcher detail pages, per-pitch-type breakdowns -- can
// be added without touching the database or the pipeline, just this file
// and index.html.

const { createClient } = supabase;
const client = createClient(window.USCORE_CONFIG.SUPABASE_URL, window.USCORE_CONFIG.SUPABASE_ANON_KEY);

const METRIC_LABELS = {
  adjusted_uscore: "Adjusted uScore",
  uscore: "uScore",
  delivery_quotient: "Delivery Quotient",
  adj_delivery_quotient: "Adj. Delivery Quotient",
  quotient: "Pitch Quotient",
  usage_rate: "Usage",
};

const state = {
  metric: "adjusted_uscore",
  pitchType: "",
  search: "",
  rows: [],
};

async function fetchOverallLeaderboard(metric) {
  const { data, error } = await client
    .from("pitchers")
    .select("player_id, pitcher_name, uscore, adjusted_uscore, delivery_quotient, adj_delivery_quotient")
    .order(metric, { ascending: false })
    .limit(250);
  if (error) throw error;
  return data.map((r) => ({ ...r, value: r[metric] }));
}

async function fetchPitchTypeLeaderboard(pitchType) {
  const { data, error } = await client
    .from("pitch_metrics")
    .select("player_id, quotient, usage_rate, pitchers(pitcher_name)")
    .eq("pitch_type", pitchType)
    .order("quotient", { ascending: false })
    .limit(250);
  if (error) throw error;
  return data.map((r) => ({
    player_id: r.player_id,
    pitcher_name: r.pitchers ? r.pitchers.pitcher_name : "(unknown)",
    value: r.quotient,
    usage_rate: r.usage_rate,
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

function renderStatus(text) {
  document.getElementById("status").textContent = text;
}

function renderUpdatedAt(iso) {
  const el = document.getElementById("updated-at");
  if (!iso) {
    el.textContent = "";
    return;
  }
  const d = new Date(iso);
  el.textContent = "Data last refreshed " + d.toLocaleString();
}

function renderTable(rows, metricLabel) {
  const head = document.getElementById("leaderboard-head");
  const body = document.getElementById("leaderboard-body");

  head.innerHTML = `
    <tr>
      <th>#</th>
      <th>Pitcher</th>
      <th>${metricLabel}</th>
    </tr>
  `;

  body.innerHTML = "";
  rows.forEach((r, i) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td class="rank">${i + 1}</td>
      <td>${escapeHtml(r.pitcher_name || "")}</td>
      <td class="value">${formatValue(r.value)}</td>
    `;
    body.appendChild(tr);
  });
}

function formatValue(v) {
  if (v === null || v === undefined || Number.isNaN(v)) return "-";
  return Number(v).toFixed(3);
}

function escapeHtml(s) {
  const div = document.createElement("div");
  div.textContent = s;
  return div.innerHTML;
}

async function loadAndRender() {
  renderStatus("Loading...");
  try {
    const rows = state.pitchType
      ? await fetchPitchTypeLeaderboard(state.pitchType)
      : await fetchOverallLeaderboard(state.metric);

    state.rows = rows;
    applyFilterAndRender();
    renderStatus("");
  } catch (err) {
    console.error(err);
    renderStatus("Couldn't load data. Check that config.js has your Supabase project's URL and anon key, and that the daily refresh has run at least once.");
  }
}

function applyFilterAndRender() {
  const q = state.search.trim().toLowerCase();
  const filtered = q
    ? state.rows.filter((r) => (r.pitcher_name || "").toLowerCase().includes(q))
    : state.rows;

  const label = state.pitchType
    ? "Pitch Quotient"
    : METRIC_LABELS[state.metric];

  renderTable(filtered, label);
}

document.getElementById("metric-select").addEventListener("change", (e) => {
  state.metric = e.target.value;
  loadAndRender();
});

document.getElementById("pitch-select").addEventListener("change", (e) => {
  state.pitchType = e.target.value;
  document.getElementById("metric-select").disabled = !!state.pitchType;
  loadAndRender();
});

document.getElementById("search").addEventListener("input", (e) => {
  state.search = e.target.value;
  applyFilterAndRender();
});

(async function init() {
  renderUpdatedAt(await fetchLastUpdated());
  loadAndRender();
})();
