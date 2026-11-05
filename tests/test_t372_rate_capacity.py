"""T-372 L2b (2026-11-05): REST 429 / POST budget (limiter, adapter, GuardedAdapter, teardown),
the Community stock pre-check and the capacity wait of the provisioner (fakes, no network)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from f_fixtures import cfg_with
from harness import Harness, make_job
from llmhub.clock import FakeClock
from llmhub.cloud_manager import provisioner as prov
from llmhub.configuration.models import RetryConfig
from llmhub.persistence.db import connect, migrate
from llmhub.provider_adapters import rate_limit
from llmhub.provider_adapters.base import (
    InstanceSpec,
    NormalizedError,
    ProviderError,
    RemoteResource,
    UnavailableReason,
)
from llmhub.provider_adapters.guarded import CircuitBreaker, GuardedAdapter
from llmhub.provider_adapters.labels import make_label, new_op_id
from llmhub.provider_adapters.mock import MockKnobs, MockProvider
from llmhub.provider_adapters.runpod import RunpodAdapter, normalize_runpod_error
from llmhub.provider_adapters.runpod_api import RunpodApi, RunpodHttpError, classify
from runpod_fake import FAKE_KEY, GPU_ID, FakeRunpod, make_spec, runpod_cfg

LIMIT_BODY = '{"error":"rate limit exceeded for the hour window","status":429}'
SECRET = b"s" * 32
G4090 = "NVIDIA GeForce RTX 4090"


def _label() -> str:
    return make_label(new_op_id(), SECRET)


def _limited(fake: FakeRunpod, **lim: Any) -> tuple[RunpodAdapter, rate_limit.RestLimiter]:
    limiter = rate_limit.RestLimiter(fake.clock.now, **lim)
    api = RunpodApi(lambda: FAKE_KEY, transport=fake.transport(), limiter=limiter)
    return RunpodAdapter(api, runpod_cfg(), fake.clock, disk_gb_hint=40), limiter


def _rest(fake: FakeRunpod) -> list[tuple[str, str]]:
    return [(r.method, r.url.path) for r in fake.requests if r.url.host == "rest.runpod.io"]


# ------------------------------------------------------------------ classification
def test_429_is_rate_limited_and_a_definite_refusal() -> None:
    assert classify(LIMIT_BODY, status=429) == "rate_limited"
    exc = RunpodHttpError(method="POST", path="/pods", status=429, code="rate_limited",
                          retry_after_s=900.0, sent=True)  # fmt: skip
    err = normalize_runpod_error(exc)
    assert err.rate_limited and err.retryable and not err.outcome_unknown
    assert err.reason == UnavailableReason.PROVIDER_API_ERROR and err.retry_after_s == 900.0
    local = RunpodHttpError(method="POST", path="/pods", status=None, code="post_budget",
                            retry_after_s=10.0, sent=False)  # fmt: skip
    assert normalize_runpod_error(local).rate_limited
    assert not normalize_runpod_error(local).outcome_unknown


async def test_429_on_post_stops_create_after_one_post() -> None:
    fake = FakeRunpod(FakeClock())
    fake.inject("POST", "/pods", "status", status=429, body=LIMIT_BODY)
    adapter = RunpodAdapter(fake.api(), runpod_cfg(), fake.clock, disk_gb_hint=40)
    with pytest.raises(ProviderError) as ei:
        await adapter.create(make_spec(), _label())
    assert ei.value.err.rate_limited and not ei.value.err.outcome_unknown
    assert fake.posts() == 1 and adapter.last_create_attempts == [(GPU_ID, "13.0", "rate_limited")]


# ------------------------------------------------------------------ limiter
async def test_hold_after_429_blocks_post_trickles_reads_and_passes_delete() -> None:
    clock = FakeClock()
    fake = FakeRunpod(clock)
    adapter, lim = _limited(fake)
    pid = fake.add_pod("llmhub-0123456789ab-01234567")
    fake.inject("GET", "/pods", "status", status=429, body=LIMIT_BODY)
    with pytest.raises(ProviderError) as ei:
        await adapter.list_owned()
    assert ei.value.err.rate_limited and ei.value.err.retry_after_s == pytest.approx(900)
    assert lim.held() and lim.hold_until == pytest.approx(clock.now() + 900)
    n = len(_rest(fake))
    with pytest.raises(ProviderError) as ei:  # create: refused locally, nothing sent
        await adapter.create(make_spec(), _label())
    assert ei.value.err.rate_limited and fake.posts() == 0
    with pytest.raises(ProviderError):  # a read within the trickle window: nothing sent
        await adapter.list_owned()
    assert len(_rest(fake)) == n
    assert (await adapter.destroy(pid)).value == "accepted"  # DELETE always goes out
    assert _rest(fake)[-1][0] == "DELETE"
    clock.advance_to(clock.now() + 61)
    n = len(_rest(fake))
    await adapter.list_owned()  # one burst a minute: all 3 GETs of list_owned go out
    assert len(_rest(fake)) == n + 3
    assert lim.held()  # a 200 inside the window does not end the hold (live 01:00)
    clock.advance_to(clock.now() + 6)  # the burst is closed
    with pytest.raises(ProviderError):
        await adapter.list_owned()
    clock.advance_to(clock.now() + 900)
    assert not lim.held()
    await adapter.create(make_spec(), _label())  # max_live_pods: the pod above was deleted


def test_retry_after_header_and_post_budget() -> None:
    clock = FakeClock()
    lim = rate_limit.RestLimiter(clock.now, post_budget_per_hour=2)
    assert lim.on_response("GET", 429, 120.0) == pytest.approx(120)
    assert lim.admit("POST", "/pods") == "rate_limited"
    clock.advance_to(clock.now() + 121)
    assert lim.admit("POST", "/pods") is None and lim.admit("POST", "/pods") is None
    assert lim.admit("POST", "/pods") == "post_budget"
    assert lim.wait_s("post_budget") == pytest.approx(3600)
    clock.advance_to(clock.now() + 3600)
    assert lim.admit("POST", "/pods") is None and lim.posts_last_hour() == 1


async def test_post_budget_refuses_without_http() -> None:
    fake = FakeRunpod(FakeClock(), cuda_capacity=set())
    adapter, _ = _limited(fake, post_budget_per_hour=2)
    with pytest.raises(ProviderError) as ei:
        await adapter.create(make_spec(), _label())
    assert fake.posts() == 2 and ei.value.err.rate_limited
    assert [r for *_, r in adapter.last_create_attempts] == ["no_capacity", "no_capacity",
                                                             "post_budget"]  # fmt: skip


def test_export_absorb_newest_hold_wins_posts_union() -> None:
    clock = FakeClock()
    a, b = rate_limit.RestLimiter(clock.now), rate_limit.RestLimiter(clock.now)
    a.on_response("GET", 429, None)
    a.admit("GET", "/pods")
    b.absorb(a.export())
    assert b.held()
    clock.advance_to(clock.now() + 61)
    b.on_response("GET", 429, 30.0)  # a shorter Retry-After never shortens a hold
    assert b.hold_until == a.hold_until
    b.hold_until, b.hold_cause, b.hold_changed_at = 0.0, None, clock.now()  # newer decision
    b.posts.append(clock.now() - 10)
    a.absorb(b.export())
    assert not a.held() and len(a.posts) == 1
    a.absorb({"hold_until": "x"})  # unreadable: ignored
    assert not a.held()


def test_db_state_create_blocked_and_status_view(tmp_path: Path) -> None:
    conn = connect(tmp_path / "s.db")
    migrate(conn)
    clock = FakeClock()
    now = clock.now()
    assert rate_limit.create_blocked(conn, "runpod", now) is None
    assert rate_limit.status_view(conn, "runpod", now) is None
    lim = rate_limit.RestLimiter(clock.now, post_budget_per_hour=1)
    lim.admit("POST", "/pods")
    rate_limit.sync(conn, "runpod", lim, now)
    until, cause = rate_limit.create_blocked(conn, "runpod", now) or (0.0, "")
    assert cause == "post_budget" and until == pytest.approx(now + 3600)
    rate_limit.set_hold(conn, "runpod", now + 300, "rate_limited", now)
    assert rate_limit.create_blocked(conn, "runpod", now) == (now + 300, "rate_limited")
    view = rate_limit.status_view(conn, "runpod", now)
    assert view == {"held": True, "hold_until": now + 300, "hold_cause": "rate_limited",
                    "posts_last_hour": 1, "post_budget_per_hour": 1}  # fmt: skip


# ------------------------------------------------------------------ GuardedAdapter
def _guarded(adapter: Any, tmp_path: Path, clock: FakeClock) -> tuple[GuardedAdapter, Any]:
    conn = connect(tmp_path / "g.db")
    migrate(conn)
    retry = RetryConfig(breaker_errors=1)
    breaker = CircuitBreaker(conn, adapter.name, clock, retry)
    return GuardedAdapter(adapter, clock, retry, breaker), conn


async def test_guarded_read_429_not_retried_not_counted_and_shared(tmp_path: Path) -> None:
    clock = FakeClock()
    fake = FakeRunpod(clock)
    adapter, _ = _limited(fake)
    g, conn = _guarded(adapter, tmp_path, clock)
    fake.inject("GET", "/pods", "status", status=429, body=LIMIT_BODY)
    t0 = clock.now()
    with pytest.raises(ProviderError) as ei:
        await g.list_owned()
    assert ei.value.err.rate_limited and clock.now() == t0  # no 900 s sleep in a retry
    assert g.breaker.state == "closed"  # never opens the breaker
    blocked = rate_limit.create_blocked(conn, "runpod", clock.now())
    assert blocked is not None and blocked[1] == "rate_limited"


async def test_guarded_absorbs_hold_of_another_process(tmp_path: Path) -> None:
    clock = FakeClock()
    fake = FakeRunpod(clock)
    adapter, lim = _limited(fake)
    g, conn = _guarded(adapter, tmp_path, clock)
    rate_limit.set_hold(conn, "runpod", clock.now() + 600, "http_429", clock.now())
    with pytest.raises(ProviderError):
        await g.create(make_spec(), _label())
    assert fake.posts() == 0 and lim.held()


class _NoLimiter(MockProvider):
    async def get(self, ref: str) -> RemoteResource | None:
        raise ProviderError(NormalizedError(
            reason=UnavailableReason.PROVIDER_API_ERROR, retryable=True, outcome_unknown=False,
            retry_after_s=None, http_status=429, message="rate limited", rate_limited=True))  # fmt: skip


async def test_guarded_sets_hold_for_adapter_without_limiter(tmp_path: Path) -> None:
    from llmhub.provider_adapters.mock_store import MockStore

    clock = FakeClock()
    g, conn = _guarded(_NoLimiter(MockStore(":memory:"), clock), tmp_path, clock)
    with pytest.raises(ProviderError):
        await g.get("x")
    blocked = rate_limit.create_blocked(conn, "mock", clock.now())
    assert blocked == (pytest.approx(clock.now() + 900), "rate_limited")


# ------------------------------------------------------------------ stock pre-check
async def test_stock_precheck_skips_types_without_stock() -> None:
    gpus: dict[str, float | None] = {GPU_ID: 0.22, G4090: 0.34, "NVIDIA RTX A2000": 0.12}
    fake = FakeRunpod(FakeClock(), gpus=gpus, community_stock={G4090: "Low",
                                                               "NVIDIA RTX A2000": "High"})
    a = RunpodAdapter(fake.api(), runpod_cfg(gpu_type_ids={"RTX24G": [GPU_ID, G4090]},
                                             price_ceiling_usd_h=0.5),
                      fake.clock, disk_gb_hint=40)  # fmt: skip
    res = await a.create(make_spec(offer_id=GPU_ID, max_price_usd_h=0.35), _label())
    assert res.offer_id == G4090 and fake.posts() == 1
    assert a.last_create_attempts == [(GPU_ID, "", "no_stock"), (G4090, "13.0", "created")]


async def test_no_stock_in_class_means_no_post_definite() -> None:
    fake = FakeRunpod(FakeClock(), gpus={GPU_ID: 0.22, "NVIDIA RTX A2000": 0.12},
                      community_stock={"NVIDIA RTX A2000": "Low"})  # fmt: skip
    a = RunpodAdapter(fake.api(), runpod_cfg(), fake.clock, disk_gb_hint=40)
    with pytest.raises(ProviderError) as ei:
        await a.create(make_spec(), _label())
    err = ei.value.err
    assert err.reason == UnavailableReason.GPU_UNAVAILABLE and not err.outcome_unknown
    assert fake.posts() == 0 and "no community stock" in err.message
    assert _rest(fake) == []  # the stock check runs before the REST pod count


@pytest.mark.parametrize("case", ["all_null", "graphql_down", "disabled"])
async def test_stock_precheck_never_blocks_without_information(case: str) -> None:
    fake = FakeRunpod(FakeClock(), community_stock=None if case == "all_null" else "High")
    if case == "graphql_down":
        fake.community_stock = None
        fake.inject("POST", "graphql:gpuTypes", "status", status=503, times=1)
    cfg = runpod_cfg(stock_precheck=case != "disabled")
    a = RunpodAdapter(fake.api(), cfg, fake.clock, disk_gb_hint=40)
    if case == "graphql_down":  # the price query comes first: let it pass, fail the stock one
        orig = a.api.community_stock

        async def down(ids: Any) -> Any:
            raise RunpodHttpError(method="POST", path="graphql:gpuStock", status=503,
                                  code=None, retry_after_s=None, sent=True)  # fmt: skip

        a.api.community_stock = down  # type: ignore[method-assign]
        fake.faults.clear()
        assert orig is not None
    res = await a.create(make_spec(), _label())
    assert res.ref and fake.posts() == 1
    stock_queries = [r for r in fake.requests if r.url.host == "api.runpod.io"
                     and b"secureCloud" in r.content]  # fmt: skip
    assert (stock_queries == []) == (case != "all_null")


# ------------------------------------------------------------------ teardown cap
async def test_rate_limited_destroy_waits_at_most_backoff_cap() -> None:
    from llmhub.lifecycle_manager.teardown import _destroy_refs
    from test_c_support import T0, add_instance, inst, make_cfg, make_conn

    class _Cloud:
        name = "fake"

        async def destroy(self, ref: str) -> Any:
            raise ProviderError(NormalizedError(
                reason=UnavailableReason.PROVIDER_API_ERROR, retryable=True,
                outcome_unknown=False, retry_after_s=900.0, http_status=429,
                message="rate limited", rate_limited=True))  # fmt: skip

        def normalize_error(self, exc: BaseException) -> NormalizedError:
            assert isinstance(exc, ProviderError)
            return exc.err

    conn, cfg = make_conn(), make_cfg()
    add_instance(conn, "a", "STOPPING", provider_ref="r1")
    ok = await _destroy_refs(conn, _Cloud(), inst(conn, "a"), ["r1"], T0, cfg)  # type: ignore[arg-type]
    assert not ok
    assert inst(conn, "a")["next_destroy_at"] == T0 + cfg.retry.destroy_backoff_max_s


# ------------------------------------------------------------------ capacity wait
def test_capacity_backoff_steps_and_jitter() -> None:
    cfg = cfg_with(Path("/nonexistent"))
    for rnd, base in ((1, 120), (2, 300), (3, 600), (7, 600)):
        v = prov.capacity_backoff_s(cfg, "p", rnd, 1000.0)
        assert base * 0.8 <= v <= base * 1.2
        assert v == prov.capacity_backoff_s(cfg, "p", rnd, 1000.0)  # deterministic
    assert prov.capacity_backoff_s(cfg, "p", 1, 1000.0) != prov.capacity_backoff_s(cfg, "q", 1,
                                                                                    1000.0)
    with pytest.raises(ValueError, match="capacity_backoff_s"):
        RetryConfig(capacity_backoff_s=[0.0])


class _NoCapacity(MockProvider):
    """Every create is a definite "no capacity" until `capacity_at`."""

    capacity_at = float("inf")

    async def create(self, spec: InstanceSpec, label: str) -> RemoteResource:
        if self.clock.now() < self.capacity_at:
            self.create_calls += 1
            raise ProviderError(NormalizedError(
                reason=UnavailableReason.GPU_UNAVAILABLE, retryable=True, outcome_unknown=False,
                retry_after_s=None, http_status=500, message="no_capacity"))  # fmt: skip
        return await super().create(spec, label)


def _cap_harness(tmp_path: Path, offers: int = 1, wait: float = 3600) -> Harness:
    cfg = cfg_with(tmp_path, timeouts={"offer_wait_s": wait, "provisioning_s": wait + 600})
    h = Harness(tmp_path, cfg, knobs=MockKnobs(offers=offers))
    nc = _NoCapacity(h.provider.store, h.clock, h.provider.knobs)
    h.provider = nc
    h.inference.provider = nc  # type: ignore[attr-defined]
    h.controller = h._make_controller()
    return h


async def test_capacity_wait_backs_off_between_rounds_then_resets(tmp_path: Path) -> None:
    h = _cap_harness(tmp_path, offers=2)
    p = h.provider
    assert isinstance(p, _NoCapacity)
    t0 = h.clock.now()
    h.submit(make_job(0))
    await h.run_until(t0 + 60)
    assert p.create_calls == 2  # both offers of round 1 (one per host), no immediate repeat
    pool = h.instances()[0]["pool"]
    view = prov.capacity_wait_view(h.conn, pool, h.clock.now())
    assert view is not None and view["round"] == 1 and view["waiting"]
    assert 96 <= view["until"] - t0 <= 150
    assert h.instances()[0]["last_error"] == "capacity_wait round 1"
    await h.run_until(view["until"] - 1)
    assert p.create_calls == 2  # nothing during the wait
    await h.run_until(view["until"] + 30)
    assert p.create_calls == 4
    v2 = prov.capacity_wait_view(h.conn, pool, h.clock.now())
    assert v2 is not None and v2["round"] == 2 and 240 <= v2["until"] - h.clock.now() <= 360
    p.capacity_at = v2["until"]
    await h.run_until(v2["until"] + 600)
    assert prov.capacity_wait_view(h.conn, pool, h.clock.now()) is None  # created: reset
    assert h.jobs()[0]["status"] == "completed" and h.jobs()[0]["route"] == "cloud"


async def test_capacity_wait_ends_at_offer_wait_with_gpu_unavailable(tmp_path: Path) -> None:
    h = _cap_harness(tmp_path, wait=300)
    t0 = h.clock.now()
    h.submit(make_job(0))
    await h.run_until(t0 + 400)
    assert ("PROVISIONING", "ERROR", "gpu_unavailable") in h.transitions()
    assert h.provider.create_calls <= 3


async def test_rate_hold_blocks_rounds_without_any_call(tmp_path: Path) -> None:
    h = _cap_harness(tmp_path, wait=600)
    p = h.provider
    assert isinstance(p, _NoCapacity)
    t0 = h.clock.now()
    rate_limit.set_hold(h.conn, "mock", t0 + 300, "rate_limited", t0)
    p.capacity_at = 0.0
    h.submit(make_job(0))
    await h.run_until(t0 + 290)
    assert p.create_calls == 0 and h.instances()[0]["last_error"] == "rate_limited"
    await h.run_until(t0 + 900)
    assert p.create_calls == 1 and h.jobs()[0]["status"] == "completed"


async def test_rate_hold_until_offer_wait_is_provider_api_error(tmp_path: Path) -> None:
    h = _cap_harness(tmp_path, wait=300)
    t0 = h.clock.now()
    rate_limit.set_hold(h.conn, "mock", t0 + 3000, "rate_limited", t0)
    h.submit(make_job(0))
    await h.run_until(t0 + 400)
    assert ("PROVISIONING", "ERROR", "provider_api_error") in h.transitions()
    assert h.provider.create_calls == 0


async def test_rate_limited_create_refunds_the_attempt(tmp_path: Path) -> None:
    from test_c_support import T0, add_instance, inst, make_cfg, make_conn

    conn, cfg = make_conn(), make_cfg()
    add_instance(conn, "a", "PROVISIONING", create_called_at=T0, attempts=1)
    err = NormalizedError(reason=UnavailableReason.PROVIDER_API_ERROR, retryable=True,
                          outcome_unknown=False, retry_after_s=900, http_status=429,
                          message="POST /pods -> 429 rate_limited", rate_limited=True)  # fmt: skip
    for _ in range(3):
        out = prov._create_failed(conn, inst(conn, "a"), err, "", cfg, T0)
        assert out == prov.CreateOutcome.RETRY_LATER
    assert inst(conn, "a")["attempts"] == 0 and inst(conn, "a")["state"] == "PROVISIONING"


def test_old_capacity_wait_starts_over(tmp_path: Path) -> None:
    conn = connect(tmp_path / "s.db")
    migrate(conn)
    cfg = cfg_with(tmp_path)
    u1 = prov.start_capacity_wait(conn, "p", "i1", 1000.0, cfg)
    u2 = prov.start_capacity_wait(conn, "p", "i1", u1 + 1, cfg)
    assert prov.capacity_wait_view(conn, "p", u2 - 1)["round"] == 2  # type: ignore[index]
    prov.start_capacity_wait(conn, "p", "i1", u2 + 3 * 600, cfg)
    assert prov.capacity_wait_view(conn, "p", u2)["round"] == 1  # type: ignore[index]

