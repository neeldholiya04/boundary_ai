import pytest

from boundary_agent.limits import BudgetExceeded, DailySpend, RateLimiter


async def test_spend_cap_blocks_once_reached_and_zero_means_no_cap():
    spend = DailySpend(0.05)
    await spend.check()
    await spend.add(0.03)
    await spend.check()
    await spend.add(0.03)
    with pytest.raises(BudgetExceeded, match=r"\$0\.05"):
        await spend.check()

    uncapped = DailySpend(0)
    await uncapped.add(100.0)
    await uncapped.check()  # no cap configured: never raises


async def test_spend_cap_fails_closed_when_the_store_is_down():
    class Broken:
        async def get(self, key):
            raise ConnectionError("redis down")

    with pytest.raises(BudgetExceeded, match="unavailable"):
        await DailySpend(1.0, redis=Broken()).check()


async def test_rate_limiter_counts_per_client_and_bucket():
    limiter = RateLimiter()
    results = [await limiter.hit("scan", "1.2.3.4", limit=2, window_s=60) for _ in range(3)]
    assert [ok for ok, _ in results] == [True, True, False]
    assert 0 < results[-1][1] <= 61  # retry-after within the window
    assert (await limiter.hit("scan", "5.6.7.8", limit=2, window_s=60))[0]  # other client unaffected
    assert (await limiter.hit("attack", "1.2.3.4", limit=2, window_s=60))[0]  # other bucket unaffected


async def test_rate_limiter_fails_open_when_the_store_is_down():
    class Broken:
        async def incr(self, key):
            raise ConnectionError("redis down")

    assert (await RateLimiter(redis=Broken()).hit("scan", "x", limit=1, window_s=60))[0]
