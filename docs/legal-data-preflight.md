# Legal / Data Preflight — artifact template

> **What this is.** The Phase 1B gate. Nothing in RELAY can ingest a real
> person's data until this document is completed, signed off, and its
> SHA-256 recorded via `POST /internal/preflight/approve`. The database
> enforces that: a lead whose `lawful_basis` is anything other than
> `synthetic` or `test_consent` is rejected at INSERT for tenants without
> an approved, unrevoked preflight record.
>
> **What this is not.** Legal advice. Every section below must be
> answered by (or reviewed with) whoever owns compliance for the
> deployment — for a client engagement that is usually the client's
> counsel or DPO. RELAY records the decision; it does not make it.

**How to approve, once completed:**

```bash
sha256sum docs/legal-data-preflight.md   # after filling it in
curl -X POST $API/internal/preflight/approve \
  -H "X-Admin-Token: $RELAY_ADMIN_TOKEN" -H "Content-Type: application/json" \
  -d '{"tenant_id": "…", "artifact_sha256": "…", "approved_by": "…",
       "artifact_ref": "docs/legal-data-preflight.md@<git-sha>"}'
```

Re-approve (new hash) whenever this document changes. Revoke via
`/internal/preflight/revoke` to close the gate immediately.

---

## 1. Jurisdiction matrix

*Which countries/regions will prospects be sourced from, stored in, and
contacted in? For each: applicable regime (GDPR, UK GDPR, CASL,
CAN-SPAM, …), whether B2B outreach under legitimate interest is
permitted, and any regional suppression/opt-in rules.*

| Region | Regime(s) | Prospecting allowed under | Special rules | In scope? |
| --- | --- | --- | --- | --- |
| _e.g. Germany_ | _GDPR + UWG_ | _…_ | _double opt-in expectations_ | _yes/no_ |

## 2. Lawful-basis / consent model per region

*Which `lawful_basis` values are permitted per region above, and what
each one requires operationally (e.g. legitimate-interest assessment on
file, consent records, contract reference). Unpermitted combinations
should also be listed explicitly.*

## 3. Controller vs. processor role

*For each tenant/client: who is the data controller, who is the
processor? Where does RELAY's operator sit? Reference the DPA if one
exists.*

## 4. Data-source provenance rules

*What makes a source acceptable? (License terms permit outreach use,
scraping permitted by ToS, purchased list warranties, client-provided
list warranties…) This becomes the discipline behind
`lead_source_register.terms_allow_use`.*

## 5. Privacy notice

*Where is the notice prospects can be pointed to? Does first-contact
messaging need to reference it (GDPR Art. 14 transparency)? Link the
published notice.*

## 6. Retention policy

*Maximum retention per data category and basis. These numbers feed
`leads.retention_until` (REQUIRED for every real-data lead; the purge
worker deletes on expiry).*

| Data | Basis | Retention | Trigger for earlier deletion |
| --- | --- | --- | --- |
| _prospect record, never replied_ | _legitimate interest_ | _e.g. 6 months_ | _DSR, opt-out_ |

## 7. DSR / deletion workflow

*Who receives data-subject requests, on what address, with what SLA?
Confirm the technical path: `POST /dsr/erasure` removes the record from
the datastore and CRM and leaves only a hashed suppression entry so the
person cannot be re-contacted. Who is authorized to trigger it?*

## 8. Allowed-source list

*The concrete, named sources approved for ingestion (provider names,
list origins). Anything not on this list must not be registered with
`terms_allow_use = 'yes'`.*

---

**Sign-off**

| Role | Name | Date | Signature/ref |
| --- | --- | --- | --- |
| Compliance owner | | | |
| Deployment operator | | | |
