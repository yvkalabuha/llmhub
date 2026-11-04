"""T-368 review round 2: the controller's 60 s orphan sweep (F-R2-1) must also run when the
controller is otherwise idle. Before the fix `step()` returned INF once nothing was live, so in
`run_forever` the sweep waited for the next API call (the reaper remained the only guard)."""

from __future__ import annotations

import math
from pathlib import Path

from harness import Harness, make_job
from llmhub.controller import ORPHAN_SWEEP_S, ORPHAN_WATCH_S


async def _stopped(h: Harness) -> None:
    h.submit(make_job(0))
    await h.run_for(600)
    assert h.state_of() == "STOPPED"


async def _follow_wakes(h: Harness, until: float) -> None:
    """Step only at the controller's own wake-ups (as `run_forever` does without API calls)."""
    nw = await h.step()
    while nw <= until:
        h.clock.advance_to(max(nw, h.clock.now()))
        nw = await h.step()


async def test_idle_controller_wakes_for_the_orphan_sweep(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    await _stopped(h)
    nw = await h.step()
    assert nw - h.clock.now() <= ORPHAN_SWEEP_S


async def test_late_resource_of_stopped_row_destroyed_by_controller_wakes_alone(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path)
    await _stopped(h)
    late = h.provider.add_orphan(h.instances()[0]["label"])
    await _follow_wakes(h, h.clock.now() + 2 * ORPHAN_SWEEP_S)
    assert late not in h.provider.live_refs()


async def test_no_endless_polling_after_the_watch_window(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    await _stopped(h)
    h.clock.advance_to(h.clock.now() + ORPHAN_WATCH_S + 1)
    await h.step()
    assert math.isinf(await h.step())
