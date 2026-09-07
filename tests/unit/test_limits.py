"""Limits that keep one server from becoming everyone else's outage."""

from __future__ import annotations

import threading

import pytest

from floodline.limits import (
    ConcurrencyLimiter,
    RateLimiter,
    TooBusyError,
    TooManyRequestsError,
)


def test_a_burst_is_allowed_then_the_rate_bites() -> None:
    """A page loading its own map must not trip the limit; a scraper must."""
    limiter = RateLimiter(per_minute=60.0, burst=5)
    for _ in range(5):
        limiter.check("a", now=0.0)
    with pytest.raises(TooManyRequestsError):
        limiter.check("a", now=0.0)


def test_the_bucket_refills_continuously() -> None:
    """Fixed windows let a client spend two windows' allowance across a boundary."""
    limiter = RateLimiter(per_minute=60.0, burst=2)
    limiter.check("a", now=0.0)
    limiter.check("a", now=0.0)
    with pytest.raises(TooManyRequestsError):
        limiter.check("a", now=0.0)
    # One token a second at 60/minute, so a second later exactly one is available.
    limiter.check("a", now=1.0)
    with pytest.raises(TooManyRequestsError):
        limiter.check("a", now=1.0)


def test_clients_do_not_share_a_bucket() -> None:
    limiter = RateLimiter(per_minute=60.0, burst=1)
    limiter.check("a", now=0.0)
    limiter.check("b", now=0.0)  # one noisy client must not lock everyone else out
    with pytest.raises(TooManyRequestsError):
        limiter.check("a", now=0.0)


def test_the_refusal_says_when_to_come_back() -> None:
    """Without a wait, a caller retries immediately and the limit achieves nothing."""
    limiter = RateLimiter(per_minute=60.0, burst=1)
    limiter.check("a", now=0.0)
    with pytest.raises(TooManyRequestsError) as caught:
        limiter.check("a", now=0.0)
    assert caught.value.retry_after_s > 0


def test_concurrency_admits_up_to_the_limit_and_refuses_past_it() -> None:
    limiter = ConcurrencyLimiter(limit=2, wait_s=0.05)
    held = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with limiter:
            held.set()
            release.wait(timeout=5)

    workers = [threading.Thread(target=hold) for _ in range(2)]
    for w in workers:
        w.start()
    held.wait(timeout=5)
    # Both slots are taken; a third caller waits its bounded wait and is refused.
    import time

    time.sleep(0.05)
    with pytest.raises(TooBusyError), limiter:
        pass
    release.set()
    for w in workers:
        w.join(timeout=5)
    # And the slots come back afterwards.
    with limiter:
        pass


def test_a_slot_is_returned_even_when_the_work_raises() -> None:
    """A leaked slot degrades the server permanently and silently."""
    limiter = ConcurrencyLimiter(limit=1, wait_s=0.05)
    with pytest.raises(ValueError, match="boom"), limiter:
        raise ValueError("boom")
    with limiter:
        pass


def test_impossible_limits_are_refused_at_construction() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        ConcurrencyLimiter(limit=0)
    with pytest.raises(ValueError, match="positive"):
        RateLimiter(per_minute=0)
    with pytest.raises(ValueError, match="at least 1"):
        RateLimiter(burst=0)
