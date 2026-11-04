"""F3: the harness drives the real Controller deterministically on a FakeClock."""

from __future__ import annotations

from pathlib import Path

from harness import Harness, make_job


async def _run(d: Path) -> tuple[list[tuple[str, str, str]], list[float]]:
    h = Harness(d)
    t0 = h.clock.now()
    for i in range(4):
        h.arrive_at(t0 + 37 * i, make_job(i))
    h.arrive_at(t0 + 900, make_job(9))
    await h.run_until(t0 + 3 * 3600)
    times = [r["ts"] - t0 for r in h.conn.execute("SELECT ts FROM transitions ORDER BY id")]
    return [(a, b, "") for a, b, _ in h.transitions()], times


async def test_two_runs_same_transitions(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    ra = await _run(tmp_path / "a")
    rb = await _run(tmp_path / "b")
    assert ra == rb
    assert ra[0][-1][1] == "STOPPED"


async def test_arrive_at_and_run_until(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    t0 = h.clock.now()
    h.arrive_at(t0 + 100, make_job(0))
    await h.run_until(t0 + 50)
    assert h.instances() == [] and h.clock.now() == t0 + 50
    await h.run_until(t0 + 2000)
    assert [j["status"] for j in h.jobs()] == ["completed"]
    assert h.provider.create_calls == 1 and h.provider.live_refs() == []


async def test_controller_heartbeat_and_backup(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    await h.step()
    assert h.conn.execute("SELECT value FROM controller_state WHERE key='heartbeat'").fetchone()
    # REV2-7: one backup dir shared with `llmhub backup`, own prefix (separate rotation)
    assert list((tmp_path / "backup").glob("controller-*.db"))
    assert not (tmp_path / "backups").exists()
