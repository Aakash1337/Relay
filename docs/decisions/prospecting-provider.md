# Decision: prospecting data provider (finding clients)

**Status:** OPEN — no provider selected; nothing built. This record
frames the evaluation so the decision is a day's work when the trigger
fires, not a research project.

**Trigger to decide** (from prototype-status item 8): sustained volume
beyond hand-research (~hundreds of prospects/month) or the first
external tenant needing self-serve discovery.

## The question

RELAY starts at "you already have a list." When list-building itself
needs automating, which licensed data provider supplies the
companies-people-emails pipeline, under what terms, at what cost?

## Ground rules (settled now, whoever wins)

1. **Buy, never scrape.** The lead source register (§7) requires every
   source to declare terms and proof of lawful use; scraped LinkedIn
   data cannot honestly satisfy that, and LinkedIn automation violates
   its ToS outright. Only providers whose license permits outreach use
   qualify.
2. **Verification is non-negotiable.** Unverified lists bounce at
   10–30%; bounces are exactly what the reputation gates and the SES
   account health punish. Whatever the source, addresses pass a
   deliverability check before real-mode use (provider-side or a
   dedicated verifier as a second adapter).
3. **Discovery feeds the shortlist, never the send queue.** Scale on
   the finding side must not become unreviewed sending volume: imported
   candidates enter as `dry_run=true`, walk scoring, and wait in
   `/prospects` for a human. The existing gates stay binding.
4. **One adapter seam** in `ingest/` mirroring `senders/`: a
   `ProspectSource` interface (`search(criteria) → candidates`), one
   provider implementation, config-selected, offline stub for tests.
   Per-campaign ICP criteria live on the campaign.

## Candidates to evaluate

| Provider | Shape | Why / why not | Check before deciding |
| --- | --- | --- | --- |
| **Apollo.io** | company + people search, emails included, REST API, free tier + ~$50/mo tiers | the default starter: one vendor covers search→email; good API docs | API terms on export/outreach use; email accuracy on a 50-lead sample; credit costs at target volume |
| **Hunter.io / Snov.io** | email finding + verification, simple APIs | best as the verification leg, or paired with a company source | coverage on your ICP's domains |
| **Clay** | multi-provider waterfall + per-row LLM research | conceptually closest to RELAY's enrichment ambitions; study even if not bought | pricing scales with rows × providers; API vs UI-only workflows |
| **ZoomInfo** | enterprise gold standard | data quality ceiling; five-figure annual pricing | not at prototype scale |
| **People Data Labs / Proxycurl-class APIs** | raw data APIs, pay-per-record | cheapest programmatic path if you build more glue | license terms vary sharply on outreach use — read them |

## What the evaluation must answer

1. License: does the provider's agreement permit using exported
   contacts for cold outreach (some prohibit it)? Record the clause.
2. Accuracy: bounce rate on a verified 50-address sample from your
   actual ICP, not their marketing number.
3. Cost per *usable* lead at target volume (after filtering + bounces).
4. GDPR posture: what the provider claims as their lawful basis for the
   records they sell, and what that means for `lawful_basis` +
   `RELAY_REGION_BASIS_RULES` on import (EU prospects likely stay
   excluded until the legal-preflight artifact exists — item 1).
5. Deletion propagation: when RELAY erases a person (DSR), nothing
   forces the provider to; the source register's `deletion_mechanism`
   field must record what the provider actually offers.

## Decision

*(unfilled — record provider, date, license clause, and sample bounce
rate here when the trigger fires)*
