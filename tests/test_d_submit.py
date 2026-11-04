import threading
from pathlib import Path

import pytest

from d_fixtures import T0, Env, spec
from llmhub.task_queue import queue
from llmhub.task_queue.models import IdempotencyConflict, JobStatus


def sub(env: Env, s, now: float = T0):
    return queue.submit(env.conn, env.bodies, s, now, env.cfg)


def test_explicit_key_same_body_same_job(tmp_path: Path) -> None:
    env = Env(tmp_path)
    a = sub(env, spec(1, key="k"))
    b = sub(env, spec(1, key="k"), T0 + 5)
    assert a.created and not b.created and a.job_id == b.job_id
    assert env.conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1
    assert len(list((tmp_path / "bodies").glob("*.req"))) == 1  # duplicate body removed


def test_explicit_key_other_body_conflict(tmp_path: Path) -> None:
    env = Env(tmp_path)
    a = sub(env, spec(1, key="k"))
    with pytest.raises(IdempotencyConflict) as e:
        sub(env, spec(2, key="k"))
    assert e.value.job_id == a.job_id
    assert len(list((tmp_path / "bodies").glob("*.req"))) == 1


def test_explicit_key_scoped_per_client(tmp_path: Path) -> None:
    env = Env(tmp_path)
    a = sub(env, spec(1, key="k"))
    b = sub(env, spec(2, client="c2", key="k"))
    assert a.job_id != b.job_id


def test_explicit_key_expires_after_7_days(tmp_path: Path) -> None:
    env = Env(tmp_path)
    a = sub(env, spec(1, key="k"))
    day = 86400
    same = sub(env, spec(1, key="k"), T0 + 7 * day - 1)
    assert same.job_id == a.job_id
    b = sub(env, spec(1, key="k"), T0 + 7 * day + 1)
    assert b.created and b.job_id != a.job_id
    assert env.job(a.job_id)["idem_key"] is None


def test_implicit_dedup_while_queued_and_running(tmp_path: Path) -> None:
    env = Env(tmp_path)
    a = sub(env, spec(1))
    assert not sub(env, spec(1), T0 + 3600).created  # still queued: no time limit
    queue.mark_running(env.conn, a.job_id, "i1", "w", T0 + 4000)
    b = sub(env, spec(1), T0 + 5000)
    assert not b.created and b.status is JobStatus.RUNNING


def test_implicit_dedup_window_after_completion(tmp_path: Path) -> None:
    from llmhub.cloud_manager.inference import JobRunResult

    env = Env(tmp_path)
    a = sub(env, spec(1))
    queue.mark_running(env.conn, a.job_id, "i1", "w", T0)
    queue.complete(env.conn, a.job_id, JobRunResult(1, 1, "stop", b""), 0.0, T0 + 10)
    assert sub(env, spec(1), T0 + 10 + 900).job_id == a.job_id
    again = sub(env, spec(1), T0 + 10 + 901)
    assert again.created and again.job_id != a.job_id


def test_implicit_dedup_not_across_clients(tmp_path: Path) -> None:
    env = Env(tmp_path)
    a = sub(env, spec(1))
    b = sub(env, spec(1, client="c2"))
    assert b.created and a.job_id != b.job_id


def test_submit_creates_implicit_lease(tmp_path: Path) -> None:
    env = Env(tmp_path)
    a = sub(env, spec(1))
    assert env.lease(a.job_id)["kind"] == "implicit"
    assert env.job(a.job_id)["max_attempts"] == env.cfg.retry.job_max_attempts


def test_ten_threads_one_key_one_job(tmp_path: Path) -> None:
    env = Env(tmp_path)
    from llmhub.persistence.db import connect

    results: list[str] = []
    errors: list[BaseException] = []

    def work() -> None:
        try:
            conn = connect(env.path, busy_timeout_ms=5000)
            r = queue.submit(conn, env.bodies, spec(1, key="same"), T0, env.cfg)
            results.append(r.job_id)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    ts = [threading.Thread(target=work) for _ in range(10)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors
    assert len(set(results)) == 1 and len(results) == 10
    assert env.conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1
    assert len(list((tmp_path / "bodies").glob("*.req"))) == 1
