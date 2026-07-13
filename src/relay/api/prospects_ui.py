"""The shortlist picker — one self-contained HTML page, like /review.

The working set is every scored-qualified lead waiting in
shortlist_pending, best fit first. A person checks the prospects worth
pursuing and submits once: checked leads move to drafting, unchecked
ones can be skipped in bulk. Nothing here drafts or sends anything —
pursue only queues the lead for the next pipeline run.
"""

from __future__ import annotations

PROSPECTS_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>RELAY — Prospect shortlist</title>
<style>
  :root { font-family: system-ui, sans-serif; color: #1a1a1a; }
  body { margin: 0; padding: 1.5rem 2rem; max-width: 1100px; }
  h1 { font-size: 1.1rem; margin: 0 0 1rem; }
  .banner { background: #fff8e1; border: 1px solid #e6c700; padding: .5rem .75rem;
            border-radius: 6px; font-size: .85rem; margin-bottom: 1rem; }
  .controls { display: flex; gap: .6rem; margin-bottom: 1rem; flex-wrap: wrap; }
  input[type=text], input[type=password] { font: inherit; padding: .4rem;
     min-width: 220px; }
  button { font: inherit; padding: .5rem 1rem; border-radius: 6px;
           border: 1px solid #999; background: #fff; cursor: pointer; }
  button.primary { background: #2e7d32; color: #fff; border-color: #2e7d32; }
  button.danger { background: #c62828; color: #fff; border-color: #c62828; }
  table { border-collapse: collapse; width: 100%; font-size: .9rem; }
  th, td { text-align: left; padding: .45rem .6rem; border-bottom: 1px solid #eee;
           vertical-align: top; }
  th { background: #fafafa; position: sticky; top: 0; }
  tr:hover { background: #f2f6ff; }
  .fit { font-variant-numeric: tabular-nums; font-weight: 600; }
  .bio { color: #555; font-size: .82rem; max-width: 420px; }
  #status { margin-left: .5rem; font-size: .9rem; color: #444; }
</style>
</head>
<body>
<h1>RELAY prospect shortlist</h1>
<div class="banner"><b>Pursue</b> only queues a lead for drafting — every
email still needs its own human approval later, and nothing on this page
can send. <b>Skip</b> is terminal: the lead is never drafted or emailed.</div>
<div class="controls">
  <input type="password" id="api-key" placeholder="Tenant API key (rk_…)">
  <input type="text" id="actor" placeholder="Your name">
  <button onclick="loadProspects()">Load prospects</button>
  <button class="primary" onclick="submitDecisions('pursue')">
    Pursue checked</button>
  <button class="danger" onclick="submitDecisions('skip')">Skip checked</button>
  <span id="status"></span>
</div>
<table id="table" style="display:none">
  <thead><tr>
    <th><input type="checkbox" id="all"
         onclick="document.querySelectorAll('.pick')
           .forEach(c => c.checked = this.checked)"></th>
    <th>Fit</th><th>Name</th><th>Title</th><th>Company</th>
    <th>Region</th><th>Research notes</th>
  </tr></thead>
  <tbody id="rows"></tbody>
</table>
<div id="empty" style="display:none">No prospects waiting for a decision.</div>
<script>
const $ = (id) => document.getElementById(id);
const headers = () => {
  const key = $("api-key").value.trim();
  sessionStorage.setItem("relay_key", key);
  return { "X-API-Key": key, "Content-Type": "application/json" };
};
$("api-key").value = sessionStorage.getItem("relay_key") || "";

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g,
    (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[c]);
}

async function loadProspects() {
  $("status").textContent = "";
  const res = await fetch("/prospects/pending?limit=500", { headers: headers() });
  if (!res.ok) { $("status").textContent = "Error " + res.status; return; }
  const data = await res.json();
  const rows = $("rows");
  rows.innerHTML = "";
  $("table").style.display = data.count ? "table" : "none";
  $("empty").style.display = data.count ? "none" : "block";
  for (const p of data.prospects) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td><input type="checkbox" class="pick" value="${p.lead_id}"></td>
      <td class="fit">${p.fit_score == null ? "—" : p.fit_score.toFixed(3)}</td>
      <td>${esc(p.first_name)} ${esc(p.last_name)}</td>
      <td>${esc(p.title)}</td>
      <td>${esc(p.company_name)} <small>${esc(p.company_domain)}</small></td>
      <td>${esc(p.region_assumption)}</td>
      <td class="bio">${esc(p.bio)}</td>`;
    rows.appendChild(tr);
  }
}

async function submitDecisions(decision) {
  const ids = [...document.querySelectorAll(".pick:checked")].map(c => c.value);
  if (!ids.length) { $("status").textContent = "Nothing checked."; return; }
  const body = {
    actor: $("actor").value.trim() || "operator",
    items: ids.map(id => ({ lead_id: id, decision })),
  };
  const res = await fetch("/prospects/batch-shortlist", {
    method: "POST", headers: headers(), body: JSON.stringify(body),
  });
  const out = await res.json();
  $("status").textContent = res.ok
    ? `pursued ${out.pursued} · skipped ${out.skipped} · failed ${out.failed}`
    : `Error: ${out.detail || res.status}`;
  if (res.ok) loadProspects();
}
</script>
</body>
</html>
"""
