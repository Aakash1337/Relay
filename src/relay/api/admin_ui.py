"""The minimal admin console (Phase 4) — one self-contained HTML page.

Same posture as /review and /ops: no build step, no external assets.
The admin token is entered in the page and kept in sessionStorage only.
It drives the existing admin endpoints — onboarding, key rotation,
sender-identity attestation, global suppression — so a prototype
operator needs no curl. Everything the page can do is exactly what the
API can do: it adds no capability, only convenience.
"""

from __future__ import annotations

ADMIN_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>RELAY — Admin</title>
<style>
  :root { font-family: system-ui, sans-serif; color: #1a1a1a; }
  body { max-width: 46rem; margin: 2rem auto; padding: 0 1rem; }
  h1 { font-size: 1.4rem; } h2 { font-size: 1.05rem; margin-top: 2rem; }
  fieldset { border: 1px solid #ccc; border-radius: 6px; margin: 1rem 0;
             padding: 1rem; }
  label { display: block; margin: .45rem 0 .1rem; font-size: .85rem; }
  input, select { width: 100%; padding: .4rem; box-sizing: border-box; }
  button { margin-top: .8rem; padding: .5rem 1rem; cursor: pointer; }
  pre { background: #f4f4f4; padding: .8rem; border-radius: 6px;
        overflow-x: auto; font-size: .8rem; white-space: pre-wrap; }
  .muted { color: #666; font-size: .85rem; }
</style>
</head>
<body>
<h1>RELAY admin console</h1>
<p class="muted">Drives the admin API only — onboarding never sends
anything; sending stays behind the eligibility gates and the worker.</p>

<fieldset>
  <legend>Admin token</legend>
  <label for="token">X-Admin-Token (kept in sessionStorage only)</label>
  <input id="token" type="password" autocomplete="off">
</fieldset>

<h2>Onboard a tenant</h2>
<fieldset>
  <label>Tenant name</label><input id="ob-name">
  <label>Source name</label><input id="ob-source" value="seed-contacts">
  <label>Source type</label>
  <select id="ob-source-type">
    <option>seed</option><option>synthetic</option><option>api</option>
    <option>uploaded_list</option><option>licensed_provider</option>
    <option>crm_import</option><option>public_registry</option>
    <option>website</option>
  </select>
  <label>Campaign name</label><input id="ob-campaign" value="first-campaign">
  <label>Sequence length (1 = single-shot)</label>
  <input id="ob-seq-len" type="number" value="1" min="1" max="10">
  <label>Sequence delay hours</label>
  <input id="ob-seq-delay" type="number" value="72" min="0">
  <label>Daily send cap (blank = global)</label>
  <input id="ob-daily-cap" type="number" min="0">
  <label>Monthly spend cap, units (blank = none)</label>
  <input id="ob-spend-cap" type="number" min="0">
  <label>Sender from-address (blank = global identity)</label>
  <input id="ob-from">
  <button onclick="onboard()">Onboard</button>
</fieldset>

<h2>Tenant operations</h2>
<fieldset>
  <label>Tenant id</label><input id="op-tenant">
  <button onclick="rotateKey()">Rotate API key</button>
  <button onclick="attestSender()">Attest sender identity verified</button>
</fieldset>

<h2>Global suppression (platform-wide do-not-contact)</h2>
<fieldset>
  <label>Tenant id (record-keeping)</label><input id="gs-tenant">
  <label>Email address</label><input id="gs-email">
  <button onclick="globalSuppress()">Suppress globally</button>
</fieldset>

<h2>Result</h2>
<pre id="out">—</pre>

<script>
const tokenInput = document.getElementById("token");
tokenInput.value = sessionStorage.getItem("relay_admin_token") || "";
tokenInput.addEventListener("change", () =>
  sessionStorage.setItem("relay_admin_token", tokenInput.value));

function val(id) { return document.getElementById(id).value.trim(); }
function num(id) { const v = val(id); return v === "" ? null : Number(v); }
function show(x) {
  document.getElementById("out").textContent =
    typeof x === "string" ? x : JSON.stringify(x, null, 2);
}
async function call(method, path, body) {
  const resp = await fetch(path, {
    method,
    headers: {
      "X-Admin-Token": tokenInput.value,
      ...(body ? {"Content-Type": "application/json"} : {}),
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  let payload;
  try { payload = await resp.json(); } catch { payload = await resp.text(); }
  show({status: resp.status, response: payload});
  return payload;
}
function onboard() {
  call("POST", "/internal/tenants/onboard", {
    name: val("ob-name"),
    source: {
      name: val("ob-source"),
      source_type: val("ob-source-type"),
      terms_allow_use: "yes",
    },
    campaign: {
      name: val("ob-campaign"),
      sequence_length: num("ob-seq-len") ?? 1,
      sequence_delay_hours: num("ob-seq-delay") ?? 72,
    },
    daily_send_cap: num("ob-daily-cap"),
    monthly_spend_cap_units: num("ob-spend-cap"),
    sender_from_address: val("ob-from") || null,
  });
}
function rotateKey() {
  call("POST", `/internal/tenants/${val("op-tenant")}/rotate-key`);
}
function attestSender() {
  call("POST", `/internal/tenants/${val("op-tenant")}/attest-sender-identity`);
}
function globalSuppress() {
  call("POST", "/internal/suppression/global", {
    tenant_id: val("gs-tenant"),
    email: val("gs-email"),
  });
}
</script>
</body>
</html>"""
