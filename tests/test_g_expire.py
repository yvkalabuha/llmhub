"""T-369 G3a: expire_overdue."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from d_fixtures import T0, Env, spec
from llmhub.task_queue import local, queue


def sub(env: Env, n: int, *, cloud: bool, **kw: object) -> str:
    s = replace(spec(n), **kw)  # type: ignore[arg-type]
    if not cloud:
        s = replace(s, route="local", pool=None)
    return queue.submit(env.conn, env.bodies, s, T0, env.cfg).job_id


def test_deadline_local_and_cloud(tmp_path: Path) -> None:
    env = Env(tmp_path)
    lj = sub(env, 1, cloud=False, deadline_at=T0 + 100)
    cj = sub(env, 2, cloud=True, deadline_at=T0 + 100)
    assert local.expire_overdue(env.conn, T0 + 99) == []
    assert env.job(lj)["status"] == "queued"
    out = local.expire_overdue(env.conn, T0 + 100)
    assert sorted(out) == sorted([(lj, "deadline_exceeded"), (cj, "deadline_exceeded")])
    for j in (lj, cj):
        r = env.job(j)
        assert (r["status"], r["error_code"], r["finished_at"]) == (
            "failed", "deadline_exceeded", T0 + 100,
        )  # fmt: skip
    assert env.lease(cj)["closed_at"] == T0 + 100  # pool no longer pinned


def test_queue_wait(tmp_path: Path) -> None:
    env = Env(tmp_path)
    lj = sub(env, 1, cloud=False, max_queue_wait_s=60.0)
    cj = sub(env, 2, cloud=True, max_queue_wait_s=60.0)
    assert local.expire_overdue(env.conn, T0 + 59) == []
    out = local.expire_overdue(env.conn, T0 + 60)
    assert sorted(out) == sorted([(lj, "queue_wait_exceeded"), (cj, "queue_wait_exceeded")])
    assert env.lease(cj)["closed_at"] is not None


def test_deadline_wins_when_both(tmp_path: Path) -> None:
    env = Env(tmp_path)
    j = sub(env, 1, cloud=False, deadline_at=T0 + 10, max_queue_wait_s=10.0)
    assert local.expire_overdue(env.conn, T0 + 20) == [(j, "deadline_exceeded")]


def test_running_and_terminal_untouched(tmp_path: Path) -> None:
    env = Env(tmp_path)
    r = sub(env, 1, cloud=False, deadline_at=T0 + 1)
    local.mark_running_local(env.conn, r, "w", "local:cpu", T0)
    d = sub(env, 2, cloud=False, deadline_at=T0 + 1)
    queue.cancel(env.conn, d, T0)
    n = sub(env, 3, cloud=False)  # no limits
    assert local.expire_overdue(env.conn, T0 + 10_000) == []
    assert env.job(r)["status"] == "running" and env.job(d)["status"] == "cancelled"
    assert env.job(n)["status"] == "queued"
