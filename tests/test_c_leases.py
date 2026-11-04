"""C3: lease kinds, TTL/heartbeat, expiry, drain, live counting."""

import pytest

from llmhub.configuration.models import LeaseConfig
from llmhub.lifecycle_manager import leases
from llmhub.lifecycle_manager.leases import LeaseClosed, LeaseError, LeaseExpired, LeaseKind
from test_c_support import POOL, T0, add_instance, add_job, inst, make_conn


def test_implicit_lives_with_job_and_closes_for_job() -> None:
    conn = make_conn()
    add_job(conn, "j1", lease=False)
    lid = leases.create_implicit(conn, "j1", POOL, "c1", T0)
    row = conn.execute("SELECT * FROM leases WHERE id=?", (lid,)).fetchone()
    assert row["kind"] == "implicit" and row["expires_at"] is None
    assert leases.live_count(conn, POOL, T0 + 10**6) == 1  # never expires by time
    leases.close_for_job(conn, "j1", T0 + 5)
    assert leases.live_count(conn, POOL, T0 + 6) == 0
    leases.close_for_job(conn, "j1", T0 + 9)  # idempotent
    assert conn.execute("SELECT closed_at FROM leases WHERE id=?", (lid,)).fetchone()[0] == T0 + 5


def test_implicit_cannot_be_renewed() -> None:
    conn = make_conn()
    add_job(conn, "j1")
    with pytest.raises(LeaseError):
        leases.renew(conn, "l-j1", T0)


def test_explicit_default_ttl_and_range() -> None:
    conn = make_conn()
    lid = leases.create_explicit(conn, holder="agent", client_id="c1", pool=POOL, now=T0)
    row = conn.execute("SELECT * FROM leases WHERE id=?", (lid,)).fetchone()
    assert row["kind"] == "explicit" and row["ttl_s"] == 600 and row["expires_at"] == T0 + 600
    for bad in (29, 3601):
        with pytest.raises(LeaseError):
            leases.create_explicit(conn, holder="a", client_id=None, pool=POOL, ttl_s=bad, now=T0)
    leases.create_explicit(conn, holder="a", client_id=None, pool=POOL, ttl_s=30, now=T0)
    leases.create_explicit(conn, holder="a", client_id=None, pool=POOL, ttl_s=3600, now=T0)


def test_prewarm_kind() -> None:
    conn = make_conn()
    lid = leases.create_explicit(conn, holder="a", client_id=None, pool=POOL, now=T0, prewarm=True)
    assert conn.execute("SELECT kind FROM leases WHERE id=?", (lid,)).fetchone()[0] == "prewarm"
    assert leases.live_count(conn, POOL, T0, [LeaseKind.PREWARM]) == 1
    assert leases.live_count(conn, POOL, T0, [LeaseKind.EXPLICIT]) == 0


def test_renew_extends_and_silent_agent_20_min_with_heartbeat_stays_alive() -> None:
    conn = make_conn()
    lid = leases.create_explicit(conn, holder="a", client_id=None, pool=POOL, ttl_s=600, now=T0)
    t = T0
    for _ in range(6):  # heartbeat every 200 s for 20 minutes (no HTTP traffic at all)
        t += 200
        assert leases.renew(conn, lid, t) == t + 600
        assert leases.expire_due(conn, t) == []
        assert leases.live_count(conn, POOL, t) == 1
    assert t - T0 == 1200


def test_no_heartbeat_expires_with_incident() -> None:
    conn = make_conn()
    lid = leases.create_explicit(conn, holder="a", client_id=None, pool=POOL, ttl_s=600, now=T0)
    assert leases.live_count(conn, POOL, T0 + 599) == 1
    assert leases.live_count(conn, POOL, T0 + 600) == 0  # expires_at > now is required
    assert leases.expire_due(conn, T0 + 601) == [lid]
    assert leases.expire_due(conn, T0 + 700) == []  # already closed
    inc = conn.execute("SELECT * FROM incidents").fetchall()
    assert [(i["kind"], i["severity"], i["ref"]) for i in inc] == [("lease_expired", "info", lid)]
    assert (
        conn.execute("SELECT close_reason FROM leases WHERE id=?", (lid,)).fetchone()[0]
        == "expired"
    )


