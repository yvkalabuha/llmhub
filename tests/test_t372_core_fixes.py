"""T-372 C1-C4: list_offers failure reason, no second create after an unknown outcome,
blacklisted host after create, weights token TTL (Mock provider, FakeClock)."""

from __future__ import annotations

import dataclasses
from pathlib import Path

from f_fixtures import cfg_with, edges
from harness import Harness, make_job
from llmhub.cloud_manager.instance_env import build_material
from llmhub.lifecycle_manager.recovery import reconcile_on_start
from llmhub.lifecycle_manager.safety import blacklist_host
from llmhub.provider_adapters.base import InstanceSpec, RemoteResource
from llmhub.provider_adapters.mock import Fault, MockKnobs, MockProvider
from llmhub.weights.tokens import token_expiry
from test_c_support import FakeCloud, FakeInference, add_instance, inst, make_cfg, make_conn, trail
from w_fixtures import CA_PEMS, SECRETS, inst_row, weights_cfg


def _jobs_fallback(h: Harness) -> list[str | None]:
    return [j["fallback_reason"] for j in h.jobs()]


# --------------------------------------------------------------------- C1


async def test_list_offers_failure_fails_with_provider_api_error(tmp_path: Path) -> None:
    cfg = cfg_with(tmp_path, timeouts={"offer_wait_s": 300},
                   retry={"breaker_errors": 1000})  # fmt: skip
    h = Harness(tmp_path, cfg)
    h.provider.inject("list_offers", Fault.API_5XX, times=10_000)
    t0 = h.clock.now()
    h.submit(make_job(0))
    await h.run_until(t0 + 60)
    assert h.instances()[0]["last_error"] == "provider_api_error"
    await h.run_until(t0 + 400)
    assert ("PROVISIONING", "ERROR", "provider_api_error") in h.transitions()
    assert _jobs_fallback(h) == ["provider_api_error"] and h.provider.create_calls == 0


async def test_breaker_open_until_offer_wait_is_provider_api_error(tmp_path: Path) -> None:
    cfg = cfg_with(tmp_path, timeouts={"offer_wait_s": 300})
    h = Harness(tmp_path, cfg)
    h.provider.inject("list_offers", Fault.API_5XX, times=10_000)
    t0 = h.clock.now()
    h.submit(make_job(0))
    await h.run_until(t0 + 400)
    assert ("PROVISIONING", "ERROR", "provider_api_error") in h.transitions()
    assert _jobs_fallback(h) == ["provider_api_error"]


async def test_no_offer_stays_gpu_unavailable(tmp_path: Path) -> None:
    cfg = cfg_with(tmp_path, timeouts={"offer_wait_s": 300})
    h = Harness(tmp_path, cfg, knobs=MockKnobs(gpu_available=False))
    t0 = h.clock.now()
    h.submit(make_job(0))
    await h.run_until(t0 + 400)
    assert _jobs_fallback(h) == ["gpu_unavailable"]


# --------------------------------------------------------------------- C2 (F1)


def _f1_cfg(tmp_path: Path, grace: float = 600) -> object:
    return cfg_with(tmp_path, timeouts={"create_visibility_grace_s": grace,
                                        "provisioning_s": 1200},
                    retry={"create_unknown_max_retries": 0})  # fmt: skip


async def test_unknown_create_never_repeated_resolves_to_failed(tmp_path: Path) -> None:
    h = Harness(tmp_path, _f1_cfg(tmp_path))  # type: ignore[arg-type]
    h.provider.inject("create", Fault.CREATE_TIMEOUT_NOT_CREATED, times=5)
    t0 = h.clock.now()
    h.submit(make_job(0))
    await h.run_until(t0 + 590)
    assert h.state_of() == "PROVISIONING" and h.provider.create_calls == 1
    await h.run_until(t0 + 700)
    assert h.provider.create_calls == 1
    assert ("PROVISIONING", "ERROR", "create_unknown_unresolved") in h.transitions()
    (inc,) = h.incidents("create_unknown_unresolved")
    assert inc["severity"] == "high"
    await h.run_until(t0 + 3000)
    assert h.provider.create_calls == 1 and h.provider.live_refs() == []
    assert h.state_of() == "STOPPED"


async def test_unknown_create_visible_late_is_adopted_not_repeated(tmp_path: Path) -> None:
    knobs = MockKnobs(late_visible_s=150)
    h = Harness(tmp_path, _f1_cfg(tmp_path), knobs=knobs)  # type: ignore[arg-type]
    h.provider.inject("create", Fault.CREATE_TIMEOUT_CREATED_LATE)
    t0 = h.clock.now()
    h.submit(make_job(0))
    await h.run_until(t0 + 2000)
    assert h.provider.create_calls == 1
    assert h.jobs()[0]["status"] == "completed" and h.jobs()[0]["route"] == "cloud"
    assert h.incidents("create_unknown_unresolved") == []


