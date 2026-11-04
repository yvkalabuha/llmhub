"""C5: request_destroy, confirm_zero, mark_leaks, late-ref destroy (kill during create)."""

import sqlite3
from typing import Any

from llmhub.budget_manager.limits import Gate
from llmhub.lifecycle_manager import machine, teardown
from llmhub.lifecycle_manager.safety import is_halted, provider_blocked
from llmhub.provider_adapters.base import RemoteStatus, ResourceKind
from test_c_support import (
    POOL,
    T0,
    FakeCloud,
    add_instance,
    inst,
    make_cfg,
    make_conn,
    trail,
)

CFG = make_cfg()


def _stopping(
    conn: sqlite3.Connection, cloud: FakeCloud, iid: str = "a", **cols: Any
) -> sqlite3.Row:
    r = cloud.add(f"llmhub-{iid}")
    return add_instance(conn, iid, "STOPPING", provider_ref=r.ref, **cols)


async def _destroy(
    conn: sqlite3.Connection, cloud: FakeCloud, now: float, iid: str = "a"
) -> sqlite3.Row:
    await teardown.request_destroy(conn, cloud, inst(conn, iid), now, CFG, "controller")
    return inst(conn, iid)


async def _zero(
    conn: sqlite3.Connection, cloud: FakeCloud, now: float, iid: str = "a", **kw: object
) -> bool:
    return await teardown.confirm_zero(conn, cloud, inst(conn, iid), now, CFG, "controller", **kw)  # type: ignore[arg-type]


# --- request_destroy: STOPPING -> PENDING_DELETE (E19) ---------------------------------------


async def test_destroy_accepted_goes_pending_delete() -> None:
    conn, cloud = make_conn(), FakeCloud()
    _stopping(conn, cloud)
    row = await _destroy(conn, cloud, T0 + 10)
    assert row["state"] == "PENDING_DELETE"
    assert (row["destroy_requested_at"], row["destroy_accepted_at"]) == (T0 + 10, T0 + 10)
    assert trail(conn)[-1] == ("STOPPING", "PENDING_DELETE", "E19")
    assert cloud.resources == {}


async def test_already_gone_is_accepted_too() -> None:
    conn, cloud = make_conn(), FakeCloud()
    add_instance(conn, "a", "STOPPING", provider_ref="r-missing")
    assert (await _destroy(conn, cloud, T0))["state"] == "PENDING_DELETE"
    assert cloud.destroy_calls == ["r-missing"]


async def test_destroy_failures_back_off_then_stop_confirm_timeout() -> None:
    conn, cloud = make_conn(), FakeCloud()
    _stopping(conn, cloud)
    cloud.destroy_fail = 1000
    t = T0
    seen_delays: list[float] = []
    while True:
        row = await _destroy(conn, cloud, t)
        if row["state"] != "STOPPING":
            break
        seen_delays.append(row["next_destroy_at"] - t)
        t = row["next_destroy_at"]
    assert seen_delays[:6] == [5, 10, 20, 40, 80, 120]  # exponential, capped at 120 s
    assert row["state"] == "PENDING_DELETE"
    assert row["destroy_accepted_at"] == t and t - T0 >= CFG.timeouts.stop_confirm_s
    assert trail(conn)[-1] == ("STOPPING", "PENDING_DELETE", "stop_confirm_timeout")
    assert row["last_error"] == "destroy failed"
    assert len(cloud.destroy_calls) == len(seen_delays) + 1  # no calls inside the backoff


async def test_no_destroy_call_before_next_destroy_at() -> None:
    conn, cloud = make_conn(), FakeCloud()
    _stopping(conn, cloud)
    cloud.destroy_fail = 1
    await _destroy(conn, cloud, T0)
    calls = len(cloud.destroy_calls)
    row = await _destroy(conn, cloud, T0 + 3)  # backoff 5 s
    assert row["state"] == "STOPPING" and len(cloud.destroy_calls) == calls
    row = await _destroy(conn, cloud, T0 + 5)
    assert row["state"] == "PENDING_DELETE" and len(cloud.destroy_calls) == calls + 1


