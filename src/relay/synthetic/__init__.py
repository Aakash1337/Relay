"""Synthetic data (Phase 1A) — realistic fake prospects, adversarial on purpose.

Everything RELAY processes before the Legal/Data Preflight (Phase 1B) comes
from here: Faker-generated people at .test domains, with edge cases the
pipeline MUST survive deliberately mixed in — prompt-injection bios,
unicode names, sparse records, near-duplicate emails — plus deterministic
simulated replies for the triage path.
"""

from relay.synthetic.generator import (
    EdgeCase,
    ReplyIntent,
    SyntheticProspect,
    generate_prospects,
    simulated_reply_text,
)

__all__ = [
    "EdgeCase",
    "ReplyIntent",
    "SyntheticProspect",
    "generate_prospects",
    "simulated_reply_text",
]
