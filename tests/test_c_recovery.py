"""C6: every branch of reconcile_on_start (plan section 8) + the live-duplicate rule."""

import sqlite3

from llmhub.lifecycle_manager import leases
from llmhub.lifecycle_manager.recovery import ReconcileReport, reconcile_on_start
from llmhub.provider_adapters.base import RemoteStatus
from test_c_support import (
    POOL,
    T0,
    FakeCloud,
    FakeInference,
    add_instance,
    add_job,
    inst,
    make_cfg,
    make_conn,
    trail,
)

CFG = make_cfg()
NOW = T0 + 1000


async def _recon(
    conn: sqlite3.Connection, cloud: FakeCloud, inf: FakeInference | None = None
) -> ReconcileReport:
    return await reconcile_on_start(
        conn, {"fake": cloud}, {"fake": inf or FakeInference()}, b"s" * 32, NOW, CFG, "boot-2"
    )  # type: ignore[arg-type]


async def test_records_boot_id() -> None:
    conn = make_conn()
    await _recon(conn, FakeCloud())
    assert (
        conn.execute("SELECT value FROM controller_state WHERE key='boot_id'").fetchone()[0]
        == "boot-2"
    )


# --- PROVISIONING ------------------------------------------------------------------------------


async def test_provisioning_without_ref_adopts_running_resource_by_label() -> None:
    conn, cloud = make_conn(), FakeCloud()
    r = cloud.add("llmhub-a", host_id="h7")
    add_instance(conn, "a", "PROVISIONING", create_called_at=T0, attempts=1)
    rep = await _recon(conn, cloud)
    row = inst(conn, "a")
    assert (row["state"], row["provider_ref"], row["host_id"]) == ("BOOTING", r.ref, "h7")
    assert row["endpoint"] == f"fake://{r.ref}"
    assert rep.adopted == ["a"] and cloud.destroy_calls == []


async def test_provisioning_adopts_pending_resource_and_stays() -> None:
    conn, cloud = make_conn(), FakeCloud()
    r = cloud.add("llmhub-a", status=RemoteStatus.PENDING)
    add_instance(conn, "a", "PROVISIONING", create_called_at=T0, attempts=1)
    await _recon(conn, cloud)
    assert (inst(conn, "a")["state"], inst(conn, "a")["provider_ref"]) == ("PROVISIONING", r.ref)


async def test_provisioning_unknown_outcome_retries_create_with_same_label() -> None:
    conn, cloud = make_conn(), FakeCloud()
    add_instance(conn, "a", "PROVISIONING", create_called_at=T0, attempts=1)
    await _recon(conn, cloud)
    row = inst(conn, "a")
    assert row["state"] == "PROVISIONING" and row["create_called_at"] is None
    assert row["label"] == "llmhub-a" and row["attempts"] == 1


async def test_provisioning_unknown_outcome_at_max_attempts_errors_and_stops() -> None:
    conn, cloud = make_conn(), FakeCloud()
    add_instance(conn, "a", "PROVISIONING", create_called_at=T0, attempts=2)
    rep = await _recon(conn, cloud)
    assert inst(conn, "a")["state"] == "STOPPING" and rep.errored == ["a"]
    assert [t[1] for t in trail(conn)] == ["ERROR", "STOPPING"]
    assert trail(conn)[0][2] == "create_unknown_max_attempts"


async def test_provisioning_inside_visibility_grace_or_never_called_is_untouched() -> None:
    conn, cloud = make_conn(), FakeCloud()
    add_instance(conn, "a", "PROVISIONING", create_called_at=NOW - 30, attempts=1)
    add_instance(conn, "b", "PROVISIONING", pool="q")
    await _recon(conn, cloud)
    assert (
        inst(conn, "a")["create_called_at"] == NOW - 30
        and inst(conn, "a")["state"] == "PROVISIONING"
    )
    assert inst(conn, "b")["state"] == "PROVISIONING"


async def test_provisioning_with_terminated_resource_errors() -> None:
    conn, cloud = make_conn(), FakeCloud()
    cloud.add("llmhub-a", status=RemoteStatus.TERMINATED)
    add_instance(conn, "a", "PROVISIONING", create_called_at=T0, attempts=1)
    await _recon(conn, cloud)
    assert inst(conn, "a")["state"] == "STOPPING"


