# AWS email infrastructure as code

Everything that was clicked together in the SES console, as Terraform:
the sending-domain identity with DKIM, the custom MAIL FROM, the
configuration set, and the SES → SNS → SQS return path RELAY's event
worker drains. Standing up a new sending domain becomes one apply plus
pasting a handful of DNS records.

This is the **email side only** — compute lives wherever you deployed
it (`deploy/gcp/`, a VPS, anywhere). Cross-cloud is the intended shape:
RELAY calls SES/SQS as APIs over the internet.

## What one `terraform apply` creates

| Resource | Purpose | Replaces (manual step) |
| --- | --- | --- |
| SES domain identity + DKIM | who mail is from, cryptographically | "Verified identities → Create identity" per domain/address |
| Custom MAIL FROM (`mail.<domain>`) | SPF-aligned bounce return path | MAIL FROM console config |
| Configuration set (TLS required, reputation metrics on) | every RELAY send is tagged with it | config-set console setup |
| SNS topic + SQS queue + subscription + queue policy | bounce/complaint/delivery events back to RELAY | the SNS/SQS wiring done by hand for the pilot |
| (optional) IAM user + key, least-privilege | `ses:Send*` on this identity + drain this queue, nothing else | the hand-made pilot IAM user |

## Steps

```bash
cd deploy/aws
cp terraform.tfvars.example terraform.tfvars   # set your sending domain
terraform init && terraform apply

terraform output dns_records        # paste these into your DNS
terraform output identity_verification   # ...then poll until VERIFIED
terraform output env_values         # → AWS_REGION, RELAY_SES_CONFIGURATION_SET, RELAY_SQS_QUEUE_URL
terraform output -raw aws_secret_access_key   # → AWS_SECRET_ACCESS_KEY (once, into your secret store)
```

Map the outputs into RELAY's environment (`.env` locally, Secret
Manager + `app_env` on GCP), set `RELAY_SES_FROM` to an address at the
domain, and the existing attestation flags
(`RELAY_SENDER_IDENTITY_APPROVED`, `RELAY_SENDER_DOMAIN_AUTHENTICATED`)
become true statements you can flip.

Keep the Terraform state private — with `create_iam_user=true` the
access key secret lives in it (same caveat, and same fix, as the GCP
module's generated DB passwords).

## What this does NOT automate, on purpose

**Per-recipient verification does not exist outside the sandbox.** The
manual "configure every recipient email" work from the pilot is the SES
*sandbox* anti-spam gate: recipients must click a confirmation link,
which is precisely the thing that cannot and should not be automated.
The real path for "50 researched clients" is **production access**, after
which recipients need zero AWS setup — you verify sending domains (this
module), never recipients.

## Requesting SES production access (the actual unlock)

Console → SES → Account dashboard → "Request production access". It's a
short form reviewed by a human at AWS; RELAY gives you honest answers:

- **Use case:** B2B outreach with per-recipient lawful-basis tracking;
  every message individually approved by a human before sending.
- **Bounce/complaint handling:** automated — SES events flow through
  SNS/SQS into a suppression list written in the same transaction;
  suppressed addresses are structurally unmailable afterwards; alarms
  fire on bounce-rate thresholds (`RELAY_ALERT_BOUNCE_RATE`).
- **Unsubscribe:** RFC 8058 one-click plus List-Unsubscribe mailto on
  every message; processing is automatic and permanent.
- **Volume ramp:** start under the warmup schedule
  (`RELAY_WARMUP_DAILY_START` / `_INCREMENT`), citing your intended
  daily volume.

Approval typically lands within a day or two. Until then the sandbox
allowlist path (`RELAY_PILOT_RECIPIENTS`) keeps working unchanged.

## After access: what changes in RELAY

Nothing structural — the gates simply stop being the binding
constraint. `RELAY_PILOT_RECIPIENTS` stops being the recipient filter
only when you remove it (it fails closed; an empty allowlist blocks
real sends, which is what you want until the legal-preflight artifacts
from `docs/prototype-status.md` items 1–2 are in place). Daily caps,
pacing, warmup, and per-send human approval all keep applying.
