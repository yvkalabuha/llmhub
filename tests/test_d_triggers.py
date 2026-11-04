import pytest
from pydantic import ValidationError

from llmhub.configuration.models import PoolConfig, QueueConfig
from llmhub.scheduler.triggers import (
    QueuedJob,
    dispatch_capacity,
    effective_deadline,
    idle_stop_at,
    launch_trigger,
    load_queued,
    work_seconds,
)
from llmhub.task_queue.models import Priority

NOW = 10_000.0
Q = QueueConfig()


def job(i: int, prio: Priority = Priority.LOW, created: float = NOW, **kw: float) -> QueuedJob:
    return QueuedJob(f"j{i}", "c", prio, created, **kw)  # type: ignore[arg-type]


def trig(jobs, now=NOW, q=Q, cold=540.0, work=60.0, prewarm=False):
    return launch_trigger(jobs, now, q, cold, lambda j: work, prewarm)


def test_empty_queue() -> None:
    d = trig([])
    assert (d.launch, d.reason, d.wake_at) == (False, "empty", None)


def test_urgent_immediately() -> None:
    assert trig([job(1, Priority.URGENT), job(2)]).reason == "urgent"


def test_normal_immediately() -> None:
    d = trig([job(1, Priority.NORMAL)])
    assert d.launch and d.reason == "batch:normal"


def test_four_low_wait_five_low_launch() -> None:
    jobs = [job(i, created=NOW - 100 + i) for i in range(4)]
    d = trig(jobs)
    assert not d.launch and d.wake_at == NOW - 100 + 1800
    assert trig(jobs + [job(5)]).launch


def test_batch_wait_elapsed() -> None:
    jobs = [job(1, created=NOW - 1800), job(2)]
    d = trig(jobs)
    assert d.launch and d.reason == "batch_wait:low"


def test_deadline_includes_work_and_margin() -> None:
    # lead = 540 + 60 (work) + 60 (margin) = 660
    j = [job(1, deadline_at=NOW + 661)]
    d = trig(j)
    assert not d.launch and d.wake_at == NOW + 1
    assert trig(j, now=NOW + 1).reason == "deadline"


def test_deadline_parity_mode_ignores_work() -> None:
    q = QueueConfig(deadline_includes_work=False)
    j = [job(1, deadline_at=NOW + 601)]
    assert not trig(j, q=q, work=1000).launch  # lead = 600
    assert trig(j, now=NOW + 1, q=q, work=1000).launch
    q2 = QueueConfig(deadline_includes_work=True)
    assert trig(j, q=q2, work=1000).launch


def test_max_queue_wait_is_deadline() -> None:
    j = job(1, created=NOW - 5400 + 100)  # low: max queue wait 5400 -> deadline NOW+100
    assert effective_deadline(j, Q) == NOW + 100
    assert trig([j]).reason in {"deadline", "batch_wait:low"}


def test_effective_deadline_min_and_none() -> None:
    assert effective_deadline(job(1, Priority.URGENT), Q) is None
    j = job(1, deadline_at=NOW + 10)
    assert effective_deadline(j, Q) == NOW + 10
    assert effective_deadline(job(1, max_queue_wait_s=50), Q) == NOW + 50


def test_prewarm_launches_empty_queue() -> None:
    d = trig([], prewarm=True)
    assert d.launch and d.reason == "prewarm"


def test_dispatch_capacity() -> None:
    assert (
        dispatch_capacity(0, 2) == 2
        and dispatch_capacity(2, 2) == 0
        and dispatch_capacity(3, 2) == 0
    )


def _pool(**kw: object) -> PoolConfig:
    return PoolConfig(name="p", provider="mock", gpu="g", vram_gb=24, pp_tps=1000, tg_tps=50,
                      image="i", disk_gb=10, **kw)  # type: ignore[arg-type]  # fmt: skip


def test_idle_timeout_range() -> None:
    assert _pool().idle_timeout_s == 300
    assert idle_stop_at(100.0, _pool(idle_timeout_s=0)) == 100.0
    assert idle_stop_at(100.0, _pool(idle_timeout_s=900)) == 1000.0
    with pytest.raises(ValidationError):
        _pool(idle_timeout_s=901)
    with pytest.raises(ValidationError):
        _pool(idle_timeout_s=-1)


def test_work_seconds() -> None:
    assert (
        work_seconds(
            QueuedJob("a", "c", Priority.LOW, 0, est_tokens_in=2000, est_tokens_out=100), _pool()
        )
        == 2 + 2
    )


def test_load_queued_from_db(tmp_path) -> None:
    from d_fixtures import T0, Env, spec
    from llmhub.task_queue import queue

    env = Env(tmp_path)
    queue.submit(env.conn, env.bodies, spec(1, prio=Priority.LOW), T0, env.cfg)
    queue.submit(env.conn, env.bodies, spec(2, prio=Priority.URGENT), T0 + 1, env.cfg)
    jobs = load_queued(env.conn, "p1")
    assert [j.priority for j in jobs] == [Priority.URGENT, Priority.LOW]
    assert trig(jobs, now=T0 + 1).reason == "urgent"