# --- BOOTING / LOADING_MODEL -------------------------------------------------------------------


async def test_booting_and_loading_found_continue_with_original_timers() -> None:
    conn, cloud = make_conn(), FakeCloud()
    r1, r2 = cloud.add("llmhub-a"), cloud.add("llmhub-b")
    add_instance(conn, "a", "BOOTING", provider_ref=r1.ref, state_since=T0 + 100, endpoint="e")
    add_instance(conn, "b", "LOADING_MODEL", pool="q", provider_ref=r2.ref, state_since=T0 + 200)
    rep = await _recon(conn, cloud)
    assert (inst(conn, "a")["state"], inst(conn, "a")["state_since"]) == ("BOOTING", T0 + 100)
    assert (inst(conn, "b")["state"], inst(conn, "b")["state_since"]) == ("LOADING_MODEL", T0 + 200)
    assert sorted(rep.adopted) == ["a", "b"]


async def test_booting_and_loading_not_found_errors_and_blacklists_host() -> None:
    conn, cloud = make_conn(), FakeCloud()
    add_instance(conn, "a", "BOOTING", provider_ref="gone1", host_id="h1", provider="fake")
    add_instance(conn, "b", "LOADING_MODEL", pool="q", provider_ref="gone2", host_id="h2")
    await _recon(conn, cloud)
    assert {inst(conn, "a")["state"], inst(conn, "b")["state"]} == {"STOPPING"}
    assert [t[:2] for t in trail(conn, "a")] == [("BOOTING", "ERROR"), ("ERROR", "STOPPING")]
    assert [t[:2] for t in trail(conn, "b")] == [("LOADING_MODEL", "ERROR"), ("ERROR", "STOPPING")]
    assert {r["host_id"] for r in conn.execute("SELECT host_id FROM host_blacklist")} == {
        "h1",
        "h2",
    }


# --- READY / BUSY / IDLE -----------------------------------------------------------------------


async def test_ready_healthy_no_work_goes_idle_with_fresh_timer() -> None:
    conn, cloud = make_conn(), FakeCloud()
    r = cloud.add("llmhub-a")
    add_instance(conn, "a", "READY", provider_ref=r.ref, endpoint="e")
    await _recon(conn, cloud)
    row = inst(conn, "a")
    assert row["state"] == "IDLE" and row["idle_since"] == NOW


async def test_ready_healthy_with_queue_or_lease_stays_ready_for_dispatch() -> None:
    conn, cloud = make_conn(), FakeCloud()
    r1, r2 = cloud.add("llmhub-a"), cloud.add("llmhub-b")
    add_instance(conn, "a", "READY", provider_ref=r1.ref, endpoint="e")
    add_job(conn, "j1", "queued")
    add_instance(conn, "b", "READY", pool="q", provider_ref=r2.ref, endpoint="e")
    leases.create_explicit(conn, holder="agent", client_id=None, pool="q", now=NOW)
    await _recon(conn, cloud)
    assert inst(conn, "a")["state"] == "READY" and inst(conn, "b")["state"] == "READY"


async def test_idle_healthy_resets_idle_timer() -> None:
    conn, cloud = make_conn(), FakeCloud()
    r = cloud.add("llmhub-a")
    add_instance(conn, "a", "IDLE", provider_ref=r.ref, endpoint="e", idle_since=T0)
    await _recon(conn, cloud)
    assert inst(conn, "a")["state"] == "IDLE" and inst(conn, "a")["idle_since"] == NOW


async def test_busy_healthy_old_boot_job_is_requeued_without_attempt() -> None:
    conn, cloud = make_conn(), FakeCloud()
    r = cloud.add("llmhub-a")
    add_instance(conn, "a", "BUSY", provider_ref=r.ref, endpoint="e")
    add_job(conn, "j1", "running", instance_id="a", worker_id="boot-1", attempts=1)
    rep = await _recon(conn, cloud)
    j = conn.execute("SELECT * FROM jobs").fetchone()
    assert (j["status"], j["attempts"], j["restarts"], j["instance_id"]) == ("queued", 0, 1, None)
    assert inst(conn, "a")["state"] == "BUSY"  # queued work remains: no GPU loss, no extra resource
    assert rep.requeued_jobs == ["j1"] and cloud.destroy_calls == [] and len(cloud.resources) == 1


