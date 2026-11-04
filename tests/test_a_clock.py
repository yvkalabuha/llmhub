import asyncio

import pytest

from llmhub.clock import FakeClock, SystemClock


async def test_fake_clock_default_start() -> None:
    assert FakeClock().now() == 1_793_750_400.0


async def test_sleep_until_wakes_in_time_order() -> None:
    clock = FakeClock(start=100.0)
    woke: list[float] = []

    async def sleeper(t: float) -> None:
        await clock.sleep_until(t)
        woke.append(t)

    tasks = [asyncio.create_task(sleeper(t)) for t in (130.0, 110.0, 120.0)]
    await asyncio.sleep(0)
    assert clock.next_waiter() == 110.0
    clock.advance_to(125.0)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert woke == [110.0, 120.0]
    assert clock.now() == 125.0
    assert clock.next_waiter() == 130.0
    clock.advance_to(200.0)
    await asyncio.gather(*tasks)
    assert woke == [110.0, 120.0, 130.0]
    assert clock.next_waiter() is None


async def test_sleep_until_past_returns_immediately() -> None:
    clock = FakeClock(start=100.0)
    await clock.sleep_until(50.0)
    assert clock.next_waiter() is None


async def test_advance_backwards_rejected() -> None:
    clock = FakeClock(start=100.0)
    with pytest.raises(ValueError):
        clock.advance_to(99.0)


async def test_system_clock_now_and_past_sleep() -> None:
    clock = SystemClock()
    t = clock.now()
    assert t > 1_700_000_000
    await clock.sleep_until(t - 1)
