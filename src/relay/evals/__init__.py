"""Reasoning-quality evaluation harness (Phase 2).

Software tests prove the pipeline's plumbing; these evals prove the
*reasoning* behind it still behaves after a prompt or model change. Run
them whenever RELAY_*_MODEL, a prompt template, or a backend changes —
a drop below threshold is a regression gate, not a suggestion.
"""

from relay.evals.harness import EvalReport, run_evals

__all__ = ["EvalReport", "run_evals"]