async def test_job_of_the_current_boot_is_left_alone() -> None:
    conn, cloud = make_conn(), FakeCloud()
    add_job(conn, "j1", "running", worker_id="boot-2", attempts=1, lease=False)
    add_job(conn, "j2", "running", worker_id="boot-1", attempts=1, lease=False)
    rep = await _recon(conn, cloud)
    st = {r["id"]: r["status"] for r in conn.execute("SELECT id, status FROM jobs")}
    assert st == {"j1": "running", "j2": "queued"} and rep.requeued_jobs == ["j2"]


async def test_busy_resource_missing_at_restart_refunds_the_attempt_and_stops() -> None:
    """L4 (coordinator 2026-11-04): a loss found by the restart reconcile is not an attempt."""
    conn, cloud = make_conn(), FakeCloud()
    add_instance(conn, "a", "BUSY", provider_ref="gone", endpoint="e")
    add_job(conn, "j1", "running", instance_id="a", worker_id="boot-1", attempts=1)
    rep = await _recon(conn, cloud)
    j = conn.execute("SELECT * FROM jobs").fetchone()
    assert (j["status"], j["attempts"], j["restarts"]) == ("queued", 0, 1)  # restart, refunded
    assert rep.requeued_jobs == ["j1"]
    assert inst(conn, "a")["state"] == "STOPPING"
    assert [t[:2] for t in trail(conn)] == [("BUSY", "ERROR"), ("ERROR", "STOPPING")]


async def test_ready_resource_missing_goes_e10_then_stopping() -> None:
    conn, cloud = make_conn(), FakeCloud()
    add_instance(conn, "a", "READY", provider_ref="gone", endpoint="e")
    await _recon(conn, cloud)
    assert [t[:2] for t in trail(conn)] == [("READY", "ERROR"), ("ERROR", "STOPPING")]


async def test_health_false_goes_error_then_stopping_and_destroys_later() -> None:
    conn, cloud, inf = make_conn(), FakeCloud(), FakeInference()
    r = cloud.add("llmhub-a")
    add_instance(conn, "a", "IDLE", provider_ref=r.ref, endpoint="e")
    inf.healthy = False
    rep = await _recon(conn, cloud, inf)
    assert inst(conn, "a")["state"] == "STOPPING" and rep.errored == ["a"]
    assert trail(conn)[0][2] == "health_false"
    assert inst(conn, "a")["last_error"] == "health_false"


async def test_exited_resource_is_not_alive() -> None:
    conn, cloud = make_conn(), FakeCloud()
    r = cloud.add("llmhub-a", status=RemoteStatus.EXITED)
    add_instance(conn, "a", "IDLE", provider_ref=r.ref, endpoint="e")
    await _recon(conn, cloud)
    assert inst(conn, "a")["state"] == "STOPPING"


# --- teardown states, ERROR ----------------------------------------------------------------------


async def test_teardown_states_are_left_for_normal_steps_and_error_goes_stopping() -> None:
    conn, cloud = make_conn(), FakeCloud()
    add_instance(conn, "a", "STOPPING", provider_ref="r1")
    add_instance(conn, "b", "PENDING_DELETE", destroy_requested_at=T0)
    add_instance(conn, "c", "LEAKED", destroy_requested_at=T0)
    add_instance(conn, "d", "ERROR", pool="q")
    await _recon(conn, cloud)
    assert [inst(conn, i)["state"] for i in "abcd"] == [
        "STOPPING",
        "PENDING_DELETE",
        "LEAKED",
        "STOPPING",
    ]
    assert cloud.destroy_calls == []


# --- live duplicate rule -----------------------------------------------------------------------


