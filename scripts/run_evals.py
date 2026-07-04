"""Run the reasoning evals against the CONFIGURED backends.

    just evals             # both tiers, as configured in .env
    uv run python scripts/run_evals.py local
    uv run python scripts/run_evals.py hosted

Exit code is non-zero if any evaluated backend fails its threshold —
wire this into CI or a pre-deploy check when models/prompts change.
Note: this calls the real configured providers and spends real quota.
"""

from __future__ import annotations

import sys

from relay.compute.registry import backend_for
from relay.evals import run_evals
from relay.logs import setup_logging
from relay.routing.router import ComputeTier


def main() -> None:
    setup_logging()
    which = sys.argv[1] if len(sys.argv) > 1 else "both"
    tiers = {
        "local": [ComputeTier.LOCAL],
        "hosted": [ComputeTier.HOSTED],
        "both": [ComputeTier.LOCAL, ComputeTier.HOSTED],
    }[which]

    failed = False
    for tier in tiers:
        backend = backend_for(tier)
        report = run_evals(backend)
        print(f"\n─── {tier} tier: {report.backend}:{report.model} ───")
        for category, (passed, total) in sorted(report.by_category.items()):
            marker = "✓" if passed == total else "✗"
            print(f"  {marker} {category:16s} {passed}/{total}")
        verdict = "PASS" if report.passed else "FAIL"
        print(
            f"  → {verdict} (pass rate {report.pass_rate:.0%}, "
            f"threshold {report.threshold:.0%})"
        )
        for r in report.failures:
            print(f"    ✗ {r.case_id}: {r.detail}")
        failed = failed or not report.passed

    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
