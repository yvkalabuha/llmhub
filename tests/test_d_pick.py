from pathlib import Path

from d_fixtures import T0, Env, spec
from llmhub.task_queue import queue
from llmhub.task_queue.models import Priority


def add(env: Env, n: int, client: str, prio: Priority = Priority.NORMAL) -> str:
    return queue.submit(
        env.conn, env.bodies, spec(n, client=client, prio=prio), T0 + n, env.cfg
    ).job_id


def drain(env: Env, count: int) -> list[str]:
    out = []
    for i in range(count):
        row = queue.pick_next(env.conn, "p1", T0 + 100 + i)
        assert row is not None
        assert queue.mark_running(env.conn, row["id"], "i1", "w", T0 + 100 + i)
        out.append(row["client_id"])
    return out


def test_priority_order(tmp_path: Path) -> None:
    env = Env(tmp_path)
    add(env, 1, "client-1", Priority.LOW)
    add(env, 2, "client-1", Priority.NORMAL)
    add(env, 3, "client-1", Priority.URGENT)
    got = [queue.pick_next(env.conn, "p1", T0)["priority"]]  # type: ignore[index]
    assert got == ["urgent"]
    assert [r["priority"] for r in queue.pool_queue(env.conn, "p1")] == ["urgent", "normal", "low"]


def test_round_robin_three_clients(tmp_path: Path) -> None:
    env = Env(tmp_path)
    for n, c in enumerate(["client-1", "client-1", "client-1", "c2", "c3"]):
        add(env, n, c)
    assert drain(env, 5) == ["client-1", "c2", "c3", "client-1", "client-1"]


def test_priority_beats_round_robin(tmp_path: Path) -> None:
    env = Env(tmp_path)
    add(env, 1, "client-1")
    add(env, 2, "c2")
    assert drain(env, 1) == ["client-1"]
    add(env, 3, "client-1", Priority.URGENT)
    assert drain(env, 1) == ["client-1"]  # urgent first although c1 ran last


def test_empty_queue(tmp_path: Path) -> None:
    env = Env(tmp_path)
    assert queue.pick_next(env.conn, "p1", T0) is None