async def test_live_duplicate_with_other_ref_is_destroyed_with_incident() -> None:
    conn, cloud = make_conn(), FakeCloud()
    keep = cloud.add("llmhub-a", created_at=T0)
    dup = cloud.add("llmhub-a", created_at=T0 + 90)  # late result of an unknown-outcome create
    add_instance(conn, "a", "BUSY", provider_ref=keep.ref, endpoint="e")
    rep = await _recon(conn, cloud)
    assert list(cloud.resources) == [keep.ref] and rep.destroyed == [dup.ref]
    assert inst(conn, "a")["provider_ref"] == keep.ref and inst(conn, "a")["state"] == "IDLE"
    inc = conn.execute("SELECT * FROM incidents WHERE kind='duplicate_resource'").fetchall()
    assert len(inc) == 1 and inc[0]["severity"] == "high" and inc[0]["ref"] == "a"
    await _recon(conn, cloud)  # idempotent: nothing more to do, no second incident
    assert conn.execute("SELECT count(*) FROM incidents").fetchone()[0] == 1


async def test_duplicates_with_null_ref_adopt_oldest_destroy_rest() -> None:
    conn, cloud = make_conn(), FakeCloud()
    first = cloud.add("llmhub-a", created_at=T0)
    cloud.add("llmhub-a", created_at=T0 + 100)
    cloud.add("llmhub-a", created_at=T0 + 200)
    add_instance(conn, "a", "PROVISIONING", create_called_at=T0, attempts=2)
    await _recon(conn, cloud)
    assert list(cloud.resources) == [first.ref]
    assert inst(conn, "a")["provider_ref"] == first.ref
    assert (
        conn.execute("SELECT count(*) FROM incidents WHERE kind='duplicate_resource'").fetchone()[0]
        == 2
    )


async def test_ghost_ref_moves_to_the_resource_found_by_label() -> None:
    conn, cloud = make_conn(), FakeCloud()
    real = cloud.add("llmhub-a")
    add_instance(conn, "a", "BOOTING", provider_ref="ghost-ref")
    await _recon(conn, cloud)
    row = inst(conn, "a")
    assert row["provider_ref"] == real.ref and row["state"] == "BOOTING"
    assert cloud.destroy_calls == []  # the ghost does not exist, the real one is not a duplicate


async def test_foreign_and_unowned_resources_are_never_touched() -> None:
    conn, cloud = make_conn(), FakeCloud()
    foreign = cloud.add("someone-elses-box")
    orphan = cloud.add(
        "llmhub-zzz"
    )  # llmhub label without a row: the reaper decides, not reconcile
    r = cloud.add("llmhub-a")
    add_instance(conn, "a", "IDLE", provider_ref=r.ref, endpoint="e")
    await _recon(conn, cloud)
    assert {foreign.ref, orphan.ref, r.ref} == set(cloud.resources) and cloud.destroy_calls == []


async def test_stopped_rows_are_ignored() -> None:
    conn, cloud = make_conn(), FakeCloud()
    cloud.add("llmhub-a")
    add_instance(conn, "a", "STOPPED")
    await _recon(conn, cloud)
    assert cloud.destroy_calls == []


async def test_failed_list_changes_nothing_and_records_incident() -> None:
    conn, cloud = make_conn(), FakeCloud()
    cloud.list_fail = True
    add_instance(conn, "a", "READY", provider_ref="r1", endpoint="e")
    add_instance(conn, "b", "BOOTING", pool="q", provider_ref="r2")
    rep = await _recon(conn, cloud)
    assert inst(conn, "a")["state"] == "READY" and inst(conn, "b")["state"] == "BOOTING"
    assert rep == ReconcileReport()
    assert conn.execute("SELECT kind FROM incidents").fetchone()[0] == "reconcile_list_failed"


async def test_leases_that_expired_during_downtime_are_closed() -> None:
    conn, cloud = make_conn(), FakeCloud()
    lid = leases.create_explicit(conn, holder="a", client_id=None, pool=POOL, ttl_s=60, now=T0)
    await _recon(conn, cloud)
    assert (
        conn.execute("SELECT close_reason FROM leases WHERE id=?", (lid,)).fetchone()[0]
        == "expired"
    )
