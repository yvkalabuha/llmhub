"""T-369 H3: EWMA, availability, external busy, local ETA."""

from __future__ import annotations

import pytest

from llmhub.configuration.api_models import LocalConfig
from llmhub.local_inference.client import LocalChatResult, ProbeResult
from llmhub.local_inference.eta import local_eta_s, work_s
from llmhub.local_inference.stats import BackendStats, Ewma


def res(pp: float | None, tg: float | None, tout: int = 40, lat: float = 10.0) -> LocalChatResult:
    return LocalChatResult(b"{}", 100, tout, "stop", pp, tg, lat)


def stats(pp: float = 100.0, tg: float = 5.0) -> BackendStats:
    return BackendStats(Ewma(0.3, pp), Ewma(0.3, tg))


def test_ewma_values() -> None:
    e = Ewma(0.3, 10.0)
    assert e.update(20.0) == pytest.approx(13.0)
    assert e.update(20.0) == pytest.approx(15.1)
    assert e.value == pytest.approx(15.1)


def test_on_result_uses_timings() -> None:
    s = stats()
    s.on_result(res(200.0, 10.0))
    assert s.pp.value == pytest.approx(130.0) and s.tg.value == pytest.approx(6.5)


def test_on_result_without_timings_uses_latency() -> None:
    s = stats()
    s.on_result(res(None, None, tout=40, lat=10.0))  # 4 tok/s observed
    assert s.tg.value == pytest.approx(0.3 * 4 + 0.7 * 5)
    assert s.pp.value == 100.0
    s.on_result(res(None, None, tout=0, lat=10.0))  # nothing to learn
    assert s.tg.value == pytest.approx(0.3 * 4 + 0.7 * 5)


def test_probe_availability() -> None:
    s = stats()
    bad = ProbeResult(False, 0, None, None)
    s.on_probe(bad, False, 3)
    s.on_probe(bad, False, 3)
    assert s.available and s.failures == 2
    s.on_probe(bad, False, 3)
    assert not s.available
    s.on_probe(ProbeResult(True, 0, 1, 200), False, 3)
    assert s.available and s.failures == 0


def test_busy_external_only_without_our_job() -> None:
    s = stats()
    busy = ProbeResult(True, 1, 1, 200)
    s.on_probe(busy, True, 3)
    assert not s.busy_external
    s.on_probe(busy, False, 3)
    assert s.busy_external
    s.on_probe(ProbeResult(True, 0, 1, 200), False, 3)
    assert not s.busy_external
    s.on_probe(busy, False, 3)
    s.on_probe(ProbeResult(False, 0, None, None), False, 3)
    assert not s.busy_external


@pytest.mark.parametrize(
    ("tin", "tout", "pp", "tg", "want"),
    [(1000, 100, 100, 5, 30.0), (0, 50, 100, 10, 5.0), (200, 0, 50, 5, 4.0)],
)
def test_work_s(tin: int, tout: int, pp: float, tg: float, want: float) -> None:
    assert work_s(tin, tout, pp, tg) == pytest.approx(want)


def test_local_eta_table() -> None:
    cfg = LocalConfig(external_busy_penalty_s=120)
    s = stats(100, 5)
    own = (1000, 100)  # 30 s
    assert local_eta_s(ahead=[], running_left_s=0, own=own, stats=s, cfg=cfg) == pytest.approx(30)
    assert local_eta_s(ahead=[(1000, 100)] * 2, running_left_s=0, own=own, stats=s,
                       cfg=cfg) == pytest.approx(90)  # fmt: skip
    assert local_eta_s(ahead=[(500, 50)], running_left_s=7, own=own, stats=s,
                       cfg=cfg) == pytest.approx(7 + 15 + 30)  # fmt: skip
    s.busy_external = True
    assert local_eta_s(ahead=[], running_left_s=0, own=own, stats=s, cfg=cfg) == pytest.approx(150)
    assert local_eta_s(ahead=[], running_left_s=-5, own=own, stats=s, cfg=cfg) == pytest.approx(150)
