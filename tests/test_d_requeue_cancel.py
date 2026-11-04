from pathlib import Path

from d_fixtures import T0, Env, spec
from llmhub.task_queue import queue
from llmhub.task_queue.models import JobStatus


def running(env: Env, n: int = 1, worker: str = "boot-1", at: float = T0) -> str:
    jid = queue.submit(env.conn, env.bodies, spec(n), at, env.cfg).job_id
    assert queue.mark_running(env.conn, jid, "i1", worker, at)
    return jid


def test_requeue_counts_attempt_then_falls_back(tmp_path: Path) -> None:
    env = Env(tmp_path)
    jid = running(env)
    assert queue.requeue(env.conn, jid, T0 + 1, count_attempt=True, reason="gpu_lost") == "queued"
    j = env.job(jid)
    assert (j["status"], j["attempts"], j["instance_id"], j["worker_id"]) == (
        "queued",
        1,
        None,
        None,
    )
    queue.mark_running(env.conn, jid, "i1", "boot-1", T0 + 2)
    assert queue.requeue(env.conn, jid, T0 + 3, count_attempt=True, reason="gpu_lost") == "fallback"
    j = env.job(jid)
    assert j["route"] == "local:fallback" and j["pool"] is None and j["status"] == "queued"
    assert "attempts_exhausted" in j["fallback_reason"]
    assert env.lease(jid)["closed_at"] is not None


def test_requeue_without_attempt_refunds(tmp_path: Path) -> None:
    env = Env(tmp_path)
    jid = running(env)
    assert queue.requeue(env.conn, jid, T0 + 1, count_attempt=False, reason="x") == "queued"
    assert env.job(jid)["attempts"] == 0
    for _ in range(5):  # never exhausts
        queue.mark_running(env.conn, jid, "i1", "w", T0)
        assert queue.requeue(env.conn, jid, T0, count_attempt=False, reason="x") == "queued"


def test_requeue_close_lease_and_terminal(tmp_path: Path) -> None:
    env = Env(tmp_path)
    jid = running(env)
    queue.requeue(
        env.conn, jid, T0 + 1, count_attempt=True, reason="job_test_failed", close_lease=True
    )
    lease = env.lease(jid)
    assert lease["closed_at"] == T0 + 1 and lease["close_reason"] == "job_test_failed"
    queue.cancel(env.conn, jid, T0 + 2)
    assert queue.requeue(env.conn, jid, T0 + 3, count_attempt=True, reason="x") == "failed"
    assert env.job(jid)["status"] == "cancelled"


def test_fallback_local_closes_lease(tmp_path: Path) -> None:
    env = Env(tmp_path)
    jid = queue.submit(env.conn, env.bodies, spec(1), T0, env.cfg).job_id
    queue.fallback_local(env.conn, jid, "budget_limit_reached", T0 + 5)
    j = env.job(jid)
    assert j["route"] == "local:fallback" and j["fallback_reason"] == "budget_limit_reached"
    assert queue.pool_queue(env.conn, "p1") == []
    assert env.lease(jid)["close_reason"] == "fallback_local"


def test_cancel_queued_running_terminal(tmp_path: Path) -> None:
    env = Env(tmp_path)
    q = queue.submit(env.conn, env.bodies, spec(1), T0, env.cfg).job_id
    assert queue.cancel(env.conn, q, T0 + 1) is JobStatus.CANCELLED
    assert env.lease(q)["closed_at"] == T0 + 1
    assert queue.cancel(env.conn, q, T0 + 2) is JobStatus.CANCELLED
    r = running(env, 2)
    assert queue.cancel(env.conn, r, T0 + 1) is JobStatus.RUNNING
    assert env.job(r)["cancel_requested"] == 1 and env.lease(r)["closed_at"] is None
    # a cancelled-while-running job is cancelled, not requeued, on interruption
    queue.requeue(env.conn, r, T0 + 2, count_attempt=True, reason="x")
    assert env.job(r)["status"] == "cancelled"


def test_orphaned_running_by_boot_id_not_an_attempt(tmp_path: Path) -> None:
    env = Env(tmp_path)
    old = running(env, 1, worker="boot-old")
    mine = running(env, 2, worker="boot-2")
    queue.heartbeat(env.conn, [mine], "boot-2", T0 + 100)
    ids = queue.requeue_orphaned_running(env.conn, "boot-2", T0 + 120, stale_s=180)
    assert ids == [old]
    j = env.job(old)
    assert (j["status"], j["attempts"], j["restarts"]) == ("queued", 0, 1)
    assert env.job(mine)["status"] == "running"
    assert env.lease(old)["closed_at"] is None


def test_orphaned_running_stale_heartbeat(tmp_path: Path) -> None:
    env = Env(tmp_path)
    jid = running(env, 1, worker="boot-2")
    assert queue.requeue_orphaned_running(env.conn, "boot-2", T0 + 100, 180) == []
    assert queue.requeue_orphaned_running(env.conn, "boot-2", T0 + 200, 180) == [jid]


def test_orphans_never_exhaust_attempts(tmp_path: Path) -> None:
    env = Env(tmp_path)
    jid = running(env, 1, worker="b0")
    for i in range(5):
        queue.requeue_orphaned_running(env.conn, f"b{i + 1}", T0, 180)
        queue.mark_running(env.conn, jid, "i1", f"b{i + 1}", T0)
    assert env.job(jid)["route"] == "cloud" and env.job(jid)["restarts"] == 5


def test_purge_expired(tmp_path: Path) -> None:
    from llmhub.cloud_manager.inference import JobRunResult

    env = Env(tmp_path)
    jid = running(env, 1)
    res = env.bodies.write_result(jid, b"r")
    env.conn.execute("UPDATE jobs SET idem_key='k', idem_expires_at=? WHERE id=?", (T0 + 100, jid))
    queue.complete(env.conn, jid, JobRunResult(1, 1, "stop", b"r"), 0.0, T0 + 10, str(res))
    queue.purge_expired(env.conn, T0 + 50)
    assert env.job(jid)["idem_key"] == "k" and res.exists()
    queue.purge_expired(env.conn, T0 + 101)
    assert env.job(jid)["idem_key"] is None and res.exists()
    queue.purge_expired(env.conn, T0 + 10 + 604800)
    j = env.job(jid)
    assert j["res_path"] is None and not res.exists() and j["status"] == "completed"
