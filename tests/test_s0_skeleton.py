import sqlite3
from pathlib import Path

import pytest

from harness import TEST_SECRET, Harness, make_job
from llmhub.lifecycle_manager.machine import transition
from llmhub.lifecycle_manager.states import (
    BILLABLE,
    LIVE_IN_INDEX,
    TRANSITIONS,
    IllegalTransition,
    State,
    check_edge,
)
from llmhub.provider_adapters.labels import make_label, verify_label

HAPPY = [
    ("STOPPED", "PROVISIONING"),
    ("PROVISIONING", "BOOTING"),
    ("BOOTING", "LOADING_MODEL"),
    ("LOADING_MODEL", "READY"),
    ("READY", "BUSY"),
    ("BUSY", "IDLE"),
    ("IDLE", "STOPPING"),
    ("STOPPING", "PENDING_DELETE"),
    ("PENDING_DELETE", "STOPPED"),
]


async def test_happy_path(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    t0 = h.clock.now()
    h.arrive_at(t0, make_job(0))
    await h.run_until(t0 + 3600)

    rows = h.transitions()
    assert [(a, b) for a, b, _ in rows] == HAPPY
    assert [r for _, _, r in rows] == [
        "E1", "E2", "E4", "E6", "E8", "E12", "idle_timeout", "E19", "E20",
    ]  # fmt: skip

    job = h.conn.execute("SELECT * FROM jobs").fetchone()
    assert job["status"] == "completed" and job["tokens_out"] == 780
    assert job["attempts"] == 1 and job["res_path"] is not None
    assert h.conn.execute("SELECT count(*) FROM leases WHERE closed_at IS NULL").fetchone()[0] == 0

    inst = h.conn.execute("SELECT * FROM instances").fetchone()
    assert inst["state"] == "STOPPED" and inst["zero_confirmed_at"] is not None
    assert verify_label(inst["label"], TEST_SECRET)
    assert h.provider.create_calls == 1
    assert h.provider.live_refs() == []
    assert h.provider.billed_total() > 0


async def test_deterministic(tmp_path: Path) -> None:
    results = []
    for i in range(2):
        d = tmp_path / str(i)
        d.mkdir()
        h = Harness(d)
        h.arrive_at(h.clock.now(), make_job(0))
        await h.run_until(h.clock.now() + 3600)
        results.append([(a, b) for a, b, _ in h.transitions()])
    assert results[0] == results[1]


async def test_second_job_reuses_instance(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    t0 = h.clock.now()
    h.arrive_at(t0, make_job(0))
    h.arrive_at(t0 + 200, make_job(1))  # arrives while the first instance idles
    await h.run_until(t0 + 3600)
    assert h.provider.create_calls == 1
    done = h.conn.execute("SELECT count(*) FROM jobs WHERE status='completed'").fetchone()[0]
    assert done == 2


def test_transition_table() -> None:
    assert {e.trigger for e in TRANSITIONS} == {f"E{i}" for i in range(1, 23)}
    assert len(TRANSITIONS) == 28
    assert State.STOPPED not in LIVE_IN_INDEX and State.ERROR in LIVE_IN_INDEX
    assert State.STOPPED not in BILLABLE and State.LEAKED in BILLABLE
    assert check_edge(State.READY, State.BUSY, "E8").trigger == "E8"


@pytest.mark.parametrize(
    ("src", "dst"),
    [
        (State.STOPPED, State.BUSY),
        (State.PENDING_DELETE, State.READY),
        (State.STOPPING, State.ERROR),
    ],
)
def test_illegal_edges(src: State, dst: State) -> None:
    with pytest.raises(IllegalTransition):
        check_edge(src, dst, "E8")


async def test_cas_loses_race(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.submit(make_job(0))
    await h.controller.step()
    inst = h.conn.execute("SELECT * FROM instances").fetchone()
    assert inst["state"] == "PROVISIONING"
    ok = transition(h.conn, inst["id"], State.BOOTING, State.LOADING_MODEL, "E4", 0.0, "reaper")
    assert ok is False
    assert h.conn.execute("SELECT count(*) FROM transitions").fetchone()[0] == 1


def test_label_roundtrip() -> None:
    label = make_label("0123456789ab", TEST_SECRET)
    assert verify_label(label, TEST_SECRET)
    assert not verify_label(label, b"other-secret-other-secret-other-secret")
    assert not verify_label("llmhub-0123456789ab-00000000", TEST_SECRET)
    assert not verify_label("foreign", TEST_SECRET)


def test_unique_pool_blocks_second_launch(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.submit(make_job(0))
    from llmhub.budget_manager.limits import Gate
    from llmhub.lifecycle_manager.machine import insert_provisioning

    a = insert_provisioning(h.conn, pool="p1", provider="mock", op_id="a" * 12, label="l1",
                            max_lifetime_at=1.0, now=0.0, gate=lambda c: Gate(True))  # fmt: skip
    b = insert_provisioning(h.conn, pool="p1", provider="mock", op_id="b" * 12, label="l2",
                            max_lifetime_at=1.0, now=0.0, gate=lambda c: Gate(True))  # fmt: skip
    assert a is not None and b is None
    c = insert_provisioning(h.conn, pool="p2", provider="mock", op_id="c" * 12, label="l3",
                            max_lifetime_at=1.0, now=0.0, gate=lambda c: Gate(False))  # fmt: skip
    assert c is None
    assert isinstance(h.conn, sqlite3.Connection)