async def test_default_keeps_one_retry_for_mock(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.provider.inject("create", Fault.CREATE_TIMEOUT_NOT_CREATED)
    t0 = h.clock.now()
    h.submit(make_job(0))
    await h.run_until(t0 + 300)
    assert h.provider.create_calls == 2  # create_unknown_max_retries = 1 (default)


async def test_recovery_after_restart_does_not_repeat_create() -> None:
    cfg = make_cfg()
    cfg = cfg.model_copy(update={"retry": cfg.retry.model_copy(
        update={"create_unknown_max_retries": 0})})  # fmt: skip
    conn, cloud = make_conn(), FakeCloud()
    from test_c_support import T0

    add_instance(conn, "a", "PROVISIONING", create_called_at=T0, attempts=1)
    now = T0 + cfg.timeouts.create_visibility_grace_s + 1
    await reconcile_on_start(conn, {"fake": cloud}, {"fake": FakeInference()}, b"s" * 32, now,
                             cfg, "boot-2")  # type: ignore[arg-type]  # fmt: skip
    assert inst(conn, "a")["create_called_at"] == T0  # not cleared for a retry
    assert trail(conn)[0][2] == "create_unknown_unresolved"
    n = conn.execute("SELECT count(*) FROM incidents WHERE kind='create_unknown_unresolved'"
                     " AND severity='high'").fetchone()[0]  # fmt: skip
    assert n == 1


# --------------------------------------------------------------------- C3


class BadHostOnce(MockProvider):
    """The first create lands on a blacklisted machine (RunPod picks the host itself)."""

    bad = 1

    async def create(self, spec: InstanceSpec, label: str) -> RemoteResource:
        res = await super().create(spec, label)
        if self.bad > 0:
            self.bad -= 1
            return dataclasses.replace(res, host_id="host-bad")
        return res


async def test_blacklisted_host_after_create_is_destroyed_and_retried(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.provider = BadHostOnce(h.provider.store, h.clock, h.provider.knobs)
    h.inference.provider = h.provider
    h.controller = h._make_controller()
    blacklist_host(h.conn, "mock", "host-bad", h.clock.now(), 86400, "test")
    t0 = h.clock.now()
    h.submit(make_job(0))
    await h.run_until(t0 + 600)
    assert h.provider.create_calls == 2
    assert len(h.incidents("blacklisted_host_assigned")) == 1
    job = h.jobs()[0]
    assert (job["status"], job["route"]) == ("completed", "cloud")
    assert h.instances()[0]["host_id"] != "host-bad"
    await h.run_until(t0 + 3000)
    assert h.provider.live_refs() == []


async def test_blacklisted_host_twice_fails_row(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    p = BadHostOnce(h.provider.store, h.clock, h.provider.knobs)
    p.bad = 5
    h.provider = p
    h.inference.provider = p
    h.controller = h._make_controller()
    blacklist_host(h.conn, "mock", "host-bad", h.clock.now(), 86400, "test")
    t0 = h.clock.now()
    h.submit(make_job(0))
    await h.run_until(t0 + 3000)
    assert p.create_calls == 2  # create_max_attempts
    assert ("PROVISIONING", "ERROR", "blacklisted_host") in h.transitions()
    assert p.live_refs() == [] and h.state_of() == "STOPPED"
    assert ("STOPPED", "PROVISIONING") in edges(h)


# --------------------------------------------------------------------- C4 (F9)


def test_weights_token_lives_at_most_30_min(tmp_path: Path) -> None:
    cfg = weights_cfg(tmp_path)
    t = cfg.timeouts.model_copy(update={"provisioning_s": 600, "booting_s": 600,
                                        "loading_model_s": 1200})  # fmt: skip
    cfg = cfg.model_copy(update={"timeouts": t})
    row = inst_row(created_at=1_762_000_000.0)
    assert token_expiry(row["created_at"], cfg.timeouts) - row["created_at"] == 2400  # 40 min
    m = build_material(row, cfg.pools["p1"], cfg, SECRETS, CA_PEMS)
    exp = int(m.secret_env["LLMHUB_WEIGHTS_TOKEN"].split(".")[2])
    assert exp - row["created_at"] <= 1800
    short = cfg.timeouts.model_copy(update={"provisioning_s": 60, "booting_s": 60,
                                            "loading_model_s": 60})  # fmt: skip
    m2 = build_material(row, cfg.pools["p1"], cfg.model_copy(update={"timeouts": short}),
                        SECRETS, CA_PEMS)  # fmt: skip
    assert int(m2.secret_env["LLMHUB_WEIGHTS_TOKEN"].split(".")[2]) == 1_762_000_180
