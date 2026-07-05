# Decision record: Sending Provider (§6)

**Status:** RESOLVED for Phase 1C (testing sender). Production sender
selected but deferred to implementation until the real-prospect phase on
corporate budget.
**Date:** Phase 1C.
**Revisit:** before the first real-prospect send (see Revisit criteria),
and any time a provider's cold-outreach ToS changes.

## The question

1C ("tiny real-send pilot") needs a concrete email sending provider. Two
sub-questions: which provider sends the test emails at zero cost without
tripping any acceptable-use policy, and which provider sends to real
prospects in production. These are not the same provider, and the reason
they differ is a hard terms-of-service fork, not a preference.

## The ToS fork (why this isn't a free choice)

Email providers split into two non-overlapping categories for our purpose:

- **Transactional ESPs — prohibit cold outreach.** Postmark, SendGrid,
  Mailgun, Brevo, and Amazon SES production sending all require
  permission-based/opt-in recipients and treat cold prospecting as a
  policy violation, enforced by automated complaint-rate monitoring. An
  account ban typically locks contact exports during review. These are
  correct only for genuine transactional mail (internal notifications),
  never for prospect outreach.
- **Cold-outreach platforms — permit and are built for it.** Smartlead,
  Instantly, and peers permit cold in ToS and provide the infrastructure
  a transactional API lacks: mailbox rotation, automated warmup, reply
  detection, deliverability tooling. Warmup is not optional in 2026;
  skipping it lands mail in spam within days.

Consequence: no transactional ESP may be used for prospect sending. SES
appears below only in **sandbox mode sending to our own verified
inboxes**, which is not cold outreach.

## Decision

### 1. Testing sender (Phase 1C) = Amazon SES, sandbox mode

- SES stays in its default sandbox, which can send only to verified
  identities — i.e. our own controlled inboxes. This makes it
  structurally impossible to email a stranger, so the cold-outreach ToS
  problem does not arise (self-to-self is not cold outreach), and no SES
  production-access request is filed.
- Sending identity is an address at **testings.work** (a dedicated,
  disposable domain; not a production or personal domain). The domain is
  verified in SES and its SPF, DKIM, and DMARC records are published in
  Cloudflare DNS. This makes 1C exercise the real DNS/authentication
  path rather than faking it.
- Recipients for the pilot are two inboxes we control (one Gmail, one
  Outlook), verified in SES as sandbox recipients.
- Bounce and complaint events are delivered via SES notifications (SNS)
  and ingested into the existing suppression/state machinery, so webhook
  ingestion is exercised for real.
- Cost: effectively zero. Sandbox requires no spend approval; even on
  paid SES a handful of test messages cost fractions of a cent. This
  satisfies the project rule that the testing phase spends no money.

### 2. Production sender (real prospects, deferred) = Smartlead, via its REST API

- Selected over Instantly because RELAY already owns the brain and
  orchestration and needs sending infrastructure it drives, not an
  all-in-one product. Smartlead is API-first (campaigns, leads, sender
  accounts, follow-ups, warmup, webhooks are all programmatic),
  integrates natively with n8n, prices by lead volume rather than
  mailbox count, and exposes the exact webhook events the suppression
  list consumes (sent, opened, replied, bounced, unsubscribed).
  Instantly remains the fallback if Smartlead's ToS or API posture
  changes.
- **Not built during testing.** The Smartlead adapter is implemented
  only when RELAY moves to real prospects on corporate/paid
  infrastructure. Building it now would be untestable scaffolding
  against a paid account we are not yet using — the exact anti-pattern
  this project avoids.
- Plan note for procurement: API access is gated behind Smartlead's Pro
  tier (~$94/mo at time of writing). Verify current pricing and,
  critically, re-confirm cold-outreach is still permitted in their ToS
  at the time of purchase.

### 3. Sender interface abstraction

The sender is a seam behind the existing send-eligibility and
suppression gates. It must abstract two operational shapes, because the
two providers work differently:

- **Direct send (SES):** RELAY emits one message now; the send and its
  result are effectively synchronous from RELAY's side (delivery result
  async via SNS).
- **Campaign enrollment (Smartlead):** RELAY hands a lead to a campaign;
  Smartlead owns rotation, scheduling, and the actual send; all outcomes
  (sent/replied/bounced) arrive asynchronously via webhook.

Build the SES adapter now; design the interface so a Smartlead adapter
drops in later without touching the gates, the state machine, or call
sites.

*Implementation note (Phase 1C):* `relay.senders.base` defines both
shapes — `DirectSender` (implemented: `SESSender`) and
`EnrollmentSender` (interface only, carrying the idempotency-boundary
contract below in its docstring).

## Idempotency boundary note (read before building the Smartlead adapter)

RELAY's existing structural guarantee — the `uq_send_jobs_idempotency_key`
unique constraint on `(tenant_id, idempotency_key)` plus the partial
unique index enforcing one active send per lead — guards the moment
RELAY creates/executes a send. That holds as-is under SES, because RELAY
owns the send and the send-job row is 1:1 with the actual email.

Under Smartlead the send moment moves outside RELAY's process, and the
guarantee must be re-expressed accordingly:

- "One active send per lead" becomes **"one active campaign enrollment
  per lead,"** enforced in RELAY's DB before the enroll API call. The
  existing constraint that guards the send row does not guard
  Smartlead's internal queue.
- The idempotency key must guard **the enroll call**, so a retried
  enroll cannot double-enroll. Do not rely on Smartlead deduping by
  email address.
- State transition to `sent` is driven by the **sent webhook**, not the
  enroll API response — the API response only confirms enrollment, not
  delivery.
- **Crash recovery must extend across the API boundary.** If RELAY
  crashes after calling the enroll API but before recording it, restart
  must detect the lead is already enrolled (via the idempotency key, or
  by querying Smartlead) rather than re-enrolling. This is the same
  "outcome unknown → retry anyway → double-send" trap Phase 2 crash
  recovery closed; it now has to hold across the provider boundary, not
  just the local send row.

Record any deviation from the above here when the Smartlead adapter is
implemented.

## Compliance

- Cold outreach obligations (CAN-SPAM, GDPR where applicable) apply from
  the first real send and are gated upstream in the pipeline, not at the
  sender. The sender decision does not discharge them.
- Testing (SES sandbox, self-to-self, synthetic content) touches no
  third-party personal data and raises none of these obligations.
- Production provider choice is conditional on that provider permitting
  cold outreach **in writing** at time of purchase; re-verify, do not
  assume.

## Revisit criteria (before first real-prospect send)

Open this record and complete it when all hold:

1. Smartlead (or the chosen cold-permissive provider) account exists on
   corporate budget, with cold outreach confirmed permitted in its
   current ToS.
2. Sending domain and warmed mailboxes for real outreach exist (a
   dedicated production outreach domain, not testings.work and not any
   personal/brand domain), with SPF/DKIM/DMARC aligned and warmup
   completed.
3. The Smartlead adapter is implemented behind the existing gates, with
   the idempotency boundary above correctly re-expressed as
   enrollment-level and validated by test.
4. Volume caps, bounce/complaint webhook ingestion, and the
   human-approval gate are wired to the production sender.

**Until then: SES sandbox, self-to-self only.**