def test_renew_expired_or_closed_raises() -> None:
    conn = make_conn()
    lid = leases.create_explicit(conn, holder="a", client_id=None, pool=POOL, ttl_s=60, now=T0)
    with pytest.raises(LeaseExpired):
        leases.renew(conn, lid, T0 + 61)
    leases.close(conn, lid, T0 + 10)
    with pytest.raises(LeaseClosed):
        leases.renew(conn, lid, T0 + 20)
    with pytest.raises(LeaseClosed):
        leases.renew(conn, "nope", T0)


def test_hold_cap_8h_and_extends_instance_lifetime() -> None:
    conn = make_conn()
    add_instance(conn, "a", "READY", max_lifetime_at=T0 + 10800)
    with pytest.raises(LeaseError):
        leases.create_hold(conn, holder="admin", pool=POOL, ttl_s=28801, now=T0)
    with pytest.raises(LeaseError):
        leases.create_hold(conn, holder="admin", pool=POOL, ttl_s=0, now=T0)
    lid = leases.create_hold(conn, holder="admin", pool=POOL, ttl_s=28800, now=T0)
    assert inst(conn, "a")["max_lifetime_at"] == T0 + 28800
    assert leases.live_count(conn, POOL, T0, [LeaseKind.HOLD]) == 1
    # a short hold never shortens the lifetime
    leases.create_hold(conn, holder="admin", pool=POOL, ttl_s=600, now=T0)
    assert inst(conn, "a")["max_lifetime_at"] == T0 + 28800
    # renew cannot push a hold beyond created_at + 8 h
    assert leases.renew(conn, lid, T0 + 28000) == T0 + 28800


def test_hold_max_follows_config() -> None:
    conn = make_conn()
    cfg = LeaseConfig(hold_max_ttl_s=3600)
    with pytest.raises(LeaseError):
        leases.create_hold(conn, holder="a", pool=POOL, ttl_s=3601, now=T0, cfg=cfg)


def test_close_final_marks_drain_and_is_idempotent() -> None:
    conn = make_conn()
    lid = leases.create_explicit(conn, holder="a", client_id=None, pool=POOL, now=T0)
    assert not leases.drain_requested(conn, POOL, T0)
    leases.close(conn, lid, T0 + 100, final=True, reason="batch_done")
    leases.close(conn, lid, T0 + 200)  # second close changes nothing
    row = conn.execute("SELECT * FROM leases WHERE id=?", (lid,)).fetchone()
    assert (row["closed_at"], row["final"], row["close_reason"]) == (T0 + 100, 1, "batch_done")
    assert leases.drain_requested(conn, POOL, T0)
    assert not leases.drain_requested(conn, POOL, T0 + 101)  # older than the idle period
    assert not leases.drain_requested(conn, "other", T0)


def test_release_holds_by_pool_and_all() -> None:
    conn = make_conn()
    leases.create_hold(conn, holder="a", pool=POOL, ttl_s=600, now=T0)
    leases.create_hold(conn, holder="a", pool="q", ttl_s=600, now=T0)
    assert leases.release_holds(conn, POOL, T0 + 1, "released") == 1
    assert leases.live_count(conn, POOL, T0 + 1) == 0 and leases.live_count(conn, "q", T0 + 1) == 1
    assert leases.release_holds(conn, None, T0 + 2, "kill") == 1
    assert leases.hold_ended_since(conn, "q", T0)


def test_live_count_by_pool_and_kind() -> None:
    conn = make_conn()
    add_job(conn, "j1")
    leases.create_explicit(conn, holder="a", client_id=None, pool=POOL, now=T0)
    leases.create_explicit(conn, holder="a", client_id=None, pool="q", now=T0)
    assert leases.live_count(conn, POOL, T0) == 2
    assert leases.live_count(conn, POOL, T0, [LeaseKind.IMPLICIT]) == 1
    assert leases.live_count(conn, "q", T0) == 1