async def test_destroy_429_uses_retry_after_and_never_blocks_the_provider() -> None:
    conn, cloud = make_conn(), FakeCloud()
    _stopping(conn, cloud)
    cloud.destroy_fail, cloud.destroy_retry_after = 1, 37.0
    row = await _destroy(conn, cloud, T0)
    assert row["next_destroy_at"] == T0 + 37
    assert provider_blocked(conn, "fake") is None


async def test_stuck_destroy_frees_the_pool_for_a_new_cycle() -> None:
    conn, cloud = make_conn(), FakeCloud()
    _stopping(conn, cloud)
    cloud.destroy_fail = 1000
    await _destroy(conn, cloud, T0)
    assert (
        machine.insert_provisioning(
            conn,
            pool=POOL,
            provider="mock",
            op_id="bbbbbbbbbbbb",
            label="llmhub-bbbbbbbbbbbb-0",
            max_lifetime_at=T0 + 9999,
            now=T0 + 1,
            gate=lambda c: Gate(ok=True),
        )
        is None
    )  # STOPPING still holds the pool
    await _destroy(conn, cloud, T0 + 180)
    assert inst(conn, "a")["state"] == "PENDING_DELETE"
    assert machine.insert_provisioning(
        conn, pool=POOL, provider="mock", op_id="bbbbbbbbbbbb", label="llmhub-bbbbbbbbbbbb-0",
        max_lifetime_at=T0 + 9999, now=T0 + 181, gate=lambda c: Gate(ok=True),
    ) is not None  # fmt: skip


async def test_row_without_ref_destroys_resource_found_by_label() -> None:
    conn, cloud = make_conn(), FakeCloud()
    cloud.add("llmhub-a")  # create succeeded but its answer was lost
    add_instance(conn, "a", "STOPPING", create_called_at=T0)
    row = await _destroy(conn, cloud, T0 + 5)
    assert cloud.resources == {} and row["state"] == "PENDING_DELETE"


# --- confirm_zero --------------------------------------------------------------------------


async def test_zero_confirmed_to_stopped_with_hook_and_reservation_release() -> None:
    conn, cloud = make_conn(), FakeCloud()
    _stopping(conn, cloud)
    await _destroy(conn, cloud, T0)
    conn.execute(
        "INSERT INTO reservations(id, scope, ref_id, instance_id, amount_usd, created_at)"
        " VALUES ('r1','session','x','a',1.0,?)", (T0,),
    )  # fmt: skip
    hook_calls: list[tuple[str, bool]] = []

    def hook(c: sqlite3.Connection, row: sqlite3.Row, now: float) -> None:
        hook_calls.append((row["state"], c.in_transaction))

    assert await _zero(conn, cloud, T0 + 5, on_stopped=hook)
    row = inst(conn, "a")
    assert row["state"] == "STOPPED" and row["zero_confirmed_at"] == T0 + 5
    assert hook_calls == [("STOPPED", True)]
    assert (
        conn.execute("SELECT release_reason FROM reservations").fetchone()[0] == "instance_stopped"
    )
    assert trail(conn)[-1] == ("PENDING_DELETE", "STOPPED", "E20")
    assert not await _zero(conn, cloud, T0 + 6)  # STOPPED: nothing to confirm


async def test_leftover_resource_is_not_zero_and_is_destroyed_again() -> None:
    conn, cloud = make_conn(), FakeCloud()
    cloud.destroy_never = True
    _stopping(conn, cloud)
    await _destroy(conn, cloud, T0)
    cloud.destroy_never = False
    assert not await _zero(conn, cloud, T0 + 1)  # still within next_destroy_at: no new call
    assert len(cloud.resources) == 1
    assert not await _zero(conn, cloud, T0 + 6)  # due: destroy again, gone from now on
    assert cloud.resources == {}
    assert await _zero(conn, cloud, T0 + 7)


async def test_zero_needs_every_resource_kind_partial_volume() -> None:
    conn, cloud = make_conn(), FakeCloud()
    cloud.partial_volume = True  # DESTROY_PARTIAL: the instance goes, its volume stays
    _stopping(conn, cloud)
    await _destroy(conn, cloud, T0)
    assert inst(conn, "a")["state"] == "PENDING_DELETE"
    assert [r.kind for r in cloud.resources.values()] == [ResourceKind.VOLUME]
    assert not await _zero(conn, cloud, T0 + 6)  # an instance list alone would say "zero"
    assert cloud.resources == {}  # the volume was destroyed by its own ref
    assert await _zero(conn, cloud, T0 + 30)
    assert inst(conn, "a")["state"] == "STOPPED"


