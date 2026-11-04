"""T-368 independent review, cycle 2 (failures and restarts): money-leak reproductions.

The reproductions of the findings (task file, «Ревью (цикл 2: сбои и перезапуски)») were
`xfail(strict=True)` until the fix round 1 and now pass.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from f_fixtures import cfg_with
from harness import TEST_SECRET, Harness, make_job
from llmhub.clock import SystemClock
from llmhub.lifecycle_manager import machine
from llmhub.persistence.db import connect, migrate
from llmhub.provider_adapters.labels import make_label, new_op_id
from llmhub.provider_adapters.mock import Fault, MockKnobs, MockProvider
from llmhub.provider_adapters.mock_store import MockStore


async def _late_visible_after_max_attempts(tmp_path: Path) -> Harness:
    """Attempt 1 creates a resource that the list shows only after 300 s (> grace 120 s);
    attempt 2 is an unknown outcome that created nothing. E3 at ~240 s. Before the fix zero
    was confirmed (STOPPED) before the first resource became visible; since fix round 1 the
    row waits in PENDING_DELETE until first_create_called_at + 3 * grace."""
    h = Harness(tmp_path, knobs=MockKnobs(late_visible_s=300))
    h.provider.inject("create", Fault.CREATE_TIMEOUT_CREATED_LATE)
    h.provider.inject("create", Fault.CREATE_TIMEOUT_NOT_CREATED)
    h.submit(make_job(0))
    await h.run_for(290)
    assert h.provider.create_calls == 2
    assert h.state_of() == "PENDING_DELETE"  # F-R2-1: zero not confirmed yet
    assert len(h.provider.live_refs()) == 1  # ...but attempt 1 lives on (still invisible)
    return h


async def test_late_visible_resource_of_earlier_attempt_controller_alone(tmp_path: Path) -> None:
    h = await _late_visible_after_max_attempts(tmp_path)
    await h.run_for(3600)
    assert h.provider.live_refs() == []


async def test_late_visible_resource_is_caught_by_reaper(tmp_path: Path) -> None:
    """Backstop works: one reaper pass after the resource becomes visible destroys it."""
    h = await _late_visible_after_max_attempts(tmp_path)
    h.clock.advance_to(h.clock.now() + 60)
    await h.drive(h.reaper().run_once())
    assert h.provider.live_refs() == []


async def test_reaper_destroys_resource_of_error_row_when_controller_dead(tmp_path: Path) -> None:
    """Checked, not a bug: orphan_sweep destroys the resource of an ERROR row (the row itself
    waits for the lifetime fuse, but nothing is billed)."""
    h = Harness(tmp_path)
    h.submit(make_job(0))
    await h.run_for(150)
    assert h.state_of() == "IDLE"
    iid = h.instances()[0]["id"]
    # the controller marks the instance lost (e.g. network loss) and dies right after
    assert machine.mark_lost(h.conn, iid, h.clock.now(), "network_loss", cfg=h.cfg)
    r = h.reaper()
    for _ in range(10):  # ~15 min of reaper passes every 90 s
        h.clock.advance_to(h.clock.now() + 90)
        await h.drive(r.run_once())
    assert h.provider.live_refs() == []


def _drop_pool_p1(h: Harness, tmp_path: Path) -> None:
    p2 = h.cfg.pools["p1"].model_copy(update={"name": "p2"})
    h.cfg = cfg_with(tmp_path).model_copy(update={"pools": {"p2": p2}})


async def test_restart_with_pool_removed_idle_instance_is_torn_down(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.submit(make_job(0))
    await h.run_for(150)
    assert h.state_of() == "IDLE"
    _drop_pool_p1(h, tmp_path)
    h.restart()
    await h.run_for(3600)
    assert h.provider.live_refs() == []


async def test_restart_with_pool_removed_ready_instance_does_not_crash(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.submit(make_job(0))
    await h.run_for(150)
    assert h.state_of() == "IDLE"
    # crash right after E6 (READY, nothing dispatched yet)
    h.conn.execute("UPDATE instances SET state='READY', idle_since=NULL")
    _drop_pool_p1(h, tmp_path)
    h.restart()
    await h.step()


@pytest.mark.slow
def test_reaper_process_kill_file_with_corrupt_db(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"data_dir: {tmp_path}\npools: {{}}\n", encoding="utf-8")
    conn = connect(tmp_path / "state.db")
    migrate(conn)
    conn.close()
    (tmp_path / "state.db").write_bytes(b"garbage" * 1000)
    for side in ("state.db-wal", "state.db-shm"):
        (tmp_path / side).unlink(missing_ok=True)
    provider = MockProvider(MockStore(tmp_path / "mock_cloud.db"), SystemClock())
    ours = provider.add_orphan(make_label(new_op_id(), TEST_SECRET))
    (tmp_path / "KILL").write_text("stop", encoding="utf-8")
    env = {
        **os.environ,
        "LLMHUB_LABEL_SECRET": TEST_SECRET.decode(),
        "LLMHUB_MOCK_STORE": str(tmp_path / "mock_cloud.db"),
    }
    out = subprocess.run(
        [sys.executable, "-m", "llmhub.reaper", "--config", str(cfg), "--once"],
        env=env, capture_output=True, text=True, timeout=120, check=False,
    )  # fmt: skip
    assert ours not in provider.live_refs(), out.stderr[-500:]

