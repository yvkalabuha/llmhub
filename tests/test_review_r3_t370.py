"""T-370 review round 3: the weights hold cannot be bypassed by urgent jobs, and
`rate_limited` stays on the host-fault (blacklist) path, bounded by MAX_FAILED_CYCLES."""

from __future__ import annotations

from pathlib import Path

from harness import Harness, make_job
from llmhub.cloud_manager.provisioner import set_weights_hold, weights_hold_until
from llmhub.provider_adapters.mock_inference import MockInferenceKnobs
from llmhub.task_queue.models import Priority
from test_fix2_t370 import CONFIG_KNOBS, _cfg, _launches, _wave
from test_w_provisioner_loading import make_h


async def test_urgent_job_while_held_goes_local_without_launch(tmp_path: Path) -> None:
    h = make_h(tmp_path, _cfg(tmp_path), knobs=CONFIG_KNOBS)
    set_weights_hold(h.conn, "p1", h.clock.now(), h.cfg, "weights_failed:exhausted")
    h.submit(make_job(0, priority=Priority.URGENT))
    await h.run_until(h.clock.now() + 60)
    assert _launches(h) == 0
    (job,) = h.jobs()
    assert job["route"] == "local:fallback"
    assert job["fallback_reason"] == "deployment_error"


async def test_rate_limited_exhaustion_blacklists_host_and_never_holds(tmp_path: Path) -> None:
    knobs = MockInferenceKnobs(load_fail="exhausted", load_causes=("rate_limited",))
    h = make_h(tmp_path, _cfg(tmp_path), knobs=knobs)
    await _wave(h, 0)
    assert 1 <= _launches(h) <= 3  # MAX_FAILED_CYCLES, each on a new (blacklisted) host
    assert h.conn.execute("SELECT COUNT(*) FROM host_blacklist").fetchone()[0] >= 1
    assert weights_hold_until(h.conn, "p1", h.clock.now(), h.cfg) is None
    assert not h.incidents("weights_config")


async def test_rate_limited_waves_are_bounded_by_failed_cycles(tmp_path: Path) -> None:
    """Documents the accepted host-fault path: per wave at most MAX_FAILED_CYCLES launches."""
    from llmhub.controller import MAX_FAILED_CYCLES

    knobs = MockInferenceKnobs(load_fail="exhausted", load_causes=("rate_limited",))
    h: Harness = make_h(tmp_path, _cfg(tmp_path), knobs=knobs)
    for n in range(2):
        await _wave(h, n, 1200.0)
    assert _launches(h) <= 2 * MAX_FAILED_CYCLES
