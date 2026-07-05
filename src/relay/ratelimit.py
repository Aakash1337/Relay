"""Rate limiting & bounded retries for external calls (Phase 2).

Every call that leaves the process (compute providers, CRM) goes through
a named token bucket and, for transient failures only, a bounded
exponential backoff. Two deliberate properties:

- **Backpressure is visible, not silent.** When a bucket cannot admit a
  call within the configured wait, it raises ``Backpressure`` — callers
  park work (error_retryable) instead of queueing unboundedly.
- **Retries never change the answer's provenance.** Only
  ``ComputeUnavailable``-class failures are retried; refusals and
  invalid outputs are never re-rolled, and there is no tier/provider
  fallback here or anywhere else.

Buckets are process-local by design (documented limitation; distributed
limiting arrives with the multi-process deployment story in Phase 3).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from relay.config import get_settings
from relay.logs import get_logger

log = get_logger(__name__)


class Backpressure(Exception):
    """The rate limiter cannot admit this call within the allowed wait."""


@dataclass
class TokenBucket:
    """Classic token bucket; injectable clock/sleeper for testability."""

    rate: float  # tokens per second
    capacity: float
    clock: Callable[[], float] = time.monotonic
    sleeper: Callable[[float], None] = time.sleep
    _tokens: float = field(init=False)
    _updated: float = field(init=False)
    _lock: threading.Lock = field(init=False, default_factory=threading.Lock)

    def __post_init__(self) -> None:
        if self.rate <= 0:
            raise ValueError("rate must be positive (0 means: no bucket at all)")
        self.capacity = max(self.capacity, 1.0)
        self._tokens = self.capacity
        self._updated = self.clock()

    def _refill_locked(self) -> None:
        now = self.clock()
        self._tokens = min(
            self.capacity, self._tokens + (now - self._updated) * self.rate
        )
        self._updated = now

    def acquire(self, *, max_wait: float) -> float:
        """Take one token, sleeping if needed. Returns seconds waited.

        Raises :class:`Backpressure` if the wait would exceed ``max_wait``.
        """
        with self._lock:
            self._refill_locked()
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return 0.0
            wait = (1.0 - self._tokens) / self.rate
            if wait > max_wait:
                raise Backpressure(
                    f"rate limit backpressure: next slot in {wait:.1f}s "
                    f"exceeds max wait {max_wait:.1f}s"
                )
            # Reserve the token now (may go negative-free since we take
            # after sleeping outside the lock would race; instead deduct
            # the future token here).
            self._tokens -= 1.0
        self.sleeper(wait)
        return wait


_buckets: dict[str, TokenBucket] = {}
_buckets_lock = threading.Lock()


def bucket(name: str, rps: float, *, burst: float = 5.0) -> TokenBucket | None:
    """The process-wide bucket for ``name`` at ``rps``. None when rps==0
    (limiting disabled). Rebuilt if the configured rate changed."""
    if rps <= 0:
        return None
    key = f"{name}@{float(rps)}"
    with _buckets_lock:
        existing = _buckets.get(key)
        if existing is None:
            existing = TokenBucket(rate=rps, capacity=burst)
            _buckets[key] = existing
        return existing


def reset_buckets() -> None:
    with _buckets_lock:
        _buckets.clear()


def limit_compute(tier: str) -> None:
    """Apply the configured per-tier compute rate limit (no-op at rps 0)."""
    settings = get_settings()
    rps = (
        settings.rate_limit_local_rps
        if tier == "local"
        else settings.rate_limit_hosted_rps
    )
    b = bucket(f"compute:{tier}", rps)
    if b is not None:
        waited = b.acquire(max_wait=settings.rate_limit_max_wait_seconds)
        if waited > 0:
            log.info("rate limit wait", bucket=f"compute:{tier}", waited=waited)


def limit_crm() -> None:
    settings = get_settings()
    b = bucket("crm", settings.rate_limit_crm_rps)
    if b is not None:
        b.acquire(max_wait=settings.rate_limit_max_wait_seconds)


def with_backoff[T](
    fn: Callable[[], T],
    *,
    attempts: int,
    base_seconds: float,
    retry_on: tuple[type[BaseException], ...] | Iterable[type[BaseException]],
    sleeper: Callable[[float], None] = time.sleep,
    what: str = "call",
) -> T:
    """Run ``fn`` with up to ``attempts`` retries on ``retry_on`` failures.

    Bounded and exponential (base, 2·base, 4·base…). Anything not listed
    in ``retry_on`` — refusals, invalid output, config errors — raises
    immediately: retrying those either re-rolls a decision or hammers a
    broken configuration.
    """
    retry_types = tuple(retry_on)
    attempt = 0
    while True:
        try:
            return fn()
        except retry_types as exc:
            if attempt >= attempts:
                raise
            delay = base_seconds * (2**attempt)
            attempt += 1
            log.warning(
                "transient failure — retrying",
                what=what,
                attempt=attempt,
                max_attempts=attempts,
                delay_seconds=delay,
                error=str(exc),
            )
            sleeper(delay)
