"""Abuse controls: a global daily LLM spend cap and per-client rate limits.

Both use Redis when it is configured (so several API replicas share one budget and one limit) and an
in-process store otherwise. Redis failures fail *closed* for the spend cap (no budget visibility
means no spending) and *open* for rate limits (a Redis blip should not take the playground down).
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger("boundary.limits")


class BudgetExceeded(RuntimeError):
    """The daily LLM budget is used up."""


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


class DailySpend:
    """Global LLM spend for the current UTC day, against an optional cap (0 or less = no cap)."""

    def __init__(
        self, cap_usd: float, redis: Any | None = None, *, prefix: str = "boundary:llm_spend"
    ) -> None:
        self.cap_usd = cap_usd
        self.redis = redis
        self.prefix = prefix
        self._memory: dict[str, float] = {}

    @property
    def capped(self) -> bool:
        return self.cap_usd > 0

    async def spent_today(self) -> float:
        key = f"{self.prefix}:{_today()}"
        if self.redis is None:
            return self._memory.get(key, 0.0)
        value = await self.redis.get(key)
        return float(value or 0.0)

    async def check(self) -> None:
        """Raise BudgetExceeded when the cap is reached. Unknown spend (Redis down) counts as reached."""
        if not self.capped:
            return
        try:
            spent = await self.spent_today()
        except Exception as exc:
            logger.warning("spend store unavailable; refusing LLM calls: %s", exc)
            raise BudgetExceeded("LLM budget store unavailable") from exc
        if spent >= self.cap_usd:
            raise BudgetExceeded(f"daily LLM budget of ${self.cap_usd:.2f} reached (spent ${spent:.4f})")

    async def add(self, cost_usd: float) -> None:
        if cost_usd <= 0:
            return
        key = f"{self.prefix}:{_today()}"
        if self.redis is None:
            self._memory[key] = self._memory.get(key, 0.0) + cost_usd
            return
        try:
            await self.redis.incrbyfloat(key, cost_usd)
            await self.redis.expire(key, 3 * 86400)
        except Exception:
            logger.warning("could not record LLM spend", exc_info=True)


class RateLimiter:
    """Fixed-window counters per (bucket, client). `hit` returns (allowed, retry_after_seconds)."""

    def __init__(self, redis: Any | None = None, *, prefix: str = "boundary:rate") -> None:
        self.redis = redis
        self.prefix = prefix
        self._memory: dict[str, tuple[int, float]] = {}

    async def hit(self, bucket: str, client: str, *, limit: int, window_s: int) -> tuple[bool, int]:
        if limit <= 0:
            return True, 0
        now = time.time()
        window = int(now // window_s)
        retry_after = int((window + 1) * window_s - now) + 1
        key = f"{self.prefix}:{bucket}:{client}:{window}"
        if self.redis is None:
            count, _ = self._memory.get(key, (0, now))
            count += 1
            self._memory[key] = (count, now)
            if len(self._memory) > 10_000:  # drop old windows
                self._memory = {k: v for k, v in self._memory.items() if now - v[1] < window_s}
        else:
            try:
                count = int(await self.redis.incr(key))
                if count == 1:
                    await self.redis.expire(key, window_s + 5)
            except Exception:
                logger.warning("rate limiter store unavailable; allowing request", exc_info=True)
                return True, 0
        return count <= limit, retry_after
