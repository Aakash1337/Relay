"""Rate limiter + bounded backoff: deterministic tests with fake clocks."""

from __future__ import annotations

import pytest

from relay.compute.base import ComputeRefused, ComputeUnavailable
from relay.config import get_settings
from relay.ratelimit import (
    Backpressure,
    TokenBucket,
    bucket,
    reset_buckets,
    with_backoff,
)


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture(autouse=True)
def _fresh():
    reset_buckets()
    yield
    reset_buckets()
    get_settings.cache_clear()


# ── Token bucket ─────────────────────────────────────────────────────────────


def test_bucket_allows_burst_then_paces():
    clock = _FakeClock()
    sleeps: list[float] = []

    def sleeper(s: float) -> None:
        sleeps.append(s)
        clock.sleep(s)

    b = TokenBucket(rate=1.0, capacity=2.0, clock=clock, sleeper=sleeper)
    assert b.acquire(max_wait=10) == 0.0  # burst token 1
    assert b.acquire(max_wait=10) == 0.0  # burst token 2
    waited = b.acquire(max_wait=10)  # now empty: must wait ~1s
    assert waited == pytest.approx(1.0)
    assert sleeps == [pytest.approx(1.0)]


def test_bucket_refills_over_time():
    clock = _FakeClock()
    b = TokenBucket(rate=2.0, capacity=2.0, clock=clock, sleeper=clock.sleep)
    b.acquire(max_wait=0.01)
    b.acquire(max_wait=0.01)
    clock.sleep(1.0)  # 2 tokens refilled
    assert b.acquire(max_wait=0.01) == 0.0
    assert b.acquire(max_wait=0.01) == 0.0


def test_bucket_raises_backpressure_beyond_max_wait():
    clock = _FakeClock()
    b = TokenBucket(rate=0.1, capacity=1.0, clock=clock, sleeper=clock.sleep)
    b.acquire(max_wait=1)
    with pytest.raises(Backpressure, match="max wait"):
        b.acquire(max_wait=1)  # next slot is 10s away


def test_bucket_registry_disabled_at_zero_rps():
    assert bucket("x", 0.0) is None
    assert bucket("x", 1.0) is not None
    # Same name+rate → same bucket instance (shared pacing).
    assert bucket("x", 1.0) is bucket("x", 1.0)


# ── Bounded backoff ──────────────────────────────────────────────────────────


def test_backoff_retries_transient_then_succeeds():
    calls: list[int] = []
    sleeps: list[float] = []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise ComputeUnavailable("blip")
        return "ok"

    result = with_backoff(
        flaky,
        attempts=2,
        base_seconds=0.5,
        retry_on=(ComputeUnavailable,),
        sleeper=sleeps.append,
    )
    assert result == "ok"
    assert len(calls) == 3
    assert sleeps == [0.5, 1.0]  # exponential


def test_backoff_is_bounded():
    def always_down():
        raise ComputeUnavailable("still down")

    with pytest.raises(ComputeUnavailable):
        with_backoff(
            always_down,
            attempts=2,
            base_seconds=0.0,
            retry_on=(ComputeUnavailable,),
            sleeper=lambda s: None,
        )


def test_backoff_never_retries_refusals():
    calls: list[int] = []

    def refuses():
        calls.append(1)
        raise ComputeRefused("no")

    with pytest.raises(ComputeRefused):
        with_backoff(
            refuses,
            attempts=5,
            base_seconds=0.0,
            retry_on=(ComputeUnavailable,),
            sleeper=lambda s: None,
        )
    assert len(calls) == 1  # a refusal is never re-rolled


# ── Integration: the executor seam applies the limiter ─────────────────────


def test_execute_applies_compute_bucket(tenant_a, monkeypatch):
    from relay.guardrails.harness import RunHarness
    from relay.routing.executors import execute
    from relay.routing.router import TaskType

    tenant_id, _ = tenant_a
    # Near-zero refill rate: tokens spent by execute() stay visibly spent
    # (the default burst of 5 covers the single call without waiting).
    monkeypatch.setenv("RELAY_RATE_LIMIT_LOCAL_RPS", "0.001")
    get_settings.cache_clear()

    harness = RunHarness(tenant_id=tenant_id, kind="rl")
    result = execute(TaskType.SUMMARIZATION, harness=harness)
    assert result.backend == "offline"
    # The bucket exists and was drawn from.
    b = bucket("compute:local", 0.001)
    assert b is not None and b._tokens < b.capacity
