"""The minimal ops dashboard — one self-contained HTML page, like /review.

Reads /metrics and /alerts with the tenant key entered in the page.
No build step, no external assets; deliberately a glanceable status
board, not a BI tool (Grafana over /metrics/prometheus is the Phase 3
answer for real dashboards).
"""

from __future__ import annotations

OPS_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>RELAY — Ops</title>
<style>
  :root { font-family: system-ui, sans-serif; color: #1a1a1a; }
  body { margin: 0; padding: 1.5rem 2rem; max-width: 1100px; }
  h1 { font-size: 1.15rem; }
  .bar { display: flex; gap: .6rem; margin-bottom: 1.2rem; }
  input { font: inherit; padding: .45rem; flex: 1; }
  button { font: inherit; padding: .45rem 1rem; cursor: pointer;
           border: 1px solid #999; border-radius: 6px; background: #fff; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill,
          minmax(240px, 1fr)); gap: 1rem; }
  .card { border: 1px solid #ddd; border-radius: 8px; padding: .9rem 1.1rem; }
  .card h2 { font-size: .8rem; text-transform: uppercase; color: #666;
             margin: 0 0 .5rem; letter-spacing: .04em; }
  .big { font-size: 1.6rem; font-weight: 650; }
  table { width: 100%; border-collapse: collapse; font-size: .85rem; }
  td { padding: .15rem 0; } td:last-child { text-align: right;
       font-variant-numeric: tabular-nums; }
  .alert { border-left: 4px solid #c62828; background: #fdecea;
           padding: .6rem .9rem; border-radius: 4px; margin-bottom: .5rem; }
  .alert.warning { border-color: #b26a00; background: #fff8e1; }
  .ok { color: #2e7d32; }
  #updated { color: #777; font-size: .8rem; margin-left: auto;
             align-self: center; }
</style>
</head>
<body>
<h1>RELAY ops</h1>
<div class="bar">
  <input type="password" id="key" placeholder="Tenant API key (rk_…)">
  <button onclick="refresh()">Refresh</button>
  <label style="align-self:center;font-size:.85rem">
    <input type="checkbox" id="auto" onchange="autoRefresh()"> auto (30s)
  </label>
  <span id="updated"></span>
</div>
<div id="alerts"></div>
<div class="grid" id="cards"></div>
<script>
const $ = (id) => document.getElementById(id);
$("key").value = sessionStorage.getItem("relay_key") || "";
let timer = null;

function esc(s) {
  return String(s).replace(/[&<>"']/g,
    (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[c]);
}

function table(obj) {
  const rows = Object.entries(obj).sort()
    .map(([k, v]) => `<tr><td>${esc(k)}</td><td>${v}</td></tr>`).join("");
  return rows ? `<table>${rows}</table>` : "<em>none</em>";
}

async function refresh() {
  const headers = { "X-API-Key": $("key").value.trim() };
  sessionStorage.setItem("relay_key", $("key").value.trim());
  const [mRes, aRes] = await Promise.all([
    fetch("/metrics", { headers }), fetch("/alerts", { headers }),
  ]);
  if (!mRes.ok) { $("cards").innerHTML = `Error ${mRes.status}`; return; }
  const m = await mRes.json();
  const a = aRes.ok ? await aRes.json() : { alerts: [] };

  $("alerts").innerHTML = a.alerts.length
    ? a.alerts.map(x => `<div class="alert ${x.severity}">
        <b>${esc(x.rule)}</b> — ${esc(x.detail)}</div>`).join("")
    : `<div class="ok">No active alerts.</div>`;

  const errRate = m.run_error_rate == null ? "–"
    : (m.run_error_rate * 100).toFixed(0) + "%";
  const replyRate = m.reply_rate == null ? "–"
    : (m.reply_rate * 100).toFixed(0) + "%";

  $("cards").innerHTML = `
    <div class="card"><h2>Cost (24h)</h2>
      <div class="big">${m.cost_units_window.toFixed(1)}</div> units</div>
    <div class="card"><h2>Run error rate (24h)</h2>
      <div class="big">${errRate}</div></div>
    <div class="card"><h2>Sent / replies (24h)</h2>
      <div class="big">${m.sent_window} / ${m.replies_window}</div>
      reply rate ${replyRate}</div>
    <div class="card"><h2>Suppression entries</h2>
      <div class="big">${m.suppression_entries}</div></div>
    <div class="card"><h2>Leads by state</h2>${table(m.lead_states)}</div>
    <div class="card"><h2>Runs (24h)</h2>${table(m.runs_window)}</div>
    <div class="card"><h2>Send jobs</h2>${table(m.send_jobs)}</div>`;
  $("updated").textContent =
    "updated " + new Date().toLocaleTimeString();
}

function autoRefresh() {
  if (timer) { clearInterval(timer); timer = null; }
  if ($("auto").checked) timer = setInterval(refresh, 30000);
}
</script>
</body>
</html>
"""
