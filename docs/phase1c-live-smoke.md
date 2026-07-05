# Phase 1C live smoke checklist — run when AWS answers arrive

Everything below reuses the exact code paths proven hermetically in
`tests/test_phase1c_send.py` and `tests/test_ses_ingest.py`; the live
smoke swaps the fake SES client for the real one via configuration only.

## Inputs needed (from the operator)

1. AWS credentials (IAM user scoped to `ses:SendEmail`,
   `sqs:ReceiveMessage`, `sqs:DeleteMessage`) — exported as the standard
   `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` env vars, never
   committed.
2. `RELAY_AWS_REGION` — the SES region.
3. `RELAY_SES_FROM_ADDRESS` — the verified identity at `testings.work`.
4. The two verified sandbox recipients (the Gmail and the Outlook inbox).
5. Confirmation that SES domain verification + SPF/DKIM/DMARC records in
   Cloudflare are green (SES console → verified identities), or ask
   RELAY's operator to generate them:
   `aws sesv2 create-email-identity --email-identity testings.work`
   returns the DKIM tokens to publish.
6. Event transport: SNS topic for bounces/complaints/deliveries on the
   identity, subscribed to an SQS queue; `RELAY_SQS_QUEUE_URL` set.
   (Alternative: SNS HTTPS subscription to `/webhooks/ses?token=…` with
   `RELAY_SES_WEBHOOK_TOKEN` set and the API publicly reachable.)

## .env for the pilot

```bash
RELAY_REAL_SEND_ENABLED=true
RELAY_SENDER_PROVIDER=ses
RELAY_AWS_REGION=…
RELAY_SES_FROM_ADDRESS=pilot@testings.work
RELAY_UNSUBSCRIBE_MAILTO=unsubscribe@testings.work
RELAY_SENDER_IDENTITY_APPROVED=true       # after checking the SES console
RELAY_SENDER_DOMAIN_AUTHENTICATED=true    # after checking DKIM/SPF/DMARC green
RELAY_PROVIDER_TERMS_RECORD=docs/decisions/sending-provider.md
RELAY_REAL_SEND_DAILY_CAP=5
RELAY_SQS_QUEUE_URL=…
```

## The smoke itself

1. Create a tenant + campaign (`dry_run=false`) + two leads with
   `lawful_basis=test_consent`, emails = the two verified inboxes.
2. Run the pipeline to the human gate; approve BOTH drafts in `/review`
   (every pilot send is human-approved — exit-gate requirement).
3. `just worker` — expect 2 real sends; check both inboxes received the
   mail, with the List-Unsubscribe header present (view raw source).
4. Verify audit: `send.executed` rows with provider message ids.
5. Bounce path: send one more approved message to
   `bounce@simulator.amazonses.com` — SES's sandbox-safe bounce
   simulator (add it as a third lead; it needs no verification). Then
   `just events` — expect the lead in `bounce_received` and a
   `hard_bounce` suppression entry.
6. Re-run `just worker` and confirm the suppressed address can never be
   queued again (eligibility + trigger).
7. Confirm the daily cap: with `RELAY_REAL_SEND_DAILY_CAP=5`, a sixth
   approved send in 24h must land in `send_blocked` naming
   `mailbox_active_below_cap`.

Exit gate (§ roadmap): a handful of real, eligible, approved,
non-duplicate sends through the approved provider; suppression and
unsubscribe verified end to end; every send audited.