async def test_list_stale_zero_needs_two_observations_staleness_apart() -> None:
    conn, cloud = make_conn(), FakeCloud()
    cloud.list_staleness_s = 60
    _stopping(conn, cloud)
    await _destroy(conn, cloud, T0)
    assert not await _zero(conn, cloud, T0 + 1)  # first empty list: only noted
    assert not await _zero(conn, cloud, T0 + 60)  # 59 s apart: too early
    assert inst(conn, "a")["state"] == "PENDING_DELETE"
    assert await _zero(conn, cloud, T0 + 61)  # 60 s apart
    assert inst(conn, "a")["state"] == "STOPPED"
    assert (
        conn.execute(
            "SELECT count(*) FROM controller_state WHERE key LIKE 'zero_seen:%'"
        ).fetchone()[0]
        == 0
    )


async def test_stale_list_that_reappears_resets_the_zero_clock() -> None:
    conn, cloud = make_conn(), FakeCloud()
    cloud.list_staleness_s = 60
    _stopping(conn, cloud)
    await _destroy(conn, cloud, T0)
    assert not await _zero(conn, cloud, T0 + 1)
    cloud.add("llmhub-a")  # the resource shows up again
    assert not await _zero(conn, cloud, T0 + 50)
    assert (
        conn.execute(
            "SELECT count(*) FROM controller_state WHERE key LIKE 'zero_seen:%'"
        ).fetchone()[0]
        == 0
    )
    cloud.resources.clear()
    assert not await _zero(conn, cloud, T0 + 100)  # clock restarted here
    assert not await _zero(conn, cloud, T0 + 159)
    assert await _zero(conn, cloud, T0 + 160)


async def test_hidden_but_billing_resource_is_not_zero() -> None:
    conn, cloud = make_conn(), FakeCloud()
    r = cloud.add("llmhub-a", status=RemoteStatus.EXITED, spend=0.3, hidden=True)
    add_instance(conn, "a", "PENDING_DELETE", provider_ref=r.ref, destroy_requested_at=T0)
    assert not await _zero(conn, cloud, T0 + 1)
    assert cloud.destroy_calls == [r.ref]
    assert await _zero(conn, cloud, T0 + 10)


async def test_provider_list_error_is_not_zero() -> None:
    conn, cloud = make_conn(), FakeCloud()
    add_instance(conn, "a", "PENDING_DELETE", provider_ref="r1", destroy_requested_at=T0)
    cloud.list_fail = True
    assert not await _zero(conn, cloud, T0 + 5)
    assert inst(conn, "a")["state"] == "PENDING_DELETE"


async def test_row_without_ref_waits_create_visibility_grace() -> None:
    conn, cloud = make_conn(), FakeCloud()
    add_instance(conn, "a", "PENDING_DELETE", create_called_at=T0, destroy_requested_at=T0)
    assert not await _zero(conn, cloud, T0 + 119)  # list_owned may lag behind create
    assert await _zero(conn, cloud, T0 + 120)
    # a row whose create was never called has nothing to wait for
    add_instance(conn, "b", "PENDING_DELETE", pool="q", destroy_requested_at=T0)
    assert await _zero(conn, cloud, T0, iid="b")


# --- leaks ----------------------------------------------------------------------------------


