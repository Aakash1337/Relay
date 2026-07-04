"""The minimal approval UI (Phase 1A) — one self-contained HTML page.

No build step, no external assets, no framework: the page is served by
the API itself and talks to the same JSON endpoints a human's tools
would. The tenant API key is entered in the page and kept in
sessionStorage only — the page carries no credentials.

This is deliberately a reviewer's tool, not a dashboard: queue on the
left, one draft at a time, the three rubric decisions and their
reason checkboxes. Approval never sends; the page says so on screen.
"""

from __future__ import annotations

from relay.domain.vocab import ReviewReason

_REASON_OPTIONS = "".join(
    f'<label class="reason"><input type="checkbox" value="{r}"> {r.replace("_", " ")}'
    "</label>"
    for r in ReviewReason
)

REVIEW_PAGE = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>RELAY — Review queue</title>
<style>
  :root {{ font-family: system-ui, sans-serif; color: #1a1a1a; }}
  body {{ margin: 0; display: grid; grid-template-columns: 320px 1fr;
         height: 100vh; }}
  aside {{ border-right: 1px solid #ddd; overflow-y: auto; padding: 1rem; }}
  main {{ padding: 1.5rem 2rem; overflow-y: auto; }}
  h1 {{ font-size: 1.1rem; margin: 0 0 1rem; }}
  .banner {{ background: #fff8e1; border: 1px solid #e6c700; padding: .5rem .75rem;
            border-radius: 6px; font-size: .85rem; margin-bottom: 1rem; }}
  .item {{ padding: .6rem; border: 1px solid #ddd; border-radius: 6px;
          margin-bottom: .5rem; cursor: pointer; }}
  .item:hover, .item.active {{ border-color: #3b6cd4; background: #f2f6ff; }}
  .item small {{ color: #666; }}
  #draft-subject {{ font-weight: 600; font-size: 1.05rem; }}
  #draft-body {{ white-space: pre-wrap; background: #fafafa; padding: 1rem;
               border: 1px solid #eee; border-radius: 6px; margin: .75rem 0; }}
  textarea {{ width: 100%; min-height: 10rem; font: inherit; padding: .5rem; }}
  .reasons {{ display: flex; flex-wrap: wrap; gap: .4rem .9rem; margin: .75rem 0; }}
  .reason {{ font-size: .85rem; }}
  button {{ font: inherit; padding: .5rem 1rem; border-radius: 6px;
           border: 1px solid #999; background: #fff; cursor: pointer; }}
  button.primary {{ background: #2e7d32; color: #fff; border-color: #2e7d32; }}
  button.warn {{ background: #b26a00; color: #fff; border-color: #b26a00; }}
  button.danger {{ background: #c62828; color: #fff; border-color: #c62828; }}
  .row {{ display: flex; gap: .6rem; margin-top: 1rem; align-items: center; }}
  #status {{ margin-left: .5rem; font-size: .9rem; color: #444; }}
  .sources {{ font-size: .8rem; color: #555; }}
  input[type=text], input[type=password] {{ font: inherit; padding: .4rem;
     width: 100%; box-sizing: border-box; margin-bottom: .5rem; }}
</style>
</head>
<body>
<aside>
  <h1>RELAY review queue</h1>
  <div class="banner">Approval <b>never sends</b>. Sending happens later,
  behind the eligibility gate and the internal worker.</div>
  <input type="password" id="api-key" placeholder="Tenant API key (rk_…)">
  <input type="text" id="reviewer" placeholder="Your name (reviewer)">
  <button onclick="loadQueue()">Load queue</button>
  <div id="queue" style="margin-top:1rem"></div>
</aside>
<main>
  <div id="empty">Select a draft from the queue.</div>
  <div id="panel" style="display:none">
    <div id="draft-subject"></div>
    <div id="draft-meta" class="sources"></div>
    <div id="draft-body"></div>
    <details><summary class="sources">Personalization sources</summary>
      <pre id="draft-sources" class="sources"></pre></details>
    <h3>Edit (optional — filling this makes the decision “approve with edits”)</h3>
    <textarea id="edit-body" placeholder="Edited body…"></textarea>
    <div class="reasons" id="reasons">{_REASON_OPTIONS}</div>
    <input type="text" id="notes" placeholder="Notes (optional)">
    <div class="row">
      <button class="primary" onclick="decide('approved')">Approve</button>
      <button class="warn" onclick="decide('approved_with_edits')">
        Approve with edits</button>
      <button class="danger" onclick="decide('rejected')">Reject</button>
      <span id="status"></span>
    </div>
  </div>
</main>
<script>
let current = null;
const $ = (id) => document.getElementById(id);
const headers = () => {{
  const key = $("api-key").value.trim();
  sessionStorage.setItem("relay_key", key);
  return {{ "X-API-Key": key, "Content-Type": "application/json" }};
}};
$("api-key").value = sessionStorage.getItem("relay_key") || "";

async function loadQueue() {{
  $("status").textContent = "";
  const res = await fetch("/outreach-drafts/pending", {{ headers: headers() }});
  if (!res.ok) {{ $("queue").textContent = "Error " + res.status; return; }}
  const data = await res.json();
  const q = $("queue");
  q.innerHTML = "";
  if (!data.drafts.length) q.textContent = "Queue is empty.";
  for (const d of data.drafts) {{
    const el = document.createElement("div");
    el.className = "item";
    el.innerHTML = `<div>${{esc(d.subject)}}</div>
      <small>${{esc(d.lead_first_name || "?")}} · ${{esc(d.lead_company || "?")}}
      · v${{d.version}}</small>`;
    el.onclick = () => show(d, el);
    q.appendChild(el);
  }}
}}

function esc(s) {{
  return String(s).replace(/[&<>"']/g,
    (c) => ({{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}})[c]);
}}

function show(d, el) {{
  current = d;
  document.querySelectorAll(".item").forEach(i => i.classList.remove("active"));
  el.classList.add("active");
  $("empty").style.display = "none";
  $("panel").style.display = "block";
  $("draft-subject").textContent = d.subject;
  $("draft-meta").textContent =
    `lead ${{d.lead_id}} · state ${{d.lead_state}} · version ${{d.version}}`;
  $("draft-body").textContent = d.body;
  $("draft-sources").textContent =
    JSON.stringify(d.personalization_sources, null, 2);
  $("edit-body").value = "";
  $("status").textContent = "";
  document.querySelectorAll("#reasons input").forEach(c => c.checked = false);
}}

async function decide(decision) {{
  if (!current) return;
  const reasons = [...document.querySelectorAll("#reasons input:checked")]
    .map(c => c.value);
  const edited = $("edit-body").value.trim();
  if (edited && decision === "approved") decision = "approved_with_edits";
  const body = {{
    reviewer: $("reviewer").value.trim() || "reviewer",
    decision, reasons,
    notes: $("notes").value.trim() || null,
    edited_body: decision === "approved_with_edits" ? (edited || null) : null,
  }};
  const res = await fetch(`/outreach-drafts/${{current.draft_id}}/review`, {{
    method: "POST", headers: headers(), body: JSON.stringify(body),
  }});
  const out = await res.json();
  $("status").textContent = res.ok
    ? `${{decision}} → lead ${{out.lead_state}} (nothing was sent)`
    : `Error: ${{out.detail || res.status}}`;
  if (res.ok) {{ $("panel").style.display = "none"; loadQueue(); }}
}}
</script>
</body>
</html>
"""
