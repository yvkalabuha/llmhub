"""T-369 G3: local queue operations."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from d_fixtures import T0, Env, spec
from llmhub.cloud_manager.inference import JobRunResult
from llmhub.task_queue import local, queue
from llmhub.task_queue.models import Priority

RES = JobRunResult(tokens_in=10, tokens_out=5, finish_reason="stop", body=b"{}")


def lspec(n: int, **kw: object):  # type: ignore[no-untyped-def]
    return replace(spec(n, **kw), route="local", pool=None)  # type: ignore[arg-type]


def sub(env: Env, n: int, at: float = T0, **kw: object) -> str:
    return queue.submit(env.conn, env.bodies, lspec(n, **kw), at, env.cfg).job_id


def test_pick_only_local_in_order(tmp_path: Path) -> None:
    env = Env(tmp_path)
    queue.submit(env.conn, env.bodies, spec(0), T0, env.cfg)  # cloud: ignored
    assert local.pick_next_local(env.conn, T0) is None
    low = sub(env, 1, T0, prio=Priority.LOW)
    a1 = sub(env, 2, T0 + 1, client="c2")
    urgent = sub(env, 3, T0 + 2, prio=Priority.URGENT, client="c3")
    assert local.pick_next_local(env.conn, T0)["id"] == urgent  # type: ignore[index]
    env.conn.execute("UPDATE jobs SET status='completed' WHERE id=?", (urgent,))
    assert local.pick_next_local(env.conn, T0)["id"] == a1  # type: ignore[index]  # normal > low
    env.conn.execute("UPDATE jobs SET status='completed' WHERE id=?", (a1,))
    assert local.pick_next_local(env.conn, T0)["id"] == low  # type: ignore[index]


def test_round_robin_by_client(tmp_path: Path) -> None:
    env = Env(tmp_path)
    a1 = sub(env, 1, T0)
    a2 = sub(env, 2, T0 + 1)
    b1 = sub(env, 3, T0 + 2, client="c2")
    assert local.mark_running_local(env.conn, a1, "w", "local:cpu", T0 + 3)
    env.conn.execute("UPDATE jobs SET status='completed' WHERE id=?", (a1,))
    # client A just ran: B goes before A's second job although it is younger
    assert local.pick_next_local(env.conn, T0 + 4)["id"] == b1  # type: ignore[index]
    assert a2 != b1


def test_mark_running_cas(tmp_path: Path) -> None:
    env = Env(tmp_path)
    j = sub(env, 1)
    assert local.mark_running_local(env.conn, j, "w", "local:cpu", T0 + 1)
    assert not local.mark_running_local(env.conn, j, "w2", "local:cpu", T0 + 2)
    r = env.job(j)
    assert (r["status"], r["worker_id"], r["backend"], r["attempts"], r["instance_id"]) == (
        "running",
        "w",
        "local:cpu",
        1,
        None,
    )


def test_mark_running_refuses_cloud_job(tmp_path: Path) -> None:
    env = Env(tmp_path)
    j = queue.submit(env.conn, env.bodies, spec(0), T0, env.cfg).job_id
    assert not local.mark_running_local(env.conn, j, "w", "local:cpu", T0)


def test_complete_local_cas(tmp_path: Path) -> None:
    env = Env(tmp_path)
    j = sub(env, 1)
    assert not local.complete_local(env.conn, j, "w", RES, T0 + 1, "r")  # not running
    local.mark_running_local(env.conn, j, "w", "local:cpu", T0)
    assert not local.complete_local(env.conn, j, "other", RES, T0 + 1, "r")
    assert env.job(j)["status"] == "running" and env.job(j)["res_path"] is None
    assert local.complete_local(env.conn, j, "w", RES, T0 + 2, "r")
    r = env.job(j)
    assert (r["status"], r["cost_usd"], r["tokens_out"], r["res_path"]) == (
        "completed",
        0.0,
        5,
        "r",
    )
    assert not local.complete_local(env.conn, j, "w", RES, T0 + 3, "r2")


def test_complete_local_cancel_requested(tmp_path: Path) -> None:
    env = Env(tmp_path)
    j = sub(env, 1)
    local.mark_running_local(env.conn, j, "w", "local:cpu", T0)
    queue.cancel(env.conn, j, T0 + 1)
    assert local.complete_local(env.conn, j, "w", RES, T0 + 2, "r")
    assert env.job(j)["status"] == "cancelled"


def test_requeue_local_until_attempts_then_fail(tmp_path: Path) -> None:
    env = Env(tmp_path)
    j = sub(env, 1)
    local.mark_running_local(env.conn, j, "w", "local:cpu", T0)
    assert local.requeue_local(env.conn, j, T0 + 1, "upstream_timeout") == "queued"
    r = env.job(j)
    assert (r["status"], r["worker_id"], r["error_code"]) == ("queued", None, "upstream_timeout")
    local.mark_running_local(env.conn, j, "w", "local:cpu", T0 + 2)
    assert local.requeue_local(env.conn, j, T0 + 3, "upstream_timeout") == "failed"
    r = env.job(j)
    assert (r["status"], r["error_code"], r["route"]) == ("failed", "upstream_timeout", "local")
    # unknown reasons map to local_unavailable
    k = sub(env, 2)
    local.mark_running_local(env.conn, k, "w", "local:cpu", T0)
    assert local.requeue_local(env.conn, k, T0, "boom", max_attempts=1) == "failed"
    assert env.job(k)["error_code"] == "local_unavailable"


def test_fail_closes_lease_and_keeps_reason(tmp_path: Path) -> None:
    env = Env(tmp_path)
    j = queue.submit(env.conn, env.bodies, spec(0), T0, env.cfg).job_id  # cloud, has lease
    queue.fallback_local(env.conn, j, "budget_limit_reached", T0 + 1)
    local.fail(env.conn, j, "cloud_unavailable", T0 + 2)
    r = env.job(j)
    assert (r["status"], r["error_code"], r["fallback_reason"], r["finished_at"]) == (
        "failed",
        "cloud_unavailable",
        "budget_limit_reached",
        T0 + 2,
    )
    k = queue.submit(env.conn, env.bodies, spec(1), T0, env.cfg).job_id
    local.fail(env.conn, k, "x", T0 + 2, reason="why")
    assert env.job(k)["fallback_reason"] == "why"
    assert env.lease(k)["closed_at"] == T0 + 2
    local.fail(env.conn, k, "y", T0 + 3)  # terminal: untouched
    assert env.job(k)["error_code"] == "x"


def test_local_queue_ahead(tmp_path: Path) -> None:
    env = Env(tmp_path)
    run = sub(env, 1, T0)
    local.mark_running_local(env.conn, run, "w", "local:cpu", T0)
    n1 = sub(env, 2, T0 + 1)
    urgent = sub(env, 3, T0 + 2, prio=Priority.URGENT)
    low = sub(env, 4, T0 + 3, prio=Priority.LOW)

    def ids(rank: int, at: float) -> list[str]:
        return [r["id"] for r in local.local_queue_ahead(env.conn, rank, at)]

    assert ids(1, T0 + 10) == [run, n1, urgent] or set(ids(1, T0 + 10)) == {run, n1, urgent}
    assert set(ids(2, T0 + 10)) == {run, n1, urgent, low}
    assert set(ids(0, T0 + 10)) == {run, urgent}