async def test_destroy_never_leaks_after_15_minutes_and_halts() -> None:
    conn, cloud = make_conn(), FakeCloud()
    cloud.destroy_never = True
    _stopping(conn, cloud, provider="mock")
    t = T0
    await _destroy(conn, cloud, t)
    assert inst(conn, "a")["state"] == "PENDING_DELETE"
    while t < T0 + 900:
        t = min(float(inst(conn, "a")["next_destroy_at"]), T0 + 900)
        assert not await _zero(conn, cloud, t)
        assert teardown.mark_leaks(conn, t, CFG, "controller") == []  # 900 s exactly: not yet
    assert len(cloud.destroy_calls) > 5 and len(cloud.resources) == 1
    assert teardown.mark_leaks(conn, T0 + 901, CFG, "controller") == ["a"]
    assert inst(conn, "a")["state"] == "LEAKED"
    assert trail(conn)[-1] == ("PENDING_DELETE", "LEAKED", "leak")
    inc = conn.execute("SELECT * FROM incidents WHERE kind='leak'").fetchall()
    assert len(inc) == 1 and inc[0]["severity"] == "critical" and inc[0]["ref"] == "a"
    assert provider_blocked(conn, "mock") == "leak" and is_halted(conn)
    assert teardown.mark_leaks(conn, T0 + 950, CFG, "controller") == []  # idempotent
    # the destroy keeps being retried; once zero is seen the row is STOPPED but HALT stays
    cloud.destroy_never = False
    t2 = T0 + 1200
    assert not await _zero(conn, cloud, t2)  # destroys the leftover
    assert await _zero(conn, cloud, t2 + 1)
    assert inst(conn, "a")["state"] == "STOPPED"
    assert trail(conn)[-1] == ("LEAKED", "STOPPED", "E22")
    assert is_halted(conn) and provider_blocked(conn, "mock") == "leak"


def test_mark_leaks_uses_destroy_requested_at_not_state_since() -> None:
    conn = make_conn()
    add_instance(conn, "a", "PENDING_DELETE", state_since=T0 + 800, destroy_requested_at=T0)
    add_instance(
        conn, "b", "PENDING_DELETE", pool="q", state_since=T0, destroy_requested_at=T0 + 500
    )
    assert teardown.mark_leaks(conn, T0 + 901, CFG, "reaper") == ["a"]
    assert trail(conn, "a")[-1] == ("PENDING_DELETE", "LEAKED", "leak")
    assert (
        conn.execute("SELECT actor FROM transitions WHERE instance_id='a'").fetchone()[0]
        == "reaper"
    )


# --- set_provider_ref + immediate destroy ---------------------------------------------------


async def test_late_ref_after_kill_is_destroyed_at_once() -> None:
    conn, cloud = make_conn(), FakeCloud()
    add_instance(conn, "a", "PROVISIONING", create_called_at=T0)
    machine.kill_to_stopping(conn, T0 + 5)  # kill while create hangs
    res = await cloud.create(None, "llmhub-a")  # type: ignore[arg-type]
    assert await teardown.adopt_ref_or_destroy(conn, cloud, "a", res, T0 + 70) is False
    assert cloud.destroy_calls == [res.ref] and cloud.resources == {}
    assert inst(conn, "a")["provider_ref"] == res.ref  # kept for teardown bookkeeping
    assert inst(conn, "a")["state"] == "STOPPING"
    await _destroy(conn, cloud, T0 + 71)
    assert await _zero(conn, cloud, T0 + 72)


async def test_ref_in_provisioning_is_adopted_not_destroyed() -> None:
    conn, cloud = make_conn(), FakeCloud()
    add_instance(conn, "a", "PROVISIONING")
    res = await cloud.create(None, "llmhub-a")  # type: ignore[arg-type]
    assert await teardown.adopt_ref_or_destroy(conn, cloud, "a", res, T0)
    assert cloud.destroy_calls == [] and inst(conn, "a")["provider_ref"] == res.ref


async def test_late_ref_destroy_failure_records_incident_instead_of_raising() -> None:
    conn, cloud = make_conn(), FakeCloud()
    add_instance(conn, "a", "PENDING_DELETE", destroy_requested_at=T0)
    res = await cloud.create(None, "llmhub-a")  # type: ignore[arg-type]
    cloud.destroy_fail = 1
    assert await teardown.adopt_ref_or_destroy(conn, cloud, "a", res, T0 + 1) is False
    inc = conn.execute("SELECT kind, severity FROM incidents").fetchall()
    assert [(i[0], i[1]) for i in inc] == [("late_ref_destroy_failed", "high")]
    # confirm_zero still finds the resource by label and removes it
    assert not await _zero(conn, cloud, T0 + 2)
    assert await _zero(conn, cloud, T0 + 20)
