from llmhub_infer.speed import SpeedMonitor

from i_fakes import FakeClock


def mk(min_bps: float = 100.0) -> tuple[FakeClock, SpeedMonitor]:
    c = FakeClock()
    return c, SpeedMonitor(c, grace_s=5, window_s=2, min_bps=min_bps)


def test_never_slow_before_grace() -> None:
    c, m = mk()
    c.advance(4.9)
    assert not m.too_slow()


def test_slow_after_grace() -> None:
    c, m = mk()
    for _ in range(6):
        c.advance(1)
        m.add(10)  # 10 B/s
    assert m.too_slow() and m.bps() < 100


def test_fast_not_slow() -> None:
    c, m = mk()
    for _ in range(6):
        c.advance(1)
        m.add(1000)
    assert not m.too_slow()


def test_burst_after_pause_counted_by_window() -> None:
    c, m = mk()
    c.advance(10)  # long pause, then a burst inside the window
    m.add(1000)
    assert m.bps() == 500 and not m.too_slow()
    c.advance(3)  # burst has left the window
    assert m.too_slow()


def test_zero_min_never() -> None:
    c, m = mk(0)
    c.advance(100)
    assert not m.too_slow()
