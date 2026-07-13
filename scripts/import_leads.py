"""Import researched prospects from a CSV into RELAY via the batch API.

The 50-clients flow: research produces a spreadsheet; this turns it into
leads under one campaign/source in a single call, with per-row results.
Intake only — nothing here drafts or sends anything.

CSV columns (header row required; only `email` is mandatory):
    email, first_name, last_name, title, company_name, company_domain,
    region, lawful_basis, bio

Usage:
    uv run python scripts/import_leads.py prospects.csv \\
        --api-url http://127.0.0.1:8000 --api-key rk_... \\
        --campaign <campaign-uuid> --source <source-uuid> \\
        [--lawful-basis legitimate_interest_b2b] [--region US] [--real]

Rows may override --lawful-basis / --region with their own columns.
Leads default to dry_run=true; pass --real only when this batch is
genuinely meant for the real-send path (all the usual gates still apply).
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

import httpx

BATCH_LIMIT = 500


def rows_from_csv(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        return [
            {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            for row in reader
        ]


def to_item(row: dict, *, lawful_basis: str, region: str, dry_run: bool) -> dict:
    if not row.get("email"):
        raise ValueError("row has no email")
    item = {
        "email": row["email"],
        "lawful_basis": row.get("lawful_basis") or lawful_basis,
        "region_assumption": row.get("region") or region,
        "dry_run": dry_run,
    }
    for field in (
        "first_name",
        "last_name",
        "title",
        "company_name",
        "company_domain",
        "bio",
    ):
        if row.get(field):
            item[field] = row[field]
    if row.get("retention_until"):
        item["retention_until"] = row["retention_until"]
    return item


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("csv_file", type=Path)
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    parser.add_argument("--api-key", required=True)
    parser.add_argument("--campaign", required=True, help="campaign UUID")
    parser.add_argument("--source", required=True, help="source-register UUID")
    parser.add_argument("--lawful-basis", default="synthetic")
    parser.add_argument("--region", default="US")
    parser.add_argument(
        "--real",
        action="store_true",
        help="mark leads dry_run=false (real-send path; all gates still apply)",
    )
    args = parser.parse_args()

    rows = rows_from_csv(args.csv_file)
    if not rows:
        print("CSV contains no rows", file=sys.stderr)
        return 1

    items, bad_rows = [], 0
    for n, row in enumerate(rows, start=2):  # 1-based + header line
        try:
            items.append(
                to_item(
                    row,
                    lawful_basis=args.lawful_basis,
                    region=args.region,
                    dry_run=not args.real,
                )
            )
        except ValueError as exc:
            bad_rows += 1
            print(f"line {n}: skipped ({exc})", file=sys.stderr)

    created = failed = 0
    with httpx.Client(
        base_url=args.api_url,
        headers={"X-API-Key": args.api_key},
        timeout=60,
    ) as client:
        for start in range(0, len(items), BATCH_LIMIT):
            chunk = items[start : start + BATCH_LIMIT]
            response = client.post(
                "/leads/batch",
                json={
                    "campaign_id": args.campaign,
                    "source_id": args.source,
                    "items": chunk,
                },
            )
            if response.status_code not in (200, 207):
                print(f"batch failed: {response.status_code} {response.text[:300]}")
                return 1
            body = response.json()
            created += body["created"]
            failed += body["failed"]
            for item in body["results"]:
                if not item["ok"]:
                    print(f"  {item['email']}: {item['error']}", file=sys.stderr)

    print(
        json.dumps(
            {
                "rows": len(rows),
                "skipped_rows": bad_rows,
                "created": created,
                "failed": failed,
            }
        )
    )
    return 0 if failed == 0 and bad_rows == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
