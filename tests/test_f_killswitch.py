"""F7: kill switch -> 0 resources within 60 s on the FakeClock; KILL file; hanging create."""

from __future__ import annotations

from pathlib import Path

import pytest

from harness import TEST_SECRET, Harness, make_job
from llmhub.controller import KillFilePresent
from llmhub.lifecycle_manager.safety import is_halted
from llmhub.provider_adapters.base import ResourceKind
from llmhub.provider_adapters.labels import make_label, new_op_id
from llmhub.provider_adapters.mock import Fault, MockKnobs


async def _busy(h: Harness) -> float:
    t0 = h.clock.now()
    h.submit(make_job(0, max_tokens=78 * 600))
    await h.run_until(t0 + 200)
    assert h.state_of() == "BUSY"
    return t0


async def test_kill_zero_resources_within_60s(tmp_path: Path) -> None:
    h = Harness(tmp_path, knobs=MockKnobs(destroy_delay_s=20))
    await _busy(h)
    inst = h.instances()[0]
    h.provider.add_attached(inst["label"], ResourceKind.VOLUME)
    orphan = h.provider.add_orphan(make_label(new_op_id(), TEST_SECRET))
    foreign = h.provider.add_foreign("not-ours")
    assert len(h.provider.live_refs()) == 4
    start = h.clock.now()
    report = await h.drive(h.controller.kill())
    assert report.completed and report.elapsed_s <= 60
    assert h.clock.now() - start <= 60
    assert h.provider.live_refs() == [foreign]
    assert orphan in report.destroyed
    assert is_halted(h.conn)
    job = h.jobs()[0]
    assert (job["status"], job["attempts"], job["restarts"]) == ("queued", 0, 1)


async def test_after_kill_no_launch_until_resume(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    await _busy(h)
    await h.drive(h.controller.kill())
    h.submit(make_job(1))
    await h.run_for(600)
    assert h.provider.create_calls == 1
    assert all(j["route"] == "local:fallback" for j in h.jobs())
    h.controller.resume()
    h.submit(make_job(2))
    await h.run_for(600)
    assert h.provider.create_calls == 2 and h.jobs()[-1]["status"] == "completed"


async def test_kill_file_controller(tmp_path: Path) -> None:
    h = Harness(tmp_path, knobs=MockKnobs(destroy_delay_s=20))
    await _busy(h)
    (tmp_path / "KILL").touch()
    start = h.clock.now()
    await h.run_until(start + 60)
    assert h.provider.live_refs() == [] and is_halted(h.conn)
    zero_at = h.conn.execute("SELECT zero_confirmed_at FROM instances").fetchone()[0]
    assert zero_at is not None and zero_at - start <= 60
    with pytest.raises(KillFilePresent):
        h.controller.resume()
    (tmp_path / "KILL").unlink()
    h.controller.resume()
    assert not is_halted(h.conn)


async def test_create_hangs_kill_zero(tmp_path: Path) -> None:
    h = Harness(tmp_path)  # hang_s = 3600: the call is abandoned by the 20 s guard
    h.provider.inject("create", Fault.CREATE_HANG)
    t0 = h.clock.now()
    h.submit(make_job(0))
    await h.run_until(t0 + 3)
    assert h.instances()[0]["provider_ref"] is None and len(h.provider.live_refs()) == 1
    report = await h.drive(h.controller.kill())
    assert report.completed and h.clock.now() - t0 <= 60
    assert h.provider.live_refs() == []
    await h.run_until(t0 + 400)
    assert h.provider.live_refs() == [] and h.state_of() == "STOPPED"


async def test_kill_during_create_lag(tmp_path: Path) -> None:
    h = Harness(tmp_path, knobs=MockKnobs(list_owned_lag_s=300, hang_s=10))
    h.provider.inject("create", Fault.CREATE_HANG)
    t0 = h.clock.now()
    h.submit(make_job(0))
    await h.run_until(t0 + 2)
    assert len(h.provider.live_refs()) == 1  # exists, invisible in list_owned (lag > grace)
    await h.drive(h.controller.kill())
    await h.run_until(t0 + 60)  # the late answer arrives at +10: ref stored without CAS
    assert h.provider.live_refs() == []
    inst = h.instances()[0]
    assert inst["provider_ref"] is not None
    assert inst["state"] in ("PENDING_DELETE", "STOPPED")
    await h.run_until(t0 + 600)
    assert h.state_of() == "STOPPED"
