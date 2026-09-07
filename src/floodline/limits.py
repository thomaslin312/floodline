"""Keeping one server from becoming everyone else's outage.

This service is a thin front end over other people's infrastructure. A watershed
request pulls elevation tiles from USGS, gauge records from NWIS, structures from
USACE and, in places, claims from FEMA - all keyless, all with this machine's identity
attached. Left uncapped, a handful of visitors turns into a burst of range reads
against a public agency that has no way to tell them apart from an attack, and the
polite outcome is a block.

Two limits, because they guard different things.

**Concurrency** guards this machine. A watershed at 30 m is tens of millions of cells
and depression filling is global, so the whole grid has to be resident; a few of those
in parallel exhausts memory whatever the request rate is. The cap is on work in
flight, not on requests arriving.

**Rate** guards everyone upstream. It is per client, refills continuously, and allows
a burst so that a page loading its own map does not trip it.

Both are in-process and therefore per-worker. That is honest for a single-process
deployment and wrong for a fleet, where a shared store would be needed; the docstring
says so rather than letting someone discover it in production.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

__all__ = [
    "ConcurrencyLimiter",
    "RateLimiter",
    "TooBusyError",
    "TooManyRequestsError",
]


class TooBusyError(RuntimeError):
    """Raised when the machine already has as much work in flight as it can hold."""

    def __init__(self, waited_s: float, limit: int) -> None:
        super().__init__(
            f"{limit} watershed computations are already running and one did not "
            f"finish within {waited_s:g}s"
        )
        self.waited_s = waited_s
        self.limit = limit


class TooManyRequestsError(RuntimeError):
    """Raised when one client has asked for more than its share."""

    def __init__(self, retry_after_s: float) -> None:
        super().__init__(f"rate limit exceeded; retry in {retry_after_s:.0f}s")
        self.retry_after_s = retry_after_s


@dataclass
class ConcurrencyLimiter:
    """Cap on expensive work in flight, with a bounded wait rather than a queue.

    A queue would accept every request and make each one slower until they all time
    out somewhere else. Refusing quickly with a Retry-After is more useful to a caller
    and much more useful to whoever is reading the logs.
    """

    limit: int = 2
    wait_s: float = 20.0
    _semaphore: threading.Semaphore = field(init=False)

    def __post_init__(self) -> None:
        """Build the semaphore, and refuse a limit that would admit nobody."""
        if self.limit < 1:
            raise ValueError(f"limit must be at least 1, got {self.limit}")
        self._semaphore = threading.Semaphore(self.limit)

    def __enter__(self) -> ConcurrencyLimiter:
        """Take a slot, or raise once the bounded wait is up."""
        if not self._semaphore.acquire(timeout=self.wait_s):
            raise TooBusyError(self.wait_s, self.limit)
        return self

    def __exit__(self, *exc: object) -> None:
        """Give the slot back, whether the work succeeded or raised."""
        self._semaphore.release()


@dataclass
class RateLimiter:
    """A token bucket per client.

    Continuous refill rather than fixed windows: a window boundary lets a client spend
    a whole window's allowance twice in quick succession, which is exactly the burst
    this exists to prevent.

    Buckets for clients that have gone away are dropped on a sweep rather than by a
    background task, so nothing has to be started or stopped alongside the app.
    """

    per_minute: float = 30.0
    burst: int = 10
    _buckets: dict[str, tuple[float, float]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _last_sweep: float = field(default=0.0)

    def __post_init__(self) -> None:
        """Refuse a rate or a burst that could never admit a request."""
        if self.per_minute <= 0:
            raise ValueError(f"per_minute must be positive, got {self.per_minute}")
        if self.burst < 1:
            raise ValueError(f"burst must be at least 1, got {self.burst}")

    def check(self, client: str, *, now: float | None = None) -> None:
        """Spend one token for `client`, or raise `TooManyRequestsError`."""
        moment = time.monotonic() if now is None else now
        refill = self.per_minute / 60.0
        with self._lock:
            self._sweep(moment)
            tokens, last = self._buckets.get(client, (float(self.burst), moment))
            tokens = min(float(self.burst), tokens + (moment - last) * refill)
            if tokens < 1.0:
                self._buckets[client] = (tokens, moment)
                raise TooManyRequestsError((1.0 - tokens) / refill)
            self._buckets[client] = (tokens - 1.0, moment)

    def _sweep(self, moment: float) -> None:
        """Drop buckets that have refilled completely and so hold no state worth keeping."""
        if moment - self._last_sweep < 60.0:
            return
        self._last_sweep = moment
        full_after = self.burst / (self.per_minute / 60.0)
        self._buckets = {
            client: entry
            for client, entry in self._buckets.items()
            if moment - entry[1] < full_after
        }
